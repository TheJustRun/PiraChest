from __future__ import annotations
import logging
import re
from collections import OrderedDict
from PySide6.QtCore import (
    Qt, QSize, Signal, QTimer,
)
from PySide6.QtGui import QPixmap, QColor, QIcon
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QGridLayout, QLabel,
    QScrollArea, QStackedWidget, QSizePolicy,
)
from qfluentwidgets import (
    TitleLabel, BodyLabel, StrongBodyLabel, CaptionLabel, SubtitleLabel,
    PushButton, PrimaryPushButton, SearchLineEdit, CheckBox, ScrollArea,
    SmoothScrollArea, ImageLabel, PillPushButton,
    FluentIcon, HyperlinkButton, qconfig, isDarkTheme, themeColor,
    FlowLayout as QFlowLayout, MessageBoxBase, InfoBar, InfoBarPosition,
    Pivot, TransparentToolButton,
)

from src.core.worker import submit, cancel
from src.core.books import manager as books_manager
from src.core.books.manga import manager as manga_manager
from src.core.books.manga.manager import MangaItem as MangaEntry, MangaChapter, MangaDownloadBridge
from src.core.models import BookItem
from src.core.artwork import images
from src.core.theme import palette, apply_pivot_theme, themed_icon, apply_tool_button_theme
from src.core.translations import tr, register_locale_refresh
from .repacks_page import (
    MetaField, make_tag_pill, render_description_html,
    _stop_previous_movie,
    _muted_text_color, _body_text_color, _surface_tint_color, _grid_colors,
)
from .grid import PosterGrid, ViewModeButton, attrs_subtitle
from .home_page import AccentColorMixin, DetailsViewMixin, MediaPageBase

logger = logging.getLogger(__name__)

_PROVIDER_LABELS = {
    'gutenberg': 'Gutenberg',
    'libgen': 'Library Genesis',
}

_FORMAT_LABELS = {
    'application/epub+zip': 'EPUB',
    'application/pdf': 'PDF',
    'text/plain; charset=utf-8': 'TXT',
    'text/plain; charset=us-ascii': 'TXT',
    'text/plain': 'TXT',
}


def _book_fallback_icon() -> FluentIcon | None:
    for name in ('BOOK_SHELF', 'LIBRARY', 'EDUCATION', 'DOCUMENT', 'FOLDER', 'GAME'):
        icon = getattr(FluentIcon, name, None)
        if icon is not None:
            return icon
    return None


def _set_book_fallback_icon(label: QLabel) -> None:
    icon = _book_fallback_icon()
    if icon is not None:
        label.setPixmap(icon.icon(color=QColor(_body_text_color())).pixmap(48, 48))
    else:
        label.setPixmap(QPixmap())

def _book_icon() -> QIcon:
    icon = _book_fallback_icon()
    return icon.icon(color=QColor(_body_text_color())) if icon is not None else QIcon()


_book_subtitle = attrs_subtitle('author', 'provider', labels=_PROVIDER_LABELS)


def _book_tooltip(entry) -> str:
    sub = _book_subtitle(entry)
    return f"{entry.title or ''}\n{sub}" if sub else (entry.title or '')


class BookGridView(PosterGrid):
    book_clicked = Signal(object)
    _NEAR_BOTTOM_THRESHOLD_PX = 400

    def __init__(self, parent=None):
        super().__init__(parent, cover_attrs=('artwork_url',), key_attr='key', show_caption=True,
                         tooltip_fn=_book_tooltip, subtitle_fn=_book_subtitle,
                         fallback_icon_fn=_book_icon, colors_fn=_grid_colors)
        self.poster_clicked.connect(self.book_clicked.emit)

    def shutdown(self) -> None:
        self.clear()


class MangaGridView(PosterGrid):
    manga_clicked = Signal(object)
    _NEAR_BOTTOM_THRESHOLD_PX = 400

    def __init__(self, parent=None):
        super().__init__(parent, cover_attrs=('cover_url',), key_attr='key', show_caption=True,
                         subtitle_fn=attrs_subtitle('status', 'year'),
                         fallback_icon_fn=_book_icon, colors_fn=_grid_colors)
        self.poster_clicked.connect(self.manga_clicked.emit)

    def add_entries(self, entries: list[MangaEntry]) -> None:
        self.append_entries(entries)

    def shutdown(self) -> None:
        self.clear()


class BookDownloadActionWidget(AccentColorMixin, QWidget):
    def __init__(self, download_bridge, parent=None):
        super().__init__(parent)
        self._download_bridge = download_bridge
        self._details: BookItem | None = None
        self._item_id: str | None = None
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        self._formats_row = QHBoxLayout()
        self._formats_row.setSpacing(8)
        layout.addLayout(self._formats_row)
        self._format_buttons: list[PushButton] = []
        self._status_lbl = CaptionLabel('')
        self._status_lbl.setStyleSheet(f'color: {_muted_text_color()};')
        layout.addWidget(self._status_lbl)
        self._current_accent_color: QColor | None = None
        qconfig.themeColorChanged.connect(self._deferred_reapply_accent_color)
        qconfig.themeChanged.connect(self._deferred_reapply_accent_color)

    def _reapply_accent_color(self) -> None:
        color = self._current_accent_color
        if color is None:
            color = self._default_button_color()
        luminance = 0.299 * color.red() + 0.587 * color.green() + 0.114 * color.blue()
        text_color = '#1a1a1a' if luminance > 150 else '#ffffff'
        hover = color.lighter(112)
        pressed = color.darker(112)
        qss = (
            f'PrimaryPushButton {{ background-color: rgb({color.red()}, {color.green()}, {color.blue()}); '
            f'color: {text_color}; border: none; border-radius: 8px; }} '
            f'PrimaryPushButton:hover {{ background-color: rgb({hover.red()}, {hover.green()}, {hover.blue()}); color: {text_color}; }} '
            f'PrimaryPushButton:pressed {{ background-color: rgb({pressed.red()}, {pressed.green()}, {pressed.blue()}); color: {text_color}; }}'
        )
        for btn in self._format_buttons:
            if isinstance(btn, PrimaryPushButton):
                btn.setStyleSheet(qss)

    def set_details(self, details: BookItem) -> None:
        self._details = details
        self._item_id = None
        self._status_lbl.setText('')
        self._clear_buttons()
        formats = details.formats or {}
        if not formats:
            self._status_lbl.setText(tr('books.no_downloads_available'))
            return
        read_url = formats.get('read')
        first = True
        for mime, _url in formats.items():
            if mime == 'read':
                continue
            ext = _FORMAT_LABELS.get(mime, mime.split('/')[-1].split('+')[0].split(';')[0].upper())
            btn = PrimaryPushButton(ext) if first else PushButton(ext)
            btn.setFixedHeight(32)
            btn.clicked.connect(lambda _checked=False, m=mime: self._on_click(m))
            self._formats_row.addWidget(btn)
            self._format_buttons.append(btn)
            first = False
        if read_url:
            btn = PrimaryPushButton(tr('books.read_online')) if first else PushButton(tr('books.read_online'))
            btn.setFixedHeight(32)
            btn.clicked.connect(lambda: self._open_read_url(read_url))
            self._formats_row.addWidget(btn)
            self._format_buttons.append(btn)
        self._reapply_accent_color()

    def _clear_buttons(self) -> None:
        while self._formats_row.count():
            item = self._formats_row.takeAt(0)
            if item is not None and item.widget() is not None:
                item.widget().deleteLater()
        self._format_buttons.clear()

    def _open_read_url(self, url: str) -> None:
        from PySide6.QtGui import QDesktopServices
        from PySide6.QtCore import QUrl
        QDesktopServices.openUrl(QUrl(url))

    def _on_click(self, mime: str) -> None:
        if self._details is None or self._item_id is not None:
            return
        self._status_lbl.setText(tr('books.download_queued'))
        item_id = self._download_bridge.download(self._details, mime)
        if item_id is None:
            self._status_lbl.setText(tr('books.download_failed'))
            return
        self._item_id = item_id

    def shutdown(self) -> None:
        pass

class BookDetailsView(DetailsViewMixin, QWidget):
    back_requested = Signal()
    COVER_WIDTH = 280
    COVER_HEIGHT = 392
    SECTIONS_WIDTH = 300
    READING_WIDTH_MAX = 860

    def __init__(self, download_bridge, parent=None):
        super().__init__(parent)
        self._download_action = BookDownloadActionWidget(download_bridge)
        self._pending_cover_url: str | None = None
        self._meta_widgets: list[MetaField] = []
        self._tag_widgets: list[PillPushButton] = []

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 4, 0, 0)
        outer.setSpacing(10)

        back_btn = PushButton(themed_icon('books.detail_back', FluentIcon.RETURN), tr('books.back_to_grid'))
        back_btn.setFixedHeight(32)
        self._back_btn = back_btn
        outer.addWidget(back_btn, 0, Qt.AlignmentFlag.AlignLeft)
        back_btn.clicked.connect(self.back_requested.emit)

        self._scroll = SmoothScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        self._scroll.setStyleSheet('background: transparent; border: none;')
        outer.addWidget(self._scroll, 1)

        content = QWidget()
        self._scroll.setWidget(content)
        self._content_layout = QVBoxLayout(content)
        self._content_layout.setContentsMargins(4, 12, 24, 40)
        self._content_layout.setSpacing(24)

        top_row = QHBoxLayout()
        top_row.setSpacing(40)
        top_row.setAlignment(Qt.AlignmentFlag.AlignTop)

        cover_container = QWidget()
        cover_container.setFixedWidth(self.COVER_WIDTH)
        cover_col = QVBoxLayout(cover_container)
        cover_col.setContentsMargins(0, 0, 0, 0)
        cover_col.setSpacing(12)

        self._cover_lbl = ImageLabel(cover_container)
        self._cover_lbl.setBorderRadius(10, 10, 10, 10)
        self._cover_lbl.setFixedSize(self.COVER_WIDTH, self.COVER_HEIGHT)
        cover_col.addWidget(self._cover_lbl, 0, Qt.AlignmentFlag.AlignTop)

        self._cover_fallback = QWidget(cover_container)
        self._cover_fallback.setFixedSize(self.COVER_WIDTH, self.COVER_HEIGHT)
        self._cover_fallback.setVisible(False)
        self._cover_fallback.setStyleSheet(f'background-color: {_surface_tint_color(14)}; border-radius: 10px;')
        cover_fallback_layout = QVBoxLayout(self._cover_fallback)
        cover_fallback_layout.setContentsMargins(20, 0, 20, 0)
        cover_fallback_layout.setSpacing(10)
        cover_fallback_layout.addStretch(1)
        self._cover_fallback_icon_lbl = QLabel(self._cover_fallback)
        _set_book_fallback_icon(self._cover_fallback_icon_lbl)
        self._cover_fallback_icon_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._cover_fallback_icon_lbl.setStyleSheet('background: transparent;')
        cover_fallback_layout.addWidget(self._cover_fallback_icon_lbl)
        self._cover_fallback_title_lbl = QLabel('', self._cover_fallback)
        self._cover_fallback_title_lbl.setWordWrap(True)
        self._cover_fallback_title_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._cover_fallback_title_lbl.setStyleSheet(f'background: transparent; font-size: 13px; color: {palette()["primary_text"]};')
        cover_fallback_layout.addWidget(self._cover_fallback_title_lbl)
        cover_fallback_layout.addStretch(1)
        cover_col.addWidget(self._cover_fallback, 0, Qt.AlignmentFlag.AlignTop)

        cover_col.addWidget(self._download_action, 0, Qt.AlignmentFlag.AlignTop)
        cover_col.addStretch(1)
        top_row.addWidget(cover_container, 0, Qt.AlignmentFlag.AlignTop)

        info_col_container = QWidget()
        info_col_container.setMaximumWidth(self.READING_WIDTH_MAX)
        info_col = QVBoxLayout(info_col_container)
        info_col.setContentsMargins(0, 0, 0, 0)
        info_col.setSpacing(20)

        self._title_lbl = TitleLabel('')
        self._title_lbl.setWordWrap(True)
        self._title_lbl.setStyleSheet(f'font-weight: 700; color: {palette()["primary_text"]};')
        info_col.addWidget(self._title_lbl)

        self._author_lbl = BodyLabel('')
        self._author_lbl.setStyleSheet(f'color: {_muted_text_color()}; font-weight: 400;')
        info_col.addWidget(self._author_lbl)

        self._provider_badge = PillPushButton('', info_col_container)
        self._provider_badge.setChecked(True)
        self._provider_badge.setCheckable(False)
        self._provider_badge.setCursor(Qt.CursorShape.ArrowCursor)
        badge_row = QHBoxLayout()
        badge_row.setContentsMargins(0, 0, 0, 0)
        badge_row.addWidget(self._provider_badge, 0, Qt.AlignmentFlag.AlignLeft)
        badge_row.addStretch(1)
        info_col.addLayout(badge_row)

        self._meta_grid = QGridLayout()
        self._meta_grid.setHorizontalSpacing(40)
        self._meta_grid.setVerticalSpacing(10)
        self._meta_grid.setColumnStretch(0, 0)
        self._meta_grid.setColumnStretch(1, 0)
        info_col.addLayout(self._meta_grid)

        tags_header = CaptionLabel(tr('books.subjects_genres'))
        tags_header.setStyleSheet(f'font-weight: 400; color: {_muted_text_color()};')
        self._tags_header = tags_header
        info_col.addWidget(tags_header)
        self._tags_container = QWidget()
        self._tags_layout = QFlowLayout(self._tags_container, needAni=False, isTight=True)
        self._tags_layout.setContentsMargins(0, 0, 0, 0)
        self._tags_layout.setHorizontalSpacing(8)
        self._tags_layout.setVerticalSpacing(8)
        info_col.addWidget(self._tags_container)

        desc_header = StrongBodyLabel(tr('repacks.description'))
        desc_header.setStyleSheet(f'font-size: 16px; font-weight: 600; color: {palette()["primary_text"]};')
        self._desc_header = desc_header
        info_col.addWidget(desc_header)
        self._desc_lbl = BodyLabel('')
        self._desc_lbl.setWordWrap(True)
        self._desc_lbl.setTextFormat(Qt.TextFormat.RichText)
        self._desc_lbl.setOpenExternalLinks(True)
        self._desc_lbl.setStyleSheet(f'font-size: 15px; font-weight: 500; line-height: 170%; color: {_body_text_color()};')
        info_col.addWidget(self._desc_lbl)
        self._raw_desc_text = ''
        qconfig.themeChanged.connect(self._on_theme_changed)

        info_col.addStretch(1)
        top_row.addWidget(info_col_container, 1, Qt.AlignmentFlag.AlignTop)

        sections_container = QWidget()
        sections_container.setFixedWidth(self.SECTIONS_WIDTH)
        self._sections_col = QVBoxLayout(sections_container)
        self._sections_col.setContentsMargins(0, 0, 0, 0)
        self._sections_col.setSpacing(14)
        sections_header = StrongBodyLabel(tr('repacks.details'))
        sections_header.setStyleSheet(f'font-size: 16px; font-weight: 600; color: {palette()["primary_text"]};')
        self._sections_header = sections_header
        self._sections_col.addWidget(sections_header)
        self._site_link_btn = HyperlinkButton('', tr('repacks.view_on_website'), sections_container, themed_icon('books.site_link', FluentIcon.LINK))
        self._site_link_btn.setIconSize(QSize(14, 14))
        self._apply_site_link_style()
        qconfig.themeColorChanged.connect(self._deferred_apply_site_link_style)
        qconfig.themeChanged.connect(self._deferred_apply_site_link_style)
        self._site_link_btn.setVisible(False)
        self._sections_col.addWidget(self._site_link_btn)
        self._sections_col.addStretch(1)
        top_row.addWidget(sections_container, 0, Qt.AlignmentFlag.AlignTop)

        self._content_layout.addLayout(top_row)
        self._content_layout.addStretch(1)

        register_locale_refresh(self, self._apply_locale)

    def _apply_locale(self, *_args) -> None:
        self._back_btn.setText(tr('books.back_to_grid'))
        self._tags_header.setText(tr('books.subjects_genres'))
        self._desc_header.setText(tr('repacks.description'))
        self._sections_header.setText(tr('repacks.details'))
        self._site_link_btn.setText(tr('repacks.view_on_website'))

    def _apply_site_link_style(self) -> None:
        color = themeColor()
        base_alpha = 55 if isDarkTheme() else 40
        hover_alpha = 85 if isDarkTheme() else 65
        text_color = palette()['primary_text']
        self._site_link_btn.setStyleSheet(
            f'HyperlinkButton {{ background-color: rgba({color.red()}, {color.green()}, {color.blue()}, {base_alpha}); '
            f'border-radius: 6px; padding: 6px 10px 6px 30px; font-weight: 600; color: {text_color}; }} '
            f'HyperlinkButton:hover {{ background-color: rgba({color.red()}, {color.green()}, {color.blue()}, {hover_alpha}); color: {text_color}; }}'
        )
        self._site_link_btn.setIcon(themed_icon('books.site_link', FluentIcon.LINK, color=text_color))

    def _on_theme_changed(self, *_args) -> None:
        self._refresh_cover_fallback_theme()

    def _refresh_cover_fallback_theme(self) -> None:
        self._cover_fallback.setStyleSheet(f'background-color: {_surface_tint_color(14)}; border-radius: 10px;')
        _set_book_fallback_icon(self._cover_fallback_icon_lbl)
        self._cover_fallback_title_lbl.setStyleSheet(f'background: transparent; font-size: 13px; color: {palette()["primary_text"]};')

    def show_loading(self, entry: BookItem) -> None:
        self._title_lbl.setText(entry.title)
        self._author_lbl.setText(entry.author or '')
        self._author_lbl.setVisible(bool(entry.author))
        self._provider_badge.setText(_PROVIDER_LABELS.get(entry.provider, (entry.provider or '').title()))
        self._provider_badge.setVisible(bool(entry.provider))
        self._raw_desc_text = ''
        self._desc_lbl.setText(tr('books.loading_details'))
        self._clear_meta()
        self._clear_tags()
        self._site_link_btn.setVisible(False)
        self._download_action.set_accent_color(None)
        if entry.artwork_url:
            self._cover_lbl.setVisible(True)
            self._cover_fallback.setVisible(False)
            self._pending_cover_url = entry.artwork_url
            self._set_cover_path(entry.artwork_url)
        else:
            self._cover_lbl.setVisible(False)
            self._show_cover_fallback(entry.title)

    def show_details(self, details: BookItem) -> None:
        self._download_action.set_details(details)
        self._title_lbl.setText(details.title)
        self._author_lbl.setText(details.author or '')
        self._author_lbl.setVisible(bool(details.author))
        self._provider_badge.setText(_PROVIDER_LABELS.get(details.provider, (details.provider or '').title()))
        self._provider_badge.setVisible(bool(details.provider))
        self._raw_desc_text = details.description or ''
        self._desc_lbl.setText(render_description_html(self._raw_desc_text) or tr('books.no_description'))

        self._clear_meta()
        self._clear_tags()

        row = 0
        col = 0
        meta_fields = [
            ('publisher', tr('books.publisher'), getattr(details, 'publisher', None)),
            ('publish_date', tr('books.publish_date'), getattr(details, 'publish_date', None) or details.year),
            ('isbn', tr('books.isbn'), getattr(details, 'isbn', None)),
            ('language', tr('books.language'), details.language),
            ('page_count', tr('books.page_count'), str(getattr(details, 'page_count', '') or '') or None),
        ]
        for _key, label, value in meta_fields:
            if not value:
                continue
            field = MetaField(label, str(value))
            span = 2 if len(str(value)) > 28 else 1
            if span == 2 and col == 1:
                row += 1
                col = 0
            self._meta_grid.addWidget(field, row, col, 1, span)
            self._meta_widgets.append(field)
            col += span
            if col >= 2:
                col = 0
                row += 1

        subjects = list(details.subjects or [])[:20]
        self._tags_header.setVisible(bool(subjects))
        self._tags_container.setVisible(bool(subjects))
        for subject in subjects:
            pill = make_tag_pill(subject, self._tags_container)
            pill.adjustSize()
            self._tags_layout.addWidget(pill)
            self._tag_widgets.append(pill)
        if subjects:
            QTimer.singleShot(0, self._tags_layout.update)

        read_url = (details.formats or {}).get('read')
        self._site_link_btn.setVisible(bool(read_url))
        if read_url:
            self._site_link_btn.setUrl(read_url)

        cover_url = details.artwork_url
        if cover_url:
            self._cover_lbl.setVisible(True)
            self._cover_fallback.setVisible(False)
            self._pending_cover_url = cover_url
            self._set_cover_path(cover_url)
        else:
            self._cover_lbl.setVisible(False)
            self._show_cover_fallback(details.title)

    def show_error(self, message: str) -> None:
        self._clear_meta()
        self._clear_tags()
        self._desc_lbl.setText(tr('books.failed_to_load'))

    def _set_cover_path(self, path: str) -> None:
        _stop_previous_movie(self._cover_lbl)

        def _apply(pix, _src=path):
            if self._pending_cover_url and self._pending_cover_url != _src:
                return
            if pix.isNull():
                self._cover_lbl.setVisible(False)
                self._show_cover_fallback(self._title_lbl.text())
                return
            self._cover_lbl.setImage(pix)
            self._cover_lbl.setFixedSize(self.COVER_WIDTH, self.COVER_HEIGHT)

        def _accent(colors):
            if colors:
                self._download_action.set_accent_color(colors[0])
        images.get(path, (self.COVER_WIDTH, self.COVER_HEIGHT), _apply, owner=self)
        images.dominant(path, _accent, owner=self)

    def release_full_cover(self) -> None:
        _stop_previous_movie(self._cover_lbl)
        self._cover_lbl.setImage(None)
        self._cover_lbl.setFixedSize(self.COVER_WIDTH, self.COVER_HEIGHT)


class BookSourceFilterDialog(MessageBoxBase):
    def __init__(self, all_sources: list[str], selected_sources: list[str], parent=None):
        super().__init__(parent)
        self.titleLabel = SubtitleLabel(tr('books.filter_sources'), self)
        self.viewLayout.addWidget(self.titleLabel)
        scroll = ScrollArea(self)
        scroll.setFixedHeight(min(220, 44 * max(1, len(all_sources)) + 16))
        scroll.setWidgetResizable(True)
        scroll.setStyleSheet('background: transparent; border: none;')
        container = QWidget(scroll)
        container.setStyleSheet('background: transparent;')
        col = QVBoxLayout(container)
        col.setSpacing(4)
        self._checks: dict[str, CheckBox] = {}
        for source in all_sources:
            label = _PROVIDER_LABELS.get(source, source.title())
            cb = CheckBox(label, container)
            cb.setChecked(source in selected_sources)
            self._checks[source] = cb
            col.addWidget(cb)
        col.addStretch(1)
        scroll.setWidget(container)
        self.viewLayout.addWidget(scroll)
        self.yesButton.setText(tr('books.apply'))
        self.cancelButton.setText(tr('books.cancel'))
        self.widget.setMinimumWidth(320)

    def selected_sources(self) -> list[str]:
        return [source for source, cb in self._checks.items() if cb.isChecked()]


_MANGA_DESC_CUTOFF_RE = re.compile(r'\n\s*-{3,}\s*\n')
_MANGA_DESC_LINKS_RE = re.compile(r'(?im)^\s*\**links?:?\**\s*$.*', re.DOTALL)
_MANGA_MD_LINK_RE = re.compile(r'\[([^\]]+)\]\([^)]*\)')
_MANGA_BARE_URL_RE = re.compile(r'https?://\S+')


def _clean_manga_description(text: str) -> str:
    if not text:
        return ''
    match = _MANGA_DESC_CUTOFF_RE.search(text)
    if match:
        text = text[:match.start()]
    text = _MANGA_DESC_LINKS_RE.sub('', text)
    text = _MANGA_MD_LINK_RE.sub(lambda m: m.group(1), text)
    text = _MANGA_BARE_URL_RE.sub('', text)
    return text.strip()


class MangaChapterSelectDialog(MessageBoxBase):
    _WARN_THRESHOLD = 20

    def __init__(self, chapters: list[MangaChapter], parent=None):
        super().__init__(parent)
        self._checks: list[tuple[CheckBox, MangaChapter]] = []
        self.titleLabel = SubtitleLabel(tr('books.manga_choose_chapters'), self)
        self.viewLayout.addWidget(self.titleLabel)

        select_row = QHBoxLayout()
        self._select_all_btn = PushButton(tr('books.manga_select_all'), self)
        self._select_all_btn.clicked.connect(self._toggle_select_all)
        select_row.addWidget(self._select_all_btn)
        select_row.addStretch(1)
        self.viewLayout.addLayout(select_row)

        self._warning_lbl = CaptionLabel(tr('books.manga_bulk_download_warning'), self)
        self._warning_lbl.setWordWrap(True)
        self._warning_lbl.setStyleSheet(f"color: {palette()['state_verifying']};")
        self._warning_lbl.setVisible(False)
        self.viewLayout.addWidget(self._warning_lbl)

        scroll = ScrollArea(self)
        scroll.setFixedHeight(min(360, 36 * max(1, len(chapters)) + 16))
        scroll.setWidgetResizable(True)
        scroll.setStyleSheet('background: transparent; border: none;')
        container = QWidget(scroll)
        container.setStyleSheet('background: transparent;')
        col = QVBoxLayout(container)
        col.setSpacing(4)
        for chapter in chapters:
            label = chapter.label
            if chapter.scanlation_group:
                label = f'{label}  ·  {chapter.scanlation_group}'
            cb = CheckBox(label, container)
            cb.setChecked(True)
            cb.stateChanged.connect(self._update_warning)
            self._checks.append((cb, chapter))
            col.addWidget(cb)
        col.addStretch(1)
        scroll.setWidget(container)
        self.viewLayout.addWidget(scroll)

        self.yesButton.setText(tr('books.manga_download_selected'))
        self.cancelButton.setText(tr('books.cancel'))
        self.widget.setMinimumWidth(360)
        self._update_warning()

    def _toggle_select_all(self) -> None:
        any_unchecked = any(not cb.isChecked() for cb, _ in self._checks)
        for cb, _ in self._checks:
            cb.setChecked(any_unchecked)

    def _update_warning(self, *_args) -> None:
        selected_count = sum(1 for cb, _ in self._checks if cb.isChecked())
        self._warning_lbl.setVisible(selected_count > self._WARN_THRESHOLD)

    def selected_chapters(self) -> list[MangaChapter]:
        return [chapter for cb, chapter in self._checks if cb.isChecked()]


class MangaDownloadActionWidget(AccentColorMixin, QWidget):
    def __init__(self, download_bridge: MangaDownloadBridge, parent=None):
        super().__init__(parent)
        self._download_bridge = download_bridge
        self._entry: MangaEntry | None = None
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)
        self._button = PrimaryPushButton(tr('books.manga_download_button'), self)
        self._button.setFixedHeight(32)
        self._button.clicked.connect(self._on_click)
        layout.addWidget(self._button)
        self._current_accent_color: QColor | None = None
        qconfig.themeColorChanged.connect(self._deferred_reapply_accent_color)
        qconfig.themeChanged.connect(self._deferred_reapply_accent_color)

    def _reapply_accent_color(self) -> None:
        color = self._current_accent_color
        if color is None:
            color = self._default_button_color()
        luminance = 0.299 * color.red() + 0.587 * color.green() + 0.114 * color.blue()
        text_color = '#1a1a1a' if luminance > 150 else '#ffffff'
        hover = color.lighter(112)
        pressed = color.darker(112)
        self._button.setStyleSheet(
            f'PrimaryPushButton {{ background-color: rgb({color.red()}, {color.green()}, {color.blue()}); '
            f'color: {text_color}; border: none; border-radius: 8px; }} '
            f'PrimaryPushButton:hover {{ background-color: rgb({hover.red()}, {hover.green()}, {hover.blue()}); color: {text_color}; }} '
            f'PrimaryPushButton:pressed {{ background-color: rgb({pressed.red()}, {pressed.green()}, {pressed.blue()}); color: {text_color}; }}'
        )

    def set_entry(self, entry: MangaEntry | None) -> None:
        self._entry = entry
        downloadable = bool(entry and any(c.is_downloadable for c in entry.chapters))
        self.setVisible(downloadable)

    def _on_click(self) -> None:
        if self._entry is None:
            return
        downloadable = [c for c in self._entry.chapters if c.is_downloadable]
        if not downloadable:
            return
        dialog = MangaChapterSelectDialog(downloadable, parent=self.window())
        if not dialog.exec():
            return
        for chapter in dialog.selected_chapters():
            try:
                self._download_bridge.download_chapter(self._entry.title, chapter)
            except Exception as exc:
                logger.warning('Failed to queue manga chapter download: %s', exc)

    def shutdown(self) -> None:
        pass


class MangaDetailsView(DetailsViewMixin, QWidget):
    back_requested = Signal()

    COVER_WIDTH = 220
    COVER_HEIGHT = 310

    def __init__(self, download_bridge: MangaDownloadBridge, parent=None):
        super().__init__(parent)
        self.setStyleSheet('background: transparent;')
        self._download_bridge = download_bridge
        self._cover_url = ''

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 4, 0, 0)
        outer.setSpacing(10)

        back_btn = PushButton(themed_icon('books.detail_back', FluentIcon.RETURN), tr('books.back_to_grid'))
        back_btn.setFixedHeight(32)
        back_btn.clicked.connect(self.back_requested.emit)
        outer.addWidget(back_btn, 0, Qt.AlignmentFlag.AlignLeft)

        self._scroll = SmoothScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        self._scroll.setStyleSheet('background: transparent; border: none;')
        outer.addWidget(self._scroll, 1)

        content = QWidget()
        self._scroll.setWidget(content)
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(4, 12, 24, 40)
        content_layout.setSpacing(24)

        top_row = QHBoxLayout()
        top_row.setSpacing(40)
        top_row.setAlignment(Qt.AlignmentFlag.AlignTop)

        cover_container = QWidget()
        cover_container.setFixedWidth(self.COVER_WIDTH)
        cover_col = QVBoxLayout(cover_container)
        cover_col.setContentsMargins(0, 0, 0, 0)
        cover_col.setSpacing(12)
        self._cover_lbl = ImageLabel(cover_container)
        self._cover_lbl.setBorderRadius(10, 10, 10, 10)
        self._cover_lbl.setFixedSize(self.COVER_WIDTH, self.COVER_HEIGHT)
        cover_col.addWidget(self._cover_lbl, 0, Qt.AlignmentFlag.AlignTop)

        self._download_action = MangaDownloadActionWidget(download_bridge, cover_container)
        cover_col.addWidget(self._download_action, 0, Qt.AlignmentFlag.AlignTop)

        self._open_btn = HyperlinkButton('', tr('books.manga_view_on_mangadex'), cover_container)
        self._open_btn.setFixedHeight(32)
        self._apply_open_btn_style()
        qconfig.themeColorChanged.connect(self._deferred_apply_open_btn_style)
        qconfig.themeChanged.connect(self._deferred_apply_open_btn_style)
        cover_col.addWidget(self._open_btn, 0)
        cover_col.addStretch(1)
        top_row.addWidget(cover_container, 0, Qt.AlignmentFlag.AlignTop)

        info_col_container = QWidget()
        info_col = QVBoxLayout(info_col_container)
        info_col.setContentsMargins(0, 0, 0, 0)
        info_col.setSpacing(16)

        self._title_lbl = TitleLabel('')
        self._title_lbl.setWordWrap(True)
        self._title_lbl.setStyleSheet(f'font-weight: 700; color: {palette()["primary_text"]};')
        info_col.addWidget(self._title_lbl)

        self._meta_grid = QGridLayout()
        self._meta_grid.setHorizontalSpacing(40)
        self._meta_grid.setVerticalSpacing(10)
        info_col.addLayout(self._meta_grid)

        tags_header = CaptionLabel(tr('books.subjects_genres'))
        tags_header.setStyleSheet(f'font-weight: 400; color: {_muted_text_color()};')
        info_col.addWidget(tags_header)
        self._tags_container = QWidget()
        self._tags_layout = QFlowLayout(self._tags_container, needAni=False, isTight=True)
        self._tags_layout.setContentsMargins(0, 0, 0, 0)
        self._tags_layout.setHorizontalSpacing(8)
        self._tags_layout.setVerticalSpacing(8)
        info_col.addWidget(self._tags_container)
        self._tag_widgets: list[QWidget] = []

        desc_header = StrongBodyLabel(tr('repacks.description'))
        desc_header.setStyleSheet(f'font-size: 16px; font-weight: 600; color: {palette()["primary_text"]};')
        info_col.addWidget(desc_header)
        self._desc_lbl = BodyLabel('')
        self._desc_lbl.setWordWrap(True)
        self._desc_lbl.setTextFormat(Qt.TextFormat.RichText)
        self._desc_lbl.setOpenExternalLinks(True)
        self._desc_lbl.setStyleSheet(f'font-size: 15px; font-weight: 500; line-height: 170%; color: {_body_text_color()};')
        info_col.addWidget(self._desc_lbl)
        info_col.addStretch(1)
        top_row.addWidget(info_col_container, 1, Qt.AlignmentFlag.AlignTop)
        content_layout.addLayout(top_row)

        chapters_header = StrongBodyLabel(tr('books.manga_chapters'))
        chapters_header.setStyleSheet(f'font-size: 16px; font-weight: 600; color: {palette()["primary_text"]};')
        content_layout.addWidget(chapters_header)
        self._chapters_container = QWidget()
        self._chapters_layout = QVBoxLayout(self._chapters_container)
        self._chapters_layout.setContentsMargins(0, 0, 0, 0)
        self._chapters_layout.setSpacing(6)
        content_layout.addWidget(self._chapters_container)
        content_layout.addStretch(1)

        self._loading_lbl = BodyLabel(tr('books.loading_details'), self)
        self._loading_lbl.setStyleSheet(f'color: {_muted_text_color()};')
        content_layout.addWidget(self._loading_lbl)

    def _deferred_apply_open_btn_style(self) -> None:
        self._apply_open_btn_style()
        QTimer.singleShot(0, self._apply_open_btn_style)

    def _apply_open_btn_style(self) -> None:
        color = themeColor()
        base_alpha = 55 if isDarkTheme() else 40
        hover_alpha = 85 if isDarkTheme() else 65
        text_color = palette()['primary_text']
        self._open_btn.setStyleSheet(
            f'HyperlinkButton {{ background-color: rgba({color.red()}, {color.green()}, {color.blue()}, {base_alpha}); '
            f'border-radius: 8px; padding: 6px 16px; font-weight: 600; color: {text_color}; }} '
            f'HyperlinkButton:hover {{ background-color: rgba({color.red()}, {color.green()}, {color.blue()}, {hover_alpha}); color: {text_color}; }}'
        )

    def _chapter_button_qss(self) -> tuple[str, str]:
        color = themeColor()
        base_alpha = 55 if isDarkTheme() else 40
        hover_alpha = 85 if isDarkTheme() else 65
        text_color = palette()['primary_text']
        read_qss = (
            f'HyperlinkButton {{ background-color: rgba({color.red()}, {color.green()}, {color.blue()}, {base_alpha}); '
            f'border-radius: 6px; padding: 4px 12px; font-weight: 600; color: {text_color}; }} '
            f'HyperlinkButton:hover {{ background-color: rgba({color.red()}, {color.green()}, {color.blue()}, {hover_alpha}); color: {text_color}; }}'
        )
        neutral = QColor(_muted_text_color())
        neutral_base_alpha = 45 if isDarkTheme() else 35
        neutral_hover_alpha = 75 if isDarkTheme() else 55
        download_qss = (
            f'TransparentToolButton {{ background-color: rgba({neutral.red()}, {neutral.green()}, {neutral.blue()}, {neutral_base_alpha}); border-radius: 6px; }} '
            f'TransparentToolButton:hover {{ background-color: rgba({neutral.red()}, {neutral.green()}, {neutral.blue()}, {neutral_hover_alpha}); }}'
        )
        return read_qss, download_qss

    def show_loading(self, entry: MangaEntry) -> None:
        self._title_lbl.setText(entry.title)
        self._cover_lbl.setImage(QPixmap())
        self._desc_lbl.setText('')
        self._clear_meta()
        self._clear_tags()
        self._clear_chapters()
        self._open_btn.setUrl(entry.web_url)
        self._download_action.set_accent_color(None)
        self._download_action.set_entry(None)
        self._loading_lbl.setVisible(True)
        cover_url = entry.cover_url_full or entry.cover_url
        self._cover_url = cover_url
        if not cover_url:
            return
        self._apply_cover_path(cover_url)

    def _apply_cover_path(self, path: str) -> None:
        def _apply(pix, _src=path):
            if self._cover_url != _src or pix.isNull():
                return
            self._cover_lbl.setImage(pix)
            self._cover_lbl.setFixedSize(self.COVER_WIDTH, self.COVER_HEIGHT)

        def _accent(colors):
            if colors and self._cover_url == path:
                self._download_action.set_accent_color(colors[0])
        images.get(path, (self.COVER_WIDTH, self.COVER_HEIGHT), _apply, owner=self)
        images.dominant(path, _accent, owner=self)

    def show_details(self, entry: MangaEntry) -> None:
        self._loading_lbl.setVisible(False)
        self._title_lbl.setText(entry.title)
        self._open_btn.setUrl(entry.web_url)
        self._download_action.set_entry(entry)
        self._desc_lbl.setText(render_description_html(_clean_manga_description(entry.description)) or tr('books.no_description'))

        self._clear_meta()
        row = 0
        if entry.status:
            self._meta_grid.addWidget(MetaField(tr('books.manga_status'), entry.status), 0, row)
            row += 1
        if entry.year:
            self._meta_grid.addWidget(MetaField(tr('books.manga_year'), str(entry.year)), 0, row)
            row += 1
        if entry.content_rating:
            self._meta_grid.addWidget(MetaField(tr('books.manga_content_rating'), entry.content_rating), 0, row)
            row += 1

        self._clear_tags()
        seen_tags: set[str] = set()
        for tag in entry.tags:
            key = tag.strip().lower()
            if not key or key in seen_tags:
                continue
            seen_tags.add(key)
            pill = make_tag_pill(tag, self._tags_container)
            pill.adjustSize()
            self._tags_layout.addWidget(pill)
            self._tag_widgets.append(pill)
        if self._tag_widgets:
            QTimer.singleShot(0, self._tags_layout.update)

        self._clear_chapters()
        if not entry.chapters:
            empty_lbl = CaptionLabel(tr('books.manga_no_chapters'), self._chapters_container)
            empty_lbl.setStyleSheet(f'color: {_muted_text_color()};')
            self._chapters_layout.addWidget(empty_lbl)
        else:
            for chapter in entry.chapters:
                self._chapters_layout.addWidget(self._build_chapter_row(entry.title, chapter))

    def _build_chapter_row(self, manga_title: str, chapter: MangaChapter) -> QWidget:
        row = QWidget(self._chapters_container)
        row_layout = QHBoxLayout(row)
        row_layout.setContentsMargins(0, 0, 0, 0)
        row_layout.setSpacing(12)
        label_text = chapter.label
        if chapter.scanlation_group:
            label_text = f'{label_text}  ·  {chapter.scanlation_group}'
        lbl = BodyLabel(label_text, row)
        lbl.setStyleSheet(f'color: {_body_text_color()};')
        row_layout.addWidget(lbl, 1)

        read_btn = HyperlinkButton(chapter.web_url, tr('books.manga_read'), row)
        row_layout.addWidget(read_btn, 0)

        read_qss, download_qss = self._chapter_button_qss()
        read_btn.setStyleSheet(read_qss)

        if chapter.is_downloadable:
            status_lbl = CaptionLabel('', row)
            status_lbl.setStyleSheet(f'color: {_muted_text_color()};')
            row_layout.addWidget(status_lbl, 0)

            download_btn = TransparentToolButton(themed_icon('books.chapter_download', FluentIcon.DOWNLOAD), row)
            download_btn.setFixedSize(28, 28)
            download_btn.setStyleSheet(download_qss)
            download_btn.setToolTip(tr('books.manga_download'))
            download_btn.clicked.connect(
                lambda _checked=False, c=chapter, s=status_lbl, b=download_btn: self._start_chapter_download(manga_title, c, s, b)
            )
            row_layout.addWidget(download_btn, 0, Qt.AlignmentFlag.AlignRight)
        return row

    def _start_chapter_download(self, manga_title: str, chapter: MangaChapter, status_lbl: QLabel, btn: TransparentToolButton) -> None:
        btn.setEnabled(False)
        try:
            self._download_bridge.download_chapter(manga_title, chapter)
        except Exception as exc:
            logger.warning('Failed to queue manga chapter download: %s', exc)
            status_lbl.setText(tr('books.manga_download_failed'))
            btn.setEnabled(True)
            return
        status_lbl.setText(tr('books.manga_downloading'))
        btn.setEnabled(True)

    def show_error(self, _message: str) -> None:
        self._loading_lbl.setVisible(False)
        self._desc_lbl.setText(tr('books.failed_to_load'))

    def _clear_meta(self) -> None:
        while self._meta_grid.count():
            item = self._meta_grid.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()

    def _clear_chapters(self) -> None:
        while self._chapters_layout.count():
            item = self._chapters_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()

    def shutdown(self) -> None:
        try:
            images.release(self)
        except (TypeError, RuntimeError):
            pass


class MangaBrowserTab(MediaPageBase):
    def __init__(self, download_manager, parent=None, manga_download_bridge=None):
        super().__init__(parent)
        self.setStyleSheet('background: transparent;')
        self._download_bridge = manga_download_bridge or MangaDownloadBridge(download_manager, self)
        self._query = ''
        self._page = 1
        self._has_more = True
        self._loading = False
        self._task_id: str | None = None
        self._details_task_id: str | None = None
        self._details_cache: "OrderedDict[str, MangaEntry]" = OrderedDict()
        self._current_details_key: str | None = None

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(14)

        header_row = QHBoxLayout()
        self._search_edit = SearchLineEdit(self)
        self._search_edit.setMinimumWidth(140)
        self._search_edit.setMaximumWidth(280)
        self._search_edit.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self._search_edit.setPlaceholderText(tr('books.manga_search_placeholder'))
        self._search_edit.returnPressed.connect(self._on_search)
        self._search_edit.searchSignal.connect(self._on_search)
        self._search_edit.clearSignal.connect(self._on_search_cleared)
        header_row.addWidget(self._search_edit)

        self._refresh_btn = TransparentToolButton(themed_icon('repacks.refresh', FluentIcon.SYNC), self)
        apply_tool_button_theme(self._refresh_btn)
        self._refresh_btn.setToolTip(tr('repacks.refresh_tooltip'))
        self._refresh_btn.setFixedSize(32, 32)
        self._refresh_btn.clicked.connect(self._on_refresh_clicked)
        header_row.addWidget(self._refresh_btn)
        header_row.addStretch(1)
        outer.addLayout(header_row)

        self._stack = QStackedWidget()
        outer.addWidget(self._stack, 1)

        self._grid = MangaGridView(self)
        self._grid.manga_clicked.connect(self._show_details)
        self._grid.near_bottom.connect(self._on_near_bottom)
        view_btn = ViewModeButton(self._grid, parent=self, list_tip=tr('books.list_view', default='List view'),
                                  grid_tip=tr('books.grid_view', default='Grid view'))
        apply_tool_button_theme(view_btn)
        header_row.insertWidget(header_row.count() - 1, view_btn)
        self._stack.addWidget(self._grid)

        self._details = MangaDetailsView(self._download_bridge, self)
        self._details.back_requested.connect(self._show_grid)
        self._stack.addWidget(self._details)

        self._stack.setCurrentWidget(self._grid)
        self._load_page(reset=True)

    def _on_search(self) -> None:
        query = self._search_edit.text().strip()
        if query == self._query:
            return
        self._query = query
        self._load_page(reset=True)

    def _on_search_cleared(self) -> None:
        if not self._query:
            return
        self._query = ''
        self._load_page(reset=True)

    def _on_near_bottom(self) -> None:
        if not self._loading and self._has_more:
            self._load_page(reset=False)

    def _on_refresh_clicked(self) -> None:
        self._details_cache.clear()
        if self._stack.currentWidget() is not self._grid:
            self._show_grid()
        self._load_page(reset=True, use_cache=False)

    def _load_page(self, reset: bool, use_cache: bool = True) -> None:
        if self._task_id is not None:
            cancel(self._task_id)
            self._task_id = None
        if reset:
            self._page = 1
            self._has_more = True
            self._grid.clear()
        self._loading = True
        page = self._page
        if self._query:
            self._task_id = submit(
                manga_manager.search_all, args=(self._query, page),
                kwargs={'use_cache': use_cache},
                on_done=lambda result: self._on_page_loaded(page, result),
                on_error=self._on_page_error,
            )
        else:
            self._task_id = submit(
                manga_manager.browse_all, args=(page,),
                kwargs={'use_cache': use_cache},
                on_done=lambda result: self._on_page_loaded(page, result),
                on_error=self._on_page_error,
            )

    def _on_page_loaded(self, page: int, result) -> None:
        self._task_id = None
        self._loading = False
        self._has_more = result.has_more
        self._page = page + 1
        self._grid.add_entries(result.entries)

    def _on_page_error(self, message: str) -> None:
        logger.warning('MangaDex page load failed: %s', message)
        self._task_id = None
        self._loading = False
        self._has_more = False
        if self._page > 1:
            return
        InfoBar.error(
            title=tr('books.manga_load_failed_title'),
            content=str(message),
            orient=Qt.Orientation.Horizontal,
            isClosable=True,
            position=InfoBarPosition.TOP_RIGHT,
            duration=4000,
            parent=self.window(),
        )

    def _show_details(self, entry: MangaEntry) -> None:
        if self._details_task_id is not None:
            cancel(self._details_task_id)
            self._details_task_id = None
        self._current_details_key = entry.key
        self._details.show_loading(entry)
        self._switch_stack_animated(self._details)

        cached = self._details_cache.get(entry.key)
        if cached is not None:
            self._details_cache.move_to_end(entry.key)
            self._details.show_details(cached)
            return

        self._details_task_id = submit(
            manga_manager.details_for_entry, args=(entry,),
            on_done=lambda details, k=entry.key: self._on_details_loaded(k, details),
            on_error=lambda error, k=entry.key: self._on_details_error(k, error),
        )

    def _on_details_loaded(self, key: str, details) -> None:
        self._details_task_id = None
        if key != self._current_details_key:
            return
        if details is None:
            self._details.show_error('not found')
            return
        self._details_cache[key] = details
        self._details_cache.move_to_end(key)
        while len(self._details_cache) > 25:
            self._details_cache.popitem(last=False)
        self._details.show_details(details)

    def _on_details_error(self, key: str, message: str) -> None:
        self._details_task_id = None
        if key != self._current_details_key:
            return
        logger.warning('Manga details load failed: %s', message)
        self._details.show_error(message)

    def _show_grid(self) -> None:
        if self._details_task_id is not None:
            cancel(self._details_task_id)
            self._details_task_id = None
        self._current_details_key = None
        self._switch_stack_animated(self._grid)

    def shutdown(self) -> None:
        if self._task_id is not None:
            cancel(self._task_id)
        if self._details_task_id is not None:
            cancel(self._details_task_id)
        self._details.shutdown()
        self._grid.clear()


class BooksBrowserTab(MediaPageBase):
    _DETAILS_CACHE_LIMIT = 25

    def __init__(self, download_manager, parent=None):
        super().__init__(parent)
        from src.core.books.manager import BookDownloadBridge
        self._download_bridge = BookDownloadBridge(download_manager, self)
        self._query = ''
        self._has_more = True
        self._next_page = 0
        self._pending_pages: set[int] = set()
        self._task_ids: dict[int, str] = {}
        self._details_task_id: str | None = None
        self._details_cache: "OrderedDict[str, BookItem]" = OrderedDict()
        self._current_details_key: str | None = None
        self._all_sources = books_manager.provider_keys()
        self._selected_sources: list[str] = list(self._all_sources)
        self.setStyleSheet('background: transparent;')
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(14)
        header_row = QHBoxLayout()
        self._search_edit = SearchLineEdit(self)
        self._search_edit.setMinimumWidth(140)
        self._search_edit.setMaximumWidth(280)
        self._search_edit.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self._search_edit.setPlaceholderText(tr('books.search_placeholder'))
        self._search_edit.returnPressed.connect(self._on_search)
        self._search_edit.searchSignal.connect(self._on_search)
        self._search_edit.clearSignal.connect(self._on_search_cleared)
        header_row.addWidget(self._search_edit)
        self._filter_btn = PushButton(themed_icon('books.filter_sources', FluentIcon.FILTER), tr('books.filter_sources'), self)
        self._filter_btn.setFixedHeight(32)
        self._filter_btn.clicked.connect(self._on_filter_clicked)
        header_row.addWidget(self._filter_btn)

        self._refresh_btn = TransparentToolButton(themed_icon('repacks.refresh', FluentIcon.SYNC), self)
        apply_tool_button_theme(self._refresh_btn)
        self._refresh_btn.setToolTip(tr('repacks.refresh_tooltip'))
        self._refresh_btn.setFixedSize(32, 32)
        self._refresh_btn.clicked.connect(self._on_refresh_clicked)
        header_row.addWidget(self._refresh_btn)
        header_row.addStretch(1)
        outer.addLayout(header_row)

        self._stack = QStackedWidget()
        outer.addWidget(self._stack, 1)

        self._grid = BookGridView(self)
        self._grid.book_clicked.connect(self._show_details)
        self._grid.near_bottom.connect(self._on_near_bottom)
        view_btn = ViewModeButton(self._grid, parent=self, list_tip=tr('books.list_view', default='List view'),
                                  grid_tip=tr('books.grid_view', default='Grid view'))
        apply_tool_button_theme(view_btn)
        header_row.insertWidget(header_row.count() - 1, view_btn)
        self._stack.addWidget(self._grid)

        self._details = BookDetailsView(self._download_bridge)
        self._details.back_requested.connect(self._show_grid)

        self._stack.addWidget(self._details)

        self._stack.setCurrentWidget(self._grid)
        register_locale_refresh(self, self._apply_locale)
        self._request_page(0)

    def _apply_locale(self, *_args) -> None:
        self._search_edit.setPlaceholderText(tr('books.search_placeholder'))
        self._filter_btn.setText(tr('books.filter_sources'))

    def _on_search(self) -> None:
        query = self._search_edit.text().strip()
        if query == self._query:
            return
        self._query = query
        self._reset_and_load_first()

    def _on_search_cleared(self) -> None:
        if not self._query:
            return
        self._query = ''
        self._reset_and_load_first()

    def _on_filter_clicked(self) -> None:
        dialog = BookSourceFilterDialog(self._all_sources, self._selected_sources, self.window())
        if not dialog.exec():
            return
        selected = dialog.selected_sources()
        if not selected:
            InfoBar.warning(
                title=tr('books.no_sources_title'),
                content=tr('books.no_sources_content'),
                orient=Qt.Orientation.Horizontal,
                isClosable=True,
                position=InfoBarPosition.TOP_RIGHT,
                duration=3000,
                parent=self.window(),
            )
            return
        if selected == self._selected_sources:
            return
        self._selected_sources = selected
        self._reset_and_load_first()

    def _on_near_bottom(self) -> None:
        if not self._has_more:
            return
        self._request_page(self._next_page)

    def _on_refresh_clicked(self) -> None:
        self._details_cache.clear()
        if self._stack.currentWidget() is not self._grid:
            self._show_grid()
        self._reset_and_load_first(use_cache=False)

    def _reset_and_load_first(self, use_cache: bool = True) -> None:
        for task_id in self._task_ids.values():
            cancel(task_id)
        self._task_ids.clear()
        self._pending_pages.clear()
        self._has_more = True
        self._next_page = 0
        self._grid.clear()
        self._request_page(0, use_cache=use_cache)

    def _request_page(self, grid_page_no: int, use_cache: bool = True) -> None:
        if grid_page_no < 0 or grid_page_no in self._pending_pages or grid_page_no < self._next_page:
            return
        if not self._has_more:
            return
        self._pending_pages.add(grid_page_no)
        manager_page = grid_page_no + 1
        if self._query:
            task_id = submit(
                books_manager.search_all,
                args=(self._query, manager_page),
                kwargs={'sources': list(self._selected_sources), 'use_cache': use_cache},
                on_done=lambda result, p=grid_page_no: self._on_page_loaded(p, result),
                on_error=lambda error, p=grid_page_no: self._on_page_error(p, error),
            )
        else:
            task_id = submit(
                books_manager.browse_all,
                args=(manager_page,),
                kwargs={'sources': list(self._selected_sources), 'use_cache': use_cache},
                on_done=lambda result, p=grid_page_no: self._on_page_loaded(p, result),
                on_error=lambda error, p=grid_page_no: self._on_page_error(p, error),
            )
        self._task_ids[grid_page_no] = task_id

    def _on_page_loaded(self, grid_page_no: int, result) -> None:
        self._pending_pages.discard(grid_page_no)
        self._task_ids.pop(grid_page_no, None)
        self._has_more = result.has_more
        self._next_page = max(self._next_page, grid_page_no + 1)
        if grid_page_no == 0:
            self._grid.set_entries(result.entries)
        else:
            self._grid.append_entries(result.entries)

    def _on_page_error(self, grid_page_no: int, error: str) -> None:
        logger.warning('Books page load failed: %s', error)
        self._pending_pages.discard(grid_page_no)
        self._task_ids.pop(grid_page_no, None)
        if grid_page_no == 0:
            self._has_more = False

    def _show_details(self, entry: BookItem) -> None:
        if self._details_task_id is not None:
            cancel(self._details_task_id)
            self._details_task_id = None
        self._current_details_key = entry.key
        self._details.show_loading(entry)
        self._switch_stack_animated(self._details)

        cached = self._details_cache.get(entry.key)
        if cached is not None:
            self._details_cache.move_to_end(entry.key)
            self._details.show_details(cached)
            return

        self._details_task_id = submit(
            books_manager.details_for_entry,
            args=(entry,),
            on_done=lambda details, k=entry.key: self._on_details_loaded(k, details),
            on_error=lambda error, k=entry.key: self._on_details_error(k, error),
        )

    def _on_details_loaded(self, key: str, details: BookItem | None) -> None:
        self._details_task_id = None
        if key != self._current_details_key:
            return
        if details is None:
            self._details.show_error('not found')
            return
        self._details_cache[key] = details
        self._details_cache.move_to_end(key)
        while len(self._details_cache) > self._DETAILS_CACHE_LIMIT:
            self._details_cache.popitem(last=False)
        self._details.show_details(details)

    def _on_details_error(self, key: str, message: str) -> None:
        self._details_task_id = None
        if key != self._current_details_key:
            return
        logger.warning('Book details load failed: %s', message)
        self._details.show_error(message)

    def _show_grid(self) -> None:
        if self._details_task_id is not None:
            cancel(self._details_task_id)
            self._details_task_id = None
        self._current_details_key = None
        self._switch_stack_animated(self._grid)
        self._details.release_full_cover()

    def shutdown(self) -> None:
        try:
            images.release(self._details)
        except (TypeError, RuntimeError):
            pass
        self._grid.shutdown()
        self._download_bridge.shutdown()


class BooksPage(QWidget):
    def __init__(self, download_manager, parent=None, manga_download_bridge=None):
        super().__init__(parent)
        self.setObjectName('booksPage')
        self.setStyleSheet('background: transparent;')
        outer = QVBoxLayout(self)
        outer.setContentsMargins(20, 12, 20, 20)
        outer.setSpacing(14)

        self._pivot = Pivot()
        outer.addWidget(self._pivot)

        self._section_stack = QStackedWidget()
        outer.addWidget(self._section_stack, 1)

        self._tabs: dict[str, QWidget] = {}
        self._tab_display_names: dict[str, str] = {}

        self._books_tab = BooksBrowserTab(download_manager, self)
        self._add_tab('books', tr('settings.tab_books_title'), self._books_tab)

        self._manga_tab = MangaBrowserTab(
            download_manager, self, manga_download_bridge=manga_download_bridge
        )
        self._add_tab('manga', tr('settings.tab_manga_title'), self._manga_tab)

        self._current_tab_key = 'books'
        self._rebuild_tab_selector()
        from src.core.config import tab_visibility_bus as _tabvis_bus
        _tabvis_bus.changed.connect(self._on_tab_visibility_changed)

    def _add_tab(self, key: str, display_name: str, widget: QWidget) -> None:
        self._tabs[key] = widget
        self._tab_display_names[key] = display_name
        self._section_stack.addWidget(widget)

    def _rebuild_tab_selector(self) -> None:
        from src.core.config import enabled_tab_keys
        enabled = [k for k in enabled_tab_keys('books') if k in self._tabs]
        if not enabled:
            enabled = list(self._tabs.keys())
        current = self._current_tab_key if self._current_tab_key in enabled else enabled[0]
        if getattr(self, '_tab_handler_connected', False):
            try:
                self._pivot.currentItemChanged.disconnect(self._on_tab_changed)
            except (RuntimeError, TypeError):
                pass
        self._pivot.clear()
        for key in enabled:
            self._pivot.addItem(routeKey=key, text=self._tab_display_names.get(key, key))
        apply_pivot_theme(self._pivot)
        self._pivot.currentItemChanged.connect(self._on_tab_changed)
        self._tab_handler_connected = True
        self._pivot.setVisible(len(enabled) > 1)
        self._pivot.setCurrentItem(current)
        self._section_stack.setCurrentWidget(self._tabs[current])
        self._current_tab_key = current

    def _on_tab_visibility_changed(self, scope: str) -> None:
        if scope == 'books':
            self._rebuild_tab_selector()

    def _on_tab_changed(self, key: str) -> None:
        widget = self._tabs.get(key)
        if widget is not None:
            self._section_stack.setCurrentWidget(widget)
            self._current_tab_key = key

    def open_detail(self, entry) -> None:
        from src.core.config import is_tab_enabled
        if not is_tab_enabled('books', 'books'):
            return
        self._pivot.setCurrentItem('books')
        self._section_stack.setCurrentWidget(self._books_tab)
        self._current_tab_key = 'books'
        if hasattr(self._books_tab, '_show_details'):
            self._books_tab._show_details(entry)

    def open_manga_detail(self, entry) -> None:
        from src.core.config import is_tab_enabled
        if not is_tab_enabled('books', 'manga'):
            return
        self._pivot.setCurrentItem('manga')
        self._section_stack.setCurrentWidget(self._manga_tab)
        self._current_tab_key = 'manga'
        if hasattr(self._manga_tab, '_show_details'):
            self._manga_tab._show_details(entry)

    def shutdown(self) -> None:
        for tab in self._tabs.values():
            shutdown_fn = getattr(tab, 'shutdown', None)
            if callable(shutdown_fn):
                try:
                    shutdown_fn()
                except Exception:
                    logger.exception('Failed to shut down books sub-tab')
