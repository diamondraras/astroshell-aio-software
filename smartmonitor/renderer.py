"""Off-screen renderer feeding the device (one ThemeScene, refreshed every frame)."""
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
        self._last_values = 0.0
        self._values: dict = {}

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
        return True

    def reload(self):
        if self.theme_name:
            self.load(self.theme_name)

    def render(self) -> QImage:
        now = time.monotonic()
        if now - self._last_values >= 0.5:  # sensors change slowly; text relayout is costly
            self._values = self.sensors.snapshot()
            self._last_values = now
        self.scene.update_values(self._values, fahrenheit=self.cfg.get("temp/uint") == 1)
        return self.scene.render_image()

    def close(self):
        self.scene.close()
