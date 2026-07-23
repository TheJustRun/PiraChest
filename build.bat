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
if exist dist\PiraChest rmdir /s /q dist\PiraChest
echo.

echo [build] Running PyInstaller...
echo.

python -m PyInstaller ^
    --name=PiraChest ^
    --icon=".\src\gui\icon.ico" ^
    --onedir ^
    --windowed ^
    --noconfirm ^
    --noupx ^
    --clean ^
    --exclude-module=PyQt5 ^
    --exclude-module=PySide2 ^
    --exclude-module=PySide6 ^
    --exclude-module=shiboken2 ^
    --exclude-module=shiboken6 ^
    --exclude-module=tkinter ^
    --exclude-module=matplotlib ^
    --exclude-module=numpy ^
    --exclude-module=numpy.core ^
    --exclude-module=numpy.lib ^
    --exclude-module=numpy.random ^
    --exclude-module=numpy.fft ^
    --exclude-module=numpy.linalg ^
    --exclude-module=numpy.polynomial ^
    --exclude-module=scipy ^
    --exclude-module=scipy.special ^
    --exclude-module=scipy.spatial ^
    --exclude-module=scipy.stats ^
    --exclude-module=scipy.sparse ^
    --exclude-module=scipy.linalg ^
    --exclude-module=scipy.optimize ^
    --exclude-module=scipy.signal ^
    --exclude-module=scipy.fft ^
    --exclude-module=scipy.integrate ^
    --exclude-module=scipy.interpolate ^
    --exclude-module=scipy.io ^
    --exclude-module=scipy.ndimage ^
    --exclude-module=scipy.odr ^
    --exclude-module=scipy.fftpack ^
    --exclude-module=scipy.misc ^
    --exclude-module=scipy.cluster ^
    --exclude-module=scipy.constants ^
    --exclude-module=scipy.version ^
    --exclude-module=setuptools ^
    --exclude-module=distutils ^
    --exclude-module=pkg_resources ^
    --exclude-module=PyQt6.QtQml ^
    --exclude-module=PyQt6.QtQuick ^
    --exclude-module=PyQt6.QtMultimedia ^
    --exclude-module=PyQt6.QtMultimediaWidgets ^
    --exclude-module=PyQt6.QtWebEngineWidgets ^
    --exclude-module=PyQt6.QtWebEngineCore ^
    --exclude-module=PyQt6.QtDesigner ^
    --exclude-module=PyQt6.QtBluetooth ^
    --exclude-module=PyQt6.QtNetwork ^
    --exclude-module=PyQt6.QtPositioning ^
    --exclude-module=PyQt6.QtSensors ^
    --exclude-module=PyQt6.QtSerialPort ^
    --exclude-module=PyQt6.QtTest ^
    --exclude-module=PyQt6.QtOpenGL ^
    --exclude-module=PyQt6.QtPrintSupport ^
    --exclude-module=PyQt6.QtSql ^
    --exclude-module=PyQt6.QtHelp ^
    --exclude-module=PyQt6.QtUiTools ^
    --exclude-module=PyQt6.QtConcurrent ^
    --exclude-module=PyQt6.QtDBus ^
    --exclude-module=PyQt6.QtX11Extras ^
    --exclude-module=PyQt6.QtWinExtras ^
    --exclude-module=PyQt6.QtMacExtras ^
    --collect-submodules=PyQt6.QtCore ^
    --collect-submodules=PyQt6.QtGui ^
    --collect-submodules=PyQt6.QtWidgets ^
    --collect-submodules=PyQt6.QtSvg ^
    --collect-submodules=PyQt6.QtXml ^
    --collect-submodules=qfluentwidgets ^
    --collect-data=qfluentwidgets ^
    --add-data "src;src" ^
    --hidden-import=PyQt6.QtCore ^
    --hidden-import=PyQt6.QtGui ^
    --hidden-import=PyQt6.QtWidgets ^
    --hidden-import=PyQt6.QtSvg ^
    --hidden-import=PyQt6.QtXml ^
    --hidden-import=requests ^
    --hidden-import=orjson ^
    --hidden-import=qfluentwidgets ^
    src/main.py

if errorlevel 1 (
    echo.
    echo [error] Build failed. See above for details.
    exit /b 1
)

echo.
echo [build] ────────────────────────────────────────────────
echo [done]  Output directory: dist\PiraChest\
echo          Run: dist\PiraChest\PiraChest.exe
echo          Started:  %START_TIME%
echo          Finished: %time%
echo [build] ────────────────────────────────────────────────
endlocal
