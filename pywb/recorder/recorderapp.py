import json
import tempfile
import traceback

import gevent
import gevent.queue
import requests
import six
from six.moves.urllib.parse import parse_qsl
from warcio.recordloader import ArcWarcRecordLoader

from pywb.recorder.filters import CollectionFilter, SkipRangeRequestFilter
from pywb.utils.format import ParamFormatter
from pywb.utils.io import BUFF_SIZE, StreamIter, no_except_close
from pywb.warcserver.inputrequest import DirectWSGIInputRequest


# ==============================================================================
class RecorderApp(object):
    def __init__(self, upstream_host, writer, skip_filters=None, **kwargs):
        self.upstream_host = upstream_host

        self.writer = writer

        self.rec_source_name = kwargs.get('name', 'recorder')

        self.create_buff_func = kwargs.get('create_buff_func') or self.default_create_buffer

        self.write_queue = gevent.queue.Queue()
        gevent.spawn(self._write_loop)

        if not skip_filters:
            skip_filters = self.create_default_filters(kwargs)

        self.skip_filters = skip_filters

    @staticmethod
    def create_default_filters(kwargs):
        skip_filters = [SkipRangeRequestFilter()]

        accept_colls = kwargs.get('accept_colls')
        if accept_colls:
            skip_filters.append(CollectionFilter(accept_colls))

        return skip_filters

    @staticmethod
    def default_create_buffer(params, name):
        return tempfile.SpooledTemporaryFile(max_size=512 * 1024)

    def _write_loop(self):
        while True:
            try:
                self._write_one()
            except Exception:
                traceback.print_exc()

    def _write_one(self):
        req_pay = None
        resp_pay = None
        try:
            result = self.write_queue.get()

            req_head, req_pay, resp_head, resp_pay, params = result

            resp_length = resp_pay.tell()
            resp_pay.seek(0)
            resp = ArcWarcRecordLoader().parse_record_stream(resp_pay)

            if resp.rec_type == 'response':
                uri = resp.rec_headers.get_header('WARC-Target-Uri')
                req_length = req_pay.tell()
                req_pay.seek(0)
                req = self.writer.create_warc_record(uri=uri,
                                                     record_type='request',
                                                     payload=req_pay,
                                                     length=req_length,
                                                     warc_headers_dict=req_head)

                self.writer.write_request_response_pair(req, resp, params)

            else:
                self.writer.write_record(resp, params)

        finally:
            try:
                if req_pay:
                    no_except_close(req_pay)

                if resp_pay:
                    no_except_close(resp_pay)
            except Exception as e:
                traceback.print_exc()

    def send_error(self, exc, start_response):
        return self.send_message({'error': repr(exc)},
                                 '400 Bad Request',
                                 start_response)

    def send_message(self, msg, status, start_response):
        message = json.dumps(msg)
        headers = [('Content-Type', 'application/json; charset=utf-8'),
                   ('Content-Length', str(len(message)))]

        start_response(status, headers)
        return [message.encode('utf-8')]

    def _put_record(self, request_uri, input_buff, record_type,
                    headers, params, start_response):

        if record_type == 'stream':
            if self.writer.write_stream_to_file(params, input_buff):
                msg = {'success': 'true'}
            else:
                msg = {'error_message': 'upload_error'}

            return self.send_message(msg, '200 OK',
                                     start_response)

        req_stream = None
        try:
            req_stream = ReqWrapper(input_buff,
                                    headers,
                                    params,
                                    self.create_buff_func)

            while True:
                buff = req_stream.read()
                if not buff:
                    break

            content_type = headers.get('Content-Type')

            payload_length = req_stream.out.tell()
            req_stream.out.seek(0)

            record = self.writer.create_warc_record(uri=params['url'],
                                                    record_type=record_type,
                                                    payload=req_stream.out,
                                                    length=payload_length,
                                                    warc_content_type=content_type,
                                                    warc_headers_dict=req_stream.headers)

            self.writer.write_record(record, params)

            msg = {'success': 'true',
                   'WARC-Date': record.rec_headers.get_header('WARC-Date')}

        finally:
            if req_stream:
                no_except_close(req_stream.out)

        return self.send_message(msg,
                                 '200 OK',
                                 start_response)

    def _get_params(self, environ):
        params = dict(parse_qsl(environ.get('QUERY_STRING')))
        params['_formatter'] = ParamFormatter(params, name=self.rec_source_name)
        return params

    def __call__(self, environ, start_response):
        try:
            return self.handle_call(environ, start_response)
        except Exception:
            traceback.print_exc()

    def handle_call(self, environ, start_response):
        input_req = DirectWSGIInputRequest(environ)

        params = self._get_params(environ)

        request_uri = input_req.get_full_request_uri()

        input_buff = input_req.get_req_body()

        headers = input_req.get_req_headers()

        method = input_req.get_req_method()

        path = environ['PATH_INFO']

        # write request body as metadata/resource
        put_record = params.get('put_record')
        if put_record and method in ('PUT', 'POST'):
            return self._put_record(request_uri,
                                    input_buff,
                                    put_record,
                                    headers,
                                    params,
                                    start_response)

        skipping = any(x.skip_request(path, headers) for x in self.skip_filters)

        req_is_wrapped = False

        if not skipping:
            req_stream = ReqWrapper(input_buff,
                                    headers,
                                    params,
                                    self.create_buff_func)
            req_is_wrapped = True
        else:
            req_stream = input_buff

        data = None
        if input_buff:
            data = req_stream

        try:
            res = requests.request(url=self.upstream_host + request_uri,
                                   method=method,
                                   data=data,
                                   headers=headers,
                                   allow_redirects=False,
                                   stream=True)
            res.raise_for_status()
        except Exception as e:
            if req_is_wrapped:
                no_except_close(req_stream.out)
            return self.send_error(e, start_response)

        if not skipping:
            skipping = any(x.skip_response(path,
                                           req_stream.headers,
                                           res.headers,
                                           params)
                           for x in self.skip_filters)

        if not skipping:
            resp_stream = RespWrapper(res.raw,
                                      res.headers,
                                      req_stream,
                                      params,
                                      self.write_queue,
                                      path,
                                      self.create_buff_func)

        else:
            resp_stream = res.raw
            if req_is_wrapped:
                no_except_close(req_stream.out)

        resp_iter = StreamIter(resp_stream)

        # ensure TE header from upstream is not included,
        # added automatically by wsgi app
        res.headers.pop('Transfer-Encoding', '')

        start_response('200 OK', list(res.headers.items()))
        return resp_iter


# ==============================================================================
class Wrapper(object):
    def __init__(self, stream, params, create_func):
        self.stream = stream
        self.params = params
        self.out = create_func(params, self.__class__.__name__)
        self.interrupted = False

    def read(self, *args, **kwargs):
        try:
            buff = self.stream.read(*args, **kwargs)
        except Exception as e:
            print('INTERRUPT READ')
            self.interrupted = True
            raise

        self.out.write(buff)
        return buff


# ==============================================================================
class RespWrapper(Wrapper):
    def __init__(self, stream, headers, req,
                 params, queue, path, create_func):

        super(RespWrapper, self).__init__(stream, params, create_func)
        self.headers = headers
        self.req = req
        self.queue = queue
        self.path = path

    def close(self):
        try:
            while True:
                if not self.read(BUFF_SIZE):
                    break

        except Exception as e:
            print(e)
            self.interrupted = True

        finally:
            try:
                self.stream.close()
            except Exception as e:
                traceback.print_exc()

            self._write_to_file()

    def _write_to_file(self):
        skipping = False
        try:
            if self.interrupted:
                skipping = True

            if not skipping:
                entry = (self.req.headers, self.req.out,
                         self.headers, self.out, self.params)
                self.queue.put(entry)
        except Exception:
            traceback.print_exc()
            skipping = True

        finally:
            if skipping:
                no_except_close(self.out)
                no_except_close(self.req.out)

            no_except_close(self.req)
            self.req = None


# ==============================================================================
class ReqWrapper(Wrapper):
    def __init__(self, stream, req_headers, params, create_func):
        super(ReqWrapper, self).__init__(stream, params, create_func)
        self.headers = {}

        for n in six.iterkeys(req_headers):
            if n.upper().startswith('WARC-'):
                self.headers[n] = req_headers[n]

    def close(self):
        # no need to close wsgi.input
        pass
