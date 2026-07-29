from __future__ import annotations

import logging

from PyQt6.QtCore import QObject, QThread, pyqtSignal

from .base import RepackDetails, RepackEntry, RepackPage
from .sources import get_source

logger = logging.getLogger(__name__)

_IN_FLIGHT: set[tuple[QThread, QObject]] = set()


def _run_async(worker: QObject, on_done, on_error=None):
    thread = QThread()
    worker.moveToThread(thread)
    thread.started.connect(worker.run)

    entry = (thread, worker)
    _IN_FLIGHT.add(entry)

    def _safe_call(callback, *args):
        if callback is None:
            return
        try:
            callback(*args)
        except RuntimeError:
            logger.debug("Callback target was already deleted; skipping.")

    def _cleanup(*_args):
        thread.quit()
        thread.wait()
        _IN_FLIGHT.discard(entry)
        thread.deleteLater()
        worker.deleteLater()

    worker.finished.connect(lambda result: _safe_call(on_done, result))
    worker.finished.connect(_cleanup)
    worker.failed.connect(lambda err: _safe_call(on_error, err))
    worker.failed.connect(_cleanup)

    thread.start()
    return thread, worker


class RepackPageWorker(QObject):
    finished = pyqtSignal(object)
    failed = pyqtSignal(str)

    def __init__(self, source_key: str, page: int, use_cache: bool = True):
        super().__init__()
        self._source_key = source_key
        self._page = page
        self._use_cache = use_cache

    def run(self) -> None:
        try:
            source = get_source(self._source_key)
            result: RepackPage = source.fetch_page(self._page, use_cache=self._use_cache)
            self.finished.emit(result)
        except Exception as exc:
            logger.exception("Failed to fetch repack page %s (source=%s)", self._page, self._source_key)
            self.failed.emit(str(exc))


def fetch_page_async(source_key: str, page: int, on_done, on_error=None, use_cache: bool = True):
    worker = RepackPageWorker(source_key, page, use_cache=use_cache)
    return _run_async(worker, on_done, on_error)


class RepackSearchWorker(QObject):
    finished = pyqtSignal(object)
    failed = pyqtSignal(str)

    def __init__(self, source_key: str, query: str, page: int, use_cache: bool = True):
        super().__init__()
        self._source_key = source_key
        self._query = query
        self._page = page
        self._use_cache = use_cache

    def run(self) -> None:
        try:
            source = get_source(self._source_key)
            result: RepackPage = source.search(self._query, self._page, use_cache=self._use_cache)
            self.finished.emit(result)
        except NotImplementedError:
            self.failed.emit("__no_search__")
        except Exception as exc:
            logger.exception(
                "Failed to search repacks for %r (source=%s)", self._query, self._source_key
            )
            self.failed.emit(str(exc))


def fetch_search_async(source_key: str, query: str, page: int, on_done, on_error=None, use_cache: bool = True):
    worker = RepackSearchWorker(source_key, query, page, use_cache=use_cache)
    return _run_async(worker, on_done, on_error)


class RepackDetailsWorker(QObject):
    finished = pyqtSignal(object)
    failed = pyqtSignal(str)

    def __init__(self, source_key: str, entry: RepackEntry, use_cache: bool = True):
        super().__init__()
        self._source_key = source_key
        self._entry = entry
        self._use_cache = use_cache

    def run(self) -> None:
        try:
            source = get_source(self._source_key)
            details: RepackDetails = source.fetch_details(self._entry, use_cache=self._use_cache)
            self.finished.emit(details)
        except Exception as exc:
            logger.exception(
                "Failed to fetch repack details for %s (source=%s)", self._entry, self._source_key
            )
            self.failed.emit(str(exc))


def fetch_details_async(source_key: str, entry: RepackEntry, on_done, on_error=None, use_cache: bool = True):
    worker = RepackDetailsWorker(source_key, entry, use_cache=use_cache)
    return _run_async(worker, on_done, on_error)


class RepackUpcomingWorker(QObject):
    finished = pyqtSignal(object)
    failed = pyqtSignal(str)

    def __init__(self, source_key: str, use_cache: bool = True):
        super().__init__()
        self._source_key = source_key
        self._use_cache = use_cache

    def run(self) -> None:
        try:
            source = get_source(self._source_key)
            fetch_upcoming = getattr(source, "fetch_upcoming_repacks", None)
            if fetch_upcoming is None:
                self.failed.emit("__not_supported__")
                return
            details = fetch_upcoming(use_cache=self._use_cache)
            if details is None:
                self.failed.emit("No upcoming repacks post found")
                return
            self.finished.emit(details)
        except Exception as exc:
            logger.exception("Failed to fetch upcoming repacks (source=%s)", self._source_key)
            self.failed.emit(str(exc))


def fetch_upcoming_repacks_async(source_key: str, on_done, on_error=None, use_cache: bool = True):
    worker = RepackUpcomingWorker(source_key, use_cache=use_cache)
    return _run_async(worker, on_done, on_error)
