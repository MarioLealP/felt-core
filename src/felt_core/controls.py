"""WP-42.5: her two controls, hide and quiet (Mario 10-03: "I need quiet and
I need hide. So one hides but doesn't quiet her, the other hides and quiets
her. Like on phone you have the bell for notifications and the moon for full
dnd ... Tap for unlimited, slide for set timer 15 30 1h 8h").

- hide: off the screen; she still talks and tells him things.
- quiet: off the screen and silent; unprompted lines wait in the Margins.
  Server alert pop-ups still come through - quiet is about her (Mario
  10-03: "No this is for felt, raw ntfy can come through").

She lives on the server, so the switches live here: the app sets them from
anywhere, the desktop tray applies them to felt-animation (it polls
GET /v1/controls) and reports her real state and mood back
(POST /v1/controls/desktop), so the controls page is right from the phone
too, and says so when the PC is off.
"""

import json
from datetime import UTC, datetime, timedelta

from felt_core.db import get_setting, set_setting

CONTROLS = ("hide", "quiet")
TIMERS = (15, 30, 60, 480)  # the slide's stops, minutes
# the desktop counts as there if it reported this recently
DESKTOP_FRESH = timedelta(seconds=45)
_FMT = "%Y-%m-%dT%H:%M:%SZ"


def _now(now: datetime | None) -> datetime:
    return now or datetime.now(UTC)


def _read(db_path: str, name: str) -> dict:
    try:
        return json.loads(get_setting(db_path, f"control_{name}") or "{}")
    except ValueError:
        return {}


def state(db_path: str, now: datetime | None = None) -> dict:
    """Both switches as they are now (a timer that ran out is off), plus a
    version that changes whenever either is set, for the tray."""
    now = _now(now)
    out: dict = {"version": int(get_setting(db_path, "controls_version") or 0)}
    for name in CONTROLS:
        saved = _read(db_path, name)
        until = saved.get("until")
        on = bool(saved.get("on")) and (
            until is None or datetime.strptime(until, _FMT).replace(tzinfo=UTC) > now
        )
        out[name] = {"on": on, "until": until if on else None}
    return out


def set_control(
    db_path: str, name: str, on: bool, minutes: int | None = None, now: datetime | None = None
) -> dict:
    """Tap (minutes None) = on until turned off; a slide = on for `minutes`."""
    if name not in CONTROLS:
        raise ValueError(f"unknown control {name!r}")
    until = (_now(now) + timedelta(minutes=minutes)).strftime(_FMT) if on and minutes else None
    set_setting(db_path, f"control_{name}", json.dumps({"on": on, "until": until}))
    set_setting(
        db_path, "controls_version", str(int(get_setting(db_path, "controls_version") or 0) + 1)
    )
    return state(db_path, now)


def quiet(db_path: str, now: datetime | None = None) -> bool:
    return state(db_path, now)["quiet"]["on"]


# -- the WP-58 kill switch -------------------------------------------------------


def claude_alerts(db_path: str) -> bool:
    """Whether the server's Claude looks into alerts (WP-58's kill switch,
    in the app's settings). On unless Mario turned it off."""
    return get_setting(db_path, "claude_alerts", "1") != "0"


def set_claude_alerts(db_path: str, on: bool) -> None:
    set_setting(db_path, "claude_alerts", "1" if on else "0")


# -- what the desktop reports ---------------------------------------------------


def report_desktop(db_path: str, report: dict, now: datetime | None = None) -> None:
    """The tray's heartbeat: her real presence, hush and mood on the PC."""
    keep = {k: report.get(k) for k in ("presence", "hushed", "mood", "animation", "applied")}
    keep["at"] = _now(now).strftime(_FMT)
    set_setting(db_path, "desktop_state", json.dumps(keep))


def desktop(db_path: str, now: datetime | None = None) -> dict | None:
    """The latest heartbeat, or None when the PC hasn't reported lately
    (off, asleep, or the tray isn't running)."""
    try:
        saved = json.loads(get_setting(db_path, "desktop_state") or "{}")
        at = datetime.strptime(saved["at"], _FMT).replace(tzinfo=UTC)
    except (ValueError, KeyError):
        return None
    return saved if _now(now) - at <= DESKTOP_FRESH else None
