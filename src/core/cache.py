from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import sqlite3
import sys
import threading
import time
import zlib

from src.core.config import dir_size_bytes, empty_dir, paths, settings

logger = logging.getLogger(__name__)
DEFAULT_TTL_SECONDS = 6 * 60 * 60
MAX_AGE_SECONDS = 90 * 24 * 60 * 60
DB_NAME = 'cache.db'


_COMPRESS_MIN = 2048
_ZMARK = b'\x01'


def _encode(value) -> bytes:
    raw = json.dumps(value, separators=(',', ':'), ensure_ascii=False).encode('utf-8')
    if len(raw) >= _COMPRESS_MIN:
        packed = zlib.compress(raw, 1)
        if len(packed) < len(raw):
            return _ZMARK + packed
    return raw


def _loads(body):
    try:
        if body[:1] == _ZMARK:
            body = zlib.decompress(body[1:])
        return json.loads(body)
    except (ValueError, zlib.error):
        logger.warning('Dropping unreadable cache row')
        return None


def _file_size(path: str) -> int:
    try:
        return os.path.getsize(path)
    except OSError:
        return 0


class CacheManager:
    def __init__(self, db_path: str | None = None):
        self._path = db_path or os.path.join(paths.cache_dir, DB_NAME)
        folder = os.path.dirname(self._path)
        os.makedirs(folder, exist_ok=True)
        self._lock = threading.Lock()
        self._closed = False
        self._con = self._open()
        legacy = os.path.join(folder, 'shared')

        threading.Thread(target=self._housekeeping, args=(legacy,), daemon=True, name='cache-housekeeping').start()

    def _housekeeping(self, legacy: str) -> None:
        try:
            con = sqlite3.connect(self._path, timeout=3, isolation_level=None)
            try:
                con.execute('DELETE FROM kv WHERE at < ?', (time.time() - MAX_AGE_SECONDS,))
            finally:
                con.close()
        except sqlite3.Error:
            logger.debug('Cache purge skipped', exc_info=True)
        if os.path.isdir(legacy):
            shutil.rmtree(legacy, True)
        trim_stream_cache()

    def _open(self) -> sqlite3.Connection:
        for attempt in (0, 1):
            con = None
            try:
                con = sqlite3.connect(self._path, check_same_thread=False, isolation_level=None)
                con.execute('PRAGMA journal_mode=WAL')
                con.execute('PRAGMA synchronous=NORMAL')
                con.execute('PRAGMA temp_store=MEMORY')
                con.execute('PRAGMA journal_size_limit=4194304')
                con.execute('PRAGMA busy_timeout=3000')
                con.execute('CREATE TABLE IF NOT EXISTS kv (ns TEXT NOT NULL, key TEXT NOT NULL, '
                            'at REAL NOT NULL, body BLOB NOT NULL, PRIMARY KEY (ns, key))')
                return con
            except sqlite3.DatabaseError:
                if con is not None:
                    con.close()
                if attempt:
                    raise
                logger.warning('Cache database unreadable, recreating it', exc_info=True)
                for suffix in ('', '-wal', '-shm'):
                    try:
                        os.remove(self._path + suffix)
                    except OSError:
                        pass

    def _exec(self, sql: str, args: tuple = ()) -> list:
        if self._closed:
            return []
        try:
            with self._lock:
                return self._con.execute(sql, args).fetchall()
        except sqlite3.Error:
            logger.warning('Cache query failed', exc_info=True)
            return []

    def _get(self, namespace: str, key: str):
        rows = self._exec('SELECT at, body FROM kv WHERE ns=? AND key=?', (namespace, key))
        return rows[0] if rows else None

    def _put(self, namespace: str, key: str, value) -> None:
        try:
            body = _encode(value)
        except (TypeError, ValueError):
            logger.error('Cache value for %s/%s is not JSON serializable', namespace, key)
            return
        self._exec('INSERT OR REPLACE INTO kv (ns, key, at, body) VALUES (?, ?, ?, ?)',
                   (namespace, key, time.time(), body))

    def load(self, namespace: str, key: str, ttl_seconds: int | None = DEFAULT_TTL_SECONDS) -> object | None:
        row = self._get(namespace, key)
        if row is None or (ttl_seconds is not None and time.time() - row[0] > ttl_seconds):
            return None
        return _loads(row[1])

    def save(self, namespace: str, key: str, value) -> None:
        self._put(namespace, key, value)

    def get_entry(self, namespace: str, key: str) -> dict | None:
        row = self._get(namespace, key)
        return None if row is None else _loads(row[1])

    def set_entry(self, namespace: str, key: str, entry: dict) -> None:
        self._put(namespace, key, entry)

    def delete_entry(self, namespace: str, key: str) -> None:
        self._exec('DELETE FROM kv WHERE ns=? AND key=?', (namespace, key))

    def delete_prefix(self, namespace: str, key_prefix: str) -> None:
        self._exec('DELETE FROM kv WHERE ns=? AND substr(key, 1, ?)=?', (namespace, len(key_prefix), key_prefix))

    def clear_namespace(self, namespace: str) -> None:
        self._exec('DELETE FROM kv WHERE ns=? OR substr(ns, 1, ?)=?',
                   (namespace, len(namespace) + 1, namespace + ':'))

    def count_namespace(self, namespace: str) -> int:
        rows = self._exec('SELECT COUNT(*) FROM kv WHERE ns=? OR substr(ns, 1, ?)=?',
                          (namespace, len(namespace) + 1, namespace + ':'))
        return int(rows[0][0]) if rows and rows[0] else 0

    def clear_all(self) -> None:
        self._exec('DELETE FROM kv')
        self._exec('VACUUM')
        self._exec('PRAGMA wal_checkpoint(TRUNCATE)')

    def size_bytes(self) -> int:
        return sum(_file_size(self._path + suffix) for suffix in ('', '-wal', '-shm'))

    def flush_all(self) -> None:
        self._exec('PRAGMA wal_checkpoint(TRUNCATE)')

    def close(self) -> None:
        self.flush_all()
        with self._lock:
            self._closed = True
            self._con.close()

    async def get_entry_async(self, namespace: str, key: str) -> dict | None:
        return await asyncio.to_thread(self.get_entry, namespace, key)

    async def set_entry_async(self, namespace: str, key: str, entry: dict) -> None:
        await asyncio.to_thread(self.set_entry, namespace, key, entry)

    async def delete_entry_async(self, namespace: str, key: str) -> None:
        await asyncio.to_thread(self.delete_entry, namespace, key)

    async def delete_prefix_async(self, namespace: str, key_prefix: str) -> None:
        await asyncio.to_thread(self.delete_prefix, namespace, key_prefix)


cache = CacheManager()


def _sidecar_dirs() -> tuple[str, ...]:
    return (os.path.join(paths.cache_dir, 'shaders'), paths.torrent_cache, paths.stream_cache)


def _stop_stream() -> None:
    module = sys.modules.get('src.core.tv.torrent_stream')
    if module is not None:
        try:
            module.get_streamer().stop()
        except Exception:
            logger.debug('Could not stop the stream before clearing its cache', exc_info=True)


def trim_stream_cache(limit_gb: int | None = None) -> None:
    limit = int(getattr(settings, 'tv_stream_cache_limit_gb', 0) if limit_gb is None else limit_gb) * 1024 ** 3
    if limit <= 0:
        return
    files, total = [], 0
    for root, _dirs, names in os.walk(paths.stream_cache):
        for name in names:
            path = os.path.join(root, name)
            try:
                st = os.stat(path)
            except OSError:
                continue
            files.append((st.st_mtime, st.st_size, path))
            total += st.st_size
    for _mtime, size, path in sorted(files):
        if total <= limit:
            break
        try:
            os.remove(path)
            total -= size
        except OSError:
            pass


def clear_stream_cache() -> None:
    _stop_stream()
    empty_dir(paths.stream_cache)


def app_cache_size() -> int:
    return cache.size_bytes() + sum(dir_size_bytes(d) for d in _sidecar_dirs())


def clear_app_cache() -> None:
    _stop_stream()
    cache.clear_all()
    for folder in _sidecar_dirs():
        empty_dir(folder)
