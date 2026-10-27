"""Shared HTTP plumbing.

`httpx.Client(...)` loads and parses every CA certificate (~150 ms) the moment it
is constructed. Several pages build their clients at module import time, which
put that cost on the UI thread the first time the page was opened.

Two small helpers fix both halves of that:

* `shared_ssl_context()` builds one context per process and reuses it, so the
  certificate bundle is parsed once instead of once per client.
* `LazyHttpClient` only constructs the real `httpx.Client` when a request is
  actually made, which normally happens on a worker thread.
"""

from __future__ import annotations

import os
import ssl
import threading

_lock = threading.Lock()
_context: ssl.SSLContext | None = None


def shared_ssl_context() -> ssl.SSLContext:
    """One verified TLS context for every client in the process."""
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
    """`httpx.Client` built on first use, with a shared TLS context.

    The module-level clients in the page backends are only touched from worker
    threads, so deferring construction keeps the certificate-store load off the
    UI thread. Attribute access forwards to the real client, which covers get,
    stream, close and anything else callers already use.
    """

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
