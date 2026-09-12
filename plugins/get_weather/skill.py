"""Weather via Open-Meteo — free, no API key, no vendor lock-in.

Location comes from config (FELT_HOME_LATITUDE/LONGITUDE), never
hardcoded — unset means the skill reports itself unavailable rather than
silently guessing a location.

2026-09-12: covers current conditions plus any day within the forecast
window (up to 16 days out, Open-Meteo's max) via one combined request
(current + daily data including rain probability). "now" is its own
branch (current conditions, not a day lookup); "weekend" resolves to
the upcoming Saturday+Sunday pair. Everything else — "today",
"tomorrow", "next tuesday", "in 3 days", a literal date — goes through
`date_resolution.resolve_date`, which actually parses the phrase via
`dateparser` instead of matching a fixed set of strings (the original
version only handled now/today/tomorrow/weekend and was flagged by
Mario as "not right" for exactly that reason).
"""

from datetime import date

import httpx

from felt_core.date_resolution import resolve_date, upcoming_weekend
from felt_core.params import get_single_param
from felt_core.plugin import SkillResult

name = "get_weather"
description = (
    "Gets the weather at the configured home location: current conditions, or "
    "the forecast for any day or date within the next couple weeks (today, "
    "tomorrow, a weekday name, 'in N days', the weekend, or a specific date). "
    "Use this, NOT web_search, for any weather question."
)
parameters_schema = {
    "type": "object",
    "properties": {
        "day": {
            "type": "string",
            "description": (
                "'now' (default) for current conditions, or a day/date reference "
                "such as 'today', 'tomorrow', 'weekend', 'next tuesday', 'in 3 days', "
                "or an explicit date"
            ),
        }
    },
}

_WMO_CODES = {
    0: "clear sky",
    1: "mainly clear",
    2: "partly cloudy",
    3: "overcast",
    45: "fog",
    48: "depositing rime fog",
    51: "light drizzle",
    53: "moderate drizzle",
    55: "dense drizzle",
    61: "slight rain",
    63: "moderate rain",
    65: "heavy rain",
    71: "slight snow",
    73: "moderate snow",
    75: "heavy snow",
    80: "slight rain showers",
    81: "moderate rain showers",
    82: "violent rain showers",
    95: "thunderstorm",
    96: "thunderstorm with slight hail",
    99: "thunderstorm with heavy hail",
}


def _describe_day(daily: dict, index: int) -> str:
    condition = _WMO_CODES.get(daily["weather_code"][index], f"code {daily['weather_code'][index]}")
    rain_chance = daily["precipitation_probability_max"][index]
    return (
        f"{daily['time'][index]}: {condition}, high {daily['temperature_2m_max'][index]}°C, "
        f"low {daily['temperature_2m_min'][index]}°C, rain chance {rain_chance}%"
    )


async def handle(params, ctx):
    config = ctx.config
    if config.home_latitude is None or config.home_longitude is None:
        return SkillResult(text="Weather isn't configured (no home location set).")

    day = str(get_single_param(params, "day", "when", "date") or "now").lower()

    async with httpx.AsyncClient(timeout=20) as client:
        response = await client.get(
            "https://api.open-meteo.com/v1/forecast",
            params={
                "latitude": config.home_latitude,
                "longitude": config.home_longitude,
                "current": "temperature_2m,weather_code,wind_speed_10m",
                "daily": "temperature_2m_max,temperature_2m_min,weather_code,precipitation_probability_max",
                "forecast_days": 16,
                "timezone": "auto",
            },
        )
        response.raise_for_status()
        data = response.json()

    current = data["current"]
    daily = data["daily"]
    today = date.fromisoformat(daily["time"][0])

    if day in ("now", "right now", "currently", "current"):
        condition = _WMO_CODES.get(current["weather_code"], f"code {current['weather_code']}")
        return SkillResult(
            text=f"Right now: {current['temperature_2m']}°C, {condition}, wind {current['wind_speed_10m']} km/h"
        )

    if "weekend" in day:
        saturday, sunday = upcoming_weekend(today)
        lines = [
            _describe_day(daily, daily["time"].index(d.isoformat()))
            for d in (saturday, sunday)
            if d.isoformat() in daily["time"]
        ]
        if not lines:
            return SkillResult(text="The upcoming weekend is outside the forecast window I have data for.")
        return SkillResult(text="Weekend — " + "; ".join(lines))

    target = resolve_date(day, today)
    if target is None:
        # Couldn't parse it as a date at all — best-effort fall back to
        # today, same as the original behavior for an unrecognized phrase.
        target = today

    if target.isoformat() not in daily["time"]:
        return SkillResult(
            text=f"{target.isoformat()} is outside the forecast window I have data for (next {len(daily['time'])} days)."
        )

    index = daily["time"].index(target.isoformat())
    prefix = f"Right now: {current['temperature_2m']}°C. " if target == today else ""
    return SkillResult(text=f"{prefix}{_describe_day(daily, index)}")
