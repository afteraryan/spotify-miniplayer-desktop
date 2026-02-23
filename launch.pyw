"""
Double-click this file to start the Spotify Taskbar Player.
The .pyw extension tells Windows to run it silently (no terminal window).
Only one instance can run at a time (enforced by a named mutex).
"""

import sys
import os
import ctypes

# Make sure imports find our project files
os.chdir(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def _already_running():
    """Use a Windows named mutex to ensure only one instance runs."""
    ctypes.windll.kernel32.CreateMutexW(None, False, "SpotifyTaskbarPlayer_Mutex")
    return ctypes.windll.kernel32.GetLastError() == 183   # ERROR_ALREADY_EXISTS


def main():
    if _already_running():
        return   # another instance is already up

    from PySide6.QtWidgets import QApplication
    from widget import PlayerWidget

    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)
    player = PlayerWidget()
    player.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
