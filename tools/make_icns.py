from __future__ import annotations

import struct
import zlib
from pathlib import Path

_TYPES = {
    16: b"icp4",
    32: b"icp5",
    64: b"icp6",
    128: b"ic07",
    256: b"ic08",
    512: b"ic09",
    1024: b"ic10",
}


def _png(width: int, height: int, rows: list[bytes]) -> bytes:
    raw = b"".join(b"\x00" + row for row in rows)

    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)

    header = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header)
            + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b""))


def _dib_to_png(blob: bytes) -> bytes | None:
    if len(blob) < 40:
        return None
    size, width, doubled, _planes, bits, compression = struct.unpack_from("<IiiHHI", blob, 0)
    if size < 40 or width <= 0 or doubled <= 0 or compression != 0 or bits not in (32, 24):
        return None
    height = doubled // 2
    step = width * (4 if bits == 32 else 3)
    start = size
    if len(blob) < start + step * height:
        return None
    rows = []
    for y in range(height - 1, -1, -1):
        line = blob[start + y * step:start + (y + 1) * step]
        if bits == 32:
            pixels = bytearray(width * 4)
            for x in range(width):
                b, g, r, a = line[x * 4:x * 4 + 4]
                pixels[x * 4:x * 4 + 4] = bytes((r, g, b, a))
            rows.append(bytes(pixels))
        else:
            pixels = bytearray(width * 4)
            for x in range(width):
                b, g, r = line[x * 3:x * 3 + 3]
                pixels[x * 4:x * 4 + 4] = bytes((r, g, b, 255))
            rows.append(bytes(pixels))
    return _png(width, height, rows)


def _read_ico(path: str) -> dict[int, bytes]:
    data = Path(path).read_bytes()
    if len(data) < 6 or data[:4] != b"\x00\x00\x01\x00":
        return {}
    count = struct.unpack_from("<H", data, 4)[0]
    frames: dict[int, bytes] = {}
    for index in range(count):
        offset = 6 + index * 16
        if offset + 16 > len(data):
            break
        width = data[offset] or 256
        height = data[offset + 1] or 256
        size, start = struct.unpack_from("<II", data, offset + 8)
        blob = data[start:start + size]
        if blob[:8] == b"\x89PNG\r\n\x1a\n":
            frames[max(width, height)] = blob
        else:
            converted = _dib_to_png(blob)
            if converted is not None:
                frames[max(width, height)] = converted
    return frames


def build_png(destination: str, source: str) -> str | None:
    frames = _read_ico(source)
    if not frames:
        return None
    target = Path(destination)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(frames[max(frames)])
    return str(target)


def build_icns(destination: str, source: str) -> str | None:
    frames = _read_ico(source)
    chunks = []
    for size in sorted(frames):
        kind = _TYPES.get(size)
        if kind is not None:
            blob = frames[size]
            chunks.append(kind + struct.pack(">I", len(blob) + 8) + blob)
    if not chunks:
        return None
    payload = b"".join(chunks)
    output = b"icns" + struct.pack(">I", len(payload) + 8) + payload
    target = Path(destination)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(output)
    return str(target)
