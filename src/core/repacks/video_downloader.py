from __future__ import annotations
import hashlib
import logging
import os
import time
from collections import deque
import httpx
from PySide6.QtCore import QObject, Signal
from src.core.config import paths
from src.core.net import LazyHttpClient, shared_ssl_context
from src.core.worker import IO, Priority, cancel as _cancel, engine, submit as _submit
logger = logging.getLogger(__name__)
_VIDEO_DIR = os.path.join(paths.cache_dir, 'repacks', 'videos')
_CHUNK_SIZE = 1 << 18
_MAX_CONCURRENT_DOWNLOADS = 4
_MAX_VIDEO_BYTES = 128 * 1024 * 1024
_DISK_BUDGET = 768 * 1024 * 1024
_HEADERS = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36', 'Accept-Encoding': 'gzip, deflate, br'}
_session = LazyHttpClient(headers=_HEADERS, trust_env=False, follow_redirects=True, limits=httpx.Limits(max_connections=_MAX_CONCURRENT_DOWNLOADS, max_keepalive_connections=_MAX_CONCURRENT_DOWNLOADS), transport_factory=lambda: httpx.HTTPTransport(retries=0, verify=shared_ssl_context()))

_video_dir_ready = False

def video_cache_path(url: str) -> str:
    global _video_dir_ready
    if not _video_dir_ready:
        os.makedirs(_VIDEO_DIR, exist_ok=True)
        _video_dir_ready = True
    digest = hashlib.blake2b(url.encode('utf-8'), digest_size=16).hexdigest()
    ext = os.path.splitext(url.split('?', 1)[0])[1]
    if not ext or len(ext) > 5:
        ext = '.webm'
    return os.path.join(_VIDEO_DIR, digest + ext)

def _touch(path: str) -> None:
    try:
        if time.time() - os.stat(path).st_mtime > 86400:
            os.utime(path)
    except OSError:
        pass


def _trim_videos() -> None:
    files, total = [], 0
    try:
        entries = list(os.scandir(_VIDEO_DIR))
    except OSError:
        return
    for entry in entries:
        try:
            st = entry.stat()
        except OSError:
            continue
        if entry.name.endswith('.part') and time.time() - st.st_mtime > 3600:
            try:
                os.remove(entry.path)
            except OSError:
                pass
            continue
        files.append((st.st_mtime, st.st_size, entry.path))
        total += st.st_size
    if total <= _DISK_BUDGET:
        return
    for _mtime, size, path in sorted(files):
        try:
            os.remove(path)
        except OSError:
            continue
        total -= size
        if total <= _DISK_BUDGET * 0.8:
            break

def _do_download_video(url: str, is_cancelled=None) -> str:
    dest_path = video_cache_path(url)
    if os.path.isfile(dest_path):
        return dest_path
    tmp_path = dest_path + '.part'
    os.makedirs(os.path.dirname(dest_path), exist_ok=True)
    try:
        with _session.stream('GET', url, timeout=httpx.Timeout(15.0, connect=5.0)) as resp:
            resp.raise_for_status()
            declared = resp.headers.get('Content-Length', '')
            if declared.isdigit() and int(declared) > _MAX_VIDEO_BYTES:
                raise ValueError('video exceeds size limit')
            written = 0
            with open(tmp_path, 'wb', buffering=0) as fh:
                for chunk in resp.iter_bytes(chunk_size=_CHUNK_SIZE):
                    if not chunk:
                        continue
                    written += len(chunk)
                    if written > _MAX_VIDEO_BYTES:
                        raise ValueError('video exceeds size limit')
                    fh.write(chunk)
                    if is_cancelled is not None and is_cancelled():
                        raise RuntimeError('download cancelled')
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

    def __init__(self, parent=None, max_concurrent: int=_MAX_CONCURRENT_DOWNLOADS):
        super().__init__(parent)
        self._max_concurrent = max_concurrent
        self._active: dict[str, str] = {}
        self._queue: deque[str] = deque()
        self._queued: set[str] = set()
        self._shutting_down = False
        self._trimmed = False

    def shutdown(self, wait_ms: int=2000) -> None:
        self._shutting_down = True
        for task_id in self._active.values():
            _cancel(task_id)
        self._active.clear()
        self._queue.clear()
        self._queued.clear()

    def _emit_if_cached(self, url: str) -> bool:
        path = video_cache_path(url)
        if os.path.isfile(path):
            _touch(path)
            self.video_ready.emit(url, path)
            return True
        return False

    def request(self, url: str) -> None:
        if not url or self._shutting_down:
            return
        if self._emit_if_cached(url):
            return
        if url in self._active or url in self._queued:
            return
        if not self._trimmed:
            self._trimmed = True
            engine().submit(_trim_videos, priority=Priority.LOW, pool=IO)
        if len(self._active) < self._max_concurrent:
            self._start(url)
        else:
            self._queue.append(url)
            self._queued.add(url)

    def cancel(self, url: str) -> None:
        self._queued.discard(url)
        try:
            self._queue.remove(url)
        except ValueError:
            pass
        task_id = self._active.pop(url, None)
        if task_id is not None:
            try:
                _cancel(task_id)
            except Exception:
                pass

    def _start(self, url: str) -> None:
        task_id = _submit(_do_download_video, args=(url,), on_done=lambda path, u=url: self._on_finished(u, path), on_error=lambda err, u=url: self._on_failed(u, err), priority=Priority.NORMAL)
        self._active[url] = task_id

    def _advance_queue(self) -> None:
        while self._queue and len(self._active) < self._max_concurrent and (not self._shutting_down):
            next_url = self._queue.popleft()
            self._queued.discard(next_url)
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
            logger.warning('Failed to download video %s: %s', url, error)
            self.video_failed.emit(url, error)
        self._advance_queue()
