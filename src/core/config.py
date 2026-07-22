from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

_PROJECT_ROOT = os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
)

@dataclass(frozen=True)
class Paths:
    project_root: str = _PROJECT_ROOT
    data_dir: str = field(default_factory=lambda: os.path.join(_PROJECT_ROOT, "src", "data"))
    db_path: str = field(default_factory=lambda: os.path.join(_PROJECT_ROOT, "src", "data", "minerva_index.db"))
    download_root: str = field(default_factory=lambda: os.path.join(_PROJECT_ROOT, "downloads"))
    torrent_cache: str = field(default_factory=lambda: os.path.join(_PROJECT_ROOT, "downloads", "torrents"))

@dataclass(frozen=True)
class Network:
    cdn_base: str = "https://cdn.minerva-archive.org/torrents"
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
    extra_trackers: tuple[str, ...] = (
        "udp://tracker.opentrackr.org:1337/announce",
        "udp://open.stealth.si:80/announce",
        "udp://tracker.torrent.eu.org:451/announce",
        "udp://exodus.desync.com:6969/announce",
        "udp://tracker.openbittorrent.com:6969/announce",
    )

class ThemeMode:
    DARK = "Dark"
    LIGHT = "Light"
    AUTO = "Auto"

@dataclass
class Settings:
    download_dir: str = field(default_factory=lambda: Paths().download_root)
    speed_limit: int = 0
    upload_speed_limit: int = 500
    seed_time: int = 0
    auto_download: bool = False
    delete_torrent_after: bool = True
    theme_mode: str = ThemeMode.DARK
    hide_alpha_disclaimer: bool = False
    pc_games_enabled: bool = False
    local_dat_enabled: bool = False

    def to_dict(self) -> dict:
        return {k: v for k, v in self.__dict__.items()}

    @classmethod
    def from_dict(cls, data: dict) -> Settings:
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})

paths = Paths()
network = Network()
libtorrent_defaults = LibtorrentDefaults()
settings = Settings()

def apply_settings(**kwargs) -> None:
    for key, value in kwargs.items():
        if hasattr(settings, key):
            setattr(settings, key, value)
            logger.info("Settings.%s = %s", key, value)

def resolve_theme(qfluent_set_theme) -> None:
    from qfluentwidgets import Theme as FWTheme

    mode = settings.theme_mode
    if mode == ThemeMode.AUTO:
        try:
            import darketect
            theme = FWTheme.DARK if darketect.is_dark() else FWTheme.LIGHT
        except ImportError:
            logger.warning("darketect not available; defaulting to dark theme for AUTO mode")
            theme = FWTheme.DARK
    elif mode == ThemeMode.LIGHT:
        theme = FWTheme.LIGHT
    else:
        theme = FWTheme.DARK
    qfluent_set_theme(theme)
    logger.info("Theme resolved to %s (mode=%s)", theme, mode)