#!/bin/bash
set -e

echo "[build] PiraChest (Linux AppImage)..."

echo "[build] Installing system libtorrent bindings..."
sudo dnf install -y python3-pip python3-pyqt6 rb_libtorrent-python3 fuse

echo "[build] Installing Python requirements..."
pip install --user -r requirements.txt

echo "[build] Installing PyInstaller..."
pip install --user pyinstaller

echo "[build] Running PyInstaller..."
~/.local/bin/pyinstaller \
    --name=PiraChest \
    --onedir \
    --windowed \
    --noconfirm \
    --clean \
    --exclude-module=PyQt5 \
    --exclude-module=PySide2 \
    --exclude-module=PySide6 \
    --exclude-module=tkinter \
    --exclude-module=matplotlib \
    --exclude-module=numpy \
    --exclude-module=scipy \
    --exclude-module=setuptools \
    --exclude-module=distutils \
    --exclude-module=pkg_resources \
    --exclude-module=PyQt6.QtQml \
    --exclude-module=PyQt6.QtQuick \
    --exclude-module=PyQt6.QtWebEngineWidgets \
    --exclude-module=PyQt6.QtDesigner \
    --exclude-module=PyQt6.QtNetwork \
    --exclude-module=PyQt6.QtTest \
    --exclude-module=PyQt6.QtOpenGL \
    --collect-submodules=PyQt6.QtCore \
    --collect-submodules=PyQt6.QtGui \
    --collect-submodules=PyQt6.QtWidgets \
    --collect-submodules=PyQt6.QtSvg \
    --collect-submodules=qfluentwidgets \
    --collect-data=qfluentwidgets \
    --collect-data=PyQt6 \
    --add-data "src/gui:src/gui" \
    --add-data "src/core:src/core" \
    --hidden-import=PyQt6.QtCore \
    --hidden-import=PyQt6.QtGui \
    --hidden-import=PyQt6.QtWidgets \
    --hidden-import=PyQt6.QtSvg \
    --hidden-import=requests \
    --hidden-import=qfluentwidgets \
    src/main.py

echo "[build] PyInstaller onedir output ready at dist/PiraChest/"

echo "[build] Assembling AppDir..."
rm -rf AppDir
mkdir -p AppDir/usr/bin
cp -r dist/PiraChest/* AppDir/usr/bin/

mkdir -p AppDir/usr/share/applications
cat > AppDir/usr/share/applications/PiraChest.desktop <<'EOF'
[Desktop Entry]
Type=Application
Name=PiraChest
Exec=PiraChest
Icon=PiraChest
Categories=Network;FileTransfer;
Terminal=false
EOF

mkdir -p AppDir/usr/share/icons/hicolor/256x256/apps
if [ -f src/gui/logo.png ]; then
    cp src/gui/logo.png AppDir/usr/share/icons/hicolor/256x256/apps/PiraChest.png
else
    echo "[build] No src/gui/logo.png found, generating a placeholder icon."
    if command -v convert >/dev/null 2>&1; then
        convert -size 256x256 xc:'#282828' AppDir/usr/share/icons/hicolor/256x256/apps/PiraChest.png
    else
        sudo dnf install -y ImageMagick
        convert -size 256x256 xc:'#282828' AppDir/usr/share/icons/hicolor/256x256/apps/PiraChest.png
    fi
fi

echo "[build] Fetching linuxdeploy tools (if needed)..."
if [ ! -f linuxdeploy-x86_64.AppImage ]; then
    wget -q https://github.com/linuxdeploy/linuxdeploy/releases/download/continuous/linuxdeploy-x86_64.AppImage
    chmod +x linuxdeploy-x86_64.AppImage
fi
if [ ! -f linuxdeploy-plugin-qt-x86_64.AppImage ]; then
    wget -q https://github.com/linuxdeploy/linuxdeploy/releases/download/continuous/linuxdeploy-plugin-qt-x86_64.AppImage
    chmod +x linuxdeploy-plugin-qt-x86_64.AppImage
fi

echo "[build] Building AppImage..."
export ARCH=x86_64
export NO_STRIP=1
./linuxdeploy-x86_64.AppImage \
    --appdir AppDir \
    --executable AppDir/usr/bin/PiraChest \
    --desktop-file AppDir/usr/share/applications/PiraChest.desktop \
    --icon-file AppDir/usr/share/icons/hicolor/256x256/apps/PiraChest.png \
    --plugin qt \
    --output appimage
APPIMAGE_FILE=$(ls PiraChest*.AppImage | head -n1)
mv "$APPIMAGE_FILE" "dist/${APPIMAGE_FILE}"

echo "[done]  Onedir output:  dist/PiraChest/"
echo "        AppImage:       dist/$(basename "$APPIMAGE_FILE")"
echo "        Run: dist/$(basename "$APPIMAGE_FILE")"
