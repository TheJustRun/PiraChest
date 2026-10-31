from __future__ import annotations

import contextvars
import html as _html
import re
import unicodedata
from dataclasses import dataclass, field
from functools import lru_cache
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
    clean_title: Optional[str] = None
    meta_text: Optional[str] = None
    poster_fallback: Optional[str] = None

    @property
    def display_title(self) -> str:
        # Always re-parse the raw title. Cached/clean_title values can come from
        # an older build with a different cleaning policy, and trusting them
        # made long names (for example "Call of Duty: Black Ops Cold War")
        # collapse to whatever a stale cache contained.
        raw = self.title or ""
        if raw:
            parsed = parse_title(raw)
            clean = (parsed.clean or raw).strip()
            if clean:
                return clean
        return self.clean_title or raw

    @property
    def title_meta(self) -> str:
        raw = self.title or ""
        if raw:
            meta = parse_title(raw).display_meta
            if meta:
                return meta
        return self.meta_text or ""

    @property
    def steam_poster(self) -> str:
        return self.poster_fallback or ""

    def to_dict(self) -> dict:
        return {
            "source": self.source,
            "title": self.title,
            "url": self.url,
            "poster_url": self.poster_url,
            "poster_path": self.poster_path,
            "slug": self.slug,
            "extra": self.extra,
            "clean_title": self.clean_title,
            "meta_text": self.meta_text,
            "poster_fallback": self.poster_fallback,
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
            clean_title=data.get("clean_title"),
            meta_text=data.get("meta_text"),
            poster_fallback=data.get("poster_fallback"),
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


# --- Title cleaning: clean game name + faded extras (edition, version, repack tags) ---
__all__ = ["ParsedTitle", "parse_title", "normalize_title", "matching_keys", "title_score", "title_year", "display_meta", "display_title", "search_text"]

_WS_RE = re.compile(r"\s+")
_YEAR_RE = re.compile(r"(?<![\w])((?:19|20)\d{2})(?![\w])")
_VERSION_RE = re.compile(
    r"\b(v(?:ersion)?\s*\d+(?:[.+_][A-Za-z0-9]+)*)\b"
    r"|\b(v[A-Za-z]{1,4}\d(?:[A-Za-z0-9._]*[A-Za-z0-9])?)\b"
    r"|\b(?:v(?:ersion)?\s*)?(\d+\.\d+(?:\.\d+){0,3})\b"
    r"|\b(build\s*\d{3,})\b"
    r"|\b(update\s*\d+(?:\.\d+)*)\b"
    r"|\b(patch\s*\d+(?:\.\d+)*)\b",
    re.IGNORECASE,
)
_VERSION_WORD_RE = re.compile(
    r"^(?:v(?:ersion)?\s*\d|v[A-Za-z]{1,4}\d|version\b|build\b|update\b|patch\b)", re.IGNORECASE)
_EDITION_GENERIC_RE = re.compile(
    r"\s*[\-–—:|,]\s*(?:the\s+)?(?P<edition>(?:[\w'’]+\s+){0,4}(?:edition|cut|goty|bundle|pack))\s*$",
    re.IGNORECASE)
_REPACK_TAIL_RE = re.compile(
    r"\s*[\-–—|:]\s*[^\-–—|:]*\b(?:fitgirl|repack|multi\d*|selective\s+download|elamigos)\b[^\-–—|:]*$",
    re.IGNORECASE)
_DLC_RE = re.compile(
    r"(?:\+\s*)?(?:(?:all|\d+)\s+)?(?:dlcs?|downloadable\s+content)\b"
    r"|(?:\+\s*)?(?:bonus\s+content|bonus\s+soundtrack|soundtrack|artbook|art\s+book|ost)\b",
    re.IGNORECASE,
)
_MULTI_RE = re.compile(r"\bmulti\s*[-_]?\s*\d+\b|\b(?:rus|eng|ger|fre|spa|ita|pol)\s*/\s*(?:rus|eng|ger|fre|spa|ita|pol)\b", re.IGNORECASE)
_REPACK_META_RE = re.compile(
    r"\b(?:fitgirl|repack|selective\s+download|elamigos|gog|iso|multi\d*|"
    r"lossless|not\s+repacked|language\s+pack|optional)\b",
    re.IGNORECASE,
)
_TRAILING_SEP_RE = re.compile(r"[\s\-–—:|/+&,;]+$")
_LEADING_SEP_RE = re.compile(r"^[\s\-–—:|/+&,;]+")
_PAREN_RE = re.compile(r"[\(\[]([^()\[\]]{1,160})[\)\]]")

_EDITION_PATTERNS = (
    r"digital\s+deluxe\s+edition",
    r"super\s+deluxe\s+edition",
    r"deluxe\s+edition",
    r"ultimate\s+edition",
    r"definitive\s+edition",
    r"complete\s+edition",
    r"enhanced\s+edition",
    r"premium\s+edition",
    r"gold\s+edition",
    r"special\s+edition",
    r"collector'?s\s+edition",
    r"limited\s+edition",
    r"anniversary\s+edition",
    r"legacy\s+edition",
    r"standard\s+edition",
    r"game\s+of\s+the\s+year(?:\s+edition)?",
    r"goty(?:\s+edition)?",
    r"director'?s\s+cut",
)
_EDITION_RE = re.compile(
    r"(?:[\s\-–—:|,]+(?:the\s+)?)?(?P<edition>"
    + "|".join(_EDITION_PATTERNS)
    + r")\s*$",
    re.IGNORECASE,
)
_EDITION_SINGLE_RE = re.compile(
    r"\b(?:" + "|".join(_EDITION_PATTERNS) + r"|\w+\s+edition)\b", re.IGNORECASE
)
_ROMAN_TO_DIGIT = {
    "ii": "2", "iii": "3", "iv": "4", "v": "5", "vi": "6", "vii": "7",
    "viii": "8", "ix": "9", "x": "10", "xi": "11", "xii": "12",
    "xiii": "13", "xiv": "14", "xv": "15",
}
_DIGIT_TO_ROMAN = {v: k for k, v in _ROMAN_TO_DIGIT.items()}


def _clean_spaces(text: str) -> str:
    return _WS_RE.sub(" ", _html.unescape(str(text or ""))).strip()


def _strip_seps(text: str) -> str:
    value = _LEADING_SEP_RE.sub("", _clean_spaces(text))
    return _TRAILING_SEP_RE.sub("", value).strip()


@lru_cache(maxsize=4096)
def normalize_title(value: str) -> str:
    """Return a comparison key for a title.

    Accents, punctuation, ``&``/``and`` and apostrophes are normalised, but
    every meaningful word (including sequel numbers and subtitles) remains.
    """
    text = unicodedata.normalize("NFKD", str(value or ""))
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = text.casefold().replace("&", " and ")
    text = text.replace("’", "").replace("'", "").replace("`", "")
    text = re.sub(r"\b(?:tm|r)\b", " ", text)
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return " ".join(text.split())


def matching_keys(value: str) -> tuple[str, ...]:
    """Normalised keys used by exact/alias matching.

    A trailing Roman numeral gets a digit twin (and vice versa) so ``GTA V``
    and ``GTA 5`` compare equal without rewriting the displayed title.
    """
    key = normalize_title(value)
    if not key:
        return ()
    keys = {key}
    tokens = key.split()
    if len(tokens) >= 2 and tokens[-1] in _ROMAN_TO_DIGIT:
        keys.add(" ".join(tokens[:-1] + [_ROMAN_TO_DIGIT[tokens[-1]]]))
    elif len(tokens) >= 2 and tokens[-1] in _DIGIT_TO_ROMAN:
        keys.add(" ".join(tokens[:-1] + [_DIGIT_TO_ROMAN[tokens[-1]]]))
    return tuple(sorted(keys))


def _has_meta(text: str) -> bool:
    return bool(
        _VERSION_RE.search(text)
        or _DLC_RE.search(text)
        or _MULTI_RE.search(text)
        or _REPACK_META_RE.search(text)
        or _EDITION_SINGLE_RE.search(text)
        or _YEAR_RE.fullmatch(text.strip())
    )


def _take_meta_groups(raw: str) -> tuple[str, list[str], str | None, int | None]:
    """Remove recognised metadata brackets and gather what they contained."""
    notes: list[str] = []
    version: str | None = None
    year: int | None = None

    def _replace(match: re.Match) -> str:
        nonlocal version, year
        content = match.group(1).strip()
        if not content or not _has_meta(content):
            return match.group(0)
        if _YEAR_RE.fullmatch(content):
            year = int(content)
            return " "
        vmatch = _VERSION_RE.search(content)
        if vmatch:
            version = version or _version_text(vmatch)
        if _DLC_RE.search(content):
            note = _VERSION_RE.sub("", content)
            note = re.sub(r"(?i)\b(?:v|version|build|update|patch)\b", " ", note)
            notes.append(_clean_note(note))
            return " "
        if _EDITION_SINGLE_RE.search(content):
            notes.append(_clean_note(content))
            return " "
        if _MULTI_RE.search(content) or _REPACK_META_RE.search(content):
            return " "
        if vmatch:
            leftover = _VERSION_RE.sub("", content)
            leftover = re.sub(r"(?i)\b(?:v|version|build|update|patch)\b", " ", leftover)
            if not _strip_seps(leftover):
                return " "
        return match.group(0)

    return _PAREN_RE.sub(_replace, raw), notes, version, year


def _version_text(match: re.Match) -> str:
    text = next((g for g in match.groups() if g), "") or match.group(0)
    text = _clean_spaces(text)
    text = re.sub(r"(?i)^version\s*", "v", text)
    text = re.sub(r"(?i)^v\s+", "v", text)
    if re.match(r"^\d", text):
        text = "v" + text
    return text


def _clean_note(text: str) -> str:
    value = _clean_spaces(text)
    value = re.sub(r"^\+?\s*", "", value)
    return _TRAILING_SEP_RE.sub("", value).strip()


def _extract_version(text: str) -> tuple[str, str | None]:
    match = _VERSION_RE.search(text)
    if match is None:
        return text, None
    version = _version_text(match)
    # Only remove a version token that looks like metadata, never a number
    # in the title itself (e.g. "Cyberpunk 2077").
    start, end = match.span()
    before = text[:start]
    after = text[end:]
    if _VERSION_WORD_RE.search(match.group(0)) or re.search(r"[\-–—:|,(]\s*$", before):
        return _strip_seps(before + " " + after), version
    return text, None


def _extract_plus_tail(text: str) -> tuple[str, list[str]]:
    match = next((m for m in re.finditer(r"\s\+\s*(.+)$", text)
                  if text[:m.start()].count("(") <= text[:m.start()].count(")")
                  and text[:m.start()].count("[") <= text[:m.start()].count("]")), None)
    if match is None:
        return text, []
    tail = match.group(1).strip()
    if not (_DLC_RE.search(tail) or _EDITION_SINGLE_RE.search(tail)):
        return text, []
    note = _clean_note(tail)
    return _strip_seps(text[:match.start()]), ([note] if note else [])


def _extract_edition(text: str) -> tuple[str, str | None]:
    match = _EDITION_RE.search(text) or _EDITION_GENERIC_RE.search(text)
    if match is None:
        return text, None
    edition = _clean_spaces(match.group("edition"))
    return _strip_seps(text[:match.start()]), edition


_DASH_SPLIT_RE = re.compile(r"\s[–—]\s")
_TRAIL_GROUP_RE = re.compile(r"\s*[\(\[]([^()\[\]]+)[\)\]]\s*$")
_SEG_SPLIT_RE = re.compile(r"\s*[:,|]\s+|\s+[\-–—]\s+")
_RELEASE_NOUNS = frozenset(
    "edition cut goty patch update pack pass dlc dlcs bundle content fix hotfix build version "
    "expansion soundtrack remaster remastered upgrade".split())
_EDITION_NOUNS = frozenset(("edition", "cut", "goty"))


def _release_parts(text: str, notes: list[str]) -> tuple[str | None, int | None]:
    """Everything after the title proper is release info: pull version/year, keep the rest as notes."""
    version: str | None = None
    year: int | None = None

    def _group(match: re.Match) -> str:
        nonlocal year, version
        content = match.group(1).strip()
        inner = _VERSION_RE.search(content)
        if _YEAR_RE.fullmatch(content):
            year = int(content)
        else:
            if inner:
                version = version or _version_text(inner)
            notes.extend(_split_notes(content))
        return " "

    text = _PAREN_RE.sub(_group, text)
    vmatch = _VERSION_RE.search(text)
    if vmatch:
        version = _version_text(vmatch)
        text = text[:vmatch.start()] + " " + text[vmatch.end():]
    notes.extend(_split_notes(text))
    return version, year


def _split_notes(text: str) -> list[str]:
    out = []
    for part in re.split(r"\s*\+\s*|\s*,\s*", _VERSION_RE.sub(" ", text)):
        part = _clean_note(re.sub(r"(?i)\b(?:v|version|build|update|patch)\b\s*$", "", _clean_note(part)))
        if part and not _REPACK_META_RE.search(part) and not _MULTI_RE.search(part):
            out.append(part)
    return out


def _looks_like_release(segment: str) -> bool:
    words = re.findall(r"[\w'’]+", segment.casefold())
    if not words or len(words) > 6:
        return False
    return (words[-1] in _RELEASE_NOUNS or bool(_VERSION_RE.search(segment))
            or bool(_DLC_RE.search(segment)) or bool(_EDITION_SINGLE_RE.search(segment)))


_PREFIX_RELEASE_WORDS = frozenset(
    "edition cut goty game of the year complete definitive ultimate deluxe enhanced anniversary "
    "remaster remastered reloaded directors director gold premium legendary collectors collection "
    "special digital standard launch day one content expansion season pass dlc dlcs bundle pack "
    "update patch build version fix hotfix next gen generation".split())


def _release_only_suffix(segment: str) -> bool:
    """True when a suffix consists only of release/edition words.

    ``Reloaded Edition`` and ``Game of the Year`` qualify; ``GO: Definitive
    Edition`` or ``Chicken Bundle`` do not, because their leading words are
    part of a different product name.
    """
    words = normalize_title(segment).split()
    return bool(words) and len(words) <= 8 and all(
        word in _PREFIX_RELEASE_WORDS or word.isdigit() for word in words)


def _peel_release_tail(text: str, notes: list[str]) -> tuple[str, str]:
    """Strip trailing '(…)' groups and ': / , ' segments that read like release info, not name."""
    edition = ""
    for _ in range(4):
        group = _TRAIL_GROUP_RE.search(text)
        if group and group.start() > 0:
            content = group.group(1).strip()
            if not _YEAR_RE.fullmatch(content):
                notes.extend(_split_notes(content))
            text = text[:group.start()]
            continue
        parts = list(_SEG_SPLIT_RE.finditer(text))
        if not parts:
            break
        last = parts[-1]
        segment = text[last.end():]
        if not _strip_seps(text[:last.start()]) or not _looks_like_release(segment):
            break
        words = re.findall(r"[\w'’]+", segment.casefold())
        if words[-1] in _EDITION_NOUNS and not edition:
            edition = _clean_spaces(segment)
        else:
            notes.append(_clean_note(segment))
        text = text[:last.start()]
    return _strip_seps(text), edition


def _clean_title(raw: str) -> str:
    value = _clean_spaces(raw)
    value = value.replace("’", "'")
    value = _strip_seps(value)
    return value


@dataclass(frozen=True)
class ParsedTitle:
    raw: str
    clean: str
    edition: str = ""
    version: str = ""
    extras: str = ""
    year: int | None = None
    aliases: tuple[str, ...] = field(default_factory=tuple)

    @property
    def key(self) -> str:
        return normalize_title(self.clean)

    @property
    def keys(self) -> tuple[str, ...]:
        out: set[str] = set(matching_keys(self.clean))
        for alias in self.aliases:
            out.update(matching_keys(alias))
        return tuple(sorted(out))

    @property
    def base_aliases(self) -> tuple[str, ...]:
        return self.aliases

    @property
    def display_meta(self) -> str:
        return display_meta(self)

    @property
    def search_text(self) -> str:
        return search_text(self)

    def to_dict(self) -> dict:
        return {
            "raw": self.raw,
            "clean": self.clean,
            "edition": self.edition,
            "version": self.version,
            "extras": self.extras,
            "year": self.year,
            "aliases": list(self.aliases),
        }

    @classmethod
    def from_dict(cls, data: dict | None) -> "ParsedTitle | None":
        if not isinstance(data, dict) or not data.get("clean"):
            return None
        return cls(
            raw=str(data.get("raw") or data.get("clean") or ""),
            clean=str(data["clean"]),
            edition=str(data.get("edition") or ""),
            version=str(data.get("version") or ""),
            extras=str(data.get("extras") or ""),
            year=data.get("year") if isinstance(data.get("year"), int) else None,
            aliases=tuple(str(a) for a in (data.get("aliases") or ()) if a),
        )


@lru_cache(maxsize=4096)
def parse_title(raw: str) -> ParsedTitle:
    original = _clean_title(raw)
    if not original:
        return ParsedTitle(raw="", clean="")

    text = original
    notes: list[str] = []
    version: str | None = None
    year: int | None = None

    # A spaced en/em dash separates the game name from release info.
    dash = _DASH_SPLIT_RE.search(text)
    if dash:
        version, year = _release_parts(text[dash.end():], notes)
        text = text[:dash.start()]

    text, group_notes, group_version, group_year = _take_meta_groups(text)
    notes.extend(group_notes)
    version = version or group_version
    year = year or group_year

    text, plus_notes = _extract_plus_tail(text)
    notes.extend(plus_notes)

    text, found_version = _extract_version(text)
    version = found_version or version

    stripped = _REPACK_TAIL_RE.sub("", text)
    if stripped.strip():
        text = stripped

    text = _strip_seps(text)
    text, tail_edition = _peel_release_tail(text, notes)
    text, edition = _extract_edition(text)
    edition = edition or tail_edition
    if not edition:
        for index, note in enumerate(notes):
            match = _EDITION_RE.search(note)
            if match is None:
                continue
            edition = _clean_spaces(match.group("edition"))
            remainder = _strip_seps(note[:match.start()])
            if remainder:
                notes[index] = remainder
            else:
                notes.pop(index)
            break

    # A year left as bare trailing metadata ("Game 2015") is unusual; keep it
    # in the title rather than risk cutting a number from the name.

    clean = _strip_seps(text) or original
    for opener, closer in ("()", "[]"):          # never leave a dangling bracket in the name
        if clean.count(opener) > clean.count(closer):
            clean = _strip_seps(clean[:clean.rindex(opener)]) or clean
    if not clean:
        clean = original

    notes_clean: list[str] = []
    seen: set[str] = set()
    for note in notes:
        note = _clean_note(note)
        key = normalize_title(note)
        if note and key and key not in seen:
            seen.add(key)
            notes_clean.append(note)
    extras = " · ".join(notes_clean)

    aliases: list[str] = []
    head = clean.split(":", 1)[0].strip() if ":" in clean else ""
    if head:
        words = re.findall(r"[\w'’]+", head)
        useful = len(words) >= 3 or any(
            re.fullmatch(r"(?:\d+|[ivx]+)", word, re.IGNORECASE) for word in words)
        if not useful:
            head = ""
    for candidate in (
        original,
        _strip_seps(re.sub(r"^(?:the)\s+", "", clean, flags=re.IGNORECASE)),
        head,
    ):
        candidate = _clean_spaces(candidate)
        if candidate and candidate.casefold() != clean.casefold():
            aliases.append(candidate)
    if edition:
        aliases.append(f"{clean} {edition}")
    aliases = tuple(dict.fromkeys(aliases))

    return ParsedTitle(
        raw=original,
        clean=clean,
        edition=edition or "",
        version=version or "",
        extras=extras,
        year=year,
        aliases=aliases,
    )


def display_meta(title: ParsedTitle) -> str:
    parts = [
        title.edition,
        title.version,
        title.extras,
        str(title.year) if title.year else "",
    ]
    return " · ".join(part for part in parts if part)


def display_title(value: str | ParsedTitle) -> str:
    parsed = value if isinstance(value, ParsedTitle) else parse_title(value)
    return parsed.clean or parsed.raw


def search_text(value: str | ParsedTitle) -> str:
    parsed = value if isinstance(value, ParsedTitle) else parse_title(value)
    parts = [parsed.raw, parsed.clean, parsed.edition, parsed.version, parsed.extras]
    if parsed.year:
        parts.append(str(parsed.year))
    parts.extend(parsed.aliases)
    return " ".join(dict.fromkeys(p for p in parts if p))


def _ratio(a: str, b: str) -> float:
    from difflib import SequenceMatcher

    return SequenceMatcher(None, a, b).ratio()


_YEAR_SUFFIX_RE = re.compile(r"[\(\[]\s*((?:19|20)\d{2})\s*[\)\]]\s*$")


def title_year(value: str) -> int | None:
    """Return a parenthesised/bracketed release year.

    Only a structured year counts, so names such as ``Cyberpunk 2077`` or
    ``Battlefield 1942`` keep their number as part of the title.
    """
    text = _clean_spaces(value)
    match = _YEAR_SUFFIX_RE.search(text)
    return int(match.group(1)) if match else None


def _without_year(value: str) -> str:
    return _strip_seps(_YEAR_SUFFIX_RE.sub("", _clean_spaces(value)))


def title_score(title: ParsedTitle, candidate: str, *, year: int | None = None) -> tuple[float, str]:
    """Score one Steam candidate against a parsed title.

    Priority: exact, exact + year, alias, edition, a release suffix on an
    otherwise exact name (``Reloaded Edition``, ``Remastered``), then fuzzy.
    A candidate that names a different year than the parsed title is never a
    match, which keeps remakes and originals apart.
    """
    cand_key = normalize_title(candidate)
    if not cand_key:
        return 0.0, "none"

    target_year = title.year if year is None else year
    cand_year = title_year(candidate)
    if target_year and cand_year is not None and cand_year != target_year:
        base = normalize_title(_without_year(candidate))
        if base and any(base == key or base.startswith(key + " ") for key in title.keys):
            return 0.0, "none"

    if cand_key in title.keys:
        method = "exact" if cand_key == title.key else "alias"
        score = 1.0 if method == "exact" else 0.96
        return score, method

    if target_year and cand_year == target_year:
        base = normalize_title(_without_year(candidate))
        if base in title.keys:
            return 1.0, "exact_year"

    edition_key = normalize_title(title.edition)
    if edition_key:
        cand_tokens = set(cand_key.split())
        edition_tokens = set(edition_key.split())
        if edition_tokens and edition_tokens <= cand_tokens:
            base_tokens = [t for t in cand_key.split() if t not in edition_tokens and t != "edition"]
            base_key = " ".join(base_tokens)
            if any(base_key == key or base_key.startswith(key + " ") for key in title.keys):
                return 0.94, "edition"
            if base_key and _ratio(base_key, title.key) >= 0.92:
                return 0.93, "edition"

    # Steam frequently appends release wording ('Remastered', 'Reloaded
    # Edition', 'Game of the Year') to an otherwise exact name.  Only accept
    # that when the remainder really reads like release information; a real
    # subtitle ('Separate Ways', 'Underworld') must not collapse into the base
    # game.
    for key in title.keys:
        if cand_key.startswith(key + " "):
            suffix = cand_key[len(key) + 1:]
            first = suffix.split()[:2]
            if any(token.isdigit() or token in _ROMAN_TO_DIGIT for token in first):
                continue
            if suffix and _release_only_suffix(suffix):
                return 0.95, "prefix"

    best = 0.0
    for key in title.keys:
        best = max(best, _ratio(key, cand_key))
    if best >= 0.95:
        return 0.90, "fuzzy"
    if best >= 0.92:
        return 0.88, "fuzzy"
    return 0.0, "none"
