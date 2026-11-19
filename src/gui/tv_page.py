from __future__ import annotations
import asyncio
import datetime as _dt
import json
import logging
import os
import random
import re
import shutil
import threading
from contextlib import suppress
from concurrent.futures import ThreadPoolExecutor
import time
from typing import Any, Optional

from PySide6.QtCore import (
    Qt, QSize, Signal, QTimer, QEvent, QUrl, QObject, QVariantAnimation, QEasingCurve,
)
from PySide6.QtWidgets import QGraphicsDropShadowEffect
from PySide6.QtGui import QIcon, QFont, QColor, QPixmap, QDesktopServices, QBrush
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QGridLayout, QStackedWidget, QListWidgetItem, QFileDialog, QDialog,
    QLabel, QScrollArea, QAbstractScrollArea, QTreeWidgetItem, QSizePolicy, QTableWidgetItem, QHeaderView,
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

def _clean_query(text: str) -> str:
    value = re.sub(r'\s+', ' ', str(text or '')).strip()
    return value.strip('"\'.,;:!?()[]{}<>')

from .repacks_page import _tm_contains, _tm_variants


_LOGO_SIZE = (48, 48)
from src.core.tv import channels as channels_backend
from src.core.tv import streams as stream_backend
from src.core.tv import tmdb
from src.core.tv.anime import anime_backend
from src.core.config import settings
from src.core.system import clean_env as _clean_env, release_input_state

import zlib
from collections import OrderedDict
from PySide6.QtCore import QAbstractListModel, QModelIndex, QPoint, QPointF, QRect, QRectF, QSettings
from PySide6.QtGui import QFontMetrics, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QAbstractItemView, QBoxLayout, QFrame, QStyle, QStyledItemDelegate, QToolButton
from qfluentwidgets import Action, ListView, RoundMenu, TableItemDelegate, themeColor
from src.core.models import AnimeItem, assess_source_files, classify_file
from src.core.translations import tr, register_locale_refresh
from src.core.theme import to_color, palette, apply_pivot_theme, apply_tool_button_theme, apply_label_theme, count_badge_qss, register_theme_refresh, themed_icon, apply_slot_icon_theme
from .home_page import MediaPageBase
from .repacks_page import make_tag_chip, show_tag_menu, toggled_tags, bind_enter, split_tags, join_tags, partition_resolved
from .stuff.video import (
    AnimePlayerWidget, _Spinner, _UA, _accent, _ad_hoc_client, _play_icon, _wicon, close_ad_hoc_client,
)
from .stuff.grid import PosterGrid, PosterStrip, ViewModeButton, attrs_subtitle, grid_colors as _grid_colors


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


def _genre_pill(text: str, parent=None, on_click=None) -> PillPushButton:
    pill = PillPushButton(text, parent)
    pill.setChecked(True)
    pill.setCheckable(False)
    if on_click is not None:
        pill.setCursor(Qt.CursorShape.PointingHandCursor)
        pill.setToolTip(tr('tv.genre.show', default='Show titles tagged "{genre}"', genre=text))
        pill.clicked.connect(lambda _checked=False, t=text: on_click(t))
    else:
        pill.setCursor(Qt.CursorShape.ArrowCursor)

    def _repolish():
        style = pill.style()
        style.unpolish(pill)
        style.polish(pill)
        pill.update()
    register_theme_refresh(pill, _repolish)
    return pill


def _genre_key(name: str) -> str:
    return re.sub(r'[^a-z0-9]+', '', (name or '').lower())


def _suggest_genres(name: str, pool, limit: int = 3) -> list:
    import difflib
    key = _genre_key(name)
    keys = {_genre_key(g): g for g in pool if g}
    for g in [g for g in pool if g]:
        for word in re.findall(r'[A-Za-z0-9]{3,}', g):
            keys.setdefault(_genre_key(word), g)
    picked: list = []
    if len(key) >= 2:
        for k in sorted((k for k in keys if k.startswith(key) or key in k), key=lambda k: (not k.startswith(key), len(k))):
            if keys[k] not in picked:
                picked.append(keys[k])
    scored = sorted(((difflib.SequenceMatcher(None, key, k).ratio(), k) for k in keys), reverse=True)
    best = scored[0][0] if scored else 0.0
    for ratio, k in scored:
        if ratio < 0.6 or ratio < best - 0.15:
            break
        if keys[k] not in picked:
            picked.append(keys[k])
    return picked[:limit]


_RNG = random.SystemRandom()


class _DiceButton(QToolButton):

    def __init__(self, tip: str, parent=None) -> None:
        super().__init__(parent)
        self.setFixedSize(32, 32)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.TabFocus)
        self.setToolTip(tip)
        self.setAccessibleName(tip)
        self._hover = 0.0
        self._angle = 0.0
        self._hover_anim = QVariantAnimation(self)
        self._hover_anim.setDuration(160)
        self._hover_anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._hover_anim.valueChanged.connect(self._set_hover)
        self._spin_anim = QVariantAnimation(self)
        self._spin_anim.setStartValue(0.0)
        self._spin_anim.setEndValue(360.0)
        self._spin_anim.setDuration(520)
        self._spin_anim.setEasingCurve(QEasingCurve.Type.InOutCubic)
        self._spin_anim.valueChanged.connect(self._set_angle)
        self._spin_anim.finished.connect(lambda: self._set_angle(0.0))
        self.pressed.connect(self._roll)

    def set_busy(self, busy: bool) -> None:
        self._spin_anim.setLoopCount(-1 if busy else 1)
        if busy:
            self._roll()

    def _roll(self) -> None:
        if self._spin_anim.state() != QVariantAnimation.State.Running:
            self._spin_anim.start()

    def _set_hover(self, value) -> None:
        self._hover = float(value)
        self.update()

    def _set_angle(self, value) -> None:
        self._angle = float(value)
        self.update()

    def _fade_to(self, target: float) -> None:
        self._hover_anim.stop()
        self._hover_anim.setStartValue(self._hover)
        self._hover_anim.setEndValue(target)
        self._hover_anim.start()

    def enterEvent(self, event) -> None:
        self._fade_to(1.0)
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:
        self._fade_to(0.0)
        super().leaveEvent(event)

    def paintEvent(self, _event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        ink = to_color(palette().get('primary_text', '#ffffff'), '#ffffff')
        h = max(self._hover, 1.0 if self.hasFocus() else 0.0)
        alpha = 42 if self.isDown() else int(28 * h)
        if alpha:
            bg = QColor(ink)
            bg.setAlpha(alpha)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(bg)
            p.drawRoundedRect(QRectF(self.rect()), 6, 6)
        side = 15.0
        p.translate(self.width() / 2, self.height() / 2)
        p.rotate(-12.0 * self._hover + self._angle)
        scale = 0.9 if self.isDown() else 1.0 + 0.08 * self._hover
        p.scale(scale, scale)
        p.setOpacity(0.8 + 0.2 * self._hover)
        p.setPen(QPen(ink, 1.6))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawRoundedRect(QRectF(-side / 2, -side / 2, side, side), 3.4, 3.4)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(ink)
        off = side * 0.25
        for dx, dy in ((-1, -1), (1, -1), (0, 0), (-1, 1), (1, 1)):
            p.drawEllipse(QPointF(dx * off, dy * off), 1.3, 1.3)
        p.end()


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

        self._mode = Pivot(self)
        self._mode.addItem("url", tr("tv.channels.mode_link", default="Link"), onClick=lambda: self._set_mode(0))
        self._mode.addItem("file", tr("tv.channels.mode_file", default="Local file"), onClick=lambda: self._set_mode(1))
        self._mode.setCurrentItem("url")
        apply_pivot_theme(self._mode)
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


def _theme_color(key: str, fallback: str) -> QColor:
    color = QColor(str(palette().get(key) or ""))
    return color if color.isValid() else QColor(fallback)


class _StreamRowDelegate(TableItemDelegate):
    _LEFT = Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
    _RIGHT = Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
    _ALIGN = (_LEFT, _LEFT, _LEFT, _RIGHT, _RIGHT, _LEFT)

    def sizeHint(self, option, index) -> QSize:
        return QSize(option.rect.width(), 30)

    def paint(self, painter, option, index) -> None:
        rect, column, dark = option.rect, index.column(), isDarkTheme()
        painter.save()
        painter.setClipRect(rect)
        if option.state & QStyle.StateFlag.State_Selected:
            painter.fillRect(rect, QColor(255, 255, 255, 28) if dark else QColor(0, 0, 0, 20))
        elif index.row() == getattr(self, "hoverRow", -1):
            painter.fillRect(rect, QColor(255, 255, 255, 12) if dark else QColor(0, 0, 0, 9))
        brush = index.data(Qt.ItemDataRole.ForegroundRole)
        if isinstance(brush, QBrush):
            color = brush.color()
        elif column == 0:
            color = _theme_color("primary_text", "#ffffff" if dark else "#1a1a1a")
        else:
            color = _theme_color("muted", "#9a9a9a" if dark else "#666666")
        font = QFont(option.font)
        if column:
            font.setPointSizeF(max(font.pointSizeF() - 0.5, 7.5))
        painter.setFont(font)
        painter.setPen(color)
        width = max(rect.width() - 20, 10)
        text = QFontMetrics(font).elidedText(str(index.data(Qt.ItemDataRole.DisplayRole) or ""),
                                             Qt.TextElideMode.ElideRight, width)
        painter.drawText(QRectF(rect.left() + 10, rect.top(), width, rect.height()), int(self._ALIGN[column]), text)
        painter.restore()


def _show_picker(dialog) -> None:
    dialog.setWindowModality(Qt.WindowModality.NonModal)
    dialog.show()


class _StreamPickerDialog(MessageBoxBase):
    _COLUMNS = (
        ("tv.stream.col_name", "Name", 0),
        ("tv.stream.col_type", "Type", 76),
        ("tv.stream.col_quality", "Quality", 76),
        ("tv.stream.col_size", "Size", 84),
        ("tv.stream.col_peers", "Peers", 64),
        ("tv.stream.col_source", "Source", 120),
    )

    def __init__(self, sources: list, title: str, parent=None, action: str = "play") -> None:
        super().__init__(parent)
        self._pirachest_no_sfx = True
        self._sources = list(sources)
        dark = isDarkTheme()
        lay = self.viewLayout
        lay.setSpacing(8)

        self.titleLabel = StrongBodyLabel(tr("tv.download.pick_title", default="Choose a source to download")
                                          if action == "download"
                                          else tr("tv.stream.pick_title", default="Choose a stream"), self)
        lay.addWidget(self.titleLabel)

        for_label = BodyLabel(title, self)
        for_label.setWordWrap(True)
        lay.addWidget(for_label)

        hint = CaptionLabel(tr("tv.download.pick_hint",
                               default="You pick exactly which files to grab on the next step.") if action == "download"
                            else tr("tv.stream.pick_hint",
                                    default="Direct sources start instantly. For torrents, more seeders stream more smoothly."), self)
        hint.setWordWrap(True)
        apply_label_theme(hint, "muted")
        lay.addWidget(hint)
        safety = assess_source_files(
            [source.label or source.name for source in self._sources
             if not getattr(source, "is_direct", False)], context="video")
        if safety.level != "ok":
            warning = CaptionLabel(f"{safety.summary}. {tr('tbhb.file_type_review')}", self)
            warning.setWordWrap(True)
            warning.setToolTip(safety.details)
            warning.setStyleSheet(f"color: {palette()['state_verifying']};")
            lay.addWidget(warning)

        table = self._table = TableWidget(self)
        table.setBorderVisible(False)
        table.setWordWrap(False)
        table.setShowGrid(False)
        table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        table.setAlternatingRowColors(False)
        table.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        table.viewport().setAutoFillBackground(False)
        table.setItemDelegate(_StreamRowDelegate(table))
        table.setColumnCount(len(self._COLUMNS))
        table.setHorizontalHeaderLabels([tr(key, default=default) for key, default, _w in self._COLUMNS])
        header = table.horizontalHeader()
        header.setStretchLastSection(False)
        header.setHighlightSections(False)
        header.setMinimumSectionSize(48)
        header.setFixedHeight(26)
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for column, (_key, _default, width) in enumerate(self._COLUMNS[1:], start=1):
            header.setSectionResizeMode(column, QHeaderView.ResizeMode.Interactive)
            table.setColumnWidth(column, width)
            table.horizontalHeaderItem(column).setTextAlignment(
                (Qt.AlignmentFlag.AlignRight if column in (3, 4) else Qt.AlignmentFlag.AlignLeft)
                | Qt.AlignmentFlag.AlignVCenter)
        table.horizontalHeaderItem(0).setTextAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        line = "rgba(255,255,255,0.10)" if dark else "rgba(0,0,0,0.10)"
        muted = palette().get("muted") or "#9a9a9a"
        header.setStyleSheet(
            f"QHeaderView{{background:transparent;border:none;border-bottom:1px solid {line};}}"
            f"QHeaderView::section{{background:transparent;color:{muted};border:none;"
            f"border-right:1px solid {line};padding:0 10px;font-size:11px;}}"
            f"QHeaderView::section:last{{border-right:none;}}")
        table.verticalHeader().setVisible(False)
        table.verticalHeader().setDefaultSectionSize(30)
        table.setRowCount(len(self._sources))
        for row, source in enumerate(self._sources):
            self._fill_row(row, source)
        if self._sources:
            table.selectRow(0)
            table.setCurrentCell(0, 0)
        table.cellDoubleClicked.connect(lambda _row, _col: self.accept())
        table.setMinimumHeight(320)

        box = QFrame(self)
        box.setObjectName("streamBox")
        box.setStyleSheet(f"#streamBox{{background:{'rgba(0,0,0,0.28)' if dark else 'rgba(0,0,0,0.045)'};border-radius:8px;}}")
        box_lay = QVBoxLayout(box)
        box_lay.setContentsMargins(4, 2, 4, 4)
        box_lay.addWidget(table)
        lay.addWidget(box, 1)

        if action == "download":
            self.yesButton.setText(tr("tv.download.pick", default="Download"))
            self.yesButton.setIcon(FluentIcon.DOWNLOAD)
        else:
            self.yesButton.setText(tr("tv.stream.play", default="Play"))
            self.yesButton.setIcon(_play_icon(follow=True))
        self.cancelButton.setText(tr("tv.stream.cancel", default="Cancel"))
        self.yesButton.setFixedHeight(32)
        self.cancelButton.setFixedHeight(32)
        self.buttonGroup.setMinimumHeight(56)
        self.buttonLayout.setContentsMargins(24, 12, 24, 12)
        self.widget.setMinimumWidth(840)
        self.widget.setMaximumWidth(980)

    def _fill_row(self, row: int, source) -> None:
        label = source.label or source.name or source.info_hash
        info = classify_file(label)
        name = QTableWidgetItem(label)
        name.setData(Qt.ItemDataRole.UserRole, row)
        name.setToolTip(label)
        type_item = QTableWidgetItem(info.label)
        type_item.setToolTip(info.reason)
        if info.risk != "ok":
            type_item.setForeground(QColor(palette()["state_error" if info.risk == "dangerous"
                                                    else "state_verifying"]))
        source_item = QTableWidgetItem(source.indexer or _SOURCE_NAMES.get(source.source, source.source or "\u2014"))
        if source.detail:
            source_item.setToolTip(source.detail)
        cells = (
            name,
            type_item,
            QTableWidgetItem(source.quality or "\u2014"),
            QTableWidgetItem(stream_backend.human_size(source.size) if source.size else "\u2014"),
            QTableWidgetItem("Direct" if getattr(source, "is_direct", False)
                             else (str(source.seeders) if source.seeders else "\u2014")),
            source_item,
        )
        for column, item in enumerate(cells):
            self._table.setItem(row, column, item)

    def chosen(self):
        row = self._table.currentRow()
        return self._sources[row] if 0 <= row < len(self._sources) else None


class _HoverTree(TreeWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.hover = None
        self.setMouseTracking(True)
        self.viewport().setMouseTracking(True)

    def _set_hover(self, item) -> None:
        if item is not self.hover:
            self.hover = item
            self.viewport().update()

    def mouseMoveEvent(self, event) -> None:
        self._set_hover(self.itemAt(event.position().toPoint()))
        super().mouseMoveEvent(event)

    def leaveEvent(self, event) -> None:
        self._set_hover(None)
        super().leaveEvent(event)


class _FileListDelegate(QStyledItemDelegate):
    _LEFT = Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
    _RIGHT = Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter

    def __init__(self, tree: _HoverTree):
        super().__init__(tree)
        self._tree = tree

    def sizeHint(self, option, index) -> QSize:
        return QSize(option.rect.width(), 28)

    def paint(self, painter, option, index) -> None:
        tree, rect, column, dark = self._tree, option.rect, index.column(), isDarkTheme()
        item = tree.itemFromIndex(index.siblingAtColumn(0))
        painter.save()
        painter.setClipRect(rect)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        if item is not None and item is tree.hover:
            painter.fillRect(rect, QColor(255, 255, 255, 12) if dark else QColor(0, 0, 0, 9))
        muted = _theme_color("muted", "#9a9a9a" if dark else "#666666")
        font = QFont(option.font)
        left = rect.left() + 10
        if column == 0:
            box = QRectF(left, rect.center().y() - 7, 14, 14)
            if item is not None and item.checkState(0) == Qt.CheckState.Checked:
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(QColor(themeColor()))
                painter.drawRoundedRect(box, 3, 3)
                pen = QPen(QColor("#ffffff"), 1.7)
                pen.setCapStyle(Qt.PenCapStyle.RoundCap)
                pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
                painter.setPen(pen)
                c, mark = box.center(), QPainterPath()
                mark.moveTo(c.x() - 3.4, c.y() + 0.2)
                mark.lineTo(c.x() - 1.0, c.y() + 2.6)
                mark.lineTo(c.x() + 3.6, c.y() - 2.6)
                painter.drawPath(mark)
            else:
                painter.setPen(QPen(muted, 1.2))
                painter.setBrush(Qt.BrushStyle.NoBrush)
                painter.drawRoundedRect(box.adjusted(0.6, 0.6, -0.6, -0.6), 3, 3)
            left = int(box.right()) + 8
            color, align = _theme_color("primary_text", "#ffffff" if dark else "#1a1a1a"), self._LEFT
        else:
            font.setPointSizeF(max(font.pointSizeF() - 0.5, 7.5))
            brush = index.data(Qt.ItemDataRole.ForegroundRole)
            color = brush.color() if isinstance(brush, QBrush) else muted
            align = self._RIGHT if column == 2 else self._LEFT
        width = max(rect.right() - 10 - left, 10)
        painter.setFont(font)
        painter.setPen(color)
        painter.drawText(QRectF(left, rect.top(), width, rect.height()), int(align),
                         QFontMetrics(font).elidedText(str(index.data(Qt.ItemDataRole.DisplayRole) or ""),
                                                       Qt.TextElideMode.ElideRight, int(width)))
        painter.restore()

    def editorEvent(self, event, model, option, index) -> bool:
        etype = event.type()
        if etype == QEvent.Type.MouseButtonRelease:
            if event.button() != Qt.MouseButton.LeftButton:
                return False
        elif not (etype == QEvent.Type.KeyPress and event.key() in (Qt.Key.Key_Space, Qt.Key.Key_Select)):
            return False
        item = self._tree.itemFromIndex(index.siblingAtColumn(0))
        if item is None:
            return False
        item.setCheckState(0, Qt.CheckState.Unchecked if item.checkState(0) == Qt.CheckState.Checked
                           else Qt.CheckState.Checked)
        return True


def _resolve_manager(owner):
    if owner._download_manager is None:
        win = owner.window()
        owner._download_manager = next(
            (v for o in (win, *vars(win).values()) for v in getattr(o, "__dict__", {}).values()
             if type(v).__name__ == "DownloadManager"), None)
    return owner._download_manager


def _check_tree(parent, headers: list, widths: tuple, header_visible: bool = True):
    dark = isDarkTheme()
    tree = _HoverTree(parent)
    tree.setColumnCount(len(headers))
    tree.setHeaderLabels(headers)
    tree.setHeaderHidden(not header_visible)
    tree.setRootIsDecorated(False)
    tree.setIndentation(0)
    tree.setUniformRowHeights(True)
    tree.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
    tree.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
    tree.setBorderVisible(False)
    tree.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
    tree.viewport().setAutoFillBackground(False)
    tree.setMinimumHeight(320)
    tree.setItemDelegate(_FileListDelegate(tree))
    header = tree.header()
    header.setStretchLastSection(False)
    header.setSectionsMovable(False)
    header.setHighlightSections(False)
    header.setMinimumSectionSize(56)
    header.setFixedHeight(26)
    header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
    for column, width in enumerate(widths, start=1):
        header.setSectionResizeMode(column, QHeaderView.ResizeMode.Interactive)
        tree.setColumnWidth(column, width)
    header.setDefaultAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
    line = "rgba(255,255,255,0.10)" if dark else "rgba(0,0,0,0.10)"
    muted = palette().get("muted") or "#9a9a9a"
    header.setStyleSheet(
        f"QHeaderView{{background:transparent;border:none;border-bottom:1px solid {line};}}"
        f"QHeaderView::section{{background:transparent;color:{muted};border:none;"
        f"border-right:1px solid {line};padding:0 10px;font-size:11px;}}"
        f"QHeaderView::section:last{{border-right:none;}}")
    box = QFrame(parent)
    box.setObjectName("checkBox")
    box.setStyleSheet(f"#checkBox{{background:{'rgba(0,0,0,0.28)' if dark else 'rgba(0,0,0,0.045)'};border-radius:8px;}}")
    box_lay = QVBoxLayout(box)
    box_lay.setContentsMargins(4, 2, 4, 4)
    box_lay.addWidget(tree)
    return tree, box


class _TorrentFilesDialog(MessageBoxBase):
    def __init__(self, entries: list, title: str, parent=None, safety=None) -> None:
        super().__init__(parent)
        self._items: list = []
        muted = palette().get("muted") or "#9a9a9a"
        lay = self.viewLayout
        lay.setSpacing(8)

        self.titleLabel = StrongBodyLabel(tr("tv.download.files_title", default="Files to download"), self)
        lay.addWidget(self.titleLabel)

        for_label = BodyLabel(title, self)
        for_label.setWordWrap(True)
        lay.addWidget(for_label)

        if safety is not None and safety.level != "ok":
            warning = CaptionLabel(f"{safety.summary}. {tr('tbhb.file_type_review')}", self)
            warning.setWordWrap(True)
            warning.setToolTip(safety.details)
            warning.setStyleSheet(f"color: {palette()['state_error' if safety.level == 'dangerous' else 'state_verifying']};")
            lay.addWidget(warning)

        bar = QHBoxLayout()
        self._all_chk = CheckBox(tr("tv.download.select_all", default="Select all"), self)
        self._all_chk.setChecked(True)
        self._all_chk.stateChanged.connect(self._on_select_all)
        self._count_lbl = CaptionLabel("", self)
        self._count_lbl.setStyleSheet(f"color: {muted};")
        bar.addWidget(self._all_chk)
        bar.addStretch(1)
        bar.addWidget(self._count_lbl)
        lay.addLayout(bar)

        tree, box = _check_tree(self, [tr("tv.download.col_file", default="File"),
                                       tr("tv.download.col_type", default="Type"),
                                       tr("repacks.size", default="Size")], (80, 96))
        self._tree = tree
        tree.headerItem().setTextAlignment(2, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        tree.itemChanged.connect(self._on_item_changed)
        lay.addWidget(box, 1)
        self._build_tree(entries)

        self.yesButton.setText(tr("tv.download.pick", default="Download"))
        self.yesButton.setIcon(FluentIcon.DOWNLOAD)
        self.cancelButton.setText(tr("tv.stream.cancel", default="Cancel"))
        self.yesButton.setFixedHeight(32)
        self.cancelButton.setFixedHeight(32)
        self.buttonGroup.setMinimumHeight(56)
        self.buttonLayout.setContentsMargins(24, 12, 24, 12)
        self.widget.setMinimumWidth(780)
        self.widget.setMaximumWidth(940)

    @staticmethod
    def _label(entry: dict) -> str:
        if entry.get("episode") is not None:
            head = (f"S{int(entry['season']):02d}E{int(entry['episode']):02d}"
                    if entry.get("season") is not None else f"E{int(entry['episode']):02d}")
            return f"{head}  ·  {entry['name']}"
        return entry["name"]

    def _build_tree(self, entries: list) -> None:
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
            item.setCheckState(0, Qt.CheckState.Checked)
            item.setToolTip(0, entry["name"])
            self._items.append((entry, item))
        self._tree.blockSignals(False)
        self._sync_select_all()

    def _on_select_all(self, _state=None) -> None:
        checked = Qt.CheckState.Checked if self._all_chk.isChecked() else Qt.CheckState.Unchecked
        self._tree.blockSignals(True)
        for _entry, item in self._items:
            item.setCheckState(0, checked)
        self._tree.blockSignals(False)
        self._update_count()

    def _on_item_changed(self, *_args) -> None:
        self._sync_select_all()

    def _sync_select_all(self) -> None:
        states = {item.checkState(0) for _entry, item in self._items}
        self._all_chk.blockSignals(True)
        self._all_chk.setChecked(bool(states) and states == {Qt.CheckState.Checked})
        self._all_chk.blockSignals(False)
        self._update_count()

    def _update_count(self) -> None:
        picked = [entry for entry, item in self._items if item.checkState(0) == Qt.CheckState.Checked]
        self._count_lbl.setText(f"{len(picked)} / {len(self._items)} \u00b7 "
                                f"{stream_backend.human_size(sum(entry['size'] for entry in picked))}")

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
        self._c_play_bar = QColor(_accent())

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

    def set_rows(self, rows: list[int], val: bool) -> None:
        on = self.on
        changed = [r for r in rows if on[r] != val]
        if not changed:
            return
        for r in changed:
            on[r] = val
        self.dataChanged.emit(self.index(min(changed)), self.index(max(changed)))


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
        self.widget.setMinimumWidth(420)
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
        view = self._view
        self._model.set_rows([i for i in range(self._model.rowCount()) if not view.isRowHidden(i)], val)
        self._update_summary()

    def _on_search(self, text: str) -> None:
        q = text.strip()
        view = self._view
        view.setUpdatesEnabled(False)
        try:
            for i, n in enumerate(self._model.low):
                hide = bool(q) and not _tm_contains(q, n)
                if view.isRowHidden(i) != hide:
                    view.setRowHidden(i, hide)
        finally:
            view.setUpdatesEnabled(True)

    def _update_summary(self) -> None:
        n = self._model.on.count(True)
        self._summary.setText(tr("tv.channels.n_selected", default="{n} selected").format(n=n))
        self._ok.setEnabled(n > 0)

    def selected_codes(self) -> list[str]:
        m = self._model
        return [c for c, on in zip(m.codes, m.on, strict=True) if on]


def _fetch_countries() -> tuple:
    return channels_backend.get_available_countries(), channels_backend.get_selected_countries()


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
        if self._player is not None and self._player.has_media():
            self._player.hide()
        task: dict = {"id": None}

        def _cancel() -> None:
            if task["id"] is not None:
                _worker_module.cancel(task["id"])
                task["id"] = None

        def _fetch() -> None:
            _cancel()
            dlg.set_loading()

            def _done(res: tuple) -> None:
                if not _qt_is_valid(dlg):
                    return
                task["id"] = None
                countries, sel = res
                if not countries:
                    dlg.set_error(tr("tv.channels.no_countries", default="No countries available."))
                    return
                dlg.set_countries(countries, set(sel) if sel is not None else None)

            def _err(msg: str) -> None:
                if not _qt_is_valid(dlg):
                    return
                task["id"] = None
                logger.error("Failed to load country list: %s", msg)
                dlg.set_error(tr("tv.channels.countries_error", default="Couldn't load the country list."))

            task["id"] = _worker_module.submit(_fetch_countries, on_done=_done, on_error=_err)

        def _finished(code: int) -> None:
            _cancel()
            self._prompting = False
            if self._player is not None:
                self._player.show()
            codes = dlg.selected_codes() if code == QDialog.DialogCode.Accepted else []
            dlg.deleteLater()
            if codes:
                channels_backend.set_selected_countries(codes)
                self._load_channels(use_cache=True)

        dlg.retry_requested.connect(_fetch)
        dlg.finished.connect(_finished)
        _fetch()
        dlg.open()


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
        q = self._search.text().strip()
        sf, cf, gf = (self._facet(c) for c in (self._source_combo, self._country_combo, self._group_combo))
        rows = self._all
        if sf is None and cf is None and gf is None and not q:
            out = rows
        elif q:
            if self._keys is None:
                self._keys = [f"{c.get('name') or ''}\n{c.get('group') or ''}" for c in rows]
            out = [c for c, k in zip(rows, self._keys, strict=True)
                   if _tm_contains(q, k) and (sf is None or c["source_id"] == sf)
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
        if self._player is not None and self._player.has_media():
            self._player.hide()
        dialog = _AddSourceDialog(self.window())
        try:
            accepted = dialog.exec() == QDialog.DialogCode.Accepted
        finally:
            if self._player is not None:
                self._player.show()
        if accepted:
            source = dialog.result_source()
            if source is not None:
                InfoBar.success(title=tr("tv.channels.source_added", default="Source added"),
                                content=source["name"], parent=self.window(), duration=2000, isClosable=True)
                if self._state != "idle":
                    self._load_channels(use_cache=False)

    def _on_manage_sources(self) -> None:
        if self._player is not None and self._player.has_media():
            self._player.hide()
        dialog = _ManageSourcesDialog(self.window())
        try:
            dialog.exec()
        finally:
            if self._player is not None:
                self._player.show()
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
    def paintEvent(self, e) -> None:
        if (self.isHover or self.isPressed) and not self.underMouse():
            self.isHover = self.isPressed = False
        super().paintEvent(e)

    def hideEvent(self, e) -> None:
        self.isHover = self.isPressed = False
        super().hideEvent(e)

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


class _SplitPlayButton(PrimaryPushButton):
    menuRequested = Signal(QPoint)
    _ARROW_W = 34

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._accented = False
        self._arrow_hot = False
        self.setMouseTracking(True)
        self.setFixedHeight(32)
        self.setIconSize(QSize(20, 20))
        font = QFont(self.font())
        font.setPointSizeF(10.5)
        font.setWeight(QFont.Weight.Medium)
        font.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, 0.3)
        font.setStyleStrategy(QFont.StyleStrategy.PreferAntialias | QFont.StyleStrategy.PreferQuality)
        font.setHintingPreference(QFont.HintingPreference.PreferNoHinting)
        self.setFont(font)

    def set_accent(self, base: str = "") -> None:
        self._accented = bool(base)
        if not base:
            self.setStyleSheet("")
            self.update()
            return
        col = QColor(base)

        def grad(top, bottom):
            return (f"qlineargradient(x1:0,y1:0,x2:0,y2:1,stop:0 {top.name()},stop:1 {bottom.name()})")

        pad = f"padding:5px {self._ARROW_W + 18}px 5px 38px;min-height:20px;"
        self.setStyleSheet(
            f"PrimaryPushButton{{background:{grad(col.lighter(106), col.darker(104))};color:#fff;border:none;border-radius:5px;{pad}}}"
            f"PrimaryPushButton:hover{{background:{grad(col.lighter(118), col.lighter(106))};}}"
            f"PrimaryPushButton:pressed{{background:{grad(col.darker(108), col.darker(116))};}}"
            "PrimaryPushButton:disabled{background-color:rgba(128,128,128,70);color:rgba(255,255,255,120);}")
        self.update()

    def _in_arrow(self, x: float) -> bool:
        return x >= self.width() - self._ARROW_W

    def mouseMoveEvent(self, e) -> None:
        hot = self._in_arrow(e.position().x())
        if hot != self._arrow_hot:
            self._arrow_hot = hot
            self.update()
        super().mouseMoveEvent(e)

    def leaveEvent(self, e) -> None:
        self._arrow_hot = False
        super().leaveEvent(e)

    def mousePressEvent(self, e) -> None:
        if e.button() != Qt.MouseButton.LeftButton:
            return
        if self._in_arrow(e.position().x()):
            self.menuRequested.emit(self.mapToGlobal(self.rect().bottomLeft()))
            return
        super().mousePressEvent(e)

    def contextMenuEvent(self, e) -> None:
        self.menuRequested.emit(e.globalPos())

    def paintEvent(self, e) -> None:
        super().paintEvent(e)
        ink = QColor("#fff") if self._accented or not isDarkTheme() else QColor("#000")
        if not self.isEnabled():
            ink.setAlphaF(0.4)
        seam = QColor(ink)
        seam.setAlphaF(ink.alphaF() * 0.35)
        x, h = self.width() - self._ARROW_W, self.height()
        ax, cy = x + self._ARROW_W / 2, h / 2
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        if self._arrow_hot and self.isEnabled():
            glow = QColor(ink)
            glow.setAlphaF(0.2)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(glow)
            p.drawRoundedRect(QRectF(x + 2, 3, self._ARROW_W - 5, h - 6), 4, 4)
        p.setPen(QPen(seam, 1))
        p.drawLine(QPointF(x, 7), QPointF(x, h - 7))
        p.setPen(QPen(ink, 1.6, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
        p.drawPolyline([QPointF(ax - 4, cy - 2), QPointF(ax, cy + 2), QPointF(ax + 4, cy - 2)])
        p.end()


class _DetailsChrome:
    def set_top_inset(self, px: int) -> None:
        left, top, right, bottom = self._content_margins
        self._content_layout.setContentsMargins(left, top + px, right, bottom)

    def showEvent(self, event) -> None:
        super().showEvent(event)
        self.banner_changed.emit()

    def hideEvent(self, event) -> None:
        super().hideEvent(event)
        self.banner_changed.emit()


class _MediaDetailsView(_DetailsChrome, QWidget):
    POSTER_WIDTH = 220
    POSTER_HEIGHT = 330
    banner_changed = Signal()
    genre_requested = Signal(str)

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
        self._content_layout = content_layout
        self._content_margins = (4, 4, 20, 32)
        content_layout.addWidget(back_btn, 0, Qt.AlignmentFlag.AlignLeft)

        top_row = QHBoxLayout()
        top_row.setSpacing(28)
        top_row.setAlignment(Qt.AlignmentFlag.AlignTop)

        cover = QWidget(content)
        cover.setFixedWidth(self.POSTER_WIDTH)
        cover_col = QVBoxLayout(cover)
        cover_col.setContentsMargins(0, 0, 0, 0)
        cover_col.setSpacing(8)

        self._poster_lbl = ImageLabel(cover)
        self._poster_lbl.setBorderRadius(10, 10, 10, 10)
        self._poster_lbl.setFixedSize(self.POSTER_WIDTH, self.POSTER_HEIGHT)
        self._poster_lbl.setStyleSheet(f"background: {palette()['poster_fallback_bg']};")
        cover_col.addWidget(self._poster_lbl)

        self._watch_btn = _SplitPlayButton(_play_icon(follow=True), tr("tv.watch", default="Watch"), cover)
        self._watch_btn.clicked.connect(self._on_watch)
        self._watch_btn.menuRequested.connect(self._show_source_menu)
        self._watch_btn.setVisible(bool(getattr(settings, "tv_stream_enabled", True)))
        cover_col.addWidget(self._watch_btn)

        top_row.addWidget(cover, 0, Qt.AlignmentFlag.AlignTop)

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
        self._wl_btn = _WatchLaterButton(content)
        self._wl_btn.clicked.connect(self._on_toggle_watch_later)
        self._wl_btn.hide()
        watch_row.addWidget(self._wl_btn)
        self._imdb_btn = PushButton(FluentIcon.LINK, "IMDb", content)
        self._imdb_btn.clicked.connect(lambda: QDesktopServices.openUrl(QUrl(self._imdb_url)))
        self._imdb_btn.hide()
        watch_row.addWidget(self._imdb_btn)
        self._trailer_btn = PushButton(FluentIcon.VIDEO, tr("tv.movies.trailer", default="Trailer"), content)
        self._trailer_btn.clicked.connect(lambda: QDesktopServices.openUrl(QUrl(self._trailer_url)))
        self._trailer_btn.hide()
        watch_row.addWidget(self._trailer_btn)
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

        self._player_host = (content, content_layout)
        self._player_widget = self._make_player(content)
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
        self._autoplay_episode = False
        self._watch_btn.setVisible(bool(getattr(settings, "tv_stream_enabled", True)))
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
        self._watch_btn.set_accent()
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
            if not genre.get("name"):
                continue
            tag = _genre_pill(genre["name"], self._tags_container, self.genre_requested.emit)
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
        self._watch_btn.set_accent(c.name())

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
        menu.addSeparator()
        menu.addAction(Action(FluentIcon.DOWNLOAD,
                              tr("tv.download.episode", default="Download this episode..."),
                              triggered=lambda: self._on_download(episode=episode)))
        menu.exec(pos)

    def set_download_manager(self, manager) -> None:
        self._download_manager = manager

    def _manager(self):
        return _resolve_manager(self)

    def start_watch(self) -> None:
        if self._media_id is None or not getattr(settings, "tv_stream_enabled", True):
            return
        if self._imdb_id or self._title_lbl.text():
            self._on_watch()
        else:
            self._autoplay_episode = True

    def start_download(self) -> None:
        if self._media_id is None:
            return
        if self._imdb_id or self._title_lbl.text():
            self._on_download()
        else:
            self._download_pending = True

    def _on_download(self, episode: Optional[dict] = None) -> None:
        title = self._title_lbl.text()
        if self._media_id is None or self._download_task_id is not None:
            return
        if self._manager() is None:
            InfoBar.error(title=tr("tv.download.failed_title", default="Download failed"),
                          content=tr("tv.download.no_manager", default="The download manager is not available."),
                          duration=5000, parent=self)
            return
        if not (self._imdb_id or title):
            InfoBar.warning(title=tr("tv.stream.no_imdb_title", default="Cannot search streams"),
                            content=tr("tv.stream.no_imdb_body", default="This title has no name to look up."),
                            duration=4000, parent=self)
            return
        tv = self._media_type == "tv"
        sn = int(self._current_season or 1) if tv else None
        number = int((episode or {}).get("episode_number") or self._current_episode or 1) if tv else None
        label = f"{title} - S{sn:02d}E{number:02d}" if tv else title

        def _done(sources: list) -> None:
            self._download_idle()
            if not sources:
                InfoBar.warning(title=tr("tv.stream.none_title", default="No streams found"),
                                content=tr("tv.stream.none_body", default="No playable sources were found for this title."),
                                duration=5000, parent=self)
                return
            def _pick() -> None:
                window = self.window()
                window.activateWindow()
                dialog = _StreamPickerDialog(sources, label, window, action="download")

                def _fin(result: int) -> None:
                    chosen = dialog.chosen()
                    dialog.deleteLater()
                    if result == QDialog.DialogCode.Accepted and chosen is not None:
                        self._download_source(chosen, label, sn, number)
                dialog.finished.connect(_fin)
                _show_picker(dialog)
            QTimer.singleShot(0, _pick)

        self._set_streaming(True, tr("tv.download.searching", default="Looking for sources to download..."))
        self._download_task_id = _worker_module.submit_coro(
            _find_media_sources,
            args=(self._imdb_id, self._media_type, sn, number, title, self._media_year(), self._media_id, False),
            on_done=_done, on_error=self._download_error)

    def _download_idle(self) -> None:
        self._download_task_id = None
        self._set_streaming(False)

    def _download_error(self, message: str) -> None:
        self._download_idle()
        InfoBar.error(title=tr("tv.download.failed_title", default="Download failed"),
                      content=str(message), duration=6000, parent=self)

    def _download_source(self, source, label: str, season: Optional[int], number: Optional[int]) -> None:
        if getattr(source, "is_direct", False):
            self._download_direct_source(source, (season, number) if season and number else None)
            return
        magnet = source.magnet or (stream_backend.magnet_for(source.info_hash, source.name) if source.info_hash else source.name)

        def _done(files: list) -> None:
            self._download_idle()
            entries = _torrent_file_entries(files or [])
            if not entries:
                InfoBar.warning(title=tr("tv.download.no_files_title", default="Nothing to download"),
                                content=tr("tv.download.no_files_body", default="This torrent has no matching video file."),
                                duration=5000, parent=self)
                return
            if season is None:
                dialog = _TorrentFilesDialog(entries, label, self.window(),
                                             assess_source_files([e["name"] for e in entries], context="video"))
                if dialog.exec() != QDialog.DialogCode.Accepted:
                    return
                ids = dialog.selected_indices()
            else:
                matched = [e for e in entries if e["season"] in (None, season) and e["episode"] == number]
                matched = matched or (entries if len(entries) == 1 else [])
                if not matched:
                    InfoBar.warning(title=tr("tv.download.no_files_title", default="Nothing to download"),
                                    content=tr("tv.download.no_episode_body", default="This torrent doesn't contain that episode."),
                                    duration=5000, parent=self)
                    return
                ids = [max(matched, key=lambda e: e["size"])["index"] + 1]
            try:
                self._manager().add(
                    torrent_file=magnet, file_id=ids[0], file_ids=ids, game_name=label,
                    console=self._title_lbl.text(),
                    source=source.indexer or _SOURCE_NAMES.get(source.source, source.source or "Torrent"),
                    category="tv")
            except Exception as exc:
                logger.exception("Could not queue the download")
                self._download_error(f"{type(exc).__name__}: {exc}")
                return
            InfoBar.success(title=tr("tv.download.queued_title", default="Download queued"),
                            content=label, duration=3000, parent=self)

        self._set_streaming(True, tr("tv.download.reading", default="Reading torrent files..."))
        self._download_task_id = _worker_module.submit(
            self._manager().fetch_file_list, args=(magnet,),
            kwargs={"timeout_secs": 45.0, "category": "tv"}, on_done=_done, on_error=self._download_error)

    def _ensure_ffmpeg_ready(self, source) -> bool:
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

    def _download_direct_source(self, source, ep: Optional[tuple] = None) -> None:
        manager = self._manager()
        if manager is None or not getattr(source, "url", ""):
            return
        if not self._ensure_ffmpeg_ready(source):
            return
        self._start_direct_download(source, ep)

    def _start_direct_download(self, source, ep: Optional[tuple] = None) -> None:
        manager = self._manager()
        if manager is None or not getattr(source, "url", ""):
            return
        title = self._title_lbl.text() or "Video"
        season, episode = ep or self._stream_episode or (None, None)
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

    def _show_source_menu(self, pos: QPoint) -> None:
        if self._media_id is None or not getattr(settings, "tv_stream_enabled", True):
            return
        menu = RoundMenu(parent=self)
        pick = Action(FluentIcon.MENU, tr("tv.stream.pick_title", default="Choose a stream"), menu)
        pick.triggered.connect(lambda _=False: self._open_stream_picker(force_picker=True))
        menu.addAction(pick)
        menu.addSeparator()
        download = Action(FluentIcon.DOWNLOAD, tr("tv.download.choose", default="Download..."), menu)
        download.triggered.connect(lambda _=False: self._on_download())
        menu.addAction(download)
        menu.closedSignal.connect(menu.deleteLater)
        menu.exec(pos)

    def _on_alt_stream(self, _stream: dict) -> None:
        self._open_stream_picker(force_picker=True)

    def _open_stream_picker(self, refresh: bool = False, force_picker: bool = False) -> None:
        if self._source_task_id is not None:
            if force_picker:
                self._picker_forced = True
                if self._source_quick:
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
        QTimer.singleShot(0, lambda: _show_picker(dialog))

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

    def _make_player(self, parent: QWidget) -> "AnimePlayerWidget":
        pl = AnimePlayerWidget(parent)
        pl.alt_stream_requested.connect(self._on_alt_stream)
        pl.playback_error.connect(self._on_player_error)
        pl.ended.connect(self._on_player_ended)
        pl.next_requested.connect(self._play_next)
        pl.close_requested.connect(self._close_media_player)
        pl.set_closable(True)
        pl.hide()
        return pl

    def _replace_player(self) -> None:
        content, layout = self._player_host
        old = self._player_widget
        index = max(layout.indexOf(old), 0)
        layout.removeWidget(old)
        old.hide()
        new = self._player_widget = self._make_player(content)
        layout.insertWidget(index, new, 0, Qt.AlignmentFlag.AlignHCenter)
        old.destroy()
        release_input_state()

    def _close_media_player(self) -> None:
        p = getattr(self, "_picker", None)
        if p is not None and _qt_is_valid(p):
            p.close()
        self._stop_playback()
        QTimer.singleShot(0, self._replace_player)
        w = self.window()
        if w is not None:
            w.activateWindow()

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
        self._random_token = 0
        self._random_task_id: Optional[str] = None
        self._last_random: Any = None
        self._genre_names: list[str] = []
        self._genre_ids: list = []
        self._genre_excl: list = []
        self._genre_excl_ids: set = set()
        self._want_exc: list = []
        self._genre_missing: list[str] = []
        self._genre_seq = 0
        self._last_genre_query: list = []
        self._genre_pool: list = []
        self._genre_page = 0
        self._genre_has_more = True
        self._genre_loading = False
        self._genre_stale = 0
        self._genre_task_id: Optional[str] = None

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
        self._search_box.textChanged.connect(self._on_search_text_changed)
        self._search_box.searchSignal.connect(self._on_search_commit)
        bind_enter(self._search_box, self._on_search_commit)
        self._search_box.clearSignal.connect(self._on_search_commit)
        from src.gui.stuff.search_history import attach as attach_search_history
        attach_search_history(self._search_box, 'tv', lambda _text: self._on_search_commit())
        self._search_box.setMinimumWidth(160)
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
        self._dice_btn = _DiceButton(tr("tv.random", default="Surprise me"), self._browse_page)
        self._dice_btn.clicked.connect(self._surprise_me)
        search_row.addWidget(self._dice_btn)
        browse_layout.addLayout(search_row)

        self._genre_bar = QWidget(self._browse_page)
        self._genre_bar.setStyleSheet('background: transparent;')
        bar_row = QHBoxLayout(self._genre_bar)
        bar_row.setContentsMargins(0, 0, 0, 0)
        bar_row.setSpacing(8)
        self._genre_hint = CaptionLabel('', self._genre_bar)
        apply_label_theme(self._genre_hint, 'muted')
        self._genre_pill_row = QHBoxLayout()
        self._genre_pill_row.setContentsMargins(0, 0, 0, 0)
        self._genre_pill_row.setSpacing(6)
        self._genre_note = CaptionLabel('', self._genre_bar)
        apply_label_theme(self._genre_note, 'muted')
        self._genre_note.setVisible(False)
        self._genre_add = TransparentToolButton(FluentIcon.ADD, self._genre_bar)
        apply_tool_button_theme(self._genre_add)
        self._genre_add.setFixedSize(24, 24)
        self._genre_add.setIconSize(QSize(12, 12))
        self._genre_add.setToolTip(tr('tv.genre.add', default='Add or remove genres'))
        self._genre_add.clicked.connect(lambda _c=False: self._open_genre_menu())
        self._genre_clear = TransparentToolButton(FluentIcon.CLOSE, self._genre_bar)
        apply_tool_button_theme(self._genre_clear)
        self._genre_clear.setFixedSize(24, 24)
        self._genre_clear.setIconSize(QSize(12, 12))
        self._genre_clear.setToolTip(tr('tv.genre.back', default='Back to browsing'))
        self._genre_clear.clicked.connect(lambda _c=False: self._clear_genres())
        bar_row.addWidget(self._genre_hint)
        bar_row.addLayout(self._genre_pill_row)
        bar_row.addWidget(self._genre_add)
        bar_row.addWidget(self._genre_clear)
        bar_row.addWidget(self._genre_note)
        bar_row.addStretch(1)
        self._genre_bar.setVisible(False)
        browse_layout.addWidget(self._genre_bar)
        self._suggest_bar = QWidget(self._browse_page)
        self._suggest_bar.setStyleSheet('background: transparent;')
        self._suggest_row = QHBoxLayout(self._suggest_bar)
        self._suggest_row.setContentsMargins(0, 0, 0, 0)
        self._suggest_row.setSpacing(8)
        self._suggest_bar.setVisible(False)
        browse_layout.addWidget(self._suggest_bar)

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
        self._search_grid.near_bottom.connect(self._on_genre_near_bottom)
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
        self._details.genre_requested.connect(self._on_genre_pill)
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
            elif self._genre_names or self._genre_excl:
                self._restart_genre_list()
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
        text = self._search_box.text()
        tags = self._parse_tag_query(text)
        if tags is not None:
            self._search_timer.stop()
            self._search_genres(tags)
            return
        self._leave_genre_mode()
        self._query = _clean_query(text)
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
        alts = _tm_variants(query)[1:]

        def _done(data) -> None:
            results = self._filter_released(data.get("results") or [])
            if not results and alts:
                self._search_task_id = tmdb.search_async(self._media_type, alts.pop(0), 1, use_cache=not self._force,
                                                         on_error=on_error, on_done=_done)
                return
            on_done(results)
        return tmdb.search_async(self._media_type, query, 1, use_cache=not self._force, on_error=on_error, on_done=_done)

    def _render_search(self, results: list[dict]) -> None:
        self._search_grid.set_entries([self._wrap(item) for item in results])
        self._search_grid.set_highlight(self._query)
        self._search_grid.show()
        self._view_btn.show()

    _TAG_QUERY_RE = re.compile(r'^\s*tags?\s*[:\uff1a]\s*(.*)$', re.IGNORECASE | re.DOTALL)

    @classmethod
    def _parse_tag_query(cls, text: str):
        match = cls._TAG_QUERY_RE.match(text or '')
        if not match:
            return None
        seen: set = set()
        tags: list = []
        for part in re.split(r'[,;]+', match.group(1)):
            tag = re.sub(r'\s+', ' ', part).strip()
            if tag and tag.lower() not in seen:
                seen.add(tag.lower())
                tags.append(tag)
        return tags

    def _on_search_text_changed(self, _text: str) -> None:
        if self._parse_tag_query(self._search_box.text()) is not None:
            self._search_timer.stop()
            return
        self._search_timer.start()

    def _set_genre_status(self, text: str) -> None:
        self._status_lbl.setText(text)
        self._status_lbl.setVisible(bool(text))

    def _on_genre_pill(self, name: str) -> None:
        self._search_genres([name])

    def _start_resolve_genres(self, names, on_done, on_error):
        return tmdb.resolve_genres_async(self._media_type, names, on_done=on_done, on_error=on_error)

    def _start_genre_page(self, page: int, on_done, on_error):
        def _done(data: dict) -> None:
            total = int(data.get("total_pages") or 1)
            on_done({"page": page, "has_more": page < min(total, 500),
                     "items": self._filter_released(data.get("results") or [])})
        return tmdb.discover_async(self._media_type, list(self._genre_ids), page,
                                   use_cache=not self._force, on_done=_done, on_error=on_error)

    def _search_genres(self, tags: list) -> None:
        if self._stack.currentWidget() is not self._browse_page:
            self._show_grid()
        if not tags:
            self._set_genre_status(tr('tv.genre.hint', default='Type genres after "tag:", e.g.  tag:action, mystery  then press Enter.'))
            return
        self._genre_seq += 1
        seq = self._genre_seq
        self._last_genre_query = list(tags)
        want_inc, self._want_exc = split_tags(tags)
        tags = want_inc + self._want_exc
        self._clear_genre_suggestions()
        self._set_genre_status(tr('tv.genre.looking_up', default='Looking up genres\u2026'))
        try:
            self._start_resolve_genres(
                list(tags),
                on_done=lambda res, s=seq: self._on_genres_resolved(s, res),
                on_error=lambda msg, s=seq: self._on_genres_lookup_failed(s, msg))
        except Exception:
            logger.exception("Failed to start genre lookup")
            self._on_genres_lookup_failed(seq, "")

    def _on_genres_lookup_failed(self, seq: int, message: str) -> None:
        if seq != self._genre_seq or not _qt_is_valid(self):
            return
        logger.error("%s genre lookup failed: %s", self._media_type, message)
        self._set_genre_status(tr('tv.genre.lookup_failed', default='Genre lookup failed. Try again in a moment.'))

    def _on_genres_resolved(self, seq: int, result) -> None:
        if seq != self._genre_seq or not _qt_is_valid(self):
            return
        found = [str(t) for t in (result or {}).get('tags') or ()]
        ids = list((result or {}).get('ids') or [])
        missing = [str(t) for t in (result or {}).get('missing') or ()]
        pool = [str(t) for t in (result or {}).get('pool') or ()]
        if pool:
            self._genre_pool = pool
        if not found:
            names = ', '.join(f'"{m}"' for m in missing) or 'that'
            self._set_genre_status(tr('tv.genre.unknown', default='No genre named {names}. Check the spelling (e.g. tag:action, mystery).', names=names))
            self._show_genre_suggestions(missing, pool)
            return
        self._set_genre_status('')
        if len(ids) == len(found):
            id_of = {n: i for n, i in zip(found, ids)}
        else:
            id_of = {}
        inc, exc = partition_resolved(found, self._want_exc)
        exc_ids = {int(id_of[n]) for n in exc if n in id_of and str(id_of[n]).lstrip('-').isdigit()}
        ids = [id_of[n] for n in inc if n in id_of] if id_of else ids
        found = inc
        if found == self._genre_names and exc == self._genre_excl and self._search_grid.isVisible():
            self._genre_missing = missing
            self._sync_genre_bar()
            self._show_genre_suggestions(missing, pool)
            return
        self._genre_excl = list(exc)
        self._genre_excl_ids = exc_ids
        self._enter_genre_mode(found, ids, missing)
        self._show_genre_suggestions(missing, pool)

    def _open_genre_menu(self) -> None:
        if self._genre_pool:
            show_tag_menu(self._genre_add, self._genre_pool, self._all_genre_tokens(), self._on_genre_menu_pick, self._on_genre_exclude)

    def _all_genre_tokens(self) -> list:
        return join_tags(self._genre_names, self._genre_excl)

    def _on_genre_exclude(self, name: str) -> None:
        self._on_genre_menu_pick(name, True)

    def _on_genre_menu_pick(self, name: str, exclude: bool = False) -> None:
        names = toggled_tags(self._all_genre_tokens(), name, exclude)
        if names:
            self._search_genres(names)
        else:
            self._clear_genres()

    def _clear_genre_suggestions(self) -> None:
        while self._suggest_row.count():
            item = self._suggest_row.takeAt(0)
            if item.widget() is not None:
                item.widget().deleteLater()
        self._suggest_bar.setVisible(False)

    def _show_genre_suggestions(self, missing: list, pool) -> None:
        self._clear_genre_suggestions()
        shown = False
        for name in missing:
            close = _suggest_genres(name, pool)
            if not close:
                continue
            hint = CaptionLabel(tr('tv.genre.did_you_mean', default='Did you mean, instead of "{name}":', name=name),
                                self._suggest_bar)
            apply_label_theme(hint, 'muted')
            self._suggest_row.addWidget(hint)
            for label in close:
                pill = _genre_pill(label, self._suggest_bar, lambda t, m=name: self._retry_genre(m, t))
                pill.adjustSize()
                self._suggest_row.addWidget(pill)
            shown = True
        if shown:
            self._suggest_row.addStretch(1)
        self._suggest_bar.setVisible(shown)

    def _retry_genre(self, missing: str, suggestion: str) -> None:
        def norm(t: str) -> str:
            return re.sub(r'\s+', ' ', t or '').strip().lower()
        query, replaced = [], False
        for t in self._last_genre_query:
            if norm(t.lstrip('-')) == norm(missing):
                query.append(('-' if t.startswith('-') else '') + suggestion)
                replaced = True
            else:
                query.append(t)
        if not replaced:
            query.append(suggestion)
        self._search_genres(query)

    def _enter_genre_mode(self, found: list, ids: list, missing: list) -> None:
        self._cancel_genre_task()
        if self._search_task_id is not None:
            _worker_module.cancel(self._search_task_id)
            self._search_task_id = None
        self._search_timer.stop()
        self._query = ""
        self._genre_names = list(found)
        self._genre_ids = list(ids)
        self._genre_missing = list(missing)
        self._genre_page = 0
        self._genre_has_more = True
        self._genre_loading = False
        self._genre_stale = 0
        self._search_box.blockSignals(True)
        self._search_box.setText('tag:' + ', '.join(self._all_genre_tokens()))
        self._search_box.blockSignals(False)
        self._rows_scroll.hide()
        self._search_grid.clear()
        self._search_grid.show()
        self._view_btn.show()
        self._sync_genre_bar()
        self._load_next_genre_page()

    def _sync_genre_bar(self) -> None:
        while self._genre_pill_row.count():
            item = self._genre_pill_row.takeAt(0)
            if item.widget() is not None:
                item.widget().deleteLater()
        self._genre_note.setText('')
        self._genre_note.setVisible(False)
        self._clear_genre_suggestions()
        on = bool(self._genre_names or self._genre_excl)
        if on:
            for name in self._all_genre_tokens():
                pill = make_tag_chip(name, self._genre_bar, self._on_genre_menu_pick)
                pill.adjustSize()
                self._genre_pill_row.addWidget(pill)
            self._genre_hint.setText(
                (tr('tv.genre.showing_all', default='Showing titles with all of these genres') if len(self._genre_names) > 1
                 else tr('tv.genre.showing', default='Showing titles tagged')) if self._genre_names
                else tr('tv.genre.hiding', default='Hiding titles tagged'))
            if self._genre_missing:
                self._genre_note.setText(tr('tv.genre.ignored', default='Ignored unknown: {names}',
                                            names=', '.join(self._genre_missing)))
                self._genre_note.setVisible(True)
        self._genre_bar.setVisible(on)

    def _cancel_genre_task(self) -> None:
        if self._genre_task_id is not None:
            _worker_module.cancel(self._genre_task_id)
            self._genre_task_id = None

    def _leave_genre_mode(self) -> None:
        self._genre_seq += 1
        self._cancel_genre_task()
        self._genre_names = []
        self._genre_ids = []
        self._genre_excl = []
        self._genre_excl_ids = set()
        self._genre_missing = []
        self._genre_loading = False
        self._sync_genre_bar()

    def _clear_genres(self) -> None:
        self._leave_genre_mode()
        self._query = ""
        self._search_box.blockSignals(True)
        self._search_box.setText('')
        self._search_box.blockSignals(False)
        self._search_grid.hide()
        self._search_grid.clear()
        self._view_btn.hide()
        self._rows_scroll.show()
        self._set_genre_status('')

    def _restart_genre_list(self) -> None:
        self._genre_seq += 1
        self._cancel_genre_task()
        self._genre_page = 0
        self._genre_has_more = True
        self._genre_loading = False
        self._genre_stale = 0
        self._search_grid.clear()
        self._load_next_genre_page()

    def _load_next_genre_page(self) -> None:
        if not _qt_is_valid(self) or self._genre_loading or not self._genre_has_more or not (self._genre_names or self._genre_excl):
            return
        self._genre_loading = True
        seq = self._genre_seq
        try:
            self._genre_task_id = self._start_genre_page(
                self._genre_page + 1,
                on_done=lambda res, s=seq: self._on_genre_page(s, res),
                on_error=lambda msg, s=seq: self._on_genre_page_failed(s, msg))
        except Exception:
            logger.exception("Failed to start genre page load")
            self._on_genre_page_failed(seq, "")

    def _on_genre_page(self, seq: int, result: dict) -> None:
        if seq != self._genre_seq or not _qt_is_valid(self) or not _qt_is_valid(self._search_grid):
            return
        self._genre_task_id = None
        self._genre_loading = False
        page = int(result.get("page") or 0)
        if self._genre_page and page <= self._genre_page:
            return
        self._genre_page = page
        self._genre_has_more = bool(result.get("has_more"))
        raw = result.get("items") or []
        excl = self._genre_excl_ids
        items = [i for i in raw if not excl.intersection(int(g) for g in (i.get("genre_ids") or ()) if str(g).isdigit())] if excl else raw
        if raw and not items and self._genre_has_more:
            QTimer.singleShot(0, self._load_next_genre_page)
            return
        before = self._search_grid.count_entries()
        self._search_grid.append_entries([self._wrap(item) for item in items])
        added = self._search_grid.count_entries() - before
        if added == 0 and items:
            self._genre_stale += 1
            if self._genre_stale >= 3:
                self._genre_has_more = False
        elif added > 0:
            self._genre_stale = 0
        if self._search_grid.count_entries() == 0 and not self._genre_has_more:
            self._set_genre_status(tr('tv.genre.empty', default='No titles found for these genres.'))
        else:
            self._set_genre_status('')
        QTimer.singleShot(150, self._on_genre_near_bottom)

    def _on_genre_page_failed(self, seq: int, message: str) -> None:
        if seq != self._genre_seq or not _qt_is_valid(self):
            return
        self._genre_task_id = None
        self._genre_loading = False
        logger.error("%s genre page load failed: %s", self._media_type, message)
        self._set_genre_status(tr("tv.movies.load_error", default="Failed to load"))

    def _on_genre_near_bottom(self) -> None:
        if not (self._genre_names or self._genre_excl) or not _qt_is_valid(self) or not _qt_is_valid(self._search_grid):
            return
        if self._genre_loading or not self._genre_has_more:
            return
        bar = self._search_grid.verticalScrollBar()
        if bar.maximum() - bar.value() > self._search_grid._NEAR_BOTTOM_THRESHOLD_PX:
            return
        self._load_next_genre_page()

    def _start_random(self, exclude, on_done, on_error):
        return tmdb.random_title_async(self._media_type, exclude,
                                       on_done=lambda res: on_done(res or None), on_error=on_error)

    def _open_random(self, item) -> None:
        self._last_random = item.get("id")
        self._open_details(item, self._media_type)

    def _surprise_me(self) -> None:
        self._random_token += 1
        token = self._random_token
        if self._random_task_id is not None:
            _worker_module.cancel(self._random_task_id)
            self._random_task_id = None
        self._dice_btn.set_busy(True)

        def _finish(item) -> None:
            if token != self._random_token or not _qt_is_valid(self):
                return
            self._random_task_id = None
            self._dice_btn.set_busy(False)
            if not item:
                InfoBar.warning(
                    title=tr("tv.random_failed_title", default="Couldn't pick a title"),
                    content=tr("tv.random_failed", default="Nothing came back this time. Try again in a moment."),
                    parent=self.window(), duration=2500, isClosable=True,
                )
                return
            try:
                self._open_random(item)
            except Exception:
                logger.exception("Failed to open random %s title", self._media_type)

        def _fail(msg) -> None:
            logger.error("%s random pick error: %s", self._media_type, msg)
            _finish(None)

        try:
            self._random_task_id = self._start_random(self._last_random, _finish, _fail)
        except Exception:
            logger.exception("Failed to start random pick")
            _finish(None)

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
        self._dice_btn.setToolTip(tr("tv.random", default="Surprise me"))
        self._genre_clear.setToolTip(tr('tv.genre.back', default='Back to browsing'))
        self._sync_genre_bar()
        self._watchlater_view.apply_locale()

    def shutdown(self) -> None:
        if self._search_task_id is not None:
            _worker_module.cancel(self._search_task_id)
        self._random_token += 1
        if self._random_task_id is not None:
            _worker_module.cancel(self._random_task_id)
            self._random_task_id = None
        self._leave_genre_mode()
        self._details.shutdown()
        self._watchlater_view.shutdown()
        try:
            images.release(self)
        except (TypeError, RuntimeError):
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


async def _resolve_anime_genres(names: list) -> dict:
    return await anime_backend.resolve_genres(names)


async def _browse_anime_genres(tags: list, page: int) -> dict:
    items, more = await anime_backend.browse_genres(tags, page)
    return {"page": page, "has_more": more, "items": [AnimeItem.from_anilist_media(m) for m in items]}


async def _random_anime(exclude: Any) -> list[AnimeItem]:
    media = await anime_backend.random_anime(exclude)
    return [AnimeItem.from_anilist_media(media)] if media else []


async def _search_anime(query: str) -> list[AnimeItem]:
    cleaned = _clean_query(query)
    results = await anime_backend.search_cached(cleaned)
    if not results:
        for alt in _tm_variants(cleaned)[1:]:
            results = await anime_backend.search_cached(alt)
            if results:
                break
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

_SANITIZE_FILENAME_RE = re.compile(r'[\\/:*?"<>|]')
_KEY_IV_RE = re.compile(r"IV=0x([0-9a-fA-F]+)")
_RESOLUTION_DIGITS_RE = re.compile(r"(\d{3,4})")


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
        "/opt/homebrew/bin/ffmpeg",
        "/usr/local/bin/ffmpeg",
        "/usr/bin/ffmpeg",
    ):
        if os.path.isfile(candidate):
            return candidate
    return None

def _download_hls_pure(url: str, referer: Optional[str], headers: dict, dest_path: str, item_id, download_manager, is_cancelled=None) -> None:
    import subprocess
    import tempfile
    from urllib.parse import urljoin

    req_headers = {"User-Agent": _UA}
    if referer:
        req_headers["Referer"] = referer
    req_headers.update(headers)

    _SEGMENT_MAX = 32 << 20
    _KEY_MAX = 64 << 10
    _PLAYLIST_MAX = 4 << 20
    _ATTR_RE = re.compile(r'([A-Z0-9-]+)=("[^"]*"|[^,]*)')
    _WORKERS = 4

    def _attrs(line: str) -> dict[str, str]:
        return {key.upper(): value.strip('"') for key, value in _ATTR_RE.findall(line)}

    def _read(u: str, limit: int) -> bytes:
        buf = bytearray()
        with _ad_hoc_client.stream("GET", u, headers=req_headers, timeout=30) as r:
            r.raise_for_status()
            for chunk in r.iter_bytes(1 << 16):
                if is_cancelled and is_cancelled():
                    raise RuntimeError("Cancelled")
                buf.extend(chunk)
                if len(buf) > limit:
                    raise RuntimeError("HLS resource is too large")
        return bytes(buf)

    def _text(u: str) -> str:
        return _read(u, _PLAYLIST_MAX).decode("utf-8", "replace")

    def _small(u: str, limit: int) -> bytes:
        return _read(u, limit)

    def _parse_media(base: str, text: str):
        map_url = map_key = map_iv = None
        key_url = key_iv = None
        sequence = 0
        segs: list[tuple[str, Optional[str], Optional[bytes]]] = []
        for raw in text.splitlines():
            line = raw.strip()
            if not line:
                continue
            upper = line.upper()
            if upper.startswith("#EXT-X-MEDIA-SEQUENCE:"):
                try:
                    sequence = max(0, int(line.split(":", 1)[1].strip()))
                except ValueError:
                    sequence = 0
            elif upper.startswith("#EXT-X-MAP:"):
                attrs = _attrs(line)
                if attrs.get("URI"):
                    map_url = urljoin(base, attrs["URI"])
                    map_key = key_url
                    map_iv = key_iv or ((sequence.to_bytes(16, "big")) if key_url else None)
            elif upper.startswith("#EXT-X-KEY:"):
                attrs = _attrs(line)
                method = (attrs.get("METHOD") or "NONE").upper()
                key_url = urljoin(base, attrs["URI"]) if method != "NONE" and attrs.get("URI") else None
                match = _KEY_IV_RE.search(line)
                key_iv = bytes.fromhex(match.group(1)) if match else None
            elif not line.startswith("#"):
                iv = key_iv or ((sequence + len(segs)).to_bytes(16, "big") if key_url else None)
                segs.append((urljoin(base, line), key_url, iv))
        return map_url, map_key, map_iv, segs

    def _media(base: str) -> tuple:
        text = _text(base)
        if "#EXT-X-STREAM-INF" in text:
            pending = None
            variants = []
            for raw in text.splitlines():
                line = raw.strip()
                if not line:
                    continue
                if line.upper().startswith("#EXT-X-STREAM-INF:"):
                    pending = _attrs(line)
                elif not line.startswith("#") and pending is not None:
                    pending["URI"] = line
                    variants.append(pending)
                    pending = None
            if variants:
                chosen = max(variants, key=lambda v: int(v.get("BANDWIDTH") or 0))
                base = urljoin(base, chosen["URI"])
                text = _text(base)
        return _parse_media(base, text)

    master_url = url
    master = _text(url)
    audio_url = None
    if "#EXT-X-STREAM-INF" in master:
        audio_groups: dict[str, list[dict]] = {}
        pending = None
        variants = []
        for raw in master.splitlines():
            line = raw.strip()
            if not line:
                continue
            upper = line.upper()
            if upper.startswith("#EXT-X-MEDIA:") and "TYPE=AUDIO" in upper:
                attrs = _attrs(line)
                if attrs.get("URI"):
                    audio_groups.setdefault(attrs.get("GROUP-ID", ""), []).append(attrs)
            elif upper.startswith("#EXT-X-STREAM-INF:"):
                pending = _attrs(line)
            elif not line.startswith("#") and pending is not None:
                pending["URI"] = line
                variants.append(pending)
                pending = None
        if variants:
            chosen = max(variants, key=lambda v: int(v.get("BANDWIDTH") or 0))
            url = urljoin(url, chosen["URI"])
            entries = audio_groups.get(chosen.get("AUDIO", ""), [])
            if entries:
                pick = next((e for e in entries if (e.get("DEFAULT") or "").upper() == "YES"), entries[0])
                audio_url = urljoin(master_url, pick["URI"])
            master = _text(url)

    map_url, map_key, map_iv, seg_urls = _parse_media(url, master)
    if not seg_urls:
        raise RuntimeError("No segments found in HLS playlist")

    key_cache: dict[str, bytes] = {}
    cipher_cls = None
    AES = None
    Cipher = algorithms = modes = None

    def _key(u: str) -> bytes:
        if u not in key_cache:
            key_cache[u] = _small(u, _KEY_MAX)
        return key_cache[u]

    def _decrypt(data: bytes, key_u: str, iv: bytes) -> bytes:
        nonlocal cipher_cls, AES, Cipher, algorithms, modes
        if cipher_cls is None:
            try:
                from Crypto.Cipher import AES
                cipher_cls = "pycryptodome"
            except ImportError:
                try:
                    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
                    cipher_cls = "cryptography"
                except ImportError:
                    raise RuntimeError("Encrypted stream requires the pycryptodome or cryptography package") from None
        if cipher_cls == "pycryptodome":
            out = AES.new(_key(key_u), AES.MODE_CBC, iv).decrypt(data)
        else:
            decryptor = Cipher(algorithms.AES(_key(key_u)), modes.CBC(iv)).decryptor()
            out = decryptor.update(data) + decryptor.finalize()
        pad = out[-1] if out else 0
        if 1 <= pad <= 16 and len(out) >= pad and out.endswith(bytes([pad]) * pad):
            out = out[:-pad]
        return out

    tmpdir = tempfile.mkdtemp(prefix="pirachest-hls-", dir=os.path.dirname(dest_path) or ".")
    downloaded_bytes = 0
    start_time = time.time()
    last_emit = 0.0
    last_bytes = 0
    last_time = start_time
    avg_seg_size = 0.0
    try:
        def _fetch(idx: int, seg: tuple[str, Optional[str], Optional[bytes]]):
            seg_url, key_url, key_iv = seg
            part = os.path.join(tmpdir, f"{idx:06d}.part")
            if key_url:
                data = _small(seg_url, _SEGMENT_MAX)
                if is_cancelled and is_cancelled():
                    raise RuntimeError("Cancelled")
                data = _decrypt(data, key_url, key_iv if key_iv else idx.to_bytes(16, "big"))
                with open(part, "wb") as fh:
                    fh.write(data)
                return part, len(data)
            written = 0
            with _ad_hoc_client.stream("GET", seg_url, headers=req_headers, timeout=30) as resp:
                resp.raise_for_status()
                with open(part, "wb") as fh:
                    for chunk in resp.iter_bytes(1 << 18):
                        if is_cancelled and is_cancelled():
                            raise RuntimeError("Cancelled")
                        written += len(chunk)
                        if written > _SEGMENT_MAX:
                            raise RuntimeError("HLS segment is too large")
                        fh.write(chunk)
            return part, written

        def _write_map(fh, u, key_u, iv):
            if not u:
                return 0
            data = _small(u, _SEGMENT_MAX)
            if key_u:
                data = _decrypt(data, key_u, iv if iv else b"\0" * 16)
            fh.write(data)
            return len(data)

        def _concat(segs, out_path: str, start_frac: float, end_frac: float,
                    start_map=None, start_key=None, start_iv=None) -> int:
            nonlocal downloaded_bytes, last_emit, last_bytes, last_time, avg_seg_size
            total = len(segs)
            written_total = 0
            with open(out_path, "wb") as fh:
                written_total += _write_map(fh, start_map, start_key, start_iv)
                with ThreadPoolExecutor(max_workers=_WORKERS) as pool:
                    futures = {}
                    next_submit = 0
                    for idx in range(total):
                        if is_cancelled and is_cancelled():
                            raise RuntimeError("Cancelled")
                        while next_submit < total and next_submit < idx + _WORKERS:
                            futures[next_submit] = pool.submit(_fetch, next_submit, segs[next_submit])
                            next_submit += 1
                        part, size = futures.pop(idx).result()
                        with open(part, "rb") as src:
                            shutil.copyfileobj(src, fh, 1 << 20)
                        try:
                            os.remove(part)
                        except OSError:
                            pass
                        written_total += size
                        downloaded_bytes += size
                        avg_seg_size = size if idx == 0 else avg_seg_size * 0.8 + size * 0.2
                        now = time.time()
                        if now - last_emit > 0.3 or idx == total - 1:
                            elapsed = now - last_time
                            speed_kbps = ((downloaded_bytes - last_bytes) / 1024) / elapsed if elapsed > 0 else 0.0
                            last_bytes = downloaded_bytes
                            last_time = now
                            last_emit = now
                            progress = (start_frac + (end_frac - start_frac) * (idx + 1) / total) * 100
                            remaining = total - (idx + 1)
                            estimated_total = downloaded_bytes + int(avg_seg_size * remaining)
                            download_manager.update_external(
                                item_id,
                                downloaded_bytes=downloaded_bytes,
                                total_bytes=max(estimated_total, downloaded_bytes),
                                progress=progress,
                                speed_down_kbps=speed_kbps,
                            )
            return written_total

        video_part = os.path.join(tmpdir, "video.ts")
        _concat(seg_urls, video_part, 0.0, 0.85 if audio_url else 1.0, map_url, map_key, map_iv)

        audio_part = None
        if audio_url:
            try:
                a_map, a_key, a_iv, audio_segs = _media(audio_url)
                if audio_segs:
                    audio_part = os.path.join(tmpdir, "audio.ts")
                    _concat(audio_segs, audio_part, 0.85, 1.0, a_map, a_key, a_iv)
            except Exception:
                if is_cancelled and is_cancelled():
                    raise
                logger.warning("HLS audio rendition failed; keeping video track", exc_info=True)
                audio_part = None

        if audio_part and os.path.getsize(audio_part) > 0:
            ffmpeg = _resolve_ffmpeg()
            if ffmpeg:
                kwargs = {"env": _clean_env()}
                if os.name == "nt":
                    startupinfo = subprocess.STARTUPINFO()
                    startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
                    kwargs["startupinfo"] = startupinfo
                    kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
                try:
                    subprocess.run(
                        [ffmpeg, "-y", "-loglevel", "error", "-i", video_part,
                         "-i", audio_part, "-c", "copy", "-f", "mpegts", dest_path],
                        check=True, timeout=900, **kwargs)
                    return dest_path
                except Exception:
                    logger.warning("HLS remux failed; falling back to video only", exc_info=True)
            else:
                logger.warning("HLS stream has a separate audio rendition but ffmpeg is unavailable; "
                               "downloading video only")

        os.replace(video_part, dest_path)
        return dest_path
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


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
    kwargs = {"env": _clean_env()}
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
            popen_kwargs = {"env": _clean_env()}
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
    _NON_PROVIDERS = {"page", "type", "mappings", "_unknownProviders"}
    _GENERIC_TITLE = re.compile(r"(?i)(?:episode|ep\.?)\s*#?\s*\d+(?:\.\d+)?")

    def __init__(self, episodes_by_provider: dict, default_provider: Optional[str], default_audio: str,
                 parent=None, preset_episode: Any = None) -> None:
        super().__init__(parent)
        self._episodes_by_provider = episodes_by_provider
        self._items: list[tuple[dict, QTreeWidgetItem]] = []
        self._preset_episode = _episode_key_number(preset_episode)
        lay = self.viewLayout
        lay.setSpacing(8)
        lay.addWidget(SubtitleLabel(tr("anime.download_dialog_title", default="Download Episodes"), self))

        names = sorted(
            name for name, pdata in episodes_by_provider.items()
            if name not in self._NON_PROVIDERS and isinstance(pdata, dict) and not pdata.get("error")
            and isinstance(pdata.get("episodes"), dict) and pdata["episodes"].get("sub"))

        def combo(entries: list, current: int = 0) -> ComboBox:
            box = ComboBox()
            for label, value in entries:
                box.addItem(label, userData=value)
            box.setCurrentIndex(current)
            return box

        self._provider_combo = combo([(n, n) for n in names], names.index(default_provider) if default_provider in names else 0)
        self._audio_combo = combo([(tr("anime.sub", default="Sub"), "sub"), (tr("anime.dub", default="Dub"), "dub")],
                                  1 if default_audio == "dub" else 0)
        self._quality_combo = combo([
            (tr("anime.quality_best", default="Best available"), "best"), ("1080p", "1080"), ("720p", "720"),
            ("480p", "480"), ("360p", "360"), (tr("anime.quality_worst", default="Worst available"), "worst")])
        self._subtitle_combo = combo([
            (tr("anime.subtitles_none", default="No subtitles"), "none"),
            (tr("anime.subtitles_auto", default="Auto (default track)"), "auto"),
            (tr("anime.subtitles_all", default="All languages"), "all"),
            ("English", "english"), ("Spanish", "spanish"), ("French", "french"), ("German", "german"),
            ("Portuguese", "portuguese"), ("Arabic", "arabic"), ("Japanese", "japanese")])
        grid = QGridLayout()
        grid.setHorizontalSpacing(12)
        grid.setVerticalSpacing(8)
        grid.setColumnStretch(0, 1)
        grid.setColumnStretch(1, 1)
        fields = ((tr("anime.provider", default="Provider"), self._provider_combo),
                  (tr("anime.audio", default="Audio"), self._audio_combo),
                  (tr("anime.quality", default="Quality"), self._quality_combo),
                  (tr("anime.subtitles", default="Subtitles"), self._subtitle_combo))
        self._fields = {}
        for i, (label, widget) in enumerate(fields):
            self._fields[widget] = _meta_field(label, widget, self)
            grid.addWidget(self._fields[widget], i // 2, i % 2)
        lay.addLayout(grid)

        bar = QHBoxLayout()
        self._all_chk = CheckBox(tr("anime.select_all", default="Select All"), self)
        self._all_chk.stateChanged.connect(self._on_select_all)
        self._count_lbl = CaptionLabel("", self)
        apply_label_theme(self._count_lbl, "muted")
        bar.addWidget(self._all_chk)
        bar.addStretch(1)
        bar.addWidget(self._count_lbl)
        lay.addLayout(bar)

        self._tree, box = _check_tree(self, [""], (), False)
        self._tree.setMinimumHeight(180)
        self._tree.itemChanged.connect(self._sync)
        lay.addWidget(box, 1)

        self._error_lbl = CaptionLabel("", self)
        self._error_lbl.setStyleSheet(f"color: {palette()['state_error']};")
        self._error_lbl.hide()
        lay.addWidget(self._error_lbl)

        self.yesButton.setText(tr("anime.download", default="Download"))
        self.yesButton.setIcon(FluentIcon.DOWNLOAD)
        self.cancelButton.setText(tr("anime.cancel", default="Cancel"))
        self.widget.setMinimumWidth(520)

        self._provider_combo.currentIndexChanged.connect(self._on_provider_changed)
        self._audio_combo.currentIndexChanged.connect(self._rebuild_episode_list)
        self._on_provider_changed()

    def _on_provider_changed(self, _index: int = 0) -> None:
        provider = self._provider_combo.currentData()
        listing = (self._episodes_by_provider.get(provider) or {}).get("episodes") or {}
        has_dub = bool(listing.get("dub"))
        if not has_dub:
            self._audio_combo.blockSignals(True)
            self._audio_combo.setCurrentIndex(0)
            self._audio_combo.blockSignals(False)
        self._fields[self._audio_combo].setVisible(has_dub)
        self._fields[self._quality_combo].setVisible(provider in _QUALITY_CAPABLE_PROVIDERS)
        self._fields[self._subtitle_combo].setVisible(provider in _SUBTITLE_CAPABLE_PROVIDERS)
        self._rebuild_episode_list()

    def _on_select_all(self, _state) -> None:
        state = Qt.CheckState.Checked if self._all_chk.isChecked() else Qt.CheckState.Unchecked
        self._tree.blockSignals(True)
        for _ep, item in self._items:
            item.setCheckState(0, state)
        self._tree.blockSignals(False)
        self._sync()

    def _sync(self, *_args) -> None:
        picked = sum(item.checkState(0) == Qt.CheckState.Checked for _ep, item in self._items)
        self._all_chk.blockSignals(True)
        self._all_chk.setChecked(bool(self._items) and picked == len(self._items))
        self._all_chk.blockSignals(False)
        self._count_lbl.setText(f"{picked} / {len(self._items)}")
        self._error_lbl.hide()

    def validate(self) -> bool:
        if any(item.checkState(0) == Qt.CheckState.Checked for _ep, item in self._items):
            return True
        self._error_lbl.setText(tr("anime.select_episode_error", default="Select an episode to download"))
        self._error_lbl.show()
        return False

    def _rebuild_episode_list(self) -> None:
        self._tree.blockSignals(True)
        self._tree.clear()
        self._items = []
        listing = (self._episodes_by_provider.get(self._provider_combo.currentData()) or {}).get("episodes")
        for ep in (listing.get(self._audio_combo.currentData() or "sub", []) if isinstance(listing, dict) else []):
            label = _episode_number_label(ep.get("number"))
            title = (ep.get("title") or "").strip()
            if title and not self._GENERIC_TITLE.fullmatch(title):
                label = f"{label}  \u00b7  {title}"
            item = QTreeWidgetItem(self._tree, [label])
            item.setToolTip(0, label)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            preset = self._preset_episode is not None and _episode_key_number(ep.get("number")) == self._preset_episode
            item.setCheckState(0, Qt.CheckState.Checked if preset else Qt.CheckState.Unchecked)
            self._items.append((ep, item))
        self._tree.blockSignals(False)
        self._sync()

    def selected(self) -> tuple[Optional[str], str, list[dict], str, str]:
        return (self._provider_combo.currentData(), self._audio_combo.currentData() or "sub",
                [ep for ep, item in self._items if item.checkState(0) == Qt.CheckState.Checked],
                self._quality_combo.currentData() or "best", self._subtitle_combo.currentData() or "none")


class AnimeDetailView(_DetailsChrome, QWidget):
    back_requested = Signal()
    download_requested = Signal(object, str, str, object, str, str)
    provider_requested = Signal(str)
    season_requested = Signal(object)
    banner_changed = Signal()
    genre_requested = Signal(str)

    def _set_genre_pills(self, genres: list) -> None:
        for p in self._genre_pills:
            p.deleteLater()
        self._genre_pills = []
        for g in genres:
            pill = _genre_pill(str(g), self._genres_box, self.genre_requested.emit)
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
        self._content_layout = content_layout
        self._content_margins = (0, 0, 4, 20)
        content_layout.addLayout(top_row)
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

        self._play_pool = None
        self._tried_urls: set = set()
        self._player_host = (content, content_layout)
        self._player_widget = self._make_player(content)
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

    def _make_player(self, parent: QWidget) -> AnimePlayerWidget:
        player = AnimePlayerWidget(parent)
        player.alt_stream_requested.connect(self._on_alt_stream)
        player.stream_failed.connect(self._on_player_stream_failed)
        player.ended.connect(self._on_player_ended)
        player.next_requested.connect(self._play_next_episode)
        player.close_requested.connect(self._close_player)
        player.set_closable(True)
        player.hide()
        return player

    def _replace_player(self) -> None:
        content, layout = self._player_host
        old = self._player_widget
        index = max(layout.indexOf(old), 0)
        layout.removeWidget(old)
        old.hide()
        new = self._player_widget = self._make_player(content)
        layout.insertWidget(index, new, 0, Qt.AlignmentFlag.AlignHCenter)
        old.destroy()
        release_input_state()

    def _close_player(self) -> None:
        if self._stream_task_id is not None:
            _worker_module.cancel(self._stream_task_id)
            self._stream_task_id = None
        self._pending_play_number = None
        self._now_episode = None
        self._play_pool = None
        self._tried_urls = set()
        self._player_widget.stop()
        self._player_widget.hide()
        QTimer.singleShot(0, self._replace_player)
        window = self.window()
        window.activateWindow()
        release_input_state()
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
        listing = (self._episodes_by_provider.get(self._current_provider) or {}).get("episodes") or {}
        only = listing.get(self._current_audio) or listing.get("sub") or []
        if preset_episode is None and len(only) == 1 and self._current_provider:
            audio = self._current_audio if listing.get(self._current_audio) else "sub"
            self.download_requested.emit(self._media, self._current_provider, audio, list(only), "best", "none")
            return
        def _pick() -> None:
            window = self.window()
            window.activateWindow()
            dialog = AnimeDownloadDialog(self._episodes_by_provider, self._current_provider, self._current_audio,
                                         parent=window, preset_episode=preset_episode)

            def _fin(result: int) -> None:
                chosen = dialog.selected() if result == QDialog.DialogCode.Accepted else None
                dialog.deleteLater()
                if chosen and chosen[0] and chosen[2]:
                    self.download_requested.emit(self._media, *chosen)
            dialog.finished.connect(_fin)
            _show_picker(dialog)
        QTimer.singleShot(0, _pick)

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

    def _start_resolve_genres(self, names, on_done, on_error):
        return _worker_module.submit_coro(_resolve_anime_genres, args=(names,), on_done=on_done, on_error=on_error)

    def _start_genre_page(self, page: int, on_done, on_error):
        return _worker_module.submit_coro(_browse_anime_genres, args=(list(self._genre_names), page),
                                          on_done=on_done, on_error=on_error)

    def _start_random(self, exclude, on_done, on_error):
        return _worker_module.submit_coro(_random_anime, args=(exclude,),
                                          on_done=lambda res: on_done(res[0] if res else None), on_error=on_error)

    def _open_random(self, item) -> None:
        self._last_random = _safe_int(item.id)
        self._on_anime_clicked(item)

    def _open_details(self, item, media_type: str = "anime") -> None:
        self._on_anime_clicked(item)

    def _show_grid(self) -> None:
        self._detail_view.banner_active = False
        self._detail_view.banner_changed.emit()
        self._switch_stack_animated(self._browse_page)

    def set_download_manager(self, manager) -> None:
        self._download_manager = manager

    def _on_download_requested(self, media: dict, provider: str, audio: str, episodes: list, quality: str, subtitle_lang: str) -> None:
        manager = _resolve_manager(self)
        if manager is None:
            InfoBar.error(title=tr("anime.download_failed_title", default="Download failed"),
                          content=tr("tv.download.no_manager", default="The download manager is not available."),
                          parent=self.window(), duration=4000, isClosable=True)
            return
        anilist_id = media.get("id")
        title_obj = media.get("title") or {}
        title = title_obj.get("english") or title_obj.get("romaji") or "Anime"
        dest_dir = os.path.join(settings.download_dir_anime, _sanitize_filename(title))

        for ep in episodes:
            label = _episode_number_label(ep.get("number"))
            item_id = manager.add_external(
                game_name=f"{title} - {label}", console=title, source=provider, category="anime",
            )

            def _on_done(path: str, iid=item_id) -> None:
                manager.complete_external(iid, path)

            def _on_err(msg: str, iid=item_id) -> None:
                manager.fail_external(iid, msg)

            task_id = _worker_module.submit(
                _do_download_anime_episode,
                args=(manager, item_id, anilist_id, provider, audio, ep, dest_dir),
                kwargs={"quality": quality, "subtitle_lang": subtitle_lang},
                on_done=_on_done, on_error=_on_err,
            )
            manager.register_external_cancel(item_id, lambda tid=task_id: _worker_module.cancel(tid))

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
        self._dice_btn = _DiceButton(tr('tv.random', default='Surprise me'), self)
        self._dice_btn.clicked.connect(self._surprise_me)
        header.addWidget(self._dice_btn)
        self._last_random: Optional[str] = None
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

    def _surprise_me(self) -> None:
        store = watch_later_store()
        entries = [e for e in store.all() if e.get('raw')]
        if not entries:
            InfoBar.info(
                title=tr('tv.watchlater_random_empty_title', default='Nothing to pick from'),
                content=tr('tv.watchlater_random_empty', default='Save something to Watch later first.'),
                parent=self.window(), duration=2500, isClosable=True,
            )
            return
        pool = [e for e in entries if store.entry_key(e) != self._last_random] or entries
        entry = _RNG.choice(pool)
        self._last_random = store.entry_key(entry)
        self.open_requested.emit(entry)

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
        self._dice_btn.setToolTip(tr('tv.random', default='Surprise me'))
        self._title_lbl.setText(tr('tv.watchlater', default='Watch later'))
        self._empty_lbl.setText(tr('tv.watchlater_empty',
                                   default='Nothing saved yet. Use the watch later button on a movie, show, or anime.'))
        self.refresh()

    def shutdown(self) -> None:
        try:
            images.release(self)
        except (TypeError, RuntimeError):
            pass


class TV_page(MediaPageBase):
    _TABS = (
        ('movies', FluentIcon.MOVIE, 'tv.movies'),
        ('series', FluentIcon.VIDEO, 'tv.series'),
        ('anime', FluentIcon.VIDEO, 'tv.anime'),
        ('channels', FluentIcon.TILES, 'tv.channels'),
    )

    _TOP = 12

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName('tvPage')
        self.setStyleSheet('background: transparent;')
        self._download_manager: Any = None
        outer = QVBoxLayout(self)
        outer.setContentsMargins(24, self._TOP, 24, 0)
        outer.setSpacing(12)

        self._pivot = Pivot()
        self._header = QWidget(self)
        header_layout = QVBoxLayout(self._header)
        header_layout.setContentsMargins(0, 0, 0, 0)
        header_layout.addWidget(self._pivot)
        outer.addWidget(self._header)
        self._floating = False
        self._pivot_shown = True
        self._inset = -1

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

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._sync_top()

    def _sync_top(self) -> None:
        tab = self._stack.currentWidget()
        details = getattr(tab, '_details', None)
        inner = getattr(tab, '_stack', None)
        height = self._header.sizeHint().height() if self._pivot_shown else 0
        active = (details is not None and inner is not None and height > 0
                  and inner.currentWidget() is details and details.isVisible())
        layout = self.layout()
        margins = layout.contentsMargins()
        if active != self._floating:
            self._floating = active
            layout.setContentsMargins(margins.left(), 0 if active else self._TOP, margins.right(), margins.bottom())
            if active:
                layout.removeWidget(self._header)
            else:
                layout.insertWidget(0, self._header)
        if active:
            self._header.setGeometry(margins.left(), self._TOP - details.banner_offset(),
                                     self.width() - margins.left() - margins.right(), height)
            self._header.raise_()
        inset = height + self._TOP + layout.spacing() if height else 0
        if inset != self._inset:
            self._inset = inset
            for child in self._tabs.values():
                view = getattr(child, '_details', None)
                if hasattr(view, 'set_top_inset'):
                    view.set_top_inset(inset)
        self.update()

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
            details.banner_changed.connect(self._sync_top)
            if hasattr(details, 'set_top_inset') and self._inset > 0:
                details.set_top_inset(self._inset)
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
        self._pivot_shown = len(enabled) > 1
        self._header.setVisible(self._pivot_shown)
        self._pivot.setVisible(self._pivot_shown)
        self._pivot.setCurrentItem(current)
        tab = self._ensure_tab(current)
        if tab is not None:
            self._switch_stack_animated(tab)
        self._current_tab_key = current
        if tab is not None and hasattr(tab, 'activate'):
            tab.activate()
        self._sync_top()

    def _on_tab_visibility_changed(self, scope: str) -> None:
        if scope == 'tv':
            self._rebuild_tab_selector()

    def _on_tab_changed(self, key: str) -> None:
        self._current_tab_key = key
        tab = self._ensure_tab(key)
        if tab is None:
            return
        self._switch_stack_animated(tab)
        if hasattr(tab, 'activate'):
            tab.activate()
        self._sync_top()

    def open_detail(self, item: dict, media_kind: str = 'show') -> None:
        from src.core.config import is_tab_enabled
        tab_key = 'movies' if media_kind == 'movie' else 'series'
        if not is_tab_enabled('tv', tab_key):
            return
        tab = self._ensure_tab(tab_key)
        if tab is None or not hasattr(tab, '_open_details'):
            return
        self._pivot.setCurrentItem(tab_key)
        self._switch_stack_animated(tab)
        self._current_tab_key = tab_key
        tmdb_media_type = 'movie' if media_kind == 'movie' else 'tv'
        tab._open_details(item, tmdb_media_type)
        self._sync_top()

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
        self._switch_stack_animated(tab)
        self._current_tab_key = key
        if hasattr(tab, 'activate'):
            tab.activate()
        self._sync_top()
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
