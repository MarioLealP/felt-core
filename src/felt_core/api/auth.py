"""Who may call what: the token, SSO and origin rules the auth middleware applies."""

import hmac
import re

from fastapi import (
    Request,
    WebSocket,
)

from felt_core.config import load_config, reveal

# WP-55: what stays reachable without the token - the container healthcheck
# (/openapi.json, schema only, no data) and the docs page shell. OPTIONS
# carries no credentials and gets no CORS answer, so it passes too.
_AUTH_EXEMPT = frozenset({"/openapi.json", "/docs", "/docs/oauth2-redirect", "/redoc"})
# The /shopping-list phone page and exactly the calls it makes. A browser
# can't hold the API token, so Caddy puts SSO in front of these paths
# instead (Mario 2026-10-01: "SSO whenever possible"). Safe only because
# Caddy is the sole way in: no host port since WP-55.
_SSO_GATED = (
    ("GET", re.compile(r"/shopping-list")),
    ("GET", re.compile(r"/v1/shopping-list")),
    ("POST", re.compile(r"/v1/shopping-list")),
    ("DELETE", re.compile(r"/v1/shopping-list/checked")),
    ("PATCH", re.compile(r"/v1/shopping-list/[0-9a-f]{32}")),
    ("DELETE", re.compile(r"/v1/shopping-list/[0-9a-f]{32}")),
    # WP-42.2: the Felt app (app_api.py), which also refuses any request
    # without Caddy's X-Authentik-Username header.
    ("GET", re.compile(r"/v1/app/me")),
    ("GET", re.compile(r"/v1/app/history")),
    ("GET", re.compile(r"/v1/app/asides")),
    ("GET", re.compile(r"/v1/app/margins")),
    ("GET", re.compile(r"/v1/app/margins/count")),
    ("POST", re.compile(r"/v1/app/margins/mark")),
    ("GET", re.compile(r"/v1/app/stream")),
    ("POST", re.compile(r"/v1/app/marks/seen")),
    ("POST", re.compile(r"/v1/app/chat")),
    # WP-42.5: the Systems page (systems_api.py)
    ("GET", re.compile(r"/v1/app/systems")),
    ("GET", re.compile(r"/v1/app/incidents/\d+")),
    ("POST", re.compile(r"/v1/app/incidents/\d+/handled")),
    # WP-42.5: the controls (controls_api.py)
    ("GET", re.compile(r"/v1/app/controls")),
    ("POST", re.compile(r"/v1/app/controls")),
    # WP-58's kill switch lives in the app's settings
    ("GET", re.compile(r"/v1/app/settings")),
    ("POST", re.compile(r"/v1/app/settings")),
)


def _sso_gated(method: str, path: str) -> bool:
    return any(method == m and rx.fullmatch(path) for m, rx in _SSO_GATED)


# WP-58: the only routes the responder's own token opens
_RESPONDER_ROUTES = (
    ("GET", re.compile(r"/v1/incidents/waiting")),
    ("POST", re.compile(r"/v1/incidents/(handoff|\d+/(report|local))")),
)


def _token_ok(given: str, token: str | None) -> bool:
    return bool(token) and hmac.compare_digest(given.encode(), token.encode())


_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


def _origin_ok(request: Request, app_origins: tuple[str, ...]) -> bool:
    """WP-62 phase 4 CSRF guard for the SSO-gated (cookie/forward-auth) routes.

    The forward-auth cookie is SameSite=Lax and every *.clockmost.com app is
    same-site, so a sibling page could drive a credentialed mutation; these
    routes carry no bearer token and felt-core has no CSRF token. A safe method
    is always fine; an unsafe one is refused when the browser says the request
    is cross-origin (Origin set and not us, or Sec-Fetch-Site same-site/
    cross-site). A same-origin app request sends one of app_origins (the SSO
    host, or felt-tray's cert-only window host); native clients use the token
    path, not these routes, and send no Origin.
    """
    if request.method in _SAFE_METHODS:
        return True
    origin = request.headers.get("origin")
    if origin is not None and origin not in app_origins:
        return False
    site = request.headers.get("sec-fetch-site")
    return site is None or site in ("same-origin", "none")


def _ws_authorized(websocket: WebSocket) -> bool:
    """WP-55: the HTTP auth middleware doesn't see websockets.

    Browsers can't set headers on them - so a Bearer header or ?token= is
    checked here.
    """
    config = load_config()
    if config.auth_mode == "identity":
        return False  # B5.4: no shared token any more, and no machine caller uses websockets
    token = reveal(config.api_token)
    if not token:
        return True
    given = websocket.headers.get("authorization", "")
    given = given[7:] if given.startswith("Bearer ") else websocket.query_params.get("token", "")
    return hmac.compare_digest(given.encode(), token.encode())
