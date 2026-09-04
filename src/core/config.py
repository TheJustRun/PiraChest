from __future__ import annotations
import json
import logging
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
logger = logging.getLogger(__name__)

def _detect_app_root() -> str:
    if getattr(sys, 'frozen', False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

def _detect_user_data_root() -> str:
    appimage_path = os.environ.get('APPIMAGE')
    if appimage_path:
        base = os.path.dirname(os.path.abspath(appimage_path))
    elif getattr(sys, 'frozen', False):
        base = os.path.dirname(os.path.abspath(sys.executable))
    else:
        base = _detect_app_root()
    return os.path.join(base, 'data')

def is_admin() -> bool:
    if sys.platform != 'win32':
        return False
    import ctypes
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False

def relaunch_as_admin() -> bool:
    if sys.platform != 'win32':
        return False
    import ctypes
    try:
        params = ' '.join((f'"{a}"' for a in sys.argv))
        ctypes.windll.shell32.ShellExecuteW(None, 'runas', sys.executable, params, None, 1)
        return True
    except Exception:
        return False

def detect_windows_accent_color() -> str:
    fallback = '#00b7c3'
    if sys.platform != 'win32':
        return fallback
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, 'Software\\Microsoft\\Windows\\DWM') as key:
            value, _ = winreg.QueryValueEx(key, 'ColorizationColor')
        argb = int(value) & 4294967295
        r = argb >> 16 & 255
        g = argb >> 8 & 255
        b = argb & 255
        return f'#{r:02x}{g:02x}{b:02x}'
    except Exception:
        logger.warning('Could not read Windows accent color; using fallback')
        return fallback
_PROJECT_ROOT = _detect_app_root()
_APP_DATA_DIR = _detect_user_data_root()

@dataclass(frozen=True)
class Paths:
    project_root: str = _PROJECT_ROOT
    app_data_dir: str = _APP_DATA_DIR
    data_dir: str = field(default_factory=lambda: os.path.join(_APP_DATA_DIR, 'index'))
    db_path: str = field(default_factory=lambda: os.path.join(_APP_DATA_DIR, 'index', 'minerva_index.db'))
    download_root: str = field(default_factory=lambda: os.path.join(_APP_DATA_DIR, 'downloads'))
    torrent_cache: str = field(default_factory=lambda: os.path.join(_APP_DATA_DIR, 'downloads', 'torrents'))
    cache_dir: str = field(default_factory=lambda: os.path.join(_APP_DATA_DIR, 'cache'))
    config_dir: str = field(default_factory=lambda: os.path.join(_APP_DATA_DIR, 'config'))
    yt_dir: str = field(default_factory=lambda: os.path.join(_APP_DATA_DIR, 'yt'))

    def ensure_dirs(self) -> None:
        for d in (self.app_data_dir, self.data_dir, self.download_root, self.torrent_cache, self.cache_dir, self.config_dir, self.yt_dir):
            try:
                os.makedirs(d, exist_ok=True)
            except OSError as exc:
                logger.error('Could not create directory %s: %s', d, exc)

@dataclass(frozen=True)
class Network:
    cdn_base: str = 'https://cdn.minerva-archive.org/torrents'
    torrent_download_timeout: int = 60
    metadata_timeout: int = 300
    max_retries: int = 3

@dataclass(frozen=True)
class LibtorrentDefaults:
    speed_limit: int = 0
    seed_time: int = 0
    max_upload_speed: int = 0
    check_integrity: bool = False
    enable_dht: bool = True
    enable_peer_exchange: bool = True
    sequential_download: bool = True
    bt_stop_timeout: int = 300
    max_connections_per_torrent: int = 400
    max_uploads_per_torrent: int = 40
    extra_trackers: tuple[str, ...] = ('udp://tracker.opentrackr.org:1337/announce', 'udp://open.stealth.si:80/announce', 'udp://tracker.torrent.eu.org:451/announce', 'udp://exodus.desync.com:6969/announce', 'udp://tracker.openbittorrent.com:6969/announce')

class ThemeMode:
    DARK = 'Dark'
    LIGHT = 'Light'
    AUTO = 'Auto'

@dataclass
class Settings:
    download_dir: str = field(default_factory=lambda: Paths().download_root)
    download_dir_repacks: str = field(default_factory=lambda: os.path.join(Paths().download_root, 'repacks'))
    download_dir_minerva: str = field(default_factory=lambda: Paths().download_root)
    download_dir_music: str = field(default_factory=lambda: os.path.join(Paths().download_root, 'music'))
    download_dir_anime: str = field(default_factory=lambda: os.path.join(Paths().download_root, 'anime'))
    download_dir_youtube: str = field(default_factory=lambda: os.path.join(Paths().download_root, 'youtube'))
    download_dir_manga: str = field(default_factory=lambda: os.path.join(Paths().download_root, 'manga'))
    download_dir_books: str = field(default_factory=lambda: os.path.join(Paths().download_root, 'books'))
    speed_limit: int = 0
    upload_speed_limit: int = 500
    seed_time: int = 0
    auto_download: bool = False
    delete_torrent_after: bool = True
    theme_mode: str = ThemeMode.DARK
    hide_alpha_disclaimer: bool = False
    onboarding_completed: bool = False
    sync_prompt_shown: bool = False
    pc_games_enabled: bool = False
    minerva_enabled: bool = False
    local_dat_enabled: bool = False
    music_enabled: bool = False
    books_enabled: bool = False
    anime_enabled: bool = False
    tv_enabled: bool = False
    youtube_enabled: bool = False
    accent_color: str = field(default_factory=detect_windows_accent_color)
    close_to_tray: bool = False
    admin_mode: bool = False
    show_console: bool = False
    log_to_file: bool = False
    language: str = 'en'
    on_finish_action: str = 'nothing'

    def to_dict(self) -> dict:
        return {k: v for k, v in self.__dict__.items()}

    @classmethod
    def from_dict(cls, data: dict) -> Settings:
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})
def _legacy_os_app_data_dir() -> str:
    if sys.platform == 'win32':
        base = os.environ.get('LOCALAPPDATA') or os.path.expanduser('~\\AppData\\Local')
    elif sys.platform == 'darwin':
        base = os.path.expanduser('~/Library/Application Support')
    else:
        base = os.environ.get('XDG_DATA_HOME') or os.path.expanduser('~/.local/share')
    return os.path.join(base, 'PiraChest')

def _migrate_legacy_app_data(paths: Paths) -> None:
    if os.listdir(paths.app_data_dir) if os.path.isdir(paths.app_data_dir) else False:
        return
    candidates = [os.path.join(_PROJECT_ROOT, 'data'), _legacy_os_app_data_dir()]
    for legacy_dir in candidates:
        if os.path.abspath(legacy_dir) == os.path.abspath(paths.app_data_dir):
            continue
        if not os.path.isdir(legacy_dir) or not os.listdir(legacy_dir):
            continue
        try:
            for name in os.listdir(legacy_dir):
                src = os.path.join(legacy_dir, name)
                dst = os.path.join(paths.app_data_dir, name)
                if not os.path.exists(dst):
                    os.replace(src, dst)
            logger.info('Migrated legacy app data from %s to %s', legacy_dir, paths.app_data_dir)
        except OSError as exc:
            logger.warning('Legacy app data migration failed: %s', exc)
        return

def dir_size_bytes(path: str) -> int:
    total = 0
    if not os.path.isdir(path):
        return 0
    for dirpath, _dirnames, filenames in os.walk(path):
        for name in filenames:
            fp = os.path.join(dirpath, name)
            try:
                total += os.path.getsize(fp)
            except OSError:
                pass
    return total


def format_size(n: float) -> str:
    for unit in ('B', 'KB', 'MB', 'GB', 'TB'):
        if abs(n) < 1024.0:
            return f'{n:.1f} {unit}'
        n /= 1024.0
    return f'{n:.1f} PB'


def clear_cache_dirs() -> None:
    import shutil
    for d in (paths.cache_dir, paths.torrent_cache):
        if not os.path.isdir(d):
            continue
        for name in os.listdir(d):
            fp = os.path.join(d, name)
            try:
                if os.path.isdir(fp):
                    shutil.rmtree(fp, ignore_errors=True)
                else:
                    os.remove(fp)
            except OSError:
                pass


def wipe_all_app_data() -> None:
    import shutil
    root = paths.app_data_dir
    if not os.path.isdir(root):
        return
    for name in os.listdir(root):
        fp = os.path.join(root, name)
        try:
            if os.path.isdir(fp):
                shutil.rmtree(fp, ignore_errors=True)
            else:
                os.remove(fp)
        except OSError:
            pass


paths = Paths()
network = Network()
libtorrent_defaults = LibtorrentDefaults()
paths.ensure_dirs()
if os.path.isdir(paths.app_data_dir):
    _migrate_legacy_app_data(paths)
_CONFIG_DIR = paths.config_dir
_SETTINGS_FILE = os.path.join(_CONFIG_DIR, 'pirachest_settings.json')
_LEGACY_SETTINGS_FILE = os.path.join(_CONFIG_DIR, 'minerva_settings.json')
_LEGACY_ROOT_CONFIG_DIR = os.path.join(_PROJECT_ROOT, '.config')
_LEGACY_ROOT_SETTINGS_FILE = os.path.join(_LEGACY_ROOT_CONFIG_DIR, 'pirachest_settings.json')

def load_settings() -> Settings:
    path = _SETTINGS_FILE
    if not os.path.isfile(path) and os.path.isfile(_LEGACY_SETTINGS_FILE):
        path = _LEGACY_SETTINGS_FILE
    if not os.path.isfile(path) and os.path.isfile(_LEGACY_ROOT_SETTINGS_FILE):
        path = _LEGACY_ROOT_SETTINGS_FILE
    if os.path.isfile(path):
        try:
            with open(path, 'r', encoding='utf-8') as fh:
                data = json.load(fh)
            loaded = Settings.from_dict(data)
            if 'onboarding_completed' not in data:
                loaded.onboarding_completed = True
            if 'download_dir_repacks' not in data and 'download_dir' in data:
                loaded.download_dir_repacks = data['download_dir']
            if 'download_dir_minerva' not in data and 'download_dir' in data:
                loaded.download_dir_minerva = data['download_dir']
            return loaded
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning('Failed to load settings: %s — using defaults', exc)
    return Settings()

def save_settings(s: Settings) -> None:
    os.makedirs(_CONFIG_DIR, exist_ok=True)
    data = s.to_dict()
    try:
        with open(_SETTINGS_FILE, 'w', encoding='utf-8') as fh:
            json.dump(data, fh, indent=2)
        logger.info('Settings saved to %s', _SETTINGS_FILE)
    except OSError as exc:
        logger.error('Failed to save settings: %s', exc)
settings = load_settings()

def apply_settings(**kwargs) -> None:
    for key, value in kwargs.items():
        if hasattr(settings, key):
            setattr(settings, key, value)
            logger.info('Settings.%s = %s', key, value)

def resolve_theme(qfluent_set_theme) -> None:
    from qfluentwidgets import Theme as FWTheme
    mode = settings.theme_mode
    if mode == ThemeMode.AUTO:
        try:
            import darketect
            theme = FWTheme.DARK if darketect.is_dark() else FWTheme.LIGHT
        except ImportError:
            logger.warning('darketect not available; defaulting to dark theme for AUTO mode')
            theme = FWTheme.DARK
    elif mode == ThemeMode.LIGHT:
        theme = FWTheme.LIGHT
    else:
        theme = FWTheme.DARK
    qfluent_set_theme(theme)
    logger.info('Theme resolved to %s (mode=%s)', theme, mode)
