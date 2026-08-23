from __future__ import annotations

from typing import Any, Optional
from urllib.parse import urlparse, quote

import httpx

from ..anime_backend import episode_meta, expected_count

BASE = "https://epeng.animeapps.top"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"


async def _fetch_json(url: str) -> Any:
    async with httpx.AsyncClient(timeout=20) as client:
        resp = await client.get(url, headers={"User-Agent": UA, "Accept": "application/json"})
    if resp.status_code >= 400:
        raise RuntimeError(f"anibd {resp.status_code}: {url}")
    return resp.json()


async def _fetch_html(url: str, referer: Optional[str] = None) -> str:
    headers = {"User-Agent": UA, "Accept": "text/html,application/xhtml+xml"}
    if referer:
        headers["Referer"] = referer
    async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
        resp = await client.get(url, headers=headers)
    if resp.status_code >= 400:
        raise RuntimeError(f"anibd {resp.status_code}: {url}")
    return resp.text


async def _fetch_servers(anilist_id: Any) -> list[dict]:
    data = await _fetch_json(f"{BASE}/api2.php?epid={anilist_id}")
    return data if isinstance(data, list) else []


async def _fetch_player_links(provider_link: str) -> list[dict]:
    data = await _fetch_json(f"{BASE}/apilink.php?data={quote(provider_link, safe='')}")
    return data if isinstance(data, list) else []


import re

_VIDEO_URL_RE = re.compile(r'videoUrl\s*:\s*"([^"]+)"')


def _extract_video_url(html: str, origin: str) -> Optional[str]:
    m = _VIDEO_URL_RE.search(html)
    if not m:
        return None
    raw = m.group(1)
    if re.match(r"^https?://", raw, re.IGNORECASE):
        return raw
    return f"{origin}{'' if raw.startswith('/') else '/'}{raw}"


async def _resolve_player_stream(player_link: str) -> dict:
    origin = f"{urlparse(player_link).scheme}://{urlparse(player_link).netloc}"
    referer = f"{origin}/"
    html = await _fetch_html(player_link, referer)
    hls = _extract_video_url(html, origin)
    if not hls:
        raise RuntimeError(f"anibd: no videoUrl found at {player_link}")
    return {"hls": hls, "referer": referer}


def _audio_from_server_name(name: str = "") -> str:
    return "dub" if re.search(r"dub", name, re.IGNORECASE) else "sub"


def _build_episode_lists(anilist_id: Any, groups: list[dict], ctx: dict, expected: Optional[int]) -> dict:
    sub: list[dict] = []
    dub: list[dict] = []
    seen_sub: set[int] = set()
    seen_dub: set[int] = set()

    for group in groups:
        audio = _audio_from_server_name(group.get("server_name", ""))
        for ep in group.get("server_data") or []:
            raw_number = ep.get("name", ep.get("slug"))
            try:
                number = int(float(raw_number))
            except (TypeError, ValueError):
                continue
            if number < 1:
                continue
            if expected and number > expected:
                continue

            bucket = dub if audio == "dub" else sub
            seen = seen_dub if audio == "dub" else seen_sub
            if number in seen:
                continue
            seen.add(number)

            meta = episode_meta(number, ctx)
            bucket.append({
                "id": f"watch/anibd/{anilist_id}/{audio}/anibd-{number}",
                "number": number,
                "title": meta["title"] or f"Episode {number}",
                "duration": meta["duration"],
                "filler": meta["filler"],
                "uncensored": meta["uncensored"],
                "description": meta["description"],
                "image": meta["image"],
                "airDate": meta["airDate"],
                "sourceLink": ep.get("link"),
                "audio": audio,
            })

    sub.sort(key=lambda e: e["number"])
    dub.sort(key=lambda e: e["number"])
    return {"sub": sub, "dub": dub}


async def get_episodes(anilist_id: Any, ctx: Optional[dict] = None) -> dict:
    ctx = ctx or {}
    groups = await _fetch_servers(anilist_id)
    if not groups:
        raise RuntimeError(f"anibd: no episodes found for AniList {anilist_id}")

    expected = expected_count(ctx.get("media"), ctx.get("anizip"), ctx.get("jikanEps"))
    return {
        "meta": {
            "id": str(anilist_id),
            "source": "anibd",
            "matchScore": 1,
            "numbering": "standard",
            "episodeOffset": 0,
        },
        "episodes": _build_episode_lists(anilist_id, groups, ctx, expected),
    }


async def _find_episode_link(anilist_id: Any, audio: str, ep_num: Any) -> Optional[str]:
    groups = await _fetch_servers(anilist_id)
    for group in groups:
        if _audio_from_server_name(group.get("server_name", "")) != audio:
            continue
        for ep in group.get("server_data") or []:
            raw_number = ep.get("name", ep.get("slug"))
            try:
                if int(float(raw_number)) == int(ep_num):
                    return ep.get("link")
            except (TypeError, ValueError):
                continue
    return None


async def get_watch(anilist_id: Any, audio: str, ep_num: Any) -> dict:
    provider_link = await _find_episode_link(anilist_id, audio, ep_num)
    if not provider_link:
        raise RuntimeError(f"anibd episode {ep_num} not found")

    servers = await _fetch_player_links(provider_link)
    streams: list[dict] = []
    active_assigned = False

    for entry in servers:
        link = entry.get("link")
        if not link:
            continue
        try:
            resolved = await _resolve_player_stream(link)
            streams.append({
                "url": resolved["hls"],
                "type": "hls",
                "server": entry.get("server", "AniBD"),
                "referer": resolved["referer"],
                "priority": 4 if active_assigned else 5,
                "isActive": not active_assigned,
            })
            active_assigned = True
        except Exception:
            origin = f"{urlparse(link).scheme}://{urlparse(link).netloc}/"
            streams.append({
                "url": link,
                "type": "embed",
                "server": entry.get("server", "AniBD"),
                "referer": origin,
                "priority": 1,
                "isActive": False,
            })

    return {
        "anilistId": int(anilist_id),
        "episode": int(ep_num),
        "audio": audio,
        "streams": streams,
    }
