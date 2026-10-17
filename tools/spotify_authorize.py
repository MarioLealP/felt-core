"""Spotify consent, the browser half (WP-48).

Opens Spotify's consent page for felt-core's app, catches the redirect on
127.0.0.1:8888, checks `state`, and writes the one-time code to a 0600 file
under $XDG_RUNTIME_DIR. The code is swapped for a refresh token on the
server, where the app's client secret lives - never here.

    python3 tools/spotify_authorize.py <client_id> [scope,scope,...]

Spotify only redirects to registered URIs: `http://127.0.0.1:8888/callback`
must be listed in the app's settings on developer.spotify.com.
"""

import http.server
import os
import secrets
import shutil
import subprocess
import sys
import urllib.parse
import webbrowser

# 8888, not the dashboard's original 8765: that's Felt's overlay port on the desktop
PORT = int(os.environ.get("SPOTIFY_REDIRECT_PORT", "8888"))
REDIRECT = f"http://127.0.0.1:{PORT}/callback"
SCOPES = [
    "user-read-playback-state",  # what's playing, devices (skip detection)
    "user-modify-playback-state",  # play, skip, pause, volume, queue
    "user-library-read",  # what's already saved (so a discovery is new)
    "user-library-modify",  # "save this song"
    "user-top-read",  # seed her taste memory
    "user-read-recently-played",  # seed + what's new to Mario
]


def main() -> None:
    client_id = sys.argv[1]
    # optional: only these scopes (to find one Spotify refuses), e.g. "user-read-playback-state"
    scopes = sys.argv[2].split(",") if len(sys.argv) > 2 else SCOPES
    state = secrets.token_urlsafe(32)
    url = "https://accounts.spotify.com/authorize?" + urllib.parse.urlencode(
        {
            "client_id": client_id,
            "response_type": "code",
            "redirect_uri": REDIRECT,
            "scope": " ".join(scopes),
            "state": state,
            "show_dialog": "true",
        }
    )
    out = os.path.join(os.environ.get("XDG_RUNTIME_DIR", "/tmp"), "felt-spotify.code")
    result: dict[str, str] = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            if urllib.parse.urlparse(self.path).path != "/callback":
                self.send_error(404)
                return
            if query.get("state", [""])[0] != state:
                result["error"] = "state mismatch (not our request)"
            elif "error" in query:
                result["error"] = query["error"][0]
            else:
                result["code"] = query.get("code", [""])[0]
            ok = "code" in result
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(
                b"Done - you can close this tab." if ok else b"Spotify said no - see the terminal."
            )

        def log_message(self, *args) -> None:
            pass

    server = http.server.HTTPServer(("127.0.0.1", PORT), Handler)
    print("Opening Spotify's consent page (a private window: blockers can break Spotify's page).")
    print("If nothing opens, use this link:")
    print(url)
    firefox = shutil.which("firefox")
    if firefox:
        # nosemgrep: clockmost-shell-out  fixed binary, the URL as one argv element, no shell
        subprocess.Popen([firefox, "--private-window", url])
    else:
        webbrowser.open(url)
    while not result:
        server.handle_request()
    if "error" in result:
        sys.exit(f"Spotify: {result['error']}")
    fd = os.open(out, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(result["code"])
    print(f"saved the one-time code to {out} - tell Claude")


if __name__ == "__main__":
    main()
