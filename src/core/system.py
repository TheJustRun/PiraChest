from __future__ import annotations

import os
import platform
import shutil
import subprocess
import sys

WINDOWS = sys.platform == "win32"
MACOS = sys.platform == "darwin"
LINUX = not WINDOWS and not MACOS

_ARCH_ALIASES = {
    "amd64": "x64",
    "x86_64": "x64",
    "x64": "x64",
    "i386": "x86",
    "i486": "x86",
    "i586": "x86",
    "i686": "x86",
    "x86": "x86",
    "arm64": "arm64",
    "aarch64": "arm64",
    "armv8": "arm64",
    "armv8l": "arm64",
}

_OS_TOKENS = {
    "windows": ("windows", "win32", "win"),
    "macos": ("macos", "mac os", "osx", "darwin", "mac"),
    "linux": ("linux",),
}

_ARCH_TOKENS = {
    "x64": ("x86_64", "x86-64", "amd64", "x64"),
    "arm64": ("aarch64", "arm64", "armv8"),
    "x86": ("i386", "i686", "x86"),
}


def arch() -> str:
    raw = (platform.machine() or os.environ.get("PROCESSOR_ARCHITECTURE") or "").strip().lower()
    return _ARCH_ALIASES.get(raw, raw or "x64")


def os_key() -> str:
    if WINDOWS:
        return "windows"
    if MACOS:
        return "macos"
    return "linux"


def is_64bit() -> bool:
    return arch() in ("x64", "arm64")


def name_matches_platform(name: str, target_os: str | None = None, target_arch: str | None = None) -> bool:
    low = (name or "").lower()
    tokens = _OS_TOKENS.get(target_os or os_key(), ())
    if tokens and not any(token in low for token in tokens):
        return False
    arch_tokens = _ARCH_TOKENS.get(target_arch or arch(), ())
    return not arch_tokens or any(token in low for token in arch_tokens)


_LINUX_OPENERS = ("gio", "xdg-open", "kde-open5", "gnome-open")


def _launch(command: list[str]) -> bool:
    try:
        proc = subprocess.Popen(
            command,
            env=clean_env(),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            **detached_kwargs(),
        )
    except OSError:
        return False
    try:
        code = proc.wait(timeout=2)
    except subprocess.TimeoutExpired:
        return True
    return code == 0


def _open_linux(target: str) -> bool:
    for name in _LINUX_OPENERS:
        exe = shutil.which(name)
        if not exe:
            continue
        command = [exe, "open", target] if name == "gio" else [exe, target]
        if _launch(command):
            return True
    return False


def open_path(path: str) -> None:
    path = os.path.abspath(path)
    if WINDOWS:
        os.startfile(path)
    elif MACOS:
        subprocess.Popen(["open", path])
    else:
        _open_linux(path)


def open_external(target: str) -> bool:
    target = str(target or "").strip()
    if not target:
        return False
    try:
        if WINDOWS:
            os.startfile(target)
        elif MACOS:
            subprocess.Popen(["open", target])
        else:
            return _open_linux(target)
        return True
    except OSError:
        return False


def reveal_path(path: str, select: bool = False) -> None:
    path = os.path.abspath(path)
    if WINDOWS:
        if select:
            subprocess.Popen(["explorer", "/select,", os.path.normpath(path)])
        else:
            os.startfile(path if os.path.isdir(path) else os.path.dirname(path))
    elif MACOS:
        subprocess.Popen(["open", "-R", path] if select else ["open", path])
    else:
        target = os.path.dirname(path) if select and os.path.isfile(path) else path
        _open_linux(target)


def detached_kwargs() -> dict:
    if WINDOWS:
        return {"creationflags": 0x00000008 | 0x00000200, "close_fds": True}
    return {"start_new_session": True, "close_fds": True}


def clean_env() -> dict:
    env = dict(os.environ)
    for name in ("PYTHONHOME", "PYTHONPATH", "PYTHONSTARTUP", "_MEIPASS",
                 "QT_PLUGIN_PATH", "QT_QPA_PLATFORM_PLUGIN_PATH"):
        env.pop(name, None)
    if MACOS:
        for name in ("DYLD_LIBRARY_PATH", "DYLD_FRAMEWORK_PATH", "DYLD_INSERT_LIBRARIES"):
            env.pop(name, None)
    elif LINUX:
        env.pop("LD_LIBRARY_PATH", None)
    return env


def _autostart_paths(name: str) -> tuple[str, str]:
    slug = "".join(ch if ch.isalnum() else "-" for ch in name.lower()).strip("-") or "pirachest"
    if MACOS:
        label = f"com.justrun.{slug}"
        base = os.path.expanduser("~/Library/LaunchAgents")
        return label, os.path.join(base, f"{label}.plist")
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return slug, os.path.join(base, "autostart", f"{slug}.desktop")


def _desktop_quote(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _xml_escape(value: str) -> str:
    return value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def startup_enabled(name: str) -> bool:
    if WINDOWS:
        return False
    _label, path = _autostart_paths(name)
    return os.path.isfile(path)


def set_startup(name: str, command: list[str], enabled: bool) -> bool:
    if WINDOWS:
        return False
    label, path = _autostart_paths(name)
    try:
        if not enabled:
            if os.path.isfile(path):
                os.remove(path)
            return True
        os.makedirs(os.path.dirname(path), exist_ok=True)
        if MACOS:
            args = "".join(f"<string>{_xml_escape(part)}</string>" for part in command)
            payload = (
                '<?xml version="1.0" encoding="UTF-8"?>\n'
                '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
                '"http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
                '<plist version="1.0"><dict>'
                f"<key>Label</key><string>{label}</string>"
                f"<key>ProgramArguments</key><array>{args}</array>"
                "<key>RunAtLoad</key><true/>"
                "<key>ProcessType</key><string>Interactive</string>"
                "</dict></plist>\n"
            )
        else:
            exec_line = " ".join(_desktop_quote(part) for part in command)
            payload = (
                "[Desktop Entry]\n"
                "Type=Application\n"
                f"Name={name}\n"
                f"Exec={exec_line}\n"
                "Terminal=false\n"
                "X-GNOME-Autostart-enabled=true\n"
            )
        with open(path, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(payload)
        return True
    except OSError:
        return False


def release_input_state() -> None:
    if WINDOWS:
        try:
            import ctypes
            ctypes.windll.user32.ReleaseCapture()
        except Exception:
            pass
    try:
        from PySide6.QtWidgets import QApplication, QWidget
    except Exception:
        return
    app = QApplication.instance()
    if app is None:
        return
    try:
        if QWidget.mouseGrabber() is not None:
            QWidget.mouseGrabber().releaseMouse()
        if QWidget.keyboardGrabber() is not None:
            QWidget.keyboardGrabber().releaseKeyboard()
    except Exception:
        pass
    for _ in range(8):
        if app.overrideCursor() is None:
            break
        app.restoreOverrideCursor()
