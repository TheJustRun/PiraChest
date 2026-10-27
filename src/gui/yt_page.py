from __future__ import annotations

import logging
from functools import partial

from PySide6.QtCore import Qt, QObject, QRunnable, QThreadPool, Signal
from PySide6.QtWidgets import QHBoxLayout, QLabel, QListWidget, QListWidgetItem, QStackedWidget, QVBoxLayout, QWidget
from qfluentwidgets import (
    BodyLabel, CaptionLabel, CardWidget, CheckBox, ComboBox, FluentIcon, ImageLabel, IndeterminateProgressRing, InfoBar,
    InfoBarPosition, LineEdit, PrimaryPushButton, ProgressBar, PushButton, SegmentedWidget, SmoothScrollArea, StrongBodyLabel,
)

from src.core.artwork import images
from src.core.theme import palette, themed_icon
from src.core.translations import register_locale_refresh, tr
from src.core.worker import cancel as cancel_task, submit
from src.core.yt import provider

logger = logging.getLogger(__name__)

_THUMB = (320, 180)


_HEIGHTS = (0, 1080, 720, 480, 360, 240, 144)


def _mb(n: int) -> str:
    return f"{n / 1048576:.1f}"


class _ToolSignals(QObject):
    progress = Signal(str, int, int)
    finished = Signal()
    failed = Signal(str)


class _ToolJob(QRunnable):
    def __init__(self, signals: _ToolSignals):
        super().__init__()
        self.cancelled = False
        self._signals = signals

    def run(self) -> None:
        try:
            provider.download_yt_tools(self._signals.progress.emit, lambda: self.cancelled)
        except Exception as exc:
            if not self.cancelled:
                if not isinstance(exc, provider.YtError):
                    logger.exception("yt tools install failed")
                self._signals.failed.emit(str(exc))
            return
        if not self.cancelled:
            self._signals.finished.emit()


class YtPage(QWidget):
    ffmpeg_declined = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._bridge = None
        self._info: dict | None = None
        self._playlist: dict | None = None
        self._fetch_id = None
        self._tool_job: _ToolJob | None = None
        self._tool_signals = _ToolSignals(self)
        self._tool_signals.progress.connect(self._on_tool_progress)
        self._tool_signals.finished.connect(self._show_content)
        self._tool_signals.failed.connect(self._on_tool_failed)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        self._stack = QStackedWidget(self)
        outer.addWidget(self._stack)
        self._gate = self._build_gate()
        self._content = self._build_content()
        self._stack.addWidget(self._gate)
        self._stack.addWidget(self._content)
        self._stack.setCurrentWidget(self._content if provider.has_yt_tools() else self._gate)
        register_locale_refresh(self, self._apply_locale)

    def _muted(self) -> str:
        return f"color: {palette()['muted']};"

    def _build_gate(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lay.setSpacing(14)
        icon = QLabel()
        icon.setPixmap(themed_icon("yt.ffmpeg_gate", FluentIcon.DOWNLOAD, size=40, color=palette()["muted"]).pixmap(40, 40))
        icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._gate_title = StrongBodyLabel(tr("yt.ffmpeg_required_title"))
        self._gate_title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._gate_msg = BodyLabel(tr("yt.ffmpeg_required_body"))
        self._gate_msg.setWordWrap(True)
        self._gate_msg.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._gate_msg.setStyleSheet(self._muted() + "max-width: 360px;")
        self._gate_no = PushButton(tr("yt.ffmpeg_no"))
        self._gate_no.clicked.connect(self.ffmpeg_declined)
        self._gate_yes = PrimaryPushButton(tr("yt.ffmpeg_yes"))
        self._gate_yes.clicked.connect(self._install_tools)
        buttons = QHBoxLayout()
        buttons.setSpacing(10)
        buttons.addWidget(self._gate_no)
        buttons.addWidget(self._gate_yes)
        self._gate_bar = ProgressBar()
        self._gate_bar.setFixedWidth(280)
        self._gate_lbl = CaptionLabel("")
        self._gate_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._gate_lbl.setStyleSheet(self._muted())
        for progress_widget in (self._gate_bar, self._gate_lbl):
            progress_widget.setVisible(False)
        for widget in (icon, self._gate_title, self._gate_msg):
            lay.addWidget(widget)
        lay.addLayout(buttons)
        lay.addWidget(self._gate_bar, 0, Qt.AlignmentFlag.AlignCenter)
        lay.addWidget(self._gate_lbl)
        return w

    def _build_content(self) -> QWidget:
        scroll = SmoothScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(scroll.Shape.NoFrame)
        scroll.setStyleSheet("background: transparent; border: none;")
        body = QWidget()
        body.setStyleSheet("background: transparent;")
        scroll.setWidget(body)
        lay = QVBoxLayout(body)
        lay.setContentsMargins(24, 24, 28, 24)
        lay.setSpacing(18)

        row = QHBoxLayout()
        row.setSpacing(10)
        self._url = LineEdit()
        self._url.setPlaceholderText(tr("yt.url_placeholder"))
        self._url.setClearButtonEnabled(True)
        self._url.returnPressed.connect(self._fetch)
        self._fetch_btn = PrimaryPushButton(tr("yt.fetch"))
        self._fetch_btn.setFixedHeight(34)
        self._fetch_btn.clicked.connect(self._fetch)
        row.addWidget(self._url, 1)
        row.addWidget(self._fetch_btn)
        lay.addLayout(row)

        self._empty_lbl = BodyLabel(tr("yt.empty_state"))
        self._empty_lbl.setStyleSheet(self._muted())
        self._empty = self._status_card(160, themed_icon("yt.empty_state", FluentIcon.LINK, size=36, color=palette()["muted"]).pixmap(36, 36), self._empty_lbl)
        self._spinner = IndeterminateProgressRing()
        self._spinner.setFixedSize(32, 32)
        self._loading_lbl = CaptionLabel(tr("yt.fetching"))
        self._loading_lbl.setStyleSheet(self._muted())
        self._loading = self._status_card(160, None, self._loading_lbl, self._spinner)
        self._error_lbl = BodyLabel("")
        self._error_lbl.setStyleSheet(f"color: {palette()['state_error']};")
        self._error_lbl.setWordWrap(True)
        self._error = self._status_card(120, None, self._error_lbl)
        for card in (self._empty, self._loading, self._error):
            lay.addWidget(card)

        self._card = CardWidget()
        card = QVBoxLayout(self._card)
        card.setContentsMargins(16, 16, 16, 16)
        card.setSpacing(14)
        top = QHBoxLayout()
        top.setSpacing(16)
        self._thumb = ImageLabel()
        self._thumb.setBorderRadius(8, 8, 8, 8)
        self._thumb.setFixedSize(*_THUMB)
        self._thumb.setStyleSheet(f"background-color: {palette()['card_bg']};")
        meta = QVBoxLayout()
        meta.setSpacing(6)
        self._title = StrongBodyLabel("")
        self._title.setWordWrap(True)
        self._title.setStyleSheet(f"font-size: 16px; color: {palette()['primary_text']};")
        self._channel = BodyLabel("")
        self._channel.setStyleSheet(self._muted())
        self._duration = CaptionLabel("")
        self._duration.setStyleSheet(self._muted())
        for widget in (self._title, self._channel, self._duration):
            meta.addWidget(widget)
        meta.addStretch(1)
        top.addWidget(self._thumb, 0, Qt.AlignmentFlag.AlignTop)
        top.addLayout(meta, 1)
        card.addLayout(top)

        options = QHBoxLayout()
        options.setSpacing(10)
        self._mode = SegmentedWidget()
        self._mode.addItem("video", tr("yt.mode_video"), lambda: self._fill_quality("video"))
        self._mode.addItem("audio", tr("yt.mode_audio"), lambda: self._fill_quality("audio"))
        self._mode.setCurrentItem("video")
        self._quality_lbl = CaptionLabel(tr("yt.quality"))
        self._quality_lbl.setStyleSheet(self._muted())
        self._quality = ComboBox()
        self._quality.setMinimumWidth(200)
        options.addWidget(self._mode)
        options.addStretch(1)
        options.addWidget(self._quality_lbl)
        options.addWidget(self._quality)
        card.addLayout(options)

        extras = QHBoxLayout()
        extras.setSpacing(10)
        self._sub_lbl = CaptionLabel(tr("yt.subtitles"))
        self._sub_lbl.setStyleSheet(self._muted())
        self._subs = ComboBox()
        self._subs.setMinimumWidth(200)
        self._clip = CheckBox(tr("yt.trim"))
        self._clip_start = LineEdit()
        self._clip_start.setPlaceholderText("0:00")
        self._clip_end = LineEdit()
        self._clip_end.setPlaceholderText(tr("yt.trim_end"))
        for edit in (self._clip_start, self._clip_end):
            edit.setFixedWidth(120 if edit is self._clip_start else 170)
            edit.setEnabled(False)
        self._clip.toggled.connect(lambda on: [e.setEnabled(on) for e in (self._clip_start, self._clip_end)])
        extras.addWidget(self._sub_lbl)
        extras.addWidget(self._subs)
        extras.addStretch(1)
        extras.addWidget(self._clip)
        extras.addWidget(self._clip_start)
        extras.addWidget(self._clip_end)
        card.addLayout(extras)

        actions = QHBoxLayout()
        actions.addStretch(1)
        self._download_btn = PrimaryPushButton(themed_icon("yt.download", FluentIcon.PLAY), tr("yt.download"))
        self._download_btn.setFixedHeight(34)
        self._download_btn.clicked.connect(self._download)
        actions.addWidget(self._download_btn)
        card.addLayout(actions)
        lay.addWidget(self._card)

        self._plist_card = CardWidget()
        plist = QVBoxLayout(self._plist_card)
        plist.setContentsMargins(16, 16, 16, 16)
        plist.setSpacing(12)
        self._plist_title = StrongBodyLabel("")
        self._plist_title.setStyleSheet(f"font-size: 16px; color: {palette()['primary_text']};")
        self._plist_count = CaptionLabel("")
        self._plist_count.setStyleSheet(self._muted())
        self._plist_list = QListWidget()
        self._plist_list.setFixedHeight(280)
        self._plist_list.setStyleSheet("QListWidget { background: transparent; border: none; }")
        self._plist_list.itemChanged.connect(self._update_plist_count)
        controls = QHBoxLayout()
        controls.setSpacing(10)
        toggle = PushButton(tr("yt.select_all"))
        toggle.clicked.connect(self._toggle_all)
        self._plist_quality = ComboBox()
        for height in _HEIGHTS:
            self._plist_quality.addItem(tr("yt.quality_best") if not height else tr("yt.quality_max", height=height), userData=height)
        self._plist_quality.addItem(tr("yt.mode_audio"), userData=-1)
        self._plist_btn = PrimaryPushButton(themed_icon("yt.download", FluentIcon.PLAY), tr("yt.download"))
        self._plist_btn.clicked.connect(self._download_playlist)
        controls.addWidget(toggle)
        controls.addStretch(1)
        controls.addWidget(self._plist_quality)
        controls.addWidget(self._plist_btn)
        for widget in (self._plist_title, self._plist_count, self._plist_list):
            plist.addWidget(widget)
        plist.addLayout(controls)
        lay.addWidget(self._plist_card)
        lay.addStretch(1)
        self._set_view("empty")
        return scroll

    @staticmethod
    def _status_card(height: int, pixmap, label, spinner=None) -> CardWidget:
        card = CardWidget()
        card.setFixedHeight(height)
        lay = QVBoxLayout(card)
        lay.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lay.setSpacing(10)
        if pixmap is not None:
            icon = QLabel()
            icon.setPixmap(pixmap)
            icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
            lay.addWidget(icon)
        if spinner is not None:
            lay.addWidget(spinner, 0, Qt.AlignmentFlag.AlignCenter)
        label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lay.addWidget(label)
        return card

    def set_download_bridge(self, bridge) -> None:
        self._bridge = bridge

    def shutdown(self) -> None:
        if self._fetch_id is not None:
            cancel_task(self._fetch_id)
            self._fetch_id = None
        if self._tool_job is not None:
            self._tool_job.cancelled = True
            self._tool_job = None

    def _apply_locale(self, *_args) -> None:
        self._url.setPlaceholderText(tr("yt.url_placeholder"))
        self._fetch_btn.setText(tr("yt.fetch"))
        self._empty_lbl.setText(tr("yt.empty_state"))
        self._loading_lbl.setText(tr("yt.fetching"))
        self._quality_lbl.setText(tr("yt.quality"))
        self._download_btn.setText(tr("yt.download"))
        self._mode.setItemText("video", tr("yt.mode_video"))
        self._mode.setItemText("audio", tr("yt.mode_audio"))

    def _set_view(self, view: str) -> None:
        for name, widget in (("empty", self._empty), ("loading", self._loading), ("error", self._error), ("info", self._card), ("playlist", self._plist_card)):
            widget.setVisible(name == view)

    def _show_content(self) -> None:
        self._tool_job = None
        self._stack.setCurrentWidget(self._content)

    def _install_tools(self) -> None:
        self._gate_title.setText(tr("yt.ffmpeg_downloading_title"))
        for widget in (self._gate_msg, self._gate_no, self._gate_yes):
            widget.setVisible(False)
        for widget in (self._gate_bar, self._gate_lbl):
            widget.setVisible(True)
        self._gate_bar.setValue(0)
        self._gate_lbl.setText(tr("yt.ffmpeg_downloading_starting"))
        self._tool_job = _ToolJob(self._tool_signals)
        QThreadPool.globalInstance().start(self._tool_job)

    def _on_tool_progress(self, stage: str, done: int, total: int) -> None:
        self._gate_bar.setValue(int(done * 100 / total) if total else 0)
        self._gate_lbl.setText(f"{stage}: {_mb(done)} / {_mb(total)} MB" if total else f"{stage}: {_mb(done)} MB")

    def _on_tool_failed(self, error: str) -> None:
        self._tool_job = None
        self._gate_title.setText(tr("yt.ffmpeg_download_failed_title"))
        self._gate_msg.setText(tr("yt.ffmpeg_download_failed_body", message=error))
        self._gate_yes.setText(tr("yt.ffmpeg_retry"))
        for widget in (self._gate_bar, self._gate_lbl):
            widget.setVisible(False)
        for widget in (self._gate_msg, self._gate_no, self._gate_yes):
            widget.setVisible(True)

    def _fetch(self) -> None:
        url = self._url.text().strip()
        if not url:
            return
        if not provider.is_valid_url(url):
            InfoBar.warning(tr("yt.invalid_url"), "", duration=2500, position=InfoBarPosition.TOP, parent=self)
            return
        if self._fetch_id is not None:
            cancel_task(self._fetch_id)
        self._fetch_btn.setEnabled(False)
        self._set_view("loading")
        if provider.is_playlist_url(url):
            self._fetch_id = submit(provider.fetch_playlist, args=(url,), on_done=self._on_playlist, on_error=self._on_fetch_error)
        else:
            self._fetch_id = submit(provider.fetch_info, args=(url,), on_done=self._on_info, on_error=self._on_fetch_error)

    def _on_playlist(self, playlist: dict) -> None:
        self._fetch_id = None
        self._fetch_btn.setEnabled(True)
        self._playlist = playlist
        self._plist_title.setText(playlist["title"])
        self._plist_list.blockSignals(True)
        self._plist_list.clear()
        for entry in playlist["entries"]:
            item = QListWidgetItem(f"{entry['title']}   {entry['duration']}".rstrip())
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(Qt.CheckState.Checked)
            self._plist_list.addItem(item)
        self._plist_list.blockSignals(False)
        self._update_plist_count()
        self._set_view("playlist")

    def _checked_rows(self) -> list[int]:
        return [i for i in range(self._plist_list.count()) if self._plist_list.item(i).checkState() == Qt.CheckState.Checked]

    def _update_plist_count(self, *_args) -> None:
        total = self._plist_list.count()
        self._plist_count.setText(tr("yt.playlist_selected", selected=len(self._checked_rows()), total=total))

    def _toggle_all(self) -> None:
        state = Qt.CheckState.Unchecked if len(self._checked_rows()) == self._plist_list.count() else Qt.CheckState.Checked
        self._plist_list.blockSignals(True)
        for i in range(self._plist_list.count()):
            self._plist_list.item(i).setCheckState(state)
        self._plist_list.blockSignals(False)
        self._update_plist_count()

    def _download_playlist(self) -> None:
        if self._playlist is None or self._bridge is None:
            return
        rows = self._checked_rows()
        if not rows:
            return
        height = self._plist_quality.currentData()
        for i in rows:
            entry = self._playlist["entries"][i]
            self._bridge.download({"url": entry["url"], "title": entry["title"]}, "audio" if height == -1 else "video", max_height=max(height, 0), subdir=self._playlist["title"])
        InfoBar.success(tr("yt.download_started"), f"{len(rows)}", duration=2500, position=InfoBarPosition.TOP, parent=self)

    def _on_info(self, info: dict) -> None:
        self._fetch_id = None
        self._fetch_btn.setEnabled(True)
        self._info = info
        self._title.setText(info["title"])
        self._channel.setText(info["uploader"])
        self._duration.setText(info["duration_label"])
        if info["thumbnail"]:
            images.get(info["thumbnail"], _THUMB, partial(self._on_thumb, info["thumbnail"]), owner=self)
        self._mode.setCurrentItem("video")
        self._fill_quality("video")
        self._subs.clear()
        self._subs.addItem(tr("yt.subtitles_none"), userData="")
        for lang, sub in sorted(provider.manual_subtitles(info).items()):
            self._subs.addItem(f"{lang} - {sub.get('name') or lang}", userData=lang)
        self._subs.setCurrentIndex(0)
        self._clip.setChecked(False)
        self._clip_start.clear()
        self._clip_end.clear()
        self._set_view("info")

    def _on_thumb(self, url: str, pixmap) -> None:
        if pixmap.isNull() or self._info is None or self._info["thumbnail"] != url:
            return
        self._thumb.setImage(pixmap)
        self._thumb.setFixedSize(*_THUMB)

    def _on_fetch_error(self, error: str) -> None:
        self._fetch_id = None
        self._fetch_btn.setEnabled(True)
        self._error_lbl.setText(tr("yt.fetch_failed", message=error))
        self._set_view("error")

    def _fill_quality(self, mode: str) -> None:
        self._quality.clear()
        if self._info is None:
            return
        tiers = self._info["video_tiers" if mode == "video" else "audio_tiers"]
        if not tiers:
            self._quality.addItem(tr("yt.quality_auto"), userData="")
        for tier in tiers:
            self._quality.addItem(tier["label"], userData=tier["id"])
        self._quality.setCurrentIndex(0)

    def _download(self) -> None:
        if self._info is None:
            return
        if self._bridge is None:
            InfoBar.error(tr("yt.download_failed", message=tr("yt.bridge_missing")), "", duration=3000, position=InfoBarPosition.TOP, parent=self)
            return
        clip = None
        if self._clip.isChecked():
            try:
                start = provider.parse_time(self._clip_start.text() or "0")
                end = provider.parse_time(self._clip_end.text()) if self._clip_end.text().strip() else None
            except provider.YtError as exc:
                InfoBar.warning(str(exc), "", duration=3000, position=InfoBarPosition.TOP, parent=self)
                return
            if end is not None and end <= start:
                InfoBar.warning(tr("yt.trim_invalid"), "", duration=3000, position=InfoBarPosition.TOP, parent=self)
                return
            clip = (start, end)
        self._bridge.download(self._info, self._mode.currentRouteKey(), self._quality.currentData() or "", subtitle=self._subs.currentData() or "", clip=clip)
        InfoBar.success(tr("yt.download_started"), "", duration=2500, position=InfoBarPosition.TOP, parent=self)
