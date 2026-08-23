from __future__ import annotations

import json
import logging
import os
import sys
from typing import Optional

from PySide6.QtCore import QObject, Signal

from src.core.config import settings, apply_settings, save_settings, _PROJECT_ROOT

logger = logging.getLogger(__name__)


def _resolve_lang_dir() -> str:
    base = getattr(sys, "_MEIPASS", None)
    if base:
        candidate = os.path.join(base, "src", "gui", "lang")
        if os.path.isdir(candidate):
            return candidate
        candidate = os.path.join(base, "gui", "lang")
        if os.path.isdir(candidate):
            return candidate
    return os.path.join(_PROJECT_ROOT, "src", "gui", "lang")


_LANG_DIR = _resolve_lang_dir()
_FALLBACK_LANG = "en"

_strings_cache: dict[str, dict[str, str]] = {}
_meta_cache: Optional[dict[str, str]] = None
_available_files_cache: Optional[list[str]] = None


class _LocaleSignal(QObject):
    changed = Signal()


locale_signal = _LocaleSignal()


def _list_lang_files() -> list[str]:
    global _available_files_cache
    if _available_files_cache is not None:
        return _available_files_cache
    try:
        _available_files_cache = sorted(f for f in os.listdir(_LANG_DIR) if f.endswith(".json"))
    except OSError:
        _available_files_cache = []
    return _available_files_cache


def _load_raw(lang_code: str) -> dict:
    path = os.path.join(_LANG_DIR, f"{lang_code}.json")
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("Failed to load language file %s: %s", path, exc)
        return {}


def _load(lang_code: str) -> dict[str, str]:
    cached = _strings_cache.get(lang_code)
    if cached is not None:
        return cached
    data = _load_raw(lang_code)
    strings = data.get("strings")
    if not isinstance(strings, dict):
        strings = {k: v for k, v in data.items() if k != "_meta"}
    _strings_cache[lang_code] = strings
    return strings


def available_languages() -> dict[str, str]:
    global _meta_cache
    if _meta_cache is not None:
        return _meta_cache
    result: dict[str, str] = {}
    for fname in _list_lang_files():
        code = fname[:-5]
        data = _load_raw(code)
        result[code] = data.get("_meta", {}).get("display_name", code)
    if not result:
        result = {_FALLBACK_LANG: "English"}
    _meta_cache = result
    return result


def current_language() -> str:
    lang = getattr(settings, "language", _FALLBACK_LANG) or _FALLBACK_LANG
    return lang if lang in available_languages() else _FALLBACK_LANG


def set_language(lang_code: str) -> None:
    if lang_code == current_language() or lang_code not in available_languages():
        return
    apply_settings(language=lang_code)
    save_settings(settings)
    locale_signal.changed.emit()


def tr(key: str, **kwargs) -> str:
    lang = current_language()
    text = _load(lang).get(key)
    if text is None and lang != _FALLBACK_LANG:
        text = _load(_FALLBACK_LANG).get(key)
    if text is None:
        return key
    if not kwargs:
        return text
    try:
        return text.format(**kwargs)
    except (KeyError, IndexError, ValueError):
        return text


def register_locale_refresh(widget, callback) -> None:
    callback()
    locale_signal.changed.connect(callback)
