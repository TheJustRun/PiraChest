from __future__ import annotations

import re
from typing import Any, Optional
from urllib.parse import quote, urlparse

from ..anime_backend import (
    attr, build_titles, decode_entities, episode_meta, expected_count,
    fetch_html, find_top_slugs, get_prequel_offset, select_series, strip_tags,
    get_media, get_entry as get, set_entry as cache_set, is_fresh, SHOW_IDENTITY_TTL,
)

BASE = "https://anineko.to"


async def search(query: str) -> list[dict]:
    html = await fetch_html(f"{BASE}/browser?keyword={quote(query)}")
    results = []
    for m in re.finditer(r'<a\b[^>]*class=["\'][^"\']*nv-anime-thumb[^"\']*["\'][^>]*>[\s\S]*?</a>', html, re.IGNORECASE):
        tag_m = re.search(r"<a\b[^>]*>", m.group(0), re.IGNORECASE)
        tag = tag_m.group(0) if tag_m else ""
        href = attr(tag, "href")
        slug_m = re.search(r"/watch/([^/?#]+)", href)
        if not slug_m:
            continue
        slug = slug_m.group(1)
        title_m = re.search(
            r'<(?:h3|[^>]+class=["\'][^"\']*nv-anime-title[^"\']*["\'][^>]*)>([\s\S]*?)</(?:h3|[^>]+)>',
            m.group(0), re.IGNORECASE,
        )
        text = strip_tags(title_m.group(1)) if title_m else slug.replace("-", " ")
        results.append({"slug": slug, "text": text})
    return results


async def scrape_series(slug: str) -> list[dict]:
    html = await fetch_html(f"{BASE}/watch/{slug}")
    episodes = []
    for m in re.finditer(
        r'<article\b[^>]*class=["\'][^"\']*nv-info-episode-item[^"\']*["\'][^>]*>([\s\S]*?)</article>',
        html, re.IGNORECASE,
    ):
        block = m.group(1)
        link_m = re.search(r'<a\b[^>]*class=["\'][^"\']*nv-info-episode-main[^"\']*["\'][^>]*>', block, re.IGNORECASE)
        link = link_m.group(0) if link_m else ""
        href = attr(link, "href")
        num_m = re.search(r"/ep-(\d+)", href)
        if not num_m:
            continue
        num = int(num_m.group(1))
        title_m = re.search(
            r'<a\b[^>]*class=["\'][^"\']*nv-info-episode-main[^"\']*["\'][^>]*>[\s\S]*?<span[^>]*>([\s\S]*?)</span>',
            block, re.IGNORECASE,
        )
        title = strip_tags(title_m.group(1)) if title_m else ""
        badges = [strip_tags(b.group(1)).lower() for b in re.finditer(r"<span\b[^>]*>([\s\S]*?)</span>", block, re.IGNORECASE)]
        episodes.append({
            "number": num,
            "title": title or f"Episode {num}",
            "epSlug": f"ep-{num}",
            "hasSub": "sub" in badges,
            "hasDub": "dub" in badges,
        })

    episodes.sort(key=lambda e: e["number"])
    seen = set()
    out = []
    for e in episodes:
        if e["number"] in seen:
            continue
        seen.add(e["number"])
        out.append(e)
    return out


_HLS_PATTERNS = [
    re.compile(r'const\s+src\s*=\s*["\'](https?://[^"\']+\.m3u8[^"\']*)["\']', re.IGNORECASE),
    re.compile(r'file\s*:\s*["\'](https?://[^"\']+\.m3u8[^"\']*)["\']', re.IGNORECASE),
    re.compile(r'["\'](https?://[^"\']+/master\.m3u8[^"\']*)["\']', re.IGNORECASE),
    re.compile(r'["\'](https?://[^"\']+\.m3u8[^"\']*)["\']', re.IGNORECASE),
]


async def _extract_hls(embed_url: str) -> Optional[str]:
    try:
        html = await fetch_html(embed_url, {"Referer": f"{BASE}/"})
    except Exception:
        html = ""
    for pattern in _HLS_PATTERNS:
        m = pattern.search(html)
        if m:
            return decode_entities(m.group(1))
    return None


_SERVER_GRID_RE = re.compile(
    r'<div\b[^>]*class=["\'][^"\']*nv-server-grid[^"\']*["\'][^>]*data-id=["\']([^"\']+)["\'][^>]*>([\s\S]*?)(?=<div\b[^>]*class=["\'][^"\']*nv-server-grid|$)',
    re.IGNORECASE,
)
_DATA_VIDEO_RE = re.compile(r'data-video=["\']([^"\']+)["\']', re.IGNORECASE)


async def scrape_episode_watch(series_slug: str, ep_slug: str, audio: str) -> list[dict]:
    html = await fetch_html(f"{BASE}/watch/{series_slug}/{ep_slug}", {"Referer": f"{BASE}/watch/{series_slug}"})
    by_audio: dict[str, list[str]] = {"sub": [], "dub": []}
    for panel_m in _SERVER_GRID_RE.finditer(html):
        raw_audio = panel_m.group(1).lower()
        panel_audio = "dub" if "dub" in raw_audio else "sub"
        for btn_m in _DATA_VIDEO_RE.finditer(panel_m.group(2)):
            by_audio[panel_audio].append(decode_entities(btn_m.group(1)))

    audios = ["sub", "dub"] if audio == "all" else [audio]
    streams: list[dict] = []

    import asyncio

    async def _resolve_one(aud: str):
        embeds = by_audio.get(aud, [])

        async def _one(i: int, embed: str) -> dict:
            hls = await _extract_hls(embed)
            origin = f"{urlparse(embed).scheme}://{urlparse(embed).netloc}/"
            return {
                "url": hls or embed,
                "type": "hls" if hls else "embed",
                "embed": embed,
                "audio": aud,
                "server": "AniNeko",
                "priority": len(embeds) - i,
                "referer": origin,
                "isActive": i == 0,
            }

        resolved = await asyncio.gather(*(_one(i, e) for i, e in enumerate(embeds)))
        streams.extend(resolved)

    await asyncio.gather(*(_resolve_one(aud) for aud in audios))
    return streams


async def resolve_series(anilist_id: Any, ctx: Optional[dict] = None) -> dict:
    ctx = ctx or {}
    cache_key = f"np:anineko:{anilist_id}"
    cached = get(cache_key)
    if is_fresh(cached):
        return cached["data"]

    media = ctx.get("media") or await get_media(anilist_id)
    titles = build_titles(media, ctx.get("anizip"))
    candidates = await find_top_slugs(titles, search)
    expected = expected_count(media, ctx.get("anizip"), ctx.get("jikanEps"))
    try:
        offset = await get_prequel_offset(anilist_id)
    except Exception:
        offset = 0

    selected = await select_series(candidates, scrape_series, expected, (media or {}).get("status"), offset)
    if not selected:
        raise RuntimeError(f"AniNeko match not found for AniList {anilist_id}")

    data = {"slug": selected["slug"], "title": selected["title"], "mode": selected["mode"], "offset": offset, "score": selected["score"]}
    cache_set(cache_key, data, SHOW_IDENTITY_TTL)
    return data


def _build_episode_lists(anilist_id: Any, series: dict, provider_episodes: list[dict], ctx: dict, expected: Optional[int]) -> dict:
    sub, dub = [], []
    for src in provider_episodes:
        number = src["number"] - series["offset"] if series["mode"] == "offset" else src["number"]
        if number < 1:
            continue
        if expected and number > expected:
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
            sub.append({"id": f"watch/anineko/{anilist_id}/sub/anineko-{number}", **base, "audio": "sub"})
        if src["hasDub"]:
            dub.append({"id": f"watch/anineko/{anilist_id}/dub/anineko-{number}", **base, "audio": "dub"})
    return {"sub": sub, "dub": dub}


async def get_episodes(anilist_id: Any, ctx: Optional[dict] = None) -> dict:
    ctx = ctx or {}
    media = ctx.get("media") or await get_media(anilist_id)
    local_ctx = {**ctx, "media": media}
    series = await resolve_series(anilist_id, local_ctx)
    episodes = await scrape_series(series["slug"])
    expected = expected_count(media, ctx.get("anizip"), ctx.get("jikanEps"))
    return {
        "meta": {
            "id": series["slug"],
            "title": series["title"],
            "source": "anineko",
            "matchScore": round(series["score"], 3),
            "numbering": series["mode"],
            "episodeOffset": series["offset"] if series["mode"] == "offset" else 0,
        },
        "episodes": _build_episode_lists(anilist_id, series, episodes, local_ctx, expected),
    }


async def get_watch(anilist_id: Any, audio: str, ep_num: Any, ctx: Optional[dict] = None) -> dict:
    ctx = ctx or {}
    series = await resolve_series(anilist_id, ctx)
    provider_ep = int(ep_num) + series["offset"] if series["mode"] == "offset" else int(ep_num)
    streams = await scrape_episode_watch(series["slug"], f"ep-{provider_ep}", audio)
    return {
        "anilistId": int(anilist_id),
        "episode": int(ep_num),
        "providerEpisode": provider_ep,
        "audio": audio,
        "streams": streams,
    }
