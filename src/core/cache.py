from __future__ import annotations

import asyncio
import gzip
import hashlib
import json
import logging
import math
import os
import re
import threading
import time
from collections import OrderedDict
from typing import Optional

from src.core.config import paths

logger = logging.getLogger(__name__)

DEFAULT_TTL_SECONDS = 6 * 60 * 60
_EXT = ".json.gz"
_MEM_LIMIT = 256
_GZIP_LEVEL = 1
_JSON_SEPARATORS = (",", ":")
_SAFE_RE = re.compile(r"[^A-Za-z0-9_-]+")
_INF_SENTINEL = "__inf__"


def _entry_json_default(o):
    if isinstance(o, float) and math.isinf(o):
        return _INF_SENTINEL
    raise TypeError(f"Object of type {type(o)} is not JSON serializable")


def _fix_inf(v):
    return math.inf if v == _INF_SENTINEL else v


def _safe(key: str) -> str:
    return hashlib.blake2b(key.encode("utf-8"), digest_size=16).hexdigest()


def _safe_namespace_parts(namespace: str) -> list[str]:
    parts = namespace.split(":")
    return [_SAFE_RE.sub("_", part).strip("_") or "_" for part in parts]


class CacheManager:
    def __init__(self):
        self._root = os.path.join(paths.cache_dir, "shared")
        os.makedirs(self._root, exist_ok=True)
        self._mem: "OrderedDict[str, tuple[float, object]]" = OrderedDict()
        self._lock = threading.Lock()
        self._entry_index: dict[str, set[str]] = {}

    def _namespace_dir(self, namespace: str) -> str:
        d = os.path.join(self._root, *_safe_namespace_parts(namespace))
        os.makedirs(d, exist_ok=True)
        return d

    def _path(self, namespace: str, key: str) -> str:
        return os.path.join(self._namespace_dir(namespace), f"{_safe(key)}{_EXT}")

    def _mem_get(self, mem_key: str):
        with self._lock:
            entry = self._mem.get(mem_key)
            if entry is None:
                return None
            self._mem.move_to_end(mem_key)
            return entry

    def _mem_set(self, mem_key: str, fetched_at: float, value) -> None:
        with self._lock:
            self._mem[mem_key] = (fetched_at, value)
            self._mem.move_to_end(mem_key)
            while len(self._mem) > _MEM_LIMIT:
                self._mem.popitem(last=False)

    def load(self, namespace: str, key: str, ttl_seconds: int = DEFAULT_TTL_SECONDS) -> Optional[object]:
        mem_key = f"{namespace}:{key}"
        cached = self._mem_get(mem_key)
        if cached is not None:
            fetched_at, value = cached
            if ttl_seconds is None or (time.time() - fetched_at) <= ttl_seconds:
                return value
        path = self._path(namespace, key)
        try:
            with open(path, "rb") as raw:
                with gzip.GzipFile(fileobj=raw, mode="rb") as fh:
                    payload = json.loads(fh.read())
        except OSError:
            return None
        except (json.JSONDecodeError, EOFError) as exc:
            logger.warning("Failed to read cache %s/%s: %s", namespace, key, exc)
            return None
        fetched_at = payload.get("fetched_at", 0)
        if ttl_seconds is not None and (time.time() - fetched_at) > ttl_seconds:
            return None
        value = payload.get("value")
        self._mem_set(mem_key, fetched_at, value)
        return value

    def save(self, namespace: str, key: str, value) -> None:
        fetched_at = time.time()
        mem_key = f"{namespace}:{key}"
        self._mem_set(mem_key, fetched_at, value)
        path = self._path(namespace, key)
        payload = json.dumps({"fetched_at": fetched_at, "value": value}, separators=_JSON_SEPARATORS, ensure_ascii=False).encode("utf-8")
        tmp_path = f"{path}.{os.getpid()}.{threading.get_ident()}.tmp"
        try:
            with open(tmp_path, "wb") as raw:
                with gzip.GzipFile(fileobj=raw, mode="wb", compresslevel=_GZIP_LEVEL) as fh:
                    fh.write(payload)
            os.replace(tmp_path, path)
        except OSError as exc:
            logger.error("Failed to write cache %s/%s: %s", namespace, key, exc)
            try:
                os.remove(tmp_path)
            except OSError:
                pass

    def clear_namespace(self, namespace: str) -> None:
        d = os.path.join(self._root, *_safe_namespace_parts(namespace))
        if not os.path.isdir(d):
            return
        for name in os.listdir(d):
            try:
                os.remove(os.path.join(d, name))
            except OSError:
                pass
        with self._lock:
            for mem_key in [k for k in self._mem if k.startswith(f"{namespace}:")]:
                del self._mem[mem_key]

    def clear_all(self) -> None:
        if not os.path.isdir(self._root):
            return
        for root, dirs, files in os.walk(self._root, topdown=False):
            for name in files:
                try:
                    os.remove(os.path.join(root, name))
                except OSError:
                    pass
        with self._lock:
            self._mem.clear()

    def raw_path(self, namespace: str, key: str) -> str:
        return self._path(namespace, key)

    def _mem_key(self, namespace: str, key: str) -> str:
        return f"{namespace}\x00{key}"

    def get_entry(self, namespace: str, key: str) -> Optional[dict]:
        mem_key = self._mem_key(namespace, key)
        cached = self._mem_get(mem_key)
        if cached is not None:
            return cached[1]

        path = self._path(namespace, key)
        try:
            with open(path, "rb") as raw:
                with gzip.GzipFile(fileobj=raw, mode="rb") as fh:
                    entry = json.loads(fh.read())
        except (OSError, json.JSONDecodeError, EOFError):
            return None

        for k in ("ttl", "refreshAfter", "expiresAt"):
            if k in entry:
                entry[k] = _fix_inf(entry[k])

        self._mem_set(mem_key, entry.get("cachedAt", time.time()), entry)
        with self._lock:
            self._entry_index.setdefault(namespace, set()).add(key)
        return entry

    def set_entry(self, namespace: str, key: str, entry: dict) -> None:
        mem_key = self._mem_key(namespace, key)
        self._mem_set(mem_key, entry.get("cachedAt", time.time()), entry)
        with self._lock:
            self._entry_index.setdefault(namespace, set()).add(key)

        path = self._path(namespace, key)
        tmp_path = f"{path}.{os.getpid()}.{threading.get_ident()}.tmp"
        payload = json.dumps(entry, separators=_JSON_SEPARATORS, ensure_ascii=False, default=_entry_json_default).encode("utf-8")
        try:
            with open(tmp_path, "wb") as raw:
                with gzip.GzipFile(fileobj=raw, mode="wb", compresslevel=_GZIP_LEVEL) as fh:
                    fh.write(payload)
            os.replace(tmp_path, path)
        except OSError as exc:
            logger.error("Failed to write cache entry %s/%s: %s", namespace, key, exc)
            try:
                os.remove(tmp_path)
            except OSError:
                pass

    def delete_entry(self, namespace: str, key: str) -> None:
        mem_key = self._mem_key(namespace, key)
        with self._lock:
            self._mem.pop(mem_key, None)
            self._entry_index.get(namespace, set()).discard(key)
        try:
            os.remove(self._path(namespace, key))
        except OSError:
            pass

    def delete_prefix(self, namespace: str, key_prefix: str) -> None:
        with self._lock:
            known = set(self._entry_index.get(namespace, set()))
        for key in known:
            if key.startswith(key_prefix):
                self.delete_entry(namespace, key)

    async def get_entry_async(self, namespace: str, key: str) -> Optional[dict]:
        return await asyncio.to_thread(self.get_entry, namespace, key)

    async def set_entry_async(self, namespace: str, key: str, entry: dict) -> None:
        await asyncio.to_thread(self.set_entry, namespace, key, entry)

    async def delete_entry_async(self, namespace: str, key: str) -> None:
        await asyncio.to_thread(self.delete_entry, namespace, key)

    async def delete_prefix_async(self, namespace: str, key_prefix: str) -> None:
        await asyncio.to_thread(self.delete_prefix, namespace, key_prefix)


cache = CacheManager()
