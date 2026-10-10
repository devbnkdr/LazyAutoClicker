#!/usr/bin/env bash
set -euo pipefail

DESKTOP="$(xdg-user-dir DESKTOP 2>/dev/null || echo "$HOME/Desktop")"

rm -f "$HOME/.local/bin/lazyautoclicker" \
      "$HOME/.local/share/applications/lazyautoclicker.desktop" \
      "$HOME/.local/share/icons/hicolor/scalable/apps/lazyautoclicker.svg" \
      "$DESKTOP/lazyautoclicker.desktop"
rm -rf "$HOME/.config/lazyautoclicker"

echo "Application removed."
echo
echo "The udev rule and input-group membership are left in place because other"
echo "tools may rely on them. To remove those too:"
echo "  sudo rm /etc/udev/rules.d/99-lazyautoclicker.rules /etc/modules-load.d/uinput.conf"
echo "  sudo gpasswd -d \$USER input"
