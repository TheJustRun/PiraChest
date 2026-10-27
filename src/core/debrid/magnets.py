from __future__ import annotations

import base64
import binascii
import hashlib
import os
import re
from dataclasses import dataclass, field
from typing import Any, Optional
from urllib.parse import parse_qs, unquote, urlsplit

_MAGNET_HASH = re.compile(r"xt=urn:btih:([A-Za-z0-9]+)", re.I)
_HEX_HASH = re.compile(r"^[0-9a-fA-F]{40}$")


class BencodeError(ValueError):
    pass


@dataclass(frozen=True)
class TorrentRef:
    info_hash: str = ""
    name: str = ""
    files: tuple[tuple[str, int], ...] = ()
    raw: bytes = b""
    magnet: str = ""
    path: str = ""
    source: str = ""


@dataclass(frozen=True)
class TorrentFileRef:
    path: str
    size: int


def _scan(data: bytes, pos: int) -> int:
    if pos >= len(data):
        raise BencodeError("Unexpected end of bencode data")
    char = data[pos:pos + 1]
    if char == b"i":
        end = data.find(b"e", pos)
        if end < 0:
            raise BencodeError("Unterminated integer")
        return end + 1
    if char in (b"l", b"d"):
        pos += 1
        while True:
            if pos >= len(data):
                raise BencodeError("Unterminated container")
            if data[pos:pos + 1] == b"e":
                return pos + 1
            pos = _scan(data, pos)
    if char.isdigit():
        colon = data.find(b":", pos)
        if colon < 0:
            raise BencodeError("Unterminated string length")
        try:
            length = int(data[pos:colon])
        except ValueError as exc:
            raise BencodeError("Invalid string length") from exc
        end = colon + 1 + length
        if end > len(data):
            raise BencodeError("String exceeds bencode data")
        return end
    raise BencodeError(f"Invalid bencode token at {pos}")


def _decode(data: bytes) -> Any:
    if not data:
        raise BencodeError("Empty bencode data")
    char = data[0:1]
    if char == b"i":
        end = data.find(b"e")
        if end < 0:
            raise BencodeError("Unterminated integer")
        return int(data[1:end])
    if char == b"l":
        items = []
        pos = 1
        while pos < len(data):
            if data[pos:pos + 1] == b"e":
                return items
            end = _scan(data, pos)
            items.append(_decode(data[pos:end]))
            pos = end
        raise BencodeError("Unterminated list")
    if char == b"d":
        items: dict[bytes, Any] = {}
        pos = 1
        while pos < len(data):
            if data[pos:pos + 1] == b"e":
                return items
            key_end = _scan(data, pos)
            key = _decode(data[pos:key_end])
            value_end = _scan(data, key_end)
            items[key] = _decode(data[key_end:value_end])
            pos = value_end
        raise BencodeError("Unterminated dictionary")
    if char.isdigit():
        colon = data.find(b":")
        if colon < 0:
            raise BencodeError("Unterminated string")
        length = int(data[:colon])
        return data[colon + 1:colon + 1 + length]
    raise BencodeError("Invalid bencode value")


def _decode_text(value: Any) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    return str(value or "")


def _text(raw: dict, key: bytes, default: str = "") -> str:
    return _decode_text(raw.get(key, default))


def info_hash_from_bytes(data: bytes) -> str:
    if not data.startswith(b"d"):
        raise BencodeError("Torrent file is not a bencoded dictionary")
    pos = 1
    while pos < len(data):
        if data[pos:pos + 1] == b"e":
            break
        key_end = _scan(data, pos)
        key = _decode(data[pos:key_end])
        value_start = key_end
        value_end = _scan(data, value_start)
        if key == b"info":
            return hashlib.sha1(data[value_start:value_end]).hexdigest()
        pos = value_end
    raise BencodeError("Torrent file has no info dictionary")


def info_hash_from_magnet(source: str) -> str:
    match = _MAGNET_HASH.search(source or "")
    if not match:
        return ""
    value = match.group(1).strip()
    if _HEX_HASH.match(value):
        return value.lower()
    try:
        padded = value.upper() + "=" * (-len(value) % 8)
        digest = base64.b32decode(padded, casefold=True)
    except (binascii.Error, ValueError):
        return ""
    return digest.hex() if len(digest) == 20 else ""


def _magnet_query(source: str) -> dict[str, list[str]]:
    query = source.split("?", 1)[1] if "?" in source else ""
    return parse_qs(query, keep_blank_values=True)


def magnet_display_name(source: str) -> str:
    if not source.startswith("magnet:"):
        return ""
    values = _magnet_query(source)
    return unquote((values.get("dn") or [""])[0]).strip()


def magnet_trackers(source: str) -> tuple[str, ...]:
    values = _magnet_query(source)
    return tuple(tracker for tracker in values.get("tr", []) if tracker)


def _normalise_path(path: str, name: str = "") -> str:
    path = (path or "").replace("\\", "/").strip("/")
    parts = [part for part in path.split("/") if part and part not in (".", "..")]
    joined = "/".join(parts)
    if not joined and name:
        joined = name
    return joined or "download"


def _torrent_files(raw: dict, root: str) -> tuple[tuple[str, int], ...]:
    files = raw.get(b"files")
    if isinstance(files, list):
        out = []
        for item in files:
            if not isinstance(item, dict):
                continue
            length = item.get(b"length")
            path = item.get(b"path")
            if not isinstance(length, int) or length < 0 or not isinstance(path, list):
                continue
            relative = "/".join(_decode_text(part) for part in path)
            out.append((_normalise_path(relative, root), length))
        return tuple(out)
    length = raw.get(b"length")
    if isinstance(length, int) and length >= 0:
        return ((_normalise_path(root, "download"), length),)
    return ()


def parse_source(source: str, *, read_bytes: bool = True) -> TorrentRef:
    source = (source or "").strip()
    if source.startswith("magnet:"):
        info_hash = info_hash_from_magnet(source)
        return TorrentRef(info_hash=info_hash, name=magnet_display_name(source) or info_hash, magnet=source, source=source)
    path = source
    if source.startswith(("http://", "https://")):
        return TorrentRef(name=os.path.basename(urlsplit(source).path) or "torrent", source=source)
    if os.path.isfile(source):
        if not read_bytes:
            return TorrentRef(path=os.path.abspath(source), name=os.path.basename(source), source=source)
        with open(source, "rb") as handle:
            data = handle.read()
        info_hash = info_hash_from_bytes(data)
        top = _decode(data)
        info = top.get(b"info", {}) if isinstance(top, dict) else {}
        name = _text(info, b"name") or os.path.basename(source)
        files = _torrent_files(info, name)
        return TorrentRef(info_hash=info_hash, name=name, files=files, raw=data, path=os.path.abspath(source), source=source)
    return TorrentRef(name=os.path.basename(source), source=source)


def torrent_info_hash(source: str) -> str:
    return parse_source(source, read_bytes=False).info_hash


def build_magnet(info_hash: str, name: str = "", trackers: tuple[str, ...] = ()) -> str:
    if not info_hash:
        raise ValueError("An info hash is required")
    parts = [f"magnet:?xt=urn:btih:{info_hash}"]
    if name:
        from urllib.parse import quote

        parts.append(f"dn={quote(name)}")
    for tracker in trackers:
        from urllib.parse import quote

        parts.append(f"tr={quote(tracker, safe='')}")
    return "&".join(parts)


def pick_video_file(files, episode: Optional[tuple[int, int]] = None):
    video_ext = (".mkv", ".mp4", ".avi", ".m4v", ".mov", ".webm", ".ts", ".m2ts", ".wmv", ".flv", ".mpg", ".mpeg")
    candidates = []
    for index, item in enumerate(files):
        path = getattr(item, "path", None) or (item[0] if isinstance(item, (tuple, list)) else str(item))
        size = getattr(item, "size", None) or (item[1] if isinstance(item, (tuple, list)) and len(item) > 1 else 0)
        if str(path).lower().endswith(video_ext):
            candidates.append((index, str(path), int(size or 0)))
    if not candidates:
        return None
    if episode:
        season, number = episode
        patterns = (
            re.compile(rf"(?i)s{season:02d}e{number:02d}"),
            re.compile(rf"(?i)(?:^|[^a-z0-9]){season}x{number:02d}(?:[^a-z0-9]|$)"),
            re.compile(rf"(?i)(?:^|[^a-z0-9])e(?:p|pisode)?[ ._-]?{number:02d}(?:[^a-z0-9]|$)"),
        )
        for pattern in patterns:
            matched = [item for item in candidates if pattern.search(item[1])]
            if matched:
                return max(matched, key=lambda item: item[2])
        if season == 0:
            matched = [item for item in candidates if re.search(rf"(?i)(?:^|[^a-z0-9]){number:02d}(?:[^a-z0-9]|$)", item[1])]
            if matched:
                return max(matched, key=lambda item: item[2])
    return max(candidates, key=lambda item: item[2])
