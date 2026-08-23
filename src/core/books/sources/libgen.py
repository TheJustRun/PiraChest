from __future__ import annotations

import logging
import random
import threading
from collections import OrderedDict
from urllib.parse import urlparse, urljoin, parse_qs

import requests
from bs4 import BeautifulSoup

from .base import BookProvider, Cancelled, IsCancelled
from ...models import BookItem, MediaPage

logger = logging.getLogger(__name__)

_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"

_EXT_MIME = {
    "epub": "application/epub+zip",
    "pdf": "application/pdf",
    "txt": "text/plain",
    "mobi": "application/x-mobipocket-ebook",
    "azw3": "application/vnd.amazon.ebook",
    "djvu": "image/vnd.djvu",
    "djv": "image/vnd.djvu",
    "fb2": "application/x-fictionbook+xml",
    "cbr": "application/x-cbr",
    "cbz": "application/x-cbz",
    "rtf": "application/rtf",
    "doc": "application/msword",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
}

_SEM = threading.Semaphore(4)
_BOOK_CACHE_LIMIT = 500
_BROWSE_QUERIES = [
    "fiction", "science", "history", "fantasy", "philosophy", "poetry",
    "biography", "mathematics", "psychology", "art", "mystery", "romance",
    "physics", "adventure", "horror", "economics",
]


def _check(is_cancelled: IsCancelled) -> None:
    if is_cancelled is not None and is_cancelled():
        raise Cancelled()


def _resolve_download_link(book_obj) -> str | None:
    mirrors = [m for m in (getattr(book_obj, "mirrors", None) or []) if m]
    md5 = getattr(book_obj, "md5", "") or ""
    for mirror_url in mirrors:
        try:
            parsed = urlparse(mirror_url)
            if not parsed.scheme or not parsed.netloc:
                continue
            root = f"{parsed.scheme}://{parsed.netloc}"
            resp = requests.get(mirror_url, stream=True, timeout=20, headers={"User-Agent": _UA})
            resp.raise_for_status()
            content_type = resp.headers.get("Content-Type", "").lower()
            if "text/html" not in content_type:
                return mirror_url
            soup = BeautifulSoup(resp.text, "html.parser")
            for a in soup.find_all("a", string=lambda s: s and s.strip().upper() == "GET"):
                href = a.get("href")
                if not href:
                    continue
                full_url = urljoin(mirror_url, href)
                key_vals = parse_qs(urlparse(full_url).query).get("key")
                if key_vals and key_vals[0] and md5:
                    return f"{root}/get.php?md5={md5}&key={key_vals[0]}"
        except Exception as exc:
            logger.warning("LibGen mirror failed %s: %s", mirror_url, exc)
            continue
    return None


class LibgenProvider(BookProvider):
    key = "libgen"
    display_name = "Library Genesis"
    priority = 10

    def __init__(self) -> None:
        self._book_cache: "OrderedDict[str, object]" = OrderedDict()
        self._lock = threading.Lock()

    def _remember(self, book_obj) -> None:
        bid = str(getattr(book_obj, "id", "") or "")
        if not bid:
            return
        with self._lock:
            self._book_cache[bid] = book_obj
            self._book_cache.move_to_end(bid)
            while len(self._book_cache) > _BOOK_CACHE_LIMIT:
                self._book_cache.popitem(last=False)

    def _recall(self, book_id: str):
        with self._lock:
            return self._book_cache.get(book_id)

    def search(self, query: str, page: int, is_cancelled: IsCancelled = None) -> MediaPage:
        if not query.strip():
            return MediaPage(entries=[], page=page, has_more=False)
        _check(is_cancelled)
        results = []
        _SEM.acquire(True)
        try:
            from libgen_api_enhanced import LibgenSearch

            results = LibgenSearch().search_default(query, add_upload_info=False) or []
        except Cancelled:
            raise
        except Exception as exc:
            logger.warning("LibGen search failed: %s", exc)
            return MediaPage(entries=[], page=page, has_more=False)
        finally:
            _SEM.release()

        entries = []
        for book_obj in results:
            _check(is_cancelled)
            if not getattr(book_obj, "id", None) or not getattr(book_obj, "title", None):
                continue
            self._remember(book_obj)
            entries.append(self._book_to_item(book_obj, enrich=False))

        return MediaPage(entries=entries, page=page, has_more=False)

    def browse(self, page: int, is_cancelled: IsCancelled = None) -> MediaPage:
        query = random.choice(_BROWSE_QUERIES)
        return self.search(query, page, is_cancelled=is_cancelled)

    def details(self, book_id: str, is_cancelled: IsCancelled = None) -> BookItem | None:
        _check(is_cancelled)
        book_obj = self._recall(book_id)
        if book_obj is None:
            return None

        try:
            link = _resolve_download_link(book_obj)
            if link:
                book_obj.resolved_download_link = link
        except Cancelled:
            raise
        except Exception as exc:
            logger.warning("LibGen link resolution failed for %s: %s", book_id, exc)

        return self._book_to_item(book_obj, include_download=True, enrich=True)

    def _book_to_item(self, book_obj, include_download: bool = False, enrich: bool = False) -> BookItem:
        from ...artwork import get_cover_url
        from .openlibrary_meta import lookup as ol_lookup

        bid = str(getattr(book_obj, "id", "") or "")
        md5 = getattr(book_obj, "md5", "") or ""
        ext = (getattr(book_obj, "extension", "") or "").lower()
        title = getattr(book_obj, "title", "") or "Unknown"
        author = getattr(book_obj, "author", "") or ""

        cover_url = getattr(book_obj, "cover_url", None) or (get_cover_url(bid, md5) if md5 else None)

        formats: dict = {}
        if include_download:
            direct = getattr(book_obj, "resolved_download_link", None)
            tor = getattr(book_obj, "tor_download_link", None)
            link = direct or tor
            if link:
                fmt_key = _EXT_MIME.get(ext, f"application/{ext}" if ext else "application/octet-stream")
                formats[fmt_key] = link

        description = ""
        isbn = ""
        subjects: list = []
        year = str(getattr(book_obj, "year", "") or "")
        language = getattr(book_obj, "language", "") or ""

        if enrich:
            meta = ol_lookup(title, author)
            if meta:
                description = meta.get("description", "")
                isbn = meta.get("isbn", "")
                subjects = meta.get("subjects", [])
                if not year:
                    year = meta.get("year", "")
                if not language:
                    language = meta.get("language", "")
                if not cover_url and isbn:
                    cover_url = f"https://covers.openlibrary.org/b/isbn/{isbn}-L.jpg"

        return BookItem(
            provider=self.key,
            id=bid,
            title=title,
            author=author,
            artwork_url=cover_url,
            year=year,
            language=language,
            pages=int(getattr(book_obj, "pages", 0) or 0) if str(getattr(book_obj, "pages", "") or "").isdigit() else 0,
            description=description,
            isbn=isbn,
            subjects=subjects,
            formats=formats,
        )
