from __future__ import annotations
import copy
import gc
import json
import logging
import os
import re
import shutil
import threading
import time
import uuid
from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import Any, Optional
from PySide6.QtCore import QObject, QTimer, Signal
from src.core.config import libtorrent_defaults, network, paths, settings
logger = logging.getLogger(__name__)
try:
    import libtorrent as lt
except ImportError as _exc:
    lt = None
    _LT_IMPORT_ERROR = _exc
else:
    _LT_IMPORT_ERROR = None

def _require_libtorrent() -> None:
    if lt is None:
        raise RuntimeError("python-libtorrent is not installed. Install it with 'pip install libtorrent==2.0.13' (or your OS package, e.g. 'apt install python3-libtorrent').") from _LT_IMPORT_ERROR
_CONFIG_DIR = paths.config_dir
_QUEUE_FILE = os.path.join(_CONFIG_DIR, 'pirachest_downloads.json')
_METADATA_TIMEOUT_SECS = 120
_MAX_ACTIVE_TORRENTS = 3
_DEFAULT_MAX_PEERS = 400
_DEFAULT_CONNECTIONS_LIMIT = 200
_DEFAULT_CONNECTION_SPEED = 25

class DLState(str, Enum):
    queued = 'Queued'
    downloading = 'Downloading'
    verifying = 'Verifying'
    paused = 'Paused'
    seeding = 'Seeding'
    completed = 'Completed'
    error = 'Error'
    cancelled = 'Cancelled'

def _download_dir_for_category(category: str) -> str:
    if category == 'repacks':
        return getattr(settings, 'download_dir_repacks', settings.download_dir)
    if category == 'music':
        return getattr(settings, 'download_dir_music', os.path.join(settings.download_dir, 'music'))
    if category == 'anime':
        return getattr(settings, 'download_dir_anime', os.path.join(settings.download_dir, 'anime'))
    if category == 'youtube':
        return getattr(settings, 'download_dir_youtube', os.path.join(settings.download_dir, 'youtube'))
    return getattr(settings, 'download_dir_minerva', settings.download_dir)

def _human_bytes(n: float) -> str:
    for unit in ('B', 'KiB', 'MiB', 'GiB', 'TiB'):
        if abs(n) < 1024.0:
            return f'{n:3.2f} {unit}'
        n /= 1024.0
    return f'{n:.2f} PiB'

def _human_eta(seconds: float) -> str:
    if seconds is None or seconds < 0 or seconds > 10 ** 8:
        return '-'
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    if h:
        return f'{h}h {m:02d}m'
    if m:
        return f'{m}m {s:02d}s'
    return f'{s}s'

def _human_duration(seconds: float) -> str:
    if not seconds or seconds < 0:
        return '-'
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    if h:
        return f'{h}h {m:02d}m'
    return f'{m}m {s:02d}s'

@dataclass
class DownloadItem:
    id: str
    torrent_file: str
    file_id: int
    game_name: str
    console: str
    source: str = 'Minerva'
    backend: str = 'torrent'
    category: str = 'minerva'
    file_ids: list[int] = field(default_factory=list)
    seed_after: bool = True
    max_down_kbps: int = 0
    max_up_kbps: int = 0
    max_peers: int = _DEFAULT_MAX_PEERS
    ratio_limit: float = 0.0
    seed_time_limit_min: int = 0
    state: DLState = DLState.queued
    error: str = ''
    download_path: str = ''
    retries: int = 0
    progress: float = 0.0
    downloaded_bytes: int = 0
    total_bytes: int = 0
    speed_down: str = '0 B/s'
    speed_up: str = '0 B/s'
    speed_down_kbps: float = 0.0
    speed_up_kbps: float = 0.0
    eta: str = '-'
    peers: int = 0
    uploaded_bytes: int = 0
    ratio: float = 0.0
    seed_time: str = '-'

    def to_persist_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d['state'] = self.state.value
        return d

    @classmethod
    def from_persist_dict(cls, d: dict[str, Any]) -> 'DownloadItem':
        d = dict(d)
        state_val = d.pop('state', DLState.queued.value)
        try:
            state = DLState(state_val)
        except ValueError:
            state = DLState.queued
        valid = {f for f in cls.__dataclass_fields__}
        d = {k: v for k, v in d.items() if k in valid}
        item = cls(**d)
        item.state = state
        return item

    def display_size(self) -> str:
        if not self.total_bytes:
            return f'{_human_bytes(self.downloaded_bytes)} / ?'
        return f'{_human_bytes(self.downloaded_bytes)} / {_human_bytes(self.total_bytes)}'

class DownloadManager(QObject):

    @staticmethod
    def _create_session(settings_pack: dict):
        try:
            return lt.session(settings_pack)
        except Exception as exc:
            logger.warning('lt.session() rejected the full settings pack (%s); retrying with a minimal safe pack', exc)
        safe_pack = {'user_agent': settings_pack.get('user_agent', 'PiraChest/2.0'), 'listen_interfaces': '0.0.0.0:6881', 'enable_dht': settings_pack.get('enable_dht', True), 'enable_lsd': settings_pack.get('enable_lsd', True), 'enable_upnp': settings_pack.get('enable_upnp', True), 'enable_natpmp': settings_pack.get('enable_natpmp', True), 'download_rate_limit': settings_pack.get('download_rate_limit', 0), 'upload_rate_limit': settings_pack.get('upload_rate_limit', 0)}
        try:
            return lt.session(safe_pack)
        except Exception as exc:
            logger.warning('lt.session() rejected the safe pack (%s); retrying with IPv4-only listen interface', exc)
        try:
            return lt.session({'listen_interfaces': '0.0.0.0:6881', 'enable_dht': True})
        except Exception:
            logger.exception('lt.session() rejected even the minimal safe pack; falling back to defaults')
            return lt.session()
    item_added = Signal(str)
    item_updated = Signal(str)
    item_removed = Signal(str)
    stats_changed = Signal()
    order_changed = Signal()

    def __init__(self, parent: Optional[QObject]=None) -> None:
        super().__init__(parent)
        _require_libtorrent()
        self.global_down_kbps = int(getattr(settings, 'speed_limit', 0))
        self.global_up_kbps = int(getattr(settings, 'upload_speed_limit', 500))
        settings_pack = {'user_agent': 'PiraChest/2.0', 'listen_interfaces': '0.0.0.0:6881,[::]:6881', 'enable_dht': libtorrent_defaults.enable_dht, 'enable_lsd': True, 'enable_upnp': True, 'enable_natpmp': True, 'download_rate_limit': self.global_down_kbps * 1024 if self.global_down_kbps else 0, 'upload_rate_limit': self.global_up_kbps * 1024, 'active_downloads': _MAX_ACTIVE_TORRENTS, 'active_seeds': _MAX_ACTIVE_TORRENTS, 'active_limit': _MAX_ACTIVE_TORRENTS * 2, 'active_dht_limit': 88, 'active_tracker_limit': 1600, 'active_lsd_limit': 60, 'connections_limit': max(_DEFAULT_CONNECTIONS_LIMIT, libtorrent_defaults.max_connections_per_torrent * _MAX_ACTIVE_TORRENTS), 'unchoke_slots_limit': -1, 'connection_speed': 200, 'rate_limit_utp': True, 'mixed_mode_algorithm': int(lt.bandwidth_mixed_algo_t.prefer_tcp), 'aio_threads': 4, 'send_buffer_watermark': 1 * 1024 * 1024, 'send_buffer_low_watermark': 256 * 1024, 'send_buffer_watermark_factor': 100, 'max_out_request_queue': 500, 'max_allowed_in_request_queue': 500, 'request_queue_time': 2, 'checking_mem_usage': 1024, 'use_parole_mode': True, 'smooth_connects': True, 'whole_pieces_threshold': 20, 'request_timeout': 20, 'peer_connect_timeout': 8, 'auto_manage_interval': 30, 'seed_time_limit': 0, 'inactivity_timeout': 300, 'stop_tracker_timeout': 5, 'tracker_completion_timeout': 20, 'tracker_receive_timeout': 10, 'peer_timeout': 60, 'urlseed_timeout': 15, 'piece_timeout': 10, 'suggest_mode': int(lt.suggest_mode_t.suggest_read_cache), 'disk_io_write_mode': int(lt.io_buffer_mode_t.enable_os_cache), 'disk_io_read_mode': int(lt.io_buffer_mode_t.enable_os_cache)}
        self._session = self._create_session(settings_pack)
        if libtorrent_defaults.enable_dht:
            for host, port in (('router.bittorrent.com', 6881), ('dht.transmissionbt.com', 6881), ('router.utorrent.com', 6881)):
                try:
                    self._session.add_dht_router(host, port)
                except Exception:
                    pass
        self._items: dict[str, DownloadItem] = {}
        self._order: list[str] = []
        self._handles: dict[str, 'lt.torrent_handle'] = {}
        self._file_index: dict[str, list[int]] = {}
        self._selected_size: dict[str, int] = {}
        self._start_time: dict[str, float] = {}
        self._last_bytes: dict[str, tuple[float, int, int]] = {}
        self._resolving: set[str] = set()
        self._finalizing: set[str] = set()
        self._external_cancel_callbacks: dict[str, Any] = {}
        self._external_last_emit: dict[str, float] = {}
        self._torrent_cache = paths.torrent_cache
        os.makedirs(self._torrent_cache, exist_ok=True)
        os.makedirs(_CONFIG_DIR, exist_ok=True)
        self._lock = threading.RLock()
        self._timer = QTimer(self)
        self._timer.setInterval(1500)
        self._timer.timeout.connect(self._poll)
        self._save_counter = 0
        self._dirty = False
        self._load_state()
        self._timer.start()
        self._try_start_next()

    def _create_queued_item(self, torrent_file: str, file_id: int, game_name: str, console: str, source: str='Minerva', file_ids: Optional[list[int]]=None, category: Optional[str]=None) -> DownloadItem:
        item_id = uuid.uuid4().hex
        if category is None:
            category = 'minerva' if (source or 'Minerva') == 'Minerva' else 'repacks'
        item = DownloadItem(id=item_id, torrent_file=torrent_file, file_id=int(file_id or 1), file_ids=sorted({int(f) for f in file_ids}) if file_ids else [], game_name=game_name or 'Unknown', console=console or '', source=source or 'Minerva', category=category, backend='torrent', state=DLState.queued)
        with self._lock:
            self._items[item_id] = item
            self._order.append(item_id)
        self.item_added.emit(item_id)
        return item

    def add(self, torrent_file: str, file_id: int, game_name: str, console: str, source: str='Minerva', file_ids: Optional[list[int]]=None, category: Optional[str]=None) -> str:
        item = self._create_queued_item(torrent_file, file_id, game_name, console, source, file_ids, category)
        self._save_state()
        self._try_start_next()
        return item.id

    def add_external(self, *, game_name: str, console: str='', source: str='', category: str='music', item_id: Optional[str]=None, total_bytes: int=0) -> str:
        item_id = item_id or uuid.uuid4().hex
        item = DownloadItem(id=item_id, torrent_file='', file_id=0, game_name=game_name or 'Unknown', console=console or '', source=source or category.title(), category=category, backend='external', state=DLState.downloading, total_bytes=total_bytes)
        with self._lock:
            self._items[item_id] = item
            self._order.append(item_id)
        self.item_added.emit(item_id)
        self._save_state()
        self._sync_timer_state()
        return item_id

    def register_external_cancel(self, item_id: str, callback) -> None:
        self._external_cancel_callbacks[item_id] = callback

    def update_external(self, item_id: str, *, downloaded_bytes: Optional[int]=None, total_bytes: Optional[int]=None, progress: Optional[float]=None, speed_down_kbps: Optional[float]=None) -> None:
        with self._lock:
            item = self._items.get(item_id)
            if item is None or item.backend != 'external':
                return
            if downloaded_bytes is not None:
                item.downloaded_bytes = downloaded_bytes
            if total_bytes is not None:
                item.total_bytes = total_bytes
            if progress is not None:
                item.progress = max(0.0, min(100.0, progress))
            elif item.total_bytes:
                item.progress = round(item.downloaded_bytes / item.total_bytes * 100, 2)
            if speed_down_kbps is not None:
                item.speed_down_kbps = speed_down_kbps
                item.speed_down = f'{_human_bytes(speed_down_kbps * 1024)}/s'
        now = time.monotonic()
        last = self._external_last_emit.get(item_id, 0.0)
        if now - last < 0.5:
            return
        self._external_last_emit[item_id] = now
        self.item_updated.emit(item_id)

    def complete_external(self, item_id: str, download_path: str='') -> None:
        with self._lock:
            item = self._items.get(item_id)
            if item is None or item.backend != 'external':
                return
            item.state = DLState.completed
            item.progress = 100.0
            if download_path:
                item.download_path = download_path
            item.speed_down = '0 B/s'
            item.speed_down_kbps = 0.0
        self._external_cancel_callbacks.pop(item_id, None)
        self._external_last_emit.pop(item_id, None)
        self.item_updated.emit(item_id)
        self._save_state()

    def fail_external(self, item_id: str, error: str) -> None:
        with self._lock:
            item = self._items.get(item_id)
            if item is None or item.backend != 'external':
                return
            item.state = DLState.error
            item.error = error
            item.speed_down = '0 B/s'
            item.speed_down_kbps = 0.0
        self._external_cancel_callbacks.pop(item_id, None)
        self._external_last_emit.pop(item_id, None)
        self.item_updated.emit(item_id)
        self._save_state()

    def add_many(self, roms_and_meta: list[dict[str, Any]]) -> list[str]:
        if not roms_and_meta:
            return []
        ids = [self._create_queued_item(torrent_file=m.get('torrent_file', ''), file_id=m.get('file_id', 1), game_name=m.get('game_name', ''), console=m.get('console', ''), source=m.get('source', 'Minerva')).id for m in roms_and_meta]
        self._save_state()
        self._try_start_next()
        return ids

    def items_in_order(self) -> list[DownloadItem]:
        with self._lock:
            return [copy.copy(self._items[i]) for i in self._order if i in self._items]

    def get(self, item_id: str) -> Optional[DownloadItem]:
        with self._lock:
            it = self._items.get(item_id)
            return copy.copy(it) if it else None

    def reorder(self, new_order_ids: list[str]) -> None:
        with self._lock:
            existing = set(self._order)
            cleaned = [i for i in new_order_ids if i in existing]
            for i in self._order:
                if i not in cleaned:
                    cleaned.append(i)
            self._order = cleaned
        self.order_changed.emit()
        self._save_state()
        self._try_start_next()

    def pause(self, item_id: str) -> None:
        with self._lock:
            item = self._items.get(item_id)
            if not item:
                return
            if item.backend == 'external':
                return
            handle = self._handles.get(item_id)
            if handle is not None and item.state in (DLState.downloading, DLState.verifying, DLState.seeding):
                handle.pause()
            item.state = DLState.paused
        self.item_updated.emit(item_id)
        self.stats_changed.emit()
        self._save_state()

    def resume(self, item_id: str) -> None:
        with self._lock:
            item = self._items.get(item_id)
            if not item:
                return
            if item.backend == 'external':
                return
            handle = self._handles.get(item_id)
            if handle is not None:
                handle.resume()
                item.state = DLState.downloading
            else:
                item.state = DLState.queued
        self.item_updated.emit(item_id)
        self.stats_changed.emit()
        self._save_state()
        self._try_start_next()

    def cancel(self, item_id: str, delete_files: bool=False) -> None:
        with self._lock:
            item = self._items.get(item_id)
            is_external = item is not None and item.backend == 'external'
        if is_external:
            callback = self._external_cancel_callbacks.pop(item_id, None)
            if callback is not None:
                try:
                    callback()
                except Exception:
                    logger.exception('External cancel callback failed for %s', item_id)
        else:
            self._teardown_handle(item_id, remove_files=delete_files)
        with self._lock:
            item = self._items.get(item_id)
            if item:
                item.state = DLState.cancelled
                item.speed_down = '0 B/s'
                item.speed_up = '0 B/s'
                item.speed_down_kbps = 0.0
                item.speed_up_kbps = 0.0
        if item:
            self.item_updated.emit(item_id)
        self.stats_changed.emit()
        self._save_state()
        self._try_start_next()

    def remove(self, item_id: str, delete_files: bool=False) -> None:
        self._teardown_handle(item_id, remove_files=delete_files)
        self._external_cancel_callbacks.pop(item_id, None)
        with self._lock:
            self._items.pop(item_id, None)
            if item_id in self._order:
                self._order.remove(item_id)
        self.item_removed.emit(item_id)
        self.stats_changed.emit()
        self._save_state()
        self._try_start_next()

    def retry(self, item_id: str) -> None:
        with self._lock:
            item = self._items.get(item_id)
            is_external = item is not None and item.backend == 'external'
        if is_external:
            with self._lock:
                item = self._items.get(item_id)
                if item:
                    item.state = DLState.queued
                    item.error = ''
            self.item_updated.emit(item_id)
            self._save_state()
            return
        self._teardown_handle(item_id, remove_files=False)
        with self._lock:
            item = self._items.get(item_id)
            if not item:
                return
            item.state = DLState.queued
            item.error = ''
            item.retries += 1
            item.progress = 0.0
            item.downloaded_bytes = 0
            item.total_bytes = 0
            item.speed_down = '0 B/s'
            item.speed_up = '0 B/s'
            item.speed_down_kbps = 0.0
            item.speed_up_kbps = 0.0
            item.eta = '-'
            item.peers = 0
        self.item_updated.emit(item_id)
        self._save_state()
        self._try_start_next()

    def force_recheck(self, item_id: str) -> None:
        with self._lock:
            handle = self._handles.get(item_id)
            item = self._items.get(item_id)
            if handle is None or item is None:
                return
            item.state = DLState.verifying
        handle.force_recheck()
        self.item_updated.emit(item_id)

    def open_folder(self, item_id: str) -> Optional[str]:
        item = self._items.get(item_id)
        if not item:
            return None
        target = item.download_path or os.path.join(_download_dir_for_category(item.category), item.console or '', item.game_name or '')
        if not target:
            return None
        folder = target if os.path.isdir(target) else os.path.dirname(target)
        return folder if folder and os.path.isdir(folder) else None

    def open_file(self, item_id: str) -> Optional[str]:
        item = self._items.get(item_id)
        if not item or not item.download_path:
            return None
        return item.download_path if os.path.isfile(item.download_path) else None

    def find_repack_installer(self, item_id: str) -> Optional[str]:
        item = self._items.get(item_id)
        if not item or item.category != 'repacks':
            return None
        folder = self._repack_folder(item)
        if not folder or not os.path.isdir(folder):
            return None
        for dirpath, _dirnames, filenames in os.walk(folder):
            for fname in filenames:
                if fname.lower() == 'setup.exe':
                    return os.path.join(dirpath, fname)
        return None

    def _repack_folder(self, item: DownloadItem) -> Optional[str]:
        if item.download_path and os.path.isdir(item.download_path):
            return item.download_path
        if item.download_path and os.path.isfile(item.download_path):
            return os.path.dirname(item.download_path)
        base_dir = os.path.join(_download_dir_for_category(item.category), item.console or '')
        candidate = os.path.join(base_dir, item.game_name or '')
        if os.path.isdir(candidate):
            return candidate
        if item.game_name and os.path.isdir(base_dir):
            target = item.game_name.strip().lower()
            best_match = None
            for name in os.listdir(base_dir):
                full = os.path.join(base_dir, name)
                if not os.path.isdir(full):
                    continue
                name_lower = name.strip().lower()
                if name_lower == target:
                    return full
                if name_lower.startswith(target) and best_match is None:
                    best_match = full
            if best_match:
                return best_match
        return None

    def set_torrent_settings(self, item_id: str, *, seed_after: Optional[bool]=None, max_down_kbps: Optional[int]=None, max_up_kbps: Optional[int]=None, max_peers: Optional[int]=None, ratio_limit: Optional[float]=None, seed_time_limit_min: Optional[int]=None) -> None:
        with self._lock:
            item = self._items.get(item_id)
            if not item:
                return
            if seed_after is not None:
                item.seed_after = seed_after
            if max_down_kbps is not None:
                item.max_down_kbps = max_down_kbps
            if max_up_kbps is not None:
                item.max_up_kbps = max_up_kbps
            if max_peers is not None:
                item.max_peers = max_peers
            if ratio_limit is not None:
                item.ratio_limit = ratio_limit
            if seed_time_limit_min is not None:
                item.seed_time_limit_min = seed_time_limit_min
            handle = self._handles.get(item_id)
        if handle is not None:
            self._apply_handle_limits(handle, item)
        self.item_updated.emit(item_id)
        self._save_state()

    def set_global_limits(self, down_kbps: int, up_kbps: int) -> None:
        self.global_down_kbps = int(down_kbps)
        self.global_up_kbps = int(up_kbps)
        self._session.apply_settings({'download_rate_limit': self.global_down_kbps * 1024 if self.global_down_kbps else 0, 'upload_rate_limit': self.global_up_kbps * 1024})
        self._try_start_next()

    def summary(self) -> dict[str, Any]:
        with self._lock:
            items = list(self._items.values())
        active = [i for i in items if i.state in (DLState.downloading, DLState.verifying)]
        queued = [i for i in items if i.state == DLState.queued]
        completed = [i for i in items if i.state == DLState.completed]
        total_down = sum((i.speed_down_kbps for i in items if i.state in (DLState.downloading, DLState.verifying)))
        total_up = sum((i.speed_up_kbps for i in items if i.state in (DLState.downloading, DLState.seeding, DLState.verifying)))
        return {'active': len(active), 'queued': len(queued), 'completed': len(completed), 'total_down': _human_bytes(total_down * 1024) + '/s', 'total_up': _human_bytes(total_up * 1024) + '/s'}

    def shutdown(self) -> None:
        self._timer.stop()
        self._save_state()
        with self._lock:
            handle_ids = list(self._handles.keys())
        for item_id in handle_ids:
            handle = self._handles.get(item_id)
            if handle is None:
                continue
            try:
                self._session.remove_torrent(handle)
            except Exception:
                pass
        self._handles.clear()

    def _active_torrent_count(self) -> int:
        return sum((1 for i in self._handles if self._items.get(i) is not None and self._items[i].state in (DLState.downloading, DLState.verifying)))

    def _has_pending_work(self) -> bool:
        if self._handles or self._resolving:
            return True
        for item_id in self._order:
            item = self._items.get(item_id)
            if item is not None and item.state in (DLState.queued, DLState.downloading, DLState.verifying, DLState.seeding):
                return True
        return False

    def _sync_timer_state(self) -> None:
        if self._has_pending_work():
            if not self._timer.isActive():
                self._timer.start()
        else:
            if self._timer.isActive():
                self._timer.stop()
            QTimer.singleShot(2000, self._deferred_gc)

    def _deferred_gc(self) -> None:
        if not self._has_pending_work():
            gc.collect()

    def _try_start_next(self) -> None:
        with self._lock:
            order = list(self._order)
        free_slots = _MAX_ACTIVE_TORRENTS - self._active_torrent_count()
        if free_slots > 0:
            for item_id in order:
                if free_slots <= 0:
                    break
                item = self._items.get(item_id)
                if item is None or item.state != DLState.queued or item_id in self._resolving or (item.backend != 'torrent'):
                    continue
                self._resolving.add(item_id)
                threading.Thread(target=self._start_item, args=(item_id,), daemon=True).start()
                free_slots -= 1
        self._sync_timer_state()

    def _start_item(self, item_id: str) -> None:
        item = self._items.get(item_id)
        if item is None:
            self._resolving.discard(item_id)
            return
        try:
            source = self._resolve_torrent(item.torrent_file)
            handle = self._add_torrent(source, _download_dir_for_category(item.category))
            with self._lock:
                if self._items.get(item_id) is None or self._items[item_id].state == DLState.cancelled:
                    try:
                        self._session.remove_torrent(handle)
                    except Exception:
                        pass
                    self._resolving.discard(item_id)
                    return
                self._handles[item_id] = handle
                self._start_time[item_id] = time.time()
            metadata_wait_start = time.time()
            while True:
                with self._lock:
                    if self._handles.get(item_id) is not handle:
                        self._resolving.discard(item_id)
                        return
                    cur_item = self._items.get(item_id)
                    if cur_item is None or cur_item.state == DLState.cancelled:
                        try:
                            self._session.remove_torrent(handle)
                        except Exception:
                            pass
                        self._handles.pop(item_id, None)
                        self._resolving.discard(item_id)
                        return
                    try:
                        has_metadata = handle.status().has_metadata
                    except Exception:
                        self._handles.pop(item_id, None)
                        self._resolving.discard(item_id)
                        return
                if has_metadata:
                    break
                if time.time() - metadata_wait_start > _METADATA_TIMEOUT_SECS:
                    with self._lock:
                        try:
                            self._session.remove_torrent(handle)
                        except Exception:
                            pass
                        self._handles.pop(item_id, None)
                    raise RuntimeError(f'Timed out waiting for torrent metadata after {_METADATA_TIMEOUT_SECS}s (no peers/DHT reachable?)')
                time.sleep(0.2)
            with self._lock:
                if self._handles.get(item_id) is not handle:
                    self._resolving.discard(item_id)
                    return
                torrent_info = handle.torrent_file()
                num_files = torrent_info.num_files()
                if item.file_ids:
                    zero_based_list = sorted({fid - 1 for fid in item.file_ids})
                else:
                    zero_based_list = [item.file_id - 1]
                for zb in zero_based_list:
                    if zb < 0 or zb >= num_files:
                        raise RuntimeError(f'file index {zb + 1} out of range ({num_files} files)')
                priorities = [0] * num_files
                for zb in zero_based_list:
                    priorities[zb] = 7
                handle.set_sequential_download(True)
                handle.prioritize_files(priorities)
                actual = handle.file_priorities()
                if not all((zb < len(actual) and actual[zb] > 0 for zb in zero_based_list)):
                    logger.warning('prioritize_files() did not take immediately for item=%s; will re-check on next poll', item_id)
                try:
                    handle.resume()
                except Exception:
                    logger.exception('resume() failed for %s', item_id)
                self._file_index[item_id] = zero_based_list
                files_obj = torrent_info.files()
                self._selected_size[item_id] = sum((files_obj.file_size(zb) for zb in zero_based_list))
                item.total_bytes = self._selected_size[item_id]
                item.state = DLState.downloading
                self._apply_handle_limits(handle, item)
            self.item_updated.emit(item_id)
        except Exception as exc:
            logger.exception('Failed to start download %s', item_id)
            if item is not None:
                item.state = DLState.error
                item.error = str(exc)
                self.item_updated.emit(item_id)
        finally:
            self._resolving.discard(item_id)
            self._save_state()

    def _apply_handle_limits(self, handle, item: DownloadItem) -> None:
        try:
            handle.set_download_limit(item.max_down_kbps * 1024 if item.max_down_kbps else 0)
            handle.set_upload_limit(item.max_up_kbps * 1024 if item.max_up_kbps else 0)
            handle.set_max_connections(item.max_peers or libtorrent_defaults.max_connections_per_torrent or _DEFAULT_MAX_PEERS)
            handle.set_max_uploads(libtorrent_defaults.max_uploads_per_torrent)
        except Exception:
            pass

    def _teardown_handle(self, item_id: str, remove_files: bool) -> None:
        with self._lock:
            handle = self._handles.pop(item_id, None)
            self._file_index.pop(item_id, None)
            self._selected_size.pop(item_id, None)
            self._start_time.pop(item_id, None)
            self._last_bytes.pop(item_id, None)
            if handle is not None:
                try:
                    if remove_files and lt is not None:
                        self._session.remove_torrent(handle, lt.options_t.delete_files)
                    else:
                        self._session.remove_torrent(handle)
                except Exception:
                    pass
        if remove_files:
            self._delete_item_files(item_id)

    def _find_file_by_stem(self, base_dir: str, stem: str) -> Optional[str]:
        if not stem or not os.path.isdir(base_dir):
            return None
        target = stem.strip().lower()
        for name in os.listdir(base_dir):
            full = os.path.join(base_dir, name)
            if not os.path.isfile(full):
                continue
            name_stem = os.path.splitext(name)[0].strip().lower()
            if name_stem == target:
                return full
        return None

    def _delete_item_files(self, item_id: str) -> None:
        item = self._items.get(item_id)
        if item is None:
            return
        candidates = []
        base_dir = os.path.join(_download_dir_for_category(item.category), item.console or '')
        if item.category == 'repacks':
            folder = self._repack_folder(item)
            if folder:
                candidates.append(folder)
        if item.download_path:
            candidates.append(item.download_path)
            containing_dir = os.path.dirname(item.download_path)
            if containing_dir and os.path.abspath(containing_dir) != os.path.abspath(base_dir):
                candidates.append(containing_dir)
        if item.game_name:
            candidates.append(os.path.join(base_dir, item.game_name))
            by_stem = self._find_file_by_stem(base_dir, item.game_name)
            if by_stem:
                candidates.append(by_stem)
        deleted_any = False
        for path in candidates:
            if not path or not os.path.exists(path):
                continue
            try:
                if os.path.isdir(path):
                    shutil.rmtree(path, ignore_errors=True)
                else:
                    os.remove(path)
                deleted_any = True
            except Exception:
                logger.exception('Failed to delete files for %s at %s', item_id, path)
        if not deleted_any:
            logger.warning('No files found to delete for %s (checked: %s)', item_id, candidates)

    def _poll(self) -> None:
        for item_id in list(self._handles.keys()):
            item = self._items.get(item_id)
            handle = self._handles.get(item_id)
            if item is None or handle is None:
                continue
            if item.state == DLState.cancelled:
                continue
            try:
                self._poll_one(item_id, item, handle)
            except Exception:
                logger.exception('Poll error for %s', item_id)
        self._try_start_next()
        self.stats_changed.emit()
        if self._dirty:
            self._save_counter += 1
            if self._save_counter >= 5:
                self._save_counter = 0
                self._dirty = False
                self._save_state()
        self._sync_timer_state()

    def _poll_one(self, item_id: str, item: DownloadItem, handle) -> None:
        s = handle.status()
        if item.state == DLState.paused:
            item.speed_down = '0 B/s'
            item.speed_up = '0 B/s'
            item.speed_down_kbps = 0.0
            item.speed_up_kbps = 0.0
            self.item_updated.emit(item_id)
            return
        if item_id in self._finalizing:
            return
        if item.state == DLState.downloading:
            zb_list = self._file_index.get(item_id)
            if zb_list:
                cur_pri = handle.file_priorities()
                if any((zb < len(cur_pri) and cur_pri[zb] == 0 for zb in zb_list)):
                    logger.warning('File priority reset detected mid-download; re-applying. item=%s', item_id)
                    pri = [0] * len(cur_pri)
                    for zb in zb_list:
                        if zb < len(pri):
                            pri[zb] = 7
                    handle.prioritize_files(pri)
        zb_list = self._file_index.get(item_id)
        selected_size = self._selected_size.get(item_id, 0)
        if zb_list and selected_size:
            file_progress = handle.file_progress()
            done_bytes = sum((file_progress[zb] for zb in zb_list if zb < len(file_progress)))
        else:
            done_bytes = s.total_done
        now = time.time()
        prev = self._last_bytes.get(item_id)
        down_speed = up_speed = 0.0
        if prev is not None:
            dt = max(now - prev[0], 0.001)
            down_speed = max(done_bytes - prev[1], 0) / dt
            up_speed = max(s.total_upload - prev[2], 0) / dt
        self._last_bytes[item_id] = (now, done_bytes, s.total_upload)
        with self._lock:
            item.downloaded_bytes = done_bytes
            item.total_bytes = selected_size or item.total_bytes
            item.progress = round(done_bytes / selected_size * 100 if selected_size else s.progress * 100, 2)
            item.speed_down_kbps = down_speed / 1024.0
            item.speed_up_kbps = up_speed / 1024.0
            item.speed_down = f'{_human_bytes(down_speed)}/s'
            item.speed_up = f'{_human_bytes(up_speed)}/s'
            item.peers = s.num_peers
            item.uploaded_bytes = s.total_upload
            item.ratio = round(s.total_upload / done_bytes, 3) if done_bytes else 0.0
            remaining = max((selected_size or 0) - done_bytes, 0)
            item.eta = _human_eta(int(remaining / down_speed) if down_speed > 0 else -1)
            lt_state = str(s.state)
            if 'checking' in lt_state and done_bytes == 0:
                item.state = DLState.verifying
            elif item.state == DLState.verifying:
                item.state = DLState.downloading
        self._dirty = True
        finished = selected_size > 0 and done_bytes >= selected_size
        if finished and item.state not in (DLState.completed, DLState.seeding):
            if item_id not in self._finalizing:
                self._finalizing.add(item_id)
                item.state = DLState.verifying
                self.item_updated.emit(item_id)
                threading.Thread(target=self._finalize_download_bg, args=(item_id, handle), daemon=True).start()
            return
        if item.state == DLState.seeding:
            started = self._start_time.get(item_id, now)
            seed_secs = now - started
            item.seed_time = _human_duration(seed_secs)
            hit_ratio = item.ratio_limit and item.ratio >= item.ratio_limit
            hit_time = item.seed_time_limit_min and seed_secs >= item.seed_time_limit_min * 60
            if hit_ratio or hit_time:
                self._teardown_handle(item_id, remove_files=False)
                item.state = DLState.completed
                item.speed_up = '0 B/s'
                item.speed_up_kbps = 0.0
        self.item_updated.emit(item_id)

    def _torrent_has_common_root_folder(self, torrent_info) -> bool:
        try:
            files_obj = torrent_info.files()
            num_files = torrent_info.num_files()
            if num_files <= 1:
                return False
            first_path = files_obj.file_path(0)
            top = first_path.split('/')[0] if '/' in first_path else first_path.split(os.sep)[0]
            if not top or top == first_path:
                return False
            for i in range(1, num_files):
                p = files_obj.file_path(i)
                p_top = p.split('/')[0] if '/' in p else p.split(os.sep)[0]
                if p_top != top:
                    return False
            return True
        except Exception:
            return False

    def _finalize_download_bg(self, item_id: str, handle) -> None:
        item = self._items.get(item_id)
        if item is None:
            self._finalizing.discard(item_id)
            return
        final_path = None
        zero_based_list = self._file_index.get(item_id) or []
        try:
            torrent_info = handle.torrent_file()
            has_common_root = self._torrent_has_common_root_folder(torrent_info)
            base_dir = os.path.join(_download_dir_for_category(item.category), item.console or '')
            dest_dir = base_dir if has_common_root else os.path.join(base_dir, item.game_name or '')
            os.makedirs(dest_dir, exist_ok=True)
            current_save_path = handle.status().save_path
            if os.path.abspath(current_save_path) != os.path.abspath(dest_dir):
                handle.move_storage(dest_dir)
                deadline = time.time() + 30
                while time.time() < deadline:
                    if os.path.abspath(handle.status().save_path) == os.path.abspath(dest_dir):
                        break
                    time.sleep(0.2)
                else:
                    logger.warning('move_storage did not confirm completion within timeout for %s', item_id)
            files_obj = torrent_info.files()
            moved_paths = [os.path.join(dest_dir, files_obj.file_path(zb)) for zb in zero_based_list]
            if len(moved_paths) == 1:
                final_path = moved_paths[0]
            elif moved_paths:
                final_path = os.path.join(dest_dir, files_obj.file_path(0).split('/')[0]) if has_common_root else dest_dir
        except Exception:
            logger.exception('Finalize failed for %s', item_id)
        seed_after = item.seed_after
        if not seed_after:
            self._teardown_handle(item_id, remove_files=False)
        with self._lock:
            item = self._items.get(item_id)
            if item is not None:
                if final_path is not None:
                    item.download_path = final_path
                item.progress = 100.0
                if seed_after:
                    item.state = DLState.seeding
                    self._start_time[item_id] = time.time()
                else:
                    item.state = DLState.completed
            self._finalizing.discard(item_id)
        if seed_after and item is not None:
            try:
                self._apply_handle_limits(handle, item)
            except Exception:
                pass
        self.item_updated.emit(item_id)
        self._save_state()

    def fetch_file_list(self, torrent_file: str, timeout_secs: float=30.0) -> list[tuple[str, int]]:
        source = self._resolve_torrent(torrent_file)
        handle = self._add_torrent(source, settings.download_dir)
        try:
            start = time.time()
            while not handle.status().has_metadata:
                if time.time() - start > timeout_secs:
                    raise RuntimeError(f'Timed out waiting for torrent metadata after {timeout_secs}s')
                time.sleep(0.2)
            torrent_info = handle.torrent_file()
            files_obj = torrent_info.files()
            num_files = torrent_info.num_files()
            try:
                handle.prioritize_files([0] * num_files)
            except Exception:
                pass
            return [(files_obj.file_path(i), files_obj.file_size(i)) for i in range(num_files)]
        finally:
            try:
                handle.pause()
            except Exception:
                pass
            try:
                self._session.remove_torrent(handle)
            except Exception:
                pass

    def _add_torrent(self, torrent_source: str, save_path: str):
        if torrent_source.startswith('magnet:'):
            atp = lt.parse_magnet_uri(torrent_source)
            atp.save_path = save_path
            atp.storage_mode = lt.storage_mode_t.storage_mode_sparse
            atp.flags &= ~lt.torrent_flags.auto_managed
            handle = self._session.add_torrent(atp)
        else:
            info = lt.torrent_info(torrent_source)
            handle = self._session.add_torrent({'ti': info, 'save_path': save_path, 'storage_mode': lt.storage_mode_t.storage_mode_sparse, 'flags': lt.torrent_flags.default_flags & ~lt.torrent_flags.auto_managed})
        for tracker_url in libtorrent_defaults.extra_trackers:
            try:
                handle.add_tracker({'url': tracker_url, 'tier': 0})
            except Exception:
                pass
        try:
            handle.force_reannounce()
        except Exception:
            pass
        try:
            handle.set_flags(lt.torrent_flags.auto_managed, False)
            handle.resume()
        except Exception:
            pass
        return handle

    def _resolve_torrent(self, torrent_file: str) -> str:
        if torrent_file.startswith('magnet:'):
            return torrent_file
        if os.path.isfile(torrent_file):
            return os.path.abspath(torrent_file)
        cached = os.path.join(self._torrent_cache, torrent_file)
        if os.path.isfile(cached):
            return cached
        candidates: list[str] = []
        seen: set[str] = set()

        def _add(name: str) -> None:
            if name and name not in seen:
                seen.add(name)
                candidates.append(name)
        _add(torrent_file)
        _add(torrent_file.replace(' ', '_'))
        if torrent_file.startswith('Minerva_Myrient '):
            _add('Minerva_Myrient' + torrent_file[len('Minerva_Myrient'):])
        _add(torrent_file.replace('_', ' '))
        from urllib.parse import quote
        for name in candidates:
            url = f'{network.cdn_base}/{quote(name, safe='')}'
            try:
                return self._fetch_torrent(url, cached)
            except FileNotFoundError:
                continue
        matched = self._find_on_cdn_listing(torrent_file)
        if matched:
            url = f'{network.cdn_base}/{quote(matched, safe='')}'
            return self._fetch_torrent(url, cached)
        raise RuntimeError(f"Could not locate '{torrent_file}' on the Minerva CDN.")

    def _fetch_torrent(self, url: str, cache_dest: str) -> str:
        import urllib.error
        import urllib.request
        req = urllib.request.Request(url, headers={'User-Agent': 'PiraChest/2.0'})
        try:
            with urllib.request.urlopen(req, timeout=network.torrent_download_timeout) as resp:
                if resp.status == 404:
                    raise FileNotFoundError(url)
                data = resp.read()
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                raise FileNotFoundError(url) from exc
            raise RuntimeError(f'Failed to fetch torrent from {url}: {exc}') from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f'Failed to fetch torrent from {url}: {exc}') from exc
        os.makedirs(os.path.dirname(cache_dest), exist_ok=True)
        with open(cache_dest, 'wb') as fh:
            fh.write(data)
        return cache_dest

    def _find_on_cdn_listing(self, torrent_file: str) -> Optional[str]:
        import difflib
        import urllib.request
        from urllib.parse import unquote
        req = urllib.request.Request(network.cdn_base + '/', headers={'User-Agent': 'PiraChest/2.0'})
        with urllib.request.urlopen(req, timeout=network.torrent_download_timeout) as resp:
            html = resp.read().decode('utf-8', errors='replace')
        names = [unquote(n) for n in re.findall('href="([^"]+\\.torrent)"', html)]
        if not names:
            return None
        matches = difflib.get_close_matches(torrent_file, names, n=1, cutoff=0.4)
        return matches[0] if matches else None

    def _save_state(self) -> None:
        try:
            with self._lock:
                data = {'order': list(self._order), 'items': [self._items[i].to_persist_dict() for i in self._order if i in self._items], 'global_down_kbps': self.global_down_kbps, 'global_up_kbps': self.global_up_kbps}
                tmp = f'{_QUEUE_FILE}.{os.getpid()}.{threading.get_ident()}.tmp'
                try:
                    with open(tmp, 'w', encoding='utf-8') as fh:
                        json.dump(data, fh, indent=2)
                    for attempt in range(5):
                        try:
                            os.replace(tmp, _QUEUE_FILE)
                            break
                        except PermissionError:
                            if attempt == 4:
                                raise
                            time.sleep(0.05)
                finally:
                    if os.path.exists(tmp):
                        try:
                            os.remove(tmp)
                        except OSError:
                            pass
        except Exception:
            logger.exception('Failed to save download queue state')

    def _load_state(self) -> None:
        if not os.path.isfile(_QUEUE_FILE):
            return
        try:
            with open(_QUEUE_FILE, 'r', encoding='utf-8') as fh:
                data = json.load(fh)
        except Exception:
            logger.exception('Failed to load download queue state')
            return
        self.global_down_kbps = data.get('global_down_kbps', self.global_down_kbps)
        self.global_up_kbps = data.get('global_up_kbps', self.global_up_kbps)
        for raw in data.get('items', []):
            try:
                item = DownloadItem.from_persist_dict(raw)
            except Exception:
                continue
            if item.backend == 'external' and item.state in (DLState.downloading, DLState.verifying):
                item.state = DLState.error
                item.error = 'Interrupted by app restart'
                item.speed_down = '0 B/s'
                item.speed_down_kbps = 0.0
            elif item.state in (DLState.downloading, DLState.verifying, DLState.paused, DLState.seeding):
                item.state = DLState.queued if item.state != DLState.paused else DLState.paused
                item.speed_down = '0 B/s'
                item.speed_up = '0 B/s'
                item.speed_down_kbps = 0.0
                item.speed_up_kbps = 0.0
            self._items[item.id] = item
            self._order.append(item.id)