from __future__ import annotations

import os
from typing import Any

from ..base import DebridError, DebridFile, DebridLink, DebridProvider, DebridTransfer, fetch_torrent_bytes

_READY = frozenset({"ready", "completed", "finished"})
_FAILED = frozenset({"error", "failed", "expired", "deleted"})


class AllDebridProvider(DebridProvider):
    key = "alldebrid"
    label = "AllDebrid"
    base_url = "https://api.alldebrid.com/v4"
    help_url = "https://alldebrid.com/apikeys/"
    token_label = "API key"

    @staticmethod
    def _data(payload: Any) -> Any:
        if isinstance(payload, dict) and payload.get("status") == "error":
            details = payload.get("error")
            if isinstance(details, dict):
                message = details.get("message") or details.get("code")
            else:
                message = details
            raise DebridError(str(message or "AllDebrid request failed"), provider="alldebrid")
        return payload.get("data") if isinstance(payload, dict) else payload

    def test(self) -> dict:
        data = self._data(self._request("GET", "/user"))
        user = data.get("user") if isinstance(data, dict) else {}
        user = user if isinstance(user, dict) else {}
        return {
            "account": user.get("username") or "",
            "premium": bool(user.get("isPremium") or user.get("is_premium")),
            "expires": user.get("premiumUntil") or user.get("premium_until") or "",
        }

    def add(self, source: str) -> DebridTransfer:
        source = (source or "").strip()
        if source.startswith("magnet:"):
            payload = self._request("POST", "/magnet/upload", data={"magnets[]": source})
        else:
            raw, name = fetch_torrent_bytes(source, self._timeout)
            payload = self._request(
                "POST", "/magnet/upload/file",
                files={"files[]": (name if name.endswith(".torrent") else f"{name}.torrent", raw, "application/x-bittorrent")},
            )
        data = self._data(payload) or {}
        magnets = data.get("magnets") if isinstance(data, dict) else None
        if isinstance(magnets, list) and magnets:
            magnet = magnets[0]
        elif isinstance(magnets, dict):
            magnet = magnets
        else:
            magnet = {}
        transfer_id = str(magnet.get("id") or "") if isinstance(magnet, dict) else ""
        if not transfer_id:
            raise DebridError("AllDebrid did not return a magnet id", provider=self.key)
        return self.status(transfer_id)

    def _magnet(self, payload: Any) -> dict:
        data = self._data(payload) or {}
        magnets = data.get("magnets") if isinstance(data, dict) else None
        if isinstance(magnets, list):
            return magnets[0] if magnets and isinstance(magnets[0], dict) else {}
        if isinstance(magnets, dict):
            return magnets
        return data if isinstance(data, dict) else {}

    @staticmethod
    def _flatten(entries: Any, prefix: str = "") -> list[tuple[str, int, str]]:
        out: list[tuple[str, int, str]] = []
        if not isinstance(entries, list):
            return out
        for item in entries:
            if not isinstance(item, dict):
                continue
            name = str(item.get("n") or item.get("name") or "")
            path = f"{prefix}/{name}" if prefix else name
            size = int(item.get("s") or item.get("size") or 0)
            link = str(item.get("l") or item.get("link") or "")
            children = item.get("e") or item.get("files") or item.get("children")
            if isinstance(children, list) and children:
                out.extend(AllDebridProvider._flatten(children, path))
            else:
                out.append((path or f"file-{len(out) + 1}", size, link))
        return out

    def status(self, transfer_id: str) -> DebridTransfer:
        magnet = self._magnet(self._request("GET", "/magnet/status", params={"id": transfer_id}))
        raw_status = str(magnet.get("status") or "").lower()
        error = str(magnet.get("error") or "") if raw_status in _FAILED else ""
        files: list[DebridFile] = []
        links = magnet.get("links") if isinstance(magnet.get("links"), list) else []
        if links:
            for index, item in enumerate(links, start=1):
                if not isinstance(item, dict):
                    continue
                files.append(self._file(index, str(item.get("filename") or ""), item.get("size") or 0, item.get("link") or ""))
        if not files:
            for index, (path, size, link) in enumerate(self._flatten(magnet.get("files")), start=1):
                files.append(self._file(index, path, size, link))
        download = magnet.get("downloaded")
        total = magnet.get("size")
        progress = 1.0 if raw_status in _READY else (float(download) / float(total) if total and download else 0.0)
        return DebridTransfer(
            id=str(magnet.get("id") or transfer_id),
            name=str(magnet.get("name") or magnet.get("filename") or ""),
            info_hash=str(magnet.get("hash") or ""),
            status=raw_status or "queued",
            progress=max(0.0, min(1.0, progress)),
            files=files,
            ready=raw_status in _READY and bool(files),
            error=error,
            raw=magnet,
        )

    def links(self, transfer: DebridTransfer, files: list[DebridFile]) -> list[DebridLink]:
        current = self.status(transfer.id)
        by_index = {item.index: item for item in current.files}
        result = []
        for selected in files:
            source = by_index.get(selected.index, selected)
            link = source.provider_id or selected.provider_id
            if not link:
                raise DebridError("AllDebrid did not return a link for a selected file", provider=self.key)
            payload = self._request("POST", "/link/unlock", data={"link": link})
            data = self._data(payload) or {}
            url = str(data.get("link") or "") if isinstance(data, dict) else ""
            if not url:
                url = link
            name = str(data.get("filename") or source.path) if isinstance(data, dict) else source.path
            size = int(data.get("size") or data.get("filesize") or source.size or 0) if isinstance(data, dict) else source.size
            result.append(DebridLink(name or os.path.basename(url) or source.path, url, size))
        return result

    def delete(self, transfer_id: str) -> None:
        try:
            self._request("POST", "/magnet/delete", data={"id": transfer_id})
        except DebridError:
            return
