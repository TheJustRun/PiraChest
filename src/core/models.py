from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional


class DLState(str, Enum):
    queued = "queued"
    downloading = "downloading"
    completed = "completed"
    failed = "failed"
    cancelled = "cancelled"


_LOSSLESS_EXTS = {"flac", "wav", "alac", "ape", "wv", "tta", "dsf", "dff"}


@dataclass(slots=True)
class MediaItem:
    provider: str
    id: str
    title: str
    artwork_url: Optional[str] = None
    artwork_path: Optional[str] = None
    metadata: dict = field(default_factory=dict)

    @property
    def key(self) -> str:
        return f"{self.provider}:{self.id}"

    def to_dict(self) -> dict:
        return {
            "provider": self.provider,
            "id": self.id,
            "title": self.title,
            "artwork_url": self.artwork_url,
            "artwork_path": self.artwork_path,
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, data: dict) -> "MediaItem":
        return cls(
            provider=data.get("provider", ""),
            id=str(data.get("id", "")),
            title=data.get("title", "Unknown"),
            artwork_url=data.get("artwork_url"),
            artwork_path=data.get("artwork_path"),
            metadata=dict(data.get("metadata") or {}),
        )


def _media_item_kwargs(data: dict) -> dict:
    return dict(
        provider=data.get("provider", ""),
        id=str(data.get("id", "")),
        title=data.get("title", "Unknown"),
        artwork_url=data.get("artwork_url"),
        artwork_path=data.get("artwork_path"),
        metadata=dict(data.get("metadata") or {}),
    )


@dataclass(slots=True)
class GameItem(MediaItem):
    platform: str = ""
    developer: str = ""
    genre: str = ""

    def to_dict(self) -> dict:
        d = super(GameItem, self).to_dict()
        d.update(platform=self.platform, developer=self.developer, genre=self.genre)
        return d

    @classmethod
    def from_dict(cls, data: dict) -> "GameItem":
        return cls(**_media_item_kwargs(data), platform=data.get("platform", ""), developer=data.get("developer", ""), genre=data.get("genre", ""))


@dataclass(slots=True)
class BookItem(MediaItem):
    author: str = ""
    isbn: str = ""
    pages: int = 0
    description: str = ""
    formats: dict = field(default_factory=dict)
    subjects: list = field(default_factory=list)
    language: str = ""
    year: str = ""

    def to_dict(self) -> dict:
        d = super(BookItem, self).to_dict()
        d.update(author=self.author, isbn=self.isbn, pages=self.pages, description=self.description,
                  formats=dict(self.formats), subjects=list(self.subjects), language=self.language, year=self.year)
        return d

    @classmethod
    def from_dict(cls, data: dict) -> "BookItem":
        return cls(**_media_item_kwargs(data), author=data.get("author", ""), isbn=data.get("isbn", ""),
                    pages=data.get("pages", 0), description=data.get("description", ""),
                    formats=dict(data.get("formats") or {}), subjects=list(data.get("subjects") or []),
                    language=data.get("language", ""), year=data.get("year", ""))


@dataclass(slots=True)
class MusicItem(MediaItem):
    artist: str = ""
    album: str = ""
    duration_s: Optional[int] = None
    ext: Optional[str] = None
    bitrate: Optional[int] = None
    codec: Optional[str] = None
    file_size_bytes: Optional[int] = None
    download_url: Optional[Any] = None
    source: str = ""
    file_size: Optional[str] = None
    duration: Optional[str] = None
    lyric: Optional[str] = None
    raw: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        d = super(MusicItem, self).to_dict()
        d.update(artist=self.artist, album=self.album, duration_s=self.duration_s, ext=self.ext,
                  bitrate=self.bitrate, codec=self.codec, file_size_bytes=self.file_size_bytes,
                  download_url=self.download_url if isinstance(self.download_url, str) else None,
                  source=self.source, file_size=self.file_size, duration=self.duration,
                  lyric=self.lyric, raw=self.raw if isinstance(self.raw, dict) else {})
        return d

    @classmethod
    def from_dict(cls, data: dict) -> "MusicItem":
        return cls(**_media_item_kwargs(data), artist=data.get("artist", ""), album=data.get("album", ""),
                    duration_s=data.get("duration_s"), ext=data.get("ext"), bitrate=data.get("bitrate"),
                    codec=data.get("codec"), file_size_bytes=data.get("file_size_bytes"),
                    download_url=data.get("download_url"), source=data.get("source", ""),
                    file_size=data.get("file_size"), duration=data.get("duration"),
                    lyric=data.get("lyric"), raw=data.get("raw") or {})

    @classmethod
    def from_song_info(cls, source: str, song_info: Any) -> "MusicItem":
        identifier = str(getattr(song_info, "identifier", "") or "")
        return cls(
            provider=source,
            id=identifier,
            title=getattr(song_info, "song_name", "") or "",
            artist=getattr(song_info, "singers", "") or "",
            source=source,
            album=getattr(song_info, "album", None) or "",
            ext=getattr(song_info, "ext", None),
            file_size=getattr(song_info, "file_size", None),
            file_size_bytes=getattr(song_info, "file_size_bytes", None),
            duration=getattr(song_info, "duration", None),
            duration_s=getattr(song_info, "duration_s", None),
            bitrate=getattr(song_info, "bitrate", None),
            codec=getattr(song_info, "codec", None),
            artwork_url=getattr(song_info, "cover_url", None),
            lyric=getattr(song_info, "lyric", None),
            download_url=getattr(song_info, "download_url", None),
            raw=getattr(song_info, "raw_data", {}) or {},
        )

    @property
    def is_lossless(self) -> bool:
        codec = (self.codec or "").lower()
        if codec:
            return codec in _LOSSLESS_EXTS or codec in {"pcm_s16le", "pcm_s24le", "pcm_s32le", "alac", "wavpack"}
        return (self.ext or "").lower().lstrip(".") in _LOSSLESS_EXTS

    @property
    def quality_label(self) -> str:
        parts = []
        if self.is_lossless:
            parts.append("Lossless")
        elif self.bitrate:
            parts.append(f"{self.bitrate}kbps")
        elif self.ext:
            parts.append(self.ext.upper().lstrip("."))
        if self.codec and self.codec.lower() not in (self.ext or "").lower() and not self.is_lossless:
            parts.append(self.codec.upper())
        return " · ".join(parts)

    @property
    def quality_tier(self) -> str:
        if self.is_lossless:
            return "lossless"
        ext = (self.ext or "").lower().lstrip(".")
        if ext == "mp3":
            return "mp3"
        if ext in ("aac", "m4a"):
            return "aac"
        return "other"

    @property
    def song_name(self) -> str:
        return self.title

    @song_name.setter
    def song_name(self, value: str) -> None:
        self.title = value

    @property
    def singers(self) -> str:
        return self.artist

    @singers.setter
    def singers(self, value: str) -> None:
        self.artist = value

    @property
    def identifier(self) -> str:
        return self.id

    @identifier.setter
    def identifier(self, value: str) -> None:
        self.id = value

    @property
    def cover_url(self) -> Optional[str]:
        return self.artwork_url

    @cover_url.setter
    def cover_url(self, value: Optional[str]) -> None:
        self.artwork_url = value

    @property
    def cover_path(self) -> Optional[str]:
        return self.artwork_path

    @cover_path.setter
    def cover_path(self, value: Optional[str]) -> None:
        self.artwork_path = value


@dataclass(slots=True)
class AnimeItem(MediaItem):
    id_mal: Optional[int] = None
    title_english: str = ""
    title_romaji: str = ""
    title_native: str = ""
    status: str = ""
    format: str = ""
    season_year: Optional[int] = None
    episodes: Optional[int] = None
    genres: list = field(default_factory=list)
    description: str = ""
    synonyms: list = field(default_factory=list)

    def to_dict(self) -> dict:
        d = super(AnimeItem, self).to_dict()
        d.update(id_mal=self.id_mal, title_english=self.title_english, title_romaji=self.title_romaji,
                  title_native=self.title_native, status=self.status, format=self.format,
                  season_year=self.season_year, episodes=self.episodes, genres=list(self.genres),
                  description=self.description, synonyms=list(self.synonyms))
        return d

    @classmethod
    def from_dict(cls, data: dict) -> "AnimeItem":
        return cls(**_media_item_kwargs(data), id_mal=data.get("id_mal"),
                    title_english=data.get("title_english", ""), title_romaji=data.get("title_romaji", ""),
                    title_native=data.get("title_native", ""), status=data.get("status", ""),
                    format=data.get("format", ""), season_year=data.get("season_year"),
                    episodes=data.get("episodes"), genres=list(data.get("genres") or []),
                    description=data.get("description", ""), synonyms=list(data.get("synonyms") or []))

    @classmethod
    def from_anilist_media(cls, media: dict) -> "AnimeItem":
        """Builds an AnimeItem straight from the media shape returned by
        anilist.get_media()/search_anime(), so UI code works with the same
        dataclass (and gets the same `.key` = 'provider:id' convention used
        for caching/downloads elsewhere) instead of passing raw dicts
        around."""
        title = media.get("title") or {}
        display_title = title.get("english") or title.get("romaji") or title.get("native") or "Unknown"
        return cls(
            provider="anilist",
            id=str(media.get("id", "")),
            title=display_title,
            artwork_url=media.get("coverImage"),
            id_mal=media.get("idMal"),
            title_english=title.get("english") or "",
            title_romaji=title.get("romaji") or "",
            title_native=title.get("native") or "",
            status=media.get("status") or "",
            format=media.get("format") or "",
            season_year=media.get("seasonYear"),
            episodes=media.get("episodes"),
            genres=list(media.get("genres") or []) if isinstance(media.get("genres"), list) else [],
            description=media.get("description") or "",
            synonyms=list(media.get("synonyms") or []) if isinstance(media.get("synonyms"), list) else [],
        )

    @property
    def display_title(self) -> str:
        return self.title_english or self.title_romaji or self.title_native or self.title


@dataclass(slots=True)
class EpisodeItem:
    """Mirrors the shape produced by anime.provider_utils.episode_meta(),
    giving provider modules and the UI a typed object instead of a loose
    dict for per-episode metadata (title, synopsis, air date, filler flag)."""

    number: Any
    title: str = ""
    description: str = ""
    duration_s: Optional[int] = None
    filler: bool = False
    air_date: Optional[str] = None
    image_url: Optional[str] = None

    @classmethod
    def from_meta(cls, number: Any, meta: dict) -> "EpisodeItem":
        return cls(
            number=number,
            title=meta.get("title") or f"Episode {number}",
            description=meta.get("description") or "",
            duration_s=meta.get("duration"),
            filler=bool(meta.get("filler")),
            air_date=meta.get("airDate"),
            image_url=meta.get("image"),
        )

    def to_dict(self) -> dict:
        return {
            "number": self.number,
            "title": self.title,
            "description": self.description,
            "duration_s": self.duration_s,
            "filler": self.filler,
            "air_date": self.air_date,
            "image_url": self.image_url,
        }


@dataclass(slots=True)
class RepackItem(MediaItem):
    size: str = ""
    files: list = field(default_factory=list)
    url: str = ""

    def to_dict(self) -> dict:
        d = super(RepackItem, self).to_dict()
        d.update(size=self.size, files=list(self.files), url=self.url)
        return d

    @classmethod
    def from_dict(cls, data: dict) -> "RepackItem":
        return cls(**_media_item_kwargs(data), size=data.get("size", ""), files=list(data.get("files") or []), url=data.get("url", ""))


@dataclass(slots=True)
class MediaPage:
    entries: list
    page: int
    has_more: bool


@dataclass(slots=True)
class DownloadRecord:
    id: str
    item: MediaItem
    state: DLState
    progress: float = 0.0
    error: Optional[str] = None
    output_path: Optional[str] = None

    def to_persist_dict(self) -> dict:
        return {
            "id": self.id,
            "item": self.item.to_dict(),
            "state": self.state.value,
            "progress": self.progress,
            "error": self.error,
            "output_path": self.output_path,
        }
