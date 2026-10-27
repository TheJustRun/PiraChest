from __future__ import annotations

import contextvars
import re
from dataclasses import dataclass, field
from typing import Optional
from urllib.parse import urlparse, parse_qs, unquote_plus

import httpx

from ..cache import cache as _cache
from ..net import LazyHttpClient, shared_ssl_context

DEFAULT_TTL_SECONDS = 6 * 60 * 60


class Cancelled(BaseException):
    task_cancel = True
    quiet = True


_cancel_check: contextvars.ContextVar = contextvars.ContextVar("repack_cancel_check", default=None)


def set_cancel_check(fn):
    return _cancel_check.set(fn)


def reset_cancel_check(token) -> None:
    _cancel_check.reset(token)


def check_cancelled() -> None:
    fn = _cancel_check.get()
    if fn is not None and fn():
        raise Cancelled()


def submit_ctx(executor, fn, *args, **kwargs):
    ctx = contextvars.copy_context()
    return executor.submit(ctx.run, fn, *args, **kwargs)


_HTTP_UA = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) '
       'Chrome/124.0 Safari/537.36')


# Built on first use: constructing the client parses the CA bundle, and module
# import must not pay that on the UI thread.
http_client = LazyHttpClient(
    headers={'User-Agent': _HTTP_UA, 'Accept': 'application/json, text/html;q=0.9,*/*;q=0.8'},
    timeout=10,
    follow_redirects=True,
    limits=httpx.Limits(max_connections=16, max_keepalive_connections=16, keepalive_expiry=30),
    transport_factory=lambda: httpx.HTTPTransport(retries=1, verify=shared_ssl_context()),
)


def http_get(url: str, **kwargs) -> httpx.Response:
    check_cancelled()
    return http_client.get(url, **kwargs)


def http_get_json(url: str, **kwargs):
    resp = http_get(url, **kwargs)
    resp.raise_for_status()
    return resp.json()


_OG_IMAGE_RES = (
    re.compile(r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)["\']', re.I),
    re.compile(r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:image["\']', re.I),
)
_IMG_TAG_RE = re.compile(r'<img[^>]+(?:data-src|src)=["\']([^"\']+)["\']', re.I)


def _first_image(text: str, allow_img_tag: bool) -> str | None:
    for rx in _OG_IMAGE_RES:
        m = rx.search(text)
        if m and not m.group(1).startswith('data:'):
            return m.group(1)
    if allow_img_tag:
        for m in _IMG_TAG_RE.finditer(text):
            if not m.group(1).startswith('data:'):
                return m.group(1)
    return None


def fetch_page_image(url: str, timeout: float = 5, max_bytes: int = 131072, scan_body: bool = False) -> str | None:
    check_cancelled()
    buf = ''
    try:
        with http_client.stream('GET', url, timeout=timeout) as resp:
            if resp.status_code != 200:
                return None
            total = 0
            for chunk in resp.iter_bytes(8192):
                check_cancelled()
                total += len(chunk)
                buf += chunk.decode('utf-8', errors='ignore')
                found = _first_image(buf, allow_img_tag=False)
                if found:
                    return found
                if '</head>' in buf.lower():
                    if not scan_body:
                        break
                    found = _first_image(buf, allow_img_tag=True)
                    if found:
                        return found
                if total >= max_bytes:
                    break
    except httpx.HTTPError:
        return None
    return _first_image(buf, allow_img_tag=True)


def load_page(source_key: str, page, ttl_seconds: int = DEFAULT_TTL_SECONDS):
    return _cache.load(f"repacks:{source_key}:pages", str(page), ttl_seconds=ttl_seconds)


def save_page(source_key: str, page, entries: list, has_more: bool) -> None:
    _cache.save(f"repacks:{source_key}:pages", str(page), {"entries": entries, "has_more": has_more})


def load_details(source_key: str, key: str, ttl_seconds: int = DEFAULT_TTL_SECONDS):
    return _cache.load(f"repacks:{source_key}:details", key, ttl_seconds=ttl_seconds)


def save_details(source_key: str, key: str, details: dict) -> None:
    _cache.save(f"repacks:{source_key}:details", key, details)


def clear_source_cache(source_key: str, include_details: bool = False) -> None:
    _cache.clear_namespace(f"repacks:{source_key}:pages")
    if include_details:
        _cache.clear_namespace(f"repacks:{source_key}:details")


_ALL_SOURCE_KEYS = ["fitgirl", "gog"]


def clear_all_cache() -> None:
    for key in _ALL_SOURCE_KEYS:
        clear_source_cache(key, include_details=True)


def magnet_display_name(magnet_url: Optional[str]) -> str:
    if not magnet_url:
        return ""
    try:
        query = urlparse(magnet_url).query
        dn = parse_qs(query).get("dn", [""])[0]
        return unquote_plus(dn)
    except Exception:
        return ""


@dataclass
class RepackEntry:
    source: str
    title: str
    url: str
    poster_url: Optional[str] = None
    poster_path: Optional[str] = None
    slug: Optional[str] = None
    extra: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "source": self.source,
            "title": self.title,
            "url": self.url,
            "poster_url": self.poster_url,
            "poster_path": self.poster_path,
            "slug": self.slug,
            "extra": self.extra,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "RepackEntry":
        return cls(
            source=data.get("source", ""),
            title=data.get("title", ""),
            url=data.get("url", ""),
            poster_url=data.get("poster_url"),
            poster_path=data.get("poster_path"),
            slug=data.get("slug"),
            extra=data.get("extra") or {},
        )


@dataclass
class RepackPage:
    entries: list[RepackEntry]
    page: int
    has_more: bool


@dataclass
class RepackDetails:
    source: str
    url: str
    title: str
    cover_url: Optional[str] = None
    cover_path: Optional[str] = None
    description: str = ""
    size_info: Optional[str] = None
    extra: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "source": self.source,
            "url": self.url,
            "title": self.title,
            "cover_url": self.cover_url,
            "cover_path": self.cover_path,
            "description": self.description,
            "size_info": self.size_info,
            "extra": self.extra,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "RepackDetails":
        return cls(
            source=data.get("source", ""),
            url=data.get("url", ""),
            title=data.get("title", ""),
            cover_url=data.get("cover_url"),
            cover_path=data.get("cover_path"),
            description=data.get("description", ""),
            size_info=data.get("size_info"),
            extra=data.get("extra") or {},
        )

    @property
    def screenshot_urls(self) -> list[str]:
        return self.extra.get("screenshot_urls") or []

    @screenshot_urls.setter
    def screenshot_urls(self, value: list[str]) -> None:
        if value:
            self.extra["screenshot_urls"] = value
        else:
            self.extra.pop("screenshot_urls", None)


class RepackSource:
    key: str = ""
    display_name: str = ""

    def fetch_page(self, page: int) -> RepackPage:
        raise NotImplementedError

    def fetch_details(self, entry: RepackEntry) -> "RepackDetails":
        raise NotImplementedError

    def search(self, query: str, page: int = 1, use_cache: bool = True) -> RepackPage:
        raise NotImplementedError

    def fetch_upcoming_repacks(self, use_cache: bool = True) -> Optional["RepackDetails"]:
        return None

    def fetch_popular_repacks(self, use_cache: bool = True) -> list["RepackEntry"]:
        return []

    def fetch_latest_repacks(self, use_cache: bool = True) -> list["RepackEntry"]:
        return []
