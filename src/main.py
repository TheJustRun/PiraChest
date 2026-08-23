import logging
import os
import sys
import gc
import multiprocessing

# Frozen/windowed builds start with sys.stdout/stderr = None (no console
# attached). logging.basicConfig() below would otherwise bind a
# StreamHandler to None and crash on every single log call. Route to
# devnull until/unless a real console is allocated (see
# src/gui/main_window.py::_set_debug_console_visible), which swaps these
# out for real console-backed streams.
if getattr(sys, 'frozen', False):
    if sys.stdout is None or sys.stderr is None:
        _devnull = open(os.devnull, 'w')
        sys.stdout = sys.stdout or _devnull
        sys.stderr = sys.stderr or _devnull

if sys.platform == 'win32':
    os.environ['QT_MEDIA_BACKEND'] = 'ffmpeg'
elif sys.platform.startswith('linux'):
    os.environ.setdefault('__GL_SYNC_TO_VBLANK', '1')
    os.environ.setdefault('vblank_mode', '1')

logging.basicConfig(level=logging.WARNING, format='%(asctime)s [%(levelname)s] %(name)s — %(message)s')
logger = logging.getLogger(__name__)

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)


def _cleanup_stale_temp() -> None:
    if not getattr(sys, 'frozen', False):
        return
    import tempfile
    import shutil

    temp_dir = os.path.join(os.environ.get('TEMP', tempfile.gettempdir()), 'PiraChest')
    try:
        with os.scandir(temp_dir) as it:
            for entry in it:
                if entry.is_dir():
                    shutil.rmtree(entry.path, ignore_errors=True)
    except FileNotFoundError:
        pass
    except Exception:
        pass


def memory_optimize() -> None:
    gc.disable()


def enable_gc() -> None:
    gc.set_threshold(20000, 50, 50)
    gc.enable()


def memory_purge() -> None:
    gc.collect()
    if sys.platform == 'win32':
        try:
            import ctypes
            ctypes.windll.kernel32.SetProcessWorkingSetSize(-1, -1, -1)
        except Exception:
            pass


def main() -> None:
    multiprocessing.freeze_support()
    memory_optimize()
    _cleanup_stale_temp()

    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QApplication

    QApplication.setHighDpiScaleFactorRoundingPolicy(Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)

    from src.gui.main_window import create_application, MainWindow
    app = create_application(sys.argv)
    app.setApplicationName('PiraChest')

    window = MainWindow()
    window.show()

    enable_gc()
    logger.info('GUI application started')

    from PySide6.QtCore import QTimer
    idle_timer = QTimer(app)
    idle_timer.setInterval(5 * 60 * 1000)
    idle_timer.timeout.connect(memory_purge)
    idle_timer.start()

    sys.exit(app.exec())


if __name__ == '__main__':
    try:
        main()
    except Exception:
        logger.exception('Fatal error')
        sys.exit(1)
    finally:
        memory_purge()
