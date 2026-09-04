from __future__ import annotations
import logging
import re
import requests
from concurrent.futures import ThreadPoolExecutor
from typing import Optional
from urllib.parse import quote
from ..base import (
    RepackEntry,
    RepackPage,
    RepackDetails,
    RepackSource,
    DEFAULT_TTL_SECONDS,
    load_page,
    save_page,
    load_details,
    save_details,
)

logger = logging.getLogger(__name__)

_BCU_BASE = 'https://gog-rev.com/bcu'
_GOG_API_BASE = 'https://api.gog.com/v2/games'
_PAGE_SIZE = 40
_GOG_API_TTL_SECONDS = 24 * 60 * 60
_INDEX_TTL_SECONDS = 6 * 60 * 60
_REQUEST_TIMEOUT = 10
_MAX_WORKERS = 24

_HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36',
    'Accept': 'application/json',
}

_session = requests.Session()
_session.headers.update(_HEADERS)
_session.trust_env = False
_adapter = requests.adapters.HTTPAdapter(pool_connections=_MAX_WORKERS, pool_maxsize=_MAX_WORKERS, max_retries=0)
_session.mount('https://', _adapter)
_session.mount('http://', _adapter)

_executor = ThreadPoolExecutor(max_workers=_MAX_WORKERS, thread_name_prefix='gog-fetch')


def _get_json(url: str):
    resp = _session.get(url, timeout=_REQUEST_TIMEOUT)
    resp.raise_for_status()
    return resp.json()


def _slug_matches_query(slug: str, query_tokens: list[str]) -> bool:
    haystack = slug.replace('_', ' ').lower()
    return all(tok in haystack for tok in query_tokens)


class GogRevivedSource(RepackSource):
    key = 'gog'
    display_name = 'GOG Revived'

    def __init__(self, ttl_seconds: int = DEFAULT_TTL_SECONDS):
        self._ttl_seconds = ttl_seconds

    # ---- slug index (cheap: one request, no per-game fetches) ----

    def _load_slugs(self, use_cache: bool = True) -> list[str]:
        cache_key = '__slug_index__'
        if use_cache:
            cached = load_page(self.key, cache_key, ttl_seconds=_INDEX_TTL_SECONDS)
            if cached is not None:
                return cached.get('entries', [{}])[0].get('slugs', [])
        index_data = _get_json(f'{_BCU_BASE}/index.json')
        slugs = index_data.get('games', []) if isinstance(index_data, dict) else index_data
        slugs = list(reversed(slugs))
        save_page(self.key, cache_key, [{'slugs': slugs}], has_more=False)
        return slugs

    # ---- per-game torrent metadata (only present once archived) ----

    def _load_bcu_game(self, slug: str, use_cache: bool = True):
        cache_key = f'bcu_game:{slug}'
        if use_cache:
            cached = load_details(self.key, cache_key, ttl_seconds=self._ttl_seconds)
            if cached is not None:
                return cached or None
        try:
            data = _get_json(f'{_BCU_BASE}/{quote(slug)}.json')
        except Exception as exc:
            logger.info('No BCU torrent entry for slug %s (likely not archived yet): %s', slug, exc)
            save_details(self.key, cache_key, {})
            return None
        save_details(self.key, cache_key, data)
        return data

    def _load_bcu_games(self, slugs: list[str], use_cache: bool) -> list[dict]:
        if not slugs:
            return []
        futures = [_executor.submit(self._load_bcu_game, slug, use_cache) for slug in slugs]
        games = []
        for future in futures:
            try:
                game = future.result()
                if game:
                    games.append(game)
            except Exception as exc:
                logger.warning('Failed to load BCU entry: %s', exc)
        return games

    # ---- GOG store API (cover art, description, screenshots) ----

    def _load_gog_api(self, product_id: str, use_cache: bool = True):
        if not product_id:
            return None
        cache_key = f'gogapi:{product_id}'
        if use_cache:
            cached = load_details(self.key, cache_key, ttl_seconds=_GOG_API_TTL_SECONDS)
            if cached is not None:
                return cached or None
        try:
            data = _get_json(f'{_GOG_API_BASE}/{product_id}?locale=en-US')
        except Exception as exc:
            logger.info('GOG API enrichment unavailable for product %s: %s', product_id, exc)
            save_details(self.key, cache_key, {})
            return None
        save_details(self.key, cache_key, data or {})
        return data or None

    def _load_store_page_text(self, slug: str, use_cache: bool = True) -> Optional[str]:
        if not slug:
            return None
        cache_key = f'store_page_text:{slug}'
        if use_cache:
            cached = load_details(self.key, cache_key, ttl_seconds=_GOG_API_TTL_SECONDS)
            if cached is not None:
                return cached.get('text') or None
        try:
            resp = _session.get(f'https://www.gog.com/en/game/{quote(slug)}', timeout=_REQUEST_TIMEOUT)
            resp.raise_for_status()
            html = resp.text
        except Exception as exc:
            logger.info('GOG store page unavailable for slug %s: %s', slug, exc)
            save_details(self.key, cache_key, {'text': ''})
            return None
        text = re.sub('<[^>]+>', ' ', html)
        text = re.sub('&amp;', '&', text)
        text = re.sub('[ \\t]+', ' ', text)
        save_details(self.key, cache_key, {'text': text})
        return text

    def _extract_rating_from_page(self, text: str) -> Optional[str]:
        if not text:
            return None
        match = re.search('(\\d\\.\\d)\\s*/\\s*5\\s*\\(\\s*([\\d,]+)\\s*Reviews?\\s*\\)', text)
        if not match:
            return None
        avg, count = match.group(1), match.group(2).replace(',', '')
        return f'{avg}/5 ({count} reviews)'

    def _extract_system_requirements_from_page(self, text: str) -> Optional[str]:
        if not text:
            return None
        match = re.search('What are the system requirements[^?]*\\?\\s*(.+?)\\s*(?:Show more|Show less|Critics reviews|$)', text, re.DOTALL)
        if not match:
            return None
        body = re.sub('\\s+', ' ', match.group(1)).strip()
        body = re.sub('\\s*Recommended:', '\n\nRecommended:', body)
        body = re.sub('\\s*Minimum:', 'Minimum:', body)
        return body or None

    def _extract_api_fields(self, data: dict) -> dict:
        out: dict = {}
        try:
            links = data.get('_links') or {}
            embedded = data.get('_embedded') or {}
            product = embedded.get('product') or {}

            def _href(container: dict, key: str) -> Optional[str]:
                value = (container or {}).get(key)
                return value.get('href') if isinstance(value, dict) else None

            description = data.get('description')
            if isinstance(description, str):
                description = re.sub('<[^>]+>', ' ', description)
                description = re.sub('\\s+', ' ', description).strip()
            out['description'] = description
            out['cover_url'] = _href(links, 'boxArtImage')
            out['logo_url'] = _href(links, 'logo')
            out['background_url'] = _href(links, 'backgroundImage')
            out['icon_url'] = _href(links, 'icon')

            urls = []
            for shot in embedded.get('screenshots') or []:
                self_link = ((shot or {}).get('_links') or {}).get('self') or {}
                url = self_link.get('href')
                if url:
                    urls.append(url.replace('{formatter}', 'product_card_v2_mobile_slider_639'))
            out['screenshot_urls'] = urls
            out['title'] = product.get('title')
        except Exception as exc:
            logger.warning('Failed to parse GOG API payload: %s', exc)
        return out

    # ---- entry construction ----

    def _entry_from_game(self, game: dict, enriched: Optional[dict] = None) -> RepackEntry:
        enriched = enriched or {}
        slug = game.get('slug') or ''
        title = game.get('title') or enriched.get('title') or slug
        poster_url = enriched.get('cover_url') or enriched.get('logo_url')
        return RepackEntry(
            source=self.key,
            title=title,
            url=game.get('pageUrl') or f'https://gog-rev.com/games/{slug}/',
            poster_url=poster_url,
            slug=slug,
            extra={
                'productId': game.get('productId'),
                'currentVersion': game.get('currentVersion'),
                'size': game.get('size'),
                'platforms': game.get('platforms') or {},
                'hasTorrent': True,
                'md5Url': game.get('md5Url'),
            },
        )

    def _enrich_many(self, games: list[dict], use_cache: bool) -> list[tuple[dict, dict]]:
        def _fetch_one(game: dict):
            product_id = game.get('productId')
            enriched = {}
            if product_id:
                api_data = self._load_gog_api(product_id, use_cache=use_cache)
                if api_data:
                    enriched = self._extract_api_fields(api_data)
            return (game, enriched)

        if not games:
            return []
        futures = [_executor.submit(_fetch_one, game) for game in games]
        results: list[tuple[dict, dict]] = []
        for future in futures:
            try:
                results.append(future.result())
            except Exception as exc:
                logger.warning('Failed to enrich GOG-Revived entry: %s', exc)
        return results

    def _entries_for_slugs(self, slugs: list[str], use_cache: bool) -> list[RepackEntry]:
        games = self._load_bcu_games(slugs, use_cache)
        results = self._enrich_many(games, use_cache)
        return [self._entry_from_game(game, enriched) for game, enriched in results]

    # ---- RepackSource interface ----

    def fetch_page(self, page: int, use_cache: bool = True) -> RepackPage:
        if use_cache:
            cached = load_page(self.key, page, ttl_seconds=self._ttl_seconds)
            if cached is not None:
                entries = [RepackEntry.from_dict(e) for e in cached['entries']]
                return RepackPage(entries=entries, page=page, has_more=cached['has_more'])
        slugs = self._load_slugs(use_cache=use_cache)
        start = (page - 1) * _PAGE_SIZE
        page_slugs = slugs[start:start + _PAGE_SIZE]
        entries = self._entries_for_slugs(page_slugs, use_cache)
        has_more = start + _PAGE_SIZE < len(slugs)
        save_page(self.key, page, [e.to_dict() for e in entries], has_more)
        return RepackPage(entries=entries, page=page, has_more=has_more)

    def fetch_details(self, entry: RepackEntry, use_cache: bool = True) -> RepackDetails:
        slug = entry.slug or ''
        cache_key = f'details:{slug}'
        if use_cache:
            cached = load_details(self.key, cache_key, ttl_seconds=self._ttl_seconds)
            if cached is not None:
                return RepackDetails.from_dict(cached)

        game = self._load_bcu_game(slug, use_cache=use_cache) or {}
        product_id = game.get('productId') or (entry.extra or {}).get('productId')
        api_data = self._load_gog_api(product_id, use_cache=use_cache) if product_id else None
        enriched = self._extract_api_fields(api_data) if api_data else {}
        store_page_text = self._load_store_page_text(slug, use_cache=use_cache)
        rating = self._extract_rating_from_page(store_page_text)
        system_requirements = self._extract_system_requirements_from_page(store_page_text)

        platforms = game.get('platforms') or {}
        platform_names = [name.capitalize() for name, supported in platforms.items() if supported]
        extra = {
            'magnet_url': game.get('magnet'),
            'repack_size': game.get('size') or '',
            'platforms': ', '.join(platform_names),
            'current_version': game.get('currentVersion'),
            'has_torrent': bool(game.get('magnet')),
            'rating': rating,
            'system_requirements': system_requirements,
        }
        extra = {k: v for k, v in extra.items() if v not in (None, '', False)}
        if enriched.get('screenshot_urls'):
            extra['screenshot_urls'] = enriched['screenshot_urls']

        details = RepackDetails(
            source=self.key,
            url=game.get('pageUrl') or entry.url,
            title=game.get('title') or enriched.get('title') or entry.title,
            cover_url=enriched.get('cover_url') or enriched.get('logo_url') or enriched.get('background_url') or entry.poster_url,
            cover_path=None,
            description=enriched.get('description') or '',
            size_info=game.get('size'),
            extra=extra,
        )
        save_details(self.key, cache_key, details.to_dict())
        return details

    def search(self, query: str, page: int = 1, use_cache: bool = True) -> RepackPage:
        query = (query or '').strip().lower()
        if not query:
            return RepackPage(entries=[], page=page, has_more=False)
        query_tokens = re.split('\\s+', query)
        slugs = self._load_slugs(use_cache=use_cache)
        matching = [s for s in slugs if _slug_matches_query(s, query_tokens)]
        start = (page - 1) * _PAGE_SIZE
        page_slugs = matching[start:start + _PAGE_SIZE]
        entries = self._entries_for_slugs(page_slugs, use_cache)
        has_more = start + _PAGE_SIZE < len(matching)
        return RepackPage(entries=entries, page=page, has_more=has_more)

    def fetch_upcoming_repacks(self, use_cache: bool = True) -> RepackDetails | None:
        return None

    def fetch_popular_repacks(self, use_cache: bool = True) -> list[RepackEntry]:
        return []

    def fetch_latest_repacks(self, use_cache: bool = True) -> list[RepackEntry]:
        cache_key = '__latest_repacks__'
        if use_cache:
            cached = load_page(self.key, cache_key, ttl_seconds=self._ttl_seconds)
            if cached is not None:
                return [RepackEntry.from_dict(e) for e in cached['entries']]
        slugs = self._load_slugs(use_cache=use_cache)
        recent_slugs = slugs[:12] if slugs else []
        entries = self._entries_for_slugs(recent_slugs, use_cache)
        save_page(self.key, cache_key, [e.to_dict() for e in entries], False)
        return entries
