from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import tempfile
import urllib.request
import urllib.error

logger = logging.getLogger(__name__)

def _load_version() -> str:
    if getattr(sys, "frozen", False):
        base = getattr(sys, "_MEIPASS", os.path.dirname(sys.executable))
    else:
        base = os.path.dirname(os.path.abspath(__file__))
    version_file = os.path.join(base, "VERSION")
    try:
        with open(version_file, "r") as fh:
            return fh.read().strip()
    except OSError:
        return "0.0.0-dev"

__version__ = _load_version()

_REPO = "TheJustRun/PiraChest"
_API_URL = f"https://api.github.com/repos/{_REPO}/releases/latest"


def _parse_version(tag: str) -> tuple:
    tag = tag.lstrip("v")
    tag = tag.split("-")[0]
    parts = tag.split(".")
    out = []
    for p in parts:
        try:
            out.append(int(p))
        except ValueError:
            out.append(0)
    return tuple(out)


def check_for_update() -> dict | None:
    req = urllib.request.Request(
        _API_URL,
        headers={"Accept": "application/vnd.github+json", "User-Agent": "PiraChest-Updater"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        logger.warning("Update check failed: %s", exc)
        return None

    tag = data.get("tag_name", "")
    if not tag:
        return None

    latest = _parse_version(tag)
    current = _parse_version(__version__)

    if latest <= current:
        return None

    asset = None
    for a in data.get("assets", []):
        name = a.get("name", "")
        if name.lower().endswith(".exe") and "windows" in name.lower():
            asset = a
            break

    if asset is None:
        logger.warning("No matching exe asset found in latest release")
        return None

    checksum_url = None
    checksum_names = (asset["name"] + ".sha256", "SHA256SUMS", "checksums.txt", "SHA256SUMS.txt")
    for a in data.get("assets", []):
        if a.get("name", "") in checksum_names:
            checksum_url = a["browser_download_url"]
            break

    return {
        "tag": tag,
        "version": latest,
        "download_url": asset["browser_download_url"],
        "asset_name": asset["name"],
        "checksum_url": checksum_url,
        "notes": data.get("body", ""),
    }


def download_update(download_url: str, on_progress=None) -> tuple[str, str]:
    import hashlib

    tmp_dir = tempfile.gettempdir()
    tmp_path = os.path.join(tmp_dir, "PiraChest-update.exe")

    req = urllib.request.Request(download_url, headers={"User-Agent": "PiraChest-Updater"})
    hasher = hashlib.sha256()
    with urllib.request.urlopen(req, timeout=60) as resp:
        total = int(resp.headers.get("Content-Length", 0))
        downloaded = 0
        chunk_size = 1024 * 256
        with open(tmp_path, "wb") as fh:
            while True:
                chunk = resp.read(chunk_size)
                if not chunk:
                    break
                fh.write(chunk)
                hasher.update(chunk)
                downloaded += len(chunk)
                if on_progress is not None and total > 0:
                    try:
                        on_progress(downloaded, total)
                    except Exception:
                        logger.exception("Update progress callback raised")

    if total > 0 and downloaded != total:
        try:
            os.remove(tmp_path)
        except OSError:
            pass
        raise RuntimeError(f"Download incomplete: got {downloaded} bytes, expected {total}")

    return tmp_path, hasher.hexdigest()


def fetch_expected_checksum(checksum_url: str, asset_name: str) -> str | None:
    req = urllib.request.Request(checksum_url, headers={"User-Agent": "PiraChest-Updater"})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            text = resp.read().decode("utf-8", errors="replace")
    except (urllib.error.URLError, TimeoutError) as exc:
        logger.warning("Failed to fetch checksum file: %s", exc)
        return None

    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split()
        if len(parts) == 1:
            return parts[0].lower()
        if len(parts) >= 2:
            digest, name = parts[0].lower(), parts[-1].lstrip("*")
            if name == asset_name or asset_name in name:
                return digest
    return None


def verify_checksum(file_path: str, expected_sha256: str) -> bool:
    import hashlib
    hasher = hashlib.sha256()
    with open(file_path, "rb") as fh:
        while True:
            chunk = fh.read(1024 * 256)
            if not chunk:
                break
            hasher.update(chunk)
    return hasher.hexdigest().lower() == expected_sha256.lower()


def apply_update_and_restart(new_exe_path: str) -> None:
    if sys.platform != "win32":
        raise RuntimeError("Self-update is only supported on Windows")

    if not getattr(sys, "frozen", False):
        raise RuntimeError("Self-update requires a frozen (built) executable")

    current_exe = os.path.abspath(sys.executable)
    exe_dir = os.path.dirname(current_exe)
    exe_name = os.path.basename(current_exe)

    script_path = os.path.join(tempfile.gettempdir(), "pirachest_update.bat")
    log_path = os.path.join(tempfile.gettempdir(), "pirachest_update_error.log")

    script = f"""@echo off
setlocal
set TARGET="{current_exe}"
set NEWEXE="{new_exe_path}"
set LOG="{log_path}"

:waitloop
tasklist /FI "IMAGENAME eq {exe_name}" 2>NUL | find /I "{exe_name}" >NUL
if "%ERRORLEVEL%"=="0" (
    timeout /t 1 /nobreak >NUL
    goto waitloop
)

set RETRIES=0
:moveloop
move /Y %NEWEXE% %TARGET% >NUL 2>NUL
if exist %NEWEXE% (
    set /a RETRIES+=1
    if %RETRIES% GEQ 10 (
        echo Failed to move update after 10 attempts: %NEWEXE% -^> %TARGET% > %LOG%
        goto end
    )
    timeout /t 1 /nobreak >NUL
    goto moveloop
)

start "" %TARGET%
:end
del "%~f0"
"""

    with open(script_path, "w") as fh:
        fh.write(script)

    subprocess.Popen(
        ["cmd.exe", "/c", script_path],
        cwd=exe_dir,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )

    sys.exit(0)
