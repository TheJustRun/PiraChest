from __future__ import annotations

import heapq
import inspect
import itertools
import logging
import threading
import time
import weakref
from collections import deque
from enum import IntEnum
from typing import Any, Callable, Optional

from PySide6.QtCore import QCoreApplication, QEvent, QObject, QThread, QTimer, Signal
from shiboken6 import isValid as _alive

logger = logging.getLogger(__name__)


class Priority(IntEnum):
    PREFETCH = 0
    LOW = 2
    NORMAL = 5
    VISIBLE = 8
    HIGH = 10


IO = "io"
CPU = "cpu"
DL = "dl"


_DRAIN_BUDGET_MS = 16.0


def _ref_live(ref) -> bool:
    if ref is None:
        return True
    o = ref()
    return o is not None and _alive(o)


def _accepts(fn: Callable, name: str) -> bool:
    try:
        return name in inspect.signature(fn).parameters
    except (TypeError, ValueError):
        return False


class Task:
    __slots__ = ("id", "key", "pool", "_prio", "_seq", "_fn", "_args", "_kwargs",
                 "_subs", "_cancelled", "_state", "_owner_ids", "_engine", "_wants_cancel")

    QUEUED, RUNNING, DONE = 0, 1, 2

    def __init__(self, engine, tid, key, pool, prio, seq, fn, args, kwargs):
        self._engine = engine
        self.id = tid
        self.key = key
        self.pool = pool
        self._prio = prio
        self._seq = seq
        self._fn = fn
        self._args = args
        self._kwargs = kwargs
        self._subs: list = []
        self._owner_ids: set = set()
        self._cancelled = threading.Event()
        self._state = Task.QUEUED
        self._wants_cancel = _accepts(fn, "is_cancelled")

    @property
    def cancelled(self) -> bool:
        return self._cancelled.is_set()


class Ticket:
    __slots__ = ("task", "sub_id")

    def __init__(self, task: Task, sub_id: int) -> None:
        self.task = task
        self.sub_id = sub_id

    def cancel(self) -> None:
        self.task._engine.cancel(self)

    @property
    def cancelled(self) -> bool:
        return self.task.cancelled

    @property
    def id(self) -> int:
        return self.sub_id


class _Inbox(QObject):
    _EVT = QEvent.Type(QEvent.registerEventType())

    def __init__(self) -> None:
        super().__init__()
        self._q: deque = deque()
        self._latest: dict = {}
        self._lock = threading.Lock()
        self._armed = False
        self._closed = False


    def post(self, fn: Callable, coalesce: Any = None) -> None:
        with self._lock:
            if self._closed:
                return
            if coalesce is not None:
                first = coalesce not in self._latest
                self._latest[coalesce] = fn
                if first:
                    self._q.append(coalesce)
            else:
                self._q.append(fn)
            if self._armed:
                return
            self._armed = True
        QCoreApplication.postEvent(self, QEvent(self._EVT))

    def close(self) -> None:
        with self._lock:
            self._closed = True
            self._q.clear()
            self._latest.clear()


    def customEvent(self, event) -> None:
        if event.type() != self._EVT:
            return
        deadline = time.perf_counter() + _DRAIN_BUDGET_MS / 1000.0
        while True:
            with self._lock:
                if not self._q:
                    self._armed = False
                    return
                item = self._q.popleft()
                fn = self._latest.pop(item) if not callable(item) else item
            try:
                fn()
            except Exception:
                logger.warning("inbox callback failed", exc_info=True)
            if time.perf_counter() >= deadline:
                with self._lock:
                    if not self._q:
                        self._armed = False
                        return
                QCoreApplication.postEvent(self, QEvent(self._EVT))
                return


class _Pool:
    def __init__(self, name: str, size: int) -> None:
        self.name = name
        self._max_threads = max(1, int(size))
        self._heap: list = []
        self._cv = threading.Condition()
        self._closed = False
        self._threads: list[threading.Thread] = []
        self._runner: Optional[Callable[[Task], None]] = None

    def start(self, runner: Callable[[Task], None]) -> None:
        self._runner = runner

    def push(self, task: Task) -> bool:
        with self._cv:
            if self._closed:
                return False
            heapq.heappush(self._heap, (-task._prio, task._seq, task))
            if len(self._threads) < self._max_threads:
                thread = threading.Thread(
                    target=self._loop,
                    name=f"{self.name}-{len(self._threads)}",
                    daemon=True,
                )
                self._threads.append(thread)
                thread.start()
            self._cv.notify()
            return True

    def rebuild(self) -> None:
        with self._cv:
            self._heap = [(-t._prio, t._seq, t) for _, _, t in self._heap if t._state == Task.QUEUED]
            heapq.heapify(self._heap)

    def _loop(self) -> None:
        while True:
            with self._cv:
                while not self._heap and not self._closed:
                    self._cv.wait()
                if self._closed:
                    return
                _, _, task = heapq.heappop(self._heap)
                if task._cancelled.is_set() or task._state != Task.QUEUED:
                    continue
                task._state = Task.RUNNING
            self._runner(task)

    def close(self) -> None:
        with self._cv:
            self._closed = True
            self._heap.clear()
            self._cv.notify_all()

    def join(self, timeout_s: float) -> None:
        end = time.monotonic() + timeout_s
        for t in self._threads:
            t.join(max(0.0, end - time.monotonic()))

    def alive_count(self) -> int:
        return sum(t.is_alive() for t in self._threads)


class TaskEngine:
    def __init__(self, io_threads: int = 4, cpu_threads: int = 2, dl_threads: int = 2) -> None:
        self._inbox = _Inbox()
        app = QCoreApplication.instance()
        if app is not None and QThread.currentThread() != app.thread():
            self._inbox.moveToThread(app.thread())
        self._pools = {IO: _Pool("pc-io", io_threads), CPU: _Pool("pc-cpu", cpu_threads), DL: _Pool("pc-dl", dl_threads)}
        for p in self._pools.values():
            p.start(self._run)
        self._lock = threading.Lock()
        self._seq = itertools.count()
        self._sub_seq = itertools.count(1)
        self._tasks: dict[int, Task] = {}
        self._by_key: dict[Any, Task] = {}
        self._by_owner: dict[int, set[int]] = {}
        self._hooked: set[int] = set()
        self._closed = False

    def submit(self, fn: Callable, args: tuple = (), kwargs: Optional[dict] = None, *,
               on_done: Optional[Callable] = None, on_error: Optional[Callable] = None,
               owner: Optional[QObject] = None, key: Any = None,
               priority: int = Priority.NORMAL, pool: str = IO) -> Optional[Ticket]:
        with self._lock:
            if self._closed:
                return None
            pool = pool if pool in self._pools else IO
            task = self._by_key.get(key) if key is not None else None
            if task is not None and not task.cancelled:
                sid = self._subscribe(task, on_done, on_error, owner)
                promote = int(priority) > task._prio and task._state == Task.QUEUED
                if int(priority) > task._prio:
                    task._prio = int(priority)
                if promote:
                    self._pools[task.pool].rebuild()
                return Ticket(task, sid)
            tid = next(self._seq)
            task = Task(self, tid, key, pool, int(priority), tid, fn, args, dict(kwargs or {}))
            self._tasks[tid] = task
            if key is not None:
                self._by_key[key] = task
            sid = self._subscribe(task, on_done, on_error, owner)
        if not self._pools[pool].push(task):
            with self._lock:
                self._finish(task)
            return None
        return Ticket(task, sid)

    def cancel(self, ticket) -> None:
        if ticket is None:
            return
        if isinstance(ticket, Ticket):
            t, sid = ticket.task, ticket.sub_id
        elif isinstance(ticket, Task):
            t, sid = ticket, None
        else:
            return
        with self._lock:
            if sid is None:
                t._subs.clear()
            else:
                t._subs = [x for x in t._subs if x[3] != sid]
            self._maybe_cancel(t)

    def cancel_owner(self, owner: QObject) -> None:
        oid = id(owner)
        with self._lock:
            ids = self._by_owner.pop(oid, set())
            for tid in ids:
                t = self._tasks.get(tid)
                if t is None:
                    continue
                t._subs = [s for s in t._subs if s[2] is None or (s[2]() is not owner and _ref_live(s[2]))]
                self._maybe_cancel(t)

    def reprioritize(self, ticket, priority: int) -> None:
        task = ticket.task if isinstance(ticket, Ticket) else ticket
        if task is None or task._state != Task.QUEUED or int(priority) == task._prio:
            return
        with self._lock:
            task._prio = int(priority)
        self._pools[task.pool].rebuild()

    def post(self, fn: Callable, coalesce: Any = None) -> None:
        self._inbox.post(fn, coalesce)

    def progress(self, task_owner: Optional[QObject], cb: Callable, coalesce: Any = None) -> Callable:
        ref = weakref.ref(task_owner) if task_owner is not None else None
        token = coalesce if coalesce is not None else object()
        last = {"t": 0.0}

        def _send(*a, **k):
            now = time.monotonic()


            if now - last["t"] < 0.05:
                def _run_throttled(a=a, k=k):
                    if ref is not None:
                        o = ref()
                        if o is None or not _alive(o):
                            return
                    cb(*a, **k)
                self._inbox.post(_run_throttled, coalesce=token)
                return
            last["t"] = now

            def _run():
                if ref is not None:
                    o = ref()
                    if o is None or not _alive(o):
                        return
                cb(*a, **k)
            self._inbox.post(_run, coalesce=token)
        return _send

    def shutdown(self, wait_ms: int = 3000) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            for t in self._tasks.values():
                t._cancelled.set()
            self._tasks.clear()
            self._by_key.clear()
            self._by_owner.clear()
        for p in self._pools.values():
            p.close()
        self._inbox.close()

    def join(self, timeout_s: float = 3.0) -> None:
        for p in self._pools.values():
            p.join(timeout_s)

    def stats(self) -> dict:
        with self._lock:
            return {"tasks": len(self._tasks), "keys": len(self._by_key), "owners": len(self._by_owner),
                    "io_threads": self._pools[IO].alive_count(), "cpu_threads": self._pools[CPU].alive_count(),
                    "dl_threads": self._pools[DL].alive_count()}


    def _subscribe(self, task: Task, on_done, on_error, owner) -> int:
        ref = None
        if owner is not None:
            ref = weakref.ref(owner)
            oid = id(owner)
            self._by_owner.setdefault(oid, set()).add(task.id)
            task._owner_ids.add(oid)
            if oid not in self._hooked:
                self._hooked.add(oid)


                def _hook(o=owner, _oid=oid):
                    try:
                        o.destroyed.connect(lambda *_a, o=_oid: self._owner_gone(o))
                    except (RuntimeError, AttributeError):
                        pass
                try:
                    if QThread.currentThread() != QCoreApplication.instance().thread():
                        self._inbox.post(_hook)
                    else:
                        _hook()
                except Exception:
                    try:
                        owner.destroyed.connect(lambda *_a, o=oid: self._owner_gone(o))
                    except (RuntimeError, AttributeError):
                        pass
        sid = next(self._sub_seq)
        task._subs.append((on_done, on_error, ref, sid))
        return sid

    def _owner_gone(self, oid: int) -> None:
        with self._lock:
            self._hooked.discard(oid)
            ids = self._by_owner.pop(oid, set())
            for tid in ids:
                t = self._tasks.get(tid)
                if t is None:
                    continue
                t._subs = [s for s in t._subs if s[2] is None or _ref_live(s[2])]
                self._maybe_cancel(t)

    def _maybe_cancel(self, t: Task) -> None:
        if t._subs:
            return
        t._cancelled.set()
        if t._state == Task.QUEUED:
            self._finish(t)

    def _finish(self, t: Task) -> None:
        t._state = Task.DONE
        self._tasks.pop(t.id, None)
        if t.key is not None and self._by_key.get(t.key) is t:
            del self._by_key[t.key]
        for oid in t._owner_ids:
            ids = self._by_owner.get(oid)
            if ids is not None:
                ids.discard(t.id)
                if not ids:
                    del self._by_owner[oid]
        t._owner_ids.clear()

    def _run(self, t: Task) -> None:
        if t._wants_cancel:
            t._kwargs.setdefault("is_cancelled", t._cancelled.is_set)
        result, error = None, None
        try:
            result = t._fn(*t._args, **t._kwargs)
        except Exception as exc:
            error = exc
            if not t._cancelled.is_set():
                if getattr(exc, 'quiet', False):
                    logger.info("task %s failed (transient): %s", getattr(t._fn, "__name__", t._fn), exc)
                else:
                    logger.warning("task %s failed: %s", getattr(t._fn, "__name__", t._fn), exc, exc_info=True)
        except BaseException as exc:
            if not getattr(exc, "task_cancel", False):
                raise
            error = exc
        with self._lock:
            subs = list(t._subs)
            self._finish(t)
        cancelled = t._cancelled.is_set()


        box = [subs, result, error, t._fn, t._args, t._kwargs]
        t._subs = []
        t._fn, t._args, t._kwargs = None, (), {}
        subs = result = error = None

        def _ui() -> None:
            b_subs, b_result, b_error = box[0], box[1], box[2]
            box.clear()
            if not cancelled and b_subs:
                self._deliver(b_subs, b_result, b_error)

        self._inbox.post(_ui)

    def _deliver(self, subs, result, error) -> None:
        for on_done, on_error, ref, _sid in subs:
            if not _ref_live(ref):
                continue
            cb = on_error if error is not None else on_done
            if cb is None:
                continue
            try:
                cb(str(error) if error is not None else result)
            except RuntimeError:
                logger.debug("callback target deleted")
            except Exception:
                logger.warning("task callback raised", exc_info=True)


_engine: Optional[TaskEngine] = None
_engine_lock = threading.Lock()


def engine() -> TaskEngine:
    global _engine
    if _engine is None:
        with _engine_lock:
            if _engine is None:
                _engine = TaskEngine()
    return _engine


def submit(fn: Callable, args: tuple = (), kwargs: Optional[dict] = None,
           on_done: Optional[Callable] = None, on_error: Optional[Callable] = None,
           priority: int = Priority.NORMAL, pool: Any = None,
           owner: Optional[QObject] = None, key: Any = None) -> Optional[Ticket]:
    return engine().submit(fn, args, kwargs, on_done=on_done, on_error=on_error, owner=owner,
                           key=key, priority=priority, pool=pool if pool in (IO, CPU, DL) else IO)


def cancel(ticket: Optional[Ticket]) -> None:
    engine().cancel(ticket)


def cancel_owner(owner: QObject) -> None:
    engine().cancel_owner(owner)


def wrap_callback(callback: Callable, owner: Optional[QObject] = None) -> Callable:
    return engine().progress(owner, callback)


def post(fn: Callable) -> None:
    engine().post(fn)


_coro_local = threading.local()


def submit_coro(coro_fn: Callable, args: tuple = (), kwargs: Optional[dict] = None,
                on_done: Optional[Callable] = None, on_error: Optional[Callable] = None,
                priority: int = Priority.NORMAL, owner: Optional[QObject] = None,
                key: Any = None) -> Optional[Ticket]:
    import asyncio

    def _runner(*a, **kw):
        loop = getattr(_coro_local, "loop", None)
        if loop is None or loop.is_closed():
            loop = asyncio.new_event_loop()
            _coro_local.loop = loop
            asyncio.set_event_loop(loop)
        return loop.run_until_complete(coro_fn(*a, **kw))
    return submit(_runner, args, kwargs, on_done=on_done, on_error=on_error,
                  priority=priority, owner=owner, key=key)


_repack_source_instances: dict[str, object] = {}
_source_lock = threading.Lock()


def _resolve_repack_source(source_key: str):
    with _source_lock:
        source = _repack_source_instances.get(source_key)
        if source is not None:
            return source
        if source_key == "fitgirl":
            from src.core.repacks.sources.fitgirl import FitGirlSource
            source = FitGirlSource()
        elif source_key == "gog":
            from src.core.repacks.sources.gog import GogRevivedSource
            source = GogRevivedSource()
        else:
            return None
        _repack_source_instances[source_key] = source
        return source


def _need(source_key: str):
    source = _resolve_repack_source(source_key)
    if source is None:
        raise RuntimeError(f"Unknown repack source: {source_key}")
    return source


def _with_cancel(is_cancelled, fn, *args, **kwargs):
    from src.core.repacks.base import set_cancel_check, reset_cancel_check
    token = set_cancel_check(is_cancelled)
    try:
        return fn(*args, **kwargs)
    finally:
        reset_cancel_check(token)


def _do_fetch_repack_page(source_key, page, use_cache=True, is_cancelled=None):
    return _with_cancel(is_cancelled, lambda: _need(source_key).fetch_page(page, use_cache=use_cache))


def _do_fetch_repack_details(source_key, entry, use_cache=True, is_cancelled=None):
    return _with_cancel(is_cancelled, lambda: _need(source_key).fetch_details(entry, use_cache=use_cache))


def _do_fetch_upcoming_repacks(source_key, use_cache=True, is_cancelled=None):
    return _with_cancel(is_cancelled, lambda: _need(source_key).fetch_upcoming_repacks(use_cache=use_cache))


def _do_fetch_latest_repacks(source_key, use_cache=True, is_cancelled=None):
    source = _resolve_repack_source(source_key)
    return _with_cancel(is_cancelled, lambda: source.fetch_latest_repacks(use_cache=use_cache)) if source else []


def _do_fetch_popular_repacks(source_key, use_cache=True, is_cancelled=None):
    source = _resolve_repack_source(source_key)
    return _with_cancel(is_cancelled, lambda: source.fetch_popular_repacks(use_cache=use_cache)) if source else []


def _do_search_repacks(source_key, query, page, use_cache=True, is_cancelled=None):
    try:
        return _with_cancel(is_cancelled, lambda: _need(source_key).search(query, page, use_cache=use_cache))
    except NotImplementedError:
        raise RuntimeError("__no_search__") from None


def fetch_page_async(source_key, page, on_done=None, on_error=None, use_cache=True, owner=None):
    return submit(_do_fetch_repack_page, (source_key, page), {"use_cache": use_cache},
                  on_done, on_error, Priority.HIGH, owner=owner, key=("rp", source_key, page, use_cache))


def fetch_details_async(source_key, entry, on_done=None, on_error=None, use_cache=True, owner=None,
                        priority=Priority.HIGH):
    return submit(_do_fetch_repack_details, (source_key, entry), {"use_cache": use_cache},
                  on_done, on_error, priority, owner=owner,
                  key=("rd", source_key, getattr(entry, "url", id(entry)), use_cache))


def fetch_upcoming_repacks_async(source_key, on_done=None, on_error=None, use_cache=True, owner=None):
    return submit(_do_fetch_upcoming_repacks, (source_key,), {"use_cache": use_cache},
                  on_done, on_error, Priority.LOW, owner=owner, key=("ru", source_key, use_cache))


def fetch_latest_repacks_async(source_key, on_done=None, use_cache=True, owner=None):
    done = on_done or (lambda _r: None)
    return submit(_do_fetch_latest_repacks, (source_key,), {"use_cache": use_cache},
                  done, lambda _e: done([]), Priority.LOW, owner=owner, key=("rl", source_key, use_cache))


def fetch_popular_repacks_async(source_key, on_done=None, use_cache=True, owner=None):
    done = on_done or (lambda _r: None)
    return submit(_do_fetch_popular_repacks, (source_key,), {"use_cache": use_cache},
                  done, lambda _e: done([]), Priority.LOW, owner=owner, key=("rpop", source_key, use_cache))


def fetch_search_async(source_key, query, page, on_done=None, on_error=None, use_cache=True, owner=None):
    return submit(_do_search_repacks, (source_key, query, page), {"use_cache": use_cache},
                  on_done, on_error, Priority.HIGH, owner=owner, key=("rs", source_key, query, page, use_cache))


class _ShutdownSignal(QObject):
    done = Signal()


def _blocking_teardown(wait_s: float) -> None:
    engine().join(wait_s)
    try:
        from src.core.artwork import images
        images.shutdown()
    except Exception:
        logger.warning("image pipeline shutdown failed", exc_info=True)
    try:
        from src.core.cache import cache
        cache.flush_all()
    except Exception:
        logger.warning("cache flush failed", exc_info=True)
    for key in list(_repack_source_instances):
        try:
            mod = "gog" if key == "gog" else "fitgirl"
            exec_mod = __import__(f"src.core.repacks.sources.{mod}", fromlist=["shutdown_executor"])
            exec_mod.shutdown_executor(wait=True)
        except Exception:
            logger.warning("executor shutdown failed for %s", key, exc_info=True)


def shutdown_all_async(on_complete: Callable[[], None], wait_ms: int = 8000, hard_timeout_ms: int = 9000) -> None:
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance()
    engine().shutdown()
    sig = _ShutdownSignal(app)
    state = {"fired": False}

    def _finish() -> None:
        if not state["fired"]:
            state["fired"] = True
            on_complete()

    timer = QTimer(app)
    timer.setSingleShot(True)
    timer.timeout.connect(_finish)
    timer.start(hard_timeout_ms)
    sig.done.connect(lambda: (timer.stop(), _finish()))

    def _run():
        try:
            _blocking_teardown(wait_ms / 1000.0)
        finally:
            try:
                sig.done.emit()
            except RuntimeError:
                pass
    threading.Thread(target=_run, daemon=True, name="app-shutdown").start()
