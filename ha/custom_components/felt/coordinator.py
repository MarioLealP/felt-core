"""Polls felt-core for her switches, the Systems summary and today's brief.

Her switches are quiet, hide and DJ mode; the Systems summary is
Zabbix/Wazuh/ntfy as triaged incidents. They arrive as {"controls",
"systems", "brief"}. The brief is looked up every 5 minutes, not every
poll (it pages through her Margins).
"""

from __future__ import annotations

import logging
import time
from datetime import timedelta

from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .brief import todays_brief
from .client import FeltClient, FeltError

BRIEF_EVERY = 300  # seconds

_LOGGER = logging.getLogger(__name__)


class FeltCoordinator(DataUpdateCoordinator[dict]):
    """Poll felt-core on a timer for her controls, systems and brief."""

    def __init__(self, hass: HomeAssistant, client: FeltClient) -> None:
        """Store the felt-core client and set a 30-second poll interval."""
        super().__init__(hass, _LOGGER, name="Felt", update_interval=timedelta(seconds=30))
        self.client = client
        self._brief: dict | None = None
        self._brief_checked: float | None = None

    async def _async_update_data(self) -> dict:
        try:
            controls = await self.client.controls()
            systems = await self.client.systems()
        except FeltError as e:
            raise UpdateFailed(str(e)) from e
        return {"controls": controls, "systems": systems, "brief": await self._todays_brief()}

    async def _todays_brief(self) -> dict | None:
        now = time.monotonic()
        if self._brief_checked is not None and now - self._brief_checked < BRIEF_EVERY:
            return self._brief
        try:
            self._brief = await todays_brief(
                lambda path: self.client.app_call("GET", path),
                dt_util.now().date(),
                dt_util.get_default_time_zone(),
            )
            self._brief_checked = now
        except FeltError as e:
            _LOGGER.debug("brief: %s", e)  # keep the last one; try again next poll
        return self._brief
