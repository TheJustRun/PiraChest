from __future__ import annotations

import hashlib
import html as _html
import json
import logging
import re
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from functools import lru_cache
from urllib.parse import urlparse, urlsplit, urlunsplit

import httpx

from ..cache import cache
from ..config import settings
from ..net import LazyHttpClient, shared_ssl_context
from ..worker import Priority, submit
from .base import ParsedTitle, display_meta, normalize_title, parse_title, search_text, title_score, title_year

logger = logging.getLogger(__name__)

__all__ = [
    "GameEnrichment",
    "enrichment_enabled",
    "identity_for",
    "peek_enrichment",
    "needs_refresh",
    "enrich",
    "request_enrichment",
    "request_artwork",
    "refresh_enrichment",
    "search_candidates",
    "set_manual_match",
    "clear_match",
    "search_text_for",
    "clean_title_for",
    "display_meta_for",
    "cached_card_art",
    "resolve_appid",
    "clear_steam_cache",
    "safe_art_url",
    "sanitize_html",
    "plain_text",
]

_MIN_AUTO_SCORE = 0.88
_MIN_FUZZY_MARGIN = 0.05
_PROVIDER_TTL = 30 * 24 * 3600
_PROVIDER_EMPTY_TTL = 6 * 3600
APP_TTL = 7 * 24 * 3600
SEARCH_TTL = 24 * 3600
NOMATCH_TTL = 7 * 24 * 3600
_MAX_ABOUT = 30_000
_MAX_IMAGES = 8
_MAX_SEARCH = 10
_MAX_VALIDATE = 4
_YEAR_GRACE = 8          # PC ports/remasters may arrive years after the console release
_ART_MEMO_TTL = 3600

MAPPING_NS, APP_NS, SEARCH_NS, ART_NS, NOMATCH_NS = (
    "steam:mapping", "steam:app", "steam:search", "steam:artwork", "steam:nomatch")

_APP_URL = "https://store.steampowered.com/api/appdetails"
_SEARCH_URL = "https://store.steampowered.com/api/storesearch/"
# Key-backed artwork only; Steam metadata stays on its public endpoints.
_ART_PROXY = "https://tmdb-proxy.justrun.workers.dev"
_CDN = "https://shared.akamai.steamstatic.com/store_item_assets/steam/apps"
_CDN_OLD = "https://steamcdn-a.akamaihd.net/steam/apps"
_MAX_BODY = 2 * 1024 * 1024
_APPID_RE = re.compile(r"^\d{1,12}$")

_session = LazyHttpClient(
    headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                           "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
             "Accept": "application/json",
             "Accept-Language": "en-US,en;q=0.9"},
    follow_redirects=True, trust_env=False,
    timeout=httpx.Timeout(8.0, connect=4.0),
    limits=httpx.Limits(max_connections=4, max_keepalive_connections=2),
    transport_factory=lambda: httpx.HTTPTransport(retries=0, verify=shared_ssl_context()),
)
_net_slots = threading.BoundedSemaphore(3)   # caps concurrent Steam requests (CPU, 429s)

_IMAGE_HOSTS = {"cdn.steamgriddb.com", "cdn2.steamgriddb.com", "images.steamgriddb.com", "steamgriddb.com",
                "shared.akamai.steamstatic.com", "steamcdn-a.akamaihd.net", "cdn.cloudflare.steamstatic.com",
                "images.igdb.com", "store.steampowered.com"}
_MEDIA_HOSTS = {"steampowered.com", "steamcommunity.com", "steamstatic.com", "steamgriddb.com", "igdb.com"}
_LINK_HOSTS = ("steampowered.com", "steamcommunity.com", "steamstatic.com", "steamgriddb.com", "igdb.com")
_ADDON_RE = re.compile(
    r"\b(?:dlc|soundtrack|ost|demo|access\s+pass|expansion|bundle|pack|upgrade|bonus|"
    r"artbook|art\s+book|tools?|server|skin|cosmetic|costume|weapon|chapter)\b", re.IGNORECASE)
_DROP_TAGS = ("script", "style", "form", "input", "button", "textarea", "select", "option", "iframe", "frame",
              "frameset", "object", "embed", "applet", "video", "audio", "source", "track", "link", "meta",
              "noscript", "template", "svg", "math", "canvas")
_ALLOWED_TAGS = {"p", "br", "hr", "ul", "ol", "li", "strong", "b", "em", "i", "u", "s", "h1", "h2", "h3", "h4",
                 "blockquote", "div", "span", "img", "a"}


class _LRU:
    """Tiny thread-safe LRU so grid cards/search never hit the disk cache twice."""
    _MISS = object()

    def __init__(self, size: int):
        self._d: OrderedDict = OrderedDict()
        self._size = size
        self._lock = threading.Lock()

    def get(self, key):
        with self._lock:
            if key in self._d:
                self._d.move_to_end(key)
                return self._d[key]
        return self._MISS

    def put(self, key, value):
        with self._lock:
            self._d[key] = value
            self._d.move_to_end(key)
            while len(self._d) > self._size:
                self._d.popitem(last=False)

    def clear(self):
        with self._lock:
            self._d.clear()


_map_memo = _LRU(4096)
_app_memo = _LRU(32)
_art_memo = _LRU(128)


class SteamError(RuntimeError):
    pass


def enrichment_enabled() -> bool:
    return bool(getattr(settings, "steam_enrichment_enabled", False))


def _wants_about() -> bool:
    return bool(getattr(settings, "steam_description", False))


def artwork_proxy_base() -> str:
    value = str(getattr(settings, "steam_proxy_url", "") or "").strip() or _ART_PROXY
    if not value or len(value) > 2048:
        return ""
    try:
        parts = urlsplit(value)
    except ValueError:
        return ""
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return ""
    if parts.username or parts.password or parts.query or parts.fragment:
        return ""
    if parts.scheme == "http" and parts.hostname not in ("127.0.0.1", "::1", "localhost"):
        return ""
    if not re.fullmatch(r"(?:/[A-Za-z0-9._~-]+)*/?", parts.path or ""):
        return ""
    return urlunsplit((parts.scheme, parts.netloc, parts.path.rstrip("/"), "", ""))


def resolve_appid(value) -> int | None:
    text = str(value if value is not None else "").strip()
    if not _APPID_RE.fullmatch(text):
        return None
    appid = int(text)
    return appid if 0 < appid < 10**10 else None


def _absolute(value: str) -> str:
    value = str(value or "").strip()
    if value.startswith("//"):
        return "https:" + value
    if value.startswith("/"):
        return "https://store.steampowered.com" + value
    return value


def _host_ok(url: str, hosts, *, suffix: bool) -> bool:
    try:
        parsed = urlparse(url)
    except ValueError:
        return False
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        return False
    if parsed.username or parsed.password:
        return False
    host = parsed.hostname.casefold()
    if suffix:
        return any(host == h or host.endswith("." + h) for h in hosts)
    if host in hosts:
        return True
    return host.endswith(".steamstatic.com")


def safe_art_url(value) -> str:
    value = _absolute(value)
    return value[:2048] if value and _host_ok(value, _IMAGE_HOSTS, suffix=False) else ""


def safe_media_url(value) -> str:
    value = _absolute(value)
    return value[:2048] if value and _host_ok(value, _MEDIA_HOSTS, suffix=True) else ""


def safe_web_url(value) -> str:
    value = str(value or "").strip()[:2048]
    try:
        parsed = urlsplit(value)
    except ValueError:
        return ""
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        return ""
    if parsed.username or parsed.password:
        return ""
    return value


# --- HTTP ----------------------------------------------------------------
def _retry_after(value) -> float:
    try:
        return min(3.0, max(0.2, float(str(value or "1"))))
    except (TypeError, ValueError):
        return 1.0


def _get_json(url: str, params: dict | None = None):
    """Fetch JSON with a hard size cap.  Raises SteamError on transport failure."""
    last_error: Exception | None = None
    for attempt in range(2):
        delay = 0.0
        try:
            with _net_slots:
                with _session.stream("GET", url, params=params) as response:
                    status = response.status_code
                    if status == 429:
                        delay = _retry_after(response.headers.get("Retry-After"))
                    elif status >= 500:
                        delay = 0.4
                    else:
                        response.raise_for_status()
                        chunks: list[bytes] = []
                        total = 0
                        for chunk in response.iter_bytes(1 << 16):
                            total += len(chunk)
                            if total > _MAX_BODY:
                                raise SteamError("response too large")
                            chunks.append(chunk)
                        if not chunks:
                            raise SteamError("empty response")
                        return json.loads(b"".join(chunks))
        except SteamError as exc:
            if str(exc) == "response too large":
                raise
            last_error = exc
            delay = delay or 0.35
        except (httpx.HTTPError, ValueError) as exc:
            last_error = exc
            delay = delay or 0.35
        if attempt == 0 and delay:
            time.sleep(delay)
    logger.debug("Steam request failed for %s", urlparse(url).netloc or "provider")
    raise SteamError("Steam request failed") from last_error


# --- Payload shaping -----------------------------------------------------
def _year(value: str) -> int | None:
    m = re.search(r"\b((?:19|20)\d{2})\b", value or "")
    return int(m.group(1)) if m else None


def _movie_url(movie: dict) -> str:
    for kind in ("mp4", "webm"):
        files = movie.get(kind)
        if isinstance(files, dict):
            for key in ("max", "480", "360"):
                if files.get(key):
                    return str(files[key])
    for kind in ("hls_h264", "dash_h264", "dash_av1"):
        if movie.get(kind):
            return str(movie[kind])
    return ""


def _names(items) -> list[str]:
    return [str(i["description"]) for i in (items or ()) if isinstance(i, dict) and i.get("description")]


def normalize_appdetails(appid: int, data: dict) -> dict | None:
    name = str(data.get("name") or "").strip()[:300] if isinstance(data, dict) else ""
    app_type = str(data.get("type") or "game").casefold() if name else ""
    if app_type != "game":
        return None
    release = data.get("release_date") if isinstance(data.get("release_date"), dict) else {}
    meta = data.get("metacritic") if isinstance(data.get("metacritic"), dict) else {}
    header = safe_art_url(data.get("header_image"))
    bg = safe_art_url(data.get("background_raw") or data.get("background"))
    movies = []
    for movie in (data.get("movies") or ())[:4]:
        if not isinstance(movie, dict):
            continue
        url = safe_media_url(_movie_url(movie))
        if url:
            movies.append({"name": str(movie.get("name") or "Trailer")[:120],
                           "thumbnail": safe_art_url(movie.get("thumbnail")), "url": url})
    shots = []
    for shot in (data.get("screenshots") or ())[:10]:
        if isinstance(shot, dict):
            url = safe_art_url(shot.get("path_full") or shot.get("path_thumbnail"))
            if url:
                shots.append(url)
    return {
        "appid": int(appid), "name": name, "type": app_type,
        "short_description": str(data.get("short_description") or "")[:600],
        "about_html": str(data.get("about_the_game") or data.get("detailed_description") or "")[:_MAX_ABOUT * 2],
        "developers": [str(x)[:120] for x in (data.get("developers") or ())[:4] if x],
        "publishers": [str(x)[:120] for x in (data.get("publishers") or ())[:4] if x],
        "genres": [str(x)[:80] for x in _names(data.get("genres"))[:6]],
        "release_date": str(release.get("date") or "")[:40], "year": _year(str(release.get("date") or "")),
        "metacritic": int(meta["score"]) if str(meta.get("score") or "").isdigit() else None,
        "website": safe_web_url(data.get("website")),
        "artwork": {"banner": header, "hero": bg or header,
                    "capsule": safe_art_url(data.get("capsule_imagev5") or data.get("capsule_image") or header),
                    "background": bg},
        "screenshots": shots, "movies": movies,
    }


def _slim(p: dict) -> dict:
    """Compact what we persist: no derivable URLs, sanitised description only when it will be shown."""
    art = {}
    for key, value in (p.get("artwork") or {}).items():
        if key == "poster_fallback" or not isinstance(value, str) or not value.startswith(("http://", "https://")):
            continue
        url = safe_art_url(value)
        if url:
            art[key] = url
    shots = []
    for shot in p.get("screenshots") or ():
        url = (shot.get("full") or shot.get("thumb")) if isinstance(shot, dict) else shot
        url = safe_art_url(url)
        if url:
            shots.append(url)
    movies = []
    for movie in (p.get("movies") or ())[:4]:
        if not isinstance(movie, dict):
            continue
        url = safe_media_url(movie.get("url"))
        if url:
            movies.append({"name": str(movie.get("name") or "Trailer")[:120],
                           "thumbnail": safe_art_url(movie.get("thumbnail")), "url": url})
    with_about = _wants_about()
    out = {k: p.get(k) for k in ("appid", "name", "type", "short_description", "developers", "publishers",
                                 "genres", "release_date", "year", "metacritic", "website")}
    out.update(artwork=art, screenshots=shots[:10], movies=movies,
               about_html=sanitize_html(str(p.get("about_html") or "")) if with_about else "",
               with_about=with_about, fetched_at=time.time())
    return out


def _fetch_appdetails(appid: int) -> dict | None:
    raw = _get_json(_APP_URL, {"appids": appid, "cc": "us", "l": "english"})
    if not isinstance(raw, dict):
        return None
    entry = raw.get(str(appid))
    data = raw.get("data") if isinstance(raw.get("data"), dict) else (
        entry.get("data") if isinstance(entry, dict) else None)
    data = data if isinstance(data, dict) else raw
    if (data.get("name") and isinstance(data.get("artwork"), dict)
            and str(data.get("type") or "game").casefold() == "game"):
        return dict(data, appid=int(appid), name=str(data["name"])[:300])
    return normalize_appdetails(appid, data)


def _steam_search(query: str) -> list[dict] | None:
    try:
        raw = _get_json(_SEARCH_URL, {"term": query[:160], "cc": "us", "l": "english"})
    except SteamError:
        return None
    if not isinstance(raw, dict):
        return None                       # malformed provider response, not "no hits"
    items = raw.get("items")
    if items is None and isinstance(raw.get("data"), dict):
        items = raw["data"].get("items")
    out = []
    for item in items or ():
        if not isinstance(item, dict) or (item.get("type") and item.get("type") != "app"):
            continue
        appid, name = resolve_appid(item.get("id") or item.get("appid")), str(item.get("name") or "").strip()
        if appid and name:
            out.append({"appid": appid, "name": name[:300]})
            if len(out) >= _MAX_SEARCH:
                break
    return out


def _first_url(items, prefer=None) -> str:
    best, best_score = "", -1.0
    for item in items if isinstance(items, list) else ():
        url = safe_art_url(item.get("url") if isinstance(item, dict) else item)
        if not url:
            continue
        score = 0.0
        if prefer and isinstance(item, dict):
            try:
                w, h = float(item.get("width") or 0), float(item.get("height") or 0)
            except (TypeError, ValueError):
                w = h = 0.0
            if w > 0 and h > 0:
                score = max(0.0, 1.0 - abs(w / h - prefer))
        if score > best_score:
            best, best_score = url, score
    return best


def _provider_art(appid: int, *, allow_network: bool) -> dict:
    base = artwork_proxy_base()
    if not base:
        return {}
    key = str(int(appid))
    memo = _art_memo.get(key)
    if memo is not _LRU._MISS and time.time() - float(memo.get("at") or 0) <= _ART_MEMO_TTL:
        return dict(memo.get("art") or {})
    art: dict = {}
    cached = cache.get_entry(ART_NS, key)
    if isinstance(cached, dict):
        art = dict(cached.get("artwork") or {})
        ttl = _PROVIDER_TTL if art else _PROVIDER_EMPTY_TTL
        if time.time() - float(cached.get("fetched_at") or 0) <= ttl:
            _art_memo.put(key, {"at": time.time(), "art": art})
            return art
    if not allow_network:
        return art          # stale artwork still beats an empty card
    try:
        data = _get_json(f"{base}/steamgriddb/artwork", {"appid": int(appid)})
    except SteamError:
        return art          # provider outage keeps the previous artwork
    if not isinstance(data, dict):
        return art
    fresh = {}
    for name, urls, prefer in (
            ("poster", data.get("grids"), 2 / 3), ("hero", data.get("heroes"), 16 / 9),
            ("banner", data.get("heroes"), 16 / 9), ("logo", data.get("logos"), None),
            ("icon", data.get("icons"), None)):
        url = _first_url(urls, prefer)
        if url:
            fresh[name] = url
    cache.set_entry(ART_NS, key, {"artwork": fresh, "fetched_at": time.time()})
    _art_memo.put(key, {"at": time.time(), "art": fresh})
    return fresh


@lru_cache(maxsize=8)
def sanitize_html(html: str, max_chars: int = _MAX_ABOUT) -> str:
    text = str(html or "")[:max_chars]
    if not text:
        return ""
    from bs4 import BeautifulSoup, Comment      # bs4 only; no bleach/html5lib import cost

    try:
        soup = BeautifulSoup(text, "html.parser")
    except Exception:
        return ""
    for tag in soup.find_all(_DROP_TAGS):
        tag.decompose()
    for comment in soup.find_all(string=lambda v: isinstance(v, Comment)):
        comment.extract()
    images = 0
    links = 0
    for tag in soup.find_all(True):
        if tag.name not in _ALLOWED_TAGS:
            tag.unwrap()
        elif tag.name == "a":
            href = _absolute(str(tag.get("href") or "").strip())
            links += 1
            if links <= 64 and _host_ok(href, _LINK_HOSTS, suffix=True):
                tag.attrs = {"href": href}
            else:
                tag.unwrap()
        elif tag.name == "img":
            src = _absolute(str(tag.get("src") or tag.get("data-src") or "").strip())
            if not src:
                srcset = str(tag.get("srcset") or "").strip()
                src = _absolute(srcset.split(",", 1)[0].strip().split(" ", 1)[0]) if srcset else ""
            src = safe_art_url(src)
            images += 1
            if not src or images > _MAX_IMAGES:
                tag.decompose()
            else:
                tag.attrs = {"src": src, "alt": str(tag.get("alt") or "")[:200]}
        else:
            tag.attrs = {}
    return str(soup).strip()


def plain_text(html: str, limit: int = 2000) -> str:
    if not html:
        return ""
    return " ".join(_html.unescape(re.sub(r"<[^>]+>", " ", str(html))).split())[:limit]


# --- Cache layer ---------------------------------------------------------
def _identity_key(parsed: ParsedTitle, url: str = "") -> str:
    if not parsed.key:
        return hashlib.sha1((url or parsed.raw).strip().casefold().encode("utf-8", "ignore")).hexdigest()
    # Same-name remakes must not share a mapping ("Resident Evil 4 (2005)"
    # vs "(2023)"), while editions/versions of one release still do.
    return f"{parsed.key}:{parsed.year}" if parsed.year else parsed.key


def _load_mapping(parsed: ParsedTitle, url: str = "") -> dict | None:
    key = _identity_key(parsed, url)
    hit = _map_memo.get(key)
    if hit is not _LRU._MISS:
        return hit
    data = cache.get_entry(MAPPING_NS, key)
    appid = resolve_appid(data.get("appid")) if isinstance(data, dict) else None
    result = dict(data, appid=appid) if appid else None
    _map_memo.put(key, result)
    return result


def _save_mapping(parsed: ParsedTitle, mapping: dict, *, manual: bool = False, url: str = "",
                  payload: dict | None = None) -> None:
    key = _identity_key(parsed, url)
    existing = _load_mapping(parsed, url)
    if existing and existing.get("manual") and not manual:
        return
    appid = resolve_appid(mapping.get("appid"))
    if appid is None:
        return
    data = dict(mapping, appid=appid, manual=bool(manual or mapping.get("manual")), updated=time.time())
    if payload:      # compact blob so grid search never needs the full app payload
        data["search"] = " ".join(filter(None, [payload.get("name"), *(payload.get("developers") or ()),
                                                 *(payload.get("publishers") or ()), *(payload.get("genres") or ())]))
    cache.set_entry(MAPPING_NS, key, data)
    _map_memo.put(key, data)


def _load_app(appid: int) -> dict | None:
    hit = _app_memo.get(appid)
    if hit is not _LRU._MISS:
        return hit
    data = cache.get_entry(APP_NS, str(int(appid)))
    data = data if isinstance(data, dict) else None
    if data and str(data.get("type") or "game").casefold() == "game":
        _app_memo.put(appid, data)
        return data
    return None


def _usable(payload: dict | None) -> bool:
    if not payload:
        return False
    try:
        fresh = time.time() - float(payload.get("fetched_at") or 0) <= APP_TTL
    except (TypeError, ValueError):
        return False
    return fresh and (not _wants_about() or bool(payload.get("with_about")))


def _load_nomatch(parsed: ParsedTitle, url: str = "") -> bool:
    return isinstance(cache.load(NOMATCH_NS, _identity_key(parsed, url), ttl_seconds=NOMATCH_TTL), dict)


def _save_nomatch(parsed: ParsedTitle, url: str = "") -> None:
    cache.save(NOMATCH_NS, _identity_key(parsed, url), {"query": parsed.clean, "updated": time.time()})


# --- Model ---------------------------------------------------------------
@dataclass
class GameEnrichment:
    parsed: ParsedTitle
    appid: int
    steam_title: str
    confidence: float
    method: str
    manual: bool
    payload: dict = field(default_factory=dict)
    artwork: dict = field(default_factory=dict)
    cached: bool = False

    def _list(self, key): return list(self.payload.get(key) or [])
    developers = property(lambda self: self._list("developers"))
    publishers = property(lambda self: self._list("publishers"))
    genres = property(lambda self: self._list("genres"))
    release_date = property(lambda self: str(self.payload.get("release_date") or ""))
    short_description = property(lambda self: str(self.payload.get("short_description") or ""))
    meta_text = property(lambda self: display_meta(self.parsed))
    summary_text = property(lambda self: plain_text(self.short_description, limit=500))

    @property
    def year(self) -> int | None:
        v = self.payload.get("year")
        return v if isinstance(v, int) else None

    @property
    def metacritic(self) -> int | None:
        v = self.payload.get("metacritic")
        return v if isinstance(v, int) else None

    @property
    def about_html(self) -> str:
        return str(self.payload.get("about_html") or "") if self.payload.get("with_about") else \
            sanitize_html(str(self.payload.get("about_html") or ""))

    @property
    def website(self) -> str:
        v = str(self.payload.get("website") or "")
        return v if v.startswith(("http://", "https://")) else ""

    @property
    def screenshots(self) -> list[str]:
        return [u for u in map(str, self.payload.get("screenshots") or ()) if u.startswith(("http://", "https://"))]

    @property
    def movies(self) -> list[dict]:
        return [{"name": str(m.get("name") or "Trailer"), "thumbnail": str(m.get("thumbnail") or ""),
                 "url": str(m["url"])} for m in self.payload.get("movies") or ()
                if isinstance(m, dict) and str(m.get("url") or "").startswith(("http://", "https://"))]


def _entry_url(obj) -> str:
    return str(getattr(obj, "url", "") or "")


def _identity(obj) -> ParsedTitle:
    return parse_title(str(getattr(obj, "title", "") or ""))


identity_for = _identity


def _build(parsed: ParsedTitle, mapping: dict, payload: dict, *, cached: bool) -> GameEnrichment:
    appid = int(mapping.get("appid") or payload.get("appid") or 0)
    art = {"poster": f"{_CDN}/{appid}/library_600x900.jpg", "poster_fallback": f"{_CDN_OLD}/{appid}/library_600x900.jpg",
           "logo": f"{_CDN}/{appid}/logo.png"}
    art.update(payload.get("artwork") or {})
    for k, v in _provider_art(appid, allow_network=False).items():
        if v:
            art[k] = v
    return GameEnrichment(parsed, appid, str(payload.get("name") or mapping.get("title") or ""),
                          float(mapping.get("confidence") or 0.0), str(mapping.get("method") or "auto"),
                          bool(mapping.get("manual")), payload, art, cached)


def peek_enrichment(obj) -> GameEnrichment | None:
    if not enrichment_enabled():
        return None
    try:
        parsed = _identity(obj)
        mapping = _load_mapping(parsed, _entry_url(obj))
        payload = _load_app(mapping["appid"]) if mapping else None
        return _build(parsed, mapping, payload, cached=True) if mapping and payload else None
    except Exception:
        logger.debug("Could not read cached enrichment", exc_info=True)
        return None


def needs_refresh(obj) -> bool:
    if not enrichment_enabled():
        return False
    try:
        parsed = _identity(obj)
        mapping = _load_mapping(parsed, _entry_url(obj))
        if mapping is None:
            return not _load_nomatch(parsed, _entry_url(obj))
        return not _usable(_load_app(mapping["appid"]))
    except Exception:
        return False


# --- Matching ------------------------------------------------------------
_app_locks: dict[int, threading.Lock] = {}
_app_locks_guard = threading.Lock()


def _app_lock(appid: int) -> threading.Lock:
    with _app_locks_guard:
        if len(_app_locks) > 128:
            _app_locks.clear()
        return _app_locks.setdefault(appid, threading.Lock())


def _fetch_and_store(appid: int, *, force: bool = False) -> dict | None:
    with _app_lock(appid):
        payload = _load_app(appid)
        if not force and _usable(payload):
            return payload
        fresh = _fetch_appdetails(appid)
        if fresh:
            fresh = _slim(fresh)
            cache.set_entry(APP_NS, str(appid), fresh)
            _app_memo.put(appid, fresh)
            return fresh
        return payload


def _rank_candidates(parsed: ParsedTitle, candidates: list[dict]) -> list[dict]:
    """Score and order Steam search hits, demoting DLC and add-on entries."""
    ranked: list[dict] = []
    seen: set[int] = set()
    for raw in candidates:
        appid = resolve_appid(raw.get("appid"))
        name = str(raw.get("name") or "").strip()
        if not appid or not name or appid in seen:
            continue
        seen.add(appid)
        score, method = title_score(parsed, name)
        if score <= 0:
            continue
        cand_key = normalize_title(name)
        suffix = ""
        for key in parsed.keys:
            if cand_key.startswith(key + " "):
                suffix = cand_key[len(key) + 1:]
                break
        # A release suffix may legitimately trail the name ('Reloaded
        # Edition'), but a DLC/add-on suffix ('Chicken Bundle', 'Developer
        # Tools') must not win just because Steam indexed it under the game.
        if method != "exact" and suffix and _ADDON_RE.search(suffix):
            score = max(0.0, score - 0.30)
            if score < _MIN_AUTO_SCORE:
                continue
        ranked.append({"appid": appid, "name": name[:300], "score": score, "method": method})
    ranked.sort(key=lambda item: (-item["score"], item["name"].casefold()))
    return ranked


def _validate_candidate(parsed: ParsedTitle, candidate: dict) -> dict | None:
    """Load appdetails and reject non-games and contradicted release years."""
    payload = _fetch_and_store(int(candidate["appid"]))
    if not isinstance(payload, dict):
        return None                       # DLC, soundtrack, bundle, or fetch failure
    name_year = title_year(str(payload.get("name") or candidate.get("name") or ""))
    if parsed.year:
        # Trust an explicit year in Steam's own title (Resident Evil 4 (2005)
        # lists a 2014 PC release date).  Otherwise allow PC-port delays but
        # reject a remake/reboot from another era.
        if name_year != parsed.year:
            year = payload.get("year")
            if isinstance(year, int) and abs(year - parsed.year) > _YEAR_GRACE:
                return None
    return payload


def _match_candidates(parsed: ParsedTitle, candidates: list[dict]) -> dict | None:
    ranked = _rank_candidates(parsed, candidates)
    checked = 0
    for candidate in ranked:
        if candidate["score"] < _MIN_AUTO_SCORE or checked >= _MAX_VALIDATE:
            break
        if candidate["method"] == "fuzzy":
            peers = [c for c in ranked if c is not candidate
                     and abs(c["score"] - candidate["score"]) < _MIN_FUZZY_MARGIN]
            if peers:
                continue                  # ambiguous fuzzy hits are not a match
        payload = _validate_candidate(parsed, candidate)
        if payload is not None:
            return {**candidate, "payload": payload}
        checked += 1
    return None


def _search_cached(query: str) -> list[dict] | None:
    """Cached Steam search.  ``None`` means a transport failure, not "no hits"."""
    query = " ".join(str(query or "").split())
    if len(query) < 2:
        return []
    key = f"us:{query.casefold()}"
    cached = cache.load(SEARCH_NS, key, ttl_seconds=SEARCH_TTL)
    if isinstance(cached, list):
        return cached
    items = _steam_search(query)
    if items is not None:
        cache.save(SEARCH_NS, key, items)
    return items


def _queries(parsed: ParsedTitle) -> list[str]:
    """Year-first query order keeps remakes and originals apart."""
    out: list[str] = []
    if parsed.year:
        out.append(f"{parsed.clean} {parsed.year}")
    out.append(parsed.clean)
    out.extend(parsed.aliases)
    seen: set[str] = set()
    queries = []
    for query in out:
        q = " ".join(re.sub(r"[–—]", " ", query).split())
        key = q.casefold()
        if len(q) >= 2 and key not in seen:
            seen.add(key)
            queries.append(q)
        if len(queries) >= 3:
            break
    return queries


def _find_match(parsed: ParsedTitle) -> tuple[dict | None, bool]:
    """Return ``(match, failed)``; a failed query must not become no-match cache."""
    failed = False
    for query in _queries(parsed):
        items = _search_cached(query)
        if items is None:
            failed = True
            continue
        chosen = _match_candidates(parsed, items)
        if chosen is not None:
            return chosen, failed
    return None, failed


def _auto_enrich(parsed: ParsedTitle, url: str) -> GameEnrichment | None:
    chosen, failed = _find_match(parsed)
    if chosen is None:
        if not failed:
            _save_nomatch(parsed, url)
        return None
    payload = chosen["payload"]
    mapping = {"appid": int(chosen["appid"]), "title": str(payload.get("name") or chosen["name"]),
               "confidence": round(float(chosen["score"]), 3), "method": chosen["method"]}
    _save_mapping(parsed, mapping, url=url, payload=payload)
    return _build(parsed, mapping, payload, cached=False)


def enrich(obj, *, refresh: bool = False) -> GameEnrichment | None:
    if not enrichment_enabled():
        return None
    parsed = _identity(obj)
    if not parsed.clean:
        return None
    url = _entry_url(obj)
    try:
        mapping = _load_mapping(parsed, url)
        if mapping is not None:
            payload = _fetch_and_store(int(mapping["appid"]), force=refresh)
            return _build(parsed, mapping, payload, cached=not refresh) if payload else None
        if not refresh and _load_nomatch(parsed, url):
            return None
        return _auto_enrich(parsed, url)
    except Exception:
        logger.warning("Steam enrichment failed for %r", parsed.clean, exc_info=True)
        return peek_enrichment(obj)


def _submit(obj, owner, on_done, on_error, *, refresh: bool):
    parsed = _identity(obj)
    if not enrichment_enabled() or on_done is None or not parsed.clean:
        return None

    def _task(is_cancelled=None):
        return None if is_cancelled is not None and is_cancelled() else enrich(obj, refresh=refresh)

    return submit(_task, on_done=on_done, on_error=on_error, priority=Priority.VISIBLE, owner=owner,
                  key=f"steam:{'refresh' if refresh else 'lookup'}:{_identity_key(parsed, _entry_url(obj))}")


def request_enrichment(obj, owner, on_done, on_error=None):
    return _submit(obj, owner, on_done, on_error, refresh=False)


def request_artwork(appid: int, owner, on_done, on_error=None):
    """Fetch key-backed artwork without delaying the Steam metadata result."""
    if not enrichment_enabled():
        return None
    resolved = resolve_appid(appid)
    if resolved is None or on_done is None:
        return None

    def _task(is_cancelled=None):
        if is_cancelled is not None and is_cancelled():
            return {}
        return _provider_art(resolved, allow_network=True)

    return submit(_task, on_done=on_done, on_error=on_error,
                  priority=Priority.NORMAL, owner=owner, key=f"steam:art:{resolved}")


def refresh_enrichment(obj, owner, on_done, on_error=None):
    return _submit(obj, owner, on_done, on_error, refresh=True)


def search_candidates(query: str) -> list[dict]:
    if not enrichment_enabled():
        return []
    return _search_cached(query) or []


def set_manual_match(obj, appid: int) -> GameEnrichment | None:
    if not enrichment_enabled():
        return None
    appid = resolve_appid(appid)
    parsed = _identity(obj)
    if appid is None or not parsed.clean:
        return None
    url = _entry_url(obj)
    payload = _fetch_and_store(appid)
    if not payload:                    # DLC, non-game, or failed fetch: keep the old mapping
        return None
    mapping = {"appid": appid, "title": str(payload.get("name") or ""), "confidence": 1.0,
               "method": "manual", "manual": True}
    _save_mapping(parsed, mapping, manual=True, url=url, payload=payload)
    cache.delete_entry(NOMATCH_NS, _identity_key(parsed, url))
    return _build(parsed, mapping, payload, cached=False)


def clear_match(obj) -> None:
    parsed = _identity(obj)
    url = _entry_url(obj)
    key = _identity_key(parsed, url)
    cache.delete_entry(MAPPING_NS, key)
    _map_memo.put(key, None)
    _save_nomatch(parsed, url)


def clear_steam_cache() -> int:
    """Drop Steam mappings/payloads; image files and FitGirl data are untouched."""
    removed = cache.count_namespace("steam")
    cache.clear_namespace("steam")
    for memo in (_map_memo, _app_memo, _art_memo):
        memo.clear()
    return removed


# --- Cheap helpers for grid cards / search (mapping memo only, no payload loads) ----
def search_text_for(obj) -> str:
    parsed = _identity(obj)
    parts = [search_text(parsed), str(getattr(obj, "title", "") or "")]
    if enrichment_enabled():
        try:
            mapping = _load_mapping(parsed, _entry_url(obj))
            if mapping:
                parts += [str(mapping.get("search") or ""), str(mapping.get("title") or "")]
        except Exception:
            pass
    return " ".join(dict.fromkeys(p for p in parts if p))


def clean_title_for(obj) -> str:
    return _identity(obj).clean


def display_meta_for(obj) -> str:
    return _identity(obj).display_meta


def cached_card_art(obj) -> str:
    if not enrichment_enabled() or getattr(obj, "poster_path", None) or getattr(obj, "poster_url", None):
        return ""
    try:
        mapping = _load_mapping(_identity(obj), _entry_url(obj))
        if not mapping:
            return ""
        appid = int(mapping["appid"])
        art = _provider_art(appid, allow_network=False)
        return art.get("poster") or f"{_CDN}/{appid}/library_600x900.jpg"
    except Exception:
        return ""
