#!/usr/bin/env bash
# Lazy Auto Clicker installer.
#
#   ./install.sh                     full install (asks for sudo once)
#   sudo ./install.sh --permissions-only   just the uinput/udev/group setup
#
set -euo pipefail

cd "$(dirname "$(readlink -f "$0")")"

PERMS_ONLY=0
[[ "${1:-}" == "--permissions-only" ]] && PERMS_ONLY=1

REAL_USER="${SUDO_USER:-$USER}"
BIN_DIR="$HOME/.local/bin"
APP_DIR="$HOME/.local/share/applications"
ICON_DIR="$HOME/.local/share/icons/hicolor/scalable/apps"

setup_permissions() {
    echo "==> Loading the uinput kernel module"
    modprobe uinput || true
    echo uinput > /etc/modules-load.d/uinput.conf

    echo "==> Installing udev rule"
    install -Dm644 packaging/99-lazyautoclicker.rules /etc/udev/rules.d/99-lazyautoclicker.rules
    udevadm control --reload-rules
    udevadm trigger --subsystem-match=misc --action=change || true

    echo "==> Adding $REAL_USER to the input group"
    gpasswd -a "$REAL_USER" input >/dev/null

    echo
    echo "Permissions done. Log out and back in for the group change to apply."
}

if ((PERMS_ONLY)); then
    [[ $EUID -eq 0 ]] || { echo "Run this with sudo."; exit 1; }
    setup_permissions
    exit 0
fi

[[ $EUID -eq 0 ]] && { echo "Run the full install as your normal user, not root."; exit 1; }

# Works from a source checkout (after ./build.sh) or an unpacked release tarball.
if [[ -x dist/lazyautoclicker ]]; then
    BINARY=dist/lazyautoclicker
elif [[ -x lazyautoclicker ]]; then
    BINARY=lazyautoclicker
else
    echo "No lazyautoclicker binary found. Run ./build.sh first, or download a release from"
    echo "https://github.com/devbnkdr/LazyAutoClicker/releases"
    exit 1
fi

echo "==> Requesting sudo for the one-time permission setup"
sudo "$0" --permissions-only

echo "==> Installing the application"
install -Dm755 "$BINARY"                 "$BIN_DIR/lazyautoclicker"
install -Dm644 assets/lazyautoclicker.svg      "$ICON_DIR/lazyautoclicker.svg"
install -Dm644 packaging/lazyautoclicker.desktop "$APP_DIR/lazyautoclicker.desktop"
# The desktop session's PATH often excludes ~/.local/bin, so make Exec absolute.
sed -i "s|^Exec=lazyautoclicker|Exec=$BIN_DIR/lazyautoclicker|" "$APP_DIR/lazyautoclicker.desktop"

update-desktop-database "$APP_DIR" 2>/dev/null || true
gtk-update-icon-cache -f -t "$HOME/.local/share/icons/hicolor" 2>/dev/null || true

DESKTOP="$(xdg-user-dir DESKTOP 2>/dev/null || echo "$HOME/Desktop")"
if [[ -d "$DESKTOP" ]]; then
    echo "==> Placing a shortcut on the desktop"
    install -Dm755 "$APP_DIR/lazyautoclicker.desktop" "$DESKTOP/lazyautoclicker.desktop"
    # Plasma 6 only launches desktop files it trusts.
    command -v kwriteconfig6 >/dev/null && \
        gio set "$DESKTOP/lazyautoclicker.desktop" metadata::trusted true 2>/dev/null || true
fi

case ":$PATH:" in
    *":$BIN_DIR:"*) ;;
    *) echo
       echo "NOTE: $BIN_DIR is not on your PATH. For fish, add:"
       echo "  fish_add_path ~/.local/bin" ;;
esac

echo
echo "Installed. Launch it from the application menu, the desktop icon, or run: lazyautoclicker"
echo "If clicking fails with a permission error, log out and back in first."
