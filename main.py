"""Entry point. Bootstraps libmpv before anything else touches the mpv module."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from app import startup_log  # noqa: E402
from app.runtime import init_gl_format, init_libmpv  # noqa: E402

startup_log.begin()

_qt_translator = None  # kept alive for the lifetime of the process


def main() -> int:
    from PySide6.QtWidgets import QApplication, QMessageBox

    from PySide6.QtCore import QLibraryInfo, QLocale, QTranslator

    startup_log.stage("imports", "Qt widgets 导入完成")

    from app import icons, theme

    QApplication.setApplicationName("媒体播放器")
    QApplication.setOrganizationName("LocalMediaPlayer")
    app = QApplication(sys.argv)
    startup_log.attach(app)
    startup_log.stage("qapp", "QApplication 构建完成")

    # 单实例（Windows 命名互斥体，见 app/single_instance.py）：已有实例
    # 在跑时，第二次启动无论带不带文件都转交给它后退出——带文件 = 转发
    # 路径并播放；裸双击 exe = 把已有窗口唤到前台。此前只查带参数的启动，
    # 裸双击直接漏过（双开的根因）。互斥体由内核在进程死亡时自动回收：
    # 没有残留清理，也没有两进程同时自认第一的竞态。
    # 自动化模式跳过：测试/时间线脚本要能在播放器开着时启动。
    from app.runtime import automation_mode
    from app.single_instance import (
        InstanceServer, acquire_single_instance, forward_to_running)

    server = None
    if automation_mode():
        startup_log.stage("server", "自动化模式：跳过单实例（不探测不监听）")
    elif acquire_single_instance():
        # 尽早占住单实例管道：下面 libmpv/样式表/首启语言框是好几秒，
        # 这期间再来的启动必须已经能找到我们。互斥体在手，listen 失败
        # 只剩崩溃残留一种解释——清掉重听是安全的。
        server = InstanceServer()
        server.listen()
        startup_log.stage("server", "单实例服务就绪")
    else:
        # 已有实例持锁：转交（裸启动=唤醒）后退出。即便转发本身失败也
        # 退出——宁可丢一次转发，不能开第二个窗口。
        forward_to_running(sys.argv[1:])
        startup_log.end("已有实例在运行，本进程转交后退出")
        return 0

    try:
        init_libmpv()
    except RuntimeError as exc:
        QMessageBox.critical(None, "缺少组件", str(exc))
        startup_log.end(f"libmpv 缺失: {exc}")
        return 2
    startup_log.stage("libmpv", "libmpv 初始化完成")

    init_gl_format()
    startup_log.stage("gl-format", "OpenGL 格式设置完成")

    # Qt's own strings (file dialog, standard buttons) default to English otherwise.
    # The translator must stay referenced or it gets garbage collected.
    global _qt_translator
    _qt_translator = QTranslator()
    if _qt_translator.load(
        QLocale.system(), "qtbase", "_", QLibraryInfo.path(QLibraryInfo.TranslationsPath)
    ):
        app.installTranslator(_qt_translator)

    # icon font detection needs a live QApplication, and the stylesheet needs the result
    app.setStyleSheet(theme.build_stylesheet(icons.detect_family()))
    startup_log.stage("stylesheet", f"全局样式表+图标字体({icons.FAMILY})就绪")

    # First launch: ask for the interface language before any window shows.
    from app.config import flush, settings
    from app.runtime import automation_mode

    if not settings["language"] and not automation_mode():
        from PySide6.QtWidgets import QMessageBox

        box = QMessageBox()
        box.setWindowTitle("选择界面语言 / Choose language")
        box.setText(
            "欢迎使用！请选择界面语言：\n"
            "Welcome! Choose your interface language:"
        )
        btn_zh = box.addButton("中文", QMessageBox.AcceptRole)
        btn_en = box.addButton("English", QMessageBox.AcceptRole)
        box.setDefaultButton(btn_zh)
        box.exec()
        settings["language"] = "zh" if box.clickedButton() is btn_zh else "en"
        flush()
    elif not settings["language"]:
        # 自动化模式：不弹首启语言选择（无人点击会永久卡住），默认中文
        settings["language"] = "zh"
        flush()

    from app.main_window import MainWindow

    startup_file = None
    if len(sys.argv) > 1:
        target = Path(sys.argv[1])
        if target.is_file():
            startup_file = target

    win = MainWindow(startup_file=startup_file)
    startup_log.stage("mainwindow", "主窗口构建完成")
    if server is not None:
        server.paths_received.connect(win.handle_external_paths)
        server.raise_requested.connect(win.raise_window)
        # 启动早期（listen 到信号接上之间）到达的转发会丢——补投
        early_paths, early_raise = server.take_early()
        if early_paths:
            win.handle_external_paths(early_paths)
        if early_raise:
            win.raise_window()
    win.show()
    startup_log.stage("shown", "主窗口 show() 完成，进入事件循环")

    # A folder passed on the command line wins over the remembered location.
    # Files are handled inside MainWindow (player first, then the folder scan).
    if len(sys.argv) > 1:
        target = Path(sys.argv[1])
        if target.is_dir():
            win.set_folder(target)

    rc = app.exec()
    startup_log.end(f"事件循环退出 rc={rc}")
    return rc


if __name__ == "__main__":
    sys.exit(main())
