from __future__ import annotations

import functools
import hashlib
import logging
import os
from collections import OrderedDict
from urllib.parse import urlsplit

import requests
from PySide6.QtCore import QBuffer, QByteArray, QObject, QRunnable, QThreadPool, Qt, Signal
from PySide6.QtGui import QImage, QImageReader

from src.core.config import paths

logger = logging.getLogger(__name__)

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept-Encoding": "gzip, deflate, br",
}

_session = requests.Session()
_session.headers.update(_HEADERS)
_session.trust_env = False
_adapter = requests.adapters.HTTPAdapter(pool_connections=16, pool_maxsize=16, max_retries=0)
_session.mount("https://", _adapter)
_session.mount("http://", _adapter)

_cover_pool = QThreadPool()
_cover_pool.setMaxThreadCount(4)


@functools.lru_cache(maxsize=2048)
def _resolve_cover_url_cached(book_id: int, md5_lower: str, isbn_str: str) -> str | None:
    try:
        bid = int(book_id) if book_id else 0
        folder_bucket = (bid // 1000) * 1000
        url = f"https://libgen.li/covers/{folder_bucket}/{md5_lower.lower()}.jpg"
        resp = _session.head(url, timeout=5, allow_redirects=True)
        if resp.status_code == 200:
            return url
    except Exception:
        pass

    isbn_val = (isbn_str or "").strip().replace("-", "")
    if len(isbn_val) in (10, 13):
        fallback = f"https://covers.openlibrary.org/b/isbn/{isbn_val}-L.jpg"
        try:
            resp = _session.head(fallback, timeout=5, allow_redirects=True)
            if resp.status_code == 200:
                return fallback
        except Exception:
            pass

    return None


def get_cover_url(book_id: str, md5: str = "", isbn: str = "") -> str | None:
    bid = int(book_id) if book_id else 0
    md5_lower = (md5 or "").lower()
    return _resolve_cover_url_cached(bid, md5_lower, isbn or "")


def has_libgen_cover(book_id: str, md5: str = "", isbn: str = "") -> bool:
    return get_cover_url(book_id, md5, isbn) is not None


class _CoverSignals(QObject):
    resolved = Signal(str, object)


class _CoverTask(QRunnable):
    def __init__(self, book_id: str, md5: str, isbn: str, signals: _CoverSignals, request_key: str):
        super().__init__()
        self._book_id = book_id
        self._md5 = md5
        self._isbn = isbn
        self._signals = signals
        self._request_key = request_key
        self.setAutoDelete(True)

    def run(self) -> None:
        url = get_cover_url(self._book_id, self._md5, self._isbn)
        self._signals.resolved.emit(self._request_key, url)


class CoverResolver(QObject):
    cover_resolved = Signal(str, object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._signals = _CoverSignals()
        self._signals.resolved.connect(self.cover_resolved)
        self._pending: set[str] = set()

    def request(self, book_id: str, md5: str = "", isbn: str = "") -> None:
        key = f"{book_id}:{md5}:{isbn}"
        if key in self._pending:
            return
        self._pending.add(key)
        task = _CoverTask(book_id, md5, isbn, self._signals, key)
        _cover_pool.start(task)

    def cover_resolved_and_clear(self, key: str) -> None:
        self._pending.discard(key)


cover_resolver = CoverResolver()
cover_resolver.cover_resolved.connect(lambda key, url: cover_resolver.cover_resolved_and_clear(key))

_ART_ROOT = os.path.join(paths.cache_dir, "artwork")
_THUMB_DIM = 160
_FULL_DIM = 512
_THUMB_QUALITY = 78
_FULL_QUALITY = 84
_MEM_LIMIT = 48
_MAX_DOWNLOAD_BYTES = 12 * 1024 * 1024
_DL_CHUNK = 1 << 16
_CACHE_VERSION = "v2"


def _dir_for(kind: str) -> str:
    d = os.path.join(_ART_ROOT, kind)
    os.makedirs(d, exist_ok=True)
    return d


def _origin_headers(url: str) -> dict:
    parts = urlsplit(url)
    if not parts.scheme or not parts.netloc:
        return {}
    origin = f"{parts.scheme}://{parts.netloc}"
    return {"Referer": origin + "/", "Origin": origin}


def _digest(url: str) -> str:
    return hashlib.blake2b(f"{_CACHE_VERSION}:{url}".encode("utf-8"), digest_size=16).hexdigest()


def full_path(kind: str, url: str) -> str:
    return os.path.join(_dir_for(kind), f"{_digest(url)}.webp")


def thumb_path(kind: str, url: str) -> str:
    return os.path.join(_dir_for(kind), f"{_digest(url)}_thumb.webp")


def has_full(kind: str, url: str) -> bool:
    return os.path.isfile(full_path(kind, url))


def has_thumb(kind: str, url: str) -> bool:
    return os.path.isfile(thumb_path(kind, url))


def _fetch_bytes(url: str) -> bytes:
    with _session.get(url, timeout=15, headers=_origin_headers(url), stream=True) as resp:
        resp.raise_for_status()
        buf = bytearray()
        for chunk in resp.iter_content(chunk_size=_DL_CHUNK):
            if not chunk:
                continue
            buf += chunk
            if len(buf) >= _MAX_DOWNLOAD_BYTES:
                break
        return bytes(buf[:_MAX_DOWNLOAD_BYTES])


def _decode_qt(raw_bytes: bytes) -> QImage:
    reader = QImageReader()
    reader.setDecideFormatFromContent(True)
    buf = QBuffer()
    buf.setData(QByteArray(raw_bytes))
    buf.open(QBuffer.OpenModeFlag.ReadOnly)
    reader.setDevice(buf)
    return reader.read()


def _save_scaled_qt(image: QImage, dest_path: str, max_dim: int, quality: int) -> bool:
    if image.isNull():
        return False
    if image.width() > max_dim or image.height() > max_dim:
        scale = max_dim / max(image.width(), image.height())
        image = image.scaled(
            max(1, round(image.width() * scale)),
            max(1, round(image.height() * scale)),
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
    if not image.hasAlphaChannel():
        image = image.convertToFormat(QImage.Format.Format_RGB32)
    return image.save(dest_path, "WEBP", quality)


def _encode_variants(raw_bytes: bytes, jobs: list[tuple[str, int, int]]) -> bool:
    image = _decode_qt(raw_bytes)
    if image.isNull():
        logger.warning("Artwork: could not decode image")
        return False
    ok = True
    for dest_path, max_dim, quality in jobs:
        if not _save_scaled_qt(image, dest_path, max_dim, quality):
            logger.warning("Artwork: failed to save %s", dest_path)
            ok = False
    return ok


class _FetchSignals(QObject):
    finished = Signal(str, str, str, bool)
    failed = Signal(str, str, str)


class _FetchTask(QRunnable):
    def __init__(self, kind: str, url: str, want_full: bool, signals: _FetchSignals):
        super().__init__()
        self._kind = kind
        self._url = url
        self._want_full = want_full
        self._signals = signals
        self.setAutoDelete(True)

    def run(self) -> None:
        try:
            raw = _fetch_bytes(self._url)
            if not raw:
                raise ValueError("empty response body")

            t_path = thumb_path(self._kind, self._url)
            f_path = full_path(self._kind, self._url)
            need_thumb = not os.path.isfile(t_path)
            need_full = not os.path.isfile(f_path)

            jobs: list[tuple[str, int, int]] = []
            if need_thumb:
                jobs.append((t_path, _THUMB_DIM, _THUMB_QUALITY))
            if need_full:
                jobs.append((f_path, _FULL_DIM, _FULL_QUALITY))

            if jobs and not _encode_variants(raw, jobs):
                raise RuntimeError("artwork encode failed")
            del raw

            self._signals.finished.emit(self._kind, self._url, t_path, False)
            if self._want_full:
                self._signals.finished.emit(self._kind, self._url, f_path, True)
        except Exception as exc:
            logger.warning("Artwork fetch failed [%s] %s: %s", self._kind, self._url, exc)
            self._signals.failed.emit(self._kind, self._url, str(exc))


class ArtworkManager(QObject):
    thumb_ready = Signal(str, str, str)
    full_ready = Signal(str, str, str)
    failed = Signal(str, str, str)

    def __init__(self, parent=None, max_concurrent: int = 4):
        super().__init__(parent)
        self._pool = QThreadPool()
        self._pool.setMaxThreadCount(max_concurrent)
        self._signals = _FetchSignals()
        self._signals.finished.connect(self._on_finished)
        self._signals.failed.connect(self._on_failed)
        self._in_flight: set[tuple[str, str, bool]] = set()
        self._retry_counts: dict[tuple[str, str], int] = {}
        self._mem: "OrderedDict[str, QImage]" = OrderedDict()
        self._shutting_down = False

    def shutdown(self, wait_ms: int = 2000) -> None:
        self._shutting_down = True
        self._in_flight.clear()
        self._retry_counts.clear()
        self._mem.clear()
        self._pool.waitForDone(wait_ms)

    def _mem_key(self, kind: str, url: str, full: bool) -> str:
        return f"{kind}:{url}:{'full' if full else 'thumb'}"

    def cached_image(self, kind: str, url: str, full: bool = False):
        key = self._mem_key(kind, url, full)
        img = self._mem.get(key)
        if img is not None:
            self._mem.move_to_end(key)
        return img

    def _store_mem(self, kind: str, url: str, full: bool, path: str) -> None:
        key = self._mem_key(kind, url, full)
        img = QImage(path)
        if img.isNull():
            return
        self._mem[key] = img
        self._mem.move_to_end(key)
        while len(self._mem) > _MEM_LIMIT:
            self._mem.popitem(last=False)

    def request(self, kind: str, url: str, want_full: bool = False) -> None:
        if not url or self._shutting_down:
            return

        if not want_full and has_thumb(kind, url):
            self.thumb_ready.emit(kind, url, thumb_path(kind, url))
            return
        if want_full and has_full(kind, url):
            self.full_ready.emit(kind, url, full_path(kind, url))
            if has_thumb(kind, url):
                self.thumb_ready.emit(kind, url, thumb_path(kind, url))
            return

        flight_key = (kind, url, want_full)
        if flight_key in self._in_flight:
            return
        self._in_flight.add(flight_key)
        task = _FetchTask(kind, url, want_full, self._signals)
        self._pool.start(task)

    def _on_finished(self, kind: str, url: str, path: str, is_full: bool) -> None:
        self._in_flight.discard((kind, url, is_full))
        self._retry_counts.pop((kind, url), None)
        if self._shutting_down:
            return
        if is_full:
            self.full_ready.emit(kind, url, path)
        else:
            self.thumb_ready.emit(kind, url, path)

    def _on_failed(self, kind: str, url: str, error: str) -> None:
        self._in_flight.discard((kind, url, False))
        self._in_flight.discard((kind, url, True))
        if self._shutting_down:
            return
        attempts = self._retry_counts.get((kind, url), 0)
        if attempts < 2:
            self._retry_counts[(kind, url)] = attempts + 1
            self.request(kind, url)
            return
        self._retry_counts.pop((kind, url), None)
        self.failed.emit(kind, url, error)


artwork = ArtworkManager()
