from __future__ import annotations

import logging
from typing import Optional

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QFontMetrics, QGuiApplication, QKeySequence, QShortcut
from PySide6.QtWidgets import (QFrame, QHBoxLayout, QLabel, QSizePolicy, QSplitter, QStackedWidget,
                               QVBoxLayout, QWidget)
from qfluentwidgets import (BodyLabel, CardWidget, CaptionLabel, CheckBox, ComboBox, FluentIcon,
                            IconWidget, IndeterminateProgressBar, LineEdit, PrimaryPushButton,
                            PrimaryToolButton, PushButton, SearchLineEdit,
                            StrongBodyLabel, SubtitleLabel, ToolButton, TransparentToolButton)
from qfluentwidgets.components.widgets.combo_box import ComboBoxMenu

from src.core.minerva import console_variants
from src.core.minerva import database as db
from src.core.minerva import indexer
from src.core.theme import accent_icon, legible_text, palette, register_theme_refresh, themed_icon
from src.core.translations import register_locale_refresh, tr
from src.core.worker import submit, wrap_callback

logger = logging.getLogger(__name__)

PAGE_SIZE = 30
SEARCH_DEBOUNCE_MS = 350
DETAILS_WIDTH = 340


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


def rom_key(rom: dict) -> str:
    rid = rom.get('id')
    if rid is not None:
        return str(rid)
    return '{}|{}|{}'.format(rom.get('title', ''), rom.get('console', ''), rom.get('torrent_file', ''))


def _icon(name: str, fallback):
    return getattr(FluentIcon, name, fallback)


class _ElidedLabel(StrongBodyLabel):
    _full = ''

    def set_full_text(self, text: str) -> None:
        self._full = text
        self.setToolTip(text)
        self._elide()

    def _elide(self) -> None:
        shown = QFontMetrics(self.font()).elidedText(self._full, Qt.TextElideMode.ElideRight, max(0, self.width()))
        QLabel.setText(self, shown)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._elide()


def make_elided_label() -> _ElidedLabel:
    lbl = _ElidedLabel()
    lbl.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
    lbl.setMinimumWidth(0)
    return lbl


class RomCardWidget(CardWidget):
    rom_selected = Signal(dict)
    download_clicked = Signal(dict)
    selection_toggled = Signal(dict, bool)

    def __init__(self, rom: dict, parent=None):
        super().__init__(parent)
        self._rom = rom
        self.setFixedHeight(60)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setProperty('active', False)
        row = QHBoxLayout(self)
        row.setContentsMargins(12, 6, 10, 6)
        row.setSpacing(10)
        self._select_chk = CheckBox()
        self._select_chk.setFixedWidth(20)
        self._select_chk.toggled.connect(lambda checked: self.selection_toggled.emit(self._rom, checked))
        row.addWidget(self._select_chk, 0, Qt.AlignmentFlag.AlignVCenter)
        text = QVBoxLayout()
        text.setSpacing(1)
        text.setContentsMargins(0, 0, 0, 0)
        self._title_lbl = make_elided_label()
        self._sub_lbl = CaptionLabel('')
        self._sub_lbl.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self._sub_lbl.setMinimumWidth(0)
        text.addStretch(1)
        text.addWidget(self._title_lbl)
        text.addWidget(self._sub_lbl)
        text.addStretch(1)
        row.addLayout(text, 1)
        self._dl_btn = PrimaryToolButton(accent_icon('minerva.tile_download', FluentIcon.DOWNLOAD), self)
        self._dl_btn.setFixedSize(34, 34)
        self._dl_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._dl_btn.clicked.connect(lambda _=False: self.download_clicked.emit(self._rom))
        row.addWidget(self._dl_btn, 0, Qt.AlignmentFlag.AlignVCenter)
        self._render()
        register_locale_refresh(self, self._render)

    def refresh_theme(self) -> None:
        self._dl_btn.setIcon(accent_icon('minerva.tile_download', FluentIcon.DOWNLOAD))

    def _render(self) -> None:
        rom = self._rom
        self._title_lbl.set_full_text(str(rom.get('title', '—') or '—'))
        parts = [str(p) for p in (rom.get('console'), rom.get('source'), rom.get('file_size')) if p]
        self._sub_lbl.setText('  •  '.join(parts) if parts else tr('home.unknown'))

    @property
    def rom(self) -> dict:
        return self._rom

    def set_rom(self, rom: dict, checked: bool = False, active: bool = False) -> None:
        self._rom = rom
        self._render()
        self.set_checked(checked)
        self.set_active(active)

    def set_checked(self, checked: bool) -> None:
        self._select_chk.blockSignals(True)
        self._select_chk.setChecked(checked)
        self._select_chk.blockSignals(False)

    def set_active(self, active: bool) -> None:
        if bool(self.property('active')) == active:
            return
        self.setProperty('active', active)
        self.style().unpolish(self)
        self.style().polish(self)
        self.update()

    def mouseReleaseEvent(self, event):
        super().mouseReleaseEvent(event)
        if event.button() == Qt.MouseButton.LeftButton and self.rect().contains(event.position().toPoint()):
            self.rom_selected.emit(self._rom)


class DetailsPanel(QWidget):
    download_clicked = Signal(dict)
    closed = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._rom: dict = {}
        self.setMinimumWidth(280)
        self.setMaximumWidth(400)
        root = QVBoxLayout(self)
        root.setContentsMargins(12, 10, 12, 12)
        root.setSpacing(8)

        head = QHBoxLayout()
        head.setSpacing(4)
        self._title_lbl = SubtitleLabel('')
        self._title_lbl.setWordWrap(True)
        self._title_lbl.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        head.addWidget(self._title_lbl, 1, Qt.AlignmentFlag.AlignTop)
        self._close_btn = TransparentToolButton(_icon('CLOSE', FluentIcon.CANCEL), self)
        self._close_btn.setFixedSize(28, 28)
        self._close_btn.clicked.connect(self.closed.emit)
        head.addWidget(self._close_btn, 0, Qt.AlignmentFlag.AlignTop)
        root.addLayout(head)

        self._info_lbls = [CaptionLabel('') for _ in range(7)]
        for lbl in self._info_lbls:
            lbl.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            lbl.setWordWrap(True)
            root.addWidget(lbl)

        self._desc_title_lbl = StrongBodyLabel('')
        root.addSpacing(4)
        root.addWidget(self._desc_title_lbl)
        scroll = make_smooth_scroll_area()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setStyleSheet('QScrollArea { background: transparent; border: none; }')
        scroll.viewport().setStyleSheet('background: transparent;')
        body = QWidget()
        body.setStyleSheet('background: transparent;')
        body_l = QVBoxLayout(body)
        body_l.setContentsMargins(0, 0, 4, 0)
        self._desc_lbl = BodyLabel('')
        self._desc_lbl.setWordWrap(True)
        self._desc_lbl.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self._desc_lbl.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
        body_l.addWidget(self._desc_lbl)
        body_l.addStretch(1)
        scroll.setWidget(body)
        root.addWidget(scroll, 1)

        self._copy_btn = PushButton(_icon('COPY', FluentIcon.DOCUMENT), '')
        self._copy_btn.clicked.connect(self._copy_title)
        root.addWidget(self._copy_btn)
        self._dl_btn = PrimaryPushButton(accent_icon('minerva.detail_download', FluentIcon.DOWNLOAD), '')
        self._dl_btn.setEnabled(False)
        self._dl_btn.clicked.connect(self._on_download)
        root.addWidget(self._dl_btn)
        register_locale_refresh(self, self._apply_locale)
        register_theme_refresh(self, self._apply_theme)

    def _apply_theme(self, *_args) -> None:
        self._dl_btn.setIcon(accent_icon('minerva.detail_download', FluentIcon.DOWNLOAD))

    def _apply_locale(self) -> None:
        self._dl_btn.setText(tr('home.download_rom'))
        self._copy_btn.setText(tr('home.copy_title', default='Copy title'))
        self._close_btn.setToolTip(tr('home.close_details', default='Close details'))
        self._render()

    def _render(self) -> None:
        rom = self._rom
        self._desc_title_lbl.setText(tr('rom_details.description_title'))
        if not rom:
            self._title_lbl.setText(tr('home.select_a_rom'))
            for lbl in self._info_lbls:
                lbl.setText('')
                lbl.setVisible(False)
            self._desc_lbl.setText(tr('rom_details.no_description'))
            return
        self._title_lbl.setText(str(rom.get('title', '—') or '—'))
        size_val = rom.get('file_size') or rom.get('file_size_bytes')
        lines = [
            tr('rom_details.console', value=rom.get('console') or '—'),
            tr('rom_details.author', value=rom['author']) if rom.get('author') else '',
            tr('rom_details.size', value=size_val) if size_val else tr('rom_details.size_unknown'),
            tr('rom_details.source', value=rom.get('source') or '—'),
            tr('rom_details.region', value=rom['region']) if rom.get('region') else '',
            tr('rom_details.language', value=rom['lang']) if rom.get('lang') else '',
            tr('rom_details.date', value=rom['date']) if rom.get('date') else '',
        ]
        for lbl, text in zip(self._info_lbls, lines, strict=True):
            lbl.setText(text)
            lbl.setVisible(bool(text))
        desc = rom.get('description')
        self._desc_lbl.setText(str(desc) if desc and desc != 'None' else tr('rom_details.no_description'))

    def select_rom(self, rom: dict) -> None:
        self._rom = rom if isinstance(rom, dict) and rom else {}
        self._dl_btn.setEnabled(bool(self._rom))
        self._copy_btn.setEnabled(bool(self._rom))
        self._render()

    def _copy_title(self) -> None:
        if self._rom:
            QGuiApplication.clipboard().setText(str(self._rom.get('title', '') or ''))

    def _on_download(self) -> None:
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
        capped = self._capped_width()
        if self.view.width() > capped:
            size = self.view.size()
            size.setWidth(capped)
            self.view.setFixedSize(size)
        if self.width() > capped:
            size = self.size()
            size.setWidth(capped)
            self.setFixedSize(size)


class ConsoleComboBox(ComboBox):
    MAX_VISIBLE_ROWS = 10

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMaxVisibleItems(self.MAX_VISIBLE_ROWS)

    def _createComboMenu(self):
        return _BoundedComboBoxMenu(self)


class _EmptyState(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        lay = QVBoxLayout(self)
        lay.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lay.setSpacing(8)
        self.icon = IconWidget(FluentIcon.SEARCH, self)
        self.icon.setFixedSize(44, 44)
        self.title = StrongBodyLabel('')
        self.body = CaptionLabel('')
        self.body.setWordWrap(True)
        self.button = PrimaryPushButton('')
        self._handler = None
        self.button.clicked.connect(lambda: self._handler() if self._handler else None)
        for w in (self.title, self.body):
            w.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lay.addWidget(self.icon, 0, Qt.AlignmentFlag.AlignHCenter)
        lay.addWidget(self.title)
        lay.addWidget(self.body)
        lay.addSpacing(6)
        lay.addWidget(self.button, 0, Qt.AlignmentFlag.AlignHCenter)

    def show_state(self, icon, title: str, body: str, button_text: str, handler) -> None:
        self.icon.setIcon(icon)
        self.title.setText(title)
        self.body.setText(body)
        self.button.setText(button_text)
        self._handler = handler


class MinervaPage(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setStyleSheet('background: transparent;')
        self._selected_roms: dict[str, dict] = {}
        self._active_key: Optional[str] = None
        self._current_query = ''
        self._current_console: Optional[str] = None
        self._current_variant: Optional[str] = None
        self._last_variant_by_console: dict[str, str] = {}
        self._current_page = 0
        self._total = 0
        self._sort_field = 'title'
        self._sort_dir = 'ASC'
        self._sync_task_id = None
        self._sync_total = 0
        self._unloaded = False
        self._card_pool: list[RomCardWidget] = []
        self._details = DetailsPanel()
        self._details.download_clicked.connect(self._on_download)
        self._details.closed.connect(self._close_details)
        self._ui_ready = False
        self._pending_calls: list[tuple[str, tuple, dict]] = []
        self._search_timer = QTimer(self)
        self._search_timer.setSingleShot(True)
        self._search_timer.setInterval(SEARCH_DEBOUNCE_MS)
        self._search_timer.timeout.connect(self._on_search)
        self._status_timer = QTimer(self)
        self._status_timer.setSingleShot(True)
        self._status_timer.setInterval(6000)


    def _ensure_ui(self) -> None:
        if self._ui_ready:
            return
        self._ui_ready = True
        self._init_ui()
        pending, self._pending_calls = self._pending_calls, []
        if not pending:
            self.sync_model()
        for name, args, kwargs in pending:
            getattr(self, name)(*args, **kwargs)

    def _defer_until_ui_ready(self, method_name: str, *args, **kwargs) -> bool:
        if self._ui_ready:
            return False
        self._pending_calls.append((method_name, args, kwargs))
        return True

    def _init_ui(self) -> None:
        main = QVBoxLayout(self)
        main.setContentsMargins(0, 0, 0, 0)
        main.setSpacing(0)

        self._progress_indeterminate = IndeterminateProgressBar(start=False)
        self._progress_indeterminate.setVisible(False)
        self._progress_indeterminate.setMaximumHeight(2)
        main.addWidget(self._progress_indeterminate)


        bar = QWidget()
        bar.setStyleSheet('background: transparent;')
        bar_l = QVBoxLayout(bar)
        bar_l.setContentsMargins(12, 10, 12, 4)
        bar_l.setSpacing(8)

        row1 = QHBoxLayout()
        row1.setSpacing(8)
        self._search_input = SearchLineEdit()
        self._search_input.setFixedHeight(32)
        self._search_input.setClearButtonEnabled(True)
        self._search_input.setMinimumWidth(120)
        self._search_input.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self._search_input.returnPressed.connect(self._on_search_now)
        self._search_input.searchSignal.connect(lambda *_: self._on_search_now())
        self._search_input.textChanged.connect(lambda *_: self._search_timer.start())
        row1.addWidget(self._search_input, 3)
        self._source_filter = ComboBox()
        self._console_filter = ConsoleComboBox()
        self._variant_filter = ComboBox()
        self._sort_combo = ComboBox()
        for combo, stretch in ((self._source_filter, 1), (self._console_filter, 1), (self._variant_filter, 1), (self._sort_combo, 1)):
            combo.setFixedHeight(32)
            combo.setMinimumWidth(90)
            row1.addWidget(combo, stretch)
        self._source_filter.currentIndexChanged.connect(self._on_source_change)
        self._console_filter.currentIndexChanged.connect(self._on_console_change)
        self._variant_filter.currentIndexChanged.connect(self._on_filter_change)
        self._variant_filter.setVisible(False)
        self._sort_combo.currentIndexChanged.connect(self._on_filter_change)
        bar_l.addLayout(row1)

        row2 = QHBoxLayout()
        row2.setSpacing(8)
        self._summary_lbl = CaptionLabel('')
        row2.addWidget(self._summary_lbl)
        self._reset_btn = TransparentToolButton(_icon('CANCEL', FluentIcon.CLOSE), self)
        self._reset_btn.setFixedSize(26, 26)
        self._reset_btn.clicked.connect(self._reset_filters)
        self._reset_btn.setVisible(False)
        row2.addWidget(self._reset_btn)
        row2.addStretch(1)
        self._status_lbl = CaptionLabel('')
        row2.addWidget(self._status_lbl)
        self._btn_download_selected = PushButton(themed_icon('minerva.download_selected', FluentIcon.DOWNLOAD), '')
        self._btn_download_selected.setFixedHeight(30)
        self._btn_download_selected.setMinimumWidth(110)
        self._btn_download_selected.setEnabled(False)
        self._btn_download_selected.clicked.connect(self._on_download_selected)
        row2.addWidget(self._btn_download_selected)
        self._sync_btn = PrimaryPushButton(accent_icon('minerva.sync', FluentIcon.SYNC), '')
        self._sync_btn.setFixedHeight(30)
        self._sync_btn.setMinimumWidth(130)
        self._sync_btn.clicked.connect(self.run_sync)
        row2.addWidget(self._sync_btn)
        bar_l.addLayout(row2)
        main.addWidget(bar)


        self._splitter = QSplitter(Qt.Orientation.Horizontal)
        self._splitter.setHandleWidth(3)
        self._splitter.setChildrenCollapsible(False)
        left = QWidget()
        left.setStyleSheet('background: transparent;')
        left_l = QVBoxLayout(left)
        left_l.setContentsMargins(12, 2, 12, 8)
        left_l.setSpacing(6)

        sel_row = QHBoxLayout()
        sel_row.setSpacing(8)
        self._select_all_chk = CheckBox('')
        self._select_all_chk.clicked.connect(self._on_select_all_clicked)
        sel_row.addWidget(self._select_all_chk)
        self._selected_lbl = CaptionLabel('')
        sel_row.addWidget(self._selected_lbl)
        self._clear_sel_btn = TransparentToolButton(_icon('CLOSE', FluentIcon.CANCEL), self)
        self._clear_sel_btn.setFixedSize(24, 24)
        self._clear_sel_btn.clicked.connect(self._clear_selection)
        self._clear_sel_btn.setVisible(False)
        sel_row.addWidget(self._clear_sel_btn)
        sel_row.addStretch(1)
        left_l.addLayout(sel_row)

        self._stack = QStackedWidget()
        scroll = make_smooth_scroll_area()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.viewport().setStyleSheet('background: transparent;')
        scroll.setObjectName('romListScroll')
        self._cards_container = QWidget()
        self._cards_container.setObjectName('romListContainer')
        self._cards_layout = QVBoxLayout(self._cards_container)
        self._cards_layout.setContentsMargins(0, 2, 0, 2)
        self._cards_layout.setSpacing(4)
        self._cards_layout.addStretch()
        scroll.setWidget(self._cards_container)
        self._list_surface = scroll
        self._empty = _EmptyState()
        self._stack.addWidget(scroll)
        self._stack.addWidget(self._empty)
        left_l.addWidget(self._stack, 1)

        pag = QHBoxLayout()
        pag.setSpacing(4)
        pag.addStretch(1)
        self._btn_first = ToolButton(_icon('PAGE_LEFT', FluentIcon.LEFT_ARROW))
        self._btn_prev = ToolButton(FluentIcon.LEFT_ARROW)
        self._btn_next = ToolButton(FluentIcon.RIGHT_ARROW)
        self._btn_last = ToolButton(_icon('PAGE_RIGHT', FluentIcon.RIGHT_ARROW))
        for btn in (self._btn_first, self._btn_prev, self._btn_next, self._btn_last):
            btn.setFixedSize(30, 28)
        self._btn_first.clicked.connect(lambda: self._go_to(0))
        self._btn_prev.clicked.connect(lambda: self._go_to(self._current_page - 1))
        self._btn_next.clicked.connect(lambda: self._go_to(self._current_page + 1))
        self._btn_last.clicked.connect(lambda: self._go_to(self._total_pages() - 1))
        self._page_input = LineEdit()
        self._page_input.setMaxLength(6)
        self._page_input.setFixedWidth(52)
        self._page_input.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._page_input.returnPressed.connect(self._on_jump_page)
        self._page_total_lbl = CaptionLabel('')
        for w in (self._btn_first, self._btn_prev, self._page_input, self._page_total_lbl, self._btn_next, self._btn_last):
            pag.addWidget(w)
        pag.addStretch(1)
        left_l.addLayout(pag)

        self._splitter.addWidget(left)
        self._splitter.addWidget(self._details)
        self._splitter.setStretchFactor(0, 1)
        self._splitter.setStretchFactor(1, 0)
        self._details.setVisible(False)
        main.addWidget(self._splitter, 1)


        self._shortcuts = []
        for seq, handler in (('Ctrl+F', self._focus_search), ('Esc', self._on_escape),
                             ('Alt+Left', lambda: self._go_to(self._current_page - 1)),
                             ('Alt+Right', lambda: self._go_to(self._current_page + 1)),
                             ('Ctrl+D', self._on_download_selected)):
            sc = QShortcut(QKeySequence(seq), self)
            sc.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
            sc.activated.connect(handler)
            self._shortcuts.append(sc)

        register_theme_refresh(self, self._apply_theme)
        self._apply_locale()
        register_locale_refresh(self, self._apply_locale)


    def _apply_theme(self, *_args) -> None:
        if not self._ui_ready:
            return
        c = palette()
        accent = legible_text(c.get('accent', '#2f9bff'), 'card_bg', 3.0)
        self._sync_btn.setIcon(accent_icon('minerva.sync', FluentIcon.SYNC))
        for card in self._cards_container.findChildren(RomCardWidget):
            card.refresh_theme()
        self._list_surface.setStyleSheet(
            'QScrollArea#romListScroll {{ background-color: {bg}; border: none; border-radius: 8px; }}'
            'QScrollArea#romListScroll > QWidget > QWidget {{ background-color: transparent; }}'
            '#romListContainer {{ background-color: transparent; }}'.format(bg=c['list_bg']))
        self._cards_container.setStyleSheet(
            'RomCardWidget {{ background-color: {bg}; border: 1px solid {bd}; border-radius: 6px; }}'
            'RomCardWidget:hover {{ background-color: {hv}; }}'
            'RomCardWidget[active="true"] {{ border: 1px solid {ac}; }}'.format(
                bg=c['card_bg'], bd=c['card_border'], hv=c['card_hover'], ac=accent))
        self._summary_lbl.setStyleSheet('color: {};'.format(c['muted']))
        self._status_lbl.setStyleSheet('color: {};'.format(c['muted']))

    def _apply_locale(self) -> None:
        self._search_input.setPlaceholderText(tr('home.search_placeholder'))
        self._source_filter.setPlaceholderText(tr('home.source_placeholder'))
        self._console_filter.setPlaceholderText(tr('home.console_placeholder'))
        self._variant_filter.setPlaceholderText(tr('home.variant_placeholder'))
        sort_selection = self._sort_combo.currentData()
        self._sort_combo.blockSignals(True)
        self._sort_combo.clear()
        for key, data in (('home.sort_name_az', ('title', 'ASC')), ('home.sort_name_za', ('title', 'DESC')),
                          ('home.sort_source_az', ('source', 'ASC')), ('home.sort_source_za', ('source', 'DESC'))):
            self._sort_combo.addItem(tr(key), userData=data)
        idx = self._sort_combo.findData(sort_selection) if sort_selection else -1
        self._sort_combo.setCurrentIndex(idx if idx >= 0 else 0)
        self._sort_combo.blockSignals(False)
        self._reload_source_filter()
        self._reload_console_filter()
        self._reload_variant_filter()
        self._select_all_chk.setText(tr('home.select_page', default='Select all on this page'))
        self._reset_btn.setToolTip(tr('home.reset_filters', default='Reset filters'))
        self._clear_sel_btn.setToolTip(tr('home.clear_selection', default='Clear selection'))
        self._btn_first.setToolTip(tr('home.first_page', default='First page'))
        self._btn_prev.setToolTip(tr('home.prev_page', default='Previous page'))
        self._btn_next.setToolTip(tr('home.next_page', default='Next page'))
        self._btn_last.setToolTip(tr('home.last_page', default='Last page'))
        self._page_input.setToolTip(tr('home.page_jump_tooltip', default='Go to page'))

        rtl = self.isRightToLeft() or QGuiApplication.layoutDirection() == Qt.LayoutDirection.RightToLeft
        back, fwd = (FluentIcon.RIGHT_ARROW, FluentIcon.LEFT_ARROW) if rtl else (FluentIcon.LEFT_ARROW, FluentIcon.RIGHT_ARROW)
        first, last = (_icon('PAGE_RIGHT', back), _icon('PAGE_LEFT', fwd)) if rtl else (_icon('PAGE_LEFT', back), _icon('PAGE_RIGHT', fwd))
        self._btn_first.setIcon(first)
        self._btn_prev.setIcon(back)
        self._btn_next.setIcon(fwd)
        self._btn_last.setIcon(last)
        self._update_sync_button()
        self._update_selection_ui()
        self._update_pagination_ui()
        self._update_summary()


    def refresh_filters(self) -> None:
        if self._defer_until_ui_ready('refresh_filters'):
            return
        self._reload_source_filter()
        self._reload_console_filter()
        self._reload_variant_filter()

    def _get_active_sources(self) -> Optional[list]:
        source = self._source_filter.currentData()
        return [source] if source else None

    def _reload_source_filter(self) -> None:
        current = self._source_filter.currentData()
        self._source_filter.blockSignals(True)
        self._source_filter.clear()
        self._source_filter.addItem(tr('home.all_sources'), userData=None)
        for source in get_all_sources():
            self._source_filter.addItem(source, userData=source)
        idx = self._source_filter.findData(current) if current else 0
        self._source_filter.setCurrentIndex(idx if idx >= 0 else 0)
        self._source_filter.blockSignals(False)

    def _reload_console_filter(self) -> None:
        current = self._console_filter.currentData()
        self._console_filter.blockSignals(True)
        self._console_filter.clear()
        self._console_filter.addItem(tr('home.all_consoles'), userData=None)
        try:
            consoles = db.get_all_consoles(sources=self._get_active_sources())
        except Exception:
            consoles = []
        for console in consoles:
            self._console_filter.addItem(console, userData=console)
        idx = self._console_filter.findData(current) if current else 0
        self._console_filter.setCurrentIndex(idx if idx >= 0 else 0)
        self._console_filter.blockSignals(False)
        self._current_console = self._console_filter.currentData()

    def _reload_variant_filter(self) -> None:
        combo = self._variant_filter
        combo.blockSignals(True)
        combo.clear()
        console = self._current_console
        variants: list[str] = []
        if console:
            try:
                variants = list(console_variants.get_variants(console))
            except Exception:
                variants = []
        if len(variants) <= 1:
            combo.setVisible(False)
            self._current_variant = None
            combo.blockSignals(False)
            return
        combo.addItem(tr('home.all_variants'), userData=None)
        for variant in variants:
            combo.addItem(variant, userData=variant)
        combo.setVisible(True)
        remembered = self._last_variant_by_console.get(console)
        idx = combo.findData(remembered) if remembered in variants else 0
        combo.setCurrentIndex(idx if idx >= 0 else 0)
        self._current_variant = combo.currentData()
        combo.blockSignals(False)

    def _apply_filter_state(self) -> None:
        self._current_query = self._search_input.text().strip()
        self._current_console = self._console_filter.currentData()
        self._current_variant = self._variant_filter.currentData() if self._variant_filter.isVisible() else None
        self._sort_field, self._sort_dir = self._sort_combo.currentData() or ('title', 'ASC')

    def _filters_active(self) -> bool:
        return bool(self._current_query or self._current_console or self._current_variant or self._get_active_sources())

    def _on_search(self) -> None:
        if self._current_query == self._search_input.text().strip():
            return
        self._on_filter_change()

    def _on_search_now(self) -> None:
        self._search_timer.stop()
        self._on_search()

    def _on_source_change(self) -> None:
        self._reload_console_filter()
        self._reload_variant_filter()
        self._on_filter_change()

    def _on_console_change(self) -> None:
        self._current_console = self._console_filter.currentData()
        self._reload_variant_filter()
        self._on_filter_change()

    def _on_filter_change(self) -> None:
        self._apply_filter_state()
        if self._current_console and self._current_variant:
            self._last_variant_by_console[self._current_console] = self._current_variant
        self._current_page = 0
        self.sync_model()

    def _reset_filters(self) -> None:
        self._search_timer.stop()
        for w in (self._search_input, self._source_filter, self._console_filter, self._variant_filter):
            w.blockSignals(True)
        self._search_input.clear()
        self._source_filter.setCurrentIndex(0)
        self._console_filter.setCurrentIndex(0)
        for w in (self._search_input, self._source_filter, self._console_filter, self._variant_filter):
            w.blockSignals(False)
        self._reload_console_filter()
        self._reload_variant_filter()
        self._on_filter_change()


    def _total_pages(self) -> int:
        return max(1, (self._total + PAGE_SIZE - 1) // PAGE_SIZE)

    def _query(self, offset: int) -> list[dict]:
        try:
            return db.search_roms(query=self._current_query, console=self._current_console,
                                  sources=self._get_active_sources(), variant=self._current_variant,
                                  offset=offset, limit=PAGE_SIZE, sort_field=self._sort_field, sort_dir=self._sort_dir)
        except Exception:
            logger.exception('ROM query failed')
            return []

    def _count(self) -> int:
        try:
            return int(db.count_roms(query=self._current_query, console=self._current_console,
                                     sources=self._get_active_sources(), variant=self._current_variant))
        except Exception:
            logger.exception('ROM count failed')
            return 0

    def sync_model(self) -> None:
        if self._defer_until_ui_ready('sync_model'):
            return
        self._total = self._count()
        self._current_page = max(0, min(self._current_page, self._total_pages() - 1))
        roms = self._query(self._current_page * PAGE_SIZE)
        self._populate(roms)
        self._update_pagination_ui()
        self._update_summary()

    def _populate(self, roms: list[dict]) -> None:
        layout, pool = self._cards_layout, self._card_pool
        for idx, rom in enumerate(roms):
            key = rom_key(rom)
            if idx < len(pool):
                card = pool[idx]
            else:
                card = RomCardWidget(rom)
                card.rom_selected.connect(self._on_card_selected)
                card.download_clicked.connect(self._on_download)
                card.selection_toggled.connect(self._on_card_selection_toggled)
                pool.append(card)
                layout.insertWidget(layout.count() - 1, card)
            card.set_rom(rom, checked=key in self._selected_roms, active=key == self._active_key)
            card.setVisible(True)
        for card in pool[len(roms):]:
            card.setVisible(False)
        if len(pool) > PAGE_SIZE * 2:
            for card in pool[PAGE_SIZE:]:
                layout.removeWidget(card)
                card.deleteLater()
            del pool[PAGE_SIZE:]
        self._show_empty_if_needed(not roms)
        self._update_select_all_state()
        if roms:
            self._list_surface.verticalScrollBar().setValue(0)

    def _show_empty_if_needed(self, empty: bool) -> None:
        if not empty:
            self._stack.setCurrentIndex(0)
            return
        if self._filters_active():
            self._empty.show_state(FluentIcon.SEARCH, tr('home.no_match_title', default='No ROMs match your filters'),
                                   tr('home.no_match_body', default='Try a different search or reset the filters.'),
                                   tr('home.reset_filters', default='Reset filters'), self._reset_filters)
        else:
            self._empty.show_state(FluentIcon.SYNC, tr('home.empty_index_title', default='Your ROM index is empty'),
                                   tr('home.empty_index_body', default='Sync the database to fetch the latest ROM list.'),
                                   tr('home.sync_database'), self.run_sync)
        self._stack.setCurrentIndex(1)

    def _unload_cards(self) -> None:
        if not self._ui_ready:
            return
        layout = self._cards_layout
        while layout.count():
            child = layout.takeAt(0)
            if child.widget():
                child.widget().deleteLater()
        layout.addStretch()
        self._card_pool.clear()

    def hideEvent(self, event) -> None:
        self._search_timer.stop()
        self._unload_cards()
        self._unloaded = True
        super().hideEvent(event)

    def showEvent(self, event) -> None:
        if not self._ui_ready:
            self._ensure_ui()
            self._unloaded = False
        elif self._unloaded:
            self._unloaded = False
            self.sync_model()
        super().showEvent(event)


    def _update_summary(self) -> None:
        if not self._ui_ready:
            return
        if self._total:
            start = self._current_page * PAGE_SIZE + 1
            end = min(self._total, start + PAGE_SIZE - 1)
            self._summary_lbl.setText(tr('home.showing_range', default='{start}–{end} of {total}',
                                         start=f'{start:,}', end=f'{end:,}', total=f'{self._total:,}'))
        else:
            self._summary_lbl.setText(tr('home.no_roms_found'))
        self._reset_btn.setVisible(self._filters_active())

    def _update_pagination_ui(self) -> None:
        pages = self._total_pages()
        page = self._current_page
        self._btn_first.setEnabled(page > 0)
        self._btn_prev.setEnabled(page > 0)
        self._btn_next.setEnabled(page < pages - 1)
        self._btn_last.setEnabled(page < pages - 1)
        self._page_input.setText(str(page + 1))
        self._page_total_lbl.setText(tr('home.page_of_total', default='/ {pages}', pages=f'{pages:,}'))

    def _go_to(self, page: int) -> None:
        if not self._ui_ready:
            return
        page = max(0, min(page, self._total_pages() - 1))
        if page != self._current_page:
            self._current_page = page
            self.sync_model()

    def _on_jump_page(self) -> None:
        try:
            page = int(self._page_input.text()) - 1
        except ValueError:
            self._update_pagination_ui()
            return
        self._go_to(page)
        self._update_pagination_ui()


    def _on_card_selection_toggled(self, rom: dict, checked: bool) -> None:
        key = rom_key(rom)
        if checked:
            self._selected_roms[key] = rom
        else:
            self._selected_roms.pop(key, None)
        self._update_selection_ui()
        self._update_select_all_state()

    def _visible_cards(self) -> list[RomCardWidget]:
        return [c for c in self._card_pool if c.isVisibleTo(self)]

    def _on_select_all_clicked(self, checked: bool) -> None:
        for card in self._visible_cards():
            card.set_checked(checked)
            key = rom_key(card.rom)
            if checked:
                self._selected_roms[key] = card.rom
            else:
                self._selected_roms.pop(key, None)
        self._update_selection_ui()

    def _update_select_all_state(self) -> None:
        cards = self._visible_cards()
        all_on = bool(cards) and all(rom_key(c.rom) in self._selected_roms for c in cards)
        self._select_all_chk.blockSignals(True)
        self._select_all_chk.setChecked(all_on)
        self._select_all_chk.blockSignals(False)
        self._select_all_chk.setEnabled(bool(cards))

    def _clear_selection(self) -> None:
        self._selected_roms.clear()
        for card in self._card_pool:
            card.set_checked(False)
        self._update_selection_ui()
        self._update_select_all_state()

    def _update_selection_ui(self) -> None:
        if not self._ui_ready:
            return
        count = len(self._selected_roms)
        self._selected_lbl.setText(tr('home.selected_count', default='{count} selected', count=count) if count else '')
        self._clear_sel_btn.setVisible(count > 0)
        self._btn_download_selected.setText(tr('home.download_count', count=count) if count else tr('home.download'))
        self._btn_download_selected.setEnabled(count > 0)
        self._btn_download_selected.setToolTip(tr('home.download_selected_tooltip', count=count) if count else '')


    def open_rom(self, rom: dict) -> None:
        if not rom:
            return
        if self._defer_until_ui_ready('open_rom', dict(rom)):
            return
        self._on_card_selected(dict(rom))

    def _on_card_selected(self, rom: dict) -> None:
        try:
            self._active_key = rom_key(rom)
            self._details.select_rom(rom)
            if not self._details.isVisible():
                self._details.setVisible(True)
                width = max(self._splitter.width(), DETAILS_WIDTH * 2)
                QTimer.singleShot(0, lambda: self._splitter.setSizes([width - DETAILS_WIDTH, DETAILS_WIDTH]))
            for card in self._card_pool:
                card.set_active(card.isVisibleTo(self) and rom_key(card.rom) == self._active_key)
        except Exception:
            logger.exception('Card selection error')

    def _close_details(self) -> None:
        self._active_key = None
        self._details.select_rom({})
        self._details.setVisible(False)
        for card in self._card_pool:
            card.set_active(False)

    def _get_download_panel(self):
        return getattr(self.window(), 'download_page', None)

    def _on_download(self, rom: dict) -> None:
        try:
            panel = self._get_download_panel()
            if panel is not None:
                panel.add_from_rom(rom)
        except Exception:
            logger.exception('Download handler error')

    def _on_download_selected(self) -> None:
        roms = list(self._selected_roms.values())
        if not roms:
            return
        try:
            panel = self._get_download_panel()
            if panel is not None:
                panel.add_many_from_roms(roms)
        except Exception:
            logger.exception('Bulk download handler error')
            return
        self._clear_selection()


    def _focus_search(self) -> None:
        self._search_input.setFocus()
        self._search_input.selectAll()

    def _on_escape(self) -> None:
        if self._search_input.hasFocus() and self._search_input.text():
            self._search_input.clear()
        elif self._details.isVisible():
            self._close_details()
        elif self._selected_roms:
            self._clear_selection()


    def _update_sync_button(self) -> None:
        if not self._ui_ready:
            return
        syncing = self._sync_task_id is not None
        self._sync_btn.setText(tr('home.syncing', default='Syncing…') if syncing else tr('home.sync_database'))
        self._sync_btn.setEnabled(not syncing)

    def _set_status(self, text: str, transient: bool = False) -> None:
        self._status_lbl.setText(text)
        self._status_timer.stop()
        if transient and text:
            try:
                self._status_timer.timeout.disconnect()
            except (RuntimeError, TypeError):
                pass
            self._status_timer.timeout.connect(lambda: self._status_lbl.setText(''))
            self._status_timer.start()

    def _end_sync_ui(self) -> None:
        self._sync_task_id = None
        self._progress_indeterminate.stop()
        self._progress_indeterminate.setVisible(False)
        self._update_sync_button()

    def run_sync(self) -> None:
        if self._defer_until_ui_ready('run_sync'):
            return
        if self._sync_task_id is not None:
            return
        self._sync_total = 0
        self._progress_indeterminate.setVisible(True)
        self._progress_indeterminate.start()
        self._set_status(tr('home.indexing', default='Indexing… {count} ROMs so far', count='0'))
        on_progress = wrap_callback(self._on_sync_progress)
        self._sync_task_id = submit(indexer.sync_index, kwargs={'on_progress': on_progress},
                                    on_done=self._on_sync_done, on_error=self._on_sync_error)
        self._update_sync_button()

    def _on_sync_progress(self, processed: int) -> None:
        if self._sync_task_id is None:
            return
        self._sync_total = processed
        self._set_status(tr('home.indexing', default='Indexing… {count} ROMs so far', count=f'{processed:,}'))

    def _on_sync_done(self, total: int) -> None:
        self._end_sync_ui()
        count = total if isinstance(total, int) and total else self._sync_total
        self._set_status(tr('home.sync_done', default='Indexed {count} ROMs', count=f'{count:,}') if count else '', transient=True)
        try:
            self.refresh_filters()
            self.sync_model()
        except Exception:
            logger.exception('Refresh after sync failed')

    def _on_sync_error(self, error: str) -> None:
        logger.error('Sync failed: %s', error)
        self._end_sync_ui()
        self._set_status(tr('home.sync_failed', default='Sync failed — check your connection and try again'), transient=True)
