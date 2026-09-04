from __future__ import annotations
import logging
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
from ..cache import cache
from ..config import paths
from ..models import MusicItem as Song
from .settings import settings
logger = logging.getLogger(__name__)

class _MusicdlNoiseFilter(logging.Filter):

    def filter(self, record: logging.LogRecord) -> bool:
        if record.name.startswith('musicdl') and 'quark_parser_config' in record.getMessage():
            record.levelno = logging.DEBUG
            record.levelname = 'DEBUG'
            return logging.getLogger().isEnabledFor(logging.DEBUG)
        return True

logging.getLogger('musicdl').addFilter(_MusicdlNoiseFilter())
_client_lock = threading.Lock()
_client = None
_client_sources_key = None
MUSICDL_WORK_DIR = os.path.join(paths.cache_dir, 'musicdl')

def _sources_key(sources: list[str]) -> str:
    return '|'.join(sorted(sources))

def _get_client(sources: list[str]):
    global _client, _client_sources_key
    size = settings.search_size_per_source
    key = f'{_sources_key(sources)}#{size}'
    with _client_lock:
        if _client is None or _client_sources_key != key:
            from musicdl.musicdl import MusicClient
            os.makedirs(settings.download_dir, exist_ok=True)
            os.makedirs(MUSICDL_WORK_DIR, exist_ok=True)
            cfg = {source: {'search_size_per_source': size, 'work_dir': MUSICDL_WORK_DIR} for source in sources}
            _client = MusicClient(music_sources=sources, init_music_clients_cfg=cfg)
            _client_sources_key = key
        return _client

def _filter_by_quality(songs: list[Song]) -> list[Song]:
    allowed = set(settings.quality_filters)
    if not allowed or allowed >= set(('lossless', 'mp3', 'aac', 'other')):
        return songs
    return [s for s in songs if getattr(s, 'quality_tier', None) in allowed]

def search(query: str, sources: list[str] | None=None, use_cache: bool=True) -> list[Song]:
    sources = sources or settings.preferred_sources
    key = _sources_key(sources)
    if use_cache:
        cached = cache.load('music_search', f'{query}:{key}')
        if cached:
            return _filter_by_quality([Song.from_dict(d) for d in cached])
    client = _get_client(sources)
    raw_results = client.search(query)
    songs: list[Song] = []
    for source, song_infos in raw_results.items():
        for song_info in song_infos:
            songs.append(Song.from_song_info(source, song_info))
    if songs:
        cache.save('music_search', f'{query}:{key}', [s.to_dict() for s in songs])
    return _filter_by_quality(songs)

def search_streaming(query: str, on_source_done, sources: list[str] | None=None, use_cache: bool=True, is_cancelled=None, per_source_timeout: float | None=None) -> list[Song]:
    sources = sources or settings.preferred_sources
    key = _sources_key(sources)
    if use_cache:
        cached = cache.load('music_search', f'{query}:{key}')
        if cached:
            songs = _filter_by_quality([Song.from_dict(d) for d in cached])
            on_source_done(songs)
            return songs
    client = _get_client(sources)
    all_songs: list[Song] = []
    ex = ThreadPoolExecutor(max_workers=min(len(sources), 10))
    try:
        futures = {}
        for source in sources:
            music_client = client.music_clients.get(source)
            if music_client is None:
                continue
            futures[ex.submit(music_client.search, keyword=query, main_process_context=None, main_progress_lock=None)] = source
        pending = set(futures.keys())
        deadline = time.monotonic() + per_source_timeout if per_source_timeout else None
        while pending:
            if is_cancelled is not None and is_cancelled():
                ex.shutdown(wait=False, cancel_futures=True)
                return _filter_by_quality(all_songs)
            if deadline is not None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    for fut in pending:
                        source = futures[fut]
                        logger.warning('Search timed out for source %s after %.1fs — abandoning', source, per_source_timeout)
                    break
            else:
                remaining = None
            done, pending = wait(pending, timeout=remaining, return_when=FIRST_COMPLETED)
            for future in done:
                source = futures[future]
                try:
                    song_infos = future.result()
                except Exception as exc:
                    logger.warning('Search failed for source %s: %s', source, exc)
                    continue
                batch = [Song.from_song_info(source, si) for si in song_infos or []]
                if not batch:
                    continue
                all_songs.extend(batch)
                filtered_batch = _filter_by_quality(batch)
                if filtered_batch:
                    on_source_done(filtered_batch)
    finally:
        ex.shutdown(wait=False)
    if all_songs:
        cache.save('music_search', f'{query}:{key}', [s.to_dict() for s in all_songs])
    return _filter_by_quality(all_songs)

def download(songs: list[Song], sources: list[str] | None=None) -> list[tuple[Song, bool, str]]:
    sources = sources or settings.preferred_sources
    client = _get_client(sources)
    from musicdl.modules.utils import SongInfo
    song_infos = []
    for song in songs:
        song_info = SongInfo.fromdict(song.raw) if song.raw else SongInfo()
        song_info.source = song.source
        song_info.song_name = song.song_name
        song_info.singers = song.singers
        song_info.identifier = song.identifier
        song_info.album = song.album
        song_info.ext = song.ext
        song_info.cover_url = song.cover_url
        song_info.download_url = song.download_url
        song_info.work_dir = settings.download_dir
        song_infos.append(song_info)
    results = client.download({'_': song_infos} if isinstance(song_infos, list) else song_infos)
    output = []
    for original, result in zip(songs, results):
        ok = bool(getattr(result, 'downloaded_contents', None) or os.path.isfile(getattr(result, 'save_path', '') or ''))
        path = getattr(result, 'save_path', None) if ok else None
        output.append((original, ok, path or ''))
    return output

def parse_playlist(playlist_url: str, sources: list[str] | None=None) -> list[Song]:
    sources = sources or settings.preferred_sources
    client = _get_client(sources)
    song_infos = client.parseplaylist(playlist_url)
    return [Song.from_song_info(getattr(si, 'source', '') or 'playlist', si) for si in song_infos]

def available_sources() -> list[str]:
    from musicdl.modules import MusicClientBuilder
    return sorted(MusicClientBuilder.REGISTERED_MODULES.keys())

FALLBACK_COVER_SOURCES = ('NeteaseMusicClient', 'DeezerMusicClient', 'QQMusicClient', 'KuwoMusicClient')

def fetch_fallback_cover_url(song: Song) -> str | None:
    if getattr(song, 'cover_url', None):
        return song.cover_url
    query = f"{song.song_name or ''} {(song.singers or '').split(',')[0]}".strip()
    if not query:
        return None
    try:
        available = set(available_sources())
    except Exception:
        available = set()
    for source in FALLBACK_COVER_SOURCES:
        if source == getattr(song, 'source', None) or (available and source not in available):
            continue
        try:
            for result in search(query, sources=[source], use_cache=True):
                if result.cover_url:
                    return result.cover_url
        except Exception:
            logger.warning('Fallback cover lookup failed via %s', source)
    return None