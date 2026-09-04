from __future__ import annotations
import json
import logging
import os
from dataclasses import dataclass, field
from ..config import paths
logger = logging.getLogger(__name__)
_MUSIC_CONFIG_DIR = os.path.join(paths.config_dir, 'music')
_SETTINGS_FILE = os.path.join(_MUSIC_CONFIG_DIR, 'music_settings.json')
DEFAULT_SOURCES = [ 
    "BodianMusicClient",
    "DeezerMusicClient",
    "FMAMusicClient",
    "FangpiMusicClient",
    "GequhaiMusicClient",
    "HTQYYMusicClient",
    "ITingWaMusicClient",
    "JBSouMusicClient",
    "JooxMusicClient",
    "KugouMusicClient",
    "KuwoMusicClient",
    "LRTSMusicClient",
    "LivePOOMusicClient",
    "MGMP3MusicClient",
    "MOOVMusicClient",
    "MiguMusicClient",
    "NeteaseMusicClient",
    "QQMusicClient",
    "QianqianMusicClient",
    "QobuzMusicClient",
    "SgogoMusicClient",
    "SodaMusicClient",
    "TuneHubMusicClient",
    "MituMusicClient"
    ]
QUALITY_TIERS = ['lossless', 'mp3', 'aac', 'other']
SLOW_OR_GATED_SOURCES = ['AppleMusicClient', 'SoundCloudMusicClient', 'StreetVoiceMusicClient', 'TIDALMusicClient', 'YouTubeMusicClient', 'SpotifyMusicClient', 'MituMusicClient', 'GequbaoMusicClient', 'YinyuedaoMusicClient', 'BuguyyMusicClient']

@dataclass
class MusicSettings:
    download_dir: str = field(default_factory=lambda: os.path.join(paths.download_root, 'music'))
    preferred_sources: list[str] = field(default_factory=lambda: list(DEFAULT_SOURCES))
    quality_filters: list[str] = field(default_factory=lambda: list(QUALITY_TIERS))
    search_size_per_source: int = 8
    preferred_quality: str = 'auto'
    auto_download_lyrics: bool = True
    max_concurrent_downloads: int = 3
    max_concurrent_searches: int = 5

    def to_dict(self) -> dict:
        return {k: v for k, v in self.__dict__.items()}

    @classmethod
    def from_dict(cls, data: dict) -> 'MusicSettings':
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})

def load_settings() -> MusicSettings:
    if os.path.isfile(_SETTINGS_FILE):
        try:
            with open(_SETTINGS_FILE, 'r', encoding='utf-8') as fh:
                data = json.load(fh)
            return MusicSettings.from_dict(data)
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning('Failed to load music settings: %s — using defaults', exc)
    return MusicSettings()

def save_settings(s: MusicSettings) -> None:
    os.makedirs(_MUSIC_CONFIG_DIR, exist_ok=True)
    try:
        with open(_SETTINGS_FILE, 'w', encoding='utf-8') as fh:
            json.dump(s.to_dict(), fh, indent=2)
    except OSError as exc:
        logger.error('Failed to save music settings: %s', exc)
settings = load_settings()

def apply_settings(**kwargs) -> None:
    for key, value in kwargs.items():
        if hasattr(settings, key):
            setattr(settings, key, value)
    save_settings(settings)