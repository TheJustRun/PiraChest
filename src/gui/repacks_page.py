from __future__ import annotations
import logging
import re
from PyQt6.QtCore import Qt, QSize, QRect, QThread, pyqtSignal, pyqtProperty, QPropertyAnimation, QEasingCurve
from PyQt6.QtGui import QColor
from PyQt6.QtWidgets import QWidget, QVBoxLayout, QHBoxLayout, QLayout, QLayoutItem, QScrollArea, QSizePolicy, QDialog, QTreeWidgetItem, QHeaderView
from qfluentwidgets import CardWidget, FluentIcon, Pivot, TitleLabel, SubtitleLabel, BodyLabel, StrongBodyLabel, CaptionLabel, TransparentToolButton, PushButton, PrimaryPushButton, ImageLabel, PillPushButton, FlowLayout as QFlowLayout, qrouter, themeColor, SearchLineEdit, TreeWidget, MessageBoxBase, SmoothScrollArea
from ..core.repacks.base import RepackEntry, RepackDetails
from ..core.repacks.worker import fetch_page_async, fetch_details_async, fetch_upcoming_repacks_async
from ..core.repacks.poster_downloader import PosterDownloader
from ..core.download_manager import DownloadManager, DLState

_SOURCE_DONATION_URLS = {
    'fitgirl': 'https://fitgirl-repacks.site/donations/',
}
_SOURCE_UPCOMING_SUPPORTED = {'fitgirl'}
logger = logging.getLogger(__name__)

class FlowLayout(QLayout):

    def __init__(self, parent=None, margin: int=0, spacing: int=12):
        super().__init__(parent)
        self._items: list[QLayoutItem] = []
        self.setContentsMargins(margin, margin, margin, margin)
        self.setSpacing(spacing)

    def addItem(self, item: QLayoutItem) -> None:
        self._items.append(item)

    def count(self) -> int:
        return len(self._items)

    def itemAt(self, index: int):
        if 0 <= index < len(self._items):
            return self._items[index]
        return None

    def takeAt(self, index: int):
        if 0 <= index < len(self._items):
            return self._items.pop(index)
        return None

    def expandingDirections(self):
        return Qt.Orientation(0)

    def hasHeightForWidth(self) -> bool:
        return True

    def heightForWidth(self, width: int) -> int:
        return self._do_layout(QRect(0, 0, width, 0), test_only=True)

    def setGeometry(self, rect: QRect) -> None:
        super().setGeometry(rect)
        self._do_layout(rect, test_only=False)

    def sizeHint(self) -> QSize:
        return self.minimumSize()

    def minimumSize(self) -> QSize:
        size = QSize()
        for item in self._items:
            size = size.expandedTo(item.minimumSize())
        margins = self.contentsMargins()
        size += QSize(margins.left() + margins.right(), margins.top() + margins.bottom())
        return size

    def _do_layout(self, rect: QRect, test_only: bool) -> int:
        left, top, right, bottom = self.getContentsMargins()
        effective_rect = rect.adjusted(left, top, -right, -bottom)
        x = effective_rect.x()
        y = effective_rect.y()
        line_height = 0
        spacing = self.spacing()
        for item in self._items:
            widget = item.widget()
            if widget is not None and (not widget.isVisible()):
                continue
            item_width = item.sizeHint().width()
            item_height = item.sizeHint().height()
            next_x = x + item_width + spacing
            if next_x - spacing > effective_rect.right() and line_height > 0:
                x = effective_rect.x()
                y = y + line_height + spacing
                next_x = x + item_width + spacing
                line_height = 0
            if not test_only:
                item.setGeometry(QRect(x, y, item_width, item_height))
            x = next_x
            line_height = max(line_height, item_height)
        return y + line_height - rect.y() + bottom

def _stop_previous_movie(image_label) -> None:
    try:
        movie = image_label.movie()
    except Exception:
        movie = None
    if movie is not None:
        movie.stop()
        movie.deleteLater()
        from PyQt6.QtWidgets import QLabel
        QLabel.setMovie(image_label, None)

class PosterCard(CardWidget):
    clicked_poster = pyqtSignal(object)
    POSTER_WIDTH = 150
    POSTER_HEIGHT = 210

    def __init__(self, entry: RepackEntry, parent=None):
        super().__init__(parent)
        self._entry = entry
        self.setFixedSize(self.POSTER_WIDTH, self.POSTER_HEIGHT)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setToolTip(entry.title)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        self._poster_lbl = ImageLabel()
        self._poster_lbl.setBorderRadius(6, 6, 6, 6)
        self._poster_lbl.setFixedSize(self.POSTER_WIDTH, self.POSTER_HEIGHT)
        if entry.poster_path:
            self.set_poster_path(entry.poster_path)
        layout.addWidget(self._poster_lbl)

    @property
    def entry(self) -> RepackEntry:
        return self._entry

    @property
    def poster_url(self) -> str | None:
        return self._entry.poster_url

    def set_poster_path(self, path: str) -> None:
        try:
            _stop_previous_movie(self._poster_lbl)
            self._poster_lbl.setImage(path)
            self._poster_lbl.scaledToWidth(self.POSTER_WIDTH)
            self._entry.poster_path = path
        except Exception:
            logger.warning('Failed to load poster image: %s', path)

    def unload_pixmap(self) -> None:
        if self._entry.poster_path:
            _stop_previous_movie(self._poster_lbl)
            self._poster_lbl.setImage(None)
            self._poster_lbl.setFixedSize(self.POSTER_WIDTH, self.POSTER_HEIGHT)

    def reload_pixmap_if_needed(self) -> None:
        if self._entry.poster_path and self._poster_lbl.isNull():
            self.set_poster_path(self._entry.poster_path)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked_poster.emit(self._entry)
        super().mousePressEvent(event)

class PosterGrid(SmoothScrollArea):
    poster_clicked = pyqtSignal(object)
    near_bottom = pyqtSignal()
    _NEAR_BOTTOM_THRESHOLD_PX = 400
    _VISIBILITY_MARGIN_PX = 600
    _VISIBILITY_DEBOUNCE_MS = 150

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWidgetResizable(True)
        self.setFrameShape(QScrollArea.Shape.NoFrame)
        self.setStyleSheet('background: transparent; border: none;')
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._container = QWidget()
        self._container.setStyleSheet('background: transparent;')
        self._flow_layout = FlowLayout(self._container, margin=4, spacing=14)
        self.setWidget(self._container)
        self._entries: list[RepackEntry] = []
        self._cards_by_url: dict[str, PosterCard] = {}
        self._poster_downloader = PosterDownloader(self)
        self._poster_downloader.poster_ready.connect(self._on_poster_ready)
        from PyQt6.QtCore import QTimer
        self._visibility_timer = QTimer(self)
        self._visibility_timer.setSingleShot(True)
        self._visibility_timer.setInterval(self._VISIBILITY_DEBOUNCE_MS)
        self._visibility_timer.timeout.connect(self._update_offscreen_cards)
        self.verticalScrollBar().valueChanged.connect(self._check_near_bottom)
        self.verticalScrollBar().valueChanged.connect(self._on_scroll_changed)
        self._card_states: dict[str, bool] = {}

    def clear(self) -> None:
        self._entries = []
        self._cards_by_url = {}
        while self._flow_layout.count():
            item = self._flow_layout.takeAt(0)
            if item is not None and item.widget() is not None:
                item.widget().deleteLater()

    def set_entries(self, entries: list[RepackEntry]) -> None:
        self.clear()
        self.append_entries(entries)

    def append_entries(self, entries: list[RepackEntry]) -> None:
        for entry in entries:
            if entry.url in self._cards_by_url:
                continue
            self._entries.append(entry)
            card = PosterCard(entry, self._container)
            card.clicked_poster.connect(self.poster_clicked.emit)
            self._flow_layout.addWidget(card)
            self._cards_by_url[entry.url] = card
            if entry.poster_url and (not entry.poster_path):
                self._poster_downloader.request(entry.poster_url)
        self._visibility_timer.start()

    def _on_poster_ready(self, url: str, path: str) -> None:
        for card in self._cards_by_url.values():
            if card.poster_url == url:
                card.set_poster_path(path)

    def _check_near_bottom(self, value: int) -> None:
        bar = self.verticalScrollBar()
        if bar.maximum() - value <= self._NEAR_BOTTOM_THRESHOLD_PX:
            self.near_bottom.emit()

    def _on_scroll_changed(self, value: int) -> None:
        if not self._visibility_timer.isActive():
            self._visibility_timer.start(self._VISIBILITY_DEBOUNCE_MS)

    def _update_offscreen_cards(self) -> None:
        viewport_top = self.verticalScrollBar().value() - self._VISIBILITY_MARGIN_PX
        viewport_bottom = viewport_top + self.viewport().height() + self._VISIBILITY_MARGIN_PX
        for url, card in self._cards_by_url.items():
            if not card.isVisible():
                continue
            card_top = card.y()
            card_bottom = card_top + card.height()
            in_range = card_bottom >= viewport_top and card_top <= viewport_bottom
            was_in_range = self._card_states.get(url)
            if in_range == was_in_range:
                continue
            self._card_states[url] = in_range
            if in_range:
                card.reload_pixmap_if_needed()
            else:
                card.unload_pixmap()

    def filter_by_title(self, query: str) -> None:
        query = query.strip().lower()
        for entry in self._entries:
            card = self._cards_by_url.get(entry.url)
            if card is None:
                continue
            card.setVisible(not query or query in entry.title.lower())
        self._flow_layout.update()
        self._visibility_timer.start()

class MetaField(QWidget):

    def __init__(self, label: str, value: str, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(2)
        label_lbl = CaptionLabel(label.upper())
        label_lbl.setTextColor(QColor(255, 255, 255, 130), QColor(0, 0, 0, 130))
        label_lbl.setStyleSheet('font-weight: 400;')
        layout.addWidget(label_lbl)
        value_lbl = StrongBodyLabel(value)
        value_lbl.setWordWrap(True)
        value_lbl.setStyleSheet('font-weight: 700;')
        layout.addWidget(value_lbl)

def make_tag_pill(text: str, parent=None) -> PillPushButton:
    pill = PillPushButton(text, parent)
    pill.setChecked(True)
    pill.setCheckable(False)
    pill.setCursor(Qt.CursorShape.ArrowCursor)
    return pill

class _RotatingChevron(QWidget):

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedSize(16, 16)
        self._angle = 0.0
        self._color = QColor(255, 255, 255, 235)

    def get_angle(self) -> float:
        return self._angle

    def set_angle(self, value: float) -> None:
        self._angle = value
        self.update()
    angle = pyqtProperty(float, get_angle, set_angle)

    def set_color(self, color: QColor) -> None:
        self._color = color
        self.update()

    def paintEvent(self, event):
        from PyQt6.QtGui import QPainter, QPen, QPainterPath
        from PyQt6.QtCore import QPointF
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        pen = QPen(self._color)
        pen.setWidthF(1.6)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        painter.setPen(pen)
        cx, cy = (self.width() / 2, self.height() / 2)
        painter.translate(cx, cy)
        painter.rotate(self._angle)
        painter.translate(-cx, -cy)
        path = QPainterPath()
        path.moveTo(QPointF(cx - 4, cy - 2.5))
        path.lineTo(QPointF(cx, cy + 2.5))
        path.lineTo(QPointF(cx + 4, cy - 2.5))
        painter.drawPath(path)
        painter.end()

class CollapsibleSection(CardWidget):
    _ANIM_MS = 160
    toggled = pyqtSignal(bool)

    def __init__(self, title: str, parent=None, expanded: bool=False):
        super().__init__(parent)
        self.setBorderRadius(12)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        self._header = QWidget()
        self._header.setCursor(Qt.CursorShape.PointingHandCursor)
        self._header.setFixedHeight(50)
        self._header.setObjectName('sectionHeader')
        header_layout = QHBoxLayout(self._header)
        header_layout.setContentsMargins(16, 0, 14, 0)
        header_layout.setSpacing(10)
        self._title_lbl = StrongBodyLabel(title)
        self._title_lbl.setTextColor(QColor(255, 255, 255, 235), QColor(255, 255, 255, 235))
        self._title_lbl.setStyleSheet('font-size: 14px; font-weight: 600; background: transparent;')
        header_layout.addWidget(self._title_lbl, 1)
        self._chevron = _RotatingChevron(self._header)
        header_layout.addWidget(self._chevron, 0, Qt.AlignmentFlag.AlignRight)
        outer.addWidget(self._header)
        self._body_wrap = QWidget()
        wrap_layout = QVBoxLayout(self._body_wrap)
        wrap_layout.setContentsMargins(0, 0, 0, 0)
        wrap_layout.setSpacing(0)
        self._body = QWidget()
        body_layout = QVBoxLayout(self._body)
        body_layout.setContentsMargins(16, 4, 16, 18)
        self._body_lbl = BodyLabel('')
        self._body_lbl.setWordWrap(True)
        self._body_lbl.setTextFormat(Qt.TextFormat.RichText)
        self._body_lbl.setOpenExternalLinks(True)
        self._body_lbl.setStyleSheet(f'font-size: 14px; font-weight: 500; line-height: {_DESC_LINE_HEIGHT_PCT}%; color: rgba(255, 255, 255, 225); background: transparent;')
        body_layout.addWidget(self._body_lbl)
        wrap_layout.addWidget(self._body)
        outer.addWidget(self._body_wrap)
        self._header.mousePressEvent = lambda _e: self.toggle()
        self._height_anim = QPropertyAnimation(self._body_wrap, b'maximumHeight')
        self._height_anim.setDuration(self._ANIM_MS)
        self._height_anim.setEasingCurve(QEasingCurve.Type.OutQuad)
        self._chevron_anim = QPropertyAnimation(self._chevron, b'angle')
        self._chevron_anim.setDuration(self._ANIM_MS)
        self._chevron_anim.setEasingCurve(QEasingCurve.Type.OutQuad)
        self._hovering = False
        self._cached_body_height = 0
        self._expanded = expanded
        self._body_wrap.setMaximumHeight(16777215 if self._expanded else 0)
        self._chevron.set_angle(180.0 if self._expanded else 0.0)
        self._update_header_style()

    def enterEvent(self, event):
        self._hovering = True
        self._update_header_style()
        super().enterEvent(event)

    def leaveEvent(self, event):
        self._hovering = False
        self._update_header_style()
        super().leaveEvent(event)

    def set_body_text(self, text: str) -> None:
        lines = [ln.strip() for ln in (text or '').split('\n') if ln.strip()]
        looks_like_features = bool(lines) and all((ln.startswith(('•', '- ', '* ')) for ln in lines))
        if looks_like_features:
            html = render_features_html(text)
        else:
            html = render_description_html(text)
        self._body_lbl.setText(html)
        self._cached_body_height = self._body_wrap.sizeHint().height()
        if self._expanded:
            self._body_wrap.setMaximumHeight(self._cached_body_height)

    def toggle(self) -> None:
        self._expanded = not self._expanded
        self.toggled.emit(self._expanded)
        if self._expanded:
            self._cached_body_height = self._body_wrap.sizeHint().height()
            target_height = self._cached_body_height
        else:
            target_height = 0
        target_angle = 180.0 if self._expanded else 0.0
        try:
            self._height_anim.finished.disconnect(self._on_anim_finished)
        except TypeError:
            pass
        start_height = self._body_wrap.maximumHeight()
        if start_height > 16000000:
            start_height = self._cached_body_height
        self._height_anim.stop()
        self._height_anim.setStartValue(start_height)
        self._height_anim.setEndValue(target_height)
        self._height_anim.finished.connect(self._on_anim_finished)
        self._height_anim.start()
        self._chevron_anim.stop()
        self._chevron_anim.setStartValue(self._chevron.get_angle())
        self._chevron_anim.setEndValue(target_angle)
        self._chevron_anim.start()
        self._update_header_style()

    def _on_anim_finished(self) -> None:
        if self._expanded:
            self._body_wrap.setMaximumHeight(16777215)

    def _update_header_style(self) -> None:
        self._title_lbl.setTextColor(QColor(255, 255, 255, 235), QColor(255, 255, 255, 235))
        if self._hovering:
            self._header.setStyleSheet('#sectionHeader { background: rgba(255, 255, 255, 16); border-top-left-radius: 12px; border-top-right-radius: 12px; }')
        else:
            self._header.setStyleSheet('#sectionHeader { background: transparent; }')

    @property
    def is_expanded(self) -> bool:
        return self._expanded
_URL_RE = re.compile('(https?://[^\\s<>\\"]+)')

def _escape_html(text: str) -> str:
    return text.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')

def _linkify_inline(text: str) -> str:

    def _wrap(match: 're.Match[str]') -> str:
        url = match.group(1)
        return f'<a href="{url}" style="color:{themeColor().name()};">{url}</a>'
    return _URL_RE.sub(_wrap, text)

def _linkify(text: str) -> str:
    if not text:
        return text
    escaped = _linkify_inline(_escape_html(text))
    paragraphs = escaped.split('\n\n')
    paragraphs = [p.replace('\n', '<br>') for p in paragraphs]
    return '<br><br>'.join(paragraphs)
_DESC_PARAGRAPH_MARGIN_PX = 22
_DESC_LIST_ITEM_SPACING_PX = 8
_DESC_LINE_HEIGHT_PCT = 170

def render_description_html(text: str) -> str:
    if not text:
        return ''
    blocks = re.split('\\n\\s*\\n', text.strip())
    html_parts: list[str] = []

    def _flush_prose(prose_lines: list[str]) -> None:
        if not prose_lines:
            return
        joined = '\n'.join(prose_lines).strip()
        if not joined:
            return
        if joined.startswith('## '):
            heading_text = _linkify_inline(_escape_html(joined[3:].strip()))
            html_parts.append(f'<p style="margin:0 0 {_DESC_PARAGRAPH_MARGIN_PX}px 0; font-weight:600; font-size:16px;">{heading_text}</p>')
            return
        para_text = _linkify_inline(_escape_html(joined)).replace('\n', '<br>')
        html_parts.append(f'<p style="margin:0 0 {_DESC_PARAGRAPH_MARGIN_PX}px 0;">{para_text}</p>')

    def _flush_list(bullet_lines: list[str]) -> None:
        if not bullet_lines:
            return
        items = []
        for ln in bullet_lines:
            item_text = re.sub('^[•*-]\\s*', '', ln.strip())
            if not item_text:
                continue
            title, rest = _split_feature_title(item_text)
            title_html = _linkify_inline(_escape_html(title))
            if rest:
                rest_html = _linkify_inline(_escape_html(rest))
                content = f'<span style="font-weight:700;">{title_html}.</span> <span style="font-weight:500;">{rest_html}</span>'
            else:
                content = _linkify_inline(_escape_html(item_text))
            items.append(f'<li style="margin-bottom:{_DESC_LIST_ITEM_SPACING_PX}px;">{content}</li>')
        if items:
            html_parts.append(f'<ul style="margin:0 0 {_DESC_PARAGRAPH_MARGIN_PX}px 0; padding-left:22px;">' + ''.join(items) + '</ul>')
    for block in blocks:
        block = block.strip('\n')
        if not block.strip():
            continue
        pending_prose: list[str] = []
        pending_bullets: list[str] = []
        for raw_line in block.split('\n'):
            line = raw_line.strip()
            if not line:
                continue
            if line.startswith(('•', '- ', '* ')):
                if pending_prose:
                    _flush_prose(pending_prose)
                    pending_prose = []
                pending_bullets.append(line)
            else:
                if pending_bullets:
                    _flush_list(pending_bullets)
                    pending_bullets = []
                pending_prose.append(line)
        if pending_bullets:
            _flush_list(pending_bullets)
        if pending_prose:
            _flush_prose(pending_prose)
    if html_parts:
        last = html_parts[-1]
        html_parts[-1] = re.sub('margin:0 0 \\d+px 0', 'margin:0', last, count=1)
    return ''.join(html_parts)
_FEATURE_TITLE_SPLIT_RE = re.compile('(\\.{3}|…|(?<!\\.)\\.(?!\\.))\\s*')

def _split_feature_title(item_text: str) -> tuple[str, str]:
    match = _FEATURE_TITLE_SPLIT_RE.search(item_text)
    if not match:
        return (item_text.strip(), '')
    boundary_end = match.end()
    title = item_text[:match.start()].strip()
    rest = item_text[boundary_end:].strip()
    if not title or not rest:
        return (item_text.strip(), '')
    return (title, rest)
_FEATURE_BLOCK_SPACING_PX = 18

def render_features_html(text: str) -> str:
    if not text:
        return ''
    raw_lines = [ln.strip() for ln in text.strip().split('\n') if ln.strip()]
    items_html: list[str] = []
    for raw_line in raw_lines:
        item_text = re.sub('^[•*-]\\s*', '', raw_line).strip()
        if not item_text:
            continue
        title, rest = _split_feature_title(item_text)
        if rest:
            title_html = _linkify_inline(_escape_html(title))
            rest_html = _linkify_inline(_escape_html(rest))
            content = f'<span style="font-weight:700;">{title_html}.</span> <span style="font-weight:500;">{rest_html}</span>'
        else:
            content = f'<span style="font-weight:500;">{_linkify_inline(_escape_html(item_text))}</span>'
        items_html.append(f'<li style="margin-bottom:{_FEATURE_BLOCK_SPACING_PX}px;">{content}</li>')
    if not items_html:
        return ''
    items_html[-1] = items_html[-1].replace(f'margin-bottom:{_FEATURE_BLOCK_SPACING_PX}px;', 'margin-bottom:0px;', 1)
    return '<ul style="margin:0; padding-left:20px;">' + ''.join(items_html) + '</ul>'
_BACKWARDS_COMPAT_RE = re.compile('(?im)^\\s*This repack (?:IS|is not|IS NOT|is)\\b.*backwards compatible.*$')

def _extract_trailing_backwards_compat(intro_text: str) -> tuple[str, str]:
    if not intro_text:
        return (intro_text, '')
    lines = intro_text.split('\n')
    note_lines: list[str] = []
    while lines and (not lines[-1].strip() or _BACKWARDS_COMPAT_RE.match(lines[-1].strip())):
        line = lines.pop()
        stripped = line.strip()
        if stripped:
            note_lines.insert(0, stripped)
    if not note_lines:
        return (intro_text, '')
    cleaned = '\n'.join(lines).rstrip()
    note = ' '.join(note_lines).strip()
    return (cleaned, note)

def _split_description_sections(description: str) -> list[tuple[str, str]]:
    if not description:
        return []
    blocks = re.split('\\n\\n(?=## )', description)
    sections: list[tuple[str, str]] = []
    for block in blocks:
        block = block.strip()
        if not block:
            continue
        if block.startswith('## '):
            newline_idx = block.find('\n')
            if newline_idx == -1:
                heading, body = (block[3:].strip(), '')
            else:
                heading, body = (block[3:newline_idx].strip(), block[newline_idx + 1:].strip())
        else:
            heading, body = ('', block)
        sections.append((heading, body))
    return sections

_BONUS_FILE_PREFIX = 'fg-optional'

class SelectiveDownloadEntry:
    __slots__ = ('label', 'category', 'patterns', 'required', 'size_hint', 'size_bytes', 'file_index')

    def __init__(self, label: str, category: str, patterns: list[str], required: bool=False, size_hint: str='', size_bytes: int=0, file_index: int=0):
        self.label = label
        self.category = category
        self.patterns = patterns
        self.required = required
        self.size_hint = size_hint
        self.size_bytes = size_bytes
        self.file_index = file_index

def _human_size(num_bytes: int) -> str:
    size = float(num_bytes)
    for unit in ('B', 'KB', 'MB', 'GB', 'TB'):
        if size < 1024.0:
            return f'{size:.1f} {unit}' if unit != 'B' else f'{int(size)} {unit}'
        size /= 1024.0
    return f'{size:.1f} PB'

def build_selective_entries_from_torrent(torrent_files: list[str], file_sizes: list[int] | None = None) -> list[SelectiveDownloadEntry]:
    entries: list[SelectiveDownloadEntry] = []
    if not torrent_files:
        return entries

    for i, path in enumerate(torrent_files):
        display_name = path.replace('\\', '/').split('/')[-1] or path
        size_bytes = file_sizes[i] if file_sizes and i < len(file_sizes) else 0
        size_hint = _human_size(size_bytes)
        is_bonus = display_name.lower().startswith(_BONUS_FILE_PREFIX)
        required = not is_bonus
        category = 'Bonus Content' if is_bonus else 'Required'
        entries.append(SelectiveDownloadEntry(
            label=display_name,
            category=category,
            patterns=[path],
            required=required,
            size_hint=size_hint,
            size_bytes=size_bytes,
            file_index=i + 1,
        ))

    return entries

def resolve_selective_file_indices(entries: list[SelectiveDownloadEntry], selected_labels: set[str], torrent_files: list[str]) -> list[int]:
    wanted = [e for e in entries if e.label in selected_labels]
    indices: set[int] = set()
    for entry in wanted:
        if entry.file_index:
            indices.add(entry.file_index)
    return sorted(indices)

class SelectiveDownloadDialog(MessageBoxBase):
    _ORDERED_CATEGORIES = ('Required', 'Bonus Content')

    def __init__(self, entries: list[SelectiveDownloadEntry], parent=None):
        super().__init__(parent)
        self._entries = entries
        self._checkboxes: dict[str, QTreeWidgetItem] = {}

        self.titleLabel = SubtitleLabel('Choose Files to Download', self)
        self.viewLayout.addWidget(self.titleLabel)

        info = CaptionLabel('Required files are always included. Select any optional files you also want.', self)
        info.setWordWrap(True)
        info.setStyleSheet('color: rgba(255, 255, 255, 130);')
        self.viewLayout.addWidget(info)

        self._tree = TreeWidget(self)
        self._tree.setHeaderLabels(['Include', 'Size'])
        self._tree.header().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self._tree.setRootIsDecorated(True)
        self._tree.setBorderVisible(True)
        self._tree.setBorderRadius(8)
        self._tree.setMinimumHeight(420)
        self.viewLayout.addWidget(self._tree)

        by_category: dict[str, list[SelectiveDownloadEntry]] = {}
        for e in entries:
            by_category.setdefault(e.category, []).append(e)

        for cat in self._ORDERED_CATEGORIES:
            if cat not in by_category:
                continue
            cat_item = QTreeWidgetItem([cat, ''])
            cat_item.setFlags(cat_item.flags() & ~Qt.ItemFlag.ItemIsUserCheckable)
            self._tree.addTopLevelItem(cat_item)
            font = self._tree.font()
            font.setBold(True)
            cat_item.setFont(0, font)
            for entry in by_category[cat]:
                child = QTreeWidgetItem([entry.label, entry.size_hint])
                child.setFlags(child.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                child.setCheckState(0, Qt.CheckState.Checked if entry.required else Qt.CheckState.Unchecked)
                cat_item.addChild(child)
                self._checkboxes[entry.label] = child
            cat_item.setExpanded(True)

        self._size_by_label: dict[str, int] = {e.label: e.size_bytes for e in entries}
        self._total_lbl = StrongBodyLabel('', self)
        self.viewLayout.addWidget(self._total_lbl)
        self._tree.itemChanged.connect(self._update_total)
        self._update_total()

        self.yesButton.setText('OK')
        self.cancelButton.setText('Cancel')
        self.widget.setMinimumWidth(560)

    def _update_total(self, *_args) -> None:
        total = 0
        for label, item in self._checkboxes.items():
            if item.checkState(0) == Qt.CheckState.Checked:
                total += self._size_by_label.get(label, 0)
        self._total_lbl.setText(f'Total download size: {_human_size(total)}')

    def selected_labels(self) -> set[str]:
        selected = set()
        for label, item in self._checkboxes.items():
            if item.checkState(0) == Qt.CheckState.Checked:
                selected.add(label)
        return selected

class _FileListFetchThread(QThread):
    finished_ok = pyqtSignal(list)
    finished_err = pyqtSignal(str)

    def __init__(self, manager: DownloadManager, source: str, parent=None):
        super().__init__(parent)
        self._manager = manager
        self._source = source

    def run(self) -> None:
        try:
            files = self._manager.fetch_file_list(self._source)
        except Exception as exc:
            self.finished_err.emit(str(exc))
            return
        self.finished_ok.emit(files)

class DownloadActionWidget(QWidget):

    def __init__(self, manager: DownloadManager, parent=None):
        super().__init__(parent)
        self._manager = manager
        self._details: RepackDetails | None = None
        self._item_id: str | None = None
        self._filelist_thread: _FileListFetchThread | None = None
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)
        self._button = PrimaryPushButton('DOWNLOAD')
        self._button.setFixedHeight(32)
        self._button.clicked.connect(self._on_click)
        layout.addWidget(self._button)
        self._status_lbl = CaptionLabel('')
        self._status_lbl.setWordWrap(True)
        self._status_lbl.setVisible(False)
        layout.addWidget(self._status_lbl)
        self._manager.item_updated.connect(self._on_item_updated)
        self._manager.item_removed.connect(self._on_item_removed)

    def set_details(self, details: RepackDetails) -> None:
        self._details = details
        self._item_id = None
        self._button.setText('DOWNLOAD')
        self._button.setEnabled(True)
        self._status_lbl.setVisible(False)
        self._status_lbl.setText('')

    def _on_click(self) -> None:
        if self._details is None or self._item_id is not None:
            return
        magnet_url = self._details.extra.get('magnet_url')
        torrent_url = self._details.extra.get('torrent_url')
        source = magnet_url or torrent_url
        if not source:
            self._button.setText('NO LINK FOUND')
            self._button.setEnabled(False)
            return
        self._start_selective_download(source)

    def _start_selective_download(self, source: str) -> None:
        self._button.setText('LOADING FILE LIST...')
        self._button.setEnabled(False)
        self._filelist_thread = _FileListFetchThread(self._manager, source, self)
        self._filelist_thread.finished_ok.connect(lambda files: self._on_file_list_ready(source, files))
        self._filelist_thread.finished_err.connect(lambda err: self._on_file_list_failed(source, err))
        self._filelist_thread.start()

    def _on_file_list_failed(self, source: str, err: str) -> None:
        logger.error('Failed to fetch file list for selective download: %s', err)
        self._button.setText('DOWNLOAD')
        self._button.setEnabled(True)
        self._queue_download(source, file_ids=None)

    def _on_file_list_ready(self, source: str, torrent_files: list[tuple[str, int]]) -> None:
        self._button.setText('DOWNLOAD')
        self._button.setEnabled(True)

        if not torrent_files:
            self._queue_download(source, file_ids=None)
            return

        paths = [p for p, _size in torrent_files]
        sizes = [s for _p, s in torrent_files]
        entries = build_selective_entries_from_torrent(paths, sizes)

        dialog = SelectiveDownloadDialog(entries, parent=self.window())
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        selected_labels = dialog.selected_labels()
        file_ids = resolve_selective_file_indices(entries, selected_labels, paths)
        if not file_ids:
            logger.warning('Selective download selection produced no file indices; downloading full torrent')
            file_ids = None
        self._queue_download(source, file_ids=file_ids)

    def _queue_download(self, source: str, file_ids: list[int] | None) -> None:
        self._button.setText('QUEUING...')
        self._button.setEnabled(False)
        self._item_id = self._manager.add(torrent_file=source, file_id=file_ids[0] if file_ids else 1, file_ids=file_ids, game_name=self._details.title, console='PC', source='FitGirl')
        self._status_lbl.setVisible(True)
        self._status_lbl.setText('Queued')


    def _on_item_updated(self, item_id: str) -> None:
        if item_id != self._item_id:
            return
        item = self._manager.get(item_id)
        if item is None:
            return
        if item.state == DLState.queued:
            self._button.setText('QUEUING...')
        else:
            self._button.setText(item.state.value.upper())
        pct = int(item.progress * 100) if item.progress <= 1 else int(item.progress)
        self._status_lbl.setText(f'{pct}%  •  {item.speed_down}  •  ETA {item.eta}')

    def _on_item_removed(self, item_id: str) -> None:
        if item_id != self._item_id:
            return
        self._item_id = None
        self._button.setText('DOWNLOAD')
        self._button.setEnabled(True)
        self._status_lbl.setVisible(False)
        self._status_lbl.setText('')

class RepackDetailsView(QWidget):
    back_requested = pyqtSignal()
    COVER_WIDTH = 230
    COVER_HEIGHT = 322
    _META_LABELS = (('repack_size', 'Repack Size'), ('original_size', 'Original Size'), ('company', 'Company'), ('languages', 'Languages'))

    def __init__(self, manager: DownloadManager, parent=None):
        super().__init__(parent)
        self._download_action = DownloadActionWidget(manager)
        self._poster_downloader = PosterDownloader(self)
        self._poster_downloader.poster_ready.connect(self._on_cover_ready)
        self._pending_cover_url: str | None = None
        self._section_widgets: list[CollapsibleSection] = []
        self._meta_widgets: list[MetaField] = []
        self._tag_widgets: list[PillPushButton] = []
        self._expanded_state: dict[str, bool] = {}
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 4, 0, 0)
        outer.setSpacing(10)
        back_btn = PushButton(FluentIcon.RETURN, 'Back to grid')
        back_btn.setFixedHeight(32)
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
        self.READING_WIDTH_MAX = 860
        self.SECTIONS_WIDTH = 300
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
        self._cover_action_area = QWidget(cover_container)
        self._cover_action_area.setFixedWidth(self.COVER_WIDTH)
        self._cover_action_layout = QVBoxLayout(self._cover_action_area)
        self._cover_action_layout.setContentsMargins(0, 0, 0, 0)
        self._cover_action_layout.setSpacing(8)
        cover_col.addWidget(self._cover_action_area, 0, Qt.AlignmentFlag.AlignTop)
        cover_col.addStretch(1)
        top_row.addWidget(cover_container, 0, Qt.AlignmentFlag.AlignTop)
        info_col_container = QWidget()
        info_col_container.setMaximumWidth(self.READING_WIDTH_MAX)
        info_col = QVBoxLayout(info_col_container)
        info_col.setContentsMargins(0, 0, 0, 0)
        info_col.setSpacing(20)
        self._title_lbl = TitleLabel('')
        self._title_lbl.setWordWrap(True)
        self._title_lbl.setStyleSheet('font-weight: 700;')
        info_col.addWidget(self._title_lbl)
        from PyQt6.QtWidgets import QGridLayout
        self._meta_grid = QGridLayout()
        self._meta_grid.setHorizontalSpacing(40)
        self._meta_grid.setVerticalSpacing(10)
        self._meta_grid.setColumnStretch(0, 0)
        self._meta_grid.setColumnStretch(1, 0)
        info_col.addLayout(self._meta_grid)
        tags_header = CaptionLabel('GENRES / TAGS')
        tags_header.setTextColor(QColor(255, 255, 255, 120), QColor(0, 0, 0, 120))
        tags_header.setStyleSheet('font-weight: 400;')
        self._tags_header = tags_header
        info_col.addWidget(tags_header)
        self._tags_container = QWidget()
        self._tags_layout = QFlowLayout(self._tags_container, needAni=False, isTight=True)
        self._tags_layout.setContentsMargins(0, 0, 0, 0)
        self._tags_layout.setHorizontalSpacing(8)
        self._tags_layout.setVerticalSpacing(8)
        info_col.addWidget(self._tags_container)
        desc_header = StrongBodyLabel('Description')
        desc_header.setStyleSheet('font-size: 16px; font-weight: 600;')
        info_col.addWidget(desc_header)
        self._desc_lbl = BodyLabel('')
        self._desc_lbl.setWordWrap(True)
        self._desc_lbl.setTextFormat(Qt.TextFormat.RichText)
        self._desc_lbl.setOpenExternalLinks(True)
        self._desc_lbl.setStyleSheet(f'font-size: 18px; font-weight: 500; line-height: {_DESC_LINE_HEIGHT_PCT}%;')
        self._desc_default_qss = self._desc_lbl.styleSheet()
        info_col.addWidget(self._desc_lbl)
        info_col.addStretch(1)
        top_row.addWidget(info_col_container, 1, Qt.AlignmentFlag.AlignTop)
        sections_container = QWidget()
        sections_container.setFixedWidth(self.SECTIONS_WIDTH)
        self._sections_col = QVBoxLayout(sections_container)
        self._sections_col.setContentsMargins(0, 0, 0, 0)
        self._sections_col.setSpacing(14)
        sections_header = StrongBodyLabel('Details')
        sections_header.setStyleSheet('font-size: 16px; font-weight: 600;')
        self._sections_header = sections_header
        self._sections_col.addWidget(sections_header)
        self._sections_insert_index = self._sections_col.count()
        self._sections_header.setVisible(False)
        top_row.addWidget(sections_container, 0, Qt.AlignmentFlag.AlignTop)
        self._content_layout.addLayout(top_row)
        self._content_layout.addStretch(1)
        self.set_cover_action_widget(self._download_action)

    def set_cover_action_widget(self, widget) -> None:
        while self._cover_action_layout.count():
            item = self._cover_action_layout.takeAt(0)
            if item is not None and item.widget() is not None:
                item.widget().deleteLater()
        if widget is not None:
            self._cover_action_layout.addWidget(widget)

    def show_loading(self, entry: RepackEntry) -> None:
        self._title_lbl.setText(entry.title)
        self._title_lbl.setStyleSheet('font-weight: 700;')
        self._desc_lbl.setStyleSheet(self._desc_default_qss)
        self._desc_lbl.setText('Loading details…')
        self._clear_sections()
        self._clear_meta()
        self._clear_tags()
        self._pending_cover_url = entry.poster_url
        if entry.poster_path:
            self._set_cover_path(entry.poster_path)
        elif entry.poster_url:
            self._poster_downloader.request(entry.poster_url)

    def show_details(self, details: RepackDetails) -> None:
        self._download_action.set_details(details)
        self._title_lbl.setText(details.title)
        sections = _split_description_sections(details.description)
        self._clear_sections()
        self._clear_meta()
        self._clear_tags()
        intro_text = ''
        extra_sections = []
        for heading, body in sections:
            if not heading:
                intro_text = (intro_text + '\n\n' + body).strip() if intro_text else body
            else:
                extra_sections.append((heading, body))
        intro_text, trailing_compat_note = _extract_trailing_backwards_compat(intro_text)
        if trailing_compat_note:
            for i, (heading, body) in enumerate(extra_sections):
                if heading.strip().lower() == 'backwards compatibility':
                    merged = (body + '\n\n' + trailing_compat_note).strip() if body else trailing_compat_note
                    extra_sections[i] = (heading, merged)
                    break
            else:
                extra_sections.append(('Backwards Compatibility', trailing_compat_note))
        self._desc_lbl.setText(render_description_html(intro_text) or 'No description available.')
        extra = dict(details.extra or {})
        is_announcement = bool(extra.pop('is_announcement', False))
        if is_announcement:
            self._title_lbl.setStyleSheet('font-size: 26px; font-weight: 700;')
            self._desc_lbl.setStyleSheet(f'font-size: 16px; font-weight: 500; line-height: {_DESC_LINE_HEIGHT_PCT}%;')
        else:
            self._title_lbl.setStyleSheet('font-weight: 700;')
            self._desc_lbl.setStyleSheet(self._desc_default_qss)
        if details.size_info:
            extra.setdefault('repack_size', details.size_info)
        row = 0
        col = 0
        for key, label in self._META_LABELS:
            value = extra.get(key)
            if not value:
                continue
            field = MetaField(label, value)
            span = 2 if len(value) > 28 else 1
            if span == 2 and col == 1:
                row += 1
                col = 0
            self._meta_grid.addWidget(field, row, col, 1, span)
            self._meta_widgets.append(field)
            col += span
            if col >= 2:
                col = 0
                row += 1
        genres_raw = extra.get('genres', '')
        seen_tags: set[str] = set()
        genre_tags: list[str] = []
        for g in genres_raw.split(','):
            g = g.strip()
            key = g.lower()
            if g and key not in seen_tags:
                seen_tags.add(key)
                genre_tags.append(g)
        self._tags_header.setVisible(bool(genre_tags))
        self._tags_container.setVisible(bool(genre_tags))
        for tag_text in genre_tags:
            pill = make_tag_pill(tag_text, self._tags_container)
            pill.adjustSize()
            self._tags_layout.addWidget(pill)
            self._tag_widgets.append(pill)
        if genre_tags:
            from PyQt6.QtCore import QTimer
            QTimer.singleShot(0, self._tags_layout.update)
        self._sections_header.setVisible(bool(extra_sections) or bool(extra.get('repack_features')))
        repack_features_text = extra.pop('repack_features', '')
        if repack_features_text:
            repack_sub_sections = _split_description_sections(repack_features_text)
            for heading, body in repack_sub_sections:
                heading = heading or 'Repack Features'
                expanded = self._expanded_state.get(heading, False)
                section = CollapsibleSection(heading, expanded=expanded)
                section.set_body_text(body)
                section.toggled.connect(lambda is_open, h=heading: self._expanded_state.__setitem__(h, is_open))
                self._sections_col.insertWidget(self._sections_insert_index, section)
                self._section_widgets.append(section)
                self._sections_insert_index += 1
        for heading, body in extra_sections:
            expanded = self._expanded_state.get(heading, False)
            section = CollapsibleSection(heading, expanded=expanded)
            section.set_body_text(body)
            section.toggled.connect(lambda is_open, h=heading: self._expanded_state.__setitem__(h, is_open))
            self._sections_col.insertWidget(self._sections_insert_index, section)
            self._section_widgets.append(section)
            self._sections_insert_index += 1
        cover_url = details.cover_url
        if details.cover_path:
            self._set_cover_path(details.cover_path)
        elif cover_url:
            self._pending_cover_url = cover_url
            self._poster_downloader.request(cover_url)

    def show_error(self, message: str) -> None:
        self._clear_sections()
        self._clear_meta()
        self._clear_tags()
        self._desc_lbl.setText(f'Failed to load details: {message}')

    def _clear_sections(self) -> None:
        self._sections_insert_index = self._sections_col.indexOf(self._sections_header) + 1
        for section in self._section_widgets:
            self._sections_col.removeWidget(section)
            section.deleteLater()
        self._section_widgets.clear()

    def _clear_meta(self) -> None:
        for field in self._meta_widgets:
            self._meta_grid.removeWidget(field)
            field.deleteLater()
        self._meta_widgets.clear()

    def _clear_tags(self) -> None:
        for tag in self._tag_widgets:
            self._tags_layout.removeWidget(tag)
            tag.deleteLater()
        self._tag_widgets.clear()

    def _on_cover_ready(self, url: str, path: str) -> None:
        if url == self._pending_cover_url:
            self._set_cover_path(path)

    def _set_cover_path(self, path: str) -> None:
        try:
            _stop_previous_movie(self._cover_lbl)
            self._cover_lbl.setImage(path)
            self._cover_lbl.scaledToWidth(self.COVER_WIDTH)
        except Exception:
            logger.warning('Failed to load cover image: %s', path)

class SourceTab(QWidget):

    def __init__(self, source_key: str, source_name: str, manager: DownloadManager, parent=None):
        super().__init__(parent)
        self._source_key = source_key
        self._source_name = source_name
        self.setStyleSheet('background: transparent;')
        self._current_page = 0
        self._has_more = True
        self._is_loading = False
        self._loaded_once = False
        self._active_thread = None
        self._active_worker = None
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 12, 0, 0)
        layout.setSpacing(8)
        self._status_lbl = CaptionLabel('')
        self._status_lbl.setVisible(False)
        layout.addWidget(self._status_lbl)
        from PyQt6.QtWidgets import QStackedWidget
        self._stack = QStackedWidget()
        layout.addWidget(self._stack, 1)
        self._grid = PosterGrid()
        self._grid.poster_clicked.connect(self._show_details)
        self._grid.near_bottom.connect(self._on_near_bottom)
        self._stack.addWidget(self._grid)
        self._details = RepackDetailsView(manager)
        self._details.back_requested.connect(self._show_grid)
        self._stack.addWidget(self._details)
        self._details_thread = None
        self._details_worker = None
        self._stack.setCurrentWidget(self._grid)

    def shutdown(self) -> None:
        try:
            self._grid._poster_downloader.shutdown()
        except Exception:
            logger.exception('Failed to shut down grid poster downloader for %s', self._source_key)
        try:
            self._details._poster_downloader.shutdown()
        except Exception:
            logger.exception('Failed to shut down details poster downloader for %s', self._source_key)

    def load_initial(self) -> None:
        if self._loaded_once:
            return
        self._loaded_once = True
        self._load_next_page(use_cache=True)

    def filter_grid(self, query: str) -> None:
        self._grid.filter_by_title(query)

    def refresh(self) -> None:
        from ..core.repacks import cache as repack_cache
        repack_cache.clear_source_cache(self._source_key)
        self._grid.clear()
        self._current_page = 0
        self._has_more = True
        self._load_next_page(use_cache=False)

    def _on_near_bottom(self) -> None:
        if self._has_more and (not self._is_loading):
            self._load_next_page(use_cache=True)

    def _load_next_page(self, use_cache: bool) -> None:
        if self._is_loading or not self._has_more:
            return
        self._is_loading = True
        next_page = self._current_page + 1
        self._set_status(f'Loading page {next_page}…')
        self._active_thread, self._active_worker = fetch_page_async(self._source_key, next_page, on_done=self._on_page_loaded, on_error=self._on_page_error, use_cache=use_cache)

    def _on_page_loaded(self, result) -> None:
        self._is_loading = False
        self._current_page = result.page
        self._has_more = result.has_more
        self._grid.append_entries(result.entries)
        if not result.entries and self._current_page == 1:
            self._set_status('No repacks found.')
        else:
            self._set_status('')
        if self._has_more:
            from PyQt6.QtCore import QTimer
            QTimer.singleShot(0, self._fill_viewport_if_needed)

    def _fill_viewport_if_needed(self) -> None:
        if self._is_loading or not self._has_more:
            return
        bar = self._grid.verticalScrollBar()
        if bar.maximum() <= 0:
            self._load_next_page(use_cache=True)

    def _on_page_error(self, message: str) -> None:
        self._is_loading = False
        self._set_status(f'Failed to load: {message}')
        logger.error('Repack page load failed for %s: %s', self._source_key, message)

    def _set_status(self, text: str) -> None:
        self._status_lbl.setText(text)
        self._status_lbl.setVisible(bool(text))

    def _show_details(self, entry: RepackEntry) -> None:
        self._details.show_loading(entry)
        self._stack.setCurrentWidget(self._details)
        self._details_thread, self._details_worker = fetch_details_async(self._source_key, entry, on_done=self._on_details_loaded, on_error=self._on_details_error, use_cache=True)

    def _on_details_loaded(self, details) -> None:
        self._details.show_details(details)

    def _on_details_error(self, message: str) -> None:
        self._details.show_error(message)
        logger.error('Repack details load failed for %s: %s', self._source_key, message)

    def _show_grid(self) -> None:
        self._stack.setCurrentWidget(self._grid)

class UpcomingRepacksCard(QWidget):
    CARD_WIDTH = 560
    CARD_HEIGHT = 460

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedSize(self.CARD_WIDTH, self.CARD_HEIGHT)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setStyleSheet(
            'UpcomingRepacksCard { background-color: rgba(255, 255, 255, 12); border: 1px solid rgba(255, 255, 255, 18); border-radius: 8px; }'
        )

        layout = QVBoxLayout(self)
        layout.setContentsMargins(28, 24, 28, 24)
        layout.setSpacing(10)

        self._title_lbl = TitleLabel('Upcoming Repacks')
        self._title_lbl.setStyleSheet('font-weight: 800;')
        layout.addWidget(self._title_lbl)

        self._date_lbl = CaptionLabel('')
        self._date_lbl.setTextColor(QColor(255, 255, 255, 140), QColor(0, 0, 0, 140))
        layout.addWidget(self._date_lbl)

        self._scroll = SmoothScrollArea(self)
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        self._scroll.setStyleSheet('QScrollArea { background: transparent; border: none; }')
        self._scroll.viewport().setStyleSheet('background: transparent;')

        self._list_container = QWidget()
        self._list_container.setStyleSheet('background: transparent;')
        self._list_layout = QVBoxLayout(self._list_container)
        self._list_layout.setContentsMargins(0, 8, 0, 0)
        self._list_layout.setSpacing(6)
        self._list_layout.addStretch(1)
        self._scroll.setWidget(self._list_container)
        layout.addWidget(self._scroll, 1)

        self._empty_lbl = BodyLabel('Loading…')
        self._empty_lbl.setWordWrap(True)
        self._empty_lbl.setStyleSheet('color: rgba(255, 255, 255, 180);')
        self._list_layout.insertWidget(0, self._empty_lbl)

        self._entry_labels: list[BodyLabel] = []

    def set_titles(self, title: str, date_text: str, game_titles: list[str]) -> None:
        self._title_lbl.setText(title or 'Upcoming Repacks')
        self._date_lbl.setText(date_text)
        self._date_lbl.setVisible(bool(date_text))
        for lbl in self._entry_labels:
            self._list_layout.removeWidget(lbl)
            lbl.deleteLater()
        self._entry_labels.clear()
        if not game_titles:
            self._empty_lbl.setText('No upcoming repacks listed right now.')
            self._empty_lbl.setVisible(True)
            return
        self._empty_lbl.setVisible(False)
        for game_title in game_titles:
            lbl = BodyLabel(f'→  {game_title}')
            lbl.setWordWrap(True)
            lbl.setStyleSheet('font-size: 14px; font-weight: 500;')
            self._list_layout.insertWidget(self._list_layout.count() - 1, lbl)
            self._entry_labels.append(lbl)

    def set_error(self, message: str) -> None:
        for lbl in self._entry_labels:
            self._list_layout.removeWidget(lbl)
            lbl.deleteLater()
        self._entry_labels.clear()
        self._date_lbl.setVisible(False)
        self._empty_lbl.setText(f'Failed to load: {message}')
        self._empty_lbl.setVisible(True)

class UpcomingRepacksDialog(MessageBoxBase):

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.yesButton.setText('Close')
        self.cancelButton.hide()
        self._card = UpcomingRepacksCard(self)
        self.viewLayout.addWidget(self._card)
        self.widget.setFixedWidth(self._card.CARD_WIDTH + 48)

    def show_loading(self) -> None:
        self._card.set_titles('Upcoming Repacks', '', [])
        self._card._empty_lbl.setText('Loading…')

    def show_details(self, details: RepackDetails) -> None:
        try:
            from datetime import datetime
            date_text = datetime.now().strftime('%d/%m/%Y')
            titles = list((details.extra or {}).get('upcoming_titles') or [])
            self._card.set_titles(details.title or 'Upcoming Repacks', date_text, titles)
        except RuntimeError:
            pass

    def show_error(self, message: str) -> None:
        try:
            self._card.set_error(message)
        except RuntimeError:
            pass

class RepacksPage(QWidget):

    def __init__(self, manager: DownloadManager, parent=None):
        super().__init__(parent)
        self._manager = manager
        self.setObjectName('repacksPage')
        self.setStyleSheet('background: transparent;')
        outer = QVBoxLayout(self)
        outer.setContentsMargins(24, 20, 24, 20)
        outer.setSpacing(12)
        header = TitleLabel('Game Repacks')
        header.setStyleSheet('font-weight: 700;')
        outer.addWidget(header)
        subheader = BodyLabel('Browse PC game repacks from supported sources.')
        subheader.setStyleSheet('color: rgba(255, 255, 255, 150); background: transparent; font-weight: 400;')
        outer.addWidget(subheader)
        self._pivot = Pivot()
        outer.addWidget(self._pivot)
        self._search_bar = SearchLineEdit()
        self._search_bar.setPlaceholderText('Search repacks by title…')
        self._search_bar.setFixedWidth(320)
        self._search_bar.textChanged.connect(self._on_search_text_changed)
        self._search_bar.searchSignal.connect(self._on_search_text_changed)
        self._search_bar.clearSignal.connect(lambda: self._on_search_text_changed(''))
        self._donate_btn = PushButton(FluentIcon.HEART, 'Donate to FitGirl')
        self._donate_btn.clicked.connect(self._on_donate_clicked)
        self._upcoming_btn = PushButton(FluentIcon.CALENDAR, 'Upcoming Repacks')
        self._upcoming_btn.clicked.connect(self._on_upcoming_clicked)
        self._refresh_btn = TransparentToolButton(FluentIcon.SYNC)
        self._refresh_btn.setToolTip('Refresh repacks and upcoming list')
        self._refresh_btn.setFixedSize(32, 32)
        self._refresh_btn.clicked.connect(self._on_refresh_clicked)
        search_row = QHBoxLayout()
        search_row.setSpacing(8)
        search_row.addWidget(self._search_bar, 0, Qt.AlignmentFlag.AlignLeft)
        search_row.addWidget(self._donate_btn, 0, Qt.AlignmentFlag.AlignLeft)
        search_row.addWidget(self._upcoming_btn, 0, Qt.AlignmentFlag.AlignLeft)
        search_row.addWidget(self._refresh_btn, 0, Qt.AlignmentFlag.AlignLeft)
        search_row.addStretch(1)
        outer.addLayout(search_row)
        from PyQt6.QtWidgets import QStackedWidget
        self._stack = QStackedWidget()
        outer.addWidget(self._stack, 1)
        self._tabs: dict[str, SourceTab] = {}
        self._add_source_tab('fitgirl', 'FitGirl Repacks')
        self._pivot.currentItemChanged.connect(self._on_tab_changed)
        if self._tabs:
            first_key = next(iter(self._tabs))
            self._pivot.setCurrentItem(first_key)
            self._stack.setCurrentWidget(self._tabs[first_key])
            self._tabs[first_key].load_initial()
            self._update_donate_button(first_key)
            self._update_upcoming_button(first_key)
        from PyQt6.QtWidgets import QApplication
        app = QApplication.instance()
        if app is not None:
            app.aboutToQuit.connect(self._on_app_about_to_quit)

    def _on_app_about_to_quit(self) -> None:
        for tab in self._tabs.values():
            try:
                tab.shutdown()
            except Exception:
                logger.exception('Failed to shut down source tab %s', getattr(tab, '_source_key', '?'))
        from ..core.repacks import cache as repack_cache
        try:
            repack_cache.clear_all_cache()
        except Exception:
            logger.exception('Failed to clear repacks cache on shutdown')

    def _on_tab_changed(self, key: str) -> None:
        self._stack.setCurrentWidget(self._tabs[key])
        self._tabs[key].load_initial()
        self._tabs[key].filter_grid(self._search_bar.text())
        self._update_donate_button(key)
        self._update_upcoming_button(key)

    def _update_donate_button(self, key: str) -> None:
        url = _SOURCE_DONATION_URLS.get(key)
        if url:
            display_name = self._tabs[key]._source_name if key in self._tabs else key
            self._donate_btn.setText(f'Donate to {display_name}')
            self._donate_btn.setVisible(True)
        else:
            self._donate_btn.setVisible(False)

    def _update_upcoming_button(self, key: str) -> None:
        self._upcoming_btn.setVisible(key in _SOURCE_UPCOMING_SUPPORTED)

    def _on_donate_clicked(self) -> None:
        from PyQt6.QtCore import QUrl
        from PyQt6.QtGui import QDesktopServices

        current_key = self._pivot.currentRouteKey()
        url = _SOURCE_DONATION_URLS.get(current_key)
        if url:
            QDesktopServices.openUrl(QUrl(url))

    def _on_upcoming_clicked(self) -> None:
        current_key = self._pivot.currentRouteKey()
        if current_key not in _SOURCE_UPCOMING_SUPPORTED:
            return
        existing_thread = getattr(self, '_upcoming_thread', None)
        if existing_thread is not None and existing_thread.isRunning():
            return
        dialog = UpcomingRepacksDialog(self)
        dialog.show_loading()
        dialog.show()
        self._upcoming_thread, self._upcoming_worker = fetch_upcoming_repacks_async(
            current_key,
            on_done=dialog.show_details,
            on_error=dialog.show_error,
            use_cache=True,
        )

    def _on_refresh_clicked(self) -> None:
        current_key = self._pivot.currentRouteKey()
        if not current_key or current_key not in self._tabs:
            return
        from ..core.repacks import cache as repack_cache
        try:
            repack_cache.clear_source_cache(current_key)
        except Exception:
            logger.exception('Failed to clear cache for %s during refresh', current_key)
        self._tabs[current_key].refresh()

    def _on_search_text_changed(self, text: str) -> None:
        current_key = self._pivot.currentRouteKey()
        if current_key in self._tabs:
            self._tabs[current_key].filter_grid(text)

    def _add_source_tab(self, key: str, display_name: str) -> None:
        tab = SourceTab(key, display_name, self._manager, self)
        self._tabs[key] = tab
        self._stack.addWidget(tab)
        self._pivot.addItem(routeKey=key, text=display_name)