from __future__ import annotations

import queue as _queue
import logging
import re
import ssl
import threading
import time
import urllib.parse
from collections import OrderedDict
from concurrent.futures import Future
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Optional
from urllib.parse import urljoin

import httpx

from src.core.net import shared_ssl_context

_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"

logger = logging.getLogger(__name__)


_HTTP_TIMEOUT = httpx.Timeout(connect=6.0, read=15.0, write=10.0, pool=8.0)


def _new_session(insecure: bool = False) -> httpx.Client:
    verify = False if insecure else shared_ssl_context()
    return httpx.Client(
        timeout=_HTTP_TIMEOUT, follow_redirects=True,
        limits=httpx.Limits(max_connections=16, max_keepalive_connections=8, keepalive_expiry=30),
        transport=httpx.HTTPTransport(retries=2, verify=verify),
    )


_TLS_FAILED_HOSTS: set[str] = set()


def _is_cert_error(exc: BaseException) -> bool:
    seen: set[int] = set()
    e: Optional[BaseException] = exc
    while e is not None and id(e) not in seen:
        seen.add(id(e))
        if isinstance(e, ssl.SSLCertVerificationError):
            return True
        e = e.__cause__ or e.__context__
    return False


class _UpstreamError(Exception):
    def __init__(self, status: int = 502):
        super().__init__(status)
        self.status = status


class _NotCacheable(Exception):
    pass


_MPEGURL = "application/vnd.apple.mpegurl"
_NET_ERRORS = (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError, httpx.HTTPError, httpx.StreamError)


_RANGE_RE = re.compile(r"^bytes=(\d*)-(\d*)$")


def _parse_range(header: Optional[str], size: int):
    m = _RANGE_RE.match((header or "").strip())
    if not m:
        return None
    a, b = m.groups()
    if not a and not b:
        return None
    if not a:
        n = int(b)
        return False if n == 0 else (max(0, size - n), size - 1)
    start = int(a)
    end = int(b) if b else size - 1
    if start >= size:
        return False
    return (start, min(end, size - 1)) if end >= start else None


class _RelayHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    disable_nagle_algorithm = True
    wbufsize = 1 << 16
    timeout = 60

    def log_message(self, *_args):
        pass

    def _send(self, status: int, ctype: str, body: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def _send_cached(self, data: bytes, ctype: str, rng: Optional[str]) -> None:
        size = len(data)
        span = _parse_range(rng, size)
        if span is False:
            self.send_response(416)
            self.send_header("Content-Range", f"bytes */{size}")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if span is None:
            status, body = 200, data
        else:
            status, body = 206, data[span[0]:span[1] + 1]
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Accept-Ranges", "bytes")
        if span:
            self.send_header("Content-Range", f"bytes {span[0]}-{span[1]}/{size}")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        relay: HlsRelayServer = self.server.relay
        path = self.path.split("?")[0]
        url = relay.resolve(path)
        if not url:
            self.send_error(404, "Unknown relay path")
            return
        rng = self.headers.get("Range")
        try:
            if url.split("?")[0].lower().endswith(".m3u8"):
                self._send(200, _MPEGURL, relay.playlist(url).encode("utf-8"))
                return
            if relay.is_cacheable(path):
                try:
                    data, ctype = relay.segment(path)
                except _NotCacheable:
                    pass
                else:
                    if data[:7] == b"#EXTM3U":
                        body = relay.rewrite_playlist(data.decode("utf-8", "replace"), url).encode("utf-8")
                        self._send(200, _MPEGURL, body)
                    else:
                        self._send_cached(data, ctype, rng)
                    return
            self._passthrough(relay, url, rng)
        except _UpstreamError as exc:
            try:
                self.send_error(exc.status, "Upstream error")
            except _NET_ERRORS:
                pass
        except _NET_ERRORS:
            pass
        except Exception as exc:
            logger.warning("HLS relay error for %s: %s", url, exc)
            try:
                self.send_error(502, "Relay error")
            except _NET_ERRORS:
                pass

    def _passthrough(self, relay: "HlsRelayServer", url: str, rng: Optional[str]) -> None:
        headers = relay.headers()
        if rng:
            headers["Range"] = rng
        retry_headers = None
        if "Referer" not in headers:
            extra = relay._self_referer(url)
            retry_headers = {**headers, **extra} if extra else None
        upstream = None
        session = relay._session_for(url)
        for attempt in range(3):
            try:
                upstream = session.send(session.build_request("GET", url, headers=headers), stream=True)
            except httpx.HTTPError as exc:
                if _is_cert_error(exc):
                    relay._note_cert_failure(url)
                    session = relay._session_for(url)
                if attempt >= 1:
                    logger.warning("HLS relay upstream fetch failed for %s: %s", url, exc)
                    raise _UpstreamError(502) from exc
                continue
            if upstream.status_code in (401, 403) and retry_headers is not None:
                upstream.close()
                headers, retry_headers = retry_headers, None
                continue
            break
        try:
            status = upstream.status_code
            if status >= 400 and status != 416:
                raise _UpstreamError(status)
            ctype = upstream.headers.get("Content-Type", "application/octet-stream")
            if "mpegurl" in ctype.lower():
                text = upstream.read().decode("utf-8", "replace")
                self._send(200, _MPEGURL, relay.rewrite_playlist(text, url).encode("utf-8"))
                return
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            length = upstream.headers.get("Content-Length")
            if length:
                self.send_header("Content-Length", length)
            else:
                self.send_header("Connection", "close")
                self.close_connection = True
            for h in ("Content-Range", "Accept-Ranges"):
                v = upstream.headers.get(h)
                if v:
                    self.send_header(h, v)
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            for chunk in upstream.iter_raw(chunk_size=1 << 18):
                self.wfile.write(chunk)
            self.wfile.flush()
        finally:
            upstream.close()


_TAG_RE = re.compile(r'URI="([^"]+)"')
_SUBTITLE_MEDIA_RE = re.compile(r"^#EXT-X-MEDIA:TYPE=SUBTITLES", re.IGNORECASE)
_STREAM_INF_RE = re.compile(r"^#EXT-X-STREAM-INF:", re.IGNORECASE)
_STRIP_SUBTITLES_ATTR_RE = re.compile(r',?SUBTITLES="[^"]*"')


class _RelayServer(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 64
    allow_reuse_address = True

    def __init__(self, addr, handler, relay):
        self.relay = relay
        super().__init__(addr, handler)

    def handle_error(self, *_args) -> None:
        pass


def _single_variant(text: str) -> str:
    lines = text.splitlines()
    pairs = [(i, lines[i]) for i in range(len(lines) - 1) if lines[i].startswith("#EXT-X-STREAM-INF")]
    if len(pairs) < 2:
        return text

    def rank(item) -> tuple:
        attrs = item[1]
        res = re.search(r"RESOLUTION=\d+x(\d+)", attrs)
        bw = re.search(r"(?<!AVERAGE-)BANDWIDTH=(\d+)", attrs)
        height = int(res.group(1)) if res else 0
        return (height <= 1080, height if height <= 1080 else -height, int(bw.group(1)) if bw else 0)

    keep = max(pairs, key=rank)[0]
    drop = {i for i, _l in pairs if i != keep} | {i + 1 for i, _l in pairs if i != keep}
    return "\n".join(l for i, l in enumerate(lines) if i not in drop)


class HlsRelayServer:
    PREFETCH_AHEAD = 6
    CACHE_MAX_BYTES = 96 << 20
    ENTRY_MAX_BYTES = 40 << 20
    PREFETCH_WORKERS = 6

    def __init__(self) -> None:
        self._server: Optional[_RelayServer] = None
        self._lock = threading.RLock()
        self._path_map: dict[str, str] = {}
        self._url_map: dict[str, str] = {}
        self._cacheable: set[str] = set()
        self._next_seg: dict[str, str] = {}
        self._pl_slots: dict[tuple[str, int], str] = {}
        self._next_id = 0
        self._epoch = 0
        self._last_seg: Optional[str] = None
        self.referer: Optional[str] = None
        self.extra_headers: dict = {}
        self._session = _new_session()
        self._insecure_session: Optional[httpx.Client] = None
        self._cache: "OrderedDict[str, tuple[bytes, str]]" = OrderedDict()
        self._cache_bytes = 0
        self._inflight: dict[str, Future] = {}
        self._raw_pl: dict[str, tuple[float, str]] = {}
        self._pq: "_queue.Queue[Optional[tuple[str, str, int]]]" = _queue.Queue()
        self._seg_chain: dict[str, str] = {}
        self._chain_last: dict[str, str] = {}
        self._chain_gen: dict[str, int] = {}
        self._queued: set[str] = set()
        self._workers: list[threading.Thread] = []

    def _session_for(self, url: str) -> httpx.Client:
        if urllib.parse.urlsplit(url).hostname in _TLS_FAILED_HOSTS:
            if self._insecure_session is None:
                self._insecure_session = _new_session(insecure=True)
            return self._insecure_session
        return self._session

    def _note_cert_failure(self, url: str) -> None:
        host = urllib.parse.urlsplit(url).hostname
        if host and host not in _TLS_FAILED_HOSTS:
            _TLS_FAILED_HOSTS.add(host)
            logger.warning("HLS relay: certificate verification failed for %s - continuing unverified", host)


    def start(self) -> int:
        if self._server is not None:
            return self._server.server_port
        self._server = _RelayServer(("127.0.0.1", 0), _RelayHandler, self)
        threading.Thread(target=self._server.serve_forever, kwargs={"poll_interval": 0.1}, daemon=True).start()
        self._pq = _queue.Queue()
        self._workers = [threading.Thread(target=self._prefetch_loop, args=(self._pq,), daemon=True)
                         for _ in range(self.PREFETCH_WORKERS)]
        for t in self._workers:
            t.start()
        return self._server.server_port

    def stop(self) -> None:
        for _ in self._workers:
            self._pq.put(None)
        self._workers = []
        srv, self._server = self._server, None
        if srv is not None:
            threading.Thread(target=lambda: (srv.shutdown(), srv.server_close()), daemon=True).start()
        self._reset_state(drain_queue=False)
        try:
            self._session.close()
        except Exception:
            pass
        self._session = _new_session()
        if self._insecure_session is not None:
            try:
                self._insecure_session.close()
            except Exception:
                pass
            self._insecure_session = None

    def _reset_state(self, drain_queue: bool = True) -> None:
        with self._lock:
            self._epoch += 1
            self._path_map.clear()
            self._url_map.clear()
            self._cacheable.clear()
            self._next_seg.clear()
            self._pl_slots.clear()
            self._cache.clear()
            self._cache_bytes = 0
            self._raw_pl.clear()
            self._inflight.clear()
            self._seg_chain.clear()
            self._chain_last.clear()
            self._chain_gen.clear()
            self._queued.clear()
            self._last_seg = None
        if drain_queue:
            try:
                while True:
                    self._pq.get_nowait()
            except _queue.Empty:
                pass

    def set_stream(self, root_url: str, referer: Optional[str] = None, extra_headers: Optional[dict] = None, live: bool = False) -> str:
        self.PREFETCH_AHEAD, self.CACHE_MAX_BYTES = (3, 32 << 20) if live else (6, 96 << 20)
        if self._server is None:
            self.start()
        self.referer = referer
        self.extra_headers = extra_headers or {}
        self._reset_state()
        with self._lock:
            local = self._register(root_url)
        return f"http://127.0.0.1:{self._server.server_port}{local}"

    def register(self, absolute_url: str) -> str:
        if self._server is None:
            self.start()
        with self._lock:
            local = self._register(absolute_url)
        return f"http://127.0.0.1:{self._server.server_port}{local}"

    def resolve(self, path: str) -> Optional[str]:
        with self._lock:
            return self._path_map.get(path)

    def is_cacheable(self, path: str) -> bool:
        return path in self._cacheable

    def headers(self) -> dict:
        h = {"User-Agent": _UA, "Accept-Encoding": "identity"}
        if self.referer:
            h["Referer"] = self.referer
            try:
                p = urllib.parse.urlsplit(self.referer)
                if p.scheme and p.netloc:
                    h["Origin"] = f"{p.scheme}://{p.netloc}"
            except ValueError:
                pass
        h.update(self.extra_headers or {})
        return h


    @staticmethod
    def _self_referer(url: str) -> dict:
        try:
            p = urllib.parse.urlsplit(url)
        except ValueError:
            return {}
        if not p.scheme or not p.netloc:
            return {}
        origin = f"{p.scheme}://{p.netloc}"
        return {"Referer": f"{origin}/", "Origin": origin}

    def _get(self, url: str) -> tuple[bytes, str]:
        scheme = urllib.parse.urlsplit(url).scheme.lower()
        if scheme not in ("http", "https"):
            logger.warning("HLS relay refused a non-http upstream url: %r", url[:120])
            raise _UpstreamError(400)
        last: Exception = _UpstreamError(502)
        headers = self.headers()

        retry_headers = None
        if "Referer" not in headers:
            extra = self._self_referer(url)
            retry_headers = {**headers, **extra} if extra else None
        session = self._session_for(url)
        for attempt in range(3):
            try:
                with session.stream("GET", url, headers=headers) as r:
                    if r.status_code >= 400:
                        if r.status_code in (401, 403) and retry_headers is not None:
                            headers, retry_headers = retry_headers, None
                            last = _UpstreamError(r.status_code)
                        elif r.status_code in (408, 425, 429) or r.status_code >= 500:
                            last = _UpstreamError(r.status_code)
                        else:
                            raise _UpstreamError(r.status_code)
                    else:
                        ctype = r.headers.get("Content-Type", "application/octet-stream")
                        limit = (4 << 20) if ("mpegurl" in ctype.lower() or ".m3u8" in url.lower()) else self.ENTRY_MAX_BYTES
                        length = r.headers.get("Content-Length")
                        if length and length.isdigit() and int(length) > limit:
                            raise _NotCacheable()
                        data = bytearray()
                        for chunk in r.iter_bytes(1 << 16):
                            data += chunk
                            if len(data) > limit:
                                raise _NotCacheable()
                        return bytes(data), ctype
            except httpx.HTTPError as exc:
                last = exc
                if _is_cert_error(exc):
                    self._note_cert_failure(url)
                    session = self._session_for(url)
            if attempt < 2:
                time.sleep(0.25 * (attempt + 1))
        if isinstance(last, _UpstreamError):
            raise last
        logger.warning("HLS relay upstream failed for %s: %s", url, last)
        raise _UpstreamError(502)

    def fetch_text(self, url: str) -> str:
        now = time.monotonic()
        with self._lock:
            hit = self._raw_pl.get(url)
            if hit and hit[0] > now:
                return hit[1]
        try:
            data, _ct = self._get(url)
        except _UpstreamError:
            with self._lock:
                stale = self._raw_pl.get(url)
            if stale is None:
                raise


            logger.debug("HLS relay replaying last known playlist for %s", url)
            return stale[1]
        text = data.decode("utf-8", "replace")
        ttl = 600.0 if ("#EXT-X-ENDLIST" in text or "#EXT-X-STREAM-INF" in text) else 0.8
        with self._lock:
            if len(self._raw_pl) > 48:
                self._raw_pl.clear()
            self._raw_pl[url] = (now + ttl, text)
        return text

    def playlist(self, url: str) -> str:
        return self.rewrite_playlist(_single_variant(self.fetch_text(url)), url)


    def segment(self, path: str) -> tuple[bytes, str]:
        with self._lock:
            chain = self._seg_chain.get(path, "")
            last = self._chain_last.get(chain)
            if last is not None and path != last and path != self._next_seg.get(last):
                self._chain_gen[chain] = self._chain_gen.get(chain, 0) + 1
                self._queued.clear()
            self._chain_last[chain] = path
        self._schedule_ahead(path)
        return self._ensure(path)

    def _ensure(self, path: str) -> tuple[bytes, str]:
        with self._lock:
            epoch = self._epoch
            hit = self._cache.get(path)
            if hit is not None:
                self._cache.move_to_end(path)
                return hit
            fut = self._inflight.get(path)
            owner = fut is None
            if owner:
                fut = self._inflight[path] = Future()
            url = self._path_map.get(path)
        if not owner:
            return fut.result(timeout=40)
        try:
            if not url:
                raise _UpstreamError(404)
            data, ctype = self._get(url)
            if ctype.lower().startswith("image/"):
                peek = data[:12]
                real_img = (peek[:3] == b"\xff\xd8\xff" or peek[:8] == b"\x89PNG\r\n\x1a\n"
                            or peek[:6] in (b"GIF87a", b"GIF89a") or (peek[:4] == b"RIFF" and peek[8:12] == b"WEBP"))
                if real_img and peek[:1] != b"\x47":
                    logger.warning("HLS relay got a genuine image for %s - likely hotlink-blocked", url)
                    raise _UpstreamError(502)
                ctype = "video/mp2t"
            res = (data, ctype)
            with self._lock:
                if epoch == self._epoch and len(data) <= self.ENTRY_MAX_BYTES:
                    self._cache[path] = res
                    self._cache_bytes += len(data)
                    while self._cache_bytes > self.CACHE_MAX_BYTES and len(self._cache) > 1:
                        _k, (old, _c) = self._cache.popitem(last=False)
                        self._cache_bytes -= len(old)
            fut.set_result(res)
            return res
        except BaseException as exc:
            fut.set_exception(exc)
            raise
        finally:
            with self._lock:
                if self._inflight.get(path) is fut:
                    self._inflight.pop(path, None)

    def _schedule_ahead(self, path: str) -> None:
        with self._lock:
            chain = self._seg_chain.get(path, "")
            gen = self._chain_gen.get(chain, 0)
            p = path
            for _ in range(self.PREFETCH_AHEAD):
                p = self._next_seg.get(p)
                if not p:
                    break
                if p not in self._cache and p not in self._inflight and p not in self._queued:
                    self._queued.add(p)
                    self._pq.put((p, chain, gen))

    def _prefetch_loop(self, q) -> None:
        while True:
            item = q.get()
            if item is None:
                return
            p, chain, gen = item
            with self._lock:
                self._queued.discard(p)
                if self._chain_gen.get(chain, 0) != gen:
                    continue
            try:
                self._ensure(p)
            except BaseException:
                pass


    def _register(self, absolute_url: str, cacheable: bool = False) -> str:
        path = self._url_map.get(absolute_url)
        if path is None:
            path = f"/relay/{self._next_id}{self._guess_extension(absolute_url)}"
            self._next_id += 1
            self._path_map[path] = absolute_url
            self._url_map[absolute_url] = path
        if cacheable and not path.endswith(".m3u8"):
            self._cacheable.add(path)
        return path

    def _register_playlist_slot(self, base_url: str, slot: int, absolute_url: str) -> str:
        path = self._pl_slots.get((base_url, slot))
        if path is None:
            path = self._register(absolute_url)
            self._pl_slots[(base_url, slot)] = path
            return path
        old = self._path_map.get(path)
        if old != absolute_url:
            if old is not None:
                self._url_map.pop(old, None)
            self._path_map[path] = absolute_url
            self._url_map[absolute_url] = path
        return path

    _ALLOWED_EXTS = {
        ".3gp", ".aac", ".avi", ".flac", ".mkv", ".m3u8", ".mov", ".mp3",
        ".mp4", ".mpegts", ".mpd", ".ogg", ".ogv", ".ts", ".vtt", ".webm",
    }
    _FMP4_REMAP = {".m4s", ".cmfv", ".cmfa", ".m4v", ".m4a", ".mp4a"}

    @classmethod
    def _guess_extension(cls, absolute_url: str) -> str:
        clean = absolute_url.split("?")[0].split("#")[0]
        dot = clean.rfind(".")
        slash = clean.rfind("/")
        if dot > slash and dot != -1:
            ext = clean[dot:].lower()
            if 1 < len(ext) <= 6:
                if ext in cls._ALLOWED_EXTS:
                    return ext
                if ext in cls._FMP4_REMAP:
                    return ".mp4"
        return ".ts"

    def rewrite_playlist(self, text: str, base_url: str) -> str:
        out = []
        prev: Optional[str] = None
        pl_slot = 0
        with self._lock:
            for line in text.splitlines():
                s = line.strip()
                if not s:
                    out.append(line)
                    continue
                if s.startswith("#"):
                    if _SUBTITLE_MEDIA_RE.match(s):
                        continue
                    if _STREAM_INF_RE.match(s):
                        s = _STRIP_SUBTITLES_ATTR_RE.sub("", s)
                    m = _TAG_RE.search(s)
                    if m:
                        u = urljoin(base_url, m.group(1))
                        cacheable = s.startswith(("#EXT-X-MAP", "#EXT-X-KEY", "#EXT-X-SESSION-KEY"))
                        if not cacheable and u.split("?")[0].lower().endswith(".m3u8"):
                            lp = self._register_playlist_slot(base_url, pl_slot, u)
                            pl_slot += 1
                        else:
                            lp = self._register(u, cacheable=cacheable)
                        s = s[:m.start(1)] + lp + s[m.end(1):]
                    out.append(s)
                    continue
                u = urljoin(base_url, s)
                if u.split("?")[0].lower().endswith(".m3u8"):
                    lp = self._register_playlist_slot(base_url, pl_slot, u)
                    pl_slot += 1
                else:
                    lp = self._register(u, cacheable=True)
                if lp in self._cacheable:
                    self._seg_chain[lp] = base_url
                if prev is not None and lp != prev and lp in self._cacheable:
                    self._next_seg[prev] = lp
                prev = lp if lp in self._cacheable else None
                out.append(lp)
        return "\n".join(out)


_relay: Optional[HlsRelayServer] = None

def get_relay() -> HlsRelayServer:
    global _relay
    if _relay is None:
        _relay = HlsRelayServer()
    return _relay
