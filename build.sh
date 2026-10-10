#!/usr/bin/env bash
# Builds dist/lazyautoclicker — a single self-contained executable.
set -euo pipefail

cd "$(dirname "$(readlink -f "$0")")"

VENV="$PWD/.buildenv"

if command -v pacman >/dev/null 2>&1; then
    # Arch / CachyOS: build against the distro's PySide6 and evdev.
    echo "==> Checking runtime dependencies"
    missing=()
    for pkg in pyside6 python-evdev; do
        pacman -Qq "$pkg" >/dev/null 2>&1 || missing+=("$pkg")
    done
    if ((${#missing[@]})); then
        echo "Missing: ${missing[*]}"
        echo "Install with:  sudo pacman -S --needed ${missing[*]}"
        exit 1
    fi

    if [[ ! -x "$VENV/bin/pyinstaller" ]]; then
        echo "==> Creating build environment in $VENV"
        rm -rf "$VENV"
        python -m venv --system-site-packages "$VENV"
        "$VENV/bin/pip" install --quiet pyinstaller
    fi
else
    # Anything else (and CI): pull everything from PyPI into an isolated venv.
    if [[ ! -x "$VENV/bin/pyinstaller" ]]; then
        echo "==> Creating build environment in $VENV (PyPI)"
        rm -rf "$VENV"
        python3 -m venv "$VENV"
        "$VENV/bin/pip" install --quiet --upgrade pip
        "$VENV/bin/pip" install --quiet -r requirements.txt pyinstaller
    fi
fi

echo "==> Verifying the build interpreter can see PySide6 and evdev"
if ! "$VENV/bin/python" -c "import PySide6, evdev" 2>/dev/null; then
    echo
    echo "FAILED: the build interpreter cannot import PySide6 and/or evdev."
    echo "Without them PyInstaller produces a binary that crashes on startup."
    echo
    echo "Most likely the venv was built against a different Python than the"
    echo "one pacman installed the packages for. Recreate it:"
    echo "    rm -rf $VENV"
    echo "    ./build.sh"
    exit 1
fi
echo "    ok — $("$VENV/bin/python" -c 'import PySide6; print("PySide6", PySide6.__version__)')"

echo "==> Cleaning previous build"
rm -rf build dist lazyautoclicker.spec

echo "==> Building (a couple of minutes)"
"$VENV/bin/pyinstaller" \
    --onefile \
    --name lazyautoclicker \
    --windowed \
    --clean \
    --noconfirm \
    --hidden-import evdev \
    --collect-submodules evdev \
    --exclude-module PySide6.QtWebEngineCore \
    --exclude-module PySide6.QtWebEngineWidgets \
    --exclude-module PySide6.QtQuick3D \
    --exclude-module PySide6.QtMultimedia \
    --exclude-module PySide6.Qt3DCore \
    --exclude-module tkinter \
    --exclude-module numpy \
    lazyautoclicker.py

chmod +x dist/lazyautoclicker

echo "==> Smoke-testing the binary"
if ! out="$(timeout 30 ./dist/lazyautoclicker --check-imports 2>&1)"; then
    echo "FAILED: the built binary cannot import its dependencies."
    echo "$out"
    exit 1
fi
echo "    $out"

echo
echo "Built: $PWD/dist/lazyautoclicker  ($(du -h dist/lazyautoclicker | cut -f1))"
echo "Next:  ./install.sh"
