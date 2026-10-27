import logging
import os
import time

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QKeySequence, QPixmap, QShortcut
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QButtonGroup, QHBoxLayout, QHeaderView, QLabel,
    QSizePolicy, QTableWidgetItem, QVBoxLayout, QWidget,
)
from qfluentwidgets import (
    BodyLabel, CaptionLabel, CardWidget, ComboBox, FlowLayout, FluentIcon, IconWidget,
    IndeterminateProgressBar, InfoBar, InfoBarPosition, PillPushButton, PushButton,
    SearchLineEdit, SmoothScrollArea, StrongBodyLabel, SubtitleLabel, TableWidget,
    TransparentToolButton,
)

from src.core.tbhb.api import search_async, details_async, track_download
from src.core.tbhb.models import TBHBEntry, TBHBDetails, CATEGORIES, category_bucket, format_date
from src.core.worker import Priority as TaskPriority, cancel as cancel_task, submit as submit_task
from src.core.downloader import DownloadManager
from src.core.translations import tr, register_locale_refresh
from src.core.theme import palette, themed_icon
from .repacks_page import DownloadActionWidget

logger = logging.getLogger(__name__)

_PAGE_LIMIT = 15
_AUTO_FETCHES = 4
_SEARCH_TIMEOUT_MS = 30000
_DOUBLE_SEARCH_GUARD = 0.35
_MAX_FILE_ROWS = 300
_GUI_DIR = os.path.dirname(os.path.abspath(__file__))
_LOGO_PATH = os.path.join(_GUI_DIR, 'photos', 'tbhb_logo.png')
_LOGO_HEIGHT = 380


_ROW_SPACING = 16
_COL_CAT, _COL_SIZE, _COL_AGE, _COL_SEEDS, _COL_LEECH = 64, 84, 84, 56, 56

_SORT_KEYS = ("best", "seeders", "newest", "largest")


def _sort_labels() -> list[str]:
    return [
        tr('tbhb.sort_best', default='Best match'),
        tr('tbhb.sort_seeders', default='Most seeders'),
        tr('tbhb.sort_newest', default='Newest'),
        tr('tbhb.sort_largest', default='Largest'),
    ]


def _make_elided(base):
    class _Elided(base):
        def __init__(self, text="", parent=None):
            super().__init__(parent)
            self._full = ""
            self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
            self.setMinimumWidth(40)
            self.set_full_text(text)

        def set_full_text(self, text: str) -> None:
            self._full = text
            self.setToolTip(text)
            self._elide()

        def resizeEvent(self, event):
            super().resizeEvent(event)
            self._elide()

        def _elide(self) -> None:
            width = self.width()
            if width <= 1:
                self.setText(self._full)
            else:
                self.setText(self.fontMetrics().elidedText(self._full, Qt.TextElideMode.ElideRight, width))
    return _Elided


ElidedBodyLabel = _make_elided(BodyLabel)
ElidedCaptionLabel = _make_elided(CaptionLabel)


def _cell(text: str, width: int, color: str | None = None) -> CaptionLabel:
    lbl = CaptionLabel(text)
    lbl.setFixedWidth(width)
    lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
    if color:
        lbl.setStyleSheet(f"color: {color};")
    return lbl


class ColumnHeader(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(20, 0, 20, 0)
        layout.setSpacing(_ROW_SPACING)
        muted = palette()['muted']
        self._labels = {}
        for key, default, width in (
            ('name', 'Name', 0), ('cat', 'Type', _COL_CAT), ('size', 'Size', _COL_SIZE),
            ('age', 'Added', _COL_AGE), ('seeds', '↑ Seeds', _COL_SEEDS), ('leech', '↓ Peers', _COL_LEECH),
        ):
            lbl = CaptionLabel(tr(f'tbhb.col_{key}', default=default))
            lbl.setStyleSheet(f"color: {muted};")
            if width:
                lbl.setFixedWidth(width)
                lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
                layout.addWidget(lbl)
            else:
                layout.addWidget(lbl, 1)
            self._labels[key] = (lbl, default)

    def refresh_locale(self) -> None:
        for key, (lbl, default) in self._labels.items():
            lbl.setText(tr(f'tbhb.col_{key}', default=default))


class ResultRow(CardWidget):
    def __init__(self, entry: TBHBEntry, on_click, parent=None):
        super().__init__(parent)
        self.entry = entry
        self._on_click = on_click
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFixedHeight(68)
        pal = palette()
        layout = QHBoxLayout(self)
        layout.setContentsMargins(20, 8, 20, 8)
        layout.setSpacing(_ROW_SPACING)

        title_col = QVBoxLayout()
        title_col.setSpacing(3)
        title_col.setAlignment(Qt.AlignmentFlag.AlignVCenter)
        title_col.addWidget(ElidedBodyLabel(entry.title))
        meta_parts = []
        if entry.username:
            meta_parts.append(tr('tbhb.by_uploader', name=entry.username))
        if entry.trust:
            meta_parts.append("VIP" if entry.trust == "vip" else tr('tbhb.trusted', default='Trusted'))
        if entry.format_guess:
            meta_parts.append(entry.format_guess)
        if entry.source:
            meta_parts.append(entry.source)
        title_col.addWidget(ElidedCaptionLabel(" • ".join(meta_parts)))
        layout.addLayout(title_col, 1)

        layout.addWidget(_cell(entry.category_name, _COL_CAT), 0, Qt.AlignmentFlag.AlignVCenter)
        layout.addWidget(_cell(entry.size_label or "—", _COL_SIZE), 0, Qt.AlignmentFlag.AlignVCenter)
        layout.addWidget(_cell(entry.age_label or "—", _COL_AGE), 0, Qt.AlignmentFlag.AlignVCenter)
        seed_color = pal['state_seeding'] if entry.seeders > 0 else pal['faint_text']
        layout.addWidget(_cell(str(entry.seeders), _COL_SEEDS, seed_color), 0, Qt.AlignmentFlag.AlignVCenter)
        layout.addWidget(_cell(str(entry.leechers), _COL_LEECH, pal['muted']), 0, Qt.AlignmentFlag.AlignVCenter)

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton and self.rect().contains(event.position().toPoint()):
            self._on_click(self.entry)
        super().mouseReleaseEvent(event)


class ResultsList(QWidget):
    def __init__(self, on_row_click, parent=None):
        super().__init__(parent)
        self._on_row_click = on_row_click
        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(4, 4, 4, 4)
        self._layout.setSpacing(10)
        self._layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        self._rows: list[ResultRow] = []

    def clear(self) -> None:
        for r in self._rows:
            r.setParent(None)
            r.deleteLater()
        self._rows.clear()

    def set_entries(self, entries: list[TBHBEntry]) -> None:
        self.clear()
        for entry in entries:
            row = ResultRow(entry, self._on_row_click)
            self._layout.addWidget(row)
            self._rows.append(row)


class PaginationBar(QWidget):
    def __init__(self, on_prev, on_next, parent=None):
        super().__init__(parent)
        self._page = 0
        self._has_next = False
        self._busy = False
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)
        self._prev_btn = TransparentToolButton(themed_icon('tbhb.results_prev_page', FluentIcon.LEFT_ARROW))
        self._prev_btn.setFixedSize(28, 28)
        self._prev_btn.clicked.connect(on_prev)
        layout.addWidget(self._prev_btn)
        self._label = StrongBodyLabel(tr('tbhb.page', num=1))
        layout.addWidget(self._label)
        self._next_btn = TransparentToolButton(themed_icon('tbhb.results_next_page', FluentIcon.RIGHT_ARROW))
        self._next_btn.setFixedSize(28, 28)
        self._next_btn.clicked.connect(on_next)
        layout.addWidget(self._next_btn)
        self._sync()

    def _sync(self) -> None:
        self._prev_btn.setEnabled(self._page > 0 and not self._busy)
        self._next_btn.setEnabled(self._has_next and not self._busy)

    def set_page(self, page: int, has_next: bool) -> None:
        self._page, self._has_next = page, has_next
        self._label.setText(tr('tbhb.page', num=page + 1))
        self._sync()

    def set_busy(self, busy: bool) -> None:
        self._busy = busy
        self._sync()

    def refresh_locale(self) -> None:
        self._label.setText(tr('tbhb.page', num=self._page + 1))


class DetailsView(QWidget):
    def __init__(self, manager: DownloadManager, parent=None):
        super().__init__(parent)
        self._details: TBHBDetails | None = None
        self._download_action = DownloadActionWidget(manager, "tbhb")
        self._download_action.download_started.connect(self._track_download)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 4, 0, 0)
        outer.setSpacing(10)

        top = QHBoxLayout()
        top.setSpacing(8)
        self._back_btn = PushButton(themed_icon('tbhb.detail_back', FluentIcon.RETURN), tr('tbhb.back_to_results'))
        self._back_btn.setFixedHeight(32)
        self.back_requested = self._back_btn.clicked
        top.addWidget(self._back_btn)
        self._copy_btn = PushButton(themed_icon('tbhb.detail_copy', FluentIcon.COPY), tr('tbhb.copy_magnet', default='Copy magnet'))
        self._copy_btn.setFixedHeight(32)
        self._copy_btn.clicked.connect(self._copy_magnet)
        top.addWidget(self._copy_btn)
        top.addStretch(1)
        outer.addLayout(top)

        scroll = SmoothScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setStyleSheet("QScrollArea{background: transparent; border: none;}")
        body = QWidget()
        body.setStyleSheet("background: transparent;")
        body_l = QVBoxLayout(body)
        body_l.setContentsMargins(0, 0, 8, 8)
        body_l.setSpacing(14)
        scroll.setWidget(body)
        outer.addWidget(scroll, 1)

        header_card = CardWidget()
        header_l = QVBoxLayout(header_card)
        header_l.setContentsMargins(20, 18, 20, 18)
        header_l.setSpacing(12)
        self._title_lbl = SubtitleLabel("")
        self._title_lbl.setWordWrap(True)
        self._title_lbl.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        header_l.addWidget(self._title_lbl)

        stats_host = QWidget()
        stats_host.setStyleSheet("background: transparent;")
        flow = FlowLayout(stats_host, needAni=False)
        flow.setContentsMargins(0, 0, 0, 0)
        flow.setHorizontalSpacing(28)
        flow.setVerticalSpacing(8)
        self._category_lbl = self._stat(flow, themed_icon('tbhb.stat_category', FluentIcon.TAG))
        self._size_lbl = self._stat(flow, themed_icon('tbhb.stat_size', FluentIcon.DOCUMENT))
        self._seeders_lbl = self._stat(flow, themed_icon('tbhb.stat_seeders', FluentIcon.UP))
        self._leechers_lbl = self._stat(flow, themed_icon('tbhb.stat_leechers', FluentIcon.DOWN))
        self._username_lbl = self._stat(flow, themed_icon('tbhb.stat_uploader', FluentIcon.PEOPLE))
        self._format_lbl = self._stat(flow, themed_icon('tbhb.stat_format', FluentIcon.ALBUM))
        self._added_lbl = self._stat(flow, themed_icon('tbhb.stat_added', FluentIcon.CALENDAR))
        self._source_lbl = self._stat(flow, themed_icon('tbhb.stat_source', FluentIcon.GLOBE))
        header_l.addWidget(stats_host)

        self._hash_lbl = CaptionLabel("")
        self._hash_lbl.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        header_l.addWidget(self._hash_lbl)
        body_l.addWidget(header_card)

        body_l.addWidget(self._download_action)

        files_card = CardWidget()
        files_l = QVBoxLayout(files_card)
        files_l.setContentsMargins(20, 16, 20, 16)
        files_l.setSpacing(8)
        files_head = QHBoxLayout()
        self._files_title = StrongBodyLabel(tr('tbhb.files', default='Files'))
        files_head.addWidget(self._files_title)
        self._files_caption = CaptionLabel("")
        files_head.addWidget(self._files_caption)
        files_head.addStretch(1)
        files_l.addLayout(files_head)
        self._files_table = TableWidget()
        self._files_table.setColumnCount(2)
        self._files_table.verticalHeader().hide()
        self._files_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._files_table.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self._files_table.setShowGrid(False)
        self._files_table.setWordWrap(False)
        self._files_table.setBorderVisible(True)
        self._files_table.setBorderRadius(8)
        self._files_table.setFixedHeight(260)
        header = self._files_table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        self._apply_table_headers()
        self._files_table.setVisible(False)
        files_l.addWidget(self._files_table)
        body_l.addWidget(files_card)
        body_l.addStretch(1)

    def _apply_table_headers(self) -> None:
        self._files_table.setHorizontalHeaderLabels([tr('tbhb.col_name', default='Name'), tr('tbhb.col_size', default='Size')])

    def _stat(self, flow: FlowLayout, icon) -> BodyLabel:
        holder = QWidget()
        holder.setStyleSheet("background: transparent;")
        h = QHBoxLayout(holder)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(6)
        icon_w = IconWidget(icon)
        icon_w.setFixedSize(16, 16)
        h.addWidget(icon_w)
        lbl = BodyLabel("")
        h.addWidget(lbl)
        flow.addWidget(holder)
        lbl._holder = holder
        return lbl

    @staticmethod
    def _set_optional(lbl: BodyLabel, text: str) -> None:
        lbl.setText(text)
        lbl._holder.setVisible(bool(text))

    def _track_download(self) -> None:
        submit_task(track_download, priority=TaskPriority.LOW)

    def _copy_magnet(self) -> None:
        magnet = (self._details.extra.get("magnet_url") if self._details else "") or ""
        if not magnet:
            InfoBar.warning(tr('tbhb.no_magnet', default='No magnet link available'), "", duration=2200, position=InfoBarPosition.TOP, parent=self)
            return
        QApplication.clipboard().setText(magnet)
        self._track_download()
        InfoBar.success(tr('tbhb.magnet_copied', default='Magnet link copied'), "", duration=1800, position=InfoBarPosition.TOP, parent=self)

    def show_details(self, details: TBHBDetails, files_state: str = "ready") -> None:
        self._details = details
        pal = palette()
        self._title_lbl.setText(details.title)
        self._category_lbl.setText(details.category_name)
        self._size_lbl.setText(details.size_label or "—")
        self._seeders_lbl.setText(str(details.seeders))
        self._seeders_lbl.setStyleSheet(f"color: {pal['state_seeding'] if details.seeders > 0 else pal['faint_text']};")
        self._leechers_lbl.setText(str(details.leechers))
        uploader = details.username or tr('tbhb.unknown_uploader')
        if details.trust:
            uploader += " · " + ("VIP" if details.trust == "vip" else tr('tbhb.trusted', default='Trusted'))
        self._username_lbl.setText(uploader)
        self._format_lbl.setText(details.file_format or "—")
        self._set_optional(self._added_lbl, format_date(details.added))
        self._set_optional(self._source_lbl, details.source)
        self._hash_lbl.setText(tr('tbhb.hash', hash=details.info_hash) if details.info_hash else "")
        self._fill_files(details, files_state)
        self._download_action.set_details(details)

    def set_files_state(self, state: str) -> None:
        if self._details is not None:
            self._fill_files(self._details, state)

    def _fill_files(self, details: TBHBDetails, state: str) -> None:
        files = details.files
        table = self._files_table
        if state == "loading" and not files:
            self._files_caption.setText(tr('tbhb.files_loading', default='Loading files…'))
            table.setVisible(False)
            return
        if not files:
            key, default = (('tbhb.files_unavailable', 'File list unavailable') if state == "error"
                            else ('tbhb.files_none', 'No file list for this torrent'))
            self._files_caption.setText(tr(key, default=default))
            table.setVisible(False)
            return
        total = details.total_size_label
        self._files_caption.setText(f"{len(files)} • {total}" if total else str(len(files)))
        shown = files[:_MAX_FILE_ROWS]
        extra = len(files) - len(shown)
        table.setRowCount(len(shown) + (1 if extra > 0 else 0))
        right = Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        for row, f in enumerate(shown):
            name_item = QTableWidgetItem(f["name"])
            name_item.setToolTip(f["name"])
            size_item = QTableWidgetItem(f.get("sizeLabel") or "")
            size_item.setTextAlignment(right)
            table.setItem(row, 0, name_item)
            table.setItem(row, 1, size_item)
        if extra > 0:
            table.setItem(len(shown), 0, QTableWidgetItem(tr('tbhb.files_more', default='… and more files') + f" (+{extra})"))
            table.setItem(len(shown), 1, QTableWidgetItem(""))
        table.setVisible(True)

    def refresh_locale(self) -> None:
        self._back_btn.setText(tr('tbhb.back_to_results'))
        self._copy_btn.setText(tr('tbhb.copy_magnet', default='Copy magnet'))
        self._files_title.setText(tr('tbhb.files', default='Files'))
        self._apply_table_headers()

    def shutdown(self) -> None:
        self._download_action.shutdown()


class TBHBPage(QWidget):
    def __init__(self, manager: DownloadManager, parent=None):
        super().__init__(parent)
        self._manager = manager
        self._query = ""
        self._category = 0
        self._page = 0
        self._has_next = False
        self._loading = False
        self._seq = 0
        self._fetches_left = 0
        self._last_search_at = 0.0
        self._sort_key = "best"
        self._search_task = None
        self._details_task = None
        self._details_entry_id = None
        self._pill_sets: list[dict[int, PillPushButton]] = []
        self._reset_results()
        self._search_watchdog = QTimer(self)
        self._search_watchdog.setSingleShot(True)
        self._search_watchdog.timeout.connect(self._on_search_timeout)

        root = QVBoxLayout(self)
        root.setContentsMargins(24, 24, 24, 12)
        root.setSpacing(12)


        self._search_section = QWidget()
        search_layout = QVBoxLayout(self._search_section)
        search_layout.setContentsMargins(0, 0, 0, 0)
        search_layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
        search_layout.addStretch(2)
        logo_lbl = QLabel()
        if os.path.isfile(_LOGO_PATH):
            pix = QPixmap(_LOGO_PATH)
            logo_lbl.setPixmap(pix.scaledToHeight(_LOGO_HEIGHT, Qt.TransformationMode.SmoothTransformation))
        else:
            logo_lbl.setText("The Black Hole Bay")
        logo_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        search_layout.addWidget(logo_lbl)

        bar_row = QHBoxLayout()
        bar_row.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._search_bar = SearchLineEdit()
        self._search_bar.setPlaceholderText(tr('tbhb.search_placeholder'))
        self._search_bar.setFixedWidth(480)
        self._bind_search(self._search_bar)
        bar_row.addWidget(self._search_bar)
        search_layout.addLayout(bar_row)

        cat_row = self._build_pills()
        cat_row.setAlignment(Qt.AlignmentFlag.AlignCenter)
        search_layout.addLayout(cat_row)

        self._hint_lbl = CaptionLabel(tr('tbhb.browse_hint', default='Press Enter on an empty search to browse the latest uploads'))
        self._hint_lbl.setStyleSheet(f"color: {palette()['muted']};")
        self._hint_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        search_layout.addWidget(self._hint_lbl)
        search_layout.addStretch(3)
        root.addWidget(self._search_section, 1)


        self._results_section = QWidget()
        results_layout = QVBoxLayout(self._results_section)
        results_layout.setContentsMargins(0, 0, 0, 0)
        results_layout.setSpacing(10)

        top_row = QHBoxLayout()
        top_row.setSpacing(10)
        self._new_search_btn = PushButton(themed_icon('tbhb.new_search', FluentIcon.HOME), tr('tbhb.new_search'))
        self._new_search_btn.clicked.connect(self._reset_to_search)
        top_row.addWidget(self._new_search_btn)
        self._results_search = SearchLineEdit()
        self._results_search.setPlaceholderText(tr('tbhb.search_placeholder'))
        self._bind_search(self._results_search)
        top_row.addWidget(self._results_search, 1)
        results_layout.addLayout(top_row)

        results_layout.addLayout(self._build_pills())

        info_row = QHBoxLayout()
        info_row.setSpacing(10)
        self._status_lbl = CaptionLabel("")
        info_row.addWidget(self._status_lbl, 1)
        self._sort_lbl = CaptionLabel(tr('tbhb.sort_by', default='Sort by'))
        info_row.addWidget(self._sort_lbl)
        self._sort_combo = ComboBox()
        self._sort_combo.addItems(_sort_labels())
        self._sort_combo.setFixedWidth(150)
        self._sort_combo.currentIndexChanged.connect(self._on_sort_changed)
        info_row.addWidget(self._sort_combo)
        self._pagination = PaginationBar(self._on_prev_page, self._on_next_page)
        info_row.addWidget(self._pagination)
        results_layout.addLayout(info_row)

        self._busy_bar = IndeterminateProgressBar(start=False)
        self._busy_bar.setVisible(False)
        results_layout.addWidget(self._busy_bar)

        self._column_header = ColumnHeader()
        results_layout.addWidget(self._column_header)

        self._list = ResultsList(self._open_details)
        self._scroll_area = SmoothScrollArea()
        self._scroll_area.setWidgetResizable(True)
        self._scroll_area.setWidget(self._list)
        self._scroll_area.setStyleSheet("QScrollArea{background: transparent; border: none;}")
        self._list.setStyleSheet("background: transparent;")
        results_layout.addWidget(self._scroll_area, 1)
        self._results_section.setVisible(False)
        root.addWidget(self._results_section, 1)


        self._details_view = DetailsView(manager)
        self._details_view.back_requested.connect(self._show_results)
        self._details_view.setVisible(False)
        root.addWidget(self._details_view, 1)

        esc = QShortcut(QKeySequence(Qt.Key.Key_Escape), self)
        esc.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        esc.activated.connect(self._on_escape)

        register_locale_refresh(self, self._apply_locale)


    def _bind_search(self, bar: SearchLineEdit) -> None:
        bar.searchSignal.connect(self._on_search)
        bar.returnPressed.connect(lambda b=bar: self._on_search(b.text()))

    def _build_pills(self) -> QHBoxLayout:
        row = QHBoxLayout()
        row.setSpacing(8)
        group = QButtonGroup(self)
        group.setExclusive(True)
        buttons: dict[int, PillPushButton] = {}
        for code, name in CATEGORIES:
            btn = PillPushButton(name)
            btn.setCheckable(True)
            group.addButton(btn)
            row.addWidget(btn)
            buttons[code] = btn
        buttons[self._category].setChecked(True)
        group.buttonClicked.connect(self._on_category_clicked)
        self._pill_sets.append(buttons)
        return row

    def _apply_locale(self, *_args) -> None:
        for bar in (self._search_bar, self._results_search):
            bar.setPlaceholderText(tr('tbhb.search_placeholder'))
        self._hint_lbl.setText(tr('tbhb.browse_hint', default='Press Enter on an empty search to browse the latest uploads'))
        self._new_search_btn.setText(tr('tbhb.new_search'))
        self._sort_lbl.setText(tr('tbhb.sort_by', default='Sort by'))
        for i, text in enumerate(_sort_labels()):
            self._sort_combo.setItemText(i, text)
        self._column_header.refresh_locale()
        self._pagination.refresh_locale()
        self._details_view.refresh_locale()
        if self._loading:
            self._status_lbl.setText(tr('tbhb.loading'))
        elif self._all_entries or self._query or self._results_section.isVisible():
            self._render_page()


    def _on_escape(self) -> None:
        if self._details_view.isVisible():
            self._show_results()

    def _reset_to_search(self) -> None:
        self._cancel_search()
        self._results_section.setVisible(False)
        self._details_view.setVisible(False)
        self._search_section.setVisible(True)
        self._search_bar.setText("")
        self._search_bar.setFocus()

    def _show_results(self) -> None:
        self._details_view.setVisible(False)
        self._results_section.setVisible(True)


    def _reset_results(self, keep_fallback: bool = False) -> None:
        self._all_entries: list[TBHBEntry] = []
        self._seen: set[str] = set()
        self._server_page = -1
        self._server_exhausted = False
        self._stale = False
        if not keep_fallback:
            self._fallback_active = False

    def _effective_category(self) -> int:
        return 0 if self._fallback_active else self._category

    def _on_category_clicked(self, btn) -> None:
        code = next((c for s in self._pill_sets for c, b in s.items() if b is btn), 0)
        self._set_category(code)

    def _set_category(self, code: int) -> None:
        changed = code != self._category
        self._category = code
        for buttons in self._pill_sets:
            buttons[code].setChecked(True)
        if changed and self._results_section.isVisible():
            self._start_search(use_cache=True)

    def _on_sort_changed(self, index: int) -> None:
        self._sort_key = _SORT_KEYS[max(0, min(index, len(_SORT_KEYS) - 1))]
        self._page = 0
        if not self._loading:
            self._fetches_left = _AUTO_FETCHES
            self._ensure_loaded()

    def _on_search(self, text: str) -> None:
        now = time.monotonic()
        if now - self._last_search_at < _DOUBLE_SEARCH_GUARD:
            return
        self._last_search_at = now
        self._query = " ".join(text.split())
        self._start_search(use_cache=False)

    def _start_search(self, use_cache: bool) -> None:
        self._cancel_search()
        self._reset_results()
        self._page = 0
        self._search_section.setVisible(False)
        self._details_view.setVisible(False)
        self._results_section.setVisible(True)
        self._results_search.setText(self._query)
        self._fetches_left = _AUTO_FETCHES
        self._ensure_loaded(use_cache)

    def _cancel_search(self) -> None:
        self._seq += 1
        self._search_watchdog.stop()
        if self._search_task is not None:
            cancel_task(self._search_task)
            self._search_task = None
        self._set_loading(False)

    def _set_loading(self, loading: bool) -> None:
        self._loading = loading
        self._pagination.set_busy(loading)
        self._busy_bar.setVisible(loading)
        if loading:
            self._busy_bar.start()
            self._status_lbl.setText(tr('tbhb.loading'))
        else:
            self._busy_bar.stop()

    def _ensure_loaded(self, use_cache: bool = True) -> None:
        need = (self._page + 1) * _PAGE_LIMIT + 1
        if len(self._all_entries) < need and not self._server_exhausted and self._fetches_left > 0:
            self._fetches_left -= 1
            self._fetch(self._server_page + 1, use_cache)
        else:
            self._render_page()

    def _fetch(self, api_page: int, use_cache: bool) -> None:
        if self._search_task is not None:
            cancel_task(self._search_task)
        self._seq += 1
        seq = self._seq
        self._set_loading(True)
        self._search_task = search_async(
            self._query, self._effective_category(), api_page,
            on_done=lambda data, s=seq, p=api_page: self._on_search_done(data, s, p),
            on_error=lambda err, s=seq: self._on_search_error(err, s),
            use_cache=use_cache,
        )
        self._search_watchdog.start(_SEARCH_TIMEOUT_MS)

    def _on_search_timeout(self) -> None:
        if not self._loading:
            return
        self._cancel_search()
        self._show_error(tr('tbhb.request_timed_out'))

    def _on_search_done(self, data: dict, seq: int, api_page: int) -> None:
        if seq != self._seq:
            return
        self._search_watchdog.stop()
        self._search_task = None
        self._set_loading(False)
        self._stale = self._stale or bool(data.get("_stale"))
        parsed = [TBHBEntry.from_api(r) for r in (data.get("results") or []) if isinstance(r, dict)]
        fresh = []
        for e in parsed:
            if e.dedupe_key not in self._seen:
                self._seen.add(e.dedupe_key)
                fresh.append(e)

        if not fresh:
            self._server_exhausted = True
        self._server_page = api_page
        category = self._effective_category()
        if category:
            fresh = [e for e in fresh if category_bucket(e.category) == category]
        if api_page == 0 and not fresh and self._category and not self._fallback_active:
            self._fallback_active = True
            self._reset_results(keep_fallback=True)
            self._fetches_left = _AUTO_FETCHES
            self._ensure_loaded()
            return
        self._all_entries.extend(fresh)
        self._ensure_loaded()

    def _on_search_error(self, error, seq: int | None = None) -> None:
        if seq is not None and seq != self._seq:
            return
        self._search_watchdog.stop()
        self._search_task = None
        self._set_loading(False)
        self._show_error(str(error))

    def _show_error(self, message: str) -> None:
        low = message.lower()
        if "429" in message or "rate limit" in low:
            text = tr('tbhb.rate_limited')
        elif "403" in message or "banned" in low:
            text = tr('tbhb.blocked')
        else:
            text = tr('tbhb.search_failed', error=message)
        InfoBar.error(tr('tbhb.search_failed_title'), message, position=InfoBarPosition.TOP, parent=self)
        if self._all_entries:
            self._render_page()
        else:
            self._status_lbl.setText(text)
            self._list.clear()
            self._pagination.set_page(0, False)


    def _sorted_entries(self) -> list[TBHBEntry]:
        entries = list(self._all_entries)
        if self._sort_key == "seeders":
            entries.sort(key=lambda e: (-e.seeders, -e.leechers))
        elif self._sort_key == "newest":
            entries.sort(key=lambda e: -e.added)
        elif self._sort_key == "largest":
            entries.sort(key=lambda e: -e.size)
        return entries

    def _on_prev_page(self) -> None:
        if self._loading or self._page == 0:
            return
        self._page -= 1
        self._render_page()

    def _on_next_page(self) -> None:
        if self._loading or not self._has_next:
            return
        self._page += 1
        self._fetches_left = _AUTO_FETCHES
        self._ensure_loaded()

    def _render_page(self) -> None:
        view = self._sorted_entries()
        if self._page > 0 and self._page * _PAGE_LIMIT >= len(view):
            self._page = max(0, (len(view) - 1) // _PAGE_LIMIT)
        start = self._page * _PAGE_LIMIT
        chunk = view[start:start + _PAGE_LIMIT]
        self._has_next = bool(view) and (len(view) > start + _PAGE_LIMIT or not self._server_exhausted)
        self._pagination.set_page(self._page, self._has_next)
        if not view:
            label = f'"{self._query}"' if self._query else tr('tbhb.latest_uploads')
            self._status_lbl.setText(tr('tbhb.no_results_for', category=self._current_category_name(), query=label))
            self._list.clear()
            return
        count = len(view) if self._server_exhausted else f"{len(view)}+"
        if self._fallback_active:
            text = tr('tbhb.no_category_matches', category=self._current_category_name(), count=count)
        else:
            text = tr('tbhb.results_count', count=count)
        if self._stale:
            text += " · " + tr('tbhb.cached_results', default='showing cached results')
        self._status_lbl.setText(text)
        self._list.set_entries(chunk)
        self._scroll_area.verticalScrollBar().setValue(0)

    def _current_category_name(self) -> str:
        for code, name in CATEGORIES:
            if code == self._category:
                return name
        return "All"


    def _open_details(self, entry: TBHBEntry) -> None:
        if self._details_task is not None:
            try:
                cancel_task(self._details_task)
            except Exception:
                pass
            self._details_task = None
        self._details_entry_id = entry.id
        self._results_section.setVisible(False)
        self._details_view.setVisible(True)
        self._details_view.show_details(TBHBDetails.from_entry(entry), "loading")
        self._details_task = details_async(
            entry.id,
            on_done=lambda d, e=entry: self._on_details_done(e, d),
            on_error=lambda err, eid=entry.id: self._on_details_error(err, eid),
        )

    def _on_details_done(self, entry: TBHBEntry, data: dict) -> None:
        if entry.id != self._details_entry_id:
            return
        self._details_task = None
        details = TBHBDetails.from_api(data)
        details.fill_missing_from(entry)
        self._details_view.show_details(details, "error" if details.extra.get("files_error") else "ready")

    def _on_details_error(self, error, entry_id: str | None = None) -> None:
        if entry_id is not None and entry_id != self._details_entry_id:
            return
        self._details_task = None
        self._details_view.set_files_state("error")
        InfoBar.error(tr('tbhb.details_failed_title'), str(error), position=InfoBarPosition.TOP, parent=self)

    def shutdown(self) -> None:
        self._seq += 1
        self._search_watchdog.stop()
        for task_id in (self._search_task, self._details_task):
            if task_id is not None:
                cancel_task(task_id)
        self._details_view.shutdown()
