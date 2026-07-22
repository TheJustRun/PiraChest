from __future__ import annotations

import logging

from PyQt6.QtCore import QObject, pyqtSignal

from . import database as db

logger = logging.getLogger(__name__)

BATCH_SIZE = 5_000

class SyncWorker(QObject):
    progress = pyqtSignal(int, int, str)
    finished = pyqtSignal(int)
    error = pyqtSignal(str)

    def run(self) -> int:
        try:
            db.init_db()
            db.clear_roms()

            logger.info("Starting sync from GitHub markdown index…")
            self.progress.emit(0, 0, "Downloading index from GitHub…")
            total = self._sync_from_markdown()

            self.progress.emit(total, total, "Sync complete")
            self.finished.emit(total)
            return total

        except Exception as exc:
            logger.error("Sync failed: %s", exc, exc_info=True)
            self.error.emit(str(exc))
            return 0

    def _on_sync_progress(self, processed: int) -> None:
        self.progress.emit(processed, 0, f"Indexing… {processed:,} ROMs so far")

    def _sync_from_markdown(self) -> int:
        from .indexer import sync_index

        try:
            sync_index(
                batch_size=BATCH_SIZE,
                on_progress=self._on_sync_progress,
            )
        except Exception as exc:
            logger.error("Markdown sync failed: %s", exc, exc_info=True)
            raise RuntimeError(f"Index sync failed: {exc}") from exc

        return db.count_roms()

def main() -> None:
    worker = SyncWorker()

    def on_progress(cur: int, tot: int, msg: str = "") -> None:
        if tot > 0:
            pct = int(cur / tot * 100)
            print(f"\r[Sync {pct:3d}%] {msg}", end="", flush=True)
        else:
            print(f"\r[Sync] {msg}", end="", flush=True)

    def on_finished(total: int) -> None:
        print(f"\nSync complete. {total:,} ROMs in database.")

    def on_error(msg: str) -> None:
        print(f"\nSync failed: {msg}")

    worker.progress.connect(on_progress)
    worker.finished.connect(on_finished)
    worker.error.connect(on_error)

    try:
        worker.run()
    except Exception as exc:
        worker.error.emit(str(exc))
        raise

if __name__ == "__main__":
    main()