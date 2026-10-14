# SmartMonitor28 A3 `.theme` file format

Target: `C:\Program Files (x86)\SmartMonitor28 A3\SmartMonitor28 A3.exe`
(x64 PE, stripped, Qt 5.12.12, sha256 `769c4d72e3ab6f7454b49ecc506aef2e8de7b115797ab52c5509b3871d174665`).
Tooling: REA / Ghidra 12.1.4 decompilation. All `FUN_xxxx` / `0x14...` addresses refer to this binary.
The class names (`DispNumberWidget` etc.) come from the RTTI `vftable` symbols Ghidra recovered.

Reference implementation: `D:\rea-test\smartmonitor\theme_format.py`.
Verification (`python -I theme_format.py verify`): **39/39 sample themes parse with no unknown records,
and `serialize(parse(x)) == x` byte-for-byte for every one of them.** A synthetic round-trip covering
types 2, 7, 8, 9, 10, 11 and background mode 2 (which are absent from the samples) also passes. That only
shows the module agrees with itself. These encoders have not been checked against files written by the app.

Legend: **[O]** = observed directly in decompiled code or sample bytes. **[I]** = inferred (reasoned,
not proven). **UNKNOWN** = meaning not established.

---

## 1. Container

| Item | Value | Evidence |
|---|---|---|
| Header / magic | none [O] | loader starts reading records at offset 0 (`FUN_140031490`) |
| Record | `u32 size` **little-endian**, includes the 4 size bytes, followed by `size-4` bytes of payload [O] | loader: `QIODevice::read(4)`, `size-4`, then the `data corrupted` check. Writers patch bytes 0..3 of the QByteArray with `(char)len, len>>8, len>>16, len>>24` |
| Payload encoding | `QDataStream`, default settings: big-endian, version Qt_5_12 (18), `floatingPointPrecision = DoublePrecision` [O for big-endian; I for version 18, which matches the QFont layout] | all serializers build `QDataStream(QByteArray*, WriteOnly=2)` with no `setVersion` |
| First payload field | `i32` 0 at write time, overwritten by the LE size. When the loader reads it back as part of the payload it is the type id (see below) [O] | |

Detail: each serializer first writes `i32 0` and then `i32 type`. Bytes 0..3 of the QByteArray are later
replaced by the LE total length, so the record is `[LE size][BE type][fields...]`. Both loaders treat the
four bytes after the size prefix as the type:
- `FUN_140031490` (scene loader, "file %1 not found" xref `0x1400314f9`): `switch(type-1)` dispatches
  types 1..11 to `vtable+0xf0` (deserialize) of a freshly created widget. Type 0 falls through to the
  default case and is ignored.
- `FUN_140021300` (main-window loader, xref `0x140021376`): only handles records whose type == 0.

Writers:
- `FUN_140030d20` (DispScene save, "save done: " / "bytes=>"): iterates `QGraphicsScene::items(...)`.
  For every item with a non-empty `objectName` it appends `vtable+0xe8()` (serialize). Names containing
  `"text_"` go to `FUN_14003e780` (DispTextWidget). Opens the file with mode 10 (WriteOnly|Truncate).
- `FUN_14001ebc0` then opens the same file with `QIODevice::Append` (4) and appends the **type-0
  SceneSettings record last** [O].
- Path: `<dir>/theme_<baseName>/<fileName>.theme`, with resources copied into a `res` sub-folder
  [O: strings `"/theme_"` 0x14337b880, `"/"` 0x14337b878, `".theme"` 0x14337b870; the `res` name is I].
- Record order follows `QGraphicsScene::items()` order [O]. The loader does not depend on order, except
  that type 0 comes last.
- `DispLabelWidget` children (the `_unit` / `_name` labels of a number widget) also have object names,
  but their serialize (`FUN_1400097c0`) returns an empty QByteArray, so they produce **no record of
  their own** [O]. They are embedded in the parent type-6 record instead.

## 2. Qt primitive encodings (Qt 5.12, big-endian)

| Type | Encoding |
|---|---|
| bool | u8 0/1 |
| int | i32 |
| float (C++ `float` member) | **8-byte double** (DoublePrecision default) [I from Qt semantics; consistent with record sizes] |
| QString | u32 byte length (0xFFFFFFFF = null), then UTF-16BE |
| QByteArray | u32 length (0xFFFFFFFF = null), then the bytes |
| QPointF | f64 x, f64 y |
| QRectF | f64 x, f64 y, f64 w, f64 h |
| QSize | i32 w, i32 h |
| QColor | i8 spec (1 = Rgb), u16 alpha, u16 r, u16 g, u16 b, u16 pad (11 bytes). 8-bit value v is stored as v*0x101 |
| QFont (v18) | QString family, QString styleName, f64 pointSize, i32 pixelSize, u8 styleHint, u16 styleStrategy, u8 0 (charset), u8 weight, u8 bits, u16 stretch, u8 extendedBits, i32 letterSpacing (QFixed, 1/64), i32 wordSpacing, u8 hintingPreference, u8 capitalization |
| QVector\<QGradientStop\> | u32 count, then count × (f64 position, QColor) (`FUN_140011880`) |

QFont `bits`: 0x01 italic, 0x02 underline, 0x04 strikeOut, 0x08 fixedPitch, 0x10 kerning, 0x40 overline,
0x80 oblique (Qt source, [I]). The samples use `bits=0x10`, `extendedBits=0x01`, `stretch=0`
(AnyStretch), `styleHint=5`, `styleStrategy=1`, weight 75 (Bold) or 87 (Black), pixelSize -1 and
point sizes 10..30.

## 3. Record layouts

Every payload starts with `i32 type`. Below, "name" means `QString objectName`.

### Type 0: SceneSettings (writer `FUN_14001ebc0`, reader `FUN_140021300`)
| # | Field | Type | Meaning |
|---|---|---|---|
| 1 | background_mode | i32 | [O] 1: radio button @+0x1a8 checked, scene background = transparent. 2: radio @+0x1d0 checked, transparent. 3: neither checked, background brush = colour below, which is also pushed to the colour picker @+0x130 (`FUN_140013410`) |
| 2 | colour(s) | QColor | [O] mode 1 writes constant `QColor(Qt::black)`. Mode 3 writes the picker colour. **Mode 2 writes two QColors** (picker colour, then black), but the reader only reads one. This is an app bug: a mode-2 file is misparsed by the app itself. Our parser follows the writer |
| 3 | flag_0x88 | bool | [O] `panel@+0x88 → checkbox@+0x60` (`FUN_140028470` / `FUN_1400287c0`). Meaning **UNKNOWN**. False in all samples |
| 4 | datetime_flags[10] | 10 × bool | [O] checkboxes of the date/time panel @+0x90 (`FUN_140017680` / `FUN_140017f10`): [1] date fmt `yyyy/MM/dd`, [2] `MM/dd`, [3] `dd/MM/yyyy`, [4] `dd/MM`, [5] time fmt `hh:mm:ss ap`, [6] `hh:mm:ss`. [0], [7], [8], [9] **UNKNOWN** (buttons @+0x88, +0x68, +0x70, +0x78) |
| 5 | sensor_flags[8] | 8 × bool | [O] sensor panel @+0x98 (`FUN_1400256a0` / `FUN_1400263a0`): [0] home sensors shown (false hides every `data_home_sN`), [1..6] slot s1..s6 enabled (its value/name/unit visible; when unchecked the editor restores default colour/font). [7] checkbox @+0xa8 = auto carousel: when set, a QTimer starts with interval×1000 ms |
| 6 | carousel_interval | i32 | [O] QSpinBox @+0x70 value, in seconds [I for the unit: `QTimer::start(v*1000)`] |
| 7 | flag_0x1c0 | bool | [O] main-window checkbox @+0x1c0. Meaning **UNKNOWN** |
| 8 | width | i32 | [O] `*(scene+0x60)+0x10`. **Not read back** by `FUN_140021300`. 320 in all samples [I: canvas width] |
| 9 | height | i32 | [O] `*(scene+0x60)+0x14`. 320 in all samples [I: canvas height] |

Size: 55 bytes with one colour (all samples), 66 bytes with mode 2.

### Type 1: DispImageWidget (ser `FUN_1400297a0`, deser `FUN_140029220`, ctor `FUN_1400287d0`)
`name, QRectF rect(x,y,w,h), QString file` [O].
- `file` is a bare file name relative to `<theme>/res/`. On save the source is `QFile::copy`'d into res/.
- On load: `setFixedSize(w,h)`, `moveBy(x,y)`. If the name contains `image_theme_background`, z = -1
  (drawn behind everything).
- The player thread (`FUN_1400458f0`, ffmpeg DLLs shipped) also plays **.gif and .mp4**: 35 sample
  backgrounds are .mp4, 3 are .gif, and the 21 `image_home_mask` overlays are .png.
- Playback speed ("speed(fps):" UI string 0x14337dc28) is **not stored** in the theme [O: the serializer
  writes only the three fields].

### Types 2..5: text/date/time/weekday labels
Layout for all four: `name, QPointF pos, QColor color, QFont font, QString text` [O].
| Type | Class | ser / deser | Notes |
|---|---|---|---|
| 2 | DispTextWidget (QGraphicsTextItem) | `FUN_14003e780` / `FUN_14003df20` | text = plain text. Colour = `defaultTextColor`. Default name `text_%1` |
| 3 | DispDateWidget | `FUN_140014360` / `FUN_140014140` | default fmt `yyyy/MM/dd` (ctor `FUN_140013860`, refresh every 59 s) |
| 4 | DispTimeWidget | `FUN_1400455f0` / `FUN_140014140` | default fmt `hh:mm:ss` (ctor `FUN_140045320`, refresh every 1 s) |
| 5 | DispWeekdayWidget | `FUN_140046620` / `FUN_140014140` | fmt `dddd` (ctor `FUN_1400463d0`, refresh every 1 s). Locale-dependent: one sample stores "segunda-feira" |

For 3, 4 and 5 the `text` field is only a **snapshot** of the formatted value at save time (for example
"09:35:36" or "2024/10/16"). The format string is **not** in the widget record. It comes from the
date/time flags in the type-0 record (see above) or from the ctor default [O].

### Type 6: DispNumberWidget (sensor value; ser `FUN_14002bb40`, deser `FUN_14002b3f0`, ctor `FUN_14002a2b0`)
```
name, QPointF pos, i32 sensor, QColor color, QFont font, QString text
[ i32 1, LabelItem unit  ]    present if the unit child exists  (objectName "<name>_unit", default text "Hz")
[ i32 2, LabelItem label ]    present if the name child exists  (objectName "<name>_name", default text "Sensor?")
LabelItem = name, QPointF pos, QColor color, QFont font, QString text
```
- `sensor` (member +0x70, setter `FUN_14002bac0`) is an index into the sensor table built by
  `FUN_140038da0` [O]:

  | id | label | HWiNFO source | value range (`FUN_1400378e0`) |
  |---|---|---|---|
  | 0 | none (value updates ignored) | | 0..100 |
  | 1 | CPU Freq | `CPU [#0]:` / `Core 0 Clock` | 0..10000 |
  | 2 | CPU Temp | `CPU [#0]:` / `CPU Die` | 0..120 |
  | 3 | CPU Usage | `CPU [#0]:` / `Total CPU Usage` | 0..100 |
  | 4 | GPU Freq | `GPU [#0]:` / `GPU Clock` | 0..10000 |
  | 5 | GPU Temp | `GPU [#0]:` / `GPU Temperature` | 0..120 |
  | 6 | GPU Usage | `GPU [#0]:` / `GPU Core Load` | 0..100 |

  Samples: `data_home_s1..s6` always map to ids 2, 1, 3, 5, 4, 6 with labels CPU Temp / CPU Freq /
  CPU Usage / GPU Temp / GPU Freq / GPU Usage and units °C / MHz / % [O].
- Value formatting (slot `FUN_14002be90`): `text = QString::number(value, 'f', precision)`. The unit
  child text is set to the unit string supplied by the sensor source [O]. Where `precision` comes from is
  **UNKNOWN** (it is an argument of the sensor signal). Setting the sensor also sets the name child text
  to the sensor label (`FUN_140037940`).
- On load the parent `pos` is applied to the children first, then each child's own pos [O].
- Reader quirk [O]: the reader reads marker #1; if it is 1 it reads the unit block; then it **always**
  reads a second i32 marker and reads the name block if either marker == 2. A record containing only a
  name child (`2, block`) is therefore misread by the app. All 85 sample number widgets have both children.
- The stored `text` is a snapshot of the last value ("65", "1234", ...).

### Type 7: DispScreenCaptureWidget (ser `FUN_140034300`, deser `FUN_140033c40`, ctor `FUN_1400338e0`)
`name, QRectF capture_rect, QPointF pos, QSize widget_size` [O].
Default name `screen_capture`. Grabs the desktop region `capture_rect` into a QLabel every 33 ms
(`QTimer::start(0x21)`) [O]. Not in the samples.

### Type 8: DispProgressBarWidget (ser `FUN_14002f1f0`, deser `FUN_14002ea50`, ctor `FUN_14002cf30`)
`name, QRectF rect(pos,size), i32 sensor, QByteArray style` [O]. Default name `progress_bar_%1`.
`style` blob (`FUN_14002cd50` / `FUN_14002c0c0`, widget `DispProgressBar`, ctor `FUN_14002bf10`):

| field | type | ctor default |
|---|---|---|
| flag_30, flag_31, flag_32, flag_33 | 4 × bool | 0, 0, 1, 1 |
| float_34, float_38, float_3c, float_40, float_48 | 5 × f64 | 0, 2, 100, 30, 0 |
| color_4c, color_5c | 2 × QColor | Qt::gray, Qt::red |
| gradient stops | QVector\<QGradientStop\> | (0.0 green), (1.0 yellow) |

The meaning of each field is **UNKNOWN**. Editor strings "Border color:", "Height:" exist, so
border colour / bar height / min / max are plausible [I]. A float at +0x44 (default 15) and one at +0xb0
(default 50) are **not** serialized. Not in the samples.

### Type 9: DispArcWidget (ser `FUN_14000eb10`, deser `FUN_14000e470`, ctor `FUN_14000d240`)
`name, QRectF rect, i32 sensor, QByteArray style` [O]. Default name `gauge_%1` (same prefix as type 10).
`style` blob (`FUN_140012bc0` / `FUN_140011e30`, widget `DispCircularProgressBar`, ctor `FUN_140011af0`):

| field | type | ctor default |
|---|---|---|
| flag_30, flag_31, flag_32 | 3 × bool | 1, 1, 0 |
| color_34, color_44 | 2 × QColor | Qt::gray, Qt::white |
| gradient stops | QVector\<QGradientStop\> | (1.0 green), (0.5 yellow), (0.0 red) |
| float_60, float_64, float_6c, float_70, float_74 | 5 × f64 | 10, 90, 360, 0, 100 |

The meaning is **UNKNOWN** [I: arc width, start angle, span angle, min, max; the "Angle:" UI string
exists]. +0x68 (default -216) and +0x78 (default 60) are not serialized. On load the sensor value range
comes from `FUN_1400378e0` by sensor id, not from the file. Not in the samples.

### Type 10: DispGaugeWidget (ser `FUN_140015760`, deser `FUN_140015470`, ctor `FUN_140014b40`)
`name, QRectF rect, i32 sensor` [O]. Only geometry and sensor are stored. Dial, needle, colours and
"MHz" text are hard-coded in the ctor. The range follows `FUN_1400378e0`. Not in the samples.

### Type 11: DispAnalogClockWidget (ser `FUN_14000d0a0`, deser `FUN_14000c840`, ctor `FUN_14000b8e0`)
`name, QRectF rect, QByteArray style` [O]. Default name `clock_%1`.
`style` blob (`FUN_14000b7f0` / `FUN_14000b140`, widget `DispAnalogClock`, ctor `FUN_14000add0`):
`QString dial, QString hour, QString minute, QString second, i32 p_hour, i32 p_min, i32 p_sec, QColor color, bool flag_6e`.
- The four strings are passed to `FUN_14000b560` (dial) and `FUN_14000b610`/`b6b0`/`b750`
  (hands, each with its int). I assume they are image paths [I].
- The ints default to 10, the colour to Qt::white, the flag to true. Their meaning is **UNKNOWN**.
- Repaint every 100 ms. Not in the samples.

## 4. What the theme determines about rendering
- **Canvas**: 320×320 px [O: width/height in the type-0 record = 320/320 in all 39 samples; every
  background rect = (0, 0, 320, 320)]. No 480×480 or 320×240 values occur in the samples.
- **Coordinates**: scene coordinates in px (doubles). Label widgets store their top-left `pos`; proxy
  widgets store a full rect.
- **Background**: a type-1 image named `image_theme_background` (z = -1, png/gif/mp4), optionally with an
  `image_home_mask` png overlay. The scene background brush comes from the type-0 mode/colour.
- **Fonts and colours**: per label (QFont + QColor). The editor's "reset" defaults are Arial 48 cyan
  (value) and Arial 36 #5500ff (name/unit) (`FUN_1400263a0`).
- **Sensor values**: `QString::number(v,'f',prec)` in the value label, with the unit in the child label.
  Only the 6 sensors in §3 type 6 exist.
- **Date/time formats**: `yyyy/MM/dd`, `MM/dd`, `dd/MM/yyyy`, `dd/MM`; `hh:mm:ss`, `hh:mm:ss ap`;
  weekday `dddd` (locale-dependent). The choice is in SceneSettings.datetime_flags, not in the widget.
- Not stored at all: video fps, gauge styling, arc/progress fields not listed above.

## 5. baseInfo.cfg (INI, same folder)
Observed keys: `[disk] type`, `[gpuType] type`, `[network] type`, `[brightness] value=100`,
`[language] type`, `[rotate] value`, `[temp] uint`, `[autoRun] state`, `[theme] name=T39`
(the selected theme = folder `theme_<name>`). Their semantics were not reverse-engineered here
(UNKNOWN beyond the names).

## 6. UNKNOWN / open items
1. SceneSettings `flag_0x88`, `flag_0x1c0`, and datetime_flags[0], [7], [8], [9].
2. Meaning of the ProgressBarStyle / ArcStyle scalar and colour fields. Only offsets and defaults are known.
3. AnalogClock int parameters and `flag_6e`.
4. Where the decimal precision used for sensor formatting comes from.
5. Whether the device-side renderer (USB upload) consumes `.theme` directly or a rendered frame. This was
   not examined here (see PROTOCOL.md work).
6. The stream version is inferred as Qt_5_12 from the QFont layout (it includes styleName,
   hintingPreference and capitalization but not the Qt 5.13 `families` list), which matches the bundled Qt5 DLLs.
