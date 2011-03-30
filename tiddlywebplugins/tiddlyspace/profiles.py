"""
Start at the infrastructure for OStatus, including webfinger,
user profiles, etc.
"""

import urllib
import urllib2

from tiddlyweb.control import filter_tiddlers
from tiddlyweb.store import StoreError, NoUserError
from tiddlyweb.model.bag import Bag
from tiddlyweb.model.tiddler import Tiddler
from tiddlyweb.model.policy import ForbiddenError, UserRequiredError
from tiddlyweb.model.user import User
from tiddlyweb.web.handler.search import get as search
from tiddlyweb.web.http import HTTP404, HTTP400, HTTP415
from tiddlyweb.web.util import (server_host_url, server_base_url,
        encode_name, get_serialize_type)
from tiddlywebplugins.tiddlyspace.spaces import space_uri
from tiddlywebplugins.tiddlyspace.web import determine_host
from tiddlyweb.wikitext import render_wikitext
from tiddlywebplugins.dispatcher.listener import Listener as BaseListener
from tiddlywebplugins.utils import get_store


HOST_META_TEMPLATE = """<?xml version="1.0" encoding="UTF-8"?>
<XRD xmlns="http://docs.oasis-open.org/ns/xri/xrd-1.0" xmlns:hm="http://host-meta.net/xrd/1.0">
<hm:Host xmlns="http://host-meta.net/xrd/1.0">%(host)s</hm:Host>
<Link rel="lrdd" template="%(server_host)s/webfinger?q={uri}">
<Title>Resource Descriptor</Title>
</Link>
</XRD>
"""


WEBFINGER_TEMPLATE = """<?xml version="1.0"?>
<XRD xmlns="http://docs.oasis-open.org/ns/xri/xrd-1.0">
<Subject>acct:%(username)s@%(host)s</Subject>
<Alias>%(server_host)s/profiles/%(username)s</Alias>
<Link rel="http://webfinger.net/rel/profile-page"
      href="%(server_host)s/profiles/%(username)s"
      type="text/html"/>
<Link rel="describedby"
      href="%(server_host)s/profiles/%(username)s"
      type="text/html"/>
<Link rel="http://schemas.google.com/g/2010#updates-from"
      href="%(server_host)s/profiles/%(username)s.atom"
      type="application/atom+xml"/>
</XRD>
"""

PROFILE_TEMPLATE = """
<div class="tiddler">
<a href="%(space_uri)s" title="space link"><img style="float:right" src="%(avatar_path)s" alt="avatar"></img></a>
%(profile)s
</div>
<div>
<ul id="tiddlers" class="listing">
%(tiddlers)s
</div>
</ul>
"""


def add_profile_routes(selector):
    """
    Add the necessary routes for profiles and webfinger.
    """
    selector.add('/.well-known/host-meta', GET=host_meta)
    selector.add('/webfinger', GET=webfinger)
    selector.add('/profiles/{username}[.{format}]', GET=profile)


def profile(environ, start_response):
    """
    Choose between an atom or html profile.
    """
    http_host, host_url = determine_host(environ)
    if http_host != host_url:
        # Or should it be a 302?
        raise HTTP404('No profiles at this host: %s' % http_host)

    _, mime_type = get_serialize_type(environ)
    if 'atom' in mime_type:
        return atom_profile(environ, start_response)
    elif 'html' in mime_type:
        return html_profile(environ, start_response)
    else:
        raise HTTP415('Atom and HTML only')


def atom_profile(environ, start_response):
    """
    Send the atom profile, which is actually a search
    for tiddlers modified by the user.
    """
    username = environ['wsgiorg.routing_args'][1]['username']
    search_string = _search_string(username)
    environ['tiddlyweb.query']['q'] = [search_string]
    return search(environ, start_response)


def html_profile(environ, start_response):
    """
    Send the HTML profile, made up for the user's SiteIcon,
    their profile from their space, and their most recently
    modified tiddlers.
    """
    username = environ['wsgiorg.routing_args'][1]['username']
    usersign = environ['tiddlyweb.usersign']

    store = environ['tiddlyweb.store']

    try:
        _ = store.get(User(username))
    except NoUserError:
        raise HTTP404('Profile not found for %s' % username)

    activity_feed = profile_atom_url(environ, username)

    environ['tiddlyweb.links'] = [
           '<link rel="alternate" type="application/atom+xml"'
           'title="Atom activity feed" href="%s" />'
           % activity_feed]

    profile_tiddler = Tiddler('profile', '%s_public' % username)
    try:
        profile_tiddler = store.get(profile_tiddler)
    except StoreError, exc:
        raise HTTP404('No profile for %s: %s' % (username, exc))

    profile_text = render_wikitext(profile_tiddler, environ)

    tiddlers = store.search(_search_string(username))
    tiddlers_list = []

    # XXX this bag_readable loop is copied from
    # tiddlyweb.web.handler.search:get, it should be factored
    # out of there for resue
    bag_readable = {}
    for tiddler in tiddlers:
        try:
            if bag_readable[tiddler.bag]:
                tiddlers_list.append(_tiddler_in_list(tiddler))
        except KeyError:
            bag = Bag(tiddler.bag)
            bag = store.get(bag)
            try:
                bag.policy.allows(usersign, 'read')
                tiddlers_list.append(_tiddler_in_list(tiddler))
                bag_readable[tiddler.bag] = True
            except(ForbiddenError, UserRequiredError):
                bag_readable[tiddler.bag] = False

    profile_tiddlers = '\n'.join(tiddlers_list)

    avatar_path = '/recipes/%s_public/tiddlers/SiteIcon' % username

    start_response('200 OK', [
        ('Content-Type', 'text/html; charset=UTF-8')])

    environ['tiddlyweb.title'] = 'Profile for %s' % username
    return [PROFILE_TEMPLATE % {'username': username,
        'avatar_path': avatar_path, 'space_uri': space_uri(environ, username),
        'profile': profile_text, 'tiddlers': profile_tiddlers}]


def host_meta(environ, start_response):
    """
    Send the host_meta information, so webfinger info can be found.
    """
    http_host, host_url = determine_host(environ)
    if http_host != host_url:
        # Or should it be a 302?
        raise HTTP404('No host-meta at this host: %s' % http_host)

    start_response('200 OK', [
        ('Content-Type', 'application/xrd+xml')])

    return [HOST_META_TEMPLATE % {'host': http_host, 'server_host':
        server_base_url(environ)}]


def profile_atom_url(environ, username):
    return (server_host_url(environ) +
            '/profiles/%s.atom' % encode_name(username))


def webfinger(environ, start_response):
    """
    Display the webfinger information for a given user.
    """
    http_host, host_url = determine_host(environ)
    if http_host != host_url:
        raise HTTP404('No webfinger at this host: %s' % http_host)

    username = environ['tiddlyweb.query'].get('q', [None])[0]

    if not username:
        raise HTTP400('No account provided to webfinger query')

    if username.startswith('acct:'):
        username = username.split(':', 1)[1]
    username = username.split('@', 1)[0]

    start_response('200 OK', [
        ('Content-Type', 'application/xrd+xml')])

    return [WEBFINGER_TEMPLATE % {'username': username,
        'host': http_host, 'server_host': server_base_url(environ)}]


class Listener(BaseListener):

    TUBE = 'pushping'
    STORE = None

    def _act(self, job):
        if not self.STORE:
            self.STORE = get_store(self.config)
        info = self._unpack(job)

        tiddler = Tiddler(info['tiddler'], info['bag'])
        try:
            self.STORE.get(tiddler)
        except StoreError:
            return None #  Tiddler's not there, no need to notify
        user = tiddler.modifier
        if self._user_has_profile(user):
            self._send_ping(user)

    def _user_has_profile(self, user):
        try:
            tiddler = Tiddler('profile', '%s_public' % user)
            self.STORE.get(tiddler)
        except StoreError:
            return False
        return True

    def _send_ping(self, user):
        data = {
                'hub.mode': 'publish',
                'hub.url': profile_atom_url({'tiddlyweb.config': self.config},
                    user),
                }

        try:
            target = self.config['atom.hub']
        except KeyError:
            return None
        encoded_data = urllib.urlencode(data)

        try:
            response = urllib2.urlopen(target, encoded_data)
            status = response.getcode()
            if status != '204':
                logging.warn('non 204 response from hub: %s', status)
        except urllib2.URLError, exc:
            logging.warn('error when publishing to hub: %s', exc)


def _search_string(username):
    """
    Construct the search string to be used for creating
    the recent tiddlers for this username.
    """
    return 'modifier:%s _limit:20' % username


def _tiddler_in_list(tiddler):
    return ('<li><a href="/bags/%s/tiddlers/%s">%s</a></li>'
            % (encode_name(tiddler.bag), encode_name(tiddler.title),
                tiddler.title))
