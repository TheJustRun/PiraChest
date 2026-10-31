from __future__ import annotations

import collections
import ctypes
import gc
import importlib
import logging
import math
import os
import re
import struct
import subprocess
import sys
import threading
import time
import weakref
from contextlib import suppress
from dataclasses import dataclass, field
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Callable

from PySide6.QtCore import (
    Property,
    QAbstractAnimation,
    QEasingCurve,
    QElapsedTimer,
    QEvent,
    qInstallMessageHandler,
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
    QtMsgType,
    QUrl,
    Signal,
)
from PySide6.QtGui import (
    QAction,
    QBrush,
    QColor,
    QCursor,
    QDesktopServices,
    QFont,
    QFontDatabase,
    QFontMetrics,
    QIcon,
    QImage,
    QImageReader,
    QKeySequence,
    QLinearGradient,
    QMatrix4x4,
    QMouseEvent,
    QMovie,
    QPainter,
    QPainterPath,
    QPalette,
    QPen,
    QPixmap,
    QPixmapCache,
    QShortcut,
    QSurfaceFormat,
)
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
from PySide6.QtOpenGLWidgets import (
    QOpenGLWidget,
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
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMenu,
    QMessageBox,
    QPlainTextEdit,
    QScrollArea,
    QScrollBar,
    QSizePolicy,
    QSlider,
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
    MessageBoxBase,
    NavigationItemPosition,
    Pivot,
    PrimaryPushButton,
    PushButton,
    qconfig,
    setFont,
    setTheme,
    Slider,
    SplashScreen,
    StrongBodyLabel,
    SubtitleLabel,
    Theme,
    themeColor,
    ToolTip,
    ToolTipPosition,
    TransparentToolButton,
)
from shiboken6 import (
    isValid as _qt_is_valid,
)

from src.core.config import (
    apply_settings,
    associations_available,
    associations_need_repair,
    check_dir_writable,
    download_dir_changes,
    DOWNLOAD_DIR_SUBFOLDERS,
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
    activate_theme,
    active_preset_id,
    all_preset_ids,
    apply_label_theme,
    apply_pivot_theme,
    apply_tool_button_theme,
    ChromeChip,
    ensure_contrast,
    filled_icon,
    global_app_qss,
    manager as theme_manager,
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
    themed_nav_icon,
    to_color,
    trimmed_icon,
    WindowChrome,
)
from src.core.translations import (
    current_language,
    register_locale_refresh,
    set_language,
    tr,
    tr_for,
)

from .home_page import (
    HomePage,
)
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
DISCORD_URL = 'https://discord.gg/j7dBxTZMDE'

_LOG_FORMAT = '%(asctime)s [%(levelname)s] %(name)s — %(message)s'
_LOG_FILE_HANDLER: logging.FileHandler | None = None
_PREV_EXCEPTHOOK = None
_QT_MESSAGE_HANDLER_INSTALLED = False
_LOG_MAX_BYTES = 2 * 1024 * 1024
_LOG_BACKUPS = 3

_REDACT_MAGNET_RE = re.compile(r'magnet:\?[^\s"\'<>]+', re.IGNORECASE)
_REDACT_URL_QUERY_RE = re.compile(r'(\b(?:https?|ftp)://[^\s"\'<>?]+)\?[^\s"\'<>]*', re.IGNORECASE)
_REDACT_URL_AUTH_RE = re.compile(r'(\b(?:https?|ftp)://)[^/\s"\'<>@]+@', re.IGNORECASE)
_REDACT_SECRET_RE = re.compile(
    r'(?i)\b(token|api[_-]?key|apikey|password|passwd|secret|auth|authorization|passkey)[=:]\s*[^\s&,;"\'<>]+')
_REDACT_BEARER_RE = re.compile(r'(?i)\b(bearer)\s+[A-Za-z0-9._~+/=-]{8,}')
_REDACT_WIN_USER_RE = re.compile(r'(?i)(\b[A-Z]:\\Users\\)[^\\\s"\']+')
_REDACT_UNIX_USER_RE = re.compile(r'(?i)(/home/)[^/\s"\']+')


def _redact_text(text: str) -> str:
    if not text:
        return text
    try:
        text = _REDACT_MAGNET_RE.sub('magnet:[redacted]', text)
        text = _REDACT_URL_QUERY_RE.sub(r'\1?<redacted>', text)
        text = _REDACT_URL_AUTH_RE.sub(r'\1<redacted>@', text)
        text = _REDACT_SECRET_RE.sub(lambda match: f'{match.group(1)}=<redacted>', text)
        text = _REDACT_BEARER_RE.sub(r'\1 <redacted>', text)
        text = _REDACT_WIN_USER_RE.sub(r'\1<user>', text)
        text = _REDACT_UNIX_USER_RE.sub(r'\1<user>', text)
    except Exception:
        pass
    return text


class _RedactingFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        return _redact_text(super().format(record))


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
        handler = RotatingFileHandler(
            path, maxBytes=_LOG_MAX_BYTES, backupCount=_LOG_BACKUPS, encoding='utf-8')
        handler.setFormatter(_RedactingFormatter(_LOG_FORMAT))
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
    formatter = _RedactingFormatter(_LOG_FORMAT)
    for handler in root_logger.handlers:
        handler.setFormatter(formatter)
    if enable_file:
        set_file_logging_enabled(True)


def _qt_message_handler(mode, context, message) -> None:
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
    qInstallMessageHandler(_qt_message_handler)
    _QT_MESSAGE_HANDLER_INSTALLED = True


def shutdown_logging() -> None:
    global _LOG_FILE_HANDLER
    if _LOG_FILE_HANDLER is not None:
        logging.getLogger().removeHandler(_LOG_FILE_HANDLER)
        _LOG_FILE_HANDLER.close()
        _LOG_FILE_HANDLER = None
    logging.shutdown()


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


_GUI_DIR = Path(__file__).resolve().parent
_FONT_DIR = _GUI_DIR / 'lang' / 'fonts'
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
        self.widget.setMinimumWidth(480)
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
        etiquette = StrongBodyLabel(t('disclaimer.etiquette', "Have a pirate's etiquette and know what you're doing. This app is for the lazy, not the stupid."), self.widget)
        etiquette.setWordWrap(True)
        self.viewLayout.addWidget(etiquette)
        self._chk_never_show = CheckBox(t('disclaimer.never_show', "Don't show this again"), self.widget)
        self.viewLayout.addWidget(self._chk_never_show)
        self.yesButton.setText(t('disclaimer.confirm', 'I understand'))
        self.cancelButton.hide()

    def never_show_again(self) -> bool:
        return self._chk_never_show.isChecked()


LANGUAGES = (
    ('en', 'English'),
    ('ar', 'العربية'),
    ('es', 'Español'),
    ('ru', 'Русский'),
    ('de', 'Deutsch'),
)

FEATURES = (
    ('minerva_enabled', 'nav.minerva', 'Minerva', 'onboarding.feature_minerva_desc',
     'Browse and sync the Minerva ROM index.'),
    ('pc_games_enabled', 'nav.pc_games', 'PC Games', 'onboarding.feature_pc_games_desc',
     'Download repacks and PC games.'),
    ('youtube_enabled', 'nav.youtube', 'YouTube', 'onboarding.feature_youtube_desc',
     'Download videos and audio from YouTube.'),
    ('tv_enabled', 'nav.tv', 'TV', 'onboarding.feature_tv_desc',
     'Browse movies, series and live channels.'),
    ('tbhb_enabled', 'nav.tbhb', 'TBHB', 'onboarding.feature_tbhb_desc',
     'Search torrents from The Black Hole Bay.'),
    ('music_enabled', 'nav.music', 'Music', 'onboarding.feature_music_desc',
     'Browse and manage your music library.'),
    ('books_enabled', 'nav.books', 'Books', 'onboarding.feature_books_desc',
     'Browse and manage your books.'),
    ('sailseek_enabled', 'nav.sailseek', 'SailSeek', 'onboarding.feature_sailseek_desc',
     'Search instantly and open YouTube links with one shortcut.'),
)

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
    base = max(12, round(fm.height() * 0.82))
    return _Metrics(
        compact=compact,
        base_px=base,
        title_px=round(base * (1.5 if compact else 1.82)),
        subtitle_px=base + 1,
        caption_px=max(10, base - 2),
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


class _Button(QAbstractButton):

    def __init__(self, text: str, kind: str, metrics: _Metrics, parent=None):
        super().__init__(parent)
        self._kind = kind
        self._metrics = metrics
        self._hover = False
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

    def enterEvent(self, event) -> None:
        self._hover = True
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:
        self._hover = False
        self.update()
        super().leaveEvent(event)

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
        hovered = self._hover or self.isDown()
        if self._kind == 'primary':
            fill = accent.lighter(108) if hovered else accent
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(fill)
            p.drawRoundedRect(rect, radius, radius)
            text_color = on_accent
        elif self._kind == 'ghost':
            if hovered:
                tint = accent
                tint.setAlphaF(0.08)
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(tint)
                p.drawRoundedRect(rect, radius, radius)
            text_color = to_color(pal.get('accent_text', pal.get('accent', '#0078d4')))
        else:
            fill = _mix(surface, to_color(pal.get('card_hover', '#242424')), 0.8 if hovered else 0.0)
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


class _ToggleCard(QAbstractButton):

    TRACK_W = 40.0
    TRACK_H = 22.0
    KNOB = 16.0

    def __init__(self, icon: QIcon, title: str, description: str, metrics: _Metrics, parent=None):
        super().__init__(parent)
        self._icon = icon
        self._title = title
        self._description = description
        self._metrics = metrics
        self._hover = False
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
        if not self.isVisible():
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
        return _font_px(self.font(), max(10, self._metrics.base_px - 1))

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

    def enterEvent(self, event) -> None:
        self._hover = True
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:
        self._hover = False
        self.update()
        super().leaveEvent(event)

    def paintEvent(self, _event) -> None:
        m = self._metrics
        pal = palette()
        accent = to_color(pal.get('accent', '#0078d4'))
        on_accent = to_color(pal.get('on_accent', '#111111'))
        bg = to_color(pal.get('card_bg', '#1b1b1b'))
        border = to_color(pal.get('card_border', '#444444'))
        position = self._position
        hovered = self._hover or self.isDown()
        rect = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        radius = 11.0
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setRenderHint(QPainter.RenderHint.TextAntialiasing)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(_mix(bg, to_color(pal.get('card_hover', '#242424')), 0.7 if hovered else 0.0))
        p.drawRoundedRect(rect, radius, radius)
        if position > 0:
            tint = QColor(accent)
            tint.setAlphaF(0.08 * position)
            p.setBrush(tint)
            p.drawRoundedRect(rect, radius, radius)
        pen_color = _mix(border, accent, 0.55 if hovered else 0.55 * position)
        p.setPen(QPen(pen_color, 1.0 + 0.4 * position))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawRoundedRect(rect, radius, radius)
        if self.hasFocus():
            p.setPen(QPen(accent, 1, Qt.PenStyle.DashLine))
            p.drawRoundedRect(rect.adjusted(3, 3, -3, -3), radius - 2, radius - 2)

        pad = m.card_pad
        icon_rect = QRectF(pad, (self.height() - m.icon_px) / 2, m.icon_px, m.icon_px)
        if not self._icon.isNull():
            self._icon.paint(p, icon_rect.toRect(), Qt.AlignmentFlag.AlignCenter)

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


class _VolumeCard(QWidget):

    changed = Signal(int)

    def __init__(self, icon: QIcon, title: str, description: str, value: int,
                 metrics: _Metrics, off_text: str, parent=None):
        super().__init__(parent)
        self._icon = icon
        self._title = title
        self._description = description
        self._metrics = metrics
        self._off_text = off_text
        self._hover = False
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
        return _font_px(self.font(), max(10, self._metrics.base_px - 1))

    def _text_width(self, width: int) -> int:
        m = self._metrics
        return max(40, int(width) - m.card_pad * 2 - m.icon_px - m.gap - 54)

    def heightForWidth(self, width: int) -> int:
        m = self._metrics
        text_w = self._text_width(width)
        title_h = _text_height(QFontMetrics(self._title_font()), self._title, text_w)
        desc_h = _text_height(QFontMetrics(self._description_font()), self._description, text_w)
        return m.card_pad * 2 + max(m.icon_px, title_h) + 4 + desc_h + 6 + 22

    def sizeHint(self) -> QSize:
        return QSize(self._metrics.feature_min_col, self.heightForWidth(self._metrics.feature_min_col))

    def minimumSizeHint(self) -> QSize:
        return QSize(0, self.heightForWidth(self._metrics.feature_min_col))

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        m = self._metrics
        self._slider.setFixedHeight(22)
        self._slider.setGeometry(m.card_pad, self.height() - m.card_pad - 22,
                                 max(1, self.width() - m.card_pad * 2), 22)

    def enterEvent(self, event) -> None:
        self._hover = True
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:
        self._hover = False
        self.update()
        super().leaveEvent(event)

    def _on_value(self, value: int) -> None:
        self.changed.emit(int(value))
        self.update()

    def _value_text(self) -> str:
        value = self.value()
        return str(value) if value > 0 else self._off_text

    def paintEvent(self, _event) -> None:
        m = self._metrics
        pal = palette()
        accent = to_color(pal.get('accent', '#0078d4'))
        bg = to_color(pal.get('card_bg', '#1b1b1b'))
        border = to_color(pal.get('card_border', '#444444'))
        rect = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        radius = 11.0
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setRenderHint(QPainter.RenderHint.TextAntialiasing)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(_mix(bg, to_color(pal.get('card_hover', '#242424')), 0.7 if self._hover else 0.0))
        p.drawRoundedRect(rect, radius, radius)
        p.setPen(QPen(border, 1))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawRoundedRect(rect, radius, radius)

        pad = m.card_pad
        icon_rect = QRectF(pad, pad, m.icon_px, m.icon_px)
        tile = QColor(accent)
        tile.setAlphaF(0.08)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(tile)
        p.drawRoundedRect(icon_rect, 8, 8)
        if not self._icon.isNull():
            self._icon.paint(p, icon_rect.toRect(), Qt.AlignmentFlag.AlignCenter)

        text_x = pad + m.icon_px + m.gap
        text_w = self._text_width(self.width())
        title_font = self._title_font()
        desc_font = self._description_font()
        title_h = _text_height(QFontMetrics(title_font), self._title, text_w)
        p.setFont(title_font)
        p.setPen(to_color(pal.get('primary_text', '#eaeaea')))
        flags = int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop | Qt.TextFlag.TextWordWrap)
        p.drawText(QRectF(text_x, pad, text_w, title_h), flags, self._title)
        p.setFont(desc_font)
        p.setPen(to_color(pal.get('muted', '#9a9a9a')))
        desc_y = pad + max(m.icon_px, title_h) + 4
        p.drawText(QRectF(text_x, desc_y, text_w, max(1, self.height() - pad - 22 - desc_y - 6)),
                   flags, self._description)

        value_font = _font_px(self.font(), m.caption_px, QFont.Weight.DemiBold)
        p.setFont(value_font)
        value_color = accent if self.value() > 0 else to_color(pal.get('muted', '#9a9a9a'))
        p.setPen(value_color)
        p.drawText(QRectF(self.width() - pad - 50, pad, 50, m.icon_px),
                   int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter), self._value_text())


class _ChoiceCard(QAbstractButton):

    def __init__(self, text: str, metrics: _Metrics, parent=None):
        super().__init__(parent)
        self._metrics = metrics
        self._hover = False
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
        self.update()

    def heightForWidth(self, width: int) -> int:
        return self._metrics.lang_h

    def hasHeightForWidth(self) -> bool:
        return True

    def sizeHint(self) -> QSize:
        return QSize(max(168, self._metrics.feature_min_col // 2), self._metrics.lang_h)

    def minimumSizeHint(self) -> QSize:
        return QSize(0, self._metrics.lang_h)

    def enterEvent(self, event) -> None:
        self._hover = True
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:
        self._hover = False
        self.update()
        super().leaveEvent(event)

    def paintEvent(self, _event) -> None:
        pal = palette()
        accent = to_color(pal.get('accent', '#0078d4'))
        on_accent = to_color(pal.get('on_accent', '#111111'))
        bg = to_color(pal.get('card_bg', '#1b1b1b'))
        border = to_color(pal.get('card_border', '#444444'))
        checked = self.isChecked()
        rect = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        radius = 10.0
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(_mix(bg, to_color(pal.get('card_hover', '#242424')), 0.7 if self._hover else 0.0))
        p.drawRoundedRect(rect, radius, radius)
        if checked:
            tint = QColor(accent)
            tint.setAlphaF(0.10)
            p.setBrush(tint)
            p.drawRoundedRect(rect, radius, radius)
        p.setPen(QPen(_mix(border, accent, 0.85 if checked else (0.35 if self._hover else 0.0)),
                      1.8 if checked else 1.0))
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
        if checked:
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
        else:
            p.setPen(QPen(_mix(border, to_color(pal.get('primary_text', '#eaeaea')), 0.25), 1))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawEllipse(center, check_d / 2 - 0.5, check_d / 2 - 0.5)


class _ThemeCard(QAbstractButton):

    def __init__(self, option_id: str, label: str, swatch: tuple, media: str, kind: str,
                 metrics: _Metrics, parent=None):
        super().__init__(parent)
        self.option_id = option_id
        self._label = label
        self._bg, self._text, self._accent = (to_color(c) for c in swatch)
        self._media = media
        self._kind = kind
        self._metrics = metrics
        self._hover = False
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
        self.update()

    def refresh_theme(self) -> None:
        self.update()

    def _label_font(self) -> QFont:
        return _font_px(self.font(), max(10, self._metrics.base_px - 1), QFont.Weight.DemiBold)

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
        checked = self.isChecked()
        pad = m.theme_pad
        rect = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        radius = 10.0
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(_mix(bg, to_color(pal.get('card_hover', '#242424')), 0.7 if self._hover else 0.0))
        p.drawRoundedRect(rect, radius, radius)
        if checked:
            tint = QColor(accent)
            tint.setAlphaF(0.09)
            p.setBrush(tint)
            p.drawRoundedRect(rect, radius, radius)
        p.setPen(QPen(_mix(border, accent, 0.88 if checked else (0.35 if self._hover else 0.0)),
                      1.9 if checked else 1.0))
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
        p.setPen(accent if checked else to_color(pal.get('primary_text', '#eaeaea')))
        flags = int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop | Qt.TextFlag.TextWordWrap)
        p.drawText(QRectF(pad, preview.bottom() + 5, max(1, self.width() - pad * 2), label_h),
                   flags, self._label)

        if checked:
            center = QPointF(preview.right() - 11, preview.top() + 11)
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

    def enterEvent(self, event) -> None:
        self._hover = True
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:
        self._hover = False
        self.update()
        super().leaveEvent(event)


class _ResponsiveGrid(QWidget):

    def __init__(self, gap: int, min_col: int, max_cols: int, parent=None):
        super().__init__(parent)
        self._gap = max(0, int(gap))
        self._min_col = max(1, int(min_col))
        self._max_cols = max(1, int(max_cols))
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
        return max(1, min(self._max_cols, len(self._items), int(cols)))

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
        cols = len(rows[0])
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
        self._texts = [(code, tr_for(code, 'onboarding.language_hello', 'Hello'))
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
        self._anim.setDuration(380)
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
        if not animate:
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

    def __init__(self, owner: QWidget | None, steps: int = 1):
        super().__init__(owner.window() if owner is not None and owner.window() is not owner else owner)
        self._owner = owner.window() if owner is not None and owner.window() is not None else owner
        self.setObjectName('pirachestOnboardingOverlay')
        self.setWindowFlags(Qt.WindowType.Dialog | Qt.WindowType.FramelessWindowHint
                            | Qt.WindowType.NoDropShadowWindowHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setModal(True)
        self.setStyleSheet(
            '#pirachestOnboardingOverlay { background: transparent; border: none; }'
        )
        self._bounds = QRect(0, 0, 900, 600)
        self._inset = 24
        self._font = QFont(self.font())
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
        self._apply_bounds()
        if self._owner is not None:
            self._owner.installEventFilter(self)

    def _owner_rect(self) -> QRect:
        owner = self._owner
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
        p.fillRect(self.rect(), QColor(0, 0, 0, 118))

    def eventFilter(self, obj, event) -> bool:
        if obj is self._owner and event.type() in (QEvent.Type.Resize, QEvent.Type.Move):
            self._apply_bounds()
            self._request_refit()
        return super().eventFilter(obj, event)

    def showEvent(self, event) -> None:
        self._apply_bounds()
        super().showEvent(event)
        self._request_refit()

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
        self._body.setFixedHeight(max(1, int(body_h)))
        self._header.setFixedWidth(width)
        self._footer.setFixedWidth(width)
        self._surface.setFixedSize(width, max(1, header_h + body_h + footer_h))

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
        }
        if sys.platform == 'win32':
            prefs['startup'] = bool(get_launch_at_startup())
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
        ('onboarding.language_continue', 'Continue'),
        ('onboarding.theme_continue', 'Continue'),
        ('onboarding.features_continue', 'Continue'),
        ('onboarding.next', 'Next'),
        ('onboarding.finish', 'Finish'),
    )
    _PAGE_TEXT = (
        ('onboarding.language_title', 'Choose your language',
         'onboarding.language_subtitle', "Choose the language you'd like to use. You can change it later in Settings."),
        ('onboarding.theme_title', 'Choose your theme',
         'onboarding.theme_subtitle', 'Pick a theme. You can change it any time in Settings.'),
        ('onboarding.features_title', 'Choose Your Features',
         'onboarding.features_subtitle', "Pick which sections you'd like to use. Everything is off by default."),
        ('onboarding.downloads_title', 'Where should downloads go?',
         'onboarding.downloads_subtitle', 'Pick one folder and PiraChest sets up every section inside it.'),
        ('onboarding.prefs_title', 'A few preferences',
         'onboarding.prefs_subtitle', 'All optional, and all changeable later in Settings.'),
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
        self._content_ready = True
        self._go(0, animate=False)
        register_theme_refresh(self, self._theme_changed)
        register_locale_refresh(self, self._locale_changed)

    def _t(self, key: str, default: str = '') -> str:
        return tr(key, default)

    def _apply_texts(self) -> None:
        index = max(0, min(self._index, len(self._BUILDERS) - 1))
        title_key, title_default, subtitle_key, subtitle_default = self._PAGE_TEXT[index]
        self._header.set_texts(
            self._t(title_key, title_default),
            self._t(subtitle_key, subtitle_default),
            f'{index + 1} / {len(self._BUILDERS)}',
        )
        self._footer.set_texts(
            self._t('onboarding.back', 'Back'),
            self._t(*self._NEXT_KEYS[index]),
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
        page = self._pages[index]
        if page is None:
            page = self._build_page(index)
        changed = page is not self._page
        self._index = index
        self.set_page(page)
        self._apply_texts()
        self._refit()
        if animate and changed:
            self._fade_page(page)

    def _fade_page(self, page: QWidget) -> None:
        if page.graphicsEffect() is not None:
            page.setGraphicsEffect(None)
        effect = QGraphicsOpacityEffect(page)
        effect.setOpacity(0.0)
        page.setGraphicsEffect(effect)
        anim = QPropertyAnimation(effect, b'opacity', effect)
        anim.setDuration(180)
        anim.setStartValue(0.0)
        anim.setEndValue(1.0)
        anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        anim.finished.connect(
            lambda p=page: QTimer.singleShot(0, p, lambda: p.setGraphicsEffect(None)))
        anim.start()

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
        grid = _ResponsiveGrid(self._metrics.theme_gap, self._metrics.theme_min_col, 5, page)
        self._theme_cards = {}
        for option_id, label, swatch, media, kind in _theme_options():
            card = _ThemeCard(option_id, label, swatch, media, kind, self._metrics, grid)
            card.set_selected(option_id == self._state.theme)
            card.clicked.connect(lambda _checked=False, o=option_id: self._pick_theme(o))
            grid.add(card)
            self._theme_cards[option_id] = card
        page.add(grid, 0)
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
        for attr, slot, fallback, desc_key, desc_default in FEATURES:
            icon = themed_icon(slot, _feature_icon(slot), size=self._metrics.icon_px)
            title = self._t(slot, fallback)
            card = _ToggleCard(icon, title, self._t(desc_key, desc_default), self._metrics, grid)
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
            self, self._t('onboarding.downloads_pick', 'Select download folder'), start)
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
        self.path_row.set_browse_text(t('onboarding.downloads_browse', 'Browse'))
        self.status_row.set_reset_text(t('onboarding.downloads_reset', 'Use default'))
        self.subfolders.set_texts(t('onboarding.downloads_subfolders', 'Organize by section'),
                                  t('onboarding.downloads_subfolders_desc',
                                    'Each section gets its own subfolder.'))
        self.folder_title.setText(t('onboarding.downloads_preview', 'Folders that will be used'))
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
                f'{format_size(free)} {tr("onboarding.downloads_free", "free on this drive")}')
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
            f'{tr("onboarding.downloads_error", "Can\'t use this folder:")} {problem}', error=True)


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
            if pref_id == 'startup' and sys.platform != 'win32':
                continue
            card = _ToggleCard(themed_icon(None, icon, size=metrics.icon_px), '', '', metrics, grid)
            card.setChecked(bool(state.prefs.get(pref_id, False)))
            card.toggled.connect(lambda checked, p=pref_id: self.pref_changed.emit(p, checked))
            grid.add(card)
            self.pref_cards[pref_id] = card
        off_text = tr('settings.volume_off', 'Off')
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
            title = t(f'onboarding.pref_{pref_id}', pref_id.title())
            desc = t(f'onboarding.pref_{pref_id}_desc', '')
            card.set_texts(title, desc)
        for volume_id, card in self.volume_cards.items():
            title = t(f'onboarding.pref_{volume_id}', volume_id.title())
            desc = t(f'onboarding.pref_{volume_id}_desc', '')
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
        'nav.tbhb': FluentIcon.SEARCH,
        'nav.music': FluentIcon.MUSIC,
        'nav.books': FluentIcon.BOOK_SHELF,
        'nav.sailseek': FluentIcon.GLOBE,
    }.get(slot, FluentIcon.APPLICATION)


def _theme_options() -> list[tuple[str, str, tuple, str, str]]:
    options = [
        (mode, tr(f'settings.theme_{mode}', 'Dark' if mode == 'dark' else 'Light'),
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
            tr('onboarding.theme_title', 'Choose your theme'),
            tr('onboarding.theme_subtitle', 'Pick a theme. You can change it any time in Settings.'),
            '',
        )
        self._footer.set_texts(
            tr('common.cancel', 'Cancel'),
            tr('onboarding.theme_continue', 'Continue'),
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
        grid = _ResponsiveGrid(self._metrics.theme_gap, self._metrics.theme_min_col, 5, page)
        self._cards = {}
        for option_id, label, swatch, media, kind in _theme_options():
            card = _ThemeCard(option_id, label, swatch, media, kind, self._metrics, grid)
            card.set_selected(option_id == self.theme)
            card.clicked.connect(lambda _checked=False, o=option_id: self._pick(o))
            grid.add(card)
            self._cards[option_id] = card
        page.add(grid, 0)
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
            tr('onboarding.theme_title', 'Choose your theme'),
            tr('onboarding.theme_subtitle', 'Pick a theme. You can change it any time in Settings.'),
            '',
        )
        self._footer.set_texts(
            tr('common.cancel', 'Cancel'),
            tr('onboarding.theme_continue', 'Continue'),
            True,
        )
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


def _maybe_run_onboarding(parent: MainWindow) -> None:
    if not settings.onboarding_completed:
        _trim_working_set_now()
        run_onboarding(parent)
        parent._sync_optional_pages()
        if parent.settings_page is not None and parent.settings_page._built:
            parent.settings_page._populate()
        _trim_idle_working_set(600)
    else:
        _trim_idle_working_set(1800)
    QTimer.singleShot(0, parent, parent._init_sailseek)
    QTimer.singleShot(2500, parent, parent._freeze_gc)
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


def _photo_path(*names: str) -> str | None:
    roots = [_GUI_DIR]
    bundle = getattr(sys, '_MEIPASS', None)
    if bundle:
        roots.append(Path(bundle) / 'src' / 'gui')
    return next((str(p) for n in names for r in roots if (p := r / 'photos' / n).is_file()), None)


def _discord_icon_path() -> str | None:
    for candidate in (_GUI_DIR.parent / 'photo' / 'd.png', _GUI_DIR / 'photo' / 'd.png'):
        if candidate.is_file():
            return str(candidate)
    path = _photo_path('d.png')
    if path is None:
        logger.warning('Discord icon not found')
    return path


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


def _donate_qr_path(key: str) -> str | None:
    return _photo_path(f'donations/{key}-qr.png')


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

    def _clear_stale_background(self) -> None:
        shader_bg = self._shader_bg
        if shader_bg is not None and (shader_bg.isVisible() or shader_bg._frag_path is not None):
            shader_bg.hide()
            shader_bg.clear()
        self._drop_background_photo()
        self.update()

    def _refresh_background_photo(self) -> None:
        preset = active_preset_id()
        shader_frag = theme_shader_path(preset)
        if self._defer_background_until_modal_closes():
            if shader_frag is None or self._shader_bg is None:
                self._clear_stale_background()
                return
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
        self._settings_prepare_timer = QTimer(self, singleShot=True, interval=400)
        self._settings_prepare_timer.timeout.connect(self._prepare_settings_page)
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
        app_icon_path = _photo_path('logo.ico', 'icon.ico', 'icon.png', 'logo.png')
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
        self.sailseek = None
        from src.core.downloader import DownloadManager
        self.download_manager = DownloadManager(self)
        if self._pending_open_targets:
            QTimer.singleShot(0, self._flush_pending_open_targets)

        self.home_nav_page = HomePage(self)
        self.home_nav_page.setObjectName('homePage')
        self._nav_home = self.addSubInterface(self.home_nav_page, themed_nav_icon('nav.home', FluentIcon.HOME), 'Home', position=NavigationItemPosition.TOP)

        if settings.onboarding_completed:
            self._sync_optional_pages()

        def _build_downloads():
            from .download_page import DownloadManagerPage
            return DownloadManagerPage(self.download_manager, self)

        self.download_page = LazyPage(_build_downloads, keep_alive=True)
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
        self._nav_settings = self.addSubInterface(self.settings_page, themed_nav_icon('nav.settings', FluentIcon.SETTING), tr('nav.settings'), position=NavigationItemPosition.BOTTOM)
        self._settings_nav_widget = self.navigationInterface.widget('settingsPage')
        self.navigationInterface.addItem(
            routeKey='openDataFolder',
            icon=themed_nav_icon('nav.open_data_folder', FluentIcon.FOLDER),
            text=tr('nav.open_data_folder'),
            onClick=self._on_open_data_folder,
            selectable=False,
            position=NavigationItemPosition.BOTTOM,
        )
        self.about_page = LazyPage(self._create_about_page, keep_alive=True)
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
        self._settings_warmup_timer = QTimer(self, singleShot=True, interval=1000)
        self._settings_warmup_timer.timeout.connect(self._prepare_settings_page)
        self._settings_warmup_timer.start()

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
            self.download_page.call_when_ready('_start_selective_add', target, source_label='External')
            self._force_foreground_focus()
        except Exception:
            logger.exception('Failed to open external target: %s', target)

    def _init_sailseek(self) -> None:
        if self.sailseek is not None:
            return
        from .sailseek import SailSeekController
        self.sailseek = SailSeekController(self)
        with suppress(Exception):
            self.sailseek.apply_settings()

    def _prepare_settings_page(self) -> None:
        page = getattr(self, 'settings_page', None)
        if page is not None:
            page.prepare()

    def _freeze_gc(self) -> None:
        try:
            gc.collect(1)
            gc.freeze()
            logger.info('Garbage collector frozen (%d objects)', gc.get_freeze_count())
        except Exception:
            logger.debug('Could not freeze the garbage collector', exc_info=True)

    def _create_about_page(self):
        from .about_page import AboutPage
        return AboutPage(self)

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
            item = self.download_manager.get(item_id)
            if item is not None and str(getattr(item, 'category', '') or '') == 'repacks':
                from src.core.repacks.base import display_title

                display_name = display_title(display_name)
        except Exception:
            logger.debug('Could not clean the download-complete title', exc_info=True)
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
            if not associations_available() or not settings.associations_enabled:
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
            chip = ChromeChip(tr('download.donate'), trimmed_icon(filled_icon(FluentIcon.HEART, bake=64).pixmap(64, 64), fill=1.0), title_bar)
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
            icon_path = _discord_icon_path()
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
        path = _photo_path('icon.ico')
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
        _tray = _photo_path('icon.ico')
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
        elif et in (_ET_ENTER, _ET_LEAVE):
            nav = getattr(self, '_settings_nav_widget', None)
            if nav is not None and isinstance(obj, QWidget):
                with suppress(RuntimeError):
                    if obj is nav or nav.isAncestorOf(obj):
                        timer = getattr(self, '_settings_prepare_timer', None)
                        if timer is not None:
                            if et == _ET_ENTER:
                                timer.start()
                            else:
                                timer.stop()
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
                if getattr(self.settings_page, '_built', False):
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
        with suppress(Exception):
            from src.core.tv.external import shutdown_external
            shutdown_external()
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
        if getattr(self.settings_page, '_built', False):
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
    _logo = _photo_path('logo.ico')
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
