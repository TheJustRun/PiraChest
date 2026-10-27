from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Optional

import requests

logger = logging.getLogger(__name__)

_USER_AGENT = "PiraChest/3.0 Debrid"
_DEFAULT_TIMEOUT = 30.0
_MAX_TORRENT_BYTES = 16 * 1024 * 1024


class DebridError(Exception):
    def __init__(self, message: str, status: int | None = None, provider: str = "") -> None:
        super().__init__(message)
        self.status = status
        self.provider = provider


class DebridAuthError(DebridError):
    pass


class DebridRateLimited(DebridError):
    pass


class DebridTimeout(DebridError):
    pass


class DebridNotCached(DebridError):
    pass


@dataclass(frozen=True)
class DebridFile:
    index: int
    path: str
    size: int = 0
    provider_id: str = ""
    selected: bool = True


@dataclass
class DebridTransfer:
    id: str
    name: str = ""
    info_hash: str = ""
    status: str = "queued"
    progress: float = 0.0
    files: list[DebridFile] = field(default_factory=list)
    ready: bool = False
    needs_selection: bool = False
    error: str = ""
    raw: dict = field(default_factory=dict)


@dataclass(frozen=True)
class DebridLink:
    path: str
    url: str
    size: int = 0
    archive: bool = False


@dataclass
class DebridResolution:
    provider: str
    info_hash: str
    transfer: DebridTransfer
    files: list[DebridFile]
    links: list[DebridLink]
    archive: bool = False


def wanted_indices(file_ids: Optional[Iterable[int]]) -> list[int]:
    indices = set()
    for value in file_ids or ():
        text = str(value).strip()
        if text.lstrip("-").isdigit() and int(text) > 0:
            indices.add(int(text))
    return sorted(indices)


def fetch_torrent_bytes(source: str, timeout: float = _DEFAULT_TIMEOUT) -> tuple[bytes, str]:
    source = (source or "").strip()
    if source.startswith(("http://", "https://")):
        response = requests.get(source, timeout=timeout, headers={"User-Agent": _USER_AGENT})
        response.raise_for_status()
        data = response.content
        name = os.path.basename(source.split("?", 1)[0]) or "torrent"
    else:
        with open(source, "rb") as handle:
            data = handle.read(_MAX_TORRENT_BYTES + 1)
        name = os.path.basename(source) or "torrent"
    if len(data) > _MAX_TORRENT_BYTES:
        raise DebridError("The torrent file is too large")
    if not data.startswith(b"d"):
        raise DebridError("The supplied file is not a .torrent file")
    return data, name


class DebridProvider:
    key = ""
    label = ""
    base_url = ""
    help_url = ""
    token_label = "API token"

    def __init__(self, token: str, timeout: float = _DEFAULT_TIMEOUT) -> None:
        if not (token or "").strip():
            raise DebridAuthError(f"No {self.label or self.key} token has been saved")
        self._token = token.strip()
        self._timeout = max(5.0, float(timeout))
        self._session = requests.Session()
        self._session.headers.update({"User-Agent": _USER_AGENT, "Accept": "application/json"})
        self._apply_proxy()

    def _apply_proxy(self) -> None:
        try:
            from src.core.config import settings
            from urllib.parse import quote

            net = getattr(settings, "net", {}) or {}
            proxy_type = str(net.get("proxy_type") or "none")
            host = str(net.get("proxy_host") or "").strip()
            port = int(net.get("proxy_port") or 0)
            if proxy_type not in ("http", "socks5") or not host or not port:
                return
            user = str(net.get("proxy_user") or "")
            password = str(net.get("proxy_pass") or "")
            auth = f"{quote(user, safe='')}:{quote(password, safe='')}@" if user else ""
            scheme = "socks5h" if proxy_type == "socks5" else "http"
            url = f"{scheme}://{auth}{host}:{port}"
            self._session.trust_env = False
            self._session.proxies = {"http": url, "https": url}
        except Exception:
            logger.debug("Could not apply the proxy to the debrid session", exc_info=True)

    @property
    def token(self) -> str:
        return self._token

    def close(self) -> None:
        self._session.close()

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._token}"}

    def _request(self, method: str, path: str, *, params: Optional[dict] = None, data: Any = None,
                 json: Any = None, files: Any = None, headers: Optional[dict] = None) -> Any:
        url = path if path.startswith(("http://", "https://")) else f"{self.base_url}{path}"
        merged = dict(self._headers())
        merged.update(headers or {})
        try:
            response = self._session.request(
                method.upper(), url, params=params, data=data, json=json, files=files,
                headers=merged, timeout=self._timeout,
            )
        except requests.RequestException as exc:
            raise DebridError(f"{self.label} request failed: {exc}", provider=self.key) from exc
        status = response.status_code
        if status == 429:
            retry = response.headers.get("Retry-After", "")
            message = f"{self.label} rate limit reached"
            if retry:
                message += f" (retry after {retry}s)"
            raise DebridRateLimited(message, status, self.key)
        if status in (401, 403):
            raise DebridAuthError(f"{self.label} rejected the API token", status, self.key)
        if status >= 400:
            message = self._error_message(response)
            raise DebridError(message or f"{self.label} returned HTTP {status}", status, self.key)
        if not response.content:
            return {}
        try:
            payload = response.json()
        except ValueError as exc:
            raise DebridError(f"{self.label} returned an invalid response", status, self.key) from exc
        return payload

    @staticmethod
    def _error_message(response: requests.Response) -> str:
        try:
            payload = response.json()
        except ValueError:
            return (response.text or "").strip()[:400]
        if isinstance(payload, dict):
            for key in ("error", "message", "detail", "error_description"):
                value = payload.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()
            data = payload.get("data")
            if isinstance(data, dict):
                for key in ("error", "message"):
                    value = data.get(key)
                    if isinstance(value, str) and value.strip():
                        return value.strip()
        return ""

    def test(self) -> dict:
        raise NotImplementedError

    def add(self, source: str) -> DebridTransfer:
        raise NotImplementedError

    def status(self, transfer_id: str) -> DebridTransfer:
        raise NotImplementedError

    def select(self, transfer_id: str, files: list[DebridFile]) -> None:
        return None

    def links(self, transfer: DebridTransfer, files: list[DebridFile]) -> list[DebridLink]:
        raise NotImplementedError

    def delete(self, transfer_id: str) -> None:
        return None

    @staticmethod
    def _file(index: int, path: str, size: int = 0, provider_id: Any = "") -> DebridFile:
        return DebridFile(index=index, path=path or f"file-{index}", size=max(0, int(size or 0)), provider_id=str(provider_id or index))

    @staticmethod
    def _progress(value: Any) -> float:
        try:
            number = float(value or 0)
        except (TypeError, ValueError):
            return 0.0
        if number > 1.0:
            number /= 100.0
        return max(0.0, min(1.0, number))

    @staticmethod
    def _selected(files: list[DebridFile], file_ids: Optional[Iterable[int]]) -> list[DebridFile]:
        wanted = wanted_indices(file_ids)
        if not wanted:
            return list(files)
        chosen = [item for item in files if item.index in wanted]
        return chosen or list(files)


def wait_for_ready(
    provider: DebridProvider,
    transfer: DebridTransfer,
    *,
    timeout: float,
    cancel: Optional[Callable[[], bool]] = None,
    progress: Optional[Callable[[str, float], None]] = None,
    sleep: Callable[[float], None] = time.sleep,
    file_ids: Optional[Iterable[int]] = None,
) -> DebridTransfer:
    deadline = time.monotonic() + max(5.0, float(timeout))
    interval = 1.0
    current = transfer
    wanted = set(wanted_indices(file_ids))
    selected = False
    while True:
        if cancel is not None and cancel():
            raise DebridError("The debrid request was cancelled", provider=provider.key)
        if current.error:
            raise DebridError(f"{provider.label}: {current.error}", provider=provider.key)
        if wanted and current.files:
            available = {item.index for item in current.files}
            if wanted - available:
                raise DebridError("None of the selected files exist in this torrent", provider=provider.key)
            target = [item for item in current.files if item.index in wanted]
        else:
            target = list(current.files)
        pending = [item for item in target if not item.selected]
        if not selected and (current.needs_selection or pending):
            chosen = target or list(current.files)
            if chosen:
                provider.select(current.id, chosen)
                selected = True
                current = provider.status(current.id)
                continue
        if current.ready and not pending:
            return current
        if time.monotonic() >= deadline:
            raise DebridTimeout(f"{provider.label} did not finish resolving the torrent in time", provider=provider.key)
        if progress is not None:
            progress("waiting", current.progress)
        sleep(min(interval, max(0.0, deadline - time.monotonic())))
        interval = min(5.0, interval + 1.0)
        current = provider.status(current.id)
