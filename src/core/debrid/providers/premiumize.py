from __future__ import annotations

import os
from typing import Any, Optional

from ..base import DebridError, DebridFile, DebridLink, DebridProvider, DebridTransfer, fetch_torrent_bytes

_READY = frozenset({"finished", "seeding"})
_FAILED = frozenset({"error", "stalled", "deleted", "expired"})


class PremiumizeProvider(DebridProvider):
    key = "premiumize"
    label = "Premiumize"
    base_url = "https://www.premiumize.me/api"
    help_url = "https://www.premiumize.me/account"
    token_label = "API key"

    def _headers(self) -> dict[str, str]:
        return {}

    def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        params = dict(kwargs.pop("params", None) or {})
        params["apikey"] = self._token
        return super()._request(method, path, params=params, **kwargs)

    def test(self) -> dict:
        data = self._request("GET", "/account/info")
        if not isinstance(data, dict) or data.get("status") == "error":
            raise DebridError("Premiumize rejected the API key", provider=self.key)
        return {
            "account": str(data.get("customer_id") or data.get("email") or ""),
            "premium": bool(data.get("premium_until")),
            "expires": data.get("premium_until") or "",
        }

    def add(self, source: str) -> DebridTransfer:
        source = (source or "").strip()
        if source.startswith("magnet:") or source.startswith(("http://", "https://")):
            payload = self._request("POST", "/transfer/create", data={"src": source})
        else:
            raw, name = fetch_torrent_bytes(source, self._timeout)
            payload = self._request(
                "POST", "/transfer/create",
                files={"file": (name if name.endswith(".torrent") else f"{name}.torrent", raw, "application/x-bittorrent")},
            )
        if not isinstance(payload, dict) or payload.get("status") == "error":
            raise DebridError(str((payload or {}).get("message") or "Premiumize could not add the torrent"), provider=self.key)
        transfer_id = str(payload.get("id") or "")
        if not transfer_id:
            raise DebridError("Premiumize did not return a transfer id", provider=self.key)
        return self.status(transfer_id)

    def _list_folder(self, folder_id: str, prefix: str = "", depth: int = 0) -> list[tuple[str, int, str]]:
        if not folder_id or depth > 6:
            return []
        payload = self._request("GET", "/folder/list", params={"id": folder_id})
        content = payload.get("content") if isinstance(payload, dict) else None
        out: list[tuple[str, int, str]] = []
        for item in content if isinstance(content, list) else []:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "")
            path = f"{prefix}/{name}" if prefix else name
            if str(item.get("type") or "") == "folder":
                out.extend(self._list_folder(str(item.get("id") or ""), path, depth + 1))
            else:
                out.append((path or f"file-{len(out) + 1}", int(item.get("size") or 0), str(item.get("link") or "")))
        return out

    def _transfer(self, transfer_id: str) -> dict:
        payload = self._request("GET", "/transfer/list")
        transfers = payload.get("transfers") if isinstance(payload, dict) else None
        for item in transfers if isinstance(transfers, list) else []:
            if isinstance(item, dict) and str(item.get("id") or "") == str(transfer_id):
                return item
        return {}

    def status(self, transfer_id: str) -> DebridTransfer:
        item = self._transfer(transfer_id)
        raw_status = str(item.get("status") or "").lower()
        error = str(item.get("message") or raw_status) if raw_status in _FAILED else ""
        files: list[DebridFile] = []
        if raw_status in _READY:
            folder = str(item.get("folder_id") or item.get("file_id") or "")
            for index, (path, size, link) in enumerate(self._list_folder(folder), start=1):
                files.append(DebridFile(index=index, path=path, size=size, provider_id=link or str(index)))
        return DebridTransfer(
            id=str(item.get("id") or transfer_id),
            name=str(item.get("name") or ""),
            info_hash=str(item.get("hash") or ""),
            status=raw_status or "queued",
            progress=self._progress(item.get("progress")),
            files=files,
            ready=raw_status in _READY and bool(files),
            error=error,
            raw=item,
        )

    def links(self, transfer: DebridTransfer, files: list[DebridFile]) -> list[DebridLink]:
        current = self.status(transfer.id)
        by_index = {item.index: item for item in current.files}
        result = []
        for selected in files:
            source = by_index.get(selected.index, selected)
            url = source.provider_id or selected.provider_id
            if not url or not url.startswith(("http://", "https://")):
                raise DebridError("Premiumize did not return a direct link", provider=self.key)
            result.append(DebridLink(source.path or os.path.basename(url), url, source.size))
        return result

    def delete(self, transfer_id: str) -> None:
        try:
            self._request("POST", "/transfer/delete", data={"id": transfer_id})
        except DebridError:
            return
