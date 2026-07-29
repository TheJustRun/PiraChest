from __future__ import annotations

import gc
import logging
import os
from typing import Optional

from PyQt6.QtCore import QObject, QThread, Qt, QEvent, QTimer, pyqtSignal, QPropertyAnimation, QEasingCurve, QPoint, QSize
from PyQt6.QtGui import QColor, QGuiApplication
from PyQt6.QtWidgets import (
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QFrame,
    QVBoxLayout,
    QWidget,
    QSplitter,
    QSizePolicy,
    QGraphicsOpacityEffect,
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
    InfoBadge,
    InfoBadgePosition,
    IndeterminateProgressBar,
    LineEdit,
    MessageBoxBase,
    NavigationItemPosition,
    PrimaryPushButton,
    PrimaryToolButton,
    TransparentToolButton,
    ProgressBar,
    PushButton,
    SearchLineEdit,
    SettingCard,
    SettingCardGroup,
    StrongBodyLabel,
    SubtitleLabel,
    SwitchButton,
    SwitchSettingCard,
    IconWidget,
    TitleLabel,
    setTheme,
    qconfig,
    CaptionLabel,
)

from ..core import database as db, sync as sync_module
from ..core import console_variants
from ..core.config import settings as _global_settings, resolve_theme, ThemeMode
from ..core.theme import palette, settings_qss
from ..core.config import save_settings
from ..core.translations import tr, available_languages, current_language, set_language, register_locale_refresh
from .repacks_page import RepacksPage

logger = logging.getLogger(__name__)

PAGE_SIZE = 30
TOOLBAR_COLLAPSE_WIDTH = 700

_GUI_DIR = os.path.dirname(os.path.abspath(__file__))
_LOGO_CANDIDATES = ("logo.ico", "logo.png", "logo.svg", "logo.jpg", "logo.jpeg")


def find_logo_path() -> Optional[str]:
    for name in _LOGO_CANDIDATES:
        candidate = os.path.join(_GUI_DIR, name)
        if os.path.isfile(candidate):
            return candidate
    return None


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


class _LazyPage(QWidget):

    __slots__ = ('_factory', '_real_page', '_layout')

    def __init__(self, factory, parent=None):
        super().__init__(parent)
        self._factory = factory
        self._real_page: Optional[QWidget] = None
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self._layout = layout

    def showEvent(self, event):
        if self._real_page is None:
            self._real_page = self._factory()
            self._layout.addWidget(self._real_page)
        super().showEvent(event)

    def hideEvent(self, event):
        if self._real_page is not None:
            page = self._real_page
            self._real_page = None
            self._layout.removeWidget(page)
            page.setParent(None)
            page.deleteLater()
            gc.collect()
        super().hideEvent(event)


class _LazyRepacksPage(QWidget):
    __slots__ = ('_download_manager', '_real_page', '_layout')

    def __init__(self, download_manager, parent=None):
        super().__init__(parent)
        self._download_manager = download_manager
        self._real_page: Optional[QWidget] = None
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self._layout = layout

    def showEvent(self, event):
        if self._real_page is None:
            self._real_page = RepacksPage(self._download_manager)
            self._layout.addWidget(self._real_page)
        super().showEvent(event)

    def hideEvent(self, event):
        if self._real_page is not None:
            page = self._real_page
            self._real_page = None
            try:
                for tab in getattr(page, '_tabs', {}).values():
                    try:
                        tab.shutdown()
                    except Exception:
                        logger.exception('Failed to shut down repacks source tab during page unload')
            except Exception:
                logger.exception('Failed to shut down repacks tabs during page unload')
            self._layout.removeWidget(page)
            page.setParent(None)
            page.deleteLater()
            gc.collect()
        super().hideEvent(event)


_UPDATE_THREADS_IN_FLIGHT: set = set()


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
        layout.setContentsMargins(8, 4, 8, 4)
        layout.setSpacing(8)
        self._select_chk = CheckBox()
        self._select_chk.setFixedWidth(18)
        self._select_chk.toggled.connect(lambda checked: self.selection_toggled.emit(self._rom, checked))
        layout.addWidget(self._select_chk)
        title_val = str(rom.get("title", "—") or "—")
        self._title_lbl = StrongBodyLabel(title_val)
        self._title_lbl.setWordWrap(False)
        self._title_lbl.setToolTip(title_val)
        layout.addWidget(self._title_lbl, stretch=1)
        self._sub_lbl = CaptionLabel("")
        self._sub_lbl.setFixedHeight(16)
        layout.addWidget(self._sub_lbl)
        self._dl_btn = PrimaryToolButton(FluentIcon.DOWNLOAD, self)
        self._dl_btn.setFixedSize(32, 32)
        self._dl_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._dl_btn.clicked.connect(lambda _=False: self.download_clicked.emit(self._rom))
        layout.addWidget(self._dl_btn)
        self._refresh_subtitle()
        register_locale_refresh(self, self._refresh_subtitle)

    def _refresh_subtitle(self):
        parts = [str(p) for p in (self._rom.get("console", ""), self._rom.get("source", ""), self._rom.get("file_size", "")) if p]
        self._sub_lbl.setText("  •  ".join(parts) if parts else tr("home.unknown"))

    def set_rom(self, rom: dict):
        self._rom = rom
        title_val = str(rom.get("title", "—") or "—")
        self._title_lbl.setText(title_val)
        self._title_lbl.setToolTip(title_val)
        self._refresh_subtitle()
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
        self.setMaximumWidth(360)
        self.setMinimumWidth(260)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(4)
        self._title_lbl = SubtitleLabel("")
        self._title_lbl.setWordWrap(True)
        layout.addWidget(self._title_lbl)
        self._console_lbl = CaptionLabel("")
        self._author_lbl = CaptionLabel("")
        self._size_lbl = CaptionLabel("")
        self._source_lbl = CaptionLabel("")
        self._region_lbl = CaptionLabel("")
        self._lang_lbl = CaptionLabel("")
        self._date_lbl = CaptionLabel("")
        for lbl in (self._console_lbl, self._author_lbl, self._size_lbl, self._source_lbl, self._region_lbl, self._lang_lbl, self._date_lbl):
            layout.addWidget(lbl)
        self._desc_title_lbl = StrongBodyLabel("")
        layout.addWidget(self._desc_title_lbl)
        self._desc_card = CardWidget()
        desc_layout = QVBoxLayout(self._desc_card)
        desc_layout.setContentsMargins(8, 8, 8, 8)
        self._desc_edit = BodyLabel("")
        self._desc_edit.setWordWrap(True)
        desc_layout.addWidget(self._desc_edit)
        self._desc_card.setMinimumHeight(60)
        self._desc_card.setMaximumHeight(200)
        layout.addWidget(self._desc_card)
        layout.addStretch()
        self._dl_btn = PrimaryPushButton(FluentIcon.DOWNLOAD, "")
        self._dl_btn.setEnabled(False)
        self._dl_btn.clicked.connect(self._on_download)
        layout.addWidget(self._dl_btn)
        self._apply_locale()
        register_locale_refresh(self, self._apply_locale)

    def _apply_locale(self):
        self._dl_btn.setText(tr("home.download_rom"))
        self._render()

    def _render(self):
        rom = self._rom
        if not rom:
            self._title_lbl.setText(tr("home.select_a_rom"))
            for lbl in (self._console_lbl, self._author_lbl, self._size_lbl, self._source_lbl, self._region_lbl, self._lang_lbl, self._date_lbl):
                lbl.setText("")
            self._desc_title_lbl.setText(tr("rom_details.description_title"))
            self._desc_edit.setText(tr("rom_details.no_description"))
            return
        self._title_lbl.setText(str(rom.get("title", "—") or "—"))
        self._console_lbl.setText(tr("rom_details.console", value=rom.get("console") or "—"))
        self._author_lbl.setText(tr("rom_details.author", value=rom.get("author") or "—") if rom.get("author") else "")
        size_val = rom.get("file_size") or rom.get("file_size_bytes")
        self._size_lbl.setText(tr("rom_details.size", value=size_val) if size_val else tr("rom_details.size_unknown"))
        self._source_lbl.setText(tr("rom_details.source", value=rom.get("source") or "—"))
        self._region_lbl.setText(tr("rom_details.region", value=rom.get("region") or "—") if rom.get("region") else "")
        self._lang_lbl.setText(tr("rom_details.language", value=rom.get("lang") or "—") if rom.get("lang") else "")
        date_val = rom.get("date") or ""
        self._date_lbl.setText(tr("rom_details.date", value=date_val) if date_val else "")
        self._desc_title_lbl.setText(tr("rom_details.description_title"))
        desc = rom.get("description")
        self._desc_edit.setText(str(desc) if desc and desc != "None" else tr("rom_details.no_description"))

    def select_rom(self, rom: dict):
        if not rom or not isinstance(rom, dict):
            self._rom = {}
            self._dl_btn.setEnabled(False)
            self._render()
            return
        self._rom = rom
        self._dl_btn.setEnabled(True)
        self._render()

    def _on_download(self):
        if self._rom:
            self.download_clicked.emit(self._rom)


def _make_smooth_scroll_area(parent=None):
    from qfluentwidgets import SmoothScrollArea
    from PyQt6.QtWidgets import QAbstractScrollArea
    scroll = SmoothScrollArea(parent) if parent is not None else SmoothScrollArea()
    try:
        scroll.setViewportUpdateMode(QAbstractScrollArea.ViewportUpdateMode.FullViewportUpdate)
    except AttributeError:
        pass
    try:
        scroll.setSingleStep(60)
    except AttributeError:
        pass
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
        screen = (owner.screen() if owner is not None and hasattr(owner, "screen") else None) or QGuiApplication.primaryScreen()
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
        self._selected_roms: dict = {}
        self._current_query = ""
        self._current_console = None
        self._current_variant = None
        self._last_variant_by_console: dict[str, str] = {}
        self._current_page = 0
        self._sort_field = "title"
        self._sort_dir = "ASC"
        self._toolbar_collapsed = False
        self._sync_thread = None
        self._sync_worker = None
        self._unloaded = False
        self._details = DetailsPanel()
        self._details.download_clicked.connect(self._on_download)
        self._init_ui()

    def _init_ui(self):
        main = QVBoxLayout(self)
        main.setContentsMargins(0, 0, 0, 0)
        main.setSpacing(0)

        self._progress = ProgressBar()
        self._progress.setVisible(False)
        self._progress.setMaximumHeight(2)
        main.addWidget(self._progress)

        self._progress_indeterminate = IndeterminateProgressBar(start=False)
        self._progress_indeterminate.setVisible(False)
        self._progress_indeterminate.setMaximumHeight(2)
        main.addWidget(self._progress_indeterminate)

        filter_widget = QWidget()
        filter_widget.setObjectName("filterBar")
        filter_widget.setStyleSheet("background: transparent;")
        filter_layout = QHBoxLayout(filter_widget)
        filter_layout.setContentsMargins(12, 8, 12, 8)
        filter_layout.setSpacing(8)

        self._search_input = SearchLineEdit()
        self._search_input.setFixedHeight(30)
        self._search_input.setClearButtonEnabled(True)
        self._search_input.setMinimumWidth(80)
        self._search_input.setMaximumWidth(300)
        self._search_input.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self._search_input.returnPressed.connect(self._on_search)
        filter_layout.addWidget(self._search_input)

        cv_group = QWidget()
        cv_group.setObjectName("consoleVariantGroup")
        cv_group.setStyleSheet("#consoleVariantGroup { border: none; background: transparent; }")
        cv_layout = QHBoxLayout(cv_group)
        cv_layout.setContentsMargins(0, 0, 0, 0)
        cv_layout.setSpacing(4)

        self._console_filter = ConsoleComboBox()
        self._console_filter.setFixedHeight(28)
        self._console_filter.setMinimumWidth(80)
        self._console_filter.currentIndexChanged.connect(self._on_console_change)
        cv_layout.addWidget(self._console_filter)

        separator = QFrame()
        separator.setFrameShape(QFrame.Shape.VLine)
        separator.setFrameShadow(QFrame.Shadow.Sunken)
        separator.setStyleSheet("color: palette(mid);")
        separator.setFixedWidth(1)
        cv_layout.addWidget(separator)

        self._variant_filter = ComboBox()
        self._variant_filter.setFixedHeight(28)
        self._variant_filter.setMinimumWidth(70)
        self._variant_filter.currentIndexChanged.connect(self._on_filter_change)
        self._variant_filter.setVisible(False)
        cv_layout.addWidget(self._variant_filter)
        filter_layout.addWidget(cv_group)

        self._sort_combo = ComboBox()
        self._sort_combo.setFixedHeight(28)
        self._sort_combo.setMinimumWidth(70)
        self._sort_combo.currentIndexChanged.connect(self._on_filter_change)
        filter_layout.addWidget(self._sort_combo)
        filter_layout.addStretch(1)

        self._source_filter = ComboBox()
        self._source_filter.setFixedHeight(28)
        self._source_filter.setMinimumWidth(70)
        self._source_filter.currentIndexChanged.connect(self._on_source_change)
        filter_layout.addWidget(self._source_filter)

        self._btn_download_selected = PushButton(FluentIcon.DOWNLOAD, "")
        self._btn_download_selected.setFixedHeight(28)
        self._btn_download_selected.setMinimumWidth(110)
        self._btn_download_selected.setEnabled(False)
        self._btn_download_selected.clicked.connect(self._on_download_selected)
        filter_layout.addWidget(self._btn_download_selected)

        self._sync_btn = PrimaryPushButton(FluentIcon.SYNC, "")
        self._sync_btn.setFixedHeight(28)
        self._sync_btn.setMinimumWidth(130)
        self._sync_btn.clicked.connect(self._on_sync)
        filter_layout.addWidget(self._sync_btn)

        self._filter_widget = filter_widget
        self._filter_widget.setMinimumHeight(48)
        self._filter_widget.installEventFilter(self)
        main.addWidget(filter_widget, 0)

        content_splitter = QWidget()
        content_splitter.setStyleSheet("background: transparent;")
        content_main = QVBoxLayout(content_splitter)
        content_main.setContentsMargins(0, 0, 0, 0)
        content_main.setSpacing(0)

        self._splitter = QSplitter(Qt.Orientation.Horizontal)
        self._splitter.setHandleWidth(3)

        left_panel = QWidget()
        left_panel.setStyleSheet("background: transparent;")
        left_layout = QVBoxLayout(left_panel)
        left_layout.setContentsMargins(12, 6, 12, 6)
        left_layout.setSpacing(3)

        pag_row = QHBoxLayout()
        pag_row.setSpacing(6)

        self._btn_page_prev = PushButton("◀")
        self._btn_page_prev.setFixedSize(28, 24)
        self._btn_page_prev.setEnabled(False)
        self._btn_page_prev.clicked.connect(self._on_page_prev)
        pag_row.addWidget(self._btn_page_prev)

        self._lbl_page = CaptionLabel("")
        pag_row.addWidget(self._lbl_page)

        self._page_input = LineEdit()
        self._page_input.setMaxLength(6)
        self._page_input.setMaximumWidth(40)
        self._page_input.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._page_input.returnPressed.connect(self._on_jump_page)
        pag_row.addWidget(self._page_input)

        self._btn_page_next = PushButton("▶")
        self._btn_page_next.setFixedSize(28, 24)
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
        self._cards_layout.setContentsMargins(0, 2, 0, 2)
        self._cards_layout.setSpacing(3)
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
        qconfig.themeChanged.connect(self._apply_list_surface_tint)
        self._apply_locale()
        register_locale_refresh(self, self._apply_locale)

    def _apply_locale(self):
        self._search_input.setPlaceholderText(tr("home.search_placeholder"))
        self._console_filter.setPlaceholderText(tr("home.console_placeholder"))
        self._variant_filter.setPlaceholderText(tr("home.variant_placeholder"))

        sort_selection = self._sort_combo.currentData()
        self._sort_combo.blockSignals(True)
        self._sort_combo.clear()
        self._sort_combo.addItem(tr("home.sort_name_az"), userData=("title", "ASC"))
        self._sort_combo.addItem(tr("home.sort_name_za"), userData=("title", "DESC"))
        self._sort_combo.addItem(tr("home.sort_source_az"), userData=("source", "ASC"))
        self._sort_combo.addItem(tr("home.sort_source_za"), userData=("source", "DESC"))
        idx = self._sort_combo.findData(sort_selection) if sort_selection else -1
        self._sort_combo.setCurrentIndex(idx if idx >= 0 else 0)
        self._sort_combo.blockSignals(False)

        current_source = self._source_filter.currentData()
        self._source_filter.blockSignals(True)
        self._source_filter.clear()
        self._source_filter.addItem(tr("home.all_sources"), userData=None)
        for source in _get_all_sources():
            self._source_filter.addItem(source, userData=source)
        idx = self._source_filter.findData(current_source) if current_source else 0
        self._source_filter.setCurrentIndex(idx if idx >= 0 else 0)
        self._source_filter.blockSignals(False)

        current_console = self._console_filter.currentData()
        self._console_filter.blockSignals(True)
        self._console_filter.clear()
        self._console_filter.addItem(tr("home.all_consoles"), userData=None)
        try:
            consoles = db.get_all_consoles(sources=self._get_active_sources())
        except Exception:
            consoles = []
        for console in consoles:
            self._console_filter.addItem(console, userData=console)
        idx = self._console_filter.findData(current_console) if current_console else 0
        self._console_filter.setCurrentIndex(idx if idx >= 0 else 0)
        self._console_filter.blockSignals(False)

        self._reload_variant_filter()
        self._update_download_selected_button()
        self._sync_btn.setText(tr("home.sync_database"))
        self._update_pagination_ui()

    def eventFilter(self, obj, event):
        if obj is self._filter_widget and event.type() == QEvent.Type.Resize:
            self._update_toolbar_layout()
        return super().eventFilter(obj, event)

    def _update_toolbar_layout(self):
        collapsed = self._filter_widget.width() < TOOLBAR_COLLAPSE_WIDTH
        if collapsed == self._toolbar_collapsed:
            return
        self._toolbar_collapsed = collapsed
        if collapsed:
            self._search_input.setMinimumWidth(60)
            self._search_input.setMaximumWidth(140)
            self._console_filter.setMinimumWidth(60)
            self._variant_filter.setMinimumWidth(50)
            self._sort_combo.setMinimumWidth(50)
            self._source_filter.setMinimumWidth(50)
        else:
            self._search_input.setMinimumWidth(80)
            self._search_input.setMaximumWidth(300)
            self._console_filter.setMinimumWidth(80)
            self._variant_filter.setMinimumWidth(70)
            self._sort_combo.setMinimumWidth(70)
            self._source_filter.setMinimumWidth(70)

    def _apply_list_surface_tint(self, *_args):
        c = palette()
        self._list_surface.setStyleSheet(
            f"QScrollArea#romListScroll {{ background-color: {c['list_bg']}; border: none; }}"
            f"QScrollArea#romListScroll > QWidget > QWidget {{ background-color: transparent; }}"
            f"#romListContainer {{ background-color: transparent; }}"
        )
        self._cards_container.setStyleSheet(
            f"RomCardWidget {{ background-color: {c['card_bg']}; border: 1px solid {c['card_border']}; "
            f"border-radius: 6px; padding: 0px; }} "
            f"RomCardWidget:hover {{ background-color: {c['card_hover']}; border: 1px solid {c['card_border']}; "
            f"border-radius: 6px; }}"
        )

    def _reload_source_filter(self):
        current = self._source_filter.currentData()
        self._source_filter.blockSignals(True)
        self._source_filter.clear()
        self._source_filter.addItem(tr("home.all_sources"), userData=None)
        sources = _get_all_sources()
        for source in sources:
            self._source_filter.addItem(source, userData=source)
        idx = self._source_filter.findData(current) if current else 0
        self._source_filter.setCurrentIndex(idx if idx >= 0 else 0)
        self._source_filter.blockSignals(False)

    def _get_active_sources(self) -> Optional[list]:
        source = self._source_filter.currentData()
        return [source] if source else None

    def _on_source_change(self):
        sources = self._get_active_sources()
        current_console = self._current_console
        try:
            consoles = db.get_all_consoles(sources=sources)
        except Exception:
            consoles = []
        self._console_filter.blockSignals(True)
        self._console_filter.clear()
        self._console_filter.addItem(tr("home.all_consoles"), userData=None)
        for console in consoles:
            self._console_filter.addItem(console, userData=console)
        idx = self._console_filter.findData(current_console) if current_console in consoles else 0
        self._console_filter.setCurrentIndex(idx if idx >= 0 else 0)
        self._console_filter.blockSignals(False)
        self._current_console = self._console_filter.currentData()
        self._reload_variant_filter()
        self._on_filter_change()

    def _load_cards(self):
        layout = self._cards_layout
        while layout.count():
            child = layout.takeAt(0)
            if child.widget():
                child.widget().deleteLater()
        sources = self._get_active_sources()
        try:
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
        except Exception:
            roms = []
        self._selected_roms.clear()
        self._update_download_selected_button()
        if roms:
            for rom in roms:
                card = RomCardWidget(rom)
                card.rom_selected.connect(self._on_card_selected)
                card.download_clicked.connect(self._on_download)
                card.selection_toggled.connect(self._on_card_selection_toggled)
                layout.insertWidget(layout.count() - 1, card)
        else:
            empty_lbl = CaptionLabel(tr("home.no_roms_found"))
            empty_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
            empty_lbl.setObjectName("emptyRomsLabel")
            empty_lbl.setStyleSheet(f"QLabel#emptyRomsLabel {{ padding: 16px; color: {palette()['muted']}; }}")
            layout.insertWidget(layout.count() - 1, empty_lbl)
        gc.collect()

    def _update_pagination_ui(self):
        sources = self._get_active_sources()
        try:
            total = db.count_roms(query=self._current_query, console=self._current_console, sources=sources, variant=self._current_variant)
        except Exception:
            total = 0
        total_pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
        self._btn_page_prev.setEnabled(self._current_page > 0)
        self._btn_page_next.setEnabled(self._current_page < total_pages - 1 and total_pages > 1)
        self._lbl_page.setText(tr("home.page_label", page=self._current_page + 1))

    def _sync_model(self):
        self._load_cards()
        self._update_pagination_ui()

    def _unload_cards(self) -> None:
        layout = self._cards_layout
        while layout.count():
            child = layout.takeAt(0)
            if child.widget():
                child.widget().deleteLater()
        gc.collect()

    def hideEvent(self, event):
        self._unload_cards()
        self._unloaded = True
        super().hideEvent(event)

    def showEvent(self, event):
        if self._unloaded:
            self._unloaded = False
            self._sync_model()
        super().showEvent(event)

    def _current_console_from_combo(self) -> Optional[str]:
        return self._console_filter.currentData()

    def _apply_common_filter_state(self):
        self._current_query = self._search_input.text().strip()
        self._current_console = self._current_console_from_combo()
        self._current_variant = self._active_variant_filter()
        sort_data = self._sort_combo.currentData() or ("title", "ASC")
        self._sort_field, self._sort_dir = sort_data

    def _on_search(self):
        self._apply_common_filter_state()
        self._current_page = 0
        self._sync_model()

    def _on_console_change(self):
        self._current_console = self._current_console_from_combo()
        self._reload_variant_filter()
        self._on_filter_change()

    def _reload_variant_filter(self):
        self._variant_filter.blockSignals(True)
        self._variant_filter.clear()
        console = self._current_console
        if not console:
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
        self._variant_filter.addItem(tr("home.all_variants"), userData=None)
        for variant in variants:
            self._variant_filter.addItem(variant, userData=variant)
        self._variant_filter.setVisible(True)
        remembered = self._last_variant_by_console.get(console)
        if remembered and remembered in variants:
            idx = self._variant_filter.findData(remembered)
            self._variant_filter.setCurrentIndex(idx if idx >= 0 else 0)
            self._current_variant = remembered
        else:
            self._variant_filter.setCurrentIndex(0)
            self._current_variant = None
        self._variant_filter.blockSignals(False)

    def _active_variant_filter(self) -> Optional[str]:
        if not self._variant_filter.isVisible():
            return None
        return self._variant_filter.currentData()

    def _on_filter_change(self):
        self._apply_common_filter_state()
        if self._current_console and self._current_variant:
            self._last_variant_by_console[self._current_console] = self._current_variant
        self._current_page = 0
        self._sync_model()

    @staticmethod
    def _rom_key(rom: dict) -> str:
        rid = rom.get("id")
        return str(rid) if rid is not None else f"{rom.get('title', '')}|{rom.get('console', '')}|{rom.get('torrent_file', '')}"

    def _on_card_selection_toggled(self, rom: dict, checked: bool):
        key = self._rom_key(rom)
        if checked:
            self._selected_roms[key] = rom
        else:
            self._selected_roms.pop(key, None)
        self._update_download_selected_button()

    def _update_download_selected_button(self):
        count = len(self._selected_roms)
        self._btn_download_selected.setText(tr("home.download_count", count=count) if count else tr("home.download"))
        self._btn_download_selected.setEnabled(count > 0)
        self._btn_download_selected.setToolTip(tr("home.download_selected_tooltip", count=count) if count else "")

    def _on_download_selected(self):
        roms = list(self._selected_roms.values())
        if not roms:
            return
        panel = self._get_download_panel()
        if panel is not None:
            panel.add_many_from_roms(roms)
        self._selected_roms.clear()
        self._update_download_selected_button()
        self._load_cards()

    def _get_download_panel(self):
        return getattr(self.window(), "download_page", None)

    def _on_card_selected(self, rom: dict):
        try:
            self._details.setVisible(True)
            QTimer.singleShot(0, lambda: self._splitter.setSizes([300, 550]))
            self._details.select_rom(rom)
        except Exception:
            logger.exception("Card selection error")

    def _on_download(self, rom: dict):
        try:
            panel = self._get_download_panel()
            if panel is not None:
                panel.add_from_rom(rom)
        except Exception:
            logger.exception("Download handler error")

    def _on_sync(self):
        if self._sync_thread and self._sync_thread.isRunning():
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

    def _on_sync_done(self) -> None:
        self._sync_btn.setEnabled(True)
        self._progress_indeterminate.stop()
        self._progress_indeterminate.setVisible(False)
        self._progress.setVisible(False)
        self._progress.setValue(0)
        try:
            self._load_cards()
            self._update_pagination_ui()
        except Exception:
            pass
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
        gc.collect()

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
        if page >= 0:
            self._current_page = page
            self._sync_model()


class SettingsPage(QWidget):
    settings_changed = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setStyleSheet("background: transparent;")
        self._update_thread = None
        self._update_worker = None
        self._download_thread = None
        self._download_worker = None
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
        layout.setContentsMargins(24, 16, 28, 20)
        layout.setSpacing(6)
        self._title_lbl = SubtitleLabel(tr("settings.title"))
        layout.addWidget(self._title_lbl)
        self._subtitle_lbl = CaptionLabel(tr("settings.subtitle"))
        layout.addWidget(self._subtitle_lbl)
        layout.addSpacing(16)

        self._app_group = SettingCardGroup(tr("settings.group_appearance"), self)
        self._theme_combo = ComboBox()
        self._theme_combo.addItems([tr("settings.theme_dark"), tr("settings.theme_light"), tr("settings.theme_auto")])
        self._theme_combo.setMinimumWidth(140)
        self._theme_combo.currentIndexChanged.connect(self._on_theme_changed)
        self._theme_card = SettingCard(FluentIcon.BRUSH, tr("settings.theme_mode_title"), tr("settings.theme_mode_content"), self)
        self._theme_card.hBoxLayout.addWidget(self._theme_combo, 0, Qt.AlignmentFlag.AlignRight)
        self._theme_card.hBoxLayout.addSpacing(4)
        self._app_group.addSettingCard(self._theme_card)
        self._lang_codes = list(available_languages().keys())
        self._lang_combo = ComboBox()
        self._lang_combo.addItems([available_languages()[code] for code in self._lang_codes])
        self._lang_combo.setMinimumWidth(140)
        self._lang_combo.currentIndexChanged.connect(self._on_language_changed)
        self._lang_card = SettingCard(FluentIcon.LANGUAGE, tr("settings.language_title"), tr("settings.language_content"), self)
        self._lang_card.hBoxLayout.addWidget(self._lang_combo, 0, Qt.AlignmentFlag.AlignRight)
        self._lang_card.hBoxLayout.addSpacing(4)
        self._app_group.addSettingCard(self._lang_card)
        from qfluentwidgets import ColorPickerButton
        self._accent_color_btn = ColorPickerButton(QColor("#00b7c3"), tr("settings.accent_color_title"), self, enableAlpha=False)
        self._accent_color_btn.colorChanged.connect(self._on_accent_color_changed)
        self._reset_accent_btn = PushButton(tr("settings.use_windows_accent"))
        self._reset_accent_btn.clicked.connect(self._on_reset_accent_color)
        self._accent_card = SettingCard(FluentIcon.PALETTE, tr("settings.accent_color_title"), tr("settings.accent_color_content"), self)
        self._accent_card.hBoxLayout.addWidget(self._reset_accent_btn, 0, Qt.AlignmentFlag.AlignRight)
        self._accent_card.hBoxLayout.addSpacing(8)
        self._accent_card.hBoxLayout.addWidget(self._accent_color_btn, 0, Qt.AlignmentFlag.AlignRight)
        self._accent_card.hBoxLayout.addSpacing(4)
        self._app_group.addSettingCard(self._accent_card)
        layout.addWidget(self._app_group)
        layout.addSpacing(16)

        self._dl_group = SettingCardGroup(tr("settings.group_download_dir"), self)
        self._txt_download_dir = LineEdit()
        self._txt_download_dir.setReadOnly(True)
        self._txt_download_dir.setMinimumWidth(220)
        self._dl_browse_btn = PushButton(tr("settings.browse"))
        self._dl_browse_btn.clicked.connect(self._browse_download_dir)
        self._dl_card = SettingCard(FluentIcon.FOLDER, tr("settings.download_folder_title"), tr("settings.download_folder_content"), self)
        self._dl_card.hBoxLayout.addWidget(self._txt_download_dir, 0, Qt.AlignmentFlag.AlignRight)
        self._dl_card.hBoxLayout.addSpacing(8)
        self._dl_card.hBoxLayout.addWidget(self._dl_browse_btn, 0, Qt.AlignmentFlag.AlignRight)
        self._dl_card.hBoxLayout.addSpacing(4)
        self._dl_group.addSettingCard(self._dl_card)
        self._chk_console_structure = SwitchSettingCard(icon=FluentIcon.IOT, title=tr("settings.console_structure_title"), content=tr("settings.console_structure_content"))
        self._connect_switch(self._chk_console_structure, self._on_console_structure_toggled)
        self._dl_group.addSettingCard(self._chk_console_structure)
        layout.addWidget(self._dl_group)
        layout.addSpacing(16)

        self._perf_group = SettingCardGroup(tr("settings.group_seeding_perf"), self)
        self._spin_seed = CompactSpinBox()
        self._spin_seed.setRange(0, 9999)
        self._spin_seed.setSuffix(tr("settings.suffix_min"))
        self._spin_seed.valueChanged.connect(self._on_seed_time_changed)
        self._seed_card = SettingCard(FluentIcon.HISTORY, tr("settings.seed_time_title"), tr("settings.seed_time_content"), self)
        self._seed_card.hBoxLayout.addWidget(self._spin_seed, 0, Qt.AlignmentFlag.AlignRight)
        self._seed_card.hBoxLayout.addSpacing(4)
        self._perf_group.addSettingCard(self._seed_card)
        self._spin_speed = CompactSpinBox()
        self._spin_speed.setRange(0, 100000)
        self._spin_speed.setSuffix(tr("settings.suffix_kbps"))
        self._spin_speed.valueChanged.connect(self._on_speed_limit_changed)
        self._speed_card = SettingCard(FluentIcon.SPEED_HIGH, tr("settings.download_limit_title"), tr("settings.download_limit_content"), self)
        self._speed_card.hBoxLayout.addWidget(self._spin_speed, 0, Qt.AlignmentFlag.AlignRight)
        self._speed_card.hBoxLayout.addSpacing(4)
        self._perf_group.addSettingCard(self._speed_card)
        self._spin_upload_speed = CompactSpinBox()
        self._spin_upload_speed.setRange(0, 100000)
        self._spin_upload_speed.setSuffix(tr("settings.suffix_kbps"))
        self._spin_upload_speed.valueChanged.connect(self._on_upload_speed_changed)
        self._upload_speed_card = SettingCard(FluentIcon.SPEED_HIGH, tr("settings.upload_limit_title"), tr("settings.upload_limit_content"), self)
        self._upload_speed_card.hBoxLayout.addWidget(self._spin_upload_speed, 0, Qt.AlignmentFlag.AlignRight)
        self._upload_speed_card.hBoxLayout.addSpacing(4)
        self._perf_group.addSettingCard(self._upload_speed_card)
        self._chk_auto = SwitchSettingCard(icon=FluentIcon.MEDIA, title=tr("settings.auto_download_title"), content=tr("settings.auto_download_content"))
        self._connect_switch(self._chk_auto, self._on_auto_download_toggled)
        self._perf_group.addSettingCard(self._chk_auto)
        self._chk_delete_torrent = SwitchSettingCard(icon=FluentIcon.DELETE, title=tr("settings.delete_torrent_title"), content=tr("settings.delete_torrent_content"))
        self._connect_switch(self._chk_delete_torrent, self._on_delete_torrent_toggled)
        self._perf_group.addSettingCard(self._chk_delete_torrent)
        self._chk_close_to_tray = SwitchSettingCard(icon=FluentIcon.MINIMIZE, title=tr("settings.close_to_tray_title"), content=tr("settings.close_to_tray_content"))
        self._connect_switch(self._chk_close_to_tray, self._on_close_to_tray_toggled)
        self._perf_group.addSettingCard(self._chk_close_to_tray)
        layout.addWidget(self._perf_group)
        layout.addSpacing(16)

        self._feature_group = SettingCardGroup(tr("settings.group_features"), self)
        self._chk_minerva = SwitchSettingCard(icon=FluentIcon.LIBRARY, title=tr("settings.minerva_title"), content=tr("settings.minerva_content"))
        self._connect_switch(self._chk_minerva, self._on_minerva_toggled)
        self._feature_group.addSettingCard(self._chk_minerva)
        self._chk_pc_games = SwitchSettingCard(icon=FluentIcon.GAME, title=tr("settings.pc_games_title"), content=tr("settings.pc_games_content"))
        self._connect_switch(self._chk_pc_games, self._on_pc_games_toggled)
        self._feature_group.addSettingCard(self._chk_pc_games)
        self._chk_local_dat = SwitchSettingCard(icon=FluentIcon.DOCUMENT, title=tr("settings.local_dat_title"), content=tr("settings.local_dat_content"))
        self._connect_switch(self._chk_local_dat, self._on_local_dat_toggled)
        self._feature_group.addSettingCard(self._chk_local_dat)
        self._chk_music = SwitchSettingCard(icon=FluentIcon.MUSIC, title=tr("settings.music_title"), content=tr("settings.music_content"))
        self._connect_switch(self._chk_music, self._on_music_toggled)
        self._feature_group.addSettingCard(self._chk_music)
        self._chk_books = SwitchSettingCard(icon=FluentIcon.BOOK_SHELF, title=tr("settings.books_title"), content=tr("settings.books_content"))
        self._connect_switch(self._chk_books, self._on_books_toggled)
        self._feature_group.addSettingCard(self._chk_books)
        layout.addWidget(self._feature_group)
        layout.addSpacing(16)

        self._adv_group = SettingCardGroup(tr("settings.group_advanced"), self)
        self._chk_admin_mode = SwitchSettingCard(icon=FluentIcon.DEVELOPER_TOOLS, title=tr("settings.admin_mode_title"), content=tr("settings.admin_mode_content"))
        self._connect_switch(self._chk_admin_mode, self._on_admin_mode_toggled)
        self._adv_group.addSettingCard(self._chk_admin_mode)
        from ..core.updater import __version__ as _app_version
        self._app_version = _app_version
        self._update_status_lbl = CaptionLabel("")
        self._btn_check_update = PushButton(tr("settings.check_for_updates"))
        self._btn_check_update.clicked.connect(self._on_check_update)
        self._update_card = SettingCard(FluentIcon.SYNC, tr("settings.software_updates_title"), tr("settings.current_version", version=_app_version), self)
        self._update_card.hBoxLayout.addWidget(self._update_status_lbl, 0, Qt.AlignmentFlag.AlignRight)
        self._update_card.hBoxLayout.addSpacing(8)
        self._update_card.hBoxLayout.addWidget(self._btn_check_update, 0, Qt.AlignmentFlag.AlignRight)
        self._update_card.hBoxLayout.addSpacing(4)
        self._adv_group.addSettingCard(self._update_card)
        layout.addWidget(self._adv_group)
        layout.addSpacing(16)

        layout.addStretch()
        self._populate()
        register_locale_refresh(self, self._apply_locale)

    @staticmethod
    def _connect_switch(card, slot):
        sig = getattr(card, "checkedChanged", None) or card.switchButton.checkedChanged
        sig.connect(slot)

    def _apply_locale(self, *_args) -> None:
        self._title_lbl.setText(tr("settings.title"))
        self._subtitle_lbl.setText(tr("settings.subtitle"))

        self._app_group.titleLabel.setText(tr("settings.group_appearance"))
        self._theme_card.setTitle(tr("settings.theme_mode_title"))
        self._theme_card.setContent(tr("settings.theme_mode_content"))
        theme_idx = self._theme_combo.currentIndex()
        self._theme_combo.blockSignals(True)
        self._theme_combo.clear()
        self._theme_combo.addItems([tr("settings.theme_dark"), tr("settings.theme_light"), tr("settings.theme_auto")])
        self._theme_combo.setCurrentIndex(theme_idx)
        self._theme_combo.blockSignals(False)
        self._lang_card.setTitle(tr("settings.language_title"))
        self._lang_card.setContent(tr("settings.language_content"))
        self._accent_color_btn.title = tr("settings.accent_color_title")
        self._reset_accent_btn.setText(tr("settings.use_windows_accent"))
        self._accent_card.setTitle(tr("settings.accent_color_title"))
        self._accent_card.setContent(tr("settings.accent_color_content"))

        self._dl_group.titleLabel.setText(tr("settings.group_download_dir"))
        self._dl_browse_btn.setText(tr("settings.browse"))
        self._dl_card.setTitle(tr("settings.download_folder_title"))
        self._dl_card.setContent(tr("settings.download_folder_content"))
        self._chk_console_structure.setTitle(tr("settings.console_structure_title"))
        self._chk_console_structure.setContent(tr("settings.console_structure_content"))

        self._perf_group.titleLabel.setText(tr("settings.group_seeding_perf"))
        self._spin_seed.setSuffix(tr("settings.suffix_min"))
        self._seed_card.setTitle(tr("settings.seed_time_title"))
        self._seed_card.setContent(tr("settings.seed_time_content"))
        self._spin_speed.setSuffix(tr("settings.suffix_kbps"))
        self._speed_card.setTitle(tr("settings.download_limit_title"))
        self._speed_card.setContent(tr("settings.download_limit_content"))
        self._spin_upload_speed.setSuffix(tr("settings.suffix_kbps"))
        self._upload_speed_card.setTitle(tr("settings.upload_limit_title"))
        self._upload_speed_card.setContent(tr("settings.upload_limit_content"))
        self._chk_auto.setTitle(tr("settings.auto_download_title"))
        self._chk_auto.setContent(tr("settings.auto_download_content"))
        self._chk_delete_torrent.setTitle(tr("settings.delete_torrent_title"))
        self._chk_delete_torrent.setContent(tr("settings.delete_torrent_content"))
        self._chk_close_to_tray.setTitle(tr("settings.close_to_tray_title"))
        self._chk_close_to_tray.setContent(tr("settings.close_to_tray_content"))

        self._feature_group.titleLabel.setText(tr("settings.group_features"))
        self._chk_minerva.setTitle(tr("settings.minerva_title"))
        self._chk_minerva.setContent(tr("settings.minerva_content"))
        self._chk_pc_games.setTitle(tr("settings.pc_games_title"))
        self._chk_pc_games.setContent(tr("settings.pc_games_content"))
        self._chk_local_dat.setTitle(tr("settings.local_dat_title"))
        self._chk_local_dat.setContent(tr("settings.local_dat_content"))
        self._chk_music.setTitle(tr("settings.music_title"))
        self._chk_music.setContent(tr("settings.music_content"))
        self._chk_books.setTitle(tr("settings.books_title"))
        self._chk_books.setContent(tr("settings.books_content"))

        self._adv_group.titleLabel.setText(tr("settings.group_advanced"))
        self._chk_admin_mode.setTitle(tr("settings.admin_mode_title"))
        self._chk_admin_mode.setContent(tr("settings.admin_mode_content"))
        self._btn_check_update.setText(tr("settings.check_for_updates"))
        self._update_card.setTitle(tr("settings.software_updates_title"))
        self._update_card.setContent(tr("settings.current_version", version=self._app_version))

    def _toggle_setting(self, attr: str, checked: bool):
        from ..core.config import settings as _s, apply_settings
        setattr(_s, attr, checked)
        apply_settings(**{attr: checked})
        save_settings(_s)
        self.settings_changed.emit()

    def _apply_setting(self, attr: str, value):
        from ..core.config import settings as _s, apply_settings
        setattr(_s, attr, value)
        apply_settings(**{attr: value})
        save_settings(_s)
        self.settings_changed.emit()

    def _on_minerva_toggled(self, checked: bool):
        self._toggle_setting("minerva_enabled", checked)

    def _on_pc_games_toggled(self, checked: bool):
        self._toggle_setting("pc_games_enabled", checked)

    def _on_local_dat_toggled(self, checked: bool):
        self._toggle_setting("local_dat_enabled", checked)

    def _on_music_toggled(self, checked: bool):
        self._toggle_setting("music_enabled", checked)

    def _on_books_toggled(self, checked: bool):
        self._toggle_setting("books_enabled", checked)

    def _on_auto_download_toggled(self, checked: bool):
        self._toggle_setting("auto_download", checked)

    def _on_delete_torrent_toggled(self, checked: bool):
        self._toggle_setting("delete_torrent_after", checked)

    def _on_close_to_tray_toggled(self, checked: bool):
        self._toggle_setting("close_to_tray", checked)

    def _on_console_structure_toggled(self, checked: bool):
        self._apply_setting("_console_structure", checked)

    def _on_seed_time_changed(self, value: int):
        self._apply_setting("seed_time", value)

    def _on_speed_limit_changed(self, value: int):
        self._apply_setting("speed_limit", value)

    def _on_upload_speed_changed(self, value: int):
        self._apply_setting("upload_speed_limit", value)

    def _on_theme_changed(self, index: int):
        from ..core.config import settings as _s, apply_settings, resolve_theme
        from qfluentwidgets import setTheme
        theme_modes = [ThemeMode.DARK, ThemeMode.LIGHT, ThemeMode.AUTO]
        if not (0 <= index < len(theme_modes)):
            return
        new_theme_mode = theme_modes[index]
        if new_theme_mode == _s.theme_mode:
            return
        _s.theme_mode = new_theme_mode
        apply_settings(theme_mode=new_theme_mode)
        save_settings(_s)
        resolve_theme(setTheme)
        self.settings_changed.emit()

    def _on_language_changed(self, index: int):
        if 0 <= index < len(self._lang_codes):
            set_language(self._lang_codes[index])
            self.settings_changed.emit()

    def _on_download_dir_changed(self, path: str):
        from ..core.config import paths
        self._apply_setting("download_dir", path or paths.download_root)

    def _on_check_update(self):
        self._btn_check_update.setEnabled(False)
        self._update_status_lbl.setText(tr("update.checking"))
        from ..core import updater as _updater

        class _CheckWorker(QObject):
            done = pyqtSignal(object)

            def run(self):
                try:
                    result = _updater.check_for_update()
                except Exception:
                    result = None
                self.done.emit(result)

        thread = QThread()
        worker = _CheckWorker()
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        entry = (thread, worker)
        _UPDATE_THREADS_IN_FLIGHT.add(entry)

        def _safe_done(result):
            try:
                self._on_update_check_done(result)
            except RuntimeError:
                pass

        worker.done.connect(_safe_done)
        worker.done.connect(thread.quit)
        thread.finished.connect(lambda: _UPDATE_THREADS_IN_FLIGHT.discard(entry))
        thread.finished.connect(worker.deleteLater)
        thread.finished.connect(thread.deleteLater)
        thread.start()

    def _on_update_check_done(self, result):
        self._btn_check_update.setEnabled(True)
        if result is None:
            self._update_status_lbl.setText(tr("update.up_to_date"))
            return
        self._update_status_lbl.setText(tr("update.available", tag=result["tag"]))
        box = MessageBoxBase(self.window())
        box_layout = QVBoxLayout()
        box_layout.addWidget(StrongBodyLabel(tr("update.available_title", tag=result["tag"])))
        notes = result.get("notes") or tr("update.no_notes")
        notes_lbl = BodyLabel(notes[:500])
        notes_lbl.setWordWrap(True)
        box_layout.addWidget(notes_lbl)
        box.viewLayout.addLayout(box_layout)
        box.yesButton.setText(tr("update.update_now"))
        box.cancelButton.setText(tr("update.later"))
        if box.exec():
            self._start_update_download(result)

    def _start_update_download(self, result):
        from ..core import updater as _updater
        self._update_status_lbl.setText(tr("update.downloading"))
        self._btn_check_update.setEnabled(False)

        class _DownloadWorker(QObject):
            progress = pyqtSignal(int, int)
            finished = pyqtSignal(str)
            error = pyqtSignal(str)

            def run(self):
                try:
                    path = _updater.download_update(result["download_url"], on_progress=lambda d, t: self.progress.emit(d, t))
                    self.finished.emit(path)
                except Exception as exc:
                    self.error.emit(str(exc))

        thread = QThread()
        worker = _DownloadWorker()
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        entry = (thread, worker)
        _UPDATE_THREADS_IN_FLIGHT.add(entry)

        def _safe(callback, *args):
            try:
                callback(*args)
            except RuntimeError:
                pass

        worker.progress.connect(lambda d, t: _safe(self._on_update_download_progress, d, t))
        worker.finished.connect(lambda path: _safe(self._on_update_download_finished, path))
        worker.error.connect(lambda msg: _safe(self._on_update_download_error, msg))
        worker.finished.connect(thread.quit)
        worker.error.connect(thread.quit)
        thread.finished.connect(lambda: _UPDATE_THREADS_IN_FLIGHT.discard(entry))
        thread.finished.connect(worker.deleteLater)
        thread.finished.connect(thread.deleteLater)
        thread.start()

    def _on_update_download_progress(self, downloaded: int, total: int):
        if total > 0:
            self._update_status_lbl.setText(tr("update.downloading_pct", percent=int(downloaded / total * 100)))

    def _on_update_download_finished(self, path: str):
        self._update_status_lbl.setText(tr("update.restarting"))
        from ..core import updater as _updater
        try:
            win = self.window()
            if hasattr(win, "_force_quit"):
                win._force_quit = True
            _updater.apply_update_and_restart(path)
        except Exception:
            self._update_status_lbl.setText(tr("update.failed"))
            self._btn_check_update.setEnabled(True)

    def _on_update_download_error(self, msg: str):
        self._update_status_lbl.setText(tr("update.download_failed"))
        self._btn_check_update.setEnabled(True)

    def _on_accent_color_changed(self, color: QColor):
        from qfluentwidgets import setThemeColor
        from ..core.config import settings as _s, apply_settings
        setThemeColor(color)
        hex_color = color.name(QColor.NameFormat.HexRgb)
        _s.accent_color = hex_color
        apply_settings(accent_color=hex_color)
        save_settings(_s)
        self.settings_changed.emit()

    def _on_reset_accent_color(self):
        from ..core.config import detect_windows_accent_color
        color = QColor(detect_windows_accent_color())
        self._accent_color_btn.setColor(color)
        self._on_accent_color_changed(color)

    def _on_admin_mode_toggled(self, checked: bool):
        from ..core.config import settings as _s, apply_settings, save_settings, relaunch_as_admin, is_admin
        _s.admin_mode = checked
        apply_settings(admin_mode=checked)
        save_settings(_s)
        if checked and not is_admin():
            box = MessageBoxBase(self.window())
            box_layout = QVBoxLayout()
            box_layout.addWidget(BodyLabel("Restart as administrator?"))
            box.viewLayout.addLayout(box_layout)
            box.yesButton.setText("Restart Now")
            box.cancelButton.setText("Later")
            if box.exec() and relaunch_as_admin():
                win = self.window()
                if hasattr(win, "_force_quit"):
                    win._force_quit = True
                win.close()
        self.settings_changed.emit()

    def _browse_download_dir(self):
        path = QFileDialog.getExistingDirectory(self, "Select Download Directory", self._txt_download_dir.text())
        if path:
            self._txt_download_dir.setText(path)
            self._on_download_dir_changed(path)

    def _populate(self):
        from ..core.config import settings as _s, paths
        self._txt_download_dir.setText(_s.download_dir or paths.download_root)
        self._chk_console_structure.setChecked(getattr(_s, "_console_structure", True))
        self._spin_seed.setValue(_s.seed_time)
        self._spin_speed.setValue(_s.speed_limit)
        self._spin_upload_speed.setValue(getattr(_s, "upload_speed_limit", 500))
        self._chk_auto.setChecked(getattr(_s, "auto_download", False))
        self._chk_delete_torrent.setChecked(getattr(_s, "delete_torrent_after", True))
        self._chk_minerva.setChecked(getattr(_s, "minerva_enabled", True))
        self._chk_pc_games.setChecked(getattr(_s, "pc_games_enabled", False))
        self._chk_local_dat.setChecked(getattr(_s, "local_dat_enabled", False))
        self._chk_music.setChecked(getattr(_s, "music_enabled", False))
        self._chk_books.setChecked(getattr(_s, "books_enabled", False))
        self._accent_color_btn.setColor(QColor(getattr(_s, "accent_color", "#00b7c3")))
        self._chk_close_to_tray.setChecked(getattr(_s, "close_to_tray", False))
        self._chk_admin_mode.setChecked(getattr(_s, "admin_mode", False))
        mode_map = {"Dark": 0, "Light": 1, "Auto": 2}
        self._theme_combo.setCurrentIndex(mode_map.get(_s.theme_mode, 0))
        lang_code = current_language()
        if lang_code in self._lang_codes:
            self._lang_combo.setCurrentIndex(self._lang_codes.index(lang_code))


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
        layout.setContentsMargins(24, 20, 28, 20)
        layout.setSpacing(16)
        self._title_lbl = SubtitleLabel("")
        header_row = QHBoxLayout()
        header_row.setSpacing(16)
        header_left = QVBoxLayout()
        header_left.setSpacing(4)
        header_left.addWidget(self._title_lbl)
        from ..core.updater import __version__ as _about_version
        self._about_version = _about_version
        self._version_lbl = BodyLabel("")
        header_left.addWidget(self._version_lbl)
        self._desc_lbl = BodyLabel("")
        self._desc_lbl.setWordWrap(True)
        header_left.addWidget(self._desc_lbl)
        header_row.addLayout(header_left, 1)
        self._about_banner = self._build_about_banner()
        header_row.addWidget(self._about_banner, 0, Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignRight)
        layout.addLayout(header_row)

        self._links_title_lbl = StrongBodyLabel("")
        layout.addWidget(self._links_title_lbl)
        self._github_card = SettingCard(FluentIcon.GITHUB, "", "", self)
        self._github_card.setFixedHeight(84)
        self._github_card.hBoxLayout.setContentsMargins(20, 12, 16, 12)
        self._github_btn = HyperlinkButton(self.GITHUB_URL, "")
        self._github_card.hBoxLayout.addWidget(self._github_btn, 0, Qt.AlignmentFlag.AlignRight)
        self._github_card.hBoxLayout.addSpacing(4)
        layout.addWidget(self._github_card)

        self._credits_title_lbl = StrongBodyLabel("")
        layout.addWidget(self._credits_title_lbl)
        author_card = CardWidget()
        author_card.setFixedHeight(88)
        author_layout = QHBoxLayout(author_card)
        author_layout.setContentsMargins(20, 12, 16, 12)
        author_layout.setSpacing(16)
        author_layout.setAlignment(Qt.AlignmentFlag.AlignVCenter)
        self._author_avatar = self._build_author_avatar()
        author_layout.addWidget(self._author_avatar, 0, Qt.AlignmentFlag.AlignVCenter)
        author_text_col = QVBoxLayout()
        author_text_col.setSpacing(4)
        author_text_col.setContentsMargins(0, 0, 0, 0)
        self._made_by_lbl = StrongBodyLabel("")
        self._dev_role_lbl = CaptionLabel("")
        author_text_col.addWidget(self._made_by_lbl)
        author_text_col.addWidget(self._dev_role_lbl)
        author_layout.addLayout(author_text_col)
        author_layout.addStretch(1)
        self._profile_btn = HyperlinkButton(self.AUTHOR_URL, "")
        author_layout.addWidget(self._profile_btn, 0, Qt.AlignmentFlag.AlignVCenter)
        self._thanks_card = SettingCard(FluentIcon.HEART, "", "", self)
        self._thanks_card.setFixedHeight(84)
        self._thanks_card.hBoxLayout.setContentsMargins(20, 12, 16, 12)
        self._thanks_btn = HyperlinkButton(self.LOGO_ARTIST_URL, self.LOGO_ARTIST_REDDIT_USER)
        self._thanks_card.hBoxLayout.addWidget(self._thanks_btn, 0, Qt.AlignmentFlag.AlignRight)
        self._thanks_card.hBoxLayout.addSpacing(4)
        layout.addWidget(author_card)
        layout.addSpacing(8)
        layout.addWidget(self._thanks_card)
        layout.addStretch()

        self._apply_locale()
        register_locale_refresh(self, self._apply_locale)

    def _apply_locale(self):
        self._title_lbl.setText(tr("about.title"))
        self._version_lbl.setText(tr("about.version", version=self._about_version))
        self._desc_lbl.setText(tr("about.description"))
        self._links_title_lbl.setText(tr("about.links_group"))
        self._github_card.setTitle(tr("about.source_code_title"))
        self._github_card.setContent(tr("about.source_code_content"))
        self._github_btn.setText(tr("about.open_github"))
        self._credits_title_lbl.setText(tr("about.credits_group"))
        self._made_by_lbl.setText(tr("about.made_by", name=self.AUTHOR_NAME))
        self._dev_role_lbl.setText(tr("about.developer_role"))
        self._profile_btn.setText(tr("about.profile"))
        self._thanks_card.setTitle(tr("about.special_thanks"))
        self._thanks_card.setContent(tr("about.logo_credit", name=self.LOGO_ARTIST_REDDIT_USER))

    def _build_about_banner(self) -> QWidget:
        from PyQt6.QtGui import QPixmap
        MAX_W, MAX_H = 400, 140
        banner_widget = QWidget()
        banner_widget.setMaximumSize(MAX_W, MAX_H)
        banner_widget.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Preferred)
        layout = QVBoxLayout(banner_widget)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)
        banner_path = None
        for name in ("banner.png", "banner.jpg", "banner.jpeg", "banner.svg"):
            candidate = os.path.join(_GUI_DIR, name)
            if os.path.isfile(candidate):
                banner_path = candidate
                break
        banner_label = QLabel()
        banner_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        if banner_path:
            pixmap = QPixmap(banner_path)
            if not pixmap.isNull():
                scaled = pixmap.scaled(MAX_W, MAX_H, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)
                banner_label.setFixedSize(scaled.size())
                banner_label.setPixmap(scaled)
                layout.addWidget(banner_label)
            else:
                banner_path = None
        if not banner_path:
            row = QHBoxLayout()
            row.setSpacing(12)
            row.addStretch(1)
            logo_path = find_logo_path()
            if logo_path:
                pixmap = QPixmap(logo_path)
                if not pixmap.isNull():
                    logo_size = min(80, MAX_H - 16)
                    logo_label = QLabel()
                    logo_label.setFixedSize(logo_size, logo_size)
                    logo_label.setScaledContents(True)
                    logo_label.setPixmap(pixmap.scaled(logo_size, logo_size, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation))
                    logo_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
                    row.addWidget(logo_label, 0, Qt.AlignmentFlag.AlignVCenter)
            text_col = QVBoxLayout()
            text_col.setSpacing(2)
            text_col.addWidget(SubtitleLabel("PiraChest"))
            text_col.addWidget(CaptionLabel("v1.0.0"))
            row.addLayout(text_col)
            row.addStretch(1)
            layout.addStretch(1)
            layout.addLayout(row)
            layout.addStretch(1)
        return banner_widget

    def _build_author_avatar(self) -> QWidget:
        from PyQt6.QtGui import QPixmap, QPainter, QPainterPath, QBrush, QColor as _QColor
        size = 48
        avatar = QLabel()
        avatar.setFixedSize(size, size)
        pixmap_path = None
        for name in ("author.png", "author.jpg", "author.jpeg", "author.ico", "author.svg"):
            candidate = os.path.join(_GUI_DIR, name)
            if os.path.isfile(candidate):
                pixmap_path = candidate
                break
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
                src = src.scaled(size, size, Qt.AspectRatioMode.KeepAspectRatioByExpanding, Qt.TransformationMode.SmoothTransformation)
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
            font.setPointSize(18)
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
        card.setMinimumWidth(380)
        card.setMaximumWidth(420)
        card_layout = QVBoxLayout(card)
        card_layout.setContentsMargins(28, 28, 28, 28)
        card_layout.setSpacing(10)
        icon_lbl = QLabel()
        try:
            icon_lbl.setPixmap(icon.icon().pixmap(40, 40))
        except Exception:
            pass
        icon_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        card_layout.addWidget(icon_lbl)
        title_lbl = SubtitleLabel(title)
        title_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        card_layout.addWidget(title_lbl)
        body_lbl = BodyLabel(message)
        body_lbl.setWordWrap(True)
        body_lbl.setMinimumWidth(300)
        body_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        card_layout.addWidget(body_lbl)
        status_lbl = CaptionLabel(tr("common.coming_soon"))
        status_lbl.setWordWrap(True)
        status_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        card_layout.addWidget(status_lbl)
        self._status_lbl = status_lbl
        register_locale_refresh(self, self._apply_locale)
        center_row = QHBoxLayout()
        center_row.addStretch(1)
        center_row.addWidget(card)
        center_row.addStretch(1)
        outer.addLayout(center_row)
        outer.addStretch(1)

    def _apply_locale(self):
        self._status_lbl.setText(tr("common.coming_soon"))



class AlphaDisclaimerDialog(MessageBoxBase):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumWidth(420)
        self.viewLayout.addWidget(SubtitleLabel("PiraChest is in Alpha"))
        body = BodyLabel("PiraChest is still in active alpha development.\nSome features may be incomplete or missing.")
        body.setWordWrap(True)
        self.viewLayout.addWidget(body)
        self.viewLayout.addSpacing(6)
        self._chk_never_show = CheckBox("Don't show this again")
        self.viewLayout.addWidget(self._chk_never_show)
        self.yesButton.setText("Got it")
        self.cancelButton.hide()

    def never_show_again(self) -> bool:
        return self._chk_never_show.isChecked()


_ONBOARDING_FEATURES = (
    ("minerva_enabled", FluentIcon.LIBRARY, "Minerva ROM Index", "Show the Home section with ROM browsing and sync"),
    ("pc_games_enabled", FluentIcon.GAME, "Repacks (PC Games)", "Show a PC Games section in the sidebar"),
    ("local_dat_enabled", FluentIcon.DOCUMENT, "Local DAT Support", "Show a Local DAT section in the sidebar (A Placeholder, coming in future updates.)"),
    ("music_enabled", FluentIcon.MUSIC, "Music", "Show a Music section in the sidebar (A Placeholder, coming in future updates.)"),
    ("books_enabled", FluentIcon.BOOK_SHELF, "Books", "Show a Books section in the sidebar (A Placeholder, coming in future updates.)"),
)


class _CompactFeatureRow(QWidget):
    __slots__ = ('switch',)

    def __init__(self, icon, title: str, desc: str, parent=None):
        super().__init__(parent)
        c = palette()
        self.setStyleSheet(f"_CompactFeatureRow {{ background-color: {c['card_bg']}; border: 1px solid {c['card_border']}; border-radius: 6px; }}")
        row = QHBoxLayout(self)
        row.setContentsMargins(8, 6, 8, 6)
        row.setSpacing(8)
        icon_w = IconWidget(icon, self)
        icon_w.setFixedSize(16, 16)
        row.addWidget(icon_w, 0, Qt.AlignmentFlag.AlignVCenter)
        text_col = QVBoxLayout()
        text_col.setSpacing(0)
        title_lbl = StrongBodyLabel(title)
        title_lbl.setStyleSheet(f"font-size: 12px; color: {c['primary_text']};")
        desc_lbl = CaptionLabel(desc)
        desc_lbl.setWordWrap(True)
        desc_lbl.setStyleSheet(f"color: {c['muted']}; font-size: 10px;")
        text_col.addWidget(title_lbl)
        text_col.addWidget(desc_lbl)
        row.addLayout(text_col, 1)
        self.switch = SwitchButton(self)
        self.switch.setChecked(False)
        row.addWidget(self.switch, 0, Qt.AlignmentFlag.AlignVCenter)


class FeatureOnboardingDialog(MessageBoxBase):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumWidth(480)
        self.widget.setMinimumWidth(480)
        self.viewLayout.addWidget(SubtitleLabel("Choose Your Features"))
        body = BodyLabel("Pick which sections you'd like to use. Everything is off by default.")
        body.setWordWrap(True)
        self.viewLayout.addWidget(body)
        self.viewLayout.addSpacing(4)
        rows_layout = QVBoxLayout()
        rows_layout.setSpacing(4)
        self._switches: dict[str, _CompactFeatureRow] = {}
        for key, icon, feat_title, desc in _ONBOARDING_FEATURES:
            row = _CompactFeatureRow(icon, feat_title, desc)
            self._switches[key] = row
            rows_layout.addWidget(row)
        self.viewLayout.addLayout(rows_layout)
        self.yesButton.setText("Continue")
        self.cancelButton.hide()

    def selections(self) -> dict:
        return {key: row.switch.isChecked() for key, row in self._switches.items()}


def _maybe_show_feature_onboarding(parent: "MainWindow") -> None:
    from ..core.config import settings as _s
    if getattr(_s, "onboarding_completed", False):
        return
    dialog = FeatureOnboardingDialog(parent)
    dialog.exec()
    for key, value in dialog.selections().items():
        setattr(_s, key, value)
    _s.onboarding_completed = True
    try:
        save_settings(_s)
    except Exception:
        pass
    parent._sync_optional_pages()
    if parent.settings_page is not None:
        parent.settings_page._populate()


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
            pass


class _IconSplashScreen(QWidget):
    def __init__(self, icon, parent):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        from qfluentwidgets import Theme as _FWTheme
        bg_color = "#1c1c1c" if qconfig.theme == _FWTheme.DARK else "#f3f3f3"
        self.setStyleSheet(f"background-color: {bg_color};")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        icon_label = QLabel(self)
        icon_label.setPixmap(icon.pixmap(120, 120))
        icon_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(icon_label, alignment=Qt.AlignmentFlag.AlignCenter)
        parent.installEventFilter(self)
        self.raise_()
        self.show()

    def eventFilter(self, obj, event):
        if obj is self.parent() and event.type() == QEvent.Type.Resize:
            self.resize(self.parent().size())
        return super().eventFilter(obj, event)

    def finish(self):
        self.parent().removeEventFilter(self)
        self.deleteLater()


class MainWindow(FluentWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("PiraChest")
        self.resize(1100, 740)
        self.setMinimumSize(900, 600)
        self._force_quit = False
        self._tray_icon = None
        self._downloads_badge = None

        resolve_theme(setTheme)
        try:
            from qfluentwidgets import setThemeColor
            from ..core.config import settings as _s
            setThemeColor(QColor(getattr(_s, "accent_color", "#00b7c3")))
        except Exception:
            pass

        from PyQt6.QtGui import QIcon
        from PyQt6.QtWidgets import QApplication

        logo_path = find_logo_path()
        if logo_path:
            self.setWindowIcon(QIcon(logo_path))

        self.splashScreen = _IconSplashScreen(self.windowIcon(), self)
        self.showMaximized()
        QApplication.processEvents()

        self.navigationInterface.setExpandWidth(180)
        self.navigationInterface.setCollapsible(False)

        self._init_tray_icon()

        qconfig.themeChanged.connect(self._apply_content_surface_tint)
        qconfig.themeChanged.connect(self._on_global_theme_changed)

        self.home_page: Optional[HomePage] = None
        self.pc_games_page: Optional[QWidget] = None
        self.local_dat_page: Optional[QWidget] = None
        self.music_page: Optional[QWidget] = None
        self.books_page: Optional[QWidget] = None

        from ..core.download_manager import DLState, DownloadManager
        from .download_manager_panel import DownloadManagerPage

        self.download_manager = DownloadManager(self)

        from ..core.config import settings as _s_init
        if getattr(_s_init, "onboarding_completed", False):
            self._sync_optional_pages()

        self.download_page = DownloadManagerPage(self.download_manager, self)
        self.download_page.setObjectName("downloadPage")
        self._nav_downloads = self.addSubInterface(self.download_page, FluentIcon.DOWNLOAD, tr("nav.downloads"), position=NavigationItemPosition.BOTTOM)

        self.download_manager.item_added.connect(self._update_downloads_badge)
        self.download_manager.item_updated.connect(self._update_downloads_badge)
        self.download_manager.item_removed.connect(self._update_downloads_badge)
        self._update_downloads_badge()

        self.settings_page = SettingsPage(self)
        self.settings_page.setObjectName("settingsPage")
        self._nav_settings = self.addSubInterface(self.settings_page, FluentIcon.SETTING, tr("nav.settings"), position=NavigationItemPosition.BOTTOM)

        self.about_page = AboutPage(self)
        self.about_page.setObjectName("aboutPage")
        self._nav_about = self.addSubInterface(self.about_page, FluentIcon.INFO, tr("nav.about"), position=NavigationItemPosition.BOTTOM)

        register_locale_refresh(self, self._refresh_nav_text)

        self._apply_content_surface_tint()

        if self.home_page is not None:
            self.switchTo(self.home_page)

        self.settings_page.settings_changed.connect(self._on_settings_changed)
        self._load_filters()
        self._auto_sync()
        self.splashScreen.finish()

        _maybe_show_alpha_disclaimer(self)
        _maybe_show_feature_onboarding(self)
        gc.collect()

    def _update_downloads_badge(self, *_args) -> None:
        from ..core.download_manager import DLState
        active = sum(1 for item in self.download_manager.items_in_order() if item.state in (DLState.queued, DLState.downloading, DLState.verifying))
        if self._downloads_badge is not None:
            self._downloads_badge.deleteLater()
            self._downloads_badge = None
        if active <= 0:
            return
        nav_widget = self.navigationInterface.widget(self.download_page.objectName())
        if nav_widget is None:
            return
        self._downloads_badge = InfoBadge.attension(active, parent=self, target=nav_widget, position=InfoBadgePosition.NAVIGATION_ITEM)

    def _on_global_theme_changed(self, *_args) -> None:
        from PyQt6.QtWidgets import QApplication
        app = QApplication.instance()
        if app is not None:
            _force_full_repolish(app)

    def _apply_content_surface_tint(self, *_args):
        style = settings_qss()
        self.settings_page.setStyleSheet(style)
        self.about_page.setStyleSheet(style)

    def _init_tray_icon(self):
        from PyQt6.QtWidgets import QSystemTrayIcon, QMenu
        from PyQt6.QtGui import QAction
        if not QSystemTrayIcon.isSystemTrayAvailable():
            return
        self._tray_icon = QSystemTrayIcon(self.windowIcon(), self)
        self._tray_icon.setToolTip("PiraChest")
        menu = QMenu()
        show_action = QAction("Show", self)
        show_action.triggered.connect(self._restore_from_tray)
        menu.addAction(show_action)
        menu.addSeparator()
        exit_action = QAction("Exit", self)
        exit_action.triggered.connect(self._quit_from_tray)
        menu.addAction(exit_action)
        self._tray_icon.setContextMenu(menu)
        self._tray_icon.activated.connect(self._on_tray_activated)
        self._tray_icon.show()

    def _on_tray_activated(self, reason):
        from PyQt6.QtWidgets import QSystemTrayIcon
        if reason in (QSystemTrayIcon.ActivationReason.Trigger, QSystemTrayIcon.ActivationReason.DoubleClick):
            self._restore_from_tray()

    def _restore_from_tray(self):
        self.showNormal() if not self.isMaximized() else self.showMaximized()
        self.raise_()
        self.activateWindow()

    def _quit_from_tray(self):
        self._force_quit = True
        self.close()

    def closeEvent(self, event):
        from ..core.config import settings as _s
        close_to_tray = getattr(_s, "close_to_tray", False)
        if close_to_tray and not self._force_quit and self._tray_icon is not None:
            event.ignore()
            self.hide()
            return
        try:
            self.download_manager.shutdown()
        except Exception:
            pass
        try:
            from ..core.repacks import cache as repack_cache
            repack_cache.clear_all_cache()
        except Exception:
            pass
        if self._tray_icon is not None:
            self._tray_icon.hide()
        super().closeEvent(event)
        from PyQt6.QtWidgets import QApplication
        QApplication.quit()

    def _on_settings_changed(self):
        try:
            self._sync_optional_pages()
        except Exception:
            pass
        try:
            if self.home_page:
                self.home_page._sync_model()
        except Exception:
            pass
        try:
            from ..core.config import settings as _s
            self.download_manager.set_global_limits(down_kbps=_s.speed_limit, up_kbps=getattr(_s, "upload_speed_limit", 500))
        except Exception:
            pass
        try:
            from ..core.config import settings as _s
            seed_minutes = getattr(_s, "seed_time", 0)
            for item_id in list(getattr(self.download_manager, "_items", {}).keys()):
                try:
                    self.download_manager.set_torrent_settings(item_id, seed_time_limit_min=seed_minutes)
                except Exception:
                    pass
        except Exception:
            pass

    def _remove_subinterface(self, page: Optional[QWidget]) -> None:
        if page is None:
            return
        try:
            self.navigationInterface.removeWidget(page.objectName())
        except Exception:
            pass
        try:
            self.stackedWidget.removeWidget(page)
        except Exception:
            pass
        page.deleteLater()

    def _sync_optional_pages(self) -> None:
        from ..core.config import settings as _s
        minerva_enabled = getattr(_s, "minerva_enabled", True)
        pc_enabled = getattr(_s, "pc_games_enabled", False)
        dat_enabled = getattr(_s, "local_dat_enabled", False)
        music_enabled = getattr(_s, "music_enabled", False)
        books_enabled = getattr(_s, "books_enabled", False)

        if minerva_enabled and self.home_page is None:
            self.home_page = HomePage(self)
            self.home_page.setObjectName("minervaPage")
            self._nav_minerva = self.addSubInterface(self.home_page, FluentIcon.LIBRARY, tr("nav.minerva"), position=NavigationItemPosition.TOP)
        elif not minerva_enabled and self.home_page is not None:
            self._remove_subinterface(self.home_page)
            self.home_page = None
            self._nav_minerva = None

        if pc_enabled and self.pc_games_page is None:
            self.pc_games_page = _LazyRepacksPage(self.download_manager)
            self.pc_games_page.setObjectName("pcGamesPage")
            self._nav_pc_games = self.addSubInterface(self.pc_games_page, FluentIcon.GAME, tr("nav.pc_games"), position=NavigationItemPosition.TOP)
        elif not pc_enabled and self.pc_games_page is not None:
            self._remove_subinterface(self.pc_games_page)
            self.pc_games_page = None
            self._nav_pc_games = None

        if dat_enabled and self.local_dat_page is None:
            self.local_dat_page = _LazyPage(lambda: PlaceholderPage(FluentIcon.DOCUMENT, tr("nav.local_dat_title"), tr("nav.local_dat_message")))
            self.local_dat_page.setObjectName("localDatPage")
            self._nav_local_dat = self.addSubInterface(self.local_dat_page, FluentIcon.DOCUMENT, tr("nav.local_dat"), position=NavigationItemPosition.TOP)
        elif not dat_enabled and self.local_dat_page is not None:
            self._remove_subinterface(self.local_dat_page)
            self.local_dat_page = None
            self._nav_local_dat = None

        if music_enabled and self.music_page is None:
            self.music_page = _LazyPage(lambda: PlaceholderPage(FluentIcon.MUSIC, tr("nav.music_title"), tr("nav.music_message")))
            self.music_page.setObjectName("musicPage")
            self._nav_music = self.addSubInterface(self.music_page, FluentIcon.MUSIC, tr("nav.music"), position=NavigationItemPosition.TOP)
        elif not music_enabled and self.music_page is not None:
            self._remove_subinterface(self.music_page)
            self.music_page = None
            self._nav_music = None

        if books_enabled and self.books_page is None:
            self.books_page = _LazyPage(lambda: PlaceholderPage(FluentIcon.BOOK_SHELF, tr("nav.books_title"), tr("nav.books_message")))
            self.books_page.setObjectName("booksPage")
            self._nav_books = self.addSubInterface(self.books_page, FluentIcon.BOOK_SHELF, tr("nav.books"), position=NavigationItemPosition.TOP)
        elif not books_enabled and self.books_page is not None:
            self._remove_subinterface(self.books_page)
            self.books_page = None
            self._nav_books = None
        gc.collect()

    def _refresh_nav_text(self) -> None:
        pairs = (
            (getattr(self, "_nav_downloads", None), "nav.downloads"),
            (getattr(self, "_nav_settings", None), "nav.settings"),
            (getattr(self, "_nav_about", None), "nav.about"),
            (getattr(self, "_nav_minerva", None), "nav.minerva"),
            (getattr(self, "_nav_pc_games", None), "nav.pc_games"),
            (getattr(self, "_nav_local_dat", None), "nav.local_dat"),
            (getattr(self, "_nav_music", None), "nav.music"),
            (getattr(self, "_nav_books", None), "nav.books"),
        )
        for widget, key in pairs:
            if widget is not None:
                try:
                    widget.setText(tr(key))
                except RuntimeError:
                    pass

    def _load_filters(self):
        if self.home_page is None:
            return
        consoles = _get_all_consoles()
        if consoles:
            self.home_page._console_filter.clear()
            self.home_page._console_filter.addItem("All Consoles")
            self.home_page._console_filter.addItems(consoles)
        else:
            self.home_page._console_filter.addItem("All Consoles")
        try:
            self.home_page._reload_source_filter()
        except Exception:
            pass
        try:
            self.home_page._sync_model()
        except Exception:
            pass

    def _auto_sync(self):
        if self.home_page is None:
            return
        from ..core.config import settings as _s
        if getattr(_s, "sync_prompt_shown", False):
            return
        try:
            count = db.count_roms()
        except Exception:
            count = 0
        if count != 0:
            return
        _s.sync_prompt_shown = True
        try:
            save_settings(_s)
        except Exception:
            pass
        card = SyncPromptCard(self)
        card.sync_requested.connect(self.home_page._on_sync)
        card.show_anchored(self)


class SyncPromptCard(CardWidget):
    sync_requested = pyqtSignal()
    dismissed = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedWidth(360)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 16, 18, 16)
        layout.setSpacing(10)

        header = QHBoxLayout()
        header.setSpacing(10)
        icon = IconWidget(FluentIcon.INFO, self)
        icon.setFixedSize(20, 20)
        header.addWidget(icon, 0, Qt.AlignmentFlag.AlignVCenter)
        self._title_lbl = StrongBodyLabel("")
        header.addWidget(self._title_lbl, 1, Qt.AlignmentFlag.AlignVCenter)
        self._close_btn = TransparentToolButton(FluentIcon.CLOSE, self)
        self._close_btn.setFixedSize(26, 26)
        self._close_btn.setIconSize(QSize(11, 11))
        self._close_btn.clicked.connect(self._on_close)
        header.addWidget(self._close_btn, 0, Qt.AlignmentFlag.AlignVCenter)
        layout.addLayout(header)

        self._body_lbl = BodyLabel("")
        self._body_lbl.setWordWrap(True)
        layout.addWidget(self._body_lbl)

        btn_row = QHBoxLayout()
        btn_row.setSpacing(8)
        btn_row.addStretch(1)
        self._sync_btn = PrimaryPushButton("")
        self._sync_btn.setFixedHeight(30)
        self._sync_btn.clicked.connect(self._on_sync_clicked)
        btn_row.addWidget(self._sync_btn)
        layout.addLayout(btn_row)

        self._apply_locale()
        register_locale_refresh(self, self._apply_locale)

    def _apply_locale(self):
        self._title_lbl.setText(tr("rom_index.empty_title"))
        self._body_lbl.setText(tr("rom_index.empty_content"))
        self._sync_btn.setText(tr("rom_index.sync_now"))

    def _on_close(self):
        self.dismissed.emit()
        self._fade_out()

    def _on_sync_clicked(self):
        self.sync_requested.emit()
        self._fade_out()

    def _fade_out(self):
        effect = QGraphicsOpacityEffect(self)
        self.setGraphicsEffect(effect)
        anim = QPropertyAnimation(effect, b"opacity", self)
        anim.setDuration(160)
        anim.setStartValue(1.0)
        anim.setEndValue(0.0)
        anim.finished.connect(self.deleteLater)
        anim.start(QPropertyAnimation.DeletionPolicy.DeleteWhenStopped)
        self._fade_anim = anim

    def show_anchored(self, window: QWidget, margin: int = 20, top: int = 50) -> None:
        self.adjustSize()
        start_x = window.width() - self.width() - margin
        end_x = start_x
        self.move(start_x, top - 24)
        self.setWindowOpacity(0.0)
        self.show()
        self.raise_()
        pos_anim = QPropertyAnimation(self, b"pos", self)
        pos_anim.setDuration(220)
        pos_anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        pos_anim.setStartValue(QPoint(start_x, top - 24))
        pos_anim.setEndValue(QPoint(end_x, top))
        opacity_anim = QPropertyAnimation(self, b"windowOpacity", self)
        opacity_anim.setDuration(220)
        opacity_anim.setStartValue(0.0)
        opacity_anim.setEndValue(1.0)
        pos_anim.start(QPropertyAnimation.DeletionPolicy.DeleteWhenStopped)
        opacity_anim.start(QPropertyAnimation.DeletionPolicy.DeleteWhenStopped)
        self._entry_anims = (pos_anim, opacity_anim)


def _force_full_repolish(app) -> None:
    base_font = _build_app_font()
    app.setFont(base_font)
    try:
        from qfluentwidgets import setFont
        setFont(app, fontSize=base_font.pointSize())
    except Exception:
        pass
    for top_level in app.topLevelWidgets():
        top_level.style().unpolish(top_level)
        top_level.style().polish(top_level)
        top_level.update()


def _build_app_font() -> "QFont":
    from PyQt6.QtGui import QFont
    font = QFont()
    font.setFamilies(["Segoe UI Variable", "Segoe UI", "Inter", "Noto Sans", "Arial"])
    font.setPointSize(10)
    font.setStyleStrategy(QFont.StyleStrategy.PreferAntialias | QFont.StyleStrategy.PreferQuality)
    font.setHintingPreference(QFont.HintingPreference.PreferFullHinting)
    return font


def create_application(argv: Optional[list] = None):
    import sys
    from PyQt6.QtWidgets import QApplication
    from PyQt6.QtGui import QSurfaceFormat
    from qfluentwidgets import setFont

    surface_format = QSurfaceFormat()
    surface_format.setSwapInterval(1)
    surface_format.setSwapBehavior(QSurfaceFormat.SwapBehavior.TripleBuffer)
    QSurfaceFormat.setDefaultFormat(surface_format)

    app = QApplication(argv if argv is not None else sys.argv)
    app.setApplicationName("PiraChest")
    app.setApplicationDisplayName("PiraChest")
    app.setOrganizationName("PiraChest")
    app.setQuitOnLastWindowClosed(False)

    base_font = _build_app_font()
    app.setFont(base_font)
    setFont(app, fontSize=base_font.pointSize())
    return app
