from __future__ import annotations
import html as _html
import logging
import re
import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
try:
    from urllib3.util.retry import Retry
except ImportError:
    from requests.packages.urllib3.util.retry import Retry
from .. import cache
from ..base import RepackEntry, RepackPage, RepackDetails, RepackSource, magnet_display_name
logger = logging.getLogger(__name__)
_BASE_URL = 'https://fitgirl-repacks.site'
_API_POSTS_URL = f'{_BASE_URL}/wp-json/wp/v2/posts'
_PAGE_URL_TMPL = f'{_BASE_URL}/page/{{page}}/'
_PER_PAGE = 20
_HEADERS = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36', 'Accept': 'application/json, text/html;q=0.9,*/*;q=0.8'}
_SLUG_RE = re.compile('/([a-z0-9\\-]+)/?$')
_WP_SIZE_SUFFIX_RE = re.compile('-(\\d+)x(\\d+)(\\.\\w+)$')

_session = requests.Session()
_retry = Retry(total=2, backoff_factor=0.2, status_forcelist=(502, 503, 504), allowed_methods=('GET',))
_adapter = HTTPAdapter(pool_connections=24, pool_maxsize=24, max_retries=_retry)
_session.mount('https://', _adapter)
_session.mount('http://', _adapter)
_session.headers.update(_HEADERS)

def _get(url: str, **kwargs):
    kwargs.pop('headers', None)
    return _session.get(url, **kwargs)

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

_RIOTPIXELS_P_RE = re.compile('<p[^>]*style="[^"]*height\\s*:\\s*\\d+px[^"]*"[^>]*>(.*?)</p>', re.IGNORECASE | re.DOTALL)
_A_TAG_RE = re.compile('<a\\b([^>]*)>', re.IGNORECASE)
_HREF_RE = re.compile('href=["\\\']([^"\\\']+)["\\\']', re.IGNORECASE)

def _extract_riotpixels_fallback_link(html: str) -> str | None:
    for p_match in _RIOTPIXELS_P_RE.finditer(html):
        inner = p_match.group(1)
        a_match = _A_TAG_RE.search(inner)
        if not a_match:
            continue
        attrs = a_match.group(1)
        if 'noopener' not in attrs or 'target=' not in attrs:
            continue
        href_match = _HREF_RE.search(attrs)
        if href_match and 'riotpixels.com/games/' in href_match.group(1):
            return href_match.group(1)
    return None

def _slug_from_url(url: str) -> str:
    match = _SLUG_RE.search(url.rstrip('/') + '/')
    return match.group(1) if match else url

_SEARCH_NORMALIZE_RE = re.compile(r"[^a-z0-9]+")
_OG_IMAGE_RE = re.compile(r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)["\']', re.IGNORECASE)
_IMG_TAG_RE = re.compile(r'<img[^>]+(?:data-src|src)=["\']([^"\']+)["\']', re.IGNORECASE)
_IMG_EXT_RE = re.compile(r'\.(jpg|jpeg|png|webp|gif)(\?|$)', re.IGNORECASE)
_BG_URL_RE = re.compile('url\\(["\\\']?(.*?)["\\\']?\\)')

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

def _extract_download_links(content_html: str) -> dict:

    if not content_html:
        return {}
    soup = BeautifulSoup(content_html, 'html.parser')

    sources: dict[str, dict] = {}
    magnet_url = None
    torrent_url = None

    list_items = soup.find_all('li')
    for li in list_items:
        links = li.find_all('a', href=True)
        if not links:
            continue
        source_name = None
        source_page_url = None
        li_magnet = None
        li_torrent = None
        for a in links:
            href = a['href'].strip()
            label = a.get_text(strip=True)
            if href.startswith('magnet:'):
                if li_magnet is None:
                    li_magnet = href
                continue
            if href.lower().endswith('.torrent') or 'torrent file only' in label.lower():
                if li_torrent is None:
                    li_torrent = href
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

    if not sources:
        for a in soup.find_all('a', href=True):
            href = a['href'].strip()
            if not magnet_url and href.startswith('magnet:'):
                magnet_url = href
            elif not torrent_url and href.lower().endswith('.torrent'):
                torrent_url = href
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
    else:
        logger.warning('No per-source download links parsed from post body (magnet_url=%s, torrent_url=%s)', bool(magnet_url), bool(torrent_url))

    result: dict = {}
    if magnet_url:
        result['magnet_url'] = magnet_url
    if torrent_url:
        result['torrent_url'] = torrent_url
    if sources:
        result['download_sources'] = sources
    return result

def _extract_game_updates(content_html: str) -> str | None:
    if not content_html:
        return None
    soup = BeautifulSoup(content_html, 'html.parser')
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
        wrapper.append(el.extract())

    for tag in wrapper.find_all(True):
        for attr in ('style', 'class', 'id', 'target', 'rel'):
            if attr in tag.attrs:
                del tag.attrs[attr]
        if tag.name == 'a' and not tag.get('href'):
            tag.unwrap()
        if tag.name == 'img':
            tag.decompose()

    body_text = wrapper.get_text(strip=True)
    if not body_text:
        return None
    return str(wrapper)

class FitGirlSource(RepackSource):
    key = 'fitgirl'
    display_name = 'FitGirl Repacks'

    def __init__(self, ttl_seconds: int=cache.DEFAULT_TTL_SECONDS):
        self._ttl_seconds = ttl_seconds

    def fetch_page(self, page: int, use_cache: bool=True) -> RepackPage:
        if use_cache:
            cached = cache.load_page(self.key, page, ttl_seconds=self._ttl_seconds)
            if cached is not None:
                entries = [RepackEntry.from_dict(e) for e in cached['entries']]
                return RepackPage(entries=entries, page=page, has_more=cached['has_more'])
        entries, has_more = self._fetch_via_api(page)
        if entries is None:
            entries, has_more = self._fetch_via_html(page)
        entries = entries or []
        cache.save_page(self.key, page, [e.to_dict() for e in entries], has_more)
        return RepackPage(entries=entries, page=page, has_more=has_more)

    def fetch_upcoming_repacks(self, use_cache: bool=True) -> RepackDetails | None:
        cache_key = '__upcoming_repacks__'
        if use_cache:
            cached = cache.load_details(self.key, cache_key, ttl_seconds=self._ttl_seconds)
            if cached is not None:
                return RepackDetails.from_dict(cached)
        details = self._fetch_upcoming_repacks_via_html()
        if details is not None:
            cache.save_details(self.key, cache_key, details.to_dict())
        return details

    def fetch_popular_repacks(self, use_cache: bool=True) -> list[RepackEntry]:
        cache_key = '__popular_repacks__'
        if use_cache:
            cached = cache.load_page(self.key, cache_key, ttl_seconds=self._ttl_seconds)
            if cached is not None:
                return [RepackEntry.from_dict(e) for e in cached['entries']]
        entries = self._fetch_popular_repacks_via_html()
        cache.save_page(self.key, cache_key, [e.to_dict() for e in entries], False)
        return entries

    def _fetch_popular_repacks_via_html(self) -> list[RepackEntry]:
        try:
            resp = _get(_BASE_URL, timeout=15)
        except Exception as exc:
            logger.warning('FitGirl popular-repacks request failed: %s', exc)
            return []
        if resp.status_code != 200:
            return []
        try:
            soup = BeautifulSoup(resp.text, 'html.parser')
        except Exception:
            return []
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

    def fetch_latest_repacks(self, use_cache: bool=True) -> list[RepackEntry]:
        cache_key = '__latest_repacks__'
        if use_cache:
            cached = cache.load_page(self.key, cache_key, ttl_seconds=self._ttl_seconds)
            if cached is not None:
                return [RepackEntry.from_dict(e) for e in cached['entries']]
        entries = self._fetch_latest_repacks_via_html()
        cache.save_page(self.key, cache_key, [e.to_dict() for e in entries], False)
        return entries

    def _fetch_latest_repacks_via_html(self) -> list[RepackEntry]:
        try:
            resp = _get(_BASE_URL, timeout=15)
        except Exception as exc:
            logger.warning('FitGirl latest-repacks request failed: %s', exc)
            return []
        if resp.status_code != 200:
            return []
        try:
            soup = BeautifulSoup(resp.text, 'html.parser')
        except Exception:
            return []
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

    def _fetch_upcoming_repacks_via_html(self) -> RepackDetails | None:
        try:
            resp = _get(_BASE_URL + '/', timeout=15)
        except Exception as exc:
            logger.warning('FitGirl upcoming-repacks HTML request failed: %s', exc)
            return None
        if resp.status_code != 200:
            return None
        soup = BeautifulSoup(resp.text, 'html.parser')
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
        soup = BeautifulSoup(content_html, 'html.parser')

        _ARROW_PREFIX_RE = re.compile(r'^[\u21E2\u2192\u2013\u2014\-•]\s*')
        _SKIP_SUBSTRINGS = ('more switch', 'more ps3', 'do not ask', 'never serve', 'latest repacks')
        _GREEN_COLOR_RE = re.compile(r'#339966', re.IGNORECASE)

        titles: list[str] = []
        seen: set[str] = set()

        def _add(text: str) -> None:
            cleaned = text.strip()
            cleaned = _ARROW_PREFIX_RE.sub('', cleaned).strip()
            if not cleaned:
                return
            lowered = cleaned.lower()
            if any(marker in lowered for marker in _SKIP_SUBSTRINGS):
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
        scored_entries: list[tuple[int, RepackEntry]] = []
        pages_to_scan = 3 if page == 1 else 1
        last_has_more = False
        for offset in range(pages_to_scan):
            current_page = page + offset
            params = {'s': query}
            if current_page > 1:
                params['paged'] = current_page
            try:
                resp = _get(_BASE_URL + '/', params=params, timeout=15)
            except Exception as exc:
                logger.warning('FitGirl search request failed: %s', exc)
                if offset == 0:
                    raise
                break
            if resp.status_code != 200:
                logger.warning('FitGirl search page returned status %s for query %r', resp.status_code, query)
                if offset == 0:
                    raise RuntimeError(f'FitGirl search returned HTTP {resp.status_code}')
                break
            soup = BeautifulSoup(resp.text, 'html.parser')
            articles = soup.select('article')
            if not articles:
                break
            for article in articles:
                link_tag = article.select_one('h1.entry-title a, h2.entry-title a')
                if link_tag is None:
                    continue
                title = self._clean_title(link_tag.get_text(strip=True))
                url = link_tag.get('href', '')
                if not title or not url:
                    continue
                if _is_excluded_post_title(title):
                    continue
                score = _title_match_score(query, title)
                if score is None:
                    continue
                entry = RepackEntry(source=self.key, title=title, url=url, poster_url=None, slug=_slug_from_url(url))
                scored_entries.append((score, entry))
            last_has_more = soup.select_one('.nav-previous a, a.next, .pagination .next') is not None
            if not last_has_more:
                break
        seen_urls: set[str] = set()
        deduped: list[tuple[int, RepackEntry]] = []
        for score, entry in scored_entries:
            if entry.url in seen_urls:
                continue
            seen_urls.add(entry.url)
            deduped.append((score, entry))
        deduped.sort(key=lambda pair: pair[0], reverse=True)
        entries = [entry for _, entry in deduped]
        has_more = last_has_more

        if not entries and page == 1:
            entries = self._search_cached_catalog(query)
            has_more = False

        self._resolve_missing_posters(entries[:24], max_workers=12, timeout=6)
        return RepackPage(entries=entries, page=page, has_more=has_more)

    @staticmethod
    def _resolve_missing_posters(entries: list[RepackEntry], max_workers: int = 8, timeout: int = 8, max_bytes: int = 393216, retries: int = 1) -> None:
        from concurrent.futures import ThreadPoolExecutor, as_completed
        targets = [e for e in entries if not e.poster_url and e.url]
        if not targets:
            return

        def _fetch(entry: RepackEntry) -> tuple[str, str | None]:
            for attempt in range(retries + 1):
                try:
                    with _get(entry.url, timeout=timeout, stream=True, allow_redirects=True) as resp:
                        if resp.status_code != 200:
                            logger.warning('Poster fetch for %s returned status %s', entry.url, resp.status_code)
                            continue
                        buffer = b''
                        for chunk in resp.iter_content(chunk_size=8192):
                            buffer += chunk
                            text = buffer.decode('utf-8', errors='ignore')
                            match = _OG_IMAGE_RE.search(text)
                            if match:
                                return (entry.url, _upgrade_to_original(match.group(1)))
                            if '</head>' in text:
                                break
                            if len(buffer) >= max_bytes:
                                break
                        text = buffer.decode('utf-8', errors='ignore')
                        match = _OG_IMAGE_RE.search(text)
                        if match:
                            return (entry.url, _upgrade_to_original(match.group(1)))
                        match = _IMG_TAG_RE.search(text)
                        if match:
                            return (entry.url, _upgrade_to_original(match.group(1)))
                except Exception as exc:
                    logger.warning('Failed to fetch poster for %s (attempt %d): %s', entry.url, attempt, exc)
                    continue
                break
            try:
                full_resp = _get(entry.url, timeout=timeout)
                if full_resp.status_code == 200:
                    riot_url = _extract_riotpixels_fallback_link(full_resp.text)
                    if riot_url:
                        riot_resp = _get(riot_url, timeout=timeout)
                        if riot_resp.status_code == 200:
                            riot_match = _OG_IMAGE_RE.search(riot_resp.text)
                            if riot_match:
                                return (entry.url, riot_match.group(1))
            except Exception as exc:
                logger.warning('Riotpixels fallback poster fetch failed for %s: %s', entry.url, exc)
            return (entry.url, None)

        by_url = {e.url: e for e in targets}
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            futures = [pool.submit(_fetch, entry) for entry in targets]
            for future in as_completed(futures):
                url, poster_url = future.result()
                if poster_url:
                    by_url[url].poster_url = poster_url

    def _search_cached_catalog(self, query: str, max_pages: int=10) -> list[RepackEntry]:
        scored: list[tuple[int, RepackEntry]] = []
        seen_urls: set[str] = set()
        for page_num in range(1, max_pages + 1):
            cached = cache.load_page(self.key, page_num, ttl_seconds=self._ttl_seconds)
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

    def _fetch_via_api(self, page: int):
        params = {'page': page, 'per_page': _PER_PAGE, '_embed': 'wp:featuredmedia'}
        try:
            resp = _get(_API_POSTS_URL, params=params, timeout=15)
        except Exception as exc:
            logger.warning('FitGirl API request failed: %s', exc)
            return (None, False)
        if resp.status_code == 400:
            return ([], False)
        if resp.status_code != 200:
            logger.warning('FitGirl API returned status %s', resp.status_code)
            return (None, False)
        try:
            posts = resp.json()
        except ValueError:
            logger.warning('FitGirl API returned invalid JSON')
            return (None, False)
        total_pages_header = resp.headers.get('X-WP-TotalPages')
        try:
            total_pages = int(total_pages_header) if total_pages_header else None
        except ValueError:
            total_pages = None
        entries: list[RepackEntry] = []
        for post in posts:
            title = post.get('title', {}).get('rendered', '').strip()
            url = post.get('link', '')
            slug = post.get('slug') or _slug_from_url(url)
            poster_url = self._extract_featured_image(post)
            if not title or not url:
                continue
            if _is_excluded_post_title(title):
                continue
            entries.append(RepackEntry(source=self.key, title=self._clean_title(title), url=url, poster_url=poster_url, slug=slug))
        has_more = total_pages is None or page < total_pages
        self._resolve_missing_posters(entries, max_workers=12, timeout=6)
        return (entries, has_more)

    def fetch_details(self, entry: RepackEntry, use_cache: bool=True) -> RepackDetails:
        slug = entry.slug or _slug_from_url(entry.url)
        if use_cache:
            cached = cache.load_details(self.key, slug, ttl_seconds=self._ttl_seconds)
            if cached is not None:
                return RepackDetails.from_dict(cached)
        details = self._fetch_details_via_api(entry, slug)
        if details is None:
            details = self._fetch_details_via_html(entry, slug)
        if details.screenshot_urls:
            details.screenshot_urls = self._resolve_screenshot_images(details.screenshot_urls)
        cache.save_details(self.key, slug, details.to_dict())
        return details

    @staticmethod
    def _resolve_screenshot_images(page_urls: list[str], limit: int = 8) -> list[str]:
        resolved: list[str] = []
        for candidate in page_urls[:limit]:
            if candidate.startswith('video::'):
                if candidate not in resolved:
                    resolved.append(candidate)
                continue
            if _IMG_EXT_RE.search(candidate):
                upgraded = _upgrade_to_original(candidate)
                if upgraded not in resolved:
                    resolved.append(upgraded)
                continue
            try:
                resp = _get(candidate, timeout=8)
            except Exception as exc:
                logger.warning('Failed to fetch screenshot page %s: %s', candidate, exc)
                continue
            if resp.status_code != 200:
                logger.warning('Screenshot page %s returned status %s', candidate, resp.status_code)
                continue
            try:
                soup = BeautifulSoup(resp.text, 'html.parser')
            except Exception:
                continue
            img = (
                soup.select_one('meta[property="og:image"]')
                or soup.select_one('.screenshot img')
                or soup.select_one('#screenshot img')
                or soup.select_one('.image-holder img')
                or soup.select_one('article img')
                or soup.select_one('img')
            )
            if img is None:
                logger.warning('No image found on screenshot page %s', candidate)
                continue
            if img.name == 'meta':
                src = img.get('content')
            else:
                src = img.get('data-src') or img.get('src')
            if src and src not in resolved:
                resolved.append(_upgrade_to_original(src))
        return resolved

    def _fetch_details_via_api(self, entry: RepackEntry, slug: str):
        params = {'slug': slug, '_embed': 'wp:featuredmedia'}
        try:
            resp = _get(_API_POSTS_URL, params=params, timeout=15)
        except Exception as exc:
            logger.warning('FitGirl API details request failed: %s', exc)
            return None
        if resp.status_code != 200:
            return None
        try:
            posts = resp.json()
        except ValueError:
            return None
        if not posts:
            return None
        post = posts[0]
        title = self._clean_title(post.get('title', {}).get('rendered', '').strip()) or entry.title
        content_html = post.get('content', {}).get('rendered', '')
        cover_url = self._extract_featured_image(post) or entry.poster_url
        if not cover_url:
            fallback_entry = RepackEntry(source=self.key, title=title, url=entry.url, poster_url=None)
            self._resolve_missing_posters([fallback_entry], max_workers=1, timeout=8)
            cover_url = fallback_entry.poster_url
        description, size_info, metadata = self._parse_description_and_size(content_html)
        if not metadata and (not size_info):
            metadata = dict(metadata)
            metadata['is_announcement'] = True
        metadata.update(_extract_download_links(content_html))
        game_updates_html = _extract_game_updates(content_html)
        if game_updates_html:
            metadata['game_updates_html'] = game_updates_html
        screenshot_urls = metadata.pop('screenshot_urls', [])
        details = RepackDetails(source=self.key, url=entry.url, title=title, cover_url=cover_url, description=description, size_info=size_info, extra=metadata)
        details.screenshot_urls = screenshot_urls
        return details

    def _fetch_details_via_html(self, entry: RepackEntry, slug: str) -> RepackDetails:
        try:
            resp = _get(entry.url, timeout=15)
        except Exception as exc:
            logger.warning('FitGirl HTML details request failed: %s', exc)
            return RepackDetails(source=self.key, url=entry.url, title=entry.title, cover_url=entry.poster_url, description='', size_info=None)
        if resp.status_code != 200:
            return RepackDetails(source=self.key, url=entry.url, title=entry.title, cover_url=entry.poster_url, description='', size_info=None)
        soup = BeautifulSoup(resp.text, 'html.parser')
        title_tag = soup.select_one('h1.entry-title')
        title = self._clean_title(title_tag.get_text(strip=True)) if title_tag else entry.title
        content_el = soup.select_one('.entry-content')
        content_html = str(content_el) if content_el is not None else ''
        cover_url = entry.poster_url
        if content_el is not None:
            img_tag = content_el.select_one('img')
            if img_tag is not None:
                cover_url = img_tag.get('data-lazy-src') or img_tag.get('data-src') or img_tag.get('data-original') or img_tag.get('src') or cover_url
        if not cover_url:
            fallback_entry = RepackEntry(source=self.key, title=title, url=entry.url, poster_url=None)
            self._resolve_missing_posters([fallback_entry], max_workers=1, timeout=8)
            cover_url = fallback_entry.poster_url
        description, size_info, metadata = self._parse_description_and_size(content_html)
        if not metadata and (not size_info):
            metadata = dict(metadata)
            metadata['is_announcement'] = True
        metadata.update(_extract_download_links(content_html))
        game_updates_html = _extract_game_updates(content_html)
        if game_updates_html:
            metadata['game_updates_html'] = game_updates_html
        screenshot_urls = metadata.pop('screenshot_urls', [])
        details = RepackDetails(source=self.key, url=entry.url, title=title, cover_url=cover_url, description=description, size_info=size_info, extra=metadata)
        details.screenshot_urls = screenshot_urls
        return details

    _META_FIELD_LABELS: dict[str, str] = {'genres/tags': 'genres', 'genre/tags': 'genres', 'genres': 'genres', 'tags': 'genres', 'companies': 'company', 'company': 'company', 'languages': 'languages', 'language': 'languages', 'original size': 'original_size', 'repack size': 'repack_size', 'final size': 'repack_size'}

    @classmethod
    def _extract_metadata(cls, full_text: str) -> dict:
        label_pattern = '|'.join((re.escape(lbl) for lbl in cls._META_FIELD_LABELS))
        matches = list(re.finditer(f'(?i)({label_pattern})\\s*:\\s*', full_text))
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
        label_pattern = '|'.join((re.escape(lbl) for lbl in cls._META_FIELD_LABELS))
        matches = list(re.finditer(f'(?i)({label_pattern})\\s*:\\s*', text))
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
    def _parse_description_and_size(content_html: str) -> tuple[str, str | None, dict]:
        if not content_html:
            return ('', None, {})
        soup = BeautifulSoup(content_html, 'html.parser')
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
        embedded = post.get('_embedded', {})
        media_list = embedded.get('wp:featuredmedia') or []
        if media_list:
            media = media_list[0]
            if 'code' not in media:
                sizes = media.get('media_details', {}).get('sizes', {})
                for size_key in ('medium', 'medium_large', 'full'):
                    if size_key in sizes:
                        return sizes[size_key].get('source_url')
                source_url = media.get('source_url')
                if source_url:
                    return source_url
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
        post_link = post.get('link')
        if post_link:
            return self._fetch_og_image_from_page(post_link)
        return None

    @staticmethod
    def _fetch_og_image_from_page(page_url: str, timeout: int = 8) -> str | None:
        try:
            resp = _get(page_url, timeout=timeout)
        except Exception as exc:
            logger.warning('Failed to fetch og:image fallback for %s: %s', page_url, exc)
            return None
        if resp.status_code != 200:
            return None
        match = _OG_IMAGE_RE.search(resp.text)
        if not match:
            return None
        return _upgrade_to_original(match.group(1))

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
            resp = _get(url, timeout=15)
        except Exception as exc:
            logger.warning('FitGirl HTML request failed: %s', exc)
            return ([], False)
        if resp.status_code == 404:
            return ([], False)
        if resp.status_code != 200:
            logger.warning('FitGirl HTML page returned status %s', resp.status_code)
            return ([], False)
        soup = BeautifulSoup(resp.text, 'html.parser')
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
                poster_url = img_tag.get('data-src') or img_tag.get('src')
            entries.append(RepackEntry(source=self.key, title=title, url=page_url, poster_url=poster_url, slug=_slug_from_url(page_url)))
        has_more = soup.select_one('.nav-previous a, a.next, .pagination .next') is not None
        return (entries, has_more)

    @staticmethod
    def _clean_title(raw_title: str) -> str:
        title = re.sub('&#8211;.*$', '', raw_title)
        title = re.sub('\\s*[-–]\\s*(RePack|Repack).*$', '', title, flags=re.IGNORECASE)
        title = _html.unescape(title)
        return title.strip()
