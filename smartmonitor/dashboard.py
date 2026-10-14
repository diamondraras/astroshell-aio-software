"""Built-in system dashboard for the 320x320 display (an addition of this rebuild).

Layout:
  header   analog clock | time, date, uptime
  cards    CPU and GPU: usage %, temperature °C, 1-minute graph of both
  network  download / upload speed, data used since boot, 1-minute graph, online state

Rendering cost is kept low: the static layer (background, card frames, titles) is drawn once,
and the dynamic layer is redrawn only when the second changes or new sensor data arrives.
"""
from __future__ import annotations

import math
import time
from datetime import datetime

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import (QBrush, QColor, QFont, QFontMetricsF, QImage, QLinearGradient, QPainter, QPainterPath,
                           QPen, QRadialGradient)

BG_TOP, BG_BOTTOM = QColor("#0d1320"), QColor("#04060a")
CARD, CARD_EDGE = QColor("#111827"), QColor("#1f2a3c")
TEXT, MUTED, DIM = QColor("#f3f5f9"), QColor("#93a0b8"), QColor("#5b6680")
CPU_C, GPU_C = QColor("#38bdf8"), QColor("#a78bfa")
TEMP_C, HOT_C = QColor("#fb923c"), QColor("#ef4444")
DOWN_C, UP_C = QColor("#34d399"), QColor("#f472b6")
OK_C, BAD_C = QColor("#22c55e"), QColor("#ef4444")
SECOND_HAND = QColor("#ff5a5f")

FONT_FAMILIES = ["Segoe UI", "Inter", "Noto Sans", "DejaVu Sans", "Arial"]


def _font(px: int, weight=QFont.Normal) -> QFont:
    f = QFont()
    f.setFamilies(FONT_FAMILIES)
    f.setPixelSize(px)
    f.setWeight(weight)
    f.setHintingPreference(QFont.PreferNoHinting)
    return f


def fmt_rate(kb_per_s: float | None) -> tuple[str, str]:
    if kb_per_s is None:
        return "--", "KB/s"
    if kb_per_s < 1000:
        return f"{kb_per_s:.0f}", "KB/s"
    mb = kb_per_s / 1024
    return (f"{mb:.2f}" if mb < 10 else f"{mb:.1f}" if mb < 100 else f"{mb:.0f}"), "MB/s"


def fmt_bytes(n: float | None) -> str:
    if n is None:
        return "--"
    units = ("B", "KB", "MB", "GB", "TB")
    i = 0
    while n >= 1000 and i < len(units) - 1:
        n /= 1024
        i += 1
    return f"{n:.0f} {units[i]}" if i < 2 or n >= 100 else f"{n:.3g} {units[i]}"


def fmt_uptime(seconds: float) -> str:
    h, rem = divmod(int(seconds), 3600)
    return f"{h} h {rem // 60:02d} min"


def nice_ceiling(v: float) -> float:
    """Smallest 1/2/5 x 10^n that is >= v."""
    if v <= 0:
        return 1.0
    e = 10 ** math.floor(math.log10(v))
    for m in (1, 2, 5, 10):
        if m * e >= v:
            return m * e
    return 10 * e


class DashboardRenderer:
    animated = False  # content changes at most once per second

    def __init__(self, sensors, cfg, size=(320, 320)):
        self.sensors, self.cfg = sensors, cfg
        self.w, self.h = size
        self.frame_id = 0
        self._key = None
        self._image: QImage | None = None
        self._static: QImage | None = None
        self.f_time = _font(46, QFont.DemiBold)
        self.f_sec = _font(20, QFont.Medium)
        self.f_date = _font(15, QFont.Medium)
        self.f_small = _font(11, QFont.DemiBold)
        self.f_tiny = _font(9, QFont.Medium)
        self.f_title = _font(12, QFont.Bold)
        self.f_big = _font(30, QFont.Bold)
        self.f_unit = _font(14, QFont.DemiBold)
        self.f_temp = _font(16, QFont.Bold)
        self.f_rate = _font(19, QFont.Bold)
        self.f_rate_unit = _font(11, QFont.DemiBold)
        self.f_total = _font(15, QFont.Bold)

    # geometry ------------------------------------------------------------------------------
    CLOCK_C, CLOCK_R = QPointF(50, 52), 43.0
    CPU_RECT = QRectF(6, 104, 151, 100)
    GPU_RECT = QRectF(163, 104, 151, 100)
    NET_RECT = QRectF(6, 210, 308, 104)

    # public API --------------------------------------------------------------------------
    def render(self) -> QImage:
        key = (int(time.time()), self.sensors.version, self.sensors.online,
               self.cfg.get("temp/uint"), self.cfg.get("dashboard/hour24"))
        if key != self._key or self._image is None:
            self._key = key
            self._image = self._draw()
            self.frame_id += 1
        return self._image

    def load(self, *_):
        return True

    def close(self):
        pass

    # drawing ---------------------------------------------------------------------------------
    def _static_layer(self) -> QImage:
        img = QImage(self.w, self.h, QImage.Format_ARGB32_Premultiplied)
        p = QPainter(img)
        p.setRenderHint(QPainter.Antialiasing)
        g = QLinearGradient(0, 0, 0, self.h)
        g.setColorAt(0, BG_TOP)
        g.setColorAt(1, BG_BOTTOM)
        p.fillRect(0, 0, self.w, self.h, g)
        for r in (self.CPU_RECT, self.GPU_RECT, self.NET_RECT):
            p.setPen(QPen(CARD_EDGE, 1))
            p.setBrush(CARD)
            p.drawRoundedRect(r.adjusted(0.5, 0.5, -0.5, -0.5), 10, 10)
        p.setFont(self.f_title)
        for r, name, c in ((self.CPU_RECT, "CPU", CPU_C), (self.GPU_RECT, "GPU", GPU_C),
                           (self.NET_RECT, "NETWORK", DOWN_C)):
            p.setPen(c)
            p.drawText(QPointF(r.x() + 10, r.y() + 18), name)
        # clock face
        c, rad = self.CLOCK_C, self.CLOCK_R
        face = QRadialGradient(c, rad)
        face.setColorAt(0, QColor("#1a2232"))
        face.setColorAt(1, QColor("#0c111b"))
        p.setBrush(face)
        p.setPen(QPen(QColor("#2b3850"), 2))
        p.drawEllipse(c, rad, rad)
        for i in range(60):
            a = math.radians(i * 6)
            major = i % 15 == 0
            hour = i % 5 == 0
            if not hour:
                continue
            l1 = rad - (9 if major else 6)
            p.setPen(QPen(TEXT if major else MUTED, 2.6 if major else 1.6, Qt.SolidLine, Qt.RoundCap))
            p.drawLine(QPointF(c.x() + l1 * math.sin(a), c.y() - l1 * math.cos(a)),
                       QPointF(c.x() + (rad - 3.5) * math.sin(a), c.y() - (rad - 3.5) * math.cos(a)))
        p.end()
        return img

    def _draw(self) -> QImage:
        if self._static is None:
            self._static = self._static_layer()
        img = self._static.copy()
        p = QPainter(img)
        p.setRenderHint(QPainter.Antialiasing)
        p.setRenderHint(QPainter.TextAntialiasing)
        vals = self.sensors.snapshot()
        fahrenheit = self.cfg.get("temp/uint") == 1
        now = datetime.now()
        self._draw_clock(p, now)
        self._draw_header(p, now)
        self._draw_chip(p, self.CPU_RECT, CPU_C, vals.get("cpu_usage"), vals.get("cpu_temp"),
                        self.sensors.history("cpu_usage"), self.sensors.history("cpu_temp"), fahrenheit)
        self._draw_chip(p, self.GPU_RECT, GPU_C, vals.get("gpu_usage"), vals.get("gpu_temp"),
                        self.sensors.history("gpu_usage"), self.sensors.history("gpu_temp"), fahrenheit)
        self._draw_net(p, vals)
        p.end()
        return img

    def _draw_clock(self, p: QPainter, now: datetime):
        c, rad = self.CLOCK_C, self.CLOCK_R
        hands = (((now.hour % 12) + now.minute / 60) * 30, 0.50, 4.2, TEXT, 0.0), \
                ((now.minute + now.second / 60) * 6, 0.74, 3.0, TEXT, 0.0), \
                (now.second * 6, 0.82, 1.4, SECOND_HAND, 0.18)
        for angle, length, width, color, tail in hands:
            a = math.radians(angle)
            dx, dy = math.sin(a), -math.cos(a)
            p.setPen(QPen(color, width, Qt.SolidLine, Qt.RoundCap))
            p.drawLine(QPointF(c.x() - dx * rad * tail, c.y() - dy * rad * tail),
                       QPointF(c.x() + dx * rad * length, c.y() + dy * rad * length))
        p.setPen(Qt.NoPen)
        p.setBrush(SECOND_HAND)
        p.drawEllipse(c, 3.2, 3.2)

    def _draw_header(self, p: QPainter, now: datetime):
        x = 106
        hour24 = self.cfg.get("dashboard/hour24")
        hh = now.strftime("%H:%M") if hour24 else now.strftime("%I:%M").lstrip("0")
        p.setFont(self.f_time)
        p.setPen(TEXT)
        p.drawText(QPointF(x - 2, 46), hh)
        tw = QFontMetricsF(self.f_time).horizontalAdvance(hh)
        p.setFont(self.f_sec)
        p.setPen(MUTED)
        p.drawText(QPointF(x + tw + 2, 46), f"{now.second:02d}" + ("" if hour24 else now.strftime(" %p")))
        p.setFont(self.f_date)
        p.setPen(QColor("#c7cfdd"))
        p.drawText(QPointF(x, 70), now.strftime("%a %d %b %Y"))
        # uptime pill
        text = "UP  " + fmt_uptime(self.sensors.uptime())
        p.setFont(self.f_small)
        tw = QFontMetricsF(self.f_small).horizontalAdvance(text)
        pill = QRectF(x, 79, tw + 30, 19)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor("#1b2638"))
        p.drawRoundedRect(pill, 9.5, 9.5)
        self._power_icon(p, QPointF(pill.x() + 11, pill.center().y()), 5, OK_C)
        p.setPen(QColor("#d6dceb"))
        p.drawText(QPointF(pill.x() + 21, pill.y() + 14), text)

    @staticmethod
    def _power_icon(p: QPainter, c: QPointF, r: float, color: QColor):
        p.setPen(QPen(color, 1.6, Qt.SolidLine, Qt.RoundCap))
        p.setBrush(Qt.NoBrush)
        p.drawArc(QRectF(c.x() - r, c.y() - r, 2 * r, 2 * r), 120 * 16, 300 * 16)
        p.drawLine(QPointF(c.x(), c.y() - r - 1), QPointF(c.x(), c.y() - 0.5))

    def _temp_text(self, t, fahrenheit) -> str:
        if t is None:
            return "--°C" if not fahrenheit else "--°F"
        return f"{t * 9 / 5 + 32:.0f}°F" if fahrenheit else f"{t:.0f}°C"

    def _draw_chip(self, p, r: QRectF, accent: QColor, usage, temp, h_usage, h_temp, fahrenheit):
        # temperature, top right
        tcol = HOT_C if (temp or 0) >= 85 else TEMP_C
        ttext = self._temp_text(temp, fahrenheit)
        p.setFont(self.f_temp)
        tw = QFontMetricsF(self.f_temp).horizontalAdvance(ttext)
        p.setPen(tcol if temp is not None else DIM)
        p.drawText(QPointF(r.right() - 10 - tw, r.y() + 20), ttext)
        self._thermo_icon(p, QPointF(r.right() - 18 - tw, r.y() + 14), tcol if temp is not None else DIM)
        # usage, big
        p.setFont(self.f_big)
        p.setPen(TEXT if usage is not None else DIM)
        u = "--" if usage is None else f"{usage:.0f}"
        p.drawText(QPointF(r.x() + 9, r.y() + 52), u)
        uw = QFontMetricsF(self.f_big).horizontalAdvance(u)
        p.setFont(self.f_unit)
        p.setPen(MUTED)
        p.drawText(QPointF(r.x() + 11 + uw, r.y() + 52), "%")
        # usage bar next to the number
        bar = QRectF(r.x() + 40 + uw, r.y() + 43, r.right() - 10 - (r.x() + 40 + uw), 5)
        if bar.width() > 10:
            p.setPen(Qt.NoPen)
            p.setBrush(QColor("#1f2937"))
            p.drawRoundedRect(bar, 2.5, 2.5)
            if usage:
                fill = QRectF(bar)
                fill.setWidth(max(5.0, bar.width() * min(1.0, usage / 100)))
                p.setBrush(accent)
                p.drawRoundedRect(fill, 2.5, 2.5)
        graph = QRectF(r.x() + 8, r.y() + 58, r.width() - 16, r.height() - 64)
        self._grid(p, graph)
        self._series(p, graph, h_usage, 100.0, accent, fill=True)
        self._series(p, graph, h_temp, 100.0, TEMP_C, fill=False, width=1.3)
        if not h_usage:
            p.setFont(self.f_tiny)
            p.setPen(DIM)
            p.drawText(graph, Qt.AlignCenter, "collecting…")

    @staticmethod
    def _thermo_icon(p: QPainter, c: QPointF, color: QColor):
        p.setPen(QPen(color, 1.4))
        p.setBrush(Qt.NoBrush)
        p.drawRoundedRect(QRectF(c.x() - 1.8, c.y() - 7, 3.6, 8), 1.8, 1.8)
        p.setBrush(color)
        p.drawEllipse(QPointF(c.x(), c.y() + 3), 3, 3)

    @staticmethod
    def _grid(p: QPainter, g: QRectF):
        p.setPen(QPen(QColor(255, 255, 255, 18), 1, Qt.DashLine))
        for f in (0.0, 0.5):
            y = g.y() + g.height() * f
            p.drawLine(QPointF(g.x(), y), QPointF(g.right(), y))
        p.setPen(QPen(QColor(255, 255, 255, 40), 1))
        p.drawLine(QPointF(g.x(), g.bottom()), QPointF(g.right(), g.bottom()))

    @staticmethod
    def _series(p: QPainter, g: QRectF, data: list, vmax: float, color: QColor, fill: bool, width=1.6):
        n = 60
        pts = []
        start = n - len(data)
        for i, v in enumerate(data):
            if v is None:
                continue
            x = g.x() + g.width() * (start + i) / (n - 1)
            y = g.bottom() - g.height() * max(0.0, min(1.0, v / vmax))
            pts.append(QPointF(x, y))
        if len(pts) < 2:
            return
        path = QPainterPath(pts[0])
        for pt in pts[1:]:
            path.lineTo(pt)
        if fill:
            area = QPainterPath(path)
            area.lineTo(pts[-1].x(), g.bottom())
            area.lineTo(pts[0].x(), g.bottom())
            area.closeSubpath()
            grad = QLinearGradient(0, g.y(), 0, g.bottom())
            top, bottom = QColor(color), QColor(color)
            top.setAlpha(120)
            bottom.setAlpha(10)
            grad.setColorAt(0, top)
            grad.setColorAt(1, bottom)
            p.fillPath(area, QBrush(grad))
        p.setPen(QPen(color, width, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
        p.setBrush(Qt.NoBrush)
        p.drawPath(path)

    def _draw_net(self, p: QPainter, vals: dict):
        r = self.NET_RECT
        online = self.sensors.online
        offline = online is False
        # status, top right
        status = "OFFLINE" if offline else "ONLINE" if online else "CHECKING"
        col = BAD_C if offline else OK_C if online else MUTED
        p.setFont(self.f_small)
        sw = QFontMetricsF(self.f_small).horizontalAdvance(status)
        p.setPen(col)
        p.drawText(QPointF(r.right() - 10 - sw, r.y() + 18), status)
        self._globe_icon(p, QPointF(r.right() - 22 - sw, r.y() + 14), 6.5, col, crossed=offline)
        # speeds
        x = r.x() + 10
        for arrow, key, color in (("↓", "net_down", DOWN_C), ("↑", "net_up", UP_C)):
            num, unit = fmt_rate(vals.get(key))
            p.setFont(self.f_rate)
            p.setPen(color if not offline else DIM)
            p.drawText(QPointF(x, r.y() + 44), f"{arrow} {num}")
            nw = QFontMetricsF(self.f_rate).horizontalAdvance(f"{arrow} {num}")
            p.setFont(self.f_rate_unit)
            p.setPen(MUTED)
            p.drawText(QPointF(x + nw + 3, r.y() + 44), unit)
            x += 104
        # total since boot
        down, up = vals.get("net_total_down"), vals.get("net_total_up")
        total = None if down is None else down + (up or 0)
        ttext = fmt_bytes(total)
        p.setFont(self.f_total)
        tw = QFontMetricsF(self.f_total).horizontalAdvance(ttext)
        p.setPen(TEXT if total is not None else DIM)
        p.drawText(QPointF(r.right() - 10 - tw, r.y() + 40), ttext)
        p.setFont(self.f_tiny)
        lw = QFontMetricsF(self.f_tiny).horizontalAdvance("used since boot")
        p.setPen(MUTED)
        p.drawText(QPointF(r.right() - 10 - lw, r.y() + 51), "used since boot")
        # graph
        g = QRectF(r.x() + 8, r.y() + 57, r.width() - 16, r.height() - 63)
        h_down, h_up = self.sensors.history("net_down"), self.sensors.history("net_up")
        peak = max([v for v in h_down + h_up if v is not None] or [0.0])
        peak = max(peak, 100.0)
        scale = nice_ceiling(peak) if peak < 1000 else nice_ceiling(peak / 1024) * 1024
        self._grid(p, g)
        self._series(p, g, h_down, scale, DOWN_C, fill=True)
        self._series(p, g, h_up, scale, UP_C, fill=False, width=1.3)
        num, unit = fmt_rate(scale)
        p.setFont(self.f_tiny)
        p.setPen(DIM)
        p.drawText(QPointF(g.x() + 2, g.y() + 9), f"{num} {unit}")
        if offline:
            p.fillRect(g, QColor(4, 6, 10, 170))
            c = g.center()
            self._globe_icon(p, QPointF(c.x() - 42, c.y()), 13, BAD_C, crossed=True)
            p.setFont(self.f_total)
            p.setPen(BAD_C)
            p.drawText(QPointF(c.x() - 22, c.y() + 6), "No internet")

    @staticmethod
    def _globe_icon(p: QPainter, c: QPointF, r: float, color: QColor, crossed: bool):
        p.setPen(QPen(color, max(1.2, r / 5)))
        p.setBrush(Qt.NoBrush)
        p.drawEllipse(c, r, r)
        p.drawEllipse(c, r * 0.45, r)
        p.drawLine(QPointF(c.x() - r, c.y()), QPointF(c.x() + r, c.y()))
        if crossed:
            p.setPen(QPen(QColor("#04060a"), max(3.0, r / 2.2), Qt.SolidLine, Qt.RoundCap))
            p.drawLine(QPointF(c.x() - r * 1.15, c.y() - r * 1.15), QPointF(c.x() + r * 1.15, c.y() + r * 1.15))
            p.setPen(QPen(color, max(1.6, r / 4), Qt.SolidLine, Qt.RoundCap))
            p.drawLine(QPointF(c.x() - r * 1.15, c.y() - r * 1.15), QPointF(c.x() + r * 1.15, c.y() + r * 1.15))
