from __future__ import annotations

import difflib
import email.utils
import hashlib
import importlib.util
import ipaddress
import json
import logging
import os
import re
import shutil
import struct
import sys
import threading
import time
import uuid
from contextlib import suppress
from dataclasses import asdict, dataclass, field, fields
from enum import Enum
from typing import Any, Callable, Optional
from urllib.parse import quote, unquote, urlsplit

from PySide6.QtCore import QObject, QRunnable, Qt, QThread, QThreadPool, QTimer, Signal

from src.core.config import libtorrent_defaults, network, paths, save_settings, settings
from src.core.debrid import DebridError, DebridService, configured as debrid_configured
from src.core.debrid.archive import ArchiveError, extract_archive
from src.core.minerva.rom_extract import extract_rom_archives
from src.core.worker import DL, IO, Priority, submit

logger = logging.getLogger(__name__)


def get_libtorrent():
    import importlib
    try:
        return importlib.import_module("libtorrent")
    except ImportError as exc:
        raise ImportError(
            "python-libtorrent is not installed. Install it with 'pip install libtorrent==2.1.1'."
        ) from exc


try:
    lt = get_libtorrent()
except ImportError:
    lt = None
else:
    if tuple(map(int, lt.__version__.split(".")[:3])) < (2, 1, 2):
        logger.info("libtorrent %s: 2.1.2 fixes lost file-priority updates and a disk I/O race; upgrade once a wheel is published", lt.__version__)

_QUEUE_FILE = os.path.join(paths.config_dir, "pirachest_downloads.json")
_DHT_FILE = os.path.join(paths.config_dir, "bt_dht_state.dat")
_RESUME_DIR = os.path.join(paths.config_dir, "resume")
_FLAT = frozenset({"repacks", "minerva"})
_UA = "PiraChest/3.0"
_DHT_NODES = "router.bittorrent.com:6881,dht.transmissionbt.com:6881,router.utorrent.com:6881,dht.libtorrent.org:25401"


_TUNED_SESSION_SETTINGS = {
    "connection_speed": 100,
    "peer_connect_timeout": 6,
    "min_reconnect_time": 10,
    "handshake_timeout": 8,
    "peer_timeout": 75,
    "request_timeout": 45,
    "piece_timeout": 15,
    "max_failcount": 2,
    "connect_seed_every_n_download": 5,
    "allowed_fast_set_size": 32,
}
_LEGACY_SESSION_SETTINGS = {
    "connection_speed": 50,
    "peer_connect_timeout": 8,
    "min_reconnect_time": 30,
    "handshake_timeout": 10,
    "peer_timeout": 120,
    "request_timeout": 60,
    "piece_timeout": 20,
    "max_failcount": 3,
    "connect_seed_every_n_download": 10,
    "allowed_fast_set_size": 5,
}
_STALL_REANNOUNCE = 8
_STALL_SCRAPE = 30
_STALL_RETRY = 75


def _peer_fingerprint() -> str:
    return f"-PC{uuid.uuid4().hex[:4].upper()}-"
_SETTING_KEYS = frozenset({"seed_after", "max_down_kbps", "max_up_kbps", "max_peers", "ratio_limit", "seed_time_limit_min"})
_TORRENT_HREF = re.compile(r'href="([^"]+\.torrent)"')
_NAME_HDR = re.compile(r"filename\*=(?:UTF-8'')?([^;]+)|filename=\"?([^\";]+)\"?", re.I)
_BAD_NAME_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f]+')
READ_CHUNK = 256 * 1024


def _load_torrent_info(path: str):
    raw = {"max_buffer_size": 1 << 30, "max_decode_tokens": 200_000_000, "max_pieces": 1 << 24, "max_decode_depth": 100}
    attempts = []
    if hasattr(lt, "load_torrent_limits"):
        def _obj():
            limits = lt.load_torrent_limits()
            for k, v in raw.items():
                if hasattr(limits, k):
                    setattr(limits, k, v)
            return lt.load_torrent_file(path, limits).ti
        attempts.append(_obj)
    if hasattr(lt, "load_torrent_file"):
        attempts.append(lambda: lt.load_torrent_file(path, dict(raw)).ti)
        attempts.append(lambda: lt.load_torrent_file(path, dict(raw)))
    attempts.append(lambda: lt.torrent_info(path, dict(raw)))
    last: Optional[Exception] = None
    for attempt in attempts:
        try:
            ti = attempt()
            if ti is not None and hasattr(ti, "num_files"):
                return ti
        except Exception as exc:
            last = exc
    raise RuntimeError(f"Could not load torrent {os.path.basename(path)}: {last}")


MIN_SPLIT = 1024 * 1024
RETRIES = 6
PERMANENT = frozenset({400, 401, 403, 404, 405, 410, 451})
_RECORD = struct.Struct("<QQQ")
_NAME_RE = re.compile(r"filename\*=(?:UTF-8\'\')?([^;]+)|filename=\"?([^\";]+)\"?", re.IGNORECASE)
_META_CACHE = 32
_NORMAL, _TOP = 4, 7


class DLState(str, Enum):
    queued = "Queued"
    downloading = "Downloading"
    verifying = "Verifying"
    paused = "Paused"
    seeding = "Seeding"
    completed = "Completed"
    error = "Error"
    cancelled = "Cancelled"


_RUNNING = (DLState.downloading, DLState.verifying)
_LIVE = (DLState.queued, *_RUNNING)
_TRANSIENT = ("speed_down_kbps", "speed_up_kbps", "eta", "peers", "seeds", "seed_seconds", "ratio")


@dataclass
class NetPrefs:
    proxy_type: str = "none"
    proxy_host: str = ""
    proxy_port: int = 0
    proxy_user: str = ""
    proxy_pass: str = ""
    proxy_peers: bool = True
    force_proxy: bool = False
    anonymous: bool = False
    require_encryption: bool = False
    dht: bool = field(default_factory=lambda: bool(libtorrent_defaults.enable_dht))
    lsd: bool = True
    upnp: bool = True
    bind: str = ""
    private_history: bool = False

    @property
    def has_proxy(self) -> bool:
        return self.proxy_type in ("http", "socks5") and bool(self.proxy_host and self.proxy_port)

    def proxy_url(self) -> str:
        if not self.has_proxy:
            return ""
        auth = f"{quote(self.proxy_user, safe='')}:{quote(self.proxy_pass, safe='')}@" if self.proxy_user else ""
        return f"{'socks5h' if self.proxy_type == 'socks5' else 'http'}://{auth}{self.proxy_host}:{self.proxy_port}"

    def bind_ip(self) -> str:
        try:
            return str(ipaddress.ip_address(self.bind.strip()))
        except ValueError:
            return ""

    def leak_guard(self) -> str:
        return "Force-proxy is enabled but no proxy is configured" if self.force_proxy and not self.has_proxy else ""


NET = NetPrefs()


def human_bytes(n: float) -> str:
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if abs(n) < 1024.0:
            return f"{int(n)} B" if unit == "B" else f"{n:.2f} {unit}"
        n /= 1024.0
    return f"{n:.2f} PiB"


def human_speed(kbps: float) -> str:
    return f"{human_bytes(kbps * 1024)}/s"


def human_duration(seconds: float) -> str:
    if seconds is None or not 0 <= seconds <= 10 ** 8:
        return "-"
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    return f"{h}h {m:02d}m" if h else f"{m}m {s:02d}s" if m else f"{s}s"


_RESERVED = re.compile(r"^(con|prn|aux|nul|com[1-9]|lpt[1-9])(\..*)?$", re.I)
_RISKY_EXT = frozenset(".exe .msi .bat .cmd .com .scr .pif .ps1 .psm1 .vbs .vbe .js .jse .wsf .wsh .hta .jar .lnk .reg .dll .cpl "
                       ".msc .msp .appx .msix .sh .bash .command .app .apk .appimage .run .scpt .desktop .url".split())
_F_RISKY, _F_LOCKED = 1, 2
_PRIOS = (0, 1, 4, 6, 7)


def to_safe_name(name: str, fallback: str = "download") -> str:
    out = _BAD_NAME_RE.sub("_", name).strip().strip(". ")[:180]
    return ("_" + out if _RESERVED.match(out) else out) or fallback


def is_risky_file(path: str) -> bool:
    return os.path.splitext(path)[1].lower() in _RISKY_EXT


def _is_reparse(path: str) -> bool:
    try:
        st = os.lstat(path)
    except OSError:
        return False
    return os.path.islink(path) or bool(getattr(st, "st_file_attributes", 0) & 0x400)


def _inside(root: str, path: str) -> bool:
    try:
        r, p = os.path.normcase(os.path.abspath(root)), os.path.normcase(os.path.abspath(path))
        return os.path.commonpath([r, p]) == r
    except ValueError:
        return False


def _contained(root: str, path: str) -> bool:
    if not _inside(root, path):
        return False
    cur = os.path.abspath(root)
    for part in os.path.relpath(os.path.abspath(path), cur).split(os.sep):
        cur = os.path.join(cur, part)
        if part not in ("", ".") and _is_reparse(cur):
            return False
    return True


def _bad_torrent_path(p: str) -> bool:
    p = p.replace("\\", "/")
    return (not p or len(p) > 1024 or p.startswith("/") or (os.name == "nt" and ":" in p) or "\x00" in p
            or any(part in ("..", "") for part in p.split("/")))


def _file_flags(fs, i: int) -> int:
    fl = fs.file_flags(i)
    return (_F_LOCKED if fl & 9 else 0) | (_F_RISKY if is_risky_file(fs.file_path(i)) else 0)


def _validate_ti(ti, save_path: str, sel: Optional[list[int]] = None) -> None:
    fs, seen = ti.files(), set()
    for i in range(fs.num_files()):
        if _bad_torrent_path(fs.file_path(i)):
            raise RuntimeError("Torrent contains an unsafe file path")
    for i in (sel if sel is not None else range(fs.num_files())):
        d = os.path.dirname(os.path.join(save_path, fs.file_path(i)))
        if d not in seen:
            if not _contained(save_path, d):
                raise RuntimeError("Refusing to write through a link or outside the download folder")
            seen.add(d)


def _secs(x) -> int:
    try:
        return int(x.total_seconds())
    except AttributeError:
        pass
    try:
        return int(x.timestamp())
    except AttributeError:
        return int(x or 0)


def _scrub(text: str) -> str:
    text = re.sub(r"(?i)\b([a-z][a-z0-9+.-]*://)[^/\s@]+@", r"\1***@", str(text))
    home = os.path.expanduser("~")
    return text.replace(home, "~") if len(home) > 3 else text


def category_dir(category: str) -> str:
    default = settings.download_dir if category in _FLAT else os.path.join(settings.download_dir, category)
    return getattr(settings, f"download_dir_{category}", default)


def _base_dir(item: "DownloadItem") -> str:
    return os.path.join(category_dir(item.category), to_safe_name(item.console, "") if item.console else "")


def _game_dir(item: "DownloadItem") -> str:
    return os.path.join(_base_dir(item), to_safe_name(item.game_name or "", "download"))


def _is_remote_source(torrent_file: str) -> bool:
    return torrent_file.startswith(("magnet:", "http://", "https://"))


def _root_folder(ti) -> str:
    f = ti.files()
    names = [f.file_path(i).replace("\\", "/") for i in range(f.num_files())]
    top = names[0].split("/")[0] if names else ""
    return top if top and all(n.startswith(top + "/") for n in names) else ""


def _safe_relative(path: str, fallback: str = "download") -> str:
    parts = [to_safe_name(part) for part in str(path or "").replace("\\", "/").split("/") if part and part not in (".", "..")]
    return os.path.join(*parts) if parts else to_safe_name(fallback)


def _common_root(paths: list[str]) -> str:
    names = [str(path or "").replace("\\", "/").strip("/") for path in paths if path]
    if not names:
        return ""
    first = names[0].split("/")[0]
    return first if first and all(name.startswith(first + "/") for name in names) else ""


class HttpError(Exception):
    pass


class Cancelled(Exception):
    pass


class RateLimiter:
    def __init__(self, bytes_per_sec: int = 0):
        self._rate = bytes_per_sec
        self._lock = threading.Lock()
        self._next = time.monotonic()

    def set_rate(self, bytes_per_sec: int) -> None:
        with self._lock:
            self._rate = max(0, int(bytes_per_sec))

    def acquire(self, n: int, cancel: threading.Event) -> None:
        with self._lock:
            if not self._rate:
                return
            now = time.monotonic()
            start = max(now, self._next)
            self._next = start + n / self._rate
            delay = start - now
        if delay > 0:
            cancel.wait(delay)


@dataclass(frozen=True)
class Probe:
    url: str
    size: int
    ranged: bool
    name: str
    last_modified: str


@dataclass
class _Segment:
    start: int
    end: int
    pos: int

    @property
    def remaining(self) -> int:
        return self.end - self.pos + 1


def new_session(connections: int, headers: Optional[dict] = None) -> Any:
    import requests
    from requests.adapters import HTTPAdapter

    class _Adapter(HTTPAdapter):
        def __init__(self, connections: int, source: str):
            self._source = source
            super().__init__(pool_connections=1, pool_maxsize=max(connections, 1), max_retries=0)

        def init_poolmanager(self, *args, **kwargs):
            if self._source:
                kwargs["source_address"] = (self._source, 0)
            super().init_poolmanager(*args, **kwargs)

        def proxy_manager_for(self, proxy, **kwargs):
            if self._source:
                kwargs["source_address"] = (self._source, 0)
            return super().proxy_manager_for(proxy, **kwargs)

    if msg := NET.leak_guard():
        raise HttpError(msg)
    session = requests.Session()
    adapter = _Adapter(connections, NET.bind_ip())
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    session.headers.update({"Accept-Encoding": "identity", "User-Agent": "Mozilla/5.0" if NET.anonymous else _UA, **(headers or {})})
    if NET.has_proxy:
        if NET.proxy_type == "socks5" and importlib.util.find_spec("socks") is None:
            raise HttpError("SOCKS proxy for direct downloads needs 'pip install requests[socks]'")
        session.trust_env = False
        session.proxies = {"http": NET.proxy_url(), "https": NET.proxy_url()}
    return session


def probe(session: Any, url: str, timeout: float = 20.0) -> Probe:
    with session.get(url, headers={"Range": "bytes=0-0"}, stream=True, timeout=timeout) as r:
        if r.status_code >= 400:
            raise HttpError(f"HTTP {r.status_code}")
        ranged = r.status_code == 206
        if ranged:
            total = r.headers.get("Content-Range", "").rpartition("/")[2]
            size = int(total) if total.isdigit() else 0
            ranged = size > 0
        else:
            size = int(r.headers.get("Content-Length") or 0)
        match = _NAME_RE.search(r.headers.get("Content-Disposition", ""))
        name = unquote((match.group(1) or match.group(2)).strip()) if match else ""
        name = name or unquote(os.path.basename(urlsplit(r.url).path))
        return Probe(r.url, size, ranged, to_safe_name(name), r.headers.get("Last-Modified", ""))


class SegmentedDownload:
    def __init__(
        self,
        url: str,
        path: str,
        *,
        headers: Optional[dict] = None,
        connections: int = 8,
        window: int = 0,
        limiter: Optional[RateLimiter] = None,
        on_progress: Optional[Callable[[int, int, float], None]] = None,
        info: Optional[Probe] = None,
    ):
        self.path = path
        self.total = 0
        self._url = url
        self._record = path + ".ghd"
        self._connections = max(1, connections)
        self._window = window
        self._limiter = limiter or RateLimiter()
        self._on_progress = on_progress
        self._info = info
        self._session = new_session(self._connections, headers)
        self._cancel = threading.Event()
        self._lock = threading.Lock()
        self._segments: list[_Segment] = []
        self._errors: list[BaseException] = []
        self._ranged = False

    def cancel(self) -> None:
        self._cancel.set()

    def run(self) -> None:
        try:
            self._run()
        finally:
            self._session.close()

    def _run(self) -> None:
        info = self._info or probe(self._session, self._url)
        self._url = info.url
        self.total = info.size
        self._ranged = info.ranged
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        resumed = self._ranged and os.path.exists(self.path) and self._load_record(info.size)
        if not resumed:
            self._remove_record()
            self._segments = self._plan(info)
            with open(self.path, "wb") as f:
                if info.size:
                    f.truncate(info.size)
        pending = [s for s in self._segments if s.end < 0 or s.pos <= s.end]
        threads = [threading.Thread(target=self._worker, args=(s,), daemon=True) for s in pending]
        done = threading.Event()
        supervisor = threading.Thread(target=self._supervise, args=(done,), daemon=True)
        supervisor.start()
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        done.set()
        supervisor.join()
        if self._errors:
            self._save_record()
            raise self._errors[0]
        if self._cancel.is_set():
            self._save_record()
            raise Cancelled()
        self._remove_record()
        if info.last_modified:
            try:
                mtime = email.utils.parsedate_to_datetime(info.last_modified).timestamp()
                os.utime(self.path, (mtime, mtime))
            except (TypeError, ValueError, OSError):
                pass

    def _plan(self, info: Probe) -> list[_Segment]:
        if not info.ranged:
            return [_Segment(0, -1, 0)]
        count = max(1, min(self._connections, info.size // MIN_SPLIT))
        step = info.size // count
        segments = [_Segment(i * step, (i + 1) * step - 1, i * step) for i in range(count)]
        segments[-1].end = info.size - 1
        return segments

    def _load_record(self, size: int) -> bool:
        try:
            with open(self._record, "rb") as f:
                segments = [_Segment(s, e, p) for s, p, e in _RECORD.iter_unpack(f.read())]
        except (OSError, struct.error):
            return False
        if not segments or max(s.end for s in segments) != size - 1:
            return False
        self._segments = segments
        return True

    def _save_record(self) -> None:
        if not self._ranged:
            return
        with self._lock:
            payload = b"".join(_RECORD.pack(s.start, s.pos, s.end) for s in self._segments)
        tmp = self._record + ".tmp"
        try:
            with open(tmp, "wb") as f:
                f.write(payload)
            os.replace(tmp, self._record)
        except OSError:
            pass

    def _remove_record(self) -> None:
        for p in (self._record, self._record + ".tmp"):
            try:
                os.remove(p)
            except OSError:
                pass

    def _bytes(self) -> int:
        return sum(s.pos - s.start for s in self._segments)

    def _supervise(self, done: threading.Event) -> None:
        last_t, last_n, speed = time.monotonic(), self._bytes(), 0.0
        while not done.wait(0.5):
            now, n = time.monotonic(), self._bytes()
            instant = (n - last_n) / max(now - last_t, 1e-6)
            speed = instant if speed == 0 else speed * 0.6 + instant * 0.4
            last_t, last_n = now, n
            if self._on_progress:
                self._on_progress(n, self.total, speed)
            self._save_record()
        if self._on_progress:
            n = self._bytes()
            self._on_progress(n, self.total or n, 0.0)

    def _steal(self) -> Optional[_Segment]:
        with self._lock:
            victim = max(self._segments, key=lambda s: s.remaining)
            if victim.remaining < MIN_SPLIT * 2:
                return None
            half = victim.remaining // 2
            start = victim.end - half + 1
            seg = _Segment(start, victim.end, start)
            victim.end = start - 1
            self._segments.append(seg)
            return seg

    def _worker(self, seg: _Segment) -> None:
        try:
            with open(self.path, "r+b", buffering=0) as f:
                while seg is not None and not self._cancel.is_set():
                    self._fetch(seg, f)
                    seg = self._steal() if self._ranged else None
        except Cancelled:
            pass
        except Exception as exc:
            self._errors.append(exc)
            self._cancel.set()

    def _write(self, f, chunk: bytes) -> None:
        try:
            f.write(chunk)
        except OSError as exc:
            raise HttpError(f"Disk error: {exc}") from exc

    def _fetch(self, seg: _Segment, f) -> None:
        import requests
        stream = seg.end < 0
        attempt = 0
        while stream or seg.pos <= seg.end:
            if self._cancel.is_set():
                raise Cancelled()
            if stream:
                headers = {}
            else:
                hi = min(seg.end, seg.pos + self._window - 1) if self._window else seg.end
                headers = {"Range": f"bytes={seg.pos}-{hi}"}
            try:
                with self._session.get(self._url, headers=headers, stream=True, timeout=(10, 30)) as r:
                    if r.status_code in PERMANENT:
                        raise HttpError(f"HTTP {r.status_code}")
                    if r.status_code != (200 if stream else 206):
                        raise requests.RequestException(f"HTTP {r.status_code}")
                    if stream:
                        f.truncate(0)
                        seg.pos = 0
                    f.seek(seg.pos)
                    for chunk in r.iter_content(READ_CHUNK):
                        if self._cancel.is_set():
                            raise Cancelled()
                        if not stream:
                            chunk = chunk[: seg.end - seg.pos + 1]
                            if not chunk:
                                break
                        self._limiter.acquire(len(chunk), self._cancel)
                        self._write(f, chunk)
                        seg.pos += len(chunk)
                if stream:
                    self.total = seg.pos
                    return
                attempt = 0
            except (Cancelled, HttpError):
                raise
            except (requests.RequestException, OSError, EOFError) as exc:
                attempt += 1
                if attempt > RETRIES:
                    raise HttpError(str(exc)) from exc
                self._cancel.wait(min(2 ** attempt, 15))


@dataclass
class DownloadItem:
    id: str
    torrent_file: str = ""
    file_id: int = 1
    game_name: str = "Unknown"
    console: str = ""
    source: str = "Minerva"
    backend: str = "torrent"
    category: str = "minerva"
    url: str = ""
    file_name: str = ""
    file_ids: list[int] = field(default_factory=list)
    debrid_provider: str = ""
    debrid_transfer_id: str = ""
    info_hash: str = ""
    fallback_used: bool = False
    seed_after: bool = True
    max_down_kbps: int = 0
    max_up_kbps: int = 0
    max_peers: int = libtorrent_defaults.max_connections_per_torrent
    ratio_limit: float = 0.0
    seed_time_limit_min: int = 0
    state: DLState = DLState.queued
    error: str = ""
    download_path: str = ""
    priority: int = 0
    progress: float = 0.0
    downloaded_bytes: int = 0
    total_bytes: int = 0
    speed_down_kbps: float = 0.0
    speed_up_kbps: float = 0.0
    eta: int = -1
    peers: int = 0
    seeds: int = 0
    seed_seconds: int = 0
    ratio: float = 0.0

    def to_persist_dict(self) -> dict[str, Any]:
        data = {k: v for k, v in asdict(self).items() if k not in _TRANSIENT}
        data["state"] = self.state.value
        return data

    @classmethod
    def from_persist_dict(cls, data: dict[str, Any]) -> "DownloadItem":
        state = DLState(data.get("state", DLState.queued.value))
        return cls(**{k: v for k, v in data.items() if k in _PERSISTED and k != "state"}, state=state)

    def clear_live(self) -> None:
        self.speed_down_kbps = self.speed_up_kbps = 0.0
        self.eta = -1
        self.peers = self.seeds = 0


_PERSISTED = frozenset(DownloadItem.__dataclass_fields__) - frozenset(_TRANSIENT)


class _HttpSignals(QObject):
    started = Signal(str, str, str)
    progress = Signal(str, int, int, float)
    finished = Signal(str)
    failed = Signal(str, str)


class _HttpJob(QRunnable):
    def __init__(self, item: DownloadItem, signals: _HttpSignals, limiter: RateLimiter, connections: int):
        super().__init__()
        self.item_id = item.id
        self._url = item.url
        self._path = item.download_path
        self._dir = _base_dir(item)
        self._file_name = item.file_name
        self._signals = signals
        self._limiter = limiter
        self._connections = connections
        self.download: Optional[SegmentedDownload] = None
        self.cancelled = False

    def cancel(self) -> None:
        self.cancelled = True
        if self.download:
            self.download.cancel()

    def run(self) -> None:
        try:
            with new_session(1) as session:
                info = probe(session, self._url)
            if not self._path:
                self._path = os.path.join(self._dir, to_safe_name(self._file_name) if self._file_name else info.name)
            self._signals.started.emit(self.item_id, os.path.basename(self._path), self._path)
            self.download = SegmentedDownload(
                info.url, self._path, connections=self._connections, limiter=self._limiter, info=info,
                on_progress=lambda n, total, speed: self._signals.progress.emit(self.item_id, n, total, speed / 1024.0),
            )
            if self.cancelled:
                return
            self.download.run()
        except Cancelled:
            return
        except Exception as exc:
            if not self.cancelled:
                self._signals.failed.emit(self.item_id, str(exc))
            return
        if not self.cancelled:
            self._signals.finished.emit(self.item_id)


class _DebridJob(QRunnable):
    def __init__(self, item: DownloadItem, signals: _HttpSignals, limiter: RateLimiter, connections: int,
                 resolver: Optional[Callable[[str], str]] = None):
        super().__init__()
        self.item_id = item.id
        self._source = item.torrent_file
        self._file_ids = list(item.file_ids)
        self._provider = item.debrid_provider
        self._game_name = item.game_name
        self._dir = _game_dir(item)
        self._signals = signals
        self._limiter = limiter
        self._connections = connections
        self._resolver = resolver
        self.download: Optional[SegmentedDownload] = None
        self.cancelled = False
        self._cancel = threading.Event()

    def cancel(self) -> None:
        self.cancelled = True
        self._cancel.set()
        if self.download is not None:
            self.download.cancel()

    def _target(self, resolution) -> str:
        root = _common_root([item.path for item in resolution.files])
        return root, self._dir

    @staticmethod
    def _merge_tree(source: str, target: str) -> None:
        for name in os.listdir(source):
            src = os.path.join(source, name)
            dst = os.path.join(target, name)
            if os.path.isdir(src):
                if os.path.exists(dst) and not os.path.isdir(dst):
                    os.remove(dst)
                os.makedirs(dst, exist_ok=True)
                _DebridJob._merge_tree(src, dst)
                continue
            if os.path.isdir(dst):
                shutil.rmtree(dst)
            os.replace(src, dst)

    @staticmethod
    def _commit_archive(source: str, target: str) -> None:
        entries = [os.path.join(source, name) for name in os.listdir(source)]
        if len(entries) == 1 and os.path.isdir(entries[0]):
            entries = [os.path.join(entries[0], name) for name in os.listdir(entries[0])]
        os.makedirs(target, exist_ok=True)
        for entry in entries:
            destination = os.path.join(target, os.path.basename(entry))
            if os.path.isdir(entry):
                if os.path.exists(destination) and not os.path.isdir(destination):
                    os.remove(destination)
                os.makedirs(destination, exist_ok=True)
                _DebridJob._merge_tree(entry, destination)
                continue
            if os.path.isdir(destination):
                shutil.rmtree(destination)
            os.replace(entry, destination)

    def _download_archives(self, service: DebridService, source: str, resolution, folder: str,
                           total: int, done: int) -> int:
        work = os.path.join(folder, ".pirachest-debrid")
        shutil.rmtree(work, ignore_errors=True)
        os.makedirs(work, exist_ok=True)
        try:
            for link in resolution.links:
                if self.cancelled:
                    raise Cancelled()
                name = os.path.basename(str(link.path or "").replace("\\", "/").rstrip("/"))
                if not name:
                    name = os.path.basename(urlsplit(link.url).path) or "archive"
                if not os.path.splitext(name)[1]:
                    name += os.path.splitext(urlsplit(link.url).path)[1] or ".rar"
                path = os.path.join(work, _safe_relative(name))
                os.makedirs(os.path.dirname(path) or work, exist_ok=True)
                expected = int(link.size or 0)
                retried = False
                while True:
                    if self.cancelled:
                        raise Cancelled()
                    try:
                        self.download = SegmentedDownload(
                            link.url, path, connections=self._connections, limiter=self._limiter,
                            on_progress=lambda n, size, speed, base=done: self._signals.progress.emit(
                                self.item_id, base + n, max(total, base + size), speed / 1024.0),
                        )
                        self.download.run()
                        break
                    except Cancelled:
                        raise
                    except Exception:
                        if retried or self.cancelled:
                            raise
                        retried = True
                        refreshed = service.resolve(source, file_ids=self._file_ids, cancel=self._cancel.is_set)
                        if not refreshed.archive or not refreshed.links:
                            raise
                        link = refreshed.links[0]
                        self.download = None
                done += int(self.download.total or expected or 0)
                self.download = None
                self._signals.progress.emit(self.item_id, done, max(total, done), 0.0)
                extract_dir = os.path.join(work, "extracted")
                shutil.rmtree(extract_dir, ignore_errors=True)
                try:
                    extracted = extract_archive(path, extract_dir, cancel=self._cancel.is_set)
                except InterruptedError:
                    raise Cancelled()
                except ArchiveError as exc:
                    raise RuntimeError(
                        f"Could not extract {name or 'the debrid archive'}: {exc}"
                    ) from exc
                if not extracted:
                    raise RuntimeError(f"The debrid archive {name or ''} contained no files".strip())
                self._commit_archive(extract_dir, folder)
                try:
                    os.remove(path)
                except OSError:
                    logger.debug("Could not remove the debrid archive %s", path, exc_info=True)
                self._signals.progress.emit(self.item_id, done, max(total, done), 0.0)
        finally:
            shutil.rmtree(work, ignore_errors=True)
        return done

    def run(self) -> None:
        service = None
        try:
            service = DebridService.from_settings(self._provider)
            source = self._source
            if (self._resolver is not None and source
                    and not source.startswith(("magnet:", "http://", "https://"))
                    and not os.path.isfile(source)):
                source = self._resolver(source)
            resolution = service.resolve(source, file_ids=self._file_ids, cancel=self._cancel.is_set)
            if self.cancelled:
                return
            root, folder = self._target(resolution)
            os.makedirs(folder, exist_ok=True)
            self._signals.started.emit(self.item_id, os.path.basename(folder), folder)
            if resolution.archive:
                total = sum(int(link.size or 0) for link in resolution.links)
                self._download_archives(service, source, resolution, folder, total, 0)
            else:
                total = sum(int(link.size or item.size or 0) for link, item in zip(resolution.links, resolution.files))
                done = 0
                for position, (link, entry) in enumerate(zip(resolution.links, resolution.files)):
                    if self.cancelled:
                        return
                    relative = entry.path.replace("\\", "/").lstrip("/")
                    if root and relative.startswith(root + "/"):
                        relative = relative[len(root) + 1:]
                    path = os.path.join(folder, _safe_relative(relative or link.path))
                    expected = int(link.size or entry.size or 0)
                    if expected and os.path.isfile(path) and os.path.getsize(path) == expected:
                        done += expected
                        self._signals.progress.emit(self.item_id, done, total, 0.0)
                        continue
                    os.makedirs(os.path.dirname(path) or folder, exist_ok=True)
                    retried = False
                    while True:
                        if self.cancelled:
                            return
                        try:
                            self.download = SegmentedDownload(
                                link.url, path, connections=self._connections, limiter=self._limiter,
                                on_progress=lambda n, size, speed, base=done: self._signals.progress.emit(
                                    self.item_id, base + n, total, speed / 1024.0),
                            )
                            self.download.run()
                            break
                        except Cancelled:
                            return
                        except Exception:
                            if retried or self.cancelled:
                                raise
                            retried = True
                            refreshed = service.resolve(source, file_ids=[entry.index], cancel=self._cancel.is_set)
                            match = next((item for item in refreshed.links if item), None)
                            if match is None:
                                raise
                            link = match
                            self.download = None
                    done += int(self.download.total or expected or 0)
                    self.download = None
                    self._signals.progress.emit(self.item_id, done, total, 0.0)
            if bool(getattr(settings, "debrid_delete_after", False)):
                try:
                    service.cleanup(resolution)
                except Exception:
                    logger.warning("Could not delete the debrid transfer after the download", exc_info=True)
        except Cancelled:
            return
        except Exception as exc:
            if not self.cancelled:
                self._signals.failed.emit(self.item_id, str(exc))
            return
        finally:
            if service is not None:
                service.close()
        if not self.cancelled:
            self._signals.finished.emit(self.item_id)


class DownloadManager(QObject):
    item_added = Signal(str)
    item_updated = Signal(str)
    item_removed = Signal(str)
    stats_changed = Signal()
    order_changed = Signal()
    all_downloads_finished = Signal()
    download_completed = Signal(str, str)
    _wake = Signal()
    _external_op = Signal(object)

    def __init__(self, parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self.global_down_kbps = int(getattr(settings, "speed_limit", 0))
        self.global_up_kbps = int(getattr(settings, "upload_speed_limit", 0))
        self._ses = None
        self._closed = False
        self._lock = threading.Lock()
        self._items: dict[str, DownloadItem] = {}
        self._order: list[str] = []
        self._handles: dict[str, Any] = {}
        self._ids: dict[Any, str] = {}
        self._sel: dict[str, list[int]] = {}
        self._closing: set[str] = set()
        self._jobs: dict[str, _HttpJob] = {}
        self._external: dict[str, tuple[Callable, Optional[Callable]]] = {}
        self._notified: set[str] = set()
        self._last_emit: dict[str, float] = {}
        self._idle: dict[str, float] = {}
        self._sig: dict[str, tuple] = {}
        self._stall_stage: dict[str, int] = {}
        self._peer_fp = _peer_fingerprint()
        self._meta: dict[str, Any] = {}
        self._probes: dict[str, Any] = {}
        self._locks: dict[str, threading.Lock] = {}
        self._stats = {"dht_nodes": 0, "incoming": False}
        self._cap, self._best, self._peak = 0, 0.0, 0.0
        self._tick, self._was_working = 0, False
        self._limiter = RateLimiter(self.global_down_kbps * 1024)
        self._pool = QThreadPool(self)
        self._pool.setMaxThreadCount(16)
        self._http = _HttpSignals(self)
        self._http.started.connect(self._on_http_started)
        self._http.progress.connect(self._on_http_progress)
        self._http.finished.connect(self._finish_http)
        self._http.failed.connect(self._fail)
        self._wake.connect(self._drain, Qt.ConnectionType.QueuedConnection)
        self._external_op.connect(self._run_external_op, Qt.ConnectionType.QueuedConnection)
        self._save_timer = QTimer(self, singleShot=True, interval=500)
        self._save_timer.timeout.connect(self._write_state)
        os.makedirs(_RESUME_DIR, exist_ok=True)
        os.makedirs(paths.torrent_cache, exist_ok=True)
        self._load_net()
        self._load_state()
        self._timer = QTimer(self, interval=1000)
        self._timer.timeout.connect(self._poll)
        self._timer.start()
        self._fast = QTimer(self, interval=250)
        self._fast.timeout.connect(self._poll_live)
        self._try_start_next()


    def _bt(self):
        with self._lock:
            if self._ses is None:
                if lt is None:
                    raise RuntimeError("python-libtorrent is not installed. Install it with 'pip install libtorrent'.")
                if msg := NET.leak_guard():
                    raise RuntimeError(msg)
                try:
                    with open(_DHT_FILE, "rb") as fh:
                        params = lt.read_session_params(fh.read(), lt.save_state_flags_t.save_dht_state)
                except Exception:
                    params = lt.session_params()
                params.settings = {**self._perf_pack(), **self._net_pack()}
                self._ses = lt.session(params)
                self._ses.set_alert_notify(self._wake.emit)
        return self._ses

    def _perf_pack(self) -> dict[str, Any]:
        cat = lt.alert.category_t
        return {
            "dht_bootstrap_nodes": _DHT_NODES,
            "download_rate_limit": self.global_down_kbps * 1024,
            "upload_rate_limit": self.global_up_kbps * 1024,
            "connections_limit": max(500, libtorrent_defaults.max_connections_per_torrent * 2),
            **_TUNED_SESSION_SETTINGS,
            "max_webtorrent_offers": 0,
            "active_downloads": -1, "active_seeds": -1, "active_limit": -1, "active_checking": 2,
            "announce_to_all_trackers": True, "announce_to_all_tiers": True,
            "mixed_mode_algorithm": int(lt.bandwidth_mixed_algo_t.prefer_tcp),
            "choking_algorithm": int(lt.choking_algorithm_t.rate_based_choker),
            "seed_choking_algorithm": int(lt.seed_choking_algorithm_t.fastest_upload),
            "unchoke_slots_limit": -1,
            "max_out_request_queue": 1500, "max_allowed_in_request_queue": 2000,
            "suggest_mode": int(lt.suggest_mode_t.suggest_read_cache),
            "allow_multiple_connections_per_ip": True, "rate_limit_ip_overhead": True,
            "hashing_threads": 4, "file_pool_size": 200,
            "alert_mask": int(cat.error_notification | cat.status_notification | cat.storage_notification | cat.stats_notification),
        }

    def _net_pack(self, *, session_start: bool = True) -> dict[str, Any]:
        quiet, forced, port = NET.anonymous or bool(NET.leak_guard()), NET.require_encryption, int(getattr(settings, "torrent_port", 6881))
        host = NET.bind.strip()
        host = f"[{host}]" if ":" in host else host
        enc = int(lt.enc_policy.forced if forced else lt.enc_policy.enabled)
        pack: dict[str, Any] = {
            "user_agent": "" if quiet else _UA, "anonymous_mode": NET.anonymous,
            "listen_interfaces": f"{host}:{port}" if host else f"0.0.0.0:{port},[::]:{port}",
            "outgoing_interfaces": NET.bind.strip(),
            "enable_dht": NET.dht and not quiet, "enable_lsd": NET.lsd and not quiet,
            "enable_upnp": NET.upnp and not quiet, "enable_natpmp": NET.upnp and not quiet,
            "out_enc_policy": enc, "in_enc_policy": enc, "allowed_enc_level": int(lt.enc_level.both),
            "prefer_rc4": forced,
            "proxy_type": int(lt.proxy_type_t.none), "force_proxy": False,
        }
        if session_start:

            pack["peer_fingerprint"] = self._peer_fp
        if NET.has_proxy:
            socks, auth = NET.proxy_type == "socks5", bool(NET.proxy_user)
            kind = (lt.proxy_type_t.socks5_pw if auth else lt.proxy_type_t.socks5) if socks else (lt.proxy_type_t.http_pw if auth else lt.proxy_type_t.http)
            pack.update({
                "proxy_type": int(kind), "proxy_hostname": NET.proxy_host, "proxy_port": NET.proxy_port,
                "proxy_username": NET.proxy_user, "proxy_password": NET.proxy_pass,
                "proxy_hostnames": True, "proxy_tracker_connections": True,
                "proxy_peer_connections": NET.proxy_peers and socks, "force_proxy": NET.force_proxy,
            })
        return pack

    def net_prefs(self) -> dict[str, Any]:
        return asdict(NET)

    def set_net_prefs(self, **values: Any) -> None:
        known = {f.name for f in fields(NET)}
        for key, value in values.items():
            if key in known and value is not None:
                setattr(NET, key, type(getattr(NET, key))(value))
        settings.net = asdict(NET)
        save_settings(settings)
        if self._ses is not None:
            self._ses.apply_settings(self._net_pack(session_start=False))
        if NET.leak_guard():
            self.pause_all()

    def _drain(self) -> None:
        if self._closed or self._ses is None:
            return
        for alert in self._ses.pop_alerts():
            if isinstance(alert, lt.session_stats_alert):
                v = alert.values
                self._stats = {"dht_nodes": v.get("dht.dht_nodes", 0), "incoming": bool(v.get("net.has_incoming_connections", 0))}
                continue
            if isinstance(alert, lt.listen_failed_alert):
                logger.warning("Cannot listen for incoming peers: %s", _scrub(alert.message()))
                continue
            handle = getattr(alert, "handle", None)
            iid = self._ids.get(handle) if handle is not None else None
            if iid is None:
                continue
            if isinstance(alert, lt.metadata_received_alert):
                self._on_metadata(iid, handle)
            elif isinstance(alert, lt.torrent_finished_alert):
                self._poll_one(iid, handle)
            elif isinstance(alert, lt.save_resume_data_alert):
                self._write_resume(iid, alert.params)
                if iid in self._closing:
                    self._release(iid, handle)
            elif isinstance(alert, lt.save_resume_data_failed_alert):
                if iid in self._closing:
                    self._release(iid, handle)
            elif isinstance(alert, lt.fastresume_rejected_alert):
                self._drop_resume(iid)
            elif isinstance(alert, (lt.torrent_error_alert, lt.file_error_alert, lt.metadata_failed_alert, lt.storage_moved_failed_alert)):
                self._fail(iid, alert.message())

    def _drop(self, handle, files: bool = False) -> None:
        try:
            self._ses.remove_torrent(handle, lt.session.delete_files if files else 0)
        except Exception:
            pass

    def _release(self, iid: str, handle) -> None:
        self._closing.discard(iid)
        self._ids.pop(handle, None)
        self._drop(handle)

    def _resume_path(self, iid: str) -> str:
        return os.path.join(_RESUME_DIR, iid + ".fastresume")

    def _write_resume(self, iid: str, params) -> None:
        try:
            with open(self._resume_path(iid), "wb") as fh:
                fh.write(lt.write_resume_data_buf(params))
        except OSError:
            logger.warning("Could not write resume data for %s", iid)

    def _drop_resume(self, iid: str) -> None:
        try:
            os.remove(self._resume_path(iid))
        except OSError:
            pass

    @staticmethod
    def _request_resume(handle) -> None:
        try:
            handle.save_resume_data(lt.save_resume_flags_t.flush_disk_cache | lt.save_resume_flags_t.save_info_dict | lt.save_resume_flags_t.only_if_modified)
        except Exception:
            pass


    def _new(self, start: bool = True, **fields_: Any) -> str:
        item = DownloadItem(id=fields_.pop("id", None) or uuid.uuid4().hex, **fields_)
        self._items[item.id] = item
        self._order.append(item.id)
        self.item_added.emit(item.id)
        if start:
            self._save_state()
            self._try_start_next()
        return item.id

    def _auto_debrid(self, category: str) -> bool:
        if not bool(getattr(settings, "debrid_enabled", False)) or not debrid_configured():
            return False
        if category == "repacks":
            return bool(getattr(settings, "debrid_auto_repacks", True))
        if category == "tbhb":
            return bool(getattr(settings, "debrid_auto_tbhb", True))
        if category == "tv":
            return bool(getattr(settings, "debrid_auto_tv", True))
        return bool(getattr(settings, "debrid_auto_direct", False))

    def add(self, torrent_file: str, file_id: int, game_name: str, console: str, source: str = "Minerva",
            file_ids: Optional[list[int]] = None, category: Optional[str] = None, *, backend: str = "",
            debrid_provider: str = "", info_hash: str = "") -> str:
        source = source or "Minerva"
        resolved_category = category or ("repacks" if _is_remote_source(torrent_file) else "minerva")
        chosen = backend.strip().lower() or ("debrid" if self._auto_debrid(resolved_category) else "torrent")
        if chosen not in ("torrent", "debrid"):
            chosen = "torrent"
        return self._new(
            torrent_file=torrent_file, file_id=int(file_id or 1), file_ids=sorted({int(f) for f in file_ids or ()}),
            game_name=game_name or "Unknown", console=console or "", source=source, category=resolved_category,
            backend=chosen, debrid_provider=debrid_provider or (getattr(settings, "debrid_provider", "") if chosen == "debrid" else ""),
            info_hash=info_hash,
        )

    def add_http(self, url: str, title: str = "", file_name: str = "", source: str = "Direct", category: str = "direct") -> str:
        tail = unquote(os.path.basename(urlsplit(url).path)) or "download"
        return self._new(backend="http", url=url, game_name=title or to_safe_name(tail), file_name=to_safe_name(file_name) if file_name else "",
                         source=source, category=category)

    def add_external(self, *, game_name: str, console: str = "", source: str = "", category: str = "music", item_id: Optional[str] = None, total_bytes: int = 0) -> str:
        return self._new(id=item_id, game_name=game_name or "Unknown", console=console or "", source=source or category.title(),
                         backend="external", category=category, state=DLState.downloading, total_bytes=total_bytes)

    def _dispatch_external(self, fn: Callable) -> None:
        if self._closed:
            return
        if QThread.currentThread() == self.thread():
            fn()
        else:
            self._external_op.emit(fn)

    def _run_external_op(self, fn: Callable) -> None:
        if not self._closed:
            fn()

    def register_external_cancel(self, item_id: str, callback: Callable, retry: Optional[Callable] = None) -> None:
        self._dispatch_external(lambda: self._external.__setitem__(item_id, (callback, retry)))

    def _ext(self, item_id: str) -> Optional[DownloadItem]:
        item = self._items.get(item_id)
        return item if item is not None and item.backend == "external" else None

    def update_external(self, item_id: str, *, downloaded_bytes: Optional[int] = None, total_bytes: Optional[int] = None, progress: Optional[float] = None, speed_down_kbps: Optional[float] = None) -> None:
        self._dispatch_external(lambda: self._update_external(
            item_id, downloaded_bytes=downloaded_bytes, total_bytes=total_bytes,
            progress=progress, speed_down_kbps=speed_down_kbps))

    def _update_external(self, item_id: str, *, downloaded_bytes: Optional[int], total_bytes: Optional[int], progress: Optional[float], speed_down_kbps: Optional[float]) -> None:
        if (item := self._ext(item_id)) is None:
            return
        if downloaded_bytes is not None:
            item.downloaded_bytes = downloaded_bytes
        if total_bytes is not None:
            item.total_bytes = total_bytes
        if progress is not None:
            item.progress = max(0.0, min(100.0, progress))
        elif item.total_bytes:
            item.progress = min(100.0, item.downloaded_bytes / item.total_bytes * 100)
        if speed_down_kbps is not None:
            item.speed_down_kbps = speed_down_kbps
        self._emit_throttled(item_id)

    def complete_external(self, item_id: str, download_path: str = "") -> None:
        self._dispatch_external(lambda: self._complete_external(item_id, download_path))

    def _complete_external(self, item_id: str, download_path: str) -> None:
        if (item := self._ext(item_id)) is not None:
            self._finish(item, download_path)

    def fail_external(self, item_id: str, error: str) -> None:
        self._dispatch_external(lambda: self._fail(item_id, error))

    def can_retry(self, item_id: str) -> bool:
        item = self._items.get(item_id)
        return item is not None and (item.backend != "external" or bool(self._external.get(item_id, (None, None))[1]))

    def _emit_throttled(self, item_id: str) -> None:
        now = time.monotonic()
        if now - self._last_emit.get(item_id, 0.0) >= 0.5:
            self._last_emit[item_id] = now
            self.item_updated.emit(item_id)


    def items_in_order(self) -> list[DownloadItem]:
        return [self._items[i] for i in self._order if i in self._items]

    def get(self, item_id: str) -> Optional[DownloadItem]:
        return self._items.get(item_id)

    def summary(self) -> dict[str, Any]:
        items = list(self._items.values())
        count = lambda *states: sum(i.state in states for i in items)
        return {
            "active": count(*_RUNNING), "queued": count(DLState.queued), "completed": count(DLState.completed),
            "total_down": human_speed(sum(i.speed_down_kbps for i in items if i.state in _RUNNING)),
            "total_up": human_speed(sum(i.speed_up_kbps for i in items if i.state in (*_RUNNING, DLState.seeding))),
        }

    def network_diagnostics(self) -> dict[str, Any]:
        return {
            "dht_nodes": self._stats["dht_nodes"], "has_incoming_connections": self._stats["incoming"],
            "listen_port": self._ses.listen_port() if self._ses is not None else 0,
            "dht_enabled": NET.dht and not NET.anonymous,
        }


    def reorder(self, new_order_ids: list[str]) -> None:
        order = [i for i in new_order_ids if i in self._items]
        self._order = order + [i for i in self._order if i not in order]
        for rank, iid in enumerate(self._order):
            if iid in self._items:
                self._items[iid].priority = len(self._order) - rank
        self.order_changed.emit()
        self._save_state()
        self._try_start_next()

    def _try_start_next(self) -> None:
        limit = max(1, int(getattr(settings, "max_active_downloads", 3)))
        running = sum(i.backend != "external" and i.state in _RUNNING for i in self._items.values())
        queued = sorted((i for i in self.items_in_order() if i.state == DLState.queued and i.backend != "external"), key=lambda i: i.category != "repacks")
        for item in queued:
            if item.state == DLState.queued and running < limit + (item.category == "repacks"):
                running += 1
                self._start(item)

    def _start(self, item: DownloadItem) -> None:
        item.state, item.error = DLState.downloading, ""
        if msg := NET.leak_guard():
            item.state, item.error = DLState.error, msg
            return self._changed(item.id)
        self.item_updated.emit(item.id)
        if item.backend == "http":
            job = _HttpJob(item, self._http, self._limiter, int(getattr(settings, "http_connections", 8)))
            self._jobs[item.id] = job
            self._pool.start(job)
            return
        if item.backend == "debrid":
            job = _DebridJob(item, self._http, self._limiter, int(getattr(settings, "http_connections", 8)),
                             resolver=self._resolve_torrent)
            self._jobs[item.id] = job
            self._pool.start(job)
            return
        submit(self._resolve_torrent, args=(item.torrent_file,), owner=self, priority=Priority.HIGH, pool=DL,
               on_done=lambda src, _id=item.id: self._add_handle(_id, src), on_error=lambda exc, _id=item.id: self._fail(_id, str(exc)))

    def pause(self, item_id: str) -> None:
        item = self._items.get(item_id)
        if item is None or item.backend == "external" or item.state not in (*_LIVE, DLState.seeding):
            return
        if (handle := self._handles.get(item_id)) is not None:
            handle.pause()
            self._request_resume(handle)
        if (job := self._jobs.pop(item_id, None)) is not None:
            job.cancel()
        item.state = DLState.paused
        item.clear_live()
        self._changed(item_id)

    def resume(self, item_id: str) -> None:
        item = self._items.get(item_id)
        if item is None or item.backend == "external" or item.state not in (DLState.paused, DLState.completed):
            return
        handle = self._handles.get(item_id)
        if handle is not None and item.state == DLState.paused:
            handle.resume()
            item.state = DLState.seeding if handle.status().is_finished else DLState.downloading
        elif item.backend == "torrent" or item.state == DLState.paused:
            item.state = DLState.queued
        else:
            return
        self._changed(item_id)
        self._try_start_next()

    def retry(self, item_id: str) -> None:
        item = self._items.get(item_id)
        if item is None or not self.can_retry(item_id):
            return
        self._stop(item_id)
        item.error = ""
        item.clear_live()
        if item.backend == "external":
            item.state, item.progress, item.downloaded_bytes = DLState.downloading, 0.0, 0
            self._external[item_id][1]()
        else:
            item.state = DLState.queued
            if item.backend == "torrent":
                item.progress, item.downloaded_bytes = 0.0, 0
                self._drop_resume(item_id)
            elif item.backend == "debrid":
                item.progress, item.downloaded_bytes = 0.0, 0
                item.total_bytes = 0
        self._changed(item_id)
        self._try_start_next()

    def cancel(self, item_id: str, delete_files: bool = False) -> None:
        item = self._items.get(item_id)
        if item is None:
            return
        if (callback := self._external.pop(item_id, (None, None))[0]) is not None:
            callback()
        self._stop(item_id, delete_files)
        if delete_files:
            self._discard(item_id)
        else:
            item.state = DLState.cancelled
            item.clear_live()
        self._changed(item_id)
        self._try_start_next()

    def remove(self, item_id: str, delete_files: bool = False) -> None:
        item = self._items.get(item_id)
        if item is None:
            return
        callback = self._external.pop(item_id, (None, None))[0]
        if callback is not None and item.state in _RUNNING:
            callback()
        self._stop(item_id, delete_files)
        self._discard(item_id)
        self._changed(item_id)
        self._try_start_next()

    def pause_all(self) -> None:
        for iid in [i for i, it in self._items.items() if it.state in (*_LIVE, DLState.seeding)]:
            self.pause(iid)

    def resume_all(self) -> None:
        for iid in [i for i, it in self._items.items() if it.state == DLState.paused]:
            self.resume(iid)

    def remove_all(self, delete_files: bool = False) -> None:
        for iid in list(self._order):
            self.remove(iid, delete_files)

    def force_recheck(self, item_id: str) -> None:
        handle, item = self._handles.get(item_id), self._items.get(item_id)
        if handle is not None and item is not None:
            item.state = DLState.verifying
            handle.force_recheck()
            self.item_updated.emit(item_id)

    def set_torrent_settings(self, item_id: str, **values: Any) -> None:
        item = self._items.get(item_id)
        if item is None:
            return
        for key, value in values.items():
            if key in _SETTING_KEYS and value is not None:
                setattr(item, key, value)
        if (handle := self._handles.get(item_id)) is not None:
            self._apply_limits(handle, item)
        self._changed(item_id)

    def set_global_limits(self, down_kbps: int, up_kbps: int) -> None:
        self.global_down_kbps, self.global_up_kbps = int(down_kbps), int(up_kbps)
        self._limiter.set_rate(self.global_down_kbps * 1024)
        self._cap = 0
        if self._ses is not None:
            self._ses.apply_settings({"download_rate_limit": self.global_down_kbps * 1024, "upload_rate_limit": self.global_up_kbps * 1024})

    def _changed(self, item_id: str) -> None:
        if item_id in self._items:
            self.item_updated.emit(item_id)
        self.stats_changed.emit()
        self._save_state()

    def _stop(self, item_id: str, delete_files: bool = False) -> None:
        if (job := self._jobs.pop(item_id, None)) is not None:
            job.cancel()
        handle = self._handles.pop(item_id, None)
        self._sync_fast_timer()
        self._sel.pop(item_id, None)
        self._idle.pop(item_id, None)
        self._stall_stage.pop(item_id, None)
        if handle is not None:
            self._ids.pop(handle, None)
            self._closing.discard(item_id)
            self._drop(handle, delete_files)
        if delete_files:
            self._drop_resume(item_id)
            if (item := self._items.get(item_id)) is not None:
                submit(self._delete_paths, args=(self._owned_paths(item),), owner=self, priority=Priority.NORMAL, pool=DL)

    def _discard(self, item_id: str) -> None:
        self._items.pop(item_id, None)
        if item_id in self._order:
            self._order.remove(item_id)
        self._external.pop(item_id, None)
        self._notified.discard(item_id)
        self._last_emit.pop(item_id, None)
        self._stall_stage.pop(item_id, None)
        self._sig.pop(item_id, None)
        self.item_removed.emit(item_id)

    def _fail(self, item_id: str, error: str) -> None:
        if self._closed:
            return
        item = self._items.get(item_id)
        if item is None or item.state in (DLState.cancelled, DLState.completed):
            return
        if (handle := self._handles.pop(item_id, None)) is not None:
            self._ids.pop(handle, None)
            self._drop(handle)
        self._sync_fast_timer()
        self._jobs.pop(item_id, None)
        self._idle.pop(item_id, None)
        self._stall_stage.pop(item_id, None)
        if (item.backend == "debrid" and item.torrent_file and not item.fallback_used
                and bool(getattr(settings, "debrid_fallback", True))):
            logger.warning("Debrid download failed, falling back to peer-to-peer: %s", _scrub(error))
            item.backend, item.fallback_used, item.error = "torrent", True, ""
            item.state, item.progress, item.downloaded_bytes, item.total_bytes = DLState.queued, 0.0, 0, 0
            item.clear_live()
            self._changed(item_id)
            self._try_start_next()
            return
        item.state, item.error = DLState.error, _scrub(error)
        item.clear_live()
        self._changed(item_id)
        self._try_start_next()

    def _finish(self, item: DownloadItem, path: str = "") -> None:
        if self._closed:
            return
        item.download_path = path or item.download_path
        self._extract_roms_async(item, [item.download_path])
        item.state, item.progress = DLState.completed, 100.0
        item.clear_live()
        self._jobs.pop(item.id, None)
        self._external.pop(item.id, None)
        self._changed(item.id)
        self._announce(item)
        self._try_start_next()

    def _extract_roms_async(self, item: DownloadItem, paths: list[str]) -> None:
        if item.category != "minerva" or not bool(getattr(settings, "minerva_extract_roms", False)):
            return
        paths = [path for path in paths if path]
        if paths:
            submit(extract_rom_archives, args=(paths,), owner=self, priority=Priority.LOW, pool=IO)

    def _announce(self, item: DownloadItem) -> None:
        if item.id not in self._notified:
            self._notified.add(item.id)
            self.download_completed.emit(item.id, item.game_name or "")


    def _on_http_started(self, iid: str, name: str, path: str) -> None:
        if self._closed:
            return
        if (item := self._items.get(iid)) is not None:
            item.file_name, item.download_path = name, path
            self.item_updated.emit(iid)
            self._save_state()

    def _on_http_progress(self, iid: str, done: int, total: int, kbps: float) -> None:
        if self._closed:
            return
        item = self._items.get(iid)
        if item is None or item.state != DLState.downloading:
            return
        item.downloaded_bytes, item.total_bytes, item.speed_down_kbps = done, total, kbps
        item.progress = min(100.0, done / total * 100) if total else 0.0
        item.eta = int((total - done) / (kbps * 1024)) if total and kbps > 0 else -1
        self._emit_throttled(iid)

    def _finish_http(self, iid: str) -> None:
        if (item := self._items.get(iid)) is not None:
            self._finish(item)


    def _add_handle(self, iid: str, source: str) -> None:
        item = self._items.get(iid)
        if item is None or item.state != DLState.downloading or iid in self._handles:
            return
        try:
            ses = self._bt()
            atp, resumed = self._params(item, source)
            sel = self._select(item, atp.ti) if atp.ti is not None else []
            if atp.ti is not None:
                old, keep = list(atp.file_priorities), set(sel)
                atp.file_priorities = [((old[i] if i < len(old) and old[i] else _NORMAL) if i in keep else 0) for i in range(atp.ti.num_files())]
                if not resumed:
                    atp.save_path = self._dest(item, atp.ti)
                _validate_ti(atp.ti, atp.save_path, sel)
            if (probe := self._probes.pop(item.torrent_file, None)) is not None:
                self._drop(probe)
            handle = ses.add_torrent(atp)
            if self._ids.get(handle, iid) != iid:
                raise RuntimeError("This torrent is already in the download queue")
        except Exception as exc:
            logger.exception("Failed to start torrent %s", iid)
            return self._fail(iid, str(exc))
        self._handles[iid], self._ids[handle], self._sel[iid] = handle, iid, sel
        self._sync_fast_timer()
        self._apply_limits(handle, item)
        if atp.ti is not None:
            item.total_bytes = self._measure(atp.ti, sel)
        self.item_updated.emit(iid)

    def _params(self, item: DownloadItem, source: str):
        atp = None
        try:
            with open(self._resume_path(item.id), "rb") as fh:
                atp = lt.read_resume_data(fh.read())
        except OSError:
            pass
        except Exception:
            self._drop_resume(item.id)
        resumed = atp is not None
        if atp is None:
            atp = self._atp(source, self._meta.get(source), _base_dir(item))
        else:
            self._prepare(atp)
        return atp, resumed

    def _atp(self, source: str, ti, save_path: str):
        if source.startswith("magnet:"):
            atp = lt.parse_magnet_uri(source)
            if ti is not None:
                atp.ti = ti
        else:
            atp = lt.add_torrent_params()
            atp.ti = ti or _load_torrent_info(source)
        atp.save_path = save_path
        self._prepare(atp)
        return atp

    @staticmethod
    def _prepare(atp) -> None:
        atp.storage_mode = lt.storage_mode_t.storage_mode_sparse
        atp.flags &= ~(lt.torrent_flags.auto_managed | lt.torrent_flags.paused)
        if atp.ti is None:
            atp.flags |= lt.torrent_flags.default_dont_download
        if not (atp.ti is not None and atp.ti.priv()):
            atp.trackers = [*atp.trackers, *(t for t in libtorrent_defaults.extra_trackers if t not in atp.trackers)]

    @staticmethod
    def _select(item: DownloadItem, ti) -> list[int]:
        n = ti.num_files()
        ids = item.file_ids or ([item.file_id] if item.category == "minerva" else range(1, n + 1))
        sel = sorted({i - 1 for i in ids})
        if not sel or sel[0] < 0 or sel[-1] >= n:
            raise RuntimeError(f"file index out of range ({n} files)")
        fs = ti.files()
        sel = [i for i in sel if not _file_flags(fs, i) & _F_LOCKED]
        if not sel:
            raise RuntimeError("Torrent has no downloadable files")
        return sel

    @staticmethod
    def _measure(ti, sel: list[int]) -> int:
        return sum(ti.files().file_size(i) for i in sel)

    @staticmethod
    def _dest(item: DownloadItem, ti) -> str:
        return _base_dir(item) if _root_folder(ti) else _game_dir(item)

    def _on_metadata(self, iid: str, handle) -> None:
        item, ti = self._items.get(iid), handle.torrent_file()
        if item is None or ti is None:
            return
        try:
            sel = self._select(item, ti)
            old, keep = list(handle.get_file_priorities()), set(sel)
            handle.prioritize_files([((old[i] if i < len(old) and old[i] else _NORMAL) if i in keep else 0) for i in range(ti.num_files())])
            dest = self._dest(item, ti)
            _validate_ti(ti, dest, sel)
            if os.path.normcase(os.path.abspath(handle.status().save_path)) != os.path.normcase(os.path.abspath(dest)):
                handle.move_storage(dest)
            self._sel[iid], item.total_bytes = sel, self._measure(ti, sel)
        except Exception as exc:
            self._fail(iid, str(exc))

    def _apply_limits(self, handle, item: DownloadItem) -> None:
        try:
            handle.set_download_limit(item.max_down_kbps * 1024)
            handle.set_upload_limit(item.max_up_kbps * 1024)
            handle.set_max_connections(item.max_peers or libtorrent_defaults.max_connections_per_torrent)
            handle.set_max_uploads(-1)
            handle.set_priority(min(255, item.priority + (128 if item.category == "repacks" else 0)))
        except Exception:
            pass

    def _poll(self) -> None:
        if self._closed:
            return
        self._sync_fast_timer()
        self._tick += 1
        self._drain()
        active = bool(self._handles or self._probes) or any(
            item.state in _LIVE for item in self._items.values())
        if not active:
            if self._was_working:
                self._was_working = False
                self.all_downloads_finished.emit()
            return
        if self._ses is not None and self._tick % 5 == 0:
            if self._handles or self._probes:
                self._ses.post_session_stats()
            live = [i for i in self._items.values() if i.state in (*_RUNNING, DLState.seeding)]
            self._adapt(sum(i.speed_down_kbps for i in live) * 1024, sum(i.speed_up_kbps for i in live) * 1024)
        if self._tick % 60 == 0:
            for handle in self._handles.values():
                self._request_resume(handle)
        if self._tick % 600 == 0 and self._ses is not None:
            self._save_dht()
        if self._tick % 30 == 0:
            self._prune_missing()
        if self._tick % 5 == 0:
            self._save_state()
        self.stats_changed.emit()
        self._try_start_next()
        working = any(i.state in _LIVE for i in self._items.values())
        if self._was_working and not working:
            self.all_downloads_finished.emit()
        self._was_working = working

    def _poll_live(self) -> None:
        if self._closed:
            return
        for iid, handle in list(self._handles.items()):
            self._poll_one(iid, handle)

    def _sync_fast_timer(self) -> None:
        if self._handles:
            if not self._fast.isActive():
                self._fast.start()
        elif self._fast.isActive():
            self._fast.stop()

    def _adapt(self, down: float, up: float) -> None:
        if self.global_up_kbps or down < 32 * 1024:
            self._best = self._peak = 0.0
            return self._set_cap(0)
        self._best, self._peak = max(down, self._best * 0.98), max(up, self._peak * 0.98)
        if down < 0.6 * self._best and up > 0.8 * (self._cap or self._peak):
            self._set_cap(max(up * 0.8, self._peak * 0.5, 32 * 1024))
        elif self._cap and up > 0.9 * self._cap:
            self._set_cap(self._cap * 1.15)

    def _set_cap(self, cap: float) -> None:
        if int(cap) != self._cap:
            self._cap = int(cap)
            self._ses.apply_settings({"upload_rate_limit": self._cap})

    def _poll_one(self, iid: str, handle) -> None:
        item = self._items.get(iid)
        if item is None or item.state == DLState.paused:
            return
        try:
            s = handle.status()
            self._totals(item, s)
            total = item.total_bytes
            item.speed_down_kbps, item.speed_up_kbps = s.download_rate / 1024.0, s.upload_rate / 1024.0
            item.peers, item.seeds = s.num_peers, s.num_seeds
            item.ratio = s.all_time_upload / max(s.all_time_download or item.downloaded_bytes, 1)
            item.seed_seconds = int(s.seeding_duration.total_seconds())
            if s.is_finished and total > 0 and item.state != DLState.seeding:
                self._on_finished(item, handle, s)
            elif item.state != DLState.seeding:
                item.state = DLState.verifying if s.state.name.startswith("checking") else DLState.downloading
                if item.state == DLState.downloading:
                    self._unstall(iid, handle, s.download_rate)
            if item.state == DLState.seeding and self._seed_done(item):
                self._stop_seeding(item, handle)
            sig = (item.state, item.downloaded_bytes, item.total_bytes, int(item.speed_down_kbps), int(item.speed_up_kbps),
                   item.peers, item.seeds, item.eta, int(item.ratio * 100), item.seed_seconds)
            if self._sig.get(iid) != sig:
                self._sig[iid] = sig
                self.item_updated.emit(iid)
        except Exception:
            logger.exception("Poll error for %s", iid)

    @staticmethod
    def _totals(item: DownloadItem, s) -> None:
        done, total = s.total_wanted_done, s.total_wanted or item.total_bytes
        item.downloaded_bytes, item.total_bytes = done, total
        item.progress = min(100.0, done / total * 100) if total else 0.0
        item.eta = int((total - done) / s.download_rate) if s.download_rate > 0 and total else -1

    def _unstall(self, iid: str, handle, rate: int) -> None:
        now = time.monotonic()
        if rate > 0:
            self._idle[iid] = now
            self._stall_stage.pop(iid, None)
            return
        last = self._idle.setdefault(iid, now)
        stalled = now - last
        stage = self._stall_stage.get(iid, 0)
        try:
            if stalled >= _STALL_REANNOUNCE and stage == 0:
                self._stall_stage[iid] = 1
                handle.force_reannounce()
                handle.force_dht_announce()
                logger.debug("Torrent %s stalled for %.0fs; reannouncing", iid, stalled)
            elif stalled >= _STALL_SCRAPE and stage == 1:
                self._stall_stage[iid] = 2
                handle.force_reannounce()
                handle.scrape_tracker()
                logger.debug("Torrent %s still stalled; scraping trackers", iid)
            elif stalled >= _STALL_RETRY and stage == 2:
                self._stall_stage[iid] = 3
                handle.force_reannounce()
                handle.force_dht_announce()
                logger.debug("Torrent %s still stalled; retrying peer discovery", iid)
            elif stalled >= _STALL_RETRY and stage >= 3:
                self._idle[iid] = now
                self._stall_stage[iid] = 1
                handle.force_reannounce()
                handle.force_dht_announce()
        except Exception:
            logger.debug("Stall recovery failed for %s", iid, exc_info=True)

    def _on_finished(self, item: DownloadItem, handle, s) -> None:
        ti, sel, root = handle.torrent_file(), self._sel.get(item.id) or [0], s.save_path
        top = _root_folder(ti)
        item.download_path = os.path.join(root, ti.files().file_path(sel[0])) if len(sel) == 1 else os.path.join(root, top) if top else root
        self._extract_roms_async(item, [os.path.join(root, ti.files().file_path(i)) for i in sel])
        item.progress = 100.0
        if item.seed_after and not self._seed_done(item):
            item.state = DLState.seeding
            item.speed_down_kbps = 0.0
            self._request_resume(handle)
            self._announce(item)
            self._save_state()
        else:
            self._stop_seeding(item, handle)
            self._announce(item)

    @staticmethod
    def _seed_done(item: DownloadItem) -> bool:
        return bool((item.ratio_limit and item.ratio >= item.ratio_limit) or (item.seed_time_limit_min and item.seed_seconds >= item.seed_time_limit_min * 60))

    def _stop_seeding(self, item: DownloadItem, handle) -> None:
        self._handles.pop(item.id, None)
        self._sync_fast_timer()
        self._sel.pop(item.id, None)
        self._closing.add(item.id)
        self._request_resume(handle)
        item.state = DLState.completed
        item.clear_live()
        QTimer.singleShot(3000, lambda: self._release(item.id, handle) if item.id in self._closing else None)


    def _owned_paths(self, item: DownloadItem) -> list[str]:
        roots = (category_dir(item.category), settings.download_dir, paths.download_root)
        out = [item.download_path] if item.download_path and any(_inside(r, item.download_path) for r in roots) else []
        if item.backend == "http" and item.download_path:
            out += [item.download_path + ".ghd", item.download_path + ".ghd.tmp"]
        if item.backend in ("torrent", "debrid") and not item.download_path and item.game_name:
            out.append(_game_dir(item))
        return out

    @staticmethod
    def _delete_paths(targets: list[str]) -> None:
        for path in targets:
            if _is_reparse(path):
                with suppress(OSError):
                    os.unlink(path) if not os.path.isdir(path) or os.path.islink(path) else os.rmdir(path)
                continue
            for _ in range(8):
                try:
                    shutil.rmtree(path) if os.path.isdir(path) else os.remove(path)
                    break
                except FileNotFoundError:
                    break
                except OSError:
                    time.sleep(0.3)

    def _expected_path(self, item: DownloadItem) -> Optional[str]:
        if item.download_path:
            return item.download_path
        return self._repack_folder(item) if item.category == "repacks" else _game_dir(item) if item.game_name else None

    def _prune_missing(self) -> None:
        done = [i for i in self.items_in_order() if i.state in (DLState.completed, DLState.seeding)]
        if done:
            submit(lambda: [i.id for i in done if (p := self._expected_path(i)) and not os.path.exists(p)], on_done=self._apply_prune,
                   priority=Priority.LOW, owner=self, key=("dl-prune", id(self)), pool=IO)

    def _apply_prune(self, missing: list[str]) -> None:
        for iid in missing:
            if (item := self._items.get(iid)) is not None and item.state in (DLState.completed, DLState.seeding):
                self.remove(iid)

    def _handle(self, iid: str):
        h = self._handles.get(iid)
        try:
            return h if h is not None and h.is_valid() and h.torrent_file() is not None else None
        except Exception:
            return None

    def file_tree(self, iid: str) -> Optional[list[tuple[str, int, int]]]:
        h = self._handle(iid)
        if h is None:
            return None
        fs = h.torrent_file().files()
        return [(fs.file_path(i).replace("\\", "/"), fs.file_size(i), _file_flags(fs, i)) for i in range(fs.num_files())]

    def file_state(self, iid: str) -> Optional[dict[str, Any]]:
        h, item = self._handle(iid), self._items.get(iid)
        if h is None or item is None:
            return None
        s = h.status()
        return {"progress": h.file_progress(), "prios": h.get_file_priorities(), "state": item.state,
                "stats": {"done": s.total_wanted_done, "wanted": s.total_wanted, "progress": item.progress,
                          "down": item.speed_down_kbps, "up": item.speed_up_kbps, "eta": item.eta, "ratio": item.ratio,
                          "seeds": s.num_seeds, "peers": s.num_peers, "list_seeds": s.list_seeds,
                          "list_peers": s.list_peers, "availability": s.distributed_copies}}

    def set_file_priorities(self, iid: str, changes: dict[int, int]) -> bool:
        item, h = self._items.get(iid), self._handle(iid)
        if item is None or h is None or not changes:
            return False
        try:
            ti = h.torrent_file()
            fs, prios = ti.files(), list(h.get_file_priorities())
            for i, p in changes.items():
                if 0 <= i < len(prios) and not _file_flags(fs, i) & _F_LOCKED:
                    prios[i] = min(_PRIOS, key=lambda c: abs(c - int(p)))
            sel = [i for i, p in enumerate(prios) if p and not _file_flags(fs, i) & _F_LOCKED]
            if not sel:
                return False
            _validate_ti(ti, h.status().save_path, sel)
            h.prioritize_files(prios)
            self._sel[iid], item.file_ids = sel, [i + 1 for i in sel]
            item.total_bytes = self._measure(ti, sel)
            st = h.status()
            self._totals(item, st)
            if item.state == DLState.seeding and not st.is_finished:
                item.state = DLState.downloading
                self._notified.discard(iid)
            self._request_resume(h)
            self._changed(iid)
            return True
        except Exception:
            logger.exception("Could not change file priorities for %s", iid)
            return False

    def file_path(self, iid: str, index: int) -> Optional[str]:
        h = self._handle(iid)
        if h is None:
            return None
        try:
            root = h.status().save_path
            path = os.path.join(root, h.torrent_file().files().file_path(index))
        except Exception:
            return None
        return path if _contained(root, path) and os.path.lexists(path) else None

    def details(self, iid: str) -> Optional[dict[str, Any]]:
        h, item = self._handle(iid), self._items.get(iid)
        if h is None or item is None:
            return None
        try:
            s, ti = h.status(), h.torrent_file()
            ih = s.info_hashes
            return {
                "active": _secs(s.active_duration), "down": s.all_time_download, "down_s": s.total_download,
                "up": s.all_time_upload, "up_s": s.total_upload, "down_rate": s.download_rate, "up_rate": s.upload_rate,
                "down_lim": item.max_down_kbps * 1024, "up_lim": item.max_up_kbps * 1024, "eta": item.eta, "ratio": item.ratio,
                "reannounce": _secs(s.next_announce), "conns": s.num_connections, "max_conns": item.max_peers,
                "seeds": s.num_seeds, "list_seeds": s.list_seeds, "peers": s.num_peers, "list_peers": s.list_peers,
                "wasted": s.total_failed_bytes + s.total_redundant_bytes, "seen": _secs(s.last_seen_complete),
                "size": ti.total_size(), "added": _secs(s.added_time), "completed": _secs(s.completed_time),
                "private": bool(ti.priv()), "hash": str(ih.v1) if ih.has_v1() else "", "save_path": s.save_path,
                "pieces": ti.num_pieces(), "piece_len": ti.piece_length(), "have": s.num_pieces,
                "creator": str(ti.creator())[:200], "comment": str(ti.comment())[:300], "created": _secs(ti.creation_date()),
            }
        except Exception:
            logger.debug("details failed for %s", iid, exc_info=True)
            return None

    def trackers(self, iid: str) -> Optional[list[tuple]]:
        h = self._handle(iid)
        if h is None:
            return None
        out = []
        try:
            for t in h.trackers()[:100]:
                ep = (t.get("endpoints") or [{}])[0]
                try:
                    u = urlsplit(t.get("url", ""))
                    host = f"{u.scheme}://{u.hostname}" + (f":{u.port}" if u.port else "")
                except ValueError:
                    host = "?"
                out.append((host, t.get("tier", 0), str(ep.get("message") or t.get("message") or "")[:120],
                            ep.get("scrape_complete", -1), ep.get("scrape_incomplete", -1)))
        except Exception:
            logger.debug("tracker list failed for %s", iid, exc_info=True)
        return out

    def peers(self, iid: str) -> Optional[list[tuple]]:
        h = self._handle(iid)
        if h is None:
            return None
        out = []
        try:
            for p in h.get_peer_info():
                c = p.client if isinstance(p.client, str) else bytes(p.client).decode("utf-8", "replace")
                out.append((f"{p.ip[0]}:{p.ip[1]}", c[:40], p.progress, p.down_speed, p.up_speed, p.total_download, p.total_upload))
        except Exception:
            logger.debug("peer list failed for %s", iid, exc_info=True)
        out.sort(key=lambda r: r[3] + r[4], reverse=True)
        return out[:300]

    def open_folder(self, item_id: str) -> Optional[str]:
        item = self._items.get(item_id)
        if item is None:
            return None
        target = item.download_path or _game_dir(item)
        folder = target if os.path.isdir(target) else os.path.dirname(target)
        return folder if folder and os.path.isdir(folder) else None

    def open_file(self, item_id: str) -> Optional[str]:
        item = self._items.get(item_id)
        return item.download_path if item and os.path.isfile(item.download_path) else None

    def find_repack_installer(self, item_id: str) -> Optional[str]:
        item = self._items.get(item_id)
        folder = self._repack_folder(item) if item and item.category == "repacks" else None
        for root, _dirs, files in os.walk(folder) if folder else ():
            for name in files:
                if name.lower() == "setup.exe":
                    return os.path.join(root, name)
        return None

    def _repack_folder(self, item: DownloadItem) -> Optional[str]:
        path = item.download_path
        if path and os.path.isdir(path):
            return path
        if path and os.path.isfile(path):
            return os.path.dirname(path)
        if os.path.isdir(exact := _game_dir(item)):
            return exact
        base, target = _base_dir(item), (item.game_name or "").strip().lower()
        if target and os.path.isdir(base):
            return next((os.path.join(base, n) for n in sorted(os.listdir(base)) if n.strip().lower().startswith(target) and os.path.isdir(os.path.join(base, n))), None)
        return None


    def _meta_for(self, source: str, timeout: float, cancel: Optional[Callable[[], bool]] = None):
        with self._locks.setdefault(source, threading.Lock()):
            if (ti := self._meta.get(source)) is not None:
                return ti
            resolved = self._resolve_torrent(source)
            if not resolved.startswith("magnet:"):
                return _load_torrent_info(resolved)
            handle = self._bt().add_torrent(self._atp(resolved, None, paths.cache_dir))
            self._probes[source] = handle
            start = time.monotonic()
            end = start + timeout
            try:
                while not handle.status().has_metadata:
                    if cancel and cancel():
                        raise InterruptedError("cancelled")
                    if time.monotonic() > end:
                        raise TimeoutError(f"Timed out waiting for torrent metadata after {timeout:.0f}s ({handle.status().num_peers} peers, {self._stats['dht_nodes']} DHT nodes)")
                    time.sleep(0.05)
                ti = handle.torrent_file()
                logger.info("Metadata for %.60s fetched in %.1fs (%d peers)", source, time.monotonic() - start, handle.status().num_peers)
            finally:
                if self._probes.get(source) is handle:
                    del self._probes[source]
                    self._drop(handle)
            if len(self._meta) >= _META_CACHE:
                self._meta.pop(next(iter(self._meta)))
            self._meta[source] = ti
            return ti

    def fetch_file_list(self, torrent_file: str, timeout_secs: float = 40.0, category: str = "repacks") -> list[tuple[str, int]]:
        try:
            f = self._meta_for(torrent_file, timeout_secs).files()
            return [(f.file_path(i), f.file_size(i)) for i in range(f.num_files())]
        except Exception:
            if not (bool(getattr(settings, "debrid_enabled", False)) and debrid_configured()):
                raise
            logger.info("Reading the file list through the debrid provider for %s", torrent_file[:120])
            service = DebridService.from_settings()
            try:
                return service.file_list(torrent_file, timeout=max(timeout_secs, 90.0))
            finally:
                service.close()

    def fetch_setup_exe(self, torrent_file: str, timeout_secs: float = 75.0, category: str = "repacks", should_cancel: Optional[Callable[[], bool]] = None, progress_cb: Optional[Callable[[int, int], None]] = None) -> Optional[str]:
        use_debrid = bool(getattr(settings, "debrid_enabled", False)) and debrid_configured()
        try:
            available, path = self._fetch_setup_exe_p2p(
                torrent_file, timeout_secs, category, should_cancel, progress_cb,
            )
        except InterruptedError:
            return None
        except Exception as exc:
            if not use_debrid:
                raise
            logger.warning("Peer-to-peer setup.exe pre-fetch failed, trying debrid: %s", exc)
            return self._fetch_setup_exe_debrid(
                torrent_file, category, should_cancel, progress_cb,
            )
        if path or not available:
            return path
        if should_cancel and should_cancel():
            return None
        if not use_debrid:
            return None
        logger.info("Peer-to-peer setup.exe pre-fetch did not finish, trying debrid")
        return self._fetch_setup_exe_debrid(torrent_file, category, should_cancel, progress_cb)

    def _fetch_setup_exe_p2p(self, torrent_file: str, timeout_secs: float, category: str,
                             should_cancel: Optional[Callable[[], bool]],
                             progress_cb: Optional[Callable[[int, int], None]]) -> tuple[bool, Optional[str]]:
        ti = self._meta_for(torrent_file, 40.0, should_cancel)
        f = ti.files()
        idx = next((i for i in range(f.num_files()) if os.path.basename(f.file_path(i)).lower() == "setup.exe"), None)
        if idx is None:
            return False, None
        save, size = category_dir(category), f.file_size(idx)
        atp = self._atp(self._resolve_torrent(torrent_file), ti, save)
        atp.file_priorities = [_TOP if i == idx else 0 for i in range(f.num_files())]
        handle = self._bt().add_torrent(atp)
        self._probes[torrent_file] = handle
        end = time.monotonic() + timeout_secs
        try:
            while (done := (handle.file_progress() or [0] * (idx + 1))[idx]) < size:
                if (should_cancel and should_cancel()) or not handle.is_valid():
                    return True, None
                if progress_cb:
                    progress_cb(done, size)
                if time.monotonic() > end:
                    raise TimeoutError(f"timed out pre-fetching setup.exe ({timeout_secs:.0f}s)")
                time.sleep(0.25)
            if progress_cb:
                progress_cb(size, size)
            handle.flush_cache()
        finally:
            if self._probes.get(torrent_file) is handle:
                del self._probes[torrent_file]
                self._drop(handle)
        full = os.path.join(save, f.file_path(idx))
        for _ in range(30):
            if os.path.isfile(full) and os.path.getsize(full) >= size:
                return True, full
            time.sleep(0.1)
        return True, None

    def _download_debrid_link(self, link, target: str,
                              should_cancel: Optional[Callable[[], bool]],
                              progress_cb: Optional[Callable[[int, int], None]]) -> bool:
        download = SegmentedDownload(
            link.url, target,
            connections=int(getattr(settings, "http_connections", 8)),
            limiter=self._limiter,
            on_progress=(lambda n, size, speed: progress_cb(n, size)) if progress_cb else None,
        )
        finished = threading.Event()

        def watch() -> None:
            while not finished.wait(0.25):
                if should_cancel and should_cancel():
                    download.cancel()
                    return

        watcher = threading.Thread(target=watch, daemon=True) if should_cancel else None
        if watcher is not None:
            watcher.start()
        try:
            download.run()
        except Cancelled:
            return False
        finally:
            finished.set()
        return not (should_cancel and should_cancel())

    def _extract_debrid_setup(self, link, entry, save: str,
                              should_cancel: Optional[Callable[[], bool]],
                              progress_cb: Optional[Callable[[int, int], None]]) -> Optional[str]:
        name = os.path.basename(str(link.path or "").replace("\\", "/").rstrip("/")) or "setup.rar"
        if not os.path.splitext(name)[1]:
            name += os.path.splitext(urlsplit(link.url).path)[1] or ".rar"
        work = os.path.join(save, f".pirachest-debrid-setup-{uuid.uuid4().hex}")
        try:
            os.makedirs(work, exist_ok=True)
            archive = os.path.join(work, _safe_relative(name))
            os.makedirs(os.path.dirname(archive) or work, exist_ok=True)
            if not self._download_debrid_link(link, archive, should_cancel, progress_cb):
                return None
            extract_dir = os.path.join(work, "extracted")
            try:
                extract_archive(archive, extract_dir, cancel=should_cancel)
            except InterruptedError:
                return None
            except ArchiveError as exc:
                logger.info("Could not extract setup.exe from the debrid archive: %s", exc)
                return None
            found = None
            for base, _dirs, names in os.walk(extract_dir):
                match = next((name for name in names if name.lower() == "setup.exe"), None)
                if match:
                    found = os.path.join(base, match)
                    break
            if found is None:
                logger.info("The debrid archive did not contain setup.exe")
                return None
            target = os.path.join(save, _safe_relative(entry.path or "setup.exe"))
            os.makedirs(os.path.dirname(target) or save, exist_ok=True)
            shutil.copy2(found, target)
            return target
        finally:
            shutil.rmtree(work, ignore_errors=True)

    def _fetch_setup_exe_debrid(self, torrent_file: str, category: str,
                                should_cancel: Optional[Callable[[], bool]],
                                progress_cb: Optional[Callable[[int, int], None]]) -> Optional[str]:
        if should_cancel and should_cancel():
            return None
        source = torrent_file
        if not _is_remote_source(source) and not os.path.isfile(source):
            source = self._resolve_torrent(source)
        service = DebridService.from_settings()
        try:
            files = service.files(source, cancel=should_cancel, wait=60.0)
            entry = next((item for item in files if os.path.basename(item.path).lower() == "setup.exe"), None)
            if entry is None:
                return None
            resolution = service.resolve(source, file_ids=[entry.index], cancel=should_cancel)
            if not resolution.links:
                return None
            save = category_dir(category)
            if not resolution.archive:
                link = resolution.links[0]
                target = os.path.join(save, _safe_relative(link.path or entry.path or "setup.exe"))
                os.makedirs(os.path.dirname(target) or save, exist_ok=True)
                return target if self._download_debrid_link(link, target, should_cancel, progress_cb) else None
            current = service.files(source, cancel=should_cancel)
            if any(item.selected and item.index != entry.index for item in current):
                logger.info("The debrid archive contains other selected files; skipping the setup.exe pre-fetch")
                return None
            return self._extract_debrid_setup(
                resolution.links[0], entry, save, should_cancel, progress_cb,
            )
        finally:
            service.close()

    def _resolve_torrent(self, name: str) -> str:
        import requests
        if name.startswith("magnet:"):
            return name
        if os.path.isfile(name):
            return os.path.abspath(name)
        cached = os.path.join(paths.torrent_cache, hashlib.sha1(name.encode()).hexdigest() + ".torrent" if name.startswith(("http://", "https://")) else name)
        if os.path.isfile(cached):
            return cached
        with new_session(1) as s:
            def pull(url: str) -> Optional[str]:
                r = s.get(url, timeout=network.torrent_download_timeout)
                if r.status_code == 404:
                    return None
                r.raise_for_status()
                with open(cached, "wb") as fh:
                    fh.write(r.content)
                return cached

            cdn = lambda n: pull(f"{network.cdn_base}/{quote(n, safe='')}")
            try:
                if name.startswith(("http://", "https://")):
                    if pull(name):
                        return cached
                else:
                    spaced = name.replace("_", " ")
                    variants = list(dict.fromkeys([name, name.replace(" ", "_"), spaced]))
                    if spaced.startswith("Minerva Myrient "):
                        variants.append("Minerva_Myrient" + spaced[len("Minerva Myrient"):])
                    if any(cdn(v) for v in variants):
                        return cached
                    listing = s.get(network.cdn_base + "/", timeout=network.torrent_download_timeout).text
                    close = difflib.get_close_matches(name, [unquote(n).rpartition("/")[2] for n in _TORRENT_HREF.findall(listing)], n=1, cutoff=0.4)
                    if close and cdn(close[0]):
                        return cached
            except requests.RequestException as exc:
                raise RuntimeError(f"Failed to fetch torrent {name}: {exc}") from exc
        raise RuntimeError(f"Could not locate '{name}' as a torrent.")


    def _save_state(self) -> None:
        if not self._save_timer.isActive():
            self._save_timer.start()

    def _write_state(self) -> None:
        items = [i for i in self.items_in_order() if not (NET.private_history and i.state in (DLState.completed, DLState.cancelled))]
        payload = json.dumps({"order": self._order, "items": [i.to_persist_dict() for i in items]}, separators=(",", ":"))
        try:
            os.makedirs(os.path.dirname(_QUEUE_FILE), exist_ok=True)
            with open(_QUEUE_FILE + ".tmp", "w", encoding="utf-8") as fh:
                fh.write(payload)
            os.replace(_QUEUE_FILE + ".tmp", _QUEUE_FILE)
        except OSError:
            logger.exception("Failed to save download queue state")

    @staticmethod
    def _apply_net(values: dict) -> None:
        for f in fields(NET):
            if f.name in values:
                try:
                    setattr(NET, f.name, type(getattr(NET, f.name))(values[f.name]))
                except (TypeError, ValueError):
                    pass

    def _load_net(self) -> None:
        if not settings.net:
            try:
                with open(_QUEUE_FILE, encoding="utf-8") as fh:
                    settings.net = json.load(fh).get("net") or {}
            except (OSError, ValueError):
                return
            if settings.net:
                save_settings(settings)
        self._apply_net(settings.net)

    def _load_state(self) -> None:
        try:
            with open(_QUEUE_FILE, encoding="utf-8") as fh:
                stored = json.load(fh)
        except (OSError, ValueError):
            return
        for entry in stored.get("items", []):
            try:
                item = DownloadItem.from_persist_dict(entry)
            except (TypeError, ValueError):
                continue
            if item.backend == "torrent" and item.category == "repacks" and not _is_remote_source(item.torrent_file):
                item.category = "minerva"


            if item.state in (DLState.completed, DLState.seeding):
                self._notified.add(item.id)
            if item.backend == "external" and item.state in _RUNNING:
                item.state, item.error = DLState.error, "Interrupted by app restart"
            elif item.state in (*_RUNNING, DLState.seeding):
                item.state = DLState.queued if item.state != DLState.seeding or item.seed_after else DLState.completed
            self._items[item.id] = item
            self._order.append(item.id)
        for name in os.listdir(_RESUME_DIR):
            if name.removesuffix(".fastresume") not in self._items:
                try:
                    os.remove(os.path.join(_RESUME_DIR, name))
                except OSError:
                    pass

    def _save_dht(self) -> None:
        try:
            with open(_DHT_FILE, "wb") as fh:
                fh.write(lt.write_session_params_buf(self._ses.session_state(lt.save_state_flags_t.save_dht_state)))
        except Exception:
            logger.warning("Could not save DHT state")

    def shutdown(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._timer.stop()
        self._save_timer.stop()
        for cancel, _retry in list(self._external.values()):
            if cancel is None:
                continue
            try:
                cancel()
            except Exception:
                logger.debug('External download cancellation failed', exc_info=True)
        for job in self._jobs.values():
            job.cancel()
        self._pool.waitForDone(3000)
        if (ses := self._ses) is not None:
            ses.set_alert_notify(lambda: None)
            pending = len(self._handles)
            for handle in self._handles.values():
                self._request_resume(handle)
            end = time.monotonic() + 5
            while pending and time.monotonic() < end:
                ses.wait_for_alert(200)
                for alert in ses.pop_alerts():
                    if isinstance(alert, lt.save_resume_data_alert) and (iid := self._ids.get(alert.handle)):
                        self._write_resume(iid, alert.params)
                        pending -= 1
                    elif isinstance(alert, lt.save_resume_data_failed_alert):
                        pending -= 1
            self._save_dht()
            self._ses = None
        self._write_state()


def _benchmark_process_rss() -> int | None:
    try:
        import psutil
        return int(psutil.Process().memory_info().rss)
    except Exception:
        pass
    if os.name != "nt":
        return None
    try:
        import ctypes
        from ctypes import wintypes

        class _Counters(ctypes.Structure):
            _fields_ = [
                ("cb", wintypes.DWORD),
                ("PageFaultCount", wintypes.DWORD),
                ("PeakWorkingSetSize", ctypes.c_size_t),
                ("WorkingSetSize", ctypes.c_size_t),
                ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                ("PagefileUsage", ctypes.c_size_t),
                ("PeakPagefileUsage", ctypes.c_size_t),
            ]

        counters = _Counters()
        counters.cb = ctypes.sizeof(counters)
        get_memory = ctypes.windll.psapi.GetProcessMemoryInfo
        get_memory.argtypes = [wintypes.HANDLE, ctypes.POINTER(_Counters), wintypes.DWORD]
        get_memory.restype = wintypes.BOOL
        if get_memory(ctypes.windll.kernel32.GetCurrentProcess(), ctypes.byref(counters), counters.cb):
            return int(counters.WorkingSetSize)
    except Exception:
        pass
    return None


def _benchmark_params(mode: str, port: int) -> dict[str, Any]:
    pack = _TUNED_SESSION_SETTINGS if mode == "tuned" else _LEGACY_SESSION_SETTINGS
    return {
        "listen_interfaces": f"0.0.0.0:{port},[::]:{port}",
        "enable_dht": True,
        "enable_lsd": True,
        "enable_upnp": False,
        "enable_natpmp": False,
        "dht_bootstrap_nodes": _DHT_NODES,
        "user_agent": _UA,
        "peer_fingerprint": _peer_fingerprint(),
        "out_enc_policy": int(lt.enc_policy.enabled),
        "in_enc_policy": int(lt.enc_policy.enabled),
        "allowed_enc_level": int(lt.enc_level.both),
        "prefer_rc4": False,
        **pack,
    }


def _benchmark_torrent(source: str, save_path: str):
    if source.startswith("magnet:"):
        atp = lt.parse_magnet_uri(source)
    else:
        atp = lt.add_torrent_params()
        if source.startswith(("http://", "https://")):
            import tempfile
            import requests
            response = requests.get(source, timeout=60)
            response.raise_for_status()
            fd, path = tempfile.mkstemp(suffix=".torrent", prefix="pirachest-bench-")
            os.close(fd)
            try:
                with open(path, "wb") as fh:
                    fh.write(response.content)
                atp.ti = lt.torrent_info(path)
            finally:
                with suppress(OSError):
                    os.remove(path)
        else:
            atp.ti = lt.torrent_info(source)
    atp.save_path = save_path
    atp.flags &= ~(lt.torrent_flags.auto_managed | lt.torrent_flags.paused)
    return atp


def _benchmark_once(source: str, mode: str, seconds: float, port: int) -> dict[str, Any]:
    import tempfile
    if lt is None:
        raise RuntimeError("python-libtorrent is not installed")
    workdir = tempfile.mkdtemp(prefix="pirachest-bench-")
    params = lt.session_params()
    params.settings = _benchmark_params(mode, port)
    started = time.monotonic()
    session = lt.session(params)
    startup = time.monotonic() - started
    handle = session.add_torrent(_benchmark_torrent(source, workdir))
    cpu_start, wall_start = time.process_time(), time.monotonic()
    first_peer = first_byte = first_meaningful = recovery = None
    max_peers = max_rss = last_all = 0
    speeds: list[float] = []
    active_speeds: list[float] = []
    zero_since = None
    last_done = 0
    try:
        while (elapsed := time.monotonic() - started) < seconds:
            status = handle.status()
            peers = int(status.num_peers)
            max_peers = max(max_peers, peers)
            if first_peer is None and peers > 0:
                first_peer = elapsed
            done = int(status.total_wanted_done)
            last_all = max(last_all, int(status.total_done))
            if first_byte is None and (done > 0 or status.download_rate > 0):
                first_byte = elapsed
            if done > last_done:
                last_done = done
            speed = float(status.download_rate)
            speeds.append(speed)
            if speed >= 1024 * 1024 and first_meaningful is None:
                first_meaningful = elapsed
            if first_byte is not None and speed > 0:
                active_speeds.append(speed)
            if first_byte is not None and speed <= 0:
                if zero_since is None:
                    zero_since = elapsed
            elif zero_since is not None:
                gap = elapsed - zero_since
                recovery = gap if recovery is None else min(recovery, gap)
                zero_since = None
            rss = _benchmark_process_rss()
            if rss is not None:
                max_rss = max(max_rss, rss)
            time.sleep(0.5)
        session.remove_torrent(handle)
    finally:
        session.pause()
        with suppress(Exception):
            shutil.rmtree(workdir, ignore_errors=True)
    wall = max(time.monotonic() - wall_start, 0.001)
    cpu = (time.process_time() - cpu_start) / wall * 100.0
    peak = max(speeds, default=0.0)
    average = sum(speeds) / len(speeds) if speeds else 0.0
    active_average = sum(active_speeds) / len(active_speeds) if active_speeds else 0.0
    return {
        "mode": mode,
        "source": "magnet" if source.startswith("magnet:") else os.path.basename(source),
        "session_startup_s": round(startup, 3),
        "first_peer_s": round(first_peer, 2) if first_peer is not None else None,
        "first_byte_s": round(first_byte, 2) if first_byte is not None else None,
        "first_meaningful_speed_s": round(first_meaningful, 2) if first_meaningful is not None else None,
        "peak_kib_s": round(peak / 1024, 1),
        "average_kib_s": round(average / 1024, 1),
        "average_active_kib_s": round(active_average / 1024, 1),
        "max_peers": max_peers,
        "peer_connect_rate_per_s": round(max_peers / max(first_peer or wall, 1.0), 2),
        "cpu_percent": round(cpu, 1),
        "max_rss_mib": round(max_rss / (1024 * 1024), 1) if max_rss else None,
        "recovery_s": round(recovery, 2) if recovery is not None else None,
        "download_disk_mib": round(last_all / (1024 * 1024), 1),
        "downloaded_bytes": int(last_done),
        "sample_seconds": round(wall, 1),
    }


def _benchmark_cli(argv: list[str] | None = None) -> int:
    import argparse
    import json
    parser = argparse.ArgumentParser(
        description="Compare PiraChest libtorrent tuning on one torrent. "
                    "No GUI, no source code changes, and no torrent data is kept.")
    parser.add_argument("source", help="magnet URI, .torrent path, or http(s) torrent URL")
    parser.add_argument("--mode", choices=("tuned", "legacy", "both"), default="both")
    parser.add_argument("--seconds", type=float, default=60.0)
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--output", help="optional JSON output path")
    args = parser.parse_args(argv)
    modes = ("tuned", "legacy") if args.mode == "both" else (args.mode,)
    results = []
    try:
        for mode in modes:
            logger.info("Benchmarking %s settings for %.0fs", mode, args.seconds)
            result = _benchmark_once(args.source, mode, max(5.0, args.seconds), args.port)
            results.append(result)
            print(json.dumps(result, indent=2), flush=True)
    except Exception as exc:
        print(f"Benchmark failed: {exc}", file=sys.stderr)
        return 1
    if args.output:
        with open(args.output, "w", encoding="utf-8") as fh:
            json.dump({"results": results}, fh, indent=2)
    return 0


if __name__ == "__main__":
    raise SystemExit(_benchmark_cli())
