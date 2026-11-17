from __future__ import annotations

import logging
import math
from typing import Optional

from PySide6.QtCore import (
    QElapsedTimer, QPoint, QRect, QRectF, QSettings, QSize, Qt, QTimer, Signal,
)
from PySide6.QtGui import (
    QColor, QCursor, QFont, QFontMetrics, QGuiApplication, QIcon, QPainter, QPainterPath, QPen, QPixmap,
)
from PySide6.QtWidgets import (
    QDoubleSpinBox, QFrame, QHBoxLayout, QLabel, QSlider, QStyle, QToolTip, QVBoxLayout, QWidget,
)
from PySide6.QtWidgets import QPushButton as _QPushButton
from qfluentwidgets import FluentIcon
from qfluentwidgets import Theme as _Theme

from src.core.config import settings
from src.core.theme import palette
from src.core.translations import tr

logger = logging.getLogger(__name__)


def _format_ms(ms: int) -> str:
    total_s = max(0, ms) // 1000
    h, rem = divmod(total_s, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"


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
        self.setMinimumSize(self._MIN_W, self._MIN_VH + self._player._BAR_H)
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
        a, bar = self._aspect(), self._player._BAR_H
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
            h = int(w * 9 / 16) + self._player._BAR_H
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
        bar = self._player._BAR_H
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
