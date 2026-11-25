from __future__ import annotations

import http.client
import importlib.util
import os
import socket
import ssl
import threading
import urllib.error
import urllib.request
import weakref
from urllib.parse import unquote, urlsplit

_lock = threading.Lock()
_context: ssl.SSLContext | None = None
_clients: weakref.WeakSet = weakref.WeakSet()


class NetworkBlocked(RuntimeError):
    pass


def _prefs():
    from src.core.downloader import NET
    return NET


def guard() -> None:
    message = _prefs().leak_guard()
    if message:
        raise NetworkBlocked(message)


def proxy_url() -> str:
    net = _prefs()
    return net.proxy_url() if net.has_proxy else ''


def _loopback_proxy() -> bool:
    return _prefs().proxy_host.strip().lower() in ('127.0.0.1', 'localhost', '::1')


def _primary_ipv4() -> str:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(('192.0.2.1', 9))
            return sock.getsockname()[0]
    except OSError:
        return ''


def source_ip() -> str:
    net = _prefs()
    if net.has_proxy and _loopback_proxy():
        return ''
    if ip := net.bind_ip():
        return ip
    return _primary_ipv4() if net.block_ipv6 else ''


def httpx_transport(verify=None):
    import httpx

    proxy = proxy_url()
    if proxy and not proxy.startswith(('http://', 'https://')) and importlib.util.find_spec('socksio') is None:
        proxy = ''
    source = source_ip()
    if not proxy and not source:
        return None
    options = {'proxy': proxy or None, 'local_address': source or None}
    if verify is not None:
        options['verify'] = verify
    return httpx.HTTPTransport(**options)


def reset_clients() -> None:
    for client in list(_clients):
        try:
            client.close()
        except Exception:
            pass


def shared_ssl_context() -> ssl.SSLContext:
    global _context
    ctx = _context
    if ctx is not None:
        return ctx
    with _lock:
        if _context is None:
            import certifi

            cafile = os.environ.get('SSL_CERT_FILE') or certifi.where()
            _context = ssl.create_default_context(cafile=cafile)
        return _context


class LazyHttpClient:

    __slots__ = ('_kwargs', '_transport_factory', '_client', '_lock', '__weakref__')

    def __init__(self, *, transport_factory=None, **kwargs):
        self._kwargs = kwargs
        self._transport_factory = transport_factory
        self._client = None
        self._lock = threading.Lock()
        _clients.add(self)

    def _get(self):
        client = self._client
        if client is not None:
            return client
        with self._lock:
            client = self._client
            if client is None:
                import httpx

                guard()
                kwargs = dict(self._kwargs)
                if self._transport_factory is not None:
                    kwargs['transport'] = self._transport_factory()
                else:
                    if 'verify' not in kwargs:
                        kwargs['verify'] = shared_ssl_context()
                    transport = httpx_transport()
                    if transport is not None:
                        kwargs['transport'] = transport
                        kwargs['trust_env'] = False
                client = httpx.Client(**kwargs)
                self._client = client
        return client

    def close(self) -> None:
        client = self._client
        if client is not None:
            self._client = None
            client.close()

    def __getattr__(self, name):
        return getattr(self._get(), name)


class _GuardHandler(urllib.request.BaseHandler):
    handler_order = 100

    def http_request(self, req):
        guard()
        return req

    https_request = http_request


class _BoundHTTPConnection(http.client.HTTPConnection):
    def connect(self):
        source = source_ip()
        if source:
            self.source_address = (source, 0)
        super().connect()


class _BoundHTTPSConnection(http.client.HTTPSConnection):
    def connect(self):
        source = source_ip()
        if source:
            self.source_address = (source, 0)
        super().connect()


class _BoundHTTPHandler(urllib.request.HTTPHandler):
    def http_open(self, req):
        return self.do_open(_BoundHTTPConnection, req)


class _BoundHTTPSHandler(urllib.request.HTTPSHandler):
    def https_open(self, req):
        return self.do_open(_BoundHTTPSConnection, req, context=self._context)


def _socks_handler(url: str):
    try:
        import socks
        import sockshandler
    except ImportError:
        return urllib.request.ProxyHandler({})
    parts = urlsplit(url)
    if not parts.hostname:
        return urllib.request.ProxyHandler({})
    return sockshandler.SocksiPyHandler(
        socks.SOCKS5, parts.hostname, parts.port or 1080, rdns=True,
        username=unquote(parts.username) if parts.username else None,
        password=unquote(parts.password) if parts.password else None)


def install_urllib_guard() -> None:
    proxy = proxy_url()
    handlers = [_GuardHandler()]
    if proxy.startswith(('http://', 'https://')):
        handlers.append(urllib.request.ProxyHandler({'http': proxy, 'https': proxy}))
    elif proxy.startswith('socks'):
        handlers.append(_socks_handler(proxy))
    else:
        handlers.append(urllib.request.ProxyHandler({}))
    handlers.extend((_BoundHTTPHandler(), _BoundHTTPSHandler()))
    urllib.request.install_opener(urllib.request.build_opener(*handlers))
