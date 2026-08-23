from __future__ import annotations

import logging

import requests

from .base import BookProvider, Cancelled, IsCancelled
from ...models import BookItem, MediaPage

logger = logging.getLogger(__name__)

_session = requests.Session()
_session.headers.update({'User-Agent': 'BooksApp/1.0 (+https://example.com/contact)'})

_BASE_URL = 'https://gutendex.com/books'
_FORMAT_LABELS = {
    'application/epub+zip': 'EPUB',
    'application/pdf': 'PDF',
    'text/plain; charset=utf-8': 'TXT',
    'text/plain; charset=us-ascii': 'TXT',
    'text/plain': 'TXT',
}


def _filtered_formats(formats: dict) -> dict:
    out = {}
    seen_labels = set()
    for mime, url in formats.items():
        label = _FORMAT_LABELS.get(mime)
        if not label or label in seen_labels:
            continue
        seen_labels.add(label)
        out[mime] = url
    return out


def get(url: str, params: dict | None = None, timeout: int = 15):
    return _session.get(url, params=params, timeout=timeout)


def _check(is_cancelled: IsCancelled) -> None:
    if is_cancelled is not None and is_cancelled():
        raise Cancelled()


def _summary_text(item: dict) -> str:
    summaries = item.get('summaries') or []
    return '\n\n'.join(s for s in summaries if s) if summaries else ''


def _entry_from_result(item: dict) -> BookItem:
    authors = ', '.join(a.get('name', '') for a in (item.get('authors') or []))
    formats = item.get('formats') or {}
    languages = item.get('languages') or []
    subjects = list(item.get('subjects') or [])[:20]
    return BookItem(
        provider=GutenbergProvider.key,
        id=str(item.get('id')),
        title=item.get('title', 'Unknown'),
        author=authors,
        artwork_url=formats.get('image/jpeg'),
        language=languages[0] if languages else '',
        description=_summary_text(item),
        subjects=subjects,
    )


class GutenbergProvider(BookProvider):
    key = 'gutenberg'
    display_name = 'Project Gutenberg'
    priority = 20

    def _fetch(self, params: dict, page: int, is_cancelled: IsCancelled = None) -> MediaPage:
        _check(is_cancelled)
        try:
            resp = get(_BASE_URL, params=params)
        except Exception as exc:
            logger.warning('Gutenberg request failed: %s', exc)
            return MediaPage(entries=[], page=page, has_more=False)
        _check(is_cancelled)
        if resp.status_code != 200:
            return MediaPage(entries=[], page=page, has_more=False)
        try:
            data = resp.json()
        except Exception as exc:
            logger.warning('Gutenberg parse failed: %s', exc)
            return MediaPage(entries=[], page=page, has_more=False)
        _check(is_cancelled)
        entries = [_entry_from_result(r) for r in data.get('results', [])]
        return MediaPage(entries=entries, page=page, has_more=bool(data.get('next')))

    def search(self, query: str, page: int, is_cancelled: IsCancelled = None) -> MediaPage:
        return self._fetch({'search': query, 'page': page}, page, is_cancelled=is_cancelled)

    def browse(self, page: int, is_cancelled: IsCancelled = None) -> MediaPage:
        return self._fetch({'page': page, 'sort': 'popular'}, page, is_cancelled=is_cancelled)

    def details(self, book_id: str, is_cancelled: IsCancelled = None) -> BookItem | None:
        _check(is_cancelled)
        try:
            resp = get(f'{_BASE_URL}/{book_id}')
        except Exception as exc:
            logger.warning('Gutenberg details failed: %s', exc)
            return None
        _check(is_cancelled)
        if resp.status_code != 200:
            return None
        try:
            item = resp.json()
        except Exception as exc:
            logger.warning('Gutenberg details parse failed: %s', exc)
            return None
        _check(is_cancelled)
        authors = ', '.join(a.get('name', '') for a in (item.get('authors') or []))
        formats = item.get('formats') or {}
        subjects = list(item.get('subjects') or [])[:20]
        bookshelves = list(item.get('bookshelves') or [])
        languages = item.get('languages') or []
        download_formats = _filtered_formats(formats)
        description = _summary_text(item)
        return BookItem(
            provider=self.key,
            id=str(item.get('id')),
            title=item.get('title', 'Unknown'),
            author=authors,
            artwork_url=formats.get('image/jpeg'),
            description=description,
            formats=download_formats,
            subjects=(subjects + bookshelves)[:20],
            language=languages[0] if languages else '',
        )

