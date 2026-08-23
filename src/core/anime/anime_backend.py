from __future__ import annotations

import asyncio
import ctypes
import logging
import math
import re
import time
from collections import OrderedDict
from typing import Any, Awaitable, Callable, Optional

import httpx

from ..cache import cache

logger = logging.getLogger(__name__)

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
NAMESPACE = "anime"

_ENTITY_DEC = re.compile(r"&#(\d+);")
_ENTITY_HEX = re.compile(r"&#x([0-9a-f]+);", re.IGNORECASE)
_TAG_RE = re.compile(r"<[^>]*>")
_WS_RE = re.compile(r"\s+")
_NON_ALNUM = re.compile(r"[^a-z0-9]")

_RELATION_FRAGMENT = (
    "edges{relationType(version:2) node{id type episodes relations{edges{"
    "relationType(version:2) node{id type episodes relations{edges{"
    "relationType(version:2) node{id type episodes relations{edges{"
    "relationType(version:2) node{id type episodes}}}}}}}}}}}"
)

MIN = 60_000
HOUR = 60 * MIN
DAY = 24 * HOUR
FULL_TTL = 30 * DAY
NORMAL_PROBE_INTERVAL = 15 * MIN
AIRING_PROBE_INTERVAL = 5 * MIN
AIRING_EARLY_WINDOW = 10 * MIN
AIRING_FAST_WINDOW = 6 * HOUR
SHOW_IDENTITY_TTL = 7 * DAY
WATCH_TTL = 6 * HOUR

def _now_ms() -> float:
    return time.time() * 1000

def is_fresh(entry: Optional[dict]) -> bool:
    if not entry:
        return False
    expires_at = entry.get("expiresAt")
    return expires_at is None or _now_ms() < expires_at

def needs_refresh(entry: Optional[dict]) -> bool:
    if not entry:
        return True
    refresh_after = entry.get("refreshAfter")
    return refresh_after is not None and _now_ms() >= refresh_after

def get_entry(key: str) -> Optional[dict]:
    return cache.get_entry(NAMESPACE, key)

async def get_entry_async(key: str) -> Optional[dict]:
    return await cache.get_entry_async(NAMESPACE, key)

def set_entry(key: str, data: Any, ttl_ms: float, refresh_after_ms: Optional[float] = None) -> None:
    now = _now_ms()
    cache.set_entry(NAMESPACE, key, {
        "data": data,
        "cachedAt": now,
        "expiresAt": now + ttl_ms if ttl_ms != math.inf else math.inf,
        "refreshAfter": now + refresh_after_ms if refresh_after_ms is not None else None,
    })

async def set_entry_async(key: str, data: Any, ttl_ms: float, refresh_after_ms: Optional[float] = None) -> None:
    now = _now_ms()
    await cache.set_entry_async(NAMESPACE, key, {
        "data": data,
        "cachedAt": now,
        "expiresAt": now + ttl_ms if ttl_ms != math.inf else math.inf,
        "refreshAfter": now + refresh_after_ms if refresh_after_ms is not None else None,
    })

async def delete_entry_async(key: str) -> None:
    await cache.delete_entry_async(NAMESPACE, key)

async def delete_prefix_async(prefix: str) -> None:
    await cache.delete_prefix_async(NAMESPACE, prefix)

def episode_ttl(status: Optional[str]) -> tuple[float, float]:
    if status == "FINISHED":
        return FULL_TTL, math.inf
    return FULL_TTL, NORMAL_PROBE_INTERVAL

def jikan_page_ttl(is_last: bool, status: Optional[str]) -> tuple[float, float]:
    if not is_last:
        return FULL_TTL, math.inf
    if status == "FINISHED":
        return FULL_TTL, math.inf
    return FULL_TTL, NORMAL_PROBE_INTERVAL

def map_ttl(status: Optional[str]) -> float:
    return FULL_TTL if status == "FINISHED" else DAY

class _BoundedInflight:
    __slots__ = ("_tasks", "_limit")

    def __init__(self, limit: int = 64):
        self._tasks: "OrderedDict[Any, asyncio.Task]" = OrderedDict()
        self._limit = limit

    def get(self, key: Any) -> Optional[asyncio.Task]:
        task = self._tasks.get(key)
        if task is not None:
            self._tasks.move_to_end(key)
        return task

    def add(self, key: Any, task: asyncio.Task) -> None:
        self._tasks[key] = task
        self._tasks.move_to_end(key)
        while len(self._tasks) > self._limit:
            self._tasks.popitem(last=False)

    def discard(self, key: Any) -> None:
        self._tasks.pop(key, None)

    def __contains__(self, key: Any) -> bool:
        return key in self._tasks

class _BoundedLRU:
    __slots__ = ("_data", "_limit")

    def __init__(self, limit: int = 256):
        self._data: "OrderedDict[Any, Any]" = OrderedDict()
        self._limit = limit

    def get(self, key: Any) -> Any:
        val = self._data.get(key)
        if val is not None:
            self._data.move_to_end(key)
        return val

    def __contains__(self, key: Any) -> bool:
        return key in self._data

    def set(self, key: Any, value: Any) -> None:
        self._data[key] = value
        self._data.move_to_end(key)
        while len(self._data) > self._limit:
            self._data.popitem(last=False)

    def pop(self, key: Any) -> None:
        self._data.pop(key, None)

async def fetch_html(url: str, headers: Optional[dict] = None) -> str:
    merged = {
        "User-Agent": UA,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        **(headers or {}),
    }
    async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
        resp = await client.get(url, headers=merged)
    if resp.status_code >= 400:
        raise RuntimeError(f"HTTP {resp.status_code} fetching {url}")
    return resp.text

def decode_entities(s: str = "") -> str:
    s = _ENTITY_DEC.sub(lambda m: chr(int(m.group(1))), s)
    s = _ENTITY_HEX.sub(lambda m: chr(int(m.group(1), 16)), s)
    s = s.replace("&quot;", '"')
    s = s.replace("&#39;", "'")
    s = s.replace("&amp;", "&")
    s = s.replace("&lt;", "<")
    s = s.replace("&gt;", ">")
    return s.strip()

def strip_tags(html: str = "") -> str:
    return decode_entities(_WS_RE.sub(" ", _TAG_RE.sub(" ", html)))

def attr(tag: str, name: str) -> str:
    m = re.search(rf'{re.escape(name)}=["\']([^"\']*)["\']', tag, re.IGNORECASE)
    return decode_entities(m.group(1)) if m else ""

def norm(s: str = "") -> str:
    return _NON_ALNUM.sub("", s.lower())

def dice_coeff(a: str, b: str) -> float:
    na, nb = norm(a), norm(b)
    if na == nb:
        return 1.0
    if len(na) < 2 or len(nb) < 2:
        return 0.0

    bigrams: dict[str, int] = {}
    for i in range(len(na) - 1):
        bg = na[i:i + 2]
        bigrams[bg] = bigrams.get(bg, 0) + 1

    hits = 0
    for i in range(len(nb) - 1):
        bg = nb[i:i + 2]
        count = bigrams.get(bg, 0)
        if count > 0:
            hits += 1
            bigrams[bg] = count - 1

    return (2 * hits) / (len(na) + len(nb) - 2)

def title_score(query: str, candidate: str, slug: str) -> float:
    base = max(dice_coeff(query, candidate), dice_coeff(query, slug.replace("-", " ")))

    q_num_m = re.search(r"\d+", norm(query))
    query_first_num = q_num_m.group(0) if q_num_m else ""
    s_num_m = re.search(r"\d+", slug)
    slug_first_num = s_num_m.group(0) if s_num_m else ""

    if query_first_num and slug_first_num and query_first_num != slug_first_num:
        return base * 0.65
    if query_first_num and not slug_first_num:
        return base * 0.65
    if not query_first_num and slug_first_num:
        n = int(slug_first_num)
        if 1 < n < 1900:
            return base * (1 - 0.06 * (n - 1))

    is_movie_query = bool(re.search(r"\b(movie|film|the movie)\b", query, re.IGNORECASE))
    is_movie_match = bool(re.search(r"\b(movie|film)\b", candidate, re.IGNORECASE)) or bool(
        re.search(r"movie|film", slug)
    )
    if is_movie_query and not is_movie_match:
        return base * 0.4

    q_len = len(norm(query))
    s_len = len(norm(slug.replace("-", " ")))
    return base * 0.8 if s_len > q_len * 1.6 + 4 else base

def _build_search_queries(title: str) -> list[str]:
    queries = {title}
    words = title.strip().split()
    if len(words) > 4:
        queries.add(" ".join(words[:4]))
    if len(words) > 3:
        queries.add(" ".join(words[:3]))

    stripped = re.sub(r"\bseason\s*\d+\b", "", title, flags=re.IGNORECASE)
    stripped = re.sub(r"\bpart\s*\d+\b", "", stripped, flags=re.IGNORECASE)
    stripped = re.sub(r"\b\d+rd\b|\b\d+th\b|\b\d+st\b|\b\d+nd\b", "", stripped, flags=re.IGNORECASE)
    stripped = re.sub(
        r"\b(first|second|third|fourth|fifth|sixth|seventh|eighth|ninth|tenth|final)\s+season\b",
        "", stripped, flags=re.IGNORECASE,
    )
    stripped = re.sub(r"\bcour\s*\d*\b", "", stripped, flags=re.IGNORECASE)
    stripped = _WS_RE.sub(" ", stripped).strip()
    if stripped and stripped != title:
        queries.add(stripped)

    return [q for q in queries if len(q) >= 3]

async def find_top_slugs(
    titles: list[str],
    search_fn: Callable[[str], Awaitable[list[dict]]],
    n: int = 6,
) -> list[dict]:
    all_candidates: dict[str, str] = {}
    search_queries: set[str] = set()
    for title in titles[:4]:
        search_queries.update(_build_search_queries(title))

    async def _run(q: str):
        try:
            results = await search_fn(q)
        except Exception as exc:
            logger.warning("Provider search failed for query %r: %s: %s", q, type(exc).__name__, exc)
            return
        for r in results:
            if r["slug"] not in all_candidates:
                all_candidates[r["slug"]] = r["text"]

    await asyncio.gather(*(_run(q) for q in search_queries))

    scored = []
    for slug, text in all_candidates.items():
        best = 0.0
        for title in titles[:2]:
            best = max(best, title_score(title, text, slug))
        if best >= 0.5:
            scored.append({"slug": slug, "title": text, "score": best})

    scored.sort(key=lambda x: x["score"], reverse=True)
    return scored[:n]

async def _anilist_query(query: str, variables: dict) -> dict:
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(
            "https://graphql.anilist.co",
            headers={"Content-Type": "application/json", "Accept": "application/json", "User-Agent": AL_UA},
            json={"query": query, "variables": variables},
        )
    if resp.status_code >= 400:
        raise RuntimeError(f"AniList HTTP {resp.status_code}")
    data = resp.json()
    if data.get("errors"):
        raise RuntimeError(f"AniList: {data['errors'][0]['message']}")
    return data["data"]

def _compute_prequel_offset(relations: Optional[dict], depth: int = 0) -> int:
    if not relations or depth > 5:
        return 0
    prequel_edge = next(
        (
            e for e in relations.get("edges", [])
            if e.get("relationType") == "PREQUEL"
            and e["node"].get("type") == "ANIME"
            and (e["node"].get("episodes") or 0) >= 5
        ),
        None,
    )
    if not prequel_edge:
        return 0
    return (prequel_edge["node"].get("episodes") or 0) + _compute_prequel_offset(
        prequel_edge["node"].get("relations"), depth + 1
    )

async def get_prequel_offset(anilist_id: Any) -> int:
    key = f"np-offset:{anilist_id}"
    entry = get_entry(key)
    if is_fresh(entry):
        return entry["data"]

    data = await _anilist_query(
        f"query($id:Int){{Media(id:$id,type:ANIME){{relations{{{_RELATION_FRAGMENT}}}}}}}",
        {"id": int(anilist_id)},
    )
    offset = _compute_prequel_offset((data.get("Media") or {}).get("relations"))
    set_entry(key, offset, SHOW_IDENTITY_TTL)
    return offset

def _as_dict(v: Any) -> dict:
    return v if isinstance(v, dict) else {}

def build_titles(media: Optional[dict], anizip: Optional[dict]) -> list[str]:
    anizip = anizip if isinstance(anizip, dict) else {}
    media = media if isinstance(media, dict) else {}
    title = media.get("title")
    title = title if isinstance(title, dict) else {}
    anizip_titles = _as_dict(anizip.get("titles"))
    candidates = [
        title.get("english"),
        title.get("romaji"),
        title.get("native"),
        *(media.get("synonyms") or []),
        anizip_titles.get("en"),
        anizip_titles.get("x-jat"),
        anizip_titles.get("ja"),
    ]
    return [c for c in candidates if c]

def expected_count(media: Optional[dict], anizip: Optional[dict], jikan_eps: Optional[list[dict]]) -> Optional[int]:
    anizip = anizip if isinstance(anizip, dict) else {}
    counts = []
    if (media or {}).get("episodes"):
        counts.append(media["episodes"])
    for k in _as_dict(anizip.get("episodes")).keys():
        try:
            counts.append(int(k))
        except ValueError:
            pass
    for e in jikan_eps or []:
        if not isinstance(e, dict):
            continue
        mal_id = e.get("mal_id")
        if isinstance(mal_id, (int, float)):
            counts.append(mal_id)

    counts = [n for n in counts if isinstance(n, (int, float)) and n > 0]
    return max(counts) if counts else None

def episode_meta(n: Any, ctx: dict) -> dict:
    anizip = ctx.get("anizip")
    anizip = anizip if isinstance(anizip, dict) else {}
    az = _as_dict(anizip.get("episodes")).get(str(n), {})
    if not isinstance(az, dict):
        az = {}
    jikan_eps = ctx.get("jikanEps") or []
    jk = next((e for e in jikan_eps if isinstance(e, dict) and e.get("mal_id") == n), None)
    runtime = az.get("runtime") or az.get("length")
    az_title = _as_dict(az.get("title"))
    anizip_images = _as_dict(anizip.get("images"))

    return {
        "title": (jk or {}).get("title") or az_title.get("en") or az_title.get("x-jat"),
        "duration": runtime * 60 if runtime else None,
        "filler": (jk or {}).get("filler") or az.get("filler") or False,
        "uncensored": False,
        "description": az.get("overview") or az.get("summary"),
        "image": az.get("image") or anizip_images.get("cover"),
        "airDate": (jk or {}).get("aired") or az.get("airdate") or az.get("aired"),
    }

async def select_series(
    candidates: list[dict],
    scrape_series: Callable[[str], Awaitable[list[dict]]],
    expected: Optional[int],
    status: Optional[str],
    offset: Optional[int],
    min_score: float = 0.65,
) -> Optional[dict]:
    async def _score(candidate: dict) -> dict:
        episodes = await scrape_series(candidate["slug"])
        max_ep = max([0, *[e["number"] for e in episodes]])
        local_hits = (
            len([e for e in episodes if 1 <= e["number"] <= expected])
            if expected else len(episodes)
        )
        offset_hits = (
            len([e for e in episodes if offset < e["number"] <= offset + expected])
            if expected and offset else 0
        )
        mode = "offset" if offset_hits > local_hits else "local"
        hits = max(local_hits, offset_hits)

        count_score = 1.0
        needed = None
        if expected and expected >= 6:
            needed = (
                math.ceil(expected * 0.9) if status == "FINISHED" else max(1, expected - 3)
            )
            count_score = 1.0 if hits >= needed else hits / needed

        return {
            **candidate,
            "episodes": episodes,
            "max": max_ep,
            "mode": mode,
            "score": candidate["score"] * 0.7 + count_score * 0.3,
            "_search_score": candidate["score"],
            "_count_score": count_score,
            "_hits": hits,
            "_needed": needed,
        }

    results = await asyncio.gather(*(_score(c) for c in candidates))
    viable = sorted(
        [r for r in results if r["episodes"] and r["score"] >= min_score],
        key=lambda r: r["score"],
        reverse=True,
    )
    if viable:
        return viable[0]

    if results:
        ranked = sorted(results, key=lambda r: r["score"], reverse=True)[:5]
        details = ", ".join(
            f"{r['slug']!r} (title={r['title']!r}, searchScore={r['_search_score']:.2f}, "
            f"episodes={len(r['episodes'])}, maxEp={r['max']}, mode={r['mode']}, "
            f"hits={r['_hits']}, needed={r['_needed']}, countScore={r['_count_score']:.2f}, "
            f"finalScore={r['score']:.2f})"
            for r in ranked
        )
        logger.warning(
            "select_series: no candidate reached min_score=%.2f (expected=%s, offset=%s, status=%s). "
            "Best candidates: %s",
            min_score, expected, offset, status, details,
        )
    else:
        logger.warning(
            "select_series: no search candidates at all (expected=%s, offset=%s, status=%s)",
            expected, offset, status,
        )

    return None

AL_UA = UA
ARM = "https://arm.haglund.dev/api/v2/ids"
JIKAN = "https://api.jikan.moe/v4"

_STATUS_MAP = {
    "Currently Airing": "RELEASING",
    "Finished Airing": "FINISHED",
    "Not yet aired": "NOT_YET_RELEASED",
    "On Hiatus": "HIATUS",
}

_AL_STATUS_MAP = {
    "RELEASING": "RELEASING",
    "FINISHED": "FINISHED",
    "NOT_YET_RELEASED": "NOT_YET_RELEASED",
    "CANCELLED": "FINISHED",
    "HIATUS": "HIATUS",
}

_FULL_QUERY = (
    "query($id:Int){Media(id:$id,type:ANIME){id idMal title{english romaji native} "
    "status format episodes seasonYear startDate{year} synonyms genres description "
    "coverImage{large medium} "
    "nextAiringEpisode{episode airingAt timeUntilAiring}}}"
)

_al_resolved = _BoundedLRU(limit=200)
_al_inflight = _BoundedInflight(limit=32)

async def _fetch_from_anilist(client: httpx.AsyncClient, anilist_id: int, retries: int = 4) -> Optional[dict]:
    for attempt in range(retries + 1):
        try:
            resp = await client.post(
                "https://graphql.anilist.co",
                headers={"Content-Type": "application/json", "Accept": "application/json", "User-Agent": AL_UA},
                json={"query": _FULL_QUERY, "variables": {"id": anilist_id}},
            )
        except httpx.HTTPError:
            return None

        if resp.status_code == 429:
            retry_after = resp.headers.get("Retry-After")
            try:
                wait_s = int(retry_after) if retry_after else 1
            except ValueError:
                wait_s = 1
            wait_s = wait_s + attempt * 0.6
            if attempt < retries:
                await asyncio.sleep(wait_s)
                continue
            return None

        if resp.status_code >= 500:
            if attempt < retries:
                await asyncio.sleep(0.5 + attempt * 0.5)
                continue
            return None

        if resp.status_code != 200:
            return None

        try:
            data = resp.json()
        except ValueError:
            return None
        return (data.get("data") or {}).get("Media")

    return None

def _year_from_aired(jikan_data: dict) -> Optional[dict]:
    aired_from = (jikan_data.get("aired") or {}).get("from")
    if not aired_from:
        return None
    try:
        year = int(str(aired_from)[:4])
        return {"year": year}
    except (ValueError, TypeError):
        return None

def _media_from_anilist_only(anilist_id: int, mal_id: Optional[int], al: dict) -> dict:
    return {
        "id": anilist_id,
        "idMal": mal_id,
        "title": {
            "english": al.get("title", {}).get("english"),
            "romaji": al.get("title", {}).get("romaji"),
            "native": al.get("title", {}).get("native"),
        },
        "status": _AL_STATUS_MAP.get(al.get("status"), "RELEASING"),
        "format": al.get("format"),
        "episodes": al.get("episodes"),
        "seasonYear": al.get("seasonYear"),
        "startDate": al.get("startDate"),
        "nextAiringEpisode": al.get("nextAiringEpisode"),
        "synonyms": al.get("synonyms") if isinstance(al.get("synonyms"), list) else [],
        "genres": al.get("genres") if isinstance(al.get("genres"), list) else [],
        "description": al.get("description"),
        "coverImage": (al.get("coverImage") or {}).get("large") or (al.get("coverImage") or {}).get("medium"),
    }

async def _resolve_anilist(anilist_id: int) -> dict:
    async with httpx.AsyncClient(timeout=15) as client:
        arm = None
        try:
            arm_resp = await client.get(
                ARM, params={"source": "anilist", "id": anilist_id},
                headers={"User-Agent": AL_UA, "Accept": "application/json"},
            )
            if arm_resp.status_code == 200:
                arm = arm_resp.json()
        except httpx.HTTPError:
            arm = None

        mal_id = (arm or {}).get("myanimelist")

        if not mal_id:
            al = await _fetch_from_anilist(client, anilist_id)
            if not al:
                raise RuntimeError(f"No data found for AniList ID {anilist_id}")
            return _media_from_anilist_only(anilist_id, None, al)

        al = await _fetch_from_anilist(client, anilist_id)

        jikan_data = None
        for attempt in range(5):
            try:
                r = await client.get(
                    f"{JIKAN}/anime/{mal_id}",
                    headers={"User-Agent": AL_UA, "Accept": "application/json"},
                )
            except httpx.HTTPError:
                r = None

            if r is not None and r.status_code == 429:
                retry_after = r.headers.get("Retry-After")
                try:
                    wait_s = (int(retry_after) if retry_after else 1)
                except ValueError:
                    wait_s = 1
                wait_s = wait_s + attempt * 0.5
                if attempt < 4:
                    await asyncio.sleep(wait_s)
                    continue
                raise RuntimeError(f"Jikan 429 for MAL ID {mal_id} (exhausted retries)")

            if r is None or not (200 <= r.status_code < 300):
                if al:
                    break
                status = r.status_code if r is not None else "network error"
                raise RuntimeError(f"Jikan {status}")

            try:
                jikan_data = r.json()
            except ValueError:
                jikan_data = None
            break

        d = (jikan_data or {}).get("data") if jikan_data else None

        if not d and al:
            return _media_from_anilist_only(anilist_id, mal_id, al)

        if not d:
            raise RuntimeError(f"Jikan returned no data for MAL ID {mal_id}")

        media = {
            "id": anilist_id,
            "idMal": mal_id,
            "title": {
                "english": (al or {}).get("title", {}).get("english") or d.get("title_english"),
                "romaji": (al or {}).get("title", {}).get("romaji") or d.get("title"),
                "native": (al or {}).get("title", {}).get("native") or d.get("title_japanese"),
            },
            "status": _AL_STATUS_MAP.get((al or {}).get("status")) or _STATUS_MAP.get(d.get("status")) or "RELEASING",
            "format": (al or {}).get("format") or d.get("type"),
            "episodes": (al or {}).get("episodes") or d.get("episodes"),
            "seasonYear": (al or {}).get("seasonYear") or d.get("year"),
            "startDate": (al or {}).get("startDate") or _year_from_aired(d),
            "nextAiringEpisode": (al or {}).get("nextAiringEpisode"),
            "synonyms": [
                *[t.get("title") for t in (d.get("titles") or []) if t.get("title")],
                *((al or {}).get("synonyms") or []),
            ],
            "genres": (
                (al or {}).get("genres") if isinstance((al or {}).get("genres"), list)
                else [g.get("name") for g in (d.get("genres") or []) if isinstance(g, dict) and g.get("name")]
            ),
            "description": (al or {}).get("description") or d.get("synopsis"),
            "coverImage": (
                (al or {}).get("coverImage", {}).get("large")
                or (al or {}).get("coverImage", {}).get("medium")
                or ((d.get("images") or {}).get("jpg") or {}).get("large_image_url")
                or ((d.get("images") or {}).get("jpg") or {}).get("image_url")
            ),
        }
        return media

async def get_media(anilist_id: Any) -> dict:
    aid = int(anilist_id)
    cached = _al_resolved.get(aid)
    if cached is not None:
        return cached
    inflight = _al_inflight.get(aid)
    if inflight is not None:
        return await inflight

    async def _runner():
        try:
            media = await _resolve_anilist(aid)
            _al_resolved.set(aid, media)
            return media
        finally:
            _al_inflight.discard(aid)

    task = asyncio.ensure_future(_runner())
    _al_inflight.add(aid, task)
    return await task

async def search_anime(query: str, limit: int = 20) -> list[dict]:
    query_gql = (
        "query($search:String,$perPage:Int){Page(perPage:$perPage){"
        "media(search:$search,type:ANIME,sort:SEARCH_MATCH){"
        "id idMal title{english romaji native} status format episodes "
        "seasonYear startDate{year} coverImage{large medium} synonyms genres description}}}"
    )
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                "https://graphql.anilist.co",
                headers={"Content-Type": "application/json", "Accept": "application/json", "User-Agent": AL_UA},
                json={"query": query_gql, "variables": {"search": query, "perPage": limit}},
            )
    except httpx.HTTPError:
        return []
    if resp.status_code != 200:
        return []
    try:
        data = resp.json()
    except ValueError:
        return []

    media_list = ((data.get("data") or {}).get("Page") or {}).get("media") or []
    results = []
    for m in media_list:
        results.append({
            "id": m.get("id"),
            "idMal": m.get("idMal"),
            "title": {
                "english": (m.get("title") or {}).get("english"),
                "romaji": (m.get("title") or {}).get("romaji"),
                "native": (m.get("title") or {}).get("native"),
            },
            "status": _AL_STATUS_MAP.get(m.get("status"), "RELEASING"),
            "format": m.get("format"),
            "episodes": m.get("episodes"),
            "seasonYear": m.get("seasonYear"),
            "startDate": m.get("startDate"),
            "coverImage": (m.get("coverImage") or {}).get("large") or (m.get("coverImage") or {}).get("medium"),
            "synonyms": m.get("synonyms") or [],
            "genres": m.get("genres") or [],
            "description": m.get("description"),
        })

    for r in results:
        aid = r["id"]
        if aid is not None and aid not in _al_resolved:
            _al_resolved.set(aid, {
                "id": aid, "idMal": r["idMal"], "title": r["title"], "status": r["status"],
                "format": r["format"], "episodes": r["episodes"], "seasonYear": r["seasonYear"],
                "startDate": r["startDate"], "nextAiringEpisode": None, "synonyms": r["synonyms"],
                "genres": r["genres"], "description": r["description"], "coverImage": r["coverImage"],
            })

    return results

def forget_media(anilist_id: Any) -> None:
    _al_resolved.pop(int(anilist_id))

ARM2 = ARM
UA2 = "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:109.0) Gecko/20100101 Firefox/121.0"

_RELATIONS_QUERY = """
query ($id: Int) {
  Media(id: $id, type: ANIME) {
    id synonyms
    relations {
      edges {
        relationType(version: 2)
        node {
          id type format title { romaji english native }
          relations {
            edges {
              relationType(version: 2)
              node { id type format title { romaji english native } }
            }
          }
        }
      }
    }
  }
}
"""

def _hash_franchise_id(s: str) -> int:
    h = 0
    for ch in s:
        h = ctypes.c_int32((h << 5) - h + ord(ch)).value
    return h & 0xFFFFFFFF

async def _fetch_arm(anilist_id: Any) -> Optional[dict]:
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(
                ARM2, params={"source": "anilist", "id": anilist_id},
                headers={"User-Agent": UA2, "Accept": "application/json"},
            )
    except httpx.HTTPError:
        return None
    if resp.status_code != 200:
        return None
    try:
        return resp.json()
    except ValueError:
        return None

async def _fetch_anilist_relations(anilist_id: Any) -> Optional[dict]:
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                "https://graphql.anilist.co",
                headers={"Content-Type": "application/json", "Accept": "application/json", "User-Agent": AL_UA},
                json={"query": _RELATIONS_QUERY, "variables": {"id": int(anilist_id)}},
            )
        if resp.status_code != 200:
            return None
        data = resp.json()
        return (data.get("data") or {}).get("Media")
    except (httpx.HTTPError, ValueError):
        return None

async def map_anime_ids(anilist_id: Any) -> dict:
    async def _safe_media():
        try:
            return await get_media(anilist_id)
        except Exception:
            return None

    arm, media, al_relations = await asyncio.gather(
        _fetch_arm(anilist_id),
        _safe_media(),
        _fetch_anilist_relations(anilist_id),
    )
    arm = arm or {}

    mal_id = arm.get("myanimelist")
    fmt = (media or {}).get("format")
    (media or {}).get("seasonYear")
    title_en = (media or {}).get("title", {}).get("english") or None
    title_rom = (media or {}).get("title", {}).get("romaji") or None

    synonyms = list((media or {}).get("synonyms") or [])
    if al_relations and al_relations.get("synonyms"):
        for s in al_relations["synonyms"]:
            if s not in synonyms:
                synonyms.append(s)

    franchise_map: dict[int, dict] = {}
    if al_relations and (al_relations.get("relations") or {}).get("edges"):
        for e1 in al_relations["relations"]["edges"]:
            node1 = e1["node"]
            if node1["id"] not in franchise_map:
                franchise_map[node1["id"]] = {
                    "relation": e1["relationType"],
                    "anilistId": node1["id"],
                    "title": node1["title"].get("romaji") or node1["title"].get("english"),
                    "type": node1["type"],
                    "format": node1["format"],
                }
            for e2 in (node1.get("relations") or {}).get("edges", []):
                node2 = e2["node"]
                if node2["id"] == int(anilist_id):
                    continue
                if node2["id"] not in franchise_map:
                    franchise_map[node2["id"]] = {
                        "relation": e2["relationType"],
                        "anilistId": node2["id"],
                        "title": node2["title"].get("romaji") or node2["title"].get("english"),
                        "type": node2["type"],
                        "format": node2["format"],
                    }

    thetvdb_id = arm.get("thetvdb")
    themoviedb_id = arm.get("themoviedb")
    imdb_id = arm.get("imdb")

    return {
        "mappings": {
            "id": int(anilist_id),
            "title": title_en or title_rom,
            "type": arm.get("media"),
            "format": fmt,
            "episodes": (media or {}).get("episodes"),
            "malId": mal_id,
            "aniId": int(anilist_id),
            "anidbId": arm.get("anidb"),
            "animePlanetId": arm.get("anime-planet"),
            "kitsuId": arm.get("kitsu"),
            "animeCountdownId": arm.get("animecountdown"),
            "anisearchId": arm.get("anisearch"),
            "notifyMoeId": None,
            "simklId": arm.get("simkl"),
            "imdbId": imdb_id,
            "themoviedbId": themoviedb_id,
            "thetvdbId": thetvdb_id,
            "livechartId": arm.get("livechart"),
            "annId": arm.get("animenewsnetwork"),
            "animescheduleId": None,
            "animethemesId": None,
            "animefillerlistId": None,
            "franchiseAnchor": f"tvdb:{thetvdb_id}" if thetvdb_id else None,
            "franchiseId": _hash_franchise_id(f"tvdb:{thetvdb_id}") if thetvdb_id else None,
            "defaultTvdbSeason": str(arm["thetvdb-season"]) if arm.get("thetvdb-season") is not None else None,
            "tmdbSeason": str(arm["themoviedb-season"]) if arm.get("themoviedb-season") is not None else None,
            "episodeOffset": None,
            "tmdbOffset": None,
            "malIds": None,
            "aniskip": None,
            "animefillerlist": None,
            "synonyms": synonyms,
            "franchise": list(franchise_map.values()),
        }
    }

_es_inflight = _BoundedInflight(limit=64)
_es_bg_running: set[str] = set()

def _es_dedupe(key: str, fn: Callable[[], Awaitable[Any]]) -> Awaitable[Any]:
    existing = _es_inflight.get(key)
    if existing is not None:
        return existing

    async def _runner():
        try:
            return await fn()
        finally:
            _es_inflight.discard(key)

    task = asyncio.ensure_future(_runner())
    _es_inflight.add(key, task)
    return task

def _es_bg(key: str, fn: Callable[[], Awaitable[Any]]) -> None:
    if key in _es_bg_running:
        return
    _es_bg_running.add(key)

    async def _runner():
        try:
            await fn()
        except Exception as exc:
            logger.warning("Background refresh failed [%s]: %s", key, exc)
        finally:
            _es_bg_running.discard(key)

    asyncio.ensure_future(_runner())

async def _jikan_page(mal_id: Any, page_num: int, retries: int = 3) -> Optional[dict]:
    for attempt in range(retries + 1):
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.get(
                    f"{JIKAN}/anime/{mal_id}/episodes",
                    params={"page": page_num},
                    headers={"User-Agent": UA, "Accept": "application/json"},
                )
        except httpx.HTTPError:
            return None

        if resp.status_code == 429:
            retry_after = resp.headers.get("Retry-After")
            try:
                wait_s = int(retry_after) if retry_after else 1
            except ValueError:
                wait_s = 1
            wait_s = wait_s + attempt * 0.6
            if attempt < retries:
                await asyncio.sleep(wait_s)
                continue
            return None

        if resp.status_code >= 400:
            return None

        try:
            return resp.json()
        except ValueError:
            return None

    return None

def fetch_all_jikan_with_cache(mal_id: Any, status: Optional[str]) -> Awaitable[list[dict]]:
    return _es_dedupe(f"jikan:{mal_id}", lambda: _jikan_all(mal_id, status))

async def _jikan_all(mal_id: Any, status: Optional[str]) -> list[dict]:
    meta_key = f"jm:{mal_id}"
    meta = await get_entry_async(meta_key)

    is_finished = status == "FINISHED"
    must_check_total = not is_finished and (not meta or needs_refresh(meta))
    last_page = (meta or {}).get("data", {}).get("lastPage") if meta else None

    if must_check_total or not last_page:
        p1 = await _jikan_page(mal_id, 1)

        if not p1 and not last_page:
            return []
        if not p1 and last_page:
            return await _build_pages(mal_id, last_page, status)

        new_last = (p1.get("pagination") or {}).get("last_visible_page", 1)
        is_p1_last = new_last == 1

        p1_ttl, p1_refresh = jikan_page_ttl(is_p1_last, status)
        await set_entry_async(f"jp:{mal_id}:1", p1.get("data") or [], p1_ttl, p1_refresh)

        if last_page and new_last > last_page:
            stable_ttl, _ = jikan_page_ttl(False, "FINISHED")
            old_last_entry = await get_entry_async(f"jp:{mal_id}:{last_page}")
            if old_last_entry:
                await set_entry_async(f"jp:{mal_id}:{last_page}", old_last_entry["data"], stable_ttl, math.inf)

            async def _fetch_new_page(pn: int):
                is_last = pn == new_last
                pd = await _jikan_page(mal_id, pn)
                t, r = jikan_page_ttl(is_last, status)
                await set_entry_async(f"jp:{mal_id}:{pn}", (pd or {}).get("data") or [], t, r)

            await asyncio.gather(*(_fetch_new_page(last_page + 1 + i) for i in range(new_last - last_page)))

        m_ttl, m_refresh = episode_ttl(status)
        await set_entry_async(meta_key, {"lastPage": new_last}, m_ttl, m_refresh)
        last_page = new_last

    return await _build_pages(mal_id, last_page, status)

async def _build_pages(mal_id: Any, last_page: int, status: Optional[str]) -> list[dict]:
    async def _one_page(pn: int) -> list[dict]:
        key = f"jp:{mal_id}:{pn}"
        is_last = pn == last_page
        entry = await get_entry_async(key)

        if is_fresh(entry):
            if is_last and status == "RELEASING" and needs_refresh(entry):
                async def _refresh():
                    pd = await _jikan_page(mal_id, pn)
                    if pd:
                        t, r = jikan_page_ttl(True, status)
                        await set_entry_async(key, pd.get("data") or [], t, r)

                _es_bg(key, _refresh)
            return entry["data"]

        pd = await _jikan_page(mal_id, pn)
        data = (pd or {}).get("data") or []
        t, r = jikan_page_ttl(is_last, status)
        await set_entry_async(key, data, t, r)
        return data

    pages = await asyncio.gather(*(_one_page(pn) for pn in range(1, last_page + 1)))
    return [ep for page in pages for ep in page]

async def _with_provider_cache(key: str, status: Optional[str], fetch_fn: Callable[[], Awaitable[Any]]) -> Any:
    ttl, refresh_after = episode_ttl(status)
    entry = await get_entry_async(key)

    if is_fresh(entry):
        if needs_refresh(entry):
            async def _refresh():
                data = await fetch_fn()
                await set_entry_async(key, data, ttl, refresh_after)

            _es_bg(key, _refresh)
        return entry["data"]

    data = await fetch_fn()
    await set_entry_async(key, data, ttl, refresh_after)
    return data

async def _safe_provider_call(label: str, fn: Callable[[], Awaitable[Any]]) -> dict:
    try:
        return {"ok": True, "data": await fn()}
    except Exception as exc:
        logger.warning("Provider call failed [ep:%s]: %s: %s", label, type(exc).__name__, exc)
        return {"ok": False, "error": str(exc) or type(exc).__name__, "stack": None}

from .providers import PROVIDER_REGISTRY

def resolve_providers(raw_names: list[str]) -> dict:
    resolved: set[str] = set()
    unknown: list[str] = []
    for raw in raw_names:
        name = raw.lower()
        if name in PROVIDER_REGISTRY:
            resolved.add(name)
        else:
            unknown.append(raw)
    return {"resolved": resolved, "unknown": unknown}

def _provider_fns(anilist_id: Any, status: Optional[str], ctx: dict) -> dict[str, Callable[[], Awaitable[Any]]]:
    fns = {}
    for name, provider in PROVIDER_REGISTRY.items():
        fns[name] = (
            lambda name=name, provider=provider: _with_provider_cache(
                f"epv:{name}:{anilist_id}", status, lambda: provider.get_episodes(anilist_id, ctx)
            )
        )
    return fns

async def _build_provider_context(anilist_id: Any, media: Optional[dict], anizip: Optional[dict]) -> tuple[dict, dict]:
    status = (media or {}).get("status", "RELEASING")
    mal_id = (media or {}).get("idMal")

    jikan_eps = None
    if mal_id:
        try:
            jikan_eps = await fetch_all_jikan_with_cache(mal_id, status)
        except Exception:
            jikan_eps = None

    ctx = {"media": media, "anizip": anizip, "jikanEps": jikan_eps, "maxPages": None}
    fns = _provider_fns(anilist_id, status, ctx)
    return ctx, fns

async def build_filtered_episodes_with_cache(
    anilist_id: Any, providers: set[str], media: Optional[dict], anizip: Optional[dict]
) -> dict:
    _ctx, fns = await _build_provider_context(anilist_id, media, anizip)

    async def _one(name: str):
        result = await _safe_provider_call(name, fns[name])
        return name, (result["data"] if result["ok"] else {"error": result["error"], "stack": result["stack"]})

    pairs = await asyncio.gather(*(_one(name) for name in providers))
    return dict(pairs)

async def build_episodes_with_cache(anilist_id: Any, media: Optional[dict], anizip: Optional[dict]) -> dict:
    _ctx, fns = await _build_provider_context(anilist_id, media, anizip)

    async def _one(name: str):
        result = await _safe_provider_call(name, fns[name])
        return name, (result["data"] if result["ok"] else {"error": result["error"], "stack": result["stack"]})

    pairs = await asyncio.gather(*(_one(name) for name in PROVIDER_REGISTRY.keys()))
    return dict(pairs)

ANIZIP = "https://api.ani.zip/mappings"

_refreshing: set[str] = set()

_KNOWN_PROVIDER_CACHE_NAMES = [
    "pahe", "manga", "reanime", "anikoto", "animegg", "anineko", "anidbapp", "anizone",
]

def _run_background(env: Optional[dict], coro) -> None:
    wait_until = None
    if env is not None:
        context = env.get("context") if isinstance(env, dict) else None
        wait_until = (context or {}).get("waitUntil") if isinstance(context, dict) else env.get("waitUntil")

    if callable(wait_until):
        wait_until(coro)
        return

    async def _swallow():
        try:
            await coro
        except Exception:
            pass

    asyncio.ensure_future(_swallow())

def _latest_episode_from_response(data: Optional[dict]) -> Optional[int]:
    max_n = 0
    for provider in (data or {}).values():
        if not isinstance(provider, dict):
            continue
        episodes = provider.get("episodes")
        if not isinstance(episodes, dict):
            continue
        for lst in episodes.values():
            if not isinstance(lst, list):
                continue
            for ep in lst:
                n = (ep or {}).get("number")
                if isinstance(n, (int, float)) and n > max_n:
                    max_n = n
    return max_n or None

def _has_current_providers(data: Optional[dict]) -> bool:
    return bool(data) and "anidbapp" in data and "anizone" in data

def _latest_episode_from_anizip(anizip: Optional[dict]) -> Optional[int]:
    nums = []
    for k in ((anizip or {}).get("episodes") or {}).keys():
        try:
            nums.append(int(k))
        except ValueError:
            pass
    return max(nums) if nums else None

async def _resolve_shared(anilist_id: Any, fresh_media: bool = False) -> tuple[Optional[dict], Optional[dict]]:
    if fresh_media:
        forget_media(anilist_id)

    async def _safe_media():
        try:
            return await get_media(anilist_id)
        except Exception:
            return None

    async def _safe_anizip():
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.get(ANIZIP, params={"anilist_id": anilist_id})
            return resp.json()
        except Exception:
            return None

    return await asyncio.gather(_safe_media(), _safe_anizip())

async def _clear_provider_cache(anilist_id: Any, media: Optional[dict]) -> None:
    for p in _KNOWN_PROVIDER_CACHE_NAMES:
        await delete_entry_async(f"epv:{p}:{anilist_id}")
    mal_id = (media or {}).get("idMal")
    if mal_id:
        await delete_entry_async(f"jm:{mal_id}")
        await delete_prefix_async(f"jp:{mal_id}:")

async def _build_response(
    anilist_id: Any, media: Optional[dict], anizip: Optional[dict], force_refresh: bool = False
) -> dict:
    if force_refresh:
        await _clear_provider_cache(anilist_id, media)

    async def _safe_mapping():
        try:
            return await map_anime_ids(anilist_id)
        except Exception:
            return None

    provider_result, mapping_result = await asyncio.gather(
        build_episodes_with_cache(anilist_id, media, anizip),
        _safe_mapping(),
    )

    return {
        "page": 1,
        "type": "all",
        "mappings": (mapping_result or {}).get("mappings"),
        **provider_result,
    }

def _probe_interval(state: Optional[dict]) -> float:
    air_ms = state.get("nextAiringAt") * 1000 if state and state.get("nextAiringAt") else None
    if not air_ms:
        return NORMAL_PROBE_INTERVAL
    now = _now_ms()
    if air_ms - AIRING_EARLY_WINDOW <= now <= air_ms + AIRING_FAST_WINDOW:
        return AIRING_PROBE_INTERVAL
    return NORMAL_PROBE_INTERVAL

def _should_rebuild(entry: Optional[dict], media: Optional[dict], anizip: Optional[dict]) -> bool:
    if (media or {}).get("status", "RELEASING") == "FINISHED":
        return False

    cached_latest = _latest_episode_from_response((entry or {}).get("data")) or 0
    known_latest = max(
        _latest_episode_from_anizip(anizip) or 0,
        (media or {}).get("episodes") or 0,
    )
    if known_latest > cached_latest:
        return True

    next_ep = (media or {}).get("nextAiringEpisode") or {}
    if next_ep.get("episode") and cached_latest >= next_ep["episode"]:
        return False
    if next_ep.get("airingAt"):
        air_ms = next_ep["airingAt"] * 1000
        now = _now_ms()
        if now < air_ms - AIRING_EARLY_WINDOW:
            return False
        if now <= air_ms + AIRING_FAST_WINDOW:
            return True

    return needs_refresh(entry)

def _write_sync_state(anilist_id: Any, state: dict, ttl: float = FULL_TTL) -> None:
    set_entry(f"sync:{anilist_id}", state, ttl, NORMAL_PROBE_INTERVAL)

def _schedule_refresh(anilist_id: Any, entry: Optional[dict], env: Optional[dict]) -> None:
    key = f"ep-bg:{anilist_id}"
    if key in _refreshing:
        return

    sync_key = f"sync:{anilist_id}"
    old_entry = get_entry(sync_key)
    old_state = (old_entry or {}).get("data") or {}
    now = _now_ms()
    last_probe_at = old_state.get("lastProbeAt")
    if last_probe_at and (now - last_probe_at) < _probe_interval(old_state):
        return

    _refreshing.add(key)
    _write_sync_state(anilist_id, {**old_state, "lastProbeAt": now, "syncing": True})

    async def _task():
        try:
            media, anizip = await _resolve_shared(anilist_id, fresh_media=True)
            cached_latest = _latest_episode_from_response((entry or {}).get("data"))
            next_ep = (media or {}).get("nextAiringEpisode")

            if not _should_rebuild(entry, media, anizip):
                _write_sync_state(anilist_id, {
                    "lastProbeAt": _now_ms(),
                    "lastSyncAt": old_state.get("lastSyncAt"),
                    "latestEpisode": cached_latest,
                    "nextEpisode": (next_ep or {}).get("episode"),
                    "nextAiringAt": (next_ep or {}).get("airingAt"),
                    "syncing": False,
                })
                return

            result = await _build_response(anilist_id, media, anizip, force_refresh=True)
            latest_episode = _latest_episode_from_response(result)
            await set_entry_async(f"episodes:{anilist_id}", result, FULL_TTL, NORMAL_PROBE_INTERVAL)
            _write_sync_state(anilist_id, {
                "lastProbeAt": _now_ms(),
                "lastSyncAt": _now_ms(),
                "latestEpisode": latest_episode,
                "nextEpisode": (next_ep or {}).get("episode"),
                "nextAiringAt": (next_ep or {}).get("airingAt"),
                "syncing": False,
            })
        except Exception as exc:
            logger.warning("Background episode refresh failed [%s]: %s", anilist_id, exc)
            _write_sync_state(anilist_id, {
                **old_state,
                "lastProbeAt": _now_ms(),
                "syncing": False,
                "error": str(exc),
            }, HOUR)
        finally:
            _refreshing.discard(key)

    _run_background(env, _task())

async def get_episodes_response(anilist_id: Any, env: Optional[dict] = None) -> dict:
    cache_key = f"episodes:{anilist_id}"
    entry = await get_entry_async(cache_key)

    if entry and _has_current_providers(entry.get("data")):
        _schedule_refresh(anilist_id, entry, env)
        return entry["data"]

    media, anizip = await _resolve_shared(anilist_id)
    result = await _build_response(anilist_id, media, anizip)
    await set_entry_async(cache_key, result, FULL_TTL, NORMAL_PROBE_INTERVAL)
    _write_sync_state(anilist_id, {
        "lastProbeAt": _now_ms(),
        "lastSyncAt": _now_ms(),
        "latestEpisode": _latest_episode_from_response(result),
        "nextEpisode": ((media or {}).get("nextAiringEpisode") or {}).get("episode"),
        "nextAiringAt": ((media or {}).get("nextAiringEpisode") or {}).get("airingAt"),
        "syncing": False,
    })
    return result

async def get_filtered_episodes_response(anilist_id: Any, providers: set[str], include_map: bool) -> dict:
    media, anizip = await _resolve_shared(anilist_id)

    async def _safe_mapping():
        if not include_map:
            return None
        try:
            return await map_anime_ids(anilist_id)
        except Exception:
            return None

    provider_result, mapping_result = await asyncio.gather(
        build_filtered_episodes_with_cache(anilist_id, providers, media, anizip),
        _safe_mapping(),
    )

    result = {"page": 1, "type": "filtered"}
    if include_map:
        result["mappings"] = (mapping_result or {}).get("mappings")
    result.update(provider_result)
    return result

_watch_inflight = _BoundedInflight(limit=32)

class ApiError(Exception):
    def __init__(self, message: str, status: int = 500):
        super().__init__(message)
        self.status = status

async def _cached_watch(cache_key: str, handler_fn: Callable[[], Awaitable[dict]]) -> dict:
    entry = await get_entry_async(cache_key)
    if entry and is_fresh(entry):
        return entry["data"]

    inflight = _watch_inflight.get(cache_key)
    if inflight is not None:
        try:
            await inflight
        except Exception:
            pass
        warm = await get_entry_async(cache_key)
        if warm and is_fresh(warm):
            return warm["data"]
        return await handler_fn()

    async def _runner():
        data = await handler_fn()
        try:
            await set_entry_async(cache_key, data, WATCH_TTL)
        except Exception:
            pass
        return data

    task = asyncio.ensure_future(_runner())
    _watch_inflight.add(cache_key, task)
    try:
        return await task
    finally:
        _watch_inflight.discard(cache_key)

_MAP_RE = re.compile(r"^/map/(\d+)/?$")
_EPISODES_MULTI_RE = re.compile(r"^/episodes/((?:[\w-]+/)+)(\d+)/?$", re.IGNORECASE)
_EPISODES_ALL_RE = re.compile(r"^/episodes/(\d+)/?$")

WATCH_PROVIDERS = {
    "allmanga", "reanime", "anikoto", "animegg", "anineko", "anidbapp",
    "animenosub", "anizone", "anibd", "kaa", "animedunya",
}
STREAM_PROVIDERS = {"reanime"}
STREAM_DOWNLOAD_PROVIDERS: set[str] = set()

_WATCH_RE = re.compile(r"^/watch/([\w-]+)/(\d+)/(sub|dub)/[\w-]+-(\d+)/?$")
_STREAM_RE = re.compile(r"^/stream/([\w-]+)/(\d+)/(sub|dub)/(\d+)/?$")
_STREAM_DOWNLOAD_RE = re.compile(r"^/stream/([\w-]+)/download/(\d+)/(sub|dub)/(\d+)/?$")

async def handle(path: str, query: Optional[dict] = None, env: Optional[dict] = None) -> dict:
    query = query or {}

    m = _MAP_RE.match(path)
    if m:
        anilist_id = m.group(1)
        cache_key = f"map:{anilist_id}"
        entry = await get_entry_async(cache_key)
        if entry and is_fresh(entry):
            return entry["data"]

        try:
            media = None
            try:
                media = await get_media(anilist_id)
            except Exception:
                media = None
            data = await map_anime_ids(anilist_id)
            await set_entry_async(cache_key, data, map_ttl((media or {}).get("status", "RELEASING")))
            return data
        except Exception as exc:
            if entry:
                return entry["data"]
            raise ApiError(str(exc), 500)

    m = _EPISODES_MULTI_RE.match(path)
    if m:
        raw_names = m.group(1).rstrip("/").split("/")
        anilist_id = m.group(2)
        include_map = query.get("map") != "false"
        resolution = resolve_providers(raw_names)

        if not resolution["resolved"]:
            raise ApiError(f"No valid providers specified: unknown={resolution['unknown']}", 400)

        try:
            data = await get_filtered_episodes_response(anilist_id, resolution["resolved"], include_map)
            if resolution["unknown"]:
                data["_unknownProviders"] = resolution["unknown"]
            return data
        except Exception as exc:
            raise ApiError(str(exc), 500)

    m = _EPISODES_ALL_RE.match(path)
    if m:
        anilist_id = m.group(1)
        try:
            return await get_episodes_response(anilist_id, env)
        except Exception as exc:
            raise ApiError(str(exc), 500)

    m = _STREAM_DOWNLOAD_RE.match(path)
    if m:
        provider_name, anilist_id, audio, ep = m.groups()
        if provider_name not in STREAM_DOWNLOAD_PROVIDERS:
            raise ApiError(f"Provider '{provider_name}' has no /stream/download route", 404)
        provider = PROVIDER_REGISTRY.get(provider_name)
        if provider is None or not hasattr(provider, "get_stream"):
            raise ApiError(f"Unknown provider '{provider_name}'", 404)
        return await provider.get_stream(anilist_id, audio, ep, download=True)

    m = _STREAM_RE.match(path)
    if m:
        provider_name, anilist_id, audio, ep = m.groups()
        if provider_name not in STREAM_PROVIDERS:
            raise ApiError(f"Provider '{provider_name}' has no /stream route", 404)
        provider = PROVIDER_REGISTRY.get(provider_name)
        if provider is None or not hasattr(provider, "get_stream"):
            raise ApiError(f"Unknown provider '{provider_name}'", 404)
        return await provider.get_stream(anilist_id, audio, ep)

    m = _WATCH_RE.match(path)
    if m:
        provider_name, anilist_id, audio, ep = m.groups()
        if provider_name not in WATCH_PROVIDERS:
            raise ApiError(f"Provider '{provider_name}' has no /watch route", 404)
        provider = PROVIDER_REGISTRY.get(provider_name)
        if provider is None or not hasattr(provider, "get_watch"):
            raise ApiError(f"Unknown provider '{provider_name}'", 404)

        cache_key = f"watch:{provider_name}:{anilist_id}:{audio}:{ep}"
        return await _cached_watch(cache_key, lambda: provider.get_watch(anilist_id, audio, ep))

    return {
        "name": "Anivexa API 2.1",
        "cache": True,
        "providers": sorted(PROVIDER_REGISTRY.keys()),
        "routes": [
            "/map/:anilistId",
            "/episodes/:anilistId",
            "/episodes/:provider[/:provider...]/:anilistId?map=true|false",
            "/watch/:provider/:id/sub|dub/:provider-:ep",
            "/stream/:provider/:id/sub|dub/:ep",
            "/stream/:provider/download/:id/sub|dub/:ep",
        ],
    }
