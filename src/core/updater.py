from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import urllib.error
import urllib.request
import zipfile
import zlib
from concurrent.futures import FIRST_EXCEPTION, ThreadPoolExecutor, wait
from contextlib import suppress

from src.core import system as host
from src.core.config import APP_NAME, IS_CANARY, VERSION_TAG

logger = logging.getLogger(__name__)

_REPO = "TheJustRun/PiraChest"
_API_URL = f"https://api.github.com/repos/{_REPO}/releases/latest"
_API_URL_ALL = f"https://api.github.com/repos/{_REPO}/releases?per_page=100"
_HEADERS = {"Accept": "application/vnd.github+json", "User-Agent": f"{APP_NAME}-Updater"}
_HASH_RE = re.compile(r"^[0-9a-f]{64}$")
_ZIP_RE = re.compile(r"^pirachest.*windows.*x64\.zip$", re.I)
_CANARY_RE = re.compile(r"canary|nightly|unstable|experimental", re.I)
_MANIFEST_NAME = "update-manifest.json"
_CACHE: dict[str, tuple[float, object]] = {}
_CACHE_TTL = 300.0


def _exe_name() -> str:
    if getattr(sys, "frozen", False):
        return os.path.basename(sys.executable)
    return "PiraChest.exe" if host.WINDOWS else "PiraChest"


def _format_of(name: str) -> str:
    low = name.lower()
    if low.endswith(".zip"):
        return "zip"
    if low.endswith((".tar.gz", ".tgz")):
        return "tar.gz"
    if low.endswith((".tar.xz", ".txz")):
        return "tar.xz"
    if low.endswith((".tar.bz2", ".tbz2")):
        return "tar.bz2"
    return ""


def _app_dir() -> str:
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


class UpdateCancelled(Exception):
    pass


class ChecksumMismatch(RuntimeError):
    pass


def _parse_version(tag: str) -> tuple:
    text = re.sub(r"\(.*?\)", " ", str(tag or "")).strip().lower()
    text = (text.split() or [""])[0].lstrip("v")
    core, _, pre = text.partition("-")
    numbers = []
    for part in core.split("+")[0].split(".")[:3]:
        match = re.match(r"\d+", part)
        numbers.append(int(match.group()) if match else 0)
    while len(numbers) < 3:
        numbers.append(0)
    pre = pre.split("+")[0]
    if not pre:
        return (numbers[0], numbers[1], numbers[2], 4, 0)
    label = (re.match(r"[a-z]+", pre) or re.match(r"", pre)).group()
    stage = {"dev": 0, "canary": 0, "nightly": 0, "alpha": 1, "a": 1,
             "beta": 2, "b": 2, "pre": 2, "preview": 2, "rc": 3,
             "stable": 4, "release": 4, "ga": 4}.get(label, 2)
    match = re.match(r"[a-z]*[.\-_]?(\d+)", pre)
    return (numbers[0], numbers[1], numbers[2], stage, int(match.group(1)) if match else 0)


def _get_json(url: str):
    now = time.monotonic()
    hit = _CACHE.get(url)
    if hit is not None and now - hit[0] < _CACHE_TTL:
        return hit[1]
    for attempt in range(3):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=_HEADERS), timeout=10) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            break
        except urllib.error.HTTPError as exc:
            if exc.code in (403, 404, 429) or attempt == 2:
                raise
            time.sleep(0.6 * (attempt + 1))
        except (urllib.error.URLError, TimeoutError, OSError, ValueError):
            if attempt == 2:
                raise
            time.sleep(0.6 * (attempt + 1))
    _CACHE[url] = (now, data)
    return data


def _release_is_canary(data: dict) -> bool:
    if _CANARY_RE.search(str(data.get("tag_name") or "")):
        return True
    return any(_CANARY_RE.search(str(a.get("name") or "")) for a in (data.get("assets") or []) if isinstance(a, dict))


def _fetch_releases(include_prereleases: bool) -> list[dict]:
    if not include_prereleases:
        data = _get_json(_API_URL)
        return [data] if isinstance(data, dict) else []
    data = _get_json(_API_URL_ALL)
    if not isinstance(data, list):
        return []
    releases = [
        r for r in data
        if isinstance(r, dict) and not r.get("draft") and r.get("tag_name")
    ]
    releases.sort(key=lambda r: _parse_version(r.get("tag_name", "")), reverse=True)
    return releases


def _archive_assets(assets: list) -> list[dict]:
    return [
        a for a in assets
        if isinstance(a, dict) and _format_of(str(a.get("name") or ""))
        and "sha256" not in str(a.get("name") or "").lower()
        and "checksum" not in str(a.get("name") or "").lower()
    ]


def _manifest_choice(manifest: dict, assets: list, canary: bool) -> dict | None:
    entries = manifest.get("assets") if isinstance(manifest, dict) else None
    if not isinstance(entries, list):
        return None
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        if str(entry.get("os") or "").lower() != host.os_key():
            continue
        entry_arch = str(entry.get("arch") or "").lower()
        if entry_arch and entry_arch != host.arch():
            continue
        name = str(entry.get("name") or "")
        asset = next((a for a in assets if a.get("name") == name), None)
        if asset is None:
            continue
        if canary and not _CANARY_RE.search(name):
            continue
        if not canary and _CANARY_RE.search(name):
            continue
        return {
            "asset": asset,
            "format": str(entry.get("format") or _format_of(name)),
            "kind": str(entry.get("kind") or ""),
            "sha256": str(entry.get("sha256") or "").lower(),
        }
    return None


def _pick_asset(assets: list, canary: bool) -> dict | None:
    manifest_asset = next(
        (a for a in assets if isinstance(a, dict) and a.get("name") == _MANIFEST_NAME),
        None,
    )
    if manifest_asset is not None:
        try:
            manifest = _get_json(manifest_asset["browser_download_url"])
            chosen = _manifest_choice(manifest, assets, canary)
            if chosen is not None:
                return chosen
        except Exception as exc:
            logger.info("Update manifest unavailable: %s", exc)
    candidates = _archive_assets(assets)
    if canary:
        candidates = [a for a in candidates if _CANARY_RE.search(a.get("name", ""))]
    else:
        plain = [a for a in candidates if not _CANARY_RE.search(a.get("name", ""))]
        candidates = plain or candidates
    if not candidates:
        return None
    target_os, target_arch = host.os_key(), host.arch()
    matched = [a for a in candidates if host.name_matches_platform(a.get("name", ""), target_os, target_arch)]
    if host.WINDOWS:
        preferred = [a for a in matched if _ZIP_RE.match(a.get("name", ""))]
        matched = preferred or matched
        if not matched:
            loose = [a for a in candidates if "windows" in a.get("name", "").lower() or "win" in a.get("name", "").lower()]
            matched = loose
    if not matched:
        return None
    asset = matched[0]
    name = str(asset.get("name") or "")
    return {
        "asset": asset,
        "format": _format_of(name),
        "kind": "bundle" if host.MACOS else "onedir",
        "sha256": "",
    }


def _tag_label(tag: str) -> str:
    text = str(tag or "").strip().lstrip("vV")
    core, _, rest = text.partition("-")
    parts = [part for part in rest.split("-") if part]
    channel = parts[0].lower() if parts and parts[0].lower() in (
        "stable", "canary", "beta", "alpha", "rc", "preview", "nightly") else ""
    tail = parts[1:]
    build = tail.pop(0) if tail and tail[0].isdigit() else ""
    nick = " ".join(tail).title()
    label = f"{core}-{channel}" if channel else core
    if build:
        label = f"{label}-{build}"
    return f"{label} ({nick})" if nick else label


def _checksum_asset(assets: list, asset_name: str) -> dict | None:
    names = (f"{asset_name}.sha256", "SHA256SUMS", "checksums.txt", "SHA256SUMS.txt")
    for name in names:
        found = next((a for a in assets if isinstance(a, dict) and a.get("name") == name), None)
        if found:
            return found
    return None


def _is_choco_install() -> bool:
    if not host.WINDOWS or not getattr(sys, "frozen", False):
        return False
    app = _app_dir()
    if os.path.isfile(os.path.join(app, ".pirachest-choco")):
        return True
    return "\\chocolatey\\lib\\" in app.lower()


def check_for_update(include_prereleases: bool = True, channel: str | None = None) -> dict | None:
    if _is_choco_install():
        return None
    channel = (channel or ("auto" if IS_CANARY else "stable")).lower()
    if channel not in ("auto", "stable", "canary"):
        channel = "stable"
    try:
        for data in _fetch_releases(include_prereleases):
            tag = str(data.get("tag_name") or "")
            if not tag:
                continue
            canary = _release_is_canary(data)
            if channel == "canary" and not canary:
                continue
            if channel == "stable" and canary:
                continue
            if _parse_version(tag) <= _parse_version(VERSION_TAG):
                break
            assets = data.get("assets") or []
            picked = _pick_asset(assets, canary)
            if picked is None:
                continue
            asset = picked["asset"]
            digest = str(picked.get("sha256") or asset.get("digest") or "")
            digest = digest.split(":", 1)[1].lower() if digest.lower().startswith("sha256:") else digest.lower()
            if not _HASH_RE.match(digest):
                digest = ""
            checksum = _checksum_asset(assets, asset["name"])
            if not digest and checksum is None:
                logger.warning("Release %s has no SHA-256 checksum; skipping", tag)
                continue
            return {
                "tag": tag,
                "label": _tag_label(tag),
                "version": _parse_version(tag),
                "download_url": asset["browser_download_url"],
                "asset_name": asset["name"],
                "asset_size": int(asset.get("size") or 0),
                "checksum_url": checksum["browser_download_url"] if checksum else "",
                "checksum_sha256": digest,
                "format": picked.get("format") or _format_of(asset["name"]),
                "kind": picked.get("kind") or ("bundle" if host.MACOS else "onedir"),
                "os": host.os_key(),
                "arch": host.arch(),
                "notes": data.get("body", ""),
                "prerelease": bool(data.get("prerelease")),
                "canary": canary,
                "channel": "canary" if canary else "stable",
            }
    except Exception as exc:
        logger.warning("Update check failed: %s", exc)
    return None


def _update_staging_dir() -> str:
    try:
        from src.core.config import paths
        base = os.path.join(paths.app_data_dir, "update")
    except Exception:
        base = os.path.join(tempfile.gettempdir(), "PiraChest-update")
    os.makedirs(base, exist_ok=True)
    return base


def cleanup_update_staging(_unused: str | None = None) -> None:
    try:
        shutil.rmtree(_update_staging_dir(), ignore_errors=True)
    except Exception:
        logger.warning("Failed to clean up update staging directory", exc_info=True)


def fetch_expected_checksum(checksum_url: str, asset_name: str) -> str | None:
    if not checksum_url or not asset_name:
        return None
    req = urllib.request.Request(checksum_url, headers={"User-Agent": "PiraChest-Updater"})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            text = resp.read().decode("utf-8-sig", errors="replace")
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        logger.warning("Failed to fetch checksum file: %s", exc)
        return None
    for line in text.splitlines():
        parts = line.strip().split()
        if len(parts) == 1:
            value = parts[0].lower()
            if _HASH_RE.match(value):
                return value
        elif len(parts) >= 2:
            name = parts[-1].lstrip("*")
            if name == asset_name or os.path.basename(name) == asset_name:
                value = parts[0].lower()
                if _HASH_RE.match(value):
                    return value
    return None


def _download(url: str, dest: str, expected_sha256: str | None, on_progress, is_cancelled) -> None:
    req = urllib.request.Request(url, headers={"User-Agent": "PiraChest-Updater", "Accept-Encoding": "identity"})
    hasher = hashlib.sha256() if expected_sha256 else None
    downloaded = 0
    last_emit = 0.0
    view = memoryview(bytearray(4 << 20))
    try:
        with urllib.request.urlopen(req, timeout=60) as resp, open(dest, "wb", buffering=8 << 20) as fh:
            total = int(resp.headers.get("Content-Length", 0))
            while True:
                if is_cancelled is not None and is_cancelled():
                    raise UpdateCancelled()
                n = resp.readinto(view)
                if not n:
                    break
                chunk = view[:n]
                fh.write(chunk)
                if hasher:
                    hasher.update(chunk)
                downloaded += n
                if on_progress and total > 0:
                    now = time.monotonic()
                    if now - last_emit >= 0.05 or downloaded == total:
                        last_emit = now
                        try:
                            on_progress(downloaded, total)
                        except Exception:
                            logger.exception("Update progress callback raised")
        if total > 0 and downloaded != total:
            raise RuntimeError(f"Download incomplete: got {downloaded} bytes, expected {total}")
        if hasher and hasher.hexdigest().lower() != expected_sha256.lower():
            raise ChecksumMismatch("Downloaded file failed checksum verification")
    except BaseException:
        try:
            os.remove(dest)
        except OSError:
            pass
        raise


_SEG_MIN = 8 << 20
_BASE_HEADERS = {"User-Agent": "PiraChest-Updater", "Accept-Encoding": "identity"}


def _probe_ranges(url: str) -> tuple[int, bool]:
    req = urllib.request.Request(url, headers={**_BASE_HEADERS, "Range": "bytes=0-0"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        resp.read(1)
        content_range = resp.headers.get("Content-Range", "")
        if getattr(resp, "status", 200) == 206 and "/" in content_range:
            return int(content_range.rsplit("/", 1)[1]), True
        return int(resp.headers.get("Content-Length", 0) or 0), False


def _sha256_file(path: str, is_cancelled) -> str:
    hasher = hashlib.sha256()
    with open(path, "rb", buffering=0) as fh:
        buf = bytearray(4 << 20)
        view = memoryview(buf)
        while True:
            if is_cancelled is not None and is_cancelled():
                raise UpdateCancelled()
            n = fh.readinto(buf)
            if not n:
                break
            hasher.update(view[:n])
    return hasher.hexdigest()


def _download_parallel(url: str, dest: str, total: int, expected: str | None, on_progress, is_cancelled, on_stage) -> None:
    part = dest + ".part"
    streams = max(3, min(8, (os.cpu_count() or 4)))
    size = -(-total // streams)
    ranges = [(i * size, min(total, (i + 1) * size) - 1) for i in range(streams) if i * size < total]
    lock = threading.Lock()
    abort = threading.Event()
    state = {"done": 0, "emit": 0.0}

    def cancelled() -> bool:
        return abort.is_set() or (is_cancelled is not None and is_cancelled())

    def tick(n: int) -> None:
        with lock:
            state["done"] += n
            now = time.monotonic()
            if now - state["emit"] < 0.05 and state["done"] < total:
                return
            state["emit"] = now
            done = state["done"]
            if on_progress:
                try:
                    on_progress(done, total)
                except Exception:
                    logger.exception("Update progress callback raised")

    def segment(start: int, end: int) -> None:
        pos = start
        attempts = 0
        with open(part, "r+b", buffering=0) as fh:
            while pos <= end:
                if cancelled():
                    raise UpdateCancelled()
                try:
                    req = urllib.request.Request(url, headers={**_BASE_HEADERS, "Range": f"bytes={pos}-{end}"})
                    with urllib.request.urlopen(req, timeout=30) as resp:
                        if getattr(resp, "status", 206) != 206:
                            raise RuntimeError("Server ignored the range request")
                        fh.seek(pos)
                        while pos <= end:
                            if cancelled():
                                raise UpdateCancelled()
                            chunk = resp.read(min(1 << 20, end - pos + 1))
                            if not chunk:
                                break
                            fh.write(chunk)
                            pos += len(chunk)
                            tick(len(chunk))
                    if pos <= end:
                        raise RuntimeError("Connection closed early")
                except UpdateCancelled:
                    raise
                except Exception:
                    attempts += 1
                    if attempts >= 4:
                        raise
                    time.sleep(min(2.0, 0.5 * attempts))

    try:
        with open(part, "wb") as fh:
            fh.truncate(total)
        with ThreadPoolExecutor(len(ranges)) as pool:
            futures = [pool.submit(segment, a, b) for a, b in ranges]
            done, _pending = wait(futures, return_when=FIRST_EXCEPTION)
            failed = [f.exception() for f in done if f.exception()]
            if failed:
                abort.set()
                real = next((e for e in failed if not isinstance(e, UpdateCancelled)), failed[0])
                raise real
            for f in futures:
                f.result()
        if on_progress:
            on_progress(total, total)
        if expected:
            if on_stage:
                on_stage("verify")
            if _sha256_file(part, is_cancelled).lower() != expected.lower():
                raise ChecksumMismatch("Downloaded file failed checksum verification")
        os.replace(part, dest)
    except BaseException:
        try:
            os.remove(part)
        except OSError:
            pass
        raise


def _fetch_archive(url: str, dest: str, expected: str | None, on_progress, is_cancelled, on_stage) -> None:
    total, ranged = 0, False
    try:
        total, ranged = _probe_ranges(url)
    except Exception as exc:
        logger.info("Range probe failed, using single stream: %s", exc)
    if ranged and total >= _SEG_MIN:
        try:
            _download_parallel(url, dest, total, expected, on_progress, is_cancelled, on_stage)
            return
        except (UpdateCancelled, ChecksumMismatch):
            raise
        except Exception as exc:
            logger.warning("Parallel download failed (%s); falling back to single stream", exc)
    _download_with_retries(url, dest, expected, on_progress, is_cancelled)
    if on_stage:
        on_stage("verify")


def _download_with_retries(url: str, dest: str, expected: str | None, on_progress, is_cancelled) -> None:
    for attempt in range(3):
        try:
            _download(url, dest, expected, on_progress, is_cancelled)
            return
        except (UpdateCancelled, ChecksumMismatch):
            raise
        except Exception as exc:
            if attempt >= 2:
                raise
            logger.warning("Update download attempt %s failed: %s", attempt + 1, exc)
            time.sleep(1.0 + attempt)


def _unchanged(path: str, info: zipfile.ZipInfo) -> bool:
    try:
        if os.stat(path).st_size != info.file_size:
            return False
        crc = 0
        with open(path, "rb") as fh:
            while chunk := fh.read(1 << 20):
                crc = zlib.crc32(chunk, crc)
        return crc == info.CRC
    except OSError:
        return False


def _safe_name(name: str) -> str:
    name = (name or "").replace("\\", "/").strip("/")
    if not name or name.startswith("/") or re.match(r"^[a-zA-Z]:", name) or ".." in name.split("/"):
        raise RuntimeError("Update archive contains an unsafe path")
    return name


def _link_target(name: str, target: str) -> str:
    target = (target or "").replace("\\", "/")
    base = name.rsplit("/", 1)[0] if "/" in name else ""
    resolved = os.path.normpath(os.path.join(base, target)).replace("\\", "/")
    if not target or target.startswith("/") or re.match(r"^[a-zA-Z]:", target) or resolved.startswith(".."):
        raise RuntimeError("Update archive contains an unsafe symbolic link")
    return target


def _open_archive(path: str):
    if _format_of(os.path.basename(path)) == "zip":
        handle = zipfile.ZipFile(path)
        entries = []
        for info in handle.infolist():
            mode = (info.external_attr >> 16) & 0o777
            link = ""
            if (info.external_attr >> 16) & 0o170000 == 0o120000:
                link = _link_target(_safe_name(info.filename), handle.read(info).decode("utf-8", "replace"))
            entries.append((_safe_name(info.filename), info.file_size, info.is_dir(), mode, info, link))
        return "zip", handle, entries
    handle = tarfile.open(path, "r:*")
    entries = []
    for member in handle.getmembers():
        link = ""
        if member.issym() or member.islnk():
            link = _link_target(_safe_name(member.name), member.linkname)
        entries.append((_safe_name(member.name), member.size, member.isdir(), member.mode & 0o777, member, link))
    return "tar", handle, entries


def _extract_one(kind: str, handle, ref, target: str, mode: int, link: str = "") -> None:
    os.makedirs(os.path.dirname(target) or ".", exist_ok=True)
    if link:
        if not host.WINDOWS:
            with suppress(OSError):
                os.remove(target)
            os.symlink(link, target)
        return
    if kind == "zip":
        with handle.open(ref) as src, open(target, "wb", buffering=1 << 20) as dst:
            shutil.copyfileobj(src, dst, 1 << 20)
    else:
        src = handle.extractfile(ref)
        if src is None:
            return
        with src, open(target, "wb", buffering=1 << 20) as dst:
            shutil.copyfileobj(src, dst, 1 << 20)
    if mode and not host.WINDOWS:
        try:
            os.chmod(target, mode | 0o600)
        except OSError:
            pass


def _archive_prefix(files: list, exe_name: str, bundle: bool) -> str:
    if bundle:
        for name, *_rest in files:
            parts = name.split("/")
            for index, part in enumerate(parts[:-1]):
                if part.endswith(".app"):
                    return "/".join(parts[: index + 1]) + "/"
        raise RuntimeError("Application bundle not found in update archive")
    for name, *_rest in files:
        if name.rsplit("/", 1)[-1] == exe_name:
            head = name.rsplit("/", 1)[0]
            return head + "/" if head else ""
    raise RuntimeError(f"{exe_name} not found in update archive")


def _stale_files(install_dir: str, keep: set[str], skip_data: bool = True) -> list[str]:
    stale = []
    for current, dirs, files in os.walk(install_dir):
        if skip_data and os.path.abspath(current) == os.path.abspath(install_dir):
            dirs[:] = [d for d in dirs if d.lower() != "data"]
        dirs[:] = [d for d in dirs if d != "__pycache__"]
        for name in files:
            if name.endswith(".pyc"):
                continue
            rel = os.path.relpath(os.path.join(current, name), install_dir).replace("\\", "/")
            if rel.lower() not in keep:
                stale.append(rel)
    return stale


def _stage_delta(archive_path: str, out_dir: str, exe_name: str, install_dir: str | None,
                 is_cancelled, kind: str = "onedir") -> None:
    bundle = kind == "bundle"
    archive_kind, handle, entries = _open_archive(archive_path)
    try:
        files = [entry for entry in entries if not entry[2]]
        prefix = _archive_prefix(files, exe_name, bundle)
        exe_lower = exe_name.lower()
        items = []
        for name, size, _is_dir, mode, ref, link in files:
            if not name.startswith(prefix):
                continue
            rel = name[len(prefix):]
            if not rel or rel.split("/", 1)[0].lower() == "__macosx":
                continue
            if not bundle and rel.split("/", 1)[0].lower() == "data":
                continue
            items.append((rel, size, mode, ref, link))
        if install_dir is None:
            changed = items
        else:
            changed = []
            for rel, size, mode, ref, link in items:
                if is_cancelled is not None and is_cancelled():
                    raise UpdateCancelled()
                if link or rel.lower() == exe_lower or archive_kind != "zip":
                    changed.append((rel, size, mode, ref, link))
                    continue
                if not _unchanged(os.path.join(install_dir, *rel.split("/")), ref):
                    changed.append((rel, size, mode, ref, link))
        os.makedirs(out_dir, exist_ok=True)
        for rel, _size, mode, ref, link in changed:
            if is_cancelled is not None and is_cancelled():
                raise UpdateCancelled()
            _extract_one(archive_kind, handle, ref, os.path.join(out_dir, *rel.split("/")), mode, link)
    finally:
        handle.close()
    if install_dir is not None:
        keep = {rel.lower() for rel, *_rest in items}
        stale = _stale_files(install_dir, keep, skip_data=not bundle)
        sep = "\\" if host.WINDOWS else "/"
        with open(os.path.join(os.path.dirname(out_dir), "delete.lst"), "w", encoding="utf-8", newline="\n") as fh:
            fh.write("".join(f"{path.replace('/', sep)}\n" for path in stale))


def _install_root() -> str:
    exe = os.path.abspath(sys.executable)
    if host.MACOS:
        parts = exe.split(os.sep)
        for index in range(len(parts) - 1, 0, -1):
            if parts[index].endswith(".app"):
                return os.sep.join(parts[: index + 1])
    return os.path.dirname(exe)


def download_and_verify_update(update_info: dict, on_progress=None, is_cancelled=None, on_stage=None) -> str:
    expected = str(update_info.get("checksum_sha256") or "").strip().lower()
    if not _HASH_RE.match(expected):
        checksum_url = update_info.get("checksum_url")
        if not checksum_url:
            raise RuntimeError("Release is missing its SHA-256 checksum")
        expected = fetch_expected_checksum(checksum_url, update_info.get("asset_name", ""))
    if not expected:
        raise RuntimeError("Could not verify the update checksum")
    staging = _update_staging_dir()
    need = int(update_info.get("asset_size") or 0) * 3
    try:
        if need and shutil.disk_usage(staging).free < need:
            raise RuntimeError("Not enough free disk space to download the update")
    except OSError:
        pass
    fmt = str(update_info.get("format") or _format_of(str(update_info.get("asset_name") or "")))
    suffix = {"zip": ".zip", "tar.xz": ".tar.xz", "tar.bz2": ".tar.bz2"}.get(fmt, ".tar.gz")
    archive_path = os.path.join(staging, f"update{suffix}")
    out_dir = os.path.join(staging, "new")
    shutil.rmtree(out_dir, ignore_errors=True)
    frozen = bool(getattr(sys, "frozen", False))
    try:
        if on_stage:
            on_stage("download")
        _fetch_archive(update_info["download_url"], archive_path, expected, on_progress, is_cancelled, on_stage)
        if on_stage:
            on_stage("extract")
        _stage_delta(archive_path, out_dir, _exe_name(), _install_root() if frozen else None,
                     is_cancelled, str(update_info.get("kind") or "onedir"))
    finally:
        try:
            os.remove(archive_path)
        except OSError:
            pass
    return out_dir


def _can_write(path: str) -> bool:
    probe = os.path.join(path, f".pirachest-write-{os.getpid()}")
    try:
        with open(probe, "w", encoding="utf-8") as fh:
            fh.write("ok")
        os.remove(probe)
        return True
    except OSError:
        return False


_UPDATE_SCRIPT = """@echo off
setlocal
chcp 65001 >NUL
set "SRC=%PIRACHEST_UPDATE_SRC%"
set "DST=%PIRACHEST_UPDATE_DST%"
set "STAGE=%PIRACHEST_UPDATE_STAGE%"
set "LOG=%PIRACHEST_UPDATE_LOG%"
set "EXE=%PIRACHEST_UPDATE_EXE%"
set "NAME=%PIRACHEST_UPDATE_NAME%"
set "PID=%PIRACHEST_UPDATE_PID%"
set "DEL=%PIRACHEST_UPDATE_DEL%"
set "TASKS=%LOG%.tasks"
set "FAILED="
del /q "%LOG%" >NUL 2>NUL
set /a WAIT=0

:waitloop
tasklist /FI "PID eq %PID%" /NH /FO CSV >"%TASKS%" 2>NUL
find /I "%NAME%" "%TASKS%" >NUL 2>NUL
if errorlevel 1 goto apply
set /a WAIT+=1 >NUL
if %WAIT% GEQ 480 goto apply
ping -n 1 -w 250 192.0.2.1 >NUL 2>NUL
goto waitloop

:apply
del /q "%TASKS%" >NUL 2>NUL
robocopy "%SRC%" "%DST%" /E /IS /MT:16 /R:10 /W:1 /NFL /NDL /NJH /NJS /NP /XD "%DST%\\data" "%SRC%\\data" >NUL 2>&1
if errorlevel 8 set FAILED=1
if defined FAILED goto done
if exist "%DEL%" for /f "usebackq tokens=* delims=" %%F in ("%DEL%") do del /f /q "%DST%\\%%F" >NUL 2>NUL

:done
if defined FAILED (
    echo Update copy failed. > "%LOG%"
) else (
    rmdir /S /Q "%STAGE%" >NUL 2>NUL
)
pushd "%DST%" 2>NUL
start "" "%EXE%"
popd 2>NUL
(goto) 2>nul & del "%~f0"
"""


_POSIX_UPDATE_SCRIPT = """#!/bin/sh
PID="$1"
SRC="$2"
DST="$3"
STAGE="$4"
EXE="$5"
DEL="$6"
KIND="$7"
i=0
while kill -0 "$PID" 2>/dev/null; do
  i=$((i + 1))
  if [ "$i" -ge 1200 ]; then break; fi
  sleep 0.25
done
if [ "$KIND" = "bundle" ]; then
  ditto "$SRC" "$DST" || exit 1
else
  cp -Rf "$SRC"/. "$DST"/ || exit 1
fi
if [ -f "$DEL" ]; then
  while IFS= read -r f; do
    if [ -n "$f" ]; then rm -f "$DST/$f"; fi
  done < "$DEL"
fi
chmod +x "$EXE" 2>/dev/null || true
rm -rf "$STAGE"
if [ "$KIND" = "bundle" ]; then
  codesign --force --deep --sign - "$DST" >/dev/null 2>&1 || true
  open "$DST" >/dev/null 2>&1
else
  cd "$(dirname "$EXE")" 2>/dev/null || cd /
  "$EXE" >/dev/null 2>&1 &
fi
"""


def _apply_update_posix(new_app_dir: str) -> None:
    current_exe = os.path.abspath(sys.executable)
    exe_name = os.path.basename(current_exe)
    install_root = _install_root()
    new_app_dir = os.path.abspath(new_app_dir)
    kind = "bundle" if host.MACOS and install_root.endswith(".app") else "onedir"
    marker = os.path.join(new_app_dir, "Contents", "MacOS", exe_name) if kind == "bundle" else os.path.join(new_app_dir, exe_name)
    if not os.path.isfile(marker):
        raise RuntimeError("The downloaded update is incomplete")
    if not _can_write(install_root):
        raise RuntimeError("PiraChest cannot write to its install folder. Move the app somewhere writable or reinstall it.")
    staging = _update_staging_dir()
    temp = tempfile.gettempdir()
    script_path = os.path.join(temp, "pirachest_update.sh")
    with open(script_path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(_POSIX_UPDATE_SCRIPT)
    try:
        os.chmod(script_path, 0o700)
    except OSError:
        pass
    env = os.environ.copy()
    env["PYINSTALLER_RESET_ENVIRONMENT"] = "1"
    subprocess.Popen(
        ["/bin/sh", script_path, str(os.getpid()), new_app_dir, install_root, staging,
         current_exe, os.path.join(staging, "delete.lst"), kind],
        cwd=temp,
        env=env,
        start_new_session=True,
        close_fds=True,
    )


def apply_update_and_restart(new_app_dir: str) -> None:
    if not getattr(sys, "frozen", False):
        raise RuntimeError("Self-update requires a frozen (built) executable")
    if not host.WINDOWS:
        _apply_update_posix(new_app_dir)
        return
    current_exe = os.path.abspath(sys.executable)
    exe_name = os.path.basename(current_exe)
    install_dir = os.path.dirname(current_exe)
    new_app_dir = os.path.abspath(new_app_dir)
    if not os.path.isfile(os.path.join(new_app_dir, exe_name)):
        raise RuntimeError("The downloaded update is incomplete")
    if not _can_write(install_dir):
        raise RuntimeError("PiraChest cannot write to its install folder. Run it as administrator or reinstall through Chocolatey.")
    staging = _update_staging_dir()
    temp = tempfile.gettempdir()
    script_path = os.path.join(temp, "pirachest_update.bat")
    with open(script_path, "w", encoding="ascii", newline="\r\n") as fh:
        fh.write(_UPDATE_SCRIPT)
    env = os.environ.copy()
    env.update({
        "PIRACHEST_UPDATE_SRC": new_app_dir,
        "PIRACHEST_UPDATE_DST": install_dir,
        "PIRACHEST_UPDATE_STAGE": staging,
        "PIRACHEST_UPDATE_LOG": os.path.join(temp, "pirachest_update_error.log"),
        "PIRACHEST_UPDATE_EXE": current_exe,
        "PIRACHEST_UPDATE_NAME": exe_name,
        "PIRACHEST_UPDATE_PID": str(os.getpid()),
        "PIRACHEST_UPDATE_DEL": os.path.join(staging, "delete.lst"),
        "PYINSTALLER_RESET_ENVIRONMENT": "1",
    })
    subprocess.Popen(
        ["cmd.exe", "/d", "/c", "pirachest_update.bat"],
        cwd=temp,
        creationflags=subprocess.CREATE_NO_WINDOW,
        close_fds=True,
        env=env,
    )
