from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from urllib.parse import quote


CATEGORIES = (
    (0, "All"), (100, "Audio"), (200, "Video"),
    (300, "Apps"), (400, "Games"), (500, "Adult"), (600, "Other"),
)
CATEGORY_NAMES = dict(CATEGORIES)


def to_int(value, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        try:
            return int(float(value))
        except (TypeError, ValueError, OverflowError):
            return default


def to_float(value, default: float = 0.0) -> float:
    try:
        f = float(value)
    except (TypeError, ValueError):
        return default
    return f if f == f else default


_TRUE = {"1", "true", "yes", "y", "on", "enabled", "active"}
_FALSE = {"0", "false", "no", "n", "off", "disabled", "inactive"}


def to_bool(value, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    if isinstance(value, (int, float)):
        return value != 0
    s = str(value).strip().lower()
    if s in _TRUE:
        return True
    if s in _FALSE:
        return False
    return default


def _pick(d, *keys, default=None):
    if isinstance(d, dict):
        for k in keys:
            v = d.get(k)
            if v is not None and v != "":
                return v
    return default


def _effective_category(raw, source) -> int:
    if str(source or "").strip().lower().startswith("nyaa"):
        return 200
    return to_int(raw)


def category_bucket(category: int) -> int:
    return (category // 100) * 100 if category else 0


def category_bucket_name(category: int) -> str:
    return CATEGORY_NAMES.get(category_bucket(category), "Other")


def format_size(num_bytes) -> str:
    size = float(max(0, to_int(num_bytes)))
    if size <= 0:
        return ""
    for unit in ("B", "KiB", "MiB", "GiB"):
        if size < 1024:
            return f"{int(size)} B" if unit == "B" else f"{size:.2f} {unit}"
        size /= 1024
    return f"{size:.2f} TiB"


_SIZE_RE = re.compile(r"\s*([\d.]+)\s*([KMGTP]?)(?:i?B)?\s*$", re.IGNORECASE)


def parse_size_label(text) -> int:
    m = _SIZE_RE.match(str(text or "").replace(",", ""))
    if not m:
        return 0
    exp = ("KMGTP".find(m.group(2).upper()) + 1) if m.group(2) else 0
    return int(to_float(m.group(1)) * 1024 ** exp)


def format_age(timestamp, now: float | None = None) -> str:
    ts = to_int(timestamp)
    if ts <= 0:
        return ""
    if ts > 10 ** 11:
        ts //= 1000
    delta = max(0.0, (now if now is not None else time.time()) - ts)
    minutes = int(delta // 60)
    if minutes < 60:
        return f"{max(1, minutes)}m ago"
    hours = minutes // 60
    if hours < 24:
        return f"{hours}h ago"
    days = hours // 24
    if days < 30:
        return f"{days}d ago"
    if days < 365:
        return f"{days // 30}mo ago"
    return f"{days // 365}y ago"


def format_date(timestamp) -> str:
    ts = to_int(timestamp)
    if ts <= 0:
        return ""
    if ts > 10 ** 11:
        ts //= 1000
    try:
        return time.strftime("%Y-%m-%d", time.localtime(ts))
    except (OverflowError, OSError, ValueError):
        return ""


def build_magnet(info_hash: str, name: str = "") -> str:
    if not info_hash:
        return ""
    uri = f"magnet:?xt=urn:btih:{info_hash}"
    return f"{uri}&dn={quote(name)}" if name else uri


_KNOWN_EXTENSIONS = (
    "mkv", "mp4", "avi", "mov", "wmv", "webm", "iso", "zip", "rar", "7z", "epub", "pdf",
    "mobi", "cbz", "cbr", "exe", "dmg", "apk", "flac", "mp3", "m4a", "m4b", "ogg", "wav",
)

_FORMAT_RE = re.compile(r"(?<![a-z0-9])(?:" + "|".join(_KNOWN_EXTENSIONS) + r")(?![a-z0-9])", re.IGNORECASE)


def guess_format_from_title(title: str) -> str:
    m = _FORMAT_RE.search(title or "")
    return m.group(0).upper() if m else ""


def normalize_files(raw) -> list[dict]:
    out: list[dict] = []
    if not isinstance(raw, list):
        return out
    for f in raw:
        if isinstance(f, dict):
            name = str(f.get("name") or f.get("path") or "")
            size = to_int(f.get("size"))
            label = f.get("sizeLabel") or format_size(size)
        else:
            name, size, label = str(f), 0, ""
        if name:
            out.append({"name": name, "size": size, "sizeLabel": label})
    return out


def primary_file_format(files: list) -> str:
    best_ext = ""
    best_size = -1
    for f in files:
        if isinstance(f, dict):
            name = f.get("name", "")
            size = to_int(f.get("size"))
        else:
            name, size = str(f), 0
        if "." not in name:
            continue
        ext = name.rsplit(".", 1)[-1].lower()
        if not ext.isalnum() or len(ext) > 5:
            continue
        if size > best_size:
            best_size = size
            best_ext = ext
    return best_ext.upper()


@dataclass
class TBHBEntry:
    id: str
    title: str
    category: int = 0
    size_label: str = ""
    seeders: int = 0
    leechers: int = 0
    magnet: str = ""
    info_hash: str = ""
    status: str = ""
    source: str = ""
    username: str = ""
    size: int = 0
    added: int = 0
    category_label: str = ""
    page_url: str = ""

    @property
    def category_name(self) -> str:
        return self.category_label or category_bucket_name(self.category)

    @property
    def format_guess(self) -> str:
        return guess_format_from_title(self.title)

    @property
    def age_label(self) -> str:
        return format_age(self.added)

    @property
    def dedupe_key(self) -> str:
        return (self.info_hash or "").lower() or self.id

    @property
    def trust(self) -> str:
        s = (self.status or "").strip().lower()
        return s if s in ("vip", "trusted") else ""

    @classmethod
    def from_api(cls, d: dict) -> "TBHBEntry":
        title = d.get("name") or "Unknown"
        info_hash = str(d.get("info_hash") or "")
        size = to_int(d.get("size"))
        return cls(
            id=str(d.get("id", "")),
            title=title,
            category=_effective_category(d.get("category"), d.get("source")),
            size_label=d.get("sizeLabel") or format_size(size),
            seeders=to_int(d.get("seeders")),
            leechers=to_int(d.get("leechers")),
            magnet=d.get("magnet") or build_magnet(info_hash, title),
            info_hash=info_hash,
            status=d.get("status") or "",
            source=d.get("source") or "",
            username=d.get("username") or "",
            size=size,
            added=to_int(d.get("added")),
            category_label=d.get("categoryLabel") or "",
            page_url=d.get("pageUrl") or "",
        )


@dataclass
class TBHBDetails:
    title: str
    category: int = 0
    size_label: str = ""
    seeders: int = 0
    leechers: int = 0
    info_hash: str = ""
    username: str = ""
    files: list = field(default_factory=list)
    extra: dict = field(default_factory=dict)
    size: int = 0
    added: int = 0
    source: str = ""
    status: str = ""
    category_label: str = ""
    page_url: str = ""
    description: str = ""

    @property
    def category_name(self) -> str:
        return self.category_label or category_bucket_name(self.category)

    @property
    def file_format(self) -> str:
        return primary_file_format(self.files)

    @property
    def total_size_label(self) -> str:
        return format_size(sum(to_int(f.get("size")) for f in self.files if isinstance(f, dict)))

    @property
    def trust(self) -> str:
        s = (self.status or "").strip().lower()
        return s if s in ("vip", "trusted") else ""

    @classmethod
    def from_entry(cls, e: TBHBEntry) -> "TBHBDetails":
        return cls(
            title=e.title, category=e.category, size_label=e.size_label, seeders=e.seeders,
            leechers=e.leechers, info_hash=e.info_hash, username=e.username, size=e.size,
            added=e.added, source=e.source, status=e.status, extra={"magnet_url": e.magnet},
            category_label=e.category_label, page_url=e.page_url,
        )

    @classmethod
    def from_api(cls, d: dict) -> "TBHBDetails":
        t = d.get("torrent") if isinstance(d.get("torrent"), dict) else d
        title = t.get("name") or "Unknown"
        info_hash = str(t.get("info_hash") or "")
        size = to_int(t.get("size"))
        return cls(
            title=title,
            category=_effective_category(t.get("category"), t.get("source")),
            size_label=t.get("sizeLabel") or format_size(size),
            seeders=to_int(t.get("seeders")),
            leechers=to_int(t.get("leechers")),
            info_hash=info_hash,
            username=t.get("username") or "",
            files=normalize_files(d.get("files")),
            extra={
                "magnet_url": t.get("magnet") or build_magnet(info_hash, title),
                "files_error": bool(d.get("files_error")),
                "stale": bool(d.get("_stale")),
            },
            size=size,
            added=to_int(t.get("added")),
            source=t.get("source") or "",
            status=t.get("status") or "",
            category_label=t.get("categoryLabel") or "",
            page_url=t.get("pageUrl") or "",
            description=t.get("description") or "",
        )

    def fill_missing_from(self, e: TBHBEntry) -> None:
        if not self.extra.get("magnet_url"):
            self.extra["magnet_url"] = e.magnet
        for attr in ("username", "category", "size_label", "info_hash", "size", "added", "source", "status",
                     "category_label", "page_url"):
            if not getattr(self, attr):
                setattr(self, attr, getattr(e, attr))
        if not (self.seeders or self.leechers):
            self.seeders, self.leechers = e.seeders, e.leechers


_TAG_RE = re.compile(r"<[^>]+>")


def clean_description(text, limit: int = 1500) -> str:
    t = _TAG_RE.sub("", str(text or "")).replace("\r", "")
    t = re.sub(r"\n{3,}", "\n\n", t).strip()
    return t if len(t) <= limit else t[:limit].rstrip() + "…"


def _pick_any(dicts, *keys):
    for d in dicts:
        v = _pick(d, *keys)
        if v is not None:
            return v
    return None


@dataclass
class UploaderReputation:
    username: str = ""
    xp: float = 0.0
    level: int = 0
    rank: str = ""
    avg_rating: float = 0.0
    rating_count: int = 0
    uploads: int = 0
    distribution: dict = field(default_factory=dict)

    @property
    def has_data(self) -> bool:
        return bool(self.xp or self.level or self.rank or self.avg_rating or self.rating_count or self.uploads)

    @property
    def summary(self) -> str:
        parts = []
        if self.avg_rating > 0:
            parts.append(f"★ {self.avg_rating:.1f}" + (f" ({self.rating_count})" if self.rating_count else ""))
        if self.rank:
            parts.append(self.rank)
        if self.level:
            parts.append(f"Lv {self.level}")
        if self.xp:
            parts.append(f"{self.xp:g} XP")
        if self.uploads:
            parts.append(f"{self.uploads} uploads")
        return " · ".join(parts)

    @property
    def detail(self) -> str:
        if not self.distribution:
            return ""
        return " · ".join(f"{n}★ {to_int(self.distribution.get(str(n)))}" for n in (5, 4, 3, 2, 1))

    @classmethod
    def from_api(cls, d, username: str = "") -> "UploaderReputation":
        if not isinstance(d, dict):
            return cls(username=username)
        r = d.get("reputation") if isinstance(d.get("reputation"), dict) else d
        sub = [x for x in (r.get("uploader"), r.get("ratings"), r.get("rating")) if isinstance(x, dict)]
        src = [r, *sub]
        rank = _pick_any(src, "rank", "tier", "title")
        level = to_int(_pick_any(src, "level", "trust_level", "trustLevel"))
        if not level and isinstance(rank, (int, float)) and not isinstance(rank, bool):
            level = int(rank)
        return cls(
            username=str(_pick_any(src, "username", "name") or username),
            xp=to_float(_pick_any(src, "xp", "score", "points")),
            level=level,
            rank=rank if isinstance(rank, str) else "",
            avg_rating=to_float(_pick_any(src, "averageRating", "average", "avg", "avgRating")),
            rating_count=to_int(_pick_any(src, "ratingCount", "count", "totalRatings")),
            uploads=to_int(_pick_any(src, "uploads", "uploadCount", "upload_count", "totalUploads")),
        )

    def apply_ratings(self, d) -> None:
        if not isinstance(d, dict):
            return
        count = to_int(d.get("ratingCount"))
        if count:
            self.rating_count = count
            self.avg_rating = to_float(d.get("averageRating"))
        if isinstance(d.get("distribution"), dict):
            self.distribution = d["distribution"]

    @classmethod
    def from_profile(cls, data, username: str = "") -> "UploaderReputation":
        data = data if isinstance(data, dict) else {}
        rep = cls.from_api(data.get("reputation"), username)
        rep.apply_ratings(data.get("ratings"))
        return rep


def normalize_nyaa_item(d: dict) -> dict | None:
    name = _pick(d, "name", "title")
    if not name:
        return None
    info_hash = str(_pick(d, "info_hash", "infoHash", default="")).lower()
    raw_size = _pick(d, "size")
    size = int(raw_size) if isinstance(raw_size, (int, float)) and not isinstance(raw_size, bool) \
        else parse_size_label(_pick(d, "sizeLabel", default=raw_size))
    return {
        "id": str(_pick(d, "id", default="") or info_hash),
        "name": str(name),
        "info_hash": info_hash,
        "magnet": _pick(d, "magnet", default="") or build_magnet(info_hash, str(name)),
        "seeders": to_int(d.get("seeders")),
        "leechers": to_int(d.get("leechers")),
        "size": size,
        "sizeLabel": _pick(d, "sizeLabel", default="") or format_size(size),
        "category": 200,
        "added": to_int(d.get("added")),
        "username": "",
        "status": "trusted" if to_bool(d.get("trusted")) else "",
        "source": "Nyaa",
        "pageUrl": str(_pick(d, "pageUrl", default="")),
    }


def parse_nyaa_results(data) -> list[dict]:
    raw = data.get("results") if isinstance(data, dict) else data
    if not isinstance(raw, list):
        return []
    return [n for n in (normalize_nyaa_item(i) for i in raw if isinstance(i, dict)) if n]
