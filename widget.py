"""
The visual player widget — sits directly ON the Windows taskbar,
looking like a native part of it. Drag left/right to reposition.

System tray icon for show/hide, auto-start, and quit.
"""

import os
import sys
import json
import time
import ctypes
import ctypes.wintypes as wintypes
import subprocess
import threading
import winreg

from PySide6.QtWidgets import (
    QWidget, QHBoxLayout, QVBoxLayout, QLabel, QPushButton, QLineEdit,
    QApplication, QSystemTrayIcon, QMenu, QStackedWidget,
)
from PySide6.QtCore import (
    Qt, QTimer, QSize, QByteArray, QPoint, Signal,
    QPropertyAnimation, QEasingCurve,
)
from PySide6.QtGui import (
    QPainter, QPixmap, QColor, QIcon, QPainterPath, QFont, QPolygon, QAction,
    QTransform,
)
from PySide6.QtSvg import QSvgRenderer

from styles import (
    WIDGET_WIDTH, WIDGET_HEIGHT, CORNER_RADIUS, ART_SIZE,
    BUTTON_SIZE, ICON_SIZE, CLOSE_SIZE, CLOSE_ICON_SIZE,
    BG_COLOR, BORDER_COLOR, TEXT_COLOR,
    FONT_FAMILY, TITLE_SIZE, ARTIST_SIZE,
    PADDING, SPACING,
    ICON_PREV, ICON_PLAY, ICON_PAUSE, ICON_NEXT, ICON_CLOSE, ICON_SEARCH, ICON_LOADING,
    BUTTON_STYLE, CLOSE_BUTTON_STYLE,
    TITLE_STYLE, ARTIST_STYLE, NO_MUSIC_STYLE,
)
from media_controller import MediaController
from spotify_auth import SpotifyAuth, CLIENT_ID
from spotify_api import SpotifyAPI
from search_popup import SearchPopup

# ── Set proper 64-bit return types for Win32 functions ────
# Without this, ctypes defaults to c_int (32-bit) which can
# truncate 64-bit handles on x64 Windows.
ctypes.windll.user32.GetForegroundWindow.restype = wintypes.HWND
ctypes.windll.user32.MonitorFromWindow.restype = wintypes.HANDLE
ctypes.windll.user32.GetWindowLongW.restype = wintypes.LONG

# ── auto-start helpers ────────────────────────────────────────

_REG_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
_REG_NAME = "SpotifyTaskbarPlayer"


def _is_autostart_enabled():
    try:
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, _REG_KEY, 0,
                             winreg.KEY_QUERY_VALUE)
        winreg.QueryValueEx(key, _REG_NAME)
        winreg.CloseKey(key)
        return True
    except (FileNotFoundError, OSError):
        return False


def _set_autostart(enable):
    key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, _REG_KEY, 0,
                         winreg.KEY_SET_VALUE)
    if enable:
        # Point at launch.pyw via pythonw.exe (no console)
        project_dir = os.path.dirname(os.path.abspath(__file__))
        pyw = os.path.join(project_dir, "launch.pyw")
        pythonw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
        winreg.SetValueEx(key, _REG_NAME, 0, winreg.REG_SZ,
                          f'"{pythonw}" "{pyw}"')
    else:
        try:
            winreg.DeleteValue(key, _REG_NAME)
        except FileNotFoundError:
            pass
    winreg.CloseKey(key)


# ── helpers ───────────────────────────────────────────────────

def svg_to_icon(svg_str, size):
    """Convert SVG to QIcon. Renders at 4× then Qt shrinks to fit — always crisp."""
    render_size = size * 4
    renderer = QSvgRenderer(QByteArray(svg_str.encode()))
    pixmap = QPixmap(render_size, render_size)
    pixmap.fill(Qt.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.Antialiasing)
    painter.setRenderHint(QPainter.SmoothPixmapTransform)
    renderer.render(painter)
    painter.end()
    return QIcon(pixmap)


# ── widget ────────────────────────────────────────────────────

class PlayerWidget(QWidget):
    _web_check_done = Signal(object)  # thread-safe signal for Web API result

    def __init__(self):
        super().__init__()
        self._web_check_done.connect(self._on_web_check_done)

        # Window: frameless, always on top, no taskbar entry (Tool flag)
        self.setWindowFlags(
            Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool
        )
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setFixedSize(WIDGET_WIDTH, WIDGET_HEIGHT)
        self.setWindowTitle("Spotify Player")

        # State
        self._drag_pos = None
        self._taskbar_y = 0
        self._last_title = None
        self._last_artist = None
        self._progress = 0.0        # 0.0–1.0 song progress
        self._duration = 0.0        # total song length in seconds
        self._seeking = False       # True while scrubbing the progress bar
        self._hovered = False       # background visible only on hover
        self._hidden_for_fullscreen = False  # True while hiding for a fullscreen app
        self._search_expanded = False  # True while search icon is visible
        self._search_mode = False      # True while in inline search mode
        self._search_loading = False   # True while a search API call is in-flight
        self._spinner_angle = 0        # rotation angle for loading spinner
        self._spinner_target = "search"  # which button the spinner is on
        self._play_loading = False     # True while checking Web API on play press
        self._spotify_active = False   # True when any source reports playback
        self._using_api_fallback = False  # True when using Web API instead of SMTC
        self._api_poll_interval = 5.0     # seconds between Web API polls
        self._last_api_poll = 0.0         # timestamp of last Web API attempt
        self._api_cached_info = None      # last successful Web API result
        self._api_cache_time = 0.0        # when the cached result was fetched

        # Pre-build the two toggle icons
        self._icon_play = svg_to_icon(ICON_PLAY, ICON_SIZE)
        self._icon_pause = svg_to_icon(ICON_PAUSE, ICON_SIZE)
        self._icon_search = svg_to_icon(ICON_SEARCH, ICON_SIZE)

        # Pre-render the loading spinner base pixmap (rotated cheaply each frame)
        _lr_size = ICON_SIZE * 4
        _lr_renderer = QSvgRenderer(QByteArray(ICON_LOADING.encode()))
        self._spinner_base = QPixmap(_lr_size, _lr_size)
        self._spinner_base.fill(Qt.transparent)
        _lr_p = QPainter(self._spinner_base)
        _lr_p.setRenderHint(QPainter.Antialiasing)
        _lr_p.setRenderHint(QPainter.SmoothPixmapTransform)
        _lr_renderer.render(_lr_p)
        _lr_p.end()

        # Spinner timer (rotates the loading icon on search or play button)
        self._spinner_timer = QTimer(self)
        self._spinner_timer.setInterval(30)
        self._spinner_timer.timeout.connect(self._spin_icon)

        # Media controller (talks to Windows SMTC on a background thread)
        self.media = MediaController()

        # Spotify API (for search & play — lazy login, no browser until first use)
        self._spotify_auth = SpotifyAuth()
        self._spotify_api = SpotifyAPI(self._spotify_auth)
        self._search_popup = None

        # Build the visual layout
        self._build_ui()
        self._setup_search_animations()

        # Position on the taskbar
        self._position_on_taskbar()

        # Re-position when screen geometry changes (resolution change, laptop wake, etc.)
        QApplication.primaryScreen().geometryChanged.connect(self._position_on_taskbar)

        # System tray icon
        self._setup_tray()

        # Poll every 1 second for song changes
        self._poll_timer = QTimer(self)
        self._poll_timer.timeout.connect(self._update_media_info)
        self._poll_timer.start(1000)

        # First update after the window has been laid out
        QTimer.singleShot(100, self._update_media_info)

        # Instantly re-raise whenever another window takes focus (e.g. taskbar click)
        self._setup_foreground_hook()

        # Fallback timer in case the hook misses anything
        self._raise_timer = QTimer(self)
        self._raise_timer.timeout.connect(self._ensure_on_top)
        self._raise_timer.start(250)

        # Clean up resources on actual app quit (tray → Quit)
        QApplication.instance().aboutToQuit.connect(self._on_app_quit)

    # ── UI construction ───────────────────────────────────────

    def _build_ui(self):
        root = QHBoxLayout(self)
        root.setContentsMargins(PADDING, PADDING, PADDING, PADDING)
        root.setSpacing(SPACING)

        # Search button (hidden by default, slides in from left on hover)
        self.btn_search = self._make_btn(
            ICON_SEARCH, BUTTON_SIZE, ICON_SIZE, BUTTON_STYLE, self._toggle_search
        )
        self.btn_search.setMinimumWidth(0)
        self.btn_search.setMaximumWidth(0)
        root.addWidget(self.btn_search)

        # Content area: stacked widget swaps between player view and search input.
        # Same fixed space — playback buttons never move.
        self._content_stack = QStackedWidget()
        self._content_stack.setStyleSheet("background: transparent;")

        # Page 0: player view (album art + title/artist)
        self._player_page = QWidget()
        self._player_page.setStyleSheet("background: transparent;")
        player_row = QHBoxLayout(self._player_page)
        player_row.setContentsMargins(0, 0, 0, 0)
        player_row.setSpacing(SPACING)

        self.art_label = QLabel()
        self.art_label.setFixedSize(ART_SIZE, ART_SIZE)
        self.art_label.setAlignment(Qt.AlignCenter)
        self.art_label.setStyleSheet(
            "background: rgba(255,255,255,8); border-radius: 4px;"
        )
        player_row.addWidget(self.art_label)

        self._text_container = QWidget()
        self._text_container.setStyleSheet("background: transparent;")
        text_col = QVBoxLayout(self._text_container)
        text_col.setSpacing(1)
        text_col.setContentsMargins(2, 0, 0, 0)

        self.title_label = QLabel("No music playing")
        self.title_label.setStyleSheet(NO_MUSIC_STYLE)
        self.title_label.setFont(self._make_font(TITLE_SIZE, bold=True))

        self.artist_label = QLabel("")
        self.artist_label.setStyleSheet(ARTIST_STYLE)
        self.artist_label.setFont(self._make_font(ARTIST_SIZE))

        text_col.addStretch()
        text_col.addWidget(self.title_label)
        text_col.addWidget(self.artist_label)
        text_col.addStretch()
        player_row.addWidget(self._text_container, 1)

        self._content_stack.addWidget(self._player_page)   # index 0

        # Page 1: search input
        self._search_input = QLineEdit()
        self._search_input.setPlaceholderText("Search...")
        self._search_input.setFont(self._make_font(TITLE_SIZE))
        self._search_input.setStyleSheet(f"""
            QLineEdit {{
                background: transparent;
                border: none;
                color: {TEXT_COLOR};
                padding: 2px 4px;
                selection-background-color: #1ED760;
            }}
        """)
        self._content_stack.addWidget(self._search_input)  # index 1

        root.addWidget(self._content_stack, 1)

        # Playback buttons
        self.btn_prev = self._make_btn(
            ICON_PREV, BUTTON_SIZE, ICON_SIZE, BUTTON_STYLE, self._on_prev
        )
        self.btn_play = self._make_btn(
            ICON_PLAY, BUTTON_SIZE, ICON_SIZE, BUTTON_STYLE, self._on_play_pause
        )
        self.btn_next = self._make_btn(
            ICON_NEXT, BUTTON_SIZE, ICON_SIZE, BUTTON_STYLE, self._on_next
        )
        root.addWidget(self.btn_prev)
        root.addWidget(self.btn_play)
        root.addWidget(self.btn_next)

    def _make_btn(self, svg, btn_size, icon_size, style, callback):
        btn = QPushButton()
        btn.setFixedSize(btn_size, btn_size)
        btn.setIcon(svg_to_icon(svg, icon_size))
        btn.setIconSize(QSize(icon_size, icon_size))
        btn.setStyleSheet(style)
        btn.setCursor(Qt.PointingHandCursor)
        btn.clicked.connect(callback)
        return btn

    @staticmethod
    def _make_font(px_size, bold=False):
        font = QFont(FONT_FAMILY)
        font.setPixelSize(px_size)
        if bold:
            font.setWeight(QFont.DemiBold)
        return font

    # ── search icon animation ──────────────────────────────────

    def _setup_search_animations(self):
        """Create the expand/collapse animation for the search icon."""
        self._anim_btn = QPropertyAnimation(self.btn_search, b"maximumWidth")
        self._anim_btn.setDuration(200)
        self._anim_btn.setEasingCurve(QEasingCurve.InOutCubic)

    def _expand_search(self):
        """Slide the search icon in from the left, pushing art/text right."""
        if self._search_expanded or not self._spotify_active:
            return
        self._search_expanded = True

        self._anim_btn.stop()
        self._anim_btn.setStartValue(self.btn_search.maximumWidth())
        self._anim_btn.setEndValue(BUTTON_SIZE)
        self._anim_btn.start()

    def _collapse_search(self):
        """Slide the search icon out — unless search popup is open."""
        if not self._search_expanded:
            return
        # Keep expanded while search popup is visible
        if self._search_popup and self._search_popup.isVisible():
            return
        self._search_expanded = False

        self._anim_btn.stop()
        self._anim_btn.setStartValue(self.btn_search.maximumWidth())
        self._anim_btn.setEndValue(0)
        self._anim_btn.start()

    # ── play/pause with Spotify launch ────────────────────────

    def _on_play_pause(self):
        """Toggle play/pause. SMTC first, Web API sync, launch as last resort."""
        # Already in API fallback mode — control via Web API
        if self._using_api_fallback and self._spotify_auth.is_authenticated():
            if self._api_cached_info and self._api_cached_info["is_playing"]:
                self._spotify_api.pause()
                self._api_cached_info["is_playing"] = False
            else:
                self._spotify_api.resume()
                if self._api_cached_info:
                    self._api_cached_info["is_playing"] = True
            # Update button icon immediately — don't wait for next poll
            is_playing = (self._api_cached_info or {}).get("is_playing", False)
            self.btn_play.setIcon(
                self._icon_pause if is_playing else self._icon_play
            )
            self._api_cache_time = time.time()
            # Let optimistic state hold — don't poll until Spotify catches up
            self._last_api_poll = time.time()
            return

        # Try SMTC (desktop Spotify)
        if self.media.play_pause():
            return

        # No desktop session — check Web API on a background thread
        if self._spotify_auth.is_authenticated():
            self._set_play_loading(True)
            threading.Thread(target=self._check_web_playback, daemon=True).start()
            return

        # Nothing anywhere — launch Spotify Desktop
        self._launch_spotify()

    def _set_play_loading(self, loading):
        """Toggle spinner on the play button."""
        self._play_loading = loading
        if loading:
            self._spinner_angle = 0
            self._spinner_target = "play"
            self._spinner_timer.start()
        else:
            if self._spinner_target == "play":
                self._spinner_timer.stop()
            # Restore the correct play/pause icon
            is_playing = (self._api_cached_info or {}).get("is_playing", False)
            self.btn_play.setIcon(
                self._icon_pause if is_playing else self._icon_play
            )

    def _check_web_playback(self):
        """Background thread: check Web API, then update UI via signal."""
        try:
            info = self._spotify_api.get_currently_playing()
        except Exception:
            info = None
        self._web_check_done.emit(info)

    def _on_web_check_done(self, info):
        """Main thread: handle the Web API result."""
        self._set_play_loading(False)
        if info:
            # Found web playback — sync with it
            self._using_api_fallback = True
            self._api_cached_info = info
            self._api_cache_time = time.time()
            self._last_api_poll = time.time()
            self._apply_media_info(info)
            if not info["is_playing"]:
                self._spotify_api.resume()
            return
        # Nothing on web either — launch Spotify Desktop
        self._launch_spotify()

    def _on_prev(self):
        """Previous track. SMTC or Web API depending on active source."""
        if self._using_api_fallback and self._spotify_auth.is_authenticated():
            self._spotify_api.prev_track()
            # Give Spotify 2s to change tracks before polling
            self._last_api_poll = time.time() - self._api_poll_interval + 2
        else:
            self.media.prev_track()

    def _on_next(self):
        """Next track. SMTC or Web API depending on active source."""
        if self._using_api_fallback and self._spotify_auth.is_authenticated():
            self._spotify_api.next_track()
            # Give Spotify 2s to change tracks before polling
            self._last_api_poll = time.time() - self._api_poll_interval + 2
        else:
            self.media.next_track()

    def _launch_spotify(self):
        """Launch Spotify exe directly."""
        spotify_exe = os.path.join(
            os.environ.get("APPDATA", ""), "Spotify", "Spotify.exe"
        )
        if os.path.exists(spotify_exe):
            subprocess.Popen([spotify_exe])
        else:
            subprocess.Popen(
                ["explorer.exe", "spotify:"],
                creationflags=subprocess.CREATE_NO_WINDOW,
            )

    # ── search ───────────────────────────────────────────────

    def _toggle_search(self):
        """Switch between player mode and search mode."""
        if self._search_mode:
            self._exit_search_mode()
            return

        # Check if client ID is configured
        if not CLIENT_ID:
            from PySide6.QtWidgets import QMessageBox
            QMessageBox.information(
                self, "Setup Needed",
                "To use search, open spotify_auth.py and paste your\n"
                "Spotify Client ID. See README.md for instructions.",
            )
            return

        # If not logged in, start the login flow on a background thread
        if not self._spotify_auth.is_authenticated():
            self._start_login()
            return

        self._enter_search_mode()

    def _enter_search_mode(self):
        """Transform the widget into a search box."""
        self._search_mode = True

        # Swap to search page — same space, buttons don't move
        self._search_input.clear()
        self._content_stack.setCurrentIndex(1)
        self._search_input.setFocus()

        # Create the results popup but don't show it yet — it appears when results arrive
        self._search_popup = SearchPopup(self, self._spotify_api, inline=True)
        self._search_popup.closed.connect(self._on_search_popup_closed)
        self._search_popup.search_started.connect(self._on_search_started)
        self._search_popup.search_finished.connect(self._on_search_finished)

        # Wire inline input → popup search
        self._search_input.textChanged.connect(self._on_inline_search_text)
        self._search_input.returnPressed.connect(
            lambda: self._search_popup.search_text(self._search_input.text())
            if self._search_popup else None
        )

        # Close search when user clicks anywhere else (focus leaves widget + popup)
        QApplication.instance().focusChanged.connect(self._on_focus_changed)

    def _on_inline_search_text(self, text):
        """Forward keystrokes from inline input to the search popup."""
        if self._search_popup:
            self._search_popup.search_text(text)

    def _on_focus_changed(self, old, new):
        """Close search mode when focus leaves the widget and popup."""
        if not self._search_mode:
            return
        # Don't close while a play is in progress (waiting for device, etc.)
        if (self._search_popup and self._search_popup._play_worker
                and self._search_popup._play_worker.isRunning()):
            return
        # If focus went to anything inside our widget or the popup, keep search open
        # (let the widget's own click handlers decide what to do)
        if new and (new is self or self.isAncestorOf(new) or
                    (self._search_popup and
                     (new is self._search_popup or self._search_popup.isAncestorOf(new)))):
            return
        # Focus left entirely — close search
        self._exit_search_mode()

    def _exit_search_mode(self):
        """Restore the widget to player mode."""
        if not self._search_mode:
            return
        self._search_mode = False

        # Stop loading spinner
        self._set_search_loading(False)

        # Disconnect inline input signals
        try:
            self._search_input.textChanged.disconnect(self._on_inline_search_text)
        except RuntimeError:
            pass
        try:
            QApplication.instance().focusChanged.disconnect(self._on_focus_changed)
        except RuntimeError:
            pass

        # Close popup if open
        if self._search_popup and self._search_popup.isVisible():
            self._search_popup.close()

        # Swap back to player page
        self._search_input.clear()
        self._content_stack.setCurrentIndex(0)

    def keyPressEvent(self, event):
        if event.key() == Qt.Key_Escape and self._search_mode:
            self._exit_search_mode()
            return
        super().keyPressEvent(event)

    def _start_login(self):
        """Run the OAuth login on a background thread so the UI doesn't freeze."""
        from PySide6.QtCore import QThread, Signal

        class _LoginWorker(QThread):
            done = Signal(bool)

            def __init__(self, auth):
                super().__init__()
                self._auth = auth

            def run(self):
                self.done.emit(self._auth.login())

        self._login_worker = _LoginWorker(self._spotify_auth)
        self._login_worker.done.connect(self._on_login_done)
        self._login_worker.start()

    def _on_login_done(self, success):
        if success:
            self._enter_search_mode()

    def _on_search_popup_closed(self):
        """When the search popup closes, exit search mode and collapse icon."""
        self._search_popup = None
        if self._search_mode:
            self._exit_search_mode()
        if not self._hovered:
            self._collapse_search()

    # ── search loading spinner ─────────────────────────────────

    def _on_search_started(self):
        """Called when the search popup begins an API call."""
        self._set_search_loading(True)

    def _on_search_finished(self):
        """Called when the search popup finishes an API call."""
        self._set_search_loading(False)

    def _set_search_loading(self, loading):
        """Toggle the spinning search icon."""
        self._search_loading = loading
        if loading:
            self._spinner_angle = 0
            self._spinner_target = "search"
            self._spinner_timer.start()
        else:
            self._spinner_timer.stop()
            self.btn_search.setIcon(self._icon_search)

    def _spin_icon(self):
        """Rotate the cached loading pixmap and apply to the active target button."""
        self._spinner_angle = (self._spinner_angle + 8) % 360
        t = QTransform()
        sz = self._spinner_base.width()
        t.translate(sz / 2, sz / 2)
        t.rotate(self._spinner_angle)
        t.translate(-sz / 2, -sz / 2)
        rotated = self._spinner_base.transformed(t, Qt.SmoothTransformation)
        # transformed() can change size due to rotation; crop back to original
        if rotated.size() != self._spinner_base.size():
            cx = (rotated.width() - sz) // 2
            cy = (rotated.height() - sz) // 2
            rotated = rotated.copy(cx, cy, sz, sz)
        icon = QIcon(rotated)
        if self._spinner_target == "play":
            self.btn_play.setIcon(icon)
        else:
            self.btn_search.setIcon(icon)

    # ── system tray ───────────────────────────────────────────

    def _setup_tray(self):
        self._tray = QSystemTrayIcon(self._create_tray_icon(), self)
        self._tray.setToolTip("Spotify Player")

        menu = QMenu()
        menu.addAction("Show / Hide", self._toggle_visibility)
        menu.addSeparator()

        self._autostart_action = QAction("Start with Windows", self)
        self._autostart_action.setCheckable(True)
        self._autostart_action.setChecked(_is_autostart_enabled())
        self._autostart_action.triggered.connect(self._toggle_autostart)
        menu.addAction(self._autostart_action)

        menu.addSeparator()
        menu.addAction("Relaunch", self._relaunch)
        menu.addAction("Quit", QApplication.quit)

        self._tray.setContextMenu(menu)
        self._tray.activated.connect(self._on_tray_click)
        self._tray.show()

    @staticmethod
    def _create_tray_icon():
        """Green circle with a white play triangle — looks like a mini Spotify."""
        pixmap = QPixmap(64, 64)
        pixmap.fill(Qt.transparent)
        p = QPainter(pixmap)
        p.setRenderHint(QPainter.Antialiasing)
        # Green circle
        p.setBrush(QColor(30, 215, 96))
        p.setPen(Qt.NoPen)
        p.drawEllipse(4, 4, 56, 56)
        # White play triangle
        p.setBrush(QColor(255, 255, 255))
        p.drawPolygon(QPolygon([QPoint(26, 16), QPoint(26, 48), QPoint(48, 32)]))
        p.end()
        return QIcon(pixmap)

    def _on_tray_click(self, reason):
        if reason == QSystemTrayIcon.ActivationReason.Trigger:
            self._toggle_visibility()

    def _toggle_visibility(self):
        if self.isVisible():
            if self._search_mode:
                self._exit_search_mode()
            self.hide()
        else:
            self._position_on_taskbar()  # recalculate in case screen changed
            self.show()
            self._ensure_on_top()

    def _relaunch(self):
        """Restart the application."""
        script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "main.py")
        subprocess.Popen(
            [sys.executable, script],
            cwd=os.path.dirname(script),
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        QApplication.quit()

    def _toggle_autostart(self, checked):
        _set_autostart(checked)

    def _on_app_quit(self):
        """Clean up all resources when the app is actually quitting."""
        # Unhook the Windows foreground event hook
        if getattr(self, '_winevent_hook', None):
            ctypes.windll.user32.UnhookWinEvent(self._winevent_hook)
            self._winevent_hook = None

        # Stop all timers
        self._spinner_timer.stop()
        self._poll_timer.stop()
        self._raise_timer.stop()

        # Safety disconnect focusChanged (in case search mode is active)
        try:
            QApplication.instance().focusChanged.disconnect(self._on_focus_changed)
        except (RuntimeError, TypeError):
            pass

        # Stop the media controller's event loop and thread
        if hasattr(self, 'media'):
            self.media.stop()

        # Wait for login worker if in progress
        if getattr(self, '_login_worker', None) and self._login_worker.isRunning():
            self._login_worker.wait(3000)

    def closeEvent(self, event):
        """X button (or close()) hides to tray instead of quitting."""
        if self._search_mode:
            self._exit_search_mode()
        event.ignore()
        self.hide()

    def showEvent(self, event):
        """Resume polling and z-order timers when the widget becomes visible."""
        super().showEvent(event)
        if not self._poll_timer.isActive():
            self._poll_timer.start(1000)
        if not self._raise_timer.isActive():
            self._raise_timer.start(250)

    def hideEvent(self, event):
        """Pause polling and z-order timers when the widget is hidden to save CPU."""
        super().hideEvent(event)
        # Don't stop timers during fullscreen hiding — _raise_timer drives
        # fullscreen-end detection and must keep running
        if not self._hidden_for_fullscreen:
            self._poll_timer.stop()
            self._raise_timer.stop()

    # ── hover: transparent ↔ solid background + search animation ──

    def enterEvent(self, event):
        self._hovered = True
        self._expand_search()
        self.update()

    def leaveEvent(self, event):
        if not self._seeking:      # don't flicker while scrubbing
            self._hovered = False
            self._collapse_search()
            self.update()

    # ── taskbar positioning ───────────────────────────────────

    _CONFIG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")

    def _position_on_taskbar(self):
        """Place the widget centered vertically ON the taskbar."""
        screen = QApplication.primaryScreen()
        full = screen.geometry()
        avail = screen.availableGeometry()
        taskbar_h = full.height() - avail.height()
        taskbar_top = avail.bottom() + 1

        self._taskbar_y = taskbar_top + (taskbar_h - self.height()) // 2

        # Restore saved x position, or default to right side
        x = full.right() - self.width() - 160
        try:
            with open(self._CONFIG_FILE) as f:
                x = json.load(f).get("x", x)
        except (FileNotFoundError, json.JSONDecodeError, KeyError):
            pass

        # Clamp x to visible screen bounds (handles resolution changes,
        # external monitor disconnect, etc.)
        x = max(full.left(), min(x, full.right() - self.width()))

        self.move(x, self._taskbar_y)

    def _save_position(self):
        """Persist the current x position so it survives restarts."""
        try:
            with open(self._CONFIG_FILE, "w") as f:
                json.dump({"x": self.x()}, f)
        except OSError:
            pass

    def _ensure_on_top(self):
        """Hide the widget during fullscreen, show + re-raise otherwise."""
        try:
            if self._is_fullscreen_active():
                if not self._hidden_for_fullscreen:
                    self._hidden_for_fullscreen = True
                    self.hide()
            else:
                if self._hidden_for_fullscreen:
                    self._hidden_for_fullscreen = False
                    self.show()
                if self.isVisible():
                    hwnd = int(self.winId())
                    SWP = 0x0002 | 0x0001 | 0x0010  # NOMOVE|NOSIZE|NOACTIVATE
                    ctypes.windll.user32.SetWindowPos(
                        hwnd, ctypes.c_void_p(-1),  # HWND_TOPMOST
                        0, 0, 0, 0, SWP,
                    )
        except Exception:
            pass

    # ── fullscreen detection constants ───────────────────────

    class _MONITORINFO(ctypes.Structure):
        _fields_ = [
            ("cbSize", wintypes.DWORD),
            ("rcMonitor", wintypes.RECT),
            ("rcWork", wintypes.RECT),
            ("dwFlags", wintypes.DWORD),
        ]

    def _is_fullscreen_active(self):
        """True if the foreground window is a real fullscreen app (video, game, etc.).

        Two-pronged detection:
        1. SHQueryUserNotificationState — catches D3D exclusive fullscreen (games).
        2. GetWindowRect EXACT match against monitor rect + window style check.
           On Windows 11, maximized windows overshoot the monitor by ~7px
           (invisible resize borders), so their rect does NOT match exactly.
           True fullscreen windows match exactly AND lack WS_CAPTION / WS_THICKFRAME.
        """
        try:
            # ── Quick check: D3D exclusive fullscreen / presentation mode ──
            state = ctypes.c_int(0)
            if ctypes.windll.shell32.SHQueryUserNotificationState(
                    ctypes.byref(state)) == 0:
                if state.value in (2, 3, 4):   # BUSY, D3D_FULLSCREEN, PRESENTATION
                    return True

            # ── Main check: foreground window analysis ──
            fg = ctypes.windll.user32.GetForegroundWindow()
            if not fg or fg == int(self.winId()):
                return False

            # Ignore desktop and taskbar windows
            cls = ctypes.create_unicode_buffer(256)
            ctypes.windll.user32.GetClassNameW(fg, cls, 256)
            if cls.value in ("Progman", "WorkerW", "Shell_TrayWnd",
                             "Shell_SecondaryTrayWnd"):
                return False

            # Get window rect (INCLUDES invisible borders for maximized windows)
            win_rect = wintypes.RECT()
            ctypes.windll.user32.GetWindowRect(fg, ctypes.byref(win_rect))

            # Get monitor rect for the monitor this window is on
            hmon = ctypes.windll.user32.MonitorFromWindow(fg, 2)  # DEFAULTTONEAREST
            mi = self._MONITORINFO()
            mi.cbSize = ctypes.sizeof(self._MONITORINFO)
            ctypes.windll.user32.GetMonitorInfoW(hmon, ctypes.byref(mi))
            mon = mi.rcMonitor

            # Check 1: window rect must EXACTLY match monitor rect.
            # Maximized windows overshoot by ~7px → they fail here.
            if (win_rect.left != mon.left or win_rect.top != mon.top
                    or win_rect.right != mon.right
                    or win_rect.bottom != mon.bottom):
                return False

            # Check 2: window must lack normal chrome (caption / sizing border).
            # Fullscreen apps strip these; maximized windows keep them.
            style = ctypes.windll.user32.GetWindowLongW(fg, -16)  # GWL_STYLE
            if style & 0x00C00000 or style & 0x00040000:  # WS_CAPTION | WS_THICKFRAME
                return False

            return True
        except Exception:
            return False

    def _setup_foreground_hook(self):
        """
        Windows event hook: fires instantly whenever ANY window comes to
        the foreground (e.g. user clicks the taskbar).  We respond by
        re-raising our widget so it's never hidden behind the taskbar.
        """
        WinEventProcType = ctypes.WINFUNCTYPE(
            None,
            wintypes.HANDLE,   # hWinEventHook
            wintypes.DWORD,    # event
            wintypes.HWND,     # hwnd
            ctypes.c_long,     # idObject
            ctypes.c_long,     # idChild
            wintypes.DWORD,    # idEventThread
            wintypes.DWORD,    # dwmsEventTime
        )

        def on_foreground_change(hook, event, hwnd, obj, child, tid, time):
            self._ensure_on_top()

        # prevent garbage collection of the callback
        self._winevent_cb = WinEventProcType(on_foreground_change)

        EVENT_SYSTEM_FOREGROUND = 0x0003
        self._winevent_hook = ctypes.windll.user32.SetWinEventHook(
            EVENT_SYSTEM_FOREGROUND,
            EVENT_SYSTEM_FOREGROUND,
            0,
            self._winevent_cb,
            0, 0,
            0x0000,   # WINEVENT_OUTOFCONTEXT
        )

    # ── drag left/right on taskbar + click/drag-to-seek ──────

    _SEEK_ZONE = 12   # bottom N pixels = seek area

    def mousePressEvent(self, event):
        if event.button() != Qt.LeftButton:
            return

        # Bottom strip → start seeking (immediate feedback on click)
        if event.position().y() >= self.height() - self._SEEK_ZONE and self._duration > 0:
            self._seeking = True
            frac = max(0.0, min(1.0, event.position().x() / self.width()))
            self._progress = frac
            self.media.seek(frac * self._duration)
            self.update()
            return

        # Everything else → drag widget
        self._seeking = False
        self._drag_pos = event.globalPosition().toPoint() - self.pos()

    def mouseMoveEvent(self, event):
        if not (event.buttons() & Qt.LeftButton):
            return

        # Scrubbing the progress bar (visual update only — seek on release)
        if self._seeking and self._duration > 0:
            frac = max(0.0, min(1.0, event.position().x() / self.width()))
            self._progress = frac
            self.update()
            return

        # Dragging the widget
        if self._drag_pos:
            new_pos = event.globalPosition().toPoint() - self._drag_pos
            new_pos.setY(self._taskbar_y)
            self.move(new_pos)

    def mouseReleaseEvent(self, event):
        # If we were scrubbing, send the final seek position
        if self._seeking and self._duration > 0:
            frac = max(0.0, min(1.0, event.position().x() / self.width()))
            self._progress = frac
            if self._using_api_fallback:
                self._spotify_api.seek(frac * self._duration)
                # Update cache so interpolation starts from the new position
                if self._api_cached_info is not None:
                    self._api_cached_info["position"] = frac * self._duration
                    self._api_cache_time = time.time()
            else:
                self.media.seek(frac * self._duration)
            self.update()
        # If we were dragging, save the new position
        elif self._drag_pos:
            self._save_position()
        self._seeking = False
        self._drag_pos = None

    # ── media polling ─────────────────────────────────────────

    def _update_media_info(self):
        try:
            info = self.media.get_media_info()
        except Exception:
            info = None

        smtc_provided = info is not None

        # SMTC found desktop Spotify — exit API fallback only if
        # desktop is actively playing. An idle/stale SMTC session
        # (e.g. Spotify open but not playing) must not override web data.
        if smtc_provided and self._using_api_fallback:
            if info.get("is_playing"):
                self._using_api_fallback = False
                self._api_cached_info = None
            else:
                # Ignore idle SMTC — keep using web API
                smtc_provided = False
                info = None

        # API fallback mode: user synced via play button, poll Web API at 5s
        if not smtc_provided and self._using_api_fallback:
            now = time.time()
            if now - self._last_api_poll >= self._api_poll_interval:
                self._last_api_poll = now
                try:
                    api_info = self._spotify_api.get_currently_playing()
                    if api_info is not None:
                        self._api_cached_info = api_info
                        self._api_cache_time = now
                    else:
                        # Web playback ended — exit fallback mode
                        self._using_api_fallback = False
                        self._api_cached_info = None
                    info = api_info
                except Exception:
                    pass
            elif self._api_cached_info is not None:
                # Between API polls: interpolate progress from last known state
                info = dict(self._api_cached_info)
                elapsed = now - self._api_cache_time
                if info["is_playing"] and info["duration"] > 0:
                    info["position"] = min(
                        info["position"] + elapsed, info["duration"]
                    )

        if info is None:
            self._spotify_active = False
            if self._last_title is not None:
                self.title_label.setText("No music playing")
                self.title_label.setStyleSheet(NO_MUSIC_STYLE)
                self.artist_label.setText("")
                self._set_default_art()
                self._last_title = None
                self._last_artist = None
                self._progress = 0.0
                self._duration = 0.0
                self.update()
            return

        self._apply_media_info(info)

    def _apply_media_info(self, info):
        """Update the widget display from a media info dict (SMTC or Web API)."""
        self._spotify_active = True

        # Progress bar (updates every poll even if song hasn't changed)
        self._duration = info["duration"]
        self._progress = (
            info["position"] / info["duration"]
            if info["duration"] > 0 else 0.0
        )
        self.update()   # trigger repaint for progress bar

        self.btn_play.setIcon(
            self._icon_pause if info["is_playing"] else self._icon_play
        )

        if info["title"] == self._last_title and info["artist"] == self._last_artist:
            return

        self._last_title = info["title"]
        self._last_artist = info["artist"]

        self.title_label.setText(self._elide(self.title_label, info["title"]))
        self.title_label.setToolTip(info["title"])
        self.title_label.setStyleSheet(TITLE_STYLE)

        self.artist_label.setText(self._elide(self.artist_label, info["artist"]))
        self.artist_label.setToolTip(info["artist"])

        if info["thumbnail"]:
            pm = QPixmap()
            pm.loadFromData(info["thumbnail"])
            if not pm.isNull():
                scaled = pm.scaled(
                    ART_SIZE, ART_SIZE,
                    Qt.KeepAspectRatioByExpanding,
                    Qt.SmoothTransformation,
                )
                self.art_label.setPixmap(self._round_pixmap(scaled, 4))
                return
        self._set_default_art()

    @staticmethod
    def _elide(label, text):
        width = label.width() or 110
        return label.fontMetrics().elidedText(text, Qt.ElideRight, width)

    # ── album art helpers ─────────────────────────────────────

    def _set_default_art(self):
        pm = QPixmap(ART_SIZE, ART_SIZE)
        pm.fill(QColor(50, 50, 50))
        p = QPainter(pm)
        p.setPen(QColor(120, 120, 120))
        p.setFont(QFont(FONT_FAMILY, 14))
        p.drawText(pm.rect(), Qt.AlignCenter, "\u266b")
        p.end()
        self.art_label.setPixmap(self._round_pixmap(pm, 4))

    @staticmethod
    def _round_pixmap(src, radius):
        out = QPixmap(src.size())
        out.fill(Qt.transparent)
        p = QPainter(out)
        p.setRenderHint(QPainter.Antialiasing)
        path = QPainterPath()
        path.addRoundedRect(0, 0, src.width(), src.height(), radius, radius)
        p.setClipPath(path)
        p.drawPixmap(0, 0, src)
        p.end()
        return out

    # ── painting ──────────────────────────────────────────────

    def _widget_shape(self, rect):
        """Rounded top corners, straight bottom edge."""
        r = CORNER_RADIUS
        x, y, w, h = rect.x(), rect.y(), rect.width(), rect.height()
        path = QPainterPath()
        path.moveTo(x, y + h)                                         # bottom-left
        path.lineTo(x, y + r)                                         # up left side
        path.arcTo(x, y, 2 * r, 2 * r, 180, -90)                     # top-left round
        path.lineTo(x + w - r, y)                                     # across top
        path.arcTo(x + w - 2 * r, y, 2 * r, 2 * r, 90, -90)          # top-right round
        path.lineTo(x + w, y + h)                                     # down right side
        path.closeSubpath()                                            # straight bottom
        return path

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)

        rect = self.rect().adjusted(1, 1, -1, -1)
        shape = self._widget_shape(rect)

        # Background: solid when hovered, nearly invisible otherwise
        if self._hovered:
            p.setBrush(QColor(*BG_COLOR))
            p.setPen(QColor(*BORDER_COLOR))
        else:
            p.setBrush(QColor(0, 0, 0, 1))
            p.setPen(Qt.NoPen)
        p.drawPath(shape)

        # Progress bar (thin line at the very bottom)
        if self._duration > 0:
            p.setClipPath(shape)
            p.setPen(Qt.NoPen)

            bar_h = 3
            bar_y = self.height() - bar_h - 1

            # Gray track
            p.setBrush(QColor(255, 255, 255, 20))
            p.drawRect(0, bar_y, self.width(), bar_h)

            # Green fill
            if self._progress > 0:
                p.setBrush(QColor(30, 215, 96))
                p.drawRect(0, bar_y, int(self.width() * self._progress), bar_h)
