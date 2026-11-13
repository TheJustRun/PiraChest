@echo off
setlocal

rem ===== Settings =====
set ROOT=C:\Users\Mahdi\Desktop\Projects\github\PiraChest
set BRAND=PiraChest
set EXENAME=PiraChest.exe
set VERSION=1.0.0
set MANUFACTURER=PiraChest
set PROJECTURL=https://example.com
rem Release GUID (same as CI). For separate test builds use: b7e0d3f2-9c41-4e8a-a6d5-2f7c1e9b4a68
set UPGRADECODE=a3f6c2d1-7b4e-4c9a-8f2d-5e1b0c7a9d34

set SRC=%ROOT%\dist\%BRAND%
set ICO=%ROOT%\src\gui\photos\icon.ico
set ASSETS=%ROOT%\installer\assets
set MSI=%ROOT%\installer\%BRAND%.msi

cd /d "%ROOT%\installer" || (echo [error] installer folder not found & pause & exit /b 1)

rem ===== Sanity checks =====
if not exist "%SRC%\%EXENAME%" (
    echo [error] %SRC%\%EXENAME% not found. Run build.bat first or fix SRC/EXENAME.
    pause & exit /b 1
)
if not exist "%ICO%" (
    echo [error] Icon not found: %ICO%
    pause & exit /b 1
)

rem ===== Convert JPG -> BMP if needed (WiX UI requires BMP) =====
if not exist "%ASSETS%\dialog.bmp" if exist "%ASSETS%\dialog.jpg" call :tobmp "%ASSETS%\dialog.jpg" "%ASSETS%\dialog.bmp" 493 312
if not exist "%ASSETS%\banner.bmp" if exist "%ASSETS%\banner.jpg" call :tobmp "%ASSETS%\banner.jpg" "%ASSETS%\banner.bmp" 493 58
if not exist "%ASSETS%\dialog.bmp" (echo [error] Missing %ASSETS%\dialog.bmp & pause & exit /b 1)
if not exist "%ASSETS%\banner.bmp" (echo [error] Missing %ASSETS%\banner.bmp & pause & exit /b 1)

rem ===== Build =====
wix extension add -g WixToolset.UI.wixext/5.0.2

wix build PiraChest.wxs -arch x64 -pdbtype none -ext WixToolset.UI.wixext ^
  -d Brand=%BRAND% ^
  -d ExeName=%EXENAME% ^
  -d UpgradeCode=%UPGRADECODE% ^
  -d Manufacturer=%MANUFACTURER% ^
  -d Version=%VERSION% ^
  -d ProjectUrl=%PROJECTURL% ^
  -d SourceDir="%SRC%" ^
  -d IconFile="%ICO%" ^
  -d DialogBmp="%ASSETS%\dialog.bmp" ^
  -d BannerBmp="%ASSETS%\banner.bmp" ^
  -o "%MSI%"
if errorlevel 1 (
    echo [error] wix build failed.
    pause & exit /b 1
)

echo [done] %MSI%
msiexec /i "%MSI%" /l*v "%ROOT%\installer\install.log"
pause
exit /b 0

:tobmp
rem %1=source image, %2=output bmp, %3=width, %4=height
powershell -NoProfile -Command "Add-Type -AssemblyName System.Drawing; $s=[System.Drawing.Image]::FromFile('%~1'); $b=New-Object System.Drawing.Bitmap(%~3,%~4,[System.Drawing.Imaging.PixelFormat]::Format24bppRgb); $g=[System.Drawing.Graphics]::FromImage($b); $g.DrawImage($s,0,0,%~3,%~4); $b.Save('%~2',[System.Drawing.Imaging.ImageFormat]::Bmp); $g.Dispose(); $b.Dispose(); $s.Dispose()"
exit /b 0
