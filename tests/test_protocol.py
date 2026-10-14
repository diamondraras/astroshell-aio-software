"""Protocol packet layouts, checked against the byte sequences recovered from the original binary."""
import struct

from smartmonitor.transport import (CMD_FRAME, CMD_HELLO, CMD_REBOOT, FW_CHUNK, brightness_packet,
                                    firmware_packets, packet)


def test_header_layout():
    p = packet(CMD_FRAME, b"\x00\x00\x00\x01abc")
    assert p[:4] == b"\x5a\xa5\x85\x00"
    assert struct.unpack("<I", p[4:8])[0] == 7
    assert p[8:] == b"\x00\x00\x00\x01abc"


def test_simple_commands():
    assert packet(CMD_HELLO, b"\x01") == bytes.fromhex("5aa5810001000000" "01")
    assert packet(CMD_REBOOT, b"\x01") == bytes.fromhex("5aa5860001000000" "01")


def test_brightness_scaling():
    # original: value = slider% * 255 / 100
    assert brightness_packet(100)[-1] == 255
    assert brightness_packet(50)[-1] == 127
    assert brightness_packet(0)[-1] == 0


def test_firmware_split_and_obfuscation():
    blob = bytes(range(256)) * 40  # 10240 bytes -> 3 chunks
    pkts = firmware_packets(blob)
    assert [p[2] for p in pkts] == [0x82, 0x83, 0x84]
    assert pkts[0][4:8] == bytes.fromhex("f80f0000")
    assert pkts[1][4:8] == bytes.fromhex("f80f0000")
    assert struct.unpack("<I", pkts[2][4:8])[0] == len(blob) - 2 * FW_CHUNK
    payload = b"".join(p[8:] for p in pkts)
    assert payload[:0x24] == blob[:0x24]                                   # header kept as-is
    assert payload[0x24:] == bytes(b ^ 0x02 for b in blob[0x24:])          # ^0x48 ^0x4a


def test_firmware_single_chunk_is_only_first_packet():
    pkts = firmware_packets(b"x" * 100)
    assert len(pkts) == 1 and pkts[0][2] == 0x82
