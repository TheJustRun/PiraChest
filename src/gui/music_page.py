from __future__ import annotations
import gc
import json
import logging
import math
import os
import re
import time
import uuid
from bisect import bisect_right
import requests
from PySide6.QtCore import Qt, QSize, QTimer, QPointF, QRectF, QPropertyAnimation, QEasingCurve, QObject, QRunnable, QThreadPool, Signal
from PySide6.QtGui import QPixmap, QImage, QImageReader, QFontMetrics, QColor, QPainter, QBrush, QRadialGradient, QIcon, QPolygonF
from PySide6.QtWidgets import QApplication, QWidget, QVBoxLayout, QHBoxLayout, QGridLayout, QLabel, QScrollArea, QGraphicsBlurEffect, QGraphicsOpacityEffect, QSlider, QListWidget, QListWidgetItem, QAbstractItemView, QStackedWidget, QSizePolicy
from qfluentwidgets import BodyLabel, CardWidget, CaptionLabel, StrongBodyLabel, SubtitleLabel, SearchLineEdit, PrimaryPushButton, ToolButton, TransparentToolButton, FluentIcon, RoundMenu, Action, IndeterminateProgressBar, InfoBar, InfoBarPosition, MessageBox, Slider, CheckBox, MessageBoxBase, ScrollArea, qconfig, LineEdit, PushButton, TransparentPushButton
from src.core.worker import submit, cancel, wrap_callback
from src.core.artwork import full_path, has_full, artwork
from src.core.music.player import MusicPreviewPlayer
from src.core.music import music_service
from src.core.music.settings import settings as music_settings, apply_settings as apply_music_settings, QUALITY_TIERS, SLOW_OR_GATED_SOURCES
from src.core.models import MusicItem as Song
from src.core.config import settings as app_settings, paths as app_paths
from src.core.theme import palette
from src.core.translations import tr, register_locale_refresh
logger = logging.getLogger(__name__)
CARD_WIDTH = 172
ART_SIZE = 148
ART_INSET = 10
GRID_SPACING = 14
CARD_HEIGHT = 268
PLAYER_BAR_HEIGHT = 84

def _format_ms(ms: int) -> str:
    if ms <= 0:
        return '0:00'
    total_seconds = ms // 1000
    minutes = total_seconds // 60
    seconds = total_seconds % 60
    return f'{minutes}:{seconds:02d}'

def _format_source(source: str) -> str:
    if not source:
        return ''
    name = source
    for suffix in ('MusicClient', 'Client'):
        if name.endswith(suffix):
            name = name[:-len(suffix)]
            break
    return name or source
_LRC_TIME_RE = re.compile('\\[(\\d+):(\\d+(?:\\.\\d+)?)\\]')

def _parse_lrc(text: str | None) -> list[tuple[int, str]]:
    if not text:
        return []
    lines: list[tuple[int, str]] = []
    for raw_line in text.splitlines():
        matches = list(_LRC_TIME_RE.finditer(raw_line))
        if not matches:
            continue
        content = _LRC_TIME_RE.sub('', raw_line).strip()
        for m in matches:
            ms = int((int(m.group(1)) * 60 + float(m.group(2))) * 1000)
            lines.append((ms, content))
    lines.sort(key=lambda item: item[0])
    return lines
_LRCLIB_HEADERS = {'Lrclib-Client': 'PiraChest v1.0 (https://github.com)'}

def _fetch_lrc_text(song) -> str:
    try:
        artist = (song.singers or '').split(',')[0].strip()
        params = {'track_name': song.song_name or '', 'artist_name': artist}
        if song.album:
            params['album_name'] = song.album
        if song.duration_s:
            params['duration'] = song.duration_s
        resp = requests.get('https://lrclib.net/api/get', params=params, timeout=8, headers=_LRCLIB_HEADERS)
        data = None
        if resp.status_code == 200:
            data = resp.json()
        else:
            resp = requests.get('https://lrclib.net/api/search', params={'track_name': params['track_name'], 'artist_name': artist}, timeout=8, headers=_LRCLIB_HEADERS)
            resp.raise_for_status()
            results = resp.json()
            data = results[0] if results else None
        if data:
            return data.get('syncedLyrics') or data.get('plainLyrics') or ''
    except Exception as exc:
        logger.debug('Lyrics fetch failed for %r: %s', song.song_name, exc)
    return ''

class _LrcFetchSignals(QObject):
    finished = Signal(str, str)

class _LrcFetchTask(QRunnable):

    def __init__(self, song):
        super().__init__()
        self.song = song
        self.signals = _LrcFetchSignals()

    def run(self) -> None:
        self.signals.finished.emit(self.song.key, _fetch_lrc_text(self.song))

def _sanitize_filename(name: str) -> str:
    name = re.sub('[\\\\/:*?"<>|]', '_', name or '').strip().strip('.')
    return name or 'Unknown'

def _unique_path(directory: str, base_name: str, ext: str) -> str:
    candidate = os.path.join(directory, f'{base_name}{ext}')
    if not os.path.exists(candidate):
        return candidate
    n = 1
    while True:
        candidate = os.path.join(directory, f'{base_name} ({n}){ext}')
        if not os.path.exists(candidate):
            return candidate
        n += 1

def _fetch_cover_bytes(song) -> tuple[bytes | None, str]:
    cover_url = getattr(song, 'cover_url', None)
    if not cover_url:
        return (None, '')
    try:
        if has_full('music', cover_url):
            cover_path = full_path('music', cover_url)
            with open(cover_path, 'rb') as fh:
                data = fh.read()
        else:
            resp = requests.get(cover_url, timeout=10)
            resp.raise_for_status()
            data = resp.content
        if data[:8] == b'\x89PNG\r\n\x1a\n':
            return (data, 'image/png')
        return (data, 'image/jpeg')
    except Exception:
        logger.debug('Cover art fetch failed for %r', getattr(song, 'song_name', ''))
        return (None, '')

def _embed_metadata(path: str, song, lrc_text: str) -> None:
    ext = os.path.splitext(path)[1].lower()
    title = song.song_name or ''
    artist = song.singers or ''
    album = song.album or ''
    cover_bytes, cover_mime = _fetch_cover_bytes(song)
    try:
        if ext == '.mp3':
            from mutagen.id3 import ID3, ID3NoHeaderError, TIT2, TPE1, TALB, USLT, APIC
            try:
                tags = ID3(path)
            except ID3NoHeaderError:
                tags = ID3()
            if title:
                tags.setall('TIT2', [TIT2(encoding=3, text=title)])
            if artist:
                tags.setall('TPE1', [TPE1(encoding=3, text=artist)])
            if album:
                tags.setall('TALB', [TALB(encoding=3, text=album)])
            if lrc_text:
                tags.delall('USLT')
                tags.add(USLT(encoding=3, lang='eng', desc='', text=lrc_text))
            if cover_bytes:
                tags.delall('APIC')
                tags.add(APIC(encoding=3, mime=cover_mime, type=3, desc='Cover', data=cover_bytes))
            tags.save(path)
        elif ext == '.flac':
            from mutagen.flac import FLAC, Picture
            audio = FLAC(path)
            if title:
                audio['title'] = title
            if artist:
                audio['artist'] = artist
            if album:
                audio['album'] = album
            if lrc_text:
                audio['LYRICS'] = lrc_text
            if cover_bytes:
                pic = Picture()
                pic.data = cover_bytes
                pic.type = 3
                pic.mime = cover_mime
                pic.desc = 'Cover'
                audio.clear_pictures()
                audio.add_picture(pic)
            audio.save()
        elif ext in ('.m4a', '.mp4', '.aac'):
            from mutagen.mp4 import MP4, MP4Cover
            audio = MP4(path)
            if title:
                audio['©nam'] = [title]
            if artist:
                audio['©ART'] = [artist]
            if album:
                audio['©alb'] = [album]
            if lrc_text:
                audio['©lyr'] = [lrc_text]
            if cover_bytes:
                fmt = MP4Cover.FORMAT_PNG if cover_mime == 'image/png' else MP4Cover.FORMAT_JPEG
                audio['covr'] = [MP4Cover(cover_bytes, imageformat=fmt)]
            audio.save()
        elif ext == '.ogg':
            import base64
            from mutagen.oggvorbis import OggVorbis
            from mutagen.flac import Picture
            audio = OggVorbis(path)
            if title:
                audio['title'] = title
            if artist:
                audio['artist'] = artist
            if album:
                audio['album'] = album
            if lrc_text:
                audio['LYRICS'] = lrc_text
            if cover_bytes:
                pic = Picture()
                pic.data = cover_bytes
                pic.type = 3
                pic.mime = cover_mime
                audio['metadata_block_picture'] = [base64.b64encode(pic.write()).decode('ascii')]
            audio.save()
    except Exception:
        logger.exception('Failed to embed metadata into %s', path)

class _SongDownloadSignals(QObject):
    progress = Signal(int, int)
    finished = Signal(bool, str, str)

def _download_match_key(song) -> tuple[str, str]:
    title = re.sub('\\s+', ' ', (song.song_name or '').strip().lower())
    artist = re.sub('\\s+', ' ', (song.singers or '').split(',')[0].strip().lower())
    return (title, artist)

class _SongDownloadTask(QRunnable):

    def __init__(self, songs: list, dest_dir: str):
        super().__init__()
        self.songs = songs
        self.dest_dir = dest_dir
        self.signals = _SongDownloadSignals()
        self._cancelled = False

    def cancel(self) -> None:
        self._cancelled = True

    def run(self) -> None:
        candidates = [s for s in self.songs if isinstance(s.download_url, str) and s.download_url]
        if not candidates:
            self.signals.finished.emit(False, '', 'No download URL available for this track.')
            return
        last_error = ''
        for song in candidates:
            if self._cancelled:
                self.signals.finished.emit(False, '', 'Cancelled')
                return
            url = song.download_url
            tmp_path = None
            try:
                os.makedirs(self.dest_dir, exist_ok=True)
                resp = requests.get(url, stream=True, timeout=30)
                resp.raise_for_status()
                content_type = resp.headers.get('Content-Type', '')
                ext = os.path.splitext(url.split('?')[0])[1].lower()
                if ext not in ('.mp3', '.flac', '.m4a', '.mp4', '.aac', '.ogg', '.wav'):
                    ext = {'audio/flac': '.flac', 'audio/x-flac': '.flac', 'audio/mp4': '.m4a', 'audio/aac': '.aac', 'audio/ogg': '.ogg', 'audio/wav': '.wav'}.get(content_type.split(';')[0].strip().lower(), '.mp3')
                artist = (song.singers or '').split(',')[0].strip()
                base_name = _sanitize_filename(f'{artist} - {song.song_name}' if artist else song.song_name or tr('download.unknown'))
                final_path = _unique_path(self.dest_dir, base_name, ext)
                tmp_path = final_path + '.part'
                total = int(resp.headers.get('Content-Length', 0) or 0)
                downloaded = 0
                with open(tmp_path, 'wb') as fh:
                    for chunk in resp.iter_content(chunk_size=262144):
                        if self._cancelled:
                            raise RuntimeError('cancelled')
                        if not chunk:
                            continue
                        fh.write(chunk)
                        downloaded += len(chunk)
                        self.signals.progress.emit(downloaded, total)
                os.replace(tmp_path, final_path)
            except Exception as exc:
                if tmp_path and os.path.exists(tmp_path):
                    try:
                        os.remove(tmp_path)
                    except Exception:
                        pass
                if self._cancelled:
                    self.signals.finished.emit(False, '', 'Cancelled')
                    return
                last_error = str(exc)[:300]
                logger.warning('Download failed for %r via %s (%s) — trying next source', song.song_name, song.source, last_error)
                continue
            if not self._cancelled and not getattr(song, 'cover_url', None):
                try:
                    fallback_url = music_service.fetch_fallback_cover_url(song)
                    if fallback_url:
                        song.cover_url = fallback_url
                except Exception:
                    logger.debug('Fallback cover fetch failed for %r', song.song_name)
            lrc_text = _fetch_lrc_text(song)
            if not self._cancelled:
                _embed_metadata(final_path, song, lrc_text)
            self.signals.finished.emit(True, final_path, '')
            return
        self.signals.finished.emit(False, '', last_error or 'All sources failed.')

def _color_distance(a: QColor, b: QColor) -> float:
    dr = a.red() - b.red()
    dg = a.green() - b.green()
    db = a.blue() - b.blue()
    return math.sqrt(dr * dr * 0.3 + dg * dg * 0.59 + db * db * 0.11)

def _tune_for_background(c: QColor) -> QColor:
    h, s, v, _ = c.getHsv()
    h = h if h >= 0 else 0
    s = min(255, max(90, int(s * 1.25)))
    v = max(55, min(v, 160))
    out = QColor()
    out.setHsv(h, s, v, 255)
    return out

def _dominant_colors(path: str, count: int=5, min_distance: float=40.0) -> list[QColor]:
    fallback = [QColor('#5b1a4a'), QColor('#1c2a4a'), QColor('#150a17'), QColor('#3a1230'), QColor('#241436')]
    image = QImage(path)
    if image.isNull():
        return fallback
    small = image.scaled(48, 48, Qt.AspectRatioMode.IgnoreAspectRatio, Qt.TransformationMode.SmoothTransformation)
    small = small.convertToFormat(QImage.Format.Format_RGB32)
    step = 24
    buckets: dict[tuple[int, int, int], list[int]] = {}
    for y in range(small.height()):
        for x in range(small.width()):
            c = small.pixelColor(x, y)
            key = (c.red() // step, c.green() // step, c.blue() // step)
            acc = buckets.setdefault(key, [0, 0, 0, 0])
            acc[0] += c.red()
            acc[1] += c.green()
            acc[2] += c.blue()
            acc[3] += 1
    ranked = sorted(buckets.values(), key=lambda acc: acc[3], reverse=True)
    picked: list[QColor] = []
    for acc in ranked:
        avg = QColor(acc[0] // acc[3], acc[1] // acc[3], acc[2] // acc[3])
        if all((_color_distance(avg, p) >= min_distance for p in picked)):
            picked.append(avg)
        if len(picked) >= count:
            break
    if not picked:
        acc0 = ranked[0]
        picked = [QColor(acc0[0] // acc0[3], acc0[1] // acc0[3], acc0[2] // acc0[3])]
    idx = 1
    while len(picked) < count:
        base = picked[0]
        h, s, v, _ = base.getHsv()
        h = (h + 45 * idx) % 360 if h >= 0 else 45 * idx % 360
        alt = QColor()
        alt.setHsv(h, s, v, 255)
        picked.append(alt)
        idx += 1
    return [_tune_for_background(c) for c in picked[:count]]

class _AnimatedGradientBackground(QWidget):
    _RENDER_SCALE = 0.16

    def __init__(self, parent=None):
        super().__init__(parent)
        self._colors = [QColor('#5b1a4a'), QColor('#1c2a4a'), QColor('#150a17'), QColor('#3a1230'), QColor('#241436')]
        self._base_positions: tuple[tuple[float, float], ...] = ()
        self._speeds: list[float] = []
        self._radii_scale: list[float] = []
        self._phase = 0.0
        self._buffer: QImage | None = None
        self._buffer_phase: float | None = None
        self._timer = QTimer(self)
        self._timer.setInterval(42)
        self._timer.timeout.connect(self._advance)
        self._rebuild_layout()

    def _rebuild_layout(self) -> None:
        n = max(1, len(self._colors))
        positions = []
        speeds = []
        radii = []
        golden_angle = 137.508
        for i in range(n):
            t = (i + 0.5) / n
            angle_deg = i * golden_angle
            r = 0.22 + 0.2 * t
            cx = 0.5 + r * math.cos(math.radians(angle_deg))
            cy = 0.48 + r * math.sin(math.radians(angle_deg)) * 0.9
            positions.append((min(0.92, max(0.08, cx)), min(0.92, max(0.08, cy))))
            speeds.append(0.55 + 0.21 * i)
            radii.append(0.62 + 0.1 * (i % 3))
        self._base_positions = tuple(positions)
        self._speeds = speeds
        self._radii_scale = radii

    def set_colors(self, colors: list[QColor]) -> None:
        colors = list(colors[:5])
        while len(colors) < 5:
            colors.append(colors[-1] if colors else QColor('#20101c'))
        self._colors = colors
        self._rebuild_layout()
        self._buffer_phase = None
        self.update()

    def start_motion(self) -> None:
        if not self._timer.isActive():
            self._timer.start()

    def stop_motion(self) -> None:
        self._timer.stop()

    def _advance(self) -> None:
        self._phase += 0.014
        self.update()

    def _render_buffer(self, bw: int, bh: int) -> QImage:
        image = QImage(bw, bh, QImage.Format.Format_RGB32)
        image.fill(QColor('#0b0710'))
        painter = QPainter(image)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        blob_radius = max(bw, bh) * 0.66
        for i, (bx, by) in enumerate(self._base_positions):
            speed = self._speeds[i]
            angle = self._phase * speed + i * 2.1
            ox = math.sin(angle) * 0.16
            oy = math.cos(angle * 1.15 + i) * 0.16
            cx = (bx + ox) * bw
            cy = (by + oy) * bh
            color = self._colors[i % len(self._colors)]
            radius = blob_radius * self._radii_scale[i % len(self._radii_scale)]
            gradient = QRadialGradient(QPointF(cx, cy), radius)
            near = QColor(color)
            near.setAlpha(235)
            mid = QColor(color)
            mid.setAlpha(90)
            far = QColor(color)
            far.setAlpha(0)
            gradient.setColorAt(0.0, near)
            gradient.setColorAt(0.55, mid)
            gradient.setColorAt(1.0, far)
            painter.setBrush(QBrush(gradient))
            painter.setPen(Qt.PenStyle.NoPen)
            painter.drawEllipse(QPointF(cx, cy), radius, radius)
        painter.end()
        return image

    def paintEvent(self, event) -> None:
        w = max(1, self.width())
        h = max(1, self.height())
        bw = max(24, int(w * self._RENDER_SCALE))
        bh = max(24, int(h * self._RENDER_SCALE))
        if self._buffer is None or self._buffer.width() != bw or self._buffer.height() != bh or (self._buffer_phase != self._phase):
            self._buffer = self._render_buffer(bw, bh)
            self._buffer_phase = self._phase
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        painter.drawImage(self.rect(), self._buffer)

class _LyricLabel(QLabel):
    clicked = Signal()

    def __init__(self, text: str, parent=None):
        super().__init__(text, parent)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit()
        super().mousePressEvent(event)

def _overlay_icon(icon: FluentIcon) -> QIcon:
    return icon.icon(color=QColor('#FFFFFF'))

class LyricsOverlay(QWidget):

    def __init__(self, player: MusicPreviewPlayer, artwork_downloader, player_bar: QWidget, parent=None):
        super().__init__(parent)
        self._player = player
        self._artwork_downloader = artwork_downloader
        self._player_bar = player_bar
        self._lines: list[tuple[int, str]] = []
        self._line_times: list[int] = []
        self._labels: list[QLabel] = []
        self._active_index = -1
        self._current_key: str | None = None
        self._lrc_cache: dict[str, str] = {}
        self._align_mode = 'center'
        self._font_scale = 1.0
        self.setVisible(False)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        self._bg = _AnimatedGradientBackground(self)
        outer.addWidget(self._bg)
        content = QVBoxLayout(self._bg)
        content.setContentsMargins(28, 18, 28, 12)
        content.setSpacing(8)
        top_row = QHBoxLayout()
        self._btn_back = TransparentToolButton(_overlay_icon(FluentIcon.RETURN), self._bg)
        self._btn_back.setFixedSize(34, 34)
        self._btn_back.setIconSize(QSize(15, 15))
        self._btn_back.clicked.connect(self.hide_overlay)
        top_row.addWidget(self._btn_back)
        top_row.addStretch(1)
        self._btn_align = TransparentToolButton(_overlay_icon(FluentIcon.ALIGNMENT), self._bg)
        self._btn_align.setFixedSize(30, 30)
        self._btn_align.setIconSize(QSize(13, 13))
        self._btn_align.setToolTip(tr('music.text_alignment'))
        self._btn_align.clicked.connect(self._on_align_clicked)
        top_row.addWidget(self._btn_align)
        self._btn_font_size = TransparentToolButton(_overlay_icon(FluentIcon.FONT_SIZE), self._bg)
        self._btn_font_size.setFixedSize(30, 30)
        self._btn_font_size.setIconSize(QSize(13, 13))
        self._btn_font_size.setToolTip(tr('music.font_size'))
        self._btn_font_size.clicked.connect(self._on_font_size_clicked)
        top_row.addWidget(self._btn_font_size)
        content.addLayout(top_row)
        self._size_popup = QWidget(self._bg)
        self._size_popup.setFixedSize(132, 34)
        self._size_popup.setStyleSheet('background: rgba(20, 14, 24, 190); border: 1px solid rgba(255, 255, 255, 40); border-radius: 17px;')
        size_popup_layout = QHBoxLayout(self._size_popup)
        size_popup_layout.setContentsMargins(12, 0, 12, 0)
        self._size_slider = Slider(Qt.Orientation.Horizontal, self._size_popup)
        self._size_slider.setRange(70, 150)
        self._size_slider.setValue(100)
        self._size_slider.setFixedWidth(108)
        self._size_slider.valueChanged.connect(self._on_font_scale_changed)
        size_popup_layout.addWidget(self._size_slider)
        self._size_popup.setVisible(False)
        self._scroll = QScrollArea(self._bg)
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        self._scroll.setStyleSheet('QScrollArea { background: transparent; border: none; }QScrollBar:vertical { background: transparent; width: 12px; margin: 6px 3px; }QScrollBar::handle:vertical { background: rgba(255, 255, 255, 55); border: 1px solid rgba(255, 255, 255, 40); border-radius: 6px; min-height: 32px; }QScrollBar::handle:vertical:hover { background: rgba(255, 255, 255, 90); }QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0px; }QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical { background: none; }')
        self._lyrics_content = QWidget()
        self._lyrics_content.setStyleSheet('background: transparent;')
        self._lyrics_layout = QVBoxLayout(self._lyrics_content)
        self._lyrics_layout.setContentsMargins(48, 32, 48, PLAYER_BAR_HEIGHT + 140)
        self._lyrics_layout.setSpacing(26)
        self._scroll.setWidget(self._lyrics_content)
        content.addWidget(self._scroll, 1)
        self._status_lbl = QLabel('', self._bg)
        self._status_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._status_lbl.setStyleSheet('color: rgba(255, 255, 255, 150); font-size: 13px; background: transparent;')
        content.addWidget(self._status_lbl)
        self._anim = QPropertyAnimation(self._scroll.verticalScrollBar(), b'value', self)
        self._anim.setDuration(420)
        self._anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._player.position_changed.connect(self._on_position_changed)
        self._player.song_changed.connect(self._on_song_changed)
        self._player.state_changed.connect(self._on_state_changed)
        self._artwork_downloader.thumb_ready.connect(self._on_artwork_ready)
        register_locale_refresh(self, self._apply_locale)

    def _apply_locale(self, *_args) -> None:
        self._btn_align.setToolTip(tr('music.text_alignment'))
        self._btn_font_size.setToolTip(tr('music.font_size'))

    def _on_align_clicked(self) -> None:
        order = ('center', 'left', 'right')
        self._align_mode = order[(order.index(self._align_mode) + 1) % len(order)]
        flag = {'center': Qt.AlignmentFlag.AlignCenter, 'left': Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, 'right': Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter}[self._align_mode]
        for lbl in self._labels:
            lbl.setAlignment(flag)

    def _on_font_size_clicked(self) -> None:
        if self._size_popup.isVisible():
            self._size_popup.setVisible(False)
            return
        pos = self._btn_font_size.mapTo(self._bg, self._btn_font_size.rect().bottomRight())
        self._size_popup.move(pos.x() - self._size_popup.width(), pos.y() + 6)
        self._size_popup.raise_()
        self._size_popup.setVisible(True)

    def _on_font_scale_changed(self, value: int) -> None:
        self._font_scale = value / 100.0
        for i, lbl in enumerate(self._labels):
            self._style_line(lbl, abs(i - self._active_index) if self._active_index >= 0 else 99)

    def _on_state_changed(self, state: str) -> None:
        if not self.isVisible():
            return
        if state == 'playing':
            self._bg.start_motion()
        else:
            self._bg.stop_motion()

    def show_overlay(self) -> None:
        if self._player.current_song is None:
            return
        self.sync_geometry(force_visible=True)
        self._on_song_changed(self._player.current_song)
        self.raise_()
        self._player_bar.raise_()
        self._player_bar.set_translucent(True)
        self.setVisible(True)
        if self._player.is_playing:
            self._bg.start_motion()
        QTimer.singleShot(0, self._recenter_active)

    def _recenter_active(self) -> None:
        if 0 <= self._active_index < len(self._labels):
            self._scroll_to(self._active_index)

    def hide_overlay(self) -> None:
        self.setVisible(False)
        self._bg.stop_motion()
        self._player_bar.set_translucent(False)

    def sync_geometry(self, force_visible: bool=False) -> None:
        parent = self.parentWidget()
        if parent is None or not (self.isVisible() or force_visible):
            return
        self.setGeometry(0, 0, parent.width(), parent.height())

    def _clear_lines(self) -> None:
        for lbl in self._labels:
            self._lyrics_layout.removeWidget(lbl)
            lbl.deleteLater()
        self._labels = []
        self._active_index = -1
        self._line_times = []

    def _on_song_changed(self, song) -> None:
        self._current_key = song.key if song else None
        self._bg.set_colors([QColor('#5b1a4a'), QColor('#1c2a4a'), QColor('#150a17'), QColor('#3a1230'), QColor('#241436')])
        self._clear_lines()
        self._status_lbl.setText('')
        if song and song.cover_url and has_full('music', song.cover_url):
            self._bg.set_colors(_dominant_colors(full_path('music', song.cover_url)))
        elif song and song.cover_url:
            self._artwork_downloader.request('music', song.cover_url)
        if song is None:
            return
        cached = self._lrc_cache.get(song.key)
        if cached is not None:
            self._apply_lrc(song.key, cached)
            return
        self._status_lbl.setText(tr('music.searching_lyrics'))
        task = _LrcFetchTask(song)
        task.signals.finished.connect(self._on_lrc_fetched)
        QThreadPool.globalInstance().start(task)

    def _on_lrc_fetched(self, song_key: str, lrc_text: str) -> None:
        if not _parse_lrc(lrc_text):
            current = self._player.current_song
            if current is not None and current.key == song_key:
                fallback = getattr(current, 'lyric', None)
                if fallback and _parse_lrc(fallback):
                    lrc_text = fallback
        self._lrc_cache[song_key] = lrc_text
        if song_key != self._current_key:
            return
        self._apply_lrc(song_key, lrc_text)

    def _apply_lrc(self, song_key: str, lrc_text: str) -> None:
        self._clear_lines()
        self._lines = _parse_lrc(lrc_text)
        self._line_times = [t for t, _ in self._lines]
        if not self._lines:
            self._status_lbl.setText(tr('music.no_lyrics_found'))
            return
        self._status_lbl.setText('')
        align_flag = {'center': Qt.AlignmentFlag.AlignCenter, 'left': Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, 'right': Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter}[self._align_mode]
        for time_ms, text in self._lines:
            lbl = _LyricLabel(text or '♪', self._lyrics_content)
            lbl.setWordWrap(True)
            lbl.setAlignment(align_flag)
            self._style_line(lbl, 99)
            lbl.clicked.connect(lambda ms=time_ms: self._player.seek(ms))
            self._lyrics_layout.addWidget(lbl)
            self._labels.append(lbl)
        current = self._player.current_song
        if current is not None and current.key == song_key:
            QTimer.singleShot(0, lambda key=song_key: self._resync_position(key))

    def _resync_position(self, song_key: str) -> None:
        current = self._player.current_song
        if current is None or current.key != song_key:
            return
        self._on_position_changed(self._player.position)

    def _ensure_blur(self, label: QLabel) -> QGraphicsBlurEffect:
        effect = label.graphicsEffect()
        if isinstance(effect, QGraphicsBlurEffect):
            return effect
        effect = QGraphicsBlurEffect(label)
        label.setGraphicsEffect(effect)
        return effect

    def _style_line(self, label: QLabel, distance: int) -> None:
        scale = self._font_scale
        if distance == 0:
            label.setGraphicsEffect(None)
            label.setStyleSheet(f'color: white; font-size: {int(42 * scale)}px; font-weight: 800; background: transparent;')
        elif distance == 1:
            self._ensure_blur(label).setBlurRadius(1.4)
            label.setStyleSheet(f'color: rgba(255, 255, 255, 168); font-size: {int(25 * scale)}px; font-weight: 600; background: transparent;')
        elif distance == 2:
            self._ensure_blur(label).setBlurRadius(2.6)
            label.setStyleSheet(f'color: rgba(255, 255, 255, 120); font-size: {int(23 * scale)}px; font-weight: 500; background: transparent;')
        else:
            self._ensure_blur(label).setBlurRadius(3.6)
            label.setStyleSheet(f'color: rgba(255, 255, 255, 95); font-size: {int(22 * scale)}px; font-weight: 500; background: transparent;')

    def _on_position_changed(self, position_ms: int) -> None:
        if not self.isVisible() or not self._lines:
            return
        idx = bisect_right(self._line_times, position_ms) - 1
        if idx == self._active_index:
            return
        prev_active = self._active_index
        self._active_index = idx
        touched = set()
        for i in (prev_active, idx):
            if 0 <= i < len(self._labels):
                for d in range(-3, 4):
                    j = i + d
                    if 0 <= j < len(self._labels):
                        touched.add(j)
        for j in touched:
            if j in (prev_active, idx):
                continue
            self._style_line(self._labels[j], abs(j - idx) if idx >= 0 else 99)
        if 0 <= prev_active < len(self._labels):
            self._animate_line(self._labels[prev_active], 1)
        if 0 <= idx < len(self._labels):
            self._animate_line(self._labels[idx], 0)
            self._scroll_to(idx)

    def _animate_line(self, label: QLabel, distance: int) -> None:
        self._style_line(label, distance)
        effect = QGraphicsOpacityEffect(label)
        start_opacity = 0.32 if distance == 0 else 1.0
        end_opacity = 1.0 if distance == 0 else 0.55
        effect.setOpacity(start_opacity)
        label.setGraphicsEffect(effect)
        anim = QPropertyAnimation(effect, b'opacity', label)
        anim.setDuration(240)
        anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        anim.setStartValue(start_opacity)
        anim.setEndValue(end_opacity)

        def _cleanup():
            if label.graphicsEffect() is effect:
                self._style_line(label, distance)
        anim.finished.connect(_cleanup)
        anim.start(QPropertyAnimation.DeletionPolicy.DeleteWhenStopped)

    def _scroll_to(self, idx: int) -> None:
        label = self._labels[idx]
        bar = self._scroll.verticalScrollBar()
        target = label.pos().y() + label.height() // 2 - self._scroll.viewport().height() // 2
        target = max(bar.minimum(), min(bar.maximum(), target))
        self._anim.stop()
        self._anim.setStartValue(bar.value())
        self._anim.setEndValue(target)
        self._anim.start()

    def _on_artwork_ready(self, kind: str, url: str, path: str) -> None:
        if kind != 'music':
            return
        song = self._player.current_song
        if song is None or song.cover_url != url:
            return
        self._bg.set_colors(_dominant_colors(path))
_PLAYLISTS_DIR = os.path.join(app_paths.config_dir, 'music')
_PLAYLISTS_FILE = os.path.join(_PLAYLISTS_DIR, 'music_playlists.json')

class Playlist:
    __slots__ = ('id', 'name', 'songs')

    def __init__(self, id: str, name: str, songs: list | None=None):
        self.id = id
        self.name = name
        self.songs: list = songs if songs is not None else []

    def to_dict(self) -> dict:
        return {'id': self.id, 'name': self.name, 'songs': [s.to_dict() for s in self.songs]}

    @classmethod
    def from_dict(cls, data: dict) -> 'Playlist':
        songs = [Song.from_dict(d) for d in data.get('songs', [])]
        return cls(id=data.get('id') or uuid.uuid4().hex, name=data.get('name') or tr('music.untitled'), songs=songs)

class PlaylistStore(QObject):
    playlists_changed = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._playlists: dict[str, Playlist] = {}
        self._order: list[str] = []
        self._load()

    def _load(self) -> None:
        if not os.path.isfile(_PLAYLISTS_FILE):
            return
        try:
            with open(_PLAYLISTS_FILE, 'r', encoding='utf-8') as fh:
                data = json.load(fh)
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning('Failed to load music playlists: %s', exc)
            return
        for entry in data.get('playlists', []):
            try:
                pl = Playlist.from_dict(entry)
            except Exception:
                logger.exception('Failed to parse a saved playlist entry')
                continue
            self._playlists[pl.id] = pl
            self._order.append(pl.id)

    def _save(self) -> None:
        os.makedirs(_PLAYLISTS_DIR, exist_ok=True)
        try:
            payload = {'playlists': [self._playlists[pid].to_dict() for pid in self._order if pid in self._playlists]}
            tmp_path = _PLAYLISTS_FILE + '.tmp'
            with open(tmp_path, 'w', encoding='utf-8') as fh:
                json.dump(payload, fh, indent=2)
            os.replace(tmp_path, _PLAYLISTS_FILE)
        except OSError as exc:
            logger.error('Failed to save music playlists: %s', exc)

    def all_playlists(self) -> list[Playlist]:
        return [self._playlists[pid] for pid in self._order if pid in self._playlists]

    def get(self, playlist_id: str) -> Playlist | None:
        return self._playlists.get(playlist_id)

    def create(self, name: str) -> Playlist:
        name = (name or '').strip() or tr('music.untitled_playlist')
        pl = Playlist(id=uuid.uuid4().hex, name=name)
        self._playlists[pl.id] = pl
        self._order.append(pl.id)
        self._save()
        self.playlists_changed.emit()
        return pl

    def rename(self, playlist_id: str, new_name: str) -> None:
        pl = self._playlists.get(playlist_id)
        if pl is None:
            return
        new_name = (new_name or '').strip()
        if not new_name:
            return
        pl.name = new_name
        self._save()
        self.playlists_changed.emit()

    def delete(self, playlist_id: str) -> None:
        if playlist_id not in self._playlists:
            return
        del self._playlists[playlist_id]
        if playlist_id in self._order:
            self._order.remove(playlist_id)
        self._save()
        self.playlists_changed.emit()

    def add_song(self, playlist_id: str, song) -> bool:
        pl = self._playlists.get(playlist_id)
        if pl is None:
            return False
        if any((s.key == song.key for s in pl.songs)):
            return False
        pl.songs.append(song)
        self._save()
        self.playlists_changed.emit()
        return True

    def remove_song(self, playlist_id: str, song_key: str) -> None:
        pl = self._playlists.get(playlist_id)
        if pl is None:
            return
        pl.songs = [s for s in pl.songs if s.key != song_key]
        self._save()
        self.playlists_changed.emit()

class SaveToPlaylistDialog(MessageBoxBase):

    def __init__(self, store: PlaylistStore, song, parent=None):
        super().__init__(parent)
        self._store = store
        self._song = song
        self._selected_id: str | None = None
        self.titleLabel = StrongBodyLabel(tr('music.save_to_playlist_title'), self)
        self.viewLayout.addWidget(self.titleLabel)
        self._list = QListWidget(self)
        self._list.setFixedHeight(180)
        self._list.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self._populate_list()
        self._list.itemSelectionChanged.connect(self._on_selection_changed)
        self._list.itemDoubleClicked.connect(self._on_item_double_clicked)
        self.viewLayout.addWidget(self._list)
        new_row = QHBoxLayout()
        self._new_name_input = LineEdit(self)
        self._new_name_input.setPlaceholderText(tr('music.new_playlist_placeholder'))
        self._btn_create = PushButton(tr('music.create'), self)
        self._btn_create.clicked.connect(self._on_create_clicked)
        new_row.addWidget(self._new_name_input, 1)
        new_row.addWidget(self._btn_create, 0)
        self.viewLayout.addLayout(new_row)
        self.yesButton.setText(tr('music.save'))
        self.cancelButton.setText(tr('music.cancel'))
        self.widget.setMinimumWidth(360)

    def _populate_list(self) -> None:
        self._list.clear()
        for pl in self._store.all_playlists():
            item = QListWidgetItem(f'{pl.name} ({len(pl.songs)})')
            item.setData(Qt.ItemDataRole.UserRole, pl.id)
            self._list.addItem(item)

    def _on_selection_changed(self) -> None:
        items = self._list.selectedItems()
        self._selected_id = items[0].data(Qt.ItemDataRole.UserRole) if items else None

    def _on_item_double_clicked(self, item: QListWidgetItem) -> None:
        self._selected_id = item.data(Qt.ItemDataRole.UserRole)
        self.accept()

    def _on_create_clicked(self) -> None:
        name = self._new_name_input.text().strip()
        if not name:
            return
        pl = self._store.create(name)
        self._new_name_input.clear()
        self._populate_list()
        for i in range(self._list.count()):
            item = self._list.item(i)
            if item.data(Qt.ItemDataRole.UserRole) == pl.id:
                self._list.setCurrentItem(item)
                break

    def selected_playlist_id(self) -> str | None:
        return self._selected_id

class SongCard(CardWidget):

    def __init__(self, song, artwork_downloader, parent=None, play_requested=None, download_requested=None, artwork_settled=None, save_playlist_requested=None):
        super().__init__(parent)
        self.song = song
        self.setFixedSize(CARD_WIDTH, CARD_HEIGHT)
        self.setBorderRadius(12)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 4)
        layout.setSpacing(3)
        art_wrap = QWidget(self)
        art_wrap.setFixedSize(CARD_WIDTH, ART_SIZE + ART_INSET * 2)
        art_wrap_layout = QVBoxLayout(art_wrap)
        art_wrap_layout.setContentsMargins(ART_INSET, ART_INSET, ART_INSET, ART_INSET)
        self._art_label = QLabel(art_wrap)
        self._art_label.setFixedSize(ART_SIZE, ART_SIZE)
        self._art_label.setScaledContents(True)
        self._apply_art_placeholder()
        art_wrap_layout.addWidget(self._art_label)
        layout.addWidget(art_wrap)
        text_col = QVBoxLayout()
        text_col.setContentsMargins(12, 0, 12, 0)
        text_col.setSpacing(1)
        self._title_lbl = StrongBodyLabel(self)
        self._title_lbl.setStyleSheet('font-size: 14px;')
        self._artist_lbl = CaptionLabel(self)
        self._album_lbl = CaptionLabel(self)
        self._duration_lbl = CaptionLabel(song.duration or '', self)
        self._muted_labels = (self._artist_lbl, self._album_lbl, self._duration_lbl)
        self._refresh_muted_style()
        self._set_elided(self._title_lbl, song.song_name or tr('download.unknown'))
        self._set_elided(self._artist_lbl, song.singers or tr('music.unknown_artist'))
        self._set_elided(self._album_lbl, song.album or '')
        text_col.addWidget(self._title_lbl)
        text_col.addWidget(self._artist_lbl)
        meta_row = QHBoxLayout()
        meta_row.setSpacing(6)
        meta_row.addWidget(self._album_lbl, 1)
        meta_row.addWidget(self._duration_lbl, 0)
        text_col.addLayout(meta_row)
        self._quality_lbl = QLabel(song.quality_label, self)
        self._quality_lbl.setVisible(bool(song.quality_label))
        self._quality_lbl.setFixedHeight(18)
        self._apply_quality_pill_style()
        self._source_lbl = QLabel(_format_source(song.source), self)
        self._source_lbl.setVisible(bool(song.source))
        self._source_lbl.setFixedHeight(18)
        self._apply_source_pill_style()
        pill_row = QHBoxLayout()
        pill_row.setContentsMargins(0, 2, 0, 0)
        pill_row.setSpacing(4)
        pill_row.addWidget(self._quality_lbl, 0)
        pill_row.addWidget(self._source_lbl, 0)
        pill_row.addStretch(1)
        text_col.addLayout(pill_row)
        layout.addLayout(text_col)
        layout.addStretch(1)
        btn_row = QHBoxLayout()
        btn_row.setContentsMargins(10, 0, 10, 0)
        btn_row.setSpacing(4)
        self._btn_play = ToolButton(FluentIcon.PLAY, self)
        self._btn_download = ToolButton(FluentIcon.DOWNLOAD, self)
        self._btn_playlist = ToolButton(FluentIcon.ADD_TO, self)
        self._btn_playlist.setToolTip(tr('music.save_to_playlist'))
        self._btn_more = TransparentToolButton(FluentIcon.MORE, self)
        for b in (self._btn_play, self._btn_download, self._btn_playlist, self._btn_more):
            b.setFixedSize(27, 27)
            b.setIconSize(QSize(13, 13))
        btn_row.addWidget(self._btn_play)
        btn_row.addWidget(self._btn_download)
        btn_row.addWidget(self._btn_playlist)
        btn_row.addStretch(1)
        btn_row.addWidget(self._btn_more)
        layout.addLayout(btn_row)
        self._btn_play.clicked.connect(self._on_play_clicked)
        self._btn_download.clicked.connect(self._on_download_clicked)
        self._btn_playlist.clicked.connect(self._on_save_playlist_clicked)
        self._btn_more.clicked.connect(self._show_menu)
        self.play_requested = play_requested
        self.download_requested = download_requested
        self.artwork_settled = artwork_settled
        self.save_playlist_requested = save_playlist_requested
        self._artwork_downloader = artwork_downloader
        self._artwork_downloader.thumb_ready.connect(self._on_artwork_ready)
        self._artwork_downloader.failed.connect(self._on_artwork_failed)
        if song.cover_url:
            self._artwork_downloader.request('music', song.cover_url)
        else:
            submit(music_service.fetch_fallback_cover_url, args=(song,), on_done=self._on_fallback_cover_found)
        qconfig.themeChanged.connect(self._on_theme_changed)

    def _on_fallback_cover_found(self, cover_url: str | None) -> None:
        if not cover_url:
            return
        try:
            if self.song.cover_url:
                return
            self.song.cover_url = cover_url
            self._artwork_downloader.request('music', cover_url)
        except RuntimeError:
            pass

    def _set_elided(self, label, text: str) -> None:
        metrics = QFontMetrics(label.font())
        available = CARD_WIDTH - 28
        elided = metrics.elidedText(text, Qt.TextElideMode.ElideRight, available)
        label.setText(elided)
        if elided != text:
            label.setToolTip(text)

    def detach(self) -> None:
        try:
            self._artwork_downloader.thumb_ready.disconnect(self._on_artwork_ready)
        except (TypeError, RuntimeError):
            pass
        try:
            self._artwork_downloader.failed.disconnect(self._on_artwork_failed)
        except (TypeError, RuntimeError):
            pass
        try:
            qconfig.themeChanged.disconnect(self._on_theme_changed)
        except (TypeError, RuntimeError):
            pass
        self._art_label.clear()

    def _apply_art_placeholder(self):
        c = palette()
        self._art_label.setStyleSheet(f'background-color: {c['poster_fallback_bg']}; border: 1px solid {c['surface_border']}; border-radius: 8px;')

    def _apply_quality_pill_style(self):
        c = palette()
        self._quality_lbl.setStyleSheet(f'background-color: {c['surface_tint_strong']}; color: {c['muted']}; border-radius: 9px; padding: 1px 8px; font-size: 10px; font-weight: 600;')

    def _apply_source_pill_style(self):
        c = palette()
        self._source_lbl.setStyleSheet(f'background-color: transparent; color: {c['muted']}; border: 1px solid {c['surface_border']}; border-radius: 9px; padding: 1px 8px; font-size: 10px; font-weight: 600;')

    def _refresh_muted_style(self):
        for lbl in self._muted_labels:
            lbl.setStyleSheet(f'color: {palette()['muted']}; font-size: 10.5px;')

    def _on_theme_changed(self, *_):
        self._refresh_muted_style()
        self._apply_quality_pill_style()
        self._apply_source_pill_style()
        if self._art_label.pixmap() is None or self._art_label.pixmap().isNull():
            self._apply_art_placeholder()

    def _on_artwork_ready(self, kind: str, url: str, path: str) -> None:
        if kind != 'music':
            return
        if url != self.song.cover_url:
            return
        reader = QImageReader(path)
        reader.setAutoTransform(True)
        size = reader.size()
        if size.isValid() and (size.width() > ART_SIZE or size.height() > ART_SIZE):
            scale = ART_SIZE / max(size.width(), size.height())
            reader.setScaledSize(QSize(max(1, int(size.width() * scale)), max(1, int(size.height() * scale))))
        image = reader.read()
        if image.isNull():
            self._notify_artwork_settled()
            return
        pix = QPixmap.fromImage(image).scaled(ART_SIZE, ART_SIZE, Qt.AspectRatioMode.KeepAspectRatioByExpanding, Qt.TransformationMode.SmoothTransformation)
        self._art_label.setStyleSheet(f'border: 1px solid {palette()['surface_border']}; border-radius: 8px;')
        self._art_label.setPixmap(pix)
        self._notify_artwork_settled()

    def _on_artwork_failed(self, kind: str, url: str, error: str) -> None:
        if kind != 'music':
            return
        if url != self.song.cover_url:
            return
        self._notify_artwork_settled()

    def _notify_artwork_settled(self) -> None:
        if callable(self.artwork_settled):
            self.artwork_settled(self.song.key)

    def _on_play_clicked(self):
        if callable(self.play_requested):
            self.play_requested(self.song)

    def _on_download_clicked(self):
        if callable(self.download_requested):
            self.download_requested(self.song)

    def _on_save_playlist_clicked(self):
        if callable(self.save_playlist_requested):
            self.save_playlist_requested(self.song)

    def _show_menu(self):
        menu = RoundMenu(parent=self)
        play_action = Action(FluentIcon.PLAY, tr('music.play'))
        play_action.triggered.connect(self._on_play_clicked)
        download_action = Action(FluentIcon.DOWNLOAD, tr('music.download_action'))
        download_action.triggered.connect(self._on_download_clicked)
        playlist_action = Action(FluentIcon.ADD_TO, tr('music.save_to_playlist_title'))
        playlist_action.triggered.connect(self._on_save_playlist_clicked)
        menu.addAction(play_action)
        menu.addAction(download_action)
        menu.addAction(playlist_action)
        menu.exec(self._btn_more.mapToGlobal(self._btn_more.rect().bottomLeft()))

def _make_track_icon(forward: bool) -> QIcon:
    size = 22
    top, bottom, mid = (4.0, 18.0, 11.0)
    pix = QPixmap(size, size)
    pix.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pix)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QColor('white'))
    if forward:
        painter.drawPolygon(QPolygonF([QPointF(10, mid), QPointF(3, top), QPointF(3, bottom)]))
        painter.drawPolygon(QPolygonF([QPointF(17, mid), QPointF(10, top), QPointF(10, bottom)]))
        painter.drawRect(QRectF(17.5, top, 2.2, bottom - top))
    else:
        painter.drawRect(QRectF(2.5, top, 2.2, bottom - top))
        painter.drawPolygon(QPolygonF([QPointF(5, mid), QPointF(12, top), QPointF(12, bottom)]))
        painter.drawPolygon(QPolygonF([QPointF(12, mid), QPointF(19, top), QPointF(19, bottom)]))
    painter.end()
    return QIcon(pix)

class _AccentSlider(QSlider):

    def __init__(self, orientation, parent=None):
        super().__init__(orientation, parent)
        self._accent = QColor('#8a8a94')
        self._dragging = False
        self.setFixedHeight(20)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.valueChanged.connect(lambda _=0: self.update())

    def setAccentColor(self, color: QColor) -> None:
        self._accent = QColor(color)
        self.update()

    def _value_from_x(self, x: float) -> int:
        margin = 7.0
        usable = max(1.0, self.width() - 2 * margin)
        frac = min(1.0, max(0.0, (x - margin) / usable))
        span = self.maximum() - self.minimum()
        return self.minimum() + round(frac * span)

    def mousePressEvent(self, event) -> None:
        if event.button() != Qt.MouseButton.LeftButton:
            return
        self._dragging = True
        self.setValue(self._value_from_x(event.position().x()))
        self.sliderPressed.emit()

    def mouseMoveEvent(self, event) -> None:
        if not self._dragging:
            return
        value = self._value_from_x(event.position().x())
        self.setValue(value)
        self.sliderMoved.emit(value)

    def mouseReleaseEvent(self, event) -> None:
        if not self._dragging:
            return
        self._dragging = False
        self.sliderReleased.emit()

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        margin = 7.0
        groove_h = 4.0
        mid_y = self.height() / 2.0
        groove = QRectF(margin, mid_y - groove_h / 2, max(0.0, self.width() - 2 * margin), groove_h)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(255, 255, 255, 46))
        painter.drawRoundedRect(groove, groove_h / 2, groove_h / 2)
        span = self.maximum() - self.minimum()
        frac = 0.0 if span <= 0 else (self.value() - self.minimum()) / span
        filled_w = groove.width() * frac
        if filled_w > 0:
            filled = QRectF(groove.x(), groove.y(), filled_w, groove_h)
            painter.setBrush(self._accent)
            painter.drawRoundedRect(filled, groove_h / 2, groove_h / 2)
        handle_x = groove.x() + filled_w
        painter.setBrush(QColor('white'))
        painter.drawEllipse(QPointF(handle_x, mid_y), 6.0, 6.0)
_BAR_TEXT = '#F2F2F5'
_BAR_MUTED = '#9CA3AF'
_BAR_BG = 'rgba(22, 18, 26, 235)'
_BAR_BG_TRANSLUCENT = 'rgba(22, 18, 26, 150)'
_BAR_BORDER = 'rgba(255, 255, 255, 14)'
_BAR_ART_BG = 'rgba(255, 255, 255, 0.07)'

def _bar_icon(icon: FluentIcon) -> QIcon:
    return icon.icon(color=QColor(_BAR_TEXT))

def _format_quality_detail(song) -> str:
    raw = getattr(song, 'raw', None) or {}
    parts = []
    bitrate = raw.get('bitrate') or raw.get('bit_rate') or raw.get('br')
    if bitrate:
        try:
            bitrate = int(float(bitrate))
            if bitrate < 50:
                bitrate *= 1000
            parts.append(f'{bitrate // 1000}kb/s' if bitrate >= 1000 else f'{bitrate}kb/s')
        except (TypeError, ValueError):
            pass
    sample_rate = raw.get('sample_rate') or raw.get('samplerate') or raw.get('sr')
    if sample_rate:
        try:
            sample_rate = float(sample_rate)
            if sample_rate > 1000:
                sample_rate /= 1000
            parts.append(f'{sample_rate:g}kHz')
        except (TypeError, ValueError):
            pass
    quality_label = getattr(song, 'quality_label', '') or ''
    if quality_label:
        parts.append(quality_label)
    ext = (getattr(song, 'ext', '') or '').lstrip('.').upper()
    if ext and ext not in ''.join(parts).upper():
        parts.append(ext)
    return ' · '.join(parts)

class MiniPlayerBar(CardWidget):
    lyrics_requested = Signal()

    def __init__(self, player: MusicPreviewPlayer, parent=None):
        super().__init__(parent)
        self._player = player
        self._seeking = False
        self._translucent = False
        self.setFixedHeight(PLAYER_BAR_HEIGHT)
        self.setBorderRadius(18)
        self._apply_bar_background()
        layout = QHBoxLayout(self)
        layout.setContentsMargins(16, 9, 18, 9)
        layout.setSpacing(16)
        self._art_label = QLabel(self)
        self._art_label.setFixedSize(58, 58)
        self._art_label.setScaledContents(True)
        self._apply_art_placeholder()
        layout.addWidget(self._art_label)
        info_col = QVBoxLayout()
        info_col.setSpacing(1)
        info_col.setContentsMargins(0, 0, 0, 0)
        self._title_lbl = StrongBodyLabel(tr('music.no_track_playing'), self)
        self._title_lbl.setStyleSheet(f'color: {_BAR_TEXT}; font-size: 15px; background: transparent;')
        self._artist_lbl = CaptionLabel('', self)
        self._artist_lbl.setStyleSheet(f'color: {_BAR_MUTED}; font-size: 12.5px; background: transparent;')
        self._quality_info_lbl = CaptionLabel('', self)
        self._quality_info_lbl.setStyleSheet(f'color: {_BAR_MUTED}; font-size: 10.5px; background: transparent;')
        info_col.addStretch(1)
        info_col.addWidget(self._title_lbl)
        info_col.addWidget(self._artist_lbl)
        info_col.addWidget(self._quality_info_lbl)
        info_col.addStretch(1)
        info_wrap = QWidget(self)
        info_wrap.setMinimumWidth(190)
        info_wrap.setLayout(info_col)
        layout.addWidget(info_wrap)
        center_col = QVBoxLayout()
        center_col.setSpacing(3)
        transport_row = QHBoxLayout()
        transport_row.setSpacing(6)
        transport_row.addStretch(1)
        self._btn_shuffle = TransparentToolButton(_bar_icon(FluentIcon.ROTATE), self)
        self._btn_prev = TransparentToolButton(self)
        self._btn_prev.setIcon(_make_track_icon(False))
        self._btn_play = TransparentToolButton(_bar_icon(FluentIcon.PLAY), self)
        self._btn_next = TransparentToolButton(self)
        self._btn_next.setIcon(_make_track_icon(True))
        self._btn_repeat = TransparentToolButton(_bar_icon(FluentIcon.SYNC), self)
        for b in (self._btn_shuffle, self._btn_prev, self._btn_next, self._btn_repeat):
            b.setFixedSize(30, 30)
            b.setIconSize(QSize(14, 14))
        self._btn_prev.setIconSize(QSize(17, 17))
        self._btn_next.setIconSize(QSize(17, 17))
        self._btn_play.setFixedSize(38, 38)
        self._btn_play.setIconSize(QSize(15, 15))
        self._btn_shuffle.setCheckable(True)
        self._btn_repeat.setCheckable(True)
        transport_row.addWidget(self._btn_shuffle)
        transport_row.addWidget(self._btn_prev)
        transport_row.addWidget(self._btn_play)
        transport_row.addWidget(self._btn_next)
        transport_row.addWidget(self._btn_repeat)
        transport_row.addStretch(1)
        center_col.addLayout(transport_row)
        seek_row = QHBoxLayout()
        seek_row.setSpacing(8)
        self._current_time_lbl = CaptionLabel('0:00', self)
        self._current_time_lbl.setFixedWidth(36)
        self._current_time_lbl.setStyleSheet(f'color: {_BAR_MUTED}; background: transparent;')
        self._current_time_lbl.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self._seek_slider = _AccentSlider(Qt.Orientation.Horizontal, self)
        self._seek_slider.setRange(0, 0)
        self._total_time_lbl = CaptionLabel('0:00', self)
        self._total_time_lbl.setFixedWidth(36)
        self._total_time_lbl.setStyleSheet(f'color: {_BAR_MUTED}; background: transparent;')
        seek_row.addWidget(self._current_time_lbl)
        seek_row.addWidget(self._seek_slider, 1)
        seek_row.addWidget(self._total_time_lbl)
        center_col.addLayout(seek_row)
        layout.addLayout(center_col, 1)
        self._btn_lyrics = TransparentToolButton(_bar_icon(FluentIcon.LABEL), self)
        self._btn_lyrics.setFixedSize(28, 28)
        self._btn_lyrics.setIconSize(QSize(14, 14))
        self._btn_lyrics.setToolTip(tr('music.lyrics'))
        self._btn_lyrics.setEnabled(False)
        self._btn_lyrics.clicked.connect(self.lyrics_requested.emit)
        layout.addWidget(self._btn_lyrics)
        self._btn_mute = TransparentToolButton(_bar_icon(FluentIcon.VOLUME), self)
        self._btn_mute.setFixedSize(28, 28)
        self._btn_mute.setIconSize(QSize(14, 14))
        self._volume_slider = _AccentSlider(Qt.Orientation.Horizontal, self)
        self._volume_slider.setRange(0, 100)
        self._volume_slider.setValue(100)
        self._volume_slider.setFixedWidth(90)
        layout.addWidget(self._btn_mute)
        layout.addWidget(self._volume_slider)
        self._btn_play.clicked.connect(self._player.toggle_play_pause)
        self._btn_prev.clicked.connect(self._player.previous)
        self._btn_next.clicked.connect(self._player.next)
        self._btn_repeat.toggled.connect(self._player.set_repeat)
        self._btn_shuffle.toggled.connect(self._player.set_shuffle)
        self._btn_mute.clicked.connect(self._on_mute_clicked)
        self._volume_slider.valueChanged.connect(self._on_volume_changed)
        self._seek_slider.sliderPressed.connect(self._on_seek_pressed)
        self._seek_slider.sliderReleased.connect(self._on_seek_released)
        self._seek_slider.sliderMoved.connect(self._on_seek_moved)
        self._player.song_changed.connect(self._on_song_changed)
        self._player.state_changed.connect(self._on_state_changed)
        self._player.position_changed.connect(self._on_position_changed)
        self._player.duration_changed.connect(self._on_duration_changed)
        self._player.error.connect(self._on_player_error)
        self._artwork_downloader = None
        register_locale_refresh(self, self._apply_locale)

    def _apply_locale(self, *_args) -> None:
        self._btn_lyrics.setToolTip(tr('music.lyrics'))
        if self._player.current_song is None:
            self._title_lbl.setText(tr('music.no_track_playing'))
        qconfig.themeChanged.connect(self._on_theme_changed)
        self._apply_accent(QColor('#8a8a94'))

    def _apply_bar_background(self) -> None:
        bg = _BAR_BG_TRANSLUCENT if self._translucent else _BAR_BG
        border_radius = 18 if not self._translucent else 18
        self.setStyleSheet(f'MiniPlayerBar {{ background-color: {bg}; border: 1px solid {_BAR_BORDER}; border-radius: {border_radius}px; }}')

    def set_translucent(self, enabled: bool) -> None:
        self._translucent = enabled
        self._apply_bar_background()

    def set_artwork_downloader(self, downloader) -> None:
        self._artwork_downloader = downloader
        downloader.thumb_ready.connect(self._on_artwork_ready)

    def _apply_art_placeholder(self):
        self._art_label.setStyleSheet(f'background-color: {_BAR_ART_BG}; border-radius: 10px;')

    def _on_theme_changed(self, *_):
        if self._art_label.pixmap() is None or self._art_label.pixmap().isNull():
            self._apply_art_placeholder()

    def _on_song_changed(self, song) -> None:
        if song is None:
            self._title_lbl.setText(tr('music.no_track_playing'))
            self._artist_lbl.setText('')
            self._quality_info_lbl.setText('')
            self._apply_art_placeholder()
            self._apply_accent(QColor('#8a8a94'))
            self._btn_lyrics.setEnabled(False)
            return
        self._title_lbl.setText(song.song_name or tr('download.unknown'))
        self._artist_lbl.setText(song.singers or tr('music.unknown_artist'))
        self._quality_info_lbl.setText(_format_quality_detail(song))
        self._apply_art_placeholder()
        self._apply_accent(QColor('#8a8a94'))
        self._btn_lyrics.setEnabled(True)
        if song.cover_url and self._artwork_downloader is not None:
            self._artwork_downloader.request('music', song.cover_url)

    def _apply_accent(self, color: QColor) -> None:
        self._accent = QColor(color)
        self._seek_slider.setAccentColor(self._accent)
        self._volume_slider.setAccentColor(self._accent)
        normal = self._accent.name()
        hover = self._accent.lighter(118).name()
        pressed = self._accent.darker(112).name()
        self._btn_play.setStyleSheet(f'TransparentToolButton {{ background-color: {normal}; border-radius: 19px; }} TransparentToolButton:hover {{ background-color: {hover}; }} TransparentToolButton:pressed {{ background-color: {pressed}; }}')

    def _pick_vivid_color(self, colors: list[QColor]) -> QColor:
        best = colors[0] if colors else QColor('#8a8a94')
        best_score = -1.0
        for c in colors:
            h, s, v, _ = c.getHsv()
            score = s * v
            if score > best_score:
                best_score = score
                best = c
        return best

    def _on_artwork_ready(self, kind: str, url: str, path: str) -> None:
        if kind != 'music':
            return
        song = self._player.current_song
        if song is None or song.cover_url != url:
            return
        reader = QImageReader(path)
        reader.setAutoTransform(True)
        size = reader.size()
        target = 58
        if size.isValid() and (size.width() > target or size.height() > target):
            scale = target / max(size.width(), size.height())
            reader.setScaledSize(QSize(max(1, int(size.width() * scale)), max(1, int(size.height() * scale))))
        image = reader.read()
        if image.isNull():
            return
        pix = QPixmap.fromImage(image).scaled(target, target, Qt.AspectRatioMode.KeepAspectRatioByExpanding, Qt.TransformationMode.SmoothTransformation)
        self._art_label.setStyleSheet('border-radius: 10px;')
        self._art_label.setPixmap(pix)
        self._apply_accent(self._pick_vivid_color(_dominant_colors(path)))

    def _on_state_changed(self, state: str) -> None:
        self._btn_play.setIcon(_bar_icon(FluentIcon.PAUSE if state == 'playing' else FluentIcon.PLAY))

    def _on_position_changed(self, position_ms: int) -> None:
        if self._seeking:
            return
        self._seek_slider.setValue(position_ms)
        text = _format_ms(position_ms)
        if text != self._current_time_lbl.text():
            self._current_time_lbl.setText(text)

    def _on_duration_changed(self, duration_ms: int) -> None:
        self._seek_slider.setRange(0, max(0, duration_ms))
        self._total_time_lbl.setText(_format_ms(duration_ms))

    def _on_seek_pressed(self) -> None:
        self._seeking = True

    def _on_seek_released(self) -> None:
        self._seeking = False
        self._player.seek(self._seek_slider.value())

    def _on_seek_moved(self, value: int) -> None:
        self._player.seek(value)
        self._current_time_lbl.setText(_format_ms(value))

    def _on_volume_changed(self, value: int) -> None:
        self._player.set_volume(value / 100)
        self._btn_mute.setIcon(_bar_icon(FluentIcon.MUTE if value == 0 else FluentIcon.VOLUME))

    def _on_mute_clicked(self) -> None:
        muted = not self._player.is_muted
        self._player.set_muted(muted)
        self._btn_mute.setIcon(_bar_icon(FluentIcon.MUTE if muted else FluentIcon.VOLUME))

    def _on_player_error(self, message: str) -> None:
        InfoBar.error(title=tr('music.playback_error_title'), content=message[:120], orient=Qt.Orientation.Horizontal, isClosable=True, position=InfoBarPosition.TOP_RIGHT, duration=4000, parent=self.window())

def _quality_labels() -> dict[str, str]:
    return {'lossless': tr('music.quality_lossless'), 'mp3': 'MP3', 'aac': 'AAC / M4A', 'other': tr('music.quality_other')}

class FilterDialog(MessageBoxBase):

    def __init__(self, all_sources: list[str], selected_sources: list[str], selected_quality: list[str], parent=None):
        super().__init__(parent)
        self.titleLabel = SubtitleLabel(tr('music.search_filters'), self)
        self.viewLayout.addWidget(self.titleLabel)
        quality_header = StrongBodyLabel(tr('music.quality'), self)
        self.viewLayout.addWidget(quality_header)
        self._quality_checks: dict[str, CheckBox] = {}
        quality_col = QVBoxLayout()
        quality_col.setSpacing(4)
        for tier in QUALITY_TIERS:
            cb = CheckBox(_quality_labels().get(tier, tier), self)
            cb.setChecked(tier in selected_quality)
            self._quality_checks[tier] = cb
            quality_col.addWidget(cb)
        self.viewLayout.addLayout(quality_col)
        sources_header = StrongBodyLabel(tr('music.sources'), self)
        self.viewLayout.addWidget(sources_header)
        scroll = ScrollArea(self)
        scroll.setFixedHeight(220)
        scroll.setWidgetResizable(True)
        scroll.setStyleSheet('background: transparent; border: none;')
        sources_container = QWidget(scroll)
        sources_container.setStyleSheet('background: transparent;')
        sources_col = QVBoxLayout(sources_container)
        sources_col.setSpacing(4)
        self._source_checks: dict[str, CheckBox] = {}
        sorted_sources = sorted(all_sources, key=lambda s: (s in SLOW_OR_GATED_SOURCES, s))
        for source in sorted_sources:
            label = tr('music.source_slow_setup', source=source) if source in SLOW_OR_GATED_SOURCES else source
            cb = CheckBox(label, sources_container)
            cb.setChecked(source in selected_sources)
            self._source_checks[source] = cb
            sources_col.addWidget(cb)
        sources_col.addStretch(1)
        scroll.setWidget(sources_container)
        self.viewLayout.addWidget(scroll)
        self.yesButton.setText(tr('music.apply'))
        self.cancelButton.setText(tr('music.cancel'))
        self.widget.setMinimumWidth(360)

    def selected_sources(self) -> list[str]:
        return [source for source, cb in self._source_checks.items() if cb.isChecked()]

    def selected_quality(self) -> list[str]:
        return [tier for tier, cb in self._quality_checks.items() if cb.isChecked()]

class _PlaylistCoverLabel(QLabel):

    def __init__(self, size: int, artwork_downloader, parent=None):
        super().__init__(parent)
        self._size = size
        self._artwork_downloader = artwork_downloader
        self._cover_url: str | None = None
        self.setFixedSize(size, size)
        self._apply_placeholder()
        if artwork_downloader is not None:
            artwork_downloader.thumb_ready.connect(self._on_artwork_ready)

    def _apply_placeholder(self) -> None:
        c = palette()
        self.setPixmap(FluentIcon.ALBUM.icon(color=QColor(c['muted'])).pixmap(int(self._size * 0.45), int(self._size * 0.45)))
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setStyleSheet(f'background-color: {c['surface_tint_strong']}; border-radius: {min(10, self._size // 4)}px;')

    def set_cover(self, cover_url: str | None) -> None:
        self._cover_url = cover_url
        self._apply_placeholder()
        if cover_url and self._artwork_downloader is not None:
            self._artwork_downloader.request('music', cover_url)

    def _on_artwork_ready(self, kind: str, url: str, path: str) -> None:
        if kind != 'music':
            return
        if not self._cover_url or url != self._cover_url:
            return
        reader = QImageReader(path)
        reader.setAutoTransform(True)
        size = reader.size()
        if size.isValid() and (size.width() > self._size or size.height() > self._size):
            scale = self._size / max(size.width(), size.height())
            reader.setScaledSize(QSize(max(1, int(size.width() * scale)), max(1, int(size.height() * scale))))
        image = reader.read()
        if image.isNull():
            return
        pix = QPixmap.fromImage(image).scaled(self._size, self._size, Qt.AspectRatioMode.KeepAspectRatioByExpanding, Qt.TransformationMode.SmoothTransformation)
        self.setStyleSheet(f'border: 1px solid {palette()['surface_border']}; border-radius: {min(10, self._size // 4)}px;')
        self.setPixmap(pix)

    def detach(self) -> None:
        if self._artwork_downloader is None:
            return
        try:
            self._artwork_downloader.thumb_ready.disconnect(self._on_artwork_ready)
        except (TypeError, RuntimeError):
            pass

class PlaylistSongRow(CardWidget):
    play_clicked = Signal(object)
    download_clicked = Signal(object)
    remove_clicked = Signal(object)

    def __init__(self, song, index: int, artwork_downloader=None, parent=None):
        super().__init__(parent)
        self.song = song
        self.index = index
        self.setFixedHeight(56)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(10, 6, 12, 6)
        layout.setSpacing(12)
        c = palette()
        idx_lbl = CaptionLabel(str(index + 1), self)
        idx_lbl.setFixedWidth(18)
        idx_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        idx_lbl.setStyleSheet(f'color: {c['muted']}; font-size: 12px;')
        layout.addWidget(idx_lbl)
        self._art_label = _PlaylistCoverLabel(40, artwork_downloader, self)
        self._art_label.set_cover(song.cover_url)
        layout.addWidget(self._art_label)
        text_col = QVBoxLayout()
        text_col.setSpacing(1)
        title_lbl = StrongBodyLabel(song.song_name or tr('download.unknown'), self)
        title_lbl.setStyleSheet('font-size: 13.5px;')
        sub_lbl = CaptionLabel(song.singers or tr('music.unknown_artist'), self)
        sub_lbl.setStyleSheet(f'color: {c['muted']}; font-size: 11px;')
        text_col.addWidget(title_lbl)
        text_col.addWidget(sub_lbl)
        layout.addLayout(text_col, 1)
        quality_lbl = CaptionLabel(song.quality_label, self)
        quality_lbl.setStyleSheet(f'color: {c['muted']}; background-color: {c['surface_tint_strong']}; border-radius: 7px; padding: 1px 6px; font-size: 9.5px; font-weight: 600;')
        quality_lbl.setVisible(bool(song.quality_label))
        layout.addWidget(quality_lbl)
        source_lbl = CaptionLabel(_format_source(song.source), self)
        source_lbl.setStyleSheet(f'color: {c['muted']}; border: 1px solid {c['surface_border']}; border-radius: 7px; padding: 1px 6px; font-size: 9.5px; font-weight: 600;')
        source_lbl.setVisible(bool(song.source))
        layout.addWidget(source_lbl)
        dur_lbl = CaptionLabel(song.duration or '', self)
        dur_lbl.setFixedWidth(38)
        dur_lbl.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        dur_lbl.setStyleSheet(f'color: {c['muted']};')
        layout.addWidget(dur_lbl)
        btn_play = ToolButton(FluentIcon.PLAY, self)
        btn_download = ToolButton(FluentIcon.DOWNLOAD, self)
        btn_remove = TransparentToolButton(FluentIcon.DELETE, self)
        for b in (btn_play, btn_download, btn_remove):
            b.setFixedSize(28, 28)
            b.setIconSize(QSize(13, 13))
            layout.addWidget(b)
        btn_play.clicked.connect(lambda: self.play_clicked.emit(self.song))
        btn_download.clicked.connect(lambda: self.download_clicked.emit(self.song))
        btn_remove.clicked.connect(lambda: self.remove_clicked.emit(self.song))

    def detach(self) -> None:
        self._art_label.detach()

class PlaylistsPanel(QWidget):

    def __init__(self, store: PlaylistStore, artwork_downloader=None, parent=None, play_requested=None, download_requested=None, download_all_requested=None, play_song_requested=None):
        super().__init__(parent)
        self._store = store
        self._artwork_downloader = artwork_downloader
        self._play_requested = play_requested
        self._download_requested = download_requested
        self._download_all_requested = download_all_requested
        self._play_song_requested = play_song_requested
        self._current_playlist_id: str | None = None
        self._song_rows: list[PlaylistSongRow] = []
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(10)
        self._detail_stack = QStackedWidget(self)
        list_page = QWidget(self._detail_stack)
        list_layout = QVBoxLayout(list_page)
        list_layout.setContentsMargins(0, 0, 0, 0)
        list_layout.setSpacing(10)
        header_row = QHBoxLayout()
        header_row.setSpacing(8)
        header_icon = QLabel(list_page)
        header_icon.setFixedSize(20, 20)
        header_icon.setPixmap(FluentIcon.MENU.icon(color=QColor(palette()['muted'])).pixmap(18, 18))
        header_row.addWidget(header_icon)
        header_lbl = StrongBodyLabel(tr('music.my_playlists'), list_page)
        header_lbl.setStyleSheet('font-size: 16px;')
        self._header_lbl = header_lbl
        header_row.addWidget(header_lbl)
        header_row.addStretch(1)
        btn_new = PrimaryPushButton(FluentIcon.ADD, tr('music.new_playlist'), list_page)
        self._btn_new = btn_new
        btn_new.clicked.connect(self._on_new_playlist)
        header_row.addWidget(btn_new)
        list_layout.addLayout(header_row)
        self._playlists_scroll = ScrollArea(list_page)
        self._playlists_scroll.setWidgetResizable(True)
        self._playlists_scroll.setStyleSheet('background: transparent; border: none;')
        self._playlists_container = QWidget()
        self._playlists_container.setStyleSheet('background: transparent;')
        self._playlists_container_layout = QVBoxLayout(self._playlists_container)
        self._playlists_container_layout.setContentsMargins(0, 0, 0, 0)
        self._playlists_container_layout.setSpacing(8)
        self._playlists_container_layout.addStretch(1)
        self._playlists_scroll.setWidget(self._playlists_container)
        list_layout.addWidget(self._playlists_scroll, 1)
        self._empty_playlists_lbl = CaptionLabel(tr('music.no_playlists_hint'), list_page)
        self._empty_playlists_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._empty_playlists_lbl.setWordWrap(True)
        self._empty_playlists_lbl.setStyleSheet(f'color: {palette()['muted']}; padding: 24px 12px;')
        list_layout.addWidget(self._empty_playlists_lbl)
        self._detail_stack.addWidget(list_page)
        detail_page = QWidget(self._detail_stack)
        detail_layout = QVBoxLayout(detail_page)
        detail_layout.setContentsMargins(0, 0, 0, 0)
        detail_layout.setSpacing(10)
        detail_header_row = QHBoxLayout()
        btn_back = TransparentPushButton(FluentIcon.RETURN, tr('music.back'), detail_page)
        self._btn_back = btn_back
        btn_back.clicked.connect(self._on_back_to_list)
        detail_header_row.addWidget(btn_back)
        self._detail_title_lbl = StrongBodyLabel('', detail_page)
        self._detail_title_lbl.setStyleSheet('font-size: 16px;')
        detail_header_row.addWidget(self._detail_title_lbl)
        detail_header_row.addStretch(1)
        btn_play_all = PrimaryPushButton(FluentIcon.PLAY, tr('music.play_all'), detail_page)
        self._btn_play_all = btn_play_all
        btn_play_all.clicked.connect(self._on_play_all)
        detail_header_row.addWidget(btn_play_all)
        btn_download_all = PushButton(FluentIcon.DOWNLOAD, tr('music.download_all'), detail_page)
        self._btn_download_all_playlist = btn_download_all
        btn_download_all.clicked.connect(self._on_download_all)
        detail_header_row.addWidget(btn_download_all)
        btn_rename = ToolButton(FluentIcon.EDIT, detail_page)
        btn_rename.setFixedSize(32, 32)
        btn_rename.clicked.connect(self._on_rename_playlist)
        detail_header_row.addWidget(btn_rename)
        btn_delete = ToolButton(FluentIcon.DELETE, detail_page)
        btn_delete.setFixedSize(32, 32)
        btn_delete.clicked.connect(self._on_delete_playlist)
        detail_header_row.addWidget(btn_delete)
        detail_layout.addLayout(detail_header_row)
        self._songs_scroll = ScrollArea(detail_page)
        self._songs_scroll.setWidgetResizable(True)
        self._songs_scroll.setStyleSheet('background: transparent; border: none;')
        self._songs_container = QWidget()
        self._songs_container.setStyleSheet('background: transparent;')
        self._songs_container_layout = QVBoxLayout(self._songs_container)
        self._songs_container_layout.setContentsMargins(0, 0, 0, 0)
        self._songs_container_layout.setSpacing(6)
        self._songs_container_layout.addStretch(1)
        self._songs_scroll.setWidget(self._songs_container)
        detail_layout.addWidget(self._songs_scroll, 1)
        self._empty_songs_lbl = CaptionLabel(tr('music.empty_playlist_hint'), detail_page)
        self._empty_songs_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._empty_songs_lbl.setWordWrap(True)
        self._empty_songs_lbl.setStyleSheet(f'color: {palette()['muted']}; padding: 24px 12px;')
        detail_layout.addWidget(self._empty_songs_lbl)
        self._detail_stack.addWidget(detail_page)
        root.addWidget(self._detail_stack, 1)
        self._store.playlists_changed.connect(self._on_store_changed)
        register_locale_refresh(self, self._apply_locale)
        self.refresh()

    def _apply_locale(self, *_args) -> None:
        self._header_lbl.setText(tr('music.my_playlists'))
        self._btn_new.setText(tr('music.new_playlist'))
        self._empty_playlists_lbl.setText(tr('music.no_playlists_hint'))
        self._btn_back.setText(tr('music.back'))
        self._btn_play_all.setText(tr('music.play_all'))
        self._btn_download_all_playlist.setText(tr('music.download_all'))
        self._empty_songs_lbl.setText(tr('music.empty_playlist_hint'))

    def refresh(self) -> None:
        self._rebuild_playlists_list()
        if self._current_playlist_id is not None:
            self._rebuild_song_rows()

    def _on_store_changed(self) -> None:
        self._rebuild_playlists_list()
        if self._current_playlist_id is not None:
            pl = self._store.get(self._current_playlist_id)
            if pl is None:
                self._on_back_to_list()
            else:
                self._rebuild_song_rows()

    def _clear_layout(self, layout) -> None:
        while layout.count() > 1:
            item = layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                detach = getattr(widget, 'detach', None)
                if callable(detach):
                    detach()
                widget.setParent(None)
                widget.deleteLater()

    def _rebuild_playlists_list(self) -> None:
        self._clear_layout(self._playlists_container_layout)
        playlists = self._store.all_playlists()
        self._empty_playlists_lbl.setVisible(not playlists)
        for pl in playlists:
            row = self._make_playlist_row(pl)
            self._playlists_container_layout.insertWidget(self._playlists_container_layout.count() - 1, row)

    def _make_playlist_row(self, pl: Playlist) -> QWidget:
        row = CardWidget(self._playlists_container)
        row.setFixedHeight(64)
        row.setCursor(Qt.CursorShape.PointingHandCursor)
        layout = QHBoxLayout(row)
        layout.setContentsMargins(10, 8, 16, 8)
        layout.setSpacing(12)
        c = palette()
        cover = _PlaylistCoverLabel(44, self._artwork_downloader, row)
        first_cover_url = pl.songs[0].cover_url if pl.songs else None
        cover.set_cover(first_cover_url)
        layout.addWidget(cover)
        text_col = QVBoxLayout()
        text_col.setSpacing(2)
        name_lbl = StrongBodyLabel(pl.name, row)
        name_lbl.setStyleSheet('font-size: 14px;')
        count_lbl = CaptionLabel(f'{len(pl.songs)} song{('s' if len(pl.songs) != 1 else '')}', row)
        count_lbl.setStyleSheet(f'color: {c['muted']}; font-size: 11.5px;')
        text_col.addWidget(name_lbl)
        text_col.addWidget(count_lbl)
        layout.addLayout(text_col, 1)
        chevron = QLabel(row)
        chevron.setFixedSize(16, 16)
        chevron.setPixmap(FluentIcon.RIGHT_ARROW.icon(color=QColor(c['muted'])).pixmap(12, 12))
        layout.addWidget(chevron)
        row.detach = cover.detach

        def _open(_event=None, playlist_id=pl.id):
            self._open_playlist(playlist_id)
        row.mousePressEvent = lambda event, f=_open: f()
        return row

    def _open_playlist(self, playlist_id: str) -> None:
        self._current_playlist_id = playlist_id
        self._detail_stack.setCurrentIndex(1)
        self._rebuild_song_rows()

    def _on_back_to_list(self) -> None:
        self._current_playlist_id = None
        self._detail_stack.setCurrentIndex(0)
        self._rebuild_playlists_list()

    def _rebuild_song_rows(self) -> None:
        self._clear_layout(self._songs_container_layout)
        self._song_rows.clear()
        pl = self._store.get(self._current_playlist_id) if self._current_playlist_id else None
        if pl is None:
            return
        self._detail_title_lbl.setText(pl.name)
        self._empty_songs_lbl.setVisible(not pl.songs)
        for idx, song in enumerate(pl.songs):
            row = PlaylistSongRow(song, idx, self._artwork_downloader, self._songs_container)
            row.play_clicked.connect(self._on_row_play)
            row.download_clicked.connect(self._on_row_download)
            row.remove_clicked.connect(self._on_row_remove)
            self._songs_container_layout.insertWidget(self._songs_container_layout.count() - 1, row)
            self._song_rows.append(row)

    def _on_row_play(self, song) -> None:
        pl = self._store.get(self._current_playlist_id) if self._current_playlist_id else None
        if pl is None:
            return
        try:
            start_index = next((i for i, s in enumerate(pl.songs) if s.key == song.key))
        except StopIteration:
            start_index = 0
        if callable(self._play_requested):
            self._play_requested(pl.songs, start_index)

    def _on_row_download(self, song) -> None:
        if callable(self._download_requested):
            self._download_requested(song)

    def _on_row_remove(self, song) -> None:
        if self._current_playlist_id is None:
            return
        self._store.remove_song(self._current_playlist_id, song.key)

    def _on_play_all(self) -> None:
        pl = self._store.get(self._current_playlist_id) if self._current_playlist_id else None
        if pl is None or not pl.songs:
            return
        if callable(self._play_requested):
            self._play_requested(pl.songs, 0)

    def _on_download_all(self) -> None:
        pl = self._store.get(self._current_playlist_id) if self._current_playlist_id else None
        if pl is None or not pl.songs:
            return
        if callable(self._download_all_requested):
            self._download_all_requested(pl.songs)
        elif callable(self._download_requested):
            for song in pl.songs:
                self._download_requested(song)

    def _on_new_playlist(self) -> None:
        dialog = MessageBoxBase(self.window())
        dialog.titleLabel = StrongBodyLabel(tr('music.new_playlist'), dialog)
        dialog.viewLayout.addWidget(dialog.titleLabel)
        name_input = LineEdit(dialog)
        name_input.setPlaceholderText(tr('music.playlist_name_placeholder'))
        dialog.viewLayout.addWidget(name_input)
        dialog.yesButton.setText(tr('music.create'))
        dialog.cancelButton.setText(tr('music.cancel'))
        if dialog.exec():
            name = name_input.text().strip()
            if name:
                self._store.create(name)

    def _on_rename_playlist(self) -> None:
        pl = self._store.get(self._current_playlist_id) if self._current_playlist_id else None
        if pl is None:
            return
        dialog = MessageBoxBase(self.window())
        dialog.titleLabel = StrongBodyLabel(tr('music.rename_playlist'), dialog)
        dialog.viewLayout.addWidget(dialog.titleLabel)
        name_input = LineEdit(dialog)
        name_input.setText(pl.name)
        dialog.viewLayout.addWidget(name_input)
        dialog.yesButton.setText(tr('music.save'))
        dialog.cancelButton.setText(tr('music.cancel'))
        if dialog.exec():
            new_name = name_input.text().strip()
            if new_name:
                self._store.rename(pl.id, new_name)
                self._detail_title_lbl.setText(new_name)

    def _on_delete_playlist(self) -> None:
        pl = self._store.get(self._current_playlist_id) if self._current_playlist_id else None
        if pl is None:
            return
        dialog = MessageBoxBase(self.window())
        dialog.titleLabel = StrongBodyLabel(f'Delete "{pl.name}"?', dialog)
        dialog.viewLayout.addWidget(dialog.titleLabel)
        body = CaptionLabel(tr('music.delete_cannot_undo'), dialog)
        dialog.viewLayout.addWidget(body)
        dialog.yesButton.setText(tr('music.delete'))
        dialog.cancelButton.setText(tr('music.cancel'))
        if dialog.exec():
            self._store.delete(pl.id)
            self._on_back_to_list()

class MusicPage(QWidget):

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setStyleSheet('background: transparent;')
        self._player = MusicPreviewPlayer(self)
        self._artwork_downloader = artwork
        self._download_manager = None
        self._download_speed_state: dict[str, tuple[float, int]] = {}
        self._cards: list[SongCard] = []
        self._all_songs = []
        self._song_keys: set[str] = set()
        self._searching = False
        self._search_token = None
        self._search_query = ''
        self._offset = 0
        self._page_size = 1
        self._current_cols = 0
        self._current_rows = 0
        self._page_rendered = False
        self._pending_artwork: set[str] = set()
        root = QVBoxLayout(self)
        root.setContentsMargins(24, 12, 24, 16)
        root.setSpacing(10)
        search_row = QHBoxLayout()
        search_row.setSpacing(8)
        self._search_bar = SearchLineEdit(self)
        self._search_bar.setPlaceholderText(tr('music.search_placeholder'))
        self._search_bar.setMinimumWidth(160)
        self._search_bar.setMaximumWidth(420)
        self._search_bar.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self._search_bar.returnPressed.connect(self._on_search_triggered)
        self._search_bar.searchSignal.connect(lambda _q: self._on_search_triggered())
        self._btn_filters = PrimaryPushButton(FluentIcon.FILTER, tr('music.filter'), self)
        self._btn_filters.setFixedHeight(34)
        self._btn_filters.setIconSize(QSize(14, 14))
        self._btn_filters.clicked.connect(self._on_filters_clicked)
        self._btn_playlists = PrimaryPushButton(FluentIcon.MENU, tr('music.playlists'), self)
        self._btn_playlists.setFixedHeight(34)
        self._btn_playlists.setIconSize(QSize(14, 14))
        self._btn_playlists.setCheckable(True)
        self._btn_playlists.clicked.connect(self._on_toggle_playlists_view)
        self._btn_cancel_search = ToolButton(FluentIcon.CLOSE, self)
        self._btn_cancel_search.setFixedHeight(34)
        self._btn_cancel_search.setVisible(False)
        self._btn_cancel_search.clicked.connect(self._on_cancel_search_clicked)
        search_row.addWidget(self._search_bar)
        search_row.addWidget(self._btn_cancel_search)
        search_row.addWidget(self._btn_filters)
        search_row.addStretch(1)
        search_row.addWidget(self._btn_playlists)
        root.addLayout(search_row)
        self._loading_bar = IndeterminateProgressBar(self, start=False)
        self._loading_bar.setFixedHeight(3)
        root.addWidget(self._loading_bar)
        self._view_stack = QStackedWidget(self)
        search_view = QWidget(self._view_stack)
        search_view_layout = QVBoxLayout(search_view)
        search_view_layout.setContentsMargins(0, 0, 0, 0)
        search_view_layout.setSpacing(10)
        self._results_area = QWidget(search_view)
        self._grid_layout = QGridLayout(self._results_area)
        self._grid_layout.setContentsMargins(0, 0, 0, 0)
        self._grid_layout.setSpacing(GRID_SPACING)
        search_view_layout.addWidget(self._results_area, 1)
        self._empty_lbl = CaptionLabel(tr('music.search_hint'), search_view)
        self._empty_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._empty_lbl.setStyleSheet(f'color: {palette()['muted']};')
        search_view_layout.addWidget(self._empty_lbl)
        qconfig.themeChanged.connect(lambda *_: self._empty_lbl.setStyleSheet(f'color: {palette()['muted']};'))
        pager_row = QHBoxLayout()
        pager_row.addStretch(1)
        self._btn_prev_page = ToolButton(FluentIcon.LEFT_ARROW, search_view)
        self._btn_prev_page.clicked.connect(self._on_prev_page)
        self._page_lbl = CaptionLabel('', search_view)
        self._btn_next_page = ToolButton(FluentIcon.RIGHT_ARROW, search_view)
        self._btn_next_page.clicked.connect(self._on_next_page)
        pager_row.addWidget(self._btn_prev_page)
        pager_row.addWidget(self._page_lbl)
        pager_row.addWidget(self._btn_next_page)
        pager_row.addStretch(1)
        search_view_layout.addLayout(pager_row)
        self._set_pager_visible(False)
        self._playlist_store = PlaylistStore(self)
        self._playlists_panel = PlaylistsPanel(self._playlist_store, self._artwork_downloader, self._view_stack, play_requested=self._on_play_playlist_requested, download_requested=self._on_download_requested, download_all_requested=self._on_download_all_requested, play_song_requested=self._on_play_requested)
        self._view_stack.addWidget(search_view)
        self._view_stack.addWidget(self._playlists_panel)
        root.addWidget(self._view_stack, 1)
        self._player_bar = MiniPlayerBar(self._player, self)
        self._player_bar.set_artwork_downloader(self._artwork_downloader)
        root.addWidget(self._player_bar)
        self._lyrics_overlay = LyricsOverlay(self._player, self._artwork_downloader, self._player_bar, self)
        self._player_bar.lyrics_requested.connect(self._lyrics_overlay.show_overlay)
        self._resize_timer = QTimer(self)
        self._resize_timer.setSingleShot(True)
        self._resize_timer.setInterval(80)
        self._resize_timer.timeout.connect(self._relayout_grid)
        register_locale_refresh(self, self._apply_locale)

    def _apply_locale(self, *_args) -> None:
        self._search_bar.setPlaceholderText(tr('music.search_placeholder'))
        self._btn_filters.setText(tr('music.filter'))
        self._btn_playlists.setText(tr('music.playlists'))
        if not self._all_songs:
            self._empty_lbl.setText(tr('music.search_hint'))
        self._update_pager_label()

    def set_download_manager(self, manager) -> None:
        self._download_manager = manager

    def shutdown(self) -> None:
        self._resize_timer.stop()
        if self._search_token is not None:
            cancel(self._search_token)
            self._search_token = None
        try:
            self._lyrics_overlay.hide_overlay()
        except RuntimeError:
            pass
        self._unload_current_page()
        try:
            self._player.stop()
        except RuntimeError:
            pass
        try:
            self._artwork_downloader.shutdown()
        except RuntimeError:
            pass
        gc.collect()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._lyrics_overlay.sync_geometry()
        self._resize_timer.start()

    def _apply_grid_stretch(self, cols: int, rows: int) -> None:
        if getattr(self, '_stretched_cols', 0) == cols and getattr(self, '_stretched_rows', 0) == rows:
            return
        prev_cols = getattr(self, '_stretched_cols', 0)
        prev_rows = getattr(self, '_stretched_rows', 0)
        for c in range(cols, prev_cols):
            self._grid_layout.setColumnStretch(c, 0)
        for c in range(cols):
            self._grid_layout.setColumnStretch(c, 1)
        for r in range(rows, prev_rows):
            self._grid_layout.setRowStretch(r, 0)
        for r in range(rows):
            self._grid_layout.setRowStretch(r, 1)
        self._stretched_cols = cols
        self._stretched_rows = rows

    def _grid_capacity(self) -> tuple[int, int]:
        area = self._results_area.size()
        cols = max(1, (area.width() + GRID_SPACING) // (CARD_WIDTH + GRID_SPACING))
        rows = max(1, (area.height() + GRID_SPACING) // (CARD_HEIGHT + GRID_SPACING))
        return (int(cols), int(rows))

    def _relayout_grid(self) -> None:
        cols, rows = self._grid_capacity()
        new_page_size = max(1, cols * rows)
        if cols == self._current_cols and rows == self._current_rows:
            return
        self._current_cols = cols
        self._current_rows = rows
        if new_page_size != self._page_size:
            self._page_size = new_page_size
            self._offset = self._offset // self._page_size * self._page_size
            self._render_current_page()
        else:
            for card in self._cards:
                self._grid_layout.removeWidget(card)
            for idx, card in enumerate(self._cards):
                row, col = divmod(idx, cols)
                self._grid_layout.addWidget(card, row, col, Qt.AlignmentFlag.AlignCenter)
            self._apply_grid_stretch(cols, rows)

    def _on_search_triggered(self) -> None:
        query = self._search_bar.text().strip()
        if not query:
            return
        if self._search_token is not None:
            cancel(self._search_token)
            self._search_token = None
        self._reset_results()
        self._search_query = query
        self._searching = True
        self._loading_bar.start()
        self._btn_cancel_search.setVisible(True)
        self._search_token = submit(music_service.search_streaming, args=(query,), kwargs={'on_source_done': wrap_callback(self._on_search_batch), 'sources': music_settings.preferred_sources, 'use_cache': True}, on_done=self._on_search_done, on_error=self._on_search_error)

    def _on_cancel_search_clicked(self) -> None:
        if self._search_token is not None:
            cancel(self._search_token)
            self._search_token = None
        self._searching = False
        self._loading_bar.stop()
        self._btn_cancel_search.setVisible(False)
        self._update_pager_label()

    def _on_search_batch(self, songs) -> None:
        new_songs = [s for s in songs if s.key not in self._song_keys]
        if not new_songs:
            return
        for s in new_songs:
            self._song_keys.add(s.key)
        self._all_songs.extend(new_songs)
        self._empty_lbl.setVisible(False)
        self._set_pager_visible(True)
        page_songs = self._current_page_songs()
        if len(self._cards) < len(page_songs):
            self._render_current_page()
        else:
            self._update_pager_label()

    def _on_search_done(self, songs) -> None:
        self._search_token = None
        self._searching = False
        self._loading_bar.stop()
        self._btn_cancel_search.setVisible(False)
        if not self._all_songs:
            self._empty_lbl.setText(tr('music.no_results_try_different'))
            self._empty_lbl.setVisible(True)
            self._set_pager_visible(False)
        else:
            self._update_pager_label()

    def _on_search_error(self, error: str) -> None:
        self._search_token = None
        self._searching = False
        self._loading_bar.stop()
        self._btn_cancel_search.setVisible(False)
        InfoBar.error(title=tr('music.search_failed_title'), content=str(error)[:120], orient=Qt.Orientation.Horizontal, isClosable=True, position=InfoBarPosition.TOP_RIGHT, duration=4000, parent=self.window())

    def _reset_results(self) -> None:
        self._unload_current_page()
        QApplication.sendPostedEvents(None, 0)
        QApplication.processEvents()
        self._all_songs = []
        self._song_keys.clear()
        self._pending_artwork.clear()
        self._offset = 0
        self._page_rendered = False
        self._update_pager_label()

    def _unload_current_page(self) -> None:
        for card in self._cards:
            self._grid_layout.removeWidget(card)
            card.detach()
            card.play_requested = None
            card.download_requested = None
            card.artwork_settled = None
            card.save_playlist_requested = None
            card.setParent(None)
            card.deleteLater()
        self._cards.clear()
        self._trim_offscreen_song_payloads()

    def _trim_offscreen_song_payloads(self) -> None:
        keep_keys = set(self._player.queue_keys)
        page_start = self._offset
        page_end = self._offset + self._page_size
        for idx, song in enumerate(self._all_songs):
            if page_start <= idx < page_end:
                continue
            if song.key in keep_keys:
                continue
            if song.raw:
                song.raw = {}

    def _current_page_songs(self):
        return self._all_songs[self._offset:self._offset + self._page_size]

    def _render_current_page(self) -> None:
        cols, rows = self._grid_capacity()
        self._current_cols = cols
        self._current_rows = rows
        self._page_size = max(1, cols * rows)
        self._unload_current_page()
        page_songs = self._current_page_songs()
        self._pending_artwork.clear()
        if not page_songs:
            self._page_rendered = False
            self._update_pager_label()
            return
        self._page_rendered = False
        self._update_pager_label()
        for song in page_songs:
            if song.cover_url:
                self._pending_artwork.add(song.key)
            card = SongCard(song, self._artwork_downloader, self._results_area, play_requested=self._on_play_requested, download_requested=self._on_download_requested, artwork_settled=self._on_card_artwork_settled, save_playlist_requested=self._on_save_playlist_requested)
            self._cards.append(card)
        for idx, card in enumerate(self._cards):
            row, col = divmod(idx, cols)
            self._grid_layout.addWidget(card, row, col, Qt.AlignmentFlag.AlignCenter)
        self._apply_grid_stretch(cols, rows)
        if not self._pending_artwork:
            self._page_rendered = True
        self._update_pager_label()

    def _on_card_artwork_settled(self, song_key: str) -> None:
        self._pending_artwork.discard(song_key)
        if not self._pending_artwork and (not self._page_rendered):
            self._page_rendered = True
            self._update_pager_label()

    def _on_play_requested(self, song) -> None:
        url = song.download_url if isinstance(song.download_url, str) else None
        if not url:
            InfoBar.warning(title=tr('music.no_preview_title'), content=tr('music.no_preview_content'), orient=Qt.Orientation.Horizontal, isClosable=True, position=InfoBarPosition.TOP_RIGHT, duration=3000, parent=self.window())
            return
        page_songs = self._current_page_songs()
        urls = {s.key: s.download_url if isinstance(s.download_url, str) else '' for s in page_songs}
        urls = {k: v for k, v in urls.items() if v}
        self._player.play_song(song, url, queue=page_songs, urls=urls)

    def _on_download_requested(self, song) -> None:
        url = song.download_url if isinstance(song.download_url, str) else ''
        if not url:
            InfoBar.warning(title=tr('music.no_download_title'), content=tr('music.no_download_content'), orient=Qt.Orientation.Horizontal, isClosable=True, position=InfoBarPosition.TOP_RIGHT, duration=3000, parent=self.window())
            return
        dest_dir = getattr(app_settings, 'download_dir_music', None) or os.path.join(app_settings.download_dir, 'music')
        dl_item_id = None
        if self._download_manager is not None:
            dl_item_id = self._download_manager.add_external(game_name=song.song_name or tr('download.unknown'), console=song.singers or '', source='Music', category='music')
            self._download_speed_state[dl_item_id] = (time.monotonic(), 0)
        task = _SongDownloadTask(self._candidates_for(song), dest_dir)
        task.signals.progress.connect(lambda downloaded, total, iid=dl_item_id: self._on_song_download_progress(iid, downloaded, total))
        task.signals.finished.connect(lambda ok, path, error, iid=dl_item_id: self._on_song_download_finished(iid, ok, path, error))
        if dl_item_id is not None and self._download_manager is not None:
            self._download_manager.register_external_cancel(dl_item_id, task.cancel)
        QThreadPool.globalInstance().start(task)

    def _candidates_for(self, song):
        key = _download_match_key(song)
        others = [s for s in self._all_songs if s is not song and _download_match_key(s) == key and isinstance(s.download_url, str) and s.download_url]
        return [song] + others

    def _on_download_all_requested(self, songs: list) -> None:
        downloadable = [s for s in songs if isinstance(s.download_url, str) and s.download_url]
        if not downloadable:
            InfoBar.warning(title=tr('music.no_downloads_title'), content=tr('music.no_downloads_content'), orient=Qt.Orientation.Horizontal, isClosable=True, position=InfoBarPosition.TOP_RIGHT, duration=3000, parent=self.window())
            return
        for song in downloadable:
            self._on_download_requested(song)

    def _on_song_download_progress(self, dl_item_id, downloaded: int, total: int) -> None:
        if dl_item_id is None or self._download_manager is None:
            return
        speed_kbps = 0.0
        now = time.monotonic()
        prev = self._download_speed_state.get(dl_item_id)
        if prev is not None:
            prev_time, prev_downloaded = prev
            dt = now - prev_time
            if dt >= 0.2:
                speed_kbps = max(0.0, (downloaded - prev_downloaded) / dt / 1024)
                self._download_speed_state[dl_item_id] = (now, downloaded)
            else:
                speed_kbps = None
        if speed_kbps is None:
            self._download_manager.update_external(dl_item_id, downloaded_bytes=downloaded, total_bytes=total)
        else:
            self._download_manager.update_external(dl_item_id, downloaded_bytes=downloaded, total_bytes=total, speed_down_kbps=speed_kbps)

    def _on_song_download_finished(self, dl_item_id, ok: bool, path: str, error: str) -> None:
        self._download_speed_state.pop(dl_item_id, None)
        if dl_item_id is None or self._download_manager is None:
            if not ok and error and (error != 'Cancelled'):
                InfoBar.error(title=tr('music.download_error_title'), content=error[:120], orient=Qt.Orientation.Horizontal, isClosable=True, position=InfoBarPosition.TOP_RIGHT, duration=4000, parent=self.window())
            return
        if ok:
            self._download_manager.complete_external(dl_item_id, path)
        elif error == 'Cancelled':
            pass
        else:
            self._download_manager.fail_external(dl_item_id, error or tr('music.download_failed_generic'))
            InfoBar.error(title=tr('music.download_error_title'), content=(error or tr('music.status_failed'))[:120], orient=Qt.Orientation.Horizontal, isClosable=True, position=InfoBarPosition.TOP_RIGHT, duration=4000, parent=self.window())

    def _update_pager_label(self) -> None:
        total = max(1, (len(self._all_songs) + self._page_size - 1) // self._page_size)
        current = self._offset // self._page_size + 1
        self._page_lbl.setText(tr('music.page_of', current=current, total=total))
        can_navigate = self._page_rendered
        self._btn_prev_page.setEnabled(can_navigate and self._offset > 0)
        has_next = self._offset + self._page_size < len(self._all_songs)
        self._btn_next_page.setEnabled(can_navigate and (has_next or self._searching))

    def _set_pager_visible(self, visible: bool) -> None:
        self._btn_prev_page.setVisible(visible)
        self._page_lbl.setVisible(visible)
        self._btn_next_page.setVisible(visible)

    def _on_prev_page(self) -> None:
        if self._offset <= 0:
            return
        self._offset = max(0, self._offset - self._page_size)
        self._page_rendered = False
        self._render_current_page()

    def _on_next_page(self) -> None:
        next_offset = self._offset + self._page_size
        if next_offset >= len(self._all_songs) and (not self._searching):
            return
        self._offset = next_offset
        self._page_rendered = False
        if len(self._all_songs) > self._offset:
            self._render_current_page()
        else:
            self._unload_current_page()
            self._update_pager_label()

    def _on_filters_clicked(self) -> None:
        all_sources = music_service.available_sources()
        dialog = FilterDialog(all_sources, music_settings.preferred_sources, music_settings.quality_filters, self.window())
        if not dialog.exec():
            return
        selected_sources = dialog.selected_sources()
        selected_quality = dialog.selected_quality()
        if not selected_sources or not selected_quality:
            InfoBar.warning(title=tr('music.filters_not_applied_title'), content=tr('music.filters_not_applied_content'), orient=Qt.Orientation.Horizontal, isClosable=True, position=InfoBarPosition.TOP_RIGHT, duration=3000, parent=self.window())
            return
        apply_music_settings(preferred_sources=selected_sources, quality_filters=selected_quality)
        if self._search_bar.text().strip():
            self._on_search_triggered()

    def _on_toggle_playlists_view(self) -> None:
        showing_playlists = self._view_stack.currentIndex() == 1
        if showing_playlists:
            self._view_stack.setCurrentIndex(0)
            self._btn_playlists.setChecked(False)
        else:
            self._view_stack.setCurrentIndex(1)
            self._btn_playlists.setChecked(True)
            self._playlists_panel.refresh()

    def _on_save_playlist_requested(self, song) -> None:
        dialog = SaveToPlaylistDialog(self._playlist_store, song, self.window())
        if not dialog.exec():
            return
        playlist_id = dialog.selected_playlist_id()
        if not playlist_id:
            InfoBar.warning(title=tr('music.no_playlist_selected_title'), content=tr('music.no_playlist_selected_content'), orient=Qt.Orientation.Horizontal, isClosable=True, position=InfoBarPosition.TOP_RIGHT, duration=3000, parent=self.window())
            return
        added = self._playlist_store.add_song(playlist_id, song)
        pl = self._playlist_store.get(playlist_id)
        pl_name = pl.name if pl is not None else 'playlist'
        if added:
            InfoBar.success(title=tr('music.saved_title'), content=tr('music.added_to_playlist', name=pl_name), orient=Qt.Orientation.Horizontal, isClosable=True, position=InfoBarPosition.TOP_RIGHT, duration=2500, parent=self.window())
        else:
            InfoBar.info(title=tr('music.already_saved_title'), content=tr('music.already_in_playlist', name=pl_name), orient=Qt.Orientation.Horizontal, isClosable=True, position=InfoBarPosition.TOP_RIGHT, duration=2500, parent=self.window())

    def _on_play_playlist_requested(self, songs: list, start_index: int) -> None:
        if not songs:
            return
        playable = [s for s in songs if isinstance(s.download_url, str) and s.download_url]
        if not playable:
            InfoBar.warning(title=tr('music.no_preview_title'), content=tr('music.no_preview_in_playlist_content'), orient=Qt.Orientation.Horizontal, isClosable=True, position=InfoBarPosition.TOP_RIGHT, duration=3000, parent=self.window())
            return
        start_song = songs[start_index] if 0 <= start_index < len(songs) else songs[0]
        if not (isinstance(start_song.download_url, str) and start_song.download_url):
            start_song = playable[0]
        urls = {s.key: s.download_url for s in playable}
        self._player.play_song(start_song, start_song.download_url, queue=playable, urls=urls)