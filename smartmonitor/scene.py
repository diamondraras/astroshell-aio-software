"""Theme scene: turns a parsed .theme into QGraphicsItems (original: DispScene + Disp*Widget).

The same scene is rendered off-screen for the device and shown in the editor's QGraphicsView.
Positions, fonts and colours come straight from the theme file; text items are QGraphicsTextItem
like the original, so layout matches.
"""
from __future__ import annotations

import copy
import logging
import math
import shutil
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QDateTime, QLocale, QPointF, QRectF, Qt
from PySide6.QtGui import (QBrush, QColor, QConicalGradient, QFont, QGuiApplication, QImage, QLinearGradient,
                           QPainter, QPen)
from PySide6.QtWidgets import QGraphicsItem, QGraphicsObject, QGraphicsScene, QGraphicsTextItem

from . import theme_format as tf
from .media import MediaSource

log = logging.getLogger(__name__)

# Sensor ids used in .theme files (FUN_140038da0) -> SensorManager keys.
# Ids 7..10 are extensions of this re-implementation (the original stops at 6).
SENSOR_IDS = {
    1: "cpu_freq", 2: "cpu_temp", 3: "cpu_usage",
    4: "gpu_freq", 5: "gpu_temp", 6: "gpu_usage",
    7: "mem_usage", 8: "disk_usage", 9: "net_down", 10: "net_up",
}
SENSOR_LABELS = {
    0: "None", 1: "CPU Freq", 2: "CPU Temp", 3: "CPU Usage", 4: "GPU Freq", 5: "GPU Temp",
    6: "GPU Usage", 7: "RAM Usage", 8: "Disk Usage", 9: "Download", 10: "Upload",
}
SENSOR_RANGES = {1: (0, 10000), 2: (0, 120), 3: (0, 100), 4: (0, 10000), 5: (0, 120), 6: (0, 100),
                 7: (0, 100), 8: (0, 100), 9: (0, 102400), 10: (0, 102400)}

TIME_FORMATS = {"24H": "hh:mm:ss", "12H": "hh:mm:ss ap"}
DATE_FORMATS = {"Y/M/D": "yyyy/MM/dd", "M/D": "MM/dd", "D/M/Y": "dd/MM/yyyy", "D/M": "dd/MM"}


# --------------------------------------------------------------------------- conversions
def to_qcolor(c: tf.QColor | None, default=Qt.white) -> QColor:
    if c is None or c.spec == 0:
        return QColor(default)
    r, g, b, a = c.rgba8()
    return QColor(r, g, b, a)


def from_qcolor(c: QColor) -> tf.QColor:
    return tf.QColor.from_rgba8(c.red(), c.green(), c.blue(), c.alpha())


def to_qfont(f: tf.QFont) -> QFont:
    q = QFont(f.family or "Arial")
    if f.style_name:
        q.setStyleName(f.style_name)
    if f.pixel_size > 0:
        q.setPixelSize(f.pixel_size)
    elif f.point_size > 0:
        q.setPointSizeF(f.point_size)
    q.setWeight(_qt5_weight_to_qt6(f.weight))
    q.setItalic(bool(f.bits & 0x01))
    q.setUnderline(bool(f.bits & 0x02))
    q.setStrikeOut(bool(f.bits & 0x04))
    return q


def from_qfont(q: QFont, base: tf.QFont | None = None) -> tf.QFont:
    f = copy.deepcopy(base) if base else tf.QFont()
    f.family = q.family()
    f.style_name = q.styleName()
    if q.pixelSize() > 0:
        f.pixel_size, f.point_size = q.pixelSize(), -1.0
    else:
        f.pixel_size, f.point_size = -1, q.pointSizeF()
    f.weight = _qt6_weight_to_qt5(q.weight())
    bits = f.bits & ~0x07
    bits |= 0x01 if q.italic() else 0
    bits |= 0x02 if q.underline() else 0
    bits |= 0x04 if q.strikeOut() else 0
    f.bits = bits
    return f


# Qt5 weights (0..99) in the file vs Qt6 weights (100..900) in PySide6.
_W5 = [(0, 100), (12, 200), (25, 300), (50, 400), (57, 500), (63, 600), (75, 700), (81, 800), (87, 900)]


def _qt5_weight_to_qt6(w5: int) -> QFont.Weight:
    best = min(_W5, key=lambda p: abs(p[0] - w5))[1]
    return QFont.Weight(best)


def _qt6_weight_to_qt5(w6) -> int:
    w6 = int(w6.value if hasattr(w6, "value") else w6)
    return min(_W5, key=lambda p: abs(p[1] - w6))[0]


# --------------------------------------------------------------------------- items
class EditableMixin:
    """Common editor behaviour (select / move)."""

    def set_editable(self, on: bool):
        self.setFlag(QGraphicsItem.ItemIsMovable, on)
        self.setFlag(QGraphicsItem.ItemIsSelectable, on)


class LabelItem(EditableMixin, QGraphicsTextItem):
    """Types 2..5 and Number widget value/unit/name labels."""

    def __init__(self, record, kind: int):
        super().__init__(record.text or "")
        self.record = record
        self.kind = kind
        self.setObjectName(record.name or "")
        self.setPos(QPointF(*record.pos))
        self.setDefaultTextColor(to_qcolor(record.color))
        self.setFont(to_qfont(record.font))
        self.fmt = None  # runtime format for date/time

    def to_record(self):
        r = self.record
        r.pos = (self.pos().x(), self.pos().y())
        r.color = from_qcolor(self.defaultTextColor())
        r.font = from_qfont(self.font(), r.font)
        r.text = self.toPlainText()
        return r

    def set_text(self, text: str):
        if self.toPlainText() != text:
            self.setPlainText(text)


class NumberItem(LabelItem):
    """Type 6: sensor value with optional unit and name labels (separate scene items)."""

    def __init__(self, record: tf.NumberWidget):
        super().__init__(record, 6)
        self.unit_item = LabelItem(record.unit, -1) if record.unit else None
        self.name_item = LabelItem(record.label, -2) if record.label else None

    @property
    def sensor(self) -> int:
        return self.record.sensor

    def children_items(self):
        return [i for i in (self.unit_item, self.name_item) if i is not None]

    def to_record(self):
        r = super().to_record()
        if self.unit_item:
            r.unit = self.unit_item.to_record()
        if self.name_item:
            r.label = self.name_item.to_record()
        return r


class RectItem(EditableMixin, QGraphicsObject):
    """Base for rect-based widgets; the item's pos is the rect's top-left."""

    def __init__(self, record):
        super().__init__()
        self.record = record
        x, y, w, h = record.rect
        self.setPos(x, y)
        self.w, self.h = w, h
        self.setObjectName(record.name or "")

    def boundingRect(self) -> QRectF:
        return QRectF(0, 0, self.w, self.h)

    def resize(self, w: float, h: float):
        self.prepareGeometryChange()
        self.w, self.h = w, h

    def to_record(self):
        self.record.rect = (self.pos().x(), self.pos().y(), self.w, self.h)
        return self.record

    def paint_selection(self, p: QPainter):
        if self.isSelected():
            p.setPen(QPen(QColor("#b49653"), 1, Qt.DashLine))
            p.setBrush(Qt.NoBrush)
            p.drawRect(self.boundingRect())


class ImageItem(RectItem):
    """Type 1: background/mask/free image, GIF or video."""

    def __init__(self, record: tf.ImageWidget, res_dir: Path):
        super().__init__(record)
        self.res_dir = res_dir
        self.source: MediaSource | None = None
        self.load()

    def load(self):
        if self.source:
            self.source.close()
        self.source = None
        if self.record.file:
            self.source = MediaSource(self.res_dir / self.record.file, (int(self.w), int(self.h)))

    def paint(self, p: QPainter, opt, widget=None):
        img = self.source.current() if self.source else None
        if img is not None:
            p.drawImage(self.boundingRect(), img)
        self.paint_selection(p)

    def close(self):
        if self.source:
            self.source.close()


class ProgressBarItem(RectItem):
    """Type 8. Style field meanings are inferred (see THEME_FORMAT.md)."""

    def __init__(self, record: tf.ProgressBarWidget):
        super().__init__(record)
        if not isinstance(record.style, tf.ProgressBarStyle):
            record.style = tf.ProgressBarStyle(color_4c=from_qcolor(QColor(Qt.gray)),
                                               color_5c=from_qcolor(QColor(Qt.red)))
        self.value = 0.0

    @property
    def sensor(self):
        return self.record.sensor

    def paint(self, p: QPainter, opt, widget=None):
        st: tf.ProgressBarStyle = self.record.style
        lo, hi = SENSOR_RANGES.get(self.record.sensor, (0, 100))
        frac = 0.0 if hi <= lo else max(0.0, min(1.0, (self.value - lo) / (hi - lo)))
        rect = self.boundingRect()
        radius = min(rect.height(), rect.width()) / 2 if st.flag_32 else 0
        p.setRenderHint(QPainter.Antialiasing)
        border = st.float_38
        p.setPen(QPen(to_qcolor(st.color_4c), border) if border > 0 and st.flag_33 else Qt.NoPen)
        p.setBrush(Qt.NoBrush if st.flag_31 else QBrush(to_qcolor(st.color_4c)))
        p.drawRoundedRect(rect, radius, radius)
        vertical = rect.height() > rect.width()
        fill = QRectF(rect)
        if vertical:
            fill.setTop(rect.bottom() - rect.height() * frac)
        else:
            fill.setWidth(rect.width() * frac)
        if st.stops:
            g = QLinearGradient(rect.bottomLeft() if vertical else rect.topLeft(),
                                rect.topLeft() if vertical else rect.topRight())
            for s in st.stops:
                g.setColorAt(s.position, to_qcolor(s.color))
            brush = QBrush(g)
        else:
            brush = QBrush(to_qcolor(st.color_5c))
        p.setPen(Qt.NoPen)
        p.setBrush(brush)
        if frac > 0:
            p.drawRoundedRect(fill, radius, radius)
        self.paint_selection(p)


class ArcItem(RectItem):
    """Type 9: circular progress bar. Defaults: width 10, start 90, total 360, range 0..100."""

    def __init__(self, record: tf.ArcWidget):
        super().__init__(record)
        if not isinstance(record.style, tf.ArcStyle):
            record.style = tf.ArcStyle(
                color_34=from_qcolor(QColor(Qt.gray)), color_44=from_qcolor(QColor(Qt.white)),
                stops=[tf.GradientStop(1.0, from_qcolor(QColor(Qt.green))),
                       tf.GradientStop(0.5, from_qcolor(QColor(Qt.yellow))),
                       tf.GradientStop(0.0, from_qcolor(QColor(Qt.red)))])
        self.value = 0.0

    @property
    def sensor(self):
        return self.record.sensor

    def paint(self, p: QPainter, opt, widget=None):
        st: tf.ArcStyle = self.record.style
        lo, hi = SENSOR_RANGES.get(self.record.sensor, (st.float_70, st.float_74))
        frac = 0.0 if hi <= lo else max(0.0, min(1.0, (self.value - lo) / (hi - lo)))
        width, start, total = st.float_60, st.float_64, st.float_6c
        r = self.boundingRect().adjusted(width / 2, width / 2, -width / 2, -width / 2)
        p.setRenderHint(QPainter.Antialiasing)
        cap = Qt.RoundCap if st.flag_30 else Qt.FlatCap
        if st.flag_31:  # background track ("Solid"); otherwise transparent
            p.setPen(QPen(to_qcolor(st.color_34), width, Qt.SolidLine, cap))
            p.drawArc(r, int(start * 16), int(-total * 16))
        if frac > 0:
            if st.stops:
                g = QConicalGradient(r.center(), start)
                for s in st.stops:
                    g.setColorAt(s.position, to_qcolor(s.color))
                pen = QPen(QBrush(g), width, Qt.SolidLine, cap)
            else:
                pen = QPen(to_qcolor(st.color_44), width, Qt.SolidLine, cap)
            p.setPen(pen)
            p.drawArc(r, int(start * 16), int(-total * frac * 16))
        self.paint_selection(p)


class GaugeItem(RectItem):
    """Type 10: needle gauge (only geometry + sensor are stored in the file)."""

    def __init__(self, record: tf.GaugeWidget):
        super().__init__(record)
        self.value = 0.0

    @property
    def sensor(self):
        return self.record.sensor

    def paint(self, p: QPainter, opt, widget=None):
        lo, hi = SENSOR_RANGES.get(self.record.sensor, (0, 100))
        frac = 0.0 if hi <= lo else max(0.0, min(1.0, (self.value - lo) / (hi - lo)))
        rect = self.boundingRect().adjusted(4, 4, -4, -4)
        p.setRenderHint(QPainter.Antialiasing)
        p.setPen(QPen(QColor(255, 255, 255, 160), 3, Qt.SolidLine, Qt.RoundCap))
        p.drawArc(rect, 225 * 16, -270 * 16)
        c = rect.center()
        ang = math.radians(225 - 270 * frac)
        rad = min(rect.width(), rect.height()) / 2 - 6
        p.setPen(QPen(QColor("#ff5533"), 3, Qt.SolidLine, Qt.RoundCap))
        p.drawLine(c, QPointF(c.x() + rad * math.cos(ang), c.y() - rad * math.sin(ang)))
        p.setBrush(QColor("#ffffff"))
        p.drawEllipse(c, 4, 4)
        self.paint_selection(p)


class ClockItem(RectItem):
    """Type 11: analog clock with optional dial / hand images."""

    def __init__(self, record: tf.AnalogClockWidget, res_dir: Path):
        super().__init__(record)
        if not isinstance(record.style, tf.ClockStyle):
            record.style = tf.ClockStyle(color=from_qcolor(QColor(Qt.white)))
        st = record.style
        self.images = {k: QImage(str(res_dir / f)) if f else None for k, f in (
            ("dial", st.dial_image), ("hour", st.hour_image), ("minute", st.minute_image),
            ("second", st.second_image))}

    def paint(self, p: QPainter, opt, widget=None):
        st: tf.ClockStyle = self.record.style
        rect = self.boundingRect()
        p.setRenderHint(QPainter.Antialiasing)
        p.setRenderHint(QPainter.SmoothPixmapTransform)
        if self.images["dial"] is not None and not self.images["dial"].isNull():
            p.drawImage(rect, self.images["dial"])
        now = datetime.now()
        angles = {"hour": (now.hour % 12 + now.minute / 60) * 30, "minute": (now.minute + now.second / 60) * 6,
                  "second": now.second * 6}
        lengths = {"hour": 0.5, "minute": 0.75, "second": 0.85}
        widths = {"hour": 4, "minute": 3, "second": 1.5}
        c = rect.center()
        rad = min(rect.width(), rect.height()) / 2
        for hand in ("hour", "minute", "second"):
            img = self.images[hand]
            p.save()
            p.translate(c)
            p.rotate(angles[hand])
            if img is not None and not img.isNull():
                scale = rect.height() / max(1, img.height())
                w, h = img.width() * scale, img.height() * scale
                p.drawImage(QRectF(-w / 2, -h / 2, w, h), img)
            else:
                color = to_qcolor(st.color) if hand != "second" else QColor("#ff4040")
                p.setPen(QPen(color, widths[hand], Qt.SolidLine, Qt.RoundCap))
                p.drawLine(QPointF(0, 0), QPointF(0, -rad * lengths[hand]))
            p.restore()
        self.paint_selection(p)


class ScreenCaptureItem(RectItem):
    """Type 7: mirrors a desktop region."""

    def __init__(self, record: tf.ScreenCaptureWidget):
        rect = (*record.pos, *(record.size if record.size[0] > 0 else (320, 320)))
        record_proxy = tf.ImageWidget(record.name, rect, "")
        super().__init__(record_proxy)
        self.capture = record
        self.frame: QImage | None = None

    def grab(self):
        screen = QGuiApplication.primaryScreen()
        x, y, w, h = self.capture.capture_rect
        if w <= 0 or h <= 0:
            g = screen.geometry()
            x, y, w, h = 0, 0, g.width(), g.height()
        self.frame = screen.grabWindow(0, int(x), int(y), int(w), int(h)).toImage()

    def paint(self, p: QPainter, opt, widget=None):
        if self.frame is not None:
            p.drawImage(self.boundingRect(), self.frame)
        self.paint_selection(p)

    def to_record(self):
        self.capture.pos = (self.pos().x(), self.pos().y())
        self.capture.size = (int(self.w), int(self.h))
        return self.capture


# --------------------------------------------------------------------------- scene
class ThemeScene(QGraphicsScene):
    def __init__(self, size=(320, 320), parent=None):
        super().__init__(0, 0, size[0], size[1], parent)
        self.size = size
        self.theme_dir: Path | None = None
        self.settings: tf.SceneSettings | None = None
        self.records: list = []
        self.widgets: list = []      # top-level items in z-order
        self.raw_records: list = []  # undecodable records kept for lossless saving
        self.time_format = TIME_FORMATS["24H"]
        self.date_format = DATE_FORMATS["Y/M/D"]
        self.locale = QLocale(QLocale.English)

    # load / save -------------------------------------------------------------
    @property
    def res_dir(self) -> Path:
        return (self.theme_dir or Path(".")) / "res"

    def clear_theme(self):
        for it in self.widgets:
            if hasattr(it, "close"):
                it.close()
        self.clear()
        self.widgets, self.raw_records, self.records = [], [], []
        self.settings = tf.SceneSettings(width=self.size[0], height=self.size[1])
        self._apply_background()

    def load(self, theme_dir: Path):
        theme_dir = Path(theme_dir)
        name = theme_dir.name.removeprefix("theme_")
        theme_file = theme_dir / f"{name}.theme"
        if not theme_file.exists():
            found = list(theme_dir.glob("*.theme"))
            if not found:
                raise FileNotFoundError(f"file {theme_file} not found")
            theme_file = found[0]
        self.clear_theme()
        self.theme_dir = theme_dir
        parsed = tf.parse_file(theme_file)
        for rec in parsed["records"]:
            self.add_record(rec)
        self._apply_background()

    def add_record(self, rec):
        if isinstance(rec, tf.SceneSettings):
            self.settings = rec
            return None
        if isinstance(rec, tf.RawRecord):
            log.warning("keeping undecodable record: %s", rec.error)
            self.raw_records.append(rec)
            return None
        item = self._make_item(rec)
        if item is None:
            return None
        self.addItem(item)
        item.setZValue(len(self.widgets))
        self.widgets.append(item)
        if isinstance(item, NumberItem):
            for child in item.children_items():
                self.addItem(child)
                child.setZValue(item.zValue())
        return item

    def _make_item(self, rec):
        if isinstance(rec, tf.ImageWidget):
            return ImageItem(rec, self.res_dir)
        if isinstance(rec, tf.NumberWidget):
            return NumberItem(rec)
        if isinstance(rec, (tf.TextWidget, tf.DateWidget, tf.TimeWidget, tf.WeekdayWidget)):
            return LabelItem(rec, rec.TYPE)
        if isinstance(rec, tf.ProgressBarWidget):
            return ProgressBarItem(rec)
        if isinstance(rec, tf.ArcWidget):
            return ArcItem(rec)
        if isinstance(rec, tf.GaugeWidget):
            return GaugeItem(rec)
        if isinstance(rec, tf.AnalogClockWidget):
            return ClockItem(rec, self.res_dir)
        if isinstance(rec, tf.ScreenCaptureWidget):
            return ScreenCaptureItem(rec)
        return None

    def remove_widget(self, item):
        if item in self.widgets:
            self.widgets.remove(item)
        if isinstance(item, NumberItem):
            for c in item.children_items():
                self.removeItem(c)
        if hasattr(item, "close"):
            item.close()
        self.removeItem(item)

    def to_theme(self) -> dict:
        self.widgets.sort(key=lambda i: i.zValue())
        records = [w.to_record() for w in self.widgets] + list(self.raw_records)
        st = self.settings or tf.SceneSettings()
        st.width, st.height = self.size
        records.append(st)
        return {"records": records}

    def save(self, theme_dir: Path | None = None):
        theme_dir = Path(theme_dir or self.theme_dir)
        theme_dir.mkdir(parents=True, exist_ok=True)
        (theme_dir / "res").mkdir(exist_ok=True)
        if self.theme_dir and theme_dir.resolve() != self.theme_dir.resolve() and self.res_dir.exists():
            shutil.copytree(self.res_dir, theme_dir / "res", dirs_exist_ok=True)
        name = theme_dir.name.removeprefix("theme_")
        tf.write_file(theme_dir / f"{name}.theme", self.to_theme())
        self.theme_dir = theme_dir

    def import_resource(self, path: str | Path) -> str:
        """Copy a media file into <theme>/res and return its relative name."""
        src = Path(path)
        self.res_dir.mkdir(parents=True, exist_ok=True)
        dst = self.res_dir / src.name
        if src.resolve() != dst.resolve():
            shutil.copy2(src, dst)
        return src.name

    # runtime -----------------------------------------------------------------
    def _apply_settings(self):
        """datetime_flags / sensor_flags semantics from docs/THEME_FORMAT.md (FUN_140017680, FUN_1400256a0)."""
        st = self.settings
        if st is None:
            return
        dt = st.datetime_flags
        for idx, fmt in zip(range(1, 5), DATE_FORMATS.values()):
            if dt[idx]:
                self.date_format = fmt
        if dt[5]:
            self.time_format = TIME_FORMATS["12H"]
        elif dt[6]:
            self.time_format = TIME_FORMATS["24H"]
        sf = st.sensor_flags
        if any(sf):
            for it in self.widgets:
                name = it.objectName()
                if isinstance(it, NumberItem) and name.startswith("data_home_s"):
                    try:
                        slot = int(name.removeprefix("data_home_s"))
                    except ValueError:
                        continue
                    visible = sf[0] and (slot > 6 or sf[slot])
                    for i in [it, *it.children_items()]:
                        i.setVisible(visible)

    def _apply_background(self):
        self._apply_settings()
        st = self.settings
        if st is None or not st.colors:
            self.setBackgroundBrush(QColor("black"))
            return
        if st.background_mode == 2 and len(st.colors) == 2:
            g = QLinearGradient(0, 0, 0, self.size[1])
            g.setColorAt(0, to_qcolor(st.colors[0], Qt.black))
            g.setColorAt(1, to_qcolor(st.colors[1], Qt.black))
            self.setBackgroundBrush(QBrush(g))
        else:
            self.setBackgroundBrush(to_qcolor(st.colors[0], Qt.black))

    def update_values(self, values: dict, fahrenheit: bool = False):
        now = QDateTime.currentDateTime()
        for it in self.widgets:
            if isinstance(it, NumberItem):
                key = SENSOR_IDS.get(it.sensor)
                v = values.get(key) if key else None
                unit_text = it.unit_item.toPlainText() if it.unit_item else ""
                if v is not None and key and key.endswith("_temp") and fahrenheit:
                    v = v * 9 / 5 + 32
                    if it.unit_item and "C" in unit_text:
                        it.unit_item.set_text(unit_text.replace("C", "F"))
                elif it.unit_item and not fahrenheit and "°F" in unit_text:
                    it.unit_item.set_text(unit_text.replace("F", "C"))
                it.set_text("--" if v is None else f"{v:.0f}")
            elif isinstance(it, LabelItem):
                if it.kind == 4:
                    it.set_text(self.locale.toString(now.time(), self.time_format))
                elif it.kind == 3:
                    it.set_text(self.locale.toString(now.date(), self.date_format))
                elif it.kind == 5:
                    it.set_text(self.locale.toString(now.date(), "dddd"))
            elif isinstance(it, (ProgressBarItem, ArcItem, GaugeItem)):
                key = SENSOR_IDS.get(it.sensor)
                it.value = values.get(key) or 0.0
                it.update()
            elif isinstance(it, ClockItem):
                it.update()
            elif isinstance(it, ScreenCaptureItem):
                it.grab()
                it.update()
            elif isinstance(it, ImageItem) and it.source is not None:
                it.update()

    def render_image(self) -> QImage:
        img = QImage(self.size[0], self.size[1], QImage.Format_ARGB32_Premultiplied)
        img.fill(Qt.black)
        selected = self.selectedItems()
        for it in selected:
            it.setSelected(False)
        p = QPainter(img)
        p.setRenderHint(QPainter.Antialiasing)
        p.setRenderHint(QPainter.TextAntialiasing)
        p.setRenderHint(QPainter.SmoothPixmapTransform)
        self.render(p, QRectF(0, 0, *self.size), QRectF(0, 0, *self.size))
        p.end()
        for it in selected:
            it.setSelected(True)
        return img

    def close(self):
        for it in self.widgets:
            if hasattr(it, "close"):
                it.close()

