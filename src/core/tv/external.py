"""Native Vyla provider integration.

Vyla is a JavaScript project. PiraChest runs its code in a dedicated Node
process instead of asking the user to install an SDK or a separate runtime:
``vyla_bridge.mjs`` loads the vendored Vyla SDK and resolves TMDB titles.

The Python side owns provisioning, caching, timeouts and the translation into
:class:`~src.core.tv.streams.StreamSource`. A broken Node process is
contained: the bridge is restarted and the rest of the stream search keeps
working.
"""
from __future__ import annotations

import asyncio
import atexit
import json
import logging
import os
import shutil
import ssl
import subprocess
import sys
import tarfile
import threading
import time
import urllib.request
import zipfile
from typing import Any, Optional

from ..config import paths, settings
from .streams import StreamSource, quality_of

logger = logging.getLogger(__name__)

_NODE_VERSION = "22.23.2"
_MIN_NODE_MAJOR = 18
_DEPS = {
    "node-tls-client": "^2.1.0",
}
_ASSET_VERSION = "2"
_TIMEOUT_FLOOR = 8

_BUNDLED_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "node")
_BUNDLED_VYLA = os.path.join(_BUNDLED_DIR, "vyla_sdk")
_INTEGRATIONS_DIR = os.path.join(paths.app_data_dir, "integrations")
_VYLA_DIR = os.path.join(_INTEGRATIONS_DIR, "vyla")
_RUNTIME_DIR = os.path.join(paths.app_data_dir, "runtime", "node")

_VYLA_BRIDGE = os.path.join(_INTEGRATIONS_DIR, "vyla_bridge.mjs")

_provision_lock = threading.Lock()
_sync_lock = threading.Lock()
_provision_thread: Optional[threading.Thread] = None
_provision_done = threading.Event()
_provision_error = ""
_provision_started = 0.0
_node_path: Optional[str] = None
_ready = False

_cache: dict[str, tuple[float, Any]] = {}
_cache_lock = threading.Lock()

_bridges: dict[str, "_Bridge"] = {}
_bridges_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Node runtime + dependency provisioning
# ---------------------------------------------------------------------------

def _creationflags() -> int:
    return 0x08000000 if os.name == "nt" else 0


def _node_candidates() -> list[str]:
    candidates: list[str] = []
    configured = (getattr(settings, "tv_node_path", "") or "").strip().strip('"')
    if configured:
        candidates.append(configured)
    found = shutil.which("node")
    if found:
        candidates.append(found)
    if os.name == "nt":
        candidates.extend([
            os.path.join(_RUNTIME_DIR, "node.exe"),
            os.path.join(os.environ.get("LOCALAPPDATA", ""), "pi-node", "current", "node.exe"),
            os.path.join(os.environ.get("ProgramFiles", ""), "nodejs", "node.exe"),
            os.path.join(os.environ.get("ProgramFiles(x86)", ""), "nodejs", "node.exe"),
        ])
    else:
        candidates.extend([
            os.path.join(_RUNTIME_DIR, "bin", "node"),
            "/usr/bin/node",
            "/usr/local/bin/node",
            "/opt/homebrew/bin/node",
        ])
    out: list[str] = []
    for candidate in candidates:
        if candidate and candidate not in out:
            out.append(candidate)
    return out


def _valid_node(path: str) -> bool:
    if not path or not os.path.isfile(path):
        return False
    try:
        proc = subprocess.run(
            [path, "--version"], capture_output=True, text=True, timeout=4,
            creationflags=_creationflags(),
        )
        if proc.returncode != 0:
            return False
        version = proc.stdout.strip()
        if not version.startswith("v"):
            return False
        try:
            major = int(version[1:].split(".", 1)[0])
        except ValueError:
            return False
        return major >= _MIN_NODE_MAJOR
    except (OSError, subprocess.SubprocessError):
        return False


def find_node() -> Optional[str]:
    global _node_path
    if _node_path and os.path.isfile(_node_path):
        return _node_path
    for candidate in _node_candidates():
        if _valid_node(candidate):
            _node_path = candidate
            return candidate
    _node_path = None
    return None


def _deps_ready() -> bool:
    marker = os.path.join(_INTEGRATIONS_DIR, ".deps-ready")
    if not os.path.isfile(marker):
        return False
    try:
        with open(marker, encoding="utf-8") as fh:
            if fh.read().strip() != _ASSET_VERSION:
                return False
    except OSError:
        return False
    modules = os.path.join(_INTEGRATIONS_DIR, "node_modules")
    return all(os.path.isdir(os.path.join(modules, name)) for name in _DEPS)


def runtime_status() -> dict:
    node = find_node()
    return {
        "node": bool(node),
        "node_path": node or "",
        "deps": _deps_ready(),
        "ready": bool(node and _deps_ready()),
        "provisioning": bool(_provision_thread and _provision_thread.is_alive()),
        "error": _provision_error,
    }


def _download_node() -> Optional[str]:
    arch = {"x86_64": "x64", "amd64": "x64", "aarch64": "arm64", "arm64": "arm64"}.get(
        os.uname().machine.lower() if hasattr(os, "uname") else os.environ.get("PROCESSOR_ARCHITECTURE", "AMD64").lower(),
        "x64",
    )
    if os.name == "nt":
        asset = f"node-v{_NODE_VERSION}-win-{arch}.zip"
        kind = "zip"
    elif sys.platform == "darwin":
        asset = f"node-v{_NODE_VERSION}-darwin-{arch}.tar.gz"
        kind = "tar"
    else:
        asset = f"node-v{_NODE_VERSION}-linux-{arch}.tar.xz"
        kind = "tar"
    url = f"https://nodejs.org/dist/v{_NODE_VERSION}/{asset}"
    os.makedirs(_RUNTIME_DIR, exist_ok=True)
    archive = os.path.join(_RUNTIME_DIR, asset)
    logger.info("Downloading Node.js runtime from %s", url)
    request = urllib.request.Request(url, headers={"User-Agent": "PiraChest"})
    context = ssl.create_default_context()
    with urllib.request.urlopen(request, timeout=60, context=context) as response, open(archive, "wb") as fh:
        shutil.copyfileobj(response, fh, length=1024 * 256)
    extract_dir = os.path.join(_RUNTIME_DIR, "_extract")
    shutil.rmtree(extract_dir, ignore_errors=True)
    os.makedirs(extract_dir, exist_ok=True)
    if kind == "zip":
        with zipfile.ZipFile(archive) as zf:
            zf.extractall(extract_dir)
    else:
        with tarfile.open(archive, "r:*") as tf:
            tf.extractall(extract_dir)
    try:
        os.remove(archive)
    except OSError:
        pass
    for root, _dirs, names in os.walk(extract_dir):
        for name in names:
            if name == "node.exe" or (name == "node" and os.access(os.path.join(root, name), os.X_OK)):
                candidate = os.path.join(root, name)
                if not _valid_node(candidate):
                    continue
                # Keep the runtime at a stable path so the next launch finds it
                # without downloading again.
                source_root = os.path.dirname(candidate)
                if os.name != "nt" and os.path.basename(source_root) == "bin":
                    # Unix tarballs keep node in bin/ and npm in lib/, so move
                    # the whole archive root instead of only bin/.
                    source_root = os.path.dirname(source_root)
                relative = os.path.relpath(candidate, source_root)
                if os.path.abspath(source_root) != os.path.abspath(_RUNTIME_DIR):
                    for entry in os.listdir(source_root):
                        source = os.path.join(source_root, entry)
                        target = os.path.join(_RUNTIME_DIR, entry)
                        if os.path.isdir(target) and not os.path.islink(target):
                            shutil.rmtree(target, ignore_errors=True)
                        elif os.path.exists(target):
                            os.remove(target)
                        shutil.move(source, target)
                shutil.rmtree(extract_dir, ignore_errors=True)
                installed = os.path.join(_RUNTIME_DIR, relative)
                return installed if _valid_node(installed) else candidate
    raise RuntimeError("The downloaded Node.js archive did not contain a usable node executable")


def _copy_if_changed(source: str, target: str) -> None:
    if not os.path.isfile(source):
        return
    try:
        if os.path.isfile(target) and os.path.getsize(source) == os.path.getsize(target) and \
                os.path.getmtime(source) <= os.path.getmtime(target):
            return
    except OSError:
        pass
    os.makedirs(os.path.dirname(target), exist_ok=True)
    shutil.copy2(source, target)


def _sync_assets() -> None:
    with _sync_lock:
        _sync_assets_unlocked()


def _sync_assets_unlocked() -> None:
    os.makedirs(_INTEGRATIONS_DIR, exist_ok=True)
    marker = os.path.join(_INTEGRATIONS_DIR, ".assets")
    signature = _ASSET_VERSION
    current = ""
    try:
        with open(marker, encoding="utf-8") as fh:
            current = fh.read().strip()
    except OSError:
        pass
    _copy_if_changed(os.path.join(_BUNDLED_DIR, "vyla_bridge.mjs"), _VYLA_BRIDGE)
    if current != signature or not os.path.isdir(_VYLA_DIR):
        if os.path.isdir(_BUNDLED_VYLA):
            shutil.copytree(_BUNDLED_VYLA, _VYLA_DIR, dirs_exist_ok=True)
        with open(marker, "w", encoding="utf-8") as fh:
            fh.write(signature)
    package = os.path.join(_INTEGRATIONS_DIR, "package.json")
    package_data = {"private": True, "name": "pirachest-integrations", "version": "1.0.0",
                    "dependencies": _DEPS}
    try:
        with open(package, encoding="utf-8") as fh:
            current_package = fh.read()
    except OSError:
        current_package = ""
    wanted_package = json.dumps(package_data, indent=2)
    if current_package.strip() != wanted_package.strip():
        with open(package, "w", encoding="utf-8") as fh:
            fh.write(wanted_package)
    notice = os.path.join(_INTEGRATIONS_DIR, "NOTICE.md")
    if not os.path.isfile(notice):
        with open(notice, "w", encoding="utf-8") as fh:
            fh.write(
                "PiraChest native provider runtime\n\n"
                "Vyla SDK: (c) Vyla Entertainment, licensed CC BY-NC 4.0.\n"
                "https://gitlab.com/vyla-entertainment/sdk\n"
            )


def _npm_command(node: str) -> Optional[list[str]]:
    node_dir = os.path.dirname(node)
    candidates = [
        os.path.join(node_dir, "node_modules", "npm", "bin", "npm-cli.js"),
        os.path.join(node_dir, "lib", "node_modules", "npm", "bin", "npm-cli.js"),
        os.path.join(node_dir, "..", "lib", "node_modules", "npm", "bin", "npm-cli.js"),
    ]
    for candidate in candidates:
        if os.path.isfile(candidate):
            return [node, os.path.abspath(candidate)]
    npm = shutil.which("npm")
    if npm:
        if os.name == "nt" and npm.lower().endswith((".cmd", ".bat")):
            return ["cmd.exe", "/c", npm]
        return [npm]
    return None


def _install_deps(node: str) -> None:
    if _deps_ready():
        return
    npm = _npm_command(node)
    if not npm:
        raise RuntimeError("npm was not found next to the Node.js runtime")
    cmd = npm + ["install", "--omit=dev", "--no-audit", "--no-fund", "--loglevel=error"]
    logger.info("Installing native provider dependencies in %s", _INTEGRATIONS_DIR)
    proc = subprocess.run(
        cmd, cwd=_INTEGRATIONS_DIR, capture_output=True, text=True, timeout=300,
        creationflags=_creationflags(),
    )
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip().splitlines()
        tail = " | ".join(detail[-4:]) if detail else f"exit {proc.returncode}"
        raise RuntimeError(f"npm install failed: {tail}")
    marker = os.path.join(_INTEGRATIONS_DIR, ".deps-ready")
    with open(marker, "w", encoding="utf-8") as fh:
        fh.write(_ASSET_VERSION)


def _provision_worker() -> None:
    global _provision_error, _node_path, _ready
    try:
        node = find_node() or _download_node()
        if not node or not _valid_node(node):
            raise RuntimeError("Node.js runtime is not available")
        _node_path = node
        _sync_assets()
        _install_deps(node)
        _ready = True
        _provision_error = ""
        logger.info("Native provider runtime is ready (%s)", node)
    except Exception as exc:
        _provision_error = str(exc) or type(exc).__name__
        logger.warning("Native provider runtime setup failed: %s", _provision_error)
    finally:
        _provision_done.set()


def ensure_runtime(wait: bool = False, force: bool = False) -> bool:
    """Start (or wait for) Node + npm dependency provisioning."""
    global _provision_thread, _provision_started
    if find_node() and _deps_ready():
        _sync_assets()
        return True
    with _provision_lock:
        alive = bool(_provision_thread and _provision_thread.is_alive())
        if not alive:
            if not force and _provision_error and time.time() - _provision_started < 60:
                return False
            _provision_started = time.time()
            _provision_done.clear()
            _provision_thread = threading.Thread(
                target=_provision_worker, daemon=True, name="pirachest-providers-setup",
            )
            _provision_thread.start()
    if wait:
        _provision_done.wait(180)
    return bool(find_node() and _deps_ready())


def _ensure_ready_for_request() -> None:
    status = runtime_status()
    if status["ready"]:
        # Deploy updated bridge assets even when the runtime was provisioned
        # by an older build.
        _sync_assets()
        return
    if not getattr(settings, "tv_external_auto_setup", True):
        raise _BridgeUnavailable("The native provider runtime is not set up")
    ensure_runtime(wait=False)
    # If Node is already present, a dependency install normally takes a few
    # seconds. Give it a short window so the first search can still succeed
    # without making the UI wait for a full Node download.
    if status["node"]:
        _provision_done.wait(7)
    if not (find_node() and _deps_ready()):
        raise _BridgeUnavailable(_provision_error or "The native provider runtime is still being prepared")


def shutdown_external() -> None:
    with _bridges_lock:
        bridges = list(_bridges.values())
        _bridges.clear()
    for bridge in bridges:
        bridge.stop()


atexit.register(shutdown_external)


# ---------------------------------------------------------------------------
# Bridge process
# ---------------------------------------------------------------------------

class _BridgeError(RuntimeError):
    pass


class _BridgeUnavailable(_BridgeError):
    pass


class _Bridge:
    _IDLE_STOP_SECONDS = 180

    def __init__(self, script: str, name: str) -> None:
        self.script = script
        self.name = name
        self._proc: Optional[subprocess.Popen] = None
        self._pending: dict[str, dict] = {}
        self._pending_lock = threading.Lock()
        self._write_lock = threading.RLock()
        self._idle_lock = threading.Lock()
        self._idle_timer: Optional[threading.Timer] = None
        self._seq = 0

    def _start(self) -> None:
        _ensure_ready_for_request()
        node = find_node()
        if not node:
            raise _BridgeUnavailable("Node.js runtime is not available")
        env = os.environ.copy()
        env["PIRACHEST_INTEGRATIONS_DIR"] = _INTEGRATIONS_DIR
        env["VYLA_SDK_PATH"] = _VYLA_DIR
        env["NODE_NO_WARNINGS"] = "1"
        proc = subprocess.Popen(
            [node, self.script],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", errors="replace", bufsize=1,
            cwd=_INTEGRATIONS_DIR, env=env, creationflags=_creationflags(),
        )
        self._proc = proc
        threading.Thread(target=self._read_stdout, args=(proc,), daemon=True,
                         name=f"{self.name}-stdout").start()
        threading.Thread(target=self._read_stderr, args=(proc,), daemon=True,
                         name=f"{self.name}-stderr").start()

    def _read_stdout(self, proc: subprocess.Popen) -> None:
        try:
            for line in proc.stdout or []:
                text = line.strip()
                if not text:
                    continue
                try:
                    message = json.loads(text)
                except ValueError:
                    logger.debug("%s bridge output: %s", self.name, text[:300])
                    continue
                request_id = str(message.get("id") or "")
                with self._pending_lock:
                    waiter = self._pending.pop(request_id, None)
                if waiter is not None:
                    waiter["message"] = message
                    waiter["event"].set()
        finally:
            self._fail_all(f"{self.name} bridge exited", proc)
            if self._proc is proc:
                self._proc = None

    def _read_stderr(self, proc: subprocess.Popen) -> None:
        try:
            for line in proc.stderr or []:
                text = line.strip()
                if text:
                    logger.debug("%s bridge: %s", self.name, text[:500])
        except (OSError, ValueError):
            pass

    def _fail_all(self, reason: str, proc: Optional[subprocess.Popen] = None) -> None:
        with self._pending_lock:
            if proc is None:
                waiters = list(self._pending.values())
                self._pending.clear()
            else:
                ids = [rid for rid, waiter in self._pending.items() if waiter.get("proc") is proc]
                waiters = [self._pending.pop(rid) for rid in ids]
        for waiter in waiters:
            waiter["error"] = reason
            waiter["event"].set()

    def request(self, method: str, params: Optional[dict] = None, timeout: Optional[float] = None) -> Any:
        timeout = max(_TIMEOUT_FLOOR, float(timeout or getattr(settings, "tv_external_timeout", 30)))
        with self._write_lock:
            if self._proc is None or self._proc.poll() is not None:
                self._start()
            with self._pending_lock:
                self._seq += 1
                request_id = str(self._seq)
                waiter = {"event": threading.Event(), "message": None, "error": None,
                          "proc": self._proc}
                self._pending[request_id] = waiter
            payload = json.dumps({"id": request_id, "method": method, "params": params or {}},
                                 ensure_ascii=False)
            try:
                assert self._proc is not None and self._proc.stdin is not None
                self._proc.stdin.write(payload + "\n")
                self._proc.stdin.flush()
            except (OSError, ValueError) as exc:
                self._fail_all(f"{self.name} bridge is unavailable: {exc}")
                raise _BridgeUnavailable(str(exc)) from exc
        if not waiter["event"].wait(timeout):
            with self._pending_lock:
                self._pending.pop(request_id, None)
            raise _BridgeError(f"{self.name} request timed out after {timeout:.0f}s")
        if waiter["error"]:
            raise _BridgeError(waiter["error"])
        message = waiter["message"] or {}
        if not message.get("ok"):
            raise _BridgeError(str(message.get("error") or f"{self.name} request failed"))
        self._schedule_idle_stop()
        return message.get("result")

    def _schedule_idle_stop(self) -> None:
        with self._idle_lock:
            if self._idle_timer is not None:
                self._idle_timer.cancel()
            timer = threading.Timer(self._IDLE_STOP_SECONDS, self._idle_stop)
            timer.daemon = True
            self._idle_timer = timer
            timer.start()

    def _idle_stop(self) -> None:
        with self._write_lock:
            with self._pending_lock:
                busy = bool(self._pending)
            if busy:
                self._schedule_idle_stop()
                return
            self.stop()

    def stop(self) -> None:
        with self._write_lock:
            with self._idle_lock:
                if self._idle_timer is not None:
                    self._idle_timer.cancel()
                    self._idle_timer = None
            proc, self._proc = self._proc, None
            if proc is None or proc.poll() is not None:
                return
            try:
                if proc.stdin:
                    proc.stdin.write('{"id":"shutdown","method":"shutdown"}\n')
                    proc.stdin.flush()
                proc.wait(timeout=2)
            except (OSError, ValueError, subprocess.TimeoutExpired):
                try:
                    proc.kill()
                except OSError:
                    pass


def _bridge(name: str) -> _Bridge:
    with _bridges_lock:
        bridge = _bridges.get(name)
        if bridge is None:
            bridge = _Bridge(_VYLA_BRIDGE, name)
            _bridges[name] = bridge
        return bridge


# ---------------------------------------------------------------------------
# TTL cache
# ---------------------------------------------------------------------------

def _cache_get(key: str) -> Any:
    with _cache_lock:
        entry = _cache.get(key)
        if entry is None:
            return None
        expires, value = entry
        if expires and expires < time.monotonic():
            _cache.pop(key, None)
            return None
        return value


def _cache_set(key: str, value: Any, ttl: float) -> None:
    with _cache_lock:
        if len(_cache) > 128:
            _cache.clear()
        _cache[key] = (time.monotonic() + ttl if ttl else 0.0, value)


# ---------------------------------------------------------------------------
# Vyla
# ---------------------------------------------------------------------------

def _subtitle_tracks(raw: Any) -> list[dict]:
    tracks: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for item in raw if isinstance(raw, list) else []:
        if not isinstance(item, dict):
            continue
        url = str(item.get("file") or item.get("url") or "").strip()
        if not url.startswith(("http://", "https://")):
            continue
        label = str(item.get("label") or item.get("language") or "Subtitle").strip()
        key = (url, label)
        if key in seen:
            continue
        seen.add(key)
        tracks.append({
            "url": url,
            "label": label,
            "srclang": str(item.get("language") or item.get("srclang") or "").strip(),
        })
        if len(tracks) >= 60:
            break
    return tracks


def _vyla_subtitles_sync(imdb_id: str = "", tmdb_id: Optional[int] = None,
                         season: Optional[int] = None, episode: Optional[int] = None) -> list[dict]:
    key = f"vyla:subs:{imdb_id or tmdb_id}:{season}:{episode}"
    cached = _cache_get(key)
    if cached is not None:
        return cached
    target = imdb_id or (str(tmdb_id) if tmdb_id else "")
    if not target:
        return []
    try:
        raw = _bridge("vyla").request("subtitles", {
            "id": target, "imdbId": imdb_id, "season": season, "episode": episode, "timeoutMs": 16000,
        }, timeout=20)
    except Exception as exc:
        logger.info("Vyla subtitles failed: %s", exc)
        raw = []
    tracks = _subtitle_tracks(raw)
    _cache_set(key, tracks, 6 * 3600)
    return tracks


def _vyla_source(item: dict, subtitles: list[dict]) -> Optional[StreamSource]:
    url = str(item.get("url") or "").strip()
    if not url.startswith(("http://", "https://")):
        return None
    headers = {
        str(key): str(value) for key, value in (item.get("headers") or {}).items()
        if isinstance(value, (str, int, float))
    }
    referer = headers.pop("Referer", "") or headers.pop("referer", "")
    quality = str(item.get("quality") or "").strip()
    label = str(item.get("server") or item.get("sourceLabel") or item.get("sourceKey") or "Vyla").strip()
    if not quality:
        quality = quality_of(f"{label} {item.get('sourceType') or ''}") or "auto"
    return StreamSource(
        source="vyla", name=label, quality=quality, url=url, headers=headers,
        referer=str(referer or ""), indexer=str(item.get("sourceKey") or ""),
        filename="", subtitles=list(subtitles or []),
    )


def _vyla_streams_sync(tmdb_id: int, media_type: str, season: Optional[int], episode: Optional[int],
                       imdb_id: str = "", quick: bool = False) -> list[StreamSource]:
    key = f"vyla:streams:{media_type}:{tmdb_id}:{season}:{episode}"
    if quick:
        key += ":quick"
    cached = _cache_get(key)
    if cached is not None:
        return list(cached)
    if quick:
        # Quick watch only needs a playable direct source. A short deadline
        # returns the sources that are ready instead of waiting for the long
        # tail of slow providers.
        deadline_ms, concurrency, ttl = 2500, 16, 2 * 60
    else:
        timeout = max(_TIMEOUT_FLOOR, int(getattr(settings, "tv_external_timeout", 30)))
        deadline_ms = max(6000, min(8000, (timeout - 4) * 1000))
        concurrency, ttl = 10, 10 * 60
    payload = _bridge("vyla").request("streams", {
        "id": int(tmdb_id), "mediaType": media_type, "season": season, "episode": episode,
        "deadlineMs": deadline_ms, "concurrency": concurrency,
    }, deadline_ms / 1000 + 5)
    items = payload.get("urls") if isinstance(payload, dict) else []
    if not items:
        _cache_set(key, [], 90)
        return []
    # Subtitles are fetched lazily by the player, so a stream search never
    # waits on the subtitle providers.
    sources = [source for item in items if (source := _vyla_source(item, [])) is not None]
    _cache_set(key, sources, ttl)
    logger.info("Vyla returned %s source(s) for %s %s", len(sources), media_type, tmdb_id)
    return sources


# ---------------------------------------------------------------------------
# Public entry points
# ---------------------------------------------------------------------------

async def external_stream_sources(*, tmdb_id: Optional[int] = None, imdb_id: str = "",
                                  media_type: str = "movie", season: Optional[int] = None,
                                  episode: Optional[int] = None, title: str = "",
                                  year: str = "", anilist_id: Optional[int] = None,
                                  titles: Optional[list[str]] = None,
                                  episode_count: Optional[int] = None,
                                  quick: bool = False) -> list[StreamSource]:
    if not getattr(settings, "tv_external_enabled", True):
        return []
    jobs = []
    loop = asyncio.get_running_loop()
    if getattr(settings, "tv_vyla_enabled", True) and tmdb_id:
        jobs.append(loop.run_in_executor(
            None, _vyla_streams_sync, int(tmdb_id), media_type, season, episode, imdb_id, quick,
        ))
    if not jobs:
        if getattr(settings, "tv_external_auto_setup", True):
            ensure_runtime(wait=False)
        return []
    results = await asyncio.gather(*jobs, return_exceptions=True)
    sources: list[StreamSource] = []
    for result in results:
        if isinstance(result, BaseException):
            logger.info("External provider failed: %s: %s", type(result).__name__, result)
            continue
        sources.extend(result)
    return sources


def vyla_subtitles(imdb_id: str = "", tmdb_id: Optional[int] = None,
                   season: Optional[int] = None, episode: Optional[int] = None) -> list[dict]:
    if not (getattr(settings, "tv_external_enabled", True) and getattr(settings, "tv_vyla_enabled", True)):
        return []
    return _vyla_subtitles_sync(imdb_id, tmdb_id, season, episode)


def integration_status() -> dict:
    status = runtime_status()
    status["vyla_sdk"] = os.path.isdir(_VYLA_DIR)
    return status
