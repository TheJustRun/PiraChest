from __future__ import annotations

import logging
import os
import re
import tempfile
import time
import zipfile
from dataclasses import dataclass, field
from typing import Any, Optional

import requests
from PySide6.QtCore import QObject, QRunnable, QThreadPool

from src.core.cache import cache

logger = logging.getLogger(__name__)

BASE_URL = "https://api.mangadex.org"
COVER_BASE_URL = "https://uploads.mangadex.org/covers"
MANGADEX_WEB_URL = "https://mangadex.org"
PAGE_SIZE = 24
REQUEST_TIMEOUT = 15

_NS = 'manga'
_TTL = 900

_USER_AGENT = "PiraChest/1.0 (+https://api.mangadex.org/docs/)"

@dataclass
class MangaChapter:
    id: str
    chapter: str = ''
    volume: str = ''
    title: str = ''
    language: str = ''
    pages: int = 0
    scanlation_group: str = ''
    external_url: str = ''

    @property
    def label(self) -> str:
        parts = []
        if self.volume:
            parts.append(f'Vol. {self.volume}')
        if self.chapter:
            parts.append(f'Ch. {self.chapter}')
        label = ' '.join(parts) if parts else 'Oneshot'
        if self.title:
            label = f'{label} — {self.title}' if parts else self.title
        return label

    @property
    def web_url(self) -> str:
        return f'{MANGADEX_WEB_URL}/chapter/{self.id}'

    @property
    def is_downloadable(self) -> bool:
        return self.pages > 0 and not self.external_url

    def to_dict(self) -> dict[str, Any]:
        return {
            'id': self.id, 'chapter': self.chapter, 'volume': self.volume,
            'title': self.title, 'language': self.language, 'pages': self.pages,
            'scanlation_group': self.scanlation_group, 'external_url': self.external_url,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> 'MangaChapter':
        return cls(
            id=d['id'], chapter=d.get('chapter', ''), volume=d.get('volume', ''),
            title=d.get('title', ''), language=d.get('language', ''),
            pages=d.get('pages', 0) or 0, scanlation_group=d.get('scanlation_group', ''),
            external_url=d.get('external_url', ''),
        )

@dataclass
class MangaItem:
    id: str
    title: str = ''
    description: str = ''
    cover_url: str = ''
    cover_url_full: str = ''
    tags: list[str] = field(default_factory=list)
    status: str = ''
    year: Optional[int] = None
    content_rating: str = ''
    original_language: str = ''
    chapters: list[MangaChapter] = field(default_factory=list)

    @property
    def key(self) -> str:
        return self.id

    @property
    def web_url(self) -> str:
        return f'{MANGADEX_WEB_URL}/title/{self.id}'

    def to_dict(self) -> dict[str, Any]:
        return {
            'id': self.id, 'title': self.title, 'description': self.description,
            'cover_url': self.cover_url, 'cover_url_full': self.cover_url_full,
            'tags': self.tags, 'status': self.status, 'year': self.year,
            'content_rating': self.content_rating, 'original_language': self.original_language,
            'chapters': [c.to_dict() for c in self.chapters],
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> 'MangaItem':
        return cls(
            id=d['id'], title=d.get('title', ''), description=d.get('description', ''),
            cover_url=d.get('cover_url', ''), cover_url_full=d.get('cover_url_full', ''),
            tags=list(d.get('tags') or []), status=d.get('status', ''), year=d.get('year'),
            content_rating=d.get('content_rating', ''), original_language=d.get('original_language', ''),
            chapters=[MangaChapter.from_dict(c) for c in d.get('chapters') or []],
        )

@dataclass
class MangaBrowseResult:
    entries: list[MangaItem]
    has_more: bool

    def to_dict(self) -> dict[str, Any]:
        return {'entries': [e.to_dict() for e in self.entries], 'has_more': self.has_more}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> 'MangaBrowseResult':
        return cls(entries=[MangaItem.from_dict(e) for e in d.get('entries') or []], has_more=bool(d.get('has_more')))

class MangaDownloadCancelled(Exception):
    pass

_session: Optional[requests.Session] = None

def _get_session() -> requests.Session:
    global _session
    if _session is None:
        _session = requests.Session()
        _session.headers.update({'User-Agent': _USER_AGENT})
    return _session

def _request(path: str, params: dict) -> dict:
    resp = _get_session().get(f'{BASE_URL}{path}', params=params, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    return resp.json()

def _pick_localized(mapping: Optional[dict], preferred: str = 'en') -> str:
    if not mapping:
        return ''
    if preferred in mapping:
        return mapping[preferred]
    return next(iter(mapping.values()), '')

def _pick_title(attrs: dict) -> str:
    title = _pick_localized(attrs.get('title'))
    if title:
        return title
    for alt in attrs.get('altTitles') or []:
        alt_title = _pick_localized(alt)
        if alt_title:
            return alt_title
    return 'Untitled'

def _cover_filename(relationships: list[dict]) -> Optional[str]:
    for rel in relationships or []:
        if rel.get('type') == 'cover_art':
            file_name = (rel.get('attributes') or {}).get('fileName')
            if file_name:
                return file_name
    return None

def _entry_from_manga_obj(obj: dict) -> MangaItem:
    manga_id = obj['id']
    attrs = obj.get('attributes', {}) or {}
    relationships = obj.get('relationships', []) or []
    cover_file = _cover_filename(relationships)
    cover_thumb = f'{COVER_BASE_URL}/{manga_id}/{cover_file}.256.jpg' if cover_file else ''
    cover_full = f'{COVER_BASE_URL}/{manga_id}/{cover_file}' if cover_file else ''
    tags = [
        name for tag in (attrs.get('tags') or [])
        if (name := _pick_localized((tag.get('attributes') or {}).get('name')))
    ]
    return MangaItem(
        id=manga_id,
        title=_pick_title(attrs),
        description=_pick_localized(attrs.get('description')),
        cover_url=cover_thumb,
        cover_url_full=cover_full,
        tags=tags,
        status=(attrs.get('status') or '').title(),
        year=attrs.get('year'),
        content_rating=(attrs.get('contentRating') or '').title(),
        original_language=attrs.get('originalLanguage') or '',
    )

def _browse_or_search(cache_key: str, params: dict, page: int, use_cache: bool) -> MangaBrowseResult:
    if use_cache:
        cached = cache.load(_NS, cache_key, ttl_seconds=_TTL)
        if cached is not None:
            return MangaBrowseResult.from_dict(cached)

    offset = max(0, page - 1) * PAGE_SIZE
    query = dict(params)
    query['limit'] = PAGE_SIZE
    query['offset'] = offset
    query.setdefault('includes[]', ['cover_art'])
    query.setdefault('contentRating[]', ['safe', 'suggestive', 'erotica'])
    data = _request('/manga', query)
    entries = [_entry_from_manga_obj(obj) for obj in data.get('data', [])]
    total = data.get('total', offset + len(entries))
    result = MangaBrowseResult(entries=entries, has_more=offset + len(entries) < total)

    if use_cache:
        cache.save(_NS, cache_key, result.to_dict())
    return result

def browse_all(page: int = 1, use_cache: bool = True) -> MangaBrowseResult:
    return _browse_or_search(f'browse:{page}', {'order[followedCount]': 'desc'}, page, use_cache)

def search_all(query: str, page: int = 1, use_cache: bool = True) -> MangaBrowseResult:
    return _browse_or_search(f'search:{query}:{page}', {'title': query}, page, use_cache)

def _feed_for_manga(manga_id: str) -> list[MangaChapter]:
    params = {
        'translatedLanguage[]': ['en'],
        'order[volume]': 'asc',
        'order[chapter]': 'asc',
        'limit': 100,
        'includes[]': ['scanlation_group'],
        'contentRating[]': ['safe', 'suggestive', 'erotica'],
    }
    try:
        data = _request(f'/manga/{manga_id}/feed', params)
    except Exception:
        logger.exception('Failed to load MangaDex feed for %s', manga_id)
        return []
    chapters: list[MangaChapter] = []
    for obj in data.get('data', []):
        attrs = obj.get('attributes', {}) or {}
        group_name = next(
            ((rel.get('attributes') or {}).get('name', '') for rel in obj.get('relationships', []) or []
             if rel.get('type') == 'scanlation_group'),
            '',
        )
        chapter = MangaChapter(
            id=obj['id'],
            chapter=attrs.get('chapter') or '',
            volume=attrs.get('volume') or '',
            title=attrs.get('title') or '',
            language=attrs.get('translatedLanguage') or '',
            pages=attrs.get('pages') or 0,
            scanlation_group=group_name or '',
            external_url=attrs.get('externalUrl') or '',
        )
        chapters.append(chapter)
    return chapters

def details_for_entry(entry: MangaItem, use_cache: bool = True) -> Optional[MangaItem]:
    cache_key = f'details:{entry.id}'
    if use_cache:
        cached = cache.load(_NS, cache_key, ttl_seconds=_TTL)
        if cached is not None:
            return MangaItem.from_dict(cached)

    try:
        data = _request(f'/manga/{entry.id}', {'includes[]': ['cover_art', 'author', 'artist']})
    except Exception:
        logger.exception('Failed to load MangaDex details for %s', entry.id)
        return None
    obj = data.get('data')
    if not obj:
        return None
    result = _entry_from_manga_obj(obj)
    result.chapters = _feed_for_manga(entry.id)

    if use_cache:
        cache.save(_NS, cache_key, result.to_dict())
    return result

def default_downloads_dir() -> str:
    from src.core.config import settings
    return getattr(settings, 'download_dir_manga', os.path.join(settings.download_dir, 'manga'))

def _safe_filename(name: str) -> str:
    cleaned = re.sub(r'[<>:"/\\|?*]+', ' ', name or '').strip()
    cleaned = re.sub(r'\s+', ' ', cleaned)
    return cleaned[:120] or 'chapter'

def chapter_page_urls(chapter_id: str, data_saver: bool = False) -> list[str]:
    data = _request(f'/at-home/server/{chapter_id}', {})
    base_url = data['baseUrl']
    chapter_data = data['chapter']
    quality_dir = 'data-saver' if data_saver else 'data'
    filenames = chapter_data.get('dataSaver' if data_saver else 'data', [])
    chapter_hash = chapter_data['hash']
    return [f'{base_url}/{quality_dir}/{chapter_hash}/{filename}' for filename in filenames]

class MangaChapterDownloadJob(QRunnable):

    def __init__(self, manga_title: str, chapter: MangaChapter, dest_dir: str, item_id: str, download_manager):
        super().__init__()
        self._manga_title = manga_title
        self._chapter = chapter
        self._dest_dir = dest_dir
        self._item_id = item_id
        self._download_manager = download_manager
        self._cancelled = False

    def cancel(self) -> None:
        self._cancelled = True

    def run(self) -> None:
        dm = self._download_manager
        if not self._chapter.is_downloadable:
            logger.warning('Manga chapter %s: not downloadable (external/no pages)', self._chapter.id)
            dm.fail_external(self._item_id, 'This chapter is hosted externally and has no downloadable pages.')
            return
        logger.info('Manga chapter download starting: %s (%s)', self._chapter.label or self._chapter.id, self._chapter.id)
        try:
            urls = chapter_page_urls(self._chapter.id)
        except Exception as exc:
            logger.exception('Manga chapter %s: failed to resolve page URLs', self._chapter.id)
            dm.fail_external(self._item_id, f'Could not resolve pages: {exc}')
            return
        if not urls:
            logger.warning('Manga chapter %s: MangaDex returned zero page URLs', self._chapter.id)
            dm.fail_external(self._item_id, 'MangaDex returned no pages for this chapter.')
            return

        total_pages = len(urls)
        logger.info('Manga chapter %s: %d pages to fetch', self._chapter.id, total_pages)
        dm.update_external(self._item_id, total_bytes=0, progress=0.0)

        safe_title = _safe_filename(self._manga_title)
        safe_chapter = _safe_filename(self._chapter.label or self._chapter.id)
        cbz_path = os.path.join(self._dest_dir, f'{safe_title} - {safe_chapter}.cbz')

        session = _get_session()
        downloaded_bytes = 0
        try:
            os.makedirs(self._dest_dir, exist_ok=True)
            with tempfile.TemporaryDirectory() as tmp_dir:
                page_paths: list[str] = []
                for index, url in enumerate(urls, start=1):
                    if self._cancelled:
                        logger.info('Manga chapter %s: cancelled before page %d/%d', self._chapter.id, index, total_pages)
                        dm.fail_external(self._item_id, 'Cancelled.')
                        return
                    page_started = time.monotonic()
                    try:
                        resp = session.get(url, timeout=(REQUEST_TIMEOUT, REQUEST_TIMEOUT))
                        resp.raise_for_status()
                    except Exception:
                        logger.exception('Manga chapter %s: page %d/%d fetch failed after %.1fs (%s)', self._chapter.id, index, total_pages, time.monotonic() - page_started, url)
                        raise
                    ext = os.path.splitext(url)[1] or '.jpg'
                    page_path = os.path.join(tmp_dir, f'{index:03d}{ext}')
                    with open(page_path, 'wb') as fh:
                        fh.write(resp.content)
                    page_paths.append(page_path)
                    downloaded_bytes += len(resp.content)
                    logger.debug('Manga chapter %s: page %d/%d done in %.2fs', self._chapter.id, index, total_pages, time.monotonic() - page_started)
                    dm.update_external(
                        self._item_id,
                        downloaded_bytes=downloaded_bytes,
                        progress=round(index / total_pages * 100, 2),
                    )

                if self._cancelled:
                    logger.info('Manga chapter %s: cancelled after pages, before packaging', self._chapter.id)
                    dm.fail_external(self._item_id, 'Cancelled.')
                    return

                with zipfile.ZipFile(cbz_path, 'w', zipfile.ZIP_DEFLATED) as zf:
                    for page_path in page_paths:
                        zf.write(page_path, arcname=os.path.basename(page_path))
        except Exception as exc:
            logger.exception('Manga chapter %s: download failed', self._chapter.id)
            try:
                if os.path.isfile(cbz_path):
                    os.remove(cbz_path)
            except OSError:
                pass
            dm.fail_external(self._item_id, str(exc))
            return

        logger.info('Manga chapter %s: completed -> %s', self._chapter.id, cbz_path)
        dm.complete_external(self._item_id, cbz_path)

class MangaDownloadBridge(QObject):

    def __init__(self, download_manager, parent=None):
        super().__init__(parent)
        from src.core.downloader import DLState
        self._DLState = DLState
        self._download_manager = download_manager
        self._pool = QThreadPool(self)
        self._pool.setMaxThreadCount(2)
        self._jobs: dict[str, MangaChapterDownloadJob] = {}
        self._download_manager.item_updated.connect(self._on_item_updated)

    def download_chapter(self, manga_title: str, chapter: MangaChapter, dest_dir: Optional[str] = None) -> str:
        if not chapter.is_downloadable:
            raise ValueError('This chapter is hosted externally and has no downloadable pages.')
        dest_dir = dest_dir or default_downloads_dir()
        label = chapter.label or chapter.id
        item_id = self._download_manager.add_external(
            game_name=f'{manga_title} — {label}'.strip(' —'),
            console='',
            source='MangaDex',
            category='manga',
        )
        job = MangaChapterDownloadJob(manga_title, chapter, dest_dir, item_id, self._download_manager)
        self._jobs[item_id] = job
        self._download_manager.register_external_cancel(item_id, job.cancel)
        self._pool.start(job)
        return item_id

    def _on_item_updated(self, item_id: str) -> None:
        job = self._jobs.get(item_id)
        if job is None:
            return
        item = self._download_manager.get(item_id)
        if item is not None and item.state in (self._DLState.completed, self._DLState.error, self._DLState.cancelled):
            self._jobs.pop(item_id, None)

    def shutdown(self) -> None:
        for job in list(self._jobs.values()):
            job.cancel()
        self._pool.waitForDone(3000)
