from __future__ import annotations

import logging
import threading
from collections import OrderedDict

import requests

logger = logging.getLogger(__name__)

_session = requests.Session()

_SEARCH_URL = 'https://openlibrary.org/search.json'
_WORKS_URL = 'https://openlibrary.org{key}.json'

_CACHE_LIMIT = 1000
_cache: "OrderedDict[str, dict]" = OrderedDict()
_lock = threading.Lock()


def _cache_get(key: str):
    with _lock:
        val = _cache.get(key)
        if val is not None:
            _cache.move_to_end(key)
        return val


def _cache_put(key: str, val: dict) -> None:
    with _lock:
        _cache[key] = val
        _cache.move_to_end(key)
        while len(_cache) > _CACHE_LIMIT:
            _cache.popitem(last=False)


def _description_text(desc) -> str:
    if isinstance(desc, dict):
        return desc.get('value', '') or ''
    if isinstance(desc, str):
        return desc
    return ''


def lookup(title: str, author: str = '') -> dict | None:
    if not title.strip():
        return None
    cache_key = f'{title.lower()}::{author.lower()}'
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached or None

    doc = _search_doc(title, author)
    if doc is None and author.strip():
        doc = _search_doc(title, '')
    if doc is None:
        _cache_put(cache_key, {})
        return None

    description = ''
    work_key = doc.get('key')
    if work_key:
        try:
            resp = _session.get(_WORKS_URL.format(key=work_key), timeout=8)
            resp.raise_for_status()
            description = _description_text(resp.json().get('description'))
        except Exception as exc:
            logger.warning('Open Library work fetch failed: %s', exc)

    result = {
        'description': description,
        'isbn': (doc.get('isbn') or [''])[0],
        'subjects': list(doc.get('subject') or [])[:20],
        'year': str(doc.get('first_publish_year') or ''),
        'language': (doc.get('language') or [''])[0],
    }
    _cache_put(cache_key, result)
    return result


def _search_doc(title: str, author: str) -> dict | None:
    params = {'title': title, 'limit': 1, 'fields': 'key,isbn,subject,first_publish_year,language'}
    if author.strip():
        params['author'] = author
    try:
        resp = _session.get(_SEARCH_URL, params=params, timeout=8)
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:
        logger.warning('Open Library search failed: %s', exc)
        return None
    docs = data.get('docs') or []
    return docs[0] if docs else None
