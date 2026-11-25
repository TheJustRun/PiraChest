from __future__ import annotations

import time
from contextlib import suppress

import shiboken6
from PySide6.QtCore import (
    QEasingCurve,
    QEvent,
    QModelIndex,
    QPoint,
    QPropertyAnimation,
    QRect,
    QRectF,
    QSize,
    Qt,
    QTimer,
)
from PySide6.QtGui import QColor, QPainter
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QFrame,
    QGraphicsOpacityEffect,
    QListWidgetItem,
    QStyledItemDelegate,
    QVBoxLayout,
)

from src.core.theme import (
    _HOVER_SOFTEN,
    _MENU_ROW,
    _apply_tm_rules,
    _class_chain,
    _menu_opaque,
    _menu_radius,
    _menu_text_pad,
    _nav_state_css,
    _over_opaque,
    _palette_color,
    manager,
)


_POPUP_GAP = 4
_POPUP_MAX_W = 260
_TEXT_PAD_RIGHT = 10


def _sampled_fill(anchor) -> QColor | None:
    win = anchor.window()
    if win is None or anchor.width() < 12 or anchor.height() < 4:
        return None
    hovered = anchor.testAttribute(Qt.WidgetAttribute.WA_UnderMouse)
    anchor.setAttribute(Qt.WidgetAttribute.WA_UnderMouse, False)
    try:
        spot = anchor.mapTo(win, QPoint(5, anchor.height() // 2))
        image = win.grab(QRect(spot, QSize(1, 1))).toImage()
        return image.pixelColor(0, 0) if not image.isNull() else None
    finally:
        anchor.setAttribute(Qt.WidgetAttribute.WA_UnderMouse, hovered)


def _popup_base(anchor) -> QColor:
    pal = manager.palette()
    page = _palette_color(pal, "page_bg") or QColor("#202020")
    page.setAlpha(255)
    sampled = _sampled_fill(anchor)
    if sampled is not None and sampled.alpha() > 0:
        return _over_opaque(sampled, page) if sampled.alpha() < 255 else QColor(sampled.rgb())
    if "ComboBox" in _class_chain(anchor):
        card = _palette_color(pal, "card_bg")
        under = _over_opaque(card, page) if card is not None else page
        fill = _palette_color(pal, "input_bg")
    else:
        under, fill = page, _palette_color(pal, "btn_bg")
    return _over_opaque(fill, under) if fill is not None else under


def _combo_popup_qss(anchor) -> str:
    base = _popup_base(anchor)
    radius = _menu_radius()
    return (
        "QFrame#comboPopup {"
        f" background: {base.name()}; border: none;"
        f" border-radius: {radius}px; }}"
        "QListWidget#comboList { background: transparent; border: none; outline: none; }"
    )


class _MarqueeItemDelegate(QStyledItemDelegate):

    _HOVER_DELAY_MS = 350
    _END_HOLD_MS = 900
    _SPEED_PX_S = 55.0
    _FRAME_MS = 16
    _INSET = 4

    def __init__(self, view, ink: QColor, selected: QColor, radius: int, selected_row: int = -1,
                 pad_left: int = 6, pad_right: int = 10):
        super().__init__(view)
        self._view = view
        self._ink = QColor(ink)
        self._sel = QColor(selected)
        self._hov = QColor(selected)
        self._hov.setAlphaF(self._hov.alphaF() * _HOVER_SOFTEN)
        self._selected_row = selected_row
        self._radius = radius
        self._pad_left = pad_left
        self._pad_right = pad_right
        self._hover = -1
        self._row = -1
        self._running = False
        self._offset = 0.0
        self._overflow = 0.0
        self._t0 = 0.0
        self._delay = QTimer(self)
        self._delay.setSingleShot(True)
        self._delay.setInterval(self._HOVER_DELAY_MS)
        self._delay.timeout.connect(self._begin)
        self._frame = QTimer(self)
        self._frame.setTimerType(Qt.TimerType.PreciseTimer)
        self._frame.setInterval(self._FRAME_MS)
        self._frame.timeout.connect(self._tick)
        view.setMouseTracking(True)
        view.viewport().setMouseTracking(True)
        view.viewport().installEventFilter(self)

    def _alive(self) -> bool:
        return shiboken6.isValid(self._view)

    def _text_rect(self, rect: QRect) -> QRect:
        return rect.adjusted(self._INSET + self._pad_left, 0, -(self._INSET + self._pad_right), 0)

    def eventFilter(self, obj, event):
        if not self._alive():
            return False
        try:
            viewport = self._view.viewport()
        except RuntimeError:
            return False
        if obj is viewport:
            kind = event.type()
            if kind == QEvent.Type.MouseMove:
                pos = event.position().toPoint()
                index = self._view.indexAt(pos) if viewport.rect().contains(pos) else QModelIndex()
                row = index.row() if index.isValid() else -1
                self._set_hover(row)
                if row != self._row:
                    self._reset()
                    if row >= 0:
                        self._row = row
                        self._delay.start()
            elif kind in (QEvent.Type.Leave, QEvent.Type.Hide):
                self._set_hover(-1)
                self._reset()
            elif kind == QEvent.Type.Wheel:
                self._reset()
        return False

    def _set_hover(self, row: int) -> None:
        old = self._hover
        if row == old:
            return
        self._hover = row
        self._repaint_row(old)
        self._repaint_row(row)

    def _repaint_row(self, row: int) -> None:
        if row >= 0 and self._alive():
            index = self._view.model().index(row, 0)
            self._view.viewport().update(self._view.visualRect(index))

    def _reset(self) -> None:
        self._delay.stop()
        self._frame.stop()
        row, self._row = self._row, -1
        was_running, self._running = self._running, False
        self._offset = 0.0
        if was_running:
            self._repaint_row(row)

    def _begin(self) -> None:
        if not self._alive() or self._row < 0:
            return
        index = self._view.model().index(self._row, 0)
        if not index.isValid() or not (index.flags() & Qt.ItemFlag.ItemIsEnabled):
            return
        text = str(index.data(Qt.ItemDataRole.DisplayRole) or "")
        width = self._text_rect(self._view.visualRect(index)).width()
        overflow = self._view.fontMetrics().horizontalAdvance(text) - width
        if width <= 0 or overflow <= 0:
            return
        self._overflow = float(overflow)
        self._offset = 0.0
        self._running = True
        self._t0 = time.perf_counter()
        self._frame.start()
        self._repaint_row(self._row)

    def _tick(self) -> None:
        if not self._alive() or self._row < 0:
            self._frame.stop()
            return
        end = self._overflow + 2.0
        travel = end / self._SPEED_PX_S
        hold = self._END_HOLD_MS / 1000.0
        elapsed = time.perf_counter() - self._t0
        if elapsed < travel:
            progress = elapsed / travel
        elif elapsed < travel + hold:
            progress = 1.0
        elif elapsed < 2 * travel + hold:
            progress = 1.0 - (elapsed - travel - hold) / travel
        else:
            self._frame.stop()
            self._running = False
            self._offset = 0.0
            self._repaint_row(self._row)
            return
        offset = end * progress * progress * (3 - 2 * progress)
        if offset != self._offset:
            self._offset = offset
            self._repaint_row(self._row)

    def paint(self, painter, option, index) -> None:
        row = index.row()
        enabled = bool(index.flags() & Qt.ItemFlag.ItemIsEnabled)
        fill = None
        if enabled:
            if row == self._selected_row:
                fill = self._sel
            elif row == self._hover:
                fill = self._hov
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        if fill is not None and fill.alpha() > 0:
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(fill)
            painter.drawRoundedRect(
                QRectF(option.rect.adjusted(self._INSET, 1, -self._INSET, -1)), self._radius, self._radius)
        rect = self._text_rect(option.rect)
        text = str(index.data(Qt.ItemDataRole.DisplayRole) or "")
        painter.setFont(self._view.font())
        metrics = painter.fontMetrics()
        ink = QColor(self._ink)
        if not enabled:
            ink.setAlphaF(0.43)
        painter.setPen(ink)
        painter.setClipRect(rect)
        flags = int(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft)
        if self._running and row == self._row:
            painter.drawText(
                QRect(rect.left() - round(self._offset), rect.top(), metrics.horizontalAdvance(text) + 4, rect.height()),
                flags, text)
        else:
            painter.drawText(rect, flags, metrics.elidedText(text, Qt.TextElideMode.ElideRight, rect.width()))
        painter.restore()


class _EmbeddedComboPopup(QFrame):

    def __init__(self, anchor, parent, entries, current, limit, on_pick):
        super().__init__(parent)
        from PySide6.QtWidgets import QListWidget

        self._combo = anchor
        self._window = parent
        self._on_pick = on_pick
        self.setObjectName("comboPopup")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setStyleSheet(_combo_popup_qss(anchor))
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

        self._effect = QGraphicsOpacityEffect(self)
        self._effect.setOpacity(1.0)
        self.setGraphicsEffect(self._effect)
        self._fade = QPropertyAnimation(self._effect, b"opacity", self)
        self._fade.setDuration(140)
        self._fade.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._fade.finished.connect(self._settle)
        self._slide = QPropertyAnimation(self, b"pos", self)
        self._slide.setDuration(170)
        self._slide.setEasingCurve(QEasingCurve.Type.OutCubic)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 4, 0, 4)
        layout.setSpacing(0)

        font = anchor.font()
        self.view = QListWidget(self)
        self.view.setObjectName("comboList")
        self.view.setFrameShape(QFrame.Shape.NoFrame)
        self.view.setFont(font)
        self.view.ensurePolished()
        self.view.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.view.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.view.setStyleSheet("background: transparent; border: none;")
        layout.addWidget(self.view)
        self.view.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        pad_left = max(0, _menu_text_pad(anchor) - 1)
        self.view.setItemDelegate(_MarqueeItemDelegate(
            self.view, _menu_opaque("primary_text", "#f2f2f2"),
            _palette_color(manager.palette(), "nav_selected_bg") or QColor("#333333"),
            max(3, _menu_radius() - 2), current, pad_left, _TEXT_PAD_RIGHT))

        for text, enabled in entries:
            entry = QListWidgetItem(text)
            entry.setSizeHint(QSize(0, _MENU_ROW))
            if not enabled:
                entry.setFlags(entry.flags() & ~Qt.ItemFlag.ItemIsEnabled)
            self.view.addItem(entry)

        visible = len(entries)
        if limit > 0:
            visible = min(visible, limit)
        self.view.setVerticalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff if visible >= len(entries)
            else Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        metrics = self.view.fontMetrics()
        widest = max(metrics.horizontalAdvance(text) for text, _ in entries)
        needed = widest + pad_left + _TEXT_PAD_RIGHT + 2 * _MarqueeItemDelegate._INSET + 2
        self.setFixedWidth(max(anchor.width(), min(needed, _POPUP_MAX_W)))
        self.setFixedHeight(visible * _MENU_ROW + 8)
        self.view.itemClicked.connect(self._choose)

    def _choose(self, item) -> None:
        index = self.view.row(item)
        self.dismiss()
        with suppress(RuntimeError):
            self._on_pick(index)

    def popup(self) -> None:
        from PySide6.QtCore import QPoint

        combo = self._combo
        win = self._window
        top = combo.mapTo(win, QPoint(0, 0))
        x = max(2, min(top.x(), win.width() - self.width() - 2))
        y = top.y() + combo.height() + _POPUP_GAP
        if y + self.height() > win.height() - 2:
            y = top.y() - self.height() - _POPUP_GAP
        self.move(x, y)
        self.show()
        self.raise_()
        app = QApplication.instance()
        if app is not None:
            app.installEventFilter(self)
        self._effect.setOpacity(0.0)
        self._fade.setStartValue(0.0)
        self._fade.setEndValue(1.0)
        self._slide.setStartValue(QPoint(x, y - 8))
        self._slide.setEndValue(QPoint(x, y))
        self._fade.start()
        self._slide.start()

    def _settle(self) -> None:
        with suppress(RuntimeError):
            self.setGraphicsEffect(None)
        self._effect = None

    def dismiss(self, animate: bool = True) -> None:
        app = QApplication.instance()
        if app is not None:
            app.removeEventFilter(self)
        with suppress(RuntimeError):
            if getattr(self._combo, "dropMenu", None) is self:
                self._combo.dropMenu = None
        if not animate or not self.isVisible():
            self._finish()
            return

        self._fade.stop()
        self._slide.stop()
        if self._effect is None:
            self._effect = QGraphicsOpacityEffect(self)
            self._effect.setOpacity(1.0)
            self.setGraphicsEffect(self._effect)
        out = QPropertyAnimation(self._effect, b"opacity", self)
        out.setDuration(90)
        out.setStartValue(self._effect.opacity())
        out.setEndValue(0.0)
        out.setEasingCurve(QEasingCurve.Type.InCubic)
        out.finished.connect(self._finish)
        self._out = out
        out.start()

    def _finish(self) -> None:
        app = QApplication.instance()
        if app is not None:
            app.removeEventFilter(self)
        with suppress(RuntimeError):
            if getattr(self._combo, "dropMenu", None) is self:
                self._combo.dropMenu = None
        self.hide()
        self.deleteLater()

    def closeEvent(self, event) -> None:
        self._finish()
        super().closeEvent(event)

    def eventFilter(self, obj, event) -> bool:
        try:
            kind = event.type()
            if kind == QEvent.Type.MouseButtonPress:
                point = event.globalPosition().toPoint()
                inside = self.rect().contains(self.mapFromGlobal(point))
                over = self._combo.rect().contains(self._combo.mapFromGlobal(point))
                if not inside and not over:
                    self.dismiss()
                return False
            if kind == QEvent.Type.KeyPress and event.key() == Qt.Key.Key_Escape:
                self.dismiss()
                return True
            if obj is self._window and kind in (QEvent.Type.Resize, QEvent.Type.Move,
                                                QEvent.Type.WindowDeactivate):
                self.dismiss()
        except RuntimeError:
            self.dismiss()
        return False


def show_dropdown(anchor, entries, on_pick, current: int = -1, limit: int = -1) -> None:
    window = anchor.window()
    if window is None or not entries:
        return
    old = getattr(anchor, "dropMenu", None)
    if old is not None and shiboken6.isValid(old):
        old.dismiss()
        return
    popup = _EmbeddedComboPopup(anchor, window, entries, current, limit, on_pick)
    anchor.dropMenu = popup
    popup.popup()


def patch_combobox_menu_embedded() -> None:
    try:
        from qfluentwidgets.components.widgets import combo_box as _cb
    except Exception:
        return

    _ComboBox = getattr(_cb, "ComboBox", None)
    if _ComboBox is None or getattr(_ComboBox, "_embedded_popup_patch", False):
        return

    def _show_combo_menu(self):
        show_dropdown(
            self, [(getattr(i, "text", str(i)), getattr(i, "isEnabled", True)) for i in self.items],
            self._onItemClicked, self.currentIndex(), self.maxVisibleItems())

    _ComboBox._showComboMenu = _show_combo_menu

    original_init = _ComboBox.__init__

    def _init(self, *args, **kwargs):
        original_init(self, *args, **kwargs)
        QTimer.singleShot(0, lambda w=self: _apply_tm_rules(w) if shiboken6.isValid(w) else None)

    _ComboBox.__init__ = _init
    _ComboBox._embedded_popup_patch = True



def patch_arrow_animation() -> None:
    try:
        from qfluentwidgets.common.animation import TranslateYAnimation
    except Exception:
        return
    if getattr(TranslateYAnimation, "_pirachest_restart", False):
        return

    def _restart(self, end: float, duration: int, curve) -> None:
        self.ani.stop()
        self.ani.setEndValue(end)
        self.ani.setDuration(duration)
        self.ani.setEasingCurve(curve)
        self.ani.start()

    TranslateYAnimation._onPress = lambda self, e: _restart(
        self, self.maxOffset, 150, QEasingCurve.Type.OutQuad)
    TranslateYAnimation._onRelease = lambda self, e: _restart(
        self, 0, 500, QEasingCurve.Type.OutElastic)
    TranslateYAnimation._pirachest_restart = True


patch_combobox_menu_embedded()
patch_arrow_animation()


_COMBO_MENU_TPL = """
RoundMenu, ComboBoxMenu { background: transparent; border: none; }
MenuActionListWidget {
    background-color: @dialog_bg; border: 1px solid @dialog_border; border-radius: 9px;
    padding: 3px 0px; outline: none;
}
MenuActionListWidget::item { border-radius: 6px; margin: 1px 5px; padding-left: 8px; }
MenuActionListWidget::item { color: @primary_text; }
MenuActionListWidget::item:hover { background-color: @nav_hover_bg; color: @primary_text; }
MenuActionListWidget::item:selected,
MenuActionListWidget::item:selected:active { background-color: @nav_selected_bg; color: @primary_text; }
MenuActionListWidget::item:disabled { color: @faint_text; }
"""


def combo_menu_qss() -> str:
    return manager.qss(("combo_menu",), _COMBO_MENU_TPL)


def combo_state_css(tag: str) -> str:
    hover = _nav_state_css("nav_selected_bg", "#333333", _HOVER_SOFTEN)
    sel = f'*[tmRule="{tag}"]'
    return (f"{sel}:hover {{ background-color: {hover}; }}\n"
            f"{sel}:pressed {{ background-color: {hover}; }}")


def is_combo_button(w) -> bool:
    return "ComboBox" in _class_chain(w) and "ComboBoxMenu" not in _class_chain(w)
