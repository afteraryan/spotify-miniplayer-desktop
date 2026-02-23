# Spotify Miniplayer Desktop

A compact music widget that sits directly **on** the Windows 11 taskbar — showing the currently playing track with album art, playback controls, and a seekable progress bar. Search for songs and play them without ever opening Spotify.

![Windows 11](https://img.shields.io/badge/Windows%2011-0078D4?style=flat&logo=windows11&logoColor=white)
![Python 3.13+](https://img.shields.io/badge/Python-3.13+-3776AB?style=flat&logo=python&logoColor=white)

> **Screenshot placeholder** — add a screenshot or GIF of the widget on the taskbar here.

## Features

- **Taskbar widget** — 310x40px, sits directly on the taskbar like a native element
- **Now playing** — album art, song title, artist, play/pause/next/prev
- **Progress bar** — click or drag to seek within a track
- **Search & play** — find songs via Spotify API and play them instantly
- **Transparent by default** — background only appears on hover
- **Fullscreen-aware** — hides during games and fullscreen video, reappears after
- **System tray** — show/hide, auto-start with Windows, quit
- **Single instance** — only one copy runs at a time

## Prerequisites

- **Windows 11**
- **Python 3.13+**
- **Spotify** desktop app (must be running for playback)
- **Spotify Premium** (required for search & play via the API)

## Installation

```bash
git clone https://github.com/YOUR_USERNAME/spotify-miniplayer-desktop.git
cd spotify-miniplayer-desktop
pip install -r requirements.txt
```

## Spotify Developer Setup (one-time)

To use the search & play feature, you need a Spotify Client ID:

1. Go to the [Spotify Developer Dashboard](https://developer.spotify.com/dashboard)
2. Click **Create App**
3. Fill in a name and description (anything you want)
4. Set **Redirect URI** to: `http://127.0.0.1:8888/callback`
5. Check **Web API** under "Which API/SDKs are you planning to use?"
6. Save the app, then copy the **Client ID**
7. Open `spotify_auth.py` and paste your Client ID into `CLIENT_ID`

The first time you use search, a browser window will open for you to log in to Spotify. After that, it stays logged in automatically.

## Usage

```bash
# Run with console output (for debugging)
python main.py

# Run silently (no console window)
pythonw launch.pyw
```

**Controls:**
- Drag the widget left/right to reposition on the taskbar
- Click the bottom edge to seek within a track
- Click the search icon to find and play songs
- Right-click the system tray icon for options

## How It Works

The widget reads now-playing info from **Windows SMTC** (System Media Transport Controls) — the same system your keyboard media keys use. No API needed for basic playback display and controls.

**Search & play** uses the Spotify Web API with OAuth PKCE authentication (no secrets needed — safe for public repos). When you search for a song and click it, the API tells Spotify to play it, and the SMTC display updates automatically.

## Project Structure

```
main.py               Entry point (with console)
launch.pyw             Silent launcher (for auto-start)
widget.py              Main UI widget on the taskbar
media_controller.py    Windows SMTC integration
spotify_auth.py        Spotify OAuth (PKCE flow)
spotify_api.py         Spotify search & play API
search_popup.py        Search popup UI
styles.py              Visual constants & SVG icons
```

## License

MIT
