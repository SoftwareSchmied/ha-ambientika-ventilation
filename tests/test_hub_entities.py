"""Edge cases of hub-backed entities, manager corner cases and diagnostics."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import MagicMock

from custom_components.ambientika_ventilation.binary_sensor import (
    HUB_BINARY_SENSORS,
    AmbientikaHubBinarySensor,
)
from custom_components.ambientika_ventilation.const import DOMAIN
from custom_components.ambientika_ventilation.datapoints import Source
from custom_components.ambientika_ventilation.diagnostics import (
    async_get_config_entry_diagnostics,
    async_get_device_diagnostics,
)
from custom_components.ambientika_ventilation.entity import clean_hub_value
from custom_components.ambientika_ventilation.hub import ResolvedValue
from custom_components.ambientika_ventilation.local.models import Direction
from custom_components.ambientika_ventilation.sensor import (
    HUB_SENSORS,
    AmbientikaHubSensor,
)

from .hub_helpers import HOUSE, MASTER, cloud, device, local, make_hub
from .test_entities import SERIAL, coordinator_with_status
from .test_local_manager import build, stored
from .wiring_helpers import FakeListener, fake_listeners

BY_KEY = {d.key: d for d in (*HUB_SENSORS, *HUB_BINARY_SENSORS)}


def hub_entity(cls, key, hub, coordinator=None, raw=False):
    coordinator = coordinator or coordinator_with_status()
    return cls(coordinator, hub, SERIAL, BY_KEY[key], raw)


def test_clean_hub_value_maps_enums_and_unknown() -> None:
    assert clean_hub_value(Direction.EXHAUST) == "exhaust"
    assert clean_hub_value(Direction.UNKNOWN) is None
    assert clean_hub_value(None) is None
    assert clean_hub_value(True) is True


def test_unknown_device_in_the_hub_gives_unavailable_entities() -> None:
    hub, _clock = make_hub()
    power = hub_entity(AmbientikaHubBinarySensor, "power", hub)
    local_power = hub_entity(AmbientikaHubBinarySensor, "power_local", hub)
    assert power.is_on is None and not power.available
    assert local_power.is_on is None and not local_power.available
    assert power.extra_state_attributes is None


def test_hub_entities_are_unavailable_when_the_device_left_the_account() -> None:
    hub, _clock = make_hub()
    hub.update_cloud(cloud(device(SERIAL)))
    coordinator = coordinator_with_status()
    entity = hub_entity(AmbientikaHubBinarySensor, "power", hub, coordinator)
    assert entity.available
    coordinator.data.devices.clear()
    assert not entity.available


def test_attributes_expose_source_and_unrecorded_age() -> None:
    hub, _clock = make_hub()
    hub.update_cloud(cloud(device(SERIAL)))
    entity = hub_entity(AmbientikaHubBinarySensor, "power", hub)
    assert entity.extra_state_attributes == {"source": "cloud", "age_s": 0}
    assert "age_s" in entity._unrecorded_attributes
    entity._resolved = ResolvedValue("power", True, Source.CLOUD, None, {})
    assert entity.extra_state_attributes == {"source": "cloud"}
    # Throttled values carry no source: it flips with every poll.
    throttled = hub_entity(AmbientikaHubSensor, "last_seen", hub)
    assert throttled.extra_state_attributes is None


def test_enum_sensor_ignores_values_outside_its_options() -> None:
    hub, _clock = make_hub()
    hub.update_cloud(cloud(device(SERIAL)))
    sensor = hub_entity(AmbientikaHubSensor, "coupling", hub)
    sensor._resolved = ResolvedValue("coupling", "sideways", Source.LOCAL_UDP, 0, {})
    assert sensor.native_value is None
    sensor._resolved = ResolvedValue("coupling", "same", Source.LOCAL_UDP, 0, {})
    assert sensor.native_value == "same"


def test_binary_sensor_ignores_non_boolean_values() -> None:
    hub, _clock = make_hub()
    hub.update_cloud(cloud(device(SERIAL)))
    entity = hub_entity(AmbientikaHubBinarySensor, "power", hub)
    entity._resolved = ResolvedValue("power", "x", Source.CLOUD, 0, {})
    assert entity.is_on is None


def test_coordinator_updates_do_not_write_hub_entities() -> None:
    hub, _clock = make_hub()
    entity = hub_entity(AmbientikaHubBinarySensor, "power", hub)
    entity.async_write_ha_state = MagicMock()
    entity._handle_coordinator_update()
    entity.async_write_ha_state.assert_not_called()


# --- manager corner cases ------------------------------------------------------


async def test_sync_while_disabled_does_nothing(hass) -> None:
    with fake_listeners():
        manager, *_ = build(hass, {"local_udp": "on"})
        await manager._async_sync()  # start was never called
        assert FakeListener.instances == []


async def test_auto_mode_with_failing_ports_only_logs(hass, hass_storage) -> None:
    with fake_listeners():
        FakeListener.failing_ports = {45001}
        manager, _hub, _clock, _coordinator, entry = build(hass, {"local_udp": "auto"})
        hass_storage[f"{DOMAIN}.{entry.entry_id}"] = stored(entry, detected=True)
        await manager.async_load()
        await manager.async_start()
        assert manager.diagnostics()["issues"] == {
            "bind_failed": False,
            "no_frames": False,
        }
        await manager.async_stop()


async def test_check_waits_for_five_silent_minutes(hass) -> None:
    with fake_listeners():
        manager, _hub, clock, *_ = build(hass, {"local_udp": "on"})
        await manager.async_start()
        clock.advance(299)
        manager.check()
        assert manager.diagnostics()["issues"]["no_frames"] is False
        clock.advance(2)
        manager.check()
        assert manager.diagnostics()["issues"]["no_frames"] is True
        await manager.async_stop()


# --- diagnostics ----------------------------------------------------------------


def hub_with_local_data():
    hub, clock = make_hub()
    hub.update_cloud(cloud(device(MASTER)))
    hub.update_local(local(clock, n=1))
    clock.advance(1)
    hub.update_local(local(clock, n=2))
    hub.update_local(local(clock, n=1, zone=9))  # unmapped zone
    return hub


def coordinator_for_diagnostics():
    coordinator = coordinator_with_status()
    coordinator.last_successful_update = None
    coordinator.last_update_duration_ms = 1
    coordinator.last_partial_failures = 0
    coordinator.last_failure_causes = ()
    coordinator.discovery_failures = 0
    coordinator.next_discovery_attempt = None
    coordinator.offline_devices = set()
    coordinator.offline_transitions = 0
    coordinator.update_interval = None
    coordinator.base_interval = SimpleNamespace(total_seconds=lambda: 60.0)
    coordinator.consecutive_failures = 0
    coordinator.cycle_durations_ms = []
    coordinator.discarded_polls = 0
    coordinator.device_changes = 0
    coordinator.firmware_history = {}
    coordinator._status_errors = {}
    return coordinator


async def test_hub_diagnostics_hide_identifiers(hass) -> None:
    entry = SimpleNamespace(
        runtime_data=SimpleNamespace(
            coordinator=coordinator_for_diagnostics(),
            api=SimpleNamespace(
                metrics=SimpleNamespace(token_refreshes=0, as_dict=dict),
                token_expires_at=None,
            ),
            hub=hub_with_local_data(),
            local=SimpleNamespace(diagnostics=lambda: {"mode": "on"}),
        ),
        version=1,
        minor_version=1,
        data={},
    )

    result = await async_get_config_entry_diagnostics(hass, entry)

    section = result["hub"]
    assert section["unmapped_local_zones"] == 1
    (live,) = section["devices"]
    assert live["redacted_id"] == f"***{MASTER[-4:]}"
    assert live["data_source"] == "local_udp"
    assert live["local_freshness"] == "fresh"
    assert section["cloud_health"]["requests_total"] == 0
    assert section["cloud_health"]["seconds_since_success"] is None
    assert result["local_udp"] == {"mode": "on"}
    text = json.dumps(result, default=str)
    assert MASTER not in text and str(HOUSE) not in text and "192.0.2" not in text


async def test_device_diagnostics_include_liveness_of_known_devices(hass) -> None:
    hub = hub_with_local_data()
    entry = SimpleNamespace(
        runtime_data=SimpleNamespace(coordinator=coordinator_for_diagnostics(), hub=hub)
    )
    unit = SimpleNamespace(identifiers={(DOMAIN, SERIAL)})

    result = await async_get_device_diagnostics(hass, entry, unit)
    assert result["liveness"] is None  # the hub does not know this serial

    hub.update_cloud(cloud(device(SERIAL, zone=1)))
    result = await async_get_device_diagnostics(hass, entry, unit)
    assert result["liveness"]["data_source"] in {"cloud", "local_udp", "degraded"}
    assert SERIAL not in json.dumps(result, default=str)
