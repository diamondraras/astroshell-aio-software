"""Persistent settings, stored in the same INI layout as the original baseInfo.cfg.

Observed keys (from the original install's baseInfo.cfg):
  [disk] type, [gpuType] type, [network] type, [brightness] value,
  [language] type, [rotate] value, [temp] uint, [autoRun] state, [theme] name
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

from PySide6.QtCore import QSettings

DEFAULTS = {
    "disk/type": 0,
    "gpuType/type": 0,
    "network/type": 0,
    "brightness/value": 100,
    "language/type": 0,      # 0 English, 1 Chinese, 2 French, 3 Japanese, 4 Korean, 5 Portuguese
    "rotate/value": 0,       # degrees: 0/90/180/270
    "temp/uint": 0,          # 0 = Celsius, 1 = Fahrenheit (key spelled "uint" in the original)
    "autoRun/state": True,
    "theme/name": "T00",
    # additions of this rebuild (not in the original baseInfo.cfg)
    "display/mode": 1,       # 0 = theme, 1 = system dashboard, 2 = screen capture
    "dashboard/hour24": True,
    "ui/trayHintShown": False,
}

MODE_THEME, MODE_DASHBOARD, MODE_CAPTURE = 0, 1, 2

LANGUAGES = ["English", "Chinese", "French", "Japanese", "Korean", "Portuguese"]


def app_dir() -> Path:
    """Directory holding the program (or the frozen executable)."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def resource_dir() -> Path:
    """Read-only bundled resources (stock themes, translations)."""
    return Path(getattr(sys, "_MEIPASS", app_dir()))


def themes_dir() -> Path:
    """Writable theme library; stock themes are copied in on first run."""
    stock = resource_dir() / "themes"
    if stock.is_dir() and os.access(stock, os.W_OK) and not os.environ.get("SMARTMONITOR_HOME"):
        return stock  # portable install: edit themes in place, no duplicate copy
    d = data_dir() / "themes"
    if not d.exists():
        d.mkdir(parents=True)
        if stock.exists() and stock.resolve() != d.resolve():
            import shutil
            for t in stock.iterdir():
                if t.is_dir():
                    shutil.copytree(t, d / t.name, dirs_exist_ok=True)
    return d


def data_dir() -> Path:
    """Writable directory for themes and settings.

    Uses the program directory when writable (like the original), otherwise a
    per-user location (~/.local/share/smartmonitor or %APPDATA%/smartmonitor).
    """
    env = os.environ.get("SMARTMONITOR_HOME")
    if env:
        p = Path(env)
    elif os.access(app_dir(), os.W_OK):
        p = app_dir()
    elif sys.platform == "win32":
        p = Path(os.environ.get("APPDATA", Path.home())) / "smartmonitor"
    else:
        p = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share")) / "smartmonitor"
    p.mkdir(parents=True, exist_ok=True)
    return p


class Config:
    def __init__(self, path: Path | None = None):
        self.path = path or data_dir() / "baseInfo.cfg"
        self._s = QSettings(str(self.path), QSettings.IniFormat)
        for k, v in DEFAULTS.items():
            if not self._s.contains(k):
                self._s.setValue(k, v)
        self._s.sync()

    def get(self, key: str):
        default = DEFAULTS.get(key)
        v = self._s.value(key, default)
        if isinstance(default, bool):
            return v if isinstance(v, bool) else str(v).lower() == "true"
        if isinstance(default, int):
            try:
                return int(v)
            except (TypeError, ValueError):
                return default
        return v

    def set(self, key: str, value) -> None:
        self._s.setValue(key, value)
        self._s.sync()


def set_autorun(enabled: bool) -> None:
    """Start with the session. Windows: HKCU Run key (as the original); Linux: XDG autostart."""
    exe = sys.executable if getattr(sys, "frozen", False) else f'"{sys.executable}" -m smartmonitor --minimized'
    if sys.platform == "win32":
        s = QSettings(r"HKEY_CURRENT_USER\Software\Microsoft\Windows\CurrentVersion\Run", QSettings.NativeFormat)
        if enabled:
            s.setValue("SmartMonitor", exe if not getattr(sys, "frozen", False) else f'"{exe}" --minimized')
        else:
            s.remove("SmartMonitor")
        return
    cfg_home = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    desktop = cfg_home / "autostart" / "smartmonitor.desktop"
    unit = cfg_home / "systemd" / "user" / "smartmonitor.service"
    cmd = f"{exe} --minimized" if getattr(sys, "frozen", False) else exe
    if enabled:
        # systemd user service: starts at boot (with `loginctl enable-linger`) or at login, restarts on
        # failure. Without a display server it falls back to the offscreen Qt platform, so the USB
        # display works on a headless host. The XDG autostart entry is removed to avoid a second instance.
        unit.parent.mkdir(parents=True, exist_ok=True)
        unit.write_text(
            "[Unit]\nDescription=SmartMonitor USB display\nAfter=graphical-session.target\n\n"
            "[Service]\nType=simple\nWorkingDirectory=%s\n"
            "ExecStart=/bin/sh -c 'if [ -z \"$DISPLAY$WAYLAND_DISPLAY\" ]; then export QT_QPA_PLATFORM=offscreen; fi; exec %s'\n"
            "Restart=on-failure\nRestartSec=5\n\n[Install]\nWantedBy=default.target\n" % (app_dir(), cmd))
        if desktop.exists():
            desktop.unlink()
        _systemctl("daemon-reload")
        _systemctl("enable", "smartmonitor.service")
    else:
        _systemctl("disable", "smartmonitor.service")
        for f in (unit, desktop):
            if f.exists():
                f.unlink()
        _systemctl("daemon-reload")


def _systemctl(*args: str) -> None:
    import subprocess
    try:
        subprocess.run(["systemctl", "--user", *args], check=False, timeout=15, capture_output=True)
    except (OSError, subprocess.SubprocessError):
        pass
