from __future__ import annotations

import logging
import os
import sqlite3
from itertools import islice
from typing import Optional

logger = logging.getLogger(__name__)

from ..config import paths as _paths

_DATA_DIR = _paths.data_dir
_DB_PATH = _paths.db_path


_ROMS_SCHEMA = """
            id              INTEGER PRIMARY KEY,
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
            variant         TEXT DEFAULT '',
            UNIQUE (console, source, title)
"""


_ROMS_INDEXES = (
    "CREATE INDEX IF NOT EXISTS idx_roms_source ON roms(source)",
    "CREATE INDEX IF NOT EXISTS idx_roms_title_console ON roms(title, console)",
    "CREATE INDEX IF NOT EXISTS idx_roms_console_variant ON roms(console, variant)",
)
_LEGACY_ROMS_INDEXES = ("idx_roms_title", "idx_roms_console")

_stats_cache: dict[str, dict] = {}


def _invalidate_stats_cache(db_path: Optional[str] = None) -> None:
    _stats_cache.pop(db_path or _DB_PATH, None)

def _create_roms_indexes(conn: sqlite3.Connection) -> None:
    for stmt in _ROMS_INDEXES:
        conn.execute(stmt)

def _build_fts(conn: sqlite3.Connection) -> None:
    conn.execute("DROP TABLE IF EXISTS roms_fts")
    try:
        conn.execute(
            "CREATE VIRTUAL TABLE roms_fts USING fts5("
            "title, author, content='roms', content_rowid='id', tokenize='trigram')"
        )
    except sqlite3.OperationalError as exc:
        logger.warning("FTS5 trigram unavailable (%s) — searches fall back to LIKE", exc)
        return
    conn.execute("INSERT INTO roms_fts(roms_fts) VALUES('rebuild')")

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

    conn.execute("DROP TABLE IF EXISTS roms_fts")
    conn.execute("DROP TABLE IF EXISTS roms")
    conn.execute(f"CREATE TABLE roms ({_ROMS_SCHEMA})")
    _create_roms_indexes(conn)

    conn.commit()
    logger.info("Table recreation complete")

def _escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")

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
    conn.execute("PRAGMA cache_size=-16384")
    conn.execute("PRAGMA mmap_size=268435456")
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
            """
            DELETE FROM roms
            WHERE console = ?
              AND EXISTS (
                  SELECT 1 FROM roms AS existing
                  WHERE existing.console = ?
                    AND existing.source = roms.source
                    AND existing.title = roms.title
              )
            """,
            (raw, canonical),
        )
        conn.execute(
            "UPDATE roms SET console = ? WHERE console = ?",
            (canonical, raw),
        )
        logger.info("  %r -> %r", raw, canonical)

    conn.commit()

def init_db(db_path: Optional[str] = None) -> None:
    conn = get_connection(db_path)

    conn.execute(f"CREATE TABLE IF NOT EXISTS roms ({_ROMS_SCHEMA})")

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
    _migrate_console_names(conn)

    _create_roms_indexes(conn)
    if not _table_exists(conn, "roms_fts") and conn.execute("SELECT 1 FROM roms LIMIT 1").fetchone():
        _build_fts(conn)
    for name in _LEGACY_ROMS_INDEXES:
        conn.execute(f"DROP INDEX IF EXISTS {name}")

    conn.commit()
    _invalidate_stats_cache(db_path)
    logger.info("Database initialised at %s", db_path)

def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name=?",
        (table,),
    ).fetchone()
    return row[0] > 0

def replace_roms_streaming(
    rom_iter,
    db_path: Optional[str] = None,
    batch_size: int = 2000,
    on_batch=None,
) -> int:
    path = db_path or _DB_PATH
    os.makedirs(os.path.dirname(path), exist_ok=True)
    conn = sqlite3.connect(path, timeout=30)
    processed = 0
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA cache_size=-65536")

        conn.execute("DROP TABLE IF EXISTS roms_staging")
        conn.execute(f"CREATE TABLE roms_staging ({_ROMS_SCHEMA})")
        conn.commit()

        insert_sql = (
            "INSERT OR IGNORE INTO roms_staging (title, console, source, torrent_file, file_id, variant)"
            " VALUES (?, ?, ?, ?, ?, ?)"
        )

        rows_iter = iter(rom_iter)
        while rows := list(islice(rows_iter, batch_size)):
            conn.executemany(insert_sql, rows)
            conn.commit()
            processed += len(rows)
            if on_batch is not None:
                try:
                    on_batch(processed)
                except Exception:
                    logger.exception("on_batch progress callback raised — continuing sync")

        if not processed:
            raise RuntimeError("Index sync produced no records — keeping the existing index")


        conn.execute("BEGIN")
        conn.execute("DROP TABLE IF EXISTS roms")
        conn.execute("ALTER TABLE roms_staging RENAME TO roms")
        _create_roms_indexes(conn)
        _build_fts(conn)
        conn.commit()
        conn.execute("PRAGMA optimize")
        total = conn.execute("SELECT COUNT(*) FROM roms").fetchone()[0]
    except BaseException:
        conn.rollback()
        conn.execute("DROP TABLE IF EXISTS roms_staging")
        conn.commit()
        raise
    finally:
        conn.close()

    _invalidate_stats_cache(db_path)
    logger.info("Rebuilt ROM index — %d rows in DB (%d records streamed)", total, processed)
    return total

def _build_rom_filter(
    conn: sqlite3.Connection,
    query: str,
    console: Optional[str],
    sources: Optional[list[str]],
    variant: Optional[str],
) -> tuple[str, list[object]]:
    clauses: list[str] = []
    params: list[object] = []

    if console:
        from . import console_mapper as _cm
        clauses.append("console = ?")
        params.append(_cm._normalize_db_console(console))
    if sources:
        placeholders = ", ".join("?" for _ in sources)
        clauses.append(f"source IN ({placeholders})")
        params.extend(sources)
    if variant:
        clauses.append("variant = ?")
        params.append(variant)
    if query:
        use_fts = _table_exists(conn, "roms_fts")
        fts_terms: list[str] = []
        for word in query.split():
            if use_fts and len(word) >= 3:
                fts_terms.append('"' + word.replace('"', '""') + '"')
            else:
                escaped = _escape_like(word)
                clauses.append("(title LIKE ? ESCAPE '\\' OR author LIKE ? ESCAPE '\\')")
                params.extend([f"%{escaped}%", f"%{escaped}%"])
        if fts_terms:
            clauses.append("id IN (SELECT rowid FROM roms_fts WHERE roms_fts MATCH ?)")
            params.append(" AND ".join(fts_terms))

    where = "WHERE " + " AND ".join(clauses) if clauses else ""
    return where, params

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


    unfiltered = not query and not console and not sources and not variant
    if unfiltered:
        cached = _stats_cache.get(db_path or _DB_PATH, {})
        if "total_roms" in cached:
            return cached["total_roms"]

    where, params = _build_rom_filter(conn, query, console, sources, variant)
    total = conn.execute(f"SELECT COUNT(*) FROM roms {where}", params).fetchone()[0]

    if unfiltered:
        _stats_cache.setdefault(db_path or _DB_PATH, {})["total_roms"] = total

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

    where, params = _build_rom_filter(conn, query, console, sources, variant)

    valid_fields = {"title", "author", "date", "console", "source", "file_size_bytes", "variant"}
    if sort_field not in valid_fields:
        sort_field = "title"
    sort_dir = "DESC" if sort_dir.upper() == "DESC" else "ASC"


    order_by = (f"title {sort_dir}, console {sort_dir}, id {sort_dir}" if sort_field == "title"
                else f"{sort_field} {sort_dir}, id {sort_dir}")

    rows = conn.execute(
        f"""
        SELECT id, title, author, date, console, source,
               file_size, file_size_bytes,
               download_url, region, lang, torrent_file, file_id, variant
        FROM roms
        {where}
        ORDER BY {order_by}
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

def get_all_sources(db_path: Optional[str] = None) -> list[str]:
    conn = get_connection(db_path)
    if not _table_exists(conn, "roms"):
        return []
    rows = conn.execute(
        "SELECT DISTINCT source FROM roms WHERE source != '' ORDER BY source"
    ).fetchall()
    return [r["source"] for r in rows]
