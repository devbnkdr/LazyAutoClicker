# Changelog

## 1.2.0 — 2026-10-10

- Now called **Lazy Auto Clicker**. The command is `lazyautoclicker` and settings
  live in `~/.config/lazyautoclicker/`.
- The app's own virtual devices are always skipped when looking for keyboards,
  whatever the app is called.
- Fixed the interval and randomize fields showing up empty with Qt 6.12.

## 1.1.0 — 2026-10-01

### New look
- Redesigned interface: grouped settings cards, segmented controls, an animated
  switch, and a live dashboard showing clicks, actual rate and target rate.
- Follows the system light/dark appearance and switches live. Force one with
  `--theme light` or `--theme dark`.
- Window controls in the title bar; they dim when the window is unfocused.
- New app icon.
- Options that don't apply stay out of the way: the click-count field and the
  fixed-point controls only appear when selected.
- Refined position-picker overlay with a target marker.

### Other
- `--version` and `--help` flags.
- `build.sh` now also builds on non-Arch distributions, pulling dependencies
  from PyPI.
- `install.sh` works from an unpacked release archive as well as a source checkout.
- Closing the window no longer hides it when no system tray is available to bring
  it back.

## 1.0.2

- First public build: uinput click backend, `/dev/input` global hotkey, fixed-point
  clicking, tray icon, persistent settings.
