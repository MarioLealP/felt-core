"""Felt's own switches in HA: quiet, hidden, DJ mode - the same ones as in
the Felt app and on the desktop, so automations can flip them."""

from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .client import FeltError
from .const import DOMAIN
from .coordinator import FeltCoordinator

SWITCHES = (
    ("quiet", "Quiet", "mdi:volume-off"),
    ("hide", "Hidden", "mdi:eye-off"),
    ("dj", "DJ mode", "mdi:music-circle"),
)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator: FeltCoordinator = entry.runtime_data.coordinator
    async_add_entities(FeltSwitch(coordinator, entry, *s) for s in SWITCHES)


class FeltSwitch(CoordinatorEntity[FeltCoordinator], SwitchEntity):
    _attr_has_entity_name = True

    def __init__(
        self, coordinator: FeltCoordinator, entry: ConfigEntry, key: str, name: str, icon: str
    ) -> None:
        super().__init__(coordinator)
        self._key = key
        self._attr_name = name
        self._attr_icon = icon
        self._attr_unique_id = f"{entry.entry_id}_{key}"
        self._attr_device_info = {"identifiers": {(DOMAIN, entry.entry_id)}, "name": "Felt"}

    @property
    def is_on(self) -> bool | None:
        return ((self.coordinator.data or {}).get("controls") or {}).get(self._key)

    async def _set(self, on: bool) -> None:
        try:
            state = await self.coordinator.client.set_control(self._key, on)
        except FeltError as e:
            raise HomeAssistantError(f"Felt: {e}") from e
        data = {**(self.coordinator.data or {}), "controls": state}
        self.coordinator.async_set_updated_data(data)
        if self._key == "dj" and on and not state.get("dj") and state.get("message"):
            # she couldn't start (e.g. Spotify asleep) - say why instead of a silent flip back
            raise HomeAssistantError(state["message"])

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._set(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._set(False)
