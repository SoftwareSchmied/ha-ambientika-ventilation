"""Select platform for Ambientika writable settings."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.components.select import SelectEntity, SelectEntityDescription
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import AmbientikaRuntimeData
from .const import (
    API_TO_HA,
    HUMIDITY_LEVELS,
    LIGHT_SENSOR_LEVELS,
    OPERATING_MODES,
    operating_modes_for_device,
)
from .entity import AmbientikaEntity
from .errors import validation_error
from .models import AmbientikaStatus, is_controllable_device

PARALLEL_UPDATES = 1

ValueFn = Callable[[AmbientikaStatus], str | None]
SupportedFn = Callable[[AmbientikaStatus | None], bool]


@dataclass(frozen=True, kw_only=True)
class AmbientikaSelectDescription(SelectEntityDescription):
    """Describe one writable Ambientika enum."""

    api_options: tuple[str, ...]
    value_fn: ValueFn
    write_field: str
    supported_fn: SupportedFn = lambda status: True


SELECTS = (
    AmbientikaSelectDescription(
        key="operating_mode",
        translation_key="operating_mode",
        api_options=OPERATING_MODES,
        value_fn=lambda status: status.operating_mode,
        write_field="operating_mode",
    ),
    AmbientikaSelectDescription(
        key="humidity_level",
        translation_key="humidity_level",
        api_options=HUMIDITY_LEVELS,
        value_fn=lambda status: status.humidity_level,
        write_field="humidity_level",
    ),
    AmbientikaSelectDescription(
        key="light_sensor_level",
        translation_key="light_sensor_level",
        api_options=LIGHT_SENSOR_LEVELS[1:],
        value_fn=lambda status: status.light_sensor_level,
        write_field="light_sensor_level",
        supported_fn=lambda status: (
            status is not None and status.light_sensor_level != "NotAvailable"
        ),
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry[AmbientikaRuntimeData],
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up supported selects and add new capabilities dynamically."""
    coordinator = entry.runtime_data.coordinator
    known: set[tuple[str, str]] = set()

    @callback
    def add_new_entities() -> None:
        entities: list[AmbientikaSelect] = []
        for serial, device_data in coordinator.data.devices.items():
            if device_data.status is None or not is_controllable_device(
                device_data.device, device_data.status
            ):
                continue
            for description in SELECTS:
                key = (serial, description.key)
                if key in known or not description.supported_fn(device_data.status):
                    continue
                known.add(key)
                entities.append(AmbientikaSelect(coordinator, serial, description))
        if entities:
            async_add_entities(entities)

    add_new_entities()
    entry.async_on_unload(coordinator.async_add_listener(add_new_entities))


class AmbientikaSelect(AmbientikaEntity, SelectEntity):
    """Represent a validated writable Ambientika setting."""

    entity_description: AmbientikaSelectDescription

    def __init__(
        self,
        coordinator: Any,
        serial: str,
        description: AmbientikaSelectDescription,
    ) -> None:
        """Initialize the select."""
        super().__init__(coordinator, serial, description.key)
        self.entity_description = description
        api_options = description.api_options
        if description.key == "operating_mode":
            status = self.status
            device_type = self.device_data.device.device_type or (
                status.device_type if status else None
            )
            api_options = operating_modes_for_device(device_type)
        self._attr_options = [API_TO_HA[value] for value in api_options]
        self._api_by_option = {API_TO_HA[value]: value for value in api_options}

    @property
    def current_option(self) -> str | None:
        """Return the normalized current API option."""
        if self.status is None:
            return None
        value = self.entity_description.value_fn(self.status)
        return API_TO_HA.get(value) if value is not None else None

    async def async_select_option(self, option: str) -> None:
        """Validate, write, and read back a selected option."""
        if option not in self._api_by_option:
            raise validation_error(
                "unsupported_select_option",
                placeholders={"option": option},
            )
        await self.coordinator.async_write_state(
            self._serial,
            **{  # type: ignore[arg-type]
                self.entity_description.write_field: self._api_by_option[option]
            },
        )
