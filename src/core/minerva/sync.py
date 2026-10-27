from __future__ import annotations

import logging

from PySide6.QtCore import QObject, Signal

from src.core.minerva import database as db
from src.core.minerva.indexer import sync_index

logger = logging.getLogger(__name__)

BATCH_SIZE = 5_000


class SyncWorker(QObject):
    progress = Signal(int, int, str)
    finished = Signal(int)
    error = Signal(str)

    def run(self) -> int:
        try:
            self.progress.emit(0, 0, "Downloading index from GitHub…")
            total = sync_index(batch_size=BATCH_SIZE, on_progress=self._on_sync_progress)
        except Exception as exc:
            logger.error("Sync failed: %s", exc, exc_info=True)
            self.error.emit(str(exc))
            return 0
        finally:
            db.close_connection()

        self.progress.emit(total, total, "Sync complete")
        self.finished.emit(total)
        return total

    def _on_sync_progress(self, processed: int) -> None:
        self.progress.emit(processed, 0, f"Indexing… {processed:,} ROMs so far")


def main() -> None:
    worker = SyncWorker()
    worker.progress.connect(lambda _cur, _tot, msg: print(f"\r[Sync] {msg}", end="", flush=True))
    worker.finished.connect(lambda total: print(f"\nSync complete. {total:,} ROMs in database."))
    worker.error.connect(lambda msg: print(f"\nSync failed: {msg}"))
    worker.run()


if __name__ == "__main__":
    main()
