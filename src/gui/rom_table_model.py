from __future__ import annotations

from PyQt6.QtCore import QAbstractTableModel, QModelIndex, Qt
from PyQt6.QtGui import QColor, QFont

from ..core import database as db
from ..core.translations import tr, locale_signal

COLUMN_KEYS = (
    "rom_table.col_title",
    "rom_table.col_console",
    "rom_table.col_source",
    "rom_table.col_size",
    "rom_table.col_region",
    "rom_table.col_language",
)
FIELD_MAP = ("title", "console", "source", "file_size", "region", "lang")

WHITE = QColor(255, 255, 255)
GRAY = QColor(166, 173, 200)
ROW_ALT = QColor(30, 30, 46)

_FONT_NORMAL = QFont()
_FONT_NORMAL.setPointSize(10)
_FONT_BOLD = QFont()
_FONT_BOLD.setPointSize(10)
_FONT_BOLD.setBold(True)


class ROMTableModel(QAbstractTableModel):
    def __init__(self, page_size: int = 30) -> None:
        super().__init__()
        self._page_size = page_size
        self._total_count = 0
        self._total_pages = 1
        self._current_page = 0
        self._sort_field = "title"
        self._sort_dir = "ASC"
        self._query = ""
        self._console = None
        self._sources = None
        self._rows: list[dict] = []
        locale_signal.changed.connect(self._on_locale_changed)

    def _on_locale_changed(self) -> None:
        self.headerDataChanged.emit(Qt.Orientation.Horizontal, 0, len(COLUMN_KEYS) - 1)

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return len(self._rows)

    def columnCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return len(COLUMN_KEYS)

    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole) -> object:
        if not index.isValid():
            return None
        row_idx = index.row()
        col_idx = index.column()
        if role == Qt.ItemDataRole.DisplayRole:
            if row_idx >= len(self._rows):
                return None
            value = self._rows[row_idx].get(FIELD_MAP[col_idx], "")
            return value if value else ""
        if role == Qt.ItemDataRole.ForegroundRole:
            return WHITE if col_idx == 0 else GRAY
        if role == Qt.ItemDataRole.BackgroundRole:
            if row_idx % 2 == 1:
                return ROW_ALT
        if role == Qt.ItemDataRole.FontRole:
            return _FONT_BOLD if col_idx == 0 else _FONT_NORMAL
        return None

    def headerData(self, section: int, orientation: Qt.Orientation, role: int = Qt.ItemDataRole.DisplayRole) -> object:
        if role == Qt.ItemDataRole.DisplayRole and orientation == Qt.Orientation.Horizontal:
            return tr(COLUMN_KEYS[section])
        return None

    def sort(self, section: int, order: Qt.SortOrder) -> None:
        self._sort_field = FIELD_MAP[section]
        self._sort_dir = "DESC" if order == Qt.SortOrder.DescendingOrder else "ASC"
        self._current_page = 0
        self.reload_page()

    @property
    def total_count(self) -> int:
        return self._total_count

    @property
    def total_pages(self) -> int:
        return self._total_pages

    @property
    def current_page(self) -> int:
        return self._current_page

    def set_filters(self, query: str = "", console: str | None = None, sources: list[str] | None = None) -> None:
        self._query = query
        self._console = console if console and console != "All Consoles" else None
        self._sources = sources
        self._current_page = 0
        self.reload_page()

    def go_to_page(self, page: int) -> None:
        if 0 <= page < self._total_pages:
            self._current_page = page
            self.reload_page()

    def reload_page(self) -> None:
        self._total_count = db.count_roms(console=self._console, sources=self._sources)
        self._total_pages = max(1, (self._total_count + self._page_size - 1) // self._page_size)
        offset = self._current_page * self._page_size
        self._rows = db.search_roms(
            query=self._query,
            console=self._console,
            sources=self._sources,
            offset=offset,
            limit=self._page_size,
            sort_field=self._sort_field,
            sort_dir=self._sort_dir,
        )
        self.layoutChanged.emit()

    def get_rom_at_row(self, row: int) -> dict | None:
        if 0 <= row < len(self._rows):
            return self._rows[row]
        return None
