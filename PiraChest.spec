# -*- mode: python ; coding: utf-8 -*-
from PyInstaller.utils.hooks import collect_data_files

datas = [('src\\gui', 'src\\gui'), ('src\\core', 'src\\core')]
datas += collect_data_files('qfluentwidgets')


a = Analysis(
    ['src\\main.py'],
    pathex=[],
    binaries=[],
    datas=datas,
    hiddenimports=['PySide6.QtCore', 'PySide6.QtGui', 'PySide6.QtWidgets', 'PySide6.QtSvg', 'PySide6.QtNetwork', 'PySide6.QtMultimedia', 'PySide6.QtMultimediaWidgets', 'libtorrent', 'orjson', 'requests', 'qfluentwidgets', 'qframelesswindow'],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['PyQt5', 'PyQt6', 'PySide2', 'shiboken2', 'tkinter', 'matplotlib', 'numpy', 'scipy', 'setuptools', 'pkg_resources', 'test', 'unittest', 'pydoc', 'doctest', 'av', 'lxml', 'curl_cffi', 'PySide6.QtQml', 'PySide6.QtQuick', 'PySide6.QtQuickWidgets', 'PySide6.QtQuickControls2', 'PySide6.QtQuick3D', 'PySide6.QtWebEngineWidgets', 'PySide6.QtWebEngineCore', 'PySide6.QtWebEngineQuick', 'PySide6.QtDesigner', 'PySide6.QtBluetooth', 'PySide6.QtNetworkAuth', 'PySide6.QtNfc', 'PySide6.QtPositioning', 'PySide6.QtLocation', 'PySide6.QtSensors', 'PySide6.QtSerialPort', 'PySide6.QtSerialBus', 'PySide6.QtTest', 'PySide6.QtOpenGL', 'PySide6.QtOpenGLWidgets', 'PySide6.QtPrintSupport', 'PySide6.QtSql', 'PySide6.QtHelp', 'PySide6.QtUiTools', 'PySide6.QtConcurrent', 'PySide6.QtDBus', 'PySide6.QtPdf', 'PySide6.QtPdfWidgets', 'PySide6.QtCharts', 'PySide6.QtDataVisualization', 'PySide6.QtRemoteObjects', 'PySide6.QtSpatialAudio', 'PySide6.QtStateMachine', 'PySide6.QtTextToSpeech', 'PySide6.QtWebChannel', 'PySide6.QtWebSockets', 'PySide6.Qt3DCore', 'PySide6.Qt3DRender', 'PySide6.Qt3DInput', 'PySide6.Qt3DLogic', 'PySide6.Qt3DAnimation', 'PySide6.Qt3DExtras'],
    noarchive=False,
    optimize=2,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [('O', None, 'OPTION'), ('O', None, 'OPTION')],
    name='PiraChest',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=['src\\gui\\photos\\logo.ico'],
)
