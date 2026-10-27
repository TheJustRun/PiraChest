from __future__ import annotations
import logging
import re
import time
import threading
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeout, as_completed
from typing import Optional
from urllib.parse import quote

from ..base import (
    RepackEntry,
    RepackPage,
    RepackDetails,
    RepackSource,
    DEFAULT_TTL_SECONDS,
    check_cancelled,
    http_get,
    http_get_json,
    submit_ctx,
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
_REQUEST_TIMEOUT = 7
_MAX_WORKERS = 4

_executor = ThreadPoolExecutor(max_workers=_MAX_WORKERS, thread_name_prefix='gog-fetch')


def shutdown_executor(wait: bool = True) -> None:
    try:
        _executor.shutdown(wait=wait, cancel_futures=True)
    except Exception:
        logger.warning('Failed to shut down GOG fetch executor', exc_info=True)


_NET = threading.BoundedSemaphore(12)


def _get_json(url: str):
    with _NET:
        return http_get_json(url, timeout=_REQUEST_TIMEOUT, headers={'Accept': 'application/json'})


def _slug_matches_query(slug: str, query_tokens: list[str]) -> bool:
    haystack = slug.replace('_', ' ').lower()
    return all(tok in haystack for tok in query_tokens)


def _is_missing(exc: Exception) -> bool:
    return getattr(getattr(exc, 'response', None), 'status_code', None) in (404, 410)


_TAG_RE = re.compile('<[^>]+>')
_WS_RE = re.compile(r'\s+')
_RATING_RE = re.compile(r'(\d\.\d)\s*/\s*5\s*\(\s*([\d,]+)\s*Reviews?\s*\)')
_SYSREQ_RE = re.compile(r'What are the system requirements[^?]*\?\s*(.+?)\s*(?:Show more|Show less|Critics reviews|$)', re.DOTALL)


class GogRevivedSource(RepackSource):
    key = 'gog'
    display_name = 'GOG Revived'

    def __init__(self, ttl_seconds: int = DEFAULT_TTL_SECONDS):
        self._ttl_seconds = ttl_seconds
        self._index_memo: tuple[float, list[str]] | None = None

    def _load_slugs(self, use_cache: bool = True) -> list[str]:
        memo = self._index_memo
        if use_cache and memo is not None and time.monotonic() - memo[0] < _INDEX_TTL_SECONDS:
            return memo[1]
        slugs = None
        if use_cache:
            cached = load_page(self.key, '__slug_index__', ttl_seconds=_INDEX_TTL_SECONDS)
            if cached is not None:
                slugs = cached.get('entries', [{}])[0].get('slugs', [])
        if slugs is None:
            index_data = _get_json(f'{_BCU_BASE}/index.json')
            slugs = list(reversed(index_data.get('games', []) if isinstance(index_data, dict) else index_data))
            save_page(self.key, '__slug_index__', [{'slugs': slugs}], has_more=False)
        self._index_memo = (time.monotonic(), slugs)
        return slugs

    def _fetch_bcu_game(self, slug: str, use_cache: bool = True):
        cache_key = f'bcu_game:{slug}'
        if use_cache:
            cached = load_details(self.key, cache_key, ttl_seconds=self._ttl_seconds)
            if cached is not None:
                return (cached or None), False
        try:
            data = _get_json(f'{_BCU_BASE}/{quote(slug)}.json')
        except Exception as exc:
            check_cancelled()
            if _is_missing(exc):
                logger.info('No BCU torrent entry for slug %s (likely not archived yet)', slug)
                save_details(self.key, cache_key, {})
                return None, False
            logger.info('BCU entry for %s unavailable right now: %s', slug, exc)
            return None, True
        if isinstance(data, dict):
            data.setdefault('slug', slug)
        save_details(self.key, cache_key, data)
        return data, False

    def _load_bcu_games(self, slugs: list[str], use_cache: bool) -> tuple[list[dict], bool]:
        if not slugs:
            return [], True
        futures = {submit_ctx(_executor, self._fetch_bcu_game, slug, use_cache): slug for slug in slugs}
        by_slug, complete = {}, True
        try:
            for future in as_completed(list(futures), timeout=30):
                try:
                    game, transient = future.result(timeout=1)
                    complete = complete and not transient
                    if game:
                        by_slug[futures[future]] = game
                except Exception as exc:
                    complete = False
                    logger.warning('Failed to load BCU entry: %s', exc)
        except FuturesTimeout:
            complete = False
            logger.warning('BCU batch timed out; using %d of %d entries', len(by_slug), len(slugs))
            for future in futures:
                future.cancel()
        return [by_slug[s] for s in slugs if s in by_slug], complete

    def _fetch_gog_api(self, product_id: str, use_cache: bool = True):
        if not product_id:
            return None, False
        cache_key = f'gogapi:{product_id}'
        if use_cache:
            cached = load_details(self.key, cache_key, ttl_seconds=_GOG_API_TTL_SECONDS)
            if cached is not None:
                return (cached or None), False
        try:
            data = _get_json(f'{_GOG_API_BASE}/{product_id}?locale=en-US')
        except Exception as exc:
            check_cancelled()
            logger.info('GOG API enrichment unavailable for product %s: %s', product_id, exc)
            if _is_missing(exc):
                save_details(self.key, cache_key, {})
                return None, False
            return None, True
        save_details(self.key, cache_key, data or {})
        return (data or None), False

    def _load_store_info(self, slug: str, use_cache: bool = True) -> tuple[dict, bool]:
        if not slug:
            return {}, False
        cache_key = f'store_info:{slug}'
        if use_cache:
            cached = load_details(self.key, cache_key, ttl_seconds=_GOG_API_TTL_SECONDS)
            if cached is not None:
                return cached, False
            legacy = load_details(self.key, f'store_page_text:{slug}', ttl_seconds=_GOG_API_TTL_SECONDS)
            if legacy is not None:
                info = self._store_info_from_text(legacy.get('text') or '')
                save_details(self.key, cache_key, info)
                return info, False
        try:
            with _NET:
                resp = http_get(f'https://www.gog.com/en/game/{quote(slug)}', timeout=_REQUEST_TIMEOUT)
            resp.raise_for_status()
            html = resp.text
        except Exception as exc:
            check_cancelled()
            logger.info('GOG store page unavailable for slug %s: %s', slug, exc)
            if _is_missing(exc):
                save_details(self.key, cache_key, {})
                return {}, False
            return {}, True
        info = self._store_info_from_text(_TAG_RE.sub(' ', html).replace('&amp;', '&'))
        save_details(self.key, cache_key, info)
        return info, False

    def _store_info_from_text(self, text: str) -> dict:
        info = {'rating': self._extract_rating_from_page(text),
                'system_requirements': self._extract_system_requirements_from_page(text)}
        return {k: v for k, v in info.items() if v}

    def _extract_rating_from_page(self, text: str) -> Optional[str]:
        match = _RATING_RE.search(text) if text else None
        if not match:
            return None
        return f"{match.group(1)}/5 ({match.group(2).replace(',', '')} reviews)"

    def _extract_system_requirements_from_page(self, text: str) -> Optional[str]:
        match = _SYSREQ_RE.search(text) if text else None
        if not match:
            return None
        body = _WS_RE.sub(' ', match.group(1)).strip()
        body = re.sub(r'\s*Recommended:', '\n\nRecommended:', body)
        body = re.sub(r'\s*Minimum:', 'Minimum:', body)
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

    def _enrich_many(self, games: list[dict], use_cache: bool) -> tuple[list[tuple[dict, dict]], bool]:
        def _fetch_one(game: dict):
            product_id = game.get('productId')
            enriched, transient = {}, False
            if product_id:
                api_data, transient = self._fetch_gog_api(product_id, use_cache=use_cache)
                if api_data:
                    enriched = self._extract_api_fields(api_data)
            return (game, enriched, transient)

        if not games:
            return [], True
        futures = {submit_ctx(_executor, _fetch_one, game): game.get('slug', '') for game in games}
        by_slug: dict[str, tuple[dict, dict]] = {}
        complete = True
        try:
            for future in as_completed(list(futures.keys()), timeout=30):
                try:
                    game, enriched, transient = future.result(timeout=1)
                    complete = complete and not transient
                    by_slug[game.get('slug', '')] = (game, enriched)
                except Exception as exc:
                    complete = False
                    logger.warning('Failed to enrich GOG-Revived entry: %s', exc)
        except FuturesTimeout:
            complete = False
            logger.warning('GOG enrich timed out')
            for future in futures:
                future.cancel()
        return [by_slug.get(g.get('slug', ''), (g, {})) for g in games if g.get('slug', '') in by_slug], complete

    def _entries_for_slugs_ex(self, slugs: list[str], use_cache: bool) -> tuple[list[RepackEntry], bool]:
        games, games_ok = self._load_bcu_games(slugs, use_cache)
        results, enrich_ok = self._enrich_many(games, use_cache)
        return [self._entry_from_game(game, enriched) for game, enriched in results], games_ok and enrich_ok

    def _entries_for_slugs(self, slugs: list[str], use_cache: bool) -> list[RepackEntry]:
        return self._entries_for_slugs_ex(slugs, use_cache)[0]


    def fetch_page(self, page: int, use_cache: bool = True) -> RepackPage:
        if use_cache:
            cached = load_page(self.key, page, ttl_seconds=self._ttl_seconds)
            if cached is not None:
                entries = [RepackEntry.from_dict(e) for e in cached['entries']]
                return RepackPage(entries=entries, page=page, has_more=cached['has_more'])
        slugs = self._load_slugs(use_cache=use_cache)
        start = (page - 1) * _PAGE_SIZE
        page_slugs = slugs[start:start + _PAGE_SIZE]
        entries, complete = self._entries_for_slugs_ex(page_slugs, use_cache)
        has_more = start + _PAGE_SIZE < len(slugs)
        if complete:
            save_page(self.key, page, [e.to_dict() for e in entries], has_more)
        return RepackPage(entries=entries, page=page, has_more=has_more)

    def fetch_details(self, entry: RepackEntry, use_cache: bool = True) -> RepackDetails:
        slug = entry.slug or ''
        if not slug:
            _m = re.search(r'/games/([^/]+)/?$', entry.url or '')
            slug = _m.group(1) if _m else ''
        cache_key = f'details2:{slug or entry.url}'
        if use_cache:
            cached = load_details(self.key, cache_key, ttl_seconds=self._ttl_seconds)
            if cached is not None:
                return RepackDetails.from_dict(cached)

        game, game_transient = self._fetch_bcu_game(slug, use_cache=use_cache)
        game = game or {}
        product_id = game.get('productId') or (entry.extra or {}).get('productId')

        f_api = submit_ctx(_executor, self._fetch_gog_api, product_id, use_cache) if product_id else None
        f_store = submit_ctx(_executor, self._load_store_info, slug, use_cache)
        transient = game_transient
        try:
            api_data, api_transient = f_api.result(timeout=12) if f_api is not None else (None, False)
        except Exception:
            api_data, api_transient = None, True
        try:
            store, store_transient = f_store.result(timeout=12)
        except Exception:
            store, store_transient = {}, True
        transient = transient or api_transient or store_transient
        enriched = self._extract_api_fields(api_data) if api_data else {}
        rating = store.get('rating')
        system_requirements = store.get('system_requirements')

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
        if not transient:
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
        entries, complete = self._entries_for_slugs_ex(recent_slugs, use_cache)
        if complete:
            save_page(self.key, cache_key, [e.to_dict() for e in entries], False)
        return entries
