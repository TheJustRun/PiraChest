from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
import zipfile
import zlib
from concurrent.futures import FIRST_EXCEPTION, ThreadPoolExecutor, wait

from src.core.config import APP_NAME, IS_CANARY, VERSION

logger = logging.getLogger(__name__)

_REPO = "TheJustRun/PiraChest"
_API_URL = f"https://api.github.com/repos/{_REPO}/releases/latest"
_API_URL_ALL = f"https://api.github.com/repos/{_REPO}/releases?per_page=100"
_HEADERS = {"Accept": "application/vnd.github+json", "User-Agent": f"{APP_NAME}-Updater"}
_HASH_RE = re.compile(r"^[0-9a-f]{64}$")
_ZIP_RE = re.compile(r"^pirachest.*windows.*x64\.zip$", re.I)
_EXE_RE = re.compile(r"^pirachest(?:[ -]canary)?\.exe$", re.I)
_CANARY_RE = re.compile(r"canary|nightly|unstable|experimental", re.I)
_CACHE: dict[str, tuple[float, object]] = {}
_CACHE_TTL = 300.0


def _app_dir() -> str:
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


class UpdateCancelled(Exception):    pass


class ChecksumMismatch(RuntimeError):
    pass


def _parse_version(tag: str) -> tuple:
    text = str(tag or "").strip().lower().lstrip("v")
    core, _, pre = text.partition("-")
    numbers = []
    for part in core.split(".")[:3]:
        match = re.match(r"\d+", part)
        numbers.append(int(match.group()) if match else 0)
    while len(numbers) < 3:
        numbers.append(0)
    if not pre:
        return (numbers[0], numbers[1], numbers[2], 4, 0)
    label = re.split(r"[.\-+_]", pre)[0]
    stage = {"dev": 0, "canary": 0, "nightly": 0, "alpha": 1, "a": 1,
             "beta": 2, "b": 2, "pre": 2, "preview": 2, "rc": 3,
             "stable": 4, "release": 4, "ga": 4}.get(label, 2)
    match = re.search(r"(\d+)", pre)
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


def _pick_zip(assets: list, canary: bool) -> dict | None:
    zips = [
        a for a in assets
        if isinstance(a, dict) and a.get("name", "").lower().endswith(".zip")
        and "sha256" not in a.get("name", "").lower()
    ]
    if canary:
        zips = [a for a in zips if _CANARY_RE.search(a.get("name", ""))]
        if not zips:
            return None
    else:
        plain = [a for a in zips if not _CANARY_RE.search(a.get("name", ""))]
        zips = plain or zips
    preferred = [a for a in zips if _ZIP_RE.match(a.get("name", ""))]
    if preferred:
        return preferred[0]
    windows = [a for a in zips if "windows" in a.get("name", "").lower() or "win" in a.get("name", "").lower()]
    if windows:
        return windows[0]
    return zips[0] if len(zips) == 1 else None


def _checksum_asset(assets: list, asset_name: str) -> dict | None:
    names = (f"{asset_name}.sha256", "SHA256SUMS", "checksums.txt", "SHA256SUMS.txt")
    for name in names:
        found = next((a for a in assets if isinstance(a, dict) and a.get("name") == name), None)
        if found:
            return found
    return None


def _is_choco_install() -> bool:
    if not getattr(sys, "frozen", False):
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
            if _parse_version(tag) <= _parse_version(VERSION):
                break
            asset = _pick_zip(data.get("assets") or [], canary)
            if asset is None:
                continue
            digest = str(asset.get("digest") or "")
            digest = digest.split(":", 1)[1].lower() if digest.lower().startswith("sha256:") else ""
            checksum = _checksum_asset(data.get("assets") or [], asset["name"])
            if not digest and checksum is None:
                logger.warning("Release %s has no SHA-256 checksum; skipping", tag)
                continue
            return {
                "tag": tag,
                "version": _parse_version(tag),
                "download_url": asset["browser_download_url"],
                "asset_name": asset["name"],
                "asset_size": int(asset.get("size") or 0),
                "checksum_url": checksum["browser_download_url"] if checksum else "",
                "checksum_sha256": digest,
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
            if any(f.exception() for f in done):
                abort.set()
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


def _workers() -> int:
    return max(2, min(8, os.cpu_count() or 4))


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


def _extract_batch(zip_path: str, batch: list, is_cancelled) -> None:
    with zipfile.ZipFile(zip_path) as zf:
        for name, target in batch:
            if is_cancelled is not None and is_cancelled():
                raise UpdateCancelled()
            os.makedirs(os.path.dirname(target), exist_ok=True)
            with zf.open(name) as src, open(target, "wb", buffering=1 << 20) as dst:
                shutil.copyfileobj(src, dst, 1 << 20)


def _partition(items: list, count: int) -> list[list]:
    buckets: list[list] = [[] for _ in range(count)]
    loads = [0] * count
    for item in sorted(items, key=lambda x: x[2], reverse=True):
        i = loads.index(min(loads))
        buckets[i].append((item[0], item[1]))
        loads[i] += item[2] + 1
    return [b for b in buckets if b]


def _stale_files(install_dir: str, keep: set[str]) -> list[str]:
    stale = []
    for current, _dirs, files in os.walk(os.path.join(install_dir, "_internal")):
        for name in files:
            rel = os.path.relpath(os.path.join(current, name), install_dir).replace("\\", "/")
            if rel.lower() not in keep:
                stale.append(rel.replace("/", "\\"))
    return stale


def _stage_delta(zip_path: str, out_dir: str, exe_name: str, install_dir: str | None, is_cancelled) -> None:
    with zipfile.ZipFile(zip_path) as zf:
        infos = zf.infolist()
    exe_lower = exe_name.lower()
    names = []
    for info in infos:
        name = info.filename.replace("\\", "/")
        if not name or name.startswith("/") or re.match(r"^[a-zA-Z]:", name) or ".." in name.split("/"):
            raise RuntimeError("Update archive contains an unsafe path")
        if (info.external_attr >> 16) & 0o170000 == 0o120000:
            raise RuntimeError("Update archive contains a symbolic link")
        names.append(name)
    roots = [n for n in names if not n.endswith("/") and _EXE_RE.match(n.rsplit("/", 1)[-1])]
    if not roots:
        raise RuntimeError(f"{exe_name} not found in update archive")
    root = min(roots, key=len)
    prefix = root[: -len(root.rsplit("/", 1)[-1])]
    files = []
    for info, name in zip(infos, names):
        if info.is_dir() or name.endswith("/") or not name.startswith(prefix):
            continue
        rel = name[len(prefix):]
        if rel.split("/", 1)[0].lower() == "data":
            continue
        if _EXE_RE.match(rel.rsplit("/", 1)[-1]):
            rel = exe_name
        files.append((info, rel))
    if install_dir is None:
        changed = files
    else:
        def differs(item) -> bool:
            if is_cancelled is not None and is_cancelled():
                raise UpdateCancelled()
            info, rel = item
            if rel.lower() == exe_lower:
                return True
            return not _unchanged(os.path.join(install_dir, *rel.split("/")), info)
        with ThreadPoolExecutor(_workers()) as pool:
            flags = list(pool.map(differs, files))
        changed = [item for item, flag in zip(files, flags) if flag]
    os.makedirs(out_dir, exist_ok=True)
    work = [(info.filename, os.path.join(out_dir, *rel.split("/")), info.file_size) for info, rel in changed]
    batches = _partition(work, _workers())
    if batches:
        with ThreadPoolExecutor(len(batches)) as pool:
            for future in [pool.submit(_extract_batch, zip_path, b, is_cancelled) for b in batches]:
                future.result()
    if install_dir is not None:
        keep = {rel.lower() for _info, rel in files}
        with open(os.path.join(os.path.dirname(out_dir), "delete.lst"), "w", encoding="utf-8", newline="\r\n") as fh:
            fh.write("".join(f"{p}\n" for p in _stale_files(install_dir, keep)))


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
    zip_path = os.path.join(staging, "update.zip")
    out_dir = os.path.join(staging, "new")
    shutil.rmtree(out_dir, ignore_errors=True)
    frozen = bool(getattr(sys, "frozen", False))
    exe_name = os.path.basename(sys.executable) if frozen else "PiraChest.exe"
    try:
        if on_stage:
            on_stage("download")
        _fetch_archive(update_info["download_url"], zip_path, expected, on_progress, is_cancelled, on_stage)
        if on_stage:
            on_stage("extract")
        _stage_delta(zip_path, out_dir, exe_name, _app_dir() if frozen else None, is_cancelled)
    finally:
        try:
            os.remove(zip_path)
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
robocopy "%SRC%" "%DST%" /E /IS /MT:16 /R:10 /W:1 /NFL /NDL /NJH /NJS /NP /XD data >NUL 2>&1
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


def apply_update_and_restart(new_app_dir: str) -> None:
    if sys.platform != "win32":
        raise RuntimeError("Self-update is only supported on Windows")
    if not getattr(sys, "frozen", False):
        raise RuntimeError("Self-update requires a frozen (built) executable")
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
