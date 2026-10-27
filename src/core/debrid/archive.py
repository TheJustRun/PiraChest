from __future__ import annotations

import logging
import os
import shutil
import subprocess
import tempfile
import time
import zipfile
from typing import Callable, Optional

logger = logging.getLogger(__name__)

_ARCHIVE_SUFFIXES = (".rar", ".zip", ".7z", ".tar", ".tgz", ".gz", ".bz2", ".xz")
_ARCHIVE_TIMEOUT = 900
_CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


class ArchiveError(Exception):
    pass


class _UnsafeArchive(ArchiveError):
    pass


def looks_like_archive(path: str) -> bool:
    name = str(path or "").split("?", 1)[0].strip().lower()
    return name.endswith(_ARCHIVE_SUFFIXES)


def _suffix(path: str) -> str:
    name = str(path or "").split("?", 1)[0].strip().lower()
    if name.endswith(".tar.gz"):
        return ".tar.gz"
    return os.path.splitext(name)[1]


def _safe_member(name: str) -> str:
    path = str(name or "").replace("\\", "/")
    if not path or path.startswith("/") or path.startswith("../") or "/../" in path or path == "..":
        raise _UnsafeArchive(f"The archive contains an unsafe path: {name}")
    if os.path.splitdrive(path)[0]:
        raise _UnsafeArchive(f"The archive contains an unsafe path: {name}")
    parts = [part for part in path.split("/") if part and part != "."]
    if not parts or ".." in parts or any(":" in part for part in parts):
        raise _UnsafeArchive(f"The archive contains an unsafe path: {name}")
    return os.path.join(*parts)


def _zip_extract(path: str, dest: str) -> list[str]:
    out: list[str] = []
    try:
        with zipfile.ZipFile(path) as archive:
            members = [member for member in archive.infolist() if not member.is_dir()]
            for member in members:
                target = os.path.join(dest, _safe_member(member.filename))
                try:
                    inside = os.path.commonpath([os.path.abspath(dest), os.path.abspath(target)])
                except ValueError as exc:
                    raise _UnsafeArchive(f"The archive contains an unsafe path: {member.filename}") from exc
                if inside != os.path.abspath(dest):
                    raise _UnsafeArchive(f"The archive contains an unsafe path: {member.filename}")
            for member in members:
                target = os.path.join(dest, _safe_member(member.filename))
                os.makedirs(os.path.dirname(target) or dest, exist_ok=True)
                with archive.open(member) as source, open(target, "wb") as handle:
                    shutil.copyfileobj(source, handle)
                out.append(target)
    except ArchiveError:
        raise
    except (OSError, RuntimeError, zipfile.BadZipFile) as exc:
        raise ArchiveError(f"Could not extract {os.path.basename(path)}: {exc}") from exc
    return out


def _tool_candidates(suffix: str) -> list[tuple[str, str]]:
    names: list[str] = []
    if suffix == ".rar":
        names.append("7z")
        names.append("7zz")
        names.append("unrar")
        names.append("WinRAR")
    elif suffix == ".7z":
        names.append("7z")
        names.append("7zz")
    else:
        names.append("7z")
        names.append("7zz")
        names.append("tar")
    candidates: list[tuple[str, str]] = []
    for name in names:
        found = shutil.which(name)
        if found:
            candidates.append((name, found))
    if os.name == "nt":
        roots = [os.environ.get("ProgramFiles", r"C:\Program Files"),
                 os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")]
        for root in roots:
            for name, relative in (
                ("7z", r"7-Zip\7z.exe"),
                ("7zz", r"7-Zip\7zz.exe"),
                ("unrar", r"WinRAR\UnRAR.exe"),
                ("WinRAR", r"WinRAR\WinRAR.exe"),
            ):
                if name not in names:
                    continue
                path = os.path.join(root, relative)
                if os.path.isfile(path) and all(existing != path for _kind, existing in candidates):
                    candidates.append((name, path))
    tar = shutil.which("tar")
    if tar and all(existing != tar for _kind, existing in candidates):
        candidates.append(("tar", tar))
    return candidates


def _command(kind: str, tool: str, path: str, dest: str) -> list[str]:
    if kind in ("7z", "7zz"):
        return [tool, "x", "-y", f"-o{dest}", path]
    if kind == "unrar":
        return [tool, "x", "-o+", "-y", path, dest + os.sep]
    if kind == "WinRAR":
        return [tool, "x", "-o+", "-y", path, dest + os.sep]
    return [tool, "-xf", path, "-C", dest]


def _files_under(root: str) -> set[str]:
    found: set[str] = set()
    for base, _dirs, names in os.walk(root):
        for name in names:
            found.add(os.path.abspath(os.path.join(base, name)))
    return found


def _run_tool(args: list[str], dest: str, *, timeout: float, cancel: Optional[Callable[[], bool]]) -> None:
    with tempfile.TemporaryFile(mode="w+", encoding="utf-8", errors="replace") as errors:
        proc = subprocess.Popen(
            args, cwd=dest, stdout=subprocess.DEVNULL, stderr=errors, text=True,
            creationflags=_CREATE_NO_WINDOW,
        )
        deadline = time.monotonic() + max(5.0, float(timeout))
        try:
            while proc.poll() is None:
                if cancel is not None and cancel():
                    proc.kill()
                    proc.wait()
                    raise InterruptedError("cancelled")
                if time.monotonic() >= deadline:
                    proc.kill()
                    proc.wait()
                    raise ArchiveError("The archive extraction timed out")
                time.sleep(0.1)
            proc.wait()
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()
        if proc.returncode != 0:
            errors.seek(0)
            message = errors.read().strip().splitlines()
            raise ArchiveError(message[-1][:400] if message else "The archive extractor failed")


def extract_archive(path: str, dest: str, *, timeout: float = _ARCHIVE_TIMEOUT,
                    cancel: Optional[Callable[[], bool]] = None) -> list[str]:
    if not os.path.isfile(path):
        raise ArchiveError(f"The archive was not found: {path}")
    os.makedirs(dest, exist_ok=True)
    suffix = _suffix(path)
    if suffix == ".zip":
        try:
            return _zip_extract(path, dest)
        except _UnsafeArchive:
            raise
        except ArchiveError as exc:
            logger.debug("Python zip extraction failed for %s: %s", path, exc)
    errors: list[str] = []
    before = _files_under(dest)
    for kind, tool in _tool_candidates(suffix):
        if cancel is not None and cancel():
            raise InterruptedError("cancelled")
        try:
            _run_tool(_command(kind, tool, path, dest), dest, timeout=timeout, cancel=cancel)
        except InterruptedError:
            raise
        except (OSError, ArchiveError) as exc:
            errors.append(f"{kind}: {exc}")
            continue
        after = _files_under(dest)
        extracted = sorted(after - before)
        if extracted:
            return extracted
        errors.append(f"{kind}: extracted no files")
    detail = "; ".join(errors) if errors else "no supported extractor is available"
    raise ArchiveError(f"Could not extract {os.path.basename(path)} ({detail})")


__all__ = ["ArchiveError", "extract_archive", "looks_like_archive"]
