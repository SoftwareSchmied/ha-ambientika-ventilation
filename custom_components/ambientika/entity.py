"""Shared entity helpers for Ambientika."""

from __future__ import annotations

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import AmbientikaCoordinator
from .models import AmbientikaDeviceData, AmbientikaStatus


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
        self._attr_unique_id = f"{serial}_{entity_key}"

    @property
    def device_data(self) -> AmbientikaDeviceData:
        """Return the current combined device data."""
        return self.coordinator.data.devices[self._serial]

    @property
    def status(self) -> AmbientikaStatus | None:
        """Return the latest device status."""
        return self.device_data.status

    @property
    def available(self) -> bool:
        """Keep last good values, but require at least one successful status."""
        return super().available and self.status is not None

    @property
    def device_info(self) -> DeviceInfo:
        """Return Home Assistant device registry metadata."""
        device = self.device_data.device
        model = device.device_type or "Ambientika ventilation unit"
        if device.device_subtype and device.device_subtype != "None":
            model = f"{model} {device.device_subtype}"
        firmware_parts = [device.micro_firmware, device.radio_firmware]
        return DeviceInfo(
            identifiers={(DOMAIN, self._serial)},
            manufacturer="Südwind",
            name=device.name,
            model=model,
            sw_version=" / ".join(part for part in firmware_parts if part) or None,
        )
