"""
Polls every 2 seconds and prints info about whatever window is in focus.
Switch between maximized / fullscreen windows at your own pace.
Press Ctrl+C to stop.
"""
import ctypes
import ctypes.wintypes as wintypes
import time

user32 = ctypes.windll.user32
shell32 = ctypes.windll.shell32

# Proper 64-bit return types
user32.GetForegroundWindow.restype = wintypes.HWND
user32.MonitorFromWindow.restype = wintypes.HANDLE
user32.GetWindowLongW.restype = wintypes.LONG


class MONITORINFO(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("rcMonitor", wintypes.RECT),
        ("rcWork", wintypes.RECT),
        ("dwFlags", wintypes.DWORD),
    ]


GWL_STYLE = -16
WS_CAPTION = 0x00C00000
WS_THICKFRAME = 0x00040000
MONITOR_DEFAULTTONEAREST = 2

last_title = None

print("Polling every 2 seconds. Switch windows at your own pace.")
print("Try: 1) a maximized window,  2) a fullscreen YouTube video (F11)")
print("Press Ctrl+C to stop.\n")

while True:
    try:
        fg = user32.GetForegroundWindow()
        if not fg:
            time.sleep(2)
            continue

        title = ctypes.create_unicode_buffer(256)
        user32.GetWindowTextW(fg, title, 256)

        # Only print when the focused window changes
        if title.value == last_title:
            time.sleep(2)
            continue
        last_title = title.value

        cls = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(fg, cls, 256)

        # Notification state
        state = ctypes.c_int(0)
        shell32.SHQueryUserNotificationState(ctypes.byref(state))
        state_names = {1: "NOT_PRESENT", 2: "BUSY", 3: "D3D_FULLSCREEN",
                       4: "PRESENTATION", 5: "NORMAL", 6: "QUIET_TIME"}

        # Window rect
        win_rect = wintypes.RECT()
        user32.GetWindowRect(fg, ctypes.byref(win_rect))

        # Monitor rect
        hmon = user32.MonitorFromWindow(fg, MONITOR_DEFAULTTONEAREST)
        mi = MONITORINFO()
        mi.cbSize = ctypes.sizeof(MONITORINFO)
        user32.GetMonitorInfoW(hmon, ctypes.byref(mi))
        mon = mi.rcMonitor

        # Style
        style = user32.GetWindowLongW(fg, GWL_STYLE)

        rect_match = (win_rect.left == mon.left and win_rect.top == mon.top
                      and win_rect.right == mon.right
                      and win_rect.bottom == mon.bottom)
        no_chrome = not (style & WS_CAPTION) and not (style & WS_THICKFRAME)

        print(f"{'='*60}")
        print(f"  '{title.value}'  (class: {cls.value})")
        print(f"  Notif state: {state_names.get(state.value, state.value)}")
        print(f"  WindowRect:  ({win_rect.left}, {win_rect.top}, {win_rect.right}, {win_rect.bottom})")
        print(f"  MonitorRect: ({mon.left}, {mon.top}, {mon.right}, {mon.bottom})")
        print(f"  Style:       CAPTION={'yes' if style & WS_CAPTION else 'NO'}  "
              f"THICKFRAME={'yes' if style & WS_THICKFRAME else 'NO'}")
        print(f"  >> Rect exact match? {rect_match}   No chrome? {no_chrome}")
        print(f"  >> FULLSCREEN? {'YES' if rect_match and no_chrome else 'no'}")

        time.sleep(2)
    except KeyboardInterrupt:
        print("\nDone.")
        break
