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

    from app.main_window import MainWindow

    win = MainWindow()
    win.show()

    # A folder or file passed on the command line (e.g. "open with") wins over the
    # remembered location.
    if len(sys.argv) > 1:
        target = Path(sys.argv[1])
        if target.is_dir():
            win.set_folder(target)
        elif target.is_file():
            win.set_folder(target.parent)
            from PySide6.QtCore import QTimer

            QTimer.singleShot(600, lambda: win._open_path(target))

    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
