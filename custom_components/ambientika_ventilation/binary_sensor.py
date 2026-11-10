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
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import AmbientikaRuntimeData
from .analytics_descriptions import DerivedDescription
from .analytics_manager import AmbientikaAnalytics, DerivedTarget
from .const import LOCAL_UDP_OFF
from .coordinator import AmbientikaCoordinator
from .derived_entity import AmbientikaDerivedEntity
from .entity import (
    AmbientikaCloudEntity,
    AmbientikaEntity,
    AmbientikaHubEntity,
    HubFields,
    hub_device_serials,
    hub_entity_wanted,
    with_variants,
)
from .hub import AmbientikaHub
from .models import AmbientikaStatus

PARALLEL_UPDATES = 0

IsOnFn = Callable[[AmbientikaStatus], bool | None]


@dataclass(frozen=True, kw_only=True)
class AmbientikaBinarySensorDescription(BinarySensorEntityDescription):
    """Describe an Ambientika binary sensor."""

    is_on_fn: IsOnFn


@dataclass(frozen=True, kw_only=True)
class AmbientikaHubBinarySensorDescription(BinarySensorEntityDescription, HubFields):
    """Describe a binary sensor whose value comes from the hub."""


HUB_BINARY_SENSORS: tuple[AmbientikaHubBinarySensorDescription, ...] = (
    *with_variants(
        AmbientikaHubBinarySensorDescription(
            key="power",
            translation_key="power",
            datapoint="power",
            device_class=BinarySensorDeviceClass.POWER,
        )
    ),
    *with_variants(
        AmbientikaHubBinarySensorDescription(
            key="running",
            translation_key="running",
            datapoint="running",
            device_class=BinarySensorDeviceClass.RUNNING,
        )
    ),
)

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


def derived_binary_description(
    derived: DerivedDescription,
) -> BinarySensorEntityDescription:
    """Return the Home Assistant description of a derived binary sensor."""
    return BinarySensorEntityDescription(
        key=derived.key,
        translation_key=derived.key,
        device_class=(
            BinarySensorDeviceClass(derived.device_class)
            if derived.device_class
            else None
        ),
        entity_category=(
            EntityCategory(derived.entity_category) if derived.entity_category else None
        ),
        entity_registry_enabled_default=derived.enabled_default,
    )


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry[AmbientikaRuntimeData],
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up binary sensors and track newly discovered devices."""
    coordinator = entry.runtime_data.coordinator
    known: set[str] = set()
    known_connection: set[str] = set()
    known_hub: set[str] = set()
    known_derived: set[str] = set()
    runtime = entry.runtime_data
    hub = runtime.hub
    local_off = runtime.local.mode == LOCAL_UDP_OFF

    @callback
    def add_new_entities() -> None:
        serials = {
            serial
            for serial, device_data in coordinator.data.devices.items()
            if device_data.status is not None
        } - known
        entities: list[BinarySensorEntity] = [
            AmbientikaBinarySensor(coordinator, serial, description)
            for serial in serials
            for description in BINARY_SENSORS
        ]
        connection_serials = {
            serial
            for serial, device_data in coordinator.data.devices.items()
            if device_data.device.role != "NotConfigured"
        } - known_connection
        entities.extend(
            AmbientikaCloudConnection(coordinator, serial)
            for serial in connection_serials
        )
        hub_serials = hub_device_serials(coordinator) - known_hub
        entities.extend(
            AmbientikaHubBinarySensor(
                coordinator, hub, serial, description, runtime.raw_variants
            )
            for serial in sorted(hub_serials)
            for description in HUB_BINARY_SENSORS
            if hub_entity_wanted(description, local_off)
        )
        derived = [
            target
            for target in runtime.analytics.targets("binary_sensor")
            if target.unique_id not in known_derived
        ]
        entities.extend(
            AmbientikaDerivedBinarySensor(runtime.analytics, t) for t in derived
        )
        if entities:
            async_add_entities(entities)
            known_derived.update(target.unique_id for target in derived)
            known.update(serials)
            known_connection.update(connection_serials)
            known_hub.update(hub_serials)

    async_add_entities([AmbientikaCloudReachable(entry.entry_id, hub)])
    add_new_entities()
    entry.async_on_unload(coordinator.async_add_topology_listener(add_new_entities))


class AmbientikaBinarySensor(AmbientikaEntity, BinarySensorEntity):
    """Represent an Ambientika alarm or condition."""

    entity_description: AmbientikaBinarySensorDescription

    def __init__(
        self,
        coordinator: AmbientikaCoordinator,
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


class AmbientikaCloudConnection(AmbientikaEntity, BinarySensorEntity):
    """Show whether the cloud currently has status data from the unit.

    Unlike the other entities this one stays available while the unit is
    offline, so that an outage is visible as "disconnected" instead of
    "unavailable".
    """

    _attr_translation_key = "cloud_connection"
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: AmbientikaCoordinator, serial: str) -> None:
        """Initialize the connection sensor."""
        super().__init__(coordinator, serial, "cloud_connection")

    @property
    def available(self) -> bool:
        """Report a state as long as the device is part of the account."""
        return self._serial in self.coordinator.data.devices

    @property
    def is_on(self) -> bool:
        """Return whether the cloud delivers current status for the unit."""
        data = self.coordinator.data
        return (
            self.coordinator.last_update_success
            and self._serial not in data.offline_devices
            and self._serial not in data.failed_devices
            and self.status is not None
        )


class AmbientikaHubBinarySensor(AmbientikaHubEntity, BinarySensorEntity):
    """Binary sensor whose value is resolved by the hub."""

    entity_description: AmbientikaHubBinarySensorDescription

    def __init__(
        self,
        coordinator: AmbientikaCoordinator,
        hub: AmbientikaHub,
        serial: str,
        description: AmbientikaHubBinarySensorDescription,
        raw_variants: bool = False,
    ) -> None:
        """Initialize the hub-backed binary sensor."""
        super().__init__(coordinator, hub, serial, description, raw_variants)
        self.entity_description = description

    @property
    def is_on(self) -> bool | None:
        """Return the resolved value."""
        value: Any = self.hub_value
        return value if isinstance(value, bool) else None


class AmbientikaCloudReachable(AmbientikaCloudEntity, BinarySensorEntity):
    """Whether the cloud delivered data recently and without repeated failures."""

    _attr_translation_key = "cloud_reachable"
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, entry_id: str, hub: AmbientikaHub) -> None:
        """Initialize the cloud reachability sensor."""
        super().__init__(entry_id, hub, "cloud_reachable")

    @property
    def is_on(self) -> bool:
        """Return whether the cloud is reachable."""
        return self._hub.cloud_reachable()


class AmbientikaDerivedBinarySensor(AmbientikaDerivedEntity, BinarySensorEntity):
    """Binary sensor of one derived condition (see ``analytics_descriptions``)."""

    def __init__(self, analytics: AmbientikaAnalytics, target: DerivedTarget) -> None:
        """Initialize the binary sensor from its derived description."""
        super().__init__(analytics, target)
        self.entity_description = derived_binary_description(target.description)

    @property
    def is_on(self) -> bool | None:
        """Return the last reported condition."""
        return self._value if isinstance(self._value, bool) else None
