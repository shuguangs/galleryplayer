"""In-app help: a scrollable cheat-sheet of every feature and keyboard shortcut.

The app grew a rich key map and a pile of toggles that were previously only
discoverable by reading the source. This dialog surfaces all of it in one place,
reachable from the toolbar (?) and from the viewer (F1 / ?).
"""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QDialog, QTextBrowser, QVBoxLayout

from . import theme
from .i18n import t

# (key, description) rows, grouped by section title.
_BROWSER = [
    ("Ctrl+O", t("help.open_folder")),
    ("Backspace", t("help.parent_dir")),
    ("F5", t("help.rescan")),
    ("Ctrl+1 / 2 / 3", t("help.views")),
    ("Ctrl+滚轮", t("help.grid_cols")),
    ("Ctrl+F", t("help.search")),
    ("Ctrl+B", t("help.toggle_tree")),
    ("Ctrl+,", t("help.settings")),
    ("双击", t("help.open_item")),
    ("右键", t("help.context_menu")),
]

_PLAYER = [
    ("空格 / K", t("help.play_pause")),
    ("F / 回车 / 双击画面", t("help.fullscreen")),
    ("Esc", t("help.esc")),
    ("Tab", t("help.toggle_panel")),
    ("滚轮 / PageUp / PageDown", t("help.prev_next")),
    ("← / →", t("help.seek")),
    ("↑ / ↓", t("help.volume")),
    ("Home / End", t("help.jump")),
    ("M", t("help.mute")),
    ("L", t("help.loop")),
    ("V", t("help.subtitle")),
    ("J", t("help.subtitle_track")),
    ("A", t("help.audio_track")),
    ("[ / ]", t("help.speed")),
    ("S", t("help.screenshot")),
    ("G", t("help.gif")),
    ("I", t("help.ab_loop")),
    ("O", t("help.ab_cancel")),
    (". / ,", t("help.frame_step")),
    ("Delete", t("help.remove_item")),
]

_IMAGE = [
    ("+ / - / Ctrl+滚轮", t("help.zoom")),
    ("0", t("help.fit")),
    ("1", t("help.actual")),
    ("R / Shift+R", t("help.rotate")),
    ("← ↑ / → ↓", t("help.prev_image")),
]

_FEATURES = [
    (t("help.feat_mixed.title"), t("help.feat_mixed.desc")),
    (t("help.feat_resume.title"), t("help.feat_resume.desc")),
    (t("help.feat_hwdec.title"), t("help.feat_hwdec.desc")),
    (t("help.feat_native.title"), t("help.feat_native.desc")),
    (t("help.feat_shot.title"), t("help.feat_shot.desc")),
    (t("help.feat_ab.title"), t("help.feat_ab.desc")),
    (t("help.feat_frame.title"), t("help.feat_frame.desc")),
    (t("help.feat_picadj.title"), t("help.feat_picadj.desc")),
    (t("help.feat_drag.title"), t("help.feat_drag.desc")),
    (t("help.feat_recent.title"), t("help.feat_recent.desc")),
    (t("help.feat_playlist.title"), t("help.feat_playlist.desc")),
    (t("help.feat_assoc.title"), t("help.feat_assoc.desc")),
    (t("help.feat_settings.title"), t("help.feat_settings.desc")),
    (t("help.feat_album.title"), t("help.feat_album.desc")),
    (t("help.feat_portable.title"), t("help.feat_portable.desc")),
]


def _rows_html(rows: list[tuple[str, str]]) -> str:
    out = []
    for key, desc in rows:
        out.append(
            f"<tr><td class='k'>{key}</td><td class='d'>{desc}</td></tr>"
        )
    return "".join(out)


def _build_html() -> str:
    def section(title: str, rows: list[tuple[str, str]]) -> str:
        return (
            f"<h2>{title}</h2>"
            f"<table cellspacing='0' cellpadding='0' width='100%'>{_rows_html(rows)}</table>"
        )

    return f"""
    <html><head><style>
      body {{ color:{theme.TEXT}; font-size:14px; }}
      h1 {{ color:{theme.ACCENT}; font-size:19px; margin:0 0 4px 0; }}
      h2 {{ color:{theme.ACCENT}; font-size:15px; margin:18px 0 6px 0;
            border-bottom:1px solid {theme.BORDER}; padding-bottom:4px; }}
      p.sub {{ color:{theme.TEXT_DIM}; margin:0 0 6px 0; }}
      td.k {{ color:{theme.TEXT}; font-family:'Consolas','Segoe UI Mono',monospace;
              white-space:nowrap; padding:3px 14px 3px 0; vertical-align:top; width:34%; }}
      td.d {{ color:{theme.TEXT_DIM}; padding:3px 0; vertical-align:top; }}
    </style></head><body>
      <h1>{t("help.title")}</h1>
      <p class='sub'>{t("help.subtitle")}</p>
      {section(t("help.section_browser"), _BROWSER)}
      {section(t("help.section_player"), _PLAYER)}
      {section(t("help.section_image"), _IMAGE)}
      {section(t("help.section_features"), _FEATURES)}
    </body></html>
    """


class HelpDialog(QDialog):
    """Modeless cheat-sheet. One instance is reused via `HelpDialog.show_for`."""

    _instance: "HelpDialog | None" = None

    def __init__(self, parent=None) -> None:
        # parent 一律不认（同 SettingsDialog）：避免 Windows owned-window
        # 联动最小化；Qt.Window 独立顶层，可各自最小化
        super().__init__(None)
        self.setWindowFlag(Qt.Window, True)
        self.setWindowTitle(t("help.title"))
        self.setMinimumSize(560, 640)
        self.setStyleSheet(
            f"QDialog {{ background:{theme.BG_BASE}; }}"
            f"QTextBrowser {{ background:{theme.BG_PANEL}; border:1px solid {theme.BORDER};"
            f" border-radius:8px; padding:10px 16px; }}"
        )
        lay = QVBoxLayout(self)
        lay.setContentsMargins(14, 14, 14, 14)
        view = QTextBrowser(self)
        view.setOpenExternalLinks(False)
        view.setHtml(_build_html())
        lay.addWidget(view)

    def keyPressEvent(self, e):  # noqa: ANN001
        if e.key() == Qt.Key_Escape:
            self.close()
            return
        super().keyPressEvent(e)

    @classmethod
    def show_for(cls, parent=None) -> "HelpDialog":
        """Open (or re-focus) the shared help window."""
        dlg = cls._instance
        if dlg is None:
            dlg = cls()  # 无 parent：独立顶层窗口，不随主窗口最小化
            cls._instance = dlg
        dlg.show()
        dlg.raise_()
        dlg.activateWindow()
        return dlg
