from __future__ import annotations

import gc
import logging
import os
import subprocess
import sys

from PyQt6.QtCore import Qt, QSize, pyqtSignal
from PyQt6.QtGui import QColor
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QFormLayout,
    QFrame,
    QGraphicsDropShadowEffect,
    QHBoxLayout,
    QListWidget,
    QListWidgetItem,
    QVBoxLayout,
    QWidget,
)

from qfluentwidgets import (
    CaptionLabel,
    CardWidget,
    CheckBox,
    CompactSpinBox,
    DoubleSpinBox,
    FluentIcon,
    InfoBar,
    InfoBarPosition,
    MessageBox,
    PrimaryPushButton,
    ProgressBar,
    PushButton,
    StrongBodyLabel,
    SubtitleLabel,
    ToolButton,
    qconfig,
)

from ..core.download_manager import DLState, DownloadItem, DownloadManager
from ..core.theme import palette, scroll_area_qss
from ..core.translations import tr, register_locale_refresh

logger = logging.getLogger(__name__)

_STATE_COLOR_KEYS = {
    DLState.queued: "state_queued",
    DLState.downloading: "state_downloading",
    DLState.verifying: "state_verifying",
    DLState.paused: "state_paused",
    DLState.seeding: "state_seeding",
    DLState.completed: "state_completed",
    DLState.error: "state_error",
    DLState.cancelled: "state_cancelled",
}

_STATE_LABEL_KEYS = {
    DLState.queued: "download.state_queued",
    DLState.downloading: "download.state_downloading",
    DLState.verifying: "download.state_verifying",
    DLState.paused: "download.state_paused",
    DLState.seeding: "download.state_seeding",
    DLState.completed: "download.state_completed",
    DLState.error: "download.state_error",
    DLState.cancelled: "download.state_cancelled",
}

def _state_label(state) -> str:
    key = _STATE_LABEL_KEYS.get(state)
    return tr(key) if key else state.value


def _state_color(state) -> str:
    return palette()[_STATE_COLOR_KEYS.get(state, "state_queued")]

_CARD_RADIUS = 8

def _muted_color() -> str:
    return palette()["muted"]

class TorrentSettingsDialog(QDialog):
    def __init__(self, item: DownloadItem, parent=None):
        super().__init__(parent)
        self.setWindowTitle(tr('download.torrent_settings_for', name=item.game_name))
        self.setFixedWidth(380)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 20, 20, 16)
        layout.setSpacing(14)

        form = QFormLayout()
        form.setSpacing(12)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignLeft)

        self.chk_seed = CheckBox(tr('download.seed_after_completion'))
        self.chk_seed.setChecked(item.seed_after)
        form.addRow(self.chk_seed)

        self.spin_down = CompactSpinBox()
        self.spin_down.setRange(0, 1_000_000)
        self.spin_down.setSuffix(tr('download.suffix_kbps_unlimited'))
        self.spin_down.setValue(item.max_down_kbps)
        form.addRow(tr('download.max_download_speed'), self.spin_down)

        self.spin_up = CompactSpinBox()
        self.spin_up.setRange(0, 1_000_000)
        self.spin_up.setSuffix(tr('download.suffix_kbps_unlimited'))
        self.spin_up.setValue(item.max_up_kbps)
        form.addRow(tr('download.max_upload_speed'), self.spin_up)

        self.spin_peers = CompactSpinBox()
        self.spin_peers.setRange(1, 1000)
        self.spin_peers.setValue(item.max_peers)
        form.addRow(tr('download.max_connections'), self.spin_peers)

        self.spin_ratio = DoubleSpinBox()
        self.spin_ratio.setRange(0, 100)
        self.spin_ratio.setSingleStep(0.1)
        self.spin_ratio.setSuffix(tr('download.suffix_unlimited'))
        self.spin_ratio.setValue(item.ratio_limit)
        form.addRow(tr('download.seed_ratio_limit'), self.spin_ratio)

        self.spin_seed_time = CompactSpinBox()
        self.spin_seed_time.setRange(0, 100000)
        self.spin_seed_time.setSuffix(tr('download.suffix_min_unlimited'))
        self.spin_seed_time.setValue(item.seed_time_limit_min)
        form.addRow(tr('download.seed_time_limit'), self.spin_seed_time)

        layout.addLayout(form)

        btn_row = QHBoxLayout()
        self.btn_recheck = PushButton(tr('download.force_recheck'))
        btn_row.addWidget(self.btn_recheck)
        btn_row.addStretch()
        self.btn_cancel = PushButton(tr('download.cancel'))
        self.btn_save = PrimaryPushButton(tr('download.save'))
        btn_row.addWidget(self.btn_cancel)
        btn_row.addWidget(self.btn_save)
        layout.addLayout(btn_row)

        self.recheck_requested = False

        def _on_recheck():
            self.recheck_requested = True
            self.accept()

        self.btn_recheck.clicked.connect(_on_recheck)
        self.btn_cancel.clicked.connect(self.reject)
        self.btn_save.clicked.connect(self.accept)

    def values(self) -> dict:
        return {
            "seed_after": self.chk_seed.isChecked(),
            "max_down_kbps": self.spin_down.value(),
            "max_up_kbps": self.spin_up.value(),
            "max_peers": self.spin_peers.value(),
            "ratio_limit": self.spin_ratio.value(),
            "seed_time_limit_min": self.spin_seed_time.value(),
        }

class _StatusDot(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedSize(8, 8)
        self._color = QColor(_state_color(DLState.queued))

    def set_color(self, hex_color: str) -> None:
        self._color = QColor(hex_color)
        self.update()

    def paintEvent(self, event):
        from PyQt6.QtGui import QPainter

        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setBrush(self._color)
        p.setPen(Qt.PenStyle.NoPen)
        p.drawEllipse(self.rect())

class DownloadItemWidget(CardWidget):
    request_pause = pyqtSignal(str)
    request_resume = pyqtSignal(str)
    request_cancel = pyqtSignal(str)
    request_retry = pyqtSignal(str)
    request_remove = pyqtSignal(str)
    request_open_folder = pyqtSignal(str)
    request_settings = pyqtSignal(str)

    def __init__(self, item: DownloadItem, parent=None):
        super().__init__(parent)
        self.item_id = item.id
        self.setFixedHeight(108)
        self.setBorderRadius(_CARD_RADIUS)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(18, 12, 14, 12)
        outer.setSpacing(8)

        top_row = QHBoxLayout()
        top_row.setSpacing(8)

        self._dot = _StatusDot()
        top_row.addWidget(self._dot, 0, Qt.AlignmentFlag.AlignVCenter)

        self._title = StrongBodyLabel(item.game_name)
        self._title.setObjectName("downloadTitleLabel")
        self._title.setStyleSheet("QLabel#downloadTitleLabel { font-size: 13px; }")
        self._title.setWordWrap(False)
        top_row.addWidget(self._title, 1)

        self._state_label = CaptionLabel(item.state.value)
        self._state_label.setStyleSheet("font-weight: 600;")
        top_row.addWidget(self._state_label, 0, Qt.AlignmentFlag.AlignVCenter)

        outer.addLayout(top_row)

        self._meta = CaptionLabel(f"{item.console}   ·   {item.source}")
        self._meta.setStyleSheet(f"color: {_muted_color()};")
        outer.addWidget(self._meta)

        prog_row = QHBoxLayout()
        prog_row.setSpacing(10)
        self._progress = ProgressBar()
        self._progress.setFixedHeight(4)
        self._progress.setRange(0, 100)
        prog_row.addWidget(self._progress, 1)
        self._pct_label = CaptionLabel("0%")
        self._pct_label.setFixedWidth(38)
        self._pct_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        prog_row.addWidget(self._pct_label)
        outer.addLayout(prog_row)

        info_row = QHBoxLayout()
        info_row.setSpacing(16)
        self._size_label = CaptionLabel("0 B / 0 B")
        self._speed_label = CaptionLabel("↓ 0 B/s   ↑ 0 B/s")
        self._extra_label = CaptionLabel("")
        for lbl in (self._size_label, self._speed_label, self._extra_label):
            lbl.setStyleSheet(f"color: {_muted_color()};")
            info_row.addWidget(lbl)
        info_row.addStretch(1)

        self._btn_pause = ToolButton(FluentIcon.PAUSE)
        self._btn_pause.setToolTip(tr('download.pause'))
        self._btn_resume = ToolButton(FluentIcon.PLAY)
        self._btn_resume.setToolTip(tr('download.resume'))
        self._btn_retry = ToolButton(FluentIcon.SYNC)
        self._btn_retry.setToolTip(tr('download.retry'))
        self._btn_folder = ToolButton(FluentIcon.FOLDER)
        self._btn_folder.setToolTip(tr('download.open_folder'))
        self._btn_settings = ToolButton(FluentIcon.SETTING)
        self._btn_settings.setToolTip(tr('download.torrent_settings'))
        self._btn_cancel = ToolButton(FluentIcon.CLOSE)
        self._btn_cancel.setToolTip(tr('download.cancel'))
        self._btn_remove = ToolButton(FluentIcon.DELETE)
        self._btn_remove.setToolTip(tr('download.remove'))

        for b in (
            self._btn_pause,
            self._btn_resume,
            self._btn_retry,
            self._btn_folder,
            self._btn_settings,
            self._btn_cancel,
            self._btn_remove,
        ):
            b.setFixedSize(26, 26)
            b.setIconSize(QSize(13, 13))
            info_row.addWidget(b)

        outer.addLayout(info_row)

        self._btn_pause.clicked.connect(lambda: self.request_pause.emit(self.item_id))
        self._btn_resume.clicked.connect(lambda: self.request_resume.emit(self.item_id))
        self._btn_cancel.clicked.connect(lambda: self.request_cancel.emit(self.item_id))
        self._btn_retry.clicked.connect(lambda: self.request_retry.emit(self.item_id))
        self._btn_remove.clicked.connect(lambda: self.request_remove.emit(self.item_id))
        self._btn_folder.clicked.connect(lambda: self.request_open_folder.emit(self.item_id))
        self._btn_settings.clicked.connect(lambda: self.request_settings.emit(self.item_id))

        self._item = item
        self.update_from_item(item)
        qconfig.themeChanged.connect(self._on_theme_changed)

    def _on_theme_changed(self, *_):
        try:
            self.update_from_item(self._item)
        except RuntimeError:
            pass

    def update_from_item(self, item: DownloadItem) -> None:
        self._item = item
        self._meta.setStyleSheet(f"color: {_muted_color()};")
        self._title.setText(item.game_name)
        self._title.setToolTip(item.game_name)
        self._title.setStyleSheet("QLabel#downloadTitleLabel { font-size: 13px; }")
        self._meta.setText(f"{item.console}   ·   {item.source}")

        color = _state_color(item.state)
        self._dot.set_color(color)
        self._state_label.setText(_state_label(item.state))
        self._state_label.setStyleSheet(f"font-weight: 600; color: {color};")

        pct = int(item.progress)
        self._progress.setValue(max(0, min(100, pct)))
        self._progress.setVisible(item.state != DLState.error)
        self._pct_label.setText(f"{pct}%" if item.state != DLState.error else "—")

        self._size_label.setStyleSheet(f"color: {_muted_color()};")
        self._speed_label.setStyleSheet(f"color: {_muted_color()};")
        self._size_label.setText(item.display_size())

        if item.state == DLState.seeding:
            self._speed_label.setText(f"↓ {item.speed_down}   ↑ {item.speed_up}")
            self._extra_label.setText(
                tr('download.seeding_stats', time=item.seed_time, ratio=f"{item.ratio:.2f}", peers=item.peers)
            )
        elif item.state == DLState.error:
            self._speed_label.setText("")
            self._extra_label.setText((item.error or tr('download.unknown_error'))[:70])
            self._extra_label.setStyleSheet(f"color: {_state_color(DLState.error)};")
        elif item.state == DLState.completed:
            self._speed_label.setText("")
            self._extra_label.setText(tr('download.done'))
        elif item.state == DLState.queued:
            self._speed_label.setText("")
            self._extra_label.setText(tr('download.waiting_for_slot'))
        else:
            self._speed_label.setText(f"↓ {item.speed_down}   ↑ {item.speed_up}")
            self._extra_label.setStyleSheet(f"color: {_muted_color()};")
            self._extra_label.setText(tr('download.eta_peers', eta=item.eta, peers=item.peers))

        can_pause = item.state in (DLState.downloading, DLState.verifying)
        can_resume = item.state == DLState.paused
        can_retry = item.state in (DLState.error, DLState.cancelled)
        can_cancel = item.state in (
            DLState.downloading,
            DLState.verifying,
            DLState.queued,
            DLState.paused,
            DLState.seeding,
        )
        can_open = bool(item.download_path) or item.state in (DLState.completed, DLState.seeding)

        self._btn_pause.setVisible(can_pause)
        self._btn_resume.setVisible(can_resume)
        self._btn_retry.setVisible(can_retry)
        self._btn_cancel.setVisible(can_cancel)
        self._btn_remove.setVisible(not can_cancel)
        self._btn_folder.setEnabled(can_open)
        self._btn_settings.setEnabled(item.state not in (DLState.completed, DLState.cancelled))

class StatsBar(CardWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setBorderRadius(_CARD_RADIUS)
        self.setFixedHeight(76)
        self._update_card_surface()
        qconfig.themeChanged.connect(lambda *_: self._update_card_surface())

        layout = QHBoxLayout(self)
        layout.setContentsMargins(24, 12, 24, 12)
        layout.setSpacing(0)

        self._labels: dict[str, tuple[CaptionLabel, StrongBodyLabel]] = {}
        self._separators: list[QFrame] = []
        fields = (
            ("active", "download.stats_active"),
            ("down", "download.stats_download_speed"),
            ("up", "download.stats_upload_speed"),
            ("queued", "download.stats_queued"),
            ("completed", "download.stats_completed"),
        )
        self._field_keys = dict(fields)
        for idx, (key, title_key) in enumerate(fields):
            if idx > 0:
                sep = QFrame()
                sep.setFrameShape(QFrame.Shape.VLine)
                sep.setFixedHeight(32)
                layout.addWidget(sep)
                layout.addSpacing(24)
                self._separators.append(sep)

            col = QVBoxLayout()
            col.setSpacing(2)
            val = StrongBodyLabel("0")
            val.setObjectName("statValueLabel")
            val.setStyleSheet("QLabel#statValueLabel { font-size: 18px; }")
            cap = CaptionLabel(tr(title_key))
            col.addWidget(val)
            col.addWidget(cap)
            layout.addLayout(col)
            self._labels[key] = (cap, val)
            if idx < len(fields) - 1:
                layout.addSpacing(24)

        layout.addStretch(1)
        self._refresh_muted_colors()
        qconfig.themeChanged.connect(lambda *_: self._refresh_muted_colors())
        qconfig.themeChanged.connect(lambda *_: self._refresh_value_sizes())

    def retranslate(self) -> None:
        for key, (cap, _val) in self._labels.items():
            cap.setText(tr(self._field_keys[key]))

    def _refresh_value_sizes(self) -> None:
        for _cap, val in self._labels.values():
            val.setStyleSheet("QLabel#statValueLabel { font-size: 18px; }")

    def _refresh_muted_colors(self) -> None:
        for sep in self._separators:
            sep.setStyleSheet(f"color: {_muted_color()}; background: transparent;")
        for cap, _val in self._labels.values():
            cap.setStyleSheet(f"color: {_muted_color()};")

    def update_stats(self, summary: dict) -> None:
        self._labels["active"][1].setText(str(summary["active"]))
        self._labels["down"][1].setText(summary["total_down"])
        self._labels["up"][1].setText(summary["total_up"])
        self._labels["queued"][1].setText(str(summary["queued"]))
        self._labels["completed"][1].setText(str(summary["completed"]))

    def _update_card_surface(self):
        c = palette()
        self.setStyleSheet(
            f"StatsBar {{ "
            f"background-color: {c['card_bg']}; "
            f"border: 1px solid {c['card_border']}; "
            f"border-radius: {_CARD_RADIUS}px; "
            f"padding: 0px; "
            f"}}"
        )

class DownloadManagerPage(QWidget):
    def __init__(self, manager: DownloadManager, parent=None):
        super().__init__(parent)
        self.setStyleSheet("background: transparent;")
        self._manager = manager

        root = QVBoxLayout(self)
        root.setContentsMargins(24, 20, 28, 20)
        root.setSpacing(16)

        header_row = QHBoxLayout()
        self._header_lbl = SubtitleLabel(tr('download.manager_title'))
        header_row.addWidget(self._header_lbl)
        header_row.addStretch()
        root.addLayout(header_row)

        self._stats = StatsBar()
        root.addWidget(self._stats)

        self._body = QWidget()
        body_layout = QVBoxLayout(self._body)
        body_layout.setContentsMargins(0, 0, 0, 0)
        body_layout.setSpacing(0)

        self._list = QListWidget()
        self._list.setSpacing(8)
        self._list.setFrameShape(QFrame.Shape.NoFrame)
        self._list.setObjectName("downloadQueueList")
        self._list.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove)
        self._list.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self._list.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self._list.model().rowsMoved.connect(self._on_rows_moved)
        self._apply_list_tint()
        qconfig.themeChanged.connect(lambda *_: self._apply_list_tint())
        qconfig.themeChanged.connect(lambda *_: self._apply_card_style())
        self._apply_card_style()

        body_layout.addWidget(self._list)

        self._empty_state = self._build_empty_state()
        body_layout.addWidget(self._empty_state)

        root.addWidget(self._body, 1)

        self._row_widgets: dict[str, DownloadItemWidget] = {}
        self._mutating_list = False

        self._manager.item_added.connect(self._on_item_added)
        self._manager.item_updated.connect(self._on_item_updated)
        self._manager.item_removed.connect(self._on_item_removed)
        self._manager.stats_changed.connect(self._refresh_stats)

        self._rebuild_all()
        self._refresh_stats()
        register_locale_refresh(self, self._on_locale_changed)

    def _on_locale_changed(self) -> None:
        self._header_lbl.setText(tr('download.manager_title'))
        self._empty_title_lbl.setText(tr('download.no_downloads_yet'))
        self._empty_sub_lbl.setText(tr('download.empty_hint'))
        self._stats.retranslate()
        for item_id, widget in list(self._row_widgets.items()):
            item = self._manager.get(item_id)
            if item is not None:
                try:
                    widget.update_from_item(item)
                except RuntimeError:
                    pass

    def _apply_list_tint(self):
        c = palette()
        self._list.setStyleSheet(
            f"""
            QListWidget {{
                background-color: {c['list_bg']};
                border: none;
                outline: none;
            }}
            QListWidget::item {{
                background: transparent;
            }}
            {scroll_area_qss()}
            """
        )

    def _apply_card_style(self):
        c = palette()
        self._list.setStyleSheet(
            self._list.styleSheet() + (
                f"DownloadItemWidget {{ "
                f"background-color: {c['card_bg']}; "
                f"border: 1px solid {c['card_border']}; "
                f"border-radius: 8px; "
                f"padding: 0px; "
                f"}} "
                f"DownloadItemWidget:hover {{ "
                f"background-color: {c['card_hover']}; "
                f"border: 1px solid {c['card_border']}; "
                f"border-radius: 8px; "
                f"}} "
            )
        )

    def _build_empty_state(self) -> QWidget:
        w = QWidget()
        layout = QVBoxLayout(w)
        layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.setSpacing(6)

        icon = ToolButton(FluentIcon.DOWNLOAD)
        icon.setEnabled(False)
        icon.setFixedSize(48, 48)
        icon.setIconSize(QSize(22, 22))

        def _refresh_icon_bg():
            icon.setStyleSheet(
                f"ToolButton {{ border-radius: 24px; background-color: {palette()['surface_tint_strong']}; }}"
            )

        _refresh_icon_bg()
        qconfig.themeChanged.connect(lambda *_: _refresh_icon_bg())
        icon_row = QHBoxLayout()
        icon_row.setAlignment(Qt.AlignmentFlag.AlignCenter)
        icon_row.addWidget(icon)
        layout.addLayout(icon_row)

        title = StrongBodyLabel(tr('download.no_downloads_yet'))
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(title)
        self._empty_title_lbl = title

        sub = CaptionLabel(tr('download.empty_hint'))
        sub.setAlignment(Qt.AlignmentFlag.AlignCenter)
        sub.setStyleSheet(f"color: {_muted_color()};")
        qconfig.themeChanged.connect(lambda *_: sub.setStyleSheet(f"color: {_muted_color()};"))
        layout.addWidget(sub)
        self._empty_sub_lbl = sub

        return w

    def _rebuild_all(self) -> None:
        self._mutating_list = True
        try:
            self._list.clear()
            self._row_widgets.clear()
            for item in self._manager.items_in_order():
                self._insert_row(item)
        finally:
            self._mutating_list = False
        self._update_empty_state()
        gc.collect()

    def _insert_row(self, item: DownloadItem) -> None:
        widget = DownloadItemWidget(item)
        widget.request_pause.connect(self._manager.pause)
        widget.request_resume.connect(self._manager.resume)
        widget.request_cancel.connect(self._on_cancel_clicked)
        widget.request_retry.connect(self._manager.retry)
        widget.request_remove.connect(self._on_remove_clicked)
        widget.request_open_folder.connect(self._on_open_folder)
        widget.request_settings.connect(self._on_settings_clicked)

        list_item = QListWidgetItem(self._list)
        list_item.setData(Qt.ItemDataRole.UserRole, item.id)
        list_item.setSizeHint(QSize(0, 116))
        self._list.addItem(list_item)
        self._list.setItemWidget(list_item, widget)
        self._row_widgets[item.id] = widget

    def _update_empty_state(self) -> None:
        has_items = self._list.count() > 0
        self._list.setVisible(has_items)
        self._empty_state.setVisible(not has_items)

    def _on_item_added(self, item_id: str) -> None:
        item = self._manager.get(item_id)
        if item is None or item_id in self._row_widgets:
            return
        self._insert_row(item)
        self._update_empty_state()

    def _on_item_updated(self, item_id: str) -> None:
        item = self._manager.get(item_id)
        widget = self._row_widgets.get(item_id)
        if item is None or widget is None:
            return
        widget.update_from_item(item)

    def _on_item_removed(self, item_id: str) -> None:
        widget = self._row_widgets.pop(item_id, None)
        if widget is None:
            return
        for row in range(self._list.count()):
            li = self._list.item(row)
            if li.data(Qt.ItemDataRole.UserRole) == item_id:
                self._list.takeItem(row)
                break
        self._update_empty_state()

    def _refresh_stats(self) -> None:
        self._stats.update_stats(self._manager.summary())

    def _on_rows_moved(self, *args) -> None:
        ids = []
        for row in range(self._list.count()):
            li = self._list.item(row)
            ids.append(li.data(Qt.ItemDataRole.UserRole))
        self._manager.reorder(ids)

    def _on_cancel_clicked(self, item_id: str) -> None:
        box = MessageBox(
            tr('download.cancel_download_title'),
            tr('download.cancel_download_content'),
            self.window(),
        )
        box.yesButton.setText(tr('download.yes'))
        box.cancelButton.setText(tr('download.no'))
        if not box.exec():
            return
        self._manager.cancel(item_id, delete_files=False)

    def _on_remove_clicked(self, item_id: str) -> None:
        box = MessageBox(
            tr('download.remove_download_title'),
            tr('download.remove_download_content'),
            self.window(),
        )
        box.yesButton.setText(tr('download.delete_file'))
        box.cancelButton.setText(tr('download.keep_file'))
        delete_files = box.exec()
        self._manager.remove(item_id, delete_files=delete_files)

    def _on_open_folder(self, item_id: str) -> None:
        path = self._manager.open_folder(item_id)
        if not path:
            InfoBar.warning(
                title=tr('download.folder_not_found_title'),
                content=tr('download.folder_not_found_content'),
                orient=Qt.Orientation.Horizontal,
                isClosable=True,
                position=InfoBarPosition.TOP_RIGHT,
                duration=3000,
                parent=self.window(),
            )
            return
        try:
            if sys.platform.startswith("win"):
                os.startfile(path)  # type: ignore[attr-defined]
            elif sys.platform == "darwin":
                subprocess.Popen(["open", path])
            else:
                subprocess.Popen(["xdg-open", path])
        except Exception:
            logger.exception("Failed to open folder %s", path)

    def _on_settings_clicked(self, item_id: str) -> None:
        item = self._manager.get(item_id)
        if item is None:
            return
        dlg = TorrentSettingsDialog(item, parent=self.window())
        if dlg.exec():
            self._manager.set_torrent_settings(item_id, **dlg.values())
            if getattr(dlg, "recheck_requested", False):
                self._manager.force_recheck(item_id)

    def add_from_rom(self, rom: dict) -> None:
        torrent = rom.get("torrent_file", "")
        if not torrent:
            InfoBar.warning(
                title=tr('download.no_torrent_title'),
                content=tr('download.no_torrent_content'),
                orient=Qt.Orientation.Horizontal,
                isClosable=True,
                position=InfoBarPosition.TOP_RIGHT,
                duration=3000,
                parent=self.window(),
            )
            return
        try:
            file_id = int(rom.get("file_id") or 1)
        except (TypeError, ValueError):
            file_id = 1
        self._manager.add(
            torrent_file=torrent,
            file_id=file_id,
            game_name=rom.get("title", "rom"),
            console=rom.get("console") or tr('download.unknown'),
            source=rom.get("source", "Minerva"),
        )
        InfoBar.success(
            title=tr('download.added_to_queue_title'),
            content=tr('download.added_to_queue_single', title=rom.get('title') or tr('download.rom_fallback')),
            orient=Qt.Orientation.Horizontal,
            isClosable=True,
            position=InfoBarPosition.TOP_RIGHT,
            duration=2500,
            parent=self.window(),
        )

    def add_many_from_roms(self, roms: list[dict]) -> None:
        added = 0
        for rom in roms:
            torrent = rom.get("torrent_file", "")
            if not torrent:
                continue
            try:
                file_id = int(rom.get("file_id") or 1)
            except (TypeError, ValueError):
                file_id = 1
            self._manager.add(
                torrent_file=torrent,
                file_id=file_id,
                game_name=rom.get("title", "rom"),
                console=rom.get("console") or tr('download.unknown'),
                source=rom.get("source", "Minerva"),
            )
            added += 1
        if added:
            InfoBar.success(
                title=tr('download.added_to_queue_title'),
                content=tr('download.added_to_queue_many', count=added),
                orient=Qt.Orientation.Horizontal,
                isClosable=True,
                position=InfoBarPosition.TOP_RIGHT,
                duration=2500,
                parent=self.window(),
            )