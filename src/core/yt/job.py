from __future__ import annotations

import logging
import os

from PySide6.QtCore import QObject, QRunnable, QThreadPool, Signal

from src.core.config import settings
from src.core.yt import provider

logger = logging.getLogger(__name__)


def _dest_dir() -> str:
    return getattr(settings, "download_dir_youtube", os.path.join(settings.download_dir, "youtube"))


class _Signals(QObject):
    progress = Signal(str, int, int, float)
    finished = Signal(str, str)
    failed = Signal(str, str)


class _Job(QRunnable):
    def __init__(self, item_id: str, request: dict, signals: _Signals):
        super().__init__()
        self.cancelled = False
        self._item_id = item_id
        self._request = request
        self._signals = signals

    def run(self) -> None:
        try:
            r = self._request
            path = provider.download(
                r["info"], os.path.join(_dest_dir(), r["subdir"]) if r["subdir"] else _dest_dir(), r["mode"], r["format_id"],
                max_height=r["max_height"], subtitle=r["subtitle"], clip=r["clip"],
                on_progress=lambda n, total, speed: self._signals.progress.emit(self._item_id, n, total, speed / 1024.0),
                is_cancelled=lambda: self.cancelled,
            )
        except Exception as exc:
            if self.cancelled:
                return
            if not isinstance(exc, provider.YtError):
                logger.exception("YouTube download failed")
            self._signals.failed.emit(self._item_id, str(exc))
            return
        if not self.cancelled:
            self._signals.finished.emit(self._item_id, path)


class YtDownloadBridge(QObject):
    def __init__(self, download_manager, parent=None):
        super().__init__(parent)
        self._manager = download_manager
        self._pool = QThreadPool(self)
        self._pool.setMaxThreadCount(2)
        self._signals = _Signals()
        self._signals.progress.connect(self._on_progress)
        self._signals.finished.connect(self._on_finished)
        self._signals.failed.connect(self._on_failed)
        self._jobs: dict[str, _Job] = {}
        self._params: dict[str, dict] = {}

    def download(self, info: dict, mode: str, format_id: str = "", *, max_height: int = 0, subtitle: str = "", clip: tuple | None = None, subdir: str = "") -> str:
        item_id = self._manager.add_external(game_name=info["title"], source="YouTube", category="youtube")
        self._params[item_id] = {"info": info, "mode": mode, "format_id": format_id, "max_height": max_height, "subtitle": subtitle, "clip": clip, "subdir": provider.safe_filename(subdir) if subdir else ""}
        self._manager.register_external_cancel(item_id, lambda: self._cancel(item_id), retry=lambda: self._start(item_id))
        self._start(item_id)
        return item_id

    def _start(self, item_id: str) -> None:
        job = _Job(item_id, self._params[item_id], self._signals)
        self._jobs[item_id] = job
        self._pool.start(job)

    def _cancel(self, item_id: str) -> None:
        job = self._jobs.pop(item_id, None)
        if job:
            job.cancelled = True

    def _on_progress(self, item_id: str, downloaded: int, total: int, speed_kbps: float) -> None:
        self._manager.update_external(item_id, downloaded_bytes=downloaded, total_bytes=total, speed_down_kbps=speed_kbps)

    def _on_finished(self, item_id: str, path: str) -> None:
        self._jobs.pop(item_id, None)
        self._params.pop(item_id, None)
        self._manager.complete_external(item_id, path)

    def _on_failed(self, item_id: str, error: str) -> None:
        self._jobs.pop(item_id, None)
        self._manager.fail_external(item_id, error)

    def shutdown(self) -> None:
        for job in self._jobs.values():
            job.cancelled = True
        self._pool.waitForDone(3000)
