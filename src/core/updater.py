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
import time
import urllib.error
import urllib.request
import zipfile

logger = logging.getLogger(__name__)

_REPO = "TheJustRun/PiraChest"
_API_URL = f"https://api.github.com/repos/{_REPO}/releases/latest"
_API_URL_ALL = f"https://api.github.com/repos/{_REPO}/releases?per_page=100"
_HEADERS = {"Accept": "application/vnd.github+json", "User-Agent": "PiraChest-Updater"}
_HASH_RE = re.compile(r"^[0-9a-f]{64}$")
_ZIP_RE = re.compile(r"^pirachest.*windows.*x64\.zip$", re.I)


def _app_dir() -> str:
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


def _load_version() -> str:
    bases = [getattr(sys, "_MEIPASS", ""), _app_dir(), os.path.dirname(os.path.abspath(__file__))]
    seen: set[str] = set()
    for base in bases:
        if not base:
            continue
        current = os.path.abspath(base)
        for _ in range(3):
            if current in seen:
                break
            seen.add(current)
            try:
                with open(os.path.join(current, "VERSION"), "r", encoding="utf-8") as fh:
                    value = fh.read().strip()
                if value:
                    return value
            except OSError:
                pass
            parent = os.path.dirname(current)
            if parent == current:
                break
            current = parent
    return "0.0.0-dev"


__version__ = _load_version()


class UpdateCancelled(Exception):
    pass


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
    stage = {"dev": 0, "alpha": 1, "a": 1, "beta": 2, "b": 2, "pre": 2, "preview": 2, "rc": 3}.get(label, 2)
    match = re.search(r"(\d+)", pre)
    return (numbers[0], numbers[1], numbers[2], stage, int(match.group(1)) if match else 0)


def _get_json(url: str):
    with urllib.request.urlopen(urllib.request.Request(url, headers=_HEADERS), timeout=8) as resp:
        return json.loads(resp.read().decode("utf-8"))


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


def _pick_zip(assets: list) -> dict | None:
    zips = [
        a for a in assets
        if isinstance(a, dict) and a.get("name", "").lower().endswith(".zip")
        and "sha256" not in a.get("name", "").lower()
    ]
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


def check_for_update(include_prereleases: bool = True) -> dict | None:
    if _is_choco_install():
        return None
    try:
        for data in _fetch_releases(include_prereleases):
            tag = str(data.get("tag_name") or "")
            if not tag or _parse_version(tag) <= _parse_version(__version__):
                break
            asset = _pick_zip(data.get("assets") or [])
            if asset is None:
                continue
            checksum = _checksum_asset(data.get("assets") or [], asset["name"])
            if checksum is None:
                logger.warning("Release %s has no SHA-256 checksum; skipping", tag)
                continue
            return {
                "tag": tag,
                "version": _parse_version(tag),
                "download_url": asset["browser_download_url"],
                "asset_name": asset["name"],
                "asset_size": int(asset.get("size") or 0),
                "checksum_url": checksum["browser_download_url"],
                "notes": data.get("body", ""),
                "prerelease": bool(data.get("prerelease")),
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
    req = urllib.request.Request(url, headers={"User-Agent": "PiraChest-Updater"})
    hasher = hashlib.sha256() if expected_sha256 else None
    downloaded = 0
    last_pct = -1
    try:
        with urllib.request.urlopen(req, timeout=60) as resp, open(dest, "wb", buffering=8 << 20) as fh:
            total = int(resp.headers.get("Content-Length", 0))
            while True:
                if is_cancelled is not None and is_cancelled():
                    raise UpdateCancelled()
                chunk = resp.read(1 << 20)
                if not chunk:
                    break
                fh.write(chunk)
                if hasher:
                    hasher.update(chunk)
                downloaded += len(chunk)
                if on_progress and total > 0:
                    pct = downloaded * 100 // total
                    if pct != last_pct:
                        last_pct = pct
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


def _download_with_retries(url: str, dest: str, expected: str | None, on_progress, is_cancelled) -> None:
    for attempt in range(3):
        try:
            _download(url, dest, expected, on_progress, is_cancelled)
            return
        except UpdateCancelled:
            raise
        except ChecksumMismatch:
            raise
        except Exception as exc:
            if attempt >= 2:
                raise
            logger.warning("Update download attempt %s failed: %s", attempt + 1, exc)
            time.sleep(1.0 + attempt)


def _safe_extract(archive: zipfile.ZipFile, out_dir: str) -> None:
    root = os.path.realpath(out_dir)
    entries = []
    for info in archive.infolist():
        name = info.filename.replace("\\", "/")
        if not name or name.startswith("/") or re.match(r"^[a-zA-Z]:", name):
            raise RuntimeError("Update archive contains an unsafe path")
        target = os.path.realpath(os.path.join(root, name))
        try:
            inside = os.path.commonpath([root, target]) == root
        except ValueError:
            inside = False
        if not inside:
            raise RuntimeError("Update archive escapes the staging directory")
        if (info.external_attr >> 16) & 0o170000 == 0o120000:
            raise RuntimeError("Update archive contains a symbolic link")
        entries.append((info, target))
    os.makedirs(root, exist_ok=True)
    for info, target in entries:
        if info.is_dir() or info.filename.endswith("/"):
            os.makedirs(target, exist_ok=True)
            continue
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with archive.open(info) as src, open(target, "wb") as dst:
            shutil.copyfileobj(src, dst, 1 << 20)


def _find_app_root(extracted: str, exe_name: str) -> str:
    root = os.path.realpath(extracted)
    for current, _dirs, files in os.walk(root):
        if exe_name in files:
            return current
    raise RuntimeError(f"{exe_name} not found in update archive")


def download_and_verify_update(update_info: dict, on_progress=None, is_cancelled=None) -> str:
    checksum_url = update_info.get("checksum_url")
    if not checksum_url:
        raise RuntimeError("Release is missing its SHA-256 checksum")
    expected = fetch_expected_checksum(checksum_url, update_info.get("asset_name", ""))
    if not expected:
        raise RuntimeError("Could not verify the update checksum")
    staging = _update_staging_dir()
    zip_path = os.path.join(staging, "update.zip")
    out_dir = os.path.join(staging, "new")
    shutil.rmtree(out_dir, ignore_errors=True)
    try:
        _download_with_retries(update_info["download_url"], zip_path, expected, on_progress, is_cancelled)
        with zipfile.ZipFile(zip_path) as zf:
            _safe_extract(zf, out_dir)
    finally:
        try:
            os.remove(zip_path)
        except OSError:
            pass
    exe_name = os.path.basename(sys.executable) if getattr(sys, "frozen", False) else "PiraChest.exe"
    return _find_app_root(out_dir, exe_name)


def _can_write(path: str) -> bool:
    probe = os.path.join(path, f".pirachest-write-{os.getpid()}")
    try:
        with open(probe, "w", encoding="utf-8") as fh:
            fh.write("ok")
        os.remove(probe)
        return True
    except OSError:
        return False


def _update_script() -> str:
    return """@echo off
setlocal
set "SRC=%PIRACHEST_UPDATE_SRC%"
set "DST=%PIRACHEST_UPDATE_DST%"
set "STAGE=%PIRACHEST_UPDATE_STAGE%"
set "LOG=%PIRACHEST_UPDATE_LOG%"
set "EXE=%PIRACHEST_UPDATE_EXE%"
set "NAME=%PIRACHEST_UPDATE_NAME%"
set "PID=%PIRACHEST_UPDATE_PID%"
set "TASKS=%LOG%.tasks"
set "FAILED="
set RC_FLAGS=/R:20 /W:1 /NFL /NDL /NJH /NJS /NP /IS
del /q "%LOG%" >NUL 2>NUL
del /q "%TASKS%" >NUL 2>NUL
set /a WAIT=0

:waitloop
tasklist /FI "PID eq %PID%" /NH /FO CSV >"%TASKS%" 2>NUL
find /I "%NAME%" "%TASKS%" >NUL 2>NUL
if errorlevel 1 goto copystart
set /a WAIT+=1 >NUL
if %WAIT% GEQ 120 goto copystart
ping -n 2 127.0.0.1 >NUL 2>NUL
goto waitloop

:copystart
del /q "%TASKS%" >NUL 2>NUL
robocopy "%SRC%" "%DST%" /E /XD _internal data %RC_FLAGS% >NUL 2>&1
set RC1=%ERRORLEVEL%
set RC2=0
if %RC1% GEQ 8 goto copyfailed
if not exist "%SRC%\\_internal" goto internaldone
robocopy "%SRC%\\_internal" "%DST%\\_internal" /MIR %RC_FLAGS% >NUL 2>&1
set RC2=%ERRORLEVEL%
goto internaldone

:copyfailed
set FAILED=1
goto finish

:internaldone
if %RC2% GEQ 8 set FAILED=1
:finish
if defined FAILED (
    echo Update copy failed ^(robocopy codes %RC1%/%RC2%^). > "%LOG%"
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
    log_path = os.path.join(tempfile.gettempdir(), "pirachest_update_error.log")
    script_path = os.path.join(tempfile.gettempdir(), "pirachest_update.bat")
    with open(script_path, "w", encoding="ascii", newline="\r\n") as fh:
        fh.write(_update_script())
    env = os.environ.copy()
    env.update({
        "PIRACHEST_UPDATE_SRC": new_app_dir,
        "PIRACHEST_UPDATE_DST": install_dir,
        "PIRACHEST_UPDATE_STAGE": staging,
        "PIRACHEST_UPDATE_LOG": log_path,
        "PIRACHEST_UPDATE_EXE": current_exe,
        "PIRACHEST_UPDATE_NAME": exe_name,
        "PIRACHEST_UPDATE_PID": str(os.getpid()),
        "PYINSTALLER_RESET_ENVIRONMENT": "1",
    })
    subprocess.Popen(
        ["cmd.exe", "/d", "/c", "pirachest_update.bat"],
        cwd=tempfile.gettempdir(),
        creationflags=subprocess.CREATE_NO_WINDOW,
        close_fds=True,
        env=env,
    )
