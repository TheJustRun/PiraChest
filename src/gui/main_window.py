from __future__ import annotations

import logging
import os
import sys
from typing import Optional

from PyQt6.QtCore import QMimeData, QObject, QThread, QUrl, Qt, QEvent, QTimer, pyqtSignal
from PyQt6.QtWidgets import QDialog, QVBoxLayout, QLabel as QtQLabel
from PyQt6.QtGui import QFont, QColor, QGuiApplication
from PyQt6.QtWidgets import (
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QFrame,
    QScrollArea,
    QVBoxLayout,
    QWidget,
    QSplitter,
    QSizePolicy,
)

from qfluentwidgets import (
    Action,
    BodyLabel,
    CardWidget,
    CheckBox,
    ComboBox,
    CompactSpinBox,
    FluentIcon,
    FluentWindow,
    HyperlinkButton,
    InfoBar,
    InfoBarPosition,
    IndeterminateProgressBar,
    LineEdit,
    MessageBoxBase,
    NavigationItemPosition,
    PrimaryPushButton,
    PrimaryToolButton,
    ProgressBar,
    PushButton,
    RoundMenu,
    SearchLineEdit,
    SettingCard,
    SettingCardGroup,
    StrongBodyLabel,
    SubtitleLabel,
    SwitchSettingCard,
    TitleLabel,
    setTheme,
    Theme,
    qconfig,
    ThemeColor,
    isDarkTheme,
    CaptionLabel,
)

from ..core import database as db, sync as sync_module
from ..core import console_variants
from ..core.config import settings as _global_settings, resolve_theme, ThemeMode
from ..core.theme import palette, settings_qss
from .settings_dialog import save_settings

logger = logging.getLogger(__name__)

PAGE_SIZE = 30

def _get_all_consoles() -> list[str]:
    try:
        return db.get_all_consoles()
    except Exception:
        return []

def _get_all_sources() -> list[str]:
    try:
        return db.get_all_sources()
    except Exception:
        return []

class RomCardWidget(CardWidget):
    rom_selected = pyqtSignal(dict)
    download_clicked = pyqtSignal(dict)
    selection_toggled = pyqtSignal(dict, bool)

    def __init__(self, rom: dict, parent=None):
        super().__init__(parent)
        self._rom = rom
        self.setFixedHeight(56)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(12, 4, 12, 4)
        layout.setSpacing(10)
        from qfluentwidgets import CheckBox as _CheckBox
        self._select_chk = _CheckBox()
        self._select_chk.setFixedWidth(20)
        self._select_chk.toggled.connect(lambda checked, r=rom: self.selection_toggled.emit(r, checked))
        layout.addWidget(self._select_chk)
        title_val = str(rom.get("title", "—") or "—")
        self._title_lbl = StrongBodyLabel(title_val)
        self._title_lbl.setWordWrap(False)
        self._title_lbl.setToolTip(title_val)
        layout.addWidget(self._title_lbl, stretch=1)
        console_val = rom.get("console", "")
        source_val = rom.get("source", "")
        size_val = rom.get("file_size", "")
        subtitle_parts = [str(p) for p in [console_val, source_val, size_val] if p]
        self._sub_lbl = CaptionLabel("  •  ".join(subtitle_parts) if subtitle_parts else "Unknown")
        layout.addWidget(self._sub_lbl)

        self._dl_btn = PrimaryToolButton(FluentIcon.DOWNLOAD, self)
        self._dl_btn.setFixedSize(36, 36)
        self._dl_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._dl_btn.clicked.connect(lambda _=False, r=rom: self.download_clicked.emit(r))
        layout.addWidget(self._dl_btn)

    def set_rom(self, rom: dict):
        self._rom = rom
        title_val = str(rom.get("title", "—") or "—")
        self._title_lbl.setText(title_val)
        self._title_lbl.setToolTip(title_val)
        parts = [str(p) for p in [rom.get("console", ""), rom.get("source", ""), rom.get("file_size", "")] if p]
        self._sub_lbl.setText("  •  ".join(parts) if parts else "Unknown")
        self._select_chk.blockSignals(True)
        self._select_chk.setChecked(False)
        self._select_chk.blockSignals(False)

    def is_selected(self) -> bool:
        return self._select_chk.isChecked()

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.rom_selected.emit(self._rom)
        else:
            super().mousePressEvent(event)

class DetailsPanel(QWidget):
    download_clicked = pyqtSignal(dict)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._rom: dict = {}
        self.setMaximumWidth(420)
        self.setMinimumWidth(300)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 12, 16, 12)
        layout.setSpacing(8)
        self._title_lbl = SubtitleLabel("Select a ROM")
        self._title_lbl.setWordWrap(True)
        layout.addWidget(self._title_lbl)
        self._info_lbl = CaptionLabel("")
        layout.addWidget(self._info_lbl)
        layout.addWidget(StrongBodyLabel("Description"))
        self._desc_card = CardWidget()
        _desc_layout = QVBoxLayout(self._desc_card)
        _desc_layout.setContentsMargins(10, 10, 10, 10)
        self._desc_edit = BodyLabel("No description available.")
        self._desc_edit.setWordWrap(True)
        _desc_layout.addWidget(self._desc_edit)
        self._desc_card.setMinimumHeight(80)
        self._desc_card.setMaximumHeight(200)
        layout.addWidget(self._desc_card)

        layout.addStretch()

        self._dl_btn = PrimaryPushButton(FluentIcon.DOWNLOAD, "Download ROM")
        self._dl_btn.setEnabled(False)
        self._dl_btn.clicked.connect(self._on_download)
        layout.addWidget(self._dl_btn)

    def select_rom(self, rom: dict):
        if not rom or not isinstance(rom, dict):
            logger.warning("select_rom called with invalid rom: %r", rom)
            self._rom = {}
            if hasattr(self, '_dl_btn') and self._dl_btn:
                self._dl_btn.setEnabled(False)
            return
        self._rom = rom
        try:
            if hasattr(self, '_dl_btn') and self._dl_btn:
                self._dl_btn.setEnabled(True)
            if hasattr(self, '_title_lbl') and self._title_lbl:
                self._title_lbl.setText(str(rom.get("title", "—") or "—"))
            if hasattr(self, '_info_lbl') and self._info_lbl:
                info_parts = [
                    str(p) for p in [
                        rom.get("console", ""),
                        rom.get("source", ""),
                        rom.get("file_size", ""),
                    ]
                    if p
                ]
                self._info_lbl.setText("  •  ".join(info_parts))

            desc = rom.get("description")
            if hasattr(self, '_desc_edit') and self._desc_edit:
                if desc and desc != "None":
                    self._desc_edit.setText(str(desc))
                else:
                    self._desc_edit.setText("No description available.")

        except Exception:
            logger.exception("Error selecting ROM")
            if hasattr(self, '_desc_edit') and self._desc_edit:
                self._desc_edit.setText("Error loading details.")

    def set_description(self, desc: str):
        self._desc_edit.setText(desc if desc else "No description available.")

    def _on_download(self):
        try:
            rom = getattr(self, "_rom", None)
            if not rom:
                logger.warning("Download clicked with no ROM selected")
                return
            self.download_clicked.emit(rom)
        except Exception:
            logger.exception("Download click handler error")

class GameListScrollArea:
    @staticmethod
    def configure(scroll) -> None:
        try:
            from PyQt6.QtWidgets import QAbstractScrollArea
            scroll.setViewportUpdateMode(QAbstractScrollArea.ViewportUpdateMode.FullViewportUpdate)
        except Exception:
            pass
        try:
            scroll.viewport().setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent, False)
            scroll.viewport().setAutoFillBackground(False)
        except Exception:
            pass
        try:
            scroll.viewport().setUpdatesEnabled(True)
        except Exception:
            pass

        try:
            scroll.setSingleStep(60)
        except Exception:
            pass

        repaint_timer = QTimer(scroll)
        repaint_timer.setInterval(8)
        repaint_timer.setTimerType(Qt.TimerType.PreciseTimer)

        def _tick(_scroll=scroll):
            viewport = _scroll.viewport()
            if viewport is not None:
                viewport.repaint()

        repaint_timer.timeout.connect(_tick)
        scroll._tearing_fix_timer = repaint_timer

        def _start_repaint_guard():
            if not repaint_timer.isActive():
                repaint_timer.start()

        def _stop_repaint_guard():
            repaint_timer.stop()

        v_bar = scroll.verticalScrollBar()
        if v_bar is not None:
            v_bar.valueChanged.connect(lambda _v: _start_repaint_guard())

        animation = getattr(scroll, "scrollAnimation", None)
        if animation is not None:
            try:
                animation.finished.connect(_stop_repaint_guard)
            except Exception:
                pass

        idle_stop_timer = QTimer(scroll)
        idle_stop_timer.setSingleShot(True)
        idle_stop_timer.setInterval(150)
        idle_stop_timer.timeout.connect(_stop_repaint_guard)
        scroll._tearing_fix_idle_timer = idle_stop_timer

        def _restart_idle_stop(_v=None):
            idle_stop_timer.start()

        if v_bar is not None:
            v_bar.valueChanged.connect(_restart_idle_stop)

        original_wheel_event = scroll.wheelEvent

        def _guarded_wheel_event(event, _scroll=scroll, _orig=original_wheel_event):
            anim = getattr(_scroll, "scrollAnimation", None)
            if anim is not None:
                try:
                    if anim.state() == anim.State.Running:
                        anim.stop()
                except Exception:
                    pass
            _start_repaint_guard()
            idle_stop_timer.start()
            return _orig(event)

        scroll.wheelEvent = _guarded_wheel_event


def _make_smooth_scroll_area(parent=None):
    from qfluentwidgets import SmoothScrollArea

    scroll = SmoothScrollArea(parent) if parent is not None else SmoothScrollArea()
    GameListScrollArea.configure(scroll)
    return scroll


from qfluentwidgets.components.widgets.combo_box import ComboBoxMenu


class _BoundedComboBoxMenu(ComboBoxMenu):
    MAX_POPUP_WIDTH = 320

    def __init__(self, parent=None):
        super().__init__(parent)
        self.view.setTextElideMode(Qt.TextElideMode.ElideRight)
        self.view.setWordWrap(False)
        self.view.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

    def _capped_width(self):
        owner = self.parent()
        window = owner.window() if owner is not None else None
        screen = owner.screen() if owner is not None and hasattr(owner, "screen") else None
        if screen is None:
            screen = QGuiApplication.primaryScreen()

        limit = self.MAX_POPUP_WIDTH
        if screen is not None:
            limit = min(limit, screen.availableGeometry().width() - 24)
        if window is not None:
            limit = min(limit, window.width() - 24)
        if owner is not None:
            limit = max(limit, owner.width())
        return max(limit, 160)

    def adjustSize(self):
        super().adjustSize()
        capped_width = self._capped_width()
        if self.view.width() > capped_width:
            size = self.view.size()
            size.setWidth(capped_width)
            self.view.setFixedSize(size)
        if self.width() > capped_width:
            size = self.size()
            size.setWidth(capped_width)
            self.setFixedSize(size)


class ConsoleComboBox(ComboBox):
    MAX_VISIBLE_ROWS = 10

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMaxVisibleItems(self.MAX_VISIBLE_ROWS)

    def _createComboMenu(self):
        return _BoundedComboBoxMenu(self)


class HomePage(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setStyleSheet("background: transparent;")
        self._in_flight_threads: list = []
        self._selected_roms: dict = {}
        self._active_sources: set = set()
        self._current_query = ""
        self._current_console = None
        self._current_variant = None
        self._last_variant_by_console: dict[str, str] = {}
        self._current_page = 0
        self._sort_field = "title"
        self._sort_dir = "ASC"
        self._details = DetailsPanel()
        self._details.download_clicked.connect(self._on_download)
        self._init_ui()
    def _init_ui(self):
        main = QVBoxLayout(self)
        main.setContentsMargins(0, 0, 0, 0)
        main.setSpacing(0)
        self._progress = ProgressBar()
        self._progress.setVisible(False)
        self._progress.setMaximumHeight(3)
        main.addWidget(self._progress)
        self._progress_indeterminate = IndeterminateProgressBar(start=False)
        self._progress_indeterminate.setVisible(False)
        self._progress_indeterminate.setMaximumHeight(3)
        main.addWidget(self._progress_indeterminate)
        filter_widget = QWidget()
        filter_widget.setObjectName("filterBar")
        filter_widget.setStyleSheet("background: transparent;")
        filter_layout = QHBoxLayout(filter_widget)
        filter_layout.setContentsMargins(16, 10, 16, 10)
        filter_layout.setSpacing(10)
        self._search_input = SearchLineEdit()
        self._search_input.setPlaceholderText("Search ROMs by name…")
        self._search_input.setFixedHeight(32)
        self._search_input.setClearButtonEnabled(True)
        self._search_input.setMinimumWidth(90)
        self._search_input.setMaximumWidth(360)
        self._search_input.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed
        )
        self._search_input.returnPressed.connect(self._on_search)
        filter_layout.addWidget(self._search_input)
        cv_group = QWidget()
        cv_group.setObjectName("consoleVariantGroup")
        cv_group.setStyleSheet(
            "#consoleVariantGroup { border: none; background: transparent; }"
        )
        cv_layout = QHBoxLayout(cv_group)
        cv_layout.setContentsMargins(0, 0, 0, 0)
        cv_layout.setSpacing(6)
        self._console_filter = ConsoleComboBox()
        self._console_filter.setPlaceholderText("Console")
        self._console_filter.setFixedHeight(30)
        self._console_filter.setMinimumWidth(90)
        self._console_filter.currentIndexChanged.connect(self._on_console_change)
        cv_layout.addWidget(self._console_filter)
        separator = QFrame()
        separator.setFrameShape(QFrame.Shape.VLine)
        separator.setFrameShadow(QFrame.Shadow.Sunken)
        separator.setStyleSheet("color: palette(mid);")
        separator.setFixedWidth(1)
        cv_layout.addWidget(separator)
        self._variant_filter = ComboBox()
        self._variant_filter.setPlaceholderText("Variant")
        self._variant_filter.setFixedHeight(30)
        self._variant_filter.setMinimumWidth(80)
        self._variant_filter.currentIndexChanged.connect(self._on_filter_change)
        self._variant_filter.setVisible(False)
        cv_layout.addWidget(self._variant_filter)

        filter_layout.addWidget(cv_group)

        self._sort_combo = ComboBox()
        self._sort_combo.addItems(["Name A-Z", "Name Z-A", "Source A-Z", "Source Z-A"])
        self._sort_combo.setFixedHeight(32)
        self._sort_combo.setMinimumWidth(80)
        self._sort_combo.currentIndexChanged.connect(self._on_filter_change)
        filter_layout.addWidget(self._sort_combo)
        filter_layout.addStretch(1)

        self._source_filter = ComboBox()
        self._source_filter.setPlaceholderText("Source")
        self._source_filter.setFixedHeight(32)
        self._source_filter.setMinimumWidth(80)
        self._source_filter.addItem("All Sources")
        self._source_filter.setCurrentIndex(0)
        self._source_filter.currentIndexChanged.connect(self._on_source_change)
        filter_layout.addWidget(self._source_filter)
        self._btn_download_selected = PushButton(FluentIcon.DOWNLOAD, "Download Selected")
        self._btn_download_selected.setFixedHeight(32)
        self._btn_download_selected.setEnabled(False)
        self._btn_download_selected.clicked.connect(self._on_download_selected)
        filter_layout.addWidget(self._btn_download_selected)

        self._sync_btn = PrimaryPushButton(FluentIcon.SYNC, "Sync Database")
        self._sync_btn.setFixedHeight(32)
        self._sync_btn.setMinimumWidth(40)
        self._sync_btn.clicked.connect(self._on_sync)
        filter_layout.addWidget(self._sync_btn)

        self._filter_widget = filter_widget
        self._filter_layout = filter_layout
        filter_widget.setMinimumHeight(56)
        filter_widget.installEventFilter(self)
        self._toolbar_collapsed = False

        main.addWidget(filter_widget, 0)

        content_splitter = QWidget()
        content_splitter.setStyleSheet("background: transparent;")
        content_main = QVBoxLayout(content_splitter)
        content_main.setContentsMargins(0, 0, 0, 0)
        content_main.setSpacing(0)

        self._splitter = QSplitter(Qt.Orientation.Horizontal)
        self._splitter.setHandleWidth(4)

        left_panel = QWidget()
        left_panel.setStyleSheet("background: transparent;")
        left_layout = QVBoxLayout(left_panel)
        left_layout.setContentsMargins(16, 8, 16, 8)
        left_layout.setSpacing(4)

        pag_row = QHBoxLayout()
        pag_row.setSpacing(8)

        self._btn_page_prev = PushButton("◀")
        self._btn_page_prev.setFixedSize(32, 28)
        self._btn_page_prev.setEnabled(False)
        self._btn_page_prev.clicked.connect(self._on_page_prev)
        pag_row.addWidget(self._btn_page_prev)

        self._lbl_page = CaptionLabel("Page 1 of 1")
        pag_row.addWidget(self._lbl_page)

        self._page_input = LineEdit()
        self._page_input.setPlaceholderText("1")
        self._page_input.setMaxLength(6)
        self._page_input.setMaximumWidth(50)
        self._page_input.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._page_input.returnPressed.connect(self._on_jump_page)
        pag_row.addWidget(self._page_input)

        self._btn_page_next = PushButton("▶")
        self._btn_page_next.setFixedSize(32, 28)
        self._btn_page_next.clicked.connect(self._on_page_next)
        pag_row.addWidget(self._btn_page_next)

        pag_row.addStretch()
        left_layout.addLayout(pag_row)

        scroll = _make_smooth_scroll_area()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setStyleSheet("QScrollArea { background: transparent; border: none; }")
        scroll.viewport().setStyleSheet("background: transparent;")

        self._cards_container = QWidget()
        self._cards_container.setStyleSheet("background: transparent;")
        self._cards_layout = QVBoxLayout(self._cards_container)
        self._cards_layout.setContentsMargins(0, 4, 0, 4)
        self._cards_layout.setSpacing(4)
        self._cards_layout.addStretch()

        scroll.setWidget(self._cards_container)

        scroll.setObjectName("romListScroll")
        self._cards_container.setObjectName("romListContainer")
        self._list_surface = scroll
        left_layout.addWidget(scroll)
        self._apply_list_surface_tint()

        self._splitter.addWidget(self._details)
        self._splitter.addWidget(left_panel)
        self._splitter.setStretchFactor(0, 1)
        self._splitter.setStretchFactor(1, 3)

        content_main.addWidget(self._splitter)
        main.addWidget(content_splitter, 1)

        self._details.setVisible(False)
        self._splitter.setSizes([0, 900])

        qconfig.themeChanged.connect(lambda *_: self._apply_list_surface_tint())

    def eventFilter(self, obj, event):
        if obj is getattr(self, "_filter_widget", None) and event.type() == QEvent.Type.Resize:
            self._update_toolbar_layout()
        return super().eventFilter(obj, event)

    def _update_toolbar_layout(self):
        width = self._filter_widget.width()
        collapsed = width < 760

        if collapsed == self._toolbar_collapsed:
            return
        self._toolbar_collapsed = collapsed

        if collapsed:
            self._search_input.setMinimumWidth(70)
            self._search_input.setMaximumWidth(180)
            self._console_filter.setMinimumWidth(70)
            self._variant_filter.setMinimumWidth(60)
            self._sort_combo.setMinimumWidth(60)
            self._source_filter.setMinimumWidth(60)
            self._btn_download_selected.setText("")
            self._sync_btn.setText("")
            self._sync_btn.setMinimumWidth(32)
            self._btn_download_selected.setMinimumWidth(32)
        else:
            self._search_input.setMinimumWidth(90)
            self._search_input.setMaximumWidth(360)
            self._console_filter.setMinimumWidth(90)
            self._variant_filter.setMinimumWidth(80)
            self._sort_combo.setMinimumWidth(80)
            self._source_filter.setMinimumWidth(80)
            self._btn_download_selected.setText("Download Selected")
            self._sync_btn.setText("Sync Database")
            self._sync_btn.setMinimumWidth(40)
            self._btn_download_selected.setMinimumWidth(40)

    def _apply_list_surface_tint(self):
        if isDarkTheme():
            tint = "rgba(0, 0, 0, 16)"
        else:
            tint = "rgba(232, 232, 237, 1)"
        self._list_surface.setStyleSheet(
            f"QScrollArea#romListScroll {{ background-color: {tint}; border: none; }}"
            f"QScrollArea#romListScroll > QWidget > QWidget {{ background-color: transparent; }}"
            f"#romListContainer {{ background-color: transparent; }}"
        )

        if isDarkTheme():
            card_tint = "rgba(255, 255, 255, 0.04)"
            card_hover = "rgba(255, 255, 255, 0.08)"
        else:
            card_tint = "rgba(255, 255, 255, 1)"
            card_hover = "rgba(248, 248, 250, 1)"
        self._cards_container.setStyleSheet(
            f"RomCardWidget {{ "
            f"background-color: {card_tint}; "
            f"border: none; "
            f"border-radius: 8px; "
            f"padding: 0px; "
            f"}} "
            f"RomCardWidget:hover {{ "
            f"background-color: {card_hover}; "
            f"border: none; "
            f"border-radius: 8px; "
            f"}} "
        )

    def _reload_source_filter(self):
        current = self._source_filter.currentText() if hasattr(self, "_source_filter") else "All Sources"

        self._source_filter.blockSignals(True)
        self._source_filter.clear()
        self._source_filter.addItem("All Sources")
        sources = _get_all_sources()
        self._source_filter.addItems(sources)

        if current and current != "All Sources" and current in sources:
            self._source_filter.setCurrentText(current)
        else:
            self._source_filter.setCurrentIndex(0)
        self._source_filter.blockSignals(False)

    def _get_active_sources(self) -> Optional[list]:
        if not hasattr(self, "_source_filter"):
            return None
        text = self._source_filter.currentText()
        if not text or text == "All Sources":
            return None
        return [text]

    def _on_source_change(self):
                                                                                
                                                            
        sources = self._get_active_sources()
        current_console = self._current_console

        try:
            consoles = db.get_all_consoles(sources=sources)
        except Exception:
            consoles = []

        self._console_filter.blockSignals(True)
        self._console_filter.clear()
        self._console_filter.addItem("All Consoles")
        self._console_filter.addItems(consoles)
        if current_console and current_console in consoles:
            self._console_filter.setCurrentText(current_console)
        else:
            self._console_filter.setCurrentIndex(0)
        self._console_filter.blockSignals(False)

        self._current_console = (
            self._console_filter.currentText()
            if self._console_filter.currentText() and self._console_filter.currentText() != "All Consoles"
            else None
        )

                                                                           
        self._reload_variant_filter()
        self._on_filter_change()

    def _load_cards(self):
        try:
            while self._cards_layout.count():
                child = self._cards_layout.takeAt(0)
                if child.widget():
                    child.widget().deleteLater()

            sources = self._get_active_sources()
            roms = db.search_roms(
                query=self._current_query,
                console=self._current_console,
                sources=sources,
                variant=self._current_variant,
                offset=self._current_page * PAGE_SIZE,
                limit=PAGE_SIZE,
                sort_field=self._sort_field,
                sort_dir=self._sort_dir,
            )
        except Exception as exc:
            logger.warning("Failed to load ROM cards: %s", exc)
            roms = []

        self._selected_roms: dict[str, dict] = {}
        self._update_download_selected_button()

        for rom in roms:
            card = RomCardWidget(rom)
            card.rom_selected.connect(self._on_card_selected)
            card.download_clicked.connect(self._on_download)
            card.selection_toggled.connect(self._on_card_selection_toggled)
            self._cards_layout.insertWidget(self._cards_layout.count() - 1, card)

        if not roms:
            empty_lbl = CaptionLabel("No ROMs found. Try adjusting your search or filters, or run a sync.")
            empty_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
            empty_lbl.setStyleSheet("padding: 20px;")
            self._cards_layout.insertWidget(self._cards_layout.count() - 1, empty_lbl)

    def _update_pagination_ui(self):
        sources = self._get_active_sources()
        try:
            total = db.count_roms(query=self._current_query, console=self._current_console, sources=sources, variant=self._current_variant)
        except Exception:
            total = 0
        total_pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)

        self._btn_page_prev.setEnabled(self._current_page > 0)
        self._btn_page_next.setEnabled(self._current_page < total_pages - 1 and total_pages > 1)
        self._lbl_page.setText(f"Page {self._current_page + 1} of {total_pages}")
        self._page_input.setText(str(self._current_page + 1))

    def _sync_model(self):
        self._load_cards()
        self._update_pagination_ui()

    def _on_search(self):
        self._current_query = self._search_input.text().strip()
        self._current_console = (
            self._console_filter.currentText()
            if self._console_filter.currentText() and self._console_filter.currentText() != "All Consoles"
            else None
        )
        self._current_variant = self._active_variant_filter()
        sort_text = self._sort_combo.currentText()
        self._sort_dir = "ASC" if "A-Z" in sort_text else "DESC"
        self._sort_field = "source" if "Source" in sort_text else "title"
        self._current_page = 0
        self._sync_model()

    def _on_console_change(self):
        self._current_console = (
            self._console_filter.currentText()
            if self._console_filter.currentText() and self._console_filter.currentText() != "All Consoles"
            else None
        )
        self._reload_variant_filter()
        self._on_filter_change()

    def _reload_variant_filter(self):
        self._variant_filter.blockSignals(True)
        self._variant_filter.clear()

        console = self._current_console
        if not console or console == "All Consoles":
            self._variant_filter.setVisible(False)
            self._current_variant = None
            self._variant_filter.blockSignals(False)
            return

        try:
            variants = console_variants.get_variants(console)
        except Exception:
            variants = [console_variants.DEFAULT_VARIANT_NAME]

        if len(variants) <= 1:
            self._variant_filter.setVisible(False)
            self._current_variant = None
            self._variant_filter.blockSignals(False)
            return

        self._variant_filter.addItem("All Variants")
        self._variant_filter.addItems(variants)
        self._variant_filter.setVisible(True)

        remembered = self._last_variant_by_console.get(console)
        if remembered and remembered in variants:
            self._variant_filter.setCurrentText(remembered)
            self._current_variant = remembered
        else:
            self._variant_filter.setCurrentIndex(0)
            self._current_variant = None

        self._variant_filter.blockSignals(False)

    def _active_variant_filter(self) -> Optional[str]:
        if not self._variant_filter.isVisible():
            return None
        text = self._variant_filter.currentText()
        if not text or text == "All Variants":
            return None
        return text

    def _on_filter_change(self):
        self._current_query = self._search_input.text().strip()
        self._current_console = (
            self._console_filter.currentText()
            if self._console_filter.currentText() and self._console_filter.currentText() != "All Consoles"
            else None
        )
        self._current_variant = self._active_variant_filter()
        if self._current_console and self._current_variant:
            self._last_variant_by_console[self._current_console] = self._current_variant
        sort_text = self._sort_combo.currentText()
        self._sort_dir = "ASC" if "A-Z" in sort_text else "DESC"
        self._sort_field = "source" if "Source" in sort_text else "title"
        self._current_page = 0
        self._sync_model()

    @staticmethod
    def _rom_key(rom: dict) -> str:
        rid = rom.get("id")
        if rid is not None:
            return str(rid)
        return f"{rom.get('title', '')}|{rom.get('console', '')}|{rom.get('torrent_file', '')}"

    def _on_card_selection_toggled(self, rom: dict, checked: bool):
        if not hasattr(self, "_selected_roms"):
            self._selected_roms = {}
        key = self._rom_key(rom)
        if checked:
            self._selected_roms[key] = rom
        else:
            self._selected_roms.pop(key, None)
        self._update_download_selected_button()

    def _update_download_selected_button(self):
        count = len(getattr(self, "_selected_roms", {}))
        if hasattr(self, "_btn_download_selected"):
            self._btn_download_selected.setText(
                f"Download Selected ({count})" if count else "Download Selected"
            )
            self._btn_download_selected.setEnabled(count > 0)

    def _on_download_selected(self):
        roms = list(getattr(self, "_selected_roms", {}).values())
        if not roms:
            return
        panel = self._get_download_panel()
        if panel is not None:
            panel.add_many_from_roms(roms)
        self._selected_roms = {}
        self._update_download_selected_button()
        self._load_cards()

    def _get_download_panel(self):
        win = self.window()
        return getattr(win, "download_page", None)

    def _on_card_selected(self, rom: dict):
        try:
            self._details.setVisible(True)
            from PyQt6.QtCore import QTimer
            QTimer.singleShot(0, lambda: self._splitter.setSizes([350, 550]))
            self._details.select_rom(rom)
        except Exception as exc:
            logger.exception("Card selection error")
            InfoBar.error(
                title="Error",
                content=f"Failed to load ROM details: {exc}",
                orient=Qt.Orientation.Horizontal,
                isClosable=True,
                position=InfoBarPosition.TOP_RIGHT,
                duration=3000,
                parent=self.window(),
            )

    def _on_download(self, rom: dict):
        try:
            panel = self._get_download_panel()
            if panel is None:
                logger.warning("Downloader panel not available ")
                return
            panel.add_from_rom(rom)
        except Exception as exc:
            logger.exception("Download handler error")
            InfoBar.error(
                title="Error",
                content=f"Failed to queue download: {exc}",
                orient=Qt.Orientation.Horizontal,
                isClosable=True,
                position=InfoBarPosition.TOP_RIGHT,
                duration=3000,
                parent=self.window(),
            )

    def _on_sync(self):
        if hasattr(self, '_sync_thread') and self._sync_thread.isRunning():
            InfoBar.warning(
                title="Sync Running",
                content="A sync operation is already in progress.",
                orient=Qt.Orientation.Horizontal,
                isClosable=True,
                position=InfoBarPosition.TOP_RIGHT,
                duration=3000,
                parent=self,
            )
            return

        self._sync_btn.setEnabled(False)
        self._progress.setVisible(False)
        self._progress_indeterminate.setVisible(True)
        self._progress_indeterminate.start()

        self._sync_thread = QThread()
        self._sync_worker = sync_module.SyncWorker()
        self._sync_worker.moveToThread(self._sync_thread)

        self._sync_thread.started.connect(self._sync_worker.run)
        self._sync_worker.progress.connect(self._on_sync_progress)
        self._sync_worker.finished.connect(self._on_sync_finished)
        self._sync_worker.error.connect(self._on_sync_error)
        self._sync_worker.finished.connect(self._sync_thread.quit)
        self._sync_worker.error.connect(self._sync_thread.quit)

        self._sync_thread.finished.connect(self._on_sync_done)
        self._sync_thread.start()

    def _on_sync_progress(self, current: int, total: int, message: str) -> None:
        if total > 0:
            if self._progress_indeterminate.isVisible():
                self._progress_indeterminate.stop()
                self._progress_indeterminate.setVisible(False)
                self._progress.setVisible(True)
            self._progress.setValue(int(current / total * 100))

    def _on_sync_finished(self, total: int) -> None:
        pass

    def _on_sync_error(self, msg: str) -> None:
        InfoBar.error(
            title="Sync Error",
            content=msg,
            orient=Qt.Orientation.Horizontal,
            isClosable=True,
            position=InfoBarPosition.TOP_RIGHT,
            duration=5000,
            parent=self.window(),
        )

    def _on_sync_done(self) -> None:
        self._sync_btn.setEnabled(True)
        self._progress_indeterminate.stop()
        self._progress_indeterminate.setVisible(False)
        self._progress.setVisible(False)
        self._progress.setValue(0)
        try:
            self._load_cards()
            self._update_pagination_ui()
        except Exception as exc:
            logger.warning("Failed to reload cards after sync: %s", exc)

        try:
            consoles = _get_all_consoles()
            self._console_filter.clear()
            self._console_filter.addItem("All Consoles")
            self._console_filter.addItems(consoles)
        except Exception:
            pass

        try:
            self._reload_source_filter()
        except Exception:
            pass

        try:
            self._reload_variant_filter()
        except Exception:
            pass

        InfoBar.success(
            title="Sync Complete",
            content=f"{db.count_roms():,} ROMs in database.",
            orient=Qt.Orientation.Horizontal,
            isClosable=True,
            position=InfoBarPosition.TOP_RIGHT,
            duration=3000,
            parent=self,
        )

    def _on_page_prev(self):
        if self._current_page > 0:
            self._current_page -= 1
            self._sync_model()

    def _on_page_next(self):
        sources = self._get_active_sources()
        try:
            total = db.count_roms(query=self._current_query, console=self._current_console, sources=sources, variant=self._current_variant)
        except Exception:
            total = 0
        total_pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
        if self._current_page < total_pages - 1:
            self._current_page += 1
            self._sync_model()

    def _on_jump_page(self):
        try:
            page = int(self._page_input.text()) - 1
        except ValueError:
            return
        if 0 <= page:
            self._current_page = page
            self._sync_model()

class SettingsPage(QWidget):
    settings_changed = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setStyleSheet("background: transparent;")

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        scroll = _make_smooth_scroll_area(self)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setStyleSheet("QScrollArea { background: transparent; border: none; }")
        scroll.viewport().setStyleSheet("background: transparent;")
        outer.addWidget(scroll)

        container = QWidget()
        container.setStyleSheet("background: transparent;")
        scroll.setWidget(container)

        layout = QVBoxLayout(container)
        layout.setContentsMargins(32, 20, 36, 24)
        layout.setSpacing(8)

        title = SubtitleLabel("Settings")
        layout.addWidget(title)

        desc = CaptionLabel("Configure download paths, theme, and performance.")
        layout.addWidget(desc)
        layout.addSpacing(20)

        app_group = SettingCardGroup("Appearance", self)

        self._theme_combo = ComboBox()
        self._theme_combo.addItems(["Dark", "Light", "Auto (Follow System)"])
        self._theme_combo.setMinimumWidth(160)
        theme_card = SettingCard(
            FluentIcon.BRUSH,
            "Theme Mode",
            "Choose between Dark, Light, or automatically follow Windows system theme",
            self,
        )
        theme_card.hBoxLayout.addWidget(self._theme_combo, 0, Qt.AlignmentFlag.AlignRight)
        theme_card.hBoxLayout.addSpacing(4)
        app_group.addSettingCard(theme_card)

        accent_layout = QHBoxLayout()
        accent_layout.setSpacing(8)
        accent_layout.addStretch()

        self._accent_preview = QWidget()
        self._accent_preview.setFixedWidth(24)
        self._accent_preview.setFixedHeight(24)
        self._accent_preview.setCursor(Qt.CursorShape.PointingHandCursor)
        self._accent_preview.setToolTip("Click to reset to system accent color")
        self._accent_preview.mousePressEvent = lambda event: self._reset_accent()

        accent_reset = PushButton("Reset to System Accent")
        accent_reset.clicked.connect(self._reset_accent)
        accent_reset.setFixedWidth(170)

        accent_layout.addWidget(self._accent_preview)
        accent_layout.addWidget(accent_reset)

        accent_card = SettingCard(
            FluentIcon.PALETTE,
            "Accent Color",
            "Uses the Windows system accent color. Click reset to restore the default.",
            self,
        )
        accent_card.hBoxLayout.addLayout(accent_layout)
        accent_card.hBoxLayout.addSpacing(4)
        app_group.addSettingCard(accent_card)

        layout.addWidget(app_group)
        layout.addSpacing(20)

        dl_group = SettingCardGroup("Download Directory", self)

        self._txt_download_dir = LineEdit()
        self._txt_download_dir.setReadOnly(True)
        self._txt_download_dir.setMinimumWidth(260)
        self._dl_browse_btn = PushButton("Browse…")
        self._dl_browse_btn.clicked.connect(self._browse_download_dir)

        dl_card = SettingCard(FluentIcon.FOLDER, "Download folder",
                              "Where completed ROM downloads are saved", self)
        dl_card.hBoxLayout.addWidget(self._txt_download_dir, 0, Qt.AlignmentFlag.AlignRight)
        dl_card.hBoxLayout.addSpacing(8)
        dl_card.hBoxLayout.addWidget(self._dl_browse_btn, 0, Qt.AlignmentFlag.AlignRight)
        dl_card.hBoxLayout.addSpacing(4)
        dl_group.addSettingCard(dl_card)

        self._chk_console_structure = SwitchSettingCard(
            icon=FluentIcon.IOT,
            title="Console-based folder structure",
            content="Organize downloads as /downloads/{Console}/{Game Title}",
        )
        dl_group.addSettingCard(self._chk_console_structure)

        layout.addWidget(dl_group)
        layout.addSpacing(20)

        perf_group = SettingCardGroup("Seeding & Performance", self)

        self._spin_seed = CompactSpinBox()
        self._spin_seed.setRange(0, 9999)
        self._spin_seed.setSuffix(" min")
        seed_card = SettingCard(FluentIcon.HISTORY, "Seed Time",
                                "How long to keep seeding after a download completes", self)
        seed_card.hBoxLayout.addWidget(self._spin_seed, 0, Qt.AlignmentFlag.AlignRight)
        seed_card.hBoxLayout.addSpacing(4)
        perf_group.addSettingCard(seed_card)

        self._spin_speed = CompactSpinBox()
        self._spin_speed.setRange(0, 100000)
        self._spin_speed.setSuffix(" KB/s")
        speed_card = SettingCard(FluentIcon.SPEED_HIGH, "Download Speed Limit",
                                 "Maximum download speed, 0 for unlimited", self)
        speed_card.hBoxLayout.addWidget(self._spin_speed, 0, Qt.AlignmentFlag.AlignRight)
        speed_card.hBoxLayout.addSpacing(4)
        perf_group.addSettingCard(speed_card)

        self._spin_upload_speed = CompactSpinBox()
        self._spin_upload_speed.setRange(0, 100000)
        self._spin_upload_speed.setSuffix(" KB/s")
        upload_speed_card = SettingCard(
            FluentIcon.SPEED_HIGH, "Upload Speed Limit",
            "Maximum upload/seeding speed, an unlimited upload here can "
            "saturate your connection and starve download speed", self,
        )
        upload_speed_card.hBoxLayout.addWidget(self._spin_upload_speed, 0, Qt.AlignmentFlag.AlignRight)
        upload_speed_card.hBoxLayout.addSpacing(4)
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

        layout.addWidget(perf_group)
        layout.addSpacing(20)

        feature_group = SettingCardGroup("Experimental Features (Currently placeholders)", self)

        self._chk_pc_games = SwitchSettingCard(
            icon=FluentIcon.GAME,
            title="PC Games (Repacks)",
            content="Show a PC Games section in the sidebar (placeholder, repack management coming later)",
        )
        self._connect_switch(self._chk_pc_games, self._on_pc_games_toggled)
        feature_group.addSettingCard(self._chk_pc_games)

        self._chk_local_dat = SwitchSettingCard(
            icon=FluentIcon.DOCUMENT,
            title="Local DAT Support",
            content="Show a Local DAT section in the sidebar (placeholder, DAT importing coming later)",
        )
        self._connect_switch(self._chk_local_dat, self._on_local_dat_toggled)
        feature_group.addSettingCard(self._chk_local_dat)

        layout.addWidget(feature_group)
        layout.addSpacing(20)

        self._save_btn = PrimaryPushButton(FluentIcon.SAVE, "Save Settings")
        self._save_btn.setFixedHeight(38)
        self._save_btn.clicked.connect(self._on_save)
        layout.addWidget(self._save_btn)

        layout.addStretch()

        self._populate()
        self._apply_accent_preview()

    @staticmethod
    def _connect_switch(card, slot):
        sig = getattr(card, "checkedChanged", None)
        if sig is None:
            sig = card.switchButton.checkedChanged
        sig.connect(slot)

    def _on_pc_games_toggled(self, checked: bool):
        from ..core.config import settings as _s, apply_settings

        _s.pc_games_enabled = checked
        apply_settings(pc_games_enabled=checked)
        save_settings(_s)
        self.settings_changed.emit()

    def _on_local_dat_toggled(self, checked: bool):
        from ..core.config import settings as _s, apply_settings

        _s.local_dat_enabled = checked
        apply_settings(local_dat_enabled=checked)
        save_settings(_s)
        self.settings_changed.emit()

    def _on_save(self):
        self.apply()
        InfoBar.success(
            title="Settings Saved",
            content="All settings have been applied and persisted.",
            orient=Qt.Orientation.Horizontal,
            isClosable=True,
            position=InfoBarPosition.TOP_RIGHT,
            duration=2000,
            parent=self.window(),
        )

    def _apply_accent_preview(self):
        try:
            from qfluentwidgets import ThemeColor
            color = ThemeColor.primary()
            self._accent_preview.setStyleSheet(
                f"background-color: {color.name(QColor.NameFormat.HexRgb)}; "
                "border-radius: 4px; border: 1px solid palette(mid);"
            )
        except Exception:
            self._accent_preview.setStyleSheet("background-color: #0078d4; border-radius: 4px; border: 1px solid palette(mid);")

    def _reset_accent(self):
        try:
            from qfluentwidgets import ThemeColor
            ThemeColor.primary()
            self._apply_accent_preview()
            InfoBar.success(
                title="Accent Reset",
                content="Accent color restored to default.",
                orient=Qt.Orientation.Horizontal,
                isClosable=True,
                position=InfoBarPosition.TOP_RIGHT,
                duration=2000,
                parent=self.window(),
            )
        except Exception as exc:
            logger.warning("Failed to reset accent color: %s", exc)

    def _browse_download_dir(self):
        path = QFileDialog.getExistingDirectory(self, "Select Download Directory", self._txt_download_dir.text())
        if path:
            self._txt_download_dir.setText(path)

    def _populate(self):
        from ..core.config import settings as _s, paths

        self._txt_download_dir.setText(_s.download_dir or paths.download_root)
        self._chk_console_structure.setChecked(getattr(_s, "_console_structure", True))
        self._spin_seed.setValue(_s.seed_time)
        self._spin_speed.setValue(_s.speed_limit)
        self._spin_upload_speed.setValue(getattr(_s, "upload_speed_limit", 500))
        self._chk_auto.setChecked(getattr(_s, "auto_download", False))
        self._chk_delete_torrent.setChecked(getattr(_s, "delete_torrent_after", True))
        self._chk_pc_games.setChecked(getattr(_s, "pc_games_enabled", False))
        self._chk_local_dat.setChecked(getattr(_s, "local_dat_enabled", False))

        mode_map = {"Dark": 0, "Light": 1, "Auto": 2}
        idx = mode_map.get(_s.theme_mode, 0)
        self._theme_combo.setCurrentIndex(idx)

    def apply(self):
        from ..core.config import settings as _s, paths, apply_settings, resolve_theme
        from qfluentwidgets import setTheme

        _s.download_dir = self._txt_download_dir.text() or paths.download_root
        _s.seed_time = self._spin_seed.value()
        _s.speed_limit = self._spin_speed.value()
        _s.upload_speed_limit = self._spin_upload_speed.value()
        _s.delete_torrent_after = self._chk_delete_torrent.isChecked()
        _s._console_structure = self._chk_console_structure.isChecked()
        _s.auto_download = self._chk_auto.isChecked()
        _s.pc_games_enabled = self._chk_pc_games.isChecked()
        _s.local_dat_enabled = self._chk_local_dat.isChecked()

        theme_idx = self._theme_combo.currentIndex()
        theme_modes = [ThemeMode.DARK, ThemeMode.LIGHT, ThemeMode.AUTO]
        _s.theme_mode = theme_modes[theme_idx]
        theme_list = [Theme.DARK, Theme.LIGHT, Theme.AUTO]
        qconfig.theme = theme_list[theme_idx]

        apply_settings(
            download_dir=_s.download_dir,
            seed_time=_s.seed_time,
            speed_limit=_s.speed_limit,
            upload_speed_limit=_s.upload_speed_limit,
            delete_torrent_after=_s.delete_torrent_after,
            theme_mode=_s.theme_mode,
        )

        resolve_theme(setTheme)
        save_settings(_s)
        self.settings_changed.emit()

class AboutPage(QWidget):
    GITHUB_URL = "https://github.com/TheJustRun/PiraChest"
    AUTHOR_NAME = "JustRun"
    AUTHOR_URL = "https://github.com/TheJustRun"
    LOGO_ARTIST_REDDIT_USER = "u/spicysaltysparty"
    LOGO_ARTIST_URL = "https://reddit.com/u/spicysaltysparty"

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setStyleSheet("background: transparent;")

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        scroll = _make_smooth_scroll_area(self)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setStyleSheet("QScrollArea { background: transparent; border: none; }")
        scroll.viewport().setStyleSheet("background: transparent;")
        outer.addWidget(scroll)

        container = QWidget()
        container.setStyleSheet("background: transparent;")
        scroll.setWidget(container)

        layout = QVBoxLayout(container)
        layout.setContentsMargins(32, 24, 36, 24)
        layout.setSpacing(16)

        title = SubtitleLabel("About PiraChest")

        header_row = QHBoxLayout()
        header_row.setSpacing(20)

        header_left = QVBoxLayout()
        header_left.setSpacing(6)
        header_left.addWidget(title)

        ver = BodyLabel("Version 0.1.0 (Alpha)")
        header_left.addWidget(ver)

        desc = BodyLabel(
            "PiraChest is a Work in Progress All in One Downloader.\n\n"
            "Index data: Minerva Archive (https://minerva-archive.org)\n"
            "UI framework: PyQt6 + QFluentWidgets\n\n"
            "This tool is for educational purposes only\n\n"
            "and PLEASE don't annoy me with the \"Oh apps like this killed Myrient!\""
        )
        desc.setWordWrap(True)
        header_left.addWidget(desc)

        header_row.addLayout(header_left, 1)

        self._about_banner = self._build_about_banner()
        header_row.addWidget(self._about_banner, 0, Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignRight)

        layout.addLayout(header_row)

        links_group = SettingCardGroup("Links", self)

        github_card = SettingCard(
            FluentIcon.GITHUB,
            "Source Code",
            "View the project, report issues, or contribute on GitHub",
            self,
        )
        github_btn = HyperlinkButton(self.GITHUB_URL, "Open GitHub")
        github_card.hBoxLayout.addWidget(github_btn, 0, Qt.AlignmentFlag.AlignRight)
        github_card.hBoxLayout.addSpacing(4)
        links_group.addSettingCard(github_card)

        layout.addWidget(links_group)

        credits_group = SettingCardGroup("Credits", self)

        author_card = CardWidget()

        author_card.setFixedHeight(84)
        author_layout = QHBoxLayout(author_card)
        author_layout.setContentsMargins(16, 0, 16, 0)
        author_layout.setSpacing(14)
        author_layout.setAlignment(Qt.AlignmentFlag.AlignVCenter)
        self._author_avatar = self._build_author_avatar()
        author_layout.addWidget(self._author_avatar, 0, Qt.AlignmentFlag.AlignVCenter)
        author_text_col = QVBoxLayout()
        author_text_col.setSpacing(2)
        author_text_col.setContentsMargins(0, 0, 0, 0)
        author_name_lbl = StrongBodyLabel(f"Made by {self.AUTHOR_NAME}")
        author_text_col.addWidget(author_name_lbl)
        author_sub_lbl = CaptionLabel("Developer & maintainer of PiraChest")
        author_text_col.addWidget(author_sub_lbl)
        author_layout.addLayout(author_text_col)
        author_layout.addStretch(1)

        author_link_btn = HyperlinkButton(self.AUTHOR_URL, "Profile")
        author_layout.addWidget(author_link_btn, 0, Qt.AlignmentFlag.AlignVCenter)

        credits_group.addSettingCard(author_card)

        thanks_card = SettingCard(
            FluentIcon.HEART,
            "Special Thanks",
            f"App logo created by {self.LOGO_ARTIST_REDDIT_USER}",
            self,
        )
        thanks_btn = HyperlinkButton(self.LOGO_ARTIST_URL, self.LOGO_ARTIST_REDDIT_USER)
        thanks_card.hBoxLayout.addWidget(thanks_btn, 0, Qt.AlignmentFlag.AlignRight)
        thanks_card.hBoxLayout.addSpacing(4)
        credits_group.addSettingCard(thanks_card)

        layout.addWidget(credits_group)

        layout.addStretch()

    def _build_about_banner(self) -> QWidget:
        from PyQt6.QtGui import QPixmap
        from PyQt6.QtWidgets import QLabel, QVBoxLayout, QHBoxLayout, QSizePolicy
        import os as _os

        MAX_W, MAX_H = 480, 160

        banner_widget = QWidget()
        banner_widget.setMaximumSize(MAX_W, MAX_H)
        banner_widget.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Preferred)
        layout = QVBoxLayout(banner_widget)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)

        banner_path = None
        try:
            gui_dir = _os.path.dirname(_os.path.abspath(__file__))
            for name in ("banner.png", "banner.jpg", "banner.jpeg", "banner.svg"):
                candidate = _os.path.join(gui_dir, name)
                if _os.path.isfile(candidate):
                    banner_path = candidate
                    break
        except Exception:
            pass

        banner_label = QLabel()
        banner_label.setAlignment(Qt.AlignmentFlag.AlignCenter)

        if banner_path:
            pixmap = QPixmap(banner_path)
            if not pixmap.isNull():
                scaled = pixmap.scaled(
                    MAX_W, MAX_H,
                    Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation
                )
                banner_label.setFixedSize(scaled.size())
                banner_label.setPixmap(scaled)
                layout.addWidget(banner_label)
            else:
                banner_path = None

        if not banner_path:
            row = QHBoxLayout()
            row.setSpacing(16)
            row.addStretch(1)

            logo_path = None
            try:
                from .splash import find_logo_path
                logo_path = find_logo_path()
            except Exception:
                pass

            if logo_path:
                pixmap = QPixmap(logo_path)
                if not pixmap.isNull():
                    logo_size = min(100, MAX_H - 20)
                    logo_label = QLabel()
                    logo_label.setFixedSize(logo_size, logo_size)
                    logo_label.setScaledContents(True)
                    logo_label.setPixmap(pixmap.scaled(
                        logo_size, logo_size,
                        Qt.AspectRatioMode.KeepAspectRatio,
                        Qt.TransformationMode.SmoothTransformation
                    ))
                    logo_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
                    row.addWidget(logo_label, 0, Qt.AlignmentFlag.AlignVCenter)

            text_col = QVBoxLayout()
            text_col.setSpacing(4)

            name_label = SubtitleLabel("PiraChest")
            text_col.addWidget(name_label)

            version_label = CaptionLabel("v1.0.0")
            text_col.addWidget(version_label)

            row.addLayout(text_col)
            row.addStretch(1)

            layout.addStretch(1)
            layout.addLayout(row)
            layout.addStretch(1)

        return banner_widget

    def _build_author_avatar(self) -> QWidget:
        from PyQt6.QtGui import QPixmap, QPainter, QPainterPath, QBrush, QColor as _QColor

        size = 56
        avatar = QLabel()
        avatar.setFixedSize(size, size)

        pixmap_path = None
        try:
            from .splash import find_logo_path
            import os as _os

            gui_dir = _os.path.dirname(_os.path.abspath(__file__))
            for name in ("author.png", "author.jpg", "author.jpeg", "author.ico", "author.svg"):
                candidate = _os.path.join(gui_dir, name)
                if _os.path.isfile(candidate):
                    pixmap_path = candidate
                    break
        except Exception:
            pixmap_path = None

        canvas = QPixmap(size, size)
        canvas.fill(Qt.GlobalColor.transparent)
        painter = QPainter(canvas)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        clip_path = QPainterPath()
        clip_path.addEllipse(0, 0, size, size)
        painter.setClipPath(clip_path)

        if pixmap_path:
            src = QPixmap(pixmap_path)
            if not src.isNull():
                src = src.scaled(
                    size, size,
                    Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                    Qt.TransformationMode.SmoothTransformation,
                )
                x = (src.width() - size) // 2
                y = (src.height() - size) // 2
                painter.drawPixmap(0, 0, src, x, y, size, size)
            else:
                pixmap_path = None

        if not pixmap_path:
            try:
                from qfluentwidgets import ThemeColor
                fill = ThemeColor.primary()
            except Exception:
                fill = _QColor("#0078d4")
            painter.setBrush(QBrush(fill))
            painter.setPen(Qt.PenStyle.NoPen)
            painter.drawEllipse(0, 0, size, size)
            painter.setPen(_QColor("white"))
            font = painter.font()
            font.setPointSize(20)
            font.setBold(True)
            painter.setFont(font)
            initial = (self.AUTHOR_NAME.strip()[:1] or "?").upper()
            painter.drawText(canvas.rect(), Qt.AlignmentFlag.AlignCenter, initial)

        painter.end()
        avatar.setPixmap(canvas)
        return avatar

class PlaceholderPage(QWidget):
    def __init__(self, icon, title: str, message: str, parent=None):
        super().__init__(parent)
        self.setStyleSheet("background: transparent;")

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        outer.addStretch(1)

        card = CardWidget()
        card.setMinimumWidth(420)
        card.setMaximumWidth(460)
        card_layout = QVBoxLayout(card)
        card_layout.setContentsMargins(36, 36, 36, 36)
        card_layout.setSpacing(14)

        icon_lbl = QLabel()
        try:
            pixmap = icon.icon().pixmap(48, 48)
            icon_lbl.setPixmap(pixmap)
        except Exception:
            pass
        icon_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        card_layout.addWidget(icon_lbl)

        title_lbl = SubtitleLabel(title)
        title_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        card_layout.addWidget(title_lbl)

        body_lbl = BodyLabel(message)
        body_lbl.setWordWrap(True)
        body_lbl.setMinimumWidth(340)
        body_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        card_layout.addWidget(body_lbl)

        status_lbl = CaptionLabel("Not implemented yet, may coming in future updates.")
        status_lbl.setWordWrap(True)
        status_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        card_layout.addWidget(status_lbl)

        center_row = QHBoxLayout()
        center_row.addStretch(1)
        center_row.addWidget(card)
        center_row.addStretch(1)
        outer.addLayout(center_row)
        outer.addStretch(1)

class AlphaDisclaimerDialog(MessageBoxBase):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumWidth(460)

        title_lbl = SubtitleLabel("PiraChest is in Alpha")
        self.viewLayout.addWidget(title_lbl)

        body = BodyLabel(
            "PiraChest is still in active alpha development.\n\n"
            "Some features may be incomplete or missing, and some things "
            "may not work as intended. You may run into bugs, crashes, or "
            "unexpected behavior, please back up anything important and "
            "report issues if you hit them."
        )
        body.setWordWrap(True)
        self.viewLayout.addWidget(body)

        self.viewLayout.addSpacing(8)

        self._chk_never_show = CheckBox("Don't show this again")
        self.viewLayout.addWidget(self._chk_never_show)

        self.yesButton.setText("Got it")
        self.cancelButton.hide()

    def never_show_again(self) -> bool:
        return self._chk_never_show.isChecked()

def _maybe_show_alpha_disclaimer(parent: "MainWindow") -> None:
    from ..core.config import settings as _s

    if getattr(_s, "hide_alpha_disclaimer", False):
        return

    dialog = AlphaDisclaimerDialog(parent)
    dialog.exec()

    if dialog.never_show_again():
        _s.hide_alpha_disclaimer = True
        try:
            save_settings(_s)
        except Exception:
            logger.exception("Fail")

class MainWindow(FluentWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("PiraChest")
        self.resize(1200, 780)
        self.setMinimumSize(1000, 650)

        resolve_theme(setTheme)

        from .splash import find_logo_path
        from PyQt6.QtGui import QIcon
        from PyQt6.QtCore import QSize
        from PyQt6.QtWidgets import QApplication
        from qfluentwidgets import SplashScreen

        logo_path = find_logo_path()
        if logo_path:
            self.setWindowIcon(QIcon(logo_path))
        else:
            logger.warning(
                "No logo"
            )

        self.splashScreen = SplashScreen(self.windowIcon(), self)
        self.splashScreen.setIconSize(QSize(160, 160))
        self.show()
        QApplication.processEvents()

        self.setMicaEffectEnabled(True)

        self.navigationInterface.setExpandWidth(180)
        self.navigationInterface.setCollapsible(False)

        qconfig.themeChanged.connect(lambda *_: self._apply_content_surface_tint())

        self.home_page = HomePage(self)
        self.home_page.setObjectName("minervaPage")
        self.addSubInterface(
            self.home_page, FluentIcon.LIBRARY, "Minerva"
        )

        self.pc_games_page: Optional[QWidget] = None
        self.local_dat_page: Optional[QWidget] = None

        self._sync_optional_pages()

        from ..core.download_manager import DownloadManager
        from .download_manager_panel import DownloadManagerPage

        self.download_manager = DownloadManager(self)
        self.download_page = DownloadManagerPage(self.download_manager, self)
        self.download_page.setObjectName("downloadPage")
        self.addSubInterface(
            self.download_page, FluentIcon.DOWNLOAD, "Downloads"
        )

        self.settings_page = SettingsPage(self)
        self.settings_page.setObjectName("settingsPage")
        self.addSubInterface(
            self.settings_page, FluentIcon.SETTING, "Settings"
        )

        self.about_page = AboutPage(self)
        self.about_page.setObjectName("aboutPage")
        self.addSubInterface(
            self.about_page, FluentIcon.INFO, "About",
            position=NavigationItemPosition.BOTTOM,
        )

        self._apply_content_surface_tint()

        self.switchTo(self.home_page)

        self.settings_page.settings_changed.connect(self._on_settings_changed)

        self._load_filters()

        self._auto_sync()

        self.splashScreen.finish()

        _maybe_show_alpha_disclaimer(self)

    def _apply_content_surface_tint(self):
        style = settings_qss()
        self.settings_page.setStyleSheet(style)
        self.about_page.setStyleSheet(style)

    def closeEvent(self, event):
        try:
            self.download_manager.shutdown()
        except Exception:
            logger.exception("Error stopping download manager")
        super().closeEvent(event)

    def _on_settings_changed(self):
        try:
            self._sync_optional_pages()
        except Exception:
            logger.exception("Failed to sync optional sidebar pages")
        try:
            self.home_page._sync_model()
        except Exception:
            pass
        try:
            from ..core.config import settings as _s

            self.download_manager.set_global_limits(
                down_kbps=_s.speed_limit,
                up_kbps=getattr(_s, "upload_speed_limit", 500),
            )
        except Exception:
            logger.exception("Failed to propagate settings to download manager")

    def _remove_subinterface(self, page: Optional[QWidget]) -> None:
        if page is None:
            return
        try:
            self.navigationInterface.removeWidget(page.objectName())
        except Exception:
            logger.exception("Failed to remove navigation item for %s", page.objectName())
        try:
            self.stackedWidget.removeWidget(page)
        except Exception:
            logger.exception("Failed to remove stacked widget for %s", page.objectName())
        page.deleteLater()

    def _sync_optional_pages(self) -> None:
        from ..core.config import settings as _s

        pc_enabled = getattr(_s, "pc_games_enabled", False)
        dat_enabled = getattr(_s, "local_dat_enabled", False)

        added_new_page = False

        if pc_enabled and self.pc_games_page is None:
            self.pc_games_page = PlaceholderPage(
                FluentIcon.GAME,
                "PC Games (Repacks)",
                "Browse, manage, and download PC game repacks from here once this "
                "feature is implemented.",
            )
            self.pc_games_page.setObjectName("pcGamesPage")
            self.addSubInterface(
                self.pc_games_page, FluentIcon.GAME, "PC Games",
                position=NavigationItemPosition.TOP,
            )
            added_new_page = True
        elif not pc_enabled and self.pc_games_page is not None:
            self._remove_subinterface(self.pc_games_page)
            self.pc_games_page = None

        if dat_enabled and self.local_dat_page is None:
            self.local_dat_page = PlaceholderPage(
                FluentIcon.DOCUMENT,
                "Local DAT Support",
                "Import and manage local DAT files to verify and organize your "
                "ROM collection once this feature is implemented.",
            )
            self.local_dat_page.setObjectName("localDatPage")
            self.addSubInterface(
                self.local_dat_page, FluentIcon.DOCUMENT, "Local DAT",
                position=NavigationItemPosition.TOP,
            )
            added_new_page = True
        elif not dat_enabled and self.local_dat_page is not None:
            self._remove_subinterface(self.local_dat_page)
            self.local_dat_page = None
        if added_new_page:
            self._pin_fixed_pages_below_optional()

    def _pin_fixed_pages_below_optional(self) -> None:
        for page, icon, text in (
            (getattr(self, "download_page", None), FluentIcon.DOWNLOAD, "Downloads"),
            (getattr(self, "settings_page", None), FluentIcon.SETTING, "Settings"),
        ):
            if page is None:
                continue
            try:
                self.navigationInterface.removeWidget(page.objectName())
            except Exception:
                logger.exception("Failed reordering navigation item for %s", page.objectName())
                continue
            self.addSubInterface(page, icon, text)

    def _load_filters(self):
        consoles = _get_all_consoles()
        if consoles:
            self.home_page._console_filter.clear()
            self.home_page._console_filter.addItem("All Consoles")
            self.home_page._console_filter.addItems(consoles)
        else:
            self.home_page._console_filter.addItem("All Consoles")

        try:
            self.home_page._reload_source_filter()
        except Exception as exc:
            logger.warning("Failed to load source filter: %s", exc)

        try:
            self.home_page._sync_model()
        except Exception as exc:
            logger.warning("Failed to load initial ROM data: %s", exc)

    def _auto_sync(self):
        try:
            count = db.count_roms()
        except Exception:
            count = 0

        if count == 0:
            logger.info("Database is empty, triggering auto-sync on launch")
            self.home_page._on_sync()

def _build_app_font() -> "QFont":
    candidates = ["Segoe UI Variable", "Segoe UI", "Inter", "Noto Sans", "Arial"]
    font = QFont()
    font.setFamilies(candidates)
    font.setPointSize(10)
    font.setStyleStrategy(
        QFont.StyleStrategy.PreferAntialias | QFont.StyleStrategy.PreferQuality
    )
    font.setHintingPreference(QFont.HintingPreference.PreferFullHinting)
    return font

def create_application(argv: Optional[list] = None):
    from PyQt6.QtWidgets import QApplication
    from PyQt6.QtGui import QSurfaceFormat
    from qfluentwidgets import setFont

    surface_format = QSurfaceFormat()
    surface_format.setSwapInterval(1)
    surface_format.setSwapBehavior(QSurfaceFormat.SwapBehavior.DoubleBuffer)
    QSurfaceFormat.setDefaultFormat(surface_format)

    app = QApplication(argv if argv is not None else sys.argv)
    app.setApplicationName("PiraChest")
    app.setApplicationDisplayName("PiraChest")
    app.setOrganizationName("PiraChest")

    base_font = _build_app_font()
    app.setFont(base_font)
    setFont(app, fontSize=base_font.pointSize())
    return app