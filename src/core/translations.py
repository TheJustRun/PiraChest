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
from PySide6.QtCore import QObject, Signal

from src.core.config import settings, apply_settings, save_settings, _PROJECT_ROOT

logger = logging.getLogger(__name__)

_FALLBACK_LANG = "en"
_CODE_RE = re.compile(r"^[A-Za-z0-9_-]{1,32}$")
_DISPLAY_NAME_RE = re.compile(r'"display_name"\s*:\s*("(?:[^"\\]|\\.)*")')


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
    locale_signal.changed.emit()


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
    lang = current_language()
    text = _lookup(key, lang)
    if text is None:
        _note_missing(key, lang)
        if default is None:
            return key
        text = default
    return _format(text, kwargs) if kwargs else text


def register_locale_refresh(widget, callback: Callable[[], None]) -> None:
    callback()
    ref = weakref.ref(widget) if widget is not None else None

    def wrapper() -> None:
        if ref is not None:
            target = ref()
            if target is None or not shiboken6.isValid(target):
                _disconnect()
                return
        try:
            callback()
        except RuntimeError:
            logger.debug("Locale refresh skipped for a deleted widget", exc_info=True)
        except Exception:
            logger.exception("Locale refresh callback failed")

    def _disconnect(*_args) -> None:
        try:
            locale_signal.changed.disconnect(wrapper)
        except (RuntimeError, TypeError, SystemError):
            pass

    if widget is not None and not shiboken6.isValid(widget):
        return
    locale_signal.changed.connect(wrapper)
    if widget is not None:
        try:
            widget.destroyed.connect(_disconnect)
        except (RuntimeError, AttributeError):
            pass
