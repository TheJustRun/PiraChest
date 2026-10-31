from __future__ import annotations
import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from contextlib import suppress
from dataclasses import dataclass, field, fields
from datetime import datetime, timezone
from PySide6.QtCore import QObject, Signal
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

_ADMIN_CACHE: bool | None = None

def is_admin() -> bool:
    global _ADMIN_CACHE
    if _ADMIN_CACHE is not None:
        return _ADMIN_CACHE
    if sys.platform != 'win32':
        _ADMIN_CACHE = False
        return False
    import ctypes
    try:
        _ADMIN_CACHE = bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        _ADMIN_CACHE = False
    return _ADMIN_CACHE

_STARTUP_RUN_KEY_PATH = r'Software\Microsoft\Windows\CurrentVersion\Run'
_STARTUP_APPROVED_KEY_PATH = r'Software\Microsoft\Windows\CurrentVersion\Explorer\StartupApproved\Run'
_STARTUP_VALUE_NAME = 'PiraChest'

def _startup_command() -> str:
    if getattr(sys, 'frozen', False):
        return f'"{os.path.abspath(sys.executable)}"'
    return f'"{os.path.abspath(sys.executable)}" "{os.path.abspath(sys.argv[0])}"'

def _startup_approved_disabled() -> bool:
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _STARTUP_APPROVED_KEY_PATH, 0, winreg.KEY_QUERY_VALUE) as key:
            data, _ = winreg.QueryValueEx(key, _STARTUP_VALUE_NAME)
        return bool(data) and data[0] & 1 == 1
    except OSError:
        return False

def get_launch_at_startup() -> bool:
    if sys.platform != 'win32':
        return False
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _STARTUP_RUN_KEY_PATH, 0, winreg.KEY_QUERY_VALUE) as key:
            value, _ = winreg.QueryValueEx(key, _STARTUP_VALUE_NAME)
    except OSError:
        return False
    return bool(value) and not _startup_approved_disabled()

def set_launch_at_startup(enabled: bool) -> bool:
    if sys.platform != 'win32':
        return False
    import winreg
    try:
        if enabled:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _STARTUP_RUN_KEY_PATH, 0, winreg.KEY_SET_VALUE) as key:
                winreg.SetValueEx(key, _STARTUP_VALUE_NAME, 0, winreg.REG_SZ, _startup_command())
            try:
                with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _STARTUP_APPROVED_KEY_PATH, 0, winreg.KEY_SET_VALUE) as key:
                    winreg.DeleteValue(key, _STARTUP_VALUE_NAME)
            except OSError:
                pass
        else:
            try:
                with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _STARTUP_RUN_KEY_PATH, 0, winreg.KEY_SET_VALUE) as key:
                    winreg.DeleteValue(key, _STARTUP_VALUE_NAME)
            except FileNotFoundError:
                pass
        return True
    except OSError as exc:
        logger.warning('Failed to update the startup entry: %s', exc)
        return False

def relaunch_as_admin() -> bool:
    if sys.platform != 'win32':
        return False
    import ctypes
    try:
        params = ' '.join(f'"{a}"' for a in sys.argv)
        ctypes.windll.shell32.ShellExecuteW(None, 'runas', sys.executable, params, None, 1)
        return True
    except Exception:
        return False

def relaunch_app() -> bool:
    cmd = [sys.executable] if getattr(sys, 'frozen', False) else [sys.executable, os.path.abspath(sys.argv[0])]
    try:
        subprocess.Popen(cmd + sys.argv[1:], env={**os.environ, _RESTART_ENV: '1'}, close_fds=True,
                         **({'creationflags': 0x00000008 | 0x00000200} if sys.platform == 'win32' else {'start_new_session': True}))
        return True
    except OSError as exc:
        logger.warning('Failed to relaunch: %s', exc)
        return False

_RESTART_ENV = 'PIRACHEST_RESTART'


APP_ACCENT_COLOR = '#0295fe'

def detect_windows_accent_color() -> str:
    fallback = APP_ACCENT_COLOR
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
    app_data_dir: str = _APP_DATA_DIR
    data_dir: str = field(default_factory=lambda: os.path.join(_APP_DATA_DIR, 'index'))
    db_path: str = field(default_factory=lambda: os.path.join(_APP_DATA_DIR, 'index', 'minerva_index.db'))
    download_root: str = field(default_factory=lambda: os.path.join(_APP_DATA_DIR, 'downloads'))
    torrent_cache: str = field(default_factory=lambda: os.path.join(_APP_DATA_DIR, 'downloads', 'torrents'))
    cache_dir: str = field(default_factory=lambda: os.path.join(_APP_DATA_DIR, 'cache'))
    stream_cache: str = field(default_factory=lambda: os.path.join(_APP_DATA_DIR, 'cache', 'stream'))
    config_dir: str = field(default_factory=lambda: os.path.join(_APP_DATA_DIR, 'config'))
    yt_dir: str = field(default_factory=lambda: os.path.join(_APP_DATA_DIR, 'yt'))

    def ensure_dirs(self) -> None:
        for d in (self.app_data_dir, self.data_dir, self.download_root, self.torrent_cache, self.cache_dir, self.stream_cache, self.config_dir, self.yt_dir):
            try:
                os.makedirs(d, exist_ok=True)
            except OSError as exc:
                logger.error('Could not create directory %s: %s', d, exc)

@dataclass(frozen=True)
class Network:
    cdn_base: str = 'https://cdn.minerva-archive.org'
    torrent_download_timeout: int = 60
    tmdb_proxy_base: str = 'https://tmdb-proxy.justrun.workers.dev/3'
    tmdb_image_base: str = 'https://image.tmdb.org/t/p'

@dataclass(frozen=True)
class LibtorrentDefaults:
    enable_dht: bool = True
    max_connections_per_torrent: int = 400
    extra_trackers: tuple[str, ...] = ('udp://tracker.opentrackr.org:1337/announce', 'udp://open.stealth.si:80/announce', 'udp://tracker.torrent.eu.org:451/announce', 'udp://exodus.desync.com:6969/announce', 'udp://tracker.openbittorrent.com:6969/announce')

class ThemeMode:
    DARK = 'Dark'
    LIGHT = 'Light'
    AUTO = 'Auto'


DOWNLOAD_DIR_SUBFOLDERS: dict[str, str] = {
    'download_dir_minerva': '',
    'download_dir_repacks': 'repacks',
    'download_dir_music': 'music',
    'download_dir_anime': 'anime',
    'download_dir_youtube': 'youtube',
    'download_dir_manga': 'manga',
    'download_dir_books': 'books',
    'download_dir_tbhb': 'tbhb',
}


def default_download_dir(attr: str) -> str:
    sub = DOWNLOAD_DIR_SUBFOLDERS.get(attr, '')
    return os.path.join(paths.download_root, sub) if sub else paths.download_root


def download_dir_changes(root: str, subfolders: bool = True) -> dict[str, str]:
    root = os.path.normpath(root)
    changes = {'download_dir': root}
    for attr, sub in DOWNLOAD_DIR_SUBFOLDERS.items():
        changes[attr] = os.path.join(root, sub) if (subfolders and sub) else root
    return changes


def check_dir_writable(path: str) -> str:
    try:
        os.makedirs(path, exist_ok=True)
        with tempfile.TemporaryFile(dir=path):
            pass
    except OSError as exc:
        return exc.strerror or str(exc)
    return ''


def free_space_bytes(path: str) -> int | None:
    probe = os.path.abspath(path)
    while not os.path.exists(probe):
        parent = os.path.dirname(probe)
        if parent == probe:
            return None
        probe = parent
    try:
        return shutil.disk_usage(probe).free
    except OSError:
        return None


def _dir_field(attr: str):
    return field(default_factory=lambda: default_download_dir(attr))


@dataclass
class Settings:
    download_dir: str = _dir_field('download_dir')
    download_dir_repacks: str = _dir_field('download_dir_repacks')
    download_dir_minerva: str = _dir_field('download_dir_minerva')
    download_dir_music: str = _dir_field('download_dir_music')
    download_dir_anime: str = _dir_field('download_dir_anime')
    download_dir_youtube: str = _dir_field('download_dir_youtube')
    download_dir_manga: str = _dir_field('download_dir_manga')
    download_dir_books: str = _dir_field('download_dir_books')
    download_dir_tbhb: str = _dir_field('download_dir_tbhb')
    speed_limit: int = 0
    upload_speed_limit: int = 0
    torrent_port: int = 6881
    max_active_downloads: int = 3
    http_connections: int = 8
    net: dict = field(default_factory=dict)
    seed_time: int = 0
    delete_torrent_after: bool = True
    theme_mode: str = ThemeMode.DARK
    theme_preset: str | None = None
    hide_vpn_disclaimer: bool = False
    onboarding_completed: bool = False
    sync_prompt_shown: bool = False
    pc_games_enabled: bool = False
    steam_enrichment_enabled: bool = False
    steam_banner: bool = False
    steam_logo: bool = False
    steam_description: bool = False
    steam_proxy_url: str = ''
    repack_show_titles: bool = False
    minerva_enabled: bool = False
    minerva_extract_roms: bool = False
    music_enabled: bool = False
    books_enabled: bool = False
    tv_enabled: bool = False
    tv_stream_enabled: bool = True
    tv_stream_addon_base: str = 'https://torrentio.strem.fun'
    tv_stream_addons: str = ''
    tv_auto_pick: bool = False
    tv_pick_quality: str = 'auto'
    tv_pick_provider: str = 'auto'
    tv_tbhb_enabled: bool = True
    tv_pick_min_gb: int = 0
    tv_pick_max_gb: int = 0
    tv_stream_cache_limit_gb: int = 0
    tv_stream_resume: bool = True
    tv_stream_fallback: bool = True
    tv_stream_connect_timeout: int = 60
    tv_stream_prebuffer_mb: int = 4
    tv_stream_clear_on_exit: bool = False
    tv_external_enabled: bool = True
    tv_vyla_enabled: bool = True
    tv_external_auto_setup: bool = True
    tv_external_timeout: int = 30
    tv_node_path: str = ''
    tv_skip_intro: bool = False
    tv_skip_credits: bool = False
    tv_autonext: bool = False
    tv_pip_always_on_top: bool = True
    debrid_enabled: bool = False
    debrid_provider: str = 'realdebrid'
    debrid_auto_repacks: bool = True
    debrid_auto_tv: bool = True
    debrid_auto_tbhb: bool = True
    debrid_auto_direct: bool = False
    debrid_fallback: bool = True
    debrid_stream: bool = True
    debrid_delete_after: bool = False
    debrid_timeout: int = 600
    sailseek_enabled: bool = True
    sailseek_translucent: bool = True
    sailseek_show_artwork: bool = True
    sailseek_show_filters: bool = True
    sailseek_remember_position: bool = True
    sailseek_delay_ms: int = 400
    sailseek_max_results: int = 30
    sailseek_categories: dict = field(default_factory=dict)
    youtube_enabled: bool = False
    tbhb_enabled: bool = False
    accent_color: str = APP_ACCENT_COLOR
    close_to_tray: bool = False
    external_torrent_client: bool = False
    admin_mode: bool = False
    launch_at_startup: bool = False
    show_console: bool = False
    log_to_file: bool = True
    language: str = 'en'
    on_finish_action: str = 'nothing'
    associations_enabled: bool = False
    associations_exe_path: str = ''
    associations_user_disabled: bool = False
    skip_update_tag: str = ''
    notify_on_download_complete: bool = True
    sfx_volume: int = 45
    ambiance_volume: int = 0
    background_image_enabled: bool = False
    background_image_path: str = ''
    background_image_blur: int = 12
    background_image_darken: int = 45
    console_structure: bool = True
    tabvis_repacks_fitgirl_enabled: bool = True
    tabvis_repacks_gog_enabled: bool = True
    tabvis_tv_movies_enabled: bool = True
    tabvis_tv_series_enabled: bool = True
    tabvis_tv_anime_enabled: bool = True
    tabvis_tv_channels_enabled: bool = True
    tabvis_books_books_enabled: bool = False
    tabvis_books_manga_enabled: bool = True
    auto_install_games: bool = False
    experimental_acknowledged: bool = False


    keyboard_shortcuts: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return dict(self.__dict__)

    @classmethod
    def from_dict(cls, data: dict) -> Settings:
        defaults = cls()
        clean: dict = {}
        for f in fields(cls):
            if f.name not in data:
                continue
            value = _coerce(data[f.name], getattr(defaults, f.name))
            if value is _INVALID:
                logger.warning('Ignoring invalid saved value for %s: %r', f.name, data[f.name])
                continue
            lo_hi = _RANGES.get(f.name)
            if lo_hi is not None:
                value = min(max(value, lo_hi[0]), lo_hi[1])
            clean[f.name] = value
        loaded = cls(**clean)
        if 'net' in clean:
            loaded.net = _sanitize_net(clean['net'])
        return loaded


_INVALID = object()

_NET_PROXY_TYPES = frozenset(('none', 'http', 'socks5'))
_NET_BOOL_KEYS = ('proxy_peers', 'force_proxy', 'anonymous', 'require_encryption', 'dht', 'lsd', 'upnp', 'private_history')
_NET_TEXT_KEYS = ('proxy_host', 'proxy_user', 'proxy_pass', 'bind')
_MAX_NET_TEXT = 2048


def _sanitize_net(value: dict) -> dict:
    if not isinstance(value, dict):
        return {}
    clean: dict = {}
    proxy_type = value.get('proxy_type')
    if isinstance(proxy_type, str) and proxy_type in _NET_PROXY_TYPES:
        clean['proxy_type'] = proxy_type
    port = value.get('proxy_port')
    if isinstance(port, int) and not isinstance(port, bool):
        clean['proxy_port'] = max(0, min(port, 65535))
    for key in _NET_TEXT_KEYS:
        text = value.get(key)
        if isinstance(text, str):
            clean[key] = text[:_MAX_NET_TEXT]
    for key in _NET_BOOL_KEYS:
        flag = value.get(key)
        if isinstance(flag, bool):
            clean[key] = flag
    return clean


_RANGES: dict[str, tuple[float, float]] = {
    'speed_limit': (0, 10_000_000),
    'upload_speed_limit': (0, 10_000_000),
    'seed_time': (0, 9999),
    'torrent_port': (0, 65535),
    'max_active_downloads': (1, 64),
    'http_connections': (1, 64),
    'sfx_volume': (0, 100),
    'ambiance_volume': (0, 100),
    'background_image_blur': (0, 40),
    'background_image_darken': (0, 90),
    'sailseek_max_results': (5, 100),
    'sailseek_delay_ms': (250, 2000),
    'debrid_timeout': (60, 7200),
}


def _coerce(value, default):
    if default is None:
        return value if value is None or isinstance(value, str) else _INVALID
    if isinstance(default, bool):
        return value if isinstance(value, bool) else (bool(value) if value in (0, 1) else _INVALID)
    if isinstance(default, int):
        if isinstance(value, bool):
            return _INVALID
        if isinstance(value, (int, float)):
            return int(value)
        return _INVALID
    if isinstance(default, float):
        return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else _INVALID
    if isinstance(default, str):
        return value if isinstance(value, str) else _INVALID
    if isinstance(default, dict):
        return value if isinstance(value, dict) else _INVALID
    if isinstance(default, list):
        return value if isinstance(value, list) else _INVALID
    return value
def _legacy_os_app_data_dir() -> str:
    if sys.platform == 'win32':
        base = os.environ.get('LOCALAPPDATA') or os.path.expanduser('~\\AppData\\Local')
    elif sys.platform == 'darwin':
        base = os.path.expanduser('~/Library/Application Support')
    else:
        base = os.environ.get('XDG_DATA_HOME') or os.path.expanduser('~/.local/share')
    return os.path.join(base, 'PiraChest')

def _migrate_legacy_app_data(paths: Paths) -> None:
    if os.path.isdir(paths.app_data_dir) and os.listdir(paths.app_data_dir):
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
    try:
        with os.scandir(path) as entries:
            for entry in entries:
                try:
                    total += dir_size_bytes(entry.path) if entry.is_dir(follow_symlinks=False) else entry.stat(follow_symlinks=False).st_size
                except OSError:
                    pass
    except OSError:
        pass
    return total


def format_size(n: float) -> str:
    for unit in ('B', 'KB', 'MB', 'GB', 'TB'):
        if abs(n) < 1024.0:
            return f'{n:.1f} {unit}'
        n /= 1024.0
    return f'{n:.1f} PB'


def empty_dir(path: str) -> None:
    try:
        with os.scandir(path) as entries:
            children = list(entries)
    except OSError:
        return
    for entry in children:
        try:
            if entry.is_dir(follow_symlinks=False):
                shutil.rmtree(entry.path, ignore_errors=True)
            else:
                os.remove(entry.path)
        except OSError:
            logger.debug('Could not remove %s', entry.path, exc_info=True)


def wipe_all_app_data() -> None:
    empty_dir(paths.app_data_dir)


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
    path = next((c for c in (_SETTINGS_FILE, _LEGACY_SETTINGS_FILE, _LEGACY_ROOT_SETTINGS_FILE) if os.path.isfile(c)), None)
    if path is None:
        return Settings()
    try:
        with open(path, encoding='utf-8') as fh:
            data = json.load(fh)
        if not isinstance(data, dict):
            raise ValueError('settings file is not a JSON object')
    except (ValueError, OSError) as exc:
        logger.warning('Failed to load settings: %s — using defaults', exc)
        _backup_corrupt(path)
        return Settings(onboarding_completed=True)
    loaded = Settings.from_dict(data)
    if 'onboarding_completed' not in data:
        loaded.onboarding_completed = True
    for attr in ('download_dir_repacks', 'download_dir_minerva'):
        if attr not in data and 'download_dir' in data:
            setattr(loaded, attr, loaded.download_dir)
    for attr in DOWNLOAD_DIR_SUBFOLDERS:
        if not getattr(loaded, attr):
            setattr(loaded, attr, default_download_dir(attr))
    if not loaded.download_dir:
        loaded.download_dir = paths.download_root
    if loaded.auto_install_games and not loaded.experimental_acknowledged:
        loaded.auto_install_games = False
        save_settings(loaded)
    return loaded


def _backup_corrupt(path: str) -> None:
    try:
        os.replace(path, path + '.corrupt')
    except OSError:
        pass


_SAVE_LOCK = threading.Lock()
_last_saved: str | None = None


def save_settings(s: Settings) -> None:
    global _last_saved
    try:
        payload = json.dumps(s.to_dict(), indent=2)
    except (TypeError, ValueError) as exc:
        logger.error('Settings are not serialisable: %s', exc)
        return
    with _SAVE_LOCK:
        if payload == _last_saved:
            return
        tmp = _SETTINGS_FILE + '.tmp'
        try:
            os.makedirs(_CONFIG_DIR, exist_ok=True)
            with open(tmp, 'w', encoding='utf-8') as fh:
                fh.write(payload)
            with suppress(OSError):
                os.chmod(tmp, 0o600)
            os.replace(tmp, _SETTINGS_FILE)
            with suppress(OSError):
                os.chmod(_SETTINGS_FILE, 0o600)
            _last_saved = payload
            logger.info('Settings saved to %s', _SETTINGS_FILE)
        except OSError as exc:
            logger.error('Failed to save settings: %s', exc)
            with suppress(OSError):
                os.remove(tmp)


_SETTINGS_BACKUP_FORMAT = 'pirachest-settings-backup'
_SETTINGS_BACKUP_VERSION = 1
_MAX_BACKUP_BYTES = 4 * 1024 * 1024
_BACKUP_LOCAL_ONLY = frozenset({'experimental_acknowledged'})


def settings_backup_payload() -> dict:
    data = settings.to_dict()
    for key in _BACKUP_LOCAL_ONLY:
        data.pop(key, None)
    return {
        'app': _APP_NAME,
        'format': _SETTINGS_BACKUP_FORMAT,
        'format_version': _SETTINGS_BACKUP_VERSION,
        'created_at': datetime.now(timezone.utc).isoformat(timespec='seconds'),
        'settings': data,
    }


def write_settings_backup(path: str) -> str:
    target = os.path.abspath(path)
    payload = json.dumps(settings_backup_payload(), indent=2, ensure_ascii=False) + '\n'
    directory = os.path.dirname(target)
    tmp = f'{target}.{os.getpid()}.tmp'
    try:
        os.makedirs(directory, exist_ok=True)
        with open(tmp, 'w', encoding='utf-8', newline='\n') as fh:
            fh.write(payload)
            fh.flush()
            os.fsync(fh.fileno())
        with suppress(OSError):
            os.chmod(tmp, 0o600)
        os.replace(tmp, target)
    except OSError:
        with suppress(OSError):
            os.remove(tmp)
        raise
    logger.info('Settings backup written to %s (%d bytes)', target, len(payload.encode('utf-8')))
    return target


def load_settings_backup(path: str) -> dict:
    source = os.path.abspath(path)
    try:
        size = os.path.getsize(source)
    except OSError as exc:
        raise ValueError(f'Cannot read backup: {exc}') from exc
    if size > _MAX_BACKUP_BYTES:
        raise ValueError('Backup is too large to be a settings file')
    try:
        with open(source, encoding='utf-8') as fh:
            data = json.load(fh)
    except (OSError, ValueError) as exc:
        raise ValueError(f'Invalid settings backup: {exc}') from exc
    if not isinstance(data, dict):
        raise ValueError('Invalid settings backup: expected a JSON object')
    raw = data.get('settings') if isinstance(data.get('settings'), dict) else data
    known = {f.name for f in fields(Settings)} - _BACKUP_LOCAL_ONLY
    base = settings.to_dict()
    recognised = 0
    for key, value in raw.items():
        if key not in known:
            continue
        recognised += 1
        if _coerce(value, getattr(settings, key)) is not _INVALID:
            base[key] = value
    if not recognised:
        raise ValueError('Invalid settings backup: no recognised settings found')
    snapshot = Settings.from_dict(base).to_dict()
    logger.info('Read settings backup from %s (%d recognised fields)', source, recognised)
    return snapshot


def apply_settings_backup(snapshot: dict) -> int:
    loaded = Settings.from_dict(snapshot)
    changed = 0
    for item in fields(Settings):
        if item.name in _BACKUP_LOCAL_ONLY:
            continue
        value = getattr(loaded, item.name)
        if getattr(settings, item.name) != value:
            setattr(settings, item.name, value)
            changed += 1
    if settings.auto_install_games and not settings.experimental_acknowledged:
        settings.auto_install_games = False
        changed += 1
        logger.warning('Automatic game installation stays disabled until experimental features are acknowledged')
    for attr in DOWNLOAD_DIR_SUBFOLDERS:
        if not getattr(settings, attr):
            setattr(settings, attr, default_download_dir(attr))
    if not settings.download_dir:
        settings.download_dir = paths.download_root
    if changed:
        save_settings(settings)
    logger.info('Applied %d setting(s) from backup', changed)
    return changed


settings = load_settings()

TAB_VISIBILITY_SCOPES = {
    'repacks': ('fitgirl', 'gog'),
    'tv': ('movies', 'series', 'anime', 'channels'),
    'books': ('books', 'manga'),
}
TAB_VISIBILITY_MIN_ONE_SCOPES = {'repacks'}

def _tab_visibility_attr(scope: str, key: str) -> str:
    return f'tabvis_{scope}_{key}_enabled'

class _TabVisibilityBus(QObject):
    changed = Signal(str)

tab_visibility_bus = _TabVisibilityBus()

def is_tab_enabled(scope: str, key: str) -> bool:
    return bool(getattr(settings, _tab_visibility_attr(scope, key), True))

def enabled_tab_keys(scope: str) -> list:
    return [k for k in TAB_VISIBILITY_SCOPES.get(scope, ()) if is_tab_enabled(scope, k)]

def set_tab_enabled(scope: str, key: str, value: bool) -> bool:
    attr = _tab_visibility_attr(scope, key)
    if not hasattr(settings, attr):
        return False
    value = bool(value)
    if not value and scope in TAB_VISIBILITY_MIN_ONE_SCOPES:
        others = [k for k in TAB_VISIBILITY_SCOPES.get(scope, ()) if k != key]
        if not any(is_tab_enabled(scope, o) for o in others):
            return False
    if bool(getattr(settings, attr)) == value:
        return True
    setattr(settings, attr, value)
    save_settings(settings)
    tab_visibility_bus.changed.emit(scope)
    return True

def apply_settings(**kwargs) -> None:
    for key, value in kwargs.items():
        if key not in Settings.__dataclass_fields__:
            logger.warning('apply_settings: unknown setting %r ignored', key)
            continue
        if getattr(settings, key) != value:
            setattr(settings, key, value)
            logger.info('Settings.%s = %s', key, value)

def _system_is_dark() -> bool:
    try:
        from PySide6.QtWidgets import QApplication
        from PySide6.QtGui import QPalette
        app = QApplication.instance()
        if app is None:
            return True
        base_color = app.palette().color(QPalette.ColorRole.Base)

        luminance = (0.299 * base_color.red() + 0.587 * base_color.green() + 0.114 * base_color.blue())
        return luminance < 128
    except Exception:
        logger.warning('Qt palette detection failed; defaulting to dark theme for AUTO mode')
        return True


def resolve_theme(qfluent_set_theme) -> None:
    from qfluentwidgets import Theme as FWTheme
    mode = settings.theme_mode
    if mode == ThemeMode.AUTO:
        theme = FWTheme.DARK if _system_is_dark() else FWTheme.LIGHT
    elif mode == ThemeMode.LIGHT:
        theme = FWTheme.LIGHT
    else:
        theme = FWTheme.DARK
    qfluent_set_theme(theme)
    logger.info('Theme resolved to %s (mode=%s)', theme, mode)

_APP_NAME = 'PiraChest'
_PROG_ID_TORRENT = 'PiraChest.Torrent'
_PROG_ID_MAGNET = 'PiraChest.MagnetURL'
_CAPABILITIES_KEY = r'Software\PiraChest\Capabilities'
_REGISTERED_APPS_KEY = r'Software\RegisteredApplications'

def _current_exe_path() -> str:
    if getattr(sys, 'frozen', False):
        return os.path.abspath(sys.executable)
    return os.path.abspath(sys.argv[0])

def _assoc_reg_set(key, name, value, vtype) -> None:
    import winreg
    winreg.SetValueEx(key, name, 0, vtype, value)

def _assoc_reg_create(path):
    import winreg
    return winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, path, 0, winreg.KEY_SET_VALUE | winreg.KEY_READ)

def _assoc_notify_shell() -> None:
    try:
        import ctypes
        ctypes.windll.shell32.SHChangeNotify(0x08000000, 0x0000, None, None)
    except Exception:
        pass

def associations_available() -> bool:
    return sys.platform == 'win32'

def associations_need_repair() -> bool:
    if not associations_available() or not settings.associations_enabled:
        return False
    if not settings.associations_exe_path:
        return True
    return os.path.normcase(os.path.abspath(settings.associations_exe_path)) != os.path.normcase(_current_exe_path())

def register_associations(exe_path: str | None = None) -> bool:
    if not associations_available():
        return False
    import winreg
    exe_path = os.path.abspath(exe_path or _current_exe_path())
    quoted = f'"{exe_path}"'
    app_key_path = rf'Software\Classes\Applications\{os.path.basename(exe_path)}'
    try:
        with _assoc_reg_create(rf'Software\Classes\{_PROG_ID_TORRENT}') as k:
            _assoc_reg_set(k, '', 'PiraChest Torrent File', winreg.REG_SZ)
            _assoc_reg_set(k, 'FriendlyTypeName', _APP_NAME, winreg.REG_SZ)
        with _assoc_reg_create(rf'Software\Classes\{_PROG_ID_TORRENT}\DefaultIcon') as k:
            _assoc_reg_set(k, '', f'{quoted},0', winreg.REG_SZ)
        with _assoc_reg_create(rf'Software\Classes\{_PROG_ID_TORRENT}\shell\open\command') as k:
            _assoc_reg_set(k, '', f'{quoted} "%1"', winreg.REG_SZ)

        with _assoc_reg_create(rf'Software\Classes\{_PROG_ID_MAGNET}') as k:
            _assoc_reg_set(k, '', 'PiraChest Magnet Link', winreg.REG_SZ)
            _assoc_reg_set(k, 'URL Protocol', '', winreg.REG_SZ)
        with _assoc_reg_create(rf'Software\Classes\{_PROG_ID_MAGNET}\DefaultIcon') as k:
            _assoc_reg_set(k, '', f'{quoted},0', winreg.REG_SZ)
        with _assoc_reg_create(rf'Software\Classes\{_PROG_ID_MAGNET}\shell\open\command') as k:
            _assoc_reg_set(k, '', f'{quoted} "%1"', winreg.REG_SZ)

        with _assoc_reg_create(rf'{app_key_path}\shell\open\command') as k:
            _assoc_reg_set(k, '', f'{quoted} "%1"', winreg.REG_SZ)
        with _assoc_reg_create(rf'{app_key_path}\shell\open') as k:
            _assoc_reg_set(k, '', 'open', winreg.REG_SZ)
        with _assoc_reg_create(app_key_path) as k:
            _assoc_reg_set(k, 'FriendlyAppName', _APP_NAME, winreg.REG_SZ)

        with _assoc_reg_create(_CAPABILITIES_KEY) as k:
            _assoc_reg_set(k, 'ApplicationName', _APP_NAME, winreg.REG_SZ)
            _assoc_reg_set(k, 'ApplicationDescription', f'{_APP_NAME} torrent client', winreg.REG_SZ)
        with _assoc_reg_create(rf'{_CAPABILITIES_KEY}\FileAssociations') as k:
            _assoc_reg_set(k, '.torrent', _PROG_ID_TORRENT, winreg.REG_SZ)
        with _assoc_reg_create(rf'{_CAPABILITIES_KEY}\URLAssociations') as k:
            _assoc_reg_set(k, 'magnet', _PROG_ID_MAGNET, winreg.REG_SZ)
        with _assoc_reg_create(_REGISTERED_APPS_KEY) as k:
            _assoc_reg_set(k, _APP_NAME, _CAPABILITIES_KEY, winreg.REG_SZ)

        with _assoc_reg_create(r'Software\Classes\.torrent\OpenWithProgids') as k:
            _assoc_reg_set(k, _PROG_ID_TORRENT, b'', winreg.REG_NONE)
        with _assoc_reg_create(r'Software\Classes\.torrent') as k:
            try:
                current, _ = winreg.QueryValueEx(k, '')
            except FileNotFoundError:
                current = ''
            if not current or current == _PROG_ID_TORRENT:
                _assoc_reg_set(k, '', _PROG_ID_TORRENT, winreg.REG_SZ)

        _assoc_notify_shell()

        settings.associations_enabled = True
        settings.associations_user_disabled = False
        settings.associations_exe_path = exe_path
        save_settings(settings)
        return True
    except OSError:
        logger.warning('Failed to register file associations')
        return False

def should_auto_register_associations() -> bool:
    # Never claim torrent/magnet associations without an explicit opt-in.
    return associations_available() and bool(settings.associations_enabled)


def ensure_associations_repaired() -> bool:
    if associations_need_repair():
        return register_associations()
    return False

def extract_cli_targets(argv: list[str]) -> list[str]:
    targets = []
    for arg in argv:
        if not arg:
            continue
        if arg.lower().startswith('magnet:'):
            targets.append(arg)
        elif arg.lower().endswith('.torrent') and os.path.isfile(arg):
            targets.append(os.path.abspath(arg))
    return targets

_SINGLE_INSTANCE_SERVER_NAME = 'PiraChest-SingleInstance'
_SINGLE_INSTANCE_SEP = '\x1e'

class SingleInstance:
    def __init__(self):
        self._server = None
        self._on_args = None

    def try_acquire(self, args: list[str], on_args_received) -> bool:
        from PySide6.QtNetwork import QLocalServer, QLocalSocket
        self._on_args = on_args_received
        socket = QLocalSocket()
        deadline = time.monotonic() + (20.0 if os.environ.pop(_RESTART_ENV, '') else 0.0)
        while True:
            socket.connectToServer(_SINGLE_INSTANCE_SERVER_NAME)
            if not socket.waitForConnected(1500) or time.monotonic() >= deadline:
                break
            socket.abort()
            time.sleep(0.3)
        if socket.state() == QLocalSocket.LocalSocketState.ConnectedState:
            payload = _SINGLE_INSTANCE_SEP.join(args).encode('utf-8', errors='ignore')
            socket.write(payload)
            socket.flush()
            socket.waitForBytesWritten(500)
            socket.disconnectFromServer()
            return False
        QLocalServer.removeServer(_SINGLE_INSTANCE_SERVER_NAME)
        self._server = QLocalServer()
        self._server.newConnection.connect(self._on_new_connection)
        self._server.listen(_SINGLE_INSTANCE_SERVER_NAME)
        return True

    def _on_new_connection(self) -> None:
        conn = self._server.nextPendingConnection()
        if conn is None:
            return
        conn.readyRead.connect(lambda c=conn: self._read(c))
        conn.disconnected.connect(conn.deleteLater)

    def _read(self, conn) -> None:
        data = bytes(conn.readAll()).decode('utf-8', errors='ignore')
        if not data:
            return
        args = [a for a in data.split(_SINGLE_INSTANCE_SEP) if a]
        if args and self._on_args is not None:
            self._on_args(args)
