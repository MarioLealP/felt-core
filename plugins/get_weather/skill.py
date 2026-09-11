"""Current weather via Open-Meteo — free, no API key, no vendor lock-in.

Location comes from config (FELT_HOME_LATITUDE/LONGITUDE), never
hardcoded — unset means the skill reports itself unavailable rather than
silently guessing a location.
"""

import httpx

from felt_core.plugin import SkillResult

name = "get_weather"
description = (
    "Gets the current weather at the configured home location — use this, "
    "NOT web_search, for any weather question."
)
parameters_schema = {"type": "object", "properties": {}}

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


async def handle(params, ctx):
    config = ctx.config
    if config.home_latitude is None or config.home_longitude is None:
        return SkillResult(text="Weather isn't configured (no home location set).")

    async with httpx.AsyncClient(timeout=20) as client:
        response = await client.get(
            "https://api.open-meteo.com/v1/forecast",
            params={
                "latitude": config.home_latitude,
                "longitude": config.home_longitude,
                "current": "temperature_2m,weather_code,wind_speed_10m",
            },
        )
        response.raise_for_status()
        current = response.json()["current"]

    condition = _WMO_CODES.get(current["weather_code"], f"code {current['weather_code']}")
    return SkillResult(
        text=(
            f"{current['temperature_2m']}°C, {condition}, "
            f"wind {current['wind_speed_10m']} km/h"
        )
    )
