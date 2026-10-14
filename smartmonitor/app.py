"""Main window (original: DispMainWIndow) and the frame pipeline.

Performance notes (additions of this rebuild):
  * the window's pages are built only when the window is first shown, so a `--minimized`
    autostart only creates the sensor/render/USB pipeline;
  * frames are re-rendered only when their content changes; static content is sent to the display
    at a low keep-alive rate instead of re-encoding identical frames 25 times per second;
  * full frame rate (25 fps, as the original) is used only for video/GIF themes and screen mirroring.
"""
from __future__ import annotations

import logging
import threading
import time

from PySide6.QtCore import QCoreApplication, QObject, Qt, QTimer, Signal
from PySide6.QtGui import QAction, QActionGroup, QColor, QGuiApplication, QIcon, QImage, QPainter, QPixmap
from PySide6.QtWidgets import (QButtonGroup, QHBoxLayout, QLabel, QMainWindow, QMenu, QStackedWidget,
                               QSystemTrayIcon, QToolButton, QVBoxLayout, QWidget)

from .config import MODE_CAPTURE, MODE_DASHBOARD, MODE_THEME
from .dashboard import DashboardRenderer
from .renderer import ThemeRenderer
from .sensors import SensorManager
from .transport import SCREEN_H, SCREEN_W, DeviceManager

log = logging.getLogger(__name__)

FAST_MS = 40          # 25 fps, the original encoder's frame rate: video, GIF, screen mirroring
SLOW_MS = 100         # static content: poll for changes (rendering is skipped when nothing changed)
IDLE_MS = 1000        # no device and no visible preview
KEEPALIVE_S = 0.5     # re-send an unchanged frame at least this often (at least 2 fps to the device)

MODE_NAMES = {MODE_DASHBOARD: "Dashboard", MODE_THEME: "Theme", MODE_CAPTURE: "Screen mirror"}

STYLE = """
QWidget { background-color: #0b0d12; color: #e8eaef; font-family: "Segoe UI", "Noto Sans", Arial; font-size: 14px; }
QGroupBox { background-color: #161a22; border: 1px solid #222838; border-radius: 10px; margin-top: 4px; padding: 10px; }
QLabel#h2 { font-size: 15px; font-weight: 600; color: #ffffff; background: transparent; }
QLabel#muted { color: #8b93a7; background: transparent; }
QToolButton#nav { border: none; border-radius: 8px; background-color: transparent; min-width: 120px; min-height: 44px;
    font-size: 15px; color: #c9cedb; text-align: left; padding-left: 12px; }
QToolButton#nav:hover { background-color: #161a22; }
QToolButton#nav:checked { color: #0b0d12; background-color: #d4b26a; font-weight: 600; }
QPushButton, QToolButton { background-color: #222838; border: 1px solid #2d3548; border-radius: 6px; padding: 5px 12px; }
QPushButton:hover, QToolButton:hover { background-color: #2b3347; }
QPushButton:pressed, QToolButton:pressed, QPushButton:checked, QToolButton:checked {
    background-color: #d4b26a; color: #0b0d12; border-color: #d4b26a; }
QPushButton:disabled { color: #5b6378; }
QComboBox, QSpinBox, QDoubleSpinBox, QLineEdit, QPlainTextEdit {
    background: #0f1218; border: 1px solid #2d3548; border-radius: 6px; padding: 3px 8px; color: #e8eaef; }
QComboBox:focus, QLineEdit:focus, QDoubleSpinBox:focus { border-color: #d4b26a; }
QListWidget { background-color: #0f1218; border: 1px solid #222838; border-radius: 10px; outline: 0px; padding: 4px; }
QListWidget::item { border-radius: 8px; padding: 2px; }
QListWidget::item:hover { background-color: #1c2230; }
QListWidget::item:selected { background-color: #d4b26a; color: #0b0d12; }
QSlider::groove:horizontal { height: 6px; background: #2d3548; border-radius: 3px; }
QSlider::sub-page:horizontal { background: #d4b26a; border-radius: 3px; }
QSlider::handle:horizontal { background: #ffffff; width: 16px; margin: -5px 0; border-radius: 8px; }
QScrollArea { border: none; }
QLabel, QCheckBox, QRadioButton { background: transparent; }
QCheckBox::indicator { width: 18px; height: 18px; }
QStatusBar { background: #0b0d12; color: #8b93a7; }
"""


class _Bridge(QObject):
    """Marshals transport callbacks (worker threads) onto the GUI thread."""
    state = Signal(bool)
    version = Signal(str)
    firmware = Signal(bool, bool, str)
    status = Signal(str)


def qimage_to_bgra(img: QImage, angle: int = 0):
    """QImage -> contiguous numpy BGRA array, rotated clockwise by `angle` degrees (multiple of 90)."""
    import numpy as np
    if img.format() not in (QImage.Format_ARGB32, QImage.Format_ARGB32_Premultiplied, QImage.Format_RGB32):
        img = img.convertToFormat(QImage.Format_ARGB32_Premultiplied)
    w, h = img.width(), img.height()
    arr = np.frombuffer(img.constBits(), np.uint8, img.sizeInBytes()).reshape(h, img.bytesPerLine())
    arr = arr[:, : w * 4].reshape(h, w, 4)
    k = (angle // 90) % 4
    if k:
        arr = np.rot90(arr, k=-k)  # Qt's positive rotation is clockwise; np.rot90 is counter-clockwise
    return np.ascontiguousarray(arr)


def prewarm_imports():
    """Import heavy optional modules on a background thread so the first frame/video isn't delayed."""
    def run():
        import importlib
        try:
            for name in ("numpy", "av"):
                importlib.import_module(name)
        except Exception as e:  # pragma: no cover
            log.debug("prewarm failed: %s", e)
    threading.Thread(target=run, name="Prewarm", daemon=True).start()


class MainWindow(QMainWindow):
    def __init__(self, cfg, apply_language):
        super().__init__()
        self.cfg = cfg
        self.apply_language = apply_language
        self.setWindowTitle("SmartMonitor")
        self.resize(1120, 720)
        self.setMinimumSize(980, 640)

        # ---- core pipeline (always running, also when started minimized)
        self.sensors = SensorManager()
        self._apply_sensor_settings()
        self.sensors.start()
        self.bridge = _Bridge()
        self.devices = DeviceManager(on_state=self.bridge.state.emit, on_version=self.bridge.version.emit,
                                     on_firmware=self.bridge.firmware.emit, on_status=self.bridge.status.emit)
        self.renderer = ThemeRenderer(self.sensors, cfg, (SCREEN_W, SCREEN_H))
        self.dashboard = DashboardRenderer(self.sensors, cfg, (SCREEN_W, SCREEN_H))
        self.mode = cfg.get("display/mode")
        if self.mode not in MODE_NAMES:
            self.mode = MODE_DASHBOARD
        self.device_version = ""
        self.device_status = DeviceManager.STATUS_SEARCHING
        self._capture_id = 0
        self._sent_id, self._sent_at, self._preview_id = None, 0.0, None
        if self.mode == MODE_THEME:
            self.renderer.load(cfg.get("theme/name"))

        # UI pages are created lazily (ensure_ui)
        self.home = self.editor = self.settings = None
        self.pages: QStackedWidget | None = None
        self.bridge.state.connect(self._set_connected)
        self.bridge.version.connect(self._on_version)
        self.bridge.firmware.connect(self._on_firmware)
        self.bridge.status.connect(self._on_status)

        self._build_tray()
        self.setWindowIcon(self._icon())

        self.devices.start()
        self.devices.set_brightness(cfg.get("brightness/value"))
        self.timer = QTimer(self, interval=SLOW_MS, timeout=self._tick)
        self.timer.setTimerType(Qt.PreciseTimer)
        self.timer.start()
        prewarm_imports()

    # ------------------------------------------------------------------ settings applied at startup
    def _apply_sensor_settings(self):
        nics = ["Auto"] + self.sensors.network_interfaces()
        i = self.cfg.get("network/type")
        self.sensors.set_network(nics[i] if 0 < i < len(nics) else None)
        disks = self.sensors.disks()
        i = self.cfg.get("disk/type")
        self.sensors.set_disk(disks[i] if 0 <= i < len(disks) else None)

    # ------------------------------------------------------------------ lazy UI
    def ensure_ui(self):
        if self.pages is not None:
            return
        from .home_page import HomePage
        self.setStyleSheet(STYLE)
        root = QWidget()
        h = QHBoxLayout(root)
        h.setContentsMargins(12, 12, 12, 12)
        h.setSpacing(12)
        nav = QVBoxLayout()
        nav.setSpacing(6)
        logo = QLabel("◼ SmartMonitor")
        logo.setStyleSheet("color:#d4b26a; font-size:18px; font-weight:700; padding:6px 4px 14px 4px;")
        nav.addWidget(logo)
        self.pages = QStackedWidget()
        self.home = HomePage(self.cfg, self.renderer, self)
        self.pages.addWidget(self.home)
        self.pages.addWidget(QWidget())  # editor placeholder
        self.pages.addWidget(QWidget())  # settings placeholder
        group = QButtonGroup(self)
        for i, (name, icon) in enumerate([("Home", "⌂"), ("Theme", "✎"), ("Setting", "⚙")]):
            btn = QToolButton()
            btn.setObjectName("nav")
            btn.setText(f"{icon}  {QCoreApplication.translate('DispMainWIndow', name)}")
            btn.setCheckable(True)
            btn.setChecked(i == 0)
            btn.setToolButtonStyle(Qt.ToolButtonTextOnly)
            group.addButton(btn, i)
            nav.addWidget(btn)
        group.idClicked.connect(self._switch_page)
        nav.addStretch(1)
        self.conn_label = QLabel()
        self.conn_label.setWordWrap(True)
        self.conn_label.setFixedWidth(130)
        nav.addWidget(self.conn_label)
        h.addLayout(nav)
        h.addWidget(self.pages, 1)
        self.setCentralWidget(root)
        self._set_connected(self.devices.connected)
        self.home.set_status(self.device_status, self.devices.connected)

    def _page(self, i: int):
        """Build the editor / settings pages on first use."""
        w = self.pages.widget(i)
        if i == 1 and self.editor is None:
            from .editor_page import EditorPage
            self.editor = w = EditorPage(self.cfg, self.sensors, self)
            self.editor.theme_saved.connect(self.home.reload_themes)
        elif i == 2 and self.settings is None:
            from .settings_page import SettingsPage
            self.settings = w = SettingsPage(self.cfg, self.devices, self.sensors)
            self.settings.language_changed.connect(self._on_language)
            self.settings.set_version(self.device_version)
        else:
            return w
        old = self.pages.widget(i)
        self.pages.insertWidget(i, w)
        self.pages.removeWidget(old)
        old.deleteLater()
        return w

    def _switch_page(self, i: int):
        self.pages.setCurrentWidget(self._page(i))

    # ------------------------------------------------------------------ tray
    @staticmethod
    def _icon() -> QIcon:
        pm = QPixmap(64, 64)
        pm.fill(Qt.transparent)
        p = QPainter(pm)
        p.setRenderHint(QPainter.Antialiasing)
        p.setBrush(QColor("#d4b26a"))
        p.setPen(Qt.NoPen)
        p.drawRoundedRect(4, 4, 56, 56, 12, 12)
        p.setBrush(QColor("#0b0d12"))
        p.drawRoundedRect(14, 14, 36, 30, 4, 4)
        p.end()
        return QIcon(pm)

    def _build_tray(self):
        self.tray = QSystemTrayIcon(self._icon(), self)
        menu = QMenu()
        menu.addAction(QAction("Open SmartMonitor", self, triggered=self.show_normal))
        menu.addSeparator()
        self._mode_actions = QActionGroup(self)
        for mode, name in MODE_NAMES.items():
            act = QAction(name, self, checkable=True, checked=mode == self.mode)
            act.triggered.connect(lambda _=False, m=mode: self.set_mode(m))
            self._mode_actions.addAction(act)
            menu.addAction(act)
        menu.addSeparator()
        menu.addAction(QAction(QCoreApplication.translate("DispMainWIndow", "Exit"), self,
                               triggered=QCoreApplication.quit))
        self._tray_menu = menu
        self.tray.setContextMenu(menu)
        self.tray.activated.connect(lambda r: self.show_normal() if r == QSystemTrayIcon.Trigger else None)
        self.tray.setToolTip("SmartMonitor — searching for the display")
        self.tray.show()

    # ------------------------------------------------------------------ display mode
    def set_mode(self, mode: int):
        if mode not in MODE_NAMES:
            return
        self.mode = mode
        self.cfg.set("display/mode", mode)
        if mode == MODE_THEME and self.renderer.theme_name is None:
            self.renderer.load(self.cfg.get("theme/name"))
        for act, m in zip(self._mode_actions.actions(), MODE_NAMES):
            act.setChecked(m == mode)
        self._sent_id = self._preview_id = None
        if self.home is not None:
            self.home.on_mode_changed(mode)

    # ------------------------------------------------------------------ frame pipeline
    def _frame(self) -> tuple[QImage, int]:
        if self.mode == MODE_CAPTURE:
            screen = QGuiApplication.primaryScreen()
            img = screen.grabWindow(0).toImage().scaled(
                SCREEN_W, SCREEN_H, Qt.KeepAspectRatioByExpanding, Qt.SmoothTransformation)
            img = img.copy((img.width() - SCREEN_W) // 2, (img.height() - SCREEN_H) // 2, SCREEN_W, SCREEN_H)
            self._capture_id += 1
            return img, ("capture", self._capture_id)
        if self.mode == MODE_THEME:
            return self.renderer.render(), ("theme", self.renderer.frame_id)
        return self.dashboard.render(), ("dash", self.dashboard.frame_id)

    def _tick(self):
        preview = self.home is not None and self.isVisible() and not self.isMinimized() \
            and self.pages.currentIndex() == 0
        connected = self.devices.connected
        if not preview and not connected:
            self._set_interval(IDLE_MS)
            return
        img, fid = self._frame()
        if preview and fid != self._preview_id:
            self._preview_id = fid
            self.home.set_preview(img)
        if connected:
            now = time.monotonic()
            if fid != self._sent_id or now - self._sent_at >= KEEPALIVE_S:
                self.devices.submit_frame(qimage_to_bgra(img, self.cfg.get("rotate/value")))
                self._sent_id, self._sent_at = fid, now
        animated = self.mode == MODE_CAPTURE or (self.mode == MODE_THEME and self.renderer.animated)
        self._set_interval(FAST_MS if animated else SLOW_MS)

    def _set_interval(self, ms: int):
        if self.timer.interval() != ms:
            self.timer.setInterval(ms)

    def invalidate(self):
        """Something on screen changed outside the renderers' knowledge (edits, rotation)."""
        self.renderer.invalidate()
        self._sent_id = self._preview_id = None

    # ------------------------------------------------------------------ device events
    def set_brightness(self, value: int):
        self.cfg.set("brightness/value", value)
        self.devices.set_brightness(value)

    def _set_connected(self, on: bool):
        if on:
            self.devices.set_brightness(self.cfg.get("brightness/value"))
            self._sent_id = None
        if self.pages is None:
            return
        text = QCoreApplication.translate("DispMainWIndow", "connect" if on else "disconnect")
        color = "#22c55e" if on else "#ef4444"
        self.conn_label.setText(f"<span style='color:{color}; font-size:16px'>●</span> {text}")

    def _on_status(self, text: str):
        self.device_status = text
        self.tray.setToolTip(f"SmartMonitor — {text}")
        if self.home is not None:
            self.home.set_status(text, self.devices.connected)

    def _on_version(self, text: str):
        self.device_version = text
        if self.settings is not None:
            self.settings.set_version(text)
        if self.home is not None:
            self.home.set_status(self.device_status, self.devices.connected, text)

    def _on_firmware(self, done: bool, ok: bool, msg: str):
        if self.settings is not None:
            self.settings.set_firmware_status(done, ok, msg)

    def _on_language(self, i: int):
        self.apply_language(i)
        self.statusBar().showMessage("Restart the application to apply the language everywhere.", 5000)

    # ------------------------------------------------------------------ window
    def show_normal(self):
        self.ensure_ui()
        self.showNormal()
        self.raise_()
        self.activateWindow()
        self._preview_id = None

    def closeEvent(self, e):
        # Like the original, closing the window keeps the display running in the tray.
        e.ignore()
        self.hide()
        if not self.cfg.get("ui/trayHintShown"):
            self.cfg.set("ui/trayHintShown", True)
            self.tray.showMessage("SmartMonitor", "Still running in the system tray. Right-click the icon to exit.",
                                  QSystemTrayIcon.Information, 3000)

    def shutdown(self):
        self.timer.stop()
        if self.editor is not None:
            self.editor.scene.selectionChanged.disconnect()
        self.devices.stop()
        self.sensors.stop()
        self.renderer.close()
        self.devices.join(timeout=2)
