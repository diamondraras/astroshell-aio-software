"""Device reply handling (fake USB device) and theme round-trips over the stock themes."""
import os
from pathlib import Path

import numpy as np
import pytest

from smartmonitor import theme_format as tf
from smartmonitor.transport import CMD_FRAME, CMD_SCREEN_INFO, DeviceManager

ROOT = Path(__file__).resolve().parent.parent
THEMES = sorted((ROOT / "themes").glob("theme_*/*.theme"))


class FakeDevice:
    def __init__(self):
        self.written = []

    def write(self, data, timeout=0):
        self.written.append(bytes(data))
        return len(data)

    def close(self):
        pass


def _reply(cmd, payload=b""):
    return b"\x5a\xa5\x00" + bytes((cmd,)) + b"\x00" + payload


def test_new_firmware_hello_triggers_screen_info_query():
    dm = DeviceManager()
    dm._probing = True
    versions = []
    dm.on_version = versions.append
    dm._handle_reply(_reply(0x81, b"SM28 v2.1\0"))
    assert versions == ["SM28 v2.1"] and not dm.old_firmware
    queued = dm._commands.get_nowait()
    assert queued[:3] == b"\x5a\xa5" + bytes((CMD_SCREEN_INFO,))


def test_old_firmware_hello_skips_screen_info():
    dm = DeviceManager()
    dm._probing = True
    dm._handle_reply(_reply(0x81, b"Smart Monitor v0.0.1\0"))
    assert dm.old_firmware and dm._commands.empty()


def test_firmware_ack_sets_flag():
    dm = DeviceManager()
    dm._handle_reply(_reply(0x8F)[:4] + b"\x01")
    assert dm._fw_ack.is_set()


def test_frame_is_encoded_and_sent():
    dm = DeviceManager()
    dm.device = FakeDevice()
    frame = np.zeros((320, 320, 4), np.uint8)
    dm.submit_frame(frame)
    # one iteration of the run loop body
    from smartmonitor.transport import H264Encoder, packet
    dm._encoder = H264Encoder()
    nal = dm._encoder.encode(dm._frames.get_nowait())
    dm.device.write(packet(CMD_FRAME, nal))
    pkt = dm.device.written[0]
    assert pkt[2] == CMD_FRAME and pkt[8:12] == b"\x00\x00\x00\x01"


@pytest.mark.skipif(not THEMES, reason="stock themes not present")
@pytest.mark.parametrize("path", THEMES, ids=[p.parent.name for p in THEMES])
def test_stock_theme_roundtrip(path):
    data = path.read_bytes()
    parsed = tf.parse(data)
    assert not any(isinstance(r, tf.RawRecord) for r in parsed["records"])
    assert tf.serialize(parsed) == data


@pytest.mark.skipif(not THEMES, reason="stock themes not present")
def test_scene_save_is_lossless(tmp_path):
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    from smartmonitor.scene import ThemeScene
    src = THEMES[0].parent
    scene = ThemeScene()
    scene.load(src)
    out = tmp_path / src.name
    scene.save(out)
    saved = tf.parse_file(out / THEMES[0].name)
    orig = tf.parse_file(THEMES[0])
    # layout, sensors and file references survive a load/save cycle
    key = lambda r: (type(r).__name__, getattr(r, "name", None), getattr(r, "pos", getattr(r, "rect", None)),
                     getattr(r, "sensor", None), getattr(r, "file", None))
    assert [key(r) for r in saved["records"]] == [key(r) for r in orig["records"]]
    scene.close()
    assert app is not None
