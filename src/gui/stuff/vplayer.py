from __future__ import annotations

import html
import logging
import math
import queue as _queue
import re
import ssl
import threading
import time
import urllib.parse
from bisect import bisect_right
from collections import OrderedDict
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import suppress
from functools import partial
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Optional
from urllib.parse import urljoin

import httpx
from PySide6.QtCore import QElapsedTimer, QEvent, QPoint, QRect, QRectF, QSettings, QSize, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import (
    QActionGroup, QColor, QCursor, QFont, QFontMetrics, QGuiApplication, QIcon, QIconEngine, QPainter,
    QPainterPath, QPen, QPixmap,
)
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
from PySide6.QtMultimediaWidgets import QVideoWidget
from PySide6.QtWidgets import (
    QDoubleSpinBox, QFrame, QHBoxLayout, QLabel, QMenu, QSizePolicy, QSlider, QStyle, QToolButton, QToolTip,
    QVBoxLayout, QWidget,
)
from PySide6.QtWidgets import QPushButton as _QPushButton
from qfluentwidgets import FluentIcon
from qfluentwidgets import Theme as _Theme
from shiboken6 import isValid as _qt_is_valid

from src.core.config import settings
from src.core.net import LazyHttpClient, shared_ssl_context
from src.core.theme import palette, register_theme_refresh, to_color
from src.core.translations import tr

logger = logging.getLogger(__name__)


_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
_ad_hoc_client = LazyHttpClient(timeout=20, follow_redirects=True, limits=httpx.Limits(max_connections=8, max_keepalive_connections=8))

def close_ad_hoc_client() -> None:
    try:
        _ad_hoc_client.close()
    except Exception:
        pass


_PLAY = QPainterPath()
_PLAY.moveTo(.22, .1)
_PLAY.lineTo(.88, .5)
_PLAY.lineTo(.22, .9)
_PLAY.closeSubpath()


class _PlayIconEngine(QIconEngine):
    def __init__(self, ink: Optional[QColor] = None, follow: bool = False) -> None:
        super().__init__()
        self._ink = QColor(ink) if ink is not None else None
        self._follow = follow

    def clone(self) -> "_PlayIconEngine":
        return _PlayIconEngine(self._ink, self._follow)

    def _color(self, painter: QPainter, mode) -> QColor:
        if self._follow:
            dev = painter.device()
            if isinstance(dev, QWidget):
                return QColor(dev.palette().buttonText().color())
        ink = QColor(self._ink) if self._ink is not None else to_color(palette().get('primary_text', '#ffffff'), '#ffffff')
        if mode == QIcon.Mode.Disabled:
            ink.setAlphaF(ink.alphaF() * .45)
        return ink

    def paint(self, painter: QPainter, rect: QRect, mode, state) -> None:
        ink = self._color(painter, mode)
        side = min(rect.width(), rect.height())
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.translate(rect.x() + (rect.width() - side) / 2, rect.y() + (rect.height() - side) / 2)
        painter.scale(side, side)
        painter.setPen(QPen(ink, .1, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
        painter.setBrush(ink)
        painter.drawPath(_PLAY)
        painter.restore()

    def pixmap(self, size: QSize, mode, state) -> QPixmap:
        pm = QPixmap(size)
        pm.fill(Qt.GlobalColor.transparent)
        p = QPainter(pm)
        self.paint(p, QRect(QPoint(0, 0), size), mode, state)
        p.end()
        return pm


def _play_icon(ink: Optional[QColor] = None, follow: bool = False) -> QIcon:
    return QIcon(_PlayIconEngine(ink, follow))


_HTTP_TIMEOUT = httpx.Timeout(connect=6.0, read=15.0, write=10.0, pool=8.0)


def _new_session(insecure: bool = False) -> httpx.Client:
    verify = False if insecure else shared_ssl_context()
    return httpx.Client(
        timeout=_HTTP_TIMEOUT, follow_redirects=True,
        limits=httpx.Limits(max_connections=16, max_keepalive_connections=8, keepalive_expiry=30),
        transport=httpx.HTTPTransport(retries=2, verify=verify),
    )


_TLS_FAILED_HOSTS: set[str] = set()


def _is_cert_error(exc: BaseException) -> bool:
    seen: set[int] = set()
    e: Optional[BaseException] = exc
    while e is not None and id(e) not in seen:
        seen.add(id(e))
        if isinstance(e, ssl.SSLCertVerificationError):
            return True
        e = e.__cause__ or e.__context__
    return False


class _UpstreamError(Exception):
    def __init__(self, status: int = 502):
        super().__init__(status)
        self.status = status


class _NotCacheable(Exception):
    pass


_MPEGURL = "application/vnd.apple.mpegurl"
_NET_ERRORS = (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError, httpx.HTTPError, httpx.StreamError)


class _RelayHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    disable_nagle_algorithm = True
    wbufsize = 1 << 16
    timeout = 60

    def log_message(self, *_args):
        pass

    def _send(self, status: int, ctype: str, body: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        relay: HlsRelayServer = self.server.relay
        path = self.path.split("?")[0]
        url = relay.resolve(path)
        if not url:
            self.send_error(404, "Unknown relay path")
            return
        rng = self.headers.get("Range")
        try:
            if url.split("?")[0].lower().endswith(".m3u8"):
                self._send(200, _MPEGURL, relay.playlist(url).encode("utf-8"))
                return
            if not rng and relay.is_cacheable(path):
                try:
                    data, ctype = relay.segment(path)
                except _NotCacheable:
                    pass
                else:
                    if data[:7] == b"#EXTM3U":
                        body = relay.rewrite_playlist(data.decode("utf-8", "replace"), url).encode("utf-8")
                        self._send(200, _MPEGURL, body)
                    else:
                        self._send(200, ctype, data)
                    return
            self._passthrough(relay, url, rng)
        except _UpstreamError as exc:
            try:
                self.send_error(exc.status, "Upstream error")
            except _NET_ERRORS:
                pass
        except _NET_ERRORS:
            pass
        except Exception as exc:
            logger.warning("HLS relay error for %s: %s", url, exc)
            try:
                self.send_error(502, "Relay error")
            except _NET_ERRORS:
                pass

    def _passthrough(self, relay: "HlsRelayServer", url: str, rng: Optional[str]) -> None:
        headers = relay.headers()
        if rng:
            headers["Range"] = rng
        retry_headers = None
        if "Referer" not in headers:
            extra = relay._self_referer(url)
            retry_headers = {**headers, **extra} if extra else None
        upstream = None
        session = relay._session_for(url)
        for attempt in range(3):
            try:
                upstream = session.send(session.build_request("GET", url, headers=headers), stream=True)
            except httpx.HTTPError as exc:
                if _is_cert_error(exc):
                    relay._note_cert_failure(url)
                    session = relay._session_for(url)
                if attempt >= 1:
                    logger.warning("HLS relay upstream fetch failed for %s: %s", url, exc)
                    raise _UpstreamError(502) from exc
                continue
            if upstream.status_code in (401, 403) and retry_headers is not None:
                upstream.close()
                headers, retry_headers = retry_headers, None
                continue
            break
        try:
            status = upstream.status_code
            if status >= 400 and status != 416:
                raise _UpstreamError(status)
            ctype = upstream.headers.get("Content-Type", "application/octet-stream")
            if "mpegurl" in ctype.lower():
                text = upstream.read().decode("utf-8", "replace")
                self._send(200, _MPEGURL, relay.rewrite_playlist(text, url).encode("utf-8"))
                return
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            length = upstream.headers.get("Content-Length")
            if length:
                self.send_header("Content-Length", length)
            else:
                self.send_header("Connection", "close")
                self.close_connection = True
            for h in ("Content-Range", "Accept-Ranges"):
                v = upstream.headers.get(h)
                if v:
                    self.send_header(h, v)
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            for chunk in upstream.iter_raw(chunk_size=1 << 18):
                self.wfile.write(chunk)
            self.wfile.flush()
        finally:
            upstream.close()


_TAG_RE = re.compile(r'URI="([^"]+)"')
_SUBTITLE_MEDIA_RE = re.compile(r"^#EXT-X-MEDIA:TYPE=SUBTITLES", re.IGNORECASE)
_STREAM_INF_RE = re.compile(r"^#EXT-X-STREAM-INF:", re.IGNORECASE)
_STRIP_SUBTITLES_ATTR_RE = re.compile(r',?SUBTITLES="[^"]*"')


class _RelayServer(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 64
    allow_reuse_address = True

    def __init__(self, addr, handler, relay):
        self.relay = relay
        super().__init__(addr, handler)

    def handle_error(self, *_args) -> None:
        pass


def _single_variant(text: str) -> str:
    lines = text.splitlines()
    pairs = [(i, lines[i]) for i in range(len(lines) - 1) if lines[i].startswith("#EXT-X-STREAM-INF")]
    if len(pairs) < 2:
        return text

    def rank(item) -> tuple:
        attrs = item[1]
        res = re.search(r"RESOLUTION=\d+x(\d+)", attrs)
        bw = re.search(r"(?<!AVERAGE-)BANDWIDTH=(\d+)", attrs)
        height = int(res.group(1)) if res else 0
        return (height <= 1080, height if height <= 1080 else -height, int(bw.group(1)) if bw else 0)

    keep = max(pairs, key=rank)[0]
    drop = {i for i, _l in pairs if i != keep} | {i + 1 for i, _l in pairs if i != keep}
    return "\n".join(l for i, l in enumerate(lines) if i not in drop)


class HlsRelayServer:
    PREFETCH_AHEAD = 6
    CACHE_MAX_BYTES = 96 << 20
    ENTRY_MAX_BYTES = 40 << 20
    PREFETCH_WORKERS = 4

    def __init__(self) -> None:
        self._server: Optional[_RelayServer] = None
        self._lock = threading.RLock()
        self._path_map: dict[str, str] = {}
        self._url_map: dict[str, str] = {}
        self._cacheable: set[str] = set()
        self._next_seg: dict[str, str] = {}
        self._pl_slots: dict[tuple[str, int], str] = {}
        self._next_id = 0
        self._epoch = 0
        self._last_seg: Optional[str] = None
        self.referer: Optional[str] = None
        self.extra_headers: dict = {}
        self._session = _new_session()
        self._insecure_session: Optional[httpx.Client] = None
        self._cache: "OrderedDict[str, tuple[bytes, str]]" = OrderedDict()
        self._cache_bytes = 0
        self._inflight: dict[str, Future] = {}
        self._raw_pl: dict[str, tuple[float, str]] = {}
        self._pq: "_queue.Queue[Optional[str]]" = _queue.Queue()
        self._workers: list[threading.Thread] = []

    def _session_for(self, url: str) -> httpx.Client:
        if urllib.parse.urlsplit(url).hostname in _TLS_FAILED_HOSTS:
            if self._insecure_session is None:
                self._insecure_session = _new_session(insecure=True)
            return self._insecure_session
        return self._session

    def _note_cert_failure(self, url: str) -> None:
        host = urllib.parse.urlsplit(url).hostname
        if host and host not in _TLS_FAILED_HOSTS:
            _TLS_FAILED_HOSTS.add(host)
            logger.warning("HLS relay: certificate verification failed for %s - continuing unverified", host)


    def start(self) -> int:
        if self._server is not None:
            return self._server.server_port
        self._server = _RelayServer(("127.0.0.1", 0), _RelayHandler, self)
        threading.Thread(target=self._server.serve_forever, kwargs={"poll_interval": 0.1}, daemon=True).start()
        self._pq = _queue.Queue()
        self._workers = [threading.Thread(target=self._prefetch_loop, args=(self._pq,), daemon=True)
                         for _ in range(self.PREFETCH_WORKERS)]
        for t in self._workers:
            t.start()
        return self._server.server_port

    def stop(self) -> None:
        for _ in self._workers:
            self._pq.put(None)
        self._workers = []
        srv, self._server = self._server, None
        if srv is not None:
            threading.Thread(target=lambda: (srv.shutdown(), srv.server_close()), daemon=True).start()
        self._reset_state(drain_queue=False)
        try:
            self._session.close()
        except Exception:
            pass
        self._session = _new_session()
        if self._insecure_session is not None:
            try:
                self._insecure_session.close()
            except Exception:
                pass
            self._insecure_session = None

    def _reset_state(self, drain_queue: bool = True) -> None:
        with self._lock:
            self._epoch += 1
            self._path_map.clear()
            self._url_map.clear()
            self._cacheable.clear()
            self._next_seg.clear()
            self._pl_slots.clear()
            self._cache.clear()
            self._cache_bytes = 0
            self._raw_pl.clear()
            self._inflight.clear()
            self._last_seg = None
        if drain_queue:
            try:
                while True:
                    self._pq.get_nowait()
            except _queue.Empty:
                pass

    def set_stream(self, root_url: str, referer: Optional[str] = None, extra_headers: Optional[dict] = None, live: bool = False) -> str:
        self.PREFETCH_AHEAD, self.CACHE_MAX_BYTES = (3, 32 << 20) if live else (6, 96 << 20)
        if self._server is None:
            self.start()
        self.referer = referer
        self.extra_headers = extra_headers or {}
        self._reset_state()
        with self._lock:
            local = self._register(root_url)
        return f"http://127.0.0.1:{self._server.server_port}{local}"

    def register(self, absolute_url: str) -> str:
        if self._server is None:
            self.start()
        with self._lock:
            local = self._register(absolute_url)
        return f"http://127.0.0.1:{self._server.server_port}{local}"

    def resolve(self, path: str) -> Optional[str]:
        with self._lock:
            return self._path_map.get(path)

    def is_cacheable(self, path: str) -> bool:
        return path in self._cacheable

    def headers(self) -> dict:
        h = {"User-Agent": _UA, "Accept-Encoding": "identity"}
        if self.referer:
            h["Referer"] = self.referer
            try:
                p = urllib.parse.urlsplit(self.referer)
                if p.scheme and p.netloc:
                    h["Origin"] = f"{p.scheme}://{p.netloc}"
            except ValueError:
                pass
        h.update(self.extra_headers or {})
        return h


    @staticmethod
    def _self_referer(url: str) -> dict:
        try:
            p = urllib.parse.urlsplit(url)
        except ValueError:
            return {}
        if not p.scheme or not p.netloc:
            return {}
        origin = f"{p.scheme}://{p.netloc}"
        return {"Referer": f"{origin}/", "Origin": origin}

    def _get(self, url: str) -> tuple[bytes, str]:
        scheme = urllib.parse.urlsplit(url).scheme.lower()
        if scheme not in ("http", "https"):
            logger.warning("HLS relay refused a non-http upstream url: %r", url[:120])
            raise _UpstreamError(400)
        last: Exception = _UpstreamError(502)
        headers = self.headers()

        retry_headers = None
        if "Referer" not in headers:
            extra = self._self_referer(url)
            retry_headers = {**headers, **extra} if extra else None
        session = self._session_for(url)
        for attempt in range(3):
            try:
                with session.stream("GET", url, headers=headers) as r:
                    if r.status_code >= 400:
                        if r.status_code in (401, 403) and retry_headers is not None:
                            headers, retry_headers = retry_headers, None
                            last = _UpstreamError(r.status_code)
                        elif r.status_code in (408, 425, 429) or r.status_code >= 500:
                            last = _UpstreamError(r.status_code)
                        else:
                            raise _UpstreamError(r.status_code)
                    else:
                        ctype = r.headers.get("Content-Type", "application/octet-stream")
                        limit = (4 << 20) if ("mpegurl" in ctype.lower() or ".m3u8" in url.lower()) else self.ENTRY_MAX_BYTES
                        length = r.headers.get("Content-Length")
                        if length and length.isdigit() and int(length) > limit:
                            raise _NotCacheable()
                        data = bytearray()
                        for chunk in r.iter_bytes(1 << 16):
                            data += chunk
                            if len(data) > limit:
                                raise _NotCacheable()
                        return bytes(data), ctype
            except httpx.HTTPError as exc:
                last = exc
                if _is_cert_error(exc):
                    self._note_cert_failure(url)
                    session = self._session_for(url)
            if attempt < 2:
                time.sleep(0.25 * (attempt + 1))
        if isinstance(last, _UpstreamError):
            raise last
        logger.warning("HLS relay upstream failed for %s: %s", url, last)
        raise _UpstreamError(502)

    def fetch_text(self, url: str) -> str:
        now = time.monotonic()
        with self._lock:
            hit = self._raw_pl.get(url)
            if hit and hit[0] > now:
                return hit[1]
        try:
            data, _ct = self._get(url)
        except _UpstreamError:
            with self._lock:
                stale = self._raw_pl.get(url)
            if stale is None:
                raise


            logger.debug("HLS relay replaying last known playlist for %s", url)
            return stale[1]
        text = data.decode("utf-8", "replace")
        ttl = 600.0 if ("#EXT-X-ENDLIST" in text or "#EXT-X-STREAM-INF" in text) else 0.8
        with self._lock:
            if len(self._raw_pl) > 48:
                self._raw_pl.clear()
            self._raw_pl[url] = (now + ttl, text)
        return text

    def playlist(self, url: str) -> str:
        return self.rewrite_playlist(_single_variant(self.fetch_text(url)), url)


    def segment(self, path: str) -> tuple[bytes, str]:
        with self._lock:
            if self._last_seg is not None and path != self._next_seg.get(self._last_seg) and path != self._last_seg:
                try:
                    while True:
                        self._pq.get_nowait()
                except _queue.Empty:
                    pass
            self._last_seg = path
        res = self._ensure(path)
        self._schedule_ahead(path)
        return res

    def _ensure(self, path: str) -> tuple[bytes, str]:
        with self._lock:
            epoch = self._epoch
            hit = self._cache.get(path)
            if hit is not None:
                self._cache.move_to_end(path)
                return hit
            fut = self._inflight.get(path)
            owner = fut is None
            if owner:
                fut = self._inflight[path] = Future()
            url = self._path_map.get(path)
        if not owner:
            return fut.result(timeout=40)
        try:
            if not url:
                raise _UpstreamError(404)
            data, ctype = self._get(url)
            if ctype.lower().startswith("image/"):
                peek = data[:12]
                real_img = (peek[:3] == b"\xff\xd8\xff" or peek[:8] == b"\x89PNG\r\n\x1a\n"
                            or peek[:6] in (b"GIF87a", b"GIF89a") or (peek[:4] == b"RIFF" and peek[8:12] == b"WEBP"))
                if real_img and peek[:1] != b"\x47":
                    logger.warning("HLS relay got a genuine image for %s - likely hotlink-blocked", url)
                    raise _UpstreamError(502)
                ctype = "video/mp2t"
            res = (data, ctype)
            with self._lock:
                if epoch == self._epoch and len(data) <= self.ENTRY_MAX_BYTES:
                    self._cache[path] = res
                    self._cache_bytes += len(data)
                    while self._cache_bytes > self.CACHE_MAX_BYTES and len(self._cache) > 1:
                        _k, (old, _c) = self._cache.popitem(last=False)
                        self._cache_bytes -= len(old)
            fut.set_result(res)
            return res
        except BaseException as exc:
            fut.set_exception(exc)
            raise
        finally:
            with self._lock:
                if self._inflight.get(path) is fut:
                    self._inflight.pop(path, None)

    def _schedule_ahead(self, path: str) -> None:
        with self._lock:
            p = path
            for _ in range(self.PREFETCH_AHEAD):
                p = self._next_seg.get(p)
                if not p:
                    break
                if p not in self._cache and p not in self._inflight:
                    self._pq.put(p)

    def _prefetch_loop(self, q) -> None:
        while True:
            p = q.get()
            if p is None:
                return
            try:
                self._ensure(p)
            except BaseException:
                pass


    def _register(self, absolute_url: str, cacheable: bool = False) -> str:
        path = self._url_map.get(absolute_url)
        if path is None:
            path = f"/relay/{self._next_id}{self._guess_extension(absolute_url)}"
            self._next_id += 1
            self._path_map[path] = absolute_url
            self._url_map[absolute_url] = path
        if cacheable and not path.endswith(".m3u8"):
            self._cacheable.add(path)
        return path

    def _register_playlist_slot(self, base_url: str, slot: int, absolute_url: str) -> str:
        path = self._pl_slots.get((base_url, slot))
        if path is None:
            path = self._register(absolute_url)
            self._pl_slots[(base_url, slot)] = path
            return path
        old = self._path_map.get(path)
        if old != absolute_url:
            if old is not None:
                self._url_map.pop(old, None)
            self._path_map[path] = absolute_url
            self._url_map[absolute_url] = path
        return path

    _ALLOWED_EXTS = {
        ".3gp", ".aac", ".avi", ".flac", ".mkv", ".m3u8", ".mov", ".mp3",
        ".mp4", ".mpegts", ".mpd", ".ogg", ".ogv", ".ts", ".vtt", ".webm",
    }
    _FMP4_REMAP = {".m4s", ".cmfv", ".cmfa", ".m4v", ".m4a", ".mp4a"}

    @classmethod
    def _guess_extension(cls, absolute_url: str) -> str:
        clean = absolute_url.split("?")[0].split("#")[0]
        dot = clean.rfind(".")
        slash = clean.rfind("/")
        if dot > slash and dot != -1:
            ext = clean[dot:].lower()
            if 1 < len(ext) <= 6:
                if ext in cls._ALLOWED_EXTS:
                    return ext
                if ext in cls._FMP4_REMAP:
                    return ".mp4"
        return ".ts"

    def rewrite_playlist(self, text: str, base_url: str) -> str:
        out = []
        prev: Optional[str] = None
        pl_slot = 0
        with self._lock:
            for line in text.splitlines():
                s = line.strip()
                if not s:
                    out.append(line)
                    continue
                if s.startswith("#"):
                    if _SUBTITLE_MEDIA_RE.match(s):
                        continue
                    if _STREAM_INF_RE.match(s):
                        s = _STRIP_SUBTITLES_ATTR_RE.sub("", s)
                    m = _TAG_RE.search(s)
                    if m:
                        u = urljoin(base_url, m.group(1))
                        cacheable = s.startswith(("#EXT-X-MAP", "#EXT-X-KEY", "#EXT-X-SESSION-KEY"))
                        if not cacheable and u.split("?")[0].lower().endswith(".m3u8"):
                            lp = self._register_playlist_slot(base_url, pl_slot, u)
                            pl_slot += 1
                        else:
                            lp = self._register(u, cacheable=cacheable)
                        s = s[:m.start(1)] + lp + s[m.end(1):]
                    out.append(s)
                    continue
                u = urljoin(base_url, s)
                if u.split("?")[0].lower().endswith(".m3u8"):
                    lp = self._register_playlist_slot(base_url, pl_slot, u)
                    pl_slot += 1
                else:
                    lp = self._register(u, cacheable=True)
                if prev is not None and lp != prev and lp in self._cacheable:
                    self._next_seg[prev] = lp
                prev = lp if lp in self._cacheable else None
                out.append(lp)
        return "\n".join(out)


_relay: Optional[HlsRelayServer] = None

def get_relay() -> HlsRelayServer:
    global _relay
    if _relay is None:
        _relay = HlsRelayServer()
    return _relay

def _format_ms(ms: int) -> str:
    total_s = max(0, ms) // 1000
    h, rem = divmod(total_s, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"

_SPEEDS = (0.5, 0.75, 1.0, 1.25, 1.5, 2.0)
_ASPECT_W, _ASPECT_H = 16, 9
_QWIDGETSIZE_MAX = 16777215

_RESOLUTION_RE = re.compile(r"RESOLUTION=(\d+)x(\d+)", re.IGNORECASE)
_BANDWIDTH_RE = re.compile(r"BANDWIDTH=(\d+)", re.IGNORECASE)

def _parse_hls_variants(playlist_text: str, base_url: str) -> list[tuple[Optional[int], int, str]]:
    lines = playlist_text.splitlines()
    variants: list[tuple[Optional[int], int, str]] = []
    for i, line in enumerate(lines):
        if not _STREAM_INF_RE.match(line.strip()):
            continue
        attrs = line.split(":", 1)[1] if ":" in line else ""
        res_m = _RESOLUTION_RE.search(attrs)
        bw_m = _BANDWIDTH_RE.search(attrs)
        height = int(res_m.group(2)) if res_m else None
        bandwidth = int(bw_m.group(1)) if bw_m else 0
        uri = None
        for j in range(i + 1, len(lines)):
            cand = lines[j].strip()
            if not cand or cand.startswith("#"):
                continue
            uri = cand
            break
        if uri:
            variants.append((height, bandwidth, urljoin(base_url, uri)))

    seen: set[str] = set()
    out: list[tuple[Optional[int], int, str]] = []
    for height, bandwidth, variant_url in sorted(variants, key=lambda v: (v[0] or 0, v[1]), reverse=True):
        if variant_url in seen:
            continue
        seen.add(variant_url)
        out.append((height, bandwidth, variant_url))
    return out

_MEDIA_SUBS_RE = re.compile(r'^#EXT-X-MEDIA:TYPE=SUBTITLES', re.IGNORECASE)
_MEDIA_ATTR_RE = re.compile(r'([A-Z-]+)=(?:"([^"]*)"|([^,]*))')

def _parse_hls_subtitle_groups(playlist_text: str, base_url: str) -> list[dict]:
    out: list[dict] = []
    for line in playlist_text.splitlines():
        stripped = line.strip()
        if not _MEDIA_SUBS_RE.match(stripped):
            continue
        attrs = {}
        for m in _MEDIA_ATTR_RE.finditer(stripped):
            key = m.group(1).upper()
            attrs[key] = m.group(2) if m.group(2) is not None else m.group(3)
        uri = attrs.get("URI")
        if not uri:
            continue
        out.append({
            "url": urljoin(base_url, uri),
            "label": attrs.get("NAME") or attrs.get("LANGUAGE") or "Subtitle",
            "srclang": attrs.get("LANGUAGE") or "",
            "default": str(attrs.get("DEFAULT", "")).upper() == "YES",
        })
    return out

_CUE_TIME_RE = re.compile(
    r"(?:(\d{1,2}):)?(\d{2}):(\d{2})[.,](\d{3})\s*-->\s*(?:(\d{1,2}):)?(\d{2}):(\d{2})[.,](\d{3})"
)
_TAG_STRIP_RE = re.compile(r"<[^>]+>")

_ASS_TIME_RE = re.compile(r"(\d+):(\d{2}):(\d{2})[.](\d{2})")
_ASS_TAG_RE = re.compile(r"\{[^}]*\}")


def _parse_ass_cues(text: str) -> list[tuple[int, int, str]]:
    cues = []
    for line in text.splitlines():
        if not line.startswith("Dialogue:"):
            continue
        parts = line[9:].split(",", 9)
        if len(parts) < 10:
            continue
        a, b = _ASS_TIME_RE.match(parts[1].strip()), _ASS_TIME_RE.match(parts[2].strip())
        if not (a and b):
            continue
        t = lambda m: ((int(m[1]) * 3600 + int(m[2]) * 60 + int(m[3])) * 1000 + int(m[4]) * 10)
        body = _ASS_TAG_RE.sub("", parts[9]).replace("\\N", "\n").replace("\\n", "\n").strip()
        if body:
            cues.append((t(a), t(b), body))
    return cues


def _parse_subtitle_cues(text: str) -> list[tuple[int, int, str]]:
    text = text.lstrip("\ufeff")
    if "[Script Info]" in text[:200] or "\nDialogue:" in text:
        cues = _parse_ass_cues(text)
        cues.sort(key=lambda c: c[0])
        return cues
    cues: list[tuple[int, int, str]] = []
    for block in re.split(r"\r?\n\r?\n+", text.strip()):
        lines = block.splitlines()
        timing_idx = next((i for i, ln in enumerate(lines) if "-->" in ln), None)
        if timing_idx is None:
            continue
        m = _CUE_TIME_RE.search(lines[timing_idx])
        if not m:
            continue
        sh, sm, ss, sms, eh, em, es, ems = m.groups()
        start_ms = (int(sh or 0) * 3600 + int(sm) * 60 + int(ss)) * 1000 + int(sms)
        end_ms = (int(eh or 0) * 3600 + int(em) * 60 + int(es)) * 1000 + int(ems)
        cue_text = _TAG_STRIP_RE.sub("", "\n".join(lines[timing_idx + 1:])).strip()
        cue_text = html.unescape(cue_text)
        if cue_text:
            cues.append((start_ms, end_ms, cue_text))
    cues.sort(key=lambda c: c[0])
    return cues


_BAR_QSS = """
QToolButton{color:#fff;background:transparent;border:none;border-radius:6px;padding:0 6px;
font:600 11px 'Segoe UI Variable Text','Segoe UI',sans-serif;}
QToolButton:hover{background:rgba(255,255,255,34);}
QToolButton:pressed{background:rgba(255,255,255,20);}
QToolButton::menu-indicator{image:none;width:0;}
QLabel{color:rgba(255,255,255,205);font:11px 'Segoe UI Variable Text','Segoe UI',sans-serif;background:transparent;}
QSlider{background:transparent;}
QSlider::groove:horizontal{height:4px;background:transparent;}
QSlider::add-page:horizontal{height:4px;border-radius:2px;background:rgba(255,255,255,70);}
QSlider::sub-page:horizontal{height:4px;border-radius:2px;background:#60cdff;}
QSlider::handle:horizontal{width:12px;height:12px;margin:-4px 0;border-radius:6px;background:#fff;}
QSlider::handle:horizontal:hover{width:14px;margin:-5px 0;border-radius:7px;}
"""
_MENU_QSS = """
QMenu{background:#2c2c31;color:#fff;border:1px solid rgba(255,255,255,40);border-radius:8px;padding:4px;
font:12px 'Segoe UI Variable Text','Segoe UI',sans-serif;}
QMenu::item{padding:5px 22px 5px 12px;border-radius:5px;}
QMenu::item:selected{background:rgba(255,255,255,36);}
"""


def _accent() -> str:
    return palette().get("accent") or "#60cdff"


def _bg_color() -> QColor:
    return QColor(palette().get("dialog_bg") or palette().get("card_bg") or palette().get("page_bg") or "#202020")


def _fg_color() -> QColor:
    return QColor(17, 17, 17) if _bg_color().lightness() > 150 else QColor(255, 255, 255)


def _themed(qss: str, bg_old: str = "") -> str:
    f = _fg_color()
    rgb = f"{f.red()},{f.green()},{f.blue()},"
    out = qss.replace("rgba(255,255,255,", f"rgba({rgb}").replace("#fff", f.name()).replace("white", f.name())
    return out.replace(bg_old, _bg_color().name()) if bg_old else out


def _bar_qss() -> str:
    return _themed(_BAR_QSS.replace("#60cdff", _accent()))


def _menu_qss() -> str:
    c = QColor(_accent())
    return _themed(_MENU_QSS.replace("rgba(255,255,255,36)", f"rgba({c.red()},{c.green()},{c.blue()},90)"), "#2c2c31")


def _wicon(name: str, fallback=FluentIcon.PLAY) -> QIcon:
    return getattr(FluentIcon, name, fallback).icon(_Theme.DARK)


class _SeekSlider(QSlider):
    def _val(self, x: float) -> int:
        return QStyle.sliderValueFromPosition(self.minimum(), self.maximum(), int(x), max(1, self.width()))

    def mousePressEvent(self, e) -> None:
        if e.button() == Qt.MouseButton.LeftButton and self.maximum() > 0:
            self.setSliderDown(True)
            self.setValue(self._val(e.position().x()))
            self.sliderMoved.emit(self.value())
            e.accept()
        else:
            super().mousePressEvent(e)

    def mouseMoveEvent(self, e) -> None:
        if self.isSliderDown():
            self.setValue(self._val(e.position().x()))
            self.sliderMoved.emit(self.value())
            e.accept()
        elif self.maximum() > 0:
            QToolTip.showText(e.globalPosition().toPoint(), _format_ms(self._val(e.position().x())), self)

    def mouseReleaseEvent(self, e) -> None:
        if self.isSliderDown():
            self.setSliderDown(False)
            e.accept()


class _SubDelayPopup(QFrame):
    changed = Signal(int)

    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent, Qt.WindowType.Popup)
        self.setObjectName("subDelay")
        self.setStyleSheet(
            "#subDelay{background:#2c2c31;border:1px solid rgba(255,255,255,40);border-radius:8px;}"
            "QLabel{color:#fff;background:transparent;font:12px 'Segoe UI Variable Text','Segoe UI',sans-serif;}"
            "QPushButton{background:rgba(255,255,255,18);color:#fff;border:none;border-radius:5px;padding:5px 6px;}"
            "QPushButton:hover{background:rgba(255,255,255,36);}"
            "QDoubleSpinBox{background:rgba(255,255,255,14);color:#fff;border:1px solid rgba(255,255,255,40);"
            "border-radius:5px;padding:4px;selection-background-color:rgba(255,255,255,60);}")
        self._ms = 0
        lay = QVBoxLayout(self)
        lay.setContentsMargins(14, 12, 14, 12)
        lay.setSpacing(10)
        self._title = QLabel(tr("anime.sub_delay_title", default="Subtitle delay"), self)
        self._title.setStyleSheet("font-weight:600;")
        lay.addWidget(self._title)
        row = QHBoxLayout()
        row.setSpacing(6)
        self._spin = QDoubleSpinBox(self)
        self._spin.setDecimals(3)
        self._spin.setRange(-3600.0, 3600.0)
        self._spin.setSingleStep(0.1)
        self._spin.setSuffix(" s")
        self._spin.setButtonSymbols(QDoubleSpinBox.ButtonSymbols.NoButtons)
        self._spin.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._spin.setFixedWidth(110)
        self._spin.setKeyboardTracking(False)
        self._spin.valueChanged.connect(lambda v: self._emit(round(v * 1000)))
        for text, step in (("\u22121 s", -1000), ("\u22120.1", -100)):
            row.addWidget(self._step_button(text, step))
        row.addWidget(self._spin)
        for text, step in (("+0.1", 100), ("+1 s", 1000)):
            row.addWidget(self._step_button(text, step))
        lay.addLayout(row)
        self._hint = QLabel(tr("anime.sub_delay_hint", default="Positive shows subtitles later, negative earlier."), self)
        lay.addWidget(self._hint)
        self._reset = _QPushButton(tr("anime.sub_delay_reset", default="Reset"), self)
        self._reset.clicked.connect(lambda: self._emit(0, sync=True))
        lay.addWidget(self._reset)

    def _step_button(self, text: str, step: int) -> _QPushButton:
        btn = _QPushButton(text, self)
        btn.setFixedWidth(52)
        btn.clicked.connect(lambda: self._emit(self._ms + step, sync=True))
        return btn

    def _emit(self, ms: int, sync: bool = False) -> None:
        if ms == self._ms and not sync:
            return
        self._ms = ms
        if sync:
            self._spin.blockSignals(True)
            self._spin.setValue(ms / 1000)
            self._spin.blockSignals(False)
        self.changed.emit(ms)

    def popup(self, ms: int, at: QPoint) -> None:
        self._ms = ms
        self._spin.blockSignals(True)
        self._spin.setValue(ms / 1000)
        self._spin.blockSignals(False)
        self.adjustSize()
        self.move(at - QPoint(0, self.sizeHint().height()))
        self.show()
        self._spin.setFocus()
        self._spin.selectAll()


class _Bar(QWidget):
    floating = False

    def paintEvent(self, _e) -> None:
        p = QPainter(self)
        if self.floating:
            p.setRenderHint(QPainter.RenderHint.Antialiasing)
            bg = _bg_color()
            bg.setAlpha(232)
            fg = _fg_color()
            fg.setAlpha(40)
            p.setBrush(bg)
            p.setPen(fg)
            p.drawRoundedRect(QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5), 12, 12)
        else:
            fg = _fg_color()
            fg.setAlpha(28)
            p.fillRect(self.rect(), _bg_color())
            p.fillRect(0, 0, self.width(), 1, fg)


class _Spinner(QWidget):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setFixedSize(20, 20)
        self._angle = 0
        self._active = False
        self._t = QTimer(self)
        self._t.setInterval(50)
        self._t.timeout.connect(self._tick)

    def set_active(self, on: bool) -> None:
        if on == self._active:
            return
        self._active = on
        self._t.start() if (on and self.isVisible()) else self._t.stop()
        self.update()

    def showEvent(self, e) -> None:
        if self._active:
            self._t.start()
        super().showEvent(e)

    def hideEvent(self, e) -> None:
        self._t.stop()
        super().hideEvent(e)

    def _tick(self) -> None:
        self._angle = (self._angle + 36) % 360
        self.update()

    def paintEvent(self, _e) -> None:
        if not self._active:
            return
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        pen = QPen(QColor(_accent()), 2.4)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        p.setPen(pen)
        p.drawArc(QRectF(3, 3, 14, 14), -self._angle * 16, -110 * 16)


class _LoadingOverlay(QWidget):
    _SIZE = 68
    _DELAY_MS = 250

    def __init__(self, player: "AnimePlayerWidget", owner: Optional[QWidget] = None) -> None:
        super().__init__(owner or player, Qt.WindowType.Tool | Qt.WindowType.FramelessWindowHint
                         | Qt.WindowType.WindowDoesNotAcceptFocus | Qt.WindowType.NoDropShadowWindowHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.setFixedSize(self._SIZE, self._SIZE)
        self._p = player
        self._active = False
        self._clock = QElapsedTimer()
        self._timer = QTimer(self)
        self._timer.setInterval(16)
        self._timer.timeout.connect(self._tick)

    def set_active(self, on: bool) -> None:
        if on == self._active:
            return
        self._active = on
        if on:
            self._clock.start()
            self._timer.start()
        else:
            self._timer.stop()
            self.hide()

    def _tick(self) -> None:
        v = self._p._vid()
        if self._clock.elapsed() < self._DELAY_MS or not v.isVisible() or v.visibleRegion().isEmpty():
            if self.isVisible():
                self.hide()
            return
        tl = v.mapToGlobal(QPoint(0, 0))
        pos = QPoint(tl.x() + (v.width() - self.width()) // 2, tl.y() + (v.height() - self.height()) // 2)
        if pos != self.pos():
            self.move(pos)
        if not self.isVisible():
            self.show()
            self.raise_()
        self.update()

    def paintEvent(self, _e) -> None:
        if not self._active:
            return
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(0, 0, 0, 120))
        p.drawEllipse(QRectF(self.rect()))
        ring = QRectF(self.rect()).adjusted(14, 14, -14, -14)
        pen = QPen(QColor(255, 255, 255, 50), 4)
        p.setPen(pen)
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawEllipse(ring)
        pen = QPen(QColor(255, 255, 255, 240), 4)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        p.setPen(pen)
        angle = (self._clock.elapsed() % 900) / 900.0 * 360.0
        p.drawArc(ring, int(-angle * 16), -100 * 16)


def _is_local_stream_url(url: str) -> bool:
    text = (url or "").strip()
    if not text:
        return True
    if text.lower().startswith("file:"):
        return True
    if text.startswith(("\\\\", "//", "/")):
        return True
    head = text.split(":", 1)[0]
    return len(head) == 1 and head.isalpha()


class _SkipBubble(QWidget):
    _QSS = ("QPushButton{background:rgba(20,20,24,225);color:white;border:2px solid rgba(255,255,255,210);"
            "border-radius:6px;padding:9px 22px;font:600 14px 'Segoe UI';}"
            "QPushButton:hover{background:white;color:black;}")

    def __init__(self, player: "AnimePlayerWidget", owner: Optional[QWidget] = None) -> None:
        super().__init__(owner or player, Qt.WindowType.Tool | Qt.WindowType.FramelessWindowHint
                         | Qt.WindowType.WindowDoesNotAcceptFocus | Qt.WindowType.NoDropShadowWindowHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self._p = player
        self._want = False
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(10)
        self._skip = _QPushButton(self)
        self._next = _QPushButton(self)
        for b in (self._skip, self._next):
            b.setStyleSheet(self._QSS)
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            b.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            b.hide()
            lay.addWidget(b)
        self._skip.clicked.connect(lambda: player._do_skip())
        self._next.clicked.connect(lambda: player.next_requested.emit())

    def set_state(self, skip: Optional[str], nxt: Optional[str]) -> None:
        for btn, text in ((self._skip, skip), (self._next, nxt)):
            if text:
                if btn.text() != text:
                    btn.setText(text)
                btn.show()
            else:
                btn.hide()
        self._want = bool(skip or nxt)
        self.reposition()

    def reposition(self) -> None:
        v = self._p._vid()
        if not self._want or not v.isVisible() or v.visibleRegion().isEmpty():
            self.hide()
            return
        self.adjustSize()
        tl = v.mapToGlobal(QPoint(0, 0))
        lift = 96 if self._p._is_fullscreen else 18
        self.move(tl.x() + v.width() - self.width() - 24, tl.y() + v.height() - self.height() - lift)
        if not self.isVisible():
            self.show()
            self.raise_()


class _SubOverlay(QWidget):
    _COLORS = ("#ffffff", "#ffe34d", "#7dffb0", "#7fd8ff")
    _FONTS = ("Segoe UI", "Arial", "Verdana", "Georgia", "Trebuchet MS", "Consolas")
    _STROKES = (0.0, 0.10, 0.17, 0.26)

    def __init__(self, player: "AnimePlayerWidget", owner: Optional[QWidget] = None) -> None:
        super().__init__(owner or player, Qt.WindowType.Tool | Qt.WindowType.FramelessWindowHint
                         | Qt.WindowType.WindowDoesNotAcceptFocus | Qt.WindowType.NoDropShadowWindowHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setCursor(Qt.CursorShape.OpenHandCursor)
        self.setToolTip(tr("anime.sub_drag_hint", default="Drag to move, scroll to resize"))
        self._p = player
        self._raw = ""
        self._pix: Optional[QPixmap] = None
        self._drag: Optional[QPoint] = None
        self._geo: Optional[tuple] = None
        qs = QSettings("PiraChest", "Subs")

        def num(key: str, default, lo, hi):
            try:
                return max(lo, min(hi, float(qs.value(key, default))))
            except (TypeError, ValueError):
                return default
        self._scale = num("scale3", 1.0, 0.5, 3.0)
        self._color = int(num("color3", 0, 0, 3))
        self._font = int(num("font3", 0, 0, 5))
        self._outline = int(num("outline3", 2, 0, 3))
        self._bg = int(num("bg3", 0, 0, 2))
        self._fx = num("fx3", 0.5, 0.0, 1.0)
        self._fy = num("fy3", 0.86, 0.0, 1.0)
        self._base: Optional[tuple] = None
        self._native = QSize()
        self._settle = QTimer(self)
        self._settle.setSingleShot(True)
        self._settle.setInterval(90)
        self._settle.timeout.connect(self._settled)
        self._timer = QTimer(self)
        self._timer.setInterval(33)
        self._timer.timeout.connect(self._follow)

    def _settled(self) -> None:
        if self._raw:
            self._style()
            self._place()

    def text(self) -> str:
        return self._raw

    def set_text(self, text: str) -> None:
        self._raw = text or ""
        if not self._raw:
            self._timer.stop()
            self.hide()
            return
        self._style()
        self._timer.start()
        self._follow()

    def _lines(self, fm: QFontMetrics, maxw: int) -> list[str]:
        out: list[str] = []
        for para in self._raw.split("\n"):
            cur = ""
            for word in para.split():
                t = f"{cur} {word}".strip()
                if cur and fm.horizontalAdvance(t) > maxw:
                    out.append(cur)
                    cur = word
                else:
                    cur = t
            if cur:
                out.append(cur)
        return out or [""]

    def _style(self, live: bool = False) -> None:
        v = self._p._vid()
        dpr = self.devicePixelRatioF()
        px = max(17, int(v.height() * 0.042 * self._scale))
        font = QFont(self._FONTS[self._font])
        font.setPixelSize(px)
        font.setBold(True)
        fm = QFontMetrics(font)
        lines = self._lines(fm, int(v.width() * 0.88))
        base = self._base
        if live and base is not None and base[3] == lines:
            k = px / base[2]
            self.setFixedSize(max(1, round(base[0] * k)), max(1, round(base[1] * k)))
            self.update()
            self._settle.start()
            return
        self._settle.stop()
        stroke = self._STROKES[self._outline] * px
        pad = px * 0.3 if self._bg else 0
        m = int(stroke + 4 + pad)
        lh = fm.lineSpacing()
        w = max(fm.horizontalAdvance(l) for l in lines) + 2 * m
        h = lh * len(lines) + 2 * m
        pix = QPixmap(int(w * dpr), int(h * dpr))
        pix.setDevicePixelRatio(dpr)
        pix.fill(Qt.GlobalColor.transparent)
        p = QPainter(pix)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        if self._bg:
            c = QColor(_accent()) if self._bg == 2 else QColor(0, 0, 0)
            c.setAlpha(215 if self._bg == 2 else 150)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(c)
            p.drawRoundedRect(QRectF(0, 0, w, h), px * 0.3, px * 0.3)
        pen = QPen(QColor(0, 0, 0, 235), stroke * 1.7)
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        for i, line in enumerate(lines):
            path = QPainterPath()
            path.addText((w - fm.horizontalAdvance(line)) / 2, m + i * lh + fm.ascent(), font, line)
            if not self._bg:
                p.save()
                p.translate(0, px * 0.07)
                p.fillPath(path, QColor(0, 0, 0, 120))
                p.restore()
                if stroke:
                    p.strokePath(path, pen)
            p.fillPath(path, QColor(self._COLORS[self._color]))
        p.end()
        self._pix = pix
        self._base = (w, h, px, lines)
        self._native = QSize(int(w), int(h))
        self.setFixedSize(self._native)
        self.update()

    def _place(self) -> None:
        v = self._p._vid()
        tl = v.mapToGlobal(QPoint(0, 0))
        x = tl.x() + int(self._fx * v.width()) - self.width() // 2
        y = tl.y() + int(self._fy * v.height()) - self.height() // 2
        low = tl.y() + v.height() - self.height()
        bar = self._p._bar
        if self._drag is None and bar.isVisible():
            low = min(low, bar.mapToGlobal(QPoint(0, 0)).y() - self.height() - 8)
        self.move(max(tl.x(), min(x, tl.x() + v.width() - self.width())), max(tl.y(), min(y, low)))

    def _follow(self, live: bool = False) -> None:
        if not self._raw or self._drag is not None:
            return
        v = self._p._vid()
        if not v.isVisible() or v.visibleRegion().isEmpty():
            self.hide()
            return
        geo = (v.mapToGlobal(QPoint(0, 0)), v.size())
        if geo != self._geo:
            if self._geo is None or self._geo[1] != geo[1]:
                self._style(live and self._geo is not None)
            self._geo = geo
            self._place()
        if not self.isVisible():
            self._place()
            self.show()
            self.raise_()

    def _save(self) -> None:
        qs = QSettings("PiraChest", "Subs")
        for k, val in (("scale3", self._scale), ("color3", self._color), ("font3", self._font),
                       ("outline3", self._outline), ("bg3", self._bg), ("fx3", self._fx), ("fy3", self._fy)):
            qs.setValue(k, val)

    def restyle(self, scale: float = 0.0, reset: bool = False) -> None:
        if scale:
            self._scale = max(0.5, min(3.0, self._scale * scale))
        if reset:
            self._fx, self._fy, self._scale = 0.5, 0.86, 1.0
        self._save()
        if self._raw:
            self._geo = None
            self._style()
            self._follow()

    def paintEvent(self, _e) -> None:
        if self._pix is None:
            return
        p = QPainter(self)
        if self.size() == self._native:
            p.drawPixmap(0, 0, self._pix)
        else:
            p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
            p.drawPixmap(self.rect(), self._pix)

    def mousePressEvent(self, e) -> None:
        if e.button() == Qt.MouseButton.LeftButton:
            self._drag = e.globalPosition().toPoint() - self.pos()
            self.setCursor(Qt.CursorShape.ClosedHandCursor)

    def mouseMoveEvent(self, e) -> None:
        if self._drag is None:
            return
        v = self._p._vid()
        tl = v.mapToGlobal(QPoint(0, 0))
        pos = e.globalPosition().toPoint() - self._drag
        x = max(tl.x(), min(pos.x(), tl.x() + v.width() - self.width()))
        y = max(tl.y(), min(pos.y(), tl.y() + v.height() - self.height()))
        self.move(x, y)
        self._fx = (x + self.width() / 2 - tl.x()) / max(1, v.width())
        self._fy = (y + self.height() / 2 - tl.y()) / max(1, v.height())

    def mouseReleaseEvent(self, e) -> None:
        if self._drag is not None:
            self._drag = None
            self.setCursor(Qt.CursorShape.OpenHandCursor)
            self._save()

    def wheelEvent(self, e) -> None:
        self.restyle(scale=1.06 if e.angleDelta().y() > 0 else 1 / 1.06)


class _PipWindow(QWidget):
    _MIN_W = 240
    _MIN_VH = 135
    _SNAP = 16

    def __init__(self, player: "AnimePlayerWidget") -> None:
        super().__init__(None, Qt.WindowType.Window | Qt.WindowType.FramelessWindowHint)
        self.setWindowTitle("PiraChest PiP")
        self.setStyleSheet("background:black;")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        self._player = player
        self._rs = None
        self._save_timer = QTimer(self)
        self._save_timer.setSingleShot(True)
        self._save_timer.setInterval(350)
        self._save_timer.timeout.connect(self.save_geometry)
        self.setMinimumSize(self._MIN_W, self._MIN_VH + AnimePlayerWidget._BAR_H)
        player._video_widget.videoSink().videoSizeChanged.connect(self._on_video_size)

    @property
    def resizing(self) -> bool:
        return self._rs is not None

    def set_always_on_top(self, on: bool) -> None:
        if bool(self.windowFlags() & Qt.WindowType.WindowStaysOnTopHint) == bool(on):
            return
        visible = self.isVisible()
        self.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, on)
        if visible:
            self.show()

    def _aspect(self) -> float:
        size = self._player._video_widget.videoSink().videoSize()
        return size.width() / size.height() if size.isValid() and size.height() > 0 else 16 / 9

    def _min_w(self, aspect: float) -> int:
        return max(self._MIN_W, math.ceil(self._MIN_VH * aspect))

    def _fitted(self, rect: QRect) -> QRect:
        a, bar = self._aspect(), AnimePlayerWidget._BAR_H
        avail = _avail_for(rect)
        lo = self._min_w(a)
        hi = max(lo, min(avail.width(), int((avail.height() - bar) * a)))
        w = max(lo, min(rect.width(), hi))
        h = round(w / a) + bar
        x = max(avail.left(), min(rect.x(), avail.right() - w + 1))
        y = max(avail.top(), min(rect.y(), avail.bottom() - h + 1))
        return QRect(x, y, w, h)

    def fit_aspect(self) -> None:
        rect = self._fitted(self.geometry())
        if rect != self.geometry():
            self.setGeometry(rect)

    def _on_video_size(self) -> None:
        if self.isVisible() and self._rs is None:
            self.fit_aspect()

    def restore_geometry(self) -> None:
        rect = QSettings("PiraChest", "PiP").value("geometry")
        if not isinstance(rect, QRect) or not rect.isValid():
            screen = self.screen() or QGuiApplication.primaryScreen()
            avail = screen.availableGeometry() if screen is not None else QRect(0, 0, 1280, 720)
            w = max(320, min(480, avail.width() - 48))
            h = int(w * 9 / 16) + AnimePlayerWidget._BAR_H
            rect = QRect(avail.right() - w - 24, avail.bottom() - h - 24, w, h)
        self.setGeometry(self._fitted(rect))

    def save_geometry(self) -> None:
        if not self.isVisible():
            return
        try:
            rect = self.normalGeometry()
            if rect.isValid():
                QSettings("PiraChest", "PiP").setValue("geometry", rect)
        except Exception:
            logger.debug("Could not save PiP geometry", exc_info=True)

    def snapped(self, pos: QPoint) -> QPoint:
        w, h = self.width(), self.height()
        avail = _avail_for(QRect(pos, self.size()))
        x, y = pos.x(), pos.y()
        if abs(x - avail.left()) < self._SNAP:
            x = avail.left()
        elif abs(x + w - 1 - avail.right()) < self._SNAP:
            x = avail.right() - w + 1
        if abs(y - avail.top()) < self._SNAP:
            y = avail.top()
        elif abs(y + h - 1 - avail.bottom()) < self._SNAP:
            y = avail.bottom() - h + 1
        return QPoint(x, y)

    def begin_resize(self, edge: Qt.Edge, gpos: QPoint) -> None:
        rect = self.geometry()
        self._rs = (edge, gpos, rect, _avail_for(rect), self._aspect())

    def update_resize(self, gpos: QPoint) -> None:
        if self._rs is None:
            return
        if not (QGuiApplication.mouseButtons() & Qt.MouseButton.LeftButton):
            self.end_resize()
            return
        edge, g0, r, avail, a = self._rs
        bar = AnimePlayerWidget._BAR_H
        d = gpos - g0
        left, top = bool(edge & Qt.Edge.LeftEdge), bool(edge & Qt.Edge.TopEdge)
        cand = []
        if edge & (Qt.Edge.LeftEdge | Qt.Edge.RightEdge):
            cand.append(r.width() + (-d.x() if left else d.x()))
        if edge & (Qt.Edge.TopEdge | Qt.Edge.BottomEdge):
            cand.append(round((r.height() - bar + (-d.y() if top else d.y())) * a))
        w = max(cand, key=lambda c: abs(c - r.width()))
        lo = self._min_w(a)
        xlim = r.x() + r.width() - avail.left() if left else avail.right() + 1 - r.x()
        ylim = r.y() + r.height() - avail.top() if top else avail.bottom() + 1 - r.y()
        w = max(lo, min(w, max(r.width(), min(xlim, int((ylim - bar) * a)))))
        h = round(w / a) + bar
        rect = QRect(r.x() + r.width() - w if left else r.x(), r.y() + r.height() - h if top else r.y(), w, h)
        if rect != self.geometry():
            self.setGeometry(rect)

    def end_resize(self) -> None:
        if self._rs is not None:
            self._rs = None
            self._save_timer.start()

    def moveEvent(self, event) -> None:
        self._save_timer.start()
        super().moveEvent(event)
        self._player.pip_geometry_changed()

    def resizeEvent(self, event) -> None:
        self._save_timer.start()
        super().resizeEvent(event)
        self._player.pip_geometry_changed()

    def closeEvent(self, event) -> None:
        if self._player._pip_active:
            self._player.exit_pip()
            event.ignore()
            return
        self.save_geometry()
        super().closeEvent(event)


def _avail_for(rect: QRect) -> QRect:
    def area(screen) -> int:
        r = screen.availableGeometry().intersected(rect)
        return max(0, r.width()) * max(0, r.height())
    best = max(QGuiApplication.screens(), key=area, default=None)
    if best is None or area(best) == 0:
        best = QGuiApplication.screenAt(QCursor.pos()) or QGuiApplication.primaryScreen()
    return best.availableGeometry().adjusted(8, 8, -8, -8) if best is not None else QRect(0, 0, 1280, 720)


class AnimePlayerWidget(QWidget):
    close_requested = Signal()
    alt_stream_requested = Signal(dict)
    playback_error = Signal(str)
    stream_failed = Signal(str)
    ended = Signal()
    next_requested = Signal()
    _qualities_ready = Signal(list)
    _subtitle_cues_ready = Signal(list)
    _manifest_subs_ready = Signal(list)
    MAX_WIDTH = 640
    _MAX_LIVE_RECONNECTS = 5
    _RECONNECT_STABLE_SECS = 20
    _BAR_H = 54
    _VOD_MAX_RETRIES = 3
    _SKIP_INTRO_MS = 85_000

    _pirachest_no_kb_ring = True

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setMouseTracking(True)
        sp = QSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Preferred)
        sp.setHeightForWidth(True)
        self.setSizePolicy(sp)
        self.setMaximumWidth(self.MAX_WIDTH)
        self.setMinimumSize(240, 135 + self._BAR_H)

        self._title = ""
        self._seeking = False
        self._muted = False
        self._pre_mute_volume = self._load_volume()
        self._is_fullscreen = False
        self._hfw = True
        self._fs_win = self._fs_bar_win = None
        self._pip_active = False
        self._pip_win = None
        self._pip_layout = None
        self._pip_index = -1
        self._pip_align = Qt.AlignmentFlag(0)
        self._pip_placeholder = None
        self._pip_drag = None
        self._pip_dragging = False
        self._suppress_hide = False
        self._seek_target = 0
        self._seek_until = 0.0
        self._auto_paused = False
        self._is_live = False
        self._live_referer = None
        self._live_headers = None
        self._live_reconnect_attempts = 0
        self._reconnect_at = 0.0
        self._master_url = None
        self._master_final_url = None
        self._pending_seek = None
        self._external_subs: list[dict] = []
        self._cues: list[tuple[int, int, str]] = []
        self._cue_starts: list[int] = []
        self._cue_text = ""
        self._intended_embedded_track = -1
        self._speed = 1.0
        self._quality_opts: list[tuple[str, Optional[str]]] = []
        self._quality_cur = 0
        self._sub_entries: list[tuple[str, int, str]] = [("off", -1, "")]
        self._sub_cur = 0
        self._last_pos_sec = -1
        self._reconnect_pending = False
        self._variant_opts: list = []
        self._alt_opts: list = []
        self._quality_label = ""
        self._sub_gen = 0
        self._sub_pref: Optional[str] = ""
        self._stream_gen = 0
        self._vod_retries = 0
        self._failed_sent = False
        self._sub_delay_ms = 0
        self._click_timer = QTimer(self)
        self._click_timer.setSingleShot(True)
        self._click_timer.timeout.connect(self._toggle_play_pause)
        self._stall_timer = QTimer(self)
        self._stall_timer.setSingleShot(True)
        self._stall_timer.setInterval(20_000)
        self._stall_timer.timeout.connect(self._on_stall_timeout)

        self._qualities_ready.connect(self._on_qualities_ready)
        self._subtitle_cues_ready.connect(self._on_subtitle_cues_ready)
        self._manifest_subs_ready.connect(self._on_manifest_subs_ready)

        self._player = QMediaPlayer(self)
        self._audio_output = QAudioOutput(self)
        self._audio_output.setVolume(self._pre_mute_volume / 100)
        self._player.setAudioOutput(self._audio_output)
        self._video_widget = QVideoWidget(self)
        self._video_widget.setStyleSheet("background:black;")
        self._video_widget.setMouseTracking(True)
        self._player.setVideoOutput(self._video_widget)

        self._player.playbackStateChanged.connect(self._on_state_changed)
        self._player.positionChanged.connect(self._on_position_changed)
        self._player.durationChanged.connect(self._on_duration_changed)
        self._player.errorOccurred.connect(self._on_error)
        self._player.mediaStatusChanged.connect(self._on_media_status_changed)
        if hasattr(self._player, "tracksChanged"):
            self._player.tracksChanged.connect(self._refresh_subtitle_tracks)
        if hasattr(self._player, "activeSubtitleTrackChanged"):
            self._player.activeSubtitleTrackChanged.connect(self._on_active_subtitle_track_changed)

        self._status_lbl = QLabel("", self)
        self._status_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._refresh_status_style()
        register_theme_refresh(self, self._refresh_status_style)
        self._status_lbl.hide()

        bar = self._bar = _Bar(self)
        bar.setStyleSheet(_bar_qss())
        bar.setMouseTracking(True)
        bar.setFixedHeight(self._BAR_H)
        v = QVBoxLayout(bar)
        v.setContentsMargins(10, 5, 10, 4)
        v.setSpacing(0)

        seek_row = QHBoxLayout()
        seek_row.setSpacing(8)
        self._position_lbl = QLabel("0:00", bar)
        self._position_lbl.setMinimumWidth(34)
        self._seek_slider = _SeekSlider(Qt.Orientation.Horizontal, bar)
        self._seek_slider.setRange(0, 0)
        self._seek_slider.setFixedHeight(16)
        self._seek_slider.setMouseTracking(True)
        self._seek_slider.sliderMoved.connect(lambda x: self._position_lbl.setText(_format_ms(x)))
        self._seek_slider.sliderPressed.connect(lambda: setattr(self, "_seeking", True))
        self._seek_slider.sliderReleased.connect(self._on_seek_released)
        self._duration_lbl = QLabel("0:00", bar)
        self._duration_lbl.setMinimumWidth(34)
        self._duration_lbl.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        seek_row.addWidget(self._position_lbl)
        seek_row.addWidget(self._seek_slider, 1)
        seek_row.addWidget(self._duration_lbl)
        v.addLayout(seek_row)

        row = QHBoxLayout()
        row.setSpacing(2)
        v.addLayout(row)

        def tb(icon=None, tip="", slot=None, size=26, text=""):
            b = QToolButton(bar)
            b.setAutoRaise(True)
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            b.setFixedHeight(26)
            if icon is not None:
                b.setIcon(icon)
                b.setIconSize(QSize(16, 16))
                b.setFixedWidth(size)
            if text:
                b.setText(text)
            b.setToolTip(tip)
            if slot:
                b.clicked.connect(slot)
            return b

        self._skip_back_btn = tb(_wicon("SKIP_BACK", FluentIcon.LEFT_ARROW),
                                 tr("anime.rewind_30", default="Rewind 30s"), lambda: self._seek_relative(-30_000))
        self._play_btn = tb(_wicon("PAUSE", FluentIcon.PAUSE), "", self._toggle_play_pause, 30)
        self._skip_fwd_btn = tb(_wicon("SKIP_FORWARD", FluentIcon.RIGHT_ARROW),
                                tr("anime.forward_30", default="Forward 30s"), lambda: self._seek_relative(30_000))
        self._mute_btn = tb(_wicon("VOLUME", FluentIcon.VOLUME), tr("anime.mute", default="Mute"), self._toggle_mute)
        self._volume_slider = QSlider(Qt.Orientation.Horizontal, bar)
        self._volume_slider.setRange(0, 100)
        self._volume_slider.setValue(self._pre_mute_volume)
        self._volume_slider.setFixedSize(60, 16)
        self._volume_slider.setToolTip(tr("anime.volume", default="Volume"))
        self._volume_slider.valueChanged.connect(self._on_volume_changed)

        self._next_btn = tb(None, tr("anime.next_episode", default="Next episode"),
                            lambda: self.next_requested.emit(), text=tr("anime.next", default="Next") + " \u203a")
        self._next_btn.hide()
        self._has_next = False
        self._near_end = False
        self._ended_prompt = False
        self._skip_segs: dict = {"intro": [], "credits": []}
        self._skip_kind: Optional[str] = None
        self._skip_done: set = set()
        self._speed_btn = tb(None, tr("anime.playback_speed", default="Playback speed"), text="1x")
        self._quality_btn = tb(None, tr("anime.quality", default="Quality"), text="Auto")
        self._sub_btn = tb(None, tr("anime.subtitles", default="Subtitles"), text="CC")
        self._pip_btn = tb(_wicon("PICTURE_IN_PICTURE", FluentIcon.VIEW),
                           tr("anime.pip_enter", default="Picture in picture"), self.toggle_pip)
        self._fullscreen_btn = tb(_wicon("FULL_SCREEN", FluentIcon.VIEW), tr("anime.fullscreen", default="Fullscreen"),
                                  self.toggle_fullscreen)
        self._close_btn = tb(_wicon("CLOSE", FluentIcon.CLOSE), tr("anime.close_player", default="Close player"),
                             lambda: self.close_requested.emit())
        self._close_btn.hide()
        for b in (self._speed_btn, self._quality_btn, self._sub_btn):
            b.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
            m = QMenu(b)
            m.setStyleSheet(_menu_qss())
            b.setMenu(m)
        self._fill_menu(self._speed_btn, [(f"{s:g}x", s) for s in _SPEEDS], _SPEEDS.index(1.0), self._set_speed)

        for w in (self._skip_back_btn, self._play_btn, self._skip_fwd_btn, self._mute_btn, self._volume_slider):
            row.addWidget(w)
        row.addStretch(1)
        for w in (self._next_btn, self._speed_btn, self._quality_btn, self._sub_btn, self._pip_btn,
                  self._fullscreen_btn, self._close_btn):
            row.addWidget(w)
        self._quality_btn.hide()
        self._sub_btn.hide()

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        outer.addWidget(self._video_widget, 1)
        outer.addWidget(self._status_lbl)
        outer.addWidget(bar)
        self._busy_on = False
        self._busy_pos = 0
        self._outer = outer
        self._video_widget.installEventFilter(self)
        bar.installEventFilter(self)

        self._bubble = _SkipBubble(self)
        self._loader = _LoadingOverlay(self)
        self._subs = _SubOverlay(self)
        self._delay_popup = _SubDelayPopup(self)
        self._cue_timer = QTimer(self)
        self._cue_timer.setInterval(40)
        self._cue_timer.timeout.connect(self._tick_cues)
        self._delay_popup.changed.connect(self._set_sub_delay)

        self._hide_timer = QTimer(self)
        self._hide_timer.setInterval(2500)
        self._hide_timer.setSingleShot(True)
        self._hide_timer.timeout.connect(self._auto_hide_controls)


    @staticmethod
    def _load_volume() -> int:
        try:
            return max(1, min(100, int(QSettings("AnimePlayer", "Player").value("volume", 80))))
        except Exception:
            return 80

    def _osd(self, text: str) -> None:
        v = self._video_widget
        QToolTip.showText(v.mapToGlobal(QPoint(v.width() // 2 - 40, v.height() // 2)), text, v, QRect(), 900)

    def set_has_next(self, has: bool) -> None:
        self._has_next = bool(has)
        self._ended_prompt = False
        self._next_btn.setText(tr("anime.next", default="Next") + " \u203a")
        self._next_btn.setVisible(self._has_next and not self._is_live)
        self._refresh_bubble()

    def prompt_next(self) -> None:
        self._ended_prompt = True
        self._refresh_bubble()

    def set_skip_segments(self, intro=None, credits=None) -> None:
        self._skip_segs = {"intro": [list(s) for s in intro or []], "credits": [list(s) for s in credits or []]}
        self._skip_done.clear()
        self._near_end = self._ended_prompt = False
        self._set_skip_kind(None)

    def _set_skip_kind(self, kind: Optional[str]) -> None:
        self._skip_kind = kind
        self._refresh_bubble()

    def _refresh_bubble(self) -> None:
        kind = self._skip_kind
        skip = (None if kind is None else tr("tv.skip_intro", default="Skip Intro") if kind == "intro"
                else tr("tv.skip_credits", default="Skip Credits"))
        nxt = None
        if self._has_next and not self._is_live and (kind == "credits" or self._near_end or self._ended_prompt):
            nxt = tr("tv.next_episode_prompt", default="Next Episode") + " \u203a"
        self._bubble.set_state(skip, nxt)

    def _refresh_status_style(self) -> None:
        pal = palette()
        with suppress(RuntimeError):
            self._status_lbl.setStyleSheet(
                f"color:{pal['state_error']};background:{pal['card_bg']};"
                "padding:4px;border-radius:6px;font:12px 'Segoe UI';")

    def _skip_seg_to(self, end) -> None:
        dur = self._player.duration()
        if end is None or (dur > 0 and end >= dur - 1500):
            if dur <= 0:
                return
            end = dur - 250
        self._seek_to(max(0, int(end)))

    def _update_skip(self, ms: int) -> None:
        if self._is_live:
            return
        dur = self._player.duration()
        self._near_end = dur > 0 and 0 < dur - ms <= 35_000
        found = None
        for kind in ("intro", "credits"):
            for start, end in self._skip_segs[kind]:
                stop = dur if end is None else end
                if stop <= 0 or not (start <= ms < stop - 500):
                    continue
                if (getattr(settings, "tv_skip_intro" if kind == "intro" else "tv_skip_credits", False)
                        and (kind, start) not in self._skip_done):
                    self._skip_done.add((kind, start))
                    self._skip_seg_to(end)
                    return
                found = kind
                break
            if found:
                break
        self._set_skip_kind(found)

    def _do_skip(self) -> None:
        kind = self._skip_kind
        if not kind:
            return
        ms, dur = self._player.position(), self._player.duration()
        for start, end in self._skip_segs[kind]:
            if start <= ms < (dur if end is None else end):
                self._skip_seg_to(end)
                break
        self._set_skip_kind(None)


    def hasHeightForWidth(self) -> bool:
        return True

    def heightForWidth(self, width: int) -> int:
        return max(1, int(width * _ASPECT_H / _ASPECT_W)) + self._BAR_H

    def sizeHint(self):
        return QSize(self.MAX_WIDTH, self.heightForWidth(self.MAX_WIDTH))

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        if self._is_fullscreen or self._pip_active or not self._hfw:
            return
        h = self.heightForWidth(self.width())
        if self.minimumHeight() != h or self.maximumHeight() != h:
            self.setFixedHeight(h)

    def _set_status(self, text: str) -> None:
        if text:
            self._busy(False)
        self._status_lbl.setText(text)
        self._status_lbl.setVisible(bool(text))

    def _busy(self, on: bool) -> None:
        self._busy_on = on
        if on:
            self._busy_pos = self._player.position()
            if self._is_fullscreen:
                self._reveal_controls()
        self._loader.set_active(on)

    def _apply_theme(self) -> None:
        self._bar.setStyleSheet(_bar_qss())
        self._bar.update()
        for b in (self._speed_btn, self._quality_btn, self._sub_btn):
            b.menu().setStyleSheet(_menu_qss())

    def play_stream(self, url: str, referer: Optional[str] = None, headers: Optional[dict] = None,
                    title: str = "", subtitles: Optional[list[dict]] = None, start_ms: int = 0,
                    alt_streams: Optional[list[tuple[str, dict]]] = None, quality_label: str = "",
                    _reconnect: bool = False) -> None:
        self._apply_theme()
        if _is_local_stream_url(url):
            logger.warning("Refused a local stream url: %r", (url or "")[:120])
            self._set_status(tr("anime.no_stream_found", default="No playable stream was returned."))
            return
        self._title = title or ""
        self._live_referer = referer
        self._live_headers = headers
        if not _reconnect:
            self._live_reconnect_attempts = 0
            self._vod_retries = 0
            self._failed_sent = False
            self._sub_delay_ms = 0
        self._stream_gen += 1
        self._set_status("")
        self._busy(True)
        self._sub_gen += 1
        self._pending_seek = None
        self._external_subs = subtitles or []
        self._clear_cues()
        self._alt_opts = alt_streams or []
        self._quality_label = quality_label
        self._reset_quality()
        self._populate_subtitle_options()

        final_url = url
        relay = None
        is_hls = url.split("?")[0].lower().endswith(".m3u8")
        if is_hls or self._is_live or headers or referer:
            relay = get_relay()
            final_url = relay.set_stream(url, referer=referer, extra_headers=headers, live=self._is_live)
        self._master_url = url
        self._master_final_url = final_url

        self._player.stop()
        self._player.setSource(QUrl(final_url))
        self._player.setPlaybackRate(self._speed)
        self._intended_embedded_track = -1
        self._set_active_sub(-1)
        if start_ms > 1000 and not self._is_live:
            self._pending_seek = (start_ms, True)
        self._player.play()
        self._reveal_controls()
        if is_hls:
            self._load_qualities_async(url, referer, headers, relay, self._stream_gen)

    def stop(self) -> None:
        if self._pip_active:
            self.exit_pip(restore=False)
        self._player.stop()
        self._player.setSource(QUrl())
        self._clear_cues()
        self.set_skip_segments()

    def toggle_pip(self) -> None:
        self.exit_pip() if self._pip_active else self.enter_pip()

    def enter_pip(self) -> None:
        if self._pip_active or self._is_fullscreen or self._player.source().isEmpty():
            return
        parent = self.parentWidget()
        layout = parent.layout() if parent is not None else None
        if layout is None:
            return
        index = layout.indexOf(self)
        if index < 0:
            return
        item = layout.itemAt(index)
        align = item.alignment() if item is not None else Qt.AlignmentFlag(0)
        placeholder = QWidget(parent)
        placeholder.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        placeholder.setFixedHeight(max(120, self.height()))
        layout.removeWidget(self)
        layout.insertWidget(index, placeholder, 0, align)

        win = self._pip_win
        if win is None or not _qt_is_valid(win):
            win = self._pip_win = _PipWindow(self)
        win.set_always_on_top(bool(getattr(settings, "tv_pip_always_on_top", True)))

        self._pip_layout, self._pip_index = layout, index
        self._pip_align, self._pip_placeholder = align, placeholder
        self._pip_active = True
        self._bar.setMouseTracking(True)
        self._video_widget.setMouseTracking(True)
        self._pip_drag = None
        self._pip_dragging = False
        self._suppress_hide = True
        self._set_hfw(False)
        try:
            self.setMinimumSize(240, 135 + self._BAR_H)
            self.setMaximumWidth(_QWIDGETSIZE_MAX)
            self.setMinimumHeight(135 + self._BAR_H)
            self.setMaximumHeight(_QWIDGETSIZE_MAX)
            self.setParent(win)
            win.layout().addWidget(self)
            self.show()
            win.restore_geometry()
            win.show()
            win.raise_()
            win.activateWindow()
            self._swap_overlay(win)
            self.setFocus()
        finally:
            self._suppress_hide = False
        self._update_pip_button()

    def exit_pip(self, restore: bool = True) -> None:
        if not self._pip_active:
            return
        self._pip_active = False
        self._bar.unsetCursor()
        self._video_widget.unsetCursor()
        self._pip_drag = None
        self._pip_dragging = False
        self._suppress_hide = True
        win = self._pip_win
        layout = self._pip_layout
        try:
            if win is not None and _qt_is_valid(win):
                win.end_resize()
                win.save_geometry()
                win.layout().removeWidget(self)
                win.hide()
            if layout is not None:
                target = layout.parentWidget()
                self.setParent(target)
                placeholder = self._pip_placeholder
                if placeholder is not None and _qt_is_valid(placeholder):
                    layout.removeWidget(placeholder)
                    placeholder.setParent(None)
                    placeholder.deleteLater()
                self._pip_placeholder = None
                self.setMaximumWidth(self.MAX_WIDTH)
                self.setMinimumSize(240, 135 + self._BAR_H)
                self.setMaximumHeight(_QWIDGETSIZE_MAX)
                width = max(240, min(self.width(), self.MAX_WIDTH))
                self._set_hfw(True)
                layout.insertWidget(self._pip_index, self, 0, self._pip_align)
                self.resize(width, self.heightForWidth(width))
                self.setFixedHeight(self.heightForWidth(width))
            self._swap_overlay(self)
            if restore:
                self.show()
        except (RuntimeError, TypeError):
            logger.debug("PiP could not be restored cleanly", exc_info=True)
        finally:
            self._pip_layout = None
            self._pip_index = -1
            self._pip_placeholder = None
            self._suppress_hide = False
        self._update_pip_button()
        if restore:
            self.setFocus()
            self._restore_window()

    def _set_hfw(self, on: bool) -> None:
        self._hfw = on
        sp = self.sizePolicy()
        sp.setHeightForWidth(on)
        self.setSizePolicy(sp)

    def pip_geometry_changed(self) -> None:
        if not self._pip_active:
            return
        self._subs._follow(True)
        self._bubble.reposition()
        if self._loader._active:
            self._loader._tick()

    def _update_pip_button(self) -> None:
        active = self._pip_active
        self._pip_btn.setIcon(_wicon("BACK_TO_WINDOW" if active else "PICTURE_IN_PICTURE",
                                     FluentIcon.BACK_TO_WINDOW if active else FluentIcon.VIEW))
        self._pip_btn.setToolTip(tr("anime.pip_exit", default="Back to player")
                                 if active else tr("anime.pip_enter", default="Picture in picture"))

    def set_closable(self, on: bool) -> None:
        self._close_btn.setVisible(on)

    def title(self) -> str:
        return self._title

    def position_ms(self) -> int:
        return self._player.position()

    def has_media(self) -> bool:
        return not self._player.source().isEmpty() and self._player.playbackState() != QMediaPlayer.PlaybackState.StoppedState

    def set_live_mode(self, live: bool) -> None:
        self._is_live = live
        for w in (self._seek_slider, self._position_lbl, self._duration_lbl,
                  self._skip_back_btn, self._skip_fwd_btn, self._speed_btn):
            w.setVisible(not live)
        if live:
            self._set_speed(1.0)

    def shutdown(self) -> None:
        self._hide_timer.stop()
        self._stall_timer.stop()
        if self._pip_active:
            self.exit_pip(restore=False)
        pip_win, self._pip_win = self._pip_win, None
        if pip_win is not None:
            try:
                pip_win.close()
                pip_win.deleteLater()
            except (RuntimeError, TypeError):
                pass
        if self._fs_win is not None:
            self._fs_bar_win.close()
            self._fs_win.close()
        self._player.stop()
        self._player.setSource(QUrl())
        self._hide_overlays()
        self._stop_relay()

    def destroy(self) -> None:
        self._stream_gen += 1
        if self._is_fullscreen:
            self.exit_fullscreen()
        self.shutdown()
        for widget in (self._fs_bar_win, self._fs_win, self._bubble, self._loader, self._subs, self._delay_popup):
            if widget is not None:
                widget.hide()
                widget.setParent(None)
                widget.deleteLater()
        self.hide()
        self.setParent(None)
        self.deleteLater()

    def _hide_overlays(self) -> None:
        self._bubble.set_state(None, None)
        for overlay in (self._loader, self._subs, self._delay_popup):
            overlay.hide()

    def _stop_relay(self) -> None:
        try:
            get_relay().stop()
        except Exception:
            logger.debug("Could not release the stream relay", exc_info=True)


    def _playing(self) -> bool:
        return self._player.playbackState() == QMediaPlayer.PlaybackState.PlayingState

    def _schedule_reconnect(self, delay_ms: int) -> None:
        if self._reconnect_pending or not self._is_live:
            return
        self._reconnect_pending = True
        QTimer.singleShot(delay_ms + 500 * self._live_reconnect_attempts, self._reconnect_live)

    def _on_stall_timeout(self) -> None:
        if not self._master_url or self._player.playbackState() == QMediaPlayer.PlaybackState.StoppedState:
            return
        if self._is_live:
            self._schedule_reconnect(0)
            return
        if self._vod_retries >= self._VOD_MAX_RETRIES:
            self._fail_vod(tr("anime.stream_stalled", default="Stream stalled"))
            return
        self._vod_retries += 1
        src = self._player.source()
        self._pending_seek = (self._player.position(), True)
        self._player.setSource(src)

    def _fail_vod(self, msg: str) -> None:
        self._stall_timer.stop()
        self._player.stop()
        self._set_status(msg)
        if not self._failed_sent:
            self._failed_sent = True
            self.stream_failed.emit(msg)

    def _reconnect_live(self) -> None:
        self._reconnect_pending = False
        if not self._is_live or not self._master_url:
            return


        if time.monotonic() - self._reconnect_at >= self._RECONNECT_STABLE_SECS:
            self._live_reconnect_attempts = 0
        self._live_reconnect_attempts += 1
        if self._live_reconnect_attempts > self._MAX_LIVE_RECONNECTS:
            self._player.stop()
            self._set_status(tr("anime.stream_unavailable", default="Stream unavailable"))
            return
        self._reconnect_at = time.monotonic()
        self.play_stream(self._master_url, self._live_referer, self._live_headers, self._title,
                         self._external_subs, alt_streams=self._alt_opts, quality_label=self._quality_label, _reconnect=True)

    def _toggle_play_pause(self) -> None:
        if self._playing():
            self._player.pause()
        elif self._is_live and self._master_url:
            self._live_reconnect_attempts = 0
            self._reconnect_live()
        else:
            self._player.play()

    def _seek_to(self, ms: int) -> None:
        dur = self._player.duration()
        ms = max(0, min(ms, dur) if dur else ms)
        self._seek_target = ms
        self._seek_until = time.monotonic() + 1.2
        self._player.setPosition(ms)
        self._seek_slider.setValue(ms)
        self._position_lbl.setText(_format_ms(ms))
        if self._cues:
            self._update_subtitle(ms)

    def _seek_relative(self, delta_ms: int) -> None:
        base = self._seek_target if time.monotonic() < self._seek_until else self._player.position()
        self._seek_to(base + delta_ms)

    def _tick_cues(self) -> None:
        if self._cues:
            self._update_subtitle(self._player.position())

    def _sync_cue_timer(self) -> None:
        if self._cues and self._playing():
            self._cue_timer.start()
        else:
            self._cue_timer.stop()

    def _on_state_changed(self, _state) -> None:
        self._sync_cue_timer()
        if self._playing():
            self._play_btn.setIcon(_wicon("PAUSE", FluentIcon.PAUSE))
            self._hide_timer.start()
        else:
            self._play_btn.setIcon(_play_icon(QColor(255, 255, 255)))
            self._hide_timer.stop()
            self._reveal_controls()


        try:
            from src.gui.settings_page import get_sound_manager
            get_sound_manager().set_media_playing('tv', self._playing(), owner=self)
        except Exception:
            logger.debug('Ambiance ducking update failed', exc_info=True)

    def _on_position_changed(self, ms: int) -> None:
        if self._busy_on and ms != self._busy_pos and self._playing():
            self._busy(False)
            self._vod_retries = 0
        if self._cues:
            self._update_subtitle(ms)
        self._update_skip(ms)
        if not self._bar.isVisible() or self._seeking:
            return
        sec = ms // 1000
        if sec != self._last_pos_sec:
            self._last_pos_sec = sec
            self._position_lbl.setText(_format_ms(ms))
        self._seek_slider.setValue(ms)

    def _on_duration_changed(self, ms: int) -> None:
        self._seek_slider.setRange(0, max(0, ms))
        self._duration_lbl.setText(_format_ms(ms))

    def _on_seek_released(self) -> None:
        self._seeking = False
        self._seek_to(self._seek_slider.value())

    def _on_media_status_changed(self, status) -> None:
        S = QMediaPlayer.MediaStatus
        if self._is_live and status in (S.StalledMedia, S.EndOfMedia):
            self._schedule_reconnect(1000)
            return
        if status == S.EndOfMedia:
            self._stall_timer.stop()
            self.ended.emit()
            return
        if status in (S.LoadingMedia, S.StalledMedia, S.BufferingMedia):
            self._busy(True)
            if not self._stall_timer.isActive():
                self._stall_timer.start()
        elif status in (S.LoadedMedia, S.BufferedMedia):
            self._busy(False)
            self._stall_timer.stop()
        if status == S.LoadedMedia:
            self._refresh_subtitle_tracks()
            if self._pending_seek is not None:
                pos, was_playing = self._pending_seek
                self._pending_seek = None
                self._player.setPosition(pos)
                self._player.play() if was_playing else self._player.pause()

    def _on_error(self, _error, msg: str) -> None:
        logger.warning("Player error: %s", msg)
        self._set_status(msg or tr("anime.playback_error", default="Playback error"))
        self.playback_error.emit(str(msg or ""))
        self._schedule_reconnect(2000)
        if not self._is_live and not self._failed_sent:
            self._failed_sent = True
            self._stall_timer.stop()
            self.stream_failed.emit(str(msg or ""))


    def _on_volume_changed(self, value: int) -> None:
        self._audio_output.setVolume(value / 100)
        self._muted = value == 0
        self._mute_btn.setIcon(_wicon("MUTE", FluentIcon.MUTE) if self._muted else _wicon("VOLUME", FluentIcon.VOLUME))
        if value > 0:
            self._pre_mute_volume = value
            try:
                QSettings("AnimePlayer", "Player").setValue("volume", value)
            except Exception:
                pass

    def _toggle_mute(self) -> None:
        if self._muted:
            self._volume_slider.setValue(self._pre_mute_volume or 80)
        else:
            self._pre_mute_volume = self._volume_slider.value() or self._pre_mute_volume
            self._volume_slider.setValue(0)

    def _set_speed(self, speed: float) -> None:
        self._speed = speed
        self._player.setPlaybackRate(speed)
        self._speed_btn.setText(f"{speed:g}x")
        self._fill_menu(self._speed_btn, [(f"{s:g}x", s) for s in _SPEEDS], _SPEEDS.index(speed), self._set_speed)

    def _step_speed(self, direction: int) -> None:
        i = min(range(len(_SPEEDS)), key=lambda k: abs(_SPEEDS[k] - self._speed))
        j = max(0, min(len(_SPEEDS) - 1, i + direction))
        self._set_speed(_SPEEDS[j])
        self._osd(f"{_SPEEDS[j]:g}x")

    def _cycle_subtitles(self) -> None:
        if len(self._sub_entries) < 2:
            return
        idx = (self._sub_cur + 1) % len(self._sub_entries)
        self._on_subtitle_picked(idx)
        k, _x, lbl = self._sub_entries[idx]
        self._osd(tr("anime.subtitles_off", default="Subtitles: Off") if k == "off" else lbl)

    def _fill_menu(self, btn, entries: list, current: int, cb) -> None:
        QTimer.singleShot(0, lambda: self._fill_menu_now(btn, entries, current, cb))

    @staticmethod
    def _fill_menu_now(btn: QToolButton, entries: list, current: int, cb) -> None:
        menu = btn.menu()
        menu.clear()
        grp = QActionGroup(menu)
        for i, (label, data) in enumerate(entries):
            act = menu.addAction(label)
            act.setCheckable(True)
            act.setChecked(i == current)
            grp.addAction(act)
            act.triggered.connect(partial(cb, data))


    def _reset_quality(self) -> None:
        self._variant_opts = []
        self._quality_opts = []
        self._quality_cur = 0
        self._rebuild_quality()

    def _rebuild_quality(self) -> None:
        opts: list = list(self._variant_opts) or ([(self._quality_label, None)] if self._quality_label else [])
        opts += [(lbl, st) for lbl, st in self._alt_opts]
        self._quality_opts = opts
        self._quality_cur = min(self._quality_cur, max(0, len(opts) - 1))
        if len(opts) <= 1:
            self._quality_btn.hide()
            return
        self._quality_btn.setText(opts[self._quality_cur][0])
        self._fill_menu(self._quality_btn, [(l, i) for i, (l, _u) in enumerate(opts)], self._quality_cur, self._on_quality_picked)
        self._quality_btn.show()

    def _on_qualities_ready(self, options: list) -> None:
        if len(options) <= 1:
            logger.info("Stream exposes a single quality (no extra HLS variants): %s", self._master_url)
            return
        self._variant_opts = options
        self._quality_cur = 0
        self._rebuild_quality()

    def _on_quality_picked(self, idx: int) -> None:
        if idx == self._quality_cur or not (0 <= idx < len(self._quality_opts)):
            return
        label, payload = self._quality_opts[idx]
        if isinstance(payload, dict):
            self.alt_stream_requested.emit(payload)
            return
        self._quality_cur = idx
        self._quality_btn.setText(label)
        src = payload if payload is not None else self._master_final_url
        if not src:
            return
        self._pending_seek = (self._player.position(), self._playing())
        self._player.setSource(QUrl(src))

    def _load_qualities_async(self, master_url, referer, headers, relay, gen: int) -> None:
        def _worker() -> None:
            try:
                text = relay.fetch_text(master_url) if relay else _ad_hoc_client.get(master_url, timeout=15).text
            except Exception as exc:
                logger.debug("Quality probe failed for %s: %s", master_url, exc)
                return
            if gen != self._stream_gen:
                return
            variants = _parse_hls_variants(text, master_url)
            if variants:
                opts: list = [(tr("anime.quality_auto", default="Auto"), None)]
                for height, bw, vurl in variants:
                    try:
                        local = relay.register(vurl) if relay else vurl
                    except Exception:
                        local = vurl
                    opts.append((f"{height}p" if height else f"{max(1, bw // 1000)} kbps", local))
                self._qualities_ready.emit(opts)
            resolved: list[dict] = []
            for group in _parse_hls_subtitle_groups(text, master_url):
                vtt = group["url"]
                if vtt.split("?")[0].endswith(".m3u8"):
                    try:
                        st = relay.fetch_text(vtt) if relay else _ad_hoc_client.get(vtt, timeout=15).text
                    except Exception:
                        continue
                    if "WEBVTT" not in st[:32].upper():
                        inner = next((l.strip() for l in st.splitlines() if l.strip() and not l.strip().startswith("#")), None)
                        if not inner:
                            continue
                        vtt = urljoin(vtt, inner)
                if relay is not None:
                    try:
                        vtt = relay.register(vtt)
                    except Exception:
                        pass
                resolved.append({**group, "url": vtt})
            if resolved and gen == self._stream_gen:
                self._manifest_subs_ready.emit(resolved)

        threading.Thread(target=_worker, daemon=True).start()


    def _clear_cues(self) -> None:
        self._cues = []
        if hasattr(self, "_cue_timer"):
            self._cue_timer.stop()
        self._cue_starts = []
        self._cue_text = ""
        self._show_sub_text("")

    def _show_sub_text(self, text: str) -> None:
        subs = getattr(self, "_subs", None)
        if subs is not None:
            subs.set_text(text)

    def _on_manifest_subs_ready(self, subs: list) -> None:
        if self._external_subs:
            return
        self._external_subs = subs
        self._populate_subtitle_options()

    def _on_subtitle_cues_ready(self, cues: list) -> None:
        cues = sorted(cues, key=lambda c: c[0])
        self._cues = cues
        self._cue_starts = [c[0] for c in cues]
        self._cue_text = ""
        self._show_sub_text("")
        if cues:
            self._update_subtitle(self._player.position())
        self._sync_cue_timer()

    def _set_sub_delay(self, ms: int) -> None:
        self._sub_delay_ms = ms
        self._cue_text = ""
        if self._cues:
            self._update_subtitle(self._player.position())

    def _nudge_sub_delay(self, delta_ms: int) -> None:
        if not self._cues:
            self._osd(tr("anime.sub_delay_na", default="No external subtitles to shift"))
            return
        self._set_sub_delay(self._sub_delay_ms + delta_ms)
        self._osd(tr("anime.sub_delay", default="Subtitle delay: {ms} ms").format(ms=f"{self._sub_delay_ms:+d}"))

    def _show_delay_popup(self) -> None:
        if not self._cues:
            self._osd(tr("anime.sub_delay_na", default="No external subtitles to shift"))
            return
        self._delay_popup.popup(self._sub_delay_ms, self._sub_btn.mapToGlobal(QPoint(0, 0)))

    def _update_subtitle(self, ms: int) -> None:
        ms -= self._sub_delay_ms
        i = bisect_right(self._cue_starts, ms) - 1
        parts = []
        for k in range(i, max(-1, i - 4), -1):
            c = self._cues[k]
            if c[1] >= ms:
                parts.append(c[2])
        text = "\n".join(reversed(parts))
        if text != self._cue_text:
            self._cue_text = text
            self._show_sub_text(text)

    def _load_subtitle_async(self, url: str) -> None:
        gen = self._sub_gen
        headers = {"User-Agent": _UA}
        if self._live_referer:
            headers["Referer"] = self._live_referer
        headers.update(self._live_headers or {})

        def _get(u: str) -> str:
            r = _ad_hoc_client.get(u, headers=headers, timeout=15)
            r.raise_for_status()
            return r.content.decode("utf-8-sig", errors="replace")

        def _worker() -> None:
            cues: list = []
            try:
                text = _get(url)
                if text.lstrip().startswith("#EXTM3U"):
                    segs, off, dur = [], 0, 0.0
                    for ln in text.splitlines():
                        ln = ln.strip()
                        if ln.startswith("#EXTINF:"):
                            dur = float(ln[8:].split(",")[0] or 0)
                        elif ln and not ln.startswith("#"):
                            segs.append((urljoin(url, ln), off))
                            off += int(dur * 1000)
                    with ThreadPoolExecutor(6) as ex:
                        parts = list(ex.map(lambda so: (so[1], _get(so[0])), segs[:600]))
                    for off, body in parts:
                        cues.extend((a + off, b + off, t) for a, b, t in _parse_subtitle_cues(body))
                    cues.sort(key=lambda c: c[0])
                else:
                    cues = _parse_subtitle_cues(text)
            except Exception as exc:
                logger.warning("Subtitle fetch failed for %s: %s", url, exc)
            if gen == self._sub_gen:
                self._subtitle_cues_ready.emit(cues)

        threading.Thread(target=_worker, daemon=True).start()

    def _set_active_sub(self, idx: int) -> None:
        if hasattr(self._player, "setActiveSubtitleTrack"):
            try:
                self._player.setActiveSubtitleTrack(idx)
            except Exception:
                pass

    def _on_active_subtitle_track_changed(self, index: int) -> None:
        if index != self._intended_embedded_track:
            self._set_active_sub(self._intended_embedded_track)

    def _rebuild_sub_menu(self) -> None:
        self._sub_btn.setVisible(len(self._sub_entries) > 1)
        self._sub_btn.setText("CC" if self._sub_cur == 0 else "CC \u25cf")
        QTimer.singleShot(0, self._rebuild_sub_menu_now)

    def _rebuild_sub_menu_now(self) -> None:
        menu = self._sub_btn.menu()
        menu.clear()
        grp = QActionGroup(menu)
        off = tr("anime.subtitles_off", default="Subtitles: Off")
        for i, (k, _x, lbl) in enumerate(self._sub_entries):
            act = menu.addAction(off if k == "off" else lbl)
            act.setCheckable(True)
            act.setChecked(i == self._sub_cur)
            grp.addAction(act)
            act.triggered.connect(partial(self._on_subtitle_picked, i))
        menu.addSeparator()
        delay = menu.addAction(tr("anime.sub_delay_menu", default="Delay\u2026"))
        delay.triggered.connect(self._show_delay_popup)
        style = menu.addMenu(tr("anime.sub_style", default="Style"))
        style.setStyleSheet(_menu_qss())
        for title, attr, opts in (
                (tr("anime.sub_size", default="Size"), "_scale", (("S", 0.8), ("M", 1.0), ("L", 1.4), ("XL", 1.8))),
                (tr("anime.sub_color_menu", default="Color"), "_color", (("White", 0), ("Yellow", 1), ("Green", 2), ("Cyan", 3))),
                (tr("anime.sub_font", default="Font"), "_font", tuple((f, i) for i, f in enumerate(_SubOverlay._FONTS))),
                (tr("anime.sub_outline", default="Outline"), "_outline", (("None", 0), ("Thin", 1), ("Normal", 2), ("Thick", 3))),
                (tr("anime.sub_bg_menu", default="Background"), "_bg", (("None", 0), ("Dark", 1), ("Blue", 2)))):
            sub = style.addMenu(title)
            sub.setStyleSheet(_menu_qss())
            group = QActionGroup(sub)
            for label, val in opts:
                act = sub.addAction(label)
                act.setCheckable(True)
                act.setChecked(abs(getattr(self._subs, attr) - val) < 0.05)
                group.addAction(act)
                act.triggered.connect(partial(self._set_sub_style, attr, val))
        style.addSeparator()
        style.addAction(tr("anime.sub_reset_style", default="Reset position")).triggered.connect(
            lambda: self._subs.restyle(reset=True))

    def _set_sub_style(self, attr: str, val, _checked: bool = False) -> None:
        setattr(self._subs, attr, val)
        self._subs._style()
        self._subs.restyle()

    def _populate_subtitle_options(self) -> None:
        self._sub_entries = [("off", -1, "")]
        for i, sub in enumerate(self._external_subs):
            label = sub.get("label") or sub.get("srclang") or f"{tr('anime.subtitle', default='Subtitle')} {i + 1}"
            self._sub_entries.append(("external", i, label))
        pick = 0
        if self._sub_pref != "" and len(self._sub_entries) > 1:
            subs = self._external_subs

            def _is_en(i: int) -> bool:
                lab = (self._sub_entries[i + 1][2] or "").lower()
                return str(subs[i].get("srclang", "")).lower().startswith("en") or lab.startswith("en")

            order = [i for i in range(len(subs))]
            cand = None
            if self._sub_pref:
                cand = next((i for i in order if self._sub_pref in self._sub_entries[i + 1][2].lower()), None)
            if cand is None:
                cand = next((i for i in order if subs[i].get("default")), None)
            if cand is None:
                cand = next((i for i in order if _is_en(i)), None)
            if cand is None and self._sub_pref is None:
                cand = 0
            if cand is not None:
                pick = cand + 1
        self._sub_cur = pick
        self._rebuild_sub_menu()
        if pick > 0:
            self._apply_subtitle(pick)

    def _refresh_subtitle_tracks(self) -> None:
        self._sub_entries = [e for e in self._sub_entries if e[0] != "embedded"]
        try:
            tracks = self._player.subtitleTracks() if hasattr(self._player, "subtitleTracks") else []
        except Exception:
            tracks = []
        for i, meta in enumerate(tracks):
            self._sub_entries.append(("embedded", i, self._describe_subtitle_track(meta, i)))
        self._sub_cur = min(self._sub_cur, len(self._sub_entries) - 1)
        self._rebuild_sub_menu()
        kind, idx, _l = self._sub_entries[self._sub_cur]
        self._intended_embedded_track = idx if kind == "embedded" else -1
        self._set_active_sub(self._intended_embedded_track)

    @staticmethod
    def _describe_subtitle_track(meta, index: int) -> str:
        try:
            from PySide6.QtMultimedia import QMediaMetaData
            title = meta.stringValue(QMediaMetaData.Key.Title)
            lang = meta.value(QMediaMetaData.Key.Language)
            if lang is not None:
                try:
                    from PySide6.QtCore import QLocale
                    name = QLocale.languageToString(QLocale.Language(lang))
                except Exception:
                    name = str(lang)
                return title or name or f"Track {index + 1}"
            if title:
                return title
        except Exception:
            pass
        return f"Track {index + 1}"

    def _on_subtitle_picked(self, idx: int) -> None:
        if not (0 <= idx < len(self._sub_entries)):
            return
        kind, _i, label = self._sub_entries[idx]
        self._sub_pref = "" if kind == "off" else label.lower()
        self._apply_subtitle(idx)

    def _apply_subtitle(self, idx: int) -> None:
        self._sub_gen += 1
        self._sub_cur = idx
        kind, i, _l = self._sub_entries[idx]
        self._intended_embedded_track = i if kind == "embedded" else -1
        self._set_active_sub(self._intended_embedded_track)
        self._clear_cues()
        self._rebuild_sub_menu()
        if kind == "external" and 0 <= i < len(self._external_subs):
            url = self._external_subs[i].get("url")
            if url:
                self._load_subtitle_async(url)


    def toggle_fullscreen(self) -> None:
        self.exit_fullscreen() if self._is_fullscreen else self.enter_fullscreen()

    def _vid(self) -> QVideoWidget:
        return self._video_widget

    def _pip_edge(self, pos) -> Qt.Edge:
        if not self._pip_active:
            return Qt.Edge(0)
        w, h, x, y = self.width(), self.height(), pos.x(), pos.y()
        m, c = 8, 18
        edge = Qt.Edge(0)
        if x <= m or (x <= c and (y <= c or y >= h - c)):
            edge |= Qt.Edge.LeftEdge
        elif x >= w - m or (x >= w - c and (y <= c or y >= h - c)):
            edge |= Qt.Edge.RightEdge
        if y <= m or (y <= c and (x <= c or x >= w - c)):
            edge |= Qt.Edge.TopEdge
        elif y >= h - m or (y >= h - c and (x <= c or x >= w - c)):
            edge |= Qt.Edge.BottomEdge
        return edge

    def _pip_cursor(self, obj: QWidget, edge: Qt.Edge) -> None:
        horiz = bool(edge & (Qt.Edge.LeftEdge | Qt.Edge.RightEdge))
        vert = bool(edge & (Qt.Edge.TopEdge | Qt.Edge.BottomEdge))
        if horiz and vert:
            diag = bool(edge & Qt.Edge.LeftEdge) == bool(edge & Qt.Edge.TopEdge)
            obj.setCursor(Qt.CursorShape.SizeFDiagCursor if diag else Qt.CursorShape.SizeBDiagCursor)
        elif horiz:
            obj.setCursor(Qt.CursorShape.SizeHorCursor)
        elif vert:
            obj.setCursor(Qt.CursorShape.SizeVerCursor)
        else:
            obj.unsetCursor()

    def _pip_mouse(self, obj: QWidget, event, t) -> bool:
        win = self._pip_win
        left = Qt.MouseButton.LeftButton
        if t == QEvent.Type.MouseMove:
            gp = event.globalPosition().toPoint()
            if win.resizing:
                win.update_resize(gp)
                return True
            if self._pip_drag is not None and (event.buttons() & left):
                origin, start = self._pip_drag
                delta = gp - origin
                if self._pip_dragging or delta.manhattanLength() > 4:
                    self._pip_dragging = True
                    self._click_timer.stop()
                    win.move(win.snapped(start + delta))
                return True
            self._pip_cursor(obj, self._pip_edge(obj.mapTo(self, event.position().toPoint())))
        elif t == QEvent.Type.MouseButtonPress and event.button() == left:
            edge = self._pip_edge(obj.mapTo(self, event.position().toPoint()))
            gp = event.globalPosition().toPoint()
            if edge:
                self._click_timer.stop()
                win.begin_resize(edge, gp)
                return True
            self._pip_drag = (gp, win.pos())
            self._pip_dragging = False
            return obj is self._bar
        elif t == QEvent.Type.MouseButtonRelease and event.button() == left:
            if win.resizing:
                win.end_resize()
                return True
            if self._pip_drag is not None:
                dragged = self._pip_dragging
                self._pip_drag = None
                self._pip_dragging = False
                return dragged or obj is self._bar
        elif t == QEvent.Type.MouseButtonDblClick:
            return bool(self._pip_edge(obj.mapTo(self, event.position().toPoint())))
        return False

    def _ensure_fs(self) -> None:
        if self._fs_win is not None:
            return
        w = QWidget(None, Qt.WindowType.Window | Qt.WindowType.FramelessWindowHint)
        w.setStyleSheet("background:black;")
        w.setMouseTracking(True)
        lay = QVBoxLayout(w)
        lay.setContentsMargins(0, 0, 0, 0)
        w.installEventFilter(self)
        bw = QWidget(w, Qt.WindowType.Tool | Qt.WindowType.FramelessWindowHint
                     | Qt.WindowType.WindowDoesNotAcceptFocus | Qt.WindowType.NoDropShadowWindowHint)
        bw.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        bw.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        bl = QVBoxLayout(bw)
        bl.setContentsMargins(0, 0, 0, 0)
        self._fs_win, self._fs_bar_win = w, bw

    def _swap_overlay(self, owner: QWidget) -> None:
        self._bubble.hide()
        self._bubble.deleteLater()
        self._bubble = _SkipBubble(self, owner)
        self._refresh_bubble()
        self._loader.set_active(False)
        self._loader.deleteLater()
        self._loader = _LoadingOverlay(self, owner)
        old, text = self._subs, self._subs.text()
        old.set_text("")
        old.deleteLater()
        self._subs = _SubOverlay(self, owner)
        self._subs.set_text(text)
        self._loader.set_active(self._busy_on)

    def enter_fullscreen(self) -> None:
        if self._is_fullscreen:
            return
        if self._pip_active:
            self.exit_pip(restore=False)
        self._ensure_fs()
        self._is_fullscreen = True
        self._fullscreen_btn.setIcon(_wicon("BACK_TO_WINDOW", FluentIcon.CLOSE))
        self._outer.removeWidget(self._video_widget)
        self._video_widget.setParent(self._fs_win)
        self._fs_win.layout().addWidget(self._video_widget)
        self._video_widget.show()
        screen = self.screen()
        if screen is not None:
            self._fs_win.setGeometry(screen.geometry())
        self._fs_win.showFullScreen()
        self._nudge_frame()
        self._outer.removeWidget(self._bar)
        self._bar.setParent(self._fs_bar_win)
        self._bar.floating = True
        self._bar.setFixedSize(self._fs_bar_w(), self._BAR_H)
        self._fs_bar_win.layout().addWidget(self._bar)
        self._bar.show()
        self._swap_overlay(self._fs_win)
        self._fs_win.activateWindow()
        self._fs_win.setFocus()
        self._reveal_controls()

    def exit_fullscreen(self) -> None:
        if not self._is_fullscreen:
            return
        self._hide_timer.stop()
        self._fs_bar_win.hide()
        self._fs_bar_win.layout().removeWidget(self._bar)
        self._is_fullscreen = False
        self._fs_win.layout().removeWidget(self._video_widget)
        self._video_widget.setParent(self)
        self._outer.insertWidget(0, self._video_widget, 1)
        self._video_widget.show()
        self._nudge_frame()
        self._bar.floating = False
        self._bar.setParent(self)
        self._bar.setFixedHeight(self._BAR_H)
        self._bar.setMinimumWidth(0)
        self._bar.setMaximumWidth(_QWIDGETSIZE_MAX)
        self._outer.addWidget(self._bar)
        self._bar.show()
        self._fullscreen_btn.setIcon(_wicon("FULL_SCREEN", FluentIcon.VIEW))
        self._fs_win.hide()
        self._fs_win.unsetCursor()
        self._swap_overlay(self)
        self.setFocus()
        self._restore_window()

    def _restore_window(self) -> None:
        win = self.window()
        if win is None:
            return
        with suppress(Exception):
            win.updateGeometry()

        def refresh() -> None:
            if not _qt_is_valid(win):
                return
            chrome = getattr(win, '_chrome', None)
            if chrome is not None:
                with suppress(Exception):
                    chrome.refresh()
            with suppress(Exception):
                win.titleBar.raise_()
            with suppress(Exception):
                win.raise_()
                win.activateWindow()
            self.raise_()

        QTimer.singleShot(0, refresh)

    def _nudge_frame(self) -> None:
        if not self._playing() and not self._player.source().isEmpty():
            self._player.setPosition(self._player.position())

    def _fs_bar_w(self) -> int:
        return max(320, min(900, int(self._fs_win.width() * 0.8)))

    def _place_fs_bar(self) -> None:
        w = self._fs_win
        if w is None or not self._is_fullscreen:
            return
        bw = self._fs_bar_w()
        self._bar.setFixedSize(bw, self._BAR_H)
        g = w.mapToGlobal(QPoint(0, 0))
        self._fs_bar_win.setGeometry(g.x() + (w.width() - bw) // 2, g.y() + w.height() - self._BAR_H - 36, bw, self._BAR_H)


    def _reveal_controls(self, zone: bool = True) -> None:
        if not self._is_fullscreen:
            return
        self._fs_win.unsetCursor()
        if zone:
            if not self._fs_bar_win.isVisible():
                self._place_fs_bar()
                self._fs_bar_win.show()
        if self._playing() or not zone:
            self._hide_timer.start(2500 if zone else 700)
        else:
            self._hide_timer.stop()

    def _auto_hide_controls(self) -> None:
        if not self._is_fullscreen or not self._playing():
            return
        if self._fs_bar_win.underMouse() or any(b.menu().isVisible() for b in (self._speed_btn, self._quality_btn, self._sub_btn)):
            self._hide_timer.start(2500)
            return
        self._fs_bar_win.hide()
        self._fs_win.setCursor(Qt.CursorShape.BlankCursor)

    def eventFilter(self, obj, event) -> bool:
        t = event.type()
        fs = self._is_fullscreen and obj is self._video_widget
        if obj is self._fs_win and t == QEvent.Type.KeyPress:
            self.keyPressEvent(event)
            return True
        if obj is self._fs_win and t in (QEvent.Type.Resize, QEvent.Type.Move) and self._is_fullscreen:
            if self._fs_bar_win is not None and self._fs_bar_win.isVisible():
                QTimer.singleShot(0, self._place_fs_bar)
        if self._pip_active and obj in (self._bar, self._video_widget) and self._pip_mouse(obj, event, t):
            return True
        if obj is self._video_widget:
            if t == QEvent.Type.MouseMove and fs:
                in_zone = event.position().y() >= obj.height() - (self._BAR_H + 70)
                self._reveal_controls(zone=in_zone)
            elif t == QEvent.Type.MouseButtonPress and event.button() == Qt.MouseButton.LeftButton:
                from PySide6.QtWidgets import QApplication
                self._click_timer.start(min(QApplication.doubleClickInterval(), 300))
                return True
            elif t == QEvent.Type.Wheel:
                self._volume_slider.setValue(max(0, min(100, self._volume_slider.value() + (5 if event.angleDelta().y() > 0 else -5))))
                self._osd(tr("anime.volume_pct", default="Volume {v}%").format(v=self._volume_slider.value()))
                return True
            elif t == QEvent.Type.MouseButtonDblClick:
                self._click_timer.stop()
                self.toggle_fullscreen()
                return True
        return super().eventFilter(obj, event)


    def hideEvent(self, event) -> None:
        self._hide_overlays()
        if not self._suppress_hide and not self._is_fullscreen and not self._pip_active:
            self.setMinimumHeight(135 + self._BAR_H)
            self.setMaximumHeight(_QWIDGETSIZE_MAX)
        if not self._suppress_hide and not self._pip_active and self._playing():
            self._player.pause()
            self._auto_paused = True
        super().hideEvent(event)

    def showEvent(self, event) -> None:
        if self._auto_paused:
            self._player.play()
            self._auto_paused = False
        super().showEvent(event)
        if not self._is_fullscreen and not self._pip_active and self._hfw:
            h = self.heightForWidth(self.width())
            if self.minimumHeight() != h or self.maximumHeight() != h:
                self.setFixedHeight(h)

    def keyPressEvent(self, event) -> None:
        key = event.key()
        shift = bool(event.modifiers() & Qt.KeyboardModifier.ShiftModifier)
        self._reveal_controls()
        vod = not self._is_live
        if key == Qt.Key.Key_Escape and self._pip_active:
            self.exit_pip()
            return
        if key in (Qt.Key.Key_Space, Qt.Key.Key_K):
            self._toggle_play_pause()
        elif key in (Qt.Key.Key_F, Qt.Key.Key_F11):
            self.toggle_fullscreen()
        elif key == Qt.Key.Key_Escape and self._is_fullscreen:
            self.exit_fullscreen()
        elif key in (Qt.Key.Key_Left, Qt.Key.Key_Right) and vod:
            d = 30_000 if shift else 5_000
            self._seek_relative(-d if key == Qt.Key.Key_Left else d)
            self._osd(f"{'-' if key == Qt.Key.Key_Left else '+'}{d // 1000}s")
        elif key in (Qt.Key.Key_J, Qt.Key.Key_L) and vod:
            self._seek_relative(-10_000 if key == Qt.Key.Key_J else 10_000)
            self._osd("-10s" if key == Qt.Key.Key_J else "+10s")
        elif key == Qt.Key.Key_S and vod:
            self._seek_relative(self._SKIP_INTRO_MS)
            self._osd(tr("anime.skip_intro", default="Skipped +{s}s").format(s=self._SKIP_INTRO_MS // 1000))
        elif key == Qt.Key.Key_Home and vod:
            self._seek_to(0)
        elif key == Qt.Key.Key_End and vod and self._player.duration():
            self._seek_to(self._player.duration() - 1000)
        elif Qt.Key.Key_0 <= key <= Qt.Key.Key_9 and vod and self._player.duration():
            self._seek_to(self._player.duration() * (key - Qt.Key.Key_0) // 10)
        elif key == Qt.Key.Key_Up:
            self._volume_slider.setValue(min(100, self._volume_slider.value() + 5))
            self._osd(tr("anime.volume_pct", default="Volume {v}%").format(v=self._volume_slider.value()))
        elif key == Qt.Key.Key_Down:
            self._volume_slider.setValue(max(0, self._volume_slider.value() - 5))
            self._osd(tr("anime.volume_pct", default="Volume {v}%").format(v=self._volume_slider.value()))
        elif key == Qt.Key.Key_M:
            self._toggle_mute()
        elif key == Qt.Key.Key_BracketRight and vod:
            self._step_speed(1)
        elif key == Qt.Key.Key_BracketLeft and vod:
            self._step_speed(-1)
        elif key == Qt.Key.Key_C:
            self._cycle_subtitles()
        elif key == Qt.Key.Key_Z:
            self._nudge_sub_delay(-100)
        elif key == Qt.Key.Key_X:
            self._nudge_sub_delay(100)
        elif key == Qt.Key.Key_N and self._next_btn.isVisible():
            self.next_requested.emit()
        else:
            super().keyPressEvent(event)
            return
        event.accept()
