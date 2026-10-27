from __future__ import annotations

import base64
import ctypes
import json
import logging
import os
import sys
import threading
import time
from contextlib import suppress
from ctypes import wintypes
from typing import Optional

logger = logging.getLogger(__name__)

_FILE_NAME = "debrid_tokens.json"
_VERSION = 1
_CRYPTPROTECT_UI_FORBIDDEN = 0x1


class _DataBlob(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]


def _dpapi_available() -> bool:
    return sys.platform == "win32"


def _blob(data: bytes) -> tuple[_DataBlob, ctypes.Array]:
    buffer = ctypes.create_string_buffer(data, len(data))
    return _DataBlob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_char))), buffer


def _protect(data: bytes) -> bytes:
    if not _dpapi_available():
        return b"plain:" + base64.b64encode(data)
    source, source_buffer = _blob(data)
    output = _DataBlob()
    ok = ctypes.windll.crypt32.CryptProtectData(
        ctypes.byref(source), None, None, None, None, _CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(output)
    )
    if not ok:
        raise OSError(ctypes.get_last_error(), "Windows DPAPI encryption failed")
    try:
        return b"dpapi:" + ctypes.string_at(output.pbData, output.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(output.pbData)


def _unprotect(data: bytes) -> bytes:
    if data.startswith(b"plain:"):
        return base64.b64decode(data[6:])
    if not data.startswith(b"dpapi:"):
        raise ValueError("Unknown token protection scheme")
    if not _dpapi_available():
        raise OSError("This token store was encrypted with Windows DPAPI")
    payload = data[6:]
    source, source_buffer = _blob(payload)
    output = _DataBlob()
    ok = ctypes.windll.crypt32.CryptUnprotectData(
        ctypes.byref(source), None, None, None, None, _CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(output)
    )
    if not ok:
        raise OSError(ctypes.get_last_error(), "Windows DPAPI decryption failed")
    try:
        return ctypes.string_at(output.pbData, output.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(output.pbData)


class TokenStore:
    def __init__(self, path: str = "") -> None:
        self._path = path or os.path.join(self._default_dir(), _FILE_NAME)
        self._lock = threading.RLock()
        self._cache: Optional[dict[str, dict]] = None

    @staticmethod
    def _default_dir() -> str:
        try:
            from src.core.config import paths

            return paths.config_dir
        except Exception:
            return os.path.join(os.path.expanduser("~"), ".pirachest")

    @property
    def path(self) -> str:
        return self._path

    def _load(self) -> dict[str, dict]:
        if self._cache is not None:
            return self._cache
        data: dict[str, dict] = {}
        try:
            with open(self._path, encoding="utf-8") as handle:
                raw = json.load(handle)
            providers = raw.get("providers") if isinstance(raw, dict) else None
            if isinstance(providers, dict):
                data = {str(key): value for key, value in providers.items() if isinstance(value, dict)}
        except FileNotFoundError:
            pass
        except (OSError, ValueError) as exc:
            logger.warning("Could not read the debrid token store: %s", exc)
        self._cache = data
        return data

    def _save(self, data: dict[str, dict]) -> None:
        payload = {"version": _VERSION, "providers": data}
        directory = os.path.dirname(self._path) or "."
        os.makedirs(directory, exist_ok=True)
        temporary = f"{self._path}.{os.getpid()}.tmp"
        with open(temporary, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        with suppress(OSError):
            os.chmod(temporary, 0o600)
        os.replace(temporary, self._path)
        with suppress(OSError):
            os.chmod(self._path, 0o600)
        self._cache = data

    def get(self, provider: str) -> str:
        with self._lock:
            entry = self._load().get((provider or "").strip().lower())
            if not entry:
                return ""
            value = entry.get("value")
            if not isinstance(value, str) or not value:
                return ""
            try:
                return _unprotect(base64.b64decode(value)).decode("utf-8")
            except Exception:
                logger.warning("Could not decrypt the saved %s token", provider, exc_info=True)
                return ""

    def set(self, provider: str, token: str) -> None:
        key = (provider or "").strip().lower()
        if not key:
            raise ValueError("A provider key is required")
        token = (token or "").strip()
        with self._lock:
            data = dict(self._load())
            if not token:
                data.pop(key, None)
            else:
                protected = _protect(token.encode("utf-8"))
                data[key] = {
                    "scheme": "dpapi" if protected.startswith(b"dpapi:") else "plain",
                    "value": base64.b64encode(protected).decode("ascii"),
                    "updated": int(time.time()),
                }
            self._save(data)

    def delete(self, provider: str) -> None:
        self.set(provider, "")

    def has(self, provider: str) -> bool:
        return bool(self.get(provider))

    def providers(self) -> list[str]:
        with self._lock:
            return sorted(self._load())

    def last_updated(self, provider: str) -> int:
        with self._lock:
            entry = self._load().get((provider or "").strip().lower()) or {}
            return int(entry.get("updated") or 0)


tokens = TokenStore()
