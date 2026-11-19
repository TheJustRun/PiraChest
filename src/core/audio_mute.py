import atexit
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from contextlib import suppress

from PySide6.QtCore import QObject, Signal

from src.core.system import clean_env

_MUTE_RE = re.compile(r'^\s*Mute:\s*(yes|no)\s*$', re.M)
_MIN_QUERY_GAP = 0.25


class AppMuteWatcher(QObject):
    muted_changed = Signal(bool)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._thread = None
        self._proc = None
        self._stop = threading.Event()

    def start(self) -> None:
        if self._thread is not None or not sys.platform.startswith('linux'):
            return
        exe = shutil.which('pactl')
        if not exe:
            return
        self._thread = threading.Thread(target=self._run, args=(exe,), name='pirachest-app-mute',
                                        daemon=True)
        self._thread.start()
        atexit.register(self.stop)

    def stop(self) -> None:
        self._stop.set()
        proc, self._proc = self._proc, None
        if proc is not None and proc.poll() is None:
            with suppress(Exception):
                proc.terminate()

    def _run(self, exe: str) -> None:
        env = clean_env()
        try:
            proc = subprocess.Popen(
                [exe, 'subscribe'], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL, env=env, text=True, encoding='utf-8', errors='replace',
                bufsize=1)
        except (OSError, ValueError):
            return
        self._proc = proc
        try:
            self._publish(self._query(exe, env))
            last = 0.0
            for line in proc.stdout:
                if self._stop.is_set():
                    break
                if ' on sink-input ' not in line:
                    continue
                now = time.monotonic()
                if now - last < _MIN_QUERY_GAP:
                    continue
                last = now
                self._publish(self._query(exe, env))
        finally:
            if proc.poll() is None:
                with suppress(Exception):
                    proc.terminate()
            if self._proc is proc:
                self._proc = None

    def _publish(self, state) -> None:
        if state is not None:
            self.muted_changed.emit(bool(state))

    @staticmethod
    def _query(exe: str, env: dict):
        try:
            out = subprocess.run(
                [exe, 'list', 'sink-inputs'], capture_output=True, text=True, encoding='utf-8',
                errors='replace', env=env, timeout=5)
        except (OSError, ValueError, subprocess.SubprocessError):
            return None
        if out.returncode != 0:
            return None
        pid = str(os.getpid())
        states = []
        for block in out.stdout.split('Sink Input #')[1:]:
            if f'application.process.id = "{pid}"' not in block and 'pirachest' not in block.lower():
                continue
            match = _MUTE_RE.search(block)
            if match:
                states.append(match.group(1) == 'yes')
        return any(states) if states else None
