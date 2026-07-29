from __future__ import annotations

import logging

from PyQt6.QtWidgets import (
    QLabel,
    QScrollArea,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from qfluentwidgets import qconfig

from ..core.theme import palette
from ..core.translations import tr, register_locale_refresh

logger = logging.getLogger(__name__)


class ROMDetailsPanel(QScrollArea):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setWidgetResizable(True)
        self.setFrameShape(QScrollArea.FrameShape.NoFrame)
        self.setMinimumWidth(320)
        self.setMaximumWidth(450)

        self._title_label = QLabel(tr("rom_details.placeholder"))
        self._title_label.setWordWrap(True)
        self._console_label = QLabel("")
        self._author_label = QLabel("")
        self._date_label = QLabel("")
        self._size_label = QLabel("")
        self._source_label = QLabel("")
        self._region_label = QLabel("")
        self._lang_label = QLabel("")
        self._desc_title = QLabel(tr("rom_details.description_title"))
        self._description = QTextEdit()
        self._description.setReadOnly(True)
        self._description.setMaximumHeight(200)
        self._description.setPlaceholderText(tr("rom_details.no_description"))

        self._current_rom: dict | None = None

        self._meta_labels = (
            self._console_label,
            self._author_label,
            self._date_label,
            self._size_label,
            self._source_label,
            self._region_label,
            self._lang_label,
        )

        container = QWidget()
        layout = QVBoxLayout(container)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(8)
        layout.addWidget(self._title_label)
        for lbl in self._meta_labels:
            layout.addWidget(lbl)
        layout.addWidget(self._desc_title)
        layout.addWidget(self._description)
        layout.addStretch()
        self.setWidget(container)

        self._apply_theme()
        qconfig.themeChanged.connect(self._apply_theme)
        register_locale_refresh(self, self._apply_locale)

    def _apply_theme(self, *_args) -> None:
        c = palette()
        self._title_label.setStyleSheet(f"font-size: 16px; font-weight: bold; color: {c['detail_title']};")
        meta_style = f"color: {c['detail_meta']}; font-size: 11px;"
        self._console_label.setStyleSheet(f"color: {c['detail_subtitle']}; font-size: 12px;")
        self._author_label.setStyleSheet(meta_style)
        self._date_label.setStyleSheet(meta_style)
        self._size_label.setStyleSheet(meta_style + " font-weight: bold;")
        self._source_label.setStyleSheet(meta_style)
        self._region_label.setStyleSheet(meta_style)
        self._lang_label.setStyleSheet(meta_style)
        self._desc_title.setStyleSheet(f"font-size: 12px; font-weight: bold; color: {c['detail_title']}; margin-top: 12px;")
        self._description.setStyleSheet(
            f"color: {c['detail_title']}; font-size: 11px; "
            f"background-color: {c['detail_box_bg']}; "
            f"border: 1px solid {c['detail_box_border']}; "
            "border-radius: 4px; padding: 8px;"
        )

    def _apply_locale(self, *_args) -> None:
        self._desc_title.setText(tr("rom_details.description_title"))
        self._description.setPlaceholderText(tr("rom_details.no_description"))
        self.select_rom(self._current_rom)

    def select_rom(self, rom: dict) -> None:
        self._current_rom = rom
        if not rom:
            self._clear()
            return
        self._title_label.setText(rom.get("title", "—") or tr("rom_details.placeholder"))
        self._console_label.setText(tr("rom_details.console", value=rom.get("console", "—") or "—"))
        self._author_label.setText(tr("rom_details.author", value=rom.get("author", "—") or "—"))
        size_val = rom.get("file_size", "") or rom.get("file_size_bytes", "")
        self._size_label.setText(tr("rom_details.size", value=size_val) if size_val else tr("rom_details.size_unknown"))
        self._source_label.setText(tr("rom_details.source", value=rom.get("source", "—") or "—"))
        self._region_label.setText(tr("rom_details.region", value=rom.get("region", "—") or "—"))
        self._lang_label.setText(tr("rom_details.language", value=rom.get("lang", "—") or "—"))
        orig_date = rom.get("date", "") or ""
        self._date_label.setText(tr("rom_details.date", value=orig_date) if orig_date else "")
        desc = rom.get("description") or ""
        self._description.setPlainText(desc if desc and desc != "None" else tr("rom_details.no_description"))

    def _clear(self) -> None:
        self._current_rom = None
        self._title_label.setText(tr("rom_details.placeholder"))
        for lbl in self._meta_labels:
            lbl.setText("")
        self._description.setPlainText(tr("rom_details.no_description"))
