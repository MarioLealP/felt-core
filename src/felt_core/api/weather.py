"""GET /v1/weather/now."""

from fastapi import APIRouter

from felt_core.brain import (
    current_weather,
)

router = APIRouter()


@router.get("/v1/weather/now")
async def weather_now_endpoint() -> dict:
    """WP-21: the current weather condition from the WP-06 precompute cache
    (refreshed every 15 min, stale after 20) - felt-animation polls it for
    weather props. `condition` is "rain" / "sun" / "snow" / "storm" or None.
    """
    return current_weather()
