from __future__ import annotations
import gc
import logging
import os
import sys
from typing import Any, Optional
from PySide6.QtCore import QObject, QThread, Qt, QEvent, QTimer, Signal, QPropertyAnimation, QEasingCurve, QPoint, QSize
from PySide6.QtGui import QColor
from PySide6.QtWidgets import QFileDialog, QHBoxLayout, QLabel, QFrame, QVBoxLayout, QWidget, QSizePolicy, QGraphicsOpacityEffect
from qfluentwidgets import BodyLabel, CardWidget, CheckBox, ComboBox, CompactSpinBox, FluentIcon, FluentWindow, HyperlinkButton, InfoBadge, InfoBadgePosition, InfoBar, InfoBarPosition, LineEdit, MessageBoxBase, NavigationItemPosition, PrimaryPushButton, TransparentToolButton, PushButton, SettingCard, SettingCardGroup, StrongBodyLabel, SubtitleLabel, SwitchButton, SwitchSettingCard, IconWidget, setTheme, qconfig, CaptionLabel
from src.core.minerva import database as db
from src.core.worker import tasks
from src.core.config import resolve_theme, ThemeMode
from src.core.theme import palette, settings_qss
from src.core.config import save_settings
from src.core.translations import tr, available_languages, current_language, set_language, register_locale_refresh
from .repacks_page import RepacksPage
from .tv_page import TV_page
from .books_page import BooksPage
from .music_page import MusicPage
from .minerva_page import MinervaPage
from .anime_page import AnimePage
from .yt_page import YtPage
logger = logging.getLogger(__name__)
PAGE_SIZE = 30
TOOLBAR_COLLAPSE_WIDTH = 700
_GUI_DIR = os.path.dirname(os.path.abspath(__file__))
_PHOTO_DIR = os.path.join(_GUI_DIR, 'photos')
_LOGO_CANDIDATES = ('logo.ico', 'logo.png')
_FONT_DIR = os.path.join(_GUI_DIR, 'lang', 'fonts')
_FIGTREE_FILENAMES = ('Inter.ttf',)

def find_logo_path() -> Optional[str]:
    for name in _LOGO_CANDIDATES:
        candidate = os.path.join(_PHOTO_DIR, name)
        if os.path.isfile(candidate):
            return candidate
    return None

def _load_bundled_figtree_fonts() -> bool:
    from PySide6.QtGui import QFontDatabase
    loaded = False
    if os.path.isdir(_FONT_DIR):
        for fname in _FIGTREE_FILENAMES:
            path = os.path.join(_FONT_DIR, fname)
            if os.path.isfile(path):
                font_id = QFontDatabase.addApplicationFont(path)
                if font_id != -1:
                    loaded = True
    return loaded

def _get_all_consoles() -> list[str]:
    try:
        return db.get_all_consoles()
    except Exception:
        return []

def _set_debug_console_visible(visible: bool) -> None:
    if os.name != 'nt':
        return
    import ctypes
    kernel32 = ctypes.windll.kernel32
    user32 = ctypes.windll.user32
    hwnd = kernel32.GetConsoleWindow()
    root_logger = logging.getLogger()
    if visible and not hwnd:
        if kernel32.AllocConsole():
            hwnd = kernel32.GetConsoleWindow()
            try:
                import sys
                sys.stdout = open('CONOUT$', 'w', buffering=1, encoding='utf-8', errors='replace')
                sys.stderr = open('CONOUT$', 'w', buffering=1, encoding='utf-8', errors='replace')
                sys.stdin = open('CONIN$', 'r')
            except Exception:
                pass
            for h in root_logger.handlers:
                if isinstance(h, logging.StreamHandler):
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

class _LazyPage(QWidget):
    __slots__ = ('_factory', '_real_page', '_layout')

    def __init__(self, factory, parent=None):
        super().__init__(parent)
        self._factory = factory
        self._real_page: Optional[QWidget] = None
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self._layout = layout

    def showEvent(self, event):
        if self._real_page is None:
            self._real_page = self._factory()
            self._layout.addWidget(self._real_page)
        super().showEvent(event)

    def hideEvent(self, event):
        if self._real_page is not None:
            page = self._real_page
            self._real_page = None
            self._layout.removeWidget(page)
            page.setParent(None)
            page.deleteLater()
            QTimer.singleShot(0, lambda: gc.collect(0))
        super().hideEvent(event)

class _LazyMusicPage(QWidget):
    __slots__ = ('_download_manager', '_real_page', '_layout')

    def __init__(self, download_manager, parent=None):
        super().__init__(parent)
        self._download_manager = download_manager
        self._real_page: Optional[QWidget] = None
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self._layout = layout

    def showEvent(self, event):
        if self._real_page is None:
            self._real_page = MusicPage()
            self._real_page.set_download_manager(self._download_manager)
            self._layout.addWidget(self._real_page)
        super().showEvent(event)

    def hideEvent(self, event):
        super().hideEvent(event)

class _LazyRepacksPage(QWidget):
    __slots__ = ('_download_manager', '_real_page', '_layout')

    def __init__(self, download_manager, parent=None):
        super().__init__(parent)
        self._download_manager = download_manager
        self._real_page: Optional[QWidget] = None
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self._layout = layout

    def showEvent(self, event):
        if self._real_page is None:
            self._real_page = RepacksPage(self._download_manager)
            self._layout.addWidget(self._real_page)
        super().showEvent(event)

    def hideEvent(self, event):
        if self._real_page is not None:
            page = self._real_page
            self._real_page = None
            try:
                for tab in getattr(page, '_tabs', {}).values():
                    try:
                        tab.shutdown()
                    except Exception:
                        logger.exception('Failed to shut down repacks source tab during page unload')
            except Exception:
                logger.exception('Failed to shut down repacks tabs during page unload')
            self._layout.removeWidget(page)
            page.setParent(None)
            page.deleteLater()
            QTimer.singleShot(0, lambda: gc.collect(0))
        super().hideEvent(event)
class _LazyTVPage(QWidget):
    __slots__ = ('_real_page', '_layout')

    def __init__(self, parent=None):
        super().__init__(parent)
        self._real_page: Optional[QWidget] = None
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self._layout = layout

    def showEvent(self, event):
        if self._real_page is None:
            self._real_page = TV_page()
            self._layout.addWidget(self._real_page)
        super().showEvent(event)

    def hideEvent(self, event):
        if self._real_page is not None:
            page = self._real_page
            self._real_page = None
            try:
                page.shutdown()
            except Exception:
                logger.exception('Failed to shut down TV page during page unload')
            self._layout.removeWidget(page)
            page.setParent(None)
            page.deleteLater()
            QTimer.singleShot(0, lambda: gc.collect(0))
        super().hideEvent(event)

_UPDATE_THREADS_IN_FLIGHT: set = set()

class _LazyBooksPage(QWidget):
    __slots__ = ('_download_manager', '_real_page', '_layout')

    def __init__(self, download_manager, parent=None):
        super().__init__(parent)
        self._download_manager = download_manager
        self._real_page: Optional[QWidget] = None
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self._layout = layout

    def showEvent(self, event):
        if self._real_page is None:
            self._real_page = BooksPage(self._download_manager)
            self._layout.addWidget(self._real_page)
        super().showEvent(event)

    def hideEvent(self, event):
        if self._real_page is not None:
            page = self._real_page
            self._real_page = None
            try:
                page.shutdown()
            except Exception:
                logger.exception('Failed to shut down books page during page unload')
            self._layout.removeWidget(page)
            page.setParent(None)
            page.deleteLater()
            QTimer.singleShot(0, lambda: gc.collect(0))
        super().hideEvent(event)

class _LazyAnimePage(QWidget):
    __slots__ = ('_download_manager', '_real_page', '_layout')

    def __init__(self, download_manager, parent=None):
        super().__init__(parent)
        self._download_manager = download_manager
        self._real_page: Optional[QWidget] = None
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self._layout = layout

    def showEvent(self, event):
        if self._real_page is None:
            self._real_page = AnimePage(self)
            self._real_page.set_download_manager(self._download_manager)
            self._layout.addWidget(self._real_page)
        super().showEvent(event)

    def hideEvent(self, event):
        if self._real_page is not None:
            page = self._real_page
            self._real_page = None
            try:
                page.shutdown()
            except Exception:
                logger.exception('Failed to shut down anime page during page unload')
            self._layout.removeWidget(page)
            page.setParent(None)
            page.deleteLater()
            QTimer.singleShot(0, lambda: gc.collect(0))
        super().hideEvent(event)

class _LazyYtPage(QWidget):
    __slots__ = ('_bridge', '_real_page', '_layout')

    ffmpeg_declined = Signal()

    def __init__(self, bridge, parent=None):
        super().__init__(parent)
        self._bridge = bridge
        self._real_page: Optional[QWidget] = None
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self._layout = layout

    def showEvent(self, event):
        if self._real_page is None:
            self._real_page = YtPage(self)
            self._real_page.set_download_bridge(self._bridge)
            self._real_page.ffmpeg_declined.connect(self.ffmpeg_declined)
            self._layout.addWidget(self._real_page)
        super().showEvent(event)

    def hideEvent(self, event):
        if self._real_page is not None:
            page = self._real_page
            self._real_page = None
            try:
                page.shutdown()
            except Exception:
                logger.exception('Failed to shut down YouTube page during page unload')
            self._layout.removeWidget(page)
            page.setParent(None)
            page.deleteLater()
            QTimer.singleShot(0, lambda: gc.collect(0))
        super().hideEvent(event)

def _make_smooth_scroll_area(parent=None):
    from qfluentwidgets import SmoothScrollArea
    from PySide6.QtWidgets import QAbstractScrollArea
    scroll = SmoothScrollArea(parent) if parent is not None else SmoothScrollArea()
    try:
        scroll.setViewportUpdateMode(QAbstractScrollArea.ViewportUpdateMode.FullViewportUpdate)
    except AttributeError:
        pass
    try:
        scroll.setSingleStep(60)
    except AttributeError:
        pass
    return scroll

class SettingsPage(QWidget):
    settings_changed = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setStyleSheet('background: transparent;')
        self._update_thread = None
        self._update_worker = None
        self._download_thread = None
        self._download_worker = None
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        scroll = _make_smooth_scroll_area(self)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setStyleSheet('QScrollArea { background: transparent; border: none; }')
        scroll.viewport().setStyleSheet('background: transparent;')
        outer.addWidget(scroll)
        container = QWidget()
        container.setStyleSheet('background: transparent;')
        scroll.setWidget(container)
        layout = QVBoxLayout(container)
        layout.setContentsMargins(24, 12, 28, 20)
        layout.setSpacing(6)
        self._app_group = SettingCardGroup(tr('settings.group_appearance'), self)
        self._theme_combo = ComboBox()
        self._theme_combo.addItems([tr('settings.theme_dark'), tr('settings.theme_light'), tr('settings.theme_auto')])
        self._theme_combo.setMinimumWidth(140)
        self._theme_combo.currentIndexChanged.connect(self._on_theme_changed)
        self._theme_card = SettingCard(FluentIcon.BRUSH, tr('settings.theme_mode_title'), tr('settings.theme_mode_content'), self)
        self._theme_card.hBoxLayout.addWidget(self._theme_combo, 0, Qt.AlignmentFlag.AlignRight)
        self._theme_card.hBoxLayout.addSpacing(4)
        self._app_group.addSettingCard(self._theme_card)
        self._lang_codes = list(available_languages().keys())
        self._lang_combo = ComboBox()
        self._lang_combo.addItems([available_languages()[code] for code in self._lang_codes])
        self._lang_combo.setMinimumWidth(140)
        self._lang_combo.currentIndexChanged.connect(self._on_language_changed)
        self._lang_card = SettingCard(FluentIcon.LANGUAGE, tr('settings.language_title'), tr('settings.language_content'), self)
        self._lang_card.hBoxLayout.addWidget(self._lang_combo, 0, Qt.AlignmentFlag.AlignRight)
        self._lang_card.hBoxLayout.addSpacing(4)
        self._app_group.addSettingCard(self._lang_card)
        from qfluentwidgets import ColorPickerButton
        self._accent_color_btn = ColorPickerButton(QColor('#00b7c3'), tr('settings.accent_color_title'), self, enableAlpha=False)
        self._accent_color_btn.colorChanged.connect(self._on_accent_color_changed)
        self._reset_accent_btn = PushButton(tr('settings.use_windows_accent'))
        self._reset_accent_btn.clicked.connect(self._on_reset_accent_color)
        self._accent_card = SettingCard(FluentIcon.PALETTE, tr('settings.accent_color_title'), tr('settings.accent_color_content'), self)
        self._accent_card.hBoxLayout.addWidget(self._reset_accent_btn, 0, Qt.AlignmentFlag.AlignRight)
        self._accent_card.hBoxLayout.addSpacing(8)
        self._accent_card.hBoxLayout.addWidget(self._accent_color_btn, 0, Qt.AlignmentFlag.AlignRight)
        self._accent_card.hBoxLayout.addSpacing(4)
        self._app_group.addSettingCard(self._accent_card)
        layout.addWidget(self._app_group)
        layout.addSpacing(16)
        self._dl_group = SettingCardGroup(tr('settings.group_download_locations'), self)
        self._download_dir_widgets: dict[str, tuple[LineEdit, PushButton]] = {}
        self._download_dir_cards: list[tuple[Any, str, str]] = []
        for category, icon, title_key, content_key, attr in (('repacks', FluentIcon.GAME, 'settings.download_folder_repacks_title', 'settings.download_folder_repacks_content', 'download_dir_repacks'), ('music', FluentIcon.MUSIC, 'settings.download_folder_music_title', 'settings.download_folder_music_content', 'download_dir_music'), ('anime', FluentIcon.VIDEO, 'settings.download_folder_anime_title', 'settings.download_folder_anime_content', 'download_dir_anime'), ('youtube', FluentIcon.PLAY, 'settings.download_folder_youtube_title', 'settings.download_folder_youtube_content', 'download_dir_youtube'), ('minerva', FluentIcon.LIBRARY, 'settings.download_folder_minerva_title', 'settings.download_folder_minerva_content', 'download_dir_minerva')):
            txt = LineEdit()
            txt.setReadOnly(True)
            txt.setMinimumWidth(220)
            browse_btn = PushButton(tr('settings.browse'))
            browse_btn.clicked.connect(lambda _checked=False, a=attr, t=txt: self._browse_download_dir_for(a, t))
            card = SettingCard(icon, tr(title_key), tr(content_key), self)
            card.hBoxLayout.addWidget(txt, 0, Qt.AlignmentFlag.AlignRight)
            card.hBoxLayout.addSpacing(8)
            card.hBoxLayout.addWidget(browse_btn, 0, Qt.AlignmentFlag.AlignRight)
            card.hBoxLayout.addSpacing(4)
            self._dl_group.addSettingCard(card)
            self._download_dir_widgets[attr] = (txt, browse_btn)
            self._download_dir_cards.append((card, title_key, content_key))
        self._chk_console_structure = SwitchSettingCard(icon=FluentIcon.IOT, title=tr('settings.console_structure_title'), content=tr('settings.console_structure_content'))
        self._connect_switch(self._chk_console_structure, self._on_console_structure_toggled)
        self._dl_group.addSettingCard(self._chk_console_structure)
        layout.addWidget(self._dl_group)
        layout.addSpacing(16)
        self._perf_group = SettingCardGroup(tr('settings.group_seeding_perf'), self)
        self._spin_seed = CompactSpinBox()
        self._spin_seed.setRange(0, 9999)
        self._spin_seed.setSuffix(tr('settings.suffix_min'))
        self._spin_seed.valueChanged.connect(self._on_seed_time_changed)
        self._seed_card = SettingCard(FluentIcon.HISTORY, tr('settings.seed_time_title'), tr('settings.seed_time_content'), self)
        self._seed_card.hBoxLayout.addWidget(self._spin_seed, 0, Qt.AlignmentFlag.AlignRight)
        self._seed_card.hBoxLayout.addSpacing(4)
        self._perf_group.addSettingCard(self._seed_card)
        self._spin_speed = CompactSpinBox()
        self._spin_speed.setRange(0, 100000)
        self._spin_speed.setSuffix(tr('settings.suffix_kbps'))
        self._spin_speed.valueChanged.connect(self._on_speed_limit_changed)
        self._speed_card = SettingCard(FluentIcon.SPEED_HIGH, tr('settings.download_limit_title'), tr('settings.download_limit_content'), self)
        self._speed_card.hBoxLayout.addWidget(self._spin_speed, 0, Qt.AlignmentFlag.AlignRight)
        self._speed_card.hBoxLayout.addSpacing(4)
        self._perf_group.addSettingCard(self._speed_card)
        self._spin_upload_speed = CompactSpinBox()
        self._spin_upload_speed.setRange(0, 100000)
        self._spin_upload_speed.setSuffix(tr('settings.suffix_kbps'))
        self._spin_upload_speed.valueChanged.connect(self._on_upload_speed_changed)
        self._upload_speed_card = SettingCard(FluentIcon.SPEED_HIGH, tr('settings.upload_limit_title'), tr('settings.upload_limit_content'), self)
        self._upload_speed_card.hBoxLayout.addWidget(self._spin_upload_speed, 0, Qt.AlignmentFlag.AlignRight)
        self._upload_speed_card.hBoxLayout.addSpacing(4)
        self._perf_group.addSettingCard(self._upload_speed_card)
        self._chk_auto = SwitchSettingCard(icon=FluentIcon.MEDIA, title=tr('settings.auto_download_title'), content=tr('settings.auto_download_content'))
        self._connect_switch(self._chk_auto, self._on_auto_download_toggled)
        self._perf_group.addSettingCard(self._chk_auto)
        self._chk_delete_torrent = SwitchSettingCard(icon=FluentIcon.DELETE, title=tr('settings.delete_torrent_title'), content=tr('settings.delete_torrent_content'))
        self._connect_switch(self._chk_delete_torrent, self._on_delete_torrent_toggled)
        self._perf_group.addSettingCard(self._chk_delete_torrent)
        self._chk_close_to_tray = SwitchSettingCard(icon=FluentIcon.MINIMIZE, title=tr('settings.close_to_tray_title'), content=tr('settings.close_to_tray_content'))
        self._connect_switch(self._chk_close_to_tray, self._on_close_to_tray_toggled)
        self._perf_group.addSettingCard(self._chk_close_to_tray)
        layout.addWidget(self._perf_group)
        layout.addSpacing(16)
        self._feature_group = SettingCardGroup(tr('settings.group_features'), self)
        self._chk_minerva = SwitchSettingCard(icon=FluentIcon.LIBRARY, title=tr('settings.minerva_title'), content=tr('settings.minerva_content'))
        self._connect_switch(self._chk_minerva, self._on_minerva_toggled)
        self._feature_group.addSettingCard(self._chk_minerva)
        self._chk_pc_games = SwitchSettingCard(icon=FluentIcon.GAME, title=tr('settings.pc_games_title'), content=tr('settings.pc_games_content'))
        self._connect_switch(self._chk_pc_games, self._on_pc_games_toggled)
        self._feature_group.addSettingCard(self._chk_pc_games)
        self._chk_local_dat = SwitchSettingCard(icon=FluentIcon.DOCUMENT, title=tr('settings.local_dat_title'), content=tr('settings.local_dat_content'))
        self._connect_switch(self._chk_local_dat, self._on_local_dat_toggled)
        self._feature_group.addSettingCard(self._chk_local_dat)
        self._chk_music = SwitchSettingCard(icon=FluentIcon.MUSIC, title=tr('settings.music_title'), content=tr('settings.music_content'))
        self._connect_switch(self._chk_music, self._on_music_toggled)
        self._feature_group.addSettingCard(self._chk_music)
        self._chk_books = SwitchSettingCard(icon=FluentIcon.BOOK_SHELF, title=tr('settings.books_title'), content=tr('settings.books_content'))
        self._connect_switch(self._chk_books, self._on_books_toggled)
        self._feature_group.addSettingCard(self._chk_books)
        self._chk_anime = SwitchSettingCard(icon=FluentIcon.VIDEO, title=tr('settings.anime_title'), content=tr('settings.anime_content'))
        self._connect_switch(self._chk_anime, self._on_anime_toggled)
        self._feature_group.addSettingCard(self._chk_anime)
        self._chk_youtube = SwitchSettingCard(icon=FluentIcon.PLAY, title=tr('settings.youtube_title'), content=tr('settings.youtube_content'))
        self._connect_switch(self._chk_youtube, self._on_youtube_toggled)
        self._feature_group.addSettingCard(self._chk_youtube)
        self._chk_tv = SwitchSettingCard(icon=FluentIcon.PROJECTOR, title=tr('settings.tv_title'), content=tr('settings.tv_content'))
        self._connect_switch(self._chk_tv, self._on_tv_toggled)
        self._feature_group.addSettingCard(self._chk_tv)
        layout.addWidget(self._feature_group)
        layout.addSpacing(16)
        self._adv_group = SettingCardGroup(tr('settings.group_advanced'), self)
        self._chk_admin_mode = SwitchSettingCard(icon=FluentIcon.DEVELOPER_TOOLS, title=tr('settings.admin_mode_title'), content=tr('settings.admin_mode_content'))
        self._connect_switch(self._chk_admin_mode, self._on_admin_mode_toggled)
        self._adv_group.addSettingCard(self._chk_admin_mode)
        self._chk_show_console = SwitchSettingCard(icon=FluentIcon.CODE, title=tr('settings.show_console_title'), content=tr('settings.show_console_content'))
        self._connect_switch(self._chk_show_console, self._on_show_console_toggled)
        self._adv_group.addSettingCard(self._chk_show_console)
        from src.core.updater import __version__ as _app_version
        self._app_version = _app_version
        self._update_status_lbl = CaptionLabel('')
        self._btn_check_update = PushButton(tr('settings.check_for_updates'))
        self._btn_check_update.clicked.connect(self._on_check_update)
        self._update_card = SettingCard(FluentIcon.SYNC, tr('settings.software_updates_title'), tr('settings.current_version', version=_app_version), self)
        self._update_card.hBoxLayout.addWidget(self._update_status_lbl, 0, Qt.AlignmentFlag.AlignRight)
        self._update_card.hBoxLayout.addSpacing(8)
        self._update_card.hBoxLayout.addWidget(self._btn_check_update, 0, Qt.AlignmentFlag.AlignRight)
        self._update_card.hBoxLayout.addSpacing(4)
        self._adv_group.addSettingCard(self._update_card)
        self._cache_status_lbl = CaptionLabel('')
        self._btn_clear_cache = PushButton(tr('settings.clear_cache_button'))
        self._btn_clear_cache.clicked.connect(self._on_clear_cache)
        self._cache_card = SettingCard(FluentIcon.BROOM, tr('settings.clear_cache_title'), tr('settings.clear_cache_content'), self)
        self._cache_card.hBoxLayout.addWidget(self._cache_status_lbl, 0, Qt.AlignmentFlag.AlignRight)
        self._cache_card.hBoxLayout.addSpacing(8)
        self._cache_card.hBoxLayout.addWidget(self._btn_clear_cache, 0, Qt.AlignmentFlag.AlignRight)
        self._cache_card.hBoxLayout.addSpacing(4)
        self._adv_group.addSettingCard(self._cache_card)
        self._btn_wipe_all = PushButton(tr('settings.wipe_all_button'))
        self._btn_wipe_all.clicked.connect(self._on_wipe_all)
        self._wipe_all_card = SettingCard(FluentIcon.DELETE, tr('settings.wipe_all_title'), tr('settings.wipe_all_content'), self)
        self._wipe_all_card.hBoxLayout.addWidget(self._btn_wipe_all, 0, Qt.AlignmentFlag.AlignRight)
        self._wipe_all_card.hBoxLayout.addSpacing(4)
        self._adv_group.addSettingCard(self._wipe_all_card)
        self._refresh_cache_size()
        layout.addWidget(self._adv_group)
        layout.addSpacing(16)
        layout.addStretch()
        self._populate()
        register_locale_refresh(self, self._apply_locale)

    @staticmethod
    def _connect_switch(card, slot):
        sig = getattr(card, 'checkedChanged', None) or card.switchButton.checkedChanged
        sig.connect(slot)

    def _apply_locale(self, *_args) -> None:
        self._app_group.titleLabel.setText(tr('settings.group_appearance'))
        self._theme_card.setTitle(tr('settings.theme_mode_title'))
        self._theme_card.setContent(tr('settings.theme_mode_content'))
        theme_idx = self._theme_combo.currentIndex()
        self._theme_combo.blockSignals(True)
        self._theme_combo.clear()
        self._theme_combo.addItems([tr('settings.theme_dark'), tr('settings.theme_light'), tr('settings.theme_auto')])
        self._theme_combo.setCurrentIndex(theme_idx)
        self._theme_combo.blockSignals(False)
        self._lang_card.setTitle(tr('settings.language_title'))
        self._lang_card.setContent(tr('settings.language_content'))
        self._accent_color_btn.title = tr('settings.accent_color_title')
        self._reset_accent_btn.setText(tr('settings.use_windows_accent'))
        self._accent_card.setTitle(tr('settings.accent_color_title'))
        self._accent_card.setContent(tr('settings.accent_color_content'))
        self._dl_group.titleLabel.setText(tr('settings.group_download_locations'))
        for card, title_key, content_key in self._download_dir_cards:
            card.setTitle(tr(title_key))
            card.setContent(tr(content_key))
        for _txt, browse_btn in self._download_dir_widgets.values():
            browse_btn.setText(tr('settings.browse'))
        self._chk_console_structure.setTitle(tr('settings.console_structure_title'))
        self._chk_console_structure.setContent(tr('settings.console_structure_content'))
        self._perf_group.titleLabel.setText(tr('settings.group_seeding_perf'))
        self._spin_seed.setSuffix(tr('settings.suffix_min'))
        self._seed_card.setTitle(tr('settings.seed_time_title'))
        self._seed_card.setContent(tr('settings.seed_time_content'))
        self._spin_speed.setSuffix(tr('settings.suffix_kbps'))
        self._speed_card.setTitle(tr('settings.download_limit_title'))
        self._speed_card.setContent(tr('settings.download_limit_content'))
        self._spin_upload_speed.setSuffix(tr('settings.suffix_kbps'))
        self._upload_speed_card.setTitle(tr('settings.upload_limit_title'))
        self._upload_speed_card.setContent(tr('settings.upload_limit_content'))
        self._chk_auto.setTitle(tr('settings.auto_download_title'))
        self._chk_auto.setContent(tr('settings.auto_download_content'))
        self._chk_delete_torrent.setTitle(tr('settings.delete_torrent_title'))
        self._chk_delete_torrent.setContent(tr('settings.delete_torrent_content'))
        self._chk_close_to_tray.setTitle(tr('settings.close_to_tray_title'))
        self._chk_close_to_tray.setContent(tr('settings.close_to_tray_content'))
        self._feature_group.titleLabel.setText(tr('settings.group_features'))
        self._chk_minerva.setTitle(tr('settings.minerva_title'))
        self._chk_minerva.setContent(tr('settings.minerva_content'))
        self._chk_pc_games.setTitle(tr('settings.pc_games_title'))
        self._chk_pc_games.setContent(tr('settings.pc_games_content'))
        self._chk_local_dat.setTitle(tr('settings.local_dat_title'))
        self._chk_local_dat.setContent(tr('settings.local_dat_content'))
        self._chk_music.setTitle(tr('settings.music_title'))
        self._chk_music.setContent(tr('settings.music_content'))
        self._chk_books.setTitle(tr('settings.books_title'))
        self._chk_books.setContent(tr('settings.books_content'))
        self._chk_anime.setTitle(tr('settings.anime_title'))
        self._chk_anime.setContent(tr('settings.anime_content'))
        self._chk_youtube.setTitle(tr('settings.youtube_title'))
        self._chk_youtube.setContent(tr('settings.youtube_content'))
        self._adv_group.titleLabel.setText(tr('settings.group_advanced'))
        self._chk_admin_mode.setTitle(tr('settings.admin_mode_title'))
        self._chk_admin_mode.setContent(tr('settings.admin_mode_content'))
        self._chk_show_console.setTitle(tr('settings.show_console_title'))
        self._chk_show_console.setContent(tr('settings.show_console_content'))
        self._btn_check_update.setText(tr('settings.check_for_updates'))
        self._update_card.setTitle(tr('settings.software_updates_title'))
        self._update_card.setContent(tr('settings.current_version', version=self._app_version))
        self._cache_card.setTitle(tr('settings.clear_cache_title'))
        self._cache_card.setContent(tr('settings.clear_cache_content'))
        self._btn_clear_cache.setText(tr('settings.clear_cache_button'))
        self._wipe_all_card.setTitle(tr('settings.wipe_all_title'))
        self._wipe_all_card.setContent(tr('settings.wipe_all_content'))
        self._btn_wipe_all.setText(tr('settings.wipe_all_button'))
        self._refresh_cache_size()

    def _toggle_setting(self, attr: str, checked: bool):
        from src.core.config import settings as _s, apply_settings
        setattr(_s, attr, checked)
        apply_settings(**{attr: checked})
        save_settings(_s)
        self.settings_changed.emit()

    def _apply_setting(self, attr: str, value):
        from src.core.config import settings as _s, apply_settings
        setattr(_s, attr, value)
        apply_settings(**{attr: value})
        save_settings(_s)
        self.settings_changed.emit()

    def _on_minerva_toggled(self, checked: bool):
        self._toggle_setting('minerva_enabled', checked)

    def _on_pc_games_toggled(self, checked: bool):
        self._toggle_setting('pc_games_enabled', checked)

    def _on_local_dat_toggled(self, checked: bool):
        self._toggle_setting('local_dat_enabled', checked)

    def _on_music_toggled(self, checked: bool):
        self._toggle_setting('music_enabled', checked)

    def _on_books_toggled(self, checked: bool):
        self._toggle_setting('books_enabled', checked)

    def _on_anime_toggled(self, checked: bool):
        self._toggle_setting('anime_enabled', checked)

    def _on_youtube_toggled(self, checked: bool):
        self._toggle_setting('youtube_enabled', checked)

    def _on_tv_toggled(self, checked: bool):
        self._toggle_setting('tv_enabled', checked)

    def _on_auto_download_toggled(self, checked: bool):
        self._toggle_setting('auto_download', checked)

    def _on_delete_torrent_toggled(self, checked: bool):
        self._toggle_setting('delete_torrent_after', checked)

    def _on_close_to_tray_toggled(self, checked: bool):
        self._toggle_setting('close_to_tray', checked)

    def _on_console_structure_toggled(self, checked: bool):
        self._apply_setting('_console_structure', checked)

    def _on_seed_time_changed(self, value: int):
        self._apply_setting('seed_time', value)

    def _on_speed_limit_changed(self, value: int):
        self._apply_setting('speed_limit', value)

    def _on_upload_speed_changed(self, value: int):
        self._apply_setting('upload_speed_limit', value)

    def _on_theme_changed(self, index: int):
        from src.core.config import settings as _s, apply_settings, resolve_theme
        from qfluentwidgets import setTheme
        theme_modes = [ThemeMode.DARK, ThemeMode.LIGHT, ThemeMode.AUTO]
        if not 0 <= index < len(theme_modes):
            return
        new_theme_mode = theme_modes[index]
        if new_theme_mode == _s.theme_mode:
            return
        _s.theme_mode = new_theme_mode
        apply_settings(theme_mode=new_theme_mode)
        save_settings(_s)
        resolve_theme(setTheme)
        self.settings_changed.emit()

    def _on_language_changed(self, index: int):
        if 0 <= index < len(self._lang_codes):
            set_language(self._lang_codes[index])
            from PySide6.QtWidgets import QApplication
            app = QApplication.instance()
            if app is not None:
                app.setLayoutDirection(Qt.LayoutDirection.LeftToRight)
            self.settings_changed.emit()

    def _on_download_dir_changed(self, attr: str, path: str):
        from src.core.config import paths
        self._apply_setting(attr, path or paths.download_root)

    def _on_check_update(self):
        self._btn_check_update.setEnabled(False)
        self._update_status_lbl.setText(tr('update.checking'))
        from src.core import updater as _updater

        class _CheckWorker(QObject):
            done = Signal(object)

            def run(self):
                try:
                    result = _updater.check_for_update()
                except Exception:
                    result = None
                self.done.emit(result)
        thread = QThread()
        worker = _CheckWorker()
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        entry = (thread, worker)
        _UPDATE_THREADS_IN_FLIGHT.add(entry)

        def _safe_done(result):
            try:
                self._on_update_check_done(result)
            except RuntimeError:
                pass
        worker.done.connect(_safe_done)
        worker.done.connect(thread.quit)
        thread.finished.connect(lambda: _UPDATE_THREADS_IN_FLIGHT.discard(entry))
        thread.finished.connect(worker.deleteLater)
        thread.finished.connect(thread.deleteLater)
        thread.start()

    def _on_update_check_done(self, result):
        self._btn_check_update.setEnabled(True)
        if result is None:
            self._update_status_lbl.setText(tr('update.up_to_date'))
            return
        self._update_status_lbl.setText(tr('update.available', tag=result['tag']))
        box = MessageBoxBase(self.window())
        box_layout = QVBoxLayout()
        box_layout.addWidget(StrongBodyLabel(tr('update.available_title', tag=result['tag'])))
        notes = result.get('notes') or tr('update.no_notes')
        notes_lbl = BodyLabel(notes[:500])
        notes_lbl.setWordWrap(True)
        box_layout.addWidget(notes_lbl)
        box.viewLayout.addLayout(box_layout)
        box.yesButton.setText(tr('update.update_now'))
        box.cancelButton.setText(tr('update.later'))
        if box.exec():
            self._start_update_download(result)

    def _start_update_download(self, result):
        from src.core import updater as _updater
        self._update_status_lbl.setText(tr('update.downloading'))
        self._btn_check_update.setEnabled(False)

        class _DownloadWorker(QObject):
            progress = Signal(int, int)
            finished = Signal(str)
            error = Signal(str)

            def run(self):
                try:
                    path = _updater.download_update(result['download_url'], on_progress=lambda d, t: self.progress.emit(d, t))
                    self.finished.emit(path)
                except Exception as exc:
                    self.error.emit(str(exc))
        thread = QThread()
        worker = _DownloadWorker()
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        entry = (thread, worker)
        _UPDATE_THREADS_IN_FLIGHT.add(entry)

        def _safe(callback, *args):
            try:
                callback(*args)
            except RuntimeError:
                pass
        worker.progress.connect(lambda d, t: _safe(self._on_update_download_progress, d, t))
        worker.finished.connect(lambda path: _safe(self._on_update_download_finished, path))
        worker.error.connect(lambda msg: _safe(self._on_update_download_error, msg))
        worker.finished.connect(thread.quit)
        worker.error.connect(thread.quit)
        thread.finished.connect(lambda: _UPDATE_THREADS_IN_FLIGHT.discard(entry))
        thread.finished.connect(worker.deleteLater)
        thread.finished.connect(thread.deleteLater)
        thread.start()

    def _on_update_download_progress(self, downloaded: int, total: int):
        if total > 0:
            self._update_status_lbl.setText(tr('update.downloading_pct', percent=int(downloaded / total * 100)))

    def _on_update_download_finished(self, path: str):
        self._update_status_lbl.setText(tr('update.restarting'))
        from src.core import updater as _updater
        try:
            win = self.window()
            if hasattr(win, '_force_quit'):
                win._force_quit = True
            _updater.apply_update_and_restart(path)
        except Exception:
            self._update_status_lbl.setText(tr('update.failed'))
            self._btn_check_update.setEnabled(True)

    def _on_update_download_error(self, msg: str):
        self._update_status_lbl.setText(tr('update.download_failed'))
        self._btn_check_update.setEnabled(True)

    def _on_accent_color_changed(self, color: QColor):
        from qfluentwidgets import setThemeColor
        from src.core.config import settings as _s, apply_settings
        setThemeColor(color)
        hex_color = color.name(QColor.NameFormat.HexRgb)
        _s.accent_color = hex_color
        apply_settings(accent_color=hex_color)
        save_settings(_s)
        self.settings_changed.emit()

    def _on_reset_accent_color(self):
        from src.core.config import detect_windows_accent_color
        color = QColor(detect_windows_accent_color())
        self._accent_color_btn.setColor(color)
        self._on_accent_color_changed(color)

    def _refresh_cache_size(self) -> None:
        from src.core.config import paths, dir_size_bytes, format_size
        total = dir_size_bytes(paths.cache_dir) + dir_size_bytes(paths.torrent_cache)
        self._cache_status_lbl.setText(format_size(total))

    def _on_clear_cache(self) -> None:
        box = MessageBoxBase(self.window())
        box_layout = QVBoxLayout()
        box_layout.addWidget(StrongBodyLabel(tr('settings.clear_cache_confirm_title')))
        content_lbl = BodyLabel(tr('settings.clear_cache_confirm_content'))
        content_lbl.setWordWrap(True)
        box_layout.addWidget(content_lbl)
        box.viewLayout.addLayout(box_layout)
        box.yesButton.setText(tr('settings.clear_cache_button'))
        box.cancelButton.setText(tr('download.cancel'))
        if not box.exec():
            return
        from src.core.config import clear_cache_dirs
        try:
            clear_cache_dirs()
        except Exception:
            logger.exception('Failed to clear cache')
        self._refresh_cache_size()
        InfoBar.success(title=tr('settings.clear_cache_done_title'), content='', orient=Qt.Orientation.Horizontal, isClosable=True, position=InfoBarPosition.TOP_RIGHT, duration=2500, parent=self.window())

    def _on_wipe_all(self) -> None:
        box = MessageBoxBase(self.window())
        box_layout = QVBoxLayout()
        warn_lbl = StrongBodyLabel(tr('settings.wipe_all_confirm_title'))
        warn_lbl.setStyleSheet(f'color: {palette()["state_error"]};')
        box_layout.addWidget(warn_lbl)
        content_lbl = BodyLabel(tr('settings.wipe_all_confirm_content'))
        content_lbl.setWordWrap(True)
        box_layout.addWidget(content_lbl)
        box.viewLayout.addLayout(box_layout)
        box.yesButton.setText(tr('settings.wipe_all_confirm_button'))
        box.cancelButton.setText(tr('download.cancel'))
        if not box.exec():
            return
        from src.core.config import wipe_all_app_data
        try:
            wipe_all_app_data()
        except Exception:
            logger.exception('Failed to wipe app data')
        win = self.window()
        if hasattr(win, '_force_quit'):
            win._force_quit = True
        win.close()

    def _on_admin_mode_toggled(self, checked: bool):
        from src.core.config import settings as _s, apply_settings, save_settings, relaunch_as_admin, is_admin
        _s.admin_mode = checked
        apply_settings(admin_mode=checked)
        save_settings(_s)
        if checked and (not is_admin()):
            box = MessageBoxBase(self.window())
            box_layout = QVBoxLayout()
            box_layout.addWidget(BodyLabel('Restart as administrator?'))
            box.viewLayout.addLayout(box_layout)
            box.yesButton.setText('Restart Now')
            box.cancelButton.setText('Later')
            if box.exec() and relaunch_as_admin():
                win = self.window()
                if hasattr(win, '_force_quit'):
                    win._force_quit = True
                win.close()
        self.settings_changed.emit()

    def _on_show_console_toggled(self, checked: bool):
        self._toggle_setting('show_console', checked)
        try:
            _set_debug_console_visible(checked)
        except Exception:
            logger.exception('Failed to toggle debug console')

    def _browse_download_dir_for(self, attr: str, txt: LineEdit):
        path = QFileDialog.getExistingDirectory(self, 'Select Download Directory', txt.text())
        if path:
            txt.setText(path)
            self._on_download_dir_changed(attr, path)

    def _populate(self):
        from src.core.config import settings as _s, paths
        _dir_defaults = {'download_dir_repacks': paths.download_root, 'download_dir_music': os.path.join(paths.download_root, 'music'), 'download_dir_anime': os.path.join(paths.download_root, 'anime'), 'download_dir_youtube': os.path.join(paths.download_root, 'youtube'), 'download_dir_minerva': paths.download_root}
        for attr, (txt, _btn) in self._download_dir_widgets.items():
            txt.setText(getattr(_s, attr, None) or _dir_defaults[attr])
        self._chk_console_structure.setChecked(getattr(_s, '_console_structure', True))
        self._spin_seed.setValue(_s.seed_time)
        self._spin_speed.setValue(_s.speed_limit)
        self._spin_upload_speed.setValue(getattr(_s, 'upload_speed_limit', 500))
        self._chk_auto.setChecked(getattr(_s, 'auto_download', False))
        self._chk_delete_torrent.setChecked(getattr(_s, 'delete_torrent_after', True))
        self._chk_minerva.setChecked(getattr(_s, 'minerva_enabled', True))
        self._chk_pc_games.setChecked(getattr(_s, 'pc_games_enabled', False))
        self._chk_local_dat.setChecked(getattr(_s, 'local_dat_enabled', False))
        self._chk_music.setChecked(getattr(_s, 'music_enabled', False))
        self._chk_books.setChecked(getattr(_s, 'books_enabled', False))
        self._chk_anime.setChecked(getattr(_s, 'anime_enabled', False))
        self._chk_youtube.setChecked(getattr(_s, 'youtube_enabled', False))
        self._chk_tv.setChecked(getattr(_s, 'tv_enabled', False))
        self._accent_color_btn.setColor(QColor(getattr(_s, 'accent_color', '#00b7c3')))
        self._chk_close_to_tray.setChecked(getattr(_s, 'close_to_tray', False))
        self._chk_admin_mode.setChecked(getattr(_s, 'admin_mode', False))
        self._chk_show_console.setChecked(getattr(_s, 'show_console', False))
        mode_map = {'Dark': 0, 'Light': 1, 'Auto': 2}
        self._theme_combo.setCurrentIndex(mode_map.get(_s.theme_mode, 0))
        lang_code = current_language()
        if lang_code in self._lang_codes:
            self._lang_combo.setCurrentIndex(self._lang_codes.index(lang_code))

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
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        scroll = _make_smooth_scroll_area(self)
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
        layout.setSpacing(18)

        from src.core.updater import __version__ as _about_version
        self._about_version = _about_version

        self._hero_card = self._build_hero_card()
        layout.addWidget(self._hero_card)

        self._links_title_lbl = StrongBodyLabel('')
        self._links_title_lbl.setLayoutDirection(Qt.LayoutDirection.LeftToRight)
        self._links_title_lbl.setStyleSheet(f'font-size: 15px; color: {palette()["primary_text"]};')
        self._links_title_lbl.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        layout.addWidget(self._links_title_lbl)
        self._github_card = CardWidget()
        self._github_card.setMinimumHeight(76)
        github_layout = QHBoxLayout(self._github_card)
        github_layout.setContentsMargins(16, 10, 12, 10)
        github_layout.setSpacing(12)
        github_layout.setAlignment(Qt.AlignmentFlag.AlignVCenter)
        github_icon = IconWidget(FluentIcon.GITHUB)
        github_icon.setFixedSize(24, 24)
        github_layout.addWidget(github_icon, 0, Qt.AlignmentFlag.AlignVCenter)
        github_text_col = QVBoxLayout()
        github_text_col.setSpacing(2)
        github_text_col.setContentsMargins(0, 0, 0, 0)
        self._github_title_lbl = StrongBodyLabel('')
        self._github_title_lbl.setLayoutDirection(Qt.LayoutDirection.LeftToRight)
        self._github_title_lbl.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        self._github_content_lbl = CaptionLabel('')
        self._github_content_lbl.setStyleSheet(f'color: {palette()["muted"]};')
        self._github_content_lbl.setLayoutDirection(Qt.LayoutDirection.LeftToRight)
        self._github_content_lbl.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        github_text_col.addWidget(self._github_title_lbl)
        github_text_col.addWidget(self._github_content_lbl)
        github_layout.addLayout(github_text_col, 1)
        self._github_btn = HyperlinkButton(self.GITHUB_URL, '')
        github_layout.addWidget(self._github_btn, 0, Qt.AlignmentFlag.AlignVCenter)
        layout.addWidget(self._github_card)

        self._credits_title_lbl = StrongBodyLabel('')
        self._credits_title_lbl.setLayoutDirection(Qt.LayoutDirection.LeftToRight)
        self._credits_title_lbl.setStyleSheet(f'font-size: 15px; color: {palette()["primary_text"]};')
        self._credits_title_lbl.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        layout.addWidget(self._credits_title_lbl)

        author_card = CardWidget()
        author_card.setMinimumHeight(64)
        author_layout = QHBoxLayout(author_card)
        author_layout.setContentsMargins(16, 10, 12, 10)
        author_layout.setSpacing(12)
        author_layout.setAlignment(Qt.AlignmentFlag.AlignVCenter)
        self._author_avatar = self._build_author_avatar()
        author_layout.addWidget(self._author_avatar, 0, Qt.AlignmentFlag.AlignVCenter)
        author_text_col = QVBoxLayout()
        author_text_col.setSpacing(2)
        author_text_col.setContentsMargins(0, 0, 0, 0)
        self._made_by_lbl = StrongBodyLabel('')
        self._made_by_lbl.setLayoutDirection(Qt.LayoutDirection.LeftToRight)
        self._made_by_lbl.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        self._dev_role_lbl = CaptionLabel('')
        self._dev_role_lbl.setStyleSheet(f'color: {palette()["muted"]};')
        self._dev_role_lbl.setLayoutDirection(Qt.LayoutDirection.LeftToRight)
        self._dev_role_lbl.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        author_text_col.addWidget(self._made_by_lbl)
        author_text_col.addWidget(self._dev_role_lbl)
        author_layout.addLayout(author_text_col, 1)
        self._profile_btn = HyperlinkButton(self.AUTHOR_URL, '')
        author_layout.addWidget(self._profile_btn, 0, Qt.AlignmentFlag.AlignVCenter)
        layout.addWidget(author_card)

        self._thanks_card = CardWidget()
        self._thanks_card.setMinimumHeight(76)
        thanks_layout = QHBoxLayout(self._thanks_card)
        thanks_layout.setContentsMargins(16, 10, 12, 10)
        thanks_layout.setSpacing(12)
        thanks_layout.setAlignment(Qt.AlignmentFlag.AlignVCenter)
        thanks_icon = IconWidget(FluentIcon.HEART)
        thanks_icon.setFixedSize(24, 24)
        thanks_layout.addWidget(thanks_icon, 0, Qt.AlignmentFlag.AlignVCenter)
        thanks_text_col = QVBoxLayout()
        thanks_text_col.setSpacing(2)
        thanks_text_col.setContentsMargins(0, 0, 0, 0)
        self._thanks_title_lbl = StrongBodyLabel('')
        self._thanks_title_lbl.setLayoutDirection(Qt.LayoutDirection.LeftToRight)
        self._thanks_title_lbl.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        self._thanks_content_lbl = CaptionLabel('')
        self._thanks_content_lbl.setStyleSheet(f'color: {palette()["muted"]};')
        self._thanks_content_lbl.setLayoutDirection(Qt.LayoutDirection.LeftToRight)
        self._thanks_content_lbl.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        thanks_text_col.addWidget(self._thanks_title_lbl)
        thanks_text_col.addWidget(self._thanks_content_lbl)
        thanks_layout.addLayout(thanks_text_col, 1)
        self._thanks_btn = HyperlinkButton(self.LOGO_ARTIST_URL, self.LOGO_ARTIST_REDDIT_USER)
        thanks_layout.addWidget(self._thanks_btn, 0, Qt.AlignmentFlag.AlignVCenter)
        layout.addWidget(self._thanks_card)

        layout.addStretch(1)
        self._apply_locale()
        register_locale_refresh(self, self._apply_locale)

    def _apply_locale(self):
        self._version_lbl.setText(tr('about.version', version=self._about_version))
        self._version_lbl.setLayoutDirection(Qt.LayoutDirection.LeftToRight)
        self._desc_lbl.setText(tr('about.description'))
        self._desc_lbl.setLayoutDirection(Qt.LayoutDirection.LeftToRight)
        self._links_title_lbl.setText(tr('about.links_group'))
        self._links_title_lbl.setLayoutDirection(Qt.LayoutDirection.LeftToRight)
        self._github_title_lbl.setText(tr('about.source_code_title'))
        self._github_title_lbl.setLayoutDirection(Qt.LayoutDirection.LeftToRight)
        self._github_content_lbl.setText(tr('about.source_code_content'))
        self._github_content_lbl.setLayoutDirection(Qt.LayoutDirection.LeftToRight)
        self._github_btn.setText(tr('about.open_github'))
        self._credits_title_lbl.setText(tr('about.credits_group'))
        self._credits_title_lbl.setLayoutDirection(Qt.LayoutDirection.LeftToRight)
        self._made_by_lbl.setText(tr('about.made_by', name=self.AUTHOR_NAME))
        self._made_by_lbl.setLayoutDirection(Qt.LayoutDirection.LeftToRight)
        self._dev_role_lbl.setText(tr('about.developer_role'))
        self._dev_role_lbl.setLayoutDirection(Qt.LayoutDirection.LeftToRight)
        self._profile_btn.setText(tr('about.profile'))
        self._thanks_title_lbl.setText(tr('about.special_thanks'))
        self._thanks_title_lbl.setLayoutDirection(Qt.LayoutDirection.LeftToRight)
        self._thanks_content_lbl.setText(tr('about.logo_credit', name=self.LOGO_ARTIST_REDDIT_USER))
        self._thanks_content_lbl.setLayoutDirection(Qt.LayoutDirection.LeftToRight)

    def _build_hero_card(self) -> QWidget:
        from PySide6.QtGui import QPixmap
        card = CardWidget()
        card.setMinimumHeight(140)
        layout = QHBoxLayout(card)
        layout.setContentsMargins(28, 24, 28, 24)
        layout.setSpacing(20)

        logo_size = 72
        logo_label = QLabel()
        logo_label.setFixedSize(logo_size, logo_size)
        logo_path = find_logo_path()
        if logo_path:
            pixmap = QPixmap(logo_path)
            if not pixmap.isNull():
                logo_label.setScaledContents(True)
                logo_label.setPixmap(pixmap.scaled(logo_size, logo_size, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation))
        logo_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(logo_label, 0, Qt.AlignmentFlag.AlignVCenter)

        text_col = QVBoxLayout()
        text_col.setSpacing(4)
        text_col.setContentsMargins(0, 0, 0, 0)
        title_row = QHBoxLayout()
        title_row.setSpacing(10)
        title_lbl = SubtitleLabel('PiraChest')
        title_lbl.setStyleSheet(f'font-size: 22px; font-weight: 700; color: {palette()["primary_text"]};')
        title_row.addWidget(title_lbl, 0)
        self._version_lbl = CaptionLabel('')
        self._version_lbl.setStyleSheet(f'color: {palette()["muted"]}; padding-top: 4px;')
        self._version_lbl.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        title_row.addWidget(self._version_lbl, 0, Qt.AlignmentFlag.AlignVCenter)
        title_row.addStretch(1)
        text_col.addLayout(title_row)
        self._desc_lbl = BodyLabel('')
        self._desc_lbl.setWordWrap(True)
        self._desc_lbl.setStyleSheet(f'color: {palette()["body_text"]};')
        self._desc_lbl.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        text_col.addWidget(self._desc_lbl)
        layout.addLayout(text_col, 1)
        return card

    def _build_author_avatar(self) -> QWidget:
        from PySide6.QtGui import QPixmap, QPainter, QPainterPath, QColor as _QColor
        size = 44
        avatar = QLabel()
        avatar.setFixedSize(size, size)
        pixmap_path = None
        for name in ('author.png',):
            candidate = os.path.join(_PHOTO_DIR, name)
            if os.path.isfile(candidate):
                pixmap_path = candidate
                break
        canvas = QPixmap(size, size)
        canvas.fill(Qt.GlobalColor.transparent)
        painter = QPainter(canvas)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        clip_path = QPainterPath()
        clip_path.addEllipse(0, 0, size, size)
        painter.setClipPath(clip_path)
        if pixmap_path:
            src = QPixmap(pixmap_path)
            if not src.isNull():
                src = src.scaled(size, size, Qt.AspectRatioMode.KeepAspectRatioByExpanding, Qt.TransformationMode.SmoothTransformation)
                x = (size - src.width()) // 2
                y = (size - src.height()) // 2
                painter.drawPixmap(x, y, src)
            else:
                pixmap_path = None
        if not pixmap_path:
            painter.setPen(_QColor(palette()['muted']))
            font = painter.font()
            font.setPointSize(16)
            font.setBold(True)
            painter.setFont(font)
            initial = (self.AUTHOR_NAME.strip()[:1] or '?').upper()
            painter.drawText(canvas.rect(), Qt.AlignmentFlag.AlignCenter, initial)
        painter.end()
        avatar.setPixmap(canvas)
        return avatar

class PlaceholderPage(QWidget):

    def __init__(self, icon, title: str, message: str, parent=None):
        super().__init__(parent)
        self.setStyleSheet('background: transparent;')
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        outer.addStretch(1)
        card = CardWidget()
        card.setMinimumWidth(380)
        card.setMaximumWidth(420)
        card_layout = QVBoxLayout(card)
        card_layout.setContentsMargins(28, 28, 28, 28)
        card_layout.setSpacing(10)
        icon_lbl = QLabel()
        try:
            icon_lbl.setPixmap(icon.icon().pixmap(40, 40))
        except Exception:
            pass
        icon_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        card_layout.addWidget(icon_lbl)
        title_lbl = SubtitleLabel(title)
        title_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        card_layout.addWidget(title_lbl)
        body_lbl = BodyLabel(message)
        body_lbl.setWordWrap(True)
        body_lbl.setMinimumWidth(300)
        body_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        card_layout.addWidget(body_lbl)
        status_lbl = CaptionLabel(tr('common.coming_soon'))
        status_lbl.setWordWrap(True)
        status_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        card_layout.addWidget(status_lbl)
        self._status_lbl = status_lbl
        register_locale_refresh(self, self._apply_locale)
        center_row = QHBoxLayout()
        center_row.addStretch(1)
        center_row.addWidget(card)
        center_row.addStretch(1)
        outer.addLayout(center_row)
        outer.addStretch(1)

    def _apply_locale(self):
        self._status_lbl.setText(tr('common.coming_soon'))

class AlphaDisclaimerDialog(MessageBoxBase):

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumWidth(420)
        self.viewLayout.addWidget(SubtitleLabel('PiraChest is in Alpha'))
        body = BodyLabel('PiraChest is still in active alpha development.\nSome features may be incomplete or missing.')
        body.setWordWrap(True)
        self.viewLayout.addWidget(body)
        self.viewLayout.addSpacing(6)
        self._chk_never_show = CheckBox("Don't show this again")
        self.viewLayout.addWidget(self._chk_never_show)
        self.yesButton.setText('Got it')
        self.cancelButton.hide()

    def never_show_again(self) -> bool:
        return self._chk_never_show.isChecked()
_ONBOARDING_FEATURES = (('minerva_enabled', FluentIcon.LIBRARY, 'Minerva ROM Index', 'Show the Home section with ROM browsing and sync'), ('pc_games_enabled', FluentIcon.GAME, 'Repacks (PC Games)', 'Show a PC Games section in the sidebar'), ('local_dat_enabled', FluentIcon.DOCUMENT, 'Local DAT Support', 'Show a Local DAT section in the sidebar (A Placeholder, coming in future updates.)'), ('music_enabled', FluentIcon.MUSIC, 'Music', 'Show a Music section in the sidebar (A Placeholder, coming in future updates.)'), ('books_enabled', FluentIcon.BOOK_SHELF, 'Books', 'Show a Books section in the sidebar (A Placeholder, coming in future updates.)'), ('anime_enabled', FluentIcon.VIDEO, 'Anime', 'Show an Anime section in the sidebar'), ('youtube_enabled', FluentIcon.PLAY, 'YouTube Downloader', 'Show a YouTube Downloader section in the sidebar'), ('tv_enabled', FluentIcon.TILES, 'TV', 'Show a TV section in the sidebar (Movies, Series, Sports, Channels)'))

class _CompactFeatureRow(QWidget):
    __slots__ = ('switch',)

    def __init__(self, icon, title: str, desc: str, parent=None):
        super().__init__(parent)
        c = palette()
        self.setStyleSheet(f'_CompactFeatureRow {{ background-color: {c['card_bg']}; border: 1px solid {c['card_border']}; border-radius: 6px; }}')
        row = QHBoxLayout(self)
        row.setContentsMargins(8, 6, 8, 6)
        row.setSpacing(8)
        icon_w = IconWidget(icon, self)
        icon_w.setFixedSize(16, 16)
        row.addWidget(icon_w, 0, Qt.AlignmentFlag.AlignVCenter)
        text_col = QVBoxLayout()
        text_col.setSpacing(0)
        title_lbl = StrongBodyLabel(title)
        title_lbl.setStyleSheet(f'font-size: 12px; color: {c['primary_text']};')
        desc_lbl = CaptionLabel(desc)
        desc_lbl.setWordWrap(True)
        desc_lbl.setStyleSheet(f'color: {c['muted']}; font-size: 10px;')
        text_col.addWidget(title_lbl)
        text_col.addWidget(desc_lbl)
        row.addLayout(text_col, 1)
        self.switch = SwitchButton(self)
        self.switch.setChecked(False)
        row.addWidget(self.switch, 0, Qt.AlignmentFlag.AlignVCenter)

class FeatureOnboardingDialog(MessageBoxBase):

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumWidth(480)
        self.widget.setMinimumWidth(480)
        self.viewLayout.addWidget(SubtitleLabel('Choose Your Features'))
        body = BodyLabel("Pick which sections you'd like to use. Everything is off by default.")
        body.setWordWrap(True)
        self.viewLayout.addWidget(body)
        self.viewLayout.addSpacing(4)
        rows_layout = QVBoxLayout()
        rows_layout.setSpacing(4)
        self._switches: dict[str, _CompactFeatureRow] = {}
        for key, icon, feat_title, desc in _ONBOARDING_FEATURES:
            row = _CompactFeatureRow(icon, feat_title, desc)
            self._switches[key] = row
            rows_layout.addWidget(row)
        self.viewLayout.addLayout(rows_layout)
        self.yesButton.setText('Continue')
        self.cancelButton.hide()

    def selections(self) -> dict:
        return {key: row.switch.isChecked() for key, row in self._switches.items()}

def _maybe_show_feature_onboarding(parent: 'MainWindow') -> None:
    from src.core.config import settings as _s
    if getattr(_s, 'onboarding_completed', False):
        return
    dialog = FeatureOnboardingDialog(parent)
    dialog.exec()
    for key, value in dialog.selections().items():
        setattr(_s, key, value)
    _s.onboarding_completed = True
    try:
        save_settings(_s)
    except Exception:
        pass
    parent._sync_optional_pages()
    if parent.settings_page is not None:
        parent.settings_page._populate()

def _maybe_show_alpha_disclaimer(parent: 'MainWindow') -> None:
    from src.core.config import settings as _s
    if getattr(_s, 'hide_alpha_disclaimer', False):
        return
    dialog = AlphaDisclaimerDialog(parent)
    dialog.exec()
    if dialog.never_show_again():
        _s.hide_alpha_disclaimer = True
        try:
            save_settings(_s)
        except Exception:
            pass

class _IconSplashScreen(QWidget):

    def __init__(self, icon, parent):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        from qfluentwidgets import Theme as _FWTheme
        bg_color = '#1c1c1c' if qconfig.theme == _FWTheme.DARK else '#f3f3f3'
        self.setStyleSheet(f'background-color: {bg_color};')
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        icon_label = QLabel(self)
        icon_label.setPixmap(icon.pixmap(120, 120))
        icon_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(icon_label, alignment=Qt.AlignmentFlag.AlignCenter)
        parent.installEventFilter(self)
        self.raise_()
        self.show()

    def eventFilter(self, obj, event):
        if obj is self.parent() and event.type() == QEvent.Type.Resize:
            self.resize(self.parent().size())
        return super().eventFilter(obj, event)

    def finish(self):
        self.parent().removeEventFilter(self)
        self.deleteLater()

class MainWindow(FluentWindow):

    def __init__(self):
        super().__init__()
        self.setWindowTitle('PiraChest')
        self.resize(1100, 740)
        self.setMinimumSize(900, 600)
        self._force_quit = False
        self._tray_icon = None
        self._downloads_badge = None
        self._downloads_badge_count = -1
        resolve_theme(setTheme)
        try:
            self.setMicaEffectEnabled(False)
        except Exception:
            logger.exception('Failed to disable Mica effect')
        try:
            from PySide6.QtGui import QColor
            self.setCustomBackgroundColor(QColor('#F3F3F4'), QColor('#1c1c1c'))
        except Exception:
            logger.exception('Failed to set custom background color')
        try:
            from qfluentwidgets import setThemeColor
            from src.core.config import settings as _s
            setThemeColor(QColor(getattr(_s, 'accent_color', '#00b7c3')))
        except Exception:
            pass
        try:
            from src.core.config import settings as _s_console
            if getattr(_s_console, 'show_console', False):
                _set_debug_console_visible(True)
        except Exception:
            logger.exception('Failed to restore debug console visibility')
        from PySide6.QtGui import QIcon
        from PySide6.QtWidgets import QApplication
        logo_path = find_logo_path()
        if logo_path:
            self.setWindowIcon(QIcon(logo_path))
        self._enlarge_title_bar_branding()
        self.splashScreen = _IconSplashScreen(self.windowIcon(), self)
        self.showMaximized()
        QApplication.processEvents()
        self._force_foreground_focus()
        QTimer.singleShot(0, self._force_foreground_focus)
        QTimer.singleShot(150, self._force_foreground_focus)
        self.navigationInterface.setExpandWidth(180)
        self.navigationInterface.setCollapsible(False)
        self._init_tray_icon()
        qconfig.themeChanged.connect(self._on_global_theme_changed)
        self.home_page: Optional[MinervaPage] = None
        self.pc_games_page: Optional[QWidget] = None
        self.local_dat_page: Optional[QWidget] = None
        self.music_page: Optional[QWidget] = None
        self.books_page: Optional[QWidget] = None
        self.anime_page: Optional[QWidget] = None
        self.yt_page: Optional[QWidget] = None
        self.tv_page: Optional[QWidget] = None
        from src.core.downloader import DownloadManager
        from .download_page import DownloadManagerPage
        self.download_manager = DownloadManager(self)
        from src.core.yt.job import YtDownloadBridge
        self.yt_bridge = YtDownloadBridge(self.download_manager, self)
        from src.core.config import settings as _s_init
        if getattr(_s_init, 'onboarding_completed', False):
            self._sync_optional_pages()
        self.download_page = DownloadManagerPage(self.download_manager, self)
        self.download_page.setObjectName('downloadPage')
        self._nav_downloads = self.addSubInterface(self.download_page, FluentIcon.DOWNLOAD, tr('nav.downloads'), position=NavigationItemPosition.BOTTOM)
        self.download_manager.item_added.connect(self._schedule_downloads_badge_update)
        self.download_manager.item_updated.connect(self._schedule_downloads_badge_update)
        self.download_manager.item_removed.connect(self._schedule_downloads_badge_update)
        self._downloads_badge_timer = QTimer(self)
        self._downloads_badge_timer.setSingleShot(True)
        self._downloads_badge_timer.setInterval(200)
        self._downloads_badge_timer.timeout.connect(self._update_downloads_badge)
        self._update_downloads_badge()
        self.settings_page = SettingsPage(self)
        self.settings_page.setObjectName('settingsPage')
        self._nav_settings = self.addSubInterface(self.settings_page, FluentIcon.SETTING, tr('nav.settings'), position=NavigationItemPosition.BOTTOM)
        self.navigationInterface.addItem(
            routeKey='openDataFolder',
            icon=FluentIcon.FOLDER,
            text=tr('nav.open_data_folder'),
            onClick=self._on_open_data_folder,
            selectable=False,
            position=NavigationItemPosition.BOTTOM,
        )
        self.about_page = AboutPage(self)
        self.about_page.setObjectName('aboutPage')
        self._nav_about = self.addSubInterface(self.about_page, FluentIcon.INFO, tr('nav.about'), position=NavigationItemPosition.BOTTOM)
        register_locale_refresh(self, self._refresh_nav_text)
        self._apply_content_surface_tint()
        if self.home_page is not None:
            self.switchTo(self.home_page)
        self.settings_page.settings_changed.connect(self._on_settings_changed)
        self._load_filters()
        self._auto_sync()
        self.splashScreen.finish()
        _maybe_show_alpha_disclaimer(self)
        _maybe_show_feature_onboarding(self)
        QTimer.singleShot(0, gc.collect)

    def _schedule_downloads_badge_update(self, *_args) -> None:
        self._downloads_badge_timer.start()

    def _update_downloads_badge(self, *_args) -> None:
        from src.core.downloader import DLState
        active = sum((1 for item in self.download_manager.items_in_order() if item.state in (DLState.queued, DLState.downloading, DLState.verifying)))
        if active == self._downloads_badge_count:
            return
        self._downloads_badge_count = active
        if self._downloads_badge is not None:
            self._downloads_badge.deleteLater()
            self._downloads_badge = None
        if active <= 0:
            return
        nav_widget = self.navigationInterface.widget(self.download_page.objectName())
        if nav_widget is None:
            return
        self._downloads_badge = InfoBadge.attension(active, parent=self, target=nav_widget, position=InfoBadgePosition.NAVIGATION_ITEM)

    def _on_global_theme_changed(self, *_args) -> None:
        self._apply_content_surface_tint()
        from PySide6.QtCore import QTimer
        from PySide6.QtWidgets import QApplication
        def _repolish():
            app = QApplication.instance()
            if app is not None:
                _force_full_repolish(app)
        QTimer.singleShot(0, _repolish)

    def _apply_content_surface_tint(self, *_args):
        style = settings_qss()
        self.settings_page.setStyleSheet(style)
        self.about_page.setStyleSheet(style)

    def _force_foreground_focus(self) -> None:
        if sys.platform != 'win32':
            self.raise_()
            self.activateWindow()
            return
        try:
            import ctypes
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

    def _enlarge_title_bar_branding(self) -> None:
        title_bar = getattr(self, 'titleBar', None)
        if title_bar is None:
            return
        icon_label = getattr(title_bar, 'iconLabel', None)
        if icon_label is not None:
            try:
                icon_size = 20
                icon_label.setFixedSize(icon_size, icon_size)
                pixmap = self.windowIcon().pixmap(icon_size, icon_size)
                if not pixmap.isNull():
                    icon_label.setPixmap(pixmap)
            except Exception:
                pass
        title_label = getattr(title_bar, 'titleLabel', None)
        if title_label is not None:
            try:
                font = title_label.font()
                font.setPointSize(font.pointSize() + 2)
                font.setBold(True)
                title_label.setFont(font)
            except Exception:
                pass

    def _init_tray_icon(self):
        from PySide6.QtWidgets import QSystemTrayIcon, QMenu
        from PySide6.QtGui import QAction
        if not QSystemTrayIcon.isSystemTrayAvailable():
            return
        self._tray_icon = QSystemTrayIcon(self.windowIcon(), self)
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
        from PySide6.QtWidgets import QSystemTrayIcon
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
    def _tr_or(key: str, fallback: str) -> str:
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
            box_layout.addWidget(StrongBodyLabel(self._tr_or('app.close_warning_title', 'Downloads in progress')))
            content_lbl = BodyLabel(self._tr_or(
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
            try:
                box.yesButton.clicked.disconnect()
            except TypeError:
                pass
            try:
                box.cancelButton.clicked.disconnect()
            except TypeError:
                pass
            box.yesButton.setText(self._tr_or('app.close_anyway', 'Close Anyway'))
            box.yesButton.clicked.connect(lambda: _pick('close'))
            box.cancelButton.setText(self._tr_or('app.close_cancel', 'Keep Downloading'))
            box.cancelButton.clicked.connect(lambda: _pick('cancel'))
            if self._tray_icon is not None:
                tray_btn = PushButton(self._tr_or('app.close_to_tray_instead', 'Minimize to Tray'))
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
                from PySide6.QtWidgets import QMessageBox
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

    def closeEvent(self, event):
        from src.core.config import settings as _s
        close_to_tray = getattr(_s, 'close_to_tray', False)
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
                from src.core.config import settings as _s2, apply_settings, save_settings
                _s2.close_to_tray = True
                apply_settings(close_to_tray=True)
                save_settings(_s2)
                try:
                    self.settings_page._chk_close_to_tray.setChecked(True)
                except Exception:
                    pass
                if self._tray_icon is not None:
                    event.ignore()
                    self.hide()
                    return
        try:
            self.download_manager.shutdown()
        except Exception:
            pass
        try:
            tasks.shutdown()
        except Exception:
            pass
        try:
            inner = getattr(self.music_page, '_real_page', None)
            if inner is not None:
                inner.shutdown()
        except Exception:
            pass
        try:
            inner = getattr(self.anime_page, '_real_page', None)
            if inner is not None:
                inner.shutdown()
        except Exception:
            pass
        try:
            inner = getattr(self.yt_page, '_real_page', None)
            if inner is not None:
                inner.shutdown()
        except Exception:
            pass
        try:
            inner = getattr(self.tv_page, '_real_page', None)
            if inner is not None:
                inner.shutdown()
        except Exception:
            pass
        try:
            self.yt_bridge.shutdown()
        except Exception:
            pass
        try:
            from src.core.cache import cache
            cache.clear_namespace('repacks_page')
            cache.clear_namespace('repacks_details')
        except Exception:
            pass
        if self._tray_icon is not None:
            self._tray_icon.hide()
        super().closeEvent(event)
        from PySide6.QtWidgets import QApplication
        QApplication.quit()

    def _on_settings_changed(self):
        try:
            self._sync_optional_pages()
        except Exception:
            pass
        try:
            if self.home_page:
                self.home_page.sync_model()
        except Exception:
            pass
        try:
            from src.core.config import settings as _s
            self.download_manager.set_global_limits(down_kbps=_s.speed_limit, up_kbps=getattr(_s, 'upload_speed_limit', 500))
        except Exception:
            pass
        try:
            from src.core.config import settings as _s
            seed_minutes = getattr(_s, 'seed_time', 0)
            for item_id in list(getattr(self.download_manager, '_items', {}).keys()):
                try:
                    self.download_manager.set_torrent_settings(item_id, seed_time_limit_min=seed_minutes)
                except Exception:
                    pass
        except Exception:
            pass

    def _remove_subinterface(self, page: Optional[QWidget]) -> None:
        if page is None:
            return
        try:
            self.navigationInterface.removeWidget(page.objectName())
        except Exception:
            pass
        try:
            self.stackedWidget.removeWidget(page)
        except Exception:
            pass
        page.deleteLater()

    def _on_yt_ffmpeg_declined(self) -> None:
        self.settings_page._chk_youtube.setChecked(False)

    def _sync_optional_pages(self) -> None:
        from src.core.config import settings as _s
        minerva_enabled = getattr(_s, 'minerva_enabled', True)
        pc_enabled = getattr(_s, 'pc_games_enabled', False)
        dat_enabled = getattr(_s, 'local_dat_enabled', False)
        music_enabled = getattr(_s, 'music_enabled', False)
        books_enabled = getattr(_s, 'books_enabled', False)
        if minerva_enabled and self.home_page is None:
            self.home_page = MinervaPage(self)
            self.home_page.setObjectName('minervaPage')
            self._nav_minerva = self.addSubInterface(self.home_page, FluentIcon.LIBRARY, tr('nav.minerva'), position=NavigationItemPosition.TOP)
        elif not minerva_enabled and self.home_page is not None:
            self._remove_subinterface(self.home_page)
            self.home_page = None
            self._nav_minerva = None
        if pc_enabled and self.pc_games_page is None:
            self.pc_games_page = _LazyRepacksPage(self.download_manager)
            self.pc_games_page.setObjectName('pcGamesPage')
            self._nav_pc_games = self.addSubInterface(self.pc_games_page, FluentIcon.GAME, tr('nav.pc_games'), position=NavigationItemPosition.TOP)
        elif not pc_enabled and self.pc_games_page is not None:
            self._remove_subinterface(self.pc_games_page)
            self.pc_games_page = None
            self._nav_pc_games = None
        if dat_enabled and self.local_dat_page is None:
            self.local_dat_page = _LazyPage(lambda: PlaceholderPage(FluentIcon.DOCUMENT, tr('nav.local_dat_title'), tr('nav.local_dat_message')))
            self.local_dat_page.setObjectName('localDatPage')
            self._nav_local_dat = self.addSubInterface(self.local_dat_page, FluentIcon.DOCUMENT, tr('nav.local_dat'), position=NavigationItemPosition.TOP)
        elif not dat_enabled and self.local_dat_page is not None:
            self._remove_subinterface(self.local_dat_page)
            self.local_dat_page = None
            self._nav_local_dat = None
        if music_enabled and self.music_page is None:
            self.music_page = _LazyMusicPage(self.download_manager)
            self.music_page.setObjectName('musicPage')
            self._nav_music = self.addSubInterface(self.music_page, FluentIcon.MUSIC, tr('nav.music'), position=NavigationItemPosition.TOP)
        elif not music_enabled and self.music_page is not None:
            inner = getattr(self.music_page, '_real_page', None)
            if inner is not None:
                try:
                    inner.shutdown()
                except Exception:
                    logger.exception('Failed to shut down music page on feature disable')
            self._remove_subinterface(self.music_page)
            self.music_page = None
            self._nav_music = None
        if books_enabled and self.books_page is None:
            self.books_page = _LazyBooksPage(self.download_manager)
            self.books_page.setObjectName('booksPage')
            self._nav_books = self.addSubInterface(self.books_page, FluentIcon.BOOK_SHELF, tr('nav.books'), position=NavigationItemPosition.TOP)
        elif not books_enabled and self.books_page is not None:
            self._remove_subinterface(self.books_page)
            self.books_page = None
            self._nav_books = None
        anime_enabled = getattr(_s, 'anime_enabled', False)
        if anime_enabled and self.anime_page is None:
            self.anime_page = _LazyAnimePage(self.download_manager)
            self.anime_page.setObjectName('animePage')
            self._nav_anime = self.addSubInterface(self.anime_page, FluentIcon.VIDEO, tr('nav.anime'), position=NavigationItemPosition.TOP)
        elif not anime_enabled and self.anime_page is not None:
            self._remove_subinterface(self.anime_page)
            self.anime_page = None
            self._nav_anime = None
        youtube_enabled = getattr(_s, 'youtube_enabled', False)
        if youtube_enabled and self.yt_page is None:
            self.yt_page = _LazyYtPage(self.yt_bridge)
            self.yt_page.setObjectName('ytPage')
            self.yt_page.ffmpeg_declined.connect(self._on_yt_ffmpeg_declined)
            self._nav_youtube = self.addSubInterface(self.yt_page, FluentIcon.PLAY, tr('nav.youtube'), position=NavigationItemPosition.TOP)
        elif not youtube_enabled and self.yt_page is not None:
            self._remove_subinterface(self.yt_page)
            self.yt_page = None
            self._nav_youtube = None
        tv_enabled = getattr(_s, 'tv_enabled', False)
        if tv_enabled and self.tv_page is None:
            self.tv_page = _LazyTVPage()
            self.tv_page.setObjectName('tvPageNav')
            self._nav_tv = self.addSubInterface(self.tv_page, FluentIcon.PROJECTOR, tr('nav.tv'), position=NavigationItemPosition.TOP)
        elif not tv_enabled and self.tv_page is not None:
            self._remove_subinterface(self.tv_page)
            self.tv_page = None
            self._nav_tv = None
        QTimer.singleShot(0, lambda: gc.collect(0))

    def _on_open_data_folder(self) -> None:
        import os
        import subprocess
        import sys
        from src.core.config import paths
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

    def _refresh_nav_text(self) -> None:
        pairs = ((getattr(self, '_nav_downloads', None), 'nav.downloads'), (getattr(self, '_nav_settings', None), 'nav.settings'), (getattr(self, '_nav_about', None), 'nav.about'), (getattr(self, '_nav_minerva', None), 'nav.minerva'), (getattr(self, '_nav_pc_games', None), 'nav.pc_games'), (getattr(self, '_nav_local_dat', None), 'nav.local_dat'), (getattr(self, '_nav_music', None), 'nav.music'), (getattr(self, '_nav_books', None), 'nav.books'), (getattr(self, '_nav_anime', None), 'nav.anime'), (getattr(self, '_nav_youtube', None), 'nav.youtube'))
        for widget, key in pairs:
            if widget is not None:
                try:
                    widget.setText(tr(key))
                except RuntimeError:
                    pass
        try:
            data_folder_item = self.navigationInterface.widget('openDataFolder')
            if data_folder_item is not None:
                data_folder_item.setText(tr('nav.open_data_folder'))
        except Exception:
            pass

    def _load_filters(self):
        if self.home_page is None:
            return
        try:
            self.home_page.refresh_filters()
        except Exception:
            pass
        try:
            self.home_page.sync_model()
        except Exception:
            pass

    def _auto_sync(self):
        if self.home_page is None:
            return
        from src.core.config import settings as _s
        if getattr(_s, 'sync_prompt_shown', False):
            return
        try:
            count = db.count_roms()
        except Exception:
            count = 0
        if count != 0:
            return
        _s.sync_prompt_shown = True
        try:
            save_settings(_s)
        except Exception:
            pass
        card = SyncPromptCard(self)
        card.sync_requested.connect(self.home_page.run_sync)
        card.show_anchored(self)

class SyncPromptCard(CardWidget):
    sync_requested = Signal()
    dismissed = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedWidth(360)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 16, 18, 16)
        layout.setSpacing(10)
        header = QHBoxLayout()
        header.setSpacing(10)
        icon = IconWidget(FluentIcon.INFO, self)
        icon.setFixedSize(20, 20)
        header.addWidget(icon, 0, Qt.AlignmentFlag.AlignVCenter)
        self._title_lbl = StrongBodyLabel('')
        header.addWidget(self._title_lbl, 1, Qt.AlignmentFlag.AlignVCenter)
        self._close_btn = TransparentToolButton(FluentIcon.CLOSE, self)
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

def _force_full_repolish(app) -> None:
    _apply_qfluentwidgets_font_families()
    from PySide6.QtWidgets import QWidget
    for top_level in app.topLevelWidgets():
        style = top_level.style()
        style.unpolish(top_level)
        style.polish(top_level)
        top_level.update()
        for child in top_level.findChildren(QWidget):
            child_style = child.style()
            child_style.unpolish(child)
            child_style.polish(child)
            child.update()

def _apply_qfluentwidgets_font_families() -> None:
    try:
        from qfluentwidgets import setFontFamilies
    except ImportError:
        return
    try:
        setFontFamilies(['Figtree', 'Segoe UI', 'Microsoft YaHei', 'PingFang SC'])
    except Exception:
        pass

def _build_app_font() -> 'QFont':
    from PySide6.QtGui import QFont
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

def create_application(argv: Optional[list]=None):
    import sys
    from PySide6.QtWidgets import QApplication
    from PySide6.QtGui import QSurfaceFormat
    from qfluentwidgets import setFont
    if sys.platform == 'win32':
        os.environ.setdefault('QT_OPENGL', 'desktop')
    surface_format = QSurfaceFormat()
    surface_format.setSwapInterval(1)
    surface_format.setSwapBehavior(QSurfaceFormat.SwapBehavior.DoubleBuffer)
    surface_format.setSamples(2)
    QSurfaceFormat.setDefaultFormat(surface_format)
    app = QApplication(argv if argv is not None else sys.argv)
    app.setLayoutDirection(Qt.LayoutDirection.LeftToRight)
    app.setApplicationName('PiraChest')
    app.setApplicationDisplayName('PiraChest')
    app.setOrganizationName('PiraChest')
    app.setQuitOnLastWindowClosed(False)
    _load_bundled_figtree_fonts()
    _apply_qfluentwidgets_font_families()
    base_font = _build_app_font()
    app.setFont(base_font)
    setFont(app, fontSize=base_font.pointSize())
    return app