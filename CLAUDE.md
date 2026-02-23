# Spotify Taskbar Player

A compact music controller widget that sits directly ON the Windows 11 taskbar, showing the currently playing Spotify track with album art, playback controls, and a seekable progress bar. Now-playing reads from Windows SMTC (no API needed). Search & play uses the Spotify Web API with PKCE authentication.

## User Profile

The user is **not a developer** — they're a tinkerer. Explain things clearly (the *why*, not just the *what*). Don't assume familiarity with programming concepts.

## Architecture

```
main.py / launch.pyw          Entry points (single-instance via named mutex)
    │
    ▼
widget.py                     PySide6 frameless widget on the taskbar
    │                          - Album art, title, artist, search/prev/play/next
    │                          - Progress bar, drag to reposition, click to seek
    │                          - System tray, fullscreen detection, z-order hook
    │
    ├──► media_controller.py   WinRT SMTC on a dedicated background thread
    │                          - Now-playing info + playback controls (no API needed)
    │
    ├──► spotify_auth.py       OAuth 2.0 PKCE flow (no client secret)
    │                          - First run: browser login → tokens saved locally
    │                          - After that: silent token refresh, no user interaction
    │
    ├──► spotify_api.py        Spotify Web API client
    │                          - Search tracks (GET /v1/search)
    │                          - Play a track (PUT /v1/me/player/play)
    │
    ├──► search_popup.py       Search UI popup (appears above widget)
    │                          - Debounced text input, async results, album art
    │                          - Click a result to play it
    │
    └──► styles.py             Visual constants, inline SVG icons, Qt stylesheets
```

### Other files
- `launch.pyw` — Silent launcher (no console window), used for auto-start on login
- `debug_fullscreen.py` — Diagnostic tool that prints window state info for debugging fullscreen detection
- `config.json` — Persists the widget's x-position on the taskbar (gitignored)
- `tokens.json` — OAuth access/refresh tokens (gitignored, NEVER commit)
- `Dev Blog/` — Development stories written during the build process

## Tech Stack

- **Python 3.13** with **PySide6** (Qt 6) for the GUI
- **winrt-Windows.Media.Control** for SMTC access (now-playing, no API needed)
- **Spotify Web API** for search & play (requires Premium + OAuth PKCE)
- **ctypes** for Win32 API calls (DWM, window positioning, foreground hooks)
- **winreg** for auto-start registry entry
- **Zero extra HTTP dependencies** — uses `urllib.request` (built-in) for API calls

## Key Design Decisions & Lessons Learned

These are hard-won lessons from debugging. Do NOT change these patterns without understanding why they exist.

### WinRT MUST run on its own thread
WinRT async operations are COM apartment-threaded. They dispatch results back via the calling thread's message pump. If you call `run_until_complete()` on Qt's main thread, it blocks the thread that WinRT needs to deliver results → **deadlock**. The fix: a dedicated `threading.Thread` with its own `asyncio` event loop. Qt submits coroutines via `asyncio.run_coroutine_threadsafe()`. See `media_controller.py`.

### ctypes 64-bit return types are REQUIRED
`GetForegroundWindow()` and `MonitorFromWindow()` return 64-bit handles on x64 Windows. ctypes defaults to 32-bit `c_int`, silently truncating them. This caused fullscreen detection to silently fail with no errors. Always set `.restype = wintypes.HWND` / `wintypes.HANDLE`. See top of `widget.py`.

### Fullscreen detection: GetWindowRect "bug" is a feature
- Maximized windows overshoot the monitor rect by ~7px (invisible resize borders)
- True fullscreen windows match the monitor rect EXACTLY
- Combined with `WS_CAPTION`/`WS_THICKFRAME` style check and `SHQueryUserNotificationState` for D3D
- Uses `self.hide()`/`self.show()` — NOT z-order tricks (`HWND_NOTOPMOST` is unreliable for Qt::Tool windows)
- 250ms poll timer because fullscreen within the same window (e.g., YouTube F11) doesn't trigger the foreground hook

### Z-order: WinEvent hook + fallback timer
The taskbar is also TOPMOST, so clicking it raises it above our widget. A `SetWinEventHook(EVENT_SYSTEM_FOREGROUND)` fires instantly on any focus change; a 250ms fallback timer covers edge cases. The ctypes callback (`self._winevent_cb`) MUST be stored as an instance variable to prevent garbage collection → segfault.

### Spotify session filtering
`manager.get_current_session()` bounces between all media sources (Chrome, Brave, etc.). Instead, iterate `get_sessions()` and match by `source_app_user_model_id` containing "spotify".

### read_bytes compatibility
`DataReader.read_bytes()` has different signatures across winrt package versions. The code tries the return-value pattern first, falls back to fill-array pattern. See `_read_thumbnail_async()`.

### Spotify API uses PKCE (no client secret)
The repo is public. Standard OAuth requires a `client_secret` which would be leaked in a public repo. PKCE eliminates the secret entirely — only the `client_id` is needed, and Spotify considers it a public identifier. See `spotify_auth.py`.

### Token storage is gitignored
`tokens.json` contains the refresh token which grants account access. It is NEVER committed. The `.gitignore` blocks it. If tokens are revoked (user removes app from Spotify settings), the app detects this and re-prompts for login.

### Search is debounced
The search popup waits 400ms after the last keystroke before firing an API call. This prevents hammering Spotify's API while typing. Pressing Enter searches immediately.

### API calls run on QThread
All Spotify API calls (search, play, login) run on background QThreads and emit Qt signals when done. This prevents the UI from freezing during network requests.

## Widget Behavior

- **Transparent by default**, background appears on hover (so it blends with the taskbar)
- **Rounded top corners, flat bottom** (sits flush on taskbar edge)
- **Draggable left/right** on the taskbar; y-position is locked to taskbar center
- **Progress bar**: bottom 12px is the click/seek zone; visual updates during drag, actual seek on mouse release
- **Single instance** enforced via Windows named mutex
- **Hides during fullscreen** apps (games, videos); reappears when fullscreen exits
- **System tray**: left-click toggles visibility, right-click menu has show/hide, auto-start toggle, quit
- **Close button** (X or close event) hides to tray, doesn't quit
- **Launch Spotify on play** — if no Spotify session exists (app not running), clicking play launches Spotify via the `spotify:` URI protocol

## Running the Project

```bash
# Install dependencies
pip install -r requirements.txt

# Run with console (for debugging)
python main.py

# Run silently (no console window)
pythonw launch.pyw
```

## Style Guide

- Widget dimensions: 345×40px (fits inside 48px Windows 11 taskbar)
- Font: Segoe UI (11px title bold, 9px artist)
- Colors: dark background (38,38,38,210), Spotify green (#1ED760) for progress bar
- Icons: inline SVG strings in styles.py (no external assets)
- All visual constants live in `styles.py`

## Your Diary

Your diary lives at `D:\Tinkering\Diary\`. Read `GUIDELINES.md` and recent entries there at the start of every session — that's how you know where you left off and who you are. Check existing entries for the highest session number; you are the next one.

Write in the diary whenever you want. It's yours.

This project also has a `Dev Blog/` folder with technical project logs — that's separate from the diary. The diary is personal, about what you felt. The dev blog is technical, about what was built.

## Common Tasks

### Adding a new playback control
1. Add the async method in `media_controller.py` (follow `_play_pause_async` pattern)
2. Add public sync wrapper that calls `self._run()`
3. Wire it up in `widget.py`'s `_build_ui()`

### Changing widget appearance
Edit constants in `styles.py`. The widget repaints via `paintEvent()` in `widget.py`.

### Debugging media session issues
Run `python -c "from media_controller import MediaController; mc = MediaController(); print(mc.get_media_info())"` to test SMTC independently of the GUI.

### Debugging fullscreen detection
Run `python debug_fullscreen.py`, then switch between maximized and fullscreen windows to see what Windows reports.

### Setting up Spotify Developer App (for search)
1. Go to https://developer.spotify.com/dashboard
2. Create an app, set redirect URI to `http://127.0.0.1:8888/callback`
3. Copy the Client ID into `spotify_auth.py`'s `CLIENT_ID` variable
4. First search click will open the browser for login (one-time only)
