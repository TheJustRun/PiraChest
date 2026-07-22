from __future__ import annotations

import os

_GUI_DIR = os.path.dirname(os.path.abspath(__file__))

_LOGO_CANDIDATES = ("logo.ico", "logo.png", "logo.svg", "logo.jpg", "logo.jpeg")

def find_logo_path() -> str | None:
    for name in _LOGO_CANDIDATES:
        candidate = os.path.join(_GUI_DIR, name)
        if os.path.isfile(candidate):
            return candidate
    return None