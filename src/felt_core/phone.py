"""Mario's phone through the Home Assistant companion app (2026-10-18).

The Windows-era felt-desktop-agent (adb alarms, KDE Connect ring/battery)
is gone since the Linux move; the HA app reaches the phone from the
server whenever it's online, desktop on or off. Timers and alarms land in
the phone's own Clock app (Android SET_TIMER / SET_ALARM intents via the
app's `command_activity` - needs its "display over other apps"
permission, granted 2026-10-18), so they ring out loud and survive
felt-core being down. `config.phone` is the app's device name in HA
(`notify.mobile_app_<phone>`, `sensor.<phone>_battery_level`).
"""

import httpx

from felt_core.config import FeltConfig

_EXTRA = "android.intent.extra.alarm."


class PhoneUnavailable(Exception):
    """Not configured, HA unreachable, or HA said no."""


def _headers(config: FeltConfig) -> dict[str, str]:
    if not (config.ha_url and config.ha_token and config.phone):
        raise PhoneUnavailable("phone not configured")
    return {"Authorization": f"Bearer {config.ha_token}"}


async def _notify(config: FeltConfig, message: str, data: dict | None = None) -> None:
    headers = _headers(config)
    body: dict = {"message": message}
    if data:
        body["data"] = data
    try:
        async with httpx.AsyncClient(timeout=15, headers=headers) as client:
            response = await client.post(
                f"{config.ha_url}/api/services/notify/mobile_app_{config.phone}", json=body
            )
    except httpx.HTTPError as e:
        raise PhoneUnavailable(repr(e)) from e
    if response.status_code != 200:
        raise PhoneUnavailable(f"HA {response.status_code}")


def _label(text: str) -> str:
    """Pure: an intent extra value - the extras string splits on , and :"""
    return " ".join(text.replace(",", " ").replace(":", " ").split())[:60]


def _activity(action: str, extras: list[str]) -> dict:
    return {"intent_action": f"android.intent.action.{action}", "intent_extras": ",".join(extras)}


def timer_extras(seconds: int, label: str = "") -> list[str]:
    """Pure: SET_TIMER's extras, started without opening the Clock app."""
    extras = [f"{_EXTRA}LENGTH:{seconds}:int", f"{_EXTRA}SKIP_UI:true:boolean"]
    if _label(label):
        extras.append(f"{_EXTRA}MESSAGE:{_label(label)}")
    return extras


def alarm_extras(hour: int, minute: int, label: str = "") -> list[str]:
    """Pure: SET_ALARM's extras - a time of day only (Android has no date
    extra), so it means "the next time it's this o'clock"."""
    extras = [f"{_EXTRA}HOUR:{hour}:int", f"{_EXTRA}MINUTES:{minute}:int"]
    extras.append(f"{_EXTRA}SKIP_UI:true:boolean")
    if _label(label):
        extras.append(f"{_EXTRA}MESSAGE:{_label(label)}")
    return extras


async def start_timer(config: FeltConfig, seconds: int, label: str = "") -> None:
    await _notify(config, "command_activity", _activity("SET_TIMER", timer_extras(seconds, label)))


async def set_alarm(config: FeltConfig, hour: int, minute: int, label: str = "") -> None:
    extras = alarm_extras(hour, minute, label)
    await _notify(config, "command_activity", _activity("SET_ALARM", extras))


async def ring(config: FeltConfig) -> None:
    """Find-my-phone: a notification on the alarm stream, loud even on
    silent."""
    await _notify(
        config,
        "Here! - Felt",
        {"ttl": 0, "priority": "high", "channel": "alarm_stream", "tag": "felt-find-phone"},
    )


async def battery(config: FeltConfig) -> tuple[int, bool]:
    """(percent, charging) from the app's sensors."""
    headers = _headers(config)
    try:
        async with httpx.AsyncClient(timeout=15, headers=headers) as client:
            level = await client.get(
                f"{config.ha_url}/api/states/sensor.{config.phone}_battery_level"
            )
            state = await client.get(
                f"{config.ha_url}/api/states/sensor.{config.phone}_battery_state"
            )
    except httpx.HTTPError as e:
        raise PhoneUnavailable(repr(e)) from e
    if level.status_code != 200:
        raise PhoneUnavailable(f"HA {level.status_code}")
    try:
        percent = int(float(level.json()["state"]))
    except (KeyError, ValueError) as e:  # "unavailable" while the phone is offline
        raise PhoneUnavailable("no battery reading") from e
    charging = state.status_code == 200 and state.json().get("state") in ("charging", "full")
    return percent, charging
