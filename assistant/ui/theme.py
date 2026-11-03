"""The single place where colours, sizes and fonts are defined. UI code reads from here."""
from __future__ import annotations

# Colours
BG_PANEL = "#1e2130"
BORDER = "#343a55"
TEXT = "#e8eaf2"
TEXT_MUTED = "#8b91ad"
ACCENT = "#5b7cfa"
SELECTION = "#2d3a5a"

# Sizes (4 px base)
RADIUS = 12
RADIUS_ROW = 8
SPACING = 8
PADDING = 12
WINDOW_WIDTH = 680
ROW_HEIGHT = 52
INPUT_HEIGHT = 48
MAX_LIST_ROWS = 8

# Fonts
FONT_FAMILY = "Segoe UI"
FONT_SIZE_INPUT = 15
FONT_SIZE_TITLE = 10.5
FONT_SIZE_SUBTITLE = 9
FONT_SIZE_STATUS = 9


def stylesheet() -> str:
    return f"""
    QFrame#panel {{
        background: {BG_PANEL};
        border: 1px solid {BORDER};
        border-radius: {RADIUS}px;
    }}
    QLineEdit#searchInput {{
        background: transparent;
        border: none;
        color: {TEXT};
        font-family: "{FONT_FAMILY}";
        font-size: {FONT_SIZE_INPUT}pt;
        padding: 0 {SPACING}px;
        selection-background-color: {ACCENT};
    }}
    QListWidget#resultList {{
        background: transparent;
        border: none;
        outline: none;
    }}
    QLabel#statusLabel {{
        color: {TEXT_MUTED};
        font-family: "{FONT_FAMILY}";
        font-size: {FONT_SIZE_STATUS}pt;
        padding: 0 {SPACING}px;
    }}
    """
