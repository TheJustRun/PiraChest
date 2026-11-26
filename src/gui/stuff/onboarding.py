from __future__ import annotations

import math
import os
import sys
from contextlib import suppress
from dataclasses import dataclass, field
from typing import Callable

from PySide6.QtCore import (
    Property, QEasingCurve, QEvent, QObject, QPointF, QPropertyAnimation, QRect, QRectF, QRunnable, QSize, Qt,
    QThreadPool, QTimer, QVariantAnimation, Signal,
)
from PySide6.QtGui import (
    QBrush, QColor, QFont, QFontMetrics, QIcon, QImage, QImageReader, QLinearGradient, QPainter, QPainterPath,
    QPen, QPixmap,
)
from PySide6.QtWidgets import (
    QAbstractButton, QApplication, QDialog, QFileDialog, QFrame, QGraphicsOpacityEffect, QHBoxLayout, QLabel,
    QLineEdit, QScrollArea, QSizePolicy, QVBoxLayout, QWidget,
)
from qfluentwidgets import FluentIcon, Slider

from src.core.config import (
    DOWNLOAD_DIR_SUBFOLDERS, ThemeMode, apply_settings, check_dir_writable, download_dir_changes, format_size,
    free_space_bytes, get_launch_at_startup, save_settings, set_launch_at_startup, settings,
)
from src.core.theme import (
    activate_theme, all_preset_ids, animations_enabled, palette, preset_label, preset_meta,
    register_theme_refresh, resolve_font_families, themed_icon, to_color,
)
from src.core.translations import current_language, register_locale_refresh, set_language, tr, tr_for

from ..settings_page import apply_theme_font_families, get_sound_manager


LANGUAGES = (
    ('en', 'English'),
    ('ar', 'العربية'),
    ('es', 'Español'),
    ('ru', 'Русский'),
    ('de', 'Deutsch'),
)

FEATURES = (
    ('minerva_enabled', 'nav.minerva', 'onboarding.feature_minerva_desc'),
    ('pc_games_enabled', 'nav.pc_games', 'onboarding.feature_pc_games_desc'),
    ('youtube_enabled', 'nav.youtube', 'onboarding.feature_youtube_desc'),
    ('tv_enabled', 'nav.tv', 'onboarding.feature_tv_desc'),
    ('tbhb_enabled', 'nav.tbhb', 'onboarding.feature_tbhb_desc'),
    ('music_enabled', 'nav.music', 'onboarding.feature_music_desc'),
    ('books_enabled', 'nav.books', 'onboarding.feature_books_desc'),
    ('sailseek_enabled', 'nav.sailseek', 'onboarding.feature_sailseek_desc'),
)

_UI_FAMILIES = ('Segoe UI Variable Text', 'Segoe UI', 'Inter', 'SF Pro Text', 'Noto Sans',
                'Helvetica Neue', 'Arial')
_PLAIN_THEMES = ('dark', 'light')
_SWATCH_KEYS = ('page_bg', 'primary_text', 'accent')
_VIDEO_EXT = frozenset(('.mp4', '.webm', '.mkv', '.mov', '.avi', '.m4v'))
_PREVIEW_PX = QSize(320, 180)
_THUMBS: dict[str, QPixmap] = {}
_THUMB_BADGE = {'gif': 'GIF', 'video': 'VIDEO', 'shader': 'SHADER', 'photo': 'PHOTO'}


def _mix(a: QColor, b: QColor, t: float) -> QColor:
    t = max(0.0, min(1.0, float(t)))
    return QColor(
        round(a.red() + (b.red() - a.red()) * t),
        round(a.green() + (b.green() - a.green()) * t),
        round(a.blue() + (b.blue() - a.blue()) * t),
        round(a.alpha() + (b.alpha() - a.alpha()) * t),
    )


def _css(color) -> str:
    c = to_color(color)
    if c.alpha() < 255:
        return f'rgba({c.red()}, {c.green()}, {c.blue()}, {c.alpha() / 255:.3f})'
    return c.name()


def _font_px(base: QFont, px: int, weight: QFont.Weight | None = None) -> QFont:
    font = QFont(base)
    font.setPixelSize(max(9, int(px)))
    if weight is not None:
        font.setWeight(weight)
    return font


def _paint_icon_tile(p: QPainter, icon: QIcon, rect: QRectF, accent: QColor, strength: float) -> None:
    tile = QColor(accent)
    tile.setAlphaF(max(0.0, min(1.0, strength)))
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(tile)
    p.drawRoundedRect(rect, rect.width() * 0.28, rect.width() * 0.28)
    if not icon.isNull():
        inset = rect.width() * 0.2
        icon.paint(p, rect.adjusted(inset, inset, -inset, -inset).toRect(), Qt.AlignmentFlag.AlignCenter)


def _height_for_width(widget: QWidget, width: int) -> int:
    width = max(1, int(width))
    value = widget.heightForWidth(width)
    if value and value > 0:
        return int(value)
    layout = widget.layout()
    if layout is not None and layout.hasHeightForWidth():
        value = layout.totalHeightForWidth(width)
        if value and value > 0:
            return int(value)
    return max(widget.sizeHint().height(), widget.minimumSizeHint().height())


def _text_height(fm: QFontMetrics, text: str, width: int) -> int:
    if not text:
        return 0
    flags = int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop | Qt.TextFlag.TextWordWrap)
    rect = fm.boundingRect(QRect(0, 0, max(1, int(width)), 100000), flags, text)
    return max(fm.height(), rect.height() + 1)


class _Tweens:

    def _init_tweens(self, **values: float) -> None:
        self._vals = values
        self._tweens: dict[str, QVariantAnimation] = {}

    def _tween(self, key: str, target: float, duration: int = 140) -> None:
        anim = self._tweens.get(key)
        if anim is not None:
            anim.stop()
        if not self.isVisible() or not animations_enabled():
            self._vals[key] = float(target)
            self.update()
            return
        if anim is None:
            anim = self._tweens[key] = QVariantAnimation(self)
            anim.setEasingCurve(QEasingCurve.Type.OutCubic)
            anim.valueChanged.connect(lambda value, k=key: self._on_tween(k, value))
        anim.setDuration(duration)
        anim.setStartValue(self._vals[key])
        anim.setEndValue(float(target))
        anim.start()

    def _on_tween(self, key: str, value) -> None:
        self._vals[key] = float(value)
        self.update()

    def enterEvent(self, event) -> None:
        self._tween('hover', 1.0)
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:
        self._tween('hover', 0.0)
        super().leaveEvent(event)


class _ThumbJob(QRunnable):

    def __init__(self, path: str, loader: '_ThumbLoader'):
        super().__init__()
        self._path = path
        self._loader = loader

    def run(self) -> None:
        reader = QImageReader(self._path)
        reader.setAutoTransform(True)
        size = reader.size()
        if size.isValid():
            reader.setScaledSize(size.scaled(_PREVIEW_PX, Qt.AspectRatioMode.KeepAspectRatioByExpanding))
        image = reader.read()
        self._loader.done.emit(self._path, image)


class _ThumbLoader(QObject):
    done = Signal(str, QImage)
    ready = Signal(str)
    _instance: '_ThumbLoader | None' = None

    def __init__(self):
        super().__init__(QApplication.instance())
        self._pending: set[str] = set()
        self._pool = QThreadPool(self)
        self._pool.setMaxThreadCount(2)
        self.done.connect(self._on_done)

    @classmethod
    def get(cls) -> '_ThumbLoader':
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def request(self, path: str) -> None:
        if path not in _THUMBS and path not in self._pending:
            self._pending.add(path)
            self._pool.start(_ThumbJob(path, self))

    def _on_done(self, path: str, image: QImage) -> None:
        self._pending.discard(path)
        if not image.isNull():
            _THUMBS[path] = QPixmap.fromImage(image)
            self.ready.emit(path)


@dataclass(frozen=True)
class _Metrics:
    compact: bool
    base_px: int
    title_px: int
    subtitle_px: int
    caption_px: int
    button_px: int
    side: int
    top: int
    bottom: int
    gap: int
    section_gap: int
    radius: int
    progress_h: int
    header_bottom: int
    title_gap: int
    footer_top: int
    footer_bottom: int
    button_h: int
    input_h: int
    icon_px: int
    card_pad: int
    lang_h: int
    theme_min_col: int
    feature_min_col: int
    grid_gap: int
    theme_gap: int
    theme_pad: int
    page_top: int
    page_bottom: int


def _make_metrics(font: QFont, compact: bool) -> _Metrics:
    fm = QFontMetrics(font)
    base = max(14, round(fm.height() * 0.78))
    return _Metrics(
        compact=compact,
        base_px=base,
        title_px=round(base * (1.5 if compact else 1.82)),
        subtitle_px=base + 1,
        caption_px=max(12, base - 1),
        button_px=base + 1,
        side=20 if compact else 28,
        top=16 if compact else 22,
        bottom=10 if compact else 14,
        gap=8 if compact else 10,
        section_gap=12 if compact else 16,
        radius=14 if compact else 18,
        progress_h=4,
        header_bottom=10 if compact else 14,
        title_gap=3,
        footer_top=10 if compact else 12,
        footer_bottom=14 if compact else 18,
        button_h=max(32, round(base * 2.55)),
        input_h=max(34, round(base * 2.7)),
        icon_px=max(28, round(base * 2.15)),
        card_pad=max(10, round(base * 0.9)),
        lang_h=max(44, round(base * 3.5)),
        theme_min_col=max(108, round(base * 8.2)),
        feature_min_col=max(232, round(base * 17.5)),
        grid_gap=8 if compact else 10,
        theme_gap=8 if compact else 10,
        theme_pad=6 if compact else 7,
        page_top=2,
        page_bottom=8,
    )


_LABEL_CAPTION = ('caption_px', None, ('muted',), '#9a9a9a')
_LABEL_STYLES = {
    'title': ('title_px', QFont.Weight.DemiBold, ('dialog_title_text', 'primary_text'), '#eaeaea'),
    'subtitle': ('subtitle_px', None, ('muted',), '#9a9a9a'),
    'step': _LABEL_CAPTION,
    'section': ('subtitle_px', QFont.Weight.Bold, ('dialog_title_text', 'primary_text'), '#eaeaea'),
    'error': ('caption_px', None, ('state_error',), '#e5484d'),
    'accent': ('caption_px', None, ('accent_text', 'accent'), '#0078d4'),
    'body': ('base_px', None, ('primary_text',), '#eaeaea'),
}


class _Label(QLabel):

    def __init__(self, text: str, role: str, metrics: _Metrics, parent=None):
        super().__init__(text, parent)
        self._role = role
        self._metrics = metrics
        self.setTextFormat(Qt.TextFormat.PlainText)
        self.setWordWrap(role != 'step')
        self.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        self.setMinimumWidth(0)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self._apply()

    def set_role(self, role: str) -> None:
        self._role = role
        self._apply()

    def refresh_theme(self) -> None:
        self._apply()

    def _apply(self) -> None:
        size_attr, weight, color_keys, fallback = _LABEL_STYLES.get(self._role, _LABEL_CAPTION)
        pal = palette()
        color = next((pal[k] for k in color_keys if k in pal), fallback)
        self.setFont(_font_px(self.font(), getattr(self._metrics, size_attr), weight))
        self.setStyleSheet(f'color: {_css(color)}; background: transparent; border: none; padding: 0;')

    def heightForWidth(self, width: int) -> int:
        if not self.wordWrap():
            return self.sizeHint().height()
        return super().heightForWidth(max(1, int(width)))


class _Button(_Tweens, QAbstractButton):

    def __init__(self, text: str, kind: str, metrics: _Metrics, parent=None):
        super().__init__(parent)
        self._kind = kind
        self._metrics = metrics
        self._init_tweens(hover=0.0)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.TabFocus)
        self.setFont(_font_px(self.font(), metrics.button_px, QFont.Weight.DemiBold))
        self.setText(text)
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)

    def set_text(self, text: str) -> None:
        self.setText(text)
        self._resize_to_hint()

    def sizeHint(self) -> QSize:
        fm = self.fontMetrics()
        pad = max(16, round(fm.height() * 1.15))
        return QSize(fm.horizontalAdvance(self.text()) + pad * 2, self._metrics.button_h)

    def minimumSizeHint(self) -> QSize:
        return self.sizeHint()

    def _resize_to_hint(self) -> None:
        hint = self.sizeHint()
        self.setFixedSize(hint)

    def keyPressEvent(self, event) -> None:
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self.click()
            event.accept()
            return
        super().keyPressEvent(event)

    def paintEvent(self, _event) -> None:
        pal = palette()
        accent = to_color(pal.get('accent', '#0078d4'))
        on_accent = to_color(pal.get('on_accent', '#ffffff'))
        primary = to_color(pal.get('primary_text', '#eaeaea'))
        border = to_color(pal.get('card_border', '#444444'))
        surface = to_color(pal.get('card_bg', '#1b1b1b'))
        radius = max(7, self.height() / 2.2)
        rect = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        if not self.isEnabled():
            p.setOpacity(0.45)
        hover = 1.0 if self.isDown() else self._vals['hover']
        if self._kind == 'primary':
            fill = _mix(accent, accent.lighter(108), hover)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(fill)
            p.drawRoundedRect(rect, radius, radius)
            text_color = on_accent
        elif self._kind == 'ghost':
            if hover > 0:
                tint = QColor(accent)
                tint.setAlphaF(0.08 * hover)
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(tint)
                p.drawRoundedRect(rect, radius, radius)
            text_color = to_color(pal.get('accent_text', pal.get('accent', '#0078d4')))
        else:
            fill = _mix(surface, to_color(pal.get('card_hover', '#242424')), 0.8 * hover)
            p.setPen(QPen(border, 1))
            p.setBrush(fill)
            p.drawRoundedRect(rect, radius, radius)
            text_color = primary
        if self.hasFocus():
            ring = QColor(accent)
            ring.setAlpha(170)
            p.setPen(QPen(ring, 1.4, Qt.PenStyle.DashLine))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawRoundedRect(rect.adjusted(2.5, 2.5, -2.5, -2.5), radius - 2, radius - 2)
        p.setPen(text_color)
        p.setFont(self.font())
        p.drawText(self.rect(), int(Qt.AlignmentFlag.AlignCenter), self.text())


class _ToggleCard(_Tweens, QAbstractButton):

    TRACK_W = 40.0
    TRACK_H = 22.0
    KNOB = 16.0

    def __init__(self, icon: QIcon, title: str, description: str, metrics: _Metrics, parent=None):
        super().__init__(parent)
        self._icon = icon
        self._title = title
        self._description = description
        self._metrics = metrics
        self._init_tweens(hover=0.0)
        self._position = 0.0
        self.setCheckable(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.TabFocus)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        policy = self.sizePolicy()
        policy.setHeightForWidth(True)
        self.setSizePolicy(policy)
        self.setToolTip(description)
        self.setMinimumWidth(0)
        self._knob_anim = QPropertyAnimation(self, b'position', self)
        self._knob_anim.setDuration(170)
        self._knob_anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        self.toggled.connect(self._animate_knob)

    def _get_position(self) -> float:
        return self._position

    def _set_position(self, value: float) -> None:
        self._position = max(0.0, min(1.0, float(value)))
        self.update()

    position = Property(float, _get_position, _set_position)

    def _animate_knob(self, checked: bool) -> None:
        self._knob_anim.stop()
        if not self.isVisible() or not animations_enabled():
            self._set_position(1.0 if checked else 0.0)
            return
        self._knob_anim.setStartValue(self._position)
        self._knob_anim.setEndValue(1.0 if checked else 0.0)
        self._knob_anim.start()

    def refresh_theme(self) -> None:
        self.update()

    def set_texts(self, title: str, description: str) -> None:
        self._title = title
        self._description = description
        self.setToolTip(description)
        self.updateGeometry()
        self.update()

    def _text_width(self, width: int) -> int:
        m = self._metrics
        return max(40, int(width) - m.card_pad * 2 - m.icon_px - m.gap - self.TRACK_W - m.gap)

    def _title_font(self) -> QFont:
        return _font_px(self.font(), self._metrics.base_px + 1, QFont.Weight.DemiBold)

    def _description_font(self) -> QFont:
        return _font_px(self.font(), max(12, self._metrics.base_px - 1))

    def heightForWidth(self, width: int) -> int:
        m = self._metrics
        text_w = self._text_width(width)
        title_h = _text_height(QFontMetrics(self._title_font()), self._title, text_w)
        desc_h = _text_height(QFontMetrics(self._description_font()), self._description, text_w)
        text_h = title_h + (4 + desc_h if desc_h else 0)
        return max(m.icon_px + m.card_pad * 2, text_h + m.card_pad * 2)

    def sizeHint(self) -> QSize:
        return QSize(self._metrics.feature_min_col, self.heightForWidth(self._metrics.feature_min_col))

    def minimumSizeHint(self) -> QSize:
        return QSize(0, self.heightForWidth(self._metrics.feature_min_col))

    def paintEvent(self, _event) -> None:
        m = self._metrics
        pal = palette()
        accent = to_color(pal.get('accent', '#0078d4'))
        on_accent = to_color(pal.get('on_accent', '#111111'))
        bg = to_color(pal.get('card_bg', '#1b1b1b'))
        border = to_color(pal.get('card_border', '#444444'))
        position = self._position
        hover = 1.0 if self.isDown() else self._vals['hover']
        rect = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        radius = 11.0
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setRenderHint(QPainter.RenderHint.TextAntialiasing)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(_mix(bg, to_color(pal.get('card_hover', '#242424')), 0.7 * hover))
        p.drawRoundedRect(rect, radius, radius)
        if position > 0:
            tint = QColor(accent)
            tint.setAlphaF(0.08 * position)
            p.setBrush(tint)
            p.drawRoundedRect(rect, radius, radius)
        pen_color = _mix(border, accent, 0.55 * max(hover, position))
        p.setPen(QPen(pen_color, 1.0 + 0.4 * position))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawRoundedRect(rect, radius, radius)
        if self.hasFocus():
            p.setPen(QPen(accent, 1, Qt.PenStyle.DashLine))
            p.drawRoundedRect(rect.adjusted(3, 3, -3, -3), radius - 2, radius - 2)

        pad = m.card_pad
        icon_rect = QRectF(pad, (self.height() - m.icon_px) / 2, m.icon_px, m.icon_px)
        _paint_icon_tile(p, self._icon, icon_rect, accent, 0.09 + 0.09 * position)

        text_x = pad + m.icon_px + m.gap
        text_w = self._text_width(self.width())
        title_font = self._title_font()
        desc_font = self._description_font()
        title_h = _text_height(QFontMetrics(title_font), self._title, text_w)
        desc_h = (_text_height(QFontMetrics(desc_font), self._description, text_w)
                  if self._description else 0)
        desc_gap = 4 if desc_h else 0
        text_h = title_h + desc_gap + desc_h
        text_y = max(pad, (self.height() - text_h) / 2)
        flags = int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop | Qt.TextFlag.TextWordWrap)
        p.setFont(title_font)
        p.setPen(to_color(pal.get('primary_text', '#eaeaea')))
        p.drawText(QRectF(text_x, text_y, text_w, title_h), flags, self._title)
        if desc_h:
            p.setFont(desc_font)
            p.setPen(to_color(pal.get('muted', '#9a9a9a')))
            p.drawText(QRectF(text_x, text_y + title_h + desc_gap, text_w, desc_h),
                       flags, self._description)

        track_w, track_h = self.TRACK_W, self.TRACK_H
        track_x = self.width() - pad - track_w
        track_y = (self.height() - track_h) / 2
        track = QRectF(track_x, track_y, track_w, track_h)
        off_track = _mix(bg, to_color(pal.get('muted', '#9a9a9a')), 0.28)
        track_color = _mix(off_track, accent, position)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(track_color)
        p.drawRoundedRect(track, track_h / 2, track_h / 2)
        p.setPen(QPen(_mix(track_color, to_color(pal.get('primary_text', '#eaeaea')), 0.14), 1))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawRoundedRect(track.adjusted(0.5, 0.5, -0.5, -0.5),
                          track_h / 2 - 0.5, track_h / 2 - 0.5)
        knob = self.KNOB
        knob_x = track.left() + 3 + (track_w - knob - 6) * position
        knob_rect = QRectF(knob_x, track_y + (track_h - knob) / 2, knob, knob)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(0, 0, 0, 38))
        p.drawEllipse(knob_rect.adjusted(0, 1, 0, 1))
        p.setBrush(_mix(QColor('#ffffff'), on_accent, position))
        p.drawEllipse(knob_rect)


class _VolumeCard(_Tweens, QWidget):

    changed = Signal(int)

    _SLIDER_H = 22

    def __init__(self, icon: QIcon, title: str, description: str, value: int,
                 metrics: _Metrics, off_text: str, parent=None):
        super().__init__(parent)
        self._icon = icon
        self._title = title
        self._description = description
        self._metrics = metrics
        self._off_text = off_text
        self._init_tweens(hover=0.0)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        policy = self.sizePolicy()
        policy.setHeightForWidth(True)
        self.setSizePolicy(policy)
        self.setMinimumWidth(0)
        self._slider = Slider(Qt.Orientation.Horizontal, self)
        self._slider.setRange(0, 100)
        self._slider.setValue(max(0, min(100, int(value))))
        self._slider.valueChanged.connect(self._on_value)
        self.setMouseTracking(True)

    def refresh_theme(self) -> None:
        self.update()

    def set_texts(self, title: str, description: str) -> None:
        self._title = title
        self._description = description
        self.updateGeometry()
        self.update()

    def value(self) -> int:
        return self._slider.value()

    def _title_font(self) -> QFont:
        return _font_px(self.font(), self._metrics.base_px + 1, QFont.Weight.DemiBold)

    def _description_font(self) -> QFont:
        return _font_px(self.font(), max(12, self._metrics.base_px - 1))

    def _value_font(self) -> QFont:
        return _font_px(self.font(), self._metrics.caption_px, QFont.Weight.DemiBold)

    def _pill_width(self) -> int:
        fm = QFontMetrics(self._value_font())
        return max(44, max(fm.horizontalAdvance('100%'), fm.horizontalAdvance(self._off_text)) + 18)

    def _text_width(self, width: int) -> int:
        m = self._metrics
        return max(40, int(width) - m.card_pad * 2 - m.icon_px - m.gap - self._pill_width() - m.gap)

    def _text_heights(self, width: int) -> tuple[int, int]:
        text_w = self._text_width(width)
        title_h = _text_height(QFontMetrics(self._title_font()), self._title, text_w)
        desc_h = _text_height(QFontMetrics(self._description_font()), self._description, text_w)
        return title_h, desc_h

    def _top_height(self, width: int) -> int:
        title_h, desc_h = self._text_heights(width)
        return max(self._metrics.icon_px, title_h + (4 + desc_h if desc_h else 0))

    def heightForWidth(self, width: int) -> int:
        m = self._metrics
        return m.card_pad * 2 + self._top_height(width) + 12 + self._SLIDER_H

    def sizeHint(self) -> QSize:
        return QSize(self._metrics.feature_min_col, self.heightForWidth(self._metrics.feature_min_col))

    def minimumSizeHint(self) -> QSize:
        return QSize(0, self.heightForWidth(self._metrics.feature_min_col))

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        m = self._metrics
        self._slider.setGeometry(m.card_pad, self.height() - m.card_pad - self._SLIDER_H,
                                 max(1, self.width() - m.card_pad * 2), self._SLIDER_H)

    def _on_value(self, value: int) -> None:
        self.changed.emit(int(value))
        self.update()

    def _value_text(self) -> str:
        value = self.value()
        return f'{value}%' if value > 0 else self._off_text

    def paintEvent(self, _event) -> None:
        m = self._metrics
        pal = palette()
        accent = to_color(pal.get('accent', '#0078d4'))
        bg = to_color(pal.get('card_bg', '#1b1b1b'))
        border = to_color(pal.get('card_border', '#444444'))
        muted = to_color(pal.get('muted', '#9a9a9a'))
        active = self.value() > 0
        rect = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        radius = 11.0
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setRenderHint(QPainter.RenderHint.TextAntialiasing)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(_mix(bg, to_color(pal.get('card_hover', '#242424')), 0.7 * self._vals['hover']))
        p.drawRoundedRect(rect, radius, radius)
        p.setPen(QPen(border, 1))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawRoundedRect(rect, radius, radius)

        pad = m.card_pad
        top_h = self._top_height(self.width())
        title_h, desc_h = self._text_heights(self.width())
        text_h = title_h + (4 + desc_h if desc_h else 0)
        icon_rect = QRectF(pad, pad + (top_h - m.icon_px) / 2, m.icon_px, m.icon_px)
        _paint_icon_tile(p, self._icon, icon_rect, accent, 0.09 + (0.09 if active else 0.0))

        text_x = pad + m.icon_px + m.gap
        text_w = self._text_width(self.width())
        text_y = pad + (top_h - text_h) / 2
        flags = int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop | Qt.TextFlag.TextWordWrap)
        title_font = self._title_font()
        p.setFont(title_font)
        p.setPen(to_color(pal.get('primary_text', '#eaeaea')))
        p.drawText(QRectF(text_x, text_y, text_w, title_h), flags, self._title)
        p.setFont(self._description_font())
        p.setPen(muted)
        p.drawText(QRectF(text_x, text_y + title_h + 4, text_w, desc_h), flags, self._description)

        value_font = self._value_font()
        pill_w = self._pill_width()
        pill_h = QFontMetrics(value_font).height() + 6
        line_h = QFontMetrics(title_font).height()
        pill = QRectF(self.width() - pad - pill_w, text_y + (line_h - pill_h) / 2, pill_w, pill_h)
        fill = QColor(accent if active else muted)
        fill.setAlphaF(0.16 if active else 0.12)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(fill)
        p.drawRoundedRect(pill, pill_h / 2, pill_h / 2)
        p.setFont(value_font)
        p.setPen(accent if active else muted)
        p.drawText(pill, int(Qt.AlignmentFlag.AlignCenter), self._value_text())


class _ChoiceCard(_Tweens, QAbstractButton):

    def __init__(self, text: str, metrics: _Metrics, parent=None):
        super().__init__(parent)
        self._metrics = metrics
        self._init_tweens(hover=0.0, sel=0.0)
        self.setText(text)
        self.setCheckable(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.TabFocus)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        policy = self.sizePolicy()
        policy.setHeightForWidth(True)
        self.setSizePolicy(policy)
        self.setFont(_font_px(self.font(), metrics.base_px, QFont.Weight.DemiBold))
        self.setFixedHeight(metrics.lang_h)

    def set_selected(self, selected: bool) -> None:
        if self.isChecked() != selected:
            self.setChecked(selected)
        self._tween('sel', 1.0 if selected else 0.0)

    def heightForWidth(self, width: int) -> int:
        return self._metrics.lang_h

    def hasHeightForWidth(self) -> bool:
        return True

    def sizeHint(self) -> QSize:
        return QSize(max(168, self._metrics.feature_min_col // 2), self._metrics.lang_h)

    def minimumSizeHint(self) -> QSize:
        return QSize(0, self._metrics.lang_h)

    def paintEvent(self, _event) -> None:
        pal = palette()
        accent = to_color(pal.get('accent', '#0078d4'))
        on_accent = to_color(pal.get('on_accent', '#111111'))
        bg = to_color(pal.get('card_bg', '#1b1b1b'))
        border = to_color(pal.get('card_border', '#444444'))
        sel = self._vals['sel']
        hover = self._vals['hover']
        rect = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        radius = 10.0
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(_mix(bg, to_color(pal.get('card_hover', '#242424')), 0.7 * self._vals['hover']))
        p.drawRoundedRect(rect, radius, radius)
        if sel > 0:
            tint = QColor(accent)
            tint.setAlphaF(0.10 * sel)
            p.setBrush(tint)
            p.drawRoundedRect(rect, radius, radius)
        p.setPen(QPen(_mix(border, accent, max(0.85 * sel, 0.35 * hover)), 1.0 + 0.8 * sel))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawRoundedRect(rect, radius, radius)
        if self.hasFocus():
            p.setPen(QPen(accent, 1, Qt.PenStyle.DashLine))
            p.drawRoundedRect(rect.adjusted(3, 3, -3, -3), radius - 2, radius - 2)

        pad = max(14, self._metrics.card_pad + 2)
        check_d = 18.0
        text_w = max(1, self.width() - pad * 2 - check_d - 10)
        p.setPen(to_color(pal.get('primary_text', '#eaeaea')))
        p.setFont(self.font())
        p.drawText(QRectF(pad, 0, text_w, self.height()),
                   int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter), self.text())

        center = QPointF(self.width() - pad - check_d / 2, self.height() / 2)
        if sel < 1:
            p.setOpacity(1.0 - sel)
            p.setPen(QPen(_mix(border, to_color(pal.get('primary_text', '#eaeaea')), 0.25), 1))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawEllipse(center, check_d / 2 - 0.5, check_d / 2 - 0.5)
        if sel > 0:
            p.setOpacity(sel)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(accent)
            p.drawEllipse(center, check_d / 2, check_d / 2)
            tick = QPainterPath(QPointF(center.x() - 4.2, center.y()))
            tick.lineTo(center.x() - 1.2, center.y() + 3.1)
            tick.lineTo(center.x() + 4.4, center.y() - 3.2)
            p.setPen(QPen(on_accent, 1.8, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap,
                          Qt.PenJoinStyle.RoundJoin))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawPath(tick)


class _ThemeCard(_Tweens, QAbstractButton):

    def __init__(self, option_id: str, label: str, swatch: tuple, media: str, kind: str,
                 metrics: _Metrics, parent=None):
        super().__init__(parent)
        self.option_id = option_id
        self._label = label
        self._bg, self._text, self._accent = (to_color(c) for c in swatch)
        self._media = media
        self._kind = kind
        self._metrics = metrics
        self._init_tweens(hover=0.0, sel=0.0)
        self._requested = not media or kind == 'video'
        self.setCheckable(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.TabFocus)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        policy = self.sizePolicy()
        policy.setHeightForWidth(True)
        self.setSizePolicy(policy)
        self.setMinimumWidth(0)
        self.setToolTip(label)

    def set_selected(self, selected: bool) -> None:
        if self.isChecked() != selected:
            self.setChecked(selected)
        self._tween('sel', 1.0 if selected else 0.0)

    def refresh_theme(self) -> None:
        self.update()

    def _label_font(self) -> QFont:
        return _font_px(self.font(), max(12, self._metrics.base_px - 1), QFont.Weight.DemiBold)

    def _label_height(self, width: int) -> int:
        pad = self._metrics.theme_pad
        return _text_height(QFontMetrics(self._label_font()), self._label,
                            max(1, int(width) - pad * 2))

    def _preview_height(self, width: int) -> int:
        pad = self._metrics.theme_pad
        return max(32, round((width - pad * 2) * 0.52))

    def heightForWidth(self, width: int) -> int:
        pad = self._metrics.theme_pad
        return pad * 2 + self._preview_height(width) + 5 + self._label_height(width)

    def sizeHint(self) -> QSize:
        return QSize(self._metrics.theme_min_col, self.heightForWidth(self._metrics.theme_min_col))

    def minimumSizeHint(self) -> QSize:
        return QSize(0, self.heightForWidth(self._metrics.theme_min_col))

    def _on_thumb(self, path: str) -> None:
        if path == self._media:
            self.update()

    def _paint_mock(self, p: QPainter, rect: QRectF) -> None:
        p.fillRect(rect, self._bg)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(self._accent)
        p.drawRoundedRect(QRectF(rect.left() + 8, rect.top() + 8, 26, 6), 3, 3)
        p.drawRoundedRect(QRectF(rect.right() - 30, rect.bottom() - 16, 22, 9), 4.5, 4.5)
        p.setBrush(_mix(self._bg, self._text, 0.55))
        p.drawRoundedRect(QRectF(rect.left() + 8, rect.top() + 22, rect.width() - 16, 4), 2, 2)
        p.drawRoundedRect(QRectF(rect.left() + 8, rect.top() + 32, rect.width() * 0.5, 4), 2, 2)

    def _paint_play(self, p: QPainter, rect: QRectF) -> None:
        c = rect.center()
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(0, 0, 0, 120))
        p.drawEllipse(c, 14, 14)
        tri = QPainterPath(QPointF(c.x() - 4, c.y() - 7))
        tri.lineTo(c.x() + 7, c.y())
        tri.lineTo(c.x() - 4, c.y() + 7)
        tri.closeSubpath()
        p.setBrush(QColor('white'))
        p.drawPath(tri)

    def _paint_badge(self, p: QPainter, rect: QRectF) -> None:
        text = _THUMB_BADGE.get(self._kind)
        if not text:
            return
        font = QFont(self.font())
        font.setPixelSize(max(9, round(self._metrics.caption_px * 0.82)))
        font.setBold(True)
        p.setFont(font)
        width = QFontMetrics(font).horizontalAdvance(text) + 12
        pill = QRectF(rect.left() + 6, rect.bottom() - 20, width, 15)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(0, 0, 0, 150))
        p.drawRoundedRect(pill, 7.5, 7.5)
        p.setPen(QColor('white'))
        p.drawText(pill, int(Qt.AlignmentFlag.AlignCenter), text)

    def paintEvent(self, _event) -> None:
        if not self._requested:
            self._requested = True
            loader = _ThumbLoader.get()
            loader.ready.connect(self._on_thumb)
            loader.request(self._media)
        m = self._metrics
        pal = palette()
        accent = to_color(pal.get('accent', '#0078d4'))
        on_accent = to_color(pal.get('on_accent', '#111111'))
        bg = to_color(pal.get('card_bg', '#1b1b1b'))
        border = to_color(pal.get('card_border', '#444444'))
        sel = self._vals['sel']
        hover = self._vals['hover']
        pad = m.theme_pad
        rect = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        radius = 10.0
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(_mix(bg, to_color(pal.get('card_hover', '#242424')), 0.7 * self._vals['hover']))
        p.drawRoundedRect(rect, radius, radius)
        if sel > 0:
            tint = QColor(accent)
            tint.setAlphaF(0.09 * sel)
            p.setBrush(tint)
            p.drawRoundedRect(rect, radius, radius)
        p.setPen(QPen(_mix(border, accent, max(0.88 * sel, 0.35 * hover)), 1.0 + 0.9 * sel))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawRoundedRect(rect, radius, radius)
        if self.hasFocus():
            p.setPen(QPen(accent, 1, Qt.PenStyle.DashLine))
            p.drawRoundedRect(rect.adjusted(3, 3, -3, -3), radius - 2, radius - 2)

        label_h = self._label_height(self.width())
        preview = QRectF(pad, pad, max(1, self.width() - pad * 2),
                         max(1, self.height() - label_h - 5 - pad * 2))
        clip = QPainterPath()
        clip.addRoundedRect(preview, 7, 7)
        p.save()
        p.setClipPath(clip)
        pixmap = _THUMBS.get(self._media)
        if pixmap is not None and not pixmap.isNull():
            scale = max(preview.width() / pixmap.width(), preview.height() / pixmap.height())
            w, h = preview.width() / scale, preview.height() / scale
            p.drawPixmap(preview, pixmap,
                         QRectF((pixmap.width() - w) / 2, (pixmap.height() - h) / 2, w, h))
        else:
            self._paint_mock(p, preview)
            if self._kind == 'video':
                self._paint_play(p, preview)
        p.restore()
        p.setPen(QPen(QColor(255, 255, 255, 30), 1))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawRoundedRect(preview, 7, 7)
        self._paint_badge(p, preview)

        label_font = self._label_font()
        p.setFont(label_font)
        p.setPen(_mix(to_color(pal.get('primary_text', '#eaeaea')), accent, sel))
        flags = int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop | Qt.TextFlag.TextWordWrap)
        p.drawText(QRectF(pad, preview.bottom() + 5, max(1, self.width() - pad * 2), label_h),
                   flags, self._label)

        if sel > 0:
            center = QPointF(preview.right() - 11, preview.top() + 11)
            p.setOpacity(sel)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(accent)
            p.drawEllipse(center, 9, 9)
            tick = QPainterPath(QPointF(center.x() - 4.2, center.y()))
            tick.lineTo(center.x() - 1.2, center.y() + 3.1)
            tick.lineTo(center.x() + 4.4, center.y() - 3.2)
            p.setPen(QPen(on_accent, 1.8, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap,
                          Qt.PenJoinStyle.RoundJoin))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawPath(tick)


class _ResponsiveGrid(QWidget):

    def __init__(self, gap: int, min_col: int, max_cols: int, parent=None, min_items: int = 0):
        super().__init__(parent)
        self._gap = max(0, int(gap))
        self._min_col = max(1, int(min_col))
        self._max_cols = max(1, int(max_cols))
        self._min_items = max(0, int(min_items))
        self._items: list[QWidget] = []
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        self.setMinimumWidth(0)
        policy = self.sizePolicy()
        policy.setHeightForWidth(True)
        self.setSizePolicy(policy)

    def add(self, widget: QWidget) -> None:
        widget.setParent(self)
        widget.show()
        self._items.append(widget)

    def columns_for(self, width: int) -> int:
        if not self._items:
            return 1
        width = max(1, int(width))
        cols = (width + self._gap) // (self._min_col + self._gap)
        return max(1, min(self._max_cols, max(len(self._items), self._min_items), int(cols)))

    def _layout_rows(self, width: int) -> tuple[list[list[QWidget]], list[int], int]:
        width = max(1, int(width))
        cols = self.columns_for(width)
        col_w = (width - self._gap * (cols - 1)) / cols
        rows: list[list[QWidget]] = []
        heights: list[int] = []
        for start in range(0, len(self._items), cols):
            row = self._items[start:start + cols]
            rows.append(row)
            heights.append(max((_height_for_width(item, round(col_w)) for item in row), default=0))
        total = sum(heights) + self._gap * max(0, len(heights) - 1)
        return rows, heights, total

    def heightForWidth(self, width: int) -> int:
        return self._layout_rows(width)[2]

    def hasHeightForWidth(self) -> bool:
        return True

    def sizeHint(self) -> QSize:
        width = max(self._min_col, self._min_col * self._max_cols + self._gap * (self._max_cols - 1))
        return QSize(width, self.heightForWidth(width))

    def minimumSizeHint(self) -> QSize:
        return QSize(0, 0)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._reposition(self.width())

    def _reposition(self, width: int) -> None:
        rows, heights, _total = self._layout_rows(width)
        if not rows:
            return
        cols = self.columns_for(width)
        col_w = (width - self._gap * (cols - 1)) / cols
        y = 0
        for row, row_h in zip(rows, heights):
            x = 0.0
            for item in row:
                item.setGeometry(round(x), round(y), max(1, round(col_w)), max(1, int(row_h)))
                x += col_w + self._gap
            y += row_h + self._gap


class _Page(QWidget):

    def __init__(self, metrics: _Metrics, parent=None):
        super().__init__(parent)
        self._metrics = metrics
        self._items: list[tuple[QWidget, int]] = []
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        self.setMinimumWidth(0)
        policy = self.sizePolicy()
        policy.setHeightForWidth(True)
        self.setSizePolicy(policy)

    def add(self, widget: QWidget, gap: int = 0) -> QWidget:
        widget.setParent(self)
        widget.show()
        self._items.append((widget, max(0, int(gap))))
        return widget

    def content_height(self, width: int) -> int:
        m = self._metrics
        inner = max(1, int(width) - m.side * 2)
        y = m.page_top
        for widget, gap in self._items:
            y += gap
            y += _height_for_width(widget, inner)
        return y + m.page_bottom

    def heightForWidth(self, width: int) -> int:
        return self.content_height(width)

    def hasHeightForWidth(self) -> bool:
        return True

    def sizeHint(self) -> QSize:
        return QSize(600, self.content_height(600))

    def minimumSizeHint(self) -> QSize:
        return QSize(0, 0)

    def refresh_theme(self) -> None:
        for widget, _gap in self._items:
            _refresh_widget(widget)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._reposition(self.width())

    def _reposition(self, width: int) -> None:
        m = self._metrics
        inner = max(1, int(width) - m.side * 2)
        y = m.page_top
        for widget, gap in self._items:
            y += gap
            h = _height_for_width(widget, inner)
            widget.setGeometry(m.side, round(y), inner, max(1, int(h)))
            y += h


class _PathRow(QWidget):

    def __init__(self, path: str, metrics: _Metrics, on_browse, parent=None):
        super().__init__(parent)
        self._metrics = metrics
        self._on_browse = on_browse
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.edit = QLineEdit(self)
        self.edit.setReadOnly(True)
        self.edit.setText(path)
        self.edit.setCursorPosition(0)
        self.edit.setToolTip(path)
        self.edit.setFont(_font_px(self.font(), metrics.base_px))
        self.edit.setFixedHeight(metrics.input_h)
        self.browse = _Button('', 'secondary', metrics, self)
        self.browse.clicked.connect(self._on_browse)
        self.refresh_theme()

    def set_path(self, path: str) -> None:
        self.edit.setText(path)
        self.edit.setCursorPosition(0)
        self.edit.setToolTip(path)

    def set_browse_text(self, text: str) -> None:
        self.browse.set_text(text)
        self.updateGeometry()

    def refresh_theme(self) -> None:
        pal = palette()
        self.edit.setStyleSheet(
            'QLineEdit {'
            f'background: {_css(pal.get("input_bg", "#1b1b1b"))};'
            f'color: {_css(pal.get("primary_text", "#eaeaea"))};'
            f'border: 1px solid {_css(pal.get("input_border", "#444444"))};'
            'border-radius: 8px; padding: 0 10px;'
            '}'
        )

    def heightForWidth(self, width: int) -> int:
        return max(self.edit.height(), self.browse.sizeHint().height())

    def hasHeightForWidth(self) -> bool:
        return True

    def sizeHint(self) -> QSize:
        return QSize(400, self.heightForWidth(400))

    def minimumSizeHint(self) -> QSize:
        return QSize(0, self.heightForWidth(400))

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        gap = self._metrics.gap
        browse_w = self.browse.sizeHint().width()
        h = self.heightForWidth(self.width())
        self.browse.setFixedSize(browse_w, h)
        self.browse.move(max(0, self.width() - browse_w), max(0, (h - self.browse.height()) // 2))
        self.edit.setGeometry(0, 0, max(1, self.width() - browse_w - gap), h)


class _StatusRow(QWidget):

    def __init__(self, metrics: _Metrics, on_reset, parent=None):
        super().__init__(parent)
        self._metrics = metrics
        self._on_reset = on_reset
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        self.label = _Label('', 'caption', metrics, self)
        self.reset = _Button('', 'ghost', metrics, self)
        self.reset.clicked.connect(self._on_reset)
        policy = self.sizePolicy()
        policy.setHeightForWidth(True)
        self.setSizePolicy(policy)

    def set_status(self, text: str, error: bool = False) -> None:
        self.label.set_role('error' if error else 'caption')
        self.label.setText(text)
        self.updateGeometry()

    def set_reset_text(self, text: str) -> None:
        self.reset.set_text(text)
        self.updateGeometry()

    def set_reset_enabled(self, enabled: bool) -> None:
        self.reset.setEnabled(bool(enabled))

    def refresh_theme(self) -> None:
        self.label.refresh_theme()
        self.reset.update()

    def _layout_values(self, width: int) -> tuple[int, int, int]:
        reset_w = self.reset.sizeHint().width()
        gap = self._metrics.gap
        label_w = max(1, int(width) - reset_w - gap)
        label_h = _height_for_width(self.label, label_w)
        return reset_w, gap, max(label_h, self.reset.sizeHint().height())

    def heightForWidth(self, width: int) -> int:
        return self._layout_values(width)[2]

    def hasHeightForWidth(self) -> bool:
        return True

    def sizeHint(self) -> QSize:
        return QSize(400, self.heightForWidth(400))

    def minimumSizeHint(self) -> QSize:
        return QSize(0, 0)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        reset_w, gap, h = self._layout_values(self.width())
        reset_h = self.reset.sizeHint().height()
        self.reset.setFixedSize(reset_w, reset_h)
        self.reset.move(max(0, self.width() - reset_w), max(0, (h - reset_h) // 2))
        self.label.setGeometry(0, 0, max(1, self.width() - reset_w - gap), h)


class _FolderList(QWidget):

    def __init__(self, metrics: _Metrics, parent=None):
        super().__init__(parent)
        self._metrics = metrics
        self._folders: list[str] = []
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        policy = self.sizePolicy()
        policy.setHeightForWidth(True)
        self.setSizePolicy(policy)

    def set_folders(self, folders: list[str]) -> None:
        self._folders = list(folders)
        self.updateGeometry()
        self.update()

    def _columns(self, width: int) -> int:
        return 2 if width >= 400 else 1

    def _row_h(self) -> int:
        return max(20, QFontMetrics(_font_px(self.font(), self._metrics.caption_px)).height() + 8)

    def heightForWidth(self, width: int) -> int:
        cols = self._columns(width)
        rows = max(1, math.ceil(max(1, len(self._folders)) / cols))
        return 14 + rows * self._row_h() + max(0, rows - 1) * 2

    def hasHeightForWidth(self) -> bool:
        return True

    def sizeHint(self) -> QSize:
        return QSize(400, self.heightForWidth(400))

    def minimumSizeHint(self) -> QSize:
        return QSize(0, 0)

    def paintEvent(self, _event) -> None:
        if not self._folders:
            return
        pal = palette()
        accent = to_color(pal.get('accent', '#0078d4'))
        bg = to_color(pal.get('card_bg', '#1b1b1b'))
        border = to_color(pal.get('card_border', '#444444'))
        text = to_color(pal.get('body_text', pal.get('primary_text', '#eaeaea')))
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        p.setPen(QPen(border, 1))
        p.setBrush(bg)
        p.drawRoundedRect(rect, 10, 10)
        cols = self._columns(self.width())
        rows = max(1, math.ceil(len(self._folders) / cols))
        col_w = (self.width() - 20) / cols
        row_h = self._row_h()
        font = _font_px(self.font(), self._metrics.caption_px)
        p.setFont(font)
        for index, folder in enumerate(self._folders):
            col = index // rows
            row = index % rows
            x = 10 + col * col_w
            y = 7 + row * (row_h + 2)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(accent)
            p.drawEllipse(QPointF(x + 4, y + row_h / 2 - 1), 2.2, 2.2)
            p.setPen(text)
            fm = QFontMetrics(font)
            available = max(1, int(col_w - 18))
            label = fm.elidedText(folder, Qt.TextElideMode.ElideMiddle, available)
            p.drawText(QRectF(x + 12, y, available, row_h),
                       int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter), label)


def _refresh_widget(widget: QWidget) -> None:
    method = getattr(widget, 'refresh_theme', None)
    if callable(method):
        with suppress(Exception):
            method()
        return
    for child in widget.findChildren(QWidget):
        method = getattr(child, 'refresh_theme', None)
        if callable(method):
            with suppress(Exception):
                method()


class _Greeting(QWidget):

    def __init__(self, metrics: _Metrics, start_code: str, parent=None):
        super().__init__(parent)
        self._metrics = metrics
        self._font = _font_px(self.font(), max(19, round(metrics.base_px * 1.75)),
                              QFont.Weight.DemiBold)
        self._dot_area = max(12, round(metrics.base_px))
        line_h = QFontMetrics(self._font).height()
        self._height = line_h + self._dot_area + 8
        self._texts = [(code, tr_for(code, 'onboarding.language_hello', ''))
                       for code, _name in LANGUAGES]
        self._index = next((i for i, (code, _text) in enumerate(self._texts)
                            if code == start_code), 0)
        self._text = self._target = self._texts[self._index][1]
        self._reveal = 1.0
        self._fading_out = False
        self._locked = False
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setMinimumWidth(0)
        self._anim = QPropertyAnimation(self, b'reveal', self)
        self._anim.setDuration(220)
        self._anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._anim.finished.connect(self._on_fade_done)
        self._timer = QTimer(self)
        self._timer.setInterval(3000)
        self._timer.timeout.connect(self._cycle)
        register_theme_refresh(self, self.refresh_theme)

    def _get_reveal(self) -> float:
        return self._reveal

    def _set_reveal(self, value: float) -> None:
        self._reveal = max(0.0, min(1.0, float(value)))
        self.update()

    reveal = Property(float, _get_reveal, _set_reveal)

    def heightForWidth(self, _width: int) -> int:
        return self._height

    def hasHeightForWidth(self) -> bool:
        return True

    def sizeHint(self) -> QSize:
        return QSize(max(1, self.width()), self._height)

    def minimumSizeHint(self) -> QSize:
        return QSize(0, self._height)

    def lock_to(self, code: str) -> None:
        self._locked = True
        self._timer.stop()
        found = next(((i, text) for i, (lang, text) in enumerate(self._texts)
                      if lang == code), None)
        if found is None:
            return
        self._index, target = found
        self._target = target
        if target == self._text:
            self._anim.stop()
            self._set_reveal(1.0)
            return
        self._fade_out()

    def _cycle(self) -> None:
        if self._locked or self._fading_out or self._anim.state() == QPropertyAnimation.State.Running:
            return
        self._index = (self._index + 1) % len(self._texts)
        self._target = self._texts[self._index][1]
        self._fade_out()

    def _fade_out(self) -> None:
        if not animations_enabled():
            self._fading_out = False
            self._text = self._target
            self._set_reveal(1.0)
            return
        self._fading_out = True
        self._anim.stop()
        self._anim.setStartValue(self._reveal)
        self._anim.setEndValue(0.0)
        self._anim.start()

    def _fade_in(self) -> None:
        self._fading_out = False
        self._anim.stop()
        self._anim.setStartValue(self._reveal)
        self._anim.setEndValue(1.0)
        self._anim.start()

    def _on_fade_done(self) -> None:
        if self._fading_out:
            self._text = self._target
            self._fade_in()
        elif self._text != self._target:
            self._fade_out()

    def refresh_theme(self) -> None:
        self.update()

    def showEvent(self, event) -> None:
        super().showEvent(event)
        if not self._locked:
            self._timer.start()

    def hideEvent(self, event) -> None:
        super().hideEvent(event)
        self._timer.stop()

    def paintEvent(self, _event) -> None:
        pal = palette()
        accent = to_color(pal.get('accent', '#0078d4'))
        muted = to_color(pal.get('muted', '#9a9a9a'))
        primary = to_color(pal.get('primary_text', '#eaeaea'))
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setRenderHint(QPainter.RenderHint.TextAntialiasing)
        ease = self._reveal * self._reveal * (3 - 2 * self._reveal)
        p.setOpacity(ease)
        offset = (1.0 - ease) * 6.0
        text_rect = QRectF(0, offset, self.width(), max(1.0, self.height() - self._dot_area - 4))
        p.setFont(self._font)
        p.setPen(primary)
        p.drawText(text_rect, int(Qt.AlignmentFlag.AlignCenter), self._text)

        dot_y = self.height() - self._dot_area / 2 - 3
        widths = [10.0 if i == self._index else 4.0 for i in range(len(self._texts))]
        gap = 6.0
        total = sum(widths) + gap * (len(widths) - 1)
        x = (self.width() - total) / 2
        p.setPen(Qt.PenStyle.NoPen)
        for i, width in enumerate(widths):
            active = i == self._index
            p.setBrush(accent if active else _mix(muted, accent, 0.15))
            p.drawRoundedRect(QRectF(x, dot_y - 2, width, 4), 2, 2)
            x += width + gap


class _Progress(QWidget):

    def __init__(self, steps: int, metrics: _Metrics, parent=None):
        super().__init__(parent)
        self._steps = max(1, int(steps))
        self._target = 0.0
        self._progress = 0.0
        self._metrics = metrics
        self.setFixedHeight(metrics.progress_h)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self._anim = QPropertyAnimation(self, b'progress', self)
        self._anim.setDuration(240)
        self._anim.setEasingCurve(QEasingCurve.Type.OutCubic)

    def _get_progress(self) -> float:
        return self._progress

    def _set_progress(self, value: float) -> None:
        self._progress = max(0.0, float(value))
        self.update()

    progress = Property(float, _get_progress, _set_progress)

    def set_step(self, index: int, animate: bool = True) -> None:
        target = float(max(0, min(self._steps - 1, int(index))) + 1)
        if target == self._target:
            return
        self._target = target
        self._anim.stop()
        if not animate or not animations_enabled():
            self._set_progress(target)
            return
        self._anim.setStartValue(self._progress)
        self._anim.setEndValue(target)
        self._anim.start()

    def refresh_theme(self) -> None:
        self.update()

    def paintEvent(self, _event) -> None:
        pal = palette()
        accent = to_color(pal.get('accent', '#0078d4'))
        track = to_color(pal.get('surface_border', pal.get('card_border', '#444444')))
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        gap = 5
        width = (self.width() - gap * (self._steps - 1)) / self._steps
        height = self.height()
        p.setPen(Qt.PenStyle.NoPen)
        for i in range(self._steps):
            x = i * (width + gap)
            p.setBrush(track)
            p.drawRoundedRect(QRectF(x, 0, width, height), height / 2, height / 2)
            fill = max(0.0, min(1.0, self._progress - i))
            if fill > 0:
                p.setBrush(accent)
                p.drawRoundedRect(QRectF(x, 0, max(1.0, width * fill), height),
                                  height / 2, height / 2)


class _Header(QWidget):

    def __init__(self, metrics: _Metrics, steps: int, parent=None):
        super().__init__(parent)
        self._metrics = metrics
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self._layout = QVBoxLayout(self)
        self._layout.setSpacing(0)
        self._layout.setContentsMargins(metrics.side, metrics.top, metrics.side, metrics.header_bottom)

        self.step = _Label('', 'step', metrics, self)
        self.step.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self._layout.addWidget(self.step)
        self._layout.addSpacing(metrics.gap)
        self.progress = _Progress(steps, metrics, self)
        self._progress_block = QWidget(self)
        block = QVBoxLayout(self._progress_block)
        block.setContentsMargins(0, 0, 0, 0)
        block.setSpacing(0)
        block.addWidget(self.progress)
        block.addSpacing(metrics.section_gap)
        self._progress_block.setFixedHeight(metrics.progress_h + metrics.section_gap)
        self._show_progress = True
        self._layout.addWidget(self._progress_block)
        self.title = _Label('', 'title', metrics, self)
        self._layout.addWidget(self.title)
        self._layout.addSpacing(metrics.title_gap)
        self.subtitle = _Label('', 'subtitle', metrics, self)
        self._layout.addWidget(self.subtitle)
        self.setFixedHeight(self.measure(640))

    def set_progress_visible(self, visible: bool) -> None:
        m = self._metrics
        self._show_progress = bool(visible)
        self.progress.setVisible(bool(visible))
        self._progress_block.setVisible(bool(visible))
        self._progress_block.setFixedHeight((m.progress_h + m.section_gap) if visible else 0)
        self.updateGeometry()

    def set_texts(self, title: str, subtitle: str, step: str) -> None:
        self.title.setText(title)
        self.subtitle.setText(subtitle)
        self.step.setText(step)
        self.step.setVisible(bool(step))
        self.updateGeometry()

    def refresh_theme(self) -> None:
        for widget in (self.step, self.title, self.subtitle, self.progress):
            method = getattr(widget, 'refresh_theme', None)
            if callable(method):
                with suppress(Exception):
                    method()

    def measure(self, width: int) -> int:
        m = self._metrics
        inner = max(1, int(width) - m.side * 2)
        step_h = _height_for_width(self.step, inner) if self.step.isVisible() else 0
        title_h = _height_for_width(self.title, inner)
        subtitle_h = _height_for_width(self.subtitle, inner) if self.subtitle.text() else 0
        self.title.setFixedHeight(title_h)
        self.subtitle.setFixedHeight(subtitle_h)
        progress_h = (m.progress_h + m.section_gap) if self._show_progress else 0
        total = (m.top + step_h + m.gap + progress_h
                 + title_h + m.title_gap + subtitle_h + m.header_bottom)
        self.setFixedHeight(total)
        return total


class _Footer(QWidget):

    def __init__(self, metrics: _Metrics, parent=None):
        super().__init__(parent)
        self._metrics = metrics
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self._layout = QHBoxLayout(self)
        self._layout.setSpacing(12)
        self._layout.setContentsMargins(metrics.side, metrics.footer_top,
                                        metrics.side, metrics.footer_bottom)
        self.back = _Button('', 'secondary', metrics, self)
        self.next = _Button('', 'primary', metrics, self)
        self._layout.addWidget(self.back, 0, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        self._layout.addStretch(1)
        self._layout.addWidget(self.next, 0, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.setFixedHeight(self._measure())

    def set_texts(self, back: str, next_text: str, show_back: bool) -> None:
        self.back.set_text(back)
        self.next.set_text(next_text)
        self.back.setVisible(bool(show_back and back))
        self.updateGeometry()

    def refresh_theme(self) -> None:
        self.back.update()
        self.next.update()

    def _measure(self) -> int:
        m = self._metrics
        return m.footer_top + max(self.back.sizeHint().height(), self.next.sizeHint().height()) + m.footer_bottom

    def measure(self, width: int) -> int:
        self.setFixedWidth(max(1, int(width)))
        value = self._measure()
        self.setFixedHeight(value)
        return value

    def paintEvent(self, _event) -> None:
        pal = palette()
        p = QPainter(self)
        p.setPen(QPen(to_color(pal.get('card_border', '#444444')), 1))
        p.drawLine(0, 0, self.width(), 0)


class _Surface(QFrame):

    def __init__(self, radius: int, parent=None):
        super().__init__(parent)
        self._radius = max(1, int(radius))
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAutoFillBackground(False)
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)

    def paintEvent(self, _event) -> None:
        pal = palette()
        bg = to_color(pal.get('dialog_bg', pal.get('card_bg', '#1b1b1b')))
        border = to_color(pal.get('dialog_border', pal.get('card_border', '#444444')))
        rect = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        path = QPainterPath()
        path.addRoundedRect(rect, self._radius, self._radius)
        gradient = QLinearGradient(rect.topLeft(), rect.bottomLeft())
        gradient.setColorAt(0.0, bg.lighter(107))
        gradient.setColorAt(0.42, bg)
        gradient.setColorAt(1.0, bg.darker(106))
        p.fillPath(path, QBrush(gradient))
        p.setPen(QPen(border, 1))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawPath(path)


class _OverlayDialog(QDialog):
    _EMBEDDED = sys.platform != 'win32'

    def __init__(self, owner: QWidget | None, steps: int = 1):
        super().__init__(owner.window() if owner is not None and owner.window() is not owner else owner)
        self._owner = owner.window() if owner is not None and owner.window() is not None else owner
        self.setObjectName('pirachestOnboardingOverlay')
        if self._EMBEDDED and self._owner is not None:
            self.setWindowFlags(Qt.WindowType.Widget)
        else:
            self._EMBEDDED = False
            self.setWindowFlags(Qt.WindowType.Dialog | Qt.WindowType.FramelessWindowHint
                                | Qt.WindowType.NoDropShadowWindowHint)
            self.setModal(True)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setStyleSheet(
            '#pirachestOnboardingOverlay { background: transparent; border: none; }'
        )
        self._bounds = QRect(0, 0, 900, 600)
        self._inset = 24
        self._font = QFont(self.font())
        self._font.setFamilies(list(_UI_FAMILIES))
        self._font.setPointSizeF(10.0)
        self.setFont(self._font)
        rect = self._owner_rect()
        self._bounds = rect
        self._inset = max(10, min(28, round(min(rect.width(), rect.height()) * 0.045)))
        max_w = max(1, rect.width() - self._inset * 2)
        max_h = max(1, rect.height() - self._inset * 2)
        self._metrics = _make_metrics(self._font, max_h < 580 or max_w < 700)
        self._surface = _Surface(self._metrics.radius, self)
        self._outer = QVBoxLayout(self)
        self._outer.setContentsMargins(self._inset, self._inset, self._inset, self._inset)
        self._outer.setSpacing(0)
        self._outer.addWidget(self._surface, 0, Qt.AlignmentFlag.AlignCenter)
        self._surface_layout = QVBoxLayout(self._surface)
        self._surface_layout.setContentsMargins(0, 0, 0, 0)
        self._surface_layout.setSpacing(0)
        self._header = _Header(self._metrics, max(1, int(steps)), self._surface)
        self._body = QScrollArea(self._surface)
        self._body.setFrameShape(QFrame.Shape.NoFrame)
        self._body.setWidgetResizable(True)
        self._body.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._body.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self._body.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self._body.viewport().setAutoFillBackground(False)
        self._body.setStyleSheet('QScrollArea { background: transparent; border: none; }'
                                 'QScrollArea > QWidget > QWidget { background: transparent; }')
        self._footer = _Footer(self._metrics, self._surface)
        self._surface_layout.addWidget(self._header)
        self._surface_layout.addWidget(self._body)
        self._surface_layout.addWidget(self._footer)
        self._page: QWidget | None = None
        self._refit_scheduled = False
        self._shell_t = 1.0
        self._closing = False
        self._opened = False
        self._close_result = 0
        self._fx: QGraphicsOpacityEffect | None = None
        self._shell_anim = QVariantAnimation(self)
        self._shell_anim.valueChanged.connect(self._set_t)
        self._shell_anim.finished.connect(self._shell_done)
        self._fit = (1, 1, 1)
        self._animate_resize = False
        self._resize_anim = QVariantAnimation(self)
        self._resize_anim.setDuration(170)
        self._resize_anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._resize_anim.valueChanged.connect(self._tick_resize)
        self._apply_bounds()
        if self._owner is not None:
            self._owner.installEventFilter(self)

    def _owner_rect(self) -> QRect:
        owner = self._owner
        if self._EMBEDDED:
            return owner.rect() if owner.width() > 80 and owner.height() > 80 else QRect(0, 0, 900, 600)
        rect = QRect()
        if owner is not None and owner.width() > 80 and owner.height() > 80:
            rect = owner.frameGeometry() if owner.isVisible() else owner.geometry()
        screen = None
        if owner is not None:
            with suppress(Exception):
                screen = owner.screen()
        if screen is None and not rect.isNull():
            screen = QApplication.screenAt(rect.center())
        if screen is None:
            screen = QApplication.primaryScreen()
        if screen is not None:
            available = screen.availableGeometry()
            if rect.width() > 80 and rect.height() > 80:
                rect = rect.intersected(available)
            else:
                rect = available
        if rect.width() <= 80 or rect.height() <= 80:
            rect = QRect(0, 0, 900, 600)
        return rect

    def _apply_bounds(self) -> None:
        rect = self._owner_rect()
        self._bounds = rect
        self._inset = max(10, min(28, round(min(rect.width(), rect.height()) * 0.045)))
        self._outer.setContentsMargins(self._inset, self._inset, self._inset, self._inset)
        self.setGeometry(rect)

    def paintEvent(self, _event) -> None:
        p = QPainter(self)
        p.fillRect(self.rect(), QColor(0, 0, 0, round(118 * self._shell_t)))

    def _place(self) -> None:
        s = self._surface
        off = round((1.0 - self._shell_t) * 14)
        s.move((self.width() - s.width()) // 2, (self.height() - s.height()) // 2 + off)

    def _set_t(self, value) -> None:
        self._shell_t = float(value)
        if self._fx is not None:
            self._fx.setOpacity(self._shell_t)
        self._place()
        self.update()

    def _run_shell(self, end: float, ms: int, curve: QEasingCurve.Type) -> None:
        anim = self._shell_anim
        anim.stop()
        if self._fx is None:
            self._fx = QGraphicsOpacityEffect(self._surface)
            self._fx.setOpacity(self._shell_t)
            self._surface.setGraphicsEffect(self._fx)
        anim.setStartValue(self._shell_t)
        anim.setEndValue(float(end))
        anim.setDuration(ms)
        anim.setEasingCurve(curve)
        anim.start()

    def _shell_done(self) -> None:
        if self._closing:
            QDialog.done(self, self._close_result)
            return
        self._surface.setGraphicsEffect(None)
        self._fx = None
        self._place()

    def done(self, result: int) -> None:
        if self._closing:
            return
        if not (self.isVisible() and animations_enabled()):
            super().done(result)
            return
        self._closing = True
        self._close_result = result
        self._surface.setEnabled(False)
        self._run_shell(0.0, 120, QEasingCurve.Type.InCubic)

    def _tick_resize(self, height) -> None:
        width, header_h, footer_h = self._fit
        height = int(height)
        self._body.setFixedHeight(height)
        self._header.setFixedWidth(width)
        self._footer.setFixedWidth(width)
        self._surface.setFixedSize(width, header_h + height + footer_h)
        self._place()

    def eventFilter(self, obj, event) -> bool:
        if obj is self._owner and event.type() in (QEvent.Type.Resize, QEvent.Type.Move):
            self._apply_bounds()
            self._request_refit()
        return super().eventFilter(obj, event)

    def showEvent(self, event) -> None:
        self._apply_bounds()
        super().showEvent(event)
        if self._EMBEDDED:
            self.raise_()
            self.setFocus()
        self._request_refit()
        if not self._opened:
            self._opened = True
            if animations_enabled():
                self._shell_t = 0.0
                self._run_shell(1.0, 200, QEasingCurve.Type.OutCubic)

    def _available(self) -> tuple[int, int]:
        return (max(1, self._bounds.width() - self._inset * 2),
                max(1, self._bounds.height() - self._inset * 2))

    def _take_page(self) -> None:
        old = self._body.takeWidget()
        if old is not None:
            old.setParent(None)
        self._page = None

    def set_page(self, page: QWidget | None) -> None:
        old = self._body.takeWidget()
        if old is not None:
            old.setParent(None)
        self._page = page
        if page is not None:
            page.setParent(self._body)
            self._body.setWidget(page)
            page.show()
        self._request_refit()

    def _preferred_width(self) -> int:
        fm = QFontMetrics(self._font)
        factor = 44 if self._metrics.compact else 46
        preferred = max(520, round(fm.height() * factor))
        max_w, _max_h = self._available()
        return max(1, min(max_w, min(840, preferred)))

    def _scrollbar_width(self) -> int:
        return max(1, self._body.verticalScrollBar().sizeHint().width())

    def _refit(self) -> None:
        self._refit_scheduled = False
        if not self.isVisible() and self.width() <= 1:
            return
        animate = self._animate_resize
        self._animate_resize = False
        self._apply_bounds()
        max_w, max_h = self._available()
        width = self._preferred_width()
        width = max(1, min(width, max_w))
        header_h = self._header.measure(width)
        footer_h = self._footer.measure(width)
        body_room = max(1, max_h - header_h - footer_h)
        page = self._page
        scroll = False
        body_h = 1
        page_need = 1
        if page is not None:
            page_need = max(1, _height_for_width(page, width))
            if page_need > body_room:
                scroll = True
                narrow = max(1, width - self._scrollbar_width())
                page_need = max(page_need, _height_for_width(page, narrow))
                body_h = body_room
            else:
                body_h = page_need
            page.setMinimumHeight(max(1, int(page_need)))
            page.setMaximumHeight(16777215)
        self._body.setVerticalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAsNeeded if scroll else Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._fit = (width, header_h, footer_h)
        body_h = max(1, int(body_h))
        anim = self._resize_anim
        if anim.state() == QPropertyAnimation.State.Running and anim.endValue() == body_h:
            return
        anim.stop()
        current = self._body.height()
        if animate and current != body_h and self.isVisible() and animations_enabled():
            anim.setStartValue(current)
            anim.setEndValue(body_h)
            anim.start()
        else:
            self._tick_resize(body_h)

    def _request_refit(self) -> None:
        if self._refit_scheduled:
            return
        self._refit_scheduled = True
        QTimer.singleShot(0, self, self._refit)

    def _refresh_theme(self) -> None:
        self._surface.update()
        self._header.refresh_theme()
        self._footer.refresh_theme()
        if self._page is not None:
            _refresh_widget(self._page)
        pal = palette()
        self._body.setStyleSheet(
            'QScrollArea { background: transparent; border: none; }'
            'QScrollArea > QWidget > QWidget { background: transparent; }'
            'QScrollBar:vertical { background: transparent; width: 10px; margin: 2px 1px; }'
            f'QScrollBar::handle:vertical {{ background: {_css(pal.get("scrollbar_handle", "#666666"))};'
            ' border-radius: 4px; min-height: 28px; }'
            'QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }'
            'QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical { background: transparent; }'
        )
        self._request_refit()
        self.update()


@dataclass
class _WizardState:
    language: str
    theme: str
    features: dict[str, bool] = field(default_factory=dict)
    download_root: str | None = None
    download_subfolders: bool = True
    prefs: dict[str, bool] = field(default_factory=dict)
    volumes: dict[str, int] = field(default_factory=dict)

    @classmethod
    def from_settings(cls) -> '_WizardState':
        language = current_language() if current_language() in {code for code, _name in LANGUAGES} else 'en'
        preset = settings.theme_preset
        if preset and preset_meta(preset) is not None:
            theme = preset
        else:
            theme = 'light' if settings.theme_mode == ThemeMode.LIGHT else 'dark'
        features = {attr: bool(getattr(settings, attr, False)) for attr, *_rest in FEATURES}
        prefs = {
            'tray': bool(settings.close_to_tray),
            'notify': bool(settings.notify_on_download_complete),
            'external': bool(settings.external_torrent_client),
            'startup': bool(get_launch_at_startup()),
        }
        volumes = {'sfx': int(settings.sfx_volume), 'ambiance': int(settings.ambiance_volume)}
        return cls(language=language, theme=theme, features=features, prefs=prefs, volumes=volumes)


class OnboardingWizard(_OverlayDialog):

    _BUILDERS = (
        '_build_language_page',
        '_build_theme_page',
        '_build_features_page',
        '_build_downloads_page',
        '_build_prefs_page',
    )
    _NEXT_KEYS = (
        'onboarding.language_continue',
        'onboarding.theme_continue',
        'onboarding.features_continue',
        'onboarding.next',
        'onboarding.finish',
    )
    _PAGE_TEXT = (
        ('onboarding.language_title', 'onboarding.language_subtitle'),
        ('onboarding.theme_title', 'onboarding.theme_subtitle'),
        ('onboarding.features_title', 'onboarding.features_subtitle'),
        ('onboarding.downloads_title', 'onboarding.downloads_subtitle'),
        ('onboarding.prefs_title', 'onboarding.prefs_subtitle'),
    )

    def __init__(self, owner: QWidget):
        self._state = _WizardState.from_settings()
        super().__init__(owner, steps=5)
        self._index = -1
        self._pages: list[QWidget | None] = [None] * len(self._BUILDERS)
        self._page_builders = {
            0: self._build_language_page,
            1: self._build_theme_page,
            2: self._build_features_page,
            3: self._build_downloads_page,
            4: self._build_prefs_page,
        }
        self._language_cards: dict[str, _ChoiceCard] = {}
        self._greeting: _Greeting | None = None
        self._theme_cards: dict[str, _ThemeCard] = {}
        self._feature_cards: dict[str, _ToggleCard] = {}
        self._pref_cards: dict[str, _ToggleCard] = {}
        self._volume_cards: dict[str, _VolumeCard] = {}
        self._pending_theme: str | None = None
        self._previewed_theme = self._state.theme
        self._theme_timer = QTimer(self)
        self._theme_timer.setSingleShot(True)
        self._theme_timer.timeout.connect(self._apply_pending_theme)
        self._footer.next.clicked.connect(self._advance)
        self._footer.back.clicked.connect(self._back)
        self._enter_widgets: list[QWidget] = []
        self._enter_dx = 0
        self._enter_anim = QVariantAnimation(self)
        self._enter_anim.setStartValue(0.0)
        self._enter_anim.setEndValue(1.0)
        self._enter_anim.setDuration(180)
        self._enter_anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._enter_anim.valueChanged.connect(self._tick_enter)
        self._enter_anim.finished.connect(self._end_enter)
        self._content_ready = True
        self._go(0, animate=False)
        register_theme_refresh(self, self._theme_changed)
        register_locale_refresh(self, self._locale_changed)

    def _t(self, key: str) -> str:
        return tr(key)

    def _apply_texts(self) -> None:
        index = max(0, min(self._index, len(self._BUILDERS) - 1))
        title_key, subtitle_key = self._PAGE_TEXT[index]
        self._header.set_texts(
            self._t(title_key),
            self._t(subtitle_key),
            f'{index + 1} / {len(self._BUILDERS)}',
        )
        self._footer.set_texts(
            self._t('onboarding.back'),
            self._t(self._NEXT_KEYS[index]),
            index > 0,
        )
        self._header.progress.set_step(index)
        if index == 3:
            self._sync_download_texts()
        elif index == 4:
            self._sync_pref_texts()

    def _sync_download_texts(self) -> None:
        page = self._pages[3]
        if not isinstance(page, _DownloadsPage):
            return
        page.retranslate()

    def _sync_pref_texts(self) -> None:
        page = self._pages[4]
        if not isinstance(page, _PrefsPage):
            return
        page.retranslate()

    def _build_page(self, index: int) -> QWidget:
        page = self._page_builders[index]()
        self._pages[index] = page
        return page

    def _drop_page(self, index: int) -> None:
        page = self._pages[index]
        if page is None:
            return
        if self._page is page:
            self._take_page()
        self._pages[index] = None
        page.setParent(None)
        page.deleteLater()
        if index == 1:
            self._theme_cards = {}
        elif index == 2:
            self._feature_cards = {}
        elif index == 3:
            self._downloads_page = None
        elif index == 4:
            self._pref_cards = {}
            self._volume_cards = {}

    def _drop_translated_pages(self) -> None:
        for index in range(1, len(self._BUILDERS)):
            self._drop_page(index)
        if self._index > 0:
            self._go(self._index, animate=False)

    def _go(self, index: int, animate: bool = False) -> None:
        index = max(0, min(int(index), len(self._BUILDERS) - 1))
        self._end_enter()
        page = self._pages[index]
        if page is None:
            page = self._build_page(index)
        changed = page is not self._page
        forward = index >= self._index
        self._index = index
        self._animate_resize = animate and changed
        self.set_page(page)
        self._apply_texts()
        self._refit()
        if animate and changed:
            self._enter(page, 28 if forward else -28)

    def _enter(self, page: QWidget, dx: int) -> None:
        if not animations_enabled():
            return
        self._enter_dx = dx
        self._enter_widgets = [page, self._header.title, self._header.subtitle]
        for widget in self._enter_widgets:
            effect = QGraphicsOpacityEffect(widget)
            effect.setOpacity(0.0)
            widget.setGraphicsEffect(effect)
        self._enter_anim.start()

    def _tick_enter(self, value) -> None:
        value = float(value)
        with suppress(RuntimeError):
            for widget in self._enter_widgets:
                effect = widget.graphicsEffect()
                if effect is not None:
                    effect.setOpacity(value)
            page = self._enter_widgets[0]
            page.move(round(self._enter_dx * (1.0 - value)), page.y())

    def _end_enter(self) -> None:
        self._enter_anim.stop()
        widgets, self._enter_widgets = self._enter_widgets, []
        for widget in widgets:
            with suppress(RuntimeError):
                widget.setGraphicsEffect(None)
        if widgets:
            with suppress(RuntimeError):
                widgets[0].move(0, widgets[0].y())

    def _advance(self) -> None:
        if self._index < len(self._BUILDERS) - 1:
            self._go(self._index + 1, animate=True)
            return
        self.accept()

    def _back(self) -> None:
        if self._index > 0:
            self._go(self._index - 1, animate=True)

    def _show(self, index: int, animate: bool = False) -> None:
        self._go(index, animate)

    def _retranslate(self) -> None:
        self._apply_texts()
        self._refit()

    def keyPressEvent(self, event) -> None:
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self._advance()
            event.accept()
            return
        if event.key() == Qt.Key.Key_Escape:
            self._back()
            event.accept()
            return
        super().keyPressEvent(event)

    def reject(self) -> None:
        if self._index > 0:
            self._back()
            return

    def closeEvent(self, event) -> None:
        if self._index > 0:
            self._back()
            event.ignore()
            return
        event.ignore()

    def _theme_changed(self) -> None:
        if not getattr(self, '_content_ready', False):
            return
        self._apply_theme_controls()
        self._refresh_theme()

    def _locale_changed(self) -> None:
        if not getattr(self, '_content_ready', False):
            return
        self._drop_translated_pages()
        self._apply_texts()
        self._refresh_theme()
        self._request_refit()

    def _apply_theme_controls(self) -> None:
        for card in self._theme_cards.values():
            card.refresh_theme()
        for card in self._feature_cards.values():
            card.refresh_theme()
        for card in self._pref_cards.values():
            card.refresh_theme()
        for card in self._volume_cards.values():
            card.refresh_theme()

    def _build_language_page(self) -> QWidget:
        page = _Page(self._metrics)
        self._greeting = page.add(_Greeting(self._metrics, self._state.language, page), 0)
        grid = _ResponsiveGrid(self._metrics.grid_gap, 168, 2, page)
        self._language_cards = {}
        for code, name in LANGUAGES:
            card = _ChoiceCard(name, self._metrics, grid)
            card.set_selected(code == self._state.language)
            card.clicked.connect(lambda _checked=False, c=code: self._pick_language(c))
            grid.add(card)
            self._language_cards[code] = card
        page.add(grid, 14)
        return page

    def _pick_language(self, code: str) -> None:
        if self._greeting is not None:
            self._greeting.lock_to(code)
        if code == self._state.language:
            return
        self._state.language = code
        for lang, card in self._language_cards.items():
            card.set_selected(lang == code)
        if code != current_language():
            set_language(code)
        self._drop_translated_pages()
        self._apply_texts()
        self._request_refit()

    def _build_theme_page(self) -> QWidget:
        page = _Page(self._metrics)
        self._theme_cards, self._theme_custom_header = _build_theme_sections(
            page, self._metrics, self._state.theme, self._pick_theme)
        return page

    def _pick_theme(self, option_id: str) -> None:
        if option_id == self._state.theme:
            return
        self._state.theme = option_id
        for oid, card in self._theme_cards.items():
            card.set_selected(oid == option_id)
        self._pending_theme = option_id
        self._theme_timer.start(0)

    def _apply_pending_theme(self) -> None:
        option_id = self._pending_theme
        self._pending_theme = None
        if option_id is None:
            return
        if _activate_option(option_id):
            self._previewed_theme = option_id

    def _flush_theme_preview(self) -> None:
        if self._pending_theme is not None:
            self._theme_timer.stop()
            self._apply_pending_theme()

    def _build_features_page(self) -> QWidget:
        page = _Page(self._metrics)
        grid = _ResponsiveGrid(self._metrics.grid_gap, self._metrics.feature_min_col, 2, page)
        self._feature_cards = {}
        for attr, slot, desc_key in FEATURES:
            icon = themed_icon(slot, _feature_icon(slot), size=self._metrics.icon_px)
            title = self._t(slot)
            card = _ToggleCard(icon, title, self._t(desc_key), self._metrics, grid)
            card.setChecked(bool(self._state.features.get(attr, False)))
            card.toggled.connect(lambda checked, a=attr: self._set_feature(a, checked))
            grid.add(card)
            self._feature_cards[attr] = card
        page.add(grid, 0)
        return page

    def _set_feature(self, attr: str, checked: bool) -> None:
        self._state.features[attr] = bool(checked)

    def _build_downloads_page(self) -> QWidget:
        page = _DownloadsPage(self._state, self._metrics)
        self._downloads_page = page
        page.reset_requested.connect(self._reset_downloads)
        page.browse_requested.connect(self._browse_downloads)
        page.subfolders_changed.connect(self._set_subfolders)
        page.retranslate()
        page.refresh()
        return page

    def _download_root(self) -> str:
        return self._state.download_root or settings.download_dir

    def _folders(self) -> list[str]:
        root = self._download_root()
        if not self._state.download_subfolders:
            return [root]
        changes = download_dir_changes(root, True)
        return [changes[attr] for attr in DOWNLOAD_DIR_SUBFOLDERS]

    def _refresh_downloads(self) -> None:
        page = self._pages[3]
        if isinstance(page, _DownloadsPage):
            page.refresh()

    def _set_subfolders(self, checked: bool) -> None:
        self._state.download_subfolders = bool(checked)
        self._refresh_downloads()
        self._request_refit()

    def _reset_downloads(self) -> None:
        self._state.download_root = None
        self._refresh_downloads()
        self._request_refit()

    def _browse_downloads(self) -> None:
        start = self._download_root()
        chosen = QFileDialog.getExistingDirectory(
            self, self._t('onboarding.downloads_pick'), start)
        if not chosen:
            return
        chosen = os.path.normpath(chosen)
        problem = check_dir_writable(chosen)
        page = self._pages[3]
        if problem:
            if isinstance(page, _DownloadsPage):
                page.show_error(problem)
            return
        self._state.download_root = chosen
        self._refresh_downloads()
        self._request_refit()

    def _build_prefs_page(self) -> QWidget:
        page = _PrefsPage(self._state, self._metrics)
        self._pref_cards = page.pref_cards
        self._volume_cards = page.volume_cards
        page.pref_changed.connect(self._set_pref)
        page.volume_changed.connect(self._set_volume)
        page.retranslate()
        return page

    def _set_pref(self, pref_id: str, checked: bool) -> None:
        self._state.prefs[pref_id] = bool(checked)

    def _set_volume(self, volume_id: str, value: int) -> None:
        self._state.volumes[volume_id] = int(value)
        with suppress(Exception):
            manager = get_sound_manager()
            if volume_id == 'sfx':
                manager.set_sfx_volume(int(value))
            else:
                manager.set_ambiance_volume(int(value))

    def commit(self) -> None:
        state = self._state
        changes: dict = {
            'language': state.language,
            'onboarding_completed': True,
        }
        if state.theme in _PLAIN_THEMES:
            changes.update(
                theme_mode=ThemeMode.DARK if state.theme == 'dark' else ThemeMode.LIGHT,
                theme_preset=None,
            )
            fonts = None
        else:
            meta = preset_meta(state.theme) if state.theme else None
            if meta is None:
                changes.update(theme_mode=ThemeMode.DARK, theme_preset=None)
                fonts = None
            else:
                changes['theme_preset'] = state.theme
                fonts = resolve_font_families(meta)
        changes.update(state.features)
        root = self._download_root()
        if (state.download_root
                or os.path.normcase(os.path.normpath(root))
                != os.path.normcase(os.path.normpath(settings.download_dir))
                or not state.download_subfolders):
            changes.update(download_dir_changes(root, state.download_subfolders))
        changes.update(
            close_to_tray=bool(state.prefs.get('tray', settings.close_to_tray)),
            notify_on_download_complete=bool(state.prefs.get('notify', settings.notify_on_download_complete)),
            external_torrent_client=bool(state.prefs.get('external', settings.external_torrent_client)),
            sfx_volume=int(state.volumes.get('sfx', settings.sfx_volume)),
            ambiance_volume=int(state.volumes.get('ambiance', settings.ambiance_volume)),
        )
        desired_startup = state.prefs.get('startup')
        if desired_startup is not None and desired_startup != get_launch_at_startup():
            if set_launch_at_startup(bool(desired_startup)):
                changes['launch_at_startup'] = bool(desired_startup)
        self._flush_theme_preview()
        if self._previewed_theme != state.theme and _activate_option(state.theme):
            self._previewed_theme = state.theme
        apply_settings(**changes)
        with suppress(Exception):
            apply_theme_font_families(fonts)
        save_settings(settings)


class _DownloadsPage(_Page):

    reset_requested = Signal()
    browse_requested = Signal()
    subfolders_changed = Signal(bool)

    def __init__(self, state: _WizardState, metrics: _Metrics, parent=None):
        super().__init__(metrics, parent)
        self._state = state
        self.path_row = self.add(_PathRow('', metrics, self.browse_requested.emit), 0)
        self.status_row = self.add(_StatusRow(metrics, self.reset_requested.emit), 4)
        self.subfolders = self.add(_ToggleCard(
            themed_icon('settings.folder', FluentIcon.FOLDER, size=metrics.icon_px),
            '', '', metrics), 12)
        self.subfolders.setChecked(bool(state.download_subfolders))
        self.subfolders.toggled.connect(self.subfolders_changed.emit)
        self.folder_title = self.add(_Label('', 'caption', metrics), 14)
        self.folder_list = self.add(_FolderList(metrics), 6)

    def retranslate(self) -> None:
        t = tr
        self.path_row.set_browse_text(t('onboarding.downloads_browse'))
        self.status_row.set_reset_text(t('onboarding.downloads_reset'))
        self.subfolders.set_texts(t('onboarding.downloads_subfolders'),
                                  t('onboarding.downloads_subfolders_desc'))
        self.folder_title.setText(t('onboarding.downloads_preview'))
        self.updateGeometry()

    def refresh_theme(self) -> None:
        super().refresh_theme()
        self.subfolders.refresh_theme()
        self.status_row.refresh_theme()

    def refresh(self) -> None:
        root = self._state.download_root or settings.download_dir
        self.path_row.set_path(root)
        self.status_row.set_reset_enabled(self._state.download_root is not None)
        free = free_space_bytes(root)
        if free is None:
            self.status_row.set_status('')
        else:
            self.status_row.set_status(
                f'{format_size(free)} {tr("onboarding.downloads_free")}')
        changes = download_dir_changes(root, self._state.download_subfolders)
        if self._state.download_subfolders:
            folders = [os.path.basename(changes[attr].rstrip('\\/')) or changes[attr]
                       for attr in DOWNLOAD_DIR_SUBFOLDERS]
        else:
            folders = [os.path.basename(os.path.normpath(root)) or root]
        self.folder_list.set_folders(folders)
        self.updateGeometry()

    def show_error(self, problem: str) -> None:
        self.status_row.set_status(
            f'{tr("onboarding.downloads_error")} {problem}', error=True)


class _PrefsPage(_Page):

    pref_changed = Signal(str, bool)
    volume_changed = Signal(str, int)

    def __init__(self, state: _WizardState, metrics: _Metrics, parent=None):
        super().__init__(metrics, parent)
        self._state = state
        self._metrics = metrics
        self.pref_cards: dict[str, _ToggleCard] = {}
        self.volume_cards: dict[str, _VolumeCard] = {}
        grid = _ResponsiveGrid(metrics.grid_gap, metrics.feature_min_col, 2, self)
        specs = (
            ('startup', FluentIcon.POWER_BUTTON),
            ('tray', FluentIcon.MINIMIZE),
            ('notify', FluentIcon.RINGER),
            ('external', FluentIcon.SEND),
        )
        for pref_id, icon in specs:
            card = _ToggleCard(themed_icon(None, icon, size=metrics.icon_px), '', '', metrics, grid)
            card.setChecked(bool(state.prefs.get(pref_id, False)))
            card.toggled.connect(lambda checked, p=pref_id: self.pref_changed.emit(p, checked))
            grid.add(card)
            self.pref_cards[pref_id] = card
        off_text = tr('settings.volume_off')
        for volume_id, icon in (('sfx', FluentIcon.VOLUME), ('ambiance', FluentIcon.MUSIC)):
            card = _VolumeCard(themed_icon(None, icon, size=metrics.icon_px), '', '',
                               int(state.volumes.get(volume_id, 0)), metrics, off_text, grid)
            card.changed.connect(lambda value, v=volume_id: self.volume_changed.emit(v, value))
            grid.add(card)
            self.volume_cards[volume_id] = card
        self.add(grid, 0)

    def retranslate(self) -> None:
        t = tr
        for pref_id, card in self.pref_cards.items():
            title = t(f'onboarding.pref_{pref_id}')
            desc = t(f'onboarding.pref_{pref_id}_desc')
            card.set_texts(title, desc)
        for volume_id, card in self.volume_cards.items():
            title = t(f'onboarding.pref_{volume_id}')
            desc = t(f'onboarding.pref_{volume_id}_desc')
            card.set_texts(title, desc)
        self.updateGeometry()

    def refresh_theme(self) -> None:
        super().refresh_theme()


def _feature_icon(slot: str):
    return {
        'nav.minerva': FluentIcon.APPLICATION,
        'nav.pc_games': FluentIcon.GAME,
        'nav.youtube': FluentIcon.PLAY,
        'nav.tv': FluentIcon.PROJECTOR,
        'nav.tbhb': FluentIcon.GLOBE,
        'nav.music': FluentIcon.MUSIC,
        'nav.books': FluentIcon.BOOK_SHELF,
        'nav.sailseek': FluentIcon.SEARCH,
    }.get(slot, FluentIcon.APPLICATION)


def _theme_options() -> list[tuple[str, str, tuple, str, str]]:
    options = [
        (mode, tr(f'settings.theme_{mode}'),
         tuple(palette(dark=mode == 'dark')[k] for k in _SWATCH_KEYS), '', '')
        for mode in _PLAIN_THEMES
    ]
    for preset_id in all_preset_ids():
        meta = preset_meta(preset_id)
        if meta is None:
            continue
        shader = meta.background_type == 'shader'
        media = (meta.background_shader_image_path if shader else meta.background_path) or ''
        ext = os.path.splitext(media)[1].lower()
        kind = 'shader' if shader else 'video' if ext in _VIDEO_EXT else 'gif' if ext == '.gif' else 'photo' if media else ''
        options.append((preset_id, preset_label(preset_id),
                        tuple(meta.palette[k] for k in _SWATCH_KEYS), media, kind))
    return options


def _is_custom_theme(option_id: str) -> bool:
    if option_id in _PLAIN_THEMES or option_id == '__draft__':
        return False
    meta = preset_meta(option_id)
    return meta is not None and bool(meta.dir)


def _build_theme_sections(page: '_Page', metrics: '_Metrics', current: str,
                          on_pick: Callable[[str], None]) -> tuple[dict, '_Label']:
    options = _theme_options()
    defaults = [o for o in options if not _is_custom_theme(o[0])]
    customs = [o for o in options if _is_custom_theme(o[0])]
    cards: dict[str, _ThemeCard] = {}

    def make_grid(items, min_items: int = 0) -> '_ResponsiveGrid':
        grid = _ResponsiveGrid(metrics.theme_gap, metrics.theme_min_col, 5, page, min_items=min_items)
        for option_id, label, swatch, media, kind in items:
            card = _ThemeCard(option_id, label, swatch, media, kind, metrics, grid)
            card.set_selected(option_id == current)
            card.clicked.connect(lambda _checked=False, o=option_id: on_pick(o))
            grid.add(card)
            cards[option_id] = card
        return grid

    page.add(make_grid(defaults), 0)
    header = _Label(tr('settings.theme_custom_header'), 'section', metrics)
    if customs:
        page.add(header, 24)
        page.add(make_grid(customs, min_items=5), 10)
    else:
        header.setParent(page)
        header.hide()
    return cards, header


def _activate_option(option_id: str) -> bool:
    plain = option_id in _PLAIN_THEMES
    if not plain and preset_meta(option_id) is None:
        return False
    activate_theme(None if plain else option_id, dark=(option_id == 'dark') if plain else None)
    return True


class ThemePickerDialog(_OverlayDialog):

    def __init__(self, owner: QWidget, current: str,
                 on_accept: Callable[[str], None] | None = None):
        super().__init__(owner)
        self._header.set_progress_visible(False)
        self.theme = current
        self._original = current
        self._on_accept = on_accept
        self._cards: dict[str, _ThemeCard] = {}
        self._pending: str | None = None
        self._previewed = current
        self._preview_timer = QTimer(self)
        self._preview_timer.setSingleShot(True)
        self._preview_timer.timeout.connect(self._apply_pending)
        self._build()
        self._header.set_texts(
            tr('onboarding.theme_title'),
            tr('onboarding.theme_subtitle'),
            '',
        )
        self._footer.set_texts(
            tr('common.cancel'),
            tr('onboarding.theme_continue'),
            True,
        )
        self._footer.next.clicked.connect(self.accept)
        self._footer.back.clicked.connect(self.reject)
        register_theme_refresh(self, self._refresh_theme)
        register_locale_refresh(self, self._retranslate)
        self._retranslate()
        self._refit()

    def _build(self) -> None:
        page = _Page(self._metrics)
        self._cards, self._custom_header = _build_theme_sections(
            page, self._metrics, self.theme, self._pick)
        self.set_page(page)
        self._page = page

    def _pick(self, option_id: str) -> None:
        if option_id == self.theme:
            return
        self.theme = option_id
        for oid, card in self._cards.items():
            card.set_selected(oid == option_id)
        self._pending = option_id
        self._preview_timer.start(0)

    def _apply_pending(self) -> None:
        option_id = self._pending
        self._pending = None
        if option_id is None:
            return
        if _activate_option(option_id):
            self._previewed = option_id

    def _flush_preview(self) -> None:
        if self._pending is not None:
            self._preview_timer.stop()
            self._apply_pending()

    def _restore_original(self) -> None:
        self._preview_timer.stop()
        self._pending = None
        if self._previewed != self._original:
            _activate_option(self._original)
            self._previewed = self._original

    def accept(self) -> None:
        self._flush_preview()
        try:
            if self._on_accept is not None and self.theme != self._original:
                self._on_accept(self.theme)
        finally:
            super().accept()

    def reject(self) -> None:
        self._restore_original()
        super().reject()

    def _retranslate(self) -> None:
        if not self._cards:
            return
        self._header.set_texts(
            tr('onboarding.theme_title'),
            tr('onboarding.theme_subtitle'),
            '',
        )
        self._footer.set_texts(
            tr('common.cancel'),
            tr('onboarding.theme_continue'),
            True,
        )
        header = getattr(self, '_custom_header', None)
        if header is not None:
            header.setText(tr('settings.theme_custom_header'))
        self._request_refit()

    def keyPressEvent(self, event) -> None:
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self.accept()
            event.accept()
            return
        if event.key() == Qt.Key.Key_Escape:
            self.reject()
            event.accept()
            return
        super().keyPressEvent(event)


def pick_theme(owner: QWidget, current: str,
               on_accept: Callable[[str], None] | None = None) -> str | None:
    prepare = getattr(owner, '_prepare_gl_surface', None)
    if callable(prepare):
        with suppress(Exception):
            prepare()
    dialog = ThemePickerDialog(owner, current, on_accept)
    try:
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return None
        return dialog.theme if dialog.theme != current else None
    finally:
        dialog.hide()
        dialog.setParent(None)
        dialog.deleteLater()


def run_onboarding(owner: QWidget) -> bool:
    wizard = OnboardingWizard(owner)
    try:
        accepted = wizard.exec() == QDialog.DialogCode.Accepted
        if accepted:
            wizard.commit()
        return accepted
    finally:
        wizard.hide()
        wizard.setParent(None)
        wizard.deleteLater()
