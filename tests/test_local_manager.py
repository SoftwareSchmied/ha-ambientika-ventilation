"""Tests for the local reception manager: listeners, probe, store and refreshes."""

from __future__ import annotations

import json
from datetime import timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock

from custom_components.ambientika_ventilation.const import DOMAIN
from custom_components.ambientika_ventilation.datapoints import Source
from custom_components.ambientika_ventilation.local_manager import (
    PROBE_DETECTED,
    PROBE_NOTHING,
    AmbientikaLocal,
    local_mode,
    make_store,
)
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from .hub_helpers import HOUSE, MASTER, SLAVE, cloud, device, local, make_hub
from .wiring_helpers import FakeListener, fake_listeners, zone_state

OTHER_HOUSE = 77


def build(
    hass: HomeAssistant,
    options: dict[str, Any],
    *,
    devices: tuple[Any, ...] | None = None,
):
    entry = MockConfigEntry(domain=DOMAIN, data={}, options=options)
    entry.add_to_hass(hass)
    hub, clock = make_hub()
    hub.update_cloud(cloud(*(devices if devices is not None else (device(MASTER),))))
    coordinator = MagicMock()
    coordinator.async_request_refresh = AsyncMock()
    manager = AmbientikaLocal(
        hass, entry, coordinator, hub, make_store(hass, entry), clock=clock
    )
    manager.attach()
    return manager, hub, clock, coordinator, entry


def stored(entry: MockConfigEntry, *, detected: bool, devices: dict | None = None):
    return {
        "version": 1,
        "minor_version": 1,
        "key": f"{DOMAIN}.{entry.entry_id}",
        "data": {
            "topology": {
                "version": 1,
                "devices": devices
                or {
                    MASTER: {
                        "house_id": HOUSE,
                        "zone_index": 1,
                        "role": "Master",
                        "device_type": "Ghost",
                    }
                },
            },
            "probe": {"detected": detected, "at": "2026-01-01T00:00:00+00:00"},
        },
    }


def test_mode_falls_back_to_auto_for_unknown_values() -> None:
    entry = MagicMock()
    entry.options = {"local_udp": "bogus"}
    assert local_mode(entry) == "auto"
    entry.options = {}
    assert local_mode(entry) == "auto"
    entry.options = {"local_udp": "off"}
    assert local_mode(entry) == "off"


async def test_off_mode_never_listens_or_probes(hass) -> None:
    with fake_listeners() as probe:
        manager, *_ = build(hass, {"local_udp": "off"})
        await manager.async_start()
    assert FakeListener.instances == []
    probe.assert_not_awaited()
    assert manager.diagnostics()["listeners"] == []


async def test_on_mode_starts_one_listener_per_house_and_feeds_the_hub(hass) -> None:
    """Zones of each house are listened to; frames reach the hub."""
    other = device("AA0000000009", house=OTHER_HOUSE, zone=2)
    with fake_listeners():
        manager, hub, clock, coordinator, _entry = build(
            hass, {"local_udp": "on"}, devices=(device(MASTER), other)
        )
        await manager.async_start()
        by_house = {item.house_id: item for item in FakeListener.instances}
        assert set(by_house) == {HOUSE, OTHER_HOUSE}
        assert by_house[HOUSE].zones == {1}
        assert by_house[OTHER_HOUSE].zones == {2}

        by_house[HOUSE].on_state(local(clock, n=1), True)
        await hass.async_block_till_done(wait_background_tasks=True)
        assert hub.resolve(MASTER, "direction").source is Source.LOCAL_UDP
        assert coordinator.async_request_refresh.await_count == 1


async def test_udp_changes_request_a_cloud_refresh_at_most_every_30_seconds(
    hass,
) -> None:
    with fake_listeners():
        manager, _hub, clock, coordinator, _entry = build(hass, {"local_udp": "on"})
        await manager.async_start()
        (listener,) = FakeListener.instances
        refreshes = coordinator.async_request_refresh

        for n in range(1, 4):
            clock.advance(5)
            listener.on_state(local(clock, n=n), True)
        await hass.async_block_till_done(wait_background_tasks=True)
        assert refreshes.await_count == 1

        clock.advance(31)
        listener.on_state(local(clock, n=4), True)
        await hass.async_block_till_done(wait_background_tasks=True)
        assert refreshes.await_count == 2

        # Heartbeats and frames of zones without a device never ask the cloud.
        clock.advance(40)
        listener.on_state(local(clock, n=5), False)
        listener.on_state(local(clock, n=6, zone=9), True)
        await hass.async_block_till_done(wait_background_tasks=True)
        assert refreshes.await_count == 2
        assert manager.diagnostics()["refreshes_requested"] == 2


async def test_topology_changes_update_zones_and_houses(hass) -> None:
    """New zones are bound, removed houses are closed."""
    other = device("AA0000000009", house=OTHER_HOUSE, zone=2)
    with fake_listeners():
        manager, hub, _clock, _coordinator, _entry = build(
            hass, {"local_udp": "on"}, devices=(device(MASTER), other)
        )
        await manager.async_start()
        by_house = {item.house_id: item for item in FakeListener.instances}

        hub.update_cloud(cloud(device(MASTER), device(SLAVE, zone=3)))
        await hass.async_block_till_done(wait_background_tasks=True)

        assert by_house[HOUSE].zone_history[-1] == {1, 3}
        assert by_house[OTHER_HOUSE].stopped
        assert not by_house[HOUSE].stopped
        assert len(FakeListener.instances) == 2

        await manager.async_stop()
        assert by_house[HOUSE].stopped


async def test_topology_changes_before_start_do_not_open_listeners(hass) -> None:
    with fake_listeners():
        _manager, hub, *_ = build(hass, {"local_udp": "off"})
        hub.update_cloud(cloud(device(MASTER), device(SLAVE, zone=3)))
        await hass.async_block_till_done()
    assert FakeListener.instances == []


async def test_auto_mode_enables_the_listener_after_a_successful_probe(
    hass, hass_storage
) -> None:
    """Frames with the account's house id switch local reception on."""
    frames = {1: [object()]}
    with fake_listeners(probe_result=frames) as probe:
        manager, hub, clock, _coordinator, entry = build(hass, {"local_udp": "auto"})
        await manager.async_start()
        assert FakeListener.instances == []  # startup is not blocked by the probe
        await hass.async_block_till_done(wait_background_tasks=True)

        probe.assert_awaited_once()
        assert probe.await_args.args[0] == HOUSE
        assert probe.await_args.args[1] == [1]
        assert probe.await_args.args[2] == 60.0
        assert probe.await_args.kwargs == {"min_frames": 1}
        assert manager.probe_state == PROBE_DETECTED
        (listener,) = FakeListener.instances
        listener.on_state(local(clock, n=1), False)
        assert hub.resolve(MASTER, "direction").source is Source.LOCAL_UDP

        await manager.async_stop()
    saved = hass_storage[f"{DOMAIN}.{entry.entry_id}"]["data"]
    assert saved["probe"]["detected"] is True
    assert MASTER in saved["topology"]["devices"]


async def test_auto_mode_stays_cloud_only_when_nothing_is_heard(
    hass, hass_storage
) -> None:
    """No frames: no listener, and the next start probes again."""
    with fake_listeners(probe_result={1: []}) as probe:
        manager, _hub, _clock, _coordinator, entry = build(hass, {"local_udp": "auto"})
        await manager.async_start()
        await hass.async_block_till_done(wait_background_tasks=True)
        assert manager.probe_state == PROBE_NOTHING
        assert FakeListener.instances == []
        await manager.async_stop()
        key = f"{DOMAIN}.{entry.entry_id}"
        assert hass_storage[key]["data"]["probe"]["detected"] is False

        second, *_ = build_again(hass, entry)
        await second.async_load()
        await second.async_start()
        await hass.async_block_till_done(wait_background_tasks=True)
        assert probe.await_count == 2


def build_again(hass: HomeAssistant, entry: MockConfigEntry):
    hub, clock = make_hub()
    hub.update_cloud(cloud(device(MASTER)))
    coordinator = MagicMock()
    manager = AmbientikaLocal(
        hass, entry, coordinator, hub, make_store(hass, entry), clock=clock
    )
    return manager, hub, clock, coordinator


async def test_auto_mode_skips_the_probe_after_an_earlier_detection(
    hass, hass_storage
) -> None:
    with fake_listeners() as probe:
        manager, _hub, _clock, _coordinator, entry = build(hass, {"local_udp": "auto"})
        hass_storage[f"{DOMAIN}.{entry.entry_id}"] = stored(entry, detected=True)
        await manager.async_load()
        await manager.async_start()
        await hass.async_block_till_done(wait_background_tasks=True)
    probe.assert_not_awaited()
    assert len(FakeListener.instances) == 1


async def test_probe_that_cannot_bind_counts_as_nothing_heard(hass) -> None:
    with fake_listeners(probe_result=OSError(98, "in use")) as probe:
        manager, *_ = build(hass, {"local_udp": "auto"})
        await manager.async_start()
        await hass.async_block_till_done(wait_background_tasks=True)
        probe.assert_awaited_once()
    assert manager.probe_state == PROBE_NOTHING
    assert FakeListener.instances == []


async def test_auto_mode_without_a_known_zone_does_not_probe(hass) -> None:
    with fake_listeners() as probe:
        manager, *_ = build(
            hass,
            {"local_udp": "auto"},
            devices=(device(MASTER, house=None, zone=None),),
        )
        await manager.async_start()
        await hass.async_block_till_done(wait_background_tasks=True)
    probe.assert_not_awaited()
    assert manager.probe_state == "idle"


async def test_cached_topology_is_loaded_and_ignored_after_cloud_data(
    hass, hass_storage
) -> None:
    """A stored topology works until the first poll replaces it."""
    entry = MockConfigEntry(domain=DOMAIN, data={}, options={"local_udp": "off"})
    entry.add_to_hass(hass)
    hass_storage[f"{DOMAIN}.{entry.entry_id}"] = stored(entry, detected=False)
    hub, _clock = make_hub()
    manager = AmbientikaLocal(hass, entry, MagicMock(), hub, make_store(hass, entry))

    await manager.async_load()

    assert hub.serials == {MASTER}
    assert not hub.cloud_received
    assert hub.devices_in_zone(HOUSE, 1) == (MASTER,)


async def test_missing_or_broken_store_is_harmless(hass, hass_storage) -> None:
    manager, hub, *_ = build(hass, {"local_udp": "off"})
    await manager.async_load()  # nothing stored
    assert hub.serials == {MASTER}
    key = f"{DOMAIN}.{manager._entry.entry_id}"
    hass_storage[key] = {
        "version": 1,
        "minor_version": 1,
        "key": key,
        "data": {"topology": "junk", "probe": 3},
    }
    await manager.async_load()
    assert manager._probe_detected is False


async def test_topology_changes_are_saved_after_a_delay(hass, hass_storage) -> None:
    _manager, hub, _clock, _coordinator, entry = build(hass, {"local_udp": "off"})
    key = f"{DOMAIN}.{entry.entry_id}"
    assert key not in hass_storage

    hub.update_cloud(cloud(device(MASTER), device(SLAVE, zone=3)))
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=11))
    await hass.async_block_till_done()

    devices = hass_storage[key]["data"]["topology"]["devices"]
    assert set(devices) == {MASTER, SLAVE}
    assert devices[SLAVE]["zone_index"] == 3
    # Exported placement carries no names or credentials.
    assert set(devices[SLAVE]) == {"house_id", "zone_index", "role", "device_type"}


async def test_diagnostics_hide_house_ids_and_addresses(hass) -> None:
    with fake_listeners():
        manager, _hub, _clock, _coordinator, _entry = build(hass, {"local_udp": "on"})
        await manager.async_start()
        FakeListener.instances[0].on_state(zone_state(n=1), False)
        text = json.dumps(manager.diagnostics())
    assert str(HOUSE) not in text
    assert "192." not in text
    assert "sender" not in text
    info = manager.diagnostics()
    assert info["mode"] == "on" and info["enabled"] is True
    assert info["houses"] == 1
    assert info["listeners"][0]["bound_ports"] == [45001]
    assert info["seconds_since_last_frame"] is not None


async def test_stop_without_start_is_safe(hass) -> None:
    manager, *_ = build(hass, {"local_udp": "off"})
    await manager.async_stop()
    assert manager.diagnostics()["seconds_since_last_frame"] is None
