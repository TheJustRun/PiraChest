from __future__ import annotations

import logging

from .base import BookProvider, Cancelled, IsCancelled
from .gutenberg import get
from ...models import BookItem, MediaPage

logger = logging.getLogger(__name__)

_API_URL = 'https://en.wikisource.org/w/api.php'
_EXPORT_URL = 'https://ws-export.wmcloud.org/?lang=en&format={fmt}&page={title}'
_PAGE_URL = 'https://en.wikisource.org/wiki/{title}'
_PAGE_SIZE = 20
_EXPORT_FORMATS = {
    'application/epub+zip': 'epub',
    'application/pdf': 'pdf',
    'application/x-mobipocket-ebook': 'mobi',
    'text/plain': 'txt',
}


def _export_formats(title: str) -> dict:
    return {mime: _EXPORT_URL.format(fmt=fmt, title=title) for mime, fmt in _EXPORT_FORMATS.items()}


def _fetch_pageimages(titles: list[str]) -> dict:
    if not titles:
        return {}
    try:
        resp = get(_API_URL, params={
            'action': 'query', 'format': 'json', 'prop': 'pageimages',
            'piprop': 'thumbnail', 'pithumbsize': '500', 'titles': '|'.join(titles),
        })
        pages = resp.json().get('query', {}).get('pages', {})
        out = {}
        for p in pages.values():
            thumb = p.get('thumbnail') or {}
            if p.get('title') and thumb.get('source'):
                out[p['title']] = thumb['source']
        return out
    except Exception as exc:
        logger.warning('Wikisource pageimages failed: %s', exc)
        return {}


def _check(is_cancelled: IsCancelled) -> None:
    if is_cancelled is not None and is_cancelled():
        raise Cancelled()


def _strip_html(text: str) -> str:
    out = []
    in_tag = False
    for ch in text:
        if ch == '<':
            in_tag = True
            continue
        if ch == '>':
            in_tag = False
            continue
        if not in_tag:
            out.append(ch)
    return ''.join(out)


def _entry_from_result(item: dict) -> BookItem:
    title = item.get('title', 'Unknown')
    snippet = _strip_html(item.get('snippet', ''))
    encoded_title = title.replace(' ', '_')
    formats = {'text/html': _PAGE_URL.format(title=encoded_title)}
    formats.update(_export_formats(encoded_title))
    return BookItem(
        provider=WikisourceProvider.key,
        id=title,
        title=title,
        author='',
        language='en',
        description=snippet,
        artwork_url=item.get('thumbnail_url'),
        formats=formats,
    )


class WikisourceProvider(BookProvider):
    key = 'wikisource'
    display_name = 'Wikisource'
    priority = 40

    def _fetch(self, params: dict, page: int, is_cancelled: IsCancelled = None) -> MediaPage:
        _check(is_cancelled)
        query_params = dict(params)
        query_params.update({
            'action': 'query',
            'list': 'search',
            'format': 'json',
            'srlimit': _PAGE_SIZE,
            'sroffset': (page - 1) * _PAGE_SIZE,
            'srnamespace': 0,
        })
        try:
            resp = get(_API_URL, params=query_params)
        except Exception as exc:
            logger.warning('Wikisource request failed: %s', exc)
            return MediaPage(entries=[], page=page, has_more=False)
        _check(is_cancelled)
        if resp.status_code != 200:
            return MediaPage(entries=[], page=page, has_more=False)
        try:
            data = resp.json()
        except Exception as exc:
            logger.warning('Wikisource parse failed: %s', exc)
            return MediaPage(entries=[], page=page, has_more=False)
        _check(is_cancelled)
        search = data.get('query', {}).get('search', [])
        titles = [r.get('title') for r in search if r.get('title')]
        thumbs = _fetch_pageimages(titles)
        _check(is_cancelled)
        entries = []
        for r in search:
            r = dict(r)
            r['thumbnail_url'] = thumbs.get(r.get('title'))
            entries.append(_entry_from_result(r))
        has_more = 'continue' in data
        return MediaPage(entries=entries, page=page, has_more=has_more)

    def search(self, query: str, page: int, is_cancelled: IsCancelled = None) -> MediaPage:
        return self._fetch({'srsearch': query}, page, is_cancelled=is_cancelled)

    def browse(self, page: int, is_cancelled: IsCancelled = None) -> MediaPage:
        _check(is_cancelled)
        try:
            resp = get(_API_URL, params={'action': 'query', 'format': 'json', 'list': 'random', 'rnnamespace': 0, 'rnlimit': _PAGE_SIZE})
        except Exception as exc:
            logger.warning('Wikisource browse failed: %s', exc)
            return MediaPage(entries=[], page=page, has_more=False)
        _check(is_cancelled)
        if resp.status_code != 200:
            return MediaPage(entries=[], page=page, has_more=False)
        try:
            data = resp.json()
        except Exception as exc:
            logger.warning('Wikisource browse parse failed: %s', exc)
            return MediaPage(entries=[], page=page, has_more=False)
        titles = [r.get('title') for r in data.get('query', {}).get('random', []) if r.get('title')]
        if not titles:
            return MediaPage(entries=[], page=page, has_more=False)
        _check(is_cancelled)
        extracts = {}
        try:
            resp2 = get(_API_URL, params={'action': 'query', 'format': 'json', 'prop': 'extracts', 'titles': '|'.join(titles), 'explaintext': 1, 'exintro': 1})
            pages = resp2.json().get('query', {}).get('pages', {})
            extracts = {p.get('title'): p.get('extract', '') for p in pages.values()}
        except Exception as exc:
            logger.warning('Wikisource browse extracts failed: %s', exc)
        thumbs = _fetch_pageimages(titles)
        entries = [_entry_from_result({'title': t, 'snippet': extracts.get(t, ''), 'thumbnail_url': thumbs.get(t)}) for t in titles]
        return MediaPage(entries=entries, page=page, has_more=True)

    def details(self, book_id: str, is_cancelled: IsCancelled = None) -> BookItem | None:
        _check(is_cancelled)
        params = {
            'action': 'query',
            'format': 'json',
            'prop': 'extracts|info|pageimages',
            'titles': book_id,
            'explaintext': 1,
            'exintro': 1,
            'inprop': 'url',
            'piprop': 'thumbnail',
            'pithumbsize': '500',
        }
        try:
            resp = get(_API_URL, params=params)
        except Exception as exc:
            logger.warning('Wikisource details failed: %s', exc)
            return None
        _check(is_cancelled)
        if resp.status_code != 200:
            return None
        try:
            data = resp.json()
        except Exception as exc:
            logger.warning('Wikisource details parse failed: %s', exc)
            return None
        _check(is_cancelled)
        pages = data.get('query', {}).get('pages', {})
        if not pages:
            return None
        page_data = next(iter(pages.values()))
        if 'missing' in page_data:
            return None
        title = page_data.get('title', book_id)
        description = page_data.get('extract', '')
        thumb = (page_data.get('thumbnail') or {}).get('source')
        encoded_title = title.replace(' ', '_')
        formats = {'text/html': _PAGE_URL.format(title=encoded_title)}
        formats.update(_export_formats(encoded_title))
        return BookItem(
            provider=self.key,
            id=title,
            title=title,
            author='',
            language='en',
            description=description,
            artwork_url=thumb,
            formats=formats,
        )
