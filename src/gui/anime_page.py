from __future__ import annotations

import logging
import os
import re
import threading
import urllib.parse
from collections import OrderedDict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Optional
from urllib.parse import urljoin

import requests
from PySide6.QtCore import (
    Qt, QSize, Signal, QTimer, QRect, QEvent, QEasingCurve, QUrl,
    QPropertyAnimation, QParallelAnimationGroup, QPoint, QVariantAnimation, QObject,
)
from PySide6.QtGui import QColor, QFontMetrics, QPainter, QPainterPath, QPixmap, QDesktopServices
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
from PySide6.QtMultimediaWidgets import QVideoWidget
from PySide6.QtWidgets import (
    QComboBox, QDialog, QHBoxLayout, QVBoxLayout, QWidget, QAbstractScrollArea, QStackedWidget,
    QSlider, QLabel, QSizePolicy, QGraphicsOpacityEffect,
)

from qfluentwidgets import (
    BodyLabel, CaptionLabel, CheckBox, ComboBox, FluentIcon, ImageLabel, IndeterminateProgressRing,
    SearchLineEdit, StrongBodyLabel, TitleLabel, SmoothScrollArea, InfoBar, TransparentToolButton,
    PrimaryPushButton, PushButton, isDarkTheme, FlowLayout,
)

from src.core import artwork as _artwork_module
from src.core import worker as _worker_module
from src.core.anime import anime_backend
from src.core.config import settings
from src.core.models import AnimeItem
from src.core.theme import palette
from src.core.translations import tr, register_locale_refresh

artwork = _artwork_module.artwork

logger = logging.getLogger(__name__)

_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"

def _icon(name: str, fallback=FluentIcon.PLAY):
    return getattr(FluentIcon, name, fallback)

CARD_W = 160
COVER_H = 240
TEXT_H = 56
SPACING = 14
MARGIN = 12
CELL_W = CARD_W + SPACING
CELL_H = COVER_H + TEXT_H + SPACING

_PLACEHOLDER_DARK = QColor(50, 38, 72)
_PLACEHOLDER_LIGHT = QColor(210, 200, 220)

def _placeholder_color() -> QColor:
    return _PLACEHOLDER_DARK if isDarkTheme() else _PLACEHOLDER_LIGHT

_CARD_LIFT_PX = 8
_CARD_HOVER_MS = 150

class _HoverLiftAnimator(QObject):
    def __init__(self, on_change, parent=None):
        super().__init__(parent)
        self._on_change = on_change
        self.value = 0.0
        self._anim = QVariantAnimation(self)
        self._anim.valueChanged.connect(self._on_value_changed)

    def _on_value_changed(self, v) -> None:
        self.value = float(v)
        self._on_change()

    def animate_to(self, target: float, duration: int = _CARD_HOVER_MS) -> None:
        self._anim.stop()
        self._anim.setDuration(max(duration, 16))
        self._anim.setStartValue(self.value)
        self._anim.setEndValue(float(target))
        self._anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._anim.start()

                                                                            
                                                                      
                                                                          
                                                                         
                                                                        
                                                                        
               

async def _search_anime(query: str) -> list[AnimeItem]:
    results = await anime_backend.search_anime(query)
    return [AnimeItem.from_anilist_media(r) for r in results]

async def _load_anime_media(anilist_id: Any) -> dict:
    return await anime_backend.get_media(anilist_id)

async def _load_anime_episodes(anilist_id: Any) -> dict:
    return await anime_backend.get_episodes_response(anilist_id)

async def _load_stream(provider: str, anilist_id: Any, audio: str, episode_number: Any) -> dict:
    path = f"/watch/{provider}/{anilist_id}/{audio}/{provider}-{episode_number}"
    return await anime_backend.handle(path)

class _RelayHandler(BaseHTTPRequestHandler):
    server: "HlsRelayServer"
    disable_nagle_algorithm = True

    def log_message(self, fmt, *args):
        pass

    def do_GET(self) -> None:
        target_url = self.server.resolve(self.path)
        if not target_url:
            self.send_error(404, "Unknown relay path")
            return

        headers = {"User-Agent": _UA}
        if self.server.referer:
            headers["Referer"] = self.server.referer
            try:
                parsed_referer = urllib.parse.urlsplit(self.server.referer)
                if parsed_referer.scheme and parsed_referer.netloc:
                    headers["Origin"] = f"{parsed_referer.scheme}://{parsed_referer.netloc}"
            except ValueError:
                pass
        for extra_key, extra_val in (self.server.extra_headers or {}).items():
            headers[extra_key] = extra_val

        try:
            upstream = self.server.session.get(target_url, headers=headers, stream=True, timeout=20)
        except requests.RequestException as exc:
            logger.warning("HLS relay upstream fetch failed for %s: %s", target_url, exc)
            self.send_error(502, "Upstream fetch failed")
            return

        if upstream.status_code >= 400:
            self.send_error(upstream.status_code, "Upstream error")
            upstream.close()
            return

        content_type = upstream.headers.get("Content-Type", "application/octet-stream")
        is_playlist = target_url.split("?")[0].endswith(".m3u8") or "mpegurl" in content_type.lower()

        if is_playlist:
            try:
                text = upstream.content.decode("utf-8", errors="replace")
            except Exception:
                text = ""
            rewritten = self.server.rewrite_playlist(text, target_url)
            body = rewritten.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/vnd.apple.mpegurl")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
                pass
            upstream.close()
            return

        if not is_playlist and content_type.lower().startswith("image/"):
            peek = b""
            try:
                for chunk in upstream.iter_content(chunk_size=16):
                    peek = chunk
                    break
            except Exception:
                peek = b""

            is_real_jpeg = peek[:3] == b"\xff\xd8\xff"
            is_real_png = peek[:8] == b"\x89PNG\r\n\x1a\n"
            is_real_gif = peek[:6] in (b"GIF87a", b"GIF89a")
            is_real_webp = peek[:4] == b"RIFF" and peek[8:12] == b"WEBP"
            is_real_ts = peek[:1] == b"\x47"
            is_real_fmp4 = len(peek) >= 8 and peek[4:8] in (b"ftyp", b"styp", b"moof", b"moov")

            if (is_real_jpeg or is_real_png or is_real_gif or is_real_webp) and not is_real_ts and not is_real_fmp4:
                logger.warning(
                    "HLS relay got a genuine image for segment %s - "
                    "likely blocked by upstream hotlink protection",
                    target_url,
                )
                self.send_error(502, "Upstream returned non-video content (likely blocked)")
                upstream.close()
                return

            self.send_response(200)
            self.send_header("Content-Type", "video/mp2t")
            content_length = upstream.headers.get("Content-Length")
            if content_length:
                self.send_header("Content-Length", content_length)
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            try:
                if peek:
                    self.wfile.write(peek)
                for chunk in upstream.iter_content(chunk_size=1 << 16):
                    if chunk:
                        self.wfile.write(chunk)
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
                pass
            finally:
                upstream.close()
            return

        self.send_response(200)
        self.send_header("Content-Type", content_type)
                                                                          
                                                                      
                                                                         
                                                                         
                                                                    
                                                                  
                                                                         
                                                                         
                                                           
        content_length = upstream.headers.get("Content-Length")
        if content_length:
            self.send_header("Content-Length", content_length)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        try:
            for chunk in upstream.iter_content(chunk_size=1 << 16):
                if chunk:
                    self.wfile.write(chunk)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
            pass
        finally:
            upstream.close()

_TAG_RE = re.compile(r'URI="([^"]+)"')

class HlsRelayServer:

    def __init__(self) -> None:
        self._server: Optional[ThreadingHTTPServer] = None
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        self._path_map: dict[str, str] = {}
        self._next_id = 0
        self.referer: Optional[str] = None
        self.extra_headers: dict = {}
        self._root_url: Optional[str] = None
        self._session = requests.Session()
        adapter = requests.adapters.HTTPAdapter(pool_connections=4, pool_maxsize=8, max_retries=1)
        self._session.mount("http://", adapter)
        self._session.mount("https://", adapter)

    def start(self) -> int:
        if self._server is not None:
            return self._server.server_port

        handler_cls = type("_BoundRelayHandler", (_RelayHandler,), {})
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
        self._server.daemon_threads = True
        self._server.__class__.resolve = staticmethod(self._resolve)
        self._server.__class__.rewrite_playlist = staticmethod(self._rewrite_playlist)
        self._server.referer = None
        self._server.extra_headers = {}
        self._server.session = self._session

        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return self._server.server_port

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
        self._thread = None
        self._path_map.clear()
        self._session.close()
        self._session = requests.Session()
        adapter = requests.adapters.HTTPAdapter(pool_connections=4, pool_maxsize=8, max_retries=1)
        self._session.mount("http://", adapter)
        self._session.mount("https://", adapter)

    def set_stream(self, root_url: str, referer: Optional[str] = None, extra_headers: Optional[dict] = None) -> str:
        if self._server is None:
            self.start()

        self._server.referer = referer
        self._server.extra_headers = extra_headers or {}
        self.referer = referer
        self.extra_headers = extra_headers or {}

        with self._lock:
            self._path_map.clear()
            self._next_id = 0
            local_path = self._register(root_url)

        port = self._server.server_port
        return f"http://127.0.0.1:{port}{local_path}"

    def register(self, absolute_url: str) -> str:
        if self._server is None:
            self.start()
        with self._lock:
            local_path = self._register(absolute_url)
        port = self._server.server_port
        return f"http://127.0.0.1:{port}{local_path}"

    def _register(self, absolute_url: str) -> str:
        for existing_path, existing_url in self._path_map.items():
            if existing_url == absolute_url:
                return existing_path
        ext = self._guess_extension(absolute_url)
        path = f"/relay/{self._next_id}{ext}"
        self._next_id += 1
        self._path_map[path] = absolute_url
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

    def _resolve(self, path: str) -> Optional[str]:
        clean_path = path.split("?")[0]
        with self._lock:
            return self._path_map.get(clean_path)

    def _rewrite_playlist(self, text: str, base_url: str) -> str:
        out_lines = []
        with self._lock:
            for line in text.splitlines():
                stripped = line.strip()
                if not stripped:
                    out_lines.append(line)
                    continue

                if stripped.startswith("#"):
                    if re.match(r"^#EXT-X-MEDIA:TYPE=SUBTITLES", stripped, re.IGNORECASE):
                        continue
                    if re.match(r"^#EXT-X-STREAM-INF:", stripped, re.IGNORECASE):
                        stripped = re.sub(r',?SUBTITLES="[^"]*"', "", stripped)

                    m = _TAG_RE.search(stripped)
                    if m:
                        abs_uri = urljoin(base_url, m.group(1))
                        local_path = self._register(abs_uri)
                        stripped = stripped[:m.start(1)] + local_path + stripped[m.end(1):]
                    out_lines.append(stripped)
                    continue

                abs_uri = urljoin(base_url, stripped)
                local_path = self._register(abs_uri)
                out_lines.append(local_path)

        return "\n".join(out_lines)

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

_STREAM_INF_RE = re.compile(r"^#EXT-X-STREAM-INF:", re.IGNORECASE)
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

def _parse_subtitle_cues(text: str) -> list[tuple[int, int, str]]:
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
        cue_text = (
            cue_text.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
            .replace("&#39;", "'").replace("&quot;", '"')
        )
        if cue_text:
            cues.append((start_ms, end_ms, cue_text))
    cues.sort(key=lambda c: c[0])
    return cues

class AnimePlayerWidget(QWidget):

    closed = Signal()
    _qualities_ready = Signal(list)
    _subtitle_cues_ready = Signal(list)
    _manifest_subs_ready = Signal(list)
    MAX_WIDTH = 640

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setMouseTracking(True)

        size_policy = QSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Preferred)
        size_policy.setHeightForWidth(True)
        self.setSizePolicy(size_policy)
        self.setMaximumWidth(self.MAX_WIDTH)
        self.setMinimumSize(240, int(240 * _ASPECT_H / _ASPECT_W))

        self._title = ""
        self._seeking = False
        self._muted = False
        self._pre_mute_volume = 80
        self._is_fullscreen = False
        self._fs_prev_parent: Optional[QWidget] = None
        self._fs_prev_layout = None
        self._fs_prev_index = -1
        self._auto_paused = False
        self._is_live = False
        self._live_referer: Optional[str] = None
        self._live_headers: Optional[dict] = None
        self._live_reconnect_attempts = 0

        self._master_url: Optional[str] = None
        self._master_final_url: Optional[str] = None
        self._pending_seek: Optional[tuple[int, bool]] = None

        self._external_subs: list[dict] = []
        self._cues: list[tuple[int, int, str]] = []
        self._cue_cursor = 0
        self._intended_embedded_track = -1

        self._qualities_ready.connect(self._on_qualities_ready)
        self._subtitle_cues_ready.connect(self._on_subtitle_cues_ready)
        self._manifest_subs_ready.connect(self._on_manifest_subs_ready)

        self._player = QMediaPlayer(self)
        if hasattr(self._player, "activeSubtitleTrackChanged"):
            self._player.activeSubtitleTrackChanged.connect(self._on_active_subtitle_track_changed)
        self._audio_output = QAudioOutput(self)
        self._audio_output.setVolume(self._pre_mute_volume / 100)
        self._player.setAudioOutput(self._audio_output)
        self._video_widget = QVideoWidget(self)
        self._video_widget.setStyleSheet("background: black;")
        self._video_widget.setMouseTracking(True)
        self._player.setVideoOutput(self._video_widget)

        self._player.playbackStateChanged.connect(self._on_state_changed)
        self._player.positionChanged.connect(self._on_position_changed)
        self._player.durationChanged.connect(self._on_duration_changed)
        self._player.errorOccurred.connect(self._on_error)
        self._player.mediaStatusChanged.connect(self._on_media_status_changed)
        if hasattr(self._player, "tracksChanged"):
            self._player.tracksChanged.connect(self._refresh_subtitle_tracks)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        outer.addWidget(self._video_widget, 1)

        self._subtitle_overlay = QLabel("", self)
        self._subtitle_overlay.setWordWrap(True)
        self._subtitle_overlay.setAlignment(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignBottom)
        self._subtitle_overlay.setStyleSheet(
            "color: white; background: transparent; font-size: 20px; font-weight: 700;"
            " padding: 2px 10px;"
        )
        self._subtitle_overlay.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self._subtitle_overlay.hide()

        self._status_lbl = QLabel("", self)
        self._status_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._status_lbl.setStyleSheet("color: white; background: rgba(0,0,0,150); padding: 6px;")
        self._status_lbl.hide()
        outer.addWidget(self._status_lbl, 0)

        self._controls_container = QWidget(self)
        self._controls_container.setMouseTracking(True)
        controls_container_layout = QVBoxLayout(self._controls_container)
        controls_container_layout.setContentsMargins(0, 0, 0, 0)
        controls_container_layout.setSpacing(0)
        outer.addWidget(self._controls_container, 0)

        seek_row = QWidget(self._controls_container)
        seek_layout = QHBoxLayout(seek_row)
        seek_layout.setContentsMargins(8, 2, 8, 0)
        seek_layout.setSpacing(6)

        self._position_lbl = CaptionLabel("0:00", seek_row)
        self._position_lbl.setFixedWidth(38)
        seek_layout.addWidget(self._position_lbl)

        self._seek_slider = QSlider(Qt.Orientation.Horizontal, seek_row)
        self._seek_slider.setRange(0, 0)
        self._seek_slider.setFixedHeight(16)
        self._seek_slider.sliderMoved.connect(self._on_seek_slider_moved)
        self._seek_slider.sliderPressed.connect(self._on_seek_pressed)
        self._seek_slider.sliderReleased.connect(self._on_seek_released)
        seek_layout.addWidget(self._seek_slider, 1)

        self._duration_lbl = CaptionLabel("0:00", seek_row)
        self._duration_lbl.setFixedWidth(38)
        seek_layout.addWidget(self._duration_lbl)

        controls_container_layout.addWidget(seek_row)

        controls = QWidget(self._controls_container)
        controls.setFixedHeight(32)
        controls_layout = QHBoxLayout(controls)
        controls_layout.setContentsMargins(6, 0, 6, 4)
        controls_layout.setSpacing(2)

        self._skip_back_btn = TransparentToolButton(_icon("SKIP_BACK", FluentIcon.LEFT_ARROW), controls)
        self._skip_back_btn.setFixedSize(24, 24)
        self._skip_back_btn.setToolTip(tr("anime.rewind_30", default="Rewind 30s"))
        self._skip_back_btn.clicked.connect(lambda: self._seek_relative(-30_000))
        controls_layout.addWidget(self._skip_back_btn)

        self._play_btn = TransparentToolButton(FluentIcon.PAUSE, controls)
        self._play_btn.setFixedSize(26, 26)
        self._play_btn.clicked.connect(self._toggle_play_pause)
        controls_layout.addWidget(self._play_btn)

        self._skip_fwd_btn = TransparentToolButton(_icon("SKIP_FORWARD", FluentIcon.RIGHT_ARROW), controls)
        self._skip_fwd_btn.setFixedSize(24, 24)
        self._skip_fwd_btn.setToolTip(tr("anime.forward_30", default="Forward 30s"))
        self._skip_fwd_btn.clicked.connect(lambda: self._seek_relative(30_000))
        controls_layout.addWidget(self._skip_fwd_btn)

        self._mute_btn = TransparentToolButton(FluentIcon.VOLUME, controls)
        self._mute_btn.setFixedSize(22, 22)
        self._mute_btn.setToolTip(tr("anime.mute", default="Mute"))
        self._mute_btn.clicked.connect(self._toggle_mute)
        controls_layout.addWidget(self._mute_btn)

        self._volume_slider = QSlider(Qt.Orientation.Horizontal, controls)
        self._volume_slider.setRange(0, 100)
        self._volume_slider.setValue(self._pre_mute_volume)
        self._volume_slider.setFixedWidth(56)
        self._volume_slider.setFixedHeight(16)
        self._volume_slider.setToolTip(tr("anime.volume", default="Volume"))
        self._volume_slider.valueChanged.connect(self._on_volume_changed)
        controls_layout.addWidget(self._volume_slider)

        controls_layout.addStretch(1)

        self._speed_combo = QComboBox(controls)
        for speed in _SPEEDS:
            self._speed_combo.addItem(f"{speed:g}x", userData=speed)
        self._speed_combo.setCurrentIndex(_SPEEDS.index(1.0))
        self._speed_combo.setToolTip(tr("anime.playback_speed", default="Playback speed"))
        self._speed_combo.setFixedWidth(52)
        self._speed_combo.currentIndexChanged.connect(self._on_speed_changed)
        controls_layout.addWidget(self._speed_combo)

        self._quality_combo = QComboBox(controls)
        self._quality_combo.setToolTip(tr("anime.quality", default="Quality"))
        self._quality_combo.setFixedWidth(88)
        self._quality_combo.currentIndexChanged.connect(self._on_quality_changed)
        self._reset_quality_combo()
        controls_layout.addWidget(self._quality_combo)

        self._subtitle_combo = QComboBox(controls)
        self._subtitle_combo.setToolTip(tr("anime.subtitles", default="Subtitles"))
        self._subtitle_combo.setFixedWidth(96)
        self._subtitle_combo.currentIndexChanged.connect(self._on_subtitle_changed)
        self._reset_subtitle_combo()
        controls_layout.addWidget(self._subtitle_combo)
        self._update_control_visibility()

        self._fullscreen_btn = TransparentToolButton(_icon("FULL_SCREEN", FluentIcon.VIEW), controls)
        self._fullscreen_btn.setFixedSize(22, 22)
        self._fullscreen_btn.setToolTip(tr("anime.fullscreen", default="Fullscreen"))
        self._fullscreen_btn.clicked.connect(self.toggle_fullscreen)
        controls_layout.addWidget(self._fullscreen_btn)

        controls_container_layout.addWidget(controls)

        self._video_widget.installEventFilter(self)
        self._controls_container.installEventFilter(self)
        for child in self._controls_container.findChildren(QWidget):
            child.installEventFilter(self)

        self._hide_timer = QTimer(self)
        self._hide_timer.setInterval(2500)
        self._hide_timer.setSingleShot(True)
        self._hide_timer.timeout.connect(self._auto_hide_controls)

    def hasHeightForWidth(self) -> bool:
        return True

    def heightForWidth(self, width: int) -> int:
        return max(1, int(width * _ASPECT_H / _ASPECT_W))

    def sizeHint(self):
        parent_width = self.parentWidget().width() if self.parentWidget() else self.MAX_WIDTH
        width = max(240, min(self.MAX_WIDTH, parent_width))
        return QSize(width, self.heightForWidth(width))

    def resizeEvent(self, event: QEvent) -> None:
        super().resizeEvent(event)
        self._position_subtitle_overlay()
        if self._is_fullscreen:
            return
        target_h = self.heightForWidth(self.width())
        if self.height() != target_h:
            self.setFixedHeight(target_h)

    def play_stream(
        self,
        url: str,
        referer: Optional[str] = None,
        headers: Optional[dict] = None,
        title: str = "",
        subtitles: Optional[list[dict]] = None,
        _reconnect: bool = False,
    ) -> None:
        self._title = title or ""
        self._live_referer = referer
        self._live_headers = headers
        if not _reconnect:
            self._live_reconnect_attempts = 0
        self._status_lbl.hide()
        self._pending_seek = None
        self._external_subs = subtitles or []
        self._cues = []
        self._cue_cursor = 0
        self._subtitle_overlay.setText("")
        self._subtitle_overlay.hide()
        self._reset_quality_combo()
        self._reset_subtitle_combo()
        self._populate_subtitle_options()

        final_url = url
        relay = None
        is_hls = url.split("?")[0].endswith(".m3u8")
        if is_hls:
            relay = get_relay()
            final_url = relay.set_stream(url, referer=referer, extra_headers=headers)

        self._master_url = url
        self._master_final_url = final_url

        self._player.stop()
        self._player.setVideoOutput(None)
        self._player.setSource(QUrl())
        self._player.setVideoOutput(self._video_widget)
        self._player.setSource(QUrl(final_url))
        self._player.setPlaybackRate(self._speed_combo.currentData() or 1.0)
        self._intended_embedded_track = -1
        if hasattr(self._player, "setActiveSubtitleTrack"):
            try:
                self._player.setActiveSubtitleTrack(-1)
            except Exception:
                pass
        self._player.play()
        self._reveal_controls()

        if is_hls:
            self._load_qualities_async(url, referer, headers, relay)

    def stop(self) -> None:
        self._player.stop()
        self._player.setSource(QUrl())
        self._cues = []
        self._cue_cursor = 0
        self._subtitle_overlay.setText("")
        self._subtitle_overlay.hide()

    def title(self) -> str:
        return self._title

    def set_live_mode(self, live: bool) -> None:
        self._is_live = live
        self._seek_slider.setVisible(not live)
        self._position_lbl.setVisible(not live)
        self._duration_lbl.setVisible(not live)
        self._skip_back_btn.setVisible(not live)
        self._skip_fwd_btn.setVisible(not live)
        self._speed_combo.setVisible(not live)
        if live:
            self._speed_combo.setCurrentIndex(_SPEEDS.index(1.0))
            self._player.setPlaybackRate(1.0)

    _MAX_LIVE_RECONNECTS = 5

    def _reconnect_live(self) -> None:
        if not self._is_live or not self._master_url:
            return
        self._live_reconnect_attempts += 1
        if self._live_reconnect_attempts > self._MAX_LIVE_RECONNECTS:
            self._player.stop()
            self._status_lbl.setText(tr("anime.stream_unavailable", default="Stream unavailable"))
            self._status_lbl.show()
            return
        self.play_stream(self._master_url, self._live_referer, self._live_headers, self._title, self._external_subs, _reconnect=True)

    def shutdown(self) -> None:
        self._hide_timer.stop()
        self._player.stop()
        self._player.setSource(QUrl())
        try:
            get_relay().stop()
        except Exception:
            pass

    def _toggle_play_pause(self) -> None:
        if self._player.playbackState() == QMediaPlayer.PlaybackState.PlayingState:
            self._player.pause()
        elif self._is_live and self._master_url:
            self._live_reconnect_attempts = 0
            self._reconnect_live()
        else:
            self._player.play()

    def _seek_relative(self, delta_ms: int) -> None:
        duration = self._player.duration()
        new_pos = max(0, self._player.position() + delta_ms)
        if duration:
            new_pos = min(new_pos, duration)
        self._player.setPosition(new_pos)

    def _on_state_changed(self, state) -> None:
        if state == QMediaPlayer.PlaybackState.PlayingState:
            self._play_btn.setIcon(FluentIcon.PAUSE)
            self._hide_timer.start()
        else:
            self._play_btn.setIcon(FluentIcon.PLAY)
            self._hide_timer.stop()
            self._reveal_controls()

    def _on_position_changed(self, position_ms: int) -> None:
        if not self._seeking:
            self._seek_slider.setValue(position_ms)
        self._position_lbl.setText(_format_ms(position_ms))
        self._update_subtitle_overlay(position_ms)

    def _on_duration_changed(self, duration_ms: int) -> None:
        self._seek_slider.setRange(0, max(0, duration_ms))
        self._duration_lbl.setText(_format_ms(duration_ms))

    def _on_seek_pressed(self) -> None:
        self._seeking = True

    def _on_seek_released(self) -> None:
        self._seeking = False
        self._player.setPosition(self._seek_slider.value())

    def _on_seek_slider_moved(self, value: int) -> None:
        self._position_lbl.setText(_format_ms(value))

    def _on_media_status_changed(self, status) -> None:
        if self._is_live and status in (QMediaPlayer.MediaStatus.StalledMedia, QMediaPlayer.MediaStatus.EndOfMedia):
            QTimer.singleShot(1000, self._reconnect_live)
            return
        if status == QMediaPlayer.MediaStatus.LoadedMedia:
            self._refresh_subtitle_tracks()
            if self._pending_seek is not None:
                position, was_playing = self._pending_seek
                self._pending_seek = None
                self._player.setPosition(position)
                if was_playing:
                    self._player.play()
                else:
                    self._player.pause()

    def _on_active_subtitle_track_changed(self, index: int) -> None:
        if index != self._intended_embedded_track:
            try:
                self._player.setActiveSubtitleTrack(self._intended_embedded_track)
            except Exception:
                pass

    def _update_control_visibility(self) -> None:
        if hasattr(self, "_quality_combo"):
            self._quality_combo.setVisible(self._quality_combo.count() > 1)
        if hasattr(self, "_subtitle_combo"):
            self._subtitle_combo.setVisible(self._subtitle_combo.count() > 1)

    def _reset_quality_combo(self) -> None:
        self._quality_combo.blockSignals(True)
        self._quality_combo.clear()
        self._quality_combo.addItem(tr("anime.quality_auto", default="Auto"), userData=None)
        self._quality_combo.blockSignals(False)
        self._quality_combo.setEnabled(False)
        self._update_control_visibility()

    def _load_qualities_async(
        self, master_url: str, referer: Optional[str], headers: Optional[dict], relay: Optional["HlsRelayServer"],
    ) -> None:
        def _worker() -> None:
            req_headers = {"User-Agent": _UA}
            if referer:
                req_headers["Referer"] = referer
            if headers:
                req_headers.update(headers)
            try:
                resp = requests.get(master_url, headers=req_headers, timeout=15)
                resp.raise_for_status()
                text = resp.text
            except Exception as exc:
                logger.debug("Quality probe failed for %s: %s", master_url, exc)
                return

            variants = _parse_hls_variants(text, master_url)
            if variants:
                options: list[tuple[str, Optional[str]]] = [(tr("anime.quality_auto", default="Auto"), None)]
                for height, bandwidth, variant_url in variants:
                    try:
                        local_url = relay.register(variant_url) if relay else variant_url
                    except Exception:
                        local_url = variant_url
                    label = f"{height}p" if height else f"{max(1, bandwidth // 1000)} kbps"
                    options.append((label, local_url))
                self._qualities_ready.emit(options)

            sub_groups = _parse_hls_subtitle_groups(text, master_url)
            resolved_subs: list[dict] = []
            for group in sub_groups:
                vtt_url = group["url"]
                if vtt_url.split("?")[0].endswith(".m3u8"):
                    try:
                        sub_resp = requests.get(vtt_url, headers=req_headers, timeout=15)
                        sub_resp.raise_for_status()
                        sub_text = sub_resp.text
                    except Exception:
                        continue
                    if "WEBVTT" in sub_text[:32].upper():
                        pass
                    else:
                        inner_uri = next(
                            (ln.strip() for ln in sub_text.splitlines() if ln.strip() and not ln.strip().startswith("#")),
                            None,
                        )
                        if not inner_uri:
                            continue
                        vtt_url = urljoin(vtt_url, inner_uri)
                if relay is not None:
                    try:
                        vtt_url = relay.register(vtt_url)
                    except Exception:
                        pass
                resolved_subs.append({**group, "url": vtt_url})
            if resolved_subs:
                self._manifest_subs_ready.emit(resolved_subs)

        threading.Thread(target=_worker, daemon=True).start()

    def _on_manifest_subs_ready(self, subs: list) -> None:
        if self._external_subs:
            return
        self._external_subs = subs
        self._populate_subtitle_options()

    def _on_qualities_ready(self, options: list) -> None:
        if len(options) <= 1:
            return
        self._quality_combo.blockSignals(True)
        self._quality_combo.clear()
        for label, url in options:
            self._quality_combo.addItem(label, userData=url)
        self._quality_combo.setCurrentIndex(0)
        self._quality_combo.blockSignals(False)
        self._quality_combo.setEnabled(True)
        self._update_control_visibility()

    def _on_quality_changed(self, _index: int) -> None:
        target_url = self._quality_combo.currentData()
        was_playing = self._player.playbackState() == QMediaPlayer.PlaybackState.PlayingState
        position = self._player.position()
        source_url = target_url if target_url is not None else self._master_final_url
        if not source_url:
            return
        self._pending_seek = (position, was_playing)
        self._player.setSource(QUrl(source_url))

    def _position_subtitle_overlay(self) -> None:
        video_geo = self._video_widget.geometry()
        if video_geo.width() <= 0 or video_geo.height() <= 0:
            return
        controls_h = self._controls_container.height() if self._controls_container.isVisible() else 0
        overlay_h = 70
        margin = 12
        self._subtitle_overlay.setGeometry(
            video_geo.x() + margin,
            video_geo.y() + video_geo.height() - controls_h - overlay_h - margin,
            max(0, video_geo.width() - 2 * margin),
            overlay_h,
        )
        self._subtitle_overlay.raise_()

    def _load_subtitle_async(self, sub_url: str) -> None:
        def _worker() -> None:
            try:
                resp = requests.get(sub_url, headers={"User-Agent": _UA}, timeout=15)
                resp.raise_for_status()
                text = resp.text
            except Exception as exc:
                logger.debug("Subtitle fetch failed for %s: %s", sub_url, exc)
                self._subtitle_cues_ready.emit([])
                return
            self._subtitle_cues_ready.emit(_parse_subtitle_cues(text))

        threading.Thread(target=_worker, daemon=True).start()

    def _on_subtitle_cues_ready(self, cues: list) -> None:
        self._cues = cues
        self._cue_cursor = 0
        self._subtitle_overlay.setText("")
        self._subtitle_overlay.setVisible(bool(cues))
        self._position_subtitle_overlay()

    def _update_subtitle_overlay(self, position_ms: int) -> None:
        if not self._cues:
            return
        idx = self._cue_cursor
        n = len(self._cues)
        if idx < 0 or idx >= n or self._cues[idx][0] > position_ms:
            idx = 0
        while idx < n - 1 and self._cues[idx][1] < position_ms:
            idx += 1
        self._cue_cursor = idx
        start, end, cue_text = self._cues[idx]
        text = cue_text if start <= position_ms <= end else ""
        if text != self._subtitle_overlay.text():
            self._subtitle_overlay.setText(text)
            self._position_subtitle_overlay()

    def _on_error(self, error, error_string: str) -> None:
        logger.warning("Anime player error: %s", error_string)
        self._status_lbl.setText(error_string or tr("anime.playback_error", default="Playback error"))
        self._status_lbl.show()
        if self._is_live and self._master_url:
            QTimer.singleShot(2000, self._reconnect_live)

    def _on_volume_changed(self, value: int) -> None:
        self._audio_output.setVolume(value / 100)
        self._muted = value == 0
        self._mute_btn.setIcon(FluentIcon.MUTE if self._muted else FluentIcon.VOLUME)
        if value > 0:
            self._pre_mute_volume = value

    def _toggle_mute(self) -> None:
        if self._muted:
            self._volume_slider.setValue(self._pre_mute_volume or 80)
        else:
            self._pre_mute_volume = self._volume_slider.value() or self._pre_mute_volume
            self._volume_slider.setValue(0)

    def _on_speed_changed(self, _index: int) -> None:
        speed = self._speed_combo.currentData()
        if speed:
            self._player.setPlaybackRate(speed)

    def _reset_subtitle_combo(self) -> None:
        self._subtitle_combo.blockSignals(True)
        self._subtitle_combo.clear()
        self._subtitle_combo.addItem(tr("anime.subtitles_off", default="Subtitles: Off"), userData=("off", -1))
        self._subtitle_combo.blockSignals(False)
        self._subtitle_combo.setEnabled(False)
        self._update_control_visibility()

    def _populate_subtitle_options(self) -> None:
        self._subtitle_combo.blockSignals(True)
        self._subtitle_combo.clear()
        self._subtitle_combo.addItem(tr("anime.subtitles_off", default="Subtitles: Off"), userData=("off", -1))
        default_idx = 0
        for i, sub in enumerate(self._external_subs):
            label = sub.get("label") or sub.get("srclang") or f"{tr('anime.subtitle', default='Subtitle')} {i + 1}"
            self._subtitle_combo.addItem(label, userData=("external", i))
            if sub.get("default"):
                default_idx = self._subtitle_combo.count() - 1
        self._subtitle_combo.setEnabled(bool(self._external_subs))
        self._subtitle_combo.setCurrentIndex(default_idx)
        self._subtitle_combo.blockSignals(False)
        self._update_control_visibility()
        if default_idx > 0:
            self._on_subtitle_changed(default_idx)

    def _refresh_subtitle_tracks(self) -> None:
        for i in reversed(range(self._subtitle_combo.count())):
            kind, _ = self._subtitle_combo.itemData(i)
            if kind == "embedded":
                self._subtitle_combo.removeItem(i)

        if not hasattr(self._player, "subtitleTracks"):
            return
        try:
            tracks = self._player.subtitleTracks()
        except Exception:
            tracks = []
        if not tracks:
            return

        self._subtitle_combo.blockSignals(True)
        for i, meta in enumerate(tracks):
            label = self._describe_subtitle_track(meta, i)
            self._subtitle_combo.addItem(label, userData=("embedded", i))
        self._subtitle_combo.setEnabled(True)
        self._subtitle_combo.blockSignals(False)
        self._update_control_visibility()

        current = self._subtitle_combo.currentData()
        current_kind, current_idx = current if current else ("off", -1)
        self._intended_embedded_track = current_idx if current_kind == "embedded" else -1
        if hasattr(self._player, "setActiveSubtitleTrack"):
            try:
                self._player.setActiveSubtitleTrack(self._intended_embedded_track)
            except Exception:
                pass

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

    def _on_subtitle_changed(self, _index: int) -> None:
        data = self._subtitle_combo.currentData()
        if data is None:
            return
        kind, idx = data

        self._intended_embedded_track = idx if kind == "embedded" else -1
        if hasattr(self._player, "setActiveSubtitleTrack"):
            try:
                self._player.setActiveSubtitleTrack(self._intended_embedded_track)
            except Exception:
                pass

        if kind == "external" and 0 <= idx < len(self._external_subs):
            sub_url = self._external_subs[idx].get("url")
            self._cues = []
            self._cue_cursor = 0
            self._subtitle_overlay.setText("")
            self._subtitle_overlay.hide()
            if sub_url:
                self._load_subtitle_async(sub_url)
        else:
            self._cues = []
            self._cue_cursor = 0
            self._subtitle_overlay.setText("")
            self._subtitle_overlay.hide()

    def toggle_fullscreen(self) -> None:
        if self._is_fullscreen:
            self.exit_fullscreen()
        else:
            self.enter_fullscreen()

    def enter_fullscreen(self) -> None:
        if self._is_fullscreen:
            return
        parent = self.parentWidget()
        layout = parent.layout() if parent is not None else None
        self._fs_prev_parent = parent
        self._fs_prev_layout = layout
        self._fs_prev_index = layout.indexOf(self) if layout is not None else -1

        self._player.setVideoOutput(None)
        self.setParent(None)
        self.setMaximumWidth(_QWIDGETSIZE_MAX)
        self.setMinimumHeight(0)
        self.setMaximumHeight(_QWIDGETSIZE_MAX)
        self.setWindowFlags(Qt.WindowType.Window | Qt.WindowType.FramelessWindowHint)
        self.setStyleSheet("background: black; border: none;")
        self._is_fullscreen = True
        self._fullscreen_btn.setIcon(_icon("FULL_SCREEN_EXIT", FluentIcon.CLOSE))
        self._fullscreen_btn.setToolTip(tr("anime.exit_fullscreen", default="Exit fullscreen"))
        self.showFullScreen()
        self._player.setVideoOutput(self._video_widget)
        self.setFocus()
        self._reveal_controls()

    def exit_fullscreen(self) -> None:
        if not self._is_fullscreen:
            return
        self._player.setVideoOutput(None)
        self.setWindowFlags(Qt.WindowType.Widget)
        self.setStyleSheet("")
        self.setMaximumWidth(self.MAX_WIDTH)
        self.setMinimumSize(240, int(240 * _ASPECT_H / _ASPECT_W))
        self._is_fullscreen = False
        self._fullscreen_btn.setIcon(_icon("FULL_SCREEN", FluentIcon.VIEW))
        self._fullscreen_btn.setToolTip(tr("anime.fullscreen", default="Fullscreen"))

        if self._fs_prev_layout is not None:
            if self._fs_prev_index >= 0:
                self._fs_prev_layout.insertWidget(self._fs_prev_index, self, 0, Qt.AlignmentFlag.AlignHCenter)
            else:
                self._fs_prev_layout.addWidget(self, 0, Qt.AlignmentFlag.AlignHCenter)
        elif self._fs_prev_parent is not None:
            self.setParent(self._fs_prev_parent)
        self.show()
        self._player.setVideoOutput(self._video_widget)
        self._reveal_controls()

    def _reveal_controls(self) -> None:
        self._controls_container.show()
        self.unsetCursor()
        if self._player.playbackState() == QMediaPlayer.PlaybackState.PlayingState:
            self._hide_timer.start()
        else:
            self._hide_timer.stop()
        self._position_subtitle_overlay()

    def _auto_hide_controls(self) -> None:
        if self._player.playbackState() != QMediaPlayer.PlaybackState.PlayingState:
            return
        self._controls_container.hide()
        if self._is_fullscreen:
            self.setCursor(Qt.CursorShape.BlankCursor)
        self._position_subtitle_overlay()

    def eventFilter(self, obj, event) -> bool:
        event_type = event.type()
        if event_type in (QEvent.Type.MouseMove, QEvent.Type.Enter, QEvent.Type.HoverMove):
            self._reveal_controls()
        if obj is self._video_widget and event_type == QEvent.Type.MouseButtonDblClick:
            self.toggle_fullscreen()
            return True
        return super().eventFilter(obj, event)

    def mouseMoveEvent(self, event) -> None:
        self._reveal_controls()
        super().mouseMoveEvent(event)

    def hideEvent(self, event) -> None:
        if self._player.playbackState() == QMediaPlayer.PlaybackState.PlayingState:
            self._player.pause()
            self._auto_paused = True
        super().hideEvent(event)

    def showEvent(self, event) -> None:
        if self._auto_paused:
            self._player.play()
            self._auto_paused = False
        super().showEvent(event)

    def keyPressEvent(self, event) -> None:
        key = event.key()
        self._reveal_controls()
        if key == Qt.Key.Key_Space:
            self._toggle_play_pause()
        elif key == Qt.Key.Key_F:
            self.toggle_fullscreen()
        elif key == Qt.Key.Key_Escape and self._is_fullscreen:
            self.exit_fullscreen()
        elif key == Qt.Key.Key_Left:
            self._seek_relative(-5_000)
        elif key == Qt.Key.Key_Right:
            self._seek_relative(5_000)
        elif key == Qt.Key.Key_Up:
            self._volume_slider.setValue(min(100, self._volume_slider.value() + 5))
        elif key == Qt.Key.Key_Down:
            self._volume_slider.setValue(max(0, self._volume_slider.value() - 5))
        elif key == Qt.Key.Key_M:
            self._toggle_mute()
        else:
            super().keyPressEvent(event)
            return
        event.accept()

class _AnimeGridView(QAbstractScrollArea):

    clicked = Signal(object)
    near_bottom = Signal()

    PRELOAD_ROWS = 2
    PIXMAP_LRU_LIMIT = 150
    SCALED_LRU_LIMIT = 60
    _LIFT_HEADROOM = _CARD_LIFT_PX + 2

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setFrameShape(QAbstractScrollArea.Shape.NoFrame)
        self.setStyleSheet("background: transparent; border: none;")
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.viewport().setStyleSheet("background: transparent;")
        self.viewport().setCursor(Qt.CursorShape.PointingHandCursor)

        try:
            from qfluentwidgets import SmoothScrollBar as _SSB
            sb = _SSB(Qt.Orientation.Vertical, self)
            sb.setScrollAnimation(400, QEasingCurve.Type.OutCubic)
            self.setVerticalScrollBar(sb)
        except Exception:
            pass

        self._entries: list[AnimeItem] = []
        self._pix_lru: "OrderedDict[str, QPixmap]" = OrderedDict()
        self._scaled_lru: "OrderedDict[str, QPixmap]" = OrderedDict()
        self._requested_keys: set[str] = set()
        self._failed_keys: set[str] = set()
        self._hit_rects: dict[int, QRect] = {}
        self._cols = 1
        self._extra_gutter = 0
        self._last_n = -1
        self._last_w = -1

        self._hover_idx: Optional[int] = None
        self._hover_lift = _HoverLiftAnimator(self._on_hover_lift_changed, self)
        self.viewport().setMouseTracking(True)
        self.setMouseTracking(True)

        self._repaint_timer = QTimer(self)
        self._repaint_timer.setSingleShot(True)
        self._repaint_timer.timeout.connect(self.viewport().update)
        self._resize_timer = QTimer(self)
        self._resize_timer.setSingleShot(True)
        self._resize_timer.timeout.connect(self._relayout)

        vbar = self.verticalScrollBar()
        if vbar is not None:
            vbar.valueChanged.connect(self._on_scroll)

        artwork.thumb_ready.connect(self._on_thumb_ready)
        artwork.failed.connect(self._on_thumb_failed)

    def shutdown(self) -> None:
        for timer in (self._repaint_timer, self._resize_timer):
            timer.stop()
        try:
            artwork.thumb_ready.disconnect(self._on_thumb_ready)
            artwork.failed.disconnect(self._on_thumb_failed)
        except (TypeError, RuntimeError):
            pass
        self._pix_lru.clear()
        self._scaled_lru.clear()
        self._requested_keys.clear()
        self._failed_keys.clear()
        self._entries.clear()

    def clear(self) -> None:
        self._entries = []
        self._pix_lru.clear()
        self._scaled_lru.clear()
        self._requested_keys.clear()
        self._failed_keys.clear()
        self._last_n = -1
        self._hover_idx = None
        self._hover_lift.value = 0.0
        self._relayout()

    def set_entries(self, entries: list[AnimeItem]) -> None:
        self.clear()
        self._entries = entries
        self._last_n = -1
        self._relayout()

    def append_entries(self, entries: list[AnimeItem]) -> None:
        self._entries.extend(entries)
        self._last_n = -1
        self._relayout()

    def resizeEvent(self, event: QEvent) -> None:
        super().resizeEvent(event)
        self._resize_timer.start(60)

    def _relayout(self) -> None:
        n = len(self._entries)
        width = self.viewport().width()
        if n == self._last_n and width == self._last_w and n > 0:
            return
        self._last_n = n
        self._last_w = width

        usable = max(0, width - 2 * MARGIN)
        cols = max(1, int((usable + SPACING) // CELL_W))
        self._cols = cols
        used_w = self._cols * CELL_W - SPACING
        self._extra_gutter = max(0, usable - used_w) // 2

        total_rows = (n + cols - 1) // cols if n else 0
        content_h = MARGIN * 2 + total_rows * CELL_H

        bar = self.verticalScrollBar()
        if bar is not None:
            bar.setRange(0, max(0, content_h - self.viewport().height()))
            bar.setPageStep(max(CELL_H, self.viewport().height()))

        self._schedule_repaint()

    def _on_scroll(self, value: int) -> None:
        if getattr(self, "_last_scroll", None) == value:
            return
        self._last_scroll = value
        self._set_hover_idx(None)
        self._schedule_repaint()
        bar = self.verticalScrollBar()
        if bar is not None and bar.maximum() - value <= 400:
            self.near_bottom.emit()

    def wheelEvent(self, event: QEvent) -> None:
        delta = event.angleDelta().y()
        if delta == 0:
            super().wheelEvent(event)
            return
        bar = self.verticalScrollBar()
        if bar is not None:
            step = max(60, int(CELL_H * 0.35))
            pixels = -int(delta / 120 * step)
            try:
                bar.scrollValue(pixels)
            except AttributeError:
                bar.setValue(bar.value() + pixels)
        event.accept()

    def _schedule_repaint(self) -> None:
        if not self._repaint_timer.isActive():
            self._repaint_timer.start(16)

    def _cache_put(self, key: str, pix: QPixmap) -> None:
        self._pix_lru[key] = pix
        self._pix_lru.move_to_end(key)
        while len(self._pix_lru) > self.PIXMAP_LRU_LIMIT:
            self._pix_lru.popitem(last=False)

    def _scaled_cache_get(self, url: str, source_pix: QPixmap) -> QPixmap:
        cached = self._scaled_lru.get(url)
        if cached is not None:
            self._scaled_lru.move_to_end(url)
            return cached
        scaled = source_pix.scaled(
            CARD_W, COVER_H, Qt.AspectRatioMode.KeepAspectRatioByExpanding, Qt.TransformationMode.SmoothTransformation,
        )
        self._scaled_lru[url] = scaled
        while len(self._scaled_lru) > self.SCALED_LRU_LIMIT:
            self._scaled_lru.popitem(last=False)
        return scaled

    def _on_thumb_ready(self, kind: str, url: str, path: str) -> None:
        if kind != "anime":
            return
        key = url
        if key not in self._requested_keys:
            return
        try:
            pix = QPixmap(path)
        except Exception:
            pix = None
        if pix is not None and not pix.isNull():
            self._cache_put(key, pix)
        else:
            self._failed_keys.add(key)
        self._requested_keys.discard(key)
        self._schedule_repaint()

    def _on_thumb_failed(self, kind: str, url: str) -> None:
        if kind != "anime":
            return
        if url in self._requested_keys:
            self._failed_keys.add(url)
            self._requested_keys.discard(url)
            self._schedule_repaint()

    def _ensure_image(self, entry: AnimeItem) -> Optional[QPixmap]:
        image_url = entry.artwork_url or ""
        if not image_url:
            return None
        pix = self._pix_lru.get(image_url)
        if pix is not None:
            self._pix_lru.move_to_end(image_url)
            return pix
        if image_url in self._failed_keys or image_url in self._requested_keys:
            return None

        self._requested_keys.add(image_url)
        artwork.request("anime", image_url)
        return None

    def _entry_at(self, idx: int) -> Optional[AnimeItem]:
        if 0 <= idx < len(self._entries):
            return self._entries[idx]
        return None

    def _on_hover_lift_changed(self) -> None:
        idx = self._hover_idx
        rect = self._hit_rects.get(idx) if idx is not None else None
        if rect is None:
            self._schedule_repaint()
            return
        dirty = QRect(rect.x(), rect.y() - self._LIFT_HEADROOM, rect.width(), rect.height() + self._LIFT_HEADROOM)
        self.viewport().update(dirty)

    def _set_hover_idx(self, idx: Optional[int]) -> None:
        if idx == self._hover_idx:
            return
        prev_idx = self._hover_idx
        self._hover_idx = idx
        self._hover_lift.animate_to(_CARD_LIFT_PX if idx is not None else 0.0)
        prev_rect = self._hit_rects.get(prev_idx) if prev_idx is not None else None
        if prev_rect is not None:
            dirty = QRect(prev_rect.x(), prev_rect.y() - self._LIFT_HEADROOM, prev_rect.width(), prev_rect.height() + self._LIFT_HEADROOM)
            self.viewport().update(dirty)

    def paintEvent(self, event: QEvent) -> None:
        painter = QPainter(self.viewport())
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        n = len(self._entries)
        self._hit_rects.clear()
        if n == 0 or self._cols <= 0:
            painter.end()
            return

        scroll_y = self.verticalScrollBar().value() if self.verticalScrollBar() is not None else 0
        viewport_h = self.viewport().height()
        first_row = max(0, scroll_y // CELL_H - self.PRELOAD_ROWS)
        last_row = (scroll_y + viewport_h) // CELL_H + self.PRELOAD_ROWS
        total_rows = (n + self._cols - 1) // self._cols if n else 0
        if total_rows > 0:
            last_row = min(total_rows - 1, last_row)

        fm = QFontMetrics(self.font())
        dirty = event.rect()

        for row in range(first_row, last_row + 1):
            base_idx = row * self._cols
            if base_idx >= n:
                break
            y = MARGIN + row * CELL_H - scroll_y
            if y - self._LIFT_HEADROOM > dirty.bottom() or y + CELL_H < dirty.top():
                for col in range(self._cols):
                    idx = base_idx + col
                    if idx >= n:
                        break
                    self._hit_rects[idx] = QRect(MARGIN + self._extra_gutter + col * CELL_W, y, CARD_W, COVER_H)
                continue
            for col in range(self._cols):
                idx = base_idx + col
                if idx >= n:
                    break
                x = MARGIN + self._extra_gutter + col * CELL_W

                entry = self._entry_at(idx)
                if entry is None:
                    continue

                rect = QRect(x, y, CARD_W, COVER_H)
                self._hit_rects[idx] = rect

                draw_rect = rect
                if idx == self._hover_idx and self._hover_lift.value:
                    draw_rect = QRect(x, round(y - self._hover_lift.value), CARD_W, COVER_H)

                pix = self._ensure_image(entry)
                if pix is not None:
                    scaled = self._scaled_cache_get(entry.artwork_url, pix)
                    path = QPainterPath()
                    path.addRoundedRect(float(draw_rect.x()), float(draw_rect.y()), float(CARD_W), float(COVER_H), 8, 8)
                    painter.save()
                    painter.setClipPath(path)
                    sx = max(0, (scaled.width() - CARD_W) // 2)
                    sy = max(0, (scaled.height() - COVER_H) // 2)
                    painter.drawPixmap(draw_rect.topLeft(), scaled, QRect(sx, sy, CARD_W, COVER_H))
                    painter.restore()
                else:
                    pc = _placeholder_color()
                    path = QPainterPath()
                    path.addRoundedRect(float(draw_rect.x()), float(draw_rect.y()), float(CARD_W), float(COVER_H), 8, 8)
                    painter.fillPath(path, pc)

                text_y = y + COVER_H + 6
                title_text = entry.display_title
                elided = fm.elidedText(title_text, Qt.TextElideMode.ElideRight, CARD_W - 12) if title_text else ""
                painter.setPen(QColor(palette()['primary_text']))
                painter.drawText(x, text_y + fm.ascent(), elided)

        painter.end()

    def viewportEvent(self, event: QEvent) -> bool:
        if event.type() == QEvent.Type.MouseMove:
            pos = event.position().toPoint()
            hovered = None
            for idx, r in self._hit_rects.items():
                if r.contains(pos):
                    hovered = idx
                    break
            self._set_hover_idx(hovered)
        elif event.type() == QEvent.Type.Leave:
            self._set_hover_idx(None)
        elif event.type() == QEvent.Type.MouseButtonRelease:
            pos = event.position().toPoint()
            for idx, r in self._hit_rects.items():
                if r.contains(pos):
                    entry = self._entry_at(idx)
                    if entry is not None:
                        self.clicked.emit(entry)
                    break
        return super().viewportEvent(event)

def _episode_number_label(n: Any) -> str:
    try:
        f = float(n)
        return f"Episode {int(f)}" if f.is_integer() else f"Episode {f}"
    except (TypeError, ValueError):
        return f"Episode {n}"

def _make_episode_button(episode: dict, parent=None) -> PushButton:
    btn = PushButton(_episode_number_label(episode.get("number")), parent)
    btn.setFixedHeight(32)
    btn.episode = episode
    return btn

def _sanitize_filename(name: str) -> str:
    return re.sub(r'[\\/:*?"<>|]', "_", str(name)).strip() or "unknown"

def _resolve_ffmpeg() -> Optional[str]:
    import shutil
    found = shutil.which("ffmpeg")
    if found:
        return found
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return None

def _download_hls_pure(url: str, referer: Optional[str], headers: dict, dest_path: str, item_id, download_manager, is_cancelled=None) -> None:
    import time
    from urllib.parse import urljoin

    req_headers = {"User-Agent": _UA}
    if referer:
        req_headers["Referer"] = referer
    req_headers.update(headers)

    def _get(u: str) -> requests.Response:
        r = requests.get(u, headers=req_headers, timeout=20)
        r.raise_for_status()
        return r

    text = _get(url).text
    if "#EXT-X-STREAM-INF" in text:
        variants = [urljoin(url, ln.strip()) for ln in text.splitlines() if ln.strip() and not ln.startswith("#")]
        if variants:
            url = variants[-1]
            text = _get(url).text

    seg_urls: list[str] = []
    key_url = None
    key_iv = None
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("#EXT-X-KEY"):
            m = re.search(r'URI="([^"]+)"', line)
            if m:
                key_url = urljoin(url, m.group(1))
            iv_m = re.search(r"IV=0x([0-9a-fA-F]+)", line)
            if iv_m:
                key_iv = bytes.fromhex(iv_m.group(1))
        elif line and not line.startswith("#"):
            seg_urls.append(urljoin(url, line))

    if not seg_urls:
        raise RuntimeError("No segments found in HLS playlist")

    key_bytes = _get(key_url).content if key_url else None
    cipher_cls = None
    AES = None
    Cipher = algorithms = modes = None
    if key_bytes:
        try:
            from Crypto.Cipher import AES
            cipher_cls = "pycryptodome"
        except ImportError:
            try:
                from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
                cipher_cls = "cryptography"
            except ImportError:
                raise RuntimeError("Encrypted stream requires the pycryptodome or cryptography package")

    total_segments = len(seg_urls)
    downloaded_bytes = 0
    start_time = time.time()
    last_emit = 0.0
    last_bytes = 0
    last_time = start_time
    avg_seg_size = 0.0

    with open(dest_path, "wb") as fh:
        for idx, seg_url in enumerate(seg_urls):
            if is_cancelled and is_cancelled():
                raise RuntimeError("Cancelled")
            resp = _get(seg_url)
            data = resp.content
            if key_bytes:
                iv = key_iv if key_iv else idx.to_bytes(16, "big")
                if cipher_cls == "pycryptodome":
                    data = AES.new(key_bytes, AES.MODE_CBC, iv).decrypt(data)
                else:
                    decryptor = Cipher(algorithms.AES(key_bytes), modes.CBC(iv)).decryptor()
                    data = decryptor.update(data) + decryptor.finalize()
            fh.write(data)
            seg_size = len(data)
            downloaded_bytes += seg_size
            avg_seg_size = seg_size if idx == 0 else avg_seg_size * 0.8 + seg_size * 0.2

            now = time.time()
            if now - last_emit > 0.3 or idx == total_segments - 1:
                elapsed = now - last_time
                speed_kbps = ((downloaded_bytes - last_bytes) / 1024) / elapsed if elapsed > 0 else 0.0
                last_bytes = downloaded_bytes
                last_time = now
                last_emit = now
                progress = (idx + 1) / total_segments * 100
                remaining = total_segments - (idx + 1)
                estimated_total = downloaded_bytes + int(avg_seg_size * remaining)
                download_manager.update_external(
                    item_id,
                    downloaded_bytes=downloaded_bytes,
                    total_bytes=max(estimated_total, downloaded_bytes),
                    progress=progress,
                    speed_down_kbps=speed_kbps,
                )

def _stream_quality_value(s: dict) -> Optional[int]:
    for key in ("quality", "label", "resolution"):
        val = s.get(key)
        if not val:
            continue
        m = re.search(r"(\d{3,4})", str(val))
        if m:
            return int(m.group(1))
    return None

def _pick_stream(streams: list, quality: str) -> Optional[dict]:
    candidates = [s for s in streams if s.get("type") in ("hls", "mp4") and s.get("url")]
    if not candidates:
        return None

    rated = [(s, _stream_quality_value(s)) for s in candidates]
    if not any(q is not None for _, q in rated):
        active = next((s for s in candidates if s.get("isActive")), None)
        return active or candidates[0]

    rated_known = [(s, q) for s, q in rated if q is not None]
    if quality == "best":
        return max(rated_known, key=lambda pair: pair[1])[0]
    if quality == "worst":
        return min(rated_known, key=lambda pair: pair[1])[0]
    try:
        target = int(quality)
    except (TypeError, ValueError):
        target = None
    if target is not None:
        return min(rated_known, key=lambda pair: abs(pair[1] - target))[0]
    return max(rated_known, key=lambda pair: pair[1])[0]

def _select_subtitles(active: dict, data: dict, subtitle_lang: str) -> list[dict]:
    if subtitle_lang == "none":
        return []
    subs = active.get("subtitles") or data.get("subtitles") or []
    if not subs:
        return []
    if subtitle_lang == "all":
        return subs
    if subtitle_lang == "auto":
        defaults = [s for s in subs if s.get("default")]
        return defaults or subs[:1]
    matched = [
        s for s in subs
        if subtitle_lang in (s.get("label") or "").lower()
        or subtitle_lang in (s.get("srclang") or "").lower()
    ]
    return matched

def _download_subtitles(subs: list[dict], dest_dir: str, base_name: str, referer: Optional[str]) -> None:
    req_headers = {"User-Agent": _UA}
    if referer:
        req_headers["Referer"] = referer
    for sub in subs:
        sub_url = sub.get("url")
        if not sub_url:
            continue
        try:
            resp = requests.get(sub_url, headers=req_headers, timeout=20)
            resp.raise_for_status()
        except Exception:
            continue
        ext = os.path.splitext(sub_url.split("?")[0])[1] or ".vtt"
        lang_tag = sub.get("srclang") or sub.get("label") or "sub"
        lang_tag = _sanitize_filename(lang_tag)
        sub_path = os.path.join(dest_dir, f"{base_name}.{lang_tag}{ext}")
        try:
            with open(sub_path, "wb") as fh:
                fh.write(resp.content)
        except OSError:
            pass

def _do_download_anime_episode(download_manager, item_id, anilist_id, provider, audio, episode, dest_dir, quality: str = "best", subtitle_lang: str = "none", is_cancelled=None) -> str:
    import asyncio
    import subprocess
    import time
    import urllib.request

    path = f"/watch/{provider}/{anilist_id}/{audio}/{provider}-{episode.get('number')}"
    data = asyncio.run(anime_backend.handle(path))
    streams = data.get("streams") or []

    active = _pick_stream(streams, quality)
    if active is None:
        raise RuntimeError("No downloadable stream found for this episode")

    os.makedirs(dest_dir, exist_ok=True)
    number = episode.get("number")
    ep_title = episode.get("title") or f"Episode {number}"
    try:
        num_label = f"{int(float(number)):02d}"
    except (TypeError, ValueError):
        num_label = str(number)
    base_name = f"{num_label} - {_sanitize_filename(ep_title)}"

    url = active["url"]
    referer = active.get("referer")
    headers = active.get("headers") or {}
    duration = episode.get("duration") or 0

    if active["type"] == "hls":
        ffmpeg_exe = _resolve_ffmpeg()
        if ffmpeg_exe:
            dest_path = os.path.join(dest_dir, f"{base_name}.mp4")
            cmd = [ffmpeg_exe, "-y", "-loglevel", "error", "-progress", "pipe:1"]
            if referer:
                cmd += ["-headers", f"Referer: {referer}\r\n"]
            for hk, hv in headers.items():
                cmd += ["-headers", f"{hk}: {hv}\r\n"]
            cmd += ["-i", url, "-c", "copy", dest_path]
            popen_kwargs = {}
            if os.name == "nt":
                startupinfo = subprocess.STARTUPINFO()
                startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
                popen_kwargs["startupinfo"] = startupinfo
                popen_kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, **popen_kwargs)
            last_bytes = 0
            last_time = time.time()
            try:
                cur_size = 0
                for line in proc.stdout:
                    if is_cancelled and is_cancelled():
                        proc.terminate()
                        raise RuntimeError("Cancelled")
                    line = line.strip()
                    if line.startswith("total_size="):
                        try:
                            cur_size = int(line.split("=")[1])
                        except (ValueError, IndexError):
                            pass
                    elif line.startswith("out_time_ms="):
                        try:
                            out_ms = int(line.split("=")[1])
                        except (ValueError, IndexError):
                            out_ms = None
                        now = time.time()
                        elapsed = now - last_time
                        speed_kbps = ((cur_size - last_bytes) / 1024) / elapsed if elapsed > 0 else 0.0
                        last_bytes = cur_size
                        last_time = now
                        kwargs = {"downloaded_bytes": cur_size, "speed_down_kbps": speed_kbps}
                        if duration and out_ms is not None:
                            kwargs["progress"] = max(0.0, min(100.0, out_ms / 1000 / duration * 100))
                        download_manager.update_external(item_id, **kwargs)
                proc.wait()
            finally:
                if proc.poll() is None:
                    proc.terminate()
            if proc.returncode != 0:
                raise RuntimeError("ffmpeg failed to download the stream")
        else:
            dest_path = os.path.join(dest_dir, f"{base_name}.ts")
            _download_hls_pure(url, referer, headers, dest_path, item_id, download_manager, is_cancelled)
    else:
        dest_path = os.path.join(dest_dir, f"{base_name}.mp4")
        req_headers = {"User-Agent": _UA}
        if referer:
            req_headers["Referer"] = referer
        req_headers.update(headers)
        req = urllib.request.Request(url, headers=req_headers)
        with urllib.request.urlopen(req, timeout=30) as resp, open(dest_path, "wb") as fh:
            total = int(resp.headers.get("Content-Length") or 0)
            downloaded = 0
            last_emit = 0.0
            while True:
                if is_cancelled and is_cancelled():
                    raise RuntimeError("Cancelled")
                chunk = resp.read(1 << 16)
                if not chunk:
                    break
                fh.write(chunk)
                downloaded += len(chunk)
                now = time.time()
                if now - last_emit > 0.3:
                    last_emit = now
                    download_manager.update_external(item_id, downloaded_bytes=downloaded, total_bytes=total)

    subs = _select_subtitles(active, data, subtitle_lang)
    if subs:
        _download_subtitles(subs, dest_dir, base_name, referer)

    return dest_path

_QUALITY_CAPABLE_PROVIDERS = {"animegg"}
_SUBTITLE_CAPABLE_PROVIDERS = {"animedunya"}

class AnimeDownloadDialog(QDialog):

    def __init__(self, episodes_by_provider: dict, default_provider: Optional[str], default_audio: str, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle(tr("anime.download_dialog_title", default="Download Episodes"))
        self.setFixedWidth(420)
        self._episodes_by_provider = episodes_by_provider
        self._checkboxes: list[tuple[dict, CheckBox]] = []

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 20, 20, 16)
        layout.setSpacing(12)

        form_row = QHBoxLayout()
        form_row.setSpacing(10)

        _non_provider_keys = {"page", "type", "mappings", "_unknownProviders"}
        provider_names = sorted(
            name for name, pdata in episodes_by_provider.items()
            if name not in _non_provider_keys
            and isinstance(pdata, dict) and not pdata.get("error")
            and isinstance(pdata.get("episodes"), dict) and pdata["episodes"].get("sub")
        )

        self._provider_combo = ComboBox()
        self._provider_combo.setFixedWidth(150)
        for name in provider_names:
            self._provider_combo.addItem(name, userData=name)
        if default_provider in provider_names:
            self._provider_combo.setCurrentIndex(provider_names.index(default_provider))

        form_row.addWidget(StrongBodyLabel(tr("anime.provider", default="Provider")))
        form_row.addWidget(self._provider_combo)
        form_row.addStretch(1)

        self._audio_container = QWidget()
        audio_container_layout = QHBoxLayout(self._audio_container)
        audio_container_layout.setContentsMargins(0, 0, 0, 0)
        audio_container_layout.setSpacing(10)
        self._audio_combo = ComboBox()
        self._audio_combo.addItem(tr("anime.sub", default="Sub"), userData="sub")
        self._audio_combo.addItem(tr("anime.dub", default="Dub"), userData="dub")
        self._audio_combo.setCurrentIndex(1 if default_audio == "dub" else 0)
        audio_container_layout.addWidget(self._audio_combo)
        form_row.addWidget(self._audio_container)

        layout.addLayout(form_row)

        quality_row = QHBoxLayout()
        quality_row.setSpacing(10)

        self._quality_container = QWidget()
        quality_container_layout = QHBoxLayout(self._quality_container)
        quality_container_layout.setContentsMargins(0, 0, 0, 0)
        quality_container_layout.setSpacing(10)
        self._quality_combo = ComboBox()
        for label, value in (
            (tr("anime.quality_best", default="Best available"), "best"),
            ("1080p", "1080"),
            ("720p", "720"),
            ("480p", "480"),
            ("360p", "360"),
            (tr("anime.quality_worst", default="Worst available"), "worst"),
        ):
            self._quality_combo.addItem(label, userData=value)
        quality_container_layout.addWidget(StrongBodyLabel(tr("anime.quality", default="Quality")))
        quality_container_layout.addWidget(self._quality_combo, 1)
        quality_row.addWidget(self._quality_container, 1)

        self._subtitle_container = QWidget()
        subtitle_container_layout = QHBoxLayout(self._subtitle_container)
        subtitle_container_layout.setContentsMargins(0, 0, 0, 0)
        subtitle_container_layout.setSpacing(10)
        self._subtitle_combo = ComboBox()
        for label, value in (
            (tr("anime.subtitles_none", default="No subtitles"), "none"),
            (tr("anime.subtitles_auto", default="Auto (default track)"), "auto"),
            (tr("anime.subtitles_all", default="All languages"), "all"),
            ("English", "english"),
            ("Spanish", "spanish"),
            ("French", "french"),
            ("German", "german"),
            ("Portuguese", "portuguese"),
            ("Arabic", "arabic"),
            ("Japanese", "japanese"),
        ):
            self._subtitle_combo.addItem(label, userData=value)
        subtitle_container_layout.addWidget(StrongBodyLabel(tr("anime.subtitles", default="Subtitles")))
        subtitle_container_layout.addWidget(self._subtitle_combo, 1)
        quality_row.addWidget(self._subtitle_container, 1)

        self._quality_row_widget = QWidget()
        self._quality_row_widget.setLayout(quality_row)
        layout.addWidget(self._quality_row_widget)

        self._select_all_chk = CheckBox(tr("anime.select_all", default="Select All"))
        self._select_all_chk.stateChanged.connect(self._on_select_all)
        layout.addWidget(self._select_all_chk)

        scroll = SmoothScrollArea(self)
        scroll.setWidgetResizable(True)
        scroll.setFixedHeight(280)
        scroll.setFrameShape(QAbstractScrollArea.Shape.NoFrame)
        scroll.setStyleSheet("QScrollArea { background: transparent; border: none; }")

        self._ep_container = QWidget()
        self._ep_layout = QVBoxLayout(self._ep_container)
        self._ep_layout.setContentsMargins(4, 4, 4, 4)
        self._ep_layout.setSpacing(6)
        scroll.setWidget(self._ep_container)
        layout.addWidget(scroll, 1)

        self._error_lbl = CaptionLabel("")
        self._error_lbl.setStyleSheet("color: #e04b4b;")
        self._error_lbl.hide()
        layout.addWidget(self._error_lbl)

        btn_row = QHBoxLayout()
        btn_row.addStretch(1)
        self._cancel_btn = PushButton(tr("anime.cancel", default="Cancel"))
        self._download_btn = PrimaryPushButton(tr("anime.download", default="Download"))
        btn_row.addWidget(self._cancel_btn)
        btn_row.addWidget(self._download_btn)
        layout.addLayout(btn_row)

        self._cancel_btn.clicked.connect(self.reject)
        self._download_btn.clicked.connect(self._on_download_clicked)
        self._provider_combo.currentIndexChanged.connect(self._on_provider_changed)
        self._audio_combo.currentIndexChanged.connect(self._rebuild_episode_list)
        self._on_provider_changed()

    def _on_provider_changed(self, _index: int = 0) -> None:
        provider = self._provider_combo.currentData()
        provider_data = self._episodes_by_provider.get(provider) or {}
        provider_episodes = provider_data.get("episodes") or {}

        has_dub = bool(provider_episodes.get("dub"))
        self._audio_container.setVisible(has_dub)
        if not has_dub:
            self._audio_combo.blockSignals(True)
            self._audio_combo.setCurrentIndex(0)
            self._audio_combo.blockSignals(False)

        has_quality = provider in _QUALITY_CAPABLE_PROVIDERS
        self._quality_container.setVisible(has_quality)
        has_subs = provider in _SUBTITLE_CAPABLE_PROVIDERS
        self._subtitle_container.setVisible(has_subs)
        self._quality_row_widget.setVisible(has_quality or has_subs)

        self._rebuild_episode_list()

    def _on_select_all(self, _state) -> None:
        checked = self._select_all_chk.isChecked()
        for _, chk in self._checkboxes:
            chk.setChecked(checked)

    def _on_download_clicked(self) -> None:
        if not any(chk.isChecked() for _, chk in self._checkboxes):
            self._error_lbl.setText(tr("anime.select_episode_error", default="Select an episode to download"))
            self._error_lbl.show()
            return
        self.accept()

    def _rebuild_episode_list(self) -> None:
        while self._ep_layout.count():
            item = self._ep_layout.takeAt(0)
            w = item.widget() if hasattr(item, "widget") else None
            if w:
                w.deleteLater()
        self._checkboxes.clear()
        self._error_lbl.hide()

        provider = self._provider_combo.currentData()
        audio = self._audio_combo.currentData() or "sub"
        provider_data = self._episodes_by_provider.get(provider) or {}
        episodes = provider_data.get("episodes")
        episodes = episodes.get(audio, []) if isinstance(episodes, dict) else []

        for ep in episodes:
            label = _episode_number_label(ep.get("number"))
            if ep.get("title"):
                label = f"{label} — {ep['title']}"
            chk = CheckBox(label)
            self._ep_layout.addWidget(chk)
            self._checkboxes.append((ep, chk))
        self._ep_layout.addStretch(1)

    def selected(self) -> tuple[Optional[str], str, list[dict], str, str]:
        provider = self._provider_combo.currentData()
        audio = self._audio_combo.currentData() or "sub"
        episodes = [ep for ep, chk in self._checkboxes if chk.isChecked()]
        quality = self._quality_combo.currentData() or "best"
        subtitle_lang = self._subtitle_combo.currentData() or "none"
        return provider, audio, episodes, quality, subtitle_lang

class AnimeDetailView(QWidget):

    back_requested = Signal()
    download_requested = Signal(object, str, str, object, str, str)

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._media: Optional[dict] = None
        self._episodes_by_provider: dict = {}
        self._current_provider: Optional[str] = None
        self._current_audio = "sub"
        self._stream_task_id: Optional[str] = None

        self._cover_url: Optional[str] = None

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        top_row = QHBoxLayout()
        top_row.setContentsMargins(0, 0, 0, 8)
        self._back_btn = TransparentToolButton(FluentIcon.LEFT_ARROW)
        self._back_btn.setFixedSize(34, 34)
        self._back_btn.clicked.connect(self.back_requested.emit)
        top_row.addWidget(self._back_btn)
        top_row.addStretch(1)
        outer.addLayout(top_row)

        scroll = SmoothScrollArea(self)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QAbstractScrollArea.Shape.NoFrame)
        scroll.setStyleSheet("QScrollArea { background: transparent; border: none; }")
        scroll.viewport().setStyleSheet("background: transparent;")
        outer.addWidget(scroll, 1)

        content = QWidget()
        content.setStyleSheet("background: transparent;")
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(0, 0, 4, 20)
        content_layout.setSpacing(12)
        scroll.setWidget(content)

        header = QHBoxLayout()
        header.setSpacing(20)

        self._poster = ImageLabel()
        self._poster.setFixedSize(200, 300)
        self._poster.setBorderRadius(8, 8, 8, 8)
        header.addWidget(self._poster, 0)

        info_col = QVBoxLayout()
        info_col.setSpacing(6)

        self._title_lbl = TitleLabel("")
        self._title_lbl.setWordWrap(True)
        info_col.addWidget(self._title_lbl)

        self._meta_lbl = CaptionLabel("")
        self._meta_lbl.setStyleSheet(f'color: {palette()["muted"]};')
        info_col.addWidget(self._meta_lbl)

        self._genres_lbl = BodyLabel("")
        self._genres_lbl.setWordWrap(True)
        info_col.addWidget(self._genres_lbl)

        self._synopsis_lbl = BodyLabel("")
        self._synopsis_lbl.setWordWrap(True)
        info_col.addWidget(self._synopsis_lbl, 1)

        info_col.addStretch(1)
        header.addLayout(info_col, 1)

        content_layout.addLayout(header)

        controls_row = QHBoxLayout()
        controls_row.setSpacing(10)

        self._episodes_title = StrongBodyLabel(tr("anime.episodes", default="Episodes"))
        controls_row.addWidget(self._episodes_title)
        controls_row.addStretch(1)

        self._provider_combo = ComboBox()
        self._provider_combo.setMinimumWidth(140)
        self._provider_combo.currentIndexChanged.connect(self._on_provider_changed)
        controls_row.addWidget(self._provider_combo)

        self._audio_combo = ComboBox()
        self._audio_combo.addItem(tr("anime.sub", default="Sub"), userData="sub")
        self._audio_combo.addItem(tr("anime.dub", default="Dub"), userData="dub")
        self._audio_combo.setMinimumWidth(80)
        self._audio_combo.currentIndexChanged.connect(self._on_audio_changed)
        controls_row.addWidget(self._audio_combo)

        self._download_btn = PushButton(FluentIcon.DOWNLOAD, tr("anime.download", default="Download"))
        self._download_btn.clicked.connect(self._on_download_clicked)
        controls_row.addWidget(self._download_btn)

        content_layout.addLayout(controls_row)

        ep_content = QWidget()
        self._ep_layout = FlowLayout(ep_content, needAni=False)
        self._ep_layout.setContentsMargins(0, 0, 0, 0)
        self._ep_layout.setVerticalSpacing(8)
        self._ep_layout.setHorizontalSpacing(8)
        content_layout.addWidget(ep_content)

        status_row = QHBoxLayout()
        status_row.setSpacing(8)
        status_row.setContentsMargins(0, 0, 0, 0)
        status_row.setAlignment(Qt.AlignmentFlag.AlignHCenter)

        self._status_spinner = IndeterminateProgressRing()
        self._status_spinner.setFixedSize(18, 18)
        self._status_spinner.setStrokeWidth(3)
        self._status_spinner.hide()
        status_row.addWidget(self._status_spinner)

        self._status_lbl = CaptionLabel("")
        self._status_lbl.setStyleSheet(f'color: {palette()["muted"]};')
        status_row.addWidget(self._status_lbl)

        content_layout.addLayout(status_row)

        self._player_widget = AnimePlayerWidget(content)
        self._player_widget.hide()
        content_layout.addWidget(self._player_widget, 0, Qt.AlignmentFlag.AlignHCenter)

        content_layout.addStretch(1)

        artwork.full_ready.connect(self._on_cover_ready)
        artwork.failed.connect(self._on_cover_failed)

    def shutdown(self) -> None:
        try:
            artwork.full_ready.disconnect(self._on_cover_ready)
            artwork.failed.disconnect(self._on_cover_failed)
        except (TypeError, RuntimeError):
            pass
        if self._stream_task_id is not None:
            _worker_module.cancel(self._stream_task_id)
            self._stream_task_id = None
        try:
            self._player_widget.shutdown()
        except Exception:
            pass

    def _set_poster_placeholder(self) -> None:
        pix = QPixmap(200, 300)
        pix.fill(_placeholder_color())
        self._poster.setImage(pix)
        self._poster.setFixedSize(200, 300)

    def show_loading(self, entry: AnimeItem) -> None:
        self._player_widget.stop()
        self._player_widget.hide()

        self._media = None
        self._episodes_by_provider = {}
        self._current_provider = None

        self._title_lbl.setText(entry.display_title or "")
        self._meta_lbl.setText("")
        self._genres_lbl.setText("")
        self._synopsis_lbl.setText("")

        self._provider_combo.blockSignals(True)
        self._provider_combo.clear()
        self._provider_combo.blockSignals(False)

        while self._ep_layout.count():
            item = self._ep_layout.takeAt(0)
            widget = item.widget() if hasattr(item, "widget") else item
            if widget:
                widget.deleteLater()

        self._status_lbl.setText(tr("anime.loading_details", default="Loading details..."))

        self._poster.clear()
        self._cover_url = entry.artwork_url or ""
        self._set_poster_placeholder()
        if self._cover_url:
            artwork.request("anime", self._cover_url, want_full=True)

    def show_detail(self, media: dict, episodes_by_provider: dict) -> None:
        self._media = media
        self._episodes_by_provider = episodes_by_provider or {}

        title = media.get("title") or {}
        self._title_lbl.setText(title.get("english") or title.get("romaji") or "")

        meta_parts = []
        if media.get("seasonYear"):
            meta_parts.append(str(media["seasonYear"]))
        if media.get("status"):
            meta_parts.append(str(media["status"]))
        self._meta_lbl.setText(" · ".join(meta_parts))

        genres = media.get("genres") or []
        self._genres_lbl.setText(", ".join(genres))

        self._synopsis_lbl.setText(media.get("description") or tr("anime.no_synopsis", default="No synopsis available."))

        self._poster.clear()
        self._cover_url = media.get("coverImage") or ""
        if self._cover_url:
            self._set_poster_placeholder()
            artwork.request("anime", self._cover_url, want_full=True)
        else:
            self._set_poster_placeholder()

    def set_episodes_loading(self) -> None:
        self._status_lbl.setText("")
        self._status_spinner.show()

    def set_episodes_error(self, msg: str) -> None:
        self._status_spinner.hide()
        self._status_lbl.setText(tr("anime.episodes_error", default="Failed to load episodes."))
        logger.error("Episode load error: %s", msg)

    def set_episodes(self, episodes_by_provider: dict) -> None:
        self._status_spinner.hide()
        self._episodes_by_provider = episodes_by_provider or {}

        _non_provider_keys = {"page", "type", "mappings", "_unknownProviders"}
        provider_names = sorted(
            name for name, data in self._episodes_by_provider.items()
            if name not in _non_provider_keys
            and isinstance(data, dict) and not data.get("error")
            and isinstance(data.get("episodes"), dict) and data["episodes"].get("sub")
        )

        self._provider_combo.blockSignals(True)
        self._provider_combo.clear()
        for name in provider_names:
            self._provider_combo.addItem(name, userData=name)
        self._provider_combo.blockSignals(False)

        if provider_names:
            self._provider_combo.setCurrentIndex(0)
            self._current_provider = provider_names[0]
        else:
            self._current_provider = None
            self._status_lbl.setText(tr("anime.no_providers", default="No providers returned episodes for this title."))

        self._current_audio = "sub"
        self._update_audio_options()

        self._render_episodes()

    def _on_cover_ready(self, kind: str, url: str, path: str) -> None:
        if kind != "anime" or url != self._cover_url:
            return
        pix = QPixmap(path)
        if not pix.isNull():
            self._poster.setImage(pix)
            self._poster.setFixedSize(200, 300)

    def _on_cover_failed(self, kind: str, url: str) -> None:
        if kind != "anime" or url != self._cover_url:
            return
        self._set_poster_placeholder()

    def _update_audio_options(self) -> None:
        provider_data = self._episodes_by_provider.get(self._current_provider) or {}
        provider_episodes = provider_data.get("episodes") or {}
        has_sub = bool(provider_episodes.get("sub"))
        has_dub = bool(provider_episodes.get("dub"))

        self._audio_combo.blockSignals(True)
        self._audio_combo.clear()
        if has_sub:
            self._audio_combo.addItem(tr("anime.sub", default="Sub"), userData="sub")
        if has_dub:
            self._audio_combo.addItem(tr("anime.dub", default="Dub"), userData="dub")
        self._audio_combo.setVisible(has_sub and has_dub)

        if self._current_audio == "dub" and has_dub:
            self._audio_combo.setCurrentIndex(self._audio_combo.findData("dub"))
        elif has_sub:
            self._audio_combo.setCurrentIndex(self._audio_combo.findData("sub"))
            self._current_audio = "sub"
        elif has_dub:
            self._audio_combo.setCurrentIndex(self._audio_combo.findData("dub"))
            self._current_audio = "dub"
        self._audio_combo.blockSignals(False)

    def _on_provider_changed(self, _index: int) -> None:
        self._current_provider = self._provider_combo.currentData()
        self._update_audio_options()
        self._render_episodes()

    def _on_audio_changed(self, _index: int) -> None:
        self._current_audio = self._audio_combo.currentData() or "sub"
        self._render_episodes()

    def _on_download_clicked(self) -> None:
        if not self._media or not self._episodes_by_provider:
            return
        dlg = AnimeDownloadDialog(self._episodes_by_provider, self._current_provider, self._current_audio, parent=self.window())
        if not dlg.exec():
            return
        provider, audio, episodes, quality, subtitle_lang = dlg.selected()
        if not provider or not episodes:
            return
        self.download_requested.emit(self._media, provider, audio, episodes, quality, subtitle_lang)

    def _render_episodes(self) -> None:
        while self._ep_layout.count():
            item = self._ep_layout.takeAt(0)
            widget = item.widget() if hasattr(item, "widget") else item
            if widget:
                widget.deleteLater()

        if not self._current_provider:
            return

        provider_data = self._episodes_by_provider.get(self._current_provider) or {}
        episodes = provider_data.get("episodes")
        episodes = episodes.get(self._current_audio, []) if isinstance(episodes, dict) else []

        if not episodes:
            self._status_lbl.setText(tr("anime.no_episodes_audio", default="No {audio} episodes from this provider.").format(audio=self._current_audio))
            return

        self._status_lbl.setText("")
        for ep in episodes:
            btn = _make_episode_button(ep)
            btn.clicked.connect(lambda _checked=False, e=ep: self._watch_episode(e))
            self._ep_layout.addWidget(btn)

    def _watch_episode(self, episode: dict) -> None:
        if self._media is None or not self._current_provider:
            return

        if self._stream_task_id is not None:
            return

        self._status_lbl.setText(tr("anime.loading_stream", default="Loading stream..."))

        def _on_done(data: dict) -> None:
            self._stream_task_id = None
            self._on_stream_ready(data)

        def _on_error(msg: str) -> None:
            self._stream_task_id = None
            self._on_stream_error(msg)

        self._stream_task_id = _worker_module.submit_coro(
            _load_stream,
            args=(self._current_provider, self._media["id"], self._current_audio, episode.get("number")),
            on_done=_on_done,
            on_error=_on_error,
        )

    def _on_stream_ready(self, data: dict) -> None:
        self._status_lbl.setText("")
        streams = data.get("streams") or []
        active = next((s for s in streams if s.get("isActive")), None) or (streams[0] if streams else None)
        if not active:
            InfoBar.error(
                title=tr("anime.playback_error", default="Playback error"),
                content=tr("anime.no_stream_found", default="No playable stream was returned."),
                parent=self.window(), duration=3000, isClosable=True,
            )
            return

        url = active.get("url", "")
        if not url:
            return

        if active.get("type") == "embed":
            InfoBar.warning(
                title=tr("anime.embed_only", default="Embed-only source"),
                content=tr(
                    "anime.embed_only_msg",
                    default="This provider only returned an embed page, which can't be played in-app. Opening in browser instead.",
                ),
                parent=self.window(), duration=4000, isClosable=True,
            )
            QDesktopServices.openUrl(QUrl(url))
            return

        referer = active.get("referer")
        headers = active.get("headers")
        title = data.get("title") or ""

        subtitles = active.get("subtitles")
        if not subtitles and active.get("subtitle"):
            sub_val = active["subtitle"]
            if isinstance(sub_val, str):
                subtitles = [{"url": sub_val, "label": tr("anime.english", default="English"), "default": True}]
            elif isinstance(sub_val, dict) and sub_val.get("url"):
                subtitles = [sub_val]
        subtitles = [s for s in (subtitles or []) if isinstance(s, dict) and s.get("url")]

        self._player_widget.play_stream(url, referer=referer, headers=headers, title=title, subtitles=subtitles)
        self._player_widget.show()
        self._player_widget.setFocus()

    def _on_stream_error(self, msg: str) -> None:
        self._status_lbl.setText(msg)
        InfoBar.error(
            title=tr("anime.playback_error", default="Playback error"),
            content=msg,
            parent=self.window(), duration=3000, isClosable=True,
        )

class AnimePage(QWidget):

    anime_selected = Signal(object)

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setStyleSheet("background: transparent;")
        self._search_task_id: Optional[str] = None
        self._detail_task_id: Optional[str] = None
        self._episodes_task_id: Optional[str] = None
        self._searching = False
        self._download_manager = None

        outer = QVBoxLayout(self)
        outer.setContentsMargins(24, 12, 24, 20)
        outer.setSpacing(14)

        search_row = QHBoxLayout()
        search_row.setSpacing(10)

        self._search_box = SearchLineEdit()
        self._search_box.setPlaceholderText(tr("anime.search_placeholder", default="Search anime..."))
        self._search_box.setFixedHeight(38)
        self._search_box.setMinimumWidth(160)
        self._search_box.setMaximumWidth(360)
        self._search_box.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self._search_box.searchSignal.connect(self._do_search)
        self._search_box.clearSignal.connect(self._on_clear_search)
        self._search_box.returnPressed.connect(self._search_box.search)
        search_row.addWidget(self._search_box)
        search_row.addStretch(1)

        outer.addLayout(search_row)

        self._stack = QStackedWidget()
        outer.addWidget(self._stack, 1)

        grid_page = QWidget()
        grid_page_layout = QVBoxLayout(grid_page)
        grid_page_layout.setContentsMargins(0, 0, 0, 0)
        grid_page_layout.setSpacing(SPACING)

        scroll = SmoothScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QAbstractScrollArea.Shape.NoFrame)
        scroll.setStyleSheet("QScrollArea { background: transparent; border: none; }")
        scroll.viewport().setStyleSheet("background: transparent;")

        grid_content = QWidget()
        gc_layout = QVBoxLayout(grid_content)
        gc_layout.setContentsMargins(0, 0, 0, 0)
        gc_layout.setSpacing(SPACING)

        self._grid_view = _AnimeGridView()
        gc_layout.addWidget(self._grid_view)

        scroll.setWidget(grid_content)
        grid_page_layout.addWidget(scroll, 1)

        self._empty_lbl = BodyLabel(tr("anime.empty_state", default="Search for an anime to get started."))
        self._empty_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._empty_lbl.setStyleSheet(f'color: {palette()["muted"]};')
        grid_page_layout.addWidget(self._empty_lbl)

        self._status_lbl = BodyLabel("")
        self._status_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._status_lbl.setStyleSheet(f'color: {palette()["muted"]};')
        self._status_lbl.hide()
        grid_page_layout.addWidget(self._status_lbl)

        self._stack.addWidget(grid_page)

        self._detail_view = AnimeDetailView()
        self._detail_view.back_requested.connect(self._show_grid)
        self._detail_view.download_requested.connect(self._on_download_requested)
        self._stack.addWidget(self._detail_view)

        self._grid_view.clicked.connect(self._on_anime_clicked)
        register_locale_refresh(self, self._apply_locale)

    def set_download_manager(self, manager) -> None:
        self._download_manager = manager

    def _on_download_requested(self, media: dict, provider: str, audio: str, episodes: list, quality: str, subtitle_lang: str) -> None:
        if self._download_manager is None:
            return
        anilist_id = media.get("id")
        title_obj = media.get("title") or {}
        title = title_obj.get("english") or title_obj.get("romaji") or "Anime"
        dest_dir = os.path.join(settings.download_dir_anime, _sanitize_filename(title))

        for ep in episodes:
            label = _episode_number_label(ep.get("number"))
            item_id = self._download_manager.add_external(
                game_name=f"{title} - {label}", console=title, source=provider, category="anime",
            )

            def _on_done(path: str, iid=item_id) -> None:
                self._download_manager.complete_external(iid, path)

            def _on_err(msg: str, iid=item_id) -> None:
                self._download_manager.fail_external(iid, msg)

            task_id = _worker_module.submit(
                _do_download_anime_episode,
                args=(self._download_manager, item_id, anilist_id, provider, audio, ep, dest_dir),
                kwargs={"quality": quality, "subtitle_lang": subtitle_lang},
                on_done=_on_done, on_error=_on_err,
            )
            self._download_manager.register_external_cancel(item_id, lambda tid=task_id: _worker_module.cancel(tid))

        InfoBar.success(
            title=tr("anime.download_queued_title", default="Download queued"),
            content=tr("anime.download_queued_content", count=len(episodes)),
            parent=self.window(), duration=2500, isClosable=True,
        )

    def _apply_locale(self) -> None:
        self._search_box.setPlaceholderText(tr("anime.search_placeholder", default="Search anime..."))

    def _switch_stack_animated(self, widget) -> None:
        if self._stack.currentWidget() is widget:
            return
        old_group = getattr(self, "_stack_fade_anim", None)
        if old_group is not None:
            old_group.stop()
            for i in range(self._stack.count()):
                page = self._stack.widget(i)
                page.setGraphicsEffect(None)
                page.move(0, 0)
            self._stack_fade_anim = None
        self._stack.setCurrentWidget(widget)
        widget.move(0, 0)
        effect = QGraphicsOpacityEffect(widget)
        widget.setGraphicsEffect(effect)
        widget.move(36, 0)

        fade = QPropertyAnimation(effect, b"opacity", widget)
        fade.setDuration(220)
        fade.setStartValue(0.0)
        fade.setEndValue(1.0)
        fade.setEasingCurve(QEasingCurve.Type.OutCubic)

        slide = QPropertyAnimation(widget, b"pos", widget)
        slide.setDuration(220)
        slide.setStartValue(QPoint(36, 0))
        slide.setEndValue(QPoint(0, 0))
        slide.setEasingCurve(QEasingCurve.Type.OutCubic)

        group = QParallelAnimationGroup(widget)
        group.addAnimation(fade)
        group.addAnimation(slide)

        def _cleanup():
            widget.setGraphicsEffect(None)
            widget.move(0, 0)
            self._stack_fade_anim = None
        group.finished.connect(_cleanup)
        self._stack_fade_anim = group
        group.start()

    def _show_grid(self) -> None:
        self._switch_stack_animated(self._stack.widget(0))

    def _on_clear_search(self) -> None:
        self._search_box.setText("")
        self._grid_view.clear()
        self._status_lbl.hide()
        self._status_lbl.setText("")
        self._empty_lbl.show()

    def _do_search(self, query: str) -> None:
        query = (query or "").strip()
        if not query:
            self._on_clear_search()
            return

        if self._search_task_id is not None:
            _worker_module.cancel(self._search_task_id)
            self._search_task_id = None

        self._searching = True
        self._empty_lbl.hide()
        self._status_lbl.show()
        self._status_lbl.setText(tr("anime.searching", default="Searching..."))

        def _on_results(results: list) -> None:
            self._search_task_id = None
            if results:
                self._grid_view.set_entries(results)
                self._status_lbl.setText(tr("anime.search_results_count", count=len(results)))
            else:
                self._grid_view.clear()
                self._status_lbl.setText(tr("anime.no_results", default="No results found"))
            self._searching = False

        def _on_error(msg: str) -> None:
            self._search_task_id = None
            self._status_lbl.setText(tr("anime.search_error", default="Search failed"))
            logger.error("Search error: %s", msg)
            self._searching = False

        self._search_task_id = _worker_module.submit_coro(
            _search_anime, args=(query,), on_done=_on_results, on_error=_on_error,
        )

    def _on_anime_clicked(self, result: AnimeItem) -> None:
        anilist_id = result.id
        if not anilist_id:
            return

        if self._detail_task_id is not None:
            _worker_module.cancel(self._detail_task_id)
            self._detail_task_id = None

        self._detail_view.show_loading(result)
        self._switch_stack_animated(self._detail_view)
        self.anime_selected.emit(result)

        def _on_media_loaded(media: dict) -> None:
            self._detail_task_id = None
            self._detail_view.show_detail(media, {})
            self._start_episode_load(anilist_id)

        def _on_media_err(msg: str) -> None:
            self._detail_task_id = None
            InfoBar.error(
                title=tr("anime.error", default="Error"),
                content=msg,
                parent=self.window(), duration=3000, isClosable=True,
            )

        self._detail_task_id = _worker_module.submit_coro(
            _load_anime_media, args=(anilist_id,), on_done=_on_media_loaded, on_error=_on_media_err,
        )

    def _start_episode_load(self, anilist_id: Any) -> None:
        if self._episodes_task_id is not None:
            _worker_module.cancel(self._episodes_task_id)
            self._episodes_task_id = None

        self._detail_view.set_episodes_loading()

        def _on_eps_loaded(episodes_by_provider: dict) -> None:
            self._episodes_task_id = None
            self._detail_view.set_episodes(episodes_by_provider)

        def _on_eps_err(msg: str) -> None:
            self._episodes_task_id = None
            self._detail_view.set_episodes_error(msg)

        self._episodes_task_id = _worker_module.submit_coro(
            _load_anime_episodes, args=(anilist_id,), on_done=_on_eps_loaded, on_error=_on_eps_err,
        )

    def shutdown(self) -> None:
        try:
            self._grid_view.shutdown()
        except Exception:
            pass
        try:
            self._detail_view.shutdown()
        except Exception:
            pass
        for attr_name in ("_search_task_id", "_detail_task_id", "_episodes_task_id"):
            task_id = getattr(self, attr_name, None)
            setattr(self, attr_name, None)
            if task_id is not None:
                _worker_module.cancel(task_id)
