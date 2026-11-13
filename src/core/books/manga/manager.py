from __future__ import annotations

import hashlib
import logging
import os
import re
import tempfile
import threading
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass, field, fields, replace as _dc_replace
from threading import Event, Lock
from typing import Any, Callable, Optional
from xml.sax.saxutils import escape as _xml

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from PySide6.QtCore import QObject, QRunnable, QThreadPool, Signal

from src.core.cache import cache

import re as _tm_re
import unicodedata as _tm_ud


def _tm_fold(text: str) -> str:
    plain = _tm_ud.normalize('NFKD', str(text or '').casefold())
    plain = ''.join(c for c in plain if not _tm_ud.combining(c))
    plain = plain.replace('&', ' and ').replace('+', ' and ').replace("'", '').replace('\u2019', '')
    return ' '.join(_tm_re.sub(r'[^0-9a-z\u0400-\u04ff\u3040-\u30ff\u4e00-\u9fff]+', ' ', plain).split())


def _tm_variants(query: str) -> list[str]:
    q = ' '.join(str(query or '').split())
    out: list[str] = []
    if _tm_re.search(r'[&+]', q):
        out.append(' '.join(_tm_re.sub(r'\s*[&+]\s*', ' and ', q).split()))
    if _tm_re.search(r'\band\b', q, _tm_re.I):
        out.append(_tm_re.sub(r'\band\b', '&', q, flags=_tm_re.I))
    plain = _tm_fold(q)
    out.append(plain)
    if ' and ' in f' {plain} ':
        out.append(' '.join(w for w in plain.split() if w != 'and'))
    seen = {q}
    return [v for v in out if v and not (v in seen or seen.add(v))]


logger = logging.getLogger(__name__)

BASE_URL = "https://api.mangadex.org"
COVER_BASE_URL = "https://uploads.mangadex.org/covers"
MANGADEX_WEB_URL = "https://mangadex.org"
REPORT_URL = "https://api.mangadex.network/report"
PAGE_SIZE = 24
REQUEST_TIMEOUT = 15
FEED_PAGE = 500
FEED_MAX = 5000
_MAX_OFFSET = 10000
_MAX_PAGE_BYTES = 48 * 1024 * 1024
_PAGE_WORKERS = 4

_NS = 'manga'
_TTL = 900
_V = 'v2'

COVER_DIR = os.path.join(tempfile.gettempdir(), 'pirachest', 'manga_covers')
_COVER_MAX_AGE = 14 * 86400

_USER_AGENT = "PiraChest/1.0 (+https://api.mangadex.org/docs/)"

RATINGS_DEFAULT = ['safe', 'suggestive', 'erotica']

LANGUAGES = {
    'en': 'English', 'ar': 'Arabic', 'fr': 'French', 'es': 'Spanish', 'es-la': 'Spanish (LATAM)',
    'pt-br': 'Portuguese (BR)', 'pt': 'Portuguese (PT)', 'de': 'German', 'it': 'Italian',
    'ru': 'Russian', 'tr': 'Turkish', 'id': 'Indonesian', 'vi': 'Vietnamese', 'pl': 'Polish',
    'uk': 'Ukrainian', 'th': 'Thai', 'zh': 'Chinese (Simplified)', 'zh-hk': 'Chinese (Traditional)',
    'ja': 'Japanese', 'ko': 'Korean',
}

SORTS = {
    'relevance': 'desc',
    'followedCount': 'desc',
    'latestUploadedChapter': 'desc',
    'rating': 'desc',
    'createdAt': 'desc',
    'title': 'asc',
}

_LINKS = {
    'al': ('AniList', 'https://anilist.co/manga/{}'),
    'mal': ('MyAnimeList', 'https://myanimelist.net/manga/{}'),
    'kt': ('Kitsu', 'https://kitsu.app/manga/{}'),
    'ap': ('Anime-Planet', 'https://www.anime-planet.com/manga/{}'),
    'mu': ('MangaUpdates', 'https://www.mangaupdates.com/series.html?id={}'),
    'engtl': ('Official', '{}'),
    'raw': ('Raw', '{}'),
}


def _num(value: str) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return float('inf')


def _pad(value: str, width: int) -> str:
    whole, dot, frac = value.partition('.')
    return f'{whole.zfill(width)}{dot}{frac}' if whole.isdigit() else value


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
    published: str = ''

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
    def file_label(self) -> str:
        parts = []
        if self.volume:
            parts.append(f'Vol. {_pad(self.volume, 2)}')
        if self.chapter:
            parts.append(f'Ch. {_pad(self.chapter, 3)}')
        base = ' '.join(parts) or 'Oneshot'
        if self.title:
            return f'{base} - {self.title}' if parts else self.title
        return base

    @property
    def sort_key(self) -> tuple[float, float]:
        return (_num(self.volume), _num(self.chapter))

    @property
    def web_url(self) -> str:
        return f'{MANGADEX_WEB_URL}/chapter/{self.id}'

    @property
    def is_downloadable(self) -> bool:
        return self.pages > 0 and not self.external_url

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> 'MangaChapter':
        names = {f.name for f in fields(cls)}
        item = cls(**{k: v for k, v in d.items() if k in names})
        item.pages = item.pages or 0
        return item


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
    authors: list[str] = field(default_factory=list)
    artists: list[str] = field(default_factory=list)
    alt_titles: list[str] = field(default_factory=list)
    demographic: str = ''
    rating: Optional[float] = None
    follows: int = 0
    available_languages: list[str] = field(default_factory=list)
    links: list[list[str]] = field(default_factory=list)

    @property
    def key(self) -> str:
        return self.id

    @property
    def web_url(self) -> str:
        return f'{MANGADEX_WEB_URL}/title/{self.id}'

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> 'MangaItem':
        names = {f.name for f in fields(cls)}
        kwargs = {k: v for k, v in d.items() if k in names}
        kwargs['chapters'] = [MangaChapter.from_dict(c) for c in d.get('chapters') or []]
        return cls(**kwargs)


@dataclass
class MangaBrowseResult:
    entries: list[MangaItem]
    has_more: bool

    def to_dict(self) -> dict[str, Any]:
        return {'entries': [e.to_dict() for e in self.entries], 'has_more': self.has_more}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> 'MangaBrowseResult':
        return cls(entries=[MangaItem.from_dict(e) for e in d.get('entries') or []], has_more=bool(d.get('has_more')))


@dataclass(frozen=True)
class BrowseFilters:
    query: str = ''
    sort: str = 'followedCount'
    status: str = ''
    genre: str = ''
    exclude: str = ''
    language: str = 'en'
    safe_only: bool = False

    def params(self) -> dict:
        sort = self.sort if self.sort in SORTS else 'followedCount'
        if sort == 'relevance' and not self.query:
            sort = 'followedCount'
        params: dict[str, Any] = {
            f'order[{sort}]': SORTS[sort],
            'includes[]': ['cover_art'],
            'contentRating[]': ['safe'] if self.safe_only else RATINGS_DEFAULT,
            'hasAvailableChapters': 'true',
        }
        if self.query:
            params['title'] = self.query
        if self.status:
            params['status[]'] = [self.status]
        if self.genre:
            params['includedTags[]'] = [g for g in self.genre.split(',') if g]
        if self.exclude:
            params['excludedTags[]'] = [g for g in self.exclude.split(',') if g]
        if self.language:
            params['availableTranslatedLanguage[]'] = [self.language]
        return params

    def cache_id(self) -> str:
        return hashlib.sha1(repr(self).encode('utf-8')).hexdigest()[:20]


_session: Optional[requests.Session] = None
_session_lock = Lock()


def _get_session() -> requests.Session:
    global _session
    if _session is None:
        with _session_lock:
            if _session is None:
                session = requests.Session()
                session.headers.update({'User-Agent': _USER_AGENT})
                retry = Retry(
                    total=3, backoff_factor=0.7, status_forcelist=(429, 500, 502, 503, 504),
                    allowed_methods=('GET',), respect_retry_after_header=True, raise_on_status=False,
                )
                adapter = HTTPAdapter(max_retries=retry, pool_connections=8, pool_maxsize=16)
                session.mount('https://', adapter)
                session.mount('http://', adapter)
                _session = session
    return _session


def _request(path: str, params: Optional[dict] = None) -> dict:
    resp = _get_session().get(f'{BASE_URL}{path}', params=params, timeout=REQUEST_TIMEOUT)
    if not resp.ok:
        try:
            detail = resp.json()['errors'][0]['detail']
        except Exception:
            detail = ''
        raise RuntimeError(f'MangaDex {resp.status_code}: {detail or resp.reason}')
    return resp.json()


def _pick_localized(mapping: Optional[dict], preferred: str = 'en') -> str:
    if not mapping:
        return ''
    for key in (preferred, 'en'):
        if mapping.get(key):
            return mapping[key]
    return next((v for v in mapping.values() if v), '')


def _pick_title(attrs: dict, lang: str = 'en') -> str:
    titles = attrs.get('title') or {}
    alts = attrs.get('altTitles') or []
    if titles.get('en'):
        return titles['en']
    for alt in alts:
        for key in (lang, 'en'):
            if alt.get(key):
                return alt[key]
    for key in ('ja-ro', 'ko-ro', 'zh-ro'):
        if titles.get(key):
            return titles[key]
    return next((v for v in titles.values() if v), '') or next((_pick_localized(a) for a in alts if a), '') or 'Untitled'


def _alt_titles(attrs: dict, main: str, limit: int = 6) -> list[str]:
    values = list((attrs.get('title') or {}).values())
    for alt in attrs.get('altTitles') or []:
        values.extend(alt.values())
    return [v for v in dict.fromkeys(values) if v and v != main][:limit]


def _cover_urls(manga_id: str, relationships: list[dict]) -> tuple[str, str]:
    for rel in relationships or []:
        if rel.get('type') == 'cover_art':
            file_name = (rel.get('attributes') or {}).get('fileName')
            if file_name:
                base = f'{COVER_BASE_URL}/{manga_id}/{file_name}'
                return f'{base}.256.jpg', f'{base}.512.jpg'
    return '', ''


def _people(relationships: list[dict], kind: str) -> list[str]:
    names = ((rel.get('attributes') or {}).get('name') for rel in relationships or [] if rel.get('type') == kind)
    return list(dict.fromkeys(n for n in names if n))


def _links(attrs: dict) -> list[list[str]]:
    out = []
    for key, value in (attrs.get('links') or {}).items():
        if key not in _LINKS or not value:
            continue
        label, template = _LINKS[key]
        url = template.format(value)
        if url.startswith('http'):
            out.append([label, url])
    return out


def _entry_from_manga_obj(obj: dict, lang: str = 'en') -> MangaItem:
    manga_id = obj['id']
    attrs = obj.get('attributes', {}) or {}
    rels = obj.get('relationships', []) or []
    thumb, full = _cover_urls(manga_id, rels)
    title = _pick_title(attrs, lang)
    tags = [
        name for tag in (attrs.get('tags') or [])
        if (name := _pick_localized((tag.get('attributes') or {}).get('name')))
    ]
    return MangaItem(
        id=manga_id,
        title=title,
        description=_pick_localized(attrs.get('description'), lang),
        cover_url=thumb,
        cover_url_full=full,
        tags=tags,
        status=(attrs.get('status') or '').title(),
        year=attrs.get('year'),
        content_rating=(attrs.get('contentRating') or '').title(),
        original_language=attrs.get('originalLanguage') or '',
        authors=_people(rels, 'author'),
        artists=_people(rels, 'artist'),
        alt_titles=_alt_titles(attrs, title),
        demographic=(attrs.get('publicationDemographic') or '').title(),
        available_languages=list(attrs.get('availableTranslatedLanguages') or []),
        links=_links(attrs),
    )


_pruned = False


def _prune_covers() -> None:
    global _pruned
    _pruned = True
    cutoff = time.time() - _COVER_MAX_AGE
    try:
        for name in os.listdir(COVER_DIR):
            path = os.path.join(COVER_DIR, name)
            if os.path.getmtime(path) < cutoff:
                os.remove(path)
    except OSError:
        pass


def fetch_cover(url: str) -> str:
    if not url or not url.startswith('http'):
        return url
    path = os.path.join(COVER_DIR, hashlib.sha1(url.encode('utf-8')).hexdigest() + '.jpg')
    if os.path.isfile(path) and os.path.getsize(path) > 0:
        return path
    tmp = f'{path}.{threading.get_ident()}.part'
    try:
        os.makedirs(COVER_DIR, exist_ok=True)
        if not _pruned:
            _prune_covers()
        resp = _get_session().get(url, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        if not resp.content or 'image' not in resp.headers.get('Content-Type', ''):
            raise ValueError('not an image')
        with open(tmp, 'wb') as fh:
            fh.write(resp.content)
        os.replace(tmp, path)
        return path
    except Exception as exc:
        logger.debug('Cover fetch failed for %s: %s', url, exc)
        try:
            os.remove(tmp)
        except OSError:
            pass
        return url


def _localize_covers(entries: list[MangaItem], full: bool = False) -> None:
    todo = [(e, attr) for e in entries for attr in (('cover_url', 'cover_url_full') if full else ('cover_url',))
            if getattr(e, attr).startswith('http')]
    if not todo:
        return
    with ThreadPoolExecutor(max_workers=8) as pool:
        for (entry, attr), path in zip(todo, pool.map(fetch_cover, [getattr(e, a) for e, a in todo])):
            setattr(entry, attr, path)


def _browse_once(filters: BrowseFilters, page: int = 1, use_cache: bool = True) -> MangaBrowseResult:
    cache_key = f'{_V}:browse:{filters.cache_id()}:{page}'
    if use_cache:
        cached = cache.load(_NS, cache_key, ttl_seconds=_TTL)
        if cached is not None:
            result = MangaBrowseResult.from_dict(cached)
            _localize_covers(result.entries)
            return result

    offset = max(0, page - 1) * PAGE_SIZE
    if offset + PAGE_SIZE > _MAX_OFFSET:
        return MangaBrowseResult(entries=[], has_more=False)
    query = filters.params()
    query.update({'limit': PAGE_SIZE, 'offset': offset})
    data = _request('/manga', query)
    entries = [_entry_from_manga_obj(obj, filters.language) for obj in data.get('data', [])]
    total = min(data.get('total', offset + len(entries)), _MAX_OFFSET)
    result = MangaBrowseResult(entries=entries, has_more=offset + len(entries) < total)

    if use_cache:
        cache.save(_NS, cache_key, result.to_dict())
    _localize_covers(entries)
    return result


def browse(filters: BrowseFilters, page: int = 1, use_cache: bool = True) -> MangaBrowseResult:
    result = _browse_once(filters, page, use_cache)
    if result.entries or page != 1 or not filters.query:
        return result
    for alt in _tm_variants(filters.query):
        found = _browse_once(_dc_replace(filters, query=alt), page, use_cache)
        if found.entries:
            return found
    return result


def browse_all(page: int = 1, use_cache: bool = True) -> MangaBrowseResult:
    return browse(BrowseFilters(), page, use_cache)


def search_all(query: str, page: int = 1, use_cache: bool = True) -> MangaBrowseResult:
    return browse(BrowseFilters(query=query, sort='relevance'), page, use_cache)


def random_manga(filters: BrowseFilters, exclude: Optional[str] = None) -> Optional[MangaItem]:
    pool = BrowseFilters(genre=filters.genre, exclude=filters.exclude, language=filters.language, safe_only=filters.safe_only)
    draw: dict[str, Any] = {
        'includes[]': ['cover_art'],
        'contentRating[]': ['safe'] if filters.safe_only else RATINGS_DEFAULT,
    }
    if filters.genre:
        draw['includedTags[]'] = [g for g in filters.genre.split(',') if g]
    if filters.exclude:
        draw['excludedTags[]'] = [g for g in filters.exclude.split(',') if g]
    for _ in range(10):
        picked = _request('/manga/random', draw).get('data')
        if not picked:
            return None
        if exclude and picked.get('id') == exclude:
            continue
        check = pool.params()
        check.update({'ids[]': [picked['id']], 'limit': 1})
        found = _request('/manga', check).get('data')
        if found:
            entry = _entry_from_manga_obj(found[0], filters.language)
            _localize_covers([entry])
            return entry
    raise RuntimeError('No readable manga turned up, try again')


from time import monotonic as _mono


def _genre_norm(name: str) -> str:
    return re.sub(r'[^a-z0-9]+', '', (name or '').lower())


_tag_memo: dict = {}


def _all_tags() -> list[tuple[str, str, str]]:
    memo = _tag_memo.get('v')
    if memo and _mono() - memo[0] < 3600:
        return memo[1]
    key = f'{_V}:alltags'
    cached = cache.load(_NS, key, ttl_seconds=86400)
    if cached is None:
        data = _request('/manga/tag')
        tags = sorted(
            [_pick_localized(t['attributes'].get('name')), t['id'], (t.get('attributes') or {}).get('group') or '']
            for t in data.get('data', [])
        )
        cached = {'tags': tags}
        cache.save(_NS, key, cached)
    result = [(name, tag_id, group) for name, tag_id, group in cached['tags']]
    _tag_memo['v'] = (_mono(), result)
    return result


def resolve_genres(names: list[str]) -> dict:
    tags = _all_tags()
    by_key = {_genre_norm(name): (name, tag_id) for name, tag_id, _group in tags}
    found: list[str] = []
    ids: list[str] = []
    missing: list[str] = []
    seen: set[str] = set()
    for raw in names:
        name = re.sub(r'\s+', ' ', raw or '').strip()
        if not name:
            continue
        group = None
        hit = by_key.get(_genre_norm(name))
        if hit:
            group = [hit]
        elif ' ' in name:
            parts = [by_key.get(_genre_norm(word)) for word in name.split(' ')]
            if all(parts):
                group = parts
        if group is None:
            missing.append(name)
            continue
        for tag_name, tag_id in group:
            if tag_id not in seen:
                seen.add(tag_id)
                found.append(tag_name)
                ids.append(tag_id)
    pool = [name for name, _tag_id, group in tags if group != 'content']
    return {'tags': found, 'ids': ids, 'missing': missing, 'pool': pool}


def genres() -> list[tuple[str, str]]:
    return [(name, tag_id) for name, tag_id, group in _all_tags() if group in ('genre', 'theme')]


def _parse_chapter(obj: dict) -> MangaChapter:
    attrs = obj.get('attributes', {}) or {}
    group = next(
        ((rel.get('attributes') or {}).get('name', '') for rel in obj.get('relationships', []) or []
         if rel.get('type') == 'scanlation_group'),
        '',
    )
    return MangaChapter(
        id=obj['id'],
        chapter=attrs.get('chapter') or '',
        volume=attrs.get('volume') or '',
        title=attrs.get('title') or '',
        language=attrs.get('translatedLanguage') or '',
        pages=attrs.get('pages') or 0,
        scanlation_group=group or '',
        external_url=attrs.get('externalUrl') or '',
        published=(attrs.get('publishAt') or '')[:10],
    )


def _dedupe(chapters: list[MangaChapter]) -> list[MangaChapter]:
    seen: dict[tuple[str, str], int] = {}
    out: list[MangaChapter] = []
    for c in chapters:
        if not c.chapter:
            out.append(c)
            continue
        idx = seen.get((c.volume, c.chapter))
        if idx is None:
            seen[(c.volume, c.chapter)] = len(out)
            out.append(c)
        elif c.is_downloadable and not out[idx].is_downloadable:
            out[idx] = c
    return out


def _feed_for_manga(manga_id: str, language: str = 'en') -> list[MangaChapter]:
    params = {
        'translatedLanguage[]': [language],
        'order[volume]': 'asc',
        'order[chapter]': 'asc',
        'limit': FEED_PAGE,
        'includes[]': ['scanlation_group'],
        'contentRating[]': RATINGS_DEFAULT,
    }
    chapters: list[MangaChapter] = []
    offset = 0
    while True:
        data = _request(f'/manga/{manga_id}/feed', {**params, 'offset': offset})
        batch = data.get('data', [])
        chapters.extend(_parse_chapter(o) for o in batch)
        offset += len(batch)
        if not batch or offset >= min(data.get('total', offset), FEED_MAX):
            break
    return sorted(_dedupe(chapters), key=lambda c: c.sort_key)


def _statistics(manga_id: str) -> tuple[Optional[float], int]:
    try:
        stats = (_request(f'/statistics/manga/{manga_id}').get('statistics') or {}).get(manga_id) or {}
    except Exception:
        logger.debug('No statistics for %s', manga_id, exc_info=True)
        return None, 0
    average = (stats.get('rating') or {}).get('average')
    return (round(average, 2) if average else None), int(stats.get('follows') or 0)


def details_for_entry(entry: MangaItem, use_cache: bool = True, language: str = 'en') -> Optional[MangaItem]:
    cache_key = f'{_V}:details:{entry.id}:{language}'
    if use_cache:
        cached = cache.load(_NS, cache_key, ttl_seconds=_TTL)
        if cached is not None:
            result = MangaItem.from_dict(cached)
            _localize_covers([result], full=True)
            return result

    with ThreadPoolExecutor(max_workers=2) as pool:
        feed = pool.submit(_feed_for_manga, entry.id, language)
        stats = pool.submit(_statistics, entry.id)
        try:
            data = _request(f'/manga/{entry.id}', {'includes[]': ['cover_art', 'author', 'artist']})
        except Exception:
            logger.exception('Failed to load MangaDex details for %s', entry.id)
            return None
        obj = data.get('data')
        if not obj:
            return None
        result = _entry_from_manga_obj(obj, language)
        result.chapters = feed.result()
        result.rating, result.follows = stats.result()

    if use_cache:
        cache.save(_NS, cache_key, result.to_dict())
    _localize_covers([result], full=True)
    return result


class _Cancelled(Exception):
    pass


def default_downloads_dir() -> str:
    from src.core.config import settings
    return getattr(settings, 'download_dir_manga', os.path.join(settings.download_dir, 'manga'))


def _safe_filename(name: str) -> str:
    cleaned = re.sub(r'[<>:\"/\\|?*]+', ' ', name or '')
    cleaned = re.sub(r'\s+', ' ', cleaned).strip(' .')
    return cleaned[:120] or 'chapter'


def _chapter_cbz_path(manga_title: str, chapter: MangaChapter, dest_dir: Optional[str] = None) -> str:
    name = f'{_safe_filename(manga_title)} - {_safe_filename(chapter.file_label or chapter.id)}.cbz'
    return os.path.join(dest_dir or default_downloads_dir(), name)


def downloaded_ids(manga_title: str, chapters: list[MangaChapter]) -> set[str]:
    dest = default_downloads_dir()
    try:
        existing = set(os.listdir(dest))
    except OSError:
        return set()
    return {c.id for c in chapters if os.path.basename(_chapter_cbz_path(manga_title, c, dest)) in existing}


def chapter_page_urls(chapter_id: str, data_saver: bool = False) -> list[str]:
    data = _request(f'/at-home/server/{chapter_id}')
    base_url = data['baseUrl']
    chapter_data = data['chapter']
    quality_dir = 'data-saver' if data_saver else 'data'
    filenames = chapter_data.get('dataSaver' if data_saver else 'data', [])
    return [f'{base_url}/{quality_dir}/{chapter_data["hash"]}/{name}' for name in filenames]


_report_pool: Optional[ThreadPoolExecutor] = None


def _report(url: str, ok: bool, size: int, started: float, headers) -> None:
    global _report_pool
    if '.mangadex.org' in url:
        return
    payload = {
        'url': url, 'success': ok, 'bytes': size,
        'duration': int((time.monotonic() - started) * 1000),
        'cached': bool(headers and headers.get('X-Cache', '').upper().startswith('HIT')),
    }
    if _report_pool is None:
        _report_pool = ThreadPoolExecutor(max_workers=1)

    def _post() -> None:
        try:
            requests.post(REPORT_URL, json=payload, headers={'User-Agent': _USER_AGENT}, timeout=5)
        except Exception:
            pass
    _report_pool.submit(_post)


def _download_page(url: str, path: str, is_cancelled: Callable[[], bool], attempts: int = 3) -> int:
    last: Optional[Exception] = None
    for attempt in range(attempts):
        if is_cancelled():
            raise _Cancelled()
        started, size = time.monotonic(), 0
        try:
            with _get_session().get(url, timeout=REQUEST_TIMEOUT, stream=True) as resp:
                resp.raise_for_status()
                with open(path, 'wb') as fh:
                    for chunk in resp.iter_content(65536):
                        if is_cancelled():
                            raise _Cancelled()
                        size += len(chunk)
                        if size > _MAX_PAGE_BYTES:
                            raise RuntimeError('Manga page is too large')
                        fh.write(chunk)
                headers = resp.headers
            _report(url, True, size, started, headers)
            return size
        except _Cancelled:
            raise
        except Exception as exc:
            last = exc
            _report(url, False, size, started, None)
            logger.warning('Page fetch failed (%d/%d) %s: %s', attempt + 1, attempts, url, exc)
            time.sleep(0.5 * (attempt + 1))
    raise last if last else RuntimeError('Page download failed')


def _comic_info(manga_title: str, chapter: MangaChapter, authors: str) -> str:
    entries = (
        ('Series', manga_title), ('Number', chapter.chapter), ('Volume', chapter.volume),
        ('Title', chapter.title), ('Writer', authors), ('LanguageISO', chapter.language),
        ('Web', chapter.web_url),
    )
    body = ''.join(f'<{tag}>{_xml(value)}</{tag}>' for tag, value in entries if value)
    return f'<?xml version="1.0" encoding="utf-8"?><ComicInfo>{body}</ComicInfo>'


class MangaChapterDownloadJob(QRunnable):
    def __init__(self, manga_title: str, chapter: MangaChapter, dest_dir: str, item_id: str, download_manager,
                 data_saver: bool = False, authors: str = ''):
        super().__init__()
        self._manga_title = manga_title
        self._chapter = chapter
        self._dest_dir = dest_dir
        self._item_id = item_id
        self._download_manager = download_manager
        self._data_saver = data_saver
        self._authors = authors
        self._cancelled = False

    def cancel(self) -> None:
        self._cancelled = True

    def run(self) -> None:
        dm, chapter = self._download_manager, self._chapter
        if not chapter.is_downloadable:
            logger.warning('Manga chapter %s: not downloadable (external/no pages)', chapter.id)
            dm.fail_external(self._item_id, 'This chapter is hosted externally and has no downloadable pages.')
            return
        logger.info('Manga chapter download starting: %s (%s)', chapter.label, chapter.id)
        try:
            urls = chapter_page_urls(chapter.id, self._data_saver)
        except Exception as exc:
            logger.exception('Manga chapter %s: failed to resolve page URLs', chapter.id)
            dm.fail_external(self._item_id, f'Could not resolve pages: {exc}')
            return
        if not urls:
            dm.fail_external(self._item_id, 'MangaDex returned no pages for this chapter.')
            return

        total = len(urls)
        dm.update_external(self._item_id, total_bytes=0, progress=0.0)
        cbz_path = _chapter_cbz_path(self._manga_title, chapter, self._dest_dir)
        part_path = cbz_path + '.part'
        abort, lock, done = Event(), Lock(), [0, 0]

        def is_cancelled() -> bool:
            return self._cancelled or abort.is_set()

        try:
            os.makedirs(self._dest_dir, exist_ok=True)
            with tempfile.TemporaryDirectory() as tmp_dir:
                paths = [os.path.join(tmp_dir, f'{i:03d}{os.path.splitext(u)[1] or ".jpg"}')
                         for i, u in enumerate(urls, start=1)]

                def fetch(i: int) -> None:
                    size = _download_page(urls[i], paths[i], is_cancelled)
                    with lock:
                        done[0] += 1
                        done[1] += size
                        count, nbytes = done
                    dm.update_external(self._item_id, downloaded_bytes=nbytes,
                                       progress=round(count / total * 100, 2))

                with ThreadPoolExecutor(max_workers=min(_PAGE_WORKERS, total)) as pool:
                    futures = [pool.submit(fetch, i) for i in range(total)]
                    try:
                        for future in as_completed(futures):
                            future.result()
                    except BaseException:
                        abort.set()
                        raise
                if self._cancelled:
                    raise _Cancelled()

                with zipfile.ZipFile(part_path, 'w', zipfile.ZIP_STORED) as zf:
                    zf.writestr('ComicInfo.xml', _comic_info(self._manga_title, chapter, self._authors))
                    for path in paths:
                        zf.write(path, arcname=os.path.basename(path))
                os.replace(part_path, cbz_path)
        except _Cancelled:
            self._remove(part_path)
            dm.fail_external(self._item_id, 'Cancelled.')
            return
        except Exception as exc:
            logger.exception('Manga chapter %s: download failed', chapter.id)
            self._remove(part_path)
            dm.fail_external(self._item_id, str(exc))
            return

        logger.info('Manga chapter %s: completed -> %s', chapter.id, cbz_path)
        dm.complete_external(self._item_id, cbz_path)

    @staticmethod
    def _remove(path: str) -> None:
        try:
            os.remove(path)
        except OSError:
            pass


class MangaDownloadBridge(QObject):
    chapter_state = Signal(str, str, float)

    def __init__(self, download_manager, parent=None):
        super().__init__(parent)
        from src.core.downloader import DLState
        self._DLState = DLState
        self._download_manager = download_manager
        self._pool = QThreadPool(self)
        self._pool.setMaxThreadCount(2)
        self._jobs: dict[str, MangaChapterDownloadJob] = {}
        self._chapter_by_item: dict[str, str] = {}
        self._item_by_chapter: dict[str, str] = {}
        self.data_saver = False
        self._download_manager.item_updated.connect(self._on_item_updated)

    def download_chapter(self, manga_title: str, chapter: MangaChapter, dest_dir: Optional[str] = None,
                         authors: str = '') -> str:
        if not chapter.is_downloadable:
            raise ValueError('This chapter is hosted externally and has no downloadable pages.')
        existing = self._item_by_chapter.get(chapter.id)
        if existing:
            return existing
        dest_dir = dest_dir or default_downloads_dir()
        label = chapter.label or chapter.id
        item_id = self._download_manager.add_external(
            game_name=f'{manga_title} — {label}'.strip(' —'),
            console='',
            source='MangaDex',
            category='manga',
        )
        job = MangaChapterDownloadJob(manga_title, chapter, dest_dir, item_id, self._download_manager,
                                      data_saver=self.data_saver, authors=authors)
        self._jobs[item_id] = job
        self._chapter_by_item[item_id] = chapter.id
        self._item_by_chapter[chapter.id] = item_id
        self._download_manager.register_external_cancel(item_id, job.cancel)
        self.chapter_state.emit(chapter.id, 'active', 0.0)
        self._pool.start(job)
        return item_id

    def _on_item_updated(self, item_id: str) -> None:
        chapter_id = self._chapter_by_item.get(item_id)
        item = self._download_manager.get(item_id) if chapter_id else None
        if item is None:
            return
        states = self._DLState
        if item.state == states.completed:
            state = 'done'
        elif item.state in (states.error, states.cancelled):
            state = 'error'
        else:
            state = 'active'
        if state != 'active':
            self._jobs.pop(item_id, None)
            self._chapter_by_item.pop(item_id, None)
            self._item_by_chapter.pop(chapter_id, None)
        self.chapter_state.emit(chapter_id, state, float(getattr(item, 'progress', 0) or 0))

    def shutdown(self) -> None:
        for job in list(self._jobs.values()):
            job.cancel()
        self._pool.waitForDone(3000)
