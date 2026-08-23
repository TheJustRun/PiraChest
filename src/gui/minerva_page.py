from __future__ import annotations
import logging
from typing import Optional

from PySide6.QtCore import Qt, QEvent, QTimer, Signal
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import QHBoxLayout, QFrame, QVBoxLayout, QWidget, QSplitter, QSizePolicy
from qfluentwidgets import (
    BodyLabel, CardWidget, CheckBox, ComboBox, FluentIcon,
    IndeterminateProgressBar, LineEdit, PrimaryPushButton, PrimaryToolButton,
    ProgressBar, PushButton, SearchLineEdit, StrongBodyLabel, SubtitleLabel,
    qconfig, CaptionLabel,
)
from qfluentwidgets.components.widgets.combo_box import ComboBoxMenu

from src.core.minerva import database as db
from src.core.minerva import console_variants
from src.core.minerva import indexer
from src.core.worker import submit, wrap_callback
from src.core.theme import palette
from src.core.translations import tr, register_locale_refresh

logger = logging.getLogger(__name__)

PAGE_SIZE = 30
TOOLBAR_COLLAPSE_WIDTH = 700


def get_all_consoles() -> list[str]:
    try:
        return db.get_all_consoles()
    except Exception:
        return []


def get_all_sources() -> list[str]:
    try:
        return db.get_all_sources()
    except Exception:
        return []


def make_smooth_scroll_area(parent=None):
    from qfluentwidgets import SmoothScrollArea
    from PySide6.QtWidgets import QAbstractScrollArea
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


class RomCardWidget(CardWidget):
    rom_selected = Signal(dict)
    download_clicked = Signal(dict)
    selection_toggled = Signal(dict, bool)

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
        title_val = str(rom.get('title', '—') or '—')
        self._title_lbl = StrongBodyLabel(title_val)
        self._title_lbl.setWordWrap(False)
        self._title_lbl.setToolTip(title_val)
        layout.addWidget(self._title_lbl, stretch=1)
        self._sub_lbl = CaptionLabel('')
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
        parts = [str(p) for p in (self._rom.get('console', ''), self._rom.get('source', ''), self._rom.get('file_size', '')) if p]
        self._sub_lbl.setText('  •  '.join(parts) if parts else tr('home.unknown'))

    def set_rom(self, rom: dict):
        self._rom = rom
        title_val = str(rom.get('title', '—') or '—')
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
    download_clicked = Signal(dict)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._rom: dict = {}
        self.setMaximumWidth(360)
        self.setMinimumWidth(260)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(4)
        self._title_lbl = SubtitleLabel('')
        self._title_lbl.setWordWrap(True)
        layout.addWidget(self._title_lbl)
        self._console_lbl = CaptionLabel('')
        self._author_lbl = CaptionLabel('')
        self._size_lbl = CaptionLabel('')
        self._source_lbl = CaptionLabel('')
        self._region_lbl = CaptionLabel('')
        self._lang_lbl = CaptionLabel('')
        self._date_lbl = CaptionLabel('')
        for lbl in (self._console_lbl, self._author_lbl, self._size_lbl, self._source_lbl, self._region_lbl, self._lang_lbl, self._date_lbl):
            layout.addWidget(lbl)
        self._desc_title_lbl = StrongBodyLabel('')
        layout.addWidget(self._desc_title_lbl)
        self._desc_card = CardWidget()
        desc_layout = QVBoxLayout(self._desc_card)
        desc_layout.setContentsMargins(8, 8, 8, 8)
        self._desc_edit = BodyLabel('')
        self._desc_edit.setWordWrap(True)
        desc_layout.addWidget(self._desc_edit)
        self._desc_card.setMinimumHeight(60)
        self._desc_card.setMaximumHeight(200)
        layout.addWidget(self._desc_card)
        layout.addStretch()
        self._dl_btn = PrimaryPushButton(FluentIcon.DOWNLOAD, '')
        self._dl_btn.setEnabled(False)
        self._dl_btn.clicked.connect(self._on_download)
        layout.addWidget(self._dl_btn)
        self._apply_locale()
        register_locale_refresh(self, self._apply_locale)

    def _apply_locale(self):
        self._dl_btn.setText(tr('home.download_rom'))
        self._render()

    def _render(self):
        rom = self._rom
        if not rom:
            self._title_lbl.setText(tr('home.select_a_rom'))
            for lbl in (self._console_lbl, self._author_lbl, self._size_lbl, self._source_lbl, self._region_lbl, self._lang_lbl, self._date_lbl):
                lbl.setText('')
            self._desc_title_lbl.setText(tr('rom_details.description_title'))
            self._desc_edit.setText(tr('rom_details.no_description'))
            return
        self._title_lbl.setText(str(rom.get('title', '—') or '—'))
        self._console_lbl.setText(tr('rom_details.console', value=rom.get('console') or '—'))
        self._author_lbl.setText(tr('rom_details.author', value=rom.get('author') or '—') if rom.get('author') else '')
        size_val = rom.get('file_size') or rom.get('file_size_bytes')
        self._size_lbl.setText(tr('rom_details.size', value=size_val) if size_val else tr('rom_details.size_unknown'))
        self._source_lbl.setText(tr('rom_details.source', value=rom.get('source') or '—'))
        self._region_lbl.setText(tr('rom_details.region', value=rom.get('region') or '—') if rom.get('region') else '')
        self._lang_lbl.setText(tr('rom_details.language', value=rom.get('lang') or '—') if rom.get('lang') else '')
        date_val = rom.get('date') or ''
        self._date_lbl.setText(tr('rom_details.date', value=date_val) if date_val else '')
        self._desc_title_lbl.setText(tr('rom_details.description_title'))
        desc = rom.get('description')
        self._desc_edit.setText(str(desc) if desc and desc != 'None' else tr('rom_details.no_description'))

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
        screen = (owner.screen() if owner is not None and hasattr(owner, 'screen') else None) or QGuiApplication.primaryScreen()
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


class MinervaPage(QWidget):
    """ROM browser/search/sync page for the Minerva archive index.

    Fully self-contained: owns its own filter state, card list, sync
    progress, and DB access. MainWindow only needs to instantiate this,
    add it as a sub-interface, and optionally call the small set of public
    methods below (refresh_filters / sync_model / run_sync) when reacting
    to app-level events like settings changes or first-run auto-sync.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setStyleSheet('background: transparent;')
        self._selected_roms: dict = {}
        self._current_query = ''
        self._current_console = None
        self._current_variant = None
        self._last_variant_by_console: dict[str, str] = {}
        self._current_page = 0
        self._sort_field = 'title'
        self._sort_dir = 'ASC'
        self._toolbar_collapsed = False
        self._sync_task_id = None
        self._unloaded = False
        self._card_pool: list[RomCardWidget] = []
        self._empty_lbl = None
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
        filter_widget.setObjectName('filterBar')
        filter_widget.setStyleSheet('background: transparent;')
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
        cv_group.setObjectName('consoleVariantGroup')
        cv_group.setStyleSheet('#consoleVariantGroup { border: none; background: transparent; }')
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
        separator.setStyleSheet('color: palette(mid);')
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
        self._btn_download_selected = PushButton(FluentIcon.DOWNLOAD, '')
        self._btn_download_selected.setFixedHeight(28)
        self._btn_download_selected.setMinimumWidth(110)
        self._btn_download_selected.setEnabled(False)
        self._btn_download_selected.clicked.connect(self._on_download_selected)
        filter_layout.addWidget(self._btn_download_selected)
        self._sync_btn = PrimaryPushButton(FluentIcon.SYNC, '')
        self._sync_btn.setFixedHeight(28)
        self._sync_btn.setMinimumWidth(130)
        self._sync_btn.clicked.connect(self.run_sync)
        filter_layout.addWidget(self._sync_btn)
        self._filter_widget = filter_widget
        self._filter_widget.setMinimumHeight(48)
        self._filter_widget.installEventFilter(self)
        main.addWidget(filter_widget, 0)
        content_splitter = QWidget()
        content_splitter.setStyleSheet('background: transparent;')
        content_main = QVBoxLayout(content_splitter)
        content_main.setContentsMargins(0, 0, 0, 0)
        content_main.setSpacing(0)
        self._splitter = QSplitter(Qt.Orientation.Horizontal)
        self._splitter.setHandleWidth(3)
        left_panel = QWidget()
        left_panel.setStyleSheet('background: transparent;')
        left_layout = QVBoxLayout(left_panel)
        left_layout.setContentsMargins(12, 6, 12, 6)
        left_layout.setSpacing(3)
        pag_row = QHBoxLayout()
        pag_row.setSpacing(6)
        self._btn_page_prev = PushButton('◀')
        self._btn_page_prev.setFixedSize(28, 24)
        self._btn_page_prev.setEnabled(False)
        self._btn_page_prev.clicked.connect(self._on_page_prev)
        pag_row.addWidget(self._btn_page_prev)
        self._lbl_page = CaptionLabel('')
        pag_row.addWidget(self._lbl_page)
        self._page_input = LineEdit()
        self._page_input.setMaxLength(6)
        self._page_input.setMaximumWidth(40)
        self._page_input.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._page_input.returnPressed.connect(self._on_jump_page)
        pag_row.addWidget(self._page_input)
        self._btn_page_next = PushButton('▶')
        self._btn_page_next.setFixedSize(28, 24)
        self._btn_page_next.clicked.connect(self._on_page_next)
        pag_row.addWidget(self._btn_page_next)
        pag_row.addStretch()
        left_layout.addLayout(pag_row)
        scroll = make_smooth_scroll_area()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setStyleSheet('QScrollArea { background: transparent; border: none; }')
        scroll.viewport().setStyleSheet('background: transparent;')
        self._cards_container = QWidget()
        self._cards_container.setStyleSheet('background: transparent;')
        self._cards_layout = QVBoxLayout(self._cards_container)
        self._cards_layout.setContentsMargins(0, 2, 0, 2)
        self._cards_layout.setSpacing(3)
        self._cards_layout.addStretch()
        scroll.setWidget(self._cards_container)
        scroll.setObjectName('romListScroll')
        self._cards_container.setObjectName('romListContainer')
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
        self._search_input.setPlaceholderText(tr('home.search_placeholder'))
        self._console_filter.setPlaceholderText(tr('home.console_placeholder'))
        self._variant_filter.setPlaceholderText(tr('home.variant_placeholder'))
        sort_selection = self._sort_combo.currentData()
        self._sort_combo.blockSignals(True)
        self._sort_combo.clear()
        self._sort_combo.addItem(tr('home.sort_name_az'), userData=('title', 'ASC'))
        self._sort_combo.addItem(tr('home.sort_name_za'), userData=('title', 'DESC'))
        self._sort_combo.addItem(tr('home.sort_source_az'), userData=('source', 'ASC'))
        self._sort_combo.addItem(tr('home.sort_source_za'), userData=('source', 'DESC'))
        idx = self._sort_combo.findData(sort_selection) if sort_selection else -1
        self._sort_combo.setCurrentIndex(idx if idx >= 0 else 0)
        self._sort_combo.blockSignals(False)
        self._reload_source_filter()
        self._reload_console_filter()
        self._reload_variant_filter()
        self._update_download_selected_button()
        self._sync_btn.setText(tr('home.sync_database'))
        self._update_pagination_ui()

    def refresh_filters(self):
        """Public entry point to reload all filter dropdowns (console,
        source, variant) from the DB, e.g. after an external sync or when
        the page is first shown. Preserves the current selection where
        possible."""
        self._reload_console_filter()
        self._reload_source_filter()
        self._reload_variant_filter()

    def _reload_console_filter(self):
        current_console = self._console_filter.currentData()
        self._console_filter.blockSignals(True)
        self._console_filter.clear()
        self._console_filter.addItem(tr('home.all_consoles'), userData=None)
        try:
            consoles = db.get_all_consoles(sources=self._get_active_sources())
        except Exception:
            consoles = []
        for console in consoles:
            self._console_filter.addItem(console, userData=console)
        idx = self._console_filter.findData(current_console) if current_console else 0
        self._console_filter.setCurrentIndex(idx if idx >= 0 else 0)
        self._console_filter.blockSignals(False)
        self._current_console = self._console_filter.currentData()

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
        self._list_surface.setStyleSheet(f'QScrollArea#romListScroll {{ background-color: {c['list_bg']}; border: none; }}QScrollArea#romListScroll > QWidget > QWidget {{ background-color: transparent; }}#romListContainer {{ background-color: transparent; }}')
        self._cards_container.setStyleSheet(f'RomCardWidget {{ background-color: {c['card_bg']}; border: 1px solid {c['card_border']}; border-radius: 6px; padding: 0px; }} RomCardWidget:hover {{ background-color: {c['card_hover']}; border: 1px solid {c['card_border']}; border-radius: 6px; }}')

    def _reload_source_filter(self):
        current = self._source_filter.currentData()
        self._source_filter.blockSignals(True)
        self._source_filter.clear()
        self._source_filter.addItem(tr('home.all_sources'), userData=None)
        sources = get_all_sources()
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
        self._console_filter.addItem(tr('home.all_consoles'), userData=None)
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
        if getattr(self, '_empty_lbl', None) is not None:
            layout.removeWidget(self._empty_lbl)
            self._empty_lbl.deleteLater()
            self._empty_lbl = None
        sources = self._get_active_sources()
        try:
            roms = db.search_roms(query=self._current_query, console=self._current_console, sources=sources, variant=self._current_variant, offset=self._current_page * PAGE_SIZE, limit=PAGE_SIZE, sort_field=self._sort_field, sort_dir=self._sort_dir)
        except Exception:
            roms = []
        self._selected_roms.clear()
        self._update_download_selected_button()
        pool = self._card_pool
        needed = len(roms)
        for idx, rom in enumerate(roms):
            if idx < len(pool):
                card = pool[idx]
                card.set_rom(rom)
                card.setVisible(True)
            else:
                card = RomCardWidget(rom)
                card.rom_selected.connect(self._on_card_selected)
                card.download_clicked.connect(self._on_download)
                card.selection_toggled.connect(self._on_card_selection_toggled)
                pool.append(card)
                layout.insertWidget(layout.count() - 1, card)
        for idx in range(needed, len(pool)):
            pool[idx].setVisible(False)
        if len(pool) > PAGE_SIZE * 2:
            for card in pool[PAGE_SIZE:]:
                layout.removeWidget(card)
                card.setParent(None)
                card.deleteLater()
            del pool[PAGE_SIZE:]
        if not roms:
            empty_lbl = CaptionLabel(tr('home.no_roms_found'))
            empty_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
            empty_lbl.setObjectName('emptyRomsLabel')
            empty_lbl.setStyleSheet(f'QLabel#emptyRomsLabel {{ padding: 16px; color: {palette()['muted']}; }}')
            layout.insertWidget(layout.count() - 1, empty_lbl)
            self._empty_lbl = empty_lbl

    def _update_pagination_ui(self):
        sources = self._get_active_sources()
        try:
            total = db.count_roms(query=self._current_query, console=self._current_console, sources=sources, variant=self._current_variant)
        except Exception:
            total = 0
        total_pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
        self._btn_page_prev.setEnabled(self._current_page > 0)
        self._btn_page_next.setEnabled(self._current_page < total_pages - 1 and total_pages > 1)
        self._lbl_page.setText(tr('home.page_label', page=self._current_page + 1))

    def sync_model(self):
        """Reload the ROM card list and pagination for the current filter
        state. Public: safe to call from outside the page (e.g. MainWindow)
        after something external changed (settings, first-run sync)."""
        self._load_cards()
        self._update_pagination_ui()

    def _unload_cards(self) -> None:
        layout = self._cards_layout
        while layout.count():
            child = layout.takeAt(0)
            if child.widget():
                child.widget().deleteLater()
        self._card_pool.clear()
        if getattr(self, '_empty_lbl', None) is not None:
            self._empty_lbl = None

    def hideEvent(self, event):
        self._unload_cards()
        self._unloaded = True
        super().hideEvent(event)

    def showEvent(self, event):
        if self._unloaded:
            self._unloaded = False
            self.sync_model()
        super().showEvent(event)

    def _current_console_from_combo(self) -> Optional[str]:
        return self._console_filter.currentData()

    def _apply_common_filter_state(self):
        self._current_query = self._search_input.text().strip()
        self._current_console = self._current_console_from_combo()
        self._current_variant = self._active_variant_filter()
        sort_data = self._sort_combo.currentData() or ('title', 'ASC')
        self._sort_field, self._sort_dir = sort_data

    def _on_search(self):
        self._apply_common_filter_state()
        self._current_page = 0
        self.sync_model()

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
        self._variant_filter.addItem(tr('home.all_variants'), userData=None)
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
        self.sync_model()

    @staticmethod
    def _rom_key(rom: dict) -> str:
        rid = rom.get('id')
        return str(rid) if rid is not None else f'{rom.get('title', '')}|{rom.get('console', '')}|{rom.get('torrent_file', '')}'

    def _on_card_selection_toggled(self, rom: dict, checked: bool):
        key = self._rom_key(rom)
        if checked:
            self._selected_roms[key] = rom
        else:
            self._selected_roms.pop(key, None)
        self._update_download_selected_button()

    def _update_download_selected_button(self):
        count = len(self._selected_roms)
        self._btn_download_selected.setText(tr('home.download_count', count=count) if count else tr('home.download'))
        self._btn_download_selected.setEnabled(count > 0)
        self._btn_download_selected.setToolTip(tr('home.download_selected_tooltip', count=count) if count else '')

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
        return getattr(self.window(), 'download_page', None)

    def _on_card_selected(self, rom: dict):
        try:
            self._details.setVisible(True)
            QTimer.singleShot(0, lambda: self._splitter.setSizes([300, 550]))
            self._details.select_rom(rom)
        except Exception:
            logger.exception('Card selection error')

    def _on_download(self, rom: dict):
        try:
            panel = self._get_download_panel()
            if panel is not None:
                panel.add_from_rom(rom)
        except Exception:
            logger.exception('Download handler error')

    def run_sync(self):
        """Public entry point to kick off a Minerva index sync. Safe to
        call repeatedly — a sync already in progress is a no-op."""
        if self._sync_task_id is not None:
            return
        self._sync_btn.setEnabled(False)
        self._progress.setVisible(False)
        self._progress_indeterminate.setVisible(True)
        self._progress_indeterminate.start()
        on_progress = wrap_callback(self._on_sync_progress)
        self._sync_task_id = submit(
            indexer.sync_index,
            kwargs={'on_progress': on_progress},
            on_done=self._on_sync_done,
            on_error=self._on_sync_error,
        )

    def _on_sync_progress(self, processed: int) -> None:
        # sync_index reports a running row count with no known total ahead
        # of time, so keep the indeterminate bar but surface the count via
        # the sync button's tooltip rather than pretending we know a %.
        self._sync_btn.setToolTip(f'{processed:,} ROMs indexed so far…' if processed else '')

    def _on_sync_done(self, total: int) -> None:
        self._sync_task_id = None
        self._sync_btn.setEnabled(True)
        self._sync_btn.setToolTip('')
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
            self.refresh_filters()
        except Exception:
            pass

    def _on_sync_error(self, error: str) -> None:
        self._sync_task_id = None
        logger.error('Sync failed: %s', error)
        self._sync_btn.setEnabled(True)
        self._sync_btn.setToolTip('')
        self._progress_indeterminate.stop()
        self._progress_indeterminate.setVisible(False)
        self._progress.setVisible(False)
        self._progress.setValue(0)

    def _on_page_prev(self):
        if self._current_page > 0:
            self._current_page -= 1
            self.sync_model()

    def _on_page_next(self):
        sources = self._get_active_sources()
        try:
            total = db.count_roms(query=self._current_query, console=self._current_console, sources=sources, variant=self._current_variant)
        except Exception:
            total = 0
        total_pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
        if self._current_page < total_pages - 1:
            self._current_page += 1
            self.sync_model()

    def _on_jump_page(self):
        try:
            page = int(self._page_input.text()) - 1
        except ValueError:
            return
        if page >= 0:
            self._current_page = page
            self.sync_model()
