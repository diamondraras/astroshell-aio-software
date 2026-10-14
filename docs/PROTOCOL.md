# SmartMonitor28 A3 – Host <-> Device Protocol (reverse-engineered)

Target: `C:\Program Files (x86)\SmartMonitor28 A3\SmartMonitor28 A3.exe`
(x64 PE, sha256 `769c4d72e3ab6f7454b49ecc506aef2e8de7b115797ab52c5509b3871d174665`), analysed with REA/Ghidra 12.1.4.

Legend: **[OBS]** = observed directly in decompilation/assembly (function address given).
**[INF]** = inferred (library semantics, naming, or reasoning); verify on hardware.
**UNKNOWN** = not determined.

---

## 0. TL;DR for a re-implementation

1. Open USB device **VID 0x1F3A / PID 0x0007**, configuration 1, interface 0, alt 0.
2. Bulk OUT EP **0x01**, bulk IN EP **0x81**.
3. Every host->device message uses an 8-byte header:
   ```
   5A A5 <cmd> 00 <len:u32 little-endian> <payload[len]>
   ```
4. On open, send `5A A5 81 00 01 00 00 00 01` (query version/hello).
5. The device answers with `5A A5 81 ...`. If the reply contains ASCII `Smart Monitor v0.0.1` → "old firmware", screen is 320x320.
   Otherwise ("new firmware") host sends `5A A5 90 00 01 00 00 00 01`; device answers `5A A5 90 ...`; host then uses 320x320.
6. Render a 320x320 BGRA frame, rotate on host if needed, convert to YUV420P, encode with **H.264 (FFmpeg `libopenh264`)**, 4 Mbit/s, GOP 10, 25 fps time base, and send
   ```
   5A A5 85 00 <h264_len:u32 LE> <H.264 bitstream bytes>
   ```
   once per frame, frame timer 25 ms.
7. Brightness: `5A A5 80 00 01 00 00 00 <0..255>`; Reboot: `5A A5 86 00 01 00 00 00 01`.
8. Firmware upgrade: commands 0x82/0x83/0x84 with 0xFF8-byte chunks, each gated by a device ACK `5A A5 ?? 8F <nonzero>`.

---

## 1. USB identification, interface, endpoints

### 1.1 VID/PID [OBS]
`UsbExample::UsbExample` ctor **FUN_14004f2c0**:
```c
FUN_1400509f0(this + 0x48, 7, 0x1f3a, 0xff, 0xff, 0, 0);   // QUsb::Id(pid, vid, bus, port, class, subclass)
... QDebug() << "usb started";
```
`FUN_1400509f0` stores `+0 = 7`, `+2 = 0x1f3a`, `+4 = 0xff`, `+5 = 0xff`, `+6 = 0`, `+7 = 0`.

Hot-plug slot **FUN_14004fe00** ("Device inserted: %04x:%04x") prints `id[1]:id[0]` and accepts the device only if
`id[1] == *(u16*)(this+0x4a)` and `id[0] == *(u16*)(this+0x48)`.

- Struct layout matches QtUsb `QUsb::Id { quint16 pid; quint16 vid; quint8 bus; quint8 port; quint8 dClass; quint8 dSubClass; }` and QtUsb `busAny = portAny = 0xFF`. **[INF]** (QtUsb library semantics)
- => **VID = 0x1F3A, PID = 0x0007** (log prints `1f3a:0007`). VID 0x1F3A is the Allwinner Technology vendor ID **[INF]**.

### 1.2 Configuration / interface [OBS + INF]
In **FUN_14004fe00** (asm 0x14004fed1..0x14004feea):
```
MOV DL,1 ; R8D=0 ; R9D=0 ; CALL FUN_1400509b0   -> QUsb::Config(1, 0, 0)
CALL FUN_1400558d0(dev, cfg)                    -> QUsbDevice::setConfig
```
=> **configuration 1, interface 0, alternate setting 0** (field meaning from QtUsb `Config{config, interface, alternate}`, **[INF]**).
Before that `FUN_140055900(dev, id)` = `QUsbDevice::setId`.

### 1.3 Endpoints [OBS]
`UsbExample::openHandle` **FUN_140050110**:
```c
this+0x58 = new QUsbEndpoint(dev, 2, 0x81);   // read endpoint
this+0x60 = new QUsbEndpoint(dev, 2, 0x01);   // write endpoint
connect(this+0x58, "2readyRead()",            this, "1onReadyRead()");
connect(this+0x60, "2bytesWritten(qint64)",   this, ...);
ep_in ->open(1 /*ReadOnly*/);  then FUN_140054190(ep_in, 1)   // [INF] setPolling(true)
ep_out->open(2 /*WriteOnly*/);
```
- Type `2` = `QUsbEndpoint::bulkEndpoint` in QtUsb (Control=0, Iso=1, Bulk=2, Interrupt=3) **[INF]**.
- **IN = 0x81 (bulk), OUT = 0x01 (bulk)**.
- If open fails: `"Could not open device!"`, runs the adb cleanup (FUN_14004f610, see §4) and retries up to ~11 times.
- After open: emits connected signal (FUN_14006c180(this,1)), logs `"Device open!"`, and immediately sends the hello/version query (FUN_14004fca0, §3.2).

### 1.4 Low-level write [OBS]
**FUN_140050370** (`UsbExample::write(QByteArray)`): if device open; if status == -6 (`LIBUSB_ERROR_BUSY`) logs `"!!!!busy now: no write"` and sleeps 500 µs; if status still non-zero → `"usb error"` and drop; else `QIODevice::write(ep_out, data, len)` (single call, entire buffer). `"write failed"` on negative return.
- No application-level chunking of the frame payload: the whole `header + H.264 frame` buffer goes into one `write()` **[OBS]**. How QtUsb splits it into bulk transfers is library-internal (**[INF]** a single `libusb_bulk_transfer` of the whole buffer; UNKNOWN whether the device needs ZLP handling).

### 1.5 Read path [OBS]
**FUN_140050060** (`UsbExample::onReadyRead`): `readAll(ep_in)` appended to `this+0x70`, copy is emitted as a signal (FUN_14006c130 → queued to Transport), buffer cleared.
Transport side: **FUN_14004da40** (slot object) → **FUN_14004df60** (`Transport::onRecv`, §3.4).

---

## 2. Video path – Transport::run / initEncoder

### 2.1 Frame source and timer [OBS]
- **FUN_1400329b0** (scene resize slot, receives `(w,h)` from Transport size signal):
  stop timer; `setSceneRect(0,0,w,h)`; `Transport::reinitEncoder(w,h)` (**FUN_14004e990** → deinit FUN_14004d640 + initEncoder FUN_14004dad0); new `QPixmap(w,h)`; **`QTimer::start(25)`** → 25 ms period (~40 fps max tick).
- **FUN_140032ca0** (timer slot): fill pixmap with `QColor(Qt::white)` (GlobalColor 3), render QGraphicsScene into it, `toImage()`, then **rotate on the host** by 90/180/270 degrees when `this+0x48` == 1/2/3 (`QMatrix::rotate` + `QImage::transformed`), then `Transport::pushFrame(img)` (**FUN_14004e8f0**).
  - Rotation is therefore *not* a device command; the device always receives an already-rotated image **[OBS]**.
- **FUN_14004e8f0** pushFrame: accepted only if `img.width()==this+0x10 && img.height()==this+0x14` and no firmware upgrade pending (`this+0x76==0`); appended to list (`this+0x18`, max 2 entries, FUN_14004d520) under mutex `+0x20`, then `QSemaphore(+0x28).release()`.

### 2.2 Encoder init – Transport::initEncoder FUN_14004dad0 [OBS]
FFmpeg 6 DLLs are shipped (avcodec-60, avutil-58, swscale-7). AVCodecContext offsets below match FFmpeg 6 layout **[INF for field names]**:

| Code | Field | Value |
|---|---|---|
| `avcodec_find_encoder_by_name("libopenh264")` | codec | OpenH264 via FFmpeg |
| `ctx+0x38 = 0x3d0900` | bit_rate | **4 000 000** bit/s |
| `ctx+0x74 / +0x78` | width / height | from caller (w,h) |
| `ctx+0x64 = {1, 25}` | time_base | **1/25** |
| `ctx+0x2c0 = {25, 1}` | framerate | **25/1** |
| `ctx+0x84 = 10` | gop_size | **10** |
| `ctx+0xa0 = 1` | max_b_frames | 1 (OpenH264 does not produce B-frames **[INF]**) |
| `ctx+0x88 = 0` | pix_fmt | **AV_PIX_FMT_YUV420P** |
| `if codec->id == 0x1b (AV_CODEC_ID_H264) av_opt_set(ctx->priv_data, "preset", "slow", 0)` | preset | `"slow"` (string at 0x1433855c8 = `736c6f77`). libopenh264 has no `preset` option, so this is likely a no-op **[INF]** |

Then `avcodec_open2`, `av_frame_alloc` (format=YUV420P, w, h), `av_frame_get_buffer(frame, 0)`, and
`sws_getContext(w, h, 0x1c /*AV_PIX_FMT_BGRA*/, w, h, 0 /*YUV420P*/, 2 /*SWS_BILINEAR*/, 0,0,0)` (asm 0x14004dda6..0x14004ddde).
Stores `this+0x10 = w`, `this+0x14 = h`. Logs `"Transport::initEncoder: done, w=..,h=.."`.

### 2.3 Transport::run FUN_14004e240 [OBS]
Loop forever:
1. If firmware upgrade requested (`this+0x76`): set ack flag `this+0x80 = 1`, run FUN_14004eaa0 (§3.5), clear `+0x76`.
2. `semaphore.acquire()`, pop the first queued QImage.
3. `sws_scale` BGRA → YUV420P (error: `"failed to translate rgb to yuv"` → thread returns!).
4. `frame->pts = 0` (offset 0x88 set to 0 every frame **[OBS]**), `avcodec_send_frame`, then loop `avcodec_receive_packet` until EAGAIN(-11)/EOF, appending every `pkt->data/size` to `this+0x60` buffer.
5. If encoder frame size == current target size (`frame->w == this+0x10 && frame->h == this+0x14`):
   - if `brightness(+0x6c) != lastBrightness(+0x70)`: send **CMD 0x80** (§3.1), update last.
   - if version request flag `+0x74`: send **CMD 0x81** payload `+0x74` (=1), clear flag.
   - if reboot flag `+0x75`: send **CMD 0x86** payload `+0x75` (=1), clear flag.
   - send frame packet:
     ```c
     local_178 = 0x0085a55a;              // bytes 5A A5 85 00
     local_174 = h264buf.size();          // u32 LE
     pkt = FUN_14004e9f0(&local_178)      // 8 raw bytes
         + h264buf;                       // encoded access unit(s)
     UsbExample::write(pkt);
     ```
   - clear h264 buffer.
   Otherwise the encoded data is discarded.

**Frame packet layout [OBS]:**
```
offset 0  : 5A
offset 1  : A5
offset 2  : 85            (command: video frame)
offset 3  : 00
offset 4-7: N  (uint32 little-endian) = length of H.264 data
offset 8..: N bytes H.264 elementary stream as output by FFmpeg libopenh264 for one input frame
```
- Bitstream format: FFmpeg libopenh264 emits Annex-B (start codes `00 00 00 01`), SPS/PPS in-band on IDR frames because `AV_CODEC_FLAG_GLOBAL_HEADER` is not set **[INF]**.
- No per-frame ack/flow control is used for video **[OBS]** (only firmware upgrade waits for 0x8F).
- No chunking at protocol level; the full packet is written at once **[OBS]**.

---

## 3. Command set

### 3.0 Generic host->device packet [OBS]
```
5A A5 CC 00 LL LL LL LL <payload>
```
`CC` = command, `LL..` = payload length, uint32 little-endian. Every sender (callers of FUN_140050370) is in the table below; there are only four call sites: FUN_14004e240, FUN_14004eaa0, FUN_14004fbe0, FUN_14004fca0 **[OBS]**.

| Cmd | Bytes (hex) | Meaning | Evidence |
|---|---|---|---|
| 0x80 | `5A A5 80 00 01 00 00 00 BB` | Set brightness, BB = slider% * 255 / 100 (0..255) | FUN_14004e240; setter FUN_14004e970 (`if (p<=100) +0x6c = p*0xff/100`); slider 0..100 created in FUN_14001d2f0 ("Brightness:") |
| 0x81 | `5A A5 81 00 01 00 00 00 01` | Hello / get device version | FUN_14004fca0 (on open, also sets "probe" flag UsbExample+0x10 = Transport+0x98 = 1); FUN_14004e240 (flag +0x74 set by FUN_14004da30, triggered by Settings "connect" button lambda FUN_14003bb60 next to "Device Version") |
| 0x82 | `5A A5 82 00 F8 0F 00 00 <0xFF8 bytes>` | Firmware: first chunk | FUN_14004eaa0 |
| 0x83 | `5A A5 83 00 F8 0F 00 00 <0xFF8 bytes>` | Firmware: middle chunk | FUN_14004eaa0 |
| 0x84 | `5A A5 84 00 <rem u32 LE> <rem bytes>` | Firmware: last chunk (length = remaining bytes) | FUN_14004eaa0 |
| 0x85 | `5A A5 85 00 <N u32 LE> <H.264>` | Video frame | FUN_14004e240 |
| 0x86 | `5A A5 86 00 01 00 00 00 01` | Reboot device | FUN_14004e240; flag +0x75 set by FUN_14004d9c0, called from lambda FUN_14003bcf0 bound to the "Reboot" tool button in settings FUN_14003a8f0 (only if device open, FUN_14004da20) |
| 0x90 | `5A A5 90 00 01 00 00 00 01` | Query screen info (new firmware) | FUN_14004fbe0, sent from FUN_14004df60 when hello reply lacks "Smart Monitor v0.0.1" |

Not device commands (host-side only) **[OBS]**:
- **Rotation** – image rotated on host (FUN_140032ca0).
- **"clear the screen"** – tooltip of a `DispColorSelectButton` in FUN_14001d2f0 (canvas background colour picker, handler FUN_1400210c0). No USB call site other than the four listed exists, so it only affects the rendered canvas.

### 3.1 Brightness [OBS]
Default value read from settings (FUN_140001520) and applied at UI build time; change is detected in run() by comparing `+0x6c` and `+0x70`, so it is sent just before the next video frame.

### 3.2 Hello / version handshake [OBS]
On device open (FUN_14004fe00 → FUN_14004fca0):
```
UsbExample+0x10 (= Transport+0x98) = 1
send 5A A5 81 00 01 00 00 00 01
```

### 3.3 Screen size learning [OBS]
Size is **not parsed from the reply payload**; it is hard-coded. In FUN_14004df60:
- reply cmd 0x81 with probe flag set → clear flag; if payload contains `"Smart Monitor v0.0.1"`: `w=h=0x140` (320), log `"old firmware....."`, emit sizeChanged(320,320).
- otherwise: log `"new firmware....."`, send CMD 0x90.
- reply cmd 0x90 → `w=h=0x140` (320), log `"mWidth: 320 , mHeight: 320"`, emit sizeChanged(320,320).
- sizeChanged → FUN_1400329b0 → encoder (re)initialised at 320x320, 25 ms timer started.
- **UNKNOWN**: where 480x480 or 320x240 sizes come from; no such dimension was used in the USB code paths examined. The 0x90 reply payload format is UNKNOWN (ignored by this version of the host).

### 3.4 Device -> host replies, parser FUN_14004df60 [OBS]
Copies up to the first 32 bytes; logs `"recv data size: "`.
```
byte0 == 0x5A && byte1 == 0xA5      (byte2 not checked)
switch (byte3):
  0x81: if (probe flag +0x98) { see 3.3 }
        else { version = QString::fromUtf8(reply.mid(5)); emit versionString(version) }   // shown in "Device Version" line edit (FUN_14003b9e0)
  0x8F: ackFlag(+0x80) = byte4          // firmware chunk ACK
  0x90: width = height = 320; emit sizeChanged(320,320)
```
So device replies are `5A A5 xx <cmd> ...`; the version string starts at offset 5 (byte4 likely a length/status byte, **UNKNOWN**). Byte2 value is UNKNOWN (ignored).

### 3.5 Firmware upgrade FUN_14004eaa0 [OBS]
Triggered by FUN_14004d9d0(path): `+0x76 = 1`, `+0x78 = path`, semaphore release. run() sets `ack(+0x80)=1` and calls FUN_14004eaa0:
1. Read whole file. Bytes `[0..0x23]` copied unchanged; every byte `i >= 0x24` becomes `(b ^ 0x48) ^ 0x4A` (net effect **`b ^ 0x02`**).
2. `totalPack = ceil(len / 0xFF8)`; log `"totalPack: "`.
3. Loop (10 ms sleep each iteration):
   - if `ack == 0`: increment wait counter; after 200 waits (~2 s) → `"time out....."`, emit updateFirmwareState(0) (FUN_14006be80), abort.
   - else `ack = 0` and send next packet:
     - packet 0: `5A A5 82 00 F8 0F 00 00` + next 0xFF8 bytes ("send 0x82")
     - last packet (index == totalPack-1): `5A A5 84 00 <remaining u32 LE>` + remaining bytes, then emit updateFirmwareState ("send 0x84")
     - others: `5A A5 83 00 F8 0F 00 00` + next 0xFF8 bytes ("send 0x83")
   - Edge case: if totalPack == 1 only a 0x82 packet is sent (no 0x84) **[OBS]**.
4. Device must answer each chunk with `5A A5 ?? 8F <nonzero>` to release the next one (ack written from byte4).

---

## 4. Disconnect / adb

- **FUN_14004ffa0** (`"Device removed: %04x:%04x"`): if VID/PID match → close endpoints (FUN_14004fa60 `"Closing Handle"`), `QUsbDevice::close`, emit connected(false), delete device. **No USB command is sent on disconnect** [OBS].
- **FUN_14004f610** runs **only when opening the device fails** (from FUN_14004fe00, before each retry), not on normal disconnect [OBS]:
  - `QProcess::start("adb", ["devices"])`, `waitForFinished(30000)` → `"Successfully Show List of devices attached"` / `"Show List of devices attached fail: "`.
  - `QProcess::start("adb", ["kill-server"])`, `waitForFinished(30000)` → `"Successfully close adb port"` / `"Failed to close adb port: "`.
  - `adb` is resolved via PATH; no adb.exe is shipped in the install dir [OBS dir listing]. Purpose (inferred): free the USB interface if an adb server has claimed the device.
- Frame rate: 25 ms QTimer for rendering (FUN_1400329b0); encoder declares 25 fps. Actual send rate is bounded by the encoder thread (queue max 2 frames) **[OBS/INF]**.

---

## 5. HWiNFO64.dll (system sensors)

Loader **FUN_140037a70**: `QLibrary(applicationDirPath() + "/HWiNFO64.dll")`, then `resolve()` with **small integer values** in RDX (asm 0x140037acb..0x140037b55) – i.e. **imports by ordinal** via GetProcAddress (QLibrary passes the pointer through) [OBS]. The shipped HWiNFO64.dll exports 801 functions by ordinal (base 106) with **no names** (export table parsed locally) [OBS], so real names are UNKNOWN.

| Global | Ordinal | Inferred role / signature [INF from call sites] |
|---|---|---|
| DAT_1433c7948 | 590 (0x24E) | `int Init(int flags)` called with `0x840`; non-zero → `"init failed: <rc>"` |
| DAT_1433c7950 | 906 (0x38A) | `void Deinit(void)` – called in DispSensorManager dtor FUN_140034c30 |
| DAT_1433c7958 | 883 (0x373) | `int GetSensorCount(void)` |
| DAT_1433c7960 | 726 (0x2D6) | `void Refresh/Update(void)` – called once before enumeration |
| DAT_1433c7968 | 749 (0x2ED) | `void SelectSensor(int idx)` / per-sensor prep |
| DAT_1433c7970 | 618 (0x26A) | `void GetSensorName(int idx, char* buf[, int size=0x100])` |
| DAT_1433c7978 | 106 (0x6A)  | `bool GetReading(int type, int sensorIdx, int readingIdx, READING* out /*0x1D0 bytes*/)`; readingIdx 0..0x1FF |

Reading types queried: 1, 3, 5, 6, 7, 8 – consistent with HWiNFO `SENSOR_READING_TYPE` (1 Temp, 3 Fan, 5 Power, 6 Clock, 7 Usage, 8 Other) **[INF]**.

`READING` (0x1D0 bytes) offsets used [OBS FUN_140038600 / FUN_140039a00 / FUN_1400393b0]:
`+0x08 double value`, `+0x10 char unit[]` (overwritten with °C/°F, F = C*9/5+32), `+0x30 int category`, `+0x3C char label[]`, `+0x13C int`, `+0x148 char label2[]`, `+0x1C8 int readingType`.
Category values filtered: 0x0B (CPU; Intel "Enhanced" check), 0x0C (only type 3), 0x0D (GPU), 0x0F (only type 1), 0x10 – exact meaning **[INF]**, partly UNKNOWN. CPU vendor detected from sensor name containing "Intel"/"AMD".

Default display mapping table FUN_140038da0 `(type, group, reading label) -> display` [OBS]:

| Type | Sensor group | Reading label | Display |
|---|---|---|---|
| 6 (clock) | `CPU [#0]:` | `Core 0 Clock` | CPU Freq |
| 1 (temp)  | `CPU [#0]:` | `CPU Die` | CPU Temp |
| 7 (usage) | `CPU [#0]:` | `Total CPU Usage` | CPU Usage |
| 6 (clock) | `GPU [#0]:` | `GPU Clock` | GPU Freq |
| 1 (temp)  | `GPU [#0]:` | `GPU Temperature` | GPU Temp |
| 7 (usage) | `GPU [#0]:` | `GPU Core Load` | GPU Usage |

Other label strings present near the table (alternates, not fully traced): `Effective`, `DTS`, `GPU D3D Usage`, `GPU Freq`, `::`, `Enhanced`.
Network: sensors whose group starts with `Network:` are collected into a NIC list (FUN_140036c80), name = group minus the `Network:` prefix. Disk: UNKNOWN (no disk-specific label found; category 0x0F type-temp list is a candidate **[INF]**).

---

## 6. Key function index

| Address | Role |
|---|---|
| FUN_14004f2c0 | UsbExample ctor, VID/PID |
| FUN_14004fe00 | onDeviceInserted / setupDevice |
| FUN_14004ffa0 | onDeviceRemoved |
| FUN_140050110 | openHandle (endpoints 0x81/0x01) |
| FUN_14004fa60 | closeHandle |
| FUN_140050370 | write(QByteArray) |
| FUN_140050060 | onReadyRead |
| FUN_14004fca0 | send 0x81 hello |
| FUN_14004fbe0 | send 0x90 |
| FUN_14004f610 | adb devices / adb kill-server |
| FUN_14004dad0 | Transport::initEncoder |
| FUN_14004e990 | Transport reinit encoder (w,h) |
| FUN_14004e240 | Transport::run |
| FUN_14004df60 | Transport recv parser |
| FUN_14004eaa0 | firmware upgrade |
| FUN_14004e970 | setBrightness(percent) |
| FUN_14004da30 / FUN_14004d9c0 / FUN_14004d9d0 | request version / reboot / firmware |
| FUN_1400329b0 / FUN_140032ca0 | scene resize + 25 ms timer / frame render + rotate |
| FUN_140037a70 | HWiNFO64.dll loader |
| FUN_1400350e0 / FUN_1400393b0 | sensor enumeration / refresh |
| FUN_140038da0 | sensor display mapping table |

## 7. Open items / UNKNOWN
- Format of 0x81 reply beyond "version string from offset 5"; meaning of reply byte2/byte4.
- 0x90 reply payload (ignored; host hard-codes 320x320).
- Whether any device variant uses 480x480 / 320x240 (not observed in USB path).
- HWiNFO64.dll real export names (ordinal-only DLL).
- USB transfer-level details (QtUsb bulk transfer sizes, ZLP, timeouts).
