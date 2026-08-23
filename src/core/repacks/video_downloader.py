from __future__ import annotations
import hashlib
import logging
import os
import shutil
import requests
from PySide6.QtCore import QObject, QTimer, Signal
from src.core.artwork import artwork as _artwork
from src.core.config import paths
from src.core.worker import Priority, cancel as _cancel, submit as _submit

logger = logging.getLogger(__name__)
_VIDEO_DIR = os.path.join(paths.cache_dir, "repacks", "videos")
_CHUNK_SIZE = 1 << 18
_MAX_CONCURRENT_DOWNLOADS = 2

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept-Encoding": "gzip, deflate, br",
}
_session = requests.Session()
_session.headers.update(_HEADERS)
_adapter = requests.adapters.HTTPAdapter(pool_connections=_MAX_CONCURRENT_DOWNLOADS, pool_maxsize=_MAX_CONCURRENT_DOWNLOADS, max_retries=0)
_session.mount("https://", _adapter)
_session.mount("http://", _adapter)
_session.trust_env = False


def video_cache_path(url: str) -> str:
    os.makedirs(_VIDEO_DIR, exist_ok=True)
    digest = hashlib.blake2b(url.encode("utf-8"), digest_size=16).hexdigest()
    ext = os.path.splitext(url.split("?", 1)[0])[1]
    if not ext or len(ext) > 5:
        ext = ".webm"
    return os.path.join(_VIDEO_DIR, digest + ext)


def has_cached_video(url: str) -> bool:
    return os.path.isfile(video_cache_path(url))


class PosterDownloader(QObject):
    poster_ready = Signal(str, str)
    poster_failed = Signal(str, str)

    def __init__(self, parent=None, max_concurrent: int = 3, drain_batch: int = 2, drain_interval_ms: int = 40):
        super().__init__(parent)
        self._drain_batch = drain_batch
        self._ready_queue: list[tuple[str, str]] = []
        self._shutting_down = False
        self._drain_timer = QTimer(self)
        self._drain_timer.setInterval(drain_interval_ms)
        self._drain_timer.timeout.connect(self._drain_ready_queue)
        _artwork.full_ready.connect(self._on_ready)
        _artwork.failed.connect(self._on_failed)

    def shutdown(self, wait_ms: int = 2000) -> None:
        self._shutting_down = True
        self._drain_timer.stop()
        self._ready_queue.clear()

    def request(self, url: str) -> None:
        if not url or self._shutting_down:
            return
        _artwork.request("repack", url, want_full=True)

    def _on_ready(self, kind: str, url: str, path: str) -> None:
        if kind != "repack" or self._shutting_down:
            return
        self._ready_queue.append((url, path))
        if not self._drain_timer.isActive():
            self._drain_timer.start()

    def _drain_ready_queue(self) -> None:
        batch, self._ready_queue = self._ready_queue[: self._drain_batch], self._ready_queue[self._drain_batch:]
        for url, path in batch:
            self.poster_ready.emit(url, path)
        if not self._ready_queue:
            self._drain_timer.stop()

    def _on_failed(self, kind: str, url: str, error: str) -> None:
        if kind == "repack" and not self._shutting_down:
            self.poster_failed.emit(url, error)


def _do_download_video(url: str, is_cancelled=None) -> str:
    dest_path = video_cache_path(url)
    if os.path.isfile(dest_path):
        return dest_path
    tmp_path = dest_path + ".part"
    try:
        with _session.get(url, timeout=30, stream=True) as resp:
            resp.raise_for_status()
            with open(tmp_path, "wb", buffering=0) as fh:
                if is_cancelled is None:
                    shutil.copyfileobj(resp.raw, fh, length=_CHUNK_SIZE)
                else:
                    raw = resp.raw
                    raw.decode_content = True
                    while True:
                        chunk = raw.read(_CHUNK_SIZE)
                        if not chunk:
                            break
                        fh.write(chunk)
                        if is_cancelled():
                            raise RuntimeError("download cancelled")
        os.replace(tmp_path, dest_path)
        return dest_path
    except Exception:
        try:
            os.remove(tmp_path)
        except OSError:
            pass
        raise


class VideoDownloader(QObject):
    video_ready = Signal(str, str)
    video_failed = Signal(str, str)

    def __init__(self, parent=None, max_concurrent: int = _MAX_CONCURRENT_DOWNLOADS):
        super().__init__(parent)
        self._max_concurrent = max_concurrent
        self._active: dict[str, str] = {}
        self._queue: list[str] = []
        self._shutting_down = False

    def shutdown(self, wait_ms: int = 2000) -> None:
        self._shutting_down = True
        for task_id in self._active.values():
            _cancel(task_id)
        self._active.clear()
        self._queue.clear()

    def _emit_if_cached(self, url: str) -> bool:
        if has_cached_video(url):
            self.video_ready.emit(url, video_cache_path(url))
            return True
        return False

    def request(self, url: str) -> None:
        if not url or self._shutting_down:
            return
        if self._emit_if_cached(url):
            return
        if url in self._active or url in self._queue:
            return
        if len(self._active) < self._max_concurrent:
            self._start(url)
        else:
            self._queue.append(url)

    def _start(self, url: str) -> None:
        task_id = _submit(
            _do_download_video,
            args=(url,),
            on_done=lambda path, u=url: self._on_finished(u, path),
            on_error=lambda err, u=url: self._on_failed(u, err),
            priority=Priority.LOW,
        )
        self._active[url] = task_id

    def _advance_queue(self) -> None:
        while self._queue and len(self._active) < self._max_concurrent and not self._shutting_down:
            next_url = self._queue.pop(0)
            if self._emit_if_cached(next_url):
                continue
            self._start(next_url)

    def _on_finished(self, url: str, path: str) -> None:
        self._active.pop(url, None)
        if not self._shutting_down:
            self.video_ready.emit(url, path)
        self._advance_queue()

    def _on_failed(self, url: str, error: str) -> None:
        self._active.pop(url, None)
        if not self._shutting_down:
            logger.warning("Failed to download video %s: %s", url, error)
            self.video_failed.emit(url, error)
        self._advance_queue()
