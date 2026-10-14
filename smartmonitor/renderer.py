"""Off-screen renderer feeding the device (one ThemeScene).

A static theme is redrawn only when the displayed second or the sensor data changes, which
avoids repainting identical frames 25 times per second. Video/GIF/screen-capture themes are
redrawn on every call.
"""
from __future__ import annotations

import logging
import time
from pathlib import Path

from PySide6.QtGui import QImage

from .config import themes_dir
from .scene import ThemeScene

log = logging.getLogger(__name__)


class ThemeRenderer:
    def __init__(self, sensors, cfg, size=(320, 320)):
        self.sensors = sensors
        self.cfg = cfg
        self.scene = ThemeScene(size)
        self.scene.clear_theme()
        self.theme_name: str | None = None
        self.frame_id = 0
        self._key = None
        self._image: QImage | None = None

    @property
    def animated(self) -> bool:
        return self.scene.animated

    def theme_path(self, name: str) -> Path:
        return themes_dir() / f"theme_{name}"

    def load(self, name: str) -> bool:
        path = self.theme_path(name)
        try:
            self.scene.load(path)
        except Exception as e:
            log.warning("can't load theme %s: %s", name, e)
            self.scene.clear_theme()
            return False
        self.theme_name = name
        self.cfg.set("theme/name", name)
        self.invalidate()
        return True

    def reload(self):
        if self.theme_name:
            self.load(self.theme_name)

    def invalidate(self):
        """Force a redraw on the next render() (call after editing the scene)."""
        self._key = None

    def render(self) -> QImage:
        fahrenheit = self.cfg.get("temp/uint") == 1
        key = (int(time.time()), self.sensors.version, fahrenheit)
        if self.scene.animated or key != self._key or self._image is None:
            self._key = key
            self.scene.update_values(self.sensors.snapshot(), fahrenheit=fahrenheit)
            self._image = self.scene.render_image()
            self.frame_id += 1
        return self._image

    def close(self):
        self.scene.close()
