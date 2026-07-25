from __future__ import annotations
import logging
import re
from ..base import RepackEntry, RepackPage, RepackDetails, RepackSource
from .. import cache
logger = logging.getLogger(__name__)
_BASE_URL = 'https://fitgirl-repacks.site'
_API_POSTS_URL = f'{_BASE_URL}/wp-json/wp/v2/posts'
_PAGE_URL_TMPL = f'{_BASE_URL}/page/{{page}}/'
_PER_PAGE = 20
_HEADERS = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36', 'Accept': 'application/json, text/html;q=0.9,*/*;q=0.8'}
_SLUG_RE = re.compile('/([a-z0-9\\-]+)/?$')
_EXCLUDED_TITLES = {'upcoming repacks'}

def _slug_from_url(url: str) -> str:
    match = _SLUG_RE.search(url.rstrip('/') + '/')
    return match.group(1) if match else url

def _extract_download_links(content_html: str) -> dict:
    from bs4 import BeautifulSoup
    if not content_html:
        return {}
    soup = BeautifulSoup(content_html, 'html.parser')
    magnet_url = None
    torrent_url = None
    for a in soup.find_all('a', href=True):
        href = a['href'].strip()
        if not magnet_url and href.startswith('magnet:'):
            magnet_url = href
        elif not torrent_url and href.lower().endswith('.torrent'):
            torrent_url = href
        if magnet_url and torrent_url:
            break
    result = {}
    if magnet_url:
        result['magnet_url'] = magnet_url
    if torrent_url:
        result['torrent_url'] = torrent_url
    return result

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
        details = self._fetch_upcoming_repacks_via_api()
        if details is None:
            details = self._fetch_upcoming_repacks_via_html()
        if details is not None:
            cache.save_details(self.key, cache_key, details.to_dict())
        return details

    def _fetch_upcoming_repacks_via_api(self) -> RepackDetails | None:
        import requests
        params = {'search': 'Upcoming Repacks', 'per_page': 5, '_embed': 'wp:featuredmedia'}
        try:
            resp = requests.get(_API_POSTS_URL, params=params, headers=_HEADERS, timeout=15)
        except Exception as exc:
            logger.warning('FitGirl upcoming-repacks API request failed: %s', exc)
            return None
        if resp.status_code != 200:
            return None
        try:
            posts = resp.json()
        except ValueError:
            return None
        post = next((p for p in posts if p.get('title', {}).get('rendered', '').strip().lower() in _EXCLUDED_TITLES), None)
        if post is None:
            return None
        title = self._clean_title(post.get('title', {}).get('rendered', '').strip()) or 'Upcoming Repacks'
        url = post.get('link', _BASE_URL)
        content_html = post.get('content', {}).get('rendered', '')
        titles = self._extract_upcoming_titles(content_html)
        return RepackDetails(source=self.key, url=url, title=title, cover_url=None, description='', extra={'upcoming_titles': titles})

    def _fetch_upcoming_repacks_via_html(self) -> RepackDetails | None:
        import requests
        from bs4 import BeautifulSoup
        try:
            resp = requests.get(_BASE_URL + '/', headers=_HEADERS, timeout=15)
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
        """Extract the actual list of upcoming game titles from the
        Upcoming Repacks post body.

        FitGirl wraps each entry in a green-colored span, e.g.:
            <span style="color: #339966;">→ Pass the Fear</span>
        so we match on that styling directly rather than guessing at
        which arrow glyph or list markup is used, which is far more
        reliable than scanning plain text.
        """
        from bs4 import BeautifulSoup
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

        # Fallback for posts that don't use the green-span styling: scan
        # <br>-separated lines for the arrow-prefixed format instead.
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
        if use_cache:
            cached = cache.load_search(self.key, query, page)
            if cached is not None:
                entries = [RepackEntry.from_dict(e) for e in cached['entries']]
                return RepackPage(entries=entries, page=page, has_more=cached['has_more'])
        entries, has_more = self._search_via_api(query, page)
        entries = entries or []
        cache.save_search(self.key, query, page, [e.to_dict() for e in entries], has_more)
        return RepackPage(entries=entries, page=page, has_more=has_more)

    def _search_via_api(self, query: str, page: int):
        import requests
        params = {'search': query, 'page': page, 'per_page': _PER_PAGE, '_embed': 'wp:featuredmedia'}
        try:
            resp = requests.get(_API_POSTS_URL, params=params, headers=_HEADERS, timeout=15)
        except Exception as exc:
            logger.warning('FitGirl search request failed: %s', exc)
            return ([], False)
        if resp.status_code == 400:
            return ([], False)
        if resp.status_code != 200:
            logger.warning('FitGirl search returned status %s', resp.status_code)
            return ([], False)
        try:
            posts = resp.json()
        except ValueError:
            logger.warning('FitGirl search returned invalid JSON')
            return ([], False)
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
            if title.strip().lower() in _EXCLUDED_TITLES:
                continue
            entries.append(RepackEntry(source=self.key, title=self._clean_title(title), url=url, poster_url=poster_url, slug=slug))
        has_more = total_pages is None or page < total_pages
        return (entries, has_more)

    def _fetch_via_api(self, page: int):
        import requests
        params = {'page': page, 'per_page': _PER_PAGE, '_embed': 'wp:featuredmedia'}
        try:
            resp = requests.get(_API_POSTS_URL, params=params, headers=_HEADERS, timeout=15)
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
            if title.strip().lower() in _EXCLUDED_TITLES:
                continue
            entries.append(RepackEntry(source=self.key, title=self._clean_title(title), url=url, poster_url=poster_url, slug=slug))
        has_more = total_pages is None or page < total_pages
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
        cache.save_details(self.key, slug, details.to_dict())
        return details

    def _fetch_details_via_api(self, entry: RepackEntry, slug: str):
        import requests
        params = {'slug': slug, '_embed': 'wp:featuredmedia'}
        try:
            resp = requests.get(_API_POSTS_URL, params=params, headers=_HEADERS, timeout=15)
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
        description, size_info, metadata = self._parse_description_and_size(content_html)
        if not metadata and (not size_info):
            metadata = dict(metadata)
            metadata['is_announcement'] = True
        metadata.update(_extract_download_links(content_html))
        return RepackDetails(source=self.key, url=entry.url, title=title, cover_url=cover_url, description=description, size_info=size_info, extra=metadata)

    def _fetch_details_via_html(self, entry: RepackEntry, slug: str) -> RepackDetails:
        import requests
        from bs4 import BeautifulSoup
        try:
            resp = requests.get(entry.url, headers=_HEADERS, timeout=15)
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
                cover_url = img_tag.get('data-src') or img_tag.get('src') or cover_url
        description, size_info, metadata = self._parse_description_and_size(content_html)
        if not metadata and (not size_info):
            metadata = dict(metadata)
            metadata['is_announcement'] = True
        metadata.update(_extract_download_links(content_html))
        return RepackDetails(source=self.key, url=entry.url, title=title, cover_url=cover_url, description=description, size_info=size_info, extra=metadata)
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
    _VALUE_PATTERNS = {'genres': '[A-Za-z0-9][A-Za-z0-9 \\-]*(?:\\s*,\\s*[A-Za-z0-9][A-Za-z0-9 \\-]*)*', 'company': '[A-Za-z0-9][A-Za-z0-9 &\\-\\.]*(?:\\s*,\\s*[A-Za-z0-9][A-Za-z0-9 &\\-\\.]*)*', 'languages': '[A-Za-z0-9/]+(?:\\s*,\\s*[A-Za-z0-9/]+)*', 'original_size': '[\\d.]+\\s*(?:GB|MB|TB)', 'repack_size': '(?:from\\s+)?[\\d.]+(?:\\s*/\\s*[\\d.]+)?\\s*(?:GB|MB|TB)(?:\\s*\\[\\s*Selective Download\\s*\\])?'}

    @classmethod
    def _strip_metadata_line(cls, text: str) -> str:
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
                if run_len >= 2:
                    candidate = (spans[run_start_idx][0], spans[i - 1][1])
                    if best_run is None or candidate[1] - candidate[0] > best_run[1] - best_run[0]:
                        best_run = candidate
                run_start_idx = i
        if best_run is None:
            return text
        chain_start, chain_end = best_run
        remainder = (text[:chain_start] + ' ' + text[chain_end:]).strip()
        return remainder

    @staticmethod
    def _parse_description_and_size(content_html: str) -> tuple[str, str | None, dict]:
        from bs4 import BeautifulSoup
        if not content_html:
            return ('', None, {})
        soup = BeautifulSoup(content_html, 'html.parser')
        full_text = soup.get_text('\n')
        metadata = FitGirlSource._extract_metadata(full_text)
        size_info = metadata.pop('repack_size', None)
        if size_info is None:
            size_match = re.search('(Repack Size|Final Size)\\s*:?\\s*([^\\n<]+)', full_text, re.IGNORECASE)
            if size_match:
                size_info = size_match.group(2).strip()
                size_info = re.sub('\\[\\s*Selective Download\\s*\\]', '', size_info, flags=re.IGNORECASE).strip()
                size_info = re.sub('(?i)^from\\s+', '', size_info).strip()
        _STOP_HEADINGS = ('download mirrors', 'screenshots', 'system requirements', 'changelog', 'torrent', 'magnet', 'direct links')
        _REPACK_FEATURE_HEADINGS = ('repack features', 'backwards compatibility', 'installation notes', 'included dlcs', 'included dlc')
        _GAME_DESCRIPTION_HEADINGS = ('game features', 'game description')

        def _looks_like_stop_heading(text: str) -> bool:
            lowered = text.lower().strip()
            if len(lowered) <= 40:
                return any((marker in lowered for marker in _STOP_HEADINGS))
            # Some posts cram the stop-heading and everything after it
            # (mirror lists, filenames, etc.) into a single paragraph with
            # no separating tag, e.g. "Download Mirrors (Direct
            # Links)Filehoster: ...part01.rar...". In that case the
            # heading phrase still appears at the very start of the text,
            # so check the leading portion instead of requiring the whole
            # block to be short.
            lead = lowered[:60]
            return any(lead.startswith(marker) for marker in _STOP_HEADINGS)

        def _classify_heading(text: str) -> str | None:
            stripped = text.strip().rstrip(':').strip().lower()
            if stripped in _REPACK_FEATURE_HEADINGS:
                return 'repack'
            if stripped in _GAME_DESCRIPTION_HEADINGS:
                return 'game_merge'
            return None

        def _looks_like_pseudo_heading(text: str) -> tuple[str, str] | None:
            stripped = text.strip().rstrip(':').strip()
            if len(stripped) > 40:
                return None
            kind = _classify_heading(stripped)
            if kind is not None:
                return (kind, stripped)
            return None
        _DISCUSSION_LINE_RE = re.compile('discussion\\s+and\\s*\\(?possible\\)?\\s*future\\s+updates.*thread', re.IGNORECASE)

        def _looks_like_discussion_link_line(text: str) -> bool:
            return bool(_DISCUSSION_LINE_RE.search(text))
        _BACKWARDS_COMPAT_LINE_RE = re.compile('this repack (is|is not|isn.t) backwards compatible', re.IGNORECASE)

        def _looks_like_backwards_compat_line(text: str) -> bool:
            return bool(_BACKWARDS_COMPAT_LINE_RE.search(text))
        game_sections: list[tuple[str, list[str]]] = [('', [])]
        repack_sections: list[tuple[str, list[str]]] = []
        current_stream = 'game'
        discard_current = False
        last_repack_heading = ''
        for el in soup.find_all(['p', 'h2', 'h3', 'h4', 'ul', 'ol'], recursive=True):
            if el.find_parent(['ul', 'ol']) is not None:
                continue
            if el.name in ('h2', 'h3', 'h4'):
                heading_text = el.get_text(strip=True)
                if not heading_text:
                    continue
                kind = _classify_heading(heading_text)
                if kind == 'repack':
                    repack_sections.append((heading_text, []))
                    current_stream = 'repack'
                    last_repack_heading = heading_text.strip().rstrip(':').strip().lower()
                    discard_current = False
                    continue
                if kind == 'game_merge':
                    current_stream = 'game'
                    discard_current = False
                    continue
                if _looks_like_stop_heading(heading_text):
                    discard_current = True
                    continue
                continue
            if el.name in ('ul', 'ol'):
                if discard_current:
                    continue
                items = [li.get_text(' ', strip=True) for li in el.find_all('li', recursive=False)]
                items = [item for item in items if item]
                if not items:
                    continue
                target = repack_sections if current_stream == 'repack' else game_sections
                target[-1][1].append('\n'.join((f'• {item}' for item in items)))
                continue
            text = el.get_text(strip=True)
            if not text:
                continue
            has_br = el.find('br') is not None
            if has_br:
                raw_lines = [piece.strip() for piece in el.get_text('\n', strip=True).split('\n') if piece.strip()]
                if len(raw_lines) > 1 and any((ln.startswith(('•', '-', '*')) for ln in raw_lines)):
                    text = '\n'.join(raw_lines)
            text = FitGirlSource._strip_metadata_line(text)
            if not text:
                continue
            pseudo_heading = _looks_like_pseudo_heading(text)
            if pseudo_heading is not None:
                kind, heading_text = pseudo_heading
                if kind == 'repack':
                    repack_sections.append((heading_text, []))
                    current_stream = 'repack'
                    last_repack_heading = heading_text.strip().rstrip(':').strip().lower()
                else:
                    current_stream = 'game'
                discard_current = False
                continue
            if _looks_like_stop_heading(text):
                discard_current = True
                continue
            if _looks_like_discussion_link_line(text):
                continue
            if discard_current:
                continue
            if _looks_like_backwards_compat_line(text):
                if last_repack_heading != 'backwards compatibility':
                    repack_sections.append(('Backwards Compatibility', []))
                    last_repack_heading = 'backwards compatibility'
                current_stream = 'repack'
                repack_sections[-1][1].append(text)
                continue
            if current_stream == 'repack':
                current_stream = 'game'
            target = repack_sections if current_stream == 'repack' else game_sections
            target[-1][1].append(text)

        def _render(sections: list[tuple[str, list[str]]]) -> str:
            rendered: list[str] = []
            for heading, blocks in sections:
                body = '\n\n'.join(blocks).strip()
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
                fallback_parts.append(text)
            description = '\n\n'.join(fallback_parts).strip()
        if repack_features:
            metadata['repack_features'] = repack_features
        return (description, size_info, metadata)

    def _extract_featured_image(self, post: dict) -> str | None:
        embedded = post.get('_embedded', {})
        media_list = embedded.get('wp:featuredmedia') or []
        if media_list:
            media = media_list[0]
            sizes = media.get('media_details', {}).get('sizes', {})
            for size_key in ('medium', 'medium_large', 'full'):
                if size_key in sizes:
                    return sizes[size_key].get('source_url')
            source_url = media.get('source_url')
            if source_url:
                return source_url
        content_html = post.get('content', {}).get('rendered', '')
        return self._first_image_from_html(content_html)

    @staticmethod
    def _first_image_from_html(html: str) -> str | None:
        if not html:
            return None
        match = re.search('<img[^>]+(?:data-src|src)=["\\\']([^"\\\']+)["\\\']', html)
        if match:
            return match.group(1)
        return None

    def _fetch_via_html(self, page: int):
        import requests
        from bs4 import BeautifulSoup
        url = _BASE_URL + '/' if page <= 1 else _PAGE_URL_TMPL.format(page=page)
        try:
            resp = requests.get(url, headers=_HEADERS, timeout=15)
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
            if title.strip().lower() in _EXCLUDED_TITLES:
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
        return title.strip()