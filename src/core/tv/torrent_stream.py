from __future__ import annotations

import logging
import math
import os
import re
import secrets
import shutil
import socket
import threading
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Optional

from ..config import libtorrent_defaults, paths, settings
from ..downloader import get_libtorrent

logger = logging.getLogger(__name__)

lt = get_libtorrent()

_METADATA_TIMEOUT = 75.0
_HEAD_TIMEOUT = 150.0
_RANGE_TIMEOUT = 120.0
_STALL_TIMEOUT = 45.0
_LOOKAHEAD_PIECES = 96
_DEADLINE_STEP_MS = 80
_BOOTSTRAP = ("router.bittorrent.com:6881", "dht.transmissionbt.com:6881",
              "dht.libtorrent.org:25401", "router.utorrent.com:6881")

_VIDEO_EXTS = (".mp4", ".mkv", ".avi", ".m4v", ".mov", ".webm", ".ts", ".m2ts", ".wmv", ".flv", ".mpg", ".mpeg")
_JUNK_RE = re.compile(r"(?:^|[^a-z0-9])(sample|trailer|extras?|featurette|proof)(?:[^a-z0-9]|$)", re.I)
_CONTENT_TYPES = {
    ".mp4": "video/mp4", ".m4v": "video/mp4", ".mov": "video/quicktime",
    ".mkv": "video/x-matroska", ".webm": "video/webm", ".avi": "video/x-msvideo",
    ".ts": "video/mp2t", ".m2ts": "video/mp2t", ".wmv": "video/x-ms-wmv", ".flv": "video/x-flv",
}


class StreamError(RuntimeError):
    pass


@dataclass
class StreamSession:
    source: Any
    episode: Optional[tuple[int, int]] = None
    url: str = ""
    state: str = "starting"
    message: str = "Connecting to peers..."
    file_name: str = ""
    file_size: int = 0
    progress: float = 0.0
    rate: int = 0
    peers: int = 0
    error: str = ""
    stop_event: threading.Event = field(default_factory=threading.Event, repr=False)

    @property
    def done(self) -> bool:
        return self.state in ("error", "stopped")

    def snapshot(self) -> dict:
        return {
            "state": self.state, "message": self.message, "url": self.url, "error": self.error,
            "file_name": self.file_name, "file_size": self.file_size, "progress": self.progress,
            "rate": self.rate, "peers": self.peers,
        }


def _pick_port(preferred: int) -> int:
    for candidate in (int(preferred) + 1, 0):
        free = True
        for kind in (socket.SOCK_STREAM, socket.SOCK_DGRAM):
            probe = socket.socket(socket.AF_INET, kind)
            try:
                probe.bind(("0.0.0.0", candidate))
                if candidate == 0:
                    return probe.getsockname()[1]
            except OSError:
                free = False
            finally:
                probe.close()
            if not free:
                break
        if free:
            return candidate
    return 0


def _episode_tokens(season: int, episode: int) -> tuple[str, ...]:
    season, episode = int(season), int(episode)
    return (
        f"s{season:02d}e{episode:02d}", f"s{season}e{episode}", f"{season}x{episode:02d}",
        f"season {season} episode {episode}", f"e{episode:02d}", f"ep{episode}",
    )


def _pick_file(ti: Any, file_idx: Optional[int], episode: Optional[tuple[int, int]],
               filename_hint: str = "") -> tuple[int, str, int]:
    files = ti.files()
    count = ti.num_files()
    sizes = [int(files.file_size(i)) for i in range(count)]
    paths = [str(files.file_path(i)).replace("\\", "/") for i in range(count)]

    def usable(index: int) -> bool:
        name = paths[index].lower()
        return (name.endswith(_VIDEO_EXTS) and sizes[index] >= 2 * 1024 ** 2
                and not _JUNK_RE.search(os.path.basename(name)))

    if isinstance(file_idx, int) and 0 <= file_idx < count and paths[file_idx].lower().endswith(_VIDEO_EXTS):
        return file_idx, paths[file_idx], sizes[file_idx]

    candidates = [index for index in range(count) if usable(index)]
    if not candidates:
        candidates = [index for index in range(count) if sizes[index] > 0]
    if not candidates:
        raise StreamError("This torrent has no playable file.")

    if episode:
        tokens = _episode_tokens(episode[0], episode[1])
        matched = [index for index in candidates
                   if any(token in re.sub(r"[^a-z0-9]+", " ", paths[index].lower()) for token in tokens)]
        if matched:
            candidates = matched

    if filename_hint:
        wanted = os.path.basename(filename_hint).lower()
        exact = [index for index in candidates if os.path.basename(paths[index]).lower() == wanted]
        if exact:
            candidates = exact

    best = max(candidates, key=lambda index: sizes[index])
    return best, paths[best], sizes[best]


class _PieceRead:
    __slots__ = ("event", "buffer", "failed")

    def __init__(self) -> None:
        self.event = threading.Event()
        self.buffer = b""
        self.failed = False


class _AlertPump(threading.Thread):
    def __init__(self, session: Any, engine: "TorrentStreamer") -> None:
        super().__init__(name="torrent-stream-alerts", daemon=True)
        self._session = session
        self._engine = engine
        self._stop = threading.Event()

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        while not self._stop.is_set():
            try:
                self._session.wait_for_alert(250)
                alerts = self._session.pop_alerts()
            except Exception:
                return
            for alert in alerts:
                if isinstance(alert, lt.read_piece_alert):
                    self._engine.deliver_piece(int(alert.piece), bytes(alert.buffer),
                                               bool(alert.error.value()))


class _StreamHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "PiraChestStream"

    def log_message(self, *args: Any) -> None:
        pass

    @property
    def session(self) -> Optional[StreamSession]:
        return self.server.session

    @property
    def engine(self) -> "TorrentStreamer":
        return self.server.engine

    def _send_headers(self, status: int, length: int, extra: dict[str, str]) -> None:
        self.send_response(status)
        for key, value in extra.items():
            self.send_header(key, value)
        self.send_header("Content-Length", str(length))
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Connection", "close")
        self.end_headers()

    def do_HEAD(self) -> None:
        self._serve(head_only=True)

    def do_GET(self) -> None:
        self._serve()

    def _serve(self, head_only: bool = False) -> None:
        session = self.session
        if session is None or not self.path.startswith(f"/s/{self.server.token}"):
            self._send_headers(404, 0, {})
            return
        if session.state == "stopped":
            self._send_headers(410, 0, {})
            return
        size = session.file_size
        if size <= 0:
            self._send_headers(503, 0, {"Retry-After": "1"})
            return

        start, end, status = 0, size - 1, 200
        header = self.headers.get("Range") or ""
        if header.startswith("bytes="):
            match = re.match(r"(\d*)-(\d*)", header[len("bytes="):].strip())
            first, last = match.groups() if match else ("", "")
            if not first and not last:
                self._send_headers(416, 0, {"Content-Range": f"bytes */{size}"})
                return
            if first:
                start = int(first)
                end = int(last) if last else size - 1
            else:
                start = max(0, size - int(last))
            end = min(end, size - 1)
            if start > end or start >= size:
                self._send_headers(416, 0, {"Content-Range": f"bytes */{size}"})
                return
            status = 206

        extra = {"Content-Type": _CONTENT_TYPES.get(os.path.splitext(session.file_name)[1].lower(),
                                                    "application/octet-stream")}
        if status == 206:
            extra["Content-Range"] = f"bytes {start}-{end}/{size}"
        self._send_headers(status, end - start + 1, extra)
        if head_only:
            return
        try:
            self._pump(start, end)
        except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
            pass
        except StreamError as exc:
            logger.info("Stream read stopped: %s", exc)
            session.state, session.error = "error", str(exc)
        except Exception:
            logger.exception("Streaming request failed")

    def _pump(self, start: int, end: int) -> None:
        session = self.session
        total = end - start + 1
        sent = 0
        while sent < total:
            if session is None or session.stop_event.is_set():
                raise StreamError("Streaming was cancelled.")
            position = start + sent
            piece, offset_in_piece = self.engine.locate(position)
            if piece < 0:
                raise StreamError("The stream is no longer available.")
            self.engine.await_range(position, position, _RANGE_TIMEOUT)
            payload = self.engine.read_piece(piece, _RANGE_TIMEOUT)
            take = min(len(payload) - offset_in_piece, total - sent)
            if take <= 0:
                raise StreamError("The torrent file ended unexpectedly.")
            self.wfile.write(payload[offset_in_piece:offset_in_piece + take])
            sent += take


class TorrentStreamer:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._session: Optional[StreamSession] = None
        self._handle: Any = None
        self._ses: Any = None
        self._info: Optional[dict] = None
        self._root = ""
        self._token = ""
        self._port = 0
        self._server: Optional[ThreadingHTTPServer] = None
        self._thread: Optional[threading.Thread] = None
        self._pump: Optional[_AlertPump] = None
        self._pending_reads: dict[int, list[_PieceRead]] = {}
        self._progress_at = 0.0


    def play(self, source: Any, episode: Optional[tuple[int, int]] = None) -> StreamSession:
        if lt is None:
            raise StreamError("python-libtorrent is not installed.")
        self.stop()
        session = StreamSession(source=source, episode=episode)
        self._ensure_server()
        with self._lock:
            self._session = session
            self._token = secrets.token_urlsafe(16)
            self._server.token = self._token
            self._server.session = session
        session.url = f"http://127.0.0.1:{self._port}/s/{self._token}"
        self._thread = threading.Thread(target=self._run, args=(session,),
                                        name="torrent-stream", daemon=True)
        self._thread.start()
        return session

    def stop(self) -> None:
        with self._lock:
            session, handle, ses, root = self._session, self._handle, self._ses, self._root
            self._session, self._handle, self._info, self._root = None, None, None, ""
            self._pending_reads.clear()
        if session is None:
            return
        session.stop_event.set()
        session.state, session.message = "stopped", ""
        if self._thread is not None and self._thread.is_alive() and self._thread is not threading.current_thread():
            self._thread.join(timeout=5)
        if handle is not None and ses is not None:
            try:
                ses.remove_torrent(handle, lt.options_t.delete_files)
            except Exception:
                logger.debug("Could not remove the stream torrent", exc_info=True)
        for attempt in range(4):
            if not root or not os.path.isdir(root):
                break
            shutil.rmtree(root, ignore_errors=True)
            if os.path.isdir(root):
                time.sleep(0.25 * (attempt + 1))
        self._thread = None

    def status(self) -> Optional[dict]:
        session = self._session
        if session is None:
            return None
        if session.state in ("metadata", "buffering", "ready"):
            self._refresh_stats(session)
        return session.snapshot()

    def shutdown(self) -> None:
        self.stop()
        with self._lock:
            server, ses, pump = self._server, self._ses, self._pump
            self._server, self._ses, self._pump = None, None, None
        if pump is not None:
            pump.stop()
        if server is not None:
            server.shutdown()
            server.server_close()
        if ses is not None:
            try:
                ses.pause()
            except Exception:
                pass


    def locate(self, position: int) -> tuple[int, int]:
        with self._lock:
            info = self._info
        if info is None or position < 0 or position >= info["size"]:
            return -1, 0
        torrent_position = info["offset"] + position
        return torrent_position // info["piece_length"], torrent_position % info["piece_length"]

    def deliver_piece(self, index: int, buffer: bytes, failed: bool) -> None:
        with self._lock:
            waiters = self._pending_reads.pop(index, [])
        for waiter in waiters:
            waiter.buffer, waiter.failed = buffer, failed
            waiter.event.set()

    def read_piece(self, index: int, timeout: float) -> bytes:
        with self._lock:
            handle = self._handle
        if handle is None:
            raise StreamError("The stream is no longer available.")
        waiter = _PieceRead()
        with self._lock:
            self._pending_reads.setdefault(index, []).append(waiter)
        try:
            handle.read_piece(index)
        except Exception as exc:
            with self._lock:
                pending = self._pending_reads.get(index)
                if pending and waiter in pending:
                    pending.remove(waiter)
            raise StreamError(f"Could not read the video data: {exc}") from exc
        if not waiter.event.wait(timeout):
            with self._lock:
                pending = self._pending_reads.get(index)
                if pending and waiter in pending:
                    pending.remove(waiter)
            raise StreamError("Timed out waiting for data - try another source.")
        if waiter.failed or not waiter.buffer:
            raise StreamError("The video data could not be read - try another source.")
        return waiter.buffer

    def _prime(self, first_piece: int) -> None:
        with self._lock:
            handle, info = self._handle, self._info
        if handle is None or info is None:
            return
        last = min(max(0, first_piece) + _LOOKAHEAD_PIECES, info["pieces"])
        for piece in range(max(0, first_piece), last):
            try:
                handle.set_piece_deadline(piece, (piece - first_piece + 1) * _DEADLINE_STEP_MS)
            except Exception:
                return

    def await_range(self, start: int, end: int, timeout: float) -> None:
        with self._lock:
            handle, info = self._handle, self._info
        if handle is None or info is None:
            raise StreamError("The stream is no longer available.")
        piece_length, base = info["piece_length"], info["offset"]
        first = max(0, (base + start) // piece_length)
        last = min(info["pieces"] - 1, (base + end) // piece_length)
        deadline = time.monotonic() + timeout
        self._prime(first)
        last_done, last_change = -1, time.monotonic()
        for piece in range(first, last + 1):
            while not handle.have_piece(piece):
                session = self._session
                if session is None or session.stop_event.is_set():
                    raise StreamError("Streaming was cancelled.")
                if time.monotonic() > deadline:
                    raise StreamError("Timed out waiting for data - try another source.")
                status = handle.status()
                done = int(status.total_done) + int(status.total_payload_download)
                if done != last_done:
                    last_done, last_change = done, time.monotonic()
                elif time.monotonic() - last_change > _STALL_TIMEOUT:
                    raise StreamError("No seeders are responding - try another source.")
                time.sleep(0.08)
            self._prime(piece + 1)


    def _ensure_session(self) -> Any:
        with self._lock:
            if self._ses is not None:
                return self._ses
            from ..downloader import NET
            if message := NET.leak_guard():
                raise StreamError(message)
            quiet = NET.anonymous or bool(NET.leak_guard())
            port = _pick_port(int(getattr(settings, "torrent_port", 6881)))
            host = NET.bind.strip()
            host = f"[{host}]" if ":" in host else host
            encoding = int(lt.enc_policy.forced if NET.require_encryption else lt.enc_policy.enabled)
            pack: dict[str, Any] = {
                "dht_bootstrap_nodes": ",".join(_BOOTSTRAP),
                "listen_interfaces": f"{host}:{port}" if host else f"0.0.0.0:{port},[::]:{port}",
                "outgoing_interfaces": NET.bind.strip(),
                "enable_dht": bool(NET.dht) and not quiet, "enable_lsd": bool(NET.lsd) and not quiet,
                "enable_upnp": bool(NET.upnp) and not quiet, "enable_natpmp": bool(NET.upnp) and not quiet,
                "user_agent": "" if quiet else "PiraChest/3.0", "anonymous_mode": bool(NET.anonymous),
                "out_enc_policy": encoding, "in_enc_policy": encoding,
                "allowed_enc_level": int(lt.enc_level.both), "prefer_rc4": bool(NET.require_encryption),
                "proxy_type": int(lt.proxy_type_t.none), "force_proxy": False,
                "max_webtorrent_offers": 0,
                "active_downloads": -1, "active_limit": -1,
                "connections_limit": max(200, int(libtorrent_defaults.max_connections_per_torrent) * 2),
                "announce_to_all_trackers": True, "announce_to_all_tiers": True,
                "upload_rate_limit": int(getattr(settings, "upload_speed_limit", 0) or 512) * 1024,
                "alert_mask": int(lt.alert_category.storage),
            }
            if NET.has_proxy:
                socks, auth = NET.proxy_type == "socks5", bool(NET.proxy_user)
                pack.update({
                    "proxy_type": int((lt.proxy_type_t.socks5_pw if auth else lt.proxy_type_t.socks5)
                                      if socks else (lt.proxy_type_t.http_pw if auth else lt.proxy_type_t.http)),
                    "proxy_hostname": NET.proxy_host, "proxy_port": NET.proxy_port,
                    "proxy_username": NET.proxy_user, "proxy_password": NET.proxy_pass,
                    "proxy_hostnames": True, "proxy_tracker_connections": True,
                    "proxy_peer_connections": NET.proxy_peers and socks, "force_proxy": NET.force_proxy,
                })
            params = lt.session_params()
            params.settings = pack
            self._ses = lt.session(params)
            self._pump = _AlertPump(self._ses, self)
            self._pump.start()
            return self._ses

    def _ensure_server(self) -> None:
        with self._lock:
            if self._server is not None:
                return
            server = ThreadingHTTPServer(("127.0.0.1", 0), _StreamHandler)
            server.daemon_threads = True
            server.engine = self
            server.token = ""
            server.session = None
            self._port = int(server.server_address[1])
            threading.Thread(target=server.serve_forever, name="torrent-stream-http",
                             daemon=True).start()
            self._server = server

    def _run(self, session: StreamSession) -> None:
        try:
            ses = self._ensure_session()
            root = os.path.join(paths.cache_dir, "stream", secrets.token_hex(8))
            os.makedirs(root, exist_ok=True)
            with self._lock:
                self._root = root
            session.state, session.message = "metadata", "Finding peers..."
            try:
                handle = lt.add_magnet_uri(ses, session.source.magnet, {
                    "save_path": root, "storage_mode": lt.storage_mode_t.storage_mode_sparse,
                })
            except Exception as exc:
                raise StreamError(f"This source cannot be streamed: {exc}") from exc
            with self._lock:
                self._handle = handle

            deadline = time.monotonic() + float(getattr(settings, "tv_stream_connect_timeout", _METADATA_TIMEOUT))
            while True:
                if session.stop_event.is_set():
                    return
                status = handle.status()
                if status.has_metadata:
                    break
                error = getattr(status, "errc", None)
                if error is not None and error.value():
                    raise StreamError(f"The tracker rejected this torrent: {error.message()}")
                if time.monotonic() > deadline:
                    raise StreamError("No peers answered - this torrent looks dead.")
                time.sleep(0.1)

            ti = handle.torrent_file()
            index, relative, size = _pick_file(ti, session.source.file_idx, session.episode,
                                               getattr(session.source, "filename", ""))
            priorities = [0] * ti.num_files()
            priorities[index] = 7
            handle.prioritize_files(priorities)
            handle.set_sequential_download(True)

            offset = sum(int(ti.files().file_size(i)) for i in range(index))
            with self._lock:
                self._info = {
                    "index": index, "size": size, "offset": offset,
                    "piece_length": int(ti.piece_length()), "pieces": int(ti.num_pieces()),
                }
            logger.info("Streaming %s (%s) from %s", relative, size, session.source.info_hash)
            session.file_name, session.file_size = relative, size
            session.state, session.message = "buffering", "Buffering..."

            piece_length = max(1, int(ti.piece_length()))
            head_bytes = int(getattr(settings, "tv_stream_prebuffer_mb", 4)) * 1024 ** 2
            head_pieces = max(1, min(32, math.ceil(min(head_bytes, size) / piece_length)))
            self._prime(0)
            self.await_range(0, min(size - 1, head_pieces * piece_length - 1), _HEAD_TIMEOUT)
            if session.stop_event.is_set():
                return
            self._refresh_stats(session, force=True)
            session.state, session.message = "ready", ""
        except StreamError as exc:
            session.state, session.error, session.message = "error", str(exc), str(exc)
        except Exception as exc:
            logger.exception("Torrent streaming failed")
            session.state = "error"
            session.error = f"{type(exc).__name__}: {exc}"
            session.message = session.error

    def _refresh_stats(self, session: StreamSession, force: bool = False) -> None:
        with self._lock:
            handle, info = self._handle, self._info
        if handle is None or info is None:
            return
        try:
            status = handle.status()
            session.rate = int(status.download_rate)
            session.peers = int(status.num_peers)


            if info["size"] and (force or time.monotonic() - self._progress_at > 1.5):
                self._progress_at = time.monotonic()
                piece_length = info["piece_length"]
                first = info["offset"] // piece_length
                last = min(info["pieces"] - 1, (info["offset"] + info["size"] - 1) // piece_length)
                have = sum(1 for piece in range(first, last + 1) if handle.have_piece(piece))
                session.progress = have / max(1, last - first + 1)
        except Exception:
            pass


_streamer: Optional[TorrentStreamer] = None


def get_streamer() -> TorrentStreamer:
    global _streamer
    if _streamer is None:
        _streamer = TorrentStreamer()
    return _streamer


def shutdown_streamer() -> None:
    global _streamer
    if _streamer is not None:
        _streamer.shutdown()
        _streamer = None
