#!/bin/bash
set -e

cd "$(dirname "$(readlink -f "$0")")"

echo "[build] PiraChest (Linux AppImage)..."

if [ ! -f "requirements.txt" ] || [ ! -f "src/main.py" ]; then
    echo "[build] error: this script must be run from the PiraChest project root."
    echo "[build] expected to find requirements.txt and src/main.py in the current directory: $(pwd)"
    exit 1
fi

echo "[build] Detecting package manager..."
if command -v dnf >/dev/null 2>&1; then
    PKG_INSTALL="sudo dnf install -y python3-pip fuse ImageMagick curl libxcb libxkbcommon-x11 xcb-util-cursor mesa-libEGL sqlite-devel"
elif command -v apt-get >/dev/null 2>&1; then
    sudo apt-get update
    PKG_INSTALL="sudo apt-get install -y python3-pip python3-venv fuse imagemagick curl libsqlite3-dev libgl1 libegl1 libxkbcommon0 libxkbcommon-x11-0 libdbus-1-3 libxcb-cursor0 libxcb-icccm4 libxcb-image0 libxcb-keysyms1 libxcb-randr0 libxcb-render-util0 libxcb-xinerama0 libxcb-xfixes0"
elif command -v pacman >/dev/null 2>&1; then
    PKG_INSTALL="sudo pacman -Sy --noconfirm python-pip python-virtualenv fuse2 imagemagick curl sqlite libxcb libxkbcommon-x11 xcb-util-cursor"
elif command -v zypper >/dev/null 2>&1; then
    PKG_INSTALL="sudo zypper install -y python3-pip python3-venv fuse imagemagick curl sqlite3-devel libxcb1 libxkbcommon-x11-0 xcb-util-cursor0"
else
    echo "[build] No supported package manager found (dnf/apt/pacman/zypper)."
    echo "[build] Please install manually: python3-pip, python3-venv, fuse, imagemagick."
    PKG_INSTALL=""
fi

if [ -n "$PKG_INSTALL" ]; then
    echo "[build] Installing system dependencies..."
    $PKG_INSTALL
fi

echo "[build] Setting up build virtual environment..."
if [[ "$(pwd)" == /mnt/hgfs/* ]] || [[ "$(stat -f -c %T . 2>/dev/null)" == "vmhgfs"* ]]; then
    echo "[build] Detected a VM shared folder (hgfs); building in a local temp directory to avoid symlink issues."
    BUILD_DIR="$(mktemp -d /tmp/pirachest-build.XXXXXX)"
    PROJECT_DIR="$(pwd)"
    trap 'rm -rf "$BUILD_DIR"' EXIT
    if command -v rsync >/dev/null 2>&1; then
        rsync -a --exclude='.build-venv' --exclude='dist' --exclude='build' --exclude='AppDir' "$PROJECT_DIR"/ "$BUILD_DIR"/
    else
        cp -r "$PROJECT_DIR"/. "$BUILD_DIR"/
        rm -rf "$BUILD_DIR/.build-venv" "$BUILD_DIR/dist" "$BUILD_DIR/build" "$BUILD_DIR/AppDir"
    fi
    cd "$BUILD_DIR"
else
    PROJECT_DIR="$(pwd)"
    BUILD_DIR="$(pwd)"
fi

VENV_DIR=".build-venv"
python3 -m venv "$VENV_DIR" --system-site-packages
PIP="$VENV_DIR/bin/pip"
PYINSTALLER="$VENV_DIR/bin/pyinstaller"

echo "[build] Installing Python requirements..."
"$PIP" install --upgrade pip
"$PIP" install -r requirements.txt

echo "[build] Installing PyInstaller..."
"$PIP" install --upgrade pyinstaller
"$PIP" check

echo "[build] Running PyInstaller..."
export PYTHONOPTIMIZE=2
export PYINSTALLER_DISABLE_DISTUTILS_ALIAS=1
"$PYINSTALLER" \
    --name=PiraChest \
    --onedir \
    --windowed \
    --noconfirm \
    --clean \
    --strip \
    --noupx \
    --optimize=2 \
    --icon=src/gui/photos/logo.ico \
    --exclude-module=PyQt5 \
    --exclude-module=PyQt6 \
    --exclude-module=PySide2 \
    --exclude-module=shiboken2 \
    --exclude-module=tkinter \
    --exclude-module=matplotlib \
    --exclude-module=numpy \
    --exclude-module=scipy \
    --exclude-module=setuptools \
    --exclude-module=pkg_resources \
    --exclude-module=test \
    --exclude-module=unittest \
    --exclude-module=pydoc \
    --exclude-module=doctest \
    --exclude-module=av \
    --exclude-module=lxml \
    --exclude-module=PySide6.QtQml \
    --exclude-module=PySide6.QtQuick \
    --exclude-module=PySide6.QtQuickWidgets \
    --exclude-module=PySide6.QtQuickControls2 \
    --exclude-module=PySide6.QtQuick3D \
    --exclude-module=PySide6.QtWebEngineWidgets \
    --exclude-module=PySide6.QtWebEngineCore \
    --exclude-module=PySide6.QtWebEngineQuick \
    --exclude-module=PySide6.QtDesigner \
    --exclude-module=PySide6.QtBluetooth \
    --exclude-module=PySide6.QtNetworkAuth \
    --exclude-module=PySide6.QtNfc \
    --exclude-module=PySide6.QtPositioning \
    --exclude-module=PySide6.QtLocation \
    --exclude-module=PySide6.QtSensors \
    --exclude-module=PySide6.QtSerialPort \
    --exclude-module=PySide6.QtSerialBus \
    --exclude-module=PySide6.QtTest \
    --exclude-module=PySide6.QtOpenGL \
    --exclude-module=PySide6.QtOpenGLWidgets \
    --exclude-module=PySide6.QtPrintSupport \
    --exclude-module=PySide6.QtSql \
    --exclude-module=PySide6.QtHelp \
    --exclude-module=PySide6.QtUiTools \
    --exclude-module=PySide6.QtConcurrent \
    --exclude-module=PySide6.QtDBus \
    --exclude-module=PySide6.QtPdf \
    --exclude-module=PySide6.QtPdfWidgets \
    --exclude-module=PySide6.QtCharts \
    --exclude-module=PySide6.QtDataVisualization \
    --exclude-module=PySide6.QtRemoteObjects \
    --exclude-module=PySide6.QtSpatialAudio \
    --exclude-module=PySide6.QtStateMachine \
    --exclude-module=PySide6.QtTextToSpeech \
    --exclude-module=PySide6.QtWebChannel \
    --exclude-module=PySide6.QtWebSockets \
    --exclude-module=PySide6.Qt3DCore \
    --exclude-module=PySide6.Qt3DRender \
    --exclude-module=PySide6.Qt3DInput \
    --exclude-module=PySide6.Qt3DLogic \
    --exclude-module=PySide6.Qt3DAnimation \
    --exclude-module=PySide6.Qt3DExtras \
    --collect-submodules=qfluentwidgets \
    --collect-submodules=sqlite3 \
    --collect-binaries=PySide6 --collect-binaries=libtorrent --collect-data=qfluentwidgets \
    --add-data "src/gui:src/gui" \
    --add-data "src/core:src/core" \
    --hidden-import=sqlite3 \
    --hidden-import=_sqlite3 \
    --hidden-import=PySide6.QtCore \
    --hidden-import=PySide6.QtGui \
    --hidden-import=PySide6.QtWidgets \
    --hidden-import=PySide6.QtSvg \
    --hidden-import=PySide6.QtNetwork \
    --hidden-import=PySide6.QtMultimedia \
    --hidden-import=PySide6.QtMultimediaWidgets \
    --hidden-import=libtorrent \
    --hidden-import=curl_cffi \
    --hidden-import=orjson \
    --hidden-import=requests \
    --hidden-import=httpx \
    --collect-submodules=httpx \
    --hidden-import=qfluentwidgets \
    --hidden-import=qframelesswindow \
    src/main.py

echo "[build] PyInstaller onedir output ready at dist/PiraChest/"

echo "[build] Assembling AppDir..."
rm -rf AppDir
mkdir -p AppDir/usr/bin
cp -r dist/PiraChest/. AppDir/usr/bin/
chmod +x AppDir/usr/bin/PiraChest

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
if [ -f src/gui/photos/logo.ico ]; then
    convert src/gui/photos/logo.ico[0] -resize 256x256 -gravity center -background none -extent 256x256 AppDir/usr/share/icons/hicolor/256x256/apps/PiraChest.png
elif [ -f src/gui/photos/logo.png ]; then
    convert src/gui/photos/logo.png -resize 256x256 -gravity center -background none -extent 256x256 AppDir/usr/share/icons/hicolor/256x256/apps/PiraChest.png
else
    echo "[build] No icon found, generating a placeholder icon."
    convert -size 256x256 xc:'#282828' AppDir/usr/share/icons/hicolor/256x256/apps/PiraChest.png
fi

echo "[build] Fetching linuxdeploy..."
if [ ! -f linuxdeploy-x86_64.AppImage ]; then
    if command -v curl >/dev/null 2>&1; then
        curl -fL --retry 5 --retry-delay 5 --retry-all-errors \
            -o linuxdeploy-x86_64.AppImage \
            https://github.com/linuxdeploy/linuxdeploy/releases/download/continuous/linuxdeploy-x86_64.AppImage
    elif command -v wget >/dev/null 2>&1; then
        wget -O linuxdeploy-x86_64.AppImage \
            https://github.com/linuxdeploy/linuxdeploy/releases/download/continuous/linuxdeploy-x86_64.AppImage
    else
        echo "[build] error: neither curl nor wget is available to download linuxdeploy."
        exit 1
    fi
    chmod +x linuxdeploy-x86_64.AppImage
fi

echo "[build] Building AppImage..."
export ARCH=x86_64
./linuxdeploy-x86_64.AppImage \
    --appdir AppDir \
    --executable AppDir/usr/bin/PiraChest \
    --desktop-file AppDir/usr/share/applications/PiraChest.desktop \
    --icon-file AppDir/usr/share/icons/hicolor/256x256/apps/PiraChest.png \
    --output appimage

APPIMAGE_FILE=$(ls PiraChest*.AppImage | head -n1)
mkdir -p dist
mv "$APPIMAGE_FILE" "dist/${APPIMAGE_FILE}"

if [ "$BUILD_DIR" != "$PROJECT_DIR" ]; then
    echo "[build] Copying output back to project directory..."
    mkdir -p "$PROJECT_DIR/dist"
    cp "dist/${APPIMAGE_FILE}" "$PROJECT_DIR/dist/${APPIMAGE_FILE}"
    rm -rf "$PROJECT_DIR/dist/PiraChest"
    cp -r "dist/PiraChest" "$PROJECT_DIR/dist/PiraChest" 2>/dev/null || true
    cd "$PROJECT_DIR"
fi

echo "[done]  Onedir output:  dist/PiraChest/"
echo "        AppImage:       dist/$(basename "$APPIMAGE_FILE")"
echo "        Run: dist/$(basename "$APPIMAGE_FILE")"