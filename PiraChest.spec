# -*- mode: python ; coding: utf-8 -*-
from PyInstaller.utils.hooks import collect_data_files
from PyInstaller.utils.hooks import collect_submodules

datas = [('src', 'src')]
hiddenimports = ['PyQt6.QtCore', 'PyQt6.QtGui', 'PyQt6.QtWidgets', 'PyQt6.QtSvg', 'PyQt6.QtXml', 'requests', 'orjson', 'qfluentwidgets']
datas += collect_data_files('qfluentwidgets')
hiddenimports += collect_submodules('PyQt6.QtCore')
hiddenimports += collect_submodules('PyQt6.QtGui')
hiddenimports += collect_submodules('PyQt6.QtWidgets')
hiddenimports += collect_submodules('PyQt6.QtSvg')
hiddenimports += collect_submodules('PyQt6.QtXml')
hiddenimports += collect_submodules('qfluentwidgets')


a = Analysis(
    ['src\\main.py'],
    pathex=[],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['PyQt5', 'PySide2', 'PySide6', 'shiboken2', 'shiboken6', 'tkinter', 'matplotlib', 'numpy', 'numpy.core', 'numpy.lib', 'numpy.random', 'numpy.fft', 'numpy.linalg', 'numpy.polynomial', 'scipy', 'scipy.special', 'scipy.spatial', 'scipy.stats', 'scipy.sparse', 'scipy.linalg', 'scipy.optimize', 'scipy.signal', 'scipy.fft', 'scipy.integrate', 'scipy.interpolate', 'scipy.io', 'scipy.ndimage', 'scipy.odr', 'scipy.fftpack', 'scipy.misc', 'scipy.cluster', 'scipy.constants', 'scipy.version', 'setuptools', 'distutils', 'pkg_resources', 'PyQt6.QtQml', 'PyQt6.QtQuick', 'PyQt6.QtMultimedia', 'PyQt6.QtMultimediaWidgets', 'PyQt6.QtWebEngineWidgets', 'PyQt6.QtWebEngineCore', 'PyQt6.QtDesigner', 'PyQt6.QtBluetooth', 'PyQt6.QtNetwork', 'PyQt6.QtPositioning', 'PyQt6.QtSensors', 'PyQt6.QtSerialPort', 'PyQt6.QtTest', 'PyQt6.QtOpenGL', 'PyQt6.QtPrintSupport', 'PyQt6.QtSql', 'PyQt6.QtHelp', 'PyQt6.QtUiTools', 'PyQt6.QtConcurrent', 'PyQt6.QtDBus', 'PyQt6.QtX11Extras', 'PyQt6.QtWinExtras', 'PyQt6.QtMacExtras'],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='PiraChest',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
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
    strip=False,
    upx=False,
    upx_exclude=[],
    name='PiraChest',
)
