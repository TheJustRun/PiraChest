from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path
from typing import Iterable
from uuid import uuid4

logger = logging.getLogger(__name__)

ARCHIVE_SUFFIXES = (".zip", ".7z", ".rar", ".tar", ".tar.gz", ".tgz", ".tar.bz2", ".tar.xz")
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _normalize(name: str) -> str:
    """Return an archive path relative to the extraction root, or raise."""
    name = name.replace("\\", "/")
    if not name or "\x00" in name:
        raise ValueError("archive contains an empty path")
    if name.startswith("/") or (len(name) > 1 and name[1] == ":"):
        raise ValueError(f"archive contains an absolute path: {name}")
    parts = [part for part in name.split("/") if part not in ("", ".")]
    if any(part == ".." for part in parts):
        raise ValueError(f"archive path escapes its folder: {name}")
    return "/".join(parts)


def _inside(root: str, target: str) -> bool:
    try:
        return os.path.commonpath([root, target]) == root
    except ValueError:
        return False


def _target(root: Path, name: str) -> Path:
    target = root.joinpath(*name.split("/"))
    if not _inside(os.path.realpath(root), os.path.realpath(target)):
        raise ValueError(f"archive path escapes its folder: {name}")
    return target


def _extract_zip(archive: Path, root: Path) -> int:
    with zipfile.ZipFile(archive) as zf:
        entries = []
        for info in zf.infolist():
            name = _normalize(info.filename)
            if not name:
                continue
            if (info.external_attr >> 16) & 0o170000 == 0o120000:
                raise ValueError("archive contains a symbolic link")
            entries.append((info, name, _target(root, name)))
        count = 0
        for info, name, target in entries:
            if info.is_dir() or info.filename.endswith(("/", "\\")):
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(info) as src, open(target, "wb") as dst:
                shutil.copyfileobj(src, dst, 1 << 20)
            count += 1
        return count


def _run_tar(args: list[str], timeout: int = 300) -> subprocess.CompletedProcess:
    return subprocess.run(
        args,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        timeout=timeout,
        creationflags=_NO_WINDOW,
    )


def _tar_entries(tool: str, archive: Path) -> list[str]:
    result = _run_tar([tool, "-tf", str(archive)])
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(detail or "unknown archive format")
    names = []
    for raw in result.stdout.splitlines():
        name = raw.decode("utf-8", errors="surrogateescape")
        normalized = _normalize(name)
        if normalized:
            names.append(normalized)
    return names


def _check_symlinks(root: Path) -> None:
    for current, dirs, files in os.walk(root):
        for name in (*dirs, *files):
            if os.path.islink(os.path.join(current, name)):
                raise ValueError("archive contains a symbolic link")


def _extract_external(archive: Path, root: Path) -> int:
    tool = shutil.which("tar")
    if not tool:
        raise RuntimeError("no archive extractor is available")
    _tar_entries(tool, archive)
    result = _run_tar([tool, "-xf", str(archive), "-C", str(root)])
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(detail or "archive extraction failed")
    _check_symlinks(root)
    return sum(1 for path in root.rglob("*") if path.is_file() and not path.is_symlink())


def _merge_dir(source: Path, target: Path) -> None:
    target.mkdir(parents=True, exist_ok=True)
    for entry in list(source.iterdir()):
        destination = target / entry.name
        if entry.is_dir() and not entry.is_symlink():
            _merge_dir(entry, destination)
        else:
            if destination.is_dir():
                shutil.rmtree(destination)
            os.replace(entry, destination)


def _commit(temp: Path, final: Path) -> None:
    entries = list(temp.iterdir())
    if not entries:
        return
    if len(entries) == 1 and entries[0].is_dir() and not entries[0].is_symlink():
        source = entries[0]
        if final.exists():
            _merge_dir(source, final)
        else:
            os.replace(source, final)
        return
    final.mkdir(parents=True, exist_ok=True)
    for entry in entries:
        destination = final / entry.name
        if entry.is_dir() and not entry.is_symlink():
            _merge_dir(entry, destination)
        else:
            if destination.is_dir():
                shutil.rmtree(destination)
            os.replace(entry, destination)


def extract_archive(archive: Path) -> bool:
    """Extract one archive beside itself. The archive itself is kept."""
    if not archive.is_file() or not archive.name.lower().endswith(ARCHIVE_SUFFIXES):
        return False
    stem = archive.stem
    for suffix in sorted(ARCHIVE_SUFFIXES, key=len, reverse=True):
        if archive.name.lower().endswith(suffix):
            stem = archive.name[:-len(suffix)]
            break
    final = archive.parent / stem
    if final.exists() and not final.is_dir():
        final = archive.parent / f"{stem} (extracted)"
    temp = archive.parent / f".{stem}.extracting-{uuid4().hex}"
    try:
        temp.mkdir()
        if archive.name.lower().endswith(".zip"):
            count = _extract_zip(archive, temp)
        else:
            count = _extract_external(archive, temp)
        if count <= 0:
            return False
        _commit(temp, final)
        logger.info("Extracted ROM archive %s to %s", archive, final)
        return True
    finally:
        shutil.rmtree(temp, ignore_errors=True)


def extract_rom_archives(paths: Iterable[str]) -> int:
    """Extract any archive files in *paths*. Failures are logged, never raised."""
    extracted = 0
    for raw in dict.fromkeys(str(path) for path in paths if path):
        archive = Path(raw)
        try:
            if extract_archive(archive):
                extracted += 1
        except Exception as exc:
            logger.warning("Could not extract ROM archive %s: %s", archive, exc)
    return extracted
