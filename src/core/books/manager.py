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
from PySide6.QtCore import QObject, QRunnable, QThreadPool

from src.core.models import BookItem, MediaPage
from src.core.cache import cache
from src.core.books.sources.base import BookProvider, Cancelled, IsCancelled
from src.core.books.sources.gutenberg import GutenbergProvider
from src.core.books.sources.internet_archive import InternetArchiveProvider
from src.core.books.sources.libgen import LibgenProvider
from src.core.books.sources.wikisource import WikisourceProvider
from src.core.books.sources.gutenberg import _session as _http_session

logger = logging.getLogger(__name__)

_PROVIDERS: dict[str, BookProvider] = {p.key: p for p in (GutenbergProvider(), InternetArchiveProvider(), LibgenProvider(), WikisourceProvider())}
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


def get_session():
    return _http_session


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


def provider_display_name(key: str) -> str:
    provider = _PROVIDERS.get(key)
    return provider.display_name if provider is not None else key.title()


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


def _dedup_fetch(cache_key: str, use_cache: bool, fetch_fn: Callable[[], MediaPage]) -> MediaPage:
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
        entry.event.wait(timeout=30)
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
    return _dedup_fetch(cache_key, use_cache, lambda: provider.search(query, page, is_cancelled=is_cancelled))


def browse(provider_key: str, page: int, use_cache: bool = True, is_cancelled: IsCancelled = None) -> MediaPage:
    provider = _provider(provider_key)
    if provider is None:
        return MediaPage(entries=[], page=page, has_more=False)
    _check_cancelled(is_cancelled)
    cache_key = f'browse:{provider_key}:{page}'
    return _dedup_fetch(cache_key, use_cache, lambda: provider.browse(page, is_cancelled=is_cancelled))


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


class BookDownloadJob(QRunnable):

    def __init__(self, url: str, dest_path: str, item_id: str, download_manager):
        super().__init__()
        self._url = url
        self._dest_path = dest_path
        self._item_id = item_id
        self._download_manager = download_manager
        self._cancelled = False

    def cancel(self) -> None:
        self._cancelled = True

    def run(self) -> None:
        tmp_path = self._dest_path + '.part'
        resp = None
        try:
            os.makedirs(os.path.dirname(self._dest_path), exist_ok=True)
            parts = urlsplit(self._url)
            headers = {}
            if parts.scheme and parts.netloc:
                origin = f'{parts.scheme}://{parts.netloc}'
                headers = {'Referer': origin + '/', 'Origin': origin}
            total = 0
            try:
                head_resp = get_download_session().head(self._url, timeout=15, headers=headers, allow_redirects=True)
                total = int(head_resp.headers.get('Content-Length', 0))
            except Exception:
                total = 0
            resp = get_download_session().get(self._url, stream=True, timeout=(10, 20), headers=headers, allow_redirects=True)
            if resp.status_code != 200:
                self._download_manager.fail_external(self._item_id, f'status {resp.status_code}')
                return
            content_type = resp.headers.get('Content-Type', '').lower()
            if 'text/html' in content_type:
                self._download_manager.fail_external(self._item_id, 'download link expired or invalid')
                return
            if not total:
                total = int(resp.headers.get('Content-Length', 0))
            self._download_manager.update_external(self._item_id, total_bytes=total, progress=0.0 if total else None)
            downloaded = 0
            last_t = time.monotonic()
            last_bytes = 0
            with open(tmp_path, 'wb') as fh:
                for chunk in resp.iter_content(chunk_size=1024 * 128):
                    if self._cancelled:
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
                        self._download_manager.update_external(self._item_id, downloaded_bytes=downloaded, total_bytes=total, speed_down_kbps=speed_kbps)
                    else:
                        est = 100.0 * (1 - 1 / (1 + downloaded / (5 * 1024 * 1024)))
                        self._download_manager.update_external(self._item_id, downloaded_bytes=downloaded, total_bytes=0, progress=min(95.0, est), speed_down_kbps=speed_kbps)
            if downloaded == 0:
                try:
                    os.remove(tmp_path)
                except OSError:
                    pass
                self._download_manager.fail_external(self._item_id, 'empty response')
                return
            os.replace(tmp_path, self._dest_path)
            self._download_manager.complete_external(self._item_id, self._dest_path)
        except Exception as exc:
            try:
                if os.path.isfile(tmp_path):
                    os.remove(tmp_path)
            except OSError:
                pass
            self._download_manager.fail_external(self._item_id, str(exc))
        finally:
            if resp is not None:
                resp.close()


class BookDownloadBridge(QObject):

    def __init__(self, download_manager, parent=None):
        super().__init__(parent)
        from src.core.downloader import DLState
        self._DLState = DLState
        self._download_manager = download_manager
        self._pool = QThreadPool(self)
        self._pool.setMaxThreadCount(2)
        self._jobs: dict[str, BookDownloadJob] = {}
        self._download_manager.item_updated.connect(self._on_item_updated)

    def download(self, details: BookItem, format_mime: str) -> Optional[str]:
        file_url = details.formats.get(format_mime)
        if not file_url:
            refreshed = details_for_entry(details, use_cache=False)
            file_url = refreshed.formats.get(format_mime) if refreshed is not None else None
        if not file_url:
            item_id = self._download_manager.add_external(game_name=details.title, console=details.author, source='Books', category='books')
            self._download_manager.fail_external(item_id, 'could not resolve download link')
            return item_id
        from src.core.config import settings
        dest_dir = getattr(settings, 'download_dir_books', os.path.join(settings.download_dir, 'books'))
        filename = f'{safe_filename(details.title)}.{format_extension(format_mime)}'
        dest_path = os.path.join(dest_dir, filename)
        item_id = self._download_manager.add_external(game_name=details.title, console=details.author, source='Books', category='books')
        job = BookDownloadJob(file_url, dest_path, item_id, self._download_manager)
        self._jobs[item_id] = job
        self._download_manager.register_external_cancel(item_id, job.cancel)
        self._pool.start(job)
        return item_id

    def _on_item_updated(self, item_id: str) -> None:
        job = self._jobs.get(item_id)
        if job is None:
            return
        item = self._download_manager.get(item_id)
        if item is not None and item.state in (self._DLState.completed, self._DLState.error, self._DLState.cancelled):
            self._jobs.pop(item_id, None)

    def shutdown(self) -> None:
        for job in list(self._jobs.values()):
            job.cancel()
        self._pool.waitForDone(3000)

