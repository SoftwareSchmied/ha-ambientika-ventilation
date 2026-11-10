"""Fan platform for Ambientika ventilation units."""

from __future__ import annotations

import math
from typing import Any

import voluptuous as vol
from homeassistant.components.fan import FanEntity, FanEntityFeature
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_platform
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import AmbientikaRuntimeData
from .const import (
    API_TO_HA,
    HUMIDITY_LEVELS,
    LIGHT_SENSOR_LEVELS,
    OPERATING_MODES,
    USER_OPERATING_MODES,
    WRITABLE_FAN_SPEEDS,
    mode_allows_setting,
    operating_modes_for_device,
)
from .coordinator import AmbientikaCoordinator
from .entity import AmbientikaEntity
from .errors import validation_error
from .models import is_controllable_device, turbo_usable

PARALLEL_UPDATES = 1

SERVICE_SET_STATE = "set_state"
# Fields of the set_state action: Home Assistant option names and API values.
STATE_FIELDS: dict[str, tuple[str, ...]] = {
    "operating_mode": OPERATING_MODES,
    "fan_speed": WRITABLE_FAN_SPEEDS,
    "humidity_level": HUMIDITY_LEVELS,
    "light_sensor_level": LIGHT_SENSOR_LEVELS[1:],
}
SET_STATE_SCHEMA = vol.Schema(
    {
        vol.Optional(field): vol.In([API_TO_HA[value] for value in values])
        for field, values in STATE_FIELDS.items()
    }
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry[AmbientikaRuntimeData],
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up fan entities and discover future devices without a reload."""
    coordinator = entry.runtime_data.coordinator
    known: set[str] = set()

    @callback
    def add_new_entities() -> None:
        serials = {
            serial
            for serial, device_data in coordinator.data.devices.items()
            if device_data.status is not None
            and is_controllable_device(device_data.device, device_data.status)
        } - known
        if serials:
            async_add_entities(AmbientikaFan(coordinator, serial) for serial in serials)
            known.update(serials)

    platform = entity_platform.async_get_current_platform()
    platform.async_register_entity_service(
        SERVICE_SET_STATE, SET_STATE_SCHEMA.schema, "async_set_state"
    )

    add_new_entities()
    entry.async_on_unload(coordinator.async_add_topology_listener(add_new_entities))


class AmbientikaFan(AmbientikaEntity, FanEntity):
    """Control power and fan speed for one Ambientika unit."""

    _attr_translation_key = "ventilation"

    def __init__(self, coordinator: AmbientikaCoordinator, serial: str) -> None:
        """Initialize an Ambientika fan entity."""
        super().__init__(coordinator, serial, "ventilation")
        self._attr_preset_modes = ["night"]

    @property
    def is_on(self) -> bool | None:
        """Return whether the ventilation unit is operating."""
        if self.status is None or self.status.operating_mode is None:
            return None
        return self.status.operating_mode != "Off"

    @property
    def speed_count(self) -> int:
        """Return the number of user-selectable speeds."""
        return len(self._writable_speeds)

    @property
    def supported_features(self) -> FanEntityFeature:
        """Expose only controls currently enabled by the official app."""
        status = self.status
        if (
            status is None
            or status.filter_status == "Bad"
            or status.schedule_state == "On"
        ):
            return FanEntityFeature(0)
        features = (
            FanEntityFeature.TURN_ON
            | FanEntityFeature.TURN_OFF
            | FanEntityFeature.PRESET_MODE
        )
        if mode_allows_setting(status.operating_mode, "fan_speed"):
            features |= FanEntityFeature.SET_SPEED
        return features

    @property
    def percentage(self) -> int | None:
        """Map the reported fan speed to a Home Assistant percentage."""
        if self.status is None or self.status.fan_speed is None:
            return None
        if not mode_allows_setting(self.status.operating_mode, "fan_speed"):
            return None
        speeds = self._writable_speeds
        try:
            return round((speeds.index(self.status.fan_speed) + 1) * 100 / len(speeds))
        except ValueError:
            return None

    @property
    def _writable_speeds(self) -> tuple[str, ...]:
        """Return speeds supported by this status packet."""
        speeds: tuple[str, ...] = WRITABLE_FAN_SPEEDS[:3]
        if turbo_usable(self.status, self.coordinator.data.feature_flags):
            speeds += ("Turbo",)
        return speeds

    @property
    def preset_mode(self) -> str | None:
        """Represent the API's special Night speed as a fan preset."""
        if self.status is None:
            return None
        if self.status.operating_mode == "Night":
            return "night"
        return None

    async def async_turn_on(
        self,
        percentage: int | None = None,
        preset_mode: str | None = None,
        **kwargs: Any,
    ) -> None:
        """Turn on, restoring the last safe user operating mode."""
        if preset_mode is not None:
            await self.async_set_preset_mode(preset_mode)
            return
        status = self.status
        if status is None:
            return
        mode = status.operating_mode
        device_type = self.device_data.device.device_type or status.device_type
        allowed_modes = operating_modes_for_device(device_type)
        if percentage is not None and not mode_allows_setting(mode, "fan_speed"):
            mode = "ManualHeatRecovery"
        elif mode == "Off":
            mode = (
                status.last_operating_mode
                if status.last_operating_mode in USER_OPERATING_MODES
                and status.last_operating_mode in allowed_modes
                else "ManualHeatRecovery"
            )
        speed = self._speed_for_percentage(percentage) if percentage else None
        await self.coordinator.async_write_state(
            self._serial,
            operating_mode=mode,
            fan_speed=speed,
        )

    async def async_set_state(self, **fields: str) -> None:
        """Set mode, fan speed, humidity target and light sensor in one command."""
        if not fields:
            raise validation_error("set_state_empty")
        api_values: dict[str, str] = {}
        for field, option in fields.items():
            by_option = {API_TO_HA[value]: value for value in STATE_FIELDS[field]}
            if option not in by_option:
                raise validation_error(
                    "unsupported_select_option", placeholders={"option": option}
                )
            api_values[field] = by_option[option]
        if (
            api_values.get("fan_speed") == "Turbo"
            and "Turbo" not in self._writable_speeds
        ):
            raise validation_error(
                "unsupported_select_option", placeholders={"option": "turbo"}
            )
        await self.coordinator.async_write_state(
            self._serial,
            operating_mode=api_values.get("operating_mode"),
            fan_speed=api_values.get("fan_speed"),
            humidity_level=api_values.get("humidity_level"),
            light_sensor_level=api_values.get("light_sensor_level"),
        )

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn the ventilation unit off."""
        await self.coordinator.async_write_state(self._serial, operating_mode="Off")

    async def async_set_percentage(self, percentage: int) -> None:
        """Set a validated fan speed or turn off at zero percent."""
        if percentage == 0:
            await self.async_turn_off()
            return
        await self.coordinator.async_write_state(
            self._serial,
            fan_speed=self._speed_for_percentage(percentage),
        )

    async def async_set_preset_mode(self, preset_mode: str) -> None:
        """Set the special low-noise Night operating state."""
        if preset_mode != "night":
            raise validation_error(
                "unsupported_preset_mode",
                placeholders={"preset": preset_mode},
            )
        await self.coordinator.async_write_state(
            self._serial,
            operating_mode="Night",
        )

    def _speed_for_percentage(self, percentage: int) -> str:
        """Map 1..100 to the closest supported ordered speed."""
        if not 1 <= percentage <= 100:
            raise validation_error("invalid_fan_percentage")
        speeds = self._writable_speeds
        index = min(len(speeds) - 1, math.ceil(percentage * len(speeds) / 100) - 1)
        return speeds[index]
