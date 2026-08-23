@echo off
setlocal enabledelayedexpansion

set START_TIME=%time%

echo [build] PiraChest (Windows)...
echo.

set PYINSTALLER_DISABLE_DISTUTILS_ALIAS=1
set PYTHONOPTIMIZE=2

python -c "import PyInstaller" 2>nul
if errorlevel 1 (
    echo [error] PyInstaller not found.
    exit /b 1
)

echo [build] Cleaning...
for /d /r . %%d in (__pycache__) do @if exist "%%d" rd /s /q "%%d" 2>nul
if exist build rmdir /s /q build
if exist dist\PiraChest.exe del /q dist\PiraChest.exe 2>nul
echo.


echo [build] Running PyInstaller...
echo.

python -m PyInstaller ^
    --name=PiraChest ^
    --icon=".\src\gui\photos\logo.ico" ^
    --onefile ^
    --noconfirm ^
    --clean ^
    --noupx ^
    --noconsole ^
    --optimize=2 ^
    --exclude-module=PyQt5 ^
    --exclude-module=PyQt6 ^
    --exclude-module=PySide2 ^
    --exclude-module=shiboken2 ^
    --exclude-module=tkinter ^
    --exclude-module=matplotlib ^
    --exclude-module=numpy ^
    --exclude-module=scipy ^
    --exclude-module=setuptools ^
    --exclude-module=pkg_resources ^
    --exclude-module=test ^
    --exclude-module=unittest ^
    --exclude-module=pydoc ^
    --exclude-module=doctest ^
    --exclude-module=av ^
    --exclude-module=lxml ^
    --exclude-module=PySide6.QtQml ^
    --exclude-module=PySide6.QtQuick ^
    --exclude-module=PySide6.QtQuickWidgets ^
    --exclude-module=PySide6.QtQuickControls2 ^
    --exclude-module=PySide6.QtQuick3D ^
    --exclude-module=PySide6.QtWebEngineWidgets ^
    --exclude-module=PySide6.QtWebEngineCore ^
    --exclude-module=PySide6.QtWebEngineQuick ^
    --exclude-module=PySide6.QtDesigner ^
    --exclude-module=PySide6.QtBluetooth ^
    --exclude-module=PySide6.QtNetworkAuth ^
    --exclude-module=PySide6.QtNfc ^
    --exclude-module=PySide6.QtPositioning ^
    --exclude-module=PySide6.QtLocation ^
    --exclude-module=PySide6.QtSensors ^
    --exclude-module=PySide6.QtSerialPort ^
    --exclude-module=PySide6.QtSerialBus ^
    --exclude-module=PySide6.QtTest ^
    --exclude-module=PySide6.QtOpenGL ^
    --exclude-module=PySide6.QtOpenGLWidgets ^
    --exclude-module=PySide6.QtPrintSupport ^
    --exclude-module=PySide6.QtSql ^
    --exclude-module=PySide6.QtHelp ^
    --exclude-module=PySide6.QtUiTools ^
    --exclude-module=PySide6.QtConcurrent ^
    --exclude-module=PySide6.QtDBus ^
    --exclude-module=PySide6.QtPdf ^
    --exclude-module=PySide6.QtPdfWidgets ^
    --exclude-module=PySide6.QtCharts ^
    --exclude-module=PySide6.QtDataVisualization ^
    --exclude-module=PySide6.QtRemoteObjects ^
    --exclude-module=PySide6.QtSpatialAudio ^
    --exclude-module=PySide6.QtStateMachine ^
    --exclude-module=PySide6.QtTextToSpeech ^
    --exclude-module=PySide6.QtWebChannel ^
    --exclude-module=PySide6.QtWebSockets ^
    --exclude-module=PySide6.Qt3DCore ^
    --exclude-module=PySide6.Qt3DRender ^
    --exclude-module=PySide6.Qt3DInput ^
    --exclude-module=PySide6.Qt3DLogic ^
    --exclude-module=PySide6.Qt3DAnimation ^
    --exclude-module=PySide6.Qt3DExtras ^
    --add-data "src\gui;src\gui" ^
    --add-data "src\core;src\core" ^
    --hidden-import=PySide6.QtCore ^
    --hidden-import=PySide6.QtGui ^
    --hidden-import=PySide6.QtWidgets ^
    --hidden-import=PySide6.QtSvg ^
    --hidden-import=PySide6.QtNetwork ^
    --hidden-import=PySide6.QtMultimedia ^
    --hidden-import=PySide6.QtMultimediaWidgets ^
    --hidden-import=libtorrent ^
    --hidden-import=curl_cffi ^
    --hidden-import=orjson ^
    --hidden-import=requests ^
    --hidden-import=qfluentwidgets ^
    --hidden-import=qframelesswindow ^
    --collect-data=qfluentwidgets ^
    src/main.py

if errorlevel 1 (
    echo.
    echo [error] Build failed.
    exit /b 1
)

echo.
for %%I in ("dist\PiraChest.exe") do set "size=%%~zI"
set /a "size_mb=!size! / 1048576"

echo [done] dist\PiraChest.exe - !size_mb! MB
echo Started: %START_TIME% - Finished: %time%
