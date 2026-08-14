"""Entry point. Bootstraps libmpv before anything else touches the mpv module."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.runtime import init_gl_format, init_libmpv  # noqa: E402

_qt_translator = None  # kept alive for the lifetime of the process


def main() -> int:
    try:
        init_libmpv()
    except RuntimeError as exc:
        from PySide6.QtWidgets import QApplication, QMessageBox

        app = QApplication(sys.argv)
        QMessageBox.critical(None, "缺少组件", str(exc))
        return 2

    init_gl_format()

    from PySide6.QtWidgets import QApplication

    from PySide6.QtCore import QLibraryInfo, QLocale, QTranslator

    from app import icons, theme

    QApplication.setApplicationName("媒体播放器")
    QApplication.setOrganizationName("LocalMediaPlayer")
    app = QApplication(sys.argv)

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

    # First launch: ask for the interface language before any window shows.
    from app.config import flush, settings

    if not settings["language"]:
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

    from app.main_window import MainWindow

    startup_file = None
    if len(sys.argv) > 1:
        target = Path(sys.argv[1])
        if target.is_file():
            startup_file = target

    win = MainWindow(startup_file=startup_file)
    win.show()

    # A folder passed on the command line wins over the remembered location.
    # Files are handled inside MainWindow (player first, then the folder scan).
    if len(sys.argv) > 1:
        target = Path(sys.argv[1])
        if target.is_dir():
            win.set_folder(target)

    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
