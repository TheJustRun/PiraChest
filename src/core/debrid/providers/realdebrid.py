from __future__ import annotations

import os
from dataclasses import replace
from typing import Any

from ..archive import looks_like_archive
from ..base import DebridAuthError, DebridError, DebridFile, DebridLink, DebridProvider, DebridTransfer, fetch_torrent_bytes
from ..magnets import parse_source

_READY = frozenset({"downloaded"})
_BUSY = frozenset({"magnet_conversion", "waiting_files_selection", "queued", "downloading", "compressing", "uploading"})
_FAILED = frozenset({"magnet_error", "error", "virus", "dead"})


class RealDebridProvider(DebridProvider):
    key = "realdebrid"
    label = "Real-Debrid"
    base_url = "https://api.real-debrid.com/rest/1.0"
    help_url = "https://real-debrid.com/apitoken"
    token_label = "Private API token"

    def test(self) -> dict:
        data = self._request("GET", "/user")
        return {
            "account": data.get("username") or data.get("email") or "",
            "premium": bool(data.get("premium")) or str(data.get("type") or "").lower() == "premium",
            "expires": data.get("expiration") or "",
            "points": data.get("points"),
        }

    def add(self, source: str) -> DebridTransfer:
        source = (source or "").strip()
        if source.startswith("magnet:"):
            data = self._request("POST", "/torrents/addMagnet", data={"magnet": source})
        else:
            raw, name = fetch_torrent_bytes(source, self._timeout)
            data = self._request(
                "POST", "/torrents/addTorrent",
                files={"file": (name if name.endswith(".torrent") else f"{name}.torrent", raw, "application/x-bittorrent")},
            )
        transfer_id = str(data.get("id") or "")
        if not transfer_id:
            raise DebridError("Real-Debrid did not return a torrent id", provider=self.key)
        return self.status(transfer_id)

    @staticmethod
    def _is_selected(value: Any) -> bool:
        if isinstance(value, str):
            return value.strip().lower() not in ("", "0", "false", "no", "none")
        return True if value is None else bool(value)

    def status(self, transfer_id: str) -> DebridTransfer:
        data = self._request("GET", f"/torrents/info/{transfer_id}")
        raw_status = str(data.get("status") or "queued").lower()
        files = []
        index = 0
        for item in data.get("files") or []:
            if not isinstance(item, dict):
                continue
            index += 1
            path = str(item.get("path") or "").replace("\\", "/").lstrip("/")
            files.append(DebridFile(
                index=index,
                path=path or f"file-{index}",
                size=max(0, int(item.get("bytes") or 0)),
                provider_id=str(item.get("id") or index),
                selected=self._is_selected(item.get("selected", 1)),
            ))
        error = ""
        if raw_status in _FAILED:
            error = str(data.get("error") or raw_status.replace("_", " "))
        return DebridTransfer(
            id=str(data.get("id") or transfer_id),
            name=str(data.get("filename") or data.get("original_filename") or ""),
            info_hash=str(data.get("hash") or ""),
            status=raw_status,
            progress=self._progress(data.get("progress")),
            files=files,
            ready=raw_status in _READY and bool(files),
            needs_selection=raw_status == "waiting_files_selection",
            error=error,
            raw=data,
        )

    def select(self, transfer_id: str, files: list[DebridFile]) -> None:
        chosen = [item.provider_id for item in files if item.provider_id]
        value = ",".join(chosen) if chosen else "all"
        self._request("POST", f"/torrents/selectFiles/{transfer_id}", data={"files": value})

    def _unrestrict(self, path: str, url: str, size: int = 0) -> DebridLink:
        try:
            unrestricted = self._request("POST", "/unrestrict/link", data={"link": url})
        except DebridError:
            return DebridLink(path, url, size)
        download = str(unrestricted.get("download") or url)
        name = str(unrestricted.get("filename") or "") or path or os.path.basename(download)
        size = int(unrestricted.get("filesize") or size or 0)
        return DebridLink(name, download, size)

    @staticmethod
    def _same_basename(name: str, path: str) -> bool:
        def base(value: str) -> str:
            return os.path.basename(str(value or "").replace("\\", "/").rstrip("/")).lower()

        return base(name) == base(path)

    @staticmethod
    def _is_archive_link(link: DebridLink) -> bool:
        return looks_like_archive(link.path) or looks_like_archive(link.url)

    def links(self, transfer: DebridTransfer, files: list[DebridFile]) -> list[DebridLink]:
        if not files:
            return []
        data = self._request("GET", f"/torrents/info/{transfer.id}")
        urls = [str(url) for url in (data.get("links") or []) if isinstance(url, str) and url]
        if not urls:
            raise DebridError("Real-Debrid did not return any download links", provider=self.key)
        entries: list[tuple[int, bool]] = []
        index = 0
        for item in data.get("files") or []:
            if not isinstance(item, dict):
                continue
            index += 1
            entries.append((index, self._is_selected(item.get("selected", 1))))
        by_index: dict[int, str] = {}
        if len(urls) == len(entries):
            by_index = {file_index: url for (file_index, _), url in zip(entries, urls)}
        else:
            selected = [file_index for file_index, is_selected in entries if is_selected]
            if len(urls) == len(selected):
                by_index = dict(zip(selected, urls))
        if not by_index:
            # Real-Debrid packs non-video selections into a single archive link.
            if len(urls) == 1:
                link = self._unrestrict(files[0].path, urls[0], sum(int(item.size or 0) for item in files))
                if self._is_archive_link(link) and (
                    len(files) > 1 or not self._same_basename(link.path, files[0].path)
                ):
                    return [replace(link, archive=True)]
            raise DebridError(
                "Real-Debrid did not return a usable link for the selected file(s)",
                provider=self.key,
            )
        result = []
        for item in files:
            url = by_index.get(item.index)
            if not url:
                raise DebridError(
                    f"Real-Debrid did not return a link for {item.path or 'the selected file'}",
                    provider=self.key,
                )
            result.append(self._unrestrict(item.path, url, item.size))
        if len(result) == 1 and len(files) == 1:
            link = result[0]
            if self._is_archive_link(link) and not self._same_basename(link.path, files[0].path):
                result[0] = replace(link, archive=True)
        return result

    def delete(self, transfer_id: str) -> None:
        try:
            self._request("DELETE", f"/torrents/delete/{transfer_id}")
        except DebridError:
            return
