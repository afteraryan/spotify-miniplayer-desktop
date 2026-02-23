# Visual constants for the Spotify Taskbar Player
# Compact layout — sized to sit ON the Windows 11 taskbar (48px)

# ── Window ────────────────────────────────────────────────────
WIDGET_WIDTH = 310
WIDGET_WIDTH_EXPANDED = 345   # when search icon is visible (on hover)
WIDGET_HEIGHT = 40          # fits inside the 48px taskbar with margin
CORNER_RADIUS = 8

# ── Album art ─────────────────────────────────────────────────
ART_SIZE = 30

# ── Buttons ───────────────────────────────────────────────────
BUTTON_SIZE = 30
ICON_SIZE = 18
CLOSE_SIZE = 20
CLOSE_ICON_SIZE = 10

# ── Colors (R, G, B, A) ──────────────────────────────────────
BG_COLOR = (38, 38, 38, 210)       # blends with Win11 dark taskbar
BORDER_COLOR = (80, 80, 80, 60)
TEXT_COLOR = "#FFFFFF"
SUBTEXT_COLOR = "#B3B3B3"
HOVER_BG = "rgba(255, 255, 255, 25)"
CLOSE_HOVER_BG = "rgba(232, 17, 35, 180)"

# ── Font ──────────────────────────────────────────────────────
FONT_FAMILY = "Segoe UI"
TITLE_SIZE = 11
ARTIST_SIZE = 9

# ── Layout ────────────────────────────────────────────────────
PADDING = 5
SPACING = 5

# ── SVG icons (white, Material Design style) ─────────────────
ICON_PREV = '<svg viewBox="0 0 24 24"><path d="M6 6h2v12H6zm3.5 6l8.5 6V6z" fill="white"/></svg>'
ICON_PLAY = '<svg viewBox="0 0 24 24"><path d="M8 5v14l11-7z" fill="white"/></svg>'
ICON_PAUSE = '<svg viewBox="0 0 24 24"><path d="M6 19h4V5H6zm8-14v14h4V5z" fill="white"/></svg>'
ICON_NEXT = '<svg viewBox="0 0 24 24"><path d="M6 18l8.5-6L6 6zm8.5-6V6h2v12h-2z" fill="white"/></svg>'
ICON_CLOSE = '<svg viewBox="0 0 24 24"><path d="M19 6.41L17.59 5 12 10.59 6.41 5 5 6.41 10.59 12 5 17.59 6.41 19 12 13.41 17.59 19 19 17.59 13.41 12z" fill="white"/></svg>'
ICON_SEARCH = '<svg viewBox="0 0 24 24"><path d="M15.5 14h-.79l-.28-.27A6.47 6.47 0 0016 9.5 6.5 6.5 0 109.5 16c1.61 0 3.09-.59 4.23-1.57l.27.28v.79l5 4.99L20.49 19l-4.99-5zm-6 0C7.01 14 5 11.99 5 9.5S7.01 5 9.5 5 14 7.01 14 9.5 11.99 14 9.5 14z" fill="white"/></svg>'
ICON_LOADING = '<svg viewBox="0 0 24 24"><path d="M12 4V1L8 5l4 4V6c3.31 0 6 2.69 6 6 0 1.01-.25 1.97-.7 2.8l1.46 1.46C19.54 15.03 20 13.57 20 12c0-4.42-3.58-8-8-8zm0 14c-3.31 0-6-2.69-6-6 0-1.01.25-1.97.7-2.8L5.24 7.74C4.46 8.97 4 10.43 4 12c0 4.42 3.58 8 8 8v3l4-4-4-4v3z" fill="white"/></svg>'

# ── Stylesheets ───────────────────────────────────────────────
BUTTON_STYLE = f"""
    QPushButton {{
        background: transparent;
        border: none;
        border-radius: {BUTTON_SIZE // 2}px;
    }}
    QPushButton:hover {{
        background: {HOVER_BG};
    }}
    QPushButton:pressed {{
        background: rgba(255, 255, 255, 40);
    }}
"""

CLOSE_BUTTON_STYLE = f"""
    QPushButton {{
        background: transparent;
        border: none;
        border-radius: {CLOSE_SIZE // 2}px;
    }}
    QPushButton:hover {{
        background: {CLOSE_HOVER_BG};
    }}
    QPushButton:pressed {{
        background: rgba(232, 17, 35, 220);
    }}
"""

TITLE_STYLE = f"color: {TEXT_COLOR}; background: transparent;"
ARTIST_STYLE = f"color: {SUBTEXT_COLOR}; background: transparent;"
NO_MUSIC_STYLE = f"color: {SUBTEXT_COLOR}; background: transparent;"

# ── Search popup ────────────────────────────────────────────────
SEARCH_POPUP_WIDTH = 350
SEARCH_POPUP_BG = (30, 30, 30, 240)
SEARCH_RESULT_HEIGHT = 50
SEARCH_ART_SIZE = 40
