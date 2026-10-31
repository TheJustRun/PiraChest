from __future__ import annotations
import logging
import os
import re
import threading
import time
from contextlib import suppress
from functools import wraps
from PySide6.QtCore import Qt, QSize, QSizeF, QRect, QThread, QObject, Signal, Property, QPropertyAnimation, QEasingCurve, QTimer, QVariantAnimation, QAbstractAnimation, QEvent
from shiboken6 import isValid as _qt_is_valid
from PySide6.QtGui import QBrush, QColor, QIcon, QImage, QFontMetrics, QMovie, QTextDocument
from PySide6.QtWidgets import QWidget, QVBoxLayout, QHBoxLayout, QLayout, QLayoutItem, QScrollArea, QDialog, QTreeWidgetItem, QHeaderView, QGridLayout, QLabel, QSizePolicy, QFileDialog, QGraphicsOpacityEffect, QTextBrowser, QListWidget, QListWidgetItem
from qfluentwidgets import CardWidget, FluentIcon, Pivot, TitleLabel, SubtitleLabel, BodyLabel, StrongBodyLabel, CaptionLabel, TransparentToolButton, PushButton, PrimaryPushButton, ImageLabel, PillPushButton, FlowLayout as QFlowLayout, themeColor, SearchLineEdit, LineEdit, CheckBox, TreeWidget, MessageBoxBase, SmoothScrollArea, HyperlinkButton, qconfig, isDarkTheme
from .home_page import AccentColorMixin, DetailsViewMixin, MediaPageBase
from PySide6.QtGui import QAction, QDesktopServices, QPainter, QPainterPath, QPen, QPixmap, QRegion
from PySide6.QtWidgets import QApplication, QGraphicsScene, QGraphicsView, QMenu, QPushButton as _QPB, QStackedWidget, QStyle, QSystemTrayIcon, QWidgetItem
from PySide6.QtCore import QCoreApplication, QRectF, QUrl
from qfluentwidgets import InfoBar, InfoBarPosition, RoundMenu
_bookmark_icon_cache: dict[str, QIcon] = {}

def _bookmark_icon(filled: bool=False) -> QIcon:
    key = 'filled' if filled else 'outline'
    cached = _bookmark_icon_cache.get(key)
    if cached is not None:
        return cached
    import math
    pm = QPixmap(96, 96)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    path = QPainterPath()
    for i in range(10):
        ang = -math.pi / 2 + i * math.pi / 5
        outer, inner = (46.0, 21.5) if filled else (42.0, 17.5)
        r = outer if i % 2 == 0 else inner
        x, y = (48.0 + r * math.cos(ang), 48.0 + r * math.sin(ang))
        path.moveTo(x, y) if i == 0 else path.lineTo(x, y)
    path.closeSubpath()
    base = QColor(palette()['primary_text'])
    if filled:
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(base)
    else:
        pen = QPen(base)
        pen.setWidthF(8.0)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        p.setPen(pen)
        p.setBrush(Qt.BrushStyle.NoBrush)
    p.drawPath(path)
    p.end()
    icon = _bookmark_icon_cache[key] = QIcon(pm)
    return icon
from src.core.models import assess_source_files, classify_file
from src.core.repacks.base import RepackEntry, RepackDetails, display_title, magnet_display_name
from src.core.worker import fetch_page_async, fetch_details_async, fetch_upcoming_repacks_async, fetch_search_async, fetch_popular_repacks_async, cancel as cancel_task, Priority
from src.core.downloader import DownloadManager, DLState
from src.core.theme import palette, to_color, register_theme_refresh, apply_pivot_theme, apply_tool_button_theme, branded_icon, count_badge_qss, themed_icon, apply_slot_icon_theme, ScrollingTextDelegate
from .grid import PosterGrid, ViewModeButton, attrs_subtitle, play_sfx
_source_subtitle = attrs_subtitle('source', labels={'fitgirl': 'FitGirl Repacks', 'gog': 'GOG Revived'})


def _entry_title(entry) -> str:
    raw = str(getattr(entry, 'title', '') or '')
    clean = str(getattr(entry, 'display_title', '') or '')
    if clean and clean != raw:
        return clean
    try:
        return display_title(raw) or raw
    except Exception:
        return raw


def _decorate_repack_entry(entry):
    try:
        from src.core.repacks.base import parse_title

        parsed = parse_title(getattr(entry, 'title', '') or '')
        if parsed.clean:
            entry.clean_title = parsed.clean
        entry.meta_text = parsed.display_meta
    except Exception:
        pass
    try:
        from src.core.config import settings as _settings

        if not getattr(_settings, 'steam_enrichment_enabled', False):
            entry.poster_fallback = None
        elif not getattr(entry, 'poster_path', None) and not getattr(entry, 'poster_url', None):
            from src.core.repacks import enrichment as service

            entry.poster_fallback = service.cached_card_art(entry) or None
    except Exception:
        pass
    return entry


def _entry_search_text(entry) -> str:
    try:
        from src.core.repacks import enrichment as service

        return service.search_text_for(entry)
    except Exception:
        return str(getattr(entry, 'title', '') or '')


def _entry_meta_text(entry) -> str:
    return str(getattr(entry, 'meta_text', '') or '')


def _titles_enabled() -> bool:
    try:
        from src.core.config import settings as _s
        return bool(getattr(_s, 'repack_show_titles', False))
    except Exception:
        return False
from src.core.translations import tr, register_locale_refresh
from src.core.repacks.installer import get_install_coordinator, parse_size_bytes, inspect_inno_setup, build_inno_selection
try:
    from src.core.repacks.installer import default_install_dir, remember_install_dir
except ImportError:
    def default_install_dir() -> str:
        import os as _os
        try:
            from src.core.config import settings as _s
            for key in ('games_install_dir', 'install_dir'):
                d = getattr(_s, key, None)
                if d:
                    return d
        except Exception:
            pass
        try:
            from src.core.config import paths as _paths
            base = _paths.download_root
        except Exception:
            base = _os.path.join(_os.path.expanduser('~'), 'PiraChest')
        return _os.path.join(base, 'Games')

    def remember_install_dir(root: str) -> None:
        try:
            from src.core.config import settings as _s, save_settings as _save
            _s.games_install_dir = root
            _save(_s)
        except Exception:
            pass
_SOURCE_DONATION_URLS = {'fitgirl': 'https://fitgirl-repacks.site/donations/', 'gog': 'https://gog-rev.com/support/'}
_SOURCE_UPCOMING_SUPPORTED = {'fitgirl'}

_NAV_REFRESH_INTERVAL_S = 180.0


_DETAILS_PREFETCH_MAX = 3
_DETAILS_PREFETCH_PER_SCREEN = 12
logger = logging.getLogger(__name__)
_GUI_DIR = os.path.dirname(os.path.abspath(__file__))
_PHOTO_DIR = os.path.join(_GUI_DIR, 'photos')
_SOURCE_TAB_ICON_FILENAMES = {'fitgirl': 'fg.png', 'gog': 'gr.png'}

def _source_tab_icon(source_key: str, size: int = 22) -> QIcon | None:
    filename = _SOURCE_TAB_ICON_FILENAMES.get(source_key)
    if not filename:
        return None
    path = os.path.join(_PHOTO_DIR, filename)
    if not os.path.isfile(path):
        return None
    icon = branded_icon(f'repacks.tab_{source_key}', QIcon(path), size=size)
    return icon if not icon.isNull() else None
import json as _json

def _bookmarks_path() -> str:
    try:
        from src.core.config import paths as _paths
        base = _paths.config_dir
    except Exception:
        base = os.path.join(os.path.expanduser('~'), '.pirachest')
    return os.path.join(base, 'pirachest_bookmarks.json')

class _BookmarkStore(QObject):
    changed = Signal()
    _lock = threading.Lock()

    def __init__(self):
        super().__init__()
        self._items = {}
        self._loaded = False

    def _ensure_loaded(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        try:
            with open(_bookmarks_path(), 'r', encoding='utf-8') as fh:
                data = _json.load(fh)
            if isinstance(data, list):
                for d in data:
                    if isinstance(d, dict) and d.get('url'):
                        self._items[f'{d.get('source', '')}|{d.get('url', '')}'] = d
        except Exception:
            pass

    def _save(self) -> None:
        try:
            path = _bookmarks_path()
            os.makedirs(os.path.dirname(path), exist_ok=True)
            tmp = path + '.tmp'
            with open(tmp, 'w', encoding='utf-8') as fh:
                _json.dump(list(self._items.values()), fh, ensure_ascii=False, indent=1)
            os.replace(tmp, path)
        except Exception:
            logger.exception('Failed to save bookmarks')

    def all(self) -> list:
        with self._lock:
            self._ensure_loaded()
            return list(self._items.values())

    def count(self) -> int:
        with self._lock:
            self._ensure_loaded()
            return len(self._items)

    def has(self, source: str, url: str) -> bool:
        with self._lock:
            self._ensure_loaded()
            return f'{source}|{url}' in self._items

    def add_entry(self, entry) -> bool:
        source = getattr(entry, 'source', '') or ''
        url = getattr(entry, 'url', '') or ''
        if not url:
            return False
        with self._lock:
            self._ensure_loaded()
            key = f'{source}|{url}'
            if key in self._items:
                return False
            self._items[key] = {'source': source, 'title': getattr(entry, 'title', ''), 'url': url, 'slug': getattr(entry, 'slug', None), 'poster_url': getattr(entry, 'poster_url', None), 'poster_path': getattr(entry, 'poster_path', None)}
        self._save()
        self.changed.emit()
        return True

    def remove(self, source: str, url: str) -> bool:
        with self._lock:
            self._ensure_loaded()
            key = f'{source}|{url}'
            if key not in self._items:
                return False
            del self._items[key]
        self._save()
        self.changed.emit()
        return True

    def to_entry(self, d: dict) -> RepackEntry:
        entry = RepackEntry(
            source=d.get('source', ''), title=d.get('title', ''), url=d.get('url', ''),
            poster_url=d.get('poster_url'), poster_path=d.get('poster_path'), slug=d.get('slug'),
            clean_title=d.get('clean_title'), meta_text=d.get('meta_text'),
            poster_fallback=d.get('poster_fallback'))
        return _decorate_repack_entry(entry)
_bookmark_store = None

def bookmark_store() -> _BookmarkStore:
    global _bookmark_store
    if _bookmark_store is None:
        _bookmark_store = _BookmarkStore()
    return _bookmark_store
_DWMWA_USE_IMMERSIVE_DARK_MODE = 20
_DWMWA_WINDOW_CORNER_PREFERENCE = 33
_DWMWA_SYSTEMBACKDROP_TYPE = 38
_DWMWCP_ROUND = 2
_DWMSBT_TRANSIENTWINDOW = 3

class FlowLayout(QLayout):
    def __init__(self, parent=None, margin: int=0, spacing: int=12):
        super().__init__(parent)
        self._items: list[QLayoutItem] = []
        self.setContentsMargins(margin, margin, margin, margin)
        self.setSpacing(spacing)

    def addItem(self, item: QLayoutItem) -> None:
        self._items.append(item)

    def insertWidget(self, index: int, widget: QWidget) -> None:
        self.addChildWidget(widget)
        index = max(0, min(index, len(self._items)))
        self._items.insert(index, QWidgetItem(widget))
        self.invalidate()

    def count(self) -> int:
        return len(self._items)

    def itemAt(self, index: int):
        if 0 <= index < len(self._items):
            return self._items[index]
        return None

    def takeAt(self, index: int):
        if 0 <= index < len(self._items):
            return self._items.pop(index)
        return None

    def expandingDirections(self):
        return Qt.Orientation(0)

    def hasHeightForWidth(self) -> bool:
        return True

    def heightForWidth(self, width: int) -> int:
        return self._do_layout(QRect(0, 0, width, 0), test_only=True)

    def setGeometry(self, rect: QRect) -> None:
        if rect == self.geometry():
            super().setGeometry(rect)
            return
        super().setGeometry(rect)
        self._do_layout(rect, test_only=False)

    def sizeHint(self) -> QSize:
        return self.minimumSize()

    def minimumSize(self) -> QSize:
        size = QSize()
        for item in self._items:
            size = size.expandedTo(item.minimumSize())
        margins = self.contentsMargins()
        size += QSize(margins.left() + margins.right(), margins.top() + margins.bottom())
        return size

    def _do_layout(self, rect: QRect, test_only: bool) -> int:
        left, top, right, bottom = self.getContentsMargins()
        effective_rect = rect.adjusted(left, top, -right, -bottom)
        base_spacing = self.spacing()
        visible_items = [item for item in self._items if item.widget() is None or item.widget().isVisible()]
        row: list[QLayoutItem] = []
        y = effective_rect.y()
        line_height = 0
        available_w = effective_rect.width()

        def _flush_row(is_last: bool) -> None:
            nonlocal y, line_height
            if not row:
                return
            n = len(row)
            items_w = sum((it.sizeHint().width() for it in row))
            if n > 1 and (not is_last):
                gap = max(base_spacing, (available_w - items_w) / (n - 1))
            else:
                gap = base_spacing
            x = effective_rect.x()
            row_line_h = 0
            for it in row:
                iw = it.sizeHint().width()
                ih = it.sizeHint().height()
                if not test_only:
                    it.setGeometry(QRect(round(x), y, iw, ih))
                x += iw + gap
                row_line_h = max(row_line_h, ih)
            line_height = row_line_h
            y += line_height + base_spacing
        for item in visible_items:
            item_width = item.sizeHint().width()
            items_w_if_added = sum((it.sizeHint().width() for it in row)) + item_width
            gaps_if_added = len(row) * base_spacing
            if row and items_w_if_added + gaps_if_added > available_w:
                _flush_row(is_last=False)
                row = [item]
            else:
                row.append(item)
        if row:
            _flush_row(is_last=True)
        total_h = y - base_spacing - effective_rect.y() if line_height else 0
        return total_h + top + bottom
from src.core.artwork import images, image_file, FIT

def _stop_previous_movie(image_label) -> None:
    try:
        movie = image_label.movie()
    except Exception:
        movie = None
    if movie is not None:
        movie.stop()
        movie.deleteLater()
        QLabel.setMovie(image_label, None)

class _SteppedAnimator(QObject):
    finished = Signal()

    def __init__(self, setter, parent=None):
        super().__init__(parent)
        self._setter = setter
        self._anim = QVariantAnimation(self)
        self._anim.setDuration(150)
        self._anim.valueChanged.connect(lambda v: self._setter(float(v)))
        self._anim.finished.connect(self.finished.emit)

    def stop(self) -> None:
        self._anim.stop()

    def setDuration(self, ms: int) -> None:
        self._anim.setDuration(max(ms, 16))

    def setStartValue(self, value) -> None:
        self._anim.setStartValue(float(value))

    def setEndValue(self, value) -> None:
        self._anim.setEndValue(float(value))
        self._anim.setEasingCurve(QEasingCurve.Type.OutCubic)

    def start(self) -> None:
        self._anim.start()

class _AnimatedPosterMixin:
    _LIFT_PX = 8
    _HOVER_MS = 0
    _PRESS_MS = 90

    def _init_poster_animations(self) -> None:
        self._anim_base_pos = self._poster_lbl.pos()
        self._pos_anim = _SteppedAnimator(self._apply_lift_offset)

    def _apply_lift_offset(self, offset_y: float) -> None:
        poster_lbl = getattr(self, '_poster_lbl', None)
        if poster_lbl is None or not _qt_is_valid(self) or (not _qt_is_valid(poster_lbl)):
            self._stop_poster_animation()
            return
        base = self._anim_base_pos
        poster_lbl.move(base.x(), round(base.y() - offset_y))
        self.update()

    def _stop_poster_animation(self) -> None:
        pos_anim = getattr(self, '_pos_anim', None)
        if pos_anim is not None and _qt_is_valid(pos_anim):
            pos_anim.stop()

    def _animate_to(self, offset_y: int, duration: int) -> None:
        if not _qt_is_valid(self) or not _qt_is_valid(self._poster_lbl):
            return
        self._pos_anim.stop()
        if duration <= 0:
            self._apply_lift_offset(float(offset_y))
            return
        current_offset = self._anim_base_pos.y() - self._poster_lbl.pos().y()
        self._pos_anim.setDuration(duration)
        self._pos_anim.setStartValue(current_offset)
        self._pos_anim.setEndValue(offset_y)
        self._pos_anim.start()

    def animate_hover_enter(self) -> None:
        self._animate_to(self._LIFT_PX, self._HOVER_MS)

    def animate_hover_leave(self) -> None:
        self._animate_to(0, self._HOVER_MS)

    def animate_press(self) -> None:
        self._animate_to(self._LIFT_PX - 3, self._PRESS_MS)

    def animate_release(self, still_hovering: bool) -> None:
        if still_hovering:
            self._animate_to(self._LIFT_PX, self._PRESS_MS)
        else:
            self._animate_to(0, self._PRESS_MS)

    def hideEvent(self, event) -> None:
        self._stop_poster_animation()
        super().hideEvent(event)

class PosterCard(_AnimatedPosterMixin, QWidget):
    clicked_poster = Signal(object)
    download_requested = Signal(object)
    hovered = Signal(object)
    POSTER_WIDTH = 150
    POSTER_HEIGHT = 210
    _HOVER_HEADROOM_PX = 8

    def __init__(self, parent=None):
        super().__init__(parent)
        self._entry = None
        self.setFixedSize(self.POSTER_WIDTH, self.POSTER_HEIGHT + self._HOVER_HEADROOM_PX)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setStyleSheet('background: transparent; border: none;')
        self._poster_lbl = ImageLabel(self)
        self._poster_lbl.setBorderRadius(6, 6, 6, 6)
        self._poster_lbl.setGeometry(0, self._HOVER_HEADROOM_PX, self.POSTER_WIDTH, self.POSTER_HEIGHT)
        self._fallback_icon = None
        self._source_badge_lbl = None
        self._init_poster_animations()

    def bind(self, entry) -> None:
        self._entry = entry
        self.setToolTip(_entry_title(entry))
        if self._fallback_icon is not None:
            self._fallback_icon.deleteLater()
            self._fallback_icon = None
            self._poster_lbl.setStyleSheet('')
        if self._source_badge_lbl is not None:
            self._source_badge_lbl.deleteLater()
            self._source_badge_lbl = None
        _stop_previous_movie(self._poster_lbl)
        self._poster_lbl.setImage(None)
        self._poster_lbl.setFixedSize(self.POSTER_WIDTH, self.POSTER_HEIGHT)
        if entry.poster_path:
            self.set_poster_path(entry.poster_path)
        elif not entry.poster_url:
            self.show_fallback_icon()

    def set_source_badge(self, source: str) -> None:
        filename = _SOURCE_TAB_ICON_FILENAMES.get(source)
        if not filename or self._source_badge_lbl is not None:
            return
        path = os.path.join(_PHOTO_DIR, filename)
        if not os.path.isfile(path):
            return
        icon = branded_icon(f'repacks.tab_{source}', QIcon(path), size=20)
        pix = icon.pixmap(20, 20)
        if pix.isNull():
            return
        badge = QLabel(self._poster_lbl)
        badge.setPixmap(pix)
        badge.setFixedSize(26, 26)
        badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
        badge.setStyleSheet(
            f'background-color: {palette()["card_bg"]};'
            f' border: 1px solid {palette()["surface_border"]}; border-radius: 6px;')
        badge.move(6, 6)
        badge.show()
        badge.raise_()
        self._source_badge_lbl = badge

    def enterEvent(self, event):
        if self._entry is not None:
            self.hovered.emit(self._entry)
        grid = self.parent()
        while grid is not None and not hasattr(grid, 'is_scrolling'):
            grid = grid.parent()
        if grid is not None and grid.is_scrolling:
            super().enterEvent(event)
            return
        self.raise_()
        self.animate_hover_enter()
        play_sfx('hover')
        super().enterEvent(event)

    def leaveEvent(self, event):
        self.hovered.emit(None)
        self.animate_hover_leave()
        super().leaveEvent(event)

    def show_fallback_icon(self) -> None:
        if self._fallback_icon is not None:
            return
        self._poster_lbl.setStyleSheet(f'background-color: {_surface_tint_color(14)}; border-radius: 6px;')
        wrapper = QWidget(self._poster_lbl)
        wrapper.setFixedSize(self.POSTER_WIDTH, self.POSTER_HEIGHT)
        wrapper.setStyleSheet('background: transparent;')
        wrapper_layout = QVBoxLayout(wrapper)
        wrapper_layout.setContentsMargins(10, 0, 10, 0)
        wrapper_layout.setSpacing(8)
        wrapper_layout.addStretch(1)
        icon_lbl = QLabel(wrapper)
        icon_lbl.setPixmap(themed_icon('repacks.card_fallback', FluentIcon.GAME, size=36, color=_body_text_color()).pixmap(36, 36))
        icon_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        icon_lbl.setStyleSheet('background: transparent;')
        wrapper_layout.addWidget(icon_lbl)
        title_lbl = QLabel(_entry_title(self._entry), wrapper)
        title_lbl.setWordWrap(True)
        title_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        title_lbl.setStyleSheet(f'background: transparent; font-size: 11px; color: {palette()['primary_text']};')
        title_lbl.setMaximumHeight(60)
        wrapper_layout.addWidget(title_lbl)
        wrapper_layout.addStretch(1)
        wrapper.show()
        self._fallback_icon = wrapper

    @property
    def entry(self):
        return self._entry

    @property
    def poster_url(self) -> str | None:
        return self._entry.poster_url if self._entry is not None else None

    def set_poster_path(self, path: str) -> None:
        if self._fallback_icon is not None:
            self._fallback_icon.deleteLater()
            self._fallback_icon = None
            self._poster_lbl.setStyleSheet('')
        _stop_previous_movie(self._poster_lbl)
        self._entry.poster_path = path

        def _apply(pix, _path=path):
            if not _qt_is_valid(self) or not _qt_is_valid(self._poster_lbl):
                return
            if self._entry is None or self._entry.poster_path != _path:
                return
            if pix.isNull():
                self.show_fallback_icon()
                return
            self._poster_lbl.setImage(pix)
            self._poster_lbl.setFixedSize(self.POSTER_WIDTH, self.POSTER_HEIGHT)
        images.get(path, (self.POSTER_WIDTH, self.POSTER_HEIGHT), _apply, owner=self)

    def unload_pixmap(self) -> None:
        if self._entry is not None and self._entry.poster_path and not self._poster_lbl.isNull():
            _stop_previous_movie(self._poster_lbl)
            self._poster_lbl.setImage(None)
            self._poster_lbl.setFixedSize(self.POSTER_WIDTH, self.POSTER_HEIGHT)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.RightButton:
            self._show_grid_context_menu()
            event.accept()
            return
        if event.button() == Qt.MouseButton.LeftButton:
            self.animate_press()
        super().mousePressEvent(event)

    def _show_grid_context_menu(self) -> None:
        menu = _mica_menu(self)
        act_open = QAction(tr('repacks.open_details'), menu)
        act_dl = QAction(tr('repacks.download').capitalize(), menu)
        act_web = QAction(tr('repacks.open_in_website'), menu)
        source = getattr(self._entry, 'source', '') or ''
        marked = bookmark_store().has(source, getattr(self._entry, 'url', ''))
        act_bm = QAction(tr('repacks.remove_bookmark') if marked else tr('repacks.bookmark_it'), menu)
        act_open.triggered.connect(lambda _checked=False: self.clicked_poster.emit(self._entry))
        act_dl.triggered.connect(lambda _checked=False: self.download_requested.emit(self._entry))
        act_web.triggered.connect(lambda _checked=False: open_external_url(getattr(self._entry, 'url', '')))
        act_bm.triggered.connect(lambda _checked=False: bookmark_store().remove(source, self._entry.url) if marked else bookmark_store().add_entry(self._entry))
        menu.addAction(act_open)
        menu.addAction(act_dl)
        menu.addAction(act_web)
        menu.addAction(act_bm)
        menu.exec(self.cursor().pos())

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            still_hovering = self.rect().contains(event.position().toPoint())
            self.animate_release(still_hovering)
            if still_hovering:
                play_sfx('click')
                self.clicked_poster.emit(self._entry)
        super().mouseReleaseEvent(event)

class PopularCarousel(QWidget):
    poster_clicked = Signal(object)
    download_requested = Signal(object)
    hovered = Signal(object)

    PAGE_SIZE = 5
    MIN_VISIBLE = 2
    _ROW_FIXED_OVERHEAD = 96
    _CARD_SPACING = 14

    def __init__(self, parent=None):
        super().__init__(parent)
        self._entries: list = []
        self._page = 0
        self._cards: list[QWidget] = []
        self._dots: list[QWidget] = []
        self._rendered_key: tuple = (None, None)

        self._card_h = PosterCard.POSTER_HEIGHT + PosterCard._HOVER_HEADROOM_PX
        self.setFixedHeight(self._card_h + 24 + 10 + 6 + 10)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(10)

        self._header_lbl = StrongBodyLabel(tr('repacks.most_popular_this_week'))
        self._header_lbl.setStyleSheet(f'font-size: 16px; font-weight: 600; color: {palette()["primary_text"]};')
        self._header_lbl.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        outer.addWidget(self._header_lbl)


        row = QHBoxLayout()
        row.addStretch(1)
        inner = QHBoxLayout()
        inner.setSpacing(10)
        self.prev_btn = TransparentToolButton(themed_icon('repacks.latest_prev', FluentIcon.LEFT_ARROW), self)
        apply_tool_button_theme(self.prev_btn)
        self.prev_btn.setFixedSize(32, 32)
        self.prev_btn.clicked.connect(self._go_prev)
        inner.addWidget(self.prev_btn, 0, Qt.AlignmentFlag.AlignVCenter)

        self._cards_row = QHBoxLayout()
        self._cards_row.setSpacing(self._CARD_SPACING)
        inner.addLayout(self._cards_row, 0)

        self.next_btn = TransparentToolButton(themed_icon('repacks.latest_next', FluentIcon.RIGHT_ARROW), self)
        apply_tool_button_theme(self.next_btn)
        self.next_btn.setFixedSize(32, 32)
        self.next_btn.clicked.connect(self._go_next)
        inner.addWidget(self.next_btn, 0, Qt.AlignmentFlag.AlignVCenter)
        row.addLayout(inner, 0)
        row.addStretch(1)
        outer.addLayout(row)

        dots_row = QHBoxLayout()
        dots_row.setSpacing(6)
        dots_row.addStretch(1)
        self._dots_row = dots_row
        dots_row.addStretch(1)
        outer.addLayout(dots_row)

        self.setVisible(False)
        register_locale_refresh(self, self._apply_locale)

    def _apply_locale(self, *_args) -> None:
        self._header_lbl.setText(tr('repacks.most_popular_this_week'))


    def _visible_count(self) -> int:
        row_width = self.width() - self._ROW_FIXED_OVERHEAD
        card_w = PosterCard.POSTER_WIDTH + self._CARD_SPACING
        if card_w <= 0 or row_width <= 0:
            return self.MIN_VISIBLE
        return max(self.MIN_VISIBLE, min(self.PAGE_SIZE, row_width // card_w))

    def _page_count(self, visible_count: int) -> int:
        if not self._entries or visible_count <= 0:
            return 0
        return (len(self._entries) + visible_count - 1) // visible_count


    def set_entries(self, entries: list) -> None:
        self._entries = entries or []
        self._page = 0
        show = bool(self._entries)
        self.setVisible(show)
        self.setFixedHeight(self._card_h + 24 + 10 + 6 + 10 if show else 0)
        self._rendered_key = (None, None)
        self._render()

    def set_section_visible(self, visible: bool) -> None:
        self.setVisible(bool(visible) and bool(self._entries))

    def _render(self) -> None:
        if not self._entries:
            if self._rendered_key != (None, None):
                self._rendered_key = (None, None)
                self._clear_cards()
                self._rebuild_dots(0)
            return

        visible_count = self._visible_count()
        page_count = self._page_count(visible_count)


        self._page = max(0, min(self._page, page_count - 1)) if page_count else 0

        render_key = (visible_count, self._page)
        if render_key == self._rendered_key:
            return
        self._rendered_key = render_key

        self._clear_cards()
        start = self._page * visible_count
        page_entries = self._entries[start:start + visible_count]
        for entry in page_entries:
            card = PosterCard(self)
            card.bind(entry)
            card.clicked_poster.connect(self.poster_clicked.emit)
            card.download_requested.connect(self.download_requested.emit)
            card.hovered.connect(self.hovered)
            self._cards_row.addWidget(card)
            self._cards.append(card)
            if entry.poster_url and not entry.poster_path:
                card.set_poster_path(entry.poster_url)
        for _ in range(visible_count - len(page_entries)):
            placeholder = QWidget(self)
            placeholder.setFixedSize(PosterCard.POSTER_WIDTH, self._card_h)
            self._cards_row.addWidget(placeholder)
            self._cards.append(placeholder)

        self.prev_btn.setEnabled(self._page > 0)
        self.next_btn.setEnabled(self._page < page_count - 1)
        self._rebuild_dots(page_count)

    def _clear_cards(self) -> None:
        for card in self._cards:
            if isinstance(card, PosterCard):
                card.unload_pixmap()
            self._cards_row.removeWidget(card)
            card.setVisible(False)
            card.setParent(None)
            card.deleteLater()
        self._cards.clear()

    def _rebuild_dots(self, page_count: int) -> None:
        for dot in self._dots:
            self._dots_row.removeWidget(dot)
            dot.deleteLater()
        self._dots.clear()
        for _ in range(page_count):
            dot = QWidget(self)
            dot.setFixedSize(7, 7)
            self._dots_row.insertWidget(self._dots_row.count() - 1, dot)
            self._dots.append(dot)
        self._update_dot_colors()

    def _update_dot_colors(self) -> None:
        for i, dot in enumerate(self._dots):
            color = themeColor().name() if i == self._page else _inactive_dot_color()
            dot.setStyleSheet(f'background-color: {color}; border-radius: 3px;')

    def _go_prev(self) -> None:
        if self._page > 0:
            self._page -= 1
            self._render()

    def _go_next(self) -> None:
        page_count = self._page_count(self._visible_count())
        if self._page < page_count - 1:
            self._page += 1
            self._render()


    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._render()

    def showEvent(self, event):
        super().showEvent(event)
        self._render()


class BookmarkPosterCard(PosterCard):
    open_requested = Signal(object)
    download_requested = Signal(object)
    website_requested = Signal(object)
    remove_requested = Signal(object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._remove_x = _QPB('✕', self._poster_lbl)
        self._remove_x.setFixedSize(20, 20)
        self._remove_x.setCursor(Qt.CursorShape.PointingHandCursor)
        self._style_remove_x()
        register_theme_refresh(self, self._style_remove_x)
        self._remove_x.clicked.connect(lambda _checked=False: self.remove_requested.emit(self._entry))
        self._remove_x.show()
        self._remove_x.raise_()

    def _style_remove_x(self) -> None:
        pal = palette()
        with suppress(RuntimeError):
            self._remove_x.setStyleSheet(
                'QPushButton {'
                f' background-color: {pal["card_bg"]}; color: {pal["primary_text"]};'
                f' border: 1px solid {pal["card_border"]}; border-radius: 10px; font-size: 11px;'
                ' }'
                ' QPushButton:hover {'
                f' background-color: {pal["state_error"]}; color: {pal["on_accent"]};'
                ' }')

    def bind(self, entry) -> None:
        super().bind(entry)
        self.set_source_badge(getattr(entry, 'source', ''))
        self._remove_x.raise_()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        x = getattr(self, '_remove_x', None)
        if x is not None:
            x.move(max(0, self._poster_lbl.width() - 26), 6)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.RightButton:
            self._show_menu(event.globalPosition().toPoint())
            event.accept()
            return
        super().mousePressEvent(event)

    def _show_menu(self, global_pos) -> None:
        menu = _mica_menu(self)
        act_open = QAction(tr('repacks.open_details'), menu)
        act_dl = QAction(tr('repacks.download').capitalize(), menu)
        act_web = QAction(tr('repacks.open_in_website'), menu)
        act_rm = QAction(tr('repacks.remove_bookmark'), menu)
        act_open.triggered.connect(lambda _checked=False: self.open_requested.emit(self._entry))
        act_dl.triggered.connect(lambda _checked=False: self.download_requested.emit(self._entry))
        act_web.triggered.connect(lambda _checked=False: self.website_requested.emit(self._entry))
        act_rm.triggered.connect(lambda _checked=False: self.remove_requested.emit(self._entry))
        menu.addAction(act_open)
        menu.addAction(act_dl)
        menu.addAction(act_web)
        menu.addSeparator()
        menu.addAction(act_rm)
        menu.exec(global_pos)

class BookmarksPage(QWidget):
    open_requested = Signal(object)
    download_requested = Signal(object)
    back_requested = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName('bookmarksPage')
        self.setStyleSheet('background: transparent;')
        outer = QVBoxLayout(self)


        outer.setContentsMargins(24, 12, 24, 20)
        outer.setSpacing(10)
        back_btn = PushButton(themed_icon('repacks.bookmarks_back', FluentIcon.RETURN), tr('repacks.back_to_grid'))
        back_btn.setFixedHeight(32)
        self._back_btn = back_btn
        back_btn.clicked.connect(self.back_requested.emit)
        outer.addWidget(back_btn, 0, Qt.AlignmentFlag.AlignLeft)
        header = QHBoxLayout()
        self._title_lbl = TitleLabel(tr('repacks.bookmarks_title'))
        header.addWidget(self._title_lbl)
        self._count_lbl = CaptionLabel('')
        self._count_lbl.setStyleSheet(f'color: {_muted_text_color()};')
        header.addWidget(self._count_lbl, 0, Qt.AlignmentFlag.AlignBottom)
        header.addStretch(1)
        outer.addLayout(header)
        self._scroll = SmoothScrollArea(self)
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        self._scroll.setStyleSheet('background: transparent; border: none;')
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._wrap = QWidget()
        self._wrap.setStyleSheet('background: transparent;')
        self._wrap_lay = QVBoxLayout(self._wrap)
        self._wrap_lay.setContentsMargins(4, 6, 8, 6)
        self._wrap_lay.setSpacing(0)
        self._flow_container = QWidget(self._wrap)
        self._flow_container.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        self._flow_container.setStyleSheet('background: transparent;')
        self._flow = FlowLayout(self._flow_container, margin=0, spacing=14)
        self._wrap_lay.addWidget(self._flow_container)
        self._wrap_lay.addStretch(1)
        self._scroll.setWidget(self._wrap)
        outer.addWidget(self._scroll, 1)
        self._empty_lbl = BodyLabel(tr('repacks.bookmarks_empty'))
        self._empty_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._empty_lbl.setStyleSheet(f'color: {_muted_text_color()};')
        outer.addWidget(self._empty_lbl)
        self._empty_lbl.hide()
        self._cards = []
        self._dirty = True
        bookmark_store().changed.connect(lambda: setattr(self, '_dirty', True))

    def showEvent(self, event) -> None:
        super().showEvent(event)
        if self._dirty:
            self.rebuild()
        QTimer.singleShot(0, self._force_relayout)

    def rebuild(self) -> None:
        self._dirty = False
        entries = [bookmark_store().to_entry(d) for d in bookmark_store().all()]
        for card in self._cards:
            self._flow.removeWidget(card)
            card.hide()
            card.setParent(None)
            card.deleteLater()
        self._cards.clear()
        for entry in entries:
            card = BookmarkPosterCard(self._flow_container)
            card.bind(entry)
            card.clicked_poster.connect(self.open_requested.emit)
            card.open_requested.connect(self.open_requested.emit)
            card.download_requested.connect(self.download_requested.emit)
            card.website_requested.connect(self._open_website)
            card.remove_requested.connect(self._remove)
            self._flow.addWidget(card)
            card.show()
            self._cards.append(card)
            if entry.poster_path and os.path.isfile(entry.poster_path):
                card.set_poster_path(entry.poster_path)
            elif entry.poster_url:
                card.set_poster_path(entry.poster_url)
            else:
                card.show_fallback_icon()
        self._count_lbl.setText(f'{len(entries)}')
        self._empty_lbl.setVisible(not entries)
        self._flow.invalidate()
        self._flow.update()
        self._flow.activate()
        self._flow_container.updateGeometry()
        QTimer.singleShot(0, self._force_relayout)

    def _force_relayout(self) -> None:
        self._flow.activate()
        self._flow.update()
        self._flow_container.updateGeometry()


    def _open_website(self, entry) -> None:
        open_external_url(getattr(entry, 'url', ''))

    def _remove(self, entry) -> None:
        bookmark_store().remove(getattr(entry, 'source', ''), getattr(entry, 'url', ''))
        self.rebuild()

class MetaField(QWidget):
    def __init__(self, label: str, value: str, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(2)
        align = Qt.AlignmentFlag.AlignLeft
        label_lbl = CaptionLabel(label.upper())
        label_lbl.setAlignment(align)
        label_lbl.setStyleSheet(f'font-weight: 400; color: {_muted_text_color()};')
        layout.addWidget(label_lbl)
        value_lbl = StrongBodyLabel(value)
        value_lbl.setAlignment(align)
        value_lbl.setWordWrap(True)
        value_lbl.setStyleSheet(f'font-weight: 700; color: {palette()['primary_text']};')
        layout.addWidget(value_lbl)

def make_tag_pill(text: str, parent=None) -> PillPushButton:
    pill = PillPushButton(text, parent)
    pill.setChecked(True)
    pill.setCheckable(False)
    pill.setCursor(Qt.CursorShape.ArrowCursor)

    def _repolish():
        style = pill.style()
        style.unpolish(pill)
        style.polish(pill)
        pill.update()
    register_theme_refresh(pill, _repolish)
    return pill

def _round_widget_corners(widget: QWidget, radius: int) -> None:
    def _apply_mask() -> None:
        path = QPainterPath()
        path.addRoundedRect(QRectF(0, 0, widget.width(), widget.height()), radius, radius)
        widget.setMask(QRegion(path.toFillPolygon().toPolygon()))
    _apply_mask()
    original_resize_event = widget.resizeEvent

    def _resize_event(event):
        original_resize_event(event)
        _apply_mask()
    widget.resizeEvent = _resize_event

class _LightboxCloseButton(_QPB):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedSize(40, 40)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setAttribute(Qt.WidgetAttribute.WA_Hover, True)

    def paintEvent(self, event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        alpha = 100 if self.isDown() else 72 if self.underMouse() else 38
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(255, 255, 255, alpha))
        p.drawEllipse(self.rect().adjusted(1, 1, -1, -1))
        pen = QPen(QColor(255, 255, 255, 235), 2)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        p.setPen(pen)
        cx, cy, d = self.width() // 2, self.height() // 2, 7
        p.drawLine(cx - d, cy - d, cx + d, cy + d)
        p.drawLine(cx - d, cy + d, cx + d, cy - d)
        p.end()


class ScreenshotEnlargeDialog(QWidget):
    finished = Signal(int)
    _MARGIN = 56
    _DIM = 200

    def __init__(self, image_path: str, parent=None):
        super().__init__(parent)
        self._path = image_path
        self._pix = None
        self._t = 0.0
        self._closing = False
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setMouseTracking(True)
        self.setCursor(Qt.CursorShape.ArrowCursor)
        self._close_btn = _LightboxCloseButton(self)
        self._close_btn.clicked.connect(self.close_lightbox)
        self._anim = QVariantAnimation(self)
        self._anim.setDuration(160)
        self._anim.setStartValue(0.0)
        self._anim.setEndValue(1.0)
        self._anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._anim.valueChanged.connect(self._on_anim)
        self._anim.finished.connect(self._on_anim_finished)
        self._reload = QTimer(self)
        self._reload.setSingleShot(True)
        self._reload.setInterval(120)
        self._reload.timeout.connect(self._request)
        if parent is not None:
            parent.installEventFilter(self)
        self._fit_to_parent()

    def _fit_to_parent(self) -> None:
        parent = self.parentWidget()
        if parent is not None:
            self.setGeometry(parent.rect())

    def _available(self) -> QRectF:
        full = QRectF(self.rect()).adjusted(self._MARGIN, self._MARGIN, -self._MARGIN, -self._MARGIN)
        w = min(full.width(), max(320.0, min(self.width() * 0.6, 1000.0)))
        h = min(full.height(), max(180.0, min(self.height() * 0.65, 640.0)))
        c = QRectF(self.rect()).center()
        return QRectF(c.x() - w / 2, c.y() - h / 2, w, h)

    def _target_rect(self) -> QRectF:
        avail = self._available()
        if self._pix is not None and not self._pix.isNull():
            dpr = self._pix.devicePixelRatio() or 1.0
            w, h = self._pix.width() / dpr, self._pix.height() / dpr
        else:
            w, h = 640.0, 360.0
        scale = min(1.0, avail.width() / max(1.0, w), avail.height() / max(1.0, h))
        w, h = w * scale, h * scale
        c = avail.center()
        return QRectF(c.x() - w / 2, c.y() - h / 2, w, h)

    def _request(self) -> None:
        avail = self._available()
        images.get(self._path, (max(120, int(avail.width())), max(120, int(avail.height()))),
                   self._on_ready, mode=FIT, owner=self)

    def _on_ready(self, pix) -> None:
        if not _qt_is_valid(self) or pix is None or pix.isNull():
            return
        self._pix = pix
        self.update()

    def _on_anim(self, value) -> None:
        self._t = float(value)
        self.update()

    def _on_anim_finished(self) -> None:
        if self._closing:
            self.hide()
            self.finished.emit(0)
            self.deleteLater()

    def close_lightbox(self) -> None:
        if self._closing:
            return
        self._closing = True
        self._anim.stop()
        self._anim.setDirection(QAbstractAnimation.Direction.Backward)
        self._anim.start()

    def showEvent(self, event) -> None:
        super().showEvent(event)
        self._fit_to_parent()
        self.raise_()
        self.setFocus(Qt.FocusReason.PopupFocusReason)
        self._anim.setDirection(QAbstractAnimation.Direction.Forward)
        self._anim.start()
        self._request()

    def hideEvent(self, event) -> None:
        try:
            images.release(self)
        except Exception:
            pass
        super().hideEvent(event)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._close_btn.move(self.width() - self._close_btn.width() - 20, 20)
        self._close_btn.raise_()
        self._reload.start()

    def eventFilter(self, obj, event) -> bool:
        if obj is self.parentWidget() and event.type() == QEvent.Type.Resize:
            self._fit_to_parent()
        return False

    def paintEvent(self, event) -> None:
        p = QPainter(self)
        p.setRenderHints(QPainter.RenderHint.Antialiasing | QPainter.RenderHint.SmoothPixmapTransform)
        p.fillRect(self.rect(), QColor(0, 0, 0, int(self._DIM * self._t)))
        rect = self._target_rect()
        path = QPainterPath()
        path.addRoundedRect(rect, 10, 10)
        p.setOpacity(self._t)
        if self._pix is not None and not self._pix.isNull():
            p.save()
            p.setClipPath(path)
            p.drawPixmap(rect, self._pix, QRectF(self._pix.rect()))
            p.restore()
        else:
            p.fillPath(path, to_color(palette()['card_bg']))
        p.setPen(QPen(QColor(255, 255, 255, 40), 1))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawPath(path)
        p.end()

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton and not self._target_rect().contains(event.position()):
            self.close_lightbox()
        event.accept()

    def mouseReleaseEvent(self, event) -> None:
        event.accept()

    def mouseMoveEvent(self, event) -> None:
        event.accept()

    def mouseDoubleClickEvent(self, event) -> None:
        event.accept()

    def wheelEvent(self, event) -> None:
        event.accept()

    def keyPressEvent(self, event) -> None:
        if event.key() == Qt.Key.Key_Escape:
            self.close_lightbox()
        event.accept()

    def closeEvent(self, event) -> None:
        if not self._closing:
            event.ignore()
            self.close_lightbox()
            return
        super().closeEvent(event)

class _VideoThumb(QWidget):
    MAX_W = 280
    MAX_H = 280
    clicked = Signal(str)

    def __init__(self, video_url: str, downloader, parent=None):
        super().__init__(parent)
        self._video_url = video_url
        self._local_path: str | None = None
        self._started = False
        self._pending_play = False
        self._requested = False
        self._downloader = downloader
        w, h = (self.MAX_W, round(self.MAX_W * 9 / 16))
        self.setFixedSize(w, h)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self._w, self._h = (w, h)
        self._scene = None
        self._video_item = None
        self._view = None
        self._player = None
        self._last_state_change = 0.0
        layout.addStretch(1)
        self._layout = layout

    def _ensure_pipeline(self) -> None:
        if self._player is not None:
            return
        from PySide6.QtMultimediaWidgets import QGraphicsVideoItem
        from PySide6.QtMultimedia import QMediaPlayer
        w, h = (self._w, self._h)
        self._scene = QGraphicsScene(self)
        self._video_item = QGraphicsVideoItem()
        self._video_item.setSize(QSizeF(w, h))
        self._scene.addItem(self._video_item)
        self._view = QGraphicsView(self._scene, self)
        self._view.setFixedSize(w, h)
        self._view.setFrameShape(QGraphicsView.Shape.NoFrame)
        self._view.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._view.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._view.setRenderHint(QPainter.RenderHint.Antialiasing)
        self._view.setStyleSheet('background: transparent; border: none;')
        self._view.setSceneRect(0, 0, w, h)
        self._view.setInteractive(False)
        self._view.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._view.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        _round_widget_corners(self._view, 8)
        self._layout.insertWidget(0, self._view)
        self._player = QMediaPlayer(self)
        self._player.setVideoOutput(self._video_item)
        self._player.setLoops(QMediaPlayer.Loops.Infinite)

    def _release_pipeline(self) -> None:
        if self._player is None:
            return
        try:
            self._player.stop()
            self._player.setVideoOutput(None)
            self._player.setSource(QUrl())
        except RuntimeError:
            pass
        view = getattr(self, '_view', None)
        if view is not None:
            try:
                view.hide()
                self._layout.removeWidget(view)
                view.deleteLater()
            except RuntimeError:
                pass
        try:
            self._player.deleteLater()
        except RuntimeError:
            pass
        self._view = None
        self._video_item = None
        self._scene = None
        self._player = None
        self._local_path = None
        self._started = False
        self._requested = False

    def activate(self) -> None:
        if self._requested and self._player is not None:
            return
        self._requested = True
        self._last_state_change = time.monotonic()
        self._ensure_pipeline()
        self._downloader.video_ready.connect(self._on_video_ready, Qt.ConnectionType.UniqueConnection)
        self._downloader.request(self._video_url)

    def deactivate(self) -> None:
        if not self._requested:
            return
        self._last_state_change = time.monotonic()
        try:
            self._downloader.video_ready.disconnect(self._on_video_ready)
        except (TypeError, RuntimeError):
            pass
        self._release_pipeline()

    def _on_video_ready(self, url: str, path: str) -> None:
        if url != self._video_url or self._player is None:
            return
        self._local_path = path
        self._player.setSource(QUrl.fromLocalFile(path))
        if self._pending_play or self.isVisible():
            self._player.play()

    def showEvent(self, event) -> None:
        super().showEvent(event)
        if self._player is None or self._local_path is None:
            self._pending_play = True
            return
        if not self._started:
            self._started = True
            self._player.play()
        elif self._player.playbackState() != self._player.PlaybackState.PlayingState:
            self._player.play()

    def hideEvent(self, event) -> None:
        self.pause()
        super().hideEvent(event)

    def pause(self) -> None:
        if self._player is not None:
            self._player.pause()
        self._pending_play = False

    def resume(self) -> None:
        if not self.isVisible():
            return
        if self._player is None:
            self.activate()
            self._pending_play = True
        elif self._local_path is not None:
            self._player.play()
        else:
            self._pending_play = True

    def mousePressEvent(self, event) -> None:
        self.clicked.emit(self._video_url)
        super().mousePressEvent(event)

class VideoEnlargeDialog(MessageBoxBase):
    def __init__(self, video_url: str, downloader, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.yesButton.setText(tr('repacks.close'))
        self.hideCancelButton()
        self._video_url = video_url
        self._downloader = downloader
        from PySide6.QtMultimediaWidgets import QGraphicsVideoItem
        from PySide6.QtMultimedia import QMediaPlayer
        W, H = (900, 506)
        self._scene = QGraphicsScene(self)
        self._video_item = QGraphicsVideoItem()
        self._video_item.setSize(QSizeF(W, H))
        self._scene.addItem(self._video_item)
        self._view = QGraphicsView(self._scene, self)
        self._view.setFixedSize(W, H)
        self._view.setFrameShape(QGraphicsView.Shape.NoFrame)
        self._view.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._view.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._view.setRenderHint(QPainter.RenderHint.Antialiasing)
        self._view.setStyleSheet('background: transparent; border: none;')
        self._view.setSceneRect(0, 0, W, H)
        self._view.setInteractive(False)
        self._view.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._view.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        _round_widget_corners(self._view, 12)
        self._player = QMediaPlayer(self)
        self._player.setVideoOutput(self._video_item)
        self._player.setLoops(QMediaPlayer.Loops.Infinite)
        self.viewLayout.addWidget(self._view)
        downloader.video_ready.connect(self._on_video_ready)
        downloader.request(video_url)

    def _on_video_ready(self, url: str, path: str) -> None:
        if url != self._video_url:
            return
        self._player.setSource(QUrl.fromLocalFile(path))
        self._player.play()

    def _stop_playback(self) -> None:
        try:
            self._player.stop()
            self._player.setSource(QUrl())
        except RuntimeError:
            pass

    def closeEvent(self, event) -> None:
        self._stop_playback()
        super().closeEvent(event)

    def reject(self) -> None:
        self._stop_playback()
        super().reject()

    def keyPressEvent(self, event) -> None:
        if event.key() == Qt.Key.Key_Escape:
            self.reject()
            event.accept()
            return
        super().keyPressEvent(event)

class _ScreenshotThumb(QWidget):
    MAX_W = 280
    MAX_H = 280
    FALLBACK_H = 158
    clicked = Signal(str)

    def __init__(self, url: str, parent=None):
        super().__init__(parent)
        self._path: str | None = None
        self.setFixedSize(self.MAX_W, self.FALLBACK_H)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self._img = ImageLabel(self)
        self._img.setBorderRadius(8, 8, 8, 8)
        self._img.setFixedSize(self.MAX_W, self.FALLBACK_H)
        layout.addWidget(self._img)
        self._path = url
        images.get(url, (self.MAX_W, self.MAX_H), self._on_ready, mode=FIT, owner=self)

    def _on_ready(self, pix) -> None:
        if pix.isNull() or not _qt_is_valid(self):
            return
        dpr = pix.devicePixelRatio() or 1.0
        w, h = (pix.width() / dpr, pix.height() / dpr)
        if w > self.MAX_W:
            h = h * self.MAX_W / w
            w = self.MAX_W
        w, h = (max(1, round(w)), max(1, round(h)))
        self.setFixedSize(w, h)
        self._img.setFixedSize(w, h)
        self._img.setImage(pix)
        self._img.setScaledSize(QSize(w, h))

    def mousePressEvent(self, event) -> None:
        if self._path:
            self.clicked.emit(self._path)
        super().mousePressEvent(event)

class ScreenshotGallery(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._thumbs: list[QWidget] = []
        from src.core.repacks.video_downloader import VideoDownloader
        self._video_downloader = VideoDownloader(self)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(10)
        header = StrongBodyLabel(tr('repacks.screenshots'))
        header.setStyleSheet(f'font-size: 14px; font-weight: 600; color: {palette()['primary_text']};')
        self._header = header
        outer.addWidget(header)
        self._stack_layout = QVBoxLayout()
        self._stack_layout.setContentsMargins(0, 0, 0, 0)
        self._stack_layout.setSpacing(10)
        outer.addLayout(self._stack_layout)
        self.setVisible(False)
        register_locale_refresh(self, self._apply_locale)

    def _apply_locale(self, *_args) -> None:
        self._header.setText(tr('repacks.screenshots'))

    def set_urls(self, urls: list[str]) -> None:
        self.clear()
        if not urls:
            self.setVisible(False)
            return
        self.setVisible(True)
        ordered = sorted(urls, key=lambda u: 0 if u.startswith('video::') else 1)
        for url in ordered:
            if url.startswith('video::'):
                video_url = url[len('video::'):]
                thumb = _VideoThumb(video_url, self._video_downloader, self)
                thumb.clicked.connect(self._on_video_clicked)
            else:
                thumb = _ScreenshotThumb(url, self)
                thumb.clicked.connect(self._on_thumb_clicked)
            self._stack_layout.addWidget(thumb)
            self._thumbs.append(thumb)
        self.update_visible_videos()

    def clear(self) -> None:
        for thumb in self._thumbs:
            if isinstance(thumb, _VideoThumb):
                thumb.deactivate()
            images.release(thumb)
            self._stack_layout.removeWidget(thumb)
            thumb.deleteLater()
        self._thumbs.clear()

    def pause_videos(self) -> None:
        for thumb in self._thumbs:
            if isinstance(thumb, _VideoThumb):
                thumb.deactivate()

    def play_external(self, url: str, title: str = '') -> None:
        if not url:
            return
        dialog = VideoEnlargeDialog(url, self._video_downloader, self.window())
        if title:
            dialog.setWindowTitle(title)
        dialog.exec()

    def update_visible_videos(self) -> None:
        scroll = self._find_scroll_area()
        if scroll is None:
            for thumb in self._thumbs:
                if isinstance(thumb, _VideoThumb):
                    thumb.activate()
            return
        viewport_top = scroll.verticalScrollBar().value()
        viewport_bottom = viewport_top + scroll.viewport().height()
        margin = 300
        now = time.monotonic()
        for thumb in self._thumbs:
            if not isinstance(thumb, _VideoThumb):
                continue
            top_left = thumb.mapTo(scroll.widget(), thumb.rect().topLeft())
            thumb_top = top_left.y()
            thumb_bottom = thumb_top + thumb.height()
            if thumb_bottom >= viewport_top - margin and thumb_top <= viewport_bottom + margin:
                if not thumb._requested or now - thumb._last_state_change >= 1.0:
                    thumb.activate()
            elif thumb._player is not None and now - thumb._last_state_change >= 1.0:
                thumb.deactivate()

    def _find_scroll_area(self):
        widget = self.parentWidget()
        while widget is not None:
            if isinstance(widget, SmoothScrollArea):
                return widget
            widget = widget.parentWidget()
        return None

    def _pause_all_videos(self) -> None:
        for thumb in self._thumbs:
            if isinstance(thumb, _VideoThumb):
                thumb.pause()

    def _resume_all_videos(self) -> None:
        for thumb in self._thumbs:
            if isinstance(thumb, _VideoThumb):
                thumb.resume()

    def _on_thumb_clicked(self, path: str) -> None:
        self._pause_all_videos()
        dialog = ScreenshotEnlargeDialog(path, self.window())
        dialog.finished.connect(lambda *_: self._resume_all_videos())
        dialog.show()

    def _on_video_clicked(self, video_url: str) -> None:
        self._pause_all_videos()
        dialog = VideoEnlargeDialog(video_url, self._video_downloader, self.window())
        dialog.setModal(True)
        dialog.finished.connect(lambda *_: self._resume_all_videos())
        dialog.show()
        dialog.raise_()
        dialog.activateWindow()

class _RotatingChevron(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedSize(16, 16)
        self._angle = 0.0
        self._color = _strong_text_qcolor()

    def get_angle(self) -> float:
        return self._angle

    def set_angle(self, value: float) -> None:
        self._angle = value
        self.update()
    angle = Property(float, get_angle, set_angle)

    def set_color(self, color: QColor) -> None:
        self._color = color
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        pen = QPen(self._color)
        pen.setWidthF(1.6)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        painter.setPen(pen)
        cx, cy = (self.width() / 2, self.height() / 2)
        painter.translate(cx, cy)
        painter.rotate(self._angle)
        painter.translate(-cx, -cy)
        path = QPainterPath()
        path.moveTo(QPointF(cx - 4, cy - 2.5))
        path.lineTo(QPointF(cx, cy + 2.5))
        path.lineTo(QPointF(cx + 4, cy - 2.5))
        painter.drawPath(path)
        painter.end()

class CachedRichLabel(BodyLabel):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._hfw_cache = {}

    def heightForWidth(self, width: int) -> int:
        cached = self._hfw_cache.get(width)
        if cached is not None:
            return cached
        height = super().heightForWidth(width)
        if len(self._hfw_cache) > 8:
            self._hfw_cache.clear()
        self._hfw_cache[width] = height
        return height

    def _drop_hfw_cache(self) -> None:
        cache = getattr(self, '_hfw_cache', None)
        if cache:
            cache.clear()

    def setText(self, text: str) -> None:
        self._drop_hfw_cache()
        super().setText(text)

    def setStyleSheet(self, style: str) -> None:
        self._drop_hfw_cache()
        super().setStyleSheet(style)

    def changeEvent(self, event) -> None:
        try:
            if event.type() in (QEvent.Type.FontChange, QEvent.Type.StyleChange,
                                QEvent.Type.PaletteChange):
                self._drop_hfw_cache()
        except Exception:
            pass
        super().changeEvent(event)


class CollapsibleSection(CardWidget):
    _ANIM_MS = 190
    toggled = Signal(bool)

    def __init__(self, title: str, parent=None, expanded: bool=False):
        super().__init__(parent)
        self.setBorderRadius(12)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self._apply_card_background()
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        self._header = QWidget()
        self._header.setCursor(Qt.CursorShape.PointingHandCursor)
        self._header.setFixedHeight(50)
        self._header.setObjectName('sectionHeader')
        header_layout = QHBoxLayout(self._header)
        header_layout.setContentsMargins(16, 0, 14, 0)
        header_layout.setSpacing(10)
        self._title_lbl = StrongBodyLabel(title)
        self._title_lbl.setStyleSheet(f'font-size: 14px; font-weight: 600; background: transparent; color: {palette()['primary_text']};')
        header_layout.addWidget(self._title_lbl, 1)
        self._chevron = _RotatingChevron(self._header)
        header_layout.addWidget(self._chevron, 0, Qt.AlignmentFlag.AlignRight)
        outer.addWidget(self._header)


        self._body_wrap = QWidget()
        self._body_wrap.setObjectName('sectionBodyWrap')
        wrap_layout = QVBoxLayout(self._body_wrap)
        wrap_layout.setContentsMargins(0, 0, 0, 0)
        wrap_layout.setSpacing(0)
        self._body = QWidget()
        body_layout = QVBoxLayout(self._body)
        body_layout.setContentsMargins(16, 4, 16, 18)
        self._body_lbl = CachedRichLabel()
        self._body_lbl.setWordWrap(True)
        self._body_lbl.setTextFormat(Qt.TextFormat.RichText)
        self._body_lbl.setOpenExternalLinks(True)
        self._body_lbl.setStyleSheet(f'font-size: 14px; font-weight: 500; line-height: {_DESC_LINE_HEIGHT_PCT}%; color: {_body_text_color()}; background: transparent;')
        body_layout.addWidget(self._body_lbl)
        wrap_layout.addWidget(self._body)
        outer.addWidget(self._body_wrap)

        self._header.mousePressEvent = lambda _e: self.toggle()
        self._anim = QVariantAnimation(self)
        self._anim.setDuration(self._ANIM_MS)
        self._anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._anim.valueChanged.connect(self._on_wrap_height)
        self._anim.finished.connect(self._on_anim_finished)
        self._chevron_anim = QPropertyAnimation(self._chevron, b'angle')
        self._chevron_anim.setDuration(self._ANIM_MS)
        self._chevron_anim.setEasingCurve(QEasingCurve.Type.OutCubic)

        self._hovering = False
        self._expanded = expanded
        self._raw_body_text = ''
        self._body_is_features = False
        self._current_html = ''
        self._body.setFixedHeight(self._content_height())
        self._body_wrap.setFixedHeight(self._content_height() if expanded else 0)
        self._chevron.set_angle(180.0 if expanded else 0.0)
        self._update_header_style()
        qconfig.themeChanged.connect(self._on_theme_changed)
        self.destroyed.connect(self._disconnect_theme_signal)

    def _disconnect_theme_signal(self, *_args) -> None:
        try:
            qconfig.themeChanged.disconnect(self._on_theme_changed)
        except TypeError:
            pass

    def _on_theme_changed(self, *_args) -> None:
        self._refresh_body_html()
        self._update_header_style()
        QTimer.singleShot(0, self._update_header_style)

    def enterEvent(self, event):
        self._hovering = True
        self._update_header_style()
        super().enterEvent(event)

    def leaveEvent(self, event):
        self._hovering = False
        self._update_header_style()
        super().leaveEvent(event)

    def focusInEvent(self, event):
        self._hovering = True
        self.isHover = True
        self._update_header_style()
        self._updateBackgroundColor()
        super().focusInEvent(event)

    def focusOutEvent(self, event):
        self._hovering = self.underMouse()
        self.isHover = self._hovering
        self._update_header_style()
        self._updateBackgroundColor()
        super().focusOutEvent(event)

    def keyPressEvent(self, event):
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_Space) and \
                event.modifiers() in (Qt.KeyboardModifier.NoModifier, Qt.KeyboardModifier.KeypadModifier):
            self.toggle()
            event.accept()
            return
        super().keyPressEvent(event)

    def set_body_text(self, text: str) -> None:
        lines = [ln.strip() for ln in (text or '').split('\n') if ln.strip()]
        looks_like_features = bool(lines) and all((ln.startswith(('•', '- ', '* ')) for ln in lines))
        self._raw_body_text = text or ''
        self._body_is_features = looks_like_features
        if looks_like_features:
            html = render_features_html(text)
        else:
            html = render_description_html(text)
        self.set_body_html(html)

    def _refresh_body_html(self) -> None:
        if not self._raw_body_text:
            return
        html = render_features_html(self._raw_body_text) if self._body_is_features else render_description_html(self._raw_body_text)
        if html != self._current_html:
            self.set_body_html(html)

    def _content_height(self) -> int:
        width = self._body_lbl.width()
        if width <= 0:
            width = self.width() - 32 if self.width() > 32 else 268
        h = self._body_lbl.heightForWidth(width)
        if h <= 0:
            h = self._body_lbl.sizeHint().height()
        return h + 22

    def _apply_body_height(self) -> None:
        height = self._content_height()
        if abs(self._body.height() - height) > 1:
            self._body.setFixedHeight(height)

    def set_body_html(self, html: str) -> None:
        self._current_html = html or ''
        self._body_lbl.setText(html)
        self._apply_body_height()
        self._sync_wrap_height()

    def _anim_running(self) -> bool:
        try:
            return self._anim.state() == QAbstractAnimation.State.Running
        except Exception:
            return False

    def _sync_wrap_height(self) -> None:
        if self._anim_running():
            return
        target = self._content_height() if self._expanded else 0
        if abs(self._body_wrap.height() - target) > 1:
            self._body_wrap.setFixedHeight(target)

    def resizeEvent(self, event):
        super().resizeEvent(event)

        if not self._anim_running():
            self._sync_wrap_height()

    def toggle(self) -> None:
        self._set_expanded(not self._expanded)

    def _set_expanded(self, expanded: bool) -> None:
        self._expanded = expanded
        self.toggled.emit(expanded)
        self._apply_body_height()
        target = self._content_height() if expanded else 0
        start = self._body_wrap.height()
        if start == target:
            self._anim.stop()
            self._body_wrap.setFixedHeight(target)
        else:
            self._anim.stop()
            self._anim.setStartValue(start)
            self._anim.setEndValue(target)
            self._anim.start()
        self._chevron_anim.stop()
        self._chevron_anim.setStartValue(self._chevron.get_angle())
        self._chevron_anim.setEndValue(180.0 if expanded else 0.0)
        self._chevron_anim.start()
        self._update_header_style()

    def _on_wrap_height(self, value) -> None:
        self._body_wrap.setFixedHeight(int(value))

    def _on_anim_finished(self) -> None:
        self._sync_wrap_height()

    def _apply_card_background(self) -> None:
        self.update()

    def _card_bg_color(self) -> QColor:
        return _qcolor_from_palette(palette()['section_card_bg'])

    def _card_border_color(self) -> QColor:
        return _qcolor_from_palette(palette()['section_card_border'])

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        path = QPainterPath()
        r = float(self.getBorderRadius())
        path.addRoundedRect(QRectF(0.5, 0.5, self.width() - 1, self.height() - 1), r, r)
        painter.fillPath(path, self._card_bg_color())
        painter.setPen(self._card_border_color())
        painter.drawPath(path)
        painter.end()
        super().paintEvent(event)

    def _update_header_style(self) -> None:
        self._apply_card_background()
        self._title_lbl.setStyleSheet(f'font-size: 14px; font-weight: 600; background: transparent; color: {palette()['primary_text']};')
        self._chevron.set_color(_strong_text_qcolor())
        self._body_lbl.setStyleSheet(f'font-size: 14px; font-weight: 500; line-height: {_DESC_LINE_HEIGHT_PCT}%; color: {_body_text_color()}; background: transparent;')
        if self._hovering:
            self._header.setStyleSheet(f'#sectionHeader {{ background: {_hover_tint_color()}; border-top-left-radius: 12px; border-top-right-radius: 12px; }}')
        else:
            self._header.setStyleSheet('#sectionHeader { background: transparent; }')

_URL_RE = re.compile('(https?://[^\\s<>\\"]+)')
_HTTP_URL_RE = re.compile('(?i)^https?://')


def open_external_url(url: str) -> bool:
    url = (url or '').strip()
    if not _HTTP_URL_RE.match(url):
        return False
    return bool(QDesktopServices.openUrl(QUrl(url)))

def _escape_html(text: str) -> str:
    return text.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')

def _linkify_inline(text: str) -> str:
    def _wrap(match: 're.Match[str]') -> str:
        url = match.group(1)
        return f'<a href="{url}" style="color:{themeColor().name()};">{url}</a>'
    return _URL_RE.sub(_wrap, text)

_DESC_PARAGRAPH_MARGIN_PX = 22
_DESC_LIST_ITEM_SPACING_PX = 8
_DESC_LINE_HEIGHT_PCT = 170
_UPDATE_SEPARATOR_WORDS = {'or', 'and then'}

def render_game_updates_html(raw_html: str) -> str:
    if not raw_html:
        return ''
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(raw_html, 'html.parser')
    color = themeColor().name()
    for ol in soup.find_all('ol'):
        ol['style'] = f'margin:0 0 {_DESC_PARAGRAPH_MARGIN_PX}px 0; padding-left:22px;'
    for li in soup.find_all('li'):
        li['style'] = f'margin-bottom:{_DESC_LIST_ITEM_SPACING_PX}px; line-height:{_DESC_LINE_HEIGHT_PCT}%;'
    for p in soup.find_all('p'):
        text = p.get_text(' ', strip=True).lower()
        is_separator = len(text) <= 80 and any((text == w or text.startswith(w + ' ') or text.endswith(' ' + w) or (f' {w} ' in text) for w in _UPDATE_SEPARATOR_WORDS))
        if is_separator:
            p['style'] = f'margin:{_DESC_LIST_ITEM_SPACING_PX}px 0 {_DESC_PARAGRAPH_MARGIN_PX}px 0; font-style:italic; color:{_muted_text_color()};'
        else:
            p['style'] = f'margin:0 0 {_DESC_PARAGRAPH_MARGIN_PX}px 0;'
    for a in soup.find_all('a'):
        href = (a.get('href') or '').strip()
        if not _HTTP_URL_RE.match(href):
            a.attrs = {}
            continue
        a.attrs = {'href': href, 'style': f'color:{color}; text-decoration:underline; word-wrap:break-word; overflow-wrap:break-word; word-break:break-all;'}
    container = soup.find('div')
    body = container.decode_contents() if container is not None else soup.decode_contents()
    return f'<div style="line-height:{_DESC_LINE_HEIGHT_PCT}%; word-wrap:break-word; overflow-wrap:break-word; word-break:break-all;">{body}</div>'

_DESC_SIZE_NOISE_RE = re.compile(
    r'^(?:\[\s*)?(?:from\s+)?\d+(?:[.,]\d+)?(?:\s*(?:~|～|–|—|-|to)\s*\d+(?:[.,]\d+)?)?'
    r'\s*(?:GB|MB|TB)(?:\s*(?:GB|MB|TB))?(?:\s*\[?\s*Selective\s+Download\s*\]?)?\s*\]?\.?$',
    re.IGNORECASE,
)
_DESC_META_NOISE_RE = re.compile(
    r'^(?:genres?/tags?|companies?|languages?|original\s+size|repack\s+size|final\s+size)\s*:'
    r'|^\[?\s*Selective\s+Download\s*\]?$',
    re.IGNORECASE,
)


def _desc_noise_line(line: str) -> bool:
    candidate = (line or '').replace('\x00', ' ').strip()
    candidate = re.sub('^[•*-]\\s*', '', candidate).strip()
    if not candidate:
        return True
    return bool(_DESC_SIZE_NOISE_RE.match(candidate) or _DESC_META_NOISE_RE.match(candidate))


def render_description_html(text: str) -> str:
    if not text:
        return ''
    blocks = re.split('\\n\\s*\\n', text.strip())
    html_parts: list[str] = []

    def _flush_prose(prose_lines: list[str]) -> None:
        prose_lines = [line for line in prose_lines if not _desc_noise_line(line)]
        if not prose_lines:
            return
        joined = '\n'.join(prose_lines).strip()
        if not joined:
            return
        if joined.startswith('## '):
            heading_text = _linkify_inline(_escape_html(joined[3:].strip()))
            html_parts.append(f'<p style="margin:0 0 {_DESC_PARAGRAPH_MARGIN_PX}px 0; font-weight:600; font-size:16px;">{heading_text}</p>')
            return
        para_text = _linkify_inline(_escape_html(joined)).replace('\n', '<br>')
        html_parts.append(f'<p style="margin:0 0 {_DESC_PARAGRAPH_MARGIN_PX}px 0;">{para_text}</p>')

    def _flush_list(bullet_lines: list[str]) -> None:
        if not bullet_lines:
            return
        items = []
        for ln in bullet_lines:
            if _desc_noise_line(ln):
                continue
            item_text = re.sub('^[•*-]\\s*', '', ln.strip())
            if not item_text:
                continue
            content = _linkify_inline(_escape_html(item_text))
            items.append(f'<li style="margin-bottom:{_DESC_LIST_ITEM_SPACING_PX}px;">{content}</li>')
        if items:
            html_parts.append(f'<ul style="margin:0 0 {_DESC_PARAGRAPH_MARGIN_PX}px 0; padding-left:22px;">' + ''.join(items) + '</ul>')
    for block in blocks:
        block = block.strip('\n')
        if not block.strip():
            continue
        pending_prose: list[str] = []
        pending_bullets: list[str] = []
        for raw_line in block.split('\n'):
            line = raw_line.strip()
            if not line:
                continue
            if line.startswith(('•', '- ', '* ')):
                if pending_prose:
                    _flush_prose(pending_prose)
                    pending_prose = []
                pending_bullets.append(line)
            else:
                if pending_bullets:
                    _flush_list(pending_bullets)
                    pending_bullets = []
                pending_prose.append(line)
        if pending_bullets:
            _flush_list(pending_bullets)
        if pending_prose:
            _flush_prose(pending_prose)
    if html_parts:
        last = html_parts[-1]
        html_parts[-1] = re.sub('margin:0 0 \\d+px 0', 'margin:0', last, count=1)
    return f'<div style="word-wrap:break-word; overflow-wrap:break-word; word-break:break-all;">{''.join(html_parts)}</div>'
_FEATURE_BLOCK_SPACING_PX = 18

def render_features_html(text: str) -> str:
    if not text:
        return ''
    blocks = re.split('\\n\\s*\\n', text.strip())
    html_parts: list[str] = []

    def _render_list(bullet_lines: list[str]) -> str:
        items_html: list[str] = []
        for raw_line in bullet_lines:
            item_text = re.sub('^[•*-]\\s*', '', raw_line.strip())
            if not item_text:
                continue
            content = _linkify_inline(_escape_html(item_text))
            items_html.append(f'<li style="margin-bottom:{_FEATURE_BLOCK_SPACING_PX}px;">{content}</li>')
        if not items_html:
            return ''
        items_html[-1] = items_html[-1].replace(f'margin-bottom:{_FEATURE_BLOCK_SPACING_PX}px;', 'margin-bottom:0px;', 1)
        return '<ul style="margin:0 0 ' + str(_FEATURE_BLOCK_SPACING_PX) + 'px 0; padding-left:20px;">' + ''.join(items_html) + '</ul>'
    for block in blocks:
        block = block.strip('\n')
        if not block.strip():
            continue
        pending_prose: list[str] = []
        pending_bullets: list[str] = []
        for raw_line in block.split('\n'):
            line = raw_line.strip()
            if not line:
                continue
            if line.startswith(('•', '- ', '* ')):
                if pending_prose:
                    joined = ' '.join(pending_prose).strip()
                    if joined:
                        html_parts.append(f'<p style="margin:0 0 {_FEATURE_BLOCK_SPACING_PX}px 0; font-weight:600;">{_linkify_inline(_escape_html(joined))}</p>')
                    pending_prose = []
                pending_bullets.append(line)
            else:
                if pending_bullets:
                    rendered = _render_list(pending_bullets)
                    if rendered:
                        html_parts.append(rendered)
                    pending_bullets = []
                pending_prose.append(line)
        if pending_bullets:
            rendered = _render_list(pending_bullets)
            if rendered:
                html_parts.append(rendered)
        if pending_prose:
            joined = ' '.join(pending_prose).strip()
            if joined:
                html_parts.append(f'<p style="margin:0 0 {_FEATURE_BLOCK_SPACING_PX}px 0; font-weight:600;">{_linkify_inline(_escape_html(joined))}</p>')
    if not html_parts:
        return ''
    last = html_parts[-1]
    html_parts[-1] = re.sub('margin(?:-bottom)?:0(?:px)? 0 \\d+px 0;?', 'margin:0;', last, count=1)
    html_parts[-1] = re.sub('margin-bottom:\\d+px;">([^<]*)</li>$', 'margin-bottom:0px;">\\1</li>', html_parts[-1])
    return f'<div style="word-wrap:break-word; overflow-wrap:break-word; word-break:break-all;">{''.join(html_parts)}</div>'
_BACKWARDS_COMPAT_RE = re.compile('(?im)^\\s*This repack (?:IS|is not|IS NOT|is)\\b.*backwards compatible.*$')

def _extract_trailing_backwards_compat(intro_text: str) -> tuple[str, str]:
    if not intro_text:
        return (intro_text, '')
    lines = intro_text.split('\n')
    note_lines: list[str] = []
    while lines and (not lines[-1].strip() or _BACKWARDS_COMPAT_RE.match(lines[-1].strip())):
        line = lines.pop()
        stripped = line.strip()
        if stripped:
            note_lines.insert(0, stripped)
    if not note_lines:
        return (intro_text, '')
    cleaned = '\n'.join(lines).rstrip()
    note = ' '.join(note_lines).strip()
    return (cleaned, note)

_BULLETS = ('•', '- ', '* ')


def _peel_list_groups(text: str) -> tuple[str, list[tuple[str, str]]]:
    if not text or not any(ln.lstrip().startswith(_BULLETS) for ln in text.split('\n')):
        return (text, [])
    lines = text.split('\n')
    keep: list[str] = []
    groups: list[tuple[str, str]] = []
    i = 0
    n = len(lines)

    def _next_nonblank(k: int) -> int:
        while k < n and not lines[k].strip():
            k += 1
        return k

    while i < n:
        head = lines[i].strip()
        if head and not head.startswith(_BULLETS) and len(head) <= 100 and not head.endswith(('.', '!', '?')):
            j = _next_nonblank(i + 1)
            if j < n and lines[j].strip().startswith(_BULLETS):
                body: list[str] = []
                while j < n:
                    cur = lines[j].strip()
                    if cur.startswith(_BULLETS):
                        body.append(cur)
                        j += 1
                        continue
                    k = _next_nonblank(j)
                    if not cur and k < n and lines[k].strip().startswith(_BULLETS):
                        j = k
                        continue
                    break
                groups.append((head.rstrip(':').strip(), '\n'.join(body)))
                i = j
                continue
        keep.append(lines[i])
        i += 1
    return (re.sub('\\n{3,}', '\n\n', '\n'.join(keep)).strip(), groups)


def _merge_sections(sections: list[tuple[str, str]]) -> list[tuple[str, str]]:
    def _norm(value: str) -> str:
        return ' '.join(re.sub('^[•*-]\\s*', '', value.strip()).casefold().split())

    merged: dict[str, tuple[str, list[str]]] = {}
    seen: set[str] = set()
    for heading, body in sections:
        key = _norm(heading)
        _h, lines = merged.setdefault(key, (heading, []))
        for line in (body or '').split('\n'):
            norm = _norm(line)
            if not norm:
                if lines and lines[-1]:
                    lines.append('')
                continue
            if norm in seen and line.strip().startswith(_BULLETS):
                continue
            seen.add(norm)
            lines.append(line)
    out = []
    for heading, lines in merged.values():
        body = '\n'.join(lines).strip()
        if body:
            out.append((heading, body))
    return out


def _split_description_sections(description: str) -> list[tuple[str, str]]:
    if not description:
        return []
    blocks = re.split('\\n\\n(?=## )', description)
    sections: list[tuple[str, str]] = []
    for block in blocks:
        block = block.strip()
        if not block:
            continue
        if block.startswith('## '):
            newline_idx = block.find('\n')
            if newline_idx == -1:
                heading, body = (block[3:].strip(), '')
            else:
                heading, body = (block[3:newline_idx].strip(), block[newline_idx + 1:].strip())
        else:
            heading, body = ('', block)
        sections.append((heading, body))
    return sections
_BONUS_FILE_PREFIX = 'fg-optional'
_BONUS_PATH_TOKENS = ('optional', 'selective')

class SelectiveDownloadEntry:
    __slots__ = ('label', 'category', 'patterns', 'required', 'size_hint', 'size_bytes', 'file_index')

    def __init__(self, label: str, category: str, patterns: list[str], required: bool=False, size_hint: str='', size_bytes: int=0, file_index: int=0):
        self.label = label
        self.category = category
        self.patterns = patterns
        self.required = required
        self.size_hint = size_hint
        self.size_bytes = size_bytes
        self.file_index = file_index

def _human_size(num_bytes: int) -> str:
    size = float(num_bytes)
    for unit in ('B', 'KB', 'MB', 'GB', 'TB'):
        if size < 1024.0:
            return f'{size:.1f} {unit}' if unit != 'B' else f'{int(size)} {unit}'
        size /= 1024.0
    return f'{size:.1f} PB'

def build_selective_entries_from_torrent(torrent_files: list[str], file_sizes: list[int] | None=None) -> list[SelectiveDownloadEntry]:
    entries: list[SelectiveDownloadEntry] = []
    if not torrent_files:
        return entries
    for i, path in enumerate(torrent_files):
        display_name = path.replace('\\', '/').split('/')[-1] or path
        path_norm = path.replace('\\', '/').lower()
        size_bytes = file_sizes[i] if file_sizes and i < len(file_sizes) else 0
        size_hint = _human_size(size_bytes)
        is_bonus = display_name.lower().startswith(_BONUS_FILE_PREFIX) or any((tok in path_norm for tok in _BONUS_PATH_TOKENS))
        required = not is_bonus
        category = 'Bonus Content' if is_bonus else 'Required'
        entries.append(SelectiveDownloadEntry(label=display_name, category=category, patterns=[path], required=required, size_hint=size_hint, size_bytes=size_bytes, file_index=i + 1))
    return entries

def resolve_selective_file_indices(entries: list[SelectiveDownloadEntry], selected_labels: set[str], torrent_files: list[str]) -> list[int]:
    wanted = [e for e in entries if e.label in selected_labels]
    indices: set[int] = set()
    for entry in wanted:
        if entry.file_index:
            indices.add(entry.file_index)
    return sorted(indices)

class _SourceOptionCard(CardWidget):
    clicked_source = Signal(str)

    def __init__(self, key: str, display_name: str, magnet_url: str | None, parent=None):
        super().__init__(parent)
        self._key = key
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        text_col = QVBoxLayout()
        text_col.setSpacing(2)
        name_lbl = StrongBodyLabel(display_name)
        text_col.addWidget(name_lbl)
        if magnet_url:
            magnet_name = magnet_display_name(magnet_url)
            if magnet_name:
                sub_text = magnet_name if len(magnet_name) <= 60 else magnet_name[:57] + '...'
            else:
                sub_text = 'Magnet link available'
        else:
            sub_text = 'No magnet link found'
        sub_lbl = CaptionLabel(sub_text)
        sub_lbl.setWordWrap(True)
        sub_lbl.setStyleSheet(f'color: {_muted_text_color()};')
        text_col.addWidget(sub_lbl)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(16, 10, 16, 10)
        layout.setSpacing(4)
        layout.addLayout(text_col, 1)
        self.setMinimumHeight(64)
        if not magnet_url:
            self.setEnabled(False)
            self.setCursor(Qt.CursorShape.ArrowCursor)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton and self.isEnabled():
            self.clicked_source.emit(self._key)
        super().mousePressEvent(event)

def _norm_source(text) -> str:
    return re.sub(r'[^a-z0-9]', '', str(text or '').lower())

def _preferred_source_key(download_sources: dict) -> str | None:
    from src.core.config import settings as _s
    preferred = _norm_source(getattr(_s, 'repack_preferred_source', 'ask'))
    if not preferred or preferred == 'ask':
        return None
    for key, data in download_sources.items():
        if data.get('magnet_url') and preferred in _norm_source(f"{key}{data.get('name', '')}"):
            return key
    return None

class SourceSelectDialog(MessageBoxBase):
    def __init__(self, download_sources: dict, parent=None):
        super().__init__(parent)
        self._chosen_key: str | None = None
        self.titleLabel = SubtitleLabel(tr('repacks.choose_download_source'), self)
        self.viewLayout.addWidget(self.titleLabel)
        info = CaptionLabel(tr('repacks.pick_mirror_hint'), self)
        info.setWordWrap(True)
        info.setStyleSheet(f'color: {_muted_text_color()};')
        self.viewLayout.addWidget(info)
        for key, data in download_sources.items():
            card = _SourceOptionCard(key, data.get('name', key), data.get('magnet_url'), self)
            card.clicked_source.connect(self._on_source_clicked)
            self.viewLayout.addWidget(card)
        self.yesButton.setVisible(False)
        self.cancelButton.setText(tr('repacks.cancel'))
        self.widget.setMinimumWidth(420)

    def _on_source_clicked(self, key: str) -> None:
        self._chosen_key = key
        self.accept()

    def chosen_source_key(self) -> str | None:
        return self._chosen_key

def external_torrent_client_enabled() -> bool:
    from src.core.config import settings as _s
    return bool(getattr(_s, 'external_torrent_client', False))

def send_to_external_torrent_client(source: str, parent=None) -> bool:
    if not external_torrent_client_enabled():
        return False
    url = QUrl(source) if source.startswith('magnet:') else QUrl.fromLocalFile(source)
    ok = QDesktopServices.openUrl(url)
    if ok:
        InfoBar.success(title=tr('settings.sent_to_external_client_title'), content=tr('settings.sent_to_external_client_content'), orient=Qt.Orientation.Horizontal, isClosable=True, position=InfoBarPosition.TOP_RIGHT, duration=3000, parent=parent)
    else:
        InfoBar.error(title=tr('settings.external_client_failed_title'), content=tr('settings.external_client_failed_content'), orient=Qt.Orientation.Horizontal, isClosable=True, position=InfoBarPosition.TOP_RIGHT, duration=4000, parent=parent)
    return True

class SelectiveDownloadDialog(MessageBoxBase):
    _ORDERED_CATEGORIES = ('Required', 'Bonus Content')

    def __init__(self, entries: list[SelectiveDownloadEntry], parent=None, subtitle: str | None=None,
                 context: str = 'other'):
        super().__init__(parent)
        self._entries = entries
        self._checkboxes: dict[str, QTreeWidgetItem] = {}
        self.titleLabel = SubtitleLabel(tr('repacks.choose_files_to_download'), self)
        self.viewLayout.addWidget(self.titleLabel)
        if subtitle:
            name_lbl = CaptionLabel(subtitle, self)
            name_lbl.setWordWrap(True)
            name_lbl.setStyleSheet(f'color: {_muted_text_color()};')
            self.viewLayout.addWidget(name_lbl)
        safety = assess_source_files([entry.patterns[0] if entry.patterns else entry.label for entry in entries],
                                     context=context)
        info = CaptionLabel(tr('repacks.required_files_hint'), self)
        info.setWordWrap(True)
        info.setStyleSheet(f'color: {_muted_text_color()};')
        if safety.level != 'ok':
            info.setText(f"{info.text()}\n{safety.summary}. {tr('repacks.file_type_review')}")
            info.setToolTip(safety.details)
        self.viewLayout.addWidget(info)
        self._tree = TreeWidget(self)
        self._tree.setHeaderLabels([tr('repacks.include'), tr('repacks.file_type'), tr('repacks.size')])
        self._tree.header().setStretchLastSection(False)
        self._tree.header().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self._tree.header().setSectionResizeMode(1, QHeaderView.ResizeMode.Fixed)
        self._tree.header().setSectionResizeMode(2, QHeaderView.ResizeMode.Fixed)
        self._tree.setColumnWidth(1, 72)
        self._tree.setColumnWidth(2, 92)
        self._tree.setTextElideMode(Qt.TextElideMode.ElideRight)
        self._name_delegate = ScrollingTextDelegate(self._tree, 0, padding=4)
        self._tree.setRootIsDecorated(True)
        self._tree.setBorderVisible(True)
        self._tree.setBorderRadius(8)
        self._tree.setMinimumHeight(320)
        self.viewLayout.addWidget(self._tree)
        highlighted = {name.lower() for name in safety.highlighted}
        pal = palette()
        by_category: dict[str, list[SelectiveDownloadEntry]] = {}
        for e in entries:
            by_category.setdefault(e.category, []).append(e)
        _category_labels = {'Required': tr('repacks.required'), 'Bonus Content': tr('repacks.bonus_content')}
        for cat in self._ORDERED_CATEGORIES:
            if cat not in by_category:
                continue
            cat_item = QTreeWidgetItem([_category_labels.get(cat, cat), '', ''])
            cat_item.setFlags(cat_item.flags() & ~Qt.ItemFlag.ItemIsUserCheckable)
            self._tree.addTopLevelItem(cat_item)
            font = self._tree.font()
            font.setBold(True)
            cat_item.setFont(0, font)
            for entry in by_category[cat]:
                path = entry.patterns[0] if entry.patterns else entry.label
                file_info = classify_file(path)
                child = QTreeWidgetItem([entry.label, file_info.label, entry.size_hint])
                child.setToolTip(1, file_info.reason)
                if os.path.basename(path.replace('\\', '/')).lower() in highlighted:
                    color = pal['state_error'] if file_info.risk == 'dangerous' else pal['state_verifying']
                    child.setForeground(1, QBrush(QColor(color)))
                child.setFlags(child.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                child.setCheckState(0, Qt.CheckState.Checked)
                cat_item.addChild(child)
                self._checkboxes[entry.label] = child
            cat_item.setExpanded(True)
        self._size_by_label: dict[str, int] = {e.label: e.size_bytes for e in entries}
        self._total_lbl = StrongBodyLabel('', self)
        self.viewLayout.addWidget(self._total_lbl)
        self._tree.itemChanged.connect(self._update_total)
        self._update_total()
        self.yesButton.setText(tr('repacks.ok'))
        self.cancelButton.setText(tr('repacks.cancel'))
        self.widget.setMinimumWidth(680)

    def _update_total(self, *_args) -> None:
        total = 0
        for label, item in self._checkboxes.items():
            if item.checkState(0) == Qt.CheckState.Checked:
                total += self._size_by_label.get(label, 0)
        self._total_lbl.setText(tr('repacks.total_download_size', size=_human_size(total)))

    def selected_labels(self) -> set[str]:
        selected = set()
        for label, item in self._checkboxes.items():
            if item.checkState(0) == Qt.CheckState.Checked:
                selected.add(label)
        return selected

def _guarded(method):
    n_params = method.__code__.co_argcount - 1

    @wraps(method)
    def wrapper(self, *args, **kwargs):
        try:
            return method(self, *args[:n_params], **kwargs)
        except Exception as exc:
            logger.exception('Download step %s failed', method.__name__)
            self._recover_from_error(exc)
    return wrapper

class _FileListFetchThread(QThread):
    finished_ok = Signal(list)
    finished_err = Signal(str)

    def __init__(self, manager: DownloadManager, source: str, parent=None, category: str='repacks'):
        super().__init__(parent)
        self._manager = manager
        self._source = source
        self._category = category

    def run(self) -> None:
        try:
            files = self._manager.fetch_file_list(self._source, category=self._category)
        except Exception as exc:
            self.finished_err.emit(str(exc))
            return
        self.finished_ok.emit(files)

class _SetupExeFetchThread(QThread):
    finished_ok = Signal(str)
    finished_err = Signal(str)
    progress = Signal(int, int)

    def __init__(self, manager: DownloadManager, source: str, parent=None, category: str='repacks'):
        super().__init__(parent)
        self._manager = manager
        self._source = source
        self.source = source
        self._category = category
        self._cancelled = threading.Event()

    def cancel(self) -> None:
        self._cancelled.set()

    def run(self) -> None:
        try:
            path = self._manager.fetch_setup_exe(self._source, category=self._category, should_cancel=self._cancelled.is_set, progress_cb=lambda done, total: self.progress.emit(done, total))
        except Exception as exc:
            self.finished_err.emit(str(exc))
            return
        self.finished_ok.emit(path or '')

class DownloadActionWidget(AccentColorMixin, QWidget):
    download_started = Signal()

    def __init__(self, manager: DownloadManager, source_key: str | None=None, parent=None):
        super().__init__(parent)
        self._manager = manager
        self._source_key = source_key
        self._details: RepackDetails | None = None
        self._item_id: str | None = None
        self._filelist_thread: _FileListFetchThread | None = None
        self._orphaned_filelist_threads: list[_FileListFetchThread] = []
        self._setup_thread: _SetupExeFetchThread | None = None
        self._pending_queue_args: tuple | None = None
        self._setup_cache: dict[str, str | None] = {}
        self._setup_retry_counts: dict[str, int] = {}
        self._queued_download_sources: set = set()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)
        self._button = PrimaryPushButton(tr('repacks.download'))
        self._button.setFixedHeight(32)
        self._button.clicked.connect(self._on_click)
        layout.addWidget(self._button)
        self._manager.item_updated.connect(self._on_item_updated)
        self._manager.item_removed.connect(self._on_item_removed)
        self._current_accent_color: QColor | None = None
        qconfig.themeColorChanged.connect(self._deferred_reapply_accent_color)
        qconfig.themeChanged.connect(self._deferred_reapply_accent_color)

    def set_details(self, details: RepackDetails) -> None:
        self._details = details
        self._item_id = None
        self._pending_queue_args = None
        self._queued_download_sources = set()
        self._setup_retry_counts = {}
        self._cancel_setup_thread()
        self._button.setText(tr('repacks.download'))
        self._button.setEnabled(True)
        self._setup_cache = {}
        self._maybe_prefetch_setup_exe_silently()

    def _recover_from_error(self, exc: BaseException) -> None:
        self._pending_queue_args = None
        if self._item_id is None:
            self._button.setText(tr('repacks.download'))
            self._button.setEnabled(True)
        try:
            title = tr('repacks.download_failed')
            if not title or title == 'repacks.download_failed':
                title = 'Download failed'
            InfoBar.error(title=title, content=str(exc) or exc.__class__.__name__, orient=Qt.Orientation.Horizontal, isClosable=True, position=InfoBarPosition.TOP_RIGHT, duration=6000, parent=self.window())
        except Exception:
            logger.exception('Could not show download error')

    def _cancel_setup_thread(self, only_source: str | None=None) -> None:
        thread = self._setup_thread
        if thread is None or (only_source is not None and thread.source != only_source):
            return
        self._setup_thread = None
        for sig in (thread.finished_ok, thread.finished_err, thread.finished, thread.progress):
            try:
                sig.disconnect()
            except (TypeError, RuntimeError):
                pass
        try:
            if thread.isRunning():
                thread.cancel()
                self._orphaned_filelist_threads.append(thread)
                thread.finished.connect(lambda t=thread: self._cleanup_orphaned_thread(t))
            else:
                thread.deleteLater()
        except RuntimeError:
            pass

    def _maybe_prefetch_setup_exe_silently(self) -> None:
        if self._source_key != 'fitgirl' or self._details is None:
            return
        if not _auto_install_enabled():
            logger.info('setup.exe prefetch skipped for %r: auto install is disabled', getattr(self._details, 'title', ''))
            return
        if not self._details.extra.get('selective_download'):
            logger.info('setup.exe prefetch skipped for %r: not a Selective Download release, nothing optional to inspect', getattr(self._details, 'title', ''))
            return
        if self._details.extra.get('download_sources'):
            logger.info('setup.exe prefetch deferred for %r: multiple download_sources, waiting for user pick', getattr(self._details, 'title', ''))
            return
        magnet_url = self._details.extra.get('magnet_url')
        torrent_url = self._details.extra.get('torrent_url')
        source = magnet_url or torrent_url
        if not source:
            logger.info('setup.exe prefetch skipped for %r: no magnet_url/torrent_url in details.extra', getattr(self._details, 'title', ''))
            return
        logger.info('setup.exe prefetch starting silently for %r (source=%s)', getattr(self._details, 'title', ''), source)
        self._start_setup_exe_prefetch(source, silent=True)

    def _reapply_accent_color(self) -> None:
        color = self._current_accent_color
        if color is None:
            color = self._default_button_color()
        luminance = 0.299 * color.red() + 0.587 * color.green() + 0.114 * color.blue()
        text_color = '#1a1a1a' if luminance > 150 else '#ffffff'
        hover = color.lighter(112)
        pressed = color.darker(112)
        self._button.setStyleSheet(f'PrimaryPushButton {{ background-color: rgb({color.red()}, {color.green()}, {color.blue()}); color: {text_color}; border: none; border-radius: 8px; }} PrimaryPushButton:hover {{ background-color: rgb({hover.red()}, {hover.green()}, {hover.blue()}); color: {text_color}; }} PrimaryPushButton:pressed {{ background-color: rgb({pressed.red()}, {pressed.green()}, {pressed.blue()}); color: {text_color}; }} PrimaryPushButton:disabled {{ background-color: rgb({color.red()}, {color.green()}, {color.blue()}); color: {text_color}; }}')

    @_guarded
    def _on_click(self) -> None:
        if self._details is None or self._item_id is not None:
            return
        download_sources = self._details.extra.get('download_sources') or {}
        if download_sources:
            key = _preferred_source_key(download_sources)
            if key is None:
                dialog = SourceSelectDialog(download_sources, parent=self.window())
                if dialog.exec() != QDialog.DialogCode.Accepted:
                    return
                key = dialog.chosen_source_key()
                if key is None:
                    return
            chosen = download_sources.get(key) or {}
            magnet_url = chosen.get('magnet_url')
            if not magnet_url:
                self._button.setText(tr('repacks.no_magnet_found_short'))
                self._button.setEnabled(False)
                return
            self.download_started.emit()
            if send_to_external_torrent_client(magnet_url, parent=self.window()):
                return
            self._start_selective_download(magnet_url)
            return
        magnet_url = self._details.extra.get('magnet_url')
        torrent_url = self._details.extra.get('torrent_url')
        source = magnet_url or torrent_url
        if not source:
            download_links = self._details.extra.get('download_links') or []
            if download_links:
                self.download_started.emit()
                if len(download_links) == 1:
                    self._start_ddl_download(download_links[0])
                else:
                    self._open_ddl_menu(download_links)
                return
            self._button.setText(tr('repacks.no_link_found'))
            self._button.setEnabled(False)
            return
        self.download_started.emit()
        if send_to_external_torrent_client(source, parent=self.window()):
            return
        self._start_selective_download(source)

    def _open_ddl_menu(self, links: list[dict]) -> None:
        menu = _mica_menu(self)
        for link in links:
            if not link.get('url'):
                continue
            label = link.get('label') or link.get('host') or link['url']
            action = QAction(label, menu)
            action.triggered.connect(lambda checked=False, l=link: self._start_ddl_download(l))
            menu.addAction(action)
        menu.exec(self._button.mapToGlobal(self._button.rect().bottomLeft()))

    @_guarded
    def _start_ddl_download(self, link: dict) -> None:
        url = link.get('url')
        if not url or self._details is None:
            return
        from urllib.parse import urlparse, unquote
        name = unquote(os.path.basename(urlparse(url).path)) or f'{self._details.title}.zip'
        safe_name = re.sub('[\\\\/:*?"<>|]', '_', name)
        self._button.setText(tr('repacks.queuing'))
        self._button.setEnabled(False)
        self._item_id = self._manager.add_http(url, title=self._details.title, file_name=safe_name, source='Kryoto', category='repacks')

    _FILELIST_TIMEOUT_MS = 15000

    def _nothing_found_text(self) -> str:
        return tr('repacks.dead_link_gog') if self._source_key == 'gog' else tr('repacks.nothing_found')

    def _start_selective_download(self, source: str) -> None:
        if self._filelist_thread is not None and self._filelist_thread.isRunning():
            return


        if self._setup_thread is not None and self._setup_thread.source == source:
            self._cancel_setup_thread(only_source=source)
            self._setup_cache.pop(source, None)
        self._button.setText(tr('repacks.loading_file_list'))
        self._button.setEnabled(False)
        thread = _FileListFetchThread(self._manager, source, None, category='tbhb' if self._source_key == 'tbhb' else 'repacks')
        thread.finished_ok.connect(lambda files: self._on_file_list_ready(source, files))
        thread.finished_err.connect(lambda err: self._on_file_list_failed(source, err))
        thread.finished.connect(self._on_filelist_thread_finished)
        self._filelist_thread = thread
        watchdog = QTimer(self)
        watchdog.setSingleShot(True)
        watchdog.timeout.connect(lambda: self._on_filelist_timeout(source))
        self._filelist_watchdog = watchdog
        watchdog.start(self._FILELIST_TIMEOUT_MS)
        thread.start()

    def _on_filelist_timeout(self, source: str) -> None:
        thread = self._filelist_thread
        if thread is None or not thread.isRunning():
            return
        try:
            thread.finished_ok.disconnect()
        except (TypeError, RuntimeError):
            pass
        try:
            thread.finished_err.disconnect()
        except (TypeError, RuntimeError):
            pass
        try:
            thread.finished.disconnect()
        except (TypeError, RuntimeError):
            pass
        self._orphaned_filelist_threads.append(thread)
        thread.finished.connect(lambda t=thread: self._cleanup_orphaned_thread(t))
        self._filelist_thread = None
        logger.warning('File list fetch timed out for source; giving up')
        self._button.setText(self._nothing_found_text())
        self._button.setEnabled(False)

    def _cleanup_orphaned_thread(self, thread: '_FileListFetchThread') -> None:
        if thread in self._orphaned_filelist_threads:
            self._orphaned_filelist_threads.remove(thread)
        thread.deleteLater()

    def _on_filelist_thread_finished(self) -> None:
        watchdog = getattr(self, '_filelist_watchdog', None)
        if watchdog is not None:
            watchdog.stop()
            self._filelist_watchdog = None
        thread = self._filelist_thread
        self._filelist_thread = None
        if thread is not None:
            thread.deleteLater()

    def _on_file_list_failed(self, source: str, err: str) -> None:
        logger.error('Failed to fetch file list for selective download: %s', err)
        self._button.setText(self._nothing_found_text())
        self._button.setEnabled(False)

    @_guarded
    def _on_file_list_ready(self, source: str, torrent_files: list[tuple[str, int]]) -> None:
        self._button.setText(tr('repacks.download'))
        self._button.setEnabled(True)
        if self._source_key == 'fitgirl' and self._details is not None and self._details.extra.get('selective_download') and _auto_install_enabled():
            self._start_setup_exe_prefetch(source, silent=True)
        if not torrent_files or len(torrent_files) == 1:
            self._queue_download(source, file_ids=None)
            return
        paths = [p for p, _size in torrent_files]
        sizes = [s for _p, s in torrent_files]
        entries = build_selective_entries_from_torrent(paths, sizes)
        dialog = SelectiveDownloadDialog(entries, parent=self.window(), context='repack')
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        selected_labels = dialog.selected_labels()
        file_ids = resolve_selective_file_indices(entries, selected_labels, paths)
        if not file_ids:
            logger.warning('Selective download selection produced no file indices; downloading full torrent')
            file_ids = None
        self._queue_download(source, file_ids=file_ids)

    def _queue_download(self, source: str, file_ids: list[int] | None) -> None:
        if self._source_key == 'fitgirl' and self._details is not None:
            self._pending_queue_args = (source, file_ids)
            if not _auto_install_enabled() or not self._details.extra.get('selective_download'):
                self._show_install_options_dialog(setup_path='')
                return
            cached = self._setup_cache.get(source, '')
            if cached != '' or (self._setup_thread is not None and self._setup_thread.source == source):
                if cached != '':
                    self._show_install_options_dialog(setup_path=cached or '')
                else:
                    self._button.setText(tr('repacks.inspecting_installer'))
                    self._button.setEnabled(False)
                    try:
                        self._setup_thread.progress.disconnect(self._on_setup_exe_progress)
                    except (TypeError, RuntimeError):
                        pass
                    self._setup_thread.progress.connect(self._on_setup_exe_progress)
                return
            self._start_setup_exe_prefetch(source, silent=False)
            if self._setup_thread is None and self._setup_cache.get(source, '') == '' and self._pending_queue_args is not None:
                self._show_install_options_dialog(setup_path='')
            return
        self._continue_queue_download(source, file_ids, install_spec=None)

    @_guarded
    def _start_setup_exe_prefetch(self, source: str, silent: bool) -> None:
        if source in self._queued_download_sources:
            return
        if self._setup_cache.get(source, '') != '':
            return
        if self._setup_thread is not None:
            if self._setup_thread.source == source:
                return
        self._setup_cache[source] = ''
        if not silent:
            self._button.setText(tr('repacks.inspecting_installer'))
            self._button.setEnabled(False)
        category = 'tbhb' if self._source_key == 'tbhb' else 'repacks'
        thread = _SetupExeFetchThread(self._manager, source, category=category)
        thread.finished_ok.connect(lambda path, s=source: self._on_setup_exe_ready(s, path))
        thread.finished_err.connect(lambda err, s=source: self._on_setup_exe_failed(s, err))
        thread.finished.connect(self._on_setup_thread_finished)
        if not silent:
            thread.progress.connect(self._on_setup_exe_progress)
        self._setup_thread = thread
        thread.start()

    def _on_setup_exe_progress(self, done: int, total: int) -> None:
        base = tr('repacks.inspecting_installer')
        if total > 0:
            pct = int(done / total * 100)
            self._button.setText(f'{base} ({pct}%)')
        else:
            self._button.setText(base)

    def _on_setup_thread_finished(self) -> None:
        self._setup_thread = None

    @_guarded
    def _on_setup_exe_failed(self, source: str, err: str) -> None:
        logger.warning('setup.exe pre-fetch failed for %s, continuing without Inno inspection: %s', source, err)
        retry_count = self._setup_retry_counts.get(source, 0)
        transient = any((tok in (err or '').lower() for tok in ('invalid torrent handle', 'metadata', 'timed out')))
        if transient and retry_count < 4:
            self._setup_retry_counts[source] = retry_count + 1
            self._setup_cache.pop(source, None)
            delay_ms = min(3000 * (retry_count + 1), 12000)
            logger.info('Retrying setup.exe pre-fetch for %s in %dms (attempt %d)', source, delay_ms, retry_count + 2)
            QTimer.singleShot(delay_ms, lambda s=source: self._start_setup_exe_prefetch(s, silent=True))
            return
        self._setup_cache[source] = None
        if self._pending_queue_args is not None and self._pending_queue_args[0] == source:
            self._show_install_options_dialog(setup_path='')

    @_guarded
    def _on_setup_exe_ready(self, source: str, setup_path: str) -> None:
        if setup_path:
            logger.info('setup.exe pre-fetch finished for %s -> %s', source, setup_path)
        else:
            logger.warning('setup.exe pre-fetch finished for %s but no path returned (no setup.exe filename found in torrent, or fetch timed out)', source)
        self._setup_cache[source] = setup_path or None
        if self._pending_queue_args is not None and self._pending_queue_args[0] == source:
            self._show_install_options_dialog(setup_path=setup_path)

    def _show_install_options_dialog(self, setup_path: str) -> None:
        args = self._pending_queue_args
        self._pending_queue_args = None
        if args is None or self._details is None:
            if self._item_id is None:
                self._button.setText(tr('repacks.download'))
                self._button.setEnabled(True)
            return
        source, file_ids = args
        if not _auto_install_enabled():
            self._continue_queue_download(source, file_ids, install_spec=None)
            return
        dlg = GameInstallOptionsDialog(self._details, setup_path=setup_path, parent=self.window())
        if dlg.exec() != QDialog.DialogCode.Accepted:
            self._button.setText(tr('repacks.download'))
            self._button.setEnabled(True)
            return
        install_spec = dlg.result_spec()
        self._continue_queue_download(source, file_ids, install_spec=install_spec)

    def _continue_queue_download(self, source: str, file_ids: list[int] | None, install_spec: dict | None) -> None:
        self._queued_download_sources.add(source)
        self._last_queued_source = source
        self._cancel_setup_thread(only_source=source)
        self._button.setText(tr('repacks.queuing'))
        self._button.setEnabled(False)
        if self._source_key == 'tbhb':
            category = 'tbhb'
            console = getattr(self._details, 'category_name', '') or ''
            source_label = getattr(self._details, 'username', '') or 'TBHB'
            logger.info('TBHB download queued: title=%r raw_category=%r resolved_console=%r', self._details.title, getattr(self._details, 'category', None), console)
        else:
            category = 'repacks'
            console = ''
            source_label = 'FitGirl'
        self._item_id = self._manager.add(torrent_file=source, file_id=file_ids[0] if file_ids else 1, file_ids=file_ids, game_name=self._details.title, console=console, source=source_label, category=category)
        if install_spec is not None:
            get_install_coordinator().register_pending(self._item_id, install_spec)

    def _on_item_updated(self, item_id: str) -> None:
        if item_id != self._item_id:
            return
        item = self._manager.get(item_id)
        if item is None:
            return
        if item.state == DLState.queued:
            self._button.setText(tr('repacks.queuing'))
        else:
            self._button.setText(item.state.value.upper())

    def _on_item_removed(self, item_id: str) -> None:
        if item_id != self._item_id:
            return
        self._item_id = None
        self._queued_download_sources.discard(getattr(self, '_last_queued_source', None))
        self._button.setText(tr('repacks.download'))
        self._button.setEnabled(True)

    def shutdown(self) -> None:
        watchdog = getattr(self, '_filelist_watchdog', None)
        if watchdog is not None:
            watchdog.stop()
            self._filelist_watchdog = None
        self._cancel_setup_thread()
        for orphan in list(self._orphaned_filelist_threads):
            try:
                if orphan.isRunning():
                    orphan.wait(500)
            except RuntimeError:
                pass
        thread = self._filelist_thread
        self._filelist_thread = None
        if thread is None:
            return
        for sig in (thread.finished_ok, thread.finished_err, thread.finished):
            try:
                sig.disconnect()
            except (TypeError, RuntimeError):
                pass
        try:
            if thread.isRunning():
                thread.wait(500)
            if thread.isRunning():
                self._orphaned_filelist_threads.append(thread)
                thread.finished.connect(lambda t=thread: self._cleanup_orphaned_thread(t))
            else:
                thread.deleteLater()
        except RuntimeError:
            pass

class ElidedLabel(QLabel):

    def __init__(self, text: str = "", parent=None):
        super().__init__("", parent)
        self._full = ""
        self.setWordWrap(False)
        self.setText(text)

    def setText(self, text: str) -> None:
        self._full = str(text or "")
        self.setToolTip(self._full)
        self._refresh_elide()

    def fullText(self) -> str:
        return self._full

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._refresh_elide()

    def _refresh_elide(self) -> None:
        if not self._full:
            super().setText("")
            return
        width = max(0, self.width() - 2)
        if width <= 0:
            super().setText(self._full)
            return
        text = QFontMetrics(self.font()).elidedText(self._full, Qt.TextElideMode.ElideRight, width)
        super().setText(text)


class SteamInfoWidget(QWidget):
    refresh_requested = Signal()
    change_match_requested = Signal()
    clear_match_requested = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setVisible(False)
        self._row = QHBoxLayout(self)
        self._row.setContentsMargins(0, 0, 0, 0)
        self._row.setSpacing(6)
        self._match_btn = PushButton(tr("steam.match_manually", "Match manually"), self)
        self._match_btn.setFixedHeight(26)
        self._match_btn.clicked.connect(self.change_match_requested.emit)
        self._more_btn = TransparentToolButton(FluentIcon.MORE, self)
        self._more_btn.setFixedSize(28, 28)
        self._more_btn.clicked.connect(self._show_menu)

    def _show_menu(self) -> None:
        from PySide6.QtWidgets import QMenu

        menu = QMenu(self)
        menu.addAction(tr("steam.refresh", "Refresh Steam data"), self.refresh_requested.emit)
        menu.addAction(tr("steam.change_match", "Change match"), self.change_match_requested.emit)
        menu.addSeparator()
        menu.addAction(tr("steam.clear_match", "Remove match"), self.clear_match_requested.emit)
        menu.exec(self._more_btn.mapToGlobal(self._more_btn.rect().bottomLeft()))

    def _reset(self) -> None:
        while self._row.count():
            item = self._row.takeAt(0)
            widget = item.widget()
            if widget is not None and widget not in (self._match_btn, self._more_btn):
                widget.deleteLater()
        self._match_btn.setVisible(False)
        self._more_btn.setVisible(False)

    def set_loading(self) -> None:
        self._reset()
        self.setVisible(False)

    def show_no_match(self, *, enabled: bool = True) -> None:
        self._reset()
        self._row.addWidget(self._match_btn)
        self._row.addStretch(1)
        self._match_btn.setVisible(enabled)
        self.setVisible(enabled)

    def set_enrichment(self, enrichment) -> None:
        self._reset()
        self._row.addWidget(self._more_btn)
        self._row.addStretch(1)
        self._more_btn.setVisible(True)
        self.setVisible(True)

    def clear(self) -> None:
        self._reset()
        self.setVisible(False)


class SteamMatchDialog(MessageBoxBase):

    def __init__(self, title: str, parent=None):
        super().__init__(parent)
        self._selected_appid: int | None = None
        self._search_seq = 0
        self.titleLabel = SubtitleLabel(tr("steam.choose_match", "Choose Steam match"), self)
        self.viewLayout.addWidget(self.titleLabel)
        self._hint = CaptionLabel(tr("steam.choose_match_hint", "Search by name or enter a Steam AppID."), self)
        self._hint.setWordWrap(True)
        self.viewLayout.addWidget(self._hint)
        self._query = LineEdit(self)
        self._query.setText(title)
        self._query.setClearButtonEnabled(True)
        self._query.returnPressed.connect(self._search)
        self.viewLayout.addWidget(self._query)
        search_btn = PushButton(tr("steam.search", "Search"), self)
        search_btn.clicked.connect(self._search)
        self.viewLayout.addWidget(search_btn)
        self._results = QListWidget(self)
        self._results.setMinimumHeight(220)
        self._results.itemDoubleClicked.connect(lambda _item: self._accept_selected())
        self.viewLayout.addWidget(self._results)
        self._appid_edit = LineEdit(self)
        self._appid_edit.setPlaceholderText(tr("steam.manual_appid", "Steam AppID (optional)"))
        self.viewLayout.addWidget(self._appid_edit)
        self._status = CaptionLabel("", self)
        self._status.setWordWrap(True)
        self.viewLayout.addWidget(self._status)
        self.yesButton.setText(tr("steam.use_match", "Use match"))
        self.cancelButton.setText(tr("common.cancel", "Cancel"))
        self._apply_theme()
        register_theme_refresh(self, self._apply_theme)
        self._search()

    def _apply_theme(self, *_args) -> None:
        color = palette()["muted"]
        self._hint.setStyleSheet(f"color: {color};")
        self._status.setStyleSheet(f"color: {color};")

    def _search(self) -> None:
        query = self._query.text().strip()
        if len(query) < 2:
            self._status.setText(tr("steam.search_too_short", "Enter at least two characters."))
            return
        self._search_seq += 1
        seq = self._search_seq
        self._status.setText(tr("steam.searching", "Searching Steam…"))
        from src.core.repacks import enrichment as service
        from src.core.worker import submit

        submit(
            service.search_candidates,
            args=(query,),
            on_done=lambda items, s=seq: self._on_results(items, s),
            on_error=lambda error, s=seq: self._on_error(error, s),
            owner=self,
            key=f"steam:dialog:{query.casefold()}",
        )

    def _on_results(self, items, seq: int) -> None:
        if not _qt_is_valid(self) or seq != self._search_seq:
            return
        self._results.clear()
        for item in items or ():
            name = str(item.get("name") or "")
            appid = item.get("appid")
            if not name or not appid:
                continue
            row = QListWidgetItem(f"{name}  ·  {appid}")
            row.setData(Qt.ItemDataRole.UserRole, int(appid))
            self._results.addItem(row)
        if self._results.count():
            self._results.setCurrentRow(0)
            self._status.setText(tr("steam.results_count", "{count} result(s)", count=self._results.count()))
        else:
            self._status.setText(tr("steam.no_results", "No Steam results. You can enter an AppID above."))

    def _on_error(self, error, seq: int) -> None:
        if not _qt_is_valid(self) or seq != self._search_seq:
            return
        logger.debug("Steam match search failed", exc_info=error if isinstance(error, BaseException) else None)
        self._status.setText(tr("steam.search_failed", "Steam search failed. You can enter an AppID above."))

    def _accept_selected(self) -> None:
        if self.validate():
            self.accept()

    def validate(self) -> bool:
        from src.core.repacks import enrichment as service

        manual = str(self._appid_edit.text() or "").strip()
        if manual:
            appid = service.resolve_appid(manual)
            if appid is None:
                self._status.setText(tr("steam.invalid_appid", "Enter a numeric Steam AppID."))
                return False
            self._selected_appid = appid
            return True
        item = self._results.currentItem()
        if item is None:
            self._status.setText(tr("steam.select_result", "Select a result or enter an AppID."))
            return False
        try:
            appid = int(item.data(Qt.ItemDataRole.UserRole))
        except (TypeError, ValueError):
            return False
        if appid <= 0:
            return False
        self._selected_appid = appid
        return True

    def selected_appid(self) -> int | None:
        return self._selected_appid


class _FrameAnimation(QObject):
    """Small looped frame player for formats QMovie cannot open (animated AVIF)."""

    frame = Signal(QImage)

    def __init__(self, frames: list[tuple[QImage, int]], parent=None):
        super().__init__(parent)
        self._frames = list(frames)
        self._index = 0
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self._advance)

    def start(self) -> None:
        if not self._frames:
            return
        self._index = 0
        self._advance()

    def stop(self) -> None:
        self._timer.stop()

    def _advance(self) -> None:
        if not self._frames:
            return
        image, delay = self._frames[self._index]
        self._index = (self._index + 1) % len(self._frames)
        self.frame.emit(image)
        self._timer.start(max(20, int(delay or 40)))


def _animation_payload(src: str):
    path = image_file(src)
    if not path:
        return path, []
    if re.search(r'\.avif(?:$|[?#])', src, re.IGNORECASE):
        from src.core.artwork import avif_animation

        return path, avif_animation(path)
    return path, []


class _SteamDesc(QTextBrowser):
    _ANIMATED_RE = re.compile(r'\.(?:gif|webp|avif)(?:$|[?#])', re.IGNORECASE)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._html = ''
        self._imgs = {}
        self._movies = {}
        self._frame_anims: dict[str, _FrameAnimation] = {}
        self._animated_srcs: set[str] = set()
        self.setOpenExternalLinks(True)
        self.setFrameShape(QTextBrowser.Shape.NoFrame)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._refresh = QTimer(self)
        self._refresh.setSingleShot(True)
        self._refresh.setInterval(60)
        self._refresh.timeout.connect(lambda: self.setHtml(self._html))
        self.document().documentLayout().documentSizeChanged.connect(
            lambda size: self.setFixedHeight(int(size.height()) + 6))

    def loadResource(self, kind, url):
        return self._imgs.get(url.toString()) or self._imgs.get(bytes(url.toEncoded()).decode())

    def set_content(self, html: str) -> None:
        import html as _html_lib

        self._stop_animations()
        self._html = html
        self._imgs = {}
        self.setHtml(html)
        for raw in dict.fromkeys(re.findall(r'<img[^>]+src="([^"]+)"', html)):
            src = _html_lib.unescape(raw)
            if self._ANIMATED_RE.search(src):
                self._animated_srcs.add(src)
                self._load_animation(src, html)
            else:
                images.get(src, (760, 1600), lambda pix, s=src, h=html: self._on_image(s, h, pix), mode=FIT, owner=self)

    def _load_animation(self, src: str, html: str) -> None:
        from src.core.worker import submit

        submit(lambda: _animation_payload(src),
               on_done=lambda payload, s=src, h=html: self._on_animation_file(s, h, payload),
               on_error=lambda _error, s=src, h=html: self._on_image_failed(s, h),
               owner=self, key=('steam-anim', src), priority=Priority.VISIBLE)

    def _on_animation_file(self, src: str, html: str, payload) -> None:
        if html != self._html or src not in self._animated_srcs:
            return
        path, frames = payload if isinstance(payload, tuple) else (payload, [])
        if frames and len(frames) > 1:
            anim = _FrameAnimation(frames, self)
            anim.frame.connect(lambda image, s=src: self._set_frame(s, QPixmap.fromImage(image)))
            self._frame_anims[src] = anim
            if self.isVisible():
                anim.start()
            else:
                anim.frame.emit(frames[0][0])
            return
        if frames:
            image = frames[0][0]
            if image.width() > 760 or image.height() > 1600:
                image = image.scaled(760, 1600, Qt.AspectRatioMode.KeepAspectRatio,
                                     Qt.TransformationMode.SmoothTransformation)
            pix = QPixmap.fromImage(image)
            if not pix.isNull():
                self._set_frame(src, pix)
            return
        movie = QMovie(self)
        movie.setFileName(str(path or ''))
        movie.setCacheMode(QMovie.CacheMode.CacheNone)
        if not movie.isValid() or movie.frameCount() <= 1:
            movie.deleteLater()
            self._on_image_failed(src, html)
            return
        # QMovie.frameChanged(int) passes the frame number; swallow it so the
        # captured source stays the first positional argument.
        movie.frameChanged.connect(lambda _frame, s=src, m=movie: self._on_movie_frame(s, m))
        self._movies[src] = movie
        movie.jumpToFrame(0)
        pix = movie.currentPixmap()
        if not pix.isNull() and (pix.width() > 760 or pix.height() > 1600):
            movie.setScaledSize(pix.size().scaled(760, 1600, Qt.AspectRatioMode.KeepAspectRatio))
            movie.jumpToFrame(0)
            pix = movie.currentPixmap()
        if not pix.isNull():
            self._set_frame(src, pix)
        if self.isVisible():
            movie.start()

    def _on_movie_frame(self, src: str, movie) -> None:
        if self._movies.get(src) is not movie:
            return
        pix = movie.currentPixmap()
        if not pix.isNull():
            self._set_frame(src, pix)

    def _set_frame(self, src: str, pix) -> None:
        self._imgs[src] = pix
        try:
            self.document().addResource(QTextDocument.ResourceType.ImageResource, QUrl(src), pix)
        except RuntimeError:
            return
        self.viewport().update()

    def _on_image_failed(self, src: str, html: str) -> None:
        if html == self._html:
            self._animated_srcs.discard(src)
            images.get(src, (760, 1600), lambda pix, s=src, h=html: self._on_image(s, h, pix),
                       mode=FIT, owner=self)

    def _on_image(self, src: str, html: str, pix) -> None:
        if html != self._html or pix.isNull():
            return
        self._imgs[src] = pix
        self._refresh.start()

    def _stop_animations(self) -> None:
        for movie in self._movies.values():
            with suppress(RuntimeError, TypeError):
                movie.stop()
                movie.frameChanged.disconnect()
            movie.deleteLater()
        self._movies.clear()
        for anim in self._frame_anims.values():
            anim.stop()
            anim.deleteLater()
        self._frame_anims.clear()
        self._animated_srcs.clear()

    def hideEvent(self, event) -> None:
        for movie in self._movies.values():
            with suppress(RuntimeError):
                movie.stop()
        for anim in self._frame_anims.values():
            anim.stop()
        super().hideEvent(event)

    def showEvent(self, event) -> None:
        super().showEvent(event)
        for movie in self._movies.values():
            with suppress(RuntimeError):
                movie.start()
        for anim in self._frame_anims.values():
            anim.start()


class RepackDetailsView(DetailsViewMixin, QWidget):
    back_requested = Signal()
    banner_changed = Signal()
    COVER_WIDTH = 280
    COVER_HEIGHT = 392

    @property
    def _META_LABELS(self):
        return (('repack_size', tr('repacks.repack_size')), ('original_size', tr('repacks.original_size')), ('company', tr('repacks.company')), ('languages', tr('repacks.languages')), ('rating', tr('repacks.rating')), ('released', tr('steam.released_label', 'Released')), ('metacritic', tr('steam.metacritic_label', 'Metacritic')))

    def __init__(self, manager: DownloadManager, source_key: str | None=None, parent=None):
        super().__init__(parent)
        self._download_action = DownloadActionWidget(manager, source_key, self)
        self._cover_src: str | None = None
        self._cover_token = 0
        self._cover_gen = 0
        self._section_widgets: list[CollapsibleSection] = []
        self._meta_widgets: list[MetaField] = []
        self._tag_widgets: list[PillPushButton] = []
        self._expanded_state: dict[str, bool] = {}
        outer = QVBoxLayout(self)


        outer.setContentsMargins(24, 0, 24, 20)
        outer.setSpacing(10)
        back_btn = PushButton(themed_icon('repacks.detail_back', FluentIcon.RETURN), tr('repacks.back_to_grid'))
        back_btn.setFixedHeight(32)
        self._back_btn = back_btn
        self._current_details: RepackDetails | None = None
        self._bookmark_btn = PushButton(tr('repacks.bookmark_it'))
        self._bookmark_btn.setFixedHeight(32)
        self._bookmark_btn.setIcon(_bookmark_icon())
        self._bookmark_btn.setIconSize(QSize(18, 18))
        self._bookmark_btn.clicked.connect(self._on_bookmark_clicked)
        bookmark_store().changed.connect(self._update_bookmark_button)
        btn_row = QHBoxLayout()
        btn_row.setContentsMargins(0, 0, 0, 0)
        btn_row.setSpacing(8)
        btn_row.addWidget(self._back_btn, 0, Qt.AlignmentFlag.AlignLeft)
        btn_row.addWidget(self._bookmark_btn, 0, Qt.AlignmentFlag.AlignLeft)
        btn_row.addStretch(1)
        back_btn.clicked.connect(self.back_requested.emit)
        self._scroll = SmoothScrollArea(self)
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        self._scroll.setStyleSheet('background: transparent; border: none;')
        outer.addWidget(self._scroll, 1)
        self._scroll.verticalScrollBar().valueChanged.connect(lambda _v: self.banner_changed.emit())
        content = QWidget()
        self._scroll.setWidget(content)
        self._content_layout = QVBoxLayout(content)
        self._content_layout.setContentsMargins(4, 12, 24, 40)
        self._content_layout.setSpacing(24)
        self._content_layout.addLayout(btn_row)
        self.SECTIONS_WIDTH = 300
        top_row = QHBoxLayout()
        top_row.setSpacing(40)
        top_row.setAlignment(Qt.AlignmentFlag.AlignTop)
        cover_container = QWidget()
        cover_container.setFixedWidth(self.COVER_WIDTH)
        cover_col = QVBoxLayout(cover_container)
        cover_col.setContentsMargins(0, 0, 0, 0)
        cover_col.setSpacing(12)
        self._cover_lbl = ImageLabel(cover_container)
        self._cover_lbl.setBorderRadius(10, 10, 10, 10)
        self._cover_lbl.setFixedSize(self.COVER_WIDTH, self.COVER_HEIGHT)
        cover_col.addWidget(self._cover_lbl, 0, Qt.AlignmentFlag.AlignTop)
        self._cover_fallback = QWidget(cover_container)
        self._cover_fallback.setFixedSize(self.COVER_WIDTH, self.COVER_HEIGHT)
        self._cover_fallback.setVisible(False)
        self._cover_fallback.setStyleSheet(f'background-color: {_surface_tint_color(14)}; border-radius: 10px;')
        cover_fallback_layout = QVBoxLayout(self._cover_fallback)
        cover_fallback_layout.setContentsMargins(20, 0, 20, 0)
        cover_fallback_layout.setSpacing(10)
        cover_fallback_layout.addStretch(1)
        self._cover_fallback_icon_lbl = QLabel(self._cover_fallback)
        self._cover_fallback_icon_lbl.setPixmap(themed_icon('repacks.detail_cover_fallback', FluentIcon.GAME, size=48, color=_body_text_color()).pixmap(48, 48))
        self._cover_fallback_icon_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._cover_fallback_icon_lbl.setStyleSheet('background: transparent;')
        cover_fallback_layout.addWidget(self._cover_fallback_icon_lbl)
        self._cover_fallback_title_lbl = QLabel('', self._cover_fallback)
        self._cover_fallback_title_lbl.setWordWrap(True)
        self._cover_fallback_title_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._cover_fallback_title_lbl.setStyleSheet(f'background: transparent; font-size: 13px; color: {palette()['primary_text']};')
        cover_fallback_layout.addWidget(self._cover_fallback_title_lbl)
        cover_fallback_layout.addStretch(1)
        cover_col.addWidget(self._cover_fallback, 0, Qt.AlignmentFlag.AlignTop)
        self._cover_action_area = QWidget(cover_container)
        self._cover_action_area.setFixedWidth(self.COVER_WIDTH)
        self._cover_action_layout = QVBoxLayout(self._cover_action_area)
        self._cover_action_layout.setContentsMargins(0, 0, 0, 0)
        self._cover_action_layout.setSpacing(8)
        cover_col.addWidget(self._cover_action_area, 0, Qt.AlignmentFlag.AlignTop)
        self._gallery = ScreenshotGallery(cover_container)
        cover_col.addWidget(self._gallery, 0, Qt.AlignmentFlag.AlignTop)
        self._gallery_visibility_timer = QTimer(self)
        self._gallery_visibility_timer.setSingleShot(True)
        self._gallery_visibility_timer.setInterval(150)
        self._gallery_visibility_timer.timeout.connect(self._gallery.update_visible_videos)
        self._scroll.verticalScrollBar().valueChanged.connect(lambda _v: self._gallery_visibility_timer.start())
        cover_col.addStretch(1)
        top_row.addWidget(cover_container, 0, Qt.AlignmentFlag.AlignTop)
        info_col_container = QWidget()
        info_col = QVBoxLayout(info_col_container)
        info_col.setContentsMargins(0, 0, 0, 0)
        info_col.setSpacing(20)
        self._title_lbl = TitleLabel('')
        self._title_lbl.setWordWrap(True)
        self._title_lbl.setStyleSheet(f'font-weight: 700; color: {palette()['primary_text']};')
        info_col.addWidget(self._title_lbl)
        self._logo_lbl = QLabel(info_col_container)
        self._logo_lbl.setStyleSheet('background: transparent;')
        self._logo_lbl.setVisible(False)
        info_col.addWidget(self._logo_lbl, 0, Qt.AlignmentFlag.AlignLeft)
        self._logo_src = None
        self._steam_desc_html = ''
        self._steam_host = QWidget(info_col_container)
        self._steam_layout = QVBoxLayout(self._steam_host)
        self._steam_layout.setContentsMargins(0, 0, 0, 0)
        self._steam_layout.setSpacing(6)
        self._steam_host.setVisible(False)
        info_col.addWidget(self._steam_host)
        self._meta_lbl = None
        self._steam_widget = None
        self._parsed_title = None
        self._current_entry = None
        self._steam_enrichment = None
        self._steam_gen = 0
        self._meta_extra = {}
        self._banner_src = None
        self._banner_pix = None
        self._banner_cache = (0, None)
        self._meta_grid = QGridLayout()
        self._meta_grid.setHorizontalSpacing(40)
        self._meta_grid.setVerticalSpacing(10)
        self._meta_grid.setColumnStretch(0, 0)
        self._meta_grid.setColumnStretch(1, 0)
        info_col.addLayout(self._meta_grid)
        tags_header = CaptionLabel(tr('repacks.genres_tags'))
        tags_header.setStyleSheet(f'font-weight: 400; color: {_muted_text_color()};')
        self._tags_header = tags_header
        info_col.addWidget(tags_header)
        self._tags_container = QWidget()
        self._tags_layout = QFlowLayout(self._tags_container, needAni=False, isTight=True)
        self._tags_layout.setContentsMargins(0, 0, 0, 0)
        self._tags_layout.setHorizontalSpacing(8)
        self._tags_layout.setVerticalSpacing(8)
        info_col.addWidget(self._tags_container)
        desc_header = StrongBodyLabel(tr('repacks.description'))
        desc_header.setStyleSheet(f'font-size: 16px; font-weight: 600; color: {palette()['primary_text']};')
        self._desc_header = desc_header
        info_col.addWidget(desc_header)
        self._desc_lbl = CachedRichLabel()
        self._desc_lbl.setWordWrap(True)
        self._desc_lbl.setTextFormat(Qt.TextFormat.RichText)
        self._desc_lbl.setOpenExternalLinks(True)
        self._desc_default_qss_template = 'font-size: 18px; font-weight: 500; line-height: {pct}%; color: {color};'
        self._desc_announcement_qss_template = 'font-size: 16px; font-weight: 500; line-height: {pct}%; color: {color};'
        self._desc_default_qss = self._desc_default_qss_template.format(pct=_DESC_LINE_HEIGHT_PCT, color=_body_text_color())
        self._desc_lbl.setStyleSheet(self._desc_default_qss)
        info_col.addWidget(self._desc_lbl)
        self._steam_desc = _SteamDesc(info_col_container)
        self._steam_desc.setVisible(False)
        info_col.addWidget(self._steam_desc)
        self._raw_intro_text = ''
        self._is_announcement = False
        qconfig.themeChanged.connect(self._on_theme_changed)
        info_col.addStretch(1)
        top_row.addWidget(info_col_container, 1, Qt.AlignmentFlag.AlignTop)
        sections_container = QWidget()
        sections_container.setFixedWidth(self.SECTIONS_WIDTH)
        self._sections_col = QVBoxLayout(sections_container)
        self._sections_col.setContentsMargins(0, 0, 0, 0)
        self._sections_col.setSpacing(14)
        sections_header = StrongBodyLabel(tr('repacks.details'))
        sections_header.setStyleSheet(f'font-size: 16px; font-weight: 600; color: {palette()['primary_text']};')
        self._sections_header = sections_header
        self._sections_col.addWidget(sections_header)
        self._site_link_btn = HyperlinkButton('', tr('repacks.view_on_website'), sections_container, themed_icon('repacks.site_link', FluentIcon.LINK))
        self._site_link_btn.setIconSize(QSize(14, 14))
        qconfig.themeColorChanged.connect(self._deferred_apply_site_link_style)
        qconfig.themeChanged.connect(self._deferred_apply_site_link_style)
        self._site_link_btn.setVisible(False)
        self._sections_col.addWidget(self._site_link_btn)
        self._steam_btn = HyperlinkButton('', tr('steam.view_on_steam', 'View on Steam'), sections_container, themed_icon('repacks.site_link', FluentIcon.LINK))
        self._steam_btn.setIconSize(QSize(14, 14))
        self._steam_btn.setVisible(False)
        self._sections_col.addWidget(self._steam_btn)
        self._apply_site_link_style()
        self._sections_insert_index = self._sections_col.count()
        self._sections_header.setVisible(False)
        top_row.addWidget(sections_container, 0, Qt.AlignmentFlag.AlignTop)
        self._content_layout.addLayout(top_row)
        self._content_layout.addStretch(1)
        self.set_cover_action_widget(self._download_action)
        register_locale_refresh(self, self._apply_locale)

    def _on_bookmark_clicked(self) -> None:
        details = self._current_details
        if details is None:
            return
        source = getattr(self._download_action, '_source_key', '') or getattr(details, 'source', '')
        store = bookmark_store()
        if store.has(source, details.url):
            store.remove(source, details.url)
        else:
            store.add_entry(RepackEntry(source=source, title=details.title, url=details.url, poster_url=details.cover_url, poster_path=details.cover_path))

    def _update_bookmark_button(self) -> None:
        details = getattr(self, '_current_details', None)
        if details is None:
            self._bookmark_btn.setVisible(False)
            return
        self._bookmark_btn.setVisible(not bool((details.extra or {}).get('is_announcement')))
        source = getattr(self._download_action, '_source_key', '') or getattr(details, 'source', '')
        marked = bookmark_store().has(source, details.url)
        self._bookmark_btn.setText(tr('repacks.bookmarked') if marked else tr('repacks.bookmark_it'))
        self._bookmark_btn.setIcon(_bookmark_icon(marked))

    def _apply_locale(self, *_args) -> None:
        self._back_btn.setText(tr('repacks.back_to_grid'))
        self._update_bookmark_button()
        self._tags_header.setText(tr('repacks.genres_tags'))
        self._desc_header.setText(tr('repacks.description'))
        self._sections_header.setText(tr('repacks.details'))
        self._site_link_btn.setText(tr('repacks.view_on_website'))
        self._steam_btn.setText(tr('steam.view_on_steam', 'View on Steam'))

    def _apply_site_link_style(self) -> None:
        color = themeColor()
        base_alpha = 55 if isDarkTheme() else 40
        hover_alpha = 85 if isDarkTheme() else 65
        text_color = palette()['primary_text']
        qss = f'HyperlinkButton {{ background-color: rgba({color.red()}, {color.green()}, {color.blue()}, {base_alpha}); border-radius: 6px; padding: 6px 10px 6px 30px; font-weight: 600; color: {text_color}; }} HyperlinkButton:hover {{ background-color: rgba({color.red()}, {color.green()}, {color.blue()}, {hover_alpha}); color: {text_color}; }}'
        for btn in (self._site_link_btn, self._steam_btn):
            btn.setStyleSheet(qss)
            btn.setIcon(themed_icon('repacks.site_link', FluentIcon.LINK, color=text_color))

    def _apply_desc_style(self) -> None:
        title_color = palette()['primary_text']
        if self._is_announcement:
            self._title_lbl.setStyleSheet(f'font-size: 26px; font-weight: 700; color: {title_color};')
            self._desc_lbl.setStyleSheet(self._desc_announcement_qss_template.format(pct=_DESC_LINE_HEIGHT_PCT, color=_body_text_color()))
        else:
            self._title_lbl.setStyleSheet(f'font-weight: 700; color: {title_color};')
            self._desc_default_qss = self._desc_default_qss_template.format(pct=_DESC_LINE_HEIGHT_PCT, color=_body_text_color())
            self._desc_lbl.setStyleSheet(self._desc_default_qss)

    def _on_theme_changed(self, *_args) -> None:
        self._refresh_desc_theme()
        self._refresh_cover_fallback_theme()
        self._tags_header.setStyleSheet(f'font-weight: 400; color: {_muted_text_color()};')
        self._desc_header.setStyleSheet(f'font-size: 16px; font-weight: 600; color: {palette()['primary_text']};')
        self._sections_header.setStyleSheet(f'font-size: 16px; font-weight: 600; color: {palette()['primary_text']};')
        if self._meta_lbl is not None:
            self._meta_lbl.setStyleSheet(
                f'color: {_muted_text_color()}; font-size: 12px; background: transparent;')
        self._apply_site_link_style()
        QTimer.singleShot(0, self._refresh_desc_theme)

    def _refresh_cover_fallback_theme(self) -> None:
        self._cover_fallback.setStyleSheet(f'background-color: {_surface_tint_color(14)}; border-radius: 10px;')
        self._cover_fallback_icon_lbl.setPixmap(themed_icon('repacks.detail_cover_fallback', FluentIcon.GAME, size=48, color=_body_text_color()).pixmap(48, 48))
        self._cover_fallback_title_lbl.setStyleSheet(f'background: transparent; font-size: 13px; color: {palette()['primary_text']};')

    def _refresh_desc_theme(self) -> None:
        self._apply_desc_style()
        if self._raw_intro_text or self._steam_desc_html:
            self._apply_desc_text()

    def set_cover_action_widget(self, widget) -> None:
        while self._cover_action_layout.count():
            item = self._cover_action_layout.takeAt(0)
            if item is not None and item.widget() is not None:
                item.widget().deleteLater()
        if widget is not None:
            self._cover_action_layout.addWidget(widget)


    def _ensure_steam_ui(self) -> None:
        if self._steam_widget is not None:
            return
        self._meta_lbl = ElidedLabel('', self._steam_host)
        self._meta_lbl.setStyleSheet(
            f'color: {_muted_text_color()}; font-size: 12px; background: transparent;')
        self._steam_layout.addWidget(self._meta_lbl)
        self._steam_widget = SteamInfoWidget(self._steam_host)
        self._steam_layout.addWidget(self._steam_widget)
        self._steam_widget.refresh_requested.connect(self._on_steam_refresh)
        self._steam_widget.change_match_requested.connect(self._on_steam_change_match)
        self._steam_widget.clear_match_requested.connect(self._on_steam_clear_match)

    def _update_steam_host(self) -> None:
        meta = bool(self._meta_lbl is not None and self._meta_lbl.fullText())
        steam = self._steam_widget is not None and not self._steam_widget.isHidden()
        self._steam_host.setVisible(meta or steam)

    def _apply_clean_title(self, raw_title: str) -> None:
        self._ensure_steam_ui()
        try:
            from src.core.repacks.base import parse_title
            parsed = parse_title(raw_title or '')
        except Exception:
            logger.debug('Title parsing unavailable', exc_info=True)
            parsed = None
        self._parsed_title = parsed
        self._title_lbl.setText((parsed.clean if parsed else '') or raw_title or '')
        meta = parsed.display_meta if parsed else ''
        self._meta_lbl.setText(meta)
        self._meta_lbl.setVisible(bool(meta))
        self._update_steam_host()

    def _begin_steam(self, entry) -> None:
        self._steam_gen += 1
        gen = self._steam_gen
        self._steam_enrichment = None
        self._ensure_steam_ui()
        self._steam_widget.clear()
        try:
            from src.core.repacks import enrichment as service

            enabled = service.enrichment_enabled()
        except Exception:
            enabled = False
        is_announcement = bool((getattr(entry, 'extra', None) or {}).get('is_announcement'))
        if not enabled or is_announcement or not entry:
            self._update_steam_host()
            return
        try:
            cached = service.peek_enrichment(entry)
        except Exception:
            cached = None
        if cached is not None:
            self._on_steam_ready(cached, gen)
            try:
                if not service.needs_refresh(entry):
                    return

                def _apply(result, g=gen):
                    if result is not None:
                        self._on_steam_ready(result, g)

                service.request_enrichment(
                    entry, owner=self,
                    on_done=_apply,
                    on_error=lambda error, g=gen: None)
            except Exception:
                pass
            return
        self._steam_widget.set_loading()
        self._update_steam_host()
        try:
            service.request_enrichment(
                entry, owner=self,
                on_done=lambda result, g=gen: self._on_steam_ready(result, g),
                on_error=lambda error, g=gen: self._on_steam_error(error, g))
        except Exception:
            logger.debug('Could not queue Steam enrichment', exc_info=True)
            self._steam_widget.show_no_match(enabled=True)
            self._update_steam_host()

    def _on_steam_ready(self, enrichment, gen: int) -> None:
        if not _qt_is_valid(self) or gen != self._steam_gen:
            return
        self._steam_enrichment = enrichment
        if enrichment is None:
            self._steam_widget.show_no_match(enabled=True)
        else:
            self._steam_widget.set_enrichment(enrichment)
            self._apply_steam_artwork(enrichment)
        self._sync_steam_extras()
        self._update_steam_host()
        if enrichment is not None:
            self._request_steam_art(enrichment, gen)

    def _request_steam_art(self, enrichment, gen: int) -> None:
        try:
            appid = int(enrichment.appid)
        except (TypeError, ValueError):
            return
        if appid <= 0:
            return
        try:
            from src.core.repacks import enrichment as service

            def _apply(art, g=gen, a=appid):
                if not _qt_is_valid(self) or g != self._steam_gen or not isinstance(art, dict):
                    return
                current = self._steam_enrichment
                if current is None or int(getattr(current, 'appid', 0) or 0) != a:
                    return
                merged = dict(getattr(current, 'artwork', None) or {})
                fresh = {k: v for k, v in art.items() if v and merged.get(k) != v}
                if not fresh:
                    return
                merged.update(fresh)
                current.artwork = merged
                entry = self._current_entry
                if entry is not None and not (getattr(entry, 'poster_path', None)
                                              or getattr(entry, 'poster_url', None)):
                    entry.poster_fallback = merged.get('poster') or None
                if fresh.keys() & {'poster', 'hero', 'banner', 'capsule'}:
                    self._apply_steam_artwork(current)
                if fresh.keys() & {'hero', 'banner', 'background', 'logo'}:
                    self._sync_steam_extras()

            service.request_artwork(appid, owner=self, on_done=_apply)
        except Exception:
            logger.debug('Could not queue Steam artwork', exc_info=True)

    def _sync_steam_extras(self) -> None:
        from src.core.config import settings as _s

        enrichment = self._steam_enrichment
        live = enrichment is not None and not self._is_announcement
        self._steam_btn.setVisible(live)
        art = (getattr(enrichment, 'artwork', None) or {}) if live else {}
        banner = art.get('hero') or art.get('banner') or art.get('background') or ''
        self._set_banner(banner if getattr(_s, 'steam_banner', False) else '')
        logo = ''
        if live:
            self._steam_btn.setUrl(f'https://store.steampowered.com/app/{int(enrichment.appid)}/')
            logo = art.get('logo') or f'https://shared.akamai.steamstatic.com/store_item_assets/steam/apps/{int(enrichment.appid)}/logo.png'
        self._set_logo(logo if getattr(_s, 'steam_logo', False) else '')
        self._steam_desc_html = ''
        if live and getattr(_s, 'steam_description', False):
            html = enrichment.about_html
            has_content = len(re.sub(r'<[^>]+>', '', html).strip()) > 20 or '<img' in html
            self._steam_desc_html = html if has_content else enrichment.summary_text
        if self._current_details is not None:
            self._apply_desc_text()
            self._fill_meta()

    def _apply_desc_text(self) -> None:
        steam = bool(self._steam_desc_html)
        self._steam_desc.setVisible(steam)
        self._desc_lbl.setVisible(not steam)
        if steam:
            self._steam_desc.setStyleSheet(f'background: transparent; border: none; font-size: 16px; color: {_body_text_color()};')
            if self._steam_desc._html != self._steam_desc_html:
                self._steam_desc.set_content(self._steam_desc_html)
        else:
            self._desc_lbl.setText(render_description_html(self._raw_intro_text) or tr('repacks.no_description'))

    def _set_logo(self, src: str) -> None:
        if src == self._logo_src:
            return
        self._logo_src = src
        self._logo_lbl.setVisible(False)
        self._title_lbl.setVisible(True)
        if not src:
            return

        def _apply(pix, _src=src):
            if _src == self._logo_src and not pix.isNull():
                from PySide6.QtGui import QBitmap

                pix = QPixmap(pix)
                pix.setDevicePixelRatio(1.0)
                box = QRegion(QBitmap.fromImage(pix.toImage().createAlphaMask())).boundingRect()
                if not box.isEmpty():
                    pix = pix.copy(box)
                pix = pix.scaled(460, 120, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)
                self._logo_lbl.setFixedSize(pix.size())
                self._logo_lbl.setPixmap(pix)
                self._logo_lbl.setToolTip(self._title_lbl.text())
                self._logo_lbl.setVisible(True)
                self._title_lbl.setVisible(False)

        images.get(src, (460, 170), _apply, mode=FIT, owner=self)

    def _set_banner(self, src: str) -> None:
        if src == self._banner_src:
            return
        self._banner_src = src
        self._banner_pix = None
        self._banner_cache = (0, None)
        self.banner_changed.emit()
        if not src:
            return

        def _apply(pix, _src=src):
            if _src == self._banner_src and not pix.isNull():
                self._banner_pix = pix
                self.banner_changed.emit()

        images.get(src, (1920, 1080), _apply, mode=FIT, owner=self)

    def banner_for(self, width: int):
        pix = self._banner_pix
        if pix is None or width <= 0:
            return None
        if self._banner_cache[0] != width:
            from PySide6.QtGui import QImage, QLinearGradient, QPainter

            pix = QPixmap(pix)
            pix.setDevicePixelRatio(1.0)
            scaled = pix.scaledToWidth(width, Qt.TransformationMode.SmoothTransformation)
            img = scaled.copy(0, 0, width, min(scaled.height(), 900)).toImage()
            img = img.convertToFormat(QImage.Format.Format_ARGB32_Premultiplied)
            fade = QLinearGradient(0, 0, 0, img.height())
            for pos, alpha in ((0.0, 215), (0.16, 130), (0.38, 45), (0.6, 0)):
                fade.setColorAt(pos, QColor(0, 0, 0, alpha))
            painter = QPainter(img)
            painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_DestinationIn)
            painter.fillRect(img.rect(), fade)
            painter.end()
            self._banner_cache = (width, QPixmap.fromImage(img))
        return self._banner_cache[1]

    def set_top_inset(self, px: int) -> None:
        self._content_layout.setContentsMargins(4, 12 + px, 24, 40)

    def banner_offset(self) -> int:
        return self._scroll.verticalScrollBar().value()

    def showEvent(self, event) -> None:
        super().showEvent(event)
        self.banner_changed.emit()

    def hideEvent(self, event) -> None:
        super().hideEvent(event)
        self.banner_changed.emit()

    def _on_steam_error(self, _error, gen: int) -> None:
        if not _qt_is_valid(self) or gen != self._steam_gen:
            return
        self._steam_widget.show_no_match(enabled=True)
        self._update_steam_host()

    def _apply_steam_artwork(self, enrichment) -> None:
        entry = self._current_entry
        details = self._current_details
        fitgirl_art = bool(
            (entry is not None and (getattr(entry, 'poster_path', None) or getattr(entry, 'poster_url', None)))
            or (details is not None and (getattr(details, 'cover_path', None) or getattr(details, 'cover_url', None)))
        )
        if not fitgirl_art:
            art = getattr(enrichment, 'artwork', None) or {}
            src = art.get('poster') or art.get('hero') or art.get('banner') or art.get('capsule')
            if src:
                self._show_cover(str(src), self._title_lbl.text())
        if details is None:
            return
        base = list(details.screenshot_urls or ())
        trailer = next((m['url'] for m in enrichment.movies if re.search(r'\.(mp4|webm)(\?|$)', m['url'], re.I)), '')
        extra = [f'video::{trailer}'] if trailer and f'video::{trailer}' not in base else []
        has_shots = any(not u.startswith('video::') for u in base)
        urls = extra + base + ([] if has_shots else list(enrichment.screenshots or ()))
        if urls and urls != base:
            self._gallery.set_urls(urls)
            self._gallery.setVisible(True)

    def _queue_manual_steam(self, appid: int) -> None:
        entry = self._current_entry
        if entry is None:
            return
        self._steam_gen += 1
        gen = self._steam_gen
        self._steam_widget.set_loading()
        self._update_steam_host()
        from src.core.worker import submit

        def _task():
            from src.core.repacks import enrichment as service

            return service.set_manual_match(entry, appid)

        submit(_task, on_done=lambda result, g=gen: self._on_steam_ready(result, g),
               on_error=lambda error, g=gen: self._on_steam_error(error, g),
               owner=self, key=f'steam:manual:{appid}:{entry.url}')

    def _on_steam_refresh(self) -> None:
        entry = self._current_entry
        if entry is None:
            return
        self._steam_gen += 1
        gen = self._steam_gen
        self._steam_widget.set_loading()
        self._update_steam_host()
        try:
            from src.core.repacks import enrichment as service

            service.refresh_enrichment(
                entry, owner=self,
                on_done=lambda result, g=gen: self._on_steam_ready(result, g),
                on_error=lambda error, g=gen: self._on_steam_error(error, g))
        except Exception:
            self._on_steam_error(None, gen)

    def _on_steam_change_match(self) -> None:
        entry = self._current_entry
        if entry is None:
            return
        title = (self._parsed_title.clean if self._parsed_title is not None else '') or entry.title
        dialog = SteamMatchDialog(title, self.window())
        try:
            if dialog.exec():
                appid = dialog.selected_appid()
                if appid:
                    self._queue_manual_steam(int(appid))
        finally:
            dialog.deleteLater()

    def _on_steam_clear_match(self) -> None:
        entry = self._current_entry
        if entry is None:
            return
        self._steam_gen += 1
        self._steam_enrichment = None
        try:
            from src.core.repacks import enrichment as service

            service.clear_match(entry)
        except Exception:
            logger.debug('Could not clear Steam match', exc_info=True)
        self._steam_widget.show_no_match(enabled=True)
        self._sync_steam_extras()
        self._update_steam_host()

    def show_loading(self, entry: RepackEntry) -> None:
        self._current_details = None
        self._current_entry = entry
        self._bookmark_btn.setVisible(False)
        self._steam_btn.setVisible(False)
        self._set_banner('')
        self._set_logo('')
        self._steam_desc_html = ''
        self._is_announcement = False
        self._apply_clean_title(entry.title)
        self._begin_steam(entry)
        self._raw_intro_text = ''
        self._apply_desc_style()
        self._desc_lbl.setText(tr('repacks.loading_details'))
        self._steam_desc.setVisible(False)
        self._desc_lbl.setVisible(True)
        self._clear_sections()
        self._meta_extra = {}
        self._clear_meta()
        self._clear_tags()
        self._gallery.clear()
        self._gallery.setVisible(False)
        self._site_link_btn.setVisible(False)
        self._download_action.set_accent_color(None)
        self._download_action.setVisible(True)
        cover_src = entry.poster_path or entry.poster_url or None
        if not cover_src and self._steam_enrichment is not None:
            art = getattr(self._steam_enrichment, 'artwork', None) or {}
            cover_src = art.get('poster') or art.get('hero') or art.get('banner') or art.get('capsule')
        cover_src = cover_src or getattr(entry, 'steam_poster', '') or None
        self._show_cover(cover_src, entry.title)

    def show_details(self, details: RepackDetails) -> None:
        self._current_details = details
        self._download_action.set_details(details)
        self._apply_clean_title(details.title)
        self._update_bookmark_button()
        if details.url:
            self._site_link_btn.setUrl(details.url)
        sections = _split_description_sections(details.description)
        self._clear_sections()
        self._clear_meta()
        self._clear_tags()
        intro_text = ''
        extra_sections = []
        for heading, body in sections:
            if not heading:
                intro_text = (intro_text + '\n\n' + body).strip() if intro_text else body
            else:
                extra_sections.append((heading, body))
        intro_text, trailing_compat_note = _extract_trailing_backwards_compat(intro_text)
        if trailing_compat_note:
            for i, (heading, body) in enumerate(extra_sections):
                if heading.strip().lower() == 'backwards compatibility':
                    merged = (body + '\n\n' + trailing_compat_note).strip() if body else trailing_compat_note
                    extra_sections[i] = (heading, merged)
                    break
            else:
                extra_sections.append(('Backwards Compatibility', trailing_compat_note))
        intro_text, moved_groups = _peel_list_groups(intro_text)
        extra_sections.extend(moved_groups)
        self._raw_intro_text = intro_text
        self._desc_lbl.setText(render_description_html(intro_text) or tr('repacks.no_description'))
        extra = dict(details.extra or {})
        system_requirements_text = extra.get('system_requirements')
        if system_requirements_text:
            extra_sections.append((tr('repacks.system_requirements'), system_requirements_text))
        is_announcement = bool(extra.pop('is_announcement', False))
        self._is_announcement = is_announcement
        if is_announcement and self._steam_widget is not None:
            self._steam_gen += 1
            self._steam_enrichment = None
            self._steam_widget.clear()
            self._update_steam_host()
        self._sync_steam_extras()
        self._download_action.setVisible(not is_announcement)
        self._site_link_btn.setVisible(bool(details.url) and (not is_announcement))
        self._apply_desc_style()
        if details.size_info:
            extra.setdefault('repack_size', details.size_info)
        self._meta_extra = extra
        self._fill_meta()
        genres_raw = extra.get('genres', '')
        seen_tags: set[str] = set()
        genre_tags: list[str] = []
        for g in genres_raw.split(','):
            g = g.strip()
            key = g.lower()
            if g and key not in seen_tags:
                seen_tags.add(key)
                genre_tags.append(g)
        self._tags_header.setVisible(bool(genre_tags))
        self._tags_container.setVisible(bool(genre_tags))
        for tag_text in genre_tags:
            pill = make_tag_pill(tag_text, self._tags_container)
            pill.adjustSize()
            self._tags_layout.addWidget(pill)
            self._tag_widgets.append(pill)
        if genre_tags:
            QTimer.singleShot(0, self._tags_layout.update)
        self._sections_header.setVisible(bool(extra_sections) or bool(extra.get('repack_features')) or bool(extra.get('game_updates_html')))
        game_updates_html = extra.pop('game_updates_html', '')
        if game_updates_html:
            self._add_section(tr('repacks.game_updates'), html=render_game_updates_html(game_updates_html))
        repack_features_text = extra.pop('repack_features', '')
        if repack_features_text:
            extra_sections = [(h or tr('repacks.repack_features'), b)
                              for h, b in _split_description_sections(repack_features_text)] + extra_sections
        for heading, body in _merge_sections(extra_sections):
            self._add_section(heading, text=body)

        keep = bool(details.cover_url) and details.cover_url == self._cover_src
        self._show_cover(self._cover_src if keep else details.cover_path or details.cover_url, details.title)
        self._gallery.set_urls(details.screenshot_urls)
        if self._steam_enrichment is not None:
            self._apply_steam_artwork(self._steam_enrichment)

    def _fill_meta(self) -> None:
        self._clear_meta()
        extra = dict(self._meta_extra)
        enrichment = self._steam_enrichment
        if enrichment is not None and not self._is_announcement:
            if enrichment.release_date:
                extra['released'] = enrichment.release_date
            if enrichment.metacritic:
                extra['metacritic'] = str(enrichment.metacritic)
        row = 0
        col = 0
        for key, label in self._META_LABELS:
            value = extra.get(key)
            if not value:
                continue
            field = MetaField(label, value)
            span = 2 if len(value) > 28 else 1
            if span == 2 and col == 1:
                row += 1
                col = 0
            self._meta_grid.addWidget(field, row, col, 1, span)
            self._meta_widgets.append(field)
            col += span
            if col >= 2:
                col = 0
                row += 1

    def _add_section(self, heading: str, text: str | None=None, html: str | None=None) -> None:
        section = CollapsibleSection(heading, expanded=self._expanded_state.get(heading, False))
        if html is not None:
            section.set_body_html(html)
        else:
            section.set_body_text(text or '')
        section.toggled.connect(lambda is_open, h=heading: self._expanded_state.__setitem__(h, is_open))
        self._sections_col.insertWidget(self._sections_insert_index, section)
        self._section_widgets.append(section)
        self._sections_insert_index += 1

    def show_error(self, message: str) -> None:
        self._clear_sections()
        self._clear_meta()
        self._clear_tags()
        self._gallery.clear()
        self._gallery.setVisible(False)
        self._desc_lbl.setText(tr('repacks.failed_to_load', message=message))

    def pause_media(self) -> None:
        self._gallery.pause_videos()

    def _clear_sections(self) -> None:
        self._sections_insert_index = self._sections_col.indexOf(self._sections_header) + 1
        for section in self._section_widgets:
            self._sections_col.removeWidget(section)
            section.deleteLater()
        self._section_widgets.clear()

    def _show_cover(self, src: str | None, title: str) -> None:
        if not src:
            self._cover_src = None
            self._cancel_cover()
            self._cover_lbl.setVisible(False)
            self._show_cover_fallback(title)
            return
        self._cover_lbl.setVisible(True)
        self._cover_fallback.setVisible(False)

        def _accent(colors):
            if _qt_is_valid(self) and src == self._cover_src and colors:
                self._download_action.set_accent_color(colors[0])

        if src == self._cover_src:
            images.dominant(src, _accent, owner=self)
            return
        self._cover_src = src
        self._cancel_cover()
        gen = self._cover_gen
        size = (self.COVER_WIDTH, self.COVER_HEIGHT)
        _stop_previous_movie(self._cover_lbl)

        def _apply(pix):
            if gen != self._cover_gen or not _qt_is_valid(self) or pix.isNull():
                return
            self._cover_lbl.setImage(pix)
            self._cover_lbl.setFixedSize(*size)

        hit = images.peek(src, size)
        if hit is not None and not hit.isNull():
            _apply(hit)
        else:
            small = images.peek(src, (PosterCard.POSTER_WIDTH, PosterCard.POSTER_HEIGHT))
            if small is not None and not small.isNull():
                _apply(small)
            self._cover_token = images.get(src, size, _apply, owner=self)
        images.dominant(src, _accent, owner=self)

    def _cancel_cover(self) -> None:
        self._cover_gen += 1
        if self._cover_token:
            images.cancel(self._cover_token)
            self._cover_token = 0

def _fade_in_widget(widget, duration_ms: int = 140, owner=None, slot: str = "_fade_anim") -> None:
    try:
        prev = getattr(owner, slot, None) if owner is not None else None
        if prev is not None:
            try:
                target = getattr(prev, "_fade_target", None)
                effect = getattr(prev, "_fade_effect", None)
                prev.stop()
                if target is not None and _qt_is_valid(target):
                    try:
                        if effect is not None and target.graphicsEffect() is effect:
                            target.setGraphicsEffect(None)
                    except RuntimeError:
                        pass
            except Exception:
                pass
            if owner is not None:
                try:
                    setattr(owner, slot, None)
                except Exception:
                    pass
        if widget is None or not _qt_is_valid(widget) or not widget.isVisible():
            return
        effect = QGraphicsOpacityEffect(widget)
        effect.setOpacity(0.0)
        widget.setGraphicsEffect(effect)
        anim = QPropertyAnimation(effect, b"opacity", owner if owner is not None else widget)
        anim.setDuration(max(1, int(duration_ms)))
        anim.setStartValue(0.0)
        anim.setEndValue(1.0)
        anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        anim._fade_target = widget
        anim._fade_effect = effect

        def _cleanup() -> None:
            try:
                if _qt_is_valid(widget) and widget.graphicsEffect() is effect:
                    widget.setGraphicsEffect(None)
            except RuntimeError:
                pass
            if owner is not None and getattr(owner, slot, None) is anim:
                try:
                    setattr(owner, slot, None)
                except Exception:
                    pass

        anim.finished.connect(_cleanup)
        if owner is not None:
            try:
                setattr(owner, slot, anim)
            except Exception:
                pass
        anim.start(QPropertyAnimation.DeletionPolicy.DeleteWhenStopped)
    except Exception:
        try:
            if widget is not None and _qt_is_valid(widget):
                widget.setGraphicsEffect(None)
        except Exception:
            pass


class SourceTab(MediaPageBase):
    _FADE_MS = 140

    def __init__(self, source_key: str, source_name: str, manager: DownloadManager, parent=None):
        super().__init__(parent)
        self._source_key = source_key
        self._source_name = source_name
        self.setStyleSheet('background: transparent;')
        self._current_page = 0
        self._has_more = True
        self._is_loading = False
        self._loaded_once = False
        self._search_query = ''
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 12, 0, 0)
        layout.setSpacing(8)
        self._status_lbl = CaptionLabel('')
        self._status_lbl.setVisible(False)
        layout.addWidget(self._status_lbl)
        self._stack = QStackedWidget()
        grid_page = QWidget()
        grid_page_layout = QVBoxLayout(grid_page)
        grid_page_layout.setContentsMargins(0, 0, 0, 0)
        grid_page_layout.setSpacing(20)
        grid_row = QWidget()
        self._grid_row = grid_page
        grid_row_layout = QHBoxLayout(grid_row)
        grid_row_layout.setContentsMargins(0, 0, 0, 0)
        grid_row_layout.setSpacing(24)
        carousel = PopularCarousel()
        self._grid = PosterGrid(
            self, header=carousel, subtitle_fn=_source_subtitle, colors_fn=_grid_colors,
            title_attr='display_title', cover_attrs=('poster_path', 'poster_url', 'poster_fallback', 'steam_poster'),
            show_caption=_titles_enabled(), meta_fn=_entry_meta_text, search_fn=_entry_search_text,
            fallback_icon_fn=lambda: themed_icon('repacks.card_fallback', FluentIcon.GAME, size=48, color=_body_text_color()))
        self._caption_on = _titles_enabled()
        self._grid.context_requested.connect(self._show_grid_menu)
        self._grid.poster_clicked.connect(self._show_details)
        self._grid.download_requested.connect(self.open_details_and_download)
        self._grid.near_bottom.connect(self._on_near_bottom)
        self._grid.hovered.connect(self._on_grid_hover)
        self._grid.visible_changed.connect(self._on_visible_changed)
        carousel.hovered.connect(self._on_grid_hover)
        grid_row_layout.addWidget(self._grid, 1)
        grid_page_layout.addWidget(grid_row, 1)
        self._retry_bar = QWidget()
        retry_bar_layout = QHBoxLayout(self._retry_bar)
        retry_bar_layout.setContentsMargins(0, 4, 0, 8)
        retry_bar_layout.addStretch(1)
        self._retry_btn = PushButton(self._safe_tr('repacks.retry_loading', 'Retry loading more'))
        self._retry_btn.clicked.connect(self._on_retry_clicked)
        retry_bar_layout.addWidget(self._retry_btn)
        retry_bar_layout.addStretch(1)
        self._retry_bar.setVisible(False)
        grid_page_layout.addWidget(self._retry_bar)
        self._stack.addWidget(grid_page)
        layout.addWidget(self._stack, 1)
        self._details = RepackDetailsView(manager, source_key, self)
        self._details.back_requested.connect(self._show_grid)
        self._stack.addWidget(self._details)
        self._latest_task = None
        self._auto_click_download = False
        self._load_watchdog = QTimer(self)
        self._load_watchdog.setSingleShot(True)
        self._load_watchdog.setInterval(12000)
        self._load_watchdog.timeout.connect(self._on_load_watchdog)
        self._last_fetch_mono = 0.0
        self._stall_retries = 0
        self._active_fetch_task_id = None
        self._hover_entry = None
        self._prefetch_queue: list[RepackEntry] = []
        self._prefetch_touched: set[str] = set()
        self._prefetch_inflight = 0
        self._prefetch_dwell = QTimer(self)
        self._prefetch_dwell.setSingleShot(True)
        self._prefetch_dwell.setInterval(400)
        self._prefetch_dwell.timeout.connect(self._prefetch_hovered_details)
        self._stack.setCurrentWidget(grid_page)
        self._load_latest_repacks()

    def showEvent(self, event) -> None:
        super().showEvent(event)
        on = _titles_enabled()
        if on == self._caption_on:
            return
        self._caption_on = on
        grid = self._grid
        setter = getattr(grid, 'set_show_caption', None)
        if setter is not None:
            setter(on)
        else:
            for attr in ('show_caption', '_show_caption'):
                if hasattr(grid, attr):
                    setattr(grid, attr, on)
        grid.refresh()

    def _load_latest_repacks(self) -> None:
        if self._latest_task is not None:
            return
        self._latest_task = fetch_popular_repacks_async(self._source_key, on_done=self._on_latest_repacks_loaded, use_cache=True)

    def _on_latest_repacks_loaded(self, entries: list) -> None:
        self._latest_task = None
        if self._search_query:
            return
        self._grid.set_latest_entries(self._decorate_entries(entries or []))

    def _decorate_entries(self, entries):
        for entry in entries or ():
            _decorate_repack_entry(entry)
        return entries

    def open_details_and_download(self, entry: RepackEntry) -> None:
        self._pending_auto_download_url = getattr(entry, 'url', None)
        self._auto_click_download = True
        self._show_details(entry)

    def _on_retry_clicked(self) -> None:
        self._retry_bar.setVisible(False)
        self._stall_retries = 0
        self._set_status('')
        self._load_next_page(use_cache=False)

    def _on_load_watchdog(self) -> None:
        if not self._is_loading:
            return
        if self._active_fetch_task_id is not None:
            cancel_task(self._active_fetch_task_id)
            self._active_fetch_task_id = None
        if self._stall_retries >= 3:
            self._is_loading = False
            self._set_status(tr('repacks.load_failed_message', message=tr('repacks.timed_out')))
            self._retry_bar.setVisible(True)
            logger.error('Repack page load timed out after %d retries for %s', self._stall_retries, self._source_key)
            return
        self._stall_retries += 1
        logger.warning('Repack page load stalled for %s, retrying (attempt %d)', self._source_key, self._stall_retries)
        self._is_loading = False
        self._has_more = True
        QTimer.singleShot(1200, self._on_stall_retry)

    def _on_stall_retry(self) -> None:
        if not self._search_query:
            self._load_next_page(use_cache=False)

    @staticmethod
    def _safe_tr(key: str, fallback: str) -> str:
        try:
            value = tr(key)
        except Exception:
            return fallback
        if not value or value == key:
            return fallback
        return value

    def shutdown(self) -> None:
        self._has_more = False
        self._load_watchdog.stop()
        self._prefetch_dwell.stop()
        self._prefetch_queue.clear()
        if self._active_fetch_task_id is not None:
            cancel_task(self._active_fetch_task_id)
            self._active_fetch_task_id = None
        try:
            self._details._download_action.shutdown()
        except Exception:
            logger.exception('Failed to shut down file-list thread for %s', self._source_key)

    def load_initial(self) -> None:
        if self._loaded_once:
            return
        self._loaded_once = True
        self._load_next_page(use_cache=True)

    def _show_grid_menu(self, entry, global_pos) -> None:
        menu = _mica_menu(self)
        source = getattr(entry, 'source', '') or ''
        url = getattr(entry, 'url', '')
        marked = bookmark_store().has(source, url)
        act_open = QAction(tr('repacks.open_details'), menu)
        act_dl = QAction(tr('repacks.download').capitalize(), menu)
        act_web = QAction(tr('repacks.open_in_website'), menu)
        act_bm = QAction(tr('repacks.remove_bookmark') if marked else tr('repacks.bookmark_it'), menu)
        act_open.triggered.connect(lambda _checked=False: self._show_details(entry))
        act_dl.triggered.connect(lambda _checked=False: self.open_details_and_download(entry))
        act_web.triggered.connect(lambda _checked=False: open_external_url(url))
        act_bm.triggered.connect(lambda _checked=False: bookmark_store().remove(source, url) if marked else bookmark_store().add_entry(entry))
        for act in (act_open, act_dl, act_web, act_bm):
            menu.addAction(act)
        menu.exec(global_pos)

    def filter_grid(self, query: str) -> None:
        query = (query or '').strip()
        if query == self._search_query:
            return
        self._search_query = query
        if not query:
            self._restore_browse_view()
            return


        self._stack.setCurrentWidget(self._grid_row)
        self._grid.set_latest_section_visible(False)
        self._drop_prefetch_queue()
        self._grid.filter_by_title(query)
        self._run_search(query)

    def _run_search(self, query: str) -> None:
        self._set_status(tr('repacks.searching_for', query=query))
        fetch_search_async(self._source_key, query, 1, on_done=lambda result, q=query: self._on_search_done(q, result), on_error=lambda message, q=query: self._on_search_error(q, message), use_cache=False)

    def _on_search_done(self, query: str, result) -> None:
        if query != self._search_query:
            return
        self._grid.set_latest_section_visible(False)
        self._grid.set_entries(self._decorate_entries(result.entries))
        self._stack.setCurrentWidget(self._grid_row)
        self._set_status('' if result.entries else tr('repacks.no_games_matching', query=query))

    def _on_search_error(self, query: str, message: str) -> None:
        if query != self._search_query:
            return
        if message == '__no_search__':
            self._grid.set_latest_section_visible(False)
            self._grid.filter_by_title(query)
            self._stack.setCurrentWidget(self._grid_row)
            self._set_status('')
            return
        self._stack.setCurrentWidget(self._grid_row)
        self._set_status(tr('repacks.search_failed', message=message))
        logger.error('Repack search failed for %s (%r): %s', self._source_key, query, message)

    def _restore_browse_view(self) -> None:
        self._stack.setCurrentWidget(self._grid_row)
        self._set_status('')
        self._retry_bar.setVisible(False)
        self._grid.set_latest_section_visible(True)
        self._grid.clear()
        self._drop_prefetch_queue()
        self._current_page = 0
        self._has_more = True
        self._is_loading = False
        self._loaded_once = False
        self.load_initial()

    def refresh(self) -> None:
        from src.core.repacks.base import clear_source_cache
        clear_source_cache(self._source_key)
        self._drop_prefetch_queue()
        self._retry_bar.setVisible(False)
        self._grid.clear()
        self._current_page = 0
        self._has_more = True
        self._load_next_page(use_cache=False)
        self._load_latest_repacks()

    def _on_near_bottom(self) -> None:
        if not _qt_is_valid(self) or not _qt_is_valid(self._grid):
            return
        if self._search_query or not self._has_more or self._is_loading:
            return
        bar = self._grid.verticalScrollBar()
        if bar.maximum() - bar.value() > self._grid._NEAR_BOTTOM_THRESHOLD_PX:
            return
        self._load_next_page(use_cache=True)

    def _load_next_page(self, use_cache: bool) -> None:
        if not _qt_is_valid(self):
            return
        if self._is_loading or not self._has_more:
            return
        self._is_loading = True
        self._load_watchdog.start()
        next_page = self._current_page + 1
        self._active_fetch_task_id = fetch_page_async(self._source_key, next_page, on_done=self._on_page_loaded, on_error=self._on_page_error, use_cache=use_cache)

    def _on_page_loaded(self, result) -> None:
        if not _qt_is_valid(self) or not _qt_is_valid(self._grid):
            return
        self._load_watchdog.stop()
        self._active_fetch_task_id = None
        self._stall_retries = 0
        self._last_fetch_mono = time.monotonic()
        self._is_loading = False
        self._retry_bar.setVisible(False)
        if self._current_page and result.page <= self._current_page:
            return
        self._current_page = result.page
        self._has_more = result.has_more
        before = self._grid.count_entries()
        self._grid.append_entries(self._decorate_entries(result.entries))
        added = self._grid.count_entries() - before
        logger.info('Repack page %d: +%d entries, has_more=%s', result.page, added, result.has_more)
        if added == 0 and result.entries:
            self._stale_pages = getattr(self, '_stale_pages', 0) + 1
            if self._stale_pages >= 3:
                self._has_more = False
                logger.warning('Repack feed repeating pages; stopped at page %d', result.page)
        elif added > 0:
            self._stale_pages = 0
        if not result.entries and self._current_page == 1:
            self._set_status(tr('repacks.no_repacks_found'))
        else:
            self._set_status('')
        QTimer.singleShot(150, self._on_near_bottom)

    def _on_page_error(self, message: str) -> None:
        if not _qt_is_valid(self):
            return
        self._load_watchdog.stop()
        self._active_fetch_task_id = None
        self._is_loading = False
        self._set_status(tr('repacks.failed_to_load', message=message))
        logger.error('Repack page load failed for %s: %s', self._source_key, message)

    def _set_status(self, text: str) -> None:
        self._status_lbl.setText(text)
        self._status_lbl.setVisible(bool(text))

    def _switch_stack_animated(self, widget) -> None:
        if self._stack.currentWidget() is widget:
            return
        try:
            from src.gui.main_window import get_sound_manager
            get_sound_manager().play('navigate')
        except Exception:
            pass
        super()._switch_stack_animated(widget)
        _fade_in_widget(widget, self._FADE_MS, self)

    def _show_details(self, entry: RepackEntry) -> None:
        self._auto_click_download = getattr(entry, 'url', None) == getattr(self, '_pending_auto_download_url', None)
        self._prefetch_dwell.stop()
        self._hover_entry = None
        self._forget_prefetch(entry)
        self._details.show_loading(entry)
        self._switch_stack_animated(self._details)
        fetch_details_async(self._source_key, entry, on_done=self._on_details_loaded, on_error=self._on_details_error, use_cache=True)

    def _on_grid_hover(self, entry) -> None:
        self._prefetch_dwell.stop()
        self._hover_entry = entry
        if entry is not None:
            self._prefetch_dwell.start()

    def _prefetch_hovered_details(self) -> None:
        entry, self._hover_entry = self._hover_entry, None
        if entry is None or not _qt_is_valid(self):
            return
        if self._stack.currentWidget() is not self._grid_row:
            return
        self._queue_details_prefetch([entry], front=True)

    def _on_visible_changed(self, entries) -> None:
        if not _qt_is_valid(self) or self._stack.currentWidget() is not self._grid_row:
            return
        self._queue_details_prefetch(entries[:_DETAILS_PREFETCH_PER_SCREEN])

    def _queue_details_prefetch(self, entries, front: bool = False) -> None:
        fresh = [e for e in entries
                 if getattr(e, 'url', None) and e.url not in self._prefetch_touched]
        if not fresh:
            return
        for e in fresh:
            self._prefetch_touched.add(e.url)
        if front:
            self._prefetch_queue[0:0] = fresh
        else:
            self._prefetch_queue.extend(fresh)
        self._pump_details_prefetch()

    def _pump_details_prefetch(self) -> None:
        while self._prefetch_inflight < _DETAILS_PREFETCH_MAX and self._prefetch_queue:
            entry = self._prefetch_queue.pop(0)
            self._prefetch_inflight += 1
            fetch_details_async(self._source_key, entry, on_done=self._on_prefetch_settled,
                                on_error=self._on_prefetch_settled, use_cache=True, owner=self,
                                priority=Priority.PREFETCH)

    def _on_prefetch_settled(self, _result=None) -> None:
        self._prefetch_inflight = max(0, self._prefetch_inflight - 1)
        self._pump_details_prefetch()

    def _forget_prefetch(self, entry) -> None:
        url = getattr(entry, 'url', None)
        if not url:
            return
        self._prefetch_touched.add(url)
        if self._prefetch_queue:
            self._prefetch_queue = [e for e in self._prefetch_queue if getattr(e, 'url', None) != url]

    def _drop_prefetch_queue(self) -> None:
        self._prefetch_queue.clear()

    def _on_details_loaded(self, details) -> None:
        self._details.show_details(details)
        if self._auto_click_download:
            self._auto_click_download = False
            QTimer.singleShot(0, self._details._download_action._on_click)

    def _on_details_error(self, message: str) -> None:
        self._details.show_error(message)
        logger.error('Repack details load failed for %s: %s', self._source_key, message)

    def _show_grid(self) -> None:
        self._auto_click_download = False
        self._pending_auto_download_url = None
        self._switch_stack_animated(self._grid_row)
        self._details.pause_media()
        QTimer.singleShot(230, self._refresh_grid_after_show)

    def _refresh_grid_after_show(self) -> None:
        self._grid.refresh()


from PySide6.QtGui import QPainter, QPixmap, QPainterPath, QCursor, QFont as _QFont
from PySide6.QtCore import QRectF, QPointF

_FEED_URL = 'https://fitgirl-repacks.site/feed/'
_FEED_POLL_MS = 30 * 60 * 1000
_FEED_FIRST_CHECK_MS = 10 * 1000
_FEED_UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) PiraChest/1.0'


def _norm_title(title: str) -> str:
    t = (title or '').lower().replace('&amp;', '&').replace('&#8217;', "'").replace('’', "'")
    t = re.split(r'\s[–—-]\s|\s\+\s|,\s*from\s|\sv\d', t)[0]
    t = re.sub(r'\(.*?\)|\[.*?\]', ' ', t)
    t = re.sub(r"[^a-z0-9]+", ' ', t)
    return t.strip()


def _watch_path() -> str:
    return os.path.join(os.path.dirname(_bookmarks_path()), 'pirachest_upcoming_watch.json')


class _UpcomingWatchStore(QObject):
    changed = Signal()
    _lock = threading.Lock()

    def __init__(self):
        super().__init__()
        self._items: dict[str, str] = {}
        try:
            with open(_watch_path(), 'r', encoding='utf-8') as fh:
                data = _json.load(fh)
            if isinstance(data, dict):
                self._items = {str(k): str(v) for k, v in data.items() if k}
        except Exception:
            pass

    def _save(self) -> None:
        try:
            path = _watch_path()
            os.makedirs(os.path.dirname(path), exist_ok=True)
            tmp = path + '.tmp'
            with open(tmp, 'w', encoding='utf-8') as fh:
                _json.dump(self._items, fh, ensure_ascii=False, indent=1)
            os.replace(tmp, path)
        except Exception:
            logger.exception('Failed to save upcoming watch list')

    def snapshot(self) -> dict[str, str]:
        with self._lock:
            return dict(self._items)

    def has(self, title: str) -> bool:
        with self._lock:
            return _norm_title(title) in self._items

    def toggle(self, title: str) -> bool:
        key = _norm_title(title)
        if not key:
            return False
        with self._lock:
            if key in self._items:
                del self._items[key]
                watched = False
            else:
                self._items[key] = title
                watched = True
        self._save()
        self.changed.emit()
        return watched

    def remove_keys(self, keys) -> None:
        with self._lock:
            for k in keys:
                self._items.pop(k, None)
        self._save()
        self.changed.emit()


_watch_store = None


def upcoming_watch_store() -> _UpcomingWatchStore:
    global _watch_store
    if _watch_store is None:
        _watch_store = _UpcomingWatchStore()
    return _watch_store


class _FeedCheckThread(QThread):
    hits = Signal(list)

    def __init__(self, watched: dict, parent=None):
        super().__init__(parent)
        self._watched = watched

    @staticmethod
    def _get(url: str, limit: int) -> bytes:
        import urllib.request
        req = urllib.request.Request(url, headers={'User-Agent': _FEED_UA, 'Accept': '*/*'})
        with urllib.request.urlopen(req, timeout=20) as resp:
            return resp.read(limit)

    def run(self) -> None:
        found: list[dict] = []
        try:
            import html as _html
            import xml.etree.ElementTree as ET
            root = ET.fromstring(self._get(_FEED_URL, 4 * 1024 * 1024))
            ns_content = '{http://purl.org/rss/1.0/modules/content/}encoded'
            seen: set[str] = set()
            for item in root.iter('item'):
                raw_title = _html.unescape((item.findtext('title') or '').strip())
                key = _norm_title(raw_title)
                if not key or key in seen or key not in self._watched:
                    continue
                seen.add(key)
                body = item.findtext(ns_content) or item.findtext('description') or ''
                m = re.search(r'<img[^>]+src=["\']([^"\']+)["\']', body)
                poster_url = _html.unescape(m.group(1)) if m else ''
                data = b''
                if poster_url:
                    try:
                        data = self._get(poster_url, 4 * 1024 * 1024)
                    except Exception:
                        data = b''
                found.append({'key': key, 'title': self._watched[key], 'feed_title': raw_title,
                              'url': (item.findtext('link') or '').strip(), 'poster_url': poster_url,
                              'poster_bytes': data})
        except Exception:
            logger.exception('FitGirl feed check failed')
        self.hits.emit(found)


class _ReleaseWatcher(QObject):
    released = Signal(list)

    def __init__(self):
        super().__init__()
        self._thread: _FeedCheckThread | None = None
        self._timer = QTimer(self)
        self._timer.setInterval(_FEED_POLL_MS)
        self._timer.timeout.connect(self.check_now)

    def start(self) -> None:
        if not self._timer.isActive():
            self._timer.start()
            QTimer.singleShot(_FEED_FIRST_CHECK_MS, self.check_now)

    def stop(self) -> None:
        self._timer.stop()
        thread = self._thread
        if thread is not None and _qt_is_valid(thread):
            thread.wait(2000)

    def check_now(self) -> None:
        if self._thread is not None:
            return
        watched = upcoming_watch_store().snapshot()
        if not watched:
            return
        thread = _FeedCheckThread(watched)
        self._thread = thread
        thread.hits.connect(self._on_hits)
        thread.finished.connect(self._on_finished)
        thread.start()

    def _on_finished(self) -> None:
        thread, self._thread = self._thread, None
        if thread is not None:
            thread.deleteLater()

    def _on_hits(self, hits: list) -> None:
        if not hits:
            return
        upcoming_watch_store().remove_keys([h['key'] for h in hits])
        self.released.emit(hits)


_release_watcher = None


def release_watcher() -> _ReleaseWatcher:
    global _release_watcher
    if _release_watcher is None:
        _release_watcher = _ReleaseWatcher()
    return _release_watcher


def _ink(alpha: int) -> QColor:
    base = 255 if isDarkTheme() else 0
    return QColor(base, base, base, alpha)


def _bell_icon(active: bool):
    icon = getattr(FluentIcon, 'RINGER', None) or getattr(FluentIcon, 'ALARM_CLOCK', None) or FluentIcon.HEART
    if active:
        try:
            return icon.icon(color=themeColor())
        except Exception:
            pass
    return icon


class _UpcomingRow(QWidget):
    HEIGHT = 54

    def __init__(self, index: int, title: str, parent=None):
        super().__init__(parent)
        self._title, self._index, self._hover = title, index, False
        self._watched = upcoming_watch_store().has(title)
        self.setFixedHeight(self.HEIGHT)
        self.setAttribute(Qt.WidgetAttribute.WA_Hover, True)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(60, 0, 10, 0)
        lay.setSpacing(8)
        self._lbl = BodyLabel(title)
        self._lbl.setWordWrap(True)
        self._lbl.setStyleSheet(f"font-size: 14px; font-weight: 600; color: {palette()['primary_text']}; background: transparent;")
        lay.addWidget(self._lbl, 1)
        self._bell = TransparentToolButton()
        self._bell.setFixedSize(34, 34)
        self._bell.setIconSize(QSize(18, 18))
        self._bell.clicked.connect(self._toggle)
        lay.addWidget(self._bell)
        self._sync()

    def _toggle(self) -> None:
        self._watched = upcoming_watch_store().toggle(self._title)
        self._sync()
        if self._watched:
            QTimer.singleShot(400, release_watcher().check_now)

    def _sync(self) -> None:
        self._bell.setIcon(_bell_icon(self._watched))
        self._bell.setToolTip(tr('repacks.notify_off', default='Notifications on – click to turn off') if self._watched
                              else tr('repacks.notify_on', default='Notify me when this is released'))
        self.update()

    def enterEvent(self, event):
        self._hover = True
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event):
        self._hover = False
        self.update()
        super().leaveEvent(event)

    def paintEvent(self, event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        accent = themeColor()
        fill = _ink(22 if self._hover else 12)
        if self._watched:
            fill = QColor(accent.red(), accent.green(), accent.blue(), 34)
        p.setPen(QColor(accent.red(), accent.green(), accent.blue(), 120) if self._watched else _ink(20))
        p.setBrush(fill)
        p.drawRoundedRect(r, 10, 10)

        badge = QRectF(14, (self.height() - 30) / 2, 30, 30)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(accent.red(), accent.green(), accent.blue(), 200 if self._watched else 70))
        p.drawEllipse(badge)
        f = _QFont(self.font())
        f.setPixelSize(12)
        f.setWeight(_QFont.Weight.Bold)
        p.setFont(f)
        p.setPen(QColor('white') if self._watched else (QColor('white') if isDarkTheme() else QColor(30, 30, 30)))
        p.drawText(badge, int(Qt.AlignmentFlag.AlignCenter), str(self._index))
        p.end()


class UpcomingRepacksCard(QWidget):
    CARD_WIDTH = 580
    CARD_HEIGHT = 520

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedSize(self.CARD_WIDTH, self.CARD_HEIGHT)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 0, 4, 0)
        layout.setSpacing(6)
        head = QHBoxLayout()
        head.setSpacing(10)
        self._title_lbl = TitleLabel(tr('repacks.upcoming_repacks'))
        self._title_lbl.setStyleSheet(f"font-weight: 800; color: {palette()['primary_text']};")
        head.addWidget(self._title_lbl, 1)
        self._count_lbl = CaptionLabel('')
        self._count_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._count_lbl.setFixedHeight(22)
        self._count_lbl.setStyleSheet(f'background-color: {_surface_tint_color(14)}; border-radius: 11px; padding: 0 10px; color: {_body_text_color()};')
        self._count_lbl.hide()
        head.addWidget(self._count_lbl, 0, Qt.AlignmentFlag.AlignVCenter)
        layout.addLayout(head)
        self._date_lbl = CaptionLabel('')
        self._date_lbl.setTextColor(_qcolor_from_palette(palette(dark=False)['muted']), _qcolor_from_palette(palette(dark=True)['muted']))
        layout.addWidget(self._date_lbl)
        self._hint_lbl = CaptionLabel(tr('repacks.notify_hint', default='Tap the bell next to a game to be notified when it is released.'))
        self._hint_lbl.setWordWrap(True)
        self._hint_lbl.setStyleSheet(f'color: {_muted_text_color()};')
        self._hint_lbl.hide()
        layout.addWidget(self._hint_lbl)
        self._scroll = SmoothScrollArea(self)
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._scroll.setStyleSheet('QScrollArea { background: transparent; border: none; }')
        self._scroll.viewport().setStyleSheet('background: transparent;')
        self._list_container = QWidget()
        self._list_container.setStyleSheet('background: transparent;')
        self._list_layout = QVBoxLayout(self._list_container)
        self._list_layout.setContentsMargins(0, 8, 8, 4)
        self._list_layout.setSpacing(8)
        self._list_layout.addStretch(1)
        self._scroll.setWidget(self._list_container)
        layout.addWidget(self._scroll, 1)
        self._empty_lbl = BodyLabel(tr('repacks.loading'))
        self._empty_lbl.setWordWrap(True)
        self._empty_lbl.setStyleSheet(f'color: {_muted_text_color()};')
        self._list_layout.insertWidget(0, self._empty_lbl)
        self._entry_labels: list[_UpcomingRow] = []

    def _clear_rows(self) -> None:
        for row in self._entry_labels:
            self._list_layout.removeWidget(row)
            row.deleteLater()
        self._entry_labels.clear()

    def set_titles(self, title: str, date_text: str, game_titles: list[str]) -> None:
        self._title_lbl.setText(title or tr('repacks.upcoming_repacks'))
        self._date_lbl.setText(date_text)
        self._date_lbl.setVisible(bool(date_text))
        self._clear_rows()
        game_titles = [t for t in game_titles if t and (not t.strip().startswith('#'))]
        self._count_lbl.setVisible(bool(game_titles))
        self._hint_lbl.setVisible(bool(game_titles))
        if not game_titles:
            self._empty_lbl.setText(tr('repacks.no_upcoming_repacks'))
            self._empty_lbl.setVisible(True)
            return
        self._count_lbl.setText(str(len(game_titles)))
        self._empty_lbl.setVisible(False)
        for i, game_title in enumerate(game_titles, 1):
            row = _UpcomingRow(i, game_title.strip())
            self._list_layout.insertWidget(self._list_layout.count() - 1, row)
            self._entry_labels.append(row)

    def set_error(self, message: str) -> None:
        self._clear_rows()
        self._date_lbl.setVisible(False)
        self._count_lbl.hide()
        self._hint_lbl.hide()
        self._empty_lbl.setText(tr('repacks.failed_to_load', message=message))
        self._empty_lbl.setVisible(True)


class UpcomingRepacksDialog(MessageBoxBase):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.yesButton.setText(tr('repacks.close'))
        self.hideCancelButton()
        self._card = UpcomingRepacksCard(self)
        self.viewLayout.addWidget(self._card)
        self.widget.setFixedWidth(self._card.CARD_WIDTH + 48)
        self.finished.connect(self._force_repaint)

    def _force_repaint(self, *_):
        try:
            win = self.window()
            if win is not None:
                win.update()
        except Exception:
            pass

    def show_loading(self) -> None:
        self._card.set_titles(tr('repacks.upcoming_repacks'), '', [])
        self._card._empty_lbl.setText(tr('repacks.loading'))

    def show_details(self, details: RepackDetails) -> None:
        try:
            from datetime import datetime
            date_text = datetime.now().strftime('%d/%m/%Y')
            titles = list((details.extra or {}).get('upcoming_titles') or [])
            self._card.set_titles(details.title or tr('repacks.upcoming_repacks'), date_text, titles)
        except RuntimeError:
            pass

    def show_error(self, message: str) -> None:
        try:
            self._card.set_error(message)
        except RuntimeError:
            pass


class _ReleasedPoster(QWidget):
    clicked = Signal(dict)
    W, H = 168, 236

    def __init__(self, hit: dict, parent=None):
        super().__init__(parent)
        self._hit, self._hover = hit, False
        self._pm = QPixmap()
        data = hit.get('poster_bytes') or b''
        if data:
            self._pm.loadFromData(data)
        self.setFixedSize(self.W, self.H + 46)
        self.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        self.setAttribute(Qt.WidgetAttribute.WA_Hover, True)
        self.setToolTip(hit.get('title', ''))


    def enterEvent(self, event):
        self._hover = True
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event):
        self._hover = False
        self.update()
        super().leaveEvent(event)

    def mouseReleaseEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton and self.rect().contains(event.position().toPoint()):
            self.clicked.emit(self._hit)

    def paintEvent(self, event) -> None:
        p = QPainter(self)
        p.setRenderHints(QPainter.RenderHint.Antialiasing | QPainter.RenderHint.SmoothPixmapTransform)
        lift = 6 if self._hover else 0
        cover = QRectF(0, 8 - lift, self.W, self.H - 8)
        path = QPainterPath()
        path.addRoundedRect(cover, 10, 10)
        p.save()
        p.setClipPath(path)
        if not self._pm.isNull():
            scaled = self._pm.scaled(cover.size().toSize(), Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                                     Qt.TransformationMode.SmoothTransformation)
            p.drawPixmap(int(cover.x() - (scaled.width() - cover.width()) / 2),
                         int(cover.y() - (scaled.height() - cover.height()) / 2), scaled)
        else:
            p.fillRect(cover, _ink(30))
        p.restore()
        if self._hover:
            p.setPen(QColor(themeColor()))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawRoundedRect(cover.adjusted(1, 1, -1, -1), 10, 10)
        f = _QFont(self.font())
        f.setPixelSize(13)
        f.setWeight(_QFont.Weight.DemiBold)
        p.setFont(f)
        p.setPen(_strong_text_qcolor())
        p.drawText(QRectF(2, self.H + 6, self.W - 4, 38),
                   int(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop | Qt.TextFlag.TextWordWrap),
                   self._hit.get('title', ''))
        p.end()


class ReleasedRepacksDialog(MessageBoxBase):
    open_requested = Signal(dict)
    COLS = 3

    def __init__(self, hits: list, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.yesButton.setText(tr('repacks.close'))
        self.hideCancelButton()
        self._title = TitleLabel('')
        self._title.setStyleSheet(f"font-weight: 800; color: {palette()['primary_text']};")
        self.viewLayout.addWidget(self._title)
        self._sub = CaptionLabel(tr('repacks.released_hint', default='Click a poster to open the game.'))
        self._sub.setStyleSheet(f'color: {_muted_text_color()};')
        self.viewLayout.addWidget(self._sub)
        self._scroll = SmoothScrollArea(self)
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._scroll.setStyleSheet('QScrollArea { background: transparent; border: none; }')
        self._scroll.viewport().setStyleSheet('background: transparent;')
        self._body = QWidget()
        self._body.setStyleSheet('background: transparent;')
        self._grid = QGridLayout(self._body)
        self._grid.setContentsMargins(0, 12, 0, 4)
        self._grid.setHorizontalSpacing(16)
        self._grid.setVerticalSpacing(14)
        self._scroll.setWidget(self._body)
        self.viewLayout.addWidget(self._scroll)
        self._hits: list[dict] = []
        self.add_items(hits)
        self.widget.setFixedWidth(self.COLS * _ReleasedPoster.W + (self.COLS - 1) * 16 + 80)

    def add_items(self, hits: list) -> None:
        self._hits.extend(hits)
        while self._grid.count():
            w = self._grid.takeAt(0).widget()
            if w is not None:
                w.deleteLater()
        for i, hit in enumerate(self._hits):
            tile = _ReleasedPoster(hit)
            tile.clicked.connect(self._on_clicked)
            self._grid.addWidget(tile, i // self.COLS, i % self.COLS)
        n = len(self._hits)
        if n == 1:
            self._title.setText(tr('repacks.released_title_one', default='Your game is out!'))
        else:
            text = tr('repacks.released_title_many', default='{count} games you were waiting for are out!', count=n)
            self._title.setText(text.replace('{count}', str(n)))
        rows = -(-n // self.COLS)
        self._scroll.setFixedHeight(min(2, rows) * (_ReleasedPoster.H + 46 + 14) + 20)

    def _on_clicked(self, hit: dict) -> None:
        self.accept()
        self.open_requested.emit(hit)


def _released_entry(hit: dict) -> RepackEntry:
    return RepackEntry(source='fitgirl', title=hit.get('feed_title') or hit.get('title', ''),
                       url=hit.get('url', ''), poster_url=hit.get('poster_url') or None, poster_path=None)


class _ReleaseNotifier(QObject):
    def __init__(self):
        super().__init__()
        self._page_ref = None
        self._dialog = None
        self._tray = None
        release_watcher().released.connect(self._on_released)

    def attach_page(self, page) -> None:
        import weakref
        self._page_ref = weakref.ref(page)

    def _page(self):
        page = self._page_ref() if self._page_ref else None
        return page if page is not None and _qt_is_valid(page) else None

    def _main_window(self):
        page = self._page()
        if page is not None:
            return page.window()
        tops = [w for w in QApplication.topLevelWidgets()
                if w.isVisible() and w.parent() is None and not isinstance(w, QDialog)]
        return max(tops, key=lambda w: w.width() * w.height()) if tops else None

    def _on_released(self, hits: list) -> None:
        self._show_toast(hits)
        self._show_dialog(hits)

    def _show_dialog(self, hits: list) -> None:
        dialog = self._dialog
        if dialog is not None and _qt_is_valid(dialog) and dialog.isVisible():
            dialog.add_items(hits)
            return
        dialog = ReleasedRepacksDialog(hits, self._main_window())
        dialog.open_requested.connect(self._open_hit)
        dialog.finished.connect(lambda *_: setattr(self, '_dialog', None))
        self._dialog = dialog
        dialog.show()
        dialog.raise_()

    def _open_hit(self, hit: dict) -> None:
        page = self._page()
        if page is not None:
            page._open_released(hit)
            return

        win = self._main_window()
        target = getattr(win, 'pc_games_page', None)
        if target is not None and hasattr(win, 'switchTo'):
            win.switchTo(target)
            target.call_when_ready('open_detail', _released_entry(hit), 'fitgirl')
        elif hit.get('url'):
            open_external_url(hit.get('url') or '')

    def _show_toast(self, hits: list) -> None:
        try:
            if not QSystemTrayIcon.isSystemTrayAvailable():
                return
            app = QApplication.instance()
            icon = app.windowIcon()
            if icon.isNull():
                icon = app.style().standardIcon(QStyle.StandardPixmap.SP_ComputerIcon)
            old, self._tray = self._tray, None
            if old is not None:
                old.hide()
                old.deleteLater()
            tray = QSystemTrayIcon(icon, self)
            tray.setToolTip('PiraChest')
            tray.messageClicked.connect(self._on_toast_clicked)
            tray.show()
            names = [h.get('title', '') for h in hits]
            if len(names) == 1:
                body = tr('repacks.toast_one', default='{title} has been released!').replace('{title}', names[0])
            else:
                shown = ', '.join(names[:2])
                rest = len(names) - 2
                body = shown + (f' +{rest}' if rest > 0 else '') + ' ' + tr('repacks.toast_many', default='have been released!')
            tray.showMessage(tr('repacks.toast_title', default='Repack released'), body,
                             QSystemTrayIcon.MessageIcon.Information, 12000)
            self._tray = tray

            def _cleanup(t=tray):
                if self._tray is t:
                    self._tray = None
                if _qt_is_valid(t):
                    t.hide()
                    t.deleteLater()
            QTimer.singleShot(20000, _cleanup)
        except Exception:
            logger.exception('Failed to show release notification')

    def _on_toast_clicked(self) -> None:
        win = self._main_window()
        if win is not None:
            win.showNormal()
            win.raise_()
            win.activateWindow()
        dialog = self._dialog
        if dialog is not None and _qt_is_valid(dialog):
            dialog.raise_()


_release_notifier = None


def release_notifier() -> _ReleaseNotifier:
    global _release_notifier
    if _release_notifier is None:
        _release_notifier = _ReleaseNotifier()
    return _release_notifier


def start_release_watching() -> None:
    release_notifier()
    release_watcher().start()


def _autostart_release_watching() -> None:
    if QCoreApplication.instance() is not None:
        QTimer.singleShot(0, start_release_watching)


_autostart_release_watching()


class RepacksPage(QWidget):
    def __init__(self, manager: DownloadManager, parent=None):
        super().__init__(parent)
        self._manager = manager
        self.setObjectName('repacksPage')
        self.setStyleSheet('background: transparent;')
        outer = QVBoxLayout(self)


        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(12)
        self._pivot = Pivot()
        self._pivot.setContentsMargins(24, 12, 24, 0)
        self._header = QWidget(self)
        header_layout = QVBoxLayout(self._header)
        header_layout.setContentsMargins(0, 0, 0, 0)
        header_layout.setSpacing(12)
        header_layout.addWidget(self._pivot)
        self._top_state = None
        self._floating = False
        self._header_h = 0
        self._header.installEventFilter(self)
        self._search_bar = SearchLineEdit()
        self._search_bar.setPlaceholderText(tr('repacks.search_placeholder'))
        self._search_bar.setMinimumWidth(160)
        self._search_bar.setMaximumWidth(320)
        self._search_bar.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self._search_debounce = QTimer(self)
        self._search_debounce.setSingleShot(True)
        self._search_debounce.setInterval(250)
        self._search_debounce.timeout.connect(lambda: self._on_search_text_changed(self._search_bar.text()))
        self._search_bar.textChanged.connect(lambda _text: self._search_debounce.start())
        self._search_bar.searchSignal.connect(self._on_search_immediate)
        self._search_bar.clearSignal.connect(lambda: self._on_search_immediate(''))
        self._donate_btn = PushButton(themed_icon('repacks.donate', FluentIcon.HEART), tr('repacks.donate_to_fitgirl'))
        apply_tool_button_theme(self._donate_btn)
        apply_slot_icon_theme(self._donate_btn, 'repacks.donate', FluentIcon.HEART)
        self._donate_btn.clicked.connect(self._on_donate_clicked)
        self._upcoming_btn = PushButton(themed_icon('repacks.upcoming', FluentIcon.CALENDAR), tr('repacks.upcoming_repacks'))
        apply_tool_button_theme(self._upcoming_btn)
        apply_slot_icon_theme(self._upcoming_btn, 'repacks.upcoming', FluentIcon.CALENDAR)
        self._upcoming_btn.clicked.connect(self._on_upcoming_clicked)
        self._refresh_btn = TransparentToolButton(themed_icon('repacks.refresh', FluentIcon.SYNC))
        apply_tool_button_theme(self._refresh_btn)
        self._refresh_btn.setToolTip(tr('repacks.refresh_tooltip'))
        self._refresh_btn.setFixedSize(32, 32)
        self._refresh_btn.clicked.connect(self._on_refresh_clicked)
        self._view_btn = ViewModeButton(parent=self, list_tip=tr('repacks.list_view', default='List view'),
                                        grid_tip=tr('repacks.grid_view', default='Grid view'))
        apply_tool_button_theme(self._view_btn)
        self._bookmarks_btn = TransparentToolButton()
        apply_tool_button_theme(self._bookmarks_btn)
        self._bookmarks_btn.setIcon(_bookmark_icon(True))
        self._bookmarks_btn.setIconSize(QSize(22, 22))
        self._bookmarks_badge = QLabel(self._bookmarks_btn)
        self._bookmarks_badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._bookmarks_badge.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self._bookmarks_badge.setStyleSheet(count_badge_qss())
        self._bookmarks_badge.hide()
        QTimer.singleShot(0, self._sync_bookmark_badge)
        self._bookmarks_btn.setToolTip(tr('repacks.bookmarks_tooltip'))
        self._bookmarks_btn.setFixedSize(32, 32)
        self._bookmarks_btn.setCheckable(True)
        self._bookmarks_btn.clicked.connect(self._on_bookmarks_clicked)
        self._discord_btn = PushButton(themed_icon('repacks.discord', FluentIcon.CHAT), tr('repacks.gog_discord'))
        apply_tool_button_theme(self._discord_btn)
        apply_slot_icon_theme(self._discord_btn, 'repacks.discord', FluentIcon.CHAT)
        self._discord_btn.clicked.connect(self._on_discord_clicked)
        search_row = QHBoxLayout()
        search_row.setContentsMargins(24, 0, 24, 0)
        search_row.setSpacing(8)
        search_row.addWidget(self._search_bar, 0, Qt.AlignmentFlag.AlignLeft)
        search_row.addWidget(self._donate_btn, 0, Qt.AlignmentFlag.AlignLeft)
        search_row.addWidget(self._upcoming_btn, 0, Qt.AlignmentFlag.AlignLeft)
        search_row.addWidget(self._discord_btn, 0, Qt.AlignmentFlag.AlignLeft)
        search_row.addWidget(self._bookmarks_btn, 0, Qt.AlignmentFlag.AlignLeft)
        search_row.addWidget(self._refresh_btn, 0, Qt.AlignmentFlag.AlignLeft)
        search_row.addWidget(self._view_btn, 0, Qt.AlignmentFlag.AlignLeft)
        search_row.addStretch(1)
        header_layout.addLayout(search_row)
        outer.addWidget(self._header)
        self._stack = QStackedWidget()
        outer.addWidget(self._stack, 1)
        self._tabs: dict[str, SourceTab] = {}
        self._tab_display_names: dict[str, str] = {}
        self._tab_fade_anim: QPropertyAnimation | None = None
        self._current_tab_key: str | None = None
        self._prev_tab_key: str | None = None
        self._bookmark_page = None
        bookmark_store().changed.connect(self._sync_bookmark_badge)
        self._add_source_tab('fitgirl', 'FitGirl Repacks')
        self._add_source_tab('gog', 'GOG Revived')
        self._rebuild_tab_selector()
        register_theme_refresh(self, self._refresh_pivot_icons)
        register_theme_refresh(self, self._refresh_badge_style)
        from src.core.config import tab_visibility_bus as _tabvis_bus
        _tabvis_bus.changed.connect(self._on_tab_visibility_changed)
        app = QApplication.instance()
        if app is not None:
            app.aboutToQuit.connect(self._on_app_about_to_quit)
        qconfig.themeChanged.connect(self._on_global_theme_changed)
        register_locale_refresh(self, self._apply_locale)
        release_notifier().attach_page(self)
        start_release_watching()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._sync_top()

    def eventFilter(self, obj, event) -> bool:
        if obj is self._header and event.type() in (QEvent.Type.LayoutRequest, QEvent.Type.Show, QEvent.Type.Hide):
            QTimer.singleShot(0, self._sync_top)
        return super().eventFilter(obj, event)

    def _sync_top(self) -> None:
        tab = self._stack.currentWidget()
        details = getattr(tab, '_details', None)
        inner = getattr(tab, '_stack', None)
        active = (details is not None and inner is not None and inner.currentWidget() is details
                  and details.isVisible() and self._header_h > 0)
        outer = self.layout()
        if not active:
            if self._floating:
                self._floating = False
                outer.insertWidget(0, self._header)
                self._header.show()
            elif self._header.height() > 0:
                self._header_h = self._header.height()
        else:
            if not self._floating:
                self._floating = True
                outer.removeWidget(self._header)
            self._header.setGeometry(0, -details.banner_offset(), self.width(), self._header_h)
            self._header.raise_()
        if self._header_h and self._top_state != self._header_h:
            self._top_state = self._header_h
            for t in self._tabs.values():
                t._details.set_top_inset(self._header_h + 12)
        self.update()

    def paintEvent(self, event) -> None:
        super().paintEvent(event)
        details = getattr(self._stack.currentWidget(), '_details', None)
        if details is None or not details.isVisible():
            return
        pix = details.banner_for(self.width())
        if pix is not None:
            QPainter(self).drawPixmap(0, -details.banner_offset(), pix)

    def _open_released(self, hit: dict) -> None:
        entry = _released_entry(hit)
        switch = getattr(self.window(), 'switchTo', None)
        if callable(switch):
            try:
                switch(self)
            except Exception:
                pass
        self.open_detail(entry, 'fitgirl')

    def _apply_locale(self, *_args) -> None:
        self._search_bar.setPlaceholderText(tr('repacks.search_placeholder'))
        self._upcoming_btn.setText(tr('repacks.upcoming_repacks'))
        self._refresh_btn.setToolTip(tr('repacks.refresh_tooltip'))
        self._bookmarks_btn.setToolTip(tr('repacks.bookmarks_tooltip_count', count=bookmark_store().count()))

    def _on_global_theme_changed(self, *_args) -> None:
        if getattr(self, '_repolish_pending', False):
            return
        self._repolish_pending = True
        QTimer.singleShot(0, self._repolish_subtree)

    def _repolish_subtree(self) -> None:
        self._repolish_pending = False
        for child in self.findChildren(CardWidget):
            try:
                child.style().unpolish(child)
                child.style().polish(child)
            except Exception:
                pass
        self.update()

    def _open_bookmark_details(self, entry) -> None:
        self.open_detail(entry, getattr(entry, 'source', '') or 'fitgirl')

    def _download_bookmark(self, entry) -> None:
        key = getattr(entry, 'source', '') or 'fitgirl'
        from src.core.config import is_tab_enabled
        tab = self._tabs.get(key)
        if tab is None or not is_tab_enabled('repacks', key):
            return
        self._pivot.setCurrentItem(key)
        tab.open_details_and_download(entry)

    def _on_bookmarks_clicked(self) -> None:
        self._view_btn.setEnabled(not self._bookmarks_btn.isChecked())
        if self._bookmarks_btn.isChecked():
            self._prev_tab_key = self._pivot.currentRouteKey()
            if self._bookmark_page is None:
                self._bookmark_page = BookmarksPage(self)
                self._bookmark_page.open_requested.connect(self._open_bookmark_details)
                self._bookmark_page.download_requested.connect(self._download_bookmark)
                self._bookmark_page.back_requested.connect(self._on_bookmarks_back_clicked)
                self._stack.addWidget(self._bookmark_page)
            self._bookmark_page.rebuild()
            self._stack.setCurrentWidget(self._bookmark_page)
        else:
            key = getattr(self, '_prev_tab_key', None)
            if key not in self._tabs:
                key = next(iter(self._tabs), None)
            if key:
                self._pivot.setCurrentItem(key)
                self._stack.setCurrentWidget(self._tabs[key])

    def _on_bookmarks_back_clicked(self) -> None:
        self._bookmarks_btn.setChecked(False)
        self._view_btn.setEnabled(True)
        key = getattr(self, '_prev_tab_key', None)
        if key not in self._tabs:
            key = next(iter(self._tabs), None)
        if key:
            self._pivot.setCurrentItem(key)
            self._stack.setCurrentWidget(self._tabs[key])

    def _refresh_badge_style(self) -> None:
        badge = getattr(self, '_bookmarks_badge', None)
        if badge is not None:
            with suppress(RuntimeError):
                badge.setStyleSheet(count_badge_qss())

    def _sync_bookmark_badge(self) -> None:
        n = bookmark_store().count()
        self._bookmarks_btn.setToolTip(tr('repacks.bookmarks_tooltip_count', count=n))
        badge = getattr(self, '_bookmarks_badge', None)
        if badge is None:
            return
        if n > 0:
            text = '99+' if n > 99 else str(n)
            badge.setText(text)
            w = 16 if len(text) < 2 else 22
            badge.resize(w, 15)
            badge.move(32 - w, 0)
            badge.show()
        else:
            badge.hide()

    def _on_app_about_to_quit(self) -> None:
        try:
            release_watcher().stop()
        except Exception:
            logger.exception('Failed to stop release watcher')
        if self._tab_fade_anim is not None:
            self._tab_fade_anim.stop()
            self._tab_fade_anim = None
        task_id = getattr(self, '_upcoming_task', None)
        if task_id is not None:
            cancel_task(task_id)
        for tab in self._tabs.values():
            try:
                tab.shutdown()
            except Exception:
                logger.exception('Failed to shut down source tab %s', getattr(tab, '_source_key', '?'))
        from src.core.repacks.base import clear_all_cache
        try:
            clear_all_cache()
        except Exception:
            logger.exception('Failed to clear repacks cache on shutdown')

    def _on_tab_changed(self, key: str) -> None:
        if self._bookmarks_btn.isChecked():
            self._bookmarks_btn.blockSignals(True)
            self._bookmarks_btn.setChecked(False)
            self._bookmarks_btn.blockSignals(False)
        self._switch_tab_animated(key)
        self._tabs[key].load_initial()
        self._tabs[key].filter_grid(self._search_bar.text())
        self._update_donate_button(key)
        self._update_upcoming_button(key)
        self._update_discord_button(key)

    def open_detail(self, entry: 'RepackEntry', source_key: str='fitgirl') -> None:
        from src.core.config import is_tab_enabled
        tab = self._tabs.get(source_key)
        if tab is None or not is_tab_enabled('repacks', source_key):
            return
        self._pivot.setCurrentItem(source_key)
        self._stack.setCurrentWidget(tab)
        self._current_tab_key = source_key
        self._update_donate_button(source_key)
        self._update_upcoming_button(source_key)
        self._update_discord_button(source_key)
        if hasattr(tab, '_show_details'):
            tab._show_details(entry)

    def _switch_tab_animated(self, key: str) -> None:
        target = self._tabs.get(key)
        if target is None:
            return
        if self._current_tab_key == key and self._stack.currentWidget() is target:
            return
        self._current_tab_key = key
        prev_widget = self._stack.currentWidget()
        if prev_widget is not None and prev_widget is not target:
            prev_widget.setGraphicsEffect(None)
        self._stack.setCurrentWidget(target)
        _fade_in_widget(target, SourceTab._FADE_MS, self, "_tab_fade_anim")

    def _update_donate_button(self, key: str) -> None:
        url = _SOURCE_DONATION_URLS.get(key)
        if url:
            display_name = self._tabs[key]._source_name if key in self._tabs else key
            self._donate_btn.setText(tr('repacks.donate_to', name=display_name))
            self._donate_btn.setVisible(True)
        else:
            self._donate_btn.setVisible(False)

    def _update_upcoming_button(self, key: str) -> None:
        self._upcoming_btn.setVisible(key in _SOURCE_UPCOMING_SUPPORTED)

    def _update_discord_button(self, key: str) -> None:
        self._discord_btn.setVisible(key == 'gog')

    def _on_discord_clicked(self) -> None:
        QDesktopServices.openUrl(QUrl('https://discord.gg/qXtqrkVXaT'))

    def _on_donate_clicked(self) -> None:
        current_key = self._pivot.currentRouteKey()
        url = _SOURCE_DONATION_URLS.get(current_key)
        if url:
            QDesktopServices.openUrl(QUrl(url))

    def _on_upcoming_clicked(self) -> None:
        current_key = self._pivot.currentRouteKey()
        if current_key not in _SOURCE_UPCOMING_SUPPORTED:
            return
        if getattr(self, '_upcoming_task', None) is not None:
            return
        top_level = self.window()
        dialog = UpcomingRepacksDialog(top_level if top_level is not None else self)
        dialog.show_loading()

        def _safe_done(details):
            self._upcoming_task = None
            if dialog.isVisible():
                dialog.show_details(details)

        def _safe_error(message):
            self._upcoming_task = None
            if dialog.isVisible():
                dialog.show_error(message)
        self._upcoming_task = fetch_upcoming_repacks_async(current_key, on_done=_safe_done, on_error=_safe_error, use_cache=True)

        def _cleanup_task():
            task_id = getattr(self, '_upcoming_task', None)
            if task_id is not None:
                cancel_task(task_id)
                self._upcoming_task = None
        dialog.finished.connect(_cleanup_task)
        dialog.setModal(True)
        dialog.show()
        dialog.raise_()
        dialog.activateWindow()

    def _on_refresh_clicked(self) -> None:
        current_key = self._pivot.currentRouteKey()
        if not current_key or current_key not in self._tabs:
            return
        from src.core.repacks.base import clear_source_cache
        try:
            clear_source_cache(current_key)
        except Exception:
            logger.exception('Failed to clear cache for %s during refresh', current_key)
        self._tabs[current_key].refresh()

    def refresh_visible(self) -> None:
        try:
            current_key = self._pivot.currentRouteKey()
        except Exception:
            return
        if not current_key:
            return
        tab = self._tabs.get(current_key)
        if tab is None:
            return
        try:
            if tab._stack.currentWidget() is not tab._grid_row:
                return
            if tab._search_query or tab._is_loading:
                return
            if not tab._loaded_once:
                tab.load_initial()
                return


            if time.monotonic() - tab._last_fetch_mono >= _NAV_REFRESH_INTERVAL_S:
                tab._last_fetch_mono = 0.0
                tab.refresh()
        except Exception:
            logger.exception('Failed to auto-refresh repacks tab %r', current_key)
            return
        if current_key in _SOURCE_UPCOMING_SUPPORTED:
            try:
                from src.core.worker import fetch_upcoming_repacks_async
                fetch_upcoming_repacks_async(current_key, use_cache=True, owner=self)
            except Exception:
                logger.exception('Failed to prefetch upcoming repacks for %r', current_key)

    def _on_search_text_changed(self, text: str) -> None:
        current_key = self._pivot.currentRouteKey()
        if current_key in self._tabs:
            self._tabs[current_key].filter_grid(text)

    def _on_search_immediate(self, text: str) -> None:
        self._search_debounce.stop()
        self._on_search_text_changed(text)

    def _add_source_tab(self, key: str, display_name: str) -> None:
        tab = SourceTab(key, display_name, self._manager, self)
        tab._details.banner_changed.connect(self._sync_top)
        self._tabs[key] = tab
        self._view_btn.bind(tab._grid)
        self._tab_display_names[key] = display_name
        self._stack.addWidget(tab)

    def _rebuild_tab_selector(self) -> None:
        from src.core.config import enabled_tab_keys
        enabled = [k for k in enabled_tab_keys('repacks') if k in self._tabs]
        if not enabled:
            enabled = list(self._tabs.keys())
        current = self._current_tab_key if self._current_tab_key in enabled else enabled[0]
        if getattr(self, '_tab_handler_connected', False):
            try:
                self._pivot.currentItemChanged.disconnect(self._on_tab_changed)
            except (RuntimeError, TypeError):
                pass
        self._pivot.clear()
        for key in enabled:
            self._pivot.addItem(routeKey=key, text=self._tab_display_names.get(key, key), icon=_source_tab_icon(key))
            item = self._pivot.items.get(key)
            if item is not None:
                item.setIconSize(QSize(22, 22))
        apply_pivot_theme(self._pivot)
        self._pivot.currentItemChanged.connect(self._on_tab_changed)
        self._tab_handler_connected = True
        self._pivot.setVisible(len(enabled) > 1)
        self._sync_top()
        self._pivot.setCurrentItem(current)
        self._stack.setCurrentWidget(self._tabs[current])
        self._current_tab_key = current
        self._tabs[current].load_initial()
        self._update_donate_button(current)
        self._update_upcoming_button(current)
        self._update_discord_button(current)

    def _on_tab_visibility_changed(self, scope: str) -> None:
        if scope == 'repacks':
            self._rebuild_tab_selector()

    def _refresh_pivot_icons(self) -> None:
        for key, item in getattr(self._pivot, 'items', {}).items():
            icon = _source_tab_icon(key)
            if icon is not None:
                item.setIcon(icon)


def _qcolor_from_palette(value) -> QColor:
    if isinstance(value, QColor):
        return value
    try:
        c = QColor(str(value))
        return c if c.isValid() else QColor(128, 128, 128)
    except Exception:
        return QColor(128, 128, 128)

def _body_text_color() -> str:
    p = palette()
    v = p.get('secondary_text') or p.get('primary_text')
    return str(v) if v else '#dddddd' if isDarkTheme() else '#222222'

def _muted_text_color() -> str:
    p = palette()
    v = p.get('muted') or p.get('secondary_text') or p.get('primary_text')
    return str(v) if v else '#9a9a9a' if isDarkTheme() else '#666666'

def _strong_text_qcolor() -> QColor:
    return _qcolor_from_palette(palette().get('primary_text') or ('#ffffff' if isDarkTheme() else '#1a1a1a'))

def _alpha_rgba(rgb: str, alpha_percent: int) -> str:
    a = max(0, min(100, int(alpha_percent))) * 255 // 100
    return f'rgba({rgb},{a})'

def _surface_tint_color(alpha_percent: int=10) -> str:
    return _alpha_rgba('255,255,255' if isDarkTheme() else '0,0,0', alpha_percent)

def _hover_tint_color() -> str:
    return _alpha_rgba('255,255,255' if isDarkTheme() else '0,0,0', 8)

def _grid_colors() -> tuple[QColor, QColor, QColor]:
    base = 255 if isDarkTheme() else 0
    return _strong_text_qcolor(), _qcolor_from_palette(_muted_text_color()), QColor(base, base, base, 36)

def _inactive_dot_color() -> str:
    return _alpha_rgba('255,255,255' if isDarkTheme() else '0,0,0', 26)

def _mica_menu(parent=None):
    try:
        return RoundMenu(parent=parent)
    except Exception:
        return QMenu(parent)

def _auto_install_enabled() -> bool:
    try:
        from src.core.config import settings as _s
        return bool(getattr(_s, 'auto_install_games', False))
    except Exception:
        return False

def _muted() -> str:
    return palette()['muted']

def _sanitize_dirname(title: str) -> str:
    name = ''.join((c for c in title or 'Game' if c not in '\\/:*?"<>|')).strip().rstrip('.')
    return name[:80] or 'Game'

class GameInstallOptionsDialog(MessageBoxBase):
    def __init__(self, details, setup_path: str='', parent=None):
        super().__init__(parent)
        self._details = details
        if setup_path:
            self._inspected = inspect_inno_setup(setup_path)
            if not self._inspected.get('available'):
                logger.info('Install options: inspection unavailable for %s, using static options', setup_path)
        else:
            self._inspected = {'available': False}
        self.titleLabel = SubtitleLabel(tr('library.install_options_title'), self)
        self.viewLayout.addWidget(self.titleLabel)
        name = CaptionLabel(_entry_title(details), self)
        name.setStyleSheet(f'color: {_muted()};')
        self.viewLayout.addWidget(name)
        hint = BodyLabel(tr('library.install_options_hint'), self)
        hint.setWordWrap(True)
        self.viewLayout.addWidget(hint)
        row = QHBoxLayout()
        self._dir_edit = LineEdit(self)
        self._dir_edit.setText(default_install_dir())
        self._dir_edit.setMinimumWidth(320)
        browse = PushButton(themed_icon('repacks.browse_folder', FluentIcon.FOLDER), tr('common.browse'), self)
        browse.clicked.connect(self._browse)
        row.addWidget(self._dir_edit, 1)
        row.addWidget(browse)
        self.viewLayout.addLayout(row)
        scroll = QScrollArea(self)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        scroll.setStyleSheet('QScrollArea { background: transparent; border: none; }')
        scroll.setMaximumHeight(420)
        body = QWidget()
        body.setStyleSheet('background: transparent;')
        body_layout = QVBoxLayout(body)
        body_layout.setContentsMargins(0, 0, 0, 0)
        body_layout.setSpacing(6)
        scroll.setWidget(body)
        self.viewLayout.addWidget(scroll)

        def section_caption(text: str) -> None:
            c = CaptionLabel(text, body)
            c.setStyleSheet(f'color: {_muted()};')
            body_layout.addWidget(c)

        def chk_flow(items: list[tuple[str, bool]]) -> list[CheckBox]:
            flow = QGridLayout()
            flow.setHorizontalSpacing(16)
            flow.setVerticalSpacing(2)
            boxes = []
            for i, (text, checked) in enumerate(items):
                c = CheckBox(text, body)
                c.setChecked(checked)
                flow.addWidget(c, i // 2, i % 2)
                boxes.append(c)
            wrapper = QWidget(body)
            wrapper.setLayout(flow)
            body_layout.addWidget(wrapper)
            return boxes

        def chk(text: str, checked: bool) -> CheckBox:
            c = CheckBox(text, body)
            c.setChecked(checked)
            body_layout.addWidget(c)
            return c
        auto_default = _auto_install_enabled()
        self._chk_auto = chk(tr('library.opt_auto'), auto_default)
        body_layout.addSpacing(6)
        self._chk_scan_before = chk(tr('library.opt_scan_before'), True)
        self._chk_scan_after = chk(tr('library.opt_scan_after'), False)
        script_verify = self._inspected.get('verify') or {}
        if bool(script_verify):
            self._chk_verify_installer = chk(tr('library.opt_verify_files'), False)
        else:
            self._chk_verify_installer = None
        body_layout.addSpacing(6)
        self._chk_ram = chk(tr('library.opt_ram'), False)
        self._chk_redist = chk(tr('library.opt_directx_vc'), True)
        self._chk_sm = chk(tr('library.opt_startmenu'), True)
        self._local_checks: list[tuple[str, CheckBox]] = []
        local_info = self._inspected.get('local')
        if local_info and local_info.get('items'):
            body_layout.addSpacing(6)
            section_caption(tr('library.opt_local_hint'))
            for g in local_info.get('groups') or []:
                body_layout.addWidget(CaptionLabel(g['label'], body))
                group_items = [it for it in local_info['items'] if it.get('group') == g['name']]
                boxes = chk_flow([(it.get('description') or it['name'], bool(it.get('default_checked', False))) for it in group_items])
                for it, box in zip(group_items, boxes, strict=True):
                    self._local_checks.append((it['name'], box))
            group_items = [it for it in local_info['items'] if not it.get('group')]
            if group_items:
                boxes = chk_flow([(it.get('description') or it['name'], bool(it.get('default_checked', False))) for it in group_items])
                for it, box in zip(group_items, boxes, strict=True):
                    self._local_checks.append((it['name'], box))
        self._other_checks: list[tuple[str, CheckBox]] = []
        other_items = [it for it in self._inspected.get('other') or [] if not it.get('required')]
        if other_items:
            body_layout.addSpacing(6)
            section_caption(tr('library.opt_bonus_hint'))
            boxes = chk_flow([(it.get('description') or it['name'], bool(it.get('default_checked', True))) for it in other_items])
            for it, box in zip(other_items, boxes, strict=True):
                self._other_checks.append((it['name'], box))
        note = CaptionLabel(tr('library.opt_note'), self)
        note.setWordWrap(True)
        note.setStyleSheet(f'color: {_muted()};')
        self.viewLayout.addWidget(note)
        self.yesButton.setText(tr('library.continue_download'))
        self.cancelButton.setText(tr('download.cancel'))
        self.widget.setMinimumWidth(760)

    def _browse(self):
        d = QFileDialog.getExistingDirectory(self, tr('library.choose_install_dir'), self._dir_edit.text())
        if d:
            self._dir_edit.setText(d)

    def result_spec(self) -> dict:
        title = self._details.title or 'Game'
        root = self._dir_edit.text().strip() or default_install_dir()
        remember_install_dir(root)
        extra = getattr(self._details, 'extra', {}) or {}
        verify_checked = self._chk_verify_installer.isChecked() if self._chk_verify_installer is not None else False
        local_selected = [name for name, c in self._local_checks if c.isChecked()]
        other_selected = [name for name, c in self._other_checks if c.isChecked()]
        inno = build_inno_selection(self._inspected, redist=self._chk_redist.isChecked(), verify=verify_checked, local_children=local_selected, other_selected=other_selected)
        if self._inspected.get('types'):
            inno['types'] = self._inspected['types'][0]
        return {'title': title, 'enabled': self._chk_auto.isChecked(), 'scan_before': self._chk_scan_before.isChecked(), 'scan_after': self._chk_scan_after.isChecked(), 'install_dir': os.path.join(root, _sanitize_dirname(title)), 'options': {'ram2gb': self._chk_ram.isChecked(), 'directx': self._chk_redist.isChecked(), 'vcredist': self._chk_redist.isChecked(), 'startmenu': self._chk_sm.isChecked(), 'verify_installer': verify_checked, 'inno': inno}, 'size_bytes': parse_size_bytes(getattr(self._details, 'size_info', '') or '') or parse_size_bytes(str(extra.get('repack_size', ''))), 'cover_url': getattr(self._details, 'cover_url', '') or '', 'description': (getattr(self._details, 'description', '') or '')[:2500], 'screenshots': list(getattr(self._details, 'screenshot_urls', []) or [])[:4], 'metadata': {k: extra.get(k) for k in ('genres', 'company', 'languages', 'original_size', 'repack_size') if extra.get(k)}}
