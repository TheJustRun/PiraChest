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
import xml.etree.ElementTree as ET
import zipfile
import zlib
from concurrent.futures import FIRST_EXCEPTION, ThreadPoolExecutor, wait
from contextlib import suppress
from urllib.parse import quote

from src.core import system as host
from src.core.config import IS_CANARY, VERSION_TAG

logger = logging.getLogger(__name__)

_GITHUB = "TheJustRun/PiraChest"
_GITLAB = "TheJustRun%2FPiraChest"
_SOURCEFORGE = "pirachest"
_SF_DOWNLOADS = f"https://downloads.sourceforge.net/project/{_SOURCEFORGE}/"
_MIRRORS = (
    f"https://github.com/{_GITHUB}/releases/download/{{tag}}/{{name}}",
    f"https://gitlab.com/api/v4/projects/{_GITLAB}/packages/generic/PiraChest/{{tag}}/{{name}}",
    _SF_DOWNLOADS + "{tag}/{name}",
)
_HEADERS = {"User-Agent": "PiraChest-Updater", "Accept-Encoding": "identity"}
_HASH_RE = re.compile(r"^[0-9a-f]{64}$")
_ZIP_RE = re.compile(r"^pirachest.*windows.*x64\.zip$", re.I)
_CANARY_RE = re.compile(r"canary|nightly|unstable|experimental", re.I)
_MANIFEST_NAME = "update-manifest.json"
_ERROR_LOG = "pirachest_update_error.log"
_CACHE: dict[str, tuple[float, bytes]] = {}
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


class UpdateCheckError(RuntimeError):
    pass


def _error_log_path() -> str:
    return os.path.join(tempfile.gettempdir(), _ERROR_LOG)


def take_update_error() -> bool:
    path = _error_log_path()
    if not os.path.isfile(path):
        return False
    with suppress(OSError):
        os.remove(path)
    return True


def _ensure_writable(root: str) -> None:
    if _can_write(root):
        return
    if host.WINDOWS:
        raise RuntimeError("PiraChest cannot write to its install folder. Run it as administrator or reinstall through Chocolatey.")
    raise RuntimeError("PiraChest cannot write to its install folder. Move the app somewhere writable or reinstall it.")


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


def _get(url: str) -> bytes:
    hit = _CACHE.get(url)
    if hit and time.monotonic() - hit[0] < _CACHE_TTL:
        return hit[1]
    for attempt in (0, 1):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=_HEADERS), timeout=10) as resp:
                data = resp.read()
            break
        except OSError as exc:
            if attempt or (isinstance(exc, urllib.error.HTTPError) and exc.code < 500):
                raise
            time.sleep(0.6)
    _CACHE[url] = (time.monotonic(), data)
    return data


def _get_json(url: str):
    return json.loads(_get(url))


def _github() -> list[dict]:
    return [
        {"tag": r["tag_name"], "body": r.get("body") or "",
         "assets": [{"name": a["name"], "url": a["browser_download_url"],
                     "size": a.get("size") or 0, "digest": a.get("digest") or ""}
                    for a in r.get("assets") or ()]}
        for r in _get_json(f"https://api.github.com/repos/{_GITHUB}/releases?per_page=100")
        if r.get("tag_name") and not r.get("draft")
    ]


def _gitlab() -> list[dict]:
    return [
        {"tag": r["tag_name"], "body": r.get("description") or "",
         "assets": [{"name": a["name"], "url": a["url"], "size": 0, "digest": ""}
                    for a in r["assets"]["links"]]}
        for r in _get_json(f"https://gitlab.com/api/v4/projects/{_GITLAB}/releases?per_page=100")
        if not r.get("upcoming_release")
    ]


def _sourceforge() -> list[dict]:
    releases: dict[str, list[dict]] = {}
    root = ET.fromstring(_get(f"https://sourceforge.net/projects/{_SOURCEFORGE}/rss?path=/&limit=500"))
    for item in root.iter("item"):
        path = item.findtext("title", "").strip().strip("/")
        tag, _, name = path.partition("/")
        if not name or "/" in name:
            continue
        size = next((int(c.get("filesize") or 0) for c in item if c.tag.endswith("}content")), 0)
        releases.setdefault(tag, []).append(
            {"name": name, "url": _SF_DOWNLOADS + quote(path), "size": size, "digest": ""})
    return [{"tag": tag, "body": "", "assets": assets} for tag, assets in releases.items()]


_SOURCES = (_github, _gitlab, _sourceforge)


def _urls(tag: str, asset: dict) -> list[str]:
    fields = {"tag": quote(tag), "name": quote(asset["name"])}
    urls = dict.fromkeys([asset["url"], *(m.format(**fields) for m in _MIRRORS)])
    return [u for u in urls if u.lower().startswith("https://")]


def _manifest_choice(manifest: dict, assets: list[dict]) -> dict | None:
    by_name = {a["name"]: a for a in assets}
    for entry in manifest["assets"]:
        name = entry["name"]
        if (name not in by_name
                or entry.get("os", "").lower() != host.os_key()
                or entry.get("arch", "").lower() not in ("", host.arch())):
            continue
        return {
            "asset": by_name[name],
            "format": entry.get("format") or _format_of(name),
            "kind": entry.get("kind") or "",
            "sha256": entry.get("sha256", "").lower(),
            "size": int(entry.get("size") or 0),
        }
    return None


def _pick_asset(assets: list[dict]) -> dict | None:
    manifest = next((a for a in assets if a["name"] == _MANIFEST_NAME), None)
    if manifest:
        try:
            chosen = _manifest_choice(_get_json(manifest["url"]), assets)
            if chosen:
                return chosen
        except Exception as exc:
            logger.info("Update manifest unavailable: %s", exc)
    matched = [a for a in assets
               if _format_of(a["name"]) and host.name_matches_platform(a["name"], host.os_key(), host.arch())]
    if host.WINDOWS:
        matched = [a for a in matched if _ZIP_RE.match(a["name"])] or matched
    return {"asset": matched[0]} if matched else None


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


def _checksum_asset(assets: list[dict], asset_name: str) -> dict | None:
    wanted = (f"{asset_name}.sha256", "SHA256SUMS", "checksums.txt", "SHA256SUMS.txt")
    return next((a for name in wanted for a in assets if a["name"] == name), None)


def _is_choco_install() -> bool:
    if not host.WINDOWS or not getattr(sys, "frozen", False):
        return False
    app = _app_dir()
    if "\\windowsapps\\" in app.lower():
        return True
    if os.path.isfile(os.path.join(app, ".pirachest-choco")):
        return True
    return "\\chocolatey\\lib\\" in app.lower()


_MANAGED_PREFIXES = ("/usr/", "/snap/", "/var/lib/flatpak/")


def _is_package_managed() -> bool:
    if not getattr(sys, "frozen", False):
        return False
    if host.WINDOWS:
        return _is_choco_install()
    if not host.LINUX:
        return False
    if any(os.environ.get(k) for k in ("APPIMAGE", "FLATPAK_ID", "SNAP")):
        return True
    root = os.path.realpath(_install_root()).replace(os.sep, "/") + "/"
    if root.startswith(_MANAGED_PREFIXES):
        return True
    if not root.startswith("/opt/"):
        return False
    for bindir in ("/usr/bin", "/usr/local/bin"):
        try:
            names = os.listdir(bindir)
        except OSError:
            continue
        for name in names:
            link = os.path.join(bindir, name)
            if os.path.islink(link) and os.path.realpath(link).startswith(root):
                return True
    return False


def _newest(releases: list[dict], channel: str) -> dict | None:
    current = _parse_version(VERSION_TAG)
    for release in sorted(releases, key=lambda r: _parse_version(r["tag"]), reverse=True):
        tag, assets = release["tag"], release["assets"]
        canary = bool(_CANARY_RE.search(tag)) or any(_CANARY_RE.search(a["name"]) for a in assets)
        if channel != "auto" and canary != (channel == "canary"):
            continue
        version = _parse_version(tag)
        if version <= current:
            return None
        picked = _pick_asset(assets)
        if picked is None:
            continue
        asset = picked["asset"]
        digest = next((d for d in ((picked.get("sha256") or "").lower(),
                                   (asset["digest"] or "").lower().removeprefix("sha256:"))
                       if _HASH_RE.match(d)), "")
        checksum = _checksum_asset(assets, asset["name"])
        if not digest and checksum is None:
            logger.warning("Release %s has no SHA-256 checksum; skipping", tag)
            continue
        return {
            "tag": tag,
            "label": _tag_label(tag),
            "version": version,
            "download_urls": _urls(tag, asset),
            "asset_name": asset["name"],
            "asset_size": int(asset["size"] or picked.get("size") or 0),
            "checksum_urls": _urls(tag, checksum) if checksum else [],
            "checksum_sha256": digest,
            "format": picked.get("format") or _format_of(asset["name"]),
            "kind": picked.get("kind") or ("bundle" if host.MACOS else "onedir"),
            "os": host.os_key(),
            "arch": host.arch(),
            "notes": release["body"],
            "canary": canary,
            "channel": "canary" if canary else "stable",
        }
    return None


def check_for_update(channel: str | None = None, strict: bool = False) -> dict | None:
    if _is_package_managed():
        return None
    channel = (channel or ("auto" if IS_CANARY else "stable")).lower()
    if channel not in ("auto", "stable", "canary"):
        channel = "stable"
    for source in _SOURCES:
        try:
            return _newest(source(), channel)
        except Exception as exc:
            logger.warning("Update source %s failed: %s", source.__name__.lstrip("_"), exc)
    if strict:
        raise UpdateCheckError("All update sources failed")
    return None


def _update_staging_dir() -> str:
    try:
        from src.core.config import paths
        base = os.path.join(paths.app_data_dir, "update")
    except Exception:
        base = os.path.join(tempfile.gettempdir(), "PiraChest-update")
    os.makedirs(base, exist_ok=True)
    return base


def cleanup_update_staging(_path: str | None = None) -> None:
    try:
        shutil.rmtree(_update_staging_dir(), ignore_errors=True)
    except Exception:
        logger.warning("Failed to clean up update staging directory", exc_info=True)


def fetch_expected_checksum(checksum_url: str, asset_name: str) -> str | None:
    if not checksum_url or not asset_name:
        return None
    try:
        text = _get(checksum_url)[:1 << 20].decode("utf-8-sig", errors="replace")
    except Exception as exc:
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
    req = urllib.request.Request(url, headers=_HEADERS)
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


def _probe_ranges(url: str) -> tuple[int, bool]:
    req = urllib.request.Request(url, headers={**_HEADERS, "Range": "bytes=0-0"})
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
                    req = urllib.request.Request(url, headers={**_HEADERS, "Range": f"bytes={pos}-{end}"})
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
    except urllib.error.HTTPError as exc:
        if exc.code < 500 and exc.code not in (408, 429):
            raise
        logger.info("Range probe failed, using single stream: %s", exc)
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
            if attempt >= 2 or (isinstance(exc, urllib.error.HTTPError)
                                and exc.code < 500 and exc.code not in (408, 429)):
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
        head, _sep, leaf = name.rpartition("/")
        if leaf == exe_name:
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
        if not items:
            raise RuntimeError(f"{exe_name} not found in update archive")
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
        name = update_info.get("asset_name", "")
        expected = next(filter(None, (fetch_expected_checksum(url, name)
                                      for url in update_info.get("checksum_urls", ()))), "")
    if not expected:
        raise RuntimeError("Could not verify the update checksum")
    if getattr(sys, "frozen", False):
        _ensure_writable(_install_root())
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
        failure = RuntimeError("No download source available")
        for url in update_info["download_urls"]:
            try:
                _fetch_archive(url, archive_path, expected, on_progress, is_cancelled, on_stage)
                break
            except UpdateCancelled:
                raise
            except Exception as exc:
                logger.warning("Download from %s failed: %s", url, exc)
                failure = exc
        else:
            raise failure
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


def _snapshot(new_dir: str, install_dir: str, staging: str) -> tuple[str, str]:
    backup = os.path.join(staging, "backup")
    added_list = os.path.join(staging, "added.lst")
    shutil.rmtree(backup, ignore_errors=True)
    added = []
    for current, _dirs, files in os.walk(new_dir):
        for name in files:
            rel = os.path.relpath(os.path.join(current, name), new_dir)
            target = os.path.join(install_dir, rel)
            if os.path.lexists(target):
                saved = os.path.join(backup, rel)
                os.makedirs(os.path.dirname(saved), exist_ok=True)
                shutil.copy2(target, saved, follow_symlinks=False)
            else:
                added.append(rel)
    with open(added_list, "w", encoding="utf-8", newline="\n") as fh:
        fh.write("".join(f"{rel}\n" for rel in added))
    return backup, added_list


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
set "BAK=%PIRACHEST_UPDATE_BAK%"
set "ADD=%PIRACHEST_UPDATE_ADD%"
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
if exist "%DEL%" for /f "usebackq eol=| tokens=* delims=" %%F in ("%DEL%") do del /f /q "%DST%\\%%F" >NUL 2>NUL

:done
if defined FAILED (
    robocopy "%BAK%" "%DST%" /E /IS /R:5 /W:1 /NFL /NDL /NJH /NJS /NP >NUL 2>&1
    if exist "%ADD%" for /f "usebackq eol=| tokens=* delims=" %%F in ("%ADD%") do del /f /q "%DST%\\%%F" >NUL 2>NUL
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
LOG="$8"
BAK="$9"
ADD="${10}"
rm -f "$LOG"
OK=1
i=0
while kill -0 "$PID" 2>/dev/null; do
  i=$((i + 1))
  if [ "$i" -ge 1200 ]; then break; fi
  sleep 0.25
done
if [ "$KIND" = "bundle" ]; then
  ditto "$SRC" "$DST" || OK=0
else
  cp -Rf "$SRC"/. "$DST"/ || OK=0
fi
if [ "$OK" = 1 ]; then
  if [ -f "$DEL" ]; then
    while IFS= read -r f; do
      if [ -n "$f" ]; then rm -f "$DST/$f"; fi
    done < "$DEL"
  fi
  rm -rf "$STAGE"
else
  if [ -d "$BAK" ]; then cp -Rf "$BAK"/. "$DST"/ 2>/dev/null; fi
  if [ -f "$ADD" ]; then
    while IFS= read -r f; do
      if [ -n "$f" ]; then rm -f "$DST/$f"; fi
    done < "$ADD"
  fi
  echo "Update copy failed." > "$LOG"
fi
chmod +x "$EXE" 2>/dev/null || true
rm -f "$0"
if [ "$KIND" = "bundle" ]; then
  if [ "$OK" = 1 ]; then codesign --force --deep --sign - "$DST" >/dev/null 2>&1 || true; fi
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
        raise RuntimeError(f"The downloaded update is incomplete: {marker} is missing")
    _ensure_writable(install_root)
    staging = _update_staging_dir()
    backup, added_list = _snapshot(new_app_dir, install_root, staging)
    temp = tempfile.gettempdir()
    fd, script_path = tempfile.mkstemp(prefix="pirachest_update_", suffix=".sh")
    with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(_POSIX_UPDATE_SCRIPT)
    env = os.environ.copy()
    env["PYINSTALLER_RESET_ENVIRONMENT"] = "1"
    subprocess.Popen(
        ["/bin/sh", script_path, str(os.getpid()), new_app_dir, install_root, staging,
         current_exe, os.path.join(staging, "delete.lst"), kind, _error_log_path(), backup, added_list],
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
        raise RuntimeError(f"The downloaded update is incomplete: {os.path.join(new_app_dir, exe_name)} is missing")
    _ensure_writable(install_dir)
    staging = _update_staging_dir()
    backup, added_list = _snapshot(new_app_dir, install_dir, staging)
    temp = tempfile.gettempdir()
    script_path = os.path.join(temp, "pirachest_update.bat")
    with open(script_path, "w", encoding="ascii", newline="\r\n") as fh:
        fh.write(_UPDATE_SCRIPT)
    env = os.environ.copy()
    env.update({
        "PIRACHEST_UPDATE_SRC": new_app_dir,
        "PIRACHEST_UPDATE_DST": install_dir,
        "PIRACHEST_UPDATE_STAGE": staging,
        "PIRACHEST_UPDATE_LOG": _error_log_path(),
        "PIRACHEST_UPDATE_EXE": current_exe,
        "PIRACHEST_UPDATE_NAME": exe_name,
        "PIRACHEST_UPDATE_PID": str(os.getpid()),
        "PIRACHEST_UPDATE_DEL": os.path.join(staging, "delete.lst"),
        "PIRACHEST_UPDATE_BAK": backup,
        "PIRACHEST_UPDATE_ADD": added_list,
        "PYINSTALLER_RESET_ENVIRONMENT": "1",
    })
    subprocess.Popen(
        ["cmd.exe", "/d", "/c", "pirachest_update.bat"],
        cwd=temp,
        creationflags=subprocess.CREATE_NO_WINDOW,
        close_fds=True,
        env=env,
    )
