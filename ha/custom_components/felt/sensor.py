"""The Systems summary as sensors.

One per area (state = status, like the Felt app's dots), "needs you" in total,
and the latest incidents. Plus today's brief (state = when she gave it, the
text as an attribute).
"""

from __future__ import annotations

from datetime import datetime

from homeassistant.components.sensor import SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import FeltCoordinator

AREAS = {
    "server": ("Server", "mdi:server"),
    "backups": ("Backups", "mdi:backup-restore"),
    "security": ("Security", "mdi:shield-half-full"),
    "desktop": ("Desktop", "mdi:monitor"),
    "laptop": ("Laptop", "mdi:laptop"),
    "router": ("Router", "mdi:router-network"),
}
STATUSES = ["needs-you", "looking", "watching", "fine"]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback) -> None:  # ruff: ignore[unused-async]  # async by contract
    """Add one sensor per area, plus the needs-you and brief sensors."""
    coordinator: FeltCoordinator = entry.runtime_data.coordinator
    async_add_entities(
        [AreaSensor(coordinator, entry, key, *meta) for key, meta in AREAS.items()]
        + [NeedsYouSensor(coordinator, entry), BriefSensor(coordinator, entry)]
    )


class _Base(CoordinatorEntity[FeltCoordinator], SensorEntity):
    _attr_has_entity_name = True

    def __init__(self, coordinator: FeltCoordinator, entry: ConfigEntry, key: str) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_systems_{key}"
        self._attr_device_info = {"identifiers": {(DOMAIN, entry.entry_id)}, "name": "Felt"}

    @property
    def _systems(self) -> dict:
        return (self.coordinator.data or {}).get("systems") or {}


class AreaSensor(_Base):
    """The sensor for one area's status in the Systems summary."""

    _attr_device_class = "enum"
    _attr_options = STATUSES

    def __init__(self, coordinator: FeltCoordinator, entry: ConfigEntry, key: str, name: str, icon: str) -> None:
        """Store the area key and set this sensor's name and icon."""
        super().__init__(coordinator, entry, key)
        self._key = key
        self._attr_name = name
        self._attr_icon = icon

    @property
    def native_value(self) -> str | None:
        """This area's status from the Systems summary, or None."""
        return (self._systems.get("areas") or {}).get(self._key, {}).get("status")

    @property
    def extra_state_attributes(self) -> dict:
        """The area's open incident count as {"open": count}."""
        return {"open": (self._systems.get("areas") or {}).get(self._key, {}).get("count", 0)}


class NeedsYouSensor(_Base):
    """The sensor for how many incidents need you."""

    _attr_name = "Needs you"
    _attr_icon = "mdi:alert-circle-outline"
    _attr_native_unit_of_measurement = "incidents"

    def __init__(self, coordinator: FeltCoordinator, entry: ConfigEntry) -> None:
        """Attach the sensor to the needs_you key."""
        super().__init__(coordinator, entry, "needs_you")

    @property
    def native_value(self) -> int | None:
        """How many incidents need you, or None."""
        return self._systems.get("needs_you")

    @property
    def extra_state_attributes(self) -> dict:
        """The latest incidents under "latest"."""
        return {"latest": self._systems.get("latest") or []}


class BriefSensor(_Base):
    """Today's brief: state is when she gave it, the text as an attribute."""

    _attr_name = "Brief"
    _attr_icon = "mdi:script-text-outline"
    _attr_device_class = "timestamp"

    def __init__(self, coordinator: FeltCoordinator, entry: ConfigEntry) -> None:
        """Attach the sensor to the brief key."""
        super().__init__(coordinator, entry, "brief")

    @property
    def _brief(self) -> dict | None:
        return (self.coordinator.data or {}).get("brief")

    @property
    def native_value(self) -> datetime | None:
        """When she gave today's brief, or None."""
        return self._brief["at"] if self._brief else None

    @property
    def extra_state_attributes(self) -> dict:
        """The brief's text under "text"."""
        return {"text": self._brief["text"] if self._brief else ""}
