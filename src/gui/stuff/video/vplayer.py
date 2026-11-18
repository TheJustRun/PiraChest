from __future__ import annotations

import html
import logging
import math
import re
import threading
import time
from bisect import bisect_right
from concurrent.futures import ThreadPoolExecutor
from contextlib import suppress
from functools import partial
from typing import Optional
from urllib.parse import urljoin

import httpx
from PySide6.QtCore import QElapsedTimer, QEvent, QPoint, QRect, QRectF, QSettings, QSize, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import (
    QActionGroup, QColor, QCursor, QFont, QFontMetrics, QGuiApplication, QIcon, QIconEngine, QPainter,
    QPainterPath, QPen, QPixmap,
)
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
from PySide6.QtWidgets import (
    QDoubleSpinBox, QFrame, QHBoxLayout, QLabel, QMenu, QSizePolicy, QSlider, QStyle, QToolButton, QToolTip,
    QVBoxLayout, QWidget,
)
from PySide6.QtWidgets import QPushButton as _QPushButton
from qfluentwidgets import FluentIcon
from qfluentwidgets import Theme as _Theme
from shiboken6 import isValid as _qt_is_valid

from src.core.config import settings
from src.core.system import release_input_state
from src.core.net import LazyHttpClient
from src.core.theme import palette, register_theme_refresh, to_color
from src.core.translations import tr
from src.core.tv.relay import HlsRelayServer, _STREAM_INF_RE, _UA, get_relay

from .player_widgets import (
    _Bar, _LoadingOverlay, _PipWindow, _SeekSlider, _SkipBubble, _Spinner, _SubDelayPopup, _SubOverlay,
    _accent, _avail_for, _bar_qss, _format_ms, _is_local_stream_url, _menu_qss, _wicon,
)
from .video_surface import VideoSurface

logger = logging.getLogger(__name__)


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
        self._video_widget = VideoSurface(self)
        self._player.setVideoSink(self._video_widget.videoSink())

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
        bar_qss, menu_qss = _bar_qss(), _menu_qss()
        if (bar_qss, menu_qss) == getattr(self, "_theme_key", None):
            return
        self._theme_key = (bar_qss, menu_qss)
        self._bar.setStyleSheet(bar_qss)
        self._bar.update()
        for b in (self._speed_btn, self._quality_btn, self._sub_btn):
            b.menu().setStyleSheet(menu_qss)

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
        self._click_timer.stop()
        if self._is_fullscreen:
            self.exit_fullscreen()
        if self._pip_active:
            self.exit_pip(restore=False)
        self._player.stop()
        self._player.setSource(QUrl())
        self._player.setVideoSink(None)
        self._clear_cues()
        self.set_skip_segments()
        self._hide_overlays()
        self._stop_relay()
        self._video_widget.clear()
        self._release_windows()

    def _release_windows(self) -> None:
        for attr in ("_pip_win", "_fs_bar_win", "_fs_win"):
            win = getattr(self, attr, None)
            setattr(self, attr, None)
            if win is None or not _qt_is_valid(win):
                continue
            with suppress(RuntimeError, TypeError):
                win.hide()
                win.setParent(None)
                win.deleteLater()
        self._pip_layout = None
        release_input_state()

    def destroy(self) -> None:
        self._stream_gen += 1
        main = self.window()
        cls = type(self)
        self.shutdown()
        for widget in (self._bubble, self._loader, self._subs, self._delay_popup):
            if widget is not None:
                widget.hide()
                widget.setParent(None)
                widget.deleteLater()
        self.hide()
        self.setParent(None)
        self.deleteLater()
        QTimer.singleShot(0, lambda: cls._restore_main(main))

    @staticmethod
    def _restore_main(main: Optional[QWidget]) -> None:
        release_input_state()
        if main is None or not _qt_is_valid(main):
            return
        with suppress(RuntimeError, TypeError):
            main.raise_()
            main.activateWindow()
        chrome = getattr(main, '_chrome', None)
        if chrome is None:
            return
        with suppress(Exception):
            chrome.settle((0, 60, 180, 360, 720))

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

    def _retry_vod_error(self, error) -> bool:
        if self._is_live or self._failed_sent or self._player.source().isEmpty():
            return False
        E = QMediaPlayer.Error
        pos = self._player.position()
        if not (error == E.NetworkError or (error == E.ResourceError and pos > 0)):
            return False
        if self._vod_retries >= self._VOD_MAX_RETRIES:
            return False
        self._vod_retries += 1
        gen, src = self._stream_gen, self._player.source()
        self._pending_seek = (pos, True)
        self._busy(True)

        def _go() -> None:
            if _qt_is_valid(self) and gen == self._stream_gen and not self._failed_sent:
                self._player.setSource(src)
                self._player.play()

        QTimer.singleShot(700 * self._vod_retries, _go)
        return True

    def _on_error(self, error, msg: str) -> None:
        logger.warning("Player error: %s", msg)
        if self._retry_vod_error(error):
            return
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

    def _vid(self) -> VideoSurface:
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
        bw = QWidget(w)
        bw.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        bw.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        bw.setAttribute(Qt.WidgetAttribute.WA_NoMousePropagation)
        bw.hide()
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
                    chrome.settle((0, 60, 180, 360))
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
        if self._bar.width() != bw or self._bar.height() != self._BAR_H:
            self._bar.setFixedSize(bw, self._BAR_H)
        target = QRect(
            (w.width() - bw) // 2,
            w.height() - self._BAR_H - 36,
            bw,
            self._BAR_H,
        )
        if self._fs_bar_win.geometry() != target:
            self._fs_bar_win.setGeometry(target)
        self._fs_bar_win.raise_()


    def _reveal_controls(self, zone: bool = True) -> None:
        if not self._is_fullscreen:
            return
        self._fs_win.unsetCursor()
        if zone:
            self._place_fs_bar()
            if not self._fs_bar_win.isVisible():
                self._fs_bar_win.show()
            self._fs_bar_win.raise_()
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
            if self._fs_bar_win is not None:
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
