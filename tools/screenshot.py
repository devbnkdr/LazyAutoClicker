#!/usr/bin/env python3
"""Renders the README screenshots offscreen.

    QT_QPA_PLATFORM=offscreen python3 tools/screenshot.py

Writes docs/screenshot-dark.png, docs/screenshot-light.png and docs/icon.png. Uses a scratch
config directory so your real settings are neither read nor touched.
"""

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ["XDG_CONFIG_HOME"] = tempfile.mkdtemp()
sys.path.insert(0, str(ROOT))

import lazyautoclicker  # noqa: E402

OUT = ROOT / "docs"


def main() -> None:
    OUT.mkdir(exist_ok=True)
    sys.argv = [sys.argv[0], "--theme", "dark"]
    app, win = lazyautoclicker.build_app()
    win._check_permissions = lambda: None      # no modal dialogs while rendering

    def shoot(theme: str, scale: float = 2.0) -> None:
        lazyautoclicker.apply_theme(app, theme)
        win.on_theme_changed()
        win.show()
        win.activateWindow()
        for _ in range(10):
            app.processEvents()
        pm = win.grab()
        pm.save(str(OUT / f"screenshot-{theme}.png"))
        print(f"wrote docs/screenshot-{theme}.png  {pm.width()}x{pm.height()}")

    # A representative state: a run in progress.
    win.sp_ms.setValue(50)
    win._on_progress(1284, 19.8)
    win._set_status("Clicking", "running")
    win.action_btn.setText("Stop Clicking")
    win.action_btn.setObjectName("Danger")
    lazyautoclicker.repolish(win.action_btn)

    # The offscreen platform only activates the window after the first show,
    # so render one throwaway frame to get the coloured traffic lights.
    for theme in ("light", "dark", "light"):
        shoot(theme)

    icon = lazyautoclicker.app_icon().pixmap(256, 256)
    icon.save(str(OUT / "icon.png"))
    print("wrote docs/icon.png")
    app.quit()


if __name__ == "__main__":
    main()
