import asyncio
import re
from typing import Any, Optional
from urllib.parse import quote, urlparse

from ..anime_backend import episode_meta, fetch_html, fetch_json

BASE = "https://epeng.animeapps.top"

_VIDEO_URL_RE = re.compile(r'videoUrl\s*:\s*"([^"]+)"')


async def _fetch_servers(anilist_id: Any) -> list[dict]:
    data = await fetch_json(f"{BASE}/api2.php?epid={anilist_id}")
    return data if isinstance(data, list) else []


async def _fetch_player_links(provider_link: str) -> list[dict]:
    data = await fetch_json(f"{BASE}/apilink.php?data={quote(provider_link, safe='')}")
    return data if isinstance(data, list) else []


def _origin(url: str) -> str:
    parts = urlparse(url)
    return f"{parts.scheme}://{parts.netloc}"


def _extract_video_url(html: str, origin: str) -> Optional[str]:
    m = _VIDEO_URL_RE.search(html)
    if not m:
        return None
    raw = m.group(1)
    if re.match(r"^https?://", raw, re.IGNORECASE):
        return raw
    return f"{origin}{'' if raw.startswith('/') else '/'}{raw}"


async def _resolve_player_stream(player_link: str) -> dict:
    origin = _origin(player_link)
    html = await fetch_html(player_link, {"Referer": f"{origin}/"})
    hls = _extract_video_url(html, origin)
    if not hls:
        raise RuntimeError(f"anibd: no videoUrl found at {player_link}")
    return {"hls": hls, "referer": f"{origin}/"}


def _audio_from_server_name(name: str = "") -> str:
    return "dub" if re.search(r"dub", name, re.IGNORECASE) else "sub"


def _build_episode_lists(anilist_id: Any, groups: list[dict], ctx: dict) -> dict:
    sub: list[dict] = []
    dub: list[dict] = []
    seen: set[tuple[str, int]] = set()

    for group in groups:
        audio = _audio_from_server_name(group.get("server_name", ""))
        bucket = dub if audio == "dub" else sub
        for ep in group.get("server_data") or []:
            raw_number = ep.get("name", ep.get("slug"))
            try:
                number = int(float(raw_number))
            except (TypeError, ValueError):
                continue
            if number < 1:
                continue
            if (audio, number) in seen:
                continue
            seen.add((audio, number))

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

    return {
        "meta": {
            "id": str(anilist_id),
            "source": "anibd",
            "matchScore": 1,
            "numbering": "standard",
            "episodeOffset": 0,
        },
        "episodes": _build_episode_lists(anilist_id, groups, ctx),
    }


async def _find_episode_link(anilist_id: Any, audio: str, ep_num: Any) -> Optional[str]:
    groups = await _fetch_servers(anilist_id)
    for group in groups:
        if _audio_from_server_name(group.get("server_name", "")) != audio:
            continue
        for ep in group.get("server_data") or []:
            try:
                if int(float(ep.get("name", ep.get("slug")))) == int(ep_num):
                    return ep.get("link")
            except (TypeError, ValueError):
                continue
    return None


async def get_watch(anilist_id: Any, audio: str, ep_num: Any) -> dict:
    provider_link = await _find_episode_link(anilist_id, audio, ep_num)
    if not provider_link:
        raise RuntimeError(f"anibd episode {ep_num} not found")

    servers = [e for e in await _fetch_player_links(provider_link) if e.get("link")]
    resolved = await asyncio.gather(
        *(_resolve_player_stream(e["link"]) for e in servers), return_exceptions=True
    )

    streams: list[dict] = []
    active_assigned = False
    for entry, result in zip(servers, resolved, strict=True):
        link = entry["link"]
        server = entry.get("server", "AniBD")
        if isinstance(result, Exception):
            streams.append({
                "url": link,
                "type": "embed",
                "server": server,
                "referer": f"{_origin(link)}/",
                "priority": 1,
                "isActive": False,
            })
            continue
        streams.append({
            "url": result["hls"],
            "type": "hls",
            "server": server,
            "referer": result["referer"],
            "priority": 4 if active_assigned else 5,
            "isActive": not active_assigned,
        })
        active_assigned = True

    return {
        "anilistId": int(anilist_id),
        "episode": int(ep_num),
        "audio": audio,
        "streams": streams,
    }
