"""USB transport to the SmartMonitor display (see docs/PROTOCOL.md).

Recovered from the original executable:
  * device VID:PID 1F3A:0007, configuration 1, interface 0, alt 0
  * bulk OUT 0x01, bulk IN 0x81
  * every host->device packet: 5A A5 <cmd> 00 <len u32 LE> <payload>
      0x80 brightness (1 byte, 0..255)    0x81 hello/version (01)
      0x82/0x83/0x84 firmware chunks       0x85 H.264 video frame
      0x86 reboot (01)                     0x90 screen-info query (01)
  * device replies: 5A A5 ?? <cmd> ...   0x81 version string at offset 5,
                                          0x8F firmware ACK flag in byte 4, 0x90 screen info
  * video: H.264, 4 Mbit/s, 25 fps, GOP 10, YUV420P, 320x320
"""
from __future__ import annotations

import logging
import queue
import struct
import threading
import time
from fractions import Fraction

import numpy as np

log = logging.getLogger(__name__)

VID, PID = 0x1F3A, 0x0007
EP_OUT, EP_IN = 0x01, 0x81
MAGIC = b"\x5a\xa5"

CMD_BRIGHTNESS = 0x80
CMD_HELLO = 0x81
CMD_FW_FIRST, CMD_FW_MIDDLE, CMD_FW_LAST = 0x82, 0x83, 0x84
CMD_FRAME = 0x85
CMD_REBOOT = 0x86
CMD_FW_ACK = 0x8F
CMD_SCREEN_INFO = 0x90

OLD_FIRMWARE_ID = b"Smart Monitor v0.0.1"
SCREEN_W = SCREEN_H = 320
FW_CHUNK = 0xFF8
FW_PLAIN_HEADER = 0x24


def packet(cmd: int, payload: bytes = b"") -> bytes:
    return MAGIC + bytes((cmd, 0)) + struct.pack("<I", len(payload)) + payload


def brightness_packet(percent: int) -> bytes:
    percent = max(0, min(100, int(percent)))
    return packet(CMD_BRIGHTNESS, bytes((percent * 255 // 100,)))


def firmware_packets(blob: bytes) -> list[bytes]:
    """Split a firmware .bin exactly as the original upgrade routine does."""
    data = blob[:FW_PLAIN_HEADER] + bytes(b ^ 0x48 ^ 0x4A for b in blob[FW_PLAIN_HEADER:])
    chunks = [data[i:i + FW_CHUNK] for i in range(0, len(data), FW_CHUNK)] or [b""]
    out = []
    for i, c in enumerate(chunks):
        if i == 0:
            out.append(MAGIC + bytes((CMD_FW_FIRST, 0)) + struct.pack("<I", FW_CHUNK) + c)
        elif i == len(chunks) - 1:
            out.append(MAGIC + bytes((CMD_FW_LAST, 0)) + struct.pack("<I", len(c)) + c)
        else:
            out.append(MAGIC + bytes((CMD_FW_MIDDLE, 0)) + struct.pack("<I", FW_CHUNK) + c)
    return out


# --------------------------------------------------------------------------- H.264 encoder
class H264Encoder:
    """Mirrors Transport::initEncoder: 4 Mbit/s, 25 fps, GOP 10, yuv420p."""

    CANDIDATES = ("libopenh264", "libx264", "h264_mf", "h264_nvenc", "h264_qsv", "h264_amf", "h264_v4l2m2m")

    def __init__(self, width: int = SCREEN_W, height: int = SCREEN_H):
        import av
        self.width, self.height = width, height
        self._pts = 0
        last_err = None
        for name in self.CANDIDATES:
            try:
                ctx = av.CodecContext.create(name, "w")
                ctx.width, ctx.height = width, height
                ctx.pix_fmt = "yuv420p"
                ctx.bit_rate = 4_000_000
                ctx.time_base = Fraction(1, 25)
                ctx.framerate = Fraction(25, 1)
                ctx.gop_size = 10
                ctx.max_b_frames = 0
                if name == "libx264":
                    ctx.options = {"preset": "veryfast", "tune": "zerolatency", "profile": "baseline"}
                ctx.open()
                self.ctx, self.codec_name = ctx, name
                log.info("H.264 encoder: %s", name)
                return
            except Exception as e:  # try next encoder
                last_err = e
        raise RuntimeError(f"no usable H.264 encoder: {last_err}")

    def encode(self, rgba: np.ndarray) -> bytes:
        import av
        frame = av.VideoFrame.from_ndarray(rgba, format="rgba").reformat(
            self.width, self.height, format="yuv420p")
        frame.pts = self._pts
        self._pts += 1
        return b"".join(bytes(p) for p in self.ctx.encode(frame))

    def close(self):
        try:
            for _ in self.ctx.encode(None):
                pass
        except Exception:
            pass


# --------------------------------------------------------------------------- device
class Device:
    """Thread-safe handle to one display. Use DeviceManager for hot-plug handling."""

    def __init__(self, dev):
        import usb.util
        self.dev = dev
        try:
            if dev.is_kernel_driver_active(0):
                dev.detach_kernel_driver(0)
        except Exception:
            pass
        try:
            dev.set_configuration(1)
        except Exception:
            pass  # already configured
        usb.util.claim_interface(dev, 0)
        self._wlock = threading.Lock()

    def write(self, data: bytes, timeout: int = 2000) -> int:
        with self._wlock:
            return self.dev.write(EP_OUT, data, timeout)

    def read(self, size: int = 4096, timeout: int = 100) -> bytes:
        return bytes(self.dev.read(EP_IN, size, timeout))

    def close(self):
        import usb.util
        try:
            usb.util.release_interface(self.dev, 0)
            usb.util.dispose_resources(self.dev)
        except Exception:
            pass


def find_device():
    import usb.core
    backend = None
    try:
        import libusb_package
        import usb.backend.libusb1
        backend = usb.backend.libusb1.get_backend(find_library=libusb_package.find_library)
    except Exception:
        pass
    return usb.core.find(idVendor=VID, idProduct=PID, backend=backend)


class DeviceManager(threading.Thread):
    """Owns connection, reply parsing, pending commands and the frame stream.

    Callbacks (called from worker threads; marshal to the GUI thread yourself):
      on_state(connected: bool), on_version(text: str), on_firmware(done: bool, ok: bool, msg: str)
    """

    def __init__(self, on_state=None, on_version=None, on_firmware=None):
        super().__init__(name="Transport", daemon=True)
        self.on_state = on_state or (lambda c: None)
        self.on_version = on_version or (lambda v: None)
        self.on_firmware = on_firmware or (lambda d, ok, m: None)
        self.device: Device | None = None
        self.version = ""
        self.old_firmware = False
        self.width, self.height = SCREEN_W, SCREEN_H
        self._frames: queue.Queue = queue.Queue(maxsize=2)  # original kept at most 2 frames queued
        self._commands: queue.Queue = queue.Queue()
        self._halt = threading.Event()
        self._fw_ack = threading.Event()
        self._firmware: bytes | None = None
        self._encoder: H264Encoder | None = None
        self._reader: threading.Thread | None = None

    # public API -------------------------------------------------------------
    @property
    def connected(self) -> bool:
        return self.device is not None

    def submit_frame(self, rgba: np.ndarray):
        """Queue a rendered RGBA frame (H x W x 4, uint8). Drops the oldest when full."""
        try:
            self._frames.put_nowait(rgba)
        except queue.Full:
            try:
                self._frames.get_nowait()
            except queue.Empty:
                pass
            self._frames.put_nowait(rgba)

    def set_brightness(self, percent: int):
        self._commands.put(brightness_packet(percent))

    def request_version(self):
        self._commands.put(packet(CMD_HELLO, b"\x01"))

    def reboot(self):
        self._commands.put(packet(CMD_REBOOT, b"\x01"))

    def upgrade_firmware(self, blob: bytes):
        self._firmware = blob

    def stop(self):
        self._halt.set()

    # worker -----------------------------------------------------------------
    def run(self):
        while not self._halt.is_set():
            if self.device is None:
                if not self._try_open():
                    self._halt.wait(1.0)
                    continue
            try:
                if self._firmware is not None:
                    blob, self._firmware = self._firmware, None
                    self._do_firmware(blob)
                    continue
                while not self._commands.empty():
                    self.device.write(self._commands.get_nowait())
                try:
                    rgba = self._frames.get(timeout=0.1)
                except queue.Empty:
                    continue
                if self._encoder is None:
                    self._encoder = H264Encoder(self.width, self.height)
                nal = self._encoder.encode(rgba)
                if nal:
                    self.device.write(packet(CMD_FRAME, nal))
            except Exception as e:
                log.warning("usb error: %s", e)
                self._close()
        self._close()

    def _try_open(self) -> bool:
        try:
            dev = find_device()
            if dev is None:
                return False
            self.device = Device(dev)
        except Exception as e:
            log.debug("Could not open device: %s", e)
            self.device = None
            return False
        log.info("Device open")
        self._reader = threading.Thread(target=self._read_loop, name="UsbReader", daemon=True)
        self._reader.start()
        self.device.write(packet(CMD_HELLO, b"\x01"))
        self._probing = True
        self.on_state(True)
        return True

    def _close(self):
        if self.device is not None:
            self.device.close()
            self.device = None
            self.on_state(False)
        if self._encoder is not None:
            self._encoder.close()
            self._encoder = None

    def _read_loop(self):
        dev = self.device
        while dev is not None and self.device is dev and not self._halt.is_set():
            try:
                data = dev.read(4096, timeout=200)
            except Exception as e:
                if "timeout" in str(e).lower() or getattr(e, "errno", None) in (110, 10060):
                    continue
                log.debug("read error: %s", e)
                time.sleep(0.2)
                continue
            self._handle_reply(data)

    def _handle_reply(self, data: bytes):
        if len(data) < 4 or data[:2] != MAGIC:
            return
        cmd = data[3]
        if cmd == CMD_HELLO:
            text = data[5:].split(b"\0", 1)[0]
            if getattr(self, "_probing", False):
                self._probing = False
                self.old_firmware = OLD_FIRMWARE_ID in data
                if not self.old_firmware:
                    self._commands.put(packet(CMD_SCREEN_INFO, b"\x01"))
            self.version = text.decode("utf-8", "replace").strip()
            self.on_version(self.version)
        elif cmd == CMD_FW_ACK and len(data) > 4 and data[4]:
            self._fw_ack.set()
        elif cmd == CMD_SCREEN_INFO:
            # Original hard-codes 320x320 here; the payload layout is unknown.
            self.width, self.height = SCREEN_W, SCREEN_H

    def _do_firmware(self, blob: bytes):
        pkts = firmware_packets(blob)
        self._fw_ack.set()  # first chunk is sent immediately
        for i, p in enumerate(pkts):
            if not self._fw_ack.wait(2.0):  # original: 200 polls x 10 ms
                self.on_firmware(True, False, "Upgrade fail.")
                return
            self._fw_ack.clear()
            self.device.write(p, timeout=5000)
            self.on_firmware(False, True, f"{i + 1}/{len(pkts)}")
        self.on_firmware(True, True, "Upgrade success.")
