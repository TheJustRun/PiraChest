@echo off
setlocal
cd /d "%~dp0"

if exist "build_env\Scripts\activate.bat" (
    call "build_env\Scripts\activate.bat"
) else if exist ".venv\Scripts\activate.bat" (
    call ".venv\Scripts\activate.bat"
)

python -c "import sys" >nul 2>nul
if errorlevel 1 (
    echo [error] No usable Python found.
    exit /b 1
)

set "PIP=python -m pip install --disable-pip-version-check"
where uv >nul 2>nul
if not errorlevel 1 if defined VIRTUAL_ENV set "PIP=uv pip install"

echo [build] Installing dependencies...
%PIP% -r requirements.txt
if errorlevel 1 (
    echo [error] Dependency installation failed.
    exit /b 1
)

python -m PyInstaller --version >nul 2>nul
if errorlevel 1 (
    echo [build] Installing PyInstaller...
    %PIP% pyinstaller==6.22.2 pyinstaller-hooks-contrib==2026.9
    if errorlevel 1 (
        echo [error] PyInstaller installation failed.
        exit /b 1
    )
)

echo [build] Verifying imports...
python -c "import src.gui.main_window"
if errorlevel 1 (
    echo [error] Import check failed.
    exit /b 1
)

echo [build] Cleaning...
for /d /r "src" %%d in (__pycache__) do @if exist "%%d" rd /s /q "%%d" 2>nul
if exist build rmdir /s /q build
if exist dist\PiraChest rmdir /s /q dist\PiraChest
if not exist VERSION echo 0.1.0-beta> VERSION

echo [build] Running PyInstaller...
python -m PyInstaller --noconfirm --clean PiraChest.spec
if errorlevel 1 (
    echo [error] Build failed.
    exit /b 1
)

if exist "dist\PiraChest\data" rmdir /s /q "dist\PiraChest\data"

echo [build] Pruning...
set "PRUNE_ARGS=--msi"
if defined SKIP_MSI set "PRUNE_ARGS="
python tools\prune_build.py %PRUNE_ARGS%
if errorlevel 1 (
    echo [error] Build pruning failed.
    exit /b 1
)

echo [done] dist\PiraChest\PiraChest.exe
for %%f in ("dist\PiraChest-*-Windows-x64.msi") do @if exist "%%f" echo [done] %%~ff
exit /b 0
