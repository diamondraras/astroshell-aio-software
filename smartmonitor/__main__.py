"""Entry point: python -m smartmonitor [--minimized] [--debug]"""
from __future__ import annotations

import logging
import sys
from pathlib import Path

if __package__ in (None, ""):  # running as a script / frozen by PyInstaller
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    __package__ = "smartmonitor"

from PySide6.QtCore import QTranslator
from PySide6.QtNetwork import QLocalServer, QLocalSocket
from PySide6.QtWidgets import QApplication, QMessageBox

from smartmonitor.app import MainWindow
from smartmonitor.config import Config, resource_dir

INSTANCE_KEY = "RealTimeTransportApp"  # same single-instance key as the original
QM_FILES = ["zh_EN.qm", "zh_CH.qm", "zh_French.qm", "zh_Japanese.qm", "zh_Korean.qm", "zh_Portuguese.qm"]


def main() -> int:
    logging.basicConfig(level=logging.DEBUG if "--debug" in sys.argv else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    app = QApplication(sys.argv)
    app.setApplicationName("SmartMonitor")
    app.setQuitOnLastWindowClosed(False)

    sock = QLocalSocket()
    sock.connectToServer(INSTANCE_KEY)
    if sock.waitForConnected(300):
        # Already running: this app answers "ok" and brings its window to the front.
        # No answer means the original SmartMonitor28 A3 (same key) holds the display.
        sock.write(b"show")
        sock.flush()
        sock.waitForBytesWritten(300)
        if not (sock.waitForReadyRead(3000) and sock.readAll().data().startswith(b"ok")):
            QMessageBox.warning(None, "SmartMonitor",
                                "Another display app (probably SmartMonitor28 A3) is running.\n"
                                "Close it first: only one program can drive the display.")
        return 0
    QLocalServer.removeServer(INSTANCE_KEY)
    server = QLocalServer()
    server.listen(INSTANCE_KEY)

    cfg = Config()
    translator = QTranslator()

    def apply_language(index: int):
        app.removeTranslator(translator)
        qm = resource_dir() / "translations" / QM_FILES[index if 0 <= index < len(QM_FILES) else 0]
        if translator.load(str(qm)):
            app.installTranslator(translator)

    apply_language(cfg.get("language/type"))
    win = MainWindow(cfg, apply_language)

    def on_second_instance():
        conn = server.nextPendingConnection()
        conn.write(b"ok")
        conn.flush()
        win.show_normal()
    server.newConnection.connect(on_second_instance)
    if "--minimized" not in sys.argv:
        win.show_normal()  # builds the pages; a minimized (autostart) launch builds them on first open
    rc = app.exec()
    win.shutdown()
    return rc


if __name__ == "__main__":
    sys.exit(main())
