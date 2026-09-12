"""Weather via Open-Meteo — free, no API key, no vendor lock-in.

Location comes from config (FELT_HOME_LATITUDE/LONGITUDE), never
hardcoded — unset means the skill reports itself unavailable rather than
silently guessing a location.

2026-09-12: covers the four real questions Mario actually asks —
current, tomorrow, weekend, and yes/no rain-today — via one combined
Open-Meteo request (current + a week of daily data including rain
probability), rather than a separate endpoint per case. The chat model
answers the specific nuance ("does it rain today?") from whichever
day's data is included in the result.

KNOWN LIMITATION, flagged by Mario as "not right" (2026-09-12): this is
a fixed small set of string-matched cases (now/today/tomorrow/weekend),
not genuinely dynamic. It doesn't handle "next Tuesday," "in 3 days,"
"next week," a specific date, etc. To be expanded later to actually
parse an arbitrary day/date reference (e.g. via `dateparser` or similar)
and index into a longer Open-Meteo forecast window generically, instead
of hand-matching a handful of known phrases. Not built now — marked for
later per Mario's own instruction.
"""

from datetime import date

import httpx

from felt_core.params import get_single_param
from felt_core.plugin import SkillResult

name = "get_weather"
description = (
    "Gets the weather at the configured home location: current conditions, "
    "tomorrow, the upcoming weekend, or a specific day's rain chance. Use "
    "this, NOT web_search, for any weather question."
)
parameters_schema = {
    "type": "object",
    "properties": {
        "day": {
            "type": "string",
            "description": "'now' (default), 'today', 'tomorrow', or 'weekend'",
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
                "forecast_days": 7,
                "timezone": "auto",
            },
        )
        response.raise_for_status()
        data = response.json()

    current = data["current"]
    daily = data["daily"]

    if "tomorrow" in day:
        return SkillResult(text=f"Tomorrow — {_describe_day(daily, 1)}")

    if "weekend" in day:
        weekend_lines = [
            _describe_day(daily, i) for i, iso in enumerate(daily["time"]) if date.fromisoformat(iso).weekday() >= 5
        ]
        return SkillResult(text="Weekend — " + "; ".join(weekend_lines))

    if day in ("now", "right now", "currently", "current"):
        condition = _WMO_CODES.get(current["weather_code"], f"code {current['weather_code']}")
        return SkillResult(
            text=f"Right now: {current['temperature_2m']}°C, {condition}, wind {current['wind_speed_10m']} km/h"
        )

    # "today" and anything else (e.g. "does it rain today") — the whole
    # day's data, including rain chance, so the model can answer either
    # kind of question about today from the same result.
    return SkillResult(text=f"Right now: {current['temperature_2m']}°C. Today — {_describe_day(daily, 0)}")
