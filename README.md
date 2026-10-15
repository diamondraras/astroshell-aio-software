# SmartMonitor (clean re-implementation)

A cross-platform (Windows + Linux) re-implementation of **SmartMonitor28 A3** (vendor: www.gz-haiji.com),
the companion app for the 2.8" USB "smart monitor" display. It was rebuilt from scratch using
reverse-engineering evidence gathered with REA/Ghidra from the shipped `SmartMonitor28 A3.exe`
(Qt 5.12.12, x64, SHA-256 `769c4d72…174665`). No original code was reused.

| Original (Windows only)               | This rebuild                                         |
|---------------------------------------|------------------------------------------------------|
| C++ / Qt 5.12, MSVC                   | Python 3.10+ / PySide6 (Qt 6), no compiler needed    |
| QtUsb + libusb (WinUSB)               | pyusb + libusb (bundled via `libusb-package`)        |
| FFmpeg 6 + libopenh264                | PyAV (bundled FFmpeg) + libx264, baseline profile    |
| HWiNFO64.dll (proprietary, Windows)   | psutil, Linux hwmon/sysfs, NVML, HWiNFO shared mem   |
| `.theme` files (QDataStream)          | **Same format, byte-for-byte compatible**            |
| `zh_*.qm` translations                | **Same files reused**, same translation contexts     |

## Features
- Live rendering of themes to the 320×320 display at 25 fps, H.264-encoded over USB bulk.
- Home: live preview, local theme library with thumbnails, import/export/delete themes,
  replace background image/video/GIF and mask, edit sensor slots S1–S6 (sensor, name, unit),
  brightness, rotation (0/90/180/270), desktop screen-capture mode.
- Theme editor: Text, Number (sensor), Image/GIF/Video, progress Bar, Arc, Gauge, analog Clock,
  Date, Time, Weekday. Move with the mouse, edit position/size/colour/font/sensor, raise/lower,
  delete, Save / Save As. Saved themes open in the original app as well.
- Settings: language (English, Chinese, French, Japanese, Korean, Portuguese), auto-run (Windows registry Run key; Linux systemd user service, started at boot when lingering is
  enabled: `loginctl enable-linger $USER`), °C/°F, network interface, disk, device
  version, firmware upgrade (`.bin`), reboot, export of sensor readings.
- System tray (with a display-mode menu), single instance, start minimized with `--minimized`.

### Additions beyond the original
- **System dashboard** (default display mode, `smartmonitor/dashboard.py`). It shows:
  - an analog clock with the digital time, date and computer uptime in hours;
  - CPU and GPU usage % and temperature in °C, each with a 1-minute graph;
  - network download/upload speed with a 1-minute graph, data used since boot, and an online
    indicator that switches to a crossed-globe "No internet" logo when offline.
  - disk space as a pie chart (used / free, percentage in the centre; amber, orange above 75 %, red above
    90 %) for the disk chosen in Settings (default `/`);
  - Online state: a TCP handshake to 1.1.1.1:443, 8.8.8.8:53 or 9.9.9.9:443 every 10 s. No data
    is sent.
  - Network interface: the one holding the default route. It is found with a UDP `connect`, which
    sends no packet.
- **Display modes:** Dashboard, Theme or Screen mirror. Switch from the Home page or the tray menu.
- **Per-widget rotation** in the editor: −180…180° from the property panel, ±90° from the toolbar
  or with Ctrl+L / Ctrl+R. Rotations are stored in `extra.json` next to the `.theme` file, so the
  `.theme` stays byte-identical and still opens in the original app (unrotated).
- **Editor shortcuts:** Ctrl+S, Ctrl+Shift+S, Ctrl+N, Ctrl+O, Del, arrows (Shift = 10 px).
- **Lower resource use:**
  - Frames are re-rendered only when their content changes.
  - Static content is sent to the display at a 2 fps keep-alive.
  - The full 25 fps is used only for video/GIF themes and screen mirroring.
  - Pages are built on first use, and a `--minimized` autostart builds no UI at all.
  - Heavy imports (PyAV, numpy) are preloaded in the background.
- **CPU temperature on Windows** has no unprivileged source. The app reads it from HWiNFO
  (Settings → Shared Memory Support) or from LibreHardwareMonitor (Options → Remote Web Server,
  port 8085), whichever is running.

## Run from source

### Windows
```powershell
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
.venv\Scripts\python -m smartmonitor
```
The display must be bound to the **WinUSB** driver (the original app also uses libusb/WinUSB).
If it is not detected, install WinUSB for device `1F3A:0007` with [Zadig](https://zadig.akeo.ie/).

### Linux (Debian/Ubuntu example)
```sh
sudo apt install python3-venv libxcb-cursor0 libegl1      # Qt runtime deps
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
# allow non-root access to the display:
sudo cp packaging/99-smartmonitor.rules /etc/udev/rules.d/
sudo udevadm control --reload && sudo udevadm trigger
python -m smartmonitor
```

## Build a standalone executable
- Windows: `powershell -ExecutionPolicy Bypass -File build.ps1` → `dist\SmartMonitor\SmartMonitor.exe`
- Linux: `./build.sh` → `dist/smartmonitor/smartmonitor`

PyInstaller does not cross-compile: build on the OS you target.

## Tests
```sh
python -m pytest -q tests      # 61 tests: protocol, replies, 39 theme round-trips, dashboard, rotation
python smartmonitor/theme_format.py verify themes   # parse + re-serialize every stock theme
```

## Layout
```
smartmonitor/
  __main__.py      entry point, single instance, translations
  app.py           main window, tray, frame pipeline (render → rotate → encode → USB)
  transport.py     USB protocol + H.264 encoder           (docs/PROTOCOL.md)
  theme_format.py  .theme reader/writer                   (docs/THEME_FORMAT.md)
  scene.py         theme → QGraphicsScene items (shared by renderer and editor)
  renderer.py      off-screen rendering for the device
  media.py         image / GIF / video backgrounds
  sensors.py       cross-platform hardware sensors
  home_page.py, editor_page.py, settings_page.py, config.py
themes/            the 39 stock themes copied from the original install
translations/      the original zh_*.qm files
docs/              reverse-engineering notes with evidence
```

## Sources / evidence
All protocol and file-format facts come from static analysis of the original install in
`C:\Program Files (x86)\SmartMonitor28 A3`, with function addresses cited:
- **`docs/PROTOCOL.md`**: USB IDs, endpoints, packet framing, commands 0x80–0x90, replies, encoder
  settings, firmware upgrade, HWiNFO ordinals and sensor mapping.
- **`docs/THEME_FORMAT.md`**: the record layout for all 12 widget types, verified by a byte-exact
  round-trip of all 39 stock themes.
- Other sources: strings in the executable (UI text, date/time formats), the `.qm` translation
  catalogues (UI contexts and strings), `baseInfo.cfg` (settings keys), the Inno Setup script
  (publisher, `.myp` association), and `Qt5Core.dll` (Qt 5.12.12).

## Known differences and unknowns (not guessed silently)
- **Not tested against real hardware.** No display was connected during development. The protocol
  is reconstructed from decompiled code only.
- **H.264 encoder:** the original uses OpenH264, while PyAV wheels ship libx264. This rebuild
  uses libx264 in Constrained Baseline profile with no B-frames, and falls back to libopenh264 or
  the hardware encoders when they are available.
- **CPU temperature on Windows** needs HWiNFO running with "Shared Memory Support" enabled. The
  original bundled a licensed HWiNFO DLL, which cannot be redistributed. On Linux it is read
  from hwmon (`coretemp`/`k10temp`).
- **Unconfirmed theme fields.** The meaning of the progress-bar, arc and clock style fields, and
  of a few scene flags, is unconfirmed. They are preserved byte-for-byte and rendered with
  best-effort interpretations (see THEME_FORMAT.md). The carousel option is not implemented.
- **Sensor ids 7–10** (RAM, disk, download, upload) are extensions of this rebuild. The original
  only knows ids 1–6.
- **Device replies:** the payloads of replies 0x81 and 0x90 are only partly known. As in the
  original, the screen size is fixed at 320×320.
