from __future__ import annotations

import ctypes
import logging
import os
import re
import sys
import time
from collections import OrderedDict
from contextlib import suppress
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from PySide6.QtCore import (
    QAbstractListModel,
    QAbstractNativeEventFilter,
    QEasingCurve,
    QEvent,
    QModelIndex,
    QObject,
    QPoint,
    QPropertyAnimation,
    QRect,
    QRectF,
    QSettings,
    QSize,
    Qt,
    QTimer,
    QVariantAnimation,
    Signal,
)
from PySide6.QtGui import (
    QColor,
    QCursor,
    QFont,
    QFontMetrics,
    QGuiApplication,
    QIcon,
    QImageReader,
    QKeySequence,
    QPainter,
    QPainterPath,
    QPalette,
    QPen,
    QPixmap,
    QShortcut,
)
from PySide6.QtWidgets import (
    QApplication,
    QFrame,
    QLabel,
    QLineEdit,
    QListView,
    QStyle,
    QStyledItemDelegate,
    QStyleOptionViewItem,
    QWidget,
)

from qfluentwidgets import Action, FluentIcon, RoundMenu

from src.core.artwork import images
from src.core.config import settings
from src.core.translations import register_locale_refresh, tr
from src.core.theme import palette, register_theme_refresh, themed_icon, to_color
from src.core import worker
from src.core.worker import Priority

logger = logging.getLogger(__name__)


def _sfx(name: str) -> None:
    try:
        from .settings_page import get_sound_manager
        get_sound_manager().play(name)
    except Exception:
        logger.debug('SailSeek sfx %r failed', name, exc_info=True)


WM_HOTKEY = 0x0312
MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_SHIFT = 0x0004
MOD_WIN = 0x0008
MOD_NOREPEAT = 0x4000
_HOTKEY_ID = 0x5043
_ONLINE_TTL = 900.0
_ONLINE_CACHE_MAX = 96
_DEBOUNCE_MS = 400
_DELAY_MIN_MS = 250
_DELAY_MAX_MS = 2000
_MIN_NET_GAP = 0.8
_SEARCH_DEADLINE_MS = 12_000
_RICKROLL_URL = 'https://www.youtube.com/watch?v=dQw4w9WgXcQ'

_RESULT_ROLE = Qt.ItemDataRole.UserRole + 1

_CATEGORY_ORDER = (
    'downloads', 'games', 'roms', 'movies', 'tv', 'anime', 'manga', 'books',
)
_ONLINE_CATEGORIES = ('movies', 'tv', 'anime', 'manga', 'books')
_GAME_SOURCES = ('fitgirl', 'gog')

_CATEGORY_ICONS = {
    'movies': FluentIcon.VIDEO,
    'tv': FluentIcon.PROJECTOR,
    'anime': FluentIcon.PLAY,
    'manga': FluentIcon.DICTIONARY,
    'books': FluentIcon.BOOK_SHELF,
    'games': FluentIcon.GAME,
    'downloads': FluentIcon.DOWNLOAD,
    'roms': FluentIcon.APPLICATION,
    'youtube': FluentIcon.PLAY,
}

_DOWNLOAD_RANK = {
    'downloading': 0, 'verifying': 1, 'queued': 2, 'seeding': 3,
    'paused': 4, 'error': 5, 'completed': 6, 'cancelled': 7,
}


@dataclass
class SailSeekResult:
    uid: str
    category: str
    title: str
    subtitle: str = ''
    image: str = ''
    score: int = 2
    action: Optional[Callable[[], None]] = None
    secondary: list[tuple[str, Callable[[], None]]] = field(default_factory=list)
    progress: Optional[float] = None


def _type_label(kind: str) -> str:
    return {
        'movie': tr('sailseek.type_movie', 'Movie'),
        'tv': tr('sailseek.type_series', 'Series'),
        'anime': tr('sailseek.type_anime', 'Anime'),
        'manga': tr('sailseek.type_manga', 'Manga'),
        'book': tr('sailseek.type_book', 'Book'),
    }.get(kind, kind.title())


def _palette_color(key: str, fallback: str = '#808080') -> QColor:
    try:
        return to_color(palette().get(key, fallback), fallback)
    except Exception:
        return QColor(fallback)


def _with_alpha(color: QColor, alpha: int) -> QColor:
    out = QColor(color)
    out.setAlpha(max(0, min(255, alpha)))
    return out


def _screen_for_cursor():
    try:
        screen = QGuiApplication.screenAt(QCursor.pos())
    except Exception:
        screen = None
    return screen or QGuiApplication.primaryScreen()


def _normalize_query(text: str) -> str:
    return ' '.join(str(text or '').split())


def _match_score(query: str, title: str, extra: str = '') -> Optional[int]:
    q = query.casefold()
    title_f = (title or '').casefold()
    hay = f'{title_f} {extra or ""}'.casefold()
    if not q:
        return 4
    if title_f == q:
        return 0
    if title_f.startswith(q):
        return 1
    if q in title_f:
        return 2
    terms = q.split()
    if terms and all(term in hay for term in terms):
        return 3
    return None


def _format_year(value: Any) -> str:
    text = str(value or '').strip()
    return text[:4] if len(text) >= 4 and text[:4].isdigit() else text


_W = 640
_SHADOW = 14
_SEARCH_H = 54
_GAP = 6
_PAD = 4
_FOOT_H = 28
_ROW_H = 46
_MAX_ROWS = 7
_ICON_W = 28
_ICON_H = 36
_ANIM_MS = 120
_YT_H = 142
_YT_THUMB = (128, 72)
_YT_HEIGHTS = (0, 1080, 720, 480, 360, 240, 144)

_PREFIXES = {
    'd': 'downloads', 'dl': 'downloads', 'downloads': 'downloads',
    'g': 'games', 'game': 'games', 'games': 'games',
    'r': 'roms', 'rom': 'roms', 'roms': 'roms',
    'm': 'movies', 'movie': 'movies', 'movies': 'movies',
    's': 'tv', 'tv': 'tv', 'series': 'tv',
    'a': 'anime', 'anime': 'anime',
    'mg': 'manga', 'manga': 'manga',
    'b': 'books', 'book': 'books', 'books': 'books',
}
_PREFIX_RE = re.compile(r'^\s*([A-Za-z]{1,9}):\s?(.*)$', re.S)


def _category_label(key: str) -> str:
    return {
        'all': tr('sailseek.category_all', 'All'),
        'downloads': tr('sailseek.category_downloads', 'Downloads'),
        'games': tr('sailseek.category_games', 'Games'),
        'roms': tr('sailseek.category_roms', 'ROMs'),
        'movies': tr('sailseek.category_movies', 'Movies'),
        'tv': tr('sailseek.category_tv', 'TV'),
        'anime': tr('sailseek.category_anime', 'Anime'),
        'manga': tr('sailseek.category_manga', 'Manga'),
        'books': tr('sailseek.category_books', 'Books'),
    }.get(key, key.title())


def _clean_game_title(raw: str) -> str:
    text = raw or ''
    try:
        from src.core.repacks.base import display_title

        return display_title(text) or text
    except Exception:
        return text


def _contrast_fg(bg: QColor) -> QColor:
    lum = (0.299 * bg.red() + 0.587 * bg.green() + 0.114 * bg.blue()) / 255.0
    return QColor('#ffffff') if lum < 0.62 else QColor('#101010')


def _cover_pixmap(pixmap: QPixmap, w: int, h: int, dpr: float) -> QPixmap:
    tw, th = max(1, int(w * dpr)), max(1, int(h * dpr))
    scaled = pixmap.scaled(tw, th, Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                           Qt.TransformationMode.SmoothTransformation)
    sx = max(0, (scaled.width() - tw) // 2)
    sy = max(0, (scaled.height() - th) // 2)
    out = scaled.copy(sx, sy, tw, th)
    out.setDevicePixelRatio(dpr)
    return out


def os_path_mtime(path: str) -> float:
    try:
        return os.path.getmtime(path)
    except Exception:
        return 0.0


class _ResultsModel(QAbstractListModel):
    def __init__(self, parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self._items: list[SailSeekResult] = []
        self._pixmaps: dict[str, QPixmap] = {}
        self._rows: dict[str, int] = {}

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:
        if parent.isValid():
            return 0
        return len(self._items)

    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole):
        if not index.isValid() or not (0 <= index.row() < len(self._items)):
            return None
        item = self._items[index.row()]
        if role == _RESULT_ROLE:
            return item
        if role == Qt.ItemDataRole.DisplayRole:
            return item.title
        return None

    def flags(self, index: QModelIndex):
        if not index.isValid():
            return Qt.ItemFlag.NoItemFlags
        return Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable

    def result_at(self, row: int) -> Optional[SailSeekResult]:
        if 0 <= row < len(self._items):
            return self._items[row]
        return None

    def uids(self) -> list[str]:
        return [r.uid for r in self._items]

    def row_for_uid(self, uid: str) -> int:
        return self._rows.get(uid, -1)

    def pixmap(self, uid: str) -> Optional[QPixmap]:
        return self._pixmaps.get(uid)

    def set_results(self, results: list[SailSeekResult]) -> None:
        new = list(results)
        if [r.uid for r in new] == [r.uid for r in self._items]:

            self._items = new
            if new:
                self.dataChanged.emit(self.index(0, 0), self.index(len(new) - 1, 0))
            return
        self.beginResetModel()
        self._items = new
        uids = {item.uid for item in new}
        self._pixmaps = {uid: pm for uid, pm in self._pixmaps.items() if uid in uids}
        self._rows = {item.uid: row for row, item in enumerate(new)}
        self.endResetModel()

    def clear_pixmaps(self) -> None:
        if not self._pixmaps:
            return
        self._pixmaps.clear()
        if self._items:
            self.dataChanged.emit(self.index(0, 0), self.index(len(self._items) - 1, 0))

    def set_pixmap(self, uid: str, pixmap: QPixmap, dpr: float = 1.0) -> None:
        row = self._rows.get(uid)
        if row is None or pixmap.isNull():
            return
        self._pixmaps[uid] = _cover_pixmap(pixmap, _ICON_W, _ICON_H, dpr)
        index = self.index(row, 0)
        self.dataChanged.emit(index, index)


class _ResultDelegate(QStyledItemDelegate):
    ROW_HEIGHT = _ROW_H

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._font_key = None
        self._title_font = QFont()
        self._sub_font = QFont()
        self._hint_font = QFont()
        self._tm = self._sm = self._hm = None
        self._elide: dict[tuple, str] = {}
        self._icons: dict[str, Any] = {}
        self._empty_icon: Any = None
        self.refresh()

    @staticmethod
    def _make_icon(icon) -> Any:
        try:
            return themed_icon(None, icon, 18)
        except Exception:
            return icon.icon() if hasattr(icon, 'icon') else icon

    def refresh(self) -> None:
        self._icons = {key: self._make_icon(icon) for key, icon in _CATEGORY_ICONS.items()}
        self._empty_icon = self._make_icon(FluentIcon.SEARCH)
        self._primary = _palette_color('primary_text', '#f0f0f0')
        self._muted = _palette_color('muted', '#9a9a9a')
        self._accent = _palette_color('accent', '#4f7cff')
        self._sel_fg = _contrast_fg(self._accent)

    def sizeHint(self, option: QStyleOptionViewItem, index: QModelIndex) -> QSize:
        return QSize(option.rect.width(), _ROW_H)

    def _ensure_fonts(self, font: QFont) -> None:
        key = font.toString()
        if key == self._font_key:
            return
        self._font_key = key
        t = QFont(font)
        t.setPixelSize(13)
        t.setWeight(QFont.Weight.DemiBold)
        s = QFont(font)
        s.setPixelSize(11)
        h = QFont(font)
        h.setPixelSize(10)
        self._title_font, self._sub_font, self._hint_font = t, s, h
        self._tm, self._sm, self._hm = QFontMetrics(t), QFontMetrics(s), QFontMetrics(h)
        self._elide.clear()

    def _elided(self, text: str, kind: int, metrics: QFontMetrics, width: int) -> str:
        key = (kind, text, width)
        val = self._elide.get(key)
        if val is None:
            if len(self._elide) > 800:
                self._elide.clear()
            val = metrics.elidedText(text, Qt.TextElideMode.ElideRight, width)
            self._elide[key] = val
        return val

    def paint(self, painter: QPainter, option: QStyleOptionViewItem, index: QModelIndex) -> None:
        item = index.data(_RESULT_ROLE)
        if not isinstance(item, SailSeekResult):
            return
        self._ensure_fonts(option.font)
        model = index.model()
        row = option.rect.adjusted(4, 1, -4, -1)
        selected = bool(option.state & QStyle.StateFlag.State_Selected)
        hovered = bool(option.state & QStyle.StateFlag.State_MouseOver)
        fg = self._sel_fg if selected else self._primary
        sub_fg = _with_alpha(self._sel_fg, 200) if selected else self._muted

        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)
        if selected:
            painter.setBrush(self._accent)
            painter.drawRoundedRect(QRectF(row), 6, 6)
        elif hovered:
            painter.setBrush(_with_alpha(self._primary, 16))
            painter.drawRoundedRect(QRectF(row), 6, 6)

        icon_rect = QRect(row.left() + 10, row.top() + (row.height() - _ICON_H) // 2,
                          _ICON_W, _ICON_H)
        pixmap = model.pixmap(item.uid) if hasattr(model, 'pixmap') else None
        clip = QPainterPath()
        clip.addRoundedRect(QRectF(icon_rect), 5, 5)
        painter.save()
        painter.setClipPath(clip)
        if pixmap is not None and not pixmap.isNull():
            painter.drawPixmap(icon_rect.topLeft(), pixmap)
        else:
            painter.fillRect(icon_rect, _with_alpha(self._sel_fg if selected else self._muted,
                                                    40 if selected else 28))
            icon = self._icons.get(item.category, self._empty_icon)
            if isinstance(icon, QIcon):
                c = icon_rect.center()
                icon.paint(painter, QRect(c.x() - 9, c.y() - 9, 18, 18))
        painter.restore()

        text_left = icon_rect.right() + 12
        text_right = row.right() - 70
        text_w = max(20, text_right - text_left)
        th, sh = self._tm.height(), self._sm.height()
        y0 = row.top() + max(2, (row.height() - (th + sh + 1)) // 2)

        painter.setFont(self._title_font)
        painter.setPen(fg)
        painter.drawText(QRect(text_left, y0, text_w, th),
                         Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                         self._elided(item.title, 0, self._tm, text_w))
        painter.setFont(self._sub_font)
        painter.setPen(sub_fg)
        painter.drawText(QRect(text_left, y0 + th + 1, text_w, sh),
                         Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                         self._elided(item.subtitle or '', 1, self._sm, text_w))

        if item.progress is not None:
            pct = max(0.0, min(100.0, float(item.progress)))
            track = QRectF(text_left, row.bottom() - 4, row.right() - 14 - text_left, 2)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(_with_alpha(fg, 50))
            painter.drawRoundedRect(track, 1, 1)
            if pct > 0:
                fill = QRectF(track)
                fill.setWidth(track.width() * pct / 100.0)
                painter.setBrush(self._sel_fg if selected else self._accent)
                painter.drawRoundedRect(fill, 1, 1)

        if selected:
            hint = 'Enter'
            hw = self._hm.horizontalAdvance(hint) + 12
            box = QRect(row.right() - hw - 10, row.center().y() - 9, hw, 18)
            painter.setPen(QPen(_with_alpha(self._sel_fg, 110), 1))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRoundedRect(QRectF(box).adjusted(0.5, 0.5, -0.5, -0.5), 4, 4)
            painter.setFont(self._hint_font)
            painter.setPen(_with_alpha(self._sel_fg, 210))
            painter.drawText(box, Qt.AlignmentFlag.AlignCenter, hint)
        painter.restore()


class _ResultsView(QListView):
    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setVerticalScrollMode(QListView.ScrollMode.ScrollPerPixel)
        self.verticalScrollBar().setSingleStep(15)
        self.setSelectionMode(QListView.SelectionMode.SingleSelection)
        self.setEditTriggers(QListView.EditTrigger.NoEditTriggers)
        self.setUniformItemSizes(True)
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.viewport().setAutoFillBackground(False)
        self.setStyleSheet(
            'QListView { background: transparent; border: none; outline: none; }'
            'QScrollBar:vertical { background: transparent; width: 6px; margin: 2px 0; }'
            'QScrollBar::handle:vertical { background: rgba(154, 154, 154, 80); '
            'min-height: 28px; border-radius: 3px; }'
            'QScrollBar::handle:vertical:hover { background: rgba(154, 154, 154, 130); }'
            'QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }'
            'QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical '
            '{ background: transparent; }'
        )
        self.placeholder = ''
        self._hover_row = -1

    def mouseMoveEvent(self, event) -> None:
        index = self.indexAt(event.position().toPoint())
        row = index.row() if index.isValid() else -1
        if row != self._hover_row:
            self._hover_row = row
            if row >= 0:
                _sfx('hover')
        super().mouseMoveEvent(event)

    def leaveEvent(self, event) -> None:
        self._hover_row = -1
        super().leaveEvent(event)

    def paintEvent(self, event) -> None:
        super().paintEvent(event)
        model = self.model()
        if (model is not None and model.rowCount() > 0) or not self.placeholder:
            return
        painter = QPainter(self.viewport())
        font = QFont(self.font())
        font.setPixelSize(12)
        painter.setFont(font)
        painter.setPen(_palette_color('muted', '#9a9a9a'))
        rect = self.viewport().rect().adjusted(16, 0, -16, 0)
        text = QFontMetrics(font).elidedText(self.placeholder, Qt.TextElideMode.ElideRight, rect.width())
        painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, text)


class _Footer(QWidget):

    chipSelected = Signal(str)

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._font = QFont(self.font())
        self._font.setPixelSize(11)
        self._chips: list[tuple[str, str]] = []
        self._rects: list[QRect] = []
        self._active = 'all'
        self._hover = -1
        self._status = ''
        self._chips_end = 0

    def set_chips(self, keys) -> None:
        self._chips = [(k, _category_label(k)) for k in ['all', *keys]]
        self._relayout()
        self.update()

    def set_active(self, key: str) -> None:
        if key != self._active:
            self._active = key
            self.update()

    def set_status(self, text: str) -> None:
        if text != self._status:
            self._status = text
            self.update()

    def _relayout(self) -> None:
        fm = QFontMetrics(self._font)
        x = 6
        self._rects = []
        for _key, label in self._chips:
            w = fm.horizontalAdvance(label) + 16
            self._rects.append(QRect(x, (self.height() - 20) // 2, w, 20))
            x += w + 2
        self._chips_end = x

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._relayout()

    def _hit(self, pos: QPoint) -> int:
        for i, rect in enumerate(self._rects):
            if rect.contains(pos):
                return i
        return -1

    def mouseMoveEvent(self, event) -> None:
        i = self._hit(event.position().toPoint())
        if i != self._hover:
            self._hover = i
            if i >= 0:
                _sfx('hover')
            self.setCursor(Qt.CursorShape.PointingHandCursor if i >= 0 else Qt.CursorShape.ArrowCursor)
            self.update()
        super().mouseMoveEvent(event)

    def leaveEvent(self, event) -> None:
        self._hover = -1
        self.unsetCursor()
        self.update()
        super().leaveEvent(event)

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            i = self._hit(event.position().toPoint())
            if i >= 0:
                _sfx('click')
                self.chipSelected.emit(self._chips[i][0])
                event.accept()
                return
        super().mousePressEvent(event)

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        muted = _palette_color('muted', '#9a9a9a')
        primary = _palette_color('primary_text', '#f0f0f0')
        accent = _palette_color('accent', '#4f7cff')
        painter.setPen(QPen(_with_alpha(_palette_color('card_border', '#3a3a3a'), 150), 1))
        painter.drawLine(8, 0, self.width() - 8, 0)
        painter.setFont(self._font)
        for i, ((key, label), rect) in enumerate(zip(self._chips, self._rects)):
            active = key == self._active
            if active:
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(_with_alpha(accent, 70))
                painter.drawRoundedRect(QRectF(rect), 10, 10)
            elif i == self._hover:
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(_with_alpha(primary, 20))
                painter.drawRoundedRect(QRectF(rect), 10, 10)
            painter.setPen(primary if active else muted)
            painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, label)
        avail = self.width() - 12 - (self._chips_end + 8)
        if self._status and avail > 50:
            fm = QFontMetrics(self._font)
            painter.setPen(_with_alpha(muted, 190))
            painter.drawText(
                QRect(self._chips_end + 8, 0, avail, self.height()),
                Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                fm.elidedText(self._status, Qt.TextElideMode.ElideRight, avail))


class _YtCard(QWidget):
    download = Signal()
    menuRequested = Signal(str, QPoint)

    _PAD = 14
    _CTL = 28

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._info: dict = {}
        self._thumb: Optional[QPixmap] = None
        self._playlist = False
        self._mode = 'video'
        self._tiers: dict[str, list[tuple[str, Any]]] = {'video': [], 'audio': []}
        self._pick = {'video': 0, 'audio': 0}
        self._subs: list[tuple[str, str]] = []
        self._sub = 0
        self.done = False
        self._hits: list[tuple[QRect, str, Any]] = []
        self._hover = -1

    def set_info(self, info: dict) -> None:
        self._info, self._thumb, self.done = info, None, False
        self._playlist = 'entries' in info
        self._subs = [(tr('yt.subtitles_none'), '')]
        if self._playlist:
            video = [(tr('yt.quality_max', height=h) if h else tr('yt.quality_best'), h) for h in _YT_HEIGHTS]
            audio = [(tr('yt.quality_best'), 0)]
        else:
            auto = [(tr('yt.quality_auto'), '')]
            video = [(t['label'], t['id']) for t in info.get('video_tiers') or ()] or auto
            audio = [(t['label'], t['id']) for t in info.get('audio_tiers') or ()] or auto
            from src.core.yt import provider
            self._subs += [(sub.get('name') or lang, lang) for lang, sub in sorted(provider.manual_subtitles(info).items())]
        self._tiers = {'video': video, 'audio': audio}
        self._mode, self._pick, self._sub = 'video', {'video': 0, 'audio': 0}, 0
        self.update()

    def set_thumb(self, pixmap: Optional[QPixmap]) -> None:
        if pixmap is None or pixmap.isNull():
            return
        dpr = self.devicePixelRatioF()
        w, h = round(_YT_THUMB[0] * dpr), round(_YT_THUMB[1] * dpr)
        scaled = pixmap.scaled(w, h, Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                               Qt.TransformationMode.SmoothTransformation)
        scaled = scaled.copy((scaled.width() - w) // 2, (scaled.height() - h) // 2, w, h)
        scaled.setDevicePixelRatio(dpr)
        self._thumb = scaled
        self.update()

    def selection(self) -> tuple[str, Any, str]:
        mode = self._mode
        return mode, self._tiers[mode][self._pick[mode]][1], self._subs[self._sub][1] if mode == 'video' else ''

    def options(self, kind: str) -> list[tuple[str, bool]]:
        if kind == 'subs':
            return [(label, i == self._sub) for i, (label, _v) in enumerate(self._subs)]
        return [(label, i == self._pick[self._mode]) for i, (label, _v) in enumerate(self._tiers[self._mode])]

    def choose(self, kind: str, index: int) -> None:
        if kind == 'subs':
            self._sub = index
        else:
            self._pick[self._mode] = index
        self.update()

    def toggle_mode(self) -> None:
        self._mode = 'audio' if self._mode == 'video' else 'video'
        self.update()

    def step(self, key: int) -> None:
        if key in (Qt.Key.Key_Left, Qt.Key.Key_Right):
            last = len(self._tiers[self._mode]) - 1
            delta = 1 if key == Qt.Key.Key_Right else -1
            self._pick[self._mode] = max(0, min(last, self._pick[self._mode] + delta))
        else:
            self._sub = (self._sub + (1 if key == Qt.Key.Key_Down else -1)) % len(self._subs)
        self.update()

    def _hit(self, pos: QPoint) -> int:
        return next((i for i, (rect, _k, _v) in enumerate(self._hits) if rect.contains(pos)), -1)

    def mouseMoveEvent(self, event) -> None:
        i = self._hit(event.position().toPoint())
        if i != self._hover:
            self._hover = i
            if i >= 0:
                _sfx('hover')
            self.setCursor(Qt.CursorShape.PointingHandCursor if i >= 0 else Qt.CursorShape.ArrowCursor)
            self.update()

    def leaveEvent(self, event) -> None:
        self._hover = -1
        self.unsetCursor()
        self.update()
        super().leaveEvent(event)

    def mousePressEvent(self, event) -> None:
        i = self._hit(event.position().toPoint()) if event.button() == Qt.MouseButton.LeftButton else -1
        if i < 0:
            super().mousePressEvent(event)
            return
        rect, kind, value = self._hits[i]
        _sfx('click')
        if kind == 'mode':
            self._mode = value
            self.update()
        elif kind == 'go':
            self.download.emit()
        else:
            self.menuRequested.emit(kind, self.mapToGlobal(rect.bottomLeft() + QPoint(0, 4)))
        event.accept()

    def _font(self, px: int, bold: bool = False) -> QFont:
        font = QFont(self.font())
        font.setPixelSize(px)
        font.setBold(bold)
        return font

    def paintEvent(self, event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        primary = _palette_color('primary_text', '#f0f0f0')
        muted = _palette_color('muted', '#9a9a9a')
        accent = _palette_color('accent', '#4f7cff')
        pad, ctl = self._PAD, self._CTL
        w, info, playlist = self.width(), self._info, self._playlist
        hits: list[tuple[QRect, str, Any]] = []
        hot = lambda rect: 0 <= self._hover < len(self._hits) and self._hits[self._hover][0] == rect

        thumb = QRect(pad, pad, *_YT_THUMB)
        clip = QPainterPath()
        clip.addRoundedRect(QRectF(thumb), 6, 6)
        p.save()
        p.setClipPath(clip)
        if self._thumb is not None:
            p.drawPixmap(thumb.topLeft(), self._thumb)
        else:
            p.fillRect(thumb, _with_alpha(muted, 36))
        p.restore()
        duration = '' if playlist else info.get('duration_label') or ''
        if duration:
            p.setFont(self._font(11, True))
            bw = QFontMetrics(p.font()).horizontalAdvance(duration) + 10
            rect = QRect(thumb.right() - bw - 5, thumb.bottom() - 21, bw, 16)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(0, 0, 0, 185))
            p.drawRoundedRect(QRectF(rect), 4, 4)
            p.setPen(QColor(255, 255, 255))
            p.drawText(rect, Qt.AlignmentFlag.AlignCenter, duration)

        x0 = thumb.right() + 14
        tw = w - pad - x0
        title_font = self._font(15, True)
        flags = int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop | Qt.TextFlag.TextWordWrap)
        title = info.get('title', '')
        title_h = min(40, QFontMetrics(title_font).boundingRect(QRect(0, 0, tw, 40), flags, title).height())
        title_rect = QRect(x0, pad, tw, title_h)
        p.save()
        p.setClipRect(title_rect)
        p.setFont(title_font)
        p.setPen(primary)
        p.drawText(title_rect, flags, title)
        p.restore()
        meta = tr('sailseek.yt_playlist', '{count} videos', count=len(info.get('entries') or ())) if playlist \
            else info.get('uploader') or ''
        p.setFont(self._font(12))
        p.setPen(muted)
        p.drawText(QRect(x0, title_rect.bottom() + 6, tw, 16), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                   QFontMetrics(p.font()).elidedText(meta, Qt.TextElideMode.ElideRight, tw))

        row = thumb.bottom() + pad + 1
        p.setFont(self._font(12))
        fm = QFontMetrics(p.font())
        modes = (('video', tr('yt.mode_video')), ('audio', tr('yt.mode_audio')))
        sizes = [fm.horizontalAdvance(label) + 28 for _m, label in modes]
        seg = QRect(pad, row, sum(sizes), ctl)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(_with_alpha(primary, 16))
        p.drawRoundedRect(QRectF(seg), 6, 6)
        x = seg.left()
        for (mode, label), size in zip(modes, sizes):
            half = QRect(x, row, size, ctl)
            active = mode == self._mode
            if active:
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(_with_alpha(accent, 90))
                p.drawRoundedRect(QRectF(half).adjusted(2, 2, -2, -2), 4.5, 4.5)
            p.setPen(primary if active else muted)
            p.drawText(half, Qt.AlignmentFlag.AlignCenter, label)
            hits.append((half, 'mode', mode))
            x += size

        label = tr('yt.download') if not self.done else tr('sailseek.yt_added', 'Added')
        bw = max(88, fm.horizontalAdvance(label) + 32)
        button = QRect(w - pad - bw, row, bw, ctl)
        x = seg.right() + 8
        has_subs = not playlist and len(self._subs) > 1 and self._mode == 'video'
        subs_text = self._subs[self._sub][0] if self._sub else tr('yt.subtitles')
        subs_w = min(fm.horizontalAdvance(subs_text) + 36, 150) if has_subs else 0
        quality_text = self._tiers[self._mode][self._pick[self._mode]][0]
        room = button.left() - 10 - x - (subs_w + 8 if has_subs else 0)
        quality_w = max(72, min(fm.horizontalAdvance(quality_text) + 36, room))
        controls = [('quality', QRect(x, row, quality_w, ctl), quality_text, False)]
        if has_subs:
            controls.append(('subs', QRect(x + quality_w + 8, row, subs_w, ctl), subs_text, self._sub > 0))
        for kind, rect, text, on in controls:
            hovered = hot(rect)
            p.setPen(QPen(_with_alpha(accent if on else primary, 150 if on else 70 if hovered else 45), 1))
            p.setBrush(_with_alpha(accent, 36) if on else _with_alpha(primary, 14) if hovered else Qt.BrushStyle.NoBrush)
            p.drawRoundedRect(QRectF(rect).adjusted(0.5, 0.5, -0.5, -0.5), 6, 6)
            p.setPen(primary if on or hovered else muted)
            p.drawText(QRect(rect.left() + 12, row, rect.width() - 32, ctl),
                       Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                       fm.elidedText(text, Qt.TextElideMode.ElideRight, rect.width() - 32))
            cx, cy = rect.right() - 13, rect.center().y()
            p.setPen(QPen(p.pen().color(), 1.4, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
            p.drawPolyline([QPoint(cx - 3, cy - 1), QPoint(cx, cy + 2), QPoint(cx + 3, cy - 1)])
            hits.append((rect, kind, None))

        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(_with_alpha(accent, 70) if self.done else accent.lighter(112) if hot(button) else accent)
        p.drawRoundedRect(QRectF(button), 6, 6)
        p.setPen(primary if self.done else _contrast_fg(accent))
        p.drawText(button, Qt.AlignmentFlag.AlignCenter, label)
        hits.append((button, 'go', None))
        self._hits = hits
        p.end()


class SailSeekWindow(QWidget):
    _GEOMETRY_ORG = 'PiraChest'
    _GEOMETRY_APP = 'SailSeek'
    _LEGACY_GEOMETRY_APP = 'Spotlight'

    def __init__(self, controller: 'SailSeekController') -> None:
        super().__init__(None, Qt.WindowType.Tool | Qt.WindowType.FramelessWindowHint
                         | Qt.WindowType.WindowStaysOnTopHint | Qt.WindowType.NoDropShadowWindowHint)
        self._controller = controller
        self._menu_open = False
        self._background_path = ''
        self._background_key = None
        self._background_pixmap: Optional[QPixmap] = None
        self._active_filter = 'all'
        self._panel_on = False
        self._count = 0
        self._held = 0
        self._busy = False
        self._busy_since = 0.0
        self._user_moved = False
        self._dragged = False
        self._target = -1

        self._save_timer = QTimer(self)
        self._save_timer.setSingleShot(True)
        self._save_timer.setInterval(350)
        self._save_timer.timeout.connect(self.save_geometry)
        self._art_timer = QTimer(self)
        self._art_timer.setSingleShot(True)
        self._art_timer.setInterval(40)
        self._art_timer.timeout.connect(self._request_visible_artwork)
        self._busy_timer = QTimer(self)
        self._busy_timer.setInterval(33)
        self._busy_timer.timeout.connect(self._tick_busy)

        self.setObjectName('sailseekWindow')
        self.setWindowTitle('SailSeek')
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setFixedWidth(_W + 2 * _SHADOW)

        self._search_icon = QLabel(self)
        self._search_icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._search_icon.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self._edit = _SearchEdit(self)
        self._edit.setPlaceholderText(tr('sailseek.placeholder', 'Search SailSeek…'))

        self._model = _ResultsModel(self)
        self._view = _ResultsView(self)
        self._view.setModel(self._model)
        self._delegate = _ResultDelegate(self._view)
        self._view.setItemDelegate(self._delegate)
        self._view.clicked.connect(self._on_clicked)
        self._view.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._view.customContextMenuRequested.connect(self._on_context_menu)
        self._view.verticalScrollBar().valueChanged.connect(self._schedule_artwork)

        self._footer = _Footer(self)
        self._footer.chipSelected.connect(self.set_category_filter)
        self._yt_on = False
        self._yt = _YtCard(self)
        self._yt.hide()
        self._yt.download.connect(controller._youtube_download)
        self._yt.menuRequested.connect(self._yt_menu)

        self._height_anim = QVariantAnimation(self)
        self._height_anim.setDuration(_ANIM_MS)
        self._height_anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._height_anim.valueChanged.connect(lambda v: self._apply_height(int(v)))
        self._fade = QPropertyAnimation(self, b'windowOpacity', self)
        self._fade.setDuration(90)
        self._fade.setStartValue(0.0)
        self._fade.setEndValue(1.0)

        self._edit.textChanged.connect(self._on_text_changed)

        register_theme_refresh(self, self.refresh_theme)
        register_locale_refresh(self, self.apply_locale)
        self.refresh_theme()
        self._footer.set_chips([])
        self._update_height(False)


    @staticmethod
    def _foot_h() -> int:
        return _FOOT_H if bool(getattr(settings, 'sailseek_show_filters', True)) else 6

    def _max_height(self) -> int:
        return 2 * _SHADOW + _SEARCH_H + _GAP + _PAD + _MAX_ROWS * _ROW_H + self._foot_h()

    def _search_rect(self) -> QRect:
        return QRect(_SHADOW, _SHADOW, _W, _SEARCH_H)

    def _panel_rect(self) -> QRect:
        top = _SHADOW + _SEARCH_H + _GAP
        return QRect(_SHADOW, top, _W, max(0, self.height() - _SHADOW - top))

    def _rows_now(self) -> int:
        rows = max(1, min(self._count, _MAX_ROWS))
        if self._busy:

            self._held = max(self._held, rows)
            rows = self._held
        return rows

    def _target_height(self) -> int:
        h = 2 * _SHADOW + _SEARCH_H
        if self._yt_on:
            h += _GAP + _YT_H
        elif self._panel_on:
            h += _GAP + _PAD + self._rows_now() * _ROW_H + self._foot_h()
        return h

    def _update_height(self, animate: bool) -> None:
        target = self._target_height()
        if target == self._target:
            return
        self._target = target
        self._height_anim.stop()
        if not animate or not self.isVisible() or abs(target - self.height()) < 2:
            self._apply_height(target)
            return
        self._height_anim.setStartValue(self.height())
        self._height_anim.setEndValue(target)
        self._height_anim.start()

    def _apply_height(self, h: int) -> None:
        if h != self.height():
            self.resize(self.width(), h)
        else:
            self._layout_children()

    def _layout_children(self) -> None:
        s = self._search_rect()
        self._edit.setGeometry(s.left() + 18, s.top() + 8, s.width() - 18 - 56, s.height() - 16)
        self._search_icon.setGeometry(s.right() - 46, s.top() + (s.height() - 24) // 2, 32, 24)
        p = self._panel_rect()
        foot = self._foot_h()
        self._yt.setVisible(self._yt_on and p.height() >= 12)
        if self._yt_on:
            self._yt.setGeometry(p)
        show = not self._yt_on and p.height() >= foot + 2 * _PAD
        self._view.setVisible(show)
        self._footer.setVisible(show and foot == _FOOT_H)
        if show:
            self._footer.setGeometry(p.left(), p.top() + p.height() - foot, p.width(), foot)
            self._view.setGeometry(p.left() + _PAD, p.top() + _PAD, p.width() - 2 * _PAD,
                                   max(0, p.height() - foot - _PAD))

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._layout_children()

    def _yt_menu(self, kind: str, pos: QPoint) -> None:
        menu = RoundMenu(parent=self)
        menu.setMaxVisibleItems(7)
        for i, (label, active) in enumerate(self._yt.options(kind)):
            action = Action(label, triggered=lambda _checked=False, i=i: self._yt.choose(kind, i))
            action.setCheckable(True)
            action.setChecked(active)
            menu.addAction(action)
        self._menu_open = True
        try:
            menu.exec(pos)
        finally:
            self._menu_open = False
            self._edit.setFocus()

    def set_youtube(self, info: Optional[dict]) -> None:
        if info is not None:
            self._yt.set_info(info)
        self._yt_on = info is not None
        self._update_height(self.isVisible())
        self._layout_children()
        self.update()


    def showEvent(self, event) -> None:
        super().showEvent(event)
        self._art_timer.start()
        try:
            self._fade.stop()
            self.setWindowOpacity(0.0)
            self._fade.start()
        except Exception:
            self.setWindowOpacity(1.0)
        if self._busy:
            self._busy_timer.start()

    def hideEvent(self, event) -> None:
        self._busy_timer.stop()
        self._height_anim.stop()
        self.save_geometry()
        super().hideEvent(event)

    def closeEvent(self, event) -> None:
        self.save_geometry()
        super().closeEvent(event)

    def event(self, event) -> bool:
        if event.type() == QEvent.Type.WindowDeactivate and self.isVisible() and not self._menu_open:
            QTimer.singleShot(70, self._hide_if_inactive)
        return super().event(event)

    def _hide_if_inactive(self) -> None:
        if not self.isVisible() or self._menu_open:
            return
        active = QApplication.activeWindow()
        if active is self or (active is not None and active.parent() is self):
            return
        self.hide()

    def moveEvent(self, event) -> None:
        super().moveEvent(event)
        if self._dragged:
            self._save_timer.start()

    def mousePressEvent(self, event) -> None:
        if (event.button() == Qt.MouseButton.LeftButton
                and self._search_rect().contains(event.position().toPoint())):
            handle = self.windowHandle()
            if handle is not None:
                self._dragged = True
                handle.startSystemMove()
                event.accept()
                return
        super().mousePressEvent(event)


    def reset_geometry(self) -> None:
        try:
            for app in (self._GEOMETRY_APP, self._LEGACY_GEOMETRY_APP):
                store = QSettings(self._GEOMETRY_ORG, app)
                store.remove('geometry')
                store.remove('pos')
        except Exception:
            logger.debug('Could not clear SailSeek geometry', exc_info=True)
        self._dragged = False
        self.apply_default_geometry()

    def apply_default_geometry(self) -> None:
        self.move(self._default_pos(_screen_for_cursor()))

    def _default_pos(self, screen) -> QPoint:
        avail = screen.availableGeometry() if screen is not None else QRect(0, 0, 1280, 800)
        w = _W + 2 * _SHADOW
        x = avail.left() + (avail.width() - w) // 2
        y = avail.top() + int(avail.height() * 0.2) - _SHADOW
        return self._clamp_pos(QPoint(x, y), avail)

    def _clamp_pos(self, pos: QPoint, avail: QRect) -> QPoint:
        w = _W + 2 * _SHADOW
        x = min(max(pos.x(), avail.left()), max(avail.left(), avail.right() - w + 1))
        y = min(max(pos.y(), avail.top()), max(avail.top(), avail.bottom() - self._max_height() + 1))
        return QPoint(x, y)

    def restore_geometry(self) -> None:
        screen = _screen_for_cursor()
        avail = screen.availableGeometry() if screen is not None else QRect(0, 0, 1280, 800)
        pos = None
        if not bool(getattr(settings, 'sailseek_remember_position', True)):
            self.move(self._default_pos(screen))
            return
        try:
            saved = QSettings(self._GEOMETRY_ORG, self._GEOMETRY_APP).value('pos')
            if isinstance(saved, QPoint) and avail.contains(saved + QPoint(_W // 2, 24)):
                pos = saved
        except Exception:
            pos = None
        self.move(self._clamp_pos(pos, avail) if pos is not None else self._default_pos(screen))

    def save_geometry(self, force: bool = False) -> None:
        if not self._dragged or (not force and not self.isVisible()):
            return
        if not bool(getattr(settings, 'sailseek_remember_position', True)):
            return
        try:
            QSettings(self._GEOMETRY_ORG, self._GEOMETRY_APP).setValue('pos', self.pos())
        except Exception:
            logger.debug('Could not save SailSeek position', exc_info=True)


    def refresh_theme(self) -> None:
        try:
            primary = _palette_color('primary_text', '#f2f2f2')
            muted = _palette_color('muted', '#9a9a9a')
            accent_soft = _with_alpha(_palette_color('accent', '#4f7cff'), 90).name(
                QColor.NameFormat.HexArgb)
            self.setStyleSheet(
                'QWidget#sailseekWindow { background: transparent; }'
                f'QLineEdit {{ background: transparent; border: none; color: {primary.name()}; '
                f'selection-background-color: {accent_soft}; font-size: 20px; padding: 0; }}'
                'QLineEdit:focus { border: none; }'
                'QLabel { background: transparent; }'
            )
            pal = self._edit.palette()
            pal.setColor(QPalette.ColorRole.PlaceholderText, _with_alpha(muted, 200))
            self._edit.setPalette(pal)
        except Exception:
            logger.debug('SailSeek stylesheet refresh failed', exc_info=True)
        try:
            self._search_icon.setPixmap(
                themed_icon('sailseek.search', FluentIcon.SEARCH, 18).pixmap(18, 18))
        except Exception:
            pass
        self._delegate.refresh()
        self._footer.update()
        self._yt.update()
        self._view.viewport().update()
        self.update_background()
        self._target = -1
        self._update_height(False)
        self.update()

    def apply_locale(self) -> None:
        self._edit.setPlaceholderText(tr('sailseek.placeholder', 'Search SailSeek…'))
        self._footer.set_chips(self._controller.available_categories())
        self._controller.relocalize()


    def _chip_keys(self) -> list[str]:
        return ['all', *self._controller.available_categories()]

    def set_category_filter(self, key: str) -> None:
        key = key or 'all'
        stripped = self._controller.strip_prefix(self._edit.text())
        if stripped is not None:
            self._edit.blockSignals(True)
            self._edit.setText(stripped)
            self._edit.blockSignals(False)
        self._user_moved = False
        self.set_active_filter(key)
        self._controller.set_filter(key, stripped)

    def set_active_filter(self, key: str) -> None:
        self._active_filter = key or 'all'
        self._footer.set_active(self._active_filter)

    def cycle_filter(self, step: int) -> None:
        keys = self._chip_keys()
        try:
            i = keys.index(self._active_filter)
        except ValueError:
            i = 0
        self.set_category_filter(keys[(i + step) % len(keys)])

    def select_chip_index(self, i: int) -> None:
        keys = self._chip_keys()
        if 0 <= i < len(keys):
            self.set_category_filter(keys[i])

    def filter_category(self) -> str:
        return self._active_filter


    def _on_text_changed(self, text: str) -> None:
        self._user_moved = False
        self._controller.on_text_changed(text)

    def reset_for_show(self) -> None:
        self._edit.blockSignals(True)
        self._edit.clear()
        self._edit.blockSignals(False)
        self._active_filter = 'all'
        self._user_moved = False
        self._footer.set_chips(self._controller.available_categories())
        self._footer.set_active('all')
        self._footer.set_status('')
        self._edit.setFocus(Qt.FocusReason.ShortcutFocusReason)
        self.restore_geometry()
        self.update_background()
        self._model.set_results([])
        self._yt_on = False
        self._yt.hide()
        self._view.placeholder = ''
        self._view.scrollToTop()
        self._panel_on = False
        self._count = 0
        self._held = 0
        self._busy = False
        self._busy_timer.stop()
        self._target = -1
        self._update_height(False)
        self._art_timer.start()

    def set_results(self, results: list[SailSeekResult], panel: bool = True) -> None:
        if bool(panel) == self._panel_on and [r.uid for r in results] == self._model.uids():
            self._model.set_results(results)
            self._count = len(results)
            self._update_height(self.isVisible())
            self._view.viewport().update()
            return
        current = self.current_result()
        current_uid = current.uid if current is not None else ''
        self._panel_on = bool(panel)
        self._model.set_results(results)
        self._count = len(results)
        if results:

            row = self._model.row_for_uid(current_uid) if self._user_moved else -1
            if row < 0:
                row = 0
                self._view.scrollToTop()
            index = self._model.index(row, 0)
            self._view.setCurrentIndex(index)
            if row != 0:
                self._view.scrollTo(index, QListView.ScrollHint.EnsureVisible)
        else:
            self._view.clearSelection()
            self._view.setCurrentIndex(QModelIndex())
            self._view.scrollToTop()
        self._update_height(self.isVisible())
        self._view.viewport().update()
        self._schedule_artwork()

    def set_placeholder(self, text: str) -> None:
        text = text or ''
        if text != self._view.placeholder:
            self._view.placeholder = text
            self._view.viewport().update()

    def set_status(self, text: str, online_text: str = '') -> None:
        self._footer.set_status(text or '')

    def set_busy(self, busy: bool) -> None:
        busy = bool(busy)
        if busy == self._busy:
            return
        self._busy = busy
        if busy:
            self._held = max(1, min(self._count, _MAX_ROWS)) if self._panel_on else 0
            self._busy_since = time.monotonic()
            if self.isVisible():
                self._busy_timer.start()
        else:
            self._busy_timer.stop()
            self._held = 0
            self._update_height(self.isVisible())
            self.update(self._search_rect().adjusted(0, _SEARCH_H - 4, 0, 0))

    def _tick_busy(self) -> None:
        if self.isVisible():
            s = self._search_rect()
            self.update(QRect(s.left(), s.bottom() - 3, s.width(), 5))

    def current_result(self) -> Optional[SailSeekResult]:
        index = self._view.currentIndex()
        if not index.isValid():
            return None
        return index.data(_RESULT_ROLE)

    def move_selection(self, delta: int) -> None:
        count = self._model.rowCount()
        if count <= 0:
            return
        index = self._view.currentIndex()
        row = index.row() if index.isValid() else -1
        if abs(delta) == 1:
            row = (row + delta) % count if row >= 0 else (0 if delta > 0 else count - 1)
        else:
            row = max(0, min(count - 1, row + delta))
        self._user_moved = True
        new_index = self._model.index(row, 0)
        self._view.setCurrentIndex(new_index)
        self._view.scrollTo(new_index, QListView.ScrollHint.EnsureVisible)

    def select_edge(self, last: bool) -> None:
        count = self._model.rowCount()
        if count <= 0:
            return
        self._user_moved = True
        index = self._model.index(count - 1 if last else 0, 0)
        self._view.setCurrentIndex(index)
        self._view.scrollTo(index, QListView.ScrollHint.EnsureVisible)

    def _on_clicked(self, index: QModelIndex) -> None:
        item = index.data(_RESULT_ROLE)
        if isinstance(item, SailSeekResult):
            _sfx('click')
            self._controller.activate(item)

    def _on_context_menu(self, pos: QPoint) -> None:
        index = self._view.indexAt(pos)
        item = index.data(_RESULT_ROLE) if index.isValid() else None
        if not isinstance(item, SailSeekResult):
            return
        menu = RoundMenu(parent=self)
        open_label = tr('sailseek.open', 'Open')
        menu.addAction(Action(FluentIcon.PLAY, open_label,
                              triggered=lambda _checked=False, it=item: self._controller.activate(it)))
        for label, callback in item.secondary:
            menu.addAction(Action(FluentIcon.FOLDER, label,
                                  triggered=lambda _checked=False, cb=callback: self._run_secondary(cb)))
        self._menu_open = True
        try:
            menu.exec(self._view.viewport().mapToGlobal(pos))
        finally:
            self._menu_open = False

    def _run_secondary(self, callback: Callable[[], None]) -> None:
        self.hide()
        try:
            callback()
        except Exception:
            logger.exception('SailSeek secondary action failed')


    def _schedule_artwork(self) -> None:
        if self.isVisible():
            self._art_timer.start()

    def _request_visible_artwork(self) -> None:
        if not self.isVisible():
            return
        self._controller.request_visible_artwork()

    def visible_rows(self) -> tuple[int, int]:
        count = self._model.rowCount()
        if count == 0:
            return 0, -1
        top = self._view.indexAt(QPoint(2, 2)).row()
        bottom = self._view.indexAt(QPoint(2, max(2, self._view.viewport().height() - 2))).row()
        if top < 0:
            top = 0
        if bottom < 0:
            bottom = min(count - 1, top + _MAX_ROWS)
        return top, bottom

    def row_result(self, row: int) -> Optional[SailSeekResult]:
        return self._model.result_at(row)

    def row_pixmap(self, uid: str) -> Optional[QPixmap]:
        return self._model.pixmap(uid)

    def set_row_pixmap(self, uid: str, pixmap: QPixmap) -> None:
        self._model.set_pixmap(uid, pixmap, self.devicePixelRatioF())


    def _background_source(self) -> str:
        try:
            if settings.background_image_enabled and settings.background_image_path:
                return settings.background_image_path
            from src.core.theme import theme_background
            preset = getattr(settings, 'theme_preset', None)
            info = theme_background(preset) if preset else None
            if info:
                return info[0] or ''
        except Exception:
            logger.debug('Could not resolve SailSeek background', exc_info=True)
        return ''

    def update_background(self) -> None:
        path = self._background_source()
        key = (path, os_path_mtime(path))
        if key == self._background_key:
            return
        self._background_key = key
        self._background_pixmap = None
        if not path:
            return
        try:
            reader = QImageReader(path)
            reader.setAutoTransform(True)
            size = QSize(_W, self._max_height() - 2 * _SHADOW)
            source_size = reader.size()
            if source_size.isValid():
                reader.setScaledSize(source_size.scaled(size, Qt.AspectRatioMode.KeepAspectRatioByExpanding))
            image = reader.read()
            if not image.isNull():
                pm = QPixmap.fromImage(image)
                sx = max(0, (pm.width() - size.width()) // 2)
                sy = max(0, (pm.height() - size.height()) // 2)
                self._background_pixmap = pm.copy(sx, sy, size.width(), size.height())
        except Exception:
            logger.debug('Could not load SailSeek background %s', path, exc_info=True)


    def _paint_card(self, painter: QPainter, rect: QRect, fill: QColor, src_y: int,
                    opaque: bool) -> None:
        path = QPainterPath()
        path.addRoundedRect(QRectF(rect).adjusted(0.5, 0.5, -0.5, -0.5), 9, 9)
        painter.fillPath(path, fill)
        pm = self._background_pixmap
        if pm is not None and not pm.isNull():
            painter.save()
            painter.setClipPath(path)
            painter.setOpacity(0.5 if not opaque else 0.75)
            painter.drawPixmap(rect.topLeft(), pm, QRect(0, src_y, rect.width(), rect.height()))
            painter.setOpacity(1.0)
            scrim = _palette_color('page_bg', '#161616')
            scrim.setAlpha(150 if not opaque else 210)
            painter.fillPath(path, scrim)
            painter.restore()
        border = _palette_color('card_border', '#3a3a3a')
        border.setAlpha(200)
        painter.setPen(QPen(border, 1))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawPath(path)

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        search = self._search_rect()
        panel = self._panel_rect()
        has_panel = panel.height() >= 12
        opaque = not bool(getattr(settings, 'sailseek_translucent', True))


        painter.setPen(Qt.PenStyle.NoPen)
        for rect in ((search, panel) if has_panel else (search,)):
            for i in (12, 9, 6, 3):
                painter.setBrush(QColor(0, 0, 0, 9))
                painter.drawRoundedRect(QRectF(rect.adjusted(-i, -i + 3, i, i + 3)), 9 + i, 9 + i)

        base = _palette_color('card_bg', '#202020')
        base.setAlpha(255 if opaque or self._yt_on else 242)
        top_fill = QColor(base).lighter(118)
        top_fill.setAlpha(base.alpha())
        self._paint_card(painter, search, top_fill, 0, opaque)
        if has_panel:
            self._paint_card(painter, panel, base, _SEARCH_H + _GAP, opaque)

        if self._busy and time.monotonic() - self._busy_since > 0.15:
            phase = (time.monotonic() % 1.1) / 1.1
            phase = phase * phase * (3 - 2 * phase)
            seg = 140
            x = search.left() - seg + (search.width() + seg) * phase
            clip = QPainterPath()
            clip.addRoundedRect(QRectF(search), 9, 9)
            painter.save()
            painter.setClipPath(clip)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(_with_alpha(_palette_color('accent', '#4f7cff'), 230))
            painter.drawRoundedRect(QRectF(x, search.bottom() - 2, seg, 2), 1, 1)
            painter.restore()


class _SearchEdit(QLineEdit):
    def __init__(self, window: SailSeekWindow) -> None:
        super().__init__(window)
        self._window = window
        self.setFrame(False)
        self.setClearButtonEnabled(False)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

    def event(self, event) -> bool:
        if event.type() == QEvent.Type.KeyPress and event.key() in (Qt.Key.Key_Tab, Qt.Key.Key_Backtab):
            back = (event.key() == Qt.Key.Key_Backtab
                    or bool(event.modifiers() & Qt.KeyboardModifier.ShiftModifier))
            if self._window._yt_on:
                self._window._yt.toggle_mode()
            else:
                self._window.cycle_filter(-1 if back else 1)
            event.accept()
            return True
        return super().event(event)

    def keyPressEvent(self, event) -> None:
        key = event.key()
        mods = event.modifiers()
        ctrl = bool(mods & Qt.KeyboardModifier.ControlModifier)
        if key == Qt.Key.Key_Escape:
            self._window.hide()
            event.accept()
            return
        if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            if ctrl:
                self._window._controller.activate_secondary()
            else:
                self._window._controller.activate_current()
            event.accept()
            return
        if ctrl and int(Qt.Key.Key_1) <= key <= int(Qt.Key.Key_9):
            self._window.select_chip_index(key - int(Qt.Key.Key_1))
            event.accept()
            return
        if self._window._yt_on and key in (Qt.Key.Key_Left, Qt.Key.Key_Right, Qt.Key.Key_Up, Qt.Key.Key_Down):
            self._window._yt.step(key)
            event.accept()
            return
        moves = {Qt.Key.Key_Down: 1, Qt.Key.Key_Up: -1, Qt.Key.Key_PageDown: _MAX_ROWS - 1,
                 Qt.Key.Key_PageUp: -(_MAX_ROWS - 1)}
        if key in moves:
            self._window.move_selection(moves[key])
            event.accept()
            return
        if key in (Qt.Key.Key_Home, Qt.Key.Key_End) and not (
                mods & (Qt.KeyboardModifier.ShiftModifier | Qt.KeyboardModifier.ControlModifier)):
            self._window.select_edge(key == Qt.Key.Key_End)
            event.accept()
            return
        super().keyPressEvent(event)


if sys.platform == 'win32':
    import ctypes.wintypes as _wintypes

    _user32 = ctypes.WinDLL('user32', use_last_error=True)
    _user32.RegisterHotKey.argtypes = [_wintypes.HWND, ctypes.c_int, _wintypes.UINT, _wintypes.UINT]
    _user32.RegisterHotKey.restype = _wintypes.BOOL
    _user32.UnregisterHotKey.argtypes = [_wintypes.HWND, ctypes.c_int]
    _user32.UnregisterHotKey.restype = _wintypes.BOOL
    _user32.VkKeyScanW.argtypes = [_wintypes.WCHAR]
    _user32.VkKeyScanW.restype = ctypes.c_short
else:
    _wintypes = None
    _user32 = None


_VK_KEYS = {
    Qt.Key.Key_Space: 0x20,
    Qt.Key.Key_Escape: 0x1B,
    Qt.Key.Key_Tab: 0x09,
    Qt.Key.Key_Backtab: 0x09,
    Qt.Key.Key_Backspace: 0x08,
    Qt.Key.Key_Return: 0x0D,
    Qt.Key.Key_Enter: 0x0D,
    Qt.Key.Key_Insert: 0x2D,
    Qt.Key.Key_Delete: 0x2E,
    Qt.Key.Key_Home: 0x24,
    Qt.Key.Key_End: 0x23,
    Qt.Key.Key_PageUp: 0x21,
    Qt.Key.Key_PageDown: 0x22,
    Qt.Key.Key_Left: 0x25,
    Qt.Key.Key_Up: 0x26,
    Qt.Key.Key_Right: 0x27,
    Qt.Key.Key_Down: 0x28,
    Qt.Key.Key_CapsLock: 0x14,
    Qt.Key.Key_NumLock: 0x90,
    Qt.Key.Key_ScrollLock: 0x91,
    Qt.Key.Key_Print: 0x2C,
    Qt.Key.Key_Pause: 0x13,
}


def _vk_for_key(key: int, char: str) -> Optional[int]:
    if key in _VK_KEYS:
        return _VK_KEYS[key]
    if int(Qt.Key.Key_A) <= key <= int(Qt.Key.Key_Z):
        return key
    if int(Qt.Key.Key_0) <= key <= int(Qt.Key.Key_9):
        return key
    if int(Qt.Key.Key_F1) <= key <= int(Qt.Key.Key_F24):
        return 0x70 + (key - int(Qt.Key.Key_F1))
    if _user32 is not None and char and len(char) == 1:
        try:
            scan = int(_user32.VkKeyScanW(char))
        except Exception:
            return None
        if scan != -1:
            return scan & 0xFF
    return None


def _global_safe(sequence_text: str) -> bool:
    try:
        sequence = QKeySequence(str(sequence_text or '').strip())
        if sequence.count() != 1 or sequence.isEmpty():
            return False
        modifiers = sequence[0].keyboardModifiers()
    except Exception:
        return False
    return bool(modifiers & (Qt.KeyboardModifier.AltModifier
                             | Qt.KeyboardModifier.ShiftModifier
                             | Qt.KeyboardModifier.MetaModifier))


class _HotkeyFilter(QAbstractNativeEventFilter):
    def __init__(self, hotkey: 'GlobalHotkey') -> None:
        super().__init__()
        self._hotkey = hotkey

    def nativeEventFilter(self, event_type, message):
        if sys.platform == 'win32':
            try:
                et = bytes(event_type)
            except Exception:
                et = event_type
            if et in (b'windows_generic_MSG', b'windows_dispatcher_MSG'):
                try:
                    msg = _wintypes.MSG.from_address(int(message))
                    if msg.message == WM_HOTKEY and int(msg.wParam) == self._hotkey.hotkey_id:
                        self._hotkey.fire()
                except Exception:
                    logger.debug('Could not decode native hotkey message', exc_info=True)
        return False, 0


class GlobalHotkey(QObject):
    triggered = Signal()
    _FILTER_EVENTS = (b'windows_generic_MSG', b'windows_dispatcher_MSG')

    def __init__(self, parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self.hotkey_id = _HOTKEY_ID
        self._registered = False
        self._filter: Optional[_HotkeyFilter] = None
        self.error = ''

    @property
    def registered(self) -> bool:
        return self._registered

    def register(self, sequence_text: str) -> bool:
        self.unregister()
        self.error = ''
        if sys.platform != 'win32' or _user32 is None:
            self.error = tr('sailseek.hotkey_windows_only', 'Global shortcuts need Windows')
            return False
        sequence = QKeySequence(str(sequence_text or '').strip())
        if sequence.count() != 1 or sequence.isEmpty():
            self.error = tr('sailseek.hotkey_invalid', 'Invalid shortcut')
            return False
        combo = sequence[0]
        key = int(combo.key())
        modifiers = combo.keyboardModifiers()
        mods = 0
        if modifiers & Qt.KeyboardModifier.ControlModifier:
            mods |= MOD_CONTROL
        if modifiers & Qt.KeyboardModifier.AltModifier:
            mods |= MOD_ALT
        if modifiers & Qt.KeyboardModifier.ShiftModifier:
            mods |= MOD_SHIFT
        if modifiers & Qt.KeyboardModifier.MetaModifier:
            mods |= MOD_WIN
        char = ''
        try:
            if 0 < key < 0x110000:
                char = chr(key)
        except ValueError:
            char = ''
        vk = _vk_for_key(key, char)
        if vk is None:
            self.error = tr('sailseek.hotkey_unsupported', 'That key cannot be used as a global shortcut')
            return False
        try:
            ok = bool(_user32.RegisterHotKey(None, self.hotkey_id, mods | MOD_NOREPEAT, int(vk)))
        except Exception:
            logger.exception('RegisterHotKey failed')
            ok = False
        if not ok:
            code = ctypes.get_last_error()
            self.error = tr('sailseek.hotkey_conflict', 'Shortcut is already in use ({code})', code=code)
            return False
        self._filter = _HotkeyFilter(self)
        app = QApplication.instance()
        if app is None:
            _user32.UnregisterHotKey(None, self.hotkey_id)
            self._filter = None
            self.error = tr('sailseek.hotkey_unavailable', 'Shortcut could not be registered')
            return False
        app.installNativeEventFilter(self._filter)
        self._registered = True
        return True

    def unregister(self) -> None:
        app = QApplication.instance()
        if self._filter is not None and app is not None:
            try:
                app.removeNativeEventFilter(self._filter)
            except Exception:
                logger.debug('Could not remove the SailSeek hotkey filter', exc_info=True)
        self._filter = None
        if self._registered and _user32 is not None:
            try:
                _user32.UnregisterHotKey(None, self.hotkey_id)
            except Exception:
                logger.debug('Could not unregister the SailSeek hotkey', exc_info=True)
        self._registered = False

    def fire(self) -> None:
        self.triggered.emit()


class SailSeekController(QObject):
    status_changed = Signal(str)

    def __init__(self, main_window) -> None:
        super().__init__(main_window)
        self._mw = main_window
        self._window: Optional[SailSeekWindow] = None
        self._hotkey = GlobalHotkey(self)
        self._hotkey.triggered.connect(self.toggle)
        self._fallback: Optional[QShortcut] = None
        self._suspended = False
        self._status = ''
        self._query = ''
        self._generation = 0
        self._debounce = QTimer(self)
        self._debounce.setSingleShot(True)
        self._debounce.setInterval(_DEBOUNCE_MS)
        self._debounce.timeout.connect(self._start_search)
        self._live = QTimer(self)
        self._live.setInterval(700)
        self._live.timeout.connect(self._refresh_downloads)
        self._deadline = QTimer(self)
        self._deadline.setSingleShot(True)
        self._deadline.setInterval(_SEARCH_DEADLINE_MS)
        self._deadline.timeout.connect(self._on_deadline)
        self._pending = 0
        self._busy = False
        self._online_error = False
        self._online_ok = False
        self._online_failed = False
        self._last_net = 0.0
        self._filter = 'all'
        self._chip_filter = 'all'
        self._prefix_filter: Optional[str] = None
        self._missing: list[tuple[str, Any]] = []
        self._avail: Optional[list[str]] = None
        self._closed = False
        self._fast_results: list[SailSeekResult] = []
        self._rom_results: list[SailSeekResult] = []
        self._online_results: dict[str, list[SailSeekResult]] = {}
        self._game_results: dict[str, list[SailSeekResult]] = {}
        self._cache: 'OrderedDict[tuple[str, str], tuple[float, Any]]' = OrderedDict()
        self._art_requested: set[str] = set()
        self._art_gen = -1
        self._last_status = ''
        self._yt_url = ''
        self._yt_rows: list[SailSeekResult] = []
        self._yt_error = ''
        self._yt_info: Optional[dict] = None
        register_locale_refresh(self, self._on_locale_changed)
        self.register_shortcut()
        QTimer.singleShot(1500, self._prewarm)


    @property
    def window(self) -> SailSeekWindow:
        if self._window is None:
            self._window = SailSeekWindow(self)
        return self._window

    def _prewarm(self) -> None:
        if self._closed or self._window is not None:
            return
        try:
            _ = self.window
        except Exception:
            logger.debug('SailSeek prewarm failed', exc_info=True)

    def status_text(self) -> str:
        return self._status

    def _set_status(self, text: str) -> None:
        if text != self._status:
            self._status = text
            self.status_changed.emit(text)

    def register_shortcut(self) -> None:
        self._teardown_shortcut()
        if not bool(getattr(settings, 'sailseek_enabled', True)):
            self._set_status(tr('sailseek.status_disabled', 'SailSeek is disabled'))
            return
        if self._suspended:
            self._set_status(tr('sailseek.status_suspended', 'Shortcut paused while assigning keys'))
            return
        try:
            from .settings_page import shortcut_map
            key = str(shortcut_map().get('sailseek', 'Ctrl+Shift+Space'))
        except Exception:
            key = 'Ctrl+Shift+Space'
        if _global_safe(key):
            registered = self._hotkey.register(key)
            error = '' if registered else (
                self._hotkey.error or tr('sailseek.hotkey_unavailable', 'Shortcut could not be registered'))
        else:
            registered = False
            error = tr('sailseek.hotkey_needs_modifier',
                       'Add Shift, Alt, or Win so this shortcut works everywhere')
        if registered:
            self._set_status(tr('sailseek.status_global', 'Shortcut active: {shortcut}', shortcut=key))
            return
        try:
            shortcut = QShortcut(QKeySequence(key), self._mw)
            shortcut.setContext(Qt.ShortcutContext.ApplicationShortcut)
            shortcut.activated.connect(self.toggle)
            self._fallback = shortcut
            self._set_status(tr('sailseek.status_window', 'In-app only: {error}', error=error))
        except Exception:
            logger.debug('SailSeek fallback shortcut could not be created', exc_info=True)
            self._set_status(tr('sailseek.status_invalid', 'Shortcut unavailable: {error}', error=error))

    def _teardown_shortcut(self) -> None:
        self._hotkey.unregister()
        shortcut, self._fallback = self._fallback, None
        if shortcut is not None:
            try:
                shortcut.setEnabled(False)
                shortcut.deleteLater()
            except Exception:
                pass

    def set_suspended(self, suspended: bool) -> None:
        suspended = bool(suspended)
        if suspended == self._suspended:
            return
        self._suspended = suspended
        self.register_shortcut()

    def apply_settings(self) -> None:
        self._avail = None
        if not bool(getattr(settings, 'sailseek_enabled', True)):
            self.hide()
        self.register_shortcut()
        if self._window is not None:
            self._window.refresh_theme()
            if self._query and self._window.isVisible():
                self._start_search()

    def reset_geometry(self) -> None:
        self.window.reset_geometry()

    def _on_locale_changed(self) -> None:
        if self._window is not None:
            self._window.apply_locale()


    def toggle(self) -> None:
        if self._suspended:
            return
        if not bool(getattr(settings, 'sailseek_enabled', True)):
            return
        if QApplication.activeModalWidget() is not None:
            return
        if self.window.isVisible():
            self.hide()
        else:
            self.show()

    def show(self) -> None:
        if self._suspended or not bool(getattr(settings, 'sailseek_enabled', True)):
            return
        self._avail = None
        self._chip_filter = 'all'
        self._prefix_filter = None
        self._filter = 'all'
        window = self.window
        window.reset_for_show()
        self._query = ''
        self._generation += 1
        self._pending = 0
        self._missing = []
        self._online_error = False
        self._online_ok = False
        self._online_failed = False
        self._art_requested.clear()
        self._art_gen = self._generation
        self._show_quick_actions()
        window.show()
        window.raise_()
        window.activateWindow()
        self._live.start()
        QTimer.singleShot(0, self._focus_window)

    def _focus_window(self) -> None:
        window = self._window
        if window is None or not window.isVisible():
            return
        try:
            window.raise_()
            window.activateWindow()
            window._edit.setFocus(Qt.FocusReason.ShortcutFocusReason)
        except Exception:
            logger.debug('Could not focus SailSeek', exc_info=True)

    def hide(self) -> None:
        window = self._window
        if window is None:
            return
        window.set_busy(False)
        window.hide()
        self._live.stop()
        self._debounce.stop()
        self._deadline.stop()
        self._generation += 1
        self._pending = 0
        self._busy = False
        self._yt_url, self._yt_info = '', None
        self._art_requested.clear()
        try:
            worker.cancel_owner(self)
            images.release(self)
        except Exception:
            logger.debug('Could not release SailSeek search resources', exc_info=True)

    def shutdown(self) -> None:
        self._closed = True
        self.hide()
        self._teardown_shortcut()
        window, self._window = self._window, None
        if window is not None:
            try:
                window.close()
                window.deleteLater()
            except Exception:
                pass


    def _available(self) -> list[str]:
        if self._avail is None:
            self._avail = self.available_categories()
        return self._avail

    def _parse_prefix(self, text: str) -> tuple[Optional[str], str]:
        m = _PREFIX_RE.match(text or '')
        if m:
            key = _PREFIXES.get(m.group(1).lower())
            if key and key in self._available():
                return key, m.group(2)
        return None, text

    def strip_prefix(self, text: str) -> Optional[str]:
        prefix, rest = self._parse_prefix(text)
        return rest if prefix else None

    def _sync_filter(self) -> None:
        self._filter = self._prefix_filter or self._chip_filter
        window = self._window
        if window is not None:
            window.set_active_filter(self._filter)

    @staticmethod
    def _youtube_url(text: str) -> str:
        url = (text or '').strip()
        if 'youtu' not in url:
            return ''
        from src.core.yt import provider
        return url if provider.is_valid_url(url) else ''

    def _clear_youtube(self) -> None:
        self._yt_url, self._yt_rows, self._yt_error, self._yt_info = '', [], '', None
        if self._window is not None:
            self._window.set_youtube(None)

    def _fetch_youtube(self, url: str) -> None:
        if url == self._yt_url:
            return
        self._clear_youtube()
        self._yt_url = url
        self._query, self._missing, self._pending = url, [], 0
        self._generation += 1
        self._deadline.stop()
        with suppress(Exception):
            worker.cancel_owner(self)
            images.release(self)
        self._busy = True
        self._apply_results()
        self._debounce.start(250)

    def _run_youtube(self) -> None:
        from src.core.yt import provider
        url = self._yt_url
        if not provider.has_yt_tools():
            self._busy = False
            self._yt_rows = [self._youtube_tools_row()]
            self._apply_results()
            return
        fetch = provider.fetch_playlist if provider.is_playlist_url(url) else provider.fetch_info
        worker.submit(fetch, args=(url,), on_done=lambda data: self._on_youtube(url, data),
                      on_error=lambda error: self._on_youtube(url, None, str(error)),
                      owner=self, priority=Priority.HIGH)

    def _on_youtube(self, url: str, data, error: str = '') -> None:
        if url != self._yt_url:
            return
        self._busy = False
        self._yt_error = error
        self._yt_info = data or None
        window = self._window
        if data and window is not None:
            window.set_youtube(data)
            thumb = data.get('thumbnail')
            if thumb:
                with suppress(Exception):
                    images.get(thumb, QSize(256, 144), lambda pixmap: self._on_youtube_thumb(url, pixmap),
                               owner=self, priority=Priority.VISIBLE)
        self._apply_results()

    def _on_youtube_thumb(self, url: str, pixmap: QPixmap) -> None:
        if url == self._yt_url and self._window is not None:
            self._window._yt.set_thumb(pixmap)

    def _youtube_tools_row(self) -> SailSeekResult:
        ready = getattr(self._mw, 'yt_page', None) is not None
        return SailSeekResult(
            uid='yt:tools', category='youtube', title=tr('sailseek.yt_tools', 'YouTube tools required'),
            subtitle=tr('sailseek.yt_tools_open', 'Open the YouTube section to install them') if ready
            else tr('sailseek.yt_tools_off', 'Enable the YouTube section in Settings first'),
            action=self._open_youtube if ready else None)

    def _open_youtube(self) -> None:
        self._restore_main_window()
        page = getattr(self._mw, 'yt_page', None)
        if page is not None:
            self._mw.switchTo(page)

    def _youtube_download(self) -> None:
        info, window = self._yt_info, self._window
        if not info or window is None or window._yt.done:
            return
        mode, quality, subtitle = window._yt.selection()
        bridge = self._mw._ensure_yt_bridge()
        if 'entries' in info:
            for entry in info['entries']:
                bridge.download({'url': entry['url'], 'title': entry['title']}, mode,
                                max_height=quality if mode == 'video' else 0, subdir=info['title'])
        else:
            bridge.download(info, mode, quality, subtitle=subtitle)
        window._yt.done = True
        window._yt.update()
        QTimer.singleShot(700, self.hide)

    def on_text_changed(self, text: str) -> None:
        url = self._youtube_url(text)
        if url:
            self._fetch_youtube(url)
            return
        if self._yt_url:
            self._clear_youtube()
            self._busy = False
        prefix, rest = self._parse_prefix(text)
        self._prefix_filter = prefix
        self._query = _normalize_query(rest)
        self._debounce.stop()
        self._sync_filter()
        if not self._query:
            self._generation += 1
            self._pending = 0
            self._missing = []
            self._busy = False
            self._deadline.stop()
            try:
                worker.cancel_owner(self)
            except Exception:
                pass
            self._show_quick_actions()
            return


        self._prepare(self._query)
        if self._missing:
            self._arm_debounce()

    def _arm_debounce(self) -> None:
        try:
            delay = int(getattr(settings, 'sailseek_delay_ms', _DEBOUNCE_MS))
        except (TypeError, ValueError):
            delay = _DEBOUNCE_MS
        self._debounce.start(max(_DELAY_MIN_MS, min(_DELAY_MAX_MS, delay)))

    def _refresh_downloads(self) -> None:
        window = self._window
        if window is None or not window.isVisible() or self._yt_url:
            return
        if self._filter not in ('all', 'downloads') or not self._category_available('downloads'):
            return
        manager = getattr(self._mw, 'download_manager', None)
        if manager is None:
            return
        try:
            items = manager.items_in_order()
            active = any(str(getattr(item.state, 'value', item.state)).lower()
                         in ('downloading', 'verifying', 'seeding') for item in items)
        except Exception:
            return
        if not active:
            return
        downloads = self._download_results(self._query)
        self._fast_results = [r for r in self._fast_results if r.category != 'downloads'] + downloads
        self._apply_results()

    def _on_deadline(self) -> None:
        if self._pending > 0:
            self._pending = 0
            self._busy = False
            self._online_failed = True
            self._online_error = not self._online_ok
            self._apply_results()
            self._set_busy_status()

    @staticmethod
    def _online_ready(query: str) -> bool:
        return len(query) >= 3 or any(ord(ch) > 127 for ch in query)

    def _wanted(self, key: str) -> bool:
        return self._filter in ('all', key) and self._category_available(key)

    def _rom_limit(self) -> int:
        return 25 if self._filter == 'roms' else 8

    def _prepare(self, query: str) -> None:
        if self._yt_url:
            return
        self._fast_results = self._collect_fast_local(query)
        self._rom_results = []
        self._online_results = {}
        self._game_results = {}
        self._online_error = False
        self._online_ok = False
        self._online_failed = False
        missing: list[tuple[str, Any]] = []

        if self._wanted('roms'):
            limit = self._rom_limit()
            cached = self._cache_get(f'roms:{limit}', query)
            if cached is not None:
                self._rom_results = self._map_roms(cached)
            else:
                missing.append(('roms', limit))
        online = self._online_ready(query)
        if online and self._wanted('games'):
            for source in _GAME_SOURCES:
                cached = self._cache_get(f'games:{source}', query)
                if cached is not None:
                    self._game_results[source] = self._map_repack_entries(
                        cached, query, self._per_source_limit())
                    self._online_ok = True
                else:
                    missing.append(('games', source))
        for category in _ONLINE_CATEGORIES:
            if not online or not self._wanted(category):
                continue
            cached = self._cache_get(category, query)
            if cached is not None:
                self._online_results[category] = self._map_online(category, cached)
                self._online_ok = True
            else:
                missing.append(('online', category))
        self._missing = missing
        self._busy = bool(missing)
        self._apply_results()
        self._set_busy_status()

    def _start_search(self, force: bool = False) -> None:
        if self._yt_url:
            self._run_youtube()
            return
        query = self._query
        if not query:
            self._show_quick_actions()
            return
        if not force and any(kind != 'roms' for kind, _key in self._missing):
            wait = _MIN_NET_GAP - (time.monotonic() - self._last_net)
            if wait > 0:
                self._debounce.start(int(wait * 1000) + 1)
                return
        self._generation += 1
        gen = self._generation
        self._deadline.stop()
        try:
            worker.cancel_owner(self)
            images.release(self)
        except Exception:
            logger.debug('Could not cancel the previous SailSeek search', exc_info=True)
        self._art_requested.clear()
        self._art_gen = gen
        missing = list(self._missing)
        self._pending = len(missing)
        if not missing:
            self._busy = False
            self._set_busy_status()
            QTimer.singleShot(0, self.request_visible_artwork)
            return
        self._busy = True
        if any(kind != 'roms' for kind, _key in missing):
            self._last_net = time.monotonic()
        for kind, key in missing:
            if kind == 'roms':
                worker.submit(
                    self._fetch_roms, args=(query, key),
                    on_done=lambda rows, g=gen, q=query, k=key: self._on_roms(rows, g, q, k),
                    on_error=lambda error, g=gen: self._on_rom_error(error, g),
                    owner=self, priority=Priority.HIGH,
                )
            elif kind == 'games':
                self._submit_game(key, query, gen)
            else:
                self._submit_online(key, query, gen)
        self._set_busy_status()
        self._deadline.start()
        QTimer.singleShot(0, self.request_visible_artwork)

    def _set_busy_status(self) -> None:
        window = self._window
        if window is None:
            return
        window.set_busy(self._busy)
        window.set_placeholder(self._empty_placeholder())
        window.set_status(self._status_line())

    def _show_quick_actions(self) -> None:
        self._busy = False
        self._pending = 0
        self._missing = []
        self._online_error = False
        self._online_ok = False
        self._online_failed = False
        self._fast_results = self._quick_results()
        self._rom_results = []
        self._online_results = {}
        self._game_results = {}
        self._art_requested.clear()
        self._art_gen = self._generation
        self._apply_results()
        window = self._window
        if window is not None:
            window.set_busy(False)

    def _apply_results(self) -> None:
        window = self._window
        if window is None:
            return
        results = self._compose_results()
        panel = bool(results) or bool(self._query) or self._filter != 'all'
        window.set_busy(self._busy)
        window.set_results(results, panel)
        window.set_placeholder(self._empty_placeholder())
        window.set_status(self._status_line())

    def _compose_results(self) -> list[SailSeekResult]:
        if self._yt_url:
            return list(self._yt_rows)
        results = list(self._fast_results)
        results.extend(self._rom_results)
        for category in _GAME_SOURCES:
            results.extend(self._game_results.get(category, ()))
        for category in _ONLINE_CATEGORIES:
            results.extend(self._online_results.get(category, ()))
        if self._filter != 'all':
            results = [r for r in results if r.category == self._filter]
        results = self._dedupe(results)
        results.sort(key=lambda r: (_CATEGORY_ORDER.index(r.category)
                                    if r.category in _CATEGORY_ORDER else 99,
                                    r.score, r.title.casefold()))
        try:
            limit = max(5, min(100, int(getattr(settings, 'sailseek_max_results', 30))))
        except (TypeError, ValueError):
            limit = 30
        if self._filter == 'all' and len(results) > limit:
            buckets: dict[str, list[SailSeekResult]] = {}
            for r in results:
                buckets.setdefault(r.category, []).append(r)
            quota = max(3, limit // max(1, len(buckets)))
            picked = {id(r) for items in buckets.values() for r in items[:quota]}
            for r in results:
                if len(picked) >= limit:
                    break
                picked.add(id(r))
            results = [r for r in results if id(r) in picked]
        return results[:limit]

    def _dedupe(self, results: list[SailSeekResult]) -> list[SailSeekResult]:
        seen: set[str] = set()
        kept: list[SailSeekResult] = []
        for result in results:
            if not result or not result.title:
                continue
            key = result.uid or result.title.casefold()
            if key in seen:
                continue
            seen.add(key)
            kept.append(result)
        return kept

    def _status_line(self) -> str:
        window = self._window
        if window is None:
            return ''
        if self._busy:
            return tr('sailseek.searching', 'Searching…')
        count = window._model.rowCount()
        if count:
            return tr('sailseek.results', '{count} results', count=count)
        if self._yt_url:
            return ''
        if self._online_error:
            return tr('sailseek.online_unavailable', 'Online search unavailable')
        if self._query:
            return tr('sailseek.no_results', 'No matches for "{query}"', query=self._query)
        return ''

    def _empty_placeholder(self) -> str:
        if self._yt_url:
            if self._yt_error:
                return tr('sailseek.yt_failed', 'Could not load video: {error}', error=self._yt_error)
            return tr('sailseek.yt_fetching', 'Fetching video…')
        if self._busy:
            return tr('sailseek.searching', 'Searching…')
        if self._query:
            if self._online_error:
                return tr('sailseek.online_unavailable', 'Online search unavailable')
            if not self._online_ready(self._query):
                return tr('sailseek.keep_typing', 'Keep typing to search online…')
            return tr('sailseek.no_results', 'No matches for "{query}"', query=self._query)
        if self._filter != 'all':
            return tr('sailseek.hint_filter', 'Type to search {category}',
                      category=_category_label(self._filter))
        return tr('sailseek.hint',
                  'Search PC Games, movies, TV, anime, manga, books and downloads.')

    def _online_status(self) -> str:
        if self._online_error:
            return tr('sailseek.online_unavailable', 'Online search unavailable')
        return tr('sailseek.local_online', 'Local + online')

    def set_filter(self, key: str, text: Optional[str] = None) -> None:
        self._chip_filter = key or 'all'
        if text is not None:
            self._prefix_filter = None
            self._query = _normalize_query(text)
        self._sync_filter()
        self._debounce.stop()
        if not self._query:
            self._show_quick_actions()
            return
        self._prepare(self._query)
        if self._missing:
            self._start_search(force=True)

    def relocalize(self) -> None:
        if not self._query:
            self._show_quick_actions()
        else:
            self._prepare(self._query)
            if self._missing:
                self._arm_debounce()


    def _category_enabled(self, key: str) -> bool:
        raw = getattr(settings, 'sailseek_categories', None) or {}
        try:
            return bool(raw.get(key, True))
        except Exception:
            return True

    def _category_available(self, key: str) -> bool:
        if not self._category_enabled(key):
            return False
        try:
            from src.core.config import is_tab_enabled
            if key in ('movies', 'tv', 'anime'):
                tab = {'movies': 'movies', 'tv': 'series', 'anime': 'anime'}[key]
                return bool(getattr(settings, 'tv_enabled', True)) and is_tab_enabled('tv', tab)
            if key == 'manga':
                return bool(getattr(settings, 'books_enabled', True)) and is_tab_enabled('books', 'manga')
            if key == 'books':
                return bool(getattr(settings, 'books_enabled', True)) and is_tab_enabled('books', 'books')
            if key == 'games':
                return bool(getattr(settings, 'pc_games_enabled', True))
            if key == 'roms':
                return bool(getattr(settings, 'minerva_enabled', True))
        except Exception:
            return False
        return True

    def available_categories(self) -> list[str]:
        keys = []
        for key in _CATEGORY_ORDER:
            if self._category_available(key):
                keys.append(key)
        return keys


    def _quick_results(self) -> list[SailSeekResult]:
        results: list[SailSeekResult] = []
        if self._filter in ('all', 'downloads') and self._category_available('downloads'):
            results.extend(self._download_results('')[:8])
        elif self._filter == 'games' and self._category_available('games'):
            results.extend(self._bookmark_game_results('')[:12])
        return self._dedupe(results)

    def _collect_fast_local(self, query: str) -> list[SailSeekResult]:
        results: list[SailSeekResult] = []
        if query.strip().casefold() in ('rickroll', 'rick astley', 'never gonna give you up', 'dqw4w9wgxcq'):
            results.append(SailSeekResult(
                uid='egg:rickroll', category='youtube',
                title='Never Gonna Give You Up', subtitle='Rick Astley · 1987', score=0,
                action=lambda: self._open_url(_RICKROLL_URL),
            ))
        if self._filter in ('all', 'downloads') and self._category_available('downloads'):
            results.extend(self._download_results(query))
        if self._filter in ('all', 'games') and self._category_available('games'):
            results.extend(self._bookmark_game_results(query))
        return results

    def _download_results(self, query: str) -> list[SailSeekResult]:
        manager = getattr(self._mw, 'download_manager', None)
        if manager is None:
            return []
        try:
            from src.core.downloader import (DLState, human_bytes, human_duration,
                                             human_speed)
            items = manager.items_in_order()
        except Exception:
            return []
        if not query:
            items = sorted(
                items,
                key=lambda item: _DOWNLOAD_RANK.get(
                    str(getattr(item.state, 'value', item.state)).lower(), 9))
        results = []
        for item in items:
            title = item.game_name or tr('sailseek.untitled', 'Untitled')
            category = str(getattr(item, 'category', '') or '')
            source = str(getattr(item, 'source', '') or '')
            if category == 'repacks' or source.casefold() in ('fitgirl', 'gog'):
                title = _clean_game_title(title)
            try:
                state = item.state if isinstance(item.state, DLState) else DLState(item.state)
            except Exception:
                state = item.state
            state_name = str(getattr(state, 'value', state)).lower()
            state_label = tr(f'download.state_{state_name}',
                             str(getattr(state, 'value', state)).replace('_', ' ').title())
            pct = 100.0 if state in (DLState.seeding, DLState.completed) else max(
                0.0, min(100.0, float(item.progress or 0)))
            parts = [state_label]
            if state in (DLState.downloading, DLState.verifying):
                parts.append(f'{pct:.0f}%')
                if item.speed_down_kbps > 0:
                    parts.append(human_speed(item.speed_down_kbps))
                if item.eta >= 0:
                    parts.append(tr('download.eta_left', eta=human_duration(item.eta)))
            elif state == DLState.paused:
                parts.append(f'{pct:.0f}%')
            elif state == DLState.seeding:
                if item.speed_up_kbps > 0:
                    parts.append(f'↑ {human_speed(item.speed_up_kbps)}')
                if item.ratio:
                    parts.append(f'{item.ratio:.2f}')
            elif state == DLState.error and item.error:
                parts.append(item.error)
            if item.total_bytes > 0:
                parts.append(human_bytes(item.total_bytes))
            elif item.downloaded_bytes > 0:
                parts.append(human_bytes(item.downloaded_bytes))
            if item.source:
                parts.append(item.source)
            subtitle = ' · '.join(str(x) for x in parts if x)
            score = _match_score(
                query, title,
                f'{item.console} {item.source} {state_label} {item.category} {item.file_name}')
            if score is None:
                continue
            item_id = item.id
            show_progress = state not in (DLState.completed, DLState.cancelled, DLState.error)
            results.append(SailSeekResult(
                uid=f'download:{item_id}', category='downloads', title=title,
                subtitle=subtitle, image='', score=score,
                action=lambda i=item_id: self._open_download(i),
                secondary=[(tr('sailseek.action_open_folder', 'Open folder'),
                            lambda i=item_id: self._open_download_folder(i))],
                progress=pct if show_progress else None,
            ))
        return results

    def _bookmark_game_results(self, query: str) -> list[SailSeekResult]:
        try:
            from .repacks_page import bookmark_store
            store = bookmark_store()
            raw_entries = store.all()
        except Exception:
            return []
        entries = []
        for raw in raw_entries:
            try:
                entries.append(store.to_entry(raw))
            except Exception:
                continue
        return self._map_repack_entries(entries, query)

    def _map_repack_entries(self, entries, query: str,
                            limit: Optional[int] = None) -> list[SailSeekResult]:
        results = self._map_repack_all(entries, query)
        if limit:
            results.sort(key=lambda r: (r.score, r.title.casefold()))
            results = results[:limit]
        return results

    def _map_repack_all(self, entries, query: str) -> list[SailSeekResult]:
        results = []
        try:
            from .repacks_page import _decorate_repack_entry
        except Exception:
            _decorate_repack_entry = None
        for entry in entries or []:
            if _decorate_repack_entry is not None:
                try:
                    _decorate_repack_entry(entry)
                except Exception:
                    pass
            raw_title = str(getattr(entry, 'title', '') or '')
            if not raw_title:
                continue
            title = _clean_game_title(str(getattr(entry, 'display_title', '') or raw_title))
            source = str(getattr(entry, 'source', '') or '')
            source_label = {'fitgirl': 'FitGirl', 'gog': 'GOG'}.get(source, source.title())
            meta = str(getattr(entry, 'title_meta', '') or '')
            search_blob = raw_title
            try:
                from src.core.repacks import enrichment as service

                search_blob = service.search_text_for(entry) or raw_title
            except Exception:
                pass
            score = _match_score(query, title, f'{meta} {search_blob} {source_label}')
            if score is None:
                continue
            url = str(getattr(entry, 'url', '') or '')
            image = str(getattr(entry, 'poster_url', '') or getattr(entry, 'poster_path', '')
                        or getattr(entry, 'steam_poster', '') or '')
            subtitle = f'{source_label} · {tr("sailseek.type_pc_game", "PC game")}'
            if meta:
                subtitle = f'{subtitle} · {meta}'
            results.append(SailSeekResult(
                uid=f'repack:{source}:{url or title}', category='games', title=title,
                subtitle=subtitle,
                image=image, score=score, action=lambda e=entry: self._open_repack(e),
                secondary=[(tr('sailseek.action_open_source', 'Open source page'),
                            lambda u=url: self._open_url(u))],
            ))
        return results


    def _per_source_limit(self) -> int:
        try:
            limit = int(getattr(settings, 'sailseek_max_results', 30))
        except (TypeError, ValueError):
            limit = 30
        if self._filter != 'all':
            return max(10, min(25, limit))
        return max(3, min(8, limit // 3))

    @staticmethod
    def _fetch_tmdb_movies(query: str) -> list[dict]:
        from src.core.tv import tmdb
        data = tmdb.search('movie', query, 1)
        return [item for item in (data.get('results') or [])
                if not item.get('adult') and (item.get('title') or item.get('name'))]

    @staticmethod
    def _fetch_tmdb_tv(query: str) -> list[dict]:
        from src.core.tv import tmdb
        data = tmdb.search('tv', query, 1)
        return [item for item in (data.get('results') or [])
                if not item.get('adult') and (item.get('title') or item.get('name'))]

    @staticmethod
    def _fetch_manga(query: str) -> list:
        from src.core.books.manga import manager as manga_manager
        result = manga_manager.search_all(query, 1)
        return list(getattr(result, 'entries', None) or [])

    @staticmethod
    def _fetch_books(query: str) -> list:
        from src.core.books import manager as books_manager
        page = books_manager.search_all(query, 1)
        return list(getattr(page, 'entries', None) or [])

    @staticmethod
    def _fetch_roms(query: str, limit: int = 8) -> list[dict]:
        from src.core.minerva import database as db
        return db.search_roms(query=query, limit=limit)

    def _submit_game(self, source: str, query: str, generation: int) -> None:
        on_done = lambda page, s=source, g=generation, q=query: self._on_game_search(s, page, g, q)
        on_error = lambda error, s=source, g=generation, q=query: self._on_game_search(
            s, None, g, q, error)
        try:
            worker.fetch_search_async(
                source, query, 1, on_done=on_done, on_error=on_error,
                use_cache=True, owner=self)
        except Exception:
            logger.exception('Could not start SailSeek %s search', source)
            self._on_game_search(source, None, generation, query, RuntimeError('search did not start'))

    def _on_game_search(self, source: str, page, generation: int, query: str,
                        error=None) -> None:
        if generation != self._generation or query != self._query:
            return
        if self._pending > 0:
            self._pending -= 1
        if error is not None or page is None:
            self._online_failed = True
            logger.debug('SailSeek %s search failed: %s', source, error)
            self._game_results.pop(source, None)
        else:
            entries = list(getattr(page, 'entries', None) or [])
            self._game_results[source] = self._map_repack_entries(
                entries, query, self._per_source_limit())
            self._cache_put(f'games:{source}', query, entries)
            self._online_ok = True
        self._online_error = self._online_failed and not self._online_ok
        self._apply_results()
        if self._pending <= 0:
            self._deadline.stop()
            self._busy = False
        self._set_busy_status()

    def _submit_online(self, category: str, query: str, generation: int) -> None:
        on_done = lambda data, c=category, g=generation, q=query: self._on_online(c, data, g, q)
        on_error = lambda error, c=category, g=generation, q=query: self._on_online(
            c, None, g, q, error)
        try:
            if category == 'movies':
                worker.submit(self._fetch_tmdb_movies, args=(query,), on_done=on_done,
                              on_error=on_error, owner=self, priority=Priority.NORMAL)
            elif category == 'tv':
                worker.submit(self._fetch_tmdb_tv, args=(query,), on_done=on_done,
                              on_error=on_error, owner=self, priority=Priority.NORMAL)
            elif category == 'anime':
                from src.core.tv.anime import anime_backend
                worker.submit_coro(anime_backend.search_cached, args=(query, self._per_source_limit()),
                                   on_done=on_done, on_error=on_error, owner=self,
                                   priority=Priority.NORMAL)
            elif category == 'manga':
                worker.submit(self._fetch_manga, args=(query,), on_done=on_done,
                              on_error=on_error, owner=self, priority=Priority.NORMAL)
            elif category == 'books':
                worker.submit(self._fetch_books, args=(query,), on_done=on_done,
                              on_error=on_error, owner=self, priority=Priority.NORMAL)
        except Exception:
            logger.exception('Could not start SailSeek %s search', category)
            self._on_online(category, None, generation, query, RuntimeError('search did not start'))

    def _on_online(self, category: str, data, generation: int, query: str, error=None) -> None:
        if generation != self._generation or query != self._query:
            return
        if self._pending > 0:
            self._pending -= 1
        if error is not None or data is None:
            self._online_failed = True
            logger.debug('SailSeek %s search failed: %s', category, error)
            self._online_results.pop(category, None)
        else:
            self._online_ok = True
            self._online_results[category] = self._map_online(category, data)
            self._cache_put(category, query, data)
        self._online_error = self._online_failed and not self._online_ok
        self._apply_results()
        if self._pending <= 0:
            self._deadline.stop()
            self._busy = False
        self._set_busy_status()

    def _on_roms(self, rows, generation: int, query: str = '', limit: int = 8) -> None:
        if generation != self._generation or (query and query != self._query):
            return
        if self._pending > 0:
            self._pending -= 1
        if rows is not None and query:
            self._cache_put(f'roms:{limit}', query, rows)
        self._rom_results = self._map_roms(rows or [])
        self._apply_results()
        if self._pending <= 0:
            self._deadline.stop()
            self._busy = False
        self._set_busy_status()

    def _on_rom_error(self, error, generation: int) -> None:
        if generation != self._generation:
            return
        if self._pending > 0:
            self._pending -= 1
        logger.debug('SailSeek ROM search failed: %s', error)
        self._rom_results = []
        self._apply_results()
        if self._pending <= 0:
            self._deadline.stop()
            self._busy = False
        self._set_busy_status()

    def _map_online(self, category: str, data) -> list[SailSeekResult]:
        limit = self._per_source_limit()
        results: list[SailSeekResult] = []
        try:
            if category in ('movies', 'tv'):
                results = self._map_tmdb(category, data)[:limit]
            elif category == 'anime':
                results = self._map_anime(data)[:limit]
            elif category == 'manga':
                results = self._map_manga(data)[:limit]
            elif category == 'books':
                results = self._map_books(data)[:limit]
        except Exception:
            logger.exception('Could not map SailSeek %s results', category)
            return []
        results.sort(key=lambda r: (r.score, r.title.casefold()))
        return results

    def _map_tmdb(self, category: str, data) -> list[SailSeekResult]:
        from src.core.tv import tmdb
        results = []
        for item in data or []:
            title = item.get('title') or item.get('name') or ''
            year = _format_year(item.get('release_date') or item.get('first_air_date'))
            rating = item.get('vote_average') or 0
            meta = [ _type_label('movie' if category == 'movies' else 'tv'), year,
                     f'{rating:.1f}' if rating else '', 'TMDB']
            subtitle = ' · '.join(str(x) for x in meta if x)
            media_kind = 'movie' if category == 'movies' else 'tv'
            uid_id = item.get('id')
            poster = tmdb.image_url(item.get('poster_path'), 'w185') or ''
            results.append(SailSeekResult(
                uid=f'tmdb:{media_kind}:{uid_id}', category=category, title=title,
                subtitle=subtitle, image=poster, score=2,
                action=lambda raw=dict(item), kind=media_kind: self._open_tv_detail(raw, kind),
            ))
        return results

    def _map_anime(self, data) -> list[SailSeekResult]:
        results = []
        for media in data or []:
            title_obj = media.get('title') or {}
            title = (title_obj.get('english') or title_obj.get('romaji')
                     or title_obj.get('native') or media.get('name') or '')
            if not title:
                continue
            year = _format_year(media.get('seasonYear'))
            fmt = str(media.get('format') or '').replace('_', ' ').title()
            subtitle = ' · '.join(str(x) for x in (_type_label('anime'), year, fmt, 'AniList') if x)
            cover = media.get('coverImage') or {}
            if isinstance(cover, dict):
                cover = cover.get('large') or cover.get('medium') or ''
            results.append(SailSeekResult(
                uid=f'anilist:{media.get("id")}', category='anime', title=title,
                subtitle=subtitle, image=str(cover or ''), score=2,
                action=lambda raw=dict(media): self._open_anime(raw),
            ))
        return results

    def _map_manga(self, entries) -> list[SailSeekResult]:
        results = []
        for item in entries or []:
            title = str(getattr(item, 'title', '') or '')
            if not title:
                continue
            year = _format_year(getattr(item, 'year', ''))
            status = str(getattr(item, 'status', '') or '').replace('_', ' ').title()
            subtitle = ' · '.join(str(x) for x in (_type_label('manga'), year, status, 'MangaDex') if x)
            results.append(SailSeekResult(
                uid=f'manga:{getattr(item, "id", title)}', category='manga', title=title,
                subtitle=subtitle, image=str(getattr(item, 'cover_url', '') or ''), score=2,
                action=lambda entry=item: self._open_manga(entry),
            ))
        return results

    def _map_books(self, entries) -> list[SailSeekResult]:
        results = []
        for item in entries or []:
            title = str(getattr(item, 'title', '') or '')
            if not title:
                continue
            year = _format_year(getattr(item, 'year', ''))
            author = str(getattr(item, 'author', '') or '')
            provider = str(getattr(item, 'provider', '') or '').title()
            subtitle = ' · '.join(str(x) for x in (_type_label('book'), author, year, provider) if x)
            results.append(SailSeekResult(
                uid=f'book:{getattr(item, "provider", "")}:{getattr(item, "id", title)}',
                category='books', title=title, subtitle=subtitle,
                image=str(getattr(item, 'artwork_url', '') or ''), score=2,
                action=lambda entry=item: self._open_book(entry),
            ))
        return results

    def _map_roms(self, rows) -> list[SailSeekResult]:
        results = []
        for rom in rows or []:
            title = str(rom.get('title') or '')
            if not title:
                continue
            subtitle = ' · '.join(str(x) for x in (
                rom.get('console'), rom.get('source'), rom.get('file_size')) if x)
            results.append(SailSeekResult(
                uid=f'rom:{rom.get("id")}', category='roms', title=title,
                subtitle=subtitle, image='', score=2,
                action=lambda data=dict(rom): self._open_rom(data),
            ))
        return results


    def _cache_get(self, category: str, query: str):
        key = (category, query.casefold())
        entry = self._cache.get(key)
        if entry is None:
            return None
        stamp, data = entry
        if time.monotonic() - stamp > _ONLINE_TTL:
            self._cache.pop(key, None)
            return None
        self._cache.move_to_end(key)
        return data

    def _cache_put(self, category: str, query: str, data) -> None:
        key = (category, query.casefold())
        self._cache[key] = (time.monotonic(), data)
        self._cache.move_to_end(key)
        while len(self._cache) > _ONLINE_CACHE_MAX:
            self._cache.popitem(last=False)


    def request_visible_artwork(self) -> None:
        window = self._window
        if window is None or not window.isVisible():
            return
        if not bool(getattr(settings, 'sailseek_show_artwork', True)):
            return
        model = window._model
        first, last = window.visible_rows()
        if last < 0:
            return
        gen = self._generation
        if gen != self._art_gen:
            self._art_requested.clear()
            self._art_gen = gen
        for row in range(max(0, first - 1), min(model.rowCount(), last + 3)):
            result = window.row_result(row)
            if result is None or not result.image or result.uid in self._art_requested:
                continue
            if window.row_pixmap(result.uid) is not None:
                continue
            self._art_requested.add(result.uid)
            try:
                images.get(result.image, QSize(44, 58),
                           lambda pixmap, uid=result.uid, g=gen: self._on_artwork(uid, g, pixmap),
                           owner=self, priority=Priority.VISIBLE)
            except Exception:
                logger.debug('SailSeek artwork request failed for %s', result.image, exc_info=True)

    def _on_artwork(self, uid: str, generation: int, pixmap: QPixmap) -> None:
        if generation != self._generation:
            return
        window = self._window
        if window is None or pixmap is None or pixmap.isNull():
            return
        window.set_row_pixmap(uid, pixmap)


    def activate_current(self) -> None:
        if self._yt_info:
            self._youtube_download()
            return
        window = self._window
        if window is None:
            return
        result = window.current_result()
        if result is None:
            result = window.row_result(0)
        if result is not None:
            self.activate(result)

    def activate_secondary(self) -> None:
        window = self._window
        result = window.current_result() if window is not None else None
        if result is None or not result.secondary:
            return
        callback = result.secondary[0][1]
        self.hide()
        try:
            callback()
        except Exception:
            logger.exception('SailSeek secondary action failed for %s', result.uid)

    def activate(self, result: SailSeekResult) -> None:
        self.hide()
        try:
            if result.action is not None:
                result.action()
        except Exception:
            logger.exception('SailSeek action failed for %s', result.uid)

    def _restore_main_window(self) -> None:
        mw = self._mw
        try:
            if not mw.isVisible() or mw.isMinimized():
                if mw.isMaximized():
                    mw.showMaximized()
                else:
                    mw.showNormal()
            mw.raise_()
            mw.activateWindow()
            force = getattr(mw, '_force_foreground_focus', None)
            if callable(force):
                force()
        except Exception:
            logger.debug('Could not restore the main window', exc_info=True)

    def _open_download(self, item_id: str) -> None:
        self._restore_main_window()
        page = getattr(self._mw, 'download_page', None)
        if page is None:
            return
        try:
            self._mw.switchTo(page)
            page.focus_item(item_id)
        except Exception:
            logger.exception('Could not focus download %s', item_id)

    def _open_download_folder(self, item_id: str) -> None:
        page = getattr(self._mw, 'download_page', None)
        if page is None:
            return
        try:
            page.open_item_folder(item_id)
        except Exception:
            logger.exception('Could not open download folder %s', item_id)

    def _open_tv_detail(self, raw: dict, kind: str) -> None:
        self._restore_main_window()
        page = getattr(self._mw, 'tv_page', None)
        if page is None:
            return
        try:
            self._mw.switchTo(page)
            if hasattr(page, 'call_when_ready'):
                page.call_when_ready('open_detail', dict(raw), kind)
            else:
                page.open_detail(dict(raw), kind)
        except Exception:
            logger.exception('Could not open TV detail')

    def _open_anime(self, media: dict) -> None:
        self._restore_main_window()
        page = getattr(self._mw, 'tv_page', None)
        if page is None:
            return
        try:
            self._mw.switchTo(page)
            if hasattr(page, 'call_when_ready'):
                page.call_when_ready('open_anime_detail', dict(media))
            else:
                page.open_anime_detail(dict(media))
        except Exception:
            logger.exception('Could not open anime detail')

    def _open_manga(self, entry) -> None:
        self._restore_main_window()
        page = getattr(self._mw, 'books_page', None)
        if page is None:
            return
        try:
            self._mw.switchTo(page)
            if hasattr(page, 'call_when_ready'):
                page.call_when_ready('open_manga_detail', entry)
            else:
                page.open_manga_detail(entry)
        except Exception:
            logger.exception('Could not open manga detail')

    def _open_book(self, entry) -> None:
        self._restore_main_window()
        page = getattr(self._mw, 'books_page', None)
        if page is None:
            return
        try:
            self._mw.switchTo(page)
            if hasattr(page, 'call_when_ready'):
                page.call_when_ready('open_detail', entry)
            else:
                page.open_detail(entry)
        except Exception:
            logger.exception('Could not open book detail')

    def _open_repack(self, entry) -> None:
        self._restore_main_window()
        page = getattr(self._mw, 'pc_games_page', None)
        if page is None:
            return
        source = getattr(entry, 'source', '') or 'fitgirl'
        try:
            self._mw.switchTo(page)
            if hasattr(page, 'call_when_ready'):
                page.call_when_ready('open_detail', entry, source)
            else:
                page.open_detail(entry, source)
        except Exception:
            logger.exception('Could not open repack detail')

    def _open_rom(self, rom: dict) -> None:
        self._restore_main_window()
        page = getattr(self._mw, 'home_page', None)
        if page is None:
            return
        try:
            self._mw.switchTo(page)
            if hasattr(page, 'call_when_ready'):
                page.call_when_ready('open_rom', dict(rom))
            else:
                page.open_rom(dict(rom))
        except Exception:
            logger.exception('Could not open ROM detail')

    @staticmethod
    def _open_url(url: str) -> None:
        if not url:
            return
        try:
            from PySide6.QtGui import QDesktopServices
            from PySide6.QtCore import QUrl
            QDesktopServices.openUrl(QUrl(url))
        except Exception:
            logger.debug('Could not open URL %s', url, exc_info=True)
