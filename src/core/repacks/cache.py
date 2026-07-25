from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from typing import Optional

from ..config import paths

logger = logging.getLogger(__name__)

_CACHE_ROOT = os.path.join(paths.cache_dir, "repacks")
_POSTER_DIR = os.path.join(_CACHE_ROOT, "posters")

DEFAULT_TTL_SECONDS = 6 * 60 * 60


def _ensure_dirs() -> None:
    os.makedirs(_CACHE_ROOT, exist_ok=True)
    os.makedirs(_POSTER_DIR, exist_ok=True)


def _page_cache_path(source_key: str, page: int) -> str:
    _ensure_dirs()
    source_dir = os.path.join(_CACHE_ROOT, source_key)
    os.makedirs(source_dir, exist_ok=True)
    return os.path.join(source_dir, f"page_{page}.json")


def load_page(source_key: str, page: int, ttl_seconds: int = DEFAULT_TTL_SECONDS) -> Optional[dict]:
    path = _page_cache_path(source_key, page)
    if not os.path.isfile(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as fh:
            payload = json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("Failed to read cache %s: %s", path, exc)
        return None

    fetched_at = payload.get("fetched_at", 0)
    if ttl_seconds is not None and (time.time() - fetched_at) > ttl_seconds:
        return None
    return payload


def save_page(source_key: str, page: int, entries: list[dict], has_more: bool) -> None:
    path = _page_cache_path(source_key, page)
    payload = {
        "fetched_at": time.time(),
        "page": page,
        "has_more": has_more,
        "entries": entries,
    }
    try:
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2)
    except OSError as exc:
        logger.error("Failed to write cache %s: %s", path, exc)


def _details_cache_path(source_key: str, entry_url: str) -> str:
    _ensure_dirs()
    source_dir = os.path.join(_CACHE_ROOT, source_key, "details")
    os.makedirs(source_dir, exist_ok=True)
    digest = hashlib.sha256(entry_url.encode("utf-8")).hexdigest()
    return os.path.join(source_dir, f"{digest}.json")


def load_details(source_key: str, entry_url: str, ttl_seconds: int = DEFAULT_TTL_SECONDS) -> Optional[dict]:
    path = _details_cache_path(source_key, entry_url)
    if not os.path.isfile(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as fh:
            payload = json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("Failed to read cache %s: %s", path, exc)
        return None

    fetched_at = payload.get("fetched_at", 0)
    if ttl_seconds is not None and (time.time() - fetched_at) > ttl_seconds:
        return None
    return payload.get("details")


def save_details(source_key: str, entry_url: str, details: dict) -> None:
    path = _details_cache_path(source_key, entry_url)
    payload = {
        "fetched_at": time.time(),
        "entry_url": entry_url,
        "details": details,
    }
    try:
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2)
    except OSError as exc:
        logger.error("Failed to write cache %s: %s", path, exc)


def clear_source_cache(source_key: str) -> None:
    source_dir = os.path.join(_CACHE_ROOT, source_key)
    if not os.path.isdir(source_dir):
        return
    for root, dirs, files in os.walk(source_dir, topdown=False):
        for name in files:
            try:
                os.remove(os.path.join(root, name))
            except OSError:
                pass
        for name in dirs:
            try:
                os.rmdir(os.path.join(root, name))
            except OSError:
                pass


def clear_all_cache() -> None:
    if not os.path.isdir(_CACHE_ROOT):
        return
    for root, dirs, files in os.walk(_CACHE_ROOT, topdown=False):
        for name in files:
            try:
                os.remove(os.path.join(root, name))
            except OSError:
                pass
        for name in dirs:
            try:
                os.rmdir(os.path.join(root, name))
            except OSError:
                pass
    try:
        os.rmdir(_CACHE_ROOT)
    except OSError:
        pass


def poster_cache_path(url: str) -> str:
    _ensure_dirs()
    digest = hashlib.sha256(url.encode("utf-8")).hexdigest()
    ext = os.path.splitext(url.split("?")[0])[1]
    if not ext or len(ext) > 5:
        ext = ".jpg"
    return os.path.join(_POSTER_DIR, f"{digest}{ext}")


def has_cached_poster(url: str) -> bool:
    return os.path.isfile(poster_cache_path(url))
