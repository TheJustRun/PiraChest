import os

from PyInstaller.utils.hooks import collect_data_files

datas = [
    ('src/gui', 'src/gui'),
    ('src/core', 'src/core'),
    ('VERSION', '.'),
    ('LICENSE', '.'),
]
datas += collect_data_files(
    'qfluentwidgets',
    excludes=['_rc/i18n', '_rc/js'],
)

hiddenimports = [
    'libtorrent',
    'musicdl',
    'qfluentwidgets',
    'qframelesswindow',
    'socks',
    'sockshandler',
]

excludes = [
    'tkinter', 'unittest', 'test', 'pydoc', 'doctest', 'setuptools', 'pkg_resources',
    'numpy', 'scipy', 'pandas', 'matplotlib',
    'PyQt5', 'PyQt6', 'PySide2', 'shiboken2',
    'av', 'PIL', 'lxml', 'curl_cffi', 'brotli', '_brotli', 'websocket', 'yt_dlp', 'imageio_ffmpeg',
    'win32evtlog', 'win32evtlogutil',
    'PySide6.QtWebEngineCore', 'PySide6.QtWebEngineWidgets', 'PySide6.QtWebEngineQuick',
    'PySide6.QtWebEngineQuickDelegatesQml', 'PySide6.QtWebView', 'PySide6.QtWebChannel',
    'PySide6.QtWebSockets', 'PySide6.QtHttpServer', 'PySide6.QtNetworkAuth',
    'PySide6.Qt3DCore', 'PySide6.Qt3DRender', 'PySide6.Qt3DInput', 'PySide6.Qt3DLogic',
    'PySide6.Qt3DAnimation', 'PySide6.Qt3DExtras', 'PySide6.Qt3DQuick',
    'PySide6.QtQuick3D', 'PySide6.QtVirtualKeyboard', 'PySide6.QtVirtualKeyboardQml',
    'PySide6.QtVirtualKeyboardSettings', 'PySide6.QtCharts', 'PySide6.QtDataVisualization',
    'PySide6.QtGraphs', 'PySide6.QtGraphsWidgets', 'PySide6.QtPdf', 'PySide6.QtPdfWidgets',
    'PySide6.QtSql', 'PySide6.QtTest', 'PySide6.QtDesigner', 'PySide6.QtHelp', 'PySide6.QtUiTools',
    'PySide6.QtPositioning', 'PySide6.QtLocation', 'PySide6.QtNfc', 'PySide6.QtBluetooth',
    'PySide6.QtSerialPort', 'PySide6.QtSerialBus', 'PySide6.QtScxml', 'PySide6.QtSensors',
    'PySide6.QtRemoteObjects', 'PySide6.QtSpatialAudio', 'PySide6.QtTextToSpeech',
    'PySide6.QtQuickControls2', 'PySide6.QtQuickTest', 'PySide6.QtStateMachine',
    'PySide6.QtPrintSupport', 'PySide6.QtConcurrent',
    'PySide6.QtDBus',
    'PySide6.QtQml', 'PySide6.QtQuick', 'PySide6.QtQuickWidgets', 'PySide6.QtMultimediaQuick',
    'fake_useragent',
]

a = Analysis(
    ['src/main.py'],
    pathex=[],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
    optimize=2,
)

_UNUSED_BINARIES = {
    'qt6pdf.dll',
    'qt6qml.dll',
    'qt6qmlmeta.dll',
    'qt6qmlmodels.dll',
    'qt6qmlworkerscript.dll',
    'qt6quick.dll',
    'qt6virtualkeyboard.dll',
    'qpdf.dll',
    'qtvirtualkeyboardplugin.dll',
    'qicns.dll',
    'qtga.dll',
    'qtiff.dll',
    'qwbmp.dll',
    'opengl32sw.dll',
}
a.binaries = [b for b in a.binaries if os.path.basename(b[0]).lower() not in _UNUSED_BINARIES]
a.datas = [
    d for d in a.datas
    if '__pycache__' not in d[0].replace('\\', '/').lower() and not d[0].lower().endswith('.pyc')
]

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
    icon=['src/gui/photos/icon.ico'],
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
