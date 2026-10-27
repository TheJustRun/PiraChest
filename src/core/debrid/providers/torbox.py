from __future__ import annotations

import os
from typing import Any, Optional

from ..base import DebridError, DebridFile, DebridLink, DebridProvider, DebridTransfer, fetch_torrent_bytes

_READY = frozenset({"completed", "cached", "finished", "seeding"})
_FAILED = frozenset({"error", "failed", "deleted", "expired", "missingfiles"})


class TorBoxProvider(DebridProvider):
    key = "torbox"
    label = "TorBox"
    base_url = "https://api.torbox.app/v1/api"
    help_url = "https://torbox.app/settings"
    token_label = "API key"

    @staticmethod
    def _payload(payload: Any) -> Any:
        if isinstance(payload, dict) and payload.get("success") is False:
            raise DebridError(str(payload.get("detail") or payload.get("error") or "TorBox request failed"), provider="torbox")
        return payload.get("data") if isinstance(payload, dict) else payload

    def test(self) -> dict:
        data = self._payload(self._request("GET", "/user/me")) or {}
        if isinstance(data, list) and data:
            data = data[0]
        data = data if isinstance(data, dict) else {}
        return {
            "account": str(data.get("email") or data.get("username") or ""),
            "premium": bool(data.get("plan") or data.get("is_premium") or data.get("premium")),
            "expires": data.get("premium_expires_at") or data.get("expires_at") or "",
        }

    def add(self, source: str) -> DebridTransfer:
        source = (source or "").strip()
        if source.startswith("magnet:"):
            payload = self._request("POST", "/torrents/createtorrent", data={"magnet": source, "seed": 1, "allow_zip": "false"})
        else:
            raw, name = fetch_torrent_bytes(source, self._timeout)
            payload = self._request(
                "POST", "/torrents/createtorrent",
                data={"seed": 1, "allow_zip": "false"},
                files={"file": (name if name.endswith(".torrent") else f"{name}.torrent", raw, "application/x-bittorrent")},
            )
        data = self._payload(payload) or {}
        if isinstance(data, list):
            data = data[0] if data else {}
        transfer_id = str(data.get("torrent_id") or data.get("id") or "") if isinstance(data, dict) else ""
        if not transfer_id:
            raise DebridError("TorBox did not return a torrent id", provider=self.key)
        return self.status(transfer_id)

    def _torrent(self, transfer_id: str) -> dict:
        data = self._payload(self._request("GET", "/torrents/mylist", params={"id": transfer_id, "bypass_cache": "true"})) or {}
        if isinstance(data, dict):
            for key in ("torrents", "data"):
                nested = data.get(key)
                if isinstance(nested, list):
                    data = nested
                    break
        if isinstance(data, list):
            for item in data:
                if isinstance(item, dict) and str(item.get("id") or "") == str(transfer_id):
                    return item
            return data[0] if data and isinstance(data[0], dict) else {}
        return data if isinstance(data, dict) else {}

    def status(self, transfer_id: str) -> DebridTransfer:
        item = self._torrent(transfer_id)
        state = str(item.get("download_state") or item.get("state") or "").lower()
        finished = bool(item.get("download_finished")) or state in _READY
        error = str(item.get("download_error") or item.get("error") or state) if state in _FAILED else ""
        files = []
        for index, entry in enumerate(item.get("files") or [], start=1):
            if not isinstance(entry, dict):
                continue
            files.append(DebridFile(
                index=index,
                path=str(entry.get("name") or entry.get("path") or f"file-{index}"),
                size=max(0, int(entry.get("size") or 0)),
                provider_id=str(entry.get("id") or index),
            ))
        return DebridTransfer(
            id=str(item.get("id") or transfer_id),
            name=str(item.get("name") or ""),
            info_hash=str(item.get("hash") or ""),
            status=state or "queued",
            progress=self._progress(item.get("progress")),
            files=files,
            ready=finished and bool(files),
            error=error,
            raw=item,
        )

    def links(self, transfer: DebridTransfer, files: list[DebridFile]) -> list[DebridLink]:
        result = []
        for item in files:
            payload = self._request(
                "GET", "/torrents/requestdl",
                params={"token": self._token, "torrent_id": transfer.id, "file_id": item.provider_id, "zip_link": "false"},
            )
            data = self._payload(payload)
            url = data if isinstance(data, str) else ""
            if not url and isinstance(data, dict):
                url = str(data.get("url") or data.get("download_url") or "")
            if not url:
                raise DebridError("TorBox did not return a download link", provider=self.key)
            result.append(DebridLink(item.path or os.path.basename(url), url, item.size))
        return result

    def delete(self, transfer_id: str) -> None:
        try:
            self._request("POST", "/torrents/controltorrent", data={"torrent_id": transfer_id, "operation": "delete"})
        except DebridError:
            return
