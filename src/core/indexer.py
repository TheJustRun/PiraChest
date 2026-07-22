import argparse
import glob
import logging
import os
import re
import shutil
import urllib.request
import urllib.error
import zipfile
from typing import Optional

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
)
logger = logging.getLogger(__name__)

INDEX_RAW_ZIP_URL = "https://github.com/Caprico1/Minerva-archive-ids/archive/refs/heads/main.zip"

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_CLONE_DIR = os.path.join(_PROJECT_ROOT, "tmp", "minerva-index-clone")
_DATA_DIR = os.path.join(_PROJECT_ROOT, "src", "data")

_RE_TABLE_HEADER = re.compile(r"^\|\s*ID\s*")

_SKIP_PROVIDERS = frozenset({
    "bitsavers",
    "Internet Archive",
    "Laserdisc Collection",
    "Hardware Target Game Database",
    "Total DOS Collection",
    "Touhou Project Collection",
    "eXo",
    "Exo",
    "Eggman's Arcade Repository",
    "Lost Level",
    "Outliers",
    "Miscellaneous",
    "T-En Collection",
    "RetroAchievements",
    "MAME",
    "HBMAME",
    "FinalBurn Neo",
})

def _parse_table(content: str) -> list[dict]:
    records: list[dict] = []
    lines = content.splitlines()

    header_line_idx = None
    for idx, line in enumerate(lines):
        if _RE_TABLE_HEADER.match(line):
            header_line_idx = idx
            break

    if header_line_idx is None:
        return records

    for line in lines[header_line_idx + 1:]:
        line = line.strip()
        if not line:
            continue
        if all(c in "-|: " for c in line):
            continue

        parts = [p.strip() for p in line.split("|") if p.strip()]
        if len(parts) < 2:
            continue

        try:
            file_id = int(parts[0])
        except ValueError:
            continue

        original_path = parts[1]
        game_name = original_path.rstrip("/").split("/")[-1] if original_path else ""

        if not game_name:
            continue

        records.append({
            "name": game_name,
            "file_id": file_id,
            "original_path": original_path,
        })

    return records

def _extract_torrent_name_from_md(filepath: str) -> str:
    with open(filepath, "r", encoding="utf-8") as fh:
        first_line = fh.readline().strip()

    header_match = re.match(r"^#\s+(.+)$", first_line)
    if header_match:
        return header_match.group(1).strip()

    base = os.path.basename(filepath)
    name = base.replace("-ids.md", "")
    return name

def _parse_markdown_file(filepath: str) -> tuple[str, str, str, list[dict]]:
    with open(filepath, "r", encoding="utf-8") as fh:
        content = fh.read()

    torrent_name = _extract_torrent_name_from_md(filepath)

    classification = _classify_torrent_name(torrent_name)
    provider = classification.provider

    if provider in _SKIP_PROVIDERS:
        return classification.console, provider, "", []

    records = _parse_table(content)

    if not records:
        return classification.console, provider, "", []

    if torrent_name.endswith(".torrent"):
        torrent_file = torrent_name
    else:
        torrent_file = f"{torrent_name}.torrent"

    return classification.console, provider, torrent_file, records

def _classify_torrent_name(torrent_name: str) -> "Classification":
    from .console_mapper import classify_torrent_name
    return classify_torrent_name(torrent_name)

def download_index_zip(target_dir: str) -> str:
    logger.info("Downloading index repository from %s", INDEX_RAW_ZIP_URL)

    os.makedirs(target_dir, exist_ok=True)
    zip_path = os.path.join(target_dir, "index.zip")

    try:
        urllib.request.urlretrieve(INDEX_RAW_ZIP_URL, zip_path)
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Failed to download index ZIP: {exc}") from exc

    logger.info("Extracting ZIP to %s", target_dir)
    with zipfile.ZipFile(zip_path, "r") as zf:
        zf.extractall(target_dir)

    extracted = os.path.join(target_dir, "Minerva-archive-ids-main")
    if not os.path.isdir(extracted):
        dirs = [
            d for d in os.listdir(target_dir)
            if os.path.isdir(os.path.join(target_dir, d))
        ]
        if dirs:
            extracted = os.path.join(target_dir, dirs[0])
        else:
            raise RuntimeError(f"No directory found in extracted ZIP at {target_dir}")

    os.remove(zip_path)
    logger.info("Index downloaded and extracted to %s", extracted)
    return extracted

def download_index_git_fallback(target_dir: str) -> str:
    logger.info("Falling back to git clone for %s", INDEX_RAW_ZIP_URL)

    import subprocess

    os.makedirs(target_dir, exist_ok=True)
    try:
        subprocess.run(
            ["git", "clone", INDEX_RAW_ZIP_URL, target_dir],
            check=True,
            capture_output=True,
        )
    except FileNotFoundError:
        raise RuntimeError(
            "git not found in PATH. Install git or ensure the ZIP download succeeds."
        ) from None
    except subprocess.CalledProcessError as exc:
        stderr = exc.stderr.decode("utf-8", errors="replace") if exc.stderr else "unknown"
        raise RuntimeError(f"git clone failed: {stderr}") from exc

    logger.info("Index cloned to %s", target_dir)
    return target_dir

def clone_index(target_dir: str) -> str:
    try:
        return download_index_zip(target_dir)
    except Exception as exc:
        logger.warning("ZIP download failed (%s), trying git fallback…", exc)
        try:
            return download_index_git_fallback(target_dir)
        except Exception as exc2:
            raise RuntimeError(
                f"Could not download index: {exc2}"
            ) from exc2

def _is_valid_console(console_name: str) -> bool:
    from .console_mapper import _CONSOLE_CANONICAL
    canonical_values = set(_CONSOLE_CANONICAL.values())

    if console_name in canonical_values:
        return True

    if console_name == "MAME":
        return True

    for known in canonical_values:
        if console_name.startswith(known):
            return True

    words = console_name.split()
    if len(words) >= 2 and all(w.isalpha() or w.isdigit() for w in words):
        return True

    if len(words) == 1 and len(words[0]) >= 3 and words[0].isalpha():
        return True

    return False

def sync_index(
    db_path: Optional[str] = None,
    batch_size: int = 2000,
    on_progress=None,
) -> int:
    from .database import clear_roms, init_db, upsert_roms_streaming
    from .console_variants import guess_variant

    logger.info("Starting ROM index sync (Console-first architecture)")
    logger.info("=" * 60)

    init_db(db_path)
    clear_roms(db_path)
    logger.info("Database cleared and ready for upsert")

    clone_dir = clone_index(_CLONE_DIR)

    try:
        md_files = sorted(glob.glob(os.path.join(clone_dir, "**", "*.md"), recursive=True))

        if not md_files:
            logger.warning("No .md files found in %s index may be empty", clone_dir)
            return 0

        logger.info("Found %d markdown file(s) to parse", len(md_files))

        total_parsed = 0

        def _record_generator():
            nonlocal total_parsed
            for md_file in md_files:
                basename = os.path.basename(md_file)
                if basename == "README.md":
                    continue

                try:
                    console_name, provider, torrent_file, records = _parse_markdown_file(md_file)
                except Exception as exc:
                    logger.warning("Failed to parse %s: %s", basename, exc)
                    continue

                if not records or not _is_valid_console(console_name):
                    continue

                classification = _classify_torrent_name(
                    _extract_torrent_name_from_md(md_file)
                )
                variant = guess_variant(console_name, list(classification.variant_parts))

                is_bare_brand = (
                    provider in ("TOSEC", "TOSEC-ISO", "TOSEC-PIX")
                    and len(console_name.split()) <= 1
                )

                for rec in records:
                    rec_console_name = console_name

                    if is_bare_brand:
                        path_bits = rec.get("original_path", "").split("/")
                        if len(path_bits) > 4 and path_bits[4].strip():
                            rec_console_name = f"{console_name} {path_bits[4].strip()}"

                    total_parsed += 1
                    yield {
                        "title": rec["name"],
                        "author": "",
                        "date": None,
                        "console": rec_console_name,
                        "source": provider,
                        "file_size": None,
                        "file_size_bytes": 0,
                        "download_url": None,
                        "region": None,
                        "lang": None,
                        "torrent_file": torrent_file,
                        "file_id": rec.get("file_id", 1),
                        "variant": variant,
                    }
                records.clear()

        total = upsert_roms_streaming(
            _record_generator(),
            db_path,
            batch_size=batch_size,
            on_batch=on_progress,
        )

        logger.info(
            "Total ROM records: %d in database (parsed %d rows, duplicates autoskipped by DB constraint)",
            total, total_parsed,
        )
        logger.info("Sync complete. %d ROMs in database.", total)
        return total
    finally:
        shutil.rmtree(_CLONE_DIR, ignore_errors=True)

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Minerva Archive ROM Indexer, sync, search, or show stats.",
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument(
        "--sync",
        action="store_true",
        help="Download, parse, and populate the ROM index database.",
    )
    group.add_argument(
        "--search",
        type=str,
        metavar="QUERY",
        help="Search the ROM index and print results.",
    )
    group.add_argument(
        "--stats",
        action="store_true",
        help="Show index statistics.",
    )

    args = parser.parse_args()

    if args.sync:
        total = sync_index()
        print(f"\n[OK] Sync complete. {total} ROMs in database.")

    elif args.search:
        from .database import search_roms, init_db

        init_db()
        results = search_roms(args.search)
        if not results:
            print(f"No ROMs matching '{args.search}'.")
        else:
            print(f"\nFound {len(results)} ROM(s) matching '{args.search}':\n")
            print(f"{'Console':<45} {'Source':<18} {'File ID':>8}  {'Game Name'}")
            print("-" * 110)
            for rom in results:
                print(
                    f"{rom['console']:<45} {rom['source']:<18} "
                    f"{rom['file_id']:>8}  {rom['title']}"
                )
            print(f"\n({len(results)} result(s))")

    elif args.stats:
        from .database import get_stats, init_db

        init_db()
        stats = get_stats()
        print("\nROM Index Statistics:")
        print(f"  Total ROMs : {stats['total_roms']}")
        print(f"  Consoles   : {stats['consoles']}")
        print(f"  Sources    : {stats['sources']}")

if __name__ == "__main__":
    main()