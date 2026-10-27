import asyncio
import logging
import re
from typing import Any, Optional
from urllib.parse import quote

from ..anime_backend import (
    attr, build_titles, decode_entities, episode_meta, expected_count, fetch_html, fetch_json,
    strip_tags, get_entry as get, set_entry as cache_set, is_fresh, get_media, SHOW_IDENTITY_TTL,
)

logger = logging.getLogger(__name__)

BASE = "https://anidb.app"

NAV_HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

XHR_HEADERS = {
    "Accept": "application/json, text/html, */*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "X-Requested-With": "XMLHttpRequest",
}


def _with_referer(headers: dict, referer: Optional[str]) -> dict:
    return {**headers, "Referer": referer} if referer else headers


async def _fetch_anidb_html(url: str, referer: Optional[str] = None) -> str:
    return await fetch_html(url, _with_referer(NAV_HEADERS, referer))


async def _fetch_xhr(url: str, referer: Optional[str] = None) -> str:
    return await fetch_html(url, _with_referer(XHR_HEADERS, referer))


async def _fetch_json(url: str, referer: Optional[str] = None) -> Any:
    return await fetch_json(url, _with_referer(XHR_HEADERS, referer))


async def _search(query: str) -> list[dict]:
    try:
        html = await _fetch_xhr(f"{BASE}/search/suggestions?q={quote(query)}", f"{BASE}/home")
    except Exception:
        html = ""

    results = []
    for m in re.finditer(r"<a\b[^>]*data-search-item\b[^>]*>[\s\S]*?</a>", html, re.IGNORECASE):
        tag_m = re.search(r"<a\b[^>]*>", m.group(0), re.IGNORECASE)
        tag = tag_m.group(0) if tag_m else ""
        href = attr(tag, "href")
        path = _pathname(href) if href.startswith("http") else href
        slug_m = re.match(r"^/anime/([^/?#]+)", path)
        if not slug_m:
            continue
        slug = slug_m.group(1)
        title_m = re.search(r'<p\b[^>]*class=["\'][^"\']*text-sm[^"\']*["\'][^>]*>([\s\S]*?)</p>', m.group(0), re.IGNORECASE)
        title = strip_tags(title_m.group(1)) if title_m else ""
        meta_m = re.search(r'<p\b[^>]*class=["\'][^"\']*text-xs[^"\']*["\'][^>]*>([\s\S]*?)</p>', m.group(0), re.IGNORECASE)
        meta = strip_tags(meta_m.group(1)) if meta_m else ""
        site_id_m = re.search(r"-(\d+)$", slug)
        site_id = int(site_id_m.group(1)) if site_id_m else None
        results.append({"slug": slug, "title": title or slug.replace("-", " "), "meta": meta, "siteId": site_id})

    if results:
        return results

    try:
        browse_html = await _fetch_anidb_html(f"{BASE}/browse?q={quote(query)}", f"{BASE}/home")
    except Exception:
        browse_html = ""

    seen: set[str] = set()
    pattern = re.compile(
        r'<a\b[^>]*href=["\'](?:https://anidb\.app)?/anime/([^"\']+)["\'][^>]*class=["\'][^"\']*\banime-card\b[^"\']*["\'][^>]*>[\s\S]*?</a>',
        re.IGNORECASE,
    )
    for m in pattern.finditer(browse_html):
        slug = m.group(1)
        if slug in seen:
            continue
        seen.add(slug)
        title_m = re.search(r'title=["\']([^"\']+)["\']', m.group(0), re.IGNORECASE)
        alt_m = re.search(r'alt=["\']([^"\']+)["\']', m.group(0), re.IGNORECASE)
        title = (strip_tags(title_m.group(1)) if title_m else "") or (strip_tags(alt_m.group(1)) if alt_m else "") or slug.replace("-", " ")
        site_id_m = re.search(r"-(\d+)$", slug)
        site_id = int(site_id_m.group(1)) if site_id_m else None
        results.append({"slug": slug, "title": title, "meta": "", "siteId": site_id})

    return results


def _pathname(url: str) -> str:
    from urllib.parse import urlparse
    return urlparse(url).path


def _parse_external_ids(html: str) -> dict:
    al_m = re.search(r"https://anilist\.co/anime/(\d+)", html, re.IGNORECASE)
    mal_m = re.search(r"https://myanimelist\.net/anime/(\d+)", html, re.IGNORECASE)
    anidb_m = re.search(r"https://anidb\.net/anime/(\d+)", html, re.IGNORECASE)
    kitsu_m = re.search(r"https://kitsu\.app/anime/(\d+)", html, re.IGNORECASE)
    return {
        "anilistId": int(al_m.group(1)) if al_m else None,
        "malId": int(mal_m.group(1)) if mal_m else None,
        "anidbId": int(anidb_m.group(1)) if anidb_m else None,
        "kitsuId": int(kitsu_m.group(1)) if kitsu_m else None,
    }


def _parse_page_title(html: str) -> str:
    m = re.search(r"<h1\b[^>]*>([\s\S]*?)</h1>", html, re.IGNORECASE)
    return strip_tags(m.group(1)) if m else ""


def _search_queries(media: Optional[dict], anizip: Optional[dict]) -> list[str]:
    titles = build_titles(media, anizip)
    out: set[str] = set()
    for title in titles[:5]:
        out.add(title)
        words = title.strip().split()
        if len(words) > 4:
            out.add(" ".join(words[:4]))
    return [q for q in out if len(q) >= 2]


def _series_data(candidate: dict, html: str, ids: dict, match_type: str, score: float) -> dict:
    site_id_m = re.search(r"-(\d+)$", candidate["slug"])
    return {
        "slug": candidate["slug"],
        "siteId": candidate.get("siteId") or (int(site_id_m.group(1)) if site_id_m else None),
        "title": _parse_page_title(html) or candidate["title"],
        "matchType": match_type,
        "matchScore": score,
        **ids,
    }


async def resolve_series(anilist_id: Any, ctx: Optional[dict] = None) -> dict:
    ctx = ctx or {}
    cache_key = f"np:anidbapp:{anilist_id}"
    cached = get(cache_key)
    if is_fresh(cached):
        return cached["data"]

    media = ctx.get("media") or await get_media(anilist_id)
    queries = _search_queries(media, ctx.get("anizip"))
    candidates: dict[str, dict] = {}

    async def _run(q: str) -> None:
        try:
            results = await _search(q)
        except Exception as exc:
            logger.warning("AniDB.app search failed for query %r: %s", q, exc)
            return
        for r in results:
            candidates.setdefault(r["slug"], r)

    await asyncio.gather(*(_run(q) for q in queries))
    if not candidates:
        raise RuntimeError(f"AniDB.app match not found for AniList {anilist_id}")

    sem = asyncio.Semaphore(6)

    async def _check(candidate: dict) -> Optional[dict]:
        async with sem:
            try:
                html = await _fetch_anidb_html(f"{BASE}/anime/{candidate['slug']}", f"{BASE}/home")
            except Exception:
                return None
        return {"candidate": candidate, "html": html, "ids": _parse_external_ids(html)}

    checks = await asyncio.gather(*(_check(c) for c in candidates.values()))

    mal_id = (media or {}).get("idMal")
    mal_fallback: Optional[dict] = None
    for check in checks:
        if not check:
            continue
        ids = check["ids"]
        if ids["anilistId"] == int(anilist_id):
            data = _series_data(check["candidate"], check["html"], ids, "anilist", 1)
            cache_set(cache_key, data, SHOW_IDENTITY_TTL)
            return data
        if (
            mal_fallback is None
            and mal_id
            and not ids["anilistId"]
            and ids["malId"] == int(mal_id)
        ):
            mal_fallback = _series_data(check["candidate"], check["html"], ids, "mal", 0.9)

    if mal_fallback:
        cache_set(cache_key, mal_fallback, SHOW_IDENTITY_TTL)
        return mal_fallback

    raise RuntimeError(f"AniDB.app match not found for AniList {anilist_id}")


async def _fetch_provider_episodes(site_id: Any) -> list[dict]:
    data = await _fetch_json(f"{BASE}/api/frontend/anime/{site_id}/episodes", f"{BASE}/anime/{site_id}")
    return data.get("episodes") if isinstance(data.get("episodes"), list) else []


def _infer_offset(provider_episodes: list[dict], expected: Optional[int]) -> int:
    nums = [int(e["number"]) for e in provider_episodes if _is_positive_num(e.get("number"))]
    if not nums or not expected:
        return 0
    lo, hi = min(nums), max(nums)
    if lo > expected:
        return lo - 1
    if lo > 1 and (hi - lo + 1) >= expected:
        return lo - 1
    return 0


def _is_positive_num(v: Any) -> bool:
    try:
        return float(v) > 0
    except (TypeError, ValueError):
        return False


async def _fetch_languages(episode_id: Any, series_slug: str) -> list[dict]:
    try:
        data = await _fetch_json(f"{BASE}/api/frontend/episode/{episode_id}/languages", f"{BASE}/anime/{series_slug}")
    except Exception:
        return []
    return data.get("languages") if isinstance(data.get("languages"), list) else []


def _language_for_audio(languages: list[dict], audio: str) -> Optional[dict]:
    preferred = ["jpn", "ja", "japanese"] if audio == "sub" else ["eng", "en", "english"]
    for l in languages:
        if str(l.get("code", "")).lower() in preferred:
            return l
    for l in languages:
        if str(l.get("name", "")).lower() in preferred:
            return l
    return None


def _has_language(languages: list[dict], audio: str) -> bool:
    lang = _language_for_audio(languages, audio)
    return bool(lang and lang.get("embed_url"))


def _build_episode_lists(anilist_id: Any, provider_episodes: list[dict], ctx: dict, offset: int, availability: dict) -> dict:
    sub, dub = [], []
    for src in provider_episodes:
        try:
            source_number = int(src.get("number"))
        except (TypeError, ValueError):
            continue
        number = source_number - offset
        if number < 1:
            continue
        meta = episode_meta(number, ctx)
        base = {
            "number": number,
            "title": meta["title"] or f"Episode {number}",
            "duration": meta["duration"],
            "filler": src.get("filler") if src.get("filler") is not None else meta["filler"],
            "uncensored": meta["uncensored"],
            "description": meta["description"],
            "image": meta["image"],
            "airDate": meta["airDate"],
            "sourceNumber": source_number,
            "sourceId": src.get("id"),
        }
        if availability["hasSub"]:
            sub.append({**base, "id": f"watch/anidbapp/{anilist_id}/sub/anidbapp-{number}", "audio": "sub"})
        if availability["hasDub"]:
            dub.append({**base, "id": f"watch/anidbapp/{anilist_id}/dub/anidbapp-{number}", "audio": "dub"})
    return {"sub": sub, "dub": dub}


_HLS_PATTERNS = [
    re.compile(r'file\s*:\s*["\'](https?://[^"\']+\.m3u8[^"\']*)["\']', re.IGNORECASE),
    re.compile(r'sources\s*:\s*\[\s*\{[^}]*file\s*:\s*["\'](https?://[^"\']+\.m3u8[^"\']*)["\']', re.IGNORECASE),
    re.compile(r'["\'](https?://[^"\']+/master\.m3u8[^"\']*)["\']', re.IGNORECASE),
    re.compile(r'["\'](https?://[^"\']+\.m3u8[^"\']*)["\']', re.IGNORECASE),
]


def _extract_hls(html: str) -> Optional[str]:
    for pattern in _HLS_PATTERNS:
        m = pattern.search(html)
        if m:
            return decode_entities(m.group(1))
    return None


async def _streams_for_embed(embed_url: str, audio: str, language: dict) -> list[dict]:
    from urllib.parse import urlparse
    try:
        html = await _fetch_anidb_html(embed_url, f"{BASE}/")
    except Exception:
        html = ""
    hls = _extract_hls(html) if html else None
    streams = []
    origin = f"{urlparse(embed_url).scheme}://{urlparse(embed_url).netloc}/"
    if hls:
        streams.append({
            "url": hls, "type": "hls", "audio": audio, "language": language.get("code"),
            "server": "AniDB.app", "embed": embed_url, "referer": origin, "priority": 5, "isActive": True,
        })
    streams.append({
        "url": embed_url, "type": "embed", "audio": audio, "language": language.get("code"),
        "server": "AniDB.app-embed", "referer": f"{BASE}/", "priority": 4, "isActive": not hls,
    })
    return streams


async def get_episodes(anilist_id: Any, ctx: Optional[dict] = None) -> dict:
    ctx = ctx or {}
    media = ctx.get("media") or await get_media(anilist_id)
    local_ctx = {**ctx, "media": media}
    series = await resolve_series(anilist_id, local_ctx)
    episodes = await _fetch_provider_episodes(series["siteId"])
    expected = expected_count(media, ctx.get("anizip"), ctx.get("jikanEps"))
    offset = _infer_offset(episodes, expected)
    sample_languages = await _fetch_languages(episodes[0]["id"], series["slug"]) if episodes and episodes[0].get("id") else []
    availability = {
        "hasSub": _has_language(sample_languages, "sub") or not sample_languages,
        "hasDub": _has_language(sample_languages, "dub"),
    }
    return {
        "meta": {
            "id": series["slug"],
            "siteId": series["siteId"],
            "title": series["title"],
            "source": "anidbapp",
            "matchScore": series["matchScore"],
            "matchType": series["matchType"],
            "anilistId": series.get("anilistId"),
            "malId": series.get("malId"),
            "numbering": "offset" if offset else "local",
            "episodeOffset": offset,
        },
        "episodes": _build_episode_lists(anilist_id, episodes, local_ctx, offset, availability),
    }


async def get_watch(anilist_id: Any, audio: str, ep_num: Any, ctx: Optional[dict] = None) -> dict:
    ctx = ctx or {}
    series = await resolve_series(anilist_id, ctx)
    episodes = await _fetch_provider_episodes(series["siteId"])
    try:
        media = ctx.get("media") or await get_media(anilist_id)
    except Exception:
        media = None
    expected = expected_count(media, ctx.get("anizip"), ctx.get("jikanEps"))
    offset = _infer_offset(episodes, expected)
    provider_ep = int(ep_num) + offset
    episode = next((e for e in episodes if int(e.get("number", -1)) == provider_ep), None)
    if not episode:
        raise RuntimeError(f"AniDB.app episode {ep_num} not found")

    languages = await _fetch_languages(episode["id"], series["slug"])
    language = _language_for_audio(languages, audio)
    if not language or not language.get("embed_url"):
        return {"anilistId": int(anilist_id), "episode": int(ep_num), "providerEpisode": provider_ep, "audio": audio, "streams": []}

    embed_url = decode_entities(language["embed_url"])
    streams = await _streams_for_embed(embed_url, audio, language)
    return {
        "anilistId": int(anilist_id), "episode": int(ep_num), "providerEpisode": provider_ep,
        "audio": audio, "language": language.get("code"), "streams": streams,
    }
