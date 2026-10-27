from __future__ import annotations

import logging
import mimetypes
import os
import re
import threading
import time
from typing import Callable, Optional
from urllib.parse import urlsplit

import requests
from PySide6.QtCore import QObject

from src.core.models import BookItem, MediaPage
from src.core.cache import cache
from src.core.worker import DL, Priority, cancel as _cancel_task, submit as _submit
from src.core.books.sources.base import BookProvider, Cancelled, IsCancelled
from src.core.books.sources.gutenberg import GutenbergProvider
from src.core.books.sources.libgen import LibgenProvider

logger = logging.getLogger(__name__)

_PROVIDERS: dict[str, BookProvider] = {p.key: p for p in (GutenbergProvider(), LibgenProvider())}
_PROVIDER_ORDER: list[BookProvider] = sorted(_PROVIDERS.values(), key=lambda p: p.priority)

_NS = 'books'
_TTL = 900

_DL_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
_download_session = requests.Session()
_download_session.headers.update({
    "User-Agent": _DL_UA,
    "Accept": "*/*",
    "Accept-Language": "en-US,en;q=0.9",
})


def get_download_session():
    return _download_session


def safe_filename(name: str) -> str:
    cleaned = re.sub(r'[<>:"/\\|?*]+', '_', name).strip().strip('.')
    return cleaned[:150] or 'book'


def format_extension(mime: str) -> str:
    ext = mimetypes.guess_extension(mime.split(';')[0].strip())
    if ext:
        return ext.lstrip('.')
    return mime.split('/')[-1].split('+')[0] or 'bin'


def provider_keys() -> list[str]:
    return [p.key for p in _PROVIDER_ORDER]


def _provider(key: str) -> Optional[BookProvider]:
    return _PROVIDERS.get(key)


def _check_cancelled(is_cancelled: IsCancelled) -> None:
    if is_cancelled is not None and is_cancelled():
        raise Cancelled()


class _InFlight:
    __slots__ = ('event', 'result', 'error')

    def __init__(self) -> None:
        self.event = threading.Event()
        self.result: Optional[MediaPage] = None
        self.error: Optional[BaseException] = None


_inflight_lock = threading.Lock()
_inflight: dict[str, _InFlight] = {}


_FOLLOW_TIMEOUT_S = 30.0
_FOLLOW_SLICE_S = 0.1


def _dedup_fetch(cache_key: str, use_cache: bool, fetch_fn: Callable[[], MediaPage],
                 is_cancelled: IsCancelled = None) -> MediaPage:
    if use_cache:
        cached = cache.load(_NS, cache_key, ttl_seconds=_TTL)
        if cached is not None:
            return MediaPage(entries=[BookItem.from_dict(d) for d in cached['entries']], page=cached['page'], has_more=cached['has_more'])

    with _inflight_lock:
        entry = _inflight.get(cache_key)
        is_leader = entry is None
        if is_leader:
            entry = _InFlight()
            _inflight[cache_key] = entry

    if not is_leader:
        deadline = time.monotonic() + _FOLLOW_TIMEOUT_S
        while not entry.event.wait(_FOLLOW_SLICE_S):
            _check_cancelled(is_cancelled)
            if time.monotonic() >= deadline:
                break
        if entry.error is not None:
            raise entry.error
        if entry.result is not None:
            return entry.result

    try:
        result = fetch_fn()
    except BaseException as exc:
        with _inflight_lock:
            entry.error = exc
            _inflight.pop(cache_key, None)
        entry.event.set()
        raise
    else:
        if use_cache:
            cache.save(_NS, cache_key, {'entries': [e.to_dict() for e in result.entries], 'page': result.page, 'has_more': result.has_more})
        with _inflight_lock:
            entry.result = result
            _inflight.pop(cache_key, None)
        entry.event.set()
        return result


def search(provider_key: str, query: str, page: int, use_cache: bool = True, is_cancelled: IsCancelled = None) -> MediaPage:
    provider = _provider(provider_key)
    if provider is None:
        return MediaPage(entries=[], page=page, has_more=False)
    _check_cancelled(is_cancelled)
    cache_key = f'search:{provider_key}:{query}:{page}'
    return _dedup_fetch(cache_key, use_cache, lambda: provider.search(query, page, is_cancelled=is_cancelled), is_cancelled)


def browse(provider_key: str, page: int, use_cache: bool = True, is_cancelled: IsCancelled = None) -> MediaPage:
    provider = _provider(provider_key)
    if provider is None:
        return MediaPage(entries=[], page=page, has_more=False)
    _check_cancelled(is_cancelled)
    cache_key = f'browse:{provider_key}:{page}'
    return _dedup_fetch(cache_key, use_cache, lambda: provider.browse(page, is_cancelled=is_cancelled), is_cancelled)


def details(provider_key: str, book_id: str, use_cache: bool = True, is_cancelled: IsCancelled = None) -> Optional[BookItem]:
    provider = _provider(provider_key)
    if provider is None:
        return None
    _check_cancelled(is_cancelled)
    cache_key = f'details:{provider_key}:{book_id}'
    if use_cache:
        cached = cache.load(_NS, cache_key, ttl_seconds=_TTL)
        if cached is not None:
            return BookItem.from_dict(cached)
    result = provider.details(book_id, is_cancelled=is_cancelled)
    if result is not None and use_cache:
        cache.save(_NS, cache_key, result.to_dict())
    return result


def details_for_entry(entry: BookItem, use_cache: bool = True, is_cancelled: IsCancelled = None) -> Optional[BookItem]:
    return details(entry.provider, entry.id, use_cache=use_cache, is_cancelled=is_cancelled)


def _providers_for(sources: Optional[list[str]]) -> list[BookProvider]:
    if not sources:
        return _PROVIDER_ORDER
    wanted = set(sources)
    return [p for p in _PROVIDER_ORDER if p.key in wanted]


def _merge_provider_pages(fetch: Callable[[BookProvider], MediaPage], page: int, is_cancelled: IsCancelled,
                           sources: Optional[list[str]] = None) -> MediaPage:
    entries: list[BookItem] = []
    has_more = False
    for provider in _providers_for(sources):
        _check_cancelled(is_cancelled)
        try:
            result = fetch(provider)
        except Cancelled:
            raise
        except Exception as exc:
            logger.warning('Books provider %s failed: %s', provider.key, exc)
            continue
        entries.extend(result.entries)
        has_more = has_more or result.has_more
    return MediaPage(entries=entries, page=page, has_more=has_more)


def search_all(query: str, page: int, use_cache: bool = True, is_cancelled: IsCancelled = None,
               sources: Optional[list[str]] = None) -> MediaPage:
    return _merge_provider_pages(
        lambda p: search(p.key, query, page, use_cache=use_cache, is_cancelled=is_cancelled),
        page, is_cancelled, sources=sources,
    )


def browse_all(page: int, use_cache: bool = True, is_cancelled: IsCancelled = None,
               sources: Optional[list[str]] = None) -> MediaPage:
    return _merge_provider_pages(
        lambda p: browse(p.key, page, use_cache=use_cache, is_cancelled=is_cancelled),
        page, is_cancelled, sources=sources,
    )


def _download_book(url: str, dest_path: str, item_id: str, download_manager, is_cancelled: IsCancelled = None) -> None:
    def cancelled() -> bool:
        return bool(is_cancelled is not None and is_cancelled())

    tmp_path = dest_path + '.part'
    resp = None
    try:
        os.makedirs(os.path.dirname(dest_path), exist_ok=True)
        parts = urlsplit(url)
        headers = {}
        if parts.scheme and parts.netloc:
            origin = f'{parts.scheme}://{parts.netloc}'
            headers = {'Referer': origin + '/', 'Origin': origin}
        total = 0
        try:
            head_resp = get_download_session().head(url, timeout=15, headers=headers, allow_redirects=True)
            total = int(head_resp.headers.get('Content-Length', 0))
        except Exception:
            total = 0
        if cancelled():
            return
        resp = get_download_session().get(url, stream=True, timeout=(10, 20), headers=headers, allow_redirects=True)
        if resp.status_code != 200:
            download_manager.fail_external(item_id, f'status {resp.status_code}')
            return
        content_type = resp.headers.get('Content-Type', '').lower()
        if 'text/html' in content_type:
            download_manager.fail_external(item_id, 'download link expired or invalid')
            return
        if not total:
            total = int(resp.headers.get('Content-Length', 0))
        download_manager.update_external(item_id, total_bytes=total, progress=0.0 if total else None)
        downloaded = 0
        last_t = time.monotonic()
        last_bytes = 0
        with open(tmp_path, 'wb') as fh:
            for chunk in resp.iter_content(chunk_size=1024 * 128):
                if cancelled():
                    fh.close()
                    try:
                        os.remove(tmp_path)
                    except OSError:
                        pass
                    return
                if not chunk:
                    continue
                fh.write(chunk)
                downloaded += len(chunk)
                now = time.monotonic()
                elapsed = now - last_t
                speed_kbps = None
                if elapsed >= 0.5:
                    speed_kbps = ((downloaded - last_bytes) / 1024) / elapsed
                    last_t = now
                    last_bytes = downloaded
                if total:
                    download_manager.update_external(item_id, downloaded_bytes=downloaded, total_bytes=total, speed_down_kbps=speed_kbps)
                else:
                    est = 100.0 * (1 - 1 / (1 + downloaded / (5 * 1024 * 1024)))
                    download_manager.update_external(item_id, downloaded_bytes=downloaded, total_bytes=0, progress=min(95.0, est), speed_down_kbps=speed_kbps)
        if downloaded == 0:
            try:
                os.remove(tmp_path)
            except OSError:
                pass
            download_manager.fail_external(item_id, 'empty response')
            return
        os.replace(tmp_path, dest_path)
        download_manager.complete_external(item_id, dest_path)
    except Exception as exc:
        try:
            if os.path.isfile(tmp_path):
                os.remove(tmp_path)
        except OSError:
            pass
        if not cancelled():
            download_manager.fail_external(item_id, str(exc))
    finally:
        if resp is not None:
            resp.close()


def _resolve_and_download(details: BookItem, format_mime: str, dest_path: str, item_id: str,
                          download_manager, is_cancelled: IsCancelled = None) -> None:
    file_url = details.formats.get(format_mime)
    if not file_url:
        try:
            refreshed = details_for_entry(details, use_cache=False, is_cancelled=is_cancelled)
        except Cancelled:
            return
        file_url = refreshed.formats.get(format_mime) if refreshed is not None else None
    if not file_url:
        download_manager.fail_external(item_id, 'could not resolve download link')
        return
    _download_book(file_url, dest_path, item_id, download_manager, is_cancelled)


class BookDownloadBridge(QObject):
    def __init__(self, download_manager, parent=None):
        super().__init__(parent)
        self._download_manager = download_manager
        self._tickets: dict[str, object] = {}
        self._download_manager.item_updated.connect(self._on_item_updated)

    def download(self, details: BookItem, format_mime: str) -> Optional[str]:
        from src.core.config import settings
        dest_dir = getattr(settings, 'download_dir_books', os.path.join(settings.download_dir, 'books'))
        filename = f'{safe_filename(details.title)}.{format_extension(format_mime)}'
        dest_path = os.path.join(dest_dir, filename)
        item_id = self._download_manager.add_external(game_name=details.title, console=details.author, source='Books', category='books')

        ticket = _submit(_resolve_and_download, args=(details, format_mime, dest_path, item_id, self._download_manager),
                         on_error=lambda msg, i=item_id: self._download_manager.fail_external(i, msg),
                         owner=self, priority=Priority.HIGH, pool=DL)
        if ticket is not None:
            self._tickets[item_id] = ticket
            self._download_manager.register_external_cancel(item_id, lambda t=ticket: _cancel_task(t))
        return item_id

    def _on_item_updated(self, item_id: str) -> None:
        if item_id not in self._tickets:
            return
        item = self._download_manager.get(item_id)
        if item is None:
            self._tickets.pop(item_id, None)
            return
        from src.core.downloader import DLState
        if item.state in (DLState.completed, DLState.error, DLState.cancelled):
            self._tickets.pop(item_id, None)

    def shutdown(self) -> None:
        for ticket in list(self._tickets.values()):
            _cancel_task(ticket)
        self._tickets.clear()
