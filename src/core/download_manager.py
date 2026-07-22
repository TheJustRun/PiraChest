from __future__ import annotations

import copy
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

from PyQt6.QtCore import QObject, QTimer, pyqtSignal

from .config import Paths, libtorrent_defaults, network, paths, settings

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
        raise RuntimeError(
            "python-libtorrent is not installed. Install it with "
            "'pip install libtorrent==2.0.13' (or your OS package, e.g. "
            "'apt install python3-libtorrent')."
        ) from _LT_IMPORT_ERROR

_CONFIG_DIR = os.path.join(Paths().project_root, ".config")
_QUEUE_FILE = os.path.join(_CONFIG_DIR, "pirachest_downloads.json")

class DLState(str, Enum):
    queued = "Queued"
    downloading = "Downloading"
    verifying = "Verifying"
    paused = "Paused"
    seeding = "Seeding"
    completed = "Completed"
    error = "Error"
    cancelled = "Cancelled"

def _human_bytes(n: float) -> str:
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if abs(n) < 1024.0:
            return f"{n:3.2f} {unit}"
        n /= 1024.0
    return f"{n:.2f} PiB"

def _human_eta(seconds: float) -> str:
    if seconds is None or seconds < 0 or seconds > 10**8:
        return "-"
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    if h:
        return f"{h}h {m:02d}m"
    if m:
        return f"{m}m {s:02d}s"
    return f"{s}s"

def _human_duration(seconds: float) -> str:
    if not seconds or seconds < 0:
        return "-"
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    if h:
        return f"{h}h {m:02d}m"
    return f"{m}m {s:02d}s"

@dataclass
class DownloadItem:
    id: str
    torrent_file: str
    file_id: int
    game_name: str
    console: str
    source: str = "Minerva"

    seed_after: bool = True
    max_down_kbps: int = 0
    max_up_kbps: int = 0
    max_peers: int = 500
    ratio_limit: float = 0.0
    seed_time_limit_min: int = 0

    state: DLState = DLState.queued
    error: str = ""
    download_path: str = ""
    retries: int = 0

    progress: float = 0.0
    downloaded_bytes: int = 0
    total_bytes: int = 0
    speed_down: str = "0 B/s"
    speed_up: str = "0 B/s"
    speed_down_kbps: float = 0.0
    speed_up_kbps: float = 0.0
    eta: str = "-"
    peers: int = 0
    uploaded_bytes: int = 0
    ratio: float = 0.0
    seed_time: str = "-"

    def to_persist_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["state"] = self.state.value
        return d

    @classmethod
    def from_persist_dict(cls, d: dict[str, Any]) -> "DownloadItem":
        d = dict(d)
        state_val = d.pop("state", DLState.queued.value)
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
            return f"{_human_bytes(self.downloaded_bytes)} / ?"
        return f"{_human_bytes(self.downloaded_bytes)} / {_human_bytes(self.total_bytes)}"

class DownloadManager(QObject):
    item_added = pyqtSignal(str)
    item_updated = pyqtSignal(str)
    item_removed = pyqtSignal(str)
    stats_changed = pyqtSignal()
    order_changed = pyqtSignal()

    def __init__(self, parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        _require_libtorrent()

        self.global_down_kbps = int(getattr(settings, "speed_limit", 0))
        self.global_up_kbps = int(getattr(settings, "upload_speed_limit", 500))

        settings_pack = {
            "user_agent": "PiraChest/2.0",
            "listen_interfaces": "0.0.0.0:6881,[::]:6881",
            "enable_dht": libtorrent_defaults.enable_dht,
            "enable_lsd": True,
            "enable_upnp": True,
            "enable_natpmp": True,
            "download_rate_limit": self.global_down_kbps * 1024 if self.global_down_kbps else 0,
            "upload_rate_limit": self.global_up_kbps * 1024,
            "active_downloads": -1,
            "active_seeds": -1,
            "active_limit": -1,
            "active_dht_limit": -1,
            "active_tracker_limit": -1,
            "active_lsd_limit": -1,
            "connections_limit": 800,
            "unchoke_slots_limit": -1,
            "connection_speed": 100,
            "rate_limit_utp": False,
            "aio_threads": 8,
            "send_buffer_watermark": 3 * 1024 * 1024,
            "checking_mem_usage": 2048,
        }
        self._session = lt.session(settings_pack)
        if libtorrent_defaults.enable_dht:
            for host, port in (
                ("router.bittorrent.com", 6881),
                ("dht.transmissionbt.com", 6881),
                ("router.utorrent.com", 6881),
            ):
                try:
                    self._session.add_dht_router(host, port)
                except Exception:
                    pass

        self._items: dict[str, DownloadItem] = {}
        self._order: list[str] = []
        self._handles: dict[str, "lt.torrent_handle"] = {}
        self._file_index: dict[str, int] = {}
        self._selected_size: dict[str, int] = {}
        self._start_time: dict[str, float] = {}
        self._last_bytes: dict[str, tuple[float, int, int]] = {}
        self._resolving: set[str] = set()
        self._finalizing: set[str] = set()

        self._torrent_cache = paths.torrent_cache
        os.makedirs(self._torrent_cache, exist_ok=True)
        os.makedirs(_CONFIG_DIR, exist_ok=True)

        self._lock = threading.RLock()

        self._timer = QTimer(self)
        self._timer.setInterval(1000)
        self._timer.timeout.connect(self._poll)

        self._save_counter = 0

        self._load_state()
        self._timer.start()
        self._try_start_next()

    def add(
        self,
        torrent_file: str,
        file_id: int,
        game_name: str,
        console: str,
        source: str = "Minerva",
    ) -> str:
        item_id = uuid.uuid4().hex
        item = DownloadItem(
            id=item_id,
            torrent_file=torrent_file,
            file_id=int(file_id or 1),
            game_name=game_name or "Unknown",
            console=console or "",
            source=source or "Minerva",
            state=DLState.queued,
        )
        with self._lock:
            self._items[item_id] = item
            self._order.append(item_id)
        self.item_added.emit(item_id)
        self._save_state()
        self._try_start_next()
        return item_id

    def add_many(self, roms_and_meta: list[dict[str, Any]]) -> list[str]:
        ids = []
        for m in roms_and_meta:
            ids.append(
                self.add(
                    torrent_file=m.get("torrent_file", ""),
                    file_id=m.get("file_id", 1),
                    game_name=m.get("game_name", ""),
                    console=m.get("console", ""),
                    source=m.get("source", "Minerva"),
                )
            )
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
            handle = self._handles.get(item_id)
            item = self._items.get(item_id)
            if not item:
                return
            if handle is not None and item.state in (DLState.downloading, DLState.verifying, DLState.seeding):
                handle.pause()
            item.state = DLState.paused
        self.item_updated.emit(item_id)
        self._save_state()

    def resume(self, item_id: str) -> None:
        with self._lock:
            item = self._items.get(item_id)
            if not item:
                return
            handle = self._handles.get(item_id)
            if handle is not None:
                handle.resume()
                item.state = DLState.downloading
            else:
                item.state = DLState.queued
        self.item_updated.emit(item_id)
        self._save_state()
        self._try_start_next()

    def cancel(self, item_id: str) -> None:
        self._teardown_handle(item_id, remove_files=False)
        with self._lock:
            item = self._items.get(item_id)
            if item:
                item.state = DLState.cancelled
                item.speed_down = "0 B/s"
                item.speed_up = "0 B/s"
                item.speed_down_kbps = 0.0
                item.speed_up_kbps = 0.0
        if item:
            self.item_updated.emit(item_id)
        self._save_state()
        self._try_start_next()

    def remove(self, item_id: str, delete_files: bool = False) -> None:
        self._teardown_handle(item_id, remove_files=delete_files)
        with self._lock:
            self._items.pop(item_id, None)
            if item_id in self._order:
                self._order.remove(item_id)
        self.item_removed.emit(item_id)
        self._save_state()
        self._try_start_next()

    def retry(self, item_id: str) -> None:
        self._teardown_handle(item_id, remove_files=False)
        with self._lock:
            item = self._items.get(item_id)
            if not item:
                return
            item.state = DLState.queued
            item.error = ""
            item.retries += 1
            item.progress = 0.0
            item.downloaded_bytes = 0
            item.total_bytes = 0
            item.speed_down = "0 B/s"
            item.speed_up = "0 B/s"
            item.speed_down_kbps = 0.0
            item.speed_up_kbps = 0.0
            item.eta = "-"
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
        target = item.download_path or os.path.join(settings.download_dir, item.console or "", item.game_name or "")
        return target if os.path.exists(target) else None

    def set_torrent_settings(
        self,
        item_id: str,
        *,
        seed_after: Optional[bool] = None,
        max_down_kbps: Optional[int] = None,
        max_up_kbps: Optional[int] = None,
        max_peers: Optional[int] = None,
        ratio_limit: Optional[float] = None,
        seed_time_limit_min: Optional[int] = None,
    ) -> None:
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
        self._session.apply_settings(
            {
                "download_rate_limit": self.global_down_kbps * 1024 if self.global_down_kbps else 0,
                "upload_rate_limit": self.global_up_kbps * 1024,
            }
        )
        self._try_start_next()

    def summary(self) -> dict[str, Any]:
        with self._lock:
            items = list(self._items.values())
        active = [i for i in items if i.state in (DLState.downloading, DLState.verifying)]
        queued = [i for i in items if i.state == DLState.queued]
        completed = [i for i in items if i.state == DLState.completed]

        total_down = sum(
            i.speed_down_kbps for i in items if i.state in (DLState.downloading, DLState.verifying)
        )
        total_up = sum(
            i.speed_up_kbps for i in items if i.state in (DLState.downloading, DLState.seeding, DLState.verifying)
        )
        return {
            "active": len(active),
            "queued": len(queued),
            "completed": len(completed),
            "total_down": _human_bytes(total_down * 1024) + "/s",
            "total_up": _human_bytes(total_up * 1024) + "/s",
        }

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

    _MAX_ACTIVE_TORRENTS = 5

    def _active_torrent_count(self) -> int:
        return sum(
            1
            for i in self._handles
            if self._items.get(i) is not None
            and self._items[i].state in (DLState.downloading, DLState.verifying)
        )

    def _try_start_next(self) -> None:
        with self._lock:
            order = list(self._order)

        free_slots = self._MAX_ACTIVE_TORRENTS - self._active_torrent_count()
        if free_slots <= 0:
            return

        for item_id in order:
            if free_slots <= 0:
                break
            item = self._items.get(item_id)
            if item is None or item.state != DLState.queued or item_id in self._resolving:
                continue
            self._resolving.add(item_id)
            threading.Thread(target=self._start_item, args=(item_id,), daemon=True).start()
            free_slots -= 1

    def _start_item(self, item_id: str) -> None:
        item = self._items.get(item_id)
        if item is None:
            self._resolving.discard(item_id)
            return
        try:
            source = self._resolve_torrent(item.torrent_file)
            handle = self._add_torrent(source, settings.download_dir)

            self._handles[item_id] = handle
            self._start_time[item_id] = time.time()

            _METADATA_TIMEOUT_SECS = 120
            metadata_wait_start = time.time()
            while not handle.status().has_metadata:
                if self._items.get(item_id) is None or self._items[item_id].state == DLState.cancelled:
                    self._session.remove_torrent(handle)
                    self._handles.pop(item_id, None)
                    self._resolving.discard(item_id)
                    return
                if time.time() - metadata_wait_start > _METADATA_TIMEOUT_SECS:
                    self._session.remove_torrent(handle)
                    self._handles.pop(item_id, None)
                    raise RuntimeError(
                        f"Timed out waiting for torrent metadata after "
                        f"{_METADATA_TIMEOUT_SECS}s (no peers/DHT reachable?)"
                    )
                time.sleep(0.2)

            torrent_info = handle.torrent_file()
            num_files = torrent_info.num_files()
            zero_based = item.file_id - 1
            if zero_based < 0 or zero_based >= num_files:
                raise RuntimeError(f"file_id={item.file_id} out of range ({num_files} files)")

            priorities = [0] * num_files
            priorities[zero_based] = 7

            for attempt in range(5):
                handle.prioritize_files(priorities)
                handle.set_sequential_download(True)
                time.sleep(0.3)
                actual = handle.file_priorities()
                if zero_based < len(actual) and actual[zero_based] > 0:
                    break
                logger.warning(
                    "prioritize_files() did not take (attempt %d/5) for item=%s; retrying",
                    attempt + 1, item_id,
                )
            else:
                logger.error(
                    "File priority never took after 5 attempts — download will "
                    "likely stall at 0 B/s. item=%s", item_id,
                )

            try:
                handle.force_recheck()
                handle.resume()
            except Exception:
                logger.exception("force_recheck/resume failed for %s", item_id)

            self._file_index[item_id] = zero_based
            self._selected_size[item_id] = torrent_info.files().file_size(zero_based)
            item.total_bytes = self._selected_size[item_id]
            item.state = DLState.downloading
            self._apply_handle_limits(handle, item)

            self.item_updated.emit(item_id)
        except Exception as exc:
            logger.exception("Failed to start download %s", item_id)
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
            handle.set_max_connections(item.max_peers or -1)
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
                logger.exception("Poll error for %s", item_id)

        self._try_start_next()
        self.stats_changed.emit()

        self._save_counter += 1
        if self._save_counter >= 5:
            self._save_counter = 0
            self._save_state()

    def _poll_one(self, item_id: str, item: DownloadItem, handle) -> None:
        s = handle.status()

        if item.state == DLState.paused:
            item.speed_down = "0 B/s"
            item.speed_up = "0 B/s"
            item.speed_down_kbps = 0.0
            item.speed_up_kbps = 0.0
            self.item_updated.emit(item_id)
            return

        if item_id in self._finalizing:
            return

        if item.state == DLState.downloading:
            zb = self._file_index.get(item_id)
            if zb is not None:
                cur_pri = handle.file_priorities()
                if zb < len(cur_pri) and cur_pri[zb] == 0:
                    logger.warning("File priority reset detected mid-download; re-applying. item=%s", item_id)
                    pri = [0] * len(cur_pri)
                    pri[zb] = 7
                    handle.prioritize_files(pri)

        zero_based = self._file_index.get(item_id)
        selected_size = self._selected_size.get(item_id, 0)

        if zero_based is not None and selected_size:
            file_progress = handle.file_progress()
            done_bytes = file_progress[zero_based] if zero_based < len(file_progress) else 0
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
            item.progress = round((done_bytes / selected_size * 100) if selected_size else s.progress * 100, 2)
            item.speed_down_kbps = down_speed / 1024.0
            item.speed_up_kbps = up_speed / 1024.0
            item.speed_down = f"{_human_bytes(down_speed)}/s"
            item.speed_up = f"{_human_bytes(up_speed)}/s"
            item.peers = s.num_peers
            item.uploaded_bytes = s.total_upload
            item.ratio = round(s.total_upload / done_bytes, 3) if done_bytes else 0.0

            remaining = max((selected_size or 0) - done_bytes, 0)
            item.eta = _human_eta(int(remaining / down_speed) if down_speed > 0 else -1)

            lt_state = str(s.state)
            if "checking" in lt_state and done_bytes == 0:
                item.state = DLState.verifying
            elif item.state == DLState.verifying:
                item.state = DLState.downloading

        finished = selected_size > 0 and done_bytes >= selected_size

        if finished and item.state not in (DLState.completed, DLState.seeding):
            if item_id not in self._finalizing:
                self._finalizing.add(item_id)
                item.state = DLState.verifying
                self.item_updated.emit(item_id)
                threading.Thread(
                    target=self._finalize_download_bg,
                    args=(item_id, handle),
                    daemon=True,
                ).start()
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
                item.speed_up = "0 B/s"
                item.speed_up_kbps = 0.0

        self.item_updated.emit(item_id)

    def _finalize_download_bg(self, item_id: str, handle) -> None:
        item = self._items.get(item_id)
        if item is None:
            self._finalizing.discard(item_id)
            return

        final_path = None
        zero_based = self._file_index.get(item_id)
        try:
            torrent_info = handle.torrent_file()
            selected_path = torrent_info.files().file_path(zero_based) if zero_based is not None else ""
            downloaded_path = os.path.join(settings.download_dir, selected_path)
            dest_dir = os.path.join(settings.download_dir, item.console or "", item.game_name or "")
            final_path = self._finalize_file(downloaded_path, dest_dir)
        except Exception:
            logger.exception("Finalize failed for %s", item_id)

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

    @staticmethod
    def _finalize_file(downloaded_path: str, dest_dir: str) -> str:
        if not os.path.isfile(downloaded_path):
            return downloaded_path
        os.makedirs(dest_dir, exist_ok=True)
        target = os.path.join(dest_dir, os.path.basename(downloaded_path))
        if os.path.abspath(downloaded_path) != os.path.abspath(target):
            shutil.move(downloaded_path, target)
        return target

    def _add_torrent(self, torrent_source: str, save_path: str):
        if torrent_source.startswith("magnet:"):
            atp = lt.parse_magnet_uri(torrent_source)
            atp.save_path = save_path
            atp.storage_mode = lt.storage_mode_t.storage_mode_sparse
            atp.flags &= ~lt.torrent_flags.auto_managed
            handle = self._session.add_torrent(atp)
        else:
            info = lt.torrent_info(torrent_source)
            handle = self._session.add_torrent(
                {
                    "ti": info,
                    "save_path": save_path,
                    "storage_mode": lt.storage_mode_t.storage_mode_sparse,
                    "flags": lt.torrent_flags.default_flags & ~lt.torrent_flags.auto_managed,
                }
            )
        try:
            handle.set_flags(lt.torrent_flags.auto_managed, False)
            handle.resume()
        except Exception:
            pass
        return handle

    def _resolve_torrent(self, torrent_file: str) -> str:
        if torrent_file.startswith("magnet:"):
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
        _add(torrent_file.replace(" ", "_"))
        if torrent_file.startswith("Minerva_Myrient "):
            _add("Minerva_Myrient" + torrent_file[len("Minerva_Myrient"):])
        _add(torrent_file.replace("_", " "))

        from urllib.parse import quote

        for name in candidates:
            url = f"{network.cdn_base}/{quote(name, safe='')}"
            try:
                return self._fetch_torrent(url, cached)
            except FileNotFoundError:
                continue

        matched = self._find_on_cdn_listing(torrent_file)
        if matched:
            url = f"{network.cdn_base}/{quote(matched, safe='')}"
            return self._fetch_torrent(url, cached)

        raise RuntimeError(f"Could not locate '{torrent_file}' on the Minerva CDN.")

    def _fetch_torrent(self, url: str, cache_dest: str) -> str:
        import urllib.error
        import urllib.request

        req = urllib.request.Request(url, headers={"User-Agent": "PiraChest/2.0"})
        try:
            with urllib.request.urlopen(req, timeout=network.torrent_download_timeout) as resp:
                if resp.status == 404:
                    raise FileNotFoundError(url)
                data = resp.read()
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                raise FileNotFoundError(url) from exc
            raise RuntimeError(f"Failed to fetch torrent from {url}: {exc}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"Failed to fetch torrent from {url}: {exc}") from exc

        os.makedirs(os.path.dirname(cache_dest), exist_ok=True)
        with open(cache_dest, "wb") as fh:
            fh.write(data)
        return cache_dest

    def _find_on_cdn_listing(self, torrent_file: str) -> Optional[str]:
        import difflib
        import urllib.request
        from urllib.parse import unquote

        req = urllib.request.Request(network.cdn_base + "/", headers={"User-Agent": "PiraChest/2.0"})
        with urllib.request.urlopen(req, timeout=network.torrent_download_timeout) as resp:
            html = resp.read().decode("utf-8", errors="replace")

        names = [unquote(n) for n in re.findall(r'href="([^"]+\.torrent)"', html)]
        if not names:
            return None
        matches = difflib.get_close_matches(torrent_file, names, n=1, cutoff=0.4)
        return matches[0] if matches else None

    def _save_state(self) -> None:
        try:
            with self._lock:
                data = {
                    "order": list(self._order),
                    "items": [self._items[i].to_persist_dict() for i in self._order if i in self._items],
                    "global_down_kbps": self.global_down_kbps,
                    "global_up_kbps": self.global_up_kbps,
                }
            tmp = _QUEUE_FILE + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(data, fh, indent=2)
            os.replace(tmp, _QUEUE_FILE)
        except Exception:
            logger.exception("Failed to save download queue state")

    def _load_state(self) -> None:
        if not os.path.isfile(_QUEUE_FILE):
            return
        try:
            with open(_QUEUE_FILE, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except Exception:
            logger.exception("Failed to load download queue state")
            return

        self.global_down_kbps = data.get("global_down_kbps", self.global_down_kbps)
        self.global_up_kbps = data.get("global_up_kbps", self.global_up_kbps)

        for raw in data.get("items", []):
            try:
                item = DownloadItem.from_persist_dict(raw)
            except Exception:
                continue
            if item.state in (DLState.downloading, DLState.verifying, DLState.paused, DLState.seeding):
                item.state = DLState.queued if item.state != DLState.paused else DLState.paused
                item.speed_down = "0 B/s"
                item.speed_up = "0 B/s"
                item.speed_down_kbps = 0.0
                item.speed_up_kbps = 0.0
            self._items[item.id] = item
            self._order.append(item.id)