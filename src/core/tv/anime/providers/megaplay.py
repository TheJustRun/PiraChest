import base64
import json
import re
from typing import Any, Optional

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from ..anime_backend import fetch_html, fetch_json

BASE = "https://megaplay.buzz"
REFERER = f"{BASE}/"

EMBED_URL_RE = re.compile(r"https?://(?:[\w.-]+\.)?megaplay\.[\w.-]+/stream/[^\s\"'<>\\]+", re.IGNORECASE)

_DATA_ID_RE = re.compile(r'data-id="(\d+)"')
_ENC_KEY = b"i?LMTAx0Q6,:}50U"
_ENC_IV = b"W0;27ToaUpl_P%'c"

LANG_MAP = {
    "english": "en", "arabic": "ar", "french": "fr", "german": "de", "spanish": "es",
    "portuguese": "pt", "italian": "it", "russian": "ru", "japanese": "ja",
}


def find_embed_url(html: str) -> Optional[str]:
    m = EMBED_URL_RE.search(html or "")
    return m.group(0) if m else None


def decrypt_payload(payload: Any) -> Optional[dict]:
    enc = payload.get("enc") if isinstance(payload, dict) else None
    if not isinstance(enc, str) or not enc:
        return None
    try:
        key = (_ENC_KEY + b"\x00" * 32)[:32]
        ciphertext = base64.urlsafe_b64decode(enc + "=" * (-len(enc) % 4))
        decryptor = Cipher(algorithms.AES(key), modes.CBC(_ENC_IV)).decryptor()
        plain = decryptor.update(ciphertext) + decryptor.finalize()
        pad = plain[-1]
        if 1 <= pad <= 16 and plain[-pad:] == bytes([pad]) * pad:
            plain = plain[:-pad]
        parsed = json.loads(plain.decode("utf-8", errors="replace"))
        return parsed if isinstance(parsed, dict) else None
    except Exception:
        return None


def map_track(track: dict, source: str = "") -> dict:
    label = track.get("label") or track.get("language") or "Subtitle"
    key = label.lower().split(" ")[0]
    return {
        "url": track.get("file"),
        "label": label,
        "srclang": LANG_MAP.get(key, key[:2]),
        "default": bool(track.get("default")),
        "source": source,
    }


def _skip_range(value: Any) -> Optional[dict]:
    if not isinstance(value, dict):
        return None
    try:
        start, end = float(value.get("start") or 0), float(value.get("end") or 0)
    except (TypeError, ValueError):
        return None
    return {"start": start, "end": end} if start or end else None


async def resolve(page_url: str, *, referer: Optional[str] = None) -> dict:
    embed_m = EMBED_URL_RE.search(page_url or "")
    if embed_m:
        page_url = embed_m.group(0)

    referers = [referer, REFERER] if referer and referer != REFERER else [REFERER]
    file_id: Optional[str] = None
    last_exc: Optional[Exception] = None
    for ref in referers:
        try:
            page = await fetch_html(page_url, {"Referer": ref, "Accept-Language": "en-US,en;q=0.9"})
            m = _DATA_ID_RE.search(page)
            if not m:
                raise RuntimeError(f"megaplay: no data-id on {page_url}")
            file_id = m.group(1)
            break
        except Exception as exc:
            last_exc = exc
    if not file_id:
        raise last_exc or RuntimeError(f"megaplay: no data-id on {page_url}")

    payload = await fetch_json(
        f"{BASE}/stream/getSources",
        {"Referer": page_url, "X-Requested-With": "XMLHttpRequest"},
        params={"id": file_id, "platform": "OTHER"},
    )
    payload = payload if isinstance(payload, dict) else {}

    file_url = (decrypt_payload(payload) or {}).get("file")
    if not file_url:
        raise RuntimeError(f"megaplay: no playable source for {page_url}")

    subtitles = [
        map_track(t)
        for t in payload.get("tracks") or []
        if isinstance(t, dict) and t.get("file")
    ]
    return {
        "url": file_url,
        "referer": REFERER,
        "fileId": file_id,
        "subtitles": subtitles,
        "intro": _skip_range(payload.get("intro")),
        "outro": _skip_range(payload.get("outro")),
    }
