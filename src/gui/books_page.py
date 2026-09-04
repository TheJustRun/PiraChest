from __future__ import annotations
import logging
import re
import weakref
from collections import OrderedDict
from PySide6.QtCore import (
    Qt, QSize, Signal, QTimer, QRect, QEvent, QEasingCurve,
    QPropertyAnimation, QParallelAnimationGroup, QPoint, QVariantAnimation, QObject,
)
from PySide6.QtGui import QPainter, QPixmap, QColor, QPainterPath, QFontMetrics, QWheelEvent
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QGridLayout, QLabel,
    QAbstractScrollArea, QScrollArea, QStackedWidget, QSizePolicy, QGraphicsOpacityEffect,
)
from qfluentwidgets import (
    TitleLabel, BodyLabel, StrongBodyLabel, CaptionLabel, SubtitleLabel,
    PushButton, PrimaryPushButton, SearchLineEdit, CheckBox, ScrollArea,
    SmoothScrollBar, SmoothScrollArea, ImageLabel, PillPushButton,
    FluentIcon, HyperlinkButton, qconfig, isDarkTheme, themeColor,
    FlowLayout as QFlowLayout, MessageBoxBase, InfoBar, InfoBarPosition,
    Pivot, TransparentToolButton,
)

from src.core.worker import submit, cancel
from src.core.books import manager as books_manager
from src.core.books.manga import manager as manga_manager
from src.core.books.manga.manager import MangaItem as MangaEntry, MangaChapter, MangaDownloadBridge
from src.core.models import BookItem
from src.core.artwork import artwork, thumb_path as artwork_thumb_path, has_thumb as artwork_has_thumb, full_path as artwork_full_path, has_full as artwork_has_full
from src.core.theme import palette
from src.core.translations import tr, register_locale_refresh
from .repacks_page import (
    MetaField, make_tag_pill, render_description_html,
    _load_scaled_pixmap, _dominant_color, _stop_previous_movie,
    _muted_text_color, _body_text_color, _surface_tint_color,
    _center_crop_pixmap, FlowLayout,
)

logger = logging.getLogger(__name__)

_PROVIDER_LABELS = {
    'gutenberg': 'Gutenberg',
    'archive': 'Internet Archive',
    'libgen': 'Library Genesis',
}

_FORMAT_LABELS = {
    'application/epub+zip': 'EPUB',
    'application/pdf': 'PDF',
    'text/plain; charset=utf-8': 'TXT',
    'text/plain; charset=us-ascii': 'TXT',
    'text/plain': 'TXT',
}


def _book_fallback_icon() -> FluentIcon | None:
    for name in ('BOOK_SHELF', 'LIBRARY', 'EDUCATION', 'DOCUMENT', 'FOLDER', 'GAME'):
        icon = getattr(FluentIcon, name, None)
        if icon is not None:
            return icon
    return None


def _set_book_fallback_icon(label: QLabel) -> None:
    icon = _book_fallback_icon()
    if icon is not None:
        label.setPixmap(icon.icon(color=QColor(_body_text_color())).pixmap(48, 48))
    else:
        label.setPixmap(QPixmap())

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

class BookGridView(QAbstractScrollArea):

    book_clicked = Signal(object)
    near_bottom = Signal()
    page_needed = Signal(int)

    CARD_W = 150
    COVER_H = 210
    TEXT_H = 58
    SPACING = 14
    MARGIN = 12
    CELL_W = CARD_W + SPACING
    CELL_H = COVER_H + TEXT_H + SPACING
    PRELOAD_ROWS = 2
    PIXMAP_LRU_LIMIT = 150
    SCALED_LRU_LIMIT = 60
    MAX_RESIDENT_PAGES = 4
    MAX_TRACKED_PAGE_OFFSETS = 500
    _NEAR_BOTTOM_THRESHOLD_PX = 400
    _RESIZE_DEBOUNCE_MS = 60
    _REPAINT_BATCH_MS = 16
    _LIFT_HEADROOM = _CARD_LIFT_PX + 2

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFrameShape(QAbstractScrollArea.Shape.NoFrame)
        self.setStyleSheet('background: transparent; border: none;')
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.viewport().setStyleSheet('background: transparent;')
        self.viewport().setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent, False)
        self.viewport().setCursor(Qt.CursorShape.PointingHandCursor)
        self.viewport().setMouseTracking(True)
        self.setMouseTracking(True)

        self._smooth_bar = None
        try:
            bar = SmoothScrollBar(Qt.Orientation.Vertical, self)
            bar.setScrollAnimation(400, QEasingCurve.Type.OutCubic)
            self.setVerticalScrollBar(bar)
            self._smooth_bar = bar
        except Exception:
            logger.warning('SmoothScrollBar unavailable, falling back to default scrollbar')

        self._pages: "OrderedDict[int, list[BookItem]]" = OrderedDict()
        self._page_start: dict[int, int] = {}
        self._page_len: dict[int, int] = {}
        self._url_owner_page: dict[str, int] = {}
        self._known_count = 0
        self._has_more = True

        self._pix_lru: "OrderedDict[str, QPixmap]" = OrderedDict()
        self._pix_weak: "weakref.WeakValueDictionary[str, QPixmap]" = weakref.WeakValueDictionary()
        self._scaled_lru: "OrderedDict[str, QPixmap]" = OrderedDict()

        self._requested: set[str] = set()
        self._visible_urls: set[str] = set()
        self._resident_pages: set[int] = set()

        self._hit_rects: dict[int, QRect] = {}

        self._hover_idx: int | None = None
        self._hover_lift = _HoverLiftAnimator(self._on_hover_lift_changed, self)

        self._cols = 1
        self._extra_gutter = 0
        self._last_layout_n = -1
        self._last_layout_width = -1
        self._muted_color_cache: QColor | None = None
        self._placeholder_color_cache: QColor | None = None
        qconfig.themeChanged.connect(self._invalidate_paint_color_cache)

        self._repaint_timer = QTimer(self)
        self._repaint_timer.setSingleShot(True)
        self._repaint_timer.timeout.connect(self.viewport().update)

        self._resize_timer = QTimer(self)
        self._resize_timer.setSingleShot(True)
        self._resize_timer.timeout.connect(self._relayout)

        self._evict_timer = QTimer(self)
        self._evict_timer.setSingleShot(True)
        self._evict_timer.timeout.connect(self._evict_far_pages)

        self._last_scroll_value = None
        self.verticalScrollBar().valueChanged.connect(self._on_scroll)

        artwork.thumb_ready.connect(self._on_thumb_ready)

    def shutdown(self) -> None:
        try:
            artwork.thumb_ready.disconnect(self._on_thumb_ready)
        except (TypeError, RuntimeError):
            pass
        self._repaint_timer.stop()
        self._resize_timer.stop()
        self._evict_timer.stop()
        self._pix_lru.clear()
        self._pix_weak.clear()
        self._scaled_lru.clear()
        self._requested.clear()
        self._hit_rects.clear()
        self._pages.clear()
        self._url_owner_page.clear()

    def clear(self) -> None:
        self._pages.clear()
        self._page_start.clear()
        self._page_len.clear()
        self._url_owner_page.clear()
        self._pix_lru.clear()
        self._pix_weak.clear()
        self._scaled_lru.clear()
        self._requested.clear()
        self._hit_rects.clear()
        self._known_count = 0
        self._has_more = True
        self._last_layout_n = -1
        self._hover_idx = None
        self._hover_lift.value = 0.0
        self._relayout()

    def set_page(self, page_no: int, entries: list[BookItem], has_more: bool) -> None:
        self._pages[page_no] = entries
        self._pages.move_to_end(page_no)
        prev_page = page_no - 1
        if page_no == 0:
            start = 0
        elif prev_page in self._page_len:
            start = self._page_start[prev_page] + self._page_len[prev_page]
        else:
            start = self._page_start.get(page_no, self._known_count)
        self._page_start[page_no] = start
        self._page_len[page_no] = len(entries)
        for entry in entries:
            if entry.artwork_url:
                self._url_owner_page[entry.artwork_url] = page_no
        self._known_count = max(self._known_count, start + len(entries))
        self._has_more = has_more
        self._evict_far_pages()
        self._trim_page_offsets()
        self._last_layout_n = -1
        self._relayout()
        self._schedule_repaint()

    def has_page(self, page_no: int) -> bool:
        return page_no in self._pages

    def known_page_count(self) -> int:
        return (max(self._page_start) + 1) if self._page_start else 0

    def _locate(self, idx: int) -> tuple[int | None, BookItem | None]:
        for page_no, start in self._page_start.items():
            length = self._page_len.get(page_no, 0)
            if start <= idx < start + length:
                page = self._pages.get(page_no)
                if page is None:
                    return page_no, None
                self._pages.move_to_end(page_no)
                offset = idx - start
                return page_no, (page[offset] if offset < len(page) else None)
        return None, None

    def _entry_at(self, idx: int) -> BookItem | None:
        return self._locate(idx)[1]

    def _on_hover_lift_changed(self) -> None:
        self._schedule_repaint()

    def _set_hover_idx(self, idx: int | None) -> None:
        if idx == self._hover_idx:
            return
        self._hover_idx = idx
        self._hover_lift.animate_to(_CARD_LIFT_PX if idx is not None else 0.0)

    def _evict_far_pages(self) -> None:
        protected = set(self._resident_pages)
        while len(self._pages) > self.MAX_RESIDENT_PAGES:
            for page_no in self._pages:
                if page_no not in protected:
                    victim = page_no
                    break
            else:
                break
            self._evict_page(victim)

    def _trim_page_offsets(self) -> None:
        if len(self._page_start) <= self.MAX_TRACKED_PAGE_OFFSETS:
            return
        protected = {0} | set(self._resident_pages) | set(self._pages)
        removable = sorted(p for p in self._page_start if p not in protected)
        overflow = len(self._page_start) - self.MAX_TRACKED_PAGE_OFFSETS
        for page_no in removable[:overflow]:
            self._page_start.pop(page_no, None)
            self._page_len.pop(page_no, None)

    def _evict_page(self, page_no: int) -> None:
        entries = self._pages.pop(page_no, None)
        if not entries:
            return
        for entry in entries:
            url = entry.artwork_url
            if not url:
                continue
            if self._url_owner_page.get(url) != page_no:
                continue
            self._url_owner_page.pop(url, None)
            if url in self._visible_urls:
                continue
            self._pix_lru.pop(url, None)
            self._pix_weak.pop(url, None)
            if url in self._requested:
                self._requested.discard(url)

    def set_entries(self, entries: list[BookItem]) -> None:
        self.clear()
        self.set_page(0, entries, has_more=False)

    def append_entries(self, entries: list[BookItem]) -> None:
        self.set_page(self.known_page_count(), entries, has_more=True)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._resize_timer.start(self._RESIZE_DEBOUNCE_MS)

    def _relayout(self) -> None:
        n = self._known_count
        width = self.viewport().width()
        if n == self._last_layout_n and width == self._last_layout_width:
            return
        self._last_layout_n = n
        self._last_layout_width = width
        usable = max(0, width - 2 * self.MARGIN)
        cols = max(1, (usable + self.SPACING) // self.CELL_W)
        self._cols = int(cols)
        used_w = self._cols * self.CELL_W - self.SPACING
        self._extra_gutter = max(0, usable - used_w) // 2
        rows = (n + self._cols - 1) // self._cols if n else 0
        content_h = self.MARGIN * 2 + rows * self.CELL_H
        bar = self.verticalScrollBar()
        bar.setRange(0, max(0, content_h - self.viewport().height()))
        bar.setSingleStep(48)
        bar.setPageStep(max(self.CELL_H, self.viewport().height()))
        self._schedule_repaint()

    def _on_scroll(self, value: int) -> None:
        if value == self._last_scroll_value:
            return
        self._last_scroll_value = value
        self._set_hover_idx(None)
        self._schedule_repaint()
        if not self._evict_timer.isActive():
            self._evict_timer.start(250)
        bar = self.verticalScrollBar()
        if bar.maximum() - value <= self._NEAR_BOTTOM_THRESHOLD_PX:
            self.near_bottom.emit()

    def wheelEvent(self, event: QWheelEvent) -> None:
        delta = event.angleDelta().y()
        if delta == 0:
            super().wheelEvent(event)
            return
        bar = self.verticalScrollBar()
        step = max(60, int(self.CELL_H * 0.35))
        pixels = -int(delta / 120 * step)
        if hasattr(bar, 'scrollValue'):
            bar.scrollValue(pixels)
        else:
            bar.setValue(bar.value() + pixels)
        event.accept()

    def _schedule_repaint(self) -> None:
        if not self._repaint_timer.isActive():
            self._repaint_timer.start(self._REPAINT_BATCH_MS)

    def _cache_get(self, url: str) -> QPixmap | None:
        pix = self._pix_lru.get(url)
        if pix is not None:
            self._pix_lru.move_to_end(url)
            return pix
        pix = self._pix_weak.get(url)
        if pix is not None:
            self._pix_lru[url] = pix
            self._pix_lru.move_to_end(url)
            return pix
        return None

    def _cache_put(self, url: str, pix: QPixmap) -> None:
        self._pix_lru[url] = pix
        self._pix_lru.move_to_end(url)
        try:
            self._pix_weak[url] = pix
        except TypeError:
            pass
        while len(self._pix_lru) > self.PIXMAP_LRU_LIMIT:
            old_url, _ = self._pix_lru.popitem(last=False)
            self._requested.discard(old_url)

    def _ensure_thumb(self, entry: BookItem) -> QPixmap | None:
        url = entry.artwork_url
        if not url:
            return None
        pix = self._cache_get(url)
        if pix is not None:
            return pix
        if artwork_has_thumb('book', url):
            loaded = QPixmap(artwork_thumb_path('book', url))
            if not loaded.isNull():
                self._cache_put(url, loaded)
                return loaded
        if url not in self._requested:
            self._requested.add(url)
            artwork.request('book', url)
        return None

    def _scaled_cache_get(self, url: str, source_pix: QPixmap) -> QPixmap:
        cached = self._scaled_lru.get(url)
        if cached is not None:
            self._scaled_lru.move_to_end(url)
            return cached
        scaled = source_pix.scaled(
            self.CARD_W, self.COVER_H, Qt.AspectRatioMode.KeepAspectRatioByExpanding, Qt.TransformationMode.SmoothTransformation,
        )
        self._scaled_lru[url] = scaled
        while len(self._scaled_lru) > self.SCALED_LRU_LIMIT:
            self._scaled_lru.popitem(last=False)
        return scaled

    def _on_thumb_ready(self, kind: str, url: str, path: str) -> None:
        if kind != 'book':
            return
        self._requested.discard(url)
        pix = QPixmap(path)
        if pix.isNull():
            return
        self._cache_put(url, pix)
        if url in self._visible_urls:
            self._schedule_repaint()

    def paintEvent(self, event) -> None:
        painter = QPainter(self.viewport())
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        n = self._known_count
        self._hit_rects.clear()
        self._visible_urls.clear()
        new_resident_pages: set[int] = set()
        if n == 0 or self._cols <= 0:
            painter.end()
            self._resident_pages = new_resident_pages
            return

        scroll_y = self.verticalScrollBar().value()
        viewport_h = self.viewport().height()
        first_row = max(0, scroll_y // self.CELL_H - self.PRELOAD_ROWS)
        last_row = (scroll_y + viewport_h) // self.CELL_H + self.PRELOAD_ROWS
        total_rows = (n + self._cols - 1) // self._cols
        last_row = min(total_rows - 1, last_row) if total_rows else -1

        muted, placeholder = self._paint_colors()
        fm_title = QFontMetrics(self.font())
        dirty = event.rect()

        for row in range(first_row, last_row + 1):
            base_idx = row * self._cols
            if base_idx >= n:
                break
            y = self.MARGIN + row * self.CELL_H - scroll_y
            row_visible = (y - self._LIFT_HEADROOM) <= dirty.bottom() and (y + self.CELL_H) >= dirty.top()
            for col in range(self._cols):
                idx = base_idx + col
                if idx >= n:
                    break
                owner_page, entry = self._locate(idx)
                if owner_page is not None:
                    new_resident_pages.add(owner_page)
                x = self.MARGIN + self._extra_gutter + col * self.CELL_W
                if entry is None:
                    cell_rect = QRect(x, y, self.CARD_W, self.COVER_H + self.TEXT_H)
                    if row_visible:
                        self._paint_placeholder_cell(painter, x, y, placeholder)
                    if owner_page is not None:
                        self._request_missing_page(owner_page)
                    continue
                cell_rect = QRect(x, y, self.CARD_W, self.COVER_H + self.TEXT_H)
                self._hit_rects[idx] = cell_rect
                if entry.artwork_url:
                    self._visible_urls.add(entry.artwork_url)
                lift = self._hover_lift.value if idx == self._hover_idx else 0.0
                self._paint_cell(painter, entry, x, y, placeholder, muted, fm_title, lift)

        painter.end()
        self._resident_pages = new_resident_pages
        self._evict_far_pages()

    def _paint_colors(self) -> tuple[QColor, QColor]:
        if self._muted_color_cache is None:
            muted = QColor(palette().get('muted', '#888888'))
            placeholder = QColor(muted)
            placeholder.setAlpha(40)
            self._muted_color_cache = muted
            self._placeholder_color_cache = placeholder
        return (self._muted_color_cache, self._placeholder_color_cache)

    def _invalidate_paint_color_cache(self, *_args) -> None:
        self._muted_color_cache = None
        self._placeholder_color_cache = None
        self.viewport().update()

    def _paint_placeholder_cell(self, painter: QPainter, x: int, y: int, placeholder: QColor) -> None:
        cover_rect = QRect(x, y, self.CARD_W, self.COVER_H)
        path = QPainterPath()
        path.addRoundedRect(float(cover_rect.x()), float(cover_rect.y()),
                             float(cover_rect.width()), float(cover_rect.height()), 6, 6)
        painter.fillPath(path, placeholder)

    def _request_missing_page(self, page_no: int) -> None:
        self.page_needed.emit(page_no)

    def _paint_cell(self, painter: QPainter, entry: BookItem, x: int, y: int,
                     placeholder: QColor, muted: QColor, fm_title: QFontMetrics, lift: float = 0.0) -> None:
        cover_rect = QRect(x, round(y - lift), self.CARD_W, self.COVER_H)
        pix = self._ensure_thumb(entry) if entry.artwork_url else None
        if pix is not None:
            scaled = self._scaled_cache_get(entry.artwork_url, pix)
            path = QPainterPath()
            path.addRoundedRect(float(cover_rect.x()), float(cover_rect.y()),
                                 float(cover_rect.width()), float(cover_rect.height()), 6, 6)
            painter.save()
            painter.setClipPath(path)
            src_x = max(0, (scaled.width() - cover_rect.width()) // 2)
            src_y = max(0, (scaled.height() - cover_rect.height()) // 2)
            painter.drawPixmap(cover_rect.topLeft(), scaled, QRect(src_x, src_y, cover_rect.width(), cover_rect.height()))
            painter.restore()
        else:
            path = QPainterPath()
            path.addRoundedRect(float(cover_rect.x()), float(cover_rect.y()),
                                 float(cover_rect.width()), float(cover_rect.height()), 6, 6)
            painter.fillPath(path, placeholder)

        text_y = y + self.COVER_H + 4
        title = fm_title.elidedText(entry.title, Qt.TextElideMode.ElideRight, self.CARD_W - 4)
        painter.setPen(QColor(palette()['primary_text']))
        painter.drawText(x, text_y + fm_title.ascent(), title)
        if entry.author:
            painter.setPen(muted)
            author = fm_title.elidedText(entry.author, Qt.TextElideMode.ElideRight, self.CARD_W - 4)
            painter.drawText(x, text_y + fm_title.height() + fm_title.ascent(), author)
        self._paint_source_pill(painter, entry, x, text_y + fm_title.height() * 2 + 4, muted)

    def _paint_source_pill(self, painter: QPainter, entry: BookItem, x: int, y: int, muted: QColor) -> None:
        label = _PROVIDER_LABELS.get(entry.provider, (entry.provider or '').title())
        if not label:
            return
        font = painter.font()
        font.setPointSizeF(max(7.0, font.pointSizeF() - 2))
        painter.save()
        painter.setFont(font)
        fm = QFontMetrics(font)
        text_w = fm.horizontalAdvance(label)
        pad_x, pill_h = 7, fm.height() + 4
        pill_w = min(self.CARD_W, text_w + pad_x * 2)
        pill_rect = QRect(x, y, pill_w, pill_h)
        bg = QColor(muted)
        bg.setAlpha(45)
        path = QPainterPath()
        path.addRoundedRect(float(pill_rect.x()), float(pill_rect.y()),
                             float(pill_rect.width()), float(pill_rect.height()), pill_h / 2, pill_h / 2)
        painter.fillPath(path, bg)
        painter.setPen(muted)
        painter.drawText(pill_rect, Qt.AlignmentFlag.AlignCenter, label)
        painter.restore()

    def viewportEvent(self, event) -> bool:
        etype = event.type()
        if etype == QEvent.Type.MouseMove:
            pos = event.position().toPoint()
            hovered = None
            for idx, rect in self._hit_rects.items():
                cover_rect = QRect(rect.x(), rect.y(), self.CARD_W, self.COVER_H)
                if cover_rect.contains(pos):
                    hovered = idx
                    break
            self._set_hover_idx(hovered)
        elif etype == QEvent.Type.Leave:
            self._set_hover_idx(None)
        elif etype == QEvent.Type.MouseButtonRelease:
            pos = event.position().toPoint()
            for idx, rect in self._hit_rects.items():
                if rect.contains(pos):
                    entry = self._entry_at(idx)
                    if entry is not None:
                        self.book_clicked.emit(entry)
                    break
        return super().viewportEvent(event)

class BookDownloadActionWidget(QWidget):

    def __init__(self, download_bridge, parent=None):
        super().__init__(parent)
        self._download_bridge = download_bridge
        self._details: BookItem | None = None
        self._item_id: str | None = None
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        self._formats_row = QHBoxLayout()
        self._formats_row.setSpacing(8)
        layout.addLayout(self._formats_row)
        self._format_buttons: list[PushButton] = []
        self._status_lbl = CaptionLabel('')
        self._status_lbl.setStyleSheet(f'color: {_muted_text_color()};')
        layout.addWidget(self._status_lbl)
        self._current_accent_color: QColor | None = None
        qconfig.themeColorChanged.connect(self._deferred_reapply_accent_color)
        qconfig.themeChanged.connect(self._deferred_reapply_accent_color)

    _DEFAULT_BUTTON_COLOR = QColor('#00b7c3')

    def set_accent_color(self, color) -> None:
        self._current_accent_color = color
        self._reapply_accent_color()

    def _deferred_reapply_accent_color(self) -> None:
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
        qss = (
            f'PrimaryPushButton {{ background-color: rgb({color.red()}, {color.green()}, {color.blue()}); '
            f'color: {text_color}; border: none; border-radius: 8px; }} '
            f'PrimaryPushButton:hover {{ background-color: rgb({hover.red()}, {hover.green()}, {hover.blue()}); color: {text_color}; }} '
            f'PrimaryPushButton:pressed {{ background-color: rgb({pressed.red()}, {pressed.green()}, {pressed.blue()}); color: {text_color}; }}'
        )
        for btn in self._format_buttons:
            if isinstance(btn, PrimaryPushButton):
                btn.setStyleSheet(qss)

    def set_details(self, details: BookItem) -> None:
        self._details = details
        self._item_id = None
        self._status_lbl.setText('')
        self._clear_buttons()
        formats = details.formats or {}
        if not formats:
            self._status_lbl.setText(tr('books.no_downloads_available'))
            return
        read_url = formats.get('read')
        first = True
        for mime, url in formats.items():
            if mime == 'read':
                continue
            ext = _FORMAT_LABELS.get(mime, mime.split('/')[-1].split('+')[0].split(';')[0].upper())
            btn = PrimaryPushButton(ext) if first else PushButton(ext)
            btn.setFixedHeight(32)
            btn.clicked.connect(lambda _checked=False, m=mime: self._on_click(m))
            self._formats_row.addWidget(btn)
            self._format_buttons.append(btn)
            first = False
        if read_url:
            btn = PrimaryPushButton(tr('books.read_online')) if first else PushButton(tr('books.read_online'))
            btn.setFixedHeight(32)
            btn.clicked.connect(lambda: self._open_read_url(read_url))
            self._formats_row.addWidget(btn)
            self._format_buttons.append(btn)
        self._reapply_accent_color()

    def _clear_buttons(self) -> None:
        while self._formats_row.count():
            item = self._formats_row.takeAt(0)
            if item is not None and item.widget() is not None:
                item.widget().deleteLater()
        self._format_buttons.clear()

    def _open_read_url(self, url: str) -> None:
        from PySide6.QtGui import QDesktopServices
        from PySide6.QtCore import QUrl
        QDesktopServices.openUrl(QUrl(url))

    def _on_click(self, mime: str) -> None:
        if self._details is None or self._item_id is not None:
            return
        self._status_lbl.setText(tr('books.download_queued'))
        item_id = self._download_bridge.download(self._details, mime)
        if item_id is None:
            self._status_lbl.setText(tr('books.download_failed'))
            return
        self._item_id = item_id

    def shutdown(self) -> None:
        pass

class BookDetailsView(QWidget):
    back_requested = Signal()
    COVER_WIDTH = 280
    COVER_HEIGHT = 392
    SECTIONS_WIDTH = 300
    READING_WIDTH_MAX = 860

    def __init__(self, download_bridge, parent=None):
        super().__init__(parent)
        self._download_action = BookDownloadActionWidget(download_bridge)
        self._pending_cover_url: str | None = None
        self._meta_widgets: list[MetaField] = []
        self._tag_widgets: list[PillPushButton] = []

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 4, 0, 0)
        outer.setSpacing(10)

        back_btn = PushButton(FluentIcon.RETURN, tr('books.back_to_grid'))
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
        _set_book_fallback_icon(self._cover_fallback_icon_lbl)
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

        cover_col.addWidget(self._download_action, 0, Qt.AlignmentFlag.AlignTop)
        cover_col.addStretch(1)
        top_row.addWidget(cover_container, 0, Qt.AlignmentFlag.AlignTop)

        info_col_container = QWidget()
        info_col_container.setMaximumWidth(self.READING_WIDTH_MAX)
        info_col = QVBoxLayout(info_col_container)
        info_col.setContentsMargins(0, 0, 0, 0)
        info_col.setSpacing(20)

        self._title_lbl = TitleLabel('')
        self._title_lbl.setWordWrap(True)
        self._title_lbl.setStyleSheet(f'font-weight: 700; color: {palette()["primary_text"]};')
        info_col.addWidget(self._title_lbl)

        self._author_lbl = BodyLabel('')
        self._author_lbl.setStyleSheet(f'color: {_muted_text_color()}; font-weight: 400;')
        info_col.addWidget(self._author_lbl)

        self._provider_badge = PillPushButton('', info_col_container)
        self._provider_badge.setChecked(True)
        self._provider_badge.setCheckable(False)
        self._provider_badge.setCursor(Qt.CursorShape.ArrowCursor)
        badge_row = QHBoxLayout()
        badge_row.setContentsMargins(0, 0, 0, 0)
        badge_row.addWidget(self._provider_badge, 0, Qt.AlignmentFlag.AlignLeft)
        badge_row.addStretch(1)
        info_col.addLayout(badge_row)

        self._meta_grid = QGridLayout()
        self._meta_grid.setHorizontalSpacing(40)
        self._meta_grid.setVerticalSpacing(10)
        self._meta_grid.setColumnStretch(0, 0)
        self._meta_grid.setColumnStretch(1, 0)
        info_col.addLayout(self._meta_grid)

        tags_header = CaptionLabel(tr('books.subjects_genres'))
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
        desc_header.setStyleSheet(f'font-size: 16px; font-weight: 600; color: {palette()["primary_text"]};')
        self._desc_header = desc_header
        info_col.addWidget(desc_header)
        self._desc_lbl = BodyLabel('')
        self._desc_lbl.setWordWrap(True)
        self._desc_lbl.setTextFormat(Qt.TextFormat.RichText)
        self._desc_lbl.setOpenExternalLinks(True)
        self._desc_lbl.setStyleSheet(f'font-size: 15px; font-weight: 500; line-height: 170%; color: {_body_text_color()};')
        info_col.addWidget(self._desc_lbl)
        self._raw_desc_text = ''
        qconfig.themeChanged.connect(self._on_theme_changed)

        info_col.addStretch(1)
        top_row.addWidget(info_col_container, 1, Qt.AlignmentFlag.AlignTop)

        sections_container = QWidget()
        sections_container.setFixedWidth(self.SECTIONS_WIDTH)
        self._sections_col = QVBoxLayout(sections_container)
        self._sections_col.setContentsMargins(0, 0, 0, 0)
        self._sections_col.setSpacing(14)
        sections_header = StrongBodyLabel(tr('repacks.details'))
        sections_header.setStyleSheet(f'font-size: 16px; font-weight: 600; color: {palette()["primary_text"]};')
        self._sections_header = sections_header
        self._sections_col.addWidget(sections_header)
        self._detail_meta_widgets: list[MetaField] = []
        self._sections_insert_index = self._sections_col.count()
        self._site_link_btn = HyperlinkButton('', tr('repacks.view_on_website'), sections_container, FluentIcon.LINK)
        self._site_link_btn.setIconSize(QSize(14, 14))
        self._apply_site_link_style()
        qconfig.themeColorChanged.connect(self._deferred_apply_site_link_style)
        qconfig.themeChanged.connect(self._deferred_apply_site_link_style)
        self._site_link_btn.setVisible(False)
        self._sections_col.addWidget(self._site_link_btn)
        self._sections_col.addStretch(1)
        top_row.addWidget(sections_container, 0, Qt.AlignmentFlag.AlignTop)

        self._content_layout.addLayout(top_row)
        self._content_layout.addStretch(1)

        register_locale_refresh(self, self._apply_locale)

    def _apply_locale(self, *_args) -> None:
        self._back_btn.setText(tr('books.back_to_grid'))
        self._tags_header.setText(tr('books.subjects_genres'))
        self._desc_header.setText(tr('repacks.description'))
        self._sections_header.setText(tr('repacks.details'))
        self._site_link_btn.setText(tr('repacks.view_on_website'))

    def _deferred_apply_site_link_style(self) -> None:
        self._apply_site_link_style()
        QTimer.singleShot(0, self._apply_site_link_style)

    def _apply_site_link_style(self) -> None:
        color = themeColor()
        base_alpha = 55 if isDarkTheme() else 40
        hover_alpha = 85 if isDarkTheme() else 65
        text_color = palette()['primary_text']
        self._site_link_btn.setStyleSheet(
            f'HyperlinkButton {{ background-color: rgba({color.red()}, {color.green()}, {color.blue()}, {base_alpha}); '
            f'border-radius: 6px; padding: 6px 10px 6px 30px; font-weight: 600; color: {text_color}; }} '
            f'HyperlinkButton:hover {{ background-color: rgba({color.red()}, {color.green()}, {color.blue()}, {hover_alpha}); color: {text_color}; }}'
        )
        self._site_link_btn.setIcon(FluentIcon.LINK.icon(color=QColor(text_color)))

    def _on_theme_changed(self, *_args) -> None:
        self._refresh_cover_fallback_theme()

    def _refresh_cover_fallback_theme(self) -> None:
        self._cover_fallback.setStyleSheet(f'background-color: {_surface_tint_color(14)}; border-radius: 10px;')
        _set_book_fallback_icon(self._cover_fallback_icon_lbl)
        self._cover_fallback_title_lbl.setStyleSheet(f'background: transparent; font-size: 13px; color: {palette()["primary_text"]};')

    def show_loading(self, entry: BookItem) -> None:
        self._title_lbl.setText(entry.title)
        self._author_lbl.setText(entry.author or '')
        self._author_lbl.setVisible(bool(entry.author))
        self._provider_badge.setText(_PROVIDER_LABELS.get(entry.provider, (entry.provider or '').title()))
        self._provider_badge.setVisible(bool(entry.provider))
        self._raw_desc_text = ''
        self._desc_lbl.setText(tr('books.loading_details'))
        self._clear_meta()
        self._clear_tags()
        self._site_link_btn.setVisible(False)
        self._download_action.set_accent_color(None)
        if entry.artwork_url:
            self._cover_lbl.setVisible(True)
            self._cover_fallback.setVisible(False)
            self._pending_cover_url = entry.artwork_url
            if artwork_has_full('book', entry.artwork_url):
                self._set_cover_path(artwork_full_path('book', entry.artwork_url))
            else:
                artwork.request('book', entry.artwork_url, want_full=True)
        else:
            self._cover_lbl.setVisible(False)
            self._show_cover_fallback(entry.title)

    def show_details(self, details: BookItem) -> None:
        self._download_action.set_details(details)
        self._title_lbl.setText(details.title)
        self._author_lbl.setText(details.author or '')
        self._author_lbl.setVisible(bool(details.author))
        self._provider_badge.setText(_PROVIDER_LABELS.get(details.provider, (details.provider or '').title()))
        self._provider_badge.setVisible(bool(details.provider))
        self._raw_desc_text = details.description or ''
        self._desc_lbl.setText(render_description_html(self._raw_desc_text) or tr('books.no_description'))

        self._clear_meta()
        self._clear_tags()

        row = 0
        col = 0
        meta_fields = [
            ('publisher', tr('books.publisher'), getattr(details, 'publisher', None)),
            ('publish_date', tr('books.publish_date'), getattr(details, 'publish_date', None) or details.year),
            ('isbn', tr('books.isbn'), getattr(details, 'isbn', None)),
            ('language', tr('books.language'), details.language),
            ('page_count', tr('books.page_count'), str(getattr(details, 'page_count', '') or '') or None),
        ]
        for _key, label, value in meta_fields:
            if not value:
                continue
            field = MetaField(label, str(value))
            span = 2 if len(str(value)) > 28 else 1
            if span == 2 and col == 1:
                row += 1
                col = 0
            self._meta_grid.addWidget(field, row, col, 1, span)
            self._meta_widgets.append(field)
            col += span
            if col >= 2:
                col = 0
                row += 1

        subjects = list(details.subjects or [])[:20]
        self._tags_header.setVisible(bool(subjects))
        self._tags_container.setVisible(bool(subjects))
        for subject in subjects:
            pill = make_tag_pill(subject, self._tags_container)
            pill.adjustSize()
            self._tags_layout.addWidget(pill)
            self._tag_widgets.append(pill)
        if subjects:
            QTimer.singleShot(0, self._tags_layout.update)

        read_url = (details.formats or {}).get('read')
        self._site_link_btn.setVisible(bool(read_url))
        if read_url:
            self._site_link_btn.setUrl(read_url)

        cover_url = details.artwork_url
        if cover_url:
            self._cover_lbl.setVisible(True)
            self._cover_fallback.setVisible(False)
            self._pending_cover_url = cover_url
            if artwork_has_full('book', cover_url):
                self._set_cover_path(artwork_full_path('book', cover_url))
            else:
                artwork.request('book', cover_url, want_full=True)
        else:
            self._cover_lbl.setVisible(False)
            self._show_cover_fallback(details.title)

    def show_error(self, message: str) -> None:
        self._clear_meta()
        self._clear_tags()
        self._desc_lbl.setText(tr('books.failed_to_load'))

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

    def on_cover_ready(self, kind: str, url: str, path: str) -> None:
        if kind != 'book' or url != self._pending_cover_url:
            return
        self._cover_lbl.setVisible(True)
        self._cover_fallback.setVisible(False)
        self._set_cover_path(path)

    def on_cover_failed(self, kind: str, url: str, error: str) -> None:
        if kind != 'book' or url != self._pending_cover_url:
            return
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
            logger.warning('Failed to load book cover image: %s', path)

    def release_full_cover(self) -> None:
        _stop_previous_movie(self._cover_lbl)
        self._cover_lbl.setImage(None)
        self._cover_lbl.setFixedSize(self.COVER_WIDTH, self.COVER_HEIGHT)


class BookSourceFilterDialog(MessageBoxBase):

    def __init__(self, all_sources: list[str], selected_sources: list[str], parent=None):
        super().__init__(parent)
        self.titleLabel = SubtitleLabel(tr('books.filter_sources'), self)
        self.viewLayout.addWidget(self.titleLabel)
        scroll = ScrollArea(self)
        scroll.setFixedHeight(min(220, 44 * max(1, len(all_sources)) + 16))
        scroll.setWidgetResizable(True)
        scroll.setStyleSheet('background: transparent; border: none;')
        container = QWidget(scroll)
        container.setStyleSheet('background: transparent;')
        col = QVBoxLayout(container)
        col.setSpacing(4)
        self._checks: dict[str, CheckBox] = {}
        for source in all_sources:
            label = _PROVIDER_LABELS.get(source, source.title())
            cb = CheckBox(label, container)
            cb.setChecked(source in selected_sources)
            self._checks[source] = cb
            col.addWidget(cb)
        col.addStretch(1)
        scroll.setWidget(container)
        self.viewLayout.addWidget(scroll)
        self.yesButton.setText(tr('books.apply'))
        self.cancelButton.setText(tr('books.cancel'))
        self.widget.setMinimumWidth(320)

    def selected_sources(self) -> list[str]:
        return [source for source, cb in self._checks.items() if cb.isChecked()]


class MangaCoverLabel(QLabel):
    """Small helper label that lazily loads a MangaDex cover via the shared artwork cache."""

    COVER_W = 150
    COVER_H = 210

    _pixmap_cache: "OrderedDict[str, QPixmap]" = OrderedDict()
    _PIXMAP_CACHE_LIMIT = 256

    def __init__(self, url: str, parent=None):
        super().__init__(parent)
        self.setFixedSize(self.COVER_W, self.COVER_H)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setStyleSheet(f'background-color: {_surface_tint_color(14)}; border-radius: 8px;')
        self._url = url
        self._connected = False
        cached = self._cache_get(url) if url else None
        if cached is not None and not cached.isNull():
            self.setPixmap(cached)
        elif url:
            self._set_placeholder()
            artwork.thumb_ready.connect(self._on_thumb_ready)
            artwork.failed.connect(self._on_failed)
            self._connected = True
            if artwork_has_thumb('manga', url):
                self._apply_path(artwork_thumb_path('manga', url))
            else:
                artwork.request('manga', url)
        else:
            self._set_placeholder()

    @classmethod
    def _cache_get(cls, url: str) -> QPixmap | None:
        pix = cls._pixmap_cache.get(url)
        if pix is not None:
            cls._pixmap_cache.move_to_end(url)
        return pix

    @classmethod
    def _cache_put(cls, url: str, pix: QPixmap) -> None:
        cls._pixmap_cache[url] = pix
        cls._pixmap_cache.move_to_end(url)
        while len(cls._pixmap_cache) > cls._PIXMAP_CACHE_LIMIT:
            cls._pixmap_cache.popitem(last=False)

    def _set_placeholder(self) -> None:
        icon = _book_fallback_icon()
        if icon is not None:
            self.setPixmap(icon.icon(color=QColor(_muted_text_color())).pixmap(48, 48))
        else:
            self.setPixmap(QPixmap())

    def _apply_path(self, path: str) -> None:
        pix = QPixmap(path)
        if pix.isNull():
            self._set_placeholder()
            return
        scale = max(self.COVER_W / max(pix.width(), 1), self.COVER_H / max(pix.height(), 1))
        scaled = pix.scaled(
            max(1, round(pix.width() * scale)), max(1, round(pix.height() * scale)),
            Qt.AspectRatioMode.KeepAspectRatioByExpanding, Qt.TransformationMode.SmoothTransformation,
        )
        cropped = _center_crop_pixmap(scaled, self.COVER_W, self.COVER_H)
        self._cache_put(self._url, cropped)
        self.setPixmap(cropped)

    def _on_thumb_ready(self, kind: str, url: str, path: str) -> None:
        if kind != 'manga' or url != self._url:
            return
        self._apply_path(path)

    def _on_failed(self, kind: str, url: str, _error: str) -> None:
        if kind != 'manga' or url != self._url:
            return
        self._set_placeholder()

    def shutdown(self) -> None:
        if self._connected:
            try:
                artwork.thumb_ready.disconnect(self._on_thumb_ready)
                artwork.failed.disconnect(self._on_failed)
            except (TypeError, RuntimeError):
                pass
            self._connected = False



class MangaCard(QWidget):
    """A single cover + title tile in the manga grid."""

    clicked = Signal(object)

    def __init__(self, item: MangaEntry, parent=None):
        super().__init__(parent)
        self._item = item
        self.setFixedWidth(MangaCoverLabel.COVER_W)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)
        self._cover = MangaCoverLabel(item.cover_url, self)
        layout.addWidget(self._cover)
        title_lbl = CaptionLabel(item.title, self)
        title_lbl.setWordWrap(True)
        title_lbl.setFixedWidth(MangaCoverLabel.COVER_W)
        title_lbl.setMaximumHeight(34)
        layout.addWidget(title_lbl)

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit(self._item)
        super().mousePressEvent(event)

    def shutdown(self) -> None:
        self._cover.shutdown()


_MANGA_DESC_CUTOFF_RE = re.compile(r'\n\s*-{3,}\s*\n')
_MANGA_DESC_LINKS_RE = re.compile(r'(?im)^\s*\**links?:?\**\s*$.*', re.DOTALL)
_MANGA_MD_LINK_RE = re.compile(r'\[([^\]]+)\]\([^)]*\)')
_MANGA_BARE_URL_RE = re.compile(r'https?://\S+')


def _clean_manga_description(text: str) -> str:
    if not text:
        return ''
    match = _MANGA_DESC_CUTOFF_RE.search(text)
    if match:
        text = text[:match.start()]
    text = _MANGA_DESC_LINKS_RE.sub('', text)
    text = _MANGA_MD_LINK_RE.sub(lambda m: m.group(1), text)
    text = _MANGA_BARE_URL_RE.sub('', text)
    return text.strip()


class MangaGridView(ScrollArea):
    """Simple flow-layout grid of manga covers, auto-paginated by scroll position."""

    manga_clicked = Signal(object)
    near_bottom = Signal()
    near_top = Signal()
    _NEAR_BOTTOM_THRESHOLD_PX = 400

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWidgetResizable(True)
        self.setFrameShape(QScrollArea.Shape.NoFrame)
        self.setStyleSheet('background: transparent; border: none;')
        self.viewport().setStyleSheet('background: transparent;')

        self._container = QWidget()
        self._container.setStyleSheet('background: transparent;')
        self._flow = FlowLayout(self._container, margin=0, spacing=16)
        self.setWidget(self._container)

        self._cards: list[MangaCard] = []

        self._empty_lbl = BodyLabel(tr('books.manga_no_results'), self._container)
        self._empty_lbl.setStyleSheet(f'color: {_muted_text_color()};')
        self._empty_lbl.setVisible(False)
        self._flow.addWidget(self._empty_lbl)

        self.verticalScrollBar().valueChanged.connect(self._on_scroll_value_changed)

    def _on_scroll_value_changed(self, value: int) -> None:
        bar = self.verticalScrollBar()
        if bar.maximum() - value <= self._NEAR_BOTTOM_THRESHOLD_PX:
            self.near_bottom.emit()
        if value <= 0:
            self.near_top.emit()

    def clear(self) -> None:
        for card in self._cards:
            card.shutdown()
            self._flow.removeWidget(card)
            card.deleteLater()
        self._cards.clear()
        self._empty_lbl.setVisible(False)

    def add_entries(self, entries: list[MangaEntry]) -> None:
        self._flow.removeWidget(self._empty_lbl)
        for entry in entries:
            card = MangaCard(entry, self._container)
            card.clicked.connect(self.manga_clicked.emit)
            self._flow.addWidget(card)
            self._cards.append(card)
        self._flow.addWidget(self._empty_lbl)
        self._empty_lbl.setVisible(not self._cards)

    def set_has_more(self, has_more: bool, loading: bool = False) -> None:
        pass


class MangaChapterSelectDialog(MessageBoxBase):

    _WARN_THRESHOLD = 20

    def __init__(self, chapters: list[MangaChapter], parent=None):
        super().__init__(parent)
        self._checks: list[tuple[CheckBox, MangaChapter]] = []
        self.titleLabel = SubtitleLabel(tr('books.manga_choose_chapters'), self)
        self.viewLayout.addWidget(self.titleLabel)

        select_row = QHBoxLayout()
        self._select_all_btn = PushButton(tr('books.manga_select_all'), self)
        self._select_all_btn.clicked.connect(self._toggle_select_all)
        select_row.addWidget(self._select_all_btn)
        select_row.addStretch(1)
        self.viewLayout.addLayout(select_row)

        self._warning_lbl = CaptionLabel(tr('books.manga_bulk_download_warning'), self)
        self._warning_lbl.setWordWrap(True)
        self._warning_lbl.setStyleSheet('color: #d68a1f;')
        self._warning_lbl.setVisible(False)
        self.viewLayout.addWidget(self._warning_lbl)

        scroll = ScrollArea(self)
        scroll.setFixedHeight(min(360, 36 * max(1, len(chapters)) + 16))
        scroll.setWidgetResizable(True)
        scroll.setStyleSheet('background: transparent; border: none;')
        container = QWidget(scroll)
        container.setStyleSheet('background: transparent;')
        col = QVBoxLayout(container)
        col.setSpacing(4)
        for chapter in chapters:
            label = chapter.label
            if chapter.scanlation_group:
                label = f'{label}  ·  {chapter.scanlation_group}'
            cb = CheckBox(label, container)
            cb.setChecked(True)
            cb.stateChanged.connect(self._update_warning)
            self._checks.append((cb, chapter))
            col.addWidget(cb)
        col.addStretch(1)
        scroll.setWidget(container)
        self.viewLayout.addWidget(scroll)

        self.yesButton.setText(tr('books.manga_download_selected'))
        self.cancelButton.setText(tr('books.cancel'))
        self.widget.setMinimumWidth(360)
        self._update_warning()

    def _toggle_select_all(self) -> None:
        any_unchecked = any(not cb.isChecked() for cb, _ in self._checks)
        for cb, _ in self._checks:
            cb.setChecked(any_unchecked)

    def _update_warning(self, *_args) -> None:
        selected_count = sum(1 for cb, _ in self._checks if cb.isChecked())
        self._warning_lbl.setVisible(selected_count > self._WARN_THRESHOLD)

    def selected_chapters(self) -> list[MangaChapter]:
        return [chapter for cb, chapter in self._checks if cb.isChecked()]


class MangaDownloadActionWidget(QWidget):

    _DEFAULT_BUTTON_COLOR = QColor('#00b7c3')

    def __init__(self, download_bridge: MangaDownloadBridge, parent=None):
        super().__init__(parent)
        self._download_bridge = download_bridge
        self._entry: MangaEntry | None = None
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)
        self._button = PrimaryPushButton(tr('books.manga_download_button'), self)
        self._button.setFixedHeight(32)
        self._button.clicked.connect(self._on_click)
        layout.addWidget(self._button)
        self._current_accent_color: QColor | None = None
        qconfig.themeColorChanged.connect(self._deferred_reapply_accent_color)
        qconfig.themeChanged.connect(self._deferred_reapply_accent_color)

    def set_accent_color(self, color) -> None:
        self._current_accent_color = color
        self._reapply_accent_color()

    def _deferred_reapply_accent_color(self) -> None:
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
        self._button.setStyleSheet(
            f'PrimaryPushButton {{ background-color: rgb({color.red()}, {color.green()}, {color.blue()}); '
            f'color: {text_color}; border: none; border-radius: 8px; }} '
            f'PrimaryPushButton:hover {{ background-color: rgb({hover.red()}, {hover.green()}, {hover.blue()}); color: {text_color}; }} '
            f'PrimaryPushButton:pressed {{ background-color: rgb({pressed.red()}, {pressed.green()}, {pressed.blue()}); color: {text_color}; }}'
        )

    def set_entry(self, entry: MangaEntry | None) -> None:
        self._entry = entry
        downloadable = bool(entry and any(c.is_downloadable for c in entry.chapters))
        self.setVisible(downloadable)

    def _on_click(self) -> None:
        if self._entry is None:
            return
        downloadable = [c for c in self._entry.chapters if c.is_downloadable]
        if not downloadable:
            return
        dialog = MangaChapterSelectDialog(downloadable, parent=self.window())
        if not dialog.exec():
            return
        for chapter in dialog.selected_chapters():
            try:
                self._download_bridge.download_chapter(self._entry.title, chapter)
            except Exception as exc:
                logger.warning('Failed to queue manga chapter download: %s', exc)

    def shutdown(self) -> None:
        pass


class MangaDetailsView(QWidget):

    back_requested = Signal()

    COVER_WIDTH = 220
    COVER_HEIGHT = 310

    def __init__(self, download_bridge: MangaDownloadBridge, parent=None):
        super().__init__(parent)
        self.setStyleSheet('background: transparent;')
        self._download_bridge = download_bridge
        self._current_manga_title = ''
        self._cover_url = ''
        artwork.full_ready.connect(self._on_cover_ready)
        artwork.failed.connect(self._on_cover_failed)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 4, 0, 0)
        outer.setSpacing(10)

        back_btn = PushButton(FluentIcon.RETURN, tr('books.back_to_grid'))
        back_btn.setFixedHeight(32)
        back_btn.clicked.connect(self.back_requested.emit)
        outer.addWidget(back_btn, 0, Qt.AlignmentFlag.AlignLeft)

        self._scroll = SmoothScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        self._scroll.setStyleSheet('background: transparent; border: none;')
        outer.addWidget(self._scroll, 1)

        content = QWidget()
        self._scroll.setWidget(content)
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(4, 12, 24, 40)
        content_layout.setSpacing(24)

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

        self._download_action = MangaDownloadActionWidget(download_bridge, cover_container)
        cover_col.addWidget(self._download_action, 0, Qt.AlignmentFlag.AlignTop)

        self._open_btn = HyperlinkButton('', tr('books.manga_view_on_mangadex'), cover_container)
        self._open_btn.setFixedHeight(32)
        self._apply_open_btn_style()
        qconfig.themeColorChanged.connect(self._deferred_apply_open_btn_style)
        qconfig.themeChanged.connect(self._deferred_apply_open_btn_style)
        cover_col.addWidget(self._open_btn, 0)
        cover_col.addStretch(1)
        top_row.addWidget(cover_container, 0, Qt.AlignmentFlag.AlignTop)

        info_col_container = QWidget()
        info_col = QVBoxLayout(info_col_container)
        info_col.setContentsMargins(0, 0, 0, 0)
        info_col.setSpacing(16)

        self._title_lbl = TitleLabel('')
        self._title_lbl.setWordWrap(True)
        self._title_lbl.setStyleSheet(f'font-weight: 700; color: {palette()["primary_text"]};')
        info_col.addWidget(self._title_lbl)

        self._meta_grid = QGridLayout()
        self._meta_grid.setHorizontalSpacing(40)
        self._meta_grid.setVerticalSpacing(10)
        info_col.addLayout(self._meta_grid)

        tags_header = CaptionLabel(tr('books.subjects_genres'))
        tags_header.setStyleSheet(f'font-weight: 400; color: {_muted_text_color()};')
        info_col.addWidget(tags_header)
        self._tags_container = QWidget()
        self._tags_layout = QFlowLayout(self._tags_container, needAni=False, isTight=True)
        self._tags_layout.setContentsMargins(0, 0, 0, 0)
        self._tags_layout.setHorizontalSpacing(8)
        self._tags_layout.setVerticalSpacing(8)
        info_col.addWidget(self._tags_container)
        self._tag_widgets: list[QWidget] = []

        desc_header = StrongBodyLabel(tr('repacks.description'))
        desc_header.setStyleSheet(f'font-size: 16px; font-weight: 600; color: {palette()["primary_text"]};')
        info_col.addWidget(desc_header)
        self._desc_lbl = BodyLabel('')
        self._desc_lbl.setWordWrap(True)
        self._desc_lbl.setTextFormat(Qt.TextFormat.RichText)
        self._desc_lbl.setOpenExternalLinks(True)
        self._desc_lbl.setStyleSheet(f'font-size: 15px; font-weight: 500; line-height: 170%; color: {_body_text_color()};')
        info_col.addWidget(self._desc_lbl)
        info_col.addStretch(1)
        top_row.addWidget(info_col_container, 1, Qt.AlignmentFlag.AlignTop)
        content_layout.addLayout(top_row)

        chapters_header = StrongBodyLabel(tr('books.manga_chapters'))
        chapters_header.setStyleSheet(f'font-size: 16px; font-weight: 600; color: {palette()["primary_text"]};')
        content_layout.addWidget(chapters_header)
        self._chapters_container = QWidget()
        self._chapters_layout = QVBoxLayout(self._chapters_container)
        self._chapters_layout.setContentsMargins(0, 0, 0, 0)
        self._chapters_layout.setSpacing(6)
        content_layout.addWidget(self._chapters_container)
        content_layout.addStretch(1)

        self._loading_lbl = BodyLabel(tr('books.loading_details'), self)
        self._loading_lbl.setStyleSheet(f'color: {_muted_text_color()};')
        content_layout.addWidget(self._loading_lbl)

    def _deferred_apply_open_btn_style(self) -> None:
        self._apply_open_btn_style()
        QTimer.singleShot(0, self._apply_open_btn_style)

    def _apply_open_btn_style(self) -> None:
        color = themeColor()
        base_alpha = 55 if isDarkTheme() else 40
        hover_alpha = 85 if isDarkTheme() else 65
        text_color = palette()['primary_text']
        self._open_btn.setStyleSheet(
            f'HyperlinkButton {{ background-color: rgba({color.red()}, {color.green()}, {color.blue()}, {base_alpha}); '
            f'border-radius: 8px; padding: 6px 16px; font-weight: 600; color: {text_color}; }} '
            f'HyperlinkButton:hover {{ background-color: rgba({color.red()}, {color.green()}, {color.blue()}, {hover_alpha}); color: {text_color}; }}'
        )

    def _chapter_button_qss(self) -> tuple[str, str]:
        color = themeColor()
        base_alpha = 55 if isDarkTheme() else 40
        hover_alpha = 85 if isDarkTheme() else 65
        text_color = palette()['primary_text']
        read_qss = (
            f'HyperlinkButton {{ background-color: rgba({color.red()}, {color.green()}, {color.blue()}, {base_alpha}); '
            f'border-radius: 6px; padding: 4px 12px; font-weight: 600; color: {text_color}; }} '
            f'HyperlinkButton:hover {{ background-color: rgba({color.red()}, {color.green()}, {color.blue()}, {hover_alpha}); color: {text_color}; }}'
        )
        neutral = QColor(_muted_text_color())
        neutral_base_alpha = 45 if isDarkTheme() else 35
        neutral_hover_alpha = 75 if isDarkTheme() else 55
        download_qss = (
            f'TransparentToolButton {{ background-color: rgba({neutral.red()}, {neutral.green()}, {neutral.blue()}, {neutral_base_alpha}); border-radius: 6px; }} '
            f'TransparentToolButton:hover {{ background-color: rgba({neutral.red()}, {neutral.green()}, {neutral.blue()}, {neutral_hover_alpha}); }}'
        )
        return read_qss, download_qss

    def show_loading(self, entry: MangaEntry) -> None:
        self._title_lbl.setText(entry.title)
        self._cover_lbl.setImage(QPixmap())
        self._desc_lbl.setText('')
        self._clear_meta()
        self._clear_tags()
        self._clear_chapters()
        self._open_btn.setUrl(entry.web_url)
        self._download_action.set_accent_color(None)
        self._download_action.set_entry(None)
        self._loading_lbl.setVisible(True)
        cover_url = entry.cover_url_full or entry.cover_url
        self._cover_url = cover_url
        if not cover_url:
            return
        if artwork_has_full('manga', cover_url):
            self._apply_cover_path(artwork_full_path('manga', cover_url))
        else:
            artwork.request('manga', cover_url, want_full=True)

    def _on_cover_ready(self, kind: str, url: str, path: str) -> None:
        if kind != 'manga' or url != self._cover_url:
            return
        self._apply_cover_path(path)

    def _on_cover_failed(self, kind: str, url: str, _error: str) -> None:
        if kind != 'manga' or url != self._cover_url:
            return

    def _apply_cover_path(self, path: str) -> None:
        pix = _load_scaled_pixmap(path, self.COVER_WIDTH, self.COVER_HEIGHT)
        if pix is None or pix.isNull():
            return
        self._cover_lbl.setImage(pix)
        self._cover_lbl.setFixedSize(self.COVER_WIDTH, self.COVER_HEIGHT)
        self._download_action.set_accent_color(_dominant_color(path))

    def show_details(self, entry: MangaEntry) -> None:
        self._loading_lbl.setVisible(False)
        self._title_lbl.setText(entry.title)
        self._current_manga_title = entry.title
        self._open_btn.setUrl(entry.web_url)
        self._download_action.set_entry(entry)
        self._desc_lbl.setText(render_description_html(_clean_manga_description(entry.description)) or tr('books.no_description'))

        self._clear_meta()
        row = 0
        if entry.status:
            self._meta_grid.addWidget(MetaField(tr('books.manga_status'), entry.status), 0, row)
            row += 1
        if entry.year:
            self._meta_grid.addWidget(MetaField(tr('books.manga_year'), str(entry.year)), 0, row)
            row += 1
        if entry.content_rating:
            self._meta_grid.addWidget(MetaField(tr('books.manga_content_rating'), entry.content_rating), 0, row)
            row += 1

        self._clear_tags()
        seen_tags: set[str] = set()
        for tag in entry.tags:
            key = tag.strip().lower()
            if not key or key in seen_tags:
                continue
            seen_tags.add(key)
            pill = make_tag_pill(tag, self._tags_container)
            pill.adjustSize()
            self._tags_layout.addWidget(pill)
            self._tag_widgets.append(pill)
        if self._tag_widgets:
            QTimer.singleShot(0, self._tags_layout.update)

        self._clear_chapters()
        if not entry.chapters:
            empty_lbl = CaptionLabel(tr('books.manga_no_chapters'), self._chapters_container)
            empty_lbl.setStyleSheet(f'color: {_muted_text_color()};')
            self._chapters_layout.addWidget(empty_lbl)
        else:
            for chapter in entry.chapters:
                self._chapters_layout.addWidget(self._build_chapter_row(entry.title, chapter))

    def _build_chapter_row(self, manga_title: str, chapter: MangaChapter) -> QWidget:
        row = QWidget(self._chapters_container)
        row_layout = QHBoxLayout(row)
        row_layout.setContentsMargins(0, 0, 0, 0)
        row_layout.setSpacing(12)
        label_text = chapter.label
        if chapter.scanlation_group:
            label_text = f'{label_text}  ·  {chapter.scanlation_group}'
        lbl = BodyLabel(label_text, row)
        lbl.setStyleSheet(f'color: {_body_text_color()};')
        row_layout.addWidget(lbl, 1)

        read_btn = HyperlinkButton(chapter.web_url, tr('books.manga_read'), row)
        row_layout.addWidget(read_btn, 0)

        read_qss, download_qss = self._chapter_button_qss()
        read_btn.setStyleSheet(read_qss)

        if chapter.is_downloadable:
            status_lbl = CaptionLabel('', row)
            status_lbl.setStyleSheet(f'color: {_muted_text_color()};')
            row_layout.addWidget(status_lbl, 0)

            download_btn = TransparentToolButton(FluentIcon.DOWNLOAD, row)
            download_btn.setFixedSize(28, 28)
            download_btn.setStyleSheet(download_qss)
            download_btn.setToolTip(tr('books.manga_download'))
            download_btn.clicked.connect(
                lambda _checked=False, c=chapter, s=status_lbl, b=download_btn: self._start_chapter_download(manga_title, c, s, b)
            )
            row_layout.addWidget(download_btn, 0, Qt.AlignmentFlag.AlignRight)
        return row

    def _start_chapter_download(self, manga_title: str, chapter: MangaChapter, status_lbl: QLabel, btn: TransparentToolButton) -> None:
        btn.setEnabled(False)
        try:
            self._download_bridge.download_chapter(manga_title, chapter)
        except Exception as exc:
            logger.warning('Failed to queue manga chapter download: %s', exc)
            status_lbl.setText(tr('books.manga_download_failed'))
            btn.setEnabled(True)
            return
        status_lbl.setText(tr('books.manga_downloading'))
        btn.setEnabled(True)

    def show_error(self, _message: str) -> None:
        self._loading_lbl.setVisible(False)
        self._desc_lbl.setText(tr('books.failed_to_load'))

    def _clear_meta(self) -> None:
        while self._meta_grid.count():
            item = self._meta_grid.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()

    def _clear_tags(self) -> None:
        for tag in self._tag_widgets:
            self._tags_layout.removeWidget(tag)
            tag.deleteLater()
        self._tag_widgets.clear()

    def _clear_chapters(self) -> None:
        while self._chapters_layout.count():
            item = self._chapters_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()

    def shutdown(self) -> None:
        try:
            artwork.full_ready.disconnect(self._on_cover_ready)
            artwork.failed.disconnect(self._on_cover_failed)
        except (TypeError, RuntimeError):
            pass


class MangaBrowserTab(QWidget):
    """Browse/search MangaDex, backed by https://api.mangadex.org."""

    def __init__(self, download_manager, parent=None, manga_download_bridge=None):
        super().__init__(parent)
        self.setStyleSheet('background: transparent;')
        self._download_bridge = manga_download_bridge or MangaDownloadBridge(download_manager, self)
        self._query = ''
        self._page = 1
        self._has_more = True
        self._loading = False
        self._task_id: str | None = None
        self._details_task_id: str | None = None
        self._details_cache: "OrderedDict[str, MangaEntry]" = OrderedDict()
        self._current_details_key: str | None = None

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(14)

        header_row = QHBoxLayout()
        self._search_edit = SearchLineEdit(self)
        self._search_edit.setMinimumWidth(140)
        self._search_edit.setMaximumWidth(280)
        self._search_edit.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self._search_edit.setPlaceholderText(tr('books.manga_search_placeholder'))
        self._search_edit.returnPressed.connect(self._on_search)
        self._search_edit.searchSignal.connect(self._on_search)
        self._search_edit.clearSignal.connect(self._on_search_cleared)
        header_row.addWidget(self._search_edit)
        header_row.addStretch(1)
        outer.addLayout(header_row)

        self._stack = QStackedWidget()
        outer.addWidget(self._stack, 1)

        self._grid = MangaGridView(self)
        self._grid.manga_clicked.connect(self._show_details)
        self._grid.near_bottom.connect(self._on_near_bottom)
        self._grid.near_top.connect(self._on_near_top)
        self._stack.addWidget(self._grid)

        self._details = MangaDetailsView(self._download_bridge, self)
        self._details.back_requested.connect(self._show_grid)
        self._stack.addWidget(self._details)

        self._stack.setCurrentWidget(self._grid)
        self._load_page(reset=True)

    def _on_search(self) -> None:
        query = self._search_edit.text().strip()
        if query == self._query:
            return
        self._query = query
        self._load_page(reset=True)

    def _on_search_cleared(self) -> None:
        if not self._query:
            return
        self._query = ''
        self._load_page(reset=True)

    def _on_near_bottom(self) -> None:
        if not self._loading and self._has_more:
            self._load_page(reset=False)

    def _on_near_top(self) -> None:
        if self._loading or self._page <= 2:
            return
        self._task_id and cancel(self._task_id)
        self._task_id = None
        self._loading = False
        self._has_more = True
        self._page = 1
        self._grid.clear()
        self._load_page(reset=False)

    def _load_page(self, reset: bool) -> None:
        if self._task_id is not None:
            cancel(self._task_id)
            self._task_id = None
        if reset:
            self._page = 1
            self._has_more = True
            self._grid.clear()
        self._loading = True
        self._grid.set_has_more(self._has_more, loading=True)
        page = self._page
        if self._query:
            self._task_id = submit(
                manga_manager.search_all, args=(self._query, page),
                on_done=lambda result: self._on_page_loaded(page, result),
                on_error=self._on_page_error,
            )
        else:
            self._task_id = submit(
                manga_manager.browse_all, args=(page,),
                on_done=lambda result: self._on_page_loaded(page, result),
                on_error=self._on_page_error,
            )

    def _on_page_loaded(self, page: int, result) -> None:
        self._task_id = None
        self._loading = False
        self._has_more = result.has_more
        self._page = page + 1
        self._grid.add_entries(result.entries)
        self._grid.set_has_more(self._has_more, loading=False)

    def _on_page_error(self, message: str) -> None:
        logger.warning('MangaDex page load failed: %s', message)
        self._task_id = None
        self._loading = False
        self._has_more = False
        self._grid.set_has_more(False, loading=False)
        InfoBar.error(
            title=tr('books.manga_load_failed_title'),
            content=str(message),
            orient=Qt.Orientation.Horizontal,
            isClosable=True,
            position=InfoBarPosition.TOP_RIGHT,
            duration=4000,
            parent=self.window(),
        )

    def _switch_stack_animated(self, widget) -> None:
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

    def _show_details(self, entry: MangaEntry) -> None:
        if self._details_task_id is not None:
            cancel(self._details_task_id)
            self._details_task_id = None
        self._current_details_key = entry.key
        self._details.show_loading(entry)
        self._switch_stack_animated(self._details)

        cached = self._details_cache.get(entry.key)
        if cached is not None:
            self._details_cache.move_to_end(entry.key)
            self._details.show_details(cached)
            return

        self._details_task_id = submit(
            manga_manager.details_for_entry, args=(entry,),
            on_done=lambda details, k=entry.key: self._on_details_loaded(k, details),
            on_error=lambda error, k=entry.key: self._on_details_error(k, error),
        )

    def _on_details_loaded(self, key: str, details) -> None:
        self._details_task_id = None
        if key != self._current_details_key:
            return
        if details is None:
            self._details.show_error('not found')
            return
        self._details_cache[key] = details
        self._details_cache.move_to_end(key)
        while len(self._details_cache) > 25:
            self._details_cache.popitem(last=False)
        self._details.show_details(details)

    def _on_details_error(self, key: str, message: str) -> None:
        self._details_task_id = None
        if key != self._current_details_key:
            return
        logger.warning('Manga details load failed: %s', message)
        self._details.show_error(message)

    def _show_grid(self) -> None:
        if self._details_task_id is not None:
            cancel(self._details_task_id)
            self._details_task_id = None
        self._current_details_key = None
        self._switch_stack_animated(self._grid)

    def shutdown(self) -> None:
        if self._task_id is not None:
            cancel(self._task_id)
        if self._details_task_id is not None:
            cancel(self._details_task_id)
        self._details.shutdown()
        self._grid.clear()


class BooksBrowserTab(QWidget):

    _DETAILS_CACHE_LIMIT = 25

    def __init__(self, download_manager, parent=None):
        super().__init__(parent)
        from src.core.books.manager import BookDownloadBridge
        self._download_bridge = BookDownloadBridge(download_manager, self)
        self._query = ''
        self._has_more = True
        self._pending_pages: set[int] = set()
        self._task_ids: dict[int, str] = {}
        self._details_task_id: str | None = None
        self._details_cache: "OrderedDict[str, BookItem]" = OrderedDict()
        self._current_details_key: str | None = None
        self._all_sources = books_manager.provider_keys()
        self._selected_sources: list[str] = list(self._all_sources)
        self.setStyleSheet('background: transparent;')
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(14)
        header_row = QHBoxLayout()
        self._search_edit = SearchLineEdit(self)
        self._search_edit.setMinimumWidth(140)
        self._search_edit.setMaximumWidth(280)
        self._search_edit.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self._search_edit.setPlaceholderText(tr('books.search_placeholder'))
        self._search_edit.returnPressed.connect(self._on_search)
        self._search_edit.searchSignal.connect(self._on_search)
        self._search_edit.clearSignal.connect(self._on_search_cleared)
        header_row.addWidget(self._search_edit)
        self._filter_btn = PushButton(FluentIcon.FILTER, tr('books.filter_sources'), self)
        self._filter_btn.setFixedHeight(32)
        self._filter_btn.clicked.connect(self._on_filter_clicked)
        header_row.addWidget(self._filter_btn)
        header_row.addStretch(1)
        outer.addLayout(header_row)

        self._stack = QStackedWidget()
        outer.addWidget(self._stack, 1)

        self._grid = BookGridView(self)
        self._grid.book_clicked.connect(self._show_details)
        self._grid.near_bottom.connect(self._on_near_bottom)
        self._grid.page_needed.connect(self._on_page_needed)
        self._stack.addWidget(self._grid)

        self._details = BookDetailsView(self._download_bridge)
        self._details.back_requested.connect(self._show_grid)
        artwork.full_ready.connect(self._details.on_cover_ready)
        artwork.failed.connect(self._details.on_cover_failed)
        self._stack.addWidget(self._details)

        self._stack.setCurrentWidget(self._grid)
        register_locale_refresh(self, self._apply_locale)
        self._request_page(0)

    def _apply_locale(self, *_args) -> None:
        self._search_edit.setPlaceholderText(tr('books.search_placeholder'))
        self._filter_btn.setText(tr('books.filter_sources'))

    def _on_search(self) -> None:
        query = self._search_edit.text().strip()
        if query == self._query:
            return
        self._query = query
        self._reset_and_load_first()

    def _on_search_cleared(self) -> None:
        if not self._query:
            return
        self._query = ''
        self._reset_and_load_first()

    def _on_filter_clicked(self) -> None:
        dialog = BookSourceFilterDialog(self._all_sources, self._selected_sources, self.window())
        if not dialog.exec():
            return
        selected = dialog.selected_sources()
        if not selected:
            InfoBar.warning(
                title=tr('books.no_sources_title'),
                content=tr('books.no_sources_content'),
                orient=Qt.Orientation.Horizontal,
                isClosable=True,
                position=InfoBarPosition.TOP_RIGHT,
                duration=3000,
                parent=self.window(),
            )
            return
        if selected == self._selected_sources:
            return
        self._selected_sources = selected
        self._reset_and_load_first()

    def _on_near_bottom(self) -> None:
        if not self._has_more:
            return
        next_page = self._grid.known_page_count()
        self._request_page(next_page)

    def _on_page_needed(self, grid_page_no: int) -> None:
        self._request_page(grid_page_no)

    def _reset_and_load_first(self) -> None:
        for task_id in self._task_ids.values():
            cancel(task_id)
        self._task_ids.clear()
        self._pending_pages.clear()
        self._has_more = True
        self._grid.clear()
        self._request_page(0)

    def _request_page(self, grid_page_no: int) -> None:
        if grid_page_no < 0 or grid_page_no in self._pending_pages or self._grid.has_page(grid_page_no):
            return
        if not self._has_more and grid_page_no >= self._grid.known_page_count():
            return
        self._pending_pages.add(grid_page_no)
        manager_page = grid_page_no + 1
        if self._query:
            task_id = submit(
                books_manager.search_all,
                args=(self._query, manager_page),
                kwargs={'sources': list(self._selected_sources)},
                on_done=lambda result, p=grid_page_no: self._on_page_loaded(p, result),
                on_error=lambda error, p=grid_page_no: self._on_page_error(p, error),
            )
        else:
            task_id = submit(
                books_manager.browse_all,
                args=(manager_page,),
                kwargs={'sources': list(self._selected_sources)},
                on_done=lambda result, p=grid_page_no: self._on_page_loaded(p, result),
                on_error=lambda error, p=grid_page_no: self._on_page_error(p, error),
            )
        self._task_ids[grid_page_no] = task_id

    def _on_page_loaded(self, grid_page_no: int, result) -> None:
        self._pending_pages.discard(grid_page_no)
        self._task_ids.pop(grid_page_no, None)
        self._has_more = result.has_more
        self._grid.set_page(grid_page_no, result.entries, result.has_more)

    def _on_page_error(self, grid_page_no: int, error: str) -> None:
        logger.warning('Books page load failed: %s', error)
        self._pending_pages.discard(grid_page_no)
        self._task_ids.pop(grid_page_no, None)
        if grid_page_no == 0:
            self._has_more = False

    def _switch_stack_animated(self, widget) -> None:
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

    def _show_details(self, entry: BookItem) -> None:
        if self._details_task_id is not None:
            cancel(self._details_task_id)
            self._details_task_id = None
        self._current_details_key = entry.key
        self._details.show_loading(entry)
        self._switch_stack_animated(self._details)

        cached = self._details_cache.get(entry.key)
        if cached is not None:
            self._details_cache.move_to_end(entry.key)
            self._details.show_details(cached)
            return

        self._details_task_id = submit(
            books_manager.details_for_entry,
            args=(entry,),
            on_done=lambda details, k=entry.key: self._on_details_loaded(k, details),
            on_error=lambda error, k=entry.key: self._on_details_error(k, error),
        )

    def _on_details_loaded(self, key: str, details: BookItem | None) -> None:
        self._details_task_id = None
        if key != self._current_details_key:
            return
        if details is None:
            self._details.show_error('not found')
            return
        self._details_cache[key] = details
        self._details_cache.move_to_end(key)
        while len(self._details_cache) > self._DETAILS_CACHE_LIMIT:
            self._details_cache.popitem(last=False)
        self._details.show_details(details)

    def _on_details_error(self, key: str, message: str) -> None:
        self._details_task_id = None
        if key != self._current_details_key:
            return
        logger.warning('Book details load failed: %s', message)
        self._details.show_error(message)

    def _show_grid(self) -> None:
        if self._details_task_id is not None:
            cancel(self._details_task_id)
            self._details_task_id = None
        self._current_details_key = None
        self._switch_stack_animated(self._grid)
        self._details.release_full_cover()

    def shutdown(self) -> None:
        try:
            artwork.full_ready.disconnect(self._details.on_cover_ready)
            artwork.failed.disconnect(self._details.on_cover_failed)
        except (TypeError, RuntimeError):
            pass
        self._grid.shutdown()
        self._download_bridge.shutdown()


class BooksPage(QWidget):
    """Top-level page hosting the Books and Manga tabs."""

    def __init__(self, download_manager, parent=None, manga_download_bridge=None):
        super().__init__(parent)
        self.setObjectName('booksPage')
        self.setStyleSheet('background: transparent;')
        outer = QVBoxLayout(self)
        outer.setContentsMargins(20, 12, 20, 20)
        outer.setSpacing(14)

        self._pivot = Pivot()
        outer.addWidget(self._pivot)

        self._section_stack = QStackedWidget()
        outer.addWidget(self._section_stack, 1)

        self._tabs: dict[str, QWidget] = {}

        self._books_tab = BooksBrowserTab(download_manager, self)
        self._add_tab('books', 'Books', self._books_tab)

        self._manga_tab = MangaBrowserTab(
            download_manager, self, manga_download_bridge=manga_download_bridge
        )
        self._add_tab('manga', 'Manga', self._manga_tab)

        self._pivot.currentItemChanged.connect(self._on_tab_changed)
        self._pivot.setCurrentItem('books')
        self._section_stack.setCurrentWidget(self._books_tab)

    def _add_tab(self, key: str, display_name: str, widget: QWidget) -> None:
        self._tabs[key] = widget
        self._section_stack.addWidget(widget)
        self._pivot.addItem(routeKey=key, text=display_name)

    def _on_tab_changed(self, key: str) -> None:
        widget = self._tabs.get(key)
        if widget is not None:
            self._section_stack.setCurrentWidget(widget)

    def shutdown(self) -> None:
        for tab in self._tabs.values():
            shutdown_fn = getattr(tab, 'shutdown', None)
            if callable(shutdown_fn):
                try:
                    shutdown_fn()
                except Exception:
                    logger.exception('Failed to shut down books sub-tab')


