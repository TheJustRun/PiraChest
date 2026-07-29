@echo off
setlocal enabledelayedexpansion

set START_TIME=%time%

echo [build] PiraChest (Windows)...
echo.
python -c "import PyInstaller" 2>nul
if errorlevel 1 (
    echo [error] PyInstaller not found. Install it first:
    echo     pip install -r requirements.txt
    echo     pip install pyinstaller
    exit /b 1
)
echo [build] Cleaning up pycache, build, and old outputs...
for /d /r . %%d in (__pycache__) do @if exist "%%d" rd /s /q "%%d"
if exist build rmdir /s /q build
if exist dist\PiraChest.exe del /q dist\PiraChest.exe
echo.

echo [build] Running PyInstaller...
echo.

python -m PyInstaller ^
    --name=PiraChest ^
    --icon=".\src\gui\icon.ico" ^
    --onedir ^
    --windowed ^
    --noconfirm ^
    --clean ^
    --strip ^
    --optimize=2 ^
    --exclude-module=PyQt5 ^
    --exclude-module=PySide2 ^
    --exclude-module=PySide6 ^
    --exclude-module=shiboken2 ^
    --exclude-module=shiboken6 ^
    --exclude-module=tkinter ^
    --exclude-module=matplotlib ^
    --exclude-module=numpy ^
    --exclude-module=scipy ^
    --exclude-module=setuptools ^
    --exclude-module=distutils ^
    --exclude-module=pkg_resources ^
    --exclude-module=test ^
    --exclude-module=unittest ^
    --exclude-module=pydoc ^
    --exclude-module=doctest ^
    --exclude-module=PyQt6.QtQml ^
    --exclude-module=PyQt6.QtQuick ^
    --exclude-module=PyQt6.QtQuickWidgets ^
    --exclude-module=PyQt6.QtWebEngineWidgets ^
    --exclude-module=PyQt6.QtWebEngineCore ^
    --exclude-module=PyQt6.QtWebEngineQuick ^
    --exclude-module=PyQt6.QtDesigner ^
    --exclude-module=PyQt6.QtBluetooth ^
    --exclude-module=PyQt6.QtNetwork ^
    --exclude-module=PyQt6.QtNetworkAuth ^
    --exclude-module=PyQt6.QtNfc ^
    --exclude-module=PyQt6.QtPositioning ^
    --exclude-module=PyQt6.QtLocation ^
    --exclude-module=PyQt6.QtSensors ^
    --exclude-module=PyQt6.QtSerialPort ^
    --exclude-module=PyQt6.QtSerialBus ^
    --exclude-module=PyQt6.QtTest ^
    --exclude-module=PyQt6.QtOpenGL ^
    --exclude-module=PyQt6.QtOpenGLWidgets ^
    --exclude-module=PyQt6.QtPrintSupport ^
    --exclude-module=PyQt6.QtSql ^
    --exclude-module=PyQt6.QtHelp ^
    --exclude-module=PyQt6.QtUiTools ^
    --exclude-module=PyQt6.QtConcurrent ^
    --exclude-module=PyQt6.QtDBus ^
    --exclude-module=PyQt6.QtX11Extras ^
    --exclude-module=PyQt6.QtWinExtras ^
    --exclude-module=PyQt6.QtMacExtras ^
    --exclude-module=PyQt6.QtPdf ^
    --exclude-module=PyQt6.QtPdfWidgets ^
    --exclude-module=PyQt6.QtCharts ^
    --exclude-module=PyQt6.QtDataVisualization ^
    --exclude-module=PyQt6.QtRemoteObjects ^
    --exclude-module=PyQt6.QtSpatialAudio ^
    --exclude-module=PyQt6.QtStateMachine ^
    --exclude-module=PyQt6.QtTextToSpeech ^
    --exclude-module=PyQt6.QtWebChannel ^
    --exclude-module=PyQt6.QtWebSockets ^
    --exclude-module=PyQt6.Qt3DCore ^
    --exclude-module=PyQt6.Qt3DRender ^
    --exclude-module=PyQt6.Qt3DInput ^
    --exclude-module=PyQt6.Qt3DLogic ^
    --exclude-module=PyQt6.Qt3DAnimation ^
    --exclude-module=PyQt6.Qt3DExtras ^
    --add-data "src\gui;src\gui" ^
    --add-data "src\core;src\core" ^
    --hidden-import=PyQt6.QtCore ^
    --hidden-import=PyQt6.QtGui ^
    --hidden-import=PyQt6.QtWidgets ^
    --hidden-import=PyQt6.QtSvg ^
    --hidden-import=PyQt6.QtMultimedia ^
    --hidden-import=PyQt6.QtMultimediaWidgets ^
    --hidden-import=requests ^
    --hidden-import=qfluentwidgets ^
    --collect-data=qfluentwidgets ^
    src/main.py

if errorlevel 1 (
    echo.
    echo [error] Build failed. See above for details.
    exit /b 1
)

echo.
echo [build] ────────────────────────────────────────────────
echo [done]  Output file: dist\PiraChest.exe
echo          Run: dist\PiraChest.exe
echo          Started:  %START_TIME%
echo          Finished: %time%
echo [build] ────────────────────────────────────────────────
endlocal