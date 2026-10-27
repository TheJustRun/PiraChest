import asyncio
import json
import logging
import re
from typing import Any, Optional
from urllib.parse import quote_plus

import httpx

from ..anime_backend import (
    MIN, SHOW_IDENTITY_TTL, UA, _request,
    build_titles, decode_entities, episode_meta, expected_count,
    find_top_slugs, get_prequel_offset, get_media,
    select_series, get_entry as get, set_entry as cache_set, is_fresh,
)

logger = logging.getLogger(__name__)

BASE = "https://anizone.to"

_HTML_HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": f"{BASE}/",
}

_EP_NUM_RE = re.compile(r"/(\d+)(?:[?#].*)?$")
_JSON_ARG_RES: dict[str, re.Pattern] = {}

_SEARCH_TTL = 30 * MIN
_YEAR_RE = re.compile(r"\(\d{4}\)")
_PAREN_RE = re.compile(r"\([^)]*\)")
_SEASON_RE = re.compile(r"\b(?:season|part|cour)\s*\d+\b|\b\d+(?:st|nd|rd|th)\s+season\b", re.IGNORECASE)
_WS_RE = re.compile(r"\s+")


_search_locks: dict[tuple[int, str], asyncio.Lock] = {}


def _search_queries(titles: list[str]) -> list[str]:
    queries: list[str] = []
    for title in titles[:2]:
        title = (title or "").strip()
        stripped = _WS_RE.sub(" ", _SEASON_RE.sub("", _YEAR_RE.sub("", title))).strip(" -:")
        no_parens = _WS_RE.sub(" ", _PAREN_RE.sub(" ", title)).strip(" -:")
        for candidate in (title, stripped, no_parens):
            if len(candidate) >= 3 and candidate not in queries:
                queries.append(candidate)
    return queries[:4]


def _extract_json_arg(text: str, key: str) -> Optional[str]:
    pat = _JSON_ARG_RES.get(key)
    if pat is None:
        pat = _JSON_ARG_RES[key] = re.compile(
            re.escape(key) + r"\s*[:(]\s*JSON\.parse\('((?:[^'\\]|\\.)*)'\)"
        )
    m = pat.search(text)
    return m.group(1) if m else None


def _process_json_arg(raw: str) -> Any:
    s = decode_entities(raw).replace("\\'", "'")
    try:
        return json.loads(json.loads(f'"{s}"'))
    except (json.JSONDecodeError, ValueError):
        pass
    try:
        return json.loads(s)
    except (json.JSONDecodeError, ValueError):
        return {}


def _pick_title(titles: dict) -> str:
    return titles.get("1") or titles.get("5") or titles.get("8") or (next(iter(titles.values()), "") if titles else "")


_ATTEMPT_TIMEOUTS = (10, 10, 50)


async def _fetch_anizone(url: str) -> str:
    merged = {"User-Agent": UA, **_HTML_HEADERS}
    last: Optional[Exception] = None
    for attempt, read_timeout in enumerate(_ATTEMPT_TIMEOUTS):
        try:
            resp = await _request("GET", url, headers=merged, retries=0, timeout=httpx.Timeout(read_timeout, connect=6))
            if resp.status_code < 400:
                return resp.text
            last = RuntimeError(f"HTTP {resp.status_code} fetching {url}")
            if resp.status_code < 500 and resp.status_code != 429:
                break
        except httpx.TransportError as exc:
            last = exc
        if attempt < len(_ATTEMPT_TIMEOUTS) - 1:
            await asyncio.sleep(0.3)
    raise last or RuntimeError(f"AniZone fetch failed: {url}")


async def search(query: str) -> list[dict]:
    html = await _fetch_anizone(f"{BASE}/anime?search={quote_plus(query)}")
    raw = _extract_json_arg(html, "items")
    items = _process_json_arg(raw) if raw else []

    results = []
    seen: set[str] = set()
    for it in items if isinstance(items, list) else []:
        if not isinstance(it, dict):
            continue
        slug = str(it.get("slug") or "").strip().strip("/")
        title = it.get("main_title") or _pick_title(it.get("title_list") or {})
        if not slug or not title or slug in seen:
            continue
        seen.add(slug)
        results.append({"slug": slug, "text": title})

    if not results:
        logger.warning("AniZone search: 0 results for query %r (html_len=%d)", query, len(html))
    return results


async def _cached_search(query: str) -> list[dict]:
    cache_key = f"np:anizone:q:{query.lower()}"
    entry = get(cache_key)
    if is_fresh(entry) and entry.get("data") is not None:
        return entry["data"]

    loop_key = (id(asyncio.get_running_loop()), cache_key)
    async with _search_locks.setdefault(loop_key, asyncio.Lock()):
        entry = get(cache_key)
        if is_fresh(entry) and entry.get("data") is not None:
            return entry["data"]
        results = await search(query)
        if results:
            cache_set(cache_key, results, _SEARCH_TTL)
        return results


_EPS_TTL = 10 * MIN


async def scrape_series(slug: str) -> list[dict]:
    cache_key = f"np:anizone:eps:{slug}"
    entry = get(cache_key)
    if is_fresh(entry) and entry.get("data"):
        return entry["data"]

    html = await _fetch_anizone(f"{BASE}/anime/{slug}")
    raw = _extract_json_arg(html, "items")
    items = _process_json_arg(raw) if raw else []

    episodes = []
    seen: set[int] = set()
    for it in items if isinstance(items, list) else []:
        if not isinstance(it, dict):
            continue
        num_m = _EP_NUM_RE.search(str(it.get("url") or ""))
        if num_m:
            num = int(num_m.group(1))
        elif str(it.get("slug") or "").isdigit():
            num = int(it["slug"])
        else:
            continue
        if num < 1 or num in seen:
            continue
        seen.add(num)
        title = _pick_title(it.get("title_list") or {}) or f"Episode {num}"
        episodes.append({"number": num, "title": title, "hasSub": True, "hasDub": False})

    episodes.sort(key=lambda e: e["number"])
    if episodes:
        cache_set(cache_key, episodes, _EPS_TTL)
    return episodes


async def scrape_watch(slug: str, episode_num: Any) -> dict:
    html = await _fetch_anizone(f"{BASE}/anime/{slug}/{episode_num}")
    raw = _extract_json_arg(html, "vidstackPlayer")
    player = _process_json_arg(raw) if raw else {}
    if not isinstance(player, dict):
        player = {}

    subtitles = []
    for t in player.get("subtitles") or []:
        if not isinstance(t, dict) or not t.get("file"):
            continue
        subtitles.append({
            "url": t["file"],
            "label": t.get("title") or t.get("language") or "Subtitle",
            "srclang": t.get("language") or "",
            "format": t.get("format") or "vtt",
            "default": bool(t.get("default")),
        })

    return {
        "hls": player.get("src") or None,
        "subtitles": subtitles,
        "storyboard": player.get("storyboard") or None,
        "chapters": player.get("chapter") or None,
    }


async def resolve_series(anilist_id: Any, ctx: Optional[dict] = None) -> dict:
    ctx = ctx or {}
    cache_key = f"np:anizone:{anilist_id}"
    cached = get(cache_key)
    if is_fresh(cached):
        return cached["data"]

    media = ctx.get("media") or await get_media(anilist_id)
    titles = build_titles(media, ctx.get("anizip"))
    candidates = await find_top_slugs(titles, _cached_search, queries=_search_queries(titles))

    season_year = (media or {}).get("seasonYear")
    if season_year and any(re.search(r"\(\d{4}\)", c["title"]) for c in candidates):
        rescored = []
        for c in candidates:
            m = re.search(r"\((\d{4})\)", c["title"])
            if m:
                if int(m.group(1)) == season_year:
                    rescored.append({**c, "score": min(1.0, c["score"] * 1.3)})
                else:
                    rescored.append({**c, "score": c["score"] * 0.5})
            else:
                rescored.append({**c, "score": c["score"] * 0.65})
        candidates = sorted(rescored, key=lambda c: c["score"], reverse=True)

    expected = expected_count(media, ctx.get("anizip"), ctx.get("jikanEps"))
    try:
        offset = await get_prequel_offset(anilist_id)
    except Exception:
        offset = 0

    selected = await select_series(candidates, scrape_series, expected, (media or {}).get("status"), offset)
    if not selected:
        raise RuntimeError(f"AniZone match not found for AniList {anilist_id}")

    data = {"slug": selected["slug"], "title": selected["title"], "mode": selected["mode"], "offset": offset, "score": selected["score"]}
    cache_set(cache_key, data, SHOW_IDENTITY_TTL)
    return data


def _build_episode_lists(anilist_id: Any, series: dict, provider_episodes: list[dict], ctx: dict) -> dict:
    sub, dub = [], []
    for src in provider_episodes:
        number = src["number"] - series["offset"] if series["mode"] == "offset" else src["number"]
        if number < 1:
            continue
        meta = episode_meta(number, ctx)
        base = {
            "number": number,
            "title": meta["title"] or src.get("title") or f"Episode {number}",
            "duration": meta["duration"],
            "filler": meta["filler"],
            "uncensored": meta["uncensored"],
            "description": meta["description"],
            "image": meta["image"],
            "airDate": meta["airDate"],
            "sourceNumber": src["number"],
        }
        if src["hasSub"]:
            sub.append({"id": f"watch/anizone/{anilist_id}/sub/anizone-{number}", **base, "audio": "sub"})
        if src["hasDub"]:
            dub.append({"id": f"watch/anizone/{anilist_id}/dub/anizone-{number}", **base, "audio": "dub"})
    return {"sub": sub, "dub": dub}


async def get_episodes(anilist_id: Any, ctx: Optional[dict] = None) -> dict:
    ctx = ctx or {}
    media = ctx.get("media") or await get_media(anilist_id)
    local_ctx = {**ctx, "media": media}
    series = await resolve_series(anilist_id, local_ctx)
    episodes = await scrape_series(series["slug"])
    return {
        "meta": {
            "id": series["slug"],
            "title": series["title"],
            "source": "anizone",
            "matchScore": round(series["score"], 3),
            "numbering": series["mode"],
            "episodeOffset": series["offset"] if series["mode"] == "offset" else 0,
        },
        "episodes": _build_episode_lists(anilist_id, series, episodes, local_ctx),
    }


async def get_watch(anilist_id: Any, audio: str, ep_num: Any, ctx: Optional[dict] = None) -> dict:
    ctx = ctx or {}
    series = await resolve_series(anilist_id, ctx)
    provider_ep = int(ep_num) + series["offset"] if series["mode"] == "offset" else int(ep_num)
    watch = await scrape_watch(series["slug"], provider_ep)
    if not watch["hls"]:
        raise RuntimeError(f"No HLS stream found for AniZone episode {provider_ep}")
    return {
        "anilistId": int(anilist_id),
        "episode": int(ep_num),
        "providerEpisode": provider_ep,
        "audio": audio,
        "streams": [{
            "url": watch["hls"],
            "type": "hls",
            "server": "AniZone",
            "subtitles": watch["subtitles"],
            "storyboard": watch["storyboard"],
            "chapters": watch["chapters"],
            "priority": 1,
            "isActive": True,
        }],
    }
