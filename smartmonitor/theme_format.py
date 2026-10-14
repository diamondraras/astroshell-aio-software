"""Reader/writer for SmartMonitor28 A3 ``.theme`` files (pure Python, struct only).

Format reverse-engineered from "SmartMonitor28 A3.exe" (Qt 5.12.12, x64).
See D:/rea-test/docs/THEME_FORMAT.md for the evidence (function addresses).

File  = sequence of records, no header, no trailer.
Record = u32 LITTLE-endian total size (includes these 4 bytes)
         + QDataStream payload (big-endian, stream version Qt_5_12 = 18,
           floatingPointPrecision = DoublePrecision, so C++ ``float`` fields
           are written as 8-byte IEEE doubles).
Payload always starts with i32 widget type id:

    0  SceneSettings          (main window, FUN_14001ebc0 / FUN_140021300)
    1  DispImageWidget        (ser FUN_1400297a0, deser FUN_140029220)
    2  DispTextWidget         (ser FUN_14003e780, deser FUN_14003df20)
    3  DispDateWidget         (ser FUN_140014360, deser FUN_140014140)
    4  DispTimeWidget         (ser FUN_1400455f0, deser FUN_140014140)
    5  DispWeekdayWidget      (ser FUN_140046620, deser FUN_140014140)
    6  DispNumberWidget       (ser FUN_14002bb40, deser FUN_14002b3f0)
    7  DispScreenCaptureWidget(ser FUN_140034300, deser FUN_140033c40)
    8  DispProgressBarWidget  (ser FUN_14002f1f0, deser FUN_14002ea50)
    9  DispArcWidget          (ser FUN_14000eb10, deser FUN_14000e470)
    10 DispGaugeWidget        (ser FUN_140015760, deser FUN_140015470)
    11 DispAnalogClockWidget  (ser FUN_14000d0a0, deser FUN_14000c840)

API:
    parse(data: bytes) -> dict           {"records": [Record, ...]}
    serialize(theme: dict) -> bytes
    parse_file(path) / write_file(path, theme)
    to_jsonable(theme) -> plain dict/list structure (for json.dumps)
Unknown/unparseable records are kept as RawRecord so round-trips are lossless.
"""
from __future__ import annotations

import struct
import sys
from dataclasses import dataclass, field, asdict, is_dataclass
from typing import List, Optional, Tuple, Union

__all__ = [
    "QColor", "QFont", "GradientStop", "LabelItem",
    "SceneSettings", "ImageWidget", "TextWidget", "DateWidget", "TimeWidget",
    "WeekdayWidget", "NumberWidget", "ScreenCaptureWidget", "ProgressBarStyle",
    "ProgressBarWidget", "ArcStyle", "ArcWidget", "GaugeWidget", "ClockStyle",
    "AnalogClockWidget", "RawRecord", "parse", "serialize", "parse_file",
    "write_file", "to_jsonable", "SENSOR_NAMES", "WIDGET_TYPES",
]

WIDGET_TYPES = {
    0: "SceneSettings", 1: "Image", 2: "Text", 3: "Date", 4: "Time",
    5: "Weekday", 6: "Number", 7: "ScreenCapture", 8: "ProgressBar",
    9: "Arc", 10: "Gauge", 11: "AnalogClock",
}

# Sensor ids as registered by FUN_140038da0 (index into the sensor table;
# 0 = no sensor).  Value ranges from FUN_1400378e0.
SENSOR_NAMES = {
    0: "none",
    1: "CPU Freq (MHz, HWiNFO 'CPU [#0]:'/'Core 0 Clock', range 0..10000)",
    2: "CPU Temp (degC, 'CPU [#0]:'/'CPU Die', range 0..120)",
    3: "CPU Usage (%, 'CPU [#0]:'/'Total CPU Usage', range 0..100)",
    4: "GPU Freq (MHz, 'GPU [#0]:'/'GPU Clock', range 0..10000)",
    5: "GPU Temp (degC, 'GPU [#0]:'/'GPU Temperature', range 0..120)",
    6: "GPU Usage (%, 'GPU [#0]:'/'GPU Core Load', range 0..100)",
}


class ThemeFormatError(ValueError):
    pass


# --------------------------------------------------------------------------
# QDataStream primitives (big-endian)
# --------------------------------------------------------------------------
class _Reader:
    def __init__(self, data: bytes):
        self.d = data
        self.p = 0

    def remaining(self) -> int:
        return len(self.d) - self.p

    def _take(self, n: int) -> bytes:
        if self.p + n > len(self.d):
            raise ThemeFormatError(f"read past end ({n} bytes at {self.p}/{len(self.d)})")
        b = self.d[self.p:self.p + n]
        self.p += n
        return b

    def u8(self) -> int: return self._take(1)[0]
    def i8(self) -> int: return struct.unpack(">b", self._take(1))[0]
    def u16(self) -> int: return struct.unpack(">H", self._take(2))[0]
    def i32(self) -> int: return struct.unpack(">i", self._take(4))[0]
    def u32(self) -> int: return struct.unpack(">I", self._take(4))[0]
    def f64(self) -> float: return struct.unpack(">d", self._take(8))[0]

    def boolean(self) -> bool:
        v = self.u8()
        if v not in (0, 1):
            raise ThemeFormatError(f"bool byte {v} at {self.p - 1}")
        return bool(v)

    def qstring(self) -> Optional[str]:
        n = self.u32()
        if n == 0xFFFFFFFF:
            return None  # null QString
        if n & 1:
            raise ThemeFormatError("odd QString byte length")
        return self._take(n).decode("utf-16-be", errors="surrogatepass")

    def qbytearray(self) -> Optional[bytes]:
        n = self.u32()
        if n == 0xFFFFFFFF:
            return None
        return self._take(n)

    def qpointf(self) -> Tuple[float, float]:
        return (self.f64(), self.f64())

    def qrectf(self) -> Tuple[float, float, float, float]:
        return (self.f64(), self.f64(), self.f64(), self.f64())

    def qsize(self) -> Tuple[int, int]:
        return (self.i32(), self.i32())

    def qcolor(self) -> "QColor":
        return QColor(self.i8(), self.u16(), self.u16(), self.u16(), self.u16(), self.u16())

    def qfont(self) -> "QFont":
        return QFont(
            family=self.qstring(), style_name=self.qstring(),
            point_size=self.f64(), pixel_size=self.i32(),
            style_hint=self.u8(), style_strategy=self.u16(), charset=self.u8(),
            weight=self.u8(), bits=self.u8(), stretch=self.u16(),
            extended_bits=self.u8(), letter_spacing=self.i32(),
            word_spacing=self.i32(), hinting_preference=self.u8(),
            capitalization=self.u8())


class _Writer:
    def __init__(self):
        self.parts: List[bytes] = []

    def bytes(self) -> bytes:
        return b"".join(self.parts)

    def u8(self, v): self.parts.append(struct.pack(">B", v))
    def i8(self, v): self.parts.append(struct.pack(">b", v))
    def u16(self, v): self.parts.append(struct.pack(">H", v))
    def i32(self, v): self.parts.append(struct.pack(">i", v))
    def u32(self, v): self.parts.append(struct.pack(">I", v))
    def f64(self, v): self.parts.append(struct.pack(">d", v))
    def boolean(self, v): self.u8(1 if v else 0)

    def qstring(self, s: Optional[str]):
        if s is None:
            self.u32(0xFFFFFFFF)
            return
        b = s.encode("utf-16-be", errors="surrogatepass")
        self.u32(len(b))
        self.parts.append(b)

    def qbytearray(self, b: Optional[bytes]):
        if b is None:
            self.u32(0xFFFFFFFF)
            return
        self.u32(len(b))
        self.parts.append(bytes(b))

    def qpointf(self, p): self.f64(p[0]); self.f64(p[1])
    def qrectf(self, r):
        for v in r:
            self.f64(v)
    def qsize(self, s): self.i32(s[0]); self.i32(s[1])

    def qcolor(self, c: "QColor"):
        self.i8(c.spec); self.u16(c.alpha); self.u16(c.red)
        self.u16(c.green); self.u16(c.blue); self.u16(c.pad)

    def qfont(self, f: "QFont"):
        self.qstring(f.family); self.qstring(f.style_name)
        self.f64(f.point_size); self.i32(f.pixel_size)
        self.u8(f.style_hint); self.u16(f.style_strategy); self.u8(f.charset)
        self.u8(f.weight); self.u8(f.bits); self.u16(f.stretch)
        self.u8(f.extended_bits); self.i32(f.letter_spacing)
        self.i32(f.word_spacing); self.u8(f.hinting_preference)
        self.u8(f.capitalization)


# --------------------------------------------------------------------------
# Qt value types
# --------------------------------------------------------------------------
@dataclass
class QColor:
    """QColor QDataStream form (Qt >= 4): qint8 spec, then 5 x quint16.

    spec: 0 Invalid, 1 Rgb, 2 Hsv, 3 Cmyk, 4 Hsl, 5 ExtendedRgb.
    For spec==1 the components are 16-bit (8-bit value * 0x101).
    """
    spec: int = 1
    alpha: int = 0xFFFF
    red: int = 0
    green: int = 0
    blue: int = 0
    pad: int = 0

    @classmethod
    def from_rgba8(cls, r: int, g: int, b: int, a: int = 255) -> "QColor":
        return cls(1, a * 0x101, r * 0x101, g * 0x101, b * 0x101, 0)

    def rgba8(self) -> Tuple[int, int, int, int]:
        """8-bit RGBA (only meaningful for spec == 1/Rgb)."""
        return (self.red >> 8, self.green >> 8, self.blue >> 8, self.alpha >> 8)

    def hex(self) -> str:
        r, g, b, a = self.rgba8()
        return f"#{a:02x}{r:02x}{g:02x}{b:02x}"


@dataclass
class QFont:
    """QFont QDataStream form for stream version Qt_5_12 (18).

    bits:  0x01 italic, 0x02 underline, 0x04 strikeOut, 0x08 fixedPitch,
           0x10 kerning, 0x40 overline, 0x80 oblique.
    extended_bits: 0x01 ignorePitch, 0x02 letterSpacing-is-absolute(?)
    weight: QFont::Weight (50 Normal, 75 Bold, 87 Black ...).
    letter/word spacing are QFixed raw values (1/64 px).
    """
    family: Optional[str] = "Arial"
    style_name: Optional[str] = ""
    point_size: float = 12.0
    pixel_size: int = -1
    style_hint: int = 5          # QFont::AnyStyle
    style_strategy: int = 1      # QFont::PreferDefault
    charset: int = 0             # always 0 (legacy)
    weight: int = 50
    bits: int = 0x10
    stretch: int = 0             # 0 = AnyStretch
    extended_bits: int = 1
    letter_spacing: int = 0
    word_spacing: int = 0
    hinting_preference: int = 0
    capitalization: int = 0

    @property
    def bold(self) -> bool:
        return self.weight >= 75

    @property
    def italic(self) -> bool:
        return bool(self.bits & 0x01)


@dataclass
class GradientStop:
    position: float
    color: QColor


# --------------------------------------------------------------------------
# Record types
# --------------------------------------------------------------------------
@dataclass
class SceneSettings:
    """Type 0 (written last by FUN_14001ebc0; read by FUN_140021300)."""
    TYPE = 0
    background_mode: int = 1        # 1, 2 or 3 (radio buttons, see doc)
    colors: List[QColor] = field(default_factory=lambda: [QColor()])
    flag_0x88: bool = True          # UNKNOWN checkbox (panel@+0x88, widget+0x60)
    datetime_flags: List[bool] = field(default_factory=lambda: [False] * 10)
    sensor_flags: List[bool] = field(default_factory=lambda: [False] * 8)
    carousel_interval: int = 0      # QSpinBox value (seconds)
    flag_0x1c0: bool = False        # UNKNOWN checkbox (main window +0x1c0)
    width: int = 320                # canvas size (not read back by loader)
    height: int = 320

    def write(self, w: _Writer):
        w.i32(self.background_mode)
        if self.background_mode == 2 and len(self.colors) != 2:
            raise ThemeFormatError("background_mode 2 writes two colours")
        if self.background_mode != 2 and len(self.colors) != 1:
            raise ThemeFormatError("background_mode 1/3 writes one colour")
        for c in self.colors:
            w.qcolor(c)
        w.boolean(self.flag_0x88)
        if len(self.datetime_flags) != 10 or len(self.sensor_flags) != 8:
            raise ThemeFormatError("flag list length")
        for b in self.datetime_flags:
            w.boolean(b)
        for b in self.sensor_flags:
            w.boolean(b)
        w.i32(self.carousel_interval)
        w.boolean(self.flag_0x1c0)
        w.i32(self.width)
        w.i32(self.height)

    @classmethod
    def read(cls, r: _Reader):
        mode = r.i32()
        colors = [r.qcolor()]
        if mode == 2:
            colors.append(r.qcolor())
        return cls(mode, colors, r.boolean(),
                   [r.boolean() for _ in range(10)],
                   [r.boolean() for _ in range(8)],
                   r.i32(), r.boolean(), r.i32(), r.i32())


@dataclass
class ImageWidget:
    """Type 1. rect = (x, y, w, h); file is relative to <theme>/res/."""
    TYPE = 1
    name: Optional[str] = "image"
    rect: Tuple[float, float, float, float] = (0.0, 0.0, 320.0, 320.0)
    file: Optional[str] = ""

    def write(self, w):
        w.qstring(self.name); w.qrectf(self.rect); w.qstring(self.file)

    @classmethod
    def read(cls, r):
        return cls(r.qstring(), r.qrectf(), r.qstring())


@dataclass
class _LabelBase:
    """Common layout of types 2..5: name, pos, color, font, text."""
    name: Optional[str] = ""
    pos: Tuple[float, float] = (0.0, 0.0)
    color: QColor = field(default_factory=QColor)
    font: QFont = field(default_factory=QFont)
    text: Optional[str] = ""

    def write(self, w):
        w.qstring(self.name); w.qpointf(self.pos); w.qcolor(self.color)
        w.qfont(self.font); w.qstring(self.text)

    @classmethod
    def read(cls, r):
        return cls(r.qstring(), r.qpointf(), r.qcolor(), r.qfont(), r.qstring())


@dataclass
class TextWidget(_LabelBase):
    """Type 2: free text (QGraphicsTextItem); text = plain text shown."""
    TYPE = 2


@dataclass
class DateWidget(_LabelBase):
    """Type 3: text is a snapshot only; format is runtime (default yyyy/MM/dd)."""
    TYPE = 3


@dataclass
class TimeWidget(_LabelBase):
    """Type 4: text is a snapshot only; format is runtime (default hh:mm:ss)."""
    TYPE = 4


@dataclass
class WeekdayWidget(_LabelBase):
    """Type 5: text is a snapshot only; format is runtime ("dddd")."""
    TYPE = 5


@dataclass
class LabelItem:
    """Child label of a NumberWidget (unit = marker 1, name = marker 2)."""
    name: Optional[str] = ""
    pos: Tuple[float, float] = (0.0, 0.0)
    color: QColor = field(default_factory=QColor)
    font: QFont = field(default_factory=QFont)
    text: Optional[str] = ""

    def write(self, w):
        w.qstring(self.name); w.qpointf(self.pos); w.qcolor(self.color)
        w.qfont(self.font); w.qstring(self.text)

    @classmethod
    def read(cls, r):
        return cls(r.qstring(), r.qpointf(), r.qcolor(), r.qfont(), r.qstring())


@dataclass
class NumberWidget:
    """Type 6: sensor value label (+ optional unit / name child labels)."""
    TYPE = 6
    name: Optional[str] = "data"
    pos: Tuple[float, float] = (0.0, 0.0)
    sensor: int = 0                 # see SENSOR_NAMES
    color: QColor = field(default_factory=QColor)
    font: QFont = field(default_factory=QFont)
    text: Optional[str] = "00"      # last rendered value (snapshot)
    unit: Optional[LabelItem] = None    # marker 1, objectName "<name>_unit"
    label: Optional[LabelItem] = None   # marker 2, objectName "<name>_name"

    def write(self, w):
        w.qstring(self.name); w.qpointf(self.pos); w.i32(self.sensor)
        w.qcolor(self.color); w.qfont(self.font); w.qstring(self.text)
        if self.unit is not None:
            w.i32(1); self.unit.write(w)
        if self.label is not None:
            w.i32(2); self.label.write(w)

    @classmethod
    def read(cls, r):
        o = cls(r.qstring(), r.qpointf(), r.i32(), r.qcolor(), r.qfont(), r.qstring())
        if r.remaining():
            m = r.i32()
            if m == 1:
                o.unit = LabelItem.read(r)
                if r.remaining():
                    m = r.i32()
            if m == 2:
                o.label = LabelItem.read(r)
            elif m != 1:
                raise ThemeFormatError(f"bad number-widget child marker {m}")
        return o


@dataclass
class ScreenCaptureWidget:
    """Type 7: mirrors a desktop region (capture_rect) into a label."""
    TYPE = 7
    name: Optional[str] = "screen_capture"
    capture_rect: Tuple[float, float, float, float] = (0.0, 0.0, -1.0, -1.0)
    pos: Tuple[float, float] = (0.0, 0.0)
    size: Tuple[int, int] = (0, 0)

    def write(self, w):
        w.qstring(self.name); w.qrectf(self.capture_rect)
        w.qpointf(self.pos); w.qsize(self.size)

    @classmethod
    def read(cls, r):
        return cls(r.qstring(), r.qrectf(), r.qpointf(), r.qsize())


def _write_stops(w, stops):
    w.u32(len(stops))
    for s in stops:
        w.f64(s.position); w.qcolor(s.color)


def _read_stops(r):
    return [GradientStop(r.f64(), r.qcolor()) for _ in range(r.u32())]


@dataclass
class ProgressBarStyle:
    """DispProgressBar blob (FUN_14002cd50 / FUN_14002c0c0).

    Field meaning is NOT confirmed; names give widget offsets and ctor
    defaults (FUN_14002bf10).  floats are stored as doubles.
    """
    flag_30: bool = False
    flag_31: bool = False
    flag_32: bool = True
    flag_33: bool = True
    float_34: float = 0.0     # default 0
    float_38: float = 2.0     # default 2
    float_3c: float = 100.0   # default 100
    float_40: float = 30.0    # default 30
    float_48: float = 0.0     # default 0
    color_4c: QColor = field(default_factory=QColor)   # default Qt::gray
    color_5c: QColor = field(default_factory=QColor)   # default Qt::red
    stops: List[GradientStop] = field(default_factory=list)  # QLinearGradient

    def write(self, w):
        for b in (self.flag_30, self.flag_31, self.flag_32, self.flag_33):
            w.boolean(b)
        for f in (self.float_34, self.float_38, self.float_3c, self.float_40, self.float_48):
            w.f64(f)
        w.qcolor(self.color_4c); w.qcolor(self.color_5c)
        _write_stops(w, self.stops)

    @classmethod
    def read(cls, r):
        return cls(r.boolean(), r.boolean(), r.boolean(), r.boolean(),
                   r.f64(), r.f64(), r.f64(), r.f64(), r.f64(),
                   r.qcolor(), r.qcolor(), _read_stops(r))


@dataclass
class ArcStyle:
    """DispCircularProgressBar blob (FUN_140012bc0 / FUN_140011e30).

    Meaning NOT confirmed; ctor defaults from FUN_140011af0 noted.
    """
    flag_30: bool = True
    flag_31: bool = True
    flag_32: bool = False
    color_34: QColor = field(default_factory=QColor)   # default Qt::gray
    color_44: QColor = field(default_factory=QColor)   # default Qt::white
    stops: List[GradientStop] = field(default_factory=list)  # default 1.0 green, 0.5 yellow, 0.0 red
    float_60: float = 10.0    # default 10
    float_64: float = 90.0    # default 90
    float_6c: float = 360.0   # default 360
    float_70: float = 0.0     # default 0
    float_74: float = 100.0   # default 100

    def write(self, w):
        for b in (self.flag_30, self.flag_31, self.flag_32):
            w.boolean(b)
        w.qcolor(self.color_34); w.qcolor(self.color_44)
        _write_stops(w, self.stops)
        for f in (self.float_60, self.float_64, self.float_6c, self.float_70, self.float_74):
            w.f64(f)

    @classmethod
    def read(cls, r):
        return cls(r.boolean(), r.boolean(), r.boolean(), r.qcolor(), r.qcolor(),
                   _read_stops(r), r.f64(), r.f64(), r.f64(), r.f64(), r.f64())


@dataclass
class ClockStyle:
    """DispAnalogClock blob (FUN_14000b7f0 / FUN_14000b140).

    Strings are image paths for dial / hour / minute / second hand (setters
    FUN_14000b560/b610/b6b0/b750); the three ints are passed to the hand
    setters (default 10 each; meaning UNKNOWN, possibly pivot offset).
    """
    dial_image: Optional[str] = ""
    hour_image: Optional[str] = ""
    minute_image: Optional[str] = ""
    second_image: Optional[str] = ""
    hour_param: int = 10
    minute_param: int = 10
    second_param: int = 10
    color: QColor = field(default_factory=QColor)  # default Qt::white
    flag_6e: bool = True

    def write(self, w):
        for s in (self.dial_image, self.hour_image, self.minute_image, self.second_image):
            w.qstring(s)
        w.i32(self.hour_param); w.i32(self.minute_param); w.i32(self.second_param)
        w.qcolor(self.color); w.boolean(self.flag_6e)

    @classmethod
    def read(cls, r):
        return cls(r.qstring(), r.qstring(), r.qstring(), r.qstring(),
                   r.i32(), r.i32(), r.i32(), r.qcolor(), r.boolean())


def _blob_to_style(blob: Optional[bytes], style_cls):
    """Parse an embedded QByteArray; keep raw bytes if it does not fit."""
    if blob is None:
        return None
    try:
        r = _Reader(blob)
        st = style_cls.read(r)
        if r.remaining() == 0:
            return st
    except ThemeFormatError:
        pass
    return blob


def _style_to_blob(style) -> Optional[bytes]:
    if style is None or isinstance(style, (bytes, bytearray)):
        return style
    w = _Writer()
    style.write(w)
    return w.bytes()


@dataclass
class ProgressBarWidget:
    """Type 8. rect = (x, y, w, h)."""
    TYPE = 8
    name: Optional[str] = "progress_bar_0"
    rect: Tuple[float, float, float, float] = (0.0, 0.0, 100.0, 100.0)
    sensor: int = 1
    style: Union[ProgressBarStyle, bytes, None] = None

    def write(self, w):
        w.qstring(self.name); w.qrectf(self.rect); w.i32(self.sensor)
        w.qbytearray(_style_to_blob(self.style))

    @classmethod
    def read(cls, r):
        return cls(r.qstring(), r.qrectf(), r.i32(),
                   _blob_to_style(r.qbytearray(), ProgressBarStyle))


@dataclass
class ArcWidget:
    """Type 9 (object name "gauge_%1"; circular progress bar)."""
    TYPE = 9
    name: Optional[str] = "gauge_0"
    rect: Tuple[float, float, float, float] = (0.0, 0.0, 80.0, 80.0)
    sensor: int = 1
    style: Union[ArcStyle, bytes, None] = None

    def write(self, w):
        w.qstring(self.name); w.qrectf(self.rect); w.i32(self.sensor)
        w.qbytearray(_style_to_blob(self.style))

    @classmethod
    def read(cls, r):
        return cls(r.qstring(), r.qrectf(), r.i32(),
                   _blob_to_style(r.qbytearray(), ArcStyle))


@dataclass
class GaugeWidget:
    """Type 10: needle gauge; only geometry + sensor are persisted."""
    TYPE = 10
    name: Optional[str] = "gauge_0"
    rect: Tuple[float, float, float, float] = (0.0, 0.0, 80.0, 80.0)
    sensor: int = 1

    def write(self, w):
        w.qstring(self.name); w.qrectf(self.rect); w.i32(self.sensor)

    @classmethod
    def read(cls, r):
        return cls(r.qstring(), r.qrectf(), r.i32())


@dataclass
class AnalogClockWidget:
    """Type 11 (object name "clock_%1")."""
    TYPE = 11
    name: Optional[str] = "clock_0"
    rect: Tuple[float, float, float, float] = (0.0, 0.0, 100.0, 100.0)
    style: Union[ClockStyle, bytes, None] = None

    def write(self, w):
        w.qstring(self.name); w.qrectf(self.rect)
        w.qbytearray(_style_to_blob(self.style))

    @classmethod
    def read(cls, r):
        return cls(r.qstring(), r.qrectf(), _blob_to_style(r.qbytearray(), ClockStyle))


@dataclass
class RawRecord:
    """Record that could not be decoded (unknown type or layout mismatch).

    payload excludes the 4-byte LE size prefix but includes the type id.
    """
    payload: bytes = b""
    error: str = ""

    @property
    def type_id(self) -> Optional[int]:
        return struct.unpack(">i", self.payload[:4])[0] if len(self.payload) >= 4 else None


_CLASSES = {c.TYPE: c for c in (
    SceneSettings, ImageWidget, TextWidget, DateWidget, TimeWidget, WeekdayWidget,
    NumberWidget, ScreenCaptureWidget, ProgressBarWidget, ArcWidget, GaugeWidget,
    AnalogClockWidget)}

Record = Union[SceneSettings, ImageWidget, TextWidget, DateWidget, TimeWidget,
               WeekdayWidget, NumberWidget, ScreenCaptureWidget, ProgressBarWidget,
               ArcWidget, GaugeWidget, AnalogClockWidget, RawRecord]


# --------------------------------------------------------------------------
# Top level
# --------------------------------------------------------------------------
def _decode_payload(payload: bytes) -> Record:
    r = _Reader(payload)
    try:
        t = r.i32()
        cls = _CLASSES.get(t)
        if cls is None:
            return RawRecord(payload, f"unknown type {t}")
        rec = cls.read(r)
        if r.remaining():
            return RawRecord(payload, f"type {t}: {r.remaining()} trailing bytes")
        # paranoia: re-encode must reproduce payload, otherwise keep raw
        if _encode_payload(rec) != payload:
            return RawRecord(payload, f"type {t}: re-encode mismatch")
        return rec
    except ThemeFormatError as e:
        return RawRecord(payload, str(e))


def _encode_payload(rec: Record) -> bytes:
    if isinstance(rec, RawRecord):
        return bytes(rec.payload)
    w = _Writer()
    w.i32(rec.TYPE)
    rec.write(w)
    return w.bytes()


def parse(data: bytes) -> dict:
    """Parse a .theme file. Mirrors the loader loop of FUN_140031490."""
    records: List[Record] = []
    p = 0
    n = len(data)
    while p < n:
        if p + 4 > n:
            raise ThemeFormatError(f"truncated size prefix at {p}")
        size = struct.unpack_from("<I", data, p)[0]
        if size < 4 or p + size > n:
            raise ThemeFormatError(f"bad record size {size} at offset {p}")
        records.append(_decode_payload(data[p + 4:p + size]))
        p += size
    return {"records": records}


def serialize(theme: dict) -> bytes:
    out = []
    for rec in theme["records"]:
        payload = _encode_payload(rec)
        out.append(struct.pack("<I", len(payload) + 4))
        out.append(payload)
    return b"".join(out)


def parse_file(path) -> dict:
    with open(path, "rb") as f:
        return parse(f.read())


def write_file(path, theme: dict) -> None:
    with open(path, "wb") as f:
        f.write(serialize(theme))


def to_jsonable(obj):
    """Convert parsed structures to JSON-friendly data (bytes -> hex)."""
    if is_dataclass(obj):
        d = {"_type": type(obj).__name__}
        for k, v in asdict(obj).items():
            d[k] = to_jsonable(getattr(obj, k))
        if isinstance(obj, QColor):
            d["hex"] = obj.hex()
        return d
    if isinstance(obj, dict):
        return {k: to_jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [to_jsonable(v) for v in obj]
    if isinstance(obj, (bytes, bytearray)):
        return {"_bytes_hex": bytes(obj).hex()}
    return obj


def _main(argv):
    import json
    import glob
    import os
    if len(argv) >= 2 and argv[1] == "verify":
        root = argv[2] if len(argv) > 2 else r"C:\Program Files (x86)\SmartMonitor28 A3"
        files = sorted(glob.glob(os.path.join(root, "theme_*", "*.theme")))
        ok = 0
        for fn in files:
            data = open(fn, "rb").read()
            th = parse(data)
            raw = [r for r in th["records"] if isinstance(r, RawRecord)]
            same = serialize(th) == data
            types = [WIDGET_TYPES.get(getattr(r, "TYPE", -1), "RAW") for r in th["records"]]
            print(f"{'OK ' if same and not raw else 'BAD'} {os.path.basename(fn)} "
                  f"{len(data)}B records={len(th['records'])} raw={len(raw)} {types}")
            for r in raw:
                print("    raw:", r.error)
            ok += same and not raw
        print(f"{ok}/{len(files)} files parsed fully and round-tripped byte-for-byte")
        return 0 if ok == len(files) else 1
    if len(argv) == 2:
        print(json.dumps(to_jsonable(parse_file(argv[1])), indent=1, ensure_ascii=False))
        return 0
    print("usage: theme_format.py <file.theme> | verify [theme_root]")
    return 2


if __name__ == "__main__":
    sys.exit(_main(sys.argv))
