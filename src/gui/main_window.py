from __future__ import annotations

import collections
import ctypes
import importlib
import logging
import os
import subprocess
import sys
import threading
import time
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import (
    Property,
    QAbstractAnimation,
    QEasingCurve,
    QElapsedTimer,
    QEvent,
    QObject,
    QPoint,
    QPointF,
    QPropertyAnimation,
    QRect,
    QRectF,
    QRunnable,
    QSize,
    Qt,
    QThreadPool,
    QTimer,
    QUrl,
    Signal,
)
from PySide6.QtGui import (
    QAction,
    QColor,
    QCursor,
    QDesktopServices,
    QFont,
    QFontDatabase,
    QFontMetrics,
    QIcon,
    QImageReader,
    QKeySequence,
    QMouseEvent,
    QMovie,
    QPainter,
    QPainterPath,
    QPalette,
    QPen,
    QPixmap,
    QShortcut,
    QSurfaceFormat,
)
from PySide6.QtWidgets import (
    QAbstractButton,
    QAbstractItemView,
    QAbstractScrollArea,
    QAbstractSpinBox,
    QApplication,
    QComboBox,
    QDial,
    QDialog,
    QFileDialog,
    QFrame,
    QGraphicsBlurEffect,
    QGraphicsOpacityEffect,
    QGraphicsPixmapItem,
    QGraphicsScene,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMenu,
    QMessageBox,
    QPlainTextEdit,
    QScrollBar,
    QSizePolicy,
    QSlider,
    QStackedWidget,
    QSystemTrayIcon,
    QTableView,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)
from qfluentwidgets import (
    BodyLabel,
    CaptionLabel,
    CardWidget,
    CheckBox,
    FluentIcon,
    FluentWindow,
    IconWidget,
    InfoBadge,
    InfoBadgePosition,
    InfoBar,
    InfoBarPosition,
    LineEdit,
    MessageBoxBase,
    NavigationItemPosition,
    Pivot,
    PrimaryPushButton,
    PushButton,
    qconfig,
    ScrollArea,
    Slider,
    SplashScreen,
    setFont,
    setTheme,
    StrongBodyLabel,
    SubtitleLabel,
    SwitchButton,
    Theme,
    themeColor,
    ToolTip,
    ToolTipPosition,
    TransparentToolButton,
)
from shiboken6 import isValid as _qt_is_valid

from src.core.config import (
    apply_settings,
    associations_available,
    associations_need_repair,
    check_dir_writable,
    download_dir_changes,
    extract_cli_targets,
    format_size,
    free_space_bytes,
    get_launch_at_startup,
    is_admin,
    paths,
    register_associations,
    resolve_theme,
    save_settings,
    set_launch_at_startup,
    settings,
    ThemeMode,
)
from src.core.theme import (
    filled_icon,
    activate_theme,
    active_preset_id,
    all_preset_ids,
    apply_label_theme,
    apply_pivot_theme,
    apply_tool_button_theme,
    ChromeChip,
    ensure_contrast,
    manager as theme_manager,
    global_app_qss,
    open_theme_maker,
    palette,
    preset_label,
    preset_meta,
    register_theme_refresh,
    resolve_font_families,
    set_popout_sfx_hook,
    settings_qss,
    theme_background,
    theme_background_scope,
    theme_shader_aa,
    theme_shader_fps,
    theme_shader_image_path,
    theme_shader_path,
    themed_icon,
    trimmed_icon,
    themed_nav_icon,
    to_color,
    WindowChrome,
)
from src.core.translations import current_language, register_locale_refresh, set_language, tr

from .about_page import AboutPage, DISCORD_URL, find_app_icon_path, find_discord_icon_path
from .home_page import HomePage
from .sailseek import SailSeekController
from .settings_page import (
    apply_qfluentwidgets_font_families,
    apply_theme_font_families,
    confirm_experimental_features,
    get_sound_manager,
    install_ui_sound_filter,
    play_popout_sfx,
    set_debug_console_visible,
    SettingsPage,
    SHORTCUT_DEFAULTS,
    shortcut_map,
    show_update_dialog,
    tr_or,
    update_controller,
    UpdateOverlay,
)

set_popout_sfx_hook(play_popout_sfx)
logger = logging.getLogger(__name__)

_LOG_FORMAT = '%(asctime)s [%(levelname)s] %(name)s — %(message)s'
_LOG_FILE_HANDLER: logging.FileHandler | None = None
_PREV_EXCEPTHOOK = None
_QT_MESSAGE_HANDLER_INSTALLED = False


def log_file_path() -> str:
    return os.path.join(paths.app_data_dir, 'logs', 'app.log')


def _crash_excepthook(exc_type, exc_value, exc_tb) -> None:
    logging.getLogger().critical('Unhandled exception', exc_info=(exc_type, exc_value, exc_tb))
    if _PREV_EXCEPTHOOK is not None:
        _PREV_EXCEPTHOOK(exc_type, exc_value, exc_tb)


def install_exception_hooks() -> None:
    global _PREV_EXCEPTHOOK
    if _PREV_EXCEPTHOOK is None:
        _PREV_EXCEPTHOOK = sys.excepthook
        sys.excepthook = _crash_excepthook


def set_file_logging_enabled(enabled: bool) -> None:
    global _LOG_FILE_HANDLER
    root_logger = logging.getLogger()
    if enabled:
        if _LOG_FILE_HANDLER is not None:
            return
        path = log_file_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        handler = logging.FileHandler(path, encoding='utf-8')
        handler.setFormatter(logging.Formatter(_LOG_FORMAT))
        root_logger.addHandler(handler)
        if root_logger.level > logging.INFO or root_logger.level == logging.NOTSET:
            root_logger.setLevel(logging.INFO)
        _LOG_FILE_HANDLER = handler
        logger.info('File logging enabled: %s', path)
    elif _LOG_FILE_HANDLER is not None:
        root_logger.removeHandler(_LOG_FILE_HANDLER)
        _LOG_FILE_HANDLER.close()
        _LOG_FILE_HANDLER = None


def configure_logging(enable_file: bool = True) -> None:
    root_logger = logging.getLogger()
    if not root_logger.handlers:
        logging.basicConfig(level=logging.INFO, format=_LOG_FORMAT)
    elif root_logger.level > logging.INFO or root_logger.level == logging.NOTSET:
        root_logger.setLevel(logging.INFO)
    if enable_file:
        set_file_logging_enabled(True)


def _qt_message_handler(mode, context, message) -> None:
    from PySide6.QtCore import QtMsgType
    levels = {
        QtMsgType.QtDebugMsg: logging.DEBUG,
        QtMsgType.QtInfoMsg: logging.INFO,
        QtMsgType.QtWarningMsg: logging.WARNING,
        QtMsgType.QtCriticalMsg: logging.ERROR,
        QtMsgType.QtFatalMsg: logging.CRITICAL,
    }
    logging.getLogger('qt').log(levels.get(mode, logging.INFO), message)


def install_qt_message_handler() -> None:
    global _QT_MESSAGE_HANDLER_INSTALLED
    if _QT_MESSAGE_HANDLER_INSTALLED:
        return
    from PySide6.QtCore import qInstallMessageHandler
    qInstallMessageHandler(_qt_message_handler)
    _QT_MESSAGE_HANDLER_INSTALLED = True


def shutdown_logging() -> None:
    global _LOG_FILE_HANDLER
    if _LOG_FILE_HANDLER is not None:
        logging.getLogger().removeHandler(_LOG_FILE_HANDLER)
        _LOG_FILE_HANDLER.close()
        _LOG_FILE_HANDLER = None
    logging.shutdown()


import math
import re
import struct
import weakref

from PySide6.QtGui import QImage, QMatrix4x4
from PySide6.QtOpenGL import (
    QOpenGLBuffer,
    QOpenGLFramebufferObject,
    QOpenGLShader,
    QOpenGLShaderProgram,
    QOpenGLTexture,
    QOpenGLVersionFunctionsFactory,
    QOpenGLVersionProfile,
    QOpenGLVertexArrayObject,
)
from PySide6.QtOpenGLWidgets import QOpenGLWidget

_GL_COLOR_BUFFER_BIT = 0x4000
_GL_TRIANGLE_STRIP = 0x0005
_GL_FLOAT = 0x1406
_GL_TEXTURE_2D = 0x0DE1
_GL_TEXTURE0 = 0x84C0
_GL_FRAMEBUFFER = 0x8D40
_GL_BLEND = 0x0BE2
_GL_ONE = 1
_GL_SRC_ALPHA = 0x0302
_GL_ONE_MINUS_SRC_ALPHA = 0x0303
_GL_MULTISAMPLE = 0x809D

_VERTEX_SHADER = """#version 330 core
layout(location = 0) in vec2 qt_Vertex;
layout(location = 1) in vec2 qt_UV;
uniform mat4 qt_Matrix;
uniform float flipV;
out vec2 qt_TexCoord0;
void main() {
    qt_TexCoord0 = vec2(qt_UV.x, mix(qt_UV.y, 1.0 - qt_UV.y, flipV));
    gl_Position = qt_Matrix * vec4(qt_Vertex, 0.0, 1.0);
}
"""

_BLIT_FRAG = """#version 330 core
in vec2 qt_TexCoord0;
out vec4 fragColor;
uniform sampler2D src;
uniform float qt_Opacity;
void main() {
    fragColor = texture(src, qt_TexCoord0) * qt_Opacity;
}
"""

_SOLID_FRAG = """#version 330 core
out vec4 fragColor;
uniform vec4 color;
void main() {
    fragColor = color;
}
"""

_MEDIA_BLUR_FRAG = """#version 330 core
in vec2 qt_TexCoord0;
out vec4 fragColor;
uniform sampler2D src;
uniform float qt_Opacity;
uniform vec2 direction;
uniform float sigma;

vec4 fetch(vec2 uv) {
    if (uv.x < 0.0 || uv.x > 1.0 || uv.y < 0.0 || uv.y > 1.0) return vec4(0.0);
    return texture(src, uv);
}

void main() {
    if (sigma < 0.05) { fragColor = texture(src, qt_TexCoord0) * qt_Opacity; return; }

    const int MAXR = 96;
    int radius = int(min(float(MAXR), ceil(sigma * 3.0)));
    float inv2s2 = 1.0 / (2.0 * sigma * sigma);

    float wsum = 1.0;
    vec4 acc = fetch(qt_TexCoord0);
    // Two neighbouring taps folded into one bilinear fetch: 2K+1 weights for K+1 reads.
    for (int i = 1; i <= MAXR; i += 2) {
        if (i > radius) break;
        float w1 = exp(-float(i * i) * inv2s2);
        float w2 = (i + 1 <= radius) ? exp(-float((i + 1) * (i + 1)) * inv2s2) : 0.0;
        float w  = w1 + w2;
        float off = (float(i) * w1 + float(i + 1) * w2) / w;
        acc  += (fetch(qt_TexCoord0 + direction * off) + fetch(qt_TexCoord0 - direction * off)) * w;
        wsum += 2.0 * w;
    }
    fragColor = (acc / wsum) * qt_Opacity;
}
"""

_VERSION_RE = re.compile(r'#version[^\n]*')
_BUF_BLOCK_RE = re.compile(r'layout\s*\(\s*std140[^)]*\)\s*uniform\s+buf\s*\{(.*?)\}\s*;', re.S)
_SAMPLER_BINDING_RE = re.compile(r'layout\s*\(\s*binding\s*=\s*\d+\s*\)\s*uniform\s+')
_LOCATION_RE = re.compile(r'layout\s*\(\s*location\s*=\s*\d+\s*\)\s*(?=in\b|out\b)')


def _translate_fragment(source: str) -> str:
    if _VERSION_RE.search(source):
        source = _VERSION_RE.sub('#version 330 core', source, count=1)
    else:
        source = '#version 330 core\n' + source

    def _expand(match: re.Match) -> str:
        body = re.sub(r'//[^\n]*', '', match.group(1))
        return '\n'.join('uniform ' + part.strip() + ';' for part in body.split(';') if part.strip())

    source = _BUF_BLOCK_RE.sub(_expand, source)
    source = _SAMPLER_BINDING_RE.sub('uniform ', source)
    return _LOCATION_RE.sub('', source)


def _file_stamp(path: str):
    try:
        st = os.stat(path)
    except OSError:
        return None
    return (st.st_mtime_ns, st.st_size)


def _shader_aa_samples() -> int:
    env_val = os.environ.get('PIRACHEST_SHADER_AA')
    if env_val is not None:
        try:
            n = int(env_val)
        except (TypeError, ValueError):
            n = 0
        return n if n in (2, 4, 8) else 0
    theme_aa = theme_shader_aa(active_preset_id())
    if theme_aa in (2, 4, 8):
        return theme_aa
    try:
        n = int(getattr(settings, 'shader_aa', 0))
    except Exception:
        n = 0
    return n if n in (2, 4, 8) else 0


def _max_shader_aa_samples() -> int:
    if os.environ.get('PIRACHEST_SHADER_AA') is not None:
        return _shader_aa_samples()
    samples = [_shader_aa_samples()]
    try:
        for preset_id in all_preset_ids():
            meta = preset_meta(preset_id)
            if meta is None or meta.background_type != 'shader':
                continue
            aa = theme_shader_aa(preset_id)
            if aa in (2, 4, 8):
                samples.append(aa)
    except Exception:
        logger.exception('Could not scan themes for shader AA')
    return max(samples)


def _gl_surface_wanted() -> bool:
    try:
        for pid in all_preset_ids():
            meta = preset_meta(pid)
            if meta is not None and meta.background_type == 'shader':
                return True
    except Exception:
        logger.exception('Could not scan themes for shader backgrounds')
    try:
        path = (settings.background_image_path or '') if settings.background_image_enabled else ''
        return os.path.splitext(path)[1].lower() in ('.gif', '.mp4', '.webm', '.mov', '.mkv', '.avi')
    except Exception:
        return False


def _report_bg_mode(mode: str, detail: str = '') -> None:
    logger.warning('[PiraChest] Background renderer: %s%s', mode, f' ({detail})' if detail else '')


class _Program:

    __slots__ = ('program', 'gl', '_locations')

    def __init__(self, program: QOpenGLShaderProgram, gl) -> None:
        self.program = program
        self.gl = gl
        self._locations: dict[str, int] = {}

    def bind(self) -> None:
        self.program.bind()

    def release(self) -> None:
        self.program.release()

    def delete_later(self) -> None:
        self.program.deleteLater()

    def _location(self, name: str) -> int:
        try:
            return self._locations[name]
        except KeyError:
            location = self.program.uniformLocation(name)
            self._locations[name] = location
            return location

    def set(self, name: str, value) -> None:
        location = self._location(name)
        if location < 0:
            return
        if isinstance(value, QMatrix4x4):
            self.program.setUniformValue(location, value)
        elif isinstance(value, QColor):
            self.gl.glUniform4f(location, value.redF(), value.greenF(), value.blueF(), value.alphaF())
        elif isinstance(value, QPointF):
            self.gl.glUniform2f(location, value.x(), value.y())
        elif isinstance(value, bool):
            self.gl.glUniform1i(location, int(value))
        elif isinstance(value, int):
            self.gl.glUniform1i(location, value)
        else:
            self.gl.glUniform1f(location, float(value))

    def has(self, name: str) -> bool:
        return self._location(name) >= 0

    def f1(self, name: str, value: float) -> None:
        location = self._location(name)
        if location >= 0:
            self.gl.glUniform1f(location, value)

    def f2(self, name: str, x: float, y: float) -> None:
        location = self._location(name)
        if location >= 0:
            self.gl.glUniform2f(location, x, y)

    def f4(self, name: str, r: float, g: float, b: float, a: float) -> None:
        location = self._location(name)
        if location >= 0:
            self.gl.glUniform4f(location, r, g, b, a)


_STD_SAMPLERS = ('source', 'src', 'iChannel0', 'tex', 'image', 'uTexture')
_MOUSE_UNIFORMS = ('mousePosition', 'mouseVelocity', 'clickPosition', 'clickTime', 'movementSpeed')

if sys.platform == 'win32':
    from ctypes import wintypes as _wintypes
    try:
        _user32 = ctypes.WinDLL('user32')
        _TIMERPROC = ctypes.WINFUNCTYPE(None, _wintypes.HWND, _wintypes.UINT, ctypes.c_size_t, _wintypes.DWORD)
        _SetTimer = _user32.SetTimer
        _SetTimer.argtypes = [_wintypes.HWND, ctypes.c_size_t, _wintypes.UINT, _TIMERPROC]
        _SetTimer.restype = ctypes.c_size_t
        _KillTimer = _user32.KillTimer
        _KillTimer.argtypes = [_wintypes.HWND, ctypes.c_size_t]
        _KillTimer.restype = _wintypes.BOOL
    except (AttributeError, OSError):
        _TIMERPROC = _SetTimer = _KillTimer = None
else:
    _TIMERPROC = _SetTimer = _KillTimer = None


class _ModalFramePump:

    _TIMER_ID = 0x5043
    _RETIRE_AFTER = 45

    def __init__(self, owner) -> None:
        self._owner = weakref.ref(owner)
        self._hwnd = 0
        self._healthy = 0
        self._proc = _TIMERPROC(self._on_timer) if _TIMERPROC is not None else None

    @property
    def available(self) -> bool:
        return self._proc is not None and _SetTimer is not None

    def kick(self) -> None:
        self._healthy = 0
        if not self.available:
            return
        owner = self._owner()
        if owner is None or not _qt_is_valid(owner):
            return
        try:
            hwnd = int(owner.window().winId())
        except Exception:
            return
        if not hwnd or hwnd == self._hwnd:
            return
        self.stop()
        if _SetTimer(hwnd, self._TIMER_ID, 10, self._proc):
            self._hwnd = hwnd

    def stop(self) -> None:
        hwnd, self._hwnd = self._hwnd, 0
        if hwnd and _KillTimer is not None:
            _KillTimer(hwnd, self._TIMER_ID)

    def _on_timer(self, _hwnd, _msg, _timer_id, _tick) -> None:
        try:
            owner = self._owner()
            if owner is None or not _qt_is_valid(owner):
                self.stop()
            elif owner.drive_if_starved():
                self._healthy = 0
            else:
                self._healthy += 1
                if self._healthy >= self._RETIRE_AFTER:
                    self.stop()
        except Exception:
            logger.exception('modal frame pump failed')
            self.stop()


class _GLBackground(QOpenGLWidget):

    def __init__(self, parent=None, *, alpha: bool = True, samples: int | None = None) -> None:
        super().__init__(parent)
        fmt = QSurfaceFormat()
        fmt.setRenderableType(QSurfaceFormat.RenderableType.OpenGL)
        fmt.setVersion(3, 3)
        fmt.setProfile(QSurfaceFormat.OpenGLContextProfile.CoreProfile)
        fmt.setAlphaBufferSize(8 if alpha else 0)
        aa = _shader_aa_samples() if samples is None else samples
        if aa:
            fmt.setSamples(aa)
        self._samples = aa
        self.setFormat(fmt)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground, True)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)

        self._gl = None
        self._vao = None
        self._vbo = None


    def _init_gl(self) -> bool:
        if self._gl is not None:
            return True
        context = self.context()
        functions = None
        for major, minor, core in ((3, 3, True), (3, 2, True), (3, 3, False)):
            profile = QOpenGLVersionProfile()
            profile.setVersion(major, minor)
            if core:
                profile.setProfile(QSurfaceFormat.OpenGLContextProfile.CoreProfile)
            functions = QOpenGLVersionFunctionsFactory.get(profile, context)
            if functions is not None:
                break
        if functions is None:
            return False
        with suppress(Exception):
            functions.initializeOpenGLFunctions()
        self._gl = functions
        if self._samples:
            self._gl.glEnable(_GL_MULTISAMPLE)

        quad = struct.pack(
            '<16f',
            -1.0, -1.0, 0.0, 0.0,
            1.0, -1.0, 1.0, 0.0,
            -1.0, 1.0, 0.0, 1.0,
            1.0, 1.0, 1.0, 1.0,
        )
        self._vao = QOpenGLVertexArrayObject()
        if not self._vao.create():
            self._vao = None
            return False
        self._vbo = QOpenGLBuffer(QOpenGLBuffer.Type.VertexBuffer)
        if not self._vbo.create():
            self._vbo = None
            return False
        self._vbo.bind()
        self._vbo.allocate(quad, len(quad))

        setup = QOpenGLShaderProgram()
        setup.addShaderFromSourceCode(QOpenGLShader.ShaderTypeBit.Vertex, _VERTEX_SHADER)
        self._vao.bind()
        self._vbo.bind()
        setup.enableAttributeArray(0)
        setup.setAttributeBuffer(0, _GL_FLOAT, 0, 2, 16)
        setup.enableAttributeArray(1)
        setup.setAttributeBuffer(1, _GL_FLOAT, 8, 2, 16)
        self._vao.release()
        self._vbo.release()
        return True

    def _build_program(self, fragment_source: str):
        program = QOpenGLShaderProgram()
        if not program.addShaderFromSourceCode(QOpenGLShader.ShaderTypeBit.Vertex, _VERTEX_SHADER):
            return None, program.log()
        if not program.addShaderFromSourceCode(QOpenGLShader.ShaderTypeBit.Fragment, fragment_source):
            return None, program.log()
        program.bindAttributeLocation('qt_Vertex', 0)
        program.bindAttributeLocation('qt_UV', 1)
        if not program.link():
            return None, program.log()
        return _Program(program, self._gl), ''

    def _draw_quad(self, program: _Program, matrix: QMatrix4x4, *,
                   texture: int = 0, flip: bool = False, opacity: float = 1.0) -> None:
        gl = self._gl
        program.bind()
        program.set('qt_Matrix', matrix)
        program.set('flipV', 1.0 if flip else 0.0)
        program.set('qt_Opacity', float(opacity))
        if texture:
            gl.glActiveTexture(_GL_TEXTURE0)
            gl.glBindTexture(_GL_TEXTURE_2D, texture)
            program.set('src', 0)
        gl.glDisable(_GL_BLEND)
        self._vao.bind()
        gl.glDrawArrays(_GL_TRIANGLE_STRIP, 0, 4)
        self._vao.release()
        program.release()

    def _cover_matrix(self, media_w: int, media_h: int) -> QMatrix4x4:
        w = max(1, self.width())
        h = max(1, self.height())
        scale = max(w / max(1, media_w), h / max(1, media_h))
        draw_w = media_w * scale
        draw_h = media_h * scale
        dx = (w - draw_w) / 2.0
        dy = (h - draw_h) / 2.0
        matrix = QMatrix4x4()
        matrix.translate(2.0 * (dx + draw_w / 2.0) / w - 1.0, 1.0 - 2.0 * (dy + draw_h / 2.0) / h)
        matrix.scale(draw_w / w, draw_h / h)
        return matrix


class ShaderBackgroundWidget(_GLBackground):

    errorOccurred = Signal(str)

    def __init__(self, parent=None, *, samples: int | None = None) -> None:
        super().__init__(parent, samples=samples)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self._base_color = QColor('#101010')
        self._transparent_window = False
        self._active = True
        self._target_fps = 0.0
        self._failed = False
        self._pending_frag = None
        self._frag_path = None
        self._program = None
        self._image_program = None
        self._frag_stamp = None
        self._samplers = ()
        self._uses_mouse = True
        self._identity = QMatrix4x4()
        self._pump = None
        self._last_paint_t = self._last_tick_t = time.monotonic()

        self._time = 0.0
        self._delta = 0.0
        self._tilt_angle = 0.0
        self._tilt_velocity = 0.0
        self._mouse_pos = QPointF(0.0, 0.0)
        self._mouse_velocity = QPointF(0.0, 0.0)
        self._click_pos = QPointF(0.0, 0.0)
        self._click_time = -1000.0
        self._window_pos = QPoint(0, 0)
        self._window_velocity = QPointF(0.0, 0.0)
        self._accent_color = QColor('#00b7c3')
        self._page_index = 0.0

        self._image_path = None
        self._image_tex = None
        self._image_tex_path = None

        self._mouse_timer = QElapsedTimer()
        self._mouse_timer.start()
        self._last_mouse_pos = None
        self._move_timer = QElapsedTimer()
        self._move_timer.start()
        self._last_window_pos = None
        self._clock = QElapsedTimer()
        self._clock.start()

        self._anim = QTimer(self)
        self._anim.setTimerType(Qt.TimerType.PreciseTimer)
        self._anim.timeout.connect(self._tick)
        self._restart_anim()


    @property
    def uses_mouse(self) -> bool:
        return self._uses_mouse

    def sync_samples(self, samples: int | None = None) -> bool:
        want = _max_shader_aa_samples() if samples is None else samples
        if want <= self._samples or self._gl is not None:
            return True
        fmt = self.format()
        fmt.setSamples(want)
        self.setFormat(fmt)
        self._samples = want
        return True

    def set_shader(self, frag_path: str) -> bool:
        if self.is_active_shader(frag_path):
            return True
        self._pending_frag = frag_path
        self._failed = False
        self.update()
        return True

    def is_active_shader(self, frag_path: str) -> bool:
        return (self._frag_path == frag_path and self._program is not None
                and self._frag_stamp == _file_stamp(frag_path))

    def clear(self) -> None:
        if self._program is not None:
            self._program.delete_later()
        self._program = None
        self._frag_path = None
        self._frag_stamp = None
        self._pending_frag = None
        self._failed = False
        self._samplers = ()
        self._uses_mouse = True
        self._release_image()
        self._image_path = None
        self.update()

    def set_accent_color(self, color) -> None:
        color = QColor(color)
        if color == self._accent_color:
            return
        self._accent_color = color
        self.update()

    def set_base_color(self, color) -> None:
        try:
            color = QColor(color)
        except Exception:
            return
        if color == self._base_color:
            return
        self._base_color = color
        self.update()

    def set_transparent_window(self, enabled: bool) -> None:
        enabled = bool(enabled)
        if enabled == self._transparent_window:
            return
        self._transparent_window = enabled
        self.update()

    def set_image(self, image_path: str | None) -> None:
        if image_path == self._image_path:
            return
        self._image_path = image_path
        if image_path is None:
            self._release_image()
        self.update()

    def _release_image(self) -> None:
        if self._image_tex is not None and self.context() is not None:
            self.makeCurrent()
            self._image_tex.destroy()
            self.doneCurrent()
        self._image_tex = None
        self._image_tex_path = None

    def set_fps(self, fps: float) -> None:
        fps = max(0.0, float(fps))
        if fps == self._target_fps:
            return
        self._target_fps = fps
        self._restart_anim()

    def set_page_index(self, index: int) -> None:
        self._page_index = float(index)

    def set_active(self, active: bool) -> None:
        active = bool(active)
        if active == self._active:
            return
        self._active = active
        self._restart_anim()
        self.update()

    def report_mouse_move(self, pos: QPointF) -> None:
        now = self._mouse_timer.restart()
        dt = max(now / 1000.0, 1.0 / 240.0)
        last = self._last_mouse_pos
        self._last_mouse_pos = QPointF(pos)
        if last is None:
            return
        self._mouse_velocity = QPointF((pos.x() - last.x()) / dt, (pos.y() - last.y()) / dt)
        self._mouse_pos = QPointF(pos)

    def report_mouse_click(self, pos: QPointF) -> None:
        self._click_pos = QPointF(pos)
        self._click_time = self._time

    def report_window_moved(self, pos: QPoint) -> None:
        elapsed_ms = self._move_timer.restart()
        dt = max(elapsed_ms / 1000.0, 1.0 / 240.0)
        last = self._last_window_pos
        self._last_window_pos = QPoint(pos)
        if last is None:
            self._window_pos = QPoint(pos)
            return
        if dt > 0.2:
            return
        raw_vx = (pos.x() - last.x()) / dt
        raw_vy = (pos.y() - last.y()) / dt
        blend = 0.35
        vx = self._window_velocity.x() + (raw_vx - self._window_velocity.x()) * blend
        vy = self._window_velocity.y() + (raw_vy - self._window_velocity.y()) * blend
        self._window_pos = QPoint(pos)
        self._window_velocity = QPointF(max(-2500.0, min(2500.0, vx)), max(-2500.0, min(2500.0, vy)))


    def showEvent(self, event) -> None:
        super().showEvent(event)
        self._restart_anim()
        self.update()

    def hideEvent(self, event) -> None:
        super().hideEvent(event)
        self._anim.stop()
        if self._pump is not None:
            self._pump.stop()

    def note_native_loop(self, drive: bool = True) -> None:
        if _TIMERPROC is not None:
            if self._pump is None:
                self._pump = _ModalFramePump(self)
            self._pump.kick()
        if drive:
            self.drive_if_starved()

    def drive_if_starved(self) -> bool:
        if not (self._active and self._program is not None and not self._failed and self.isVisible()):
            return False
        now = time.monotonic()
        stall = max(0.04, self._anim.interval() / 1000.0 * 2.5)
        if now - self._last_paint_t < stall:
            return False
        if now - self._last_tick_t >= stall:
            self._advance(driven=True)
        self.update()
        QApplication.sendPostedEvents(self.window(), int(QEvent.Type.UpdateRequest))
        return True

    def _restart_anim(self) -> None:
        if self._target_fps > 0:
            self._anim.setInterval(max(1, round(1000.0 / self._target_fps)))
        else:
            self._anim.setInterval(16)
        if self._active and self.isVisible():
            self._clock.restart()
            self._last_paint_t = self._last_tick_t = time.monotonic()
            self._anim.start()
        else:
            self._anim.stop()

    def _tick(self) -> None:
        self._advance()
        self.update()

    def _advance(self, driven: bool = False) -> None:
        elapsed = self._clock.restart() / 1000.0
        dt = elapsed if (driven or self._target_fps <= 0) else self._anim.interval() / 1000.0
        self._last_tick_t = time.monotonic()
        self._time += dt
        self._delta = max(0.0, min(0.25, dt))

        velocity = self._window_velocity
        if velocity.x() != 0.0 or velocity.y() != 0.0:
            decay = math.pow(0.25, self._delta)
            nx, ny = velocity.x() * decay, velocity.y() * decay
            nx = 0.0 if abs(nx) < 1.0 else nx
            ny = 0.0 if abs(ny) < 1.0 else ny
            self._window_velocity = QPointF(nx, ny)
        velocity = self._mouse_velocity
        if velocity.x() != 0.0 or velocity.y() != 0.0:
            decay = math.pow(0.02, self._delta)
            nx, ny = velocity.x() * decay, velocity.y() * decay
            nx = 0.0 if abs(nx) < 0.01 else nx
            ny = 0.0 if abs(ny) < 0.01 else ny
            self._mouse_velocity = QPointF(nx, ny)

        target = max(-0.6, min(0.6, self._window_velocity.x() * 0.0009))
        accel = (target - self._tilt_angle) * 18.0 - self._tilt_velocity * 6.0
        self._tilt_velocity += accel * self._delta
        self._tilt_angle += self._tilt_velocity * self._delta


    def initializeGL(self) -> None:
        if not self._init_gl():
            self._fail('OpenGL 3.3 core profile is not available')
            return
        self._gl.glClearColor(0.0, 0.0, 0.0, 1.0)
        self._gl.glClear(_GL_COLOR_BUFFER_BIT)

    def paintGL(self) -> None:
        if self._gl is None:
            return
        self._last_paint_t = time.monotonic()
        if self._pending_frag is not None and not self._failed:
            self._compile_pending()
        if self._image_path is not None and self._image_tex_path != self._image_path:
            self._load_image_texture()

        gl = self._gl
        base = self._base_color
        if self._transparent_window:
            gl.glClearColor(0.0, 0.0, 0.0, 0.0)
        else:
            gl.glClearColor(base.redF(), base.greenF(), base.blueF(), 1.0)
        gl.glClear(_GL_COLOR_BUFFER_BIT)

        if self._image_tex is not None:
            matrix = self._cover_matrix(self._image_tex.width(), self._image_tex.height())
            self._draw_quad(self._image_program, matrix, texture=self._image_tex.textureId(), flip=True)

        if self._program is None or self._failed:
            return

        program = self._program
        program.bind()
        program.set('qt_Matrix', self._identity)
        program.f1('qt_Opacity', 1.0)
        program.f1('flipV', 1.0)
        if self._image_tex is not None:
            gl.glActiveTexture(_GL_TEXTURE0)
            gl.glBindTexture(_GL_TEXTURE_2D, self._image_tex.textureId())
            for name in self._samplers:
                program.set(name, 0)
        f1, f2 = program.f1, program.f2
        wv, mv, mp, cp, wp = self._window_velocity, self._mouse_velocity, self._mouse_pos, self._click_pos, self._window_pos
        acc = self._accent_color
        f1('time', self._time)
        f1('deltaTime', self._delta)
        f2('resolution', float(max(1, self.width())), float(max(1, self.height())))
        f2('mousePosition', mp.x(), mp.y())
        f2('mouseVelocity', mv.x(), mv.y())
        f2('clickPosition', cp.x(), cp.y())
        f1('clickTime', self._click_time)
        f2('windowPosition', float(wp.x()), float(wp.y()))
        f2('windowVelocity', wv.x(), wv.y())
        f1('windowMovement', math.hypot(wv.x(), wv.y()))
        f1('movementSpeed', math.hypot(mv.x(), mv.y()))
        f1('pageIndex', self._page_index)
        f1('tiltAngle', self._tilt_angle)
        f1('tiltVelocity', self._tilt_velocity)
        program.f4('accentColor', acc.redF(), acc.greenF(), acc.blueF(), acc.alphaF())
        gl.glEnable(_GL_BLEND)
        gl.glBlendFunc(_GL_SRC_ALPHA, _GL_ONE_MINUS_SRC_ALPHA)
        self._vao.bind()
        gl.glDrawArrays(_GL_TRIANGLE_STRIP, 0, 4)
        self._vao.release()
        program.release()
        gl.glDisable(_GL_BLEND)

    def _compile_pending(self) -> None:
        path = self._pending_frag
        self._pending_frag = None
        try:
            with open(path, 'r', encoding='utf-8', errors='replace') as f:
                source = _translate_fragment(f.read())
        except OSError as exc:
            self._fail(f'Shader file not found: {path} ({exc})')
            return
        program, log = self._build_program(source)
        if program is None:
            self._fail(f'Shader failed to compile:\n{(log or "unknown compile error").strip()}')
            return
        if self._program is not None:
            self._program.delete_later()
        self._program = program
        self._frag_path = path
        self._frag_stamp = _file_stamp(path)
        self._uses_mouse = any(program.has(n) for n in _MOUSE_UNIFORMS)
        self._samplers = tuple(n for n in _STD_SAMPLERS if program.has(n))

    def _fail(self, message: str) -> None:
        if self._failed:
            return
        self._failed = True
        self._frag_path = None
        self._frag_stamp = None
        logger.warning('Shader background disabled: %s', message)
        QTimer.singleShot(0, self, lambda: self.errorOccurred.emit(message))

    def _load_image_texture(self) -> None:
        image = QImage(self._image_path)
        if image.isNull():
            self._image_path = None
            self._image_tex_path = None
            return
        if self._image_program is None:
            self._image_program, _ = self._build_program(_BLIT_FRAG)
        if self._image_tex is not None:
            self._image_tex.destroy()
        self._image_tex = QOpenGLTexture(image, QOpenGLTexture.MipMapGeneration.DontGenerateMipMaps)
        self._image_tex.setMinificationFilter(QOpenGLTexture.Filter.Linear)
        self._image_tex.setMagnificationFilter(QOpenGLTexture.Filter.Linear)
        self._image_tex.setWrapMode(QOpenGLTexture.WrapMode.ClampToEdge)
        self._image_tex_path = self._image_path


class MediaBackgroundWidget(_GLBackground):

    failed = Signal(str)

    SIGMA_PER_RADIUS = 0.468

    def __init__(self, parent=None) -> None:
        super().__init__(parent, alpha=False)
        self._kind = ''
        self._blur = 0
        self._darken = 0
        self._media_active = True
        self._failed = False
        self._player = None
        self._sink = None
        self._movie = None
        self._frame_image = None
        self._frame_tex = None
        self._fbo_a = None
        self._fbo_b = None
        self._fbo_size = None
        self._blur_program = None
        self._blit_program = None
        self._solid_program = None


    def start(self, kind: str, path: str, blur: int, darken: int) -> None:
        self._failed = False
        self._stop_media()
        self._kind = kind
        self._blur = int(blur)
        self._darken = int(darken)
        if kind == 'video':
            self._ensure_player()
            self._player.setSource(QUrl.fromLocalFile(path))
            self._sync_media_active()
        elif kind == 'gif':
            movie = QMovie(path, parent=self)
            if not movie.isValid():
                self._fail('GIF failed to load')
                return
            self._movie = movie
            movie.frameChanged.connect(self._on_movie_frame)
            self._sync_media_active()
        self.update()

    def set_params(self, blur: int, darken: int) -> None:
        self._blur = int(blur)
        self._darken = int(darken)
        self.update()

    def set_active(self, active: bool) -> None:
        self._media_active = bool(active)
        self._sync_media_active()

    def stop(self) -> None:
        self._stop_media()
        self._kind = ''
        self._frame_image = None
        self.update()


    def _ensure_player(self) -> None:
        if self._player is not None:
            return
        from PySide6.QtMultimedia import QMediaPlayer, QVideoSink
        sink = QVideoSink(self)
        sink.videoFrameChanged.connect(self._on_video_frame)
        player = QMediaPlayer(self)
        player.setVideoSink(sink)
        player.setLoops(QMediaPlayer.Loops.Infinite)
        player.setAudioOutput(None)
        self._sink = sink
        self._player = player

    def _sync_media_active(self) -> None:
        running = self._media_active
        if self._player is not None:
            if running and self._kind == 'video':
                self._player.play()
            else:
                self._player.pause()
        if self._movie is not None and self._kind == 'gif':
            if running:
                if self._movie.state() == QMovie.MovieState.NotRunning:
                    self._movie.start()
                else:
                    self._movie.setPaused(False)
            elif self._movie.state() == QMovie.MovieState.Running:
                self._movie.setPaused(True)

    def _stop_media(self) -> None:
        if self._player is not None:
            self._player.stop()
            self._player.setSource(QUrl())
        if self._movie is not None:
            self._movie.stop()
            with suppress(RuntimeError, TypeError):
                self._movie.frameChanged.disconnect(self._on_movie_frame)
            self._movie.deleteLater()
            self._movie = None

    def _on_video_frame(self, frame) -> None:
        image = frame.toImage() if frame is not None else None
        if image is not None and not image.isNull():
            self._frame_image = image
            self.update()

    def _on_movie_frame(self, _index: int) -> None:
        movie = self._movie
        if movie is None:
            return
        pixmap = movie.currentPixmap()
        if not pixmap.isNull():
            self._frame_image = pixmap.toImage()
            self.update()

    def _fail(self, message: str) -> None:
        if self._failed:
            return
        self._failed = True
        summary = message.strip().splitlines()[0][:120] if message.strip() else 'unknown error'
        logger.warning('GPU media background disabled, using CPU path: %s', summary)
        _report_bg_mode('CPU (GPU failed)', summary)
        QTimer.singleShot(0, self, lambda: self.failed.emit(message))


    def initializeGL(self) -> None:
        if not self._init_gl():
            self._fail('OpenGL 3.3 core profile is not available')
            return
        self._gl.glClearColor(0.0, 0.0, 0.0, 1.0)
        self._gl.glClear(_GL_COLOR_BUFFER_BIT)
        self._blur_program, log = self._build_program(_MEDIA_BLUR_FRAG)
        if self._blur_program is None:
            self._fail(f'Blur shader failed to compile: {log}')
            return
        self._blit_program, log = self._build_program(_BLIT_FRAG)
        if self._blit_program is None:
            self._fail(f'Blit shader failed to compile: {log}')
            return
        self._solid_program, log = self._build_program(_SOLID_FRAG)
        if self._solid_program is None:
            self._fail(f'Overlay shader failed to compile: {log}')
            return
        aa = _shader_aa_samples()
        _report_bg_mode('GPU', f'media, AA={str(aa) + "x" if aa else "off"}')

    def paintGL(self) -> None:
        gl = self._gl
        if gl is None:
            return
        gl.glClearColor(0.0, 0.0, 0.0, 1.0)
        gl.glClear(_GL_COLOR_BUFFER_BIT)

        image = self._frame_image
        if image is None or self._blur_program is None or self._failed:
            return
        self._sync_frame_texture(image)
        if self._frame_tex is None:
            return

        w = max(1, self.width())
        h = max(1, self.height())
        cover = max(w / max(1, image.width()), h / max(1, image.height()))
        sigma = self._blur * self.SIGMA_PER_RADIUS * cover
        cover_matrix = self._cover_matrix(image.width(), image.height())
        full_matrix = QMatrix4x4()

        if self._blur > 0 and sigma >= 0.05 and self._ensure_fbos():
            dpr = self.devicePixelRatioF()
            fw = max(1, int(round(w * dpr)))
            fh = max(1, int(round(h * dpr)))
            gl.glViewport(0, 0, fw, fh)
            self._fbo_a.bind()
            self._draw_quad(self._blit_program, cover_matrix, texture=self._frame_tex.textureId(), flip=True)
            self._fbo_b.bind()
            self._blur_pass(self._fbo_a.texture(), (1.0 / fw, 0.0), sigma * dpr, full_matrix)
            gl.glBindFramebuffer(_GL_FRAMEBUFFER, int(self.defaultFramebufferObject()))
            gl.glViewport(0, 0, fw, fh)
            self._blur_pass(self._fbo_b.texture(), (0.0, 1.0 / fh), sigma * dpr, full_matrix)
        else:
            self._draw_quad(self._blit_program, cover_matrix, texture=self._frame_tex.textureId(), flip=True)

        if self._darken > 0 and self._solid_program is not None:
            self._solid_program.bind()
            self._solid_program.set('qt_Matrix', full_matrix)
            self._solid_program.set('color', QColor(0, 0, 0, int(round(255 * min(100, self._darken) / 100.0))))
            gl.glEnable(_GL_BLEND)
            gl.glBlendFunc(_GL_SRC_ALPHA, _GL_ONE_MINUS_SRC_ALPHA)
            self._vao.bind()
            gl.glDrawArrays(_GL_TRIANGLE_STRIP, 0, 4)
            self._vao.release()
            self._solid_program.release()

    def _blur_pass(self, texture: int, direction, sigma: float, matrix: QMatrix4x4) -> None:
        program = self._blur_program
        gl = self._gl
        program.bind()
        program.set('qt_Matrix', matrix)
        program.set('flipV', 0.0)
        program.set('qt_Opacity', 1.0)
        program.set('direction', QPointF(*direction))
        program.set('sigma', float(sigma))
        gl.glActiveTexture(_GL_TEXTURE0)
        gl.glBindTexture(_GL_TEXTURE_2D, texture)
        program.set('src', 0)
        gl.glDisable(_GL_BLEND)
        self._vao.bind()
        gl.glDrawArrays(_GL_TRIANGLE_STRIP, 0, 4)
        self._vao.release()
        program.release()

    def _sync_frame_texture(self, image: QImage) -> None:
        texture = self._frame_tex
        if texture is not None and (texture.width() != image.width() or texture.height() != image.height()):
            texture.destroy()
            texture = None
        if texture is None:
            texture = QOpenGLTexture(image, QOpenGLTexture.MipMapGeneration.DontGenerateMipMaps)
            texture.setMinificationFilter(QOpenGLTexture.Filter.Linear)
            texture.setMagnificationFilter(QOpenGLTexture.Filter.Linear)
            texture.setWrapMode(QOpenGLTexture.WrapMode.ClampToEdge)
            self._frame_tex = texture
        else:
            texture.setData(image)

    def _ensure_fbos(self) -> bool:
        dpr = self.devicePixelRatioF()
        size = (max(1, int(round(self.width() * dpr))), max(1, int(round(self.height() * dpr))))
        if self._fbo_a is not None and self._fbo_size == size and self._fbo_a.isValid() and self._fbo_b.isValid():
            return True
        self._fbo_a = QOpenGLFramebufferObject(size[0], size[1])
        self._fbo_b = QOpenGLFramebufferObject(size[0], size[1])
        if not self._fbo_a.isValid() or not self._fbo_b.isValid():
            self._fail('Could not allocate blur framebuffers')
            return False
        self._fbo_size = size
        return True


_ET = QEvent.Type
_ET_TOOLTIP, _ET_ENTER, _ET_LEAVE, _ET_PRESS, _ET_HIDE, _ET_DESTROY, _ET_DEACTIVATE = (
    _ET.ToolTip, _ET.Enter, _ET.Leave, _ET.MouseButtonPress, _ET.Hide, _ET.Destroy, _ET.ApplicationDeactivate)
_ET_KEY, _ET_FOCUS_IN, _ET_RESIZE, _ET_MOVE = _ET.KeyPress, _ET.FocusIn, _ET.Resize, _ET.MouseMove
_MAIN_FILTER_TYPES = frozenset({_ET_KEY, _ET_FOCUS_IN, _ET_RESIZE, _ET_MOVE, _ET_PRESS})

_KONAMI_CODE = (
    Qt.Key.Key_Up, Qt.Key.Key_Up, Qt.Key.Key_Down, Qt.Key.Key_Down,
    Qt.Key.Key_Left, Qt.Key.Key_Right, Qt.Key.Key_Left, Qt.Key.Key_Right,
    Qt.Key.Key_B, Qt.Key.Key_A,
)
_KONAMI_URL = 'https://www.youtube.com/watch?v=dQw4w9WgXcQ'


class _AdminShield(QWidget):

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName('adminShield')
        self.setFixedSize(26, 16)

    def paintEvent(self, _event) -> None:
        try:
            color = QColor(themeColor())
        except Exception:
            color = QColor('#00b7c3')
        x, y, w, h = 8.0, 1.0, 12.0, 14.0
        shield = QPainterPath()
        shield.moveTo(x + w / 2, y)
        shield.lineTo(x + w, y + h * 0.18)
        shield.lineTo(x + w, y + h * 0.52)
        shield.cubicTo(x + w, y + h * 0.78, x + w * 0.72, y + h * 0.92, x + w / 2, y + h)
        shield.cubicTo(x + w * 0.28, y + h * 0.92, x, y + h * 0.78, x, y + h * 0.52)
        shield.lineTo(x, y + h * 0.18)
        shield.closeSubpath()
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.fillPath(shield, color)
        pen = QPen(QColor('#ffffff') if color.lightness() < 160 else QColor('#1b1b1b'), 1.6)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        painter.setPen(pen)
        tick = QPainterPath()
        tick.moveTo(x + w * 0.28, y + h * 0.50)
        tick.lineTo(x + w * 0.45, y + h * 0.66)
        tick.lineTo(x + w * 0.74, y + h * 0.34)
        painter.drawPath(tick)
        painter.end()


def _darken_alpha(percent: int) -> int:
    return max(0, min(255, int(255 * percent / 100)))


def _darken_pixmap(pixmap: QPixmap, darken_percent: int) -> QPixmap:
    if darken_percent <= 0:
        return pixmap
    result = QPixmap(pixmap.size())
    result.fill(Qt.GlobalColor.transparent)
    painter = QPainter(result)
    painter.drawPixmap(0, 0, pixmap)
    painter.fillRect(result.rect(), QColor(0, 0, 0, _darken_alpha(darken_percent)))
    painter.end()
    return result


class _BlurRenderer:
    def __init__(self):
        self._scene = None
        self._item = None
        self._effect = None
        self._radius = None

    def _ensure(self, radius: int) -> None:
        if self._scene is None:
            self._scene = QGraphicsScene()
            self._scene.setBackgroundBrush(Qt.BrushStyle.NoBrush)
            self._item = QGraphicsPixmapItem()
            self._effect = QGraphicsBlurEffect()
            self._effect.setBlurHints(QGraphicsBlurEffect.BlurHint.QualityHint)
            self._item.setGraphicsEffect(self._effect)
            self._scene.addItem(self._item)
        if self._radius != radius:
            self._effect.setBlurRadius(radius)
            self._radius = radius

    def render(self, pixmap: QPixmap, radius: int, darken_percent: int) -> QPixmap:
        self._ensure(radius)
        self._item.setPixmap(pixmap)
        size = pixmap.size()
        out = QPixmap(size)
        out.fill(Qt.GlobalColor.transparent)
        painter = QPainter(out)
        painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_Source)
        self._scene.render(painter, target=QRectF(out.rect()), source=QRectF(pixmap.rect()))
        if darken_percent > 0:
            painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceOver)
            painter.fillRect(out.rect(), QColor(0, 0, 0, _darken_alpha(darken_percent)))
        painter.end()
        return out


_blur_renderer: _BlurRenderer | None = None


def _process_background_pixmap(pixmap: QPixmap, blur_radius: int, darken_percent: int) -> QPixmap:
    global _blur_renderer
    if blur_radius > 0:
        if _blur_renderer is None:
            _blur_renderer = _BlurRenderer()
        return _blur_renderer.render(pixmap, blur_radius, darken_percent)
    return _darken_pixmap(pixmap, darken_percent)


_GUI_DIR = os.path.dirname(os.path.abspath(__file__))
_FONT_DIR = os.path.join(_GUI_DIR, 'lang', 'fonts')
_FIGTREE_FILENAMES = ('Inter.ttf',)


def _load_bundled_figtree_fonts() -> bool:
    files = [p for p in (os.path.join(_FONT_DIR, name) for name in _FIGTREE_FILENAMES) if os.path.isfile(p)]
    return any([QFontDatabase.addApplicationFont(p) != -1 for p in files])


_FLUENT_UPLIFT_APPLIED = False


def _event_x(e, *accessors: str) -> float:
    for name in accessors:
        fn = getattr(e, name, None)
        if callable(fn):
            with suppress(Exception):
                return float(fn().x())
    return 0.0


def _apply_switch_uplift() -> None:
    from qfluentwidgets.common.color import fallbackThemeColor, validColor
    from qfluentwidgets.common.style_sheet import ThemeColor, isDarkTheme
    from qfluentwidgets.components.widgets.switch_button import Indicator, SwitchButton

    def patch(owner, name):
        def install(fn):
            setattr(owner, name, fn)
            return fn
        return install

    Indicator.normalKnobSize, Indicator.hoverKnobSize = 12, 14
    Indicator.pressedKnobWidth, Indicator.pressedKnobHeight = 17, 14
    Indicator.knobSlotWidth, Indicator.trackLeft = 20, 1
    Indicator.offSliderX, Indicator.onSliderX, Indicator.dragThreshold = 5, 25, 2
    Indicator.controlFastDuration, Indicator.controlFasterDuration = 167, 83
    Indicator.stateEvents = (QEvent.Type.EnabledChange, QEvent.Type.MouseButtonPress,
                             QEvent.Type.Enter, QEvent.Type.Leave)

    _orig_indicator_init = Indicator.__init__

    @patch(Indicator, '__init__')
    def _indicator_init(self, parent):
        _orig_indicator_init(self, parent)
        self._sliderX = Indicator.offSliderX
        self._knobWidth = self._knobHeight = Indicator.normalKnobSize
        self._checkedProgress = 0
        self._isDragging, self._pressX, self._pressSliderX = False, None, Indicator.offSliderX
        curve = Indicator._fastOutSlowInCurve()
        self.slideAni = QPropertyAnimation(self, b'sliderX', self)
        self.checkedAni = QPropertyAnimation(self, b'checkedProgress', self)
        self.knobWidthAni = QPropertyAnimation(self, b'knobWidth', self)
        self.knobHeightAni = QPropertyAnimation(self, b'knobHeight', self)
        for ani, duration, easing in [
            (self.slideAni, Indicator.controlFastDuration, curve),
            (self.checkedAni, Indicator.controlFasterDuration, QEasingCurve.Linear),
            (self.knobWidthAni, Indicator.controlFasterDuration, curve),
            (self.knobHeightAni, Indicator.controlFasterDuration, curve),
        ]:
            ani.setDuration(duration)
            ani.setEasingCurve(easing)

    @patch(Indicator, '_fastOutSlowInCurve')
    @staticmethod
    def _fastOutSlowInCurve():
        curve = QEasingCurve(QEasingCurve.Type.BezierSpline)
        curve.addCubicBezierSegment(QPointF(0, 0), QPointF(0, 1), QPointF(1, 1))
        return curve

    @patch(Indicator, 'event')
    def _indicator_event(self, e: QEvent):
        result = super(Indicator, self).event(e)
        if e.type() in Indicator.stateEvents:
            self._updateKnobSize()
        return result

    @patch(Indicator, 'mousePressEvent')
    def _indicator_press(self, e):
        if e.button() == Qt.MouseButton.LeftButton:
            self._startDrag(_event_x(e, 'position', 'pos'))
        super(Indicator, self).mousePressEvent(e)

    @patch(Indicator, 'mouseMoveEvent')
    def _indicator_move(self, e):
        if e.buttons() & Qt.MouseButton.LeftButton and self._moveDrag(_event_x(e, 'position', 'pos')):
            e.accept()
            return
        super(Indicator, self).mouseMoveEvent(e)

    @patch(Indicator, 'mouseReleaseEvent')
    def _indicator_release(self, e):
        if e.button() == Qt.MouseButton.LeftButton and self._endDrag():
            self.checkedChanged.emit(self.isChecked())
            e.accept()
            return
        super(Indicator, self).mouseReleaseEvent(e)
        self._updateKnobSize()
        self.checkedChanged.emit(self.isChecked())

    @patch(Indicator, '_toggleSlider')
    def _toggleSlider(self):
        self._startAnimation(self.slideAni, Indicator.onSliderX if self.isChecked() else Indicator.offSliderX, self.sliderX)
        self._startAnimation(self.checkedAni, 1 if self.isChecked() else 0, self.checkedProgress)

    @patch(Indicator, '_startDrag')
    def _startDrag(self, x, updateDown=False):
        self._isDragging, self._pressX, self._pressSliderX = False, x, self.sliderX
        if updateDown:
            self.setDown(True)

    @patch(Indicator, '_moveDrag')
    def _moveDrag(self, x):
        if self._pressX is None or not self.isEnabled():
            return False
        dx = x - self._pressX
        if not self._isDragging:
            if abs(dx) < Indicator.dragThreshold:
                return False
            self._isDragging = True
            self.slideAni.stop()
            self.checkedAni.stop()
        self.setSliderX(self._pressSliderX + dx)
        self.setCheckedProgress((self.sliderX - Indicator.offSliderX) / Indicator.knobSlotWidth)
        return True

    @patch(Indicator, '_endDrag')
    def _endDrag(self):
        isDragging = self._isDragging
        self._pressX = None
        self._isDragging = False
        if not isDragging:
            return False
        self.setDown(False)
        if self.sliderX <= 15 if self.isChecked() else self.sliderX >= 15:
            self.toggle()
        else:
            self._toggleSlider()
        return True

    @patch(Indicator, 'setDown')
    def _setDown(self, isDown: bool):
        self.isPressed = isDown
        super(Indicator, self).setDown(isDown)
        self._updateKnobSize()

    @patch(Indicator, 'setHover')
    def _setHover(self, isHover: bool):
        self.isHover = isHover
        self._updateKnobSize()

    @patch(Indicator, '_drawCircle')
    def _drawCircle(self, painter):
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(self._sliderColor())
        painter.drawEllipse(self._knobRect())

    @patch(Indicator, '_knobRect')
    def _knobRect(self):
        w = self.knobWidth
        h = self.knobHeight
        y = (self.height() - h) / 2
        if self.isPressed and self.isEnabled():
            progress = self.checkedProgress if self._isDragging else int(self.isChecked())
            x = Indicator.trackLeft + 3 + (Indicator.knobSlotWidth * 2 - w - 6) * progress
        else:
            x = self.sliderX + (Indicator.normalKnobSize - w) / 2
        return QRectF(x, y, w, h)

    @patch(Indicator, '_updateKnobSize')
    def _updateKnobSize(self):
        if not self.isEnabled():
            size = (Indicator.normalKnobSize, Indicator.normalKnobSize)
        elif self.isPressed:
            size = (Indicator.pressedKnobWidth, Indicator.pressedKnobHeight)
        elif self.isHover:
            size = (Indicator.hoverKnobSize, Indicator.hoverKnobSize)
        else:
            size = (Indicator.normalKnobSize, Indicator.normalKnobSize)
        self._startKnobAnimation(*size)

    @patch(Indicator, '_startKnobAnimation')
    def _startKnobAnimation(self, width, height):
        if self.knobWidth == width and self.knobHeight == height:
            return
        self._startAnimation(self.knobWidthAni, width, self.knobWidth)
        self._startAnimation(self.knobHeightAni, height, self.knobHeight)

    @patch(Indicator, '_startAnimation')
    @staticmethod
    def _startAnimation(ani: QPropertyAnimation, endValue, startValue):
        ani.stop()
        ani.setStartValue(startValue)
        ani.setEndValue(endValue)
        ani.start()

    @patch(Indicator, '_backgroundColor')
    def _backgroundColor(self):
        return self._transitionColor(self._offBackgroundColor, self._onBackgroundColor)

    @patch(Indicator, '_onBackgroundColor')
    def _onBackgroundColor(self, isDark: bool):
        color = self.darkCheckedColor if isDark else self.lightCheckedColor
        if not self.isEnabled():
            return QColor(255, 255, 255, 41) if isDark else QColor(0, 0, 0, 56)
        if self.isPressed:
            return validColor(color, ThemeColor.LIGHT_2.color())
        elif self.isHover:
            return validColor(color, ThemeColor.LIGHT_1.color())
        return fallbackThemeColor(color)

    @patch(Indicator, '_offBackgroundColor')
    def _offBackgroundColor(self, isDark: bool):
        if not self.isEnabled():
            return QColor(0, 0, 0, 0)
        if self.isPressed:
            return QColor(255, 255, 255, 18) if isDark else QColor(0, 0, 0, 23)
        elif self.isHover:
            return QColor(255, 255, 255, 10) if isDark else QColor(0, 0, 0, 15)
        return QColor(0, 0, 0, 0)

    @patch(Indicator, '_borderColor')
    def _borderColor(self):
        return self._transitionColor(self._offBorderColor, self._onBorderColor)

    @patch(Indicator, '_onBorderColor')
    def _onBorderColor(self, isDark: bool):
        if self.isEnabled():
            return self._onBackgroundColor(isDark)
        return QColor(0, 0, 0, 0)

    @patch(Indicator, '_offBorderColor')
    def _offBorderColor(self, isDark: bool):
        if self.isEnabled():
            return QColor(255, 255, 255, 153) if isDark else QColor(0, 0, 0, 133)
        return QColor(255, 255, 255, 41) if isDark else QColor(0, 0, 0, 56)

    @patch(Indicator, '_sliderColor')
    def _sliderColor(self):
        return self._transitionColor(self._offSliderColor, self._onSliderColor)

    @patch(Indicator, '_onSliderColor')
    def _onSliderColor(self, isDark: bool):
        if self.isEnabled():
            return QColor(Qt.black if isDark else Qt.white)
        return QColor(255, 255, 255, 77) if isDark else QColor(255, 255, 255)

    @patch(Indicator, '_offSliderColor')
    def _offSliderColor(self, isDark: bool):
        if self.isEnabled():
            return QColor(255, 255, 255, 201) if isDark else QColor(0, 0, 0, 156)
        return QColor(255, 255, 255, 96) if isDark else QColor(0, 0, 0, 91)

    @patch(Indicator, '_transitionColor')
    def _transitionColor(self, offColor, onColor):
        return self._mixColor(offColor(isDarkTheme()), onColor(isDarkTheme()), self.checkedProgress)

    @patch(Indicator, '_mixColor')
    @staticmethod
    def _mixColor(start: QColor, end: QColor, progress: float):
        progress = max(0, min(1, progress))
        return QColor(
            round(start.red() + (end.red() - start.red()) * progress),
            round(start.green() + (end.green() - start.green()) * progress),
            round(start.blue() + (end.blue() - start.blue()) * progress),
            round(start.alpha() + (end.alpha() - start.alpha()) * progress)
        )

    @patch(Indicator, 'setSliderX')
    def setSliderX(self, x):
        self._sliderX = max(Indicator.offSliderX, min(Indicator.onSliderX, x))
        self.update()

    @patch(Indicator, 'getKnobWidth')
    def getKnobWidth(self):
        return self._knobWidth

    @patch(Indicator, 'setKnobWidth')
    def setKnobWidth(self, width):
        self._knobWidth = width
        self.update()

    @patch(Indicator, 'getKnobHeight')
    def getKnobHeight(self):
        return self._knobHeight

    @patch(Indicator, 'setKnobHeight')
    def setKnobHeight(self, height):
        self._knobHeight = height
        self.update()

    @patch(Indicator, 'getCheckedProgress')
    def getCheckedProgress(self):
        return self._checkedProgress

    @patch(Indicator, 'setCheckedProgress')
    def setCheckedProgress(self, progress):
        self._checkedProgress = max(0, min(1, progress))
        self.update()

    Indicator.knobWidth = Property(float, getKnobWidth, setKnobWidth)
    Indicator.knobHeight = Property(float, getKnobHeight, setKnobHeight)
    Indicator.checkedProgress = Property(float, getCheckedProgress, setCheckedProgress)

    Indicator.sliderX = Property(float, Indicator.getSliderX, setSliderX)

    _orig_init_widget = SwitchButton._SwitchButton__initWidget

    @patch(SwitchButton, '_SwitchButton__initWidget')
    def _initWidget(self):
        _orig_init_widget(self)
        with suppress(Exception):
            self.label.installEventFilter(self)

    def _indicator_x(indicator, e):
        if hasattr(e, 'globalPosition'):
            return indicator.mapFromGlobal(e.globalPosition().toPoint()).x()
        return _event_x(e, 'globalPosition', 'globalPos')

    @patch(SwitchButton, 'eventFilter')
    def _sb_event_filter(self, obj, e: QEvent):
        if obj in (self, self.label) and self.isEnabled():
            et, left, indicator = e.type(), Qt.MouseButton.LeftButton, self.indicator
            if et == _ET_PRESS and e.button() == left:
                indicator._startDrag(_indicator_x(indicator, e), True)
            elif et == _ET_MOVE and e.buttons() & left and indicator._moveDrag(_indicator_x(indicator, e)):
                return True
            elif et == _ET.MouseButtonRelease and e.button() == left:
                if not indicator._endDrag():
                    indicator.setDown(False)
                    indicator.toggle()
                return True
            elif et == _ET_ENTER:
                indicator.setHover(True)
            elif et == _ET_LEAVE:
                indicator.setHover(False)
        return super(SwitchButton, self).eventFilter(obj, e)


def apply_fluent_uplifts() -> None:
    global _FLUENT_UPLIFT_APPLIED
    if _FLUENT_UPLIFT_APPLIED:
        return
    if os.environ.get('PIRACHEST_NO_FLUENT_UPLIFT') == '1':
        return
    try:
        _apply_switch_uplift()
    except Exception:
        logger.exception('Fluent uplift failed: switch-button')
    _FLUENT_UPLIFT_APPLIED = True
apply_fluent_uplifts()


def _gc_engine_call(method: str, value) -> None:
    with suppress(Exception):
        from src.core import worker
        getattr(worker.engine(), method)(value)


def _hold_gc(ms: int) -> None:
    _gc_engine_call('hold_gc', ms)


def _hold_gc_for(seconds: float) -> None:
    _gc_engine_call('hold_for', seconds)


def _quiesce_qt_tree(root: QWidget) -> None:
    try:
        if not _qt_is_valid(root):
            return
        for obj in (root, *root.findChildren(QObject)):
            if not _qt_is_valid(obj):
                continue
            if isinstance(obj, (QTimer, QAbstractAnimation)):
                obj.stop()
            attrs = getattr(obj, '__dict__', None)
            if not attrs:
                continue
            ani = attrs.get('ani')
            if isinstance(ani, QAbstractAnimation) and _qt_is_valid(ani):
                ani.stop()
                if hasattr(ani, 'setTargetObject'):
                    ani.setTargetObject(None)

                if ani.parent() is None:
                    ani.setParent(obj)
            attrs.pop('setVerticalScrollBarPolicy', None)
            attrs.pop('setHorizontalScrollBarPolicy', None)
    except Exception:
        logger.exception('Failed to quiesce page before deletion')


class _PageOpQueue:

    _q: collections.deque = collections.deque()
    _active = False
    _GAP_MS = 12

    @classmethod
    def post(cls, fn) -> None:
        cls._q.append(fn)
        if not cls._active:
            cls._active = True
            QTimer.singleShot(0, cls._run)

    @classmethod
    def _run(cls) -> None:
        if not cls._q:
            cls._active = False
            return
        try:
            cls._q.popleft()()
        except Exception:
            logger.exception('LazyPage operation failed')
        if cls._q:
            QTimer.singleShot(cls._GAP_MS, cls._run)
        else:
            cls._active = False


class LazyPage(QWidget):
    __slots__ = (
        '_factory', '_keep_alive', '_shutdown', '_warm_up', '_on_ready',
        '_page', '_layout', '_generation', '_construct_pending', '_warmed_up',
        '_pending_calls',
    )

    def __init__(self, factory, *, keep_alive=False, shutdown=None, warm_up=None, on_ready=None, parent=None):
        super().__init__(parent)
        self._factory = factory
        self._keep_alive = keep_alive
        self._shutdown = shutdown
        self._warm_up = warm_up
        self._on_ready = on_ready
        self._page: QWidget | None = None
        self._generation = 0
        self._construct_pending = False
        self._warmed_up = False
        self._pending_calls: list[tuple[str, tuple, dict]] = []
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self._layout = layout

    @property
    def page(self) -> QWidget | None:
        return self._page

    def call_when_ready(self, method: str, *args, **kwargs) -> None:
        if self._page is not None:
            getattr(self._page, method)(*args, **kwargs)
            return
        self._pending_calls.append((method, args, kwargs))
        self._schedule_construction()

    def showEvent(self, event) -> None:
        if self._warm_up is not None and not self._warmed_up:
            self._warmed_up = True
            QTimer.singleShot(0, self._warm_up)
        self._schedule_construction()
        super().showEvent(event)

    def hideEvent(self, event) -> None:
        if not self._keep_alive:
            self._teardown()
        super().hideEvent(event)

    def _schedule_construction(self) -> None:
        if self._page is not None or self._construct_pending:
            return
        self._construct_pending = True
        generation = self._generation
        _PageOpQueue.post(lambda: self._construct(generation))

    def _construct(self, generation: int) -> None:
        if generation != self._generation:
            return
        self._construct_pending = False
        if not _qt_is_valid(self) or self._page is not None:
            return

        _hold_gc_for(3)
        page = self._factory()
        self._page = page
        self._layout.addWidget(page)
        if self._on_ready is not None:
            try:
                self._on_ready(page)
            except Exception:
                logger.exception('on_ready callback failed for %s', type(page).__name__)
        pending, self._pending_calls = self._pending_calls, []
        for method, args, kwargs in pending:
            try:
                getattr(page, method)(*args, **kwargs)
            except Exception:
                logger.exception('Deferred call to %s() failed after lazy page construction', method)

    def _teardown(self) -> None:
        self._generation += 1
        self._construct_pending = False
        if self._page is None:
            return
        page = self._page
        self._page = None
        self._pending_calls.clear()

        _hold_gc_for(3)
        page.hide()
        self._layout.removeWidget(page)
        _hold_gc(800)
        _PageOpQueue.post(lambda: self._finish_teardown(page))

    def _finish_teardown(self, page: QWidget) -> None:
        if not _qt_is_valid(page):
            return
        _hold_gc_for(3)
        self._run_shutdown(page)
        if _qt_is_valid(page):
            _quiesce_qt_tree(page)
            page.deleteLater()
        _hold_gc(800)

    def _run_shutdown(self, page: QWidget) -> None:
        if self._shutdown is not None:
            try:
                self._shutdown(page)
            except Exception:
                logger.exception('Shutdown callback failed for %s', type(page).__name__)

    def shutdown_now(self) -> None:
        if self._page is None:
            return
        page = self._page
        self._page = None
        self._generation += 1
        self._pending_calls.clear()
        self._run_shutdown(page)
        page.hide()
        self._layout.removeWidget(page)
        _quiesce_qt_tree(page)
        page.deleteLater()


def _shutdown_page(page: QWidget) -> None:
    page.shutdown()


def _shutdown_repacks_page(page: QWidget) -> None:
    from src.core import worker
    tabs = getattr(page, '_tabs', {}).values()
    for owner in (page, *tabs):
        try:
            worker.cancel_owner(owner)
        except Exception:
            logger.exception('Failed to cancel repacks tasks during page unload')
    for tab in tabs:
        try:
            tab.shutdown()
        except Exception:
            logger.exception('Failed to shut down repacks source tab during page unload')


class _GlobalToolTipFilter(QObject):
    _DELAY = 300
    _MAX_WIDTH = 320

    def __init__(self, app):
        super().__init__(app)
        self._widget = None
        self._tip = None
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self._show)

    def eventFilter(self, obj, event) -> bool:
        et = event.type()
        if et == _ET_TOOLTIP:
            if isinstance(obj, QWidget):
                self._widget = obj
                self._timer.stop()
                self._show()
            return True
        if et == _ET_ENTER:
            self._arm(obj)
        elif et == _ET_PRESS or et == _ET_DEACTIVATE:
            self._dismiss()
        elif obj is self._widget and (et == _ET_LEAVE or et == _ET_HIDE or et == _ET_DESTROY):
            self._dismiss()
        return False

    def _arm(self, obj) -> None:
        if not isinstance(obj, QWidget):
            return
        self._widget = obj
        self._timer.start(self._DELAY)

    def _dismiss(self) -> None:
        self._timer.stop()
        self._widget = None
        if self._tip is not None:
            try:
                self._tip.hide()
            except RuntimeError:
                self._tip = None

    @staticmethod
    def _item_tip(view, pos):
        try:
            index = view.indexAt(pos)
            if index is None or not index.isValid():
                return '', None
            text = index.data(Qt.ItemDataRole.ToolTipRole) or ''
            return str(text), (view.visualRect(index) if text else None)
        except Exception:
            return '', None

    @staticmethod
    def _menu_tip(menu):
        try:
            action = menu.actionAt(menu.mapFromGlobal(QCursor.pos()))
            return (action.toolTip() if action is not None else '') or ''
        except Exception:
            return ''

    def _resolve(self):
        widget = self._widget
        if widget is None:
            return None
        pos = QCursor.pos()
        while widget is not None:
            if not widget.isEnabled():
                break
            if isinstance(widget, QAbstractItemView):
                view_pos = widget.viewport().mapFromGlobal(pos) \
                    if isinstance(widget, QAbstractScrollArea) else widget.mapFromGlobal(pos)
                text, rect = self._item_tip(widget, view_pos)
                if text:
                    return text, widget, rect
            text = widget.toolTip()
            if text:
                return text, widget, None
            if isinstance(widget, QMenu):
                text = self._menu_tip(widget)
                if text:
                    return text, widget, None
            widget = widget.parentWidget()
        return None

    def _ensure_tip(self, window, text):
        from qfluentwidgets.components.widgets.tool_tip import ItemViewToolTip
        tip = self._tip
        if tip is not None:
            with suppress(RuntimeError):
                if tip.parent() is window:
                    if tip.text() != text:
                        tip.setText(text)
                    return tip
                tip.hide()
                tip.deleteLater()
            self._tip = None
        tip = ItemViewToolTip(text, window)
        tip.label.setWordWrap(True)
        tip.label.setMaximumWidth(self._MAX_WIDTH)
        tip.adjustSize()
        tip.setDuration(-1)
        self._tip = tip
        return tip

    def _show(self) -> None:
        widget = self._widget
        if widget is None:
            return
        try:
            if not _qt_is_valid(widget) or not widget.isVisible():
                self._dismiss()
                return
            resolved = self._resolve()
        except Exception:
            logger.exception('Failed to resolve tooltip text')
            self._dismiss()
            return
        if not resolved:
            self._dismiss()
            return
        text, anchor, rect = resolved
        try:
            from qfluentwidgets.components.widgets.tool_tip import ItemViewToolTipType
            window = anchor.window()
            if window is None:
                return
            tip = self._ensure_tip(window, text)
            if rect is None:
                ToolTip.adjustPos(tip, anchor, ToolTipPosition.TOP)
            else:
                tip_type = ItemViewToolTipType.TABLE if isinstance(anchor, QTableView) else ItemViewToolTipType.LIST
                tip.adjustPos(anchor, rect, tip_type)
            if tip.isVisible() and tip.text() == text:
                return
            tip.show()
        except Exception:
            logger.exception('Failed to show fluent tooltip')
            self._tip = None


_global_tool_tip: _GlobalToolTipFilter | None = None


def install_global_tool_tip(app) -> None:
    global _global_tool_tip
    if _global_tool_tip is not None:
        return
    _global_tool_tip = _GlobalToolTipFilter(app)
    app.installEventFilter(_global_tool_tip)


class VPNDisclaimerDialog(MessageBoxBase):
    def __init__(self, parent=None):
        super().__init__(parent)
        t = tr_or
        self.widget.setMinimumWidth(440)
        head = QHBoxLayout()
        head.setSpacing(10)
        icon = IconWidget(FluentIcon.INFO, self.widget)
        icon.setFixedSize(22, 22)
        head.addWidget(icon)
        head.addWidget(SubtitleLabel(t('disclaimer.title', 'Privacy and legal notice'), self.widget), 1)
        self.viewLayout.addLayout(head)
        self.viewLayout.setSpacing(10)
        for key, text in (
            ('disclaimer.safety', 'PiraChest includes privacy and security options, but no app can guarantee your safety or anonymity.'),
            ('disclaimer.legal', 'Piracy may be illegal where you live. Use a VPN and any other protection that fits your situation. You are responsible for your own safety and for following local laws.'),
        ):
            label = BodyLabel(t(key, text), self.widget)
            label.setWordWrap(True)
            self.viewLayout.addWidget(label)
        self._chk_never_show = CheckBox(t('disclaimer.never_show', "Don't show this again"), self.widget)
        self.viewLayout.addWidget(self._chk_never_show)
        self.yesButton.setText(t('disclaimer.confirm', 'I understand'))
        self.cancelButton.hide()

    def never_show_again(self) -> bool:
        return self._chk_never_show.isChecked()


LANGUAGES = (
    ('en', 'English', 'Hello, choose your language'),
    ('ar', 'العربية', 'مرحبًا، اختر لغتك'),
    ('es', 'Español', 'Hola, elige tu idioma'),
    ('ru', 'Русский', 'Привет, выберите язык'),
    ('de', 'Deutsch', 'Hallo, wähle deine Sprache'),
)

FEATURES = (
    ('minerva_enabled', 'nav.minerva', FluentIcon.APPLICATION, 'Minerva ROM Index', 'ROM browsing and sync on the Home section'),
    ('pc_games_enabled', 'nav.pc_games', FluentIcon.GAME, 'Repacks (PC Games)', 'A PC Games section in the sidebar'),
    ('youtube_enabled', 'nav.youtube', FluentIcon.PLAY, 'YouTube Downloader', 'A YouTube Downloader section in the sidebar'),
    ('tv_enabled', 'nav.tv', FluentIcon.TILES, 'TV', 'Movies, series, sports and channels'),
    ('tbhb_enabled', 'nav.tbhb', FluentIcon.SEARCH, 'The Black Hole Bay', 'Torrent search section in the sidebar'),
    ('sailseek_enabled', 'nav.sailseek', FluentIcon.SEARCH, 'SailSeek', 'Instant search and YouTube links from one shortcut'),
    ('music_enabled', 'nav.music', FluentIcon.MUSIC, 'Music', 'Placeholder, coming in a future update'),
    ('books_enabled', 'nav.books', FluentIcon.BOOK_SHELF, 'Books', 'Placeholder, coming in a future update'),
)

_PLAIN_THEMES = ('dark', 'light')
_SWATCH_KEYS = ('page_bg', 'primary_text', 'accent')


def _mix(a: QColor, b: QColor, t: float) -> QColor:
    return QColor(*(round(x + (y - x) * t) for x, y in ((a.red(), b.red()), (a.green(), b.green()), (a.blue(), b.blue()), (a.alpha(), b.alpha()))))


def _fade_in(widget: QWidget, ms: int = 240) -> None:
    effect = QGraphicsOpacityEffect(widget)
    widget.setGraphicsEffect(effect)
    anim = QPropertyAnimation(effect, b'opacity', widget)
    anim.setDuration(ms)
    anim.setStartValue(0.0)
    anim.setEndValue(1.0)
    anim.setEasingCurve(QEasingCurve.Type.OutCubic)
    anim.finished.connect(lambda: widget.setGraphicsEffect(None))
    anim.start(QPropertyAnimation.DeletionPolicy.DeleteWhenStopped)


def _animated_property(owner: QWidget, name: bytes, ms: int, curve=QEasingCurve.Type.OutCubic) -> QPropertyAnimation:
    anim = QPropertyAnimation(owner, name, owner)
    anim.setDuration(ms)
    anim.setEasingCurve(curve)
    return anim


def _glide(anim: QPropertyAnimation, end: float) -> None:
    anim.stop()
    anim.setStartValue(anim.targetObject().property(anim.propertyName().data().decode()))
    anim.setEndValue(end)
    anim.start()


class _ProgressBar(QWidget):

    def __init__(self, steps: int, parent=None):
        super().__init__(parent)
        self.setFixedHeight(4)
        self._steps = steps
        self._value = 1 / steps
        self._anim = _animated_property(self, b'value', 360, QEasingCurve.Type.InOutCubic)

    def _get_value(self) -> float:
        return self._value

    def _set_value(self, value: float) -> None:
        self._value = value
        self.update()

    value = Property(float, _get_value, _set_value)

    def go_to(self, step: int) -> None:
        _glide(self._anim, (step + 1) / self._steps)

    def paintEvent(self, _event) -> None:
        c = palette()
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(c['card_border']))
        p.drawRoundedRect(self.rect(), 2, 2)
        p.setBrush(QColor(c['accent']))
        p.drawRoundedRect(QRectF(0, 0, self.width() * self._value, self.height()), 2, 2)


class _Greeting(QWidget):

    def __init__(self, parent=None):
        super().__init__(parent)
        font = QFont(self.font())
        font.setPixelSize(22)
        font.setWeight(QFont.Weight.DemiBold)
        self.setFont(font)
        self.setFixedHeight(QFontMetrics(font).lineSpacing() * 2 + 8)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self._text = self._target = LANGUAGES[0][2]
        self._opacity = 1.0
        self._index = 0
        self._locked = False
        self._fading_out = False
        self._fade = _animated_property(self, b'textOpacity', 190, QEasingCurve.Type.Linear)
        self._fade.finished.connect(self._on_fade_done)
        self._timer = QTimer(self)
        self._timer.setInterval(2400)
        self._timer.timeout.connect(self._cycle)
        register_theme_refresh(self, self.update)

    def _get_opacity(self) -> float:
        return self._opacity

    def _set_opacity(self, value: float) -> None:
        self._opacity = value
        self.update()

    textOpacity = Property(float, _get_opacity, _set_opacity)

    def lock_to(self, code: str) -> None:
        self._locked = True
        self._timer.stop()
        self._show(next(greeting for lang, _name, greeting in LANGUAGES if lang == code))

    def _cycle(self) -> None:
        self._index = (self._index + 1) % len(LANGUAGES)
        self._show(LANGUAGES[self._index][2])

    def _show(self, text: str) -> None:
        self._target = text
        if self._fade.state() != QPropertyAnimation.State.Running:
            self._run_fade(out=True)

    def _run_fade(self, out: bool) -> None:
        self._fading_out = out
        self._fade.setStartValue(self._opacity)
        self._fade.setEndValue(0.0 if out else 1.0)
        self._fade.start()

    def _on_fade_done(self) -> None:
        if self._fading_out:
            self._text = self._target
            self._run_fade(out=False)
        elif self._text != self._target:
            self._run_fade(out=True)

    def paintEvent(self, _event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.TextAntialiasing)
        p.setOpacity(self._opacity)
        p.setPen(QColor(palette()['primary_text']))
        p.setFont(self.font())
        p.drawText(self.rect(), int(Qt.AlignmentFlag.AlignCenter | Qt.TextFlag.TextWordWrap), self._text)

    def showEvent(self, event) -> None:
        super().showEvent(event)
        if not self._locked:
            self._timer.start()

    def hideEvent(self, event) -> None:
        super().hideEvent(event)
        self._timer.stop()


class _SelectableCard(QFrame):

    clicked = Signal()

    def __init__(self, parent=None, radius: int = 10):
        super().__init__(parent)
        self._radius = radius
        self._selection = 0.0
        self._hover = 0.0
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.TabFocus)
        self._selection_anim = _animated_property(self, b'selection', 180)
        self._hover_anim = _animated_property(self, b'hover', 140)

    def _get_selection(self) -> float:
        return self._selection

    def _set_selection(self, value: float) -> None:
        self._selection = value
        self.update()

    def _get_hover(self) -> float:
        return self._hover

    def _set_hover(self, value: float) -> None:
        self._hover = value
        self.update()

    selection = Property(float, _get_selection, _set_selection)
    hover = Property(float, _get_hover, _set_hover)

    def set_selected(self, selected: bool, animate: bool = True) -> None:
        if animate and self.isVisible():
            _glide(self._selection_anim, 1.0 if selected else 0.0)
        else:
            self._set_selection(1.0 if selected else 0.0)

    def enterEvent(self, event) -> None:
        _glide(self._hover_anim, 1.0)
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:
        _glide(self._hover_anim, 0.0)
        super().leaveEvent(event)

    def mouseReleaseEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton and self.rect().contains(event.position().toPoint()):
            self.clicked.emit()
        super().mouseReleaseEvent(event)

    def keyPressEvent(self, event) -> None:
        if event.key() in (Qt.Key.Key_Space, Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self.clicked.emit()
        else:
            super().keyPressEvent(event)

    def paintEvent(self, _event) -> None:
        c = palette()
        accent = QColor(c['accent'])
        tint = QColor(accent)
        tint.setAlphaF(0.12 * self._selection + 0.05 * self._hover * (1 - self._selection))
        rect = QRectF(self.rect()).adjusted(1, 1, -1, -1)
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(c['card_bg']))
        p.drawRoundedRect(rect, self._radius, self._radius)
        p.setBrush(tint)
        p.drawRoundedRect(rect, self._radius, self._radius)
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.setPen(QPen(_mix(QColor(c['card_border']), accent, max(self._selection, self._hover * 0.5)), 1 + self._selection))
        p.drawRoundedRect(rect, self._radius, self._radius)
        if self.hasFocus():
            p.setPen(QPen(accent, 1, Qt.PenStyle.DashLine))
            p.drawRoundedRect(rect.adjusted(3, 3, -3, -3), self._radius - 2, self._radius - 2)

    def draw_check(self, p: QPainter, center: QPointF) -> None:
        if self._selection <= 0.01:
            return
        p.save()
        p.setOpacity(self._selection)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(palette()['accent']))
        p.drawEllipse(center, 9, 9)
        tick = QPainterPath(QPointF(center.x() - 4, center.y()))
        tick.lineTo(center.x() - 1, center.y() + 3)
        tick.lineTo(center.x() + 4, center.y() - 3)
        p.setPen(QPen(QColor('white'), 2, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawPath(tick)
        p.restore()

    def paint_overlay(self) -> QPainter:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        return p


class _LanguageCard(_SelectableCard):
    def __init__(self, native_name: str, parent=None):
        super().__init__(parent)
        self.setFixedHeight(52)
        row = QHBoxLayout(self)
        row.setContentsMargins(40, 0, 40, 0)
        label = StrongBodyLabel(native_name, self)
        label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        row.addWidget(label, 1, Qt.AlignmentFlag.AlignCenter)

    def paintEvent(self, event) -> None:
        super().paintEvent(event)
        self.draw_check(self.paint_overlay(), QPointF(self.width() - 24, self.height() / 2))


_VIDEO_EXT = frozenset(('.mp4', '.webm', '.mkv', '.mov', '.avi', '.m4v'))
_PREVIEW_PX = QSize(288, 152)
_THUMBS: dict[str, QPixmap] = {}
_THUMB_BADGE = {'gif': 'GIF', 'video': 'VIDEO', 'shader': 'SHADER', 'photo': 'PHOTO'}


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


class _ThemeCard(_SelectableCard):

    def __init__(self, label: str, swatch: tuple, media: str = '', kind: str = '', parent=None):
        super().__init__(parent, radius=12)
        self._label = label
        self._bg, self._text, self._accent = (QColor(c) for c in swatch)
        self._media = media
        self._kind = kind
        self.setFixedSize(160, 122)
        if media and kind != 'video':
            loader = _ThumbLoader.get()
            loader.ready.connect(self._on_thumb)
            loader.request(media)

    def media(self) -> str:
        return self._media

    def kind(self) -> str:
        return self._kind

    def _on_thumb(self, path: str) -> None:
        if path == self._media:
            self.update()

    def _paint_mock(self, p: QPainter, r: QRectF) -> None:
        p.fillRect(r, self._bg)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(self._accent)
        p.drawRoundedRect(QRectF(r.left() + 8, r.top() + 8, 26, 6), 3, 3)
        p.drawRoundedRect(QRectF(r.right() - 30, r.bottom() - 16, 22, 9), 4.5, 4.5)
        p.setBrush(_mix(self._bg, self._text, 0.55))
        p.drawRoundedRect(QRectF(r.left() + 8, r.top() + 22, r.width() - 16, 4), 2, 2)
        p.drawRoundedRect(QRectF(r.left() + 8, r.top() + 32, r.width() * 0.5, 4), 2, 2)

    def _paint_play(self, p: QPainter, r: QRectF) -> None:
        c = r.center()
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(0, 0, 0, 120))
        p.drawEllipse(c, 14, 14)
        tri = QPainterPath(QPointF(c.x() - 4, c.y() - 7))
        tri.lineTo(c.x() + 7, c.y())
        tri.lineTo(c.x() - 4, c.y() + 7)
        tri.closeSubpath()
        p.setBrush(QColor('white'))
        p.drawPath(tri)

    def _paint_badge(self, p: QPainter, r: QRectF) -> None:
        text = _THUMB_BADGE.get(self._kind)
        if not text:
            return
        font = QFont(self.font())
        font.setPixelSize(9)
        font.setBold(True)
        p.setFont(font)
        width = QFontMetrics(font).horizontalAdvance(text) + 12
        pill = QRectF(r.left() + 6, r.bottom() - 20, width, 15)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(0, 0, 0, 150))
        p.drawRoundedRect(pill, 7.5, 7.5)
        p.setPen(QColor('white'))
        p.drawText(pill, Qt.AlignmentFlag.AlignCenter, text)

    def paintEvent(self, event) -> None:
        super().paintEvent(event)
        p = self.paint_overlay()
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        preview = QRectF(7, 7, self.width() - 14, 76)
        clip = QPainterPath()
        clip.addRoundedRect(preview, 8, 8)
        p.save()
        p.setClipPath(clip)
        pixmap = _THUMBS.get(self._media)
        if pixmap is not None and not pixmap.isNull():
            scale = max(preview.width() / pixmap.width(), preview.height() / pixmap.height())
            w, h = preview.width() / scale, preview.height() / scale
            p.drawPixmap(preview, pixmap, QRectF((pixmap.width() - w) / 2, (pixmap.height() - h) / 2, w, h))
        else:
            self._paint_mock(p, preview)
            if self._kind == 'video':
                self._paint_play(p, preview)
        p.restore()
        p.setPen(QPen(QColor(255, 255, 255, 28), 1))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawRoundedRect(preview, 8, 8)
        self._paint_badge(p, preview)
        font = QFont(self.font())
        font.setPixelSize(12)
        p.setFont(font)
        p.setPen(QColor(palette()['primary_text']))
        text = QFontMetrics(font).elidedText(self._label, Qt.TextElideMode.ElideRight, self.width() - 20)
        p.drawText(QRectF(10, 86, self.width() - 20, 30), Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, text)
        self.draw_check(p, QPointF(self.width() - 22, 22))


class _FeatureCard(_SelectableCard):

    def __init__(self, icon, title: str, description: str, parent=None):
        super().__init__(parent)
        self.setMinimumHeight(66)
        row = QHBoxLayout(self)
        row.setContentsMargins(14, 10, 12, 10)
        row.setSpacing(12)
        icon_widget = IconWidget(icon, self)
        icon_widget.setFixedSize(20, 20)
        icon_widget.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        row.addWidget(icon_widget, 0, Qt.AlignmentFlag.AlignVCenter)
        text = QVBoxLayout()
        text.setSpacing(1)
        description_label = CaptionLabel(description, self)
        description_label.setWordWrap(True)
        self.title_label = StrongBodyLabel(title, self)
        self.description_label = description_label
        for label in (self.title_label, description_label):
            label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
            text.addWidget(label)
        row.addLayout(text, 1)
        self.switch = SwitchButton(self)
        self.switch.setOnText('')
        self.switch.setOffText('')
        self.switch.setAttribute(Qt.WidgetAttribute.WA_NoMousePropagation)
        self.switch.checkedChanged.connect(self.set_selected)
        self.clicked.connect(lambda: self.switch.setChecked(not self.switch.isChecked()))
        row.addWidget(self.switch, 0, Qt.AlignmentFlag.AlignVCenter)


class _VolumeCard(_SelectableCard):

    changed = Signal(int)

    def __init__(self, icon, value: int, off_text, parent=None):
        super().__init__(parent)
        self.setCursor(Qt.CursorShape.ArrowCursor)
        self.setMinimumHeight(66)
        self._off_text = off_text
        row = QHBoxLayout(self)
        row.setContentsMargins(14, 10, 14, 10)
        row.setSpacing(12)
        icon_widget = IconWidget(icon, self)
        icon_widget.setFixedSize(20, 20)
        row.addWidget(icon_widget, 0, Qt.AlignmentFlag.AlignVCenter)
        text = QVBoxLayout()
        text.setSpacing(1)
        self.title_label = StrongBodyLabel('', self)
        self.description_label = CaptionLabel('', self)
        self.description_label.setWordWrap(True)
        text.addWidget(self.title_label)
        text.addWidget(self.description_label)
        row.addLayout(text, 1)
        self.slider = Slider(Qt.Orientation.Horizontal, self)
        self.slider.setRange(0, 100)
        self.slider.setFixedWidth(150)
        self.slider.setValue(int(value))
        self._value_label = CaptionLabel(self)
        self._value_label.setFixedWidth(34)
        self._value_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.slider.valueChanged.connect(self._on_value)
        row.addWidget(self.slider, 0, Qt.AlignmentFlag.AlignVCenter)
        row.addWidget(self._value_label, 0, Qt.AlignmentFlag.AlignVCenter)
        self.refresh_label()

    def value(self) -> int:
        return self.slider.value()

    def refresh_label(self) -> None:
        v = self.slider.value()
        self._value_label.setText(str(v) if v > 0 else self._off_text())

    def _on_value(self, v: int) -> None:
        self.refresh_label()
        self.changed.emit(v)


def _set_card_texts(card, title: str, description: str) -> None:
    card.title_label.setText(title)
    card.description_label.setText(description)


class _GutterScrollArea(ScrollArea):

    _GUTTER = 14

    def __init__(self):
        super().__init__()
        self._gutter = False

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        content = self.widget()
        layout = content.layout() if content is not None else None
        if layout is None:
            return
        need = content.minimumSizeHint().height() > self.viewport().height()
        if need != self._gutter:
            self._gutter = need
            m = layout.contentsMargins()
            layout.setContentsMargins(m.left(), m.top(), m.right() + (self._GUTTER if need else -self._GUTTER), m.bottom())


def _scrolling(content: QWidget, max_height: int) -> ScrollArea:
    area = _GutterScrollArea()
    area.setWidgetResizable(True)
    area.setFrameShape(QFrame.Shape.NoFrame)
    area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
    area.setMaximumHeight(max_height)
    area.setWidget(content)
    area.enableTransparentBackground()
    return area


def _theme_options() -> list[tuple[str, str, tuple[str, str, str], str, str]]:
    options = [
        (mode, tr(f'settings.theme_{mode}'), tuple(palette(dark=mode == 'dark')[k] for k in _SWATCH_KEYS), '', '')
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
        options.append((preset_id, preset_label(preset_id), tuple(meta.palette[k] for k in _SWATCH_KEYS), media, kind))
    return options


def _activate_option(option_id: str) -> bool:
    plain = option_id in _PLAIN_THEMES
    if not plain and preset_meta(option_id) is None:
        return False
    activate_theme(None if plain else option_id, dark=(option_id == 'dark') if plain else None)
    return True


def _theme_grid(on_pick, max_height: int) -> tuple[ScrollArea, dict[str, _ThemeCard]]:
    content = QWidget()
    grid = QGridLayout(content)
    grid.setContentsMargins(2, 2, 2, 2)
    grid.setSpacing(10)
    cards: dict[str, _ThemeCard] = {}
    columns = 4
    for i, (option_id, label, swatch, media, kind) in enumerate(_theme_options()):
        card = _ThemeCard(label, swatch, media, kind, content)
        card.clicked.connect(lambda o=option_id: on_pick(o))
        grid.addWidget(card, i // columns, i % columns)
        cards[option_id] = card
    grid.setColumnStretch(columns, 1)
    return _scrolling(content, max_height), cards


class ThemePickerDialog(MessageBoxBase):

    def __init__(self, owner: QWidget, current: str):
        super().__init__(owner)
        self.theme = current
        self.widget.setMinimumWidth(max(420, min(720, owner.width() - 48)))
        title = SubtitleLabel(tr('onboarding.theme_title'), self.widget)
        subtitle = BodyLabel(tr('onboarding.theme_subtitle'), self.widget)
        subtitle.setWordWrap(True)
        grid, self._cards = _theme_grid(self._on_pick, 372)
        grid.setFixedHeight(372)
        for widget in (title, subtitle, grid):
            self.viewLayout.addWidget(widget)
        self.viewLayout.setSpacing(8)
        if current in self._cards:
            self._cards[current].set_selected(True, animate=False)
        self.yesButton.setText(tr('onboarding.theme_continue'))
        self.cancelButton.setText(owner.tr_or('common.cancel', 'Cancel'))
        register_theme_refresh(self, self._repaint_cards)

    def _on_pick(self, option_id: str) -> None:
        if option_id == self.theme or option_id not in self._cards:
            return
        self.theme = option_id
        for oid, card in self._cards.items():
            card.set_selected(oid == option_id)

    def _repaint_cards(self) -> None:
        for card in self._cards.values():
            card.update()

def pick_theme(owner: QWidget, current: str) -> str | None:
    dialog = ThemePickerDialog(owner, current)
    try:
        if not dialog.exec() or dialog.theme == current:
            return None
        return dialog.theme
    finally:
        dialog.hide()
        dialog.setParent(None)
        dialog.deleteLater()


class OnboardingWizard(MessageBoxBase):

    _NEXT_KEYS = ('onboarding.language_continue', 'onboarding.theme_continue')

    _TEXT = {
        'onboarding.next': 'Next',
        'onboarding.finish': 'Finish',
        'onboarding.downloads_title': 'Where should downloads go?',
        'onboarding.downloads_subtitle': 'Pick one folder and PiraChest sets up every section inside it. You can change any of them later in Settings.',
        'onboarding.downloads_browse': 'Browse',
        'onboarding.downloads_reset': 'Use default',
        'onboarding.downloads_subfolders': 'Organize by section',
        'onboarding.downloads_subfolders_desc': 'Repacks, Music, YouTube and others each get their own subfolder',
        'onboarding.downloads_free': 'free on this drive',
        'onboarding.downloads_preview': 'Folders that will be used',
        'onboarding.downloads_pick': 'Select download folder',
        'onboarding.downloads_error': "Can't use this folder:",
        'onboarding.prefs_title': 'A few preferences',
        'onboarding.prefs_subtitle': 'All optional, and all changeable later in Settings.',
        'onboarding.pref_startup': 'Launch at Windows startup',
        'onboarding.pref_startup_desc': 'Start PiraChest when you sign in',
        'onboarding.pref_tray': 'Keep running in the tray',
        'onboarding.pref_tray_desc': 'Closing the window minimizes it instead of quitting',
        'onboarding.pref_notify': 'Download notifications',
        'onboarding.pref_notify_desc': 'Notify me when a download finishes',
        'onboarding.pref_sfx': 'Button sounds',
        'onboarding.pref_sfx_desc': 'Hover, click and navigation sounds. 0 turns them off',
        'onboarding.pref_ambiance': 'Ambiance',
        'onboarding.pref_ambiance_desc': 'A quiet looping background track. 0 turns it off',
        'onboarding.pref_assoc': 'Open torrent files and magnet links',
        'onboarding.pref_assoc_desc': 'Use PiraChest for .torrent files and magnet: links',
    }

    def setShadowEffect(self, *args, **kwargs) -> None:
        self.widget.setGraphicsEffect(None)

    def showEvent(self, event) -> None:
        QDialog.showEvent(self, event)
        self.setWindowOpacity(0.0)
        anim = QPropertyAnimation(self, b'windowOpacity', self)
        anim.setDuration(160)
        anim.setStartValue(0.0)
        anim.setEndValue(1.0)
        anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        anim.finished.connect(lambda: self.setWindowOpacity(1.0))
        anim.start(QAbstractAnimation.DeletionPolicy.DeleteWhenStopped)
        self._show_anim = anim
        QTimer.singleShot(200, self, lambda: self.setWindowOpacity(1.0))

    def done(self, code) -> None:
        self.widget.setGraphicsEffect(None)
        self.setWindowOpacity(1.0)
        QDialog.done(self, code)

    def __init__(self, owner: QWidget):
        super().__init__(owner)
        self._owner = owner
        self._index = 0
        self._language = current_language() if current_language() in {code for code, *_ in LANGUAGES} else 'en'
        self._theme = 'dark'
        self._download_root: str | None = None
        self.widget.setMinimumWidth(max(420, min(720, owner.width() - 48)))
        self.buttonLayout.removeWidget(self.cancelButton)
        self.buttonLayout.insertWidget(0, self.cancelButton, 1, Qt.AlignmentFlag.AlignVCenter)

        self._pages = [self._build_language_page(), self._build_theme_page(), self._build_features_page(),
                       self._build_downloads_page(), self._build_prefs_page()]
        self._bar = _ProgressBar(len(self._pages))
        self._stack = QStackedWidget()
        for page in self._pages:
            self._stack.addWidget(page)
        self.viewLayout.addWidget(self._bar)
        self.viewLayout.addSpacing(18)
        self.viewLayout.addWidget(self._stack)

        register_locale_refresh(self, self._retranslate)
        register_theme_refresh(self, self._repaint_cards)
        self._retranslate()


    @staticmethod
    def _heading_page() -> tuple[QWidget, QVBoxLayout, SubtitleLabel, BodyLabel]:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)
        title = SubtitleLabel(page)
        subtitle = BodyLabel(page)
        subtitle.setWordWrap(True)
        layout.addWidget(title)
        layout.addWidget(subtitle)
        layout.addSpacing(10)
        return page, layout, title, subtitle

    def _build_language_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        self._greeting = _Greeting(page)
        layout.addWidget(self._greeting)
        layout.addSpacing(12)
        grid = QGridLayout()
        grid.setSpacing(10)
        self._language_cards: dict[str, _LanguageCard] = {}
        for i, (code, native_name, _greeting) in enumerate(LANGUAGES):
            card = _LanguageCard(native_name, page)
            card.clicked.connect(lambda c=code: self._on_language(c))
            grid.addWidget(card, i // 2, i % 2)
            self._language_cards[code] = card
        layout.addLayout(grid)
        layout.addStretch(1)
        self._language_cards[self._language].set_selected(True, animate=False)
        return page

    def _build_theme_page(self) -> QWidget:
        page, layout, self._theme_title, self._theme_subtitle = self._heading_page()
        grid, self._theme_cards = _theme_grid(self._on_theme, 340)
        layout.addWidget(grid)
        layout.addStretch(1)
        self._theme_cards[self._theme].set_selected(True, animate=False)
        return page

    def _build_features_page(self) -> QWidget:
        page, layout, self._features_title, self._features_subtitle = self._heading_page()
        grid = QGridLayout()
        grid.setSpacing(10)
        self._feature_cards: dict[str, _FeatureCard] = {}
        for i, (attr, slot, icon, title, description) in enumerate(FEATURES):
            card = _FeatureCard(themed_icon(slot, icon), title, description, page)
            card.switch.setChecked(bool(getattr(settings, attr, False)))
            grid.addWidget(card, i // 2, i % 2)
            self._feature_cards[attr] = card
        layout.addLayout(grid)
        layout.addStretch(1)
        return page

    def _t(self, key: str) -> str:
        return self._owner.tr_or(key, self._TEXT[key])

    def _build_downloads_page(self) -> QWidget:
        page, layout, self._downloads_title, self._downloads_subtitle = self._heading_page()
        row = QHBoxLayout()
        row.setSpacing(8)
        self._dir_edit = LineEdit(page)
        self._dir_edit.setReadOnly(True)
        self._dir_edit.setText(settings.download_dir)
        self._browse_btn = PushButton(page)
        self._browse_btn.clicked.connect(self._browse_downloads)
        self._reset_btn = PushButton(page)
        self._reset_btn.clicked.connect(self._reset_downloads)
        row.addWidget(self._dir_edit, 1)
        row.addWidget(self._browse_btn)
        row.addWidget(self._reset_btn)
        layout.addLayout(row)
        self._dir_info = CaptionLabel(page)
        self._dir_info.setWordWrap(True)
        layout.addWidget(self._dir_info)
        layout.addSpacing(8)
        self._subfolders_card = _FeatureCard(FluentIcon.FOLDER, '', '', page)
        self._subfolders_card.switch.setChecked(True)
        layout.addWidget(self._subfolders_card)
        layout.addSpacing(6)
        self._preview_title = StrongBodyLabel(page)
        layout.addWidget(self._preview_title)
        self._preview_rows: list[CaptionLabel] = []
        for _ in range(len(download_dir_changes('x')) - 1):
            row_label = CaptionLabel(page)
            row_label.setContentsMargins(10, 0, 0, 0)
            row_label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
            layout.addWidget(row_label)
            self._preview_rows.append(row_label)
        layout.addStretch(1)
        self._subfolders_card.switch.checkedChanged.connect(lambda _checked: self._refresh_preview())
        self._refresh_dir_info()
        self._refresh_preview()
        return page

    def _build_prefs_page(self) -> QWidget:
        page, layout, self._prefs_title, self._prefs_subtitle = self._heading_page()
        content = QWidget()
        grid = QGridLayout(content)
        grid.setContentsMargins(2, 2, 2, 2)
        grid.setSpacing(10)
        win = sys.platform == 'win32'
        specs = (
            ('startup', FluentIcon.POWER_BUTTON, get_launch_at_startup(), win),
            ('tray', FluentIcon.MINIMIZE, settings.close_to_tray, True),
            ('notify', FluentIcon.RINGER, settings.notify_on_download_complete, True),
            ('assoc', FluentIcon.LINK, associations_available() and not settings.associations_user_disabled, associations_available()),
        )
        self._pref_cards: dict[str, _FeatureCard] = {}
        for pref_id, icon, value, shown in specs:
            if not shown:
                continue
            card = _FeatureCard(icon, '', '', content)
            card.switch.setChecked(bool(value))
            n = len(self._pref_cards)
            grid.addWidget(card, n // 2, n % 2)
            self._pref_cards[pref_id] = card
        off = lambda: self._owner.tr_or('settings.volume_off', 'Off')
        self._volume_cards: dict[str, _VolumeCard] = {}
        row = (len(self._pref_cards) + 1) // 2
        for vol_id, icon, value, setter in (
                ('sfx', FluentIcon.VOLUME, settings.sfx_volume, lambda v: get_sound_manager().set_sfx_volume(v)),
                ('ambiance', FluentIcon.MUSIC, settings.ambiance_volume, lambda v: get_sound_manager().set_ambiance_volume(v))):
            card = _VolumeCard(icon, value, off, content)
            card.changed.connect(setter)
            grid.addWidget(card, row, 0, 1, 2)
            row += 1
            self._volume_cards[vol_id] = card
        for r in range(row):
            grid.setRowStretch(r, 1)
        layout.addWidget(_scrolling(content, 460), 1)
        return page


    def _refresh_dir_info(self, error: str = '') -> None:
        if error:
            self._dir_info.setText(f"{self._t('onboarding.downloads_error')} {error}")
            return
        free = free_space_bytes(self._download_root or settings.download_dir)
        self._dir_info.setText(f"{format_size(free)} {self._t('onboarding.downloads_free')}" if free is not None else '')

    def _refresh_preview(self) -> None:
        root = self._download_root or settings.download_dir
        folders = list(dict.fromkeys(download_dir_changes(root, self._subfolders_card.switch.isChecked()).values()))
        for i, row_label in enumerate(self._preview_rows):
            row_label.setVisible(i < len(folders))
            if i < len(folders):
                row_label.setText(folders[i])

    def _browse_downloads(self) -> None:
        start = self._download_root or settings.download_dir
        chosen = QFileDialog.getExistingDirectory(self, self._t('onboarding.downloads_pick'), start)
        if not chosen:
            return
        chosen = os.path.normpath(chosen)
        problem = check_dir_writable(chosen)
        if problem:
            self._refresh_dir_info(problem)
            return
        self._download_root = chosen
        self._dir_edit.setText(chosen)
        self._refresh_dir_info()
        self._refresh_preview()

    def _reset_downloads(self) -> None:
        self._download_root = None
        self._dir_edit.setText(settings.download_dir)
        self._refresh_dir_info()
        self._refresh_preview()


    def _on_language(self, code: str) -> None:
        if code == self._language:
            return
        self._language = code
        for lang, card in self._language_cards.items():
            card.set_selected(lang == code)
        self._greeting.lock_to(code)
        set_language(code)

    def _on_theme(self, option_id: str) -> None:
        if option_id == self._theme:
            return
        if not _activate_option(option_id):
            return
        self._theme = option_id
        for oid, card in self._theme_cards.items():
            card.set_selected(oid == option_id)

    def _go(self, index: int) -> None:
        self._index = index
        self._stack.setCurrentIndex(index)
        _fade_in(self._pages[index])
        self._bar.go_to(index)
        self._sync_buttons()

    def _sync_buttons(self) -> None:
        last = self._index == len(self._pages) - 1
        if self._index < len(self._NEXT_KEYS):
            self.yesButton.setText(tr(self._NEXT_KEYS[self._index]))
        else:
            self.yesButton.setText(self._t('onboarding.finish' if last else 'onboarding.next'))
        self.cancelButton.setText(self._owner.tr_or('onboarding.back', 'Back'))
        self.cancelButton.setVisible(self._index > 0)

    def _retranslate(self) -> None:
        self._theme_title.setText(tr('onboarding.theme_title'))
        self._theme_subtitle.setText(tr('onboarding.theme_subtitle'))
        self._features_title.setText(tr('onboarding.features_title'))
        self._features_subtitle.setText(tr('onboarding.features_subtitle'))
        self._downloads_title.setText(self._t('onboarding.downloads_title'))
        self._downloads_subtitle.setText(self._t('onboarding.downloads_subtitle'))
        self._browse_btn.setText(self._t('onboarding.downloads_browse'))
        self._reset_btn.setText(self._t('onboarding.downloads_reset'))
        _set_card_texts(self._subfolders_card, self._t('onboarding.downloads_subfolders'), self._t('onboarding.downloads_subfolders_desc'))
        self._refresh_dir_info()
        self._preview_title.setText(self._t('onboarding.downloads_preview'))
        self._prefs_title.setText(self._t('onboarding.prefs_title'))
        self._prefs_subtitle.setText(self._t('onboarding.prefs_subtitle'))
        for pref_id, card in (*self._pref_cards.items(), *self._volume_cards.items()):
            _set_card_texts(card, self._t(f'onboarding.pref_{pref_id}'), self._t(f'onboarding.pref_{pref_id}_desc'))
        for card in self._volume_cards.values():
            card.refresh_label()
        self._sync_buttons()

    def _repaint_cards(self) -> None:
        for widget in (*self.findChildren(_SelectableCard), self._bar):
            widget.update()

    def validate(self) -> bool:
        if self._index < len(self._pages) - 1:
            self._go(self._index + 1)
            return False
        return True

    def reject(self) -> None:
        if self._index > 0:
            self._go(self._index - 1)

    def commit(self) -> None:
        changes = {'language': self._language, 'onboarding_completed': True}
        if self._theme in _PLAIN_THEMES:
            changes.update(theme_mode=ThemeMode.DARK if self._theme == 'dark' else ThemeMode.LIGHT, theme_preset=None)
            fonts = None
        else:
            changes['theme_preset'] = self._theme
            fonts = resolve_font_families(preset_meta(self._theme))
        changes.update({attr: card.switch.isChecked() for attr, card in self._feature_cards.items()})
        if self._download_root:
            changes.update(download_dir_changes(self._download_root, self._subfolders_card.switch.isChecked()))
        on = {pref_id: card.switch.isChecked() for pref_id, card in self._pref_cards.items()}
        changes.update(close_to_tray=on['tray'], notify_on_download_complete=on['notify'],
                       sfx_volume=self._volume_cards['sfx'].value(), ambiance_volume=self._volume_cards['ambiance'].value())
        if 'startup' in on and on['startup'] != get_launch_at_startup() and set_launch_at_startup(on['startup']):
            changes['launch_at_startup'] = on['startup']
        register = False
        if 'assoc' in on:
            if on['assoc']:
                register = True
            elif not settings.associations_enabled:
                changes['associations_user_disabled'] = True
        apply_settings(**changes)
        with suppress(Exception):
            apply_theme_font_families(fonts)
        save_settings(settings)
        if register:
            with suppress(Exception):
                register_associations()


def run_onboarding(owner: QWidget) -> None:
    wizard = OnboardingWizard(owner)
    try:
        wizard.exec()
        wizard.commit()
    finally:
        wizard.hide()
        wizard.setParent(None)
        wizard.deleteLater()


def _maybe_run_onboarding(parent: MainWindow) -> None:
    if not settings.onboarding_completed:
        _trim_working_set_now()
        run_onboarding(parent)
        parent._sync_optional_pages()
        with suppress(Exception):
            parent.sailseek.apply_settings()
        if parent.settings_page is not None:
            parent.settings_page._populate()
        _trim_idle_working_set(600)
    else:
        _trim_idle_working_set(1800)
    _maybe_show_vpn_disclaimer(parent)
    QTimer.singleShot(300, parent._check_update_on_startup)
    QTimer.singleShot(1500, parent, parent._start_page_preload)


def _maybe_show_vpn_disclaimer(parent: MainWindow) -> None:
    if settings.hide_vpn_disclaimer:
        return
    dialog = VPNDisclaimerDialog(parent)
    dialog.exec()
    if dialog.never_show_again():
        settings.hide_vpn_disclaimer = True
        with suppress(Exception):
            save_settings(settings)

_PAGE_MODULES = (
    ('pc_games_page', 'src.gui.repacks_page'),
    ('tbhb_page', 'src.gui.tbhb_page'),
    ('music_page', 'src.gui.music_page'),
    ('books_page', 'src.gui.books_page'),
    ('yt_page', 'src.gui.yt_page'),
    ('tv_page', 'src.gui.tv_page'),
)


def _lazy_repacks(s):
    def build():
        from .repacks_page import RepacksPage
        return RepacksPage(s.download_manager)
    return build


def _lazy_tbhb(s):
    def build():
        from .tbhb_page import TBHBPage
        return TBHBPage(s.download_manager)
    return build


def _make_minerva_page(s):
    from .minerva_page import MinervaPage
    return MinervaPage(s)


def _lazy_tv():
    from .tv_page import TV_page
    return TV_page()


class _ThemedSplash(SplashScreen):

    def paintEvent(self, _event) -> None:
        try:
            bg = to_color(palette().get('page_bg'), '#1c1c1c')
        except Exception:
            bg = QColor(32, 32, 32)
        bg.setAlpha(255)
        p = QPainter(self)
        p.fillRect(self.rect(), bg)
        p.end()


def _send_to_torrent_client(target):
    from .repacks_page import send_to_external_torrent_client
    return send_to_external_torrent_client(target)


_KB_DECORATIVE = {'Indicator', 'Knob', 'Handle', 'Thumb', 'Dot', 'Tick'}


class _KeyboardFocusRing(QWidget):
    def __init__(self, host):
        super().__init__(host)
        self._host = host
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.setAttribute(Qt.WidgetAttribute.WA_NoMousePropagation, True)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground, True)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.hide()

    def paintEvent(self, event):
        with suppress(Exception):
            try:
                color = QColor(themeColor())
            except Exception:
                color = QColor('#00b7c3')
            color.setAlpha(235)
            radius = 8.0
            try:
                radius = float(self._host.getBorderRadius())
            except Exception:
                radius = 8.0
            painter = QPainter(self)
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            fill = QColor(color)
            fill.setAlpha(34)
            radius = 8.0
            try:
                radius = float(self._host.getBorderRadius())
            except Exception:
                radius = 8.0
            path = QPainterPath()
            path.addRoundedRect(QRectF(1.0, 1.0, self.width() - 2.0, self.height() - 2.0), radius, radius)
            painter.fillPath(path, fill)
            pen = QPen(color, 2)
            pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
            painter.setPen(pen)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawPath(path)
            painter.end()
        super().paintEvent(event)


def _trim_idle_working_set(delay_ms: int = 900) -> None:
    if sys.platform != 'win32':
        return
    with suppress(Exception):
        QTimer.singleShot(int(delay_ms), _trim_working_set_now)


def _trim_working_set_now() -> None:
    if sys.platform != 'win32':
        return
    with suppress(Exception):
        psapi = ctypes.WinDLL('psapi')
        kernel32 = ctypes.WinDLL('kernel32')
        kernel32.GetCurrentProcess.restype = ctypes.c_void_p
        psapi.EmptyWorkingSet.argtypes = [ctypes.c_void_p]
        psapi.EmptyWorkingSet.restype = ctypes.c_int
        psapi.EmptyWorkingSet(kernel32.GetCurrentProcess())


@dataclass(frozen=True)
class _DonateCoin:
    key: str
    ticker: str
    name: str
    network: str
    address: str
    stablecoin: bool = False


_EVM_ADDRESS = '0xD8e4d1408aF248B97C2cdCe85afbeD83e61Cc3f8'

_DONATE_COINS: tuple[_DonateCoin, ...] = (
    _DonateCoin('btc', 'BTC', 'Bitcoin', 'Bitcoin network', 'bc1qkghdq757u9lwntjr3029ljg3phwv9yffj926vv'),
    _DonateCoin('eth', 'ETH', 'Ethereum', 'Ethereum network', _EVM_ADDRESS),
    _DonateCoin('ltc', 'LTC', 'Litecoin', 'Litecoin network', 'Lb3qSpBgc8Bb1xAnBKpPvuCSEwFjMNeuPm'),
    _DonateCoin('sol', 'SOL', 'Solana', 'Solana network', 'Fe1JqgUTPkq45r7aSx9BYtz2nAbH6cPMSWztBJYu7Mc8'),
    _DonateCoin('usdc', 'USDC', 'USD Coin', 'Dollar stablecoin, EVM address', _EVM_ADDRESS, True),
    _DonateCoin('usdt', 'USDT', 'Tether', 'Dollar stablecoin, EVM address', _EVM_ADDRESS, True),
)

_DONATE_SELF_PAINTED = 'background: transparent; border: none;'


def _donate_t(key: str, default: str) -> str:
    try:
        value = tr(key)
    except Exception:
        return default
    return default if not value or value == key else value


def _donate_color(role: str, fallback: str = '#808080') -> QColor:
    return to_color(palette().get(role, fallback), fallback)


def _donate_px(name: str, default: int) -> int:
    with suppress(Exception):
        match = re.match(r'\s*(\d+)', str(theme_manager.layout().get(name, '')))
        if match:
            return int(match.group(1))
    return default


def _photo_path(name: str) -> str | None:
    rel = Path('photos') / name
    candidates = [Path(__file__).resolve().parent / rel]
    bundle = getattr(sys, '_MEIPASS', None)
    if bundle:
        candidates.append(Path(bundle) / 'src' / 'gui' / rel)
    return next((str(c) for c in candidates if c.is_file()), None)


def _logo_ico_path() -> str | None:
    return _photo_path('logo.ico')


def _exe_ico_path() -> str | None:
    return _photo_path('icon.ico')


_WM_SETICON = 0x0080
_ICON_SMALL, _ICON_BIG = 0, 1
_IMAGE_ICON, _LR_LOADFROMFILE = 1, 0x0010
_SM_CXICON, _SM_CYICON, _SM_CXSMICON, _SM_CYSMICON = 11, 12, 49, 50


def _load_native_icon(path: str, big: bool):
    user32 = ctypes.windll.user32
    user32.LoadImageW.restype = ctypes.c_void_p
    user32.LoadImageW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_uint,
                                  ctypes.c_int, ctypes.c_int, ctypes.c_uint]
    w = user32.GetSystemMetrics(_SM_CXICON if big else _SM_CXSMICON)
    h = user32.GetSystemMetrics(_SM_CYICON if big else _SM_CYSMICON)
    return user32.LoadImageW(None, path, _IMAGE_ICON, w, h, _LR_LOADFROMFILE)


def _donate_qr_path(key: str) -> Path | None:
    name = f'{key}-qr.png'
    candidates = [Path(__file__).resolve().parent / 'photos' / 'donations' / name]
    bundle = getattr(sys, '_MEIPASS', None)
    if bundle:
        candidates.append(Path(bundle) / 'src' / 'gui' / 'photos' / 'donations' / name)
    return next((c for c in candidates if c.is_file()), None)


def _donate_wrap(address: str) -> str:
    mid = (len(address) + 1) // 2
    return f'{address[:mid]}\n{address[mid:]}'


class _DonateWarnLabel(CaptionLabel):

    def _applyThemeColor(self) -> None:
        color = ensure_contrast(_donate_color('state_verifying'), _donate_color('dialog_bg'), 4.5)
        self.setTextColor(color, color)

    def showEvent(self, event) -> None:
        super().showEvent(event)
        self._applyThemeColor()


class _DonateQrTile(QFrame):

    IMAGE = 168
    PAD = 12

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setStyleSheet(_DONATE_SELF_PAINTED)
        side = self.IMAGE + self.PAD * 2
        self.setFixedSize(side, side)
        self._pixmap: QPixmap | None = None
        self._missing = ''

    def set_content(self, pixmap: QPixmap | None, missing_text: str) -> None:
        self._pixmap, self._missing = pixmap, missing_text
        self.update()

    def paintEvent(self, _event) -> None:
        p = QPainter(self)
        p.setRenderHints(QPainter.RenderHint.Antialiasing | QPainter.RenderHint.TextAntialiasing)
        radius = _donate_px('card_radius', 10)
        p.setBrush(QColor('#ffffff'))
        p.setPen(QPen(_donate_color('surface_border'), 1))
        p.drawRoundedRect(QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5), radius, radius)
        if self._pixmap is not None:
            p.drawPixmap(self.PAD, self.PAD, self._pixmap)
        else:
            p.setPen(QColor('#555555'))
            p.drawText(QRectF(self.rect()).adjusted(16, 16, -16, -16),
                       Qt.AlignmentFlag.AlignCenter | Qt.TextFlag.TextWordWrap, self._missing)


class _DonateAddressField(QFrame):

    clicked = Signal()

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setStyleSheet(_DONATE_SELF_PAINTED)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self._hover = False
        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 11, 14, 11)
        self._label = QLabel('')
        self._label.setLayoutDirection(Qt.LayoutDirection.LeftToRight)
        self._label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        font = QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont)
        font.setFamilies(['Cascadia Mono', 'Consolas', font.family()])
        font.setPixelSize(14)
        self._label.setFont(font)
        layout.addWidget(self._label)
        self.refresh()

    def set_address(self, address: str) -> None:
        self._label.setText(_donate_wrap(address))

    def refresh(self) -> None:
        self._label.setStyleSheet(f"color: {palette()['primary_text']}; background: transparent; border: none;")
        self.update()

    def enterEvent(self, event) -> None:
        self._hover = True
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:
        self._hover = False
        self.update()
        super().leaveEvent(event)

    def focusInEvent(self, event) -> None:
        self.update()
        super().focusInEvent(event)

    def focusOutEvent(self, event) -> None:
        self.update()
        super().focusOutEvent(event)

    def mouseReleaseEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton and self.rect().contains(event.position().toPoint()):
            self.clicked.emit()
        super().mouseReleaseEvent(event)

    def keyPressEvent(self, event) -> None:
        if event.key() in (Qt.Key.Key_Space, Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self.clicked.emit()
        else:
            super().keyPressEvent(event)

    def paintEvent(self, _event) -> None:
        focused = self.hasFocus()
        width = float(_donate_px('input_border_width_focus', 2) if focused else _donate_px('input_border_width', 1))
        border = _donate_color('input_border_focus' if focused else 'input_border_hover' if self._hover else 'input_border')
        radius = _donate_px('input_radius', 6)
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setBrush(_donate_color('input_bg'))
        p.setPen(QPen(border, width))
        p.drawRoundedRect(QRectF(self.rect()).adjusted(width / 2, width / 2, -width / 2, -width / 2), radius, radius)


class DonateDialog(MessageBoxBase):
    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self._coin = _DONATE_COINS[0]
        self._qr_cache: dict[str, QPixmap | None] = {}
        self._fade: QPropertyAnimation | None = None
        self._copy_timer = QTimer(self)
        self._copy_timer.setSingleShot(True)
        self._copy_timer.timeout.connect(self._show_copy_idle)
        self.widget.setMinimumWidth(580)

        self._title = SubtitleLabel('')
        self._intro = BodyLabel('')
        self._intro.setWordWrap(True)
        apply_label_theme(self._intro, 'dialog_body_text')
        self.viewLayout.addWidget(self._title)
        self.viewLayout.addSpacing(2)
        self.viewLayout.addWidget(self._intro)
        self.viewLayout.addSpacing(14)

        self._pivot = Pivot(self)
        for coin in _DONATE_COINS:
            self._pivot.addItem(routeKey=coin.key, text=coin.ticker)
        apply_pivot_theme(self._pivot)
        self.viewLayout.addWidget(self._pivot)
        self.viewLayout.addSpacing(14)

        self._detail = QWidget()
        self._detail.setObjectName('donateDetail')
        self._detail.setStyleSheet('#donateDetail { background: transparent; }')
        row = QHBoxLayout(self._detail)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(22)

        self._qr = _DonateQrTile()
        row.addWidget(self._qr, 0, Qt.AlignmentFlag.AlignTop)

        column = QVBoxLayout()
        column.setSpacing(0)
        self._name = StrongBodyLabel('')
        name_font = self._name.font()
        name_font.setPixelSize(18)
        self._name.setFont(name_font)
        self._network = CaptionLabel('')
        column.addWidget(self._name)
        column.addSpacing(2)
        column.addWidget(self._network)
        column.addSpacing(10)
        self._warn = _DonateWarnLabel('')
        self._warn.setWordWrap(True)
        column.addWidget(self._warn)
        column.addStretch(1)
        self._address = _DonateAddressField()
        self._address.clicked.connect(self._copy)
        column.addWidget(self._address)
        column.addSpacing(10)
        self._copy_btn = PushButton('')
        self._copy_btn.clicked.connect(self._copy)
        column.addWidget(self._copy_btn, 0, Qt.AlignmentFlag.AlignLeft)
        row.addLayout(column, 1)
        self.viewLayout.addWidget(self._detail)

        self.viewLayout.addSpacing(14)
        self._note = CaptionLabel('')
        self._note.setWordWrap(True)
        apply_label_theme(self._note, 'dialog_body_text')
        self.viewLayout.addWidget(self._note)

        self.cancelButton.hide()
        self._pivot.currentItemChanged.connect(self._on_tab)
        self._pivot.setCurrentItem(self._coin.key)
        self._apply_locale()
        self._show_coin(animate=False)
        register_theme_refresh(self, self._apply_theme)
        with suppress(Exception):
            register_locale_refresh(self, self._apply_locale)


    def _apply_theme(self) -> None:
        self._address.refresh()
        self._qr.update()
        self._warn._applyThemeColor()
        if not self._copy_timer.isActive():
            self._show_copy_idle()

    def _apply_locale(self) -> None:
        self._title.setText(_donate_t('donate.title', 'Support PiraChest'))
        self._intro.setText(_donate_t(
            'donate.intro', "PiraChest is free. Chipping in with crypto is optional, but appreciated."))
        self._note.setText(_donate_t('donate.verify', 'Check the coin and network before you send anything.'))
        self._warn.setText(_donate_t(
            'donate.stablecoin_warning',
            'USDC and USDT exist on several networks. Confirm which one before you send.'))
        self.yesButton.setText(_donate_t('donate.close', 'Close'))
        self._show_copy_idle()


    def _load_qr(self, coin: _DonateCoin) -> QPixmap | None:
        if coin.key in self._qr_cache:
            return self._qr_cache[coin.key]
        pixmap: QPixmap | None = None
        path = _donate_qr_path(coin.key)
        if path is None:
            logger.warning('Missing donation QR image for %s', coin.key)
        else:
            source = QPixmap(str(path))
            if source.isNull():
                logger.warning('Could not read donation QR image: %s', path)
            else:
                dpr = self.devicePixelRatioF()
                side = round(_DonateQrTile.IMAGE * dpr)
                pixmap = source.scaled(side, side, Qt.AspectRatioMode.KeepAspectRatio,
                                       Qt.TransformationMode.SmoothTransformation)
                pixmap.setDevicePixelRatio(dpr)
        self._qr_cache[coin.key] = pixmap
        return pixmap

    def _on_tab(self, key: str) -> None:
        coin = next((c for c in _DONATE_COINS if c.key == key), None)
        if coin is not None and coin is not self._coin:
            self._coin = coin
            self._show_coin(animate=True)

    def _show_coin(self, *, animate: bool) -> None:
        coin = self._coin
        self._name.setText(coin.name)
        self._network.setText(coin.network)
        self._qr.set_content(self._load_qr(coin), _donate_t('donate.qr_missing', 'No QR code for this coin'))
        self._address.set_address(coin.address)
        self._warn.setVisible(coin.stablecoin)
        self._show_copy_idle()
        if animate:
            self._swap_fade()

    def _swap_fade(self) -> None:
        if self._fade is not None:
            with suppress(Exception):
                self._fade.finished.disconnect()
            self._fade.stop()
        effect = QGraphicsOpacityEffect(self._detail)
        self._detail.setGraphicsEffect(effect)
        anim = QPropertyAnimation(effect, b'opacity', self._detail)
        anim.setDuration(180)
        anim.setStartValue(0.35)
        anim.setEndValue(1.0)
        anim.setEasingCurve(QEasingCurve.Type.OutExpo)
        anim.finished.connect(lambda: self._detail.setGraphicsEffect(None))
        self._fade = anim
        anim.start()

    def _copy(self) -> None:
        QApplication.clipboard().setText(self._coin.address)
        self._copy_btn.setText(_donate_t('donate.copied', 'Copied'))
        self._copy_btn.setIcon(themed_icon(None, FluentIcon.ACCEPT, 16))
        self._copy_timer.start(1800)

    def _show_copy_idle(self) -> None:
        self._copy_timer.stop()
        self._copy_btn.setText(_donate_t('donate.copy', 'Copy address'))
        self._copy_btn.setIcon(themed_icon('download.donate_copy_address', FluentIcon.COPY, 16))


class MainWindow(FluentWindow):
    _chrome = _shader_bg = _media_bg = _update_overlay = None
    _resize_settle_timer = _bg_refresh_debounce_timer = _bg_scaled_cache = None
    _bg_modal_timer = None
    _bg_movie = _bg_player = _bg_video_sink = None

    def _apply_theme_background(self, preset_id: str | None) -> None:
        try:
            if preset_id is None:
                self.setCustomBackgroundColor(QColor(palette(dark=False)['page_bg']), QColor(palette(dark=True)['page_bg']))
            else:
                meta = preset_meta(preset_id)
                bg = QColor(meta.palette['page_bg'])
                self.setCustomBackgroundColor(bg, bg)
        except Exception:
            logger.exception('Failed to set custom background color')

    def _apply_live_media_params(self) -> bool:
        media_bg = self._media_bg
        if media_bg is None or not media_bg.isVisible() or getattr(media_bg, '_failed', False):
            return False
        if theme_background(active_preset_id()) is not None:
            return False
        try:
            media_bg.set_params(int(settings.background_image_blur), int(settings.background_image_darken))
            return True
        except Exception:
            return False

    def _drop_background_photo(self) -> None:
        self._bg_photo_processed = None
        self._bg_fingerprint = None
        self._stop_background_media()

    def _schedule_background_refresh(self, delay_ms: int = 120) -> None:
        if self._apply_live_media_params():
            return

        timer = self._bg_refresh_debounce_timer
        if timer is None:
            timer = QTimer(self)
            timer.setSingleShot(True)
            timer.timeout.connect(self._refresh_background_photo)
            self._bg_refresh_debounce_timer = timer
        timer.start(delay_ms)

    def _defer_background_until_modal_closes(self) -> bool:
        if QApplication.activeModalWidget() is None:
            return False
        timer = self._bg_modal_timer
        if timer is None:
            timer = QTimer(self)
            timer.setInterval(120)
            timer.timeout.connect(self._retry_background_after_modal)
            self._bg_modal_timer = timer
        if not timer.isActive():
            timer.start()
        return True

    def _retry_background_after_modal(self) -> None:
        if QApplication.activeModalWidget() is not None:
            return
        timer = self._bg_modal_timer
        if timer is not None:
            timer.stop()
        self._refresh_background_photo()

    def _track_native_window(self, build):
        was_visible, was_max = self.isVisible(), self.isMaximized()
        try:
            before = int(self.winId())
        except Exception:
            before = 0
        result = build()
        try:
            after = int(self.winId())
        except Exception:
            after = before
        if after != before:
            self._on_native_window_recreated(was_visible, was_max)
        return result

    def _on_native_window_recreated(self, was_visible: bool = False, was_max: bool = False) -> None:
        logger.info('Native window was recreated by Qt (first OpenGL child); re-arming window state')
        with suppress(Exception):
            update = getattr(self, 'updateFrameless', None)
            if update is not None:
                update()
        with suppress(Exception):
            self._apply_taskbar_icon()
        chrome = self._chrome
        if chrome is not None:
            with suppress(Exception):
                chrome.native_window_recreated()
        if was_visible and not self.isVisible():
            self.showMaximized() if was_max else self.show()
        if self.isVisible() and chrome is not None:
            QTimer.singleShot(0, chrome.apply)

    def _prepare_gl_surface(self) -> None:
        if not _gl_surface_wanted():
            return
        try:
            self._ensure_shader_bg()
        except Exception:
            logger.exception('Could not pre-create the GL background surface')

    def _ensure_shader_bg(self):
        widget = self._shader_bg
        if widget is not None:
            widget.sync_samples()
        else:
            widget = self._track_native_window(
                lambda: ShaderBackgroundWidget(self, samples=_max_shader_aa_samples()))
            widget.setGeometry(self.rect())
            widget.lower()
            widget.hide()
            widget.errorOccurred.connect(self._on_shader_background_error)
            chrome = self._chrome
            widget.set_base_color(QColor(palette()['page_bg']))
            widget.set_transparent_window(bool(chrome is not None and getattr(chrome, '_transparent', False)))
            self._shader_bg = widget
        return widget

    def _ensure_media_bg(self):
        widget = self._media_bg
        if widget is None:
            widget = self._track_native_window(lambda: MediaBackgroundWidget(self))
            widget.setGeometry(self.rect())
            widget.lower()
            widget.hide()
            widget.failed.connect(self._on_media_background_failed)
            self._media_bg = widget
        return widget

    def _refresh_background_photo(self) -> None:
        if self._defer_background_until_modal_closes():
            return
        preset = active_preset_id()
        shader_frag = theme_shader_path(preset)
        if shader_frag is not None:
            self._drop_background_photo()
            shader_bg = self._ensure_shader_bg()
            shader_bg.setGeometry(self._shader_bg_rect())
            if not shader_bg.is_active_shader(shader_frag):
                if not shader_bg.set_shader(shader_frag):
                    shader_bg.hide()
                    self.update()
                    return
            shader_bg.set_image(theme_shader_image_path(preset))
            shader_bg.show()
            shader_bg.lower()
            _aa = _shader_aa_samples()
            _mode = f'theme shader, AA={str(_aa) + "x" if _aa else "off"}'
            if getattr(self, '_last_bg_mode', None) != _mode:
                self._last_bg_mode = _mode
                _report_bg_mode('GPU', _mode)
            shader_bg.set_accent_color(palette()['accent'])
            shader_bg.set_fps(theme_shader_fps(preset))
            self.update()
            return
        shader_bg = self._shader_bg
        if shader_bg is not None and (shader_bg.isVisible() or shader_bg._frag_path is not None):
            shader_bg.hide()
            shader_bg.clear()

        try:
            theme_bg = theme_background(preset)
            if theme_bg is not None:
                path, blur, darken = theme_bg
                scope = ('preset', preset)
            else:
                enabled = settings.background_image_enabled
                path = settings.background_image_path or ''
                if not enabled or not path or not os.path.isfile(path):
                    self._drop_background_photo()
                    self.update()
                    return
                blur = int(settings.background_image_blur)
                darken = int(settings.background_image_darken)
                scope = ('settings',)
            if not os.path.isfile(path):
                self._drop_background_photo()
                self.update()
                return
            try:
                stat = (os.path.getmtime(path), os.path.getsize(path))
            except OSError:
                stat = None
            key = (os.path.abspath(path), int(blur), int(darken), stat, scope)
            if key == getattr(self, '_bg_fingerprint', None) and self._bg_photo_processed is not None:
                self.update()
                return
            self._drop_background_photo()
            self._bg_blur = blur
            self._bg_darken = darken
            ext = os.path.splitext(path)[1].lower()
            if ext in ('.mp4', '.webm', '.mov', '.mkv', '.avi'):
                if self._start_gpu_background('video', path, blur, darken):
                    self._bg_fingerprint = key
                    return
                self._start_background_video(path, darken)
                self._bg_fingerprint = key
                return
            movie = QMovie(path)
            if movie.isValid() and movie.frameCount() != 1:
                if self._start_gpu_background('gif', path, blur, darken):
                    movie.deleteLater()
                    self._bg_fingerprint = key
                    return
                self._start_background_movie(movie, darken)
                self._bg_fingerprint = key
                return
            raw = QPixmap(path)
            if raw.isNull():
                return
            max_dim = 1280
            if raw.width() > max_dim or raw.height() > max_dim:
                raw = raw.scaled(max_dim, max_dim, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.FastTransformation)
            self._bg_photo_processed = _process_background_pixmap(raw, blur, darken)
            self._bg_fingerprint = key
        except Exception:
            logger.exception('Failed to refresh background photo')
            self._bg_photo_processed = None
            self._bg_fingerprint = None
        finally:
            self.update()

    def _on_shader_background_error(self, message: str) -> None:
        logger.warning('Shader background failed, falling back: %s', message)
        _report_bg_mode('CPU/solid (shader failed)', (message or '').strip().splitlines()[0][:120] if (message or '').strip() else 'unknown error')
        shader_bg = self._shader_bg
        if shader_bg is not None:
            shader_bg.hide()
            shader_bg.clear()
        self.update()
        try:
            InfoBar.warning(
                title=tr('settings.shader_error_title'), content=message[:300],
                orient=Qt.Orientation.Horizontal, isClosable=True,
                position=InfoBarPosition.TOP_RIGHT, duration=6000, parent=self,
            )
        except Exception:
            logger.exception('Failed to show shader error notification')

    def _start_gpu_background(self, kind: str, path: str, blur: int, darken: int) -> bool:
        if os.environ.get('PIRACHEST_CPU_MEDIA_BG', '0') == '1':
            _report_bg_mode('CPU', 'forced by PIRACHEST_CPU_MEDIA_BG=1')
            return False
        try:
            self._media_bg_fallback = (kind, path, darken)
            media_bg = self._ensure_media_bg()
            media_bg.setGeometry(self._shader_bg_rect())
            media_bg.show()
            media_bg.lower()
            media_bg.start(kind, path, blur, darken)
            self.update()
            return True
        except Exception:
            logger.exception('GPU media background failed to start; using CPU path')
            media_bg = self._media_bg
            if media_bg is not None:
                media_bg.hide()
            self._media_bg_fallback = None
            return False

    def _on_media_background_failed(self, message: str) -> None:
        fallback = getattr(self, '_media_bg_fallback', None)
        self._media_bg_fallback = None
        media_bg = self._media_bg
        if media_bg is not None:
            media_bg.stop()
            media_bg.hide()
        if fallback is None:
            return
        kind, path, darken = fallback

        try:
            if kind == 'video':
                self._start_background_video(path, darken)
            else:
                movie = QMovie(path)
                if movie.isValid():
                    self._start_background_movie(movie, darken)
        except Exception:
            logger.exception('CPU fallback for media background also failed')

    def _stop_background_media(self) -> None:
        media_bg = self._media_bg
        if media_bg is not None:
            media_bg.stop()
            media_bg.hide()
        self._media_bg_fallback = None
        movie = self._bg_movie
        if movie is not None:
            movie.stop()
            movie.frameChanged.disconnect(self._on_bg_movie_frame)
            self._bg_movie = None
        player = self._bg_player
        if player is not None:
            player.stop()
            self._bg_player = None
        sink = self._bg_video_sink
        if sink is not None:
            sink.videoFrameChanged.disconnect(self._on_bg_video_frame)
            self._bg_video_sink = None

    def _start_background_movie(self, movie: QMovie, darken: int) -> None:
        _report_bg_mode('CPU', 'GIF')
        self._bg_movie = movie
        self._bg_darken = darken
        self._bg_frame_at = 0.0
        movie.frameChanged.connect(self._on_bg_movie_frame)

        movie.start()

    def _on_bg_movie_frame(self, _frame_index: int) -> None:
        now = time.monotonic()
        if now - getattr(self, '_bg_frame_at', 0.0) < 1 / 60:
            return
        self._bg_frame_at = now
        movie = self._bg_movie
        if movie is None:
            return
        pix = movie.currentPixmap()
        if pix.isNull():
            return
        self._bg_photo_processed = _process_background_pixmap(pix, getattr(self, '_bg_blur', 0), getattr(self, '_bg_darken', 0))
        self.update()

    def _start_background_video(self, path: str, darken: int) -> None:
        _report_bg_mode('CPU', 'video')
        from PySide6.QtMultimedia import QMediaPlayer, QVideoSink
        self._bg_darken = darken
        sink = QVideoSink(self)
        sink.videoFrameChanged.connect(self._on_bg_video_frame)
        player = QMediaPlayer(self)
        player.setVideoSink(sink)
        player.setSource(path)
        player.setLoops(QMediaPlayer.Loops.Infinite)
        player.setAudioOutput(None)
        player.play()
        self._bg_video_sink = sink
        self._bg_player = player

    def _on_bg_video_frame(self, frame) -> None:
        if getattr(self, '_bg_video_busy', False):
            self._bg_video_pending = frame
            return
        self._bg_video_busy = True
        try:
            while frame is not None:
                image = frame.toImage()
                if not image.isNull():
                    pix = QPixmap.fromImage(image)
                    self._bg_photo_processed = _process_background_pixmap(pix, getattr(self, '_bg_blur', 0), getattr(self, '_bg_darken', 0))
                    self.update()
                frame = getattr(self, '_bg_video_pending', None)
                self._bg_video_pending = None
        finally:
            self._bg_video_busy = False

    def _scaled_background(self, pixmap: QPixmap, size: QSize) -> QPixmap:
        key = (pixmap.cacheKey(), size.width(), size.height())
        cached = self._bg_scaled_cache
        if cached is not None and cached[0] == key:
            return cached[1]
        scaled = pixmap.scaled(size, Qt.AspectRatioMode.KeepAspectRatioByExpanding, Qt.TransformationMode.SmoothTransformation)
        self._bg_scaled_cache = (key, scaled)
        return scaled

    def paintEvent(self, event) -> None:
        chrome = self._chrome
        if chrome is None:
            super().paintEvent(event)
            return
        framed = chrome.paint_frame(self)
        pixmap = getattr(self, '_bg_photo_processed', None)
        if pixmap is not None and not pixmap.isNull():
            painter = QPainter(self)
            target = self._bg_scope_rect(framed)
            painter.setClipRect(target)
            painter.fillRect(target, Qt.GlobalColor.black)
            scaled = self._scaled_background(pixmap, target.size())
            x = target.x() + (target.width() - scaled.width()) // 2
            y = target.y() + (target.height() - scaled.height()) // 2
            painter.drawPixmap(x, y, scaled)
            painter.end()
            return
        if framed:
            return
        super().paintEvent(event)

    def _normalBackgroundColor(self):
        base = super()._normalBackgroundColor()
        chrome = self._chrome
        return chrome.window_background(base) if chrome is not None else base

    def __init__(self, initial_targets: list | None = None):
        super().__init__()
        self._chrome = WindowChrome(self)
        self._chrome.install()
        self._chrome.applied.connect(self._on_chrome_applied)
        self.setVisible(False)
        self._ui_ready = False
        self._pending_open_targets = list(initial_targets or [])
        self.setWindowTitle('PiraChest')
        self.resize(1100, 740)
        self.setMinimumSize(900, 600)
        self._force_quit = False
        self._konami_pos = 0
        self._konami_seen = 0.0
        self._konami_cooldown = 0.0
        self._konami_stamp = None
        self._preload_thread = None
        self._preload_stop = None
        self._tray_icon = None
        self._downloads_badge = None
        self._downloads_badge_count = -1
        self._bg_photo_processed = None
        self._bg_fingerprint = None
        self._bg_movie = None
        self._bg_player = None
        self._bg_video_sink = None
        self._bg_blur = 0
        self._bg_darken = 0

        self._shader_bg = None
        self._media_bg = None
        self._media_bg_fallback = None
        self._app_active = True
        self._prepare_gl_surface()
        app = QApplication.instance()
        app.installEventFilter(self)
        app.applicationStateChanged.connect(self._on_app_state_changed)

        resolve_theme(setTheme)
        try:
            self._theme_maker_shortcut = QShortcut(
                QKeySequence('Ctrl+Shift+T'), self, activated=self._open_theme_maker_checked)
        except Exception:
            logger.exception('Theme Maker shortcut failed')
        with suppress(Exception):
            saved_preset = settings.theme_preset
            preset_meta_obj = preset_meta(saved_preset) if saved_preset else None
            activate_theme(saved_preset if preset_meta_obj is not None else None)
            if preset_meta_obj is not None:
                apply_theme_font_families(resolve_font_families(preset_meta_obj))
            self._apply_theme_background(saved_preset if preset_meta_obj is not None else None)
            self._refresh_background_photo()
        try:
            if settings.show_console:
                set_debug_console_visible(True)
        except Exception:
            logger.exception('Failed to restore debug console visibility')
        app_icon_path = _logo_ico_path() or find_app_icon_path()
        if app_icon_path:
            self.setWindowIcon(QIcon(app_icon_path))
        self._apply_taskbar_icon()
        self._enlarge_title_bar_branding()
        self._add_donate_button()
        self._add_discord_button()
        self._add_admin_shield()
        self._strip_titlebar_tooltips()
        self._update_overlay = UpdateOverlay(self)
        self.splashScreen = _ThemedSplash(self.windowIcon(), self)
        self.splashScreen.setIconSize(QSize(112, 112))
        self.showMaximized()
        self._sync_splash()
        QApplication.processEvents()
        self._sync_splash()
        self._force_foreground_focus()
        QTimer.singleShot(0, self._force_foreground_focus)
        QTimer.singleShot(150, self._force_foreground_focus)
        self.navigationInterface.setExpandWidth(180)
        self.navigationInterface.setCollapsible(False)
        self.navigationInterface.setReturnButtonVisible(False)
        self.navigationInterface.setMenuButtonVisible(True)
        with suppress(Exception):
            _panel = self.navigationInterface.panel
            _hbar = _panel.scrollArea.horizontalScrollBar()
            _hbar.setValue(0)
            if not getattr(_panel, '_pirachest_hlocked', False):
                _panel._pirachest_hlocked = True
                _hbar.valueChanged.connect(
                    lambda _v: _panel.scrollArea.horizontalScrollBar().setValue(0))
        self._init_tray_icon()
        qconfig.themeChanged.connect(self._on_global_theme_changed)
        self.home_page = None
        self.pc_games_page: LazyPage | None = None
        self.tbhb_page: LazyPage | None = None
        self.music_page: LazyPage | None = None
        self.books_page: LazyPage | None = None
        self.yt_page: LazyPage | None = None
        self.tv_page: LazyPage | None = None
        self._books_manga_bridge = None
        self.yt_bridge = None
        from src.core.downloader import DownloadManager
        from .download_page import DownloadManagerPage
        self.download_manager = DownloadManager(self)
        if self._pending_open_targets:
            QTimer.singleShot(0, self._flush_pending_open_targets)

        self.home_nav_page = HomePage(self)
        self.home_nav_page.setObjectName('homePage')
        self._nav_home = self.addSubInterface(self.home_nav_page, themed_nav_icon('nav.home', FluentIcon.HOME), 'Home', position=NavigationItemPosition.TOP)

        if settings.onboarding_completed:
            self._sync_optional_pages()
        self.download_page = DownloadManagerPage(self.download_manager, self)
        self.download_page.setObjectName('downloadPage')
        self._nav_downloads = self.addSubInterface(self.download_page, themed_nav_icon('nav.downloads', FluentIcon.DOWNLOAD), tr('nav.downloads'), position=NavigationItemPosition.BOTTOM)
        self.download_manager.all_downloads_finished.connect(self._on_all_downloads_finished)
        self.download_manager.item_added.connect(self._schedule_downloads_badge_update)
        self.download_manager.item_updated.connect(self._schedule_downloads_badge_update)
        self.download_manager.item_removed.connect(self._schedule_downloads_badge_update)
        self.download_manager.download_completed.connect(self._on_download_completed)

        self._downloads_badge_timer = QTimer(self)
        self._downloads_badge_timer.setSingleShot(True)
        self._downloads_badge_timer.setInterval(200)
        self._downloads_badge_timer.timeout.connect(self._update_downloads_badge)
        self._update_downloads_badge()
        self.settings_page = SettingsPage(self)
        self.settings_page.bind_network(self.download_manager.net_prefs, self.download_manager.set_net_prefs)
        self.settings_page.setObjectName('settingsPage')
        self.sailseek = SailSeekController(self)
        self._nav_settings = self.addSubInterface(self.settings_page, themed_nav_icon('nav.settings', FluentIcon.SETTING), tr('nav.settings'), position=NavigationItemPosition.BOTTOM)
        self.navigationInterface.addItem(
            routeKey='openDataFolder',
            icon=themed_nav_icon('nav.open_data_folder', FluentIcon.FOLDER),
            text=tr('nav.open_data_folder'),
            onClick=self._on_open_data_folder,
            selectable=False,
            position=NavigationItemPosition.BOTTOM,
        )
        self.about_page = LazyPage(lambda: AboutPage(self), keep_alive=True)
        self.about_page.setObjectName('aboutPage')
        self._nav_about = self.addSubInterface(self.about_page, themed_nav_icon('nav.about', FluentIcon.INFO), tr('nav.about'), position=NavigationItemPosition.BOTTOM)
        register_locale_refresh(self, self._refresh_nav_text)
        self._refresh_nav_icons()
        self._apply_content_surface_tint()
        self._center_scroll_nav_items()
        try:
            self.stackedWidget.currentChanged.connect(self._on_stacked_page_changed)
            self.stackedWidget.currentChanged.connect(self._record_nav_history)
        except Exception:
            logger.exception('Failed to hook navigation sound effects')

        try:
            self.switchTo(self.home_nav_page)
        except Exception:
            logger.exception('Failed to switch to Home on launch')
        self.settings_page.settings_changed.connect(self._on_settings_changed)
        self._load_filters()
        self._auto_sync()
        self._init_keyboard_shortcuts()
        with suppress(Exception):
            self.splashScreen.finish()
        self.splashScreen = None
        _maybe_run_onboarding(self)
        self._ensure_associations_registered()
        self._ui_ready = True

    def handle_external_args(self, args: list) -> None:
        targets = extract_cli_targets(args)
        if not targets:
            return
        self._pending_open_targets.extend(targets)
        self._flush_pending_open_targets()
        self._force_foreground_focus()

    def _flush_pending_open_targets(self) -> None:
        if not getattr(self, 'download_manager', None):
            return
        targets, self._pending_open_targets = self._pending_open_targets, []
        for target in targets:
            self._open_download_target(target)

    def _open_download_target(self, target: str) -> None:
        try:
            if _send_to_torrent_client(target):
                self._force_foreground_focus()
                return
            self.switchTo(self.download_page)
            self.download_page._start_selective_add(target, source_label='External')
            self._force_foreground_focus()
        except Exception:
            logger.exception('Failed to open external target: %s', target)

    def _on_stacked_page_changed(self, _index: int) -> None:
        shader_bg = self._shader_bg
        if shader_bg is not None:
            shader_bg.set_page_index(_index)
        if self._ui_ready:
            with suppress(Exception):
                get_sound_manager().play('navigate')
        current = self.stackedWidget.currentWidget()
        try:
            lazy = getattr(self, 'pc_games_page', None)
            if lazy is not None and current is lazy:
                inner = lazy.page
                if inner is not None:
                    inner.refresh_visible()
                else:
                    lazy.call_when_ready('refresh_visible')
        except Exception:
            logger.exception('Failed to auto-refresh repacks on navigation')

    def _on_download_completed(self, item_id: str, game_name: str) -> None:
        if not settings.notify_on_download_complete:
            return
        display_name = game_name or tr('download.rom_fallback')
        try:
            if self._tray_icon is not None:
                title = self.tr_or('notifications.download_complete_title', 'Download complete')
                body = self.tr_or('notifications.download_complete_content', '{name} has finished downloading.').format(name=display_name)
                self._tray_icon.showMessage(
                    title,
                    body,
                    QSystemTrayIcon.MessageIcon.Information,
                    5000,
                )
        except Exception:
            logger.exception('Failed to show download-complete notification')

    def _on_all_downloads_finished(self) -> None:
        action = settings.on_finish_action
        if action not in ('shutdown', 'sleep'):
            return
        title = tr('settings.finish_action_shutdown') if action == 'shutdown' else tr('settings.finish_action_sleep')
        countdown = {'seconds': 60}
        box = MessageBoxBase(self)
        box.titleLabel = SubtitleLabel(tr('settings.finish_confirm_title', action=title), box)
        box.viewLayout.addWidget(box.titleLabel)
        content_lbl = CaptionLabel(tr('settings.finish_confirm_content', action=title, seconds=countdown['seconds']))
        content_lbl.setWordWrap(True)
        box.viewLayout.addWidget(content_lbl)
        box.yesButton.setText(tr('settings.finish_confirm_proceed'))
        box.cancelButton.setText(tr('download.cancel'))
        timer = QTimer(box)
        timer.setInterval(1000)

        def _tick():
            countdown['seconds'] -= 1
            if countdown['seconds'] <= 0:
                timer.stop()
                box.accept()
                return
            content_lbl.setText(tr('settings.finish_confirm_content', action=title, seconds=countdown['seconds']))
        timer.timeout.connect(_tick)
        timer.start()
        result = box.exec()
        timer.stop()
        if not result:
            return
        self._perform_finish_action(action)

    def _perform_finish_action(self, action: str) -> None:
        try:
            if sys.platform == 'win32':
                cmd = ['shutdown', '/s', '/t', '0'] if action == 'shutdown' else ['rundll32.exe', 'powrprof.dll,SetSuspendState', '0,1,0']
            elif sys.platform == 'darwin':
                cmd = ['osascript', '-e', 'tell app "System Events" to shut down'] if action == 'shutdown' else ['pmset', 'sleepnow']
            else:
                cmd = ['systemctl', 'poweroff'] if action == 'shutdown' else ['systemctl', 'suspend']
            subprocess.Popen(cmd)
        except Exception:
            logger.exception('Failed to perform finish action: %s', action)

    def _schedule_downloads_badge_update(self, *_args) -> None:
        self._downloads_badge_timer.start()

    def _drop_downloads_badge(self) -> None:
        badge = self._downloads_badge
        self._downloads_badge = None
        if badge is None:
            return
        manager = getattr(badge, 'manager', None)
        if manager is not None:
            target = getattr(manager, 'target', None)
            if target is not None:
                with suppress(RuntimeError):
                    target.removeEventFilter(manager)
            with suppress(RuntimeError):
                manager.deleteLater()
            with suppress(Exception):
                badge.manager = None
        with suppress(RuntimeError):
            badge.hide()
            badge.deleteLater()

    def _update_downloads_badge(self, *_args) -> None:
        from src.core.downloader import DLState
        active = sum(1 for item in self.download_manager.items_in_order() if item.state in (DLState.queued, DLState.downloading, DLState.verifying))
        if active == self._downloads_badge_count:
            return
        self._downloads_badge_count = active

        badge = self._downloads_badge
        if badge is not None:
            try:
                badge.text()
            except RuntimeError:
                self._downloads_badge = None
                badge = None

        if active <= 0:
            if badge is not None:
                with suppress(RuntimeError):
                    badge.hide()
            return

        nav_widget = self.navigationInterface.widget(self.download_page.objectName())
        if nav_widget is None:
            return

        if badge is not None:
            manager = getattr(badge, 'manager', None)
            if manager is not None:
                try:
                    target = manager.target
                except RuntimeError:
                    target = None
                if target is not nav_widget:
                    self._drop_downloads_badge()
                    badge = None

        if badge is None:
            self._downloads_badge = InfoBadge.attension(active, parent=self, target=nav_widget, position=InfoBadgePosition.NAVIGATION_ITEM)
            return

        with suppress(RuntimeError):
            badge.setText(str(active))
            badge.adjustSize()
            manager = getattr(badge, 'manager', None)
            if manager is not None:
                badge.move(manager.position())
            badge.show()

    def _on_global_theme_changed(self, *_args) -> None:
        self._apply_theme_background(active_preset_id())
        self._repolish_theme()
        timer = self._bg_refresh_debounce_timer
        if timer is not None:
            timer.stop()
        if not self._apply_live_media_params():
            self._refresh_background_photo()

    def _repolish_theme(self) -> None:
        try:
            qss = global_app_qss()
            app = QApplication.instance()
            if app is not None and app.styleSheet() != qss:
                app.setStyleSheet(qss)
            self._chrome.apply()
            self._apply_content_surface_tint()
            self._refresh_nav_icons()
            self._center_scroll_nav_items()
        except Exception:
            logger.exception('Theme repolish failed')
        self._frame_invalidate()

    def _frame_invalidate(self) -> None:
        chrome = self._chrome
        if chrome is not None:
            chrome._frame_pixmap = None
            chrome._frame_key = ()
        self._bg_scaled_cache = None
        shader = self._shader_bg
        if shader is not None and shader.isVisible():
            shader.update()
        self.update()

    def _release_idle_memory(self) -> None:
        chrome = self._chrome
        if chrome is not None:
            chrome._frame_pixmap = None
            chrome._frame_key = ()
        self._bg_scaled_cache = None
        with suppress(Exception):
            from PySide6.QtGui import QPixmapCache
            QPixmapCache.clear()
        _trim_idle_working_set()

    def _apply_content_surface_tint(self, *_args):
        style = settings_qss()
        with suppress(Exception):
            if self.settings_page.styleSheet() != style:
                self.settings_page.setStyleSheet(style)
        with suppress(Exception):
            if self.about_page.styleSheet() != style:
                self.about_page.setStyleSheet(style)

    def _force_foreground_focus(self) -> None:
        if sys.platform != 'win32':
            self.raise_()
            self.activateWindow()
            return
        try:
            user32 = ctypes.windll.user32
            kernel32 = ctypes.windll.kernel32
            hwnd = int(self.winId())
            fg_hwnd = user32.GetForegroundWindow()
            fg_tid = user32.GetWindowThreadProcessId(fg_hwnd, None)
            cur_tid = kernel32.GetCurrentThreadId()
            if fg_hwnd and fg_hwnd != hwnd and fg_tid != cur_tid:
                user32.AttachThreadInput(fg_tid, cur_tid, True)
                try:
                    user32.SetForegroundWindow(hwnd)
                    user32.BringWindowToTop(hwnd)
                finally:
                    user32.AttachThreadInput(fg_tid, cur_tid, False)
            else:
                user32.SetForegroundWindow(hwnd)
            self.raise_()
            self.activateWindow()
        except Exception:
            logger.exception('Failed to force foreground focus')
            self.raise_()
            self.activateWindow()

    def _ensure_associations_registered(self) -> None:
        try:
            if not associations_available():
                return
            if not settings.associations_exe_path or associations_need_repair():
                register_associations()
        except Exception:
            logger.exception('Failed to auto-register file associations on startup')

    def _add_donate_button(self) -> None:
        title_bar = getattr(self, 'titleBar', None)
        if title_bar is None:
            return
        try:
            chrome = self._chrome
            chip = ChromeChip(tr('download.donate'), trimmed_icon(filled_icon(FluentIcon.HEART).pixmap(128, 128), fill=1.0), title_bar)
            chip.setObjectName('donateChip')
            chip.clicked.connect(lambda: DonateDialog(self).exec())
            if hasattr(title_bar, 'addChip'):
                title_bar.addChip(chip)
            else:
                title_bar.hBoxLayout.insertWidget(2, chip, 0, Qt.AlignmentFlag.AlignVCenter)
            if chrome is not None:
                with suppress(Exception):
                    chip.configure(chrome)
            self._donate_chip = chip
            register_locale_refresh(self, self._refresh_title_chip_text)
        except Exception:
            logger.exception('Failed to add donate button to title bar')

    def _add_admin_shield(self) -> None:
        title_bar = getattr(self, 'titleBar', None)
        if title_bar is None:
            return
        try:
            if not is_admin():
                return
            label = getattr(title_bar, 'titleLabel', None)
            layout, idx = None, -1
            if label is not None:
                for cand in (getattr(title_bar, 'hBoxLayout', None), label.parentWidget().layout() if label.parentWidget() else None):
                    if cand is not None and cand.indexOf(label) >= 0:
                        layout, idx = cand, cand.indexOf(label)
                        break
            if layout is None:
                logger.warning('Admin shield skipped: title label not found in the title bar layout')
                return
            shield = _AdminShield(title_bar)
            layout.insertWidget(idx + 1, shield, 0, Qt.AlignmentFlag.AlignVCenter)
            self._admin_shield = shield
        except Exception:
            logger.exception('Failed to add admin shield to title bar')

    def _add_discord_button(self) -> None:
        title_bar = getattr(self, 'titleBar', None)
        if title_bar is None:
            return
        try:
            chrome = self._chrome
            try:
                discord_label = self.tr_or('titlebar.discord', 'Discord')
            except Exception:
                discord_label = 'Discord'
            chip = ChromeChip(discord_label, FluentIcon.CHAT, title_bar)
            chip.setObjectName('discordChip')
            icon_path = find_discord_icon_path()
            pixmap = QPixmap(icon_path) if icon_path else QPixmap()
            if not pixmap.isNull():
                chip.setIconSource(trimmed_icon(pixmap, fill=1.0))
            chip.clicked.connect(lambda: QDesktopServices.openUrl(QUrl(DISCORD_URL)))
            if hasattr(title_bar, 'addChip'):
                title_bar.addChip(chip)
            else:
                title_bar.hBoxLayout.insertWidget(3, chip, 0, Qt.AlignmentFlag.AlignVCenter)
            if chrome is not None:
                with suppress(Exception):
                    chip.configure(chrome)
            self._discord_chip = chip
            register_locale_refresh(self, self._refresh_title_chip_text)
        except Exception:
            logger.exception('Failed to add Discord button to title bar')

    def _refresh_title_chip_text(self) -> None:
        with suppress(Exception):
            donate = getattr(self, '_donate_chip', None)
            if donate is not None:
                donate.setText(tr('download.donate'))
        with suppress(Exception):
            discord = getattr(self, '_discord_chip', None)
            if discord is not None:
                discord.setText(self.tr_or('titlebar.discord', 'Discord'))

    def _strip_titlebar_tooltips(self) -> None:
        title_bar = getattr(self, 'titleBar', None)
        if title_bar is None:
            return
        for widget in title_bar.findChildren(QWidget):
            with suppress(Exception):
                widget.setToolTip('')

    def _enlarge_title_bar_branding(self) -> None:
        self._chrome.apply()

    def _style_title_chips(self) -> None:
        chrome = self._chrome
        if chrome is None:
            return
        for chip in (getattr(self, '_donate_chip', None), getattr(self, '_discord_chip', None), getattr(self, '_admin_shield', None)):
            if chip is None:
                continue
            with suppress(Exception):
                if hasattr(chip, 'configure'):
                    chip.configure(chrome)
                else:
                    chip.update()
            with suppress(Exception):
                chip.update()

    def _apply_taskbar_icon(self) -> None:
        if sys.platform != 'win32':
            return
        path = _exe_ico_path()
        if not path:
            return
        try:
            if getattr(self, '_native_icons', None) is None:
                self._native_icons = (_load_native_icon(path, True), _load_native_icon(path, False))
            big, small = self._native_icons
            hwnd = int(self.winId())
            send = ctypes.windll.user32.SendMessageW
            send.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_void_p, ctypes.c_void_p]
            if big:
                send(hwnd, _WM_SETICON, _ICON_BIG, big)
            if small:
                send(hwnd, _WM_SETICON, _ICON_SMALL, small)
        except Exception:
            logger.warning('Failed to apply taskbar icon', exc_info=True)

    def _init_tray_icon(self):
        if not QSystemTrayIcon.isSystemTrayAvailable():
            return
        _tray = _exe_ico_path()
        self._tray_icon = QSystemTrayIcon(QIcon(_tray) if _tray else self.windowIcon(), self)
        self._tray_icon.setToolTip('PiraChest')
        menu = QMenu()
        show_action = QAction('Show', self)
        show_action.triggered.connect(self._restore_from_tray)
        menu.addAction(show_action)
        menu.addSeparator()
        exit_action = QAction('Exit', self)
        exit_action.triggered.connect(self._quit_from_tray)
        menu.addAction(exit_action)
        self._tray_icon.setContextMenu(menu)
        self._tray_icon.activated.connect(self._on_tray_activated)
        self._tray_icon.show()

    def _on_tray_activated(self, reason):
        if reason in (QSystemTrayIcon.ActivationReason.Trigger, QSystemTrayIcon.ActivationReason.DoubleClick):
            self._restore_from_tray()

    def _restore_from_tray(self):
        self.showNormal() if not self.isMaximized() else self.showMaximized()
        self.raise_()
        self.activateWindow()

    def _quit_from_tray(self):
        self._force_quit = True
        self.close()

    def _has_active_downloads(self) -> bool:
        try:
            from src.core.downloader import DLState
            items = self.download_manager.items_in_order()
            return any(item.state in (DLState.queued, DLState.downloading, DLState.verifying) for item in items)
        except Exception:
            try:
                summary = self.download_manager.summary()
                return bool(summary.get('active')) or bool(summary.get('queued'))
            except Exception:
                return False

    @staticmethod
    def tr_or(key: str, fallback: str) -> str:
        try:
            value = tr(key)
        except Exception:
            return fallback
        if not value or value == key:
            return fallback
        return value

    def _prompt_close_with_active_downloads(self) -> str:
        try:
            box = MessageBoxBase(self)
            box_layout = QVBoxLayout()
            box_layout.addWidget(StrongBodyLabel(self.tr_or('app.close_warning_title', 'Downloads in progress')))
            content_lbl = BodyLabel(self.tr_or(
                'app.close_warning_content',
                'A file is still downloading. Closing the app now can interrupt it and leave it incomplete or corrupted.'
            ))
            content_lbl.setWordWrap(True)
            box_layout.addWidget(content_lbl)
            box.viewLayout.addLayout(box_layout)
            choice = {'value': 'cancel'}

            def _pick(val):
                choice['value'] = val
                box.accept()
            with suppress(TypeError):
                box.yesButton.clicked.disconnect()
            with suppress(TypeError):
                box.cancelButton.clicked.disconnect()
            box.yesButton.setText(self.tr_or('app.close_anyway', 'Close Anyway'))
            box.yesButton.clicked.connect(lambda: _pick('close'))
            box.cancelButton.setText(self.tr_or('app.close_cancel', 'Keep Downloading'))
            box.cancelButton.clicked.connect(lambda: _pick('cancel'))
            if self._tray_icon is not None:
                tray_btn = PushButton(self.tr_or('app.close_to_tray_instead', 'Minimize to Tray'))
                tray_btn.clicked.connect(lambda: _pick('tray'))
                button_row = None
                try:
                    button_row = box.cancelButton.parentWidget().layout()
                except Exception:
                    button_row = None
                if button_row is not None:
                    button_row.insertWidget(0, tray_btn)
                else:
                    box.viewLayout.addWidget(tray_btn)
            box.widget.setMinimumWidth(360)
            box.exec()
            return choice['value']
        except Exception:
            logging.exception('Failed to show the close-with-active-downloads dialog; falling back to a plain confirm box')
            try:
                buttons = QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
                res = QMessageBox.warning(
                    self,
                    'Downloads in progress',
                    'A file is still downloading. Closing now can interrupt it. Close anyway?',
                    buttons,
                    QMessageBox.StandardButton.No,
                )
                return 'close' if res == QMessageBox.StandardButton.Yes else 'cancel'
            except Exception:
                return 'cancel'

    def _on_chrome_applied(self) -> None:
        self._style_title_chips()
        with suppress(Exception):
            chrome = self._chrome
            transparent = bool(chrome is not None and getattr(chrome, '_transparent', False))
            shader_bg = self._shader_bg
            if shader_bg is not None:
                shader_bg.set_base_color(QColor(palette()['page_bg']))
                shader_bg.set_transparent_window(transparent)
        rect = self._shader_bg_rect()
        shader_bg = self._shader_bg
        if shader_bg is not None:
            shader_bg.setGeometry(rect)
            shader_bg.lower()
        media_bg = self._media_bg
        if media_bg is not None and media_bg.isVisible():
            media_bg.setGeometry(rect)
            media_bg.lower()
        self.update()

    def _shader_bg_rect(self):
        return self._bg_scope_rect()

    def _bg_scope_rect(self, framed: bool | None = None):
        try:
            scope = theme_background_scope(active_preset_id())
        except Exception:
            scope = "client"
        if scope == "window":
            return self.rect()
        if scope == "content":
            with suppress(Exception):
                stacked = self.stackedWidget
                top_left = stacked.mapTo(self, QPoint(0, 0))
                return QRect(top_left.x(), top_left.y(), stacked.width(), stacked.height())
        chrome = self._chrome
        if chrome is None:
            return self.rect()
        if framed is None:
            try:
                framed = chrome.current.mode == "frame"
            except Exception:
                framed = False
        return chrome.client_rect() if framed else self.rect()

    def _sync_splash(self) -> None:
        sp = getattr(self, 'splashScreen', None)
        if sp is None:
            return
        with suppress(Exception):
            if _qt_is_valid(sp):
                sp.setGeometry(0, 0, self.width(), self.height())
                sp.raise_()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._sync_splash()
        chrome = self._chrome
        if chrome is not None:
            chrome.sync_geometry()

        overlay = self._update_overlay
        if overlay is not None and overlay.isVisible():
            overlay.setGeometry(self.rect())
        shader_bg = self._shader_bg
        if shader_bg is not None:
            shader_bg.setGeometry(self._shader_bg_rect())
            shader_bg.lower()
            if shader_bg.isVisible():
                shader_bg.note_native_loop(drive=False)
        media_bg = self._media_bg
        if media_bg is not None and media_bg.isVisible():
            media_bg.setGeometry(self._shader_bg_rect())
            media_bg.lower()

        timer = self._resize_settle_timer
        if timer is None:
            timer = QTimer(self)
            timer.setSingleShot(True)
            timer.timeout.connect(self._on_resize_settled)
            self._resize_settle_timer = timer
        timer.start(180)

    def _on_resize_settled(self) -> None:
        self.update()

    def moveEvent(self, event):
        super().moveEvent(event)
        shader_bg = self._shader_bg
        if shader_bg is not None:
            shader_bg.report_window_moved(self.pos())
            if shader_bg.isVisible():
                shader_bg.note_native_loop()

    def _on_app_state_changed(self, state) -> None:
        self._app_active = state == Qt.ApplicationState.ApplicationActive
        self._sync_background_active()

    def _sync_background_active(self) -> None:
        active = self._app_active and self.isVisible() and not self.isMinimized()
        shader_bg = self._shader_bg
        if shader_bg is not None:
            shader_bg.set_active(active)
        media_bg = self._media_bg
        if media_bg is not None:
            media_bg.set_active(active)

    def changeEvent(self, event):
        super().changeEvent(event)
        shader_bg = self._shader_bg
        if shader_bg is not None and event.type() == event.Type.WindowStateChange:
            shader_bg.setGeometry(self._shader_bg_rect())
            shader_bg.lower()
        if event.type() == event.Type.WindowStateChange:
            self._sync_background_active()
            minimized = bool(self.windowState() & Qt.WindowState.WindowMinimized)
            get_sound_manager().set_suspended(minimized)
            if minimized:
                controller = getattr(self, 'sailseek', None)
                if controller is not None:
                    with suppress(Exception):
                        controller.hide()
                self._release_idle_memory()

    def hideEvent(self, event):
        super().hideEvent(event)
        get_sound_manager().set_suspended(True)
        controller = getattr(self, 'sailseek', None)
        if controller is not None:
            with suppress(Exception):
                controller.hide()
        self._sync_background_active()
        self._release_idle_memory()

    def showEvent(self, event):
        super().showEvent(event)
        self._apply_taskbar_icon()
        get_sound_manager().set_suspended(False)
        sp = getattr(self, "splashScreen", None)
        if sp is not None:
            self._sync_splash()
            QTimer.singleShot(0, self._sync_splash)
        shader_bg = self._shader_bg
        if shader_bg is not None:
            shader_bg.setGeometry(self._shader_bg_rect())
            shader_bg.lower()
        media_bg = self._media_bg
        if media_bg is not None and media_bg.isVisible():
            media_bg.setGeometry(self._shader_bg_rect())
            media_bg.lower()
        self._sync_background_active()

    def _observe_konami(self, event) -> None:
        stamp = (event.timestamp(), event.key())
        if event.isAutoRepeat() or stamp == self._konami_stamp:
            return
        self._konami_stamp = stamp
        if event.modifiers() & (Qt.KeyboardModifier.ControlModifier
                                | Qt.KeyboardModifier.AltModifier
                                | Qt.KeyboardModifier.MetaModifier):
            return
        if QApplication.activeModalWidget() is not None:
            return
        with suppress(Exception):
            if self._kb_owns_arrows(QApplication.focusWidget()):
                return
        now = time.monotonic()
        if now < self._konami_cooldown:
            return
        if now - self._konami_seen > 2.2:
            self._konami_pos = 0
        self._konami_seen = now
        key = event.key()
        if key == _KONAMI_CODE[self._konami_pos]:
            self._konami_pos += 1
            if self._konami_pos == len(_KONAMI_CODE):
                self._konami_pos = 0
                self._konami_cooldown = now + 8.0
                QDesktopServices.openUrl(QUrl(_KONAMI_URL))
        else:
            self._konami_pos = 1 if key == _KONAMI_CODE[0] else 0

    def eventFilter(self, obj, event):
        et = event.type()
        if et == _ET_KEY:
            with suppress(Exception):
                self._observe_konami(event)
        if et not in _MAIN_FILTER_TYPES:
            return super().eventFilter(obj, event)
        if et == _ET_KEY:
            with suppress(Exception):
                if self._handle_nav_arrow_key(obj, event):
                    return True
            try:
                if self._handle_card_nav_key(obj, event):
                    return True
            except Exception:
                logger.exception('Keyboard navigation failed')
        elif et == _ET_FOCUS_IN:
            if getattr(self, '_kb_nav_active', False):
                with suppress(Exception):
                    self._on_kb_focus_in(obj)
        elif et == _ET_RESIZE:
            ring = getattr(obj, '_kb_ring', None)
            if ring is not None:
                with suppress(RuntimeError):
                    if ring.isVisible():
                        ring.setGeometry(obj.rect())
        else:
            self._on_app_mouse(et, event)
        return super().eventFilter(obj, event)

    def _on_app_mouse(self, et, event) -> None:
        if et == _ET_PRESS:
            self._kb_nav_active = False
            with suppress(Exception):
                self._kb_show_ring(None)
        shader_bg = self._shader_bg
        if shader_bg is None or not shader_bg.isVisible() or not shader_bg.uses_mouse:
            return
        if et == _ET_MOVE:
            clock = getattr(self, '_shader_mouse_clock', None)
            if clock is None:
                clock = self._shader_mouse_clock = QElapsedTimer()
                clock.start()
            elif clock.elapsed() < 16:
                return
            else:
                clock.restart()
            shader_bg.report_mouse_move(self.mapFromGlobal(event.globalPosition().toPoint()))
        else:
            shader_bg.report_mouse_click(self.mapFromGlobal(event.globalPosition().toPoint()))

    def show_update_overlay(self, title: str | None = None) -> None:
        self._update_overlay.show_overlay(title)

    def set_update_overlay_progress(self, downloaded: int, total: int) -> None:
        self._update_overlay.set_progress(downloaded, total)

    def set_update_overlay_indeterminate(self, title: str | None = None) -> None:
        if title is not None:
            self._update_overlay.set_title(title)
        self._update_overlay.set_indeterminate()

    def hide_update_overlay(self) -> None:
        self._update_overlay.hide_overlay()

    def closeEvent(self, event):
        close_to_tray = settings.close_to_tray
        has_active_downloads = (not self._force_quit) and self._has_active_downloads()
        if close_to_tray and (not self._force_quit) and (self._tray_icon is not None) and (not has_active_downloads):
            event.ignore()
            self.hide()
            return
        if has_active_downloads:
            choice = self._prompt_close_with_active_downloads()
            if choice == 'cancel':
                event.ignore()
                return
            if choice == 'tray':
                settings.close_to_tray = True
                apply_settings(close_to_tray=True)
                save_settings(settings)
                with suppress(Exception):
                    self.settings_page._chk_close_to_tray.setChecked(True)
                if self._tray_icon is not None:
                    event.ignore()
                    self.hide()
                    return
        self._stop_page_preload()
        controller = getattr(self, 'sailseek', None)
        if controller is not None:
            with suppress(Exception):
                controller.shutdown()
        with suppress(Exception):
            self.download_manager.shutdown()
        with suppress(Exception):
            update_controller.cancel_download()
        for lazy_page in (self.music_page, self.yt_page, self.tv_page, self.pc_games_page, self.tbhb_page, self.books_page):
            if lazy_page is not None:
                lazy_page.shutdown_now()
        self._shutdown_books_bridge()
        bridge, self.yt_bridge = self.yt_bridge, None
        if bridge is not None:
            with suppress(Exception):
                bridge.shutdown()
        from src.core.cache import cache
        cache.clear_namespace('repacks_page')
        cache.clear_namespace('repacks_details')
        cache.flush_all()
        if self._tray_icon is not None:
            self._tray_icon.hide()
        self._drop_downloads_badge()
        super().closeEvent(event)
        self.hide()
        from src.core.worker import shutdown_all_async

        def _quit():
            QApplication.quit()
        shutdown_all_async(_quit)

    def _on_settings_changed(self):
        with suppress(Exception):
            self._register_shortcuts()
        with suppress(Exception):
            self._sync_optional_pages()
        with suppress(Exception):
            if self.home_page:
                self.home_page.sync_model()
        with suppress(Exception):
            self.download_manager.set_global_limits(down_kbps=settings.speed_limit, up_kbps=settings.upload_speed_limit)
        with suppress(Exception):
            seed_minutes = settings.seed_time
            for item_id in list(getattr(self.download_manager, '_items', {}).keys()):
                with suppress(Exception):
                    self.download_manager.set_torrent_settings(item_id, seed_time_limit_min=seed_minutes)

    def _remove_subinterface(self, page: QWidget | None) -> None:
        if page is None:
            return
        with suppress(Exception):
            self.navigationInterface.removeWidget(page.objectName())
        with suppress(Exception):
            self.stackedWidget.removeWidget(page)
        page.deleteLater()

    def _create_music_page(self):
        from .music_page import MusicPage
        page = MusicPage()
        page.set_download_manager(self.download_manager)
        return page

    def _on_yt_ffmpeg_declined(self) -> None:
        self.settings_page._chk_youtube.setChecked(False)

    def _center_scroll_nav_items(self) -> None:
        try:
            layout = self.navigationInterface.panel.scrollLayout
        except Exception:
            logger.exception('Failed to access scrollable navigation layout')
            return
        try:
            for i in reversed(range(layout.count())):
                item = layout.itemAt(i)
                if item is not None and item.spacerItem() is not None:
                    layout.takeAt(i)
            layout.insertStretch(0, 1)
            layout.addStretch(1)
        except Exception:
            logger.exception('Failed to center scrollable navigation items')

    _OPTIONAL_PAGES = (
        ('home_page', 'minerva_enabled', 'minerva', FluentIcon.APPLICATION, 'minervaPage',
         lambda s: _make_minerva_page(s)),
        ('pc_games_page', 'pc_games_enabled', 'pc_games', FluentIcon.GAME, 'pcGamesPage',
         lambda s: LazyPage(_lazy_repacks(s), keep_alive=True, shutdown=_shutdown_repacks_page)),
        ('tbhb_page', 'tbhb_enabled', 'tbhb', FluentIcon.SEARCH, 'tbhbPage',
         lambda s: LazyPage(_lazy_tbhb(s), keep_alive=True, shutdown=_shutdown_page)),
        ('music_page', 'music_enabled', 'music', FluentIcon.MUSIC, 'musicPage',
         lambda s: LazyPage(s._create_music_page, keep_alive=True, shutdown=_shutdown_page)),
        ('books_page', 'books_enabled', 'books', FluentIcon.BOOK_SHELF, 'booksPage',
         lambda s: s._make_books_page()),
        ('yt_page', 'youtube_enabled', 'youtube', FluentIcon.PLAY, 'ytPage',
         lambda s: s._make_yt_page()),
        ('tv_page', 'tv_enabled', 'tv', FluentIcon.PROJECTOR, 'tvPageNav',
         lambda s: LazyPage(_lazy_tv, keep_alive=True, shutdown=_shutdown_page)),
    )

    def _build_books_page(self):
        from .books_page import BooksPage
        return BooksPage(self.download_manager, manga_download_bridge=self._books_manga_bridge)

    def _make_books_page(self) -> LazyPage:
        from src.core.books.manga.manager import MangaDownloadBridge
        self._books_manga_bridge = MangaDownloadBridge(self.download_manager, self)
        return LazyPage(
            self._build_books_page,
            keep_alive=True,
            shutdown=_shutdown_page,
        )

    def _make_yt_page(self) -> LazyPage:
        def build():
            from .yt_page import YtPage
            page = YtPage(self.yt_page)
            page.set_download_bridge(self._ensure_yt_bridge())
            return page
        return LazyPage(build, keep_alive=True, shutdown=_shutdown_page,
                        on_ready=lambda page: page.ffmpeg_declined.connect(self._on_yt_ffmpeg_declined))

    def _shutdown_books_bridge(self) -> None:
        bridge, self._books_manga_bridge = self._books_manga_bridge, None
        if bridge is not None:
            try:
                bridge.shutdown()
            except Exception:
                logger.exception('Failed to shut down manga download bridge')

    def _ensure_yt_bridge(self):
        bridge = self.yt_bridge
        if bridge is None:
            from src.core.yt.job import YtDownloadBridge
            bridge = self.yt_bridge = YtDownloadBridge(self.download_manager, self)
        return bridge

    def _start_page_preload(self) -> None:
        if getattr(self, '_preload_thread', None) is not None:
            return
        modules = [name for attr, name in _PAGE_MODULES
                   if getattr(self, attr, None) is not None]
        if not modules:
            return
        stop = threading.Event()
        self._preload_stop = stop

        def run() -> None:
            for name in modules:
                if stop.is_set():
                    return
                try:
                    importlib.import_module(name)
                except Exception:
                    logger.debug('Page preload failed for %s', name, exc_info=True)
                if stop.wait(0.10):
                    return

        thread = threading.Thread(target=run, name='page-preload', daemon=True)
        self._preload_thread = thread
        thread.start()

    def _stop_page_preload(self) -> None:
        stop = getattr(self, '_preload_stop', None)
        if stop is not None:
            stop.set()

    def _sync_optional_pages(self) -> None:
        for attr, flag, key, icon, object_name, build in self._OPTIONAL_PAGES:
            page, enabled = getattr(self, attr), getattr(settings, flag)
            if enabled and page is None:
                page = build(self)
                page.setObjectName(object_name)
                setattr(self, attr, page)
                setattr(self, f'_nav_{key}', self.addSubInterface(
                    page, themed_nav_icon(f'nav.{key}', icon), tr(f'nav.{key}'), position=NavigationItemPosition.SCROLL))
            elif not enabled and page is not None:
                if isinstance(page, LazyPage):
                    page.shutdown_now()
                self._remove_subinterface(page)
                setattr(self, attr, None)
                setattr(self, f'_nav_{key}', None)
                if attr == 'books_page':
                    self._shutdown_books_bridge()
        self._refresh_nav_icons()
        self._center_scroll_nav_items()
        if getattr(self, 'home_nav_page', None) is not None:
            self.home_nav_page.on_features_changed()

    def _on_open_data_folder(self) -> None:
        path = paths.app_data_dir
        try:
            os.makedirs(path, exist_ok=True)
            if sys.platform.startswith('win'):
                os.startfile(path)
            elif sys.platform == 'darwin':
                subprocess.Popen(['open', path])
            else:
                subprocess.Popen(['xdg-open', path])
        except Exception:
            logger.exception('Failed to open data folder %s', path)

    def _open_theme_maker_checked(self) -> None:
        if confirm_experimental_features(self):
            open_theme_maker(self)

    _FIXED_NAV = (
        ('home', 'homePage', FluentIcon.HOME),
        ('downloads', 'downloadPage', FluentIcon.DOWNLOAD),
        ('settings', 'settingsPage', FluentIcon.SETTING),
        ('about', 'aboutPage', FluentIcon.INFO),
    )

    def _nav_items(self):
        for key, route, icon in self._FIXED_NAV:
            yield getattr(self, f'_nav_{key}', None), route, key, icon
        for _attr, _flag, key, icon, *_rest in self._OPTIONAL_PAGES:
            yield getattr(self, f'_nav_{key}', None), None, key, icon

    def _refresh_nav_text(self) -> None:
        for widget, _route, key, _icon in self._nav_items():
            if widget is not None and key != 'home':
                with suppress(RuntimeError):
                    widget.setText(tr(f'nav.{key}'))
        with suppress(Exception):
            item = self.navigationInterface.widget('openDataFolder')
            if item is not None:
                item.setText(tr('nav.open_data_folder'))

    def _refresh_nav_icons(self) -> None:
        chrome = self._chrome
        for widget, route_key, key, icon in self._nav_items():
            if widget is None:
                continue
            slot_id = f'nav.{key}'
            with suppress(Exception):
                if chrome is not None and route_key:
                    with suppress(Exception):
                        chrome.note_nav_item(route_key, slot_id, icon)
                widget.setIcon(themed_nav_icon(slot_id, icon))
        with suppress(Exception):
            item = self.navigationInterface.widget('openDataFolder')
            if item is not None:
                if chrome is not None:
                    with suppress(Exception):
                        chrome.note_nav_item('openDataFolder', 'nav.open_data_folder', FluentIcon.FOLDER)
                item.setIcon(themed_nav_icon('nav.open_data_folder', FluentIcon.FOLDER))
        with suppress(Exception):
            if chrome is not None:
                chrome.refresh_nav()

    _PAGE_ORDER_ATTRS = ('home_nav_page', 'home_page', 'pc_games_page', 'tbhb_page',
                         'music_page', 'books_page', 'yt_page', 'tv_page',
                         'download_page', 'settings_page', 'about_page')

    def _ordered_pages(self) -> list:
        pages = []
        for attr in self._PAGE_ORDER_ATTRS:
            try:
                page = getattr(self, attr, None)
            except Exception:
                page = None
            if page is not None:
                pages.append(page)
        return pages

    def _init_keyboard_shortcuts(self) -> None:
        try:
            from PySide6.QtGui import QKeySequence, QShortcut
        except Exception:
            logger.exception('Failed to initialize keyboard shortcuts')
            return

        self._fixed_shortcuts = []

        def _add(key, handler):
            try:
                sc = QShortcut(QKeySequence(key), self)
                sc.setContext(Qt.ShortcutContext.WindowShortcut)
                sc.activated.connect(handler)
                self._fixed_shortcuts.append(sc)
            except Exception:
                logger.exception('Failed to register shortcut %s', key)

        _add('Alt+Left', lambda: self._cycle_page(-1))
        _add('Alt+Right', lambda: self._cycle_page(1))
        _add('F5', self._refresh_current_page)
        _add('F1', self._show_shortcuts_dialog)
        self._register_shortcuts()

    def _register_shortcuts(self) -> None:
        for sc in getattr(self, '_page_shortcuts', None) or []:
            with suppress(Exception):
                sc.setEnabled(False)
                sc.deleteLater()
        self._page_shortcuts = []
        mapping = shortcut_map()
        for action_id, _default, _label, attr in SHORTCUT_DEFAULTS:
            if action_id == 'sailseek':
                continue
            key = mapping[action_id]
            try:
                if attr is None:
                    handler = self._go_back_history
                else:
                    handler = lambda _a=attr: self._goto_shortcut_page(_a)
                sc = QShortcut(QKeySequence(key), self)
                sc.setContext(Qt.ShortcutContext.WindowShortcut)
                sc.activated.connect(handler)
                self._page_shortcuts.append(sc)
            except Exception:
                logger.exception('Failed to register shortcut %s', key)
        controller = getattr(self, 'sailseek', None)
        if controller is not None:
            with suppress(Exception):
                controller.apply_settings()

    def _set_page_shortcuts_enabled(self, enabled: bool) -> None:
        shortcuts = list(getattr(self, '_fixed_shortcuts', None) or [])
        shortcuts.extend(getattr(self, '_page_shortcuts', None) or [])
        theme_shortcut = getattr(self, '_theme_maker_shortcut', None)
        if theme_shortcut is not None:
            shortcuts.append(theme_shortcut)
        for sc in shortcuts:
            with suppress(Exception):
                sc.setEnabled(bool(enabled))
        controller = getattr(self, 'sailseek', None)
        if controller is not None:
            with suppress(Exception):
                controller.set_suspended(not enabled)

    @staticmethod
    def _focused_in_text_edit() -> bool:
        try:
            return isinstance(QApplication.focusWidget(), (QLineEdit, QTextEdit, QPlainTextEdit, QComboBox))
        except Exception:
            return False

    def _goto_shortcut_page(self, attr: str) -> None:
        if self._focused_in_text_edit():
            return
        try:
            page = getattr(self, attr, None)
        except Exception:
            return
        if page is None:
            return
        with suppress(Exception):
            self.switchTo(page)

    def _prune_history(self) -> list:
        try:
            stacked = self.stackedWidget
        except Exception:
            return []
        kept = []
        for widget in getattr(self, '_nav_history', None) or []:
            try:
                if widget is not None and _qt_is_valid(widget) and stacked.indexOf(widget) >= 0:
                    kept.append(widget)
            except Exception:
                continue
        self._nav_history = kept[-50:]
        return self._nav_history

    def _record_nav_history(self, _index: int = 0) -> None:
        try:
            current = self.stackedWidget.currentWidget()
        except Exception:
            return
        if current is None:
            return
        hist = self._prune_history()
        if not hist or hist[-1] is not current:
            hist.append(current)
            self._nav_history = hist[-50:]

    def _go_back_history(self) -> None:
        if self._focused_in_text_edit():
            return
        hist = self._prune_history()
        try:
            current = self.stackedWidget.currentWidget()
        except Exception:
            return
        while hist and hist[-1] is current:
            hist.pop()
        if not hist:
            return
        target = hist.pop()
        self._nav_history = hist
        with suppress(Exception):
            self.switchTo(target)

    def _cycle_page(self, delta: int) -> None:
        pages = self._ordered_pages()
        if not pages:
            return
        try:
            current = self.stackedWidget.currentWidget()
        except Exception:
            return
        start = pages.index(current) if current in pages else (0 if delta > 0 else len(pages) - 1)
        with suppress(Exception):
            self.switchTo(pages[(start + delta) % len(pages)])

    def _refresh_current_page(self) -> None:
        try:
            current = self.stackedWidget.currentWidget()
        except Exception:
            return
        target = current
        with suppress(Exception):
            inner = getattr(current, 'page', None)
            if inner is not None:
                target = inner
        for name in ('refresh', 'on_features_changed'):
            fn = getattr(target, name, None)
            if callable(fn):
                try:
                    fn()
                except Exception:
                    logger.exception('Page refresh shortcut failed')
                return

    def _handle_nav_arrow_key(self, obj, event) -> bool:
        key = event.key()
        if key not in (Qt.Key.Key_Up, Qt.Key.Key_Down):
            return False
        try:
            nav = self.navigationInterface
            if nav is None or not nav.isAncestorOf(obj):
                return False
        except Exception:
            return False
        self._cycle_page(-1 if key == Qt.Key.Key_Up else 1)
        return True

    @staticmethod
    def _kb_native_input(widget) -> bool:
        try:
            return isinstance(widget, (QAbstractItemView, QAbstractScrollArea, QAbstractSpinBox,
                                       QComboBox, QDial, QLineEdit, QPlainTextEdit, QSlider,
                                       QTextEdit))
        except Exception:
            return False

    @staticmethod
    def _is_kb_card(widget) -> bool:
        if not isinstance(widget, QWidget):
            return False
        try:
            if not widget.isEnabled() or not widget.isVisible():
                return False
        except RuntimeError:
            return False
        if getattr(widget, '_pirachest_no_kb_nav', False):
            return False
        if getattr(widget, '_pirachest_kb_card', False):
            return True
        name = type(widget).__name__
        if name.endswith(('Card', 'Tile', 'Row')):
            return True
        with suppress(Exception):
            if isinstance(widget, CardWidget):
                return True
        return False

    @staticmethod
    def _kb_center(widget):
        r = widget.rect()
        tl = widget.mapToGlobal(r.topLeft())
        return (tl.x() + r.width() / 2.0, tl.y() + r.height() / 2.0)

    @staticmethod
    def _kb_order_point(widget):
        x, y = MainWindow._kb_center(widget)
        child = widget
        parent = widget.parentWidget()
        while parent is not None:
            if isinstance(parent, QAbstractScrollArea):
                with suppress(Exception):
                    if child is parent.viewport():
                        x += parent.horizontalScrollBar().value()
                        y += parent.verticalScrollBar().value()
            child = parent
            parent = parent.parentWidget()
        return (x, y)

    def _kb_cards(self, focus):
        try:
            root = self.stackedWidget.currentWidget()
        except Exception:
            root = None
        if root is None:
            root = focus
        if root is None:
            return [], None

        scroll, w = None, focus
        while w is not None:
            if isinstance(w, QAbstractScrollArea):
                scroll = w
            w = w.parentWidget()
        if scroll is None:

            biggest = 0
            for area in root.findChildren(QAbstractScrollArea):
                try:
                    vp = area.viewport()
                    size = vp.width() * vp.height()
                except Exception:
                    continue
                if size > biggest:
                    biggest, scroll = size, area

        viewport = None
        bounds = root
        if scroll is not None:
            try:
                viewport = scroll.viewport()
                bounds = scroll.widget() or root
            except Exception:
                viewport = None
        try:
            found = bounds.findChildren(QWidget)
        except Exception:
            return [], scroll
        cards = []
        for candidate in found:
            if not self._is_kb_card(candidate):
                continue
            if viewport is not None and not viewport.isAncestorOf(candidate):
                continue
            if self._kb_is_clipped(candidate, bounds):
                continue
            cards.append(candidate)
        if len(cards) > 1 and len(cards) < 500:

            cards = [c for c in cards if not any(o is not c and c.isAncestorOf(o) for o in cards)]
        for card in cards:

            with suppress(Exception):
                if card.focusPolicy() == Qt.FocusPolicy.NoFocus:
                    card.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        return cards, scroll

    @staticmethod
    def _kb_is_clipped(widget, bounds) -> bool:
        w = widget
        while w is not None and w is not bounds:
            try:
                if w.height() <= 1:
                    return True
            except RuntimeError:
                return True
            w = w.parentWidget()
        return False

    @staticmethod
    def _kb_owns_arrows(widget) -> bool:
        w = widget if isinstance(widget, QWidget) else None
        while isinstance(w, QWidget):
            if getattr(w, '_pirachest_owns_arrows', False):
                return True
            w = w.parentWidget()
        return False

    @staticmethod
    def _kb_is_stop(widget) -> bool:
        if not isinstance(widget, QWidget):
            return False
        try:
            if not widget.isEnabled() or not widget.isVisible():
                return False
        except RuntimeError:
            return False
        if getattr(widget, '_pirachest_no_kb_nav', False):
            return False
        if type(widget).__name__ in _KB_DECORATIVE:
            return False
        if MainWindow._kb_in_scroll_chrome(widget):
            return False
        if isinstance(widget, QAbstractItemView):
            stoppable = True
        elif isinstance(widget, QAbstractScrollArea):
            stoppable = False
        else:
            stoppable = widget.focusPolicy() != Qt.FocusPolicy.NoFocus or MainWindow._is_kb_card(widget)
            if not stoppable and isinstance(widget, QAbstractButton):

                try:
                    widget.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
                    stoppable = True
                except Exception:
                    stoppable = False
        if not stoppable:
            return False
        return not MainWindow._kb_is_clipped(widget, None)

    @staticmethod
    def _kb_in_scroll_chrome(widget) -> bool:
        parent = widget.parentWidget()
        while parent is not None:
            if isinstance(parent, QScrollBar) or 'ScrollBar' in type(parent).__name__:
                return True
            parent = parent.parentWidget()
        return False

    @staticmethod
    def _kb_reading_order(items: list) -> list:
        points = [(MainWindow._kb_order_point(w), w) for w in items]
        points.sort(key=lambda p: p[0][1])
        rows = []
        for (cx, cy), widget in points:
            height = max(widget.height(), 1)
            if rows:
                tolerance = max(16, min(height, rows[-1][1]) // 2)
                if abs(cy - rows[-1][0]) <= tolerance:
                    rows[-1][2].append((cx, widget))
                    continue
            rows.append([cy, height, [(cx, widget)]])
        ordered = []
        for _anchor, _height, entries in rows:
            entries.sort(key=lambda e: e[0])
            ordered.extend(widget for _cx, widget in entries)
        return ordered

    def _kb_tab_stops(self) -> list:
        try:
            found = self.findChildren(QWidget)
        except Exception:
            return []
        nav = getattr(self, 'navigationInterface', None)
        stops = []
        for w in found:
            try:
                in_rail = nav is not None and nav.isAncestorOf(w)
            except Exception:
                in_rail = False
            if in_rail and w.focusPolicy() == Qt.FocusPolicy.NoFocus and \
                    'Arrow' not in type(w).__name__ and \
                    (isinstance(w, QAbstractButton) or hasattr(w, 'clicked')):

                with suppress(Exception):
                    w.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
            if self._kb_is_stop(w):
                stops.append(w)
        if not stops:
            return []
        pool = set(stops)
        drop = set()
        for w in stops:
            parent = w.parentWidget()
            while parent is not None:
                if parent in pool:
                    drop.add(parent)
                parent = parent.parentWidget()
        stops = [w for w in stops if w not in drop]
        rail, body = [], []
        for w in stops:
            try:
                in_rail = nav is not None and nav.isAncestorOf(w)
            except Exception:
                in_rail = False
            (rail if in_rail else body).append(w)
        rail.sort(key=lambda w: self._kb_center(w)[1])
        return rail + self._kb_reading_order(body)

    def _kb_nearest_stop(self, stops: list, focus):
        if not isinstance(focus, QWidget) or focus.window() is not self:
            return stops[0]
        point = self._kb_order_point(focus)
        best, best_d = None, None
        for widget in stops:
            cx, cy = self._kb_order_point(widget)
            d = (cx - point[0]) ** 2 + (cy - point[1]) ** 2
            if best_d is None or d < best_d:
                best, best_d = widget, d
        return best

    def _kb_tab(self, backwards: bool) -> bool:
        stops = self._kb_tab_stops()
        if not stops:
            return False
        focus = self.focusWidget()
        target = None
        if isinstance(focus, QWidget):
            try:
                index = stops.index(focus)
            except ValueError:
                index = -1
            if index >= 0:
                target = stops[(index + (-1 if backwards else 1)) % len(stops)]
            else:
                target = self._kb_nearest_stop(stops, focus)
        else:
            target = stops[-1] if backwards else stops[0]
        if target is None or target is focus:
            return False
        self._kb_focus_widget(target, None)
        return True

    def _kb_main_scroll(self):
        try:
            root = self.stackedWidget.currentWidget()
        except Exception:
            root = None
        if root is None:
            return None
        best, best_size = None, 0
        for area in root.findChildren(QAbstractScrollArea):
            try:
                vp = area.viewport()
                size = vp.width() * vp.height()
            except Exception:
                continue
            if size > best_size:
                best, best_size = area, size
        return best

    def _kb_scroll_step(self, key) -> bool:
        areas, seen = [], set()

        def _add(area):
            if area is not None and id(area) not in seen:
                seen.add(id(area))
                areas.append(area)

        w = self.focusWidget()
        while w is not None:
            if isinstance(w, QAbstractScrollArea):
                _add(w)
            w = w.parentWidget()
        _add(self._kb_main_scroll())
        for area in areas:
            try:
                viewport = area.viewport()
                vertical, horizontal = area.verticalScrollBar(), area.horizontalScrollBar()
                if key in (Qt.Key.Key_Up, Qt.Key.Key_Down):
                    if vertical.maximum() <= vertical.minimum():
                        continue
                    step = max(80, viewport.height() // 2)
                    if not self._kb_set_bar(vertical, vertical.value() + step * (-1 if key == Qt.Key.Key_Up else 1)):
                        continue
                else:
                    if horizontal.maximum() <= horizontal.minimum():
                        continue
                    step = max(80, viewport.width() * 2 // 3)
                    if not self._kb_set_bar(horizontal, horizontal.value() + step * (-1 if key == Qt.Key.Key_Left else 1)):
                        continue
                return True
            except Exception:
                continue
        return False

    def _kb_scroll_key(self, key) -> bool:
        scroll = self._kb_main_scroll()
        if scroll is None:
            return False
        try:
            bar = scroll.verticalScrollBar()
            viewport = scroll.viewport()
            if bar.maximum() <= bar.minimum():
                return False
            if key == Qt.Key.Key_PageDown:
                self._kb_set_bar(bar, bar.value() + viewport.height() - 48)
            elif key == Qt.Key.Key_PageUp:
                self._kb_set_bar(bar, bar.value() - viewport.height() + 48)
            elif key == Qt.Key.Key_Home:
                self._kb_set_bar(bar, bar.minimum())
            elif key == Qt.Key.Key_End:
                self._kb_set_bar(bar, bar.maximum())
            return True
        except Exception:
            return False

    @staticmethod
    def _kb_pick(cards, point, key):
        dx = {Qt.Key.Key_Left: -1.0, Qt.Key.Key_Right: 1.0}.get(key, 0.0)
        dy = {Qt.Key.Key_Up: -1.0, Qt.Key.Key_Down: 1.0}.get(key, 0.0)
        best, best_score = None, None
        for card in cards:
            cx, cy = MainWindow._kb_order_point(card)
            vx, vy = cx - point[0], cy - point[1]
            primary = vx * dx + vy * dy
            if primary <= 1.0:
                continue
            secondary = abs(vx * dy + vy * dx)
            score = primary + secondary * 3.0
            if best_score is None or score < best_score:
                best, best_score = card, score
        return best

    @staticmethod
    def _kb_set_bar(bar, value) -> bool:
        try:
            value = int(round(value))
            value = max(bar.minimum(), min(bar.maximum(), value))
            if value == bar.value():
                return False
            bar.setValue(value, useAni=False)
        except TypeError:
            try:
                bar.setValue(value)
            except Exception:
                return False
        except Exception:
            return False
        return True

    @staticmethod
    def _kb_reveal_axis(bar, lo: int, hi: int, extent: int, margin: int) -> None:
        if hi - lo >= extent:
            delta = (lo + hi - extent) * 0.5
        elif lo < margin:
            delta = lo - margin
        elif hi > extent - margin:
            delta = hi - extent + margin
        else:
            return
        MainWindow._kb_set_bar(bar, bar.value() + delta)

    @staticmethod
    def _kb_reveal(area, top, bottom) -> None:
        try:
            vp = area.viewport()
            tl = vp.mapFromGlobal(top)
            br = vp.mapFromGlobal(bottom)
            vw, vh = vp.width(), vp.height()
            hbar, vbar = area.horizontalScrollBar(), area.verticalScrollBar()
        except Exception:
            return
        MainWindow._kb_reveal_axis(hbar, tl.x(), br.x(), vw, min(48, max(8, vw // 5)))
        MainWindow._kb_reveal_axis(vbar, tl.y(), br.y(), vh, min(48, max(8, vh // 5)))

    @staticmethod
    def _kb_scroll_into_view(widget, scroll=None) -> None:
        try:
            top = widget.mapToGlobal(widget.rect().topLeft())
            bottom = widget.mapToGlobal(widget.rect().bottomRight())
        except RuntimeError:
            return
        seen = set()
        w = widget
        while w is not None:
            if isinstance(w, QAbstractScrollArea):
                seen.add(id(w))
                MainWindow._kb_reveal(w, top, bottom)
            w = w.parentWidget()
        if scroll is not None and id(scroll) not in seen:
            MainWindow._kb_reveal(scroll, top, bottom)

    def _kb_focus_widget(self, widget, scroll=None) -> None:
        with suppress(Exception):
            if widget.focusPolicy() == Qt.FocusPolicy.NoFocus:
                widget.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self._kb_nav_active = True
        with suppress(Exception):
            widget.setFocus(Qt.FocusReason.TabFocusReason)
        self._kb_scroll_into_view(widget, scroll)

    @staticmethod
    def _kb_activate(widget) -> None:
        audible = isinstance(widget, QAbstractButton) or hasattr(widget, 'clicked')
        if not audible:
            get_sound_manager().play('click')

        target = widget
        primary = widget if isinstance(widget, QAbstractButton) else None
        if primary is None and callable(getattr(widget, 'click', None)):
            primary = widget
        if primary is None:

            for attr in ('switchButton', 'expandButton', 'button'):
                child = getattr(widget, attr, None)
                if child is None:
                    continue
                target = child
                if callable(getattr(child, 'click', None)):
                    primary = child
                    break
                if hasattr(child, 'isChecked') and callable(getattr(child, 'setChecked', None)):

                    try:
                        child.setChecked(not child.isChecked())
                    except Exception:
                        logger.exception('Keyboard activation failed')
                    return
                break
        if primary is not None:
            with suppress(Exception):
                primary.click()
                return
        try:
            center = QPointF(target.rect().center())
            press = QMouseEvent(QEvent.Type.MouseButtonPress, center, center,
                                Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton,
                                Qt.KeyboardModifier.NoModifier)
            release = QMouseEvent(QEvent.Type.MouseButtonRelease, center, center,
                                  Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton,
                                  Qt.KeyboardModifier.NoModifier)
            QApplication.sendEvent(target, press)
            QApplication.sendEvent(target, release)
        except Exception:
            logger.exception('Keyboard activation failed')

    @staticmethod
    def _kb_set_hover(widget, value: bool) -> None:
        with suppress(Exception):
            if hasattr(widget, 'isHover') and hasattr(widget, '_updateBackgroundColor'):
                widget.isHover = bool(value)
                widget._updateBackgroundColor()

    def _kb_show_ring(self, card) -> None:
        host = getattr(self, '_kb_ring_host', None)
        if host is not None and host is not card:
            with suppress(RuntimeError):
                self._kb_set_hover(host, host.underMouse())
                ring = getattr(host, '_kb_ring', None)
                if ring is not None:
                    ring.hide()
        self._kb_ring_host = card
        if card is None:
            return
        ring = getattr(card, '_kb_ring', None)
        if ring is None:
            try:
                ring = _KeyboardFocusRing(card)
                card._kb_ring = ring
            except Exception:
                return
        with suppress(RuntimeError):
            ring.setGeometry(card.rect())
            ring.show()
            ring.raise_()
            self._kb_set_hover(card, True)

    def _on_kb_focus_in(self, obj) -> None:
        if not getattr(self, '_kb_nav_active', False) or not isinstance(obj, QWidget):
            return
        try:
            if obj.window() is not self:
                return
        except Exception:
            return
        target = obj if self._kb_is_stop(obj) else None
        self._kb_show_ring(target)
        if target is not None and target is not getattr(self, '_kb_last_focus', None):
            get_sound_manager().play('hover')
        self._kb_last_focus = obj

    def _handle_card_nav_key(self, obj, event) -> bool:
        key = event.key()
        mods = event.modifiers()
        arrows = (Qt.Key.Key_Left, Qt.Key.Key_Right, Qt.Key.Key_Up, Qt.Key.Key_Down)
        enter_keys = (Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_Space)
        scroll_keys = (Qt.Key.Key_PageUp, Qt.Key.Key_PageDown, Qt.Key.Key_Home, Qt.Key.Key_End)
        is_tab = key in (Qt.Key.Key_Tab, Qt.Key.Key_Backtab)

        focus = self.focusWidget()
        if not isinstance(focus, QWidget):
            focus = obj if isinstance(obj, QWidget) else None
        if focus is None:
            return False
        try:
            if focus.window() is not self:
                return False
        except Exception:
            return False
        if key in arrows and self._kb_owns_arrows(focus):
            return False
        try:
            nav = self.navigationInterface
            in_nav = nav is not None and nav.isAncestorOf(focus)
        except Exception:
            in_nav = False
        if in_nav and key in (Qt.Key.Key_Left, Qt.Key.Key_Right):
            return True
        with suppress(Exception):
            if focus.isKeyboardGrabber():
                return False

        if is_tab:
            if mods & ~Qt.KeyboardModifier.ShiftModifier:
                return False
            backwards = key == Qt.Key.Key_Backtab or bool(mods & Qt.KeyboardModifier.ShiftModifier)
            return self._kb_tab(backwards)

        if mods not in (Qt.KeyboardModifier.NoModifier, Qt.KeyboardModifier.KeypadModifier):
            return False
        if key in scroll_keys:
            if self._focused_in_text_edit() or self._kb_native_input(focus):
                return False
            return self._kb_scroll_key(key)
        if key not in arrows and key not in enter_keys:
            return False
        if self._focused_in_text_edit() or self._kb_native_input(focus):
            return False

        if key in enter_keys:
            if not (self._is_kb_card(focus) or callable(getattr(focus, 'click', None))):
                return False
            self._kb_activate(focus)
            return True

        cards, scroll = self._kb_cards(focus)
        if not cards:
            return False
        on_card = self._is_kb_card(focus)
        point = self._kb_order_point(focus)
        target = self._kb_pick([c for c in cards if c is not focus] if on_card else cards, point, key)
        if target is None:
            if not on_card:
                return False

            return self._kb_scroll_step(key)
        self._kb_focus_widget(target, scroll)
        return True

    def _show_shortcuts_dialog(self) -> None:
        if getattr(self, '_shortcuts_dialog_open', False):
            return
        try:
            box = MessageBoxBase(self)
            self._shortcuts_dialog_open = True
            box.titleLabel = StrongBodyLabel(self.tr_or('app.shortcuts_title', 'Keyboard shortcuts'), box)
            box.viewLayout.addWidget(box.titleLabel)
            mapping = shortcut_map()
            labels = {aid: label for aid, _key, label, _attr in SHORTCUT_DEFAULTS}
            rows = [(mapping[aid].replace('+', ' + '), labels[aid]) for aid in mapping]
            rows += [
                ('Tab / Shift + Tab', self.tr_or('app.shortcuts_tab', 'Next / previous control')),
                ('Arrow keys', self.tr_or('app.shortcuts_arrows', 'Move between cards, scroll at the edges')),
                ('Enter / Space', self.tr_or('app.shortcuts_open', 'Open the focused card')),
                ('PageUp / PageDown / Home / End', self.tr_or('app.shortcuts_scroll', 'Scroll the page')),
                ('Up / Down', self.tr_or('app.shortcuts_nav_arrows', 'Move between pages (sidebar focused)')),
                ('Alt + Left / Right', self.tr_or('app.shortcuts_cycle', 'Previous / next page')),
                ('Esc', self.tr_or('app.shortcuts_back', 'Back to the previous page')),
                ('F5', self.tr_or('app.shortcuts_refresh', 'Refresh current page')),
                ('F1', self.tr_or('app.shortcuts_help', 'Show this dialog')),
            ]
            for keys, desc in rows:
                row = QHBoxLayout()
                key_lbl = StrongBodyLabel(keys, box)
                desc_lbl = BodyLabel(desc, box)
                desc_lbl.setWordWrap(True)
                row.addWidget(key_lbl)
                row.addWidget(desc_lbl, 1)
                box.viewLayout.addLayout(row)
            box.yesButton.setText(self.tr_or('app.close', 'Close'))
            box.hideCancelButton()
            box.widget.setMinimumWidth(420)
            box.exec()
        except Exception:
            logger.exception('Failed to show shortcuts dialog')
        finally:
            self._shortcuts_dialog_open = False

    def _load_filters(self):
        if self.home_page is None:
            return
        with suppress(Exception):
            self.home_page.refresh_filters()
        with suppress(Exception):
            self.home_page.sync_model()

    def _auto_sync(self):
        if self.home_page is None:
            return
        if settings.sync_prompt_shown:
            return
        from src.core.minerva import database as db
        try:
            count = db.count_roms()
        except Exception:
            count = 0
        if count != 0:
            return
        settings.sync_prompt_shown = True
        with suppress(Exception):
            save_settings(settings)
        card = SyncPromptCard(self)
        card.sync_requested.connect(self.home_page.run_sync)
        card.show_anchored(self)

    def _check_update_on_startup(self):
        update_controller.disconnect_listeners()
        update_controller.check_finished.connect(self._on_startup_update_check_done)
        update_controller.check()

    def _on_startup_update_check_done(self, result):
        if result is None:
            return
        if settings.skip_update_tag == result['tag']:
            return
        choice = show_update_dialog(self, result, allow_skip=True)
        if choice != 'update':
            return
        update_controller.disconnect_listeners()
        update_controller.download_progress.connect(self._on_startup_update_download_progress)
        update_controller.download_finished.connect(self._on_startup_update_download_finished)
        update_controller.download_error.connect(self._on_startup_update_download_error)
        self.show_update_overlay(tr('update.downloading'))
        update_controller.download(result)

    def _on_startup_update_download_progress(self, downloaded: int, total: int):
        self.set_update_overlay_progress(downloaded, total)

    def _on_startup_update_download_finished(self, path: str):
        self.set_update_overlay_indeterminate(tr('update.restarting'))
        try:
            update_controller.apply_and_restart(path, self)
        except Exception:
            logger.exception('Failed to apply update')
            self.hide_update_overlay()
            InfoBar.error(title=tr('update.failed'), content='', orient=Qt.Orientation.Horizontal, isClosable=True, position=InfoBarPosition.TOP_RIGHT, duration=3500, parent=self)

    def _on_startup_update_download_error(self, msg: str):
        self.hide_update_overlay()
        InfoBar.error(title=tr('update.download_failed'), content=msg, orient=Qt.Orientation.Horizontal, isClosable=True, position=InfoBarPosition.TOP_RIGHT, duration=3500, parent=self)

class SyncPromptCard(CardWidget):
    sync_requested = Signal()
    dismissed = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedWidth(360)

        self.setBorderRadius(8)
        self.setBackgroundColor(QColor(palette()['dialog_bg']))
        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 16, 18, 16)
        layout.setSpacing(10)
        header = QHBoxLayout()
        header.setSpacing(10)
        icon = IconWidget(themed_icon('app.sync_prompt_info', FluentIcon.INFO), self)
        icon.setFixedSize(20, 20)
        header.addWidget(icon, 0, Qt.AlignmentFlag.AlignVCenter)
        self._title_lbl = StrongBodyLabel('')
        header.addWidget(self._title_lbl, 1, Qt.AlignmentFlag.AlignVCenter)
        self._close_btn = TransparentToolButton(themed_icon('app.sync_prompt_close', FluentIcon.CLOSE), self)
        apply_tool_button_theme(self._close_btn)
        self._close_btn.setFixedSize(26, 26)
        self._close_btn.setIconSize(QSize(11, 11))
        self._close_btn.clicked.connect(self._on_close)
        header.addWidget(self._close_btn, 0, Qt.AlignmentFlag.AlignVCenter)
        layout.addLayout(header)
        self._body_lbl = BodyLabel('')
        self._body_lbl.setWordWrap(True)
        layout.addWidget(self._body_lbl)
        btn_row = QHBoxLayout()
        btn_row.setSpacing(8)
        btn_row.addStretch(1)
        self._sync_btn = PrimaryPushButton('')
        self._sync_btn.setFixedHeight(30)
        self._sync_btn.clicked.connect(self._on_sync_clicked)
        btn_row.addWidget(self._sync_btn)
        layout.addLayout(btn_row)
        self._apply_locale()
        register_locale_refresh(self, self._apply_locale)

    def _apply_locale(self):
        self._title_lbl.setText(tr('rom_index.empty_title'))
        self._body_lbl.setText(tr('rom_index.empty_content'))
        self._sync_btn.setText(tr('rom_index.sync_now'))

    def _normalBackgroundColor(self):
        return QColor(palette()['dialog_bg'])

    def _hoverBackgroundColor(self):
        return self._normalBackgroundColor()

    def _pressedBackgroundColor(self):
        return self._normalBackgroundColor()

    def _on_close(self):
        self.dismissed.emit()
        self._fade_out()

    def _on_sync_clicked(self):
        self.sync_requested.emit()
        self._fade_out()

    def _fade_out(self):
        effect = QGraphicsOpacityEffect(self)
        self.setGraphicsEffect(effect)
        anim = QPropertyAnimation(effect, b'opacity', self)
        anim.setDuration(160)
        anim.setStartValue(1.0)
        anim.setEndValue(0.0)
        anim.finished.connect(self.deleteLater)
        anim.start(QPropertyAnimation.DeletionPolicy.DeleteWhenStopped)
        self._fade_anim = anim

    def show_anchored(self, window: QWidget, margin: int=20, top: int=50) -> None:
        self.adjustSize()
        start_x = window.width() - self.width() - margin
        end_x = start_x
        self.move(start_x, top - 24)
        self.setWindowOpacity(0.0)
        self.show()
        self.raise_()
        play_popout_sfx('info')
        pos_anim = QPropertyAnimation(self, b'pos', self)
        pos_anim.setDuration(220)
        pos_anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        pos_anim.setStartValue(QPoint(start_x, top - 24))
        pos_anim.setEndValue(QPoint(end_x, top))
        opacity_anim = QPropertyAnimation(self, b'windowOpacity', self)
        opacity_anim.setDuration(220)
        opacity_anim.setStartValue(0.0)
        opacity_anim.setEndValue(1.0)
        pos_anim.start(QPropertyAnimation.DeletionPolicy.DeleteWhenStopped)
        opacity_anim.start(QPropertyAnimation.DeletionPolicy.DeleteWhenStopped)
        self._entry_anims = (pos_anim, opacity_anim)


def _build_app_font() -> QFont:
    font = QFont()
    font.setFamilies(['Figtree', 'Segoe UI Variable', 'Segoe UI', 'Inter', 'Noto Sans', 'Arial'])
    font.setPointSize(10)
    font.setStyleStrategy(
        QFont.StyleStrategy.PreferAntialias
        | QFont.StyleStrategy.PreferQuality
        | QFont.StyleStrategy.NoSubpixelAntialias
    )
    font.setHintingPreference(QFont.HintingPreference.PreferDefaultHinting)
    font.setKerning(False)
    font.setWeight(QFont.Weight.Normal)
    return font

def create_application(argv: list | None=None):
    setTheme(Theme.DARK)
    if sys.platform == 'win32':
        os.environ.setdefault('QT_OPENGL', 'desktop')
    surface_format = QSurfaceFormat()

    surface_format.setSwapInterval(0 if os.environ.get('PIRACHEST_VSYNC') == '0' else 1)
    surface_format.setSwapBehavior(QSurfaceFormat.SwapBehavior.DoubleBuffer)

    surface_format.setSamples(0)
    QSurfaceFormat.setDefaultFormat(surface_format)
    app = QApplication(argv if argv is not None else sys.argv)
    app.setLayoutDirection(Qt.LayoutDirection.LeftToRight)
    app.setApplicationName('PiraChest')
    app.setApplicationDisplayName('PiraChest')
    _logo = _logo_ico_path()
    if _logo:
        app.setWindowIcon(QIcon(_logo))
    app.setOrganizationName('PiraChest')
    app.setQuitOnLastWindowClosed(False)
    _load_bundled_figtree_fonts()
    apply_qfluentwidgets_font_families()
    base_font = _build_app_font()
    app.setFont(base_font)
    setFont(app, fontSize=base_font.pointSize())

    app.setStyleSheet(global_app_qss())
    with suppress(Exception):
        saved_preset = settings.theme_preset
        bg_hex = '#1c1c1c'
        if saved_preset:
            meta = preset_meta(saved_preset)
            if meta is not None:
                bg_hex = meta.palette.get('page_bg', bg_hex)
        pal = app.palette()
        pal.setColor(QPalette.ColorRole.Window, QColor(bg_hex))
        pal.setColor(QPalette.ColorRole.Base, QColor(bg_hex))
        app.setPalette(pal)
    try:
        if settings.log_to_file:
            set_file_logging_enabled(True)
    except Exception:
        logger.exception('Failed to initialize file logging')
    try:
        install_ui_sound_filter(app)
        install_global_tool_tip(app)
    except Exception:
        logger.exception('Failed to initialize sound manager')
    return app
