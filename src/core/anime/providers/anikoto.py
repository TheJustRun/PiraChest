from __future__ import annotations

import base64
import re
from typing import Any, Optional
from urllib.parse import quote

import httpx

from ..anime_backend import get_media

ANIKOTO = "https://anikototv.to"
MAPPER = "https://mapper.nekostream.site/api/mal"
ANIZIP = "https://api.ani.zip/mappings"
SPOOF_REF = "https://hianimes.re/"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"

LANG_MAP = {
    "en": "en", "english": "en", "ja": "ja", "japanese": "ja",
    "fr": "fr", "french": "fr", "de": "de", "german": "de",
    "es": "es", "spanish": "es", "pt": "pt", "portuguese": "pt",
}


def _normalize(s: Optional[str]) -> str:
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


async def _http_get(url: str, headers: Optional[dict] = None) -> str:
    merged = {"User-Agent": UA, "Accept": "text/html,*/*", **(headers or {})}
    async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
        resp = await client.get(url, headers=merged)
    if resp.status_code >= 400:
        raise RuntimeError(f"HTTP {resp.status_code} fetching {url}")
    return resp.text


async def _get_json(url: str, headers: Optional[dict] = None) -> Any:
    merged = {"User-Agent": UA, "Accept": "application/json,*/*", **(headers or {})}
    async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
        resp = await client.get(url, headers=merged)
    if resp.status_code >= 400:
        raise RuntimeError(f"HTTP {resp.status_code} fetching {url}")
    return resp.json()


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


async def _search_anikoto(query: str) -> list[dict]:
    search_html = await _http_get(f"{ANIKOTO}/filter?keyword={quote(query)}", {"Referer": f"{ANIKOTO}/"})
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


_SHOW_ID_RE = re.compile(r'data-id="(\d+)"')


async def _find_anikoto_show(media: dict) -> dict:
    primary_en = (media.get("title") or {}).get("english")
    primary_rom = (media.get("title") or {}).get("romaji")
    synonyms = media.get("synonyms") or []

    keywords = list(dict.fromkeys(k for k in [primary_en, primary_rom, *synonyms] if k))
    all_candidates: dict[str, dict] = {}

    for k in keywords[:5]:
        try:
            res = await _search_anikoto(k)
        except Exception:
            res = []
        for c in res:
            all_candidates[c["slug"]] = c

    candidates = list(all_candidates.values())
    if not candidates:
        raise RuntimeError(f"No results found on Anikoto for: {primary_en or primary_rom}")

    scored = sorted(
        ({**c, "score": _score_candidate(c, primary_en, primary_rom, synonyms)} for c in candidates),
        key=lambda c: c["score"], reverse=True,
    )

    chosen = scored[0]
    watch_html = await _http_get(f"{ANIKOTO}/watch/{chosen['slug']}", {"Referer": f"{ANIKOTO}/"})
    show_id_m = _SHOW_ID_RE.search(watch_html)
    if not show_id_m:
        raise RuntimeError(f"Could not find show ID for slug: {chosen['slug']}")

    return {"slug": chosen["slug"], "showId": show_id_m.group(1), "title": chosen["name"]}


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


_DATA_ID_RE = re.compile(r'data-id="([^"]*)"')


async def _extract_embed_source(embed_url: str) -> Optional[dict]:
    try:
        page_html = await _http_get(embed_url, {"Referer": SPOOF_REF, "Accept-Language": "en-US,en;q=0.9"})
        m = _DATA_ID_RE.search(page_html)
        if not m or not m.group(1):
            return None
        file_id = m.group(1)
        from urllib.parse import urlparse
        origin = f"{urlparse(embed_url).scheme}://{urlparse(embed_url).netloc}"
        data = await _get_json(
            f"{origin}/stream/getSources?id={file_id}&id={file_id}",
            {"Referer": f"{origin}/", "X-Requested-With": "XMLHttpRequest"},
        )
        return {"fileId": file_id, "data": data, "origin": origin}
    except Exception:
        return None


_EPISODE_ANCHOR_RE = re.compile(r'<a\s+[^>]*data-id="([^"]*)"[^>]*>([\s\S]*?)</a>')
_EPISODE_ANCHOR_ATTR_ONLY_RE = re.compile(r'<a\s+[^>]*data-id="([^"]*)"[^>]*>')
_TITLE_SPAN_RE = re.compile(r'<span class="d-title"[^>]*>([\s\S]*?)</span>')


def _get_data_attr(tag: str, name: str) -> str:
    m = re.search(rf'data-{name}="([^"]*)"', tag)
    return m.group(1) if m else ""


async def get_episodes(anilist_id: Any, ctx: Optional[dict] = None) -> dict:
    import asyncio

    ctx = ctx or {}
    media = ctx.get("media") or await get_media(anilist_id)
    if not media:
        raise RuntimeError(f"Could not resolve media for AniList ID: {anilist_id}")

    async def _get_anizip():
        if ctx.get("anizip") is not None:
            return ctx["anizip"]
        try:
            return await _get_json(f"{ANIZIP}?anilist_id={anilist_id}")
        except Exception:
            return None

    show, anizip_res = await asyncio.gather(_find_anikoto_show(media), _get_anizip())

    list_json = await _get_json(
        f"{ANIKOTO}/ajax/episode/list/{show['showId']}",
        {"X-Requested-With": "XMLHttpRequest", "Referer": f"{ANIKOTO}/watch/{show['slug']}"},
    )

    html = list_json.get("result") or ""
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


_TYPE_BLOCK_RE = re.compile(r'<div class="type" data-type="([^"]+)">([\s\S]*?)</ul>\s*</div>')
_LI_LINK_RE = re.compile(r'<li\s+([^>]*data-link-id[^>]*)>([\s\S]*?)</li>')
_LINK_ID_RE = re.compile(r'data-link-id="([^"]+)"')


async def get_watch(anilist_id: Any, audio: str, ep_num: Any, ctx: Optional[dict] = None) -> dict:
    ctx = ctx or {}
    ep_num = int(ep_num)
    if audio not in ("sub", "dub"):
        raise RuntimeError("audio must be sub or dub")

    media = ctx.get("media") or await get_media(anilist_id)
    if not media:
        raise RuntimeError(f"Could not resolve media for AniList ID: {anilist_id}")

    show = await _find_anikoto_show(media)
    list_json = await _get_json(
        f"{ANIKOTO}/ajax/episode/list/{show['showId']}",
        {"X-Requested-With": "XMLHttpRequest", "Referer": f"{ANIKOTO}/watch/{show['slug']}"},
    )

    html = list_json.get("result") or ""
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

    import asyncio

    async def _safe(coro):
        try:
            return await coro
        except Exception:
            return None

    server_data, mapper_data = await asyncio.gather(
        _safe(_get_json(
            f"{ANIKOTO}/ajax/server/list?servers={target_ep['ids']}",
            {"X-Requested-With": "XMLHttpRequest", "Referer": f"{ANIKOTO}/"},
        )),
        _safe(_get_json(f"{MAPPER}/{target_ep['mal']}/{target_ep['slug']}/{target_ep['timestamp']}", {"Referer": f"{ANIKOTO}/"}))
        if target_ep["mal"] and target_ep["slug"] and target_ep["timestamp"] else _safe(_noop()),
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
                for d_label, d_url in audio_obj["download"].items():
                    if isinstance(d_url, str) and d_url:
                        download_items.append({"url": d_url, "name": clean_name})

    streams = []
    subtitles = []
    downloads = []
    server_seen = set()
    sub_seen = set()
    dl_seen = set()

    for item in server_items:
        if item["name"] in server_seen:
            continue
        server_seen.add(item["name"])

        if item["linkId"].startswith("http"):
            resolved = {"result": {"url": item["linkId"]}}
        else:
            resolved = await _safe(_get_json(
                f"{ANIKOTO}/ajax/server?get={item['linkId']}",
                {"X-Requested-With": "XMLHttpRequest", "Referer": f"{ANIKOTO}/"},
            ))

        embed_url = (resolved or {}).get("result", {}).get("url")
        if not embed_url:
            continue

        server_intro = {"start": 0, "end": 0}
        server_outro = {"start": 0, "end": 0}

        skip_data = (resolved or {}).get("result", {}).get("skip_data") or {}
        if len(skip_data.get("intro") or []) == 2:
            s, e = skip_data["intro"]
            if s or e:
                server_intro = {"start": float(s) if s else 0, "end": float(e) if e else 0}
        if len(skip_data.get("outro") or []) == 2:
            s, e = skip_data["outro"]
            if s or e:
                server_outro = {"start": float(s) if s else 0, "end": float(e) if e else 0}

        hls_url = None
        if "#aHR0c" in embed_url:
            b64 = embed_url.split("#")[1]
            try:
                decoded_url = base64.b64decode(b64 + "=" * (-len(b64) % 4)).decode("utf-8", errors="replace")
                if ".m3u8" in decoded_url:
                    hls_url = decoded_url
            except Exception:
                pass

        extracted = await _extract_embed_source(embed_url)
        item_subs = []

        extracted_data = (extracted or {}).get("data") or {}
        if (extracted_data.get("sources") or {}).get("file"):
            hls_url = extracted_data["sources"]["file"]
            for t in extracted_data.get("tracks") or []:
                mapped = _map_track(t, item["name"])
                item_subs.append(mapped)
                if mapped["url"] not in sub_seen:
                    sub_seen.add(mapped["url"])
                    subtitles.append(mapped)

            if (extracted_data.get("intro") or {}).get("start") or (extracted_data.get("intro") or {}).get("end"):
                server_intro = {
                    "start": float(extracted_data["intro"].get("start") or 0),
                    "end": float(extracted_data["intro"].get("end") or 0),
                }
            if (extracted_data.get("outro") or {}).get("start") or (extracted_data.get("outro") or {}).get("end"):
                server_outro = {
                    "start": float(extracted_data["outro"].get("start") or 0),
                    "end": float(extracted_data["outro"].get("end") or 0),
                }

        origin = f"{(extracted or {}).get('origin')}/" if extracted and extracted.get("origin") else None
        if not origin:
            from urllib.parse import urlparse
            origin = f"{urlparse(embed_url).scheme}://{urlparse(embed_url).netloc}/"

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

    for dl in download_items:
        dl_url = dl.get("url")
        if not dl_url and dl.get("linkId"):
            resolved = await _safe(_get_json(
                f"{ANIKOTO}/ajax/server?get={dl['linkId']}",
                {"X-Requested-With": "XMLHttpRequest", "Referer": f"{ANIKOTO}/"},
            ))
            dl_url = (resolved or {}).get("result", {}).get("url")

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
        "headers": {"User-Agent": UA, "Referer": (streams[0]["referer"] if streams else "https://anikototv.to/")},
    }


async def _noop():
    return None
