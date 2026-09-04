from __future__ import annotations
import bisect
import gc
import logging
import re
from PySide6.QtCore import Qt, QSize, QSizeF, QRect, QThread, QObject, Signal, Property, QPropertyAnimation, QEasingCurve, QTimer, QVariantAnimation
from shiboken6 import isValid as _qt_is_valid
from PySide6.QtGui import QColor
from PySide6.QtWidgets import QWidget, QVBoxLayout, QHBoxLayout, QLayout, QLayoutItem, QScrollArea, QDialog, QTreeWidgetItem, QHeaderView, QGridLayout, QLabel, QSizePolicy
from qfluentwidgets import CardWidget, FluentIcon, Pivot, TitleLabel, SubtitleLabel, BodyLabel, StrongBodyLabel, CaptionLabel, TransparentToolButton, PushButton, PrimaryPushButton, ImageLabel, PillPushButton, FlowLayout as QFlowLayout, themeColor, SearchLineEdit, TreeWidget, MessageBoxBase, SmoothScrollArea, HyperlinkButton, qconfig, isDarkTheme
from src.core.repacks.base import RepackEntry, RepackDetails, magnet_display_name
from src.core.worker import fetch_page_async, fetch_details_async, fetch_upcoming_repacks_async, fetch_search_async, fetch_latest_repacks_async, fetch_popular_repacks_async, cancel as cancel_task
from src.core.repacks.video_downloader import PosterDownloader
from src.core.downloader import DownloadManager, DLState
from src.core.theme import palette
from src.core.translations import tr, register_locale_refresh
_SOURCE_DONATION_URLS = {'fitgirl': 'https://fitgirl-repacks.site/donations/'}
_SOURCE_UPCOMING_SUPPORTED = {'fitgirl'}
logger = logging.getLogger(__name__)

def _qcolor_from_palette(value: str) -> QColor:
    value = value.strip()
    if value.startswith('rgba('):
        parts = [p.strip() for p in value[5:-1].split(',')]
        r, g, b = (int(parts[0]), int(parts[1]), int(parts[2]))
        a = parts[3]
        alpha = round(float(a) * 255) if '.' in a or float(a) <= 1 else int(a)
        return QColor(r, g, b, max(0, min(255, alpha)))
    return QColor(value)

def _muted_text_color() -> str:
    return palette()['muted']

def _faint_text_color() -> str:
    return palette()['faint_text']

def _body_text_color() -> str:
    return palette()['body_text']

def _surface_tint_color(alpha: int=12) -> str:
    return palette()['surface_tint_strong'] if alpha >= 14 else palette()['surface_tint']

def _surface_border_color(alpha: int=18) -> str:
    return palette()['surface_border']

def _hover_tint_color() -> str:
    return palette()['hover_tint']

def _strong_text_qcolor() -> QColor:
    return _qcolor_from_palette(palette()['primary_text'])

def _inactive_dot_color() -> str:
    return palette()['inactive_dot']

class FlowLayout(QLayout):

    def __init__(self, parent=None, margin: int=0, spacing: int=12):
        super().__init__(parent)
        self._items: list[QLayoutItem] = []
        self.setContentsMargins(margin, margin, margin, margin)
        self.setSpacing(spacing)

    def addItem(self, item: QLayoutItem) -> None:
        self._items.append(item)

    def insertWidget(self, index: int, widget: QWidget) -> None:
        from PySide6.QtWidgets import QWidgetItem
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
            items_w = sum(it.sizeHint().width() for it in row)
            if n > 1 and not is_last:
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
            items_w_if_added = sum(it.sizeHint().width() for it in row) + item_width
            gaps_if_added = len(row) * base_spacing
            if row and items_w_if_added + gaps_if_added > available_w:
                _flush_row(is_last=False)
                row = [item]
            else:
                row.append(item)
        if row:
            _flush_row(is_last=True)
        total_h = (y - base_spacing - effective_rect.y()) if line_height else 0
        return total_h + top + bottom

def _load_scaled_pixmap(path: str, target_w: int, target_h: int):
    from PySide6.QtGui import QImageReader, QImage, QPixmap, QPixmapCache
    if _load_scaled_pixmap._cache_sized is False:
        QPixmapCache.setCacheLimit(20 * 1024)
        _load_scaled_pixmap._cache_sized = True
    cache_key = f'poster:{path}:{target_w}x{target_h}:final'
    cached = QPixmapCache.find(cache_key)
    if cached is not None and (not cached.isNull()):
        return cached
    reader = QImageReader(path)
    reader.setAutoTransform(True)
    orig_size = reader.size()
    if orig_size.isValid() and orig_size.width() > 0 and (orig_size.height() > 0):
        scale = max(target_w / orig_size.width(), target_h / orig_size.height(), 1e-06)
        scaled_w = max(1, round(orig_size.width() * scale))
        scaled_h = max(1, round(orig_size.height() * scale))
        reader.setScaledSize(QSize(scaled_w, scaled_h))
    image = reader.read()
    if image.isNull():
        pix = QPixmap(path)
        pix = pix.scaled(target_w, target_h, Qt.AspectRatioMode.KeepAspectRatioByExpanding, Qt.TransformationMode.SmoothTransformation)
    else:
        if not image.hasAlphaChannel():
            image = image.convertToFormat(QImage.Format.Format_RGB16)
        pix = QPixmap.fromImage(image)
    pix = _center_crop_pixmap(pix, target_w, target_h)
    QPixmapCache.insert(cache_key, pix)
    return pix
_load_scaled_pixmap._cache_sized = False

def _center_crop_pixmap(pix, target_w: int, target_h: int):
    if pix.isNull():
        return pix
    if pix.width() == target_w and pix.height() == target_h:
        return pix
    x = max(0, (pix.width() - target_w) // 2)
    y = max(0, (pix.height() - target_h) // 2)
    return pix.copy(x, y, min(target_w, pix.width()), min(target_h, pix.height()))

_dominant_color_cache: dict[str, object] = {}
_DOMINANT_COLOR_CACHE_LIMIT = 200

def _dominant_color(path: str):
    cached = _dominant_color_cache.get(path)
    if cached is not None:
        return cached if cached is not False else None

    from PySide6.QtGui import QImageReader, QImage, QColor
    reader = QImageReader(path)
    reader.setScaledSize(QSize(48, 48))
    small = reader.read()
    if small.isNull():
        _dominant_color_cache[path] = False
        return None
    small = small.convertToFormat(QImage.Format.Format_RGB32)
    buckets: dict[tuple[int, int, int], list[int]] = {}
    for y in range(small.height()):
        for x in range(small.width()):
            color = QColor(small.pixel(x, y))
            r, g, b = (color.red(), color.green(), color.blue())
            mx, mn = (max(r, g, b), min(r, g, b))
            if mx > 235 and mn > 220:
                continue
            if mx < 25:
                continue
            key = (r // 24, g // 24, b // 24)
            entry = buckets.get(key)
            if entry is None:
                buckets[key] = [r, g, b, 1]
            else:
                entry[0] += r
                entry[1] += g
                entry[2] += b
                entry[3] += 1
    if not buckets:
        _dominant_color_cache[path] = False
        return None
    r_sum, g_sum, b_sum, n = max(buckets.values(), key=lambda e: e[3])
    result = QColor(r_sum // n, g_sum // n, b_sum // n)
    if len(_dominant_color_cache) >= _DOMINANT_COLOR_CACHE_LIMIT:
        _dominant_color_cache.clear()
    _dominant_color_cache[path] = result
    return result

def _stop_previous_movie(image_label) -> None:
    try:
        movie = image_label.movie()
    except Exception:
        movie = None
    if movie is not None:
        movie.stop()
        movie.deleteLater()
        from PySide6.QtWidgets import QLabel
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
    _HOVER_MS = 150
    _PRESS_MS = 90

    def _init_poster_animations(self) -> None:
        self._anim_base_pos = self._poster_lbl.pos()
        self._pos_anim = _SteppedAnimator(self._apply_lift_offset)

    def _apply_lift_offset(self, offset_y: float) -> None:
        # The animation delivers queued valueChanged callbacks asynchronously,
        # so the card/label may already have been destroyed by Qt by the time
        # this runs (e.g. card removed from the list while hover/press
        # animation was still in flight). Bail out quietly instead of
        # crashing with "Internal C++ object already deleted".
        poster_lbl = getattr(self, '_poster_lbl', None)
        if poster_lbl is None or not _qt_is_valid(self) or not _qt_is_valid(poster_lbl):
            self._stop_poster_animation()
            return
        base = self._anim_base_pos
        poster_lbl.move(base.x(), round(base.y() - offset_y))

    def _stop_poster_animation(self) -> None:
        pos_anim = getattr(self, '_pos_anim', None)
        if pos_anim is not None and _qt_is_valid(pos_anim):
            pos_anim.stop()

    def _animate_to(self, offset_y: int, duration: int) -> None:
        if not _qt_is_valid(self) or not _qt_is_valid(self._poster_lbl):
            return
        current_offset = self._anim_base_pos.y() - self._poster_lbl.pos().y()
        self._pos_anim.stop()
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
        # Cards are frequently removed/hidden from scroll lists while a
        # hover/press lift animation is still running. Stop it here so no
        # further valueChanged callbacks arrive after the widget is torn down.
        self._stop_poster_animation()
        super().hideEvent(event)

class PosterCard(_AnimatedPosterMixin, QWidget):
    clicked_poster = Signal(object)
    POSTER_WIDTH = 150
    POSTER_HEIGHT = 210
    _HOVER_HEADROOM_PX = 8

    def __init__(self, entry: RepackEntry, parent=None):
        super().__init__(parent)
        self._entry = entry
        self.setFixedSize(self.POSTER_WIDTH, self.POSTER_HEIGHT + self._HOVER_HEADROOM_PX)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setToolTip(entry.title)
        self.setStyleSheet('background: transparent; border: none;')
        self._poster_lbl = ImageLabel(self)
        self._poster_lbl.setBorderRadius(6, 6, 6, 6)
        self._poster_lbl.setGeometry(0, self._HOVER_HEADROOM_PX, self.POSTER_WIDTH, self.POSTER_HEIGHT)
        self._fallback_icon = None
        self._fallback_label = None
        if entry.poster_path:
            self.set_poster_path(entry.poster_path)
        elif not entry.poster_url:
            self.show_fallback_icon()
        self._init_poster_animations()

    def enterEvent(self, event):
        self.animate_hover_enter()
        super().enterEvent(event)

    def leaveEvent(self, event):
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
        icon_lbl.setPixmap(FluentIcon.GAME.icon(color=QColor(_body_text_color())).pixmap(36, 36))
        icon_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        icon_lbl.setStyleSheet('background: transparent;')
        wrapper_layout.addWidget(icon_lbl)
        title_lbl = QLabel(self._entry.title, wrapper)
        title_lbl.setWordWrap(True)
        title_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        title_lbl.setStyleSheet(f'background: transparent; font-size: 11px; color: {palette()["primary_text"]};')
        title_lbl.setMaximumHeight(60)
        wrapper_layout.addWidget(title_lbl)
        wrapper_layout.addStretch(1)
        wrapper.show()
        self._fallback_icon = wrapper
        self._fallback_label = title_lbl

    @property
    def entry(self) -> RepackEntry:
        return self._entry

    @property
    def poster_url(self) -> str | None:
        return self._entry.poster_url

    def set_poster_path(self, path: str) -> None:
        try:
            if self._fallback_icon is not None:
                self._fallback_icon.deleteLater()
                self._fallback_icon = None
                self._fallback_label = None
                self._poster_lbl.setStyleSheet('')
            _stop_previous_movie(self._poster_lbl)
            pix = _load_scaled_pixmap(path, self.POSTER_WIDTH, self.POSTER_HEIGHT)
            self._poster_lbl.setImage(pix)
            self._poster_lbl.setFixedSize(self.POSTER_WIDTH, self.POSTER_HEIGHT)
            self._poster_lbl.scaledToWidth(self.POSTER_WIDTH)
            self._entry.poster_path = path
        except Exception:
            logger.warning('Failed to load poster image: %s', path)

    def unload_pixmap(self) -> None:
        if self._entry.poster_path:
            _stop_previous_movie(self._poster_lbl)
            self._poster_lbl.setImage(None)
            self._poster_lbl.setFixedSize(self.POSTER_WIDTH, self.POSTER_HEIGHT)

    def reload_pixmap_if_needed(self) -> None:
        if self._entry.poster_path and self._poster_lbl.isNull():
            self.set_poster_path(self._entry.poster_path)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.animate_press()
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            still_hovering = self.rect().contains(event.position().toPoint())
            self.animate_release(still_hovering)
            if still_hovering:
                self.clicked_poster.emit(self._entry)
        super().mouseReleaseEvent(event)

class PosterGrid(SmoothScrollArea):
    poster_clicked = Signal(object)
    near_bottom = Signal()
    _NEAR_BOTTOM_THRESHOLD_PX = 400
    _VISIBILITY_MARGIN_PX = 600
    _VISIBILITY_DEBOUNCE_MS = 150

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWidgetResizable(True)
        self.setFrameShape(QScrollArea.Shape.NoFrame)
        self.setStyleSheet('background: transparent; border: none;')
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        try:
            from qfluentwidgets import SmoothMode
            self.setSmoothMode(SmoothMode.LINEAR)
        except Exception:
            pass
        self.setViewportMargins(0, 0, 0, 0)
        self.verticalScrollBar().setFixedWidth(6)
        self._container = QWidget()
        self._container.setStyleSheet('background: transparent;')
        container_layout = QVBoxLayout(self._container)
        container_layout.setContentsMargins(4, 4, 0, 4)
        container_layout.setSpacing(20)
        self._latest_section = QWidget(self._container)
        latest_layout = QVBoxLayout(self._latest_section)
        latest_layout.setContentsMargins(0, 0, 0, 0)
        latest_layout.setSpacing(10)
        latest_header = StrongBodyLabel(tr('repacks.latest_repacks'))
        latest_header.setStyleSheet(f'font-size: 16px; font-weight: 600; color: {palette()['primary_text']};')
        self._latest_header_lbl = latest_header
        latest_layout.addWidget(latest_header)
        latest_row = QHBoxLayout()
        latest_row.setSpacing(12)
        self.latest_prev_btn = TransparentToolButton(FluentIcon.LEFT_ARROW, self._latest_section)
        self.latest_prev_btn.setFixedSize(32, 32)
        self.latest_prev_btn.clicked.connect(self._go_latest_prev)
        latest_row.addWidget(self.latest_prev_btn, 0, Qt.AlignmentFlag.AlignVCenter)
        self._latest_cards_row = QHBoxLayout()
        self._latest_cards_row.setSpacing(14)
        latest_row.addLayout(self._latest_cards_row, 1)
        self.latest_next_btn = TransparentToolButton(FluentIcon.RIGHT_ARROW, self._latest_section)
        self.latest_next_btn.setFixedSize(32, 32)
        self.latest_next_btn.clicked.connect(self._go_latest_next)
        latest_row.addWidget(self.latest_next_btn, 0, Qt.AlignmentFlag.AlignVCenter)
        latest_layout.addLayout(latest_row)
        latest_dots_row = QHBoxLayout()
        latest_dots_row.setSpacing(6)
        latest_dots_row.addStretch(1)
        self._latest_dots_row = latest_dots_row
        self._latest_dot_widgets: list[QWidget] = []
        latest_dots_row.addStretch(1)
        latest_layout.addLayout(latest_dots_row)
        self._latest_section.setVisible(False)
        container_layout.addWidget(self._latest_section)
        self._flow_container = QWidget(self._container)
        self._flow_container.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        self._flow_layout = FlowLayout(self._flow_container, margin=0, spacing=14)
        container_layout.addWidget(self._flow_container)
        self.setWidget(self._container)
        self._entries: list[RepackEntry] = []
        self._entries_by_url: dict[str, RepackEntry] = {}
        self._entry_index: dict[str, int] = {}
        self._cards_by_url: dict[str, PosterCard] = {}
        self._cards_by_poster_url: dict[str, PosterCard] = {}
        self._live_positions: list[int] = []
        self._card_order: list[str] = []
        self._MAX_LIVE_CARDS = 260
        self._EVICT_MARGIN_PX = 4000
        self._MATERIALIZE_BUFFER_ROWS = 4
        self._latest_entries: list = []
        self._latest_page = 0
        self._latest_cards: list[PosterCard] = []
        self._poster_downloader = PosterDownloader(self)
        self._poster_downloader.poster_ready.connect(self._on_poster_ready)
        self._poster_downloader.poster_failed.connect(self._on_poster_failed)
        from PySide6.QtCore import QTimer
        self._visibility_timer = QTimer(self)
        self._visibility_timer.setSingleShot(True)
        self._visibility_timer.setInterval(self._VISIBILITY_DEBOUNCE_MS)
        self._visibility_timer.timeout.connect(self._update_offscreen_cards)
        self.verticalScrollBar().valueChanged.connect(self._on_scroll_value_changed)
        self._card_states: dict[str, bool] = {}
        self._card_geoms: dict[str, tuple[int, int]] = {}
        self._last_scroll_value = None
        self._filter_query = ''
        self._latest_visible_count = self.LATEST_PAGE_SIZE
        register_locale_refresh(self, self._apply_locale)
    LATEST_PAGE_SIZE = 5
    LATEST_MIN_VISIBLE = 2
    _LATEST_ROW_FIXED_OVERHEAD = 96

    def _update_latest_visible_count(self) -> None:
        row_width = self.viewport().width() - self._LATEST_ROW_FIXED_OVERHEAD
        card_w = PosterCard.POSTER_WIDTH + self._latest_cards_row.spacing()
        if card_w <= 0:
            return
        count = max(self.LATEST_MIN_VISIBLE, min(self.LATEST_PAGE_SIZE, row_width // card_w))
        if count != self._latest_visible_count:
            self._latest_visible_count = count
            if self._latest_entries:
                self._latest_page = 0
                self._rebuild_latest_dots()
                self._render_latest_page()

    def _apply_locale(self, *_args) -> None:
        self._latest_header_lbl.setText(tr('repacks.latest_repacks'))

    def set_latest_entries(self, entries: list) -> None:
        self._latest_entries = entries or []
        self._latest_page = 0
        if not self._latest_entries:
            self._latest_section.setVisible(False)
            return
        self._latest_section.setVisible(True)
        from PySide6.QtCore import QTimer
        QTimer.singleShot(0, self._update_latest_visible_count)
        self._rebuild_latest_dots()
        self._render_latest_page()

    def set_latest_section_visible(self, visible: bool) -> None:
        self._latest_section.setVisible(visible and bool(self._latest_entries))

    def _latest_page_count(self) -> int:
        if not self._latest_entries:
            return 0
        return (len(self._latest_entries) + self._latest_visible_count - 1) // self._latest_visible_count

    def _rebuild_latest_dots(self) -> None:
        for dot in self._latest_dot_widgets:
            self._latest_dots_row.removeWidget(dot)
            dot.deleteLater()
        self._latest_dot_widgets.clear()
        for _ in range(self._latest_page_count()):
            dot = QWidget(self._latest_section)
            dot.setFixedSize(7, 7)
            self._latest_dots_row.insertWidget(self._latest_dots_row.count() - 1, dot)
            self._latest_dot_widgets.append(dot)
        self._update_latest_dots()

    def _update_latest_dots(self) -> None:
        for i, dot in enumerate(self._latest_dot_widgets):
            color = themeColor().name() if i == self._latest_page else _inactive_dot_color()
            dot.setStyleSheet(f'background-color: {color}; border-radius: 3px;')

    def _render_latest_page(self) -> None:
        for card in self._latest_cards:
            if isinstance(card, PosterCard):
                card.unload_pixmap()
            self._latest_cards_row.removeWidget(card)
            card.setVisible(False)
            card.setParent(None)
            card.deleteLater()
        self._latest_cards.clear()
        start = self._latest_page * self._latest_visible_count
        page_entries = self._latest_entries[start:start + self._latest_visible_count]
        for entry in page_entries:
            card = PosterCard(entry, self._container)
            card.clicked_poster.connect(self.poster_clicked.emit)
            self._latest_cards_row.addWidget(card)
            self._latest_cards.append(card)
            if entry.poster_url:
                self._poster_downloader.request(entry.poster_url)
        for _ in range(self._latest_visible_count - len(page_entries)):
            placeholder = QWidget(self._container)
            placeholder.setFixedSize(PosterCard.POSTER_WIDTH, PosterCard.POSTER_HEIGHT + PosterCard._HOVER_HEADROOM_PX)
            self._latest_cards_row.addWidget(placeholder)
            self._latest_cards.append(placeholder)
        self.latest_prev_btn.setEnabled(self._latest_page > 0)
        self.latest_next_btn.setEnabled(self._latest_page < self._latest_page_count() - 1)
        self._update_latest_dots()

    def _go_latest_prev(self) -> None:
        if self._latest_page > 0:
            self._latest_page -= 1
            self._render_latest_page()

    def _go_latest_next(self) -> None:
        if self._latest_page < self._latest_page_count() - 1:
            self._latest_page += 1
            self._render_latest_page()

    def clear(self) -> None:
        self._entries = []
        self._entries_by_url = {}
        self._entry_index = {}
        self._cards_by_url = {}
        self._cards_by_poster_url = {}
        self._live_positions = []
        self._card_order = []
        while self._flow_layout.count():
            item = self._flow_layout.takeAt(0)
            if item is not None and item.widget() is not None:
                widget = item.widget()
                widget.hide()
                widget.setParent(None)
                widget.deleteLater()
        self._flow_layout.invalidate()
        self._flow_layout.update()
        self._container.updateGeometry()
        self._card_geoms.clear()
        self._card_states.clear()
        self._filter_query = ''
        gc.collect()

    def set_entries(self, entries: list[RepackEntry]) -> None:
        self.clear()
        self.append_entries(entries)
        self._flow_layout.invalidate()
        self._flow_layout.update()
        self._container.updateGeometry()

    def append_entries(self, entries: list[RepackEntry]) -> None:
        added_any = False
        for entry in entries:
            if entry.url in self._entries_by_url:
                continue
            idx = len(self._entries)
            self._entries.append(entry)
            self._entries_by_url[entry.url] = entry
            self._entry_index[entry.url] = idx
            added_any = True
        if not added_any:
            return
        self._card_geoms.clear()
        self._flow_layout.invalidate()
        self._flow_layout.update()
        self._container.updateGeometry()
        self._update_offscreen_cards()
        self._visibility_timer.start()
        from PySide6.QtCore import QTimer
        QTimer.singleShot(0, self._update_offscreen_cards)

    def _materialize_card(self, entry: RepackEntry) -> PosterCard:
        card = PosterCard(entry, self._container)
        card.clicked_poster.connect(self.poster_clicked.emit)
        idx = self._entry_index[entry.url]
        insert_at = bisect.bisect_left(self._live_positions, idx)
        self._flow_layout.insertWidget(insert_at, card)
        self._live_positions.insert(insert_at, idx)
        self._card_order.insert(insert_at, entry.url)
        self._cards_by_url[entry.url] = card
        if entry.poster_url:
            self._cards_by_poster_url[entry.poster_url] = card
        return card

    def _evict_card(self, url: str) -> None:
        card = self._cards_by_url.pop(url, None)
        if card is None:
            return
        pos = self._entries_by_url.get(url)
        if pos is not None and pos.poster_url:
            self._cards_by_poster_url.pop(pos.poster_url, None)
        try:
            i = self._card_order.index(url)
            self._card_order.pop(i)
            self._live_positions.pop(i)
        except ValueError:
            pass
        self._flow_layout.removeWidget(card)
        card.setParent(None)
        card.deleteLater()
        self._card_geoms.pop(url, None)
        self._card_states.pop(url, None)

    def _row_metrics(self):
        width = self._flow_container.width() or self.viewport().width()
        if width <= 0:
            width = self.width() or PosterCard.POSTER_WIDTH
        spacing = self._flow_layout.spacing()
        card_w = PosterCard.POSTER_WIDTH
        cols = max(1, (width + spacing) // (card_w + spacing))
        row_h = PosterCard.POSTER_HEIGHT + PosterCard._HOVER_HEADROOM_PX + spacing
        return cols, row_h

    def _materialize_visible_entries(self, viewport_top: int, viewport_bottom: int) -> None:
        if not self._entries:
            return
        cols, row_h = self._row_metrics()
        if row_h <= 0:
            return
        base_y = self._flow_container.y()
        first_row = max(0, (viewport_top - base_y) // row_h - self._MATERIALIZE_BUFFER_ROWS)
        last_row = (viewport_bottom - base_y) // row_h + self._MATERIALIZE_BUFFER_ROWS
        start_idx = max(0, int(first_row) * cols)
        end_idx = min(len(self._entries), (int(last_row) + 1) * cols)
        for idx in range(start_idx, end_idx):
            entry = self._entries[idx]
            if entry.url not in self._cards_by_url:
                self._materialize_card(entry)

    def _evict_far_cards(self, viewport_top: int) -> None:
        if len(self._card_order) <= self._MAX_LIVE_CARDS:
            return
        cols, row_h = self._row_metrics()
        if row_h <= 0:
            return
        base_y = self._flow_container.y()
        viewport_bottom = viewport_top + self.viewport().height() + self._VISIBILITY_MARGIN_PX
        while len(self._card_order) > self._MAX_LIVE_CARDS:
            front_url = self._card_order[0]
            back_url = self._card_order[-1]
            front_idx = self._entry_index.get(front_url, 0)
            back_idx = self._entry_index.get(back_url, 0)
            front_bottom = base_y + (front_idx // cols + 1) * row_h
            back_top = base_y + (back_idx // cols) * row_h
            front_far = front_bottom < viewport_top - self._EVICT_MARGIN_PX
            back_far = back_top > viewport_bottom + self._EVICT_MARGIN_PX
            if front_far and (not back_far or (viewport_top - front_bottom) >= (back_top - viewport_bottom)):
                self._evict_card(front_url)
            elif back_far:
                self._evict_card(back_url)
            else:
                break

    def _request_poster_if_visible(self, entry: RepackEntry, card) -> bool:
        if not entry.poster_url or entry.poster_path:
            return False
        viewport_top = self.verticalScrollBar().value() - self._VISIBILITY_MARGIN_PX
        viewport_bottom = viewport_top + self.viewport().height() + self._VISIBILITY_MARGIN_PX
        card_top = card.y()
        card_bottom = card_top + card.height()
        if card_bottom >= viewport_top and card_top <= viewport_bottom:
            self._poster_downloader.request(entry.poster_url)
            return True
        return False

    def _on_poster_ready(self, url: str, path: str) -> None:
        card = self._cards_by_poster_url.get(url)
        if card is not None:
            card.set_poster_path(path)
        for lcard in self._latest_cards:
            if isinstance(lcard, PosterCard) and lcard.poster_url == url:
                lcard.set_poster_path(path)

    def _on_poster_failed(self, url: str, error: str) -> None:
        card = self._cards_by_poster_url.get(url)
        if card is not None:
            card.show_fallback_icon()
        for lcard in self._latest_cards:
            if isinstance(lcard, PosterCard) and lcard.poster_url == url:
                lcard.show_fallback_icon()

    def _on_scroll_value_changed(self, value: int) -> None:
        if value == self._last_scroll_value:
            return
        self._last_scroll_value = value
        bar = self.verticalScrollBar()
        if bar.maximum() - value <= self._NEAR_BOTTOM_THRESHOLD_PX:
            self.near_bottom.emit()
        if not self._visibility_timer.isActive():
            self._visibility_timer.start()

    def _refresh_card_geoms(self) -> None:
        self._card_geoms = {url: (card.y(), card.height()) for url, card in self._cards_by_url.items() if card.isVisible()}

    def _update_offscreen_cards(self) -> None:
        if self._filter_query:
            return
        viewport_top = self.verticalScrollBar().value() - self._VISIBILITY_MARGIN_PX
        viewport_bottom = viewport_top + self.viewport().height() + self._VISIBILITY_MARGIN_PX
        self._materialize_visible_entries(viewport_top, viewport_bottom)
        self._evict_far_cards(viewport_top)
        if not self._card_geoms or len(self._card_geoms) != len(self._cards_by_url):
            self._refresh_card_geoms()
        for url, (card_top, card_height) in self._card_geoms.items():
            card_bottom = card_top + card_height
            in_range = card_bottom >= viewport_top and card_top <= viewport_bottom
            was_in_range = self._card_states.get(url)
            if in_range == was_in_range:
                continue
            self._card_states[url] = in_range
            card = self._cards_by_url.get(url)
            if card is None:
                continue
            if in_range:
                entry = self._entries_by_url.get(url)
                if entry is not None:
                    self._request_poster_if_visible(entry, card)
                card.reload_pixmap_if_needed()
            else:
                card.unload_pixmap()
        for card in self._latest_cards:
            if not isinstance(card, PosterCard):
                continue
            visible = card.isVisible() and self.visibleRegion().intersects(card.geometry())
            if visible:
                card.reload_pixmap_if_needed()
            else:
                card.unload_pixmap()

    def resizeEvent(self, event):
        self._card_geoms.clear()
        super().resizeEvent(event)
        bar = self.verticalScrollBar()
        bar.move(self.width() - bar.width(), bar.y())
        if not self._visibility_timer.isActive():
            self._visibility_timer.start()
        from PySide6.QtCore import QTimer
        QTimer.singleShot(0, self._update_latest_visible_count)

    def showEvent(self, event):
        super().showEvent(event)
        self._card_geoms.clear()
        if not self._visibility_timer.isActive():
            self._visibility_timer.start()
        from PySide6.QtCore import QTimer
        QTimer.singleShot(0, self._update_latest_visible_count)

    def filter_by_title(self, query: str) -> None:
        query = query.strip().lower()
        self._filter_query = query
        if not query:
            self._card_geoms.clear()
            self._update_offscreen_cards()
            self._flow_layout.update()
            self._visibility_timer.start()
            return
        for entry in self._entries:
            if query in entry.title.lower():
                if entry.url not in self._cards_by_url:
                    self._materialize_card(entry)
                card = self._cards_by_url[entry.url]
                card.setVisible(True)
                card.reload_pixmap_if_needed()
                if entry.poster_url and not entry.poster_path:
                    self._poster_downloader.request(entry.poster_url)
            else:
                card = self._cards_by_url.get(entry.url)
                if card is not None:
                    card.setVisible(False)
        self._flow_layout.update()
        self._visibility_timer.stop()

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
    return pill

def _round_widget_corners(widget: QWidget, radius: int) -> None:
    from PySide6.QtGui import QRegion, QPainterPath
    from PySide6.QtCore import QRectF

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

class ScreenshotEnlargeDialog(MessageBoxBase):

    def __init__(self, image_path: str, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.yesButton.setText(tr('repacks.close'))
        self.hideCancelButton()
        img = ImageLabel(image_path, self)
        img.setBorderRadius(8, 8, 8, 8)
        from PySide6.QtGui import QPixmap
        pix = QPixmap(image_path)
        if not pix.isNull():
            max_w, max_h = (1100, 720)
            if pix.width() > max_w or pix.height() > max_h:
                scaled = pix.scaled(max_w, max_h, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)
            else:
                scaled = pix
            img.setFixedSize(scaled.size())
            img.setImage(pix)
            img.setScaledSize(scaled.size())
        self.viewLayout.addWidget(img)

    def keyPressEvent(self, event) -> None:
        if event.key() == Qt.Key.Key_Escape:
            self.reject()
            event.accept()
            return
        super().keyPressEvent(event)

    def closeEvent(self, event) -> None:
        self.reject()
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
        layout.addStretch(1)
        self._layout = layout

    def _ensure_pipeline(self) -> None:
        if self._player is not None:
            return
        from PySide6.QtMultimediaWidgets import QGraphicsVideoItem
        from PySide6.QtMultimedia import QMediaPlayer
        from PySide6.QtWidgets import QGraphicsScene, QGraphicsView
        from PySide6.QtGui import QPainter
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
        from PySide6.QtCore import QUrl
        try:
            self._player.stop()
            self._player.setVideoOutput(None)
            self._player.setSource(QUrl())
        except RuntimeError:
            pass
        self._layout.removeWidget(self._view)
        self._view.deleteLater()
        self._player.deleteLater()
        self._view = None
        self._player = None
        self._video_item = None
        self._scene = None
        self._local_path = None
        self._started = False
        self._requested = False

    def activate(self) -> None:
        if self._requested:
            return
        self._requested = True
        self._ensure_pipeline()
        self._downloader.video_ready.connect(self._on_video_ready)
        self._downloader.request(self._video_url)

    def deactivate(self) -> None:
        if not self._requested:
            return
        try:
            self._downloader.video_ready.disconnect(self._on_video_ready)
        except (TypeError, RuntimeError):
            pass
        self._release_pipeline()

    def _on_video_ready(self, url: str, path: str) -> None:
        if url != self._video_url or self._player is None:
            return
        self._local_path = path
        from PySide6.QtCore import QUrl
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
        from PySide6.QtWidgets import QGraphicsScene, QGraphicsView
        from PySide6.QtGui import QPainter
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
        from PySide6.QtCore import QUrl
        self._player.setSource(QUrl.fromLocalFile(path))
        self._player.play()

    def _stop_playback(self) -> None:
        from PySide6.QtCore import QUrl
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

    def __init__(self, url: str, downloader, parent=None):
        super().__init__(parent)
        self._url = url
        self._path: str | None = None
        self.setFixedSize(self.MAX_W, self.FALLBACK_H)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self._img = ImageLabel(self)
        self._img.setBorderRadius(8, 8, 8, 8)
        self._img.setFixedSize(self.MAX_W, self.FALLBACK_H)
        layout.addWidget(self._img)
        self._downloader = downloader
        self._downloader.poster_ready.connect(self._on_ready)
        self._downloader.request(url)

    def _on_ready(self, url: str, path: str) -> None:
        if url != self._url:
            return
        self._path = path
        from PySide6.QtGui import QImageReader
        reader = QImageReader(path)
        orig_size = reader.size()
        if orig_size.isValid() and orig_size.width() > 0 and (orig_size.height() > 0):
            scale = min(self.MAX_W / orig_size.width(), self.MAX_H / orig_size.height(), 1.0)
            target_w = max(1, round(orig_size.width() * scale))
            target_h = max(1, round(orig_size.height() * scale))
        else:
            target_w, target_h = (self.MAX_W, self.FALLBACK_H)
        pix = _load_scaled_pixmap(path, target_w, target_h)
        if not pix.isNull():
            if pix.width() != target_w or pix.height() != target_h:
                pix = pix.scaled(target_w, target_h, Qt.AspectRatioMode.IgnoreAspectRatio, Qt.TransformationMode.SmoothTransformation)
            self.setFixedSize(target_w, target_h)
            self._img.setFixedSize(target_w, target_h)
            self._img.setImage(pix)
            self._img.setScaledSize(self._img.size())

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
        self._poster_downloader = PosterDownloader(self)
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
                thumb = _ScreenshotThumb(url, self._poster_downloader, self)
                thumb.clicked.connect(self._on_thumb_clicked)
            self._stack_layout.addWidget(thumb)
            self._thumbs.append(thumb)
        self.update_visible_videos()

    def clear(self) -> None:
        for thumb in self._thumbs:
            if isinstance(thumb, _VideoThumb):
                thumb.deactivate()
            self._stack_layout.removeWidget(thumb)
            thumb.deleteLater()
        self._thumbs.clear()

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
        for thumb in self._thumbs:
            if not isinstance(thumb, _VideoThumb):
                continue
            top_left = thumb.mapTo(scroll.widget(), thumb.rect().topLeft())
            thumb_top = top_left.y()
            thumb_bottom = thumb_top + thumb.height()
            if thumb_bottom >= viewport_top - margin and thumb_top <= viewport_bottom + margin:
                thumb.activate()
            else:
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
        dialog.setModal(True)
        dialog.finished.connect(lambda *_: self._resume_all_videos())
        dialog.show()
        dialog.raise_()
        dialog.activateWindow()

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
        from PySide6.QtGui import QPainter, QPen, QPainterPath
        from PySide6.QtCore import QPointF
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

class CollapsibleSection(CardWidget):
    _ANIM_MS = 160
    toggled = Signal(bool)

    def __init__(self, title: str, parent=None, expanded: bool=False):
        super().__init__(parent)
        self.setBorderRadius(12)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
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
        wrap_layout = QVBoxLayout(self._body_wrap)
        wrap_layout.setContentsMargins(0, 0, 0, 0)
        wrap_layout.setSpacing(0)
        self._body = QWidget()
        body_layout = QVBoxLayout(self._body)
        body_layout.setContentsMargins(16, 4, 16, 18)
        self._body_lbl = BodyLabel('')
        self._body_lbl.setWordWrap(True)
        self._body_lbl.setTextFormat(Qt.TextFormat.RichText)
        self._body_lbl.setOpenExternalLinks(True)
        self._body_lbl.setStyleSheet(f'font-size: 14px; font-weight: 500; line-height: {_DESC_LINE_HEIGHT_PCT}%; color: {_body_text_color()}; background: transparent;')
        body_layout.addWidget(self._body_lbl)
        wrap_layout.addWidget(self._body)
        outer.addWidget(self._body_wrap)
        self._header.mousePressEvent = lambda _e: self.toggle()
        self._height_anim = QPropertyAnimation(self._body_wrap, b'maximumHeight')
        self._height_anim.setDuration(self._ANIM_MS)
        self._height_anim.setEasingCurve(QEasingCurve.Type.OutQuad)
        self._chevron_anim = QPropertyAnimation(self._chevron, b'angle')
        self._chevron_anim.setDuration(self._ANIM_MS)
        self._chevron_anim.setEasingCurve(QEasingCurve.Type.OutQuad)
        self._hovering = False
        self._cached_body_height = 0
        self._expanded = expanded
        self._body_wrap.setMaximumHeight(16777215 if self._expanded else 0)
        self._chevron.set_angle(180.0 if self._expanded else 0.0)
        self._raw_body_text = ''
        self._body_is_features = False
        self._update_header_style()
        qconfig.themeChanged.connect(self._on_theme_changed)
        self.destroyed.connect(self._disconnect_theme_signal)

    def _disconnect_theme_signal(self, *_args) -> None:
        try:
            qconfig.themeChanged.disconnect(self._on_theme_changed)
        except TypeError:
            pass

    def _on_theme_changed(self, *_args) -> None:
        from PySide6.QtCore import QTimer
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

    def _content_height(self) -> int:
        width = self._body_lbl.width()
        if width <= 0:
            width = self.width() - 32 if self.width() > 32 else 268
        h = self._body_lbl.heightForWidth(width)
        if h <= 0:
            h = self._body_lbl.sizeHint().height()
        return h + 22

    def set_body_html(self, html: str) -> None:
        self._body_lbl.setText(html)
        if self._expanded:
            from PySide6.QtCore import QTimer
            QTimer.singleShot(0, self._apply_expanded_height)

    def _apply_expanded_height(self) -> None:
        if not self._expanded:
            return
        self._body_lbl.setMinimumHeight(0)
        new_height = self._content_height()
        if self._body_wrap.minimumHeight() != new_height:
            self._body_wrap.setMinimumHeight(new_height)
        if self._body_wrap.maximumHeight() != 16777215:
            self._body_wrap.setMaximumHeight(16777215)

    def toggle(self) -> None:
        self._expanded = not self._expanded
        self.toggled.emit(self._expanded)
        target_angle = 180.0 if self._expanded else 0.0
        try:
            self._height_anim.finished.disconnect(self._on_anim_finished)
        except TypeError:
            pass
        current_height = self._body_wrap.maximumHeight()
        if current_height > 16000000:
            current_height = self._content_height()
        if self._expanded:
            self._body_lbl.setMinimumHeight(0)
            start_height = 0
            target_height = self._content_height()
        else:
            start_height = current_height
            target_height = 0
            self._body_wrap.setMinimumHeight(0)
        self._height_anim.stop()
        self._height_anim.setStartValue(start_height)
        self._height_anim.setEndValue(target_height)
        self._height_anim.finished.connect(self._on_anim_finished)
        self._height_anim.start()
        self._chevron_anim.stop()
        self._chevron_anim.setStartValue(self._chevron.get_angle())
        self._chevron_anim.setEndValue(target_angle)
        self._chevron_anim.start()
        self._update_header_style()

    def _on_anim_finished(self) -> None:
        if self._expanded:
            self._apply_expanded_height()
        else:
            self._body_wrap.setMinimumHeight(0)

    def _apply_card_background(self) -> None:
        self.update()

    def _card_bg_color(self) -> QColor:
        return _qcolor_from_palette(palette()['section_card_bg'])

    def _card_border_color(self) -> QColor:
        return _qcolor_from_palette(palette()['section_card_border'])

    def paintEvent(self, event):
        from PySide6.QtGui import QPainter, QPainterPath
        from PySide6.QtCore import QRectF
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
        if self._raw_body_text:
            html = render_features_html(self._raw_body_text) if self._body_is_features else render_description_html(self._raw_body_text)
            self._body_lbl.setText(html)
        if self._hovering:
            self._header.setStyleSheet(f'#sectionHeader {{ background: {_hover_tint_color()}; border-top-left-radius: 12px; border-top-right-radius: 12px; }}')
        else:
            self._header.setStyleSheet('#sectionHeader { background: transparent; }')

    @property
    def is_expanded(self) -> bool:
        return self._expanded
_URL_RE = re.compile('(https?://[^\\s<>\\"]+)')

def _escape_html(text: str) -> str:
    return text.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')

def _linkify_inline(text: str) -> str:

    def _wrap(match: 're.Match[str]') -> str:
        url = match.group(1)
        return f'<a href="{url}" style="color:{themeColor().name()};">{url}</a>'
    return _URL_RE.sub(_wrap, text)

def _linkify(text: str) -> str:
    if not text:
        return text
    escaped = _linkify_inline(_escape_html(text))
    paragraphs = escaped.split('\n\n')
    paragraphs = [p.replace('\n', '<br>') for p in paragraphs]
    return '<br><br>'.join(paragraphs)
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
        href = a.get('href', '')
        a.attrs = {'href': href, 'style': f'color:{color}; text-decoration:underline; word-wrap:break-word; overflow-wrap:break-word; word-break:break-all;'}
    container = soup.find('div')
    body = container.decode_contents() if container is not None else soup.decode_contents()
    return f'<div style="line-height:{_DESC_LINE_HEIGHT_PCT}%; word-wrap:break-word; overflow-wrap:break-word; word-break:break-all;">{body}</div>'

def render_description_html(text: str) -> str:
    if not text:
        return ''
    blocks = re.split('\\n\\s*\\n', text.strip())
    html_parts: list[str] = []

    def _flush_prose(prose_lines: list[str]) -> None:
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
        size_bytes = file_sizes[i] if file_sizes and i < len(file_sizes) else 0
        size_hint = _human_size(size_bytes)
        is_bonus = display_name.lower().startswith(_BONUS_FILE_PREFIX)
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

class SelectiveDownloadDialog(MessageBoxBase):
    _ORDERED_CATEGORIES = ('Required', 'Bonus Content')

    def __init__(self, entries: list[SelectiveDownloadEntry], parent=None):
        super().__init__(parent)
        self._entries = entries
        self._checkboxes: dict[str, QTreeWidgetItem] = {}
        self.titleLabel = SubtitleLabel(tr('repacks.choose_files_to_download'), self)
        self.viewLayout.addWidget(self.titleLabel)
        info = CaptionLabel(tr('repacks.required_files_hint'), self)
        info.setWordWrap(True)
        info.setStyleSheet(f'color: {_muted_text_color()};')
        self.viewLayout.addWidget(info)
        self._tree = TreeWidget(self)
        self._tree.setHeaderLabels([tr('repacks.include'), tr('repacks.size')])
        self._tree.header().setStretchLastSection(False)
        self._tree.header().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self._tree.header().setSectionResizeMode(1, QHeaderView.ResizeMode.Fixed)
        self._tree.setColumnWidth(1, 92)
        self._tree.setTextElideMode(Qt.TextElideMode.ElideRight)
        self._tree.setRootIsDecorated(True)
        self._tree.setBorderVisible(True)
        self._tree.setBorderRadius(8)
        self._tree.setMinimumHeight(420)
        self.viewLayout.addWidget(self._tree)
        by_category: dict[str, list[SelectiveDownloadEntry]] = {}
        for e in entries:
            by_category.setdefault(e.category, []).append(e)
        _category_labels = {'Required': tr('repacks.required'), 'Bonus Content': tr('repacks.bonus_content')}
        for cat in self._ORDERED_CATEGORIES:
            if cat not in by_category:
                continue
            cat_item = QTreeWidgetItem([_category_labels.get(cat, cat), ''])
            cat_item.setFlags(cat_item.flags() & ~Qt.ItemFlag.ItemIsUserCheckable)
            self._tree.addTopLevelItem(cat_item)
            font = self._tree.font()
            font.setBold(True)
            cat_item.setFont(0, font)
            for entry in by_category[cat]:
                child = QTreeWidgetItem([entry.label, entry.size_hint])
                child.setToolTip(0, entry.label)
                child.setFlags(child.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                child.setCheckState(0, Qt.CheckState.Checked if entry.required else Qt.CheckState.Unchecked)
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
        self.widget.setMinimumWidth(560)

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

class _FileListFetchThread(QThread):
    finished_ok = Signal(list)
    finished_err = Signal(str)

    def __init__(self, manager: DownloadManager, source: str, parent=None):
        super().__init__(parent)
        self._manager = manager
        self._source = source

    def run(self) -> None:
        try:
            files = self._manager.fetch_file_list(self._source, category='repacks')
        except Exception as exc:
            self.finished_err.emit(str(exc))
            return
        self.finished_ok.emit(files)

class DownloadActionWidget(QWidget):

    def __init__(self, manager: DownloadManager, source_key: str | None = None, parent=None):
        super().__init__(parent)
        self._manager = manager
        self._source_key = source_key
        self._details: RepackDetails | None = None
        self._item_id: str | None = None
        self._filelist_thread: _FileListFetchThread | None = None
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
        self._button.setText(tr('repacks.download'))
        self._button.setEnabled(True)
    _DEFAULT_BUTTON_COLOR = QColor('#00b7c3')

    def set_accent_color(self, color) -> None:
        self._current_accent_color = color
        self._reapply_accent_color()

    def _deferred_reapply_accent_color(self) -> None:
        from PySide6.QtCore import QTimer
        self._reapply_accent_color()
        QTimer.singleShot(0, self._reapply_accent_color)

    def _reapply_accent_color(self) -> None:
        color = self._current_accent_color
        if color is None:
            color = self._DEFAULT_BUTTON_COLOR
        luminance = 0.299 * color.red() + 0.587 * color.green() + 0.114 * color.blue()
        text_color = '#1a1a1a' if luminance > 150 else '#ffffff'
        hover = color.lighter(112)
        pressed = color.darker(112)
        self._button.setStyleSheet(f'PrimaryPushButton {{ background-color: rgb({color.red()}, {color.green()}, {color.blue()}); color: {text_color}; border: none; border-radius: 8px; }} PrimaryPushButton:hover {{ background-color: rgb({hover.red()}, {hover.green()}, {hover.blue()}); color: {text_color}; }} PrimaryPushButton:pressed {{ background-color: rgb({pressed.red()}, {pressed.green()}, {pressed.blue()}); color: {text_color}; }} PrimaryPushButton:disabled {{ background-color: rgb({color.red()}, {color.green()}, {color.blue()}); color: {text_color}; }}')

    def _on_click(self) -> None:
        if self._details is None or self._item_id is not None:
            return
        download_sources = self._details.extra.get('download_sources') or {}
        if download_sources:
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
            self._start_selective_download(magnet_url)
            return
        magnet_url = self._details.extra.get('magnet_url')
        torrent_url = self._details.extra.get('torrent_url')
        source = magnet_url or torrent_url
        if not source:
            self._button.setText(tr('repacks.no_link_found'))
            self._button.setEnabled(False)
            return
        self._start_selective_download(source)

    _FILELIST_TIMEOUT_MS = 15000

    def _nothing_found_text(self) -> str:
        return tr('repacks.dead_link_gog') if self._source_key == 'gog' else tr('repacks.nothing_found')

    def _start_selective_download(self, source: str) -> None:
        if self._filelist_thread is not None and self._filelist_thread.isRunning():
            return
        self._button.setText(tr('repacks.loading_file_list'))
        self._button.setEnabled(False)
        self._filelist_timed_out = False
        thread = _FileListFetchThread(self._manager, source, None)
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
        self._filelist_timed_out = True
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
        thread.finished.connect(thread.deleteLater)
        self._filelist_thread = None
        logger.warning('File list fetch timed out for source; giving up')
        self._button.setText(self._nothing_found_text())
        self._button.setEnabled(False)

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

    def _on_file_list_ready(self, source: str, torrent_files: list[tuple[str, int]]) -> None:
        self._button.setText(tr('repacks.download'))
        self._button.setEnabled(True)
        if not torrent_files:
            self._queue_download(source, file_ids=None)
            return
        paths = [p for p, _size in torrent_files]
        sizes = [s for _p, s in torrent_files]
        entries = build_selective_entries_from_torrent(paths, sizes)
        dialog = SelectiveDownloadDialog(entries, parent=self.window())
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        selected_labels = dialog.selected_labels()
        file_ids = resolve_selective_file_indices(entries, selected_labels, paths)
        if not file_ids:
            logger.warning('Selective download selection produced no file indices; downloading full torrent')
            file_ids = None
        self._queue_download(source, file_ids=file_ids)

    def _queue_download(self, source: str, file_ids: list[int] | None) -> None:
        self._button.setText(tr('repacks.queuing'))
        self._button.setEnabled(False)
        self._item_id = self._manager.add(torrent_file=source, file_id=file_ids[0] if file_ids else 1, file_ids=file_ids, game_name=self._details.title, console='', source='FitGirl', category='repacks')

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
        self._button.setText(tr('repacks.download'))
        self._button.setEnabled(True)

    def shutdown(self) -> None:
        thread = self._filelist_thread
        self._filelist_thread = None
        if thread is None:
            return
        try:
            if thread.isRunning():
                thread.quit()
                thread.wait(2000)
        except RuntimeError:
            pass

class RepackDetailsView(QWidget):
    back_requested = Signal()
    COVER_WIDTH = 280
    COVER_HEIGHT = 392
    @property
    def _META_LABELS(self):
        return (('repack_size', tr('repacks.repack_size')), ('original_size', tr('repacks.original_size')), ('company', tr('repacks.company')), ('languages', tr('repacks.languages')), ('rating', tr('repacks.rating')))

    def __init__(self, manager: DownloadManager, source_key: str | None = None, parent=None):
        super().__init__(parent)
        self._download_action = DownloadActionWidget(manager, source_key)
        self._poster_downloader = PosterDownloader(self)
        self._poster_downloader.poster_ready.connect(self._on_cover_ready)
        self._poster_downloader.poster_failed.connect(self._on_cover_failed)
        self._pending_cover_url: str | None = None
        self._section_widgets: list[CollapsibleSection] = []
        self._meta_widgets: list[MetaField] = []
        self._tag_widgets: list[PillPushButton] = []
        self._expanded_state: dict[str, bool] = {}
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 4, 0, 0)
        outer.setSpacing(10)
        back_btn = PushButton(FluentIcon.RETURN, tr('repacks.back_to_grid'))
        back_btn.setFixedHeight(32)
        self._back_btn = back_btn
        outer.addWidget(back_btn, 0, Qt.AlignmentFlag.AlignLeft)
        back_btn.clicked.connect(self.back_requested.emit)
        self._scroll = SmoothScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        self._scroll.setStyleSheet('background: transparent; border: none;')
        outer.addWidget(self._scroll, 1)
        content = QWidget()
        self._scroll.setWidget(content)
        self._content_layout = QVBoxLayout(content)
        self._content_layout.setContentsMargins(4, 12, 24, 40)
        self._content_layout.setSpacing(24)
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
        self._cover_fallback_icon_lbl.setPixmap(FluentIcon.GAME.icon(color=QColor(_body_text_color())).pixmap(48, 48))
        self._cover_fallback_icon_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._cover_fallback_icon_lbl.setStyleSheet('background: transparent;')
        cover_fallback_layout.addWidget(self._cover_fallback_icon_lbl)
        self._cover_fallback_title_lbl = QLabel('', self._cover_fallback)
        self._cover_fallback_title_lbl.setWordWrap(True)
        self._cover_fallback_title_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._cover_fallback_title_lbl.setStyleSheet(f'background: transparent; font-size: 13px; color: {palette()["primary_text"]};')
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
        from PySide6.QtCore import QTimer
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
        from PySide6.QtWidgets import QGridLayout
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
        self._desc_lbl = BodyLabel('')
        self._desc_lbl.setWordWrap(True)
        self._desc_lbl.setTextFormat(Qt.TextFormat.RichText)
        self._desc_lbl.setOpenExternalLinks(True)
        self._desc_default_qss_template = 'font-size: 18px; font-weight: 500; line-height: {pct}%; color: {color};'
        self._desc_announcement_qss_template = 'font-size: 16px; font-weight: 500; line-height: {pct}%; color: {color};'
        self._desc_default_qss = self._desc_default_qss_template.format(pct=_DESC_LINE_HEIGHT_PCT, color=_body_text_color())
        self._desc_lbl.setStyleSheet(self._desc_default_qss)
        info_col.addWidget(self._desc_lbl)
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
        self._site_link_btn = HyperlinkButton('', tr('repacks.view_on_website'), sections_container, FluentIcon.LINK)
        self._site_link_btn.setIconSize(QSize(14, 14))
        self._apply_site_link_style()
        qconfig.themeColorChanged.connect(self._deferred_apply_site_link_style)
        qconfig.themeChanged.connect(self._deferred_apply_site_link_style)
        self._site_link_btn.setVisible(False)
        self._sections_col.addWidget(self._site_link_btn)
        self._sections_insert_index = self._sections_col.count()
        self._sections_header.setVisible(False)
        top_row.addWidget(sections_container, 0, Qt.AlignmentFlag.AlignTop)
        self._content_layout.addLayout(top_row)
        self._content_layout.addStretch(1)
        self.set_cover_action_widget(self._download_action)
        register_locale_refresh(self, self._apply_locale)

    def _apply_locale(self, *_args) -> None:
        self._back_btn.setText(tr('repacks.back_to_grid'))
        self._tags_header.setText(tr('repacks.genres_tags'))
        self._desc_header.setText(tr('repacks.description'))
        self._sections_header.setText(tr('repacks.details'))
        self._site_link_btn.setText(tr('repacks.view_on_website'))

    def _deferred_apply_site_link_style(self) -> None:
        from PySide6.QtCore import QTimer
        self._apply_site_link_style()
        QTimer.singleShot(0, self._apply_site_link_style)

    def _apply_site_link_style(self) -> None:
        color = themeColor()
        base_alpha = 55 if isDarkTheme() else 40
        hover_alpha = 85 if isDarkTheme() else 65
        text_color = palette()['primary_text']
        self._site_link_btn.setStyleSheet(f'HyperlinkButton {{ background-color: rgba({color.red()}, {color.green()}, {color.blue()}, {base_alpha}); border-radius: 6px; padding: 6px 10px 6px 30px; font-weight: 600; color: {text_color}; }} HyperlinkButton:hover {{ background-color: rgba({color.red()}, {color.green()}, {color.blue()}, {hover_alpha}); color: {text_color}; }}')
        self._site_link_btn.setIcon(FluentIcon.LINK.icon(color=QColor(text_color)))

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
        from PySide6.QtCore import QTimer
        self._refresh_desc_theme()
        self._refresh_cover_fallback_theme()
        QTimer.singleShot(0, self._refresh_desc_theme)

    def _refresh_cover_fallback_theme(self) -> None:
        self._cover_fallback.setStyleSheet(f'background-color: {_surface_tint_color(14)}; border-radius: 10px;')
        self._cover_fallback_icon_lbl.setPixmap(FluentIcon.GAME.icon(color=QColor(_body_text_color())).pixmap(48, 48))
        self._cover_fallback_title_lbl.setStyleSheet(f'background: transparent; font-size: 13px; color: {palette()["primary_text"]};')

    def _refresh_desc_theme(self) -> None:
        self._apply_desc_style()
        if self._raw_intro_text:
            self._desc_lbl.setText(render_description_html(self._raw_intro_text))

    def set_cover_action_widget(self, widget) -> None:
        while self._cover_action_layout.count():
            item = self._cover_action_layout.takeAt(0)
            if item is not None and item.widget() is not None:
                item.widget().deleteLater()
        if widget is not None:
            self._cover_action_layout.addWidget(widget)

    def show_loading(self, entry: RepackEntry) -> None:
        self._title_lbl.setText(entry.title)
        self._title_lbl.setStyleSheet(f'font-weight: 700; color: {palette()['primary_text']};')
        self._is_announcement = False
        self._raw_intro_text = ''
        self._desc_default_qss = self._desc_default_qss_template.format(pct=_DESC_LINE_HEIGHT_PCT, color=_body_text_color())
        self._desc_lbl.setStyleSheet(self._desc_default_qss)
        self._desc_lbl.setText(tr('repacks.loading_details'))
        self._clear_sections()
        self._clear_meta()
        self._clear_tags()
        self._gallery.clear()
        self._gallery.setVisible(False)
        self._site_link_btn.setVisible(False)
        self._download_action.set_accent_color(None)
        self._download_action.setVisible(True)
        if entry.poster_path:
            self._cover_lbl.setVisible(True)
            self._cover_fallback.setVisible(False)
            self._set_cover_path(entry.poster_path)
        elif entry.poster_url:
            self._cover_lbl.setVisible(True)
            self._cover_fallback.setVisible(False)
            self._pending_cover_url = entry.poster_url
            self._poster_downloader.request(entry.poster_url)
        else:
            self._cover_lbl.setVisible(False)
            self._show_cover_fallback(entry.title)

    def show_details(self, details: RepackDetails) -> None:
        self._download_action.set_details(details)
        self._title_lbl.setText(details.title)
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
        self._raw_intro_text = intro_text
        self._desc_lbl.setText(render_description_html(intro_text) or tr('repacks.no_description'))
        extra = dict(details.extra or {})
        system_requirements_text = extra.get('system_requirements')
        if system_requirements_text:
            extra_sections.append((tr('repacks.system_requirements'), system_requirements_text))
        is_announcement = bool(extra.pop('is_announcement', False))
        self._is_announcement = is_announcement
        self._download_action.setVisible(not is_announcement)
        self._site_link_btn.setVisible(bool(details.url) and (not is_announcement))
        self._apply_desc_style()
        if details.size_info:
            extra.setdefault('repack_size', details.size_info)
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
            from PySide6.QtCore import QTimer
            QTimer.singleShot(0, self._tags_layout.update)
        self._sections_header.setVisible(bool(extra_sections) or bool(extra.get('repack_features')) or bool(extra.get('game_updates_html')))
        game_updates_html = extra.pop('game_updates_html', '')
        if game_updates_html:
            heading = tr('repacks.game_updates')
            expanded = self._expanded_state.get(heading, False)
            section = CollapsibleSection(heading, expanded=expanded)
            section.set_body_html(render_game_updates_html(game_updates_html))
            section.toggled.connect(lambda is_open, h=heading: self._expanded_state.__setitem__(h, is_open))
            self._sections_col.insertWidget(self._sections_insert_index, section)
            self._section_widgets.append(section)
            self._sections_insert_index += 1
        repack_features_text = extra.pop('repack_features', '')
        if repack_features_text:
            repack_sub_sections = _split_description_sections(repack_features_text)
            for heading, body in repack_sub_sections:
                heading = heading or tr('repacks.repack_features')
                expanded = self._expanded_state.get(heading, False)
                section = CollapsibleSection(heading, expanded=expanded)
                section.set_body_text(body)
                section.toggled.connect(lambda is_open, h=heading: self._expanded_state.__setitem__(h, is_open))
                self._sections_col.insertWidget(self._sections_insert_index, section)
                self._section_widgets.append(section)
                self._sections_insert_index += 1
        for heading, body in extra_sections:
            expanded = self._expanded_state.get(heading, False)
            section = CollapsibleSection(heading, expanded=expanded)
            section.set_body_text(body)
            section.toggled.connect(lambda is_open, h=heading: self._expanded_state.__setitem__(h, is_open))
            self._sections_col.insertWidget(self._sections_insert_index, section)
            self._section_widgets.append(section)
            self._sections_insert_index += 1
        cover_url = details.cover_url
        if details.cover_path:
            self._cover_lbl.setVisible(True)
            self._cover_fallback.setVisible(False)
            self._set_cover_path(details.cover_path)
        elif cover_url:
            self._cover_lbl.setVisible(True)
            self._cover_fallback.setVisible(False)
            self._pending_cover_url = cover_url
            self._poster_downloader.request(cover_url)
        else:
            self._cover_lbl.setVisible(False)
            self._show_cover_fallback(details.title)
        self._gallery.set_urls(details.screenshot_urls)

    def show_error(self, message: str) -> None:
        self._clear_sections()
        self._clear_meta()
        self._clear_tags()
        self._gallery.clear()
        self._gallery.setVisible(False)
        self._desc_lbl.setText(tr('repacks.failed_to_load', message=message))

    def pause_media(self) -> None:
        for thumb in self._gallery._thumbs:
            if isinstance(thumb, _VideoThumb):
                thumb.deactivate()

    def _clear_sections(self) -> None:
        self._sections_insert_index = self._sections_col.indexOf(self._sections_header) + 1
        for section in self._section_widgets:
            self._sections_col.removeWidget(section)
            section.deleteLater()
        self._section_widgets.clear()

    def _clear_meta(self) -> None:
        for field in self._meta_widgets:
            self._meta_grid.removeWidget(field)
            field.deleteLater()
        self._meta_widgets.clear()

    def _clear_tags(self) -> None:
        for tag in self._tag_widgets:
            self._tags_layout.removeWidget(tag)
            tag.deleteLater()
        self._tag_widgets.clear()

    def _on_cover_ready(self, url: str, path: str) -> None:
        if url == self._pending_cover_url:
            self._cover_lbl.setVisible(True)
            self._cover_fallback.setVisible(False)
            self._set_cover_path(path)

    def _on_cover_failed(self, url: str, error: str) -> None:
        if url == self._pending_cover_url:
            self._cover_lbl.setVisible(False)
            self._show_cover_fallback(self._title_lbl.text())

    def _show_cover_fallback(self, title: str) -> None:
        self._cover_fallback_title_lbl.setText(title)
        self._cover_fallback.setVisible(True)

    def _set_cover_path(self, path: str) -> None:
        try:
            _stop_previous_movie(self._cover_lbl)
            pix = _load_scaled_pixmap(path, self.COVER_WIDTH, self.COVER_HEIGHT)
            self._cover_lbl.setImage(pix)
            self._cover_lbl.setFixedSize(self.COVER_WIDTH, self.COVER_HEIGHT)
            self._download_action.set_accent_color(_dominant_color(path))
        except Exception:
            logger.warning('Failed to load cover image: %s', path)

class _SidebarPosterCard(_AnimatedPosterMixin, QWidget):
    clicked_poster = Signal(object)
    THUMB_WIDTH = 140
    THUMB_HEIGHT = 196
    _HOVER_HEADROOM_PX = 8

    def __init__(self, entry: RepackEntry, parent=None):
        super().__init__(parent)
        self._entry = entry
        self.setFixedSize(self.THUMB_WIDTH, self.THUMB_HEIGHT + self._HOVER_HEADROOM_PX)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setToolTip(entry.title)
        self.setStyleSheet('background: transparent; border: none;')
        self._poster_lbl = ImageLabel(self)
        self._poster_lbl.setBorderRadius(6, 6, 6, 6)
        self._poster_lbl.setGeometry(0, self._HOVER_HEADROOM_PX, self.THUMB_WIDTH, self.THUMB_HEIGHT)
        self._fallback_icon = None
        if not entry.poster_url and (not entry.poster_path):
            self.show_fallback_icon()
        self._init_poster_animations()

    def enterEvent(self, event):
        self.animate_hover_enter()
        super().enterEvent(event)

    def leaveEvent(self, event):
        self.animate_hover_leave()
        super().leaveEvent(event)

    def show_fallback_icon(self) -> None:
        if self._fallback_icon is not None:
            return
        self._poster_lbl.setStyleSheet(f'background-color: {_surface_tint_color(14)}; border-radius: 6px;')
        icon_lbl = QLabel(self._poster_lbl)
        icon_lbl.setPixmap(FluentIcon.GAME.icon(color=QColor(_body_text_color())).pixmap(28, 28))
        icon_lbl.setFixedSize(self.THUMB_WIDTH, self.THUMB_HEIGHT)
        icon_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        icon_lbl.setStyleSheet('background: transparent;')
        icon_lbl.show()
        self._fallback_icon = icon_lbl

    def set_poster_path(self, path: str) -> None:
        try:
            if self._fallback_icon is not None:
                self._fallback_icon.deleteLater()
                self._fallback_icon = None
                self._poster_lbl.setStyleSheet('')
            pix = _load_scaled_pixmap(path, self.THUMB_WIDTH, self.THUMB_HEIGHT)
            self._poster_lbl.setImage(pix)
            self._poster_lbl.setFixedSize(self.THUMB_WIDTH, self.THUMB_HEIGHT)
            self._entry.poster_path = path
        except Exception:
            logger.warning('Failed to load sidebar poster image: %s', path)

    def unload_pixmap(self) -> None:
        if self._entry.poster_path:
            self._poster_lbl.setImage(None)
            self._poster_lbl.setFixedSize(self.THUMB_WIDTH, self.THUMB_HEIGHT)

    def reload_pixmap_if_needed(self) -> None:
        if self._entry.poster_path and self._poster_lbl.isNull():
            self.set_poster_path(self._entry.poster_path)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.animate_press()
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            still_hovering = self.rect().contains(event.position().toPoint())
            self.animate_release(still_hovering)
            if still_hovering:
                self.clicked_poster.emit(self._entry)
        super().mouseReleaseEvent(event)

class PopularRepacksSidebar(QWidget):
    SIDEBAR_WIDTH = 320
    poster_clicked = Signal(object)
    content_changed = Signal(bool)

    def __init__(self, source_key: str, parent=None):
        super().__init__(parent)
        self._source_key = source_key
        self._cards: list[_SidebarPosterCard] = []
        self._poster_downloader = PosterDownloader(self)
        self._poster_downloader.poster_ready.connect(self._on_poster_ready)
        self._poster_downloader.poster_failed.connect(self._on_poster_failed)
        self._url_to_cards: dict[str, list[_SidebarPosterCard]] = {}
        self._task = None
        self._collapsed = False
        self._has_content = False
        self._width_anim: QPropertyAnimation | None = None
        self.setFixedWidth(self.SIDEBAR_WIDTH)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(10)
        header = StrongBodyLabel(tr('repacks.most_popular_this_week'))
        header.setWordWrap(True)
        header.setStyleSheet(f'font-size: 14px; font-weight: 600; color: {palette()['primary_text']};')
        self._header = header
        outer.addWidget(header)
        self._scroll = SmoothScrollArea(self)
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        self._scroll.setStyleSheet('background: transparent; border: none;')
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        try:
            from qfluentwidgets import SmoothMode
            self._scroll.setSmoothMode(SmoothMode.LINEAR)
        except Exception:
            pass
        scroll_content = QWidget()
        scroll_content.setStyleSheet('background: transparent;')
        self._grid_layout = QGridLayout(scroll_content)
        self._grid_layout.setContentsMargins(0, 0, 4, 0)
        self._grid_layout.setSpacing(8)
        self._grid_layout.setRowStretch(100, 1)
        self._scroll.setWidget(scroll_content)
        outer.addWidget(self._scroll, 1)
        self.setVisible(False)
        from PySide6.QtCore import QTimer
        self._visibility_timer = QTimer(self)
        self._visibility_timer.setSingleShot(True)
        self._visibility_timer.setInterval(150)
        self._visibility_timer.timeout.connect(self._update_offscreen_cards)
        self._scroll.verticalScrollBar().valueChanged.connect(self._on_scroll_changed)
        self._card_states: dict[int, bool] = {}
        register_locale_refresh(self, self._apply_locale)

    def is_collapsed(self) -> bool:
        return self._collapsed

    def has_content(self) -> bool:
        return self._has_content

    def set_collapsed(self, collapsed: bool, animated: bool=True) -> None:
        """Slide the sidebar closed/open. Reuses a single QPropertyAnimation
        driving the widget's built-in `maximumWidth` Qt property, so no new
        widgets/effects are allocated and layout of the neighbouring poster
        grid reflows on every frame via the normal resize path, letting the
        cards immediately reclaim the freed space."""
        if collapsed == self._collapsed:
            return
        self._collapsed = collapsed
        if not self._has_content:
            return
        target_width = 0 if collapsed else self.SIDEBAR_WIDTH
        if self._width_anim is not None:
            self._width_anim.stop()
            self._width_anim = None
        if not animated:
            self.setMinimumWidth(0)
            self.setMaximumWidth(target_width)
            self.setVisible(not collapsed)
            if not collapsed:
                self.setFixedWidth(self.SIDEBAR_WIDTH)
            return
        if not collapsed:
            self.setVisible(True)
        start_width = self.width() if self.isVisible() else (0 if collapsed else self.SIDEBAR_WIDTH)
        # Loosen the minimum only for the duration of the slide so the
        # layout can actually shrink us; a fully expanded sidebar is
        # re-pinned to a hard SIDEBAR_WIDTH below so the 2-column poster
        # grid inside never gets squeezed by neighbouring widgets.
        self.setMinimumWidth(0)
        anim = QPropertyAnimation(self, b'maximumWidth', self)
        anim.setDuration(220)
        anim.setStartValue(start_width)
        anim.setEndValue(target_width)
        anim.setEasingCurve(QEasingCurve.Type.InOutCubic)

        def _on_finished() -> None:
            if self._collapsed:
                self.setVisible(False)
            else:
                self.setFixedWidth(self.SIDEBAR_WIDTH)
            if self._width_anim is anim:
                self._width_anim = None
        anim.finished.connect(_on_finished)
        self._width_anim = anim
        anim.start(QPropertyAnimation.DeletionPolicy.DeleteWhenStopped)

    def _apply_locale(self, *_args) -> None:
        self._header.setText(tr('repacks.most_popular_this_week'))

    def _on_scroll_changed(self, _value: int) -> None:
        if not self._visibility_timer.isActive():
            self._visibility_timer.start()

    def _update_offscreen_cards(self) -> None:
        margin = 400
        viewport_top = self._scroll.verticalScrollBar().value() - margin
        viewport_bottom = viewport_top + self._scroll.viewport().height() + margin
        for idx, card in enumerate(self._cards):
            card_top = card.y()
            card_bottom = card_top + card.height()
            in_range = card_bottom >= viewport_top and card_top <= viewport_bottom
            was_in_range = self._card_states.get(idx)
            if in_range == was_in_range:
                continue
            self._card_states[idx] = in_range
            if in_range:
                card.reload_pixmap_if_needed()
            else:
                card.unload_pixmap()

    def load(self) -> None:
        if self._task is not None:
            return
        self._fetch()

    def refresh(self) -> None:
        self._clear_cards()
        self._task = None
        self._fetch()

    def _fetch(self) -> None:
        self._task = fetch_popular_repacks_async(self._source_key, on_done=self._on_entries_loaded, use_cache=True)

    def _on_entries_loaded(self, entries: list) -> None:
        self._task = None
        self._clear_cards()
        if not entries:
            self._has_content = False
            self.setVisible(False)
            self.content_changed.emit(False)
            return
        self._has_content = True
        if not self._collapsed:
            self.setFixedWidth(self.SIDEBAR_WIDTH)
            self.setVisible(True)
        row = 0
        col = 0
        for entry in entries:
            card = _SidebarPosterCard(entry)
            card.clicked_poster.connect(self.poster_clicked)
            self._grid_layout.addWidget(card, row, col)
            self._cards.append(card)
            if entry.poster_url:
                self._url_to_cards.setdefault(entry.poster_url, []).append(card)
                self._poster_downloader.request(entry.poster_url)
            col += 1
            if col >= 2:
                col = 0
                row += 1
        self._visibility_timer.start()
        self.content_changed.emit(True)

    def _on_poster_ready(self, url: str, path: str) -> None:
        for card in self._url_to_cards.get(url, []):
            card.set_poster_path(path)

    def _on_poster_failed(self, url: str, error: str) -> None:
        for card in self._url_to_cards.get(url, []):
            card.show_fallback_icon()

    def _clear_cards(self) -> None:
        for card in self._cards:
            self._grid_layout.removeWidget(card)
            card.deleteLater()
        self._cards.clear()
        self._url_to_cards.clear()
        self._card_states.clear()

    def shutdown(self) -> None:
        if self._width_anim is not None:
            self._width_anim.stop()
            self._width_anim = None
        try:
            self._poster_downloader.shutdown()
        except Exception:
            logger.exception('Failed to shut down popular-repacks poster downloader')

class SourceTab(QWidget):

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
        from PySide6.QtWidgets import QStackedWidget
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
        self._grid = PosterGrid()
        self._grid.poster_clicked.connect(self._show_details)
        self._grid.near_bottom.connect(self._on_near_bottom)
        grid_row_layout.addWidget(self._grid, 1)
        self._sidebar_toggle_btn = TransparentToolButton(FluentIcon.RIGHT_ARROW)
        self._sidebar_toggle_btn.setFixedSize(28, 28)
        self._sidebar_toggle_btn.setIconSize(QSize(12, 12))
        self._sidebar_toggle_btn.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        self._sidebar_toggle_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._sidebar_toggle_btn.setStyleSheet(f'''
            TransparentToolButton {{
                background-color: {_surface_tint_color(14)};
                border: 1px solid {_surface_border_color()};
                border-radius: 14px;
            }}
            TransparentToolButton:hover {{
                background-color: {_hover_tint_color()};
            }}
            TransparentToolButton:pressed {{
                background-color: {_surface_tint_color(14)};
            }}
        ''')
        self._sidebar_toggle_btn.setToolTip('Hide popular this week')
        self._sidebar_toggle_btn.setVisible(False)
        self._sidebar_toggle_btn.clicked.connect(self._on_toggle_popular_sidebar)
        toggle_wrap = QWidget()
        toggle_wrap_layout = QVBoxLayout(toggle_wrap)
        toggle_wrap_layout.setContentsMargins(0, 0, 0, 0)
        toggle_wrap_layout.addStretch(1)
        toggle_wrap_layout.addWidget(self._sidebar_toggle_btn, 0, Qt.AlignmentFlag.AlignHCenter)
        toggle_wrap_layout.addStretch(1)
        grid_row_layout.addWidget(toggle_wrap, 0)
        self._popular_sidebar = PopularRepacksSidebar(source_key)
        self._popular_sidebar.poster_clicked.connect(self._show_details)
        self._popular_sidebar.content_changed.connect(self._on_popular_content_changed)
        grid_row_layout.addWidget(self._popular_sidebar, 0)
        grid_page_layout.addWidget(grid_row, 1)
        self._stack.addWidget(grid_page)
        self._search_loading_page = QWidget()
        loading_layout = QVBoxLayout(self._search_loading_page)
        loading_layout.setContentsMargins(0, 0, 0, 0)
        loading_layout.addStretch(1)
        spinner_row = QHBoxLayout()
        spinner_row.addStretch(1)
        from qfluentwidgets import IndeterminateProgressRing
        self._search_spinner = IndeterminateProgressRing()
        self._search_spinner.setFixedSize(56, 56)
        spinner_row.addWidget(self._search_spinner)
        spinner_row.addStretch(1)
        loading_layout.addLayout(spinner_row)
        loading_layout.addStretch(1)
        self._stack.addWidget(self._search_loading_page)
        layout.addWidget(self._stack, 1)
        self._details = RepackDetailsView(manager, source_key)
        self._details.back_requested.connect(self._show_grid)
        self._stack.addWidget(self._details)
        self._latest_task = None
        self._stack.setCurrentWidget(grid_page)
        self._popular_sidebar.load()
        self._load_latest_repacks()

    def _load_latest_repacks(self) -> None:
        if self._latest_task is not None:
            return
        self._latest_task = fetch_latest_repacks_async(self._source_key, on_done=self._on_latest_repacks_loaded, use_cache=True)

    def _on_latest_repacks_loaded(self, entries: list) -> None:
        self._latest_task = None
        if self._search_query:
            return
        self._grid.set_latest_entries(entries or [])

    def shutdown(self) -> None:
        try:
            self._grid._poster_downloader.shutdown()
        except Exception:
            logger.exception('Failed to shut down grid poster downloader for %s', self._source_key)
        try:
            self._details._poster_downloader.shutdown()
        except Exception:
            logger.exception('Failed to shut down details poster downloader for %s', self._source_key)
        try:
            self._details._download_action.shutdown()
        except Exception:
            logger.exception('Failed to shut down file-list thread for %s', self._source_key)
        try:
            self._popular_sidebar.shutdown()
        except Exception:
            logger.exception('Failed to shut down popular-repacks sidebar for %s', self._source_key)
        gc.collect()

    def load_initial(self) -> None:
        if self._loaded_once:
            return
        self._loaded_once = True
        self._load_next_page(use_cache=True)

    def filter_grid(self, query: str) -> None:
        query = (query or '').strip()
        if query == self._search_query:
            return
        self._search_query = query
        if not query:
            self._restore_browse_view()
            return
        self._stack.setCurrentWidget(self._search_loading_page)
        self._search_spinner.start()
        self._run_search(query)

    def _run_search(self, query: str) -> None:
        self._set_status(f'Searching for "{query}"…')
        fetch_search_async(self._source_key, query, 1, on_done=lambda result, q=query: self._on_search_done(q, result), on_error=lambda message, q=query: self._on_search_error(q, message), use_cache=False)

    def _on_search_done(self, query: str, result) -> None:
        if query != self._search_query:
            return
        self._search_spinner.stop()
        self._grid.set_latest_section_visible(False)
        self._grid.set_entries(result.entries)
        self._stack.setCurrentWidget(self._grid_row)
        self._set_status('' if result.entries else f'No games found matching "{query}".')

    def _on_search_error(self, query: str, message: str) -> None:
        if query != self._search_query:
            return
        self._search_spinner.stop()
        if message == '__no_search__':
            self._grid.set_latest_section_visible(False)
            self._grid.filter_by_title(query)
            self._stack.setCurrentWidget(self._grid_row)
            self._set_status('')
            return
        self._stack.setCurrentWidget(self._grid_row)
        self._set_status(f'Search failed: {message}')
        logger.error('Repack search failed for %s (%r): %s', self._source_key, query, message)

    def _restore_browse_view(self) -> None:
        self._search_spinner.stop()
        self._stack.setCurrentWidget(self._grid_row)
        self._set_status('')
        self._grid.set_latest_section_visible(True)
        self._grid.clear()
        self._current_page = 0
        self._has_more = True
        self._is_loading = False
        self._loaded_once = False
        self.load_initial()

    def refresh(self) -> None:
        from src.core.repacks.base import clear_source_cache
        clear_source_cache(self._source_key)
        self._grid.clear()
        self._current_page = 0
        self._has_more = True
        self._load_next_page(use_cache=False)
        self._load_latest_repacks()
        self._popular_sidebar.refresh()

    def _on_near_bottom(self) -> None:
        if self._search_query:
            return
        if self._has_more and (not self._is_loading):
            self._load_next_page(use_cache=True)

    def _load_next_page(self, use_cache: bool) -> None:
        if self._is_loading or not self._has_more:
            return
        self._is_loading = True
        next_page = self._current_page + 1
        fetch_page_async(self._source_key, next_page, on_done=self._on_page_loaded, on_error=self._on_page_error, use_cache=use_cache)

    def _on_page_loaded(self, result) -> None:
        self._is_loading = False
        self._current_page = result.page
        self._has_more = result.has_more
        self._grid.append_entries(result.entries)
        if not result.entries and self._current_page == 1:
            self._set_status(tr('repacks.no_repacks_found'))
        else:
            self._set_status('')
        if self._has_more:
            from PySide6.QtCore import QTimer
            QTimer.singleShot(50, self._fill_viewport_if_needed)

    def _fill_viewport_if_needed(self) -> None:
        if self._is_loading or not self._has_more:
            return
        self._grid._flow_layout.activate()
        self._grid.updateGeometry()
        bar = self._grid.verticalScrollBar()
        if bar.maximum() <= 0:
            self._load_next_page(use_cache=True)

    def _on_page_error(self, message: str) -> None:
        self._is_loading = False
        self._set_status(tr('repacks.failed_to_load', message=message))
        logger.error('Repack page load failed for %s: %s', self._source_key, message)

    def _set_status(self, text: str) -> None:
        self._status_lbl.setText(text)
        self._status_lbl.setVisible(bool(text))

    def _switch_stack_animated(self, widget) -> None:
        from PySide6.QtWidgets import QGraphicsOpacityEffect
        from PySide6.QtCore import QParallelAnimationGroup, QPoint
        if self._stack.currentWidget() is widget:
            return
        old_group = getattr(self, '_stack_fade_anim', None)
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

        fade = QPropertyAnimation(effect, b'opacity', widget)
        fade.setDuration(220)
        fade.setStartValue(0.0)
        fade.setEndValue(1.0)
        fade.setEasingCurve(QEasingCurve.Type.OutCubic)

        slide = QPropertyAnimation(widget, b'pos', widget)
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

    def _on_popular_content_changed(self, has_content: bool) -> None:
        self._sidebar_toggle_btn.setVisible(has_content)
        if not has_content:
            self._sidebar_toggle_btn.setIcon(FluentIcon.RIGHT_ARROW)

    def _on_toggle_popular_sidebar(self) -> None:
        collapsing = not self._popular_sidebar.is_collapsed()
        self._popular_sidebar.set_collapsed(collapsing)
        self._sidebar_toggle_btn.setIcon(FluentIcon.LEFT_ARROW if collapsing else FluentIcon.RIGHT_ARROW)
        self._sidebar_toggle_btn.setToolTip('Show popular this week' if collapsing else 'Hide popular this week')

    def _show_details(self, entry: RepackEntry) -> None:
        self._details.show_loading(entry)
        self._switch_stack_animated(self._details)
        fetch_details_async(self._source_key, entry, on_done=self._on_details_loaded, on_error=self._on_details_error, use_cache=True)

    def _on_details_loaded(self, details) -> None:
        self._details.show_details(details)

    def _on_details_error(self, message: str) -> None:
        self._details.show_error(message)
        logger.error('Repack details load failed for %s: %s', self._source_key, message)

    def _show_grid(self) -> None:
        self._switch_stack_animated(self._grid_row)
        self._details.pause_media()
        from PySide6.QtCore import QTimer
        QTimer.singleShot(230, self._refresh_grid_after_show)

    def _refresh_grid_after_show(self) -> None:
        self._grid._flow_layout.activate()
        self._grid.updateGeometry()
        self._grid._card_geoms.clear()
        self._grid._update_offscreen_cards()

class UpcomingRepacksCard(QWidget):
    CARD_WIDTH = 560
    CARD_HEIGHT = 460

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedSize(self.CARD_WIDTH, self.CARD_HEIGHT)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setStyleSheet(f'UpcomingRepacksCard {{ background-color: {_surface_tint_color(12)}; border: 1px solid {_surface_border_color(18)}; border-radius: 8px; }}')
        layout = QVBoxLayout(self)
        layout.setContentsMargins(28, 24, 28, 24)
        layout.setSpacing(10)
        self._title_lbl = TitleLabel(tr('repacks.upcoming_repacks'))
        self._title_lbl.setStyleSheet(f'font-weight: 800; color: {palette()['primary_text']};')
        layout.addWidget(self._title_lbl)
        self._date_lbl = CaptionLabel('')
        self._date_lbl.setTextColor(_qcolor_from_palette(palette(dark=False)['muted']), _qcolor_from_palette(palette(dark=True)['muted']))
        layout.addWidget(self._date_lbl)
        self._scroll = SmoothScrollArea(self)
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        self._scroll.setStyleSheet('QScrollArea { background: transparent; border: none; }')
        self._scroll.viewport().setStyleSheet('background: transparent;')
        self._list_container = QWidget()
        self._list_container.setStyleSheet('background: transparent;')
        self._list_layout = QVBoxLayout(self._list_container)
        self._list_layout.setContentsMargins(0, 8, 0, 0)
        self._list_layout.setSpacing(6)
        self._list_layout.addStretch(1)
        self._scroll.setWidget(self._list_container)
        layout.addWidget(self._scroll, 1)
        self._empty_lbl = BodyLabel(tr('repacks.loading'))
        self._empty_lbl.setWordWrap(True)
        self._empty_lbl.setStyleSheet(f'color: {_muted_text_color()};')
        self._list_layout.insertWidget(0, self._empty_lbl)
        self._entry_labels: list[BodyLabel] = []

    def set_titles(self, title: str, date_text: str, game_titles: list[str]) -> None:
        self._title_lbl.setText(title or tr('repacks.upcoming_repacks'))
        self._date_lbl.setText(date_text)
        self._date_lbl.setVisible(bool(date_text))
        for lbl in self._entry_labels:
            self._list_layout.removeWidget(lbl)
            lbl.deleteLater()
        self._entry_labels.clear()
        game_titles = [t for t in game_titles if t and not t.strip().startswith('#')]
        if not game_titles:
            self._empty_lbl.setText(tr('repacks.no_upcoming_repacks'))
            self._empty_lbl.setVisible(True)
            return
        self._empty_lbl.setVisible(False)
        for game_title in game_titles:
            lbl = BodyLabel(f'→  {game_title}')
            lbl.setWordWrap(True)
            lbl.setStyleSheet(f'font-size: 14px; font-weight: 500; color: {palette()['primary_text']};')
            self._list_layout.insertWidget(self._list_layout.count() - 1, lbl)
            self._entry_labels.append(lbl)

    def set_error(self, message: str) -> None:
        for lbl in self._entry_labels:
            self._list_layout.removeWidget(lbl)
            lbl.deleteLater()
        self._entry_labels.clear()
        self._date_lbl.setVisible(False)
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
        from PySide6.QtWidgets import QApplication
        from PySide6.QtCore import QTimer

        def _repaint_all():
            for w in QApplication.topLevelWidgets():
                w.update()
                w.repaint()
        _repaint_all()
        QTimer.singleShot(80, _repaint_all)
        QTimer.singleShot(250, _repaint_all)

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

class RepacksPage(QWidget):

    def __init__(self, manager: DownloadManager, parent=None):
        super().__init__(parent)
        self._manager = manager
        self.setObjectName('repacksPage')
        self.setStyleSheet('background: transparent;')
        outer = QVBoxLayout(self)
        outer.setContentsMargins(24, 12, 24, 20)
        outer.setSpacing(12)
        self._pivot = Pivot()
        outer.addWidget(self._pivot)
        self._search_bar = SearchLineEdit()
        self._search_bar.setPlaceholderText(tr('repacks.search_placeholder'))
        self._search_bar.setMinimumWidth(160)
        self._search_bar.setMaximumWidth(320)
        self._search_bar.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        from PySide6.QtCore import QTimer
        self._search_debounce = QTimer(self)
        self._search_debounce.setSingleShot(True)
        self._search_debounce.setInterval(450)
        self._search_debounce.timeout.connect(lambda: self._on_search_text_changed(self._search_bar.text()))
        self._search_bar.textChanged.connect(lambda _text: self._search_debounce.start())
        self._search_bar.searchSignal.connect(self._on_search_immediate)
        self._search_bar.clearSignal.connect(lambda: self._on_search_immediate(''))
        self._donate_btn = PushButton(FluentIcon.HEART, tr('repacks.donate_to_fitgirl'))
        self._donate_btn.clicked.connect(self._on_donate_clicked)
        self._upcoming_btn = PushButton(FluentIcon.CALENDAR, tr('repacks.upcoming_repacks'))
        self._upcoming_btn.clicked.connect(self._on_upcoming_clicked)
        self._refresh_btn = TransparentToolButton(FluentIcon.SYNC)
        self._refresh_btn.setToolTip(tr('repacks.refresh_tooltip'))
        self._refresh_btn.setFixedSize(32, 32)
        self._refresh_btn.clicked.connect(self._on_refresh_clicked)
        self._discord_btn = PushButton(FluentIcon.CHAT, tr('repacks.gog_discord'))
        self._discord_btn.clicked.connect(self._on_discord_clicked)
        search_row = QHBoxLayout()
        search_row.setSpacing(8)
        search_row.addWidget(self._search_bar, 0, Qt.AlignmentFlag.AlignLeft)
        search_row.addWidget(self._donate_btn, 0, Qt.AlignmentFlag.AlignLeft)
        search_row.addWidget(self._upcoming_btn, 0, Qt.AlignmentFlag.AlignLeft)
        search_row.addWidget(self._discord_btn, 0, Qt.AlignmentFlag.AlignLeft)
        search_row.addWidget(self._refresh_btn, 0, Qt.AlignmentFlag.AlignLeft)
        search_row.addStretch(1)
        outer.addLayout(search_row)
        from PySide6.QtWidgets import QStackedWidget
        self._stack = QStackedWidget()
        outer.addWidget(self._stack, 1)
        self._tabs: dict[str, SourceTab] = {}
        self._tab_fade_anim: QPropertyAnimation | None = None
        self._current_tab_key: str | None = None
        self._add_source_tab('fitgirl', 'FitGirl Repacks')
        self._add_source_tab('gog', 'GOG Revived')
        self._pivot.currentItemChanged.connect(self._on_tab_changed)
        if self._tabs:
            first_key = next(iter(self._tabs))
            self._pivot.setCurrentItem(first_key)
            self._stack.setCurrentWidget(self._tabs[first_key])
            self._current_tab_key = first_key
            self._tabs[first_key].load_initial()
            self._update_donate_button(first_key)
            self._update_upcoming_button(first_key)
            self._update_discord_button(first_key)
        from PySide6.QtWidgets import QApplication
        app = QApplication.instance()
        if app is not None:
            app.aboutToQuit.connect(self._on_app_about_to_quit)
        qconfig.themeChanged.connect(self._on_global_theme_changed)
        register_locale_refresh(self, self._apply_locale)

    def _apply_locale(self, *_args) -> None:
        self._search_bar.setPlaceholderText(tr('repacks.search_placeholder'))
        self._upcoming_btn.setText(tr('repacks.upcoming_repacks'))
        self._refresh_btn.setToolTip(tr('repacks.refresh_tooltip'))

    def _on_global_theme_changed(self, *_args) -> None:
        self._repolish_subtree()

    def _repolish_subtree(self) -> None:
        for child in self.findChildren(QWidget):
            try:
                child.style().unpolish(child)
                child.style().polish(child)
                child.update()
            except Exception:
                pass
        self.update()

    def _on_app_about_to_quit(self) -> None:
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
        self._switch_tab_animated(key)
        self._tabs[key].load_initial()
        self._tabs[key].filter_grid(self._search_bar.text())
        self._update_donate_button(key)
        self._update_upcoming_button(key)
        self._update_discord_button(key)

    def _switch_tab_animated(self, key: str) -> None:
        """Cross-fade between source tabs. Uses a single reusable
        QGraphicsOpacityEffect that is removed as soon as the animation
        finishes, so idle rendering stays on Qt's normal fast paint path
        (no permanent extra compositing buffer, no timers left running)."""
        target = self._tabs.get(key)
        if target is None:
            return
        if self._current_tab_key == key and self._stack.currentWidget() is target:
            return
        self._current_tab_key = key
        if self._tab_fade_anim is not None:
            self._tab_fade_anim.stop()
            self._tab_fade_anim = None
        prev_widget = self._stack.currentWidget()
        if prev_widget is not None and prev_widget is not target:
            prev_widget.setGraphicsEffect(None)
        from PySide6.QtWidgets import QGraphicsOpacityEffect
        effect = QGraphicsOpacityEffect(target)
        effect.setOpacity(0.0)
        target.setGraphicsEffect(effect)
        self._stack.setCurrentWidget(target)
        anim = QPropertyAnimation(effect, b'opacity', self)
        anim.setDuration(160)
        anim.setStartValue(0.0)
        anim.setEndValue(1.0)
        anim.setEasingCurve(QEasingCurve.Type.OutCubic)

        def _cleanup() -> None:
            try:
                if _qt_is_valid(target) and target.graphicsEffect() is effect:
                    target.setGraphicsEffect(None)
            except RuntimeError:
                pass
            if self._tab_fade_anim is anim:
                self._tab_fade_anim = None
        anim.finished.connect(_cleanup)
        self._tab_fade_anim = anim
        anim.start(QPropertyAnimation.DeletionPolicy.DeleteWhenStopped)

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
        from PySide6.QtCore import QUrl
        from PySide6.QtGui import QDesktopServices
        QDesktopServices.openUrl(QUrl('https://discord.gg/qXtqrkVXaT'))

    def _on_donate_clicked(self) -> None:
        from PySide6.QtCore import QUrl
        from PySide6.QtGui import QDesktopServices
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

    def _on_search_text_changed(self, text: str) -> None:
        current_key = self._pivot.currentRouteKey()
        if current_key in self._tabs:
            self._tabs[current_key].filter_grid(text)

    def _on_search_immediate(self, text: str) -> None:
        self._search_debounce.stop()
        self._on_search_text_changed(text)

    def _add_source_tab(self, key: str, display_name: str) -> None:
        tab = SourceTab(key, display_name, self._manager, self)
        self._tabs[key] = tab
        self._stack.addWidget(tab)
        self._pivot.addItem(routeKey=key, text=display_name)