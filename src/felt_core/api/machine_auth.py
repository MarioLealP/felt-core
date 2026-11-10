"""Machine callers by Felt identity (WP-62 M2b B5.4, decision D3).

Felt's own parts reach felt-core on Caddy's machine-only host
(felt-api.clockmost.com) with their 2-hour Felt client certs. Caddy requires
and verifies the cert (pki_felt only), refuses anything but one workload
SPIFFE URI, and passes it as ``X-Felt-Identity`` (+ the cert's serial as
``X-Felt-Serial``); every other Caddy site strips client copies of both.

felt-core authorizes per identity per route, deny by default: each identity
may call exactly the routes its code uses (the lists below). Revocation: Caddy
checks no CRL/OCSP (measured, B0 G2), so a cert is dead at its 2h expiry, or
at once via FELT_MACHINE_DENY_SERIALS (decimal serials, as Caddy sends them).
"""

import re

_W = "spiffe://clockmost.internal/workload/"


def _r(*routes: tuple[str, str]) -> tuple[tuple[str, re.Pattern[str]], ...]:
    return tuple((m, re.compile(p)) for m, p in routes)


MACHINE_ROUTES: dict[str, tuple[tuple[str, re.Pattern[str]], ...]] = {
    # felt-tray (desktop): api_client.py
    _W + "tray": _r(
        ("POST", r"/v1/voice-chat"),
        ("POST", r"/v1/text-chat"),
        ("GET", r"/v1/reply/[A-Za-z0-9_-]+/chunk/\d+"),
        ("POST", r"/v1/react/screen"),
        ("GET", r"/v1/settings/temperature-unit"),
        ("POST", r"/v1/settings/temperature-unit"),
        ("GET", r"/v1/notices"),
        ("GET", r"/v1/notices/latest"),
        ("POST", r"/v1/incidents/\d+/handled"),
        ("GET", r"/v1/controls"),
        ("POST", r"/v1/controls/desktop"),
        ("GET", r"/v1/music/now"),
        ("GET", r"/v1/music/lyrics"),
        ("GET", r"/v1/proactive/next"),
        ("GET", r"/v1/proactive/\d+/audio"),
        ("POST", r"/v1/proactive/\d+/requeue"),
        ("GET", r"/v1/calendar/upcoming"),
        # her body runs inside felt-tray since 2026-10-16 (felt-animation's routes, one identity)
        ("POST", r"/v1/events"),
        ("POST", r"/v1/react/event"),
        ("POST", r"/v1/reaction-tag"),
        ("GET", r"/v1/weather/now"),
    ),
    # felt-animation (desktop): event_log, behavior, loops, reaction, playback
    _W + "felt-animation": _r(
        ("POST", r"/v1/events"),
        ("POST", r"/v1/react/event"),
        ("POST", r"/v1/reaction-tag"),
        ("GET", r"/v1/weather/now"),
        ("GET", r"/v1/music/now"),
        ("GET", r"/v1/music/lyrics"),
    ),
    # felt-responder (server host): its incident routes (WP-58; local = her model takes over)
    _W + "felt-responder": _r(
        ("GET", r"/v1/incidents/waiting"),
        ("POST", r"/v1/incidents/handoff"),
        ("POST", r"/v1/incidents/\d+/(report|local)"),
    ),
}


def authorize(identity: str, serial: str, method: str, path: str, deny_serials) -> str | None:
    """None if allowed, else the reason (for the 403 and the log)."""
    if serial and serial in set(deny_serials or ()):
        return "revoked certificate"
    routes = MACHINE_ROUTES.get(identity)
    if routes is None:
        return "unknown identity"
    if not any(method == m and rx.fullmatch(path) for m, rx in routes):
        return "route not allowed for this identity"
    return None
