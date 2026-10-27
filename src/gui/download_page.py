from __future__ import annotations

import logging
import os
import subprocess
import sys
import time
from contextlib import suppress
from functools import lru_cache

from PySide6.QtCore import QByteArray, QPoint, QPointF, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QDragEnterEvent, QDropEvent, QFont, QFontMetrics, QPainter, QPen, QPixmap
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtWidgets import (
    QAbstractButton, QDialog, QFileDialog, QFormLayout, QFrame, QHBoxLayout, QScrollArea, QVBoxLayout, QWidget,
)
from qfluentwidgets import (
    CaptionLabel, CheckBox, CompactSpinBox, DoubleSpinBox, InfoBar, InfoBarPosition, LineEdit, MessageBoxBase,
    PushButton, StrongBodyLabel, SubtitleLabel, qconfig,
)

from src.core.downloader import DLState, DownloadItem, DownloadManager, human_bytes, human_duration, human_speed
from src.core.theme import palette, scroll_area_qss
from src.core.translations import register_locale_refresh, tr

logger = logging.getLogger(__name__)

_RUNNING = (DLState.downloading, DLState.verifying)
_LEFT, _VCENTER, _RIGHT, _HCENTER = (Qt.AlignmentFlag.AlignLeft, Qt.AlignmentFlag.AlignVCenter,
                                     Qt.AlignmentFlag.AlignRight, Qt.AlignmentFlag.AlignHCenter)


_QC: dict[str, QColor] = {}


def _qc(value: str, alpha: float | None = None) -> QColor:
    color = _QC.get(value)
    if color is None:
        color = QColor(value)
        if not color.isValid():
            try:
                nums = [float(x) for x in value[value.index("(") + 1:value.index(")")].split(",")]
                a = nums[3] if len(nums) > 3 else 1
                color = QColor(int(nums[0]), int(nums[1]), int(nums[2]), int(a * 255) if a <= 1 else int(a))
            except (ValueError, IndexError):
                color = QColor(0, 0, 0, 0)
        _QC[value] = color
    if alpha is None:
        return color
    out = QColor(color)
    out.setAlphaF(alpha)
    return out


def _pal(key: str, alpha: float | None = None) -> QColor:
    return _qc(palette()[key], alpha)


def _font(base: QFont, px: int, weight=QFont.Weight.Normal) -> QFont:
    font = QFont(base)
    font.setPixelSize(px)
    font.setWeight(weight)
    return font


def _text(p: QPainter, rect: QRectF, text: str, font: QFont, color: QColor, align=_LEFT) -> None:
    if not text or rect.width() < 4:
        return
    p.setFont(font)
    p.setPen(color)
    p.drawText(rect, int(align | _VCENTER), QFontMetrics(font).elidedText(text, Qt.TextElideMode.ElideRight, int(rect.width())))


_SHAPES = {
    "plus": '<path d="M5 12h14M12 5v14"/>',
    "pause": '<rect x="14" y="4" width="4" height="16" rx="1"/><rect x="6" y="4" width="4" height="16" rx="1"/>',
    "play": '<polygon points="6 3 20 12 6 21 6 3"/>',
    "retry": '<path d="M21 12a9 9 0 1 1-9-9c2.52 0 4.93 1 6.74 2.74L21 8"/><path d="M21 3v5h-5"/>',
    "folder": '<path d="M20 20a2 2 0 0 0 2-2V8a2 2 0 0 0-2-2h-7.9a2 2 0 0 1-1.69-.9L9.6 3.9A2 2 0 0 0 7.93 3H4a2 2 0 0 0-2 2v13a2 2 0 0 0 2 2Z"/>',
    "file": '<path d="M15 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V7Z"/><path d="M14 2v4a2 2 0 0 0 2 2h4"/>',
    "close": '<path d="M18 6 6 18M6 6l12 12"/>',
    "trash": '<path d="M3 6h18"/><path d="M19 6v14c0 1-1 2-2 2H7c-1 0-2-1-2-2V6"/><path d="M8 6V4c0-1 1-2 2-2h4c1 0 2 1 2 2v2"/><path d="M10 11v6M14 11v6"/>',
    "sliders": '<path d="M21 4h-7M10 4H3M21 12h-9M8 12H3M21 20h-5M12 20H3M14 2v4M8 10v4M16 18v4"/>',
    "down": '<path d="M12 5v14M19 12l-7 7-7-7"/>',
    "up": '<path d="M12 19V5M5 12l7-7 7 7"/>',
    "download": '<path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><path d="M7 10l5 5 5-5M12 15V3"/>',
    "check": '<path d="M20 6 9 17l-5-5"/>',
    "clock": '<circle cx="12" cy="12" r="10"/><path d="M12 6v6l4 2"/>',
    "alert": '<circle cx="12" cy="12" r="10"/><path d="M12 8v4M12 16h.01"/>',
    "grip": '<circle cx="9" cy="12" r="1"/><circle cx="9" cy="5" r="1"/><circle cx="9" cy="19" r="1"/><circle cx="15" cy="12" r="1"/><circle cx="15" cy="5" r="1"/><circle cx="15" cy="19" r="1"/>',
}


@lru_cache(maxsize=512)
def _pix(name: str, hex_color: str, alpha: float, px: int, stroke: float, dpr: float) -> QPixmap:
    svg = (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="{hex_color}" '
           f'stroke-opacity="{alpha:.2f}" stroke-width="{stroke}" stroke-linecap="round" stroke-linejoin="round">{_SHAPES[name]}</svg>')
    pm = QPixmap(int(px * dpr + .5), int(px * dpr + .5))
    pm.setDevicePixelRatio(dpr)
    pm.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pm)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    QSvgRenderer(QByteArray(svg.encode())).render(painter, QRectF(0, 0, px, px))
    painter.end()
    return pm


def _blit(p: QPainter, name: str, x: float, y: float, px: int, color: QColor, dpr: float, stroke: float = 1.75) -> None:
    p.drawPixmap(QPointF(x, y), _pix(name, color.name(), round(color.alphaF(), 2), px, stroke, round(dpr, 2)))


def _reveal(path: str, select: bool = False) -> None:
    if sys.platform.startswith("win"):
        subprocess.Popen(["explorer", "/select,", os.path.normpath(path)] if select else ["explorer", path])
    elif sys.platform == "darwin":
        subprocess.Popen(["open", "-R", path] if select else ["open", path])
    else:
        subprocess.Popen(["xdg-open", os.path.dirname(path) if select else path])


def _toast(parent, kind: str, title: str, content: str = "", duration: int = 2500) -> None:
    getattr(InfoBar, kind)(title=title, content=content, orient=Qt.Orientation.Horizontal, isClosable=True,
                           position=InfoBarPosition.TOP_RIGHT, duration=duration, parent=parent)


def _torrent_paths(event) -> list[str]:
    return [p for p in (u.toLocalFile() for u in event.mimeData().urls()) if p.lower().endswith(".torrent")]


def _ask(parent, title_key: str, content_key: str, negative_key: str) -> str:
    box = MessageBoxBase(parent)
    box.viewLayout.addWidget(StrongBodyLabel(tr(title_key)))
    body = CaptionLabel(tr(content_key))
    body.setWordWrap(True)
    box.viewLayout.addWidget(body)
    choice = {"v": "abort"}

    def pick(value: str) -> None:
        choice["v"] = value
        box.accept()

    for b in (box.yesButton, box.cancelButton):
        try:
            b.clicked.disconnect()
        except (TypeError, RuntimeError):
            pass
    box.yesButton.setText(tr("download.delete_file"))
    box.yesButton.clicked.connect(lambda: pick("delete"))
    box.cancelButton.setText(tr("download.keep_file"))
    box.cancelButton.clicked.connect(lambda: pick("keep"))
    abort = PushButton(tr(negative_key))
    abort.clicked.connect(lambda: pick("abort"))
    row = box.cancelButton.parentWidget().layout() if box.cancelButton.parentWidget() else None
    row.insertWidget(0, abort) if row is not None else box.viewLayout.addWidget(abort)
    box.exec()
    return choice["v"]


class _IconButton(QAbstractButton):
    def __init__(self, icon: str, tip: str = "", size: int = 28, glyph: int = 16, danger: bool = False, parent=None):
        super().__init__(parent)
        self._icon, self._glyph, self._danger, self._hover = icon, glyph, danger, False
        self.setFixedSize(size, size)
        self.setToolTip(tip)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    def set_icon(self, icon: str) -> None:
        if icon != self._icon:
            self._icon = icon
            self.update()

    def enterEvent(self, e):
        self._hover = True
        self.update()
        super().enterEvent(e)

    def leaveEvent(self, e):
        self._hover = False
        self.update()
        super().leaveEvent(e)

    def paintEvent(self, _e):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        on = self.isEnabled()
        if on and self._hover:
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(_pal("state_error", .14) if self._danger else _pal("primary_text", .08 if not self.isDown() else .14))
            p.drawRoundedRect(QRectF(self.rect()), 6, 6)
            fg = _pal("state_error") if self._danger else _pal("primary_text")
        else:
            fg = _pal("muted", 1.0 if on else .35)
        off = (self.width() - self._glyph) / 2
        _blit(p, self._icon, off, (self.height() - self._glyph) / 2, self._glyph, fg, self.devicePixelRatioF())


class _AddButton(QAbstractButton):
    def __init__(self, text: str, parent=None):
        super().__init__(parent)
        self._hover = False
        self.setFixedHeight(32)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setText(text)

    def setText(self, text: str) -> None:
        super().setText(text)
        self.setFixedWidth(QFontMetrics(_font(self.font(), 13, QFont.Weight.DemiBold)).horizontalAdvance(text) + 46)
        self.update()

    def enterEvent(self, e):
        self._hover = True
        self.update()
        super().enterEvent(e)

    def leaveEvent(self, e):
        self._hover = False
        self.update()
        super().leaveEvent(e)

    def paintEvent(self, _e):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        bg = _pal("accent")
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(bg.lighter(110) if self._hover else bg)
        p.drawRoundedRect(QRectF(self.rect()), 7, 7)
        white = QColor("white")
        _blit(p, "plus", 12, (self.height() - 16) / 2, 16, white, self.devicePixelRatioF(), 2.0)
        _text(p, QRectF(34, 0, self.width() - 42, self.height()), self.text(), _font(self.font(), 13, QFont.Weight.DemiBold), white)


class StatsBar(QWidget):
    _COUNTS = (("active", "download.stats_active"), ("queued", "download.stats_queued"), ("completed", "download.stats_completed"))

    def __init__(self, parent=None):
        super().__init__(parent)
        self._s = {"active": 0, "queued": 0, "completed": 0, "total_down": human_speed(0), "total_up": human_speed(0)}
        self.setFixedHeight(32)
        qconfig.themeChanged.connect(self._on_theme)

    def _on_theme(self, *_) -> None:
        self.update()

    def retranslate(self) -> None:
        self.update()

    def update_stats(self, summary: dict) -> None:
        new = {k: summary[k] for k in self._s}
        if new != self._s:
            self._s = new
            self.update()

    def paintEvent(self, _e):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        base, dpr, h = self.font(), self.devicePixelRatioF(), self.height()
        strong, plain = _font(base, 13, QFont.Weight.DemiBold), _font(base, 13)
        fm_s, fm_p = QFontMetrics(strong), QFontMetrics(plain)
        x = 2
        for key, label in self._COUNTS:
            num, name = str(self._s[key]), tr(label)
            _text(p, QRectF(x, 0, 200, h), num, strong, _pal("primary_text"))
            x += fm_s.horizontalAdvance(num) + 5
            _text(p, QRectF(x, 0, 200, h), name, plain, _pal("muted"))
            x += fm_p.horizontalAdvance(name) + 22
        x -= 8
        p.setPen(_pal("card_border"))
        p.drawLine(int(x), 8, int(x), h - 8)
        x += 18
        for icon, key, role in (("down", "total_down", "state_downloading"), ("up", "total_up", "state_seeding")):
            label = self._s[key]
            _blit(p, icon, x, (h - 15) / 2, 15, _pal(role), dpr, 2.0)
            _text(p, QRectF(x + 20, 0, 200, h), label, strong, _pal("primary_text"))
            x += 20 + fm_s.horizontalAdvance(label) + 20


_ROW_H = 68
_BTN = 28
_RESERVE = 12 + 5 * _BTN + 4 * 2
_COLS = {"info": 176, "speed": 124, "state": 236}
_GLYPH = {DLState.downloading: "down", DLState.verifying: "retry", DLState.paused: "pause", DLState.seeding: "up",
          DLState.completed: "check", DLState.error: "alert", DLState.cancelled: "close", DLState.queued: "clock"}


class DownloadItemWidget(QWidget):
    request_pause = Signal(str)
    request_resume = Signal(str)
    request_cancel = Signal(str)
    request_retry = Signal(str)
    request_remove = Signal(str)
    request_open_file = Signal(str)
    request_open_folder = Signal(str)
    request_settings = Signal(str)
    drag_started = Signal(str)
    drag_moved = Signal(str, int)
    drag_finished = Signal(str, int)

    def __init__(self, item: DownloadItem, can_retry: bool, parent=None):
        super().__init__(parent)
        self.item_id, self._item, self._can_retry = item.id, item, can_retry
        self._hover = self._down = False
        self._flash = False
        self.dragging = False
        self._v: dict = {}
        self._want: dict[str, bool] = {}
        self.setFixedHeight(_ROW_H)
        self.setMouseTracking(True)
        base = self.font()
        self._f_title = _font(base, 14, QFont.Weight.DemiBold)
        self._f_meta = _font(base, 12)
        self._f_small = _font(base, 12)
        self._f_strong = _font(base, 12, QFont.Weight.DemiBold)

        specs = (
            ("pause", "pause", "download.pause", False, self.request_pause),
            ("resume", "play", "download.resume", False, self.request_resume),
            ("retry", "retry", "download.retry", False, self.request_retry),
            ("open_file", "file", "download.open_file", False, self.request_open_file),
            ("folder", "folder", "download.open_folder", False, self.request_open_folder),
            ("settings", "sliders", "download.torrent_settings", False, self.request_settings),
            ("cancel", "close", "download.cancel", True, self.request_cancel),
            ("remove", "trash", "download.remove", True, self.request_remove),
        )
        self._buttons: dict[str, _IconButton] = {}
        for name, icon, tip, danger, signal in specs:
            b = _IconButton(icon, tr(tip), _BTN, 16, danger, self)
            b.clicked.connect(lambda _=False, s=signal: s.emit(self.item_id))
            b.hide()
            self._buttons[name] = b
        qconfig.themeChanged.connect(self._on_theme)
        self.update_from_item(item)

    def _on_theme(self, *_) -> None:
        self.update()


    def update_from_item(self, item: DownloadItem, diag: dict | None = None) -> None:
        self._item = item
        st, ext, torrent = item.state, item.backend == "external", item.backend == "torrent"
        err = st == DLState.error
        pct = 100 if st in (DLState.seeding, DLState.completed) else max(0, min(100, int(item.progress)))
        kind = tr("download.kind_torrent" if torrent else "download.kind_direct")
        meta = (item.error or tr("download.unknown_error")) if err else "  ·  ".join(x for x in (item.console, item.source, kind) if x)
        down, up = human_speed(item.speed_down_kbps), human_speed(item.speed_up_kbps)
        if st == DLState.seeding:
            speeds, detail = (("up", up),), tr("download.seeding_stats", time=human_duration(item.seed_seconds), ratio=f"{item.ratio:.2f}", peers=item.peers)
        elif st == DLState.completed:
            speeds, detail = (), tr("download.done")
        elif st == DLState.queued:
            speeds, detail = (), tr("download.waiting_for_slot")
        elif st in (DLState.error, DLState.paused, DLState.cancelled):
            speeds, detail = (), ""
        elif torrent:
            speeds = (("down", down), ("up", up))
            detail = tr("download.eta_peers_seeds", eta=human_duration(item.eta), peers=item.peers, seeds=item.seeds)
        else:
            speeds = (("down", down),)
            detail = tr("download.eta_left", eta=human_duration(item.eta)) if item.eta >= 0 else ""
        total = human_bytes(item.total_bytes) if item.total_bytes else "?"
        view = {
            "title": item.game_name or tr("download.unknown"), "meta": meta, "err": err, "state": st, "pct": pct,
            "pct_text": "" if err else f"{pct}%", "size_text": "" if err else f"{human_bytes(item.downloaded_bytes)} / {total}",
            "speeds": speeds, "detail": detail, "label": tr(f"download.state_{st.name}"),
        }
        if view != self._v:
            self._v = view
            self.update()

        live = torrent and st in (*_RUNNING, DLState.seeding, DLState.queued)
        tip = item.error if err else (self._diag_tip(diag) if diag is not None and live else "")
        if tip != self.toolTip():
            self.setToolTip(tip)

        repack = item.category == "repacks"
        self._buttons["open_file"].set_icon("play" if repack else "file")
        self._buttons["open_file"].setToolTip(tr("download.install" if repack else "download.open_file"))
        self._buttons["pause"].setToolTip(tr("download.pause_seeding" if st == DLState.seeding else "download.pause"))
        finished = st in (DLState.completed, DLState.seeding)
        can_cancel = st in (*_RUNNING, DLState.queued, DLState.paused, DLState.seeding)
        self._want = {
            "pause": not ext and st in (*_RUNNING, DLState.seeding),
            "resume": not ext and (st == DLState.paused or (torrent and st == DLState.completed)),
            "retry": st in (DLState.error, DLState.cancelled) and self._can_retry,
            "open_file": finished, "folder": True, "settings": torrent,
            "cancel": can_cancel, "remove": not can_cancel,
        }
        self._buttons["open_file"].setEnabled(bool(item.download_path))
        self._buttons["folder"].setEnabled(bool(item.download_path) or finished)
        self._buttons["settings"].setEnabled(st not in (DLState.completed, DLState.cancelled))
        self._sync_buttons()

    @staticmethod
    def _diag_tip(diag: dict) -> str:
        if not diag.get("dht_enabled", True):
            nat = tr("download.network_dht_disabled")
        else:
            nat = tr("download.network_nat_open" if diag.get("has_incoming_connections") else "download.network_nat_closed")
        return tr("download.network_tooltip", dht_nodes=diag.get("dht_nodes", 0), nat_status=nat, port=diag.get("listen_port", 0))


    _SLOT = {"cancel": 0, "remove": 0, "pause": 1, "resume": 1, "retry": 1, "settings": 2, "folder": 3, "open_file": 4}

    def _sync_buttons(self) -> None:
        y = (self.height() - _BTN) // 2
        for name, b in self._buttons.items():
            show = self._want.get(name, False)
            b.move(self.width() - 12 - _BTN - self._SLOT[name] * (_BTN + 2), y)
            if b.isVisible() != show:
                b.setVisible(show)

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self._sync_buttons()


    def paintEvent(self, _e):
        v = self._v
        if not v:
            return
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        w, h, st, dpr = self.width(), self.height(), v["state"], self.devicePixelRatioF()
        if self._flash:
            p.fillRect(self.rect(), _pal("accent", .14))
        elif self.dragging:
            p.fillRect(self.rect(), _pal("accent", .10))
        elif self._hover:
            p.fillRect(self.rect(), _pal("primary_text", .045))
        p.setPen(_pal("card_border"))
        p.drawLine(0, h - 1, w, h - 1)

        muted, text = _pal("muted"), _pal("primary_text")
        color = _pal(f"state_{st.name}")
        _blit(p, "grip", 6, (h - 16) / 2, 16, _pal("muted", .8 if self._hover else .25), dpr, 1.5)
        _blit(p, _GLYPH[st], 30, (h - 20) / 2, 20, color, dpr, 2.0)


        tx, edge = 68, w - _RESERVE - 20
        budget = edge - tx - 300
        pos, show = {}, []
        for name in ("state", "speed", "info"):
            if budget >= _COLS[name] + 28:
                show.append(name)
                budget -= _COLS[name] + 28
        x = edge
        for name in ("state", "speed", "info"):
            if name in show:
                x -= _COLS[name]
                pos[name] = x
                x -= 28
        tw = (x if show else edge) - tx

        _text(p, QRectF(tx, 12, tw, 20), v["title"], self._f_title, text)
        _text(p, QRectF(tx, 32, tw, 17), v["meta"], self._f_meta, _pal("state_error") if v["err"] else muted)
        if not v["err"]:
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(_pal("primary_text", .10))
            p.drawRoundedRect(QRectF(tx, 54, tw, 4), 2, 2)
            if v["pct"] > 0:
                p.setBrush(color)
                p.drawRoundedRect(QRectF(tx, 54, max(4.0, tw * v["pct"] / 100), 4), 2, 2)

        if "info" in pos and not v["err"]:
            _text(p, QRectF(pos["info"], 13, _COLS["info"], 20), v["pct_text"], self._f_strong, text)
            _text(p, QRectF(pos["info"], 33, _COLS["info"], 18), v["size_text"], self._f_small, muted)

        if "speed" in pos:
            right, fm = pos["speed"] + _COLS["speed"], QFontMetrics(self._f_strong)
            for i, (kind, label) in enumerate(v["speeds"]):
                y = 13 + i * 20
                tint = _pal("state_downloading" if kind == "down" else "state_seeding")
                _text(p, QRectF(pos["speed"], y, _COLS["speed"], 18), label, self._f_strong if i == 0 else self._f_small, text if i == 0 else muted, _RIGHT)
                _blit(p, kind, right - fm.horizontalAdvance(label) - 19, y + 2, 14, tint, dpr, 2.0)

        if "state" in pos:
            _text(p, QRectF(pos["state"], 13, _COLS["state"], 20), v["label"], self._f_strong, color)
            _text(p, QRectF(pos["state"], 33, _COLS["state"], 18), v["detail"], self._f_small, muted)


    def flash(self) -> None:
        self._flash = True
        self.update()
        QTimer.singleShot(900, self._end_flash)

    def _end_flash(self) -> None:
        self._flash = False
        self.update()

    def enterEvent(self, e):
        self._hover = True
        self._sync_buttons()
        self.update()
        super().enterEvent(e)

    def leaveEvent(self, e):
        self._hover = False
        self._sync_buttons()
        self.update()
        super().leaveEvent(e)

    def mousePressEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton and e.position().x() < 30:
            self._down = True
            self.drag_started.emit(self.item_id)

    def mouseMoveEvent(self, e):
        if self._down:
            self.drag_moved.emit(self.item_id, int(e.globalPosition().y()))
        else:
            self.setCursor(Qt.CursorShape.SizeVerCursor if e.position().x() < 30 else Qt.CursorShape.ArrowCursor)

    def mouseReleaseEvent(self, e):
        if self._down and e.button() == Qt.MouseButton.LeftButton:
            self._down = False
            self.drag_finished.emit(self.item_id, int(e.globalPosition().y()))


class _ReorderList(QScrollArea):
    order_changed = Signal(list)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWidgetResizable(True)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._body = QWidget()
        self._body.setObjectName("downloadListBody")
        self._lay = QVBoxLayout(self._body)
        self._lay.setContentsMargins(0, 0, 0, 16)
        self._lay.setSpacing(0)
        self._lay.addStretch(1)
        self.setWidget(self._body)
        self._rows: list[DownloadItemWidget] = []
        self._line = QFrame(self._body)
        self._line.setFixedHeight(2)
        self._line.hide()
        self._drag: DownloadItemWidget | None = None
        self._drop = 0

    def add_row(self, row: DownloadItemWidget) -> None:
        self._lay.insertWidget(len(self._rows), row)
        self._rows.append(row)
        row.drag_started.connect(self._begin)
        row.drag_moved.connect(self._move)
        row.drag_finished.connect(self._end)

    def remove_row(self, row: DownloadItemWidget) -> None:
        if row in self._rows:
            self._rows.remove(row)
            self._lay.removeWidget(row)

    def _begin(self, item_id: str) -> None:
        self._drag = next((r for r in self._rows if r.item_id == item_id), None)
        if self._drag is not None:
            self._drag.dragging = True
            self._drag.update()
        self._line.setStyleSheet(f"background-color: {palette()['accent']};")

    def _move(self, _id: str, gy: int) -> None:
        if self._drag is None:
            return
        vp, bar = self.viewport(), self.verticalScrollBar()
        top = vp.mapToGlobal(QPoint(0, 0)).y()
        if gy < top + 36:
            bar.setValue(bar.value() - 18)
        elif gy > top + vp.height() - 36:
            bar.setValue(bar.value() + 18)
        ly = self._body.mapFromGlobal(QPoint(0, gy)).y()
        self._drop = next((i for i, r in enumerate(self._rows) if ly < r.y() + r.height() / 2), len(self._rows))
        y = self._rows[self._drop].y() - 1 if self._drop < len(self._rows) else self._rows[-1].y() + self._rows[-1].height() - 1
        self._line.setGeometry(0, max(0, y), self._body.width(), 2)
        self._line.show()
        self._line.raise_()

    def _end(self, item_id: str, gy: int) -> None:
        row = self._drag
        if row is None:
            return
        self._move(item_id, gy)
        self._line.hide()
        self._drag = None
        row.dragging = False
        row._sync_buttons()
        row.update()
        src, dst = self._rows.index(row), self._drop
        dst -= dst > src
        if dst == src:
            return
        self._rows.insert(dst, self._rows.pop(src))
        self._lay.removeWidget(row)
        self._lay.insertWidget(dst, row)
        self.order_changed.emit([r.item_id for r in self._rows])


class _EmptyState(QWidget):
    def paintEvent(self, _e):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        cx, cy, base = self.width() / 2, self.height() * .38, self.font()
        _blit(p, "download", cx - 14, cy - 14, 28, _pal("muted", .8), self.devicePixelRatioF(), 1.5)
        _text(p, QRectF(0, cy + 26, self.width(), 22), tr("download.no_downloads_yet"), _font(base, 15, QFont.Weight.DemiBold), _pal("primary_text"), _HCENTER)
        _text(p, QRectF(0, cy + 50, self.width(), 20), tr("download.empty_hint"), _font(base, 12), _pal("muted"), _HCENTER)


class TorrentSettingsDialog(MessageBoxBase):
    _ROWS = (
        ("max_down_kbps", "download.max_download_speed", CompactSpinBox, "download.suffix_kbps_unlimited", (0, 1_000_000)),
        ("max_up_kbps", "download.max_upload_speed", CompactSpinBox, "download.suffix_kbps_unlimited", (0, 1_000_000)),
        ("max_peers", "download.max_connections", CompactSpinBox, None, (1, 1000)),
        ("ratio_limit", "download.seed_ratio_limit", DoubleSpinBox, "download.suffix_unlimited", (0, 100)),
        ("seed_time_limit_min", "download.seed_time_limit", CompactSpinBox, "download.suffix_min_unlimited", (0, 100_000)),
    )

    def __init__(self, item: DownloadItem, parent=None):
        super().__init__(parent)
        self.recheck_requested = False
        self.widget.setMinimumWidth(460)
        self.viewLayout.addWidget(SubtitleLabel(tr("download.torrent_settings_for", name=item.game_name), self))
        self._seed = CheckBox(tr("download.seed_after_completion"))
        self._seed.setChecked(item.seed_after)
        self.viewLayout.addWidget(self._seed)
        form = QFormLayout()
        form.setSpacing(12)
        self._fields = {}
        for key, label, cls, suffix, rng in self._ROWS:
            widget = cls()
            widget.setRange(*rng)
            if suffix:
                widget.setSuffix(tr(suffix))
            if key == "ratio_limit":
                widget.setSingleStep(0.1)
            widget.setValue(getattr(item, key))
            form.addRow(tr(label), widget)
            self._fields[key] = widget
        self.viewLayout.addLayout(form)
        row = QHBoxLayout()
        recheck = PushButton(tr("download.force_recheck"))
        recheck.clicked.connect(self._recheck)
        row.addWidget(recheck)
        row.addStretch(1)
        self.viewLayout.addLayout(row)
        self.yesButton.setText(tr("download.save"))
        self.cancelButton.setText(tr("download.cancel"))

    def _recheck(self) -> None:
        self.recheck_requested = True
        self.accept()

    def values(self) -> dict:
        return {"seed_after": self._seed.isChecked(), **{k: w.value() for k, w in self._fields.items()}}


class _DropZone(QWidget):
    files_dropped = Signal(list)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAcceptDrops(True)
        self.setFixedHeight(112)
        self._active = False

    def paintEvent(self, _e):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setPen(QPen(_pal("accent", .9) if self._active else _pal("primary_text", .22), 1, Qt.PenStyle.DashLine))
        p.setBrush(_pal("accent", .06) if self._active else Qt.BrushStyle.NoBrush)
        p.drawRoundedRect(QRectF(self.rect()).adjusted(.5, .5, -.5, -.5), 8, 8)
        _blit(p, "download", self.width() / 2 - 11, 26, 22, _pal("accent") if self._active else _pal("muted"), self.devicePixelRatioF(), 1.6)
        _text(p, QRectF(0, 62, self.width(), 22), tr("download.drop_hint"), _font(self.font(), 12), _pal("muted"), _HCENTER)

    def dragEnterEvent(self, e: QDragEnterEvent) -> None:
        if e.mimeData().hasUrls():
            self._active = True
            self.update()
            e.acceptProposedAction()

    def dragLeaveEvent(self, _e) -> None:
        self._active = False
        self.update()

    def dropEvent(self, e: QDropEvent) -> None:
        self._active = False
        self.update()
        if paths := _torrent_paths(e):
            self.files_dropped.emit(paths)
        e.acceptProposedAction()


class AddTorrentDialog(MessageBoxBase):
    torrent_file_chosen = Signal(str)
    magnet_submitted = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._chosen = ""
        self.viewLayout.addWidget(SubtitleLabel(tr("download.add_torrent"), self))
        zone = _DropZone(self)
        zone.files_dropped.connect(lambda paths: self._choose(paths[0]))
        self.viewLayout.addWidget(zone)
        browse = PushButton(tr("download.browse_torrent_file"))
        browse.clicked.connect(self._browse)
        row = QHBoxLayout()
        row.addStretch(1)
        row.addWidget(browse)
        row.addStretch(1)
        self.viewLayout.addLayout(row)
        self.viewLayout.addSpacing(6)
        hint = CaptionLabel(tr("download.link_hint"))
        hint.setStyleSheet(f"color: {palette()['muted']};")
        self.viewLayout.addWidget(hint)
        self._link = LineEdit(self)
        self._link.setPlaceholderText(tr("download.link_placeholder"))
        self._link.setClearButtonEnabled(True)
        self.viewLayout.addWidget(self._link)
        self.yesButton.setText(tr("download.add"))
        self.cancelButton.setText(tr("download.cancel"))
        self.widget.setMinimumWidth(460)

    def _choose(self, path: str) -> None:
        self._chosen = path
        self._link.clear()
        self.accept()

    def _browse(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, tr("download.browse_torrent_file"), "", tr("download.torrent_file_filter"))
        if path:
            self._choose(path)

    def accept(self) -> None:
        link = self._link.text().strip()
        if self._chosen:
            self.torrent_file_chosen.emit(self._chosen)
        elif link.lower().startswith("magnet:"):
            self.magnet_submitted.emit(link)
        else:
            return
        super().accept()


class DownloadManagerPage(QWidget):
    def __init__(self, manager: DownloadManager, parent=None):
        super().__init__(parent)
        self.setStyleSheet("background: transparent;")
        self.setAcceptDrops(True)
        self._manager = manager
        self._filelist_thread = None
        self._rows: dict[str, DownloadItemWidget] = {}
        self._pending: set[str] = set()
        self._diag: dict = {}
        self._diag_at = 0.0
        self._flush = QTimer(self, singleShot=True)
        self._flush.timeout.connect(self._flush_updates)

        root = QVBoxLayout(self)
        root.setContentsMargins(28, 18, 28, 0)
        root.setSpacing(0)
        top = QHBoxLayout()
        top.setContentsMargins(0, 0, 0, 14)
        top.setSpacing(4)
        self._stats = StatsBar()
        top.addWidget(self._stats, 1)
        self._actions: dict[str, tuple[QAbstractButton, str]] = {}
        add = _AddButton(tr("download.add_torrent"))
        add.clicked.connect(self._on_add)
        top.addWidget(add, 0, _VCENTER)
        self._actions["add"] = (add, "download.add_torrent")
        top.addSpacing(10)
        for name, icon, slot, danger, handler in (
            ("pause_all", "pause", "download.pause_all", False, manager.pause_all),
            ("resume_all", "play", "download.resume_all", False, manager.resume_all),
            ("delete_all", "trash", "download.delete_all", True, self._on_delete_all),
            ("open_root", "folder", "download.open_downloads_folder", False, self._on_open_root),
        ):
            b = _IconButton(icon, tr(slot), 32, 17, danger)
            b.clicked.connect(lambda _=False, h=handler: h())
            top.addWidget(b, 0, _VCENTER)
            self._actions[name] = (b, slot)
        root.addLayout(top)
        self._sep = QFrame()
        self._sep.setFixedHeight(1)
        root.addWidget(self._sep)

        self._list = _ReorderList()
        self._list.setObjectName("downloadQueueList")
        self._list.order_changed.connect(manager.reorder)
        self._empty = _EmptyState()
        root.addWidget(self._list, 1)
        root.addWidget(self._empty, 1)
        self._apply_theme()
        qconfig.themeChanged.connect(self._on_theme)

        manager.item_added.connect(self._on_added)
        manager.item_updated.connect(self._on_updated)
        manager.item_removed.connect(self._on_removed)
        manager.stats_changed.connect(lambda: self._stats.update_stats(manager.summary()))
        for item in manager.items_in_order():
            self._insert(item)
        self._refresh_visibility()
        self._stats.update_stats(manager.summary())
        register_locale_refresh(self, self._on_locale)

    def _on_theme(self, *_) -> None:
        self._apply_theme()
        self._empty.update()

    def _apply_theme(self) -> None:
        self._sep.setStyleSheet(f"background-color: {palette()['card_border']}; border: none;")
        self._list.setStyleSheet(
            f"QScrollArea#downloadQueueList {{ background: transparent; border: none; }} "
            f"QWidget#downloadListBody {{ background: transparent; }} {scroll_area_qss()}"
        )

    def _on_locale(self) -> None:
        for button, slot in self._actions.values():
            if isinstance(button, _AddButton):
                button.setText(tr(slot))
            else:
                button.setToolTip(tr(slot))
        self._stats.retranslate()
        self._empty.update()
        self._pending.update(self._rows)
        self._flush_updates()


    def _insert(self, item: DownloadItem) -> None:
        row = DownloadItemWidget(item, self._manager.can_retry(item.id))
        m = self._manager
        row.request_pause.connect(m.pause)
        row.request_resume.connect(m.resume)
        row.request_retry.connect(m.retry)
        row.request_cancel.connect(self._on_cancel)
        row.request_remove.connect(self._on_remove)
        row.request_open_file.connect(self._on_open_file)
        row.request_open_folder.connect(self._on_open_folder)
        row.request_settings.connect(self._on_settings)
        self._list.add_row(row)
        self._rows[item.id] = row

    def _refresh_visibility(self) -> None:
        has = bool(self._rows)
        self._list.setVisible(has)
        self._empty.setVisible(not has)

    def _on_added(self, item_id: str) -> None:
        item = self._manager.get(item_id)
        if item is not None and item_id not in self._rows:
            self._insert(item)
            self._refresh_visibility()

    def _on_updated(self, item_id: str) -> None:
        self._pending.add(item_id)
        if not self._flush.isActive():
            self._flush.start(0)

    def _flush_updates(self) -> None:
        pending, self._pending = self._pending, set()
        if time.monotonic() - self._diag_at >= 1.0:
            self._diag, self._diag_at = self._manager.network_diagnostics(), time.monotonic()
        for item_id in pending:
            item, row = self._manager.get(item_id), self._rows.get(item_id)
            if item is not None and row is not None:
                row.update_from_item(item, self._diag)

    def _on_removed(self, item_id: str) -> None:
        row = self._rows.pop(item_id, None)
        if row is None:
            return
        self._list.remove_row(row)
        row.setParent(None)
        row.deleteLater()
        self._refresh_visibility()


    def _on_cancel(self, item_id: str) -> None:
        choice = _ask(self.window(), "download.cancel_download_title", "download.cancel_download_content", "download.no")
        if choice != "abort":
            self._manager.cancel(item_id, delete_files=choice == "delete")

    def _on_remove(self, item_id: str) -> None:
        choice = _ask(self.window(), "download.remove_download_title", "download.remove_download_content", "download.cancel_action")
        if choice != "abort":
            self._manager.remove(item_id, delete_files=choice == "delete")

    def _on_delete_all(self) -> None:
        if not self._manager.items_in_order():
            return
        choice = _ask(self.window(), "download.delete_all_title", "download.delete_all_content", "download.cancel_action")
        if choice != "abort":
            self._manager.remove_all(delete_files=choice == "delete")

    def _on_settings(self, item_id: str) -> None:
        item = self._manager.get(item_id)
        if item is None:
            return
        dialog = TorrentSettingsDialog(item, parent=self.window())
        if dialog.exec():
            self._manager.set_torrent_settings(item_id, **dialog.values())
            if dialog.recheck_requested:
                self._manager.force_recheck(item_id)

    def _on_open_root(self) -> None:
        from src.core.config import paths
        try:
            os.makedirs(paths.download_root, exist_ok=True)
            _reveal(paths.download_root)
        except OSError:
            logger.exception("Failed to open downloads root")

    def focus_item(self, item_id: str) -> None:
        row = self._rows.get(item_id)
        if row is None:
            item = self._manager.get(item_id)
            if item is not None:
                self._insert(item)
                self._refresh_visibility()
                row = self._rows.get(item_id)
        if row is None:
            return
        with suppress(Exception):
            self._list.ensureWidgetVisible(row, 0, 12)
        row.flash()

    def open_item_folder(self, item_id: str) -> None:
        self._on_open_folder(item_id)

    def _on_open_file(self, item_id: str) -> None:
        item = self._manager.get(item_id)
        if item and item.category == "repacks":
            from src.core import worker
            worker.submit(self._manager.find_repack_installer, (item_id,), on_done=lambda p: self._open_found(p, True),
                          on_error=lambda _e: self._open_found(None, True), owner=self)
            return
        path = self._manager.open_file(item_id)
        if path:
            _reveal(path)
        else:
            self._open_found(None)

    def _open_found(self, path, installer: bool = False) -> None:
        if not path:
            prefix = "download.installer_not_found" if installer else "download.folder_not_found"
            _toast(self.window(), "warning", tr(f"{prefix}_title"), tr(f"{prefix}_content"), 3000)
            return
        try:
            _reveal(path)
        except OSError:
            logger.exception("Failed to open %s", path)

    def _on_open_folder(self, item_id: str) -> None:
        folder = self._manager.open_folder(item_id)
        if not folder:
            self._open_found(None)
            return
        item = self._manager.get(item_id)
        target = item.download_path if item and os.path.isfile(item.download_path) else None
        try:
            _reveal(target, select=True) if target else _reveal(folder)
        except OSError:
            logger.exception("Failed to open folder %s", folder)


    def dragEnterEvent(self, event: QDragEnterEvent) -> None:
        if event.mimeData().hasUrls() and _torrent_paths(event):
            event.acceptProposedAction()

    def dropEvent(self, event: QDropEvent) -> None:
        for path in _torrent_paths(event):
            self._start_selective_add(path)
        event.acceptProposedAction()

    def _on_add(self) -> None:
        dialog = AddTorrentDialog(self.window())
        dialog.torrent_file_chosen.connect(self._start_selective_add)
        dialog.magnet_submitted.connect(self._start_selective_add)
        dialog.exec()

    @staticmethod
    def _display_name(source: str) -> str:
        if source.startswith("magnet:"):
            from src.core.repacks.base import magnet_display_name
            return magnet_display_name(source) or tr("download.rom_fallback")
        return os.path.splitext(os.path.basename(source))[0]

    def _start_selective_add(self, source: str, source_label: str | None = None) -> None:
        from .repacks_page import _FileListFetchThread, send_to_external_torrent_client
        if send_to_external_torrent_client(source, parent=self.window()):
            return
        if self._filelist_thread is not None and self._filelist_thread.isRunning():
            _toast(self.window(), "warning", tr("download.loading_file_list"), duration=2000)
            return
        name = self._display_name(source)
        thread = _FileListFetchThread(self._manager, source, None)
        thread.finished_ok.connect(lambda files: self._on_file_list(source, files, name, source_label))
        thread.finished_err.connect(lambda err: (logger.error("File list failed: %s", err), self._queue(source, None, name, source_label)))
        thread.finished.connect(self._on_thread_done)
        self._filelist_thread = thread
        thread.start()

    def _on_thread_done(self) -> None:
        thread, self._filelist_thread = self._filelist_thread, None
        if thread is not None:
            thread.deleteLater()

    def _on_file_list(self, source: str, files: list, name: str, label: str | None) -> None:
        if not files:
            self._queue(source, None, name, label)
            return
        from .repacks_page import (
            SelectiveDownloadDialog, build_selective_entries_from_torrent, resolve_selective_file_indices,
        )
        names = [p for p, _ in files]
        entries = build_selective_entries_from_torrent(names, [s for _, s in files])
        dialog = SelectiveDownloadDialog(entries, parent=self.window(), subtitle=name)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        self._queue(source, resolve_selective_file_indices(entries, dialog.selected_labels(), names) or None, name, label)

    def _queue(self, source: str, file_ids: list | None, name: str, label: str | None) -> None:
        self._manager.add(torrent_file=source, file_id=file_ids[0] if file_ids else 1, file_ids=file_ids, game_name=name,
                          console=tr("download.unknown"), source=label or "Manual", category="repacks")
        _toast(self.window(), "success", tr("download.added_to_queue_title"), tr("download.added_to_queue_single", title=name))

    def add_from_rom(self, rom: dict) -> None:
        if self._add_rom(rom):
            _toast(self.window(), "success", tr("download.added_to_queue_title"),
                   tr("download.added_to_queue_single", title=rom.get("title") or tr("download.rom_fallback")))
        else:
            _toast(self.window(), "warning", tr("download.no_torrent_title"), tr("download.no_torrent_content"), 3000)

    def add_many_from_roms(self, roms: list[dict]) -> None:
        added = sum(self._add_rom(rom) for rom in roms)
        if added:
            _toast(self.window(), "success", tr("download.added_to_queue_title"), tr("download.added_to_queue_many", count=added))

    def _add_rom(self, rom: dict) -> bool:
        torrent = rom.get("torrent_file", "")
        if not torrent:
            return False
        try:
            file_id = int(rom.get("file_id") or 1)
        except (TypeError, ValueError):
            file_id = 1
        self._manager.add(torrent_file=torrent, file_id=file_id, game_name=rom.get("title", "rom"),
                          console=rom.get("console") or tr("download.unknown"), source=rom.get("source", "Minerva"),
                          category="minerva")
        return True
