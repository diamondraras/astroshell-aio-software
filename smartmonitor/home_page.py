"""Home page (original: DispHomePage): live preview, display mode, local themes, background/mask, sensors S1..S6."""
from __future__ import annotations

import logging
import shutil
from pathlib import Path

from PySide6.QtCore import QCoreApplication, QSize, Qt, QTimer
from PySide6.QtGui import QIcon, QImage, QPixmap
from PySide6.QtWidgets import (QButtonGroup, QCheckBox, QComboBox, QFileDialog, QFrame, QGridLayout, QGroupBox,
                               QHBoxLayout, QLabel, QLineEdit, QListView, QListWidget, QListWidgetItem, QMenu,
                               QMessageBox, QPushButton, QSlider, QVBoxLayout, QWidget)

from . import theme_format as tf
from .config import MODE_CAPTURE, MODE_DASHBOARD, MODE_THEME, themes_dir
from .media import FILE_FILTER
from .scene import SENSOR_LABELS, ImageItem, NumberItem

log = logging.getLogger(__name__)
PREVIEW_FILE = "preview.png"
THUMB = 96


def tr(s: str) -> str:
    return QCoreApplication.translate("DispHomePage", s)


def _h2(text: str) -> QLabel:
    lab = QLabel(text)
    lab.setObjectName("h2")
    return lab


def _muted(text: str = "") -> QLabel:
    lab = QLabel(text)
    lab.setObjectName("muted")
    lab.setWordWrap(True)
    return lab


class HomePage(QWidget):
    def __init__(self, cfg, renderer, main, parent=None):
        super().__init__(parent)
        self.cfg, self.renderer, self.main = cfg, renderer, main
        root = QHBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(12)

        # ---- left column: preview, status, display controls
        left = QVBoxLayout()
        left.setSpacing(10)
        frame = QFrame()
        frame.setStyleSheet("QFrame { background:#000; border:1px solid #2d3548; border-radius:12px; }")
        fl = QVBoxLayout(frame)
        fl.setContentsMargins(8, 8, 8, 8)
        self.preview = QLabel()
        self.preview.setFixedSize(320, 320)
        self.preview.setStyleSheet("border:none; background:#000;")
        fl.addWidget(self.preview, 0, Qt.AlignCenter)
        left.addWidget(frame, 0, Qt.AlignHCenter)
        self.status = _muted()
        self.status.setTextFormat(Qt.RichText)
        self.status.setFixedWidth(338)
        left.addWidget(self.status)

        dev = QGroupBox()
        g = QGridLayout(dev)
        g.setVerticalSpacing(10)
        g.addWidget(_h2("Display"), 0, 0, 1, 3)
        modes = QHBoxLayout()
        modes.setSpacing(0)
        self.mode_group = QButtonGroup(self)
        for mode, text, tip in ((MODE_DASHBOARD, "Dashboard", "Clock, CPU/GPU, network with live graphs"),
                                (MODE_THEME, "Theme", "Show a theme from the library"),
                                (MODE_CAPTURE, "Screen mirror", "Mirror the centre of the main screen")):
            b = QPushButton(text)
            b.setCheckable(True)
            b.setToolTip(tip)
            b.setMinimumHeight(32)
            self.mode_group.addButton(b, mode)
            modes.addWidget(b)
        self.mode_group.idClicked.connect(main.set_mode)
        g.addLayout(modes, 1, 0, 1, 3)

        g.addWidget(QLabel(tr("Brightness:")), 2, 0)
        self.brightness = QSlider(Qt.Horizontal)
        self.brightness.setRange(0, 100)
        self.brightness.setValue(cfg.get("brightness/value"))
        self.brightness_label = QLabel(f"{self.brightness.value()}%")
        self.brightness_label.setMinimumWidth(40)
        self._bright_timer = QTimer(self, singleShot=True, interval=150,
                                    timeout=lambda: main.set_brightness(self.brightness.value()))
        self.brightness.valueChanged.connect(self._on_brightness)
        g.addWidget(self.brightness, 2, 1)
        g.addWidget(self.brightness_label, 2, 2)

        g.addWidget(QLabel("Screen rotation:"), 3, 0)
        self.angle = QComboBox()
        self.angle.addItems(["0°", "90° ↻", "180°", "270° ↻"])
        self.angle.setCurrentIndex((cfg.get("rotate/value") // 90) % 4)
        self.angle.currentIndexChanged.connect(self._on_angle)
        g.addWidget(self.angle, 3, 1, 1, 2)
        left.addWidget(dev)
        left.addStretch(1)
        root.addLayout(left)

        # ---- right column
        right = QVBoxLayout()
        right.setSpacing(10)

        # dashboard options
        self.dash_box = QGroupBox()
        dv = QGridLayout(self.dash_box)
        dv.addWidget(_h2("Dashboard"), 0, 0, 1, 2)
        self.hour24 = QCheckBox("24-hour clock")
        self.hour24.setChecked(cfg.get("dashboard/hour24"))
        self.hour24.toggled.connect(lambda on: cfg.set("dashboard/hour24", on))
        dv.addWidget(self.hour24, 1, 0)
        self.unit = QComboBox()
        self.unit.addItems(["°C (Celsius)", "°F (Fahrenheit)"])
        self.unit.setCurrentIndex(cfg.get("temp/uint"))
        self.unit.currentIndexChanged.connect(lambda i: cfg.set("temp/uint", i))
        dv.addWidget(self.unit, 1, 1)
        self.dash_info = _muted()
        self.dash_info.setTextFormat(Qt.RichText)
        dv.addWidget(self.dash_info, 2, 0, 1, 2)
        right.addWidget(self.dash_box)

        # theme library
        box = QGroupBox()
        v = QVBoxLayout(box)
        head = QHBoxLayout()
        head.addWidget(_h2(tr("Local Theme")))
        head.addStretch(1)
        for text, slot, tip in ((tr("Import"), self._import, "Import a theme folder"),
                                (tr("Export"), self._export, "Export the current theme folder")):
            b = QPushButton(text)
            b.setToolTip(tip)
            b.clicked.connect(slot)
            head.addWidget(b)
        v.addLayout(head)
        v.addWidget(_muted("Click a theme to show it on the display. Right-click to delete."))
        self.themes = QListWidget()
        self.themes.setViewMode(QListView.IconMode)
        self.themes.setIconSize(QSize(THUMB, THUMB))
        self.themes.setResizeMode(QListView.Adjust)
        self.themes.setMovement(QListView.Static)
        self.themes.setSpacing(6)
        self.themes.setGridSize(QSize(THUMB + 18, THUMB + 30))
        self.themes.setUniformItemSizes(True)
        self.themes.setContextMenuPolicy(Qt.CustomContextMenu)
        self.themes.customContextMenuRequested.connect(self._theme_menu)
        self.themes.itemClicked.connect(self._on_theme_clicked)
        v.addWidget(self.themes)
        right.addWidget(box, 3)

        # current-theme editing (theme mode only)
        self.bg_box = QGroupBox()
        h = QHBoxLayout(self.bg_box)
        h.addWidget(_h2(tr("Background")))
        h.addStretch(1)
        for text, slot in ((tr("Load Image"), self._load_image), (tr("Load Video"), self._load_video),
                           (tr("Load Mask"), self._load_mask)):
            b = QPushButton(text)
            b.clicked.connect(slot)
            h.addWidget(b)
        right.addWidget(self.bg_box)

        self.sys_box = QGroupBox()
        self.sys_grid = QGridLayout(self.sys_box)
        right.addWidget(self.sys_box, 0)
        root.addLayout(right, 1)

        self._thumb_queue: list = []
        self._thumb_loader = QTimer(self, interval=0, timeout=self._load_some_thumbnails)
        self._thumb_timer = QTimer(self, singleShot=True, interval=3000, timeout=self._save_thumbnail)
        self._info_timer = QTimer(self, interval=2000, timeout=self._update_dash_info)
        self.reload_themes()
        self.on_mode_changed(main.mode)

    # ------------------------------------------------------------------ display mode / status
    def on_mode_changed(self, mode: int):
        b = self.mode_group.button(mode)
        if b:
            b.setChecked(True)
        theme = mode == MODE_THEME
        self.dash_box.setVisible(mode == MODE_DASHBOARD)
        self.bg_box.setVisible(theme)
        self.sys_box.setVisible(theme)
        if theme:
            self._build_sensor_slots()
            self._select_current_theme()
        else:
            self.themes.clearSelection()
        if mode == MODE_DASHBOARD:
            self._update_dash_info()
            self._info_timer.start()
        else:
            self._info_timer.stop()

    def set_status(self, text: str, connected: bool, version: str = ""):
        color = "#22c55e" if connected else "#f59e0b" if "in use" in text or "cannot" in text else "#8b93a7"
        ver = version or self.main.device_version
        extra = f" · {ver}" if connected and ver else ""
        self.status.setText(f"<span style='color:{color}'>●</span> {text}{extra}")

    def _update_dash_info(self):
        s = self.main.sensors
        snap = s.snapshot()
        nic = s.interface or "none"
        auto = "" if self.cfg.get("network/type") else " (auto)"
        if "cpu_temp" in snap:
            cpu = "available"
        else:
            cpu = ("<span style='color:#f59e0b'>not available</span> — Windows exposes no CPU sensor "
                   "without a helper: run <b>HWiNFO</b> (Settings → Shared Memory Support) or "
                   "<b>LibreHardwareMonitor</b> (Options → Remote Web Server, port 8085)")
        self.dash_info.setText(f"Network interface: <b>{nic}</b>{auto}<br>CPU temperature: {cpu}")

    # ------------------------------------------------------------------ themes
    def reload_themes(self):
        self.themes.clear()
        placeholder = QPixmap(THUMB, THUMB)
        placeholder.fill(Qt.black)
        icon = QIcon(placeholder)
        self._thumb_queue = []
        for d in sorted(themes_dir().glob("theme_*")):
            if not d.is_dir():
                continue
            name = d.name.removeprefix("theme_")
            it = QListWidgetItem(icon, name)
            it.setData(Qt.UserRole, name)
            it.setToolTip(name)
            self.themes.addItem(it)
            self._thumb_queue.append((it, d))
        self._select_current_theme()
        self._thumb_loader.start()  # thumbnails load a few at a time, keeping the UI responsive

    def _select_current_theme(self):
        if self.main.mode != MODE_THEME:
            return
        for i in range(self.themes.count()):
            it = self.themes.item(i)
            if it.data(Qt.UserRole) == self.renderer.theme_name:
                self.themes.setCurrentItem(it)

    def _load_some_thumbnails(self):
        for _ in range(4):
            if not self._thumb_queue:
                self._thumb_loader.stop()
                return
            it, d = self._thumb_queue.pop(0)
            it.setIcon(self._thumbnail(d))

    @staticmethod
    def _thumbnail(d: Path) -> QIcon:
        cand = [d / PREVIEW_FILE] + sorted((d / "res").glob("*.png")) + sorted((d / "res").glob("*.jpg"))
        for c in cand:
            if c.exists():
                img = QImage(str(c))
                if not img.isNull():
                    return QIcon(QPixmap.fromImage(img.scaled(THUMB, THUMB, Qt.KeepAspectRatio,
                                                              Qt.SmoothTransformation)))
        pm = QPixmap(THUMB, THUMB)
        pm.fill(Qt.darkGray)
        return QIcon(pm)

    def _on_theme_clicked(self, it: QListWidgetItem):
        name = it.data(Qt.UserRole)
        if self.main.mode != MODE_THEME:
            self.main.set_mode(MODE_THEME)
        if name != self.renderer.theme_name or self.renderer.scene.theme_dir is None:
            self.load_theme(name)
        self.themes.setCurrentItem(it)

    def load_theme(self, name: str):
        if not self.renderer.load(name):
            QMessageBox.warning(self, tr("Error"), tr("file %1 not found").replace("%1", name))
            return
        self.main.invalidate()
        self._build_sensor_slots()
        self._thumb_timer.start()

    def _save_thumbnail(self):
        if self.renderer.theme_name and self.renderer.scene.theme_dir:
            img = self.renderer.scene.render_image()
            img.save(str(self.renderer.scene.theme_dir / PREVIEW_FILE))
            for i in range(self.themes.count()):
                it = self.themes.item(i)
                if it.data(Qt.UserRole) == self.renderer.theme_name:
                    it.setIcon(QIcon(QPixmap.fromImage(img).scaled(THUMB, THUMB, Qt.KeepAspectRatio,
                                                                   Qt.SmoothTransformation)))

    def set_preview(self, img: QImage):
        self.preview.setPixmap(QPixmap.fromImage(img))

    def _theme_menu(self, pos):
        it = self.themes.itemAt(pos)
        if not it:
            return
        menu = QMenu(self)
        act = menu.addAction(QCoreApplication.translate("DispThemeList", "Delete"))
        if menu.exec(self.themes.mapToGlobal(pos)) == act:
            name = it.data(Qt.UserRole)
            if QMessageBox.question(self, "Delete theme", f"Delete theme “{name}” permanently?") \
                    != QMessageBox.Yes:
                return
            if name == self.renderer.theme_name:
                self.renderer.scene.clear_theme()
                self.renderer.theme_name = None
                self.main.invalidate()
            shutil.rmtree(themes_dir() / f"theme_{name}", ignore_errors=True)
            self.reload_themes()

    def _import(self):
        src = QFileDialog.getExistingDirectory(self, tr("theme folder(*)"))
        if not src:
            return
        src = Path(src)
        if not list(src.glob("*.theme")):
            QMessageBox.warning(self, tr("Error"), QCoreApplication.translate("QObject", "file is empty"))
            return
        name = src.name if src.name.startswith("theme_") else f"theme_{src.name}"
        dst = themes_dir() / name
        if dst.exists():
            QMessageBox.warning(self, "Warning", QCoreApplication.translate("QObject", "theme name is exit"))
            return
        shutil.copytree(src, dst)
        self.reload_themes()

    def _export(self):
        if not self.renderer.theme_name:
            QMessageBox.information(self, tr("Export"), "Select a theme first.")
            return
        dst = QFileDialog.getExistingDirectory(self, tr("Export"))
        if dst:
            src = themes_dir() / f"theme_{self.renderer.theme_name}"
            shutil.copytree(src, Path(dst) / src.name, dirs_exist_ok=True)

    # ------------------------------------------------------------------ background / mask
    def _replace_image(self, object_name: str, path: str, insert_at: int):
        scene = self.renderer.scene
        if scene.theme_dir is None:
            QMessageBox.information(self, tr("Background"), "Select a theme first.")
            return
        rel = scene.import_resource(path)
        target = next((w for w in scene.widgets if isinstance(w, ImageItem) and w.objectName() == object_name), None)
        if target is None:
            rec = tf.ImageWidget(object_name, (0.0, 0.0, float(scene.size[0]), float(scene.size[1])), rel)
            item = scene.add_record(rec)
            scene.widgets.remove(item)
            scene.widgets.insert(insert_at, item)
            for z, w in enumerate(scene.widgets):
                w.setZValue(z)
        else:
            target.record.file = rel
            target.load()
        scene.save()
        self.main.invalidate()
        self._thumb_timer.start()

    def _load_image(self):
        p, _ = QFileDialog.getOpenFileName(self, tr("Open Image"), "", FILE_FILTER)
        if p:
            self._replace_image("image_theme_background", p, 0)

    def _load_video(self):
        p, _ = QFileDialog.getOpenFileName(self, tr("Load Video"), "", tr("Video File (*.mp4 *.h264 *.mkv)"))
        if p:
            self._replace_image("image_theme_background", p, 0)

    def _load_mask(self):
        p, _ = QFileDialog.getOpenFileName(self, tr("Load Mask"), "", tr("Image Files (*.png *.webp)"))
        if p:
            self._replace_image("image_home_mask", p, 1)

    # ------------------------------------------------------------------ sensors S1..S6
    def _build_sensor_slots(self):
        while self.sys_grid.count():
            w = self.sys_grid.takeAt(0).widget()
            if w:
                w.deleteLater()
        items = [w for w in self.renderer.scene.widgets
                 if isinstance(w, NumberItem) and w.objectName().startswith("data_home_s")]
        self.sys_box.setVisible(bool(items) and self.main.mode == MODE_THEME)
        self.sys_grid.addWidget(_h2(QCoreApplication.translate("DispHomeSysInfoEditBox", "System\nInfo")
                                    .replace("\n", " ")), 0, 0, 1, 4)
        for row, item in enumerate(items, start=1):
            slot = item.objectName().removeprefix("data_home_s")
            self.sys_grid.addWidget(QLabel(tr(f"S{slot}")), row, 0)
            combo = QComboBox()
            for sid, label in SENSOR_LABELS.items():
                if sid:
                    combo.addItem(label, sid)
            combo.setCurrentIndex(max(0, combo.findData(item.record.sensor)))
            self.sys_grid.addWidget(combo, row, 1)
            name = QLineEdit(item.name_item.toPlainText() if item.name_item else "")
            name.setPlaceholderText("name")
            unit = QLineEdit(item.unit_item.toPlainText() if item.unit_item else "")
            unit.setPlaceholderText(tr("Unit:").rstrip(":"))
            unit.setMaximumWidth(70)
            self.sys_grid.addWidget(name, row, 2)
            self.sys_grid.addWidget(unit, row, 3)

            def apply(_=None, item=item, combo=combo, name=name, unit=unit):
                item.record.sensor = combo.currentData()
                if item.name_item:
                    item.name_item.set_text(name.text())
                if item.unit_item:
                    item.unit_item.set_text(unit.text())
                self.renderer.scene.save()
                self.main.invalidate()

            combo.currentIndexChanged.connect(apply)
            name.editingFinished.connect(apply)
            unit.editingFinished.connect(apply)

    def _on_brightness(self, v: int):
        self.brightness_label.setText(f"{v}%")
        self._bright_timer.start()

    def _on_angle(self, i: int):
        self.cfg.set("rotate/value", i * 90)
        self.main.invalidate()
