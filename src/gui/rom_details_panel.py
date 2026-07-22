from __future__ import annotations

import logging

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QLabel,
    QScrollArea,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from qfluentwidgets import qconfig

from ..core.theme import palette

logger = logging.getLogger(__name__)

class ROMDetailsPanel(QScrollArea):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setWidgetResizable(True)
        self.setFrameShape(QScrollArea.FrameShape.NoFrame)
        self.setMinimumWidth(320)
        self.setMaximumWidth(450)

        self._title_label = QLabel("—")
        self._title_label.setWordWrap(True)

        self._console_label = QLabel("")
        self._author_label = QLabel("")
        self._date_label = QLabel("")
        self._size_label = QLabel("")
        self._source_label = QLabel("")
        self._region_label = QLabel("")
        self._lang_label = QLabel("")

        self._desc_title = QLabel("Description")

        self._description = QTextEdit()
        self._description.setReadOnly(True)
        self._description.setMaximumHeight(200)
        self._description.setPlaceholderText("No description available.")

        container = QWidget()
        layout = QVBoxLayout(container)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(8)

        layout.addWidget(self._title_label)
        layout.addWidget(self._console_label)
        layout.addWidget(self._author_label)
        layout.addWidget(self._date_label)
        layout.addWidget(self._size_label)
        layout.addWidget(self._source_label)
        layout.addWidget(self._region_label)
        layout.addWidget(self._lang_label)
        layout.addWidget(self._desc_title)
        layout.addWidget(self._description)
        layout.addStretch()

        self.setWidget(container)

        self._apply_theme()
        qconfig.themeChanged.connect(lambda *_: self._apply_theme())

    def _apply_theme(self) -> None:
        c = palette()

        self._title_label.setStyleSheet(
            f"font-size: 16px; font-weight: bold; color: {c['detail_title']};"
        )
        self._console_label.setStyleSheet(
            f"color: {c['detail_subtitle']}; font-size: 12px;"
        )
        self._author_label.setStyleSheet(
            f"color: {c['detail_meta']}; font-size: 11px;"
        )
        self._date_label.setStyleSheet(
            f"color: {c['detail_meta']}; font-size: 11px;"
        )
        self._size_label.setStyleSheet(
            f"color: {c['detail_meta']}; font-size: 11px; font-weight: bold;"
        )
        self._source_label.setStyleSheet(
            f"color: {c['detail_meta']}; font-size: 11px;"
        )
        self._region_label.setStyleSheet(
            f"color: {c['detail_meta']}; font-size: 11px;"
        )
        self._lang_label.setStyleSheet(
            f"color: {c['detail_meta']}; font-size: 11px;"
        )
        self._desc_title.setStyleSheet(
            f"font-size: 12px; font-weight: bold; color: {c['detail_title']}; margin-top: 12px;"
        )
        self._description.setStyleSheet(
            f"color: {c['detail_title']}; font-size: 11px; "
            f"background-color: {c['detail_box_bg']}; "
            f"border: 1px solid {c['detail_box_border']}; "
            "border-radius: 4px; padding: 8px;"
        )

    def select_rom(self, rom: dict) -> None:
        if not rom:
            self._clear()
            return

        self._title_label.setText(rom.get("title", "—") or "—")
        self._console_label.setText(f"Console: {rom.get('console', '—') or '—'}")
        self._author_label.setText(f"Author: {rom.get('author', '—') or '—'}")

        size_val = rom.get("file_size", "") or rom.get("file_size_bytes", "")
        self._size_label.setText(f"Size: {size_val}" if size_val else "Size: Unknown")

        self._source_label.setText(f"Source: {rom.get('source', '—') or '—'}")
        self._region_label.setText(f"Region: {rom.get('region', '—') or '—'}")
        self._lang_label.setText(f"Language: {rom.get('lang', '—') or '—'}")

        orig_date = rom.get("date", "") or ""
        self._date_label.setText(f"Date: {orig_date}" if orig_date else "")

        desc = rom.get("description") or ""
        self._description.setPlainText(desc if desc and desc != "None" else "No description available.")

    def _clear(self) -> None:
        self._title_label.setText("—")
        self._console_label.setText("")
        self._author_label.setText("")
        self._date_label.setText("")
        self._size_label.setText("")
        self._source_label.setText("")
        self._region_label.setText("")
        self._lang_label.setText("")
        self._description.setPlainText("No description available.")