"""
Search popup — appears above the taskbar widget.
Type to search, click a result to play it.
"""

import ctypes
import ctypes.wintypes as wintypes

from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit, QPushButton,
    QScrollArea, QFrame,
)
from PySide6.QtCore import Qt, QTimer, QSize, QByteArray, QUrl, Signal, QThread
from PySide6.QtGui import (
    QPainter, QColor, QPainterPath, QFont, QPixmap, QIcon, QKeyEvent,
)
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtNetwork import QNetworkAccessManager, QNetworkRequest, QNetworkReply

from styles import (
    FONT_FAMILY, TEXT_COLOR, SUBTEXT_COLOR, BG_COLOR,
    ICON_SEARCH, ICON_CLOSE, SEARCH_POPUP_WIDTH,
    SEARCH_RESULT_HEIGHT, SEARCH_ART_SIZE,
)


# -- Windows 11 acrylic blur ------------------------------------------------

class _ACCENT_POLICY(ctypes.Structure):
    _fields_ = [
        ("AccentState", ctypes.c_int),
        ("AccentFlags", ctypes.c_int),
        ("GradientColor", ctypes.c_uint),
        ("AnimationId", ctypes.c_int),
    ]

class _WINCOMPATTRDATA(ctypes.Structure):
    _fields_ = [
        ("Attribute", ctypes.c_int),
        ("Data", ctypes.POINTER(_ACCENT_POLICY)),
        ("SizeOfData", ctypes.c_size_t),
    ]

def _enable_blur(hwnd, color_abgr=0xC0262626):
    """Enable acrylic blur-behind on a window (Windows 10 1803+ / Windows 11)."""
    accent = _ACCENT_POLICY()
    accent.AccentState = 4  # ACCENT_ENABLE_ACRYLICBLURBEHIND
    accent.AccentFlags = 2
    accent.GradientColor = color_abgr
    data = _WINCOMPATTRDATA()
    data.Attribute = 19  # WCA_ACCENT_POLICY
    data.Data = ctypes.pointer(accent)
    data.SizeOfData = ctypes.sizeof(accent)
    ctypes.windll.user32.SetWindowCompositionAttribute(hwnd, ctypes.byref(data))


# -- search worker (runs API call off the main thread) -------------------

class _SearchWorker(QThread):
    results_ready = Signal(list)
    more_ready = Signal(list)   # for "load more" appends
    error = Signal(str)

    def __init__(self, api, query, offset=0):
        super().__init__()
        self.api = api
        self.query = query
        self.offset = offset

    def run(self):
        tracks = self.api.search_tracks(self.query, offset=self.offset)

        if self.offset > 0:
            # Load-more request — just emit tracks, no playlists
            self.more_ready.emit(tracks or [])
            return

        playlists = self.api.get_my_playlists(self.query) or []

        if tracks is None and not playlists:
            self.error.emit("Search failed")
            return

        # Normalize playlist data to match track result format
        playlist_results = []
        for p in playlists[:3]:  # max 3 playlists
            playlist_results.append({
                "name": p["name"],
                "artists": f"Playlist \u2022 {p['track_count']} tracks",
                "album": "",
                "album_uri": "",
                "album_art_url": p.get("image_url"),
                "uri": p["uri"],
                "_type": "playlist",
            })

        # Playlists first, then tracks
        combined = playlist_results + (tracks or [])
        self.results_ready.emit(combined)


class _PlayWorker(QThread):
    finished = Signal(bool, str)  # success, error_message

    def __init__(self, api, uri, context_uri=None):
        super().__init__()
        self.api = api
        self.uri = uri
        self.context_uri = context_uri

    def run(self):
        ok, msg = self.api.play_track(self.uri, self.context_uri)
        self.finished.emit(ok, msg or "")


# -- search popup --------------------------------------------------------

class SearchPopup(QWidget):
    """Frameless search popup that floats above the player widget."""

    closed = Signal()

    def __init__(self, parent_widget, spotify_api, inline=False):
        super().__init__()
        self._parent_widget = parent_widget
        self._api = spotify_api
        self._inline = inline  # True = no header, driven by external input
        self._worker = None
        self._play_worker = None
        self._net = QNetworkAccessManager(self)
        self._last_query = ""
        self._track_offset = 0    # for pagination
        self._loading_more = False
        self._result_count = 0

        self.setWindowFlags(
            Qt.FramelessWindowHint
            | Qt.WindowStaysOnTopHint
            | Qt.Tool
        )
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setFixedWidth(SEARCH_POPUP_WIDTH)

        self._build_ui()
        if self._inline:
            self.setFixedHeight(0)
            self.hide()  # don't show until results arrive
        else:
            self.setFixedHeight(72)
            self._position_above_parent()

        # Debounce timer: waits 400ms after last keystroke before searching
        self._debounce = QTimer(self)
        self._debounce.setSingleShot(True)
        self._debounce.setInterval(400)
        self._debounce.timeout.connect(self._do_search)

    # -- UI construction -------------------------------------------------

    def _build_ui(self):
        self._main_layout = QVBoxLayout(self)
        self._main_layout.setContentsMargins(0, 4, 0, 4)
        self._main_layout.setSpacing(0)

        if not self._inline:
            # Header row: search input + close button (standalone mode only)
            header = QHBoxLayout()
            header.setSpacing(6)

            self._search_input = QLineEdit()
            self._search_input.setPlaceholderText("Search for a song...")
            self._search_input.setFont(QFont(FONT_FAMILY, 12))
            self._search_input.setMinimumHeight(36)
            self._search_input.setStyleSheet(f"""
                QLineEdit {{
                    background: rgba(255, 255, 255, 10);
                    border: 1px solid rgba(255, 255, 255, 20);
                    border-radius: 6px;
                    color: {TEXT_COLOR};
                    padding: 6px 12px;
                    selection-background-color: #1ED760;
                }}
                QLineEdit:focus {{
                    border-color: #1ED760;
                }}
            """)
            self._search_input.textChanged.connect(self._on_text_changed)
            self._search_input.returnPressed.connect(self._do_search)
            header.addWidget(self._search_input, 1)

            close_btn = QPushButton()
            close_btn.setFixedSize(28, 28)
            close_btn.setCursor(Qt.PointingHandCursor)
            close_btn.setIcon(self._svg_icon(ICON_CLOSE, 12))
            close_btn.setIconSize(QSize(12, 12))
            close_btn.setStyleSheet("""
                QPushButton {
                    background: transparent;
                    border: none;
                    border-radius: 14px;
                }
                QPushButton:hover {
                    background: rgba(255, 255, 255, 15);
                }
            """)
            close_btn.clicked.connect(self.close)
            header.addWidget(close_btn)

            self._main_layout.addLayout(header)

        # Results area (scrollable)
        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QFrame.NoFrame)
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self._scroll.setStyleSheet("""
            QScrollArea { background: transparent; border: none; }
            QScrollBar:vertical {
                background: transparent;
                width: 6px;
                margin: 0;
            }
            QScrollBar::handle:vertical {
                background: rgba(255, 255, 255, 30);
                border-radius: 3px;
                min-height: 20px;
            }
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {
                height: 0;
            }
        """)

        self._results_container = QWidget()
        self._results_container.setStyleSheet("background: transparent;")
        self._results_layout = QVBoxLayout(self._results_container)
        self._results_layout.setContentsMargins(0, 0, 0, 0)
        self._results_layout.setSpacing(0)
        self._results_layout.addStretch()
        self._scroll.setWidget(self._results_container)
        self._scroll.verticalScrollBar().valueChanged.connect(self._on_scroll)
        self._scroll.hide()
        self._main_layout.addWidget(self._scroll)

        # Status label (for errors / "no results")
        self._status_label = QLabel("")
        self._status_label.setFont(QFont(FONT_FAMILY, 10))
        self._status_label.setStyleSheet(f"color: {SUBTEXT_COLOR}; background: transparent;")
        self._status_label.setAlignment(Qt.AlignCenter)
        self._status_label.hide()
        self._main_layout.addWidget(self._status_label)

    # -- positioning -----------------------------------------------------

    def _position_above_parent(self):
        """Place the popup so its bottom edge sits just above the parent widget."""
        pw = self._parent_widget
        popup_x = pw.x() + (pw.width() - self.width()) // 2
        popup_y = pw.y() - self.height() - 4  # 4px gap above the widget
        self.move(popup_x, max(popup_y, 0))

    def _resize_to_fit(self, result_count):
        """Resize popup height based on number of results."""
        header_h = 0 if self._inline else 60
        status_h = 30 if self._status_label.isVisible() else 0

        if result_count > 0:
            results_h = min(result_count, 6) * (SEARCH_RESULT_HEIGHT + 2)
            total_h = header_h + results_h + status_h + 24  # margins
            self._scroll.setFixedHeight(results_h + 4)
        else:
            total_h = header_h + status_h + 24

        if self._inline and total_h <= 24:
            # Inline mode with no meaningful content — hide instead
            self.hide()
            return
        self.setFixedHeight(max(total_h, 10))
        self._position_above_parent()

    # -- search logic ----------------------------------------------------

    def _on_text_changed(self, text):
        self._debounce.start()  # restart 400ms timer

    def _do_search(self):
        self._debounce.stop()
        if self._inline:
            query = getattr(self, '_pending_query', '').strip()
        else:
            query = self._search_input.text().strip()
        if len(query) < 2:
            self._clear_results()
            return

        self._last_query = query
        self._track_offset = 0
        self._result_count = 0

        # Show loading state
        self._status_label.setText("Searching...")
        self._status_label.show()

        # Cancel previous search if still running
        if self._worker and self._worker.isRunning():
            self._worker.quit()

        self._worker = _SearchWorker(self._api, query)
        self._worker.results_ready.connect(self._show_results)
        self._worker.error.connect(self._show_error)
        self._worker.start()

    def _show_results(self, results):
        self._clear_results()

        if not results:
            self._status_label.setText("No results found")
            self._status_label.show()
            if not self.isVisible():
                self.show()
            self._resize_to_fit(0)
            return

        self._status_label.hide()
        self._scroll.show()

        # Count only track results for pagination offset
        track_count = sum(1 for r in results if r.get("_type") != "playlist")
        self._track_offset = track_count
        self._result_count = len(results)

        for track in results:
            item = _ResultItem(track, self._net)
            item.clicked.connect(self._play_track)
            self._results_layout.insertWidget(
                self._results_layout.count() - 1, item  # before the stretch
            )

        if not self.isVisible():
            self.show()
        self._resize_to_fit(len(results))

    def _on_scroll(self, value):
        """Load more results when scrolled to the bottom."""
        sb = self._scroll.verticalScrollBar()
        if value >= sb.maximum() - 5 and not self._loading_more and self._last_query:
            self._load_more()

    def _load_more(self):
        """Fetch the next page of track results."""
        self._loading_more = True
        if self._worker and self._worker.isRunning():
            return

        self._worker = _SearchWorker(self._api, self._last_query, offset=self._track_offset)
        self._worker.more_ready.connect(self._append_results)
        self._worker.start()

    def _append_results(self, tracks):
        """Append more track results to the existing list."""
        self._loading_more = False
        if not tracks:
            return

        self._track_offset += len(tracks)
        self._result_count += len(tracks)

        for track in tracks:
            item = _ResultItem(track, self._net)
            item.clicked.connect(self._play_track)
            self._results_layout.insertWidget(
                self._results_layout.count() - 1, item
            )

        self._resize_to_fit(self._result_count)

    def _show_error(self, message):
        self._clear_results()
        self._status_label.setText(message)
        self._status_label.show()
        self._resize_to_fit(0)

    def _clear_results(self):
        while self._results_layout.count() > 1:  # keep the stretch
            child = self._results_layout.takeAt(0)
            if child.widget():
                child.widget().deleteLater()
        self._scroll.hide()
        self._status_label.hide()
        if self._inline:
            self.hide()

    # -- playback --------------------------------------------------------

    def _play_track(self, uri, context_uri=None):
        self._status_label.setText("Playing...")
        self._status_label.show()

        if uri.startswith("spotify:playlist:"):
            # Playlist: play from beginning (uri IS the context)
            self._play_worker = _PlayWorker(self._api, None, uri)
        else:
            self._play_worker = _PlayWorker(self._api, uri, context_uri)
        self._play_worker.finished.connect(self._on_play_finished)
        self._play_worker.start()

    def _on_play_finished(self, success, error_msg):
        if success:
            self.close()
        else:
            self._status_label.setText(error_msg)
            self._status_label.show()

    # -- focus / keyboard ------------------------------------------------

    def focus_search_input(self):
        if not self._inline:
            self._search_input.setFocus()
        self.activateWindow()

    def search_text(self, text):
        """Called by the parent widget's inline input to trigger a search."""
        self._pending_query = text
        self._debounce.start()

    def keyPressEvent(self, event):
        if event.key() == Qt.Key_Escape:
            self.close()
            return
        super().keyPressEvent(event)

    def closeEvent(self, event):
        self.closed.emit()
        super().closeEvent(event)

    def focusOutEvent(self, event):
        # Close when clicking outside, but not when a child widget has focus
        if not self.isAncestorOf(QWidget.find(0) if False else None):
            pass  # Qt.Popup handles this; for Tool windows we keep it open
        super().focusOutEvent(event)

    # -- painting --------------------------------------------------------

    def showEvent(self, event):
        """Enable acrylic blur on first show."""
        super().showEvent(event)
        if not getattr(self, '_blur_enabled', False):
            self._blur_enabled = True
            try:
                # ABGR: alpha=0xC0(192), RGB=262626
                _enable_blur(int(self.winId()), 0xC0262626)
            except Exception:
                pass  # fallback to solid bg if blur unavailable

    def paintEvent(self, event):
        p = QPainter(self)
        p.setPen(Qt.NoPen)
        # Transparent — the acrylic blur provides the background.
        # Draw a subtle fallback tint in case blur isn't available.
        p.setBrush(QColor(38, 38, 38, 60))
        p.drawRect(self.rect())

    # -- helpers ---------------------------------------------------------

    @staticmethod
    def _svg_icon(svg_str, size):
        render_size = size * 4
        renderer = QSvgRenderer(QByteArray(svg_str.encode()))
        pixmap = QPixmap(render_size, render_size)
        pixmap.fill(Qt.transparent)
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.Antialiasing)
        renderer.render(painter)
        painter.end()
        return QIcon(pixmap)


# -- result item ---------------------------------------------------------

class _ResultItem(QWidget):
    """A single search result row with album art, title, and artist."""

    clicked = Signal(str, str)  # emits (track_uri, album_uri) on click

    def __init__(self, track_data, network_manager):
        super().__init__()
        self._uri = track_data["uri"]
        self._is_playlist = track_data.get("_type") == "playlist"
        self._context_uri = "" if self._is_playlist else track_data.get("album_uri", "")
        self._hovered = False
        self.setCursor(Qt.PointingHandCursor)
        self.setFixedHeight(SEARCH_RESULT_HEIGHT)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(8, 4, 8, 4)
        layout.setSpacing(10)

        # Album art
        self._art_label = QLabel()
        self._art_label.setFixedSize(SEARCH_ART_SIZE, SEARCH_ART_SIZE)
        self._art_label.setStyleSheet(
            "background: rgba(255,255,255,5); border-radius: 4px;"
        )
        self._art_label.setAlignment(Qt.AlignCenter)
        layout.addWidget(self._art_label)

        # Text column
        text_col = QVBoxLayout()
        text_col.setSpacing(1)
        text_col.setContentsMargins(0, 0, 0, 0)

        name_color = "#1ED760" if self._is_playlist else TEXT_COLOR
        name_label = QLabel(track_data["name"])
        name_label.setFont(QFont(FONT_FAMILY, 11))
        name_label.setStyleSheet(f"color: {name_color}; background: transparent;")
        fm = name_label.fontMetrics()
        name_label.setText(fm.elidedText(track_data["name"], Qt.ElideRight, SEARCH_POPUP_WIDTH - 100))
        name_label.setToolTip(track_data["name"])

        detail = f"{track_data['artists']}"
        if track_data.get("album"):
            detail += f" \u2022 {track_data['album']}"
        detail_label = QLabel(detail)
        detail_label.setFont(QFont(FONT_FAMILY, 9))
        detail_label.setStyleSheet(f"color: {SUBTEXT_COLOR}; background: transparent;")
        fm2 = detail_label.fontMetrics()
        detail_label.setText(fm2.elidedText(detail, Qt.ElideRight, SEARCH_POPUP_WIDTH - 100))
        detail_label.setToolTip(detail)

        text_col.addStretch()
        text_col.addWidget(name_label)
        text_col.addWidget(detail_label)
        text_col.addStretch()
        layout.addLayout(text_col, 1)

        # Load album art asynchronously
        art_url = track_data.get("album_art_url")
        if art_url:
            reply = network_manager.get(QNetworkRequest(QUrl(art_url)))
            reply.finished.connect(lambda r=reply: self._on_art_loaded(r))

    def _on_art_loaded(self, reply):
        if reply.error() == QNetworkReply.NoError:
            pm = QPixmap()
            pm.loadFromData(reply.readAll())
            if not pm.isNull():
                scaled = pm.scaled(
                    SEARCH_ART_SIZE, SEARCH_ART_SIZE,
                    Qt.KeepAspectRatioByExpanding,
                    Qt.SmoothTransformation,
                )
                self._art_label.setPixmap(self._round_pixmap(scaled, 4))
        reply.deleteLater()

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

    # Hover highlight
    def enterEvent(self, event):
        self._hovered = True
        self.update()

    def leaveEvent(self, event):
        self._hovered = False
        self.update()

    def paintEvent(self, event):
        if self._hovered:
            p = QPainter(self)
            p.setBrush(QColor(255, 255, 255, 15))
            p.setPen(Qt.NoPen)
            p.drawRect(0, 0, self.width(), self.height())

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self.clicked.emit(self._uri, self._context_uri)
