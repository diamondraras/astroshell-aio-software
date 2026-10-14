"""Main window (original: DispMainWIndow) and the frame pipeline."""
from __future__ import annotations

import logging

import numpy as np
from PySide6.QtCore import QCoreApplication, QObject, Qt, QTimer, Signal
from PySide6.QtGui import QAction, QColor, QGuiApplication, QIcon, QImage, QPainter, QPixmap, QTransform
from PySide6.QtWidgets import (QButtonGroup, QHBoxLayout, QLabel, QMainWindow, QMenu, QStackedWidget,
                               QSystemTrayIcon, QToolButton, QVBoxLayout, QWidget)

from .editor_page import EditorPage
from .home_page import HomePage
from .renderer import ThemeRenderer
from .sensors import SensorManager
from .settings_page import SettingsPage
from .transport import SCREEN_H, SCREEN_W, DeviceManager

log = logging.getLogger(__name__)

FRAME_INTERVAL_MS = 40  # 25 fps, matching the original encoder's frame rate

STYLE = """
QWidget { background-color: #030303; color: white; font-family: Arial; font-size: 14px; }
QWidget#cont_1, QGroupBox { background-color: #1d1d1d; border-radius: 8px; }
QGroupBox { border: none; margin-top: 4px; padding: 8px; }
QToolButton#nav { border: none; background-color: #1d1d1d; min-width: 110px; min-height: 60px; font-size: 16px; color: white; }
QToolButton#nav:checked { color: #b39654; background-color: #1d1d1d; border-left: 3px solid #b49653; }
QPushButton, QToolButton { background-color: #2a2a2a; border: 1px solid #444; border-radius: 5px; padding: 4px 10px; }
QPushButton:pressed, QToolButton:pressed, QPushButton:checked, QToolButton:checked { background-color: #b49552; color: black; }
QComboBox, QSpinBox, QDoubleSpinBox, QLineEdit, QPlainTextEdit {
    background: #111; border: 1px solid gray; border-radius: 5px; padding: 2px 6px; color: white; }
QListWidget { background-color: black; border-radius: 10px; outline: 0px; }
QListWidget::item:selected { background-color: #b49653; color: black; }
QSlider::groove:horizontal { height: 6px; background: #444; border-radius: 3px; }
QSlider::handle:horizontal { background: #b49653; width: 14px; margin: -5px 0; border-radius: 7px; }
"""


class _Bridge(QObject):
    """Marshals transport callbacks (worker threads) onto the GUI thread."""
    state = Signal(bool)
    version = Signal(str)
    firmware = Signal(bool, bool, str)


def qimage_to_rgba(img: QImage) -> np.ndarray:
    img = img.convertToFormat(QImage.Format_RGBA8888)
    w, h = img.width(), img.height()
    arr = np.frombuffer(img.constBits(), np.uint8, img.sizeInBytes()).reshape(h, img.bytesPerLine())
    return arr[:, : w * 4].reshape(h, w, 4).copy()


class MainWindow(QMainWindow):
    def __init__(self, cfg, apply_language):
        super().__init__()
        self.cfg = cfg
        self.apply_language = apply_language
        self.setWindowTitle("SmartMonitor")
        self.setStyleSheet(STYLE)
        self.resize(1100, 700)

        self.sensors = SensorManager()
        self.sensors.start()

        self.bridge = _Bridge()
        self.devices = DeviceManager(on_state=self.bridge.state.emit, on_version=self.bridge.version.emit,
                                     on_firmware=self.bridge.firmware.emit)
        self.renderer = ThemeRenderer(self.sensors, cfg, (SCREEN_W, SCREEN_H))
        self.screen_capture = False

        # --- layout: nav column + stacked pages
        root = QWidget()
        h = QHBoxLayout(root)
        nav = QVBoxLayout()
        logo = QLabel("SmartMonitor")
        logo.setStyleSheet("color:#b49653; font-size:18px; font-weight:bold; padding:8px;")
        nav.addWidget(logo)
        self.pages = QStackedWidget()
        self.home = HomePage(cfg, self.renderer, self)
        self.editor = EditorPage(cfg, self.sensors, self)
        self.settings = SettingsPage(cfg, self.devices, self.sensors)
        group = QButtonGroup(self)
        for i, (name, page) in enumerate([("Home", self.home), ("Theme", self.editor), ("Setting", self.settings)]):
            btn = QToolButton()
            btn.setObjectName("nav")
            btn.setText(QCoreApplication.translate("DispMainWIndow", name))
            btn.setCheckable(True)
            btn.setChecked(i == 0)
            group.addButton(btn, i)
            nav.addWidget(btn)
            self.pages.addWidget(page)
        group.idClicked.connect(self._switch_page)
        nav.addStretch(1)
        self.conn_label = QLabel()
        nav.addWidget(self.conn_label)
        h.addLayout(nav)
        h.addWidget(self.pages, 1)
        self.setCentralWidget(root)
        self._set_connected(False)

        self.bridge.state.connect(self._set_connected)
        self.bridge.version.connect(self.settings.set_version)
        self.bridge.firmware.connect(self.settings.set_firmware_status)
        self.settings.language_changed.connect(self._on_language)
        self.editor.theme_saved.connect(self.home.reload_themes)

        # --- tray icon
        self.tray = QSystemTrayIcon(self._icon(), self)
        menu = QMenu()
        act_show = QAction("Show", self, triggered=self.show_normal)
        act_quit = QAction(QCoreApplication.translate("DispMainWIndow", "Exit"), self,
                           triggered=QCoreApplication.quit)
        menu.addAction(act_show)
        menu.addAction(act_quit)
        self.tray.setContextMenu(menu)
        self.tray.activated.connect(lambda r: self.show_normal() if r == QSystemTrayIcon.Trigger else None)
        self.tray.show()
        self.setWindowIcon(self._icon())

        # --- start pipeline
        self.home.load_theme(cfg.get("theme/name"))
        self.devices.start()
        self.devices.set_brightness(cfg.get("brightness/value"))
        self.timer = QTimer(self, interval=FRAME_INTERVAL_MS, timeout=self._tick)
        self.timer.start()

    @staticmethod
    def _icon() -> QIcon:
        pm = QPixmap(64, 64)
        pm.fill(Qt.transparent)
        p = QPainter(pm)
        p.setRenderHint(QPainter.Antialiasing)
        p.setBrush(QColor("#b49653"))
        p.setPen(Qt.NoPen)
        p.drawRoundedRect(4, 4, 56, 56, 12, 12)
        p.setBrush(QColor("#030303"))
        p.drawRoundedRect(14, 14, 36, 30, 4, 4)
        p.end()
        return QIcon(pm)

    # --- frame pipeline (original: QTimer -> render -> rotate -> Transport queue)
    def _tick(self):
        if self.screen_capture:
            screen = QGuiApplication.primaryScreen()
            img = screen.grabWindow(0).toImage().scaled(
                SCREEN_W, SCREEN_H, Qt.KeepAspectRatioByExpanding, Qt.SmoothTransformation)
            img = img.copy((img.width() - SCREEN_W) // 2, (img.height() - SCREEN_H) // 2, SCREEN_W, SCREEN_H)
        else:
            img = self.renderer.render()
        self.home.set_preview(img)
        if self.devices.connected:
            angle = self.cfg.get("rotate/value")
            if angle:
                img = img.transformed(QTransform().rotate(angle))
            self.devices.submit_frame(qimage_to_rgba(img))

    def set_brightness(self, value: int):
        self.cfg.set("brightness/value", value)
        self.devices.set_brightness(value)

    def _set_connected(self, on: bool):
        text = QCoreApplication.translate("DispMainWIndow", "connect" if on else "disconnect")
        color = "#3c3" if on else "#c33"
        self.conn_label.setText(f"<span style='color:{color}'>●</span> {text}")
        if on:
            self.devices.set_brightness(self.cfg.get("brightness/value"))

    def _switch_page(self, i: int):
        self.pages.setCurrentIndex(i)

    def _on_language(self, i: int):
        self.apply_language(i)
        self.statusBar().showMessage("Restart the application to apply the language everywhere.", 5000)

    def show_normal(self):
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def closeEvent(self, e):
        # Like the original, closing the window keeps the display running in the tray.
        e.ignore()
        self.hide()
        self.tray.showMessage("SmartMonitor", "Still running in the system tray.", QSystemTrayIcon.Information, 2000)

    def shutdown(self):
        self.timer.stop()
        self.editor.scene.selectionChanged.disconnect()
        self.devices.stop()
        self.sensors.stop()
        self.renderer.close()
        self.devices.join(timeout=2)
