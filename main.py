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

    # 单实例：播放器已在运行时，外部“打开方式 / 双击”启动的第二个进程
    # 把文件路径转发给已有窗口后直接退出，不再开第二个界面。
    from app.single_instance import forward_to_running

    if forward_to_running(sys.argv[1:]):
        startup_log.end("forwarded（转交给已有实例）")
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

    from app.single_instance import InstanceServer

    server = InstanceServer()
    server.listen()
    startup_log.stage("server", "单实例服务就绪")

    startup_file = None
    if len(sys.argv) > 1:
        target = Path(sys.argv[1])
        if target.is_file():
            startup_file = target

    win = MainWindow(startup_file=startup_file)
    startup_log.stage("mainwindow", "主窗口构建完成")
    server.paths_received.connect(win.handle_external_paths)
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
