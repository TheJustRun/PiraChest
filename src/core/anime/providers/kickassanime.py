from __future__ import annotations

import logging
import re
from typing import Any, Optional

import httpx

from ..anime_backend import (
    build_titles, dice_coeff, episode_meta, expected_count,
    get_media, get_entry as get, set_entry as cache_set, is_fresh, SHOW_IDENTITY_TTL,
)

logger = logging.getLogger(__name__)

BASE = "https://kaa.lt"
HLS_BASE = "https://hls.krussdomi.com/manifest"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
H = {"User-Agent": UA, "Accept": "application/json"}


async def _kaa_search(query: str) -> list[dict]:
    async with httpx.AsyncClient(timeout=20) as client:
        resp = await client.post(f"{BASE}/api/fsearch", headers={**H, "Content-Type": "application/json"}, json={"page": 1, "query": query})
    if resp.status_code >= 400:
        raise RuntimeError(f"kaa fsearch HTTP {resp.status_code}")
    data = resp.json()
    return data.get("result") if isinstance(data.get("result"), list) else []


async def _kaa_show_info(show_slug: str) -> dict:
    async with httpx.AsyncClient(timeout=20) as client:
        resp = await client.get(f"{BASE}/api/show/{show_slug}", headers=H)
    if resp.status_code >= 400:
        raise RuntimeError(f"kaa show HTTP {resp.status_code}: {show_slug}")
    return resp.json()


async def _kaa_episode_page(show_slug: str, ep: Any) -> dict:
    async with httpx.AsyncClient(timeout=20) as client:
        resp = await client.get(f"{BASE}/api/show/{show_slug}/episodes", params={"ep": ep, "lang": "ja-JP"}, headers=H)
    if resp.status_code >= 400:
        raise RuntimeError(f"kaa episodes HTTP {resp.status_code}")
    return resp.json()


async def _kaa_all_episodes(show_slug: str) -> list[dict]:
    import asyncio

    first = await _kaa_episode_page(show_slug, 1)
    pages = first.get("pages") if isinstance(first.get("pages"), list) else []
    all_eps = list(first.get("result") or [])

    if len(pages) > 1:
        async def _fetch_rest(pg: dict) -> list[dict]:
            start_ep = (pg.get("eps") or [None])[0]
            if not start_ep:
                return []
            d = await _kaa_episode_page(show_slug, start_ep)
            return d.get("result") if isinstance(d.get("result"), list) else []

        rest = await asyncio.gather(*(_fetch_rest(pg) for pg in pages[1:]))
        for batch in rest:
            all_eps.extend(batch)

    return all_eps


async def _kaa_episode_servers(show_slug: str, full_ep_slug: str) -> dict:
    async with httpx.AsyncClient(timeout=20) as client:
        resp = await client.get(f"{BASE}/api/show/{show_slug}/episode/{full_ep_slug}", headers=H)
    if resp.status_code >= 400:
        raise RuntimeError(f"kaa episode servers HTTP {resp.status_code}")
    return resp.json()


_CJK_RE = re.compile(r"[\u3000-\u9fff\u4e00-\u9faf]")


def _build_kaa_queries(titles: list[str]) -> list[str]:
    queries: set[str] = set()
    for title in titles[:4]:
        if _CJK_RE.search(title):
            continue
        clean = re.sub(r"\s+", " ", re.sub(r"[^\w\s]", " ", title)).strip()
        if not clean or len(clean) < 3:
            continue
        words = [w for w in clean.split(" ") if w]
        if len(words) <= 3:
            queries.add(clean)
        else:
            queries.add(" ".join(words[:2]))
            queries.add(" ".join(words[:3]))
    return list(queries)


def _score_candidate(candidate: dict, titles: list[str], season_year: Optional[int], anilist_format: Optional[str]) -> float:
    title_en = candidate.get("title_en") or ""
    title_jp = candidate.get("title") or ""
    kaa_year = candidate.get("year")
    kaa_type = str(candidate.get("type") or "").lower()

    base = 0.0
    for t in titles[:3]:
        if _CJK_RE.search(t):
            continue
        base = max(base, dice_coeff(t, title_en), dice_coeff(t, title_jp))

    year_mult = 1.0
    if season_year and kaa_year:
        diff = abs(int(season_year) - int(kaa_year))
        if diff == 0:
            year_mult = 1.2
        elif diff == 1:
            year_mult = 0.8
        else:
            year_mult = 0.5

    type_mult = 1.0
    af = str(anilist_format or "").upper()
    if af == "MOVIE" and kaa_type != "movie":
        type_mult = 0.25
    elif af != "MOVIE" and kaa_type == "movie":
        type_mult = 0.25
    elif af in ("OVA", "ONA", "SPECIAL") and kaa_type == "tv":
        type_mult = 0.5
    elif af == "TV" and kaa_type in ("ova", "special"):
        type_mult = 0.5

    return min(1.0, base * year_mult) * type_mult


async def resolve_series(anilist_id: Any, ctx: Optional[dict] = None) -> dict:
    import asyncio

    ctx = ctx or {}
    cache_key = f"np:kaa:{anilist_id}"
    cached = get(cache_key)
    if is_fresh(cached):
        return cached["data"]

    media = ctx.get("media") or await get_media(anilist_id)
    titles = build_titles(media, ctx.get("anizip"))
    queries = _build_kaa_queries(titles)
    season_year = (media or {}).get("seasonYear")
    fmt = (media or {}).get("format")

    if not queries:
        raise RuntimeError(f"KAA: no usable search queries for AniList {anilist_id}")

    all_candidates: dict[str, dict] = {}

    async def _run(q: str):
        try:
            results = await _kaa_search(q)
        except Exception as exc:
            logger.warning("KAA search failed for query %r: %s", q, exc)
            return
        for r in results:
            if r.get("slug") and r["slug"] not in all_candidates:
                all_candidates[r["slug"]] = r

    await asyncio.gather(*(_run(q) for q in queries))

    if not all_candidates:
        raise RuntimeError(f"KAA: no search results for AniList {anilist_id}")

    scored = []
    for candidate in all_candidates.values():
        score = _score_candidate(candidate, titles, season_year, fmt)
        if score >= 0.5:
            scored.append({
                "slug": candidate["slug"],
                "title": candidate.get("title_en") or candidate.get("title"),
                "locales": candidate.get("locales") if isinstance(candidate.get("locales"), list) else [],
                "score": score,
            })

    scored.sort(key=lambda c: c["score"], reverse=True)

    if not scored:
        raise RuntimeError(f"KAA: no confident match for AniList {anilist_id}")

    best = scored[0]
    if best["score"] < 0.6:
        raise RuntimeError(f"KAA: low confidence match for AniList {anilist_id} — best \"{best['slug']}\" score {best['score']:.3f}")

    data = {"slug": best["slug"], "title": best["title"], "locales": best["locales"], "score": best["score"]}
    cache_set(cache_key, data, SHOW_IDENTITY_TTL)
    return data


async def _build_ep_map(show_slug: str, show_info: dict) -> list[dict]:
    if show_info.get("type") == "movie":
        m = re.search(r"/(ep-(\d+)-([a-f0-9]+))$", show_info.get("watch_uri") or "", re.IGNORECASE)
        if m:
            return [{"number": 1, "fullSlug": m.group(1)}]
        return []
    episodes = await _kaa_all_episodes(show_slug)
    return [
        {
            "number": e.get("episode_number"),
            "fullSlug": f"ep-{e.get('episode_number')}-{e.get('slug')}",
            "title": e.get("title"),
            "duration": round(e["duration_ms"] / 1000) if e.get("duration_ms") else None,
        }
        for e in episodes
    ]


async def get_episodes(anilist_id: Any, ctx: Optional[dict] = None) -> dict:
    ctx = ctx or {}
    media = ctx.get("media") or await get_media(anilist_id)
    local_ctx = {**ctx, "media": media}
    series = await resolve_series(anilist_id, local_ctx)
    show_info = await _kaa_show_info(series["slug"])

    locales = show_info.get("locales") if isinstance(show_info.get("locales"), list) else series["locales"]
    has_dub = "en-US" in locales

    ep_map = await _build_ep_map(series["slug"], show_info)
    if not ep_map:
        raise RuntimeError(f"KAA: no episodes found for AniList {anilist_id} (slug: {series['slug']})")

    expected = expected_count(media, ctx.get("anizip"), ctx.get("jikanEps"))
    sub: list[dict] = []
    dub: list[dict] = []

    for ep in ep_map:
        num = ep["number"]
        if num is None or num < 1:
            continue
        if expected and num > expected:
            continue
        meta = episode_meta(num, local_ctx)
        base = {
            "number": num,
            "title": meta["title"] or ep.get("title") or f"Episode {num}",
            "duration": meta["duration"] or ep.get("duration"),
            "filler": meta["filler"],
            "uncensored": False,
            "description": meta["description"],
            "image": meta["image"],
            "airDate": meta["airDate"],
        }
        sub.append({"id": f"watch/kaa/{anilist_id}/sub/kaa-{num}", **base, "audio": "sub"})
        if has_dub:
            dub.append({"id": f"watch/kaa/{anilist_id}/dub/kaa-{num}", **base, "audio": "dub"})

    return {
        "meta": {
            "id": series["slug"],
            "title": series["title"],
            "source": "kaa",
            "matchScore": round(series["score"], 3),
        },
        "episodes": {"sub": sub, "dub": dub},
    }


async def get_watch(anilist_id: Any, audio: str, ep_num: Any) -> dict:
    series = await resolve_series(anilist_id)
    show_info = await _kaa_show_info(series["slug"])

    locales = show_info.get("locales") if isinstance(show_info.get("locales"), list) else series["locales"]
    if audio == "dub" and "en-US" not in locales:
        raise RuntimeError(f"KAA: no English dub for AniList {anilist_id}")

    ep_map = await _build_ep_map(series["slug"], show_info)
    ep = next((e for e in ep_map if e["number"] == int(ep_num)), None)
    if not ep:
        raise RuntimeError(f"KAA: episode {ep_num} not found for AniList {anilist_id}")

    episode_data = await _kaa_episode_servers(series["slug"], ep["fullSlug"])
    servers = episode_data.get("servers") if isinstance(episode_data.get("servers"), list) else []
    if not servers:
        raise RuntimeError(f"KAA: no streams for episode {ep_num} (AniList {anilist_id})")

    streams = []
    for s in servers:
        src = s.get("src")
        if not src:
            continue
        m = re.search(r"[?&]id=([^&]+)", src)
        if not m:
            continue
        streams.append({
            "url": f"{HLS_BASE}/{m.group(1)}/master.m3u8",
            "type": "hls",
            "server": s.get("name", "KAA"),
            "headers": {"Referer": "https://krussdomi.com/"},
            "priority": 1,
            "isActive": True,
        })

    if not streams:
        raise RuntimeError(f"KAA: could not resolve stream for episode {ep_num}")

    return {"anilistId": int(anilist_id), "episode": int(ep_num), "audio": audio, "streams": streams}
