import os
import shutil
import sys
import logging
import heapq
import yaml
import re
import six

from distutils.util import strtobool
from pkg_resources import resource_string, get_distribution

from argparse import ArgumentParser, RawTextHelpFormatter

from pywb.utils.loaders import load_yaml_config
from warcio.timeutils import timestamp20_now

from pywb import DEFAULT_CONFIG

from six.moves import input


#=============================================================================
# to allow testing by mocking get_input


def get_input(msg):  # pragma: no cover
    return input(msg)

#=============================================================================
def get_version():
    """Get version of the pywb"""
    return "wb-manager " + get_distribution("pywb").version


#=============================================================================
class CollectionsManager(object):
    """ This utility is designed to
simplify the creation and management of web archive collections

It may be used via cmdline to setup and maintain the
directory structure expected by pywb
    """
    DEF_INDEX_FILE = 'index.cdxj'

    COLL_RX = re.compile('^[\w][-\w]*$')

    COLLS_DIR = 'collections'

    def __init__(self, coll_name, colls_dir=None, must_exist=True):
        colls_dir = colls_dir or self.COLLS_DIR
        self.default_config = load_yaml_config(DEFAULT_CONFIG)

        if coll_name and not self.COLL_RX.match(coll_name):
            raise ValueError('Invalid Collection Name: ' + coll_name)

        self.colls_dir = os.path.join(os.getcwd(), colls_dir)

        self.change_collection(coll_name)

        if must_exist:
            self._assert_coll_exists()

    def change_collection(self, coll_name):
        self.coll_name = coll_name
        self.curr_coll_dir = os.path.join(self.colls_dir, coll_name)

        self.archive_dir = self._get_dir('archive_paths')

        self.indexes_dir = self._get_dir('index_paths')
        self.static_dir = self._get_dir('static_path')
        self.templates_dir = self._get_dir('templates_dir')

        self.acl_dir = self._get_dir('acl_paths')

    def list_colls(self):
        print('Collections:')
        if not os.path.isdir(self.colls_dir):
            msg = ('"Collections" directory not found. ' +
                   'To create a new collection, run:\n\n{0} init <name>')
            raise IOError(msg.format(sys.argv[0]))
        for d in os.listdir(self.colls_dir):
            if os.path.isdir(os.path.join(self.colls_dir, d)):
                print('- ' + d)

    def _get_root_dir(self, name):
        return os.path.join(os.getcwd(),
                            self.default_config[name])

    def _get_dir(self, name):
        return os.path.join(self.curr_coll_dir,
                            self.default_config[name])

    def _create_dir(self, dirname):
        if not os.path.isdir(dirname):
            os.mkdir(dirname)

        logging.info('Created Directory: ' + dirname)

    def add_collection(self):
        os.makedirs(self.curr_coll_dir)
        logging.info('Created Directory: ' + self.curr_coll_dir)

        self._create_dir(self.archive_dir)
        self._create_dir(self.indexes_dir)
        self._create_dir(self.static_dir)
        self._create_dir(self.templates_dir)

        self._create_dir(self._get_root_dir('static_path'))
        self._create_dir(self._get_root_dir('templates_dir'))

    def _assert_coll_exists(self):
        if not os.path.isdir(self.curr_coll_dir):
            msg = ('Collection {0} does not exist. ' +
                   'To create a new collection, run\n\n{1} init {0}')
            raise IOError(msg.format(self.coll_name, sys.argv[0]))

    def add_warcs(self, warcs):
        if not os.path.isdir(self.archive_dir):
            raise IOError('Directory {0} does not exist'.
                          format(self.archive_dir))

        full_paths = []
        for filename in warcs:
            filename = os.path.abspath(filename)
            shutil.copy2(filename, self.archive_dir)
            full_paths.append(os.path.join(self.archive_dir, filename))
            logging.info('Copied ' + filename + ' to ' + self.archive_dir)

        self._index_merge_warcs(full_paths, self.DEF_INDEX_FILE)

    def reindex(self):
        cdx_file = os.path.join(self.indexes_dir, self.DEF_INDEX_FILE)
        logging.info('Indexing ' + self.archive_dir + ' to ' + cdx_file)
        self._cdx_index(cdx_file, [self.archive_dir])

    def _cdx_index(self, out, input_, rel_root=None):
        from pywb.indexer.cdxindexer import write_multi_cdx_index

        options = dict(append_post=True,
                       cdxj=True,
                       sort=True,
                       recurse=True,
                       rel_root=rel_root)

        write_multi_cdx_index(out, input_, **options)

    def index_merge(self, filelist, index_file):
        wrongdir = 'Skipping {0}, must be in {1} archive directory'
        notfound = 'Skipping {0}, file not found'

        filtered_warcs = []

        # Check that warcs are actually in archive dir
        abs_archive_dir = os.path.abspath(self.archive_dir)

        for f in filelist:
            abs_filepath = os.path.abspath(f)
            prefix = os.path.commonprefix([abs_archive_dir, abs_filepath])

            if prefix != abs_archive_dir:
                raise IOError(wrongdir.format(abs_filepath, abs_archive_dir))
            elif not os.path.isfile(abs_filepath):
                raise IOError(notfound.format(f))
            else:
                filtered_warcs.append(abs_filepath)

        self._index_merge_warcs(filtered_warcs, index_file, abs_archive_dir)

    def _index_merge_warcs(self, new_warcs, index_file, rel_root=None):
        cdx_file = os.path.join(self.indexes_dir, index_file)

        temp_file = cdx_file + '.tmp.' + timestamp20_now()
        self._cdx_index(temp_file, new_warcs, rel_root)

        # no existing file, so just make it the new file
        if not os.path.isfile(cdx_file):
            shutil.move(temp_file, cdx_file)
            return

        merged_file = temp_file + '.merged'

        last_line = None

        with open(cdx_file, 'rb') as orig_index:
            with open(temp_file, 'rb') as new_index:
                with open(merged_file, 'w+b') as merged:
                    for line in heapq.merge(orig_index, new_index):
                        if last_line != line:
                            merged.write(line)
                            last_line = line

        shutil.move(merged_file, cdx_file)
        #os.rename(merged_file, cdx_file)
        os.remove(temp_file)

    def set_metadata(self, namevalue_pairs):
        metadata_yaml = os.path.join(self.curr_coll_dir, 'metadata.yaml')
        metadata = None
        if os.path.isfile(metadata_yaml):
            with open(metadata_yaml, 'rb') as fh:
                metadata = yaml.safe_load(fh)

        if not metadata:
            metadata = {}

        msg = 'Metadata params must be in the form "name=value"'
        for pair in namevalue_pairs:
            v = pair.split('=', 1)
            if len(v) != 2:
                raise ValueError(msg)

            print('Set {0}={1}'.format(v[0], v[1]))
            metadata[v[0]] = v[1]

        with open(metadata_yaml, 'w+b') as fh:
            fh.write(yaml.dump(metadata, default_flow_style=False).encode('utf-8'))

    def _load_templates_map(self):
        defaults = load_yaml_config(DEFAULT_CONFIG)

        temp_dir = defaults['templates_dir']

        # Coll Templates
        templates = defaults['html_templates']

        for name in templates:
            defaults[name] = os.path.join(temp_dir, defaults[name])

        return defaults, templates

    def list_templates(self):
        defaults, templates = self._load_templates_map()

        print('HTML Shared and Per-Collection Templates')
        for n in templates:
            v = defaults[n]
            print('- {0}: (pywb/{1})'.format(n, v))

    def _confirm_overwrite(self, full_path, msg):
        if not os.path.isfile(full_path):
            return True

        res = get_input(msg)
        try:
            res = strtobool(res)
        except ValueError:
            res = False

        if not res:
            raise IOError('Skipping, {0} already exists'.format(full_path))

    def _get_template_path(self, template_name, verb):
        defaults, templates = self._load_templates_map()

        try:
            filename = defaults[template_name]
            if not self.coll_name:
                full_path = os.path.join(os.getcwd(), filename)
            else:
                full_path = os.path.join(self.templates_dir,
                                         os.path.basename(filename))

        except KeyError:
            msg = 'template name must be one of {0}'
            msg = msg.format(templates)
            raise KeyError(msg)

        return full_path, filename

    def add_template(self, template_name, force=False):
        full_path, filename = self._get_template_path(template_name, 'add')

        msg = ('Template file "{0}" ({1}) already exists. ' +
               'Overwrite with default template? (y/n) ')
        msg = msg.format(full_path, template_name)

        if not force:
            self._confirm_overwrite(full_path, msg)

        data = resource_string('pywb', filename)
        with open(full_path, 'w+b') as fh:
            fh.write(data)

        full_path = os.path.abspath(full_path)
        msg = 'Copied default template "{0}" to "{1}"'
        print(msg.format(filename, full_path))

    def remove_template(self, template_name, force=False):
        full_path, filename = self._get_template_path(template_name, 'remove')

        if not os.path.isfile(full_path):
            msg = 'Template "{0}" does not exist.'
            raise IOError(msg.format(full_path))

        msg = 'Delete template file "{0}" ({1})? (y/n) '
        msg = msg.format(full_path, template_name)

        if not force:
            self._confirm_overwrite(full_path, msg)

        os.remove(full_path)
        print('Removed template file "{0}"'.format(full_path))

    def migrate_cdxj(self, path, force=False):
        from pywb.manager.migrate import MigrateCDX

        migrate = MigrateCDX(path)
        count = migrate.count_cdx()
        if count == 0:
            print('Index files up-to-date, nothing to convert')
            return

        msg = 'Convert {0} index files? (y/n)'.format(count)
        if not force:
            res = get_input(msg)
            try:
                res = strtobool(res)
            except ValueError:
                res = False

            if not res:
                return

        migrate.convert_to_cdxj()


#=============================================================================
def main(args=None):
    description = """
Create manage file based web archive collections
"""
    # format(os.path.basename(sys.argv[0]))

    logging.basicConfig(format='%(asctime)s: [%(levelname)s]: %(message)s',
                        level=logging.DEBUG)

    parser = ArgumentParser(description=description,
                            # epilog=epilog,
                            formatter_class=RawTextHelpFormatter)

    parser.add_argument("-V", "--version", action="version", version=get_version())

    subparsers = parser.add_subparsers(dest='type')
    subparsers.required = True

    # Init Coll
    def do_init(r):
        m = CollectionsManager(r.coll_name, must_exist=False)
        m.add_collection()

    init_help = 'Init new collection, create all collection directories'
    init = subparsers.add_parser('init', help=init_help)
    init.add_argument('coll_name')
    init.set_defaults(func=do_init)

    # List Colls
    def do_list(r):
        m = CollectionsManager('', must_exist=False)
        m.list_colls()

    list_help = 'List Collections'
    listcmd = subparsers.add_parser('list', help=list_help)
    listcmd.set_defaults(func=do_list)

    # Add Warcs
    def do_add(r):
        m = CollectionsManager(r.coll_name)
        m.add_warcs(r.files)

    addwarc_help = 'Copy ARCS/WARCS to collection directory and reindex'
    addwarc = subparsers.add_parser('add', help=addwarc_help)
    addwarc.add_argument('coll_name')
    addwarc.add_argument('files', nargs='+')
    addwarc.set_defaults(func=do_add)

    # Reindex All
    def do_reindex(r):
        m = CollectionsManager(r.coll_name)
        m.reindex()

    reindex_help = 'Re-Index entire collection'
    reindex = subparsers.add_parser('reindex', help=reindex_help)
    reindex.add_argument('coll_name')
    reindex.set_defaults(func=do_reindex)

    # Index warcs
    def do_index(r):
        m = CollectionsManager(r.coll_name)
        m.index_merge(r.files, m.DEF_INDEX_FILE)

    indexwarcs_help = 'Index specified ARC/WARC files in the collection'
    indexwarcs = subparsers.add_parser('index', help=indexwarcs_help)
    indexwarcs.add_argument('coll_name')
    indexwarcs.add_argument('files', nargs='+')
    indexwarcs.set_defaults(func=do_index)

    # Set metadata
    def do_metadata(r):
        m = CollectionsManager(r.coll_name)
        m.set_metadata(r.set)

    metadata_help = 'Set Metadata'
    metadata = subparsers.add_parser('metadata', help=metadata_help)
    metadata.add_argument('coll_name')
    metadata.add_argument('--set', nargs='+')
    metadata.set_defaults(func=do_metadata)

    # Add default template
    def do_add_template(r):
        m = CollectionsManager(r.coll_name, must_exist=False)
        if r.add:
            m.add_template(r.add, r.force)
        elif r.remove:
            m.remove_template(r.remove, r.force)
        elif r.list:
            m.list_templates()

    template_help = 'Add default html template for customization'
    template = subparsers.add_parser('template', help=template_help)
    template.add_argument('coll_name', nargs='?', default='')
    template.add_argument('-f', '--force', action='store_true')
    template.add_argument('--add')
    template.add_argument('--remove')
    template.add_argument('--list', action='store_true')
    template.set_defaults(func=do_add_template)

    # Migrate CDX
    def do_migrate(r):
        m = CollectionsManager('', must_exist=False)
        m.migrate_cdxj(r.path, r.force)

    migrate_help = 'Convert any existing archive indexes to new json format'
    migrate = subparsers.add_parser('cdx-convert', help=migrate_help)
    migrate.add_argument('path', default='./', nargs='?')
    migrate.add_argument('-f', '--force', action='store_true')
    migrate.set_defaults(func=do_migrate)

    # ACL
    from pywb.manager.aclmanager import ACLManager
    def do_acl(r):
        acl = ACLManager(r)
        acl.process(r)

    acl_help = 'Configure Access Control Lists (ACL) for a collection'
    acl = subparsers.add_parser('acl', help=acl_help)
    ACLManager.init_parser(acl)
    acl.set_defaults(func=do_acl)

    # LOC
    loc_avail = False
    try:
        from pywb.manager.locmanager import LocManager
        loc_avail = True
    except:
        pass

    def do_loc(r):
        if not loc_avail:
            print("You must install i18n extensions with 'pip install pywb[i18n]' to use localization features")
            return

        loc = LocManager()
        loc.process(r)

    loc_help = 'Generate strings for i18n/localization'
    loc = subparsers.add_parser('i18n', help=loc_help)
    if loc_avail:
        LocManager.init_parser(loc)
    loc.set_defaults(func=do_loc)

    # Parse
    r = parser.parse_args(args=args)
    r.func(r)


# special wrapper for cli to avoid printing stack trace
def main_wrap_exc():  # pragma: no cover
    try:
        main()
    except Exception as e:
        print('Error: ' + str(e))
        sys.exit(2)


if __name__ == "__main__":
    main_wrap_exc()
