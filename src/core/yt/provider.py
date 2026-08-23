from __future__ import annotations

import logging
import os
import re
import shutil
import sys
import threading
import zipfile
from typing import Any, Callable, Optional

from ..cache import cache
from ..config import paths, settings

logger = logging.getLogger(__name__)

_NS = "yt"
_TTL = 21600

_URL_RE = re.compile(r"^https?://", re.IGNORECASE)

_YDL_OPTS_BASE = {
    "quiet": True,
    "no_warnings": True,
    "noplaylist": True,
    "skip_download": True,
    "extract_flat": False,
    "extractor_args": {"youtube": {"player_client": ["android", "web"]}},
}

_lock = threading.Lock()


class YtError(Exception):
    pass


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

    import yt_dlp

    try:
        with yt_dlp.YoutubeDL(dict(_YDL_OPTS_BASE)) as ydl:
            info = ydl.extract_info(url, download=False)
    except yt_dlp.utils.DownloadError as exc:
        raise YtError(str(exc)) from exc
    except Exception as exc:
        raise YtError(str(exc)) from exc

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


class _ProgressBridge:
    __slots__ = ("_on_progress", "_cancel_check", "_last_emit", "_completed_bytes", "_current_filename", "_stream_totals")

    def __init__(self, on_progress: Callable[[int, int, float], None], cancel_check: Optional[Callable[[], bool]]):
        self._on_progress = on_progress
        self._cancel_check = cancel_check
        self._last_emit = 0.0
        self._completed_bytes = 0
        self._current_filename = None
        self._stream_totals: dict[str, int] = {}

    def hook(self, d: dict) -> None:
        if self._cancel_check is not None and self._cancel_check():
            raise yt_dlp_module().utils.DownloadError("Cancelled by user")

        filename = d.get("filename")
        status = d.get("status")

        if status == "finished":
            if filename is not None:
                downloaded = d.get("downloaded_bytes") or self._stream_totals.get(filename, 0)
                self._stream_totals[filename] = downloaded
                self._completed_bytes = sum(self._stream_totals.values())
            return

        if status != "downloading":
            return

        import time
        now = time.monotonic()
        if now - self._last_emit < 0.2:
            return
        self._last_emit = now

        stream_downloaded = d.get("downloaded_bytes") or 0
        if filename is not None:
            self._stream_totals[filename] = stream_downloaded

        combined_downloaded = sum(
            v for k, v in self._stream_totals.items() if k != filename
        ) + stream_downloaded

        stream_total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
        combined_total = sum(
            v for k, v in self._stream_totals.items() if k != filename
        ) + stream_total if stream_total else 0

        speed = d.get("speed") or 0.0
        speed_kbps = speed / 1024.0 if speed else 0.0
        self._on_progress(combined_downloaded, combined_total, speed_kbps)


def yt_dlp_module():
    import yt_dlp
    return yt_dlp


def _postprocessor_hook(on_stage: Callable[[str], None]):
    def _hook(d: dict) -> None:
        status = d.get("status")
        if status:
            on_stage(status)
    return _hook


_COMMON_FFMPEG_DIRS = (
    "/usr/bin", "/usr/local/bin", "/opt/homebrew/bin", "/snap/bin",
    "/usr/lib/jellyfin-ffmpeg", "/opt/ffmpeg/bin",
    "C:\\ffmpeg\\bin", "C:\\Program Files\\ffmpeg\\bin",
)


def _find_in_dir(dir_path: str, name: str) -> Optional[str]:
    candidates = (name, name + ".exe") if os.name == "nt" else (name,)
    for c in candidates:
        p = os.path.join(dir_path, c)
        if os.path.isfile(p) and os.access(p, os.X_OK):
            return p
    return None


def _resolve_ffmpeg_dir() -> Optional[str]:
    exe = shutil.which("ffmpeg")
    probe = shutil.which("ffprobe")
    if exe and probe:
        return os.path.dirname(exe)

    for d in (*_COMMON_FFMPEG_DIRS, paths.ffmpeg_dir):
        if _find_in_dir(d, "ffmpeg") and _find_in_dir(d, "ffprobe"):
            return d

    return None


def has_ffmpeg() -> bool:
    return _resolve_ffmpeg_dir() is not None


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


def download_ffmpeg(
    on_progress: Callable[[int, int], None],
    is_cancelled: Optional[Callable[[], bool]] = None,
) -> None:
    key = _platform_key()
    if key not in _FFMPEG_ASSETS:
        raise YtError(f"No ffmpeg download available for platform: {sys.platform}")
    url, exe_name, probe_name = _FFMPEG_ASSETS[key]

    import requests

    os.makedirs(paths.ffmpeg_dir, exist_ok=True)
    tmp_zip = os.path.join(paths.ffmpeg_dir, "_ffmpeg_download.zip")
    extract_dir = os.path.join(paths.ffmpeg_dir, "_extract_tmp")

    try:
        with requests.get(url, stream=True, timeout=60) as resp:
            resp.raise_for_status()
            total = int(resp.headers.get("Content-Length") or 0)
            downloaded = 0
            with open(tmp_zip, "wb") as fh:
                for chunk in resp.iter_content(chunk_size=262144):
                    if is_cancelled is not None and is_cancelled():
                        raise YtError("Cancelled")
                    if not chunk:
                        continue
                    fh.write(chunk)
                    downloaded += len(chunk)
                    on_progress(downloaded, total)

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

        shutil.move(found_exe, os.path.join(paths.ffmpeg_dir, os.path.basename(found_exe)))
        shutil.move(found_probe, os.path.join(paths.ffmpeg_dir, os.path.basename(found_probe)))
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

    if os.name != "nt":
        for name in (exe_name, probe_name):
            found = _find_in_dir(paths.ffmpeg_dir, _stem(name))
            if found:
                try:
                    os.chmod(found, 0o755)
                except OSError:
                    pass

    if _find_in_dir(paths.ffmpeg_dir, "ffmpeg") is None or _find_in_dir(paths.ffmpeg_dir, "ffprobe") is None:
        raise YtError("Downloaded archive did not contain ffmpeg/ffprobe binaries")


def download(
    url: str,
    dest_dir: str,
    mode: str,
    format_id: str,
    title: str,
    on_progress: Callable[[int, int, float], None],
    is_cancelled: Optional[Callable[[], bool]] = None,
) -> str:
    ffmpeg_dir = _resolve_ffmpeg_dir()
    if ffmpeg_dir is None:
        raise YtError(f"ffmpeg/ffprobe not found. PATH={os.environ.get('PATH', '')}")

    import yt_dlp

    os.makedirs(dest_dir, exist_ok=True)
    base_name = safe_filename(title)
    outtmpl = os.path.join(dest_dir, base_name + ".%(ext)s")

    bridge = _ProgressBridge(on_progress, is_cancelled)

    opts: dict[str, Any] = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "outtmpl": outtmpl,
        "progress_hooks": [bridge.hook],
        "writethumbnail": True,
        "restrictfilenames": False,
        "ffmpeg_location": ffmpeg_dir,
        "extractor_args": {"youtube": {"player_client": ["android", "web"]}},
    }

    if mode == "audio":
        opts["format"] = format_id or "bestaudio/best"
        opts["postprocessors"] = [
            {"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "192"},
            {"key": "FFmpegMetadata", "add_metadata": True},
            {"key": "EmbedThumbnail"},
        ]
    else:
        if format_id:
            opts["format"] = f"{format_id}+bestaudio/best"
        else:
            opts["format"] = "bestvideo+bestaudio/best"
        opts["merge_output_format"] = "mp4"
        opts["postprocessors"] = [
            {"key": "FFmpegVideoConvertor", "preferedformat": "mp4"},
            {"key": "FFmpegMetadata", "add_metadata": True},
        ]

    with _lock:
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                result = ydl.extract_info(url, download=True)
        except yt_dlp.utils.DownloadError as exc:
            msg = str(exc)
            if "Cancelled" in msg:
                raise YtError("Cancelled") from exc
            raise YtError(msg) from exc
        except Exception as exc:
            raise YtError(str(exc)) from exc

    if mode == "audio":
        final_path = os.path.join(dest_dir, base_name + ".mp3")
    else:
        final_path = os.path.join(dest_dir, base_name + ".mp4")

    if not os.path.isfile(final_path):
        requested = result.get("requested_downloads") or []
        if requested:
            candidate = requested[0].get("filepath")
            if candidate and os.path.isfile(candidate):
                final_path = candidate

    for junk_ext in (".webp", ".jpg", ".part", ".ytdl"):
        junk_path = os.path.join(dest_dir, base_name + junk_ext)
        if os.path.isfile(junk_path) and junk_path != final_path:
            try:
                os.remove(junk_path)
            except OSError:
                pass

    return final_path
