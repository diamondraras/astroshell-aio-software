"""Background media sources (static images, GIF, video) — replacement for DispVideoDecoder.

The original accepts: *.png *.jpg *.bmp *.gif *.webp *.mp4 *.h264 *.mkv
"""
from __future__ import annotations

import logging
import threading
import time
from pathlib import Path

import av  # imported eagerly: first import is slow and would delay the first frame
from PySide6.QtCore import Qt
from PySide6.QtGui import QImage

log = logging.getLogger(__name__)

IMAGE_EXT = {".png", ".jpg", ".jpeg", ".bmp", ".webp"}
ANIM_EXT = {".gif", ".mp4", ".h264", ".mkv", ".mov", ".avi", ".webm"}
FILE_FILTER = "Image/Video Files (*.png *.jpg *.bmp *.gif *.webp *.mp4 *.h264 *.mkv)"


class MediaSource:
    """Returns the current QImage for a file; animations loop on a background decoder thread."""

    def __init__(self, path: str | Path, size: tuple[int, int] | None = None):
        self.path = Path(path)
        self.size = size
        self._image: QImage | None = None
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = None
        ext = self.path.suffix.lower()
        if not self.path.exists():
            log.warning("file %s not found", self.path)
        elif ext in ANIM_EXT:
            self._thread = threading.Thread(target=self._decode_loop, daemon=True, name="MediaDecoder")
            self._thread.start()
        else:
            img = QImage(str(self.path))
            self._image = self._scaled(img) if not img.isNull() else None

    def _scaled(self, img: QImage) -> QImage:
        img = img.convertToFormat(QImage.Format_ARGB32_Premultiplied)
        if self.size and (img.width(), img.height()) != self.size:
            img = img.scaled(self.size[0], self.size[1], Qt.IgnoreAspectRatio, Qt.SmoothTransformation)
        return img

    def current(self) -> QImage | None:
        with self._lock:
            return self._image

    def close(self):
        self._stop.set()

    def _decode_loop(self):
        while not self._stop.is_set():
            try:
                with av.open(str(self.path)) as container:
                    stream = container.streams.video[0]
                    stream.thread_type = "AUTO"
                    rate = stream.average_rate or stream.guessed_rate or 25
                    default_dt = 1.0 / float(rate)
                    t_next = time.monotonic()
                    last_pts = None
                    for frame in container.decode(stream):
                        if self._stop.is_set():
                            return
                        w, h = self.size or (frame.width, frame.height)
                        arr = frame.to_ndarray(width=w, height=h, format="rgba")
                        img = QImage(arr.data, w, h, 4 * w, QImage.Format_RGBA8888).copy()
                        with self._lock:
                            self._image = img
                        dt = default_dt
                        if frame.pts is not None and frame.time_base and last_pts is not None:
                            d = float((frame.pts - last_pts) * frame.time_base)
                            if 0 < d < 1:
                                dt = d
                        last_pts = frame.pts
                        t_next += dt
                        delay = t_next - time.monotonic()
                        if delay > 0:
                            time.sleep(delay)
                        else:
                            t_next = time.monotonic()
            except Exception as e:
                log.warning("Can't decode %s: %s", self.path, e)
                self._stop.wait(2.0)
