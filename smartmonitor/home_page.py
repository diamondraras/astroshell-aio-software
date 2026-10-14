"""Home page (original: DispHomePage): live preview, local themes, background/mask, sensors S1..S6."""
from __future__ import annotations

import logging
import shutil
from pathlib import Path

from PySide6.QtCore import QCoreApplication, QSize, Qt, QTimer
from PySide6.QtGui import QIcon, QImage, QPixmap
from PySide6.QtWidgets import (QComboBox, QFileDialog, QGridLayout, QGroupBox, QHBoxLayout, QLabel, QLineEdit,
                               QListView, QListWidget, QListWidgetItem, QMenu, QMessageBox, QPushButton,
                               QSlider, QVBoxLayout, QWidget)

from . import theme_format as tf
from .config import themes_dir
from .media import FILE_FILTER
from .scene import SENSOR_LABELS, ImageItem, NumberItem

log = logging.getLogger(__name__)
PREVIEW_FILE = "preview.png"


def tr(s: str) -> str:
    return QCoreApplication.translate("DispHomePage", s)


class HomePage(QWidget):
    def __init__(self, cfg, renderer, main, parent=None):
        super().__init__(parent)
        self.cfg, self.renderer, self.main = cfg, renderer, main
        root = QHBoxLayout(self)

        # ---- left column: preview + device controls
        left = QVBoxLayout()
        self.preview = QLabel()
        self.preview.setFixedSize(320, 320)
        self.preview.setStyleSheet("background:#000; border:1px solid #333;")
        left.addWidget(self.preview, 0, Qt.AlignHCenter)

        dev = QGroupBox()
        g = QGridLayout(dev)
        g.addWidget(QLabel(tr("Brightness:")), 0, 0)
        self.brightness = QSlider(Qt.Horizontal)
        self.brightness.setRange(0, 100)
        self.brightness.setValue(cfg.get("brightness/value"))
        self.brightness_label = QLabel(f"{self.brightness.value()}%")
        self._bright_timer = QTimer(self, singleShot=True, interval=150,
                                    timeout=lambda: main.set_brightness(self.brightness.value()))
        self.brightness.valueChanged.connect(self._on_brightness)
        g.addWidget(self.brightness, 0, 1)
        g.addWidget(self.brightness_label, 0, 2)

        g.addWidget(QLabel(tr("Angle:")), 1, 0)
        self.angle = QComboBox()
        self.angle.addItems(["0°", "90°", "180°", "270°"])
        self.angle.setCurrentIndex((cfg.get("rotate/value") // 90) % 4)
        self.angle.currentIndexChanged.connect(lambda i: cfg.set("rotate/value", i * 90))
        g.addWidget(self.angle, 1, 1, 1, 2)

        self.capture = QPushButton(tr("Screen\nCapture").replace("\n", " "))
        self.capture.setCheckable(True)
        self.capture.toggled.connect(lambda on: setattr(main, "screen_capture", on))
        g.addWidget(self.capture, 2, 0, 1, 3)
        left.addWidget(dev)
        left.addStretch(1)
        root.addLayout(left)

        # ---- right column: themes + editing of current theme
        right = QVBoxLayout()
        box = QGroupBox()
        v = QVBoxLayout(box)
        head = QHBoxLayout()
        head.addWidget(QLabel(tr("Local Theme")))
        head.addStretch(1)
        for text, slot in ((tr("Import"), self._import), (tr("Export"), self._export)):
            b = QPushButton(text)
            b.clicked.connect(slot)
            head.addWidget(b)
        v.addLayout(head)
        self.themes = QListWidget()
        self.themes.setViewMode(QListView.IconMode)
        self.themes.setIconSize(QSize(96, 96))
        self.themes.setResizeMode(QListView.Adjust)
        self.themes.setMovement(QListView.Static)
        self.themes.setSpacing(6)
        self.themes.setGridSize(QSize(112, 124))
        self.themes.setUniformItemSizes(True)
        self.themes.setContextMenuPolicy(Qt.CustomContextMenu)
        self.themes.customContextMenuRequested.connect(self._theme_menu)
        self.themes.itemClicked.connect(lambda it: self.load_theme(it.data(Qt.UserRole)))
        v.addWidget(self.themes)
        right.addWidget(box, 3)

        bg = QGroupBox()
        h = QHBoxLayout(bg)
        h.addWidget(QLabel(tr("Background")))
        for text, slot in ((tr("Load Image"), self._load_image), (tr("Load Video"), self._load_video),
                           (tr("Load Mask"), self._load_mask)):
            b = QPushButton(text)
            b.clicked.connect(slot)
            h.addWidget(b)
        h.addStretch(1)
        right.addWidget(bg)

        self.sys_box = QGroupBox()
        self.sys_grid = QGridLayout(self.sys_box)
        right.addWidget(self.sys_box, 0)
        root.addLayout(right, 1)

        self.reload_themes()
        self._thumb_timer = QTimer(self, singleShot=True, interval=3000, timeout=self._save_thumbnail)

    # ------------------------------------------------------------------ themes
    def reload_themes(self):
        self.themes.clear()
        for d in sorted(themes_dir().glob("theme_*")):
            if not d.is_dir():
                continue
            name = d.name.removeprefix("theme_")
            it = QListWidgetItem(self._thumbnail(d), name)
            it.setData(Qt.UserRole, name)
            self.themes.addItem(it)
            if name == self.renderer.theme_name:
                it.setSelected(True)

    @staticmethod
    def _thumbnail(d: Path) -> QIcon:
        cand = [d / PREVIEW_FILE] + sorted((d / "res").glob("*.png")) + sorted((d / "res").glob("*.jpg"))
        for c in cand:
            if c.exists():
                pm = QPixmap(str(c))
                if not pm.isNull():
                    return QIcon(pm.scaled(96, 96, Qt.KeepAspectRatio, Qt.SmoothTransformation))
        pm = QPixmap(96, 96)
        pm.fill(Qt.darkGray)
        return QIcon(pm)

    def load_theme(self, name: str):
        if not self.renderer.load(name):
            QMessageBox.warning(self, tr("Error"), tr("file %1 not found").replace("%1", name))
            return
        self._build_sensor_slots()
        self._thumb_timer.start()

    def _save_thumbnail(self):
        if self.renderer.theme_name and self.renderer.scene.theme_dir:
            img = self.renderer.scene.render_image()
            img.save(str(self.renderer.scene.theme_dir / PREVIEW_FILE))
            for i in range(self.themes.count()):
                it = self.themes.item(i)
                if it.data(Qt.UserRole) == self.renderer.theme_name:
                    it.setIcon(QIcon(QPixmap.fromImage(img).scaled(96, 96)))

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
            if name == self.renderer.theme_name:
                self.renderer.scene.clear_theme()
                self.renderer.theme_name = None
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
            return
        dst = QFileDialog.getExistingDirectory(self, tr("Export"))
        if dst:
            src = themes_dir() / f"theme_{self.renderer.theme_name}"
            shutil.copytree(src, Path(dst) / src.name, dirs_exist_ok=True)

    # ------------------------------------------------------------------ background / mask
    def _replace_image(self, object_name: str, path: str, insert_at: int):
        scene = self.renderer.scene
        if scene.theme_dir is None:
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
        self.sys_grid.addWidget(QLabel(QCoreApplication.translate("DispHomeSysInfoEditBox", "System\nInfo")
                                       .replace("\n", " ")), 0, 0, 1, 4)
        items = [w for w in self.renderer.scene.widgets
                 if isinstance(w, NumberItem) and w.objectName().startswith("data_home_s")]
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

            combo.currentIndexChanged.connect(apply)
            name.editingFinished.connect(apply)
            unit.editingFinished.connect(apply)

    def _on_brightness(self, v: int):
        self.brightness_label.setText(f"{v}%")
        self._bright_timer.start()
