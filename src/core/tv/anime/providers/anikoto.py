import asyncio
import base64
import re
from typing import Any, Optional
from urllib.parse import quote, urlparse

from ..anime_backend import (
    get_media, fetch_html, fetch_json, get_entry as get, set_entry as cache_set,
    is_fresh, SHOW_IDENTITY_TTL,
)
from . import megaplay

ANIKOTO = "https://anikototv.to"
MAPPER = "https://mapper.nekostream.site/api/mal"
SPOOF_REF = "https://hianimes.re/"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"

LANG_MAP = {
    "en": "en", "english": "en", "ja": "ja", "japanese": "ja",
    "fr": "fr", "french": "fr", "de": "de", "german": "de",
    "es": "es", "spanish": "es", "pt": "pt", "portuguese": "pt",
}


def _normalize(s: Optional[str]) -> str:
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


MODIFIERS = ["ova", "movie", "special", "specials", "tales", "journal", "part", "season", "kanwa", "spin-off", "theatre"]


def _score_candidate(cand: dict, primary_en: Optional[str], primary_rom: Optional[str], synonyms: list[str]) -> float:
    score = 0.0
    cand_name_norm = _normalize(cand.get("name"))
    cand_jp_norm = _normalize(cand.get("jp"))
    cand_slug_norm = _normalize(cand.get("slug"))

    norm_en = _normalize(primary_en)
    norm_rom = _normalize(primary_rom)

    if norm_en and cand_name_norm == norm_en:
        score += 1000
    if norm_rom and cand_name_norm == norm_rom:
        score += 900
    if norm_rom and cand_jp_norm == norm_rom:
        score += 800

    target_text = f"{primary_en or ''} {primary_rom or ''} {' '.join(synonyms or [])}".lower()

    for mod in MODIFIERS:
        cand_has_mod = mod in cand_name_norm or mod in cand_slug_norm
        target_has_mod = mod in target_text
        if cand_has_mod and not target_has_mod:
            score -= 300

    for t in [primary_en, primary_rom, *(synonyms or [])]:
        norm_t = _normalize(t)
        if not norm_t or len(norm_t) < 3:
            continue
        if cand_name_norm == norm_t:
            score += 200
        elif cand_name_norm.startswith(norm_t) or norm_t.startswith(cand_name_norm):
            score += 80
        elif norm_t in cand_name_norm or cand_name_norm in norm_t:
            score += 40
        if cand_jp_norm and cand_jp_norm == norm_t:
            score += 100

    length_diff = abs(len(cand_name_norm) - len(norm_en or norm_rom or ""))
    score -= length_diff * 2

    return score


_SEARCH_RE = re.compile(
    r'<a\s+class="name d-title"\s+href="https://anikototv\.to/watch/([^"/]+)(?:/ep-\d+)?"[^>]*data-jp="([^"]*)"[^>]*>([\s\S]*?)</a>'
)
_SEARCH_FALLBACK_RE = re.compile(
    r'<a\s+href="https://anikototv\.to/watch/([^"/]+)(?:/ep-\d+)?"[^>]*>([\s\S]*?)</a>'
)
_TAG_RE = re.compile(r"<[^>]*>")
_SHOW_ID_RE = re.compile(r'data-id="(\d+)"')
_DATA_ID_RE = re.compile(r'data-id="([^"]*)"')
_EPISODE_ANCHOR_RE = re.compile(r'<a\s+[^>]*data-id="([^"]*)"[^>]*>([\s\S]*?)</a>')
_EPISODE_ANCHOR_ATTR_ONLY_RE = re.compile(r'<a\s+[^>]*data-id="([^"]*)"[^>]*>')
_TITLE_SPAN_RE = re.compile(r'<span class="d-title"[^>]*>([\s\S]*?)</span>')
_TYPE_BLOCK_RE = re.compile(r'<div class="type" data-type="([^"]+)">([\s\S]*?)</ul>\s*</div>')
_LI_LINK_RE = re.compile(r'<li\s+([^>]*data-link-id[^>]*)>([\s\S]*?)</li>')
_LINK_ID_RE = re.compile(r'data-link-id="([^"]+)"')

_XHR_HEADERS = {"X-Requested-With": "XMLHttpRequest"}


async def _search_anikoto(query: str) -> list[dict]:
    search_html = await fetch_html(f"{ANIKOTO}/filter?keyword={quote(query)}", {"Referer": f"{ANIKOTO}/"})

    candidates = []
    for m in _SEARCH_RE.finditer(search_html):
        slug, jp, name_html = m.group(1), m.group(2).strip(), m.group(3)
        name = _TAG_RE.sub("", name_html).strip()
        candidates.append({"slug": slug, "name": name, "jp": jp})

    if not candidates:
        for m in _SEARCH_FALLBACK_RE.finditer(search_html):
            candidates.append({"slug": m.group(1), "name": m.group(1), "jp": ""})

    seen = set()
    out = []
    for c in candidates:
        if c["slug"] in seen:
            continue
        seen.add(c["slug"])
        out.append(c)
    return out


async def _find_anikoto_show(media: dict) -> dict:
    title = media.get("title") or {}
    primary_en = title.get("english")
    primary_rom = title.get("romaji")
    synonyms = media.get("synonyms") or []

    keywords = list(dict.fromkeys(k for k in [primary_en, primary_rom, *synonyms] if k))[:5]
    results = await asyncio.gather(*(_search_anikoto(k) for k in keywords), return_exceptions=True)

    all_candidates: dict[str, dict] = {}
    for res in results:
        for c in (res if isinstance(res, list) else []):
            all_candidates.setdefault(c["slug"], c)

    candidates = list(all_candidates.values())
    if not candidates:
        raise RuntimeError(f"No results found on Anikoto for: {primary_en or primary_rom}")

    chosen = max(candidates, key=lambda c: _score_candidate(c, primary_en, primary_rom, synonyms))

    watch_html = await fetch_html(f"{ANIKOTO}/watch/{chosen['slug']}", {"Referer": f"{ANIKOTO}/"})
    show_id_m = _SHOW_ID_RE.search(watch_html)
    if not show_id_m:
        raise RuntimeError(f"Could not find show ID for slug: {chosen['slug']}")

    return {"slug": chosen["slug"], "showId": show_id_m.group(1), "title": chosen["name"]}


async def _resolve_show(anilist_id: Any, media: dict) -> dict:
    cache_key = f"np:anikoto:{anilist_id}"
    cached = get(cache_key)
    if is_fresh(cached):
        return cached["data"]

    show = await _find_anikoto_show(media)
    cache_set(cache_key, show, SHOW_IDENTITY_TTL)
    return show


def _map_track(t: dict, source: str) -> dict:
    label = t.get("label", "")
    lang_key = label.lower().split(" ")[0] if label else ""
    return {
        "url": t.get("file"),
        "label": label or "English",
        "srclang": LANG_MAP.get(lang_key, "en"),
        "default": t.get("default", False),
        "source": source,
    }


async def _extract_embed_source(embed_url: str) -> Optional[dict]:
    try:
        page_html = await fetch_html(embed_url, {"Referer": SPOOF_REF, "Accept-Language": "en-US,en;q=0.9"})
        m = _DATA_ID_RE.search(page_html)
        if not m or not m.group(1):
            return None
        file_id = m.group(1)
        parts = urlparse(embed_url)
        origin = f"{parts.scheme}://{parts.netloc}"
        data = await fetch_json(
            f"{origin}/stream/getSources?id={file_id}&id={file_id}",
            {"Referer": f"{origin}/", "X-Requested-With": "XMLHttpRequest"},
        )
        if isinstance(data, dict) and not ((data.get("sources") or {}).get("file")):
            decrypted = megaplay.decrypt_payload(data)
            if decrypted and decrypted.get("file"):
                data = {**data, "sources": {"file": decrypted["file"]}}
        return {"fileId": file_id, "data": data, "origin": origin}
    except Exception:
        return None


def _get_data_attr(tag: str, name: str) -> str:
    m = re.search(rf'data-{name}="([^"]*)"', tag)
    return m.group(1) if m else ""


async def _fetch_episode_list(show: dict) -> str:
    list_json = await fetch_json(
        f"{ANIKOTO}/ajax/episode/list/{show['showId']}",
        {**_XHR_HEADERS, "Referer": f"{ANIKOTO}/watch/{show['slug']}"},
    )
    return list_json.get("result") or ""


async def get_episodes(anilist_id: Any, ctx: Optional[dict] = None) -> dict:
    ctx = ctx or {}
    media = ctx.get("media") or await get_media(anilist_id)
    if not media:
        raise RuntimeError(f"Could not resolve media for AniList ID: {anilist_id}")

    show = await _resolve_show(anilist_id, media)
    html = await _fetch_episode_list(show)

    anizip_res = ctx.get("anizip")
    sub: list[dict] = []
    dub: list[dict] = []
    first_mal = media.get("idMal")

    for m in _EPISODE_ANCHOR_RE.finditer(html):
        tag, inner = m.group(0), m.group(2)
        num_str = _get_data_attr(tag, "num")
        if not num_str:
            continue
        num = int(num_str)
        has_sub = _get_data_attr(tag, "sub") == "1"
        has_dub = _get_data_attr(tag, "dub") == "1"
        mal_attr = _get_data_attr(tag, "mal")
        if not first_mal and mal_attr:
            first_mal = int(mal_attr)

        title_m = _TITLE_SPAN_RE.search(inner)
        parsed_title = _TAG_RE.sub("", title_m.group(1)).strip() if title_m else ""
        ep_title = parsed_title or f"Episode {num}"

        az_ep = ((anizip_res or {}).get("episodes") or {}).get(str(num), {})
        img = az_ep.get("image")
        desc = az_ep.get("overview") or az_ep.get("summary")
        air_date = az_ep.get("airDate") or az_ep.get("airdate")

        base = {
            "number": num, "title": ep_title, "duration": None, "filler": False,
            "uncensored": False, "description": desc, "image": img, "airDate": air_date,
        }

        if has_sub:
            sub.append({"id": f"watch/anikoto/{anilist_id}/sub/anikoto-{num}", **base, "audio": "sub"})
        if has_dub:
            dub.append({"id": f"watch/anikoto/{anilist_id}/dub/anikoto-{num}", **base, "audio": "dub"})

    sub.sort(key=lambda e: e["number"])
    dub.sort(key=lambda e: e["number"])

    return {
        "meta": {"title": show["title"], "slug": show["slug"], "malId": first_mal, "source": "anikoto"},
        "episodes": {"sub": sub, "dub": dub},
    }


def _parse_skip_range(skip_data: dict, key: str) -> Optional[dict]:
    value = skip_data.get(key)
    if not isinstance(value, list) or len(value) != 2:
        return None
    s, e = value
    if not s and not e:
        return None
    return {"start": float(s) if s else 0, "end": float(e) if e else 0}


async def get_watch(anilist_id: Any, audio: str, ep_num: Any, ctx: Optional[dict] = None) -> dict:
    ctx = ctx or {}
    ep_num = int(ep_num)
    if audio not in ("sub", "dub"):
        raise RuntimeError("audio must be sub or dub")

    media = ctx.get("media") or await get_media(anilist_id)
    if not media:
        raise RuntimeError(f"Could not resolve media for AniList ID: {anilist_id}")

    show = await _resolve_show(anilist_id, media)
    html = await _fetch_episode_list(show)

    target_ep = None
    for m in _EPISODE_ANCHOR_ATTR_ONLY_RE.finditer(html):
        tag = m.group(0)
        if int(_get_data_attr(tag, "num") or -1) == ep_num:
            target_ep = {
                "ids": _get_data_attr(tag, "ids"),
                "mal": _get_data_attr(tag, "mal"),
                "slug": _get_data_attr(tag, "slug"),
                "timestamp": _get_data_attr(tag, "timestamp"),
            }
            break

    if not target_ep or not target_ep["ids"]:
        raise RuntimeError(f"Episode {ep_num} not found for show: {show['title']}")

    mal_id_num = media.get("idMal") or (int(target_ep["mal"]) if target_ep["mal"] else None)

    async def _safe(coro) -> Any:
        try:
            return await coro
        except Exception:
            return None

    async def _fetch_mapper() -> Any:
        if target_ep["mal"] and target_ep["slug"] and target_ep["timestamp"]:
            return await _safe(fetch_json(
                f"{MAPPER}/{target_ep['mal']}/{target_ep['slug']}/{target_ep['timestamp']}",
                {"Referer": f"{ANIKOTO}/"},
            ))
        return None

    server_data, mapper_data = await asyncio.gather(
        _safe(fetch_json(
            f"{ANIKOTO}/ajax/server/list?servers={target_ep['ids']}",
            {**_XHR_HEADERS, "Referer": f"{ANIKOTO}/"},
        )),
        _fetch_mapper(),
    )

    server_html = (server_data or {}).get("result") or ""
    server_items = []
    download_items = []

    for type_m in _TYPE_BLOCK_RE.finditer(server_html):
        type_name = type_m.group(1)
        for li in _LI_LINK_RE.finditer(type_m.group(2)):
            link_id_m = _LINK_ID_RE.search(li.group(1))
            link_id = link_id_m.group(1) if link_id_m else None
            name = _TAG_RE.sub("", li.group(2)).strip()
            if not link_id:
                continue
            if type_name == "dl" or "download" in name.lower() or "kiwi" in name.lower():
                download_items.append({"linkId": link_id, "name": name})
            elif type_name == audio:
                server_items.append({"linkId": link_id, "name": name})

    if mapper_data:
        for s_key, s_obj in mapper_data.items():
            if s_key == "status":
                continue
            clean_name = re.sub(r"[-_]+$", "", s_key).strip()
            audio_obj = (s_obj or {}).get(audio) or {}
            if audio_obj.get("url"):
                server_items.append({"linkId": audio_obj["url"], "name": clean_name})
            if audio_obj.get("download"):
                for _d_label, d_url in audio_obj["download"].items():
                    if isinstance(d_url, str) and d_url:
                        download_items.append({"url": d_url, "name": clean_name})

    server_seen: set[str] = set()
    unique_items = []
    for item in server_items:
        if item["name"] in server_seen:
            continue
        server_seen.add(item["name"])
        unique_items.append(item)

    async def _resolve_item(item: dict) -> Any:
        if item["linkId"].startswith("http"):
            return {"result": {"url": item["linkId"]}}
        return await _safe(fetch_json(
            f"{ANIKOTO}/ajax/server?get={item['linkId']}",
            {**_XHR_HEADERS, "Referer": f"{ANIKOTO}/"},
        ))

    async def _resolve_download(dl: dict) -> Any:
        if dl.get("url") or not dl.get("linkId"):
            return None
        return await _safe(fetch_json(
            f"{ANIKOTO}/ajax/server?get={dl['linkId']}",
            {**_XHR_HEADERS, "Referer": f"{ANIKOTO}/"},
        ))

    resolved_items, resolved_downloads = await asyncio.gather(
        asyncio.gather(*(_resolve_item(i) for i in unique_items)),
        asyncio.gather(*(_resolve_download(d) for d in download_items)),
    )

    embed_urls = [((r or {}).get("result") or {}).get("url") for r in resolved_items]

    async def _extract(item: dict, embed_url: str) -> tuple[Optional[str], Optional[dict]]:
        hls_url = None
        if "#aHR0c" in embed_url:
            b64 = embed_url.split("#")[1]
            try:
                decoded_url = base64.b64decode(b64 + "=" * (-len(b64) % 4)).decode("utf-8", errors="replace")
                if ".m3u8" in decoded_url:
                    hls_url = decoded_url
            except Exception:
                pass
        return hls_url, await _extract_embed_source(embed_url)

    extracted = await asyncio.gather(
        *(_extract(item, url) for item, url in zip(unique_items, embed_urls, strict=True) if url)
    )

    streams = []
    subtitles = []
    downloads = []
    sub_seen: set = set()
    dl_seen: set = set()
    extracted_iter = iter(extracted)

    for item, embed_url, resolved in zip(unique_items, embed_urls, resolved_items, strict=True):
        if not embed_url:
            continue
        hls_url, source = next(extracted_iter)

        skip_data = ((resolved or {}).get("result") or {}).get("skip_data") or {}
        server_intro = _parse_skip_range(skip_data, "intro") or {"start": 0, "end": 0}
        server_outro = _parse_skip_range(skip_data, "outro") or {"start": 0, "end": 0}

        item_subs = []
        extracted_data = (source or {}).get("data") or {}
        if (extracted_data.get("sources") or {}).get("file"):
            hls_url = extracted_data["sources"]["file"]
            for t in extracted_data.get("tracks") or []:
                mapped = _map_track(t, item["name"])
                item_subs.append(mapped)
                if mapped["url"] not in sub_seen:
                    sub_seen.add(mapped["url"])
                    subtitles.append(mapped)

            intro = _parse_skip_range(extracted_data, "intro")
            if intro:
                server_intro = intro
            outro = _parse_skip_range(extracted_data, "outro")
            if outro:
                server_outro = outro

        origin = f"{(source or {}).get('origin')}/" if source and source.get("origin") else None
        if not origin:
            parts = urlparse(embed_url)
            origin = f"{parts.scheme}://{parts.netloc}/"

        if hls_url:
            stream_obj = {
                "url": hls_url, "type": "hls", "server": item["name"], "embedUrl": embed_url,
                "referer": origin, "subtitles": item_subs, "priority": 5, "isActive": len(streams) == 0,
            }
        else:
            stream_obj = {
                "url": embed_url, "type": "embed", "server": item["name"],
                "referer": origin, "priority": 4, "isActive": len(streams) == 0,
            }
        if server_intro["start"] or server_intro["end"]:
            stream_obj["intro"] = server_intro
        if server_outro["start"] or server_outro["end"]:
            stream_obj["outro"] = server_outro
        streams.append(stream_obj)

    for dl, resolved in zip(download_items, resolved_downloads, strict=True):
        dl_url = dl.get("url") or ((resolved or {}).get("result") or {}).get("url")
        if dl_url and dl_url not in dl_seen:
            dl_seen.add(dl_url)
            downloads.append({"url": dl_url, "label": dl["name"]})

    return {
        "anilistId": int(anilist_id),
        "malId": mal_id_num,
        "episode": ep_num,
        "audio": audio,
        "streams": streams,
        "subtitles": subtitles,
        "downloads": downloads,
        "headers": {"User-Agent": UA, "Referer": (streams[0]["referer"] if streams else f"{ANIKOTO}/")},
    }
