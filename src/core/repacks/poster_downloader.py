from __future__ import annotations

import logging

from PyQt6.QtCore import QObject, QRunnable, QThreadPool, pyqtSignal

from . import cache

logger = logging.getLogger(__name__)

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    )
}


class _PosterFetchSignals(QObject):
    finished = pyqtSignal(str, str)
    failed = pyqtSignal(str, str)


class _PosterFetchTask(QRunnable):
    def __init__(self, url: str):
        super().__init__()
        self.url = url
        self.signals = _PosterFetchSignals()

    def run(self) -> None:
        import requests

        dest_path = cache.poster_cache_path(self.url)
        try:
            resp = requests.get(self.url, headers=_HEADERS, timeout=15)
            resp.raise_for_status()
            with open(dest_path, "wb") as fh:
                fh.write(resp.content)
            self.signals.finished.emit(self.url, dest_path)
        except Exception as exc:
            logger.warning("Failed to download poster %s: %s", self.url, exc)
            self.signals.failed.emit(self.url, str(exc))


class PosterDownloader(QObject):
    poster_ready = pyqtSignal(str, str)
    poster_failed = pyqtSignal(str, str)

    def __init__(self, parent=None, max_concurrent: int = 6):
        super().__init__(parent)
        self._pool = QThreadPool()
        self._pool.setMaxThreadCount(max_concurrent)
        self._in_flight: set[str] = set()

    def shutdown(self, wait_ms: int = 2000) -> None:
        self._pool.clear()
        self._pool.waitForDone(wait_ms)
        self._in_flight.clear()

    def request(self, url: str) -> None:
        if not url:
            return
        if cache.has_cached_poster(url):
            self.poster_ready.emit(url, cache.poster_cache_path(url))
            return
        if url in self._in_flight:
            return
        self._in_flight.add(url)

        task = _PosterFetchTask(url)
        task.signals.finished.connect(self._on_finished)
        task.signals.failed.connect(self._on_failed)
        self._pool.start(task)

    def _on_finished(self, url: str, path: str) -> None:
        self._in_flight.discard(url)
        self.poster_ready.emit(url, path)

    def _on_failed(self, url: str, error: str) -> None:
        self._in_flight.discard(url)
        self.poster_failed.emit(url, error)
