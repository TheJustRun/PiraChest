from __future__ import annotations

import gc
import logging
import os
import sqlite3
from typing import Optional

logger = logging.getLogger(__name__)

from .config import paths as _paths

_DATA_DIR = _paths.data_dir
_DB_PATH = _paths.db_path

def _migrate_name_to_title(conn: sqlite3.Connection) -> None:
    existing = {
        row["name"].lower()
        for row in conn.execute("PRAGMA table_info(roms)").fetchall()
    }

    if "title" in existing:
        return

    if "name" not in existing:
        return

    logger.info("Recreating roms table with new schema (legacy → title)")

    conn.execute("DROP TABLE IF EXISTS roms")

    conn.execute("""
        CREATE TABLE roms (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            title           TEXT    NOT NULL DEFAULT '',
            author          TEXT    NOT NULL DEFAULT '',
            date            TEXT,
            console         TEXT    NOT NULL DEFAULT '',
            source          TEXT    NOT NULL DEFAULT '',
            file_size       TEXT,
            file_size_bytes INTEGER DEFAULT 0,
            download_url    TEXT,
            region          TEXT,
            lang            TEXT,
            torrent_file    TEXT,
            file_id         INTEGER DEFAULT 1,
            variant         TEXT DEFAULT ''
        )
    """)

    conn.execute("CREATE INDEX IF NOT EXISTS idx_roms_title ON roms(title)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_roms_console ON roms(console)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_roms_source ON roms(source)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_roms_title_console ON roms(title, console)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_roms_console_variant ON roms(console, variant)")
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_roms_dedup ON roms(console, source, title)")

    conn.commit()
    logger.info("Table recreation complete")

def _escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")

def _dedup_existing_rows(conn: sqlite3.Connection) -> None:
    if not _table_exists(conn, "roms"):
        return
    conn.execute("""
        DELETE FROM roms
        WHERE id NOT IN (
            SELECT MIN(id) FROM roms GROUP BY console, source, title
        )
    """)

def get_db_path() -> str:
    return _DB_PATH

_conn_cache: dict[str, sqlite3.Connection] = {}

def get_connection(db_path: Optional[str] = None) -> sqlite3.Connection:
    if db_path is None:
        db_path = _DB_PATH
    cached = _conn_cache.get(db_path)
    if cached is not None:
        try:
            cached.execute("SELECT 1")
            return cached
        except sqlite3.Error:
            _conn_cache.pop(db_path, None)
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA temp_store=MEMORY")
    conn.execute("PRAGMA cache_size=-2000")
    _conn_cache[db_path] = conn
    return conn

def close_connection(db_path: Optional[str] = None) -> None:
    key = db_path or _DB_PATH
    conn = _conn_cache.pop(key, None)
    if conn is not None:
        try:
            conn.close()
        except sqlite3.Error:
            pass

def _add_column_if_missing(
    conn: sqlite3.Connection,
    table: str,
    column: str,
    definition: str,
) -> None:
    existing = {
        row["name"].lower()
        for row in conn.execute(f"PRAGMA table_info({table})").fetchall()
    }
    if column.lower() not in existing:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

def _migrate_console_names(conn: sqlite3.Connection) -> None:
    from . import console_mapper as _cm

    if not _table_exists(conn, "roms"):
        return

    rows = conn.execute(
        "SELECT DISTINCT console FROM roms WHERE console != ''"
    ).fetchall()

    updates = []
    for r in rows:
        raw = r["console"]
        canonical = _cm._normalize_db_console(raw)
        if canonical and canonical != raw:
            updates.append((canonical, raw))

    if not updates:
        return

    logger.info("Migrating %d stale console name(s) to canonical form", len(updates))
    for canonical, raw in updates:
        conn.execute(
            "UPDATE roms SET console = ? WHERE console = ?",
            (canonical, raw),
        )
        logger.info("  %r -> %r", raw, canonical)

    conn.commit()

def init_db(db_path: Optional[str] = None) -> None:
    conn = get_connection(db_path)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS roms (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            title           TEXT    NOT NULL DEFAULT '',
            author          TEXT    NOT NULL DEFAULT '',
            date            TEXT,
            console         TEXT    NOT NULL DEFAULT '',
            source          TEXT    NOT NULL DEFAULT '',
            file_size       TEXT,
            file_size_bytes INTEGER DEFAULT 0,
            download_url    TEXT,
            region          TEXT,
            lang            TEXT,
            torrent_file    TEXT,
            file_id         INTEGER DEFAULT 1,
            variant         TEXT DEFAULT ''
        )
    """)

    _add_column_if_missing(conn, "roms", "author", "TEXT NOT NULL DEFAULT ''")
    _add_column_if_missing(conn, "roms", "date", "TEXT")
    _add_column_if_missing(conn, "roms", "file_size", "TEXT")
    _add_column_if_missing(conn, "roms", "file_size_bytes", "INTEGER DEFAULT 0")
    _add_column_if_missing(conn, "roms", "download_url", "TEXT")
    _add_column_if_missing(conn, "roms", "region", "TEXT")
    _add_column_if_missing(conn, "roms", "lang", "TEXT")
    _add_column_if_missing(conn, "roms", "file_id", "INTEGER DEFAULT 1")
    _add_column_if_missing(conn, "roms", "variant", "TEXT DEFAULT ''")

    _migrate_name_to_title(conn)

    conn.execute("CREATE INDEX IF NOT EXISTS idx_roms_title ON roms(title)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_roms_console ON roms(console)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_roms_source ON roms(source)")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_roms_title_console ON roms(title, console)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_roms_console_variant ON roms(console, variant)"
    )
    _dedup_existing_rows(conn)
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_roms_dedup ON roms(console, source, title)"
    )

    conn.commit()
    logger.info("Database initialised at %s", db_path)

def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name=?",
        (table,),
    ).fetchone()
    return row[0] > 0

def clear_roms(db_path: Optional[str] = None) -> int:
    conn = get_connection(db_path)
    if not _table_exists(conn, "roms"):
        return 0
    total = conn.execute("SELECT COUNT(*) FROM roms").fetchone()[0]
    conn.execute("DELETE FROM roms")
    conn.commit()
    logger.info("Cleared roms table (%d rows removed)", total)
    return total

def upsert_roms(roms: list[dict], db_path: Optional[str] = None) -> int:
    if not roms:
        return 0

    for r in roms:
        r.setdefault("file_id", 1)
        r.setdefault("variant", "")

    conn = get_connection(db_path)
    conn.executemany(
        """
        INSERT OR IGNORE INTO roms
            (title, author, date, console, source,
             file_size, file_size_bytes, download_url, region, lang, torrent_file, file_id, variant)
        VALUES
            (:title, :author, :date, :console, :source,
             :file_size, :file_size_bytes, :download_url, :region, :lang, :torrent_file, :file_id, :variant)
        """,
        roms,
    )
    total = conn.execute("SELECT COUNT(*) FROM roms").fetchone()[0]
    conn.commit()
    logger.info("Inserted ROM records — total rows in DB: %d", total)
    return total

def upsert_roms_streaming(
    rom_iter,
    db_path: Optional[str] = None,
    batch_size: int = 2000,
    on_batch=None,
) -> int:
    conn = get_connection(db_path)
    insert_sql = """
        INSERT OR IGNORE INTO roms
            (title, author, date, console, source,
             file_size, file_size_bytes, download_url, region, lang, torrent_file, file_id, variant)
        VALUES
            (:title, :author, :date, :console, :source,
             :file_size, :file_size_bytes, :download_url, :region, :lang, :torrent_file, :file_id, :variant)
    """

    batch: list[dict] = []
    processed = 0

    def _flush():
        nonlocal processed
        if not batch:
            return
        conn.executemany(insert_sql, batch)
        conn.commit()
        processed += len(batch)
        batch.clear()
        if on_batch is not None:
            try:
                on_batch(processed)
            except Exception:
                logger.exception("on_batch progress callback raised — continuing sync")

    for rec in rom_iter:
        rec.setdefault("file_id", 1)
        rec.setdefault("variant", "")
        batch.append(rec)
        if len(batch) >= batch_size:
            _flush()

    _flush()

    total = conn.execute("SELECT COUNT(*) FROM roms").fetchone()[0]
    conn.execute("PRAGMA optimize")
    logger.info("Processed %d ROM records (streaming) — total rows in DB: %d", processed, total)
    gc.collect()
    return total

def parse_file_size(size_str: str | None) -> tuple[str, int]:
    if not size_str:
        return ("0 B", 0)
    size_str = size_str.strip().upper()
    multipliers: dict[str, int] = {
        "B": 1,
        "KB": 1_024,
        "MB": 1_024**2,
        "GB": 1_024**3,
        "TB": 1_024**4,
    }
    parts = size_str.split()
    if len(parts) == 2:
        try:
            value = float(parts[0])
            unit = parts[1].rstrip(".")
            bytes_val = int(value * multipliers.get(unit, 1))
            return (size_str, bytes_val)
        except (ValueError, KeyError):
            pass
    try:
        bytes_val = int(float(size_str))
        return (size_str, bytes_val)
    except ValueError:
        return ("0 B", 0)

def count_roms(
    query: str = "",
    console: Optional[str] = None,
    sources: Optional[list[str]] = None,
    variant: Optional[str] = None,
    db_path: Optional[str] = None,
) -> int:
    conn = get_connection(db_path)
    if not _table_exists(conn, "roms"):
        return 0

    clauses: list[str] = []
    params: list[object] = []

    if console:
        clauses.append("console = ?")
        params.append(console)
    if sources:
        placeholders = ", ".join("?" for _ in sources)
        clauses.append(f"source IN ({placeholders})")
        params.extend(sources)
    if variant:
        clauses.append("variant = ?")
        params.append(variant)
    if query:
        for word in query.split():
            escaped = _escape_like(word)
            clauses.append("(title LIKE ? ESCAPE '\\' OR author LIKE ? ESCAPE '\\')")
            params.extend([f"%{escaped}%", f"%{escaped}%"])

    where = "WHERE " + " AND ".join(clauses) if clauses else ""
    total = conn.execute(f"SELECT COUNT(*) FROM roms {where}", params).fetchone()[0]
    return total

def search_roms(
    query: str = "",
    console: Optional[str] = None,
    sources: Optional[list[str]] = None,
    variant: Optional[str] = None,
    offset: int = 0,
    limit: int = 20,
    sort_field: str = "title",
    sort_dir: str = "ASC",
    db_path: Optional[str] = None,
) -> list[dict]:
    conn = get_connection(db_path)
    if not _table_exists(conn, "roms"):
        return []

    clauses: list[str] = []
    params: list[object] = []

    if console:
        clauses.append("console = ?")
        params.append(console)
    if sources:
        placeholders = ", ".join("?" for _ in sources)
        clauses.append(f"source IN ({placeholders})")
        params.extend(sources)
    if variant:
        clauses.append("variant = ?")
        params.append(variant)

    if query:
        for word in query.split():
            escaped = _escape_like(word)
            clauses.append("(title LIKE ? ESCAPE '\\' OR author LIKE ? ESCAPE '\\')")
            params.extend([f"%{escaped}%", f"%{escaped}%"])

    where = "WHERE " + " AND ".join(clauses) if clauses else ""

    valid_fields = {"title", "author", "date", "console", "source", "file_size_bytes", "variant"}
    if sort_field not in valid_fields:
        sort_field = "title"
    sort_dir = "DESC" if sort_dir.upper() == "DESC" else "ASC"

    rows = conn.execute(
        f"""
        SELECT id, title, author, date, console, source,
               file_size, file_size_bytes,
               download_url, region, lang, torrent_file, file_id, variant
        FROM roms
        {where}
        ORDER BY {sort_field} {sort_dir}
        LIMIT ? OFFSET ?
        """,
        params + [limit, offset],
    ).fetchall()

    return [dict(r) for r in rows]

def get_all_consoles(
    db_path: Optional[str] = None,
    sources: Optional[list[str]] = None,
) -> list[str]:
    from . import console_mapper as _cm
    from . import console_variants as _cv

    conn = get_connection(db_path)
    if not _table_exists(conn, "roms"):
        return []

    if sources:
        placeholders = ", ".join("?" for _ in sources)
        rows = conn.execute(
            f"SELECT console, COUNT(*) as cnt FROM roms "
            f"WHERE console != '' AND source IN ({placeholders}) GROUP BY console",
            sources,
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT console, COUNT(*) as cnt FROM roms WHERE console != '' GROUP BY console"
        ).fetchall()

    canonical_counts: dict[str, int] = {}
    for r in rows:
        canonical = _cm._normalize_db_console(r["console"])
        canonical_counts[canonical] = canonical_counts.get(canonical, 0) + r["cnt"]

    curated = set(_cv.CONSOLE_VARIANT_CONFIG.keys())
    result = sorted(
        name for name in canonical_counts
        if name in curated
    )

    return result

def get_all_variants(
    console: str,
    db_path: Optional[str] = None,
    sources: Optional[list[str]] = None,
) -> list[str]:
    if not console:
        return []
    conn = get_connection(db_path)
    if not _table_exists(conn, "roms"):
        return []

    if sources:
        placeholders = ", ".join("?" for _ in sources)
        rows = conn.execute(
            f"SELECT DISTINCT variant FROM roms "
            f"WHERE console = ? AND variant != '' AND source IN ({placeholders}) "
            f"ORDER BY variant",
            [console, *sources],
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT DISTINCT variant FROM roms WHERE console = ? AND variant != '' ORDER BY variant",
            (console,),
        ).fetchall()
    return [r["variant"] for r in rows]

def get_all_sources(db_path: Optional[str] = None) -> list[str]:
    conn = get_connection(db_path)
    if not _table_exists(conn, "roms"):
        return []
    rows = conn.execute(
        "SELECT DISTINCT source FROM roms WHERE source != '' ORDER BY source"
    ).fetchall()
    return [r["source"] for r in rows]

def get_stats(db_path: Optional[str] = None) -> dict:
    conn = get_connection(db_path)
    if not _table_exists(conn, "roms"):
        return {"total_roms": 0, "consoles": 0, "sources": 0}
    total = conn.execute("SELECT COUNT(*) FROM roms").fetchone()[0]
    consoles = conn.execute("SELECT COUNT(DISTINCT console) FROM roms WHERE console != ''").fetchone()[0]
    sources = conn.execute("SELECT COUNT(DISTINCT source) FROM roms WHERE source != ''").fetchone()[0]
    return {"total_roms": total, "consoles": consoles, "sources": sources}
