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

    return {
        "tag": tag,
        "version": latest,
        "download_url": asset["browser_download_url"],
        "asset_name": asset["name"],
        "notes": data.get("body", ""),
    }


def download_update(download_url: str, on_progress=None) -> str:
    tmp_dir = tempfile.gettempdir()
    tmp_path = os.path.join(tmp_dir, "PiraChest-update.exe")

    req = urllib.request.Request(download_url, headers={"User-Agent": "PiraChest-Updater"})
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
                downloaded += len(chunk)
                if on_progress is not None and total > 0:
                    try:
                        on_progress(downloaded, total)
                    except Exception:
                        logger.exception("Update progress callback raised")

    return tmp_path


def apply_update_and_restart(new_exe_path: str) -> None:
    if sys.platform != "win32":
        raise RuntimeError("Self-update is only supported on Windows")

    if not getattr(sys, "frozen", False):
        raise RuntimeError("Self-update requires a frozen (built) executable")

    current_exe = os.path.abspath(sys.executable)
    exe_dir = os.path.dirname(current_exe)
    exe_name = os.path.basename(current_exe)

    script_path = os.path.join(tempfile.gettempdir(), "pirachest_update.bat")

    script = f"""@echo off
setlocal
set TARGET="{current_exe}"
set NEWEXE="{new_exe_path}"

:waitloop
tasklist /FI "IMAGENAME eq {exe_name}" 2>NUL | find /I "{exe_name}" >NUL
if "%ERRORLEVEL%"=="0" (
    timeout /t 1 /nobreak >NUL
    goto waitloop
)

move /Y %NEWEXE% %TARGET% >NUL
start "" %TARGET%
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
