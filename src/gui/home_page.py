import importlib
import itertools
import json
import logging
import math
import os
import random
import time
from functools import partial
from types import SimpleNamespace
from typing import Optional
from PySide6.QtCore import QEasingCurve, QEvent, QPoint, QPointF, QRect, QRectF, QSize, Qt, QTimer, QVariantAnimation, Signal
from PySide6.QtGui import QColor, QFont, QFontMetrics, QLinearGradient, QPainter, QPainterPath, QPen, QPixmap, QRegion
from PySide6.QtWidgets import QApplication, QFrame, QHBoxLayout, QLabel, QPushButton, QScrollArea, QVBoxLayout, QWidget
from qfluentwidgets import BodyLabel, CardWidget, CaptionLabel, FluentIcon, ProgressBar, SmoothScrollArea, StrongBodyLabel, SubtitleLabel, TitleLabel, TransparentToolButton
from .grid import PosterStrip, _LIFT, images
from src.core.config import paths as app_paths, settings as app_settings, is_tab_enabled, tab_visibility_bus
from src.core.theme import palette, register_theme_refresh, apply_tool_button_theme, themed_icon, to_color
from src.core.translations import current_language, register_locale_refresh, tr
logger = logging.getLogger(__name__)
_ICON_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'photos')
_PROVIDER_ICONS = {'fitgirl': 'fg.png', 'gog': 'gr.png'}
_PROVIDER_LABELS = {'fitgirl': 'FitGirl', 'gog': 'GOG'}


_badge_cache: dict[str, QPixmap] = {}


def _provider_badge(provider: str):
    pix = _badge_cache.get(provider)
    if pix is None:
        icon_file = _PROVIDER_ICONS.get(provider or '')
        icon_path = os.path.join(_ICON_DIR, icon_file) if icon_file else None
        if not (icon_path and os.path.isfile(icon_path)):
            return None
        icon = QPixmap(icon_path).scaled(22, 22, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)
        if icon.isNull():
            return None
        pix = QPixmap(28, 28)
        pix.fill(Qt.GlobalColor.transparent)
        p = QPainter(pix)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(0, 0, 0, 150))
        p.drawRoundedRect(0, 0, 28, 28, 6, 6)
        p.drawPixmap((28 - icon.width()) // 2, (28 - icon.height()) // 2, icon)
        p.end()
        _badge_cache[provider] = pix
    return None if pix.isNull() else pix


_DEFAULT_BACKDROP = '#ff6a3d'
_lazy_cache: dict = {}
_placeholders: dict = {}
_LANG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'lang')
_splash_cache: dict = {}
_KIND_META = {
    'repack': ('home.dashboard_popular_eyebrow', 'home.dashboard_popular_body', 'Popular This Week', 'One of the most downloaded PC game repacks right now.', 'pc_games_enabled', ('repacks', 'fitgirl')),
    'movie': ('home.dashboard_popular_movie_eyebrow', 'home.dashboard_popular_movie_body', 'Popular Movie', 'One of the most popular movies right now.', 'tv_enabled', ('tv', 'movies')),
    'show': ('home.dashboard_popular_show_eyebrow', 'home.dashboard_popular_show_body', 'Popular Show', 'One of the most popular shows right now.', 'tv_enabled', ('tv', 'series')),
    'anime': ('home.dashboard_popular_anime_eyebrow', 'home.dashboard_popular_anime_body', 'Popular Anime', 'One of the most popular anime right now.', 'tv_enabled', ('tv', 'anime')),
    'manga': ('home.dashboard_popular_manga_eyebrow', 'home.dashboard_popular_manga_body', 'Popular Manga', 'One of the most popular manga right now.', 'books_enabled', ('books', 'manga')),
}


def _tr_safe(key: str, fallback: str) -> str:
    try:
        value = tr(key)
    except Exception:
        return fallback
    return fallback if not value or value.strip().lower() == key.lower() else value


def _saved_playlists() -> list:
    path = os.path.join(app_paths.config_dir, 'music', 'music_playlists.json')
    try:
        with open(path, 'r', encoding='utf-8') as fh:
            entries = json.load(fh).get('playlists', [])
    except (OSError, ValueError):
        return []
    if not isinstance(entries, list):
        return []
    playlists = []
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            continue
        songs = entry.get('songs')
        covers = ([song.get('cover_url') for song in songs if isinstance(song, dict)]
                  if isinstance(songs, list) else [])
        playlists.append(SimpleNamespace(
            id=entry.get('id') or str(index),
            name=entry.get('name') or _tr_safe('music.untitled', 'Untitled'),
            songs=[SimpleNamespace(cover_url=cover) for cover in covers],
        ))
    return playlists


def _is_birthday() -> bool:
    today = time.localtime()
    return today.tm_mon == 9 and today.tm_mday == 16


def _splash_keys(lang: str) -> list:
    if lang not in _splash_cache:
        keys = []
        for code in (lang, 'en'):
            try:
                with open(os.path.join(_LANG_DIR, f'{code}.json'), encoding='utf-8') as fh:
                    keys = sorted(k for k in json.load(fh).get('strings', {}) if k.startswith('home.splash.'))
            except Exception:
                continue
            if keys:
                break
        _splash_cache[lang] = keys
    return _splash_cache[lang]


class _SplashLabel(QLabel):
    _BASE_ANGLE = -14.0
    _WOBBLE_DEGREES = 3.5
    _MAX_SCALE = 1.05
    _BOB = 2.0
    _PERIOD_MS = 5000
    _THROBS = 3
    _FONT_PX = 15

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        font = self.font()
        font.setBold(True)
        font.setPixelSize(self._FONT_PX)
        self.setFont(font)
        self._text_color = QColor('#ffd166')
        self._shadow_color = QColor(0, 0, 0, 130)
        self._pm: Optional[QPixmap] = None
        self._t = 0.0
        self._base = self._BASE_ANGLE
        self._started_at = 0.0
        self._clock = QTimer(self)
        self._clock.setInterval(33)
        self._clock.setTimerType(Qt.TimerType.CoarseTimer)
        self._clock.timeout.connect(self._advance)

    def setText(self, text: str) -> None:
        super().setText(text)
        self._pm = None
        self.update()

    def fit_width(self, avail: float) -> None:
        size = self._FONT_PX
        if avail > 0 and self.text():
            font = self.font()
            font.setPixelSize(self._FONT_PX)
            width = QFontMetrics(font).horizontalAdvance(self.text())
            target = max(80.0, (avail - 28.0) / 0.98)
            if width > target:
                size = max(9, min(self._FONT_PX, int(self._FONT_PX * target / width)))
        if size == self.font().pixelSize():
            return
        font = self.font()
        font.setPixelSize(size)
        self.setFont(font)
        self._pm = None
        self.update()

    def set_base_angle(self, degrees: float) -> None:
        self._base = degrees
        self.update()

    def set_color(self, color: str) -> None:
        self._text_color = QColor(color)
        self._pm = None
        self.update()

    def start_animation(self) -> None:
        if not self._clock.isActive():
            self._started_at = time.perf_counter() - self._t * (self._PERIOD_MS / 1000.0)
            self._clock.start()

    def stop_animation(self) -> None:
        self._clock.stop()

    def _advance(self) -> None:
        self._t = ((time.perf_counter() - self._started_at) * 1000.0 / self._PERIOD_MS) % 1.0
        self.update()

    def _raster(self) -> QPixmap:
        k = self.devicePixelRatioF() * 2
        fm = self.fontMetrics()
        text = self.text()
        w, h = fm.horizontalAdvance(text) + 4, fm.height() + 4
        pm = QPixmap(int(w * k) + 1, int(h * k) + 1)
        pm.setDevicePixelRatio(k)
        pm.fill(Qt.GlobalColor.transparent)
        p = QPainter(pm)
        p.setFont(self.font())
        p.setRenderHint(QPainter.RenderHint.TextAntialiasing)
        box = QRectF(0, 0, w, h)
        p.setPen(self._shadow_color)
        p.drawText(box.translated(1.0, 1.0), Qt.AlignmentFlag.AlignCenter, text)
        p.setPen(self._text_color)
        p.drawText(box, Qt.AlignmentFlag.AlignCenter, text)
        p.end()
        return pm

    def paintEvent(self, event) -> None:
        if not self.text():
            return
        if self._pm is None or self._pm.devicePixelRatio() != self.devicePixelRatioF() * 2:
            self._pm = self._raster()
        pm = self._pm
        turn = self._t * 6.283185307179586
        painter = QPainter(self)
        painter.setRenderHints(QPainter.RenderHint.SmoothPixmapTransform | QPainter.RenderHint.Antialiasing)
        center = QRectF(self.rect()).center()
        painter.translate(center.x(), center.y() + math.sin(turn * 2 + 1.3) * self._BOB)
        painter.rotate(self._base + math.sin(turn) * self._WOBBLE_DEGREES)
        scale = 1.0 + (self._MAX_SCALE - 1.0) * (0.5 + 0.5 * math.sin(turn * self._THROBS))
        painter.scale(scale, scale)
        k = pm.devicePixelRatio()
        painter.drawPixmap(QPointF(-pm.width() / k / 2, -pm.height() / k / 2), pm)
        painter.end()


def _lazy(module: str, attr: Optional[str] = None, package: Optional[str] = None):
    key = (module, attr)
    if key not in _lazy_cache:
        try:
            mod = importlib.import_module(module, package)
            if attr:
                try:
                    val = getattr(mod, attr)
                except AttributeError:
                    val = importlib.import_module(f'{mod.__name__}.{attr}')
            else:
                val = mod
            _lazy_cache[key] = val
        except Exception:
            logger.exception('%s unavailable for Home', module)
            _lazy_cache[key] = None
    return _lazy_cache[key]


def _ignore(*_a) -> None:
    pass


def _on(key: str) -> bool:
    return bool(getattr(app_settings, key, False))


def _clean_repack_title(raw: str) -> str:
    title = raw or ''
    try:
        from src.core.repacks.base import display_title

        return display_title(title) or title
    except Exception:
        logger.debug('Could not clean repack title', exc_info=True)
        return title


def _set_scroll_content(area: QScrollArea, widget: QWidget) -> None:
    area.setFrameShape(QFrame.Shape.NoFrame)
    area.setWidgetResizable(True)
    area.setStyleSheet('QScrollArea{background:transparent;border:none;}')
    area.setWidget(widget)
    widget.setAutoFillBackground(False)
    area.viewport().setAutoFillBackground(False)


def _blend(a: QColor, b: QColor, t: float) -> QColor:
    r1, g1, b1, a1 = a.getRgb()
    r2, g2, b2, _ = b.getRgb()
    return QColor(round(r1 + (r2 - r1) * t), round(g1 + (g2 - g1) * t), round(b1 + (b2 - b1) * t), a1)


def _palette_colors() -> tuple:
    c = palette()
    return to_color(c['primary_text']), to_color(c['muted']), to_color(c['surface_tint'])


class _ThemedCard(CardWidget):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAutoFillBackground(False)
        self.setBorderRadius(18)

    def _normalBackgroundColor(self) -> QColor:
        c = to_color(palette()['card_bg'])
        c.setAlpha(80)
        return c

    def _hoverBackgroundColor(self) -> QColor:
        c = to_color(palette()['card_hover'])
        c.setAlpha(135)
        return c

    def _pressedBackgroundColor(self) -> QColor:
        c = to_color(palette()['card_hover'])
        c.setAlpha(175)
        return c

    def paintEvent(self, event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = self.borderRadius
        border = to_color(palette()['card_border'])
        border.setAlpha(min(border.alpha(), int(self.backgroundColor.alpha() * 0.8)))
        p.setPen(QPen(border, 1))
        p.setBrush(self.backgroundColor)
        p.drawRoundedRect(QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5), r, r)
        p.end()


class _PosterTile(QFrame):
    def __init__(self, size=(140, 200), radius: int = 10, parent=None):
        super().__init__(parent)
        self.setFixedSize(*size)
        self._path: Optional[str] = None
        self._pix: Optional[QPixmap] = None
        self._clip = QPainterPath()
        self._clip.addRoundedRect(QRectF(self.rect()), radius, radius)

    def set_image_path(self, path: Optional[str]) -> None:
        path = path or None
        if path == self._path:
            return
        self._path = path
        self._pix = None
        if path:
            cached = images.peek(path, self.size())
            if cached is not None:
                self._pix = None if cached.isNull() else cached
            else:
                images.get(path, self.size(), partial(self._on_ready, path), owner=self)
        self.update()

    def set_pixmap(self, pixmap: Optional[QPixmap]) -> None:
        self._pix = pixmap
        self.update()

    def _on_ready(self, source: str, pixmap) -> None:
        if source == self._path:
            self._pix = None if pixmap.isNull() else pixmap
            self.update()

    def hideEvent(self, event) -> None:
        try:
            images.release(self)
        except Exception:
            pass
        self._pix = None
        super().hideEvent(event)

    def showEvent(self, event) -> None:
        super().showEvent(event)
        if self._path and self._pix is None:
            try:
                images.get(self._path, self.size(), partial(self._on_ready, self._path), owner=self)
            except Exception:
                pass

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        if self._pix is not None:
            painter.setClipPath(self._clip)
            painter.drawPixmap(0, 0, self._pix)
        else:
            pal = palette()
            painter.fillPath(self._clip, to_color(pal['poster_fallback_bg']))
            pad = self.width() * 0.28
            size = (max(1, int(self.width() - 2 * pad)), max(1, int(self.height() - 2 * pad)))
            key = (size, pal['muted'])
            if key not in _placeholders:
                _placeholders[key] = themed_icon('home.poster_placeholder', FluentIcon.PHOTO, size=max(size), color=pal['muted']).pixmap(*size)
            painter.drawPixmap(int(pad), int(pad), _placeholders[key])
        painter.end()


class _Card:
    WIDTH = 0
    IMG = (0, 0)
    _uids = itertools.count()

    def __init__(self, key: str, title: str, subtitle: str = '', provider: Optional[str] = None,
                 progress: Optional[float] = None):
        self.uid = next(self._uids)
        self.key, self.title, self.subtitle = str(key), title, subtitle
        self.provider, self.progress = provider, progress
        self.image: Optional[str] = None
        self.on_click = None

    def set_image_path(self, path: Optional[str]) -> None:
        self.image = path or None

    def widget(self, pixmap: Optional[QPixmap], height: int) -> CardWidget:
        raise NotImplementedError

    _heights: dict = {}

    def min_height(self) -> int:
        key = (type(self), self.title, self.subtitle, self.progress is not None)
        if key not in self._heights:
            w = self.widget(None, 0)
            self._heights[key] = w.minimumSizeHint().height()
            w.deleteLater()
        return self._heights[key]


def _two_lines(fm, text: str, width: int) -> str:
    width -= 1
    words, line, i = text.split(), '', 0
    while i < len(words):
        trial = f'{line} {words[i]}'.strip()
        if line and fm.horizontalAdvance(trial) > width:
            break
        line, i = trial, i + 1
    tail = ' '.join(words[i:]).rstrip(' -\u2013+/,:')
    elide = Qt.TextElideMode.ElideRight
    return fm.elidedText(line, elide, width) + (f'\n{fm.elidedText(tail, elide, width)}' if tail else '')


class _PosterCardWidget(_ThemedCard):
    def __init__(self, card: '_PosterCard', pixmap: Optional[QPixmap], height: int):
        super().__init__()
        self.setFixedSize(card.WIDTH, height)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 10)
        layout.setSpacing(8)
        self._tile = _PosterTile(size=card.IMG)
        self._tile.set_pixmap(pixmap)
        layout.addWidget(self._tile, 0, Qt.AlignmentFlag.AlignHCenter)
        self.badge = None
        pix = _provider_badge(card.provider or '')
        if pix is not None:
            self.badge = QLabel(self._tile)
            self.badge.setPixmap(pix)
            self.badge.setFixedSize(28, 28)
            self.badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
            self.badge.move(6, 6)
        title_lbl = StrongBodyLabel(card.title)
        title_lbl.setText(_two_lines(title_lbl.fontMetrics(), card.title, card.WIDTH - 16))
        title_lbl.setWordWrap(True)
        title_lbl.setFixedHeight(34)
        layout.addWidget(title_lbl)
        if card.subtitle:
            sub = CaptionLabel(card.subtitle)
            sub.setFixedHeight(16)
            layout.addWidget(sub)


class _DownloadCardWidget(_ThemedCard):
    def __init__(self, card: '_DownloadTileCard', pixmap: Optional[QPixmap], height: int):
        super().__init__()
        self.setFixedSize(card.WIDTH, height)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 10)
        layout.setSpacing(8)
        tile_row = QHBoxLayout()
        tile_row.addStretch(1)
        self._tile = _PosterTile(size=card.IMG, radius=8)
        self._tile.set_pixmap(pixmap)
        tile_row.addWidget(self._tile)
        tile_row.addStretch(1)
        layout.addLayout(tile_row)
        title_lbl = StrongBodyLabel(card.title)
        title_lbl.setWordWrap(True)
        title_lbl.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        title_lbl.setMaximumHeight(36)
        layout.addWidget(title_lbl)
        sub = CaptionLabel(card.subtitle)
        sub.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        layout.addWidget(sub)
        if card.progress is not None:
            bar = ProgressBar(self, useAni=False)
            bar.setRange(0, 100)
            bar.setValue(int(max(0.0, min(100.0, card.progress))))
            bar.setFixedHeight(4)
            layout.addWidget(bar)
            pct = CaptionLabel(f'{card.progress:.0f}%')
            pct.setAlignment(Qt.AlignmentFlag.AlignHCenter)
            layout.addWidget(pct)


class _PosterCard(_Card):
    WIDTH, IMG = 160, (144, 204)

    def __init__(self, key: str, title: str, subtitle: str = '', provider: Optional[str] = None):
        super().__init__(key, title, subtitle, provider)

    def widget(self, pixmap: Optional[QPixmap], height: int) -> CardWidget:
        return _PosterCardWidget(self, pixmap, height)

    def min_height(self) -> int:
        return 18 + self.IMG[1] + 8 + 34 + (8 + 16 if self.subtitle else 0)


class _DownloadTileCard(_Card):
    WIDTH, IMG = 180, (64, 64)

    def __init__(self, key: str, title: str, subtitle: str, progress_pct: Optional[float] = None):
        super().__init__(key, title, subtitle, progress=progress_pct)

    def widget(self, pixmap: Optional[QPixmap], height: int) -> CardWidget:
        return _DownloadCardWidget(self, pixmap, height)

    @classmethod
    def from_playlist(cls, playlist) -> '_DownloadTileCard':
        return cls(playlist.id, playlist.name, tr('home.dashboard_song_count', count=len(playlist.songs)))


class _CardStrip(PosterStrip):
    _SNAP = False
    _HOVER_CURSOR = Qt.CursorShape.ArrowCursor
    _MARGIN = (2, 14)

    def __init__(self, height: int):
        card_h = self._nominal_h = height - sum(self._MARGIN)
        super().__init__(cell_w=_PosterCard.WIDTH, poster_h=card_h, cover_attrs=('image',), key_attr='uid',
                         title_attr='title', show_caption=False, img_size=_PosterCard.IMG, cell_h=card_h,
                         gap=14, pad=2, top=self._MARGIN[0], height=height, colors_fn=_palette_colors)
        self._snaps: dict = {}
        self._badges: dict[int, QRect] = {}

        self.poster_clicked.connect(self._on_card_clicked)

    def _on_card_clicked(self, card) -> None:
        callback = getattr(card, 'on_click', None)
        if callback is None:
            return
        try:
            callback(card.key)
        except Exception:
            logger.exception('Home card click failed')

    def set_cards(self, cards: list) -> None:
        value = self._bar().value()
        if cards:
            self._cw, self._img = cards[0].WIDTH, cards[0].IMG

        self._cell_h = max([self._nominal_h] + [card.min_height() for card in cards])
        self.setFixedHeight(self._cell_h + sum(self._MARGIN))
        self._snaps.clear()
        self._badges.clear()
        self.set_entries(cards)
        self._bar().setValue(value)

    def _render(self, card: _Card, pixmap: Optional[QPixmap], state: str) -> QPixmap:
        key = (card.uid, state, pixmap.cacheKey() if pixmap else 0)
        render = self._snaps.get(key)
        if render is None:
            w = card.widget(pixmap, self._cell_h)
            w.isHover, w.isPressed = state != 'normal', state == 'pressed'
            w.setBackgroundColor(getattr(w, f'_{state}BackgroundColor')())
            w.layout().activate()
            ratio = w.devicePixelRatioF()
            render = QPixmap(round(w.width() * ratio), round(w.height() * ratio))
            render.setDevicePixelRatio(ratio)
            render.fill(Qt.GlobalColor.transparent)
            w.render(render, QPoint(), QRegion(), QWidget.RenderFlag.DrawChildren)
            self._snaps[key] = render
            if getattr(w, 'badge', None) is not None:
                self._badges[card.uid] = w.badge.geometry().translated(w._tile.pos())
            w.deleteLater()
        return render

    def _paint_card(self, p, pos: int, cell: QRect) -> None:
        card = self._entry(pos)
        src = self._src(pos)
        pixmap = images.peek(src, self._img) if src else None
        if pixmap is not None and pixmap.isNull():
            pixmap = None
        if self._pressed == pos:
            p.drawPixmap(cell.topLeft(), self._render(card, pixmap, 'pressed'))
            return
        p.drawPixmap(cell.topLeft(), self._render(card, pixmap, 'normal'))
        fade = min(1.0, self._lift.get(pos, 0.0) / _LIFT)
        if fade:
            p.setOpacity(fade)
            p.drawPixmap(cell.topLeft(), self._render(card, pixmap, 'hover'))
            p.setOpacity(1.0)

    def _tip(self, pos: int, point: QPoint) -> str:
        card = self._entry(pos)
        badge = self._badges.get(card.uid)
        if badge is None or not badge.translated(self._cell_rect(pos).topLeft()).contains(point):
            return ''
        return _PROVIDER_LABELS.get(card.provider, card.provider)

    def _apply_colors(self) -> None:
        self._snaps.clear()
        super()._apply_colors()


class _CardRow(QWidget):
    def __init__(self, title: str, on_see_all=None, row_height: int = 290):
        super().__init__()
        self._sections: dict = {}
        self._section_sigs: dict = {}
        self._enabled = True
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(10)
        header = QHBoxLayout()
        self.title_lbl = SubtitleLabel(title)
        header.addWidget(self.title_lbl, 1)
        if on_see_all is not None:
            btn = TransparentToolButton(themed_icon('home.see_all_chevron', FluentIcon.RIGHT_ARROW), self)
            apply_tool_button_theme(btn)
            btn.setToolTip(tr('home.dashboard_see_all'))
            btn.clicked.connect(lambda *_: on_see_all())
            header.addWidget(btn)
        outer.addLayout(header)
        self._strip = _CardStrip(row_height)
        outer.addWidget(self._strip)
        self.hide()

    def set_cards(self, cards: list, section: str = '') -> None:
        sig = tuple((card.key, card.title, card.subtitle, card.image, card.provider, card.progress)
                    for card in cards)
        if self._section_sigs.get(section) == sig:
            return
        self._section_sigs[section] = sig
        self._sections[section] = cards
        self._strip.set_cards([card for group in self._sections.values() for card in group])
        self._sync()

    def set_enabled(self, flag: bool) -> None:
        self._enabled = flag
        self._sync()

    def _sync(self) -> None:
        self.setVisible(self._enabled and any(self._sections.values()))


def _readable_text_color(bg_hex: str) -> str:
    return '#1a1a1a' if QColor(bg_hex).lightness() > 165 else '#ffffff'


class _FeaturedButton(QPushButton):
    def __init__(self, text: str = ''):
        super().__init__(text)
        self._bg = QColor(_DEFAULT_BACKDROP).lighter(140)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setAttribute(Qt.WidgetAttribute.WA_Hover, True)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)

    def set_base(self, color: QColor) -> None:
        self._bg = QColor(color)
        self.update()

    def sizeHint(self) -> QSize:
        font = QFont(self.font())
        font.setPixelSize(14)
        font.setWeight(QFont.Weight.DemiBold)
        return QSize(max(140, QFontMetrics(font).horizontalAdvance(self.text()) + 40), 36)

    def paintEvent(self, event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        bg = QColor(self._bg)
        if self.isDown():
            bg = bg.darker(112)
        elif self.underMouse():
            bg = bg.lighter(112)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(bg)
        p.drawRoundedRect(QRectF(self.rect()), 6, 6)
        font = QFont(self.font())
        font.setPixelSize(14)
        font.setWeight(QFont.Weight.DemiBold)
        p.setFont(font)
        p.setPen(QColor(_readable_text_color(self._bg.name())))
        p.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, self.text())
        p.end()


class _FeaturedCard(QFrame):
    activated = Signal(str, object)
    hovered = Signal(bool)
    _H, _PW, _RADIUS, _GAP, _PAD = 232, 155, 18, 28, 32

    def __init__(self):
        super().__init__()
        self.setLayoutDirection(Qt.LayoutDirection.LeftToRight)
        self.setFixedHeight(self._H)
        self.setAttribute(Qt.WidgetAttribute.WA_Hover, True)
        self._hover = False
        self._c0 = self._c1 = to_color(palette()['card_bg'])
        self._ct = self._pt = 1.0
        self._solid: Optional[str] = None
        self._pix: Optional[QPixmap] = None
        self._pix0: Optional[QPixmap] = None
        self._glow: Optional[QPixmap] = None
        self._poster: Optional[str] = None
        self._state = None
        self._mode: Optional[str] = None
        self._payload = None
        self._color_anim = self._tween('_ct')
        self._poster_anim = self._tween('_pt')
        self._poster_anim.finished.connect(self._drop_old_poster)
        self._lay = QHBoxLayout(self)
        self._lay.setContentsMargins(self._PAD, 24, self._PAD, 24)
        col = QVBoxLayout()
        col.setSpacing(6)
        self._eyebrow = self._plain_label(12, QFont.Weight.Bold)
        self._title = self._plain_label(28, QFont.Weight.DemiBold)
        self._title.setWordWrap(True)
        self._body = self._plain_label(14, QFont.Weight.Normal)
        self._body.setWordWrap(True)
        self._btn = _FeaturedButton('')
        self._btn.setFixedHeight(36)
        self._btn.setMinimumWidth(140)
        self._btn.hide()
        self._btn.clicked.connect(self._activate)
        col.addStretch(1)
        left = Qt.AlignmentFlag.AlignAbsolute | Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
        for w in (self._eyebrow, self._title, self._body):
            w.setAlignment(left)
            col.addWidget(w)
        col.addSpacing(14)
        col.addWidget(self._btn, 0, Qt.AlignmentFlag.AlignAbsolute | Qt.AlignmentFlag.AlignLeft)
        col.addStretch(1)
        self._lay.addLayout(col, 1)

    @staticmethod
    def _plain_label(px: int, weight) -> QLabel:
        lbl = QLabel('')
        font = QFont(lbl.font())
        font.setPixelSize(px)
        font.setWeight(weight)
        lbl.setFont(font)
        lbl.setStyleSheet('background: transparent;')
        return lbl

    @staticmethod
    def _rgba(c: QColor) -> str:
        return f'rgba({c.red()},{c.green()},{c.blue()},{c.alpha()})'

    def _activate(self) -> None:
        if self._btn.isVisible():
            self.activated.emit(self._mode or '', self._payload)

    def _set_hover(self, on: bool) -> None:
        if on != self._hover:
            self._hover = on
            self.setCursor(Qt.CursorShape.PointingHandCursor if on and self._btn.isVisible() else Qt.CursorShape.ArrowCursor)
            self.update()
            self.hovered.emit(on)

    def enterEvent(self, event) -> None:
        self._set_hover(True)
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:
        self._set_hover(False)
        super().leaveEvent(event)

    def mouseReleaseEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton and self.rect().contains(event.position().toPoint()):
            self._activate()
        super().mouseReleaseEvent(event)

    def event(self, e) -> bool:
        if e.type() == QEvent.Type.LayoutDirectionChange and self.layoutDirection() != Qt.LayoutDirection.LeftToRight:
            self.setLayoutDirection(Qt.LayoutDirection.LeftToRight)
        return super().event(e)

    def _tween(self, attr: str) -> QVariantAnimation:
        anim = QVariantAnimation(self)
        anim.setDuration(260)
        anim.setStartValue(0.0)
        anim.setEndValue(1.0)
        anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        anim.valueChanged.connect(lambda v: (setattr(self, attr, float(v)), self.update()))
        return anim

    def _restart(self, anim: QVariantAnimation, attr: str) -> None:
        anim.stop()
        setattr(self, attr, 0.0)
        anim.start()

    def _drop_old_poster(self) -> None:
        self._pix0 = None
        self.update()

    def set_content(self, mode: str, eyebrow: str, title: str, body: str, action: str, poster: Optional[str] = None, payload=None) -> None:
        self._payload = payload
        state = (mode, eyebrow, title, body, action, poster)
        if state == self._state:
            return
        self._state = state
        self._mode = mode
        self._eyebrow.setText(eyebrow)
        self._title.setText(title)
        self._body.setText(body)
        self._btn.setText(action)
        self._btn.setVisible(bool(action))
        self._set_poster(poster)
        if poster:
            images.dominant(poster, partial(self._on_color, poster), owner=self)
        else:
            self._set_colors(None)
        self.setVisible(True)

    def _set_poster(self, path: Optional[str]) -> None:
        path = path or None
        if path == self._poster:
            return
        self._poster = path
        self._pix0, self._pix = self._pix, None
        self._lay.setContentsMargins(self._PW + self._GAP if path else self._PAD, 24, self._PAD, 24)
        if path is None:
            self._pix0 = self._glow = None
            self.update()
            return
        size = QSize(self._PW, self._H)
        cached = images.peek(path, size)
        if cached is not None:
            self._on_poster(path, cached)
        else:
            images.get(path, size, partial(self._on_poster, path), owner=self)
            self.update()

    def _on_poster(self, source: str, pixmap) -> None:
        if source != self._poster:
            return
        if pixmap is None or pixmap.isNull():
            self._pix = self._pix0 = self._glow = None
            self.update()
            return
        self._pix = pixmap
        self._glow = pixmap.scaled(24, 36, Qt.AspectRatioMode.IgnoreAspectRatio, Qt.TransformationMode.SmoothTransformation)
        self._restart(self._poster_anim, '_pt')

    def _on_color(self, source: str, colors: list) -> None:
        if source == self._poster and colors:
            self._set_colors(colors[0])

    def _set_colors(self, color) -> None:
        if color is not None:
            h, s, v, _ = color.getHsv()
            solid = QColor.fromHsv(h, max(90, min(220, s)), max(70, min(190, v))).name()
        else:
            solid = _DEFAULT_BACKDROP
        if solid == self._solid:
            return
        self._solid = solid
        self._c0 = _blend(self._c0, self._c1, self._ct)
        self._c1 = QColor(solid)
        self._restart(self._color_anim, '_ct')
        dark_text = _readable_text_color(solid) == '#1a1a1a'
        text = QColor('#1a1a1a') if dark_text else QColor('#ffffff')
        muted = QColor(0, 0, 0, 200) if dark_text else QColor(255, 255, 255, 225)
        self._eyebrow.setStyleSheet(f'background: transparent; color: {self._rgba(muted)};')
        self._title.setStyleSheet(f'background: transparent; color: {self._rgba(text)};')
        self._body.setStyleSheet(f'background: transparent; color: {self._rgba(muted)};')
        bg = QColor(solid).lighter(140)
        self._btn.set_base(bg)

    def paintEvent(self, event) -> None:
        p = QPainter(self)
        p.setRenderHints(QPainter.RenderHint.Antialiasing | QPainter.RenderHint.SmoothPixmapTransform)
        rect = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        path = QPainterPath()
        path.addRoundedRect(rect, self._RADIUS, self._RADIUS)
        base = _blend(self._c0, self._c1, self._ct)
        grad = QLinearGradient(rect.topLeft(), rect.bottomRight())
        grad.setColorAt(0.0, base.lighter(106))
        grad.setColorAt(1.0, base.darker(112))
        p.fillPath(path, grad)
        if self._poster:
            dest = QRect(0, 0, self._PW, self._H)
            p.save()
            p.setClipPath(path)
            if self._glow is not None and self.width() > self._PW:
                wide = QRect(self._PW, 0, self.width() - self._PW, self._H)
                p.setOpacity(0.6 * self._pt)
                p.drawPixmap(wide, self._glow)
                p.setOpacity(1.0)
                veil = QLinearGradient(wide.left(), 0, wide.right(), 0)
                veil.setColorAt(0.0, QColor(base.red(), base.green(), base.blue(), 255))
                veil.setColorAt(0.4, QColor(base.red(), base.green(), base.blue(), 245))
                veil.setColorAt(0.8, QColor(base.red(), base.green(), base.blue(), 120))
                veil.setColorAt(1.0, QColor(base.red(), base.green(), base.blue(), 40))
                p.fillRect(wide, veil)
            if self._pix0 is not None:
                p.drawPixmap(dest, self._pix0)
            elif self._pix is None:
                p.fillRect(dest, to_color(palette()['poster_fallback_bg']))
            if self._pix is not None:
                p.setOpacity(self._pt)
                p.drawPixmap(dest, self._pix)
            p.restore()
        if self._hover:
            p.fillPath(path, QColor(255, 255, 255, 14))
        p.setPen(QPen(QColor(255, 255, 255, 46 if self._hover else 26), 1))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawPath(path)
        p.end()

    def hideEvent(self, event) -> None:
        try:
            images.release(self)
        except Exception:
            pass
        self._poster_anim.stop()
        self._pix0 = self._pix = self._glow = None
        super().hideEvent(event)

    def showEvent(self, event) -> None:
        super().showEvent(event)
        if self._poster and self._pix is None:
            try:
                images.get(self._poster, QSize(self._PW, self._H),
                           partial(self._on_poster, self._poster), owner=self)
            except Exception:
                pass


class HomePage(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._popular: list = []
        self._sigs: dict = {'playlists': ()}
        self._rows: list = []
        self._cands: dict = {}
        self._anime_lists: dict = {}
        self._rot_idx = 0
        self._animation_active = True
        self._gen = 0
        self._booting = True
        self._rot_timer = QTimer(self)
        self._rot_timer.setInterval(9000)

        self._rot_timer.setTimerType(Qt.TimerType.CoarseTimer)
        self._rot_timer.timeout.connect(self._rotate)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)


        scroll = SmoothScrollArea()
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.verticalScrollBar().setSingleStep(60)
        self._vbar = scroll.verticalScrollBar()
        self._vbar.valueChanged.connect(self._sync_splash_animation)
        container = QWidget()
        _set_scroll_content(scroll, container)
        outer.addWidget(scroll)
        layout = QVBoxLayout(container)
        layout.setContentsMargins(24, 12, 28, 20)
        layout.setSpacing(24)
        self._welcome_lbl = TitleLabel('')
        self._welcome_lbl.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        layout.addWidget(self._welcome_lbl)
        self._splash = _SplashLabel(container)
        self._splash_key: Optional[str] = None
        self._all_disabled = False
        self._featured = _FeaturedCard()
        self._featured.activated.connect(self._on_featured_activated)
        self._featured.hovered.connect(self._on_featured_hover)
        layout.addWidget(self._featured)
        add = partial(self._add_row, layout)
        add('_trending_games_row', 'home.dashboard_trending_games', lambda: self._goto('pc_games_page'), gate='pc_games_enabled')
        add('_recent_games_row', 'home.dashboard_recent_games', lambda: self._goto('pc_games_page'), gate='pc_games_enabled')
        add('_trending_tv_row', 'home.dashboard_trending_tv', lambda: self._goto('tv_page'), gate='tv_enabled')
        add('_trending_anime_row', 'home.dashboard_trending_anime', lambda: self._goto('tv_page', 'show_tab', 'anime'), gate='tv_enabled')
        add('_new_anime_row', 'home.dashboard_new_anime', lambda: self._goto('tv_page', 'show_tab', 'anime'), gate='tv_enabled')
        add('_new_manga_row', 'home.dashboard_new_manga', lambda: self._goto('books_page'), gate='books_enabled')
        add('_new_books_row', 'home.dashboard_new_books', lambda: self._goto('books_page'), gate='books_enabled')
        add('_playlists_row', 'home.dashboard_playlists_title', lambda: self._goto('music_page'), gate='music_enabled')
        layout.addStretch(1)
        self._empty_notice = TitleLabel('')
        self._empty_notice.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._empty_notice.setWordWrap(True)
        self._empty_notice.hide()
        layout.addWidget(self._empty_notice)
        layout.addStretch(1)
        self._splash_timer = _single_shot_timer(self, 0, self._position_splash)
        self._features_timer = QTimer(self)
        self._features_timer.setSingleShot(True)
        self._features_timer.timeout.connect(self._apply_features_update)
        tab_visibility_bus.changed.connect(self.on_features_changed)
        self._apply_locale()
        self._apply_theme_colors()
        register_locale_refresh(self, self._apply_locale)
        register_theme_refresh(self, self._apply_theme_colors)
        self.on_features_changed()
        app = QApplication.instance()
        if app is not None:
            app.applicationStateChanged.connect(self._on_app_state_changed)
        QTimer.singleShot(2500, self._end_boot)

    def _end_boot(self) -> None:
        self._booting = False
        self.refresh()

    def _add_row(self, layout, attr: str, title_key: str, see_all=None, height: int = 290, gate: Optional[str] = None) -> None:
        row = _CardRow(tr(title_key), see_all, height)
        layout.addWidget(row)
        setattr(self, attr, row)
        self._rows.append((row, title_key, gate))

    def showEvent(self, event) -> None:
        super().showEvent(event)
        self._rot_timer.start()
        if self._features_timer.isActive():
            self._features_timer.start(250)
        self._refresh_playlists()
        self.refresh()
        self._splash_timer.start()

    def hideEvent(self, event) -> None:
        self._rot_timer.stop()
        self._splash.stop_animation()
        try:
            images.trim(48 * 1024 * 1024)
        except Exception:
            pass
        super().hideEvent(event)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._splash_timer.start()

    def set_animation_active(self, active: bool) -> None:
        self._animation_active = bool(active)
        self._sync_splash_animation()

    def _on_app_state_changed(self, _state) -> None:
        self._sync_splash_animation()

    def _sync_splash_animation(self, *_a) -> None:
        app = QApplication.instance()
        focused = app is None or app.applicationState() == Qt.ApplicationState.ApplicationActive
        active = (self._animation_active and focused and not self.window().isMinimized()
                  and self.isVisible() and self._splash.isVisible()
                  and self._splash.geometry().bottom() > self._vbar.value())
        if active:
            self._splash.start_animation()
        else:
            self._splash.stop_animation()

    def _pick_splash(self) -> None:
        keys = [k for k in _splash_keys(current_language()) if k != 'home.splash.birthday']
        self._splash_key = 'home.splash.birthday' if _is_birthday() else (random.choice(keys) if keys else None)
        self._splash.setText(tr(self._splash_key) if self._splash_key else '')
        self._position_splash()

    def _position_splash(self) -> None:
        rect = self._welcome_lbl.geometry()
        text = self._splash.text() or ''
        if self._all_disabled or self._splash_key is None or rect.isEmpty() or not text:
            self._splash.setVisible(False)
            self._sync_splash_animation()
            return
        fm = self._welcome_lbl.fontMetrics()
        right = rect.center().x() + fm.horizontalAdvance(self._welcome_lbl.text()) // 2
        anchor_y = rect.center().y() + fm.height() // 2 - fm.descent()
        parent = self._splash.parentWidget()
        parent_w = parent.width() if parent is not None else self.width()
        right_room = max(80.0, parent_w - right - 20)
        self._splash.fit_width(min(parent_w - 24, right_room * 2))
        sfm = self._splash.fontMetrics()
        tw, th = sfm.horizontalAdvance(text), sfm.height()
        wobble = _SplashLabel._WOBBLE_DEGREES
        room = anchor_y - th / 2 - math.ceil(_SplashLabel._BOB) - 2
        s = _SplashLabel._MAX_SCALE
        fit = math.degrees(math.asin(max(0.0, min(1.0, room / (tw / 2 * s))))) - wobble
        base = max(4.0, min(abs(_SplashLabel._BASE_ANGLE), fit))
        self._splash.set_base_angle(-base)
        ang = math.radians(base + wobble)
        bw = (tw * math.cos(ang) + th * math.sin(ang)) * s
        bh = (tw * math.sin(ang) + th * math.cos(ang)) * s + 2 * _SplashLabel._BOB
        w, h = max(110, int(bw + 28)), max(56, int(bh + 24))
        cx, cy = right, anchor_y
        self._splash.setFixedSize(w, h)
        self._splash.move(int(cx - w / 2), int(cy - h / 2))
        self._splash.raise_()
        self._splash.setVisible(True)
        self._sync_splash_animation()

    def _candidates(self) -> list:
        return [(k, v) for k, v in self._cands.items() if _on(_KIND_META[k][4]) and is_tab_enabled(*_KIND_META[k][5])]

    def _set_cand(self, kind: str, title, poster, entry) -> None:
        if entry is None:
            self._cands.pop(kind, None)
        else:
            self._cands[kind] = (title, poster, entry)

    def _show_candidate(self, cands: list) -> None:
        kind, (title, poster, entry) = cands[self._rot_idx % len(cands)]
        eb_key, body_key, eb_fb, body_fb, _, _ = _KIND_META[kind]
        action = tr('home.dashboard_browse_pc_games') if kind == 'repack' else _tr_safe('home.dashboard_open_item', 'Open')
        self._featured.set_content('ref', _tr_safe(eb_key, eb_fb), title, _tr_safe(body_key, body_fb), action, poster, (kind, entry))

    def _on_featured_hover(self, on: bool) -> None:
        if not self.isVisible():
            return
        if on:
            self._rot_timer.stop()
        else:
            self._rot_timer.start()

    def _rotate(self) -> None:
        if not self.isVisible():
            return
        if len(self._candidates()) > 1:
            self._rot_idx += 1
            self.refresh()

    _UPDATES = ('_fetch_repacks', '_fetch_tv', '_fetch_anime', '_fetch_manga', '_fetch_books', '_refresh_playlists', 'refresh')

    def on_features_changed(self, *_args) -> None:
        self._gen += 1
        self._features_timer.start(0 if self.isVisible() else 700)

    def _apply_features_update(self) -> None:
        gen = self._gen
        for i, name in enumerate(self._UPDATES):
            QTimer.singleShot(i * 30, partial(self._run_update, name, gen))

    def _run_update(self, name: str, gen: int) -> None:
        if gen != self._gen:
            return
        try:
            getattr(self, name)()
        except Exception:
            logger.exception('Home update failed: %s', name)

    def _apply_locale(self, *_args) -> None:
        self._welcome_lbl.setText(tr('home.dashboard_welcome'))
        for row, key, _ in self._rows:
            row.title_lbl.setText(tr(key))
        self._empty_notice.setText(tr('home.dashboard_all_empty'))
        self._pick_splash()
        self._sigs.clear()
        self.refresh()

    def _apply_theme_colors(self, *_args) -> None:
        c = palette()
        self._welcome_lbl.setStyleSheet(f"color: {c['primary_text']};")
        self._splash.set_color(c.get('accent', '#ffd166'))

    def refresh(self) -> None:
        for row, _, gate in self._rows:
            if gate:
                row.set_enabled(_on(gate))
        cands = self._candidates()
        if cands:
            self._show_candidate(cands)
        elif not self._booting:
            self._featured.set_content('empty', tr('home.dashboard_featured_eyebrow'), tr('home.dashboard_no_featured_title'), tr('home.dashboard_no_featured_body'), tr('home.dashboard_browse_pc_games'))
        all_disabled = not any(_on(k) for k in ('pc_games_enabled', 'tv_enabled', 'books_enabled', 'music_enabled', 'minerva_enabled'))
        self._all_disabled = all_disabled
        self._welcome_lbl.setVisible(not all_disabled)
        self._featured.setVisible(not all_disabled)
        self._empty_notice.setVisible(all_disabled)
        self._position_splash()

    def _refresh_playlists(self) -> None:
        playlists = [p for p in _saved_playlists() if p.songs][:10] if _on('music_enabled') else []
        sig = tuple((p.id, p.name, len(p.songs)) for p in playlists)
        if self._sigs.get('playlists') == sig:
            return
        self._sigs['playlists'] = sig
        cards = []
        for p in playlists:
            card = _DownloadTileCard.from_playlist(p)
            card.set_image_path(next((getattr(s, 'cover_url', None) for s in p.songs if getattr(s, 'cover_url', None)), None))
            cards.append(card)
        self._playlists_row.set_cards(cards)

    def _page(self, name: str):
        window = self.window()
        page = getattr(window, name, None)
        return (window, page) if page is not None and hasattr(window, 'switchTo') else (None, None)

    def _goto(self, name: str, method: Optional[str] = None, *args):
        window, page = self._page(name)
        if page is None:
            return None
        window.switchTo(page)
        if method:
            page.call_when_ready(method, *args)
        return page

    def _open_tv(self, entry, kind: str) -> None:
        self._goto('tv_page', 'open_detail', entry, kind)

    def _open_anime(self, entry) -> None:
        self._goto('tv_page', 'open_anime_detail', entry)

    def _open_repack(self, entry, source: str) -> None:
        self._goto('pc_games_page', 'open_detail', entry, source)

    def _on_featured_activated(self, mode: str, payload) -> None:
        if mode == 'ref' and payload is not None:
            kind, entry = payload
            if kind == 'repack':
                self._open_repack(entry, 'fitgirl')
            elif kind in ('movie', 'show'):
                self._open_tv(entry, kind)
            elif kind == 'anime':
                self._open_anime(entry)
            else:
                self._goto('books_page', 'open_manga_detail', entry)
        else:
            self._goto('pc_games_page')

    def _repack_card(self, entry, source: str) -> _PosterCard:
        title = _clean_repack_title(getattr(entry, 'title', '') or tr('download.unknown'))
        card = _PosterCard(getattr(entry, 'url', title), title, provider=source)
        path = getattr(entry, 'poster_path', None)
        card.set_image_path(path if path and os.path.isfile(path) else getattr(entry, 'poster_url', None))
        card.on_click = lambda *_a, e=entry, s=source: self._open_repack(e, s)
        return card

    def _fetch_repacks(self) -> None:
        if not _on('pc_games_enabled'):
            return
        worker = _lazy('src.core.worker')
        if worker is None:
            return
        if is_tab_enabled('repacks', 'fitgirl'):
            worker.fetch_popular_repacks_async('fitgirl', on_done=self._on_popular, use_cache=True)
        else:
            self._popular = []
            self._trending_games_row.set_cards([])
        for source in ('fitgirl', 'gog'):
            if not is_tab_enabled('repacks', source):
                self._recent_games_row.set_cards([], source)
            else:
                worker.fetch_latest_repacks_async(source, on_done=partial(self._on_latest, source=source), use_cache=True)

    def _on_popular(self, entries) -> None:
        if not is_tab_enabled('repacks', 'fitgirl'):
            return
        self._popular = list(entries or [])
        first = self._popular[0] if self._popular else None
        if first is not None:
            poster = getattr(first, 'poster_path', None)
            if not (poster and os.path.isfile(poster)):
                poster = getattr(first, 'poster_url', None)
            self._set_cand('repack', _clean_repack_title(getattr(first, 'title', '') or tr('download.unknown')), poster, first)
        else:
            self._cands.pop('repack', None)
        self._trending_games_row.set_cards([self._repack_card(e, 'fitgirl') for e in self._popular[:12]])
        self.refresh()

    def _on_latest(self, entries, source: str = 'fitgirl') -> None:
        if not is_tab_enabled('repacks', source):
            return
        self._recent_games_row.set_cards([self._repack_card(e, source) for e in list(entries or [])[:12]], source)

    def _fetch_tv(self) -> None:
        if not _on('tv_enabled'):
            return
        if not (is_tab_enabled('tv', 'movies') or is_tab_enabled('tv', 'series')):
            self._trending_tv_row.set_cards([])
            return
        tmdb = _lazy('src.core.tv', 'tmdb')
        if tmdb is not None:
            tmdb.trending_async('all', 'week', on_done=self._on_tv, on_error=_ignore)

    def _on_tv(self, data) -> None:
        tmdb = _lazy('src.core.tv', 'tmdb')
        cards = []
        first = None
        for entry in (data or {}).get('results') or []:
            kind = 'movie' if entry.get('title') else 'show'
            if not is_tab_enabled('tv', 'movies' if kind == 'movie' else 'series'):
                continue
            title = entry.get('title') or entry.get('name') or tr('download.unknown')
            year = (entry.get('release_date') or entry.get('first_air_date') or '')[:4]
            card = _PosterCard(entry.get('id') or title, title, year)
            poster = entry.get('poster_path')
            url = tmdb.image_url(poster) if tmdb is not None and poster else None
            card.set_image_path(url)
            if first is None:
                first = (kind, title, url, entry)
            card.on_click = lambda *_a, e=entry, k=kind: self._open_tv(e, k)
            cards.append(card)
            if len(cards) >= 12:
                break
        self._trending_tv_row.set_cards(cards)
        self._cands.pop('movie', None)
        self._cands.pop('show', None)
        if first is not None:
            self._set_cand(first[0], first[1], first[2], first[3])
        self.refresh()

    def _fetch_anime(self) -> None:
        if not _on('tv_enabled'):
            self._anime_lists.clear()
            self._trending_anime_row.set_cards([])
            self._new_anime_row.set_cards([])
            return
        if not is_tab_enabled('tv', 'anime'):
            self._anime_lists.clear()
            self._trending_anime_row.set_cards([])
            self._new_anime_row.set_cards([])
            return
        backend = _lazy('src.core.tv.anime', 'anime_backend')
        worker = _lazy('src.core.worker')
        if backend is None or worker is None:
            return

        for which in ('trending', 'season'):
            worker.submit_coro(backend.browse_category, args=(which,),
                               on_done=partial(self._on_anime_list, which), on_error=_ignore)

    def _on_anime_list(self, which: str, results) -> None:
        if not is_tab_enabled('tv', 'anime'):
            return
        self._anime_lists[which] = list(results or [])
        trending = self._anime_lists.get('trending') or []
        trending_ids = {e.get('id') for e in trending}
        season = [e for e in (self._anime_lists.get('season') or []) if e.get('id') not in trending_ids]
        self._trending_anime_row.set_cards(self._anime_cards(trending, self._open_anime))
        self._new_anime_row.set_cards(self._anime_cards(season, self._open_anime))
        self._cands.pop('anime', None)
        if trending:
            first = trending[0]
            title = (first.get('title') or {}).get('english') or (first.get('title') or {}).get('romaji') \
                or (first.get('title') or {}).get('native') or tr('download.unknown')
            self._set_cand('anime', title, first.get('coverImage'), first)
        self.refresh()

    @staticmethod
    def _anime_cards(results, opener, limit: int = 12) -> list:
        cards = []
        for entry in (results or [])[:limit]:
            title = (entry.get('title') or {}).get('english') or (entry.get('title') or {}).get('romaji') \
                or (entry.get('title') or {}).get('native') or tr('download.unknown')
            meta = ' \u00b7 '.join(str(v) for v in (entry.get('format'), entry.get('seasonYear')) if v)
            card = _PosterCard(entry.get('id') or title, title, meta)
            card.set_image_path(entry.get('coverImage'))
            card.on_click = lambda *_a, e=entry: opener(e)
            cards.append(card)
        return cards

    def _entry_cards(self, result, cover_attr: str, opener, with_author: bool = False) -> list:
        cards = []
        for e in list(getattr(result, 'entries', None) or [])[:12]:
            title = getattr(e, 'title', '') or tr('download.unknown')
            card = _PosterCard(getattr(e, 'id', title), title, (getattr(e, 'author', '') or '') if with_author else '')
            card.set_image_path(getattr(e, cover_attr, None))
            card.on_click = lambda *_a, e=e: opener(e)
            cards.append(card)
        return cards

    def _fetch_browse(self, row: _CardRow, scope: str, manager, on_done, sources=None) -> None:
        if not _on('books_enabled'):
            return
        if not is_tab_enabled('books', scope):
            row.set_cards([])
            return
        worker = _lazy('src.core.worker')
        if manager is not None and worker is not None:
            kwargs = {'use_cache': True}
            if sources is not None:
                kwargs['sources'] = sources
            worker.submit(manager.browse_all, args=(1,), kwargs=kwargs, on_done=on_done, on_error=_ignore)

    def _fetch_manga(self) -> None:
        if not _on('books_enabled') or not is_tab_enabled('books', 'manga'):
            self._new_manga_row.set_cards([])
            return
        self._fetch_browse(self._new_manga_row, 'manga', _lazy('src.core.books.manga', 'manager'), self._on_manga)

    def _on_manga(self, result) -> None:
        self._new_manga_row.set_cards(self._entry_cards(result, 'cover_url', lambda e: self._goto('books_page', 'open_manga_detail', e)))
        entries = list(getattr(result, 'entries', None) or [])
        if entries:
            e = entries[0]
            self._set_cand('manga', getattr(e, 'title', '') or tr('download.unknown'), getattr(e, 'cover_url', None), e)
        else:
            self._cands.pop('manga', None)
        self.refresh()

    def _fetch_books(self) -> None:
        if not _on('books_enabled') or not is_tab_enabled('books', 'books'):
            self._new_books_row.set_cards([])
            return
        self._fetch_browse(self._new_books_row, 'books', _lazy('src.core.books', 'manager'), lambda r: self._new_books_row.set_cards(self._entry_cards(r, 'artwork_url', lambda e: self._goto('books_page', 'open_detail', e), True)), sources=['gutenberg'])


def _single_shot_timer(parent, interval_ms: int, slot):
    from PySide6.QtCore import QTimer
    timer = QTimer(parent)
    timer.setSingleShot(True)
    timer.setInterval(interval_ms)
    timer.timeout.connect(slot)
    return timer


class AccentColorMixin:
    def _default_button_color(self) -> QColor:
        return QColor(palette().get('accent', '#00b7c3'))

    def set_accent_color(self, color) -> None:
        self._current_accent_color = color
        self._reapply_accent_color()

    def _deferred_reapply_accent_color(self) -> None:
        QTimer.singleShot(0, self._reapply_accent_color)


class DetailsViewMixin:
    def _deferred_apply_site_link_style(self) -> None:
        QTimer.singleShot(0, self._apply_site_link_style)

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

    def _show_cover_fallback(self, title: str) -> None:
        self._cover_fallback_title_lbl.setText(title)
        self._cover_fallback.setVisible(True)


class MediaPageBase(QWidget):
    def _switch_stack_animated(self, widget) -> None:
        if self._stack.currentWidget() is widget:
            return
        old_group = getattr(self, '_stack_fade_anim', None)
        if old_group is not None:
            try:
                old_group.stop()
                try:
                    old_group.finished.disconnect()
                except Exception:
                    pass
            except Exception:
                pass
            for i in range(self._stack.count()):
                try:
                    page = self._stack.widget(i)
                    page.setGraphicsEffect(None)
                except Exception:
                    pass
            self._stack_fade_anim = None
        self._stack.setCurrentWidget(widget)
