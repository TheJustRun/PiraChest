from __future__ import annotations

import ctypes
import hashlib
import html
import logging
import os
import random
import re
import sys
import time
import warnings

import shiboken6
from contextlib import suppress
from functools import partial
from typing import Any

from PySide6.QtCore import QEvent, QObject, QPoint, QRect, QStandardPaths, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QColor, QCursor, QDesktopServices, QIcon, QKeySequence
from PySide6.QtWidgets import (
    QAbstractButton,
    QAbstractItemView,
    QAbstractScrollArea,
    QApplication,
    QButtonGroup,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QStackedWidget,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)
from qfluentwidgets import (
    BodyLabel,
    CaptionLabel,
    CheckBox,
    ColorPickerButton,
    ComboBox,
    CompactSpinBox,
    ExpandGroupSettingCard,
    FluentIcon,
    FlowLayout,
    IndicatorPosition,
    InfoBar,
    InfoBarPosition,
    LineEdit,
    MessageBoxBase,
    PillPushButton,
    PrimaryPushButton,
    ProgressBar,
    PushButton,
    qconfig,
    setFontFamilies,
    SettingCard,
    SettingCardGroup,
    Slider,
    SmoothScrollArea,
    StrongBodyLabel,
    SwitchButton,
    SwitchSettingCard,
    TransparentToolButton,
)

from src.core.debrid import PROVIDER_INFO, PROVIDER_ORDER, PROVIDERS, token_store
from src.core.config import (
    APP_ACCENT_COLOR,
    APP_NAME,
    IS_CANARY,
    NAV_SOURCE_KEYS,
    nav_order,
    apply_settings,
    apply_settings_backup,
    associations_need_repair,
    check_dir_writable,
    default_download_dir,
    detect_windows_accent_color,
    format_size,
    get_launch_at_startup,
    is_admin,
    is_tab_enabled,
    load_settings_backup,
    register_associations,
    relaunch_app,
    relaunch_as_admin,
    save_settings,
    set_launch_at_startup,
    set_tab_enabled,
    settings,
    paths,
    SLUG,
    ThemeMode,
    VERSION,
    wipe_all_app_data,
    write_settings_backup,
)
from src.core.theme import (
    accent_locked,
    activate_theme,
    active_preset_id,
    apply_tool_button_theme,
    branded_icon,
    animations_enabled,
    ensure_contrast,
    fade_in,
    manager,
    to_color,
    notify_accent_changed,
    open_theme_maker,
    palette,
    preset_meta,
    register_theme_refresh,
    reload_custom_themes,
    resolve_font_families,
    settings_qss,
    theme_background_info,
    theme_sfx_path,
    themed_icon,
    ThemedSlider,
)
from src.core.translations import (
    available_languages,
    current_language,
    register_locale_refresh,
    set_language,
    tr,
)
from PySide6.QtCore import QEasingCurve, QPointF, QPropertyAnimation, QRectF
from PySide6.QtGui import QBrush, QConicalGradient, QFont, QFontMetrics, QLinearGradient, QPainter, QPainterPath, QPen
from PySide6.QtCore import QVariantAnimation
from PySide6.QtWidgets import QGraphicsDropShadowEffect, QGraphicsOpacityEffect, QScrollArea
from qfluentwidgets import isDarkTheme, themeColor


logger = logging.getLogger(__name__)


_GUI_DIR = os.path.dirname(os.path.abspath(__file__))
_SOURCE_TAB_ICON_FILENAMES = {'fitgirl': 'fg.png', 'gog': 'gr.png'}
_ASSOC_APP_REGISTRY_NAME = SLUG


def _repacks_tab_icon(source_key: str) -> QIcon | None:
    filename = _SOURCE_TAB_ICON_FILENAMES.get(source_key)
    if not filename:
        return None
    path = os.path.join(_GUI_DIR, 'photos', filename)
    if not os.path.isfile(path):
        return None
    icon = QIcon(path)
    return icon if not icon.isNull() else None


def set_debug_console_visible(visible: bool) -> None:
    if os.name != 'nt':
        return
    kernel32 = ctypes.windll.kernel32
    user32 = ctypes.windll.user32
    hwnd = kernel32.GetConsoleWindow()
    root_logger = logging.getLogger()
    if visible and not hwnd:
        if kernel32.AllocConsole():
            hwnd = kernel32.GetConsoleWindow()
            with suppress(Exception):
                sys.stdout = open('CONOUT$', 'w', buffering=1, encoding='utf-8', errors='replace')
                sys.stderr = open('CONOUT$', 'w', buffering=1, encoding='utf-8', errors='replace')
                sys.stdin = open('CONIN$')
            for h in root_logger.handlers:
                if isinstance(h, logging.StreamHandler) and not isinstance(h, logging.FileHandler):
                    h.stream = sys.stdout
            if not root_logger.handlers:
                handler = logging.StreamHandler(sys.stdout)
                handler.setFormatter(logging.Formatter('%(asctime)s [%(levelname)s] %(name)s — %(message)s'))
                root_logger.addHandler(handler)
        root_logger.setLevel(logging.DEBUG)
        for name in logging.root.manager.loggerDict:
            if name == 'src' or name.startswith('src.'):
                logging.getLogger(name).setLevel(logging.NOTSET)
    elif not visible and hwnd:
        root_logger.setLevel(logging.WARNING)
        for name in logging.root.manager.loggerDict:
            if name == 'src' or name.startswith('src.'):
                logging.getLogger(name).setLevel(logging.NOTSET)
    if hwnd:
        SW_SHOW, SW_HIDE = 5, 0
        user32.ShowWindow(hwnd, SW_SHOW if visible else SW_HIDE)


class UpdateController(QObject):
    check_started = Signal()
    check_finished = Signal(object)
    download_progress = Signal(int, int)
    download_finished = Signal(str)
    download_error = Signal(str)
    download_stage = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._download_ticket = None
        self.current_tag = ''

    def disconnect_listeners(self) -> None:
        with warnings.catch_warnings():
            warnings.simplefilter('ignore', RuntimeWarning)
            for signal in (self.check_finished, self.download_progress,
                           self.download_finished, self.download_error):
                with suppress(RuntimeError, TypeError):
                    signal.disconnect()

    def check(self, channel: str | None = None) -> None:
        from src.core import updater as _updater
        from src.core import worker as _worker
        if channel is None:
            channel = "auto" if (IS_CANARY or settings.canary_updates) else "stable"

        def _job():
            try:
                return _updater.check_for_update(channel=channel)
            except Exception:
                return None
        self.check_started.emit()
        _worker.submit(_job, on_done=self.check_finished.emit, on_error=lambda _m: self.check_finished.emit(None),
                       owner=self, key=('update-check', channel), priority=_worker.Priority.NORMAL)

    def download(self, update_info: dict) -> None:
        from src.core import updater as _updater
        from src.core import worker as _worker

        self.current_tag = str(update_info.get('tag') or '')

        def _job(is_cancelled=None):
            progress = _worker.wrap_callback(self.download_progress.emit, owner=self)
            stage = _worker.wrap_callback(self.download_stage.emit, owner=self)
            return _updater.download_and_verify_update(update_info, on_progress=progress, is_cancelled=is_cancelled, on_stage=stage)

        def _error(message: str) -> None:
            self.download_error.emit(message)
        self._download_ticket = _worker.submit(_job, on_done=self.download_finished.emit, on_error=_error,
                                               owner=self, key=('update-download', update_info.get('tag')),
                                               priority=_worker.Priority.HIGH, pool=_worker.DL)

    def cancel_download(self) -> None:
        from src.core import worker as _worker
        _worker.cancel(self._download_ticket)
        self._download_ticket = None

    def apply_and_restart(self, path: str, window: QWidget) -> None:
        from src.core import updater as _updater
        if not getattr(sys, "frozen", False):
            logger.warning("Skipping real self-update swap: not running from a frozen build")
            try:
                _updater.cleanup_update_staging(path)
            except Exception:
                logger.exception("Failed to clean up update staging in dev mode")
        else:
            _updater.apply_update_and_restart(path)
        if hasattr(window, '_force_quit'):
            window._force_quit = True
        window.close()


update_controller = UpdateController()


_CL_SKIP = re.compile(r'^(?:\*{0,2}(?:Install|Portable):\*{0,2}.*|.+\(Windows x64\)\.?|-{3,}|\*{3,})$', re.I)
_CL_HEAD = re.compile(r'^(#{1,6})\s+(.*?)\s*#*$')
_CL_ITEM = re.compile(r'^(\s*)(?:[*+-]|\d+\.)\s+(.*)$')
_CL_REF = re.compile(r'\s*\(#(\d+)\)$')
_CL_PULL = re.compile(r'/(?:pull|issues)/(\d+)$')
_CL_TOKENS = re.compile(r'\[([^\]]+)\]\((https?://[^)\s]+)\)|(?<!["=>])(https?://[^\s<]+)')
_CL_CODE = re.compile(r'(`[^`]+`)')
_CL_BOLD = re.compile(r'\*\*(.+?)\*\*')
_CL_ITALIC = re.compile(r'(?<!\*)\*([^*\s][^*]*?)\*(?!\*)')


def _cl_link(m: re.Match) -> str:
    if m.group(1):
        return f'<a href="{m.group(2)}">{m.group(1)}</a>'
    url = m.group(3).rstrip('.,;:!?)')
    tail = m.group(3)[len(url):]
    ref = _CL_PULL.search(url)
    bare = url.split('://', 1)[1]
    label = f'#{ref.group(1)}' if ref else (bare if len(bare) <= 48 else bare[:47] + '\u2026')
    return f'<a href="{url}">{label}</a>{tail}'


def _cl_inline(text: str) -> str:
    out = []
    for i, part in enumerate(_CL_CODE.split(html.escape(text, quote=False))):
        if i & 1:
            out.append(f'<code>&nbsp;{part[1:-1]}&nbsp;</code>')
        else:
            part = _CL_TOKENS.sub(_cl_link, part)
            part = _CL_BOLD.sub(r'<b>\1</b>', part)
            out.append(_CL_ITALIC.sub(r'<i>\1</i>', part))
    return ''.join(out)


def _changelog_markdown_to_html(text: str) -> str:
    pal = palette()
    muted = _pal('muted', '#888888').name()
    heads = (
        f'<p style="margin:22px 0 6px 0; font-size:15px; font-weight:600; line-height:130%; color:{_pal("accent", APP_ACCENT_COLOR).name()};">',
        f'<p style="margin:18px 0 4px 0; font-size:14px; font-weight:600; line-height:130%; color:{to_color(pal.get("dialog_title_text") or pal.get("primary_text"), "#ffffff").name()};">',
    )
    out = []
    open_li = [False] * 6

    def close(to: int) -> None:
        for d in range(len(open_li) - 1, to, -1):
            if open_li[d]:
                out.append('</li></ul>')
                open_li[d] = False
            elif d <= depth[0]:
                out.append('</ul>')
        depth[0] = min(depth[0], to)

    depth = [0]
    for raw in text.replace('\r\n', '\n').split('\n'):
        s = raw.strip()
        if not s or _CL_SKIP.match(s):
            continue
        m = _CL_HEAD.match(s)
        if m:
            close(0)
            if open_li[0]:
                out.append('</li>')
                open_li[0] = False
            ref = _CL_REF.search(m.group(2))
            head = heads[len(m.group(1)) > 2]
            label = _cl_inline(_CL_REF.sub('', m.group(2)))
            if ref:
                label += f' <span style="color:{muted}; font-weight:400;">#{ref.group(1)}</span>'
            out.append(f'{head}{label}</p>')
            continue
        m = _CL_ITEM.match(raw.rstrip())
        if m:
            level = min(len(m.group(1).replace('\t', '  ')) // 2, 3) + 1
            close(level)
            if open_li[level]:
                out.append('</li>')
                open_li[level] = False
            while depth[0] < level:
                depth[0] += 1
                out.append(f'<ul style="-qt-list-indent:{depth[0]};">')
            item = m.group(2).replace('[ ] ', '\u2610 ', 1).replace('[x] ', '\u2611 ', 1)
            out.append(f'<li>{_cl_inline(item)}')
            open_li[level] = True
            continue
        close(0)
        out.append(f'<p>{_cl_inline(s)}</p>')
    close(0)
    return ''.join(out)


def _mix(a: QColor, b: QColor, t: float) -> QColor:
    return QColor(*(round(x + (y - x) * t) for x, y in zip(a.getRgb()[:3], b.getRgb()[:3])))


def _alpha(c: QColor, a: int) -> str:
    c = QColor(c)
    c.setAlpha(a)
    return f'rgba({c.red()}, {c.green()}, {c.blue()}, {a})'


def _changelog_css(body: QColor, strong: QColor, accent: QColor, code_bg: QColor) -> str:
    return (
        f'p, li {{ color: {body.name()}; line-height: 150%; }}'
        'p { margin: 4px 0; }'
        f'b {{ color: {strong.name()}; font-weight: 600; }}'
        'ul { margin: 4px 0 6px -22px; }'
        'li { margin: 4px 0; }'
        f'code {{ background-color: {code_bg.name()}; font-family: Consolas, "Cascadia Mono", monospace; font-size: 12px; }}'
        f'a {{ color: {accent.name()}; text-decoration: none; }}'
    )


class _NotesView(QTextBrowser):
    _FADE = 36

    def __init__(self, bg: QColor, parent=None):
        super().__init__(parent)
        self._bg = bg
        self.setOpenExternalLinks(True)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setViewportMargins(0, 0, 10, 0)
        self.document().setDocumentMargin(0)
        self.verticalScrollBar().valueChanged.connect(self.viewport().update)

    def paintEvent(self, event) -> None:
        super().paintEvent(event)
        bar = self.verticalScrollBar()
        if bar.maximum() <= 0:
            return
        port = self.viewport()
        p = QPainter(port)
        solid, clear = QColor(self._bg), QColor(self._bg)
        clear.setAlpha(0)
        h = self._FADE
        if bar.value() > 0:
            g = QLinearGradient(0, 0, 0, h)
            g.setColorAt(0, solid)
            g.setColorAt(1, clear)
            p.fillRect(0, 0, port.width(), h, g)
        if bar.value() < bar.maximum():
            y = port.height() - h
            g = QLinearGradient(0, y, 0, port.height())
            g.setColorAt(0, clear)
            g.setColorAt(1, solid)
            p.fillRect(0, y, port.width(), h, g)


def show_update_dialog(parent: QWidget, result: dict, allow_skip: bool = True) -> str:
    pal = palette()
    bg = _pal('dialog_bg', '#202020')
    body = _pal('body_text', '#dddddd')
    strong = to_color(pal.get('dialog_title_text') or pal.get('primary_text'), '#ffffff')
    accent = _pal('accent', APP_ACCENT_COLOR)
    muted = _pal('muted', '#888888')
    box = MessageBoxBase(parent)
    box.widget.setMinimumWidth(740)
    with suppress(AttributeError):
        box.viewLayout.setContentsMargins(28, 26, 28, 4)
        box.buttonGroup.setFixedHeight(76)
        box.buttonLayout.setContentsMargins(28, 14, 28, 26)
    layout = QVBoxLayout()
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(0)
    title = QLabel(tr('update.available_title', tag=result['tag']))
    title.setWordWrap(True)
    title.setStyleSheet(f'QLabel {{ background: transparent; color: {strong.name()}; font-size: 17px; font-weight: 600; }}')
    layout.addWidget(title)
    layout.addSpacing(5)
    channel = tr_or('update.channel_canary', 'Canary') if result.get('canary') else tr_or('update.channel_stable', 'Stable')
    size = int(result.get('asset_size') or 0)
    meta = QLabel(f'<span style="color:{accent.name()}; font-weight:600;">{html.escape(channel)}</span>'
                  + (f'&nbsp;&nbsp;\u00b7&nbsp;&nbsp;{_fmt_bytes(size)}' if size else ''))
    meta.setStyleSheet(f'QLabel {{ background: transparent; color: {muted.name()}; font-size: 12px; }}')
    layout.addWidget(meta)
    if result.get('canary'):
        layout.addSpacing(6)
        notice = QLabel(tr('update.canary_notice',
                           'Experimental PiraChest Canary build. It can be unstable, but stable releases are still offered when they are newer.'))
        notice.setWordWrap(True)
        notice.setStyleSheet(f'QLabel {{ background: transparent; color: {muted.name()}; font-size: 12px; }}')
        layout.addWidget(notice)
    layout.addSpacing(16)
    view = _NotesView(bg, box)
    view.document().setDefaultStyleSheet(_changelog_css(_mix(body, bg, 0.16), strong, accent, _mix(bg, body, 0.12)))
    notes = _changelog_markdown_to_html((result.get('notes') or '')[:8000]) or f'<p>{html.escape(tr("update.no_notes"))}</p>'
    view.setHtml(notes + '<p style="margin:0; font-size:14px;">&nbsp;</p>')
    view.setStyleSheet(
        'QTextBrowser { background: transparent; border: none; font-size: 13px; }'
        f'QTextBrowser {{ selection-background-color: {_alpha(accent, 90)}; }}'
        'QScrollBar:vertical { width: 6px; background: transparent; margin: 0; }'
        f'QScrollBar::handle:vertical {{ background: {_alpha(body, 50)}; border-radius: 3px; min-height: 28px; }}'
        f'QScrollBar::handle:vertical:hover {{ background: {_alpha(body, 100)}; }}'
        'QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }'
        'QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical { background: transparent; }')
    view.setMinimumHeight(400)
    layout.addWidget(view, 1)
    skip_chk = None
    if allow_skip:
        layout.addSpacing(14)
        skip_chk = CheckBox(tr('update.never_show_this_version'))
        layout.addWidget(skip_chk)
    box.viewLayout.addLayout(layout)
    box.yesButton.setText(tr('update.update_now'))
    box.cancelButton.setText(tr('update.later'))
    accepted = bool(box.exec())
    if skip_chk is not None and skip_chk.isChecked():
        settings.skip_update_tag = result['tag']
        save_settings(settings)
    return 'update' if accepted else 'later'


def _fmt_bytes(n: float) -> str:
    n = float(max(0, n))
    for unit in ('B', 'KB', 'MB', 'GB'):
        if n < 1024 or unit == 'GB':
            return f'{n:.0f} {unit}' if unit == 'B' else f'{n:.1f} {unit}'
        n /= 1024
    return ''


def _fmt_eta(seconds: float) -> str:
    seconds = int(seconds + 0.5)
    if seconds < 1:
        return '<1s'
    if seconds < 60:
        return f'{seconds}s'
    return f'{seconds // 60}m {seconds % 60:02d}s'


def _pal(key: str, fallback: str) -> QColor:
    return to_color(palette().get(key) or fallback, fallback)


_UPDATE_WEIGHTS = (0.52, 0.16, 0.18, 0.14)


def _css_rgba(c: QColor) -> str:
    return f'rgba({c.red()}, {c.green()}, {c.blue()}, {c.alpha()})'


def _tabular(font: QFont) -> QFont:
    try:
        font.setFeature(QFont.Tag('tnum'), 1)
    except Exception:
        pass
    return font


def _dialog_radius() -> float:
    try:
        return float(re.sub(r'[^0-9.]', '', str(manager.layout().get('dialog_radius', '12'))) or 12)
    except Exception:
        return 12.0


class _UpdateRail(QWidget):
    percent_changed = Signal(int)

    def __init__(self, labels: list, parent=None):
        super().__init__(parent)
        self._labels = labels
        self._stage = 0
        self._shown = [0.0, 0.0, 0.0, 0.0]
        self._from = list(self._shown)
        self._to = list(self._shown)
        self._pct = 0
        self._sweep = 0.0
        self.setFixedHeight(32)
        self._anim = QVariantAnimation(self)
        self._anim.setDuration(240)
        self._anim.setStartValue(0.0)
        self._anim.setEndValue(1.0)
        self._anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._anim.valueChanged.connect(self._on_anim)
        self._loop = QVariantAnimation(self)
        self._loop.setDuration(1500)
        self._loop.setStartValue(0.0)
        self._loop.setEndValue(1.0)
        self._loop.setLoopCount(-1)
        self._loop.setEasingCurve(QEasingCurve.Type.InOutSine)
        self._loop.valueChanged.connect(self._on_loop)

    def _on_anim(self, t) -> None:
        t = float(t)
        self._shown = [a + (b - a) * t for a, b in zip(self._from, self._to)]
        pct = int(self._shown[0] * 100 + 0.5)
        if pct != self._pct:
            self._pct = pct
            self.percent_changed.emit(pct)
        self.update()

    def _on_loop(self, v) -> None:
        self._sweep = float(v)
        self.update()

    def _retarget(self) -> None:
        self._anim.stop()
        self._from = list(self._shown)
        if not animations_enabled():
            self._shown = list(self._to)
            self._on_anim(1.0)
            return
        self._anim.start()

    def set_fraction(self, fraction: float) -> None:
        fraction = max(0.0, min(1.0, fraction))
        if self._stage == 0 and abs(self._to[0] - fraction) < 0.001:
            return
        self._to[0] = fraction
        self._retarget()

    def set_stage(self, stage: int) -> None:
        self._stage = stage
        self._to = [1.0 if i < stage else (self._to[0] if i == 0 else 0.0) for i in range(4)]
        self._retarget()
        self._sync_loop()

    def _sync_loop(self) -> None:
        if self._stage >= 1 and self.isVisible() and animations_enabled():
            if self._loop.state() != QVariantAnimation.State.Running:
                self._loop.start()
        else:
            self._loop.stop()

    def start(self) -> None:
        self._sync_loop()

    def stop(self) -> None:
        self._anim.stop()
        self._loop.stop()

    def reset(self) -> None:
        self.stop()
        self._stage = 0
        self._shown = [0.0] * 4
        self._from = [0.0] * 4
        self._to = [0.0] * 4
        self._pct = 0
        self.update()

    def showEvent(self, event) -> None:
        super().showEvent(event)
        self._sync_loop()

    def hideEvent(self, event) -> None:
        super().hideEvent(event)
        self._loop.stop()

    def paintEvent(self, _event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        bg = to_color(palette().get('dialog_bg'), '#1a1a1a')
        accent = ensure_contrast(_pal('accent', '#c9c9c9'), bg, 3.0)
        track = to_color(palette().get('surface_border'), '#33ffffff')
        done = to_color(palette().get('body_text'), '#dddddd')
        live = to_color(palette().get('primary_text'), '#ffffff')
        faint = to_color(palette().get('faint_text'), '#777777')
        gap, thick, top = 4.0, 4.0, 2.0
        usable = self.width() - gap * 3
        radius = min(thick / 2, _dialog_radius())
        font = p.font()
        font.setPixelSize(11)
        p.setFont(font)
        fm = QFontMetrics(font)
        x = 0.0
        for i, label in enumerate(self._labels):
            w = usable * _UPDATE_WEIGHTS[i]
            seg = QRectF(x, top, w, thick)
            clip = QPainterPath()
            clip.addRoundedRect(seg, radius, radius)
            p.fillPath(clip, track)
            fill = self._shown[i]
            active = i == self._stage
            if active and self._stage >= 1 and fill < 1.0:
                if animations_enabled():
                    span = w * 0.5
                    start = seg.left() - span + (w + span) * self._sweep
                    grad = QLinearGradient(start, 0, start + span, 0)
                    clear = QColor(accent)
                    clear.setAlpha(0)
                    grad.setColorAt(0.0, clear)
                    grad.setColorAt(0.5, accent)
                    grad.setColorAt(1.0, clear)
                    p.save()
                    p.setClipPath(clip)
                    p.fillRect(seg, QBrush(grad))
                    p.restore()
                else:
                    soft = QColor(accent)
                    soft.setAlpha(110)
                    p.fillPath(clip, soft)
            if fill > 0.001:
                p.save()
                p.setClipPath(clip)
                p.fillRect(QRectF(seg.left(), seg.top(), w * min(1.0, fill), thick), accent)
                p.restore()
            complete = i < self._stage
            p.setPen(live if active else (done if complete else faint))
            tx = x
            if complete:
                cy = top + thick + 14.0
                p.setPen(QPen(accent, 1.5, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
                p.drawPolyline([QPointF(x + 0.5, cy), QPointF(x + 2.7, cy + 2.4), QPointF(x + 6.5, cy - 2.4)])
                p.setPen(done)
                tx = x + 11
            text = fm.elidedText(label, Qt.TextElideMode.ElideRight, int(max(10, w + gap - (tx - x))))
            p.drawText(QRectF(tx, top + thick + 6, w + gap, 16), int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter), text)
            x += w + gap


class _UpdateCard(QFrame):
    def paintEvent(self, _event) -> None:
        dark = isDarkTheme()
        pal = palette()
        base = to_color(pal.get('dialog_bg') or pal.get('card_bg'), '#26262b' if dark else '#fafafa')
        base.setAlpha(255)
        edge = QColor(255, 255, 255, 38) if dark else QColor(0, 0, 0, 38)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(QPen(edge, 1))
        painter.setBrush(base)
        painter.drawRoundedRect(QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5), 10, 10)


class UpdateOverlay(QWidget):
    _STAGE_KEYS = {'download': 0, 'verify': 1, 'extract': 2}
    _RISE = 10

    def __init__(self, parent: QWidget):
        super().__init__(parent)
        self.setVisible(False)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self._dim = 0
        self._stage = 0
        self._last = (0.0, 0)
        self._speed = 0.0
        self._stats_at = 0.0
        self._closing = False
        self._fx = None
        self._texts = [
            tr('update.stage_download', 'Downloading update'),
            tr('update.stage_verify', 'Verifying download'),
            tr('update.stage_prepare', 'Preparing files'),
            tr('update.stage_restart', 'Restarting PiraChest'),
        ]
        self._card = _UpdateCard(self)
        self._card.setObjectName('pirachestUpdateCard')
        self._card.setFixedWidth(460)
        layout = QVBoxLayout(self._card)
        layout.setContentsMargins(32, 28, 32, 24)
        layout.setSpacing(0)
        head = QHBoxLayout()
        head.setSpacing(16)
        titles = QVBoxLayout()
        titles.setSpacing(3)
        self._title_lbl = QLabel(self._texts[0], self._card)
        self._version_lbl = QLabel('', self._card)
        titles.addWidget(self._title_lbl)
        titles.addWidget(self._version_lbl)
        head.addLayout(titles, 1)
        self._pct_lbl = QLabel('0%', self._card)
        self._pct_lbl.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        pct_font = _tabular(QFont(self._pct_lbl.font()))
        pct_font.setPixelSize(24)
        pct_font.setWeight(QFont.Weight.DemiBold)
        self._pct_lbl.setFont(pct_font)
        self._pct_lbl.setFixedWidth(QFontMetrics(pct_font).horizontalAdvance('100%') + 4)
        head.addWidget(self._pct_lbl, 0, Qt.AlignmentFlag.AlignTop)
        layout.addLayout(head)
        layout.addSpacing(22)
        self._rail = _UpdateRail([
            tr('update.step_download', 'Download'), tr('update.step_verify', 'Verify'),
            tr('update.step_prepare', 'Prepare'), tr('update.step_restart', 'Restart')], self._card)
        self._rail.percent_changed.connect(self._on_percent)
        layout.addWidget(self._rail)
        layout.addSpacing(10)
        stats = QHBoxLayout()
        stats.setSpacing(12)
        self._bytes_lbl = QLabel('', self._card)
        self._rate_lbl = QLabel('', self._card)
        self._rate_lbl.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        for lbl in (self._bytes_lbl, self._rate_lbl):
            f = _tabular(QFont(lbl.font()))
            f.setPixelSize(12)
            lbl.setFont(f)
            lbl.setMinimumHeight(16)
        stats.addWidget(self._bytes_lbl, 1)
        stats.addWidget(self._rate_lbl, 0)
        layout.addLayout(stats)
        layout.addSpacing(20)
        foot = QHBoxLayout()
        foot.addStretch(1)
        self._cancel_btn = PushButton(tr('update.cancel', 'Cancel'), self._card)
        self._cancel_btn.setMinimumWidth(96)
        policy = self._cancel_btn.sizePolicy()
        policy.setRetainSizeWhenHidden(True)
        self._cancel_btn.setSizePolicy(policy)
        self._cancel_btn.clicked.connect(self._on_cancel)
        foot.addWidget(self._cancel_btn)
        layout.addLayout(foot)
        self._outer = QVBoxLayout(self)
        self._outer.setContentsMargins(0, 0, 0, 0)
        self._outer.addStretch(1)
        row = QHBoxLayout()
        row.addStretch(1)
        row.addWidget(self._card)
        row.addStretch(1)
        self._outer.addLayout(row)
        self._outer.addStretch(1)
        self._fade = QVariantAnimation(self)
        self._fade.setStartValue(0.0)
        self._fade.setEndValue(1.0)
        self._fade.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._fade.valueChanged.connect(self._on_fade)
        self._fade.finished.connect(self._on_fade_done)
        parent.installEventFilter(self)
        update_controller.download_stage.connect(self._on_stage)
        register_theme_refresh(self, self._restyle)
        self._restyle()

    def eventFilter(self, obj, event) -> bool:
        if event.type() == QEvent.Type.Resize and obj is self.parentWidget() and self.isVisible():
            self.setGeometry(obj.rect())
        return False

    def _restyle(self) -> None:
        pal = palette()
        self._card.setStyleSheet('#pirachestUpdateCard{background: transparent; border: none;}')
        primary = _css_rgba(to_color(pal.get('dialog_title_text') or pal.get('primary_text'), '#ffffff'))
        body = _css_rgba(to_color(pal.get('dialog_body_text') or pal.get('body_text'), '#dddddd'))
        muted = _css_rgba(to_color(pal.get('muted'), '#999999'))
        self._title_lbl.setStyleSheet(f'color: {primary}; background: transparent; font-size: 15px; font-weight: 600;')
        self._version_lbl.setStyleSheet(f'color: {muted}; background: transparent; font-size: 12px;')
        self._pct_lbl.setStyleSheet(f'color: {primary}; background: transparent;')
        self._bytes_lbl.setStyleSheet(f'color: {body}; background: transparent;')
        self._rate_lbl.setStyleSheet(f'color: {muted}; background: transparent;')
        self._rail.update()
        self.update()

    def _scrim_alpha(self) -> int:
        return 135 if isDarkTheme() else 85

    def _attach_fx(self, opacity: float) -> None:
        self._fx = QGraphicsOpacityEffect(self._card)
        self._fx.setOpacity(opacity)
        self._card.setGraphicsEffect(self._fx)

    def _drop_fx(self) -> None:
        if self._fx is None:
            return
        self._fx = None
        with suppress(RuntimeError):
            self._card.setGraphicsEffect(None)

    def _on_fade(self, value) -> None:
        value = float(value)
        self._dim = int(self._scrim_alpha() * value)
        if self._fx is not None:
            with suppress(RuntimeError):
                self._fx.setOpacity(value)
        self._outer.setContentsMargins(0, int(round((1.0 - value) * self._RISE * 2)), 0, 0)
        self.update()

    def _on_fade_done(self) -> None:
        self._drop_fx()
        if self._closing:
            self._closing = False
            self.setVisible(False)
            self._rail.reset()

    def _on_percent(self, pct: int) -> None:
        self._pct_lbl.setText(f'{pct}%')

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor(0, 0, 0, self._dim))

    def mousePressEvent(self, event) -> None:
        event.accept()

    def keyPressEvent(self, event) -> None:
        if event.key() == Qt.Key.Key_Escape and self._cancel_btn.isVisible():
            self._on_cancel()
        event.accept()

    def _on_cancel(self) -> None:
        update_controller.cancel_download()
        self.hide_overlay()

    def _set_stage(self, stage: int) -> None:
        stage = max(0, min(3, stage))
        if stage < self._stage:
            return
        self._stage = stage
        self._title_lbl.setText(self._texts[stage])
        self.setAccessibleName(self._texts[stage])
        self._rail.set_stage(stage)
        self._cancel_btn.setVisible(stage < 2)
        if stage >= 1:
            self._bytes_lbl.setText('')
            self._rate_lbl.setText('')

    def _on_stage(self, key: str) -> None:
        stage = self._STAGE_KEYS.get(key)
        if stage is not None and self.isVisible():
            self._set_stage(stage)

    def set_title(self, text: str) -> None:
        return

    def set_indeterminate(self) -> None:
        self._set_stage(3)

    def set_progress(self, downloaded: int, total: int) -> None:
        if self._stage > 0:
            return
        if total <= 0:
            self._bytes_lbl.setText(_fmt_bytes(downloaded))
            return
        self._rail.set_fraction(downloaded / total)
        now = time.monotonic()
        last_t, last_b = self._last
        if last_t and now > last_t:
            inst = (downloaded - last_b) / (now - last_t)
            self._speed = inst if not self._speed else self._speed * 0.8 + inst * 0.2
        self._last = (now, downloaded)
        if now - self._stats_at >= 0.25 or downloaded >= total:
            self._stats_at = now
            self._bytes_lbl.setText(f'{_fmt_bytes(downloaded)} of {_fmt_bytes(total)}')
            rate = []
            if self._speed > 1024:
                rate.append(f'{_fmt_bytes(self._speed)}/s')
                if downloaded < total:
                    rate.append(tr('update.eta', '{time} left', time=_fmt_eta((total - downloaded) / self._speed)))
            self._rate_lbl.setText('  \u00b7  '.join(rate))

    def show_overlay(self, title: str | None = None) -> None:
        self._fade.stop()
        self._closing = False
        self._drop_fx()
        self._stage = 0
        self._last = (0.0, 0)
        self._speed = 0.0
        self._rail.reset()
        self._pct_lbl.setText('0%')
        self._set_stage(0)
        self._cancel_btn.setVisible(True)
        self._bytes_lbl.setText('')
        self._rate_lbl.setText('')
        tag = getattr(update_controller, 'current_tag', '')
        self._version_lbl.setText(f'{APP_NAME} {tag}' if tag else '')
        self.setGeometry(self.parentWidget().rect())
        self.raise_()
        self.setVisible(True)
        self.setFocus(Qt.FocusReason.PopupFocusReason)
        self._rail.start()
        if animations_enabled():
            self._attach_fx(0.0)
            self._on_fade(0.0)
            self._fade.setDirection(QVariantAnimation.Direction.Forward)
            self._fade.setDuration(220)
            self._fade.start()
        else:
            self._on_fade(1.0)

    def hide_overlay(self) -> None:
        self._fade.stop()
        self._rail.stop()
        if not self.isVisible() or not animations_enabled():
            self._drop_fx()
            self.setVisible(False)
            self._rail.reset()
            return
        self._closing = True
        self._attach_fx(1.0)
        self._fade.setDirection(QVariantAnimation.Direction.Backward)
        self._fade.setDuration(140)
        self._fade.start()


def make_smooth_scroll_area(parent=None):
    scroll = SmoothScrollArea(parent) if parent is not None else SmoothScrollArea()
    with suppress(AttributeError):
        scroll.setViewportUpdateMode(QAbstractScrollArea.ViewportUpdateMode.SmartViewportUpdate)
    with suppress(Exception):
        vbar = scroll.verticalScrollBar()
        vbar.setSingleStep(30)
    return scroll


_SETTING_CARD_ICON_PX = 20


def _fix_setting_card_icon_box(icon_label) -> None:
    if icon_label.width() == _SETTING_CARD_ICON_PX:
        return
    old = icon_label.size().width()
    icon_label.setFixedSize(_SETTING_CARD_ICON_PX, _SETTING_CARD_ICON_PX)
    shift = (_SETTING_CARD_ICON_PX - old) // 2
    if shift:
        geo = icon_label.geometry()
        icon_label.move(geo.x() - shift, geo.y() - shift)


_SS_GROUP_TITLE = 'color: palette(text); font-weight: 600; font-size: 16px; background: transparent;'
_SS_TEXT = 'color: palette(text); background: transparent;'
_SS_MUTED = 'color: palette(placeholder-text); background: transparent;'


def _set_ss(widget, sheet: str) -> None:
    if widget.styleSheet() != sheet:
        widget.setStyleSheet(sheet)


def _restyle_setting_cards(root: QWidget) -> None:
    for group in root.findChildren(SettingCardGroup):
        title = getattr(group, 'titleLabel', None)
        if title is not None:
            _set_ss(title, _SS_GROUP_TITLE)
    for card in root.findChildren(SettingCard):
        title = getattr(card, 'titleLabel', None)
        content = getattr(card, 'contentLabel', None)
        icon_label = getattr(card, 'iconLabel', None)
        if title is not None:
            _set_ss(title, _SS_TEXT if card.isEnabled() else _SS_MUTED)
        if content is not None:
            _set_ss(content, _SS_MUTED)
        if icon_label is not None:
            with suppress(Exception):
                _fix_setting_card_icon_box(icon_label)


_SFX_DIR = os.path.join(_GUI_DIR, 'sfx')
_SFX_FILENAMES = {
    'hover': 'hover.wav',
    'click': 'click.wav',
    'navigate': 'navigate.wav',

    'popout_info': 'popout_info.wav',
    'popout_success': 'popout_success.wav',
    'popout_warning': 'popout_warning.wav',
    'popout_error': 'popout_error.wav',
}
_AMBIANCE_FILENAME = 'ambiance.mp3'
_SFX_DEFAULT_VOLUME = 25
_AMBIANCE_DEFAULT_VOLUME = 45

_AMBIANCE_TICK_MS = 400
_AMBIANCE_FADE_MS = 500
_AMBIANCE_FADE_STEP_MS = 40
_AMBIANCE_START_TIMEOUT = 5.0


_HOVER_MIN_GAP = 0.04
_SFX_VOICES = {
    'hover': 4, 'click': 4, 'navigate': 3,
    'popout_info': 3, 'popout_success': 3, 'popout_warning': 3, 'popout_error': 3,
}
_SFX_TARGET_RMS = 0.14
_NORM_CACHE: dict = {}
_PITCH_SPAN = 0.006
_PITCH_OFFSETS = (-1.0, 1.0, -0.5, 0.5, -0.75, 0.75)
_PITCH_CACHE: dict = {}
_PITCH_MAX_FILES = 128


def _gain(volume: int) -> float:
    return (volume / 100.0) ** 2


def _norm_gain(path: str) -> float:
    try:
        stamp = (path, os.path.getmtime(path))
    except OSError:
        return 1.0
    cached = _NORM_CACHE.get(stamp)
    if cached is not None:
        return cached
    gain = 1.0
    try:
        import wave
        from array import array
        with wave.open(path, 'rb') as wav:
            if wav.getsampwidth() == 2:
                samples = array('h')
                samples.frombytes(wav.readframes(wav.getnframes()))
                if sys.byteorder == 'big':
                    samples.byteswap()
                if samples:
                    rms = (sum(s * s for s in samples) / len(samples)) ** 0.5 / 32768.0
                    if rms > 1e-4:
                        gain = max(0.2, min(4.0, _SFX_TARGET_RMS / rms))
    except Exception:
        gain = 1.0
    _NORM_CACHE[stamp] = gain
    return gain


def _pitch_offsets(count: int) -> tuple[float, ...]:
    shifts = []
    for index in range(max(0, count - 1)):
        shifts.append(_PITCH_SPAN * _PITCH_OFFSETS[index % len(_PITCH_OFFSETS)])
    return tuple(shifts)


def _prune_pitch_cache(directory: str) -> None:
    try:
        files = sorted(
            (os.path.join(directory, name) for name in os.listdir(directory) if name.endswith('.wav')),
            key=os.path.getmtime,
        )
        for old in files[:-_PITCH_MAX_FILES]:
            os.remove(old)
    except OSError:
        pass


def _pitch_variant(path: str, ratio: float) -> str:
    if not path or abs(ratio - 1.0) < 1e-4 or not path.lower().endswith('.wav'):
        return path
    try:
        stat = os.stat(path)
        key = (os.path.abspath(path), stat.st_mtime_ns, stat.st_size, round(ratio, 6))
    except OSError:
        return path
    cached = _PITCH_CACHE.get(key)
    if cached and os.path.isfile(cached):
        return cached
    try:
        import wave
        from array import array
        with wave.open(path, 'rb') as wav:
            channels = wav.getnchannels()
            width = wav.getsampwidth()
            rate = wav.getframerate()
            frames = wav.getnframes()
            if width != 2 or channels not in (1, 2) or frames < 2:
                return path
            data = wav.readframes(frames)
        samples = array('h')
        samples.frombytes(data)
        if sys.byteorder == 'big':
            samples.byteswap()
        out_frames = max(1, int(frames / ratio))
        out = array('h', bytes(2 * out_frames * channels))
        limit = frames - 1
        pos = 0.0
        if channels == 2:
            for i in range(out_frames):
                j = int(pos)
                if j >= limit:
                    j = limit
                    frac = 0.0
                else:
                    frac = pos - j
                k = j * 2
                a = samples[k]
                b = samples[k + 2]
                out[2 * i] = int(a + (b - a) * frac)
                a = samples[k + 1]
                b = samples[k + 3]
                out[2 * i + 1] = int(a + (b - a) * frac)
                pos += ratio
        else:
            for i in range(out_frames):
                j = int(pos)
                if j >= limit:
                    j = limit
                    frac = 0.0
                else:
                    frac = pos - j
                a = samples[j]
                b = samples[j + 1]
                out[i] = int(a + (b - a) * frac)
                pos += ratio
        directory = os.path.join(paths.cache_dir, 'sfx')
        os.makedirs(directory, exist_ok=True)
        digest = hashlib.sha1(repr(key).encode('utf-8')).hexdigest()[:20]
        target = os.path.join(directory, f'{digest}.wav')
        if not os.path.isfile(target):
            with wave.open(target, 'wb') as wav:
                wav.setnchannels(channels)
                wav.setsampwidth(2)
                wav.setframerate(rate)
                wav.writeframes(out.tobytes())
            _prune_pitch_cache(directory)
        _PITCH_CACHE[key] = target
        return target
    except Exception:
        return path


class SoundManager(QObject):
    _instance: SoundManager | None = None

    def __init__(self):
        super().__init__()
        self._sfx_volume = int(settings.sfx_volume)
        self._ambiance_volume = int(settings.ambiance_volume)
        self._media_sources: set[str] = set()
        self._media_owners: dict[str, Any] = {}
        self._suspended = False
        self._devices = None
        self._effects: dict[str, list] = {}
        self._voice_index: dict[str, int] = {}
        self._norms: dict[str, float] = {}
        self._effect_paths: dict[str, str] = {}
        self._variant_key: dict[str, str] = {}
        self._active_variants: dict[str, tuple[str, ...]] = {}
        self._ambiance_loaded = ''
        self._last_hover = 0.0
        self._ambiance_player = None
        self._ambiance_output = None
        self._ambiance_ramp = 1.0
        self._ambiance_ramp_target = 1.0
        self._ambiance_start_at = 0.0
        self._ambiance_fails = 0
        self._ambiance_retry_at = 0.0
        self._available = False
        self._setup_failed = False
        self._reload_timer = QTimer(self)
        self._reload_timer.setSingleShot(True)
        self._reload_timer.setInterval(300)
        self._reload_timer.timeout.connect(self._do_reload_sources)
        self._ambiance_tick = QTimer(self)
        self._ambiance_tick.setInterval(_AMBIANCE_TICK_MS)
        self._ambiance_tick.timeout.connect(self._sync_ambiance)
        self._ambiance_fade = QTimer(self)
        self._ambiance_fade.setInterval(_AMBIANCE_FADE_STEP_MS)
        self._ambiance_fade.timeout.connect(self._step_ambiance_fade)
        qconfig.themeChanged.connect(self.reload_sources)

    @classmethod
    def instance(cls) -> SoundManager:
        if cls._instance is None:
            cls._instance = SoundManager()
        return cls._instance

    def _ensure_ready(self) -> bool:
        if self._available:
            return True
        if self._setup_failed:
            return False
        try:
            self._setup()
        except Exception:
            self._setup_failed = True
            logger.exception('Failed to initialize audio backend; sounds disabled')
            return False
        self._available = True
        if self._suspended:
            self._stop_ambiance()
        elif self._ambiance_volume > 0:
            self._ambiance_tick.start()
            self._sync_ambiance()
        return True

    def _setup(self) -> None:
        from PySide6.QtMultimedia import QSoundEffect, QMediaPlayer, QAudioOutput, QMediaDevices
        for name, filename in _SFX_FILENAMES.items():
            if name.startswith('popout_'):
                continue
            count = _SFX_VOICES.get(name, 3)
            self._effects[name] = [QSoundEffect(self) for _ in range(count)]
            self._voice_index[name] = 0
            self._apply_effect_source(name, filename)
        self._ambiance_player = QMediaPlayer(self)
        self._ambiance_output = QAudioOutput(self)
        self._ambiance_player.setAudioOutput(self._ambiance_output)
        self._ambiance_player.setLoops(QMediaPlayer.Loops.Infinite)
        self._ambiance_output.setVolume(_gain(self._ambiance_volume))
        self._apply_ambiance_source()

        self._devices = QMediaDevices(self)
        self._devices.audioOutputsChanged.connect(self._on_audio_outputs_changed)
        self._ambiance_player.errorOccurred.connect(self._on_ambiance_error)

    def _on_audio_outputs_changed(self) -> None:
        from PySide6.QtMultimedia import QMediaDevices
        device = QMediaDevices.defaultAudioOutput()
        for voices in self._effects.values():
            for effect in voices:
                effect.setAudioDevice(device)

        if self._ambiance_output is not None and not device.isNull() and self._ambiance_output.device() != device:
            self._ambiance_output.setDevice(device)

    @staticmethod
    def _sfx_path(name: str, filename: str) -> str:
        return theme_sfx_path(active_preset_id(), name) or os.path.join(_SFX_DIR, filename)

    @staticmethod
    def _ambiance_path() -> str:
        return theme_sfx_path(active_preset_id(), 'ambiance') or os.path.join(_SFX_DIR, _AMBIANCE_FILENAME)

    def _apply_effect_source(self, name: str, filename: str) -> None:
        voices = self._effects.get(name)
        if not voices:
            return
        path = self._sfx_path(name, filename) if self._sfx_volume > 0 else ''
        if path and not os.path.isfile(path):
            path = ''
        if self._effect_paths.get(name) == path:
            self._apply_effect_volume(name)
            return
        self._effect_paths[name] = path
        self._norms[name] = _norm_gain(path) if path else 1.0
        self._variant_key.pop(name, None)
        self._active_variants.pop(name, None)
        url = QUrl.fromLocalFile(path) if path else QUrl()
        for effect in voices:
            effect.stop()
            effect.setSource(url)
        self._apply_effect_volume(name)

    def _prepare_variants(self, name: str) -> None:
        voices = self._effects.get(name)
        path = self._effect_paths.get(name) or ''
        if not voices or not path or self._variant_key.get(name) == path:
            return
        self._variant_key[name] = path
        variants = [path]
        variants.extend(_pitch_variant(path, 1.0 + shift) for shift in _pitch_offsets(len(voices)))
        self._active_variants[name] = tuple(variants)
        for effect, variant in zip(voices, variants):
            if effect.source().toLocalFile() == variant:
                continue
            effect.stop()
            effect.setSource(QUrl.fromLocalFile(variant))
        self._apply_effect_volume(name)

    def _apply_effect_volume(self, name: str) -> None:
        volume = min(1.0, _gain(self._sfx_volume) * self._norms.get(name, 1.0))
        for effect in self._effects.get(name, ()):
            effect.setVolume(volume)

    def _apply_ambiance_source(self) -> None:
        path = self._ambiance_path() if self._ambiance_volume > 0 else ''
        if path and not os.path.isfile(path):
            path = ''
        if path == self._ambiance_loaded:
            return
        self._ambiance_loaded = path
        self._ambiance_player.stop()
        self._ambiance_player.setSource(QUrl.fromLocalFile(path) if path else QUrl())

    def reload_sources(self, *_args) -> None:
        if self._available:
            self._reload_timer.start()

    def _do_reload_sources(self) -> None:
        for name, filename in _SFX_FILENAMES.items():
            self._apply_effect_source(name, filename)
        for name in ('hover', 'click', 'navigate'):
            self._prepare_variants(name)
        self._apply_ambiance_source()
        self._sync_ambiance()

    def set_sfx_volume(self, value: int) -> None:
        value = max(0, min(100, int(value)))
        was_off = self._sfx_volume <= 0
        self._sfx_volume = value
        if not self._available:
            return
        for name, filename in _SFX_FILENAMES.items():
            if value <= 0 or was_off:
                self._apply_effect_source(name, filename)
            else:
                self._apply_effect_volume(name)

    def set_ambiance_volume(self, value: int) -> None:
        value = max(0, min(100, int(value)))
        was_off = self._ambiance_volume <= 0
        self._ambiance_volume = value
        if not self._available:
            return
        if value <= 0:
            self._ambiance_tick.stop()
            self._stop_ambiance()
            self._apply_ambiance_source()
        elif was_off:
            self._apply_ambiance_source()
            if not self._suspended:
                self._ambiance_tick.start()
        self._apply_ambiance_gain()
        self._sync_ambiance()

    def set_media_playing(self, source: str, playing: bool, owner=None) -> None:
        if owner is not None and self._media_owners.get(source) is not owner:
            self._media_owners[source] = owner
            with suppress(RuntimeError):
                owner.destroyed.connect(lambda *_a, s=source, o=owner: self._release_media_source(s, o))
        playing = bool(playing)
        was_playing = source in self._media_sources
        if playing:
            self._media_sources.add(source)
        else:
            self._media_sources.discard(source)
        if playing != was_playing:
            self._sync_ambiance()

    def set_suspended(self, suspended: bool) -> None:
        suspended = bool(suspended)
        if suspended == self._suspended:
            return
        self._suspended = suspended
        if not self._available:
            return
        if suspended:
            self._ambiance_tick.stop()
            self._stop_ambiance()
            return
        if self._ambiance_volume > 0:
            self._ambiance_tick.start()
        self._sync_ambiance()

    def _release_media_source(self, source: str, owner=None) -> None:
        if owner is not None and self._media_owners.get(source) is not owner:
            return
        self._media_owners.pop(source, None)
        self._media_sources.discard(source)
        self._sync_ambiance()

    def _ambiance_window_visible(self) -> bool:
        app = QApplication.instance()
        if app is None or app.applicationState() != Qt.ApplicationState.ApplicationActive:
            return False
        active = app.activeWindow()
        return active is not None and active.isVisible() and not active.isMinimized()

    def _sync_ambiance(self) -> None:
        if not self._available or self._suspended:
            return
        if (self._ambiance_volume <= 0 or self._media_sources
                or not self._ambiance_window_visible()):
            self._stop_ambiance()
            return
        if not self._ambiance_loaded:
            if time.monotonic() < self._ambiance_retry_at:
                return
            self._apply_ambiance_source()
            if not self._ambiance_loaded:
                return
        self._ensure_ambiance_playing()

    def _ensure_ambiance_playing(self) -> None:
        from PySide6.QtMultimedia import QMediaPlayer
        player = self._ambiance_player
        state = player.playbackState()
        now = time.monotonic()
        if state == QMediaPlayer.PlaybackState.PlayingState:
            self._ambiance_start_at = 0.0
            self._ambiance_fails = 0
            self._ramp_ambiance(1.0)
            return
        if self._ambiance_start_at:
            if now - self._ambiance_start_at > _AMBIANCE_START_TIMEOUT:
                self._ambiance_fail('playback never started')
            return
        status = player.mediaStatus()
        duration = player.duration()
        if status == QMediaPlayer.MediaStatus.EndOfMedia or (duration > 0 and player.position() >= duration - 250):
            player.setPosition(0)
        if state == QMediaPlayer.PlaybackState.StoppedState and status not in (
                QMediaPlayer.MediaStatus.LoadingMedia, QMediaPlayer.MediaStatus.BufferingMedia):
            player.setPosition(0)
        player.play()
        self._ambiance_start_at = now
        self._ramp_ambiance(1.0)

    def _stop_ambiance(self) -> None:
        from PySide6.QtMultimedia import QMediaPlayer
        self._ambiance_start_at = 0.0
        player = self._ambiance_player
        if player.playbackState() == QMediaPlayer.PlaybackState.StoppedState and self._ambiance_ramp <= 0.0:
            return
        self._ramp_ambiance(0.0)

    def _ramp_ambiance(self, target: float) -> None:
        self._ambiance_ramp_target = target
        if abs(self._ambiance_ramp - target) < 0.01:
            self._ambiance_ramp = target
            self._ambiance_fade.stop()
            self._apply_ambiance_gain()
            if target <= 0.0:
                self._ambiance_player.pause()
            return
        if not self._ambiance_fade.isActive():
            self._ambiance_fade.start()

    def _step_ambiance_fade(self) -> None:
        step = _AMBIANCE_FADE_STEP_MS / _AMBIANCE_FADE_MS
        target = self._ambiance_ramp_target
        if self._ambiance_ramp < target:
            self._ambiance_ramp = min(target, self._ambiance_ramp + step)
        else:
            self._ambiance_ramp = max(target, self._ambiance_ramp - step)
        self._apply_ambiance_gain()
        if self._ambiance_ramp == target:
            self._ambiance_fade.stop()
            if target <= 0.0:
                self._ambiance_player.pause()

    def _apply_ambiance_gain(self) -> None:
        self._ambiance_output.setVolume(_gain(self._ambiance_volume) * self._ambiance_ramp)

    def _on_ambiance_error(self, _error, message: str) -> None:
        self._ambiance_fail(message or 'playback error')

    def _ambiance_fail(self, reason: str) -> None:
        self._ambiance_start_at = 0.0
        self._ambiance_fails += 1
        delay = min(60.0, 5.0 * self._ambiance_fails)
        self._ambiance_retry_at = time.monotonic() + delay
        self._ambiance_loaded = ''
        self._stop_ambiance()
        logger.warning('Ambiance track stopped (%s); retrying in %.0fs', reason, delay)

    def warm(self) -> None:
        if self._sfx_volume <= 0 and self._ambiance_volume <= 0:
            return
        if not self._available and not self._setup_failed:
            self._ensure_ready()
        if self._available and self._sfx_volume > 0:
            for name in ('hover', 'click', 'navigate'):
                self._prepare_variants(name)

    def _retry_play(self, name: str, attempt: int) -> None:
        if self._sfx_volume <= 0 or not self._available:
            return
        voices = self._effects.get(name)
        if not voices:
            return
        if any(v.isLoaded() for v in voices):
            self.play(name)
        elif attempt < 4:
            QTimer.singleShot(40, lambda: self._retry_play(name, attempt + 1))

    def _ensure_effect(self, name: str):
        voices = self._effects.get(name)
        if voices is not None:
            return voices
        filename = _SFX_FILENAMES.get(name)
        if filename is None:
            return None
        from PySide6.QtMultimedia import QSoundEffect
        count = _SFX_VOICES.get(name, 3)
        voices = [QSoundEffect(self) for _ in range(count)]
        self._effects[name] = voices
        self._voice_index[name] = 0
        self._apply_effect_source(name, filename)
        return voices

    def play(self, name: str) -> None:
        if self._sfx_volume <= 0:
            return
        if not self._ensure_ready():
            return
        voices = self._effects.get(name) or self._ensure_effect(name)
        if not voices:
            return
        if name == 'hover':
            now = time.monotonic()
            if now - self._last_hover < _HOVER_MIN_GAP:
                return
            self._last_hover = now
        self._prepare_variants(name)
        count = len(voices)
        start = random.randrange(count)
        index = -1
        for step in range(count):
            probe = (start + step) % count
            effect = voices[probe]
            if effect.isLoaded() and not effect.isPlaying():
                index = probe
                break
        if index < 0:
            index = start
            effect = voices[index]
            if not effect.isLoaded():
                if name != 'hover':
                    QTimer.singleShot(40, lambda: self._retry_play(name, 1))
                return
            effect.stop()
        if not name.startswith('popout'):
            for other_index, other in enumerate(voices):
                if other_index != index and other.isPlaying():
                    other.stop()
        self._voice_index[name] = (index + 1) % count
        voices[index].play()

    def play_popout(self, kind: str = 'info') -> None:
        key = f'popout_{kind}' if kind in ('info', 'success', 'warning', 'error') else 'popout_info'
        self.play(key)


def get_sound_manager() -> SoundManager:
    return SoundManager.instance()


def play_popout_sfx(kind: str = 'info') -> None:
    try:
        get_sound_manager().play_popout(kind)
    except Exception:
        logger.exception('Failed to play popout sfx (kind=%r)', kind)


def _root_owner(widget):
    node = widget
    for _ in range(8):
        parent = node.parentWidget()
        if parent is None or getattr(parent, 'itemWidget', None) is not node:
            break
        node = parent
    return node


class _UiSoundEventFilter(QObject):
    _INTERESTING = frozenset((
        QEvent.Type.Enter, QEvent.Type.Leave, QEvent.Type.MouseButtonPress,
        QEvent.Type.MouseButtonRelease, QEvent.Type.KeyPress,
    ))
    _ACTIVATE_KEYS = frozenset((Qt.Key.Key_Space, Qt.Key.Key_Return, Qt.Key.Key_Enter))
    _HOVER_GAP = 0.05

    def __init__(self):
        super().__init__()
        self._hover_owner = None
        self._hover_at = 0.0
        self._pending_owner = None
        self._hover_timer = None
        self._press_owner = None
        self._click_stamp = None
        self._click_at = 0.0

    @staticmethod
    def _owner(widget):
        current = widget
        for _ in range(24):
            if current is None:
                return None
            if getattr(current, '_pirachest_no_sfx', False):
                return None
            if getattr(current, '_pirachest_menu_sfx', None) is not None:
                return None
            if isinstance(current, QAbstractItemView):
                return None
            if isinstance(current, QAbstractButton) or getattr(current, '_pirachest_sfx', False):
                return _root_owner(current)
            if getattr(type(current), 'clicked', None) is not None:
                return _root_owner(current)
            current = current.parentWidget()
        return None

    @staticmethod
    def _pointer_over(owner) -> bool:
        try:
            return owner.isVisible() and owner.rect().contains(owner.mapFromGlobal(QCursor.pos()))
        except Exception:
            return False

    def _release_hover(self, owner) -> None:
        if self._pointer_over(owner):
            return
        if owner is self._pending_owner:
            self._cancel_pending_hover()
        if owner is self._hover_owner:
            self._hover_owner = None

    def _request_hover(self, owner) -> None:
        now = time.monotonic()
        if now - self._click_at < 0.12:
            return
        if owner is self._hover_owner and self._pointer_over(owner):
            return
        self._hover_owner = owner
        timer = self._hover_timer
        if timer is None:
            timer = self._hover_timer = QTimer(self)
            timer.setSingleShot(True)
            timer.timeout.connect(self._flush_hover)
        wait = self._hover_at + self._HOVER_GAP - now
        if wait <= 0.001:
            if timer.isActive():
                timer.stop()
            self._pending_owner = None
            self._hover_at = now
            get_sound_manager().play('hover')
        else:
            self._pending_owner = owner
            timer.start(max(1, round(wait * 1000)))

    def _flush_hover(self) -> None:
        owner = self._pending_owner
        self._pending_owner = None
        if owner is None or not self._pointer_over(owner):
            return
        self._hover_at = time.monotonic()
        get_sound_manager().play('hover')

    def _cancel_pending_hover(self) -> None:
        self._pending_owner = None
        timer = self._hover_timer
        if timer is not None:
            timer.stop()

    def eventFilter(self, obj, event) -> bool:
        et = event.type()
        if et not in self._INTERESTING or not isinstance(obj, QWidget):
            return False
        try:
            if et in (QEvent.Type.MouseButtonPress, QEvent.Type.MouseButtonRelease):
                self._cancel_pending_hover()
            owner = self._owner(obj)
            if et == QEvent.Type.Leave:
                if owner is not None:
                    self._release_hover(owner)
                return False
            if owner is None:
                if et == QEvent.Type.Enter:
                    self._cancel_pending_hover()
                if et == QEvent.Type.MouseButtonRelease:
                    self._press_owner = None
                return False
            if not owner.isEnabled():
                return False
            if et == QEvent.Type.Enter:
                if (QApplication.mouseButtons() != Qt.MouseButton.NoButton
                        or QApplication.applicationState() != Qt.ApplicationState.ApplicationActive):
                    return False
                self._request_hover(owner)
            elif et == QEvent.Type.MouseButtonPress:
                self._press_owner = owner if event.button() == Qt.MouseButton.LeftButton else None
            elif et == QEvent.Type.MouseButtonRelease:
                pressed = self._press_owner
                self._press_owner = None
                if (event.button() == Qt.MouseButton.LeftButton and pressed is owner
                        and owner.rect().contains(owner.mapFromGlobal(event.globalPosition().toPoint()))):
                    stamp = event.timestamp()
                    if stamp != self._click_stamp:
                        self._click_stamp = stamp
                        self._click_at = time.monotonic()
                        get_sound_manager().play('click')
            elif et == QEvent.Type.KeyPress:
                if obj is owner and not event.isAutoRepeat() and event.key() in self._ACTIVATE_KEYS:
                    self._click_at = time.monotonic()
                    get_sound_manager().play('click')
        except Exception:
            pass
        return False


class _MenuSfxFilter(QObject):
    def __init__(self, view):
        super().__init__()
        self._view = view
        self._last_row = None

    def _row_at(self, obj, pos):
        view = self._view
        if obj is not view.viewport():
            pos = view.viewport().mapFrom(view, pos)
        item = view.itemAt(pos)

        if item is None or not bool(item.flags() & Qt.ItemFlag.ItemIsEnabled):
            return None
        return view.row(item)

    def eventFilter(self, obj, event) -> bool:
        with suppress(Exception):
            et = event.type()
            if et == QEvent.Type.MouseMove:
                row = self._row_at(obj, event.position().toPoint())
                if row is None:
                    self._last_row = None
                elif row != self._last_row:
                    self._last_row = row
                    get_sound_manager().play('hover')
            elif et == QEvent.Type.MouseButtonRelease and event.button() == Qt.MouseButton.LeftButton:
                row = self._row_at(obj, event.position().toPoint())
                if row is not None:
                    get_sound_manager().play('click')
        return False


def _patch_menu_sfx() -> None:
    try:
        from qfluentwidgets.components.widgets.menu import RoundMenu
    except Exception:
        logger.exception('RoundMenu not available for sfx patching')
        return
    if getattr(RoundMenu, '_pirachest_menu_sfx', False):
        return
    _orig_show = RoundMenu.showEvent

    def _show_event(self, event, _orig=_orig_show):
        _orig(self, event)
        with suppress(Exception):
            view = getattr(self, 'view', None)
            if view is not None and not getattr(view, '_pirachest_menu_sfx', None):
                view.setMouseTracking(True)
                view.viewport().setMouseTracking(True)
                handler = _MenuSfxFilter(view)
                view.installEventFilter(handler)
                view.viewport().installEventFilter(handler)
                view._pirachest_menu_sfx = handler

    RoundMenu.showEvent = _show_event
    RoundMenu._pirachest_menu_sfx = True


_ui_sound_filter: _UiSoundEventFilter | None = None


def install_ui_sound_filter(app) -> None:
    global _ui_sound_filter
    if _ui_sound_filter is not None:
        return
    _ui_sound_filter = _UiSoundEventFilter()
    app.installEventFilter(_ui_sound_filter)
    _patch_menu_sfx()


_ONOFF_CACHE: dict = {}


def _on_off(checked: bool) -> str:
    lang = current_language()
    pair = _ONOFF_CACHE.get(lang)
    if pair is None:
        pair = _ONOFF_CACHE[lang] = (tr_or('common.off', 'Off'), tr_or('common.on', 'On'))
    return pair[bool(checked)]


def tr_or(key: str, fallback: str, **kwargs) -> str:
    try:
        value = tr(key, **kwargs)
    except Exception:
        value = ''
    if not value or value == key:
        value = fallback
    if kwargs and '{' in value:
        try:
            value = value.format(**kwargs)
        except (KeyError, IndexError, ValueError):
            pass
    return value


class _DragHandle(QWidget):
    grabbed = Signal(QPoint)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedWidth(16)
        self.setCursor(Qt.CursorShape.OpenHandCursor)
        self._hover = False
        self._down = False

    def reset(self) -> None:
        self._down = False
        self.update()

    def enterEvent(self, event) -> None:
        self._hover = True
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:
        self._hover = False
        self.update()
        super().leaveEvent(event)

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self._down = True
            self.update()
            self.grabbed.emit(event.globalPosition().toPoint())
            event.accept()
        else:
            event.ignore()

    def mouseReleaseEvent(self, event) -> None:
        self.reset()
        event.accept()

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        base = 255 if isDarkTheme() else 0
        if self._hover or self._down:
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(base, base, base, 26 if self._down else 14))
            painter.drawRoundedRect(QRectF(1, self.height() / 2 - 20, self.width() - 2, 40), 4, 4)
        alpha = 190 if self._down else (140 if self._hover else 70)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(base, base, base, alpha))
        cx, cy = self.width() / 2, self.height() / 2
        for dx in (-2.5, 2.5):
            for dy in (-5, 0, 5):
                painter.drawEllipse(QPointF(cx + dx, cy + dy), 1.3, 1.3)


class _DragGhost(QWidget):

    def __init__(self, pixmap, parent):
        super().__init__(parent)
        self._pixmap = pixmap
        self._valid = True
        self.setFixedSize(pixmap.size() / pixmap.devicePixelRatio())
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        shadow = QGraphicsDropShadowEffect(self)
        shadow.setBlurRadius(22)
        shadow.setOffset(0, 5)
        shadow.setColor(QColor(0, 0, 0, 110))
        self.setGraphicsEffect(shadow)

    def set_valid(self, valid: bool) -> None:
        if valid != self._valid:
            self._valid = valid
            self.update()

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setOpacity(0.93 if self._valid else 0.5)
        painter.drawPixmap(0, 0, self._pixmap)
        painter.setOpacity(1.0 if self._valid else 0.5)
        accent = QColor(themeColor())
        accent.setAlpha(150)
        painter.setPen(accent)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawRoundedRect(QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5), 5, 5)


class _SectionReorder(QObject):

    def __init__(self, group, on_commit, parent=None):
        super().__init__(parent)
        self._group = group
        self._layout = group.cardLayout
        self._cards = []
        self._orig = []
        self._targets = {}
        self._anims = {}
        self._settling = None
        self._top = 0
        self._gap = 0
        self._on_commit = on_commit
        self._card = None
        self._ghost = None
        self._scroll = None
        self._last = QPoint()
        self._index = 0
        self._grab_dy = 0
        self._valid = True
        self._timer = QTimer(self)
        self._timer.setInterval(16)
        self._timer.timeout.connect(self._autoscroll)

    def register(self, card) -> None:
        if card not in self._cards:
            self._cards.append(card)

    def cards(self) -> list:
        return list(self._cards)

    def arrange(self, order: list) -> None:
        if self._card is not None:
            return
        rank = {key: i for i, key in enumerate(order)}
        wanted = sorted(self._cards, key=lambda c: rank.get(c.nav_key, len(rank)))
        if wanted != self._cards:
            self._place(wanted, animate=False)

    def _glide(self, cards: list) -> None:
        for card in cards:
            end = QPoint(card.x(), self._targets[card])
            start = card.pos()
            if start == end:
                continue
            if not animations_enabled():
                card.move(end)
                continue
            anim = QPropertyAnimation(card, b'pos', self)
            anim.setDuration(170)
            anim.setEasingCurve(QEasingCurve.Type.OutCubic)
            anim.setStartValue(start)
            anim.setEndValue(end)
            anim.finished.connect(partial(self._anims.pop, card, None))
            self._anims[card] = anim
            anim.start()

    def _reorder_list(self, wanted: list) -> None:
        owned = getattr(self._layout, '_ExpandLayout__widgets', None)
        if isinstance(owned, list) and all(c in owned for c in wanted):
            slots = sorted(owned.index(c) for c in wanted)
            for slot, card in zip(slots, wanted):
                owned[slot] = card
            return
        for card in self._cards:
            self._layout.removeWidget(card)
        for card in wanted:
            self._layout.addWidget(card)

    def _place(self, wanted: list, animate: bool = True) -> None:
        for anim in list(self._anims.values()):
            anim.stop()
        self._anims.clear()
        self._reorder_list(wanted)
        self._cards = list(wanted)
        if not animate:
            self._layout.invalidate()
            return
        y = self._top
        for card in self._cards:
            self._targets[card] = y
            y += card.height() + self._gap
        self._glide(self._cards)

    def _settle(self) -> None:
        pending, self._settling = self._settling, None
        if pending is None:
            return
        card, ghost, anim = pending
        anim.stop()
        ghost.hide()
        ghost.deleteLater()
        card.setGraphicsEffect(None)
        card.handle_reset()

    def begin(self, card, gpos: QPoint) -> None:
        self._settle()
        if self._card is not None:
            return
        if len(self._cards) < 2 or card not in self._cards:
            card.handle_reset()
            return
        ordered = sorted(self._cards, key=lambda c: c.y())
        self._top = ordered[0].y()
        self._gap = max(0, ordered[1].y() - (ordered[0].y() + ordered[0].height()))
        self._targets = {c: c.y() for c in self._cards}
        self._card = card
        self._orig = list(self._cards)
        self._index = self._cards.index(card)
        self._grab_dy = self._group.mapFromGlobal(gpos).y() - card.y()
        self._ghost = _DragGhost(card.card.grab(), self._group)
        self._ghost.move(card.x(), card.y())
        self._ghost.show()
        effect = QGraphicsOpacityEffect(card)
        effect.setOpacity(0.35)
        card.setGraphicsEffect(effect)
        parent = self._group.parentWidget()
        while parent is not None and not isinstance(parent, QScrollArea):
            parent = parent.parentWidget()
        self._scroll = parent
        if parent is not None:
            parent.verticalScrollBar().valueChanged.connect(self._refresh)
        QApplication.instance().installEventFilter(self)
        QApplication.setOverrideCursor(Qt.CursorShape.ClosedHandCursor)
        self._timer.start()
        self._update(gpos)

    def eventFilter(self, obj, event) -> bool:
        if self._card is None:
            return False
        kind = event.type()
        if kind == QEvent.Type.MouseMove:
            self._update(event.globalPosition().toPoint())
            return True
        if kind == QEvent.Type.MouseButtonRelease:
            if event.button() == Qt.MouseButton.LeftButton:
                self._end(commit=True)
            return True
        if kind == QEvent.Type.MouseButtonPress:
            self._end(commit=False)
            return True
        if kind == QEvent.Type.KeyPress:
            if event.key() == Qt.Key.Key_Escape:
                self._end(commit=False)
            return True
        if kind in (QEvent.Type.ApplicationDeactivate, QEvent.Type.WindowBlocked):
            self._end(commit=False)
        return False

    def _refresh(self, *_args) -> None:
        if self._card is not None:
            self._update(self._last)

    def _region(self):
        widget = self._scroll.viewport() if self._scroll is not None else self._group
        return QRect(widget.mapToGlobal(QPoint(0, 0)), widget.size())

    def _update(self, gpos: QPoint) -> None:
        self._last = gpos
        card = self._card
        slots = self._targets
        top = min(slots.values())
        bottom = max(slots[c] + c.height() for c in self._cards)
        local_y = self._group.mapFromGlobal(gpos).y()
        raw = local_y - self._grab_dy
        y = max(top, min(raw, bottom - self._ghost.height()))
        self._ghost.move(card.x(), y)
        center = raw + self._ghost.height() / 2
        others = [c for c in self._cards if c is not card]
        index = sum(1 for c in others if slots[c] + c.height() / 2 < center)
        if index != self._index:
            self._index = index
            self._place(others[:index] + [card] + others[index:])
        region = self._region()
        valid = region.adjusted(0, -24, 0, 24).contains(gpos) and region.left() <= gpos.x() <= region.right()
        if valid != self._valid:
            self._valid = valid
            self._ghost.set_valid(valid)
            QApplication.changeOverrideCursor(
                Qt.CursorShape.ClosedHandCursor if valid else Qt.CursorShape.ForbiddenCursor)

    def _autoscroll(self) -> None:
        if self._card is None or self._scroll is None or not self._valid:
            return
        region = self._region()
        edge = 56
        y = self._last.y()
        delta = 0
        if y < region.top() + edge:
            delta = -int(max(1, (region.top() + edge - y) / 4))
        elif y > region.bottom() - edge:
            delta = int(max(1, (y - (region.bottom() - edge)) / 4))
        if delta:
            bar = self._scroll.verticalScrollBar()
            bar.setValue(bar.value() + max(-24, min(24, delta)))

    def _end(self, commit: bool) -> None:
        card, ghost = self._card, self._ghost
        changed = self._cards != self._orig
        keep = commit and self._valid
        if changed and not keep:
            self._place(list(self._orig))
        target = self._targets[card]
        self._card = None
        self._ghost = None
        self._timer.stop()
        QApplication.instance().removeEventFilter(self)
        while QApplication.overrideCursor() is not None:
            QApplication.restoreOverrideCursor()
        if self._scroll is not None:
            with suppress(Exception):
                self._scroll.verticalScrollBar().valueChanged.disconnect(self._refresh)
        self._scroll = None
        self._valid = True
        if not animations_enabled():
            ghost.move(card.x(), target)
            ghost.hide()
            ghost.deleteLater()
            card.setGraphicsEffect(None)
            card.handle_reset()
            if changed and keep:
                self._on_commit()
            return
        anim = QPropertyAnimation(ghost, b'pos', self)
        anim.setDuration(140)
        anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        anim.setStartValue(ghost.pos())
        anim.setEndValue(QPoint(card.x(), target))
        anim.finished.connect(self._settle)
        self._settling = (card, ghost, anim)
        ghost.set_valid(True)
        anim.start()
        if changed and keep:
            self._on_commit()


def _contrast_text(color: QColor) -> QColor:
    def lin(v: float) -> float:
        v /= 255
        return v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4
    lum = 0.2126 * lin(color.red()) + 0.7152 * lin(color.green()) + 0.0722 * lin(color.blue())
    white = 1.05 / (lum + 0.05)
    dark = (lum + 0.05) / 0.0556
    return QColor('#ffffff') if white >= dark else QColor('#111111')


class _PillButton(PillPushButton):
    hovered = Signal()

    def enterEvent(self, event) -> None:
        super().enterEvent(event)
        self.hovered.emit()

    def paintEvent(self, event) -> None:
        if not self.isChecked():
            super().paintEvent(event)
            return
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        accent = QColor(themeColor())
        if self.isDown():
            accent.setAlphaF(0.8)
        elif self.underMouse():
            accent.setAlphaF(0.92)
        rect = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(accent)
        painter.drawRoundedRect(rect, rect.height() / 2, rect.height() / 2)
        painter.setPen(_contrast_text(QColor(themeColor())))
        painter.setFont(self.font())
        painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, self.text())


class _AccentButton(PrimaryPushButton):

    def paintEvent(self, event) -> None:
        if not self.isEnabled() or not self.icon().isNull():
            super().paintEvent(event)
            return
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        accent = QColor(themeColor())
        if self.isDown():
            accent.setAlphaF(0.8)
        elif self.underMouse():
            accent.setAlphaF(0.92)
        rect = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(accent)
        painter.drawRoundedRect(rect, 5, 5)
        painter.setPen(_contrast_text(QColor(themeColor())))
        painter.setFont(self.font())
        painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, self.text())


class _ClickShield(QObject):

    _BLOCKED = (QEvent.Type.MouseButtonPress, QEvent.Type.MouseButtonRelease,
                QEvent.Type.MouseButtonDblClick)

    def eventFilter(self, obj, event) -> bool:
        return event.type() in self._BLOCKED


class _HandleAnchor(QObject):

    def __init__(self, header, handle) -> None:
        super().__init__(header)
        self._header = header
        self._handle = handle
        header.installEventFilter(self)
        self._sync()

    def eventFilter(self, obj, event) -> bool:
        if event.type() in (QEvent.Type.Resize, QEvent.Type.Show):
            self._sync()
        return False

    def _sync(self) -> None:
        self._handle.setGeometry(0, 0, 16, self._header.height())
        self._handle.raise_()


class _SectionCard(ExpandGroupSettingCard):

    def __init__(self, icon, title: str, content: str = '', parent=None):
        super().__init__(icon, title, content, parent)
        self.nav_key = ''
        self._handle = None
        self._shield = _ClickShield(self)
        self.card.installEventFilter(self._shield)
        self.switch = SwitchButton('', self, IndicatorPosition.RIGHT)
        self.addWidget(self.switch)
        self.switch.checkedChanged.connect(self._sync_switch_text)

    def _sync_switch_text(self, checked: bool) -> None:
        self.switch.setText(_on_off(checked))

    def enable_reorder(self, key: str, begin) -> None:
        self.nav_key = key
        self._handle = _DragHandle(self.card)
        self._anchor = _HandleAnchor(self.card, self._handle)
        self._handle.grabbed.connect(lambda pos: begin(self, pos))
        self._handle.show()

    def handle_reset(self) -> None:
        if self._handle is not None:
            self._handle.reset()

    def setChecked(self, checked: bool) -> None:
        self.switch.setChecked(bool(checked))

    def isChecked(self) -> bool:
        return self.switch.isChecked()

    def setTitle(self, title: str) -> None:
        self.card.setTitle(title)

    def setContent(self, content: str) -> None:
        self.card.setContent(content)


class _ExpandCard(ExpandGroupSettingCard):

    def setTitle(self, title: str) -> None:
        self.card.setTitle(title)

    def setContent(self, content: str) -> None:
        self.card.setContent(content)


def _path_row(edit: LineEdit, *buttons) -> QWidget:
    row = QWidget()
    layout = QHBoxLayout(row)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(8)
    layout.addWidget(edit, 1)
    for button in buttons:
        layout.addWidget(button)
    return row


def _inline_row(*widgets, spacing: int = 12) -> QWidget:
    row = QWidget()
    layout = QHBoxLayout(row)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(spacing)
    for widget in widgets:
        layout.addWidget(widget)
    return row


def _fix_value_label_width(label, samples=("100", "100%", "Off")) -> None:
    with suppress(Exception):
        fm = label.fontMetrics()
        width = max(fm.horizontalAdvance(s) for s in samples) + 8
        label.setFixedWidth(width)
        label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)


_MIN_PREFIX = ('settings.tv_min_prefix', 'Min ')
_MAX_PREFIX = ('settings.tv_max_prefix', 'Max ')
_GB = ('settings.unit_gb', ' GB')


class _SliderCommit(QObject):
    def __init__(self, slider, live, commit, idle_ms: int = 600, parent=None):
        super().__init__(parent or slider)
        self._live = live
        self._commit = commit
        self._pending = None
        self._committed = None
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(max(50, int(idle_ms)))
        self._timer.timeout.connect(self.flush)
        slider.valueChanged.connect(self._on_value)
        with suppress(Exception):
            slider.sliderReleased.connect(self.flush)

    def _on_value(self, value) -> None:
        self._pending = int(value)
        try:
            self._live(int(value))
        except Exception:
            logger.exception('Slider live update failed')
        self._timer.start()

    def flush(self) -> None:
        with suppress(Exception):
            if self._timer.isActive():
                self._timer.stop()
        if self._pending is None or self._pending == self._committed:
            return
        self._committed = self._pending
        try:
            self._commit(self._pending)
        except Exception:
            logger.exception('Slider commit failed')


def confirm_experimental_features(parent=None) -> bool:
    if settings.experimental_acknowledged:
        return True
    if parent is None:
        parent = QApplication.activeWindow() or QWidget()
    box = MessageBoxBase(parent)
    box.widget.setMinimumWidth(470)
    layout = QVBoxLayout()
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(8)
    layout.addWidget(StrongBodyLabel(tr_or(
        'settings.experimental_ack_title', 'Enable experimental features?')))
    content = BodyLabel(tr_or(
        'settings.experimental_ack_content',
        'Experimental features are unfinished and can affect downloads, installers, or the entire interface. Only continue if you understand the risk.'))
    content.setWordWrap(True)
    layout.addWidget(content)
    box.viewLayout.addLayout(layout)
    box.yesButton.setText(tr_or('settings.experimental_ack_button', 'I understand'))
    box.cancelButton.setText(tr_or('common.cancel', 'Cancel'))
    if not box.exec():
        return False
    settings.experimental_acknowledged = True
    save_settings(settings)
    logger.info('Experimental features acknowledged')
    return True


class SettingsPage(QWidget):
    settings_changed = Signal()
    _cache_size_ready = Signal(int)
    _cache_clear_finished = Signal(str)
    _vyla_update_finished = Signal(str, str)

    _CATEGORIES = (
        ('appearance', 'settings.tab_appearance', 'Appearance'),
        ('sections', 'settings.tab_sections', 'Sections'),
        ('sailseek', 'settings.tab_sailseek', 'SailSeek'),
        ('downloads', 'settings.tab_downloads', 'Downloads'),
        ('network', 'settings.tab_network', 'Network'),
        ('integrations', 'settings.tab_integrations', 'Integrations'),
        ('shortcuts', 'settings.tab_shortcuts', 'Shortcuts'),
        ('advanced', 'settings.tab_advanced', 'Advanced'),
    )

    _NET_ICONS = {
        'proxy_address': FluentIcon.WIFI,
        'proxy_login': FluentIcon.PEOPLE,
        'bind': FluentIcon.WIFI,
        'proxy': FluentIcon.GLOBE,
        'proxy_peers': FluentIcon.SHARE,
        'force_proxy': FluentIcon.CANCEL,
        'anonymous': FluentIcon.HIDE,
        'require_encryption': FluentIcon.VPN,
        'dht': FluentIcon.GLOBE,
        'lsd': FluentIcon.HOME,
        'upnp': FluentIcon.IOT,
        'private_history': FluentIcon.DELETE,
    }

    _PAGES = {
        'appearance': ('_build_general', '_build_sound'),
        'sections': ('_build_sections_start', '_build_sections_books', '_build_sections_tv', '_build_sections_end'),
        'sailseek': ('_build_sailseek',),
        'downloads': ('_build_downloads',),
        'network': ('_build_privacy',),
        'integrations': ('_build_integrations', '_build_debrid'),
        'shortcuts': ('_build_shortcuts',),
        'advanced': ('_build_advanced', '_build_experimental'),
    }
    _LISTS = ('_switches', '_sections', '_combos', '_spins', '_checks', '_texts', '_switch_labels')
    _ATTR_PAGES = {
        **dict.fromkeys(('_chk_close_to_tray', '_chk_console_structure', '_chk_delete_torrent',
                         '_chk_notify_download', '_spin_speed', '_spin_upload_speed', '_spin_seed',
                         '_spin_reserve'), 'downloads'),
        **dict.fromkeys(('_chk_minerva', '_chk_minerva_extract', '_chk_pc_games', '_chk_music', '_chk_books',
                         '_chk_youtube', '_chk_tv', '_chk_tbhb'), 'sections'),
        **dict.fromkeys(('_chk_external_client', '_chk_launch_startup'), 'integrations'),
        **dict.fromkeys(('_chk_admin_mode', '_chk_show_console', '_chk_log_to_file', '_chk_auto_update',
                         '_chk_canary_update', '_chk_auto_install'), 'advanced'),
        '_chk_sailseek': 'sailseek',
    }
    _GROUP_TARGETS = {
        'settings.group_appearance': ('appearance', '_general_group'),
        'settings.group_general': ('appearance', '_general_group'),
        'settings.group_downloads': ('downloads', '_dl_group'),
        'settings.group_downloads_network': ('downloads', '_dl_group'),
        'settings.group_network': ('network', '_privacy_group'),
        'settings.privacy_group': ('network', '_privacy_group'),
        'settings.group_features': ('sections', '_feature_group'),
        'settings.group_sections': ('sections', '_feature_group'),
        'settings.group_sound': ('appearance', '_sound_group'),
        'settings.group_shortcuts': ('shortcuts', '_shortcuts_group'),
        'settings.group_sailseek': ('sailseek', '_sailseek_group'),
        'settings.group_integrations': ('integrations', '_integrations_group'),
        'settings.group_debrid': ('integrations', '_debrid_group'),
        'settings.group_advanced': ('advanced', '_adv_group'),
        'settings.group_experimental': ('advanced', '_experimental_group'),
    }

    def __init__(self, parent=None):
        super().__init__(parent)
        self._loaded = set()
        self._ranges = {}
        self._jobs = {}
        self._ticking = False
        self._owned = {}
        self._stepping = False
        self._shell = False
        self._stale = set()
        self._built = False
        self._building = False
        self._loading = False
        self._build_locale = current_language()
        self._surface_styled = False
        self._pending_bind = None
        self._capturing_shortcut = None
        self._texts = []
        self._icon_entries = {}
        self._switch_labels = {}
        self._last_cache_probe = 0.0
        self._switches = []
        self._sections = []
        self._combos = []
        self._spins = []
        self._checks = []
        self._download_dir_widgets = {}
        self._download_dir_cards = []
        self._shortcut_rows = {}
        self._sailseek_source_switches = {}
        self._sailseek_dependents = []
        self._sailseek_status_lbl = None
        self._net_apply = None
        self._net_loading = False
        self._net_widgets = {}
        self._theme_dirty = False
        self._scroll = None
        self.setStyleSheet('background: transparent;')
        self._pending_group_adjust = set()
        self._active_category = self._CATEGORIES[0][0]
        self._selected_category = self._active_category
        self._category_buttons = {}
        self._category_layouts = {}
        self._category_pages = {}
        self._prebuild_timer = QTimer(self)
        self._prebuild_timer.setSingleShot(True)
        self._prebuild_timer.setInterval(0)
        self._prebuild_timer.timeout.connect(self._try_prebuild)

    def prepare(self) -> None:
        if not self._built and not self._jobs and not self._prebuild_timer.isActive():
            self._prebuild_timer.start()

    def _ensure_built(self) -> None:
        self._ensure_category(self._selected_category)

    def __getattr__(self, name):
        key = self._ATTR_PAGES.get(name)
        d = self.__dict__
        if key is None or '_loaded' not in d or d.get('_stepping'):
            raise AttributeError(name)
        if key in d.get('_jobs', ()) or key not in d['_loaded']:
            self._ensure_category(key)
            return object.__getattribute__(self, name)
        raise AttributeError(name)

    def _try_prebuild(self) -> None:
        if self._built or self._jobs:
            return
        app = QApplication.instance()
        if app is not None and app.activeModalWidget() is not None:
            self._prebuild_timer.start(600)
            return
        self._ensure_category(self._selected_category, sliced=True)

    def _ensure_shell(self) -> None:
        if self._shell:
            return
        self._shell = True
        self._build_locale = current_language()
        with suppress(Exception):
            self.setStyleSheet(settings_qss())
            self._surface_styled = True
        self._make_shell()
        register_locale_refresh(self, self._apply_locale)
        register_theme_refresh(self, self._on_theme_refresh)

    def _ensure_category(self, key: str, sliced: bool = False) -> None:
        self._ensure_shell()
        if key in self._loaded:
            if key in self._jobs and not sliced:
                self._advance_job(key)
            return
        self._start_job(key)
        if sliced:
            self._schedule_jobs()
        else:
            self._advance_job(key)

    def _start_job(self, key: str) -> None:
        self._loaded.add(key)
        page = self._make_category_page(key)
        page.setUpdatesEnabled(False)

        def chain():
            for name in self._PAGES[key]:
                yield from getattr(self, name)()

        self._jobs[key] = {
            'gen': chain(), 'page': page, 'attrs': set(), 'objs': [],
            'lists': {n: ({} if n == '_switch_labels' else []) for n in self._LISTS},
        }

    def _schedule_jobs(self) -> None:
        if not self._ticking:
            self._ticking = True
            QTimer.singleShot(0, self, self._tick_jobs)

    def _tick_jobs(self) -> None:
        self._ticking = False
        if not self._jobs:
            return
        key = self._selected_category if self._selected_category in self._jobs else next(iter(self._jobs))
        visible = key == self._selected_category and self.isVisible()
        self._advance_job(key, 0.014 if visible else 0.01)
        if self._jobs:
            self._schedule_jobs()

    def _advance_job(self, key: str, budget: float | None = None) -> bool:
        job = self._jobs.get(key)
        if job is None:
            return True
        prev = self._active_category
        self._active_category = key
        self._stepping = True
        saved = {n: getattr(self, n) for n in self._LISTS}
        for n in self._LISTS:
            setattr(self, n, job['lists'][n])
        attrs0 = set(self.__dict__)
        kids0 = {id(c) for c in self.children()}
        end = None if budget is None else time.perf_counter() + budget
        done = False
        try:
            while True:
                next(job['gen'])
                if end is not None and time.perf_counter() >= end:
                    break
        except StopIteration:
            done = True
        except Exception:
            self._jobs.pop(key, None)
            job['page'].setUpdatesEnabled(True)
            raise
        finally:
            for n in self._LISTS:
                setattr(self, n, saved[n])
            self._stepping = False
            self._active_category = prev
            job['attrs'] |= set(self.__dict__) - attrs0
            job['objs'].extend(c for c in self.children() if id(c) not in kids0)
        if done:
            self._finish_job(key)
        return done

    def _finish_job(self, key: str) -> None:
        job = self._jobs.pop(key)
        page = job['page']
        rng = {}
        for n in self._LISTS:
            box = getattr(self, n)
            src = job['lists'][n]
            a = len(box)
            if isinstance(box, dict):
                box.update(src)
            else:
                box.extend(src)
            rng[n] = (a, len(box))
        self._ranges[key] = rng
        self._owned[key] = (job['attrs'], job['objs'])
        page.setUpdatesEnabled(True)
        self._populate(key)
        self._flush_group_adjust()
        _restyle_setting_cards(page)
        self._built = True

    def _make_shell(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        self._category_bar = QWidget(self)
        self._category_bar.setObjectName('settingsCategoryBar')
        self._category_bar.setStyleSheet('background: transparent;')
        self._category_flow = FlowLayout(self._category_bar)
        self._category_flow.setContentsMargins(24, 10, 28, 4)
        self._category_flow.setHorizontalSpacing(6)
        self._category_flow.setVerticalSpacing(6)
        outer.addWidget(self._category_bar)
        self._category_group = QButtonGroup(self)
        self._category_group.setExclusive(True)
        self._stack = QStackedWidget(self)
        self._stack.setStyleSheet('QStackedWidget { background: transparent; border: none; }')
        outer.addWidget(self._stack, 1)
        for key, text_key, fallback in self._CATEGORIES:
            self._make_category_button(key, text_key, fallback)
        self._category_buttons[self._selected_category].setChecked(True)

    def _make_category_page(self, key: str):
        scroll = make_smooth_scroll_area(self._stack)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setStyleSheet('QScrollArea { background: transparent; border: none; }')
        scroll.viewport().setStyleSheet('background: transparent;')
        container = QWidget()
        container.setStyleSheet('background: transparent;')
        scroll.setWidget(container)
        layout = QVBoxLayout(container)
        layout.setContentsMargins(24, 4, 28, 24)
        layout.setSpacing(4)
        layout.addStretch(1)
        self._stack.addWidget(scroll)
        self._category_pages[key] = scroll
        self._category_layouts[key] = layout
        if self._scroll is None:
            self._scroll = scroll
        return scroll

    def _make_category_button(self, key: str, text_key: str, fallback: str) -> None:
        button = _PillButton(self._t(text_key, fallback), self._category_bar)
        button.setCheckable(True)
        button.setFixedHeight(30)
        button.clicked.connect(lambda _checked=False, k=key: self._show_category(k))
        button.hovered.connect(partial(self._prefetch, key))
        self._bind(button.setText, text_key, fallback)
        self._category_group.addButton(button)
        self._category_buttons[key] = button
        self._category_flow.addWidget(button)

    def _prefetch(self, key: str) -> None:
        if key not in self._loaded:
            QTimer.singleShot(45, self, partial(self._prefetch_now, key))

    def _prefetch_now(self, key: str) -> None:
        if key in self._loaded or not self.isVisible():
            return
        if self._category_buttons[key].underMouse():
            app = QApplication.instance()
            if app is None or app.activeModalWidget() is None:
                self._ensure_category(key, sliced=True)

    def _refresh_page(self, key: str) -> None:
        if key not in self._stale:
            return
        self._stale.discard(key)
        self._refresh_icons(key)

    def _teardown(self, key: str) -> None:
        if key == 'network':
            self._net_widgets.clear()
            self._net_apply = None
        elif key == 'shortcuts':
            self._cancel_shortcut_capture()
            self._shortcut_rows.clear()
        elif key == 'sailseek':
            self._sailseek_source_switches.clear()
            self._sailseek_dependents = []
            self._sailseek_status_lbl = None
        elif key == 'sections':
            self._download_dir_widgets.clear()
            self._download_dir_cards.clear()
            with suppress(Exception):
                self._vyla_update_finished.disconnect(self._on_vyla_update_finished)
        elif key == 'advanced':
            with suppress(Exception):
                self._cache_size_ready.disconnect(self.__dict__['_cache_ready_slot'])
            with suppress(Exception):
                self._cache_clear_finished.disconnect(self._on_cache_cleared)

    def _evict_pages(self, keep: str | None, sync: bool = False) -> None:
        for key in list(self._loaded):
            if key == keep or key not in self._owned or key in self._jobs:
                continue
            self._teardown(key)
            self._loaded.discard(key)
            rng = self._ranges.pop(key)
            for name in self._LISTS:
                a, b = rng[name]
                box = getattr(self, name)
                if isinstance(box, dict):
                    for k in list(box)[a:b]:
                        del box[k]
                else:
                    del box[a:b]
                for other in self._ranges.values():
                    x, y = other[name]
                    if x >= b:
                        other[name] = (x - (b - a), y - (b - a))
            attrs, objs = self._owned.pop(key)
            for name in attrs:
                self.__dict__.pop(name, None)
            for obj in objs:
                with suppress(RuntimeError):
                    if sync and shiboken6.isValid(obj):
                        shiboken6.delete(obj)
                    else:
                        obj.deleteLater()
            self._icon_entries.pop(key, None)
            self._stale.discard(key)
            self._category_layouts.pop(key, None)
            page = self._category_pages.pop(key, None)
            if page is not None:
                self._stack.removeWidget(page)
                page.hide()
                if sync and shiboken6.isValid(page):
                    shiboken6.delete(page)
                else:
                    page.deleteLater()

    def _show_category(self, key: str, animate: bool = True) -> None:
        if key not in self._category_buttons:
            return
        self._ensure_category(key)
        page = self._category_pages[key]
        button = self._category_buttons[key]
        if not button.isChecked():
            button.setChecked(True)
        self._active_category = self._selected_category = key
        self._scroll = page
        if self._capturing_shortcut is not None:
            self._cancel_shortcut_capture()
        if key in self._jobs:
            page.setUpdatesEnabled(True)
            self._schedule_jobs()
        if self._stack.currentWidget() is page:
            return
        self._refresh_page(key)
        self._stack.setCurrentWidget(page)
        if animate and key not in self._jobs:
            fade_in(page, 110)

    def scroll_to_group(self, key: str) -> None:
        target = self._GROUP_TARGETS.get(key)
        if target is None:
            return
        self._show_category(target[0], animate=False)
        self._ensure_category(target[0])
        widget = getattr(self, target[1], None)
        if widget is not None:
            QTimer.singleShot(0, lambda: self._scroll_to_widget(widget))

    def _scroll_to_widget(self, widget) -> None:
        def _apply() -> None:
            try:
                self._scroll.ensureWidgetVisible(widget, 0, 24)
            except Exception:
                with suppress(Exception):
                    self._scroll.verticalScrollBar().setValue(max(0, widget.y() - 12))
        _apply()
        QTimer.singleShot(80, _apply)

    def _populate(self, key: str | None = None) -> None:
        if key is None:
            for k in list(self._ranges):
                self._populate(k)
            return
        ranges = self._ranges.get(key)
        if ranges is None:
            return

        def part(name):
            a, b = ranges[name]
            return getattr(self, name)[a:b]

        self._loading = True
        try:
            for card, attr in part('_switches'):
                checked = bool(getattr(settings, attr, False))
                if isinstance(card, SwitchButton):
                    self._quiet_switch(card, checked)
                else:
                    self._quiet(card, card.setChecked, checked)
            for card, attr in part('_sections'):
                self._quiet_switch(card.switch, bool(getattr(settings, attr, False)))
            for combo, attr in part('_combos'):
                index = combo.findData(getattr(settings, attr, None))
                self._quiet(combo, combo.setCurrentIndex, max(0, index))
            for spin, attr in part('_spins'):
                with suppress(TypeError, ValueError):
                    self._quiet(spin, spin.setValue, int(getattr(settings, attr, 0) or 0))
            for box, attr in part('_checks'):
                self._quiet(box, box.setChecked, bool(getattr(settings, attr, False)))
        finally:
            self._loading = False
        fill = getattr(self, '_fill_' + key, None)
        if fill is not None:
            fill()

    def _fill_appearance(self) -> None:
        self._bg_path_edit.setText(settings.background_image_path or '')
        blur = int(settings.background_image_blur)
        self._quiet(self._bg_blur_slider, self._bg_blur_slider.setValue, blur)
        self._bg_blur_value_lbl.setText(str(blur))
        darken = int(settings.background_image_darken)
        self._quiet(self._bg_darken_slider, self._bg_darken_slider.setValue, darken)
        self._bg_darken_value_lbl.setText(f'{darken}%')
        self._accent_color_btn.setColor(QColor(settings.accent_color))
        language = current_language()
        if language in self._lang_codes:
            self._quiet(self._lang_combo, self._lang_combo.setCurrentIndex, self._lang_codes.index(language))
        off = self._t('settings.volume_off', 'Off')
        sfx = int(settings.sfx_volume)
        self._quiet(self._sfx_slider, self._sfx_slider.setValue, sfx)
        self._sfx_value_lbl.setText(str(sfx) if sfx > 0 else off)
        ambiance = int(settings.ambiance_volume)
        self._quiet(self._ambiance_slider, self._ambiance_slider.setValue, ambiance)
        self._ambiance_value_lbl.setText(str(ambiance) if ambiance > 0 else off)
        self._sync_theme_lock_state()

    def _fill_sections(self) -> None:
        self._quiet(self._edit_stream_addons, self._edit_stream_addons.setText,
                    getattr(settings, 'tv_stream_addons', '') or '')
        reorder = getattr(self, '_section_reorder', None)
        if reorder is not None:
            reorder.arrange(nav_order())

    def _fill_sailseek(self) -> None:
        for key, switch in self._sailseek_source_switches.items():
            self._quiet_switch(switch, self._sailseek_category_enabled(key))
        self.refresh_sailseek_status()
        self._sync_sailseek_state()

    def _fill_integrations(self) -> None:
        self._quiet(self._chk_launch_startup, self._chk_launch_startup.setChecked, bool(get_launch_at_startup()))
        self._refresh_debrid_token_state()
        if os.name == 'nt':
            self._refresh_associations_status()

    def _fill_advanced(self) -> None:
        if IS_CANARY:
            self._quiet_switch(self._chk_canary_update.switchButton, True)
            self._chk_canary_update.setEnabled(False)
        self._refresh_cache_size()

    def _fill_network(self) -> None:
        pending = self._pending_bind
        if pending is None:
            return
        source, apply = pending
        self._net_apply = apply
        self._net_loading = True
        try:
            prefs = source()
            for key, widget in self._net_widgets.items():
                value = prefs.get(key)
                if key == 'proxy_type':
                    widget.setCurrentIndex(max(0, widget.findData(value)))
                elif key == 'proxy_port':
                    widget.setValue(int(value or 0))
                elif isinstance(widget, LineEdit):
                    widget.setText(str(value or ''))
                else:
                    widget.setChecked(bool(value))
        finally:
            self._net_loading = False

    def bind_network(self, source, apply) -> None:
        self._pending_bind = (source, apply)
        if 'network' in self._ranges:
            self._fill_network()

    def _t(self, key: str, fallback: str | None = None, **kwargs) -> str:
        return tr(key, **kwargs) if fallback is None else tr_or(key, fallback, **kwargs)

    def _bind(self, setter, key: str, fallback: str | None = None, **kwargs) -> None:
        self._texts.append((setter, key, fallback, kwargs))
        with suppress(TypeError):
            setter(self._t(key, fallback, **kwargs))

    def _bind_item(self, combo: ComboBox, index: int, key: str, fallback: str | None = None) -> None:
        self._bind(partial(combo.setItemText, index), key, fallback)

    def _track_icon(self, widget, slot: str, icon, branded: bool = False) -> None:
        self._icon_entries.setdefault(self._active_category, []).append((widget, slot, icon, branded))

    def _track_group_icon(self, group, slot: str, icon, branded: bool = False) -> None:
        target = getattr(group, 'iconWidget', None)
        if target is not None:
            self._icon_entries.setdefault(self._active_category, []).append((target, slot, icon, branded))

    def _restyle_cards(self) -> None:
        _restyle_setting_cards(self)

    def _on_theme_refresh(self) -> None:
        if not self.isVisible():
            self._theme_dirty = True
            return
        self._run_theme_refresh()

    def _run_theme_refresh(self) -> None:
        self._theme_dirty = False
        self._stale = set(self._loaded)
        self.setUpdatesEnabled(False)
        try:
            self._sync_accent_button()
            self._refresh_page(self._selected_category)
            with suppress(Exception):
                qss = settings_qss()
                if self.styleSheet() != qss:
                    self.setStyleSheet(qss)
        finally:
            self.setUpdatesEnabled(True)

    def _refresh_icons(self, key: str) -> None:
        for entry in self._icon_entries.get(key, ()):
            widget, slot, icon, branded = entry
            target = (
                getattr(widget, 'iconLabel', None)
                or getattr(widget, 'iconWidget', None)
                or getattr(getattr(widget, 'card', None), 'iconLabel', None)
                or (widget if hasattr(widget, 'setIcon') else None)
            )
            if target is not None:
                with suppress(RuntimeError):
                    target.setIcon((branded_icon if branded else themed_icon)(slot, icon))

    def _sync_accent_button(self) -> None:
        button = getattr(self, '_accent_color_btn', None)
        if button is not None:
            with suppress(Exception):
                button.setColor(QColor(palette()['accent']))

    def _track_switch_text(self, switch: SwitchButton) -> None:
        if switch in self._switch_labels:
            return
        self._switch_labels[switch] = None
        switch.checkedChanged.connect(lambda checked, s=switch: self._set_switch_text(s, checked))
        self._set_switch_text(switch, switch.isChecked())

    def _set_switch_text(self, switch: SwitchButton, checked: bool) -> None:
        switch.setText(_on_off(checked))

    def _quiet(self, widget, setter, value) -> None:
        widget.blockSignals(True)
        try:
            setter(value)
        finally:
            widget.blockSignals(False)

    def _quiet_switch(self, switch: SwitchButton, checked: bool) -> None:
        self._quiet(switch, switch.setChecked, bool(checked))
        self._set_switch_text(switch, checked)

    def _tail(self, card, *widgets) -> None:
        if not widgets:
            return
        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(8)
        for widget in widgets:
            row.addWidget(widget, 0, Qt.AlignmentFlag.AlignVCenter)
        switch = getattr(card, 'switchButton', None)
        if switch is not None:
            row.setContentsMargins(0, 0, 8, 0)
            card.hBoxLayout.insertLayout(card.hBoxLayout.indexOf(switch), row)
        else:
            card.hBoxLayout.addLayout(row)
            card.hBoxLayout.setContentsMargins(16, 0, 16, 0)

    def _group(self, key: str, fallback: str = '') -> SettingCardGroup:
        group = SettingCardGroup(self._t(key, fallback or None), self)
        self._bind(group.titleLabel.setText, key, fallback or None)
        return group

    def _add_group(self, group: SettingCardGroup) -> None:
        layout = self._category_layouts.get(self._active_category)
        if layout is None:
            layout = self._category_layouts[self._CATEGORIES[0][0]]
        first = not any(
            isinstance(layout.itemAt(i).widget(), SettingCardGroup) for i in range(layout.count()))
        if first and self._active_category in ('appearance', 'integrations', 'advanced'):
            group.titleLabel.hide()
            group.titleLabel.setFixedHeight(0)
        elif self._active_category in ('downloads', 'network', 'sailseek', 'shortcuts'):
            group.titleLabel.hide()
        index = layout.count() - 1
        layout.insertWidget(index, group)
        group.show()
        layout.insertSpacing(index + 1, 12)

    def _add_card(self, group: SettingCardGroup, card: QWidget) -> None:
        if card.parentWidget() is not group:
            card.setParent(group)
        group.cardLayout.addWidget(card)
        card.show()
        if group not in self._pending_group_adjust:
            self._pending_group_adjust.add(group)
            QTimer.singleShot(0, self, self._flush_group_adjust)

    def _flush_group_adjust(self) -> None:
        pending = self._pending_group_adjust
        if not pending:
            return
        groups = list(pending)
        pending.clear()
        for group in groups:
            with suppress(RuntimeError):
                group.adjustSize()

    def _decorate(self, card, slot: str, icon, title_key: str, title_fb, content_key: str, content_fb,
                  content_kwargs: dict | None = None) -> None:
        self._track_icon(card, slot, icon)
        self._bind(card.setTitle, title_key, title_fb)
        kwargs = content_kwargs or {}
        self._bind(card.setContent, content_key, content_fb, **kwargs)

    def _plain_card(self, group, slot, icon, title_key, title_fb, content_key, content_fb, *tail,
                    content_kwargs: dict | None = None):
        kwargs = content_kwargs or {}
        card = SettingCard(
            themed_icon(slot, icon),
            self._t(title_key, title_fb),
            self._t(content_key, content_fb, **kwargs),
            group,
        )
        self._decorate(card, slot, icon, title_key, title_fb, content_key, content_fb, kwargs)
        self._tail(card, *tail)
        self._add_card(group, card)
        return card

    def _switch_card(self, group, slot, icon, title_key, title_fb, content_key, content_fb,
                     attr: str | None = None, on_change=None, *tail):
        card = SwitchSettingCard(
            icon=themed_icon(slot, icon),
            title=self._t(title_key, title_fb),
            content=self._t(content_key, content_fb),
            parent=group,
        )
        self._decorate(card, slot, icon, title_key, title_fb, content_key, content_fb)
        self._tail(card, *tail)
        self._track_switch_text(card.switchButton)
        if on_change is None and attr is not None:
            on_change = lambda checked, a=attr: self._save_switch(a, checked)
        if on_change is not None:
            card.checkedChanged.connect(on_change)
        if attr is not None:
            self._switches.append((card, attr))
        self._add_card(group, card)
        return card

    def _spin_card(self, group, slot, icon, title_key, title_fb, content_key, content_fb,
                   attr: str, minimum: int, maximum: int, suffix_key: str | None = None,
                   suffix_fb: str | None = None, prefix: str = '', special: str = ''):
        spin = CompactSpinBox()
        spin.setRange(minimum, maximum)
        if prefix:
            spin.setPrefix(prefix)
        if special:
            spin.setSpecialValueText(special)
        if suffix_key is not None:
            self._bind(spin.setSuffix, suffix_key, suffix_fb)
        spin.valueChanged.connect(lambda value, a=attr: self._save_spin(a, value))
        card = self._plain_card(group, slot, icon, title_key, title_fb, content_key, content_fb, spin)
        self._spins.append((spin, attr))
        return card, spin

    def _check(self, attr: str, key: str, fallback: str) -> CheckBox:
        box = CheckBox(self._t(key, fallback))
        self._bind(box.setText, key, fallback)
        box.toggled.connect(lambda checked, a=attr: self._save_check(a, checked))
        self._checks.append((box, attr))
        return box

    def _save_switch(self, attr: str, checked: bool) -> None:
        if not self._loading:
            self._apply_setting(attr, bool(checked))

    def _save_combo(self, combo: ComboBox, attr: str) -> None:
        if not self._loading:
            self._apply_setting(attr, combo.currentData())

    def _save_spin(self, attr: str, value: int) -> None:
        if not self._loading:
            self._apply_setting(attr, int(value))

    def _save_check(self, attr: str, checked: bool) -> None:
        if not self._loading:
            self._apply_setting(attr, bool(checked))

    def _apply_setting(self, attr: str, value) -> None:
        apply_settings(**{attr: value})
        if getattr(settings, attr, None) != value:
            setattr(settings, attr, value)
        save_settings(settings)
        self.settings_changed.emit()


    def _build_general(self):
        group = self._general_group = self._group('settings.group_appearance', 'Appearance')
        self._add_group(group)
        yield

        self._lang_codes = list(available_languages().keys())
        self._lang_combo = ComboBox()
        self._lang_combo.addItems([available_languages()[code] for code in self._lang_codes])
        self._lang_combo.setMinimumWidth(150)
        self._lang_combo.currentIndexChanged.connect(self._on_language_changed)
        self._lang_card = self._plain_card(
            group, 'settings.language', FluentIcon.LANGUAGE,
            'settings.language_title', None, 'settings.language_content', None,
            self._lang_combo,
        )
        yield

        self._theme_btn = PushButton(self._t('settings.theme_choose', 'Choose theme'))
        self._bind(self._theme_btn.setText, 'settings.theme_choose', 'Choose theme')
        self._theme_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._theme_btn.clicked.connect(self._on_theme_choose_clicked)
        self._theme_reload_btn = TransparentToolButton(themed_icon('settings.theme_reload', FluentIcon.SYNC), self)
        self._track_icon(self._theme_reload_btn, 'settings.theme_reload', FluentIcon.SYNC)
        apply_tool_button_theme(self._theme_reload_btn)
        self._theme_reload_btn.setFixedSize(32, 32)
        self._bind(self._theme_reload_btn.setToolTip, 'settings.theme_reload', 'Reload themes')
        self._theme_reload_btn.clicked.connect(self._on_theme_reload_clicked)
        self._plain_card(
            group, 'settings.theme_mode', FluentIcon.BRUSH,
            'settings.theme_mode_title', None, 'settings.theme_mode_content', None,
            self._theme_reload_btn, self._theme_btn,
        )
        yield

        self._animations_card = self._switch_card(
            group, 'settings.animations', FluentIcon.SPEED_HIGH,
            'settings.animations_title', 'Animations',
            'settings.animations_content', 'Animate navigation, cards, panels and transitions. Turn off for instant, motion-free interactions.',
            attr='animations_enabled')
        yield

        self._accent_color_btn = ColorPickerButton(
            QColor(palette()['accent']), self._t('settings.accent_color_title'), self, enableAlpha=False)
        self._bind(lambda text: setattr(self._accent_color_btn, 'title', text), 'settings.accent_color_title')
        self._accent_color_btn.colorChanged.connect(self._on_accent_color_changed)
        self._default_accent_btn = PushButton(self._t('settings.default_accent'))
        self._bind(self._default_accent_btn.setText, 'settings.default_accent')
        self._default_accent_btn.clicked.connect(self._on_default_accent_color)
        self._reset_accent_btn = PushButton(self._t('settings.use_windows_accent'))
        self._bind(self._reset_accent_btn.setText, 'settings.use_windows_accent')
        self._reset_accent_btn.clicked.connect(self._on_reset_accent_color)
        self._accent_card = self._plain_card(
            group, 'settings.accent_color', FluentIcon.PALETTE,
            'settings.accent_color_title', None, 'settings.accent_color_content', None,
            self._default_accent_btn, self._reset_accent_btn, self._accent_color_btn,
        )
        yield

        yield from self._build_background(group)

    def _build_background(self, group):
        self._bg_card = _SectionCard(
            themed_icon('settings.background', FluentIcon.PHOTO),
            self._t('settings.background_title', 'Custom Background'),
            self._t('settings.background_content', 'Use your own photo, GIF, or video as the app background'),
        )
        self._track_icon(self._bg_card, 'settings.background', FluentIcon.PHOTO)
        self._bind(self._bg_card.setTitle, 'settings.background_title', 'Custom Background')
        self._bind(self._bg_card.setContent, 'settings.background_content', 'Use your own photo, GIF, or video as the app background')
        self._track_switch_text(self._bg_card.switch)
        self._bg_card.switch.checkedChanged.connect(self._on_background_toggled)
        self._sections.append((self._bg_card, 'background_image_enabled'))
        yield

        self._bg_path_edit = LineEdit()
        self._bg_path_edit.setReadOnly(True)
        self._bg_path_edit.setMinimumWidth(210)
        self._bg_browse_btn = PushButton(self._t('settings.browse'))
        self._bind(self._bg_browse_btn.setText, 'settings.browse')
        self._bg_browse_btn.clicked.connect(self._on_browse_background_image)
        self._bg_clear_btn = PushButton(self._t('settings.background_clear', 'Clear'))
        self._bind(self._bg_clear_btn.setText, 'settings.background_clear', 'Clear')
        self._bg_clear_btn.clicked.connect(self._on_clear_background_image)
        row = _path_row(self._bg_path_edit, self._bg_browse_btn, self._bg_clear_btn)
        self._bg_photo_group_widget = self._add_group_row(
            self._bg_card, 'settings.background', FluentIcon.PHOTO,
            'settings.background_photo_title', 'Background file',
            'settings.background_photo_content', 'Pick an image, GIF, or video file to use as the background', row)
        yield

        blur, blur_label = self._make_slider_row(0, 40, 12, '')
        self._bg_blur_slider = blur
        self._bg_blur_value_lbl = blur_label
        self._bg_blur_commit = _SliderCommit(
            blur, self._on_background_blur_live, self._on_background_blur_commit, parent=self)
        self._bg_blur_group_widget = self._add_group_row(
            self._bg_card, 'settings.background_blur', FluentIcon.FILTER,
            'settings.background_blur_title', 'Blur',
            'settings.background_blur_content', 'How blurred the background is',
            _inline_row(blur, blur_label, spacing=8))
        yield

        darken, darken_label = self._make_slider_row(0, 90, 45, '%')
        self._bg_darken_slider = darken
        self._bg_darken_value_lbl = darken_label
        self._bg_darken_commit = _SliderCommit(
            darken, self._on_background_darken_live, self._on_background_darken_commit, parent=self)
        self._bg_darken_group_widget = self._add_group_row(
            self._bg_card, 'settings.background_darken', FluentIcon.QUIET_HOURS,
            'settings.background_darken_title', 'Darken',
            'settings.background_darken_content', 'How much to dim the background',
            _inline_row(darken, darken_label, spacing=8))
        self._add_card(group, self._bg_card)
        yield

    def _make_slider_row(self, minimum: int, maximum: int, value: int, suffix: str):
        slider = ThemedSlider(Qt.Orientation.Horizontal)
        slider.setRange(minimum, maximum)
        slider.setValue(value)
        label = CaptionLabel(f'{value}{suffix}')
        _fix_value_label_width(label)
        return slider, label

    def _add_group_row(self, card: _SectionCard, slot: str, icon, title_key: str, title_fb,
                       content_key: str, content_fb: str, widget) -> object:
        group = card.addGroup(
            themed_icon(slot, icon),
            self._t(title_key, title_fb),
            self._t(content_key, content_fb),
            widget,
        )
        self._track_group_icon(group, slot, icon)
        self._bind(group.setTitle, title_key, title_fb)
        self._bind(group.setContent, content_key, content_fb)
        return group

    def _build_downloads(self):
        group = self._dl_group = self._group('settings.group_downloads_network', 'Downloads & Network')
        self._add_group(group)
        yield

        self._chk_console_structure = self._switch_card(
            group, 'settings.console_structure', FluentIcon.IOT,
            'settings.console_structure_title', None, 'settings.console_structure_content', None,
            attr='console_structure')
        self._chk_delete_torrent = self._switch_card(
            group, 'settings.delete_torrent', FluentIcon.DELETE,
            'settings.delete_torrent_title', None, 'settings.delete_torrent_content', None,
            attr='delete_torrent_after')
        yield

        self._speed_card, self._spin_speed = self._spin_card(
            group, 'settings.download_limit', FluentIcon.SPEED_HIGH,
            'settings.download_limit_title', None, 'settings.download_limit_content', None,
            'speed_limit', 0, 100000, 'settings.suffix_kbps')
        self._upload_speed_card, self._spin_upload_speed = self._spin_card(
            group, 'settings.upload_limit', FluentIcon.SPEED_HIGH,
            'settings.upload_limit_title', None, 'settings.upload_limit_content', None,
            'upload_speed_limit', 0, 100000, 'settings.suffix_kbps')
        self._seed_card, self._spin_seed = self._spin_card(
            group, 'settings.seed_time', FluentIcon.HISTORY,
            'settings.seed_time_title', None, 'settings.seed_time_content', None,
            'seed_time', 0, 9999, 'settings.suffix_min')
        self._reserve_card, self._spin_reserve = self._spin_card(
            group, 'settings.min_free_space', FluentIcon.FOLDER,
            'settings.min_free_space_title', 'Keep free disk space',
            'settings.min_free_space_content', 'Refuse or stop downloads that would leave less than this free',
            'min_free_space_mb', 0, 10000000, 'settings.suffix_mb', 'MB')
        yield

        self._finish_combo = ComboBox()
        self._finish_combo.setMinimumWidth(150)
        for index, (key, fallback, action) in enumerate((
            ('settings.finish_action_nothing', 'Do nothing', 'nothing'),
            ('settings.finish_action_shutdown', 'Shut down', 'shutdown'),
            ('settings.finish_action_sleep', 'Sleep', 'sleep'),
        )):
            self._finish_combo.addItem(self._t(key, fallback), userData=action)
            self._bind_item(self._finish_combo, index, key, fallback)
        self._finish_combo.currentIndexChanged.connect(
            lambda _i: self._save_combo(self._finish_combo, 'on_finish_action'))
        self._finish_card = self._plain_card(
            group, 'settings.on_finish', FluentIcon.POWER_BUTTON,
            'settings.on_finish_title', None, 'settings.on_finish_content', None,
            self._finish_combo)
        self._combos.append((self._finish_combo, 'on_finish_action'))
        yield

        self._chk_close_to_tray = self._switch_card(
            group, 'settings.close_to_tray', FluentIcon.MINIMIZE,
            'settings.close_to_tray_title', None, 'settings.close_to_tray_content', None,
            attr='close_to_tray')
        yield

        self._chk_notify_download = self._switch_card(
            group, 'settings.notify_download', FluentIcon.RINGER,
            'settings.notify_download_title', 'Download complete notifications',
            'settings.notify_download_content',
            'Show a Windows notification whenever a download finishes.',
            attr='notify_on_download_complete')
        yield


    def _build_privacy(self):
        group = self._privacy_group = self._group('settings.privacy_group', 'Privacy')
        self._add_group(group)
        yield
        icons = self._NET_ICONS

        def add_row(key: str, *widgets):
            card = self._plain_card(
                group, f'settings.privacy_{key}', icons[key],
                f'settings.privacy_{key}_title', None,
                f'settings.privacy_{key}_content', None,
                *widgets,
            )
            return card

        def add_switch(key: str):
            card = self._switch_card(
                group, f'settings.privacy_{key}', icons[key],
                f'settings.privacy_{key}_title', None,
                f'settings.privacy_{key}_content', None,
                on_change=lambda checked, k=key: self._net_push(**{k: bool(checked)}),
            )
            self._net_widgets[key] = card
            return card

        def add_edit(key: str, width: int, placeholder: str = '', password: bool = False) -> LineEdit:
            edit = LineEdit()
            edit.setFixedWidth(width)
            edit.setClearButtonEnabled(True)
            if placeholder:
                edit.setPlaceholderText(placeholder)
            if password:
                edit.setEchoMode(LineEdit.EchoMode.Password)
            edit.editingFinished.connect(
                lambda k=key, e=edit: self._net_push(**{k: e.text().strip()}))
            self._net_widgets[key] = edit
            return edit

        proxy_type = ComboBox()
        proxy_type.setMinimumWidth(150)
        for value, label in (('none', self._t('settings.privacy_proxy_none')),
                             ('http', 'HTTP'), ('socks5', 'SOCKS5')):
            proxy_type.addItem(label, userData=value)
        self._bind_item(proxy_type, 0, 'settings.privacy_proxy_none')
        proxy_type.currentIndexChanged.connect(
            lambda index, c=proxy_type: self._net_push(proxy_type=c.itemData(index)))
        self._net_widgets['proxy_type'] = proxy_type
        add_row('proxy', proxy_type)
        yield

        port = CompactSpinBox()
        port.setRange(0, 65535)
        port.valueChanged.connect(lambda value: self._net_push(proxy_port=int(value)))
        self._net_widgets['proxy_port'] = port
        host_edit = add_edit('proxy_host', 200, '127.0.0.1')
        host_edit.setPlaceholderText('127.0.0.1')
        add_row('proxy_address', host_edit, port)
        yield

        user_edit = add_edit('proxy_user', 130)
        self._bind(user_edit.setPlaceholderText, 'settings.privacy_user_placeholder')
        pass_edit = add_edit('proxy_pass', 130, password=True)
        self._bind(pass_edit.setPlaceholderText, 'settings.privacy_pass_placeholder')
        add_row('proxy_login', user_edit, pass_edit)
        yield

        for key in ('proxy_peers', 'force_proxy', 'anonymous', 'require_encryption',
                    'dht', 'lsd', 'upnp'):
            add_switch(key)
            yield
        add_row('bind', add_edit('bind', 220, '10.8.0.2'))
        yield
        add_switch('private_history')
        yield


    def _section(self, group, slot: str, icon, title_key: str, content_key: str,
                 attr: str, on_change=None) -> _SectionCard:
        card = _SectionCard(
            themed_icon(slot, icon),
            self._t(title_key),
            self._t(content_key),
        )
        self._track_icon(card, slot, icon)
        self._bind(card.setTitle, title_key)
        self._bind(card.setContent, content_key)
        self._track_switch_text(card.switch)
        card.switch.checkedChanged.connect(
            on_change or (lambda checked, a=attr: self._save_switch(a, checked)))
        self._sections.append((card, attr))
        self._add_card(group, card)
        key = attr[:-8] if attr.endswith('_enabled') else ''
        if key in NAV_SOURCE_KEYS:
            reorder = getattr(self, '_section_reorder', None)
            if reorder is None:
                reorder = self._section_reorder = _SectionReorder(group, self._commit_section_order, self)
            card.enable_reorder(key, reorder.begin)
            reorder.register(card)
        return card

    def _commit_section_order(self) -> None:
        reorder = getattr(self, '_section_reorder', None)
        if reorder is not None:
            self._apply_setting('nav_order', [c.nav_key for c in reorder.cards()])

    def _add_download_folder(self, card: _SectionCard, slot: str, icon,
                             title_key: str, content_key: str, attr: str) -> None:
        edit = LineEdit()
        edit.setReadOnly(True)
        edit.setMinimumWidth(210)
        button = PushButton(self._t('settings.browse'))
        self._bind(button.setText, 'settings.browse')
        button.clicked.connect(
            lambda _checked=False, a=attr, e=edit: self._pick_download_folder(a, e))
        row = _path_row(edit, button)
        group = card.addGroup(
            themed_icon(slot, icon),
            self._t(title_key),
            self._t(content_key),
            row,
        )
        self._track_group_icon(group, slot, icon)
        self._bind(group.setTitle, title_key)
        self._bind(group.setContent, content_key)
        self._download_dir_widgets[attr] = (edit, button)
        self._download_dir_cards.append((group, title_key, content_key))

    def _add_section_switch(self, card: _SectionCard, slot: str, icon,
                            title_key: str, title_fb, content_key: str, content_fb,
                            attr: str) -> SwitchButton:
        switch = SwitchButton('', card, IndicatorPosition.RIGHT)
        self._quiet_switch(switch, bool(getattr(settings, attr, False)))
        self._track_switch_text(switch)
        switch.checkedChanged.connect(
            lambda checked, a=attr: self._save_switch(a, checked))
        group = card.addGroup(
            themed_icon(slot, icon),
            self._t(title_key, title_fb),
            self._t(content_key, content_fb),
            switch,
        )
        self._track_group_icon(group, slot, icon)
        self._bind(group.setTitle, title_key, title_fb)
        self._bind(group.setContent, content_key, content_fb)
        self._switches.append((switch, attr))
        return switch

    def _add_tab_switch(self, card: _SectionCard, scope: str, key: str, slot: str, icon,
                        title_key: str, content_key: str, icon_override=None,
                        branded: bool = False) -> None:
        switch = SwitchButton('', card, IndicatorPosition.RIGHT)
        self._quiet_switch(switch, is_tab_enabled(scope, key))
        self._track_switch_text(switch)
        switch.checkedChanged.connect(
            lambda checked, s=scope, k=key, w=switch: self._on_tab_switch(s, k, w, checked))
        default_icon = icon_override if icon_override is not None else icon
        if branded:
            rendered = branded_icon(slot, default_icon)
        elif icon_override is not None:
            rendered = icon_override
        else:
            rendered = themed_icon(slot, icon)
        group = card.addGroup(
            rendered,
            self._t(title_key),
            self._t(content_key),
            switch,
        )
        if icon_override is None or branded:
            self._track_group_icon(group, slot, default_icon, branded=branded)
        self._bind(group.setTitle, title_key)
        self._bind(group.setContent, content_key)

    def _on_tab_switch(self, scope: str, key: str, switch: SwitchButton, checked: bool) -> None:
        if set_tab_enabled(scope, key, bool(checked)):
            self._set_switch_text(switch, checked)
        else:
            self._quiet_switch(switch, True)

    def _build_sections_start(self):
        group = self._feature_group = self._group('settings.group_sections', 'Sections')
        self._add_group(group)
        yield

        self._chk_minerva = self._section(
            group, 'nav.minerva', FluentIcon.APPLICATION,
            'settings.minerva_title', 'settings.minerva_content', 'minerva_enabled')
        self._add_download_folder(
            self._chk_minerva, 'settings.folder_minerva', FluentIcon.FOLDER,
            'settings.download_folder_minerva_title', 'settings.download_folder_minerva_content',
            'download_dir_minerva')
        self._chk_minerva_extract = self._add_section_switch(
            self._chk_minerva, 'settings.minerva_extract', FluentIcon.ZIP_FOLDER,
            'settings.minerva_extract_title', 'Extract ROM archives',
            'settings.minerva_extract_content',
            'Unzip .zip, .7z, and .rar ROM downloads into a folder beside the archive. The original archive is kept.',
            'minerva_extract_roms')
        yield

        self._chk_pc_games = self._section(
            group, 'nav.pc_games', FluentIcon.GAME,
            'settings.pc_games_title', 'settings.pc_games_content', 'pc_games_enabled')
        self._add_download_folder(
            self._chk_pc_games, 'settings.folder_repacks', FluentIcon.FOLDER,
            'settings.download_folder_repacks_title', 'settings.download_folder_repacks_content',
            'download_dir_repacks')
        self._add_tab_switch(
            self._chk_pc_games, 'repacks', 'fitgirl', 'repacks.tab_fitgirl', FluentIcon.GAME,
            'settings.tab_fitgirl_title', 'settings.tab_fitgirl_content',
            icon_override=_repacks_tab_icon('fitgirl'), branded=True)
        self._add_tab_switch(
            self._chk_pc_games, 'repacks', 'gog', 'repacks.tab_gog', FluentIcon.GAME,
            'settings.tab_gog_title', 'settings.tab_gog_content',
            icon_override=_repacks_tab_icon('gog'), branded=True)
        card = self._chk_pc_games
        self._repack_source_combo = ComboBox()
        self._repack_source_combo.setMinimumWidth(150)
        self._repack_source_combo.addItem(self._t('settings.repack_source_ask', 'Ask every time'), userData='ask')
        self._bind_item(self._repack_source_combo, 0, 'settings.repack_source_ask', 'Ask every time')
        self._repack_source_combo.addItem('1337x', userData='1337x')
        self._repack_source_combo.addItem('RuTor', userData='rutor')
        self._repack_source_combo.currentIndexChanged.connect(
            lambda _i: self._save_combo(self._repack_source_combo, 'repack_preferred_source'))
        self._combos.append((self._repack_source_combo, 'repack_preferred_source'))
        self._add_group_row(
            card, 'settings.repack_source', FluentIcon.DOWNLOAD,
            'settings.repack_source_title', 'Preferred download source',
            'settings.repack_source_content',
            'Skip the source picker and use this mirror automatically when a repack offers several. Asks again if the mirror has no magnet link.',
            self._repack_source_combo)
        self._add_section_switch(
            card, 'settings.repack_show_titles', FluentIcon.FONT,
            'settings.repack_show_titles_title', 'Show titles under cards',
            'settings.repack_show_titles_content', 'Display the game title below each cover in the grid.',
            'repack_show_titles')
        self._add_section_switch(
            card, 'settings.steam_enrichment', FluentIcon.GLOBE,
            'settings.steam_enrichment_title', 'Steam enrichment',
            'settings.steam_enrichment_content',
            'Add optional Steam details, screenshots and trailers to FitGirl games. Cached; works offline.',
            'steam_enrichment_enabled')
        for attr, title, content in (
                ('steam_banner', 'Steam banner', 'Show the game hero behind the details page.'),
                ('steam_logo', 'Steam logo', 'Show the game logo instead of the title text.'),
                ('steam_description', 'Steam description',
                 'Replace the repack description with Steam\'s, including images. Uses more data and memory.')):
            self._add_section_switch(
                card, f'settings.{attr}', FluentIcon.GAME,
                f'settings.{attr}_title', title, f'settings.{attr}_content', content, attr)
        yield

        self._chk_music = self._section(
            group, 'nav.music', FluentIcon.MUSIC,
            'settings.music_title', 'settings.music_content', 'music_enabled')
        self._add_download_folder(
            self._chk_music, 'settings.folder_music', FluentIcon.FOLDER,
            'settings.download_folder_music_title', 'settings.download_folder_music_content',
            'download_dir_music')
        yield

    def _build_sections_books(self):
        group = self._feature_group
        self._chk_books = self._section(
            group, 'nav.books', FluentIcon.BOOK_SHELF,
            'settings.books_title', 'settings.books_content', 'books_enabled')
        self._add_download_folder(
            self._chk_books, 'settings.folder_manga', FluentIcon.DOCUMENT,
            'settings.download_folder_manga_title', 'settings.download_folder_manga_content',
            'download_dir_manga')
        self._add_tab_switch(
            self._chk_books, 'books', 'manga', 'settings.tab_manga', FluentIcon.DICTIONARY,
            'settings.tab_manga_title', 'settings.tab_manga_content')
        yield

        self._chk_youtube = self._section(
            group, 'nav.youtube', FluentIcon.PLAY,
            'settings.youtube_title', 'settings.youtube_content', 'youtube_enabled')
        self._add_download_folder(
            self._chk_youtube, 'settings.folder_youtube', FluentIcon.FOLDER,
            'settings.download_folder_youtube_title', 'settings.download_folder_youtube_content',
            'download_dir_youtube')
        yield

    def _tv_check(self, attr: str, key: str, fallback: str) -> CheckBox:
        return self._check(attr, key, fallback)

    def _tv_combo(self, attr: str, items) -> ComboBox:
        combo = ComboBox()
        for index, (label, value, *key) in enumerate(items):
            combo.addItem(label, userData=value)
            if key:
                self._bind_item(combo, index, key[0], label)
        combo.currentIndexChanged.connect(lambda _i, c=combo, a=attr: self._save_combo(c, a))
        self._combos.append((combo, attr))
        return combo

    def _tv_spin(self, attr: str, minimum: int, maximum: int, prefix=None, suffix=None,
                 special=None) -> CompactSpinBox:
        spin = CompactSpinBox()
        spin.setRange(minimum, maximum)
        for setter, text in ((spin.setPrefix, prefix), (spin.setSuffix, suffix),
                             (spin.setSpecialValueText, special)):
            if text:
                self._bind(setter, *text)
        spin.valueChanged.connect(lambda value, a=attr: self._save_spin(a, value))
        self._spins.append((spin, attr))
        return spin

    def _tv_row(self, icon, title_key: str, title_fb: str,
                content_key: str | None, content_fb: str, widget):
        group = self._chk_tv.addGroup(
            themed_icon('settings.tv_stream', icon),
            self._t(title_key, title_fb),
            self._t(content_key, content_fb) if content_key else '',
            widget,
        )
        self._track_group_icon(group, 'settings.tv_stream', icon)
        self._bind(group.setTitle, title_key, title_fb)
        if content_key:
            self._bind(group.setContent, content_key, content_fb)
        return group

    def _build_sections_tv(self):
        group = self._feature_group
        self._chk_tv = self._section(
            group, 'nav.tv', FluentIcon.PROJECTOR,
            'settings.tv_title', 'settings.tv_content', 'tv_enabled')
        self._add_download_folder(
            self._chk_tv, 'settings.folder_anime', FluentIcon.VIDEO,
            'settings.download_folder_anime_title', 'settings.download_folder_anime_content',
            'download_dir_anime')
        yield

        tab_boxes = []
        for key, fallback in (('movies', 'Movies'), ('series', 'Series'),
                              ('anime', 'Anime'), ('channels', 'Channels')):
            box = CheckBox(self._t(f'tv.tab_{key}', fallback))
            self._bind(box.setText, f'tv.tab_{key}', fallback)
            self._quiet(box, box.setChecked, is_tab_enabled('tv', key))
            box.toggled.connect(lambda checked, k=key, b=box: self._on_tv_tab(k, b, checked))
            tab_boxes.append(box)
        self._tv_row(FluentIcon.TILES, 'settings.tv_tabs_title', 'Tabs', None, '',
                     _inline_row(*tab_boxes))
        yield

        self._sw_pip_top = SwitchButton('', self, IndicatorPosition.RIGHT)
        self._quiet_switch(self._sw_pip_top, bool(settings.tv_pip_always_on_top))
        self._track_switch_text(self._sw_pip_top)
        self._sw_pip_top.checkedChanged.connect(
            lambda checked: self._save_switch('tv_pip_always_on_top', checked))
        self._switches.append((self._sw_pip_top, 'tv_pip_always_on_top'))
        self._tv_row(FluentIcon.PIN, 'settings.tv_pip_title', 'Picture-in-picture on top',
                     'settings.tv_pip_content',
                     'Keep the floating TV player above other windows.',
                     self._sw_pip_top)
        yield

        self._sw_auto_pick = SwitchButton('', self, IndicatorPosition.RIGHT)
        self._quiet_switch(self._sw_auto_pick, bool(settings.tv_auto_pick))
        self._track_switch_text(self._sw_auto_pick)
        self._sw_auto_pick.checkedChanged.connect(
            lambda checked: self._save_switch('tv_auto_pick', checked))
        self._tv_row(FluentIcon.SPEED_HIGH, 'settings.tv_auto_pick_title', 'Auto-pick stream',
                     'settings.tv_auto_pick_content',
                     'Direct sources always start instantly. Turn this on to skip the list for torrents too.',
                     self._sw_auto_pick)
        yield

        quality = self._tv_combo('tv_pick_quality', [
            ('Auto', 'auto', 'settings.tv_quality_auto'), ('4K', '4k'), ('1080p', '1080p'), ('720p', '720p'), ('480p', '480p')])
        provider = self._tv_combo('tv_pick_provider', [
            ('Any provider', 'auto', 'settings.tv_provider_any'), ('Torrentio', 'torrentio'), ('The Black Hole Bay', 'tbhb'),
            ('Pirate Bay', 'apibay'), ('Custom addons', 'addon', 'settings.tv_provider_addons'), ('1337x', '1337x'),
            ('YTS', 'yts'), ('EZTV', 'eztv'), ('TorrentGalaxy', 'torrentgalaxy'),
            ('RARBG', 'rarbg'), ('KickassTorrents', 'kickass'), ('MagnetDL', 'magnetdl'),
            ('Nyaa', 'nyaa'), ('Rutor', 'rutor')])
        self._tv_row(FluentIcon.VIDEO, 'settings.tv_pick_title', 'Preferred quality and provider',
                     None, '', _inline_row(quality, provider))
        yield

        self._tv_row(FluentIcon.SEARCH, 'settings.tv_sources_title', 'Search sources',
                     'settings.tv_sources_content',
                     'Torrentio and your custom addons are always searched.',
                     _inline_row(self._check('tv_tbhb_enabled', 'settings.tv_tbhb_enabled',
                                             'The Black Hole Bay')))

        yield

        self._external_switch = SwitchButton('', self, IndicatorPosition.RIGHT)
        self._quiet_switch(self._external_switch, bool(settings.tv_external_enabled))
        self._track_switch_text(self._external_switch)
        self._external_switch.checkedChanged.connect(self._on_external_toggled)
        self._switches.append((self._external_switch, 'tv_external_enabled'))
        self._tv_row(FluentIcon.CLOUD, 'settings.external_providers_title', 'Native providers',
                     'settings.external_providers_content',
                     'Run the bundled Vyla provider inside PiraChest',
                     self._external_switch)
        yield

        self._vyla_switch = SwitchButton('', self, IndicatorPosition.RIGHT)
        self._quiet_switch(self._vyla_switch, bool(settings.tv_vyla_enabled))
        self._track_switch_text(self._vyla_switch)
        self._vyla_switch.checkedChanged.connect(
            lambda checked: self._save_switch('tv_vyla_enabled', checked))
        self._switches.append((self._vyla_switch, 'tv_vyla_enabled'))
        self._tv_row(FluentIcon.GLOBE, 'settings.vyla_enabled_title', 'Vyla',
                     'settings.vyla_enabled_content',
                     'Resolve movies and shows through the bundled Vyla SDK',
                     self._vyla_switch)
        yield

        self._external_status_lbl = CaptionLabel('')
        self._external_status_lbl.setWordWrap(True)
        self._btn_external_setup = PushButton(self._t('settings.external_setup', 'Set up runtime'))
        self._bind(self._btn_external_setup.setText, 'settings.external_setup', 'Set up runtime')
        self._btn_external_setup.clicked.connect(self._on_external_setup)
        self._btn_vyla_update = PushButton(self._t('settings.vyla_update', 'Update Vyla SDK'))
        self._bind(self._btn_vyla_update.setText, 'settings.vyla_update', 'Update Vyla SDK')
        self._btn_vyla_update.clicked.connect(self._on_vyla_update)
        self._vyla_update_finished.connect(self._on_vyla_update_finished)
        status_box = QWidget()
        status_layout = QVBoxLayout(status_box)
        status_layout.setContentsMargins(0, 0, 0, 0)
        status_layout.setSpacing(6)
        status_layout.addWidget(self._external_status_lbl)
        button_row = QHBoxLayout()
        button_row.setContentsMargins(0, 0, 0, 0)
        button_row.setSpacing(8)
        button_row.addWidget(self._btn_external_setup, 0, Qt.AlignmentFlag.AlignVCenter)
        button_row.addWidget(self._btn_vyla_update, 0, Qt.AlignmentFlag.AlignVCenter)
        button_row.addStretch(1)
        status_layout.addLayout(button_row)
        self._external_status_card = self._tv_row(
            FluentIcon.INFO, 'settings.external_status_title', 'Provider runtime',
            'settings.external_status_content', 'Node.js and the bundled Vyla SDK',
            status_box)
        QTimer.singleShot(120, self._refresh_external_status)
        yield

        self._tv_row(FluentIcon.FOLDER, 'settings.tv_pick_size_title', 'File size',
                     'settings.tv_pick_size_content', 'Auto means no limit.',
                     _inline_row(
                         self._tv_spin('tv_pick_min_gb', 0, 200, _MIN_PREFIX, _GB, ('settings.tv_min_auto', 'Min: Auto')),
                         self._tv_spin('tv_pick_max_gb', 0, 500, _MAX_PREFIX, _GB, ('settings.tv_max_auto', 'Max: Auto'))))

        yield

        self._tv_row(FluentIcon.PLAY, 'settings.tv_playback_title', 'Playback', None, '',
                     _inline_row(
                         self._tv_check('tv_stream_resume', 'settings.tv_stream_resume', 'Resume'),
                         self._tv_check('tv_stream_fallback', 'settings.tv_stream_fallback',
                                        'Next source on failure'),
                         self._tv_check('tv_stream_clear_on_exit', 'settings.tv_stream_clear_on_exit',
                                        'Clear cache on exit')))

        yield

        self._tv_row(FluentIcon.RIGHT_ARROW, 'settings.tv_skip_title', 'Auto skip', None, '',
                     _inline_row(
                         self._tv_check('tv_skip_intro', 'settings.tv_skip_intro', 'Intros'),
                         self._tv_check('tv_skip_credits', 'settings.tv_skip_credits', 'Credits'),
                         self._tv_check('tv_autonext', 'settings.tv_autonext', 'Next episode')))

        yield

        self._edit_stream_addons = LineEdit()
        self._edit_stream_addons.setMinimumWidth(300)
        self._edit_stream_addons.setClearButtonEnabled(True)
        self._bind(self._edit_stream_addons.setPlaceholderText, 'settings.tv_stream_addons_ph',
                   'https://addon.example.com (one per line)')
        self._edit_stream_addons.editingFinished.connect(self._on_stream_addons_changed)
        self._addons_save_timer = QTimer(self)
        self._addons_save_timer.setSingleShot(True)
        self._addons_save_timer.setInterval(600)
        self._addons_save_timer.timeout.connect(self._on_stream_addons_changed)
        self._edit_stream_addons.textChanged.connect(lambda _text: self._addons_save_timer.start())
        self._tv_row(FluentIcon.GLOBE, 'settings.tv_stream_addons_title', 'Extra Stremio addons',
                     None, '', self._edit_stream_addons)

        yield

        self._tv_row(FluentIcon.STOP_WATCH, 'settings.tv_net_title', 'Peer wait and buffer', None, '',
                     _inline_row(
                         self._tv_spin('tv_stream_connect_timeout', 15, 300, ('settings.tv_wait_prefix', 'Wait '), ('settings.unit_s', ' s')),
                         self._tv_spin('tv_stream_prebuffer_mb', 2, 64, ('settings.tv_buffer_prefix', 'Buffer '), ('settings.unit_mb', ' MB'))))

        cache_spin = self._tv_spin('tv_stream_cache_limit_gb', 0, 500, None, _GB, ('settings.tv_unlimited', 'Unlimited'))
        self._stream_cache_trim_timer = QTimer(self)
        self._stream_cache_trim_timer.setSingleShot(True)
        self._stream_cache_trim_timer.setInterval(500)
        self._stream_cache_trim_timer.timeout.connect(self._trim_stream_cache)
        cache_spin.valueChanged.connect(lambda _value: self._schedule_stream_trim())
        self._tv_row(FluentIcon.DELETE, 'settings.tv_cache_limit_title', 'Stream cache limit',
                     'settings.tv_cache_limit_content',
                     'Oldest buffered data is deleted past this size.', cache_spin)
        yield

    def _on_tv_tab(self, key: str, box: CheckBox, checked: bool) -> None:
        if not set_tab_enabled('tv', key, bool(checked)):
            self._quiet(box, box.setChecked, True)

    def _schedule_stream_trim(self) -> None:
        if not self._loading:
            self._stream_cache_trim_timer.start()

    @staticmethod
    def _trim_stream_cache() -> None:
        from src.core import worker as _worker
        from src.core.cache import trim_stream_cache
        _worker.submit(trim_stream_cache, priority=_worker.Priority.LOW)

    def _build_sections_end(self):
        self._chk_tbhb = self._section(
            self._feature_group, 'nav.tbhb', FluentIcon.GLOBE,
            'settings.tbhb_title', 'settings.tbhb_content', 'tbhb_enabled')
        self._add_download_folder(
            self._chk_tbhb, 'settings.folder_tbhb', FluentIcon.FOLDER,
            'settings.download_folder_tbhb_title', 'settings.download_folder_tbhb_content',
            'download_dir_tbhb')
        yield

        group = self._sections_general_group = self._group('settings.group_sections_general', 'General')
        self._add_group(group)
        yield

        self._history_card = self._switch_card(
            group, 'settings.search_history', FluentIcon.HISTORY,
            'settings.search_history_title', 'Search history',
            'settings.search_history_content',
            'Show recent searches under each search bar. Turn off to stop recording them.',
            attr='search_history_enabled')
        yield

        clear_history = PushButton(self._t('settings.clear_search_history', 'Clear search history'))
        self._bind(clear_history.setText, 'settings.clear_search_history', 'Clear search history')
        clear_history.clicked.connect(self._on_clear_search_history)
        self._plain_card(
            group, 'settings.clear_search_history', FluentIcon.DELETE,
            'settings.clear_search_history_title', 'Clear search history',
            'settings.clear_search_history_content', 'Remove all saved searches from every search bar.',
            clear_history)
        yield


    def _build_sound(self):
        group = self._sound_group = self._group('settings.group_sound', 'Sound & Notifications')
        self._add_group(group)
        yield

        self._sfx_slider = ThemedSlider(Qt.Orientation.Horizontal)
        self._sfx_slider.setRange(0, 100)
        self._sfx_slider.setValue(_SFX_DEFAULT_VOLUME)
        self._sfx_value_lbl = CaptionLabel(str(_SFX_DEFAULT_VOLUME))
        _fix_value_label_width(self._sfx_value_lbl)
        self._sfx_commit = _SliderCommit(
            self._sfx_slider, self._on_sfx_volume_live, self._on_sfx_volume_commit, parent=self)
        self._sfx_card = self._plain_card(
            group, 'settings.sfx_volume', FluentIcon.VOLUME,
            'settings.sfx_volume_title', 'Sound effects',
            'settings.sfx_volume_content', 'Hover, click, and navigation sounds. Set to 0 to turn off.',
            self._sfx_slider, self._sfx_value_lbl)
        yield

        self._ambiance_slider = ThemedSlider(Qt.Orientation.Horizontal)
        self._ambiance_slider.setRange(0, 100)
        self._ambiance_slider.setValue(_AMBIANCE_DEFAULT_VOLUME)
        self._ambiance_value_lbl = CaptionLabel(str(_AMBIANCE_DEFAULT_VOLUME))
        _fix_value_label_width(self._ambiance_value_lbl)
        self._ambiance_commit = _SliderCommit(
            self._ambiance_slider, self._on_ambiance_volume_live,
            self._on_ambiance_volume_commit, parent=self)
        self._ambiance_card = self._plain_card(
            group, 'settings.ambiance_volume', FluentIcon.MUSIC,
            'settings.ambiance_volume_title', 'Ambiance track',
            'settings.ambiance_volume_content',
            'A looping background track. Pauses automatically while music or video is playing. Set to 0 to turn off.',
            self._ambiance_slider, self._ambiance_value_lbl)
        yield


    _SAILSEEK_SOURCES = (
        ('downloads', FluentIcon.DOWNLOAD, 'Downloads', 'Your active and finished downloads.'),
        ('games', FluentIcon.GAME, 'PC Games', 'FitGirl and GOG repacks, plus your bookmarks.'),
        ('roms', FluentIcon.APPLICATION, 'ROMs', 'The Minerva ROM library.'),
        ('movies', FluentIcon.VIDEO, 'Movies', 'Movie results from TMDB.'),
        ('tv', FluentIcon.PROJECTOR, 'TV', 'Series results from TMDB.'),
        ('anime', FluentIcon.PLAY, 'Anime', 'Anime results from AniList.'),
        ('manga', FluentIcon.DICTIONARY, 'Manga', 'Manga results from MangaDex.'),
    )

    def _build_sailseek(self):
        group = self._sailseek_group = self._group('settings.group_sailseek', 'SailSeek')
        self._add_group(group)
        self._sailseek_dependents = []
        yield

        self._chk_sailseek = self._switch_card(
            group, 'settings.sailseek_enabled', FluentIcon.SEARCH,
            'settings.sailseek_enabled_title', 'SailSeek',
            'settings.sailseek_enabled_content',
            'Open the SailSeek search window from anywhere with the shortcut below.',
            attr='sailseek_enabled', on_change=self._on_sailseek_enabled)
        yield

        sources = _ExpandCard(
            themed_icon('settings.sailseek_categories', FluentIcon.TILES),
            self._t('settings.sailseek_categories_title', 'Search in'),
            self._t('settings.sailseek_categories_content',
                    'Choose what SailSeek looks through. Sections you turned off in Sections are skipped.'),
            self)
        self._track_icon(sources, 'settings.sailseek_categories', FluentIcon.TILES)
        self._bind(sources.setTitle, 'settings.sailseek_categories_title', 'Search in')
        self._bind(sources.setContent, 'settings.sailseek_categories_content',
                   'Choose what SailSeek looks through. Sections you turned off in Sections are skipped.')
        self._sailseek_source_switches = {}
        for key, icon, title_fb, content_fb in self._SAILSEEK_SOURCES:
            self._add_sailseek_source(sources, key, icon, title_fb, content_fb)
        self._add_card(group, sources)
        self._sailseek_dependents.append(sources)
        yield

        card, self._spin_sailseek_limit = self._spin_card(
            group, 'settings.sailseek_limit', FluentIcon.TILES,
            'settings.sailseek_limit_title', 'Maximum results',
            'settings.sailseek_limit_content',
            'How many results SailSeek keeps per search. Lower is faster on slower machines.',
            attr='sailseek_max_results', minimum=5, maximum=100)
        self._sailseek_limit_card = card
        self._sailseek_dependents.append(card)
        card, self._spin_sailseek_delay = self._spin_card(
            group, 'settings.sailseek_delay', FluentIcon.SPEED_HIGH,
            'settings.sailseek_delay_title', 'Search delay',
            'settings.sailseek_delay_content',
            'How long SailSeek waits after you stop typing before it queries TMDB and other online '
            'sources. Higher values mean fewer requests. Local matches always appear instantly.',
            attr='sailseek_delay_ms', minimum=250, maximum=2000,
            suffix_key='settings.suffix_ms', suffix_fb=' ms')
        self._sailseek_dependents.append(card)
        yield

        for slot, icon, key, title_fb, content_fb, attr in (
            ('settings.sailseek_translucent', FluentIcon.VIEW, 'translucent', 'Translucent window',
             'Let your theme background show through a soft translucent surface.',
             'sailseek_translucent'),
            ('settings.sailseek_artwork', FluentIcon.PHOTO, 'artwork', 'Show artwork',
             'Posters and covers next to results. Turn off for a lighter, text-only list.',
             'sailseek_show_artwork'),
            ('settings.sailseek_filters', FluentIcon.FILTER, 'filters', 'Show filter bar',
             'Small filter chips under the results. Tab and prefixes like g: or r: work either way.',
             'sailseek_show_filters'),
            ('settings.sailseek_remember', FluentIcon.PIN, 'remember', 'Remember position',
             'Reopen where you last dragged it. When off, it always opens centered on the active monitor.',
             'sailseek_remember_position'),
        ):
            card = self._switch_card(
                group, slot, icon,
                f'settings.sailseek_{key}_title', title_fb,
                f'settings.sailseek_{key}_content', content_fb, attr=attr)
            self._sailseek_dependents.append(card)
            yield

        self._sailseek_status_lbl = CaptionLabel('')
        self._sailseek_status_lbl.setAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self._sailseek_status_lbl.setMinimumWidth(190)
        self._btn_sailseek_reset = PushButton(
            self._t('settings.sailseek_reset_button', 'Reset position'))
        self._bind(self._btn_sailseek_reset.setText, 'settings.sailseek_reset_button',
                   'Reset position')
        self._btn_sailseek_reset.clicked.connect(self._on_sailseek_reset_position)
        self._plain_card(
            group, 'settings.sailseek_position', FluentIcon.FIT_PAGE,
            'settings.sailseek_position_title', 'Shortcut & position',
            'settings.sailseek_position_content',
            'Change the shortcut under Shortcuts. Reset if the window opens off-screen.',
            self._sailseek_status_lbl, self._btn_sailseek_reset)
        self.refresh_sailseek_status()
        self._sync_sailseek_state()
        yield

    def _add_sailseek_source(self, card, key: str, icon, title_fb: str, content_fb: str) -> None:
        slot = f'settings.sailseek_src_{key}'
        title_key = f'sailseek.category_{key}'
        content_key = f'settings.sailseek_src_{key}_content'
        switch = SwitchButton('', card, IndicatorPosition.RIGHT)
        self._quiet_switch(switch, self._sailseek_category_enabled(key))
        self._track_switch_text(switch)
        switch.checkedChanged.connect(
            lambda checked, k=key: self._save_sailseek_category(k, checked))
        row = card.addGroup(
            themed_icon(slot, icon),
            self._t(title_key, title_fb),
            self._t(content_key, content_fb),
            switch,
        )
        self._track_group_icon(row, slot, icon)
        self._bind(row.setTitle, title_key, title_fb)
        self._bind(row.setContent, content_key, content_fb)
        self._sailseek_source_switches[key] = switch

    def _on_sailseek_enabled(self, checked: bool) -> None:
        self._save_switch('sailseek_enabled', checked)
        self._sync_sailseek_state()

    def _sync_sailseek_state(self) -> None:
        enabled = bool(getattr(settings, 'sailseek_enabled', True))
        for widget in self._sailseek_dependents:
            with suppress(RuntimeError):
                widget.setEnabled(enabled)
        page = self._category_pages.get('sailseek')
        if page is not None:
            _restyle_setting_cards(page)

    def _sailseek_category_enabled(self, key: str) -> bool:
        raw = getattr(settings, 'sailseek_categories', None) or {}
        try:
            return bool(raw.get(key, True))
        except Exception:
            return True

    def _save_sailseek_category(self, key: str, checked: bool) -> None:
        if self._loading:
            return
        raw = dict(getattr(settings, 'sailseek_categories', None) or {})
        raw[key] = bool(checked)
        self._apply_setting('sailseek_categories', raw)

    def _set_sailseek_status(self, text: str) -> None:
        label = self._sailseek_status_lbl
        if label is None:
            return
        with suppress(RuntimeError):
            label.setText(text or '')

    def refresh_sailseek_status(self) -> None:
        controller = getattr(self.window(), 'sailseek', None)
        if controller is None:
            self._set_sailseek_status(
                self._t('settings.sailseek_status_ready', 'Shortcut status available in the app'))
            return
        if not getattr(self, '_sailseek_status_connected', False):
            with suppress(Exception):
                controller.status_changed.connect(self._set_sailseek_status)
                self._sailseek_status_connected = True
        with suppress(Exception):
            self._set_sailseek_status(controller.status_text())

    def _on_sailseek_reset_position(self) -> None:
        controller = getattr(self.window(), 'sailseek', None)
        if controller is None:
            return
        with suppress(Exception):
            controller.reset_geometry()
        InfoBar.success(
            title=self._t('settings.sailseek_reset_done_title', 'Position reset'),
            content=self._t('settings.sailseek_reset_done_content',
                            'SailSeek will open centered on the active monitor.'),
            orient=Qt.Orientation.Horizontal, isClosable=True,
            position=InfoBarPosition.TOP_RIGHT, duration=2500, parent=self.window())


    def _build_integrations(self):
        group = self._integrations_group = self._group('settings.group_integrations', 'Integrations')
        self._add_group(group)
        yield

        self._chk_external_client = self._switch_card(
            group, 'settings.external_client', FluentIcon.SEND,
            'settings.external_client_title', None, 'settings.external_client_content', None,
            attr='external_torrent_client')
        yield

        self._chk_launch_startup = self._switch_card(
            group, 'settings.launch_at_startup', FluentIcon.SPEED_HIGH,
            'settings.launch_at_startup_title', None, 'settings.launch_at_startup_content', None,
            on_change=self._on_launch_at_startup_toggled,
        )
        yield

        if os.name != 'nt':
            return
        yield

        self._btn_make_default = _AccentButton(self._t('settings.make_default_button'))
        self._bind(self._btn_make_default.setText, 'settings.make_default_button')
        self._btn_make_default.clicked.connect(self._on_make_default_torrent_client)
        self._make_default_card = self._plain_card(
            group, 'settings.make_default', FluentIcon.RINGER,
            'settings.make_default_title', None, 'settings.make_default_content', None,
            self._btn_make_default)
        yield

        self._assoc_status_lbl = CaptionLabel('')
        self._btn_repair_associations = PushButton(self._t('settings.repair_associations_button'))
        self._bind(self._btn_repair_associations.setText, 'settings.repair_associations_button')
        self._btn_repair_associations.clicked.connect(self._on_repair_associations)
        self._repair_associations_card = self._plain_card(
            group, 'settings.repair_associations', FluentIcon.SYNC,
            'settings.repair_associations_title', None,
            'settings.repair_associations_content', None,
            self._assoc_status_lbl, self._btn_repair_associations)
        self._refresh_associations_status()
        yield


    def _on_external_toggled(self, checked: bool) -> None:
        self._save_switch('tv_external_enabled', checked)
        if checked:
            self._on_external_setup()
        else:
            from src.core.tv import external as external_backend
            external_backend.shutdown_external()

    def _on_external_setup(self) -> None:
        from src.core.tv import external as external_backend
        self._external_setup_requested = True
        external_backend.ensure_runtime(wait=False)
        self._refresh_external_status()
        timer = getattr(self, '_external_timer', None)
        if timer is None:
            timer = self._external_timer = QTimer(self)
            timer.setInterval(1000)
            timer.timeout.connect(self._refresh_external_status)
        timer.start()

    def _on_vyla_update(self) -> None:
        from src.core import worker as _worker
        from src.core.tv import external as external_backend
        self._btn_vyla_update.setEnabled(False)
        self._external_status_lbl.setText(
            self._t('settings.vyla_update_running', 'Updating Vyla SDK...'))
        _worker.submit(
            external_backend.update_vyla_sdk,
            on_done=lambda version: self._vyla_update_finished.emit(str(version or ''), ''),
            on_error=lambda message: self._vyla_update_finished.emit(
                '', str(message) or 'unknown error'),
            owner=self, key='vyla-update', priority=_worker.Priority.NORMAL)

    def _on_vyla_update_finished(self, version: str, error: str) -> None:
        self._btn_vyla_update.setEnabled(True)
        self._refresh_external_status()
        if error:
            logger.error('Vyla SDK update failed: %s', error)
            InfoBar.error(
                title=self._t('settings.vyla_update_failed', 'Vyla SDK update failed'),
                content=error, duration=6000, parent=self.window())
            return
        InfoBar.success(
            title=self._t('settings.vyla_update_done', 'Vyla SDK updated'),
            content=self._t('settings.vyla_update_done_content',
                            'Now running version {version}.', version=version),
            duration=3500, parent=self.window())

    def _refresh_external_status(self) -> None:
        from src.core.tv import external as external_backend
        status = external_backend.integration_status()
        if status.get('provisioning'):
            text = self._t('settings.external_status_preparing',
                           'Preparing Node.js and provider dependencies...')
        elif status.get('ready'):
            text = self._t('settings.external_status_ready',
                           'Node.js ready · Vyla SDK loaded')
        elif status.get('error'):
            text = self._t('settings.external_status_error',
                           'Setup failed: {error}', error=status['error'])
        elif status.get('node'):
            text = self._t('settings.external_status_deps',
                           'Node.js found. Installing provider dependencies...')
        else:
            text = self._t('settings.external_status_missing',
                           'Node.js runtime not installed. Set up downloads it automatically.')
        self._external_status_lbl.setText(text)
        timer = getattr(self, '_external_timer', None)
        if timer is not None and not status.get('provisioning'):
            timer.stop()
            if status.get('ready') and getattr(self, '_external_setup_requested', False):
                self._external_setup_requested = False
                InfoBar.success(
                    title=self._t('settings.external_setup_done', 'Providers ready'),
                    content=self._t('settings.external_setup_done_content',
                                   'Vyla is ready to use.'),
                    duration=3000, parent=self.window())

    def _build_debrid(self):
        group = self._debrid_group = self._group('settings.group_debrid', 'Debrid (WIP, needs testing)')
        self._add_group(group)
        yield

        self._debrid_connection = _SectionCard(
            themed_icon('settings.debrid_connection', FluentIcon.CLOUD),
            self._t('settings.debrid_connection_title', 'Connection'),
            self._t('settings.debrid_connection_content', 'Provider, token and request timeout'),
        )
        self._track_icon(self._debrid_connection, 'settings.debrid_connection', FluentIcon.CLOUD)
        self._bind(self._debrid_connection.setTitle, 'settings.debrid_connection_title', 'Connection')
        self._track_switch_text(self._debrid_connection.switch)
        self._debrid_connection.switch.checkedChanged.connect(self._on_debrid_enabled_changed)
        self._sections.append((self._debrid_connection, 'debrid_enabled'))
        self._add_card(group, self._debrid_connection)
        yield

        self._debrid_provider_combo = ComboBox()
        self._debrid_provider_combo.setMinimumWidth(160)
        for key in PROVIDER_ORDER:
            self._debrid_provider_combo.addItem(PROVIDER_INFO[key]['label'], userData=key)
        self._debrid_provider_combo.currentIndexChanged.connect(self._on_debrid_provider_changed)
        self._debrid_provider_card = self._add_group_row(
            self._debrid_connection, 'settings.debrid_provider', FluentIcon.CLOUD,
            'settings.debrid_provider_title', None, 'settings.debrid_provider_content', None,
            self._debrid_provider_combo)
        self._combos.append((self._debrid_provider_combo, 'debrid_provider'))
        yield

        self._debrid_token_edit = LineEdit()
        self._debrid_token_edit.setMinimumWidth(190)
        self._debrid_token_edit.setClearButtonEnabled(True)
        self._debrid_token_edit.setEchoMode(LineEdit.EchoMode.Password)
        self._debrid_token_edit.returnPressed.connect(self._on_debrid_save_token)
        self._debrid_save_btn = PushButton(self._t('settings.debrid_token_save', 'Save'))
        self._bind(self._debrid_save_btn.setText, 'settings.debrid_token_save', 'Save')
        self._debrid_save_btn.clicked.connect(self._on_debrid_save_token)
        self._debrid_test_btn = PushButton(self._t('settings.debrid_token_test', 'Test'))
        self._bind(self._debrid_test_btn.setText, 'settings.debrid_token_test', 'Test')
        self._debrid_test_btn.clicked.connect(self._on_debrid_test_token)
        self._debrid_token_card = self._add_group_row(
            self._debrid_connection, 'settings.debrid_token', FluentIcon.FINGERPRINT,
            'settings.debrid_token_title', None, 'settings.debrid_token_content', None,
            _path_row(self._debrid_token_edit, self._debrid_save_btn, self._debrid_test_btn))
        yield

        self._debrid_status_lbl = CaptionLabel('')
        self._debrid_help_btn = PushButton(self._t('settings.debrid_token_help', 'Get Token'))
        self._bind(self._debrid_help_btn.setText, 'settings.debrid_token_help', 'Get Token')
        self._bind(self._debrid_help_btn.setToolTip, 'settings.debrid_token_help_hint',
                   'Sign in to the provider site first. The token page returns 403 when you are signed out.')
        self._debrid_help_btn.clicked.connect(self._on_debrid_open_help)
        self._debrid_clear_btn = PushButton(self._t('settings.debrid_token_clear', 'Clear'))
        self._bind(self._debrid_clear_btn.setText, 'settings.debrid_token_clear', 'Clear')
        self._debrid_clear_btn.clicked.connect(self._on_debrid_clear_token)
        self._debrid_status_card = self._add_group_row(
            self._debrid_connection, 'settings.debrid_status', FluentIcon.INFO,
            'settings.debrid_status_title', None, 'settings.debrid_status_content', None,
            _path_row(self._debrid_status_lbl, self._debrid_help_btn, self._debrid_clear_btn))
        yield

        self._debrid_timeout_spin = CompactSpinBox()
        self._debrid_timeout_spin.setRange(60, 7200)
        self._bind(self._debrid_timeout_spin.setSuffix, 'settings.suffix_seconds', 's')
        self._debrid_timeout_spin.valueChanged.connect(
            lambda value: self._save_spin('debrid_timeout', value))
        self._debrid_timeout_card = self._add_group_row(
            self._debrid_connection, 'settings.debrid_timeout', FluentIcon.HISTORY,
            'settings.debrid_timeout_title', None, 'settings.debrid_timeout_content', None,
            self._debrid_timeout_spin)
        self._spins.append((self._debrid_timeout_spin, 'debrid_timeout'))
        yield

        self._debrid_automation = _ExpandCard(
            themed_icon('settings.debrid_automation', FluentIcon.SYNC),
            self._t('settings.debrid_automation_title', 'Automation'),
            self._t('settings.debrid_automation_content',
                    'Choose where debrid is used and how transfers finish'),
        )
        self._track_icon(self._debrid_automation, 'settings.debrid_automation', FluentIcon.SYNC)
        self._bind(self._debrid_automation.setTitle, 'settings.debrid_automation_title', 'Automation')
        self._bind(self._debrid_automation.setContent, 'settings.debrid_automation_content',
                   'Choose where debrid is used and how transfers finish')
        for slot, title, content, attr in (
            ('settings.debrid_auto_repacks', 'settings.debrid_auto_repacks_title', 'settings.debrid_auto_repacks_content', 'debrid_auto_repacks'),
            ('settings.debrid_auto_tv', 'settings.debrid_auto_tv_title', 'settings.debrid_auto_tv_content', 'debrid_auto_tv'),
            ('settings.debrid_auto_tbhb', 'settings.debrid_auto_tbhb_title', 'settings.debrid_auto_tbhb_content', 'debrid_auto_tbhb'),
            ('settings.debrid_auto_direct', 'settings.debrid_auto_direct_title', 'settings.debrid_auto_direct_content', 'debrid_auto_direct'),
            ('settings.debrid_fallback', 'settings.debrid_fallback_title', 'settings.debrid_fallback_content', 'debrid_fallback'),
            ('settings.debrid_stream', 'settings.debrid_stream_title', 'settings.debrid_stream_content', 'debrid_stream'),
            ('settings.debrid_delete_after', 'settings.debrid_delete_after_title', 'settings.debrid_delete_after_content', 'debrid_delete_after'),
        ):
            self._add_section_switch(self._debrid_automation, slot, FluentIcon.CLOUD,
                                     title, None, content, None, attr)
            yield
        self._add_card(group, self._debrid_automation)
        self._debrid_help_url = ''
        self._refresh_debrid_token_state()

    def _current_debrid_provider(self) -> str:
        combo = getattr(self, '_debrid_provider_combo', None)
        value = combo.currentData() if combo is not None else ''
        return str(value or settings.debrid_provider or PROVIDER_ORDER[0])

    def _set_debrid_status(self, text: str) -> None:
        label = getattr(self, '_debrid_status_lbl', None)
        if label is not None:
            with suppress(RuntimeError):
                label.setText(text)

    def _refresh_debrid_token_state(self) -> None:
        if getattr(self, '_debrid_status_lbl', None) is None:
            return
        key = self._current_debrid_provider()
        label = PROVIDER_INFO.get(key, {}).get('label', key)
        self._debrid_help_url = PROVIDER_INFO.get(key, {}).get('help_url', '')
        saved = False
        updated = 0
        try:
            saved = token_store.has(key)
            updated = token_store.last_updated(key)
        except Exception:
            logger.warning('Could not read the debrid token state', exc_info=True)
        if saved:
            date = time.strftime('%Y-%m-%d', time.localtime(updated)) if updated else ''
            state = self._t('settings.debrid_token_saved', 'Saved {date}', date=date).strip()
            placeholder = self._t('settings.debrid_token_replace',
                                  'A token is saved. Paste a new one to replace it.')
        else:
            state = self._t('settings.debrid_token_missing', 'No token saved')
            placeholder = self._t('settings.debrid_token_placeholder', 'Paste your API token')
        self._set_debrid_status(state)
        connection = getattr(self, '_debrid_connection', None)
        if connection is not None:
            with suppress(RuntimeError):
                connection.setContent(f'{label} · {state}')
        self._debrid_token_edit.setPlaceholderText(placeholder)
        self._debrid_clear_btn.setEnabled(saved)
        self._debrid_help_btn.setEnabled(bool(self._debrid_help_url))

    def _on_debrid_enabled_changed(self, checked: bool) -> None:
        self._save_switch('debrid_enabled', checked)
        self._refresh_debrid_token_state()
        if not checked:
            return
        if token_store.has(self._current_debrid_provider()):
            return
        InfoBar.warning(
            title=self._t('settings.debrid_token_missing_title', 'Debrid needs a token'),
            content=self._t('settings.debrid_token_missing_content',
                            'Choose a provider and save its API token to start using debrid.'),
            orient=Qt.Orientation.Horizontal, isClosable=True,
            position=InfoBarPosition.TOP_RIGHT, duration=5000, parent=self.window())

    def _on_debrid_provider_changed(self, _index: int) -> None:
        if self._loading:
            return
        self._apply_setting('debrid_provider', self._current_debrid_provider())
        self._debrid_token_edit.clear()
        self._refresh_debrid_token_state()

    def _on_debrid_save_token(self) -> None:
        key = self._current_debrid_provider()
        token = self._debrid_token_edit.text().strip()
        if not token:
            InfoBar.warning(
                title=self._t('settings.debrid_empty_title', 'Token required'),
                content=self._t('settings.debrid_empty_content', 'Paste a token before saving.'),
                orient=Qt.Orientation.Horizontal, isClosable=True,
                position=InfoBarPosition.TOP_RIGHT, duration=3500, parent=self.window())
            return
        try:
            token_store.set(key, token)
        except Exception as exc:
            logger.warning('Could not save the debrid token', exc_info=True)
            InfoBar.error(
                title=self._t('settings.debrid_save_failed_title', 'Could not save token'),
                content=str(exc), orient=Qt.Orientation.Horizontal, isClosable=True,
                position=InfoBarPosition.TOP_RIGHT, duration=5000, parent=self.window())
            return
        self._debrid_token_edit.clear()
        self._refresh_debrid_token_state()
        label = PROVIDER_INFO.get(key, {}).get('label', key)
        InfoBar.success(
            title=self._t('settings.debrid_saved_title', 'Token saved'),
            content=self._t('settings.debrid_saved_content', 'The {provider} token is stored securely.',
                            provider=label),
            orient=Qt.Orientation.Horizontal, isClosable=True,
            position=InfoBarPosition.TOP_RIGHT, duration=3500, parent=self.window())
        self.settings_changed.emit()

    def _on_debrid_clear_token(self) -> None:
        key = self._current_debrid_provider()
        label = PROVIDER_INFO.get(key, {}).get('label', key)
        try:
            token_store.delete(key)
        except Exception as exc:
            logger.warning('Could not clear the debrid token', exc_info=True)
            InfoBar.error(
                title=self._t('settings.debrid_clear_failed_title', 'Could not remove token'),
                content=str(exc), orient=Qt.Orientation.Horizontal, isClosable=True,
                position=InfoBarPosition.TOP_RIGHT, duration=5000, parent=self.window())
            return
        self._refresh_debrid_token_state()
        InfoBar.success(
            title=self._t('settings.debrid_cleared_title', 'Token removed'),
            content=self._t('settings.debrid_cleared_content', 'The {provider} token was deleted.',
                            provider=label),
            orient=Qt.Orientation.Horizontal, isClosable=True,
            position=InfoBarPosition.TOP_RIGHT, duration=3000, parent=self.window())
        self.settings_changed.emit()

    def _on_debrid_open_help(self) -> None:
        if self._debrid_help_url:
            QDesktopServices.openUrl(QUrl(self._debrid_help_url))

    def _on_debrid_test_token(self) -> None:
        key = self._current_debrid_provider()
        label = PROVIDER_INFO.get(key, {}).get('label', key)
        typed = self._debrid_token_edit.text().strip()
        try:
            token = typed or token_store.get(key)
        except Exception:
            token = typed
        if not token:
            InfoBar.warning(
                title=self._t('settings.debrid_empty_title', 'Token required'),
                content=self._t('settings.debrid_empty_content', 'Paste a token before saving.'),
                orient=Qt.Orientation.Horizontal, isClosable=True,
                position=InfoBarPosition.TOP_RIGHT, duration=3500, parent=self.window())
            return
        provider_cls = PROVIDERS[key]
        self._debrid_test_btn.setEnabled(False)
        self._set_debrid_status(self._t('settings.debrid_testing', 'Testing {provider}...',
                                        provider=label))
        from src.core import worker as _worker

        def _job():
            client = provider_cls(token, timeout=30.0)
            try:
                return client.test()
            finally:
                client.close()

        def _done(info) -> None:
            if not self._debrid_test_btn.isEnabled():
                self._debrid_test_btn.setEnabled(True)
            info = info if isinstance(info, dict) else {}
            account = str(info.get('account') or '')
            premium = self._t('common.on', 'On') if info.get('premium') else self._t('common.off', 'Off')
            expires = str(info.get('expires') or '-')
            detail = self._t(
                'settings.debrid_test_ok_content',
                'Account: {account}. Premium: {premium}. Expires: {expires}.',
                account=account or '-', premium=premium, expires=expires)
            self._set_debrid_status(f'{label}: {self._t("settings.debrid_test_ok_status", "Verified")}')
            InfoBar.success(
                title=self._t('settings.debrid_test_ok_title', 'Token works'),
                content=detail, orient=Qt.Orientation.Horizontal, isClosable=True,
                position=InfoBarPosition.TOP_RIGHT, duration=6000, parent=self.window())

        def _error(message: str) -> None:
            if not self._debrid_test_btn.isEnabled():
                self._debrid_test_btn.setEnabled(True)
            self._set_debrid_status(f'{label}: {message}')
            InfoBar.error(
                title=self._t('settings.debrid_test_fail_title', 'Token test failed'),
                content=str(message), orient=Qt.Orientation.Horizontal, isClosable=True,
                position=InfoBarPosition.TOP_RIGHT, duration=7000, parent=self.window())

        _worker.submit(_job, on_done=_done, on_error=_error, owner=self,
                       key=('debrid-test', key), priority=_worker.Priority.NORMAL)


    def _build_advanced(self):
        group = self._adv_group = self._group('settings.group_advanced', 'Advanced')
        self._add_group(group)
        yield

        self._chk_admin_mode = self._switch_card(
            group, 'settings.admin_mode', FluentIcon.DEVELOPER_TOOLS,
            'settings.admin_mode_title', None, 'settings.admin_mode_content', None,
            attr='admin_mode', on_change=self._on_admin_mode_toggled)
        self._chk_show_console = self._switch_card(
            group, 'settings.show_console', FluentIcon.CODE,
            'settings.show_console_title', None, 'settings.show_console_content', None,
            attr='show_console', on_change=self._on_show_console_toggled)
        self._chk_log_to_file = self._switch_card(
            group, 'settings.log_to_file', FluentIcon.SAVE,
            'settings.log_to_file_title', None, 'settings.log_to_file_content', None,
            attr='log_to_file', on_change=self._on_log_to_file_toggled)
        self._btn_open_logs = PushButton(self._t('settings.open_logs_button', 'Open Logs Folder'))
        self._bind(self._btn_open_logs.setText, 'settings.open_logs_button', 'Open Logs Folder')
        self._btn_open_logs.clicked.connect(self._on_open_logs_folder)
        self._tail(self._chk_log_to_file, self._btn_open_logs)
        yield

        self._btn_export_settings = PushButton(self._t('settings.backup_export', 'Export…'))
        self._bind(self._btn_export_settings.setText, 'settings.backup_export', 'Export…')
        self._btn_export_settings.clicked.connect(self._on_export_settings)
        self._btn_import_settings = PushButton(self._t('settings.backup_import', 'Import…'))
        self._bind(self._btn_import_settings.setText, 'settings.backup_import', 'Import…')
        self._btn_import_settings.clicked.connect(self._on_import_settings)
        self._backup_card = self._plain_card(
            group, 'settings.backup', FluentIcon.SAVE,
            'settings.backup_title', 'Backup & Restore Settings',
            'settings.backup_content',
            'Save every preference to a JSON file, or restore them from a backup. Backups include proxy credentials, so keep them private.',
            self._btn_export_settings, self._btn_import_settings)
        yield

        self._chk_auto_update = self._switch_card(
            group, 'settings.auto_update_check', FluentIcon.SYNC,
            'settings.auto_update_check_title', 'Check for updates automatically',
            'settings.auto_update_check_content', 'Look for newer releases in the background when PiraChest starts. On by default.',
            attr='auto_check_updates')
        self._chk_canary_update = self._switch_card(
            group, 'settings.canary_updates', FluentIcon.UPDATE,
            'settings.canary_updates_title', 'PiraChest Canary',
            'settings.canary_updates_content', 'Experimental cutting-edge builds that update faster and may be unstable. Canary builds also receive stable update notices.',
            attr='canary_updates')
        yield

        self._app_version = VERSION
        self._update_status_lbl = CaptionLabel('')
        self._btn_check_update = PushButton(self._t('settings.check_for_updates'))
        self._bind(self._btn_check_update.setText, 'settings.check_for_updates')
        self._btn_check_update.clicked.connect(self._on_check_update)
        self._update_card = self._plain_card(
            group, 'settings.software_updates', FluentIcon.SYNC,
            'settings.software_updates_title', None,
            'settings.current_version', None,
            self._update_status_lbl, self._btn_check_update,
            content_kwargs={'version': self._app_version})
        yield

        self._cache_status_lbl = CaptionLabel('')
        self._btn_clear_cache = PushButton(self._t('settings.clear_cache_button'))
        self._bind(self._btn_clear_cache.setText, 'settings.clear_cache_button')
        self._btn_clear_cache.clicked.connect(self._on_clear_cache)
        self._cache_ready_slot = lambda size: self._cache_status_lbl.setText(
            format_size(size) if size >= 0 else '')
        self._cache_status_lbl.setMinimumWidth(64)
        self._cache_status_lbl.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self._cache_size_ready.connect(self._cache_ready_slot)
        self._cache_clear_finished.connect(self._on_cache_cleared)
        self._cache_card = self._plain_card(
            group, 'settings.clear_cache', FluentIcon.BROOM,
            'settings.clear_cache_title', None, 'settings.clear_cache_content', None,
            self._cache_status_lbl, self._btn_clear_cache)
        yield

        self._btn_wipe_all = PushButton(self._t('settings.wipe_all_button'))
        self._bind(self._btn_wipe_all.setText, 'settings.wipe_all_button')
        self._btn_wipe_all.clicked.connect(self._on_wipe_all)
        self._wipe_all_card = self._plain_card(
            group, 'settings.wipe_all', FluentIcon.DELETE,
            'settings.wipe_all_title', None, 'settings.wipe_all_content', None,
            self._btn_wipe_all)
        yield


    def _build_experimental(self):
        group = self._experimental_group = self._group('settings.group_experimental', 'Experimental')
        self._add_group(group)
        yield

        self._experimental_warning_card = self._plain_card(
            group, 'settings.experimental_warning', FluentIcon.INFO,
            'settings.experimental_warning_title', 'Experimental features',
            'settings.experimental_warning_content',
            'These features are unfinished and may change, misbehave, or break things. Use them at your own risk.')
        yield

        self._chk_auto_install = self._switch_card(
            group, 'nav.pc_games', FluentIcon.GAME,
            'settings.auto_install_games_title', None,
            'settings.auto_install_games_content', None,
            attr='auto_install_games', on_change=self._on_auto_install_toggled)
        yield

        self._btn_theme_maker = PushButton(self._t('settings.theme_maker_button', 'Open'))
        self._bind(self._btn_theme_maker.setText, 'settings.theme_maker_button', 'Open')
        self._btn_theme_maker.clicked.connect(self._on_open_theme_maker)
        self._theme_maker_card = self._plain_card(
            group, 'settings.theme_maker', FluentIcon.BRUSH,
            'settings.theme_maker_title', 'Theme Maker (experimental)',
            'settings.theme_maker_content',
            'Click any element to restyle it, then save or share the result as a theme (Ctrl+Shift+T)',
            self._btn_theme_maker)
        yield


    def _apply_locale(self, *_args) -> None:
        self.setUpdatesEnabled(False)
        try:
            for setter, key, fallback, kwargs in self._texts:
                with suppress(RuntimeError, TypeError, KeyError):
                    setter(self._t(key, fallback, **kwargs))
            for switch in self._switch_labels:
                with suppress(RuntimeError):
                    self._set_switch_text(switch, switch.isChecked())
            if 'appearance' in self._ranges and self._theme_provides_background():
                self._bg_card.setContent(
                    self._t('settings.background_locked_by_theme', 'This theme includes its own background.'))
        finally:
            self.setUpdatesEnabled(True)
        if os.name == 'nt' and hasattr(self, '_assoc_status_lbl'):
            self._refresh_associations_status()
        self._refresh_debrid_token_state()


    def _current_theme_id(self) -> str:
        preset = settings.theme_preset
        if preset and preset_meta(preset) is not None:
            return preset
        return 'light' if settings.theme_mode == ThemeMode.LIGHT else 'dark'

    def _on_theme_choose_clicked(self) -> None:
        if getattr(self, '_theme_picker_open', False):
            return
        self._theme_picker_open = True
        QTimer.singleShot(0, self, self._open_theme_picker)

    def _open_theme_picker(self) -> None:
        from src.gui.main_window import pick_theme

        if self._shell:
            for k in list(self._jobs):
                self._advance_job(k)
            self._evict_pages(self._selected_category, sync=True)
        try:
            pick_theme(
                self.window(), self._current_theme_id(),
                on_accept=lambda option: self._on_theme_selected(option, activate=False),
            )
        finally:
            self._theme_picker_open = False

    def _on_theme_selected(self, option: str, activate: bool = True) -> None:
        if option in ('dark', 'light'):
            mode = ThemeMode.DARK if option == 'dark' else ThemeMode.LIGHT
            settings.theme_mode = mode
            settings.theme_preset = None
            apply_settings(theme_mode=mode, theme_preset=None)
            save_settings(settings)
            if activate:
                activate_theme(None, dark=(option == 'dark'))
            apply_theme_font_families(None)
        else:
            meta = preset_meta(option)
            if meta is None:
                return
            if activate:
                activate_theme(option)
            apply_theme_font_families(resolve_font_families(meta))
            settings.theme_preset = option
            apply_settings(theme_preset=option)
            save_settings(settings)
        self._sync_theme_lock_state()

    def _on_theme_reload_clicked(self) -> None:
        reload_custom_themes()

    def _on_language_changed(self, index: int) -> None:
        if 0 <= index < len(self._lang_codes):
            set_language(self._lang_codes[index])
            app = QApplication.instance()
            if app is not None:
                app.setLayoutDirection(Qt.LayoutDirection.LeftToRight)
            self.settings_changed.emit()


    def _on_accent_color_changed(self, color: QColor) -> None:
        self._evict_pages(self._selected_category, sync=True)
        for pill in getattr(self, '_category_buttons', {}).values():
            pill.update()
        self._pending_accent = QColor(color)
        if not accent_locked(active_preset_id()):
            shader = getattr(self.window(), '_shader_bg', None)
            if shader is not None:
                shader.set_accent_color(color)
        timer = getattr(self, '_accent_timer', None)
        if timer is None:
            timer = self._accent_timer = QTimer(self)
            timer.setSingleShot(True)
            timer.setInterval(60)
            timer.timeout.connect(self._apply_accent_now)
        timer.start()

    def _apply_accent_now(self) -> None:
        color = getattr(self, '_pending_accent', None)
        if color is None:
            return
        hex_color = color.name(QColor.NameFormat.HexRgb)
        if hex_color == settings.accent_color and getattr(self, '_accent_applied', None) == hex_color:
            return
        self._accent_applied = hex_color
        settings.accent_color = hex_color
        apply_settings(accent_color=hex_color)
        save_settings(settings)
        if not accent_locked(active_preset_id()):
            notify_accent_changed()
        else:
            self._sync_accent_button()

    def _on_default_accent_color(self) -> None:
        color = QColor(APP_ACCENT_COLOR)
        self._accent_color_btn.setColor(color)
        self._on_accent_color_changed(color)

    def _on_reset_accent_color(self) -> None:
        color = QColor(detect_windows_accent_color())
        self._accent_color_btn.setColor(color)
        self._on_accent_color_changed(color)

    def _on_clear_search_history(self) -> None:
        from src.gui.stuff.search_history import clear
        clear()


    def _background_photo_active(self) -> bool:
        return bool(settings.background_image_enabled) and bool(settings.background_image_path or '')

    @staticmethod
    def _theme_provides_background() -> bool:
        info = theme_background_info(settings.theme_preset)
        return bool(info.get('path') or info.get('shader'))

    def _set_background_options_enabled(self, enabled: bool) -> None:
        self._bg_photo_group_widget.setEnabled(enabled)
        self._bg_blur_group_widget.setEnabled(enabled)
        self._bg_darken_group_widget.setEnabled(enabled)

    def _force_dark_theme_for_background(self) -> None:
        window = self.window()
        if not (settings.theme_preset is None and settings.theme_mode == ThemeMode.DARK):
            settings.theme_mode = ThemeMode.DARK
            settings.theme_preset = None
            apply_settings(theme_mode=ThemeMode.DARK, theme_preset=None)
            save_settings(settings)
            activate_theme(None, dark=True)
            apply_theme_font_families(None)
            if hasattr(window, '_apply_theme_background'):
                window._apply_theme_background(None)
            self.settings_changed.emit()

    def _sync_theme_lock_state(self) -> None:
        if 'appearance' not in self._ranges:
            return
        theme_owns = self._theme_provides_background()
        self._bg_card.setEnabled(not theme_owns)
        self._set_background_options_enabled(self._bg_card.isChecked() and not theme_owns)
        active = self._background_photo_active() and not theme_owns
        self._theme_btn.setEnabled(not active)
        if active:
            self._force_dark_theme_for_background()
        preset = active_preset_id()
        self._accent_card.setEnabled(preset is None or not accent_locked(preset))
        if theme_owns:
            self._bg_card.setContent(
                self._t('settings.background_locked_by_theme', 'This theme includes its own background.'))

    def _on_background_toggled(self, checked: bool) -> None:
        if self._loading:
            return
        self._apply_setting('background_image_enabled', bool(checked))
        self._set_background_options_enabled(checked and not self._theme_provides_background())
        self._sync_theme_lock_state()
        window = self.window()
        if hasattr(window, '_refresh_background_photo'):
            window._refresh_background_photo()

    def _on_browse_background_image(self) -> None:
        path, _filter = QFileDialog.getOpenFileName(
            self, self._t('settings.background_photo_title', 'Background file'), '',
            'Images and videos (*.png *.jpg *.jpeg *.bmp *.webp *.gif *.mp4 *.webm *.mov *.mkv *.avi)')
        if not path:
            return
        self._bg_path_edit.setText(path)
        self._apply_setting('background_image_path', path)
        self._sync_theme_lock_state()
        window = self.window()
        if hasattr(window, '_refresh_background_photo'):
            window._refresh_background_photo()

    def _on_clear_background_image(self) -> None:
        self._bg_path_edit.setText('')
        self._apply_setting('background_image_path', '')
        self._sync_theme_lock_state()
        window = self.window()
        if hasattr(window, '_refresh_background_photo'):
            window._refresh_background_photo()

    def _on_background_blur_live(self, value: int) -> None:
        self._bg_blur_value_lbl.setText(str(value))

    def _on_background_blur_commit(self, value: int) -> None:
        self._apply_setting('background_image_blur', int(value))
        window = self.window()
        if hasattr(window, '_schedule_background_refresh'):
            window._schedule_background_refresh()

    def _on_background_darken_live(self, value: int) -> None:
        self._bg_darken_value_lbl.setText(f'{value}%')

    def _on_background_darken_commit(self, value: int) -> None:
        self._apply_setting('background_image_darken', int(value))
        window = self.window()
        if hasattr(window, '_schedule_background_refresh'):
            window._schedule_background_refresh()


    def _on_sfx_volume_live(self, value: int) -> None:
        self._sfx_value_lbl.setText(str(value) if value > 0 else self._t('settings.volume_off', 'Off'))
        get_sound_manager().set_sfx_volume(int(value))

    def _on_sfx_volume_commit(self, value: int) -> None:
        self._apply_setting('sfx_volume', int(value))

    def _on_ambiance_volume_live(self, value: int) -> None:
        self._ambiance_value_lbl.setText(str(value) if value > 0 else self._t('settings.volume_off', 'Off'))
        get_sound_manager().set_ambiance_volume(int(value))

    def _on_ambiance_volume_commit(self, value: int) -> None:
        self._apply_setting('ambiance_volume', int(value))


    def bind_network(self, source, apply) -> None:
        if not self._built:
            self._pending_bind = (source, apply)
            return
        self._net_apply = apply
        self._net_loading = True
        try:
            prefs = source()
            for key, widget in self._net_widgets.items():
                value = prefs.get(key)
                if key == 'proxy_type':
                    index = widget.findData(value)
                    widget.setCurrentIndex(max(0, index))
                elif key == 'proxy_port':
                    widget.setValue(int(value or 0))
                elif isinstance(widget, LineEdit):
                    widget.setText(str(value or ''))
                else:
                    widget.setChecked(bool(value))
        finally:
            self._net_loading = False

    def _net_push(self, **values) -> None:
        if self._net_loading or self._net_apply is None:
            return
        self._net_apply(**values)
        if 'anonymous' in values:
            self._offer_restart()

    def _offer_restart(self) -> None:
        box = MessageBoxBase(self.window())
        label = BodyLabel(self._t(
            'settings.privacy_restart_prompt',
            'Anonymous mode is fully applied after a restart. Restart now?'))
        label.setWordWrap(True)
        box.viewLayout.addWidget(label)
        box.yesButton.setText(self._t('settings.restart_now', 'Restart Now'))
        box.cancelButton.setText(self._t('settings.restart_later', 'Later'))
        if box.exec() and relaunch_app():
            window = self.window()
            if hasattr(window, '_force_quit'):
                window._force_quit = True
            window.close()


    def _build_shortcuts(self):
        group = self._shortcuts_group = self._group(
            'settings.group_shortcuts', 'Keyboard shortcuts')
        self._shortcuts_card = group
        self._add_group(group)
        yield

        for action_id, _default_key, label, _attr in SHORTCUT_DEFAULTS:
            button = PushButton('', group)
            button.setMinimumWidth(150)
            button.clicked.connect(
                lambda _checked=False, a=action_id: self._begin_shortcut_capture(a))
            button.installEventFilter(self)
            slot, fluent = SHORTCUT_ICONS.get(action_id, (None, FluentIcon.SETTING))
            key = f'settings.shortcut_{action_id}'
            card = SettingCard(themed_icon(slot, fluent), self._t(key, label), '', group)
            card.setFixedHeight(50)
            self._track_icon(card, slot, fluent)
            self._bind(card.setTitle, key, label)
            self._tail(card, button)
            self._add_card(group, card)
            self._shortcut_rows[action_id] = (card, button)
            yield

        self._btn_shortcuts_reset = PushButton(
            self._t('settings.shortcuts_reset', 'Reset to defaults'), group)
        self._bind(self._btn_shortcuts_reset.setText, 'settings.shortcuts_reset', 'Reset to defaults')
        self._btn_shortcuts_reset.clicked.connect(self._on_shortcuts_reset)
        self._plain_card(
            group, 'settings.keyboard_shortcut', FluentIcon.SYNC,
            'settings.shortcuts_reset_title', 'Reset shortcuts',
            'settings.shortcuts_reset_content',
            'Restore all keyboard shortcuts to their defaults',
            self._btn_shortcuts_reset)
        self._refresh_shortcut_buttons()
        yield

    @staticmethod
    def _shortcut_display(key: str) -> str:
        return ' + '.join(str(key).split('+'))

    def _refresh_shortcut_buttons(self) -> None:
        try:
            mapping = shortcut_map()
        except Exception:
            return
        for action_id, (_group, button) in self._shortcut_rows.items():
            if action_id == self._capturing_shortcut:
                continue
            with suppress(RuntimeError):
                button.setText(self._shortcut_display(mapping.get(action_id, '')))

    def _window_shortcuts(self):
        with suppress(Exception):
            window = self.window()
            if hasattr(window, '_set_page_shortcuts_enabled'):
                return window
        return None

    def _begin_shortcut_capture(self, action_id: str) -> None:
        if self._capturing_shortcut not in (None, action_id):
            self._cancel_shortcut_capture()
        self._capturing_shortcut = action_id
        button = self._shortcut_rows.get(action_id, (None, None))[1]
        if button is None:
            self._capturing_shortcut = None
            return
        window = self._window_shortcuts()
        if window is not None:
            with suppress(Exception):
                window._set_page_shortcuts_enabled(False)
        button.setText('…')
        button.setToolTip(self._t('settings.shortcut_capture_tip', 'Press the new keys (Esc cancels)'))
        with suppress(Exception):
            button.setFocus()
            button.grabKeyboard()

    def _cancel_shortcut_capture(self) -> None:
        action_id = self._capturing_shortcut
        self._capturing_shortcut = None
        if action_id is None:
            return
        button = self._shortcut_rows.get(action_id, (None, None))[1]
        if button is not None:
            with suppress(Exception):
                button.releaseKeyboard()
            with suppress(Exception):
                button.setToolTip('')
        window = self._window_shortcuts()
        if window is not None:
            with suppress(Exception):
                window._set_page_shortcuts_enabled(True)
        self._refresh_shortcut_buttons()

    def _capture_preview_text(self, event) -> str:
        try:
            modifiers = event.modifiers()
        except Exception:
            return '…'
        parts = []
        for flag, name in ((Qt.KeyboardModifier.ControlModifier, 'Ctrl'),
                           (Qt.KeyboardModifier.AltModifier, 'Alt'),
                           (Qt.KeyboardModifier.ShiftModifier, 'Shift'),
                           (Qt.KeyboardModifier.MetaModifier, 'Meta')):
            if modifiers & flag:
                parts.append(name)
        return ' + '.join(parts) + ' + …' if parts else '…'

    def _update_capture_preview(self, event) -> None:
        button = self._shortcut_rows.get(self._capturing_shortcut, (None, None))[1]
        if button is not None:
            with suppress(Exception):
                button.setText(self._capture_preview_text(event))

    def _finish_shortcut_capture(self, combo: str) -> None:
        action_id = self._capturing_shortcut
        if action_id is None:
            return
        normalized = combo.replace(' ', '').lower()
        if normalized in {'alt+left', 'alt+right', 'f5', 'f1', 'ctrl+shift+t'}:
            InfoBar.warning(
                title=self._t('settings.shortcut_conflict_title', 'Shortcut already in use'),
                content=self._t('settings.shortcut_conflict_content_generic',
                                'That combination is reserved by the app'),
                orient=Qt.Orientation.Horizontal, isClosable=True,
                position=InfoBarPosition.TOP_RIGHT, duration=2500, parent=self.window())
            return
        try:
            mapping = shortcut_map()
        except Exception:
            mapping = {}
        other_id = next((action for action, key in mapping.items()
                         if action != action_id and key.replace(' ', '').lower() == normalized), None)
        choice = None
        if other_id is not None:
            self._cancel_shortcut_capture()
            choice = self._ask_shortcut_conflict(combo, action_id, other_id)
            if choice is None:
                self._begin_shortcut_capture(action_id)
                return
        try:
            current = dict(settings.keyboard_shortcuts or {})
            current[action_id] = combo
            if other_id is not None and choice == 'swap':
                current[other_id] = mapping.get(action_id) or mapping.get(other_id)
            settings.keyboard_shortcuts = current
            save_settings(settings)
        except Exception:
            logger.exception('Failed to save shortcut')
            return
        self._cancel_shortcut_capture()
        window = self.window()
        if hasattr(window, '_register_shortcuts'):
            with suppress(Exception):
                window._register_shortcuts()
        if choice == 'reassign' and other_id is not None:
            self._begin_shortcut_capture(other_id)

    def _ask_shortcut_conflict(self, combo: str, action_id: str, other_id: str) -> str | None:
        labels = {action: label for action, _key, label, _attr in SHORTCUT_DEFAULTS}
        current_label = self._t(f'settings.shortcut_{action_id}', labels.get(action_id, action_id))
        other_label = self._t(f'settings.shortcut_{other_id}', labels.get(other_id, other_id))
        from PySide6.QtWidgets import QDialog
        box = MessageBoxBase(self.window())
        box.viewLayout.addWidget(StrongBodyLabel(
            self._t('settings.shortcut_conflict_title', 'Shortcut already in use')))
        box.viewLayout.addWidget(BodyLabel(
            self._t('settings.shortcut_conflict_content',
                    "'{combo}' is already assigned to {other}. Are you sure you want to use it for {current}?",
                    combo=combo, other=other_label, current=current_label)))
        box.viewLayout.addWidget(CaptionLabel(
            self._t('settings.shortcut_conflict_hint',
                    'Swap exchanges both shortcuts. Reassign moves {other} onto a new one - you will press it next.',
                    other=other_label)))
        box.cancelButton.setText(self._t('common.cancel', 'Cancel'))
        box.yesButton.setText(self._t('settings.shortcut_conflict_reassign', 'Reassign'))
        swap = PushButton(self._t('settings.shortcut_conflict_swap', 'Swap'), box.buttonGroup)
        box.buttonLayout.insertWidget(0, swap)
        picked = {'choice': None}

        def choose_swap() -> None:
            picked['choice'] = 'swap'
            box.accept()

        swap.clicked.connect(choose_swap)
        box.exec()
        if box.result() != QDialog.DialogCode.Accepted:
            return None
        return picked['choice'] or 'reassign'

    def _on_shortcuts_reset(self) -> None:
        try:
            settings.keyboard_shortcuts = {}
            save_settings(settings)
        except Exception:
            logger.exception('Failed to reset shortcuts')
            return
        self._cancel_shortcut_capture()
        self._refresh_shortcut_buttons()
        window = self.window()
        if hasattr(window, '_register_shortcuts'):
            with suppress(Exception):
                window._register_shortcuts()

    @staticmethod
    def _combo_from_key_event(event) -> str | None:
        try:
            modifiers = event.modifiers()
        except Exception:
            return None
        key = event.key()
        if key in (Qt.Key.Key_Control, Qt.Key.Key_Shift, Qt.Key.Key_Alt, Qt.Key.Key_Meta):
            return None
        parts = []
        for flag, name in ((Qt.KeyboardModifier.ControlModifier, 'Ctrl'),
                           (Qt.KeyboardModifier.AltModifier, 'Alt'),
                           (Qt.KeyboardModifier.ShiftModifier, 'Shift'),
                           (Qt.KeyboardModifier.MetaModifier, 'Meta')):
            if modifiers & flag:
                parts.append(name)
        if not parts:
            return None
        if Qt.Key.Key_A <= key <= Qt.Key.Key_Z or Qt.Key.Key_0 <= key <= Qt.Key.Key_9:
            name = chr(int(key))
        elif Qt.Key.Key_F1 <= key <= Qt.Key.Key_F35:
            name = f'F{int(key - Qt.Key.Key_F1) + 1}'
        else:
            name = {
                Qt.Key.Key_Left: 'Left', Qt.Key.Key_Right: 'Right', Qt.Key.Key_Up: 'Up',
                Qt.Key.Key_Down: 'Down', Qt.Key.Key_Home: 'Home', Qt.Key.Key_End: 'End',
                Qt.Key.Key_PageUp: 'PageUp', Qt.Key.Key_PageDown: 'PageDown',
                Qt.Key.Key_Insert: 'Insert', Qt.Key.Key_Delete: 'Delete',
            }.get(key)
        if name is None:
            try:
                name = QKeySequence(key).toString() or None
            except Exception:
                name = None
        if name is None:
            return None
        parts.append(name)
        return '+'.join(parts)

    def eventFilter(self, obj, event) -> bool:
        if self._capturing_shortcut is not None and obj in [
                button for _group, button in self._shortcut_rows.values()]:
            if event.type() == QEvent.Type.KeyRelease:
                self._update_capture_preview(event)
                return True
            if event.type() == QEvent.Type.KeyPress:
                modifiers = event.modifiers()
                if event.key() == Qt.Key.Key_Escape and not (modifiers & (
                        Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.AltModifier
                        | Qt.KeyboardModifier.ShiftModifier | Qt.KeyboardModifier.MetaModifier)):
                    self._cancel_shortcut_capture()
                    return True
                combo = self._combo_from_key_event(event)
                if combo is None:
                    self._update_capture_preview(event)
                else:
                    self._finish_shortcut_capture(combo)
                return True
        return super().eventFilter(obj, event)


    def _pick_download_folder(self, attr: str, edit: LineEdit) -> None:
        path = QFileDialog.getExistingDirectory(
            self, self._t('settings.select_download_dir', 'Select Download Directory'),
            edit.text() or default_download_dir(attr))
        if not path:
            return
        path = os.path.normpath(path)
        problem = check_dir_writable(path)
        if problem:
            InfoBar.error(
                title=self._t('settings.folder_not_writable', "Can't use this folder"),
                content=problem, orient=Qt.Orientation.Horizontal, isClosable=True,
                position=InfoBarPosition.TOP_RIGHT, duration=4500, parent=self.window())
            return
        edit.setText(path)
        self._on_download_dir_changed(attr, path)

    def _on_download_dir_changed(self, attr: str, path: str) -> None:
        self._apply_setting(attr, os.path.normpath(path) if path else default_download_dir(attr))

    def _on_stream_addons_changed(self) -> None:
        value = self._edit_stream_addons.text().strip()
        if value != (getattr(settings, 'tv_stream_addons', '') or ''):
            self._apply_setting('tv_stream_addons', value)


    def _on_check_update(self) -> None:
        self._btn_check_update.setEnabled(False)
        self._update_status_lbl.setText(tr('update.checking'))
        update_controller.disconnect_listeners()
        update_controller.check_finished.connect(self._on_update_check_done)
        update_controller.check()

    def _on_update_check_done(self, result) -> None:
        self._btn_check_update.setEnabled(True)
        if result is None:
            self._update_status_lbl.setText(tr('update.up_to_date'))
            return
        self._update_status_lbl.setText(tr('update.available', tag=result['tag']))
        if show_update_dialog(self.window(), result, allow_skip=True) == 'update':
            self._start_update_download(result)

    def _start_update_download(self, result) -> None:
        self._update_status_lbl.setText(tr('update.downloading'))
        self._btn_check_update.setEnabled(False)
        update_controller.disconnect_listeners()
        update_controller.download_progress.connect(self._on_update_download_progress)
        update_controller.download_finished.connect(self._on_update_download_finished)
        update_controller.download_error.connect(self._on_update_download_error)
        window = self.window()
        if hasattr(window, 'show_update_overlay'):
            window.show_update_overlay(tr('update.downloading'))
        update_controller.download(result)

    def _on_update_download_progress(self, downloaded: int, total: int) -> None:
        if total > 0:
            self._update_status_lbl.setText(
                tr('update.downloading_pct', percent=int(downloaded / total * 100)))
        window = self.window()
        if hasattr(window, 'set_update_overlay_progress'):
            window.set_update_overlay_progress(downloaded, total)

    def _on_update_download_finished(self, path: str) -> None:
        self._update_status_lbl.setText(tr('update.restarting'))
        window = self.window()
        if hasattr(window, 'set_update_overlay_indeterminate'):
            window.set_update_overlay_indeterminate(tr('update.restarting'))
        try:
            update_controller.apply_and_restart(path, window)
        except Exception:
            logger.exception('Failed to apply update')
            if hasattr(window, 'hide_update_overlay'):
                window.hide_update_overlay()
            self._update_status_lbl.setText(tr('update.failed'))
            self._btn_check_update.setEnabled(True)

    def _on_update_download_error(self, _message: str) -> None:
        window = self.window()
        if hasattr(window, 'hide_update_overlay'):
            window.hide_update_overlay()
        self._update_status_lbl.setText(tr('update.download_failed'))
        self._btn_check_update.setEnabled(True)


    def _refresh_cache_size(self) -> None:
        if 'advanced' not in self._ranges:
            return
        now = time.monotonic()
        if now - self._last_cache_probe < 20.0:
            return
        self._last_cache_probe = now
        from src.core import worker as _worker
        from src.core.cache import app_cache_size
        _worker.submit(app_cache_size, on_done=self._cache_size_ready.emit,
                       on_error=lambda _message: self._cache_size_ready.emit(-1),
                       owner=self, key='cache-size', priority=_worker.Priority.NORMAL)

    def _on_clear_cache(self) -> None:
        from src.core import worker as _worker
        from src.core.cache import clear_app_cache
        box = MessageBoxBase(self.window())
        box.viewLayout.addWidget(StrongBodyLabel(tr('settings.clear_cache_confirm_title')))
        content = BodyLabel(tr('settings.clear_cache_confirm_content'))
        content.setWordWrap(True)
        box.viewLayout.addWidget(content)
        box.yesButton.setText(tr('settings.clear_cache_button'))
        box.cancelButton.setText(tr('download.cancel'))
        if not box.exec():
            return
        self._btn_clear_cache.setEnabled(False)

        def clear_all():
            clear_app_cache()
            from src.core.repacks.enrichment import clear_steam_cache
            clear_steam_cache()
            return 0

        _worker.submit(
            clear_all,
            on_done=lambda _result: self._cache_clear_finished.emit(''),
            on_error=lambda message: self._cache_clear_finished.emit(str(message) or 'unknown error'),
            owner=self, key='cache-clear', priority=_worker.Priority.NORMAL)

    def _on_cache_cleared(self, error: str) -> None:
        self._btn_clear_cache.setEnabled(True)
        self._last_cache_probe = 0.0
        self._refresh_cache_size()
        if not error:
            self._cache_status_lbl.setText(format_size(0))
        if error:
            logger.error('Failed to clear cache: %s', error)
            return
        InfoBar.success(
            title=tr('settings.clear_cache_done_title'), content='',
            orient=Qt.Orientation.Horizontal, isClosable=True,
            position=InfoBarPosition.TOP_RIGHT, duration=2500, parent=self.window())

    def _on_wipe_all(self) -> None:
        box = MessageBoxBase(self.window())
        layout = QVBoxLayout()
        warning = StrongBodyLabel(tr('settings.wipe_all_confirm_title'))
        warning.setStyleSheet(f'color: {palette()["state_error"]};')
        layout.addWidget(warning)
        content = BodyLabel(tr('settings.wipe_all_confirm_content'))
        content.setWordWrap(True)
        layout.addWidget(content)
        box.viewLayout.addLayout(layout)
        box.yesButton.setText(tr('settings.wipe_all_confirm_button'))
        box.cancelButton.setText(tr('download.cancel'))
        if not box.exec():
            return
        try:
            from src.core.cache import cache
            cache.close()
            wipe_all_app_data()
        except Exception:
            logger.exception('Failed to wipe app data')
        window = self.window()
        if hasattr(window, '_force_quit'):
            window._force_quit = True
        window.close()


    def _on_launch_at_startup_toggled(self, checked: bool) -> None:
        if set_launch_at_startup(checked):
            self._apply_setting('launch_at_startup', bool(checked))
        else:
            self._quiet(self._chk_launch_startup, self._chk_launch_startup.setChecked, not checked)
            InfoBar.error(
                title=tr('settings.launch_at_startup_failed_title'), content='',
                orient=Qt.Orientation.Horizontal, isClosable=True,
                position=InfoBarPosition.TOP_RIGHT, duration=3500, parent=self.window())

    def _on_admin_mode_toggled(self, checked: bool) -> None:
        settings.admin_mode = bool(checked)
        apply_settings(admin_mode=bool(checked))
        save_settings(settings)
        if checked and not is_admin():
            box = MessageBoxBase(self.window())
            layout = QVBoxLayout()
            layout.addWidget(BodyLabel('Restart as administrator?'))
            box.viewLayout.addLayout(layout)
            box.yesButton.setText('Restart Now')
            box.cancelButton.setText('Later')
            if box.exec() and relaunch_as_admin():
                window = self.window()
                if hasattr(window, '_force_quit'):
                    window._force_quit = True
                window.close()
        self.settings_changed.emit()

    def _on_show_console_toggled(self, checked: bool) -> None:
        self._apply_setting('show_console', bool(checked))
        try:
            set_debug_console_visible(bool(checked))
        except Exception:
            logger.exception('Failed to toggle debug console')

    def _on_log_to_file_toggled(self, checked: bool) -> None:
        self._apply_setting('log_to_file', bool(checked))
        try:
            from src.gui.main_window import set_file_logging_enabled
            set_file_logging_enabled(bool(checked))
        except Exception:
            logger.exception('Failed to toggle file logging')

    def _on_open_logs_folder(self) -> None:
        error = ''
        try:
            from src.gui.main_window import log_file_path
            folder = os.path.dirname(log_file_path())
            os.makedirs(folder, exist_ok=True)
            opened = QDesktopServices.openUrl(QUrl.fromLocalFile(folder))
        except Exception as exc:
            logger.exception('Failed to open logs folder')
            error = str(exc or '')
            opened = False
        if not opened:
            InfoBar.error(
                title=self._t('settings.open_logs_failed_title', "Couldn't open logs folder"),
                content=error, orient=Qt.Orientation.Horizontal, isClosable=True,
                position=InfoBarPosition.TOP_RIGHT, duration=4000, parent=self.window())


    @staticmethod
    def _default_backup_dir() -> str:
        docs = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.DocumentsLocation)
        return docs or os.path.expanduser('~')

    def _on_export_settings(self) -> None:
        name = time.strftime('pirachest-settings-%Y-%m-%d.json')
        path, _filter = QFileDialog.getSaveFileName(
            self, self._t('settings.backup_export_title', 'Export Settings'),
            os.path.join(self._default_backup_dir(), name), 'JSON files (*.json)')
        if not path:
            return
        if not path.lower().endswith('.json'):
            path += '.json'
        try:
            target = write_settings_backup(path)
        except (OSError, ValueError) as exc:
            logger.exception('Failed to export settings backup')
            InfoBar.error(
                title=self._t('settings.backup_export_failed_title', "Couldn't export settings"),
                content=str(exc), orient=Qt.Orientation.Horizontal, isClosable=True,
                position=InfoBarPosition.TOP_RIGHT, duration=5000, parent=self.window())
            return
        logger.info('Settings exported to %s', target)
        InfoBar.success(
            title=self._t('settings.backup_export_done_title', 'Settings exported'),
            content=self._t('settings.backup_export_done_content',
                            'Your settings backup was saved successfully.'),
            orient=Qt.Orientation.Horizontal, isClosable=True,
            position=InfoBarPosition.TOP_RIGHT, duration=3500, parent=self.window())

    def _on_import_settings(self) -> None:
        path, _filter = QFileDialog.getOpenFileName(
            self, self._t('settings.backup_import_title', 'Import Settings'),
            self._default_backup_dir(), 'JSON files (*.json)')
        if not path:
            return
        box = MessageBoxBase(self.window())
        box.widget.setMinimumWidth(470)
        layout = QVBoxLayout()
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        layout.addWidget(StrongBodyLabel(self._t(
            'settings.backup_confirm_title', 'Restore settings from this backup?')))
        content = BodyLabel(self._t(
            'settings.backup_confirm_content',
            'This replaces all current settings with the values in the backup. PiraChest will recommend a restart afterwards.'))
        content.setWordWrap(True)
        layout.addWidget(content)
        box.viewLayout.addLayout(layout)
        box.yesButton.setText(self._t('settings.backup_restore_button', 'Restore'))
        box.cancelButton.setText(self._t('common.cancel', 'Cancel'))
        if not box.exec():
            return
        try:
            snapshot = load_settings_backup(path)
            changed = apply_settings_backup(snapshot)
        except (OSError, ValueError) as exc:
            logger.exception('Failed to restore settings backup')
            InfoBar.error(
                title=self._t('settings.backup_import_failed_title', "Couldn't restore settings"),
                content=str(exc), orient=Qt.Orientation.Horizontal, isClosable=True,
                position=InfoBarPosition.TOP_RIGHT, duration=6000, parent=self.window())
            return
        logger.info('Settings restored from backup (%d fields changed)', changed)
        self.settings_changed.emit()
        InfoBar.success(
            title=self._t('settings.backup_import_done_title', 'Settings restored'),
            content=self._t('settings.backup_import_done_content',
                            '{count} setting(s) updated. Restart PiraChest to apply everything.',
                            count=changed),
            orient=Qt.Orientation.Horizontal, isClosable=True,
            position=InfoBarPosition.TOP_RIGHT, duration=5000, parent=self.window())
        self._offer_restart_after_restore()

    def _offer_restart_after_restore(self) -> None:
        box = MessageBoxBase(self.window())
        box.widget.setMinimumWidth(430)
        layout = QVBoxLayout()
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        layout.addWidget(StrongBodyLabel(self._t(
            'settings.backup_restart_title', 'Restart to finish restoring?')))
        content = BodyLabel(self._t(
            'settings.backup_restart_content',
            'Some restored settings only take effect after a restart. Restart now?'))
        content.setWordWrap(True)
        layout.addWidget(content)
        box.viewLayout.addLayout(layout)
        box.yesButton.setText(self._t('admin.restart_yes', 'Restart Now'))
        box.cancelButton.setText(self._t('admin.restart_later', 'Later'))
        if box.exec() and relaunch_app():
            window = self.window()
            if hasattr(window, '_force_quit'):
                window._force_quit = True
            window.close()


    def _on_repair_associations(self) -> None:
        try:
            register_associations()
        except Exception:
            logger.exception('Failed to repair file associations')
        self._refresh_associations_status()

    def _on_make_default_torrent_client(self) -> None:
        try:
            if not settings.associations_enabled or associations_need_repair():
                register_associations()
        except Exception:
            logger.exception('Failed to register file associations before opening Default Apps')
        self._refresh_associations_status()
        if not self._launch_windows_default_apps_ui(_ASSOC_APP_REGISTRY_NAME):
            QDesktopServices.openUrl(QUrl('ms-settings:defaultapps'))
            InfoBar.info(tr('settings.make_default_opened_title'),
                         tr('settings.make_default_opened_content'),
                         position=InfoBarPosition.TOP, parent=self)

    @staticmethod
    def _launch_windows_default_apps_ui(app_registry_name: str) -> bool:
        if os.name != 'nt':
            return False
        from ctypes import wintypes
        ole32 = ctypes.windll.ole32
        co_init_hr = None
        pointer = ctypes.c_void_p()
        try:
            co_init_hr = ole32.CoInitialize(None)
            clsid = wintypes.GUID()
            iid = wintypes.GUID()
            ole32.CLSIDFromString(ctypes.c_wchar_p('{1f76a169-f994-40ac-8fc8-0959e8874710}'),
                                 ctypes.byref(clsid))
            ole32.CLSIDFromString(ctypes.c_wchar_p('{1968106d-f3b5-44cf-890e-116fcb9ecef1}'),
                                 ctypes.byref(iid))
            hr = ole32.CoCreateInstance(ctypes.byref(clsid), None, 1,
                                        ctypes.byref(iid), ctypes.byref(pointer))
            if hr != 0 or not pointer.value:
                return False
            vtable = ctypes.cast(pointer, ctypes.POINTER(ctypes.c_void_p * 4)).contents
            launch = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, wintypes.LPCWSTR)(vtable[3])
            return launch(pointer, app_registry_name) == 0
        except Exception:
            logger.exception('Failed to launch Windows Default Apps UI via COM')
            return False
        finally:
            if pointer.value:
                with suppress(Exception):
                    vtable = ctypes.cast(pointer, ctypes.POINTER(ctypes.c_void_p * 4)).contents
                    release = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p)(vtable[2])
                    release(pointer)
            if co_init_hr in (0, 1):
                with suppress(Exception):
                    ole32.CoUninitialize()

    def _refresh_associations_status(self) -> None:
        self._assoc_status_lbl.setText(
            tr('settings.associations_needs_repair') if associations_need_repair() else '')


    def _on_auto_install_toggled(self, checked: bool) -> None:
        if checked and not confirm_experimental_features(self.window()):
            self._quiet(self._chk_auto_install, self._chk_auto_install.setChecked, False)
            return
        self._apply_setting('auto_install_games', bool(checked))

    def _on_open_theme_maker(self) -> None:
        if not confirm_experimental_features(self.window()):
            return
        try:
            open_theme_maker(self.window())
        except Exception:
            logger.exception('Failed to open Theme Maker')


    def showEvent(self, event) -> None:
        self._show_category(self._selected_category, animate=False)
        super().showEvent(event)
        if self._theme_dirty:
            self._run_theme_refresh()
        self._refresh_cache_size()

    def hideEvent(self, event) -> None:
        with suppress(Exception):
            self._cancel_shortcut_capture()
        super().hideEvent(event)


_LAST_APPLIED_FONT_FAMILIES: tuple | None = None


def apply_theme_font_families(font_families: tuple | None) -> None:
    global _LAST_APPLIED_FONT_FAMILIES
    key = tuple(font_families) if font_families else None
    if key == _LAST_APPLIED_FONT_FAMILIES:
        return
    _LAST_APPLIED_FONT_FAMILIES = key
    families = list(font_families) if font_families else ['Figtree', 'Segoe UI Variable', 'Segoe UI', 'Inter', 'Noto Sans', 'Arial']
    app = QApplication.instance()
    if app is not None:
        font = app.font()
        font.setFamilies(families)
        app.setFont(font)
    with suppress(Exception):
        setFontFamilies(families + ['Microsoft YaHei', 'PingFang SC'])


def apply_qfluentwidgets_font_families() -> None:
    with suppress(Exception):
        setFontFamilies(['Figtree', 'Segoe UI', 'Microsoft YaHei', 'PingFang SC'])


SHORTCUT_DEFAULTS = (
    ('home', 'Ctrl+H', 'Home', 'home_nav_page'),
    ('downloads', 'Ctrl+D', 'Downloads', 'download_page'),
    ('minerva', 'Ctrl+R', 'Minerva', 'home_page'),
    ('repacks', 'Ctrl+G', 'Repacks', 'pc_games_page'),
    ('music', 'Ctrl+M', 'Music', 'music_page'),
    ('books', 'Ctrl+B', 'Books', 'books_page'),
    ('youtube', 'Ctrl+Y', 'YouTube', 'yt_page'),
    ('tv', 'Ctrl+T', 'TV', 'tv_page'),
    ('tbhb', 'Ctrl+Shift+H', 'TBHB', 'tbhb_page'),
    ('sailseek', 'Ctrl+Shift+Space', 'SailSeek', None),
    ('settings', 'Ctrl+S', 'Settings', 'settings_page'),
    ('back', 'Escape', 'Previous page', None),
)


SHORTCUT_ICONS = {
    'home': ('nav.home', FluentIcon.HOME),
    'downloads': ('nav.downloads', FluentIcon.DOWNLOAD),
    'minerva': ('nav.minerva', FluentIcon.APPLICATION),
    'repacks': ('nav.pc_games', FluentIcon.GAME),
    'music': ('nav.music', FluentIcon.MUSIC),
    'books': ('nav.books', FluentIcon.BOOK_SHELF),
    'youtube': ('nav.youtube', FluentIcon.PLAY),
    'tv': ('nav.tv', FluentIcon.PROJECTOR),
    'tbhb': ('nav.tbhb', FluentIcon.GLOBE),
    'sailseek': ('sailseek.search', FluentIcon.SEARCH),
    'settings': ('nav.settings', FluentIcon.SETTING),
    'back': (None, FluentIcon.LEFT_ARROW),
}


def shortcut_map() -> dict:
    try:
        saved = settings.keyboard_shortcuts or {}
    except Exception:
        saved = {}
    mapping = {}
    for action_id, default_key, _label, _attr in SHORTCUT_DEFAULTS:
        key = saved.get(action_id, default_key)
        if not isinstance(key, str) or not key.strip():
            key = default_key
        elif QKeySequence(key).isEmpty():
            key = default_key
        mapping[action_id] = key
    return mapping
