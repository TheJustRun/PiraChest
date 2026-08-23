from __future__ import annotations

import asyncio
import json
import os
import re
import subprocess
from typing import Any, Optional

from ..anime_backend import (
    episode_meta, get_media, get_entry as get, set_entry as cache_set, is_fresh, SHOW_IDENTITY_TTL,
)

BASE = "https://anime-dunya.com"
_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"

_SUBPROCESS_KWARGS: dict[str, Any] = {}
if os.name == "nt":
    _startupinfo = subprocess.STARTUPINFO()
    _startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    _SUBPROCESS_KWARGS["startupinfo"] = _startupinfo
    _SUBPROCESS_KWARGS["creationflags"] = subprocess.CREATE_NO_WINDOW


async def _resolve_mal_id(anilist_id: Any) -> Any:
    cache_key = f"np:animedunya:{anilist_id}"
    cached = get(cache_key)
    if is_fresh(cached):
        return cached["data"]

    media = await get_media(anilist_id)
    mal_id = (media or {}).get("idMal")
    if not mal_id:
        raise RuntimeError("AnimeDunya: no MAL ID found")

    cache_set(cache_key, mal_id, SHOW_IDENTITY_TTL)
    return mal_id


async def _fetch_html_async(url: str) -> str:
    return await asyncio.to_thread(_fetch_html, url)


def _fetch_html(url: str) -> str:
    result = subprocess.run(
        [
            "curl", "-s", "-L",
            "-A", _UA,
            "-H", "Accept: text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
            "-H", "Accept-Language: en-US,en;q=0.9",
            url,
        ],
        capture_output=True, timeout=30, **_SUBPROCESS_KWARGS,
    )
    return result.stdout.decode("utf-8", errors="replace")


_EPISODES_LIST_RE = re.compile(r'\\?"episodes\\?":\s*\[')
_STREAM_RE = re.compile(r'\\?"stream\\?":\s*')
_SOURCE_RE = re.compile(r'"source"\s*:\s*"([^"]+)"')


def _extract_episodes_list(html: str) -> list[dict]:
    match = _EPISODES_LIST_RE.search(html)
    if not match:
        return []
    idx = match.start()
    match_len = len(match.group(0))
    brace_count = 1
    result = "["
    for ch in html[idx + match_len:]:
        if ch == "[":
            brace_count += 1
        elif ch == "]":
            brace_count -= 1
        result += ch
        if brace_count == 0:
            break
    try:
        clean_str = result.replace("\\u0026", "&").replace('\\"', '"').replace("\\\\", "\\")
        return json.loads(clean_str)
    except json.JSONDecodeError:
        return []


def _extract_stream(html: str) -> Optional[dict]:
    match = _STREAM_RE.search(html)
    if not match:
        return None
    idx = match.start()
    match_len = len(match.group(0))
    brace_count = 0
    started = False
    result = ""
    for ch in html[idx + match_len:]:
        if ch == "{":
            brace_count += 1
            started = True
        elif ch == "}":
            brace_count -= 1
        if started:
            result += ch
            if brace_count == 0:
                break
    try:
        clean_str = result.replace("\\u0026", "&").replace('\\"', '"').replace("\\\\", "\\")
        return json.loads(clean_str)
    except json.JSONDecodeError:
        source_match = _SOURCE_RE.search(html)
        if source_match:
            return {"source": source_match.group(1).replace("\\", "")}
        return None


_THUMB_RE = re.compile(r'(https?://[^\s"\'`<>]+?/thumbnail/)([a-zA-Z0-9]+?)/((?:small|large)\.jpg)')


async def get_episodes(anilist_id: Any, ctx: Optional[dict] = None) -> dict:
    ctx = ctx or {}
    mal_id = await _resolve_mal_id(anilist_id)
    html = await _fetch_html_async(f"{BASE}/en/anime/{mal_id}")
    if not html:
        raise RuntimeError("AnimeDunya: episodes fetch failed")

    cdn_base = "https://cdn.anime-dunya.com/thumbnail/"
    cdn_ext = "small.jpg"

    thumb_match = _THUMB_RE.search(html)
    if thumb_match:
        cdn_base = thumb_match.group(1)
        cdn_ext = thumb_match.group(3)

    episodes = _extract_episodes_list(html)
    watchable = [ep for ep in episodes if ep.get("streamId") is not None]
    sub: list[dict] = []

    for ep in watchable:
        ep_num = ep.get("episodeNumber")
        meta = episode_meta(ep_num, ctx)
        translations = ep.get("translations")
        if isinstance(translations, list):
            custom_title = next((t.get("title") for t in translations if t.get("language") == "en"), None)
        elif isinstance(translations, dict):
            custom_title = translations.get("title")
        else:
            custom_title = None

        stream_id = ep.get("streamId")
        sub.append({
            "id": f"watch/animedunya/{anilist_id}/sub/animedunya-{ep_num}",
            "number": ep_num,
            "title": custom_title or meta["title"] or f"Episode {ep_num}",
            "duration": meta["duration"],
            "audio": "sub",
            "filler": ep.get("filler") or meta["filler"] or False,
            "uncensored": False,
            "description": meta["description"],
            "image": f"{cdn_base}{stream_id}/{cdn_ext}" if stream_id else meta["image"],
            "airDate": meta["airDate"],
        })

    sub.sort(key=lambda e: e["number"])

    media = ctx.get("media") or {}
    return {
        "meta": {
            "title": media.get("title", {}).get("english") or media.get("title", {}).get("romaji"),
            "malId": mal_id,
            "source": "animedunya",
        },
        "episodes": {"sub": sub, "dub": []},
    }


async def get_watch(anilist_id: Any, audio: str, ep_num: Any) -> dict:
    mal_id = await _resolve_mal_id(anilist_id)
    html = await _fetch_html_async(f"{BASE}/en/play/{mal_id}/{ep_num}")
    if not html:
        raise RuntimeError("AnimeDunya watch fetch failed")

    stream_data = _extract_stream(html)
    if not stream_data or not stream_data.get("source"):
        raise RuntimeError("AnimeDunya: stream source not found")

    subtitles = [
        {
            "url": s.get("src"),
            "label": s.get("label"),
            "srclang": s.get("srclang"),
            "default": s.get("default", False),
        }
        for s in (stream_data.get("subtitles") or [])
    ]

    streams = [{
        "url": stream_data["source"],
        "type": "hls",
        "server": "AnimeDunya",
        "referer": f"{BASE}/",
        "subtitles": subtitles,
        "priority": 5,
        "isActive": True,
    }]

    return {
        "anilistId": int(anilist_id),
        "malId": mal_id,
        "episode": int(ep_num),
        "audio": audio,
        "streams": streams,
    }
