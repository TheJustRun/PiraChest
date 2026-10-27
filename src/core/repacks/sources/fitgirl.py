from __future__ import annotations
import copy
import html as _html
import logging
import re
import threading
import time
from bs4 import BeautifulSoup
from ..base import (
    RepackEntry,
    RepackPage,
    RepackDetails,
    RepackSource,
    magnet_display_name,
    DEFAULT_TTL_SECONDS,
    fetch_page_image,
    http_get,
    submit_ctx,
    load_page,
    save_page,
    load_details,
    save_details,
)
import importlib.util
logger = logging.getLogger(__name__)
_PARSER = 'lxml' if importlib.util.find_spec('lxml') else 'html.parser'
from concurrent.futures import ThreadPoolExecutor
_POSTER_EXECUTOR = ThreadPoolExecutor(max_workers=4, thread_name_prefix='fitgirl-posters')


def shutdown_executor(wait: bool = True) -> None:
    try:
        _POSTER_EXECUTOR.shutdown(wait=wait, cancel_futures=True)
    except Exception:
        logger.warning('Failed to shut down FitGirl poster executor', exc_info=True)
_BASE_URL = 'https://fitgirl-repacks.site'
_API_POSTS_URL = f'{_BASE_URL}/wp-json/wp/v2/posts'
_PAGE_URL_TMPL = f'{_BASE_URL}/page/{{page}}/'
_PER_PAGE = 100
_SLUG_RE = re.compile('/([a-z0-9\\-]+)/?$')
_WP_SIZE_SUFFIX_RE = re.compile('-(\\d+)x(\\d+)(\\.\\w+)$')

_get = http_get

def _upgrade_to_original(url: str) -> str:
    match = _WP_SIZE_SUFFIX_RE.search(url)
    if not match:
        return url
    return _WP_SIZE_SUFFIX_RE.sub(match.group(3), url)
_EXCLUDED_TITLES = {'upcoming repacks'}
_EXCLUDED_PREFIXES = ('updates digest',)
_EXCLUDED_SUFFIXES = ('repack updated',)

def _is_excluded_post_title(title: str) -> bool:
    lowered = title.strip().lower()
    if lowered in _EXCLUDED_TITLES:
        return True
    if any(lowered.startswith(p) for p in _EXCLUDED_PREFIXES):
        return True
    if any(lowered.endswith(s) for s in _EXCLUDED_SUFFIXES):
        return True
    return False

def _slug_from_url(url: str) -> str:
    match = _SLUG_RE.search(url.rstrip('/') + '/')
    return match.group(1) if match else url

_SEARCH_NORMALIZE_RE = re.compile(r"[^a-z0-9]+")
_IMG_EXT_RE = re.compile(r'\.(jpg|jpeg|png|webp|gif)(\?|$)', re.IGNORECASE)
_BG_URL_RE = re.compile('url\\(["\\\']?(.*?)["\\\']?\\)')
_ARROW_PREFIX_RE = re.compile(r'^[\u21E2\u2192\u2013\u2014\-•]\s*')
_UPCOMING_SKIP_SUBSTRINGS = ('more switch', 'more ps3', 'do not ask', 'never serve', 'latest repacks')
_GREEN_COLOR_RE = re.compile(r'#339966', re.IGNORECASE)
_TITLE_ENTITY_TAIL_RE = re.compile('&#8211;.*$')
_TITLE_BRACKET_TAIL_RE = re.compile(r'\s*\[[^\]]*\]\s*$')
_TITLE_REPACK_TAIL_RE = re.compile(r'\s*[-–]\s*(RePack|Repack).*$', re.IGNORECASE)
_TITLE_PLUS_RE = re.compile(r'\s+\+\s*')
_TITLE_PAREN_JUNK_RE = re.compile(
    r'\s*\((?=[^)]*(?:\d|multi\d*|dlc|build|update|hotfix|patch|repack))[^)]*\)\s*$', re.IGNORECASE)
_TITLE_DASH_TAIL_RE = re.compile(
    r'\s*[-–—]\s*(?:v[\d][\w.\-]*|build\s+[\w.]+|update(?:\s+[\w.]+)?|'
    r'hotfix\s+[\w.]+|patch\s+[\w.]+|\d[\d.,]*.*)\s*$', re.IGNORECASE)
def _page_key(page: int) -> str:
    return f'p{_PER_PAGE}-{page}'
_HOMEPAGE_TTL_S = 30.0
_POPULAR_KEY, _LATEST_KEY, _UPCOMING_KEY = '__popular_repacks__', '__latest_repacks__', '__upcoming_repacks__'

_STOP_HEADINGS = ('download mirrors', 'download mirror', 'screenshots', 'system requirements', 'changelog', 'torrent', 'magnet', 'direct links')
_REPACK_FEATURE_HEADINGS = ('repack features', 'backwards compatibility', 'installation notes', 'included dlcs', 'included dlc')
_GAME_DESCRIPTION_HEADINGS = ('game features', 'game description')
_DISCARD_HEADINGS = ('having issues with my launcher', 'what is a hypervisor bypass', 'hypervisor bypass', 'initialization error')
_STOP_MARKER_RE = re.compile('(' + '|'.join(re.escape(m) for m in _STOP_HEADINGS) + ')', re.IGNORECASE)
_LEFTOVER_LABEL_RE = re.compile(
    r'^(?:genres?/?tags?|companies|company|languages?|original size|repack size|final size)\s*:',
    re.IGNORECASE,
)
_DISCUSSION_LINE_RE = re.compile('discussion\\s+and\\s*\\(?possible\\)?\\s*future\\s+updates.*thread', re.IGNORECASE)
_BACKWARDS_COMPAT_LINE_RE = re.compile(
    r'this repack (?:is|is not|isn.t)(?:\s+\w+){0,2}\s+backwards compatible',
    re.IGNORECASE,
)
_BARE_SIZE_FRAGMENT_RE = re.compile(
    r'^(?:from\s+)?\d+\.?\d*\s*(?:/\s*\d+\.?\d*\s*)?(?:GB|MB|TB)\s*(?:\[\s*Selective Download\s*\])?$',
    re.IGNORECASE,
)

def _strip_repeatedly(rx, text: str) -> str:
    while True:
        new = rx.sub('', text)
        if new == text:
            return text
        text = new


def _normalize_for_search(text: str) -> str:
    return _SEARCH_NORMALIZE_RE.sub(' ', text.lower()).strip()

def _title_match_score(query: str, title: str) -> int | None:
    normalized_query = _normalize_for_search(query)
    if not normalized_query:
        return None
    normalized_title = _normalize_for_search(title)
    if not normalized_title:
        return None

    if normalized_query not in normalized_title:
        return None

    if normalized_title == normalized_query:
        return 100
    if normalized_title.startswith(normalized_query):
        return 90
    return 70

_QUOTE_RE = re.compile(r"['\u2018\u2019\u201c\u201d]")

def _search_param(query: str) -> str:
    return _QUOTE_RE.sub(' ', query).strip()

def _extract_download_links(soup) -> dict:
    sources: dict[str, dict] = {}
    magnet_url = None
    torrent_url = None
    download_links: list[dict] = []

    list_items = soup.find_all('li')
    for li in list_items:
        links = li.find_all('a', href=True)
        if not links:
            continue
        source_name = None
        source_page_url = None
        li_magnet = None
        li_torrent = None
        li_direct: list[dict] = []
        for a in links:
            raw_href = (a.get('href') or '').strip()
            if not raw_href:
                continue
            href = raw_href
            low = href.lower()
            label = a.get_text(strip=True)
            low_label = label.lower()
            if low.startswith('magnet:'):
                if li_magnet is None:
                    li_magnet = href
                continue


            if '.torrent' in low.split('?')[0] or 'torrent file only' in low_label:
                if li_torrent is None:
                    li_torrent = href
                continue

            if low.startswith(('http://', 'https://')) and low_label.endswith(('.rar', '.zip', '.7z')):
                li_direct.append({'url': href, 'label': label, 'host': ''})
                continue
            if source_name is None and label and len(label) <= 30:
                source_name = label
                source_page_url = href
        if source_name and (li_magnet or li_torrent):
            key = source_name.strip().lower()
            if key not in sources:
                sources[key] = {
                    'name': source_name.strip(),
                    'page_url': source_page_url,
                    'magnet_url': li_magnet,
                    'torrent_url': li_torrent,
                }
            if magnet_url is None and li_magnet:
                magnet_url = li_magnet
            if torrent_url is None and li_torrent:
                torrent_url = li_torrent
        elif li_direct:
            host = (source_name or '').strip()
            for d in li_direct:
                d['host'] = host
            download_links.extend(li_direct)

    if not sources:
        for a in soup.find_all('a', href=True):
            raw_href = (a.get('href') or '').strip()
            if not raw_href:
                continue
            low = raw_href.lower()
            if not magnet_url and low.startswith('magnet:'):
                magnet_url = raw_href
            elif not torrent_url and '.torrent' in low.split('?')[0]:
                torrent_url = raw_href
            if magnet_url and torrent_url:
                break

    if sources:
        logger.info(
            'Parsed %d download source(s): %s',
            len(sources),
            ', '.join(
                f"{data['name']}(magnet={'yes' if data.get('magnet_url') else 'no'}, "
                f"dn={magnet_display_name(data.get('magnet_url'))!r})"
                for data in sources.values()
            ),
        )
    elif magnet_url or torrent_url or download_links:
        logger.info('Parsed fallback links (magnet=%s, torrent=%s, direct=%d)',
                    bool(magnet_url), bool(torrent_url), len(download_links))
    else:
        logger.debug('No download links in post body (likely announcement)')

    result: dict = {}
    if magnet_url:
        result['magnet_url'] = magnet_url
    if torrent_url:
        result['torrent_url'] = torrent_url
    if sources:
        result['download_sources'] = sources
    if download_links:
        result['download_links'] = download_links[:12]
    return result

def _extract_game_updates(soup) -> str | None:
    heading = None
    for h in soup.find_all(['h3', 'h2', 'h4']):
        text = h.get_text(' ', strip=True).lower()
        if 'game update' in text:
            heading = h
            break
    if heading is None:
        return None

    collected = []
    for sibling in heading.find_next_siblings():
        if sibling.name in ('h1', 'h2', 'h3', 'h4'):
            break
        collected.append(sibling)
    if not collected:
        return None

    wrapper = soup.new_tag('div')
    for el in collected:
        wrapper.append(copy.copy(el))

    for tag in wrapper.find_all(True):
        for attr in ('style', 'class', 'id', 'target', 'rel'):
            if attr in tag.attrs:
                del tag.attrs[attr]
        if tag.name == 'a' and not tag.get('href'):
            tag.unwrap()
        if tag.name == 'img':
            tag.decompose()

    if not wrapper.get_text(strip=True):
        return None
    return str(wrapper)


class FitGirlSource(RepackSource):
    key = 'fitgirl'
    display_name = 'FitGirl Repacks'

    def __init__(self, ttl_seconds: int=DEFAULT_TTL_SECONDS):
        self._ttl_seconds = ttl_seconds
        self._home_lock = threading.Lock()
        self._home_memo: tuple[float, dict] | None = None

    def _homepage(self) -> dict:
        with self._home_lock:
            memo = self._home_memo
            if memo is not None and time.monotonic() - memo[0] < _HOMEPAGE_TTL_S:
                return memo[1]
            resp = _get(_BASE_URL + '/', timeout=8)
            if resp.status_code != 200:
                raise RuntimeError(f'homepage request returned status {resp.status_code}')
            soup = BeautifulSoup(resp.text, _PARSER)
            data = {'popular': self._parse_popular(soup), 'latest': self._parse_latest(soup),
                    'upcoming': self._parse_upcoming(soup)}
            del soup
            save_page(self.key, _POPULAR_KEY, [e.to_dict() for e in data['popular']], False)
            save_page(self.key, _LATEST_KEY, [e.to_dict() for e in data['latest']], False)
            if data['upcoming'] is not None:
                save_details(self.key, _UPCOMING_KEY, data['upcoming'].to_dict())
            self._home_memo = (time.monotonic(), data)
            return data

    def _homepage_entries(self, which: str, cache_key: str, use_cache: bool) -> list[RepackEntry]:
        if use_cache:
            cached = load_page(self.key, cache_key, ttl_seconds=self._ttl_seconds)
            if cached is not None:
                return [RepackEntry.from_dict(e) for e in cached['entries']]
        try:
            return self._homepage()[which]
        except Exception as exc:
            logger.warning('FitGirl %s-repacks request failed: %s', which, exc)
            return []

    def fetch_popular_repacks(self, use_cache: bool=True) -> list[RepackEntry]:
        return self._homepage_entries('popular', _POPULAR_KEY, use_cache)

    def fetch_latest_repacks(self, use_cache: bool=True) -> list[RepackEntry]:
        return self._homepage_entries('latest', _LATEST_KEY, use_cache)

    def fetch_upcoming_repacks(self, use_cache: bool=True) -> RepackDetails | None:
        if use_cache:
            cached = load_details(self.key, _UPCOMING_KEY, ttl_seconds=self._ttl_seconds)
            if cached is not None:
                return RepackDetails.from_dict(cached)
        try:
            return self._homepage()['upcoming']
        except Exception as exc:
            logger.warning('FitGirl upcoming-repacks request failed: %s', exc)
            return None

    def fetch_page(self, page: int, use_cache: bool=True) -> RepackPage:
        cache_key = _page_key(page)
        if use_cache:
            cached = load_page(self.key, cache_key, ttl_seconds=self._ttl_seconds)
            if cached is not None:
                entries = [RepackEntry.from_dict(e) for e in cached['entries']]
                if self._resolve_page_posters(entries):
                    save_page(self.key, cache_key, [e.to_dict() for e in entries], cached['has_more'])
                return RepackPage(entries=entries, page=page, has_more=cached['has_more'])
        entries, has_more = self._fetch_via_api(page)
        if entries is None:
            entries, has_more = self._fetch_via_html(page)
        if entries is None:
            raise RuntimeError(f'FitGirl page {page} temporarily unavailable')
        self._resolve_page_posters(entries)
        save_page(self.key, cache_key, [e.to_dict() for e in entries], has_more)
        return RepackPage(entries=entries, page=page, has_more=has_more)

    def _resolve_page_posters(self, entries: list[RepackEntry]) -> bool:
        todo = [e for e in entries if not e.poster_url and e.url and not e.extra.get('poster_tried')]
        if not todo:
            return False
        before = [(bool(e.poster_url), bool(e.extra.get('poster_tried'))) for e in todo]
        self._resolve_missing_posters(todo, timeout=5, deadline=5)
        return before != [(bool(e.poster_url), bool(e.extra.get('poster_tried'))) for e in todo]

    def _parse_popular(self, soup) -> list[RepackEntry]:
        widget = soup.select_one('.jetpack_top_posts_widget')
        if widget is None:
            return []
        entries: list[RepackEntry] = []
        seen_urls: set[str] = set()
        for block in widget.select('.widget-grid-view-image'):
            a = block.find('a', href=True)
            if a is None:
                continue
            url = a['href'].strip()
            if not url or url in seen_urls:
                continue
            title = self._clean_title(a.get('title', '').strip()) or a.get_text(strip=True)
            if not title or _is_excluded_post_title(title):
                continue
            img = a.find('img')
            poster_url = None
            if img is not None:
                poster_url = img.get('data-src') or img.get('data-lazy-src') or img.get('src')
                if poster_url:
                    poster_url = _upgrade_to_original(poster_url)
            seen_urls.add(url)
            slug = _slug_from_url(url)
            entries.append(RepackEntry(source=self.key, title=title, url=url, poster_url=poster_url, slug=slug))
        return entries

    def _parse_latest(self, soup) -> list[RepackEntry]:
        widget = soup.select_one('.wplp_outside') or soup.select_one('[class*="wplp_widget"]')
        if widget is None:
            return []
        entries: list[RepackEntry] = []
        seen_urls: set[str] = set()
        bg_url_re = _BG_URL_RE
        for item in widget.select('.wplp-box-item'):
            a = item.find('a', href=True)
            if a is None:
                continue
            url = a['href'].strip()
            if not url or url in seen_urls:
                continue
            title = self._clean_title(a.get('title', '').strip())
            if not title:
                title = self._clean_title(_slug_from_url(url).replace('-', ' ').title())
            poster_url = None
            img = a.find('img') or item.find('img')
            if img is not None:
                poster_url = img.get('data-src') or img.get('data-lazy-src') or img.get('src')
            if not poster_url:
                style_holder = a if a.get('style') else item.select_one('[style*="background"]')
                if style_holder is not None:
                    match = bg_url_re.search(style_holder.get('style', ''))
                    if match:
                        poster_url = match.group(1)
            if poster_url:
                poster_url = _upgrade_to_original(poster_url)
            seen_urls.add(url)
            slug = _slug_from_url(url)
            if _is_excluded_post_title(title):
                continue
            entries.append(RepackEntry(source=self.key, title=title, url=url, poster_url=poster_url, slug=slug))
        return entries

    def _parse_upcoming(self, soup) -> RepackDetails | None:
        for article in soup.select('article'):
            link_tag = article.select_one('h1.entry-title a, h2.entry-title a')
            if link_tag is None:
                continue
            title_text = link_tag.get_text(strip=True)
            if title_text.strip().lower() not in _EXCLUDED_TITLES:
                continue
            title = self._clean_title(title_text)
            url = link_tag.get('href', _BASE_URL)
            content_el = article.select_one('.entry-content, .entry-summary')
            content_html = str(content_el) if content_el is not None else ''
            titles = self._extract_upcoming_titles(content_html)
            return RepackDetails(source=self.key, url=url, title=title, cover_url=None, description='', extra={'upcoming_titles': titles})
        return None

    @staticmethod
    def _extract_upcoming_titles(content_html: str) -> list[str]:
        if not content_html:
            return []
        soup = BeautifulSoup(content_html, _PARSER)

        titles: list[str] = []
        seen: set[str] = set()

        def _add(text: str) -> None:
            cleaned = text.strip()
            cleaned = _ARROW_PREFIX_RE.sub('', cleaned).strip()
            if not cleaned:
                return
            if cleaned.startswith('#'):
                return
            lowered = cleaned.lower()
            if any(marker in lowered for marker in _UPCOMING_SKIP_SUBSTRINGS):
                return
            if lowered in seen:
                return
            seen.add(lowered)
            titles.append(cleaned)

        green_spans = soup.find_all('span', style=_GREEN_COLOR_RE)
        for span in green_spans:
            _add(span.get_text(' ', strip=True))

        if titles:
            return titles

        for br in soup.find_all('br'):
            br.replace_with('\n')
        full_text = soup.get_text('\n')
        for line in full_text.split('\n'):
            line = line.strip()
            if _ARROW_PREFIX_RE.match(line):
                _add(line)

        return titles

    def search(self, query: str, page: int=1, use_cache: bool=True) -> RepackPage:
        query = (query or '').strip()
        if not query:
            return RepackPage(entries=[], page=page, has_more=False)


        _status, posts, total_pages = self._api_posts({
            'search': _search_param(query), 'search_columns[]': 'post_title',
            'page': page, 'per_page': _PER_PAGE})
        if posts is None:
            raise RuntimeError('FitGirl search is unavailable')
        scored: list[tuple[int, RepackEntry]] = []
        for entry in self._posts_to_entries(posts):
            score = _title_match_score(query, entry.title)
            if score is not None:
                scored.append((score, entry))
        if not scored and page == 1:
            return RepackPage(entries=self._search_cached_catalog(query), page=page, has_more=False)
        scored.sort(key=lambda pair: pair[0], reverse=True)
        return RepackPage(entries=[entry for _, entry in scored], page=page,
                          has_more=total_pages is not None and page < total_pages)

    @staticmethod
    def _resolve_missing_posters(entries: list[RepackEntry], timeout: int = 4, max_bytes: int = 131072, deadline: float = 5) -> None:
        from concurrent.futures import wait
        targets = [e for e in entries if not e.poster_url and e.url]
        if not targets:
            return

        def _one(entry: RepackEntry):
            return entry, fetch_page_image(entry.url, timeout=timeout, max_bytes=max_bytes)

        futures = [submit_ctx(_POSTER_EXECUTOR, _one, e) for e in targets]
        done, not_done = wait(futures, timeout=deadline)
        for f in not_done:
            f.cancel()
        for future in done:
            if future.cancelled():
                continue
            try:
                entry, url = future.result()
            except Exception:
                continue
            entry.extra['poster_tried'] = True
            if url:
                entry.poster_url = _upgrade_to_original(url)

    def _search_cached_catalog(self, query: str, max_pages: int=10) -> list[RepackEntry]:
        scored: list[tuple[int, RepackEntry]] = []
        seen_urls: set[str] = set()
        for page_num in range(1, max_pages + 1):
            cached = load_page(self.key, _page_key(page_num), ttl_seconds=self._ttl_seconds)
            if cached is None:
                break
            for entry_dict in cached.get('entries', []):
                entry = RepackEntry.from_dict(entry_dict)
                if entry.url in seen_urls:
                    continue
                seen_urls.add(entry.url)
                score = _title_match_score(query, entry.title)
                if score is not None:
                    scored.append((score, entry))
            if not cached.get('has_more'):
                break
        scored.sort(key=lambda pair: pair[0], reverse=True)
        return [entry for _, entry in scored]

    def _api_posts(self, params: dict, timeout: int = 10):
        try:
            resp = _get(_API_POSTS_URL, params=params, timeout=timeout)
        except Exception as exc:
            logger.warning('FitGirl API request failed: %s', exc)
            return 0, None, None
        if resp.status_code != 200:
            level = logger.info if resp.status_code == 400 else logger.warning
            level('FitGirl API returned status %s', resp.status_code)
            return resp.status_code, None, None
        try:
            posts = resp.json()
        except ValueError:
            logger.warning('FitGirl API returned invalid JSON')
            return resp.status_code, None, None
        try:
            total_pages = int(resp.headers.get('X-WP-TotalPages') or 0) or None
        except ValueError:
            total_pages = None
        return resp.status_code, posts, total_pages

    def _posts_to_entries(self, posts: list) -> list[RepackEntry]:
        entries: list[RepackEntry] = []
        for post in posts:
            title = ((post.get('title') or {}).get('rendered') or '').strip()
            url = post.get('link', '')
            if not title or not url or _is_excluded_post_title(title):
                continue
            entries.append(RepackEntry(source=self.key, title=self._clean_title(title), url=url,
                                       poster_url=self._extract_featured_image(post),
                                       slug=post.get('slug') or _slug_from_url(url)))
        return entries

    def _fetch_via_api(self, page: int):
        _status, posts, total_pages = self._api_posts({'page': page, 'per_page': _PER_PAGE})
        if posts is None:
            return (None, False)
        return (self._posts_to_entries(posts), total_pages is None or page < total_pages)

    def fetch_details(self, entry: RepackEntry, use_cache: bool=True) -> RepackDetails:
        slug = entry.slug or _slug_from_url(entry.url)
        if use_cache:
            cached = load_details(self.key, slug, ttl_seconds=self._ttl_seconds)
            if cached is not None:
                return RepackDetails.from_dict(cached)
        details = self._fetch_details_via_api(entry, slug)
        if details is None:
            details = self._fetch_details_via_html(entry, slug)
        if details is None:
            return RepackDetails(source=self.key, url=entry.url, title=entry.title, cover_url=entry.poster_url, description='', size_info=None)
        if details.screenshot_urls:
            details.screenshot_urls = self._resolve_screenshot_images(details.screenshot_urls)
        save_details(self.key, slug, details.to_dict())
        return details

    @staticmethod
    def _resolve_screenshot_images(page_urls: list[str], limit: int = 6) -> list[str]:
        from concurrent.futures import wait as _wait
        ordered = list(page_urls[:limit])
        fast: dict[str, str] = {}
        need: list[str] = []
        for candidate in ordered:
            if candidate.startswith('video::'):
                fast[candidate] = candidate
            elif _IMG_EXT_RE.search(candidate):
                fast[candidate] = _upgrade_to_original(candidate)
            else:
                need.append(candidate)

        def _fetch_one(url: str) -> tuple[str, str | None]:
            src = fetch_page_image(url, timeout=5, scan_body=True)
            return (url, _upgrade_to_original(src) if src else None)

        if need:
            futures = {submit_ctx(_POSTER_EXECUTOR, _fetch_one, u): u for u in need}
            done, not_done = _wait(list(futures.keys()), timeout=6)
            for f in not_done:
                f.cancel()
            for f in done:
                try:
                    if f.cancelled():
                        continue
                    url, src = f.result()
                    if src:
                        fast[url] = src
                except Exception:
                    pass
        resolved: list[str] = []
        for candidate in ordered:
            val = fast.get(candidate)
            if val and val not in resolved:
                resolved.append(val)
        return resolved

    def _fetch_details_via_api(self, entry: RepackEntry, slug: str):
        _status, posts, _pages = self._api_posts({'slug': slug})
        if not posts:
            return None
        post = posts[0]
        title = self._clean_title(((post.get('title') or {}).get('rendered') or '').strip()) or entry.title
        cover_url = self._extract_featured_image(post) or entry.poster_url
        return self._build_details(entry, title, cover_url, (post.get('content') or {}).get('rendered', ''))

    def _fetch_details_via_html(self, entry: RepackEntry, slug: str) -> RepackDetails | None:
        try:
            resp = _get(entry.url, timeout=10)
        except Exception as exc:
            logger.warning('FitGirl HTML details request failed: %s', exc)
            return None
        if resp.status_code != 200:
            return None
        soup = BeautifulSoup(resp.text, _PARSER)
        title_tag = soup.select_one('h1.entry-title')
        title = self._clean_title(title_tag.get_text(strip=True)) if title_tag else entry.title
        content_el = soup.select_one('.entry-content')
        cover_url = entry.poster_url
        if content_el is not None:
            img_tag = content_el.select_one('img')
            if img_tag is not None:
                cover_url = img_tag.get('data-lazy-src') or img_tag.get('data-src') or img_tag.get('data-original') or img_tag.get('src') or cover_url
        content_html = str(content_el) if content_el is not None else ''
        return self._build_details(entry, title, cover_url, content_html)

    def _build_details(self, entry: RepackEntry, title: str, cover_url: str | None, content_html: str) -> RepackDetails:
        if not cover_url:
            fallback = RepackEntry(source=self.key, title=title, url=entry.url, poster_url=None)
            self._resolve_missing_posters([fallback], timeout=8)
            cover_url = fallback.poster_url


        base = BeautifulSoup(content_html, _PARSER) if content_html else None
        links = _extract_download_links(base) if base is not None else {}
        game_updates_html = _extract_game_updates(base) if base is not None else None
        description, size_info, metadata = self._parse_description_and_size(base)
        if not metadata and not size_info:
            metadata = dict(metadata)
            metadata['is_announcement'] = True
        metadata.update(links)
        if game_updates_html:
            metadata['game_updates_html'] = game_updates_html
        screenshot_urls = metadata.pop('screenshot_urls', [])
        details = RepackDetails(source=self.key, url=entry.url, title=title, cover_url=cover_url,
                                description=description, size_info=size_info, extra=metadata)
        details.screenshot_urls = screenshot_urls
        return details

    _META_FIELD_LABELS: dict[str, str] = {'genres/tags': 'genres', 'genre/tags': 'genres', 'genres': 'genres', 'tags': 'genres', 'companies': 'company', 'company': 'company', 'languages': 'languages', 'language': 'languages', 'original size': 'original_size', 'repack size': 'repack_size', 'final size': 'repack_size'}
    _META_LABEL_RE = re.compile('(?i)(' + '|'.join(re.escape(lbl) for lbl in _META_FIELD_LABELS) + ')\\s*:\\s*')

    @classmethod
    def _extract_metadata(cls, full_text: str) -> dict:
        matches = list(cls._META_LABEL_RE.finditer(full_text))
        if not matches:
            return {}
        metadata: dict = {}
        for i, m in enumerate(matches):
            key = cls._META_FIELD_LABELS[m.group(1).lower()]
            start = m.end()
            next_label_start = matches[i + 1].start() if i + 1 < len(matches) else None
            value_pattern = cls._VALUE_PATTERNS.get(key)
            value_match = re.match(value_pattern, full_text[start:]) if value_pattern else None
            if value_match is not None:
                end = start + value_match.end()
                if next_label_start is not None:
                    end = min(end, next_label_start)
            elif next_label_start is not None:
                end = next_label_start
            else:
                end = start + 100
            value = full_text[start:end].strip().rstrip('.').strip()
            if key == 'repack_size' and value:
                is_selective = bool(re.search('\\[\\s*Selective Download\\s*\\]', value, re.IGNORECASE))
                value = re.sub('\\[\\s*Selective Download\\s*\\]', '', value, flags=re.IGNORECASE).strip()
                value = re.sub('(?i)^from\\s+', '', value).strip()
                if is_selective:
                    metadata.setdefault('selective_download', True)
            if value and key not in metadata:
                metadata[key] = value
        return metadata
    _VALUE_PATTERNS = {'genres': "(?:(?!\\bREQUIRES\\b)[A-Za-z0-9'\\(\\)\\-/ ])+(?:\\s*,\\s*(?:(?!\\bREQUIRES\\b)[A-Za-z0-9'\\(\\)\\-/ ])+)*", 'company': '[A-Za-z0-9][A-Za-z0-9 &\\-\\.]*(?:\\s*,\\s*[A-Za-z0-9][A-Za-z0-9 &\\-\\.]*)*', 'languages': '[A-Za-z0-9/]+(?:\\s*,\\s*[A-Za-z0-9/]+)*', 'original_size': '[\\d.]+\\s*(?:GB|MB|TB)', 'repack_size': '(?:from\\s+)?[\\d.]+(?:\\s*/\\s*[\\d.]+)?\\s*(?:GB|MB|TB)(?:\\s*\\[\\s*Selective Download\\s*\\])?'}

    _FEATURE_PHRASE_SPLIT_RE = re.compile(
        r'(?=\b(?:Game version|100%\s*Lossless(?:\s*&\s*MD5)?|NOTHING ripped|'
        r'Selective Download feature|Smaller (?:archive )?size|Installation takes|'
        r'After-install integrity check|HDD space after installation|'
        r'Language can be changed|Repack uses|At least \d+ ?GB of free RAM)\b)',
        re.IGNORECASE,
    )

    @staticmethod
    def _split_run_on_feature_item(item_text: str) -> list[str]:
        parts = FitGirlSource._FEATURE_PHRASE_SPLIT_RE.split(item_text)
        parts = [p.strip(' .') for p in parts if p and p.strip(' .')]
        return parts if len(parts) > 1 else [item_text]

    @staticmethod
    def _split_li_on_br(li) -> list[str]:
        from bs4 import NavigableString, Tag
        parts: list[str] = []
        current: list[str] = []
        for node in li.children:
            if isinstance(node, Tag) and node.name == 'br':
                chunk = ''.join(current).strip()
                if chunk:
                    parts.append(chunk)
                current = []
            elif isinstance(node, NavigableString):
                current.append(str(node))
            elif isinstance(node, Tag):
                current.append(node.get_text(' ', strip=False))
        chunk = ''.join(current).strip()
        if chunk:
            parts.append(chunk)
        parts = [re.sub(r'\s+', ' ', p).strip() for p in parts]
        parts = [p for p in parts if p]
        if len(parts) <= 1:
            return [li.get_text(' ', strip=True)]
        return parts

    @classmethod
    def _strip_metadata_line(cls, text: str) -> str:
        remainder = text
        for _ in range(8):
            stripped = cls._strip_one_metadata_chain(remainder)
            if stripped == remainder:
                break
            remainder = stripped
        return remainder

    @classmethod
    def _strip_one_metadata_chain(cls, text: str) -> str:
        matches = list(cls._META_LABEL_RE.finditer(text))
        if not matches:
            return text
        spans: list[tuple[int, int]] = []
        for i, m in enumerate(matches):
            key = cls._META_FIELD_LABELS[m.group(1).lower()]
            value_start = m.end()
            next_label_start = matches[i + 1].start() if i + 1 < len(matches) else None
            value_pattern = cls._VALUE_PATTERNS.get(key)
            value_match = re.match(value_pattern, text[value_start:]) if value_pattern else None
            if value_match is not None:
                value_end = value_start + value_match.end()
                if next_label_start is not None:
                    value_end = min(value_end, next_label_start)
            elif next_label_start is not None:
                value_end = next_label_start
            else:
                value_end = value_start
            spans.append((m.start(), value_end))
        best_run: tuple[int, int] | None = None
        run_start_idx = 0
        for i in range(1, len(spans) + 1):
            gap_ok = i < len(spans) and spans[i][0] - spans[i - 1][1] <= 2
            if not gap_ok:
                run_len = i - run_start_idx
                if run_len >= 1:
                    candidate = (spans[run_start_idx][0], spans[i - 1][1])
                    if best_run is None or candidate[1] - candidate[0] > best_run[1] - best_run[0]:
                        best_run = candidate
                run_start_idx = i
        if best_run is None:
            return text
        chain_start, chain_end = best_run
        remainder = (text[:chain_start] + ' ' + text[chain_end:]).strip()
        remainder = re.sub(r'(?i)\s*\bgame\s+REQUIRES\s+Windows\s+[\d./]+\s*', ' ', remainder).strip()
        remainder = re.sub(r'(?i)^\s*[•\-\*]*\s*\[\s*Selective Download\s*\]\s*', '', remainder).strip()
        remainder = re.sub(r'(?i)^\s*[•\-\*]*\s*(?:from\s+)?[\d.]+\s*(?:GB|MB|TB)\s*(?:\[\s*Selective Download\s*\])?\s*$', '', remainder).strip()
        remainder = re.sub(r'^[•\-\*]+\s*$', '', remainder).strip()
        return remainder

    @staticmethod
    def _parse_description_and_size(soup) -> tuple[str, str | None, dict]:
        if soup is None:
            return ('', None, {})
        for spoiler in soup.find_all('div', class_='su-spoiler'):
            title_el = spoiler.find('div', class_='su-spoiler-title')
            content_el = spoiler.find('div', class_='su-spoiler-content')
            if title_el is None or content_el is None:
                continue
            title_text = title_el.get_text(strip=True).lower()
            if 'game description' not in title_text and 'game features' not in title_text:
                continue
            for nav_string in list(content_el.find_all(string=True, recursive=False)):
                text = str(nav_string).strip()
                if not text:
                    nav_string.extract()
                    continue
                new_p = soup.new_tag('p')
                new_p.string = text
                nav_string.replace_with(new_p)
            heading_tag = soup.new_tag('h3')
            heading_tag.string = 'Game Description'
            spoiler.insert_before(heading_tag)
            for child in list(content_el.children):
                spoiler.insert_before(child.extract())
            spoiler.decompose()
        full_text = soup.get_text('\n')
        metadata = FitGirlSource._extract_metadata(full_text)
        size_info = metadata.pop('repack_size', None)
        if size_info is None:
            size_match = re.search('(Repack Size|Final Size)\\s*:?\\s*([^\\n<]+)', full_text, re.IGNORECASE)
            if size_match:
                size_info = size_match.group(2).strip()
                size_info = re.sub('\\[\\s*Selective Download\\s*\\]', '', size_info, flags=re.IGNORECASE).strip()
                size_info = re.sub('(?i)^from\\s+', '', size_info).strip()
        def _looks_like_stop_heading(text: str) -> bool:
            lowered = text.lower().strip()
            if len(lowered) <= 40:
                return any((marker in lowered for marker in _STOP_HEADINGS))
            lead = lowered[:60]
            return any(lead.startswith(marker) for marker in _STOP_HEADINGS)

        def _split_at_stop_marker(text: str) -> tuple[str, bool]:
            match = _STOP_MARKER_RE.search(text)
            if match is None:
                return (text, False)
            salvaged = text[:match.start()].strip()
            if _LEFTOVER_LABEL_RE.match(salvaged):
                salvaged = ''
            return (salvaged, True)

        def _classify_heading(text: str) -> str | None:
            stripped = text.strip().rstrip(':').strip().lower()
            if stripped in _REPACK_FEATURE_HEADINGS:
                return 'repack'
            if stripped in _GAME_DESCRIPTION_HEADINGS:
                return 'game_merge'
            if any(marker in stripped for marker in _DISCARD_HEADINGS):
                return 'discard'
            return None

        def _looks_like_pseudo_heading(text: str) -> tuple[str, str] | None:
            stripped = text.strip().rstrip(':').strip()
            if len(stripped) > 40:
                return None
            kind = _classify_heading(stripped)
            if kind is not None:
                return (kind, stripped)
            return None

        def _looks_like_discussion_link_line(text: str) -> bool:
            return bool(_DISCUSSION_LINE_RE.search(text))

        def _looks_like_backwards_compat_line(text: str) -> bool:
            return bool(_BACKWARDS_COMPAT_LINE_RE.search(text))
        game_sections: list[tuple[str, list[str]]] = [('', [])]
        repack_sections: list[tuple[str, list[str]]] = []
        screenshot_urls: list[str] = []
        current_stream = 'game'
        discard_current = False
        in_screenshots_section = False
        last_repack_heading = ''
        _active_repack_idx = [-1]
        _discard_bucket: list = []

        def _append_target(stream: str) -> list:
            if stream == 'repack':
                idx = _active_repack_idx[0]
                if idx == -2:
                    return _discard_bucket
                if 0 <= idx < len(repack_sections):
                    return repack_sections[idx][1]
                if repack_sections:
                    return repack_sections[-1][1]
                return _discard_bucket
            return game_sections[-1][1]

        for el in soup.find_all(['p', 'h2', 'h3', 'h4', 'ul', 'ol'], recursive=True):
            if el.find_parent(['ul', 'ol']) is not None:
                continue
            if el.name in ('h2', 'h3', 'h4'):
                heading_text = el.get_text(strip=True)
                if not heading_text:
                    continue
                kind = _classify_heading(heading_text)
                if kind == 'repack':
                    normalized_heading = heading_text.strip().rstrip(':').strip().lower()
                    existing_idx = next(
                        (i for i, (h, _) in enumerate(repack_sections) if h.strip().rstrip(':').strip().lower() == normalized_heading),
                        None,
                    )
                    if existing_idx is not None:
                        current_stream = 'repack'
                        last_repack_heading = normalized_heading
                        discard_current = False
                        in_screenshots_section = False
                        _active_repack_idx[0] = -2
                        continue
                    repack_sections.append((heading_text, []))
                    current_stream = 'repack'
                    last_repack_heading = normalized_heading
                    discard_current = False
                    in_screenshots_section = False
                    _active_repack_idx[0] = len(repack_sections) - 1
                    continue
                if kind == 'game_merge':
                    current_stream = 'game'
                    discard_current = False
                    in_screenshots_section = False
                    continue
                if kind == 'discard':
                    discard_current = True
                    in_screenshots_section = False
                    continue
                if 'screenshot' in heading_text.strip().lower():
                    in_screenshots_section = True
                    discard_current = True
                    continue
                if _looks_like_stop_heading(heading_text):
                    discard_current = True
                    in_screenshots_section = False
                    continue
                in_screenshots_section = False
                continue
            if el.name in ('ul', 'ol'):
                in_screenshots_section = False
                if discard_current:
                    continue
                items = []
                for li in el.find_all('li', recursive=False):
                    for part in FitGirlSource._split_li_on_br(li):
                        items.extend(FitGirlSource._split_run_on_feature_item(part))
                items = [FitGirlSource._strip_metadata_line(item) for item in items]
                items = [item for item in items if item and item.strip('•-* ')]
                if not items:
                    continue
                target = _append_target(current_stream)
                target.append('\n'.join((f'• {item}' for item in items)))
                continue
            if el.name == 'p' and in_screenshots_section:
                for a in el.find_all('a', href=True):
                    href = a['href'].strip()
                    if not href:
                        continue
                    video_tag = a.find('video')
                    if video_tag is not None:
                        video_src = video_tag.get('src')
                        if not video_src:
                            source_tag = video_tag.find('source')
                            if source_tag is not None:
                                video_src = source_tag.get('src')
                        if video_src and f'video::{video_src}' not in screenshot_urls:
                            screenshot_urls.append(f'video::{video_src}')
                        continue
                    img_tag = a.find('img')
                    thumb_src = None
                    if img_tag is not None:
                        thumb_src = img_tag.get('data-src') or img_tag.get('data-lazy-src') or img_tag.get('src')
                    href_path = href.split('?')[0].lower()
                    if href_path.endswith(('.jpg', '.jpeg', '.png', '.webp', '.gif')):
                        candidate = href
                    else:
                        candidate = thumb_src or href
                    candidate = _upgrade_to_original(candidate)
                    if candidate and candidate not in screenshot_urls:
                        screenshot_urls.append(candidate)
                for video_tag in el.find_all('video'):
                    video_src = video_tag.get('src')
                    if not video_src:
                        source_tag = video_tag.find('source')
                        if source_tag is not None:
                            video_src = source_tag.get('src')
                    if video_src and f'video::{video_src}' not in screenshot_urls:
                        screenshot_urls.append(f'video::{video_src}')
                in_screenshots_section = False
                continue
            text = el.get_text(strip=True)
            if not text:
                continue
            bold_tags = el.find_all(['b', 'strong'])
            bold_split = False
            if len(bold_tags) >= 2:
                for tag in bold_tags:
                    tag.insert_before('\x00')
                raw = el.get_text(strip=True)
                parts = [p.strip() for p in raw.split('\x00') if p.strip()]
                if len(parts) >= 2:
                    text = '\n'.join((f'• {p}' for p in parts))
                    bold_split = True
            elif len(bold_tags) == 1 and el.find('br') is not None:
                lead = bold_tags[0].get_text(strip=True)
                rest = el.get_text(strip=True)
                if rest.startswith(lead):
                    rest = rest[len(lead):].strip().strip('"').strip()
                if lead and rest:
                    text = f'• {lead} {rest}'
                    bold_split = True
            has_br = el.find('br') is not None
            if has_br and (not bold_split):
                raw_lines = [piece.strip() for piece in el.get_text('\n', strip=True).split('\n') if piece.strip()]
                if len(raw_lines) > 1 and any((ln.startswith(('•', '-', '*')) for ln in raw_lines)):
                    text = '\n'.join(raw_lines)
            text = FitGirlSource._strip_metadata_line(text)
            text = text.replace('\x00', '').strip()
            if not text:
                continue
            pseudo_heading = _looks_like_pseudo_heading(text)
            if pseudo_heading is not None:
                kind, heading_text = pseudo_heading
                if kind == 'repack':
                    normalized_heading = heading_text.strip().rstrip(':').strip().lower()
                    existing_idx = next(
                        (i for i, (h, _) in enumerate(repack_sections) if h.strip().rstrip(':').strip().lower() == normalized_heading),
                        None,
                    )
                    if existing_idx is not None:
                        _active_repack_idx[0] = -2
                    else:
                        repack_sections.append((heading_text, []))
                        _active_repack_idx[0] = len(repack_sections) - 1
                    current_stream = 'repack'
                    last_repack_heading = normalized_heading
                else:
                    current_stream = 'game'
                discard_current = False
                continue
            if _looks_like_stop_heading(text):
                discard_current = True
                continue
            salvaged, hit_stop_marker = _split_at_stop_marker(text)
            if hit_stop_marker:
                if salvaged and not discard_current:
                    _append_target(current_stream).append(salvaged)
                discard_current = True
                continue
            if _looks_like_discussion_link_line(text):
                continue
            if discard_current:
                continue
            if _looks_like_backwards_compat_line(text):
                if last_repack_heading != 'backwards compatibility':
                    existing_idx = next(
                        (i for i, (h, _) in enumerate(repack_sections) if h.strip().rstrip(':').strip().lower() == 'backwards compatibility'),
                        None,
                    )
                    if existing_idx is not None:
                        repack_sections[existing_idx] = ('Backwards Compatibility', [])
                        _active_repack_idx[0] = existing_idx
                    else:
                        repack_sections.append(('Backwards Compatibility', []))
                        _active_repack_idx[0] = len(repack_sections) - 1
                    last_repack_heading = 'backwards compatibility'
                current_stream = 'repack'
                _append_target(current_stream).append(text)
                continue
            if current_stream == 'repack':
                current_stream = 'game'
            _append_target(current_stream).append(text)

        def _strip_bare_size_bullets(body: str) -> str:
            lines = body.split('\n')
            kept = []
            for line in lines:
                candidate = line.strip().lstrip('•-* ').strip()
                if candidate and _BARE_SIZE_FRAGMENT_RE.match(candidate):
                    continue
                kept.append(line)
            return '\n'.join(kept).strip()

        def _render(sections: list[tuple[str, list[str]]]) -> str:
            rendered: list[str] = []
            for heading, blocks in sections:
                body = '\n\n'.join(blocks).strip()
                body = _strip_bare_size_bullets(body)
                body = body.replace('\x00', '').strip()
                if not body:
                    if heading:
                        rendered.append(f'## {heading}\nNo additional details provided.')
                    continue
                if heading:
                    rendered.append(f'## {heading}\n{body}')
                else:
                    rendered.append(body)
            return '\n\n'.join(rendered).strip()
        description = _render(game_sections)
        repack_features = _render(repack_sections)
        if not description:
            fallback_parts = []
            for p in soup.find_all('p'):
                text = p.get_text(strip=True)
                if not text:
                    continue
                text = FitGirlSource._strip_metadata_line(text)
                if not text:
                    continue
                if _looks_like_stop_heading(text) or _looks_like_discussion_link_line(text):
                    continue
                salvaged, hit_stop_marker = _split_at_stop_marker(text)
                if hit_stop_marker:
                    if salvaged:
                        fallback_parts.append(salvaged)
                    break
                fallback_parts.append(text)
            description = '\n\n'.join(fallback_parts).strip()
        if repack_features:
            metadata['repack_features'] = repack_features
        if screenshot_urls:
            metadata['screenshot_urls'] = screenshot_urls
        return (description, size_info, metadata)

    def _extract_featured_image(self, post: dict) -> str | None:
        jetpack_url = post.get('jetpack_featured_media_url')
        if jetpack_url:
            return jetpack_url
        yoast_images = post.get('yoast_head_json', {}).get('og_image') or []
        if yoast_images and yoast_images[0].get('url'):
            return yoast_images[0]['url']
        content_html = post.get('content', {}).get('rendered', '')
        from_body = self._first_image_from_html(content_html)
        if from_body:
            return from_body


        return None

    @staticmethod
    def _first_image_from_html(html: str) -> str | None:
        if not html:
            return None
        for attr in ('data-lazy-src', 'data-src', 'data-original', 'src'):
            match = re.search(rf'<img[^>]+{attr}=["\']([^"\']+)["\']', html)
            if match:
                candidate = match.group(1)
                if candidate and (not candidate.startswith('data:')):
                    return candidate
        return None

    def _fetch_via_html(self, page: int):
        url = _BASE_URL + '/' if page <= 1 else _PAGE_URL_TMPL.format(page=page)
        try:
            resp = _get(url, timeout=10)
        except Exception as exc:
            logger.warning('FitGirl HTML request failed: %s', exc)
            return (None, False)
        if resp.status_code == 404:
            return ([], False)
        if resp.status_code != 200:
            logger.warning('FitGirl HTML page returned status %s', resp.status_code)
            return (None, False)
        soup = BeautifulSoup(resp.text, _PARSER)
        articles = soup.select('article')
        entries: list[RepackEntry] = []
        for article in articles:
            link_tag = article.select_one('h1.entry-title a, h2.entry-title a')
            if link_tag is None:
                continue
            title = self._clean_title(link_tag.get_text(strip=True))
            page_url = link_tag.get('href', '')
            if not title or not page_url:
                continue
            if _is_excluded_post_title(title):
                continue
            img_tag = article.select_one('.entry-content img, .entry-summary img, img')
            poster_url = None
            if img_tag is not None:
                poster_url = img_tag.get('data-src') or img_tag.get('data-lazy-src') or img_tag.get('src')
                if poster_url and poster_url.startswith('data:'):
                    poster_url = None
            entries.append(RepackEntry(source=self.key, title=title, url=page_url, poster_url=poster_url, slug=_slug_from_url(page_url)))
        has_more = soup.select_one('.nav-previous a, a.next, .pagination .next') is not None
        return (entries, has_more)

    @staticmethod
    def _clean_title(raw_title: str) -> str:
        title = _html.unescape(raw_title or '')
        title = _TITLE_ENTITY_TAIL_RE.sub('', title)
        title = _strip_repeatedly(_TITLE_BRACKET_TAIL_RE, title)
        title = _TITLE_REPACK_TAIL_RE.sub('', title)
        title = _TITLE_PLUS_RE.split(title, maxsplit=1)[0]
        title = _strip_repeatedly(_TITLE_PAREN_JUNK_RE, title)
        title = _strip_repeatedly(_TITLE_DASH_TAIL_RE, title)
        return title.strip(' -–—\t,:')
