"""Settings page (original: DispSettingPage)."""
from __future__ import annotations

import time
from pathlib import Path

from PySide6.QtCore import QCoreApplication, Signal
from PySide6.QtWidgets import (QCheckBox, QComboBox, QFileDialog, QFormLayout, QGroupBox, QHBoxLayout,
                               QLabel, QMessageBox, QPushButton, QVBoxLayout, QWidget)

from .config import LANGUAGES, set_autorun
from .sensors import SENSORS, format_value


def tr(s: str) -> str:
    return QCoreApplication.translate("DispSettingPage", s)


class SettingsPage(QWidget):
    language_changed = Signal(int)

    def __init__(self, cfg, devices, sensors, parent=None):
        super().__init__(parent)
        self.cfg, self.devices, self.sensors = cfg, devices, sensors
        lay = QVBoxLayout(self)

        gen = QGroupBox()
        form = QFormLayout(gen)
        self.language = QComboBox()
        self.language.addItems([tr(l) for l in LANGUAGES])
        self.language.setCurrentIndex(cfg.get("language/type"))
        self.language.currentIndexChanged.connect(self._on_language)
        form.addRow(tr("Language"), self.language)

        self.autorun = QCheckBox(tr("Auto Run"))
        self.autorun.setChecked(cfg.get("autoRun/state"))
        self.autorun.toggled.connect(self._on_autorun)
        form.addRow(self.autorun)

        self.temp_unit = QComboBox()
        self.temp_unit.addItems(["°C", "°F"])
        self.temp_unit.setCurrentIndex(cfg.get("temp/uint"))
        self.temp_unit.currentIndexChanged.connect(lambda i: cfg.set("temp/uint", i))
        form.addRow("Temp", self.temp_unit)

        self.network = QComboBox()
        self.network.addItem("Auto (internet interface)")
        self.network.addItems(sensors.network_interfaces())
        idx = cfg.get("network/type")
        self.network.setCurrentIndex(idx if idx < self.network.count() else 0)
        self.network.currentIndexChanged.connect(self._on_network)
        self._on_network(self.network.currentIndex())
        form.addRow(tr("Network Card"), self.network)

        self.disk = QComboBox()
        self.disk.addItems(sensors.disks())
        idx = cfg.get("disk/type")
        self.disk.setCurrentIndex(idx if idx < self.disk.count() else 0)
        self.disk.currentIndexChanged.connect(self._on_disk)
        self._on_disk(self.disk.currentIndex())
        form.addRow(tr("Hard Disk"), self.disk)
        lay.addWidget(gen)

        dev = QGroupBox()
        dform = QFormLayout(dev)
        row = QHBoxLayout()
        self.version = QLabel("-")
        btn_ver = QPushButton("⟳")
        btn_ver.setToolTip("connect")
        btn_ver.clicked.connect(devices.request_version)
        row.addWidget(self.version, 1)
        row.addWidget(btn_ver)
        dform.addRow(tr("Device Version"), row)

        btn_fw = QPushButton(tr("update"))
        btn_fw.clicked.connect(self._on_firmware)
        self.fw_status = QLabel("")
        r2 = QHBoxLayout()
        r2.addWidget(btn_fw)
        r2.addWidget(self.fw_status, 1)
        dform.addRow(tr("Firmware Upgrade"), r2)

        btn_reboot = QPushButton(tr("Reboot"))
        btn_reboot.clicked.connect(devices.reboot)
        dform.addRow(btn_reboot)

        btn_export = QPushButton(tr("Export"))
        btn_export.setToolTip("Export sensor readings to a text file")
        btn_export.clicked.connect(self._export_sensor_info)
        dform.addRow(btn_export)
        lay.addWidget(dev)
        lay.addStretch(1)

    def set_version(self, text: str):
        self.version.setText(text or "-")

    def set_firmware_status(self, done: bool, ok: bool, msg: str):
        self.fw_status.setText(tr(msg) if done else msg)
        if done:
            (QMessageBox.information if ok else QMessageBox.warning)(self, "", tr(msg))

    def _on_language(self, i: int):
        self.cfg.set("language/type", i)
        self.language_changed.emit(i)

    def _on_autorun(self, on: bool):
        self.cfg.set("autoRun/state", on)
        try:
            set_autorun(on)
        except Exception as e:
            QMessageBox.warning(self, "Auto Run", str(e))

    def _on_network(self, i: int):
        self.cfg.set("network/type", i)
        self.sensors.set_network(None if i <= 0 else self.network.itemText(i))

    def _on_disk(self, i: int):
        self.cfg.set("disk/type", i)
        self.sensors.set_disk(self.disk.itemText(i) or None)

    def _on_firmware(self):
        path, _ = QFileDialog.getOpenFileName(self, tr("Firmware Upgrade"), "", "*.bin")
        if not path:
            return
        if not self.devices.connected:
            QMessageBox.warning(self, "", tr("Upgrade fail."))
            return
        self.devices.upgrade_firmware(Path(path).read_bytes())

    def _export_sensor_info(self):
        default = f"SensorInfo_{time.strftime('%Y%m%d%H%M')}.txt"
        path, _ = QFileDialog.getSaveFileName(self, "Save", default, "info(*.txt)")
        if not path:
            return
        snap = self.sensors.snapshot()
        lines = []
        for s in SENSORS:
            val, unit = format_value(s.key, snap.get(s.key))
            lines.append(f"name:    {s.label}\nvalue:   {val} {unit}\n")
        Path(path).write_text("\n".join(lines), encoding="utf-8")
