"""Dark theme: colour tokens plus the global stylesheet."""
from __future__ import annotations

# Telegram-desktop-ish dark palette.
BG_DEEP = "#0e0f11"      # immersive viewer background
BG_BASE = "#17181c"      # window
BG_PANEL = "#1e2026"     # toolbars, side panel
BG_RAISED = "#262931"    # inputs, buttons
BG_HOVER = "#2f333d"
BG_SELECT = "#2b5278"    # Telegram's selected-row blue
ACCENT = "#5dc0f0"
ACCENT_DIM = "#3d8fb8"
TEXT = "#e8eaed"
TEXT_DIM = "#8f96a3"
TEXT_FAINT = "#5f6673"
BORDER = "#2a2d35"
SCRIM = "rgba(0, 0, 0, 190)"

GRID_TILE_BG = "#1a1c21"

_TEMPLATE = """
* {{
    font-family: "Microsoft YaHei UI", "Segoe UI", sans-serif;
    font-size: 13px;
    outline: none;
}}
QWidget {{ background: {BG_BASE}; color: {TEXT}; }}
/* labels must blend into whatever bar they sit on, not the window colour */
QLabel {{ background: transparent; }}

QFrame#Toolbar, QFrame#StatusBar {{
    background: {BG_PANEL};
    border: none;
}}
QFrame#Toolbar {{ border-bottom: 1px solid {BORDER}; }}
QFrame#StatusBar {{ border-top: 1px solid {BORDER}; }}

QPushButton, QToolButton {{
    background: {BG_RAISED};
    border: 1px solid {BORDER};
    border-radius: 6px;
    padding: 5px 11px;
    color: {TEXT};
}}
QPushButton:hover, QToolButton:hover {{ background: {BG_HOVER}; }}
QPushButton:pressed, QToolButton:pressed {{ background: {BG_SELECT}; }}
QPushButton:checked, QToolButton:checked {{
    background: {BG_SELECT};
    border-color: {ACCENT_DIM};
    color: #ffffff;
}}
QPushButton:disabled, QToolButton:disabled {{ color: {TEXT_FAINT}; background: {BG_PANEL}; }}
QToolButton#Flat {{ background: transparent; border: none; padding: 4px 8px; }}
QToolButton#Flat:hover {{ background: {BG_HOVER}; }}
QToolButton::menu-indicator {{ image: none; width: 0; }}

QComboBox {{
    background: {BG_RAISED};
    border: 1px solid {BORDER};
    border-radius: 6px;
    padding: 4px 26px 4px 9px;
    min-width: 78px;
}}
QComboBox:hover {{ background: {BG_HOVER}; }}
/* The chevron is painted by icons.ArrowComboBox, so leave no room for a native one. */
QComboBox::drop-down {{ border: none; width: 0; }}
QComboBox QAbstractItemView {{
    background: {BG_PANEL};
    border: 1px solid {BORDER};
    selection-background-color: {BG_SELECT};
    padding: 3px;
}}

QLineEdit {{
    background: {BG_RAISED};
    border: 1px solid {BORDER};
    border-radius: 6px;
    padding: 5px 9px;
    selection-background-color: {BG_SELECT};
}}
QLineEdit:focus {{ border-color: {ACCENT_DIM}; }}

QMenu {{
    background: {BG_PANEL};
    border: 1px solid {BORDER};
    border-radius: 8px;
    padding: 5px;
}}
QMenu::item {{ padding: 6px 26px 6px 22px; border-radius: 5px; }}
QMenu::item:selected {{ background: {BG_SELECT}; }}
QMenu::item:disabled {{ color: {TEXT_FAINT}; }}
QMenu::separator {{ height: 1px; background: {BORDER}; margin: 5px 8px; }}
QMenu::indicator {{ width: 14px; height: 14px; left: 6px; }}

QSlider::groove:horizontal {{ height: 4px; background: {BG_RAISED}; border-radius: 2px; }}
QSlider::sub-page:horizontal {{ background: {ACCENT_DIM}; border-radius: 2px; }}
QSlider::handle:horizontal {{
    background: {TEXT}; width: 12px; height: 12px;
    margin: -4px 0; border-radius: 6px;
}}
QSlider::handle:horizontal:hover {{ background: {ACCENT}; }}

QTreeView, QListView, QTableView {{
    background: {BG_BASE};
    border: none;
    selection-background-color: {BG_SELECT};
    alternate-background-color: {BG_PANEL};
}}
QTreeView {{ background: {BG_PANEL}; }}
QTreeView::item, QTableView::item {{ padding: 3px; border: none; }}
QTreeView::item:hover, QTableView::item:hover {{ background: {BG_HOVER}; }}
QTreeView::branch:hover {{ background: {BG_HOVER}; }}

QHeaderView::section {{
    background: {BG_PANEL};
    color: {TEXT_DIM};
    border: none;
    border-right: 1px solid {BORDER};
    border-bottom: 1px solid {BORDER};
    padding: 6px 8px;
}}
QHeaderView::section:hover {{ background: {BG_HOVER}; color: {TEXT}; }}
QTableCornerButton::section {{ background: {BG_PANEL}; border: none; }}

QScrollBar:vertical {{ background: transparent; width: 11px; margin: 0; }}
QScrollBar:horizontal {{ background: transparent; height: 11px; margin: 0; }}
QScrollBar::handle {{ background: #3a3e48; border-radius: 5px; min-height: 30px; min-width: 30px; }}
QScrollBar::handle:hover {{ background: #4d525e; }}
QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; width: 0; }}
QScrollBar::add-page, QScrollBar::sub-page {{ background: transparent; }}

QSplitter::handle {{ background: {BORDER}; }}
QSplitter::handle:horizontal {{ width: 1px; }}
QSplitter::handle:hover {{ background: {ACCENT_DIM}; }}

/* ---- immersive viewer overlays: must stay transparent so the video shows through */
QWidget#ViewerOverlay {{ background: transparent; }}
QWidget#ViewerStage {{ background: {BG_DEEP}; }}
QLabel#OverlayTitle {{
    background: transparent; color: #ffffff;
    font-size: 14px; font-weight: 600;
}}
QLabel#OverlayDim {{ background: transparent; color: #c3c8d1; font-size: 12px; }}
QLabel#OverlayMono {{
    background: transparent; color: #ffffff;
    font-family: Consolas, "Cascadia Mono", monospace; font-size: 12px;
}}
QLabel#OverlayError {{
    background: transparent; color: #ff8f8f; font-size: 14px;
}}
QToolButton#OverlayBtn, QToolButton#OverlayIcon {{
    background: rgba(255, 255, 255, 24);
    border: none; border-radius: 6px;
    color: #ffffff; font-size: 13px; padding: 0;
}}
QToolButton#OverlayBtn:hover, QToolButton#OverlayIcon:hover {{
    background: rgba(255, 255, 255, 58);
}}
QToolButton#OverlayBtn:pressed, QToolButton#OverlayIcon:pressed {{ background: {ACCENT_DIM}; }}
QToolButton#OverlayBtn:checked, QToolButton#OverlayIcon:checked {{ background: {ACCENT_DIM}; }}
QToolButton#OverlayBtn:disabled, QToolButton#OverlayIcon:disabled {{
    background: rgba(255, 255, 255, 12); color: #6e7480;
}}
/* icon-font buttons: family is substituted in at build time */
QToolButton#OverlayIcon {{ font-family: "{icon_family}"; font-size: 15px; }}
QToolButton#IconBtn {{ font-family: "{icon_family}"; font-size: 13px; padding: 2px 6px; }}
QSlider#OverlaySlider::groove:horizontal {{
    height: 4px; background: rgba(255, 255, 255, 46); border-radius: 2px;
}}
QSlider#OverlaySlider::sub-page:horizontal {{ background: {ACCENT}; border-radius: 2px; }}
QSlider#OverlaySlider::handle:horizontal {{
    background: #ffffff; width: 11px; height: 11px; margin: -4px 0; border-radius: 6px;
}}

/* ---- docked side panel (playlist / albums / browser) */
QWidget#Panel {{ background: {BG_PANEL}; }}
QWidget#PanelHeader {{ background: {BG_PANEL}; border-bottom: 1px solid {BORDER}; }}
QWidget#AlbumStrip {{ background: {BG_BASE}; border-bottom: 1px solid {BORDER}; }}
QWidget#PanelFooter {{ background: {BG_PANEL}; border-top: 1px solid {BORDER}; }}
QToolButton#PanelTab {{
    background: transparent; border: none;
    border-bottom: 2px solid transparent;
    color: {TEXT_DIM}; padding: 4px 10px; font-size: 12px;
}}
QToolButton#PanelTab:hover {{ color: {TEXT}; background: {BG_RAISED}; }}
QToolButton#PanelTab:checked {{
    color: #ffffff; border-bottom-color: {ACCENT}; background: {BG_RAISED};
}}
QToolButton#AlbumTab {{
    background: transparent; border: none; border-radius: 4px;
    color: {TEXT_DIM}; padding: 3px 9px; font-size: 11px;
}}
QToolButton#AlbumTab:hover {{ background: {BG_RAISED}; color: {TEXT}; }}
QToolButton#AlbumTab:checked {{ background: {BG_SELECT}; color: #ffffff; }}
QToolButton#PanelIcon {{
    font-family: "{icon_family}"; font-size: 13px;
    background: transparent; border: none; border-radius: 4px; color: {TEXT_DIM};
}}
QToolButton#PanelIcon:hover {{ background: {BG_RAISED}; color: {TEXT}; }}
QToolButton#PanelIcon:pressed {{ background: {BG_SELECT}; }}
QToolButton#PanelIcon[active="true"] {{ color: {ACCENT}; }}
QToolButton#PanelBtn {{
    background: {BG_RAISED}; border: 1px solid {BORDER}; border-radius: 4px;
    color: {TEXT_DIM}; padding: 2px 8px; font-size: 11px;
}}
QToolButton#PanelBtn:hover {{ background: {BG_HOVER}; color: {TEXT}; }}
QToolButton#PanelBtn:checked {{
    background: {BG_SELECT}; color: #ffffff; border-color: {ACCENT_DIM};
}}
QLineEdit#PanelSearch {{
    background: {BG_BASE}; border: 1px solid {BORDER}; border-radius: 4px;
    padding: 3px 7px; font-size: 11px;
}}
QListWidget#PanelList {{
    background: {BG_BASE}; border: none; outline: none;
}}
QListWidget#PanelList::item {{ border: none; padding: 0; }}
QListWidget#PanelList::item:selected {{ background: transparent; }}
QTreeView#PanelTree {{ background: {BG_BASE}; border: none; font-size: 12px; }}

/* ---- welcome / empty start page */
QWidget#Welcome {{ background: {BG_BASE}; }}
QLabel#WelcomeTitle {{ color: {TEXT}; font-size: 26px; font-weight: 600; }}
QLabel#WelcomeHint {{ color: {TEXT_DIM}; font-size: 13px; }}
QPushButton#WelcomeButton {{
    font-family: "{icon_family}";
    background: {BG_SELECT}; border: 1px solid {ACCENT_DIM};
    border-radius: 8px; padding: 10px 26px; color: #ffffff; font-size: 14px;
}}
QPushButton#WelcomeButton:hover {{ background: {ACCENT_DIM}; }}
QToolButton#RecentEntry {{
    background: transparent; border: none; border-radius: 6px;
    padding: 6px 14px; color: {TEXT_DIM}; font-size: 12px;
}}
QToolButton#RecentEntry:hover {{ background: {BG_RAISED}; color: {TEXT}; }}

QLabel#Hint {{ color: {TEXT_DIM}; }}
QLabel#Placeholder {{ color: {TEXT_FAINT}; font-size: 15px; }}
QToolTip {{
    background: {BG_PANEL};
    color: {TEXT};
    border: 1px solid {BORDER};
    padding: 4px 7px;
}}
"""


def build_stylesheet(icon_family: str) -> str:
    """Fill the colour tokens and the detected icon font family into the template."""
    tokens = {
        name: value
        for name, value in globals().items()
        if name.isupper() and isinstance(value, str)
    }
    tokens["icon_family"] = icon_family
    return _TEMPLATE.format(**tokens)
