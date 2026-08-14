"""Archive browser: pick a media file inside a zip/rar/7z/tar and play it.

Members are extracted on demand into the archive cache directory (configurable
in Settings); the extracted file is handed to the main window's player.
"""
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
)

from . import archive, media
from .i18n import t


def _human_size(n: int) -> str:
    f = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if f < 1024 or unit == "GB":
            return f"{f:.0f} {unit}" if unit == "B" else f"{f:.1f} {unit}"
        f /= 1024
    return f"{f:.1f} GB"


class ArchiveBrowser(QDialog):
    """List playable media inside an archive; double-click to play."""

    def __init__(self, archive_path: Path, parent=None) -> None:
        super().__init__(parent)
        self._archive = Path(archive_path)
        self._password: str | None = None
        self._entries: list[archive.ArchiveEntry] = []
        self.setWindowTitle(f"{self._archive.name}  —  {t('archive.title_suffix')}")
        self.resize(560, 420)

        root = QVBoxLayout(self)
        self.list = QListWidget()
        self.list.itemDoubleClicked.connect(lambda _it: self._play_selected())
        root.addWidget(self.list)

        self.hint = QLabel("")
        self.hint.setWordWrap(True)
        self.hint.setObjectName("WelcomeHint")
        root.addWidget(self.hint)

        row = QHBoxLayout()
        row.addStretch(1)
        self.btn_play = QPushButton(t("archive.play"))
        self.btn_play.setObjectName("WelcomeButton")
        self.btn_play.clicked.connect(self._play_selected)
        row.addWidget(self.btn_play)
        btn_close = QPushButton(t("archive.close"))
        btn_close.clicked.connect(self.reject)
        row.addWidget(btn_close)
        root.addLayout(row)

        self._reload()

    # ------------------------------------------------------------------

    def _reload(self) -> None:
        self.list.clear()
        entries, err = archive.list_archive(self._archive, self._password)
        if err == "password":
            if not self._ask_password():
                self.hint.setText(t("archive.cancelled"))
                return
            self._reload()
            return
        if err == "no7z":
            self.hint.setText(t("archive.no7z"))
            return
        if err:
            self.hint.setText(t("archive.error").format(error=err))
            return
        self._entries = [e for e in entries if e.is_media]
        if not self._entries:
            self.hint.setText(t("archive.empty"))
            return
        for e in self._entries:
            item = QListWidgetItem(f"{e.name}      {_human_size(e.size)}")
            item.setToolTip(e.name)
            item.setData(Qt.UserRole, e.name)
            self.list.addItem(item)
        self.list.setCurrentRow(0)
        self.btn_play.setEnabled(True)

    def _ask_password(self) -> bool:
        pwd, ok = QInputDialog.getText(
            self,
            t("archive.password_title"),
            t("archive.password_prompt"),
            QInputDialog.Password,
        )
        if not ok:
            return False
        self._password = pwd
        return True

    def _play_selected(self) -> None:
        item = self.list.currentItem()
        if item is None:
            return
        member = item.data(Qt.UserRole)
        if not member:
            return
        try:
            dest = archive.cache_dir() / self._archive.stem
            extracted = archive.extract_member(
                self._archive, member, dest, self._password
            )
        except RuntimeError as exc:
            if str(exc) == "password":
                if self._ask_password():
                    self._play_selected()
                return
            QMessageBox.warning(self, t("archive.title_suffix"), str(exc))
            return
        except Exception as exc:  # noqa: BLE001
            QMessageBox.warning(self, t("archive.title_suffix"), str(exc))
            return
        owner = self.parent()
        if owner is None or not hasattr(owner, "ensure_viewer"):
            return
        item_media = media.item_for_path(extracted)
        owner.ensure_viewer().open_playlist([item_media], 0)
