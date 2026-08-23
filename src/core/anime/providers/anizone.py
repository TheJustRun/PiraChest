from __future__ import annotations

import json
import logging
import re
from typing import Any, Optional
from urllib.parse import quote

import httpx

from ..anime_backend import (
    build_titles, decode_entities, dice_coeff, episode_meta, expected_count,
    fetch_html, get_prequel_offset, norm, select_series,
    get_media, get_entry as get, set_entry as cache_set, is_fresh, SHOW_IDENTITY_TTL,
)

logger = logging.getLogger(__name__)

BASE = "https://anizone.to"

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

_session_cookies: Optional[httpx.Cookies] = None


async def _get_session_cookies() -> httpx.Cookies:
    global _session_cookies
    if _session_cookies is not None:
        return _session_cookies
    async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
        await client.get(
            f"{BASE}/",
            headers={
                "User-Agent": _UA,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9",
            },
        )
        _session_cookies = client.cookies
    return _session_cookies


async def _fetch_with_session(url: str) -> str:
    cookies = await _get_session_cookies()
    async with httpx.AsyncClient(timeout=20, follow_redirects=True, cookies=cookies) as client:
        resp = await client.get(
            url,
            headers={
                "User-Agent": _UA,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9",
                "Referer": f"{BASE}/",
            },
        )
        cookies.update(client.cookies)
    if resp.status_code >= 400:
        raise RuntimeError(f"AniZone HTTP {resp.status_code}: {url}")
    return resp.text


def _score_candidate(query: str, candidate: str, slug: str) -> float:
    base = max(dice_coeff(query, candidate), dice_coeff(query, slug.replace("-", " ")))
    is_movie_query = bool(re.search(r"\b(movie|film|the movie)\b", query, re.IGNORECASE))
    is_movie_match = bool(re.search(r"\b(movie|film)\b", candidate, re.IGNORECASE)) or bool(re.search(r"movie|film", slug))
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
    stripped = re.sub(r"\s+", " ", stripped).strip()
    if stripped and stripped != title:
        queries.add(stripped)

    return [q for q in queries if len(q) >= 3]


async def _find_candidates(titles: list[str], search_fn, n: int = 6) -> list[dict]:
    import asyncio

    all_candidates: dict[str, str] = {}
    search_queries: set[str] = set()
    for title in titles[:4]:
        search_queries.update(_build_search_queries(title))

    async def _run(q: str):
        try:
            results = await search_fn(q)
        except Exception as exc:
            logger.warning("AniZone search failed for query %r: %s: %s", q, type(exc).__name__, exc)
            return
        for r in results:
            if r["slug"] not in all_candidates:
                all_candidates[r["slug"]] = r["text"]

    await asyncio.gather(*(_run(q) for q in search_queries))

    scored = []
    for slug, text in all_candidates.items():
        best = 0.0
        for title in titles[:2]:
            best = max(best, _score_candidate(title, text, slug))
        if best >= 0.5:
            scored.append({"slug": slug, "title": text, "score": best})

    scored.sort(key=lambda x: x["score"], reverse=True)
    return scored[:n]


_PLACEHOLDER = "\x01U\x01"


def _process_json_arg(raw: str) -> dict:
    s = re.sub(r"\\\\u([0-9a-fA-F]{4})", lambda m: f"{_PLACEHOLDER}{m.group(1)}", raw)
    s = re.sub(r"\\u([0-9a-fA-F]{4})", lambda m: chr(int(m.group(1), 16)), s)
    s = s.replace(_PLACEHOLDER, "\\u")
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        return {}


def _pick_title(titles: dict) -> str:
    return titles.get("1") or titles.get("5") or titles.get("8") or (next(iter(titles.values()), "") if titles else "")


def _extract_slug(ctx: str) -> Optional[str]:
    m = re.search(r'href="(?:https://anizone\.to)?/anime/([a-z0-9-]+)"', ctx)
    return m.group(1) if m else None


def _extract_json_arg(xdata: str, key: str) -> Optional[str]:
    pattern = re.compile(re.escape(key) + r":\s*JSON\.parse\('((?:[^'\\]|\\.)*)'\)")
    m = pattern.search(xdata)
    return m.group(1) if m else None


_XDATA_SEARCH_RE = re.compile(r'x-data="(\{[^"]*anmTitles[^"]*\})"')
_CSRF_META_RE = re.compile(r'<meta name="csrf-token" content="([^"]+)"')
_WIRE_SNAPSHOT_ONLY_RE = re.compile(r'wire:snapshot="(\{.*?\})"', re.DOTALL)


def _unescape_wire_attr(s: str) -> str:
    return (
        s.replace("&quot;", '"').replace("&#039;", "'").replace("&amp;", "&")
        .replace("&lt;", "<").replace("&gt;", ">")
    )


def _find_listing_snapshot(html: str) -> Optional[tuple]:
    best = None
    for m in _WIRE_SNAPSHOT_ONLY_RE.finditer(html):
        raw = _unescape_wire_attr(m.group(1))
        try:
            snap = json.loads(raw)
        except json.JSONDecodeError:
            continue
        name = ((snap.get("memo") or {}).get("name")) or ""
        if "navbar" in name.lower():
            continue
        data = snap.get("data") or {}
        if "search" in data or "anime" in name.lower() or "browse" in name.lower() or "index" in name.lower():
            best = (raw, snap)
            if "search" in data:
                break
    return best


async def _livewire_search(query: str) -> str:
    cookies = await _get_session_cookies()
    html = await _fetch_with_session(f"{BASE}/anime")
    csrf_m = _CSRF_META_RE.search(html)
    csrf = csrf_m.group(1) if csrf_m else cookies.get("XSRF-TOKEN", "")

    found = _find_listing_snapshot(html)
    if not found:
        return html
    raw_snapshot, _snap = found

    payload = {
        "_token": csrf,
        "components": [{
            "snapshot": raw_snapshot,
            "updates": {"search": query},
            "calls": [],
        }],
    }

    async with httpx.AsyncClient(timeout=20, follow_redirects=True, cookies=cookies) as client:
        resp = await client.post(
            f"{BASE}/livewire/update",
            headers={
                "User-Agent": _UA,
                "Accept": "text/html, application/xhtml+xml",
                "Content-Type": "application/json",
                "X-Livewire": "true",
                "X-CSRF-TOKEN": csrf,
                "Referer": f"{BASE}/anime",
            },
            json=payload,
        )
        cookies.update(client.cookies)

    if resp.status_code >= 400:
        return html
    try:
        data = resp.json()
    except ValueError:
        return html

    components = data.get("components") or []
    if not components:
        return html
    effects = components[0].get("effects") or {}
    return effects.get("html") or html


async def search(query: str) -> list[dict]:
    html = await _livewire_search(query)
    results = []
    drop_no_slug = 0
    drop_no_raw = 0
    drop_no_title = 0
    for m in _XDATA_SEARCH_RE.finditer(html):
        ctx_start = max(0, m.start() - 300)
        ctx_end = min(len(html), m.start() + len(m.group(0)) + 800)
        ctx = html[ctx_start:ctx_end]
        slug = _extract_slug(ctx)
        if not slug:
            drop_no_slug += 1
            continue
        xdata = decode_entities(m.group(1))
        raw = _extract_json_arg(xdata, "anmTitles")
        if not raw:
            drop_no_raw += 1
            continue
        titles = _process_json_arg(raw)
        title = _pick_title(titles)
        if title:
            results.append({"slug": slug, "text": title})
        else:
            drop_no_title += 1
    if not results:
        blocks = _XDATA_SEARCH_RE.findall(html)
        sample_xdata = decode_entities(blocks[0]) if blocks else ""
        if not blocks:
            body_m = re.search(r"<body[^>]*>([\s\S]{0,600})", html, re.IGNORECASE)
            body_sample = re.sub(r"\s+", " ", body_m.group(1)).strip() if body_m else html[:600]
            logger.warning(
                "AniZone search: 0 results for query %r (html_len=%d, xdata_blocks_found=0). "
                "Body sample: %r",
                query, len(html), body_sample[:600],
            )
        else:
            logger.warning(
                "AniZone search: 0 results for query %r (html_len=%d, xdata_blocks_found=%d, "
                "dropped_no_slug=%d, dropped_no_raw=%d, dropped_no_title=%d, sample_xdata=%r)",
                query, len(html), len(blocks), drop_no_slug, drop_no_raw, drop_no_title, sample_xdata[:400],
            )
    return results


_XDATA_SERIES_RE = re.compile(r'x-data="(\{[^"]*epsTitles[^"]*\})"')
_EP_NUM_RE = re.compile(r'href="(?:https://anizone\.to)?/anime/[a-z0-9-]+/(\d+)"')


async def scrape_series(slug: str) -> list[dict]:
    html = await _fetch_with_session(f"{BASE}/anime/{slug}")
    episodes = []
    for m in _XDATA_SERIES_RE.finditer(html):
        ctx_start = max(0, m.start() - 400)
        ctx_end = min(len(html), m.start() + len(m.group(0)) + 800)
        ctx = html[ctx_start:ctx_end]
        num_m = _EP_NUM_RE.search(ctx)
        if not num_m:
            continue
        num = int(num_m.group(1))
        if num < 1:
            continue
        xdata = decode_entities(m.group(1))
        raw = _extract_json_arg(xdata, "epsTitles")
        title = f"Episode {num}"
        if raw:
            titles = _process_json_arg(raw)
            title = _pick_title(titles) or title
        episodes.append({"number": num, "title": title, "hasSub": True, "hasDub": False})

    seen = set()
    out = []
    for e in episodes:
        if e["number"] in seen:
            continue
        seen.add(e["number"])
        out.append(e)
    out.sort(key=lambda e: e["number"])
    return out


_HLS_SRC_RE = re.compile(r'<media-player[^>]+src="([^"]+\.m3u8[^"]*)"', re.IGNORECASE)
_TRACK_RE = re.compile(r"<track\b([^>]*)>", re.IGNORECASE)
_STORYBOARD_RE = re.compile(r'thumbnails="([^"]+\.vtt[^"]*)"', re.IGNORECASE)
_CHAPTERS_RE = re.compile(r'<track\b[^>]*kind="chapters"[^>]*src=["\']?([^\s"\'>]+)["\']?', re.IGNORECASE)


async def scrape_watch(slug: str, episode_num: Any) -> dict:
    html = await fetch_html(f"{BASE}/anime/{slug}/{episode_num}")

    hls_m = _HLS_SRC_RE.search(html)
    hls = decode_entities(hls_m.group(1)) if hls_m else None

    subtitles = []
    for t in _TRACK_RE.finditer(html):
        attrs = t.group(1)
        kind_m = re.search(r'kind="([^"]*)"', attrs, re.IGNORECASE)
        kind = kind_m.group(1) if kind_m else ""
        if kind != "subtitles":
            continue
        src_m = re.search(r'src=["\']?([^\s"\'>]+)["\']?', attrs, re.IGNORECASE)
        src = src_m.group(1) if src_m else ""
        label_m = re.search(r'label="([^"]*)"', attrs, re.IGNORECASE)
        label = label_m.group(1) if label_m else ""
        srclang_m = re.search(r'srclang="([^"]*)"', attrs, re.IGNORECASE)
        srclang = srclang_m.group(1) if srclang_m else ""
        data_type_m = re.search(r'data-type="([^"]*)"', attrs, re.IGNORECASE)
        data_type = data_type_m.group(1) if data_type_m else "vtt"
        is_default = bool(re.search(r"\bdefault\b", attrs))
        if src:
            subtitles.append({
                "url": decode_entities(src), "label": label, "srclang": srclang,
                "format": data_type, "default": is_default,
            })

    storyboard_m = _STORYBOARD_RE.search(html)
    storyboard = decode_entities(storyboard_m.group(1)) if storyboard_m else None

    chapters_m = _CHAPTERS_RE.search(html)
    chapters = decode_entities(chapters_m.group(1)) if chapters_m else None

    return {"hls": hls, "subtitles": subtitles, "storyboard": storyboard, "chapters": chapters}


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
    cache_key = f"np:anizone:{anilist_id}"
    cached = get(cache_key)
    if is_fresh(cached):
        return cached["data"]

    media = ctx.get("media") or await get_media(anilist_id)
    titles = build_titles(media, ctx.get("anizip"))
    candidates = await _find_candidates(titles, _search_fn)

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
    expected = expected_count(media, ctx.get("anizip"), ctx.get("jikanEps"))
    return {
        "meta": {
            "id": series["slug"],
            "title": series["title"],
            "source": "anizone",
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
