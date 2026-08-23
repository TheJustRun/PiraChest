from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Optional

import requests

from ..cache import cache
from ..config import paths

logger = logging.getLogger(__name__)

_CACHE_NS = "channels_playlist"
_PLAYLIST_TTL = 6 * 60 * 60
_COUNTRIES_CACHE_NS = "channels_countries"
_COUNTRIES_TTL = 7 * 24 * 60 * 60
_COUNTRIES_API = "https://iptv-org.github.io/api/countries.json"
_COUNTRY_PLAYLIST_TPL = "https://iptv-org.github.io/iptv/countries/{code}.m3u"
_SOURCES_FILE = os.path.join(paths.config_dir, "channels_sources.json")
_SELECTION_FILE = os.path.join(paths.config_dir, "channels_country_selection.json")
_LEGACY_DEFAULT_URLS = (
    "https://iptv-org.github.io/iptv/index.m3u",
    "https://iptv-org.github.io/iptv/index.country.m3u",
)
_DEFAULT_SOURCE = {
    "id": "iptv-org",
    "name": "iptv-org",
    "kind": "country_index",
    "location": "https://iptv-org.github.io/iptv/countries/",
    "enabled": True,
}

_FALLBACK_COUNTRIES = [
    {"code": "us", "name": "United States"}, {"code": "gb", "name": "United Kingdom"},
    {"code": "ca", "name": "Canada"}, {"code": "au", "name": "Australia"},
    {"code": "de", "name": "Germany"}, {"code": "fr", "name": "France"},
    {"code": "es", "name": "Spain"}, {"code": "it", "name": "Italy"},
    {"code": "in", "name": "India"}, {"code": "br", "name": "Brazil"},
    {"code": "mx", "name": "Mexico"}, {"code": "jp", "name": "Japan"},
    {"code": "kr", "name": "South Korea"}, {"code": "ru", "name": "Russia"},
    {"code": "tr", "name": "Turkey"}, {"code": "nl", "name": "Netherlands"},
    {"code": "pl", "name": "Poland"}, {"code": "ar", "name": "Argentina"},
]

_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"

_session = requests.Session()
_session.headers.update({"User-Agent": _UA})
_session.trust_env = False

_EXTINF_RE = re.compile(r'#EXTINF:(?P<dur>-?\d+(?:\.\d+)?)\s*(?P<attrs>.*?),(?P<title>.*)$')
_ATTR_RE = re.compile(r'([a-zA-Z0-9_-]+)="([^"]*)"')
_TVG_ID_COUNTRY_RE = re.compile(r'\.([a-zA-Z]{2})(?:@|$)')


def _country_from_tvg_id(tvg_id: str) -> str:
    m = _TVG_ID_COUNTRY_RE.search(tvg_id or "")
    return m.group(1).upper() if m else ""


def _source_id(location: str) -> str:
    return hashlib.blake2b(location.encode("utf-8"), digest_size=8).hexdigest()


def _channel_id(source_id: str, url: str) -> str:
    return hashlib.blake2b(f"{source_id}:{url}".encode("utf-8"), digest_size=8).hexdigest()


def parse_m3u(text: str, source_id: str) -> list[dict]:
    channels: list[dict] = []
    pending: Optional[dict] = None
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("#EXTINF"):
            m = _EXTINF_RE.match(line)
            if not m:
                pending = None
                continue
            attrs = dict(_ATTR_RE.findall(m.group("attrs")))
            tvg_id = attrs.get("tvg-id") or ""
            group_title = attrs.get("group-title") or ""
            country = (attrs.get("tvg-country") or "").upper() or _country_from_tvg_id(tvg_id) or group_title
            pending = {
                "name": m.group("title").strip() or attrs.get("tvg-name", "Unknown"),
                "logo": attrs.get("tvg-logo") or None,
                "group": group_title,
                "country": country,
                "tvg_id": tvg_id,
            }
            continue
        if line.startswith("#"):
            continue
        if pending is None:
            continue
        url = line
        channels.append({
            "id": _channel_id(source_id, url),
            "source_id": source_id,
            "url": url,
            "name": pending["name"],
            "logo": pending["logo"],
            "group": pending["group"],
            "country": pending["country"],
            "tvg_id": pending["tvg_id"],
        })
        pending = None
    return channels


def _load_sources() -> list[dict]:
    if os.path.isfile(_SOURCES_FILE):
        try:
            with open(_SOURCES_FILE, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            if isinstance(data, list) and data:
                migrated = False
                for s in data:
                    if s.get("id") == "iptv-org" and (s.get("kind") != "country_index" or s.get("location") in _LEGACY_DEFAULT_URLS):
                        s["kind"] = _DEFAULT_SOURCE["kind"]
                        s["location"] = _DEFAULT_SOURCE["location"]
                        migrated = True
                if migrated:
                    _save_sources(data)
                    cache.clear_namespace(_CACHE_NS)
                return data
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("Failed to load channel sources: %s", exc)
    return [dict(_DEFAULT_SOURCE)]


def _save_sources(sources_list: list[dict]) -> None:
    os.makedirs(paths.config_dir, exist_ok=True)
    try:
        with open(_SOURCES_FILE, "w", encoding="utf-8") as fh:
            json.dump(sources_list, fh, indent=2)
    except OSError as exc:
        logger.error("Failed to save channel sources: %s", exc)


class ChannelSourceManager:
    def __init__(self):
        self._lock = threading.Lock()
        self._sources = _load_sources()

    def list_sources(self) -> list[dict]:
        with self._lock:
            return [dict(s) for s in self._sources]

    def add_url_source(self, name: str, url: str) -> dict:
        source = {"id": _source_id(url), "name": name or url, "kind": "url", "location": url, "enabled": True}
        with self._lock:
            self._sources = [s for s in self._sources if s["id"] != source["id"]]
            self._sources.append(source)
            _save_sources(self._sources)
        return source

    def add_file_source(self, name: str, path: str) -> dict:
        source = {"id": _source_id(path), "name": name or os.path.basename(path), "kind": "file", "location": path, "enabled": True}
        with self._lock:
            self._sources = [s for s in self._sources if s["id"] != source["id"]]
            self._sources.append(source)
            _save_sources(self._sources)
        return source

    def remove_source(self, source_id: str) -> None:
        with self._lock:
            self._sources = [s for s in self._sources if s["id"] != source_id]
            _save_sources(self._sources)
        cache.clear_namespace(_CACHE_NS)

    def set_enabled(self, source_id: str, enabled: bool) -> None:
        with self._lock:
            for s in self._sources:
                if s["id"] == source_id:
                    s["enabled"] = enabled
            _save_sources(self._sources)


sources = ChannelSourceManager()


def _fetch_country_list() -> list[dict]:
    cached = cache.load(_COUNTRIES_CACHE_NS, "list", ttl_seconds=_COUNTRIES_TTL)
    if cached is not None:
        return cached
    try:
        resp = _session.get(_COUNTRIES_API, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        countries = [{"code": c["code"].lower(), "name": c["name"]} for c in data if c.get("code") and c.get("name")]
        if countries:
            cache.save(_COUNTRIES_CACHE_NS, "list", countries)
            return countries
    except Exception as exc:
        logger.warning("Failed to fetch country list: %s", exc)
    stale = cache.load(_COUNTRIES_CACHE_NS, "list", ttl_seconds=None)
    if stale:
        return stale
    return list(_FALLBACK_COUNTRIES)


def get_available_countries() -> list[dict]:
    return _fetch_country_list()


def get_selected_countries() -> Optional[list[str]]:
    if not os.path.isfile(_SELECTION_FILE):
        return None
    try:
        with open(_SELECTION_FILE, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, list):
            return [str(c).lower() for c in data]
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("Failed to load country selection: %s", exc)
    return None


def set_selected_countries(codes: list[str]) -> None:
    os.makedirs(paths.config_dir, exist_ok=True)
    try:
        with open(_SELECTION_FILE, "w", encoding="utf-8") as fh:
            json.dump(sorted({c.lower() for c in codes}), fh, indent=2)
    except OSError as exc:
        logger.error("Failed to save country selection: %s", exc)
    cache.clear_namespace(_CACHE_NS)


def _fetch_country_channels(source_id: str, country: dict) -> list[dict]:
    code = country["code"]
    name = country["name"]
    url = _COUNTRY_PLAYLIST_TPL.format(code=code)
    try:
        resp = _session.get(url, timeout=15)
        resp.raise_for_status()
        text = resp.text
    except Exception:
        return []
    channels = parse_m3u(text, f"{source_id}:{code}")
    for ch in channels:
        ch["country"] = name
        ch["source_id"] = source_id
    return channels


def fetch_country_index_channels(source: dict, use_cache: bool = True) -> list[dict]:
    if use_cache:
        cached = cache.load(_CACHE_NS, source["id"], ttl_seconds=_PLAYLIST_TTL)
        if cached is not None:
            return cached

    countries = _fetch_country_list()
    selected = get_selected_countries()
    if selected is not None:
        selected_set = set(selected)
        countries = [c for c in countries if c["code"] in selected_set]
    all_channels: list[dict] = []
    with ThreadPoolExecutor(max_workers=12) as pool:
        for result in pool.map(lambda c: _fetch_country_channels(source["id"], c), countries):
            all_channels.extend(result)

    if all_channels or not countries:
        cache.save(_CACHE_NS, source["id"], all_channels)
        return all_channels

    stale = cache.load(_CACHE_NS, source["id"], ttl_seconds=None)
    return stale if stale is not None else []


def _fetch_text(source: dict) -> str:
    if source["kind"] == "file":
        with open(source["location"], "r", encoding="utf-8", errors="replace") as fh:
            return fh.read()
    resp = _session.get(source["location"], timeout=20)
    resp.raise_for_status()
    return resp.text


def fetch_source_channels(source: dict, use_cache: bool = True) -> list[dict]:
    if source["kind"] == "country_index":
        return fetch_country_index_channels(source, use_cache=use_cache)

    is_remote = source["kind"] != "file"
    if use_cache and is_remote:
        cached = cache.load(_CACHE_NS, source["id"], ttl_seconds=_PLAYLIST_TTL)
        if cached is not None:
            return cached
    try:
        text = _fetch_text(source)
    except Exception as exc:
        logger.warning("Failed to fetch channel source %s: %s", source.get("name"), exc)
        if is_remote:
            stale = cache.load(_CACHE_NS, source["id"], ttl_seconds=None)
            if stale is not None:
                return stale
        return []
    channels = parse_m3u(text, source["id"])
    if is_remote:
        cache.save(_CACHE_NS, source["id"], channels)
    return channels


def fetch_all_channels(use_cache: bool = True) -> list[dict]:
    result: list[dict] = []
    seen: set[str] = set()
    for source in sources.list_sources():
        if not source.get("enabled", True):
            continue
        for ch in fetch_source_channels(source, use_cache=use_cache):
            if ch["id"] in seen:
                continue
            seen.add(ch["id"])
            ch = dict(ch)
            ch["source_name"] = source["name"]
            result.append(ch)
    result.sort(key=lambda c: ((c.get("country") or "").lower(), c["name"].lower()))
    return result
