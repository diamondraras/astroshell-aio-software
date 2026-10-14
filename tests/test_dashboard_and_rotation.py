"""Dashboard, per-widget rotation, frame caching and helpers added on top of the original feature set."""
import os
import shutil
import sys
from pathlib import Path

import numpy as np
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
if sys.platform == "win32":
    os.environ.setdefault("QT_QPA_FONTDIR", "C:/Windows/Fonts")

from PySide6.QtGui import QColor, QImage, QTransform  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
app = QApplication.instance() or QApplication([])

from smartmonitor import theme_format as tf  # noqa: E402
from smartmonitor.app import qimage_to_bgra  # noqa: E402
from smartmonitor.dashboard import DashboardRenderer, fmt_bytes, fmt_rate, fmt_uptime, nice_ceiling  # noqa: E402
from smartmonitor.renderer import ThemeRenderer  # noqa: E402
from smartmonitor.scene import EXTRA_FILE, ThemeScene  # noqa: E402
from smartmonitor.sensors import _LhmHttp  # noqa: E402


class Cfg:
    def __init__(self, **kw):
        self.d = {"temp/uint": 0, "dashboard/hour24": True, "theme/name": "T00", **kw}

    def get(self, k):
        return self.d.get(k)

    def set(self, k, v):
        self.d[k] = v


class Sensors:
    def __init__(self, online=True):
        self.version, self.online = 1, online

    def snapshot(self):
        return {"cpu_usage": 42.0, "cpu_temp": 61.0, "gpu_usage": 7.0, "gpu_temp": 40.0,
                "net_down": 2048.0, "net_up": 12.0, "net_total_down": 2e9, "net_total_up": 1e8}

    def history(self, key):
        return [float(i % 50) for i in range(60)]

    def uptime(self):
        return 3 * 3600 + 5 * 60


def test_formatters():
    assert fmt_rate(512) == ("512", "KB/s")
    assert fmt_rate(2048) == ("2.00", "MB/s")
    assert fmt_rate(None) == ("--", "KB/s")
    assert fmt_bytes(2e9 + 1e8) == "1.96 GB"
    assert fmt_bytes(512) == "512 B"
    assert fmt_uptime(52 * 3600 + 13 * 60 + 59) == "52 h 13 min"
    assert [nice_ceiling(v) for v in (0.3, 3, 7, 101, 1000)] == [0.5, 5, 10, 200, 1000]


@pytest.mark.parametrize("online", [True, False, None])
def test_dashboard_renders_and_caches(online):
    d = DashboardRenderer(Sensors(online), Cfg())
    img = d.render()
    assert (img.width(), img.height()) == (320, 320)
    first = d.frame_id
    assert d.render() is img and d.frame_id == first  # same second, same data: no redraw
    d.sensors.version += 1
    d.render()
    assert d.frame_id == first + 1
    # the dashboard is not a blank frame
    colors = {img.pixel(x, y) for x in range(0, 320, 7) for y in range(0, 320, 7)}
    assert len(colors) > 20


def test_rotation_is_saved_beside_theme_and_theme_stays_identical(tmp_path):
    src = ROOT / "themes" / "theme_T00"
    work = tmp_path / "theme_T00"
    shutil.copytree(src, work)
    original = (work / "T00.theme").read_bytes()
    scene = ThemeScene((320, 320))
    scene.load(work)
    target = scene.widgets[-1]
    target.set_angle(370)  # normalised to 10
    assert target.angle == pytest.approx(10)
    scene.save()
    assert (work / "T00.theme").read_bytes() == original  # .theme untouched: original app compatible
    assert (work / EXTRA_FILE).exists()

    again = ThemeScene((320, 320))
    again.load(work)
    w = next(x for x in again.widgets if x.objectName() == target.objectName())
    assert w.angle == pytest.approx(10) and w.rotation() == pytest.approx(10)

    w.set_angle(0)
    again.save()
    assert not (work / EXTRA_FILE).exists()


def test_number_widget_children_rotate_with_value(tmp_path):
    scene = ThemeScene((320, 320))
    scene.clear_theme()
    font = tf.QFont(family="Arial", point_size=20.0)
    white = tf.QColor.from_rgba8(255, 255, 255)
    rec = tf.NumberWidget(name="data_1", pos=(100.0, 100.0), color=white, font=font, text="00")
    rec.sensor = 3
    rec.unit = tf.LabelItem(name="data_1_unit", pos=(140.0, 106.0), color=white, font=font, text="%")
    item = scene.add_record(rec)
    item.set_angle(90)
    assert item.unit_item.rotation() == 90
    pivot = item.pos() + item.boundingRect().center()
    # the unit's pivot is the value's centre, so it orbits around the value
    assert item.unit_item.pos() + item.unit_item.transformOriginPoint() == pivot


def test_theme_renderer_skips_identical_frames(tmp_path, monkeypatch):
    shutil.copytree(ROOT / "themes" / "theme_T00", tmp_path / "theme_T00")
    monkeypatch.setattr("smartmonitor.renderer.themes_dir", lambda: tmp_path)
    r = ThemeRenderer(Sensors(), Cfg())
    assert r.load("T00")
    a = r.render()
    fid = r.frame_id
    if not r.animated:
        assert r.render() is a and r.frame_id == fid
    r.invalidate()
    r.render()
    assert r.frame_id == fid + 1


@pytest.mark.parametrize("angle", [0, 90, 180, 270])
def test_qimage_to_bgra_matches_qt_rotation(angle):
    img = QImage(4, 2, QImage.Format_ARGB32_Premultiplied)
    for x in range(4):
        for y in range(2):
            img.setPixelColor(x, y, QColor(x * 60, y * 120, 7, 255))
    got = qimage_to_bgra(img, angle)
    ref = img.transformed(QTransform().rotate(angle)) if angle else img
    want = qimage_to_bgra(ref, 0)
    assert got.shape == want.shape and np.array_equal(got, want)
    assert got.flags["C_CONTIGUOUS"]


def test_lhm_cpu_temperature_pick():
    tree = {"Text": "Sensor", "Children": [{"Text": "PC", "Children": [
        {"Text": "Intel Core i7", "ImageURL": "images_icon/cpu.png", "Children": [
            {"Text": "Temperatures", "Children": [
                {"Text": "CPU Core #1", "Value": "48,0 °C"},
                {"Text": "CPU Package", "Value": "55.0 °C"}]}]},
        {"Text": "NVIDIA", "ImageURL": "images_icon/nvidia.png", "Children": [
            {"Text": "GPU Core", "Value": "40.0 °C"}]}]}]}
    assert _LhmHttp.pick_cpu_temp(tree) == 55.0
    assert _LhmHttp.pick_cpu_temp({"Text": "x", "Children": []}) is None
