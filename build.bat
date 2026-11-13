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
for /f "usebackq delims=" %%S in (`python -c "import sys;sys.path.insert(0,'.');from src.core.config import SLUG;print(SLUG)"`) do set "SLUG=%%S"
if not defined SLUG set "SLUG=PiraChest"
for /d /r "src" %%d in (__pycache__) do @if exist "%%d" rd /s /q "%%d" 2>nul
if exist build rmdir /s /q build
if exist "dist\%SLUG%" rmdir /s /q "dist\%SLUG%"
if not exist VERSION echo 0.1.0-beta> VERSION

echo [build] Running PyInstaller...
python -m PyInstaller --noconfirm --clean PiraChest.spec
if errorlevel 1 (
    echo [error] Build failed.
    exit /b 1
)

if exist "dist\%SLUG%\data" rmdir /s /q "dist\%SLUG%\data"

echo [build] Pruning...
python tools\prune_build.py
if errorlevel 1 (
    echo [error] Build pruning failed.
    exit /b 1
)

if defined SKIP_MSI goto :done

where wix >nul 2>nul
if errorlevel 1 (
    echo [warn] WiX Toolset not found, skipping MSI.
    echo        Install it with: dotnet tool install --global wix --version 5.0.2
    goto :done
)

echo [build] Reading app info...
set "BRAND="
for /f "usebackq tokens=1-6 delims=|" %%A in (`python -c "import re,sys;sys.path.insert(0,'.');from src.core.config import APP_NAME,COMPANY_NAME,EXE_NAME,UPGRADE_CODE;v=open('VERSION').read().splitlines()[0].strip();m=re.match(r'(\d+)\.(\d+)\.(\d+)',v);print('|'.join([APP_NAME,EXE_NAME,COMPANY_NAME,UPGRADE_CODE,'.'.join(m.groups()),v]))"`) do (
    set "BRAND=%%A"
    set "EXENAME=%%B"
    set "COMPANY=%%C"
    set "UPGRADECODE=%%D"
    set "MSIVER=%%E"
    set "VERTEXT=%%F"
)
if not defined BRAND (
    echo [error] Could not read app info. VERSION must start with x.y.z
    exit /b 1
)

set "ASSETS=%CD%\installer\assets"
set "ICO=%CD%\src\gui\photos\icon.ico"
if not exist "%ASSETS%\dialog.bmp" (
    echo [error] Missing %ASSETS%\dialog.bmp
    exit /b 1
)
if not exist "%ASSETS%\banner.bmp" (
    echo [error] Missing %ASSETS%\banner.bmp
    exit /b 1
)
if not exist "%ICO%" (
    echo [error] Missing %ICO%
    exit /b 1
)

set "MSI=%CD%\dist\%SLUG%-v%VERTEXT%-Windows-x64.msi"
if exist "%MSI%" del /q "%MSI%"

echo [build] Building MSI from installer\PiraChest.wxs...
wix extension add -g WixToolset.UI.wixext/5.0.2 >nul 2>nul
wix build installer\PiraChest.wxs -arch x64 -pdbtype none -ext WixToolset.UI.wixext ^
  -d Brand="%BRAND%" ^
  -d ExeName="%EXENAME%" ^
  -d UpgradeCode="%UPGRADECODE%" ^
  -d Manufacturer="%COMPANY%" ^
  -d Version="%MSIVER%" ^
  -d ProjectUrl="https://github.com/PiraChest/PiraChest" ^
  -d SourceDir="%CD%\dist\%SLUG%" ^
  -d IconFile="%ICO%" ^
  -d DialogBmp="%ASSETS%\dialog.bmp" ^
  -d BannerBmp="%ASSETS%\banner.bmp" ^
  -o "%MSI%"
if errorlevel 1 (
    echo [error] WiX MSI build failed.
    exit /b 1
)

:done
echo [done] dist\%SLUG%
for %%f in ("dist\%SLUG%-*-Windows-x64.msi") do @if exist "%%f" echo [done] %%~ff
exit /b 0
