"""Empty start page shown before any folder is opened."""
from __future__ import annotations

import threading
from pathlib import Path

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from . import icons, theme
from .config import settings
from .i18n import t

MAX_RECENT = 8
MAX_RECENT_FILES = 10


class WelcomePage(QWidget):
    folder_chosen = Signal(object)   # Path
    file_chosen = Signal(object)     # Path (a single recently-played media file)
    open_requested = Signal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("Welcome")
        outer = QVBoxLayout(self)
        outer.setContentsMargins(40, 40, 40, 40)
        outer.addStretch(2)

        title = QLabel(t("welcome.title"))
        title.setObjectName("WelcomeTitle")
        title.setAlignment(Qt.AlignCenter)
        outer.addWidget(title)

        hint = QLabel(t("welcome.hint"))
        hint.setObjectName("WelcomeHint")
        hint.setAlignment(Qt.AlignCenter)
        outer.addWidget(hint)
        outer.addSpacing(22)

        row = QHBoxLayout()
        row.addStretch(1)
        btn = QPushButton(icons.FOLDER_OPEN + "   " + t("welcome.open_folder"))
        btn.setObjectName("WelcomeButton")
        btn.setCursor(Qt.PointingHandCursor)
        btn.clicked.connect(self.open_requested)
        row.addWidget(btn)
        row.addStretch(1)
        outer.addLayout(row)
        outer.addSpacing(30)

        self.recent_title = QLabel(t("welcome.recent_folders"))
        self.recent_title.setObjectName("WelcomeHint")
        self.recent_title.setAlignment(Qt.AlignCenter)
        outer.addWidget(self.recent_title)
        outer.addSpacing(6)

        self.recent_box = QVBoxLayout()
        self.recent_box.setSpacing(3)
        holder = QHBoxLayout()
        holder.addStretch(1)
        wrap = QWidget()
        wrap.setLayout(self.recent_box)
        wrap.setSizePolicy(QSizePolicy.Maximum, QSizePolicy.Preferred)
        holder.addWidget(wrap)
        holder.addStretch(1)
        outer.addLayout(holder)

        outer.addSpacing(18)
        self.recent_files_title = QLabel(t("welcome.recent_files"))
        self.recent_files_title.setObjectName("WelcomeHint")
        self.recent_files_title.setAlignment(Qt.AlignCenter)
        outer.addWidget(self.recent_files_title)
        outer.addSpacing(6)
        self.recent_files_box = QVBoxLayout()
        self.recent_files_box.setSpacing(3)
        fholder = QHBoxLayout()
        fholder.addStretch(1)
        fwrap = QWidget()
        fwrap.setLayout(self.recent_files_box)
        fwrap.setSizePolicy(QSizePolicy.Maximum, QSizePolicy.Preferred)
        fholder.addWidget(fwrap)
        fholder.addStretch(1)
        outer.addLayout(fholder)

        outer.addStretch(3)
        self.refresh_recent()

    # ------------------------------------------------------------------

    def refresh_recent(self) -> None:
        """List the remembered folders straight away, prune the dead ones afterwards.

        Checking whether each one still exists is a `stat()` per entry, and one entry on
        a sleeping USB disk or a network share that has to be re-dialled can block for
        several seconds — this runs at startup, so on the GUI thread that is the window
        appearing frozen before it has drawn anything. The list is shown first and the
        check happens on a worker.
        """
        entries = [p for p in settings["recent_folders"] if isinstance(p, str)]
        self._show_recent(entries)
        files = [p for p in settings["recent_files"] if isinstance(p, str)]
        self._show_recent_files(files)
        threading.Thread(
            target=self._prune_dead, args=(entries,), daemon=True, name="recent-check"
        ).start()

    def _prune_dead(self, entries: list[str]) -> None:
        alive = []
        for p in entries:
            try:
                if Path(p).is_dir():
                    alive.append(p)
            except OSError:
                continue
        if alive != entries:
            # Back to the GUI thread to touch settings and rebuild the buttons.
            QTimer.singleShot(0, lambda: self._apply_pruned(alive))

    def _apply_pruned(self, alive: list[str]) -> None:
        settings["recent_folders"] = alive
        self._show_recent(alive)

    def _show_recent(self, paths: list[str]) -> None:
        while self.recent_box.count():
            item = self.recent_box.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()

        self.recent_title.setVisible(bool(paths))
        for path in paths[:MAX_RECENT]:
            b = QToolButton()
            b.setObjectName("RecentEntry")
            name = Path(path).name or path
            b.setText(f"{name}      {path}")
            b.setToolTip(path)
            b.setCursor(Qt.PointingHandCursor)
            b.setSizePolicy(QSizePolicy.Maximum, QSizePolicy.Preferred)
            b.clicked.connect(lambda _=False, p=path: self.folder_chosen.emit(Path(p)))
            self.recent_box.addWidget(b)

    def _show_recent_files(self, paths: list[str]) -> None:
        while self.recent_files_box.count():
            item = self.recent_files_box.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()

        self.recent_files_title.setVisible(bool(paths))
        for path in paths[:MAX_RECENT_FILES]:
            b = QToolButton()
            b.setObjectName("RecentEntry")
            name = Path(path).name or path
            b.setText(f"{name}      {path}")
            b.setToolTip(path)
            b.setCursor(Qt.PointingHandCursor)
            b.setSizePolicy(QSizePolicy.Maximum, QSizePolicy.Preferred)
            b.clicked.connect(lambda _=False, p=path: self.file_chosen.emit(Path(p)))
            self.recent_files_box.addWidget(b)


def remember_recent(folder: Path) -> None:
    text = str(folder)
    entries = [p for p in settings["recent_folders"] if isinstance(p, str) and p != text]
    entries.insert(0, text)
    settings["recent_folders"] = entries[:MAX_RECENT]


def remember_recent_file(path: Path) -> None:
    """Push a just-opened media file to the front of the 最近播放 list."""
    text = str(path)
    entries = [p for p in settings["recent_files"] if isinstance(p, str) and p != text]
    entries.insert(0, text)
    settings["recent_files"] = entries[:MAX_RECENT_FILES]
