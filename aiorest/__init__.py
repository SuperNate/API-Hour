import collections
import inspect
import json
import re
import time
import http.cookies
import sys

import asyncio
import aiohttp, aiohttp.server

from types import MethodType
from datetime import datetime
from urllib.parse import urlsplit, parse_qsl

from .multidict import MultiDict, MutableMultiDict
from .session import Session



__version__ = '0.0.1a0'

version = __version__ + ' , Python ' + sys.version


VersionInfo = collections.namedtuple('VersionInfo',
                                     'major minor micro releaselevel serial')


def _parse_version(ver):
    RE = (r'^(?P<major>\d+)\.(?P<minor>\d+)\.'
          '(?P<micro>\d+)((?P<releaselevel>[a-z]+)(?P<serial>\d+)?)?$')
    match = re.match(RE, ver)
    try:
        major = int(match.group('major'))
        minor = int(match.group('minor'))
        micro = int(match.group('micro'))
        levels = {'rc': 'candidate',
                  'a': 'alpha',
                  'b': 'beta',
                  None: 'final'}
        releaselevel = levels[match.group('releaselevel')]
        serial = int(match.group('serial')) if match.group('serial') else 0
        return VersionInfo(major, minor, micro, releaselevel, serial)
    except Exception:
        raise ImportError("Invalid package version {}".format(ver))


version_info = _parse_version(__version__)


Entry = collections.namedtuple('Entry', 'regex method handler use_request')


class Response:

    def __init__(self, *, loop=None):
        self.headers = MutableMultiDict()
        self._cookies = http.cookies.SimpleCookie()
        self._deleted_cookies = set()
        self.on_send_headers = asyncio.Future(loop=loop)
        self.on_send_headers.add_done_callback(self._copy_cookies)

    def _copy_cookies(self, fut):
        for cookie in self._cookies.values():
            value = cookie.output(header='')[1:]
            self.headers.add('Set-Cookie', value)

    @property
    def cookies(self):
        return self._cookies

    def set_cookie(self, name, value, *, expires=None,
                   domain=None, max_age=None, path=None,
                   secure=None, httponly=None, version=None):
        """Set or update response cookie.

        Sets new cookie or updates existent with new value.
        Also updates only those params which are not None.
        """
        if name in self._deleted_cookies:
            self._deleted_cookies.remove(name)
            self._cookies.pop(name, None)

        self._cookies[name] = value
        c = self._cookies[name]
        if expires is not None:
            c['expires'] = expires
        if domain is not None:
            c['domain'] = domain
        if max_age is not None:
            c['max-age'] = max_age
        if path is not None:
            c['path'] = path
        if secure is not None:
            c['secure'] = secure
        if httponly is not None:
            c['httponly'] = httponly
        if version is not None:
            c['version'] = version

    def del_cookie(self, name, *, domain=None, path=None):
        """Delete cookie.

        Creates new empty expired cookie.
        """
        # TODO: do we need domain/path here?
        self._cookies.pop(name, None)
        self.set_cookie(name, '', max_age=0, domain=domain, path=path)
        self._deleted_cookies.add(name)


class Request:

    def __init__(self, host, message, headers, req_body, *, loop=None):
        if loop is None:
            loop = asyncio.get_event_loop()
        self._loop = loop
        self.version = message.version
        self.method = message.method.upper()
        self.host = headers.get('HOST', host)
        self.host_url = 'http://' + self.host
        self.path_qs = message.path
        res = urlsplit(self.path_qs)
        self.path = res.path
        self.path_url = self.host_url + self.path
        self.url = self.host_url + self.path_qs
        self.query_string = res.query
        self.args = MultiDict(parse_qsl(self.query_string))
        self._request_body = req_body
        self._json_body = None
        self.headers = headers
        self._response_fut = None
        self._cookies = None
        self.session = None

    @property
    def response(self):
        """Response property returns a future.

        The reason is you can want to add a callback
        on response object creation.

        See also http://docs.pylonsproject.org/projects/pyramid/en/latest/api/ \
        request.html#pyramid.request.Request.add_response_callback
        """
        if self._response_fut is None:
            self._response_fut = asyncio.Future(loop=self._loop)
            self._response_fut.set_result(Response(loop=self._loop))
        return self._response_fut

    @property
    def json_body(self):
        if self._json_body is None:
            if self._request_body:
                # TODO: store generated exception and
                # don't try to parse json next time
                self._json_body = json.loads(self._request_body.decode('utf-8'))
            else:
                raise ValueError("Request has no a body")
        return self._json_body

    @property
    def cookies(self):
        """Return request cookies.

        A read-only dictionary-like object.
        """
        if self._cookies is None:
            raw = self.headers.get('COOKIE', '')
            parsed = http.cookies.SimpleCookie(raw)
            self._cookies = MultiDict({key: val.value
                                       for key, val in parsed.items()})
        return self._cookies


class RESTServer(aiohttp.server.ServerHttpProtocol):

    DYN = re.compile(r'^\{[_a-zA-Z][_a-zA-Z0-9]*\}$')
    GOOD = r'[^{}/]+'
    PLAIN = re.compile('^'+GOOD+'$')

    METHODS = {'POST', 'GET', 'PUT', 'DELETE', 'PATCH', 'HEAD'}

    def __init__(self, *, hostname, session_factory=None, **kwargs):
        super().__init__(**kwargs)
        self.hostname = hostname
        self.session_factory = session_factory
        self._urls = []
        assert session_factory is None or callable(session_factory), \
            "session_factory must be None or callable (coroutine) function"

    @asyncio.coroutine
    def handle_request(self, message, payload):
        now = time.time()
        #self.log.debug("Start handle request %r at %d", message, now)

        try:
            headers = MutableMultiDict()
            for hdr, val in message.headers:
                headers.add(hdr, val)

            if payload is not None:
                req_body = bytearray()
                try:
                    while True:
                        chunk = yield from payload.read()
                        req_body.extend(chunk)
                except aiohttp.EofStream:
                    pass
            else:
                req_body = None

            request = Request(self.hostname, message, headers, req_body,
                              loop=self._loop)
            if self.session_factory is not None:
                sess = yield from self.session_factory(request)
                assert sess is None or isinstance(sess, Session)
                request.session = sess

            resp_impl = aiohttp.Response(self.transport, 200)
            body = yield from self.dispatch(request)
            bbody = body.encode('utf-8')

            resp_impl.add_header('Host', self.hostname)
            resp_impl.add_header('Content-Type', 'application/json')

            # content encoding
            ## accept_encoding = headers.get('accept-encoding', '').lower()
            ## if 'deflate' in accept_encoding:
            ##     resp_impl.add_header('Transfer-Encoding', 'chunked')
            ##     resp_impl.add_chunking_filter(1025)
            ##     resp_impl.add_header('Content-Encoding', 'deflate')
            ##     resp_impl.add_compression_filter('deflate')
            ## elif 'gzip' in accept_encoding:
            ##     resp_impl.add_header('Transfer-Encoding', 'chunked')
            ##     resp_impl.add_chunking_filter(1025)
            ##     resp_impl.add_header('Content-Encoding', 'gzip')
            ##     resp_impl.add_compression_filter('gzip')
            ## else:
            resp_impl.add_header('Content-Length', str(len(bbody)))

            waiter = asyncio.Future(loop=self._loop)
            resp = yield from request.response
            # if sess is not None:
            #     yield from sess.store()
            resp.on_send_headers.add_done_callback(waiter.set_result)
            resp.on_send_headers.set_result(None)
            yield from waiter
            resp_impl.add_headers(*resp.headers.items(getall=True))

            resp_impl.send_headers()
            resp_impl.write(bbody)
            resp_impl.write_eof()
            if resp_impl.keep_alive():
                self.keep_alive(True)

            #self.log.debug("Fihish handle request %r at %d -> %s",
            #               message, time.time(), body)
            self.log_access(message, None, resp_impl, time.time() - now)
        except Exception:
            #self.log.exception("Cannot handle request %r", message)
            raise

    def add_url(self, method, path, handler, use_request=False):
        """XXX"""
        assert callable(handler), handler
        if isinstance(handler, MethodType):
            holder = handler.__func__
        else:
            holder = handler
        sig = holder.__signature__ = inspect.signature(holder)

        if use_request:
            if use_request == True:
                use_request = 'request'
            try:
                p = sig.parameters[use_request]
            except KeyError:
                raise TypeError('handler {!r} has no argument {}'
                                .format(handler, use_request))
            assert p.annotation is p.empty, ("handler's arg {} "
                                             "for request name "
                                             "should not have "
                                             "annotation").format(use_request)
        else:
            use_request = None
        assert path.startswith('/')
        assert callable(handler), handler
        method = method.upper()
        assert method in self.METHODS, method
        regexp = []
        for part in path.split('/'):
            if not part:
                continue
            if self.DYN.match(part):
                regexp.append('(?P<'+part[1:-1]+'>'+self.GOOD+')')
            elif self.PLAIN.match(part):
                regexp.append(part)
            else:
                raise ValueError("Invalid path '{}'['{}']".format(path, part))
        pattern = '/' + '/'.join(regexp)
        if path.endswith('/') and pattern != '/':
            pattern += '/'
        try:
            compiled = re.compile('^' + pattern + '$')
        except re.error:
            raise ValueError("Invalid path '{}'".format(path))
        self._urls.append(Entry(compiled, method, handler, use_request))

    @asyncio.coroutine
    def dispatch(self, request):
        path = request.path
        method = request.method
        allowed_methods = set()
        for entry in self._urls:
            match = entry.regex.match(path)
            if match is None:
                continue
            if entry.method != method:
                allowed_methods.add(entry.method)
            else:
                break
        else:
            if allowed_methods:
                allow = ', '.join(sorted(allowed_methods))
                # add log
                raise aiohttp.HttpErrorException(405,
                                                 headers=(('Allow', allow),))
            else:
                # add log
                raise aiohttp.HttpErrorException(404, "Not Found")

        handler = entry.handler
        sig = inspect.signature(handler)
        kwargs = match.groupdict()
        if entry.use_request:
            assert entry.use_request not in kwargs, (entry.use_request, kwargs)
            kwargs[entry.use_request] = request
        try:
            args, kwargs, ret_ann = self.construct_args(sig, kwargs)
            if asyncio.iscoroutinefunction(handler):
                ret = yield from handler(*args, **kwargs)
            else:
                ret = handler(*args, **kwargs)
            if ret_ann is not None:
                ret = ret_ann(ret)
        except aiohttp.HttpException as exc:
            raise
        except Exception as exc:
            # add log about error
            raise aiohttp.HttpErrorException(500,
                                             "Internal Server Error") from exc
        else:
            return json.dumps(ret)

    def construct_args(self, sig, kwargs):
        try:
            bargs = sig.bind(**kwargs)
        except TypeError:
            raise
        else:
            args = bargs.arguments
            marker = object()
            for name, param in sig.parameters.items():
                if param.annotation is param.empty:
                    continue
                val = args.get(name, marker)
                if val is marker:
                    continue    # Skip default value
                try:
                    args[name] = param.annotation(val)
                except (TypeError, ValueError) as exc:
                    raise ValueError(
                        'Invalid value for argument {!r}: {!r}'
                        .format(name, exc)) from exc
            if sig.return_annotation is not sig.empty:
                return bargs.args, bargs.kwargs, sig.return_annotation
            return bargs.args, bargs.kwargs, None
