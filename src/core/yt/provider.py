from __future__ import annotations

import json
import logging
import os
import platform
import re
import shutil
import stat
import subprocess
import sys
import threading
import time
import zipfile
from typing import Callable, Optional
from urllib.parse import parse_qs, urlsplit

from ..cache import cache
from ..config import paths
from ..downloader import NET, Cancelled, HttpError, SegmentedDownload, new_session

logger = logging.getLogger(__name__)

_NS = "yt-v2"  # v2: manual-only subtitle metadata; invalidates stale cached caption data
_TTL = 1800
_STALE_AFTER = 1500
_WINDOW = 10 * 1024 * 1024
_CONNECTIONS = 8
_URL_RE = re.compile(r"^https?://", re.IGNORECASE)
_BAD_CHARS_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f]+')
_PROGRESS_TEMPLATE = "YT_PROGRESS %(progress.downloaded_bytes)s %(progress.total_bytes)s %(progress.total_bytes_estimate)s %(progress.speed)s"
_FORMAT_KEYS = ("format_id", "ext", "height", "fps", "vcodec", "acodec", "abr", "vbr", "tbr", "filesize", "filesize_approx", "protocol", "url", "http_headers", "language", "language_preference")
_ERROR_HINTS = (
    ("not available in your country", "Video is not available in your region"),
    ("private video", "Private video"),
    ("members-only", "Members-only video"),
    ("confirm your age", "Age-restricted video"),
    ("not a bot", "YouTube requires verification; try again later"),
    ("video unavailable", "Video unavailable"),
)
_NO_WINDOW = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
_EXE = ".exe" if os.name == "nt" else ""
_ASSET_BASE = "https://github.com/yt-dlp/yt-dlp-nightly-builds/releases/latest/download/"
_YTDLP_ASSET = {"win32": "yt-dlp.exe", "darwin": "yt-dlp_macos"}.get(sys.platform, "yt-dlp")
_FFMPEG_ZIP = "https://github.com/zackees/ffmpeg_bins/raw/main/v8.0/" + {"win32": "win32", "darwin": "macos"}.get(sys.platform, "linux64") + ".zip"


class YtError(Exception):
    pass


def _dir() -> str:
    return paths.yt_dir


def _tool(name: str) -> str:
    return os.path.join(_dir(), name + _EXE)


def has_ytdlp() -> bool:
    return os.path.isfile(_tool("yt-dlp"))


def has_ffmpeg() -> bool:
    return os.path.isfile(_tool("ffmpeg")) and os.path.isfile(_tool("ffprobe"))


def has_runtime() -> bool:
    return os.path.isfile(_tool("qjs"))


def has_yt_tools() -> bool:
    return has_ytdlp() and has_ffmpeg() and has_runtime()


def is_valid_url(url: str) -> bool:
    return bool(url and _URL_RE.match(url.strip()))


def safe_filename(name: str) -> str:
    return _BAD_CHARS_RE.sub("_", name).strip().strip(".")[:150] or "video"


def _run(cmd: list[str], is_cancelled: Optional[Callable[[], bool]] = None, on_line: Optional[Callable[[str], None]] = None) -> tuple[int, str, str]:
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace", creationflags=_NO_WINDOW)
    err: list[str] = []
    out: list[str] = []
    finished = threading.Event()
    reader = threading.Thread(target=lambda: err.extend(proc.stderr), daemon=True)
    reader.start()

    def watch() -> None:
        while not finished.wait(0.25):
            if is_cancelled and is_cancelled():
                proc.kill()
                return

    if is_cancelled:
        threading.Thread(target=watch, daemon=True).start()
    try:
        for line in proc.stdout:
            if on_line:
                on_line(line.rstrip("\r\n"))
            else:
                out.append(line)
        proc.wait()
    finally:
        finished.set()
        reader.join(timeout=2)
    if is_cancelled and is_cancelled():
        raise YtError("Cancelled")
    return proc.returncode, "".join(out), "".join(err)


def _fail(stderr: str, fallback: str) -> YtError:
    text = stderr.strip()
    lowered = text.lower()
    for needle, message in _ERROR_HINTS:
        if needle in lowered:
            return YtError(message)
    lines = [l for l in text.splitlines() if l.strip()]
    return YtError(lines[-1] if lines else fallback)


def _ytdlp_args(*extra: str) -> list[str]:
    args = [_tool("yt-dlp"), "--no-warnings", "--no-playlist", "--ffmpeg-location", _dir()]
    if NET.has_proxy:
        args += ["--proxy", NET.proxy_url()]
    if NET.bind_ip():
        args += ["--source-address", NET.bind_ip()]
    if has_runtime():
        args += ["--js-runtimes", f"quickjs:{_tool('qjs')}", "--remote-components", "ejs:github"]
    return args + list(extra)


def _fmt_size(n: Optional[float]) -> str:
    if not n:
        return ""
    v = float(n)
    for unit in ("B", "KiB", "MiB", "GiB"):
        if v < 1024:
            return f"{v:.1f} {unit}"
        v /= 1024
    return f"{v:.1f} TiB"


def _fmt_duration(seconds: Optional[float]) -> str:
    if not seconds:
        return ""
    h, rem = divmod(int(seconds), 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def _codec(code: str) -> str:
    c = (code or "").lower()
    for prefix, name in (("av01", "AV1"), ("vp09", "VP9"), ("vp9", "VP9"), ("avc", "H.264"), ("hev", "H.265"), ("hvc", "H.265"), ("opus", "Opus"), ("mp4a", "AAC")):
        if c.startswith(prefix):
            return name
    return ""


def _size(f: dict) -> int:
    return int(f.get("filesize") or f.get("filesize_approx") or 0)


def _rate(f: dict) -> float:
    return f.get("vbr") or f.get("abr") or f.get("tbr") or 0


def _has(f: dict, key: str) -> bool:
    return (f.get(key) or "none") != "none"


def _video_candidates(formats: list[dict]) -> list[dict]:
    video_only = [f for f in formats if _has(f, "vcodec") and not _has(f, "acodec") and f.get("height")]
    return video_only or [f for f in formats if _has(f, "vcodec") and f.get("height")]


def _audio_candidates(formats: list[dict]) -> list[dict]:
    return [f for f in formats if _has(f, "acodec") and not _has(f, "vcodec") and _rate(f)]


def _video_tiers(formats: list[dict]) -> list[dict]:
    best: dict[int, dict] = {}
    for f in _video_candidates(formats):
        key = (f.get("ext") == "mp4", _rate(f))
        cur = best.get(f["height"])
        if cur is None or key > (cur.get("ext") == "mp4", _rate(cur)):
            best[f["height"]] = f
    tiers = []
    for height in sorted(best, reverse=True):
        f = best[height]
        fps = "60" if (f.get("fps") or 0) > 30 else ""
        details = ", ".join(x for x in (_codec(f.get("vcodec")), _fmt_size(_size(f))) if x)
        tiers.append({"id": f["format_id"], "label": f"{height}p{fps}" + (f" ({details})" if details else "")})
    return tiers


def _audio_tiers(formats: list[dict]) -> list[dict]:
    best: dict[int, dict] = {}
    for f in _audio_candidates(formats):
        bucket = round(_rate(f) / 16) * 16
        cur = best.get(bucket)
        if cur is None or (f.get("ext") == "m4a", _rate(f)) > (cur.get("ext") == "m4a", _rate(cur)):
            best[bucket] = f
    tiers = []
    for bucket in sorted(best, reverse=True):
        f = best[bucket]
        codec = _codec(f.get("acodec"))
        tiers.append({"id": f["format_id"], "label": f"{int(_rate(f))} kbps" + (f" ({codec})" if codec else "")})
    return tiers


def _subtitle_map(raw: dict) -> dict[str, dict]:
    """Manual captions only. Automatic captions trigger HTTP 429 on YouTube."""
    found: dict[str, dict] = {}
    for lang, variants in (raw.get("subtitles") or {}).items():
        if lang == "live_chat":
            continue
        usable = [v for v in variants if v.get("url") and v.get("ext") in ("srt", "vtt")]
        if usable:
            best = min(usable, key=lambda v: v["ext"] != "srt")
            found[lang] = {"url": best["url"], "ext": best["ext"], "name": best.get("name") or lang}
    return found


def manual_subtitles(info: dict) -> dict[str, dict]:
    return {lang: sub for lang, sub in (info.get("subtitles") or {}).items() if not sub.get("auto")}


def _build_info(raw: dict, url: str) -> dict:
    formats = [{k: f[k] for k in _FORMAT_KEYS if f.get(k) is not None} for f in raw.get("formats") or []]
    return {
        "url": url,
        "id": raw.get("id", ""),
        "title": raw.get("title") or "Unknown",
        "uploader": raw.get("uploader") or raw.get("channel") or "",
        "thumbnail": raw.get("thumbnail") or "",
        "duration_label": _fmt_duration(raw.get("duration")),
        "webpage_url": raw.get("webpage_url") or url,
        "formats": formats,
        "video_tiers": _video_tiers(formats),
        "audio_tiers": _audio_tiers(formats),
        "subtitles": _subtitle_map(raw),
        "fetched_at": time.time(),
    }


def fetch_info(url: str, use_cache: bool = True, is_cancelled: Optional[Callable[[], bool]] = None) -> dict:
    url = url.strip()
    if not is_valid_url(url):
        raise YtError("Invalid URL")
    if not has_ytdlp():
        raise YtError("yt-dlp not found")
    if use_cache:
        cached = cache.load(_NS, url, ttl_seconds=_TTL)
        if cached is not None:
            return cached
    code, out, err = _run(_ytdlp_args("--dump-single-json", "--skip-download", url), is_cancelled)
    if code != 0:
        raise _fail(err, "Failed to fetch video information")
    try:
        raw = json.loads(out)
    except json.JSONDecodeError as exc:
        raise YtError("Failed to parse video information") from exc
    if raw.get("_type") == "playlist":
        entries = raw.get("entries") or []
        if not entries:
            raise YtError("Playlist has no playable entries")
        raw = entries[0]
    info = _build_info(raw, url)
    cache.save(_NS, url, info)
    return info


def is_playlist_url(url: str) -> bool:
    parts = urlsplit(url.strip())
    query = parse_qs(parts.query)
    return "list" in query or parts.path.rstrip("/").endswith("/playlist")


def fetch_playlist(url: str, is_cancelled: Optional[Callable[[], bool]] = None) -> dict:
    if not has_ytdlp():
        raise YtError("yt-dlp not found")
    code, out, err = _run(_ytdlp_args("--yes-playlist", "--flat-playlist", "--dump-single-json", url), is_cancelled)
    if code != 0:
        raise _fail(err, "Failed to fetch playlist")
    try:
        raw = json.loads(out)
    except json.JSONDecodeError as exc:
        raise YtError("Failed to parse playlist") from exc
    entries = []
    for e in raw.get("entries") or []:
        link = e.get("url") or e.get("webpage_url") or ""
        if link and not link.startswith("http") and e.get("id"):
            link = f"https://www.youtube.com/watch?v={e['id']}"
        if link:
            entries.append({"url": link, "title": e.get("title") or e.get("id") or "Video", "duration": _fmt_duration(e.get("duration"))})
    if not entries:
        raise YtError("Playlist has no playable entries")
    return {"title": raw.get("title") or "Playlist", "entries": entries}


def parse_time(text: str) -> float:
    parts = text.strip().split(":")
    try:
        values = [float(p) for p in parts]
    except ValueError as exc:
        raise YtError(f"Invalid time: {text}") from exc
    if not 1 <= len(values) <= 3 or any(v < 0 for v in values):
        raise YtError(f"Invalid time: {text}")
    total = 0.0
    for v in values:
        total = total * 60 + v
    return total


def _is_direct(f: dict) -> bool:
    return f.get("protocol") in ("https", "http") and bool(f.get("url"))


def _by_id(info: dict, fid: str) -> Optional[dict]:
    return next((f for f in info["formats"] if f.get("format_id") == fid), None)


def _pick_video(info: dict, fid: str, max_height: int) -> Optional[dict]:
    chosen = _by_id(info, fid) if fid else None
    if chosen:
        return chosen
    candidates = [_by_id(info, t["id"]) for t in info["video_tiers"]]
    candidates = [f for f in candidates if f]
    if max_height:
        fitting = [f for f in candidates if f["height"] <= max_height]
        candidates = fitting or candidates[-1:]
    return candidates[0] if candidates else None


def _pick_audio(info: dict, fid: str, prefer_m4a: bool) -> Optional[dict]:
    chosen = _by_id(info, fid) if fid else None
    if chosen:
        return chosen
    candidates = _audio_candidates(info["formats"])
    if prefer_m4a:
        m4a = [f for f in candidates if f.get("ext") == "m4a"]
        candidates = m4a or candidates
    originals = [f for f in candidates if (f.get("language_preference") or 0) >= 10]
    candidates = originals or candidates
    return max(candidates, key=_rate, default=None)


def _unique_path(dest_dir: str, base: str, ext: str) -> str:
    path = os.path.join(dest_dir, f"{base}.{ext}")
    n = 1
    while os.path.exists(path):
        path = os.path.join(dest_dir, f"{base} ({n}).{ext}")
        n += 1
    return path


_PATH_LOCK = threading.Lock()
_RESERVED_BASES: set[str] = set()


def _base_taken(dest_dir: str, base: str) -> bool:
    if base in _RESERVED_BASES:
        return True
    prefix = base + "."
    try:
        return any(name.startswith(prefix) for name in os.listdir(dest_dir))
    except OSError:
        return False


def _unique_base(dest_dir: str, base: str) -> str:
    with _PATH_LOCK:
        candidate = base
        n = 1
        while _base_taken(dest_dir, candidate):
            candidate = f"{base} ({n})"
            n += 1
        _RESERVED_BASES.add(candidate)
        return candidate


def _release_base(base: str) -> None:
    with _PATH_LOCK:
        _RESERVED_BASES.discard(base)


def _download_streams(streams: list[tuple[dict, str]], on_progress: Callable[[int, int, float], None], is_cancelled: Optional[Callable[[], bool]]) -> None:
    state: dict[int, tuple[int, int, float]] = {}
    lock = threading.Lock()
    estimate = sum(_size(f) for f, _ in streams)
    jobs: list[SegmentedDownload] = []
    errors: list[BaseException] = []

    def reporter(index: int) -> Callable[[int, int, float], None]:
        def report(n: int, total: int, speed: float) -> None:
            with lock:
                state[index] = (n, total, speed)
                known = sum(t for _, t, _ in state.values())
                total_all = known if len(state) == len(streams) and known else max(known, estimate)
                on_progress(sum(v[0] for v in state.values()), total_all, sum(v[2] for v in state.values()))
        return report

    def run(job: SegmentedDownload) -> None:
        try:
            job.run()
        except BaseException as exc:
            errors.append(exc)
            for other in jobs:
                other.cancel()

    for i, (f, path) in enumerate(streams):
        jobs.append(SegmentedDownload(f["url"], path, headers=f.get("http_headers"), connections=_CONNECTIONS, window=_WINDOW, on_progress=reporter(i)))
    threads = [threading.Thread(target=run, args=(j,), daemon=True) for j in jobs]
    for t in threads:
        t.start()
    while any(t.is_alive() for t in threads):
        if is_cancelled and is_cancelled():
            for j in jobs:
                j.cancel()
        for t in threads:
            t.join(0.2)
    real = [e for e in errors if not isinstance(e, Cancelled)]
    if real:
        raise real[0]
    if errors or (is_cancelled and is_cancelled()):
        raise Cancelled()


def _mux(inputs: list[str], out: str, audio_only: bool, title: str, is_cancelled: Optional[Callable[[], bool]]) -> None:
    cmd = [_tool("ffmpeg"), "-y", "-hide_banner", "-loglevel", "error"]
    for path in inputs:
        cmd += ["-i", path]
    if audio_only:
        cmd += ["-vn", "-c:a", "libmp3lame", "-q:a", "2"]
    else:
        cmd += ["-map", "0:v:0", "-map", "1:a:0", "-c", "copy", "-movflags", "+faststart"] if len(inputs) == 2 else ["-c", "copy", "-movflags", "+faststart"]
    cmd += ["-metadata", f"title={title}", out]
    code, _, err = _run(cmd, is_cancelled)
    if code != 0:
        raise _fail(err, "ffmpeg failed")


def _download_direct(info: dict, video: Optional[dict], audio: Optional[dict], dest_dir: str, base: str, on_progress, is_cancelled) -> str:
    streams = []
    for role, f in (("v", video), ("a", audio)):
        if f:
            streams.append((f, os.path.join(dest_dir, f".{base}.{role}.{f.get('ext', 'bin')}")))
    try:
        _download_streams(streams, on_progress, is_cancelled)
    except Cancelled:
        raise YtError("Cancelled") from None
    except HttpError as exc:
        raise YtError(str(exc)) from exc
    if video:
        mp4 = video.get("ext") == "mp4" and (audio is None or audio.get("ext") in ("m4a", "mp4"))
        ext = "mp4" if mp4 else "mkv"
    else:
        ext = "mp3"
    out = _unique_path(dest_dir, base, ext)
    try:
        _mux([p for _, p in streams], out, video is None, info["title"], is_cancelled)
    finally:
        for _, p in streams:
            for leftover in (p, p + ".ghd", p + ".ghd.tmp"):
                try:
                    os.remove(leftover)
                except OSError:
                    pass
    return out


def _parse_progress(line: str) -> Optional[tuple[int, int, float]]:
    parts = line.split()
    if len(parts) != 5 or parts[0] != "YT_PROGRESS":
        return None
    try:
        done = int(float(parts[1]))
        total = int(float(parts[2])) if parts[2] != "NA" else int(float(parts[3])) if parts[3] != "NA" else 0
        speed = float(parts[4]) if parts[4] != "NA" else 0.0
    except ValueError:
        return None
    return done, total, speed


def _download_fallback(info: dict, dest_dir: str, base: str, mode: str, format_id: str, max_height: int, subtitle: str, clip: Optional[tuple[float, Optional[float]]], on_progress, is_cancelled) -> str:
    out_base = _unique_base(dest_dir, base)
    args = ["--newline", "--no-simulate", "-N", str(_CONNECTIONS), "--progress-template", _PROGRESS_TEMPLATE, "--print", "after_move:filepath", "-o", os.path.join(dest_dir, out_base.replace("%", "%%") + ".%(ext)s"), "--add-metadata"]
    cap = f"[height<={max_height}]" if max_height else ""
    if mode == "audio":
        args += ["-f", f"{format_id}/bestaudio/best" if format_id else "bestaudio/best", "-x", "--audio-format", "mp3", "--audio-quality", "2"]
    elif format_id:
        args += ["-f", f"{format_id}+ba/{format_id}/bv*+ba/b", "--merge-output-format", "mp4/mkv"]
    else:
        args += ["-f", f"bv*{cap}+ba/b{cap}/b", "--merge-output-format", "mp4/mkv"]
    if subtitle:
        args += ["--write-subs", "--sub-langs", subtitle, "--convert-subs", "srt"]
    if clip:
        end = "inf" if clip[1] is None else clip[1]
        args += ["--download-sections", f"*{clip[0]}-{end}", "--force-keyframes-at-cuts"]
    final: list[str] = []

    def on_line(line: str) -> None:
        parsed = _parse_progress(line)
        if parsed:
            on_progress(*parsed)
        elif line.strip():
            final.append(line.strip())

    try:
        code, _, err = _run(_ytdlp_args(*args, info["webpage_url"]), is_cancelled, on_line)
        if code != 0:
            raise _fail(err, "yt-dlp failed")
        if not final or not os.path.isfile(final[-1]):
            raise YtError("Download finished but the output file was not found")
        return final[-1]
    finally:
        _release_base(out_base)


def _fetch_subtitle(info: dict, lang: str, out_stem: str, is_cancelled: Optional[Callable[[], bool]]) -> None:
    entry = manual_subtitles(info).get(lang)
    if not entry:
        return
    session = new_session(1)
    try:
        r = session.get(entry["url"], timeout=30)
        r.raise_for_status()
        if not r.content:
            raise ValueError("empty subtitle response")
        raw = out_stem + f".{lang}.{entry['ext']}"
        with open(raw, "wb") as fh:
            fh.write(r.content)
    except Exception:
        logger.warning("Manual subtitle download failed for %s", lang)
        return
    finally:
        session.close()
    if entry["ext"] == "vtt":
        srt = out_stem + f".{lang}.srt"
        code, _, _ = _run([_tool("ffmpeg"), "-y", "-hide_banner", "-loglevel", "error", "-i", raw, srt], is_cancelled)
        if code == 0:
            os.remove(raw)


def download(
    info: dict, dest_dir: str, mode: str, format_id: str, on_progress: Callable[[int, int, float], None], is_cancelled: Optional[Callable[[], bool]] = None,
    *, max_height: int = 0, subtitle: str = "", clip: Optional[tuple[float, Optional[float]]] = None,
) -> str:
    if not has_yt_tools():
        raise YtError("yt-dlp/ffmpeg not found")
    os.makedirs(dest_dir, exist_ok=True)
    base = safe_filename(info["title"]) + (" [clip]" if clip else "")
    for attempt in (0, 1):
        if time.time() - info.get("fetched_at", 0) > _STALE_AFTER or attempt:
            info = fetch_info(info["url"], use_cache=False, is_cancelled=is_cancelled)
        if mode == "audio":
            video, audio = None, _pick_audio(info, format_id, False)
        else:
            video = _pick_video(info, format_id, max_height)
            audio = None if video is None or _has(video, "acodec") else _pick_audio(info, "", video.get("ext") == "mp4")
        selected = [f for f in (video, audio) if f]
        if clip or not selected or not all(_is_direct(f) for f in selected):
            return _download_fallback(info, dest_dir, base, mode, format_id, max_height, subtitle, clip, on_progress, is_cancelled)
        try:
            out = _download_direct(info, video, audio, dest_dir, base, on_progress, is_cancelled)
        except YtError as exc:
            if attempt or "403" not in str(exc):
                raise
            continue
        if subtitle:
            _fetch_subtitle(info, subtitle, os.path.splitext(out)[0], is_cancelled)
        return out
    raise YtError("Download failed")


def _install(url: str, dest: str, on_progress: Callable[[int, int], None], is_cancelled: Optional[Callable[[], bool]]) -> None:
    tmp = dest + ".download"
    job = SegmentedDownload(url, tmp, connections=4, on_progress=lambda n, t, _s: (on_progress(n, t), job.cancel() if is_cancelled and is_cancelled() else None))
    try:
        job.run()
    except Cancelled:
        raise YtError("Cancelled") from None
    except Exception as exc:
        raise YtError(str(exc)) from exc
    os.replace(tmp, dest)
    if os.name != "nt":
        os.chmod(dest, os.stat(dest).st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


def _install_ffmpeg(on_progress: Callable[[int, int], None], is_cancelled: Optional[Callable[[], bool]]) -> None:
    archive = os.path.join(_dir(), "_ffmpeg.zip")
    extract = os.path.join(_dir(), "_extract")
    try:
        _install(_FFMPEG_ZIP, archive, on_progress, is_cancelled)
        shutil.rmtree(extract, ignore_errors=True)
        with zipfile.ZipFile(archive) as zf:
            zf.extractall(extract)
        wanted = {"ffmpeg" + _EXE: _tool("ffmpeg"), "ffprobe" + _EXE: _tool("ffprobe")}
        found = {}
        for root, _dirs, files in os.walk(extract):
            for name in files:
                if name.lower() in wanted and name.lower() not in found:
                    found[name.lower()] = os.path.join(root, name)
        if len(found) != len(wanted):
            raise YtError("Downloaded archive did not contain ffmpeg/ffprobe binaries")
        for name, src in found.items():
            shutil.move(src, wanted[name])
            if os.name != "nt":
                os.chmod(wanted[name], os.stat(wanted[name]).st_mode | stat.S_IEXEC)
    except zipfile.BadZipFile as exc:
        raise YtError(str(exc)) from exc
    finally:
        shutil.rmtree(extract, ignore_errors=True)
        try:
            os.remove(archive)
        except OSError:
            pass


def install_ffmpeg(on_progress: Callable[[int, int], None],
                   is_cancelled: Optional[Callable[[], bool]] = None) -> None:
    """Install only ffmpeg/ffprobe into the managed tools folder."""
    os.makedirs(_dir(), exist_ok=True)
    _install_ffmpeg(on_progress, is_cancelled)


def _qjs_asset() -> str:
    machine = platform.machine().lower()
    if sys.platform == "win32":
        return f"qjs-windows-{'x86' if machine in ('x86', 'i386', 'i686') else 'x86_64'}.exe"
    if sys.platform == "darwin":
        return f"qjs-darwin-{machine}"
    return f"qjs-linux-{'aarch64' if machine in ('arm64', 'aarch64') else 'x86_64'}"


def download_yt_tools(on_progress: Callable[[str, int, int], None], is_cancelled: Optional[Callable[[], bool]] = None) -> None:
    os.makedirs(_dir(), exist_ok=True)
    steps = (
        (has_ytdlp, "yt-dlp", lambda cb: _install(_ASSET_BASE + _YTDLP_ASSET, _tool("yt-dlp"), cb, is_cancelled)),
        (has_runtime, "quickjs", lambda cb: _install("https://github.com/quickjs-ng/quickjs/releases/latest/download/" + _qjs_asset(), _tool("qjs"), cb, is_cancelled)),
        (has_ffmpeg, "ffmpeg", lambda cb: _install_ffmpeg(cb, is_cancelled)),
    )
    for present, name, install in steps:
        if not present():
            install(lambda n, t, _name=name: on_progress(_name, n, t))
