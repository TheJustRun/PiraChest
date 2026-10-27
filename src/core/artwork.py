from __future__ import annotations

import hashlib
import logging
import os
import threading
import time
import weakref
from collections import OrderedDict
from urllib.parse import urlsplit
from urllib.request import url2pathname

from PySide6.QtCore import QRect, QSize, Qt
from PySide6.QtGui import QColor, QGuiApplication, QImage, QImageReader, QPixmap
from shiboken6 import isValid as _alive

from src.core.config import paths
from src.core.worker import CPU, IO, Priority, engine

logger = logging.getLogger(__name__)

COVER = 0
FIT = 1

_STORE = os.path.join(paths.cache_dir, 'images')
_MAX_BYTES = 16 * 1024 * 1024
_PIXMAP_BUDGET = 64 * 1024 * 1024
_DISK_BUDGET = 512 * 1024 * 1024
_MISS_TTL = 60.0
_COOLDOWN = 5.0
_TOUCH_AFTER = 6 * 3600.0
_SWATCH = 40
_COLOR_LIMIT = 512
_UA = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) '
       'Chrome/124.0 Safari/537.36')
_WIKIMEDIA_UA = 'PiraChest/1.0 (desktop app; +https://github.com/) httpx'
_FALLBACK_COLORS = ('#5b1a4a', '#1c2a4a', '#150a17', '#3a1230', '#241436')

QImageReader.setAllocationLimit(64)

_client = None
_client_lock = threading.Lock()
_cooldown: dict[str, float] = {}
_cooldown_lock = threading.Lock()


def _http_client():
    global _client
    if _client is None:
        with _client_lock:
            if _client is None:
                import httpx

                from .net import shared_ssl_context
                _client = httpx.Client(
                    headers={'User-Agent': _UA, 'Accept': 'image/*,*/*;q=0.8', 'Accept-Encoding': 'gzip, deflate'},
                    follow_redirects=True, trust_env=False, timeout=httpx.Timeout(8.0, connect=3.0),
                    limits=httpx.Limits(max_connections=16, max_keepalive_connections=16),
                    verify=shared_ssl_context(),
                )
    return _client


class _Quiet(Exception):
    quiet = True


def _is_local(src: str) -> bool:
    return src.startswith('file://') or '://' not in src


def _local_path(src: str) -> str:
    if src.startswith('file://'):
        parts = urlsplit(src)
        if parts.netloc and parts.netloc.lower() != 'localhost':
            raise _Quiet(f'refusing artwork from a network share: {src}')
        return url2pathname(parts.path)
    if src.lstrip().startswith(('\\\\', '//')):
        raise _Quiet(f'refusing artwork from a network share: {src}')
    return src


def _slot(src: str) -> str:
    digest = hashlib.blake2b(src.encode('utf-8', 'ignore'), digest_size=16).hexdigest()
    return os.path.join(_STORE, digest[:2], digest[2:])


def _rm(path: str) -> None:
    try:
        os.remove(path)
    except OSError:
        pass


def _headers(url: str) -> dict:
    parts = urlsplit(url)
    host = parts.netloc.lower()
    if host == 'wikimedia.org' or host.endswith('.wikimedia.org'):
        return {'User-Agent': _WIKIMEDIA_UA}
    origin = f'{parts.scheme}://{parts.netloc}'
    return {'Referer': origin + '/', 'Origin': origin}


def _download(url: str, dest: str) -> None:
    import httpx
    host = urlsplit(url).netloc.lower()
    with _cooldown_lock:
        cooling = _cooldown.get(host, 0.0) > time.monotonic()
    if cooling:
        raise _Quiet(f'{host} is cooling down after a connection failure')
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    tmp = f'{dest}.{os.getpid()}.{threading.get_ident()}.part'
    try:
        size = 0
        with _http_client().stream('GET', url, headers=_headers(url)) as resp:
            resp.raise_for_status()
            with open(tmp, 'wb') as fh:
                for chunk in resp.iter_bytes(1 << 16):
                    size += len(chunk)
                    if size > _MAX_BYTES:
                        raise ValueError('image exceeds size limit')
                    fh.write(chunk)
        if not size:
            raise ValueError('empty image response')
        os.replace(tmp, dest)
    except httpx.HTTPError as exc:
        exc.quiet = True
        if isinstance(exc, (httpx.ConnectError, httpx.ConnectTimeout)):
            with _cooldown_lock:
                _cooldown[host] = time.monotonic() + _COOLDOWN
        raise
    finally:
        _rm(tmp)


def _resolve(src: str) -> str:
    if _is_local(src):
        return _local_path(src)
    slot = _slot(src)
    try:
        age = time.time() - os.stat(slot).st_mtime
    except OSError:
        _download(src, slot)
        return slot
    if age > _TOUCH_AFTER:
        try:
            os.utime(slot)
        except OSError:
            pass
    return slot


def _trim_store() -> None:
    files, total = [], 0
    for root, _dirs, names in os.walk(_STORE):
        for name in names:
            path = os.path.join(root, name)
            try:
                st = os.stat(path)
            except OSError:
                continue
            if name.endswith('.part') and time.time() - st.st_mtime > 3600:
                _rm(path)
                continue
            files.append((st.st_mtime, st.st_size, path))
            total += st.st_size
    if total <= _DISK_BUDGET:
        return
    for _mtime, size, path in sorted(files):
        _rm(path)
        total -= size
        if total <= _DISK_BUDGET * 0.8:
            break


def _wipe_store() -> None:
    for root, _dirs, names in os.walk(_STORE):
        for name in names:
            _rm(os.path.join(root, name))


_dpr_value: float | None = None
_dpr_hooked = False
_hooked_screens: set[int] = set()


def _reset_dpr(*_args) -> None:
    global _dpr_value
    _dpr_value = None


def _dpr() -> float:
    global _dpr_value, _dpr_hooked
    if _dpr_value is not None:
        return _dpr_value
    screen = QGuiApplication.primaryScreen()
    if screen is None:
        return 1.0
    _dpr_value = round(screen.devicePixelRatio(), 2)
    if not _dpr_hooked:
        _dpr_hooked = True
        app = QGuiApplication.instance()
        if app is not None:
            app.primaryScreenChanged.connect(_reset_dpr)
            app.screenAdded.connect(_reset_dpr)
            app.screenRemoved.connect(_reset_dpr)
    if id(screen) not in _hooked_screens:
        _hooked_screens.add(id(screen))
        try:
            screen.logicalDotsPerInchChanged.connect(_reset_dpr)
        except (RuntimeError, TypeError):
            pass
    return _dpr_value


def _dims(size) -> tuple[int, int]:
    if isinstance(size, QSize):
        return max(0, size.width()), max(0, size.height())
    if isinstance(size, (tuple, list)):
        return max(0, int(size[0])), max(0, int(size[1]))
    value = max(0, int(size))
    return value, value


def _decode(path: str, width: int, height: int, mode: int, dpr: float) -> QImage:
    pw, ph = round(width * dpr), round(height * dpr)
    reader = QImageReader(path)
    reader.setDecideFormatFromContent(True)
    reader.setAutoTransform(True)
    native = reader.size()
    known = pw > 0 and ph > 0 and native.isValid() and native.width() > 0 and native.height() > 0
    pick = max if mode == COVER else min
    if known:
        scale = pick(pw / native.width(), ph / native.height())
        tw, th = max(1, round(native.width() * scale)), max(1, round(native.height() * scale))
        reader.setScaledSize(QSize(tw, th))
        if mode == COVER:
            reader.setScaledClipRect(QRect((tw - pw) // 2, (th - ph) // 2, min(pw, tw), min(ph, th)))
    image = reader.read()
    if image.isNull():
        return image
    if not known and pw > 0 and ph > 0:
        ratio = Qt.AspectRatioMode.KeepAspectRatioByExpanding if mode == COVER else Qt.AspectRatioMode.KeepAspectRatio
        image = image.scaled(pw, ph, ratio, Qt.TransformationMode.SmoothTransformation)
        if mode == COVER and (image.width() > pw or image.height() > ph):
            image = image.copy((image.width() - pw) // 2, (image.height() - ph) // 2, pw, ph)
    fmt = QImage.Format.Format_ARGB32_Premultiplied if image.hasAlphaChannel() else QImage.Format.Format_RGB32
    if image.format() != fmt:
        image = image.convertToFormat(fmt)
    image.setDevicePixelRatio(dpr)
    return image


def _swatches(path: str, count: int) -> list[QColor]:
    reader = QImageReader(path)
    reader.setScaledSize(QSize(_SWATCH, _SWATCH))
    image = reader.read()
    if image.isNull():
        return []
    image = image.convertToFormat(QImage.Format.Format_RGB32)
    raw, stride = bytes(image.constBits()), image.bytesPerLine()
    buckets: dict[int, list] = {}
    for y in range(image.height()):
        for x in range(image.width()):
            off = y * stride + x * 4
            b, g, r = raw[off], raw[off + 1], raw[off + 2]
            hi, lo = max(r, g, b), min(r, g, b)
            if hi < 24 or (hi > 236 and lo > 222):
                continue
            acc = buckets.setdefault((r >> 4 << 8) | (g >> 4 << 4) | (b >> 4), [0, 0, 0, 0])
            acc[0] += r
            acc[1] += g
            acc[2] += b
            acc[3] += 1
    picked: list[QColor] = []
    for acc in sorted(buckets.values(), key=lambda a: a[3], reverse=True):
        c = QColor(acc[0] // acc[3], acc[1] // acc[3], acc[2] // acc[3])
        if all(abs(c.red() - p.red()) + abs(c.green() - p.green()) + abs(c.blue() - p.blue()) >= 60 for p in picked):
            picked.append(c)
        if len(picked) >= count:
            break
    while picked and len(picked) < count:
        hue, sat, val, _ = picked[0].getHsv()
        shifted = QColor()
        shifted.setHsv((max(hue, 0) + 45 * len(picked)) % 360, sat, val, 255)
        picked.append(shifted)
    return picked


def _colors_job(src: str, count: int) -> list[QColor]:
    try:
        colors = _swatches(_resolve(src), count)
    except Exception:
        colors = []
    return colors or [QColor(c) for c in _FALLBACK_COLORS[:count]]


class _Req:
    __slots__ = ('waiters', 'job', 'prio')

    def __init__(self, prio: int):
        self.waiters: list[tuple[int, object, object]] = []
        self.job = None
        self.prio = prio


class _Images:
    def __init__(self):
        self._pix: 'OrderedDict[tuple, QPixmap]' = OrderedDict()
        self._used = 0
        self._reqs: dict[tuple, _Req] = {}
        self._tokens: dict[int, tuple] = {}
        self._owned = weakref.WeakKeyDictionary()
        self._misses: dict[str, float] = {}
        self._colors: 'OrderedDict[tuple, list[QColor]]' = OrderedDict()
        self._color_waiters: dict[tuple, list[tuple[object, object]]] = {}
        self._seq = 0
        self._closed = False
        self._trimmed = False


    def get(self, src, size, callback, mode: int = COVER, owner=None, priority: int = Priority.VISIBLE) -> int:
        if self._closed or not src or not callable(callback):
            return 0
        width, height = _dims(size)
        key = (str(src), width, height, mode, _dpr())
        hit = self._pix.get(key)
        if hit is not None:
            self._pix.move_to_end(key)
            callback(hit)
            return 0
        if self._misses.get(key[0], 0.0) > time.monotonic():
            callback(QPixmap())
            return 0
        if not self._trimmed:
            self._trimmed = True
            engine().submit(_trim_store, priority=Priority.PREFETCH, pool=IO)
        self._seq += 1
        token = self._seq
        self._tokens[token] = key
        if owner is not None:
            self._owned.setdefault(owner, set()).add(token)
        req = self._reqs.get(key)
        if req is None:
            req = self._reqs[key] = _Req(int(priority))
            req.waiters.append((token, callback, owner))
            self._start(key, req)
        else:
            req.waiters.append((token, callback, owner))
            if int(priority) > req.prio:
                req.prio = int(priority)
                if req.job is not None:
                    engine().reprioritize(req.job, req.prio)
        return token

    def peek(self, src, size, mode: int = COVER):
        if not src:
            return None
        width, height = _dims(size)
        key = (str(src), width, height, mode, _dpr())
        hit = self._pix.get(key)
        if hit is not None:
            self._pix.move_to_end(key)
        return hit

    def cancel(self, token: int) -> None:
        key = self._tokens.pop(token, None)
        req = self._reqs.get(key) if key else None
        if req is None:
            return
        for w in req.waiters:
            if w[0] == token:
                self._forget(token, w[2])
        req.waiters = [w for w in req.waiters if w[0] != token]
        if not req.waiters:
            self._reqs.pop(key, None)
            if req.job is not None:
                engine().cancel(req.job)

    def release(self, owner) -> None:
        for token in list(self._owned.pop(owner, ())):
            self.cancel(token)
        for key, waiters in list(self._color_waiters.items()):
            kept = [w for w in waiters if w[1] is None or w[1] is not owner]
            if kept:
                self._color_waiters[key] = kept
            else:
                del self._color_waiters[key]

    def dominant(self, src, callback, count: int = 1, owner=None) -> None:
        if self._closed or not src or not callable(callback):
            return
        key = (str(src), count)
        hit = self._colors.get(key)
        if hit is not None:
            self._colors.move_to_end(key)
            callback(list(hit))
            return
        waiting = self._color_waiters.get(key)
        if waiting is not None:
            waiting.append((callback, owner))
            return
        self._color_waiters[key] = [(callback, owner)]
        engine().submit(_colors_job, key, on_done=lambda colors, k=key: self._deliver_colors(k, colors),
                        key=('color',) + key, priority=Priority.LOW, pool=IO)

    def trim(self, max_bytes: int = 24 * 1024 * 1024) -> None:
        while self._used > max_bytes and len(self._pix) > 1:
            _key, evicted = self._pix.popitem(last=False)
            self._used -= evicted.width() * evicted.height() * 4

    def clear(self) -> None:
        self._pix.clear()
        self._colors.clear()
        self._misses.clear()
        self._used = 0
        engine().submit(_wipe_store, priority=Priority.PREFETCH, pool=IO)

    def shutdown(self) -> None:
        if self._closed:
            return
        self._closed = True
        for state in (self._reqs, self._color_waiters, self._tokens, self._owned, self._pix, self._colors):
            state.clear()
        self._used = 0
        if _client is not None:
            try:
                _client.close()
            except Exception:
                pass


    def _start(self, key: tuple, req: _Req) -> None:
        src = key[0]
        if _is_local(src):
            try:
                path = _local_path(src)
            except _Quiet:
                self._fail(key)
                return
            self._decode_stage(key, req, path)
            return
        req.job = engine().submit(_resolve, (src,), on_done=lambda path, k=key: self._on_resolved(k, path),
                                  on_error=lambda _e, k=key: self._fail(k), key=('src', src),
                                  priority=req.prio, pool=IO)

    def _on_resolved(self, key: tuple, path: str) -> None:
        req = self._reqs.get(key)
        if req is not None and not self._closed:
            self._decode_stage(key, req, path)

    def _decode_stage(self, key: tuple, req: _Req, path: str) -> None:
        req.job = engine().submit(_decode, (path, key[1], key[2], key[3], key[4]),
                                  on_done=lambda img, k=key, p=path: self._on_decoded(k, p, img),
                                  on_error=lambda _e, k=key: self._fail(k), key=('dec',) + key,
                                  priority=req.prio, pool=CPU)

    def _on_decoded(self, key: tuple, path: str, image: QImage) -> None:
        req = self._reqs.get(key)
        if req is None or self._closed:
            return
        if image is None or image.isNull():
            if not _is_local(key[0]):
                engine().submit(_rm, (path,), priority=Priority.PREFETCH, pool=IO)
            self._fail(key)
            return
        del self._reqs[key]
        pixmap = QPixmap.fromImage(image)
        if not pixmap.isNull():
            self._remember(key, pixmap)
        self._notify(req.waiters, pixmap)

    def _fail(self, key: tuple) -> None:
        req = self._reqs.pop(key, None)
        now = time.monotonic()
        if len(self._misses) > 1024:
            self._misses = {s: t for s, t in self._misses.items() if t > now}
        self._misses[key[0]] = now + _MISS_TTL
        if req is not None and not self._closed:
            self._notify(req.waiters, QPixmap())

    def _forget(self, token: int, owner) -> None:
        self._tokens.pop(token, None)
        bucket = self._owned.get(owner) if owner is not None else None
        if bucket is not None:
            bucket.discard(token)
            if not bucket:
                try:
                    del self._owned[owner]
                except KeyError:
                    pass

    def _notify(self, waiters, pixmap: QPixmap) -> None:
        for token, callback, owner in waiters:
            self._forget(token, owner)
            if owner is not None and not _alive(owner):
                continue
            try:
                callback(pixmap)
            except RuntimeError:
                pass
            except Exception:
                logger.warning('image callback raised', exc_info=True)

    def _remember(self, key: tuple, pixmap: QPixmap) -> None:
        old = self._pix.pop(key, None)
        if old is not None:
            self._used -= old.width() * old.height() * 4
        self._pix[key] = pixmap
        self._used += pixmap.width() * pixmap.height() * 4
        while self._used > _PIXMAP_BUDGET and len(self._pix) > 1:
            _k, evicted = self._pix.popitem(last=False)
            self._used -= evicted.width() * evicted.height() * 4

    def _deliver_colors(self, key: tuple, colors: list) -> None:
        waiters = self._color_waiters.pop(key, ())
        if self._closed:
            return
        self._colors[key] = colors
        while len(self._colors) > _COLOR_LIMIT:
            self._colors.popitem(last=False)
        for callback, owner in waiters:
            if owner is not None and not _alive(owner):
                continue
            try:
                callback(list(colors))
            except RuntimeError:
                pass


images = _Images()
