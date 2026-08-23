from __future__ import annotations
import json
import logging
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Optional
logger = logging.getLogger(__name__)
_SPOTDL_CMD = [sys.executable, '-m', 'spotdl']
_RESOLVE_TIMEOUT_S = 900
_DOWNLOAD_TIMEOUT_S = 300

# Matches spotdl/yt-dlp style progress lines, e.g.:
#   Downloading "Song Name":  45%|####5     | 45/100
#   Downloaded "Song Name": 100%
_PROGRESS_RE = re.compile(r'(\d{1,3}(?:\.\d+)?)\s*%')

class SpotDLCancelled(Exception):
    pass

@dataclass(slots=True)
class SpotSong:
    name: str
    artists: list
    album_name: Optional[str]
    duration_ms: int
    cover_url: Optional[str]
    isrc: Optional[str]
    url: str
    explicit: bool = False
    release_date: Optional[str] = None
    native: object = field(default=None, repr=False, compare=False)

    @property
    def artist_str(self) -> str:
        return ', '.join(self.artists) if self.artists else 'Unknown Artist'

    @property
    def duration_label(self) -> str:
        total_seconds = int(self.duration_ms / 1000)
        return f'{total_seconds // 60}:{total_seconds % 60:02d}'

    @classmethod
    def from_spotdl_json(cls, data: dict) -> 'SpotSong':
        duration_s = data.get('duration') or 0
        return cls(name=data.get('name', '') or '', artists=list(data.get('artists') or []), album_name=data.get('album_name'), duration_ms=int(float(duration_s) * 1000), cover_url=data.get('cover_url'), isrc=data.get('isrc'), url=data.get('url', '') or '', explicit=bool(data.get('explicit', False)), release_date=data.get('date'), native=data)

def _run_spotdl(args: list[str], *, timeout_s: float, is_cancelled: Optional[Callable[[], bool]]=None, poll_interval: float=0.2) -> tuple[int, str, str]:
    cmd = [*_SPOTDL_CMD, *args]
    logger.info('Running: %s', ' '.join(cmd))
    proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    start = time.monotonic()
    try:
        while True:
            try:
                stdout, stderr = proc.communicate(timeout=poll_interval)
                return (proc.returncode, stdout, stderr)
            except subprocess.TimeoutExpired:
                pass
            if is_cancelled is not None and is_cancelled():
                logger.info('spotdl call cancelled, terminating process')
                _kill(proc)
                raise SpotDLCancelled()
            if time.monotonic() - start > timeout_s:
                logger.warning('spotdl call timed out after %ss, killing it', timeout_s)
                _kill(proc)
                raise RuntimeError(f'spotdl timed out after {int(timeout_s)}s with no response.')
    except SpotDLCancelled:
        raise
    except RuntimeError:
        raise
    except Exception:
        _kill(proc)
        raise

def _run_spotdl_streaming(args: list[str], *, timeout_s: float, on_line: Optional[Callable[[str], None]]=None, is_cancelled: Optional[Callable[[], bool]]=None, poll_interval: float=0.2) -> tuple[int, str, str]:
    """Like _run_spotdl, but reads stdout line-by-line and invokes on_line(line)
    as output arrives, so callers can surface live progress instead of waiting
    for the whole process to finish."""
    cmd = [*_SPOTDL_CMD, *args]
    logger.info('Running: %s', ' '.join(cmd))
    proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1)
    start = time.monotonic()
    stdout_lines: list[str] = []

    def _pump_stdout() -> None:
        try:
            for line in proc.stdout:
                stdout_lines.append(line)
                if on_line is not None:
                    try:
                        on_line(line)
                    except Exception:
                        logger.exception('on_line callback failed')
        except Exception:
            pass

    reader = threading.Thread(target=_pump_stdout, daemon=True)
    reader.start()
    try:
        while True:
            returncode = proc.poll()
            if returncode is not None:
                reader.join(timeout=2)
                stderr = proc.stderr.read() if proc.stderr else ''
                return (returncode, ''.join(stdout_lines), stderr)
            if is_cancelled is not None and is_cancelled():
                logger.info('spotdl call cancelled, terminating process')
                _kill(proc)
                raise SpotDLCancelled()
            if time.monotonic() - start > timeout_s:
                logger.warning('spotdl call timed out after %ss, killing it', timeout_s)
                _kill(proc)
                raise RuntimeError(f'spotdl timed out after {int(timeout_s)}s with no response.')
            time.sleep(poll_interval)
    except SpotDLCancelled:
        raise
    except RuntimeError:
        raise
    except Exception:
        _kill(proc)
        raise

def _parse_progress_pct(line: str) -> Optional[float]:
    match = _PROGRESS_RE.search(line)
    if not match:
        return None
    try:
        pct = float(match.group(1))
    except ValueError:
        return None
    return max(0.0, min(100.0, pct))

def _kill(proc: subprocess.Popen) -> None:
    try:
        proc.terminate()
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=3)
    except Exception:
        logger.exception('Failed to kill spotdl subprocess')

def resolve(query_or_url: str, is_cancelled: Optional[Callable[[], bool]]=None) -> list[SpotSong]:
    with tempfile.TemporaryDirectory() as tmp_dir:
        save_path = os.path.join(tmp_dir, 'result.spotdl')
        args = ['save', query_or_url, '--save-file', save_path]
        returncode, stdout, stderr = _run_spotdl(args, timeout_s=_RESOLVE_TIMEOUT_S, is_cancelled=is_cancelled)
        if returncode != 0 or not os.path.exists(save_path):
            logger.warning('spotdl save exited %s\nstdout: %s\nstderr: %s', returncode, stdout, stderr)
            raise RuntimeError((stderr or stdout or 'Lookup failed').strip()[-500:])
        with open(save_path, 'r', encoding='utf-8') as f:
            raw_entries = json.load(f)
    if not isinstance(raw_entries, list):
        raw_entries = [raw_entries]
    return [SpotSong.from_spotdl_json(entry) for entry in raw_entries]

def download(songs: list[SpotSong], output_dir: str, is_cancelled: Optional[Callable[[], bool]]=None, on_progress: Optional[Callable[[SpotSong, float], None]]=None) -> list[tuple[SpotSong, bool, str]]:
    """Download songs via spotdl. If on_progress is given, it's called as
    on_progress(song, percent) whenever spotdl reports incremental progress
    on stdout, so callers can drive a live progress bar instead of only
    finding out about success/failure once the whole download finishes."""
    os.makedirs(output_dir, exist_ok=True)
    output: list[tuple[SpotSong, bool, str]] = [(song, False, '') for song in songs]
    for idx, song in enumerate(songs):
        if is_cancelled is not None and is_cancelled():
            break
        query = song.url or song.name
        before = set(os.listdir(output_dir))
        args = ['download', query, '--output', f'{output_dir}/{{artist}} - {{title}}.{{output-ext}}']

        def _handle_line(line: str, song=song) -> None:
            pct = _parse_progress_pct(line)
            if pct is not None and on_progress is not None:
                on_progress(song, pct)

        try:
            if on_progress is not None:
                returncode, stdout, stderr = _run_spotdl_streaming(args, timeout_s=_DOWNLOAD_TIMEOUT_S, on_line=_handle_line, is_cancelled=is_cancelled)
            else:
                returncode, stdout, stderr = _run_spotdl(args, timeout_s=_DOWNLOAD_TIMEOUT_S, is_cancelled=is_cancelled)
        except SpotDLCancelled:
            break
        ok = False
        path = ''
        after = set(os.listdir(output_dir))
        new_files = [f for f in after - before if os.path.isfile(os.path.join(output_dir, f))]
        if returncode == 0 and new_files:
            ok = True
            path = os.path.join(output_dir, new_files[0])
        if not ok:
            logger.warning('spotdl download exited %s for %r\nstdout: %s\nstderr: %s', returncode, query, stdout, stderr)
        elif on_progress is not None:
            on_progress(song, 100.0)
        output[idx] = (song, ok, path)
    return output