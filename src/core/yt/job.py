from __future__ import annotations

import logging
import os

from PySide6.QtCore import QObject, QRunnable, QThreadPool, Signal

from src.core.yt import provider
from src.core.config import settings

logger = logging.getLogger(__name__)


def _dest_dir() -> str:
    return getattr(settings, "download_dir_youtube", os.path.join(settings.download_dir, "youtube"))


class _JobSignals(QObject):
    progress = Signal(str, int, int, float)
    finished = Signal(str, str)
    failed = Signal(str, str)


class YtDownloadJob(QRunnable):
    def __init__(self, item_id: str, url: str, mode: str, format_id: str, title: str, signals: _JobSignals):
        super().__init__()
        self._item_id = item_id
        self._url = url
        self._mode = mode
        self._format_id = format_id
        self._title = title
        self._signals = signals
        self._cancelled = False
        self.setAutoDelete(True)

    def cancel(self) -> None:
        self._cancelled = True

    def _is_cancelled(self) -> bool:
        return self._cancelled

    def _on_progress(self, downloaded: int, total: int, speed_kbps: float) -> None:
        self._signals.progress.emit(self._item_id, downloaded, total, speed_kbps)

    def run(self) -> None:
        try:
            final_path = provider.download(
                self._url,
                _dest_dir(),
                self._mode,
                self._format_id,
                self._title,
                on_progress=self._on_progress,
                is_cancelled=self._is_cancelled,
            )
        except provider.YtError as exc:
            if self._cancelled:
                return
            logger.error("YouTube download failed: %s", exc)
            self._signals.failed.emit(self._item_id, str(exc))
            return
        except Exception as exc:
            if self._cancelled:
                return
            logger.exception("YouTube download failed")
            self._signals.failed.emit(self._item_id, str(exc))
            return
        if self._cancelled:
            return
        self._signals.finished.emit(self._item_id, final_path)


class YtDownloadBridge(QObject):
    def __init__(self, download_manager, parent=None):
        super().__init__(parent)
        self._download_manager = download_manager
        self._pool = QThreadPool(self)
        self._pool.setMaxThreadCount(2)
        self._signals = _JobSignals()
        self._signals.progress.connect(self._on_progress)
        self._signals.finished.connect(self._on_finished)
        self._signals.failed.connect(self._on_failed)
        self._jobs: dict[str, YtDownloadJob] = {}

    def download(self, url: str, mode: str, format_id: str, title: str) -> str:
        item_id = self._download_manager.add_external(
            game_name=title, console="", source="YouTube", category="youtube",
        )
        job = YtDownloadJob(item_id, url, mode, format_id, title, self._signals)
        self._jobs[item_id] = job
        self._download_manager.register_external_cancel(item_id, job.cancel)
        self._pool.start(job)
        return item_id

    def _on_progress(self, item_id: str, downloaded: int, total: int, speed_kbps: float) -> None:
        self._download_manager.update_external(
            item_id, downloaded_bytes=downloaded, total_bytes=total, speed_down_kbps=speed_kbps,
        )

    def _on_finished(self, item_id: str, final_path: str) -> None:
        self._jobs.pop(item_id, None)
        self._download_manager.complete_external(item_id, final_path)

    def _on_failed(self, item_id: str, error: str) -> None:
        self._jobs.pop(item_id, None)
        self._download_manager.fail_external(item_id, error)

    def shutdown(self) -> None:
        for job in list(self._jobs.values()):
            job.cancel()
        self._pool.waitForDone(3000)
