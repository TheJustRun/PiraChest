from __future__ import annotations

from functools import partial

from PySide6.QtCore import QEasingCurve, QEvent, QPoint, QRect, QRectF, Qt, QTimer, QVariantAnimation, Signal
from PySide6.QtGui import QColor, QCursor, QFont, QFontMetrics, QPainter, QPainterPath
from PySide6.QtWidgets import QAbstractScrollArea, QToolTip
from qfluentwidgets import FluentIcon, TransparentToolButton, isDarkTheme, qconfig

from src.core.theme import palette


class _LazyImages:
    __slots__ = ('_target',)

    def __init__(self):
        self._target = None

    def __getattr__(self, name):
        target = object.__getattribute__(self, '_target')
        if target is None:
            from src.core.artwork import images as target
            object.__setattr__(self, '_target', target)
        return getattr(target, name)

    def release(self, owner) -> None:
        target = object.__getattribute__(self, '_target')
        if target is not None:
            target.release(owner)

    def trim(self, limit: int) -> None:
        target = object.__getattribute__(self, '_target')
        if target is not None:
            target.trim(limit)


images = _LazyImages()

GRID, LIST = 'grid', 'list'


def play_sfx(name: str) -> None:
    try:
        from .settings_page import get_sound_manager
        get_sound_manager().play(name)
    except Exception:
        pass

_SPACING = 14
_SIDE = 12
_TOP = 4
_HEADER_GAP = 20
_LIFT = 8
_PRESS_LIFT = 5
_RADIUS = 6
_BAR_W = 6
_CAPTION_H = 36
_CAPTION_GAP = 8
_WHEEL_PX = 120
_ROW_H, _ROW_GAP = 76, 4
_THUMB_W, _THUMB_H = 48, 68
_STRIP_GAP, _STRIP_PAD = 12, 2

_BAR_CSS = (
    'QScrollBar:{o} {{ {dim}: 6px; background: transparent; margin: 0; border: none; }}'
    'QScrollBar::handle:{o} {{ background: rgba(255,255,255,60); border-radius: 3px; min-{long}: 24px; }}'
    'QScrollBar::add-line:{o}, QScrollBar::sub-line:{o} {{ {long}: 0; border: none; }}'
    'QScrollBar::add-page:{o}, QScrollBar::sub-page:{o} {{ background: none; }}')


def attrs_subtitle(*attrs: str, labels: dict | None = None):
    labels = labels or {}

    def fn(entry) -> str:
        parts = []
        for attr in attrs:
            value = getattr(entry, attr, None)
            if value:
                parts.append(str(labels.get(value, value)) if isinstance(value, str) else str(value))
        return ' · '.join(parts)
    return fn


def default_colors() -> tuple[QColor, QColor, QColor]:
    r = g = b = 255 if isDarkTheme() else 0
    return QColor(r, g, b, 225), QColor(r, g, b, 150), QColor(r, g, b, 36)


def grid_colors() -> tuple[QColor, QColor, QColor]:
    p = palette()
    strong = QColor(str(p.get('primary_text') or ('#ffffff' if isDarkTheme() else '#1a1a1a')))
    if not strong.isValid():
        strong = QColor(128, 128, 128)
    muted = p.get('muted') or p.get('secondary_text') or p.get('primary_text')
    muted_color = QColor(str(muted)) if muted else QColor(128, 128, 128)
    if not muted_color.isValid():
        muted_color = QColor(128, 128, 128)
    base = 255 if isDarkTheme() else 0
    return strong, muted_color, QColor(base, base, base, 36)


class _PosterView(QAbstractScrollArea):
    poster_clicked = Signal(object)
    context_requested = Signal(object, QPoint)
    hovered = Signal(object)
    visible_changed = Signal(list)

    _MARGIN_PX = 600
    _SNAP = True
    _HOVER_CURSOR = Qt.CursorShape.PointingHandCursor

    def __init__(self, parent, *, vertical, cell_w, poster_h, cover_attrs, key_attr, title_attr,
                 tooltip_fn, subtitle_fn, show_caption, fallback_icon_fn, colors_fn,
                 img_size=None, cell_h=None, meta_fn=None, search_fn=None):
        super().__init__(parent)
        self._vertical = vertical
        self._cw, self._ph, self._caption = cell_w, poster_h, show_caption
        self._fixed_cell_h = cell_h is not None
        self._cell_h = cell_h if cell_h is not None else (
            _LIFT + poster_h + ((_CAPTION_GAP + _CAPTION_H) if show_caption else 0))
        self._img = tuple(img_size) if img_size else (cell_w, poster_h)
        self._cover_attrs, self._key_attr, self._title_attr = cover_attrs, key_attr, title_attr
        self._tooltip_fn, self._subtitle_fn, self._icon_fn = tooltip_fn, subtitle_fn, fallback_icon_fn
        self._meta_fn = meta_fn
        self._search_fn = search_fn
        self._colors_fn = colors_fn or default_colors
        self._rowmode = False

        self._entries: list = []
        self._lower: list[str] = []
        self._keys: set = set()
        self._rows: list[int] = []
        self._query = ''
        self._pending: dict[int, int] = {}
        self._failed: set[int] = set()
        self._synced_span: tuple[int, int] | None = None
        self._lift: dict[int, float] = {}
        self._hover: int | None = None
        self._pressed: int | None = None
        self._icons: dict[int, object] = {}
        self.is_scrolling = False

        self._settle = QTimer(self, singleShot=True, interval=120)
        self._ticker = QTimer(self, interval=16)
        self._wheel = QVariantAnimation(self, duration=160, easingCurve=QEasingCurve.Type.OutCubic)
        base = QFont(self.font())
        self._f_title, self._f_small = QFont(base), QFont(base)
        self._f_title.setPixelSize(13)
        self._f_title.setWeight(QFont.Weight.DemiBold)
        self._f_small.setPixelSize(12)
        self._text, self._muted, self._tint = self._colors_fn()

        self.setFrameShape(QAbstractScrollArea.Shape.NoFrame)
        self.viewport().setAutoFillBackground(False)
        self.viewport().setMouseTracking(True)
        (self.setHorizontalScrollBarPolicy if vertical else self.setVerticalScrollBarPolicy)(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        bar = self._bar()
        (bar.setFixedWidth if vertical else bar.setFixedHeight)(_BAR_W)
        bar.setStyleSheet(_BAR_CSS.format(o='vertical' if vertical else 'horizontal',
                                          dim='width' if vertical else 'height',
                                          long='height' if vertical else 'width'))
        bar.valueChanged.connect(self._on_scroll)
        self._settle.timeout.connect(self._on_settled)
        self._ticker.timeout.connect(self._step_lift)
        self._wheel.valueChanged.connect(lambda v: self._bar().setValue(round(v)))
        qconfig.themeChanged.connect(self._on_theme)


    def set_entries(self, entries) -> None:
        self.clear()
        self.append_entries(entries)

    def append_entries(self, entries) -> None:
        added = False
        for entry in entries:
            key = getattr(entry, self._key_attr, None)
            key = id(entry) if key is None else key
            if key in self._keys:
                continue
            self._keys.add(key)
            self._entries.append(entry)
            self._lower.append(self._entry_search_text(entry))
            if self._query in self._lower[-1]:
                self._rows.append(len(self._entries) - 1)
            added = True
        if added:
            self._layout()

    def clear(self) -> None:
        self._entries, self._lower, self._keys, self._query = [], [], set(), ''
        self._rebuild_rows()

    def count_entries(self) -> int:
        return len(self._entries)

    def filter_by_title(self, query: str) -> None:
        self._query = query.strip().lower()
        self._rebuild_rows()

    def refresh(self) -> None:
        self._layout()

    def set_show_caption(self, on: bool) -> None:
        on = bool(on)
        if on == self._caption:
            return
        self._caption = on
        if not self._fixed_cell_h:
            self._cell_h = _LIFT + self._ph + ((_CAPTION_GAP + _CAPTION_H) if on else 0)
        self._reset_requests()
        self._layout()
        self.viewport().update()


    def _layout(self) -> None:
        raise NotImplementedError

    def _span(self, margin: int) -> tuple[int, int]:
        raise NotImplementedError

    def _cell_rect(self, pos: int) -> QRect:
        raise NotImplementedError

    def _hit(self, point: QPoint) -> int | None:
        raise NotImplementedError

    def _on_offset_changed(self) -> None:
        pass


    def _bar(self):
        return self.verticalScrollBar() if self._vertical else self.horizontalScrollBar()

    def _rebuild_rows(self) -> None:
        q = self._query
        self._rows = [i for i, t in enumerate(self._lower) if q in t] if q else list(range(len(self._entries)))
        self._reset_requests()
        self._layout()

    def _reset_requests(self) -> None:
        images.release(self)
        self._synced_span = None
        self._pending.clear()
        self._failed.clear()
        self._lift.clear()
        self._hover = self._pressed = None

    def _entry(self, pos: int):
        return self._entries[self._rows[pos]]

    def _entry_search_text(self, entry) -> str:
        if self._search_fn is not None:
            try:
                value = self._search_fn(entry)
                if value:
                    return str(value).casefold()
            except Exception:
                pass
        return str(getattr(entry, self._title_attr, '') or '').casefold()

    def _title(self, pos: int) -> str:
        return getattr(self._entry(pos), self._title_attr, '') or ''

    def _meta(self, pos: int) -> str:
        if self._meta_fn is None:
            return ''
        try:
            return str(self._meta_fn(self._entry(pos)) or '')
        except Exception:
            return ''

    def _src(self, pos: int) -> str:
        entry = self._entry(pos)
        for attr in self._cover_attrs:
            value = getattr(entry, attr, None)
            if value:
                return str(value)
        return ''

    def _img_size(self) -> tuple[int, int]:
        return (_THUMB_W, _THUMB_H) if self._rowmode else self._img


    def _sync(self, force: bool = True) -> None:
        lo, hi = self._span(self._MARGIN_PX)
        if not force and self._synced_span == (lo, hi):
            return
        self._synced_span = (lo, hi)
        vlo, vhi = self._span(0)
        if vhi >= vlo:
            self.visible_changed.emit([self._entries[self._rows[p]] for p in range(vlo, vhi + 1)])
        for pos in [p for p in self._pending if not lo <= p <= hi]:
            images.cancel(self._pending.pop(pos))
        self._failed = {p for p in self._failed if lo <= p <= hi}
        size = self._img_size()
        for pos in range(lo, hi + 1):
            if pos in self._pending or pos in self._failed:
                continue
            src = self._src(pos)
            if not src or images.peek(src, size) is not None:
                continue
            token = images.get(src, size, partial(self._on_image, pos, src), owner=self)
            if token:
                self._pending[pos] = token

    def _on_image(self, pos: int, src: str, pixmap) -> None:
        self._pending.pop(pos, None)
        if pos >= len(self._rows) or self._src(pos) != src:
            return
        if pixmap.isNull():
            self._failed.add(pos)
        lo, hi = self._span(0)
        if lo <= pos <= hi:
            self.viewport().update(self._cell_rect(pos))


    def _on_scroll(self, _value=0) -> None:
        self.is_scrolling = True
        self._settle.start()
        self._on_offset_changed()
        self._sync(force=False)
        self.viewport().update()

    def _on_settled(self) -> None:
        self.is_scrolling = False
        self._set_hover(self._hit(self.viewport().mapFromGlobal(QCursor.pos())))

    def _smooth_scroll(self, delta: float) -> None:
        bar = self._bar()
        running = self._wheel.state() == QVariantAnimation.State.Running
        base = self._wheel.endValue() if running else bar.value()
        self._wheel.stop()
        self._wheel.setStartValue(float(bar.value()))
        self._wheel.setEndValue(float(max(0, min(bar.maximum(), base + delta))))
        self._wheel.start()

    def _on_theme(self, *_args) -> None:
        QTimer.singleShot(0, self._apply_colors)

    def _apply_colors(self) -> None:
        self._text, self._muted, self._tint = self._colors_fn()
        self._icons.clear()
        self.viewport().update()


    def _target(self, pos: int) -> float:
        if pos != self._hover or self.is_scrolling:
            return 0.0
        return _PRESS_LIFT if self._pressed == pos else _LIFT

    def _set_hover(self, pos: int | None) -> None:
        if pos != self._hover:
            old, self._hover = self._hover, pos
            self.viewport().setCursor(self._HOVER_CURSOR if pos is not None else Qt.CursorShape.ArrowCursor)
            for p in (old, pos):
                if p is not None and p < len(self._rows):
                    goal = self._target(p)
                    if not self._SNAP:
                        pass
                    elif goal:
                        self._lift[p] = goal
                    else:
                        self._lift.pop(p, None)
                    self.viewport().update(self._cell_rect(p))
            self.hovered.emit(self._entry(pos) if pos is not None and pos < len(self._rows) else None)
        if not self._ticker.isActive():
            self._ticker.start()

    def _step_lift(self) -> None:
        moving = False
        for pos in set(self._lift) | ({self._hover} - {None}):
            now, goal = self._lift.get(pos, 0.0), self._target(pos)
            now += (goal - now) * 0.35
            if abs(goal - now) < 0.4:
                now = goal
            if now:
                self._lift[pos] = now
            else:
                self._lift.pop(pos, None)
            moving = moving or now != goal
            if pos < len(self._rows):
                self.viewport().update(self._cell_rect(pos))
        if not moving:
            self._ticker.stop()

    def mouseMoveEvent(self, event) -> None:
        pos = None if self.is_scrolling else self._hit(event.position().toPoint())
        if pos is not None and pos != self._hover:
            play_sfx('hover')
        self._set_hover(pos)

    def leaveEvent(self, event) -> None:
        self._pressed = None
        self._set_hover(None)

    def mousePressEvent(self, event) -> None:
        pos = self._hit(event.position().toPoint())
        if pos is None:
            return
        if event.button() == Qt.MouseButton.RightButton:
            self.context_requested.emit(self._entry(pos), event.globalPosition().toPoint())
        elif event.button() == Qt.MouseButton.LeftButton:
            self._pressed = pos
            self._set_hover(pos)

    def mouseReleaseEvent(self, event) -> None:
        pressed, self._pressed = self._pressed, None
        if event.button() != Qt.MouseButton.LeftButton or pressed is None:
            return
        if self._hit(event.position().toPoint()) == pressed:
            play_sfx('click')
            self.poster_clicked.emit(self._entry(pressed))
        self._set_hover(self._hover)

    def viewportEvent(self, event) -> bool:
        if event.type() == QEvent.Type.ToolTip:
            pos = self._hit(event.pos())
            if pos is None:
                QToolTip.hideText()
            else:
                entry = self._entry(pos)
                tip = self._tooltip_fn(entry) if self._tooltip_fn else self._title(pos)
                QToolTip.showText(event.globalPos(), tip, self.viewport())
            return True
        return super().viewportEvent(event)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._layout()


    def paintEvent(self, event) -> None:
        lo, hi = self._span(0)
        if hi < lo:
            return
        p = QPainter(self.viewport())
        p.setRenderHints(QPainter.RenderHint.Antialiasing | QPainter.RenderHint.SmoothPixmapTransform)
        dirty = event.rect()
        paint = self._paint_row if self._rowmode else self._paint_card
        for pos in range(lo, hi + 1):
            cell = self._cell_rect(pos)
            if cell.intersects(dirty):
                paint(p, pos, cell)
        p.end()

    def _paint_card(self, p: QPainter, pos: int, cell: QRect) -> None:
        poster = QRectF(cell.x(), cell.y() + _LIFT - self._lift.get(pos, 0.0), self._cw, self._ph)
        titled = not self._caption
        self._paint_cover(p, pos, poster, 36 if titled else 48, titled)
        if self._caption:
            box = QRect(cell.x(), cell.y() + _LIFT + self._ph + _CAPTION_GAP, self._cw, _CAPTION_H)
            p.save()
            p.setClipRect(box)
            title = self._title(pos)
            meta = self._meta(pos)
            title_h = 18 if meta else _CAPTION_H
            p.setFont(self._f_title if meta else self._f_small)
            p.setPen(self._text if meta else self._muted)
            fm = QFontMetrics(self._f_title if meta else self._f_small)
            p.drawText(
                QRect(box.x(), box.y(), box.width(), title_h),
                int(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop),
                fm.elidedText(title, Qt.TextElideMode.ElideRight, box.width()),
            )
            if meta:
                p.setFont(self._f_small)
                p.setPen(self._muted)
                fm_small = QFontMetrics(self._f_small)
                p.drawText(
                    QRect(box.x(), box.y() + 18, box.width(), _CAPTION_H - 18),
                    int(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop),
                    fm_small.elidedText(meta, Qt.TextElideMode.ElideRight, box.width()),
                )
            p.restore()

    def _paint_row(self, p: QPainter, pos: int, cell: QRect) -> None:
        glow = self._lift.get(pos, 0.0) / _LIFT
        if glow:
            shade = QColor(self._tint)
            shade.setAlpha(round(self._tint.alpha() * glow))
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(shade)
            p.drawRoundedRect(cell, 8, 8)
        thumb = QRectF(cell.x() + 12, cell.y() + (cell.height() - _THUMB_H) / 2, _THUMB_W, _THUMB_H)
        self._paint_cover(p, pos, thumb, 24, False)
        x = cell.x() + 12 + _THUMB_W + 14
        width = max(0, cell.right() - x - 12)
        sub = self._subtitle_fn(self._entry(pos)) if self._subtitle_fn else ''
        top = cell.y() + (cell.height() - 42) // 2 if sub else cell.y()
        p.setFont(self._f_title)
        p.setPen(self._text)
        title = QFontMetrics(self._f_title).elidedText(self._title(pos), Qt.TextElideMode.ElideRight, width)
        p.drawText(QRect(x, top, width, 20 if sub else cell.height()),
                   int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter), title)
        if sub:
            p.setFont(self._f_small)
            p.setPen(self._muted)
            sub = QFontMetrics(self._f_small).elidedText(sub, Qt.TextElideMode.ElideRight, width)
            p.drawText(QRect(x, top + 24, width, 18), int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter), sub)

    def _paint_cover(self, p: QPainter, pos: int, rect: QRectF, icon_px: int, titled: bool) -> None:
        src = self._src(pos)
        pixmap = images.peek(src, self._img_size()) if src else None
        path = QPainterPath()
        path.addRoundedRect(rect, _RADIUS, _RADIUS)
        p.save()
        p.setClipPath(path)
        if pixmap is not None and not pixmap.isNull():
            p.drawPixmap(rect.toRect(), pixmap)
        else:
            p.fillRect(rect, self._tint)
            icon = self._icon(icon_px)
            if icon is not None:
                p.drawPixmap(int(rect.center().x() - icon_px / 2),
                             int(rect.center().y() - (icon_px if titled else icon_px / 2)), icon)
            if titled:
                p.setFont(self._f_small)
                p.setPen(self._text)
                p.drawText(QRectF(rect.x() + 10, rect.center().y() + 4, rect.width() - 20, 60),
                           int(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop | Qt.TextFlag.TextWordWrap),
                           self._title(pos))
        p.restore()

    def _icon(self, size: int):
        if self._icon_fn is None:
            return None
        if size not in self._icons:
            self._icons[size] = self._icon_fn().pixmap(size, size)
        return self._icons[size]


class PosterGrid(_PosterView):
    download_requested = Signal(object)
    near_bottom = Signal()
    view_mode_changed = Signal(str)

    _NEAR_BOTTOM_THRESHOLD_PX = 2000

    def __init__(self, parent=None, *, header=None, cell_w=150, poster_h=210,
                 cover_attrs=('poster_path', 'poster_url'), key_attr='url', title_attr='title',
                 tooltip_fn=None, subtitle_fn=None, show_caption=False, fallback_icon_fn=None, colors_fn=None,
                 img_size=None, cell_h=None, side=_SIDE, top=_TOP, meta_fn=None, search_fn=None):
        self._header = header
        self._header_state = (True, 0)
        self._side, self._pad_top = side, top
        self._cols, self._gap, self._left, self._top = 1, _SPACING, side, top
        self._m_w, self._m_h, self._row_h = cell_w, _LIFT + poster_h, _LIFT + poster_h + _SPACING
        super().__init__(parent, vertical=True, cell_w=cell_w, poster_h=poster_h, cover_attrs=cover_attrs,
                         key_attr=key_attr, title_attr=title_attr, tooltip_fn=tooltip_fn, subtitle_fn=subtitle_fn,
                         show_caption=show_caption, fallback_icon_fn=fallback_icon_fn, colors_fn=colors_fn,
                         img_size=img_size, cell_h=cell_h, meta_fn=meta_fn, search_fn=search_fn)
        if header is not None:
            header.setParent(self.viewport())
            header.installEventFilter(self)
            self._header_state = (header.isHidden(), header.height())
            for name in ('poster_clicked', 'download_requested'):
                if hasattr(header, name):
                    getattr(header, name).connect(getattr(self, name).emit)


    @property
    def view_mode(self) -> str:
        return LIST if self._rowmode else GRID

    def set_view_mode(self, mode: str) -> None:
        if (mode == LIST) == self._rowmode:
            return
        first = self._span(0)[0]
        self._rowmode = mode == LIST
        self._reset_requests()
        self._layout()
        self._bar().setValue(0 if first == 0 else self._top + (first // self._cols) * self._row_h)
        self.view_mode_changed.emit(self.view_mode)

    def set_latest_entries(self, entries) -> None:
        if self._header is not None:
            self._header.set_entries(entries)

    def set_latest_section_visible(self, visible: bool) -> None:
        if self._header is not None:
            self._header.set_section_visible(visible)

    def eventFilter(self, obj, event) -> bool:
        if obj is self._header and event.type() in (QEvent.Type.Resize, QEvent.Type.Show, QEvent.Type.Hide):
            state = (obj.isHidden(), obj.height())
            if state != self._header_state:
                self._header_state = state
                self._layout()
        return super().eventFilter(obj, event)


    def _layout(self) -> None:
        vp = self.viewport()
        side = self._side
        avail = max(0, self.width() - _BAR_W) - 2 * side
        if self._rowmode:
            self._cols, self._gap, self._left = 1, 0, side
            self._m_w, self._m_h, self._row_h = avail, _ROW_H, _ROW_H + _ROW_GAP
        else:
            (self._cols, self._gap, self._left,
             self._m_w, self._m_h, self._row_h) = self._grid_metrics(avail)
        header_h = 0
        if self._header is not None and not self._header.isHidden():
            header_h = self._header.height() + _HEADER_GAP
            self._header.setFixedWidth(max(0, avail))
        self._top = self._pad_top + header_h
        rows = -(-len(self._rows) // self._cols)
        content_h = self._top + rows * self._row_h - (self._row_h - self._m_h if rows else 0)
        bar = self._bar()
        bar.setRange(0, max(0, content_h - vp.height()))
        bar.setPageStep(vp.height())
        bar.setSingleStep(self._row_h // 2)
        self._place_header()
        self._sync()
        vp.update()
        self._check_bottom()

    def _grid_metrics(self, avail: int) -> tuple:
        cols = max(1, (avail + _SPACING) // (self._cw + _SPACING))
        gap = _SPACING if cols == 1 else max(_SPACING, min(_SPACING * 4, (avail - cols * self._cw) // (cols - 1)))
        grid_w = cols * self._cw + (cols - 1) * gap
        return (cols, gap, self._side + max(0, (avail - grid_w) // 2),
                self._cw, self._cell_h, self._cell_h + _SPACING)

    def _place_header(self) -> None:
        if self._header is not None:
            self._header.move(self._side, self._pad_top - self._bar().value())

    def _check_bottom(self) -> None:
        bar = self._bar()
        if self._rows and bar.maximum() - bar.value() <= self._NEAR_BOTTOM_THRESHOLD_PX:
            self.near_bottom.emit()

    def _on_offset_changed(self) -> None:
        self._place_header()
        self._check_bottom()

    def _span(self, margin: int) -> tuple[int, int]:
        n = len(self._rows)
        value = self._bar().value()
        top = value - margin - self._top
        bottom = value + self.viewport().height() + margin - self._top
        if n == 0 or bottom <= 0:
            return 0, -1
        first_row = max(0, top) // self._row_h
        last_row = (bottom - 1) // self._row_h
        return first_row * self._cols, min(n - 1, (last_row + 1) * self._cols - 1)

    def _cell_rect(self, pos: int) -> QRect:
        row, col = divmod(pos, self._cols)
        return QRect(self._left + col * (self._m_w + self._gap),
                     self._top + row * self._row_h - self._bar().value(), self._m_w, self._m_h)

    def _hit(self, point: QPoint) -> int | None:
        col, dx = divmod(point.x() - self._left, self._m_w + self._gap)
        row, dy = divmod(point.y() + self._bar().value() - self._top, self._row_h)
        if not 0 <= col < self._cols or dx >= self._m_w or dy >= self._m_h or row < 0:
            return None
        pos = row * self._cols + col
        return pos if pos < len(self._rows) else None

    def wheelEvent(self, event) -> None:
        pixels = event.pixelDelta().y()
        if pixels:
            self._wheel.stop()
            self._bar().setValue(self._bar().value() - pixels)
        else:
            self._smooth_scroll(-event.angleDelta().y() / 120 * _WHEEL_PX)
        event.accept()


class PosterStrip(_PosterView):
    def __init__(self, parent=None, *, cell_w=138, poster_h=207, cover_attrs=('poster_url',), key_attr='id',
                 title_attr='title', tooltip_fn=None, show_caption=True, fallback_icon_fn=None, colors_fn=None,
                 img_size=None, cell_h=None, gap=_STRIP_GAP, pad=_STRIP_PAD, top=0, height=None):
        self._sgap, self._spad, self._stop = gap, pad, top
        super().__init__(parent, vertical=False, cell_w=cell_w, poster_h=poster_h, cover_attrs=cover_attrs,
                         key_attr=key_attr, title_attr=title_attr, tooltip_fn=tooltip_fn, subtitle_fn=None,
                         show_caption=show_caption, fallback_icon_fn=fallback_icon_fn, colors_fn=colors_fn,
                         img_size=img_size, cell_h=cell_h)
        self.setFixedHeight(height if height is not None else self._cell_h + top + _BAR_W + 8)

    def _layout(self) -> None:
        stride = self._cw + self._sgap
        content_w = 2 * self._spad + max(0, len(self._rows) * stride - self._sgap)
        bar = self._bar()
        bar.setRange(0, max(0, content_w - self.viewport().width()))
        bar.setPageStep(self.viewport().width())
        bar.setSingleStep(stride)
        self._sync()
        self.viewport().update()

    def _span(self, margin: int) -> tuple[int, int]:
        n = len(self._rows)
        if not n:
            return 0, -1
        stride = self._cw + self._sgap
        value = self._bar().value()
        lo = max(0, (value - margin - self._spad) // stride)
        hi = min(n - 1, (value + self.viewport().width() + margin - self._spad) // stride)
        return (lo, hi) if hi >= lo else (0, -1)

    def _cell_rect(self, pos: int) -> QRect:
        return QRect(self._spad + pos * (self._cw + self._sgap) - self._bar().value(), self._stop, self._cw, self._cell_h)

    def _hit(self, point: QPoint) -> int | None:
        x = point.x() + self._bar().value() - self._spad
        pos, dx = divmod(x, self._cw + self._sgap)
        if x < 0 or dx >= self._cw or not self._stop <= point.y() < self._stop + self._cell_h or pos >= len(self._rows):
            return None
        return pos

    def wheelEvent(self, event) -> None:
        pixels = event.pixelDelta().x()
        notches = event.angleDelta().x() / 120
        if not notches and not pixels and event.modifiers() & Qt.KeyboardModifier.ShiftModifier:
            notches = event.angleDelta().y() / 120
        if pixels:
            self._wheel.stop()
            self._bar().setValue(self._bar().value() - pixels)
        elif notches:
            self._smooth_scroll(-notches * _WHEEL_PX)
        else:
            event.ignore()
            return
        event.accept()


class ViewModeButton(TransparentToolButton):
    def __init__(self, *grids: PosterGrid, parent=None, list_tip: str = 'List view', grid_tip: str = 'Grid view'):
        super().__init__(parent)
        self._tips = {GRID: list_tip, LIST: grid_tip}
        self._grids: list[PosterGrid] = []
        self._mode = grids[0].view_mode if grids else GRID
        self.setFixedSize(32, 32)
        self.clicked.connect(lambda: self.set_mode(GRID if self._mode == LIST else LIST))
        for grid in grids:
            self.bind(grid)
        self._sync()

    def bind(self, grid: PosterGrid) -> None:
        self._grids.append(grid)
        grid.set_view_mode(self._mode)

    def set_mode(self, mode: str) -> None:
        self._mode = mode
        for grid in self._grids:
            grid.set_view_mode(mode)
        self._sync()

    def _sync(self) -> None:
        names = ('MENU', 'ALIGNMENT', 'VIEW') if self._mode == GRID else ('APPLICATION', 'TILES', 'VIEW')
        self.setIcon(next(getattr(FluentIcon, n) for n in names if hasattr(FluentIcon, n)))
        self.setToolTip(self._tips[self._mode])
