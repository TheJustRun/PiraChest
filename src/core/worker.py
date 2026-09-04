from __future__ import annotations

import logging
import threading
import uuid
from enum import IntEnum

from PySide6.QtCore import QObject, QRunnable, QThreadPool, Signal
from shiboken6 import isValid as _qt_is_valid

logger = logging.getLogger(__name__)


class Priority(IntEnum):
    LOW = 0
    NORMAL = 5
    HIGH = 10


class _Signals(QObject):
    finished = Signal(str, object)
    failed = Signal(str, str)
    progress = Signal(object)


class _Task(QRunnable):
    def __init__(self, task_id: str, fn, args, kwargs, signals: _Signals, cancel_event: threading.Event):
        super().__init__()
        self._task_id = task_id
        self._fn = fn
        self._args = args
        self._kwargs = kwargs
        self._signals = signals
        self._cancel_event = cancel_event
        self.setAutoDelete(True)

    def run(self) -> None:
        if self._cancel_event.is_set():
            return
        try:
            if "is_cancelled" in self._fn.__code__.co_varnames:
                self._kwargs.setdefault("is_cancelled", self._cancel_event.is_set)
            result = self._fn(*self._args, **self._kwargs)
        except Exception as exc:
            if not self._cancel_event.is_set():
                logger.exception("Task %s failed", self._task_id)
                if _qt_is_valid(self._signals):
                    try:
                        self._signals.failed.emit(self._task_id, str(exc))
                    except RuntimeError:
                        pass
            return
        if self._cancel_event.is_set():
            return
        if _qt_is_valid(self._signals):
            try:
                self._signals.finished.emit(self._task_id, result)
            except RuntimeError:
                pass


class TaskManager(QObject):
    def __init__(self, max_threads: int = 8):
        super().__init__()
        self._pool = QThreadPool.globalInstance()
        self._pool.setMaxThreadCount(max_threads)
        self._signals = _Signals()
        self._signals.finished.connect(self._on_finished)
        self._signals.failed.connect(self._on_failed)
        self._signals.progress.connect(self._on_progress)
        self._callbacks: dict[str, tuple[object, object]] = {}
        self._cancel_events: dict[str, threading.Event] = {}
        self._lock = threading.Lock()

    def submit(self, fn, args=(), kwargs=None, on_done=None, on_error=None, priority: Priority = Priority.NORMAL) -> str:
        task_id = uuid.uuid4().hex
        cancel_event = threading.Event()
        with self._lock:
            self._callbacks[task_id] = (on_done, on_error)
            self._cancel_events[task_id] = cancel_event
        task = _Task(task_id, fn, args, dict(kwargs or {}), self._signals, cancel_event)
        self._pool.start(task, priority=int(priority))
        return task_id

    def wrap_callback(self, callback):
        def _proxy(*args, **kwargs) -> None:
            if not _qt_is_valid(self._signals):
                return
            try:
                self._signals.progress.emit((callback, args, kwargs))
            except RuntimeError:
                pass
        return _proxy

    def _on_progress(self, payload) -> None:
        callback, args, kwargs = payload
        if callback is None:
            return
        try:
            callback(*args, **kwargs)
        except RuntimeError:
            logger.debug("progress callback target deleted")

    def cancel(self, task_id: str) -> None:
        with self._lock:
            event = self._cancel_events.get(task_id)
            if event is not None:
                event.set()

    def _pop(self, task_id: str):
        with self._lock:
            self._cancel_events.pop(task_id, None)
            return self._callbacks.pop(task_id, (None, None))

    def _on_finished(self, task_id: str, result) -> None:
        on_done, _ = self._pop(task_id)
        if on_done is None:
            return
        try:
            on_done(result)
        except RuntimeError:
            logger.debug("on_done target deleted for task %s", task_id)

    def _on_failed(self, task_id: str, error: str) -> None:
        _, on_error = self._pop(task_id)
        if on_error is None:
            return
        try:
            on_error(error)
        except RuntimeError:
            logger.debug("on_error target deleted for task %s", task_id)

    def shutdown(self, wait_ms: int = 3000) -> None:
        with self._lock:
            for event in self._cancel_events.values():
                event.set()
        self._pool.waitForDone(wait_ms)


tasks = TaskManager()


def submit(fn, args=(), kwargs=None, on_done=None, on_error=None, priority: Priority = Priority.NORMAL) -> str:
    return tasks.submit(fn, args=args, kwargs=kwargs, on_done=on_done, on_error=on_error, priority=priority)


def cancel(task_id: str) -> None:
    tasks.cancel(task_id)


def wrap_callback(callback):
    return tasks.wrap_callback(callback)


def submit_coro(coro_fn, args=(), kwargs=None, on_done=None, on_error=None, priority: Priority = Priority.NORMAL) -> str:
    import asyncio as _asyncio

    def _runner(*a, **kw):
        return _asyncio.run(coro_fn(*a, **kw))

    return submit(_runner, args=args, kwargs=kwargs, on_done=on_done, on_error=on_error, priority=priority)


_repack_source_instances: dict[str, object] = {}


def _resolve_repack_source(source_key: str):
    if source_key in _repack_source_instances:
        return _repack_source_instances[source_key]

    source = None
    if source_key == "fitgirl":
        from src.core.repacks.sources.fitgirl import FitGirlSource
        source = FitGirlSource()
    elif source_key == "gog":
        from src.core.repacks.sources.gog import GogRevivedSource
        source = GogRevivedSource()

    if source is not None:
        _repack_source_instances[source_key] = source
    return source


def _do_fetch_repack_page(source_key: str, page: int, use_cache: bool = True):
    source = _resolve_repack_source(source_key)
    if source is None:
        raise RuntimeError(f"Unknown repack source: {source_key}")
    return source.fetch_page(page, use_cache=use_cache)


def _do_fetch_repack_details(source_key: str, entry, use_cache: bool = True):
    source = _resolve_repack_source(source_key)
    if source is None:
        raise RuntimeError(f"Unknown repack source: {source_key}")
    return source.fetch_details(entry, use_cache=use_cache)


def _do_fetch_upcoming_repacks(source_key: str, use_cache: bool = True):
    source = _resolve_repack_source(source_key)
    if source is None:
        raise RuntimeError(f"Unknown repack source: {source_key}")
    return source.fetch_upcoming_repacks(use_cache=use_cache)


def _do_fetch_latest_repacks(source_key: str, use_cache: bool = True):
    source = _resolve_repack_source(source_key)
    return source.fetch_latest_repacks(use_cache=use_cache) if source is not None else []


def _do_fetch_popular_repacks(source_key: str, use_cache: bool = True):
    source = _resolve_repack_source(source_key)
    return source.fetch_popular_repacks(use_cache=use_cache) if source is not None else []


def _do_search_repacks(source_key: str, query: str, page: int, use_cache: bool = True):
    source = _resolve_repack_source(source_key)
    if source is None:
        raise RuntimeError(f"Unknown repack source: {source_key}")
    try:
        return source.search(query, page, use_cache=use_cache)
    except NotImplementedError:
        raise RuntimeError("__no_search__")


def fetch_page_async(source_key: str, page: int, on_done=None, on_error=None, use_cache: bool = True) -> str:
    return submit(_do_fetch_repack_page, args=(source_key, page), kwargs={"use_cache": use_cache}, on_done=on_done, on_error=on_error)


def fetch_details_async(source_key: str, entry, on_done=None, on_error=None, use_cache: bool = True) -> str:
    return submit(_do_fetch_repack_details, args=(source_key, entry), kwargs={"use_cache": use_cache}, on_done=on_done, on_error=on_error)


def fetch_upcoming_repacks_async(source_key: str, on_done=None, on_error=None, use_cache: bool = True) -> str:
    return submit(_do_fetch_upcoming_repacks, args=(source_key,), kwargs={"use_cache": use_cache}, on_done=on_done, on_error=on_error)


def fetch_latest_repacks_async(source_key: str, on_done=None, use_cache: bool = True) -> str:
    safe_done = on_done if on_done is not None else (lambda result: None)
    return submit(_do_fetch_latest_repacks, args=(source_key,), kwargs={"use_cache": use_cache}, on_done=safe_done, on_error=lambda error: safe_done([]))


def fetch_popular_repacks_async(source_key: str, on_done=None, use_cache: bool = True) -> str:
    safe_done = on_done if on_done is not None else (lambda result: None)
    return submit(_do_fetch_popular_repacks, args=(source_key,), kwargs={"use_cache": use_cache}, on_done=safe_done, on_error=lambda error: safe_done([]))


def fetch_search_async(source_key: str, query: str, page: int, on_done=None, on_error=None, use_cache: bool = True) -> str:
    return submit(_do_search_repacks, args=(source_key, query, page), kwargs={"use_cache": use_cache}, on_done=on_done, on_error=on_error)
