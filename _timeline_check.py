"""源码启动一次带完整打点，看启动时间线（顺带验证打点齐全）。"""
from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
os.environ["QT_QPA_PLATFORM"] = "offscreen"
os.environ["PLAYER_AUTOMATION"] = "1"

from app.runtime import USERDATA_DIR, init_gl_format, init_libmpv

for f in (USERDATA_DIR / "logs").glob("startup_*.log"):
    f.unlink()

init_libmpv()
init_gl_format()

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication

from app import icons, theme
from app.main_window import MainWindow

app = QApplication([])
app.setStyleSheet(theme.build_stylesheet(icons.detect_family()))

import app.startup_log as slog

slog.begin()
slog.attach(app)
win = MainWindow()
win.show()


def quit_soon():
    app.processEvents()
    slog.end("验证退出")
    app.quit()


QTimer.singleShot(4000, quit_soon)
app.exec()

logs = sorted((USERDATA_DIR / "logs").glob("startup_*.log"))
print(logs[-1].read_text(encoding="utf-8"))
sys.stdout.flush()
os._exit(0)
