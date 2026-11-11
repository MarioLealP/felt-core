"""Settings: /v1/settings/temperature-unit."""

from typing import Literal

from fastapi import APIRouter
from pydantic import BaseModel

from felt_core.config import load_config
from felt_core.db.settings import get_setting, set_setting

router = APIRouter()


class TemperatureUnitRequest(BaseModel):
    unit: Literal["celsius", "fahrenheit"]


@router.post("/v1/settings/temperature-unit")
async def set_temperature_unit(req: TemperatureUnitRequest) -> dict:
    config = load_config()
    set_setting(config.db_path, "temperature_unit", req.unit)
    return {"unit": req.unit}


@router.get("/v1/settings/temperature-unit")
async def get_temperature_unit() -> dict:
    config = load_config()
    return {"unit": get_setting(config.db_path, "temperature_unit", "celsius")}
