"""Binary sensor platform for Ambientika."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import AmbientikaRuntimeData
from .entity import AmbientikaEntity
from .models import AmbientikaStatus

IsOnFn = Callable[[AmbientikaStatus], bool | None]


@dataclass(frozen=True, kw_only=True)
class AmbientikaBinarySensorDescription(BinarySensorEntityDescription):
    """Describe an Ambientika binary sensor."""

    is_on_fn: IsOnFn


BINARY_SENSORS = (
    AmbientikaBinarySensorDescription(
        key="humidity_alarm",
        translation_key="humidity_alarm",
        device_class=BinarySensorDeviceClass.PROBLEM,
        is_on_fn=lambda status: status.humidity_alarm,
    ),
    AmbientikaBinarySensorDescription(
        key="filter_problem",
        translation_key="filter_problem",
        device_class=BinarySensorDeviceClass.PROBLEM,
        is_on_fn=lambda status: (
            status.filter_status != "Good" if status.filter_status is not None else None
        ),
    ),
    AmbientikaBinarySensorDescription(
        key="night",
        translation_key="night",
        device_class=BinarySensorDeviceClass.LIGHT,
        is_on_fn=lambda status: status.night_alarm,
    ),
    AmbientikaBinarySensorDescription(
        key="schedule",
        translation_key="schedule",
        is_on_fn=lambda status: (
            status.schedule_state == "On"
            if status.schedule_state not in (None, "NotAvailable")
            else None
        ),
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry[AmbientikaRuntimeData],
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up binary sensors and track newly discovered devices."""
    coordinator = entry.runtime_data.coordinator
    known: set[str] = set()

    @callback
    def add_new_entities() -> None:
        serials = set(coordinator.data.devices) - known
        entities = [
            AmbientikaBinarySensor(coordinator, serial, description)
            for serial in serials
            for description in BINARY_SENSORS
        ]
        if entities:
            async_add_entities(entities)
            known.update(serials)

    add_new_entities()
    entry.async_on_unload(coordinator.async_add_listener(add_new_entities))


class AmbientikaBinarySensor(AmbientikaEntity, BinarySensorEntity):
    """Represent an Ambientika alarm or condition."""

    entity_description: AmbientikaBinarySensorDescription

    def __init__(
        self,
        coordinator: Any,
        serial: str,
        description: AmbientikaBinarySensorDescription,
    ) -> None:
        """Initialize the binary sensor."""
        super().__init__(coordinator, serial, description.key)
        self.entity_description = description

    @property
    def is_on(self) -> bool | None:
        """Return the current condition."""
        if self.status is None:
            return None
        return self.entity_description.is_on_fn(self.status)
