from __future__ import annotations

import logging

from PySide6.QtCore import Qt, QObject, QRunnable, QThreadPool, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import QWidget, QStackedWidget, QVBoxLayout, QHBoxLayout, QLabel
from qfluentwidgets import (
    CardWidget, FluentIcon, BodyLabel, StrongBodyLabel,
    CaptionLabel, LineEdit, PrimaryPushButton, PushButton,
    ImageLabel, ComboBox, SegmentedWidget, SmoothScrollArea, IndeterminateProgressRing,
    ProgressBar, InfoBar, InfoBarPosition,
)

from src.core.yt import provider as yt_provider
from src.core.worker import submit
from src.core.artwork import artwork
from src.core.theme import palette
from src.core.translations import tr, register_locale_refresh

logger = logging.getLogger(__name__)

_THUMB_W = 320
_THUMB_H = 180


def _muted() -> str:
    return palette()['muted']


def _primary_text() -> str:
    return palette()['primary_text']


def _card_bg() -> str:
    return palette()['card_bg']


def _fmt_mb(n: int) -> str:
    return f"{n / 1024 / 1024:.1f}"


class _FfmpegSignals(QObject):
    progress = Signal(int, int)
    finished = Signal()
    failed = Signal(str)


class _FfmpegDownloadJob(QRunnable):
    def __init__(self, signals: _FfmpegSignals):
        super().__init__()
        self._signals = signals
        self._cancelled = False
        self.setAutoDelete(True)

    def cancel(self) -> None:
        self._cancelled = True

    def _is_cancelled(self) -> bool:
        return self._cancelled

    def _on_progress(self, downloaded: int, total: int) -> None:
        self._signals.progress.emit(downloaded, total)

    def run(self) -> None:
        try:
            yt_provider.download_ffmpeg(self._on_progress, is_cancelled=self._is_cancelled)
        except yt_provider.YtError as exc:
            if self._cancelled:
                return
            self._signals.failed.emit(str(exc))
            return
        except Exception as exc:
            if self._cancelled:
                return
            logger.exception('ffmpeg download failed')
            self._signals.failed.emit(str(exc))
            return
        if self._cancelled:
            return
        self._signals.finished.emit()


class YtPage(QWidget):
    ffmpeg_declined = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._bridge = None
        self._current_info: dict | None = None
        self._fetch_task_id: str | None = None
        self._ffmpeg_signals: _FfmpegSignals | None = None
        self._ffmpeg_job: _FfmpegDownloadJob | None = None

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        self._stack = QStackedWidget(self)
        outer.addWidget(self._stack)

        self._gate_widget = QWidget()
        gate_layout = QVBoxLayout(self._gate_widget)
        gate_layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
        gate_layout.setSpacing(14)
        gate_layout.setContentsMargins(40, 40, 40, 40)

        self._gate_icon = QLabel()
        self._gate_icon.setPixmap(FluentIcon.DOWNLOAD.icon(color=QColor(_muted())).pixmap(40, 40))
        self._gate_icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        gate_layout.addWidget(self._gate_icon)

        self._gate_title = StrongBodyLabel(tr('yt.ffmpeg_required_title'))
        self._gate_title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        gate_layout.addWidget(self._gate_title)

        self._gate_msg = BodyLabel(tr('yt.ffmpeg_required_body'))
        self._gate_msg.setWordWrap(True)
        self._gate_msg.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._gate_msg.setStyleSheet(f'color: {_muted()}; max-width: 360px;')
        gate_layout.addWidget(self._gate_msg)

        gate_btn_row = QHBoxLayout()
        gate_btn_row.setSpacing(10)
        self._gate_no_btn = PushButton(tr('yt.ffmpeg_no'))
        self._gate_no_btn.clicked.connect(self._on_ffmpeg_decline)
        gate_btn_row.addWidget(self._gate_no_btn)
        self._gate_yes_btn = PrimaryPushButton(tr('yt.ffmpeg_yes'))
        self._gate_yes_btn.clicked.connect(self._on_ffmpeg_accept)
        gate_btn_row.addWidget(self._gate_yes_btn)
        gate_layout.addLayout(gate_btn_row)

        self._gate_progress = ProgressBar()
        self._gate_progress.setFixedWidth(280)
        self._gate_progress.setVisible(False)
        gate_layout.addWidget(self._gate_progress, 0, Qt.AlignmentFlag.AlignCenter)

        self._gate_progress_lbl = CaptionLabel('')
        self._gate_progress_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._gate_progress_lbl.setStyleSheet(f'color: {_muted()};')
        self._gate_progress_lbl.setVisible(False)
        gate_layout.addWidget(self._gate_progress_lbl)

        self._stack.addWidget(self._gate_widget)

        self._content_widget = QWidget()
        content_outer = QVBoxLayout(self._content_widget)
        content_outer.setContentsMargins(0, 0, 0, 0)
        content_outer.setSpacing(0)

        scroll = SmoothScrollArea(self._content_widget)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(scroll.Shape.NoFrame)
        scroll.setStyleSheet('background: transparent; border: none;')
        content_outer.addWidget(scroll)

        self._stack.addWidget(self._content_widget)

        container = QWidget()
        container.setStyleSheet('background: transparent;')
        scroll.setWidget(container)
        layout = QVBoxLayout(container)
        layout.setContentsMargins(24, 24, 28, 24)
        layout.setSpacing(6)

        input_row = QHBoxLayout()
        input_row.setSpacing(10)
        self._url_input = LineEdit()
        self._url_input.setPlaceholderText(tr('yt.url_placeholder'))
        self._url_input.setClearButtonEnabled(True)
        self._url_input.returnPressed.connect(self._on_fetch_clicked)
        input_row.addWidget(self._url_input, 1)
        self._fetch_btn = PrimaryPushButton(tr('yt.fetch'))
        self._fetch_btn.setFixedHeight(34)
        self._fetch_btn.clicked.connect(self._on_fetch_clicked)
        input_row.addWidget(self._fetch_btn, 0)
        layout.addLayout(input_row)
        layout.addSpacing(18)

        self._empty_card = CardWidget()
        self._empty_card.setFixedHeight(160)
        empty_layout = QVBoxLayout(self._empty_card)
        empty_layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
        empty_icon = QLabel()
        empty_icon.setPixmap(FluentIcon.LINK.icon(color=QColor(_muted())).pixmap(36, 36))
        empty_icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        empty_layout.addWidget(empty_icon)
        self._empty_lbl = BodyLabel(tr('yt.empty_state'))
        self._empty_lbl.setStyleSheet(f'color: {_muted()};')
        self._empty_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        empty_layout.addWidget(self._empty_lbl)
        layout.addWidget(self._empty_card)

        self._loading_card = CardWidget()
        self._loading_card.setFixedHeight(160)
        self._loading_card.setVisible(False)
        loading_layout = QVBoxLayout(self._loading_card)
        loading_layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
        loading_layout.setSpacing(10)
        self._spinner = IndeterminateProgressRing()
        self._spinner.setFixedSize(32, 32)
        loading_layout.addWidget(self._spinner, 0, Qt.AlignmentFlag.AlignCenter)
        self._loading_lbl = CaptionLabel(tr('yt.fetching'))
        self._loading_lbl.setStyleSheet(f'color: {_muted()};')
        self._loading_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        loading_layout.addWidget(self._loading_lbl)
        layout.addWidget(self._loading_card)

        self._error_card = CardWidget()
        self._error_card.setFixedHeight(120)
        self._error_card.setVisible(False)
        error_layout = QVBoxLayout(self._error_card)
        error_layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._error_lbl = BodyLabel('')
        self._error_lbl.setStyleSheet(f'color: {palette()["state_error"]};')
        self._error_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._error_lbl.setWordWrap(True)
        error_layout.addWidget(self._error_lbl)
        layout.addWidget(self._error_card)

        self._info_card = CardWidget()
        self._info_card.setVisible(False)
        info_outer = QVBoxLayout(self._info_card)
        info_outer.setContentsMargins(16, 16, 16, 16)
        info_outer.setSpacing(14)

        info_top = QHBoxLayout()
        info_top.setSpacing(16)
        self._thumb_lbl = ImageLabel()
        self._thumb_lbl.setBorderRadius(8, 8, 8, 8)
        self._thumb_lbl.setFixedSize(_THUMB_W, _THUMB_H)
        self._thumb_lbl.setStyleSheet(f'background-color: {_card_bg()};')
        info_top.addWidget(self._thumb_lbl, 0, Qt.AlignmentFlag.AlignTop)

        meta_col = QVBoxLayout()
        meta_col.setSpacing(6)
        self._video_title_lbl = StrongBodyLabel('')
        self._video_title_lbl.setWordWrap(True)
        self._video_title_lbl.setStyleSheet(f'font-size: 16px; color: {_primary_text()};')
        meta_col.addWidget(self._video_title_lbl)
        self._video_channel_lbl = BodyLabel('')
        self._video_channel_lbl.setStyleSheet(f'color: {_muted()};')
        meta_col.addWidget(self._video_channel_lbl)
        self._video_duration_lbl = CaptionLabel('')
        self._video_duration_lbl.setStyleSheet(f'color: {_muted()};')
        meta_col.addWidget(self._video_duration_lbl)
        meta_col.addStretch(1)
        info_top.addLayout(meta_col, 1)
        info_outer.addLayout(info_top)

        mode_row = QHBoxLayout()
        mode_row.setSpacing(10)
        self._mode_selector = SegmentedWidget()
        self._mode_selector.addItem('video', tr('yt.mode_video'), lambda: self._on_mode_changed('video'))
        self._mode_selector.addItem('audio', tr('yt.mode_audio'), lambda: self._on_mode_changed('audio'))
        self._mode_selector.setCurrentItem('video')
        mode_row.addWidget(self._mode_selector, 0)
        mode_row.addStretch(1)
        quality_lbl = CaptionLabel(tr('yt.quality'))
        quality_lbl.setStyleSheet(f'color: {_muted()};')
        mode_row.addWidget(quality_lbl, 0, Qt.AlignmentFlag.AlignVCenter)
        self._quality_combo = ComboBox()
        self._quality_combo.setMinimumWidth(140)
        mode_row.addWidget(self._quality_combo, 0)
        info_outer.addLayout(mode_row)

        action_row = QHBoxLayout()
        action_row.addStretch(1)
        self._download_btn = PrimaryPushButton(FluentIcon.PLAY, tr('yt.download'))
        self._download_btn.setFixedHeight(34)
        self._download_btn.clicked.connect(self._on_download_clicked)
        action_row.addWidget(self._download_btn)
        info_outer.addLayout(action_row)

        layout.addWidget(self._info_card)
        layout.addStretch(1)

        register_locale_refresh(self, self._apply_locale)

        if yt_provider.has_ffmpeg():
            self._stack.setCurrentWidget(self._content_widget)
        else:
            self._stack.setCurrentWidget(self._gate_widget)

    def set_download_bridge(self, bridge) -> None:
        self._bridge = bridge

    def shutdown(self) -> None:
        if self._fetch_task_id is not None:
            from src.core.worker import cancel as cancel_task
            cancel_task(self._fetch_task_id)
            self._fetch_task_id = None
        if self._ffmpeg_job is not None:
            self._ffmpeg_job.cancel()
            self._ffmpeg_job = None

    def _on_ffmpeg_decline(self) -> None:
        self.ffmpeg_declined.emit()

    def _on_ffmpeg_accept(self) -> None:
        self._gate_title.setText(tr('yt.ffmpeg_downloading_title'))
        self._gate_msg.setVisible(False)
        self._gate_no_btn.setVisible(False)
        self._gate_yes_btn.setVisible(False)
        self._gate_progress.setVisible(True)
        self._gate_progress.setValue(0)
        self._gate_progress_lbl.setVisible(True)
        self._gate_progress_lbl.setText(tr('yt.ffmpeg_downloading_starting'))

        self._ffmpeg_signals = _FfmpegSignals()
        self._ffmpeg_signals.progress.connect(self._on_ffmpeg_progress)
        self._ffmpeg_signals.finished.connect(self._on_ffmpeg_download_done)
        self._ffmpeg_signals.failed.connect(self._on_ffmpeg_download_error)
        self._ffmpeg_job = _FfmpegDownloadJob(self._ffmpeg_signals)
        QThreadPool.globalInstance().start(self._ffmpeg_job)

    def _on_ffmpeg_progress(self, downloaded: int, total: int) -> None:
        if total > 0:
            pct = max(0, min(100, int(downloaded * 100 / total)))
            self._gate_progress.setValue(pct)
            self._gate_progress_lbl.setText(f"{_fmt_mb(downloaded)} / {_fmt_mb(total)} MB")
        else:
            self._gate_progress.setValue(0)
            self._gate_progress_lbl.setText(f"{_fmt_mb(downloaded)} MB")

    def _on_ffmpeg_download_done(self) -> None:
        self._ffmpeg_job = None
        self._stack.setCurrentWidget(self._content_widget)

    def _on_ffmpeg_download_error(self, error: str) -> None:
        self._ffmpeg_job = None
        self._gate_title.setText(tr('yt.ffmpeg_download_failed_title'))
        self._gate_msg.setText(tr('yt.ffmpeg_download_failed_body', message=error))
        self._gate_msg.setVisible(True)
        self._gate_progress.setVisible(False)
        self._gate_progress_lbl.setVisible(False)
        self._gate_no_btn.setText(tr('yt.ffmpeg_no'))
        self._gate_no_btn.setVisible(True)
        self._gate_yes_btn.setText(tr('yt.ffmpeg_retry'))
        self._gate_yes_btn.setVisible(True)

    def _apply_locale(self, *_args) -> None:
        self._url_input.setPlaceholderText(tr('yt.url_placeholder'))
        self._fetch_btn.setText(tr('yt.fetch'))
        self._empty_lbl.setText(tr('yt.empty_state'))
        self._loading_lbl.setText(tr('yt.fetching'))
        self._download_btn.setText(tr('yt.download'))
        self._mode_selector.setItemText('video', tr('yt.mode_video'))
        self._mode_selector.setItemText('audio', tr('yt.mode_audio'))

    def _set_state(self, state: str) -> None:
        self._empty_card.setVisible(state == 'empty')
        self._loading_card.setVisible(state == 'loading')
        self._error_card.setVisible(state == 'error')
        self._info_card.setVisible(state == 'info')

    def _on_fetch_clicked(self) -> None:
        url = self._url_input.text().strip()
        if not url:
            return
        if not yt_provider.is_valid_url(url):
            InfoBar.warning(tr('yt.invalid_url'), '', duration=2500, position=InfoBarPosition.TOP, parent=self)
            return
        self._fetch_btn.setEnabled(False)
        self._set_state('loading')
        self._fetch_task_id = submit(
            yt_provider.fetch_info,
            args=(url,),
            on_done=self._on_fetch_done,
            on_error=self._on_fetch_error,
        )

    def _on_fetch_done(self, info: dict) -> None:
        self._fetch_task_id = None
        self._fetch_btn.setEnabled(True)
        self._current_info = info
        self._video_title_lbl.setText(info.get('title', ''))
        self._video_channel_lbl.setText(info.get('uploader', ''))
        self._video_duration_lbl.setText(info.get('duration_label', ''))
        thumb_url = info.get('thumbnail')
        if thumb_url:
            artwork.thumb_ready.connect(self._on_thumb_ready)
            artwork.request('yt', thumb_url, want_full=True)
        self._mode_selector.setCurrentItem('video')
        self._populate_quality('video')
        self._set_state('info')

    def _on_thumb_ready(self, kind: str, url: str, path: str) -> None:
        if kind != 'yt' or self._current_info is None or url != self._current_info.get('thumbnail'):
            return
        self._thumb_lbl.setImage(path)
        self._thumb_lbl.setFixedSize(_THUMB_W, _THUMB_H)
        try:
            artwork.thumb_ready.disconnect(self._on_thumb_ready)
        except TypeError:
            pass

    def _on_fetch_error(self, error: str) -> None:
        self._fetch_task_id = None
        self._fetch_btn.setEnabled(True)
        self._error_lbl.setText(tr('yt.fetch_failed', message=error))
        self._set_state('error')

    def _on_mode_changed(self, mode: str) -> None:
        self._populate_quality(mode)

    def _populate_quality(self, mode: str) -> None:
        self._quality_combo.blockSignals(True)
        self._quality_combo.clear()
        if self._current_info is None:
            self._quality_combo.blockSignals(False)
            return
        items = self._current_info.get('video_formats' if mode == 'video' else 'audio_formats') or []
        if not items:
            self._quality_combo.addItem(tr('yt.quality_auto'), userData='')
        else:
            for item in items:
                label = item['label']
                if item.get('size_label'):
                    label = f"{label} · {item['size_label']}"
                self._quality_combo.addItem(label, userData=item['format_id'])
        self._quality_combo.setCurrentIndex(0)
        self._quality_combo.blockSignals(False)

    def _on_download_clicked(self) -> None:
        if self._current_info is None:
            return
        if self._bridge is None:
            InfoBar.error(tr('yt.download_failed', message='Download system not ready'), '', duration=3000, position=InfoBarPosition.TOP, parent=self)
            return
        mode = self._mode_selector.currentRouteKey()
        format_id = self._quality_combo.currentData() or ''
        title = self._current_info.get('title', 'video')
        url = self._current_info.get('webpage_url') or self._current_info.get('url', '')
        self._bridge.download(url, mode, format_id, title)
        InfoBar.success(tr('yt.download_started'), '', duration=2500, position=InfoBarPosition.TOP, parent=self)
