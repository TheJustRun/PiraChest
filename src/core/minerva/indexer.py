from __future__ import annotations

import logging
import posixpath
import re
import shutil
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from functools import lru_cache
from io import TextIOWrapper
from typing import Iterator, Optional

from .console_mapper import _CONSOLE_CANONICAL, _normalize_db_console, classify_torrent_name
from .console_variants import guess_variant
from .database import init_db, replace_roms_streaming
from ..config import network

logger = logging.getLogger(__name__)

INDEX_ZIP_URL = "https://github.com/Caprico1/Minerva-archive-ids/archive/refs/heads/main.zip"
TORRENTS_URL = network.cdn_base.rstrip("/") + "/"
_HEADERS = {"User-Agent": "Mozilla/5.0"}

_SKIP_PROVIDERS = frozenset({
    "bitsavers", "Internet Archive", "Laserdisc Collection", "Hardware Target Game Database",
    "Total DOS Collection", "Touhou Project Collection", "eXo", "Exo", "Eggman's Arcade Repository",
    "Lost Level", "Outliers", "Miscellaneous", "T-En Collection", "RetroAchievements",
    "MAME", "HBMAME", "FinalBurn Neo",
})
_BARE_BRAND_PROVIDERS = frozenset({"TOSEC", "TOSEC-ISO", "TOSEC-PIX"})
_CANONICAL_PREFIXES = tuple(set(_CONSOLE_CANONICAL.values()))

_RE_TORRENT_HREF = re.compile(r'href="([^"]+\.torrent)"', re.IGNORECASE)
_RE_NON_ALNUM = re.compile(r"[\W_]+")


def _torrent_key(name: str) -> str:
    return _RE_NON_ALNUM.sub("", name.removesuffix(".torrent").lower())


def _fetch_torrent_names() -> dict[str, str]:
    try:
        req = urllib.request.Request(TORRENTS_URL, headers=_HEADERS)
        with urllib.request.urlopen(req, timeout=30) as resp:
            html = resp.read().decode("utf-8", errors="replace")
    except (urllib.error.URLError, OSError) as exc:
        logger.warning("Torrent listing unavailable (%s) — keeping index spellings", exc)
        return {}
    names = (urllib.parse.unquote(href).rpartition("/")[2] for href in _RE_TORRENT_HREF.findall(html))
    return {_torrent_key(n): n for n in names}


def _download_index():
    logger.info("Downloading index from %s", INDEX_ZIP_URL)
    tmp = tempfile.TemporaryFile()
    try:
        req = urllib.request.Request(INDEX_ZIP_URL, headers=_HEADERS)
        with urllib.request.urlopen(req, timeout=60) as resp:
            shutil.copyfileobj(resp, tmp, 1 << 20)
    except (urllib.error.URLError, OSError) as exc:
        tmp.close()
        raise RuntimeError(f"Failed to download index ZIP: {exc}") from exc
    return tmp


@lru_cache(maxsize=None)
def _is_valid_console(name: str) -> bool:
    if name.startswith(_CANONICAL_PREFIXES):
        return True
    words = name.split()
    if len(words) >= 2:
        return all(w.isalpha() or w.isdigit() for w in words)
    return len(words) == 1 and len(words[0]) >= 3 and words[0].isalpha()


@lru_cache(maxsize=None)
def _brand_console(console: str, sub: str) -> str:
    return _normalize_db_console(f"{console} {sub}")


def _file_rows(fh, member: str, torrents: dict[str, str], skipped: list[str]) -> Iterator[tuple]:
    header = fh.readline().strip()
    torrent_name = header[2:].strip() if header.startswith("# ") else \
        posixpath.basename(member).replace("-ids.md", "")
    cls = classify_torrent_name(torrent_name)
    provider, console = cls.provider, cls.console
    if provider in _SKIP_PROVIDERS:
        return
    if not _is_valid_console(console):
        skipped.append(f"{torrent_name} -> {console!r}")
        return

    canonical = _normalize_db_console(console)
    variant = guess_variant(canonical, list(cls.variant_parts))

    torrent_file = torrent_name if torrent_name.endswith(".torrent") else f"{torrent_name}.torrent"
    if torrents:
        real = torrents.get(_torrent_key(torrent_file))
        if real is None:
            logger.warning("%s is not in the CDN torrent listing", torrent_file)
        else:
            torrent_file = real

    bare_brand = provider in _BARE_BRAND_PROVIDERS and len(console.split()) <= 1

    for line in fh:
        if not line.startswith("|"):
            continue
        cell, _, path = line[1:].partition("|")
        try:
            file_id = int(cell)
        except ValueError:
            continue
        path = path.strip().removesuffix("|").rstrip()
        title = path.rstrip("/").rpartition("/")[2]
        if not title:
            continue

        row_console = canonical
        if bare_brand:
            bits = path.split("/")
            if len(bits) > 4 and bits[4].strip():
                row_console = _brand_console(console, bits[4].strip())

        yield title, row_console, provider, torrent_file, file_id, variant


def _iter_rows(zf: zipfile.ZipFile, torrents: dict[str, str]) -> Iterator[tuple]:
    members = sorted(
        n for n in zf.namelist()
        if n.endswith(".md") and posixpath.basename(n) != "README.md"
    )
    logger.info("Parsing %d markdown file(s)", len(members))
    skipped: list[str] = []
    for member in members:
        try:
            with TextIOWrapper(zf.open(member), encoding="utf-8") as fh:
                yield from _file_rows(fh, member, torrents, skipped)
        except Exception as exc:
            logger.warning("Failed to parse %s: %s", member, exc)
    if skipped:
        logger.info("Skipped %d torrent(s): non-game or unrecognised consoles", len(skipped))
        for item in skipped:
            logger.debug("  skipped %s", item)


def sync_index(
    db_path: Optional[str] = None,
    batch_size: int = 2000,
    on_progress=None,
) -> int:
    init_db(db_path)
    with _download_index() as tmp, zipfile.ZipFile(tmp) as zf:
        total = replace_roms_streaming(
            _iter_rows(zf, _fetch_torrent_names()),
            db_path,
            batch_size=batch_size,
            on_batch=on_progress,
        )
    logger.info("Sync complete. %d ROMs in database.", total)
    return total
