import atexit
import logging
import multiprocessing
import os
import sys
import time

_FROZEN = getattr(sys, 'frozen', False)

if _FROZEN and (sys.stdout is None or sys.stderr is None):
    _devnull = open(os.devnull, 'w')
    sys.stdout = sys.stdout or _devnull
    sys.stderr = sys.stderr or _devnull

os.environ.setdefault('QT_MEDIA_BACKEND', 'ffmpeg')

logger = logging.getLogger(__name__)

_PROJECT_ROOT = (getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(sys.executable)))
                 if _FROZEN else os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from src.core.import_guard import block_optional_imports

block_optional_imports()


def _cleanup_stale_temp() -> None:
    import shutil
    import tempfile
    temp_dir = os.path.join(os.environ.get('TEMP', tempfile.gettempdir()), 'PiraChest')
    now = time.time()
    try:
        with os.scandir(temp_dir) as it:
            for entry in it:
                try:
                    if entry.is_dir() and (now - entry.stat().st_mtime) > 7 * 86400:
                        shutil.rmtree(entry.path, ignore_errors=True)
                except OSError:
                    continue
    except OSError:
        pass


def _log_startup_environment() -> None:
    import platform
    from src.core.config import paths
    try:
        with open(os.path.join(_PROJECT_ROOT, 'VERSION'), encoding='utf-8') as fh:
            version = fh.read().strip()
    except OSError:
        version = 'unknown'
    try:
        import PySide6
        pyside = PySide6.__version__
    except Exception:
        pyside = 'unknown'
    try:
        logger.info(
            'Environment: version=%s python=%s platform=%s release=%s machine=%s frozen=%s pid=%s cpus=%s pyside=%s',
            version, sys.version.split()[0], platform.system(), platform.release(), platform.machine(),
            _FROZEN, os.getpid(), os.cpu_count(), pyside)
        logger.info('Paths: app_data=%s cwd=%s qt_platform=%s media_backend=%s',
                    paths.app_data_dir, os.getcwd(), os.environ.get('QT_QPA_PLATFORM', ''),
                    os.environ.get('QT_MEDIA_BACKEND', ''))
    except Exception:
        logger.exception('Failed to log startup environment')


def main() -> int:
    multiprocessing.freeze_support()
    from src.core.config import settings
    from src.gui.main_window import (
        configure_logging,
        install_exception_hooks,
        install_qt_message_handler,
    )
    configure_logging(settings.log_to_file)
    install_exception_hooks()
    install_qt_message_handler()
    logger.info('Starting PiraChest (python %s, frozen=%s, file logging=%s)',
                sys.version.split()[0], _FROZEN, settings.log_to_file)
    _log_startup_environment()

    from PySide6.QtCore import Qt, QTimer
    from PySide6.QtWidgets import QApplication
    QApplication.setAttribute(Qt.ApplicationAttribute.AA_ShareOpenGLContexts)
    QApplication.setHighDpiScaleFactorRoundingPolicy(Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)

    from src.gui.main_window import MainWindow, create_application
    app = create_application(sys.argv)
    app.setApplicationName('PiraChest')

    from src.core.config import (SingleInstance, ensure_associations_repaired, extract_cli_targets)
    initial_targets = extract_cli_targets(sys.argv[1:])

    window = None
    pending_args: list = []

    def _deliver_args(args) -> None:
        if window is not None:
            window.handle_external_args(args)
        else:
            pending_args.append(args)

    instance = SingleInstance()
    if not instance.try_acquire(sys.argv[1:], _deliver_args):
        sys.exit(0)

    window = MainWindow(initial_targets=initial_targets)
    for args in pending_args:
        window.handle_external_args(args)
    pending_args.clear()

    from src.core.theme import go_live_theme
    go_live_theme()

    def _deferred_temp_cleanup() -> None:
        from src.core.updater import cleanup_update_staging
        from src.core.worker import Priority, submit
        submit(_cleanup_stale_temp, priority=Priority.PREFETCH)
        submit(cleanup_update_staging, priority=Priority.PREFETCH)

    QTimer.singleShot(3000, _deferred_temp_cleanup)

    if settings.associations_enabled:
        def _deferred_assoc():
            try:
                ensure_associations_repaired()
            except Exception:
                logger.warning('Association repair failed', exc_info=True)
        QTimer.singleShot(2000, _deferred_assoc)

    logger.info('GUI application started')
    return app.exec()


if __name__ == '__main__':
    exit_code = 1
    try:
        exit_code = main() or 0
    except SystemExit as exc:
        if isinstance(exc.code, int):
            exit_code = exc.code
        elif exc.code is None:
            exit_code = 0
    except Exception:
        logger.exception('Fatal error')
    finally:
        try:
            sys.stdout.flush()
            sys.stderr.flush()
        except Exception:
            pass
        try:
            atexit._run_exitfuncs()
        except Exception:
            pass
        try:
            from src.gui.main_window import shutdown_logging
            shutdown_logging()
        except Exception:
            pass
        os._exit(exit_code)
