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
            # First: wipe old queue by playing the track alone (no context)
            _api_put_play({"uris": [track_uri]}, token)
            # Then: immediately play with album context for fresh queue generation
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

        # For album context: clean up album tracks in background, keep auto-generated queue
        if track_uri and context_uri and "album" in context_uri:
            t = threading.Thread(
                target=self._replace_album_queue_with_radio,
                args=(track_uri, context_uri, token),
                daemon=True,
            )
            t.start()

        return True, None

    def _replace_album_queue_with_radio(self, track_uri, context_uri, token):
        """Background: wait for Spotify to populate the queue, then swap album
        tracks for the auto-generated radio songs.

        1. Wait for queue to populate (including auto-generated songs)
        2. Fetch queue, separate album tracks from auto-generated ones
        3. Replay without context (clears everything)
        4. Re-add auto-generated songs
        """
        try:
            # Wait for Spotify to populate the full queue including radio songs
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

            # Separate: album tracks vs auto-generated (different album URI)
            radio_songs = []
            for item in queue:
                item_album = item.get("album", {}).get("uri", "")
                if item_album != context_uri:
                    radio_songs.append(item["uri"])

            if not radio_songs:
                # No auto-generated songs found yet — don't clear, leave as-is
                print("[api] No radio songs in queue yet, skipping cleanup")
                return

            # Get current playback position
            progress = 0
            try:
                state = _api_get("/me/player", token)
                progress = state.get("progress_ms", 0)
            except Exception:
                pass

            # Replay without context (clears album queue)
            _api_put_play({"uris": [track_uri], "position_ms": progress}, token)

            # Re-add radio songs sequentially (order matters)
            added = 0
            for uri in radio_songs:
                try:
                    add_req = urllib.request.Request(
                        f"{_BASE}/me/player/queue?uri={uri}",
                        headers={"Authorization": f"Bearer {token}"},
                        method="POST",
                    )
                    urllib.request.urlopen(add_req)
                    added += 1
                    time.sleep(0.1)  # small delay to preserve order
                except Exception:
                    pass

            print(f"[api] Replaced album queue with {added} radio songs")

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


def _api_put_play(payload, token):
    """PUT /me/player/play. Raises on HTTP error."""
    req = urllib.request.Request(
        f"{_BASE}/me/player/play",
        data=json.dumps(payload).encode(),
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        method="PUT",
    )
    with urllib.request.urlopen(req):
        pass
