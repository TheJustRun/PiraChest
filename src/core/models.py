from __future__ import annotations
import hashlib
import os
import re
from dataclasses import dataclass, field
from typing import Any, Optional

_LOSSLESS_EXTS = {'flac', 'wav', 'alac', 'ape', 'wv', 'tta', 'dsf', 'dff'}

@dataclass(slots=True)
class MediaItem:
    provider: str
    id: str
    title: str
    artwork_url: Optional[str] = None
    artwork_path: Optional[str] = None
    metadata: dict = field(default_factory=dict)

    @property
    def key(self) -> str:
        return f'{self.provider}:{self.id}'

    def to_dict(self) -> dict:
        return {'provider': self.provider, 'id': self.id, 'title': self.title, 'artwork_url': self.artwork_url, 'artwork_path': self.artwork_path, 'metadata': dict(self.metadata)}

    @classmethod
    def from_dict(cls, data: dict) -> 'MediaItem':
        return cls(**_media_item_kwargs(data))

def _media_item_kwargs(data: dict) -> dict:
    return dict(provider=data.get('provider', ''), id=str(data.get('id', '')), title=data.get('title', 'Unknown'), artwork_url=data.get('artwork_url'), artwork_path=data.get('artwork_path'), metadata=dict(data.get('metadata') or {}))

@dataclass(slots=True)
class GameItem(MediaItem):
    platform: str = ''
    developer: str = ''
    genre: str = ''

    def to_dict(self) -> dict:
        d = super(GameItem, self).to_dict()
        d.update(platform=self.platform, developer=self.developer, genre=self.genre)
        return d

    @classmethod
    def from_dict(cls, data: dict) -> 'GameItem':
        return cls(**_media_item_kwargs(data), platform=data.get('platform', ''), developer=data.get('developer', ''), genre=data.get('genre', ''))

@dataclass(slots=True)
class BookItem(MediaItem):
    author: str = ''
    isbn: str = ''
    pages: int = 0
    description: str = ''
    formats: dict = field(default_factory=dict)
    subjects: list = field(default_factory=list)
    language: str = ''
    year: str = ''

    def to_dict(self) -> dict:
        d = super(BookItem, self).to_dict()
        d.update(author=self.author, isbn=self.isbn, pages=self.pages, description=self.description, formats=dict(self.formats), subjects=list(self.subjects), language=self.language, year=self.year)
        return d

    @classmethod
    def from_dict(cls, data: dict) -> 'BookItem':
        return cls(**_media_item_kwargs(data), author=data.get('author', ''), isbn=data.get('isbn', ''), pages=data.get('pages', 0), description=data.get('description', ''), formats=dict(data.get('formats') or {}), subjects=list(data.get('subjects') or []), language=data.get('language', ''), year=data.get('year', ''))

@dataclass(slots=True)
class MusicItem(MediaItem):
    artist: str = ''
    album: str = ''
    duration_s: Optional[int] = None
    ext: Optional[str] = None
    bitrate: Optional[int] = None
    codec: Optional[str] = None
    file_size_bytes: Optional[int] = None
    download_url: Optional[Any] = None
    source: str = ''
    file_size: Optional[str] = None
    duration: Optional[str] = None
    lyric: Optional[str] = None
    raw: dict = field(default_factory=dict)
    download_headers: dict = field(default_factory=dict)
    download_cookies: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        d = super(MusicItem, self).to_dict()
        d.update(artist=self.artist, album=self.album, duration_s=self.duration_s, ext=self.ext, bitrate=self.bitrate, codec=self.codec, file_size_bytes=self.file_size_bytes, download_url=self.download_url if isinstance(self.download_url, str) else None, source=self.source, file_size=self.file_size, duration=self.duration, lyric=self.lyric, raw=self.raw if isinstance(self.raw, dict) else {}, download_headers=self.download_headers if isinstance(self.download_headers, dict) else {}, download_cookies=self.download_cookies if isinstance(self.download_cookies, dict) else {})
        return d

    @classmethod
    def from_dict(cls, data: dict) -> 'MusicItem':
        return cls(**_media_item_kwargs(data), artist=data.get('artist', ''), album=data.get('album', ''), duration_s=data.get('duration_s'), ext=data.get('ext'), bitrate=data.get('bitrate'), codec=data.get('codec'), file_size_bytes=data.get('file_size_bytes'), download_url=data.get('download_url'), source=data.get('source', ''), file_size=data.get('file_size'), duration=data.get('duration'), lyric=data.get('lyric'), raw=data.get('raw') or {}, download_headers=data.get('download_headers') or {}, download_cookies=data.get('download_cookies') or {})

    @classmethod
    def from_song_info(cls, source: str, song_info: Any) -> 'MusicItem':
        identifier = str(getattr(song_info, 'identifier', '') or '')
        if not identifier:
            song_name = getattr(song_info, 'song_name', '') or ''
            singers = getattr(song_info, 'singers', '') or ''
            download_url = getattr(song_info, 'download_url', None)

            seed = f'{song_name}\x00{singers}\x00{download_url or ""}'.encode('utf-8', 'ignore')
            identifier = hashlib.blake2b(seed, digest_size=8).hexdigest()
        return cls(provider=source, id=identifier, title=getattr(song_info, 'song_name', '') or '', artist=getattr(song_info, 'singers', '') or '', source=source, album=getattr(song_info, 'album', None) or '', ext=getattr(song_info, 'ext', None), file_size=getattr(song_info, 'file_size', None), file_size_bytes=getattr(song_info, 'file_size_bytes', None), duration=getattr(song_info, 'duration', None), duration_s=getattr(song_info, 'duration_s', None), bitrate=getattr(song_info, 'bitrate', None), codec=getattr(song_info, 'codec', None), artwork_url=getattr(song_info, 'cover_url', None), lyric=getattr(song_info, 'lyric', None), download_url=getattr(song_info, 'download_url', None), raw=getattr(song_info, 'raw_data', {}) or {}, download_headers=getattr(song_info, 'default_download_headers', None) or {}, download_cookies=getattr(song_info, 'default_download_cookies', None) or {})

    @property
    def is_lossless(self) -> bool:
        codec = (self.codec or '').lower()
        if codec:
            return codec in _LOSSLESS_EXTS or codec in {'pcm_s16le', 'pcm_s24le', 'pcm_s32le', 'alac', 'wavpack'}
        return (self.ext or '').lower().lstrip('.') in _LOSSLESS_EXTS

    @property
    def quality_label(self) -> str:
        parts = []
        if self.is_lossless:
            parts.append('Lossless')
        elif self.bitrate:
            parts.append(f'{self.bitrate}kbps')
        elif self.ext:
            parts.append(self.ext.upper().lstrip('.'))
        if self.codec and self.codec.lower() not in (self.ext or '').lower() and (not self.is_lossless):
            parts.append(self.codec.upper())
        return ' · '.join(parts)

    @property
    def quality_tier(self) -> str:
        if self.is_lossless:
            return 'lossless'
        ext = (self.ext or '').lower().lstrip('.')
        if ext == 'mp3':
            return 'mp3'
        if ext in ('aac', 'm4a'):
            return 'aac'
        return 'other'

    @property
    def song_name(self) -> str:
        return self.title

    @song_name.setter
    def song_name(self, value: str) -> None:
        self.title = value

    @property
    def singers(self) -> str:
        return self.artist

    @singers.setter
    def singers(self, value: str) -> None:
        self.artist = value

    @property
    def identifier(self) -> str:
        return self.id

    @identifier.setter
    def identifier(self, value: str) -> None:
        self.id = value

    @property
    def cover_url(self) -> Optional[str]:
        return self.artwork_url

    @cover_url.setter
    def cover_url(self, value: Optional[str]) -> None:
        self.artwork_url = value

    @property
    def cover_path(self) -> Optional[str]:
        return self.artwork_path

    @cover_path.setter
    def cover_path(self, value: Optional[str]) -> None:
        self.artwork_path = value

@dataclass(slots=True)
class AnimeItem(MediaItem):
    id_mal: Optional[int] = None
    title_english: str = ''
    title_romaji: str = ''
    title_native: str = ''
    status: str = ''
    format: str = ''
    season_year: Optional[int] = None
    episodes: Optional[int] = None
    genres: list = field(default_factory=list)
    description: str = ''
    synonyms: list = field(default_factory=list)

    def to_dict(self) -> dict:
        d = super(AnimeItem, self).to_dict()
        d.update(id_mal=self.id_mal, title_english=self.title_english, title_romaji=self.title_romaji, title_native=self.title_native, status=self.status, format=self.format, season_year=self.season_year, episodes=self.episodes, genres=list(self.genres), description=self.description, synonyms=list(self.synonyms))
        return d

    @classmethod
    def from_dict(cls, data: dict) -> 'AnimeItem':
        return cls(**_media_item_kwargs(data), id_mal=data.get('id_mal'), title_english=data.get('title_english', ''), title_romaji=data.get('title_romaji', ''), title_native=data.get('title_native', ''), status=data.get('status', ''), format=data.get('format', ''), season_year=data.get('season_year'), episodes=data.get('episodes'), genres=list(data.get('genres') or []), description=data.get('description', ''), synonyms=list(data.get('synonyms') or []))

    @classmethod
    def from_anilist_media(cls, media: dict) -> 'AnimeItem':
        title = media.get('title') or {}
        display_title = title.get('english') or title.get('romaji') or title.get('native') or 'Unknown'
        cover = media.get('coverImage')
        if isinstance(cover, dict):
            cover_url = cover.get('large') or cover.get('medium') or cover.get('extraLarge')
        elif isinstance(cover, str):
            cover_url = cover
        else:
            cover_url = None
        return cls(provider='anilist', id=str(media.get('id', '')), title=display_title, artwork_url=cover_url, id_mal=media.get('idMal'), title_english=title.get('english') or '', title_romaji=title.get('romaji') or '', title_native=title.get('native') or '', status=media.get('status') or '', format=media.get('format') or '', season_year=media.get('seasonYear'), episodes=media.get('episodes'), genres=list(media.get('genres') or []) if isinstance(media.get('genres'), list) else [], description=media.get('description') or '', synonyms=list(media.get('synonyms') or []) if isinstance(media.get('synonyms'), list) else [])

    @property
    def display_title(self) -> str:
        return self.title_english or self.title_romaji or self.title_native or self.title

@dataclass(slots=True)
class RepackItem(MediaItem):
    size: str = ''
    files: list = field(default_factory=list)
    url: str = ''

    def to_dict(self) -> dict:
        d = super(RepackItem, self).to_dict()
        d.update(size=self.size, files=list(self.files), url=self.url)
        return d

    @classmethod
    def from_dict(cls, data: dict) -> 'RepackItem':
        return cls(**_media_item_kwargs(data), size=data.get('size', ''), files=list(data.get('files') or []), url=data.get('url', ''))

@dataclass(slots=True)
class MediaPage:
    entries: list
    page: int
    has_more: bool


# --- File-type / source safety ------------------------------------------------
# Download sources are checked locally by extension. This is deliberately
# conservative: it never blocks a download, it only highlights file types that
# deserve a second look. Game repacks routinely contain installers, so the
# assessment depends on the surrounding category.

_FILE_KINDS: dict[str, tuple[str, ...]] = {
    'video': ('.mkv', '.mp4', '.avi', '.m4v', '.mov', '.webm', '.wmv', '.flv', '.mpg',
              '.mpeg', '.ts', '.m2ts', '.vob', '.3gp', '.ogv'),
    'audio': ('.mp3', '.flac', '.m4a', '.aac', '.ogg', '.opus', '.wav', '.wma', '.alac',
              '.ape', '.wv', '.tta', '.dsf', '.dff', '.m4b'),
    'subtitle': ('.srt', '.ass', '.ssa', '.vtt', '.sub', '.idx', '.sup'),
    'archive': ('.zip', '.rar', '.7z', '.tar', '.gz', '.bz2', '.xz', '.zst', '.tgz',
                '.tbz2', '.lz', '.lzma'),
    'disc': ('.iso', '.img', '.mdf', '.mds', '.nrg', '.cue', '.bin', '.cso', '.chd',
             '.gcm', '.wbfs', '.xci', '.nsp'),
    'rom': ('.nes', '.sfc', '.smc', '.gba', '.gbc', '.gb', '.nds', '.n64', '.z64', '.v64',
            '.gen', '.md', '.sms', '.gg', '.pce', '.a26', '.a78', '.ws', '.wsc', '.3ds',
            '.cia', '.rvz'),
    'installer': ('.exe', '.msi', '.msix', '.appx', '.dmg', '.pkg', '.apk', '.jar'),
    'document': ('.pdf', '.txt', '.rtf', '.doc', '.docx', '.xls', '.xlsx', '.ppt', '.pptx',
                 '.odt', '.ods', '.odp', '.nfo', '.md', '.csv', '.json', '.xml'),
    'ebook': ('.epub', '.mobi', '.azw', '.azw3', '.fb2', '.cbz', '.cbr', '.cb7', '.djvu'),
    'image': ('.jpg', '.jpeg', '.png', '.gif', '.bmp', '.webp', '.tif', '.tiff', '.svg',
              '.ico', '.heic'),
    'stream': ('.m3u8', '.mpd'),
}

_DANGEROUS_EXTENSIONS = frozenset({
    '.bat', '.cmd', '.com', '.cpl', '.hta', '.js', '.jse', '.lnk', '.msc', '.pif',
    '.ps1', '.psm1', '.reg', '.scr', '.url', '.vbe', '.vbs', '.wsf', '.wsh',
})
_MACRO_EXTENSIONS = frozenset({'.docm', '.dotm', '.xlsm', '.xltm', '.xlam', '.pptm', '.potm', '.ppam'})
_DOUBLE_EXTENSION_RE = re.compile(
    r'\.(?:jpe?g|png|gif|webp|bmp|mp4|mkv|avi|mov|webm|mp3|flac|wav|m4a|pdf|epub|'
    r'docx?|xlsx?|pptx?|txt|nfo|zip|rar|7z|iso)\.(?:exe|scr|com|bat|cmd|ps1|vbs|'
    r'js|jse|wsf|hta|lnk|pif|msi)$',
    re.IGNORECASE,
)
_REASONS = {
    'video': 'Playable video file',
    'audio': 'Playable audio file',
    'subtitle': 'Subtitle file',
    'archive': 'Compressed archive; inspect before extracting',
    'disc': 'Disc or cartridge image',
    'rom': 'Game ROM image',
    'installer': 'Windows installer or executable program',
    'document': 'Document file',
    'ebook': 'E-book file',
    'image': 'Image file',
    'stream': 'Streaming playlist',
    'script': 'Script or shortcut that can run code when opened',
    'shortcut': 'Shortcut that can launch another program',
    'batch': 'Batch script that runs commands when opened',
    'macro': 'Document with macros; macros can run code',
    'other': 'No executable extension detected',
}
_EXPECTED_KINDS = {
    # Only the repack page treats .bat launchers as expected; .cmd is never
    # neutralised because it is far more often used to hide commands.
    'repack': frozenset({'installer', 'batch', 'archive', 'disc', 'rom', 'document', 'image', 'other'}),
    'game': frozenset({'installer', 'archive', 'disc', 'rom', 'document', 'image', 'other'}),
    'software': frozenset({'installer', 'archive', 'disc', 'document', 'image', 'other'}),
    'video': frozenset({'video', 'subtitle', 'stream', 'archive', 'image', 'document', 'other'}),
    'audio': frozenset({'audio', 'subtitle', 'archive', 'image', 'document', 'other'}),
    'ebook': frozenset({'ebook', 'document', 'archive', 'image', 'subtitle', 'other'}),
}


@dataclass(frozen=True, slots=True)
class FileSafety:
    kind: str
    label: str
    risk: str  # ok | caution | dangerous
    reason: str


@dataclass(frozen=True, slots=True)
class SourceSafety:
    level: str  # ok | caution | dangerous
    summary: str
    details: str
    highlighted: tuple[str, ...] = ()


def _extension(name: str) -> str:
    base = os.path.basename(str(name or '').replace('\\', '/')).lower()
    if '.' not in base:
        return ''
    return '.' + base.rsplit('.', 1)[-1]


def classify_file(name: str) -> FileSafety:
    """Classify one path by extension without trusting or opening it.

    ``label`` is the concrete extension shown in the UI (``mp4``, ``exe``),
    while ``kind`` is the broad category used for context-aware assessment.
    """
    base = os.path.basename(str(name or '').replace('\\', '/')).strip()
    ext = _extension(base)
    label = ext.lstrip('.') or 'no ext'
    if not base:
        return FileSafety('other', 'no ext', 'ok', _REASONS['other'])
    if _DOUBLE_EXTENSION_RE.search(base):
        return FileSafety('script', label, 'dangerous',
                          'A media or document name ends in an executable extension')
    if ext in _DANGEROUS_EXTENSIONS:
        if ext == '.bat':
            kind = 'batch'
        elif ext == '.cmd':
            kind = 'script'
        elif ext in {'.lnk', '.url'}:
            kind = 'shortcut'
        else:
            kind = 'script'
        return FileSafety(kind, label, 'dangerous', _REASONS[kind])
    if ext in _MACRO_EXTENSIONS:
        return FileSafety('macro', label, 'caution', _REASONS['macro'])
    if ext in _FILE_KINDS['installer']:
        return FileSafety('installer', label, 'caution', _REASONS['installer'])
    for kind, extensions in _FILE_KINDS.items():
        if ext in extensions:
            return FileSafety(kind, label, 'ok', _REASONS[kind])
    if not ext:
        return FileSafety('other', 'no ext', 'ok', _REASONS['other'])
    return FileSafety('other', label, 'ok', 'Unrecognised file type')


def _context_key(context: str) -> str:
    text = str(context or '').strip().lower()
    if 'repack' in text:
        return 'repack'
    if any(token in text for token in ('game', 'console', 'rom')):
        return 'game'
    if any(token in text for token in ('movie', 'tv', 'video', 'series', 'anime', 'stream')):
        return 'video'
    if any(token in text for token in ('music', 'audio', 'album', 'song')):
        return 'audio'
    if any(token in text for token in ('book', 'manga', 'ebook', 'comic')):
        return 'ebook'
    if any(token in text for token in ('app', 'software', 'program', 'tool')):
        return 'software'
    return 'other'


def assess_source_files(files, context: str = '', title: str = '') -> SourceSafety:
    """Summarise the file mix of a torrent without blocking anything.

    `files` may contain paths or dicts with a ``name`` key. The context is a
    source category such as ``Repacks``, ``Video`` or ``Books``. Installers are
    expected for games/software, and ``.bat`` launchers are expected only on
    the repack page; other scripts, including ``.cmd``, are always highlighted.
    """
    names: list[str] = []
    for entry in files or ():
        if isinstance(entry, dict):
            name = entry.get('name') or entry.get('path') or ''
        else:
            name = str(entry or '')
        base = os.path.basename(name.replace('\\', '/')).strip()
        if base:
            names.append(base)
    if not names:
        if title:
            info = classify_file(title)
            expected = _EXPECTED_KINDS.get(_context_key(context), _EXPECTED_KINDS['video'])
            if info.risk != 'ok' and not (info.kind == 'batch' and info.kind in expected):
                return SourceSafety(info.risk, f'{info.label} file', info.reason, (title,))
        return SourceSafety('ok', 'File types look expected', '', ())
    expected = _EXPECTED_KINDS.get(_context_key(context), _EXPECTED_KINDS['video'])
    dangerous: list[str] = []
    caution: list[str] = []
    for name in names:
        info = classify_file(name)
        if info.risk == 'dangerous':
            # Repacks commonly ship batch launchers; they are still shown as
            # their concrete extension but are not treated as a warning there.
            if info.kind == 'batch' and info.kind in expected:
                continue
            dangerous.append(name)
        elif info.risk == 'caution' and info.kind not in expected:
            caution.append(name)
        elif info.kind not in expected:
            caution.append(name)
    if dangerous:
        sample = ', '.join(dangerous[:3])
        details = 'Risky file types: ' + sample
        if len(dangerous) > 3:
            details += f' and {len(dangerous) - 3} more'
        return SourceSafety('dangerous', f'{len(dangerous)} risky file type(s)', details, tuple(dangerous))
    if caution:
        sample = ', '.join(caution[:3])
        details = 'Review these files: ' + sample
        if len(caution) > 3:
            details += f' and {len(caution) - 3} more'
        return SourceSafety('caution', 'Review file types', details, tuple(caution))
    return SourceSafety('ok', 'File types look expected', '', ())


def assess_title(title: str, context: str = '') -> SourceSafety | None:
    """Return a warning when a title itself advertises a risky file type."""
    info = classify_file(title)
    if info.risk == 'ok':
        return None
    expected = _EXPECTED_KINDS.get(_context_key(context), _EXPECTED_KINDS['video'])
    if info.kind == 'batch' and info.kind in expected:
        return None
    if info.risk == 'caution' and info.kind in expected:
        return None
    base = os.path.basename(str(title or '').replace('\\', '/')).strip()
    return SourceSafety(info.risk, f'{info.label} file', info.reason, (base,))
