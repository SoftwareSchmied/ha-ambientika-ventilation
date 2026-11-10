"""Sensor platform for Ambientika."""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    PERCENTAGE,
    EntityCategory,
    UnitOfTemperature,
    UnitOfTime,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.util import dt as dt_util

from . import AmbientikaRuntimeData
from .const import (
    AIR_QUALITY_LEVELS,
    API_TO_HA,
    FILTER_STATUSES,
    REPORTED_OPERATING_MODES,
)
from .coordinator import AmbientikaCoordinator
from .entity import (
    AmbientikaCloudEntity,
    AmbientikaEntity,
    AmbientikaHubEntity,
    HubFields,
    hub_device_serials,
    with_variants,
)
from .hub import (
    DATA_SOURCE_CLOUD,
    DATA_SOURCE_DEGRADED,
    DATA_SOURCE_LOCAL,
    DATA_SOURCE_OFFLINE,
    AmbientikaHub,
)
from .local.models import Coupling, Direction, LocalSpeed
from .models import AmbientikaDevice, AmbientikaStatus

ValueFn = Callable[[AmbientikaStatus], Any]
DeviceValueFn = Callable[[AmbientikaDevice], Any]
DeviceAttributesFn = Callable[[AmbientikaDevice], dict[str, Any]]


@dataclass(frozen=True, kw_only=True)
class AmbientikaSensorDescription(SensorEntityDescription):
    """Describe an Ambientika sensor."""

    value_fn: ValueFn
    enum_values: tuple[str, ...] = ()


@dataclass(frozen=True, kw_only=True)
class AmbientikaDeviceSensorDescription(SensorEntityDescription):
    """Describe a static or slowly changing diagnostic value."""

    value_fn: DeviceValueFn
    enum_values: tuple[str, ...] = ()
    attributes_fn: DeviceAttributesFn | None = None


@dataclass(frozen=True, kw_only=True)
class AmbientikaHubSensorDescription(SensorEntityDescription, HubFields):
    """Describe a sensor whose value comes from the hub."""


@dataclass(frozen=True, kw_only=True)
class AmbientikaCloudSensorDescription(SensorEntityDescription):
    """Describe a cloud-health diagnostic of the account."""

    value_fn: Callable[[AmbientikaHub], Any]


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
        key="packet_type",
        translation_key="packet_type",
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value_fn=lambda status: status.packet_type,
    ),
    AmbientikaSensorDescription(
        key="last_operating_mode",
        translation_key="last_operating_mode",
        device_class=SensorDeviceClass.ENUM,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value_fn=lambda status: status.last_operating_mode,
        enum_values=REPORTED_OPERATING_MODES,
    ),
    AmbientikaSensorDescription(
        key="signal_strength",
        translation_key="signal_strength",
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value_fn=lambda status: status.signal_strength,
    ),
)

DEVICE_SENSORS = (
    AmbientikaDeviceSensorDescription(
        key="device_type",
        translation_key="device_type",
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value_fn=lambda device: device.device_type,
    ),
    AmbientikaDeviceSensorDescription(
        key="device_subtype",
        translation_key="device_subtype",
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value_fn=lambda device: device.device_subtype,
    ),
    AmbientikaDeviceSensorDescription(
        key="device_role",
        translation_key="device_role",
        device_class=SensorDeviceClass.ENUM,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value_fn=lambda device: device.role,
        enum_values=(
            "Master",
            "SlaveEqualMaster",
            "SlaveOppositeMaster",
            "NotConfigured",
        ),
    ),
    AmbientikaDeviceSensorDescription(
        key="installation",
        translation_key="installation",
        device_class=SensorDeviceClass.TIMESTAMP,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value_fn=lambda device: device.installation,
    ),
    AmbientikaDeviceSensorDescription(
        key="micro_firmware",
        translation_key="micro_firmware",
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value_fn=lambda device: device.micro_firmware,
    ),
    AmbientikaDeviceSensorDescription(
        key="radio_firmware",
        translation_key="radio_firmware",
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value_fn=lambda device: device.radio_firmware,
    ),
    AmbientikaDeviceSensorDescription(
        key="radio_at_firmware",
        translation_key="radio_at_firmware",
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value_fn=lambda device: device.radio_at_firmware,
    ),
    AmbientikaDeviceSensorDescription(
        key="house",
        translation_key="house",
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value_fn=lambda device: device.house_name,
        attributes_fn=lambda device: _without_none(
            {
                "house_id": device.house_id,
                "address": device.house_address,
                "latitude": device.house_latitude,
                "longitude": device.house_longitude,
                "timezone": device.house_timezone,
                "iana_timezone": device.house_iana_timezone,
                "current_house_time": device.house_current_time,
                "zones_count": device.house_zones_count,
                "devices_count": device.house_devices_count,
            }
        ),
    ),
    AmbientikaDeviceSensorDescription(
        key="zone",
        translation_key="zone",
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value_fn=lambda device: device.zone_name,
        attributes_fn=lambda device: _without_none(
            {"zone_id": device.zone_id, "zone_index": device.zone_index}
        ),
    ),
    AmbientikaDeviceSensorDescription(
        key="room",
        translation_key="room",
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value_fn=lambda device: device.room_name,
        attributes_fn=lambda device: _without_none(
            {
                "room_id": device.room_id,
                "devices_count": device.room_devices_count,
            }
        ),
    ),
    AmbientikaDeviceSensorDescription(
        key="device_id",
        translation_key="device_id",
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value_fn=lambda device: device.device_id,
    ),
    AmbientikaDeviceSensorDescription(
        key="house_id",
        translation_key="house_id",
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value_fn=lambda device: device.house_id,
    ),
    AmbientikaDeviceSensorDescription(
        key="room_id",
        translation_key="room_id",
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value_fn=lambda device: device.room_id,
    ),
    AmbientikaDeviceSensorDescription(
        key="zone_id",
        translation_key="zone_id",
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value_fn=lambda device: device.zone_id,
    ),
    AmbientikaDeviceSensorDescription(
        key="zone_index",
        translation_key="zone_index",
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value_fn=lambda device: device.zone_index,
    ),
)


def _options(enum: type[Direction] | type[Coupling] | type[LocalSpeed]) -> list[str]:
    return [item.value for item in enum if item.value != "unknown"]


HUB_SENSORS: tuple[AmbientikaHubSensorDescription, ...] = (
    AmbientikaHubSensorDescription(
        key="direction",
        translation_key="direction",
        datapoint="direction",
        device_class=SensorDeviceClass.ENUM,
        options=_options(Direction),
    ),
    AmbientikaHubSensorDescription(
        key="physical_stage",
        translation_key="physical_stage",
        datapoint="physical_stage",
        device_class=SensorDeviceClass.ENUM,
        options=_options(LocalSpeed),
    ),
    AmbientikaHubSensorDescription(
        key="coupling",
        translation_key="coupling",
        datapoint="coupling",
        device_class=SensorDeviceClass.ENUM,
        options=_options(Coupling),
    ),
    AmbientikaHubSensorDescription(
        key="data_source",
        translation_key="data_source",
        datapoint="data_source",
        device_class=SensorDeviceClass.ENUM,
        entity_category=EntityCategory.DIAGNOSTIC,
        options=[
            DATA_SOURCE_LOCAL,
            DATA_SOURCE_CLOUD,
            DATA_SOURCE_DEGRADED,
            DATA_SOURCE_OFFLINE,
        ],
    ),
    AmbientikaHubSensorDescription(
        key="heartbeat_age",
        translation_key="heartbeat_age",
        datapoint="heartbeat_age",
        device_class=SensorDeviceClass.DURATION,
        native_unit_of_measurement=UnitOfTime.SECONDS,
        suggested_display_precision=0,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        throttled=True,
    ),
    AmbientikaHubSensorDescription(
        key="last_local_frame",
        translation_key="last_local_frame",
        datapoint="last_seen",
        variant="local",
        device_class=SensorDeviceClass.TIMESTAMP,
        entity_category=EntityCategory.DIAGNOSTIC,
        throttled=True,
    ),
    *with_variants(
        AmbientikaHubSensorDescription(
            key="last_seen",
            translation_key="last_seen",
            datapoint="last_seen",
            device_class=SensorDeviceClass.TIMESTAMP,
            entity_category=EntityCategory.DIAGNOSTIC,
            throttled=True,
        )
    ),
)

CLOUD_SENSORS: tuple[AmbientikaCloudSensorDescription, ...] = (
    AmbientikaCloudSensorDescription(
        key="cloud_consecutive_failures",
        translation_key="cloud_consecutive_failures",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda hub: hub.cloud_health.consecutive_failures,
    ),
    AmbientikaCloudSensorDescription(
        key="cloud_success_rate",
        translation_key="cloud_success_rate",
        native_unit_of_measurement=PERCENTAGE,
        suggested_display_precision=1,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value_fn=lambda hub: (
            None
            if hub.cloud_health.success_rate is None
            else hub.cloud_health.success_rate * 100
        ),
    ),
    AmbientikaCloudSensorDescription(
        key="cloud_last_latency",
        translation_key="cloud_last_latency",
        device_class=SensorDeviceClass.DURATION,
        native_unit_of_measurement=UnitOfTime.SECONDS,
        suggested_display_precision=2,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value_fn=lambda hub: hub.cloud_health.last_latency_s,
    ),
    AmbientikaCloudSensorDescription(
        key="cloud_last_success",
        translation_key="cloud_last_success",
        device_class=SensorDeviceClass.TIMESTAMP,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value_fn=lambda hub: hub.cloud_health.last_success_at,
    ),
    AmbientikaCloudSensorDescription(
        key="cloud_last_failure_kind",
        translation_key="cloud_last_failure_kind",
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value_fn=lambda hub: hub.cloud_health.last_failure_kind,
    ),
)


def _enum_state(value: object, known: tuple[str, ...]) -> str | None:
    """Return the translated state of a known enum value, else unknown."""
    return API_TO_HA.get(value) if isinstance(value, str) and value in known else None


def _without_none(values: dict[str, Any]) -> dict[str, Any]:
    """Remove absent optional metadata from entity attributes."""
    return {key: value for key, value in values.items() if value is not None}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry[AmbientikaRuntimeData],
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up sensors and add sensors for devices discovered later."""
    coordinator = entry.runtime_data.coordinator
    known_devices: set[str] = set()
    known_diagnostics: set[tuple[str, str]] = set()
    known_schedules: set[str] = set()
    known_hub: set[str] = set()
    runtime = entry.runtime_data
    hub = runtime.hub

    @callback
    def add_new_entities() -> None:
        serials = {
            serial
            for serial, device_data in coordinator.data.devices.items()
            if device_data.status is not None
        } - known_devices
        entities: list[SensorEntity] = [
            AmbientikaSensor(coordinator, serial, description)
            for serial in serials
            for description in SENSORS
        ]
        for serial, device_data in coordinator.data.devices.items():
            for description in DEVICE_SENSORS:
                key = (serial, description.key)
                if (
                    key not in known_diagnostics
                    and description.value_fn(device_data.device) is not None
                ):
                    entities.append(
                        AmbientikaDeviceSensor(coordinator, serial, description)
                    )
                    known_diagnostics.add(key)
            if device_data.schedule is not None and serial not in known_schedules:
                entities.append(AmbientikaScheduleSensor(coordinator, serial))
                known_schedules.add(serial)
        hub_serials = hub_device_serials(coordinator) - known_hub
        entities.extend(
            AmbientikaHubSensor(
                coordinator, hub, serial, description, runtime.raw_variants
            )
            for serial in sorted(hub_serials)
            for description in HUB_SENSORS
        )
        if entities:
            async_add_entities(entities)
            known_devices.update(serials)
            known_hub.update(hub_serials)

    async_add_entities(
        AmbientikaCloudSensor(entry.entry_id, hub, description)
        for description in CLOUD_SENSORS
    )
    add_new_entities()
    entry.async_on_unload(coordinator.async_add_topology_listener(add_new_entities))


class AmbientikaSensor(AmbientikaEntity, SensorEntity):
    """Represent a measurement or diagnostic value."""

    entity_description: AmbientikaSensorDescription

    def __init__(
        self,
        coordinator: AmbientikaCoordinator,
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
            return _enum_state(value, self.entity_description.enum_values)
        return value

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Keep the raw value when the cloud sends something not yet known."""
        description = self.entity_description
        if self.status is None or not description.enum_values:
            return None
        value = description.value_fn(self.status)
        if value is not None and _enum_state(value, description.enum_values) is None:
            return {"raw_value": str(value)}
        return None


class AmbientikaDeviceSensor(AmbientikaEntity, SensorEntity):
    """Represent static device metadata as an optional diagnostic entity."""

    entity_description: AmbientikaDeviceSensorDescription

    def __init__(
        self,
        coordinator: AmbientikaCoordinator,
        serial: str,
        description: AmbientikaDeviceSensorDescription,
    ) -> None:
        """Initialize the metadata sensor."""
        super().__init__(coordinator, serial, description.key)
        self.entity_description = description
        if description.enum_values:
            self._attr_options = [API_TO_HA[value] for value in description.enum_values]

    @property
    def available(self) -> bool:
        """Keep metadata available independently of a status packet."""
        return (
            self.coordinator.last_update_success
            and self._serial in self.coordinator.data.devices
            and self.native_value is not None
        )

    @property
    def native_value(self) -> Any:
        """Return the parsed device metadata value."""
        value = self.entity_description.value_fn(self.device_data.device)
        if self.entity_description.enum_values:
            return _enum_state(value, self.entity_description.enum_values)
        return value

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Expose related topology metadata without creating entity clutter."""
        attributes_fn = self.entity_description.attributes_fn
        return attributes_fn(self.device_data.device) if attributes_fn else None


class AmbientikaScheduleSensor(AmbientikaEntity, SensorEntity):
    """Expose the number and content of configured weekly time slots."""

    _attr_translation_key = "schedule_entries"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_entity_registry_enabled_default = False

    def __init__(self, coordinator: AmbientikaCoordinator, serial: str) -> None:
        """Initialize the schedule summary sensor."""
        super().__init__(coordinator, serial, "schedule_entries")

    @property
    def native_value(self) -> int | None:
        """Return the number of configured weekly time slots."""
        schedule = self.device_data.schedule
        return len(schedule.time_slots) if schedule is not None else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return normalized read-only schedule details."""
        schedule = self.device_data.schedule
        if schedule is None:
            return {}
        return {
            "schedule_id": schedule.schedule_id,
            "zone_id": schedule.zone_id,
            "house_id": schedule.house_id,
            "device_id": schedule.device_id,
            "time_slots": [
                {
                    "id": slot.slot_id,
                    "day_of_week": slot.day_of_week,
                    "start_time": slot.start_time,
                    "end_time": slot.end_time,
                    "operating_mode": slot.operating_mode,
                    "fan_speed": slot.fan_speed,
                    "humidity_level": slot.humidity_level,
                    "light_sensor_level": slot.light_sensor_level,
                    "schedule_id": slot.schedule_id,
                }
                for slot in schedule.time_slots
            ],
        }


class AmbientikaHubSensor(AmbientikaHubEntity, SensorEntity):
    """Sensor whose value is resolved by the hub from cloud and local data."""

    entity_description: AmbientikaHubSensorDescription

    def __init__(
        self,
        coordinator: AmbientikaCoordinator,
        hub: AmbientikaHub,
        serial: str,
        description: AmbientikaHubSensorDescription,
        raw_variants: bool = False,
    ) -> None:
        """Initialize the hub-backed sensor."""
        super().__init__(coordinator, hub, serial, description, raw_variants)
        self.entity_description = description

    @property
    def native_value(self) -> Any:
        """Return the resolved value; enum values outside the options are unknown."""
        value = self.hub_value
        options = self.entity_description.options
        if options is not None and value not in options:
            return None
        return value


class AmbientikaCloudSensor(AmbientikaCloudEntity, SensorEntity):
    """Cloud-health diagnostic of the account."""

    entity_description: AmbientikaCloudSensorDescription

    def __init__(
        self,
        entry_id: str,
        hub: AmbientikaHub,
        description: AmbientikaCloudSensorDescription,
    ) -> None:
        """Initialize the cloud-health sensor."""
        super().__init__(entry_id, hub, description.key)
        self.entity_description = description
        self._wall: tuple[float, datetime] | None = None

    @property
    def native_value(self) -> Any:
        """Return the health value; monotonic stamps become wall clock times."""
        value = self.entity_description.value_fn(self._hub)
        if self.entity_description.device_class is not SensorDeviceClass.TIMESTAMP:
            return value
        if value is None:
            return None
        if self._wall is None or self._wall[0] != value:
            age = timedelta(seconds=max(time.monotonic() - value, 0.0))
            self._wall = (value, (dt_util.utcnow() - age).replace(microsecond=0))
        return self._wall[1]

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Show the failure counts by kind next to the last failure kind."""
        if self.entity_description.key != "cloud_last_failure_kind":
            return None
        return dict(self._hub.cloud_health.failures_by_kind) or None
