"""Icon glyphs from Windows' built-in monochrome icon font.

Emoji were the obvious first choice for the toolbars, but Windows renders many of
them as colour bitmaps (and silently substitutes tofu for others), which looks
wrong in a dark UI. Segoe Fluent Icons (Win11) / Segoe MDL2 Assets (Win10) are
present on every target machine and are monochrome, so they inherit text colour.

Codepoints are written as escapes rather than pasted as literal characters: these
live in the Unicode private-use area, where a literal is invisible in an editor and
easily mangled by an encoding round-trip.
"""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QFontDatabase
from PySide6.QtWidgets import QComboBox, QStyle, QStyleOptionComboBox, QStylePainter

_FALLBACK = "Segoe UI Symbol"
FAMILY = _FALLBACK


def detect_family() -> str:
    """Pick the best available icon font. Requires a QApplication to exist."""
    global FAMILY
    families = set(QFontDatabase.families())
    for name in ("Segoe Fluent Icons", "Segoe MDL2 Assets"):
        if name in families:
            FAMILY = name
            break
    else:
        FAMILY = _FALLBACK
    return FAMILY


# --- playback
PLAY = ""
PAUSE = ""
PREVIOUS = ""
NEXT = ""
VOLUME = ""
VOLUME_LOW = ""
MUTE = ""
REPEAT_ONE = ""
FULLSCREEN = ""
FULLSCREEN_EXIT = ""

# --- image
ZOOM_IN = ""
ZOOM_OUT = ""
FIT_PAGE = ""
ROTATE = ""

# --- browser chrome
FOLDER_OPEN = ""
LEVEL_UP = ""
REFRESH = ""
VIEW_GRID = ""
VIEW_TILES = ""
VIEW_LIST = ""
SIDEBAR = ""
SORT_ASC = ""
SORT_DESC = ""
SEARCH = ""
CLOSE = ""
CHEVRON_DOWN = ""


# --- playlist panel (appended by codepoint to keep the literals out of this file)
REPEAT_ALL = chr(0xE8EE)
SHUFFLE = chr(0xE8B1)
PLUS = chr(0xE710)
MINUS = chr(0xE738)
TRASH = chr(0xE74D)
NEW_ALBUM = chr(0xE8F4)
ALBUM = chr(0xE8B7)
PLAYLIST = chr(0xE8FD)
MOVE_UP = chr(0xE70E)
MOVE_DOWN = chr(0xE70D)
CHEVRON_RIGHT = chr(0xE76C)
CHEVRON_LEFT = chr(0xE76B)
HELP = chr(0xE897)
SETTINGS = chr(0xE713)
TOOLS = chr(0xE90F)          # 工具箱（Fluent "Developer Tools" 字形近似）


class ArrowComboBox(QComboBox):
    """QComboBox that paints its own chevron.

    Styling ::down-arrow through QSS without an image asset leaves an empty box, so
    the arrow is drawn with the icon font instead.
    """

    def paintEvent(self, e):
        painter = QStylePainter(self)
        opt = QStyleOptionComboBox()
        self.initStyleOption(opt)
        painter.drawComplexControl(QStyle.CC_ComboBox, opt)
        painter.drawControl(QStyle.CE_ComboBoxLabel, opt)

        f = painter.font()
        f.setFamily(FAMILY)
        f.setPixelSize(9)
        painter.setFont(f)
        painter.setPen(self.palette().text().color())
        painter.drawText(
            self.rect().adjusted(0, 0, -8, 0), Qt.AlignRight | Qt.AlignVCenter, CHEVRON_DOWN
        )
