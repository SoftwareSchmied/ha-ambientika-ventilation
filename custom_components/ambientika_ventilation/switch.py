"""Switch platform for Ambientika schedule control."""

from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import AmbientikaRuntimeData
from .entity import AmbientikaEntity
from .models import is_controllable_device

PARALLEL_UPDATES = 1


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry[AmbientikaRuntimeData],
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up schedule switches when devices report schedule support."""
    coordinator = entry.runtime_data.coordinator
    known: set[str] = set()

    @callback
    def add_new_entities() -> None:
        serials = {
            serial
            for serial, device_data in coordinator.data.devices.items()
            if device_data.status is not None
            and device_data.status.schedule_state not in (None, "NotAvailable")
            and is_controllable_device(device_data.device, device_data.status)
        } - known
        if serials:
            async_add_entities(
                AmbientikaScheduleSwitch(coordinator, serial) for serial in serials
            )
            known.update(serials)

    add_new_entities()
    entry.async_on_unload(coordinator.async_add_listener(add_new_entities))


class AmbientikaScheduleSwitch(AmbientikaEntity, SwitchEntity):
    """Enable or disable the configured weekly schedule."""

    _attr_translation_key = "schedule"

    def __init__(self, coordinator: Any, serial: str) -> None:
        """Initialize the schedule switch."""
        super().__init__(coordinator, serial, "schedule_control")

    @property
    def is_on(self) -> bool | None:
        """Return whether schedule mode is active."""
        if self.status is None or self.status.schedule_state == "NotAvailable":
            return None
        return self.status.schedule_state == "On"

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Enable schedule mode while preserving all other settings."""
        await self.coordinator.async_write_state(self._serial, schedule_mode=True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Disable schedule mode while preserving all other settings."""
        await self.coordinator.async_write_state(self._serial, schedule_mode=False)
