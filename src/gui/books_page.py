from __future__ import annotations
import html
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
    Pivot, TransparentToolButton, ComboBox,
)

from src.core.worker import submit, cancel
from src.core.books.manga import manager as manga_manager
from src.core.books.manga.manager import MangaItem as MangaEntry, MangaChapter, MangaDownloadBridge
from src.core.artwork import images
from src.core.theme import palette, apply_pivot_theme, themed_icon, apply_tool_button_theme, register_theme_refresh, fade_in
from src.core.translations import tr, register_locale_refresh
from .repacks_page import (
    bind_enter, split_tags, join_tags, partition_resolved, MetaField, make_tag_pill, render_description_html, _DiceButton, make_tag_chip, show_tag_menu, toggled_tags,
    _stop_previous_movie,
    _muted_text_color, _body_text_color, _surface_tint_color, _grid_colors,
)
from .grid import PosterGrid, ViewModeButton, attrs_subtitle
from .home_page import AccentColorMixin, DetailsViewMixin, MediaPageBase
from .tv_page import _suggest_genres

logger = logging.getLogger(__name__)

_TAG_QUERY_RE = re.compile(r'^\s*tags?\s*[:\uff1a]\s*(.*)$', re.IGNORECASE | re.DOTALL)


def _clean_query(text: str) -> str:
    value = re.sub(r'\s+', ' ', str(text or '')).strip()
    return value.strip('"\'.,;:!?()[]{}<>')


def _parse_tag_query(text: str):
    match = _TAG_QUERY_RE.match(text or '')
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


def _manga_icon() -> QIcon:
    for name in ('BOOK_SHELF', 'LIBRARY', 'EDUCATION', 'DOCUMENT', 'FOLDER', 'GAME'):
        icon = getattr(FluentIcon, name, None)
        if icon is not None:
            return icon.icon(color=QColor(_body_text_color()))
    return QIcon()


class MangaGridView(PosterGrid):
    manga_clicked = Signal(object)
    _NEAR_BOTTOM_THRESHOLD_PX = 400

    def __init__(self, parent=None):
        super().__init__(parent, cover_attrs=('cover_url',), key_attr='key', show_caption=True,
                         subtitle_fn=attrs_subtitle('status', 'year'),
                         fallback_icon_fn=_manga_icon, colors_fn=_grid_colors)
        self.poster_clicked.connect(self.manga_clicked.emit)

    def add_entries(self, entries: list[MangaEntry]) -> None:
        self.append_entries(entries)

    def shutdown(self) -> None:
        self.clear()


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

    def __init__(self, chapters: list[MangaChapter], parent=None, manga_title: str = '', data_saver: bool = False):
        super().__init__(parent)
        self._checks: list[tuple[CheckBox, MangaChapter]] = []
        self.titleLabel = SubtitleLabel(tr('books.manga_choose_chapters'), self)
        self.viewLayout.addWidget(self.titleLabel)

        select_row = QHBoxLayout()
        self._select_all_btn = PushButton(tr('books.manga_select_all'), self)
        self._select_all_btn.clicked.connect(self._toggle_select_all)
        select_row.addWidget(self._select_all_btn)
        self._count_lbl = CaptionLabel('', self)
        self._count_lbl.setStyleSheet(f'color: {_muted_text_color()};')
        select_row.addWidget(self._count_lbl)
        select_row.addStretch(1)
        self.viewLayout.addLayout(select_row)

        self._saver_chk = CheckBox(tr('books.manga_data_saver', default='Data saver (smaller files, lower quality)'), self)
        self._saver_chk.setChecked(data_saver)
        self.viewLayout.addWidget(self._saver_chk)

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
        have = manga_manager.downloaded_ids(manga_title, chapters) if manga_title else set()
        done_text = tr('books.manga_downloaded', default='Downloaded')
        for chapter in chapters:
            label = chapter.label
            if chapter.scanlation_group:
                label = f'{label}  ·  {chapter.scanlation_group}'
            already = chapter.id in have
            if already:
                label = f'{label}  ({done_text})'
            cb = CheckBox(label, container)
            cb.setChecked(not already)
            cb.stateChanged.connect(self._update_state)
            self._checks.append((cb, chapter))
            col.addWidget(cb)
        col.addStretch(1)
        scroll.setWidget(container)
        self.viewLayout.addWidget(scroll)

        self.yesButton.setText(tr('books.manga_download_selected'))
        self.cancelButton.setText(tr('books.cancel'))
        self.widget.setMinimumWidth(360)
        self._update_state()

    @property
    def data_saver(self) -> bool:
        return self._saver_chk.isChecked()

    def _toggle_select_all(self) -> None:
        any_unchecked = any(not cb.isChecked() for cb, _ in self._checks)
        for cb, _ in self._checks:
            cb.blockSignals(True)
            cb.setChecked(any_unchecked)
            cb.blockSignals(False)
        self._update_state()

    def _update_state(self, *_args) -> None:
        selected = sum(1 for cb, _ in self._checks if cb.isChecked())
        self._warning_lbl.setVisible(selected > self._WARN_THRESHOLD)
        self._count_lbl.setText(f'{selected} / {len(self._checks)}')
        self.yesButton.setEnabled(selected > 0)

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
        dialog = MangaChapterSelectDialog(
            downloadable, parent=self.window(), manga_title=self._entry.title,
            data_saver=self._download_bridge.data_saver,
        )
        if not dialog.exec():
            return
        self._download_bridge.data_saver = dialog.data_saver
        authors = ', '.join(self._entry.authors)
        for chapter in dialog.selected_chapters():
            try:
                self._download_bridge.download_chapter(self._entry.title, chapter, authors=authors)
            except Exception as exc:
                logger.warning('Failed to queue manga chapter download: %s', exc)

    def shutdown(self) -> None:
        pass


_NO_VOLUME = object()


class MangaDetailsView(DetailsViewMixin, QWidget):
    back_requested = Signal()
    tag_requested = Signal(str)

    COVER_WIDTH = 220
    COVER_HEIGHT = 310
    _ROW_BATCH = 40

    def __init__(self, download_bridge: MangaDownloadBridge, parent=None):
        super().__init__(parent)
        self.setStyleSheet('background: transparent;')
        self._download_bridge = download_bridge
        self._cover_url = ''
        self._entry: MangaEntry | None = None
        self._newest_first = False
        self._row_gen = 0
        self._language = 'en'
        self._downloaded: set[str] = set()
        self._has_volumes = False
        self._row_widgets: dict[str, tuple[CaptionLabel, TransparentToolButton]] = {}

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

        self._alt_lbl = CaptionLabel('')
        self._alt_lbl.setWordWrap(True)
        self._alt_lbl.setStyleSheet(f'color: {_muted_text_color()};')
        self._alt_lbl.setVisible(False)
        info_col.addWidget(self._alt_lbl)

        self._meta_grid = QGridLayout()
        self._meta_grid.setHorizontalSpacing(40)
        self._meta_grid.setVerticalSpacing(10)
        info_col.addLayout(self._meta_grid)

        self._links_lbl = BodyLabel('')
        self._links_lbl.setTextFormat(Qt.TextFormat.RichText)
        self._links_lbl.setOpenExternalLinks(True)
        self._links_lbl.setWordWrap(True)
        self._links_lbl.setVisible(False)
        info_col.addWidget(self._links_lbl)

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

        chapters_row = QHBoxLayout()
        chapters_header = StrongBodyLabel(tr('books.manga_chapters'))
        chapters_header.setStyleSheet(f'font-size: 16px; font-weight: 600; color: {palette()["primary_text"]};')
        chapters_row.addWidget(chapters_header)
        self._count_lbl = CaptionLabel('')
        self._count_lbl.setStyleSheet(f'color: {_muted_text_color()};')
        chapters_row.addWidget(self._count_lbl)
        chapters_row.addStretch(1)
        self._order_btn = TransparentToolButton(FluentIcon.DOWN, self)
        apply_tool_button_theme(self._order_btn)
        self._order_btn.setFixedSize(28, 28)
        self._order_btn.setToolTip(tr('books.manga_reverse_order', default='Reverse order'))
        self._order_btn.clicked.connect(self._toggle_order)
        chapters_row.addWidget(self._order_btn)
        content_layout.addLayout(chapters_row)

        self._chapters_container = QWidget()
        self._chapters_layout = QVBoxLayout(self._chapters_container)
        self._chapters_layout.setContentsMargins(0, 0, 0, 0)
        self._chapters_layout.setSpacing(6)
        content_layout.addWidget(self._chapters_container)
        content_layout.addStretch(1)

        self._loading_lbl = BodyLabel(tr('books.loading_details'), self)
        self._loading_lbl.setStyleSheet(f'color: {_muted_text_color()};')
        content_layout.addWidget(self._loading_lbl)

        download_bridge.chapter_state.connect(self._on_chapter_state)

    def _deferred_apply_open_btn_style(self) -> None:
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

    def _chapter_button_qss(self) -> str:
        color = themeColor()
        base_alpha = 55 if isDarkTheme() else 40
        hover_alpha = 85 if isDarkTheme() else 65
        text_color = palette()['primary_text']
        neutral = QColor(_muted_text_color())
        neutral_base_alpha = 45 if isDarkTheme() else 35
        neutral_hover_alpha = 75 if isDarkTheme() else 55
        return (
            f'HyperlinkButton {{ background-color: rgba({color.red()}, {color.green()}, {color.blue()}, {base_alpha}); '
            f'border-radius: 6px; padding: 4px 12px; font-weight: 600; color: {text_color}; }} '
            f'HyperlinkButton:hover {{ background-color: rgba({color.red()}, {color.green()}, {color.blue()}, {hover_alpha}); color: {text_color}; }} '
            f'TransparentToolButton {{ background-color: rgba({neutral.red()}, {neutral.green()}, {neutral.blue()}, {neutral_base_alpha}); border-radius: 6px; }} '
            f'TransparentToolButton:hover {{ background-color: rgba({neutral.red()}, {neutral.green()}, {neutral.blue()}, {neutral_hover_alpha}); }}'
        )


    def show_loading(self, entry: MangaEntry) -> None:
        self._entry = None
        self._title_lbl.setText(entry.title)
        self._alt_lbl.setVisible(False)
        self._links_lbl.setVisible(False)
        self._count_lbl.setText('')
        self._cover_lbl.setImage(QPixmap())
        self._desc_lbl.setText('')
        self._clear_meta()
        self._clear_tags()
        self._clear_chapters()
        self._open_btn.setUrl(entry.web_url)
        self._download_action.set_accent_color(None)
        self._download_action.set_entry(None)
        self._loading_lbl.setVisible(True)
        cover_url = entry.cover_url or entry.cover_url_full
        self._cover_url = cover_url
        if cover_url:
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

    def show_details(self, entry: MangaEntry, language: str = 'en') -> None:
        self._entry = entry
        self._language = language
        self._loading_lbl.setVisible(False)
        self._title_lbl.setText(entry.title)
        self._open_btn.setUrl(entry.web_url)
        self._download_action.set_entry(entry)
        self._desc_lbl.setText(render_description_html(_clean_manga_description(entry.description)) or tr('books.no_description'))

        full = entry.cover_url_full or entry.cover_url
        if full and full != self._cover_url:
            self._cover_url = full
            self._apply_cover_path(full)

        self._alt_lbl.setText('  ·  '.join(entry.alt_titles[:4]))
        self._alt_lbl.setVisible(bool(entry.alt_titles))

        self._clear_meta()
        fields: list[tuple[str, str]] = []
        if entry.authors:
            fields.append((tr('books.manga_author', default='Author'), ', '.join(entry.authors[:3])))
        if entry.artists and entry.artists != entry.authors:
            fields.append((tr('books.manga_artist', default='Artist'), ', '.join(entry.artists[:3])))
        if entry.status:
            fields.append((tr('books.manga_status'), entry.status))
        if entry.year:
            fields.append((tr('books.manga_year'), str(entry.year)))
        if entry.demographic:
            fields.append((tr('books.manga_demographic', default='Demographic'), entry.demographic))
        if entry.content_rating:
            fields.append((tr('books.manga_content_rating'), entry.content_rating))
        if entry.original_language:
            fields.append((tr('books.manga_original_language', default='Original language'), entry.original_language.upper()))
        if entry.rating:
            fields.append((tr('books.manga_rating', default='Rating'), f'★ {entry.rating:.2f}'))
        if entry.follows:
            fields.append((tr('books.manga_follows', default='Follows'), f'{entry.follows:,}'))
        for i, (label, value) in enumerate(fields):
            row, col = divmod(i, 4)
            self._meta_grid.addWidget(MetaField(label, value), row, col)

        if entry.links:
            color = themeColor().name()
            anchors = ' &nbsp;·&nbsp; '.join(
                f'<a href="{html.escape(url, quote=True)}" style="color:{color}; text-decoration:none;">{html.escape(name)}</a>'
                for name, url in entry.links
            )
            self._links_lbl.setText(f'{tr("books.manga_links", default="Links")}: {anchors}')
        self._links_lbl.setVisible(bool(entry.links))

        self._clear_tags()
        seen_tags: set[str] = set()
        for tag in entry.tags:
            key = tag.strip().lower()
            if not key or key in seen_tags:
                continue
            seen_tags.add(key)
            pill = make_tag_pill(tag, self._tags_container, on_click=self.tag_requested.emit,
                                 tip=tr('books.manga_tag_tip', default='Show manga tagged "{tag}"', tag=tag))
            pill.adjustSize()
            self._tags_layout.addWidget(pill)
            self._tag_widgets.append(pill)
        if self._tag_widgets:
            QTimer.singleShot(0, self._tags_layout.update)

        self._render_chapters()


    def _toggle_order(self) -> None:
        self._newest_first = not self._newest_first
        self._order_btn.setIcon(FluentIcon.UP if self._newest_first else FluentIcon.DOWN)
        self._render_chapters()

    def _render_chapters(self) -> None:
        self._row_gen += 1
        self._clear_chapters()
        entry = self._entry
        if entry is None:
            return
        if not entry.chapters:
            self._count_lbl.setText('')
            text = tr('books.manga_no_chapters')
            others = [manga_manager.LANGUAGES.get(c, c) for c in entry.available_languages if c != self._language]
            if others:
                text = f'{text}  {tr("books.manga_available_in", default="Available in")}: {", ".join(others[:8])}'
            empty_lbl = CaptionLabel(text, self._chapters_container)
            empty_lbl.setWordWrap(True)
            empty_lbl.setStyleSheet(f'color: {_muted_text_color()};')
            self._chapters_layout.addWidget(empty_lbl)
            return

        chapters = list(reversed(entry.chapters)) if self._newest_first else entry.chapters
        downloadable = sum(1 for c in chapters if c.is_downloadable)
        self._count_lbl.setText(f'{len(chapters)}  ·  {downloadable} {tr("books.manga_downloadable", default="downloadable")}')
        self._downloaded = manga_manager.downloaded_ids(entry.title, chapters)
        self._chapters_container.setStyleSheet(self._chapter_button_qss())
        self._has_volumes = any(c.volume for c in chapters)
        self._build_batch(self._row_gen, iter(chapters), _NO_VOLUME)

    def _build_batch(self, gen: int, chapters, last_volume) -> None:
        if gen != self._row_gen or self._entry is None:
            return
        for _ in range(self._ROW_BATCH):
            chapter = next(chapters, None)
            if chapter is None:
                return
            if self._has_volumes and chapter.volume != last_volume:
                last_volume = chapter.volume
                header = (f'{tr("books.manga_volume", default="Volume")} {chapter.volume}' if chapter.volume
                          else tr('books.manga_no_volume', default='No volume'))
                head_lbl = StrongBodyLabel(header, self._chapters_container)
                head_lbl.setStyleSheet(f'color: {palette()["primary_text"]}; padding-top: 10px;')
                self._chapters_layout.addWidget(head_lbl)
            self._chapters_layout.addWidget(self._build_chapter_row(self._entry, chapter))
        QTimer.singleShot(0, lambda: self._build_batch(gen, chapters, last_volume))

    def _build_chapter_row(self, entry: MangaEntry, chapter: MangaChapter) -> QWidget:
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

        if chapter.published:
            date_lbl = CaptionLabel(chapter.published, row)
            date_lbl.setStyleSheet(f'color: {_muted_text_color()};')
            row_layout.addWidget(date_lbl, 0)

        row_layout.addWidget(HyperlinkButton(chapter.external_url or chapter.web_url, tr('books.manga_read'), row), 0)

        if chapter.is_downloadable:
            status_lbl = CaptionLabel(
                tr('books.manga_downloaded', default='Downloaded') if chapter.id in self._downloaded else '', row)
            status_lbl.setStyleSheet(f'color: {_muted_text_color()};')
            row_layout.addWidget(status_lbl, 0)

            download_btn = TransparentToolButton(themed_icon('books.chapter_download', FluentIcon.DOWNLOAD), row)
            download_btn.setFixedSize(28, 28)
            download_btn.setToolTip(tr('books.manga_download'))
            download_btn.clicked.connect(lambda _checked=False, c=chapter: self._start_chapter_download(entry, c))
            row_layout.addWidget(download_btn, 0, Qt.AlignmentFlag.AlignRight)
            self._row_widgets[chapter.id] = (status_lbl, download_btn)
        return row

    def _start_chapter_download(self, entry: MangaEntry, chapter: MangaChapter) -> None:
        try:
            self._download_bridge.download_chapter(entry.title, chapter, authors=', '.join(entry.authors))
        except Exception as exc:
            logger.warning('Failed to queue manga chapter download: %s', exc)
            self._on_chapter_state(chapter.id, 'error', 0.0)

    def _on_chapter_state(self, chapter_id: str, state: str, percent: float) -> None:
        widgets = self._row_widgets.get(chapter_id)
        if widgets is None:
            return
        status_lbl, btn = widgets
        try:
            if state == 'active':
                status_lbl.setText(f'{percent:.0f}%' if percent > 0 else tr('books.manga_downloading'))
            elif state == 'done':
                status_lbl.setText(tr('books.manga_downloaded', default='Downloaded'))
            else:
                status_lbl.setText(tr('books.manga_download_failed'))
            btn.setEnabled(state != 'active')
        except RuntimeError:
            self._row_widgets.pop(chapter_id, None)

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
        self._row_widgets.clear()
        while self._chapters_layout.count():
            item = self._chapters_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()

    def shutdown(self) -> None:
        self._row_gen += 1
        try:
            images.release(self)
        except (TypeError, RuntimeError):
            pass


class MangaBrowserTab(MediaPageBase):
    def __init__(self, download_manager, parent=None, manga_download_bridge=None):
        super().__init__(parent)
        self.setStyleSheet('background: transparent;')
        self._download_bridge = manga_download_bridge or MangaDownloadBridge(download_manager, self)
        self._filters = manga_manager.BrowseFilters()
        self._query = ''
        self._sort_before_search = 'followedCount'
        self._page = 1
        self._gen = 0
        self._has_more = True
        self._loading = False
        self._task_id: str | None = None
        self._details_task_id: str | None = None
        self._tag_names: list[str] = []
        self._tag_pool: list[str] = []
        self._tag_ids: list[str] = []
        self._tag_excl: list[str] = []
        self._tag_excl_ids: list[str] = []
        self._want_exc: list[str] = []
        self._tag_missing: list[str] = []
        self._tag_seq = 0
        self._tag_ticket = None
        self._last_tag_query: list[str] = []
        self._random_ticket = None
        self._random_token = 0
        self._last_random: str | None = None
        self._details_cache: "OrderedDict[str, MangaEntry]" = OrderedDict()
        self._current_details_key: str | None = None

        self._search_timer = QTimer(self)
        self._search_timer.setSingleShot(True)
        self._search_timer.setInterval(400)
        self._search_timer.timeout.connect(self._on_search)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(10)

        self._grid = MangaGridView(self)
        self._grid.manga_clicked.connect(self._show_details)
        self._grid.near_bottom.connect(self._on_near_bottom)

        header_row = QHBoxLayout()
        self._search_edit = SearchLineEdit(self)
        self._search_edit.setMinimumWidth(140)
        self._search_edit.setMaximumWidth(240)
        self._search_edit.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self._search_edit.setPlaceholderText(tr('books.manga_search_placeholder'))
        bind_enter(self._search_edit, self._on_search)
        self._search_edit.searchSignal.connect(self._on_search)
        self._search_edit.clearSignal.connect(self._on_search)
        self._search_edit.textChanged.connect(self._on_search_text_changed)
        header_row.addWidget(self._search_edit)

        view_btn = ViewModeButton(self._grid, parent=self, list_tip=tr('books.list_view', default='List view'),
                                  grid_tip=tr('books.grid_view', default='Grid view'))
        apply_tool_button_theme(view_btn)
        header_row.addWidget(view_btn)

        self._refresh_btn = TransparentToolButton(themed_icon('repacks.refresh', FluentIcon.SYNC), self)
        apply_tool_button_theme(self._refresh_btn)
        self._refresh_btn.setToolTip(tr('repacks.refresh_tooltip'))
        self._refresh_btn.setFixedSize(32, 32)
        self._refresh_btn.clicked.connect(self._on_refresh_clicked)
        header_row.addWidget(self._refresh_btn)

        self._random_btn = _DiceButton(tr('repacks.random', default='Surprise me'), self)
        self._random_btn.clicked.connect(self._on_random)
        header_row.addWidget(self._random_btn)
        header_row.setSpacing(8)
        outer.addLayout(header_row)

        self._sort = self._make_combo([
            ('relevance', tr('books.manga_sort_relevance', default='Best match')),
            ('followedCount', tr('books.manga_sort_popular', default='Popularity')),
            ('latestUploadedChapter', tr('books.manga_sort_latest', default='Latest update')),
            ('rating', tr('books.manga_sort_rating', default='Top rated')),
            ('createdAt', tr('books.manga_sort_new', default='Newest')),
            ('title', tr('books.manga_sort_title', default='Title A–Z')),
        ], current='followedCount')
        self._status = self._make_combo([
            ('', tr('books.manga_status_any', default='Any status')),
            ('ongoing', tr('books.manga_status_ongoing', default='Ongoing')),
            ('completed', tr('books.manga_status_completed', default='Completed')),
            ('hiatus', tr('books.manga_status_hiatus', default='Hiatus')),
            ('cancelled', tr('books.manga_status_cancelled', default='Cancelled')),
        ])
        self._genre = self._make_combo([('', tr('books.manga_all_genres', default='All genres'))])
        self._lang = self._make_combo(list(manga_manager.LANGUAGES.items()), current='en')
        self._safe = CheckBox(tr('books.manga_safe_only', default='Safe only'), self)
        self._safe.stateChanged.connect(self._on_filter_changed)
        filters = (self._sort, self._status, self._genre, self._lang, self._safe)
        for i, widget in enumerate(filters, start=1):
            header_row.insertWidget(i, widget)
        header_row.insertStretch(1 + len(filters), 1)

        self._tag_bar = QWidget(self)
        self._tag_bar.setStyleSheet('background: transparent;')
        tag_row = QHBoxLayout(self._tag_bar)
        tag_row.setContentsMargins(0, 0, 0, 0)
        tag_row.setSpacing(8)
        self._tag_hint = CaptionLabel('', self._tag_bar)
        self._tag_hint.setStyleSheet(f'color: {_muted_text_color()}; background: transparent;')
        self._tag_pills = QHBoxLayout()
        self._tag_pills.setContentsMargins(0, 0, 0, 0)
        self._tag_pills.setSpacing(6)
        self._tag_note = CaptionLabel('', self._tag_bar)
        self._tag_note.setStyleSheet(f'color: {_muted_text_color()}; background: transparent;')
        self._tag_note.setVisible(False)
        self._tag_add = TransparentToolButton(FluentIcon.ADD, self._tag_bar)
        apply_tool_button_theme(self._tag_add)
        self._tag_add.setFixedSize(24, 24)
        self._tag_add.setIconSize(QSize(12, 12))
        self._tag_add.setToolTip(tr('books.manga_tag_add', default='Add or remove genres'))
        self._tag_add.clicked.connect(lambda _c=False: self._open_tag_menu())
        self._tag_clear = TransparentToolButton(FluentIcon.CLOSE, self._tag_bar)
        apply_tool_button_theme(self._tag_clear)
        self._tag_clear.setFixedSize(24, 24)
        self._tag_clear.setIconSize(QSize(12, 12))
        self._tag_clear.setToolTip(tr('books.manga_tag_back', default='Back to browsing'))
        self._tag_clear.clicked.connect(lambda _c=False: self._clear_tag_search())
        tag_row.addWidget(self._tag_hint)
        tag_row.addLayout(self._tag_pills)
        tag_row.addWidget(self._tag_add)
        tag_row.addWidget(self._tag_clear)
        tag_row.addWidget(self._tag_note)
        tag_row.addStretch(1)
        self._tag_bar.setVisible(False)
        outer.addWidget(self._tag_bar)
        self._suggest_bar = QWidget(self)
        self._suggest_bar.setStyleSheet('background: transparent;')
        self._suggest_row = QHBoxLayout(self._suggest_bar)
        self._suggest_row.setContentsMargins(0, 0, 0, 0)
        self._suggest_row.setSpacing(8)
        self._suggest_bar.setVisible(False)
        outer.addWidget(self._suggest_bar)

        self._status_lbl = CaptionLabel('', self)
        self._status_lbl.setStyleSheet(f'color: {_muted_text_color()};')
        self._status_lbl.setVisible(False)
        outer.addWidget(self._status_lbl)

        self._stack = QStackedWidget()
        outer.addWidget(self._stack, 1)
        self._stack.addWidget(self._grid)

        self._details = MangaDetailsView(self._download_bridge, self)
        self._details.back_requested.connect(self._show_grid)
        self._details.tag_requested.connect(lambda name: self._search_tags([name]))
        self._stack.addWidget(self._details)

        self._stack.setCurrentWidget(self._grid)
        self._load_page(reset=True)
        submit(manga_manager.genres, args=(), on_done=self._on_genres_loaded,
               on_error=lambda m: logger.debug('MangaDex genres unavailable: %s', m))


    def _make_combo(self, options, current: str | None = None) -> ComboBox:
        combo = ComboBox(self)
        combo.setMinimumWidth(115)
        for key, label in options:
            combo.addItem(label, userData=key)
        if current is not None:
            combo.setCurrentIndex(max(0, combo.findData(current)))
        combo.currentIndexChanged.connect(self._on_filter_changed)
        return combo

    def _read_filters(self):
        return manga_manager.BrowseFilters(
            query=self._query,
            sort=self._sort.currentData() or 'followedCount',
            status=self._status.currentData() or '',
            genre=','.join(self._tag_ids) if self._tag_ids else (self._genre.currentData() or ''),
            exclude=','.join(self._tag_excl_ids),
            language=self._lang.currentData() or 'en',
            safe_only=self._safe.isChecked(),
        )

    def _on_filter_changed(self, *_args) -> None:
        new = self._read_filters()
        if new == self._filters:
            return
        self._filters = new
        if self._stack.currentWidget() is not self._grid:
            self._show_grid()
        self._load_page(reset=True)

    def _on_genres_loaded(self, genres) -> None:
        self._genre.blockSignals(True)
        selected = self._genre.currentData() or ''
        for name, tag_id in genres:
            self._genre.addItem(name, userData=tag_id)
        self._genre.setCurrentIndex(max(0, self._genre.findData(selected)))
        self._genre.blockSignals(False)

    def _sync_sort_for_query(self) -> None:
        current = self._sort.currentData()
        if self._query and current != 'relevance':
            self._sort_before_search = current
            target = 'relevance'
        elif not self._query and current == 'relevance':
            target = self._sort_before_search
        else:
            return
        self._sort.blockSignals(True)
        self._sort.setCurrentIndex(max(0, self._sort.findData(target)))
        self._sort.blockSignals(False)

    def _on_search_text_changed(self, text: str) -> None:
        if _parse_tag_query(text) is not None:
            self._search_timer.stop()
            return
        self._search_timer.start()

    def _on_search(self) -> None:
        self._search_timer.stop()
        text = self._search_edit.text()
        tags = _parse_tag_query(text)
        if tags is not None:
            self._search_tags(tags)
            return
        query = _clean_query(text)
        was_tagged = bool(self._tag_ids or self._tag_excl)
        if was_tagged:
            self._reset_tag_state()
        if query == self._query and not was_tagged:
            return
        self._query = query
        self._sync_sort_for_query()
        self._on_filter_changed()

    def _search_tags(self, tags: list) -> None:
        if self._stack.currentWidget() is not self._grid:
            self._show_grid()
        if not tags:
            self._set_status(tr('books.manga_tag_hint', default='Type genres after "tag:", e.g.  tag:romance, action  then press Enter.'))
            return
        if self._tag_ticket is not None:
            cancel(self._tag_ticket)
        self._tag_seq += 1
        seq = self._tag_seq
        self._last_tag_query = list(tags)
        want_inc, self._want_exc = split_tags(tags)
        tags = want_inc + self._want_exc
        self._clear_suggestions()
        self._set_status(tr('books.manga_tag_looking_up', default='Looking up genres\u2026'))
        self._tag_ticket = submit(
            manga_manager.resolve_genres, args=(list(tags),),
            on_done=lambda result, s=seq: self._on_tags_resolved(s, result),
            on_error=lambda message, s=seq: self._on_tags_lookup_failed(s, message),
        )

    def _on_tags_lookup_failed(self, seq: int, message: str) -> None:
        if seq != self._tag_seq:
            return
        self._tag_ticket = None
        logger.warning('MangaDex genre lookup failed: %s', message)
        self._set_status(tr('books.manga_tag_lookup_failed', default='Genre lookup failed. Try again in a moment.'))

    def _on_tags_resolved(self, seq: int, result) -> None:
        if seq != self._tag_seq:
            return
        self._tag_ticket = None
        found = [str(t) for t in (result or {}).get('tags') or ()]
        ids = [str(t) for t in (result or {}).get('ids') or ()]
        missing = [str(t) for t in (result or {}).get('missing') or ()]
        pool = [str(t) for t in (result or {}).get('pool') or ()]
        if pool:
            self._tag_pool = pool
        if not found:
            names = ', '.join(f'"{m}"' for m in missing) or 'that'
            self._set_status(tr('books.manga_tag_unknown', default='No genre named {names}. Check the spelling (e.g. tag:romance, action).', names=names))
            self._show_suggestions(missing, pool)
            return
        self._set_status('')
        id_of = dict(zip(found, ids)) if len(ids) == len(found) else {}
        inc, exc = partition_resolved(found, self._want_exc)
        self._tag_excl_ids = [id_of[n] for n in exc if n in id_of] if id_of else []
        ids = [id_of[n] for n in inc if n in id_of] if id_of else ids
        self._tag_excl = list(exc)
        self._enter_tag_mode(inc, ids, missing)
        self._show_suggestions(missing, pool)

    def _enter_tag_mode(self, found: list, ids: list, missing: list) -> None:
        self._tag_names = list(found)
        self._tag_ids = list(ids)
        self._tag_missing = list(missing)
        self._query = ''
        self._search_timer.stop()
        self._search_edit.blockSignals(True)
        self._search_edit.setText('tag:' + ', '.join(join_tags(found, self._tag_excl)))
        self._search_edit.blockSignals(False)
        self._genre.blockSignals(True)
        self._genre.setCurrentIndex(0)
        self._genre.blockSignals(False)
        self._genre.setEnabled(False)
        self._sync_sort_for_query()
        self._sync_tag_bar()
        self._on_filter_changed()

    def _reset_tag_state(self) -> None:
        self._tag_seq += 1
        if self._tag_ticket is not None:
            cancel(self._tag_ticket)
            self._tag_ticket = None
        self._tag_names = []
        self._tag_ids = []
        self._tag_excl = []
        self._tag_excl_ids = []
        self._tag_missing = []
        self._genre.setEnabled(True)
        self._sync_tag_bar()

    def _clear_tag_search(self) -> None:
        self._reset_tag_state()
        self._query = ''
        self._search_edit.blockSignals(True)
        self._search_edit.setText('')
        self._search_edit.blockSignals(False)
        self._set_status('')
        self._sync_sort_for_query()
        self._on_filter_changed()

    def _sync_tag_bar(self) -> None:
        while self._tag_pills.count():
            item = self._tag_pills.takeAt(0)
            if item.widget() is not None:
                item.widget().deleteLater()
        self._tag_note.setText('')
        self._tag_note.setVisible(False)
        self._clear_suggestions()
        on = bool(self._tag_names or self._tag_excl)
        if on:
            for name in join_tags(self._tag_names, self._tag_excl):
                self._tag_pills.addWidget(make_tag_chip(name, self._tag_bar, self._on_tag_menu_pick))
            self._tag_hint.setText(
                (tr('books.manga_tag_showing_all', default='Showing manga with all of these genres') if len(self._tag_names) > 1
                 else tr('books.manga_tag_showing', default='Showing manga tagged')) if self._tag_names
                else tr('books.manga_tag_hiding', default='Hiding manga tagged'))
            if self._tag_missing:
                self._tag_note.setText(tr('books.manga_tag_ignored', default='Ignored unknown: {names}',
                                          names=', '.join(self._tag_missing)))
                self._tag_note.setVisible(True)
        self._tag_bar.setVisible(on)

    def _open_tag_menu(self) -> None:
        if self._tag_pool:
            show_tag_menu(self._tag_add, self._tag_pool, join_tags(self._tag_names, self._tag_excl), self._on_tag_menu_pick,
                          lambda n: self._on_tag_menu_pick(n, True))

    def _on_tag_menu_pick(self, name: str, exclude: bool = False) -> None:
        names = toggled_tags(join_tags(self._tag_names, self._tag_excl), name, exclude)
        if names:
            self._search_tags(names)
        else:
            self._clear_tag_search()

    def _clear_suggestions(self) -> None:
        while self._suggest_row.count():
            item = self._suggest_row.takeAt(0)
            if item.widget() is not None:
                item.widget().deleteLater()
        self._suggest_bar.setVisible(False)

    def _show_suggestions(self, missing: list, pool) -> None:
        self._clear_suggestions()
        shown = False
        for name in missing:
            close = _suggest_genres(name, pool)
            if not close:
                continue
            hint = CaptionLabel(tr('books.manga_tag_did_you_mean', default='Did you mean, instead of "{name}":', name=name),
                                self._suggest_bar)
            hint.setStyleSheet(f'color: {_muted_text_color()}; background: transparent;')
            self._suggest_row.addWidget(hint)
            for label in close:
                pill = make_tag_pill(label, self._suggest_bar, on_click=lambda t, m=name: self._retry_tag(m, t),
                                     tip=tr('books.manga_tag_tip', default='Show manga tagged "{tag}"', tag=label))
                pill.adjustSize()
                self._suggest_row.addWidget(pill)
            shown = True
        if shown:
            self._suggest_row.addStretch(1)
        self._suggest_bar.setVisible(shown)

    def _retry_tag(self, missing: str, suggestion: str) -> None:
        def norm(t: str) -> str:
            return re.sub(r'\s+', ' ', t or '').strip().lower()
        query, replaced = [], False
        for t in self._last_tag_query:
            if norm(t.lstrip('-')) == norm(missing):
                query.append(('-' if t.startswith('-') else '') + suggestion)
                replaced = True
            else:
                query.append(t)
        if not replaced:
            query.append(suggestion)
        self._search_tags(query)

    def _set_status(self, text: str) -> None:
        self._status_lbl.setText(text)
        self._status_lbl.setVisible(bool(text))

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
            self._gen += 1
            self._page = 1
            self._has_more = True
            self._grid.clear()
            self._set_status(tr('books.loading', default='Loading…'))
        self._loading = True
        gen, page = self._gen, self._page
        self._task_id = submit(
            manga_manager.browse, args=(self._filters, page),
            kwargs={'use_cache': use_cache},
            on_done=lambda result: self._on_page_loaded(gen, page, result),
            on_error=lambda message: self._on_page_error(gen, page, message),
        )

    def _on_page_loaded(self, gen: int, page: int, result) -> None:
        if gen != self._gen:
            return
        self._task_id = None
        self._loading = False
        self._has_more = result.has_more
        self._page = page + 1
        entries = result.entries
        if self._tag_excl:
            out = {t.lower() for t in self._tag_excl}
            entries = [e for e in entries if not out.intersection(t.strip().lower() for t in e.tags)]
            if result.entries and not entries and self._has_more:
                QTimer.singleShot(0, self._on_near_bottom)
        self._grid.add_entries(entries)
        self._set_status(tr('books.manga_no_results', default='No manga found') if page == 1 and not entries and not self._has_more else '')

    def _on_page_error(self, gen: int, page: int, message: str) -> None:
        if gen != self._gen:
            return
        logger.warning('MangaDex page load failed: %s', message)
        self._task_id = None
        self._loading = False
        self._has_more = False
        if page == 1:
            self._set_status(tr('books.manga_load_failed_hint', default='Could not reach MangaDex — press refresh to retry.'))
        self._show_error_bar(message)

    def _show_error_bar(self, message: str) -> None:
        InfoBar.error(
            title=tr('books.manga_load_failed_title'),
            content=str(message),
            orient=Qt.Orientation.Horizontal,
            isClosable=True,
            position=InfoBarPosition.TOP_RIGHT,
            duration=4000,
            parent=self.window(),
        )


    def _on_random(self) -> None:
        self._random_token += 1
        token = self._random_token
        if self._random_ticket is not None:
            cancel(self._random_ticket)
            self._random_ticket = None
        self._random_btn.set_busy(True)

        def done(entry) -> None:
            if token != self._random_token:
                return
            self._random_ticket = None
            self._random_btn.set_busy(False)
            if entry is None:
                self._random_notice(tr('repacks.random_empty_title', default='Nothing to pick from'),
                                    tr('books.manga_random_empty', default='MangaDex has no manga to pick from here.'))
                return
            self._last_random = entry.key
            self._show_details(entry)

        def fail(message: str) -> None:
            if token != self._random_token:
                return
            logger.warning('MangaDex random failed: %s', message)
            self._random_ticket = None
            self._random_btn.set_busy(False)
            self._random_notice(tr('books.manga_random_failed_title', default="Couldn't pick a manga"),
                                tr('repacks.random_failed', default='Nothing came back this time. Try again in a moment.'))

        self._random_ticket = submit(manga_manager.random_manga, args=(self._filters, self._last_random),
                                     on_done=done, on_error=fail)

    def cancel_random(self) -> None:
        self._random_token += 1
        if self._random_ticket is not None:
            cancel(self._random_ticket)
            self._random_ticket = None
        self._random_btn.set_busy(False)

    def _random_notice(self, title: str, content: str) -> None:
        InfoBar.warning(
            title=title,
            content=content,
            orient=Qt.Orientation.Horizontal,
            isClosable=True,
            position=InfoBarPosition.TOP_RIGHT,
            duration=3000,
            parent=self.window(),
        )

    def _show_details(self, entry: MangaEntry) -> None:
        if self._details_task_id is not None:
            cancel(self._details_task_id)
            self._details_task_id = None
        language = self._filters.language
        key = f'{entry.key}:{language}'
        self._current_details_key = key
        self._details.show_loading(entry)
        self._switch_stack_animated(self._details)

        cached = self._details_cache.get(key)
        if cached is not None:
            self._details_cache.move_to_end(key)
            self._details.show_details(cached, language)
            return

        self._details_task_id = submit(
            manga_manager.details_for_entry, args=(entry,), kwargs={'language': language},
            on_done=lambda details: self._on_details_loaded(key, language, details),
            on_error=lambda error: self._on_details_error(key, error),
        )

    def _on_details_loaded(self, key: str, language: str, details) -> None:
        if key != self._current_details_key:
            return
        self._details_task_id = None
        if details is None:
            self._details.show_error('not found')
            return
        self._details_cache[key] = details
        self._details_cache.move_to_end(key)
        while len(self._details_cache) > 25:
            self._details_cache.popitem(last=False)
        self._details.show_details(details, language)

    def _on_details_error(self, key: str, message: str) -> None:
        if key != self._current_details_key:
            return
        self._details_task_id = None
        logger.warning('Manga details load failed: %s', message)
        self._details.show_error(message)

    def _show_grid(self) -> None:
        if self._details_task_id is not None:
            cancel(self._details_task_id)
            self._details_task_id = None
        self._current_details_key = None
        self._switch_stack_animated(self._grid)

    def shutdown(self) -> None:
        self._search_timer.stop()
        self.cancel_random()
        if self._tag_ticket is not None:
            cancel(self._tag_ticket)
        if self._task_id is not None:
            cancel(self._task_id)
        if self._details_task_id is not None:
            cancel(self._details_task_id)
        self._details.shutdown()
        self._grid.clear()


class ComingSoonTab(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setStyleSheet('background: transparent;')
        outer = QVBoxLayout(self)
        outer.setContentsMargins(24, 24, 24, 24)
        outer.addStretch(1)
        row = QHBoxLayout()
        row.addStretch(1)
        self._card = QWidget(self)
        self._card.setObjectName('comingSoonCard')
        self._card.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self._card.setMaximumWidth(460)
        col = QVBoxLayout(self._card)
        col.setContentsMargins(44, 40, 44, 40)
        col.setSpacing(12)
        self._icon = QLabel(self._card)
        self._icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        col.addWidget(self._icon)
        self._title = TitleLabel(tr('books.coming_soon_title', default='Coming soon'), self._card)
        self._title.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        col.addWidget(self._title)
        self._body = BodyLabel(tr(
            'books.coming_soon_content',
            default='Book downloads are taking a short break. Manga is still here, and books will return with a better library.',
        ), self._card)
        self._body.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        self._body.setWordWrap(True)
        col.addWidget(self._body)
        self._badge = CaptionLabel(tr('books.coming_soon_badge', default='IN THE WORKS'), self._card)
        self._badge.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        col.addWidget(self._badge)
        row.addWidget(self._card, 0)
        row.addStretch(1)
        outer.addLayout(row)
        outer.addStretch(1)
        register_theme_refresh(self, self._apply_theme)
        self._apply_theme()

    def _apply_theme(self) -> None:
        self._icon.setPixmap(FluentIcon.BOOK_SHELF.icon(color=QColor(_body_text_color())).pixmap(56, 56))
        self._card.setStyleSheet(
            f"#comingSoonCard {{ background: {_surface_tint_color(12)}; "
            f"border: 1px solid {_surface_tint_color(7)}; border-radius: 20px; }}"
        )


class BooksPage(QWidget):
    def __init__(self, download_manager, parent=None, manga_download_bridge=None):
        super().__init__(parent)
        self.setObjectName('booksPage')
        self.setStyleSheet('background: transparent;')
        outer = QVBoxLayout(self)
        outer.setContentsMargins(20, 12, 20, 0)
        outer.setSpacing(14)

        self._pivot = Pivot()
        outer.addWidget(self._pivot)

        self._section_stack = QStackedWidget()
        outer.addWidget(self._section_stack, 1)

        self._tabs: dict[str, QWidget] = {}
        self._tab_display_names: dict[str, str] = {}

        self._books_tab = ComingSoonTab(self)
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
        if 'books' in self._tabs and 'books' not in enabled:
            enabled.insert(0, 'books')
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
        manga_tab = getattr(self, '_manga_tab', None)
        if manga_tab is not None and widget is not manga_tab:
            manga_tab.cancel_random()
        if widget is not None and widget is not self._section_stack.currentWidget():
            self._section_stack.setCurrentWidget(widget)
            fade_in(widget, 130)
            self._current_tab_key = key

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
