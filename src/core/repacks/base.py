from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional
from urllib.parse import urlparse, parse_qs, unquote_plus

from ..cache import cache as _cache

DEFAULT_TTL_SECONDS = 6 * 60 * 60


def load_page(source_key: str, page, ttl_seconds: int = DEFAULT_TTL_SECONDS):
    return _cache.load(f"repacks:{source_key}:pages", str(page), ttl_seconds=ttl_seconds)


def save_page(source_key: str, page, entries: list, has_more: bool) -> None:
    _cache.save(f"repacks:{source_key}:pages", str(page), {"entries": entries, "has_more": has_more})


def load_details(source_key: str, key: str, ttl_seconds: int = DEFAULT_TTL_SECONDS):
    return _cache.load(f"repacks:{source_key}:details", key, ttl_seconds=ttl_seconds)


def save_details(source_key: str, key: str, details: dict) -> None:
    _cache.save(f"repacks:{source_key}:details", key, details)


def clear_source_cache(source_key: str) -> None:
    _cache.clear_namespace(f"repacks:{source_key}:pages")
    _cache.clear_namespace(f"repacks:{source_key}:details")


_ALL_SOURCE_KEYS = ["fitgirl"]


def clear_all_cache() -> None:
    for key in _ALL_SOURCE_KEYS:
        clear_source_cache(key)


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
