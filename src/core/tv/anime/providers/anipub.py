import asyncio
import logging
import math
import re
from typing import Any, Optional
from urllib.parse import quote

from ..anime_backend import (
    MIN, SHOW_IDENTITY_TTL, UA, build_titles, episode_meta, expected_count, fetch_html,
    fetch_json, title_score, get_media, get_entry as get, set_entry as cache_set, is_fresh,
)
from . import megaplay

logger = logging.getLogger(__name__)

BASE = "https://www.anipub.org"
_DETAILS_TTL = 30 * MIN
_SEARCH_MIN_LEN = 3
_HEADERS = {"Referer": f"{BASE}/", "Accept": "application/json"}
_PAGE_HEADERS = {"Referer": f"{BASE}/", "Accept-Language": "en-US,en;q=0.9"}

_STRIP_RE = re.compile(r"\b(?:season|part|cour)\s*\d+\b|\b\d+(?:st|nd|rd|th)\s+season\b", re.IGNORECASE)
_PAREN_RE = re.compile(r"\([^)]*\)")
_SRC_RE = re.compile(r"""src=["']?(https?://[^\s"'<>]+)""", re.IGNORECASE)
_WS_RE = re.compile(r"\s+")


def _search_queries(titles: list[str]) -> list[str]:
    queries: list[str] = []
    for title in titles[:2]:
        title = (title or "").strip()
        stripped = _WS_RE.sub(" ", _STRIP_RE.sub("", title)).strip(" -:")
        no_parens = _WS_RE.sub(" ", _PAREN_RE.sub(" ", title)).strip(" -:")
        for candidate in (title, stripped, no_parens):
            if len(candidate) >= _SEARCH_MIN_LEN and candidate not in queries:
                queries.append(candidate)
    return queries[:4]


async def search(query: str) -> list[dict]:
    data = await fetch_json(f"{BASE}/api/search/{quote(query, safe='')}", _HEADERS)
    items = data if isinstance(data, list) else ([data] if isinstance(data, dict) and data.get("Id") else [])

    results: list[dict] = []
    seen: set[str] = set()
    for item in items:
        if not isinstance(item, dict):
            continue
        slug = str(item.get("Id") or "").strip()
        name = (item.get("Name") or "").strip()
        if not slug or not name or slug in seen:
            continue
        seen.add(slug)
        results.append({"slug": slug, "text": name, "finder": (item.get("finder") or "").strip()})
    return results


async def _fetch_details(show_id: Any) -> dict:
    cache_key = f"np:anipub:details:{show_id}"
    entry = get(cache_key)
    if is_fresh(entry) and entry.get("data"):
        return entry["data"]

    data = await fetch_json(f"{BASE}/v1/api/details/{int(show_id)}", _HEADERS)
    local = data.get("local") if isinstance(data, dict) else None
    if not isinstance(local, dict):
        raise RuntimeError(f"anipub: details missing for id {show_id}")

    cache_set(cache_key, local, _DETAILS_TTL)
    return local


def _episodes_from_details(local: dict) -> list[dict]:
    episodes = local.get("ep") or []
    numbers = range(1, len(episodes) + 2)

    out: list[dict] = []
    for number in numbers:
        title = ""
        if number >= 2:
            item = episodes[number - 2]
            if isinstance(item, dict):
                title = (item.get("name") or "").strip()
        out.append({"number": number, "title": title})
    return out


async def scrape_series(slug: str) -> list[dict]:
    return _episodes_from_details(await _fetch_details(slug))


async def _find_show(media: dict, ctx: dict) -> dict:
    titles = build_titles(media, ctx.get("anizip"))
    queries = _search_queries(titles)
    if not queries:
        raise RuntimeError(f"anipub: no searchable titles for AniList {(media or {}).get('id')}")

    results = await asyncio.gather(*(search(q) for q in queries), return_exceptions=True)

    candidates: dict[str, dict] = {}
    for result in results:
        if isinstance(result, BaseException):
            logger.warning("anipub search failed: %s: %s", type(result).__name__, result)
            continue
        for candidate in result:
            candidates.setdefault(candidate["slug"], candidate)
    if not candidates:
        raise RuntimeError(f"AniPub search: no candidates for {titles[:1]}")

    scored: list[dict] = []
    for candidate in candidates.values():
        score = max(
            (title_score(t, candidate["text"], candidate.get("finder") or "") for t in titles[:2] if t),
            default=0.0,
        )
        if score >= 0.5:
            scored.append({**candidate, "score": score})
    scored.sort(key=lambda c: c["score"], reverse=True)
    if not scored:
        raise RuntimeError(f"AniPub match not found for {titles[:1]}")

    expected = expected_count(media, ctx.get("anizip"), ctx.get("jikanEps"))
    needed = 0
    if expected and expected >= 6:
        needed = math.ceil(expected * 0.9) if (media or {}).get("status") == "FINISHED" else max(1, expected - 3)

    best: Optional[dict] = None
    for candidate in scored[:3] if expected else scored[:1]:
        try:
            episodes = await scrape_series(candidate["slug"])
        except Exception as exc:
            logger.warning("anipub details failed for id %s: %s: %s", candidate["slug"], type(exc).__name__, exc)
            continue
        hits = len([e for e in episodes if 1 <= e["number"] <= expected]) if expected else len(episodes)
        count_score = 1.0 if not needed else (1.0 if hits >= needed else hits / needed)
        combined = candidate["score"] * 0.7 + count_score * 0.3
        if best is None or combined > best["score"]:
            best = {**candidate, "score": combined}
        if needed and hits >= needed:
            break

    if best is None:
        raise RuntimeError(f"AniPub: could not load details for {titles[:1]}")
    return best


async def _resolve_show(anilist_id: Any, media: dict, ctx: dict) -> dict:
    cache_key = f"np:anipub:{anilist_id}"
    entry = get(cache_key)
    if is_fresh(entry) and entry.get("data"):
        return entry["data"]

    chosen = await _find_show(media, ctx)
    data = {"id": chosen["slug"], "title": chosen["text"], "score": round(chosen["score"], 3)}
    cache_set(cache_key, data, SHOW_IDENTITY_TTL)
    return data


def _build_episode_lists(anilist_id: Any, episodes: list[dict], ctx: dict) -> dict:
    sub: list[dict] = []
    dub: list[dict] = []
    for src in episodes:
        number = src["number"]
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
        }
        sub.append({"id": f"watch/anipub/{anilist_id}/sub/anipub-{number}", **base, "audio": "sub"})
        dub.append({"id": f"watch/anipub/{anilist_id}/dub/anipub-{number}", **base, "audio": "dub"})
    return {"sub": sub, "dub": dub}


async def get_episodes(anilist_id: Any, ctx: Optional[dict] = None) -> dict:
    ctx = ctx or {}
    media = ctx.get("media") or await get_media(anilist_id)
    if not media:
        raise RuntimeError(f"Could not resolve media for AniList ID: {anilist_id}")

    local_ctx = {**ctx, "media": media}
    show = await _resolve_show(anilist_id, media, local_ctx)
    local = await _fetch_details(show["id"])
    episodes = _episodes_from_details(local)

    return {
        "meta": {
            "id": show["id"],
            "title": show["title"],
            "source": "anipub",
            "matchScore": show["score"],
            "numbering": "standard",
            "episodeOffset": 0,
        },
        "episodes": _build_episode_lists(anilist_id, episodes, local_ctx),
    }


def _episode_url(local: dict, number: int) -> Optional[str]:
    raw: Any = local.get("link") if number == 1 else None
    if number >= 2:
        episodes = local.get("ep") or []
        index = number - 2
        if 0 <= index < len(episodes) and isinstance(episodes[index], dict):
            raw = episodes[index].get("link")
    m = _SRC_RE.search(str(raw or ""))
    return m.group(1) if m else None


def _rewrite_audio(url: str, audio: str) -> str:
    if re.search(r"[?&]type=(sub|dub)\b", url, re.IGNORECASE):
        return re.sub(
            r"([?&]type=)(sub|dub)\b", lambda m: f"{m.group(1)}{audio}", url, count=1, flags=re.IGNORECASE
        )
    return re.sub(r"/(sub|dub)(?=[/?#]|$)", f"/{audio}", url, count=1, flags=re.IGNORECASE)


async def _episode_streams(url: str, audio: str) -> list[dict]:
    targets: list[str] = []
    for candidate in (_rewrite_audio(url, audio), url):
        if candidate not in targets:
            targets.append(candidate)

    last_exc: Optional[Exception] = None
    for target in targets:
        try:
            page = await fetch_html(target, _PAGE_HEADERS)
        except Exception as exc:
            last_exc = exc
            continue
        embed = megaplay.find_embed_url(page)
        if not embed:
            last_exc = RuntimeError(f"anipub: no player iframe on {target}")
            continue
        try:
            resolved = await megaplay.resolve(embed, referer=target)
        except Exception as exc:
            last_exc = exc
            logger.warning("anipub: megaplay resolve failed for %s: %s", embed, exc)
            return [{
                "url": embed, "type": "embed", "server": "AniPub",
                "referer": megaplay.REFERER, "priority": 6, "isActive": False,
            }]

        stream = {
            "url": resolved["url"],
            "type": "hls",
            "server": "AniPub",
            "referer": resolved["referer"],
            "subtitles": resolved["subtitles"],
            "priority": 4,
            "isActive": True,
        }
        if resolved.get("intro"):
            stream["intro"] = resolved["intro"]
        if resolved.get("outro"):
            stream["outro"] = resolved["outro"]
        return [stream]

    raise last_exc or RuntimeError(f"anipub: could not resolve {url}")


async def get_watch(anilist_id: Any, audio: str, ep_num: Any, ctx: Optional[dict] = None) -> dict:
    ctx = ctx or {}
    ep_num = int(ep_num)
    if audio not in ("sub", "dub"):
        raise RuntimeError("audio must be sub or dub")

    media = ctx.get("media") or await get_media(anilist_id)
    if not media:
        raise RuntimeError(f"Could not resolve media for AniList ID: {anilist_id}")

    show = await _resolve_show(anilist_id, media, ctx)
    local = await _fetch_details(show["id"])
    source_url = _episode_url(local, ep_num)
    if not source_url:
        raise RuntimeError(f"AniPub episode {ep_num} not found for {show['title']}")

    streams = await _episode_streams(source_url, audio)
    referer = (streams[0].get("referer") if streams else None) or f"{BASE}/"
    return {
        "anilistId": int(anilist_id),
        "episode": ep_num,
        "audio": audio,
        "streams": streams,
        "subtitles": (streams[0].get("subtitles") if streams else None) or [],
        "headers": {"User-Agent": UA, "Referer": referer},
    }
