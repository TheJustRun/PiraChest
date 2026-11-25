from __future__ import annotations

import json
import logging
import os
import re
import string
import sys
import weakref
from typing import Callable, Optional

import shiboken6
from PySide6.QtCore import QEvent, QObject, Qt, QTimer, Signal
from PySide6.QtWidgets import QApplication

from src.core.config import settings, apply_settings, save_settings, _PROJECT_ROOT

logger = logging.getLogger(__name__)

_FALLBACK_LANG = "en"
_CODE_RE = re.compile(r"^[A-Za-z0-9_-]{1,32}$")
_DISPLAY_NAME_RE = re.compile(r'"display_name"\s*:\s*("(?:[^"\\]|\\.)*")')
_DEBOUNCE_MS = 30
_RETRY_MS = 50


def _resolve_lang_dir() -> str:
    base = getattr(sys, "_MEIPASS", None)
    if base:
        for parts in (("src", "gui", "lang"), ("gui", "lang")):
            candidate = os.path.join(base, *parts)
            if os.path.isdir(candidate):
                return candidate
    return os.path.join(_PROJECT_ROOT, "src", "gui", "lang")


_LANG_DIR = _resolve_lang_dir()


class _LocaleSignal(QObject):
    changed = Signal()

    def __init__(self) -> None:
        super().__init__()
        self._subs: dict[int, list] = {}
        self._version = 0
        self._timer: Optional[QTimer] = None
        self._busy = False
        self._again = False
        self.changed.connect(self._dispatch)

    def request(self) -> None:
        if self._busy:
            self._again = True
            return
        if self._timer is None:
            self._timer = QTimer(self)
            self._timer.setSingleShot(True)
            self._timer.timeout.connect(self._flush)
        self._timer.start(_DEBOUNCE_MS)

    def _flush(self) -> None:
        app = QApplication.instance()
        if app is not None and (app.activePopupWidget() is not None
                                or app.mouseButtons() != Qt.MouseButton.NoButton):
            self._timer.start(_RETRY_MS)
            return
        self._busy = True
        try:
            self._version += 1
            self.changed.emit()
        finally:
            self._busy = False
        if self._again:
            self._again = False
            self.request()

    def add(self, widget, callback: Callable[[], None]) -> None:
        if widget is None:
            self._subs.setdefault(0, [None, [], self._version])[1].append(callback)
            return
        key = id(widget)
        entry = self._subs.get(key)
        if entry is None:
            def _gone(ref, k=key) -> None:
                cur = self._subs.get(k)
                if cur is not None and cur[0] is ref:
                    del self._subs[k]
            entry = self._subs[key] = [weakref.ref(widget, _gone), [], self._version]
            widget.installEventFilter(self)
        entry[1].append(callback)
        entry[2] = self._version

    def _run(self, key: int, entry: list) -> None:
        ref = entry[0]
        if ref is not None:
            target = ref()
            if target is None or not shiboken6.isValid(target):
                self._subs.pop(key, None)
                return
        entry[2] = self._version
        for callback in tuple(entry[1]):
            try:
                callback()
            except RuntimeError:
                self._subs.pop(key, None)
                return
            except Exception:
                logger.exception("Locale refresh callback failed")

    def _dispatch(self) -> None:
        for key, entry in tuple(self._subs.items()):
            if entry[2] == self._version:
                continue
            ref = entry[0]
            if ref is None:
                self._run(key, entry)
                continue
            target = ref()
            if target is None or not shiboken6.isValid(target):
                self._subs.pop(key, None)
                continue
            visible = getattr(target, "isVisible", None)
            if visible is None or visible():
                self._run(key, entry)

    def eventFilter(self, obj, event) -> bool:
        if event.type() == QEvent.Type.Show:
            entry = self._subs.get(id(obj))
            if entry is not None and entry[2] != self._version:
                self._run(id(obj), entry)
        return False


locale_signal = _LocaleSignal()

_strings_cache: dict[str, dict[str, str]] = {}
_meta_cache: Optional[dict[str, str]] = None
_resolved: tuple[object, str] = (None, _FALLBACK_LANG)
_warned_missing: set[tuple[str, str]] = set()


def _read_json(code: str) -> dict:
    if not _CODE_RE.match(code):
        return {}
    path = os.path.join(_LANG_DIR, f"{code}.json")
    try:
        with open(path, "r", encoding="utf-8-sig") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError) as exc:
        logger.warning("Failed to load language file %s: %s", path, exc)
        return {}


def _load(code: str) -> dict[str, str]:
    cached = _strings_cache.get(code)
    if cached is not None:
        return cached
    data = _read_json(code)
    raw = data.get("strings")
    if not isinstance(raw, dict):
        raw = {k: v for k, v in data.items() if k != "_meta"}
    strings = {k: v for k, v in raw.items() if isinstance(v, str) and v.strip()}
    _strings_cache[code] = strings
    return strings


def _display_name(path: str, code: str) -> str:
    try:
        with open(path, "r", encoding="utf-8-sig") as fh:
            head = fh.read(4096)
        m = _DISPLAY_NAME_RE.search(head)
        if m:
            return json.loads(m.group(1)) or code
        with open(path, "r", encoding="utf-8-sig") as fh:
            meta = json.load(fh).get("_meta")
        if isinstance(meta, dict) and isinstance(meta.get("display_name"), str):
            return meta["display_name"] or code
    except (OSError, ValueError, AttributeError):
        pass
    return code


def available_languages() -> dict[str, str]:
    global _meta_cache
    if _meta_cache is not None:
        return _meta_cache
    result: dict[str, str] = {}
    try:
        names = sorted(os.listdir(_LANG_DIR))
    except OSError:
        names = []
    for fname in names:
        code = fname[:-5]
        if fname.endswith(".json") and _CODE_RE.match(code):
            result[code] = _display_name(os.path.join(_LANG_DIR, fname), code)
    _meta_cache = result or {_FALLBACK_LANG: "English"}
    return _meta_cache


def current_language() -> str:
    global _resolved
    raw = getattr(settings, "language", None)
    if _resolved[0] is raw or _resolved[0] == raw:
        return _resolved[1]
    code = raw if isinstance(raw, str) and raw in available_languages() else _FALLBACK_LANG
    _resolved = (raw, code)
    return code


def set_language(lang_code: str) -> None:
    if lang_code == current_language() or lang_code not in available_languages():
        return
    apply_settings(language=lang_code)
    save_settings(settings)
    locale_signal.request()


class _SafeFormatter(string.Formatter):
    def get_field(self, field_name, args, kwargs):
        if "." in field_name or "[" in field_name:
            raise ValueError(f"unsupported field {field_name!r}")
        return super().get_field(field_name, args, kwargs)


_formatter = _SafeFormatter()


def _lookup(key: str, lang: str) -> Optional[str]:
    text = _load(lang).get(key)
    if text is None and lang != _FALLBACK_LANG:
        text = _load(_FALLBACK_LANG).get(key)
    return text


def _note_missing(key: str, lang: str) -> None:
    if (lang, key) not in _warned_missing:
        _warned_missing.add((lang, key))
        logger.debug("Missing translation key %r (language %r)", key, lang)


def _format(text: str, kwargs: dict) -> str:
    if "{" not in text:
        return text
    try:
        return _formatter.vformat(text, (), kwargs)
    except (KeyError, IndexError, ValueError, TypeError):
        return text


def tr(key: str, default: Optional[str] = None, **kwargs) -> str:
    return tr_for(current_language(), key, default, **kwargs)


def tr_for(lang: str, key: str, default: Optional[str] = None, **kwargs) -> str:
    text = _lookup(key, lang)
    if text is None:
        _note_missing(key, lang)
        if default is None:
            return key
        text = default
    return _format(text, kwargs) if kwargs else text


def register_locale_refresh(widget, callback: Callable[[], None]) -> None:
    callback()
    if widget is not None and not shiboken6.isValid(widget):
        return
    locale_signal.add(widget, callback)
