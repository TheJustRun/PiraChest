from __future__ import annotations

import json
import logging
import os
import threading
import time
from typing import Optional

logger = logging.getLogger(__name__)

_DEFAULT_TTL = 24 * 60 * 60
_MAX_ENTRIES = 600


class ResolutionCache:
    def __init__(self, path: str = "", ttl: int = _DEFAULT_TTL, limit: int = _MAX_ENTRIES) -> None:
        self._path = path or self._default_path()
        self._ttl = max(60, int(ttl))
        self._limit = max(16, int(limit))
        self._lock = threading.RLock()
        self._entries: Optional[dict[str, dict]] = None

    @staticmethod
    def _default_path() -> str:
        try:
            from src.core.config import paths

            return os.path.join(paths.config_dir, "debrid_cache.json")
        except Exception:
            return os.path.join(os.path.expanduser("~"), ".pirachest", "debrid_cache.json")

    @property
    def path(self) -> str:
        return self._path

    def _load(self) -> dict[str, dict]:
        if self._entries is not None:
            return self._entries
        entries: dict[str, dict] = {}
        try:
            with open(self._path, encoding="utf-8") as handle:
                raw = json.load(handle)
            source = raw.get("entries") if isinstance(raw, dict) else raw
            if isinstance(source, dict):
                now = time.time()
                entries = {
                    str(key): value
                    for key, value in source.items()
                    if isinstance(value, dict) and float(value.get("expires") or 0) > now
                }
        except FileNotFoundError:
            pass
        except (OSError, ValueError) as exc:
            logger.warning("Could not read the debrid resolution cache: %s", exc)
        self._entries = entries
        return entries

    def _save(self, entries: dict[str, dict]) -> None:
        directory = os.path.dirname(self._path) or "."
        try:
            os.makedirs(directory, exist_ok=True)
            temporary = f"{self._path}.{os.getpid()}.tmp"
            with open(temporary, "w", encoding="utf-8", newline="\n") as handle:
                json.dump({"version": 1, "entries": entries}, handle, separators=(",", ":"), sort_keys=True)
            os.replace(temporary, self._path)
        except OSError:
            logger.debug("Could not write the debrid resolution cache", exc_info=True)
        self._entries = entries

    @staticmethod
    def key(provider: str, info_hash: str) -> str:
        return f"{(provider or '').strip().lower()}:{(info_hash or '').strip().lower()}"

    def get(self, provider: str, info_hash: str) -> Optional[dict]:
        if not info_hash:
            return None
        with self._lock:
            entry = self._load().get(self.key(provider, info_hash))
            if entry is None:
                return None
            if float(entry.get("expires") or 0) <= time.time():
                entries = dict(self._load())
                entries.pop(self.key(provider, info_hash), None)
                self._save(entries)
                return None
            return dict(entry)

    def put(self, provider: str, info_hash: str, transfer_id: str, name: str = "") -> None:
        if not info_hash or not transfer_id:
            return
        with self._lock:
            entries = dict(self._load())
            entries[self.key(provider, info_hash)] = {
                "transfer_id": str(transfer_id),
                "name": name or "",
                "created": time.time(),
                "expires": time.time() + self._ttl,
            }
            if len(entries) > self._limit:
                ordered = sorted(entries.items(), key=lambda item: float(item[1].get("created") or 0))
                entries = dict(ordered[-self._limit:])
            self._save(entries)

    def forget(self, provider: str, info_hash: str) -> None:
        if not info_hash:
            return
        with self._lock:
            entries = dict(self._load())
            if entries.pop(self.key(provider, info_hash), None) is not None:
                self._save(entries)

    def clear(self) -> None:
        with self._lock:
            self._save({})


cache = ResolutionCache()
