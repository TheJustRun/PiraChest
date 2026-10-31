from __future__ import annotations
import asyncio
import datetime as _dt
import json
import logging
import os
import re
import shutil
import html
import threading
from contextlib import suppress
from concurrent.futures import ThreadPoolExecutor
import gc
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Optional
from urllib.parse import urljoin

import httpx
from PySide6.QtCore import (
    Qt, QSize, Signal, QTimer, QEvent, QUrl, QObject,
)
from PySide6.QtGui import QIcon, QFont, QColor, QPixmap, QDesktopServices
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
from PySide6.QtMultimediaWidgets import QVideoWidget
from PySide6.QtWidgets import QPushButton as _QPushButton
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QGridLayout, QStackedWidget, QListWidgetItem,
    QFileDialog, QDialog, QLabel,
    QScrollArea, QAbstractScrollArea, QTreeWidgetItem,
    QSlider, QSizePolicy, QTableWidgetItem, QHeaderView,
)
from qfluentwidgets import (
    Pivot, FluentIcon, IconWidget, BodyLabel, CaptionLabel, SearchLineEdit, ComboBox, PrimaryPushButton,
    PushButton, TransparentToolButton, StrongBodyLabel, TitleLabel, PillPushButton, ImageLabel, InfoBar,
    ListWidget, CardWidget, SmoothScrollArea, CheckBox, IndeterminateProgressRing, isDarkTheme,
    MessageBoxBase, SubtitleLabel, TableWidget, TreeWidget, SegmentedWidget, LineEdit,
)

from src.core import worker as _worker_module
from functools import partial
from shiboken6 import isValid as _qt_is_valid
from src.core.artwork import images, FIT

_LOGO_SIZE = (48, 48)
from src.core.tv import channels as channels_backend
from src.core.tv import streams as stream_backend
from src.core.tv import tmdb
from src.core.tv.anime import anime_backend
from src.core.config import settings

import zlib
from collections import OrderedDict
from PySide6.QtCore import QAbstractListModel, QElapsedTimer, QModelIndex, QPoint, QRect, QRectF, QSettings
from PySide6.QtGui import QActionGroup, QCursor, QIconEngine, QFontMetrics, QGuiApplication, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import (QAbstractItemView, QBoxLayout, QFrame, QMenu, QStyle, QStyledItemDelegate,
                               QToolButton, QToolTip)
from qfluentwidgets import Action, ListView, RoundMenu
from src.core.models import AnimeItem, assess_source_files, classify_file
from src.core.net import LazyHttpClient, shared_ssl_context
from src.core.translations import tr, register_locale_refresh
from src.core.theme import to_color, palette, apply_pivot_theme, apply_tool_button_theme, apply_label_theme, count_badge_qss, register_theme_refresh, themed_icon, apply_slot_icon_theme, ScrollingTextDelegate
from .home_page import MediaPageBase
from .grid import PosterGrid, PosterStrip, ViewModeButton, attrs_subtitle, grid_colors as _grid_colors


_CATEGORIES = (
    ("trending", "tv.movies.trending"),
    ("popular", "tv.movies.popular"),
    ("top_rated", "tv.movies.top_rated"),
    ("now_playing", "tv.movies.recent"),
    ("upcoming", "tv.movies.upcoming"),
)

logger = logging.getLogger(__name__)


def _watch_later_path() -> str:
    try:
        from src.core.config import paths as _paths
        base = _paths.config_dir
    except Exception:
        base = os.path.join(os.path.expanduser('~'), '.pirachest')
    return os.path.join(base, 'pirachest_watch_later.json')


class WatchLaterStore(QObject):
    changed = Signal()

    def __init__(self, path: Optional[str] = None) -> None:
        super().__init__()
        self._path = path or _watch_later_path()
        self._items: dict[str, dict] = {}
        self._loaded = False
        self._lock = threading.Lock()

    @staticmethod
    def key(media_type: str, media_id: Any, season: Any = None, episode: Any = None) -> str:
        base = f'{media_type}:{media_id}'
        if episode is None:
            return base
        return f'{base}:s{season if season is not None else 0}e{episode}'

    @staticmethod
    def entry_key(entry: dict) -> str:
        return WatchLaterStore.key(entry.get('media_type'), entry.get('id'),
                                   entry.get('season'), entry.get('episode'))

    def _ensure_loaded(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        try:
            with open(self._path, 'r', encoding='utf-8') as fh:
                data = json.load(fh)
        except FileNotFoundError:
            return
        except Exception:
            logger.exception('Failed to read the watch later list')
            return
        if not isinstance(data, list):
            return
        for entry in data:
            if not isinstance(entry, dict) or not entry.get('media_type') or entry.get('id') is None:
                continue
            self._items[self.entry_key(entry)] = entry

    def _save(self) -> None:
        try:
            os.makedirs(os.path.dirname(self._path), exist_ok=True)
            tmp = self._path + '.tmp'
            with open(tmp, 'w', encoding='utf-8') as fh:
                json.dump(list(self._items.values()), fh, ensure_ascii=False, indent=1)
            os.replace(tmp, self._path)
        except Exception:
            logger.exception('Failed to save the watch later list')

    def all(self) -> list[dict]:
        with self._lock:
            self._ensure_loaded()
            return sorted(self._items.values(), key=lambda d: d.get('added_at') or 0, reverse=True)

    def count(self) -> int:
        with self._lock:
            self._ensure_loaded()
            return len(self._items)

    def has(self, media_type: str, media_id: Any, season: Any = None, episode: Any = None) -> bool:
        with self._lock:
            self._ensure_loaded()
            return self.key(media_type, media_id, season, episode) in self._items

    def add(self, entry: dict) -> bool:
        media_type, media_id = entry.get('media_type'), entry.get('id')
        if not media_type or media_id is None:
            return False
        with self._lock:
            self._ensure_loaded()
            key = self.entry_key(entry)
            if key in self._items:
                return False
            item = dict(entry)
            item.setdefault('added_at', time.time())
            self._items[key] = item
        self._save()
        self.changed.emit()
        return True

    def remove(self, media_type: str, media_id: Any, season: Any = None, episode: Any = None) -> bool:
        with self._lock:
            self._ensure_loaded()
            key = self.key(media_type, media_id, season, episode)
            if key not in self._items:
                return False
            del self._items[key]
        self._save()
        self.changed.emit()
        return True

    def toggle(self, entry: dict) -> bool:
        with self._lock:
            self._ensure_loaded()
            present = self.entry_key(entry) in self._items
        if present:
            self.remove(entry.get('media_type'), entry.get('id'), entry.get('season'), entry.get('episode'))
            return False
        self.add(entry)
        return True


_watch_later_store: Optional[WatchLaterStore] = None


def watch_later_store() -> WatchLaterStore:
    global _watch_later_store
    if _watch_later_store is None:
        _watch_later_store = WatchLaterStore()
    return _watch_later_store


_HEART = QPainterPath()
_HEART.moveTo(.5, .88)
for _c in ((.18, .62, .04, .45, .04, .28), (.04, .14, .15, .06, .27, .06), (.37, .06, .46, .12, .5, .2),
           (.54, .12, .63, .06, .73, .06), (.85, .06, .96, .14, .96, .28), (.96, .45, .82, .62, .5, .88)):
    _HEART.cubicTo(*_c)
_HEART.closeSubpath()


class _WatchLaterButton(PushButton):
    def __init__(self, parent=None) -> None:
        super().__init__(parent=parent)
        self._saved = False
        self._label = ""
        self.setFixedHeight(32)
        self.setAttribute(Qt.WidgetAttribute.WA_Hover)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    def set_state(self, saved: bool, label: str) -> None:
        self._saved, self._label = saved, label
        self.updateGeometry()
        self.update()

    def sizeHint(self) -> QSize:
        return QSize(56 + self.fontMetrics().horizontalAdvance(self._label), 32)

    minimumSizeHint = sizeHint

    def paintEvent(self, _e) -> None:
        w, h = self.width(), self.height()
        ink = to_color(palette().get('primary_text', '#ffffff'), '#ffffff')
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        fill = QColor(ink)
        fill.setAlpha(10 if self.isDown() else 26 if self.underMouse() else 16)
        edge = QColor(ink)
        edge.setAlpha(30)
        p.setPen(QPen(edge, 1))
        p.setBrush(fill)
        p.drawRoundedRect(QRectF(0.5, 0.5, w - 1, h - 1), 6, 6)
        size = 16
        p.save()
        p.translate(14, (h - size) / 2)
        p.scale(size, size)
        p.setPen(QPen(ink, 1.6 / size, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
        p.setBrush(ink if self._saved else Qt.BrushStyle.NoBrush)
        p.drawPath(_HEART)
        p.restore()
        p.setPen(ink)
        room = max(0, w - 52)
        text = self.fontMetrics().elidedText(self._label, Qt.TextElideMode.ElideRight, room)
        p.drawText(QRectF(38, 0, room, h), Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, text)


_icon_cache: dict[tuple, QIcon] = {}


_PLAY = QPainterPath()
_PLAY.moveTo(.22, .1)
_PLAY.lineTo(.88, .5)
_PLAY.lineTo(.22, .9)
_PLAY.closeSubpath()


class _PlayIconEngine(QIconEngine):
    def __init__(self, ink: Optional[QColor] = None, follow: bool = False) -> None:
        super().__init__()
        self._ink = QColor(ink) if ink is not None else None
        self._follow = follow

    def clone(self) -> "_PlayIconEngine":
        return _PlayIconEngine(self._ink, self._follow)

    def _color(self, painter: QPainter, mode) -> QColor:
        if self._follow:
            dev = painter.device()
            if isinstance(dev, QWidget):
                return QColor(dev.palette().buttonText().color())
        ink = QColor(self._ink) if self._ink is not None else to_color(palette().get('primary_text', '#ffffff'), '#ffffff')
        if mode == QIcon.Mode.Disabled:
            ink.setAlphaF(ink.alphaF() * .45)
        return ink

    def paint(self, painter: QPainter, rect: QRect, mode, state) -> None:
        ink = self._color(painter, mode)
        side = min(rect.width(), rect.height())
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.translate(rect.x() + (rect.width() - side) / 2, rect.y() + (rect.height() - side) / 2)
        painter.scale(side, side)
        painter.setPen(QPen(ink, .1, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
        painter.setBrush(ink)
        painter.drawPath(_PLAY)
        painter.restore()

    def pixmap(self, size: QSize, mode, state) -> QPixmap:
        pm = QPixmap(size)
        pm.fill(Qt.GlobalColor.transparent)
        p = QPainter(pm)
        self.paint(p, QRect(QPoint(0, 0), size), mode, state)
        p.end()
        return pm


def _play_icon(ink: Optional[QColor] = None, follow: bool = False) -> QIcon:
    return QIcon(_PlayIconEngine(ink, follow))


def _heart_icon() -> QIcon:
    ink = to_color(palette().get('primary_text', '#ffffff'), '#ffffff')
    key = ('heart', ink.rgba())
    cached = _icon_cache.get(key)
    if cached is not None:
        return cached
    pm = QPixmap(96, 96)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.scale(96, 96)
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(ink)
    p.drawPath(_HEART)
    p.end()
    icon = _icon_cache[key] = QIcon(pm)
    return icon


_ALL = "__all__"


class _TVTabPlaceholder(QWidget):
    def __init__(self, icon: FluentIcon, text_key: str, parent=None):
        super().__init__(parent)
        self._text_key = text_key
        layout = QVBoxLayout(self)
        layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.setSpacing(12)
        icon_widget = IconWidget(icon, self)
        icon_widget.setFixedSize(48, 48)
        layout.addWidget(icon_widget, 0, Qt.AlignmentFlag.AlignHCenter)
        self._label = BodyLabel(tr(text_key))
        self._label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self._label, 0, Qt.AlignmentFlag.AlignHCenter)

    def apply_locale(self) -> None:
        self._label.setText(tr(self._text_key))


class _AddSourceDialog(MessageBoxBase):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.widget.setMinimumWidth(480)
        self.viewLayout.setSpacing(10)

        title = SubtitleLabel(tr("tv.channels.add_source", default="Add source"), self)
        hint = CaptionLabel(tr("tv.channels.add_source_hint",
                               default="Add an IPTV playlist from a link or from a file on this computer."), self)
        apply_label_theme(hint, "muted")
        self.viewLayout.addWidget(title)
        self.viewLayout.addWidget(hint)

        self._mode = SegmentedWidget(self)
        self._mode.addItem("url", tr("tv.channels.mode_link", default="Link"), onClick=lambda: self._set_mode(0))
        self._mode.addItem("file", tr("tv.channels.mode_file", default="Local file"), onClick=lambda: self._set_mode(1))
        self._mode.setCurrentItem("url")
        self.viewLayout.addWidget(self._mode)

        self.viewLayout.addWidget(StrongBodyLabel(tr("tv.channels.source_name", default="Name"), self))
        self._name_edit = LineEdit(self)
        self._name_edit.setClearButtonEnabled(True)
        self._name_edit.setPlaceholderText(tr("tv.channels.source_name_ph", default="My playlist (optional)"))
        self.viewLayout.addWidget(self._name_edit)

        self._pages = QStackedWidget(self)
        url_page, file_page = QWidget(), QWidget()
        ul = QVBoxLayout(url_page)
        ul.setContentsMargins(0, 0, 0, 0)
        ul.setSpacing(6)
        ul.addWidget(StrongBodyLabel(tr("tv.channels.source_url", default="M3U URL or path"), url_page))
        self._url_edit = LineEdit(url_page)
        self._url_edit.setClearButtonEnabled(True)
        self._url_edit.setPlaceholderText("https://example.com/playlist.m3u8")
        ul.addWidget(self._url_edit)
        fl = QVBoxLayout(file_page)
        fl.setContentsMargins(0, 0, 0, 0)
        fl.setSpacing(6)
        fl.addWidget(StrongBodyLabel(tr("tv.channels.source_file", default="Playlist file"), file_page))
        row = QHBoxLayout()
        row.setSpacing(8)
        self._file_edit = LineEdit(file_page)
        self._file_edit.setReadOnly(True)
        self._file_edit.setPlaceholderText(tr("tv.channels.source_file_ph", default="No file selected"))
        browse = PushButton(FluentIcon.FOLDER, tr("tv.channels.browse", default="Browse"), file_page)
        browse.clicked.connect(self._on_browse)
        row.addWidget(self._file_edit, 1)
        row.addWidget(browse)
        fl.addLayout(row)
        self._pages.addWidget(url_page)
        self._pages.addWidget(file_page)
        self.viewLayout.addWidget(self._pages)

        self._error = CaptionLabel("", self)
        self._error.setWordWrap(True)
        self._error.setStyleSheet(f"color: {_pal_color('state_error', '#e5484d').name()};")
        self._error.hide()
        self.viewLayout.addWidget(self._error)

        self.yesButton.setText(tr("tv.channels.add", default="Add"))
        self.cancelButton.setText(tr("tv.channels.cancel", default="Cancel"))
        self.yesButton.setEnabled(False)
        self._url_edit.textChanged.connect(self._refresh)
        self._file_edit.textChanged.connect(self._refresh)
        self._url_edit.returnPressed.connect(self.yesButton.click)

    def _set_mode(self, index: int) -> None:
        self._pages.setCurrentIndex(index)
        self._refresh()

    def _location(self) -> str:
        edit = self._file_edit if self._pages.currentIndex() == 1 else self._url_edit
        return edit.text().strip()

    def _refresh(self, *_a) -> None:
        self._error.hide()
        self.yesButton.setEnabled(bool(self._location()))

    def _on_browse(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, tr("tv.channels.browse_file", default="Import local file..."), "", "M3U (*.m3u *.m3u8)")
        if path:
            self._file_edit.setText(path)

    def validate(self) -> bool:
        loc = self._location()
        ok = os.path.isfile(loc) if self._pages.currentIndex() == 1 else (
            loc.lower().startswith(("http://", "https://")) or os.path.isfile(loc))
        if not ok:
            self._error.setText(tr("tv.channels.source_invalid", default="Enter a valid http(s) link or an existing file.")
                                if self._pages.currentIndex() == 0
                                else tr("tv.channels.source_file_missing", default="That file doesn't exist."))
            self._error.show()
        return ok

    def result_source(self) -> Optional[dict]:
        loc = self._location()
        if not loc:
            return None
        name = self._name_edit.text().strip()
        if os.path.isfile(loc):
            return channels_backend.sources.add_file_source(name or os.path.splitext(os.path.basename(loc))[0], loc)
        from urllib.parse import urlparse
        return channels_backend.sources.add_url_source(name or (urlparse(loc).hostname or loc), loc)


class _ManageSourcesDialog(MessageBoxBase):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.changed = False
        self.widget.setMinimumWidth(460)
        self.viewLayout.setSpacing(10)
        self.viewLayout.addWidget(SubtitleLabel(tr("tv.channels.manage_sources", default="Manage sources"), self))
        hint = CaptionLabel(tr("tv.channels.manage_hint", default="Untick a source to hide its channels."), self)
        apply_label_theme(hint, "muted")
        self.viewLayout.addWidget(hint)

        self._list = ListWidget(self)
        self._list.setMinimumHeight(200)
        self.viewLayout.addWidget(self._list, 1)
        self._empty = BodyLabel(tr("tv.channels.no_sources", default="No sources added yet."), self)
        self._empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        apply_label_theme(self._empty, "muted")
        self.viewLayout.addWidget(self._empty)

        self._remove_btn = PushButton(FluentIcon.DELETE, tr("tv.channels.remove_source", default="Remove selected"), self)
        self._remove_btn.clicked.connect(self._on_remove)
        self.viewLayout.addWidget(self._remove_btn, 0, Qt.AlignmentFlag.AlignLeft)
        self._list.currentRowChanged.connect(lambda r: self._remove_btn.setEnabled(r >= 0))

        self.yesButton.setText(tr("tv.channels.close", default="Done"))
        self.cancelButton.hide()
        self._reload()

    def _reload(self) -> None:
        self._list.blockSignals(True)
        self._list.clear()
        for source in channels_backend.sources.list_sources():
            item = QListWidgetItem(f"{source['name']} ({source['kind']})")
            item.setData(Qt.ItemDataRole.UserRole, source["id"])
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(Qt.CheckState.Checked if source.get("enabled", True) else Qt.CheckState.Unchecked)
            self._list.addItem(item)
        self._list.blockSignals(False)
        try:
            self._list.itemChanged.disconnect(self._on_item_changed)
        except (TypeError, RuntimeError):
            pass
        self._list.itemChanged.connect(self._on_item_changed)
        has = self._list.count() > 0
        self._list.setVisible(has)
        self._empty.setVisible(not has)
        self._remove_btn.setEnabled(False)
        self._remove_btn.setVisible(has)

    def _on_item_changed(self, item: QListWidgetItem) -> None:
        channels_backend.sources.set_enabled(item.data(Qt.ItemDataRole.UserRole),
                                             item.checkState() == Qt.CheckState.Checked)
        self.changed = True

    def _on_remove(self) -> None:
        item = self._list.currentItem()
        if item is None:
            return
        channels_backend.sources.remove_source(item.data(Qt.ItemDataRole.UserRole))
        self.changed = True
        self._reload()


_SOURCE_NAMES = {"torrentio": "Torrentio", "apibay": "The Pirate Bay",
                 "tbhb": "The Black Hole Bay", "addon": "Addon"}


class _StreamPickerDialog(MessageBoxBase):
    _COLUMNS = (
        ("tv.stream.col_name", "Name", 0),
        ("tv.stream.col_type", "Type", 72),
        ("tv.stream.col_quality", "Quality", 94),
        ("tv.stream.col_size", "Size", 94),
        ("tv.stream.col_peers", "Peers", 86),
        ("tv.stream.col_source", "Source", 150),
    )

    def __init__(self, sources: list, title: str, parent=None, action: str = "play") -> None:
        super().__init__(parent)
        self._pirachest_no_sfx = True
        self._sources = list(sources)

        self.titleLabel = SubtitleLabel(tr("tv.download.pick_title", default="Choose a source to download")
                                        if action == "download"
                                        else tr("tv.stream.pick_title", default="Choose a stream"), self)
        self.viewLayout.addWidget(self.titleLabel)

        for_label = StrongBodyLabel(title, self)
        for_label.setWordWrap(True)
        self.viewLayout.addWidget(for_label)

        hint = CaptionLabel(tr("tv.download.pick_hint",
                               default="You pick exactly which files to grab on the next step.") if action == "download"
                            else tr("tv.stream.pick_hint",
                                    default="Direct sources start instantly. For torrents, more seeders stream more smoothly."), self)
        hint.setWordWrap(True)
        apply_label_theme(hint, "muted")
        self.viewLayout.addWidget(hint)
        safety = assess_source_files(
            [source.label or source.name for source in self._sources
             if not getattr(source, "is_direct", False)], context="video")
        if safety.level != "ok":
            warning = CaptionLabel(f"{safety.summary}. {tr('tbhb.file_type_review')}", self)
            warning.setWordWrap(True)
            warning.setToolTip(safety.details)
            warning.setStyleSheet(f"color: {palette()['state_verifying']};")
            self.viewLayout.addWidget(warning)

        self._table = TableWidget(self)
        self._table.setBorderVisible(True)
        self._table.setBorderRadius(8)
        self._table.setWordWrap(False)
        self._table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self._table.setAlternatingRowColors(False)
        self._table.setColumnCount(len(self._COLUMNS))
        self._table.setHorizontalHeaderLabels([tr(key, default=default) for key, default, _w in self._COLUMNS])
        header = self._table.horizontalHeader()
        header.setStretchLastSection(False)
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for column, (_key, _default, width) in enumerate(self._COLUMNS[1:], start=1):
            header.setSectionResizeMode(column, QHeaderView.ResizeMode.Fixed)
            self._table.setColumnWidth(column, width)
        self._table.verticalHeader().setVisible(False)
        self._table.verticalHeader().setDefaultSectionSize(38)
        self._table.setRowCount(len(self._sources))
        for row, source in enumerate(self._sources):
            self._fill_row(row, source)
        if self._sources:
            self._table.selectRow(0)
            self._table.setCurrentCell(0, 0)
        self._table.cellDoubleClicked.connect(lambda _row, _col: self.accept())
        self._table.setMinimumHeight(340)
        self.viewLayout.addWidget(self._table)

        if action == "download":
            self.yesButton.setText(tr("tv.download.pick", default="Download"))
            self.yesButton.setIcon(FluentIcon.DOWNLOAD)
        else:
            self.yesButton.setText(tr("tv.stream.play", default="Play"))
            self.yesButton.setIcon(_play_icon(follow=True))
        self.cancelButton.setText(tr("tv.stream.cancel", default="Cancel"))
        self.widget.setMinimumWidth(760)

    def _fill_row(self, row: int, source) -> None:
        label = source.label or source.name or source.info_hash
        info = classify_file(label)
        name = QTableWidgetItem(label)
        name.setData(Qt.ItemDataRole.UserRole, row)
        type_item = QTableWidgetItem(info.label)
        type_item.setToolTip(info.reason)
        if info.risk != "ok":
            type_item.setForeground(QColor(palette()["state_error" if info.risk == "dangerous"
                                                    else "state_verifying"]))
        source_item = QTableWidgetItem(source.indexer or _SOURCE_NAMES.get(source.source, source.source or "\u2014"))
        if source.detail:
            source_item.setToolTip(source.detail)
        cells = (
            (name, Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft),
            (type_item, Qt.AlignmentFlag.AlignCenter),
            (QTableWidgetItem(source.quality or "\u2014"), Qt.AlignmentFlag.AlignCenter),
            (QTableWidgetItem(stream_backend.human_size(source.size) if source.size else "\u2014"),
             Qt.AlignmentFlag.AlignCenter),
            (QTableWidgetItem("Direct" if getattr(source, "is_direct", False)
                              else (str(source.seeders) if source.seeders else "\u2014")),
             Qt.AlignmentFlag.AlignCenter),
            (source_item, Qt.AlignmentFlag.AlignCenter),
        )
        for column, (item, alignment) in enumerate(cells):
            item.setTextAlignment(alignment)
            self._table.setItem(row, column, item)

    def chosen(self):
        row = self._table.currentRow()
        return self._sources[row] if 0 <= row < len(self._sources) else None


class _TorrentFilesDialog(MessageBoxBase):
    def __init__(self, entries: list, title: str, parent=None,
                 preset_season: Optional[int] = None, preset_episode: Optional[int] = None,
                 safety=None) -> None:
        super().__init__(parent)
        self._items: list = []

        self.titleLabel = SubtitleLabel(tr("tv.download.files_title", default="Files to download"), self)
        self.viewLayout.addWidget(self.titleLabel)

        for_label = StrongBodyLabel(title, self)
        for_label.setWordWrap(True)
        self.viewLayout.addWidget(for_label)

        if safety is not None and safety.level != "ok":
            warning = CaptionLabel(f"{safety.summary}. {tr('tbhb.file_type_review')}", self)
            warning.setWordWrap(True)
            warning.setToolTip(safety.details)
            warning.setStyleSheet(f"color: {palette()['state_error' if safety.level == 'dangerous' else 'state_verifying']};")
            self.viewLayout.addWidget(warning)

        self._all_chk = CheckBox(tr("tv.download.select_all", default="Select all"), self)
        self._all_chk.setChecked(True)
        self._all_chk.stateChanged.connect(self._on_select_all)
        self.viewLayout.addWidget(self._all_chk)

        self._tree = TreeWidget(self)
        self._tree.setHeaderLabels([tr("tv.download.col_file", default="File"),
                                    tr("tv.download.col_type", default="Type"),
                                    tr("repacks.size", default="Size")])
        header = self._tree.header()
        header.setStretchLastSection(False)
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Fixed)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Fixed)
        self._tree.setColumnWidth(1, 72)
        self._tree.setColumnWidth(2, 92)
        self._tree.setTextElideMode(Qt.TextElideMode.ElideMiddle)
        self._name_delegate = ScrollingTextDelegate(self._tree, 0, padding=4)
        self._tree.setBorderVisible(True)
        self._tree.setBorderRadius(8)
        self._tree.setMinimumHeight(300)
        self._tree.itemChanged.connect(self._on_item_changed)
        self.viewLayout.addWidget(self._tree)
        self._build_tree(entries, preset_season, preset_episode)

        self.yesButton.setText(tr("tv.download.pick", default="Download"))
        self.yesButton.setIcon(FluentIcon.DOWNLOAD)
        self.cancelButton.setText(tr("tv.stream.cancel", default="Cancel"))
        self.widget.setMinimumWidth(640)

    @staticmethod
    def _label(entry: dict) -> str:
        if entry.get("episode") is not None:
            head = (f"S{int(entry['season']):02d}E{int(entry['episode']):02d}"
                    if entry.get("season") is not None else f"E{int(entry['episode']):02d}")
            return f"{head}  ·  {entry['name']}"
        return entry["name"]

    @staticmethod
    def _preselected(entry: dict, preset_season, preset_episode) -> bool:
        if preset_episode is not None:
            if entry.get("episode") != preset_episode:
                return False
            return entry.get("season") in (None, preset_season)
        if preset_season is not None:
            return entry.get("season") == preset_season
        return True

    def _build_tree(self, entries: list, preset_season, preset_episode) -> None:
        self._tree.blockSignals(True)
        self._tree.clear()
        self._items = []

        ordered = sorted(entries, key=lambda e: (e.get("season") is None, e.get("season") or 0,
                                                 e.get("episode") or 0, e["name"].lower()))
        for entry in ordered:
            info = classify_file(entry["name"])
            item = QTreeWidgetItem(self._tree, [self._label(entry), info.label,
                                                stream_backend.human_size(entry["size"])])
            item.setData(0, Qt.ItemDataRole.UserRole, entry["index"])
            item.setToolTip(1, info.reason)
            if info.risk != "ok":
                item.setForeground(1, QColor(palette()["state_error" if info.risk == "dangerous"
                                                    else "state_verifying"]))
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(0, Qt.CheckState.Checked if self._preselected(entry, preset_season, preset_episode)
                               else Qt.CheckState.Unchecked)
            item.setTextAlignment(1, Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignCenter)
            item.setTextAlignment(2, Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignRight)
            self._items.append((entry, item))
        self._tree.blockSignals(False)
        self._sync_select_all()

    def _on_select_all(self, _state=None) -> None:
        checked = Qt.CheckState.Checked if self._all_chk.isChecked() else Qt.CheckState.Unchecked
        self._tree.blockSignals(True)
        for _entry, item in self._items:
            item.setCheckState(0, checked)
        self._tree.blockSignals(False)

    def _on_item_changed(self, *_args) -> None:
        self._sync_select_all()

    def _sync_select_all(self) -> None:
        states = {item.checkState(0) for _entry, item in self._items}
        self._all_chk.blockSignals(True)
        self._all_chk.setChecked(bool(states) and states == {Qt.CheckState.Checked})
        self._all_chk.blockSignals(False)

    def selected_indices(self) -> list:
        return sorted(entry["index"] + 1 for entry, item in self._items
                      if item.checkState(0) == Qt.CheckState.Checked)

    def validate(self) -> bool:
        if self.selected_indices():
            return True
        InfoBar.warning(title=tr("tv.download.empty_title", default="Nothing selected"),
                        content=tr("tv.download.empty_body", default="Tick at least one file to download."),
                        duration=3000, parent=self)
        return False


_MAXW = 16777215
_ROW_H = 58
_PREFETCH = 8
_AL = Qt.AlignmentFlag


def _pal_color(key: str, fallback: str) -> QColor:
    try:
        return to_color(palette()[key], fallback)
    except Exception:
        return QColor(fallback)


def _theme_sig():
    try:
        p = palette()
        return (p.get("primary_text"), p.get("accent"), p.get("hover_tint"))
    except Exception:
        return None


def _initials(name: str) -> str:
    parts = [w for w in name.replace("-", " ").split() if w[:1].isalnum()]
    if not parts:
        return "?"
    return (parts[0][0] + (parts[1][0] if len(parts) > 1 else "")).upper()


def _paint_tile(p: QPainter, rect: QRect, name: str, pix, tile_bg: QColor, font: QFont) -> None:
    p.setPen(Qt.PenStyle.NoPen)
    if pix is not None and not pix.isNull():
        p.setBrush(tile_bg)
        p.drawRoundedRect(rect, 9, 9)
        inner = rect.adjusted(5, 5, -5, -5)
        sz = pix.deviceIndependentSize()
        sw, sh = max(1.0, sz.width()), max(1.0, sz.height())
        k = min(inner.width() / sw, inner.height() / sh)
        w, h = sw * k, sh * k
        p.drawPixmap(QRectF(inner.x() + (inner.width() - w) / 2, inner.y() + (inner.height() - h) / 2, w, h),
                     pix, QRectF(pix.rect()))
        return
    p.setBrush(QColor.fromHsl(zlib.crc32(name.encode("utf-8", "ignore")) % 360, 120, 92))
    p.drawRoundedRect(rect, 9, 9)
    p.setPen(QColor(255, 255, 255, 235))
    p.setFont(font)
    p.drawText(rect, _AL.AlignCenter, _initials(name))


class _PixLRU:
    __slots__ = ("_d", "_max")

    def __init__(self, cap: int = 320) -> None:
        self._d: "OrderedDict[str, Any]" = OrderedDict()
        self._max = cap

    def get(self, k):
        v = self._d.get(k)
        if v is not None:
            self._d.move_to_end(k)
        return v

    def has(self, k) -> bool:
        return k in self._d

    def put(self, k, v) -> None:
        d = self._d
        d[k] = v
        d.move_to_end(k)
        while len(d) > self._max:
            d.popitem(last=False)

    def clear(self) -> None:
        self._d.clear()


_NULL_INDEX = QModelIndex()


class _ChannelModel(QAbstractListModel):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.rows: list[dict] = []

    def rowCount(self, parent=_NULL_INDEX) -> int:
        return 0 if parent.isValid() else len(self.rows)

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if role == Qt.ItemDataRole.DisplayRole and index.isValid():
            return self.rows[index.row()].get("name") or ""
        return None

    def set_rows(self, rows: list[dict]) -> None:
        self.beginResetModel()
        self.rows = rows
        self.endResetModel()


class _ChannelDelegate(QStyledItemDelegate):
    def __init__(self, view, model: _ChannelModel, logos: _PixLRU) -> None:
        super().__init__(view)
        self._model, self._logos = model, logos
        self.playing_id = None
        self._name_font = QFont()
        self._name_font.setPointSizeF(10.5)
        self._name_font.setWeight(QFont.Weight.DemiBold)
        self._meta_font = QFont()
        self._meta_font.setPointSizeF(8.5)
        self._tile_font = QFont()
        self._tile_font.setPointSizeF(10)
        self._tile_font.setBold(True)
        self._fm_name = QFontMetrics(self._name_font)
        self._fm_meta = QFontMetrics(self._meta_font)
        self._colors()

    def _colors(self) -> None:
        self._sig = _theme_sig()
        self._c_text = _pal_color("primary_text", "#eaeaea")
        self._c_muted = _pal_color("muted", "#9a9a9a")
        self._c_hover = _pal_color("hover_tint", "#10ffffff")
        self._c_sel = _pal_color("surface_tint_strong", "#18ffffff")
        self._c_tile = _pal_color("surface_tint", "#14ffffff")


        self._c_play = QColor(96, 205, 255, 42)
        self._c_play_bar = QColor(96, 205, 255)

    def setHoverRow(self, *_a) -> None: pass
    def setPressedRow(self, *_a) -> None: pass
    def setSelectedRows(self, *_a) -> None: pass

    def sizeHint(self, option, index) -> QSize:
        return QSize(0, _ROW_H)

    def paint(self, p, option, index) -> None:
        if self._sig != _theme_sig():
            self._colors()
        rows, row = self._model.rows, index.row()
        if row >= len(rows):
            return
        ch = rows[row]
        r = option.rect.adjusted(4, 2, -4, -2)
        st = option.state
        playing = self.playing_id is not None and ch.get("id") == self.playing_id
        p.save()
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        p.setPen(Qt.PenStyle.NoPen)
        bg = (self._c_play if playing else self._c_sel if st & QStyle.StateFlag.State_Selected
              else self._c_hover if st & QStyle.StateFlag.State_MouseOver else None)
        if bg is not None:
            p.setBrush(bg)
            p.drawRoundedRect(r, 8, 8)
        if playing:
            p.setBrush(self._c_play_bar)
            p.drawRoundedRect(QRectF(r.left(), r.top() + 14, 3, r.height() - 28), 1.5, 1.5)
        name = ch.get("name") or ""
        url = ch.get("logo")
        tile = QRect(r.left() + 12, r.top() + (r.height() - 40) // 2, 40, 40)
        _paint_tile(p, tile, name, self._logos.get(url) if url else None, self._c_tile, self._tile_font)
        x = tile.right() + 12
        w = max(10, r.right() - 10 - x)
        meta = " \u00b7 ".join(v for v in (ch.get("group"), ch.get("country")) if v)
        flags = _AL.AlignLeft | _AL.AlignVCenter
        p.setPen(self._c_text)
        p.setFont(self._name_font)
        nm = self._fm_name.elidedText(name, Qt.TextElideMode.ElideRight, w)
        if meta:
            p.drawText(QRect(x, r.top() + 7, w, 22), flags, nm)
            p.setPen(self._c_muted)
            p.setFont(self._meta_font)
            p.drawText(QRect(x, r.top() + 29, w, 18), flags,
                       self._fm_meta.elidedText(meta, Qt.TextElideMode.ElideRight, w))
        else:
            p.drawText(QRect(x, r.top(), w, r.height()), flags, nm)
        p.restore()


def _elided(base):
    class _E(base):
        _full = ""
        _ready = False

        def set_full(self, text: str) -> None:
            if not self._ready:
                self._ready = True
                self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
                self.setMinimumWidth(0)
            self._full = text
            self.setToolTip(text)
            self._elide()

        def resizeEvent(self, e) -> None:
            super().resizeEvent(e)
            self._elide()

        def _elide(self) -> None:
            self.setText(self.fontMetrics().elidedText(self._full, Qt.TextElideMode.ElideRight, max(0, self.width() - 2)))
    return _E


_ElidedStrong = _elided(StrongBodyLabel)
_ElidedCaption = _elided(CaptionLabel)


_ENGINE_LOCK = threading.Lock()


def _stop_engine_async(engine, session=None) -> None:
    def _run() -> None:
        with _ENGINE_LOCK:
            if session is not None and getattr(engine, "_session", None) is not session:
                return
            try:
                engine.stop()
            except Exception:
                logger.debug("Could not stop the stream", exc_info=True)
    threading.Thread(target=_run, daemon=True, name="stream-stop").start()


class _EmptyState(QWidget):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._cb1 = self._cb2 = None
        lay = QVBoxLayout(self)
        lay.setContentsMargins(24, 24, 24, 24)
        lay.setSpacing(10)
        lay.addStretch(1)
        self._icon = IconWidget(FluentIcon.GLOBE, self)
        self._icon.setFixedSize(44, 44)
        self._spin = _Spinner(self)
        self._title = StrongBodyLabel("", self)
        self._title.setAlignment(_AL.AlignCenter)
        self._title.setWordWrap(True)
        self._sub = CaptionLabel("", self)
        self._sub.setAlignment(_AL.AlignCenter)
        self._sub.setWordWrap(True)
        apply_label_theme(self._sub, "muted")
        self._b1 = PrimaryPushButton("", self)
        self._b2 = PushButton("", self)
        self._b1.clicked.connect(lambda: self._cb1 and self._cb1())
        self._b2.clicked.connect(lambda: self._cb2 and self._cb2())
        for w in (self._icon, self._spin, self._b1, self._b2):
            lay.addWidget(w, 0, _AL.AlignHCenter)
        lay.insertWidget(3, self._title)
        lay.insertWidget(4, self._sub)
        lay.addStretch(1)

    def show_state(self, icon=None, title="", sub="", spinner=False, btn1=None, btn2=None) -> None:
        if icon is not None:
            self._icon.setIcon(icon)
        self._icon.setVisible(icon is not None and not spinner)
        self._spin.setVisible(spinner)
        self._spin.set_active(spinner)
        self._title.setText(title)
        self._sub.setText(sub)
        self._sub.setVisible(bool(sub))
        for b, spec, attr in ((self._b1, btn1, "_cb1"), (self._b2, btn2, "_cb2")):
            b.setVisible(spec is not None)
            if spec is not None:
                b.setText(spec[0])
                setattr(self, attr, spec[1])


class _PlayerStage(QWidget):
    def __init__(self, hint: str, parent=None) -> None:
        super().__init__(parent)
        self._player = None
        self._hint = hint
        self._pm = None
        self.setMinimumSize(280, 170)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)

    def set_hint(self, text: str) -> None:
        self._hint = text
        self.update()

    def attach(self, player) -> None:
        self._player = player
        player.setParent(self)
        player.show()
        self._fit()

    def _fit(self) -> None:
        pl = self._player
        if pl is None:
            return
        bar = AnimePlayerWidget._BAR_H
        aw, ah = self.width(), self.height()
        w = max(240, min(aw, int((ah - bar) * 16 / 9)))
        h = int(w * 9 / 16) + bar
        pl.setFixedSize(w, h)
        pl.move((aw - w) // 2, max(0, (ah - h) // 2))

    def resizeEvent(self, e) -> None:
        super().resizeEvent(e)
        self._fit()

    def paintEvent(self, _e) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        if self._player is not None:
            return
        if self._pm is None:
            self._pm = _wicon("VIDEO", FluentIcon.PLAY).pixmap(44, 44)
        cx, cy = self.width() // 2, self.height() // 2
        p.setOpacity(0.5)
        p.drawPixmap(cx - 22, cy - 40, self._pm)
        p.setOpacity(1.0)
        p.setPen(_pal_color("muted", "#9a9a9a"))
        p.drawText(QRect(0, cy + 12, self.width(), 24), _AL.AlignHCenter | _AL.AlignTop, self._hint)


class _LogoTile(QWidget):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setFixedSize(44, 44)
        self._name, self._pix = "", None
        self._font = QFont()
        self._font.setPointSizeF(10.5)
        self._font.setBold(True)

    def set_channel(self, name: str, pix) -> None:
        self._name, self._pix = name, pix
        self.update()

    def paintEvent(self, _e) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        _paint_tile(p, self.rect(), self._name, self._pix, _pal_color("surface_tint", "#14ffffff"), self._font)


class _NowPlaying(QWidget):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(2, 0, 2, 0)
        lay.setSpacing(12)
        self._tile = _LogoTile(self)
        lay.addWidget(self._tile)
        col = QVBoxLayout()
        col.setSpacing(1)
        self._name = _ElidedStrong("", self)
        self._meta = _ElidedCaption("", self)
        apply_label_theme(self._name, "primary_text")
        apply_label_theme(self._meta, "muted")
        col.addWidget(self._name)
        col.addWidget(self._meta)
        lay.addLayout(col, 1)
        self._live = QLabel(tr("tv.channels.live", default="LIVE"), self)
        self._live.setStyleSheet("background:#e5484d;color:#fff;border-radius:4px;padding:2px 8px;"
                                 "font:700 10px 'Segoe UI';")
        lay.addWidget(self._live, 0, _AL.AlignVCenter)

    def set_channel(self, ch: dict, pix) -> None:
        name = ch.get("name") or ""
        self._name.set_full(name)
        self._meta.set_full(" \u00b7 ".join(v for v in (ch.get("group"), ch.get("country")) if v))
        self._tile.set_channel(name, pix)

    def set_pix(self, pix) -> None:
        self._tile.set_channel(self._tile._name, pix)

    def retext(self) -> None:
        self._live.setText(tr("tv.channels.live", default="LIVE"))


class _CountryModel(QAbstractListModel):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.names: list[str] = []
        self.low: list[str] = []
        self.codes: list[str] = []
        self.on: list[bool] = []

    def rowCount(self, parent=_NULL_INDEX) -> int:
        return 0 if parent.isValid() else len(self.names)

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if role == Qt.ItemDataRole.DisplayRole and index.isValid():
            return self.names[index.row()]
        return None

    def load(self, countries: list[dict], selected: Optional[set]) -> None:
        items = sorted(countries, key=lambda c: c["name"].lower())
        self.beginResetModel()
        self.names = [c["name"] for c in items]
        self.low = [n.lower() for n in self.names]
        self.codes = [c["code"] for c in items]
        self.on = [selected is None or c["code"] in selected for c in items]
        self.endResetModel()

    def set_row(self, row: int, val: bool) -> None:
        if self.on[row] != val:
            self.on[row] = val
            i = self.index(row)
            self.dataChanged.emit(i, i)


class _CountryDelegate(QStyledItemDelegate):
    def __init__(self, view, model: _CountryModel) -> None:
        super().__init__(view)
        self._m = model
        self._colors()

    def _colors(self) -> None:
        self._sig = _theme_sig()
        self._c_hover = _pal_color("hover_tint", "#10ffffff")
        self._c_acc = _pal_color("accent", "#0078D4")
        self._c_text = _pal_color("primary_text", "#eaeaea")
        self._c_muted = _pal_color("muted", "#9a9a9a")

    def setHoverRow(self, *_a) -> None: pass
    def setPressedRow(self, *_a) -> None: pass
    def setSelectedRows(self, *_a) -> None: pass

    def sizeHint(self, option, index) -> QSize:
        return QSize(0, 38)

    def paint(self, p, option, index) -> None:
        if self._sig != _theme_sig():
            self._colors()
        row, m = index.row(), self._m
        r = option.rect.adjusted(2, 1, -2, -1)
        p.save()
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        if option.state & QStyle.StateFlag.State_MouseOver or option.state & QStyle.StateFlag.State_Selected:
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(self._c_hover)
            p.drawRoundedRect(r, 6, 6)
        box = QRectF(r.left() + 10, r.top() + (r.height() - 18) / 2, 18, 18)
        on = m.on[row]
        acc = self._c_acc
        if on:
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(acc)
            p.drawRoundedRect(box, 4, 4)
            pen = QPen(QColor("#fff"), 2)
            pen.setCapStyle(Qt.PenCapStyle.RoundCap)
            pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
            p.setPen(pen)
            p.setBrush(Qt.BrushStyle.NoBrush)
            path = QPainterPath()
            path.moveTo(box.x() + 4.5, box.y() + 9.5)
            path.lineTo(box.x() + 7.7, box.y() + 12.7)
            path.lineTo(box.x() + 13.5, box.y() + 5.8)
            p.drawPath(path)
        else:
            p.setPen(QPen(self._c_muted, 1.5))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawRoundedRect(box.adjusted(0.75, 0.75, -0.75, -0.75), 4, 4)
        p.setPen(self._c_text)
        tr_ = QRect(int(box.right()) + 12, r.top(), r.width() - 60, r.height())
        p.drawText(tr_, _AL.AlignLeft | _AL.AlignVCenter,
                   p.fontMetrics().elidedText(m.names[row], Qt.TextElideMode.ElideRight, tr_.width()))
        code = m.codes[row]
        if code and len(code) <= 3:
            p.setPen(self._c_muted)
            p.drawText(QRect(r.right() - 44, r.top(), 36, r.height()), _AL.AlignRight | _AL.AlignVCenter, code.upper())
        p.restore()


class _CheckListView(ListView):
    row_toggled = Signal(int)

    def keyPressEvent(self, e) -> None:
        if e.key() in (Qt.Key.Key_Space, Qt.Key.Key_Return, Qt.Key.Key_Enter):
            idx = self.currentIndex()
            if idx.isValid():
                self.row_toggled.emit(idx.row())
                e.accept()
                return
        super().keyPressEvent(e)


class _CountrySelectionDialog(MessageBoxBase):
    retry_requested = Signal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.widget.setMinimumSize(420, 560)
        lay = self.viewLayout
        lay.setSpacing(10)

        title = SubtitleLabel(tr("tv.channels.select_countries", default="Select countries"), self)
        hint = CaptionLabel(tr("tv.channels.select_countries_hint",
                               default="Channels are only loaded after you press OK."), self)
        apply_label_theme(hint, "muted")
        lay.addWidget(title)
        lay.addWidget(hint)

        self._controls = QWidget(self)
        cl = QVBoxLayout(self._controls)
        cl.setContentsMargins(0, 0, 0, 0)
        cl.setSpacing(8)
        self._search = SearchLineEdit(self._controls)
        self._search.setPlaceholderText(tr("tv.channels.search_countries", default="Search countries..."))
        self._search.textChanged.connect(self._on_search)
        cl.addWidget(self._search)
        row = QHBoxLayout()
        row.setSpacing(6)
        for text, val in ((tr("tv.channels.select_all", default="Select all"), True),
                          (tr("tv.channels.select_none", default="Select none"), False)):
            b = PushButton(text, self._controls)
            b.setAutoDefault(False)
            b.clicked.connect(lambda _=False, v=val: self._set_visible(v))
            row.addWidget(b)
        row.addStretch(1)
        self._summary = CaptionLabel("", self._controls)
        apply_label_theme(self._summary, "muted")
        row.addWidget(self._summary)
        cl.addLayout(row)
        lay.addWidget(self._controls)
        self._controls.setEnabled(False)

        self._pages = QStackedWidget(self)
        self._pages.setMinimumHeight(320)
        loading = QWidget()
        ll = QVBoxLayout(loading)
        ll.setAlignment(_AL.AlignCenter)
        self._spin = IndeterminateProgressRing(loading)
        self._spin.setFixedSize(36, 36)
        self._spin.setStrokeWidth(3)
        ll.addWidget(self._spin, 0, _AL.AlignHCenter)
        ll.addWidget(BodyLabel(tr("tv.channels.loading_countries", default="Loading country list..."), loading),
                     0, _AL.AlignHCenter)
        self._model = _CountryModel(self)
        self._view = _CheckListView(self)
        self._view.setModel(self._model)
        self._view.setItemDelegate(_CountryDelegate(self._view, self._model))
        self._view.setUniformItemSizes(True)
        self._view.setFrameShape(QFrame.Shape.NoFrame)
        self._view.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._view.setMouseTracking(True)
        self._view.clicked.connect(lambda i: self._toggle(i.row()))
        self._view.row_toggled.connect(self._toggle)
        err = QWidget()
        el = QVBoxLayout(err)
        el.setAlignment(_AL.AlignCenter)
        self._err_lbl = BodyLabel("", err)
        self._err_lbl.setWordWrap(True)
        self._err_lbl.setAlignment(_AL.AlignCenter)
        retry = PushButton(tr("tv.channels.retry", default="Try again"), err)
        retry.setAutoDefault(False)
        retry.clicked.connect(self.retry_requested.emit)
        el.addWidget(self._err_lbl)
        el.addWidget(retry, 0, _AL.AlignHCenter)
        for w in (loading, self._view, err):
            self._pages.addWidget(w)
        lay.addWidget(self._pages, 1)


        self._ok = self.yesButton
        self._ok.setText(tr("tv.channels.ok", default="OK"))
        self.cancelButton.setText(tr("tv.channels.cancel", default="Cancel"))
        self._ok.setEnabled(False)

    def set_loading(self) -> None:
        self._controls.setEnabled(False)
        self._ok.setEnabled(False)
        self._pages.setCurrentIndex(0)
        self._spin.start()

    def set_error(self, msg: str) -> None:
        self._spin.stop()
        self._err_lbl.setText(msg)
        self._pages.setCurrentIndex(2)

    def set_countries(self, countries: list[dict], selected: Optional[set]) -> None:
        self._spin.stop()
        self._model.load(countries, selected)
        self._controls.setEnabled(True)
        self._pages.setCurrentIndex(1)
        self._update_summary()

    def _toggle(self, row: int) -> None:
        self._model.set_row(row, not self._model.on[row])
        self._update_summary()

    def _set_visible(self, val: bool) -> None:
        for i in range(self._model.rowCount()):
            if not self._view.isRowHidden(i):
                self._model.set_row(i, val)
        self._update_summary()

    def _on_search(self, text: str) -> None:
        q = text.strip().lower()
        for i, n in enumerate(self._model.low):
            self._view.setRowHidden(i, bool(q) and q not in n)

    def _update_summary(self) -> None:
        n = sum(self._model.on)
        self._summary.setText(tr("tv.channels.n_selected", default="{n} selected").format(n=n))
        self._ok.setEnabled(n > 0)

    def selected_codes(self) -> list[str]:
        m = self._model
        return [c for c, on in zip(m.codes, m.on, strict=True) if on]


def _fetch_indexed(use_cache: bool = True) -> list:
    chans = channels_backend.fetch_all_channels(use_cache=use_cache) or []
    return [chans,
            sorted({c["country"] for c in chans if c.get("country")}),
            sorted({c["group"] for c in chans if c.get("group")})]


class _ChannelsTab(QWidget):
    _COMPACT_W = 880

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._all: list[dict] = []
        self._rows: list[dict] = []
        self._keys: Optional[list[str]] = None
        self._countries: list[str] = []
        self._groups_all: list[str] = []
        self._group_cache: dict = {}
        self._state = "idle"
        self._gen = 0
        self._load_task: Optional[str] = None
        self._prompting = False
        self._loaded = False
        self._current: Optional[dict] = None
        self._player: Optional[AnimePlayerWidget] = None
        self._compact: Optional[bool] = None
        self._logos = _PixLRU(320)
        self._inflight: set[str] = set()
        self._failed: set[str] = set()

        self._root = QBoxLayout(QBoxLayout.Direction.LeftToRight, self)
        self._root.setContentsMargins(0, 0, 0, 0)
        self._root.setSpacing(16)
        self._list_panel = self._build_list_panel()
        self._player_panel = self._build_player_panel()

        def _timer(ms, fn):
            t = QTimer(self)
            t.setSingleShot(True)
            t.setInterval(ms)
            t.timeout.connect(fn)
            return t

        self._filter_timer = _timer(220, self._apply_filters_now)
        self._logo_timer = _timer(90, self._ensure_visible_logos)
        self._repaint_timer = _timer(40, lambda: self._view.viewport().update())
        self._apply_mode(False)
        register_locale_refresh(self, self._apply_locale)
        self._sync_view()


    def _tool_btn(self, parent, slot: str, fallback, tip: str, fn) -> TransparentToolButton:
        b = TransparentToolButton(themed_icon(slot, fallback), parent)
        apply_tool_button_theme(b)
        apply_slot_icon_theme(b, slot, fallback)
        b.setToolTip(tip)
        b.clicked.connect(fn)
        return b

    def _combo(self, parent, fn) -> ComboBox:
        c = ComboBox(parent)
        c.setMinimumWidth(0)
        c.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        c.currentIndexChanged.connect(fn)
        return c

    def _build_list_panel(self) -> QWidget:
        panel = QWidget(self)
        lay = QVBoxLayout(panel)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(8)

        bar = QHBoxLayout()
        bar.setSpacing(4)
        self._search = SearchLineEdit(panel)
        self._search.setPlaceholderText(tr("tv.channels.search_placeholder", default="Search channels..."))
        self._search.textChanged.connect(lambda _t: self._filter_timer.start())
        self._search.searchSignal.connect(self._apply_filters_now)
        self._search.clearSignal.connect(self._apply_filters_now)
        bar.addWidget(self._search, 1)
        self._countries_btn = self._tool_btn(panel, 'tv.channels_filter', _icon("GLOBE", FluentIcon.FILTER),
                                             tr("tv.channels.select_countries", default="Select countries"),
                                             self._prompt_countries)
        self._sources_btn = self._tool_btn(panel, 'tv.channels_manage', FluentIcon.SETTING,
                                           tr("tv.channels.sources", default="Sources"), self._show_sources_menu)
        self._refresh_btn = self._tool_btn(panel, 'tv.channels_refresh', FluentIcon.SYNC,
                                           tr("tv.channels.refresh", default="Refresh"),
                                           lambda: self._load_channels(use_cache=False))
        for b in (self._countries_btn, self._sources_btn, self._refresh_btn):
            bar.addWidget(b)
        lay.addLayout(bar)

        frow = QHBoxLayout()
        frow.setSpacing(6)
        self._source_combo = self._combo(panel, self._on_facet_changed)
        self._country_combo = self._combo(panel, self._on_facet_changed)
        self._group_combo = self._combo(panel, self._apply_filters_now)
        for c in (self._source_combo, self._country_combo, self._group_combo):
            frow.addWidget(c, 1)
            c.hide()
        lay.addLayout(frow)

        self._model = _ChannelModel(self)
        self._delegate = None
        v = self._view = ListView(panel)
        v.setModel(self._model)
        self._delegate = _ChannelDelegate(v, self._model, self._logos)
        v.setItemDelegate(self._delegate)
        v.setUniformItemSizes(True)
        v.setSpacing(0)
        v.setFrameShape(QFrame.Shape.NoFrame)
        v.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        v.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        v.setMouseTracking(True)
        v.clicked.connect(self._on_row_activated)
        v.activated.connect(self._on_row_activated)
        v.verticalScrollBar().valueChanged.connect(lambda _v: self._logo_timer.start())
        self._empty = _EmptyState(panel)
        self._stack = QStackedWidget(panel)
        self._stack.addWidget(v)
        self._stack.addWidget(self._empty)
        lay.addWidget(self._stack, 1)

        self._footer = QWidget(panel)
        fl = QHBoxLayout(self._footer)
        fl.setContentsMargins(4, 0, 4, 0)
        self._count_lbl = CaptionLabel("", self._footer)
        apply_label_theme(self._count_lbl, "muted")
        self._busy = _Spinner(self._footer)
        fl.addWidget(self._count_lbl)
        fl.addStretch(1)
        fl.addWidget(self._busy)
        lay.addWidget(self._footer)
        return panel

    def _build_player_panel(self) -> QWidget:
        panel = QWidget(self)
        lay = QVBoxLayout(panel)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(10)
        self._stage = _PlayerStage(tr("tv.channels.pick_hint", default="Select a channel to start watching"), panel)
        self._info = _NowPlaying(panel)
        self._info.hide()
        lay.addWidget(self._stage, 1)
        lay.addWidget(self._info)
        return panel


    def _apply_mode(self, compact: bool) -> None:
        if compact == self._compact:
            return
        self._compact = compact
        r = self._root
        r.removeWidget(self._list_panel)
        r.removeWidget(self._player_panel)
        if compact:
            r.setDirection(QBoxLayout.Direction.TopToBottom)
            r.addWidget(self._player_panel, 1)
            r.addWidget(self._list_panel, 1)
            self._list_panel.setMinimumWidth(0)
            self._list_panel.setMaximumWidth(_MAXW)
        else:
            r.setDirection(QBoxLayout.Direction.LeftToRight)
            r.addWidget(self._list_panel, 0)
            r.addWidget(self._player_panel, 1)
            self._player_panel.setMaximumHeight(_MAXW)

    def resizeEvent(self, e) -> None:
        super().resizeEvent(e)
        w, h = self.width(), self.height()
        self._apply_mode(w < self._COMPACT_W)
        if self._compact:
            self._player_panel.setMaximumHeight(max(240, int(h * 0.5)))
        else:
            self._list_panel.setFixedWidth(max(320, min(440, int(w * 0.36))))
        self._logo_timer.start()

    def showEvent(self, e) -> None:
        super().showEvent(e)
        if not self._loaded:
            self.activate()


    def activate(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        QTimer.singleShot(0, self._first_prompt)

    def _first_prompt(self) -> None:
        if not self.isVisible():
            self._loaded = False
            return
        if channels_backend.get_selected_countries() is None:
            self._prompt_countries()
        else:
            self._load_channels(use_cache=True)

    def _prompt_countries(self) -> None:
        if self._prompting:
            return
        self._prompting = True
        dlg = _CountrySelectionDialog(self.window())
        task: dict = {"id": None}

        def _fetch() -> None:
            if task["id"] is not None:
                _worker_module.cancel(task["id"])
            dlg.set_loading()

            def _done(countries: list) -> None:
                if not _qt_is_valid(dlg):
                    return
                task["id"] = None
                if not countries:
                    dlg.set_error(tr("tv.channels.no_countries", default="No countries available."))
                    return
                sel = channels_backend.get_selected_countries()
                dlg.set_countries(countries, set(sel) if sel is not None else None)

            def _err(msg: str) -> None:
                if not _qt_is_valid(dlg):
                    return
                task["id"] = None
                logger.error("Failed to load country list: %s", msg)
                dlg.set_error(tr("tv.channels.countries_error", default="Couldn't load the country list."))

            task["id"] = _worker_module.submit(channels_backend.get_available_countries, on_done=_done, on_error=_err)

        dlg.retry_requested.connect(_fetch)
        _fetch()
        try:
            accepted = dlg.exec() == QDialog.DialogCode.Accepted
        finally:
            if task["id"] is not None:
                _worker_module.cancel(task["id"])
                task["id"] = None
            self._prompting = False
        codes = dlg.selected_codes() if accepted else []
        dlg.deleteLater()
        if accepted and codes:
            channels_backend.set_selected_countries(codes)
            self._load_channels(use_cache=True)


    def _cancel_load(self) -> None:
        self._gen += 1
        if self._load_task is not None:
            _worker_module.cancel(self._load_task)
            self._load_task = None

    def _load_channels(self, use_cache: bool = True) -> None:
        self._cancel_load()
        gen = self._gen
        self._state = "loading"
        self._sync_view()

        def _done(res: list) -> None:
            if gen != self._gen:
                return
            self._load_task = None
            self._all, self._countries, self._groups_all = res[0] or [], res[1], res[2]
            self._keys = None
            self._group_cache.clear()
            self._state = "ready"
            self._populate_filters()
            self._apply_filters_now()

        def _err(msg: str) -> None:
            if gen != self._gen:
                return
            self._load_task = None
            self._state = "error"
            logger.error("Channel load error: %s", msg)
            if self._all:
                InfoBar.error(title=tr("tv.channels.load_error", default="Failed to load channels"),
                              content="", parent=self.window(), duration=3000, isClosable=True)
            self._sync_view()

        self._load_task = _worker_module.submit(_fetch_indexed, kwargs={"use_cache": use_cache},
                                                on_done=_done, on_error=_err)


    @staticmethod
    def _facet(combo: ComboBox):
        v = combo.currentData()
        return None if v in (None, _ALL) else v

    def _fill_combo(self, combo: ComboBox, items: list[tuple[str, str]]) -> None:
        combo.blockSignals(True)
        cur = combo.currentData()
        combo.clear()
        for value, label in items:
            combo.addItem(label, userData=value)
        idx = combo.findData(cur) if cur is not None else 0
        combo.setCurrentIndex(idx if idx >= 0 else 0)
        combo.blockSignals(False)

    def _populate_filters(self) -> None:
        sources = channels_backend.sources.list_sources()
        self._fill_combo(self._source_combo, [(_ALL, tr("tv.channels.all_sources", default="All sources"))]
                         + [(s["id"], s["name"]) for s in sources])
        self._source_combo.setVisible(len(sources) > 1)
        self._fill_combo(self._country_combo, [(_ALL, tr("tv.channels.all_countries", default="All countries"))]
                         + [(c, c) for c in self._countries])
        self._country_combo.setVisible(len(self._countries) > 1)
        self._refresh_groups()

    def _refresh_groups(self) -> None:
        sf, cf = self._facet(self._source_combo), self._facet(self._country_combo)
        if sf is None and cf is None:
            groups = self._groups_all
        else:
            groups = self._group_cache.get((sf, cf))
            if groups is None:
                groups = sorted({c["group"] for c in self._all if c.get("group")
                                 and (sf is None or c["source_id"] == sf) and (cf is None or c.get("country") == cf)})
                self._group_cache[(sf, cf)] = groups
        self._fill_combo(self._group_combo, [(_ALL, tr("tv.channels.all_groups", default="All groups"))]
                         + [(g, g) for g in groups])
        self._group_combo.setVisible(bool(groups))

    def _on_facet_changed(self, *_a) -> None:
        self._refresh_groups()
        self._apply_filters_now()

    def _filters_active(self) -> bool:
        return bool(self._search.text().strip()) or any(
            self._facet(c) is not None for c in (self._source_combo, self._country_combo, self._group_combo))

    def _clear_filters(self) -> None:
        self._search.blockSignals(True)
        self._search.clear()
        self._search.blockSignals(False)
        for c in (self._source_combo, self._country_combo, self._group_combo):
            c.blockSignals(True)
            c.setCurrentIndex(0)
            c.blockSignals(False)
        self._refresh_groups()
        self._apply_filters_now()

    def _apply_filters_now(self, *_a) -> None:
        self._filter_timer.stop()
        q = self._search.text().strip().lower()
        sf, cf, gf = (self._facet(c) for c in (self._source_combo, self._country_combo, self._group_combo))
        rows = self._all
        if sf is None and cf is None and gf is None and not q:
            out = rows
        elif q:
            if self._keys is None:
                self._keys = [f"{c.get('name') or ''}\n{c.get('group') or ''}".lower() for c in rows]
            out = [c for c, k in zip(rows, self._keys, strict=True)
                   if q in k and (sf is None or c["source_id"] == sf)
                   and (cf is None or c.get("country") == cf) and (gf is None or c.get("group") == gf)]
        else:
            out = [c for c in rows if (sf is None or c["source_id"] == sf)
                   and (cf is None or c.get("country") == cf) and (gf is None or c.get("group") == gf)]
        self._rows = out
        self._model.set_rows(out)
        self._view.scrollToTop()
        self._sync_view()


    def _sync_view(self) -> None:
        st, n, E = self._state, len(self._rows), self._empty
        page = self._empty
        if st == "idle":
            try:
                saved = channels_backend.get_selected_countries() is not None
            except Exception:
                saved = False
            E.show_state(FluentIcon.GLOBE,
                         tr("tv.channels.idle_title", default="Choose countries to start"),
                         tr("tv.channels.idle_sub", default="Nothing is loaded until you confirm your countries."),
                         btn1=(tr("tv.channels.select_countries", default="Select countries"), self._prompt_countries),
                         btn2=(tr("tv.channels.use_saved", default="Use saved countries"),
                               lambda: self._load_channels(use_cache=True)) if saved else None)
        elif not self._all and st == "loading":
            E.show_state(title=tr("tv.channels.loading", default="Loading channels..."), spinner=True)
        elif not self._all and st == "error":
            E.show_state(_icon("INFO", FluentIcon.SYNC), tr("tv.channels.load_error", default="Failed to load channels"),
                         btn1=(tr("tv.channels.retry", default="Try again"), lambda: self._load_channels(use_cache=False)))
        elif n == 0:
            E.show_state(_icon("SEARCH", FluentIcon.FILTER), tr("tv.channels.no_results", default="No channels found"),
                         btn1=(tr("tv.channels.clear_filters", default="Clear filters"), self._clear_filters)
                         if self._filters_active() else None)
        else:
            page = self._view
        self._stack.setCurrentWidget(page)
        self._footer.setVisible(bool(self._all))
        self._count_lbl.setText(tr("tv.channels.count", default="{count} channels").format(count=f"{n:,}"))
        self._busy.set_active(st == "loading")
        self._refresh_btn.setEnabled(st in ("ready", "error"))
        if page is self._view:
            self._logo_timer.start()


    def _ensure_visible_logos(self) -> None:
        n = self._model.rowCount()
        if not n or not self._view.isVisible():
            return
        vp = self._view.viewport()
        top = self._view.indexAt(QPoint(vp.width() // 2, 3))
        bot = self._view.indexAt(QPoint(vp.width() // 2, vp.height() - 3))
        first = top.row() if top.isValid() else 0
        last = bot.row() if bot.isValid() else min(n - 1, first + vp.height() // _ROW_H + 1)
        rows = self._model.rows
        for i in range(max(0, first - _PREFETCH), min(n, last + _PREFETCH + 1)):
            url = rows[i].get("logo")
            if not url or url in self._inflight or url in self._failed or self._logos.has(url):
                continue
            pix = images.peek(url, _LOGO_SIZE)
            if pix is not None and not pix.isNull():
                self._logos.put(url, pix)
                continue
            self._inflight.add(url)
            images.get(url, _LOGO_SIZE, partial(self._on_logo_ready, url), owner=self)
        self._repaint_timer.start()

    def _on_logo_ready(self, url: str, pixmap) -> None:
        if not _qt_is_valid(self._view):
            return
        self._inflight.discard(url)
        if pixmap.isNull():
            if len(self._failed) > 3000:
                self._failed.clear()
            self._failed.add(url)
            return
        self._logos.put(url, pixmap)
        if self._current and self._current.get("logo") == url:
            self._info.set_pix(pixmap)
        self._repaint_timer.start()


    def _on_row_activated(self, index) -> None:
        if not index.isValid() or index.row() >= len(self._model.rows):
            return
        ch = self._model.rows[index.row()]
        if (self._current and ch.get("id") == self._current.get("id")
                and self._player is not None and self._player.has_media()):
            return
        self._play(ch)

    def _ensure_player(self) -> "AnimePlayerWidget":
        if self._player is None:
            pl = AnimePlayerWidget(self._stage)
            pl.set_live_mode(True)
            self._stage.attach(pl)
            self._player = pl
        return self._player

    def _play(self, ch: dict) -> None:
        self._current = ch
        self._delegate.playing_id = ch.get("id")
        self._view.viewport().update()
        url = ch.get("logo")
        self._info.set_channel(ch, self._logos.get(url) if url else None)
        self._info.show()
        self._ensure_player().play_stream(ch["url"], referer=None, headers=None, title=ch.get("name") or "")


    def _show_sources_menu(self) -> None:
        m = RoundMenu(parent=self)
        m.addAction(Action(FluentIcon.ADD, tr("tv.channels.add_source", default="Add source"),
                           triggered=self._on_add_source))
        m.addAction(Action(FluentIcon.EDIT, tr("tv.channels.manage_sources", default="Manage sources"),
                           triggered=self._on_manage_sources))
        b = self._sources_btn
        m.exec(b.mapToGlobal(QPoint(0, b.height())))

    def _on_add_source(self) -> None:
        dialog = _AddSourceDialog(self.window())
        if dialog.exec() == QDialog.DialogCode.Accepted:
            source = dialog.result_source()
            if source is not None:
                InfoBar.success(title=tr("tv.channels.source_added", default="Source added"),
                                content=source["name"], parent=self.window(), duration=2000, isClosable=True)
                if self._state != "idle":
                    self._load_channels(use_cache=False)

    def _on_manage_sources(self) -> None:
        dialog = _ManageSourcesDialog(self.window())
        dialog.exec()
        if dialog.changed and self._state != "idle":
            self._load_channels(use_cache=True)


    def _apply_locale(self, *_a) -> None:
        self._search.setPlaceholderText(tr("tv.channels.search_placeholder", default="Search channels..."))
        self._countries_btn.setToolTip(tr("tv.channels.select_countries", default="Select countries"))
        self._sources_btn.setToolTip(tr("tv.channels.sources", default="Sources"))
        self._refresh_btn.setToolTip(tr("tv.channels.refresh", default="Refresh"))
        self._stage.set_hint(tr("tv.channels.pick_hint", default="Select a channel to start watching"))
        self._info.retext()
        self._sync_view()

    def shutdown(self) -> None:
        self._cancel_load()
        for t in (self._filter_timer, self._logo_timer, self._repaint_timer):
            t.stop()
        images.release(self)
        self._logos.clear()
        if self._player is not None:
            self._player.shutdown()


def _meta_field(label: str, value_widget: QWidget, parent=None) -> QWidget:
    container = QWidget(parent)
    layout = QVBoxLayout(container)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(2)
    caption = CaptionLabel(label, container)
    apply_label_theme(caption, "muted")
    layout.addWidget(caption)
    value_widget.setParent(container)
    layout.addWidget(value_widget)
    return container



def _tmdb_img(path, size: str) -> str:
    return (tmdb.image_url(path, size) or "") if path else ""


def _pick_art(items, langs: tuple, min_width: int = 0) -> str:
    pool = [i for i in items or [] if i.get("file_path") and not str(i["file_path"]).lower().endswith(".svg")
            and (i.get("width") or 0) >= min_width]
    if not pool:
        return ""
    def rank(i):
        lang = i.get("iso_639_1")
        pref = langs.index(lang) if lang in langs else len(langs)
        return (-pref, i.get("vote_average") or 0, i.get("width") or 0)
    return max(pool, key=rank)["file_path"]


def _pick_trailer(videos) -> str:
    pool = [v for v in videos or [] if v.get("site") == "YouTube" and v.get("key")]
    if not pool:
        return ""
    def rank(v):
        return (v.get("type") == "Trailer", bool(v.get("official")), v.get("iso_639_1") == "en", v.get("published_at") or "")
    return f"https://www.youtube.com/watch?v={max(pool, key=rank)['key']}"


def _fade_banner(pix, width: int):
    from PySide6.QtGui import QImage, QLinearGradient
    pix = QPixmap(pix)
    pix.setDevicePixelRatio(1.0)
    scaled = pix.scaledToWidth(width, Qt.TransformationMode.SmoothTransformation)
    img = scaled.copy(0, 0, width, min(scaled.height(), 900)).toImage()
    img = img.convertToFormat(QImage.Format.Format_ARGB32_Premultiplied)
    fade = QLinearGradient(0, 0, 0, img.height())
    for pos, alpha in ((0.0, 215), (0.16, 130), (0.38, 45), (0.6, 0)):
        fade.setColorAt(pos, QColor(0, 0, 0, alpha))
    painter = QPainter(img)
    painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_DestinationIn)
    painter.fillRect(img.rect(), fade)
    painter.end()
    return QPixmap.fromImage(img)


class _TmdbItem:
    __slots__ = ('raw', 'id', 'title', 'poster_url', 'subtitle')

    def __init__(self, raw: dict):
        self.raw = raw
        self.id = raw.get("id")
        title = raw.get("title") or raw.get("name") or "Untitled"
        year = (raw.get("release_date") or raw.get("first_air_date") or "")[:4]
        self.title = f"{title} ({year})" if year else title
        self.poster_url = tmdb.image_url(raw.get("poster_path"), "w185")
        rating = raw.get("vote_average")
        self.subtitle = f"\u2605 {rating:.1f}" if rating else ""


class _MediaRow(QWidget):
    context_requested = Signal(object, QPoint)

    def __init__(self, title: str, media_type: str, on_click, parent=None, wrap=_TmdbItem):
        super().__init__(parent)
        self._wrap = wrap
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        _row_title_lbl = StrongBodyLabel(title, self)
        self._title_lbl = _row_title_lbl
        apply_label_theme(_row_title_lbl, "primary_text")
        layout.addWidget(_row_title_lbl)
        self._strip = PosterStrip(self, colors_fn=_grid_colors)
        self._strip.poster_clicked.connect(lambda item: on_click(item.raw, media_type))
        self._strip.context_requested.connect(self.context_requested.emit)
        layout.addWidget(self._strip)
        self.hide()

    def set_title(self, title: str) -> None:
        self._title_lbl.setText(title)

    def set_items(self, items: list[dict]) -> None:
        self._strip.set_entries([self._wrap(item) for item in items])
        self.setVisible(bool(items))


class _LeftClickCard(CardWidget):
    def mouseReleaseEvent(self, e) -> None:
        if e.button() != Qt.MouseButton.LeftButton:
            self.isPressed = False
            self.update()
            return
        super().mouseReleaseEvent(e)


class _EpisodeThumb(_LeftClickCard):
    context_requested = Signal(object, QPoint)

    def __init__(self, ep: dict, on_click, parent=None):
        super().__init__(parent)
        self._ep = ep
        self._still_url = tmdb.image_url(ep.get("still_path"), "w300")
        self.setFixedSize(230, 172)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.clicked.connect(lambda: on_click(ep))
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.customContextMenuRequested.connect(
            lambda pos: self.context_requested.emit(self._ep, self.mapToGlobal(pos)))

        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 6, 6, 6)
        layout.setSpacing(4)
        self._thumb_lbl = QLabel(self)
        self._thumb_lbl.setFixedSize(218, 122)
        self._thumb_lbl.setScaledContents(True)
        self._thumb_lbl.setStyleSheet(f"background: {palette()['poster_fallback_bg']}; border-radius: 6px;")
        layout.addWidget(self._thumb_lbl)
        num, name = ep.get("episode_number"), ep.get("name") or ""
        _ep_lbl = CaptionLabel(f"{num}. {name}" if num else name, self)
        apply_label_theme(_ep_lbl, "muted")
        layout.addWidget(_ep_lbl)

        if self._still_url:
            images.get(self._still_url, (218, 122), self._apply_thumb, owner=self)

    def _apply_thumb(self, pixmap) -> None:
        if not pixmap.isNull():
            self._thumb_lbl.setPixmap(pixmap)


class _MediaDetailsView(QWidget):
    POSTER_WIDTH = 220
    POSTER_HEIGHT = 330
    banner_changed = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._gen = 0
        self.banner_active = False
        self._banner_src = ""
        self._banner_pix = None
        self._banner_cache = (0, None)
        self._logo_src = ""
        self._trailer_url = ""
        self._imdb_url = ""
        self._media_id: Optional[int] = None
        self._media_type = "movie"
        self._raw: dict = {}
        self._task_ids: list[str] = []
        self._season_task_id: Optional[str] = None
        self._current_season = 1
        self._current_episode = 1
        self._episode_thumbs: list[_EpisodeThumb] = []
        self._pending_poster_url: Optional[str] = None
        self._genre_tags: list[PillPushButton] = []
        self._imdb_id: Optional[str] = None
        self._imdb_task_id: Optional[str] = None
        self._source_task_id: Optional[str] = None
        self._stream_sources: list = []
        self._stream_episode: Optional[tuple[int, int]] = None
        self._engine: Any = None
        self._play_task_id: Optional[str] = None
        self._play_gen = 0
        self._tried_sources: set[int] = set()
        self._active_source: Any = None
        self._debrid_playback = False
        self._connect_ticks = 0
        self._resume_timer = QTimer(self)
        self._resume_timer.setInterval(30_000)
        self._resume_timer.timeout.connect(self._save_resume)
        self._stream_session: Any = None
        self._download_manager: Any = None
        self._download_task_id: Optional[str] = None
        self._download_target: Optional[tuple] = None
        self._download_preset: tuple = (None, None)
        self._download_pending = False
        self._autoplay_episode = False
        self._auto_pick = False
        self._picker_forced = False
        self._source_quick = False
        self._streams_full = False
        self._season_eps: dict[int, list[int]] = {}
        self._skip_gen = 0
        self._pending_play_episode: Optional[int] = None
        self._poll_timer = QTimer(self)
        self._poll_timer.setInterval(500)
        self._poll_timer.timeout.connect(self._poll_stream)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(8)

        back_btn = PushButton(themed_icon('tv.media_back', FluentIcon.LEFT_ARROW), tr("tv.movies.back", default="Back"), self)
        back_btn.setFixedHeight(32)
        back_btn.clicked.connect(self._on_back)
        outer.addWidget(back_btn, 0, Qt.AlignmentFlag.AlignLeft)

        scroll = SmoothScrollArea(self)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        scroll.setStyleSheet("background: transparent; border: none;")
        outer.addWidget(scroll, 1)

        self._scroll = scroll
        scroll.verticalScrollBar().valueChanged.connect(lambda _v: self.banner_changed.emit())
        content = QWidget()
        content.setStyleSheet("background: transparent;")
        scroll.setWidget(content)
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(4, 4, 20, 32)
        content_layout.setSpacing(24)

        top_row = QHBoxLayout()
        top_row.setSpacing(28)
        top_row.setAlignment(Qt.AlignmentFlag.AlignTop)

        self._poster_lbl = ImageLabel(content)
        self._poster_lbl.setBorderRadius(10, 10, 10, 10)
        self._poster_lbl.setFixedSize(self.POSTER_WIDTH, self.POSTER_HEIGHT)
        self._poster_lbl.setStyleSheet(f"background: {palette()['poster_fallback_bg']};")
        top_row.addWidget(self._poster_lbl, 0, Qt.AlignmentFlag.AlignTop)

        info_col = QVBoxLayout()
        info_col.setSpacing(14)

        self._title_lbl = TitleLabel("", content)
        self._title_lbl.setWordWrap(True)
        info_col.addWidget(self._title_lbl)
        self._logo_lbl = QLabel(content)
        self._logo_lbl.setStyleSheet("background: transparent;")
        self._logo_lbl.hide()
        info_col.addWidget(self._logo_lbl, 0, Qt.AlignmentFlag.AlignLeft)

        meta_grid = QGridLayout()
        meta_grid.setHorizontalSpacing(32)
        meta_grid.setVerticalSpacing(6)
        self._rating_lbl = StrongBodyLabel("", content)
        apply_label_theme(self._rating_lbl, "primary_text")
        self._date_lbl = BodyLabel("", content)
        self._runtime_lbl = BodyLabel("", content)
        meta_grid.addWidget(_meta_field(tr("tv.movies.rating", default="Rating"), self._rating_lbl, content), 0, 0)
        meta_grid.addWidget(_meta_field(tr("tv.movies.release_date", default="Release date"), self._date_lbl, content), 0, 1)
        meta_grid.addWidget(_meta_field(tr("tv.movies.runtime", default="Runtime"), self._runtime_lbl, content), 0, 2)
        info_col.addLayout(meta_grid)

        watch_row = QHBoxLayout()
        watch_row.setSpacing(10)
        self._watch_btn = PrimaryPushButton(_play_icon(follow=True),
                                            tr("tv.watch", default="Watch"), content)
        self._watch_btn.setIconSize(QSize(20, 20))
        self._watch_btn.clicked.connect(self._on_watch)
        self._watch_btn.setVisible(bool(getattr(settings, "tv_stream_enabled", True)))
        watch_row.addWidget(self._watch_btn)
        self._sources_btn = PushButton(FluentIcon.MENU,
                                       tr("tv.stream.choose_source", default="Choose source"), content)
        self._sources_btn.clicked.connect(self._on_choose_source)
        self._sources_btn.setVisible(bool(getattr(settings, "tv_stream_enabled", True)))
        watch_row.addWidget(self._sources_btn)
        self._wl_btn = _WatchLaterButton(content)
        self._wl_btn.clicked.connect(self._on_toggle_watch_later)
        self._wl_btn.hide()
        watch_row.addWidget(self._wl_btn)
        self._tmdb_btn = PushButton(FluentIcon.LINK, "TMDB", content)
        self._tmdb_btn.clicked.connect(lambda: QDesktopServices.openUrl(
            QUrl(f"https://www.themoviedb.org/{self._media_type}/{self._media_id}")))
        self._tmdb_btn.hide()
        watch_row.addWidget(self._tmdb_btn)
        self._download_btn = PushButton(themed_icon('tv.download', FluentIcon.DOWNLOAD),
                                        tr("tv.download.btn", default="Download"), content)
        self._download_btn.clicked.connect(lambda: self._on_download())
        self._download_btn.hide()
        watch_row.addWidget(self._download_btn)
        self._stream_spinner = _Spinner(content)
        self._stream_spinner.hide()
        watch_row.addWidget(self._stream_spinner)
        self._stream_status = CaptionLabel("", content)
        apply_label_theme(self._stream_status, "muted")
        watch_row.addWidget(self._stream_status, 1)
        info_col.addLayout(watch_row)

        self._tags_container = QWidget(content)
        self._tags_layout = QHBoxLayout(self._tags_container)
        self._tags_layout.setContentsMargins(0, 0, 0, 0)
        self._tags_layout.setSpacing(8)
        self._tags_layout.addStretch(1)
        info_col.addWidget(self._tags_container)

        links_row = QHBoxLayout()
        links_row.setSpacing(8)
        self._trailer_btn = PushButton(FluentIcon.VIDEO, tr("tv.movies.trailer", default="Trailer"), content)
        self._trailer_btn.clicked.connect(lambda: QDesktopServices.openUrl(QUrl(self._trailer_url)))
        self._trailer_btn.hide()
        links_row.addWidget(self._trailer_btn)
        self._imdb_btn = PushButton(FluentIcon.LINK, "IMDb", content)
        self._imdb_btn.clicked.connect(lambda: QDesktopServices.openUrl(QUrl(self._imdb_url)))
        self._imdb_btn.hide()
        links_row.addWidget(self._imdb_btn)
        links_row.addStretch(1)
        info_col.addLayout(links_row)

        overview_header = StrongBodyLabel(tr("tv.movies.overview", default="Overview"), content)
        apply_label_theme(overview_header, "primary_text")
        info_col.addWidget(overview_header)
        self._overview_lbl = BodyLabel("", content)
        self._overview_lbl.setWordWrap(True)
        info_col.addWidget(self._overview_lbl)

        cast_header = StrongBodyLabel(tr("tv.movies.cast", default="Cast"), content)
        apply_label_theme(cast_header, "primary_text")
        info_col.addWidget(cast_header)
        self._cast_lbl = BodyLabel("", content)
        self._cast_lbl.setWordWrap(True)
        info_col.addWidget(self._cast_lbl)
        self._crew_lbl = BodyLabel("", content)
        self._crew_lbl.setWordWrap(True)
        apply_label_theme(self._crew_lbl, "muted")
        self._crew_lbl.hide()
        info_col.addWidget(self._crew_lbl)

        info_col.addStretch(1)
        top_row.addLayout(info_col, 1)
        content_layout.addLayout(top_row)

        self._player_widget = AnimePlayerWidget(content)
        self._player_widget.alt_stream_requested.connect(self._on_alt_stream)
        self._player_widget.playback_error.connect(self._on_player_error)
        self._player_widget.ended.connect(self._on_player_ended)
        self._player_widget.next_requested.connect(self._play_next)
        self._player_widget.close_requested.connect(self._close_media_player)
        self._player_widget.set_closable(True)
        self._player_widget.hide()
        content_layout.addWidget(self._player_widget, 0, Qt.AlignmentFlag.AlignHCenter)

        self._episodes_section = QWidget(content)
        ep_layout = QVBoxLayout(self._episodes_section)
        ep_layout.setContentsMargins(0, 0, 0, 0)
        ep_layout.setSpacing(6)
        season_row = QHBoxLayout()
        _ep_header = StrongBodyLabel(tr("tv.series.episodes", default="Episodes"), self._episodes_section)
        apply_label_theme(_ep_header, "primary_text")
        season_row.addWidget(_ep_header)
        self._season_combo = ComboBox(self._episodes_section)
        self._season_combo.currentIndexChanged.connect(self._on_season_changed)
        season_row.addWidget(self._season_combo)
        season_row.addStretch(1)
        ep_layout.addLayout(season_row)
        ep_scroll = SmoothScrollArea(self._episodes_section)
        ep_scroll.setWidgetResizable(True)
        ep_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        ep_scroll.setFixedHeight(190)
        ep_scroll.setStyleSheet("background: transparent; border: none;")
        ep_inner = QWidget()
        self._episodes_row = QHBoxLayout(ep_inner)
        self._episodes_row.setContentsMargins(2, 2, 2, 2)
        self._episodes_row.setSpacing(10)
        self._episodes_row.addStretch(1)
        ep_scroll.setWidget(ep_inner)
        ep_layout.addWidget(ep_scroll)
        content_layout.addWidget(self._episodes_section)
        self._episodes_section.hide()

        self._collection_row = _MediaRow("", "movie", self._open_related, content)
        content_layout.addWidget(self._collection_row)
        self._recs_row = _MediaRow(tr("tv.movies.more_like_this", default="More like this"), "movie", self._open_related, content)
        content_layout.addWidget(self._recs_row)

        content_layout.addStretch(1)

    def banner_for(self, width: int):
        if not self.banner_active or self._banner_pix is None or width <= 0:
            return None
        if self._banner_cache[0] != width:
            self._banner_cache = (width, _fade_banner(self._banner_pix, width))
        return self._banner_cache[1]

    def banner_offset(self) -> int:
        return self._scroll.verticalScrollBar().value()

    def _set_banner(self, src: str) -> None:
        if src == self._banner_src:
            return
        self._banner_src = src
        self._banner_pix = None
        self._banner_cache = (0, None)
        self.banner_changed.emit()
        if not src:
            return

        def _apply(pix, _src=src):
            if _src == self._banner_src and not pix.isNull():
                self._banner_pix = pix
                self.banner_changed.emit()

        images.get(src, (1920, 1080), _apply, mode=FIT, owner=self)

    def _set_logo(self, src: str) -> None:
        if src == self._logo_src:
            return
        self._logo_src = src
        self._logo_lbl.hide()
        self._title_lbl.show()
        if not src:
            return

        def _apply(pix, _src=src):
            if _src != self._logo_src or pix.isNull():
                return
            pix = QPixmap(pix)
            pix.setDevicePixelRatio(1.0)
            pix = pix.scaled(420, 110, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)
            self._logo_lbl.setFixedSize(pix.size())
            self._logo_lbl.setPixmap(pix)
            self._logo_lbl.setToolTip(self._title_lbl.text())
            self._logo_lbl.show()
            self._title_lbl.hide()

        images.get(src, (420, 140), _apply, mode=FIT, owner=self)

    def _guard(self, gen: int, fn):
        def wrapped(*args, **kwargs):
            if gen == self._gen and _qt_is_valid(self):
                self._safe(fn)(*args, **kwargs)
        return wrapped

    def _reset_extras(self) -> None:
        self._set_banner("")
        self._set_logo("")
        self._trailer_url = self._imdb_url = ""
        self._trailer_btn.hide()
        self._imdb_btn.hide()
        self._crew_lbl.hide()
        self._collection_row.set_items([])
        self._recs_row.set_items([])

    def _paint_preview(self, raw: dict) -> None:
        if not raw:
            return
        rating = raw.get("vote_average") or 0
        if rating:
            self._rating_lbl.setText(f"\u2605 {rating:.1f}")
        self._date_lbl.setText(raw.get("release_date") or raw.get("first_air_date") or "\u2014")
        self._overview_lbl.setText(raw.get("overview") or "")
        self._set_banner(_tmdb_img(raw.get("backdrop_path"), "w1280"))
        poster = tmdb.image_url(raw.get("poster_path"), "w342")
        if poster:
            self._pending_poster_url = poster
            images.get(poster, (self.POSTER_WIDTH, self.POSTER_HEIGHT), partial(self._on_thumb_ready, poster), owner=self)

    def _open_related(self, raw: dict, media_type: str) -> None:
        tab = self.parent()
        while tab is not None and not hasattr(tab, "_open_details"):
            tab = tab.parent()
        if tab is not None and raw.get("id") is not None:
            tab._open_details(raw, media_type)

    def _on_movie_full(self, data: dict) -> None:
        if not isinstance(data, dict):
            return
        ext = data.get("external_ids") or {}
        if not data.get("imdb_id") and ext.get("imdb_id"):
            data["imdb_id"] = ext["imdb_id"]
        self._on_details(data)
        self._on_credits(data.get("credits") or {})
        imgs = data.get("images") or {}
        self._set_banner(_tmdb_img(data.get("backdrop_path") or _pick_art(imgs.get("backdrops"), (None, "en"), 1000), "w1280"))
        self._set_logo(_tmdb_img(_pick_art(imgs.get("logos"), ("en", None)), "w500"))
        self._trailer_url = _pick_trailer((data.get("videos") or {}).get("results"))
        self._trailer_btn.setVisible(bool(self._trailer_url))
        imdb = str(data.get("imdb_id") or "")
        self._imdb_url = f"https://www.imdb.com/title/{imdb}/" if imdb.startswith("tt") else ""
        self._imdb_btn.setVisible(bool(self._imdb_url))
        crew = (data.get("credits") or {}).get("crew") or []
        directors = list(dict.fromkeys(c["name"] for c in crew if c.get("job") == "Director" and c.get("name")))[:2]
        writers = list(dict.fromkeys(c["name"] for c in crew if c.get("job") in ("Screenplay", "Writer", "Story") and c.get("name")))[:2]
        parts = []
        if directors:
            parts.append(tr("tv.movies.directed_by", default="Directed by {names}", names=", ".join(directors)))
        if writers:
            parts.append(tr("tv.movies.written_by", default="Written by {names}", names=", ".join(writers)))
        self._crew_lbl.setText("  \u00b7  ".join(parts))
        self._crew_lbl.setVisible(bool(parts))
        recs = [r for r in (data.get("recommendations") or {}).get("results") or [] if r.get("poster_path")]
        self._recs_row.set_items(_MediaTab._filter_released(recs)[:14])
        coll = data.get("belongs_to_collection") or {}
        if coll.get("id"):
            gen, name, own = self._gen, coll.get("name") or "", self._media_id
            self._task_ids.append(tmdb.collection_async(
                coll["id"], on_done=self._guard(gen, lambda d: self._on_collection(name, own, d)),
                on_error=lambda _m: None))

    def _on_collection(self, name: str, own_id, data: dict) -> None:
        parts = [p for p in (data or {}).get("parts") or [] if p.get("poster_path") and p.get("id") != own_id]
        parts = _MediaTab._filter_released(parts)
        parts.sort(key=lambda p: p.get("release_date") or "")
        self._collection_row.set_title(data.get("name") or name or tr("tv.movies.collection", default="Collection"))
        self._collection_row.set_items(parts)

    def _on_back(self) -> None:
        parent_tab = self.parent()
        while parent_tab is not None and not hasattr(parent_tab, "_show_grid"):
            parent_tab = parent_tab.parent()
        if parent_tab is not None:
            parent_tab._show_grid()

    def show_media(self, media_id: int, media_type: str, title: str, raw: Optional[dict] = None) -> None:
        self._gen += 1
        gen = self._gen
        self.banner_active = True
        for task_id in self._task_ids:
            _worker_module.cancel(task_id)
        self._task_ids = []
        if self._season_task_id is not None:
            _worker_module.cancel(self._season_task_id)
            self._season_task_id = None
        if self._imdb_task_id is not None:
            _worker_module.cancel(self._imdb_task_id)
            self._imdb_task_id = None
        if self._source_task_id is not None:
            _worker_module.cancel(self._source_task_id)
            self._source_task_id = None
        self._stop_playback()
        self._imdb_id, self._stream_sources, self._stream_episode = None, [], None
        self._season_eps, self._auto_pick, self._picker_forced = {}, False, False
        self._source_quick = False
        self._streams_full = False
        self._media_id, self._media_type = media_id, media_type
        self._current_season = self._current_episode = 1
        self._pending_play_episode = None
        self._download_pending = False
        self._download_target = None
        self._download_preset = (None, None)
        self._autoplay_episode = False
        self._watch_btn.setVisible(bool(getattr(settings, "tv_stream_enabled", True)))
        self._sources_btn.setVisible(bool(getattr(settings, "tv_stream_enabled", True)))
        self._download_btn.setVisible(self._download_manager is not None)
        self._raw = raw if isinstance(raw, dict) else {}
        self._update_watch_label()
        self._update_watch_later_btn()
        self._title_lbl.setText(title)
        self._rating_lbl.setText("—")
        self._date_lbl.setText("—")
        self._runtime_lbl.setText("—")
        self._overview_lbl.setText("")
        self._cast_lbl.setText("")
        self._pending_poster_url = None
        try:
            self._watch_btn.setStyleSheet("")
        except RuntimeError:
            pass
        self._clear_tags()
        self._episodes_section.setVisible(media_type == "tv")
        self._clear_episodes()
        self._reset_extras()

        if media_type == "movie":
            self._paint_preview(self._raw)
            self._task_ids.append(tmdb.movie_full_async(
                media_id, on_done=self._guard(gen, self._on_movie_full), on_error=self._guard(gen, self._on_error)))
            return

        self._task_ids.append(tmdb.details_async(media_type, media_id, on_done=self._safe(self._on_details), on_error=self._safe(self._on_error)))
        self._task_ids.append(tmdb.credits_async(media_type, media_id, on_done=self._safe(self._on_credits)))

        if media_type == "tv":
            self._imdb_task_id = tmdb.external_ids_async(
                "tv", media_id, on_done=self._safe(self._on_external_ids), on_error=self._safe(self._on_error))
            self._load_season(1)

    def _on_external_ids(self, data: dict) -> None:
        self._imdb_task_id = None
        self._imdb_id = data.get("imdb_id") or ""
        self._run_deferred_actions()

    def _run_deferred_actions(self) -> None:
        if not self._imdb_id and not self._title_lbl.text():
            return
        if self._autoplay_episode:
            self._autoplay_episode = False
            self._open_stream_picker()
        if self._download_pending:
            self._download_pending = False
            self._on_download()

    def _update_watch_label(self) -> None:
        if self._media_type == "tv":
            self._watch_btn.setText(tr("tv.watch_episode", default="Watch S{season}E{episode}",
                                       season=self._current_season, episode=self._current_episode))
        else:
            self._watch_btn.setText(tr("tv.watch", default="Watch"))

    def _watch_later_entry(self) -> dict:
        raw = self._raw or {}
        date = str(raw.get("release_date") or raw.get("first_air_date") or "")
        return {
            "media_type": self._media_type,
            "id": self._media_id,
            "title": raw.get("title") or raw.get("name") or self._title_lbl.text(),
            "year": date[:4],
            "rating": raw.get("vote_average"),
            "poster_url": tmdb.image_url(raw.get("poster_path"), "w185"),
            "raw": raw,
        }

    def _update_watch_later_btn(self) -> None:
        self._tmdb_btn.setVisible(self._media_id is not None)
        if self._media_id is None:
            self._wl_btn.hide()
            return
        self._wl_btn.show()
        saved = watch_later_store().has(self._media_type, self._media_id)
        self._wl_btn.set_state(saved, tr("tv.watchlater_saved", default="In watch later") if saved
                               else tr("tv.watchlater_add", default="Watch later"))

    def _on_toggle_watch_later(self) -> None:
        if self._media_id is None:
            return
        store = watch_later_store()
        if store.has(self._media_type, self._media_id):
            store.remove(self._media_type, self._media_id)
        else:
            store.add(self._watch_later_entry())
        self._update_watch_later_btn()

    @staticmethod
    def _safe(fn):
        def wrapped(*args, **kwargs):
            try:
                fn(*args, **kwargs)
            except Exception:
                logger.exception("Media details callback failed")
        return wrapped

    def _on_error(self, _msg: str) -> None:
        self._overview_lbl.setText(tr("tv.movies.load_error", default="Failed to load details"))

    def _clear_tags(self) -> None:
        for tag in self._genre_tags:
            tag.deleteLater()
        self._genre_tags = []

    def _on_details(self, data: dict) -> None:
        rating = data.get("vote_average") or 0
        release = data.get("release_date") or data.get("first_air_date") or "—"
        runtime = data.get("runtime") or (data.get("episode_run_time") or [0])[0]
        self._rating_lbl.setText(f"★ {rating:.1f}")
        self._date_lbl.setText(release)
        self._runtime_lbl.setText(f"{runtime} min" if runtime else "—")
        self._overview_lbl.setText(data.get("overview") or "")
        if self._media_type == "movie":
            self._imdb_id = data.get("imdb_id") or ""
            self._run_deferred_actions()

        self._clear_tags()
        for genre in data.get("genres") or []:
            tag = PillPushButton(genre.get("name", ""), self._tags_container)
            tag.setCheckable(False)
            tag.setFixedHeight(28)
            self._tags_layout.insertWidget(self._tags_layout.count() - 1, tag)
            self._genre_tags.append(tag)

        if self._media_type == "tv":
            self._season_combo.blockSignals(True)
            self._season_combo.clear()
            for s in data.get("seasons") or []:
                if s.get("season_number", 0) > 0:
                    self._season_combo.addItem(s.get("name") or f"Season {s['season_number']}", userData=s["season_number"])
            idx = self._season_combo.findData(self._current_season)
            if idx >= 0:
                self._season_combo.setCurrentIndex(idx)
            self._season_combo.blockSignals(False)

        if self._media_type == "tv":
            self._set_banner(_tmdb_img(data.get("backdrop_path"), "w1280"))

        poster = tmdb.image_url(data.get("poster_path"), "w342")
        if poster:
            self._pending_poster_url = poster
            images.get(poster, (self.POSTER_WIDTH, self.POSTER_HEIGHT), partial(self._on_thumb_ready, poster), owner=self)
            images.dominant(poster, partial(self._on_poster_accent, poster), owner=self)

    def _on_poster_accent(self, url: str, colors) -> None:
        if not _qt_is_valid(self) or url != self._pending_poster_url or not colors:
            return
        c = QColor(colors[0])
        if not c.isValid():
            return
        h, s, l, _a = c.getHsl()
        c = QColor.fromHsl(max(h, 0), max(s, 90), min(max(l, 70), 135))
        hover, press = c.lighter(115).name(), c.darker(115).name()


        self._watch_btn.setStyleSheet(
            f"PrimaryPushButton{{background-color:{c.name()};color:#fff;border:1px solid {press};"
            f"border-radius:5px;padding:5px 18px 5px 38px;min-height:20px;}}"
            f"PrimaryPushButton:hover{{background-color:{hover};}}"
            f"PrimaryPushButton:pressed{{background-color:{press};}}"
            "PrimaryPushButton:disabled{background-color:rgba(128,128,128,70);color:rgba(255,255,255,120);}")

    def _on_credits(self, data: dict) -> None:
        cast = [c["name"] for c in (data.get("cast") or [])[:10]]
        self._cast_lbl.setText(", ".join(cast) if cast else tr("tv.movies.no_cast", default="No cast information available"))

    def _on_thumb_ready(self, url: str, pixmap) -> None:
        if pixmap.isNull() or url != self._pending_poster_url:
            return
        self._poster_lbl.setImage(pixmap)
        self._poster_lbl.setFixedSize(self.POSTER_WIDTH, self.POSTER_HEIGHT)


    def _on_season_changed(self, _idx: int) -> None:
        season_number = self._season_combo.currentData()
        if season_number is not None:
            self._load_season(season_number)

    def _load_season(self, season_number: int) -> None:
        if self._season_task_id is not None:
            _worker_module.cancel(self._season_task_id)
        self._current_season = season_number
        self._update_watch_label()
        self._season_task_id = tmdb.season_async(
            self._media_id, season_number, on_done=self._safe(self._on_season_loaded), on_error=self._safe(self._on_error),
        )

    def _clear_episodes(self) -> None:
        for thumb in self._episode_thumbs:
            thumb.deleteLater()
        self._episode_thumbs = []

    def _on_season_loaded(self, data: dict) -> None:
        self._season_task_id = None
        pending = self._pending_play_episode
        self._pending_play_episode = None
        self._clear_episodes()
        episodes = data.get("episodes") or []
        self._season_eps[int(self._current_season)] = sorted(int(e.get("episode_number") or 0) for e in episodes)
        for ep in episodes:
            thumb = _EpisodeThumb(ep, self._on_episode_clicked, self)
            thumb.context_requested.connect(self._on_episode_menu)
            self._episode_thumbs.append(thumb)
            self._episodes_row.insertWidget(self._episodes_row.count() - 1, thumb)
        target = next((ep for ep in episodes if int(ep.get("episode_number") or 0) == pending), None) if pending else None
        if target is not None:
            self._select_episode(target)
            self._start_episode(target)
        elif episodes:
            self._select_episode(episodes[0])

    def _select_episode(self, episode: dict) -> None:
        self._current_episode = episode.get("episode_number") or 1
        self._update_watch_label()

    def _on_episode_clicked(self, episode: dict) -> None:
        number = episode.get("episode_number") or 1
        already_playing = (self._player_widget.has_media()
                           and self._stream_episode == (self._current_season, number))
        self._select_episode(episode)
        if not getattr(settings, "tv_stream_enabled", True):
            return
        if already_playing:
            self._player_widget.setFocus()
            return
        self._open_stream_picker()

    def _start_episode(self, episode: dict) -> None:
        if not getattr(settings, "tv_stream_enabled", True):
            return
        if self._imdb_id or getattr(self, "_imdb_task_id", None) is None:
            self._open_stream_picker()
        else:
            self._autoplay_episode = True

    def open_episode(self, season: int, episode: int) -> None:
        if self._media_id is None:
            return
        season, episode = int(season or 1), int(episode or 1)
        self._pending_play_episode = episode
        combo_index = self._season_combo.findData(season)
        if combo_index >= 0:
            self._season_combo.blockSignals(True)
            self._season_combo.setCurrentIndex(combo_index)
            self._season_combo.blockSignals(False)
        self._load_season(season)


    def _episode_watch_later_entry(self, episode: dict) -> dict:
        entry = self._watch_later_entry()
        entry.update({
            "season": int(self._current_season or 1),
            "episode": int(episode.get("episode_number") or 1),
            "episode_title": episode.get("name") or "",
            "still_url": tmdb.image_url(episode.get("still_path"), "w300"),
        })
        return entry

    def _toggle_episode_watch_later(self, episode: dict) -> None:
        if self._media_id is None:
            return
        store = watch_later_store()
        season = int(self._current_season or 1)
        number = int(episode.get("episode_number") or 1)
        if store.has(self._media_type, self._media_id, season, number):
            store.remove(self._media_type, self._media_id, season, number)
        else:
            store.add(self._episode_watch_later_entry(episode))

    def _on_episode_menu(self, episode: dict, pos) -> None:
        if self._media_id is None:
            return
        season = int(self._current_season or 1)
        number = int(episode.get("episode_number") or 1)
        saved = watch_later_store().has(self._media_type, self._media_id, season, number)
        menu = RoundMenu(parent=self)
        menu.addAction(Action(_play_icon(), tr("tv.stream.play", default="Play"),
                              triggered=lambda: self._on_episode_clicked(episode)))
        menu.addAction(Action(themed_icon('tv.watchlater', FluentIcon.HEART),
                              tr("tv.watchlater_episode_remove", default="Remove episode from watch later") if saved
                              else tr("tv.watchlater_episode_add", default="Add episode to watch later"),
                              triggered=lambda: self._toggle_episode_watch_later(episode)))
        if self._download_manager is not None:
            menu.addSeparator()
            menu.addAction(Action(FluentIcon.DOWNLOAD,
                                  tr("tv.download.episode", default="Download this episode..."),
                                  triggered=lambda: self._on_download(episode=episode)))
            menu.addAction(Action(FluentIcon.DOWNLOAD,
                                  tr("tv.download.season", default="Download season {season}...", season=season),
                                  triggered=lambda: self._on_download(season=True)))
        menu.exec(pos)

    def set_download_manager(self, manager) -> None:
        self._download_manager = manager
        self._download_btn.setVisible(manager is not None and self._media_id is not None)

    def start_watch(self) -> None:
        if self._media_id is None or not getattr(settings, "tv_stream_enabled", True):
            return
        if self._imdb_id or self._title_lbl.text():
            self._on_watch()
        else:
            self._autoplay_episode = True

    def start_download(self) -> None:
        if self._download_manager is None or self._media_id is None:
            return
        if self._imdb_id or self._title_lbl.text():
            self._on_download()
        else:
            self._download_pending = True

    def _on_download(self, episode: Optional[dict] = None, season: bool = False) -> None:
        if self._download_manager is None or self._media_id is None:
            return
        has_imdb = bool(self._imdb_id) and str(self._imdb_id).startswith("tt")
        if not has_imdb and not self._title_lbl.text():
            InfoBar.warning(title=tr("tv.stream.no_imdb_title", default="Cannot search streams"),
                            content=tr("tv.stream.no_imdb_body", default="This title has no IMDb id to look up."),
                            duration=4000, parent=self)
            return
        if self._download_task_id is not None:
            return
        preset_episode = int((episode or {}).get("episode_number") or 0) or None
        if self._media_type == "tv":
            season_number = int(self._current_season or 1)
            if preset_episode is not None:
                self._download_target = self._download_preset = (season_number, preset_episode)
            else:
                self._download_target = self._download_preset = (season_number, None)
        else:
            self._download_target, self._download_preset = None, (None, None)
        self._pick_download_source()

    def _pick_download_source(self) -> None:
        season, episode = self._download_target or (None, None)
        self._set_streaming(True, tr("tv.download.searching", default="Looking for sources to download..."))

        def _done(sources: list) -> None:
            self._download_task_id = None
            self._set_streaming(False)
            sources = list(sources or [])
            if not sources:
                InfoBar.warning(title=tr("tv.stream.none_title", default="No streams found"),
                                content=tr("tv.stream.none_body",
                                           default="No playable sources were found for this title."),
                                duration=5000, parent=self)
                return
            title = self._title_lbl.text()
            if episode:
                title = tr("tv.stream.pick_for", default="{title} (S{season}E{episode})",
                           title=title, season=season, episode=episode)
            dialog = _StreamPickerDialog(sources, title, self.window(), action="download")
            if dialog.exec() == QDialog.DialogCode.Accepted and (source := dialog.chosen()) is not None:
                if getattr(source, "is_direct", False):
                    self._download_direct_source(source)
                else:
                    self._fetch_download_files(source)

        def _failed(message: str) -> None:
            self._download_task_id = None
            self._set_streaming(False)
            InfoBar.error(title=tr("tv.stream.search_failed", default="Source search failed"),
                          content=str(message), duration=6000, parent=self)

        self._download_task_id = _worker_module.submit_coro(
            _find_media_sources,
            args=(self._imdb_id, self._media_type, season, episode, self._title_lbl.text(),
                  self._media_year(), self._media_id),
            on_done=_done, on_error=_failed)

    def _ensure_ffmpeg_ready(self, source) -> bool:
        """Prompt for ffmpeg when a DASH download needs it.

        Returns True when the download can start immediately. When ffmpeg is
        missing and the user agrees to install it, the install runs in the
        background and starts the download through ``_start_direct_download``
        once it finishes.
        """
        if ".mpd" not in (getattr(source, "url", "") or "").lower() or _resolve_ffmpeg():
            return True

        from src.core.yt import provider
        parent = self.window()
        box = MessageBoxBase(parent)
        box.titleLabel = SubtitleLabel(
            tr("tv.download.ffmpeg_title", default="ffmpeg is required"), box)
        box.viewLayout.addWidget(box.titleLabel)
        content = CaptionLabel(tr(
            "tv.download.ffmpeg_body",
            default="This DASH stream needs ffmpeg to download. Download it now? "
                    "The download is about 70 MB."), box)
        content.setWordWrap(True)
        box.viewLayout.addWidget(content)
        box.yesButton.setText(tr("tv.download.ffmpeg_install", default="Download ffmpeg"))
        box.cancelButton.setText(tr("common.cancel", default="Cancel"))
        if not box.exec():
            return False

        from PySide6.QtWidgets import QProgressDialog
        dialog = QProgressDialog(
            tr("tv.download.ffmpeg_progress", default="Downloading ffmpeg..."),
            tr("common.cancel", default="Cancel"), 0, 100, parent)
        dialog.setWindowTitle(tr("tv.download.ffmpeg_title", default="ffmpeg is required"))
        dialog.setWindowModality(Qt.WindowModality.WindowModal)
        dialog.setMinimumDuration(0)
        dialog.setAutoClose(False)
        dialog.setAutoReset(False)
        dialog.setValue(0)
        state = {"finished": False, "cancelled": False}

        def _close() -> None:
            state["finished"] = True
            dialog.close()

        def _progress(done: int, total: int) -> None:
            if total:
                dialog.setValue(max(0, min(100, int(done * 100 / total))))

        def _done(_result) -> None:
            _close()
            if state["cancelled"]:
                return
            InfoBar.success(
                title=tr("tv.download.ffmpeg_ready", default="ffmpeg installed"),
                content=tr("tv.download.ffmpeg_ready_body",
                           default="The DASH download is starting."),
                duration=2500, parent=parent)
            self._start_direct_download(source)

        def _failed(message: str) -> None:
            _close()
            if state["cancelled"]:
                return
            InfoBar.error(
                title=tr("tv.download.ffmpeg_failed", default="Could not install ffmpeg"),
                content=str(message), duration=6000, parent=parent)

        progress = _worker_module.wrap_callback(dialog, _progress)
        ticket = _worker_module.submit(
            provider.install_ffmpeg, args=(progress,),
            on_done=_done, on_error=_failed)

        def _cancelled() -> None:
            if state["finished"]:
                return
            state["cancelled"] = True
            _worker_module.cancel(ticket)

        dialog.canceled.connect(_cancelled, Qt.ConnectionType.DirectConnection)
        dialog.open()
        return False

    def _download_direct_source(self, source) -> None:
        manager = self._download_manager
        if manager is None or not getattr(source, "url", ""):
            return
        if not self._ensure_ffmpeg_ready(source):
            return
        self._start_direct_download(source)

    def _start_direct_download(self, source) -> None:
        manager = self._download_manager
        if manager is None or not getattr(source, "url", ""):
            return
        title = self._title_lbl.text() or "Video"
        season, episode = self._stream_episode or (None, None)
        suffix = f" - S{int(season):02d}E{int(episode):02d}" if season and episode else ""
        base = f"{_sanitize_filename(title)}{suffix}"
        dest_dir = os.path.join(settings.download_dir, _sanitize_filename(title))
        try:
            os.makedirs(dest_dir, exist_ok=True)
        except OSError as exc:
            InfoBar.error(title=tr("tv.download.failed_title", default="Download failed"),
                          content=str(exc), duration=5000, parent=self)
            return
        extension = ".ts" if ".m3u8" in source.url.lower() else ".mp4"
        dest_path = os.path.join(dest_dir, base + extension)
        item_id = manager.add_external(
            game_name=base, console=title,
            source=getattr(source, "indexer", "") or "Vyla", category="tv",
        )

        def _done(path: str) -> None:
            manager.complete_external(item_id, path)

        def _failed(message: str) -> None:
            manager.fail_external(item_id, message)

        task_id = _worker_module.submit(
            _do_download_direct_source,
            args=(manager, item_id, source, dest_path),
            on_done=_done, on_error=_failed,
        )
        manager.register_external_cancel(item_id, lambda tid=task_id: _worker_module.cancel(tid))
        InfoBar.success(
            title=tr("tv.download.queued_title", default="Download queued"),
            content=tr("tv.download.queued_direct", default="The direct stream was added to Downloads."),
            duration=2500, parent=self.window())

    def _fetch_download_files(self, source) -> None:
        manager = self._download_manager
        if manager is None:
            return
        self._set_streaming(True, tr("tv.download.reading_files", default="Reading the torrent's file list..."))

        def _done(files) -> None:
            self._download_task_id = None
            self._set_streaming(False)
            self._offer_download_files(source, files or [])

        def _failed(message: str) -> None:
            self._download_task_id = None
            self._set_streaming(False)
            InfoBar.error(title=tr("tv.download.files_failed", default="Could not read the torrent"),
                          content=str(message), duration=6000, parent=self)

        self._download_task_id = _worker_module.submit(
            manager.fetch_file_list, args=(source.magnet or source.name,),
            kwargs={"timeout_secs": 45.0, "category": "tv"},
            on_done=_done, on_error=_failed)

    def _offer_download_files(self, source, files: list) -> None:
        entries = _torrent_file_entries(files)
        if not entries:
            InfoBar.warning(title=tr("tv.download.no_files_title", default="Nothing to download"),
                            content=tr("tv.download.no_files_body",
                                       default="This torrent has no video files to pick."),
                            duration=5000, parent=self)
            return
        preset_season, preset_episode = self._download_preset
        safety = assess_source_files([path for path, _size in files or ()], context="video",
                                     title=self._title_lbl.text())
        dialog = _TorrentFilesDialog(entries, self._title_lbl.text(), self.window(),
                                     preset_season=preset_season, preset_episode=preset_episode,
                                     safety=safety)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        indices = dialog.selected_indices()
        if indices:
            self._queue_torrent_download(source, indices)

    def _queue_torrent_download(self, source, indices: list) -> None:
        manager = self._download_manager
        if manager is None:
            return
        season, episode = self._download_target or (None, None)
        title = self._title_lbl.text()
        if self._media_type != "tv":
            game_name = title
        elif episode:
            game_name = f"{title} - S{int(season or 1):02d}E{int(episode):02d}"
        else:
            game_name = f"{title} - {tr('tv.download.season_label', default='Season {season}', season=int(season or 1))}"
        try:
            manager.add(torrent_file=source.magnet or source.name, file_id=indices[0], file_ids=indices,
                        game_name=game_name, console=title,
                        source=source.indexer or _SOURCE_NAMES.get(source.source, source.source or "Torrent"),
                        category="tv")
        except Exception as exc:
            logger.exception("Could not queue the download")
            InfoBar.error(title=tr("tv.download.failed_title", default="Download failed"),
                          content=f"{type(exc).__name__}: {exc}", duration=6000, parent=self)
            return
        InfoBar.success(title=tr("tv.download.queued_title", default="Download queued"),
                        content=game_name, duration=3000, parent=self)


    def _episode_target(self) -> Optional[tuple[int, int]]:
        return (self._current_season, self._current_episode) if self._media_type == "tv" else None

    def _media_year(self) -> str:
        raw = self._raw or {}
        date = str(raw.get("release_date") or raw.get("first_air_date") or "")
        return date[:4]

    def _on_watch(self) -> None:
        if self._media_id is None or not getattr(settings, "tv_stream_enabled", True):
            return
        if self._player_widget.has_media():
            self._player_widget.setFocus()
            return
        self._open_stream_picker()

    def _on_choose_source(self) -> None:
        if self._media_id is None or not getattr(settings, "tv_stream_enabled", True):
            return
        self._open_stream_picker(force_picker=True)

    def _on_alt_stream(self, _stream: dict) -> None:
        self._open_stream_picker(force_picker=True)

    def _open_stream_picker(self, refresh: bool = False, force_picker: bool = False) -> None:
        if self._source_task_id is not None:
            if force_picker:
                self._picker_forced = True
                if self._source_quick:
                    # The in-flight quick search may cancel torrent providers
                    # as soon as it finds a direct source. An explicit picker
                    # request deserves the complete list instead.
                    _worker_module.cancel(self._source_task_id)
                    self._source_task_id = None
                    self._source_quick = False
                    self._open_stream_picker(force_picker=True)
            return
        if not self._imdb_id and not self._title_lbl.text():
            self._picker_forced = False
            InfoBar.warning(title=tr("tv.stream.no_imdb_title", default="Cannot search streams"),
                            content=tr("tv.stream.no_imdb_body", default="This title has no name to look up."),
                            duration=4000, parent=self)
            return
        target = self._episode_target()
        if (self._stream_sources and target == self._stream_episode and not refresh
                and (self._streams_full or not force_picker)):
            forced, self._picker_forced = force_picker or self._picker_forced, False
            self._pick_from_sources(forced)
            return
        self._picker_forced = force_picker
        self._stream_episode = target
        quick = not force_picker
        self._source_quick = quick
        self._set_streaming(True, tr("tv.stream.searching", default="Searching sources..."))

        def _done(sources: list) -> None:
            self._source_task_id = None
            self._source_quick = False
            self._set_streaming(False)
            self._stream_sources = list(sources or [])
            self._streams_full = not quick
            forced, self._picker_forced = self._picker_forced, False
            if not self._stream_sources:
                self._auto_pick = False
                InfoBar.warning(title=tr("tv.stream.none_title", default="No streams found"),
                                content=tr("tv.stream.none_body",
                                           default="No playable sources were found for this title."),
                                duration=5000, parent=self)
                return
            self._pick_from_sources(forced)

        def _failed(message: str) -> None:
            self._source_task_id = None
            self._source_quick = False
            self._auto_pick = False
            self._picker_forced = False
            self._set_streaming(False)
            InfoBar.error(title=tr("tv.stream.search_failed", default="Source search failed"),
                          content=str(message), duration=6000, parent=self)

        self._source_task_id = _worker_module.submit_coro(
            _find_media_sources,
            args=(self._imdb_id, self._media_type,
                  target[0] if target else None, target[1] if target else None,
                  self._title_lbl.text(), self._media_year(), self._media_id, quick),
            on_done=_done, on_error=_failed)

    def _pick_from_sources(self, force_picker: bool = False) -> None:
        auto, self._auto_pick = self._auto_pick, False
        if not self._stream_sources:
            return
        direct = [s for s in self._stream_sources if getattr(s, "is_direct", False)]
        if not force_picker and (auto or direct or getattr(settings, "tv_auto_pick", False)):
            pool = direct or self._stream_sources
            best = stream_backend.pick_source(pool) or pool[0]
            self._stream_sources = [best] + [x for x in self._stream_sources if x is not best]
            self._play_source(best)
            return
        title = self._title_lbl.text()
        if self._stream_episode:
            title = tr("tv.stream.pick_for", default="{title} (S{season}E{episode})",
                       title=title, season=self._stream_episode[0], episode=self._stream_episode[1])
        old = getattr(self, "_picker", None)
        if old is not None and _qt_is_valid(old) and old.isVisible():
            return
        dialog = self._picker = _StreamPickerDialog(self._stream_sources, title, self.window())

        def _accepted() -> None:
            source = dialog.chosen()
            if source is not None:
                QTimer.singleShot(0, lambda: self._play_source(source))

        def _finished(_code: int = 0) -> None:
            if getattr(self, "_picker", None) is dialog:
                self._picker = None
            dialog.deleteLater()
        dialog.accepted.connect(_accepted)
        dialog.finished.connect(_finished)
        QTimer.singleShot(0, dialog.open)

    def _play_source(self, source, auto: bool = False) -> None:
        if self._play_task_id is not None:
            return
        if not auto:
            self._tried_sources.clear()
        self._tried_sources.add(id(source))
        from src.core.cache import trim_stream_cache
        _worker_module.submit(trim_stream_cache, priority=_worker_module.Priority.LOW)
        self._active_source = None
        self._player_widget.stop()

        if getattr(source, "is_direct", False):
            self._play_gen += 1
            self._poll_timer.stop()
            if self._engine is not None:
                _stop_engine_async(self._engine, self._stream_session)
            self._engine, self._stream_session = None, None
            self._set_streaming(False)
            self._active_source = source
            self._debrid_playback = False
            self._start_player(source.url, source=source)
            if getattr(source, "source", "") == "vyla" and not getattr(source, "subtitles", None):
                self._prefetch_vyla_subtitles(source)
            return

        if self._use_debrid_stream(source):
            self._play_gen += 1
            self._poll_timer.stop()
            if self._engine is not None:
                _stop_engine_async(self._engine, self._stream_session)
            self._engine, self._stream_session = None, None
            self._play_debrid_source(source, auto)
            return

        self._play_torrent_source(source)

    def _use_debrid_stream(self, source) -> bool:
        if not (getattr(settings, "debrid_enabled", False) and getattr(settings, "debrid_stream", True)):
            return False
        magnet = str(getattr(source, "magnet", "") or "")
        if not magnet.startswith("magnet:"):
            return False
        from src.core.debrid import configured
        return configured()

    def _play_debrid_source(self, source, auto: bool = False) -> None:
        episode, gen = self._stream_episode, self._play_gen
        self._active_source = source
        self._set_streaming(True, tr("tv.stream.debrid_resolving",
                                     default="Resolving the debrid link..."))

        def _start() -> tuple[str, str]:
            from src.core.debrid import DebridError, DebridService
            service = DebridService.from_settings()
            try:
                resolution = service.resolve_stream(
                    source.magnet, episode=episode, cancel=lambda: gen != self._play_gen)
                link = next((item for item in resolution.links if item.url), None)
                if link is None:
                    raise DebridError("The debrid provider returned no playable link.")
                return link.url, link.path
            finally:
                service.close()

        def _done(result) -> None:
            self._play_task_id = None
            if gen != self._play_gen:
                return
            url, _path = result
            self._set_streaming(False)
            self._debrid_playback = True
            self._start_player(url, source=source)

        def _failed(message: str) -> None:
            self._play_task_id = None
            if gen != self._play_gen:
                return
            logger.warning("Debrid stream failed: %s", message)
            if getattr(settings, "debrid_fallback", True):
                InfoBar.warning(
                    title=tr("tv.stream.debrid_fallback_title", default="Debrid unavailable"),
                    content=tr("tv.stream.debrid_fallback_body",
                               default="Falling back to peer-to-peer streaming..."),
                    duration=3500, parent=self)
                self._play_torrent_source(source)
                return
            self._stream_failed(str(message))

        self._play_task_id = _worker_module.submit(_start, on_done=_done, on_error=_failed)

    def _play_torrent_source(self, source) -> None:
        try:
            from src.core.tv.torrent_stream import get_streamer
        except Exception as exc:
            logger.exception("Could not load the streaming engine")
            InfoBar.error(title=tr("tv.stream.failed_title", default="Stream failed"),
                          content=str(exc), duration=6000, parent=self)
            return
        self._active_source = source
        self._debrid_playback = False
        self._set_streaming(True, tr("tv.stream.connecting", default="Connecting to peers..."))
        episode, gen = self._stream_episode, self._play_gen

        def _start() -> list:
            with _ENGINE_LOCK:
                engine = get_streamer()
                return [engine, engine.play(source, episode=episode)]

        def _done(res: list) -> None:
            self._play_task_id = None
            engine, session = res
            if gen != self._play_gen:
                _stop_engine_async(engine, session)
                return
            self._engine, self._stream_session = engine, session
            self._connect_ticks = 0
            self._poll_timer.start()

        def _failed(message: str) -> None:
            self._play_task_id = None
            if gen == self._play_gen:
                self._stream_failed(str(message))

        self._play_task_id = _worker_module.submit(_start, on_done=_done, on_error=_failed)

    def _stream_failed(self, message: str) -> None:
        self._poll_timer.stop()
        self._set_streaming(False)
        engine, session = self._engine, self._stream_session
        self._engine, self._stream_session = None, None
        self._active_source = None
        self._debrid_playback = False
        if engine is not None:
            _stop_engine_async(engine, session)
        nxt = next((src for src in self._stream_sources if id(src) not in self._tried_sources), None)
        if nxt is not None and len(self._tried_sources) < 4 and getattr(settings, "tv_stream_fallback", True):
            InfoBar.warning(title=tr("tv.stream.retry_title", default="Source didn't respond"),
                            content=tr("tv.stream.retry_body", default="Trying the next source..."),
                            duration=3000, parent=self)
            self._play_source(nxt, auto=True)
            return
        self._player_widget.hide()
        InfoBar.error(title=tr("tv.stream.failed_title", default="Stream failed"),
                      content=message or tr("tv.stream.stopped", default="The stream stopped."),
                      duration=8000, parent=self)

    def _on_player_error(self, message: str) -> None:
        source = self._active_source
        if source is None or not (getattr(source, "is_direct", False) or self._debrid_playback):
            return
        if not getattr(settings, "tv_stream_fallback", True):
            return
        self._active_source = None
        self._player_widget.stop()
        self._stream_failed(message)

    def _poll_stream(self) -> None:
        if self._engine is None or self._stream_session is None:
            self._poll_timer.stop()
            return
        status = self._engine.status()
        if status is None:
            self._poll_timer.stop()
            return
        state = status.get("state")
        if state == "ready":
            self._poll_timer.stop()
            self._set_streaming(False)
            self._start_player(status.get("url") or "")
        elif state == "error":
            self._stream_failed(status.get("error") or "")
        elif state == "stopped":
            self._poll_timer.stop()
            self._set_streaming(False)
            self._stream_session = None
        else:
            self._connect_ticks += 1
            progress = float(status.get("progress") or 0)
            peers = int(status.get("peers") or 0)
            if self._connect_ticks > 2 * int(getattr(settings, "tv_stream_connect_timeout", 60)) and progress <= 0 and peers == 0:
                self._stream_failed(tr("tv.stream.no_peers", default="No peers found for this source."))
                return
            label = (tr("tv.stream.buffering", default="Buffering...") if state == "buffering"
                     else tr("tv.stream.connecting", default="Connecting to peers..."))
            rate = stream_backend.human_size(int(status.get("rate") or 0))
            self._stream_status.setText(
                f"{label} · {int(round(progress * 100))}% · {rate}/s · {peers} {tr('tv.stream.peers', default='peers')}")


    def _resume_key(self) -> str:
        ep = self._stream_episode or (0, 0)
        return f"{self._media_type}:{self._media_id}:{ep[0]}:{ep[1]}"

    def _save_resume(self) -> None:
        if self._media_id is None or not self._player_widget.has_media() or not getattr(settings, "tv_stream_resume", True):
            return
        qs = QSettings("PiraChest", "TVResume")
        pos = self._player_widget.position_ms()
        dur = self._player_widget._player.duration()
        if pos > 30_000 and (dur <= 0 or pos < dur - 90_000):
            qs.setValue(self._resume_key(), int(pos))
        else:
            qs.remove(self._resume_key())

    def _prefetch_vyla_subtitles(self, source: Any) -> None:
        from src.core.tv import external as external_backend
        season, episode = self._stream_episode or (None, None)

        def _done(subtitles: list) -> None:
            if not subtitles:
                return
            try:
                if self._active_source is source:
                    self._player_widget._on_manifest_subs_ready(subtitles)
            except Exception:
                pass

        _worker_module.submit(
            external_backend.vyla_subtitles,
            args=(self._imdb_id or "", self._media_id, season, episode),
            on_done=_done, on_error=lambda _message: None,
        )

    def _start_player(self, url: str, source: Any = None) -> None:
        if not url:
            return
        title = self._title_lbl.text()
        if self._stream_episode:
            title = tr("tv.stream.pick_for", default="{title} (S{season}E{episode})",
                       title=title, season=self._stream_episode[0], episode=self._stream_episode[1])
        start = int(QSettings("PiraChest", "TVResume").value(self._resume_key(), 0) or 0) \
            if getattr(settings, "tv_stream_resume", True) else 0
        start = max(0, start - 3000) if start > 30_000 else 0
        self._player_widget.play_stream(
            url, referer=getattr(source, "referer", "") or None,
            headers=getattr(source, "headers", None) or None, title=title,
            subtitles=getattr(source, "subtitles", None) or None, start_ms=start)
        self._player_widget.show()
        self._player_widget.setFocus()
        self._player_widget.set_has_next(self._next_target() is not None)
        self._load_skip_segments()
        self._resume_timer.start()

    def _next_target(self) -> Optional[tuple[int, int]]:
        ep = self._stream_episode
        if self._media_type != "tv" or not ep:
            return None
        season, number = int(ep[0]), int(ep[1])
        later = [n for n in self._season_eps.get(season, []) if n > number]
        if later:
            return season, later[0]
        if season > 0 and self._season_combo.findData(season + 1) >= 0:
            return season + 1, 1
        return None

    def _play_next(self) -> None:
        target = self._next_target()
        if target is None:
            return
        self._save_resume()
        self._auto_pick = True
        self.open_episode(*target)

    def _on_player_ended(self) -> None:
        if self._next_target() is None:
            return
        if getattr(settings, "tv_autonext", False):
            self._play_next()
        else:
            self._player_widget.prompt_next()

    def _load_skip_segments(self) -> None:
        self._player_widget.set_skip_segments()
        if self._media_id is None:
            return
        ep = self._stream_episode
        self._skip_gen += 1
        gen = self._skip_gen

        def _done(data: dict) -> None:
            if gen == self._skip_gen:
                self._player_widget.set_skip_segments(data.get("intro"), data.get("credits"))
        tmdb.skip_segments_async(self._media_type, self._media_id, self._imdb_id or None,
                                 ep[0] if ep else None, ep[1] if ep else None,
                                 on_done=self._safe(_done), on_error=lambda *_a: None)

    def _set_streaming(self, on: bool, text: str = "") -> None:
        self._stream_spinner.setVisible(on)
        self._stream_spinner.set_active(on)
        self._stream_status.setText(text)
        self._watch_btn.setEnabled(not on)

    def _close_media_player(self) -> None:
        self._stop_playback()
        self._player_widget.release()

    def _stop_playback(self, wait: bool = False) -> None:
        self._play_gen += 1
        if self._play_task_id is not None:
            _worker_module.cancel(self._play_task_id)
            self._play_task_id = None
        self._resume_timer.stop()
        self._save_resume()
        self._poll_timer.stop()
        if self._engine is not None:
            if wait:
                with _ENGINE_LOCK:
                    try:
                        self._engine.stop()
                    except Exception:
                        logger.debug("Could not stop the stream", exc_info=True)
            else:
                _stop_engine_async(self._engine, self._stream_session)
        self._engine, self._stream_session = None, None
        self._active_source = None
        self._debrid_playback = False
        try:
            self._player_widget.stop()
            self._player_widget.hide()
        except (RuntimeError, TypeError):
            pass
        self._set_streaming(False)

    def shutdown(self) -> None:
        for task_id in self._task_ids:
            _worker_module.cancel(task_id)
        if self._season_task_id is not None:
            _worker_module.cancel(self._season_task_id)
        if self._imdb_task_id is not None:
            _worker_module.cancel(self._imdb_task_id)
            self._imdb_task_id = None
        if self._source_task_id is not None:
            _worker_module.cancel(self._source_task_id)
            self._source_task_id = None
        if self._download_task_id is not None:
            _worker_module.cancel(self._download_task_id)
            self._download_task_id = None
        self._stop_playback(wait=True)
        try:
            self._player_widget.shutdown()
        except (RuntimeError, TypeError):
            pass
        images.release(self)
        if getattr(settings, "tv_stream_clear_on_exit", False):
            from src.core.cache import clear_stream_cache
            clear_stream_cache()


_SERIES_CATEGORIES = (
    ("trending", "tv.series.trending"),
    ("popular", "tv.series.popular"),
    ("top_rated", "tv.series.top_rated"),
    ("on_the_air", "tv.series.on_the_air"),
)


class _MediaTab(MediaPageBase):
    _wrap = staticmethod(_TmdbItem)
    _SEARCH_PH = ("tv.movies.search_placeholder", "Search movies, shows...")

    def _create_details(self) -> QWidget:
        return _MediaDetailsView(self)

    def __init__(self, media_type: str, categories: tuple, parent=None) -> None:
        super().__init__(parent)
        self._media_type = media_type
        self._categories = categories
        self._query = ""
        self._loaded = False
        self._search_task_id: Optional[str] = None
        self._download_manager: Any = None
        self._force = False

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(10)

        self._stack = QStackedWidget(self)
        outer.addWidget(self._stack, 1)

        self._browse_page = QWidget(self)
        browse_layout = QVBoxLayout(self._browse_page)
        browse_layout.setContentsMargins(0, 0, 0, 0)
        browse_layout.setSpacing(12)

        self._search_box = SearchLineEdit(self._browse_page)
        self._search_box.setPlaceholderText(tr(self._SEARCH_PH[0], default=self._SEARCH_PH[1]))
        self._search_timer = QTimer(self)
        self._search_timer.setSingleShot(True)
        self._search_timer.setInterval(400)
        self._search_timer.timeout.connect(self._on_search_commit)
        self._search_box.textChanged.connect(lambda _t: self._search_timer.start())
        self._search_box.searchSignal.connect(self._on_search_commit)
        self._search_box.clearSignal.connect(self._on_search_commit)
        self._search_box.setMinimumWidth(280)
        self._search_box.setMaximumWidth(360)
        search_row = QHBoxLayout()
        search_row.setSpacing(10)
        search_row.addWidget(self._search_box, 0)
        self._watchlater_btn = TransparentToolButton(self._browse_page)
        apply_tool_button_theme(self._watchlater_btn)
        self._watchlater_btn.setIcon(_heart_icon())
        self._watchlater_btn.setIconSize(QSize(22, 22))
        self._watchlater_btn.setFixedSize(32, 32)
        self._watchlater_badge = QLabel(self._watchlater_btn)
        self._watchlater_badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._watchlater_badge.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self._watchlater_badge.setStyleSheet(count_badge_qss())
        self._watchlater_badge.hide()
        register_theme_refresh(self, self._refresh_watchlater_badge_style)
        self._watchlater_btn.clicked.connect(self._show_watch_later)
        watch_later_store().changed.connect(self._sync_watchlater_badge)
        QTimer.singleShot(0, self._sync_watchlater_badge)
        search_row.addWidget(self._watchlater_btn)
        self._refresh_btn = TransparentToolButton(FluentIcon.SYNC, self._browse_page)
        apply_tool_button_theme(self._refresh_btn)
        self._refresh_btn.setFixedSize(32, 32)
        self._refresh_btn.setToolTip(tr("tv.refresh", default="Refresh"))
        self._refresh_btn.clicked.connect(self.refresh)
        search_row.addWidget(self._refresh_btn)
        browse_layout.addLayout(search_row)

        self._status_lbl = BodyLabel("", self._browse_page)
        self._status_lbl.hide()
        browse_layout.addWidget(self._status_lbl)

        rows_scroll = SmoothScrollArea(self._browse_page)
        rows_scroll.setWidgetResizable(True)
        rows_scroll.setStyleSheet("background: transparent; border: none;")
        rows_inner = QWidget()
        rows_inner.setStyleSheet("background: transparent;")
        self._rows_layout = QVBoxLayout(rows_inner)
        self._rows_layout.setContentsMargins(0, 0, 0, 0)
        self._rows_layout.setSpacing(20)

        self._category_rows: dict[str, _MediaRow] = {}
        for key, title_key in categories:
            row = _MediaRow(tr(title_key, default=key.replace("_", " ").title()), media_type, self._open_details, rows_inner, self._wrap)
            row.context_requested.connect(self._on_card_menu)
            self._category_rows[key] = row
            self._rows_layout.addWidget(row)
        self._rows_layout.addStretch(1)
        rows_scroll.setWidget(rows_inner)
        self._rows_scroll = rows_scroll
        browse_layout.addWidget(rows_scroll, 1)

        self._search_grid = PosterGrid(self._browse_page, cover_attrs=("poster_url",), key_attr="id", show_caption=True,
                                       subtitle_fn=attrs_subtitle("subtitle"), colors_fn=_grid_colors)
        self._search_grid.poster_clicked.connect(lambda item: self._open_details(item.raw, self._media_type))
        self._search_grid.context_requested.connect(self._on_card_menu)
        self._search_grid.hide()
        self._view_btn = ViewModeButton(self._search_grid, parent=self._browse_page,
                                        list_tip=tr("tv.list_view", default="List view"),
                                        grid_tip=tr("tv.grid_view", default="Grid view"))
        apply_tool_button_theme(self._view_btn)
        self._view_btn.hide()
        search_row.addWidget(self._view_btn)
        search_row.addStretch(1)
        browse_layout.addWidget(self._search_grid, 1)

        self._stack.addWidget(self._browse_page)

        self._details = self._create_details()
        self._stack.addWidget(self._details)

        self._watchlater_view = _WatchLaterView(self)
        self._watchlater_view.open_requested.connect(self._on_watch_later_open)
        self._watchlater_view.back_requested.connect(self._show_grid)
        self._stack.addWidget(self._watchlater_view)

        self._stack.setCurrentWidget(self._browse_page)

        register_locale_refresh(self, self._apply_locale)

    def activate(self) -> None:
        if not self._loaded:
            self._loaded = True
            for key, _title_key in self._categories:
                self._load_category(key)

    def refresh(self) -> None:
        self._loaded = True
        self._force = True
        try:
            if self._query:
                self._on_search_commit()
            else:
                for key, _title_key in self._categories:
                    self._load_category(key)
        finally:
            self._force = False

    def _load_category(self, key: str) -> None:
        def _on_done(items: list) -> None:
            try:
                self._category_rows[key].set_items(items)
                if self._media_type == "movie":
                    tmdb.prefetch_movies([i.get("id") for i in items])
            except Exception:
                logger.exception("Failed to render %s row '%s'", self._media_type, key)

        def _on_error(msg: str) -> None:
            logger.error("%s category '%s' load error: %s", self._media_type, key, msg)

        self._start_category(key, _on_done, _on_error)

    def _start_category(self, key: str, on_done, on_error) -> None:
        def _done(data: dict) -> None:
            on_done(self._filter_released(data.get("results") or []))

        if key == "trending":
            tmdb.trending_async(self._media_type, "week", 1, use_cache=not self._force, on_done=_done, on_error=on_error)
        elif key == "popular":
            tmdb.popular_async(self._media_type, 1, use_cache=not self._force, on_done=_done, on_error=on_error)
        elif key == "top_rated":
            tmdb.top_rated_async(self._media_type, 1, use_cache=not self._force, on_done=_done, on_error=on_error)
        elif key == "now_playing" and self._media_type == "movie":
            tmdb.now_playing_async(1, use_cache=not self._force, on_done=_done, on_error=on_error)
        elif key == "on_the_air" and self._media_type == "tv":
            tmdb.on_the_air_async(1, use_cache=not self._force, on_done=_done, on_error=on_error)

    @staticmethod
    def _filter_released(results: list[dict]) -> list[dict]:
        today = _dt.date.today().isoformat()
        out = []
        for r in results:
            date = r.get("release_date") or r.get("first_air_date") or ""
            if date and date <= today:
                out.append(r)
        return out

    def _on_search_commit(self) -> None:
        self._query = self._search_box.text().strip()
        if self._search_task_id is not None:
            _worker_module.cancel(self._search_task_id)
        if not self._query:
            self._search_grid.hide()
            self._view_btn.hide()
            self._search_grid.clear()
            self._rows_scroll.show()
            return
        self._rows_scroll.hide()
        self._status_lbl.setText(tr("tv.movies.loading", default="Loading..."))
        self._status_lbl.show()

        def _on_done(results: list) -> None:
            self._search_task_id = None
            self._status_lbl.hide()
            self._render_search(results)

        def _on_error(msg: str) -> None:
            self._search_task_id = None
            self._status_lbl.setText(tr("tv.movies.load_error", default="Failed to load"))
            logger.error("%s search error: %s", self._media_type, msg)

        self._search_task_id = self._start_search(self._query, _on_done, _on_error)

    def _start_search(self, query: str, on_done, on_error):
        return tmdb.search_async(self._media_type, query, 1, use_cache=not self._force, on_error=on_error,
                                 on_done=lambda data: on_done(self._filter_released(data.get("results") or [])))

    def _render_search(self, results: list[dict]) -> None:
        self._search_grid.set_entries([self._wrap(item) for item in results])
        self._search_grid.show()
        self._view_btn.show()

    def _open_details(self, item: dict, media_type: str) -> None:
        try:
            title = item.get("title") or item.get("name") or ""
            self._details.show_media(item.get("id"), media_type, title, raw=item)
            self._switch_stack_animated(self._details)
        except Exception:
            logger.exception("Failed to open media details")

    def _show_grid(self) -> None:
        self._details.banner_active = False
        self._details.banner_changed.emit()
        self._details.shutdown()
        self._switch_stack_animated(self._browse_page)

    def set_download_manager(self, manager) -> None:
        self._download_manager = manager
        self._details.set_download_manager(manager)


    def _refresh_watchlater_badge_style(self) -> None:
        badge = getattr(self, '_watchlater_badge', None)
        if badge is not None:
            with suppress(RuntimeError):
                badge.setStyleSheet(count_badge_qss())

    def _sync_watchlater_badge(self) -> None:
        n = watch_later_store().count()
        label = tr('tv.watchlater', default='Watch later')
        self._watchlater_btn.setToolTip(f'{label} ({n})' if n else label)
        if n > 0:
            text = '99+' if n > 99 else str(n)
            w = 16 if len(text) < 2 else 22
            self._watchlater_badge.setText(text)
            self._watchlater_badge.resize(w, 15)
            self._watchlater_badge.move(32 - w, 0)
            self._watchlater_badge.show()
        else:
            self._watchlater_badge.hide()

    def _show_watch_later(self) -> None:
        self._watchlater_view.refresh()
        self._switch_stack_animated(self._watchlater_view)

    def _on_watch_later_open(self, entry: dict) -> None:
        tv_page = self.parent()
        while tv_page is not None and not hasattr(tv_page, 'open_watch_later_entry'):
            tv_page = tv_page.parent()
        if tv_page is not None:
            tv_page.open_watch_later_entry(entry)


    def _on_card_menu(self, item, pos) -> None:
        raw = getattr(item, 'raw', None)
        if not isinstance(raw, dict):
            return
        menu = RoundMenu(parent=self)
        menu.addAction(Action(FluentIcon.VIEW, tr('tv.watchlater_open', default='Open'),
                              triggered=lambda: self._open_details(raw, self._media_type)))
        if getattr(settings, 'tv_stream_enabled', True) and self._media_type == 'movie':
            menu.addAction(Action(_play_icon(), tr('tv.stream.play', default='Play'),
                                  triggered=lambda: self._open_and_watch(raw)))
        if self._download_manager is not None:
            menu.addAction(Action(FluentIcon.DOWNLOAD, tr('tv.download.action', default='Download...'),
                                  triggered=lambda: self._open_and_download(raw)))
        saved = watch_later_store().has(self._media_type, raw.get('id'))
        menu.addAction(Action(themed_icon('tv.watchlater', FluentIcon.HEART),
                              tr('tv.watchlater_remove', default='Remove from watch later') if saved
                              else tr('tv.watchlater_add', default='Watch later'),
                              triggered=lambda: self._toggle_card_watch_later(raw)))
        menu.exec(pos)

    def _open_and_watch(self, raw: dict) -> None:
        self._open_details(raw, self._media_type)
        self._details.start_watch()

    def _open_and_download(self, raw: dict) -> None:
        self._open_details(raw, self._media_type)
        self._details.start_download()

    def _toggle_card_watch_later(self, raw: dict) -> None:
        media_id = raw.get('id')
        if media_id is None:
            return
        store = watch_later_store()
        if store.has(self._media_type, media_id):
            store.remove(self._media_type, media_id)
        else:
            store.add(self._card_watch_later_entry(raw))

    def _card_watch_later_entry(self, raw: dict) -> dict:
        date = str(raw.get('release_date') or raw.get('first_air_date') or '')
        return {
            'media_type': self._media_type,
            'id': raw.get('id'),
            'title': raw.get('title') or raw.get('name') or '',
            'year': date[:4],
            'rating': raw.get('vote_average'),
            'poster_url': tmdb.image_url(raw.get('poster_path'), 'w185'),
            'raw': raw,
        }

    def _apply_locale(self, *_args) -> None:
        self._search_box.setPlaceholderText(tr(self._SEARCH_PH[0], default=self._SEARCH_PH[1]))
        self._sync_watchlater_badge()
        self._watchlater_view.apply_locale()

    def shutdown(self) -> None:
        if self._search_task_id is not None:
            _worker_module.cancel(self._search_task_id)
        self._details.shutdown()
        self._watchlater_view.shutdown()
        try:
            images.release(self)
        except (TypeError, RuntimeError):
            pass


_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
_ad_hoc_client = LazyHttpClient(timeout=20, follow_redirects=True, limits=httpx.Limits(max_connections=8, max_keepalive_connections=8))

def close_ad_hoc_client() -> None:
    try:
        _ad_hoc_client.close()
    except Exception:
        pass


async def _find_media_sources(imdb_id: str, media_type: str, season: Optional[int] = None,
                              episode: Optional[int] = None, title: str = "",
                              year: str = "", tmdb_id: Optional[int] = None,
                              quick: bool = False) -> list:
    return await stream_backend.find_streams(imdb_id, media_type, season, episode,
                                              title=title, year=year, tmdb_id=tmdb_id,
                                              quick=quick)

def _icon(name: str, fallback=FluentIcon.PLAY):
    return getattr(FluentIcon, name, fallback)


_PLACEHOLDER_DARK = QColor(50, 38, 72)
_PLACEHOLDER_LIGHT = QColor(210, 200, 220)

def _placeholder_color() -> QColor:
    return _PLACEHOLDER_DARK if isDarkTheme() else _PLACEHOLDER_LIGHT

_ANIME_CATEGORIES = (
    ("trending", "anime.cat.trending"),
    ("popular", "anime.cat.popular"),
    ("top_rated", "anime.cat.top_rated"),
    ("airing_now", "anime.cat.airing"),
    ("this_season", "anime.cat.season"),
    ("upcoming", "anime.cat.upcoming"),
    ("movies", "anime.cat.movies"),
)
_ANIME_CAT_MAP = {"airing_now": "airing", "this_season": "season"}


class _AnimeCard:
    __slots__ = ('raw', 'id', 'title', 'poster_url', 'subtitle')

    def __init__(self, item):
        self.raw = item
        self.id = item.id
        self.title = getattr(item, 'title', None) or "Untitled"
        self.poster_url = getattr(item, 'artwork_url', None)
        self.subtitle = " \u00b7 ".join(str(v) for v in (getattr(item, 'format', None), getattr(item, 'year', None)) if v)


def _safe_int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return value


def _anime_card_raw(item) -> dict:
    title = {}
    for key, attr in (('english', 'title_english'), ('romaji', 'title_romaji'), ('native', 'title_native')):
        value = getattr(item, attr, '') or ''
        if value:
            title[key] = value
    if not title:
        title['romaji'] = getattr(item, 'title', '') or ''
    cover = getattr(item, 'artwork_url', None)
    return {
        'id': _safe_int(getattr(item, 'id', None)),
        'title': title,
        'coverImage': {'large': cover} if cover else None,
        'seasonYear': getattr(item, 'season_year', None),
        'format': getattr(item, 'format', '') or None,
        'status': getattr(item, 'status', '') or None,
        'episodes': getattr(item, 'episodes', None),
    }


async def _browse_anime(category: str) -> list[AnimeItem]:
    results = await anime_backend.browse_category(_ANIME_CAT_MAP.get(category, category))
    results = await anime_backend.collapse_to_franchises(results)
    return [AnimeItem.from_anilist_media(r) for r in results]


async def _search_anime(query: str) -> list[AnimeItem]:
    results = await anime_backend.search_cached(query)
    results = await anime_backend.collapse_to_franchises(results)
    return [AnimeItem.from_anilist_media(r) for r in results]

async def _load_anime_seasons(anilist_id: Any) -> list[dict]:
    return await anime_backend.get_seasons(anilist_id)

async def _load_anime_media(anilist_id: Any) -> dict:
    return await anime_backend.get_media(anilist_id)

_EP_CACHE: dict = {}

_PROV_CACHE: dict = {}


def _lazy_provider_names() -> list[str]:
    registry = getattr(anime_backend, "PROVIDER_REGISTRY", None)
    if not registry or not callable(getattr(anime_backend, "get_filtered_episodes_response", None)):
        return []
    names = [n for n in registry if isinstance(n, str)]
    if _DEFAULT_PROVIDER in names:
        names.remove(_DEFAULT_PROVIDER)
        names.insert(0, _DEFAULT_PROVIDER)
    return names


async def _load_anime_provider_episodes(anilist_id: Any, provider: str) -> dict:
    key = (anilist_id, provider)
    hit = _PROV_CACHE.get(key)
    if hit and hit[0] > time.monotonic():
        return hit[1]
    resp = await anime_backend.get_group_provider_episodes(anilist_id, provider)
    if isinstance(resp, dict) and "notAired" in resp:
        return resp
    data = (resp or {}).get(provider) or {}
    if isinstance(data, dict) and data.get("episodes"):
        if len(_PROV_CACHE) > 120:
            _PROV_CACHE.clear()
        _PROV_CACHE[key] = (time.monotonic() + 600, data)
    return data


async def _load_anime_episodes(anilist_id: Any) -> dict:
    hit = _EP_CACHE.get(anilist_id)
    if hit and hit[0] > time.monotonic():
        return hit[1]
    data = await anime_backend.get_episodes_response(anilist_id)
    if data and any(isinstance(v, dict) and v.get("episodes") for v in data.values()):
        if len(_EP_CACHE) > 40:
            _EP_CACHE.clear()
        _EP_CACHE[anilist_id] = (time.monotonic() + 600, data)
    return data


def _not_aired_label(pending: dict) -> str:
    from PySide6.QtCore import QDate, QDateTime, QLocale
    locale = QLocale()
    when = pending.get("airingAt")
    if when:
        text = locale.toString(QDateTime.fromSecsSinceEpoch(int(when)), QLocale.FormatType.ShortFormat)
        return tr("anime.not_aired_since", default="Not aired yet - the first episode arrives {date}.",
                  date=text)
    start = pending.get("startDate") or {}
    if start.get("year") and start.get("month") and start.get("day"):
        text = locale.toString(QDate(start["year"], start["month"], start["day"]),
                               QLocale.FormatType.ShortFormat)
        return tr("anime.not_aired_since", default="Not aired yet - the first episode arrives {date}.",
                  date=text)
    year = start.get("year") or pending.get("year")
    if year:
        return tr("anime.not_aired_year", default="Not aired yet - expected in {year}.", year=year)
    return tr("anime.not_aired", default="This anime hasn't aired yet.")

async def _load_stream(provider: str, anilist_id: Any, audio: str, episode_number: Any) -> dict:
    path = f"/watch/{provider}/{anilist_id}/{audio}/{provider}-{episode_number}"
    return await anime_backend.handle(path)

import queue as _queue
import ssl
from concurrent.futures import Future

_HTTP_TIMEOUT = httpx.Timeout(connect=6.0, read=15.0, write=10.0, pool=8.0)


def _new_session(insecure: bool = False) -> httpx.Client:
    verify = False if insecure else shared_ssl_context()
    return httpx.Client(
        timeout=_HTTP_TIMEOUT, follow_redirects=True,
        limits=httpx.Limits(max_connections=16, max_keepalive_connections=8, keepalive_expiry=30),
        transport=httpx.HTTPTransport(retries=2, verify=verify),
    )


_TLS_FAILED_HOSTS: set[str] = set()


def _is_cert_error(exc: BaseException) -> bool:
    seen: set[int] = set()
    e: Optional[BaseException] = exc
    while e is not None and id(e) not in seen:
        seen.add(id(e))
        if isinstance(e, ssl.SSLCertVerificationError):
            return True
        e = e.__cause__ or e.__context__
    return False


class _UpstreamError(Exception):
    def __init__(self, status: int = 502):
        super().__init__(status)
        self.status = status


class _NotCacheable(Exception):
    pass


_MPEGURL = "application/vnd.apple.mpegurl"
_NET_ERRORS = (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError, httpx.HTTPError, httpx.StreamError)


class _RelayHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    disable_nagle_algorithm = True
    wbufsize = 1 << 16
    timeout = 60

    def log_message(self, *_args):
        pass

    def _send(self, status: int, ctype: str, body: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        relay: HlsRelayServer = self.server.relay
        path = self.path.split("?")[0]
        url = relay.resolve(path)
        if not url:
            self.send_error(404, "Unknown relay path")
            return
        rng = self.headers.get("Range")
        try:
            if url.split("?")[0].lower().endswith(".m3u8"):
                self._send(200, _MPEGURL, relay.playlist(url).encode("utf-8"))
                return
            if not rng and relay.is_cacheable(path):
                try:
                    data, ctype = relay.segment(path)
                except _NotCacheable:
                    pass
                else:
                    if data[:7] == b"#EXTM3U":
                        body = relay.rewrite_playlist(data.decode("utf-8", "replace"), url).encode("utf-8")
                        self._send(200, _MPEGURL, body)
                    else:
                        self._send(200, ctype, data)
                    return
            self._passthrough(relay, url, rng)
        except _UpstreamError as exc:
            try:
                self.send_error(exc.status, "Upstream error")
            except _NET_ERRORS:
                pass
        except _NET_ERRORS:
            pass
        except Exception as exc:
            logger.warning("HLS relay error for %s: %s", url, exc)
            try:
                self.send_error(502, "Relay error")
            except _NET_ERRORS:
                pass

    def _passthrough(self, relay: "HlsRelayServer", url: str, rng: Optional[str]) -> None:
        headers = relay.headers()
        if rng:
            headers["Range"] = rng
        retry_headers = None
        if "Referer" not in headers:
            extra = relay._self_referer(url)
            retry_headers = {**headers, **extra} if extra else None
        upstream = None
        session = relay._session_for(url)
        for attempt in range(3):
            try:
                upstream = session.send(session.build_request("GET", url, headers=headers), stream=True)
            except httpx.HTTPError as exc:
                if _is_cert_error(exc):
                    relay._note_cert_failure(url)
                    session = relay._session_for(url)
                if attempt >= 1:
                    logger.warning("HLS relay upstream fetch failed for %s: %s", url, exc)
                    raise _UpstreamError(502) from exc
                continue
            if upstream.status_code in (401, 403) and retry_headers is not None:
                upstream.close()
                headers, retry_headers = retry_headers, None
                continue
            break
        try:
            status = upstream.status_code
            if status >= 400 and status != 416:
                raise _UpstreamError(status)
            ctype = upstream.headers.get("Content-Type", "application/octet-stream")
            if "mpegurl" in ctype.lower():
                text = upstream.read().decode("utf-8", "replace")
                self._send(200, _MPEGURL, relay.rewrite_playlist(text, url).encode("utf-8"))
                return
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            length = upstream.headers.get("Content-Length")
            if length:
                self.send_header("Content-Length", length)
            else:
                self.send_header("Connection", "close")
                self.close_connection = True
            for h in ("Content-Range", "Accept-Ranges"):
                v = upstream.headers.get(h)
                if v:
                    self.send_header(h, v)
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            for chunk in upstream.iter_raw(chunk_size=1 << 16):
                if chunk:
                    self.wfile.write(chunk)
                    self.wfile.flush()
        finally:
            upstream.close()


_TAG_RE = re.compile(r'URI="([^"]+)"')
_SUBTITLE_MEDIA_RE = re.compile(r"^#EXT-X-MEDIA:TYPE=SUBTITLES", re.IGNORECASE)
_STREAM_INF_RE = re.compile(r"^#EXT-X-STREAM-INF:", re.IGNORECASE)
_STRIP_SUBTITLES_ATTR_RE = re.compile(r',?SUBTITLES="[^"]*"')
_SANITIZE_FILENAME_RE = re.compile(r'[\\/:*?"<>|]')
_KEY_URI_RE = re.compile(r'URI="([^"]+)"')
_KEY_IV_RE = re.compile(r"IV=0x([0-9a-fA-F]+)")
_RESOLUTION_DIGITS_RE = re.compile(r"(\d{3,4})")


class _RelayServer(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 64
    allow_reuse_address = True

    def __init__(self, addr, handler, relay):
        self.relay = relay
        super().__init__(addr, handler)

    def handle_error(self, *_args) -> None:
        pass


class HlsRelayServer:
    PREFETCH_AHEAD = 6
    CACHE_MAX_BYTES = 96 << 20
    ENTRY_MAX_BYTES = 40 << 20
    PREFETCH_WORKERS = 4

    def __init__(self) -> None:
        self._server: Optional[_RelayServer] = None
        self._lock = threading.RLock()
        self._path_map: dict[str, str] = {}
        self._url_map: dict[str, str] = {}
        self._cacheable: set[str] = set()
        self._next_seg: dict[str, str] = {}
        self._pl_slots: dict[tuple[str, int], str] = {}
        self._next_id = 0
        self._epoch = 0
        self._last_seg: Optional[str] = None
        self.referer: Optional[str] = None
        self.extra_headers: dict = {}
        self._session = _new_session()
        self._insecure_session: Optional[httpx.Client] = None
        self._cache: "OrderedDict[str, tuple[bytes, str]]" = OrderedDict()
        self._cache_bytes = 0
        self._inflight: dict[str, Future] = {}
        self._raw_pl: dict[str, tuple[float, str]] = {}
        self._pq: "_queue.Queue[Optional[str]]" = _queue.Queue()
        self._workers: list[threading.Thread] = []

    def _session_for(self, url: str) -> httpx.Client:
        if urllib.parse.urlsplit(url).hostname in _TLS_FAILED_HOSTS:
            if self._insecure_session is None:
                self._insecure_session = _new_session(insecure=True)
            return self._insecure_session
        return self._session

    def _note_cert_failure(self, url: str) -> None:
        host = urllib.parse.urlsplit(url).hostname
        if host and host not in _TLS_FAILED_HOSTS:
            _TLS_FAILED_HOSTS.add(host)
            logger.warning("HLS relay: certificate verification failed for %s - continuing unverified", host)


    def start(self) -> int:
        if self._server is not None:
            return self._server.server_port
        self._server = _RelayServer(("127.0.0.1", 0), _RelayHandler, self)
        threading.Thread(target=self._server.serve_forever, kwargs={"poll_interval": 0.1}, daemon=True).start()
        self._pq = _queue.Queue()
        self._workers = [threading.Thread(target=self._prefetch_loop, args=(self._pq,), daemon=True)
                         for _ in range(self.PREFETCH_WORKERS)]
        for t in self._workers:
            t.start()
        return self._server.server_port

    def stop(self) -> None:
        for _ in self._workers:
            self._pq.put(None)
        self._workers = []
        srv, self._server = self._server, None
        if srv is not None:
            threading.Thread(target=lambda: (srv.shutdown(), srv.server_close()), daemon=True).start()
        self._reset_state(drain_queue=False)
        try:
            self._session.close()
        except Exception:
            pass
        self._session = _new_session()
        if self._insecure_session is not None:
            try:
                self._insecure_session.close()
            except Exception:
                pass
            self._insecure_session = None

    def _reset_state(self, drain_queue: bool = True) -> None:
        with self._lock:
            self._epoch += 1
            self._path_map.clear()
            self._url_map.clear()
            self._cacheable.clear()
            self._next_seg.clear()
            self._pl_slots.clear()
            self._cache.clear()
            self._cache_bytes = 0
            self._raw_pl.clear()
            self._inflight.clear()
            self._last_seg = None
        if drain_queue:
            try:
                while True:
                    self._pq.get_nowait()
            except _queue.Empty:
                pass

    def set_stream(self, root_url: str, referer: Optional[str] = None, extra_headers: Optional[dict] = None, live: bool = False) -> str:
        self.PREFETCH_AHEAD, self.CACHE_MAX_BYTES = (3, 32 << 20) if live else (6, 96 << 20)
        if self._server is None:
            self.start()
        self.referer = referer
        self.extra_headers = extra_headers or {}
        self._reset_state()
        with self._lock:
            local = self._register(root_url)
        return f"http://127.0.0.1:{self._server.server_port}{local}"

    def register(self, absolute_url: str) -> str:
        if self._server is None:
            self.start()
        with self._lock:
            local = self._register(absolute_url)
        return f"http://127.0.0.1:{self._server.server_port}{local}"

    def resolve(self, path: str) -> Optional[str]:
        with self._lock:
            return self._path_map.get(path)

    def is_cacheable(self, path: str) -> bool:
        return path in self._cacheable

    def headers(self) -> dict:
        h = {"User-Agent": _UA, "Accept-Encoding": "identity"}
        if self.referer:
            h["Referer"] = self.referer
            try:
                p = urllib.parse.urlsplit(self.referer)
                if p.scheme and p.netloc:
                    h["Origin"] = f"{p.scheme}://{p.netloc}"
            except ValueError:
                pass
        h.update(self.extra_headers or {})
        return h


    @staticmethod
    def _self_referer(url: str) -> dict:
        try:
            p = urllib.parse.urlsplit(url)
        except ValueError:
            return {}
        if not p.scheme or not p.netloc:
            return {}
        origin = f"{p.scheme}://{p.netloc}"
        return {"Referer": f"{origin}/", "Origin": origin}

    def _get(self, url: str) -> tuple[bytes, str]:
        scheme = urllib.parse.urlsplit(url).scheme.lower()
        if scheme not in ("http", "https"):
            logger.warning("HLS relay refused a non-http upstream url: %r", url[:120])
            raise _UpstreamError(400)
        last: Exception = _UpstreamError(502)
        headers = self.headers()

        retry_headers = None
        if "Referer" not in headers:
            extra = self._self_referer(url)
            retry_headers = {**headers, **extra} if extra else None
        session = self._session_for(url)
        for attempt in range(3):
            try:
                with session.stream("GET", url, headers=headers) as r:
                    if r.status_code >= 400:
                        if r.status_code in (401, 403) and retry_headers is not None:
                            headers, retry_headers = retry_headers, None
                            last = _UpstreamError(r.status_code)
                        elif r.status_code in (408, 425, 429) or r.status_code >= 500:
                            last = _UpstreamError(r.status_code)
                        else:
                            raise _UpstreamError(r.status_code)
                    else:
                        length = r.headers.get("Content-Length")
                        if length and length.isdigit() and int(length) > self.ENTRY_MAX_BYTES:
                            raise _NotCacheable()
                        return r.read(), r.headers.get("Content-Type", "application/octet-stream")
            except httpx.HTTPError as exc:
                last = exc
                if _is_cert_error(exc):
                    self._note_cert_failure(url)
                    session = self._session_for(url)
            if attempt < 2:
                time.sleep(0.25 * (attempt + 1))
        if isinstance(last, _UpstreamError):
            raise last
        logger.warning("HLS relay upstream failed for %s: %s", url, last)
        raise _UpstreamError(502)

    def fetch_text(self, url: str) -> str:
        now = time.monotonic()
        with self._lock:
            hit = self._raw_pl.get(url)
            if hit and hit[0] > now:
                return hit[1]
        try:
            data, _ct = self._get(url)
        except _UpstreamError:
            with self._lock:
                stale = self._raw_pl.get(url)
            if stale is None:
                raise


            logger.debug("HLS relay replaying last known playlist for %s", url)
            return stale[1]
        text = data.decode("utf-8", "replace")
        ttl = 600.0 if ("#EXT-X-ENDLIST" in text or "#EXT-X-STREAM-INF" in text) else 0.8
        with self._lock:
            if len(self._raw_pl) > 48:
                self._raw_pl.clear()
            self._raw_pl[url] = (now + ttl, text)
        return text

    def playlist(self, url: str) -> str:
        return self.rewrite_playlist(self.fetch_text(url), url)


    def segment(self, path: str) -> tuple[bytes, str]:
        with self._lock:
            if self._last_seg is not None and path != self._next_seg.get(self._last_seg) and path != self._last_seg:
                try:
                    while True:
                        self._pq.get_nowait()
                except _queue.Empty:
                    pass
            self._last_seg = path
        res = self._ensure(path)
        self._schedule_ahead(path)
        return res

    def _ensure(self, path: str) -> tuple[bytes, str]:
        with self._lock:
            epoch = self._epoch
            hit = self._cache.get(path)
            if hit is not None:
                self._cache.move_to_end(path)
                return hit
            fut = self._inflight.get(path)
            owner = fut is None
            if owner:
                fut = self._inflight[path] = Future()
            url = self._path_map.get(path)
        if not owner:
            return fut.result(timeout=40)
        try:
            if not url:
                raise _UpstreamError(404)
            data, ctype = self._get(url)
            if ctype.lower().startswith("image/"):
                peek = data[:12]
                real_img = (peek[:3] == b"\xff\xd8\xff" or peek[:8] == b"\x89PNG\r\n\x1a\n"
                            or peek[:6] in (b"GIF87a", b"GIF89a") or (peek[:4] == b"RIFF" and peek[8:12] == b"WEBP"))
                if real_img and peek[:1] != b"\x47":
                    logger.warning("HLS relay got a genuine image for %s - likely hotlink-blocked", url)
                    raise _UpstreamError(502)
                ctype = "video/mp2t"
            res = (data, ctype)
            with self._lock:
                if epoch == self._epoch and len(data) <= self.ENTRY_MAX_BYTES:
                    self._cache[path] = res
                    self._cache_bytes += len(data)
                    while self._cache_bytes > self.CACHE_MAX_BYTES and len(self._cache) > 1:
                        _k, (old, _c) = self._cache.popitem(last=False)
                        self._cache_bytes -= len(old)
            fut.set_result(res)
            return res
        except BaseException as exc:
            fut.set_exception(exc)
            raise
        finally:
            with self._lock:
                if self._inflight.get(path) is fut:
                    self._inflight.pop(path, None)

    def _schedule_ahead(self, path: str) -> None:
        with self._lock:
            p = path
            for _ in range(self.PREFETCH_AHEAD):
                p = self._next_seg.get(p)
                if not p:
                    break
                if p not in self._cache and p not in self._inflight:
                    self._pq.put(p)

    def _prefetch_loop(self, q) -> None:
        while True:
            p = q.get()
            if p is None:
                return
            try:
                self._ensure(p)
            except BaseException:
                pass


    def _register(self, absolute_url: str, cacheable: bool = False) -> str:
        path = self._url_map.get(absolute_url)
        if path is None:
            path = f"/relay/{self._next_id}{self._guess_extension(absolute_url)}"
            self._next_id += 1
            self._path_map[path] = absolute_url
            self._url_map[absolute_url] = path
        if cacheable and not path.endswith(".m3u8"):
            self._cacheable.add(path)
        return path

    def _register_playlist_slot(self, base_url: str, slot: int, absolute_url: str) -> str:
        path = self._pl_slots.get((base_url, slot))
        if path is None:
            path = self._register(absolute_url)
            self._pl_slots[(base_url, slot)] = path
            return path
        old = self._path_map.get(path)
        if old != absolute_url:
            if old is not None:
                self._url_map.pop(old, None)
            self._path_map[path] = absolute_url
            self._url_map[absolute_url] = path
        return path

    _ALLOWED_EXTS = {
        ".3gp", ".aac", ".avi", ".flac", ".mkv", ".m3u8", ".mov", ".mp3",
        ".mp4", ".mpegts", ".mpd", ".ogg", ".ogv", ".ts", ".vtt", ".webm",
    }
    _FMP4_REMAP = {".m4s", ".cmfv", ".cmfa", ".m4v", ".m4a", ".mp4a"}

    @classmethod
    def _guess_extension(cls, absolute_url: str) -> str:
        clean = absolute_url.split("?")[0].split("#")[0]
        dot = clean.rfind(".")
        slash = clean.rfind("/")
        if dot > slash and dot != -1:
            ext = clean[dot:].lower()
            if 1 < len(ext) <= 6:
                if ext in cls._ALLOWED_EXTS:
                    return ext
                if ext in cls._FMP4_REMAP:
                    return ".mp4"
        return ".ts"

    def rewrite_playlist(self, text: str, base_url: str) -> str:
        out = []
        prev: Optional[str] = None
        pl_slot = 0
        with self._lock:
            for line in text.splitlines():
                s = line.strip()
                if not s:
                    out.append(line)
                    continue
                if s.startswith("#"):
                    if _SUBTITLE_MEDIA_RE.match(s):
                        continue
                    if _STREAM_INF_RE.match(s):
                        s = _STRIP_SUBTITLES_ATTR_RE.sub("", s)
                    m = _TAG_RE.search(s)
                    if m:
                        u = urljoin(base_url, m.group(1))
                        cacheable = s.startswith(("#EXT-X-MAP", "#EXT-X-KEY", "#EXT-X-SESSION-KEY"))
                        if not cacheable and u.split("?")[0].lower().endswith(".m3u8"):
                            lp = self._register_playlist_slot(base_url, pl_slot, u)
                            pl_slot += 1
                        else:
                            lp = self._register(u, cacheable=cacheable)
                        s = s[:m.start(1)] + lp + s[m.end(1):]
                    out.append(s)
                    continue
                u = urljoin(base_url, s)
                if u.split("?")[0].lower().endswith(".m3u8"):
                    lp = self._register_playlist_slot(base_url, pl_slot, u)
                    pl_slot += 1
                else:
                    lp = self._register(u, cacheable=True)
                if prev is not None and lp != prev and lp in self._cacheable:
                    self._next_seg[prev] = lp
                prev = lp if lp in self._cacheable else None
                out.append(lp)
        return "\n".join(out)


_relay: Optional[HlsRelayServer] = None

def get_relay() -> HlsRelayServer:
    global _relay
    if _relay is None:
        _relay = HlsRelayServer()
    return _relay

def _format_ms(ms: int) -> str:
    total_s = max(0, ms) // 1000
    h, rem = divmod(total_s, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"

_SPEEDS = (0.5, 0.75, 1.0, 1.25, 1.5, 2.0)
_ASPECT_W, _ASPECT_H = 16, 9
_QWIDGETSIZE_MAX = 16777215

_STREAM_INF_RE = re.compile(r"^#EXT-X-STREAM-INF:", re.IGNORECASE)
_RESOLUTION_RE = re.compile(r"RESOLUTION=(\d+)x(\d+)", re.IGNORECASE)
_BANDWIDTH_RE = re.compile(r"BANDWIDTH=(\d+)", re.IGNORECASE)

def _parse_hls_variants(playlist_text: str, base_url: str) -> list[tuple[Optional[int], int, str]]:
    lines = playlist_text.splitlines()
    variants: list[tuple[Optional[int], int, str]] = []
    for i, line in enumerate(lines):
        if not _STREAM_INF_RE.match(line.strip()):
            continue
        attrs = line.split(":", 1)[1] if ":" in line else ""
        res_m = _RESOLUTION_RE.search(attrs)
        bw_m = _BANDWIDTH_RE.search(attrs)
        height = int(res_m.group(2)) if res_m else None
        bandwidth = int(bw_m.group(1)) if bw_m else 0
        uri = None
        for j in range(i + 1, len(lines)):
            cand = lines[j].strip()
            if not cand or cand.startswith("#"):
                continue
            uri = cand
            break
        if uri:
            variants.append((height, bandwidth, urljoin(base_url, uri)))

    seen: set[str] = set()
    out: list[tuple[Optional[int], int, str]] = []
    for height, bandwidth, variant_url in sorted(variants, key=lambda v: (v[0] or 0, v[1]), reverse=True):
        if variant_url in seen:
            continue
        seen.add(variant_url)
        out.append((height, bandwidth, variant_url))
    return out

_MEDIA_SUBS_RE = re.compile(r'^#EXT-X-MEDIA:TYPE=SUBTITLES', re.IGNORECASE)
_MEDIA_ATTR_RE = re.compile(r'([A-Z-]+)=(?:"([^"]*)"|([^,]*))')

def _parse_hls_subtitle_groups(playlist_text: str, base_url: str) -> list[dict]:
    out: list[dict] = []
    for line in playlist_text.splitlines():
        stripped = line.strip()
        if not _MEDIA_SUBS_RE.match(stripped):
            continue
        attrs = {}
        for m in _MEDIA_ATTR_RE.finditer(stripped):
            key = m.group(1).upper()
            attrs[key] = m.group(2) if m.group(2) is not None else m.group(3)
        uri = attrs.get("URI")
        if not uri:
            continue
        out.append({
            "url": urljoin(base_url, uri),
            "label": attrs.get("NAME") or attrs.get("LANGUAGE") or "Subtitle",
            "srclang": attrs.get("LANGUAGE") or "",
            "default": str(attrs.get("DEFAULT", "")).upper() == "YES",
        })
    return out

_CUE_TIME_RE = re.compile(
    r"(?:(\d{1,2}):)?(\d{2}):(\d{2})[.,](\d{3})\s*-->\s*(?:(\d{1,2}):)?(\d{2}):(\d{2})[.,](\d{3})"
)
_TAG_STRIP_RE = re.compile(r"<[^>]+>")

_ASS_TIME_RE = re.compile(r"(\d+):(\d{2}):(\d{2})[.](\d{2})")
_ASS_TAG_RE = re.compile(r"\{[^}]*\}")


def _parse_ass_cues(text: str) -> list[tuple[int, int, str]]:
    cues = []
    for line in text.splitlines():
        if not line.startswith("Dialogue:"):
            continue
        parts = line[9:].split(",", 9)
        if len(parts) < 10:
            continue
        a, b = _ASS_TIME_RE.match(parts[1].strip()), _ASS_TIME_RE.match(parts[2].strip())
        if not (a and b):
            continue
        t = lambda m: ((int(m[1]) * 3600 + int(m[2]) * 60 + int(m[3])) * 1000 + int(m[4]) * 10)
        body = _ASS_TAG_RE.sub("", parts[9]).replace("\\N", "\n").replace("\\n", "\n").strip()
        if body:
            cues.append((t(a), t(b), body))
    return cues


def _parse_subtitle_cues(text: str) -> list[tuple[int, int, str]]:
    text = text.lstrip("\ufeff")
    if "[Script Info]" in text[:200] or "\nDialogue:" in text:
        cues = _parse_ass_cues(text)
        cues.sort(key=lambda c: c[0])
        return cues
    cues: list[tuple[int, int, str]] = []
    for block in re.split(r"\r?\n\r?\n+", text.strip()):
        lines = block.splitlines()
        timing_idx = next((i for i, ln in enumerate(lines) if "-->" in ln), None)
        if timing_idx is None:
            continue
        m = _CUE_TIME_RE.search(lines[timing_idx])
        if not m:
            continue
        sh, sm, ss, sms, eh, em, es, ems = m.groups()
        start_ms = (int(sh or 0) * 3600 + int(sm) * 60 + int(ss)) * 1000 + int(sms)
        end_ms = (int(eh or 0) * 3600 + int(em) * 60 + int(es)) * 1000 + int(ems)
        cue_text = _TAG_STRIP_RE.sub("", "\n".join(lines[timing_idx + 1:])).strip()
        cue_text = html.unescape(cue_text)
        if cue_text:
            cues.append((start_ms, end_ms, cue_text))
    cues.sort(key=lambda c: c[0])
    return cues


from bisect import bisect_right
from qfluentwidgets import Theme as _Theme

_BAR_QSS = """
QToolButton{color:#fff;background:transparent;border:none;border-radius:6px;padding:0 6px;
font:600 11px 'Segoe UI Variable Text','Segoe UI',sans-serif;}
QToolButton:hover{background:rgba(255,255,255,34);}
QToolButton:pressed{background:rgba(255,255,255,20);}
QToolButton::menu-indicator{image:none;width:0;}
QLabel{color:rgba(255,255,255,205);font:11px 'Segoe UI Variable Text','Segoe UI',sans-serif;background:transparent;}
QSlider{background:transparent;}
QSlider::groove:horizontal{height:4px;background:transparent;}
QSlider::add-page:horizontal{height:4px;border-radius:2px;background:rgba(255,255,255,70);}
QSlider::sub-page:horizontal{height:4px;border-radius:2px;background:#60cdff;}
QSlider::handle:horizontal{width:12px;height:12px;margin:-4px 0;border-radius:6px;background:#fff;}
QSlider::handle:horizontal:hover{width:14px;margin:-5px 0;border-radius:7px;}
"""
_MENU_QSS = """
QMenu{background:#2c2c31;color:#fff;border:1px solid rgba(255,255,255,40);border-radius:8px;padding:4px;
font:12px 'Segoe UI Variable Text','Segoe UI',sans-serif;}
QMenu::item{padding:5px 22px 5px 12px;border-radius:5px;}
QMenu::item:selected{background:rgba(255,255,255,36);}
"""


def _wicon(name: str, fallback=FluentIcon.PLAY) -> QIcon:
    return getattr(FluentIcon, name, fallback).icon(_Theme.DARK)


class _SeekSlider(QSlider):
    def _val(self, x: float) -> int:
        return QStyle.sliderValueFromPosition(self.minimum(), self.maximum(), int(x), max(1, self.width()))

    def mousePressEvent(self, e) -> None:
        if e.button() == Qt.MouseButton.LeftButton and self.maximum() > 0:
            self.setSliderDown(True)
            self.setValue(self._val(e.position().x()))
            self.sliderMoved.emit(self.value())
            e.accept()
        else:
            super().mousePressEvent(e)

    def mouseMoveEvent(self, e) -> None:
        if self.isSliderDown():
            self.setValue(self._val(e.position().x()))
            self.sliderMoved.emit(self.value())
            e.accept()
        elif self.maximum() > 0:
            QToolTip.showText(e.globalPosition().toPoint(), _format_ms(self._val(e.position().x())), self)

    def mouseReleaseEvent(self, e) -> None:
        if self.isSliderDown():
            self.setSliderDown(False)
            e.accept()


_SUB_FONTS = ("Segoe UI", "Arial", "Verdana", "Tahoma", "Trebuchet MS", "Georgia", "Consolas", "Comic Sans MS")
_SUB_COLORS = (("White", "#ffffff"), ("Yellow", "#ffe94d"), ("Cyan", "#6be3ff"), ("Green", "#7dff8a"),
               ("Pink", "#ff9ad5"), ("Orange", "#ffb347"))
_SUB_SIZES = (("Small", 16), ("Medium", 22), ("Large", 28), ("Extra large", 36), ("Huge", 46))
_SUB_OUTLINES = (("None", 0.0), ("Thin", 1.5), ("Medium", 3.0), ("Thick", 4.5))
_SUB_DEFAULTS = {"font": "Segoe UI", "size": 22, "bold": True, "color": "#ffffff", "outline": 0.0,
                 "bg": False, "x": 0.5, "y": 0.96}


def _load_sub_style() -> dict:
    st = dict(_SUB_DEFAULTS)
    try:
        qs = QSettings("AnimePlayer", "Subtitles")
        for k, d in _SUB_DEFAULTS.items():
            v = qs.value(k)
            if v is None:
                continue
            st[k] = (str(v).lower() == "true") if isinstance(d, bool) else type(d)(v)
    except Exception:
        pass
    return st


def _save_sub_style(st: dict) -> None:
    try:
        qs = QSettings("AnimePlayer", "Subtitles")
        for k, v in st.items():
            qs.setValue(k, v)
    except Exception:
        pass


class _SubtitleOverlay(QWidget):
    def __init__(self, player: "AnimePlayerWidget", owner: Optional[QWidget] = None) -> None:
        super().__init__(owner or player, Qt.WindowType.Tool | Qt.WindowType.FramelessWindowHint
                         | Qt.WindowType.WindowDoesNotAcceptFocus | Qt.WindowType.NoDropShadowWindowHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setCursor(Qt.CursorShape.SizeAllCursor)
        self.setToolTip("Drag to move \u00b7 scroll to resize \u00b7 double-click to reset \u00b7 right-click for style")
        self._p = player
        self._lines: list[str] = []
        self._font = QFont()
        self._pad = 8
        self._drag: Optional[tuple] = None
        self._last_key = None

    def set_text(self, text: str) -> None:
        self._lines_src = text
        self.refresh(force=True)

    def _video_rect(self) -> QRect:
        v = self._p._vid()
        return QRect(v.mapToGlobal(QPoint(0, 0)), v.size())

    def refresh(self, force: bool = False) -> None:
        p = self._p
        text = getattr(self, "_lines_src", "")
        v = p._vid()
        if not text or not v.isVisible() or v.visibleRegion().isEmpty():
            self.hide()
            return
        vr = self._video_rect()
        key = (vr.x(), vr.y(), vr.width(), vr.height())
        if not force and key == self._last_key and self.isVisible():
            return
        self._last_key = key
        st = p._sub_style
        scale = max(0.5, vr.width() / 800)
        f = QFont(st["font"])
        f.setPixelSize(max(8, int(st["size"] * scale)))
        f.setBold(bool(st["bold"]))
        self._font = f
        fm = QFontMetrics(f)
        maxw = int(vr.width() * 0.9)
        lines: list[str] = []
        for raw in text.split("\n"):
            cur = ""
            for word in raw.split():
                t = f"{cur} {word}".strip()
                if cur and fm.horizontalAdvance(t) > maxw:
                    lines.append(cur)
                    cur = word
                else:
                    cur = t
            lines.append(cur)
        self._lines = lines
        ol = st["outline"] * scale
        self._pad = int(ol + 8 * scale)
        w = max(fm.horizontalAdvance(l) for l in lines) + 2 * self._pad
        h = len(lines) * fm.lineSpacing() + 2 * self._pad
        x = int(vr.x() + st["x"] * vr.width() - w / 2)
        y = int(vr.y() + st["y"] * vr.height() - h)
        x = max(vr.left(), min(x, vr.right() - w))
        y = max(vr.top(), min(y, vr.bottom() - h))
        self.setGeometry(x, y, w, h)
        if not self.isVisible():
            self.show()
        self.update()

    def paintEvent(self, _e) -> None:
        if not self._lines:
            return
        st = self._p._sub_style
        pt = QPainter(self)
        pt.setRenderHint(QPainter.RenderHint.Antialiasing)
        scale = max(0.5, self._video_rect().width() / 800)
        if st["bg"]:
            pt.setPen(Qt.PenStyle.NoPen)
            pt.setBrush(QColor(0, 0, 0, 150))
            pt.drawRoundedRect(self.rect(), 8, 8)
        fm = QFontMetrics(self._font)
        path = QPainterPath()
        y = self._pad + fm.ascent()
        for line in self._lines:
            path.addText((self.width() - fm.horizontalAdvance(line)) / 2, y, self._font, line)
            y += fm.lineSpacing()
        if st["outline"] > 0:
            pen = QPen(QColor(0, 0, 0, 235), st["outline"] * scale * 2)
            pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
            pt.setPen(pen)
            pt.setBrush(Qt.BrushStyle.NoBrush)
            pt.drawPath(path)
        pt.setPen(Qt.PenStyle.NoPen)
        pt.setBrush(QColor(st["color"]))
        pt.drawPath(path)

    def mousePressEvent(self, e) -> None:
        if e.button() == Qt.MouseButton.LeftButton:
            self._drag = (e.globalPosition().toPoint(), self.pos())
            e.accept()

    def mouseMoveEvent(self, e) -> None:
        if self._drag is None:
            return
        vr = self._video_rect()
        d = e.globalPosition().toPoint() - self._drag[0]
        n = self._drag[1] + d
        self.move(max(vr.left(), min(n.x(), vr.right() - self.width())),
                  max(vr.top(), min(n.y(), vr.bottom() - self.height())))

    def mouseReleaseEvent(self, e) -> None:
        if self._drag is None:
            return
        self._drag = None
        vr = self._video_rect()
        st = self._p._sub_style
        st["x"] = round((self.x() + self.width() / 2 - vr.x()) / max(1, vr.width()), 4)
        st["y"] = round((self.y() + self.height() - vr.y()) / max(1, vr.height()), 4)
        _save_sub_style(st)
        self._last_key = None

    def mouseDoubleClickEvent(self, e) -> None:
        self._p._set_sub_style(x=_SUB_DEFAULTS["x"], y=_SUB_DEFAULTS["y"])

    def wheelEvent(self, e) -> None:
        st = self._p._sub_style
        self._p._set_sub_style(size=max(10, min(80, st["size"] + (2 if e.angleDelta().y() > 0 else -2))))
        e.accept()

    def contextMenuEvent(self, e) -> None:
        self._p._style_menu.exec(e.globalPos())


class _Bar(QWidget):
    floating = False

    def paintEvent(self, _e) -> None:
        p = QPainter(self)
        if self.floating:
            p.setRenderHint(QPainter.RenderHint.Antialiasing)
            p.setBrush(QColor(28, 28, 34, 228))
            p.setPen(QColor(255, 255, 255, 40))
            p.drawRoundedRect(QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5), 12, 12)
        else:
            p.fillRect(self.rect(), QColor(32, 32, 38))
            p.fillRect(0, 0, self.width(), 1, QColor(255, 255, 255, 28))


class _Spinner(QWidget):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setFixedSize(20, 20)
        self._angle = 0
        self._active = False
        self._t = QTimer(self)
        self._t.setInterval(50)
        self._t.timeout.connect(self._tick)

    def set_active(self, on: bool) -> None:
        if on == self._active:
            return
        self._active = on
        self._t.start() if (on and self.isVisible()) else self._t.stop()
        self.update()

    def showEvent(self, e) -> None:
        if self._active:
            self._t.start()
        super().showEvent(e)

    def hideEvent(self, e) -> None:
        self._t.stop()
        super().hideEvent(e)

    def _tick(self) -> None:
        self._angle = (self._angle + 36) % 360
        self.update()

    def paintEvent(self, _e) -> None:
        if not self._active:
            return
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        pen = QPen(QColor(96, 205, 255), 2.4)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        p.setPen(pen)
        p.drawArc(QRectF(3, 3, 14, 14), -self._angle * 16, -110 * 16)


class _LoadingOverlay(QWidget):
    _SIZE = 68
    _DELAY_MS = 250

    def __init__(self, player: "AnimePlayerWidget", owner: Optional[QWidget] = None) -> None:
        super().__init__(owner or player, Qt.WindowType.Tool | Qt.WindowType.FramelessWindowHint
                         | Qt.WindowType.WindowDoesNotAcceptFocus | Qt.WindowType.NoDropShadowWindowHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.setFixedSize(self._SIZE, self._SIZE)
        self._p = player
        self._active = False
        self._clock = QElapsedTimer()
        self._timer = QTimer(self)
        self._timer.setInterval(16)
        self._timer.timeout.connect(self._tick)

    def set_active(self, on: bool) -> None:
        if on == self._active:
            return
        self._active = on
        if on:
            self._clock.start()
            self._timer.start()
        else:
            self._timer.stop()
            self.hide()

    def _tick(self) -> None:
        v = self._p._vid()
        if self._clock.elapsed() < self._DELAY_MS or not v.isVisible() or v.visibleRegion().isEmpty():
            if self.isVisible():
                self.hide()
            return
        tl = v.mapToGlobal(QPoint(0, 0))
        pos = QPoint(tl.x() + (v.width() - self.width()) // 2, tl.y() + (v.height() - self.height()) // 2)
        if pos != self.pos():
            self.move(pos)
        if not self.isVisible():
            self.show()
            self.raise_()
        self.update()

    def paintEvent(self, _e) -> None:
        if not self._active:
            return
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(0, 0, 0, 120))
        p.drawEllipse(QRectF(self.rect()))
        ring = QRectF(self.rect()).adjusted(14, 14, -14, -14)
        pen = QPen(QColor(255, 255, 255, 50), 4)
        p.setPen(pen)
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawEllipse(ring)
        pen = QPen(QColor(255, 255, 255, 240), 4)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        p.setPen(pen)
        angle = (self._clock.elapsed() % 900) / 900.0 * 360.0
        p.drawArc(ring, int(-angle * 16), -100 * 16)


def _is_local_stream_url(url: str) -> bool:
    text = (url or "").strip()
    if not text:
        return True
    if text.lower().startswith("file:"):
        return True
    if text.startswith(("\\\\", "//", "/")):
        return True
    head = text.split(":", 1)[0]
    return len(head) == 1 and head.isalpha()


class _SkipBubble(QWidget):
    _QSS = ("QPushButton{background:rgba(20,20,24,225);color:white;border:2px solid rgba(255,255,255,210);"
            "border-radius:6px;padding:9px 22px;font:600 14px 'Segoe UI';}"
            "QPushButton:hover{background:white;color:black;}")

    def __init__(self, player: "AnimePlayerWidget", owner: Optional[QWidget] = None) -> None:
        super().__init__(owner or player, Qt.WindowType.Tool | Qt.WindowType.FramelessWindowHint
                         | Qt.WindowType.WindowDoesNotAcceptFocus | Qt.WindowType.NoDropShadowWindowHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self._p = player
        self._want = False
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(10)
        self._skip = _QPushButton(self)
        self._next = _QPushButton(self)
        for b in (self._skip, self._next):
            b.setStyleSheet(self._QSS)
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            b.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            b.hide()
            lay.addWidget(b)
        self._skip.clicked.connect(lambda: player._do_skip())
        self._next.clicked.connect(lambda: player.next_requested.emit())

    def set_state(self, skip: Optional[str], nxt: Optional[str]) -> None:
        for btn, text in ((self._skip, skip), (self._next, nxt)):
            if text:
                if btn.text() != text:
                    btn.setText(text)
                btn.show()
            else:
                btn.hide()
        self._want = bool(skip or nxt)
        self.reposition()

    def reposition(self) -> None:
        v = self._p._vid()
        if not self._want or not v.isVisible() or v.visibleRegion().isEmpty():
            self.hide()
            return
        self.adjustSize()
        tl = v.mapToGlobal(QPoint(0, 0))
        lift = 96 if self._p._is_fullscreen else 18
        self.move(tl.x() + v.width() - self.width() - 24, tl.y() + v.height() - self.height() - lift)
        if not self.isVisible():
            self.show()
        self.raise_()


class _PipWindow(QWidget):

    def __init__(self, player: "AnimePlayerWidget") -> None:
        super().__init__(None, Qt.WindowType.Window | Qt.WindowType.FramelessWindowHint)
        self.setWindowTitle("PiraChest PiP")
        self.setStyleSheet("background:black;")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        self._layout = lay
        self._player = player
        self._save_timer = QTimer(self)
        self._save_timer.setSingleShot(True)
        self._save_timer.setInterval(350)
        self._save_timer.timeout.connect(self.save_geometry)
        self.setMinimumSize(240, 135 + AnimePlayerWidget._BAR_H)

    def set_always_on_top(self, on: bool) -> None:
        if bool(self.windowFlags() & Qt.WindowType.WindowStaysOnTopHint) == bool(on):
            return
        visible = self.isVisible()
        self.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, on)
        if visible:
            self.show()

    def restore_geometry(self) -> None:
        qs = QSettings("PiraChest", "PiP")
        rect = qs.value("geometry")
        if not isinstance(rect, QRect) or not rect.isValid():
            screen = self.screen() or QGuiApplication.primaryScreen()
            avail = screen.availableGeometry() if screen is not None else QRect(0, 0, 1280, 720)
            w = max(320, min(480, avail.width() - 48))
            h = max(200, int(w * 9 / 16) + AnimePlayerWidget._BAR_H)
            rect = QRect(avail.right() - w - 24, avail.bottom() - h - 24, w, h)
        self.setGeometry(_clamp_rect(rect, 240, 135 + AnimePlayerWidget._BAR_H))

    def save_geometry(self) -> None:
        if not self.isVisible():
            return
        try:
            rect = self.normalGeometry()
            if rect.isValid():
                QSettings("PiraChest", "PiP").setValue("geometry", rect)
        except Exception:
            logger.debug("Could not save PiP geometry", exc_info=True)

    def moveEvent(self, event) -> None:
        self._save_timer.start()
        super().moveEvent(event)

    def resizeEvent(self, event) -> None:
        self._save_timer.start()
        super().resizeEvent(event)

    def closeEvent(self, event) -> None:
        if self._player._pip_active:
            self._player.exit_pip()
            event.ignore()
            return
        self.save_geometry()
        super().closeEvent(event)


def _clamp_rect(rect: QRect, min_w: int, min_h: int) -> QRect:
    screens = QGuiApplication.screens()
    if not screens:
        return rect
    best, best_area = None, 0
    for screen in screens:
        area = screen.availableGeometry().intersected(rect)
        size = area.width() * area.height()
        if size > best_area:
            best, best_area = screen, size
    screen = best or QGuiApplication.screenAt(QCursor.pos()) or QGuiApplication.primaryScreen()
    avail = screen.availableGeometry().adjusted(8, 8, -8, -8)
    w = max(min_w, min(rect.width(), avail.width()))
    h = max(min_h, min(rect.height(), avail.height()))
    x = min(max(rect.x(), avail.left()), avail.right() - w + 1)
    y = min(max(rect.y(), avail.top()), avail.bottom() - h + 1)
    return QRect(x, y, w, h)


class AnimePlayerWidget(QWidget):
    closed = Signal()
    close_requested = Signal()
    alt_stream_requested = Signal(dict)
    playback_error = Signal(str)
    stream_failed = Signal(str)
    ended = Signal()
    next_requested = Signal()
    _qualities_ready = Signal(list)
    _subtitle_cues_ready = Signal(list)
    _manifest_subs_ready = Signal(list)
    MAX_WIDTH = 640
    _MAX_LIVE_RECONNECTS = 5
    _RECONNECT_STABLE_SECS = 20
    _BAR_H = 54
    _VOD_MAX_RETRIES = 3
    _SKIP_INTRO_MS = 85_000

    _pirachest_no_kb_ring = True

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setMouseTracking(True)
        sp = QSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Preferred)
        sp.setHeightForWidth(True)
        self.setSizePolicy(sp)
        self.setMaximumWidth(self.MAX_WIDTH)
        self.setMinimumSize(240, 135 + self._BAR_H)

        self._title = ""
        self._seeking = False
        self._muted = False
        self._pre_mute_volume = self._load_volume()
        self._is_fullscreen = False
        self._fs_win = self._fs_bar_win = None
        self._pip_active = False
        self._pip_win = None
        self._pip_layout = None
        self._pip_index = -1
        self._pip_align = Qt.AlignmentFlag(0)
        self._pip_placeholder = None
        self._pip_drag = None
        self._pip_dragging = False
        self._suppress_hide = False
        self._seek_target = 0
        self._seek_until = 0.0
        self._auto_paused = False
        self._is_live = False
        self._live_referer = None
        self._live_headers = None
        self._live_reconnect_attempts = 0
        self._reconnect_at = 0.0
        self._master_url = None
        self._master_final_url = None
        self._pending_seek = None
        self._external_subs: list[dict] = []
        self._cues: list[tuple[int, int, str]] = []
        self._cue_starts: list[int] = []
        self._cue_text = ""
        self._intended_embedded_track = -1
        self._speed = 1.0
        self._quality_opts: list[tuple[str, Optional[str]]] = []
        self._quality_cur = 0
        self._sub_entries: list[tuple[str, int, str]] = [("off", -1, "")]
        self._sub_cur = 0
        self._last_pos_sec = -1
        self._reconnect_pending = False
        self._variant_opts: list = []
        self._alt_opts: list = []
        self._quality_label = ""
        self._sub_gen = 0
        self._sub_style = _load_sub_style()
        self._sub_pref: Optional[str] = None
        self._stream_gen = 0
        self._vod_retries = 0
        self._failed_sent = False
        self._sub_delay_ms = 0
        self._click_timer = QTimer(self)
        self._click_timer.setSingleShot(True)
        self._click_timer.timeout.connect(self._toggle_play_pause)
        self._stall_timer = QTimer(self)
        self._stall_timer.setSingleShot(True)
        self._stall_timer.setInterval(20_000)
        self._stall_timer.timeout.connect(self._on_stall_timeout)

        self._qualities_ready.connect(self._on_qualities_ready)
        self._subtitle_cues_ready.connect(self._on_subtitle_cues_ready)
        self._manifest_subs_ready.connect(self._on_manifest_subs_ready)

        self._player = QMediaPlayer(self)
        self._audio_output = QAudioOutput(self)
        self._audio_output.setVolume(self._pre_mute_volume / 100)
        self._player.setAudioOutput(self._audio_output)
        self._video_widget = QVideoWidget(self)
        self._video_widget.setStyleSheet("background:black;")
        self._video_widget.setMouseTracking(True)
        self._player.setVideoOutput(self._video_widget)

        self._player.playbackStateChanged.connect(self._on_state_changed)
        self._player.positionChanged.connect(self._on_position_changed)
        self._player.durationChanged.connect(self._on_duration_changed)
        self._player.errorOccurred.connect(self._on_error)
        self._player.mediaStatusChanged.connect(self._on_media_status_changed)
        if hasattr(self._player, "tracksChanged"):
            self._player.tracksChanged.connect(self._refresh_subtitle_tracks)
        if hasattr(self._player, "activeSubtitleTrackChanged"):
            self._player.activeSubtitleTrackChanged.connect(self._on_active_subtitle_track_changed)

        self._status_lbl = QLabel("", self)
        self._status_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._refresh_status_style()
        register_theme_refresh(self, self._refresh_status_style)
        self._status_lbl.hide()

        bar = self._bar = _Bar(self)
        bar.setStyleSheet(_BAR_QSS)
        bar.setMouseTracking(True)
        bar.setFixedHeight(self._BAR_H)
        v = QVBoxLayout(bar)
        v.setContentsMargins(10, 5, 10, 4)
        v.setSpacing(0)

        seek_row = QHBoxLayout()
        seek_row.setSpacing(8)
        self._position_lbl = QLabel("0:00", bar)
        self._position_lbl.setMinimumWidth(34)
        self._seek_slider = _SeekSlider(Qt.Orientation.Horizontal, bar)
        self._seek_slider.setRange(0, 0)
        self._seek_slider.setFixedHeight(16)
        self._seek_slider.setMouseTracking(True)
        self._seek_slider.sliderMoved.connect(lambda x: self._position_lbl.setText(_format_ms(x)))
        self._seek_slider.sliderPressed.connect(lambda: setattr(self, "_seeking", True))
        self._seek_slider.sliderReleased.connect(self._on_seek_released)
        self._duration_lbl = QLabel("0:00", bar)
        self._duration_lbl.setMinimumWidth(34)
        self._duration_lbl.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        seek_row.addWidget(self._position_lbl)
        seek_row.addWidget(self._seek_slider, 1)
        seek_row.addWidget(self._duration_lbl)
        v.addLayout(seek_row)

        row = QHBoxLayout()
        row.setSpacing(2)
        v.addLayout(row)

        def tb(icon=None, tip="", slot=None, size=26, text=""):
            b = QToolButton(bar)
            b.setAutoRaise(True)
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            b.setFixedHeight(26)
            if icon is not None:
                b.setIcon(icon)
                b.setIconSize(QSize(16, 16))
                b.setFixedWidth(size)
            if text:
                b.setText(text)
            b.setToolTip(tip)
            if slot:
                b.clicked.connect(slot)
            return b

        self._skip_back_btn = tb(_wicon("SKIP_BACK", FluentIcon.LEFT_ARROW),
                                 tr("anime.rewind_30", default="Rewind 30s"), lambda: self._seek_relative(-30_000))
        self._play_btn = tb(_wicon("PAUSE", FluentIcon.PAUSE), "", self._toggle_play_pause, 30)
        self._skip_fwd_btn = tb(_wicon("SKIP_FORWARD", FluentIcon.RIGHT_ARROW),
                                tr("anime.forward_30", default="Forward 30s"), lambda: self._seek_relative(30_000))
        self._mute_btn = tb(_wicon("VOLUME", FluentIcon.VOLUME), tr("anime.mute", default="Mute"), self._toggle_mute)
        self._volume_slider = QSlider(Qt.Orientation.Horizontal, bar)
        self._volume_slider.setRange(0, 100)
        self._volume_slider.setValue(self._pre_mute_volume)
        self._volume_slider.setFixedSize(60, 16)
        self._volume_slider.setToolTip(tr("anime.volume", default="Volume"))
        self._volume_slider.valueChanged.connect(self._on_volume_changed)

        self._next_btn = tb(None, tr("anime.next_episode", default="Next episode"),
                            lambda: self.next_requested.emit(), text=tr("anime.next", default="Next") + " \u203a")
        self._next_btn.hide()
        self._has_next = False
        self._near_end = False
        self._ended_prompt = False
        self._skip_segs: dict = {"intro": [], "credits": []}
        self._skip_kind: Optional[str] = None
        self._skip_done: set = set()
        self._speed_btn = tb(None, tr("anime.playback_speed", default="Playback speed"), text="1x")
        self._quality_btn = tb(None, tr("anime.quality", default="Quality"), text="Auto")
        self._sub_btn = tb(None, tr("anime.subtitles", default="Subtitles"), text="CC")
        self._pip_btn = tb(_wicon("PICTURE_IN_PICTURE", FluentIcon.VIEW),
                           tr("anime.pip_enter", default="Picture in picture"), self.toggle_pip)
        self._fullscreen_btn = tb(_wicon("FULL_SCREEN", FluentIcon.VIEW), tr("anime.fullscreen", default="Fullscreen"),
                                  self.toggle_fullscreen)
        self._close_btn = tb(_wicon("CLOSE", FluentIcon.CLOSE), tr("anime.close_player", default="Close player"),
                             lambda: self.close_requested.emit())
        self._close_btn.hide()
        for b in (self._speed_btn, self._quality_btn, self._sub_btn):
            b.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
            m = QMenu(b)
            m.setStyleSheet(_MENU_QSS)
            b.setMenu(m)
        self._fill_menu(self._speed_btn, [(f"{s:g}x", s) for s in _SPEEDS], _SPEEDS.index(1.0), self._set_speed)

        for w in (self._skip_back_btn, self._play_btn, self._skip_fwd_btn, self._mute_btn, self._volume_slider):
            row.addWidget(w)
        row.addStretch(1)
        for w in (self._next_btn, self._speed_btn, self._quality_btn, self._sub_btn, self._pip_btn,
                  self._fullscreen_btn, self._close_btn):
            row.addWidget(w)
        self._quality_btn.hide()
        self._sub_btn.hide()

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        outer.addWidget(self._video_widget, 1)
        outer.addWidget(self._status_lbl)
        outer.addWidget(bar)
        self._busy_on = False
        self._busy_pos = 0
        self._outer = outer
        self._video_widget.installEventFilter(self)
        bar.installEventFilter(self)

        self._sub_ov = _SubtitleOverlay(self)
        self._bubble = _SkipBubble(self)
        self._loader = _LoadingOverlay(self)
        self._ov_timer = QTimer(self)
        self._ov_timer.setInterval(120)
        self._ov_timer.timeout.connect(self._sub_ov.refresh)
        self._style_menu = self._build_style_menu()

        self._hide_timer = QTimer(self)
        self._hide_timer.setInterval(2500)
        self._hide_timer.setSingleShot(True)
        self._hide_timer.timeout.connect(self._auto_hide_controls)


    @staticmethod
    def _load_volume() -> int:
        try:
            return max(1, min(100, int(QSettings("AnimePlayer", "Player").value("volume", 80))))
        except Exception:
            return 80

    def _osd(self, text: str) -> None:
        v = self._video_widget
        QToolTip.showText(v.mapToGlobal(QPoint(v.width() // 2 - 40, v.height() // 2)), text, v, QRect(), 900)

    def set_has_next(self, has: bool) -> None:
        self._has_next = bool(has)
        self._ended_prompt = False
        self._next_btn.setText(tr("anime.next", default="Next") + " \u203a")
        self._next_btn.setVisible(self._has_next and not self._is_live)
        self._refresh_bubble()

    def prompt_next(self) -> None:
        self._ended_prompt = True
        self._refresh_bubble()

    def set_skip_segments(self, intro=None, credits=None) -> None:
        self._skip_segs = {"intro": [list(s) for s in intro or []], "credits": [list(s) for s in credits or []]}
        self._skip_done.clear()
        self._near_end = self._ended_prompt = False
        self._set_skip_kind(None)

    def _set_skip_kind(self, kind: Optional[str]) -> None:
        self._skip_kind = kind
        self._refresh_bubble()

    def _refresh_bubble(self) -> None:
        kind = self._skip_kind
        skip = (None if kind is None else tr("tv.skip_intro", default="Skip Intro") if kind == "intro"
                else tr("tv.skip_credits", default="Skip Credits"))
        nxt = None
        if self._has_next and not self._is_live and (kind == "credits" or self._near_end or self._ended_prompt):
            nxt = tr("tv.next_episode_prompt", default="Next Episode") + " \u203a"
        self._bubble.set_state(skip, nxt)

    def _refresh_status_style(self) -> None:
        pal = palette()
        with suppress(RuntimeError):
            self._status_lbl.setStyleSheet(
                f"color:{pal['state_error']};background:{pal['card_bg']};"
                "padding:4px;border-radius:6px;font:12px 'Segoe UI';")

    def _skip_seg_to(self, end) -> None:
        dur = self._player.duration()
        if end is None or (dur > 0 and end >= dur - 1500):
            if dur <= 0:
                return
            end = dur - 250
        self._seek_to(max(0, int(end)))

    def _update_skip(self, ms: int) -> None:
        if self._is_live:
            return
        dur = self._player.duration()
        self._near_end = dur > 0 and 0 < dur - ms <= 35_000
        found = None
        for kind in ("intro", "credits"):
            for start, end in self._skip_segs[kind]:
                stop = dur if end is None else end
                if stop <= 0 or not (start <= ms < stop - 500):
                    continue
                if (getattr(settings, "tv_skip_intro" if kind == "intro" else "tv_skip_credits", False)
                        and (kind, start) not in self._skip_done):
                    self._skip_done.add((kind, start))
                    self._skip_seg_to(end)
                    return
                found = kind
                break
            if found:
                break
        self._set_skip_kind(found)

    def _do_skip(self) -> None:
        kind = self._skip_kind
        if not kind:
            return
        ms, dur = self._player.position(), self._player.duration()
        for start, end in self._skip_segs[kind]:
            if start <= ms < (dur if end is None else end):
                self._skip_seg_to(end)
                break
        self._set_skip_kind(None)


    def hasHeightForWidth(self) -> bool:
        return True

    def heightForWidth(self, width: int) -> int:
        return max(1, int(width * _ASPECT_H / _ASPECT_W)) + self._BAR_H

    def sizeHint(self):
        pw = self.parentWidget().width() if self.parentWidget() else self.MAX_WIDTH
        w = max(240, min(self.MAX_WIDTH, pw))
        return QSize(w, self.heightForWidth(w))

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        if self._is_fullscreen or self._pip_active:
            return
        h = self.heightForWidth(self.width())
        if self.height() != h:
            self.setFixedHeight(h)

    def _set_status(self, text: str) -> None:
        if text:
            self._busy(False)
        self._status_lbl.setText(text)
        self._status_lbl.setVisible(bool(text))

    def _busy(self, on: bool) -> None:
        self._busy_on = on
        if on:
            self._busy_pos = self._player.position()
            if self._is_fullscreen:
                self._reveal_controls()
        self._loader.set_active(on)

    def play_stream(self, url: str, referer: Optional[str] = None, headers: Optional[dict] = None,
                    title: str = "", subtitles: Optional[list[dict]] = None, start_ms: int = 0,
                    alt_streams: Optional[list[tuple[str, dict]]] = None, quality_label: str = "",
                    _reconnect: bool = False) -> None:
        if _is_local_stream_url(url):
            logger.warning("Refused a local stream url: %r", (url or "")[:120])
            self._set_status(tr("anime.no_stream_found", default="No playable stream was returned."))
            return
        self._title = title or ""
        self._live_referer = referer
        self._live_headers = headers
        if not _reconnect:
            self._live_reconnect_attempts = 0
            self._vod_retries = 0
            self._failed_sent = False
            self._sub_delay_ms = 0
        self._stream_gen += 1
        self._set_status("")
        self._busy(True)
        self._sub_gen += 1
        self._pending_seek = None
        self._external_subs = subtitles or []
        self._clear_cues()
        self._alt_opts = alt_streams or []
        self._quality_label = quality_label
        self._reset_quality()
        self._populate_subtitle_options()

        final_url = url
        relay = None
        is_hls = url.split("?")[0].lower().endswith(".m3u8")
        if is_hls or self._is_live or headers or referer:
            relay = get_relay()
            final_url = relay.set_stream(url, referer=referer, extra_headers=headers, live=self._is_live)
        self._master_url = url
        self._master_final_url = final_url

        self._player.stop()
        self._player.setSource(QUrl(final_url))
        self._player.setPlaybackRate(self._speed)
        self._intended_embedded_track = -1
        self._set_active_sub(-1)
        if start_ms > 1000 and not self._is_live:
            self._pending_seek = (start_ms, True)
        self._player.play()
        self._reveal_controls()
        if is_hls:
            self._load_qualities_async(url, referer, headers, relay, self._stream_gen)

    def stop(self) -> None:
        if self._pip_active:
            self.exit_pip(restore=False)
        self._player.stop()
        self._player.setSource(QUrl())
        self._clear_cues()
        self.set_skip_segments()

    def toggle_pip(self) -> None:
        self.exit_pip() if self._pip_active else self.enter_pip()

    def enter_pip(self) -> None:
        if self._pip_active or self._is_fullscreen or self._player.source().isEmpty():
            return
        parent = self.parentWidget()
        layout = parent.layout() if parent is not None else None
        if layout is None:
            return
        index = layout.indexOf(self)
        if index < 0:
            return
        item = layout.itemAt(index)
        align = item.alignment() if item is not None else Qt.AlignmentFlag(0)
        placeholder = QWidget(parent)
        placeholder.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        placeholder.setFixedHeight(max(120, self.height()))
        layout.removeWidget(self)
        layout.insertWidget(index, placeholder, 0, align)

        win = self._pip_win
        if win is None or not _qt_is_valid(win):
            win = self._pip_win = _PipWindow(self)
        win.set_always_on_top(bool(getattr(settings, "tv_pip_always_on_top", True)))

        self._pip_layout, self._pip_index = layout, index
        self._pip_align, self._pip_placeholder = align, placeholder
        self._pip_active = True
        self._pip_drag = None
        self._pip_dragging = False
        self._suppress_hide = True
        try:
            self.setMinimumSize(240, 135 + self._BAR_H)
            self.setMaximumWidth(_QWIDGETSIZE_MAX)
            self.setMinimumHeight(135 + self._BAR_H)
            self.setMaximumHeight(_QWIDGETSIZE_MAX)
            self.setParent(win)
            win.layout().addWidget(self)
            self.show()
            win.restore_geometry()
            win.show()
            win.raise_()
            win.activateWindow()
            self._swap_overlay(win)
            self.setFocus()
        finally:
            self._suppress_hide = False
        self._update_pip_button()

    def exit_pip(self, restore: bool = True) -> None:
        if not self._pip_active:
            return
        self._pip_active = False
        self._pip_drag = None
        self._pip_dragging = False
        self._suppress_hide = True
        win = self._pip_win
        layout = self._pip_layout
        try:
            if win is not None and _qt_is_valid(win):
                win.save_geometry()
                win.layout().removeWidget(self)
                win.hide()
            if layout is not None:
                target = layout.parentWidget()
                self.setParent(target)
                placeholder = self._pip_placeholder
                if placeholder is not None and _qt_is_valid(placeholder):
                    layout.removeWidget(placeholder)
                    placeholder.setParent(None)
                    placeholder.deleteLater()
                self._pip_placeholder = None
                self.setMaximumWidth(self.MAX_WIDTH)
                self.setMinimumSize(240, 135 + self._BAR_H)
                self.setMaximumHeight(_QWIDGETSIZE_MAX)
                width = max(240, min(self.width(), self.MAX_WIDTH))
                layout.insertWidget(self._pip_index, self, 0, self._pip_align)
                self.resize(width, self.heightForWidth(width))
                self.setFixedHeight(self.heightForWidth(width))
            self._swap_overlay(self)
            if restore:
                self.show()
        except (RuntimeError, TypeError):
            logger.debug("PiP could not be restored cleanly", exc_info=True)
        finally:
            self._pip_layout = None
            self._pip_index = -1
            self._pip_placeholder = None
            self._suppress_hide = False
        self._update_pip_button()
        if restore:
            self.setFocus()

    def _update_pip_button(self) -> None:
        active = self._pip_active
        self._pip_btn.setIcon(_wicon("BACK_TO_WINDOW" if active else "PICTURE_IN_PICTURE",
                                     FluentIcon.BACK_TO_WINDOW if active else FluentIcon.VIEW))
        self._pip_btn.setToolTip(tr("anime.pip_exit", default="Back to player")
                                 if active else tr("anime.pip_enter", default="Picture in picture"))

    def set_closable(self, on: bool) -> None:
        self._close_btn.setVisible(on)

    def release(self) -> None:
        if self._pip_active:
            self.exit_pip(restore=False)
        if self._is_fullscreen:
            self.exit_fullscreen()
        self._busy(False)
        self._hide_timer.stop()
        self._stall_timer.stop()
        self._stream_gen += 1
        self.stop()
        self._set_status("")
        try:
            get_relay().stop()
        except Exception:
            logger.debug("Could not release the stream relay", exc_info=True)
        gc.collect()

    def title(self) -> str:
        return self._title

    def position_ms(self) -> int:
        return self._player.position()

    def has_media(self) -> bool:
        return not self._player.source().isEmpty() and self._player.playbackState() != QMediaPlayer.PlaybackState.StoppedState

    def set_live_mode(self, live: bool) -> None:
        self._is_live = live
        for w in (self._seek_slider, self._position_lbl, self._duration_lbl,
                  self._skip_back_btn, self._skip_fwd_btn, self._speed_btn):
            w.setVisible(not live)
        if live:
            self._set_speed(1.0)

    def shutdown(self) -> None:
        self._hide_timer.stop()
        self._stall_timer.stop()
        if self._pip_active:
            self.exit_pip(restore=False)
        pip_win, self._pip_win = self._pip_win, None
        if pip_win is not None:
            try:
                pip_win.close()
                pip_win.deleteLater()
            except (RuntimeError, TypeError):
                pass
        if self._fs_win is not None:
            self._fs_bar_win.close()
            self._fs_win.close()
        self._player.stop()
        self._player.setSource(QUrl())
        try:
            get_relay().stop()
        except Exception:
            pass


    def _playing(self) -> bool:
        return self._player.playbackState() == QMediaPlayer.PlaybackState.PlayingState

    def _schedule_reconnect(self, delay_ms: int) -> None:
        if self._reconnect_pending or not self._is_live:
            return
        self._reconnect_pending = True
        QTimer.singleShot(delay_ms + 500 * self._live_reconnect_attempts, self._reconnect_live)

    def _on_stall_timeout(self) -> None:
        if not self._master_url or self._player.playbackState() == QMediaPlayer.PlaybackState.StoppedState:
            return
        if self._is_live:
            self._schedule_reconnect(0)
            return
        if self._vod_retries >= self._VOD_MAX_RETRIES:
            self._fail_vod(tr("anime.stream_stalled", default="Stream stalled"))
            return
        self._vod_retries += 1
        src = self._player.source()
        self._pending_seek = (self._player.position(), True)
        self._player.setSource(src)

    def _fail_vod(self, msg: str) -> None:
        self._stall_timer.stop()
        self._player.stop()
        self._set_status(msg)
        if not self._failed_sent:
            self._failed_sent = True
            self.stream_failed.emit(msg)

    def _reconnect_live(self) -> None:
        self._reconnect_pending = False
        if not self._is_live or not self._master_url:
            return


        if time.monotonic() - self._reconnect_at >= self._RECONNECT_STABLE_SECS:
            self._live_reconnect_attempts = 0
        self._live_reconnect_attempts += 1
        if self._live_reconnect_attempts > self._MAX_LIVE_RECONNECTS:
            self._player.stop()
            self._set_status(tr("anime.stream_unavailable", default="Stream unavailable"))
            return
        self._reconnect_at = time.monotonic()
        self.play_stream(self._master_url, self._live_referer, self._live_headers, self._title,
                         self._external_subs, alt_streams=self._alt_opts, quality_label=self._quality_label, _reconnect=True)

    def _toggle_play_pause(self) -> None:
        if self._playing():
            self._player.pause()
        elif self._is_live and self._master_url:
            self._live_reconnect_attempts = 0
            self._reconnect_live()
        else:
            self._player.play()

    def _seek_to(self, ms: int) -> None:
        dur = self._player.duration()
        ms = max(0, min(ms, dur) if dur else ms)
        self._seek_target = ms
        self._seek_until = time.monotonic() + 1.2
        self._player.setPosition(ms)
        self._seek_slider.setValue(ms)
        self._position_lbl.setText(_format_ms(ms))
        if self._cues:
            self._update_subtitle(ms)

    def _seek_relative(self, delta_ms: int) -> None:
        base = self._seek_target if time.monotonic() < self._seek_until else self._player.position()
        self._seek_to(base + delta_ms)

    def _on_state_changed(self, _state) -> None:
        if self._playing():
            self._play_btn.setIcon(_wicon("PAUSE", FluentIcon.PAUSE))
            self._hide_timer.start()
        else:
            self._play_btn.setIcon(_play_icon(QColor(255, 255, 255)))
            self._hide_timer.stop()
            self._reveal_controls()


        try:
            from src.gui.settings_page import get_sound_manager
            get_sound_manager().set_media_playing('tv', self._playing(), owner=self)
        except Exception:
            logger.debug('Ambiance ducking update failed', exc_info=True)

    def _on_position_changed(self, ms: int) -> None:
        if self._busy_on and ms != self._busy_pos and self._playing():
            self._busy(False)
            self._vod_retries = 0
        if self._cues:
            self._update_subtitle(ms)
        self._update_skip(ms)
        if not self._bar.isVisible() or self._seeking:
            return
        sec = ms // 1000
        if sec != self._last_pos_sec:
            self._last_pos_sec = sec
            self._position_lbl.setText(_format_ms(ms))
        self._seek_slider.setValue(ms)

    def _on_duration_changed(self, ms: int) -> None:
        self._seek_slider.setRange(0, max(0, ms))
        self._duration_lbl.setText(_format_ms(ms))

    def _on_seek_released(self) -> None:
        self._seeking = False
        self._seek_to(self._seek_slider.value())

    def _on_media_status_changed(self, status) -> None:
        S = QMediaPlayer.MediaStatus
        if self._is_live and status in (S.StalledMedia, S.EndOfMedia):
            self._schedule_reconnect(1000)
            return
        if status == S.EndOfMedia:
            self._stall_timer.stop()
            self.ended.emit()
            return
        if status in (S.LoadingMedia, S.StalledMedia, S.BufferingMedia):
            self._busy(True)
            if not self._stall_timer.isActive():
                self._stall_timer.start()
        elif status in (S.LoadedMedia, S.BufferedMedia):
            self._busy(False)
            self._stall_timer.stop()
        if status == S.LoadedMedia:
            self._refresh_subtitle_tracks()
            if self._pending_seek is not None:
                pos, was_playing = self._pending_seek
                self._pending_seek = None
                self._player.setPosition(pos)
                self._player.play() if was_playing else self._player.pause()

    def _on_error(self, _error, msg: str) -> None:
        logger.warning("Player error: %s", msg)
        self._set_status(msg or tr("anime.playback_error", default="Playback error"))
        self.playback_error.emit(str(msg or ""))
        self._schedule_reconnect(2000)
        if not self._is_live and not self._failed_sent:
            self._failed_sent = True
            self._stall_timer.stop()
            self.stream_failed.emit(str(msg or ""))


    def _on_volume_changed(self, value: int) -> None:
        self._audio_output.setVolume(value / 100)
        self._muted = value == 0
        self._mute_btn.setIcon(_wicon("MUTE", FluentIcon.MUTE) if self._muted else _wicon("VOLUME", FluentIcon.VOLUME))
        if value > 0:
            self._pre_mute_volume = value
            try:
                QSettings("AnimePlayer", "Player").setValue("volume", value)
            except Exception:
                pass

    def _toggle_mute(self) -> None:
        if self._muted:
            self._volume_slider.setValue(self._pre_mute_volume or 80)
        else:
            self._pre_mute_volume = self._volume_slider.value() or self._pre_mute_volume
            self._volume_slider.setValue(0)

    def _set_speed(self, speed: float) -> None:
        self._speed = speed
        self._player.setPlaybackRate(speed)
        self._speed_btn.setText(f"{speed:g}x")
        self._fill_menu(self._speed_btn, [(f"{s:g}x", s) for s in _SPEEDS], _SPEEDS.index(speed), self._set_speed)

    def _step_speed(self, direction: int) -> None:
        i = min(range(len(_SPEEDS)), key=lambda k: abs(_SPEEDS[k] - self._speed))
        j = max(0, min(len(_SPEEDS) - 1, i + direction))
        self._set_speed(_SPEEDS[j])
        self._osd(f"{_SPEEDS[j]:g}x")

    def _cycle_subtitles(self) -> None:
        if len(self._sub_entries) < 2:
            return
        idx = (self._sub_cur + 1) % len(self._sub_entries)
        self._on_subtitle_picked(idx)
        k, _x, lbl = self._sub_entries[idx]
        self._osd(tr("anime.subtitles_off", default="Subtitles: Off") if k == "off" else lbl)

    def _fill_menu(self, btn, entries: list, current: int, cb) -> None:
        QTimer.singleShot(0, lambda: self._fill_menu_now(btn, entries, current, cb))

    @staticmethod
    def _fill_menu_now(btn: QToolButton, entries: list, current: int, cb) -> None:
        menu = btn.menu()
        menu.clear()
        grp = QActionGroup(menu)
        for i, (label, data) in enumerate(entries):
            act = menu.addAction(label)
            act.setCheckable(True)
            act.setChecked(i == current)
            grp.addAction(act)
            act.triggered.connect(partial(cb, data))


    def _reset_quality(self) -> None:
        self._variant_opts = []
        self._quality_opts = []
        self._quality_cur = 0
        self._rebuild_quality()

    def _rebuild_quality(self) -> None:
        opts: list = list(self._variant_opts) or ([(self._quality_label, None)] if self._quality_label else [])
        opts += [(lbl, st) for lbl, st in self._alt_opts]
        self._quality_opts = opts
        self._quality_cur = min(self._quality_cur, max(0, len(opts) - 1))
        if len(opts) <= 1:
            self._quality_btn.hide()
            return
        self._quality_btn.setText(opts[self._quality_cur][0])
        self._fill_menu(self._quality_btn, [(l, i) for i, (l, _u) in enumerate(opts)], self._quality_cur, self._on_quality_picked)
        self._quality_btn.show()

    def _on_qualities_ready(self, options: list) -> None:
        if len(options) <= 1:
            logger.info("Stream exposes a single quality (no extra HLS variants): %s", self._master_url)
            return
        self._variant_opts = options
        self._quality_cur = 0
        self._rebuild_quality()

    def _on_quality_picked(self, idx: int) -> None:
        if idx == self._quality_cur or not (0 <= idx < len(self._quality_opts)):
            return
        label, payload = self._quality_opts[idx]
        if isinstance(payload, dict):
            self.alt_stream_requested.emit(payload)
            return
        self._quality_cur = idx
        self._quality_btn.setText(label)
        src = payload if payload is not None else self._master_final_url
        if not src:
            return
        self._pending_seek = (self._player.position(), self._playing())
        self._player.setSource(QUrl(src))

    def _load_qualities_async(self, master_url, referer, headers, relay, gen: int) -> None:
        def _worker() -> None:
            try:
                text = relay.fetch_text(master_url) if relay else _ad_hoc_client.get(master_url, timeout=15).text
            except Exception as exc:
                logger.debug("Quality probe failed for %s: %s", master_url, exc)
                return
            if gen != self._stream_gen:
                return
            variants = _parse_hls_variants(text, master_url)
            if variants:
                opts: list = [(tr("anime.quality_auto", default="Auto"), None)]
                for height, bw, vurl in variants:
                    try:
                        local = relay.register(vurl) if relay else vurl
                    except Exception:
                        local = vurl
                    opts.append((f"{height}p" if height else f"{max(1, bw // 1000)} kbps", local))
                self._qualities_ready.emit(opts)
            resolved: list[dict] = []
            for group in _parse_hls_subtitle_groups(text, master_url):
                vtt = group["url"]
                if vtt.split("?")[0].endswith(".m3u8"):
                    try:
                        st = relay.fetch_text(vtt) if relay else _ad_hoc_client.get(vtt, timeout=15).text
                    except Exception:
                        continue
                    if "WEBVTT" not in st[:32].upper():
                        inner = next((l.strip() for l in st.splitlines() if l.strip() and not l.strip().startswith("#")), None)
                        if not inner:
                            continue
                        vtt = urljoin(vtt, inner)
                if relay is not None:
                    try:
                        vtt = relay.register(vtt)
                    except Exception:
                        pass
                resolved.append({**group, "url": vtt})
            if resolved and gen == self._stream_gen:
                self._manifest_subs_ready.emit(resolved)

        threading.Thread(target=_worker, daemon=True).start()


    def _clear_cues(self) -> None:
        self._cues = []
        self._cue_starts = []
        self._cue_text = ""
        self._show_sub_text("")

    def _show_sub_text(self, text: str) -> None:
        self._sub_ov.set_text(text)
        if text:
            self._ov_timer.start()
        else:
            self._ov_timer.stop()

    def _on_manifest_subs_ready(self, subs: list) -> None:
        if self._external_subs:
            return
        self._external_subs = subs
        self._populate_subtitle_options()

    def _on_subtitle_cues_ready(self, cues: list) -> None:
        cues = sorted(cues, key=lambda c: c[0])
        self._cues = cues
        self._cue_starts = [c[0] for c in cues]
        self._cue_text = ""
        self._show_sub_text("")
        if cues:
            self._update_subtitle(self._player.position())

    def _nudge_sub_delay(self, delta_ms: int) -> None:
        if not self._cues:
            self._osd(tr("anime.sub_delay_na", default="No external subtitles to shift"))
            return
        self._sub_delay_ms += delta_ms
        self._cue_text = ""
        self._update_subtitle(self._player.position())
        self._osd(tr("anime.sub_delay", default="Subtitle delay: {ms} ms").format(ms=f"{self._sub_delay_ms:+d}"))

    def _update_subtitle(self, ms: int) -> None:
        ms -= self._sub_delay_ms
        i = bisect_right(self._cue_starts, ms) - 1
        parts = []
        for k in range(i, max(-1, i - 4), -1):
            c = self._cues[k]
            if c[1] >= ms:
                parts.append(c[2])
        text = "\n".join(reversed(parts))
        if text != self._cue_text:
            self._cue_text = text
            self._show_sub_text(text)

    def _load_subtitle_async(self, url: str) -> None:
        gen = self._sub_gen
        headers = {"User-Agent": _UA}
        if self._live_referer:
            headers["Referer"] = self._live_referer
        headers.update(self._live_headers or {})

        def _get(u: str) -> str:
            r = _ad_hoc_client.get(u, headers=headers, timeout=15)
            r.raise_for_status()
            return r.content.decode("utf-8-sig", errors="replace")

        def _worker() -> None:
            cues: list = []
            try:
                text = _get(url)
                if text.lstrip().startswith("#EXTM3U"):
                    segs, off, dur = [], 0, 0.0
                    for ln in text.splitlines():
                        ln = ln.strip()
                        if ln.startswith("#EXTINF:"):
                            dur = float(ln[8:].split(",")[0] or 0)
                        elif ln and not ln.startswith("#"):
                            segs.append((urljoin(url, ln), off))
                            off += int(dur * 1000)
                    with ThreadPoolExecutor(6) as ex:
                        parts = list(ex.map(lambda so: (so[1], _get(so[0])), segs[:600]))
                    for off, body in parts:
                        cues.extend((a + off, b + off, t) for a, b, t in _parse_subtitle_cues(body))
                    cues.sort(key=lambda c: c[0])
                else:
                    cues = _parse_subtitle_cues(text)
            except Exception as exc:
                logger.warning("Subtitle fetch failed for %s: %s", url, exc)
            if gen == self._sub_gen:
                self._subtitle_cues_ready.emit(cues)

        threading.Thread(target=_worker, daemon=True).start()

    def _set_active_sub(self, idx: int) -> None:
        if hasattr(self._player, "setActiveSubtitleTrack"):
            try:
                self._player.setActiveSubtitleTrack(idx)
            except Exception:
                pass

    def _on_active_subtitle_track_changed(self, index: int) -> None:
        if index != self._intended_embedded_track:
            self._set_active_sub(self._intended_embedded_track)

    def _rebuild_sub_menu(self) -> None:
        self._sub_btn.setVisible(len(self._sub_entries) > 1)
        self._sub_btn.setText("CC" if self._sub_cur == 0 else "CC \u25cf")
        QTimer.singleShot(0, self._rebuild_sub_menu_now)

    def _rebuild_sub_menu_now(self) -> None:
        menu = self._sub_btn.menu()
        menu.clear()
        grp = QActionGroup(menu)
        off = tr("anime.subtitles_off", default="Subtitles: Off")
        for i, (k, _x, lbl) in enumerate(self._sub_entries):
            act = menu.addAction(off if k == "off" else lbl)
            act.setCheckable(True)
            act.setChecked(i == self._sub_cur)
            grp.addAction(act)
            act.triggered.connect(partial(self._on_subtitle_picked, i))
        menu.addSeparator()
        menu.addMenu(self._style_menu)

    def _set_sub_style(self, **kw) -> None:
        self._sub_style.update(kw)
        _save_sub_style(self._sub_style)
        self._sub_ov.refresh(force=True)

    def _build_style_menu(self) -> QMenu:
        m = QMenu(tr("anime.subtitle_style", default="Subtitle style"), self)
        m.setStyleSheet(_MENU_QSS)
        st = self._sub_style

        def group(title, options, key, label=lambda o: o[0], value=lambda o: o[1]):
            sub = m.addMenu(title)
            grp = QActionGroup(sub)
            for o in options:
                a = sub.addAction(label(o))
                a.setCheckable(True)
                a.setChecked(abs(float(st[key]) - float(value(o))) < 1e-6 if isinstance(value(o), (int, float)) and not isinstance(value(o), bool) else st[key] == value(o))
                grp.addAction(a)
                a.triggered.connect(partial(self._set_sub_style, **{key: value(o)}))

        def _lbl(prefix: str):
            return lambda o: tr(f"anime.sub_{prefix}.{o[0].lower().replace(' ', '_')}", default=o[0])

        group(tr("anime.sub_style.font", default="Font"), _SUB_FONTS, "font", label=lambda o: o, value=lambda o: o)
        group(tr("anime.sub_style.size", default="Size"), _SUB_SIZES, "size", label=_lbl("size"))
        group(tr("anime.sub_style.color", default="Color"), _SUB_COLORS, "color", label=_lbl("color"))
        group(tr("anime.sub_style.outline", default="Outline"), _SUB_OUTLINES, "outline", label=_lbl("outline"))
        bold = m.addAction(tr("anime.sub_style.bold", default="Bold"))
        bold.setCheckable(True)
        bold.setChecked(bool(st["bold"]))
        bold.toggled.connect(lambda v: self._set_sub_style(bold=bool(v)))
        bg = m.addAction(tr("anime.sub_style.background", default="Background box"))
        bg.setCheckable(True)
        bg.setChecked(bool(st["bg"]))
        bg.toggled.connect(lambda v: self._set_sub_style(bg=bool(v)))
        m.addSeparator()
        m.addAction(tr("anime.sub_style.reset_position", default="Reset position")).triggered.connect(
            lambda: self._set_sub_style(x=_SUB_DEFAULTS["x"], y=_SUB_DEFAULTS["y"]))
        return m

    def _populate_subtitle_options(self) -> None:
        self._sub_entries = [("off", -1, "")]
        for i, sub in enumerate(self._external_subs):
            label = sub.get("label") or sub.get("srclang") or f"{tr('anime.subtitle', default='Subtitle')} {i + 1}"
            self._sub_entries.append(("external", i, label))
        pick = 0
        if self._sub_pref != "" and len(self._sub_entries) > 1:
            subs = self._external_subs

            def _is_en(i: int) -> bool:
                lab = (self._sub_entries[i + 1][2] or "").lower()
                return str(subs[i].get("srclang", "")).lower().startswith("en") or lab.startswith("en")

            order = [i for i in range(len(subs))]
            cand = None
            if self._sub_pref:
                cand = next((i for i in order if self._sub_pref in self._sub_entries[i + 1][2].lower()), None)
            if cand is None:
                cand = next((i for i in order if subs[i].get("default")), None)
            if cand is None:
                cand = next((i for i in order if _is_en(i)), None)
            if cand is None and self._sub_pref is None:
                cand = 0
            if cand is not None:
                pick = cand + 1
        self._sub_cur = pick
        self._rebuild_sub_menu()
        if pick > 0:
            self._apply_subtitle(pick)

    def _refresh_subtitle_tracks(self) -> None:
        self._sub_entries = [e for e in self._sub_entries if e[0] != "embedded"]
        try:
            tracks = self._player.subtitleTracks() if hasattr(self._player, "subtitleTracks") else []
        except Exception:
            tracks = []
        for i, meta in enumerate(tracks):
            self._sub_entries.append(("embedded", i, self._describe_subtitle_track(meta, i)))
        self._sub_cur = min(self._sub_cur, len(self._sub_entries) - 1)
        self._rebuild_sub_menu()
        kind, idx, _l = self._sub_entries[self._sub_cur]
        self._intended_embedded_track = idx if kind == "embedded" else -1
        self._set_active_sub(self._intended_embedded_track)

    @staticmethod
    def _describe_subtitle_track(meta, index: int) -> str:
        try:
            from PySide6.QtMultimedia import QMediaMetaData
            title = meta.stringValue(QMediaMetaData.Key.Title)
            lang = meta.value(QMediaMetaData.Key.Language)
            if lang is not None:
                try:
                    from PySide6.QtCore import QLocale
                    name = QLocale.languageToString(QLocale.Language(lang))
                except Exception:
                    name = str(lang)
                return title or name or f"Track {index + 1}"
            if title:
                return title
        except Exception:
            pass
        return f"Track {index + 1}"

    def _on_subtitle_picked(self, idx: int) -> None:
        if not (0 <= idx < len(self._sub_entries)):
            return
        kind, _i, label = self._sub_entries[idx]
        self._sub_pref = "" if kind == "off" else label.lower()
        self._apply_subtitle(idx)

    def _apply_subtitle(self, idx: int) -> None:
        self._sub_gen += 1
        self._sub_cur = idx
        kind, i, _l = self._sub_entries[idx]
        self._intended_embedded_track = i if kind == "embedded" else -1
        self._set_active_sub(self._intended_embedded_track)
        self._clear_cues()
        self._rebuild_sub_menu()
        if kind == "external" and 0 <= i < len(self._external_subs):
            url = self._external_subs[i].get("url")
            if url:
                self._load_subtitle_async(url)


    def toggle_fullscreen(self) -> None:
        self.exit_fullscreen() if self._is_fullscreen else self.enter_fullscreen()

    def _vid(self) -> QVideoWidget:
        return self._video_widget

    def _pip_edge(self, pos) -> Qt.Edge:
        if not self._pip_active:
            return Qt.Edge(0)
        edge = Qt.Edge(0)
        margin = 6
        if pos.x() <= margin:
            edge |= Qt.Edge.LeftEdge
        elif pos.x() >= self.width() - margin:
            edge |= Qt.Edge.RightEdge
        if pos.y() <= margin:
            edge |= Qt.Edge.TopEdge
        elif pos.y() >= self.height() - margin:
            edge |= Qt.Edge.BottomEdge
        return edge

    def _pip_resize(self, source: QWidget, pos) -> bool:
        if not self._pip_active:
            return False
        edge = self._pip_edge(source.mapTo(self, pos))
        if not edge:
            return False
        handle = self.window().windowHandle()
        if handle is not None:
            handle.startSystemResize(edge)
            return True
        return False

    def _ensure_fs(self) -> None:
        if self._fs_win is not None:
            return
        w = QWidget(None, Qt.WindowType.Window | Qt.WindowType.FramelessWindowHint)
        w.setStyleSheet("background:black;")
        w.setMouseTracking(True)
        lay = QVBoxLayout(w)
        lay.setContentsMargins(0, 0, 0, 0)
        w.installEventFilter(self)
        bw = QWidget(w, Qt.WindowType.Tool | Qt.WindowType.FramelessWindowHint
                     | Qt.WindowType.WindowDoesNotAcceptFocus | Qt.WindowType.NoDropShadowWindowHint)
        bw.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        bw.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        bl = QVBoxLayout(bw)
        bl.setContentsMargins(0, 0, 0, 0)
        self._fs_win, self._fs_bar_win = w, bw

    def _swap_overlay(self, owner: QWidget) -> None:
        self._bubble.hide()
        self._bubble.deleteLater()
        self._bubble = _SkipBubble(self, owner)
        self._refresh_bubble()
        self._sub_ov.hide()
        self._sub_ov.deleteLater()
        self._sub_ov = _SubtitleOverlay(self, owner)
        self._ov_timer.timeout.disconnect()
        self._ov_timer.timeout.connect(self._sub_ov.refresh)
        self._loader.set_active(False)
        self._loader.deleteLater()
        self._loader = _LoadingOverlay(self, owner)
        self._loader.set_active(self._busy_on)
        if self._cue_text:
            self._show_sub_text(self._cue_text)

    def enter_fullscreen(self) -> None:
        if self._is_fullscreen:
            return
        if self._pip_active:
            self.exit_pip(restore=False)
        self._ensure_fs()
        self._is_fullscreen = True
        self._fullscreen_btn.setIcon(_wicon("BACK_TO_WINDOW", FluentIcon.CLOSE))
        self._outer.removeWidget(self._video_widget)
        self._video_widget.setParent(self._fs_win)
        self._fs_win.layout().addWidget(self._video_widget)
        self._video_widget.show()
        self._fs_win.showFullScreen()
        self._nudge_frame()
        self._outer.removeWidget(self._bar)
        self._bar.setParent(self._fs_bar_win)
        self._bar.floating = True
        self._bar.setFixedSize(self._fs_bar_w(), self._BAR_H)
        self._fs_bar_win.layout().addWidget(self._bar)
        self._bar.show()
        self._swap_overlay(self._fs_win)
        self._fs_win.activateWindow()
        self._fs_win.setFocus()
        self._reveal_controls()

    def exit_fullscreen(self) -> None:
        if not self._is_fullscreen:
            return
        self._hide_timer.stop()
        self._fs_bar_win.hide()
        self._fs_bar_win.layout().removeWidget(self._bar)
        self._is_fullscreen = False
        self._fs_win.layout().removeWidget(self._video_widget)
        self._video_widget.setParent(self)
        self._outer.insertWidget(0, self._video_widget, 1)
        self._video_widget.show()
        self._nudge_frame()
        self._bar.floating = False
        self._bar.setParent(self)
        self._bar.setFixedHeight(self._BAR_H)
        self._bar.setMinimumWidth(0)
        self._bar.setMaximumWidth(_QWIDGETSIZE_MAX)
        self._outer.addWidget(self._bar)
        self._bar.show()
        self._fullscreen_btn.setIcon(_wicon("FULL_SCREEN", FluentIcon.VIEW))
        self._fs_win.hide()
        self._fs_win.unsetCursor()
        self._swap_overlay(self)
        self.setFocus()

    def _nudge_frame(self) -> None:
        if not self._playing() and not self._player.source().isEmpty():
            self._player.setPosition(self._player.position())

    def _fs_bar_w(self) -> int:
        return max(320, min(900, int(self._fs_win.width() * 0.8)))

    def _place_fs_bar(self) -> None:
        w = self._fs_win
        bw = self._fs_bar_w()
        self._bar.setFixedSize(bw, self._BAR_H)
        g = w.mapToGlobal(QPoint(0, 0))
        self._fs_bar_win.setGeometry(g.x() + (w.width() - bw) // 2, g.y() + w.height() - self._BAR_H - 36, bw, self._BAR_H)


    def _reveal_controls(self, zone: bool = True) -> None:
        if not self._is_fullscreen:
            return
        self._fs_win.unsetCursor()
        if zone:
            if not self._fs_bar_win.isVisible():
                self._place_fs_bar()
                self._fs_bar_win.show()
        if self._playing() or not zone:
            self._hide_timer.start(2500 if zone else 700)
        else:
            self._hide_timer.stop()

    def _auto_hide_controls(self) -> None:
        if not self._is_fullscreen or not self._playing():
            return
        if self._fs_bar_win.underMouse() or any(b.menu().isVisible() for b in (self._speed_btn, self._quality_btn, self._sub_btn)):
            self._hide_timer.start(2500)
            return
        self._fs_bar_win.hide()
        self._fs_win.setCursor(Qt.CursorShape.BlankCursor)

    def eventFilter(self, obj, event) -> bool:
        t = event.type()
        fs = self._is_fullscreen and obj is self._video_widget
        if obj is self._fs_win and t == QEvent.Type.KeyPress:
            self.keyPressEvent(event)
            return True
        if obj is self._bar and self._pip_active and t == QEvent.Type.MouseButtonPress \
                and event.button() == Qt.MouseButton.LeftButton:
            if self._pip_resize(obj, event.position().toPoint()):
                return True
            handle = self.window().windowHandle()
            if handle is not None:
                handle.startSystemMove()
                return True
        if obj is self._video_widget:
            if t == QEvent.Type.MouseMove and self._pip_active:
                if self._pip_drag is not None and (event.buttons() & Qt.MouseButton.LeftButton):
                    origin, win_pos = self._pip_drag
                    delta = event.globalPosition().toPoint() - origin
                    if self._pip_dragging or delta.manhattanLength() > 4:
                        self._pip_dragging = True
                        self._click_timer.stop()
                        self.window().move(win_pos + delta)
                    return True
            elif t == QEvent.Type.MouseMove and fs:
                in_zone = event.position().y() >= obj.height() - (self._BAR_H + 70)
                self._reveal_controls(zone=in_zone)
            elif t == QEvent.Type.MouseButtonPress and event.button() == Qt.MouseButton.LeftButton:
                if self._pip_resize(obj, event.position().toPoint()):
                    return True
                if self._pip_active:
                    self._pip_drag = (event.globalPosition().toPoint(), self.window().pos())
                    self._pip_dragging = False
                from PySide6.QtWidgets import QApplication
                self._click_timer.start(min(QApplication.doubleClickInterval(), 300))
                return True
            elif t == QEvent.Type.MouseButtonRelease and self._pip_active and self._pip_drag is not None:
                self._pip_drag = None
                if self._pip_dragging:
                    self._pip_dragging = False
                    return True
            elif t == QEvent.Type.Wheel:
                self._volume_slider.setValue(max(0, min(100, self._volume_slider.value() + (5 if event.angleDelta().y() > 0 else -5))))
                self._osd(tr("anime.volume_pct", default="Volume {v}%").format(v=self._volume_slider.value()))
                return True
            elif t == QEvent.Type.MouseButtonDblClick:
                self._click_timer.stop()
                self.toggle_fullscreen()
                return True
        return super().eventFilter(obj, event)

    def mouseMoveEvent(self, event) -> None:
        super().mouseMoveEvent(event)

    def hideEvent(self, event) -> None:
        self._bubble.set_state(None, None)
        self._sub_ov.hide()
        if not self._suppress_hide and not self._pip_active and self._playing():
            self._player.pause()
            self._auto_paused = True
        super().hideEvent(event)

    def showEvent(self, event) -> None:
        if self._cue_text:
            QTimer.singleShot(0, lambda: self._sub_ov.refresh(force=True))
        if self._auto_paused:
            self._player.play()
            self._auto_paused = False
        super().showEvent(event)

    def keyPressEvent(self, event) -> None:
        key = event.key()
        shift = bool(event.modifiers() & Qt.KeyboardModifier.ShiftModifier)
        self._reveal_controls()
        vod = not self._is_live
        if key == Qt.Key.Key_Escape and self._pip_active:
            self.exit_pip()
            return
        if key in (Qt.Key.Key_Space, Qt.Key.Key_K):
            self._toggle_play_pause()
        elif key in (Qt.Key.Key_F, Qt.Key.Key_F11):
            self.toggle_fullscreen()
        elif key == Qt.Key.Key_Escape and self._is_fullscreen:
            self.exit_fullscreen()
        elif key in (Qt.Key.Key_Left, Qt.Key.Key_Right) and vod:
            d = 30_000 if shift else 5_000
            self._seek_relative(-d if key == Qt.Key.Key_Left else d)
            self._osd(f"{'-' if key == Qt.Key.Key_Left else '+'}{d // 1000}s")
        elif key in (Qt.Key.Key_J, Qt.Key.Key_L) and vod:
            self._seek_relative(-10_000 if key == Qt.Key.Key_J else 10_000)
            self._osd("-10s" if key == Qt.Key.Key_J else "+10s")
        elif key == Qt.Key.Key_S and vod:
            self._seek_relative(self._SKIP_INTRO_MS)
            self._osd(tr("anime.skip_intro", default="Skipped +{s}s").format(s=self._SKIP_INTRO_MS // 1000))
        elif key == Qt.Key.Key_Home and vod:
            self._seek_to(0)
        elif key == Qt.Key.Key_End and vod and self._player.duration():
            self._seek_to(self._player.duration() - 1000)
        elif Qt.Key.Key_0 <= key <= Qt.Key.Key_9 and vod and self._player.duration():
            self._seek_to(self._player.duration() * (key - Qt.Key.Key_0) // 10)
        elif key == Qt.Key.Key_Up:
            self._volume_slider.setValue(min(100, self._volume_slider.value() + 5))
            self._osd(tr("anime.volume_pct", default="Volume {v}%").format(v=self._volume_slider.value()))
        elif key == Qt.Key.Key_Down:
            self._volume_slider.setValue(max(0, self._volume_slider.value() - 5))
            self._osd(tr("anime.volume_pct", default="Volume {v}%").format(v=self._volume_slider.value()))
        elif key == Qt.Key.Key_M:
            self._toggle_mute()
        elif key == Qt.Key.Key_BracketRight and vod:
            self._step_speed(1)
        elif key == Qt.Key.Key_BracketLeft and vod:
            self._step_speed(-1)
        elif key == Qt.Key.Key_C:
            self._cycle_subtitles()
        elif key == Qt.Key.Key_Z:
            self._nudge_sub_delay(-100)
        elif key == Qt.Key.Key_X:
            self._nudge_sub_delay(100)
        elif key == Qt.Key.Key_N and self._next_btn.isVisible():
            self.next_requested.emit()
        else:
            super().keyPressEvent(event)
            return
        event.accept()


def _is_movie(fmt: Any) -> bool:
    return str(fmt or '').strip().upper() == 'MOVIE'


def _episode_number_label(n: Any) -> str:
    try:
        f = float(n)
        return f"Episode {int(f)}" if f.is_integer() else f"Episode {f}"
    except (TypeError, ValueError):
        return f"Episode {n}"

def _episode_key_number(value: Any) -> Any:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return int(number) if number.is_integer() else number

def _episode_tag(season: Any, episode: Any) -> str:
    def _fmt(value) -> str:
        key = _episode_key_number(value)
        value = key if key is not None else value
        return f"{value:02d}" if isinstance(value, int) else str(value)
    return f"S{_fmt(season)}E{_fmt(episode)}" if season is not None else f"E{_fmt(episode)}"

_EPISODE_FILE_RE = re.compile(r"[sS](\d{1,2})[ ._-]*[eE](\d{1,3})|(?:^|\D)(\d{1,2})x(\d{1,3})(?:\D|$)")

def _torrent_file_entries(files: list) -> list[dict]:
    entries: list[dict] = []
    for index, pair in enumerate(files or []):
        try:
            path, size = pair
        except (TypeError, ValueError):
            continue
        path = str(path or "").replace("\\", "/")
        if not stream_backend.looks_like_video(path):
            continue
        name = path.rsplit("/", 1)[-1]
        season = episode = None
        if match := _EPISODE_FILE_RE.search(name):
            if match.group(1):
                season, episode = int(match.group(1)), int(match.group(2))
            else:
                season, episode = int(match.group(3)), int(match.group(4))
        entries.append({"index": index, "path": path, "name": name, "size": int(size or 0),
                        "season": season, "episode": episode})
    return entries

_EP_IMG_KEYS = ("image", "img", "thumbnail", "thumb", "still", "snapshot", "poster", "cover")


class _AnimeEpisodeCard(_LeftClickCard):
    context_requested = Signal(object, QPoint)

    def __init__(self, ep: dict, fallback_url: str, on_click, parent=None):
        super().__init__(parent)
        self._ep = ep
        self._url = next((ep[k] for k in _EP_IMG_KEYS if isinstance(ep.get(k), str) and ep.get(k)), "") or fallback_url
        self._loaded = False
        self.setFixedSize(230, 172)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.clicked.connect(lambda: on_click(ep))
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.customContextMenuRequested.connect(
            lambda pos: self.context_requested.emit(self._ep, self.mapToGlobal(pos)))
        lay = QVBoxLayout(self)
        lay.setContentsMargins(6, 6, 6, 6)
        lay.setSpacing(4)
        self._thumb = QLabel(self)
        self._thumb.setFixedSize(218, 122)
        self._thumb.setScaledContents(True)
        self._thumb.setStyleSheet(f"background: {palette()['poster_fallback_bg']}; border-radius: 6px;")
        lay.addWidget(self._thumb)
        num = _episode_number_label(ep.get("number")).replace("Episode ", "")
        title = (ep.get("title") or "").strip()
        if re.fullmatch(r"(?i)(?:episode|ep\.?)\s*#?\s*\d+(?:\.\d+)?", title):
            title = ""
        lbl = CaptionLabel(f"{num}. {title}" if title else f"Episode {num}", self)
        lbl.setWordWrap(True)
        lbl.setMaximumHeight(32)
        apply_label_theme(lbl, "muted")
        lay.addWidget(lbl)

    def paintEvent(self, e) -> None:
        if not self._loaded:
            self._loaded = True
            if self._url:
                images.get(self._url, (218, 122), self._apply, owner=self)
        super().paintEvent(e)

    def _apply(self, pixmap) -> None:
        if not pixmap.isNull():
            self._thumb.setPixmap(pixmap)


def _sanitize_filename(name: str) -> str:
    return _SANITIZE_FILENAME_RE.sub("_", str(name)).strip() or "unknown"

def _resolve_ffmpeg() -> Optional[str]:
    found = shutil.which("ffmpeg")
    if found:
        return found
    from src.core.config import paths as _paths
    for candidate in (
        os.path.join(_paths.yt_dir, "ffmpeg.exe" if os.name == "nt" else "ffmpeg"),
        os.path.join(_paths.app_data_dir, "yt", "ffmpeg.exe" if os.name == "nt" else "ffmpeg"),
    ):
        if os.path.isfile(candidate):
            return candidate
    return None

def _download_hls_pure(url: str, referer: Optional[str], headers: dict, dest_path: str, item_id, download_manager, is_cancelled=None) -> None:
    import time
    from urllib.parse import urljoin

    req_headers = {"User-Agent": _UA}
    if referer:
        req_headers["Referer"] = referer
    req_headers.update(headers)

    def _get(u: str) -> httpx.Response:
        r = _ad_hoc_client.get(u, headers=req_headers, timeout=20)
        r.raise_for_status()
        return r

    text = _get(url).text
    if "#EXT-X-STREAM-INF" in text:
        variants = [urljoin(url, ln.strip()) for ln in text.splitlines() if ln.strip() and not ln.startswith("#")]
        if variants:
            url = variants[-1]
            text = _get(url).text

    seg_urls: list[str] = []
    key_url = None
    key_iv = None
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("#EXT-X-KEY"):
            m = _KEY_URI_RE.search(line)
            if m:
                key_url = urljoin(url, m.group(1))
            iv_m = _KEY_IV_RE.search(line)
            if iv_m:
                key_iv = bytes.fromhex(iv_m.group(1))
        elif line and not line.startswith("#"):
            seg_urls.append(urljoin(url, line))

    if not seg_urls:
        raise RuntimeError("No segments found in HLS playlist")

    key_bytes = _get(key_url).content if key_url else None
    cipher_cls = None
    AES = None
    Cipher = algorithms = modes = None
    if key_bytes:
        try:
            from Crypto.Cipher import AES
            cipher_cls = "pycryptodome"
        except ImportError:
            try:
                from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
                cipher_cls = "cryptography"
            except ImportError:
                raise RuntimeError("Encrypted stream requires the pycryptodome or cryptography package") from None

    total_segments = len(seg_urls)
    downloaded_bytes = 0
    start_time = time.time()
    last_emit = 0.0
    last_bytes = 0
    last_time = start_time
    avg_seg_size = 0.0

    with open(dest_path, "wb") as fh:
        for idx, seg_url in enumerate(seg_urls):
            if is_cancelled and is_cancelled():
                raise RuntimeError("Cancelled")
            resp = _get(seg_url)
            data = resp.content
            if key_bytes:
                iv = key_iv if key_iv else idx.to_bytes(16, "big")
                if cipher_cls == "pycryptodome":
                    data = AES.new(key_bytes, AES.MODE_CBC, iv).decrypt(data)
                else:
                    decryptor = Cipher(algorithms.AES(key_bytes), modes.CBC(iv)).decryptor()
                    data = decryptor.update(data) + decryptor.finalize()
            fh.write(data)
            seg_size = len(data)
            downloaded_bytes += seg_size
            avg_seg_size = seg_size if idx == 0 else avg_seg_size * 0.8 + seg_size * 0.2

            now = time.time()
            if now - last_emit > 0.3 or idx == total_segments - 1:
                elapsed = now - last_time
                speed_kbps = ((downloaded_bytes - last_bytes) / 1024) / elapsed if elapsed > 0 else 0.0
                last_bytes = downloaded_bytes
                last_time = now
                last_emit = now
                progress = (idx + 1) / total_segments * 100
                remaining = total_segments - (idx + 1)
                estimated_total = downloaded_bytes + int(avg_seg_size * remaining)
                download_manager.update_external(
                    item_id,
                    downloaded_bytes=downloaded_bytes,
                    total_bytes=max(estimated_total, downloaded_bytes),
                    progress=progress,
                    speed_down_kbps=speed_kbps,
                )
    return dest_path


def _download_ffmpeg_stream(url: str, referer: Optional[str], headers: dict, dest_path: str,
                            item_id, download_manager, is_cancelled=None) -> None:
    import subprocess
    import time

    ffmpeg = _resolve_ffmpeg()
    if not ffmpeg:
        raise RuntimeError(
            "This stream needs ffmpeg to download. Install ffmpeg from the YouTube page "
            "or add it to PATH.")
    cmd = [ffmpeg, "-y", "-loglevel", "error", "-progress", "pipe:1"]
    header_lines = [f"{key}: {value}" for key, value in (headers or {}).items()]
    if referer and not any(line.lower().startswith("referer:") for line in header_lines):
        header_lines.append(f"Referer: {referer}")
    if header_lines:
        cmd += ["-headers", "\r\n".join(header_lines) + "\r\n"]
    cmd += ["-i", url, "-c", "copy", dest_path]
    kwargs = {}
    if os.name == "nt":
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        kwargs["startupinfo"] = startupinfo
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                            text=True, **kwargs)
    last_bytes = 0
    last_time = time.time()
    try:
        for line in proc.stdout or []:
            if is_cancelled and is_cancelled():
                proc.terminate()
                raise RuntimeError("Cancelled")
            line = line.strip()
            if not line.startswith("total_size="):
                continue
            try:
                size = int(line.split("=", 1)[1])
            except (ValueError, IndexError):
                continue
            now = time.time()
            elapsed = now - last_time
            speed = ((size - last_bytes) / 1024) / elapsed if elapsed > 0 else 0.0
            last_bytes = size
            last_time = now
            download_manager.update_external(
                item_id, downloaded_bytes=size, speed_down_kbps=speed,
            )
        proc.wait()
    finally:
        if proc.poll() is None:
            proc.terminate()
    if proc.returncode != 0:
        raise RuntimeError("ffmpeg failed to download the stream")


def _do_download_direct_source(download_manager, item_id, source, dest_path: str, is_cancelled=None) -> str:
    url = source.url
    headers = {"User-Agent": _UA}
    headers.update(getattr(source, "headers", None) or {})
    referer = getattr(source, "referer", "") or ""
    if referer:
        headers["Referer"] = referer
    lower = url.lower()
    if ".m3u8" in lower:
        _download_hls_pure(url, referer, getattr(source, "headers", None) or {}, dest_path,
                           item_id, download_manager, is_cancelled)
        return dest_path
    if ".mpd" in lower:
        _download_ffmpeg_stream(url, referer, headers, dest_path, item_id,
                                download_manager, is_cancelled)
        return dest_path
    with _ad_hoc_client.stream("GET", url, headers=headers, timeout=60) as response:
        response.raise_for_status()
        total = int(response.headers.get("Content-Length") or 0)
        downloaded = 0
        with open(dest_path, "wb") as fh:
            for chunk in response.iter_bytes(1024 * 256):
                if is_cancelled and is_cancelled():
                    raise RuntimeError("Cancelled")
                fh.write(chunk)
                downloaded += len(chunk)
                download_manager.update_external(
                    item_id,
                    downloaded_bytes=downloaded,
                    total_bytes=max(total, downloaded),
                    progress=(downloaded / total * 100) if total else 0,
                    speed_down_kbps=0,
                )
    return dest_path


def _stream_quality_value(s: dict) -> Optional[int]:
    for key in ("quality", "label", "resolution"):
        val = s.get(key)
        if not val:
            continue
        m = _RESOLUTION_DIGITS_RE.search(str(val))
        if m:
            return int(m.group(1))
    return None

def _pick_stream(streams: list, quality: str) -> Optional[dict]:
    candidates = [s for s in streams if s.get("type") in ("hls", "mp4") and s.get("url")]
    if not candidates:
        return None

    rated = [(s, _stream_quality_value(s)) for s in candidates]
    if not any(q is not None for _, q in rated):
        active = next((s for s in candidates if s.get("isActive")), None)
        return active or candidates[0]

    rated_known = [(s, q) for s, q in rated if q is not None]
    if quality == "best":
        return max(rated_known, key=lambda pair: pair[1])[0]
    if quality == "worst":
        return min(rated_known, key=lambda pair: pair[1])[0]
    try:
        target = int(quality)
    except (TypeError, ValueError):
        target = None
    if target is not None:
        return min(rated_known, key=lambda pair: abs(pair[1] - target))[0]
    return max(rated_known, key=lambda pair: pair[1])[0]

def _select_subtitles(active: dict, data: dict, subtitle_lang: str) -> list[dict]:
    if subtitle_lang == "none":
        return []
    subs = active.get("subtitles") or data.get("subtitles") or []
    if not subs:
        return []
    if subtitle_lang == "all":
        return subs
    if subtitle_lang == "auto":
        defaults = [s for s in subs if s.get("default")]
        return defaults or subs[:1]
    matched = [
        s for s in subs
        if subtitle_lang in (s.get("label") or "").lower()
        or subtitle_lang in (s.get("srclang") or "").lower()
    ]
    return matched

def _download_subtitles(subs: list[dict], dest_dir: str, base_name: str, referer: Optional[str]) -> None:
    req_headers = {"User-Agent": _UA}
    if referer:
        req_headers["Referer"] = referer
    for sub in subs:
        sub_url = sub.get("url")
        if not sub_url:
            continue
        try:
            resp = _ad_hoc_client.get(sub_url, headers=req_headers, timeout=20)
            resp.raise_for_status()
        except Exception:
            continue
        ext = os.path.splitext(sub_url.split("?")[0])[1] or ".vtt"
        lang_tag = sub.get("srclang") or sub.get("label") or "sub"
        lang_tag = _sanitize_filename(lang_tag)
        sub_path = os.path.join(dest_dir, f"{base_name}.{lang_tag}{ext}")
        try:
            with open(sub_path, "wb") as fh:
                fh.write(resp.content)
        except OSError:
            pass

def _do_download_anime_episode(download_manager, item_id, anilist_id, provider, audio, episode, dest_dir, quality: str = "best", subtitle_lang: str = "none", is_cancelled=None) -> str:
    import asyncio
    import subprocess
    import time
    import urllib.request

    path = (f"/watch/{provider}/{episode.get('anilistId') or anilist_id}/{audio}/"
            f"{provider}-{episode.get('sourceNumber', episode.get('number'))}")
    data = asyncio.run(anime_backend.handle(path))
    streams = data.get("streams") or []

    active = _pick_stream(streams, quality)
    if active is None:
        raise RuntimeError("No downloadable stream found for this episode")

    os.makedirs(dest_dir, exist_ok=True)
    number = episode.get("number")
    ep_title = episode.get("title") or f"Episode {number}"
    try:
        num_label = f"{int(float(number)):02d}"
    except (TypeError, ValueError):
        num_label = str(number)
    base_name = f"{num_label} - {_sanitize_filename(ep_title)}"

    url = active["url"]
    referer = active.get("referer")
    headers = active.get("headers") or {}
    duration = episode.get("duration") or 0

    if active["type"] == "hls":
        ffmpeg_exe = _resolve_ffmpeg()
        if ffmpeg_exe:
            dest_path = os.path.join(dest_dir, f"{base_name}.mp4")
            cmd = [ffmpeg_exe, "-y", "-loglevel", "error", "-progress", "pipe:1"]
            header_lines = [f"{hk}: {hv}" for hk, hv in headers.items()]
            if referer and not any(line.lower().startswith("referer:") for line in header_lines):
                header_lines.append(f"Referer: {referer}")
            if header_lines:
                cmd += ["-headers", "\r\n".join(header_lines) + "\r\n"]
            cmd += ["-i", url, "-c", "copy", dest_path]
            popen_kwargs = {}
            if os.name == "nt":
                startupinfo = subprocess.STARTUPINFO()
                startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
                popen_kwargs["startupinfo"] = startupinfo
                popen_kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, **popen_kwargs)
            last_bytes = 0
            last_time = time.time()
            try:
                cur_size = 0
                for line in proc.stdout:
                    if is_cancelled and is_cancelled():
                        proc.terminate()
                        raise RuntimeError("Cancelled")
                    line = line.strip()
                    if line.startswith("total_size="):
                        try:
                            cur_size = int(line.split("=")[1])
                        except (ValueError, IndexError):
                            pass
                    elif line.startswith("out_time_ms="):
                        try:
                            out_ms = int(line.split("=")[1])
                        except (ValueError, IndexError):
                            out_ms = None
                        now = time.time()
                        elapsed = now - last_time
                        speed_kbps = ((cur_size - last_bytes) / 1024) / elapsed if elapsed > 0 else 0.0
                        last_bytes = cur_size
                        last_time = now
                        kwargs = {"downloaded_bytes": cur_size, "speed_down_kbps": speed_kbps}
                        if duration and out_ms is not None:
                            kwargs["progress"] = max(0.0, min(100.0, out_ms / 1000 / duration * 100))
                        download_manager.update_external(item_id, **kwargs)
                proc.wait()
            finally:
                if proc.poll() is None:
                    proc.terminate()
            if proc.returncode != 0:
                raise RuntimeError("ffmpeg failed to download the stream")
        else:
            dest_path = os.path.join(dest_dir, f"{base_name}.ts")
            _download_hls_pure(url, referer, headers, dest_path, item_id, download_manager, is_cancelled)
    else:
        dest_path = os.path.join(dest_dir, f"{base_name}.mp4")
        req_headers = {"User-Agent": _UA}
        if referer:
            req_headers["Referer"] = referer
        req_headers.update(headers)
        req = urllib.request.Request(url, headers=req_headers)
        with urllib.request.urlopen(req, timeout=30) as resp, open(dest_path, "wb") as fh:
            total = int(resp.headers.get("Content-Length") or 0)
            downloaded = 0
            last_emit = 0.0
            while True:
                if is_cancelled and is_cancelled():
                    raise RuntimeError("Cancelled")
                chunk = resp.read(1 << 16)
                if not chunk:
                    break
                fh.write(chunk)
                downloaded += len(chunk)
                now = time.time()
                if now - last_emit > 0.3:
                    last_emit = now
                    download_manager.update_external(item_id, downloaded_bytes=downloaded, total_bytes=total)

    subs = _select_subtitles(active, data, subtitle_lang)
    if subs:
        _download_subtitles(subs, dest_dir, base_name, referer)

    return dest_path

_DEFAULT_PROVIDER = "anipub"
_QUALITY_CAPABLE_PROVIDERS = {"animegg"}


_SUBTITLE_CAPABLE_PROVIDERS = {"animedunya", "anipub", "anikoto", "kaa", "anizone"}

class AnimeDownloadDialog(MessageBoxBase):
    def __init__(self, episodes_by_provider: dict, default_provider: Optional[str], default_audio: str,
                 parent=None, preset_episode: Any = None) -> None:
        super().__init__(parent)
        self._episodes_by_provider = episodes_by_provider
        self._checkboxes: list[tuple[dict, CheckBox]] = []
        self._preset_episode = _episode_key_number(preset_episode)

        self.titleLabel = SubtitleLabel(tr("anime.download_dialog_title", default="Download Episodes"), self)
        self.viewLayout.addWidget(self.titleLabel)

        layout = self.viewLayout
        layout.setSpacing(12)

        form_row = QHBoxLayout()
        form_row.setSpacing(10)

        _non_provider_keys = {"page", "type", "mappings", "_unknownProviders"}
        provider_names = sorted(
            name for name, pdata in episodes_by_provider.items()
            if name not in _non_provider_keys
            and isinstance(pdata, dict) and not pdata.get("error")
            and isinstance(pdata.get("episodes"), dict) and pdata["episodes"].get("sub")
        )

        self._provider_combo = ComboBox()
        self._provider_combo.setFixedWidth(150)
        for name in provider_names:
            self._provider_combo.addItem(name, userData=name)
        if default_provider in provider_names:
            self._provider_combo.setCurrentIndex(provider_names.index(default_provider))

        form_row.addWidget(StrongBodyLabel(tr("anime.provider", default="Provider")))
        form_row.addWidget(self._provider_combo)
        form_row.addStretch(1)

        self._audio_container = QWidget()
        audio_container_layout = QHBoxLayout(self._audio_container)
        audio_container_layout.setContentsMargins(0, 0, 0, 0)
        audio_container_layout.setSpacing(10)
        self._audio_combo = ComboBox()
        self._audio_combo.addItem(tr("anime.sub", default="Sub"), userData="sub")
        self._audio_combo.addItem(tr("anime.dub", default="Dub"), userData="dub")
        self._audio_combo.setCurrentIndex(1 if default_audio == "dub" else 0)
        audio_container_layout.addWidget(self._audio_combo)
        form_row.addWidget(self._audio_container)

        layout.addLayout(form_row)

        quality_row = QHBoxLayout()
        quality_row.setSpacing(10)

        self._quality_container = QWidget()
        quality_container_layout = QHBoxLayout(self._quality_container)
        quality_container_layout.setContentsMargins(0, 0, 0, 0)
        quality_container_layout.setSpacing(10)
        self._quality_combo = ComboBox()
        for label, value in (
            (tr("anime.quality_best", default="Best available"), "best"),
            ("1080p", "1080"),
            ("720p", "720"),
            ("480p", "480"),
            ("360p", "360"),
            (tr("anime.quality_worst", default="Worst available"), "worst"),
        ):
            self._quality_combo.addItem(label, userData=value)
        quality_container_layout.addWidget(StrongBodyLabel(tr("anime.quality", default="Quality")))
        quality_container_layout.addWidget(self._quality_combo, 1)
        quality_row.addWidget(self._quality_container, 1)

        self._subtitle_container = QWidget()
        subtitle_container_layout = QHBoxLayout(self._subtitle_container)
        subtitle_container_layout.setContentsMargins(0, 0, 0, 0)
        subtitle_container_layout.setSpacing(10)
        self._subtitle_combo = ComboBox()
        for label, value in (
            (tr("anime.subtitles_none", default="No subtitles"), "none"),
            (tr("anime.subtitles_auto", default="Auto (default track)"), "auto"),
            (tr("anime.subtitles_all", default="All languages"), "all"),
            ("English", "english"),
            ("Spanish", "spanish"),
            ("French", "french"),
            ("German", "german"),
            ("Portuguese", "portuguese"),
            ("Arabic", "arabic"),
            ("Japanese", "japanese"),
        ):
            self._subtitle_combo.addItem(label, userData=value)
        subtitle_container_layout.addWidget(StrongBodyLabel(tr("anime.subtitles", default="Subtitles")))
        subtitle_container_layout.addWidget(self._subtitle_combo, 1)
        quality_row.addWidget(self._subtitle_container, 1)

        self._quality_row_widget = QWidget()
        self._quality_row_widget.setLayout(quality_row)
        layout.addWidget(self._quality_row_widget)

        self._select_all_chk = CheckBox(tr("anime.select_all", default="Select All"))
        self._select_all_chk.stateChanged.connect(self._on_select_all)
        layout.addWidget(self._select_all_chk)

        scroll = SmoothScrollArea(self)
        scroll.setWidgetResizable(True)
        scroll.setFixedHeight(280)
        scroll.setFrameShape(QAbstractScrollArea.Shape.NoFrame)
        scroll.setStyleSheet("QScrollArea { background: transparent; border: none; }")

        self._ep_container = QWidget()
        self._ep_layout = QVBoxLayout(self._ep_container)
        self._ep_layout.setContentsMargins(4, 4, 4, 4)
        self._ep_layout.setSpacing(6)
        scroll.setWidget(self._ep_container)
        layout.addWidget(scroll, 1)

        self._error_lbl = CaptionLabel("")
        self._error_lbl.setStyleSheet(f"color: {palette()['state_error']};")
        self._error_lbl.hide()
        layout.addWidget(self._error_lbl)

        self.yesButton.setText(tr("anime.download", default="Download"))
        self.yesButton.setIcon(FluentIcon.DOWNLOAD)
        self.cancelButton.setText(tr("anime.cancel", default="Cancel"))
        self.widget.setMinimumWidth(460)

        self._provider_combo.currentIndexChanged.connect(self._on_provider_changed)
        self._audio_combo.currentIndexChanged.connect(self._rebuild_episode_list)
        self._on_provider_changed()

    def _on_provider_changed(self, _index: int = 0) -> None:
        provider = self._provider_combo.currentData()
        provider_data = self._episodes_by_provider.get(provider) or {}
        provider_episodes = provider_data.get("episodes") or {}

        has_dub = bool(provider_episodes.get("dub"))
        self._audio_container.setVisible(has_dub)
        if not has_dub:
            self._audio_combo.blockSignals(True)
            self._audio_combo.setCurrentIndex(0)
            self._audio_combo.blockSignals(False)

        has_quality = provider in _QUALITY_CAPABLE_PROVIDERS
        self._quality_container.setVisible(has_quality)
        has_subs = provider in _SUBTITLE_CAPABLE_PROVIDERS
        self._subtitle_container.setVisible(has_subs)
        self._quality_row_widget.setVisible(has_quality or has_subs)

        self._rebuild_episode_list()

    def _on_select_all(self, _state) -> None:
        checked = self._select_all_chk.isChecked()
        for _, chk in self._checkboxes:
            chk.setChecked(checked)

    def validate(self) -> bool:
        if any(chk.isChecked() for _ep, chk in self._checkboxes):
            return True
        self._error_lbl.setText(tr("anime.select_episode_error", default="Select an episode to download"))
        self._error_lbl.show()
        return False

    def _rebuild_episode_list(self) -> None:
        while self._ep_layout.count():
            item = self._ep_layout.takeAt(0)
            w = item.widget() if hasattr(item, "widget") else None
            if w:
                w.deleteLater()
        self._checkboxes.clear()
        self._error_lbl.hide()

        provider = self._provider_combo.currentData()
        audio = self._audio_combo.currentData() or "sub"
        provider_data = self._episodes_by_provider.get(provider) or {}
        episodes = provider_data.get("episodes")
        episodes = episodes.get(audio, []) if isinstance(episodes, dict) else []

        for ep in episodes:
            label = _episode_number_label(ep.get("number"))
            if ep.get("title"):
                label = f"{label} — {ep['title']}"
            chk = CheckBox(label)
            if self._preset_episode is not None and _episode_key_number(ep.get("number")) == self._preset_episode:
                chk.setChecked(True)
            self._ep_layout.addWidget(chk)
            self._checkboxes.append((ep, chk))
        self._ep_layout.addStretch(1)

    def selected(self) -> tuple[Optional[str], str, list[dict], str, str]:
        provider = self._provider_combo.currentData()
        audio = self._audio_combo.currentData() or "sub"
        episodes = [ep for ep, chk in self._checkboxes if chk.isChecked()]
        quality = self._quality_combo.currentData() or "best"
        subtitle_lang = self._subtitle_combo.currentData() or "none"
        return provider, audio, episodes, quality, subtitle_lang

class AnimeDetailView(QWidget):
    back_requested = Signal()
    download_requested = Signal(object, str, str, object, str, str)
    provider_requested = Signal(str)
    season_requested = Signal(object)
    banner_changed = Signal()

    def _set_genre_pills(self, genres: list) -> None:
        for p in self._genre_pills:
            p.deleteLater()
        self._genre_pills = []
        for g in genres:
            pill = PillPushButton(str(g), self._genres_box)
            pill.setCheckable(False)
            pill.setFixedHeight(28)
            self._genres_row.insertWidget(self._genres_row.count() - 1, pill)
            self._genre_pills.append(pill)

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._media: Optional[dict] = None
        self._seasons: list = []
        self._episodes_by_provider: dict = {}
        self._lazy_names: list[str] = []
        self._current_provider: Optional[str] = None
        self._current_audio = "sub"
        self._stream_task_id: Optional[str] = None
        self._pending_play_number: Any = None
        self._movie = False
        self._download_pending = False

        self._cover_url: Optional[str] = None
        self.banner_active = False
        self._banner_src = ""
        self._banner_pix = None
        self._banner_cache = (0, None)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        top_row = QHBoxLayout()
        top_row.setContentsMargins(0, 0, 0, 8)
        self._back_btn = TransparentToolButton(themed_icon('tv.anime_back', FluentIcon.LEFT_ARROW))
        self._back_btn.setFixedSize(34, 34)
        self._back_btn.clicked.connect(self.back_requested.emit)
        top_row.addWidget(self._back_btn)
        top_row.addStretch(1)
        outer.addLayout(top_row)

        scroll = SmoothScrollArea(self)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QAbstractScrollArea.Shape.NoFrame)
        scroll.setStyleSheet("QScrollArea { background: transparent; border: none; }")
        scroll.viewport().setStyleSheet("background: transparent;")
        outer.addWidget(scroll, 1)
        self._scroll = scroll
        scroll.verticalScrollBar().valueChanged.connect(lambda _v: self.banner_changed.emit())

        content = QWidget()
        content.setStyleSheet("background: transparent;")
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(0, 0, 4, 20)
        content_layout.setSpacing(12)
        scroll.setWidget(content)

        header = QHBoxLayout()
        header.setSpacing(20)

        self._poster = ImageLabel()
        self._poster.setFixedSize(200, 300)
        self._poster.setBorderRadius(8, 8, 8, 8)
        header.addWidget(self._poster, 0)

        info_col = QVBoxLayout()
        info_col.setSpacing(6)

        self._title_lbl = TitleLabel("")
        self._title_lbl.setWordWrap(True)
        info_col.addWidget(self._title_lbl)

        self._meta_lbl = CaptionLabel("")
        self._meta_lbl.setStyleSheet(f'color: {palette()["muted"]};')
        info_col.addWidget(self._meta_lbl)

        self._genre_pills: list[PillPushButton] = []
        self._genres_box = QWidget()
        self._genres_row = QHBoxLayout(self._genres_box)
        self._genres_row.setContentsMargins(0, 0, 0, 0)
        self._genres_row.setSpacing(8)
        self._genres_row.addStretch(1)
        info_col.addWidget(self._genres_box)

        self._synopsis_lbl = BodyLabel("")
        self._synopsis_lbl.setWordWrap(True)
        info_col.addWidget(self._synopsis_lbl, 1)

        info_col.addStretch(1)
        header.addLayout(info_col, 1)

        content_layout.addLayout(header)

        controls_row = QHBoxLayout()
        controls_row.setSpacing(10)

        self._episodes_title = StrongBodyLabel(tr("anime.episodes", default="Episodes"))
        controls_row.addWidget(self._episodes_title)
        controls_row.addStretch(1)

        self._season_combo = ComboBox()
        self._season_combo.setMinimumWidth(120)
        self._season_combo.currentIndexChanged.connect(self._on_season_changed)
        self._season_combo.hide()
        controls_row.addWidget(self._season_combo)

        self._provider_combo = ComboBox()
        self._provider_combo.setMinimumWidth(140)
        self._provider_combo.currentIndexChanged.connect(self._on_provider_changed)
        controls_row.addWidget(self._provider_combo)

        self._audio_combo = ComboBox()
        self._audio_combo.addItem(tr("anime.sub", default="Sub"), userData="sub")
        self._audio_combo.addItem(tr("anime.dub", default="Dub"), userData="dub")
        self._audio_combo.setMinimumWidth(80)
        self._audio_combo.currentIndexChanged.connect(self._on_audio_changed)
        controls_row.addWidget(self._audio_combo)

        self._wl_btn = _WatchLaterButton()
        self._wl_btn.clicked.connect(self._on_toggle_watch_later)
        self._wl_btn.hide()
        controls_row.addWidget(self._wl_btn)

        self._download_btn = PushButton(themed_icon('tv.anime_download', FluentIcon.DOWNLOAD), tr("anime.download", default="Download"))
        self._download_btn.clicked.connect(self._on_download_clicked)
        controls_row.addWidget(self._download_btn)

        content_layout.addLayout(controls_row)

        ep_scroll = self._ep_scroll = SmoothScrollArea(self)
        ep_scroll.setWidgetResizable(True)
        ep_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        ep_scroll.setFixedHeight(190)
        ep_scroll.setStyleSheet("background: transparent; border: none;")
        self._ep_inner = QWidget()
        self._ep_layout = QHBoxLayout(self._ep_inner)
        self._ep_layout.setContentsMargins(2, 2, 2, 2)
        self._ep_layout.setSpacing(10)
        self._ep_layout.addStretch(1)
        ep_scroll.setWidget(self._ep_inner)
        content_layout.addWidget(ep_scroll)

        status_row = QHBoxLayout()
        status_row.setSpacing(8)
        status_row.setContentsMargins(0, 0, 0, 0)
        status_row.setAlignment(Qt.AlignmentFlag.AlignHCenter)

        self._status_spinner = IndeterminateProgressRing()
        self._status_spinner.setFixedSize(18, 18)
        self._status_spinner.setStrokeWidth(3)
        self._status_spinner.hide()
        status_row.addWidget(self._status_spinner)

        self._status_lbl = CaptionLabel("")
        self._status_lbl.setStyleSheet(f'color: {palette()["muted"]};')
        status_row.addWidget(self._status_lbl)

        content_layout.addLayout(status_row)

        self._player_widget = AnimePlayerWidget(content)
        self._player_widget.alt_stream_requested.connect(self._on_alt_stream)
        self._player_widget.stream_failed.connect(self._on_player_stream_failed)
        self._player_widget.ended.connect(self._on_player_ended)
        self._player_widget.next_requested.connect(self._play_next_episode)
        self._player_widget.close_requested.connect(self._close_player)
        self._player_widget.set_closable(True)
        self._play_pool = None
        self._tried_urls: set = set()
        self._player_widget.hide()
        content_layout.addWidget(self._player_widget, 0, Qt.AlignmentFlag.AlignHCenter)

        content_layout.addStretch(1)


    def shutdown(self) -> None:
        try:
            images.release(self)
        except (TypeError, RuntimeError):
            pass
        if self._stream_task_id is not None:
            _worker_module.cancel(self._stream_task_id)
            self._stream_task_id = None
        try:
            self._player_widget.shutdown()
        except Exception:
            pass

    def _set_poster_placeholder(self) -> None:
        pix = QPixmap(200, 300)
        pix.fill(_placeholder_color())
        self._poster.setImage(pix)
        self._poster.setFixedSize(200, 300)

    def show_loading(self, entry: AnimeItem) -> None:
        self.banner_active = True
        self._set_banner("")
        self._now_episode = None
        self._pending_play_number = None
        self._download_pending = False
        self._player_widget.stop()
        self._player_widget.hide()
        self._set_movie(_is_movie(getattr(entry, 'format', None)))
        self._season_combo.hide()

        self._media = None
        self._episodes_by_provider = {}
        self._lazy_names = []
        self._current_provider = None
        self._update_watch_later_btn()

        self._title_lbl.setText(entry.display_title or "")
        self._meta_lbl.setText("")
        self._set_genre_pills([])
        self._synopsis_lbl.setText("")

        self._provider_combo.blockSignals(True)
        self._provider_combo.clear()
        self._provider_combo.blockSignals(False)

        self._clear_episode_cards()

        self._status_lbl.setText(tr("anime.loading_details", default="Loading details..."))

        self._poster.clear()
        self._cover_url = entry.artwork_url or ""
        self._set_poster_placeholder()
        if self._cover_url:
            images.get(self._cover_url, (200, 300), partial(self._on_cover_ready, self._cover_url), owner=self)

    def _close_player(self) -> None:
        if self._stream_task_id is not None:
            _worker_module.cancel(self._stream_task_id)
            self._stream_task_id = None
        self._pending_play_number = None
        self._now_episode = None
        self._play_pool = None
        self._tried_urls = set()
        self._player_widget.release()
        self._player_widget.hide()
        self._status_lbl.setText("")
        self._status_spinner.hide()

    def _set_movie(self, on: bool) -> None:
        self._movie = on
        self._ep_scroll.setVisible(not on)

    def _on_anime_banner(self, anilist_id, url: str) -> None:
        if _qt_is_valid(self) and url and self.banner_active and (self._media or {}).get("id") == anilist_id:
            self._set_banner(url)

    def set_seasons(self, seasons: list, current_id: Any) -> None:
        self._seasons = list(seasons or [])
        try:
            current = int(current_id)
        except (TypeError, ValueError):
            current = None
        idx = next((i for i, s in enumerate(self._seasons)
                    if current in (s.get("ids") or [s.get("id")])), 0)
        self._season_combo.blockSignals(True)
        self._season_combo.clear()
        for n, season in enumerate(self._seasons, 1):
            label = tr("anime.season_n", default="Season {n}").format(n=n)
            if season.get("seasonYear"):
                label += f" \u00b7 {season['seasonYear']}"
            self._season_combo.addItem(label, userData=season["id"])
        if self._seasons:
            self._season_combo.setCurrentIndex(idx)
        self._season_combo.blockSignals(False)
        self._season_combo.setVisible(len(self._seasons) > 1)

    def _on_season_changed(self, idx: int) -> None:
        if not 0 <= idx < len(self._seasons):
            return
        season = self._seasons[idx]
        if (self._media or {}).get("id") in (season.get("ids") or []):
            return
        self.season_requested.emit(season["id"])

    def begin_season_switch(self) -> None:
        self._close_player()
        self._now_episode = None
        self._pending_play_number = None
        self._media = None
        self._episodes_by_provider = {}
        self._current_provider = None
        self._provider_combo.blockSignals(True)
        self._provider_combo.clear()
        self._provider_combo.blockSignals(False)
        self._clear_episode_cards()

    def show_detail(self, media: dict, episodes_by_provider: dict) -> None:
        self.banner_active = True
        banner = str(media.get("bannerImage") or "")
        self._set_banner(banner)
        if not banner and media.get("id"):
            aid = media["id"]
            tmdb.anime_banner_async(aid, on_done=lambda url, a=aid: self._on_anime_banner(a, url))
        self._now_episode = None
        if _is_movie(media.get('format')):
            self._set_movie(True)
        self._media = media
        self._episodes_by_provider = episodes_by_provider or {}
        self._update_watch_later_btn()
        QTimer.singleShot(0, self._consume_pending_play)

        title = media.get("title") or {}
        self._title_lbl.setText(title.get("english") or title.get("romaji") or "")

        meta_parts = []
        if media.get("seasonYear"):
            meta_parts.append(str(media["seasonYear"]))
        if media.get("status"):
            meta_parts.append(str(media["status"]))
        self._meta_lbl.setText(" · ".join(meta_parts))

        genres = media.get("genres") or []
        self._set_genre_pills(genres)

        self._synopsis_lbl.setText(
            anime_backend.clean_description(media.get("description"))
            or tr("anime.no_synopsis", default="No synopsis available."))

        self._poster.clear()
        self._cover_url = media.get("coverImage") or ""
        if self._cover_url:
            self._set_poster_placeholder()
            images.get(self._cover_url, (200, 300), partial(self._on_cover_ready, self._cover_url), owner=self)
        else:
            self._set_poster_placeholder()

    def _watch_later_entry(self) -> dict:
        media = self._media or {}
        title = media.get("title") or {}
        score = media.get("averageScore")
        return {
            "media_type": "anime",
            "id": media.get("id"),
            "title": title.get("english") or title.get("romaji") or self._title_lbl.text(),
            "year": str(media.get("seasonYear") or ""),
            "rating": (score / 10) if score else None,
            "poster_url": media.get("coverImage") or self._cover_url,
            "raw": media,
        }

    def _update_watch_later_btn(self) -> None:
        media_id = (self._media or {}).get("id")
        if media_id is None:
            self._wl_btn.hide()
            return
        self._wl_btn.show()
        saved = watch_later_store().has("anime", media_id)
        self._wl_btn.set_state(saved, tr("tv.watchlater_saved", default="In watch later") if saved
                               else tr("tv.watchlater_add", default="Watch later"))

    def _on_toggle_watch_later(self) -> None:
        media_id = (self._media or {}).get("id")
        if media_id is None:
            return
        store = watch_later_store()
        if store.has("anime", media_id):
            store.remove("anime", media_id)
        else:
            store.add(self._watch_later_entry())
        self._update_watch_later_btn()

    def set_episodes_loading(self) -> None:
        self._status_lbl.setText("")
        self._status_spinner.show()

    def set_episodes_error(self, msg: str) -> None:
        self._status_spinner.hide()
        self._status_lbl.setText(tr("anime.episodes_error", default="Failed to load episodes."))
        logger.error("Episode load error: %s", msg)

    def set_episodes(self, episodes_by_provider: dict, provider_names: Optional[list] = None) -> None:
        self._status_spinner.hide()
        self._episodes_by_provider = episodes_by_provider or {}
        self._lazy_names = list(provider_names or [])

        pending = self._episodes_by_provider.get("notAired")
        if pending is not None:
            self._show_not_aired(pending or {})
            return

        _non_provider_keys = {"page", "type", "mappings", "_unknownProviders", "notAired"}
        provider_names = sorted(
            name for name, data in self._episodes_by_provider.items()
            if name not in _non_provider_keys
            and isinstance(data, dict) and not data.get("error")
            and isinstance(data.get("episodes"), dict) and data["episodes"].get("sub")
        )
        if self._lazy_names:
            provider_names = list(self._lazy_names)
        if _DEFAULT_PROVIDER in provider_names:
            provider_names.remove(_DEFAULT_PROVIDER)
            provider_names.insert(0, _DEFAULT_PROVIDER)

        self._provider_combo.blockSignals(True)
        self._provider_combo.clear()
        for name in provider_names:
            self._provider_combo.addItem(name, userData=name)
        self._provider_combo.blockSignals(False)

        if provider_names:
            self._provider_combo.setCurrentIndex(0)
            self._current_provider = provider_names[0]
        else:
            self._current_provider = None
            self._status_lbl.setText(tr("anime.no_providers", default="No providers returned episodes for this title."))

        self._current_audio = "sub"
        self._update_audio_options()

        self._render_episodes()
        QTimer.singleShot(0, self._consume_pending_play)
        if self._download_pending:
            self._download_pending = False
            QTimer.singleShot(0, self._on_download_clicked)

    def _show_not_aired(self, pending: dict) -> None:
        self._provider_combo.blockSignals(True)
        self._provider_combo.clear()
        self._provider_combo.blockSignals(False)
        self._current_provider = None
        self._current_audio = "sub"
        self._update_audio_options()
        self._render_episodes()
        self._status_lbl.setText(_not_aired_label(pending))

    def _on_cover_ready(self, url: str, pixmap) -> None:
        if url != self._cover_url:
            return
        if pixmap.isNull():
            self._set_poster_placeholder()
            return
        self._poster.setImage(pixmap)
        self._poster.setFixedSize(200, 300)

    def _update_audio_options(self) -> None:
        provider_data = self._episodes_by_provider.get(self._current_provider) or {}
        provider_episodes = provider_data.get("episodes") or {}
        has_sub = bool(provider_episodes.get("sub"))
        has_dub = bool(provider_episodes.get("dub"))

        self._audio_combo.blockSignals(True)
        self._audio_combo.clear()
        if has_sub:
            self._audio_combo.addItem(tr("anime.sub", default="Sub"), userData="sub")
        if has_dub:
            self._audio_combo.addItem(tr("anime.dub", default="Dub"), userData="dub")
        self._audio_combo.setVisible(has_sub and has_dub)

        if self._current_audio == "dub" and has_dub:
            self._audio_combo.setCurrentIndex(self._audio_combo.findData("dub"))
        elif has_sub:
            self._audio_combo.setCurrentIndex(self._audio_combo.findData("sub"))
            self._current_audio = "sub"
        elif has_dub:
            self._audio_combo.setCurrentIndex(self._audio_combo.findData("dub"))
            self._current_audio = "dub"
        self._audio_combo.blockSignals(False)

    def _on_provider_changed(self, _index: int) -> None:
        self._current_provider = self._provider_combo.currentData()
        if self._lazy_names and self._current_provider not in self._episodes_by_provider:
            self._clear_episode_cards()
            self._status_lbl.setText("")
            self._status_spinner.show()
            self.provider_requested.emit(self._current_provider)
            return
        self._update_audio_options()
        self._render_episodes()
        self._resume_on_switch()

    def set_provider_episodes(self, provider: str, data: dict) -> None:
        self._episodes_by_provider[provider] = data or {}
        if provider != self._current_provider:
            return
        self._status_spinner.hide()
        self._update_audio_options()
        self._render_episodes()
        QTimer.singleShot(0, self._consume_pending_play)
        if self._download_pending:
            self._download_pending = False
            QTimer.singleShot(0, self._on_download_clicked)

    def set_provider_error(self, provider: str, msg: str) -> None:
        if provider != self._current_provider:
            return
        self._status_spinner.hide()
        self._status_lbl.setText(tr("anime.episodes_error", default="Failed to load episodes."))
        logger.error("Episode load error (%s): %s", provider, msg)

    def _on_audio_changed(self, _index: int) -> None:
        self._current_audio = self._audio_combo.currentData() or "sub"
        self._render_episodes()
        self._resume_on_switch()

    def _resume_on_switch(self) -> None:
        ep = getattr(self, "_now_episode", None)
        if ep is None or not self._player_widget.has_media() or self._stream_task_id is not None:
            return
        data = self._episodes_by_provider.get(self._current_provider) or {}
        eps = (data.get("episodes") or {}).get(self._current_audio) or []
        if not any(e.get("number") == ep.get("number") for e in eps):
            return
        self._watch_episode(ep, resume_ms=self._player_widget.position_ms())

    def _on_download_clicked(self, preset_episode: Any = None) -> None:
        if not self._media or not self._episodes_by_provider:
            self._download_pending = True
            return
        dlg = AnimeDownloadDialog(self._episodes_by_provider, self._current_provider, self._current_audio,
                                  parent=self.window(), preset_episode=preset_episode)
        if not dlg.exec():
            return
        provider, audio, episodes, quality, subtitle_lang = dlg.selected()
        if not provider or not episodes:
            return
        self.download_requested.emit(self._media, provider, audio, episodes, quality, subtitle_lang)

    def start_download(self) -> None:
        self._on_download_clicked()


    def _episode_watch_later_entry(self, episode: dict) -> dict:
        entry = self._watch_later_entry()
        number = _episode_key_number(episode.get("number"))
        if number is not None:
            entry["episode"] = number
        entry["episode_title"] = episode.get("title") or ""
        still = next((episode[k] for k in _EP_IMG_KEYS if isinstance(episode.get(k), str) and episode.get(k)), "")
        if still:
            entry["still_url"] = still
        return entry

    def _toggle_episode_watch_later(self, episode: dict) -> None:
        media_id = (self._media or {}).get("id")
        if media_id is None:
            return
        store = watch_later_store()
        number = _episode_key_number(episode.get("number"))
        if number is None:
            return
        if store.has("anime", media_id, None, number):
            store.remove("anime", media_id, None, number)
        else:
            store.add(self._episode_watch_later_entry(episode))

    def _on_episode_menu(self, episode: dict, pos) -> None:
        media_id = (self._media or {}).get("id")
        if media_id is None:
            return
        number = _episode_key_number(episode.get("number"))
        saved = number is not None and watch_later_store().has("anime", media_id, None, number)
        menu = RoundMenu(parent=self)
        menu.addAction(Action(_play_icon(), tr("tv.stream.play", default="Play"),
                              triggered=lambda: self._watch_episode(episode)))
        menu.addAction(Action(themed_icon('tv.watchlater', FluentIcon.HEART),
                              tr("tv.watchlater_episode_remove", default="Remove episode from watch later") if saved
                              else tr("tv.watchlater_episode_add", default="Add episode to watch later"),
                              triggered=lambda: self._toggle_episode_watch_later(episode)))
        menu.addSeparator()
        menu.addAction(Action(FluentIcon.DOWNLOAD, tr("tv.download.episode", default="Download this episode..."),
                              triggered=lambda: self._on_download_clicked(preset_episode=episode.get("number"))))
        menu.exec(pos)

    def play_episode(self, number: Any) -> None:
        self._pending_play_number = _episode_key_number(number)
        if self._pending_play_number is not None:
            self._consume_pending_play()

    def _consume_pending_play(self) -> None:
        number = self._pending_play_number
        if number is None or self._media is None or not self._current_provider:
            return
        data = self._episodes_by_provider.get(self._current_provider) or {}
        episodes = data.get("episodes")
        episodes = episodes.get(self._current_audio, []) if isinstance(episodes, dict) else []
        target = next((ep for ep in episodes if _episode_key_number(ep.get("number")) == number), None)
        self._pending_play_number = None
        if target is not None:
            self._watch_episode(target)

    def _clear_episode_cards(self) -> None:
        self._render_gen = getattr(self, "_render_gen", 0) + 1
        self._ep_inner.setUpdatesEnabled(False)
        while self._ep_layout.count() > 1:
            w = self._ep_layout.takeAt(0).widget()
            if w:
                w.hide()
                w.deleteLater()
        self._ep_inner.setUpdatesEnabled(True)

    def _render_episodes(self) -> None:
        self._clear_episode_cards()
        if not self._current_provider:
            return
        data = self._episodes_by_provider.get(self._current_provider) or {}
        eps = data.get("episodes")
        eps = eps.get(self._current_audio, []) if isinstance(eps, dict) else []
        if not eps:
            self._status_lbl.setText(tr("anime.no_episodes_audio", default="No {audio} episodes from this provider.").format(audio=self._current_audio))
            return
        self._status_lbl.setText("")
        if self._movie:
            single = len(eps) == 1
            self._ep_scroll.setVisible(not single)
            if single:
                if not self._player_widget.has_media() and self._stream_task_id is None:
                    QTimer.singleShot(0, partial(self._watch_episode, eps[0]))
                return
        media = self._media or {}
        self._ep_fallback = media.get("bannerImage") or media.get("coverImage") or ""
        self._add_episode_chunk(eps, 0, self._render_gen)

    def _add_episode_chunk(self, episodes: list, start: int, gen: int) -> None:
        if gen != self._render_gen:
            return
        end = min(start + 12, len(episodes))
        self._ep_inner.setUpdatesEnabled(False)
        for ep in episodes[start:end]:
            card = _AnimeEpisodeCard(ep, self._ep_fallback, self._watch_episode)
            card.context_requested.connect(self._on_episode_menu)
            self._ep_layout.insertWidget(self._ep_layout.count() - 1, card)
        self._ep_inner.setUpdatesEnabled(True)
        if end < len(episodes):
            QTimer.singleShot(0, partial(self._add_episode_chunk, episodes, end, gen))

    def _watch_episode(self, episode: dict, resume_ms: int = 0) -> None:
        if self._media is None or not self._current_provider:
            return

        if self._stream_task_id is not None:
            return

        self._status_lbl.setText(tr("anime.loading_stream", default="Loading stream..."))

        self._now_episode = episode
        self._player_widget.set_has_next(self._next_episode() is not None)

        def _on_done(data: dict) -> None:
            self._stream_task_id = None
            self._tried_urls = set()
            self._on_stream_ready(data, resume_ms)

        def _on_error(msg: str) -> None:
            self._stream_task_id = None
            self._on_stream_error(msg)

        self._stream_task_id = _worker_module.submit_coro(
            _load_stream,
            args=(self._current_provider, episode.get("anilistId") or self._media["id"], self._current_audio,
                  episode.get("sourceNumber", episode.get("number"))),
            on_done=_on_done,
            on_error=_on_error,
        )

    def _on_stream_ready(self, data: dict, resume_ms: int = 0) -> None:
        self._status_lbl.setText("")
        streams = data.get("streams") or []


        active = next((s for s in streams if s.get("type") in ("hls", "mp4") and s.get("isActive")), None)
        if active is None:
            active = next((s for s in streams if s.get("type") in ("hls", "mp4")), None)
        if active is None:
            active = next((s for s in streams if s.get("isActive")), None) or (streams[0] if streams else None)
        if not active:
            InfoBar.error(
                title=tr("anime.playback_error", default="Playback error"),
                content=tr("anime.no_stream_found", default="No playable stream was returned."),
                parent=self.window(), duration=3000, isClosable=True,
            )
            return

        url = active.get("url", "")
        if not url:
            return

        if active.get("type") == "embed":
            InfoBar.warning(
                title=tr("anime.embed_only", default="Embed-only source"),
                content=tr(
                    "anime.embed_only_msg",
                    default="This provider only returned an embed page, which can't be played in-app. Opening in browser instead.",
                ),
                parent=self.window(), duration=4000, isClosable=True,
            )
            QDesktopServices.openUrl(QUrl(url))
            return

        title = data.get("title") or ""

        subtitles = active.get("subtitles")
        if not subtitles and active.get("subtitle"):
            sub_val = active["subtitle"]
            if isinstance(sub_val, str):
                subtitles = [{"url": sub_val, "label": tr("anime.english", default="English"), "default": True}]
            elif isinstance(sub_val, dict) and sub_val.get("url"):
                subtitles = [sub_val]
        subtitles = [s for s in (subtitles or []) if isinstance(s, dict) and s.get("url")]

        pool = [s for s in streams if s.get("type") in ("hls", "mp4") and s.get("url")]
        self._play_pool = (pool, title, subtitles)
        self._start_play(active, resume_ms)

    def _next_episode(self) -> Optional[dict]:
        ep = getattr(self, "_now_episode", None)
        if ep is None:
            return None
        data = self._episodes_by_provider.get(self._current_provider) or {}
        eps = (data.get("episodes") or {}).get(self._current_audio) or []
        for i, e in enumerate(eps):
            if e.get("number") == ep.get("number"):
                return eps[i + 1] if i + 1 < len(eps) else None
        return None

    def _play_next_episode(self) -> None:
        nxt = self._next_episode()
        if nxt is not None:
            self._watch_episode(nxt)

    def _on_player_ended(self) -> None:
        if self._next_episode() is None:
            return
        if getattr(settings, "tv_autonext", False):
            self._play_next_episode()
        else:
            self._player_widget.prompt_next()

    def _load_skip(self, episode: Optional[dict]) -> None:
        self._player_widget.set_skip_segments()
        media = self._media or {}
        aid, number = media.get("id"), (episode or {}).get("number")
        try:
            number = int(float(number))
        except (TypeError, ValueError):
            return
        if aid is None:
            return
        self._skip_gen = getattr(self, "_skip_gen", 0) + 1
        gen = self._skip_gen

        fallback = dict((self._episodes_by_provider or {}).get("mappings") or {})

        async def _job() -> dict:
            return await asyncio.to_thread(tmdb.anime_skip_segments, aid, number, fallback)

        def _done(data: dict) -> None:
            if gen == self._skip_gen and data:
                self._player_widget.set_skip_segments(data.get("intro"), data.get("credits"))
        _worker_module.submit_coro(_job, args=(), on_done=_done, on_error=lambda *_a: None)

    def _on_player_stream_failed(self, msg: str) -> None:
        pool = (self._play_pool or ([], "", []))[0]
        nxt = next((s for s in pool if s.get("url") and s["url"] not in self._tried_urls), None)
        if nxt is None:
            self._on_stream_error(msg or tr("anime.playback_error", default="Playback error"))
            return
        self._start_play(nxt, self._player_widget.position_ms())

    def _start_play(self, active: dict, start_ms: int) -> None:
        self._tried_urls.add(active.get("url"))
        pool, title, subtitles = self._play_pool
        alts = []
        seen = {str(active.get("quality") or "")}
        for st in pool:
            q = str(st.get("quality") or "")
            if st is active or not q or q in seen:
                continue
            seen.add(q)
            alts.append((q, st))
        alts.sort(key=lambda a: -(_stream_quality_value(a[1]) or 0))
        subs = [s for s in (active.get("subtitles") or subtitles) if isinstance(s, dict) and s.get("url")] or subtitles
        self._player_widget.play_stream(active["url"], referer=active.get("referer"), headers=active.get("headers"),
                                        title=title, subtitles=subs, start_ms=start_ms,
                                        alt_streams=alts if len(alts) > 0 else None,
                                        quality_label=str(active.get("quality") or ""))
        self._player_widget.show()
        self._player_widget.setFocus()
        self._load_skip(self._now_episode)

    def _on_alt_stream(self, stream: dict) -> None:
        if getattr(self, "_play_pool", None):
            self._start_play(stream, self._player_widget.position_ms())

    def _on_stream_error(self, msg: str) -> None:
        self._status_lbl.setText(msg)
        InfoBar.error(
            title=tr("anime.playback_error", default="Playback error"),
            content=msg,
            parent=self.window(), duration=3000, isClosable=True,
        )

for _name in ("banner_for", "banner_offset", "_set_banner"):
    setattr(AnimeDetailView, _name, getattr(_MediaDetailsView, _name))


class AnimePage(_MediaTab):
    anime_selected = Signal(object)
    _wrap = staticmethod(_AnimeCard)
    _SEARCH_PH = ("anime.search_placeholder", "Search anime...")

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__('anime', _ANIME_CATEGORIES, parent)
        self._detail_task_id: Optional[str] = None
        self._episodes_task_id: Optional[str] = None
        self._lazy_id: Any = None
        self._lazy_names: list[str] = []
        self._lazy_data: dict = {}
        self._lazy_tasks: dict[str, str] = {}
        self._download_manager = None
        self._detail_view = self._details

    def _create_details(self) -> QWidget:
        view = AnimeDetailView()
        view.back_requested.connect(self._show_grid)
        view.download_requested.connect(self._on_download_requested)
        view.provider_requested.connect(self._on_provider_requested)
        view.season_requested.connect(self._on_season_requested)
        return view

    def _start_category(self, key: str, on_done, on_error) -> None:
        _worker_module.submit_coro(_browse_anime, args=(key,), on_done=on_done, on_error=on_error)

    def _start_search(self, query: str, on_done, on_error):
        return _worker_module.submit_coro(_search_anime, args=(query,), on_done=on_done, on_error=on_error)

    def _open_details(self, item, media_type: str = "anime") -> None:
        self._on_anime_clicked(item)

    def _show_grid(self) -> None:
        self._detail_view.banner_active = False
        self._detail_view.banner_changed.emit()
        self._switch_stack_animated(self._browse_page)

    def set_download_manager(self, manager) -> None:
        self._download_manager = manager

    def _on_download_requested(self, media: dict, provider: str, audio: str, episodes: list, quality: str, subtitle_lang: str) -> None:
        if self._download_manager is None:
            return
        anilist_id = media.get("id")
        title_obj = media.get("title") or {}
        title = title_obj.get("english") or title_obj.get("romaji") or "Anime"
        dest_dir = os.path.join(settings.download_dir_anime, _sanitize_filename(title))

        for ep in episodes:
            label = _episode_number_label(ep.get("number"))
            item_id = self._download_manager.add_external(
                game_name=f"{title} - {label}", console=title, source=provider, category="anime",
            )

            def _on_done(path: str, iid=item_id) -> None:
                self._download_manager.complete_external(iid, path)

            def _on_err(msg: str, iid=item_id) -> None:
                self._download_manager.fail_external(iid, msg)

            task_id = _worker_module.submit(
                _do_download_anime_episode,
                args=(self._download_manager, item_id, anilist_id, provider, audio, ep, dest_dir),
                kwargs={"quality": quality, "subtitle_lang": subtitle_lang},
                on_done=_on_done, on_error=_on_err,
            )
            self._download_manager.register_external_cancel(item_id, lambda tid=task_id: _worker_module.cancel(tid))

        InfoBar.success(
            title=tr("anime.download_queued_title", default="Download queued"),
            content=tr("anime.download_queued_content", count=len(episodes)),
            parent=self.window(), duration=2500, isClosable=True,
        )

    def open_detail(self, media: dict) -> None:
        anilist_id = (media or {}).get("id")
        if not anilist_id:
            return

        if not media.get("description") and not media.get("genres"):
            self._on_anime_clicked(AnimeItem.from_anilist_media(media))
            return

        if self._detail_task_id is not None:
            _worker_module.cancel(self._detail_task_id)
            self._detail_task_id = None

        self._detail_view.show_detail(media, {})
        self._switch_stack_animated(self._detail_view)
        self._start_episode_load(anilist_id)

    def play_episode(self, number) -> None:
        self._detail_view.play_episode(number)

    def _on_card_menu(self, item, pos) -> None:
        card = getattr(item, 'raw', None)
        media_id = _safe_int(getattr(card, 'id', None))
        if card is None or media_id is None:
            return
        menu = RoundMenu(parent=self)
        menu.addAction(Action(FluentIcon.VIEW, tr('tv.watchlater_open', default='Open'),
                              triggered=lambda: self._open_details(card, 'anime')))
        if self._download_manager is not None:
            menu.addAction(Action(FluentIcon.DOWNLOAD, tr('tv.download.action', default='Download...'),
                                  triggered=lambda: self._open_anime_and_download(card)))
        saved = watch_later_store().has('anime', media_id)
        menu.addAction(Action(themed_icon('tv.watchlater', FluentIcon.HEART),
                              tr('tv.watchlater_remove', default='Remove from watch later') if saved
                              else tr('tv.watchlater_add', default='Watch later'),
                              triggered=lambda: self._toggle_anime_card_watch_later(card)))
        menu.exec(pos)

    def _toggle_anime_card_watch_later(self, card) -> None:
        media_id = _safe_int(getattr(card, 'id', None))
        if media_id is None:
            return
        store = watch_later_store()
        if store.has('anime', media_id):
            store.remove('anime', media_id)
        else:
            store.add({
                'media_type': 'anime',
                'id': media_id,
                'title': getattr(card, 'title', '') or '',
                'year': str(getattr(card, 'season_year', '') or ''),
                'poster_url': getattr(card, 'artwork_url', None),
                'raw': _anime_card_raw(card),
            })

    def _open_anime_and_download(self, card) -> None:
        self._open_details(card, 'anime')
        self._detail_view.start_download()

    def _on_anime_clicked(self, result: AnimeItem) -> None:
        anilist_id = result.id
        if not anilist_id:
            return

        if self._detail_task_id is not None:
            _worker_module.cancel(self._detail_task_id)
            self._detail_task_id = None

        self._detail_view.show_loading(result)
        self._switch_stack_animated(self._detail_view)
        self.anime_selected.emit(result)

        self._load_media_into_view(anilist_id)

        def _seasons_ok(seasons: list) -> None:
            if self._lazy_id == anilist_id or self._detail_view._media is None:
                self._detail_view.set_seasons(seasons or [], anilist_id)

        _worker_module.submit_coro(
            _load_anime_seasons, args=(anilist_id,), on_done=_seasons_ok, on_error=lambda _m: None,
        )

    def _load_media_into_view(self, anilist_id: Any) -> None:
        def _ok(media: dict) -> None:
            self._detail_task_id = None
            self._detail_view.show_detail(media, {})
            deferred, self._deferred_eps = getattr(self, "_deferred_eps", None), None
            if deferred is not None:
                self._detail_view.set_episodes(deferred, self._lazy_names or None)
            elif self._episodes_task_id is not None:
                self._detail_view.set_episodes_loading()

        def _err(msg: str) -> None:
            self._detail_task_id = None
            InfoBar.error(
                title=tr("anime.error", default="Error"),
                content=msg,
                parent=self.window(), duration=3000, isClosable=True,
            )

        self._deferred_eps = None
        self._start_episode_load(anilist_id)
        self._detail_task_id = _worker_module.submit_coro(
            _load_anime_media, args=(anilist_id,), on_done=_ok, on_error=_err,
        )

    def _on_season_requested(self, season_id: Any) -> None:
        if self._detail_task_id is not None:
            _worker_module.cancel(self._detail_task_id)
            self._detail_task_id = None
        self._detail_view.begin_season_switch()
        self._load_media_into_view(season_id)

    def _cancel_lazy_tasks(self) -> None:
        for tid in self._lazy_tasks.values():
            _worker_module.cancel(tid)
        self._lazy_tasks.clear()

    def _start_episode_load(self, anilist_id: Any) -> None:
        if self._episodes_task_id is not None:
            _worker_module.cancel(self._episodes_task_id)
            self._episodes_task_id = None
        self._cancel_lazy_tasks()

        self._detail_view.set_episodes_loading()
        self._lazy_id = anilist_id
        self._lazy_data = {}
        self._lazy_names = _lazy_provider_names()

        if self._lazy_names:
            default = self._lazy_names[0]

            def _on_default(data: dict) -> None:
                self._episodes_task_id = None
                if anilist_id != self._lazy_id:
                    return
                if isinstance(data, dict) and "notAired" in data:
                    self._lazy_names = []
                    payload = data
                else:
                    self._lazy_data[default] = data or {}
                    payload = self._lazy_data
                if self._detail_task_id is not None:
                    self._deferred_eps = payload
                    return
                self._detail_view.set_episodes(payload, self._lazy_names or None)

            def _on_default_err(msg: str) -> None:
                self._episodes_task_id = None
                self._detail_view.set_episodes_error(msg)

            self._episodes_task_id = _worker_module.submit_coro(
                _load_anime_provider_episodes, args=(anilist_id, default),
                on_done=_on_default, on_error=_on_default_err,
            )
            return

        def _on_eps_loaded(episodes_by_provider: dict) -> None:
            self._episodes_task_id = None
            if self._detail_task_id is not None:
                self._deferred_eps = episodes_by_provider
                return
            self._detail_view.set_episodes(episodes_by_provider)

        def _on_eps_err(msg: str) -> None:
            self._episodes_task_id = None
            self._detail_view.set_episodes_error(msg)

        self._episodes_task_id = _worker_module.submit_coro(
            _load_anime_episodes, args=(anilist_id,), on_done=_on_eps_loaded, on_error=_on_eps_err,
        )

    def _on_provider_requested(self, provider: str) -> None:
        anilist_id = self._lazy_id
        if anilist_id is None or provider in self._lazy_tasks:
            return
        if provider in self._lazy_data:
            self._detail_view.set_provider_episodes(provider, self._lazy_data[provider])
            return

        def _done(data: dict) -> None:
            self._lazy_tasks.pop(provider, None)
            if anilist_id != self._lazy_id:
                return
            self._lazy_data[provider] = data or {}
            self._detail_view.set_provider_episodes(provider, self._lazy_data[provider])

        def _err(msg: str) -> None:
            self._lazy_tasks.pop(provider, None)
            if anilist_id == self._lazy_id:
                self._detail_view.set_provider_error(provider, msg)

        self._lazy_tasks[provider] = _worker_module.submit_coro(
            _load_anime_provider_episodes, args=(anilist_id, provider), on_done=_done, on_error=_err,
        )

    def shutdown(self) -> None:
        self._cancel_lazy_tasks()
        for attr_name in ("_detail_task_id", "_episodes_task_id"):
            task_id = getattr(self, attr_name, None)
            setattr(self, attr_name, None)
            if task_id is not None:
                _worker_module.cancel(task_id)
        super().shutdown()


class _WatchLaterItem:
    __slots__ = ('entry', 'key', 'id', 'title', 'poster_url', 'subtitle')

    def __init__(self, entry: dict):
        self.entry = entry
        self.id = entry.get('id')
        self.key = watch_later_store().entry_key(entry)
        title = entry.get('title') or ''
        season, episode = entry.get('season'), entry.get('episode')
        if episode is not None:
            tag = _episode_tag(season, episode)
            title = f"{title} \u00b7 {tag}" if title else tag
        year = str(entry.get('year') or '')
        self.title = f"{title} ({year})" if year else title
        self.poster_url = entry.get('poster_url')
        kind = entry.get('media_type')
        parts = [tr('tv.watchlater_movie', default='Movie') if kind == 'movie' else
                 tr('tv.watchlater_series', default='Series') if kind == 'tv' else
                 tr('tv.anime', default='Anime')]
        if episode is not None:
            if entry.get('episode_title'):
                parts.append(str(entry['episode_title']))
        elif year:
            parts.append(year)
        rating = entry.get('rating')
        if rating:
            parts.append(f"\u2605 {float(rating):.1f}")
        self.subtitle = " \u00b7 ".join(parts)


class _WatchLaterView(QWidget):
    open_requested = Signal(object)
    back_requested = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(10)

        header = QHBoxLayout()
        self._back_btn = PushButton(themed_icon('tv.media_back', FluentIcon.LEFT_ARROW),
                                    tr('tv.movies.back', default='Back'), self)
        self._back_btn.setFixedHeight(32)
        self._back_btn.clicked.connect(self.back_requested.emit)
        header.addWidget(self._back_btn)
        self._title_lbl = TitleLabel(tr('tv.watchlater', default='Watch later'), self)
        header.addWidget(self._title_lbl)
        self._count_lbl = CaptionLabel('', self)
        apply_label_theme(self._count_lbl, 'muted')
        header.addWidget(self._count_lbl, 0, Qt.AlignmentFlag.AlignBottom)
        header.addStretch(1)
        outer.addLayout(header)

        self._grid = PosterGrid(self, cover_attrs=('poster_url',), key_attr='key', show_caption=True,
                                subtitle_fn=attrs_subtitle('subtitle'), colors_fn=_grid_colors)
        self._grid.poster_clicked.connect(lambda item: self.open_requested.emit(item.entry))
        self._grid.context_requested.connect(self._show_menu)
        outer.addWidget(self._grid, 1)

        self._empty_lbl = BodyLabel(tr('tv.watchlater_empty',
                                       default='Nothing saved yet. Use the watch later button on a movie, show, or anime.'), self)
        self._empty_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._empty_lbl.setWordWrap(True)
        apply_label_theme(self._empty_lbl, 'muted')
        outer.addWidget(self._empty_lbl, 1)

        self._dirty = True
        watch_later_store().changed.connect(self._on_store_changed)
        self.refresh()

    def activate(self) -> None:
        if self._dirty:
            self.refresh()

    def _on_store_changed(self) -> None:
        self._dirty = True
        if self.isVisible():
            self.refresh()

    def refresh(self) -> None:
        self._dirty = False
        entries = watch_later_store().all()
        self._grid.set_entries([_WatchLaterItem(entry) for entry in entries])
        self._count_lbl.setText(str(len(entries)))
        self._empty_lbl.setVisible(not entries)
        self._grid.setVisible(bool(entries))

    def _show_menu(self, item, pos: QPoint) -> None:
        entry = getattr(item, 'entry', None)
        if not isinstance(entry, dict):
            return
        menu = RoundMenu(parent=self)
        menu.addAction(Action(FluentIcon.VIEW, tr('tv.watchlater_open', default='Open'),
                              triggered=lambda: self.open_requested.emit(entry)))
        menu.addAction(Action(FluentIcon.DELETE, tr('tv.watchlater_remove', default='Remove from watch later'),
                              triggered=lambda: self._remove(entry)))
        menu.exec(pos)

    def _remove(self, entry: dict) -> None:
        watch_later_store().remove(entry.get('media_type'), entry.get('id'),
                                   entry.get('season'), entry.get('episode'))
        self.refresh()

    def apply_locale(self) -> None:
        self._back_btn.setText(tr('tv.movies.back', default='Back'))
        self._title_lbl.setText(tr('tv.watchlater', default='Watch later'))
        self._empty_lbl.setText(tr('tv.watchlater_empty',
                                   default='Nothing saved yet. Use the watch later button on a movie, show, or anime.'))
        self.refresh()

    def shutdown(self) -> None:
        try:
            images.release(self)
        except (TypeError, RuntimeError):
            pass


class TV_page(QWidget):
    _TABS = (
        ('movies', FluentIcon.MOVIE, 'tv.movies'),
        ('series', FluentIcon.VIDEO, 'tv.series'),
        ('anime', FluentIcon.VIDEO, 'tv.anime'),
        ('channels', FluentIcon.TILES, 'tv.channels'),
    )

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName('tvPage')
        self.setStyleSheet('background: transparent;')
        self._download_manager: Any = None
        outer = QVBoxLayout(self)
        outer.setContentsMargins(24, 12, 24, 20)
        outer.setSpacing(12)

        self._pivot = Pivot()
        outer.addWidget(self._pivot)

        self._stack = QStackedWidget()
        outer.addWidget(self._stack, 1)

        self._tabs: dict[str, QWidget] = {}
        self._tab_slots: dict[str, QWidget] = {}
        self._tab_icons: dict[str, Any] = {}
        self._tab_text_keys: dict[str, str] = {}
        for key, icon, text_key in self._TABS:
            self._tab_icons[key] = icon
            self._tab_text_keys[key] = text_key
            slot = QWidget(self)
            self._tab_slots[key] = slot
            self._stack.addWidget(slot)

        self._rebuild_tab_selector()
        from src.core.config import tab_visibility_bus as _tabvis_bus
        _tabvis_bus.changed.connect(self._on_tab_visibility_changed)

        register_locale_refresh(self, self._apply_locale)

    def _build_tab(self, key: str) -> QWidget:
        if key == 'channels':
            return _ChannelsTab(self)
        if key == 'movies':
            return _MediaTab('movie', _CATEGORIES, self)
        if key == 'series':
            return _MediaTab('tv', _SERIES_CATEGORIES, self)
        if key == 'anime':
            return AnimePage(self)
        return _TVTabPlaceholder(themed_icon(f'tv.tab_{key}', self._tab_icons[key]),
                                 self._tab_text_keys[key], self)

    def paintEvent(self, event) -> None:
        super().paintEvent(event)
        details = getattr(self._stack.currentWidget(), '_details', None)
        if details is None or not getattr(details, 'banner_active', False):
            return
        pix = details.banner_for(self.width())
        if pix is not None:
            QPainter(self).drawPixmap(0, -details.banner_offset(), pix)

    def _ensure_tab(self, key: str) -> QWidget | None:
        tab = self._tabs.get(key)
        if tab is not None:
            return tab
        if key not in self._tab_text_keys:
            return None
        tab = self._build_tab(key)
        slot = self._tab_slots.pop(key, None)
        if slot is not None:
            index = self._stack.indexOf(slot)
            self._stack.insertWidget(index, tab)
            self._stack.removeWidget(slot)
            slot.deleteLater()
        else:
            self._stack.addWidget(tab)
        self._tabs[key] = tab
        details = getattr(tab, '_details', None)
        if details is not None and hasattr(details, 'banner_changed'):
            details.banner_changed.connect(self.update)
        if self._download_manager is not None and hasattr(tab, 'set_download_manager'):
            tab.set_download_manager(self._download_manager)
        return tab

    def set_download_manager(self, manager) -> None:
        self._download_manager = manager
        for key in ('movies', 'series', 'anime'):
            tab = self._tabs.get(key)
            if tab is not None and hasattr(tab, 'set_download_manager'):
                tab.set_download_manager(manager)

    def _rebuild_tab_selector(self) -> None:
        from src.core.config import enabled_tab_keys
        enabled = [k for k, _i, _t in self._TABS if k in enabled_tab_keys('tv')]
        if not enabled:
            enabled = [k for k, _i, _t in self._TABS]
        current = getattr(self, '_current_tab_key', None)
        if current not in enabled:
            current = enabled[0]
        if getattr(self, '_tab_handler_connected', False):
            try:
                self._pivot.currentItemChanged.disconnect(self._on_tab_changed)
            except (RuntimeError, TypeError):
                pass
        self._pivot.clear()
        for key in enabled:
            self._pivot.addItem(routeKey=key, text=tr(self._tab_text_keys[key]))
        apply_pivot_theme(self._pivot)
        self._pivot.currentItemChanged.connect(self._on_tab_changed)
        self._tab_handler_connected = True
        self._pivot.setVisible(len(enabled) > 1)
        self._pivot.setCurrentItem(current)
        tab = self._ensure_tab(current)
        if tab is not None:
            self._stack.setCurrentWidget(tab)
        self._current_tab_key = current
        if tab is not None and hasattr(tab, 'activate'):
            tab.activate()

    def _on_tab_visibility_changed(self, scope: str) -> None:
        if scope == 'tv':
            self._rebuild_tab_selector()

    def _on_tab_changed(self, key: str) -> None:
        self._current_tab_key = key
        tab = self._ensure_tab(key)
        if tab is None:
            return
        self._stack.setCurrentWidget(tab)
        if hasattr(tab, 'activate'):
            tab.activate()

    def open_detail(self, item: dict, media_kind: str = 'show') -> None:
        from src.core.config import is_tab_enabled
        tab_key = 'movies' if media_kind == 'movie' else 'series'
        if not is_tab_enabled('tv', tab_key):
            return
        tab = self._ensure_tab(tab_key)
        if tab is None or not hasattr(tab, '_open_details'):
            return
        self._pivot.setCurrentItem(tab_key)
        self._stack.setCurrentWidget(tab)
        self._current_tab_key = tab_key
        tmdb_media_type = 'movie' if media_kind == 'movie' else 'tv'
        tab._open_details(item, tmdb_media_type)

    def open_watch_later_entry(self, entry: dict) -> None:
        raw = (entry or {}).get('raw') or {}
        if not raw:
            return
        kind = (entry or {}).get('media_type')
        season, episode = (entry or {}).get('season'), (entry or {}).get('episode')
        if kind == 'anime':
            self.open_anime_detail(raw)
            if episode is not None:
                anime_tab = self._tabs.get('anime')
                if anime_tab is not None and hasattr(anime_tab, 'play_episode'):
                    anime_tab.play_episode(episode)
        elif kind in ('movie', 'tv'):
            self.open_detail(raw, 'movie' if kind == 'movie' else 'show')
            if episode is not None:
                details = getattr(self._tabs.get('series'), '_details', None)
                if details is not None:
                    details.open_episode(season or 1, episode)

    def show_tab(self, key: str) -> bool:
        from src.core.config import is_tab_enabled
        if key not in self._pivot.items or not is_tab_enabled('tv', key):
            return False
        tab = self._ensure_tab(key)
        if tab is None:
            return False
        self._pivot.setCurrentItem(key)
        self._stack.setCurrentWidget(tab)
        self._current_tab_key = key
        if hasattr(tab, 'activate'):
            tab.activate()
        return True

    def open_anime_detail(self, media: dict) -> None:
        if self.show_tab('anime'):
            self._tabs['anime'].open_detail(media)

    def _apply_locale(self, *_args) -> None:
        for key, _icon, text_key in self._TABS:
            if key in self._pivot.items:
                self._pivot.setItemText(key, tr(text_key))
            tab = self._tabs.get(key)
            if tab is not None and hasattr(tab, 'apply_locale'):
                tab.apply_locale()

    def shutdown(self) -> None:
        for key in ('channels', 'movies', 'series', 'anime'):
            tab = self._tabs.get(key)
            if tab is not None and hasattr(tab, 'shutdown'):
                tab.shutdown()
        try:
            from src.core.tv.torrent_stream import shutdown_streamer
            shutdown_streamer()
        except Exception:
            logger.debug("Could not shut the torrent streamer down", exc_info=True)
        close_ad_hoc_client()
