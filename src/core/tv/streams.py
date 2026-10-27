from __future__ import annotations

import asyncio
import logging
import re
import time
import urllib.parse
from dataclasses import dataclass, field
from typing import Optional

import httpx

from ..config import settings

logger = logging.getLogger(__name__)

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
       "Chrome/124.0.0.0 Safari/537.36")
_TIMEOUT = 15.0

_PUBLIC_TRACKERS = (
    "udp://tracker.opentrackr.org:1337/announce",
    "udp://open.stealth.si:80/announce",
    "udp://tracker.torrent.eu.org:451/announce",
    "udp://open.demonii.com:1337/announce",
    "udp://exodus.desync.com:6969/announce",
)

_SEEDERS_RE = re.compile(r"👤\s*(\d+)")
_SIZE_RE = re.compile(r"💾\s*([\d.]+)\s*(TB|GB|MB|KB)")
_INDEXER_RE = re.compile(r"⚙️\s*([^\n]+)")
_TRACKER_RE = re.compile(r"(?m)^\s*tracker:\s*(\S+)")
_VIDEO_RE = re.compile(r"\.(mkv|mp4|avi|m4v|mov|webm|ts|m2ts|wmv|flv|mpg|mpeg)$", re.I)
_SUB_RE = re.compile(r"\.(srt|ass|ssa|vtt|sub)$", re.I)
_DIRECT_VIDEO_RE = re.compile(r"\.(mkv|mp4|m4v|mov|webm|m3u8|mpd|ts|m2ts)$", re.I)

_QUALITY_PATTERNS = (
    ("4k", re.compile(r"(?:^|[^a-z0-9])(?:2160p|4k|uhd)(?:[^a-z0-9]|$)", re.I)),
    ("1080p", re.compile(r"1080p?", re.I)),
    ("720p", re.compile(r"720p?", re.I)),
    ("480p", re.compile(r"(?:480p?|sd)", re.I)),
)


_QUALITY_WEIGHT = {"1080p": 1.0, "720p": 0.9, "4k": 0.8, "480p": 0.55, "": 0.7}
_PACK_RE = re.compile(r"(?:\bcomplete\b|\bseason\s*\d+|\bs\d{1,2}\b(?!\s*e\d))", re.I)


@dataclass(slots=True)
class StreamSource:
    source: str
    info_hash: str = ""
    name: str = ""
    magnet: str = ""
    file_idx: Optional[int] = None
    filename: str = ""
    size: int = 0
    seeders: int = 0
    quality: str = ""
    indexer: str = ""
    pack: bool = False
    url: str = ""
    headers: dict = field(default_factory=dict)
    subtitles: list = field(default_factory=list)
    referer: str = ""
    is_live: bool = False

    @property
    def is_direct(self) -> bool:
        return bool(self.url)

    @property
    def key(self) -> str:
        if self.url:
            return f"url:{self.url}"
        return f"{self.info_hash.lower()}:{self.file_idx if self.file_idx is not None else '-'}"

    @property
    def label(self) -> str:
        return self.filename or self.name or self.info_hash or self.url

    @property
    def detail(self) -> str:
        parts = [self.quality or "unknown"]
        if self.size:
            parts.append(human_size(self.size))
        if self.seeders:
            parts.append(f"{self.seeders} seeders")
        if self.is_direct:
            parts.append("direct")
        if self.indexer:
            parts.append(self.indexer)
        if self.pack:
            parts.append("season pack")
        return " · ".join(part for part in parts if part)


def human_size(size: int) -> str:
    value = float(size or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024
    return f"{value:.1f} TB"


def magnet_for(info_hash: str, name: str, trackers: tuple[str, ...] = ()) -> str:
    query = ["xt=urn:btih:" + info_hash, "dn=" + urllib.parse.quote(name or info_hash)]
    query += ["tr=" + urllib.parse.quote(tracker) for tracker in trackers]
    return "magnet:?" + "&".join(query)


def quality_of(text: str) -> str:
    for label, pattern in _QUALITY_PATTERNS:
        if pattern.search(text or ""):
            return label
    return ""


def looks_like_video(name: str) -> bool:
    return bool(_VIDEO_RE.search(name or ""))


def stream_score(source: StreamSource) -> float:
    weight = _QUALITY_WEIGHT.get(source.quality, 0.7)
    if source.is_direct:
        direct_weight = {"4k": 1.0, "1080p": 1.0, "720p": 0.95, "480p": 0.9, "240p": 0.5}.get(
            source.quality, 0.7)
        return 10_000.0 + direct_weight * 100.0 + (1.0 if source.filename else 0.0)
    score = float(source.seeders) * weight
    if source.size > 25 * 1024 ** 3:
        score *= 0.7
    if source.size > 60 * 1024 ** 3:
        score *= 0.7
    if source.filename:
        score *= 1.05
    return score


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        timeout=_TIMEOUT, follow_redirects=True, trust_env=False,
        headers={"User-Agent": _UA, "Accept": "application/json,text/plain,*/*"},
    )


async def find_streams(imdb_id: str, media_type: str, season: Optional[int] = None,
                       episode: Optional[int] = None, title: str = "",
                       year: str = "") -> list[StreamSource]:
    has_imdb = bool(imdb_id) and str(imdb_id).startswith("tt")
    if not has_imdb and not title:
        return []

    providers: list = []
    if title and getattr(settings, "tv_tbhb_enabled", True):
        providers.append(_tbhb(title, year, media_type, season, episode))
    if has_imdb:
        base = (getattr(settings, "tv_stream_addon_base", "") or "https://torrentio.strem.fun").rstrip("/")
        providers.append(_stremio_addon(base, imdb_id, media_type, season, episode, "torrentio"))
        providers.extend(
            _stremio_addon(base_url, imdb_id, media_type, season, episode, "addon")
            for base_url in _extra_addon_bases(base)
        )
        providers.append(_apibay(imdb_id, media_type, season, episode))

    results = await asyncio.gather(*providers, return_exceptions=True)

    merged: dict[str, StreamSource] = {}
    for result in results:
        if isinstance(result, BaseException):
            logger.info("Stream provider failed: %s: %s", type(result).__name__, result)
            continue
        for source in result:
            current = merged.get(source.key)
            if current is None or _is_better(source, current):
                merged[source.key] = source
    return sorted(merged.values(), key=stream_score, reverse=True)


def _extra_addon_bases(primary: str = "") -> list[str]:
    raw = getattr(settings, "tv_stream_addons", "") or ""
    bases: list[str] = []
    for part in re.split(r"[\s,;]+", raw):
        base = part.strip().rstrip("/")
        if not base.startswith(("http://", "https://")):
            continue
        if base.lower().endswith("/manifest.json"):
            base = base[: -len("/manifest.json")].rstrip("/")
        if base and base != primary and base not in bases:
            bases.append(base)
    return bases


def _is_better(candidate: StreamSource, current: StreamSource) -> bool:
    if candidate.is_direct != current.is_direct:
        return candidate.is_direct
    if candidate.is_direct:
        if bool(candidate.filename) != bool(current.filename):
            return bool(candidate.filename)
        return stream_score(candidate) > stream_score(current)
    if bool(candidate.filename) != bool(current.filename):
        return bool(candidate.filename)
    if (candidate.file_idx is not None) != (current.file_idx is not None):
        return candidate.file_idx is not None
    return candidate.seeders > current.seeders


async def _stremio_addon(base: str, imdb_id: str, media_type: str, season: Optional[int],
                         episode: Optional[int], source_name: str) -> list[StreamSource]:
    ref = f"movie/{imdb_id}" if media_type == "movie" else f"series/{imdb_id}:{int(season or 1)}:{int(episode or 1)}"
    url = f"{base}/stream/{ref}.json"
    async with _client() as client:
        response = await client.get(url)
        response.raise_for_status()
        payload = response.json()
    host = urllib.parse.urlsplit(base).netloc or base
    return _parse_stremio(payload if isinstance(payload, dict) else {}, source_name=source_name,
                          default_indexer=host if source_name == "addon" else "")


def _parse_stremio(payload: dict, source_name: str = "torrentio",
                   default_indexer: str = "") -> list[StreamSource]:
    sources: list[StreamSource] = []
    for stream in payload.get("streams") or []:
        if not isinstance(stream, dict):
            continue
        info_hash = (stream.get("infoHash") or "").strip()
        direct_url = str(stream.get("url") or "").strip()
        title = stream.get("title") or ""
        lines = [line.strip() for line in title.split("\n") if line.strip()]
        behavior = stream.get("behaviorHints") or {}
        filename = behavior.get("filename") or ""
        if not filename:
            filename = next((line for line in lines[1:] if _is_filename(line)), "")
        quality = quality_of((stream.get("name") or "") + " " + title + " " + filename)

        if info_hash:
            name = lines[0] if lines else info_hash
            stats = next((line for line in lines if "💾" in line or "👤" in line), "")
            seeders = int(m.group(1)) if (m := _SEEDERS_RE.search(stats)) else 0
            size = 0
            if m := _SIZE_RE.search(stats):
                size = _to_bytes(m.group(1), m.group(2))
            indexer = (m.group(1).strip() if (m := _INDEXER_RE.search(stats)) else "")
            indexer = _INDEXER_RE.sub("", indexer).strip()[:40] or default_indexer
            trackers = tuple(m.group(1) for m in _TRACKER_RE.finditer(
                "\n".join(stream.get("sources") or []) if isinstance(stream.get("sources"), list) else ""))
            file_idx = stream.get("fileIdx")
            sources.append(StreamSource(
                source=source_name, info_hash=info_hash, name=name,
                magnet=magnet_for(info_hash, name, tuple(dict.fromkeys(trackers + _PUBLIC_TRACKERS))),
                file_idx=file_idx if isinstance(file_idx, int) else None,
                filename=filename, size=size, seeders=seeders, quality=quality, indexer=indexer,
                pack=bool(not filename and _PACK_RE.search(name)),
            ))
            continue

        if not direct_url.startswith(("http://", "https://")):
            continue
        path = urllib.parse.urlsplit(direct_url).path.lower()
        if not (filename and looks_like_video(filename)) and not _DIRECT_VIDEO_RE.search(path):
            continue
        headers = _proxy_headers(behavior)
        referer = headers.pop("Referer", "")
        referer = str(referer or headers.pop("referer", ""))
        sources.append(StreamSource(
            source=source_name, name=filename or (lines[0] if lines else direct_url),
            filename=filename, quality=quality, size=0, indexer=default_indexer,
            url=direct_url, headers=headers, referer=referer,
            subtitles=_addon_subtitles(stream),
        ))
    return sources


def _proxy_headers(behavior: dict) -> dict:
    proxy = behavior.get("proxyHeaders") if isinstance(behavior, dict) else None
    request = proxy.get("request") if isinstance(proxy, dict) else None
    if not isinstance(request, dict):
        return {}
    return {str(key): str(value) for key, value in request.items() if isinstance(value, (str, int, float))}


def _addon_subtitles(stream: dict) -> list[dict]:
    subtitles = stream.get("subtitles")
    if not isinstance(subtitles, list):
        return []
    result: list[dict] = []
    for sub in subtitles:
        if not isinstance(sub, dict):
            continue
        url = str(sub.get("url") or "").strip()
        if not url.startswith(("http://", "https://")):
            continue
        lang = str(sub.get("lang") or sub.get("srclang") or "").strip()
        label = str(sub.get("label") or sub.get("name") or lang or "Subtitle").strip()
        result.append({"url": url, "label": label, "srclang": lang})
    return result


def _is_filename(line: str) -> bool:
    return bool(_VIDEO_RE.search(line) or _SUB_RE.search(line))


def _to_bytes(value: str, unit: str) -> int:
    try:
        scale = {"KB": 1024, "MB": 1024 ** 2, "GB": 1024 ** 3, "TB": 1024 ** 4}[unit.upper()]
        return int(float(value) * scale)
    except (KeyError, ValueError):
        return 0


async def _apibay(imdb_id: str, media_type: str, season: Optional[int],
                  episode: Optional[int]) -> list[StreamSource]:
    url = "https://apibay.org/q.php"
    async with _client() as client:
        response = await client.get(url, params={"q": imdb_id})
        response.raise_for_status()
        payload = response.json()
    if not isinstance(payload, list):
        return []
    return _parse_apibay(payload, media_type, season, episode, imdb_id)


def _parse_apibay(items: list, media_type: str, season: Optional[int],
                  episode: Optional[int], imdb_id: str) -> list[StreamSource]:
    sources: list[StreamSource] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        info_hash = str(item.get("info_hash") or "").strip()
        if not info_hash:
            continue
        imdb = str(item.get("imdb") or "")
        if imdb and imdb != imdb_id:
            continue
        if not str(item.get("category") or "2").startswith("2"):
            continue
        name = str(item.get("name") or "").strip()
        try:
            seeders = int(item.get("seeders") or 0)
            size = int(item.get("size") or 0)
        except (TypeError, ValueError):
            seeders, size = 0, 0
        if seeders <= 0:
            continue
        quality = quality_of(name)
        if media_type != "movie" and not _episode_match(name, season, episode):
            continue
        sources.append(StreamSource(
            source="apibay", info_hash=info_hash, name=name,
            magnet=magnet_for(info_hash, name, _PUBLIC_TRACKERS), size=size, seeders=seeders,
            quality=quality, indexer=item.get("username") or "ThePirateBay",
            pack=bool(_PACK_RE.search(name)),
        ))
    return sources


_TBHB_API = "https://theblackholebay.lol/api/search"
_TBHB_TTL = 60.0
_TBHB_VIDEO = 200
_tbhb_cache: dict[str, tuple[float, list]] = {}


async def _tbhb_query(query: str) -> list:
    now = time.monotonic()
    hit = _tbhb_cache.get(query)
    if hit and now - hit[0] < _TBHB_TTL:
        return hit[1]
    async with _client() as client:
        for attempt in range(2):
            response = await client.get(_TBHB_API, params={"q": query, "category": _TBHB_VIDEO, "page": 0})
            if response.status_code == 429 and attempt == 0:
                try:
                    delay = min(3.0, float(response.headers.get("Retry-After", "1")))
                except ValueError:
                    delay = 1.0
                await asyncio.sleep(delay)
                continue
            response.raise_for_status()
            break
        payload = response.json()
    results = payload.get("results") if isinstance(payload, dict) else None
    results = results if isinstance(results, list) else []
    if len(_tbhb_cache) > 64:
        _tbhb_cache.clear()
    _tbhb_cache[query] = (now, results)
    return results


async def _tbhb(title: str, year: str, media_type: str, season: Optional[int],
                episode: Optional[int]) -> list[StreamSource]:
    base = re.sub(r"[^\w\s]+", " ", str(title or "").replace("'", "").replace("\u2019", "")).strip()
    base = " ".join(base.split())
    if not base:
        return []
    if media_type == "movie":
        queries = [f"{base} {_year4(year)}".strip()]
    else:
        s, e = int(season or 1), int(episode or 1)
        queries = [f"{base} S{s:02d}E{e:02d}", f"{base} S{s:02d}"]
    batches = await asyncio.gather(*(_tbhb_query(q) for q in queries), return_exceptions=True)
    items: list = []
    failure: Optional[BaseException] = None
    for batch in batches:
        if isinstance(batch, BaseException):
            failure = batch
        else:
            items.extend(batch)
    if failure is not None and not items:
        raise failure
    return _parse_tbhb(items, title, year, media_type, season, episode)


def _year4(year) -> str:
    m = re.match(r"\d{4}", str(year or "").strip())
    return m.group(0) if m else ""


def _parse_tbhb(items: list, title: str, year: str, media_type: str, season: Optional[int],
                episode: Optional[int]) -> list[StreamSource]:
    want = _norm_title(title)
    if not want:
        return []
    year4 = _year4(year) if media_type == "movie" else ""
    sources: dict[str, StreamSource] = {}
    for item in items:
        if not isinstance(item, dict):
            continue
        info_hash = str(item.get("info_hash") or "").strip().lower()
        name = str(item.get("name") or "").strip()
        if not info_hash or not name:
            continue
        category = _int(item.get("category"))
        if category and category // 100 != 2:
            continue
        seeders = _int(item.get("seeders"))
        if seeders <= 0:
            continue
        if want not in _norm_title(name):
            continue
        if year4 and year4 not in name:
            continue
        if media_type != "movie" and not _episode_match(name, season, episode):
            continue
        provider = str(item.get("source") or "").strip()
        sources.setdefault(info_hash, StreamSource(
            source="tbhb", info_hash=info_hash, name=name,
            magnet=magnet_for(info_hash, name, _PUBLIC_TRACKERS), size=_int(item.get("size")),
            seeders=seeders, quality=quality_of(name),
            indexer=f"TBHB · {provider}" if provider else "TBHB",
            pack=bool(_PACK_RE.search(name)),
        ))
    return list(sources.values())


def _episode_match(name: str, season: Optional[int], episode: Optional[int]) -> bool:
    season = int(season or 1)
    episode = int(episode or 1)
    text = re.sub(r"[^a-z0-9]+", " ", (name or "").lower())
    if re.search(rf"s0*{season}\s*e0*{episode}\b", text) or re.search(rf"\b0*{season}x0*{episode}\b", text):
        return True
    if re.search(rf"(?:season|season\s+complete|s)\s*0*{season}\b", text) and _PACK_RE.search(name or ""):
        return True
    if re.search(rf"\bseason\s+0*{season}\b", text) and not re.search(rf"e0*{episode}\b", text):
        return True
    return False


def _norm_title(text: str) -> str:
    text = re.sub(r"\([^)]*\)", " ", str(text or ""))
    return re.sub(r"[^a-z0-9]+", "", text.lower())


def _int(value) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return 0


_QUALITY_ORDER = ("4k", "1080p", "720p", "480p", "240p")


_INDEXER_ALIASES = {
    "1337x": ("1337x",),
    "yts": ("yts",),
    "eztv": ("eztv",),
    "torrentgalaxy": ("torrentgalaxy", "tgx"),
    "rarbg": ("rarbg",),
    "kickass": ("kickass",),
    "magnetdl": ("magnetdl",),
    "nyaa": ("nyaa",),
    "rutor": ("rutor",),
    "tpb": ("thepiratebay", "tpb", "apibay"),
}


def provider_matches(source: StreamSource, provider: str) -> bool:
    if provider in ("", "auto") or provider == source.source:
        return True
    aliases = _INDEXER_ALIASES.get(provider)
    if not aliases:
        return False
    haystack = re.sub(r"[^a-z0-9]+", "", f"{source.indexer} {source.source}".lower())
    return any(alias in haystack for alias in aliases)


def pick_source(sources: list) -> Optional[StreamSource]:
    pool = [s for s in sources if not s.is_live]
    if not pool:
        return None
    want = getattr(settings, "tv_pick_quality", "auto")
    if want in _QUALITY_ORDER:
        exact = [s for s in pool if s.quality == want]
        if exact:
            pool = exact
        else:
            i = _QUALITY_ORDER.index(want)
            have = [q for q in _QUALITY_ORDER if any(s.quality == q for s in pool)]
            if have:
                near = min(have, key=lambda q: (abs(_QUALITY_ORDER.index(q) - i), _QUALITY_ORDER.index(q) < i))
                pool = [s for s in pool if s.quality == near]
    prov = getattr(settings, "tv_pick_provider", "auto")
    if prov != "auto":
        pool = [s for s in pool if provider_matches(s, prov)] or pool
    lo = int(getattr(settings, "tv_pick_min_gb", 0) or 0) * 1024 ** 3
    hi = int(getattr(settings, "tv_pick_max_gb", 0) or 0) * 1024 ** 3
    if lo or hi:
        pool = [s for s in pool if s.size == 0 or ((not lo or s.size >= lo) and (not hi or s.size <= hi))] or pool
    pool = [s for s in pool if s.is_direct or s.seeders > 0] or pool
    return max(pool, key=stream_score)
