"""Shared entity helpers for Ambientika."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from enum import StrEnum
from typing import Any

from homeassistant.core import callback
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity import Entity, EntityDescription
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import CLOUD_DEVICE_PREFIX, DOMAIN, HOUSE_DEVICE_PREFIX, THROTTLE_INTERVAL
from .coordinator import AmbientikaCoordinator, firmware_text
from .datapoints import Source, variant_keys
from .hub import AmbientikaHub, CloudHealth, ResolvedValue
from .models import AmbientikaDevice, AmbientikaDeviceData, AmbientikaStatus


class AmbientikaEntity(CoordinatorEntity[AmbientikaCoordinator]):
    """Base class for entities belonging to one Ambientika device."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: AmbientikaCoordinator,
        serial: str,
        entity_key: str,
    ) -> None:
        """Initialize the entity with stable identifiers."""
        super().__init__(coordinator)
        self._serial = serial
        self._initial_device_data = coordinator.data.devices[serial]
        self._attr_unique_id = f"{serial}_{entity_key}"

    @property
    def device_data(self) -> AmbientikaDeviceData:
        """Return the current combined device data."""
        return self.coordinator.data.devices.get(
            self._serial, self._initial_device_data
        )

    @property
    def status(self) -> AmbientikaStatus | None:
        """Return the latest device status."""
        device_data = self.coordinator.data.devices.get(self._serial)
        return device_data.status if device_data is not None else None

    @property
    def available(self) -> bool:
        """Keep last good values, but require at least one successful status."""
        return (
            super().available
            and self._serial in self.coordinator.data.devices
            and self._serial not in self.coordinator.data.failed_devices
            and self.status is not None
        )

    @property
    def device_info(self) -> DeviceInfo:
        """Return Home Assistant device registry metadata."""
        return unit_device_info(self._serial, self.device_data.device)


def unit_device_info(serial: str, device: AmbientikaDevice) -> DeviceInfo:
    """Return the registry metadata of one unit (also used by derived entities)."""
    model = device.device_type or "Ambientika ventilation unit"
    if device.device_subtype and device.device_subtype != "None":
        model = f"{model} {device.device_subtype}"
    return DeviceInfo(
        identifiers={(DOMAIN, serial)},
        manufacturer="Südwind",
        name=device.name,
        model=model,
        hw_version=(
            device.device_subtype
            if device.device_subtype not in (None, "None")
            else None
        ),
        serial_number=serial,
        suggested_area=device.room_name,
        sw_version=firmware_text(device),
    )


def house_device_info(house_id: int, name: str | None) -> DeviceInfo:
    """Return the registry metadata of the device that carries house-level data."""
    return DeviceInfo(
        identifiers={(DOMAIN, f"{HOUSE_DEVICE_PREFIX}{house_id}")},
        name=name or "Ambientika house",
        manufacturer="Südwind",
        model="House",
        entry_type=DeviceEntryType.SERVICE,
    )


# --- Hub-backed entities ----------------------------------------------------

VARIANT_SOURCES: dict[str, tuple[Source, ...]] = {
    "cloud": (Source.CLOUD, Source.CLOUD_HEURISTIC),
    "local": (Source.LOCAL_TCP, Source.LOCAL_UDP),
}
"""Raw sources shown by the `_cloud` and `_local` variant entities."""


@dataclass(frozen=True, kw_only=True)
class HubFields:
    """Hub-specific fields shared by the descriptions of all platforms."""

    datapoint: str
    """Key in the datapoint table."""
    variant: str | None = None
    """``cloud`` or ``local`` to show one raw source, None for the effective value."""
    throttled: bool = False
    """Volatile value, written at most once per :data:`THROTTLE_INTERVAL`."""
    local_only: bool = False
    """Exists only with local reception (``local_udp`` is not ``off``)."""


def with_variants[D: HubFields](description: D) -> tuple[D, D, D]:
    """Return the effective description and its disabled `_cloud`/`_local` variants.

    The variants are the raw per-source values; they exist for every datapoint
    with several sources but stay disabled unless the user asks for them.
    """
    assert description.datapoint in variant_keys()
    assert isinstance(description, EntityDescription)
    cloud, local = (
        replace(
            description,
            key=f"{description.key}_{variant}",
            translation_key=f"{description.key}_{variant}",
            variant=variant,
            entity_registry_enabled_default=False,
        )
        for variant in VARIANT_SOURCES
    )
    return description, cloud, local


def hub_entity_wanted(fields: HubFields, local_off: bool) -> bool:
    """Return whether a hub entity is created.

    Local-only entities and the raw ``_local`` variants make no sense while the
    local reception is switched off.
    """
    return not (local_off and (fields.local_only or fields.variant == "local"))


def hub_device_serials(coordinator: AmbientikaCoordinator) -> set[str]:
    """Return the devices that get hub-backed entities (zone members)."""
    return {
        serial
        for serial, device_data in coordinator.data.devices.items()
        if device_data.device.role != "NotConfigured"
    }


def clean_hub_value(value: Any) -> Any:
    """Return a state-ready value: enums as text, "unknown" as no value."""
    if isinstance(value, StrEnum):
        value = value.value
    return None if value == "unknown" else value


class AmbientikaHubEntity(AmbientikaEntity):
    """Entity of one device whose value comes from the hub.

    The hub notifies only real changes (heartbeats do not), so the entity
    writes state from its subscription instead of from coordinator updates.
    Volatile attributes are not recorded; the recorded ``source`` attribute has
    few distinct values.
    """

    _unrecorded_attributes = frozenset({"age_s"})

    def __init__(
        self,
        coordinator: AmbientikaCoordinator,
        hub: AmbientikaHub,
        serial: str,
        description: HubFields,
        raw_variants: bool = False,
    ) -> None:
        """Initialize the entity; ``description`` is the platform's description."""
        assert isinstance(description, EntityDescription)
        super().__init__(coordinator, serial, description.key)
        self._hub = hub
        self._hub_fields = description
        self._raw_variants = raw_variants
        self._resolved: ResolvedValue = hub.resolve(serial, description.datapoint)

    @property
    def entity_registry_enabled_default(self) -> bool:
        """Variants are enabled from the start when raw variants are requested."""
        if self._hub_fields.variant is not None and self._raw_variants:
            return True
        return super().entity_registry_enabled_default

    @property
    def hub_value(self) -> Any:
        """Return the effective value or the value of the variant's source."""
        resolved = self._resolved
        variant = self._hub_fields.variant
        if variant is None:
            return clean_hub_value(resolved.value)
        for source in VARIANT_SOURCES[variant]:
            if source in resolved.variants:
                return clean_hub_value(resolved.variants[source])
        return None

    @property
    def available(self) -> bool:
        """Report a state while the hub has a value for the device."""
        return (
            self._serial in self.coordinator.data.devices and self.hub_value is not None
        )

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Expose the winning source (recorded) and the value age (unrecorded)."""
        resolved = self._resolved
        if (
            self._hub_fields.variant is not None
            or self._hub_fields.throttled
            or resolved.source is None
        ):
            return None
        attributes: dict[str, Any] = {"source": resolved.source.value}
        if resolved.age_s is not None:
            attributes["age_s"] = round(resolved.age_s)
        return attributes

    async def async_added_to_hass(self) -> None:
        """Follow the hub for this datapoint."""
        await super().async_added_to_hass()
        self._resolved = self._hub.resolve(self._serial, self._hub_fields.datapoint)
        self.async_on_remove(
            self._hub.subscribe(
                self._serial, self._hub_fields.datapoint, self._on_resolved
            )
        )
        if self._hub_fields.throttled:
            self.async_on_remove(
                async_track_time_interval(
                    self.hass, self._on_throttle_tick, THROTTLE_INTERVAL
                )
            )

    @callback
    def _handle_coordinator_update(self) -> None:
        """Ignore coordinator updates: the hub reports every relevant change."""

    @callback
    def _on_resolved(self, resolved: ResolvedValue) -> None:
        self._resolved = resolved
        self.async_write_ha_state()

    @callback
    def _on_throttle_tick(self, _now: datetime) -> None:
        self._resolved = self._hub.resolve(self._serial, self._hub_fields.datapoint)
        self.async_write_ha_state()


class AmbientikaCloudEntity(Entity):
    """Entity of the account-level cloud device, driven by the hub's health."""

    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, entry_id: str, hub: AmbientikaHub, key: str) -> None:
        """Initialize the entity for the cloud device of one entry."""
        self._hub = hub
        self._attr_unique_id = f"{entry_id}_{key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, f"{CLOUD_DEVICE_PREFIX}{entry_id}")},
            name="Ambientika cloud",
            manufacturer="Südwind",
            entry_type=DeviceEntryType.SERVICE,
        )

    async def async_added_to_hass(self) -> None:
        """Follow reachability and counters of the cloud."""
        self.async_on_remove(self._hub.subscribe_hub(self._on_hub))

    @callback
    def _on_hub(self, _reachable: bool, _health: CloudHealth) -> None:
        self.async_write_ha_state()
