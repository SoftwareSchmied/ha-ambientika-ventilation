"""Sensor platform for Ambientika."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import PERCENTAGE, EntityCategory, UnitOfTemperature
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import AmbientikaRuntimeData
from .const import AIR_QUALITY_LEVELS, API_TO_HA, FILTER_STATUSES, OPERATING_MODES
from .entity import AmbientikaEntity
from .models import AmbientikaStatus

ValueFn = Callable[[AmbientikaStatus], Any]


@dataclass(frozen=True, kw_only=True)
class AmbientikaSensorDescription(SensorEntityDescription):
    """Describe an Ambientika sensor."""

    value_fn: ValueFn
    enum_values: tuple[str, ...] = ()


SENSORS = (
    AmbientikaSensorDescription(
        key="temperature",
        translation_key="temperature",
        device_class=SensorDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda status: status.temperature,
    ),
    AmbientikaSensorDescription(
        key="humidity",
        translation_key="humidity",
        device_class=SensorDeviceClass.HUMIDITY,
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda status: status.humidity,
    ),
    AmbientikaSensorDescription(
        key="air_quality",
        translation_key="air_quality",
        device_class=SensorDeviceClass.ENUM,
        value_fn=lambda status: status.air_quality,
        enum_values=AIR_QUALITY_LEVELS,
    ),
    AmbientikaSensorDescription(
        key="filter_status",
        translation_key="filter_status",
        device_class=SensorDeviceClass.ENUM,
        value_fn=lambda status: status.filter_status,
        enum_values=FILTER_STATUSES,
    ),
    AmbientikaSensorDescription(
        key="last_operating_mode",
        translation_key="last_operating_mode",
        device_class=SensorDeviceClass.ENUM,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value_fn=lambda status: status.last_operating_mode,
        enum_values=OPERATING_MODES,
    ),
    AmbientikaSensorDescription(
        key="signal_strength",
        translation_key="signal_strength",
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value_fn=lambda status: status.signal_strength,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry[AmbientikaRuntimeData],
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up sensors and add sensors for devices discovered later."""
    coordinator = entry.runtime_data.coordinator
    known: set[str] = set()

    @callback
    def add_new_entities() -> None:
        serials = set(coordinator.data.devices) - known
        entities = [
            AmbientikaSensor(coordinator, serial, description)
            for serial in serials
            for description in SENSORS
        ]
        if entities:
            async_add_entities(entities)
            known.update(serials)

    add_new_entities()
    entry.async_on_unload(coordinator.async_add_listener(add_new_entities))


class AmbientikaSensor(AmbientikaEntity, SensorEntity):
    """Represent a measurement or diagnostic value."""

    entity_description: AmbientikaSensorDescription

    def __init__(
        self,
        coordinator: Any,
        serial: str,
        description: AmbientikaSensorDescription,
    ) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator, serial, description.key)
        self.entity_description = description
        if description.enum_values:
            self._attr_options = [API_TO_HA[value] for value in description.enum_values]

    @property
    def native_value(self) -> Any:
        """Return the parsed and normalized sensor value."""
        if self.status is None:
            return None
        value = self.entity_description.value_fn(self.status)
        if self.entity_description.enum_values:
            return API_TO_HA.get(value)
        return value
