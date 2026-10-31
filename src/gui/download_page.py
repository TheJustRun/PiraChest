from __future__ import annotations

import logging
import os
import subprocess
import sys
import time
from contextlib import suppress
from functools import lru_cache

from PySide6.QtCore import QAbstractItemModel, QByteArray, QEvent, QModelIndex, QPoint, QPointF, QRectF, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QDragEnterEvent, QDropEvent, QFont, QFontMetrics, QGuiApplication, QPainter, QPen, QPixmap
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtWidgets import (
    QAbstractButton, QAbstractItemView, QDialog, QFileDialog, QFormLayout, QFrame, QGridLayout, QHBoxLayout, QHeaderView, QScrollArea,
    QSplitter, QStackedWidget, QStyle, QStyledItemDelegate, QTreeView, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget,
)
from qfluentwidgets import (
    Action, CaptionLabel, CheckBox, Pivot, CompactSpinBox, DoubleSpinBox, InfoBar, InfoBarPosition, LineEdit, MessageBoxBase,
    PushButton, RoundMenu, StrongBodyLabel, SubtitleLabel, TransparentPushButton, qconfig,
)

from src.core.downloader import (
    DLState, DownloadItem, DownloadManager, human_bytes, human_duration, human_speed, is_risky_file,
)
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


def _download_title(item) -> str:
    name = item.game_name or tr("download.unknown")
    if str(getattr(item, "category", "") or "") == "repacks":
        try:
            from src.core.repacks.base import display_title

            return display_title(name)
        except Exception:
            pass
    return name


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
    "chev_r": '<path d="m9 18 6-6-6-6"/>',
    "chev_d": '<path d="m6 9 6 6 6-6"/>',
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

    select = select or os.path.isfile(path)
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
    clicked = Signal(str)

    def __init__(self, item: DownloadItem, can_retry: bool, parent=None):
        super().__init__(parent)
        self.item_id, self._item, self._can_retry = item.id, item, can_retry
        self._hover = self._down = self._sel = False
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
        pct = 100 if st in (DLState.seeding, DLState.completed) else round(max(0.0, min(100.0, item.progress)), 1)
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
            "title": _download_title(item), "meta": meta, "err": err, "state": st, "pct": pct,
            "pct_text": "" if err else f"{pct:g}%", "size_text": "" if err else f"{human_bytes(item.downloaded_bytes)} / {total}",
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
        if self._sel:
            p.fillRect(self.rect(), _pal("accent", .07))
            p.fillRect(0, 0, 3, h, _pal("accent"))
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


    def set_selected(self, on: bool) -> None:
        if on != self._sel:
            self._sel = on
            self.update()

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
        elif e.button() == Qt.MouseButton.LeftButton and self.rect().contains(e.position().toPoint()):
            self.clicked.emit(self.item_id)


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
        self.viewLayout.addWidget(SubtitleLabel(tr("download.torrent_settings_for", name=_download_title(item)), self))
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


_PRIO_KEYS = {0: "files.prio_skip", 1: "files.prio_low", 4: "files.prio_normal", 6: "files.prio_high", 7: "files.prio_max"}
_FCOLS = ("files.col_name", "files.col_size", "files.col_done", "files.col_progress", "files.col_status",
         "files.col_priority", "files.col_speed", "files.col_eta")
_PROGRESS_ROLE = Qt.ItemDataRole.UserRole + 1
_CHECKED, _UNCHECKED, _PARTIAL = Qt.CheckState.Checked, Qt.CheckState.Unchecked, Qt.CheckState.PartiallyChecked


class _Node:
    __slots__ = ("parent", "kids", "name", "idx", "size", "done", "speed", "prio", "last", "row", "flags", "wanted", "n", "rem", "path")

    def __init__(self, parent, name, idx=-1, size=0, flags=0, path=""):
        self.parent, self.kids, self.name, self.idx, self.size, self.flags, self.path = parent, [], name, idx, size, flags, path
        self.done = self.speed = self.prio = self.last = self.row = self.wanted = self.n = self.rem = 0


class _FilesModel(QAbstractItemModel):
    requested = Signal(object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._root, self._files, self._dirs, self._t, self._paused = _Node(None, ""), [], [], 0.0, False
        self.visible = lambda: []
        self.warn = True

    def risky(self, k: _Node) -> bool:
        if k.idx < 0 or not k.flags & 1:
            return False
        return self.warn or os.path.splitext(k.name)[1].lower() not in (".exe", ".bat")

    def clear(self) -> None:
        self.beginResetModel()
        self._root, self._files, self._dirs, self._t = _Node(None, ""), [], [], 0.0
        self.endResetModel()

    def rebuild(self, tree) -> None:
        root, dirs, files, lookup = _Node(None, ""), [], [None] * len(tree), {}
        for i, (path, size, flags) in enumerate(tree):
            parent, parts = root, path.split("/")
            for d in range(len(parts) - 1):
                key = "/".join(parts[:d + 1])
                node = lookup.get(key)
                if node is None:
                    node = lookup[key] = _Node(parent, parts[d], path=key)
                    parent.kids.append(node)
                    dirs.append(node)
                parent = node
            files[i] = _Node(parent, parts[-1], i, size, flags, path)
            parent.kids.append(files[i])
        for d in (root, *dirs):
            d.kids.sort(key=lambda k: (k.idx >= 0, k.name.lower()))
            for r, k in enumerate(d.kids):
                k.row = r
        self.beginResetModel()
        self._root, self._files, self._dirs, self._t = root, files, dirs[::-1] + [root], 0.0
        self.endResetModel()

    def update(self, progress, prios, paused: bool) -> None:
        now = time.monotonic()
        dt, self._t, self._paused = (now - self._t if self._t else 0.0), now, paused
        for f in self._files:
            i = f.idx
            done = min(progress[i], f.size)
            if dt > 0.2:
                f.speed = .6 * max(0, done - f.done) / dt + .4 * f.speed
                if f.speed < 64:
                    f.speed = 0
            locked = f.flags & 2
            f.done, f.prio = done, prios[i]
            f.last = f.prio or f.last
            f.n = 0 if locked else 1
            f.wanted = 1 if f.prio and not locked else 0
            f.rem = f.size - done if f.wanted else 0
        for d in self._dirs:
            size = done = speed = n = wanted = rem = 0
            pr = None
            for k in d.kids:
                size += k.size; done += k.done; speed += k.speed; n += k.n; wanted += k.wanted; rem += k.rem
                if k.n:
                    pr = k.prio if pr is None else (pr if pr == k.prio else -1)
            d.size, d.done, d.speed, d.n, d.wanted, d.rem, d.prio = size, done, speed, n, wanted, rem, pr or 0
        for i in self.visible():
            self.dataChanged.emit(i, i.siblingAtColumn(len(_FCOLS) - 1))

    def leaves(self, nodes) -> list[_Node]:
        out, stack, seen = [], list(nodes), set()
        while stack:
            k = stack.pop()
            if k.idx >= 0:
                if k.n and k.idx not in seen:
                    seen.add(k.idx); out.append(k)
            else:
                stack.extend(k.kids)
        return out

    def request(self, nodes, prio: int | None) -> None:
        changes = {k.idx: (prio if prio is not None else (k.last or 4)) for k in self.leaves(nodes)}
        if changes:
            self.requested.emit(changes)

    def status(self, k: _Node) -> str:
        if not k.wanted:
            return "files.st_skipped"
        return "files.st_done" if not k.rem else "files.st_paused" if self._paused else "files.st_downloading" if k.speed else "files.st_waiting"

    def check(self, k: _Node):
        return _UNCHECKED if not k.wanted else _CHECKED if k.wanted == k.n else _PARTIAL

    def index(self, row, col, parent=QModelIndex()):
        p = parent.internalPointer() if parent.isValid() else self._root
        return self.createIndex(row, col, p.kids[row]) if 0 <= row < len(p.kids) and 0 <= col < len(_FCOLS) else QModelIndex()

    def parent(self, index=None):
        if index is None:
            return super().parent()
        p = index.internalPointer().parent if index.isValid() else None
        return self.createIndex(p.row, 0, p) if p is not None and p.parent is not None else QModelIndex()

    def rowCount(self, parent=QModelIndex()):
        if parent.column() > 0:
            return 0
        return len((parent.internalPointer() if parent.isValid() else self._root).kids)

    def columnCount(self, parent=QModelIndex()):
        return len(_FCOLS)

    def flags(self, index):
        return Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):
        if orientation == Qt.Orientation.Horizontal and role == Qt.ItemDataRole.DisplayRole:
            return tr(_FCOLS[section])
        return None

    def setData(self, index, value, role=Qt.ItemDataRole.EditRole):
        if role != Qt.ItemDataRole.CheckStateRole or index.column() != 0:
            return False
        self.request([index.internalPointer()], None if value == _CHECKED or value == 2 else 0)
        return False

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        k, col = index.internalPointer(), index.column()
        if role == Qt.ItemDataRole.CheckStateRole:
            return self.check(k) if col == 0 else None
        if role == _PROGRESS_ROLE:
            return k.done / k.size if k.size else 1.0
        if role == Qt.ItemDataRole.ToolTipRole and col == 0:
            return k.path + ("\n" + tr("files.risky_tip") if self.risky(k) else "")
        if role != Qt.ItemDataRole.DisplayRole:
            return None
        if col == 0:
            return ("\u26a0 " if self.risky(k) else "") + k.name
        if col == 1:
            return human_bytes(k.size)
        if col == 2:
            return human_bytes(k.done)
        if col == 3:
            return f"{k.done / k.size * 100:.0f}%" if k.size else "100%"
        if col == 4:
            return tr(self.status(k))
        if col == 5:
            return tr("files.prio_mixed") if k.prio < 0 else tr(_PRIO_KEYS[min(_PRIO_KEYS, key=lambda c: abs(c - k.prio))])
        if col == 6:
            return human_speed(k.speed / 1024) if k.speed else ""
        return human_duration(k.rem / k.speed) if k.speed and k.rem else ""


class _FileDelegate(QStyledItemDelegate):
    H = 22

    def __init__(self, tree):
        super().__init__(tree)
        self._f = _font(tree.font(), 12)

    def sizeHint(self, option, index):
        return QSize(0, self.H)

    def paint(self, p, opt, index):
        p.save()
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r, col, m = QRectF(opt.rect), index.column(), index.model()
        if opt.state & QStyle.StateFlag.State_Selected:
            p.fillRect(r, _pal("accent", .16))
        elif opt.state & QStyle.StateFlag.State_MouseOver:
            p.fillRect(r, _pal("primary_text", .045))
        text, muted, dpr = _pal("primary_text"), _pal("muted"), self.parent().devicePixelRatioF()
        display = m.data(index, Qt.ItemDataRole.DisplayRole)
        if col == 0:
            k, cs, cy = index.internalPointer(), m.data(index, Qt.ItemDataRole.CheckStateRole), r.center().y()
            box = QRectF(r.left() + 4, cy - 7, 14, 14)
            p.setPen(QPen(_pal("accent") if cs != _UNCHECKED else _pal("muted", .6), 1.2))
            p.setBrush(_pal("accent") if cs != _UNCHECKED else Qt.GlobalColor.transparent)
            p.drawRoundedRect(box, 3.5, 3.5)
            if cs == _CHECKED:
                _blit(p, "check", box.left() + 1.5, box.top() + 1.5, 11, QColor("white"), dpr, 2.6)
            elif cs == _PARTIAL:
                pen = QPen(QColor("white"), 2)
                pen.setCapStyle(Qt.PenCapStyle.RoundCap)
                p.setPen(pen)
                p.drawLine(QPointF(box.left() + 4, cy), QPointF(box.right() - 4, cy))
            _blit(p, "folder" if k.idx < 0 else "file", r.left() + 24, cy - 7, 14, muted, dpr, 1.6)
            risky = m.risky(k)
            _text(p, QRectF(r.left() + 43, r.top(), r.width() - 45, r.height()), display, self._f,
                  _pal("state_error") if risky else text)
        elif col == 3:
            frac = max(0.0, min(1.0, m.data(index, _PROGRESS_ROLE)))
            bar = QRectF(r.left() + 6, r.center().y() - 2, r.width() - 12, 4)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(_pal("primary_text", .10))
            p.drawRoundedRect(bar, 2, 2)
            if frac > 0:
                p.setBrush(_pal("state_completed") if frac >= 1 else _pal("accent"))
                p.drawRoundedRect(QRectF(bar.left(), bar.top(), max(4.0, bar.width() * frac), 4), 2, 2)
        else:
            right = col in (1, 2, 6, 7)
            _text(p, r.adjusted(4, 0, -8, 0), display, self._f, text if col in (1, 4, 5) else muted, _RIGHT if right else _LEFT)
        p.restore()

    def editorEvent(self, event, model, option, index):
        if index.column() == 0 and event.type() == QEvent.Type.MouseButtonRelease and event.button() == Qt.MouseButton.LeftButton:
            r = option.rect
            if QRectF(r.left() + 2, r.center().y() - 9, 18, 18).contains(event.position()):
                model.setData(index, _UNCHECKED if model.data(index, Qt.ItemDataRole.CheckStateRole) == _CHECKED else _CHECKED,
                              Qt.ItemDataRole.CheckStateRole)
                return True
        return False


class _FilesTree(QTreeView):
    menu_requested = Signal(QPoint)
    toggle_requested = Signal()

    def drawBranches(self, p, rect, index):
        if self.selectionModel().isSelected(index):
            p.fillRect(rect, _pal("accent", .16))
        if self.model().hasChildren(index) and rect.width() >= 14:
            _blit(p, "chev_d" if self.isExpanded(index) else "chev_r", rect.right() - 14, rect.center().y() - 6, 12,
                  _pal("muted"), self.devicePixelRatioF(), 2)

    def contextMenuEvent(self, e):
        self.menu_requested.emit(e.globalPos())

    def keyPressEvent(self, e):
        if e.key() == Qt.Key.Key_Space:
            return self.toggle_requested.emit()
        super().keyPressEvent(e)


class _PanelHeader(QWidget):
    clicked = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedHeight(30)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.title = self.info = ""
        self._f = _font(self.font(), 12, QFont.Weight.DemiBold)
        self._fs = _font(self.font(), 12)

    def paintEvent(self, _e):
        p, w = QPainter(self), self.width()
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        dpr = self.devicePixelRatioF()
        p.setPen(_pal("card_border"))
        p.drawLine(0, 0, w, 0)
        _blit(p, "chev_d", 2, 9, 12, _pal("muted"), dpr, 2)
        info_w = min(w * .6, QFontMetrics(self._fs).horizontalAdvance(self.info) + 8)
        _text(p, QRectF(20, 0, w - info_w - 28, 30), self.title, self._f, _pal("primary_text"))
        _text(p, QRectF(w - info_w - 4, 0, info_w, 30), self.info, self._fs, _pal("muted"), _RIGHT)

    def mouseReleaseEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit()


class _Table(QTreeWidget):

    def __init__(self, cols: tuple, widths: tuple, right: tuple = ()):
        super().__init__()
        self._cols, self._right = cols, right
        self.setColumnCount(len(cols))
        self.setRootIsDecorated(False)
        self.setUniformRowHeights(True)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        h = self.header()
        h.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        h.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        h.setFixedHeight(22)
        for c, wd in enumerate(widths):
            if wd:
                self.setColumnWidth(c, wd)
        self.retranslate()

    def retranslate(self) -> None:
        self.setHeaderLabels([tr(c) for c in self._cols])

    def set_rows(self, rows: list) -> None:
        self.setUpdatesEnabled(False)
        n = self.topLevelItemCount()
        for r, row in enumerate(rows):
            it = self.topLevelItem(r) if r < n else None
            if it is None:
                it = QTreeWidgetItem(self)
                for c in self._right:
                    it.setTextAlignment(c, int(_RIGHT | _VCENTER))
            for c, v in enumerate(row):
                if it.text(c) != v:
                    it.setText(c, v)
        while self.topLevelItemCount() > len(rows):
            self.takeTopLevelItem(self.topLevelItemCount() - 1)
        self.setUpdatesEnabled(True)


class _Bar(QWidget):
    def __init__(self):
        super().__init__()
        self.setFixedHeight(16)
        self.frac = 0.0

    def paintEvent(self, _e):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        f = _font(self.font(), 12)
        label = tr("files.gen_progress")
        lw = QFontMetrics(f).horizontalAdvance(label) + 10
        _text(p, QRectF(0, 0, lw, 16), label, f, _pal("muted"))
        bar = QRectF(lw, 5, self.width() - lw, 6)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(_pal("primary_text", .10))
        p.drawRoundedRect(bar, 3, 3)
        if self.frac > 0:
            p.setBrush(_pal("state_completed") if self.frac >= 1 else _pal("accent"))
            p.drawRoundedRect(QRectF(bar.left(), bar.top(), max(6.0, bar.width() * min(1.0, self.frac)), 6), 3, 3)


_GENERAL = (
    ("files.gen_transfer", (
        (("active", "files.gen_active"), ("down", "files.gen_downloaded"), ("down_rate", "files.gen_down_speed"),
         ("down_limit", "files.gen_down_limit"), ("ratio", "files.gen_ratio")),
        (("eta", "files.col_eta"), ("up", "files.gen_uploaded"), ("up_rate", "files.gen_up_speed"),
         ("up_limit", "files.gen_up_limit"), ("reannounce", "files.gen_reannounce")),
        (("conns", "files.gen_connections"), ("seeds", "files.gen_seeds"), ("peers", "files.gen_peers"),
         ("wasted", "files.gen_wasted"), ("seen", "files.gen_seen")))),
    ("files.gen_information", (
        (("size", "files.gen_total_size"), ("added", "files.gen_added"), ("private", "files.gen_private"),
         ("hash", "files.gen_hash"), ("save_path", "files.gen_save_path"), ("comment", "files.gen_comment")),
        (("pieces", "files.gen_pieces"), ("completed", "files.gen_completed")),
        (("creator", "files.gen_created_by"), ("created", "files.gen_created_on")))),
)


def _general_values(d: dict) -> dict[str, str]:
    B, S = human_bytes, (lambda b: human_speed(b / 1024))
    ts = lambda t: time.strftime("%x %X", time.localtime(t)) if t and t > 0 else "-"
    session = lambda a, b: f"{B(a)} ({tr('files.gen_session', v=B(b))})"
    limit = lambda b: S(b) if b else tr("files.gen_unlimited")
    return {
        "active": human_duration(d["active"]), "down": session(d["down"], d["down_s"]), "down_rate": S(d["down_rate"]),
        "down_limit": limit(d["down_lim"]), "ratio": f"{d['ratio']:.2f}", "eta": human_duration(d["eta"]),
        "up": session(d["up"], d["up_s"]), "up_rate": S(d["up_rate"]), "up_limit": limit(d["up_lim"]),
        "reannounce": human_duration(d["reannounce"]), "conns": tr("files.gen_max", n=d["conns"], m=d["max_conns"]),
        "seeds": tr("files.gen_total", n=d["seeds"], m=d["list_seeds"]), "peers": tr("files.gen_total", n=d["peers"], m=d["list_peers"]),
        "wasted": B(d["wasted"]), "seen": ts(d["seen"]), "size": B(d["size"]), "added": ts(d["added"]),
        "private": tr("files.gen_yes" if d["private"] else "files.gen_no"), "hash": d["hash"], "save_path": d["save_path"],
        "comment": d["comment"], "pieces": f"{d['pieces']} x {B(d['piece_len'])} ({tr('files.gen_have', n=d['have'])})",
        "completed": ts(d["completed"]), "creator": d["creator"], "created": ts(d["created"]),
    }


class _General(QWidget):

    def __init__(self):
        super().__init__()
        root = QVBoxLayout(self)
        root.setContentsMargins(8, 6, 8, 6)
        root.setSpacing(4)
        self._bar = _Bar()
        root.addWidget(self._bar)
        self._vals: dict[str, CaptionLabel] = {}
        self._names: list[tuple[CaptionLabel, str]] = []
        for head, groups in _GENERAL:
            title = StrongBodyLabel(tr(head))
            self._names.append((title, head))
            root.addWidget(title)
            grid = QGridLayout()
            grid.setHorizontalSpacing(8)
            grid.setVerticalSpacing(3)
            for g, rows in enumerate(groups):
                grid.setColumnStretch(g * 2 + 1, 1)
                for r, (key, label) in enumerate(rows):
                    name, val = CaptionLabel(tr(label)), CaptionLabel("")
                    name.setAlignment(_RIGHT | _VCENTER)
                    val.setTextFormat(Qt.TextFormat.PlainText)
                    val.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
                    val.setMinimumWidth(10)
                    name.setMinimumHeight(18)
                    val.setMinimumHeight(18)
                    grid.addWidget(name, r, g * 2)
                    grid.addWidget(val, r, g * 2 + 1)
                    self._vals[key] = val
                    self._names.append((name, label))
            root.addLayout(grid)
        root.addStretch(1)

    def retranslate(self) -> None:
        for w, key in self._names:
            w.setText(tr(key))
        self._bar.update()

    def update_from(self, d: dict, item) -> None:
        self._bar.frac = (item.progress / 100.0) if item is not None else 0.0
        self._bar.update()
        for k, v in _general_values(d).items():
            if self._vals[k].text() != v:
                self._vals[k].setText(v)


class FilesPanel(QWidget):
    _MIN_OPEN = 170
    _TABS = ("general", "content", "trackers", "peers")

    def __init__(self, manager: DownloadManager, parent=None):
        super().__init__(parent)
        self._m, self._iid, self._shape, self._h, self._split = manager, "", None, 260, None
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        self._head = _PanelHeader()
        self._head.clicked.connect(self.hide)
        lay.addWidget(self._head)
        self._body = QWidget()
        bl = QVBoxLayout(self._body)
        bl.setContentsMargins(0, 0, 0, 6)
        bl.setSpacing(2)
        self._tabs = Pivot()
        for key in self._TABS:
            self._tabs.addItem(routeKey=key, text=tr(f"files.tab_{key}"), onClick=lambda *_, k=key: self._switch(k))
        bl.addWidget(self._tabs, 0, Qt.AlignmentFlag.AlignLeft)
        self._note = CaptionLabel("")
        self._note.setAlignment(Qt.AlignmentFlag.AlignCenter)
        bl.addWidget(self._note, 1)
        self._stack = QStackedWidget()
        bl.addWidget(self._stack, 1)
        self._general = _General()
        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._scroll.setWidget(self._general)
        self._stack.addWidget(self._scroll)
        self._stack.addWidget(self._build_content())
        self._trackers = _Table(("files.tr_url", "files.tr_tier", "files.tr_status", "files.tr_seeds", "files.tr_peers"),
                                (0, 50, 260, 70, 70), (1, 3, 4))
        self._peers = _Table(("files.pe_addr", "files.pe_client", "files.pe_progress", "files.pe_down", "files.pe_up",
                              "files.pe_downloaded", "files.pe_uploaded"), (0, 130, 80, 80, 80, 90, 90), (2, 3, 4, 5, 6))
        self._stack.addWidget(self._trackers)
        self._stack.addWidget(self._peers)
        lay.addWidget(self._body, 1)
        self._tabs.setCurrentItem("content")
        self._stack.setCurrentIndex(1)
        self._timer = QTimer(self, interval=500)
        self._timer.timeout.connect(self.refresh)
        self._apply_theme()
        qconfig.themeChanged.connect(self._apply_theme)
        register_locale_refresh(self, self._retranslate)
        self.setMinimumHeight(self._MIN_OPEN)
        self.hide()

    def _build_content(self) -> QWidget:
        w = QWidget()
        cl = QVBoxLayout(w)
        cl.setContentsMargins(0, 2, 0, 0)
        cl.setSpacing(2)
        bar = QHBoxLayout()
        bar.setSpacing(2)
        self._stats = CaptionLabel("")
        bar.addWidget(self._stats, 1)
        self._btns: dict[str, TransparentPushButton] = {}
        for key, fn in (("files.select_all", lambda: self._all(None)), ("files.deselect_all", lambda: self._all(0))):
            b = TransparentPushButton(tr(key))
            b.setFixedHeight(24)
            b.clicked.connect(lambda _=False, f=fn: f())
            bar.addWidget(b)
            self._btns[key] = b
        cl.addLayout(bar)
        self._model = _FilesModel(self)
        self._model.requested.connect(self._apply)
        self._tree = t = _FilesTree()
        t.setModel(self._model)
        t.setItemDelegate(_FileDelegate(t))
        t.setUniformRowHeights(True)
        t.setIndentation(16)
        t.setExpandsOnDoubleClick(True)
        t.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        t.setMouseTracking(True)
        t.setFrameShape(QFrame.Shape.NoFrame)
        t.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        h = t.header()
        h.setSectionsMovable(False)
        h.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        h.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for c, wd in enumerate((0, 74, 74, 96, 96, 78, 78, 70)):
            if wd:
                t.setColumnWidth(c, wd)
        h.setMinimumSectionSize(40)
        h.setFixedHeight(22)
        t.menu_requested.connect(self._menu)
        t.toggle_requested.connect(self._toggle_selected)
        self._model.visible = self._visible
        cl.addWidget(t, 1)
        return w

    def attach(self, split: QSplitter) -> None:
        self._split = split
        split.splitterMoved.connect(lambda *_: self._remember())

    def _apply_theme(self, *_) -> None:
        pal = palette()
        a = _pal("accent", .16).name(QColor.NameFormat.HexArgb)
        qss = (f"QTreeView {{ background: transparent; border: none; outline: none; color: {pal['primary_text']}; }} "
               f"QTreeView::item:selected {{ background: {a}; color: {pal['primary_text']}; }} "
               f"QHeaderView {{ background: transparent; }} "
               f"QHeaderView::section {{ background: transparent; color: {pal['muted']}; border: none; "
               f"border-bottom: 1px solid {pal['card_border']}; padding: 0 6px; font-size: 11px; }} {scroll_area_qss()}")
        for w in (self._tree, self._trackers, self._peers):
            w.setStyleSheet(qss)
        self._scroll.setStyleSheet("QScrollArea, QScrollArea > QWidget > QWidget { background: transparent; border: none; } " + scroll_area_qss())
        self._note.setStyleSheet(f"color: {pal['muted']};")
        self._head.update()

    def _retranslate(self) -> None:
        for key in self._TABS:
            with suppress(Exception):
                self._tabs.widget(key).setText(tr(f"files.tab_{key}"))
        for key, b in self._btns.items():
            b.setText(tr(key))
        self._trackers.retranslate()
        self._peers.retranslate()
        self._general.retranslate()
        self._model.headerDataChanged.emit(Qt.Orientation.Horizontal, 0, len(_FCOLS) - 1)
        self.on_item(self._m.get(self._iid))
        self.refresh()


    def set_item(self, iid: str) -> None:
        item = self._m.get(iid)
        if iid != self._iid:
            self._iid, self._shape = iid, None
            self._model.clear()
            self._model.warn = item is None or item.category != "repacks"
        self.setVisible(bool(iid))
        self.on_item(item)
        self.refresh()

    def on_item(self, item) -> None:
        h = self._head
        h.title = tr("files.title") + (f"  ·  {_download_title(item)}" if item is not None else "")
        h.info = "" if item is None else (f"\u2193 {human_speed(item.speed_down_kbps)}  ·  {item.progress:.1f}%  ·  "
                                          f"{human_bytes(item.downloaded_bytes)} / {human_bytes(item.total_bytes)}")
        h.update()

    def showEvent(self, e) -> None:
        super().showEvent(e)
        if self._split is not None:
            self._split.setSizes([max(self._split.height() - self._h, 80), self._h])
        self._timer.start()
        self.refresh()

    def hideEvent(self, e) -> None:
        super().hideEvent(e)
        self._timer.stop()

    def _remember(self) -> None:
        if self.isVisible() and self.height() >= self._MIN_OPEN:
            self._h = self.height()

    def _switch(self, key: str) -> None:
        self._stack.setCurrentIndex(self._TABS.index(key))
        self.refresh()

    def _visible(self) -> list:
        t, out = self._tree, []
        i, bottom = t.indexAt(t.viewport().rect().topLeft()), t.viewport().height()
        while i.isValid() and len(out) < 200 and t.visualRect(i).top() < bottom:
            out.append(i)
            i = t.indexBelow(i)
        return out

    def refresh(self) -> None:
        if not (self._iid and self.isVisible()):
            return
        m, tab = self._m, self._stack.currentIndex()
        data = (m.details, m.file_state, m.trackers, m.peers)[tab](self._iid)
        if data is None:
            if self._shape is not None:
                self._shape = None
                self._model.clear()
            self._note.setText(tr("files.unavailable"))
            self._note.show(), self._stack.hide()
            return
        self._note.hide(), self._stack.show()
        if tab == 0:
            self._general.update_from(data, m.get(self._iid))
        elif tab == 1:
            self._refresh_content(data)
        elif tab == 2:
            self._trackers.set_rows([[host, str(tier), msg or "-", str(s) if s >= 0 else "-", str(p) if p >= 0 else "-"]
                                     for host, tier, msg, s, p in data])
        else:
            self._peers.set_rows([[addr, client, f"{prog * 100:.0f}%", human_speed(dn / 1024) if dn else "",
                                   human_speed(up / 1024) if up else "", human_bytes(dl), human_bytes(ul)]
                                  for addr, client, prog, dn, up, dl, ul in data])

    def _refresh_content(self, st: dict) -> None:
        n = len(st["prios"])
        if self._shape != (self._iid, n):
            tree = self._m.file_tree(self._iid)
            if tree is None or len(tree) != n:
                return
            self._model.rebuild(tree)
            self._shape = (self._iid, n)
            if self._model.rowCount() == 1 and self._model.index(0, 0).internalPointer().idx < 0:
                self._tree.expand(self._model.index(0, 0))
        self._model.update(st["progress"], st["prios"], st["state"] == DLState.paused)
        s = st["stats"]
        left = max(0, s["wanted"] - s["done"])
        self._stats.setText(tr(
            "files.stats", down=human_speed(s["down"]), up=human_speed(s["up"]), pct=f"{s['progress']:.1f}", left=human_bytes(left),
            peers=f"{s['peers']}/{s['list_peers']}", seeds=f"{s['seeds']}/{s['list_seeds']}", ratio=f"{s['ratio']:.2f}",
            avail=f"{s['availability']:.2f}" if s["availability"] >= 0 else "-", eta=human_duration(s["eta"])))


    def _apply(self, changes: dict) -> None:
        if not self._m.set_file_priorities(self._iid, changes):
            _toast(self.window(), "warning", tr("files.title"), tr("files.keep_one"), 3000)
        self.refresh()

    def _all(self, prio) -> None:
        self._model.request(self._model._root.kids, prio)

    def _selected(self) -> list:
        return [i.internalPointer() for i in self._tree.selectionModel().selectedRows(0)]

    def _toggle_selected(self) -> None:
        nodes = self._selected()
        if nodes:
            self._model.request(nodes, 0 if all(self._model.check(k) == _CHECKED for k in nodes) else None)

    def _target_path(self, node) -> str | None:
        leaf = node if node.idx >= 0 else next(iter(self._model.leaves([node])), None)
        path = self._m.file_path(self._iid, leaf.idx) if leaf is not None else None
        return path if node.idx >= 0 or not path else os.path.dirname(path)

    def _menu(self, pos: QPoint) -> None:
        idx = self._tree.indexAt(self._tree.viewport().mapFromGlobal(pos))
        if idx.isValid() and not self._tree.selectionModel().isSelected(idx):
            self._tree.setCurrentIndex(idx)
        nodes, m = self._selected(), self._model
        menu = RoundMenu(parent=self)

        def add(menu_, key, fn):
            a = Action(tr(key))
            a.triggered.connect(lambda _=False: fn())
            menu_.addAction(a)

        if nodes:
            add(menu, "files.check", lambda: m.request(nodes, None))
            add(menu, "files.uncheck", lambda: m.request(nodes, 0))
            sub = RoundMenu(tr("files.priority"), self)
            for prio in (7, 6, 4, 1):
                add(sub, _PRIO_KEYS[prio], lambda p=prio: m.request(nodes, p))
            menu.addMenu(sub)
            menu.addSeparator()
        add(menu, "files.select_all", lambda: self._all(None))
        add(menu, "files.deselect_all", lambda: self._all(0))
        add(menu, "files.expand_all", self._tree.expandAll)
        add(menu, "files.collapse_all", self._tree.collapseAll)
        if len(nodes) == 1:
            node, path = nodes[0], self._target_path(nodes[0])
            menu.addSeparator()
            if path:
                add(menu, "files.show_in_folder", lambda: _reveal(path, select=node.idx >= 0))
                add(menu, "files.copy_path", lambda: QGuiApplication.clipboard().setText(os.path.normpath(path)))
            add(menu, "files.copy_name", lambda: QGuiApplication.clipboard().setText(node.path))
        menu.exec(pos)


class DownloadManagerPage(QWidget):
    def __init__(self, manager: DownloadManager, parent=None):
        super().__init__(parent)
        self.setStyleSheet("background: transparent;")
        self.setAcceptDrops(True)
        self._manager = manager
        self._filelist_thread = None
        self._rows: dict[str, DownloadItemWidget] = {}
        self._selected = ""
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
        top_box = QWidget()
        tl = QVBoxLayout(top_box)
        tl.setContentsMargins(0, 0, 0, 0)
        tl.setSpacing(0)
        tl.addWidget(self._list, 1)
        tl.addWidget(self._empty, 1)
        self._panel = FilesPanel(manager)
        self._split = QSplitter(Qt.Orientation.Vertical)
        self._split.setChildrenCollapsible(False)
        self._split.setHandleWidth(6)
        self._split.setStyleSheet("QSplitter::handle { background: transparent; }")
        self._split.addWidget(top_box)
        self._split.addWidget(self._panel)
        self._split.setStretchFactor(0, 1)
        self._split.setStretchFactor(1, 0)
        self._panel.attach(self._split)
        root.addWidget(self._split, 1)
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
        row.clicked.connect(self._select)
        row.set_selected(item.id == self._selected)
        self._list.add_row(row)
        self._rows[item.id] = row

    def _refresh_visibility(self) -> None:
        has = bool(self._rows)
        self._list.setVisible(has)
        self._empty.setVisible(not has)

    def _select(self, item_id: str) -> None:
        if item_id == self._selected:
            self._panel.setVisible(bool(item_id) and self._panel.isHidden())
            return
        if (old := self._rows.get(self._selected)) is not None:
            old.set_selected(False)
        self._selected = item_id
        if (row := self._rows.get(item_id)) is not None:
            row.set_selected(True)
        self._panel.set_item(item_id)

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
                if item_id == self._selected:
                    self._panel.on_item(item)

    def _on_removed(self, item_id: str) -> None:
        row = self._rows.pop(item_id, None)
        if row is None:
            return
        if item_id == self._selected:
            self._selected = ""
            self._panel.set_item("")
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
