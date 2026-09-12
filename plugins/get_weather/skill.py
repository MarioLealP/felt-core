"""Weather via Open-Meteo — free, no API key, no vendor lock-in.

Location comes from config (FELT_HOME_LATITUDE/LONGITUDE), never
hardcoded — unset means the skill reports itself unavailable rather than
silently guessing a location.

2026-09-12 finding: this originally only ever fetched *current*
conditions — asking "what's the weather tomorrow" silently returned
today's weather instead, with no error or indication anything was off.
Now checks the requested day and calls Open-Meteo's daily forecast
endpoint for "tomorrow", current conditions otherwise (unchanged
behavior/API call for the already-verified "right now" case).
"""

import httpx

from felt_core.params import get_single_param
from felt_core.plugin import SkillResult

name = "get_weather"
description = (
    "Gets the weather at the configured home location — for today/right now, "
    "or tomorrow if asked. Use this, NOT web_search, for any weather question."
)
parameters_schema = {
    "type": "object",
    "properties": {
        "day": {
            "type": "string",
            "description": "'today' (default) or 'tomorrow' — only these two are supported",
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


async def _current(client: httpx.AsyncClient, lat: float, lon: float) -> str:
    response = await client.get(
        "https://api.open-meteo.com/v1/forecast",
        params={"latitude": lat, "longitude": lon, "current": "temperature_2m,weather_code,wind_speed_10m"},
    )
    response.raise_for_status()
    current = response.json()["current"]
    condition = _WMO_CODES.get(current["weather_code"], f"code {current['weather_code']}")
    return f"Right now: {current['temperature_2m']}°C, {condition}, wind {current['wind_speed_10m']} km/h"


async def _tomorrow(client: httpx.AsyncClient, lat: float, lon: float) -> str:
    response = await client.get(
        "https://api.open-meteo.com/v1/forecast",
        params={
            "latitude": lat,
            "longitude": lon,
            "daily": "temperature_2m_max,temperature_2m_min,weather_code",
            "forecast_days": 2,
            "timezone": "auto",
        },
    )
    response.raise_for_status()
    daily = response.json()["daily"]
    condition = _WMO_CODES.get(daily["weather_code"][1], f"code {daily['weather_code'][1]}")
    return (
        f"Tomorrow ({daily['time'][1]}): {condition}, "
        f"high {daily['temperature_2m_max'][1]}°C, low {daily['temperature_2m_min'][1]}°C"
    )


async def handle(params, ctx):
    config = ctx.config
    if config.home_latitude is None or config.home_longitude is None:
        return SkillResult(text="Weather isn't configured (no home location set).")

    day = str(get_single_param(params, "day", "when", "date") or "today").lower()

    async with httpx.AsyncClient(timeout=20) as client:
        if "tomorrow" in day:
            text = await _tomorrow(client, config.home_latitude, config.home_longitude)
        else:
            text = await _current(client, config.home_latitude, config.home_longitude)

    return SkillResult(text=text)
