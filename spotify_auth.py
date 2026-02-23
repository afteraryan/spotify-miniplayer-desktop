"""
Spotify OAuth 2.0 with PKCE (Proof Key for Code Exchange).

No client_secret needed — safe for public repos. Only the client_id is required,
and Spotify considers it a public identifier (not a secret).

First run:  Opens browser -> user logs in -> redirect to localhost -> tokens saved.
After that: Tokens refresh silently in the background. No user interaction needed.
"""

import os
import json
import time
import secrets
import hashlib
import base64
import webbrowser
import urllib.request
import urllib.error
from urllib.parse import urlencode, urlparse, parse_qs
from http.server import HTTPServer, BaseHTTPRequestHandler

# -- PASTE YOUR CLIENT ID HERE ------------------------------------------
# Get one at https://developer.spotify.com/dashboard
# This is safe to commit — it's a public identifier, not a secret.
CLIENT_ID = "470aea4274784a7ca4430941765b8f30"
# -----------------------------------------------------------------------

REDIRECT_URI = "http://127.0.0.1:8888/callback"
SCOPES = "user-modify-playback-state user-read-playback-state"

_AUTH_URL = "https://accounts.spotify.com/authorize"
_TOKEN_URL = "https://accounts.spotify.com/api/token"
_TOKEN_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tokens.json")


class SpotifyAuth:
    """Manages Spotify OAuth tokens using the PKCE flow."""

    def __init__(self):
        self._access_token = None
        self._refresh_token = None
        self._expires_at = 0.0
        self._load_tokens()

    # -- public API ------------------------------------------------------

    def is_authenticated(self):
        """True if we have a refresh token (may need refresh, but that's automatic)."""
        return self._refresh_token is not None

    def get_access_token(self):
        """
        Returns a valid access token, refreshing silently if expired.
        Returns None if not authenticated or refresh fails.
        """
        if not self._refresh_token:
            return None

        # Refresh 60 seconds early to avoid edge-case expiry during a request
        if time.time() >= self._expires_at - 60:
            if not self._do_refresh():
                return None

        return self._access_token

    def login(self):
        """
        Start the PKCE login flow. Opens a browser and waits for the callback.
        Returns True on success. BLOCKS the calling thread — run from a worker.
        """
        if not CLIENT_ID:
            print("[auth] No CLIENT_ID set — open spotify_auth.py and paste yours in.")
            return False

        verifier, challenge = _generate_pkce_pair()
        state = secrets.token_urlsafe(32)

        # Start the local callback server BEFORE opening the browser
        server = HTTPServer(("127.0.0.1", 8888), _CallbackHandler)
        server.timeout = 120  # give user 2 minutes to log in
        server.auth_code = None
        server.auth_state = None

        # Build the authorization URL and open it
        params = {
            "client_id": CLIENT_ID,
            "response_type": "code",
            "redirect_uri": REDIRECT_URI,
            "code_challenge_method": "S256",
            "code_challenge": challenge,
            "scope": SCOPES,
            "state": state,
        }
        webbrowser.open(f"{_AUTH_URL}?{urlencode(params)}")

        # Wait for exactly one callback request
        server.handle_request()
        server.server_close()

        # Validate
        if not server.auth_code:
            print("[auth] No authorization code received.")
            return False
        if server.auth_state != state:
            print("[auth] State mismatch — possible CSRF. Aborting.")
            return False

        # Exchange authorization code for tokens
        return self._exchange_code(server.auth_code, verifier)

    def logout(self):
        """Delete stored tokens."""
        self._access_token = None
        self._refresh_token = None
        self._expires_at = 0.0
        try:
            os.remove(_TOKEN_FILE)
        except FileNotFoundError:
            pass

    # -- token exchange --------------------------------------------------

    def _exchange_code(self, code, verifier):
        """Exchange an authorization code for access + refresh tokens."""
        data = urlencode({
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": REDIRECT_URI,
            "client_id": CLIENT_ID,
            "code_verifier": verifier,
        }).encode()

        try:
            resp = _token_request(data)
        except Exception as e:
            print(f"[auth] Token exchange failed: {e}")
            return False

        self._access_token = resp["access_token"]
        self._refresh_token = resp["refresh_token"]
        self._expires_at = time.time() + resp["expires_in"]
        self._save_tokens()
        return True

    def _do_refresh(self):
        """Silently refresh the access token. Returns True on success."""
        data = urlencode({
            "grant_type": "refresh_token",
            "refresh_token": self._refresh_token,
            "client_id": CLIENT_ID,
        }).encode()

        try:
            resp = _token_request(data)
        except Exception as e:
            print(f"[auth] Token refresh failed: {e}")
            # If refresh token is revoked, clear everything
            if isinstance(e, urllib.error.HTTPError) and e.code in (400, 401):
                self.logout()
            return False

        self._access_token = resp["access_token"]
        # Spotify may issue a new refresh token — always save it
        if "refresh_token" in resp:
            self._refresh_token = resp["refresh_token"]
        self._expires_at = time.time() + resp["expires_in"]
        self._save_tokens()
        return True

    # -- token persistence -----------------------------------------------

    def _save_tokens(self):
        payload = {
            "access_token": self._access_token,
            "refresh_token": self._refresh_token,
            "expires_at": self._expires_at,
        }
        try:
            with open(_TOKEN_FILE, "w") as f:
                json.dump(payload, f)
        except OSError as e:
            print(f"[auth] Could not save tokens: {e}")

    def _load_tokens(self):
        try:
            with open(_TOKEN_FILE) as f:
                data = json.load(f)
            self._access_token = data.get("access_token")
            self._refresh_token = data.get("refresh_token")
            self._expires_at = data.get("expires_at", 0.0)
        except (FileNotFoundError, json.JSONDecodeError, KeyError):
            pass


# -- helpers (module-level) ----------------------------------------------

def _generate_pkce_pair():
    """Generate a PKCE code_verifier and its SHA-256 code_challenge."""
    verifier = secrets.token_urlsafe(64)
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return verifier, challenge


def _token_request(data):
    """POST to Spotify's token endpoint. Returns parsed JSON response."""
    req = urllib.request.Request(
        _TOKEN_URL,
        data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read())


class _CallbackHandler(BaseHTTPRequestHandler):
    """Catches the OAuth redirect from Spotify after the user logs in."""

    def do_GET(self):
        query = parse_qs(urlparse(self.path).query)
        self.server.auth_code = query.get("code", [None])[0]
        self.server.auth_state = query.get("state", [None])[0]

        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(
            b"<html><body style='font-family:Segoe UI,sans-serif;text-align:center;"
            b"padding:60px;background:#181818;color:white'>"
            b"<h2 style='color:#1ED760'>Connected to Spotify!</h2>"
            b"<p>You can close this tab and return to the player.</p>"
            b"<script>setTimeout(function(){window.close()},2000)</script>"
            b"</body></html>"
        )

    def log_message(self, format, *args):
        pass  # suppress server access logs
