import logging
import os
import sys
import gc
import multiprocessing
if sys.platform == 'win32':
    os.environ['QT_MEDIA_BACKEND'] = 'ffmpeg'
os.environ['PYTHONOPTIMIZE'] = '2'
os.environ['PYTHONHASHSEED'] = '0'
from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QApplication
logging.basicConfig(level=logging.WARNING, format='%(asctime)s [%(levelname)s] %(name)s — %(message)s')
logger = logging.getLogger(__name__)
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

def memory_optimize():
    gc.set_threshold(700, 10, 10)

def main() -> None:
    multiprocessing.freeze_support()
    QApplication.setHighDpiScaleFactorRoundingPolicy(Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)
    memory_optimize()
    from src.gui.main_window import create_application, MainWindow
    app = create_application(sys.argv)
    app.setApplicationName('PiraChest')
    app.setStyleSheet('')
    window = MainWindow()
    window.show()
    logger.info('GUI application started')
    sys.exit(app.exec())

def memory_purge():
    gc.collect()
    try:
        import ctypes
        ctypes.windll.kernel32.SetProcessWorkingSetSize(-1, -1)
    except:
        pass
if __name__ == '__main__':
    try:
        main()
    except Exception as e:
        logger.error(f'Fatal error: {e}')
        sys.exit(1)
    finally:
        memory_purge()