"""
Thin wrapper around the Spotify Web API endpoints we need:
  - Search for tracks
  - Play a track on the active device
"""

import json
import threading
import time
import urllib.request
import urllib.error
from urllib.parse import urlencode

_BASE = "https://api.spotify.com/v1"


class SpotifyAPI:
    """Spotify Web API client. Requires a SpotifyAuth instance for tokens."""

    def __init__(self, auth):
        self.auth = auth
        self._playlist_cache = None

    def search_tracks(self, query, limit=8, offset=0):
        """
        Search for tracks by name/artist.

        Returns a list of dicts on success:
            [{"name", "artists", "album", "album_art_url", "uri", "duration_ms"}, ...]
        Returns None on error.
        """
        token = self.auth.get_access_token()
        if not token:
            return None

        params = urlencode({
            "q": query,
            "type": "track",
            "limit": limit,
            "offset": offset,
        })

        try:
            data = _api_get(f"/search?{params}", token)
        except Exception as e:
            print(f"[api] Search failed: {e}")
            return None

        results = []
        for track in data.get("tracks", {}).get("items", []):
            images = track.get("album", {}).get("images", [])
            # Smallest image (64px) is last in the list
            art_url = images[-1]["url"] if images else None

            results.append({
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

        return results

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
                # No active device — try to find one and retry
                device_id = self._find_device(token)
                if device_id:
                    try:
                        _api_put_play(payload, token, device_id=device_id)
                    except Exception:
                        return False, "No active Spotify device — open Spotify first"
                else:
                    return False, "No active Spotify device — open Spotify first"
            if e.code == 403:
                return False, "Spotify Premium is required"
            if e.code == 401:
                return False, "Session expired — please log in again"
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

    def _find_device(self, token):
        """Find a desktop Spotify device. Returns device_id or None."""
        try:
            data = _api_get("/me/player/devices", token)
            # Only consider desktop devices — never play on phone/speaker
            desktops = [
                d for d in data.get("devices", [])
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
            print(f"[api] Device lookup failed: {e}")
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
