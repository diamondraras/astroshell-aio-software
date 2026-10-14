"""Hardware sensor collection (cross-platform replacement for the original HWiNFO64.dll usage).

The original app loads a bundled, proprietary HWiNFO64.dll (Windows only) and picks readings by
HWiNFO label: sensor "CPU [#0]:" -> "Core 0 Clock"/"CPU Die"/"Total CPU Usage", sensor "GPU [#0]:"
-> "GPU Clock"/"GPU Temperature"/"GPU Core Load"/"GPU D3D Usage", sensor "Network:".
(strings observed in the original executable).

This module uses, in order of preference:
  * psutil (CPU usage/frequency, memory, disk, network; CPU temperature on Linux)
  * Linux sysfs hwmon / amdgpu (GPU temperature, load, clock)
  * NVIDIA NVML via nvidia-ml-py (Windows + Linux)
  * HWiNFO shared memory (Windows, when HWiNFO is running with "Shared Memory Support" enabled),
    using the same labels as the original
"""
from __future__ import annotations

import glob
import mmap
import os
import struct
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass

import psutil

try:
    import pynvml  # provided by the nvidia-ml-py package
except Exception:  # pragma: no cover - optional
    pynvml = None


@dataclass(frozen=True)
class SensorDef:
    key: str
    label: str      # default display name (as in the original's sensor list)
    unit: str


# Keys used throughout the app; labels mirror the original's UI strings.
SENSORS: list[SensorDef] = [
    SensorDef("cpu_temp", "CPU Temp", "°C"),
    SensorDef("cpu_freq", "CPU Freq", "MHz"),
    SensorDef("cpu_usage", "CPU Usage", "%"),
    SensorDef("gpu_temp", "GPU Temp", "°C"),
    SensorDef("gpu_freq", "GPU Freq", "MHz"),
    SensorDef("gpu_usage", "GPU Usage", "%"),
    SensorDef("mem_usage", "RAM Usage", "%"),
    SensorDef("disk_usage", "Disk Usage", "%"),
    SensorDef("net_down", "Download", "KB/s"),
    SensorDef("net_up", "Upload", "KB/s"),
]
SENSOR_BY_KEY = {s.key: s for s in SENSORS}


# --------------------------------------------------------------------------- HWiNFO shared memory
class _HwinfoShm:
    """Reader for HWiNFO's documented shared-memory interface (Windows only)."""

    NAME = "Global\\HWiNFO_SENS_SM2"
    HDR = struct.Struct("<IIIqIIIIII")  # sig, ver, rev, poll_time, off_s, size_s, n_s, off_r, size_r, n_r

    def read(self) -> dict[tuple[str, str], float]:
        if sys.platform != "win32":
            return {}
        try:
            m = mmap.mmap(-1, self.HDR.size, tagname=self.NAME, access=mmap.ACCESS_READ)
            hdr = self.HDR.unpack(m[: self.HDR.size])
            m.close()
            _, _, _, _, off_s, size_s, n_s, off_r, size_r, n_r = hdr
            total = off_r + size_r * n_r
            m = mmap.mmap(-1, total, tagname=self.NAME, access=mmap.ACCESS_READ)
            buf = m[:total]
            m.close()
        except Exception:
            return {}

        def cstr(b: bytes) -> str:
            return b.split(b"\0", 1)[0].decode("latin-1", "replace")

        sensors = [cstr(buf[off_s + i * size_s + 8: off_s + i * size_s + 136]) for i in range(n_s)]
        out: dict[tuple[str, str], float] = {}
        for i in range(n_r):
            base = off_r + i * size_r
            _typ, sidx, _rid = struct.unpack_from("<III", buf, base)
            label = cstr(buf[base + 12: base + 140])
            value = struct.unpack_from("<d", buf, base + 12 + 128 + 128 + 16)[0]
            sname = sensors[sidx] if sidx < len(sensors) else ""
            out[(sname, label)] = value
        return out


def _hw_pick(readings: dict[tuple[str, str], float], sensor_prefix: str, labels: list[str]):
    for label in labels:
        for (s, l), v in readings.items():
            if s.startswith(sensor_prefix) and l.startswith(label):
                return v
    return None


# --------------------------------------------------------------------------- GPU backends
class _Gpu:
    def __init__(self):
        self._nv = None
        if pynvml is not None:
            try:
                pynvml.nvmlInit()
                if pynvml.nvmlDeviceGetCount() > 0:
                    self._nv = pynvml.nvmlDeviceGetHandleByIndex(0)
            except Exception:
                self._nv = None
        self._amd = None
        if sys.platform.startswith("linux"):
            for card in sorted(glob.glob("/sys/class/drm/card[0-9]/device")):
                if os.path.exists(os.path.join(card, "gpu_busy_percent")):
                    self._amd = card
                    break

    @property
    def available(self) -> bool:
        return self._nv is not None or self._amd is not None

    def read(self) -> dict[str, float]:
        out: dict[str, float] = {}
        if self._nv is not None:
            try:
                out["gpu_temp"] = float(pynvml.nvmlDeviceGetTemperature(self._nv, pynvml.NVML_TEMPERATURE_GPU))
                out["gpu_usage"] = float(pynvml.nvmlDeviceGetUtilizationRates(self._nv).gpu)
                out["gpu_freq"] = float(pynvml.nvmlDeviceGetClockInfo(self._nv, pynvml.NVML_CLOCK_GRAPHICS))
            except Exception:
                pass
        elif self._amd is not None:
            out.update(_read_amd_sysfs(self._amd))
        return out


def _read_int(path: str):
    try:
        with open(path) as f:
            return int(f.read().strip())
    except Exception:
        return None


def _read_amd_sysfs(card: str) -> dict[str, float]:
    out: dict[str, float] = {}
    busy = _read_int(os.path.join(card, "gpu_busy_percent"))
    if busy is not None:
        out["gpu_usage"] = float(busy)
    for hw in glob.glob(os.path.join(card, "hwmon/hwmon*")):
        t = _read_int(os.path.join(hw, "temp1_input"))
        if t is not None:
            out["gpu_temp"] = t / 1000.0
        f = _read_int(os.path.join(hw, "freq1_input"))
        if f is not None:
            out["gpu_freq"] = f / 1e6
    if "gpu_freq" not in out:
        try:
            with open(os.path.join(card, "pp_dpm_sclk")) as fh:
                for line in fh:
                    if line.strip().endswith("*"):
                        out["gpu_freq"] = float(line.split()[1].lower().rstrip("mhz"))
        except Exception:
            pass
    return out


# --------------------------------------------------------------------------- LibreHardwareMonitor
class _LhmHttp:
    """Optional CPU temperature source: LibreHardwareMonitor's "Remote Web Server" (localhost only).

    Used on Windows when HWiNFO shared memory is unavailable. The JSON tree is walked generically:
    a node whose Value ends with "°C" below a CPU node (ImageURL/SensorId mentioning "cpu").
    """

    URL = "http://127.0.0.1:8085/data.json"
    PREFERRED = ("CPU Package", "Core (Tctl/Tdie)", "CPU (Tctl/Tdie)", "Core Average", "CPU Core")

    def __init__(self):
        self._retry_at = 0.0

    def cpu_temp(self):
        if sys.platform != "win32" or time.monotonic() < self._retry_at:
            return None
        import json
        import urllib.request
        try:
            with urllib.request.urlopen(self.URL, timeout=0.3) as r:
                tree = json.load(r)
        except Exception:
            self._retry_at = time.monotonic() + 30.0  # not running: check again later
            return None
        return self.pick_cpu_temp(tree)

    @classmethod
    def pick_cpu_temp(cls, tree):
        found: list[tuple[str, float]] = []

        def walk(node, in_cpu):
            tag = f"{node.get('ImageURL', '')} {node.get('SensorId', '')}".lower()
            in_cpu = in_cpu or "cpu" in tag
            val = str(node.get("Value", ""))
            if in_cpu and val.endswith("°C"):
                try:
                    found.append((str(node.get("Text", "")), float(val[:-2].strip().replace(",", "."))))
                except ValueError:
                    pass
            for c in node.get("Children", []) or []:
                walk(c, in_cpu)

        walk(tree, False)
        for name in cls.PREFERRED:
            for text, v in found:
                if text.startswith(name):
                    return v
        return found[0][1] if found else None


# --------------------------------------------------------------------------- network helpers
_VIRTUAL_NIC = ("loopback", "teredo", "isatap", "vmware", "virtualbox", "vethernet", "docker", "bluetooth")


def internet_interface() -> str | None:
    """Name of the interface holding the default route, or None when there is no route.

    A UDP "connect" only asks the OS for a route; no packet is sent.
    """
    import socket
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("1.1.1.1", 80))
            local_ip = s.getsockname()[0]
    except OSError:
        return None
    for nic, addrs in psutil.net_if_addrs().items():
        if any(a.address == local_ip for a in addrs):
            return nic
    return None


class _Connectivity(threading.Thread):
    """Checks real internet reachability with a TCP handshake (no payload sent)."""

    TARGETS = (("1.1.1.1", 443), ("8.8.8.8", 53), ("9.9.9.9", 443))

    def __init__(self, stop: threading.Event, period: float = 10.0):
        super().__init__(name="Connectivity", daemon=True)
        self._stop_evt, self.period = stop, period
        self.online: bool | None = None  # None = not checked yet

    def check(self) -> bool:
        import socket
        if internet_interface() is None:
            return False
        for host, port in self.TARGETS:
            try:
                socket.create_connection((host, port), timeout=2.0).close()
                return True
            except OSError:
                continue
        return False

    def run(self):
        while not self._stop_evt.is_set():
            self.online = self.check()
            self._stop_evt.wait(self.period if self.online else 3.0)


# --------------------------------------------------------------------------- manager
HISTORY_SECONDS = 60


class SensorManager:
    """Polls all sensors on a background thread (the original used DispSensorManager::run).

    Besides the instantaneous values it keeps one sample per second for the last minute
    (history()), and exposes uptime, internet state and data used since boot.
    """

    def __init__(self, interval: float = 1.0):
        self.interval = interval
        self._values: dict[str, float] = {}
        self._history: dict[str, deque] = {}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._hwinfo = _HwinfoShm()
        self._hwinfo_retry = 0.0
        self._lhm = _LhmHttp()
        self._gpu: _Gpu | None = None   # created on the sensor thread (NVML init is slow)
        self._net_prev = None
        self._nic: str | None = None    # None = automatic (interface holding the default route)
        self._auto_nic: str | None = None
        self._auto_nic_at = 0.0
        self._disk: str | None = None   # mount point; None = system drive
        self._boot = psutil.boot_time()
        self._conn = _Connectivity(self._stop)
        self._thread = threading.Thread(target=self._run, name="SensorManager", daemon=True)
        self.version = 0                # incremented after every poll

    # configuration ----------------------------------------------------------
    @staticmethod
    def network_interfaces() -> list[str]:
        return sorted(psutil.net_io_counters(pernic=True).keys())

    @staticmethod
    def disks() -> list[str]:
        return [p.mountpoint for p in psutil.disk_partitions(all=False)]

    def set_network(self, nic: str | None):
        self._nic = nic
        self._net_prev = None

    def set_disk(self, mount: str | None):
        self._disk = mount

    # lifecycle ----------------------------------------------------------------
    def start(self):
        self._thread.start()
        self._conn.start()

    def stop(self):
        self._stop.set()

    def snapshot(self) -> dict[str, float]:
        with self._lock:
            return dict(self._values)

    def value(self, key: str):
        with self._lock:
            return self._values.get(key)

    def history(self, key: str) -> list:
        """Last HISTORY_SECONDS samples, oldest first; None where the sensor had no value."""
        with self._lock:
            h = self._history.get(key)
            return list(h) if h else []

    @property
    def online(self) -> bool | None:
        return self._conn.online

    @property
    def interface(self) -> str | None:
        return self._nic or self._auto_nic

    def uptime(self) -> float:
        return max(0.0, time.time() - self._boot)

    def _run(self):
        psutil.cpu_percent(None)
        if self._gpu is None:
            self._gpu = _Gpu()
        while not self._stop.is_set():
            try:
                self._store(self._poll())
            except Exception:
                pass
            self._stop.wait(self.interval)

    def _store(self, vals: dict[str, float]):
        with self._lock:
            self._values = vals
            for k in set(vals) | set(self._history):
                self._history.setdefault(k, deque(maxlen=HISTORY_SECONDS)).append(vals.get(k))
            self.version += 1

    def _active_nic(self) -> str | None:
        if self._nic:
            return self._nic
        now = time.monotonic()
        if now >= self._auto_nic_at:
            nic = internet_interface()
            if nic != self._auto_nic:
                self._net_prev = None
            self._auto_nic, self._auto_nic_at = nic, now + 5.0
        return self._auto_nic

    def _poll(self) -> dict[str, float]:
        v: dict[str, float] = {}
        v["cpu_usage"] = psutil.cpu_percent(None)
        f = psutil.cpu_freq()
        if f:
            v["cpu_freq"] = f.current
        v["mem_usage"] = psutil.virtual_memory().percent
        try:
            mount = self._disk or ("C:\\" if sys.platform == "win32" else "/")
            v["disk_usage"] = psutil.disk_usage(mount).percent
        except Exception:
            pass

        # CPU temperature
        temps = psutil.sensors_temperatures() if hasattr(psutil, "sensors_temperatures") else {}
        for chip in ("coretemp", "k10temp", "zenpower", "cpu_thermal", "acpitz"):
            entries = temps.get(chip) if temps else None
            if entries:
                pkg = [e for e in entries if e.label.startswith(("Package", "Tctl", "Tdie"))]
                v["cpu_temp"] = (pkg or entries)[0].current
                break

        if self._gpu is not None:
            v.update(self._gpu.read())

        # HWiNFO (Windows): same labels as the original app; retried every 10 s when absent
        now = time.monotonic()
        hw = {}
        if sys.platform == "win32" and now >= self._hwinfo_retry:
            hw = self._hwinfo.read()
            if not hw:
                self._hwinfo_retry = now + 10.0
        if hw:
            picks = {
                "cpu_temp": ("CPU [#0]", ["CPU Package", "CPU Die", "CPU (Tctl/Tdie)"]),
                "cpu_freq": ("CPU [#0]", ["Core 0 Clock"]),
                "cpu_usage": ("CPU [#0]", ["Total CPU Usage"]),
                "gpu_temp": ("GPU [#0]", ["GPU Temperature"]),
                "gpu_freq": ("GPU [#0]", ["GPU Clock"]),
                "gpu_usage": ("GPU [#0]", ["GPU Core Load", "GPU D3D Usage"]),
            }
            for key, (prefix, labels) in picks.items():
                if key not in v or key in ("cpu_temp",):
                    r = _hw_pick(hw, prefix, labels)
                    if r is not None:
                        v[key] = r
        if "cpu_temp" not in v:
            t = self._lhm.cpu_temp()
            if t is not None:
                v["cpu_temp"] = t

        # Network: throughput (KB/s) and bytes counted since the interface came up (normally boot)
        counters = psutil.net_io_counters(pernic=True)
        nic = self._active_nic()
        if nic and nic in counters:
            c = counters[nic]
            sent, recv = c.bytes_sent, c.bytes_recv
        elif nic is None and self._nic is None:
            sent = recv = None  # no default route: offline
        else:
            real = [c for n, c in counters.items() if n != "lo" and not n.lower().startswith(_VIRTUAL_NIC)]
            sent = sum(c.bytes_sent for c in real)
            recv = sum(c.bytes_recv for c in real)
        if sent is not None:
            v["net_total_up"], v["net_total_down"] = float(sent), float(recv)
            if self._net_prev:
                t0, s0, r0 = self._net_prev
                dt = max(now - t0, 1e-3)
                v["net_up"] = max(0.0, (sent - s0) / dt / 1024.0)
                v["net_down"] = max(0.0, (recv - r0) / dt / 1024.0)
            self._net_prev = (now, sent, recv)
        else:
            v["net_up"] = v["net_down"] = 0.0
            self._net_prev = None
        return v


def format_value(key: str, value, fahrenheit: bool = False) -> tuple[str, str]:
    """Return (text, unit) for display."""
    sd = SENSOR_BY_KEY.get(key)
    unit = sd.unit if sd else ""
    if value is None:
        return "--", unit
    if unit == "°C" and fahrenheit:
        return f"{value * 9 / 5 + 32:.0f}", "°F"
    if unit == "KB/s" and value >= 1024:
        return f"{value / 1024:.1f}", "MB/s"
    return f"{value:.0f}", unit
