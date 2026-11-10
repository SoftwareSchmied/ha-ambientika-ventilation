"""Tests for feeding the hub from the coordinator and for command expectations."""

from __future__ import annotations

from dataclasses import replace
from unittest.mock import patch

import pytest
from custom_components.ambientika_ventilation.api import (
    AmbientikaApiError,
    AmbientikaAuthError,
    AmbientikaDeviceOfflineError,
)
from custom_components.ambientika_ventilation.const import DOMAIN
from custom_components.ambientika_ventilation.coordinator import (
    EXPECTED_COMMAND_TTL,
    AmbientikaCoordinator,
)
from custom_components.ambientika_ventilation.datapoints import Source
from custom_components.ambientika_ventilation.hub import AmbientikaHub
from custom_components.ambientika_ventilation.models import (
    AmbientikaData,
    AmbientikaDevice,
    AmbientikaDeviceData,
    AmbientikaStatus,
)
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.update_coordinator import UpdateFailed
from pytest_homeassistant_custom_component.common import MockConfigEntry

from .wiring_helpers import MASTER, mock_api


def make(hass, houses_payload, status_payload):
    entry = MockConfigEntry(domain=DOMAIN, data={})
    entry.add_to_hass(hass)
    api = mock_api(houses_payload[:1], status_payload)
    coordinator = AmbientikaCoordinator(hass, entry, api)
    coordinator.hub = AmbientikaHub()
    return coordinator, api


async def test_successful_polls_feed_hub_and_health(
    hass, houses_payload, status_payload
) -> None:
    coordinator, api = make(hass, houses_payload, status_payload)
    api.metrics.successful_requests = 4
    api.metrics.failed_requests = 1
    api.metrics.last_request_duration_ms = 120
    api.metrics.record_error("timeout", endpoint="/x")
    hub = coordinator.hub

    coordinator.data = await coordinator._async_update_data()

    power = hub.resolve(MASTER, "power")
    assert (power.value, power.source) == (True, Source.CLOUD)
    health = hub.cloud_health
    assert health.requests_total == 5 and health.failures_total == 1
    assert health.last_latency_s == 0.12
    assert health.last_failure_kind == "timeout"
    assert health.failures_by_kind == {"timeout": 1}
    assert health.last_success_at is not None
    assert health.last_change_at is not None  # first poll differs from nothing
    assert health.consecutive_failures == 0
    first_change = health.last_change_at

    with patch.object(hub, "update_cloud", wraps=hub.update_cloud) as update:
        coordinator.data = await coordinator._async_update_data()
    # An unchanged poll still refreshes the hub, but is not a change.
    update.assert_called_once()
    assert hub.cloud_health.last_change_at == first_change
    assert hub.cloud_health.last_success_at >= health.last_success_at


async def test_failed_poll_updates_only_the_health(
    hass, houses_payload, status_payload
) -> None:
    coordinator, api = make(hass, houses_payload, status_payload)
    coordinator.data = await coordinator._async_update_data()
    api.async_houses_info.side_effect = AmbientikaApiError("offline")
    api.async_device_status.side_effect = AmbientikaApiError("offline")
    api.async_house_devices_status.side_effect = AmbientikaApiError("offline")
    coordinator._last_discovery = None  # force discovery to fail too
    coordinator._known_devices = {}

    with pytest.raises(UpdateFailed):
        await coordinator._async_update_data()

    health = coordinator.hub.cloud_health
    assert health.consecutive_failures == 1
    assert health.last_failure_at is not None


async def test_offline_devices_count_as_failed_for_the_hub(
    hass, houses_payload, status_payload
) -> None:
    """The kept status of an offline unit must not make the cloud look fresh."""
    coordinator, _api = make(hass, houses_payload, status_payload)
    data = await coordinator._async_update_data()
    coordinator.data = data

    coordinator._publish_poll(replace(data, offline_devices=frozenset({MASTER})))

    assert coordinator.hub.resolve(MASTER, "power").value is None
    assert coordinator.hub.data_source(MASTER) == "offline"


async def test_polls_without_a_hub_are_unaffected(
    hass, houses_payload, status_payload
) -> None:
    coordinator, _api = make(hass, houses_payload, status_payload)
    coordinator.hub = None
    coordinator.data = await coordinator._async_update_data()
    coordinator._publish_health()  # no hub: nothing to do
    coordinator._clear_expected(MASTER, {"operating_mode": "Off"})
    assert coordinator.consecutive_failures == 0


# --- E6: commands set and clear expected values -------------------------------


def writable(hass, mode: str = "Auto"):
    entry = MockConfigEntry(domain=DOMAIN, data={})
    entry.add_to_hass(hass)
    api = mock_api([], {})
    coordinator = AmbientikaCoordinator(hass, entry, api)
    status = AmbientikaStatus(
        serial_number="serial",
        operating_mode=mode,
        fan_speed="Low",
        humidity_level="Normal",
        light_sensor_level="Low",
    )
    coordinator.data = AmbientikaData(
        devices={
            "serial": AmbientikaDeviceData(
                device=AmbientikaDevice(
                    serial_number="serial",
                    name="Unit",
                    role="Master",
                    house_id=10,
                    zone_index=1,
                ),
                status=status,
            )
        }
    )
    coordinator.hub = AmbientikaHub()
    coordinator.hub.update_cloud(coordinator.data)
    return coordinator, api


async def test_command_sets_the_expected_value_until_the_cloud_confirms(hass) -> None:
    coordinator, api = writable(hass)
    hub = coordinator.hub
    api.async_device_status.side_effect = lambda serial: {
        "deviceSerialNumber": serial,
        "operatingMode": "Off",
        "fanSpeed": "Low",
        "humidityLevel": "Normal",
        "lightSensorLevel": "Low",
    }
    with patch.object(hub, "set_expected", wraps=hub.set_expected) as expected:
        await coordinator.async_write_state("serial", operating_mode="Off")

    assert expected.call_args.args[0] == "serial"
    assert expected.call_args.args[1]["operating_mode"] == "Off"
    assert expected.call_args.kwargs == {"ttl": EXPECTED_COMMAND_TTL}
    power = hub.resolve("serial", "power")
    assert (power.value, power.source) == (False, Source.EXPECTED)

    # The next poll that shows the new state removes the expectation.
    hub.update_cloud(coordinator.data)
    power = hub.resolve("serial", "power")
    assert (power.value, power.source) == (False, Source.CLOUD)


@pytest.mark.parametrize(
    ("error", "translation_key"),
    [
        (AmbientikaApiError("boom"), "cloud_command_failed"),
        (AmbientikaDeviceOfflineError("offline"), "device_offline"),
        (AmbientikaAuthError("denied"), "auth_failed"),
    ],
)
async def test_failed_command_clears_the_expected_value(
    hass, error, translation_key
) -> None:
    coordinator, api = writable(hass)
    api.async_change_mode.side_effect = error
    with (
        patch.object(coordinator.config_entry, "async_start_reauth"),
        pytest.raises(HomeAssistantError) as raised,
    ):
        await coordinator.async_write_state("serial", operating_mode="Off")

    assert raised.value.translation_key == translation_key
    power = coordinator.hub.resolve("serial", "power")
    assert (power.value, power.source) == (True, Source.CLOUD)


async def test_settings_entities_never_follow_udp(hass) -> None:
    """UDP can neither confirm nor contradict a setting (E6)."""
    from custom_components.ambientika_ventilation.local.models import (
        Coupling,
        Direction,
    )

    from .hub_helpers import cloud as cloud_data
    from .hub_helpers import device as device_data
    from .hub_helpers import local, make_hub

    hub, clock = make_hub()
    hub.update_cloud(cloud_data(device_data(MASTER, mode="AwayHome")))
    hub.update_local(
        local(clock, n=1, coupling=Coupling.IDLE, direction=Direction.STOPPED)
    )
    clock.advance(1)
    hub.update_local(
        local(clock, n=2, coupling=Coupling.IDLE, direction=Direction.STOPPED)
    )
    for key in ("operating_mode", "fan_speed", "humidity_level", "light_sensor_level"):
        assert hub.resolve(MASTER, key).source in (Source.CLOUD, None)
    assert hub.resolve(MASTER, "operating_mode").value == "AwayHome"
    assert hub.resolve(MASTER, "power").value is True
