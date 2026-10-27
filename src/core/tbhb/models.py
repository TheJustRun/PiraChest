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

    @property
    def category_name(self) -> str:
        return category_bucket_name(self.category)

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
            category=to_int(d.get("category")),
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

    @property
    def category_name(self) -> str:
        return category_bucket_name(self.category)

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
        )

    @classmethod
    def from_api(cls, d: dict) -> "TBHBDetails":
        t = d.get("torrent") if isinstance(d.get("torrent"), dict) else d
        title = t.get("name") or "Unknown"
        info_hash = str(t.get("info_hash") or "")
        size = to_int(t.get("size"))
        return cls(
            title=title,
            category=to_int(t.get("category")),
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
        )

    def fill_missing_from(self, e: TBHBEntry) -> None:
        if not self.extra.get("magnet_url"):
            self.extra["magnet_url"] = e.magnet
        for attr in ("username", "category", "size_label", "info_hash", "size", "added", "source", "status"):
            if not getattr(self, attr):
                setattr(self, attr, getattr(e, attr))
        if not (self.seeders or self.leechers):
            self.seeders, self.leechers = e.seeders, e.leechers
