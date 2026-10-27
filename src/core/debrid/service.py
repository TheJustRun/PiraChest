from __future__ import annotations

import hashlib
import os
import threading
import time
from typing import Callable, Iterable, Optional

from .base import (
    DebridAuthError,
    DebridError,
    DebridFile,
    DebridLink,
    DebridNotCached,
    DebridProvider,
    DebridRateLimited,
    DebridResolution,
    DebridTimeout,
    DebridTransfer,
    wait_for_ready,
    wanted_indices,
)
from .cache import ResolutionCache
from .cache import cache as default_cache
from .magnets import parse_source, pick_video_file
from .providers import PROVIDERS, PROVIDER_INFO, PROVIDER_ORDER
from .tokens import TokenStore
from .tokens import tokens as default_tokens

DEFAULT_TIMEOUT = 600
_STALE_STATUSES = frozenset({"error", "expired", "deleted", "missingfiles", "magnet_error", "dead", "virus"})


class DebridService:
    def __init__(self, provider: DebridProvider, *, cache: Optional[ResolutionCache] = None,
                 timeout: float = DEFAULT_TIMEOUT) -> None:
        self._provider = provider
        self._cache = cache or default_cache
        self._timeout = max(15.0, float(timeout))
        self._lock = threading.Lock()

    @property
    def provider(self) -> DebridProvider:
        return self._provider

    @property
    def key(self) -> str:
        return self._provider.key

    @property
    def label(self) -> str:
        return self._provider.label

    @classmethod
    def from_settings(cls, provider_key: str = "", *, tokens: Optional[TokenStore] = None,
                      cache: Optional[ResolutionCache] = None, timeout: Optional[float] = None) -> "DebridService":
        from src.core.config import settings

        key = (provider_key or getattr(settings, "debrid_provider", "") or "").strip().lower()
        if key not in PROVIDERS:
            raise DebridError("Choose a debrid provider in Settings first")
        token = (tokens or default_tokens).get(key)
        if not token:
            raise DebridAuthError(f"Save a {PROVIDER_INFO[key]['label']} token in Settings first")
        wait = timeout if timeout is not None else getattr(settings, "debrid_timeout", DEFAULT_TIMEOUT)
        return cls(PROVIDERS[key](token, timeout=30.0), cache=cache, timeout=wait)

    def test(self) -> dict:
        return self._provider.test()

    def close(self) -> None:
        self._provider.close()

    def _transfer(self, source: str, *, timeout: Optional[float] = None,
                  cancel: Optional[Callable[[], bool]] = None,
                  progress: Optional[Callable[[str, float], None]] = None) -> tuple[DebridTransfer, str]:
        ref = parse_source(source, read_bytes=False)
        if not ref.info_hash and ref.path and os.path.isfile(ref.path):
            ref = parse_source(source, read_bytes=True)
        info_hash = ref.info_hash
        cache_key = info_hash
        if not cache_key and source.startswith(("http://", "https://")):
            cache_key = "url:" + hashlib.sha1(source.strip().encode("utf-8")).hexdigest()
        provider = self._provider
        current: Optional[DebridTransfer] = None
        if cache_key:
            cached = self._cache.get(provider.key, cache_key)
            if cached:
                try:
                    current = provider.status(str(cached.get("transfer_id") or ""))
                    stale = (not current.status or current.error or current.status in _STALE_STATUSES
                             or (current.ready and not current.files)
                             or (not current.raw and not current.files))
                    if stale:
                        self._cache.forget(provider.key, cache_key)
                        current = None
                except DebridError:
                    self._cache.forget(provider.key, cache_key)
                    current = None
        if current is None:
            if progress is not None:
                progress("adding", 0.0)
            current = provider.add(source)
            if cache_key and current.id:
                self._cache.put(provider.key, cache_key, current.id, current.name)
        if not current.id:
            raise DebridError(f"{provider.label} did not return a transfer id", provider=provider.key)
        if current.error:
            raise DebridError(f"{provider.label}: {current.error}", provider=provider.key)
        return current, info_hash

    def _resolution(self, current: DebridTransfer, info_hash: str,
                    files: list[DebridFile]) -> DebridResolution:
        provider = self._provider
        links = provider.links(current, files)
        if not links:
            raise DebridError(f"{provider.label} did not return any usable links", provider=provider.key)
        archived = [link for link in links if link.archive]
        if archived:
            if len(archived) != len(links):
                raise DebridError(
                    f"{provider.label} returned a mixed archive and file link selection",
                    provider=provider.key,
                )
            return DebridResolution(provider=provider.key, info_hash=info_hash or current.info_hash,
                                    transfer=current, files=files, links=links, archive=True)
        if len(links) != len(files):
            raise DebridError(
                f"{provider.label} returned {len(links)} link(s) for {len(files)} file(s)",
                provider=provider.key,
            )
        return DebridResolution(provider=provider.key, info_hash=info_hash or current.info_hash,
                                transfer=current, files=files, links=links)

    def resolve(self, source: str, *, file_ids: Optional[Iterable[int]] = None,
                timeout: Optional[float] = None, cancel: Optional[Callable[[], bool]] = None,
                progress: Optional[Callable[[str, float], None]] = None,
                sleep: Callable[[float], None] = time.sleep) -> DebridResolution:
        provider = self._provider
        current, info_hash = self._transfer(source, timeout=timeout, cancel=cancel, progress=progress)
        wanted = wanted_indices(file_ids)
        current = wait_for_ready(
            provider, current, timeout=timeout if timeout is not None else self._timeout,
            cancel=cancel, progress=progress, sleep=sleep, file_ids=wanted,
        )
        if not current.files:
            raise DebridError(f"{provider.label} finished without a file list", provider=provider.key)
        if progress is not None:
            progress("links", current.progress)
        files = [item for item in current.files if item.index in wanted] if wanted else list(current.files)
        if not files:
            raise DebridError("None of the selected files exist in this torrent", provider=provider.key)
        return self._resolution(current, info_hash, files)

    def _wait_for_files(self, current: DebridTransfer, *, timeout: float,
                        cancel: Optional[Callable[[], bool]],
                        progress: Optional[Callable[[str, float], None]] = None,
                        sleep: Callable[[float], None] = time.sleep) -> DebridTransfer:
        provider = self._provider
        deadline = time.monotonic() + max(5.0, float(timeout))
        interval = 1.0
        while not current.files:
            if cancel is not None and cancel():
                raise DebridError("The debrid request was cancelled", provider=provider.key)
            if current.error:
                raise DebridError(f"{provider.label}: {current.error}", provider=provider.key)
            if current.ready:
                raise DebridError(f"{provider.label} finished without a file list", provider=provider.key)
            if time.monotonic() >= deadline:
                raise DebridTimeout(f"{provider.label} did not return a file list in time", provider=provider.key)
            if progress is not None:
                progress("waiting", current.progress)
            sleep(min(interval, max(0.0, deadline - time.monotonic())))
            interval = min(5.0, interval + 1.0)
            current = provider.status(current.id)
        return current

    @staticmethod
    def _pick_stream_file(files: list[DebridFile], file_index: Optional[int],
                          episode: Optional[tuple[int, int]]) -> Optional[DebridFile]:
        if file_index is not None and file_index > 0:
            for item in files:
                if item.index == file_index:
                    return item
        picked = pick_video_file(files, episode)
        return files[picked[0]] if picked is not None else None

    def resolve_stream(self, source: str, *, file_index: Optional[int] = None,
                       episode: Optional[tuple[int, int]] = None, timeout: Optional[float] = None,
                       cancel: Optional[Callable[[], bool]] = None,
                       sleep: Callable[[float], None] = time.sleep) -> DebridResolution:
        provider = self._provider
        current, info_hash = self._transfer(source, timeout=timeout, cancel=cancel)
        if not current.files:
            current = self._wait_for_files(
                current, timeout=timeout if timeout is not None else self._timeout,
                cancel=cancel, sleep=sleep,
            )
        chosen = self._pick_stream_file(current.files, file_index, episode)
        if chosen is None:
            raise DebridError(f"{provider.label} did not find a playable video file", provider=provider.key)
        current = wait_for_ready(
            provider, current, timeout=timeout if timeout is not None else self._timeout,
            cancel=cancel, sleep=sleep, file_ids=[chosen.index],
        )
        chosen = next((item for item in current.files if item.index == chosen.index), chosen)
        resolution = self._resolution(current, info_hash, [chosen])
        if resolution.archive:
            raise DebridError(
                f"{provider.label} packed the selected file into an archive that cannot be streamed",
                provider=provider.key,
            )
        return resolution

    def cleanup(self, resolution: DebridResolution) -> None:
        self._provider.delete(resolution.transfer.id)

    def file_list(self, source: str, *, timeout: Optional[float] = None,
                  cancel: Optional[Callable[[], bool]] = None) -> list[tuple[str, int]]:
        provider = self._provider
        current, _info_hash = self._transfer(source, timeout=timeout, cancel=cancel)
        if not current.files:
            current = wait_for_ready(
                provider, current, timeout=timeout if timeout is not None else self._timeout,
                cancel=cancel, sleep=time.sleep,
            )
        if not current.files:
            raise DebridError(f"{provider.label} finished without a file list", provider=provider.key)
        return [(item.path, item.size) for item in current.files]

    def files(self, source: str, *, timeout: Optional[float] = None,
              cancel: Optional[Callable[[], bool]] = None,
              wait: float = 0.0) -> list[DebridFile]:
        current, _info_hash = self._transfer(source, timeout=timeout, cancel=cancel)
        if not current.files and wait > 0:
            try:
                current = self._wait_for_files(current, timeout=wait, cancel=cancel)
            except DebridTimeout:
                return []
        return list(current.files)


def configured(provider_key: str = "", *, tokens: Optional[TokenStore] = None) -> bool:
    try:
        from src.core.config import settings

        key = (provider_key or getattr(settings, "debrid_provider", "") or "").strip().lower()
        return key in PROVIDERS and bool((tokens or default_tokens).get(key))
    except Exception:
        return False


def provider_choices() -> list[tuple[str, str]]:
    return [(key, PROVIDER_INFO[key]["label"]) for key in PROVIDER_ORDER]


__all__ = [
    "DEFAULT_TIMEOUT",
    "DebridAuthError",
    "DebridError",
    "DebridFile",
    "DebridLink",
    "DebridNotCached",
    "DebridProvider",
    "DebridRateLimited",
    "DebridResolution",
    "DebridService",
    "DebridTimeout",
    "DebridTransfer",
    "PROVIDERS",
    "PROVIDER_INFO",
    "PROVIDER_ORDER",
    "TokenStore",
    "cache",
    "configured",
    "default_cache",
    "default_tokens",
    "parse_source",
    "pick_video_file",
    "provider_choices",
    "tokens",
    "wait_for_ready",
]
