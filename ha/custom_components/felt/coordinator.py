"""Polls Felt's switches (quiet, hide, DJ mode) from felt-core."""

from __future__ import annotations

import logging
from datetime import timedelta

from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .client import FeltClient, FeltError

_LOGGER = logging.getLogger(__name__)


class FeltCoordinator(DataUpdateCoordinator[dict]):
    def __init__(self, hass: HomeAssistant, client: FeltClient) -> None:
        super().__init__(hass, _LOGGER, name="Felt", update_interval=timedelta(seconds=30))
        self.client = client

    async def _async_update_data(self) -> dict:
        try:
            return await self.client.controls()
        except FeltError as e:
            raise UpdateFailed(str(e)) from e
