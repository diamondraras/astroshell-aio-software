"""Theme editor (original: DispThemePage + DispSceneView + per-widget attribute panels)."""
from __future__ import annotations

import itertools
from pathlib import Path

from PySide6.QtCore import QCoreApplication, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QPainter
from PySide6.QtWidgets import (QColorDialog, QComboBox, QDoubleSpinBox, QFileDialog, QFontDialog, QFormLayout,
                               QGraphicsView, QGroupBox, QHBoxLayout, QInputDialog, QLabel, QLineEdit,
                               QMessageBox, QPushButton, QScrollArea, QToolButton, QVBoxLayout, QWidget)

from . import theme_format as tf
from .config import themes_dir
from .media import FILE_FILTER
from .scene import (SENSOR_LABELS, ArcItem, ClockItem, GaugeItem, ImageItem, LabelItem, NumberItem,
                    ProgressBarItem, RectItem, ThemeScene, from_qcolor, to_qcolor)


def tr(s: str) -> str:
    return QCoreApplication.translate("DispThemePage", s)


def _label_record(cls, name, pos, text, size=20.0, bold=True, color=Qt.white):
    font = tf.QFont(family="Arial", style_name="Bold" if bold else "Regular", point_size=size,
                    weight=75 if bold else 50)
    return cls(name=name, pos=pos, color=from_qcolor(QColor(color)), font=font, text=text)


class EditorPage(QWidget):
    theme_saved = Signal()

    def __init__(self, cfg, sensors, main, parent=None):
        super().__init__(parent)
        self.cfg, self.sensors, self.main = cfg, sensors, main
        self.scene = ThemeScene((320, 320), self)
        self.scene.clear_theme()
        self.scene.selectionChanged.connect(self._on_selection)
        self._saved = self._snapshot()
        self._ids = itertools.count(1)

        root = QVBoxLayout(self)
        bar = QHBoxLayout()
        for text, slot in ((tr("New"), self.new_theme), (tr("Open"), self.open_theme),
                           (tr("Save"), self.save_theme), (tr("Save As"), self.save_theme_as)):
            b = QPushButton(text)
            b.clicked.connect(slot)
            bar.addWidget(b)
        bar.addSpacing(20)
        for text, slot in (("▲", self._raise), ("▼", self._lower), (tr("Cut"), self._delete)):
            b = QToolButton()
            b.setText(text)
            b.clicked.connect(slot)
            bar.addWidget(b)
        bar.addStretch(1)
        self.title = QLabel("")
        bar.addWidget(self.title)
        root.addLayout(bar)

        body = QHBoxLayout()
        tools = QGroupBox()
        tv = QVBoxLayout(tools)
        for text, kind in ((tr("Text"), "text"), (tr("Number"), "number"), (tr("Image"), "image"),
                           (tr("Bar"), "bar"), (tr("Arc"), "arc"), ("Gauge", "gauge"), ("Clock", "clock"),
                           ("Date", "date"), ("Time", "time"), ("Weekday", "weekday")):
            b = QPushButton(text)
            b.clicked.connect(lambda _=False, k=kind: self.add_widget(k))
            tv.addWidget(b)
        tv.addStretch(1)
        body.addWidget(tools)

        self.view = QGraphicsView(self.scene)
        self.view.setRenderHints(QPainter.Antialiasing | QPainter.TextAntialiasing | QPainter.SmoothPixmapTransform)
        self.view.setFixedSize(340, 340)
        self.view.setStyleSheet("background:#1d1d1d; border-radius:8px;")
        body.addWidget(self.view, 0, Qt.AlignTop)

        self.props_area = QScrollArea()
        self.props_area.setWidgetResizable(True)
        self.props_area.setMinimumWidth(300)
        body.addWidget(self.props_area, 1)
        root.addLayout(body, 1)

        self._timer = QTimer(self, interval=200, timeout=self._refresh)
        self._timer.start()
        self._on_selection()

    # ------------------------------------------------------------------ file ops
    def _snapshot(self) -> bytes:
        return tf.serialize(self.scene.to_theme())

    @property
    def dirty(self) -> bool:
        return bool(self.scene.widgets) and self._snapshot() != self._saved

    @dirty.setter
    def dirty(self, value: bool):
        if not value:
            self._saved = self._snapshot()

    def _confirm_discard(self) -> bool:
        if not self.dirty:
            return True
        r = QMessageBox.question(self, tr("Warning"), tr("The file is not saved. Whether to save it"),
                                 QMessageBox.Save | QMessageBox.Discard | QMessageBox.Cancel)
        if r == QMessageBox.Save:
            return self.save_theme()
        return r == QMessageBox.Discard

    def new_theme(self):
        if not self._confirm_discard():
            return
        self.scene.clear_theme()
        self.scene.theme_dir = None
        self.title.setText("")
        self.dirty = False

    def open_theme(self):
        if not self._confirm_discard():
            return
        d = QFileDialog.getExistingDirectory(self, tr("theme folder(*)"), str(themes_dir()))
        if not d:
            return
        try:
            self.scene.load(Path(d))
        except Exception as e:
            QMessageBox.warning(self, tr("Warning"), str(e))
            return
        for it in self.scene.items():
            if hasattr(it, "set_editable"):
                it.set_editable(True)
        self.title.setText(Path(d).name)
        self.dirty = False

    def save_theme(self) -> bool:
        if self.scene.theme_dir is None:
            return self.save_theme_as()
        self.scene.save()
        self.dirty = False
        self.theme_saved.emit()
        return True

    def save_theme_as(self) -> bool:
        name, ok = QInputDialog.getText(self, tr("Save As File"), "name:")
        name = name.strip()
        if not ok or not name:
            return False
        target = themes_dir() / f"theme_{name}"
        if target.exists() and target != self.scene.theme_dir:
            QMessageBox.warning(self, tr("Warning"), QCoreApplication.translate("QObject", "theme name is exit"))
            return False
        self.scene.save(target)
        self.title.setText(target.name)
        self.dirty = False
        self.theme_saved.emit()
        return True

    # ------------------------------------------------------------------ widgets
    def add_widget(self, kind: str):
        n = next(self._ids)
        mid = (110.0, 140.0)
        if kind == "text":
            rec = _label_record(tf.TextWidget, f"text_{n}", mid, "input text here!", 16, False)
        elif kind in ("date", "time", "weekday"):
            cls = {"date": tf.DateWidget, "time": tf.TimeWidget, "weekday": tf.WeekdayWidget}[kind]
            rec = _label_record(cls, f"{kind}_{n}_datetime", mid, "", 20)
        elif kind == "number":
            rec = _label_record(tf.NumberWidget, f"data_{n}", mid, "00", 24)
            rec.sensor = 3
            rec.unit = _label_record(tf.LabelItem, f"data_{n}_unit", (mid[0] + 40, mid[1] + 6), "%", 12)
            rec.label = _label_record(tf.LabelItem, f"data_{n}_name", (mid[0] - 10, mid[1] - 28), "CPU Usage", 12)
        elif kind == "image":
            p, _ = QFileDialog.getOpenFileName(self, "Open Image", "", FILE_FILTER)
            if not p:
                return
            if self.scene.theme_dir is None and not self.save_theme_as():
                return
            rel = self.scene.import_resource(p)
            rec = tf.ImageWidget(f"image_{n}", (0.0, 0.0, 320.0, 320.0), rel)
        elif kind == "bar":
            rec = tf.ProgressBarWidget(f"progress_bar_{n}", (60.0, 150.0, 200.0, 20.0), 3)
        elif kind == "arc":
            rec = tf.ArcWidget(f"arc_{n}", (110.0, 110.0, 100.0, 100.0), 3)
        elif kind == "gauge":
            rec = tf.GaugeWidget(f"gauge_{n}", (110.0, 110.0, 100.0, 100.0), 3)
        elif kind == "clock":
            rec = tf.AnalogClockWidget(f"clock_{n}", (85.0, 85.0, 150.0, 150.0))
        else:
            return
        item = self.scene.add_record(rec)
        for it in [item, *(item.children_items() if isinstance(item, NumberItem) else [])]:
            it.set_editable(True)
        self.scene.clearSelection()
        item.setSelected(True)

    def _selected(self):
        sel = self.scene.selectedItems()
        if not sel:
            return None
        it = sel[0]
        # children of number widgets resolve to their owner for z-order / delete
        for w in self.scene.widgets:
            if isinstance(w, NumberItem) and it in w.children_items():
                return w if it is not w else it
        return it

    def _raise(self):
        it = self._selected()
        if it in self.scene.widgets:
            i = self.scene.widgets.index(it)
            if i < len(self.scene.widgets) - 1:
                self.scene.widgets[i], self.scene.widgets[i + 1] = self.scene.widgets[i + 1], it
            self._restack()

    def _lower(self):
        it = self._selected()
        if it in self.scene.widgets:
            i = self.scene.widgets.index(it)
            if i > 0:
                self.scene.widgets[i], self.scene.widgets[i - 1] = self.scene.widgets[i - 1], it
            self._restack()

    def _restack(self):
        for z, w in enumerate(self.scene.widgets):
            w.setZValue(z)
            if isinstance(w, NumberItem):
                for c in w.children_items():
                    c.setZValue(z)

    def _delete(self):
        it = self._selected()
        if it is not None and it in self.scene.widgets:
            self.scene.remove_widget(it)

    def _refresh(self):
        if self.isVisible():
            self.scene.update_values(self.sensors.snapshot(), fahrenheit=self.cfg.get("temp/uint") == 1)

    # ------------------------------------------------------------------ property panel
    def _on_selection(self):
        panel = QWidget()
        form = QFormLayout(panel)
        sel = self.scene.selectedItems()
        if not sel:
            form.addRow(QLabel("Select a widget, or add one from the left."))
            self.props_area.setWidget(panel)
            return
        it = sel[0]
        form.addRow(QLabel(f"<b>{it.objectName()}</b>"))

        def spin(value, setter, lo=-2000, hi=2000):
            s = QDoubleSpinBox()
            s.setRange(lo, hi)
            s.setDecimals(1)
            s.setValue(value)
            s.valueChanged.connect(setter)
            return s

        form.addRow("x:", spin(it.pos().x(), lambda v: it.setPos(v, it.pos().y())))
        form.addRow("y:", spin(it.pos().y(), lambda v: it.setPos(it.pos().x(), v)))

        if isinstance(it, RectItem):
            form.addRow("w:", spin(it.w, lambda v: (it.resize(v, it.h), self._reload_media(it)), 1))
            form.addRow("h:", spin(it.h, lambda v: (it.resize(it.w, v), self._reload_media(it)), 1))

        if isinstance(it, LabelItem):
            if it.kind in (2, -1, -2):
                text = QLineEdit(it.toPlainText())
                text.textChanged.connect(it.set_text)
                form.addRow("Text", text)
            btn_color = QPushButton("color")
            btn_color.setStyleSheet(f"background:{it.defaultTextColor().name()}; color:black;")

            def pick_color():
                c = QColorDialog.getColor(it.defaultTextColor(), self, "color", QColorDialog.ShowAlphaChannel)
                if c.isValid():
                    it.setDefaultTextColor(c)
                    btn_color.setStyleSheet(f"background:{c.name()}; color:black;")
            btn_color.clicked.connect(pick_color)
            form.addRow("color:", btn_color)
            btn_font = QPushButton(f"{it.font().family()} {it.font().pointSizeF():.0f}")

            def pick_font():
                ok, f = QFontDialog.getFont(it.font(), self)
                if ok:
                    it.setFont(f)
                    btn_font.setText(f"{f.family()} {f.pointSizeF():.0f}")
            btn_font.clicked.connect(pick_font)
            form.addRow("font:", btn_font)

        if hasattr(it, "sensor") and isinstance(getattr(it, "record", None),
                                                (tf.NumberWidget, tf.ProgressBarWidget, tf.ArcWidget,
                                                 tf.GaugeWidget)):
            combo = QComboBox()
            for sid, label in SENSOR_LABELS.items():
                combo.addItem(label, sid)
            combo.setCurrentIndex(max(0, combo.findData(it.record.sensor)))
            combo.currentIndexChanged.connect(lambda _: setattr(it.record, "sensor", combo.currentData()))
            form.addRow("sensor:", combo)

        if isinstance(it, ImageItem):
            path = QPushButton(it.record.file or "open...")

            def pick_file():
                p, _ = QFileDialog.getOpenFileName(self, "Open Image", "", FILE_FILTER)
                if p:
                    it.record.file = self.scene.import_resource(p)
                    it.load()
                    path.setText(it.record.file)
            path.clicked.connect(pick_file)
            form.addRow("path:", path)

        if isinstance(it, ProgressBarItem):
            st = it.record.style
            form.addRow("Background color:", self._color_btn(st, "color_4c", it))
            form.addRow("color:", self._color_btn(st, "color_5c", it))
            form.addRow("border:", spin(st.float_38, lambda v: (setattr(st, "float_38", v), it.update()), 0, 50))
        if isinstance(it, ArcItem):
            st = it.record.style
            form.addRow("Width:", spin(st.float_60, lambda v: (setattr(st, "float_60", v), it.update()), 1, 200))
            form.addRow("Start:", spin(st.float_64, lambda v: (setattr(st, "float_64", v), it.update()), -360, 360))
            form.addRow("Total:", spin(st.float_6c, lambda v: (setattr(st, "float_6c", v), it.update()), 1, 360))
            form.addRow("Background color:", self._color_btn(st, "color_34", it))
            form.addRow("color:", self._color_btn(st, "color_44", it))
        if isinstance(it, ClockItem):
            form.addRow(QLabel("Hand/dial images: edit via theme res/ folder"))
        if isinstance(it, GaugeItem):
            form.addRow(QLabel("Needle gauge"))
        self.props_area.setWidget(panel)

    def _color_btn(self, style, attr, item):
        b = QPushButton("color")
        b.setStyleSheet(f"background:{to_qcolor(getattr(style, attr)).name()}; color:black;")

        def pick():
            c = QColorDialog.getColor(to_qcolor(getattr(style, attr)), self, "color", QColorDialog.ShowAlphaChannel)
            if c.isValid():
                setattr(style, attr, from_qcolor(c))
                if attr in ("color_44", "color_5c"):
                    style.stops = []  # a solid colour replaces the gradient
                b.setStyleSheet(f"background:{c.name()}; color:black;")
                item.update()
        b.clicked.connect(pick)
        return b

    @staticmethod
    def _reload_media(it):
        if isinstance(it, ImageItem):
            it.load()

