from six.moves.http_cookies import SimpleCookie, CookieError
import six
import re

import logging

logger = logging.getLogger(__name__)


#================================================================
class WbUrlBaseCookieRewriter(object):
    """ Base Cookie rewriter for wburl-based requests.
    """
    REMOVE_EXPIRES = re.compile('[;]\s*?expires=.{4}[^,;]+', re.I)

    def __init__(self, url_rewriter):
        self.url_rewriter = url_rewriter

    def rewrite(self, cookie_str, header='Set-Cookie'):
        results = []
        cookie_str = self.REMOVE_EXPIRES.sub('', cookie_str)
        try:
            cookie = SimpleCookie(cookie_str)
        except CookieError as e:
            logger.info(e, exc_info=True)
            return results

        for name, morsel in six.iteritems(cookie):
            morsel = self.rewrite_cookie(name, morsel)

            self._filter_morsel(morsel)

            if not self.add_prefix_cookie_for_all_mods(morsel, results,
                                                       header):
                value = morsel.OutputString()
                results.append((header, value))

        return results

    def add_prefix_cookie_for_all_mods(self, morsel, results, header):
        """  If HttpOnly cookie that is set to a path ending in /,
             and current mod is mp_ or if_,
             then assume its meant to be a prefix, and likely needed for
             other content.
             Set cookie with same prefix but for all common modifiers:
             (mp_, js_, cs_, oe_, if_, sw_, wkrf_)
        """
        curr_mod = self.url_rewriter.wburl.mod
        if curr_mod not in ('mp_', 'if_', 'sw_'):
            return False

        if not morsel.get('httponly'):
            return False

        path = morsel.get('path')
        if not path or not path.endswith('/'):
            return False

        for mod in ('mp_', 'cs_', 'js_', 'im_', 'oe_', 'if_', 'sw_', 'wkrf_'):
            new_path = path.replace(curr_mod + '/', mod + '/')
            morsel['path'] = new_path
            results.append((header, morsel.OutputString()))

        return True

    def _filter_morsel(self, morsel):
        path = morsel.get('path')
        if path:
            inx = path.find(self.url_rewriter.rel_prefix)
            if inx > 0:
                morsel['path'] = path[inx:]

        if not self.url_rewriter.full_prefix.startswith('https://'):
            # also remove secure to avoid issues when
            # proxying over plain http
            if morsel.get('secure'):
                del morsel['secure']

        if not self.url_rewriter.rewrite_opts.get('is_live'):
            self._remove_age_opts(morsel)

    def _remove_age_opts(self, morsel):
        # remove expires as it refers to archived time
        if morsel.get('expires'):
            del morsel['expires']

        # don't use max-age, just expire at end of session
        if morsel.get('max-age'):
            del morsel['max-age']


#=================================================================
class RemoveAllCookiesRewriter(WbUrlBaseCookieRewriter):

    def rewrite(self, cookie_str, header='Set-Cookie'):
        return []


#=================================================================
class MinimalScopeCookieRewriter(WbUrlBaseCookieRewriter):
    """
    Attempt to rewrite cookies to minimal scope possible

    If path present, rewrite path to current rewritten url only
    If domain present, remove domain and set to path prefix
    """

    def rewrite_cookie(self, name, morsel):
        # if domain set, no choice but to expand cookie path to root
        if morsel.get('domain'):
            del morsel['domain']
            morsel['path'] = self.url_rewriter.rel_prefix
        # else set cookie to rewritten path
        elif morsel.get('path'):
            morsel['path'] = self.url_rewriter.rewrite(morsel['path'])

        return morsel


#=================================================================
class HostScopeCookieRewriter(WbUrlBaseCookieRewriter):
    """
    Attempt to rewrite cookies to current host url..

    If path present, rewrite path to current host. Only makes sense in live
    proxy or no redirect mode, as otherwise timestamp may change.

    If domain present, remove domain and set to path prefix
    """

    def rewrite_cookie(self, name, morsel):
        # if domain set, expand cookie to host prefix
        if morsel.get('domain'):
            del morsel['domain']
            morsel['path'] = self.url_rewriter.rewrite('/')

        # set cookie to rewritten path
        elif morsel.get('path'):
            morsel['path'] = self.url_rewriter.rewrite(morsel['path'])

        return morsel


#=================================================================
class ExactPathCookieRewriter(WbUrlBaseCookieRewriter):
    """
    Rewrite cookies only using exact path, useful for live rewrite
    without a timestamp and to minimize cookie pollution

    If path or domain present, simply remove
    """

    def rewrite_cookie(self, name, morsel):
        if morsel.get('domain'):
            del morsel['domain']
        # else set cookie to rewritten path
        if morsel.get('path'):
            del morsel['path']

        return morsel


#=================================================================
class RootScopeCookieRewriter(WbUrlBaseCookieRewriter):
    """
    Sometimes it is necessary to rewrite cookies to root scope
    in order to work across time boundaries and modifiers

    This rewriter simply sets all cookies to be in the root
    """

    def rewrite_cookie(self, name, morsel):
        # get root path
        morsel['path'] = self.url_rewriter.root_path

        # remove domain
        if morsel.get('domain'):
            del morsel['domain']

        return morsel


#=================================================================
def get_cookie_rewriter(cookie_scope):
    if cookie_scope == 'root':
        return RootScopeCookieRewriter
    elif cookie_scope == 'exact':
        return ExactPathCookieRewriter
    elif cookie_scope == 'host':
        return HostScopeCookieRewriter
    elif cookie_scope == 'removeall':
        return RemoveAllCookiesRewriter
    elif cookie_scope == 'coll':
        return MinimalScopeCookieRewriter
    else:
        return HostScopeCookieRewriter
