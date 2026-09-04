from __future__ import annotations
import logging
import os
import subprocess
import sys
from PySide6.QtCore import Qt, QSize, Signal, QThread, QTimer, QUrl
from PySide6.QtGui import QColor, QPainter, QDragEnterEvent, QDropEvent
from PySide6.QtWidgets import QAbstractItemView, QDialog, QFileDialog, QFormLayout, QFrame, QHBoxLayout, QLabel, QListWidget, QListWidgetItem, QVBoxLayout, QWidget
from qfluentwidgets import CaptionLabel, CardWidget, CheckBox, CompactSpinBox, DoubleSpinBox, FluentIcon, IconWidget, InfoBar, InfoBarPosition, LineEdit, MessageBox, MessageBoxBase, PrimaryPushButton, ProgressBar, PushButton, StrongBodyLabel, SubtitleLabel, ToolButton, TransparentToolButton, themeColor, qconfig
from src.core.downloader import DLState, DownloadItem, DownloadManager
from src.core.theme import palette, scroll_area_qss
from src.core.translations import tr, register_locale_refresh
from .repacks_page import SelectiveDownloadDialog, build_selective_entries_from_torrent, resolve_selective_file_indices, _FileListFetchThread
logger = logging.getLogger(__name__)
_STATE_COLOR_KEYS = {DLState.queued: 'state_queued', DLState.downloading: 'state_downloading', DLState.verifying: 'state_verifying', DLState.paused: 'state_paused', DLState.seeding: 'state_seeding', DLState.completed: 'state_completed', DLState.error: 'state_error', DLState.cancelled: 'state_cancelled'}
_STATE_LABEL_KEYS = {DLState.queued: 'download.state_queued', DLState.downloading: 'download.state_downloading', DLState.verifying: 'download.state_verifying', DLState.paused: 'download.state_paused', DLState.seeding: 'download.state_seeding', DLState.completed: 'download.state_completed', DLState.error: 'download.state_error', DLState.cancelled: 'download.state_cancelled'}

def _state_label(state) -> str:
    key = _STATE_LABEL_KEYS.get(state)
    return tr(key) if key else state.value

def _state_color(state) -> str:
    return palette()[_STATE_COLOR_KEYS.get(state, 'state_queued')]
_CARD_RADIUS = 14

def _muted_color() -> str:
    return palette()['muted']

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
        self.spin_down.setRange(0, 1000000)
        self.spin_down.setSuffix(tr('download.suffix_kbps_unlimited'))
        self.spin_down.setValue(item.max_down_kbps)
        form.addRow(tr('download.max_download_speed'), self.spin_down)
        self.spin_up = CompactSpinBox()
        self.spin_up.setRange(0, 1000000)
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
        return {'seed_after': self.chk_seed.isChecked(), 'max_down_kbps': self.spin_down.value(), 'max_up_kbps': self.spin_up.value(), 'max_peers': self.spin_peers.value(), 'ratio_limit': self.spin_ratio.value(), 'seed_time_limit_min': self.spin_seed_time.value()}

class _DropZone(QFrame):
    files_dropped = Signal(list)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAcceptDrops(True)
        self.setFixedHeight(120)
        self.setFrameShape(QFrame.Shape.NoFrame)
        layout = QVBoxLayout(self)
        layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.setSpacing(6)
        icon = IconWidget(FluentIcon.DOWNLOAD)
        icon.setFixedSize(28, 28)
        icon_row = QHBoxLayout()
        icon_row.setAlignment(Qt.AlignmentFlag.AlignCenter)
        icon_row.addWidget(icon)
        layout.addLayout(icon_row)
        label = CaptionLabel(tr('download.drop_hint'))
        label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        label.setStyleSheet(f'color: {_muted_color()};')
        layout.addWidget(label)
        self._refresh_style()
        qconfig.themeChanged.connect(lambda *_: self._refresh_style())

    def _refresh_style(self, active: bool=False) -> None:
        c = palette()
        border = c['card_hover'] if active else c['card_border']
        self.setStyleSheet(f'_DropZone {{ background-color: {c["surface_tint"]}; border: 2px dashed {border}; border-radius: 10px; }}')

    def dragEnterEvent(self, event: QDragEnterEvent) -> None:
        if event.mimeData().hasUrls():
            self._refresh_style(active=True)
            event.acceptProposedAction()

    def dragLeaveEvent(self, event) -> None:
        self._refresh_style(active=False)

    def dropEvent(self, event: QDropEvent) -> None:
        self._refresh_style(active=False)
        paths = [u.toLocalFile() for u in event.mimeData().urls() if u.toLocalFile().lower().endswith('.torrent')]
        if paths:
            self.files_dropped.emit(paths)
        event.acceptProposedAction()

class AddTorrentDialog(MessageBoxBase):
    torrent_file_chosen = Signal(str)
    magnet_submitted = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.titleLabel = SubtitleLabel(tr('download.add_torrent'), self)
        self.viewLayout.addWidget(self.titleLabel)
        self._drop_zone = _DropZone(self)
        self._drop_zone.files_dropped.connect(self._on_files_dropped)
        self.viewLayout.addWidget(self._drop_zone)
        browse_row = QHBoxLayout()
        browse_row.addStretch(1)
        self._browse_btn = PushButton(tr('download.browse_torrent_file'))
        self._browse_btn.clicked.connect(self._on_browse)
        browse_row.addWidget(self._browse_btn)
        browse_row.addStretch(1)
        self.viewLayout.addLayout(browse_row)
        self.viewLayout.addSpacing(8)
        magnet_label = CaptionLabel(tr('download.magnet_hint'))
        magnet_label.setStyleSheet(f'color: {_muted_color()};')
        self.viewLayout.addWidget(magnet_label)
        self._magnet_edit = LineEdit(self)
        self._magnet_edit.setPlaceholderText(tr('download.magnet_placeholder'))
        self._magnet_edit.setClearButtonEnabled(True)
        self.viewLayout.addWidget(self._magnet_edit)
        self.yesButton.setText(tr('download.add'))
        self.cancelButton.setText(tr('download.cancel'))
        self.widget.setMinimumWidth(440)
        self._chosen_file = ''

    def _on_files_dropped(self, paths: list) -> None:
        self._chosen_file = paths[0]
        self._magnet_edit.clear()
        self.accept()

    def _on_browse(self) -> None:
        path, _f = QFileDialog.getOpenFileName(self, tr('download.browse_torrent_file'), '', 'Torrent Files (*.torrent)')
        if path:
            self._chosen_file = path
            self._magnet_edit.clear()
            self.accept()

    def accept(self) -> None:
        if self._chosen_file:
            self.torrent_file_chosen.emit(self._chosen_file)
            super().accept()
            return
        magnet = self._magnet_edit.text().strip()
        if magnet:
            self.magnet_submitted.emit(magnet)
            super().accept()

class DonateDialog(MessageBoxBase):
    WALLET_ADDRESS = 'TRF43NXf4rQTpKZT5oc6rf38j9PJ8RgGNs'

    def __init__(self, parent=None):
        super().__init__(parent)
        self.titleLabel = SubtitleLabel(tr('download.support_with_crypto'), self)
        self.titleLabel.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.viewLayout.addWidget(self.titleLabel)
        sub = CaptionLabel('USDT (TRC-20)')
        sub.setAlignment(Qt.AlignmentFlag.AlignCenter)
        sub.setStyleSheet(f'color: {themeColor().name()}; font-weight: 700;')
        self.viewLayout.addWidget(sub)
        self.viewLayout.addSpacing(6)
        from PySide6.QtGui import QPixmap
        qr_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'photos', 'donate_qr.png')
        qr_lbl = QLabel(self)
        qr_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        pix = QPixmap(qr_path)
        if not pix.isNull():
            qr_lbl.setPixmap(pix.scaled(220, 220, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation))
        qr_row = QHBoxLayout()
        qr_row.addStretch(1)
        qr_row.addWidget(qr_lbl)
        qr_row.addStretch(1)
        self.viewLayout.addLayout(qr_row)
        self.viewLayout.addSpacing(10)
        addr_card = CardWidget(self)
        addr_layout = QHBoxLayout(addr_card)
        addr_layout.setContentsMargins(14, 10, 14, 10)
        addr_lbl = CaptionLabel(self.WALLET_ADDRESS)
        addr_lbl.setWordWrap(True)
        addr_layout.addWidget(addr_lbl, 1)
        copy_btn = TransparentToolButton(FluentIcon.COPY, addr_card)
        copy_btn.setFixedSize(26, 26)
        copy_btn.clicked.connect(self._copy_address)
        addr_layout.addWidget(copy_btn, 0, Qt.AlignmentFlag.AlignVCenter)
        addr_card.setCursor(Qt.CursorShape.PointingHandCursor)
        addr_card.mousePressEvent = lambda _e: self._copy_address()
        self.viewLayout.addWidget(addr_card)
        self.yesButton.setText(tr('download.close'))
        self.hideCancelButton()
        self.widget.setMinimumWidth(360)

    def _copy_address(self) -> None:
        from PySide6.QtWidgets import QApplication
        QApplication.clipboard().setText(self.WALLET_ADDRESS)
        InfoBar.success(title=tr('download.copied'), content='', orient=Qt.Orientation.Horizontal, isClosable=True, position=InfoBarPosition.TOP_RIGHT, duration=1500, parent=self.window())

class _StatusDot(QWidget):

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedSize(8, 8)
        self._color = QColor(_state_color(DLState.queued))
        self._brush = self._color

    def set_color(self, hex_color: str) -> None:
        if self._color.name() == hex_color:
            return
        self._color = QColor(hex_color)
        self._brush = self._color
        self.update()

    def paintEvent(self, event):
        p = QPainter(self)
        p.setBrush(self._brush)
        p.setPen(Qt.PenStyle.NoPen)
        p.drawEllipse(self.rect())

class DownloadItemWidget(CardWidget):
    request_pause = Signal(str)
    request_resume = Signal(str)
    request_cancel = Signal(str)
    request_retry = Signal(str)
    request_remove = Signal(str)
    request_open_file = Signal(str)
    request_open_folder = Signal(str)
    request_settings = Signal(str)

    def __init__(self, item: DownloadItem, parent=None):
        super().__init__(parent)
        self.item_id = item.id
        self.setFixedHeight(112)
        self.setBorderRadius(_CARD_RADIUS)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(20, 14, 16, 14)
        outer.setSpacing(9)
        top_row = QHBoxLayout()
        top_row.setSpacing(10)
        self._dot = _StatusDot()
        top_row.addWidget(self._dot, 0, Qt.AlignmentFlag.AlignVCenter)
        self._title = StrongBodyLabel(item.game_name)
        self._title.setObjectName('downloadTitleLabel')
        self._title.setStyleSheet('QLabel#downloadTitleLabel { font-size: 13.5px; }')
        self._title.setWordWrap(False)
        top_row.addWidget(self._title, 1)
        self._kind_label = CaptionLabel('')
        self._kind_label.setStyleSheet('border-radius: 9px; padding: 2px 9px; font-size: 9.5px; font-weight: 700;')
        top_row.addWidget(self._kind_label, 0, Qt.AlignmentFlag.AlignVCenter)
        self._state_label = CaptionLabel(item.state.value)
        self._state_label.setStyleSheet('font-weight: 700;')
        top_row.addWidget(self._state_label, 0, Qt.AlignmentFlag.AlignVCenter)
        outer.addLayout(top_row)
        self._meta = CaptionLabel(f'{item.console}   ·   {item.source}')
        self._meta.setStyleSheet(f'color: {_muted_color()};')
        outer.addWidget(self._meta)
        prog_row = QHBoxLayout()
        prog_row.setSpacing(10)
        self._progress = ProgressBar(useAni=False)
        self._progress.setFixedHeight(5)
        self._progress.setRange(0, 100)
        prog_row.addWidget(self._progress, 1)
        self._pct_label = CaptionLabel('0%')
        self._pct_label.setFixedWidth(38)
        self._pct_label.setStyleSheet('font-weight: 600;')
        self._pct_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        prog_row.addWidget(self._pct_label)
        outer.addLayout(prog_row)
        info_row = QHBoxLayout()
        info_row.setSpacing(16)
        self._size_label = CaptionLabel('0 B / 0 B')
        self._speed_label = CaptionLabel('↓ 0 B/s   ↑ 0 B/s')
        self._extra_label = CaptionLabel('')
        for lbl in (self._size_label, self._speed_label, self._extra_label):
            lbl.setStyleSheet(f'color: {_muted_color()};')
            info_row.addWidget(lbl)
        info_row.addStretch(1)
        self._btn_pause = ToolButton(FluentIcon.PAUSE)
        self._btn_pause.setToolTip(tr('download.pause'))
        self._btn_resume = ToolButton(FluentIcon.PLAY)
        self._btn_resume.setToolTip(tr('download.resume'))
        self._btn_retry = ToolButton(FluentIcon.SYNC)
        self._btn_retry.setToolTip(tr('download.retry'))
        self._btn_open_file = ToolButton(FluentIcon.DOCUMENT)
        self._btn_open_file.setToolTip(tr('download.open_file'))
        self._btn_folder = ToolButton(FluentIcon.FOLDER)
        self._btn_folder.setToolTip(tr('download.open_folder'))
        self._btn_settings = ToolButton(FluentIcon.SETTING)
        self._btn_settings.setToolTip(tr('download.torrent_settings'))
        self._btn_cancel = ToolButton(FluentIcon.CLOSE)
        self._btn_cancel.setToolTip(tr('download.cancel'))
        self._btn_remove = ToolButton(FluentIcon.DELETE)
        self._btn_remove.setToolTip(tr('download.remove'))
        for b in (self._btn_pause, self._btn_resume, self._btn_retry, self._btn_open_file, self._btn_folder, self._btn_settings, self._btn_cancel, self._btn_remove):
            b.setFixedSize(28, 28)
            b.setIconSize(QSize(13, 13))
            b.setStyleSheet('ToolButton { border-radius: 8px; }')
            info_row.addWidget(b)
        outer.addLayout(info_row)
        self._btn_pause.clicked.connect(lambda: self.request_pause.emit(self.item_id))
        self._btn_resume.clicked.connect(lambda: self.request_resume.emit(self.item_id))
        self._btn_cancel.clicked.connect(lambda: self.request_cancel.emit(self.item_id))
        self._btn_retry.clicked.connect(lambda: self.request_retry.emit(self.item_id))
        self._btn_remove.clicked.connect(lambda: self.request_remove.emit(self.item_id))
        self._btn_open_file.clicked.connect(lambda: self.request_open_file.emit(self.item_id))
        self._btn_folder.clicked.connect(lambda: self.request_open_folder.emit(self.item_id))
        self._btn_settings.clicked.connect(lambda: self.request_settings.emit(self.item_id))
        self._item = item
        self._last_muted_color = None
        self._last_state_color = None
        self.update_from_item(item)
        qconfig.themeChanged.connect(self._on_theme_changed)

    def _on_theme_changed(self, *_):
        try:
            self.update_from_item(self._item)
        except RuntimeError:
            pass

    def update_from_item(self, item: DownloadItem) -> None:
        self._item = item
        is_external = item.backend == 'external'
        muted = _muted_color()
        if muted != self._last_muted_color:
            self._last_muted_color = muted
            muted_style = f'color: {muted};'
            self._meta.setStyleSheet(muted_style)
            self._size_label.setStyleSheet(muted_style)
            self._speed_label.setStyleSheet(muted_style)
            c = palette()
            self._kind_label.setStyleSheet(f'color: {muted}; background-color: {c["surface_tint_strong"]}; border-radius: 8px; padding: 1px 8px; font-size: 9.5px; font-weight: 600;')
        kind_text = 'Direct' if is_external else 'Torrent'
        if self._kind_label.text() != kind_text:
            self._kind_label.setText(kind_text)
        if self._title.text() != item.game_name:
            self._title.setText(item.game_name)
            self._title.setToolTip(item.game_name)
        meta_text = f'{item.console}   ·   {item.source}'
        if self._meta.text() != meta_text:
            self._meta.setText(meta_text)
        color = _state_color(item.state)
        self._dot.set_color(color)
        state_text = _state_label(item.state)
        if self._state_label.text() != state_text or color != self._last_state_color:
            self._last_state_color = color
            self._state_label.setText(state_text)
            self._state_label.setStyleSheet(f'font-weight: 600; color: {color};')
        pct = int(item.progress)
        self._progress.setValue(max(0, min(100, pct)))
        self._progress.setVisible(item.state != DLState.error)
        self._pct_label.setText(f'{pct}%' if item.state != DLState.error else '—')
        self._size_label.setText(item.display_size())
        if item.state == DLState.seeding:
            self._speed_label.setText(f'↓ {item.speed_down}   ↑ {item.speed_up}')
            self._extra_label.setText(tr('download.seeding_stats', time=item.seed_time, ratio=f'{item.ratio:.2f}', peers=item.peers))
        elif item.state == DLState.error:
            self._speed_label.setText('')
            self._extra_label.setText((item.error or tr('download.unknown_error'))[:70])
            self._extra_label.setStyleSheet(f'color: {_state_color(DLState.error)};')
        elif item.state == DLState.completed:
            self._speed_label.setText('')
            self._extra_label.setText(tr('download.done'))
        elif item.state == DLState.queued:
            self._speed_label.setText('')
            self._extra_label.setText(tr('download.waiting_for_slot'))
        elif is_external:
            self._speed_label.setText(f'↓ {item.speed_down}')
            self._extra_label.setStyleSheet(f'color: {muted};')
            self._extra_label.setText('')
        else:
            self._speed_label.setText(f'↓ {item.speed_down}   ↑ {item.speed_up}')
            self._extra_label.setStyleSheet(f'color: {muted};')
            self._extra_label.setText(tr('download.eta_peers', eta=item.eta, peers=item.peers))
        can_pause = not is_external and item.state in (DLState.downloading, DLState.verifying, DLState.seeding)
        can_resume = not is_external and item.state == DLState.paused
        can_retry = item.state in (DLState.error, DLState.cancelled)
        can_cancel = item.state in (DLState.downloading, DLState.verifying, DLState.queued, DLState.paused, DLState.seeding)
        can_open_folder = bool(item.download_path) or item.state in (DLState.completed, DLState.seeding)
        can_open_file = item.state in (DLState.completed, DLState.seeding) and bool(item.download_path)
        is_repack = item.category == 'repacks'
        if is_repack:
            self._btn_open_file.setIcon(FluentIcon.PLAY)
            self._btn_open_file.setToolTip(tr('download.install'))
        else:
            self._btn_open_file.setIcon(FluentIcon.DOCUMENT)
            self._btn_open_file.setToolTip(tr('download.open_file'))
        self._btn_pause.setToolTip(tr('download.pause_seeding') if item.state == DLState.seeding else tr('download.pause'))
        self._btn_pause.setVisible(can_pause)
        self._btn_resume.setVisible(can_resume)
        self._btn_retry.setVisible(can_retry)
        self._btn_cancel.setVisible(can_cancel)
        self._btn_remove.setVisible(not can_cancel)
        self._btn_open_file.setEnabled(can_open_file)
        self._btn_folder.setEnabled(can_open_folder)
        self._btn_settings.setVisible(not is_external)
        self._btn_settings.setEnabled(item.state not in (DLState.completed, DLState.cancelled))

class _StatTile(CardWidget):

    def __init__(self, icon, title_key: str, parent=None):
        super().__init__(parent)
        self.setBorderRadius(12)
        self._title_key = title_key
        layout = QHBoxLayout(self)
        layout.setContentsMargins(16, 12, 16, 12)
        layout.setSpacing(12)
        self._badge = QLabel()
        self._badge.setFixedSize(36, 36)
        self._icon = IconWidget(icon, self._badge)
        self._icon.setFixedSize(16, 16)
        self._icon.move(10, 10)
        layout.addWidget(self._badge, 0, Qt.AlignmentFlag.AlignVCenter)
        col = QVBoxLayout()
        col.setSpacing(1)
        col.setContentsMargins(0, 0, 0, 0)
        self.val_lbl = StrongBodyLabel('0')
        self.val_lbl.setStyleSheet('font-size: 16.5px;')
        self.cap_lbl = CaptionLabel(tr(title_key))
        col.addWidget(self.val_lbl)
        col.addWidget(self.cap_lbl)
        layout.addLayout(col, 1)
        self.refresh_theme()
        qconfig.themeChanged.connect(lambda *_: self.refresh_theme())

    def refresh_theme(self):
        c = palette()
        self._badge.setStyleSheet(f'background-color: {c["surface_tint_strong"]}; border-radius: 18px;')
        self.cap_lbl.setStyleSheet(f'color: {c["muted"]};')

class StatsBar(QWidget):

    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)
        self._tiles: dict[str, _StatTile] = {}
        fields = (
            ('active', 'download.stats_active', FluentIcon.SPEED_HIGH),
            ('down', 'download.stats_download_speed', FluentIcon.DOWNLOAD),
            ('up', 'download.stats_upload_speed', FluentIcon.SHARE),
            ('queued', 'download.stats_queued', FluentIcon.PAUSE_BOLD),
            ('completed', 'download.stats_completed', FluentIcon.ACCEPT),
        )
        for key, title_key, icon in fields:
            tile = _StatTile(icon, title_key)
            layout.addWidget(tile, 1)
            self._tiles[key] = tile

    def retranslate(self) -> None:
        for tile in self._tiles.values():
            tile.cap_lbl.setText(tr(tile._title_key))

    def update_stats(self, summary: dict) -> None:
        self._tiles['active'].val_lbl.setText(str(summary['active']))
        self._tiles['down'].val_lbl.setText(summary['total_down'])
        self._tiles['up'].val_lbl.setText(summary['total_up'])
        self._tiles['queued'].val_lbl.setText(str(summary['queued']))
        self._tiles['completed'].val_lbl.setText(str(summary['completed']))

class DownloadManagerPage(QWidget):

    def __init__(self, manager: DownloadManager, parent=None):
        super().__init__(parent)
        self.setStyleSheet('background: transparent;')
        self.setAcceptDrops(True)
        self._manager = manager
        self._filelist_thread: _FileListFetchThread | None = None
        root = QVBoxLayout(self)
        root.setContentsMargins(28, 12, 32, 24)
        root.setSpacing(18)
        stats_row = QHBoxLayout()
        stats_row.setContentsMargins(0, 0, 0, 0)
        stats_row.setSpacing(12)
        self._stats = StatsBar()
        stats_row.addWidget(self._stats, 1)
        self._btn_add = ToolButton(FluentIcon.ADD)
        self._btn_add.setFixedSize(32, 32)
        self._btn_add.setToolTip(tr('download.add_torrent'))
        self._btn_add.clicked.connect(self._on_add_clicked)
        stats_row.addWidget(self._btn_add, 0, Qt.AlignmentFlag.AlignVCenter)
        self._btn_pause_all = ToolButton(FluentIcon.PAUSE)
        self._btn_pause_all.setFixedSize(32, 32)
        self._btn_pause_all.setToolTip(tr('download.pause_all'))
        self._btn_pause_all.clicked.connect(self._on_pause_all_clicked)
        stats_row.addWidget(self._btn_pause_all, 0, Qt.AlignmentFlag.AlignVCenter)
        self._btn_resume_all = ToolButton(FluentIcon.PLAY)
        self._btn_resume_all.setFixedSize(32, 32)
        self._btn_resume_all.setToolTip(tr('download.resume_all'))
        self._btn_resume_all.clicked.connect(self._on_resume_all_clicked)
        stats_row.addWidget(self._btn_resume_all, 0, Qt.AlignmentFlag.AlignVCenter)
        self._btn_delete_all = ToolButton(FluentIcon.DELETE)
        self._btn_delete_all.setFixedSize(32, 32)
        self._btn_delete_all.setToolTip(tr('download.delete_all'))
        self._btn_delete_all.clicked.connect(self._on_delete_all_clicked)
        stats_row.addWidget(self._btn_delete_all, 0, Qt.AlignmentFlag.AlignVCenter)
        self._btn_open_downloads_root = PushButton(FluentIcon.FOLDER, tr('download.open_downloads_folder'))
        self._btn_open_downloads_root.clicked.connect(self._on_open_downloads_root)
        stats_row.addWidget(self._btn_open_downloads_root, 0, Qt.AlignmentFlag.AlignVCenter)
        root.addLayout(stats_row)
        self._body = QWidget()
        body_layout = QVBoxLayout(self._body)
        body_layout.setContentsMargins(0, 0, 0, 0)
        body_layout.setSpacing(0)
        self._list = QListWidget()
        self._list.setSpacing(10)
        self._list.setFrameShape(QFrame.Shape.NoFrame)
        self._list.setObjectName('downloadQueueList')
        self._list.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove)
        self._list.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self._list.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self._list.setUniformItemSizes(True)
        self._list.setResizeMode(QListWidget.ResizeMode.Fixed)
        self._list.setLayoutMode(QListWidget.LayoutMode.Batched)
        self._list.setBatchSize(20)
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
        self._btn_open_downloads_root.setText(tr('download.open_downloads_folder'))
        self._btn_pause_all.setToolTip(tr('download.pause_all'))
        self._btn_resume_all.setToolTip(tr('download.resume_all'))
        self._btn_delete_all.setToolTip(tr('download.delete_all'))
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
        self._list.setStyleSheet(f'\n            QListWidget {{\n                background-color: {c['list_bg']};\n                border: none;\n                outline: none;\n            }}\n            QListWidget::item {{\n                background: transparent;\n            }}\n            {scroll_area_qss()}\n            ')

    def _apply_card_style(self):
        c = palette()
        self._list.setStyleSheet(self._list.styleSheet() + f'DownloadItemWidget {{ background-color: {c['card_bg']}; border: 1px solid {c['card_border']}; border-radius: {_CARD_RADIUS}px; padding: 0px; }} DownloadItemWidget:hover {{ background-color: {c['card_hover']}; border: 1px solid {c['card_border']}; border-radius: {_CARD_RADIUS}px; }} ')

    def _build_empty_state(self) -> QWidget:
        w = QWidget()
        layout = QVBoxLayout(w)
        layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.setSpacing(10)
        icon = ToolButton(FluentIcon.DOWNLOAD)
        icon.setEnabled(False)
        icon.setFixedSize(64, 64)
        icon.setIconSize(QSize(26, 26))

        def _refresh_icon_bg():
            icon.setStyleSheet(f'ToolButton {{ border-radius: 32px; background-color: {palette()['surface_tint_strong']}; }}')
        _refresh_icon_bg()
        qconfig.themeChanged.connect(lambda *_: _refresh_icon_bg())
        icon_row = QHBoxLayout()
        icon_row.setAlignment(Qt.AlignmentFlag.AlignCenter)
        icon_row.addWidget(icon)
        layout.addLayout(icon_row)
        layout.addSpacing(4)
        title = StrongBodyLabel(tr('download.no_downloads_yet'))
        title.setStyleSheet('font-size: 15px;')
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(title)
        self._empty_title_lbl = title
        sub = CaptionLabel(tr('download.empty_hint'))
        sub.setAlignment(Qt.AlignmentFlag.AlignCenter)
        sub.setStyleSheet(f'color: {_muted_color()};')
        qconfig.themeChanged.connect(lambda *_: sub.setStyleSheet(f'color: {_muted_color()};'))
        layout.addWidget(sub)
        self._empty_sub_lbl = sub
        return w

    def _rebuild_all(self) -> None:
        self._mutating_list = True
        self._list.setUpdatesEnabled(False)
        try:
            for widget in self._row_widgets.values():
                try:
                    qconfig.themeChanged.disconnect(widget._on_theme_changed)
                except (TypeError, RuntimeError):
                    pass
                widget.setParent(None)
                widget.deleteLater()
            self._list.clear()
            self._row_widgets.clear()
            for item in self._manager.items_in_order():
                self._insert_row(item)
        finally:
            self._mutating_list = False
            self._list.setUpdatesEnabled(True)
        self._update_empty_state()

    def _insert_row(self, item: DownloadItem) -> None:
        widget = DownloadItemWidget(item)
        widget.request_pause.connect(self._manager.pause)
        widget.request_resume.connect(self._manager.resume)
        widget.request_cancel.connect(self._on_cancel_clicked)
        widget.request_retry.connect(self._manager.retry)
        widget.request_remove.connect(self._on_remove_clicked)
        widget.request_open_file.connect(self._on_open_file)
        widget.request_open_folder.connect(self._on_open_folder)
        widget.request_settings.connect(self._on_settings_clicked)
        list_item = QListWidgetItem(self._list)
        list_item.setData(Qt.ItemDataRole.UserRole, item.id)
        list_item.setSizeHint(QSize(0, 122))
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
        try:
            qconfig.themeChanged.disconnect(widget._on_theme_changed)
        except (TypeError, RuntimeError):
            pass
        widget.setParent(None)
        widget.deleteLater()
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
        box = MessageBox(tr('download.cancel_download_title'), tr('download.cancel_download_content'), self.window())
        box.yesButton.setText(tr('download.yes'))
        box.cancelButton.setText(tr('download.no'))
        if not box.exec():
            return
        self._manager.cancel(item_id, delete_files=False)

    def _on_remove_clicked(self, item_id: str) -> None:
        box = MessageBoxBase(self.window())
        box_layout = QVBoxLayout()
        box_layout.addWidget(StrongBodyLabel(tr('download.remove_download_title')))
        content_lbl = CaptionLabel(tr('download.remove_download_content'))
        content_lbl.setWordWrap(True)
        box_layout.addWidget(content_lbl)
        box.viewLayout.addLayout(box_layout)
        choice = {'value': 'cancel'}

        def _pick(val):
            choice['value'] = val
            box.accept()
        try:
            box.yesButton.clicked.disconnect()
        except TypeError:
            pass
        try:
            box.cancelButton.clicked.disconnect()
        except TypeError:
            pass
        box.yesButton.setText(tr('download.delete_file'))
        box.yesButton.clicked.connect(lambda: _pick('delete'))
        box.cancelButton.setText(tr('download.keep_file'))
        box.cancelButton.clicked.connect(lambda: _pick('keep'))
        cancel_btn = PushButton(tr('download.cancel_action'))
        cancel_btn.clicked.connect(lambda: _pick('cancel'))
        button_row = None
        try:
            button_row = box.cancelButton.parentWidget().layout()
        except Exception:
            button_row = None
        if button_row is not None:
            button_row.insertWidget(0, cancel_btn)
        else:
            box.viewLayout.addWidget(cancel_btn)
        box.exec()
        if choice['value'] == 'cancel':
            return
        self._manager.remove(item_id, delete_files=(choice['value'] == 'delete'))

    def _on_pause_all_clicked(self) -> None:
        self._manager.pause_all()

    def _on_resume_all_clicked(self) -> None:
        self._manager.resume_all()

    def _on_delete_all_clicked(self) -> None:
        if not self._manager.items_in_order():
            return
        box = MessageBoxBase(self.window())
        box_layout = QVBoxLayout()
        box_layout.addWidget(StrongBodyLabel(tr('download.delete_all_title')))
        content_lbl = CaptionLabel(tr('download.delete_all_content'))
        content_lbl.setWordWrap(True)
        box_layout.addWidget(content_lbl)
        box.viewLayout.addLayout(box_layout)
        choice = {'value': 'cancel'}

        def _pick(val):
            choice['value'] = val
            box.accept()
        try:
            box.yesButton.clicked.disconnect()
        except TypeError:
            pass
        try:
            box.cancelButton.clicked.disconnect()
        except TypeError:
            pass
        box.yesButton.setText(tr('download.delete_file'))
        box.yesButton.clicked.connect(lambda: _pick('delete'))
        box.cancelButton.setText(tr('download.keep_file'))
        box.cancelButton.clicked.connect(lambda: _pick('keep'))
        cancel_btn = PushButton(tr('download.cancel_action'))
        cancel_btn.clicked.connect(lambda: _pick('cancel'))
        button_row = None
        try:
            button_row = box.cancelButton.parentWidget().layout()
        except Exception:
            button_row = None
        if button_row is not None:
            button_row.insertWidget(0, cancel_btn)
        else:
            box.viewLayout.addWidget(cancel_btn)
        box.exec()
        if choice['value'] == 'cancel':
            return
        self._manager.remove_all(delete_files=(choice['value'] == 'delete'))

    def dragEnterEvent(self, event: QDragEnterEvent) -> None:
        if event.mimeData().hasUrls() and any(u.toLocalFile().lower().endswith('.torrent') for u in event.mimeData().urls()):
            event.acceptProposedAction()

    def dropEvent(self, event: QDropEvent) -> None:
        paths = [u.toLocalFile() for u in event.mimeData().urls() if u.toLocalFile().lower().endswith('.torrent')]
        for path in paths:
            self._start_selective_add(path)
        event.acceptProposedAction()

    def _on_add_clicked(self) -> None:
        dlg = AddTorrentDialog(self.window())
        dlg.torrent_file_chosen.connect(self._start_selective_add)
        dlg.magnet_submitted.connect(self._start_selective_add)
        dlg.exec()

    def _start_selective_add(self, source: str) -> None:
        if self._filelist_thread is not None and self._filelist_thread.isRunning():
            InfoBar.warning(title=tr('download.loading_file_list'), content='', orient=Qt.Orientation.Horizontal, isClosable=True, position=InfoBarPosition.TOP_RIGHT, duration=2000, parent=self.window())
            return
        thread = _FileListFetchThread(self._manager, source, None)
        thread.finished_ok.connect(lambda files: self._on_add_file_list_ready(source, files))
        thread.finished_err.connect(lambda err: self._on_add_file_list_failed(source, err))
        thread.finished.connect(self._on_add_filelist_finished)
        self._filelist_thread = thread
        thread.start()

    def _on_add_filelist_finished(self) -> None:
        thread = self._filelist_thread
        self._filelist_thread = None
        if thread is not None:
            thread.deleteLater()

    def _on_add_file_list_failed(self, source: str, err: str) -> None:
        logger.error('Failed to fetch file list for added torrent: %s', err)
        self._queue_new_download(source, file_ids=None)

    def _on_add_file_list_ready(self, source: str, torrent_files: list) -> None:
        if not torrent_files:
            self._queue_new_download(source, file_ids=None)
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
            file_ids = None
        self._queue_new_download(source, file_ids=file_ids)

    def _queue_new_download(self, source: str, file_ids: list | None) -> None:
        name = os.path.splitext(os.path.basename(source))[0] if not source.startswith('magnet:') else tr('download.rom_fallback')
        self._manager.add(torrent_file=source, file_id=file_ids[0] if file_ids else 1, file_ids=file_ids, game_name=name, console=tr('download.unknown'), source='Manual')
        InfoBar.success(title=tr('download.added_to_queue_title'), content=tr('download.added_to_queue_single', title=name), orient=Qt.Orientation.Horizontal, isClosable=True, position=InfoBarPosition.TOP_RIGHT, duration=2500, parent=self.window())

    def _on_open_downloads_root(self) -> None:
        from src.core.config import paths
        path = paths.download_root
        try:
            os.makedirs(path, exist_ok=True)
            if sys.platform.startswith('win'):
                os.startfile(path)
            elif sys.platform == 'darwin':
                subprocess.Popen(['open', path])
            else:
                subprocess.Popen(['xdg-open', path])
        except Exception:
            logger.exception('Failed to open downloads root %s', path)

    def _on_open_file(self, item_id: str) -> None:
        item = self._manager.get(item_id)
        if item and item.category == 'repacks':
            path = self._manager.find_repack_installer(item_id)
            if not path:
                InfoBar.warning(title=tr('download.installer_not_found_title'), content=tr('download.installer_not_found_content'), orient=Qt.Orientation.Horizontal, isClosable=True, position=InfoBarPosition.TOP_RIGHT, duration=3000, parent=self.window())
                return
        else:
            path = self._manager.open_file(item_id)
            if not path:
                InfoBar.warning(title=tr('download.folder_not_found_title'), content=tr('download.folder_not_found_content'), orient=Qt.Orientation.Horizontal, isClosable=True, position=InfoBarPosition.TOP_RIGHT, duration=3000, parent=self.window())
                return
        try:
            if sys.platform.startswith('win'):
                os.startfile(path)
            elif sys.platform == 'darwin':
                subprocess.Popen(['open', path])
            else:
                subprocess.Popen(['xdg-open', path])
        except Exception:
            logger.exception('Failed to open file %s', path)

    def _on_open_folder(self, item_id: str) -> None:
        path = self._manager.open_folder(item_id)
        if not path:
            InfoBar.warning(title=tr('download.folder_not_found_title'), content=tr('download.folder_not_found_content'), orient=Qt.Orientation.Horizontal, isClosable=True, position=InfoBarPosition.TOP_RIGHT, duration=3000, parent=self.window())
            return
        item = self._manager.get(item_id)
        file_path = item.download_path if item and item.download_path and os.path.isfile(item.download_path) else None
        try:
            if sys.platform.startswith('win'):
                if file_path:
                    subprocess.Popen(['explorer', '/select,', os.path.normpath(file_path)])
                else:
                    os.startfile(path)
            elif sys.platform == 'darwin':
                if file_path:
                    subprocess.Popen(['open', '-R', file_path])
                else:
                    subprocess.Popen(['open', path])
            else:
                subprocess.Popen(['xdg-open', path])
        except Exception:
            logger.exception('Failed to open folder %s', path)

    def _on_settings_clicked(self, item_id: str) -> None:
        item = self._manager.get(item_id)
        if item is None:
            return
        dlg = TorrentSettingsDialog(item, parent=self.window())
        if dlg.exec():
            self._manager.set_torrent_settings(item_id, **dlg.values())
            if getattr(dlg, 'recheck_requested', False):
                self._manager.force_recheck(item_id)

    def add_from_rom(self, rom: dict) -> None:
        torrent = rom.get('torrent_file', '')
        if not torrent:
            InfoBar.warning(title=tr('download.no_torrent_title'), content=tr('download.no_torrent_content'), orient=Qt.Orientation.Horizontal, isClosable=True, position=InfoBarPosition.TOP_RIGHT, duration=3000, parent=self.window())
            return
        try:
            file_id = int(rom.get('file_id') or 1)
        except (TypeError, ValueError):
            file_id = 1
        self._manager.add(torrent_file=torrent, file_id=file_id, game_name=rom.get('title', 'rom'), console=rom.get('console') or tr('download.unknown'), source=rom.get('source', 'Minerva'))
        InfoBar.success(title=tr('download.added_to_queue_title'), content=tr('download.added_to_queue_single', title=rom.get('title') or tr('download.rom_fallback')), orient=Qt.Orientation.Horizontal, isClosable=True, position=InfoBarPosition.TOP_RIGHT, duration=2500, parent=self.window())

    def add_many_from_roms(self, roms: list[dict]) -> None:
        added = 0
        for rom in roms:
            torrent = rom.get('torrent_file', '')
            if not torrent:
                continue
            try:
                file_id = int(rom.get('file_id') or 1)
            except (TypeError, ValueError):
                file_id = 1
            self._manager.add(torrent_file=torrent, file_id=file_id, game_name=rom.get('title', 'rom'), console=rom.get('console') or tr('download.unknown'), source=rom.get('source', 'Minerva'))
            added += 1
        if added:
            InfoBar.success(title=tr('download.added_to_queue_title'), content=tr('download.added_to_queue_many', count=added), orient=Qt.Orientation.Horizontal, isClosable=True, position=InfoBarPosition.TOP_RIGHT, duration=2500, parent=self.window())