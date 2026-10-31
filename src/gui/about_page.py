from __future__ import annotations

import logging
import os
import random
from collections import deque

from PySide6.QtCore import QElapsedTimer, QPointF, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import (
    QColor,
    QFont,
    QIcon,
    QPainter,
    QPainterPath,
    QPen,
    QPixmap,
)
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QSizePolicy, QVBoxLayout, QWidget
from qfluentwidgets import (
    BodyLabel,
    CaptionLabel,
    CardWidget,
    FluentIcon,
    HyperlinkButton,
    IconWidget,
    StrongBodyLabel,
    SubtitleLabel,
    TransparentToolButton,
)

from src.core.theme import (
    filled_icon,
    palette,
    register_theme_refresh,
    resolve_icon_color,
    themed_icon,
    tinted_icon,
    to_color,
)
from src.core.translations import register_locale_refresh, tr
from .assets import DISCORD_URL, PHOTO_DIR, find_logo_path
from .settings_page import make_smooth_scroll_area

logger = logging.getLogger(__name__)


_SERVICE_ROWS = (
    ('about.services_games', (
        ('FitGirl Repacks', 'https://fitgirl-repacks.site'),
        ('GOG', 'https://www.gog.com'),
        ('GOG-Revived', 'https://gog-rev.com'),
    )),
    ('about.services_books', (
        ('MangaDex', 'https://mangadex.org'),
        ('Gutendex', 'https://gutendex.com'),
        ('Project Gutenberg', 'https://www.gutenberg.org'),
        ('Library Genesis', 'https://libgen.li'),
    )),
    ('about.services_screen', (
        ('TMDB', 'https://www.themoviedb.org'),
        ('YouTube', 'https://www.youtube.com'),
        ('AniList', 'https://anilist.co'),
        ('Jikan', 'https://jikan.moe'),
        ('aniDB', 'https://anidb.net'),
        ('Kitsu', 'https://kitsu.app'),
        ('ani.zip', 'https://api.ani.zip'),
        ('TheIntroDB', 'https://theintrodb.org'),
        ('Torrentio', 'https://torrentio.strem.fun'),
        ('The Black Hole Bay', 'https://theblackholebay.lol'),
        ('The Pirate Bay', 'https://thepiratebay.org'),
        ('1337x', 'https://1337x.to'),
        ('IPTV-org', 'https://iptv-org.github.io'),
    )),
    ('about.services_anime', (
        ('AniBD', ''),
        ('AniDB.app', 'https://anidb.app'),
        ('AniKoto', 'https://anikototv.to'),
        ('AnimeDunya', 'https://anime-dunya.com'),
        ('AnimeGG', 'https://www.animegg.org'),
        ('AniNeko', 'https://anineko.to'),
        ('AniPub', 'https://www.anipub.org'),
        ('AniZone', 'https://anizone.to'),
        ('KickAssAnime', 'https://kaa.lt'),
        ('MegaPlay', 'https://megaplay.buzz'),
    )),
    ('about.services_music', (
        ('YouTube Music', 'https://music.youtube.com'),
        ('LRCLIB', 'https://lrclib.net'),
    )),
    ('about.services_roms', (
        ('Minerva Archive', 'https://cdn.minerva-archive.org'),
    )),
)

_PACKAGES = (
    'PySide6', 'PySide6-Fluent-Widgets', 'PySideSix-Frameless-Window',
    'libtorrent', 'yt-dlp', 'FFmpeg', 'requests', 'httpx', 'BeautifulSoup4',
    'libgen-api-enhanced', 'musicdl', 'ytmusicapi', 'mutagen', 'tinytag',
    'aigpy', 'm3u8', 'orjson', 'json-repair', 'puremagic', 'filetype',
    'pathvalidate', 'platformdirs', 'cryptography', 'pycryptodome',
    'pycryptodomex', 'pywidevine', 'rich', 'prettytable', 'tabulate',
    'prompt-toolkit', 'click', 'emoji', 'bleach', 'winrt-runtime',
    'winrt-Windows.Foundation', 'winrt-Windows.Media',
    'winrt-Windows.Media.Playback', 'winrt-Windows.Storage.Streams',
)


class _ClickableLabel(QLabel):
    clicked = Signal()

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton and self.rect().contains(event.position().toPoint()):
            self.clicked.emit()
        super().mouseReleaseEvent(event)


class _SnakeBoard(QWidget):
    closed = Signal()
    scoreChanged = Signal(int, int)
    gameOverChanged = Signal(bool)

    COLS = 22
    ROWS = 15
    BASE_INTERVAL = 112
    KEYS = {
        Qt.Key.Key_Up: (0, -1), Qt.Key.Key_W: (0, -1),
        Qt.Key.Key_Down: (0, 1), Qt.Key.Key_S: (0, 1),
        Qt.Key.Key_Left: (-1, 0), Qt.Key.Key_A: (-1, 0),
        Qt.Key.Key_Right: (1, 0), Qt.Key.Key_D: (1, 0),
    }

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setMinimumSize(320, 230)
        self._pirachest_owns_arrows = True
        self._timer = QTimer(self)
        self._timer.setTimerType(Qt.TimerType.CoarseTimer)
        self._timer.timeout.connect(self._tick)
        self._frame = QTimer(self)
        self._frame.setTimerType(Qt.TimerType.PreciseTimer)
        self._frame.setInterval(16)
        self._frame.timeout.connect(self._advance)
        self._clock = QElapsedTimer()
        self._old_tail = None
        self._prog = 1.0
        self._snake = deque()
        self._cells = set()
        self._queued = []
        self._dir = (1, 0)
        self._food = (0, 0)
        self._score = 0
        self._best = 0
        self._over = False
        self._paused = False
        self._bg = None
        self._reset()

    score = property(lambda self: self._score)
    best = property(lambda self: self._best)
    is_over = property(lambda self: self._over)

    def _run(self, on: bool) -> None:
        if on:
            self._clock.restart()
            self._timer.start()
            self._frame.start()
        else:
            self._timer.stop()
            self._frame.stop()

    def _advance(self) -> None:
        self._prog = min(1.0, self._clock.elapsed() / self._timer.interval())
        self.update(self._field()[0].adjusted(-2, -2, 2, 2).toAlignedRect())

    def start(self) -> None:
        self._reset()
        self._run(True)
        self.setFocus()

    def stop(self) -> None:
        self._run(False)
        self._paused = False
        self.update()

    def invalidate(self) -> None:
        self._bg = None
        self.update()

    def _reset(self) -> None:
        cx, cy = self.COLS // 2, self.ROWS // 2
        self._snake = deque((cx - i, cy) for i in range(3))
        self._cells = set(self._snake)
        self._dir = (1, 0)
        self._queued = []
        self._old_tail = None
        self._prog = 1.0
        self._score = 0
        self._over = self._paused = False
        self._timer.setInterval(self.BASE_INTERVAL)
        self._place_food()
        self.update()
        self.scoreChanged.emit(0, self._best)
        self.gameOverChanged.emit(False)

    def _place_food(self) -> None:
        while True:
            cell = (random.randrange(self.COLS), random.randrange(self.ROWS))
            if cell not in self._cells or len(self._cells) >= self.COLS * self.ROWS:
                self._food = cell
                return

    def _tick(self) -> None:
        if self._queued:
            self._dir = self._queued.pop(0)
        hx, hy = self._snake[0]
        head = (hx + self._dir[0], hy + self._dir[1])
        grow = head == self._food
        tail = self._snake[-1]
        if not (0 <= head[0] < self.COLS and 0 <= head[1] < self.ROWS) \
                or (head in self._cells and (grow or head != tail)):
            self._finish()
            return
        self._snake.appendleft(head)
        self._cells.add(head)
        self._clock.restart()
        self._prog = 0.0
        self._old_tail = None if grow else tail
        if grow:
            self._score += 1
            self._best = max(self._best, self._score)
            self._place_food()
            self._timer.setInterval(max(72, self.BASE_INTERVAL - self._score * 2))
            self.scoreChanged.emit(self._score, self._best)
        else:
            self._snake.pop()
            if tail != head:
                self._cells.discard(tail)
        self.update()

    def _finish(self) -> None:
        self._run(False)
        self._over = True
        self.gameOverChanged.emit(True)
        self.update()

    def keyPressEvent(self, event) -> None:
        key = event.key()
        if key == Qt.Key.Key_Escape:
            self.closed.emit()
        elif self._over and key in (Qt.Key.Key_Space, Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self.start()
        elif key in self.KEYS and not self._over:
            d = self.KEYS[key]
            last = self._queued[-1] if self._queued else self._dir
            if d != last and (d[0] + last[0], d[1] + last[1]) != (0, 0) and len(self._queued) < 2:
                self._queued.append(d)
                if self._prog > 0.5 and self._timer.isActive():
                    self._timer.start()
                    self._tick()
        else:
            super().keyPressEvent(event)
            return
        event.accept()

    def mousePressEvent(self, event) -> None:
        self.setFocus(Qt.FocusReason.MouseFocusReason)
        super().mousePressEvent(event)

    def focusInEvent(self, event) -> None:
        super().focusInEvent(event)
        if self._paused and not self._over:
            self._paused = False
            self._run(True)
        self.update()

    def focusOutEvent(self, event) -> None:
        super().focusOutEvent(event)
        if self._timer.isActive():
            self._run(False)
            self._paused = True
        self.update()

    def hideEvent(self, event) -> None:
        self.stop()
        super().hideEvent(event)

    def resizeEvent(self, event) -> None:
        self._bg = None
        super().resizeEvent(event)

    def _field(self) -> tuple[QRectF, float]:
        cell = min((self.width() - 24) / self.COLS, (self.height() - 24) / self.ROWS)
        w, h = cell * self.COLS, cell * self.ROWS
        return QRectF((self.width() - w) / 2, (self.height() - h) / 2, w, h), cell

    def _background(self, field: QRectF, cell: float) -> QPixmap:
        dpr = self.devicePixelRatioF()
        bg = QPixmap((self.size() * dpr))
        bg.setDevicePixelRatio(dpr)
        bg.fill(Qt.GlobalColor.transparent)
        pal = palette()
        surface = to_color(pal.get('section_card_bg', '#202020'), '#202020')
        tile = QColor(255, 255, 255, 7) if surface.lightness() < 128 else QColor(0, 0, 0, 9)
        p = QPainter(bg)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        clip = QPainterPath()
        clip.addRoundedRect(field, 12, 12)
        p.setClipPath(clip)
        p.fillRect(field, surface.darker(125) if surface.lightness() < 128 else surface.darker(106))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(tile)
        for r in range(self.ROWS):
            for c in range(r & 1, self.COLS, 2):
                p.drawRect(QRectF(field.x() + c * cell, field.y() + r * cell, cell, cell))
        p.end()
        return bg

    def paintEvent(self, event) -> None:
        field, cell = self._field()
        if cell <= 0:
            return
        if self._bg is None:
            self._bg = self._background(field, cell)
        accent = to_color(palette().get('accent', '#4f7cff'), '#4f7cff')
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.drawPixmap(0, 0, self._bg)
        ox, oy = field.x(), field.y()

        def center(c):
            return QPointF(ox + (c[0] + 0.5) * cell, oy + (c[1] + 0.5) * cell)

        food = center(self._food)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(255, 107, 107, 48))
        p.drawEllipse(food, cell * 0.5, cell * 0.5)
        p.setBrush(QColor('#ff6b6b'))
        p.drawEllipse(food, cell * 0.28, cell * 0.28)

        pts = [center(c) for c in self._snake]
        t = self._prog
        if len(pts) > 1:
            pts[0] = pts[1] + (pts[0] - pts[1]) * t
        if self._old_tail is not None:
            tail = center(self._old_tail)
            pts.append(tail + (pts[-1] - tail) * t)
        path = QPainterPath(pts[0])
        for pt in pts[1:]:
            path.lineTo(pt)
        p.setPen(QPen(accent, cell * 0.68, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawPath(path)

        head = pts[0]
        dx, dy = self._dir
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(12, 14, 20))
        for s in (-1, 1):
            p.drawEllipse(QPointF(head.x() + dx * cell * 0.14 - dy * s * cell * 0.17,
                                  head.y() + dy * cell * 0.14 + dx * s * cell * 0.17),
                          cell * 0.07, cell * 0.07)

        if self._over or self._paused:
            clip = QPainterPath()
            clip.addRoundedRect(field, 12, 12)
            p.fillPath(clip, QColor(0, 0, 0, 120))
            font = QFont(self.font())
            font.setPixelSize(max(14, int(cell * 1.1)))
            font.setWeight(QFont.Weight.DemiBold)
            p.setFont(font)
            p.setPen(QColor(255, 255, 255, 235))
            text = tr('about.snake_over_title', 'Game over') if self._over else tr('about.snake_paused', 'Paused')
            p.drawText(field, Qt.AlignmentFlag.AlignCenter, text)


class _SnakeOverlay(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent, True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self._pirachest_owns_arrows = True
        self._shot = None
        self._card = QFrame(self)
        self._card.setObjectName('piraSnakeCard')
        layout = QVBoxLayout(self._card)
        layout.setContentsMargins(22, 18, 22, 18)
        layout.setSpacing(14)

        header = QHBoxLayout()
        header.setSpacing(10)
        self._title = SubtitleLabel('', self._card)
        self._best_lbl = CaptionLabel('', self._card)
        self._close = TransparentToolButton(FluentIcon.CLOSE, self._card)
        self._close.setFixedSize(30, 30)
        self._close.clicked.connect(self.close_game)
        header.addWidget(self._title)
        header.addStretch(1)
        header.addWidget(self._best_lbl)
        header.addWidget(self._close)
        layout.addLayout(header)

        self._board = _SnakeBoard(self._card)
        self._board.closed.connect(self.close_game)
        self._board.scoreChanged.connect(self._on_score)
        self._board.gameOverChanged.connect(self._on_game_over)
        layout.addWidget(self._board, 1)

        footer = QHBoxLayout()
        self._score_lbl = StrongBodyLabel('', self._card)
        self._hint = CaptionLabel('', self._card)
        footer.addWidget(self._score_lbl)
        footer.addStretch(1)
        footer.addWidget(self._hint)
        layout.addLayout(footer)
        self.hide()

    def open_game(self) -> None:
        parent = self.parentWidget()
        if parent is not None:
            self.setGeometry(parent.rect())
            shot = parent.grab()
            p = QPainter(shot)
            p.fillRect(shot.rect(), QColor(0, 0, 0, 150))
            p.end()
            self._shot = shot
        self.show()
        self.raise_()
        self._board.start()

    def close_game(self) -> None:
        self._board.stop()
        self.hide()
        parent = self.parentWidget()
        if parent is not None:
            parent.setFocus()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        w = max(340, min(660, self.width() - 48))
        h = max(360, min(520, self.height() - 48))
        self._card.setGeometry((self.width() - w) // 2, (self.height() - h) // 2, w, h)

    def paintEvent(self, event) -> None:
        p = QPainter(self)
        if self._shot is None:
            p.fillRect(event.rect(), QColor(20, 20, 20))
        else:
            p.drawPixmap(self.rect(), self._shot)

    def mousePressEvent(self, event) -> None:
        if self._card.geometry().contains(event.position().toPoint()):
            super().mousePressEvent(event)
        else:
            self.close_game()
            event.accept()

    def keyPressEvent(self, event) -> None:
        if event.key() == Qt.Key.Key_Escape:
            self.close_game()
            event.accept()
        else:
            super().keyPressEvent(event)

    def showEvent(self, event) -> None:
        super().showEvent(event)
        self._board.setFocus(Qt.FocusReason.OtherFocusReason)

    def _on_score(self, score: int, best: int) -> None:
        self._score_lbl.setText(tr('about.snake_score', 'Score {score}', score=score))
        self._best_lbl.setText(tr('about.snake_best', 'Best {score}', score=best))

    def _on_game_over(self, over: bool) -> None:
        self._hint.setText(tr('about.snake_over', 'Game over · Space to restart') if over
                           else tr('about.snake_hint', 'Arrows / WASD · Esc to exit'))

    def retranslate(self) -> None:
        self._title.setText(tr('about.snake_title', 'PiraSnake'))
        self._on_score(self._board.score, self._board.best)
        self._on_game_over(self._board.is_over)

    def refresh_theme(self) -> None:
        c = palette()
        primary = c.get('primary_text', '#ffffff')
        muted = c.get('muted', '#999999')
        self._card.setStyleSheet(
            f'#piraSnakeCard {{ background-color: {c.get("section_card_bg", "#202020")};'
            f' border: 1px solid {c.get("card_border", "#444444")}; border-radius: 18px; }}')
        self._title.setStyleSheet(f'font-size: 17px; font-weight: 600; color: {primary};')
        self._score_lbl.setStyleSheet(f'color: {c.get("accent", "#4f7cff")}; font-size: 20px; font-weight: 700;')
        self._best_lbl.setStyleSheet(f'color: {muted};')
        self._hint.setStyleSheet(f'color: {muted};')
        self._board.invalidate()


class AboutPage(QWidget):
    GITHUB_URL = 'https://github.com/TheJustRun/PiraChest'
    AUTHOR_NAME = 'JustRun'
    AUTHOR_URL = 'https://github.com/TheJustRun'
    LOGO_ARTIST_REDDIT_USER = 'u/spicysaltysparty'
    LOGO_ARTIST_URL = 'https://reddit.com/u/spicysaltysparty'

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setLayoutDirection(Qt.LayoutDirection.LeftToRight)
        self.setStyleSheet('background: transparent;')
        self._logo_clicks = 0
        self._version_clicks = 0
        self._logo_timer = QTimer(self)
        self._logo_timer.setSingleShot(True)
        self._logo_timer.timeout.connect(self._reset_logo_clicks)
        self._version_timer = QTimer(self)
        self._version_timer.setSingleShot(True)
        self._version_timer.timeout.connect(self._reset_version_clicks)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        scroll = make_smooth_scroll_area(self)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setStyleSheet('QScrollArea { background: transparent; border: none; }')
        scroll.viewport().setStyleSheet('background: transparent;')
        outer.addWidget(scroll)
        container = QWidget()
        container.setStyleSheet('background: transparent;')
        scroll.setWidget(container)
        layout = QVBoxLayout(container)
        layout.setContentsMargins(24, 20, 28, 24)
        layout.setSpacing(12)

        from src.core.updater import __version__ as _about_version
        self._about_version = _about_version

        self._hero_card = self._build_hero_card()
        layout.addWidget(self._hero_card)

        self._credits_title_lbl = StrongBodyLabel('')
        self._credits_title_lbl.setLayoutDirection(Qt.LayoutDirection.LeftToRight)
        self._credits_title_lbl.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        layout.addWidget(self._credits_title_lbl)

        self._credit_icons: list[tuple[IconWidget, object]] = []
        layout.addWidget(self._build_author_card())
        layout.addWidget(self._build_thanks_card())
        layout.addWidget(self._build_services_card())
        layout.addWidget(self._build_packages_card())
        layout.addStretch(1)

        self._snake_overlay = _SnakeOverlay(self)
        self._apply_locale()
        register_locale_refresh(self, self._apply_locale)
        register_theme_refresh(self, self._apply_theme_colors)

    def _build_hero_card(self) -> QWidget:
        card = CardWidget()
        card.setMinimumHeight(154)
        layout = QHBoxLayout(card)
        layout.setContentsMargins(24, 20, 24, 20)
        layout.setSpacing(18)

        logo_size = 76
        logo = _ClickableLabel()
        logo.setFixedSize(logo_size, logo_size)
        logo.setAlignment(Qt.AlignmentFlag.AlignCenter)
        logo.setCursor(Qt.CursorShape.ArrowCursor)
        logo.clicked.connect(self._on_logo_clicked)
        self._logo_label = logo
        self._logo_path = find_logo_path()
        layout.addWidget(logo, 0, Qt.AlignmentFlag.AlignVCenter)

        text_col = QVBoxLayout()
        text_col.setSpacing(5)
        text_col.setContentsMargins(0, 0, 0, 0)
        title_row = QHBoxLayout()
        title_row.setSpacing(10)
        self._hero_title_lbl = SubtitleLabel('PiraChest')
        title_row.addWidget(self._hero_title_lbl, 0)
        self._version_lbl = _ClickableLabel('')
        self._version_lbl.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        self._version_lbl.clicked.connect(self._on_version_clicked)
        title_row.addWidget(self._version_lbl, 0, Qt.AlignmentFlag.AlignVCenter)
        title_row.addStretch(1)
        text_col.addLayout(title_row)

        self._desc_lbl = BodyLabel('')
        self._desc_lbl.setWordWrap(True)
        self._desc_lbl.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        text_col.addWidget(self._desc_lbl)

        self._secret_hint = CaptionLabel('↑ ↑ ↓ ↓ ← → ← → B A')
        self._secret_hint.setVisible(False)
        text_col.addWidget(self._secret_hint)

        links = QHBoxLayout()
        links.setSpacing(8)
        self._github_btn = HyperlinkButton(self.GITHUB_URL, '')
        self._discord_btn = HyperlinkButton(DISCORD_URL, '')
        links.addWidget(self._github_btn, 0)
        links.addWidget(self._discord_btn, 0)
        links.addStretch(1)
        text_col.addLayout(links)
        layout.addLayout(text_col, 1)
        return card

    def _build_author_card(self) -> QWidget:
        card = CardWidget()
        card.setMinimumHeight(64)
        layout = QHBoxLayout(card)
        layout.setContentsMargins(16, 10, 14, 10)
        layout.setSpacing(12)
        layout.setAlignment(Qt.AlignmentFlag.AlignVCenter)
        self._author_avatar = QLabel()
        self._author_avatar.setFixedSize(40, 40)
        layout.addWidget(self._author_avatar, 0, Qt.AlignmentFlag.AlignVCenter)
        text_col = QVBoxLayout()
        text_col.setSpacing(2)
        text_col.setContentsMargins(0, 0, 0, 0)
        self._author_title_lbl = StrongBodyLabel('')
        self._author_role_lbl = CaptionLabel('')
        text_col.addWidget(self._author_title_lbl)
        text_col.addWidget(self._author_role_lbl)
        layout.addLayout(text_col, 1)
        self._profile_btn = HyperlinkButton(self.AUTHOR_URL, '')
        layout.addWidget(self._profile_btn, 0, Qt.AlignmentFlag.AlignVCenter)
        return card

    def _build_thanks_card(self) -> QWidget:
        card = CardWidget()
        layout = QHBoxLayout(card)
        layout.setContentsMargins(16, 12, 16, 12)
        layout.setSpacing(12)
        layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        icon = IconWidget(QIcon(), card)
        icon.setFixedSize(22, 22)
        self._credit_icons.append((icon, filled_icon(FluentIcon.HEART)))
        layout.addWidget(icon, 0, Qt.AlignmentFlag.AlignTop)
        text_col = QVBoxLayout()
        text_col.setSpacing(4)
        text_col.setContentsMargins(0, 0, 0, 0)
        self._thanks_title_lbl = StrongBodyLabel('')
        self._thanks_body_lbl = BodyLabel('')
        self._thanks_body_lbl.setWordWrap(True)
        self._thanks_body_lbl.setTextFormat(Qt.TextFormat.RichText)
        self._thanks_body_lbl.setOpenExternalLinks(True)
        self._thanks_body_lbl.setTextInteractionFlags(Qt.TextInteractionFlag.TextBrowserInteraction)
        text_col.addWidget(self._thanks_title_lbl)
        text_col.addWidget(self._thanks_body_lbl)
        layout.addLayout(text_col, 1)
        return card

    def _build_services_card(self) -> QWidget:
        card = CardWidget()
        layout = QHBoxLayout(card)
        layout.setContentsMargins(16, 12, 16, 12)
        layout.setSpacing(12)
        layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        icon = IconWidget(QIcon(), card)
        icon.setFixedSize(22, 22)
        self._credit_icons.append((icon, filled_icon(FluentIcon.GLOBE)))
        layout.addWidget(icon, 0, Qt.AlignmentFlag.AlignTop)
        text_col = QVBoxLayout()
        text_col.setSpacing(4)
        text_col.setContentsMargins(0, 0, 0, 0)
        self._services_title_lbl = StrongBodyLabel('')
        self._services_body_lbl = BodyLabel('')
        self._services_body_lbl.setWordWrap(True)
        self._services_body_lbl.setTextFormat(Qt.TextFormat.RichText)
        self._services_body_lbl.setOpenExternalLinks(True)
        self._services_body_lbl.setTextInteractionFlags(Qt.TextInteractionFlag.TextBrowserInteraction)
        text_col.addWidget(self._services_title_lbl)
        text_col.addWidget(self._services_body_lbl)
        layout.addLayout(text_col, 1)
        return card

    def _build_packages_card(self) -> QWidget:
        card = CardWidget()
        layout = QHBoxLayout(card)
        layout.setContentsMargins(16, 12, 16, 12)
        layout.setSpacing(12)
        layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        icon = IconWidget(QIcon(), card)
        icon.setFixedSize(22, 22)
        self._credit_icons.append((icon, filled_icon(FluentIcon.CODE)))
        layout.addWidget(icon, 0, Qt.AlignmentFlag.AlignTop)
        text_col = QVBoxLayout()
        text_col.setSpacing(4)
        text_col.setContentsMargins(0, 0, 0, 0)
        self._packages_title_lbl = StrongBodyLabel('')
        self._packages_body_lbl = CaptionLabel('')
        self._packages_body_lbl.setWordWrap(True)
        text_col.addWidget(self._packages_title_lbl)
        text_col.addWidget(self._packages_body_lbl)
        layout.addLayout(text_col, 1)
        return card

    def _services_html(self) -> str:
        c = palette()
        accent = c.get('accent', '#4f7cff')
        primary = c.get('primary_text', '#ffffff')
        rows = []
        for key, services in _SERVICE_ROWS:
            names = []
            for name, url in services:
                if url:
                    names.append(f'<a href="{url}" style="color:{accent}; text-decoration:none;">{name}</a>')
                else:
                    names.append(name)
            rows.append(f'<span style="color:{primary}; font-weight:600;">{tr(key)}</span> '
                        + ', '.join(names))
        return '<br/>'.join(rows)

    def _thanks_html(self) -> str:
        accent = palette().get('accent', '#4f7cff')
        logo = tr('about.logo_credit', name=(
            f'<a href="{self.LOGO_ARTIST_URL}" style="color:{accent}; text-decoration:none;">'
            f'{self.LOGO_ARTIST_REDDIT_USER}</a>'))
        return '<br/>'.join((logo, tr('about.reze_credit_content'),
                             tr('about.mangadex_credit_content')))

    def _packages_html(self) -> str:
        return ' · '.join(_PACKAGES)

    def _on_logo_clicked(self) -> None:
        if self._snake_overlay.isVisible():
            return
        self._logo_clicks += 1
        self._logo_timer.start(2500)
        if self._logo_clicks >= 10:
            self._reset_logo_clicks()
            self._snake_overlay.open_game()

    def _reset_logo_clicks(self) -> None:
        self._logo_clicks = 0
        self._logo_timer.stop()

    def _on_version_clicked(self) -> None:
        self._version_clicks += 1
        self._version_timer.start(1800)
        if self._version_clicks >= 3:
            self._reset_version_clicks()
            self._secret_hint.setVisible(True)

    def _reset_version_clicks(self) -> None:
        self._version_clicks = 0
        self._version_timer.stop()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        overlay = getattr(self, '_snake_overlay', None)
        if overlay is not None:
            overlay.setGeometry(self.rect())

    def hideEvent(self, event) -> None:
        overlay = getattr(self, '_snake_overlay', None)
        if overlay is not None and overlay.isVisible():
            overlay.close_game()
        super().hideEvent(event)

    def _apply_logo(self) -> None:
        if self._logo_path:
            pixmap = QPixmap(self._logo_path)
            if not pixmap.isNull():
                self._logo_label.setPixmap(pixmap.scaled(
                    76, 76, Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation))
                return
        self._logo_label.setPixmap(themed_icon('about.app', FluentIcon.APPLICATION, size=76).pixmap(76, 76))

    def _build_author_avatar_pixmap(self) -> QPixmap:
        from PySide6.QtGui import QPainterPath, QColor as _QColor
        size = 40
        avatar_path = None
        candidate = os.path.join(PHOTO_DIR, 'author.png')
        if os.path.isfile(candidate):
            avatar_path = candidate
        canvas = QPixmap(size, size)
        canvas.fill(Qt.GlobalColor.transparent)
        painter = QPainter(canvas)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        clip_path = QPainterPath()
        clip_path.addEllipse(0, 0, size, size)
        painter.setClipPath(clip_path)
        if avatar_path:
            src = QPixmap(avatar_path)
            if not src.isNull():
                src = src.scaled(size, size, Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                                 Qt.TransformationMode.SmoothTransformation)
                painter.drawPixmap((size - src.width()) // 2, (size - src.height()) // 2, src)
            else:
                avatar_path = None
        if not avatar_path:
            painter.setPen(_QColor(palette()['muted']))
            font = painter.font()
            font.setPointSize(15)
            font.setBold(True)
            painter.setFont(font)
            initial = (self.AUTHOR_NAME.strip()[:1] or '?').upper()
            painter.drawText(canvas.rect(), Qt.AlignmentFlag.AlignCenter, initial)
        painter.end()
        return canvas

    def _apply_locale(self) -> None:
        self._version_lbl.setText(tr('about.version', version=self._about_version))
        self._desc_lbl.setText(tr('about.description'))
        self._github_btn.setText(tr('about.open_github'))
        self._discord_btn.setText(tr('about.open_discord'))
        self._credits_title_lbl.setText(tr('about.credits_group'))
        self._author_title_lbl.setText(tr('about.made_by', name=self.AUTHOR_NAME))
        self._author_role_lbl.setText(tr('about.developer_role'))
        self._profile_btn.setText(tr('about.profile'))
        self._thanks_title_lbl.setText(tr('about.special_thanks'))
        self._thanks_body_lbl.setText(self._thanks_html())
        self._services_title_lbl.setText(tr('about.services_title', 'Services & sources'))
        self._services_body_lbl.setText(self._services_html())
        self._packages_title_lbl.setText(tr('about.packages_title', 'Built with'))
        self._packages_body_lbl.setText(self._packages_html())
        self._snake_overlay.retranslate()

    def _apply_theme_colors(self) -> None:
        c = palette()
        primary = c.get('primary_text', '#ffffff')
        body = c.get('body_text', primary)
        muted = c.get('muted', '#999999')
        self._apply_logo()
        self._author_avatar.setPixmap(self._build_author_avatar_pixmap())
        self._hero_title_lbl.setStyleSheet(f'font-size: 23px; font-weight: 700; color: {primary};')
        self._version_lbl.setStyleSheet(f'color: {muted}; font-size: 12px; padding-top: 4px;')
        self._desc_lbl.setStyleSheet(f'color: {body}; font-size: 14px;')
        self._secret_hint.setStyleSheet(f'color: {muted}; font-size: 12px; letter-spacing: 2px;')
        self._credits_title_lbl.setStyleSheet(f'font-size: 16px; font-weight: 600; color: {primary};')
        for label in (self._author_title_lbl, self._thanks_title_lbl,
                      self._services_title_lbl, self._packages_title_lbl):
            label.setStyleSheet(f'color: {primary};')
        for label in (self._author_role_lbl, self._packages_body_lbl):
            label.setStyleSheet(f'color: {muted};')
        self._thanks_body_lbl.setStyleSheet(f'color: {muted}; font-size: 13px;')
        self._services_body_lbl.setStyleSheet(f'color: {muted}; font-size: 13px;')
        self._thanks_body_lbl.setText(self._thanks_html())
        self._services_body_lbl.setText(self._services_html())
        for icon, mask in self._credit_icons:
            icon.setIcon(tinted_icon(mask, resolve_icon_color(), 22))
        self._snake_overlay.refresh_theme()
