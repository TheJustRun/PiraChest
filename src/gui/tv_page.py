from __future__ import annotations
import logging
import os
from typing import Optional

from PySide6.QtCore import Qt, QSize, QTimer
from PySide6.QtGui import QIcon, QPixmap, QFont, QResizeEvent
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QStackedWidget, QListWidgetItem,
    QFileDialog, QDialog, QLineEdit, QFormLayout, QDialogButtonBox, QSizePolicy,
)
from qfluentwidgets import (
    Pivot, FluentIcon, IconWidget, BodyLabel, SearchLineEdit, ComboBox, PrimaryPushButton,
    PushButton, TransparentToolButton, StrongBodyLabel, InfoBar, ListWidget,
)

from src.core import worker as _worker_module
from src.core.artwork import artwork, has_thumb, thumb_path
from src.core.tv import channels as channels_backend
from src.core.translations import tr, register_locale_refresh
from .anime_page import AnimePlayerWidget

logger = logging.getLogger(__name__)

_ALL = "__all__"
_PAGE_SIZE = 40
_ICON_SIZE = 40
_CHANNEL_FONT_SIZE = 11


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


class _AddSourceDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle(tr("tv.channels.add_source", default="Add source"))
        layout = QVBoxLayout(self)

        form = QFormLayout()
        self._name_edit = QLineEdit(self)
        self._url_edit = QLineEdit(self)
        form.addRow(tr("tv.channels.source_name", default="Name"), self._name_edit)
        form.addRow(tr("tv.channels.source_url", default="M3U URL or path"), self._url_edit)
        layout.addLayout(form)

        browse_row = QHBoxLayout()
        browse_btn = PushButton(tr("tv.channels.browse_file", default="Import local file..."), self)
        browse_btn.clicked.connect(self._on_browse)
        browse_row.addWidget(browse_btn)
        browse_row.addStretch(1)
        layout.addLayout(browse_row)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel, self)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _on_browse(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, tr("tv.channels.browse_file", default="Import local file..."), "", "M3U (*.m3u *.m3u8)",
        )
        if path:
            self._url_edit.setText(path)
            if not self._name_edit.text().strip():
                self._name_edit.setText(os.path.splitext(os.path.basename(path))[0])

    def result_source(self) -> Optional[dict]:
        name = self._name_edit.text().strip()
        location = self._url_edit.text().strip()
        if not location:
            return None
        if os.path.isfile(location):
            return channels_backend.sources.add_file_source(name, location)
        return channels_backend.sources.add_url_source(name, location)


class _ManageSourcesDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle(tr("tv.channels.manage_sources", default="Manage sources"))
        self.setMinimumWidth(360)
        layout = QVBoxLayout(self)
        self._list = ListWidget(self)
        layout.addWidget(self._list, 1)
        self._reload()

        btn_row = QHBoxLayout()
        remove_btn = PushButton(tr("tv.channels.remove_source", default="Remove selected"), self)
        remove_btn.clicked.connect(self._on_remove)
        btn_row.addWidget(remove_btn)
        btn_row.addStretch(1)
        close_btn = PrimaryPushButton(tr("tv.channels.close", default="Close"), self)
        close_btn.clicked.connect(self.accept)
        btn_row.addWidget(close_btn)
        layout.addLayout(btn_row)

    def _reload(self) -> None:
        try:
            self._list.itemChanged.disconnect(self._on_item_changed)
        except TypeError:
            pass
        self._list.clear()
        for source in channels_backend.sources.list_sources():
            item = QListWidgetItem(f"{source['name']} ({source['kind']})")
            item.setData(Qt.ItemDataRole.UserRole, source["id"])
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(Qt.CheckState.Checked if source.get("enabled", True) else Qt.CheckState.Unchecked)
            self._list.addItem(item)
        self._list.itemChanged.connect(self._on_item_changed)

    def _on_item_changed(self, item: QListWidgetItem) -> None:
        source_id = item.data(Qt.ItemDataRole.UserRole)
        channels_backend.sources.set_enabled(source_id, item.checkState() == Qt.CheckState.Checked)

    def _on_remove(self) -> None:
        item = self._list.currentItem()
        if item is None:
            return
        source_id = item.data(Qt.ItemDataRole.UserRole)
        channels_backend.sources.remove_source(source_id)
        self._reload()


class _CountrySelectionDialog(QDialog):
    def __init__(self, countries: list[dict], selected: Optional[set[str]], parent=None):
        super().__init__(parent)
        self.setWindowTitle(tr("tv.channels.select_countries", default="Select countries"))
        self.setMinimumSize(360, 480)
        layout = QVBoxLayout(self)

        btn_row = QHBoxLayout()
        all_btn = PushButton(tr("tv.channels.select_all", default="Select all"), self)
        all_btn.clicked.connect(self._select_all)
        btn_row.addWidget(all_btn)
        none_btn = PushButton(tr("tv.channels.select_none", default="Select none"), self)
        none_btn.clicked.connect(self._select_none)
        btn_row.addWidget(none_btn)
        btn_row.addStretch(1)
        layout.addLayout(btn_row)

        self._search_box = QLineEdit(self)
        self._search_box.setPlaceholderText(tr("tv.channels.search_countries", default="Search countries..."))
        self._search_box.textChanged.connect(self._on_search)
        layout.addWidget(self._search_box)

        self._list = ListWidget(self)
        layout.addWidget(self._list, 1)
        for country in sorted(countries, key=lambda c: c["name"].lower()):
            item = QListWidgetItem(country["name"])
            item.setData(Qt.ItemDataRole.UserRole, country["code"])
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            checked = selected is None or country["code"] in selected
            item.setCheckState(Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked)
            self._list.addItem(item)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel, self)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _select_all(self) -> None:
        for i in range(self._list.count()):
            if not self._list.item(i).isHidden():
                self._list.item(i).setCheckState(Qt.CheckState.Checked)

    def _select_none(self) -> None:
        for i in range(self._list.count()):
            if not self._list.item(i).isHidden():
                self._list.item(i).setCheckState(Qt.CheckState.Unchecked)

    def _on_search(self, text: str) -> None:
        query = text.strip().lower()
        for i in range(self._list.count()):
            item = self._list.item(i)
            item.setHidden(bool(query) and query not in item.text().lower())

    def selected_codes(self) -> list[str]:
        return [
            self._list.item(i).data(Qt.ItemDataRole.UserRole)
            for i in range(self._list.count())
            if self._list.item(i).checkState() == Qt.CheckState.Checked
        ]


class _ChannelsTab(QWidget):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._all_channels: list[dict] = []
        self._filtered_channels: list[dict] = []
        self._page = 0
        self._refresh_task_id: Optional[str] = None
        self._current_channel_id: Optional[str] = None
        self._logo_items: dict[str, list[QListWidgetItem]] = {}
        self._loaded = False

        outer = QHBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(16)

        left = QWidget(self)
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.setSpacing(10)

        top_row = QHBoxLayout()
        self._search_box = SearchLineEdit(left)
        self._search_box.setPlaceholderText(tr("tv.channels.search_placeholder", default="Search channels..."))
        self._search_box.textChanged.connect(self._apply_filters)
        self._search_box.searchSignal.connect(self._apply_filters)
        self._search_box.clearSignal.connect(self._apply_filters)
        top_row.addWidget(self._search_box, 1)

        self._add_btn = TransparentToolButton(FluentIcon.ADD, left)
        self._add_btn.setToolTip(tr("tv.channels.add_source", default="Add source"))
        self._add_btn.clicked.connect(self._on_add_source)
        top_row.addWidget(self._add_btn)

        self._manage_btn = TransparentToolButton(FluentIcon.SETTING, left)
        self._manage_btn.setToolTip(tr("tv.channels.manage_sources", default="Manage sources"))
        self._manage_btn.clicked.connect(self._on_manage_sources)
        top_row.addWidget(self._manage_btn)

        self._countries_btn = TransparentToolButton(FluentIcon.FILTER, left)
        self._countries_btn.setToolTip(tr("tv.channels.select_countries", default="Select countries"))
        self._countries_btn.clicked.connect(self._on_select_countries)
        top_row.addWidget(self._countries_btn)

        self._refresh_btn = TransparentToolButton(FluentIcon.SYNC, left)
        self._refresh_btn.setToolTip(tr("tv.channels.refresh", default="Refresh"))
        self._refresh_btn.clicked.connect(lambda: self._load_channels(use_cache=False))
        top_row.addWidget(self._refresh_btn)

        left_layout.addLayout(top_row)

        filter_row = QHBoxLayout()
        self._source_combo = ComboBox(left)
        self._source_combo.currentIndexChanged.connect(self._apply_filters)
        filter_row.addWidget(self._source_combo, 1)

        self._country_combo = ComboBox(left)
        self._country_combo.currentIndexChanged.connect(self._apply_filters)
        filter_row.addWidget(self._country_combo, 1)

        self._group_combo = ComboBox(left)
        self._group_combo.currentIndexChanged.connect(self._apply_filters)
        filter_row.addWidget(self._group_combo, 1)

        left_layout.addLayout(filter_row)

        self._status_lbl = BodyLabel("", left)
        self._status_lbl.hide()
        left_layout.addWidget(self._status_lbl)

        self._list = ListWidget(left)
        self._list.setIconSize(QSize(_ICON_SIZE, _ICON_SIZE))
        self._list.setSpacing(4)
        self._list.setUniformItemSizes(True)
        list_font = QFont()
        list_font.setPointSize(_CHANNEL_FONT_SIZE)
        self._list.setFont(list_font)
        self._list.itemClicked.connect(self._on_channel_clicked)
        self._list.verticalScrollBar().valueChanged.connect(self._schedule_visible_logos)
        left_layout.addWidget(self._list, 1)

        self._visible_logo_timer = QTimer(self)
        self._visible_logo_timer.setSingleShot(True)
        self._visible_logo_timer.setInterval(150)
        self._visible_logo_timer.timeout.connect(self._load_visible_logos)

        pager_row = QHBoxLayout()
        self._prev_btn = TransparentToolButton(FluentIcon.LEFT_ARROW, left)
        self._prev_btn.clicked.connect(self._prev_page)
        pager_row.addWidget(self._prev_btn)
        self._page_lbl = BodyLabel("", left)
        self._page_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        pager_row.addWidget(self._page_lbl, 1)
        self._next_btn = TransparentToolButton(FluentIcon.RIGHT_ARROW, left)
        self._next_btn.clicked.connect(self._next_page)
        pager_row.addWidget(self._next_btn)
        left_layout.addLayout(pager_row)

        left.setMaximumWidth(460)
        outer.addWidget(left, 0)

        right = QWidget(self)
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(24, 24, 24, 24)

        self._now_playing_lbl = StrongBodyLabel("", right)
        self._now_playing_lbl.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        right_layout.addWidget(self._now_playing_lbl, 0, Qt.AlignmentFlag.AlignHCenter)

        right_layout.addStretch(1)

        self._player = AnimePlayerWidget(right)
        self._player.setMinimumSize(320, 180)
        self._player.set_live_mode(True)
        right_layout.addWidget(self._player, 0, Qt.AlignmentFlag.AlignCenter)

        right_layout.addStretch(1)

        right.resizeEvent = self._make_right_resize_handler(right)
        outer.addWidget(right, 1)
        self._right_container = right

        QTimer.singleShot(0, lambda: right.resizeEvent(QResizeEvent(right.size(), right.size())))

        artwork.thumb_ready.connect(self._on_logo_ready)
        register_locale_refresh(self, self._apply_locale)

    def _make_right_resize_handler(self, right: QWidget):
        label_h = 28
        margin = 24 * 2
        spacing = 8

        def _resize(event: QResizeEvent) -> None:
            QWidget.resizeEvent(right, event)
            avail_w = max(320, right.width() - margin)
            avail_h = max(180, right.height() - margin - label_h - spacing)
            target_w = avail_w
            target_h = int(target_w * 9 / 16)
            if target_h > avail_h:
                target_h = avail_h
                target_w = int(target_h * 16 / 9)
            self._player.setFixedSize(target_w, target_h)

        return _resize

    def activate(self) -> None:
        if not self._loaded:
            self._loaded = True
            self._prompt_country_selection(initial=True)

    def _prompt_country_selection(self, initial: bool = False) -> None:
        self._status_lbl.setText(tr("tv.channels.loading_countries", default="Loading country list..."))
        self._status_lbl.show()

        def _on_done(countries: list) -> None:
            self._status_lbl.hide()
            if not countries:
                self._load_channels(use_cache=True)
                return
            selected = channels_backend.get_selected_countries()
            selected_set = set(selected) if selected is not None else None
            dialog = _CountrySelectionDialog(countries, selected_set, self.window())
            if dialog.exec() == QDialog.DialogCode.Accepted:
                channels_backend.set_selected_countries(dialog.selected_codes())
            elif initial and selected is None:
                channels_backend.set_selected_countries([c["code"] for c in countries])
            self._load_channels(use_cache=True)

        def _on_error(msg: str) -> None:
            self._status_lbl.hide()
            logger.error("Failed to load country list: %s", msg)
            self._load_channels(use_cache=True)

        _worker_module.submit(channels_backend.get_available_countries, on_done=_on_done, on_error=_on_error)

    def _on_select_countries(self) -> None:
        self._prompt_country_selection(initial=False)

    def _load_channels(self, use_cache: bool = True) -> None:
        if self._refresh_task_id is not None:
            _worker_module.cancel(self._refresh_task_id)

        self._status_lbl.setText(tr("tv.channels.loading", default="Loading channels..."))
        self._status_lbl.show()

        def _on_done(channels: list) -> None:
            self._refresh_task_id = None
            self._all_channels = channels
            self._status_lbl.hide()
            self._populate_filters()
            self._apply_filters()

        def _on_error(msg: str) -> None:
            self._refresh_task_id = None
            self._status_lbl.setText(tr("tv.channels.load_error", default="Failed to load channels"))
            logger.error("Channel load error: %s", msg)

        self._refresh_task_id = _worker_module.submit(
            channels_backend.fetch_all_channels, kwargs={"use_cache": use_cache}, on_done=_on_done, on_error=_on_error,
        )

    def _fill_combo(self, combo: ComboBox, items: list[tuple[str, str]]) -> None:
        combo.blockSignals(True)
        current = combo.currentData()
        combo.clear()
        for value, label in items:
            combo.addItem(label, userData=value)
        idx = combo.findData(current) if current is not None else 0
        combo.setCurrentIndex(idx if idx >= 0 else 0)
        combo.blockSignals(False)

    def _populate_filters(self) -> None:
        sources_list = channels_backend.sources.list_sources()
        self._fill_combo(
            self._source_combo,
            [(_ALL, tr("tv.channels.all_sources", default="All sources"))] + [(s["id"], s["name"]) for s in sources_list],
        )

        countries = sorted({c["country"] for c in self._all_channels if c.get("country")})
        self._fill_combo(
            self._country_combo,
            [(_ALL, tr("tv.channels.all_countries", default="All countries"))] + [(c, c) for c in countries],
        )

        groups = sorted({c["group"] for c in self._all_channels if c.get("group")})
        self._fill_combo(
            self._group_combo,
            [(_ALL, tr("tv.channels.all_groups", default="All groups"))] + [(g, g) for g in groups],
        )

    def _logo_icon(self, ch: dict) -> QIcon:
        url = ch.get("logo")
        if url and has_thumb("channel", url):
            pix = QPixmap(thumb_path("channel", url))
            if not pix.isNull():
                return QIcon(pix)
        return QIcon()

    def _apply_filters(self, *_args) -> None:
        query = self._search_box.text().strip().lower()
        source_filter = self._source_combo.currentData() or _ALL
        country_filter = self._country_combo.currentData() or _ALL
        group_filter = self._group_combo.currentData() or _ALL

        filtered = []
        for ch in self._all_channels:
            if source_filter != _ALL and ch["source_id"] != source_filter:
                continue
            if country_filter != _ALL and ch.get("country") != country_filter:
                continue
            if group_filter != _ALL and ch.get("group") != group_filter:
                continue
            if query and query not in ch["name"].lower() and query not in (ch.get("group") or "").lower():
                continue
            filtered.append(ch)

        self._filtered_channels = filtered
        self._page = 0
        self._render_page()

    def _page_count(self) -> int:
        return max(1, (len(self._filtered_channels) + _PAGE_SIZE - 1) // _PAGE_SIZE)

    def _prev_page(self) -> None:
        if self._page > 0:
            self._page -= 1
            self._render_page()

    def _next_page(self) -> None:
        if self._page < self._page_count() - 1:
            self._page += 1
            self._render_page()

    def _render_page(self) -> None:
        self._list.clear()
        self._logo_items = {}

        start = self._page * _PAGE_SIZE
        page_items = self._filtered_channels[start:start + _PAGE_SIZE]

        for ch in page_items:
            label = ch["name"]
            if ch.get("group"):
                label = f"{label} — {ch['group']}"
            item = QListWidgetItem(label)
            item.setSizeHint(QSize(0, _ICON_SIZE + 12))
            item.setData(Qt.ItemDataRole.UserRole, ch)
            item.setIcon(self._logo_icon(ch))
            self._list.addItem(item)

        total_pages = self._page_count()
        self._page_lbl.setText(f"{self._page + 1} / {total_pages}")
        self._prev_btn.setEnabled(self._page > 0)
        self._next_btn.setEnabled(self._page < total_pages - 1)

        self._schedule_visible_logos()

    def _schedule_visible_logos(self, *_args) -> None:
        self._visible_logo_timer.start()

    def _load_visible_logos(self) -> None:
        for row in range(self._list.count()):
            item = self._list.item(row)
            if item.data(Qt.ItemDataRole.UserRole + 1):
                continue
            ch = item.data(Qt.ItemDataRole.UserRole)
            url = ch.get("logo") if ch else None
            if not url:
                continue
            item.setData(Qt.ItemDataRole.UserRole + 1, True)
            if has_thumb("channel", url):
                item.setIcon(self._logo_icon(ch))
                continue
            self._logo_items.setdefault(url, []).append(item)
            artwork.request("channel", url)

    def _on_logo_ready(self, kind: str, url: str, path: str) -> None:
        if kind != "channel":
            return
        items = self._logo_items.get(url)
        if not items:
            return
        pix = QPixmap(path)
        if pix.isNull():
            return
        icon = QIcon(pix)
        for item in items:
            item.setIcon(icon)

    def _on_channel_clicked(self, item: QListWidgetItem) -> None:
        ch = item.data(Qt.ItemDataRole.UserRole)
        if not ch or ch["id"] == self._current_channel_id:
            return
        self._current_channel_id = ch["id"]
        self._now_playing_lbl.setText(ch["name"])
        self._player.play_stream(ch["url"], referer=None, headers=None, title=ch["name"])

    def _on_add_source(self) -> None:
        dialog = _AddSourceDialog(self.window())
        if dialog.exec() == QDialog.DialogCode.Accepted:
            source = dialog.result_source()
            if source is not None:
                InfoBar.success(
                    title=tr("tv.channels.source_added", default="Source added"),
                    content=source["name"], parent=self.window(), duration=2000, isClosable=True,
                )
                self._load_channels(use_cache=False)

    def _on_manage_sources(self) -> None:
        dialog = _ManageSourcesDialog(self.window())
        dialog.exec()
        self._load_channels(use_cache=True)

    def _apply_locale(self, *_args) -> None:
        self._search_box.setPlaceholderText(tr("tv.channels.search_placeholder", default="Search channels..."))

    def shutdown(self) -> None:
        if self._refresh_task_id is not None:
            _worker_module.cancel(self._refresh_task_id)
            self._refresh_task_id = None
        try:
            artwork.thumb_ready.disconnect(self._on_logo_ready)
        except (TypeError, RuntimeError):
            pass
        self._player.shutdown()


class TV_page(QWidget):
    _TABS = (
        ('movies', FluentIcon.MOVIE, 'tv.movies'),
        ('series', FluentIcon.VIDEO, 'tv.series'),
        ('sports', FluentIcon.BASKETBALL, 'tv.sports'),
        ('channels', FluentIcon.TILES, 'tv.channels'),
    )

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName('tvPage')
        self.setStyleSheet('background: transparent;')
        outer = QVBoxLayout(self)
        outer.setContentsMargins(24, 12, 24, 20)
        outer.setSpacing(12)

        self._pivot = Pivot()
        outer.addWidget(self._pivot)

        self._stack = QStackedWidget()
        outer.addWidget(self._stack, 1)

        self._tabs: dict[str, QWidget] = {}
        for key, icon, text_key in self._TABS:
            tab = _ChannelsTab(self) if key == 'channels' else _TVTabPlaceholder(icon, text_key, self)
            self._tabs[key] = tab
            self._stack.addWidget(tab)
            self._pivot.addItem(routeKey=key, text=tr(text_key))

        self._pivot.currentItemChanged.connect(self._on_tab_changed)
        first_key = self._TABS[0][0]
        self._pivot.setCurrentItem(first_key)
        self._stack.setCurrentWidget(self._tabs[first_key])

        register_locale_refresh(self, self._apply_locale)

    def _on_tab_changed(self, key: str) -> None:
        self._stack.setCurrentWidget(self._tabs[key])
        if key == 'channels':
            self._tabs[key].activate()

    def _apply_locale(self, *_args) -> None:
        for key, _icon, text_key in self._TABS:
            self._pivot.setItemText(key, tr(text_key))
            if hasattr(self._tabs[key], 'apply_locale'):
                self._tabs[key].apply_locale()

    def shutdown(self) -> None:
        channels_tab = self._tabs.get('channels')
        if channels_tab is not None:
            channels_tab.shutdown()
