from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DIST = ROOT / "dist" / "PiraChest"
INTERNAL = DIST / "_internal"
WIX_DIR = ROOT / "build"
WIX_UPGRADE_CODE = "a3f6c2d1-7b4e-4c9a-8f2d-5e1b0c7a9d34"

_MSI_TEMPLATE = r"""<?xml version="1.0" encoding="utf-8"?>
<Wix xmlns="http://wixtoolset.org/schemas/v4/wxs">
  <Package Name="PiraChest" Manufacturer="PiraChest" Version="$(var.Version)"
           UpgradeCode="{upgrade}" Scope="perUser" InstallerVersion="500">
    <MajorUpgrade AllowSameVersionUpgrades="yes"
                  DowngradeErrorMessage="A newer version of PiraChest is already installed." />
    <MediaTemplate EmbedCab="yes" CompressionLevel="high" />
    <Icon Id="AppIcon" SourceFile="$(var.SourceDir)\PiraChest.exe" />
    <Property Id="ARPPRODUCTICON" Value="AppIcon" />
    <StandardDirectory Id="LocalAppDataFolder">
      <Directory Id="ProgramsFolder" Name="Programs">
        <Directory Id="INSTALLFOLDER" Name="PiraChest" />
      </Directory>
    </StandardDirectory>
    <StandardDirectory Id="ProgramMenuFolder">
      <Directory Id="AppStartMenuFolder" Name="PiraChest" />
    </StandardDirectory>
    <Feature Id="Main">
      <ComponentGroupRef Id="AppFiles" />
      <ComponentRef Id="StartMenuShortcut" />
    </Feature>
  </Package>
  <Fragment>
    <ComponentGroup Id="AppFiles" Directory="INSTALLFOLDER">
      <Files Include="$(var.SourceDir)\**" />
    </ComponentGroup>
  </Fragment>
  <Fragment>
    <Component Id="StartMenuShortcut" Directory="AppStartMenuFolder" Guid="*">
      <Shortcut Id="AppShortcut" Name="PiraChest" Description="PiraChest"
                Target="[INSTALLFOLDER]PiraChest.exe" WorkingDirectory="INSTALLFOLDER" Icon="AppIcon" />
      <RemoveFolder Id="RemoveAppStartMenuFolder" On="uninstall" />
      <RegistryValue Root="HKCU" Key="Software\PiraChest" Name="installed"
                     Type="integer" Value="1" KeyPath="yes" />
    </Component>
  </Fragment>
</Wix>
"""


def _version_text() -> str:
    try:
        return (ROOT / "VERSION").read_text(encoding="utf-8").strip()
    except OSError:
        return "0.1.0"


def _msi_version(raw: str) -> str:
    match = re.match(r"^(\d+)\.(\d+)\.(\d+)", raw)
    if match is None:
        raise ValueError(f"VERSION must start with a numeric x.y.z version, got {raw!r}")
    parts = [int(part) for part in match.groups()]
    if parts[0] > 255 or parts[1] > 255 or parts[2] > 65535:
        raise ValueError(f"VERSION is out of range for an MSI ProductVersion: {raw!r}")
    return ".".join(str(part) for part in parts)


def build_msi() -> int:
    """Build a per-user MSI from the pruned onedir output using a transient WXS."""
    exe = DIST / "PiraChest.exe"
    if not exe.is_file():
        print(f"[error] build output missing: {exe}", file=sys.stderr)
        return 1
    wix = shutil.which("wix")
    if wix is None:
        print("[warn] WiX Toolset not found; skipping MSI.", file=sys.stderr)
        print("       Install it with: dotnet tool install --global wix --version 5.0.2", file=sys.stderr)
        return 0
    version_text = _version_text()
    try:
        msi_version = _msi_version(version_text)
    except ValueError as exc:
        print(f"[error] {exc}", file=sys.stderr)
        return 1

    WIX_DIR.mkdir(parents=True, exist_ok=True)
    wxs = WIX_DIR / "PiraChest.wxs"
    wxs.write_text(_MSI_TEMPLATE.format(upgrade=WIX_UPGRADE_CODE), encoding="utf-8")
    output = ROOT / "dist" / f"PiraChest-v{version_text}-Windows-x64.msi"
    if output.exists():
        output.unlink()

    print(f"[build] Building MSI {output.name}...")
    result = subprocess.run(
        [wix, "build", "-arch", "x64", "-d", f"Version={msi_version}",
         "-d", f"SourceDir={DIST}", "-o", str(output),
         "-pdb", str(WIX_DIR / "PiraChest.wixpdb"), str(wxs)],
        cwd=ROOT,
    )
    if result.returncode != 0:
        print("[error] WiX MSI build failed.", file=sys.stderr)
        return result.returncode or 1
    if not output.is_file():
        print("[error] WiX reported success but no MSI was produced.", file=sys.stderr)
        return 1
    print(f"[build] MSI: {output}")
    return 0

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
    parser = argparse.ArgumentParser(description="Prune the PyInstaller onedir build and optionally build an MSI.")
    parser.add_argument("--msi", action="store_true", help="build a per-user WiX MSI in dist/")
    args = parser.parse_args()

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
    return build_msi() if args.msi else 0


if __name__ == "__main__":
    raise SystemExit(main())
