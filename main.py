"""
Spotify Taskbar Player — run this file to start the widget.

    python main.py

Play something in Spotify and the widget will show the current song
with playback controls on your taskbar.
"""

import sys
import ctypes
from PySide6.QtWidgets import QApplication
from widget import PlayerWidget


def _already_running():
    """Use a Windows named mutex to ensure only one instance runs."""
    ctypes.windll.kernel32.CreateMutexW(None, False, "SpotifyTaskbarPlayer_Mutex")
    return ctypes.windll.kernel32.GetLastError() == 183   # ERROR_ALREADY_EXISTS


def main():
    if _already_running():
        print("Already running.")
        return

    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)
    player = PlayerWidget()
    player.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
