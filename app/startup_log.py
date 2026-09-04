"""启动性能日志：进程一启动就自动写盘，不需要任何用户操作。

写入 userdata/logs/（与 config.json 同目录，随打包版走）：
- 每次启动一个独立文件 startup_YYYYMMDD_HHMMSS.log（崩溃/卡死时留尸）
- 启动阶段时间戳：进程起点/各 import/QApplication/样式表/主窗口构建/首帧
- GUI 线程卡顿检测：QTimer(250ms) 心跳，回调间隔 > 1s 说明事件循环被
  阻塞，记录时长（卡在哪由间隔起点 + 前一条日志内容推断）
- 兜底捕获：sys.excepthook / Qt 消息输出重定向（字体缺字、平台插件、
  OpenGL 告警等本来打到 stderr，--windowed 打包版是直接丢掉的）
- 自动清理：只保留最近 20 份启动日志

设置页的"导出诊断日志"按钮只是把最近几份日志打包成 zip 放到播放器
根文件夹——日志本身一直在写，白屏/未响应发生时文件已经在盘上，
不需要事前点任何按钮。

设计约束：
- 全部 try/except 包住，日志系统绝不能让播放器崩
"""
from __future__ import annotations

import os
import sys
import threading
import time
import traceback
from datetime import datetime
from pathlib import Path

_T0 = time.perf_counter()
_PATH: Path | None = None
_KEEP = 20


def _resolve_path() -> Path | None:
    global _PATH
    if _PATH is not None:
        return _PATH
    try:
        from .runtime import USERDATA_DIR

        folder = USERDATA_DIR / "logs"
        folder.mkdir(parents=True, exist_ok=True)
        _PATH = folder / f"startup_{datetime.now():%Y%m%d_%H%M%S}.log"
        return _PATH
    except Exception:
        return None


def _fmt(tag: str, message: str = "") -> str:
    stamp = datetime.now().strftime("%H:%M:%S")
    return f"[{time.perf_counter() - _T0:8.3f}s] {stamp} {tag:16} {message}"


def _write(line: str) -> None:
    path = _resolve_path()
    if path is None:
        return
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8", errors="replace") as fp:
            fp.write(line.rstrip() + "\n")
    except Exception:
        pass  # 日志绝不能反向搞崩宿主


def stage(tag: str, message: str = "") -> None:
    """启动阶段打点。写入失败静默。"""
    _write(_fmt(tag, message))


def _redirect_qt_messages() -> None:
    """Qt 的告警（缺字形/平台插件/OpenGL）默认进 stderr，
    --windowed 打包版直接丢——接到日志文件里。"""
    try:
        from PySide6.QtCore import qInstallMessageHandler

        def handler(kind, context, message):
            try:
                from PySide6.QtCore import QtMsgType

                names = {QtMsgType.QtDebugMsg: "qt-debug",
                         QtMsgType.QtWarningMsg: "qt-warning",
                         QtMsgType.QtCriticalMsg: "qt-critical",
                         QtMsgType.QtFatalMsg: "qt-fatal",
                         QtMsgType.QtInfoMsg: "qt-info"}
                stage(names.get(kind, "qt"), str(message)[:300])
            except Exception:
                pass

        qInstallMessageHandler(handler)
    except Exception:
        pass


def _install_excepthook() -> None:
    prev = sys.excepthook

    def hook(tp, exc, tb):
        try:
            _write(_fmt("uncaught", f"{tp.__name__}: {exc}\n"
                       + "".join(traceback.format_tb(tb))[:2000]))
        except Exception:
            pass
        prev(tp, exc, tb)

    sys.excepthook = hook


def _install_thread_excepthook() -> None:
    """子线程的未捕获异常默认只打到 stderr（--windowed 打包版=黑洞）——
    接进日志。缩略图 worker / 扫描线程 / 预载线程的崩全靠这条留痕，
    sys.excepthook 只覆盖主线程。"""
    def hook(args):
        try:
            name = getattr(args.thread, "name", "?") if args.thread else "?"
            tb = "".join(traceback.format_exception(
                args.exc_type, args.exc_value, args.exc_traceback))[:2000]
            _write(_fmt("uncaught", f"[线程 {name}] "
                                    f"{getattr(args.exc_type, '__name__', '?')}: "
                                    f"{args.exc_value}\n{tb}"))
        except Exception:
            pass

    threading.excepthook = hook


def _install_stall_detector(app) -> None:
    """GUI 线程卡顿检测：250ms 心跳，回调间隔 > 1s 记一条。

    白屏+未响应 = 事件循环不转：这条记录直接给出阻塞时长和发生时刻，
    与启动阶段打点对照即可定位卡在哪个阶段。
    """
    from PySide6.QtCore import QTimer

    last = {"t": time.perf_counter()}
    timer = QTimer(app)
    timer.setInterval(250)

    def tick():
        now = time.perf_counter()
        gap = now - last["t"]
        if gap > 1.0:
            stage("gui-stall", f"GUI 线程阻塞 {gap:.1f}s")
        last["t"] = now

    timer.timeout.connect(tick)
    timer.start()


def _prune_old_logs() -> None:
    """只保留最近 _KEEP 份启动日志（每份 ≤1MB，20 份封顶约 20MB）。"""
    path = _resolve_path()
    if path is None:
        return
    try:
        logs = sorted(path.parent.glob("startup_*.log"))
        for old in logs[:-_KEEP]:
            try:
                old.unlink()
            except OSError:
                pass
    except Exception:
        pass


def begin() -> None:
    """进程启动时调用一次（main.py 最顶部）。"""
    _write("")
    _write("=" * 72)
    _write(_fmt("process", f"启动 pid={os.getpid()} "
                           f"argv={[a[:80] for a in sys.argv][:4]} "
                           f"frozen={bool(getattr(sys, 'frozen', False))}"))
    _install_excepthook()
    _install_thread_excepthook()
    _prune_old_logs()


def attach(app) -> None:
    """QApplication 建好后调用：接 Qt 告警 + 卡顿检测。"""
    _redirect_qt_messages()
    _install_stall_detector(app)
    stage("qt-attached", f"platform={app.platformName()} "
                         f"screens={len(app.screens())} "
                         f"dpr={app.devicePixelRatio()}")


def end(reason: str = "exit") -> None:
    stage("exit", reason)


def recent_logs(n: int = 5) -> list[Path]:
    """最近 n 份启动日志，新→旧（导出打包用）。"""
    path = _resolve_path()
    if path is None:
        return []
    try:
        return sorted(path.parent.glob("startup_*.log"),
                      key=lambda p: p.stat().st_mtime,
                      reverse=True)[:n]
    except Exception:
        return []
