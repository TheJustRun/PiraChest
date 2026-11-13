
from __future__ import annotations

import os
import ssl
import threading

_lock = threading.Lock()
_context: ssl.SSLContext | None = None


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

    __slots__ = ('_kwargs', '_transport_factory', '_client', '_lock')

    def __init__(self, *, transport_factory=None, **kwargs):
        self._kwargs = kwargs
        self._transport_factory = transport_factory
        self._client = None
        self._lock = threading.Lock()

    def _get(self):
        client = self._client
        if client is not None:
            return client
        with self._lock:
            client = self._client
            if client is None:
                import httpx

                kwargs = self._kwargs
                if self._transport_factory is not None:
                    kwargs = {**kwargs, 'transport': self._transport_factory()}
                elif 'verify' not in kwargs:
                    kwargs = {**kwargs, 'verify': shared_ssl_context()}
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
