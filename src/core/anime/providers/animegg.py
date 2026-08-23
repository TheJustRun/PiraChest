from __future__ import annotations

import base64
import json
import re
from typing import Any, Optional
from urllib.parse import quote, urlparse

from ..anime_backend import (
    attr, build_titles, episode_meta, expected_count, fetch_html,
    find_top_slugs, get_prequel_offset, select_series, strip_tags,
    get_media, get_entry as get, set_entry as cache_set, is_fresh, SHOW_IDENTITY_TTL,
)

BASE = "https://www.animegg.org"


async def search(query: str) -> list[dict]:
    html = await fetch_html(f"{BASE}/search/?q={quote(query)}")
    results = []
    for m in re.finditer(r'<a\b[^>]*class=["\'][^"\']*\bmse\b[^"\']*["\'][^>]*>[\s\S]*?</a>', html, re.IGNORECASE):
        tag_m = re.search(r"<a\b[^>]*>", m.group(0), re.IGNORECASE)
        tag = tag_m.group(0) if tag_m else ""
        href = attr(tag, "href")
        slug_m = re.match(r"^/series/([^/?#]+)", href)
        if not slug_m:
            continue
        slug = slug_m.group(1)
        strong_m = re.search(r"<strong[^>]*>([\s\S]*?)</strong>", m.group(0), re.IGNORECASE)
        text = strip_tags(strong_m.group(1)) if strong_m else slug.replace("-", " ")
        results.append({"slug": slug, "text": text})
    return results


async def scrape_series(slug: str) -> list[dict]:
    html = await fetch_html(f"{BASE}/series/{slug}")
    episodes = []
    for m in re.finditer(r"<li\b[^>]*>([\s\S]*?)</li>", html, re.IGNORECASE):
        block = m.group(1)
        if not re.search(r"\banm_det_pop\b", block):
            continue
        link_m = re.search(r'<a\b[^>]*class=["\'][^"\']*anm_det_pop[^"\']*["\'][^>]*>', block, re.IGNORECASE)
        link = link_m.group(0) if link_m else ""
        href = re.sub(r"^/", "", re.sub(r"#.*$", "", attr(link, "href")))
        strong_m = re.search(r"<strong[^>]*>([\s\S]*?)</strong>", block, re.IGNORECASE)
        strong = strip_tags(strong_m.group(1)) if strong_m else ""
        range_m = re.search(r"(\d+)-(\d+)\s*$", strong)
        num_m = range_m or re.search(r"(\d+)\s*$", strong)
        if not num_m or not href:
            continue
        number = int(num_m.group(1))
        title_m = re.search(r'<i\b[^>]*class=["\'][^"\']*anititle[^"\']*["\'][^>]*>([\s\S]*?)</i>', block, re.IGNORECASE)
        title = strip_tags(title_m.group(1)) if title_m else strong
        title = title or strong
        has_sub = bool(re.search(r"\bbtn-subbed\b", block))
        has_dub = bool(re.search(r"\bbtn-dubbed\b", block))
        episodes.append({"number": number, "title": title, "epSlug": href, "hasSub": has_sub, "hasDub": has_dub})

    episodes.sort(key=lambda e: e["number"])
    seen = set()
    out = []
    for e in episodes:
        if e["number"] in seen:
            continue
        seen.add(e["number"])
        out.append(e)
    return out


_VIDEO_SOURCES_RE = re.compile(r"var\s+videoSources\s*=\s*(\[[\s\S]*?\]);")
_UNQUOTED_KEY_RE = re.compile(r'([{,]\s*)([a-zA-Z_][a-zA-Z0-9_]*)\s*:')
_SINGLE_QUOTED_RE = re.compile(r":\s*'([^']*)'")


async def scrape_embed(embed_id: str) -> list[dict]:
    html = await fetch_html(f"{BASE}/embed/{embed_id}", {"Referer": BASE})
    m = _VIDEO_SOURCES_RE.search(html)
    if not m:
        return []
    try:
        as_json = _UNQUOTED_KEY_RE.sub(r'\1"\2":', m.group(1))
        as_json = _SINGLE_QUOTED_RE.sub(r': "\1"', as_json)
        parsed = json.loads(as_json)
    except json.JSONDecodeError:
        return []

    out = []
    for s in parsed:
        backup = None
        if s.get("bk"):
            try:
                padded = s["bk"] + "=" * (-len(s["bk"]) % 4)
                backup_bytes = base64.b64decode(padded)
                backup = backup_bytes.decode("utf-8", errors="replace")
                from urllib.parse import unquote
                backup = unquote(backup)
            except Exception:
                backup = None
        file_ = s.get("file")
        url = (file_ if file_.startswith("http") else f"{BASE}{file_}") if file_ else ""
        if url:
            out.append({"quality": s.get("label", "unknown"), "url": url, "backup": backup})
    return out


async def scrape_episode_watch(ep_slug: str, audio: str) -> dict:
    html = await fetch_html(f"{BASE}/{ep_slug}", {"Referer": BASE})
    title_m = re.search(
        r'<div\b[^>]*class=["\'][^"\']*info[^"\']*["\'][^>]*>[\s\S]*?<a[^>]*>([\s\S]*?)</a>', html, re.IGNORECASE
    )
    title = strip_tags(title_m.group(1)) if title_m else ""

    tabs = []
    for m in re.finditer(r'<a\b[^>]*data-toggle=["\']tab["\'][^>]*>', html, re.IGNORECASE):
        tag = m.group(0)
        embed_id = attr(tag, "data-id")
        server = attr(tag, "data-mirror") or "AnimeGG"
        version = attr(tag, "data-version") or "subbed"
        if not embed_id:
            continue
        normalized = "dub" if version.startswith("dub") else "sub"
        if audio == "all" or normalized == audio:
            tabs.append({"embedId": embed_id, "embedUrl": f"{BASE}/embed/{embed_id}", "server": server, "normalized": normalized})

    import asyncio

    async def _one(i: int, tab: dict) -> list[dict]:
        sources = await scrape_embed(tab["embedId"])
        origin = f"{urlparse(tab['embedUrl']).scheme}://{urlparse(tab['embedUrl']).netloc}/"
        streams = []
        for j, s in enumerate(sources):
            streams.append({
                "url": s["url"],
                "type": "hls" if ".m3u8" in s["url"] else "mp4",
                "quality": s["quality"],
                "backup": s["backup"],
                "audio": tab["normalized"],
                "server": tab["server"],
                "embed": tab["embedUrl"],
                "referer": origin,
                "priority": len(tabs) - i,
                "isActive": i == 0 and j == 0,
            })
        streams.append({
            "url": tab["embedUrl"],
            "type": "embed",
            "audio": tab["normalized"],
            "server": f"{tab['server']}-embed",
            "referer": origin,
            "priority": 1,
            "isActive": False,
        })
        return streams

    results = await asyncio.gather(*(_one(i, tab) for i, tab in enumerate(tabs)), return_exceptions=True)
    streams = []
    for r in results:
        if isinstance(r, list):
            streams.extend(r)

    return {"title": title, "streams": streams}


async def _search_fn(query: str) -> list[dict]:
    r1 = await search(query)
    compact = re.sub(r"[^a-zA-Z0-9]", "", query.split()[0]) if query.split() else ""
    if len(compact) >= 4 and compact.lower() != query.lower():
        try:
            r2 = await search(compact)
            seen = {r["slug"] for r in r1}
            for r in r2:
                if r["slug"] not in seen:
                    r1.append(r)
        except Exception:
            pass
    return r1


async def resolve_series(anilist_id: Any, ctx: Optional[dict] = None) -> dict:
    ctx = ctx or {}
    cache_key = f"np:animegg:{anilist_id}"
    cached = get(cache_key)
    if is_fresh(cached):
        return cached["data"]

    media = ctx.get("media") or await get_media(anilist_id)
    titles = build_titles(media, ctx.get("anizip"))
    candidates = await find_top_slugs(titles, _search_fn)
    expected = expected_count(media, ctx.get("anizip"), ctx.get("jikanEps"))
    try:
        offset = await get_prequel_offset(anilist_id)
    except Exception:
        offset = 0

    is_single_movie = str((media or {}).get("format", "")).upper() == "MOVIE" or expected == 1
    selected = await select_series(
        candidates, scrape_series, expected, (media or {}).get("status"), offset,
        min_score=0.9 if is_single_movie else 0.65,
    )
    if not selected:
        raise RuntimeError(f"AnimeGG match not found for AniList {anilist_id}")

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
            sub.append({**base, "id": f"watch/animegg/{anilist_id}/sub/animegg-{number}", "audio": "sub"})
        if src["hasDub"]:
            dub.append({**base, "id": f"watch/animegg/{anilist_id}/dub/animegg-{number}", "audio": "dub"})
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
            "source": "animegg",
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
    episodes = await scrape_series(series["slug"])
    ep = next((e for e in episodes if e["number"] == provider_ep), None)
    if not ep:
        raise RuntimeError(f"AnimeGG episode {provider_ep} not found")
    watch = await scrape_episode_watch(ep["epSlug"], audio)
    return {
        "anilistId": int(anilist_id),
        "episode": int(ep_num),
        "providerEpisode": provider_ep,
        "audio": audio,
        "title": watch["title"],
        "streams": watch["streams"],
    }
