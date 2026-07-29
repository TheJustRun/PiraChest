# -*- mode: python ; coding: utf-8 -*-
from PyInstaller.utils.hooks import collect_data_files

datas = [('src\\gui', 'src\\gui'), ('src\\core', 'src\\core')]
datas += collect_data_files('qfluentwidgets')


a = Analysis(
    ['src\\main.py'],
    pathex=[],
    binaries=[],
    datas=datas,
    hiddenimports=['PyQt6.QtCore', 'PyQt6.QtGui', 'PyQt6.QtWidgets', 'PyQt6.QtSvg', 'PyQt6.QtMultimedia', 'PyQt6.QtMultimediaWidgets', 'requests', 'qfluentwidgets'],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['PyQt5', 'PySide2', 'PySide6', 'shiboken2', 'shiboken6', 'tkinter', 'matplotlib', 'numpy', 'scipy', 'setuptools', 'distutils', 'pkg_resources', 'test', 'unittest', 'pydoc', 'doctest', 'PyQt6.QtQml', 'PyQt6.QtQuick', 'PyQt6.QtQuickWidgets', 'PyQt6.QtWebEngineWidgets', 'PyQt6.QtWebEngineCore', 'PyQt6.QtWebEngineQuick', 'PyQt6.QtDesigner', 'PyQt6.QtBluetooth', 'PyQt6.QtNetwork', 'PyQt6.QtNetworkAuth', 'PyQt6.QtNfc', 'PyQt6.QtPositioning', 'PyQt6.QtLocation', 'PyQt6.QtSensors', 'PyQt6.QtSerialPort', 'PyQt6.QtSerialBus', 'PyQt6.QtTest', 'PyQt6.QtOpenGL', 'PyQt6.QtOpenGLWidgets', 'PyQt6.QtPrintSupport', 'PyQt6.QtSql', 'PyQt6.QtHelp', 'PyQt6.QtUiTools', 'PyQt6.QtConcurrent', 'PyQt6.QtDBus', 'PyQt6.QtX11Extras', 'PyQt6.QtWinExtras', 'PyQt6.QtMacExtras', 'PyQt6.QtPdf', 'PyQt6.QtPdfWidgets', 'PyQt6.QtCharts', 'PyQt6.QtDataVisualization', 'PyQt6.QtRemoteObjects', 'PyQt6.QtSpatialAudio', 'PyQt6.QtStateMachine', 'PyQt6.QtTextToSpeech', 'PyQt6.QtWebChannel', 'PyQt6.QtWebSockets', 'PyQt6.Qt3DCore', 'PyQt6.Qt3DRender', 'PyQt6.Qt3DInput', 'PyQt6.Qt3DLogic', 'PyQt6.Qt3DAnimation', 'PyQt6.Qt3DExtras'],
    noarchive=False,
    optimize=2,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [('O', None, 'OPTION'), ('O', None, 'OPTION')],
    exclude_binaries=True,
    name='PiraChest',
    debug=False,
    bootloader_ignore_signals=False,
    strip=True,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=['src\\gui\\icon.ico'],
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=True,
    upx=True,
    upx_exclude=[],
    name='PiraChest',
)
