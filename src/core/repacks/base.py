from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


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


class RepackSource:
    key: str = ""
    display_name: str = ""

    def fetch_page(self, page: int) -> RepackPage:
        raise NotImplementedError

    def fetch_details(self, entry: RepackEntry) -> "RepackDetails":
        raise NotImplementedError

    def search(self, query: str, page: int = 1, use_cache: bool = True) -> RepackPage:
        pass
        raise NotImplementedError

    def fetch_upcoming_repacks(self, use_cache: bool = True) -> Optional["RepackDetails"]:
        return None