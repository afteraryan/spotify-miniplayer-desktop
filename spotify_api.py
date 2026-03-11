"""
Thin wrapper around the Spotify Web API endpoints we need:
  - Search for tracks
  - Play a track on the active device
"""

import json
import os
import threading
import time
import urllib.request
import urllib.error
from urllib.parse import urlencode

_BASE = "https://api.spotify.com/v1"
_RATE_LIMIT_FILE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), ".rate_limit"
)


class SpotifyAPI:
    """Spotify Web API client. Requires a SpotifyAuth instance for tokens."""

    def __init__(self, auth):
        self.auth = auth
        self._playlist_cache = None
        self._last_art_url = None
        self._last_art_bytes = None
        self._rate_limited_until = self._load_rate_limit()

    def search_tracks(self, query, limit=8, offset=0):
        """
        Search for tracks and albums by name/artist.

        Returns a list of dicts on success:
            [{"name", "artists", "album", "album_art_url", "uri", "duration_ms", "_type"}, ...]
        Returns None on error.
        """
        token = self.auth.get_access_token()
        if not token:
            return None

        params = urlencode({
            "q": query,
            "type": "track,album",
            "limit": limit,
            "offset": offset,
        })

        try:
            data = _api_get(f"/search?{params}", token)
        except Exception as e:
            print(f"[api] Search failed: {e}")
            return None

        # Parse album results (only on first page)
        albums = []
        if offset == 0:
            for album in data.get("albums", {}).get("items", []):
                images = album.get("images", [])
                art_url = images[-1]["url"] if images else None
                artists = ", ".join(
                    a.get("name", "") for a in album.get("artists", [])
                )
                albums.append({
                    "name": album.get("name", "Unknown"),
                    "artists": artists,
                    "album": "",
                    "album_uri": album.get("uri", ""),
                    "album_art_url": art_url,
                    "uri": album.get("uri", ""),
                    "_type": "album",
                })

        # Parse track results
        tracks = []
        for track in data.get("tracks", {}).get("items", []):
            images = track.get("album", {}).get("images", [])
            art_url = images[-1]["url"] if images else None

            tracks.append({
                "name": track.get("name", "Unknown"),
                "artists": ", ".join(
                    a.get("name", "") for a in track.get("artists", [])
                ),
                "album": track.get("album", {}).get("name", ""),
                "album_uri": track.get("album", {}).get("uri", ""),
                "album_art_url": art_url,
                "uri": track["uri"],
                "duration_ms": track.get("duration_ms", 0),
            })

        return albums, tracks

    def add_to_queue(self, track_uri):
        """Add a track to the user's playback queue.
        Returns (True, None) on success, (False, error_message) on failure."""
        token = self.auth.get_access_token()
        if not token:
            return False, "Not logged in"
        try:
            params = urlencode({"uri": track_uri})
            req = urllib.request.Request(
                f"{_BASE}/me/player/queue?{params}",
                data=b"",
                headers={"Authorization": f"Bearer {token}"},
                method="POST",
            )
            with urllib.request.urlopen(req):
                pass
            return True, None
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return False, "No active Spotify device"
            elif e.code == 403:
                return False, "Premium required"
            return False, f"Queue error (HTTP {e.code})"
        except Exception as e:
            return False, f"Network error: {e}"

    def play_track(self, track_uri, context_uri=None):
        """
        Play a specific track on the user's active Spotify device.
        If context_uri is provided (artist, album, or playlist URI), plays
        within that context so Spotify generates a queue and autoplay continues.

        Returns (True, None) on success.
        Returns (False, error_message) on failure.
        """
        token = self.auth.get_access_token()
        if not token:
            return False, "Not logged in"

        if not track_uri and context_uri:
            # Play a context from the beginning (e.g. a playlist)
            payload = {"context_uri": context_uri}
        elif context_uri:
            payload = {
                "context_uri": context_uri,
                "offset": {"uri": track_uri},
            }
        else:
            payload = {"uris": [track_uri]}

        try:
            _api_put_play(payload, token)
        except urllib.error.HTTPError as e:
            if e.code == 404:
                # No active device — try to find one, poll if Spotify is still starting
                device_id = self._find_device(token)
                if not device_id:
                    device_id = self._wait_for_device(token, timeout=20)
                if not device_id:
                    return False, "No active Spotify device — open Spotify first"
                try:
                    _api_put_play(payload, token, device_id=device_id)
                except Exception:
                    return False, "No active Spotify device — open Spotify first"
                # Retry succeeded — fall through to success path
            elif e.code == 403:
                return False, "Spotify Premium is required"
            elif e.code == 401:
                return False, "Session expired — please log in again"
            else:
                err_body = ""
                try:
                    err_body = e.read().decode()
                except Exception:
                    pass
                print(f"[api] Play failed: HTTP {e.code} — {err_body}")
                return False, f"Playback error (HTTP {e.code})"
        except Exception as e:
            print(f"[api] Play failed: {e}")
            return False, "Network error"

        # For album context: replace album tracks with radio in background
        if track_uri and context_uri and "album" in context_uri:
            t = threading.Thread(
                target=self._replace_album_with_radio,
                args=(track_uri, context_uri, token),
                daemon=True,
            )
            t.start()

        return True, None

    def _wait_for_device(self, token, timeout=20):
        """Poll for a desktop Spotify device to appear (e.g. Spotify is still starting)."""
        elapsed = 0
        while elapsed < timeout:
            time.sleep(2)
            elapsed += 2
            device_id = self._find_device(token)
            if device_id:
                return device_id
        return None

    def _find_device(self, token):
        """Find a desktop Spotify device. Returns device_id or None."""
        try:
            data = _api_get("/me/player/devices", token)
            devices = data.get("devices", [])
            # Only consider desktop devices — never play on phone/speaker
            desktops = [
                d for d in devices
                if d.get("type", "").lower() == "computer"
            ]
            # Prefer the active one
            for d in desktops:
                if d.get("is_active"):
                    return d["id"]
            # Fall back to first desktop device
            if desktops:
                return desktops[0]["id"]
        except Exception as e:
            print(f"[api] Device lookup failed: {e}", flush=True)
        return None

    def _replace_album_with_radio(self, track_uri, context_uri, token):
        """Background: wait for Spotify to populate the queue, then replace
        album tracks with auto-generated radio songs.

        Uses {"uris": [track, radio1, radio2, ...]} instead of POST /me/player/queue
        so the songs don't become persistent user-queued tracks. On the next search,
        the same approach replaces everything cleanly.
        """
        try:
            time.sleep(3)

            # Fetch the current queue
            try:
                queue_data = _api_get("/me/player/queue", token)
            except Exception as e:
                print(f"[api] Queue fetch failed: {e}")
                return

            queue = queue_data.get("queue", [])
            if not queue:
                return

            # Separate: album tracks vs auto-generated radio (different album URI)
            album_count = 0
            radio_uris = []
            for item in queue:
                item_album = item.get("album", {}).get("uri", "")
                if item_album == context_uri:
                    album_count += 1
                else:
                    radio_uris.append(item["uri"])

            print(f"[api] Queue: {len(queue)} total, {album_count} album, {len(radio_uris)} radio")

            if not radio_uris:
                # No radio songs found — leave queue as-is rather than destroying it
                print("[api] No radio songs in queue, skipping cleanup")
                return

            # Get current playback position
            progress = 0
            try:
                state = _api_get("/me/player", token)
                progress = state.get("progress_ms", 0)
            except Exception:
                pass

            # Replay: current track continues at same position, radio songs as queue
            # Using uris list (not POST /queue) so next search replaces cleanly
            uris = [track_uri] + radio_uris
            _api_put_play({"uris": uris, "position_ms": progress}, token)
            print(f"[api] Replaced album queue with {len(radio_uris)} radio songs")

        except Exception as e:
            print(f"[api] Queue cleanup failed (non-fatal): {e}")

    def get_currently_playing(self):
        """
        Get the currently playing track via the Spotify Web API.
        Returns a dict matching MediaController.get_media_info() format, or None.

        Used as a fallback when SMTC has no Spotify desktop session
        (e.g. playing on Spotify Web in a browser).
        """
        token = self.auth.get_access_token()
        if not token:
            return None

        # Respect rate-limit backoff
        if time.time() < self._rate_limited_until:
            return None

        try:
            req = urllib.request.Request(
                f"{_BASE}/me/player",
                headers={"Authorization": f"Bearer {token}"},
            )
            with urllib.request.urlopen(req, timeout=3) as resp:
                body = resp.read()
                if not body:
                    return None
                data = json.loads(body)
        except urllib.error.HTTPError as e:
            if e.code == 429:
                retry_after = int(e.headers.get("Retry-After", 10))
                self._rate_limited_until = time.time() + retry_after
                self._save_rate_limit()
            return None
        except Exception:
            return None

        if not data or not data.get("item"):
            return None

        item = data["item"]

        # Only handle tracks (not episodes/podcasts)
        if item.get("type") != "track":
            return None

        title = item.get("name", "Unknown Title")
        artists = ", ".join(a.get("name", "") for a in item.get("artists", []))

        position = data.get("progress_ms", 0) / 1000.0
        duration = item.get("duration_ms", 0) / 1000.0

        # Album art — cached to avoid re-downloading every poll
        images = item.get("album", {}).get("images", [])
        art_url = images[-1]["url"] if images else None
        thumbnail = self._get_cached_thumbnail(art_url)

        return {
            "title": title,
            "artist": artists,
            "is_playing": data.get("is_playing", False),
            "thumbnail": thumbnail,
            "position": position,
            "duration": duration,
        }

    def _get_cached_thumbnail(self, url):
        """Download and cache album art. Returns bytes or None."""
        if not url:
            return None
        if url == self._last_art_url:
            return self._last_art_bytes
        try:
            with urllib.request.urlopen(url, timeout=2) as resp:
                data = resp.read()
            self._last_art_url = url
            self._last_art_bytes = data
            return data
        except Exception:
            return None

    def is_rate_limited(self):
        """True if we're currently rate-limited by Spotify."""
        return time.time() < self._rate_limited_until

    @staticmethod
    def _load_rate_limit():
        """Load persisted rate-limit timestamp (survives app restarts)."""
        try:
            with open(_RATE_LIMIT_FILE) as f:
                until = float(f.read().strip())
            if until > time.time():
                return until
        except (FileNotFoundError, ValueError):
            pass
        return 0.0

    def _save_rate_limit(self):
        """Persist the rate-limit timestamp to disk."""
        try:
            with open(_RATE_LIMIT_FILE, "w") as f:
                f.write(str(self._rate_limited_until))
        except OSError:
            pass

    def pause(self):
        """Pause playback via Web API."""
        token = self.auth.get_access_token()
        if not token:
            return False
        try:
            req = urllib.request.Request(
                f"{_BASE}/me/player/pause",
                data=b"",
                headers={"Authorization": f"Bearer {token}"},
                method="PUT",
            )
            urllib.request.urlopen(req, timeout=3)
            return True
        except Exception:
            return False

    def resume(self):
        """Resume playback via Web API."""
        token = self.auth.get_access_token()
        if not token:
            return False
        try:
            _api_put_play({}, token)
            return True
        except Exception:
            return False

    def next_track(self):
        """Skip to next track via Web API."""
        token = self.auth.get_access_token()
        if not token:
            return False
        try:
            req = urllib.request.Request(
                f"{_BASE}/me/player/next",
                data=b"",
                headers={"Authorization": f"Bearer {token}"},
                method="POST",
            )
            urllib.request.urlopen(req, timeout=3)
            return True
        except Exception:
            return False

    def prev_track(self):
        """Skip to previous track via Web API."""
        token = self.auth.get_access_token()
        if not token:
            return False
        try:
            req = urllib.request.Request(
                f"{_BASE}/me/player/previous",
                data=b"",
                headers={"Authorization": f"Bearer {token}"},
                method="POST",
            )
            urllib.request.urlopen(req, timeout=3)
            return True
        except Exception:
            return False

    def seek(self, position_seconds):
        """Seek to a position (in seconds) via Web API."""
        token = self.auth.get_access_token()
        if not token:
            return False
        position_ms = int(position_seconds * 1000)
        try:
            req = urllib.request.Request(
                f"{_BASE}/me/player/seek?position_ms={position_ms}",
                data=b"",
                headers={"Authorization": f"Bearer {token}"},
                method="PUT",
            )
            urllib.request.urlopen(req, timeout=3)
            return True
        except Exception:
            return False

    def get_my_playlists(self, query=None):
        """
        Fetch the user's playlists (cached after first call).
        If query is provided, filter by name (case-insensitive).
        Returns a list of dicts: [{"name", "uri", "track_count", "image_url", "owner"}, ...]
        """
        if self._playlist_cache is None:
            result = self._fetch_playlists()
            if result:  # only cache non-empty results (403 returns [])
                self._playlist_cache = result
            else:
                return []

        if not query:
            return self._playlist_cache

        q = query.lower()
        return [p for p in self._playlist_cache if q in p["name"].lower()]

    def _fetch_playlists(self):
        """Fetch all of the user's playlists from the API."""
        token = self.auth.get_access_token()
        if not token:
            return []

        playlists = []
        path = "/me/playlists?limit=50"

        while path:
            try:
                data = _api_get(path, token)
            except Exception as e:
                print(f"[api] Playlist fetch failed: {e}")
                break

            for item in data.get("items", []):
                if not item:
                    continue
                images = item.get("images", [])
                playlists.append({
                    "name": item.get("name", ""),
                    "uri": item.get("uri", ""),
                    "track_count": item.get("items", {}).get("total", 0) or item.get("tracks", {}).get("total", 0),
                    "image_url": images[-1]["url"] if images else None,
                    "owner": item.get("owner", {}).get("display_name", ""),
                })

            next_url = data.get("next")
            path = next_url.replace(_BASE, "") if next_url else None

        return playlists


# -- internal helpers ----------------------------------------------------

def _api_get(path, token):
    """GET request to the Spotify API. Returns parsed JSON."""
    req = urllib.request.Request(
        f"{_BASE}{path}",
        headers={"Authorization": f"Bearer {token}"},
    )
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read())


def _api_put_play(payload, token, device_id=None):
    """PUT /me/player/play. Raises on HTTP error."""
    url = f"{_BASE}/me/player/play"
    if device_id:
        url += f"?device_id={device_id}"
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        method="PUT",
    )
    with urllib.request.urlopen(req):
        pass
