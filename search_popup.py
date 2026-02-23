"""
Search popup — appears above the taskbar widget.
Type to search, click a result to play it.
"""

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
    FONT_FAMILY, TEXT_COLOR, SUBTEXT_COLOR,
    ICON_SEARCH, ICON_CLOSE, SEARCH_POPUP_WIDTH, SEARCH_POPUP_BG,
    SEARCH_RESULT_HEIGHT, SEARCH_ART_SIZE,
)


# -- search worker (runs API call off the main thread) -------------------

class _SearchWorker(QThread):
    results_ready = Signal(list)
    error = Signal(str)

    def __init__(self, api, query):
        super().__init__()
        self.api = api
        self.query = query

    def run(self):
        results = self.api.search_tracks(self.query)
        if results is not None:
            self.results_ready.emit(results)
        else:
            self.error.emit("Search failed")


class _PlayWorker(QThread):
    finished = Signal(bool, str)  # success, error_message

    def __init__(self, api, uri):
        super().__init__()
        self.api = api
        self.uri = uri

    def run(self):
        ok, msg = self.api.play_track(self.uri)
        self.finished.emit(ok, msg or "")


# -- search popup --------------------------------------------------------

class SearchPopup(QWidget):
    """Frameless search popup that floats above the player widget."""

    def __init__(self, parent_widget, spotify_api):
        super().__init__()
        self._parent_widget = parent_widget
        self._api = spotify_api
        self._worker = None
        self._play_worker = None
        self._net = QNetworkAccessManager(self)

        self.setWindowFlags(
            Qt.FramelessWindowHint
            | Qt.WindowStaysOnTopHint
            | Qt.Tool
        )
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setFixedWidth(SEARCH_POPUP_WIDTH)

        self._build_ui()
        self._position_above_parent()

        # Debounce timer: waits 400ms after last keystroke before searching
        self._debounce = QTimer(self)
        self._debounce.setSingleShot(True)
        self._debounce.setInterval(400)
        self._debounce.timeout.connect(self._do_search)

    # -- UI construction -------------------------------------------------

    def _build_ui(self):
        self._main_layout = QVBoxLayout(self)
        self._main_layout.setContentsMargins(12, 12, 12, 12)
        self._main_layout.setSpacing(8)

        # Header row: search input + close button
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
        self._results_layout = QVBoxLayout(self._results_container)
        self._results_layout.setContentsMargins(0, 0, 0, 0)
        self._results_layout.setSpacing(2)
        self._results_layout.addStretch()
        self._scroll.setWidget(self._results_container)
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
        """Place the popup centered above the parent widget."""
        pw = self._parent_widget
        popup_x = pw.x() + (pw.width() - self.width()) // 2
        popup_y = pw.y() - self.height() - 4
        self.move(popup_x, popup_y)

    def _resize_to_fit(self, result_count):
        """Resize popup height based on number of results."""
        header_h = 60  # input + margins
        status_h = 30 if self._status_label.isVisible() else 0

        if result_count > 0:
            results_h = min(result_count, 6) * (SEARCH_RESULT_HEIGHT + 2)
            total_h = header_h + results_h + status_h + 24  # margins
            self._scroll.setFixedHeight(results_h + 4)
        else:
            total_h = header_h + status_h + 24

        self.setFixedHeight(total_h)
        self._position_above_parent()

    # -- search logic ----------------------------------------------------

    def _on_text_changed(self, text):
        self._debounce.start()  # restart 400ms timer

    def _do_search(self):
        self._debounce.stop()
        query = self._search_input.text().strip()
        if len(query) < 2:
            self._clear_results()
            return

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
            self._resize_to_fit(0)
            return

        self._status_label.hide()
        self._scroll.show()

        for track in results:
            item = _ResultItem(track, self._net)
            item.clicked.connect(self._play_track)
            self._results_layout.insertWidget(
                self._results_layout.count() - 1, item  # before the stretch
            )

        self._resize_to_fit(len(results))

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

    # -- playback --------------------------------------------------------

    def _play_track(self, uri):
        self._status_label.setText("Playing...")
        self._status_label.show()

        self._play_worker = _PlayWorker(self._api, uri)
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
        self._search_input.setFocus()
        self.activateWindow()

    def keyPressEvent(self, event):
        if event.key() == Qt.Key_Escape:
            self.close()
            return
        super().keyPressEvent(event)

    def focusOutEvent(self, event):
        # Close when clicking outside, but not when a child widget has focus
        if not self.isAncestorOf(QWidget.find(0) if False else None):
            pass  # Qt.Popup handles this; for Tool windows we keep it open
        super().focusOutEvent(event)

    # -- painting --------------------------------------------------------

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)

        rect = self.rect().adjusted(1, 1, -1, -1)
        path = QPainterPath()
        path.addRoundedRect(float(rect.x()), float(rect.y()),
                            float(rect.width()), float(rect.height()), 10, 10)

        # Background
        p.setBrush(QColor(*SEARCH_POPUP_BG))
        p.setPen(QColor(80, 80, 80, 80))
        p.drawPath(path)

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

    clicked = Signal(str)  # emits track URI on click

    def __init__(self, track_data, network_manager):
        super().__init__()
        self._uri = track_data["uri"]
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

        name_label = QLabel(track_data["name"])
        name_label.setFont(QFont(FONT_FAMILY, 11))
        name_label.setStyleSheet(f"color: {TEXT_COLOR}; background: transparent;")
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
            p.setRenderHint(QPainter.Antialiasing)
            p.setBrush(QColor(255, 255, 255, 15))
            p.setPen(Qt.NoPen)
            path = QPainterPath()
            path.addRoundedRect(0, 0, self.width(), self.height(), 6, 6)
            p.drawPath(path)

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self.clicked.emit(self._uri)
