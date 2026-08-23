from __future__ import annotations

import logging

from .base import BookProvider, Cancelled, IsCancelled
from .gutenberg import get
from ...models import BookItem, MediaPage

logger = logging.getLogger(__name__)

_SEARCH_URL = 'https://archive.org/advancedsearch.php'
_METADATA_URL = 'https://archive.org/metadata/{identifier}'
_COVER_URL = 'https://archive.org/services/img/{identifier}'
_READ_URL = 'https://archive.org/details/{identifier}'
_PAGE_SIZE = 20

_QUERY_FILTER = 'mediatype:texts AND (collection:opensource OR collection:americana OR collection:inlibrary OR collection:printdisabled)'

_FIELDS = ['identifier', 'title', 'creator', 'year', 'language', 'description', 'subject']


def _check(is_cancelled: IsCancelled) -> None:
    if is_cancelled is not None and is_cancelled():
        raise Cancelled()


def _first(value) -> str:
    if isinstance(value, list):
        return value[0] if value else ''
    return value or ''


def _as_list(value) -> list:
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def _entry_from_doc(doc: dict) -> BookItem | None:
    identifier = doc.get('identifier')
    if not identifier:
        return None
    return BookItem(
        provider=InternetArchiveProvider.key,
        id=identifier,
        title=doc.get('title', 'Unknown') or 'Unknown',
        author=_first(doc.get('creator')),
        artwork_url=_COVER_URL.format(identifier=identifier),
        language=_first(doc.get('language')),
        year=str(doc.get('year') or ''),
        description=doc.get('description', '') if isinstance(doc.get('description'), str) else '',
        subjects=_as_list(doc.get('subject'))[:20],
        formats={'read': _READ_URL.format(identifier=identifier)},
    )


class InternetArchiveProvider(BookProvider):
    key = 'archive'
    display_name = 'Internet Archive'
    priority = 30

    def _fetch(self, query: str, page: int, is_cancelled: IsCancelled = None) -> MediaPage:
        _check(is_cancelled)
        params = {
            'q': query,
            'fl[]': _FIELDS,
            'rows': _PAGE_SIZE,
            'page': page,
            'output': 'json',
        }
        try:
            resp = get(_SEARCH_URL, params=params, timeout=15)
        except Exception as exc:
            logger.warning('Internet Archive request failed: %s', exc)
            return MediaPage(entries=[], page=page, has_more=False)
        _check(is_cancelled)
        if resp.status_code != 200:
            return MediaPage(entries=[], page=page, has_more=False)
        try:
            data = resp.json()
        except Exception as exc:
            logger.warning('Internet Archive parse failed: %s', exc)
            return MediaPage(entries=[], page=page, has_more=False)
        _check(is_cancelled)
        response = data.get('response') or {}
        docs = response.get('docs') or []
        total = response.get('numFound', 0)
        entries = [e for e in (_entry_from_doc(d) for d in docs) if e is not None]
        has_more = page * _PAGE_SIZE < total
        return MediaPage(entries=entries, page=page, has_more=has_more)

    def search(self, query: str, page: int, is_cancelled: IsCancelled = None) -> MediaPage:
        q = f'({query}) AND {_QUERY_FILTER}'
        return self._fetch(q, page, is_cancelled=is_cancelled)

    def browse(self, page: int, is_cancelled: IsCancelled = None) -> MediaPage:
        return self._fetch(_QUERY_FILTER, page, is_cancelled=is_cancelled)

    def details(self, book_id: str, is_cancelled: IsCancelled = None) -> BookItem | None:
        _check(is_cancelled)
        try:
            resp = get(_METADATA_URL.format(identifier=book_id))
        except Exception as exc:
            logger.warning('Internet Archive details failed: %s', exc)
            return None
        _check(is_cancelled)
        if resp.status_code != 200:
            return None
        try:
            data = resp.json()
        except Exception as exc:
            logger.warning('Internet Archive details parse failed: %s', exc)
            return None
        _check(is_cancelled)
        meta = data.get('metadata') or {}
        if not meta:
            return None
        subjects = _as_list(meta.get('subject'))[:20]
        return BookItem(
            provider=self.key,
            id=book_id,
            title=meta.get('title', 'Unknown') or 'Unknown',
            author=_first(meta.get('creator')),
            artwork_url=_COVER_URL.format(identifier=book_id),
            language=_first(meta.get('language')),
            year=str(meta.get('year') or meta.get('date') or ''),
            description=meta.get('description', '') if isinstance(meta.get('description'), str) else '',
            subjects=subjects,
            formats={'read': _READ_URL.format(identifier=book_id)},
        )

