from __future__ import annotations

import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DIST = ROOT / "dist" / "PiraChest"
INTERNAL = DIST / "_internal"

KILL = (
    "PySide6/opengl32sw.dll",
    "PySide6/Qt6WebEngine*",
    "PySide6/Qt6Pdf*",
    "PySide6/Qt6Designer*",
    "PySide6/Qt6UiTools*",
    "PySide6/Qt6Help*",
    "PySide6/Qt6Quick3D*",
    "PySide6/Qt63D*",
    "PySide6/Qt6Charts*",
    "PySide6/Qt6Graphs*",
    "PySide6/Qt6DataVisualization*",
    "PySide6/Qt6VirtualKeyboard*",
    "PySide6/Qt6Sql*",
    "PySide6/Qt6Test*",
    "PySide6/Qt6QuickControls2*",
    "PySide6/Qt6QuickTemplates2*",
    "PySide6/Qt6QuickDialogs2*",
    "PySide6/Qt6QuickEffects*",
    "PySide6/Qt6QuickShapes*",
    "PySide6/Qt6QuickLayouts*",
    "PySide6/Qt6QuickParticles*",
    "PySide6/Qt6QuickVectorImage*",
    "PySide6/Qt6QuickTimeline*",
    "PySide6/Qt6LabsStyleKit*",
    "PySide6/Qt6Scxml*",
    "PySide6/Qt6StateMachine*",
    "PySide6/Qt6RemoteObjects*",
    "PySide6/Qt6SpatialAudio*",
    "PySide6/Qt6Positioning*",
    "PySide6/Qt6Location*",
    "PySide6/Qt6Nfc*",
    "PySide6/Qt6Bluetooth*",
    "PySide6/Qt6SerialPort*",
    "PySide6/Qt6SerialBus*",
    "PySide6/Qt6Sensors*",
    "PySide6/Qt6TextToSpeech*",
    "PySide6/Qt6WebChannel*",
    "PySide6/Qt6WebSockets*",
    "PySide6/Qt6WebView*",
    "PySide6/Qt6QuickTest*",
    "PySide6/Qt6NetworkAuth*",
    "PySide6/Qt6HttpServer*",
    "PySide6/plugins/imageformats/qpdf.dll",
    "PySide6/plugins/imageformats/qtiff.dll",
    "PySide6/plugins/imageformats/qicns.dll",
    "PySide6/plugins/imageformats/qwbmp.dll",
    "PySide6/plugins/imageformats/qtga.dll",
    "PySide6/plugins/platforms/qdirect2d.dll",
    "PySide6/plugins/platforms/qminimal.dll",
    "PySide6/plugins/platforms/qoffscreen.dll",
    "PySide6/plugins/platforminputcontexts/qtvirtualkeyboardplugin.dll",
    "PySide6/Qt6Qml*",
    "PySide6/Qt6Quick*",
    "PySide6/QtQml*",
    "PySide6/QtQuick*",
    "PySide6/Qt6ShaderTools*",
    "PySide6/Qt6MultimediaQuick*",
    "PySide6/plugins/tls/qcertonlybackend.dll",
)

DIRS = (
    "PySide6/qml",
    "PySide6/translations",
    "PySide6/plugins/platforminputcontexts",
    "PySide6/plugins/sqldrivers",
    "PySide6/plugins/position",
    "PySide6/plugins/sensors",
    "PySide6/plugins/bluetooth",
    "PySide6/plugins/generic",
    "PySide6/plugins/qmltooling",
)

BLOAT = ("WebEngine", "Qt6Pdf", "Qt6Charts", "Qt6Graphs", "Qt6VirtualKeyboard",
         "Qt6Quick", "Qt6Qml", "Qt6ShaderTools", "Qt6Sql", "Qt6Designer",
         "Qt6Test", "fake_useragent")


def _remove(path: Path) -> None:
    if path.is_dir():
        shutil.rmtree(path, ignore_errors=True)
    elif path.exists():
        try:
            path.unlink()
        except OSError:
            pass


def main() -> int:
    exe = DIST / "PiraChest.exe"
    if not exe.is_file():
        print(f"[error] build output missing: {exe}", file=sys.stderr)
        return 1

    for pattern in KILL:
        for path in INTERNAL.glob(pattern):
            _remove(path)
    for name in DIRS:
        _remove(INTERNAL / name)
    for pattern in ("*.py", "*.pyc", "*.pyo"):
        for path in (INTERNAL / "src").rglob(pattern):
            _remove(path)
    for pattern in ("emoji_*.json",):
        for path in (INTERNAL / "emoji" / "unicode_codes").glob(pattern):
            _remove(path)

    files = [p for p in DIST.rglob("*") if p.is_file()]
    total_mb = sum(p.stat().st_size for p in files) / (1024 * 1024)
    print(f"[build] pruned build: {total_mb:.1f} MB")

    dirs = []
    for child in INTERNAL.iterdir() if INTERNAL.is_dir() else ():
        if child.is_dir():
            size = sum(p.stat().st_size for p in child.rglob("*") if p.is_file())
            dirs.append((size, child.name))
    for size, name in sorted(dirs, reverse=True)[:8]:
        print(f"  {size / (1024 * 1024):6.1f} MB  {name}")

    bloat = [p for p in files if any(token.lower() in p.name.lower() for token in BLOAT)]
    for path in bloat[:20]:
        print(f"[warn] unpruned: {path.relative_to(DIST)}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
