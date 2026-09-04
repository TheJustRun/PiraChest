from __future__ import annotations

import json
import logging
import os
import re
import shutil
import stat
import subprocess
import sys
import threading
import zipfile
from typing import Any, Callable, Optional

from ..cache import cache
from ..config import paths

logger = logging.getLogger(__name__)

_NS = "yt"
_TTL = 21600

_URL_RE = re.compile(r"^https?://", re.IGNORECASE)

_lock = threading.Lock()


class YtError(Exception):
    pass


def _yt_dir() -> str:
    return paths.yt_dir


def _ytdlp_name() -> str:
    return "yt-dlp.exe" if os.name == "nt" else "yt-dlp"


def _ytdlp_path() -> str:
    return os.path.join(_yt_dir(), _ytdlp_name())


def _ffmpeg_name() -> str:
    return "ffmpeg.exe" if os.name == "nt" else "ffmpeg"


def _ffprobe_name() -> str:
    return "ffprobe.exe" if os.name == "nt" else "ffprobe"


def _ffmpeg_path() -> str:
    return os.path.join(_yt_dir(), _ffmpeg_name())


def _ffprobe_path() -> str:
    return os.path.join(_yt_dir(), _ffprobe_name())


def is_valid_url(url: str) -> bool:
    return bool(url and _URL_RE.match(url.strip()))


def _fmt_size(n: Optional[int]) -> str:
    if not n:
        return ""
    v = float(n)
    for unit in ("B", "KiB", "MiB", "GiB"):
        if abs(v) < 1024.0:
            return f"{v:3.1f} {unit}"
        v /= 1024.0
    return f"{v:.1f} TiB"


def _fmt_duration(seconds: Optional[float]) -> str:
    if not seconds or seconds < 0:
        return ""
    total = int(seconds)
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"


def _video_formats(info: dict) -> list[dict]:
    out = []
    seen_heights = set()
    for f in info.get("formats") or []:
        vcodec = f.get("vcodec")
        if not vcodec or vcodec == "none":
            continue
        height = f.get("height")
        if not height:
            continue
        if height in seen_heights:
            continue
        seen_heights.add(height)
        size = f.get("filesize") or f.get("filesize_approx")
        out.append({
            "format_id": f.get("format_id", ""),
            "height": height,
            "fps": f.get("fps"),
            "ext": f.get("ext", "mp4"),
            "size_bytes": size,
            "size_label": _fmt_size(size),
            "label": f"{height}p{int(f.get('fps') or 0) if (f.get('fps') or 0) > 30 else ''}",
        })
    out.sort(key=lambda x: x["height"], reverse=True)
    return out


def _audio_formats(info: dict) -> list[dict]:
    out = []
    seen_abr = set()
    for f in info.get("formats") or []:
        acodec = f.get("acodec")
        if not acodec or acodec == "none":
            continue
        if (f.get("vcodec") or "none") != "none":
            continue
        abr = f.get("abr") or f.get("tbr")
        if not abr:
            continue
        abr_int = int(abr)
        if abr_int in seen_abr:
            continue
        seen_abr.add(abr_int)
        size = f.get("filesize") or f.get("filesize_approx")
        out.append({
            "format_id": f.get("format_id", ""),
            "abr": abr_int,
            "ext": f.get("ext", "m4a"),
            "size_bytes": size,
            "size_label": _fmt_size(size),
            "label": f"{abr_int} kbps",
        })
    out.sort(key=lambda x: x["abr"], reverse=True)
    return out


def _info_to_dict(info: dict, url: str) -> dict:
    return {
        "url": url,
        "id": info.get("id", ""),
        "title": info.get("title", "Unknown"),
        "uploader": info.get("uploader") or info.get("channel") or "",
        "thumbnail": info.get("thumbnail") or "",
        "duration": info.get("duration"),
        "duration_label": _fmt_duration(info.get("duration")),
        "description": (info.get("description") or "")[:2000],
        "upload_date": info.get("upload_date") or "",
        "view_count": info.get("view_count"),
        "webpage_url": info.get("webpage_url") or url,
        "video_formats": _video_formats(info),
        "audio_formats": _audio_formats(info),
        "is_live": bool(info.get("is_live")),
        "availability": info.get("availability") or "public",
    }


def _run_ytdlp(args: list[str], is_cancelled: Optional[Callable[[], bool]] = None) -> subprocess.Popen:
    exe = _ytdlp_path()
    if not os.path.isfile(exe):
        raise YtError("yt-dlp binary not found")
    creationflags = 0
    if os.name == "nt":
        creationflags = subprocess.CREATE_NO_WINDOW
    return subprocess.Popen(
        [exe, *args],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        creationflags=creationflags,
    )


def fetch_info(url: str, use_cache: bool = True, is_cancelled: Optional[Callable[[], bool]] = None) -> dict:
    if not is_valid_url(url):
        raise YtError("Invalid URL")

    cache_key = url.strip()
    if use_cache:
        cached = cache.load(_NS, cache_key, ttl_seconds=_TTL)
        if cached is not None:
            return cached

    if is_cancelled is not None and is_cancelled():
        raise YtError("Cancelled")

    args = [
        "--dump-single-json",
        "--no-warnings",
        "--no-playlist",
        "--skip-download",
        url,
    ]

    proc = _run_ytdlp(args)
    try:
        out, err = proc.communicate()
    except Exception as exc:
        proc.kill()
        raise YtError(str(exc)) from exc

    if proc.returncode != 0:
        raise YtError(err.strip() or "Failed to fetch video information")

    try:
        info = json.loads(out)
    except json.JSONDecodeError as exc:
        raise YtError("Failed to parse video information") from exc

    if info is None:
        raise YtError("No information could be extracted for this URL")

    if info.get("_type") == "playlist":
        entries = info.get("entries") or []
        if not entries:
            raise YtError("Playlist has no playable entries")
        info = entries[0]

    result = _info_to_dict(info, url)

    if use_cache:
        cache.save(_NS, cache_key, result)

    return result


def safe_filename(name: str) -> str:
    cleaned = re.sub(r'[<>:"/\\|?*]+', "_", name).strip().strip(".")
    return cleaned[:150] or "video"


def _parse_progress_line(line: str) -> Optional[tuple[int, int, float]]:
    if not line.startswith("YT_PROGRESS "):
        return None
    parts = line[len("YT_PROGRESS "):].strip().split(" ")
    if len(parts) != 4:
        return None
    try:
        downloaded = int(float(parts[0]))
        total = int(float(parts[1])) if parts[1] != "NA" else 0
        if not total:
            total = int(float(parts[2])) if parts[2] != "NA" else 0
        speed = float(parts[3]) if parts[3] != "NA" else 0.0
    except ValueError:
        return None
    return downloaded, total, speed / 1024.0


_PROGRESS_TEMPLATE = (
    "YT_PROGRESS %(progress.downloaded_bytes)s %(progress.total_bytes)s "
    "%(progress.total_bytes_estimate)s %(progress.speed)s"
)


def has_ffmpeg() -> bool:
    return os.path.isfile(_ffmpeg_path()) and os.path.isfile(_ffprobe_path())


def has_ytdlp() -> bool:
    return os.path.isfile(_ytdlp_path())


def has_yt_tools() -> bool:
    return has_ffmpeg() and has_ytdlp()


_YTDLP_ASSETS = {
    "win32": "https://github.com/yt-dlp/yt-dlp-nightly-builds/releases/latest/download/yt-dlp.exe",
    "darwin": "https://github.com/yt-dlp/yt-dlp-nightly-builds/releases/latest/download/yt-dlp_macos",
    "linux": "https://github.com/yt-dlp/yt-dlp-nightly-builds/releases/latest/download/yt-dlp",
}

_FFMPEG_ASSETS = {
    "win32": ("https://github.com/zackees/ffmpeg_bins/raw/main/v8.0/win32.zip", "ffmpeg.exe", "ffprobe.exe"),
    "darwin": ("https://github.com/zackees/ffmpeg_bins/raw/main/v8.0/macos.zip", "ffmpeg", "ffprobe"),
    "linux": ("https://github.com/zackees/ffmpeg_bins/raw/main/v8.0/linux64.zip", "ffmpeg", "ffprobe"),
}


def _platform_key() -> str:
    if sys.platform == "win32":
        return "win32"
    if sys.platform == "darwin":
        return "darwin"
    return "linux"


def _stem(name: str) -> str:
    return name[:-4] if name.lower().endswith(".exe") else name


def _make_executable(path: str) -> None:
    if os.name == "nt":
        return
    try:
        st = os.stat(path)
        os.chmod(path, st.st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    except OSError:
        pass


def _download_file(url: str, dest_path: str, is_cancelled: Optional[Callable[[], bool]], on_progress: Callable[[int, int], None]) -> None:
    import requests

    with requests.get(url, stream=True, timeout=60, allow_redirects=True) as resp:
        resp.raise_for_status()
        total = int(resp.headers.get("Content-Length") or 0)
        downloaded = 0
        with open(dest_path, "wb") as fh:
            for chunk in resp.iter_content(chunk_size=262144):
                if is_cancelled is not None and is_cancelled():
                    raise YtError("Cancelled")
                if not chunk:
                    continue
                fh.write(chunk)
                downloaded += len(chunk)
                on_progress(downloaded, total)


def download_ytdlp(
    on_progress: Callable[[int, int], None],
    is_cancelled: Optional[Callable[[], bool]] = None,
) -> None:
    key = _platform_key()
    if key not in _YTDLP_ASSETS:
        raise YtError(f"No yt-dlp download available for platform: {sys.platform}")
    url = _YTDLP_ASSETS[key]

    os.makedirs(_yt_dir(), exist_ok=True)
    tmp_path = os.path.join(_yt_dir(), "_ytdlp_download.tmp")

    try:
        _download_file(url, tmp_path, is_cancelled, on_progress)
        final_path = _ytdlp_path()
        if os.path.isfile(final_path):
            try:
                os.remove(final_path)
            except OSError:
                pass
        shutil.move(tmp_path, final_path)
        _make_executable(final_path)
    except YtError:
        raise
    except Exception as exc:
        raise YtError(str(exc)) from exc
    finally:
        if os.path.isfile(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:
                pass

    if not os.path.isfile(_ytdlp_path()):
        raise YtError("yt-dlp download did not produce a usable binary")


def download_ffmpeg(
    on_progress: Callable[[int, int], None],
    is_cancelled: Optional[Callable[[], bool]] = None,
) -> None:
    key = _platform_key()
    if key not in _FFMPEG_ASSETS:
        raise YtError(f"No ffmpeg download available for platform: {sys.platform}")
    url, exe_name, probe_name = _FFMPEG_ASSETS[key]

    os.makedirs(_yt_dir(), exist_ok=True)
    tmp_zip = os.path.join(_yt_dir(), "_ffmpeg_download.zip")
    extract_dir = os.path.join(_yt_dir(), "_extract_tmp")

    try:
        _download_file(url, tmp_zip, is_cancelled, on_progress)

        if os.path.isdir(extract_dir):
            shutil.rmtree(extract_dir, ignore_errors=True)
        with zipfile.ZipFile(tmp_zip) as zf:
            zf.extractall(extract_dir)

        exe_stub, probe_stub = _stem(exe_name).lower(), _stem(probe_name).lower()
        found_exe = found_probe = None
        for dirpath, _dirnames, filenames in os.walk(extract_dir):
            for fname in filenames:
                stem = _stem(fname).lower()
                if found_exe is None and stem == exe_stub:
                    found_exe = os.path.join(dirpath, fname)
                elif found_probe is None and stem == probe_stub:
                    found_probe = os.path.join(dirpath, fname)
            if found_exe and found_probe:
                break

        if not (found_exe and found_probe):
            raise YtError("Downloaded archive did not contain ffmpeg/ffprobe binaries")

        shutil.move(found_exe, _ffmpeg_path())
        shutil.move(found_probe, _ffprobe_path())
    except YtError:
        raise
    except Exception as exc:
        raise YtError(str(exc)) from exc
    finally:
        for path in (tmp_zip, extract_dir):
            try:
                if os.path.isfile(path):
                    os.remove(path)
                elif os.path.isdir(path):
                    shutil.rmtree(path, ignore_errors=True)
            except OSError:
                pass

    _make_executable(_ffmpeg_path())
    _make_executable(_ffprobe_path())

    if not (os.path.isfile(_ffmpeg_path()) and os.path.isfile(_ffprobe_path())):
        raise YtError("Downloaded archive did not contain ffmpeg/ffprobe binaries")


def download_yt_tools(
    on_progress: Callable[[str, int, int], None],
    is_cancelled: Optional[Callable[[], bool]] = None,
) -> None:
    download_ytdlp(lambda d, t: on_progress("yt-dlp", d, t), is_cancelled=is_cancelled)
    if is_cancelled is not None and is_cancelled():
        raise YtError("Cancelled")
    download_ffmpeg(lambda d, t: on_progress("ffmpeg", d, t), is_cancelled=is_cancelled)


def _known_format_size(info: dict, format_id: str, kind: str) -> Optional[int]:
    formats = info.get("video_formats" if kind == "video" else "audio_formats") or []
    for f in formats:
        if f.get("format_id") == format_id:
            return f.get("size_bytes")
    return None


def download(
    url: str,
    dest_dir: str,
    mode: str,
    format_id: str,
    title: str,
    on_progress: Callable[[int, int, float], None],
    is_cancelled: Optional[Callable[[], bool]] = None,
) -> str:
    if not has_yt_tools():
        raise YtError("yt-dlp/ffmpeg not found")

    os.makedirs(dest_dir, exist_ok=True)
    base_name = safe_filename(title)
    outtmpl = os.path.join(dest_dir, base_name + ".%(ext)s")

    known_total = 0
    try:
        info = fetch_info(url, use_cache=True, is_cancelled=is_cancelled)
    except YtError:
        info = None
    if info is not None:
        if mode == "audio":
            size = _known_format_size(info, format_id, "audio")
            if size:
                known_total = size
        else:
            video_size = _known_format_size(info, format_id, "video")
            best_audio = max(
                (f.get("size_bytes") or 0 for f in info.get("audio_formats") or []),
                default=0,
            )
            if video_size:
                known_total = video_size + best_audio

    args = [
        "--no-warnings",
        "--no-playlist",
        "--newline",
        "--progress-template", _PROGRESS_TEMPLATE,
        "--ffmpeg-location", _yt_dir(),
        "-o", outtmpl,
        "--write-thumbnail",
    ]

    if mode == "audio":
        args += [
            "-f", format_id or "bestaudio/best",
            "--extract-audio",
            "--audio-format", "mp3",
            "--audio-quality", "192",
            "--add-metadata",
            "--embed-thumbnail",
        ]
    else:
        fmt = f"{format_id}+bestaudio/best" if format_id else "bestvideo+bestaudio/best"
        args += [
            "-f", fmt,
            "--merge-output-format", "mp4",
            "--recode-video", "mp4",
            "--add-metadata",
        ]

    args.append(url)

    with _lock:
        proc = _run_ytdlp(args, is_cancelled=is_cancelled)
        stderr_lines: list[str] = []

        def _read_stderr() -> None:
            for line in proc.stderr:
                stderr_lines.append(line)

        stderr_thread = threading.Thread(target=_read_stderr, daemon=True)
        stderr_thread.start()

        try:
            for line in proc.stdout:
                if is_cancelled is not None and is_cancelled():
                    proc.terminate()
                    raise YtError("Cancelled")
                parsed = _parse_progress_line(line.strip())
                if parsed is not None:
                    downloaded, total, speed_kbps = parsed
                    on_progress(downloaded, known_total or total, speed_kbps)
        finally:
            proc.wait()
            stderr_thread.join(timeout=2)

        if proc.returncode != 0:
            msg = "".join(stderr_lines).strip() or "yt-dlp failed"
            if "Cancelled" in msg:
                raise YtError("Cancelled")
            raise YtError(msg)

    if mode == "audio":
        final_path = os.path.join(dest_dir, base_name + ".mp3")
    else:
        final_path = os.path.join(dest_dir, base_name + ".mp4")

    for junk_ext in (".webp", ".jpg", ".part", ".ytdl"):
        junk_path = os.path.join(dest_dir, base_name + junk_ext)
        if os.path.isfile(junk_path) and junk_path != final_path:
            try:
                os.remove(junk_path)
            except OSError:
                pass

    return final_path
