
import logging
import os
import sys

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QApplication

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
)
logger = logging.getLogger(__name__)

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

def main() -> None:
    QApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.Round
    )

    from src.gui.main_window import create_application, MainWindow

    app = create_application(sys.argv)

    window = MainWindow()

    logger.info("GUI application started")
    sys.exit(app.exec())

if __name__ == "__main__":
    main()