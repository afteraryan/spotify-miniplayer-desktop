"""
Reads currently-playing media info from Windows SMTC (System Media Transport Controls)
and sends playback commands (play/pause, next, previous).

No Spotify API key needed — this reads from the same system that powers
your keyboard's media keys.

All WinRT calls run on a dedicated background thread so they don't block
the Qt GUI thread (mixing asyncio + Qt on the same thread causes deadlocks).
"""

import asyncio
import threading

from winrt.windows.media.control import (
    GlobalSystemMediaTransportControlsSessionManager,
)
from winrt.windows.storage.streams import DataReader, Buffer, InputStreamOptions


class MediaController:
    """Talk to Windows to find out what's playing and control it."""

    def __init__(self):
        self._loop = None
        self._manager = None
        self._ready = threading.Event()

        # WinRT lives on its own thread with its own asyncio loop
        self._thread = threading.Thread(target=self._worker, daemon=True)
        self._thread.start()
        self._ready.wait(timeout=5)

    # ── background thread ─────────────────────────────────────

    def _worker(self):
        """Background thread: owns the asyncio loop and all WinRT objects."""
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._manager = self._loop.run_until_complete(
                GlobalSystemMediaTransportControlsSessionManager.request_async()
            )
        except Exception as e:
            print(f"Could not connect to Windows media session: {e}")
        self._ready.set()
        self._loop.run_forever()          # keep alive for future calls

    def _run(self, coro):
        """Submit a coroutine to the worker thread and wait for the result."""
        if self._loop is None or self._loop.is_closed():
            return None
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        try:
            return future.result(timeout=3)
        except Exception:
            return None

    # ── session lookup ────────────────────────────────────────

    def _find_spotify_session(self):
        """Scan all sessions and return the Spotify one, or None."""
        sessions = self._manager.get_sessions()
        for i in range(sessions.size):
            s = sessions.get_at(i)
            app_id = (s.source_app_user_model_id or "").lower()
            if "spotify" in app_id:
                return s
        return None

    # ── public: read info ─────────────────────────────────────

    def get_media_info(self):
        """
        Return a dict with the current Spotify song info, or None.

        Keys:
            title      (str)  – song name
            artist     (str)  – artist name
            is_playing (bool) – True if music is actively playing
            thumbnail  (bytes | None) – album art image data
        """
        if self._manager is None:
            return None
        return self._run(self._get_media_info_async())

    async def _get_media_info_async(self):
        session = self._find_spotify_session()
        if session is None:
            return None

        props = await session.try_get_media_properties_async()
        playback = session.get_playback_info()

        # PlaybackStatus enum: 0=Closed 1=Opened 2=Changing 3=Stopped 4=Playing 5=Paused
        is_playing = playback.playback_status == 4

        # Timeline (position / duration)
        timeline = session.get_timeline_properties()
        position = timeline.position.total_seconds()
        duration = timeline.end_time.total_seconds()

        return {
            "title": props.title or "Unknown Title",
            "artist": props.artist or "Unknown Artist",
            "is_playing": is_playing,
            "thumbnail": await self._read_thumbnail_async(props),
            "position": position,
            "duration": duration,
        }

    async def _read_thumbnail_async(self, props):
        """Read album-art bytes from the media properties. Returns bytes or None."""
        try:
            thumb_ref = props.thumbnail
            if thumb_ref is None:
                return None

            readable = await thumb_ref.open_read_async()
            size = int(readable.size)
            if size <= 0:
                return None

            buf = Buffer(size)
            await readable.read_async(buf, size, InputStreamOptions.READ_AHEAD)
            reader = DataReader.from_buffer(buf)

            # winrt's read_bytes may use a fill-array or return-value pattern
            # depending on package version — try both
            try:
                data = reader.read_bytes(buf.length)
                return bytes(data)
            except TypeError:
                data = bytearray(buf.length)
                reader.read_bytes(data)
                return bytes(data)
        except Exception:
            return None

    # ── public: playback commands ─────────────────────────────

    def play_pause(self):
        """Toggle play/pause. Returns False if no Spotify session was found."""
        if self._manager is None:
            return False
        result = self._run(self._play_pause_async())
        return result if result is not None else False

    async def _play_pause_async(self):
        session = self._find_spotify_session()
        if session:
            await session.try_toggle_play_pause_async()
            return True
        return False

    def next_track(self):
        if self._manager is None:
            return
        self._run(self._next_async())

    async def _next_async(self):
        session = self._find_spotify_session()
        if session:
            await session.try_skip_next_async()

    def prev_track(self):
        if self._manager is None:
            return
        self._run(self._prev_async())

    async def _prev_async(self):
        session = self._find_spotify_session()
        if session:
            await session.try_skip_previous_async()

    def seek(self, position_seconds):
        """Seek to a specific position (in seconds) in the current track."""
        if self._manager is None:
            return
        self._run(self._seek_async(position_seconds))

    async def _seek_async(self, position_seconds):
        session = self._find_spotify_session()
        if session:
            # SMTC expects position in 100-nanosecond ticks
            ticks = int(position_seconds * 10_000_000)
            await session.try_change_playback_position_async(ticks)
