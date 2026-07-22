from __future__ import annotations

import json
import logging
import os
from typing import Any

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QFont
from PyQt6.QtWidgets import QFileDialog, QVBoxLayout, QWidget

from qfluentwidgets import (
    CompactSpinBox,
    FluentIcon,
    InfoBar,
    InfoBarPosition,
    MessageBoxBase,
    PrimaryPushButton,
    PushButton,
    SettingCard,
    SettingCardGroup,
    SwitchSettingCard,
    LineEdit,
)

from ..core.config import Settings as _Settings, paths, settings, apply_settings

logger = logging.getLogger(__name__)

_CONFIG_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    ".config",
)
_SETTINGS_FILE = os.path.join(_CONFIG_DIR, "pirachest_settings.json")
_LEGACY_SETTINGS_FILE = os.path.join(_CONFIG_DIR, "minerva_settings.json")

def load_settings() -> _Settings:
    path = _SETTINGS_FILE if os.path.isfile(_SETTINGS_FILE) else _LEGACY_SETTINGS_FILE
    if os.path.isfile(path):
        try:
            with open(path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            return _Settings.from_dict(data)
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("Failed to load settings: %s — using defaults", exc)
    return _Settings()

def save_settings(s: _Settings) -> None:
    config_dir = os.path.dirname(_SETTINGS_FILE)
    os.makedirs(config_dir, exist_ok=True)
    data = s.to_dict()
    try:
        with open(_SETTINGS_FILE, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2)
        logger.info("Settings saved to %s", _SETTINGS_FILE)
    except OSError as exc:
        logger.error("Failed to save settings: %s", exc)

class SettingsDialog(MessageBoxBase):
    settings_changed = pyqtSignal(_Settings)

    def __init__(
        self,
        settings_obj: _Settings | None = None,
        parent=None,
    ) -> None:
        self._settings = settings_obj or load_settings()

        super().__init__(parent=parent)

        from qfluentwidgets import SubtitleLabel as _SubtitleLabel
        title_lbl = _SubtitleLabel("Settings")
        self.viewLayout.addWidget(title_lbl)
        self.setMinimumWidth(520)

        self._build_ui()
        self._populate()

    def _build_ui(self):
        dl_group = SettingCardGroup("Download Directory", self)

        self._txt_download_dir = LineEdit()
        self._txt_download_dir.setReadOnly(True)

        dl_card = SettingCard(FluentIcon.FOLDER, "Download folder",
                              "Where completed ROM downloads are saved", self)
        dl_card.hBoxLayout.addWidget(self._txt_download_dir, 0, Qt.AlignmentFlag.AlignRight)

        browse_btn = PushButton("Browse…")
        browse_btn.clicked.connect(self._browse_download_dir)
        dl_card.hBoxLayout.addWidget(browse_btn, 0, Qt.AlignmentFlag.AlignRight)

        dl_group.addSettingCard(dl_card)

        self._chk_console_structure = SwitchSettingCard(
            icon=FluentIcon.IOT,
            title="Console-based folder structure",
            content="Organize downloads as /downloads/{Console}/{Game Title}",
        )
        dl_group.addSettingCard(self._chk_console_structure)

        perf_group = SettingCardGroup("Seeding & Performance", self)

        self._spin_seed = CompactSpinBox()
        self._spin_seed.setRange(0, 9999)
        self._spin_seed.setSuffix(" min")
        seed_card = SettingCard(FluentIcon.HISTORY, "Seed Time",
                                "How long to keep seeding after a download completes", self)
        seed_card.hBoxLayout.addWidget(self._spin_seed, 0, Qt.AlignmentFlag.AlignRight)
        perf_group.addSettingCard(seed_card)

        self._spin_speed = CompactSpinBox()
        self._spin_speed.setRange(0, 100000)
        self._spin_speed.setSuffix(" KB/s")
        speed_card = SettingCard(FluentIcon.SPEED_HIGH, "Download Speed Limit",
                                 "Maximum download speed, 0 for unlimited", self)
        speed_card.hBoxLayout.addWidget(self._spin_speed, 0, Qt.AlignmentFlag.AlignRight)
        perf_group.addSettingCard(speed_card)

        self._spin_upload_speed = CompactSpinBox()
        self._spin_upload_speed.setRange(0, 100000)
        self._spin_upload_speed.setSuffix(" KB/s")
        upload_speed_card = SettingCard(
            FluentIcon.SPEED_HIGH, "Upload Speed Limit",
            "Maximum upload/seeding speed — unlimited upload can starve download speed", self,
        )
        upload_speed_card.hBoxLayout.addWidget(self._spin_upload_speed, 0, Qt.AlignmentFlag.AlignRight)
        perf_group.addSettingCard(upload_speed_card)

        self._chk_auto = SwitchSettingCard(
            icon=FluentIcon.MEDIA,
            title="Auto-download on selection",
            content="Start download immediately when a ROM is selected",
        )
        perf_group.addSettingCard(self._chk_auto)

        self._chk_delete_torrent = SwitchSettingCard(
            icon=FluentIcon.DELETE,
            title="Delete torrent after download",
            content="Remove cached .torrent file after download completes",
        )
        perf_group.addSettingCard(self._chk_delete_torrent)

        self.viewLayout.setSpacing(16)
        self.viewLayout.addWidget(dl_group)
        self.viewLayout.addWidget(perf_group)
        self.viewLayout.addStretch()

    def _populate(self):
        self._txt_download_dir.setText(self._settings.download_dir or "")
        self._chk_console_structure.setChecked(
            getattr(self._settings, "_console_structure", True)
        )
        self._spin_seed.setValue(self._settings.seed_time)
        self._spin_speed.setValue(self._settings.speed_limit)
        self._spin_upload_speed.setValue(getattr(self._settings, "upload_speed_limit", 500))
        self._chk_auto.setChecked(getattr(self._settings, "auto_download", False))
        self._chk_delete_torrent.setChecked(
            getattr(self._settings, "delete_torrent_after", True)
        )

    def _browse_download_dir(self):
        path = QFileDialog.getExistingDirectory(
            self, "Select Download Directory", self._txt_download_dir.text()
        )
        if path:
            self._txt_download_dir.setText(path)

    def _on_ok(self):
        self._settings.download_dir = self._txt_download_dir.text() or paths.download_root
        self._settings.seed_time = self._spin_seed.value()
        self._settings.speed_limit = self._spin_speed.value()
        self._settings.upload_speed_limit = self._spin_upload_speed.value()
        self._settings.delete_torrent_after = self._chk_delete_torrent.isChecked()
        self._settings._console_structure = self._chk_console_structure.isChecked()
        self._settings.auto_download = self._chk_auto.isChecked()

        apply_settings(
            download_dir=self._settings.download_dir,
            seed_time=self._settings.seed_time,
            speed_limit=self._settings.speed_limit,
            upload_speed_limit=self._settings.upload_speed_limit,
            delete_torrent_after=self._settings.delete_torrent_after,
        )

        save_settings(self._settings)

        from ..core.config import resolve_theme as _resolve_theme
        from qfluentwidgets import setTheme
        _resolve_theme(setTheme)

        self.settings_changed.emit(self._settings)
        logger.info("Settings applied and saved")