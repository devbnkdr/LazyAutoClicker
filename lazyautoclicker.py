#!/usr/bin/env python3
"""
Lazy Auto Clicker — a precision auto clicker for Wayland/X11 Linux desktops.

Input is synthesised at the kernel level through /dev/uinput, and the global
hotkey is read directly from /dev/input/event*, so it works under Wayland
compositors (KWin, Mutter, Hyprland) where XTest-based clickers do not.
"""

from __future__ import annotations

import json
import os
import selectors
import sys
import threading
import time
from dataclasses import dataclass, asdict, field
from pathlib import Path

from string import Template

from PySide6.QtCore import (
    Qt, QObject, Signal, QSize, QPoint, QPointF, QRectF, QTimer, QEvent,
    QPropertyAnimation, QEasingCurve, Property,
)
from PySide6.QtGui import (
    QGuiApplication, QIcon, QPixmap, QPainter, QColor, QPen, QFont,
    QFontDatabase, QAction, QPalette,
)
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtWidgets import (
    QApplication, QWidget, QVBoxLayout, QHBoxLayout, QLabel, QLayout,
    QPushButton, QSpinBox, QAbstractButton, QButtonGroup, QFrame,
    QGraphicsDropShadowEffect, QSystemTrayIcon, QMenu, QMessageBox,
)

APP_NAME = "Lazy Auto Clicker"
APP_ID = "lazyautoclicker"
VERSION = "1.2.0"
CONFIG_PATH = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / APP_ID / "settings.json"

# --------------------------------------------------------------------------
# Branding asset (embedded so the one-file binary needs no data files)
# --------------------------------------------------------------------------

ICON_SVG = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 256 256">
  <defs>
    <linearGradient id="bg" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0" stop-color="#5AB0FF"/>
      <stop offset="1" stop-color="#0A5FE0"/>
    </linearGradient>
    <linearGradient id="sheen" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0" stop-color="#FFFFFF" stop-opacity="0.30"/>
      <stop offset="0.55" stop-color="#FFFFFF" stop-opacity="0"/>
    </linearGradient>
    <linearGradient id="cursor" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0" stop-color="#FFFFFF"/>
      <stop offset="1" stop-color="#E6EEF9"/>
    </linearGradient>
  </defs>
  <rect x="16" y="16" width="224" height="224" rx="52" fill="url(#bg)"/>
  <rect x="16" y="16" width="224" height="224" rx="52" fill="url(#sheen)"/>
  <rect x="16.75" y="16.75" width="222.5" height="222.5" rx="51.25" fill="none" stroke="#FFFFFF" stroke-opacity="0.18" stroke-width="1.5"/>
  <circle cx="114" cy="96" r="30" fill="none" stroke="#FFFFFF" stroke-opacity="0.60" stroke-width="6"/>
  <circle cx="114" cy="96" r="54" fill="none" stroke="#FFFFFF" stroke-opacity="0.26" stroke-width="5"/>
  <path d="M114 96 L114 198 L138 174 L156 212 L178 202 L160 164 L192 160 Z"
        transform="translate(3 6)" fill="#00205A" fill-opacity="0.28"
        stroke="#00205A" stroke-opacity="0.28" stroke-width="8" stroke-linejoin="round"/>
  <path d="M114 96 L114 198 L138 174 L156 212 L178 202 L160 164 L192 160 Z"
        fill="url(#cursor)" stroke="#0B2E6B" stroke-width="7" stroke-linejoin="round"/>
</svg>"""


def app_icon() -> QIcon:
    renderer = QSvgRenderer(ICON_SVG.encode("utf-8"))
    icon = QIcon()
    for size in (16, 24, 32, 48, 64, 128, 256):
        pm = QPixmap(size, size)
        pm.fill(Qt.transparent)
        p = QPainter(pm)
        p.setRenderHint(QPainter.Antialiasing)
        renderer.render(p)
        p.end()
        icon.addPixmap(pm)
    return icon


# --------------------------------------------------------------------------
# Kernel input backend
# --------------------------------------------------------------------------

try:
    import evdev
    from evdev import UInput, AbsInfo, ecodes as ec
    EVDEV_ERROR = None
except Exception as exc:  # pragma: no cover
    evdev = None
    EVDEV_ERROR = str(exc)


class BackendError(RuntimeError):
    pass


def uinput_state() -> str:
    """Returns 'ok', 'nomodule', or 'denied'."""
    if not os.path.exists("/dev/uinput"):
        return "nomodule"
    if not os.access("/dev/uinput", os.W_OK):
        return "denied"
    return "ok"


def input_devices_readable() -> bool:
    try:
        nodes = [n for n in os.listdir("/dev/input") if n.startswith("event")]
    except OSError:
        return False
    return any(os.access(f"/dev/input/{n}", os.R_OK) for n in nodes)


class VirtualPointer:
    """A virtual mouse (relative) plus an optional absolute-positioning device."""

    BUTTONS = {"Left": "BTN_LEFT", "Middle": "BTN_MIDDLE", "Right": "BTN_RIGHT"}

    def __init__(self, virtual_size: tuple[int, int] | None = None):
        if evdev is None:
            raise BackendError(f"python-evdev is not available: {EVDEV_ERROR}")
        caps = {
            ec.EV_KEY: [ec.BTN_LEFT, ec.BTN_MIDDLE, ec.BTN_RIGHT],
            ec.EV_REL: [ec.REL_X, ec.REL_Y, ec.REL_WHEEL],
        }
        try:
            self.mouse = UInput(caps, name=f"{APP_NAME} Pointer", vendor=0x4F50,
                                product=0x0001, version=1)
        except PermissionError as exc:
            raise BackendError("No write access to /dev/uinput.") from exc
        except OSError as exc:
            raise BackendError(f"Could not open /dev/uinput: {exc}") from exc

        self.tablet = None
        self.virtual_size = virtual_size
        if virtual_size:
            w, h = virtual_size
            try:
                self.tablet = UInput(
                    {
                        ec.EV_KEY: [ec.BTN_TOOL_PEN, ec.BTN_TOUCH],
                        ec.EV_ABS: [
                            (ec.ABS_X, AbsInfo(0, 0, max(w - 1, 1), 0, 0, 100)),
                            (ec.ABS_Y, AbsInfo(0, 0, max(h - 1, 1), 0, 0, 100)),
                        ],
                    },
                    name=f"{APP_NAME} Positioner", vendor=0x4F50, product=0x0002, version=1,
                )
            except Exception:
                self.tablet = None

        # KWin/libinput can take most of a second to enumerate a new device.
        # Clicks sent before that are silently dropped.
        time.sleep(1.0)

    def move_to(self, x: int, y: int) -> bool:
        if self.tablet is None:
            return False
        w, h = self.virtual_size
        x = max(0, min(int(x), w - 1))
        y = max(0, min(int(y), h - 1))
        t = self.tablet
        t.write(ec.EV_KEY, ec.BTN_TOOL_PEN, 1)
        t.write(ec.EV_ABS, ec.ABS_X, x)
        t.write(ec.EV_ABS, ec.ABS_Y, y)
        t.syn()
        time.sleep(0.012)
        t.write(ec.EV_KEY, ec.BTN_TOOL_PEN, 0)
        t.syn()
        return True

    def move_relative(self, dx: int, dy: int) -> None:
        self.mouse.write(ec.EV_REL, ec.REL_X, int(dx))
        self.mouse.write(ec.EV_REL, ec.REL_Y, int(dy))
        self.mouse.syn()

    def click(self, button: str, hold_ms: float) -> None:
        code = getattr(ec, self.BUTTONS.get(button, "BTN_LEFT"))
        self.mouse.write(ec.EV_KEY, code, 1)
        self.mouse.syn()
        time.sleep(max(hold_ms, 0.5) / 1000.0)
        self.mouse.write(ec.EV_KEY, code, 0)
        self.mouse.syn()

    def close(self) -> None:
        for dev in (self.tablet, self.mouse):
            try:
                if dev is not None:
                    dev.close()
            except Exception:
                pass


# --------------------------------------------------------------------------
# Settings
# --------------------------------------------------------------------------

@dataclass
class Settings:
    hours: int = 0
    minutes: int = 0
    seconds: int = 0
    millis: int = 100
    button: str = "Left"
    click_type: str = "Single"
    repeat_mode: str = "forever"      # 'forever' | 'count'
    repeat_count: int = 100
    position_mode: str = "cursor"     # 'cursor' | 'fixed'
    pos_x: int = 0
    pos_y: int = 0
    jitter_pct: int = 0
    hold_ms: int = 12
    hotkey_key: str = "KEY_F6"
    hotkey_mods: list = field(default_factory=list)
    minimize_to_tray: bool = False

    @classmethod
    def load(cls) -> "Settings":
        try:
            data = json.loads(CONFIG_PATH.read_text())
            known = {f for f in cls.__dataclass_fields__}
            return cls(**{k: v for k, v in data.items() if k in known})
        except Exception:
            return cls()

    def save(self) -> None:
        try:
            CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
            CONFIG_PATH.write_text(json.dumps(asdict(self), indent=2))
        except Exception:
            pass


# --------------------------------------------------------------------------
# Click engine
# --------------------------------------------------------------------------

class Bridge(QObject):
    progress = Signal(int, float)
    stopped = Signal(str)
    hotkey = Signal()
    panic = Signal()
    captured = Signal(str, list)


class ClickEngine(threading.Thread):
    MULTI = {"Single": 1, "Double": 2, "Triple": 3}

    def __init__(self, pointer: VirtualPointer, cfg: Settings, bridge: Bridge):
        super().__init__(daemon=True)
        self.pointer = pointer
        self.cfg = cfg
        self.bridge = bridge
        self._stop = threading.Event()
        self.count = 0

    def stop(self) -> None:
        self._stop.set()

    def _interval(self) -> float:
        c = self.cfg
        base = c.hours * 3600 + c.minutes * 60 + c.seconds + c.millis / 1000.0
        base = max(base, 0.001)
        if c.jitter_pct:
            import random
            spread = base * c.jitter_pct / 100.0
            base = max(0.001, base + random.uniform(-spread, spread))
        return base

    def run(self) -> None:
        try:
            self._run()
        except Exception as exc:
            self.bridge.stopped.emit(f"error:{exc}")

    def _run(self) -> None:
        cfg = self.cfg
        reps = self.MULTI.get(cfg.click_type, 1)
        target = cfg.repeat_count if cfg.repeat_mode == "count" else 0
        started = time.perf_counter()
        deadline = started
        reason = "finished"

        if cfg.position_mode == "fixed":
            if not self.pointer.move_to(cfg.pos_x, cfg.pos_y):
                self.bridge.stopped.emit(
                    "error:This system ignores absolute positioning. "
                    "Switch to cursor mode.")
                return
            time.sleep(0.05)

        while not self._stop.is_set():
            interval = self._interval()
            hold = min(cfg.hold_ms, max(1.0, interval * 1000 / 3))

            if cfg.position_mode == "fixed":
                self.pointer.move_to(cfg.pos_x, cfg.pos_y)

            for i in range(reps):
                if self._stop.is_set():
                    break
                self.pointer.click(cfg.button, hold)
                if i < reps - 1:
                    time.sleep(0.055)

            self.count += 1
            elapsed = time.perf_counter() - started
            self.bridge.progress.emit(self.count, self.count / elapsed if elapsed > 0 else 0.0)

            if target and self.count >= target:
                break

            deadline += interval
            now = time.perf_counter()
            if deadline < now:
                deadline = now
            while not self._stop.is_set():
                remaining = deadline - time.perf_counter()
                if remaining <= 0:
                    break
                time.sleep(min(remaining, 0.015))

        if self._stop.is_set():
            reason = "stopped"
        self.bridge.stopped.emit(reason)


# --------------------------------------------------------------------------
# Global hotkey listener (reads /dev/input directly — works on Wayland)
# --------------------------------------------------------------------------

MOD_MAP = {
    "KEY_LEFTCTRL": "ctrl", "KEY_RIGHTCTRL": "ctrl",
    "KEY_LEFTALT": "alt", "KEY_RIGHTALT": "alt",
    "KEY_LEFTSHIFT": "shift", "KEY_RIGHTSHIFT": "shift",
    "KEY_LEFTMETA": "super", "KEY_RIGHTMETA": "super",
}
MOD_ORDER = ["ctrl", "alt", "shift", "super"]


def pretty_hotkey(key: str, mods: list) -> str:
    names = {"ctrl": "Ctrl", "alt": "Alt", "shift": "Shift", "super": "Super"}
    parts = [names[m] for m in MOD_ORDER if m in mods]
    label = key.removeprefix("KEY_")
    label = label.capitalize() if len(label) > 1 else label
    parts.append(label)
    return " + ".join(parts)


class HotkeyListener(threading.Thread):
    def __init__(self, bridge: Bridge, settings: Settings):
        super().__init__(daemon=True)
        self.bridge = bridge
        self.settings = settings
        self.capturing = False
        self._stop = threading.Event()
        self._held: set[str] = set()

    def stop(self) -> None:
        self._stop.set()

    def _keyboards(self):
        found = []
        for path in evdev.list_devices():
            try:
                dev = evdev.InputDevice(path)
            except OSError:
                continue
            caps = dev.capabilities()
            if ec.EV_KEY in caps and ec.KEY_A in caps[ec.EV_KEY] and APP_NAME not in dev.name:
                found.append(dev)
            else:
                dev.close()
        return found

    def run(self) -> None:
        if evdev is None:
            return
        devices = self._keyboards()
        if not devices:
            return
        sel = selectors.DefaultSelector()
        for dev in devices:
            sel.register(dev, selectors.EVENT_READ)

        while not self._stop.is_set():
            for key, _ in sel.select(timeout=0.25):
                dev = key.fileobj
                try:
                    for event in dev.read():
                        if event.type == ec.EV_KEY:
                            self._handle(event)
                except (OSError, BlockingIOError):
                    try:
                        sel.unregister(dev)
                        dev.close()
                    except Exception:
                        pass
        sel.close()
        for dev in devices:
            try:
                dev.close()
            except Exception:
                pass

    def _handle(self, event) -> None:
        names = ec.KEY.get(event.code)
        if isinstance(names, (list, tuple)):
            names = names[0]
        if not names:
            return

        if names in MOD_MAP:
            if event.value == 1:
                self._held.add(MOD_MAP[names])
            elif event.value == 0:
                self._held.discard(MOD_MAP[names])
            return

        if event.value != 1:
            return

        mods = sorted(self._held, key=MOD_ORDER.index)

        if self.capturing:
            self.capturing = False
            self.bridge.captured.emit(names, mods)
            return

        # Fixed panic stop, always available.
        if names == "KEY_ESC" and {"ctrl", "alt"} <= set(mods):
            self.bridge.panic.emit()
            return

        if names == self.settings.hotkey_key and set(mods) == set(self.settings.hotkey_mods):
            self.bridge.hotkey.emit()


# --------------------------------------------------------------------------
# Theme
# --------------------------------------------------------------------------

THEMES = {
    "dark": dict(
        bg="#1C1C1E", border="#38383C", card="#262628", card_border="#313134",
        sep="#353538", text="#F5F5F7", text2="#A1A1A6", text3="#6E6E73",
        field="#323235", field_border="#424246", seg_bg="#313134", seg_on="#5C5C61",
        seg_on_border="#5C5C61", key_shadow="#2A2A2D",
        accent="#0A84FF", accent_hover="#3395FF", accent_pressed="#0070E0",
        danger="#FF453A", danger_hover="#FF6259", danger_pressed="#E0362C",
        green="#30D158", orange="#FF9F0A", switch_off="#48484A", knob="#FFFFFF",
        disabled_bg="#2C2C2E", disabled_text="#636366", traffic_off="#48484B",
        shadow="170",
    ),
    "light": dict(
        bg="#F5F5F7", border="#D2D2D7", card="#FFFFFF", card_border="#E3E3E8",
        sep="#ECECEF", text="#1D1D1F", text2="#6E6E73", text3="#98989D",
        field="#F7F7F9", field_border="#D9D9DE", seg_bg="#EBEBEF", seg_on="#FFFFFF",
        seg_on_border="#D4D4D9", key_shadow="#C6C6CC",
        accent="#007AFF", accent_hover="#1F8BFF", accent_pressed="#006AE0",
        danger="#FF3B30", danger_hover="#FF5147", danger_pressed="#E0342A",
        green="#34C759", orange="#FF9500", switch_off="#E3E3E8", knob="#FFFFFF",
        disabled_bg="#E8E8ED", disabled_text="#AEAEB2", traffic_off="#D1D1D6",
        shadow="60",
    ),
}
THEME: dict[str, str] = dict(THEMES["dark"])

STYLE = Template("""
QWidget { color: $text; font-size: 13px; }

#Root { background: $bg; border: 1px solid $border; border-radius: 14px; }
#Root[native="true"] { border: none; border-radius: 0; }
#TitleText { color: $text; font-size: 13px; font-weight: 600; }

#Hero { background: $card; border: 1px solid $card_border; border-radius: 12px; }
#HeroValue { color: $text; font-size: 34px; font-weight: 600; }
#HeroValueMuted { color: $text2; font-size: 34px; font-weight: 300; }
#HeroUnit { color: $text2; font-size: 11px; }
#HeroHint { color: $text2; font-size: 12px; }
#StatusText { color: $text; font-size: 12px; font-weight: 600; }
#Dot { border-radius: 4px; background: $text3; }
#Dot[state="running"], #Dot[state="ok"] { background: $green; }
#Dot[state="warn"] { background: $orange; }
#Dot[state="error"] { background: $danger; }

#GroupTitle { color: $text2; font-size: 12px; font-weight: 600; padding-left: 6px; }
#Footnote { color: $text3; font-size: 11px; padding-left: 6px; padding-right: 6px; }
#Card { background: $card; border: 1px solid $card_border; border-radius: 10px; }
#Row { background: transparent; border: none; }
#Row[sep="true"] { border-top: 1px solid $sep; }
#RowLabel { color: $text; }
#Unit { color: $text2; font-size: 12px; }

QSpinBox {
    background: $field; border: 1px solid $field_border; border-radius: 6px;
    padding: 3px 4px; color: $text;
    selection-background-color: $accent; selection-color: #FFFFFF;
}
/* the step buttons are hidden by giving them no width rather than with NoButtons:
   with a border set, Qt 6.12 shrinks the text field of a NoButtons spin box to 1px */
QSpinBox::up-button, QSpinBox::down-button { width: 0px; border: none; }
QSpinBox:focus { border: 1px solid $accent; }
QSpinBox:disabled { color: $disabled_text; }

#Segmented { background: $seg_bg; border-radius: 7px; }
#Segment {
    background: transparent; border: 1px solid transparent; border-radius: 6px;
    padding: 3px 12px; color: $text; font-size: 12px;
}
#Segment:checked { background: $seg_on; border: 1px solid $seg_on_border; font-weight: 600; }

#Secondary {
    background: $field; border: 1px solid $field_border; border-radius: 6px;
    padding: 4px 12px; color: $text; font-size: 12px;
}
#Secondary:hover { border: 1px solid $text3; }
#Secondary:pressed { background: $seg_bg; }
#Secondary:disabled { color: $disabled_text; border: 1px solid $field_border; }

#KeyCap {
    background: $field; border: 1px solid $field_border; border-bottom: 2px solid $key_shadow;
    border-radius: 6px; padding: 3px 12px; color: $text;
    font-size: 12px; font-weight: 600; min-width: 48px;
}
#KeyCap:hover { border: 1px solid $text3; border-bottom: 2px solid $key_shadow; }
#KeyCap[capturing="true"] { color: $accent; border: 1px solid $accent; border-bottom: 2px solid $accent; }
#KeyCap:disabled { color: $disabled_text; }

#Primary, #Danger {
    border: none; border-radius: 12px; color: #FFFFFF; font-size: 14px; font-weight: 600;
}
#Primary { background: $accent; }
#Primary:hover { background: $accent_hover; }
#Primary:pressed { background: $accent_pressed; }
#Primary:disabled { background: $disabled_bg; color: $disabled_text; }
#Danger { background: $danger; }
#Danger:hover { background: $danger_hover; }
#Danger:pressed { background: $danger_pressed; }

QMenu { background: $card; border: 1px solid $border; border-radius: 8px; padding: 4px; }
QMenu::item { padding: 5px 20px; border-radius: 5px; color: $text; }
QMenu::item:selected { background: $accent; color: #FFFFFF; }
QMenu::separator { height: 1px; background: $sep; margin: 4px 8px; }
QToolTip { background: $card; color: $text; border: 1px solid $border; border-radius: 6px; padding: 4px 8px; }
""")


def system_theme() -> str:
    scheme = QGuiApplication.styleHints().colorScheme()
    return "light" if scheme == Qt.ColorScheme.Light else "dark"


def apply_theme(app: QApplication, name: str) -> None:
    THEME.clear()
    THEME.update(THEMES[name])
    t = THEME
    pal = QPalette()
    for role, key in (
        (QPalette.Window, "bg"), (QPalette.WindowText, "text"), (QPalette.Base, "field"),
        (QPalette.AlternateBase, "card"), (QPalette.Text, "text"), (QPalette.Button, "card"),
        (QPalette.ButtonText, "text"), (QPalette.Highlight, "accent"),
        (QPalette.ToolTipBase, "card"), (QPalette.ToolTipText, "text"),
        (QPalette.PlaceholderText, "text3"),
    ):
        pal.setColor(role, QColor(t[key]))
    pal.setColor(QPalette.HighlightedText, QColor("#FFFFFF"))
    app.setPalette(pal)
    app.setStyleSheet(STYLE.substitute(t))
    for w in app.allWidgets():
        w.update()


def repolish(w: QWidget) -> None:
    w.style().unpolish(w)
    w.style().polish(w)
    w.update()


def blend(a: QColor, b: QColor, t: float) -> QColor:
    return QColor(
        round(a.red() + (b.red() - a.red()) * t),
        round(a.green() + (b.green() - a.green()) * t),
        round(a.blue() + (b.blue() - a.blue()) * t),
    )


# --------------------------------------------------------------------------
# Controls
# --------------------------------------------------------------------------

class Switch(QAbstractButton):
    """An animated on/off switch."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setCheckable(True)
        self.setCursor(Qt.PointingHandCursor)
        self.setFixedSize(38, 22)
        self._offset = 0.0
        self._anim = QPropertyAnimation(self, b"offset", self)
        self._anim.setDuration(170)
        self._anim.setEasingCurve(QEasingCurve.OutCubic)
        self.toggled.connect(self._animate)

    def _animate(self, on: bool) -> None:
        self._anim.stop()
        self._anim.setEndValue(1.0 if on else 0.0)
        self._anim.start()

    def snap(self) -> None:
        self._anim.stop()
        self._set_offset(1.0 if self.isChecked() else 0.0)

    def _get_offset(self) -> float:
        return self._offset

    def _set_offset(self, v: float) -> None:
        self._offset = v
        self.update()

    offset = Property(float, _get_offset, _set_offset)

    def sizeHint(self) -> QSize:
        return QSize(38, 22)

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        if not self.isEnabled():
            p.setOpacity(0.45)
        r = QRectF(self.rect())
        p.setPen(Qt.NoPen)
        p.setBrush(blend(QColor(THEME["switch_off"]), QColor(THEME["accent"]), self._offset))
        p.drawRoundedRect(r, r.height() / 2, r.height() / 2)
        d = r.height() - 4
        x = 2 + self._offset * (r.width() - d - 4)
        p.setBrush(QColor(0, 0, 0, 45))
        p.drawEllipse(QRectF(x, 2.8, d, d))
        p.setBrush(QColor(THEME["knob"]))
        p.drawEllipse(QRectF(x, 2, d, d))
        p.end()


class TrafficLight(QAbstractButton):
    def __init__(self, color: str, glyph: str, parent=None):
        super().__init__(parent)
        self.color = color
        self.glyph = glyph
        self.setFixedSize(12, 12)
        self.setCursor(Qt.ArrowCursor)

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        active = self.isEnabled() and self.window().isActiveWindow()
        c = QColor(self.color if active else THEME["traffic_off"])
        p.setPen(QPen(c.darker(118), 0.6))
        p.setBrush(c)
        p.drawEllipse(QRectF(0.5, 0.5, 11, 11))
        if self.isEnabled() and self.parentWidget().underMouse():
            p.setPen(QPen(QColor(0, 0, 0, 150), 1.3, Qt.SolidLine, Qt.RoundCap))
            if self.glyph == "close":
                p.drawLine(QPointF(3.8, 3.8), QPointF(8.2, 8.2))
                p.drawLine(QPointF(8.2, 3.8), QPointF(3.8, 8.2))
            elif self.glyph == "min":
                p.drawLine(QPointF(3.2, 6), QPointF(8.8, 6))
        p.end()


class TrafficLights(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(8)
        self.close_btn = TrafficLight("#FF5F57", "close")
        self.min_btn = TrafficLight("#FEBC2E", "min")
        self.zoom_btn = TrafficLight("#28C840", "zoom")
        self.zoom_btn.setEnabled(False)   # fixed-size window, as on macOS
        for b in (self.close_btn, self.min_btn, self.zoom_btn):
            lay.addWidget(b)
        self.setFixedWidth(52)

    def enterEvent(self, ev):
        self.update()
        for b in self.findChildren(TrafficLight):
            b.update()

    def leaveEvent(self, ev):
        self.enterEvent(ev)


class Segmented(QFrame):
    """A macOS-style segmented control. Emits the value of the chosen segment."""

    changed = Signal(str)

    def __init__(self, options: list[tuple[str, str]]):
        super().__init__()
        self.setObjectName("Segmented")
        lay = QHBoxLayout(self)
        lay.setContentsMargins(2, 2, 2, 2)
        lay.setSpacing(2)
        self.group = QButtonGroup(self)
        self.buttons: dict[str, QPushButton] = {}
        for value, label in options:
            b = QPushButton(label)
            b.setObjectName("Segment")
            b.setCheckable(True)
            b.setCursor(Qt.PointingHandCursor)
            b.toggled.connect(lambda on, v=value: on and self.changed.emit(v))
            self.group.addButton(b)
            lay.addWidget(b)
            self.buttons[value] = b

    def value(self) -> str:
        for v, b in self.buttons.items():
            if b.isChecked():
                return v
        return next(iter(self.buttons))

    def set_value(self, value: str) -> None:
        (self.buttons.get(value) or next(iter(self.buttons.values()))).setChecked(True)


class Card(QFrame):
    """A rounded group of settings rows separated by hairlines."""

    def __init__(self):
        super().__init__()
        self.setObjectName("Card")
        self.lay = QVBoxLayout(self)
        self.lay.setContentsMargins(0, 0, 0, 0)
        self.lay.setSpacing(0)

    def add_row(self, label: str, *items) -> QFrame:
        row = QFrame()
        row.setObjectName("Row")
        row.setProperty("sep", self.lay.count() > 0)
        row.setMinimumHeight(42)
        h = QHBoxLayout(row)
        h.setContentsMargins(14, 6, 10, 6)
        h.setSpacing(6)
        if label:
            lbl = QLabel(label)
            lbl.setObjectName("RowLabel")
            h.addWidget(lbl)
        h.addStretch(1)
        for item in items:
            if isinstance(item, str):
                u = QLabel(item)
                u.setObjectName("Unit")
                h.addWidget(u)
                h.addSpacing(4)
            else:
                h.addWidget(item)
        self.lay.addWidget(row)
        return row


def spin(lo: int, hi: int, width: int, prefix: str = "", suffix: str = "") -> QSpinBox:
    sp = QSpinBox()
    sp.setRange(lo, hi)
    sp.setAlignment(Qt.AlignCenter)
    sp.setFixedSize(width, 28)
    sp.setPrefix(prefix)
    sp.setSuffix(suffix)
    sp.setAccelerated(True)
    return sp


def secondary(text: str) -> QPushButton:
    b = QPushButton(text)
    b.setObjectName("Secondary")
    b.setCursor(Qt.PointingHandCursor)
    b.setFixedHeight(28)
    return b


# --------------------------------------------------------------------------
# Position picker overlay
# --------------------------------------------------------------------------

class PickerOverlay(QWidget):
    picked = Signal(int, int)

    def __init__(self, screen):
        super().__init__(None, Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setCursor(Qt.CrossCursor)
        self.setMouseTracking(True)
        self._screen = screen
        self.setGeometry(screen.geometry())
        self._pos = QPoint(-1, -1)

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.fillRect(self.rect(), QColor(0, 0, 0, 110))
        accent = QColor(THEME["accent"])
        if self._pos.x() >= 0:
            x, y = self._pos.x(), self._pos.y()
            line = QColor(accent)
            line.setAlpha(170)
            p.setPen(QPen(line, 1))
            p.drawLine(0, y, self.width(), y)
            p.drawLine(x, 0, x, self.height())
            p.setPen(QPen(QColor(255, 255, 255, 230), 2))
            p.setBrush(Qt.NoBrush)
            p.drawEllipse(QPointF(x, y), 14, 14)
            p.setPen(Qt.NoPen)
            p.setBrush(accent)
            p.drawEllipse(QPointF(x, y), 4, 4)

        text = "Click anywhere to set the click point   ·   Esc to cancel"
        f = QFont(self.font())
        f.setPixelSize(14)
        f.setWeight(QFont.DemiBold)
        p.setFont(f)
        tw = p.fontMetrics().horizontalAdvance(text) + 40
        pill = QRectF((self.width() - tw) / 2, self.height() - 96, tw, 40)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(28, 28, 30, 225))
        p.drawRoundedRect(pill, 20, 20)
        p.setPen(QColor("#F5F5F7"))
        p.drawText(pill, Qt.AlignCenter, text)
        p.end()

    def mouseMoveEvent(self, ev):
        self._pos = ev.position().toPoint()
        self.update()

    def mousePressEvent(self, ev):
        g = self._screen.geometry()
        v = self._screen.virtualGeometry()
        pt = ev.position().toPoint()
        self.picked.emit(g.x() + pt.x() - v.x(), g.y() + pt.y() - v.y())

    def keyPressEvent(self, ev):
        if ev.key() == Qt.Key_Escape:
            self.picked.emit(-1, -1)


# --------------------------------------------------------------------------
# Main window
# --------------------------------------------------------------------------

class MainWindow(QWidget):
    def __init__(self, native_frame: bool = False):
        super().__init__()
        self.cfg = Settings.load()
        self.pointer: VirtualPointer | None = None
        self.engine: ClickEngine | None = None
        self.bridge = Bridge()
        self.listener: HotkeyListener | None = None
        self.overlays: list[PickerOverlay] = []
        self.native_frame = native_frame
        self.hotkey_ok = True

        self.setWindowTitle(APP_NAME)
        self.setWindowIcon(app_icon())
        if not native_frame:
            self.setWindowFlags(Qt.FramelessWindowHint)
            self.setAttribute(Qt.WA_TranslucentBackground)

        self._build_ui()
        self._wire()
        self._load_into_ui()
        self._build_tray()

        self.bridge.progress.connect(self._on_progress)
        self.bridge.stopped.connect(self._on_stopped)
        self.bridge.hotkey.connect(self.toggle)
        self.bridge.panic.connect(lambda: self.stop("panic"))
        self.bridge.captured.connect(self._on_captured)

        QTimer.singleShot(120, self._check_permissions)

    # -- construction ------------------------------------------------------

    def _build_ui(self):
        outer = QVBoxLayout(self)
        outer.setSizeConstraint(QLayout.SetFixedSize)
        if self.native_frame:
            outer.setContentsMargins(0, 0, 0, 0)
        else:
            outer.setContentsMargins(26, 18, 26, 36)     # room for the drop shadow

        self.root = QWidget()
        self.root.setObjectName("Root")
        self.root.setProperty("native", self.native_frame)
        self.root.setAttribute(Qt.WA_StyledBackground)
        self.root.setFixedWidth(460)
        outer.addWidget(self.root)

        if not self.native_frame:
            self.shadow = QGraphicsDropShadowEffect(blurRadius=44, xOffset=0, yOffset=12)
            self.root.setGraphicsEffect(self.shadow)
            self.on_theme_changed()

        col = QVBoxLayout(self.root)
        col.setContentsMargins(0, 0, 0, 0)
        col.setSpacing(0)
        if not self.native_frame:
            col.addWidget(self._title_bar())

        body = QVBoxLayout()
        body.setContentsMargins(18, 4 if not self.native_frame else 18, 18, 18)
        body.setSpacing(18)
        col.addLayout(body)

        body.addWidget(self._hero())
        body.addWidget(self._interval_group())
        body.addWidget(self._behaviour_group())
        body.addWidget(self._location_group())
        body.addWidget(self._controls_group())

        self.action_btn = QPushButton("Start Clicking")
        self.action_btn.setObjectName("Primary")
        self.action_btn.setFixedHeight(44)
        self.action_btn.setCursor(Qt.PointingHandCursor)
        body.addWidget(self.action_btn)

    def _title_bar(self) -> QWidget:
        bar = QWidget()
        bar.setFixedHeight(42)
        lay = QHBoxLayout(bar)
        lay.setContentsMargins(16, 0, 16, 0)
        self.lights = TrafficLights()
        lay.addWidget(self.lights)
        lay.addStretch(1)
        title = QLabel(APP_NAME)
        title.setObjectName("TitleText")
        lay.addWidget(title)
        lay.addStretch(1)
        lay.addSpacing(self.lights.width())
        bar.mousePressEvent = self._start_drag
        return bar

    def _hero(self) -> QWidget:
        hero = QFrame()
        hero.setObjectName("Hero")
        v = QVBoxLayout(hero)
        v.setContentsMargins(18, 14, 18, 14)
        v.setSpacing(8)

        top = QHBoxLayout()
        top.setSpacing(8)
        self.dot = QLabel()
        self.dot.setObjectName("Dot")
        self.dot.setFixedSize(8, 8)
        self.status = QLabel("Ready")
        self.status.setObjectName("StatusText")
        self.hint = QLabel()
        self.hint.setObjectName("HeroHint")
        top.addWidget(self.dot, 0, Qt.AlignVCenter)
        top.addWidget(self.status)
        top.addStretch(1)
        top.addWidget(self.hint)
        v.addLayout(top)

        nums = QHBoxLayout()
        nums.setSpacing(32)

        def metric(value: str, unit: str, name="HeroValue", align=Qt.AlignLeft):
            c = QVBoxLayout()
            c.setSpacing(0)
            val = QLabel(value)
            val.setObjectName(name)
            val.setAlignment(align)
            u = QLabel(unit)
            u.setObjectName("HeroUnit")
            u.setAlignment(align)
            c.addWidget(val)
            c.addWidget(u)
            nums.addLayout(c)
            return val, u

        self.metric_clicks, _ = metric("0", "clicks")
        self.metric_rate, _ = metric("0.0", "per second")
        nums.addStretch(1)
        self.target_value, self.target_unit = metric("", "", "HeroValueMuted", Qt.AlignRight)
        v.addLayout(nums)
        return hero

    def _group(self, title: str, card: Card, footnote: str = "") -> QWidget:
        w = QWidget()
        v = QVBoxLayout(w)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(6)
        t = QLabel(title)
        t.setObjectName("GroupTitle")
        v.addWidget(t)
        v.addWidget(card)
        if footnote:
            note = QLabel(footnote)
            note.setObjectName("Footnote")
            note.setWordWrap(True)
            v.addWidget(note)
            w.footnote = note
        return w

    def _interval_group(self) -> QWidget:
        card = Card()
        self.sp_h = spin(0, 99, 44)
        self.sp_m = spin(0, 59, 44)
        self.sp_s = spin(0, 59, 44)
        self.sp_ms = spin(0, 999, 56)
        card.add_row("Every", self.sp_h, "h", self.sp_m, "m", self.sp_s, "s", self.sp_ms, "ms")
        self.sp_jitter = spin(0, 90, 64, suffix=" %")
        self.sp_jitter.setToolTip("Vary each interval randomly by up to this much")
        card.add_row("Randomize by", self.sp_jitter)
        return self._group("Interval", card)

    def _behaviour_group(self) -> QWidget:
        card = Card()
        self.seg_button = Segmented([("Left", "Left"), ("Middle", "Middle"), ("Right", "Right")])
        card.add_row("Mouse button", self.seg_button)
        self.seg_type = Segmented([("Single", "Single"), ("Double", "Double"), ("Triple", "Triple")])
        card.add_row("Click type", self.seg_type)
        self.seg_repeat = Segmented([("forever", "Until stopped"), ("count", "Fixed count")])
        card.add_row("Repeat", self.seg_repeat)
        self.sp_count = spin(1, 10_000_000, 96)
        self.count_row = card.add_row("Stop after", self.sp_count, "clicks")
        return self._group("Clicking", card)

    def _location_group(self) -> QWidget:
        card = Card()
        self.seg_pos = Segmented([("cursor", "Cursor"), ("fixed", "Fixed point")])
        card.add_row("Click at", self.seg_pos)
        self.sp_x = spin(0, 100000, 76, prefix="X  ")
        self.sp_y = spin(0, 100000, 76, prefix="Y  ")
        self.pick_btn = secondary("Pick…")
        self.test_btn = secondary("Test")
        self.test_btn.setToolTip("Move the pointer to this point")
        self.fixed_row = card.add_row("Point", self.sp_x, self.sp_y, self.pick_btn, self.test_btn)
        group = self._group(
            "Location", card,
            "Fixed points drive a virtual absolute pointer. Press Test once to confirm "
            "your compositor honours it before relying on it.")
        self.pos_note = group.footnote
        return group

    def _controls_group(self) -> QWidget:
        card = Card()
        self.hotkey_btn = QPushButton("F6")
        self.hotkey_btn.setObjectName("KeyCap")
        self.hotkey_btn.setFixedHeight(28)
        self.hotkey_btn.setCursor(Qt.PointingHandCursor)
        self.hotkey_btn.setToolTip("Click, then press a new key combination")
        card.add_row("Start / stop shortcut", self.hotkey_btn)
        self.cb_tray = Switch()
        card.add_row("Hide window while clicking", self.cb_tray)
        self.selftest_btn = secondary("Run Test")
        self.selftest_btn.setToolTip("Traces a small square with your cursor")
        card.add_row("Pointer self-test", self.selftest_btn)
        return self._group(
            "Controls", card,
            "Ctrl + Alt + Esc always stops clicking, whatever the shortcut is set to.")

    def _build_tray(self):
        self.tray = QSystemTrayIcon(app_icon(), self)
        self.tray.setToolTip(APP_NAME)
        menu = QMenu()
        self.act_toggle = QAction("Start Clicking", self)
        self.act_toggle.triggered.connect(self.toggle)
        act_show = QAction(f"Show {APP_NAME}", self)
        act_show.triggered.connect(self._restore)
        act_quit = QAction(f"Quit {APP_NAME}", self)
        act_quit.triggered.connect(self._quit)
        menu.addAction(self.act_toggle)
        menu.addSeparator()
        menu.addAction(act_show)
        menu.addAction(act_quit)
        self.tray_menu = menu
        self.tray.setContextMenu(menu)
        self.tray.activated.connect(
            lambda r: self._restore() if r == QSystemTrayIcon.Trigger else None)
        if QSystemTrayIcon.isSystemTrayAvailable():
            self.tray.show()

    # -- wiring ------------------------------------------------------------

    def _wire(self):
        self.action_btn.clicked.connect(self.toggle)
        self.pick_btn.clicked.connect(self._pick_position)
        self.test_btn.clicked.connect(self._test_move)
        self.hotkey_btn.clicked.connect(self._capture_hotkey)
        self.selftest_btn.clicked.connect(self._self_test)
        self.seg_pos.changed.connect(lambda _: self._sync_enabled())
        self.seg_repeat.changed.connect(lambda _: self._sync_enabled())
        for sp in (self.sp_h, self.sp_m, self.sp_s, self.sp_ms, self.sp_jitter):
            sp.valueChanged.connect(self._update_rate_hint)
        if not self.native_frame:
            self.lights.min_btn.clicked.connect(self.showMinimized)
            self.lights.close_btn.clicked.connect(self.close)

    def _start_drag(self, ev):
        if ev.button() == Qt.LeftButton and self.windowHandle():
            self.windowHandle().startSystemMove()

    def on_theme_changed(self):
        if not self.native_frame:
            self.shadow.setColor(QColor(0, 0, 0, int(THEME["shadow"])))

    def changeEvent(self, ev):
        # Traffic lights grey out when the window loses focus, as on macOS.
        if ev.type() == QEvent.ActivationChange and not self.native_frame:
            for b in self.lights.findChildren(TrafficLight):
                b.update()
        super().changeEvent(ev)

    def _load_into_ui(self):
        c = self.cfg
        self.sp_h.setValue(c.hours)
        self.sp_m.setValue(c.minutes)
        self.sp_s.setValue(c.seconds)
        self.sp_ms.setValue(c.millis)
        self.sp_jitter.setValue(c.jitter_pct)
        self.seg_button.set_value(c.button)
        self.seg_type.set_value(c.click_type)
        self.seg_repeat.set_value(c.repeat_mode)
        self.sp_count.setValue(c.repeat_count)
        self.seg_pos.set_value(c.position_mode)
        self.sp_x.setValue(c.pos_x)
        self.sp_y.setValue(c.pos_y)
        self.cb_tray.setChecked(c.minimize_to_tray)
        self.cb_tray.snap()
        self.hotkey_btn.setText(pretty_hotkey(c.hotkey_key, c.hotkey_mods))
        self._sync_enabled()
        self._update_rate_hint()
        self._set_status("Ready")

    def _harvest(self):
        c = self.cfg
        c.hours, c.minutes = self.sp_h.value(), self.sp_m.value()
        c.seconds, c.millis = self.sp_s.value(), self.sp_ms.value()
        c.jitter_pct = self.sp_jitter.value()
        c.button = self.seg_button.value()
        c.click_type = self.seg_type.value()
        c.repeat_mode = self.seg_repeat.value()
        c.repeat_count = self.sp_count.value()
        c.position_mode = self.seg_pos.value()
        c.pos_x, c.pos_y = self.sp_x.value(), self.sp_y.value()
        c.minimize_to_tray = self.cb_tray.isChecked()

    def _sync_enabled(self):
        fixed = self.seg_pos.value() == "fixed"
        self.fixed_row.setVisible(fixed)
        self.pos_note.setVisible(fixed)
        self.count_row.setVisible(self.seg_repeat.value() == "count")

    def _update_rate_hint(self):
        total = (self.sp_h.value() * 3600 + self.sp_m.value() * 60 +
                 self.sp_s.value() + self.sp_ms.value() / 1000.0)
        if total <= 0:
            self.target_value.setText("—")
            self.target_unit.setText("set an interval")
        elif total < 1:
            self.target_value.setText(f"{1 / total:.0f}")
            self.target_unit.setText("target per second")
        else:
            self.target_value.setText(f"{total:g} s")
            self.target_unit.setText("between clicks")

    def _set_status(self, text: str, state: str = "idle"):
        self.status.setText(text)
        self.dot.setProperty("state", state)
        repolish(self.dot)
        running = state == "running"
        if not self.hotkey_ok:
            self.hint.setText("Use the button below to start")
        else:
            self.hint.setText(f"{self.hotkey_btn.text()} to {'stop' if running else 'start'}")

    # -- permissions -------------------------------------------------------

    def _check_permissions(self):
        if evdev is None:
            self._blocker("python-evdev is missing",
                          "The kernel input backend could not load.\n\n"
                          f"{EVDEV_ERROR}\n\nInstall it with:  sudo pacman -S python-evdev")
            return
        state = uinput_state()
        if state != "ok":
            msg = ("/dev/uinput does not exist. The uinput kernel module is not loaded."
                   if state == "nomodule" else
                   "You do not have write access to /dev/uinput.")
            self._blocker("Input access is not set up", msg +
                          "\n\nRun the installer's permission step, then log out and back in:\n\n"
                          "  sudo ./install.sh --permissions-only\n\n"
                          "It loads the uinput module, adds a udev rule, and puts you in the "
                          "input group.")
            return
        if not input_devices_readable():
            self.hotkey_ok = False
            self.hotkey_btn.setEnabled(False)
            self.hotkey_btn.setText("Unavailable")
            self._set_status("Shortcut off — no read access to /dev/input", "warn")
        else:
            self.listener = HotkeyListener(self.bridge, self.cfg)
            self.listener.start()

    def _blocker(self, title, text):
        self.action_btn.setEnabled(False)
        self._set_status(title, "error")
        box = QMessageBox(self)
        box.setWindowTitle(title)
        box.setText(title)
        box.setInformativeText(text)
        box.setIconPixmap(app_icon().pixmap(56, 56))
        box.exec()

    def _ensure_pointer(self) -> bool:
        if self.pointer is not None:
            return True
        vg = QGuiApplication.primaryScreen().virtualGeometry()
        try:
            self.pointer = VirtualPointer((vg.width(), vg.height()))
        except BackendError as exc:
            self._blocker("Could not create the virtual pointer", str(exc))
            return False
        return True

    # -- position picking --------------------------------------------------

    def _pick_position(self):
        self.hide()
        QTimer.singleShot(180, self._show_overlays)

    def _show_overlays(self):
        self.overlays = []
        for screen in QGuiApplication.screens():
            ov = PickerOverlay(screen)
            ov.picked.connect(self._on_picked)
            ov.showFullScreen()
            ov.windowHandle().setScreen(screen)
            ov.setGeometry(screen.geometry())
            self.overlays.append(ov)
        if self.overlays:
            self.overlays[0].activateWindow()

    def _on_picked(self, x, y):
        for ov in self.overlays:
            ov.close()
        self.overlays = []
        if x >= 0:
            self.sp_x.setValue(x)
            self.sp_y.setValue(y)
        self.show()
        self.raise_()

    def _test_move(self):
        if not self._ensure_pointer():
            return
        if not self.pointer.move_to(self.sp_x.value(), self.sp_y.value()):
            self._set_status("Absolute positioning isn't available here", "warn")

    def _self_test(self):
        if not self._ensure_pointer():
            return
        self.selftest_btn.setEnabled(False)
        self._set_status("Watch your cursor — it should trace a square", "warn")
        steps = []
        for dx, dy in ((26, 0), (0, 26), (-26, 0), (0, -26)):
            steps.extend([(dx, dy)] * 12)

        def step(i=0):
            if i >= len(steps):
                self.selftest_btn.setEnabled(True)
                self._set_status("Pointer moved — the click backend works", "ok")
                return
            dx, dy = steps[i]
            self.pointer.move_relative(dx, dy)
            QTimer.singleShot(16, lambda: step(i + 1))

        QTimer.singleShot(150, step)

    # -- hotkey capture ----------------------------------------------------

    def _capture_hotkey(self):
        if self.listener is None:
            return
        self.listener.capturing = True
        self.hotkey_btn.setText("Press keys…")
        self.hotkey_btn.setProperty("capturing", True)
        repolish(self.hotkey_btn)

    def _on_captured(self, key, mods):
        self.cfg.hotkey_key = key
        self.cfg.hotkey_mods = list(mods)
        self.hotkey_btn.setText(pretty_hotkey(key, mods))
        self.hotkey_btn.setProperty("capturing", False)
        repolish(self.hotkey_btn)
        self.cfg.save()
        self._set_status(self.status.text(), self.dot.property("state") or "idle")

    # -- run control -------------------------------------------------------

    def toggle(self):
        self.stop("stopped") if self.engine and self.engine.is_alive() else self.start()

    def start(self):
        self._harvest()
        c = self.cfg
        if c.hours + c.minutes + c.seconds + c.millis == 0:
            self._set_status("Set an interval above zero first", "warn")
            return
        if not self._ensure_pointer():
            return
        c.save()

        self.engine = ClickEngine(self.pointer, c, self.bridge)
        self.engine.start()

        self.action_btn.setText("Stop Clicking")
        self.action_btn.setObjectName("Danger")
        repolish(self.action_btn)
        self.act_toggle.setText("Stop Clicking")
        self._set_status("Clicking", "running")
        self.metric_clicks.setText("0")
        self.metric_rate.setText("0.0")
        self.tray.setToolTip(f"{APP_NAME} — running")
        if c.minimize_to_tray:
            self.hide()

    def stop(self, reason="stopped"):
        if self.engine:
            self.engine.stop()
        if reason == "panic":
            self._restore()

    def _on_progress(self, count, rate):
        self.metric_clicks.setText(f"{count:,}")
        self.metric_rate.setText(f"{rate:.1f}")

    def _on_stopped(self, reason):
        self._reset_button()
        self.engine = None
        if reason.startswith("error:"):
            self._set_status(reason[6:], "error")
            self._restore()
            return
        self._set_status("Finished" if reason == "finished" else "Ready")
        self.tray.setToolTip(APP_NAME)

    def _reset_button(self):
        self.action_btn.setText("Start Clicking")
        self.action_btn.setObjectName("Primary")
        repolish(self.action_btn)
        self.act_toggle.setText("Start Clicking")

    def _restore(self):
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def _quit(self):
        self.stop()
        if self.listener:
            self.listener.stop()
        if self.pointer:
            self.pointer.close()
        self._harvest()
        self.cfg.save()
        QApplication.quit()

    def closeEvent(self, ev):
        if (self.engine and self.engine.is_alive() and self.cb_tray.isChecked()
                and QSystemTrayIcon.isSystemTrayAvailable()):
            ev.ignore()
            self.hide()
            return
        self._quit()
        ev.accept()


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------

USAGE = f"""{APP_NAME} {VERSION} — a precision auto clicker for Linux

Usage: lazyautoclicker [options]

  --native-frame        use the compositor's titlebar instead of the built-in one
  --theme light|dark    force a colour scheme (default: follow the system)
  --version             print the version and exit
  --help                show this help
"""


def arg_value(name: str) -> str | None:
    for i, a in enumerate(sys.argv):
        if a == name and i + 1 < len(sys.argv):
            return sys.argv[i + 1]
        if a.startswith(name + "="):
            return a.split("=", 1)[1]
    return None


def build_app() -> tuple[QApplication, MainWindow]:
    QGuiApplication.setDesktopFileName(APP_ID)
    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setApplicationVersion(VERSION)
    app.setWindowIcon(app_icon())
    app.setQuitOnLastWindowClosed(False)
    app.setStyle("Fusion")

    families = QFontDatabase.families()
    for family in ("Inter", "Inter Variable", "SF Pro Text", "Cantarell", "Noto Sans", "DejaVu Sans"):
        if family in families:
            font = QFont(family)
            font.setPixelSize(13)
            font.setHintingPreference(QFont.PreferNoHinting)
            try:
                font.setFeature(QFont.Tag("tnum"), 1)   # steady, non-jumping digits
            except (AttributeError, TypeError):
                pass
            app.setFont(font)
            break

    forced = arg_value("--theme")
    apply_theme(app, forced if forced in THEMES else system_theme())
    win = MainWindow(native_frame="--native-frame" in sys.argv)

    if forced not in THEMES:
        def follow_system(*_):
            apply_theme(app, system_theme())
            win.on_theme_changed()
        app.styleHints().colorSchemeChanged.connect(follow_system)
    return app, win


def main():
    if "--help" in sys.argv or "-h" in sys.argv:
        print(USAGE, end="")
        sys.exit(0)
    if "--version" in sys.argv:
        print(f"{APP_NAME} {VERSION}")
        sys.exit(0)
    if "--check-imports" in sys.argv:
        import PySide6
        print(f"imports ok: PySide6 {PySide6.__version__}, evdev present={evdev is not None}")
        sys.exit(0 if evdev is not None else 1)

    app, win = build_app()
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
