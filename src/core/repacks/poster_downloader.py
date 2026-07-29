from __future__ import annotations

import logging
import os

import requests
from PyQt6.QtCore import QBuffer, QByteArray, QObject, QRunnable, QThreadPool, pyqtSignal
from PyQt6.QtGui import QImage, QImageReader

from . import cache

logger = logging.getLogger(__name__)

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    )
}

_session = requests.Session()
_session.headers.update(_HEADERS)

_MAX_POSTER_DIM = 480


def _downscale_and_save(raw_bytes: bytes, dest_path: str) -> None:
    reader = QImageReader()
    reader.setDecideFormatFromContent(True)

    buf = QBuffer()
    buf.setData(QByteArray(raw_bytes))
    buf.open(QBuffer.OpenModeFlag.ReadOnly)
    reader.setDevice(buf)

    size = reader.size()
    needs_resize = size.isValid() and (size.width() > _MAX_POSTER_DIM or size.height() > _MAX_POSTER_DIM)

    if not needs_resize:
        with open(dest_path, "wb") as fh:
            fh.write(raw_bytes)
        return

    scale = _MAX_POSTER_DIM / max(size.width(), size.height())
    reader.setScaledSize(size * scale)

    image: QImage = reader.read()
    if image.isNull():
        with open(dest_path, "wb") as fh:
            fh.write(raw_bytes)
        return

    ext = os.path.splitext(dest_path)[1].lower()
    fmt = "JPG" if ext in (".jpg", ".jpeg") else "PNG"
    quality = 85 if fmt == "JPG" else -1
    if not image.save(dest_path, fmt, quality):
        with open(dest_path, "wb") as fh:
            fh.write(raw_bytes)


class _PosterFetchSignals(QObject):
    finished = pyqtSignal(str, str)
    failed = pyqtSignal(str, str)


class _PosterFetchTask(QRunnable):
    def __init__(self, url: str):
        super().__init__()
        self.url = url
        self.signals = _PosterFetchSignals()

    def run(self) -> None:
        dest_path = cache.poster_cache_path(self.url)
        try:
            resp = _session.get(self.url, timeout=15)
            resp.raise_for_status()
            _downscale_and_save(resp.content, dest_path)
            self.signals.finished.emit(self.url, dest_path)
        except Exception as exc:
            logger.warning("Failed to download poster %s: %s", self.url, exc)
            self.signals.failed.emit(self.url, str(exc))


class PosterDownloader(QObject):
    poster_ready = pyqtSignal(str, str)
    poster_failed = pyqtSignal(str, str)

    def __init__(self, parent=None, max_concurrent: int = 3, drain_batch: int = 2, drain_interval_ms: int = 40):
        super().__init__(parent)
        self._pool = QThreadPool()
        self._pool.setMaxThreadCount(max_concurrent)
        self._in_flight: set[str] = set()
        self._drain_batch = drain_batch
        self._ready_queue: list[tuple[str, str]] = []
        from PyQt6.QtCore import QTimer
        self._drain_timer = QTimer(self)
        self._drain_timer.setInterval(drain_interval_ms)
        self._drain_timer.timeout.connect(self._drain_ready_queue)

    def shutdown(self, wait_ms: int = 2000) -> None:
        self._drain_timer.stop()
        self._ready_queue.clear()
        self._in_flight.clear()
        self._pool.waitForDone(wait_ms)

    def request(self, url: str) -> None:
        if not url:
            return
        if cache.has_cached_poster(url):
            self._enqueue_ready(url, cache.poster_cache_path(url))
            return
        if url in self._in_flight:
            return
        self._in_flight.add(url)

        task = _PosterFetchTask(url)
        task.signals.finished.connect(self._on_finished)
        task.signals.failed.connect(self._on_failed)
        self._pool.start(task)

    def _enqueue_ready(self, url: str, path: str) -> None:
        self._ready_queue.append((url, path))
        if not self._drain_timer.isActive():
            self._drain_timer.start()

    def _drain_ready_queue(self) -> None:
        batch, self._ready_queue = self._ready_queue[: self._drain_batch], self._ready_queue[self._drain_batch :]
        for url, path in batch:
            self.poster_ready.emit(url, path)
        if not self._ready_queue:
            self._drain_timer.stop()

    def _on_finished(self, url: str, path: str) -> None:
        self._in_flight.discard(url)
        self._enqueue_ready(url, path)

    def _on_failed(self, url: str, error: str) -> None:
        self._in_flight.discard(url)
        self.poster_failed.emit(url, error)


class _VideoFetchSignals(QObject):
    finished = pyqtSignal(str, str)
    failed = pyqtSignal(str, str)


class _VideoFetchTask(QRunnable):
    def __init__(self, url: str):
        super().__init__()
        self.url = url
        self.signals = _VideoFetchSignals()

    def run(self) -> None:
        dest_path = cache.video_cache_path(self.url)
        try:
            with requests.get(self.url, headers=_HEADERS, timeout=30, stream=True) as resp:
                resp.raise_for_status()
                tmp_path = dest_path + '.part'
                with open(tmp_path, 'wb') as fh:
                    for chunk in resp.iter_content(chunk_size=1 << 16):
                        if chunk:
                            fh.write(chunk)
                os.replace(tmp_path, dest_path)
            self.signals.finished.emit(self.url, dest_path)
        except Exception as exc:
            logger.warning("Failed to download video %s: %s", self.url, exc)
            self.signals.failed.emit(self.url, str(exc))


class VideoDownloader(QObject):

    video_ready = pyqtSignal(str, str)
    video_failed = pyqtSignal(str, str)

    def __init__(self, parent=None, max_concurrent: int = 2):
        super().__init__(parent)
        self._pool = QThreadPool()
        self._pool.setMaxThreadCount(max_concurrent)
        self._in_flight: set[str] = set()

    def shutdown(self, wait_ms: int = 2000) -> None:
        self._in_flight.clear()
        self._pool.waitForDone(wait_ms)

    def request(self, url: str) -> None:
        if not url:
            return
        if cache.has_cached_video(url):
            self.video_ready.emit(url, cache.video_cache_path(url))
            return
        if url in self._in_flight:
            return
        self._in_flight.add(url)

        task = _VideoFetchTask(url)
        task.signals.finished.connect(self._on_finished)
        task.signals.failed.connect(self._on_failed)
        self._pool.start(task)

    def _on_finished(self, url: str, path: str) -> None:
        self._in_flight.discard(url)
        self.video_ready.emit(url, path)

    def _on_failed(self, url: str, error: str) -> None:
        self._in_flight.discard(url)
        self.video_failed.emit(url, error)
