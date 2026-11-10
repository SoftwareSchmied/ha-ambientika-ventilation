"""Wiring of the derived-data engine: store, update triggers, commands, filter."""

from __future__ import annotations

import time
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from custom_components.ambientika_ventilation.api import (
    AmbientikaApiError,
    AmbientikaAuthError,
    AmbientikaDeviceOfflineError,
)
from custom_components.ambientika_ventilation.const import DOMAIN
from custom_components.ambientika_ventilation.local.models import Coupling, Direction
from homeassistant.const import EVENT_STATE_CHANGED
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry

from .test_local_wiring import (
    DATA,
    GEMINI,
    entity_id,
    setup_entry,
    state_of,
    tick,
)
from .wiring_helpers import (
    MASTER,
    fake_listeners,
    mock_api,
    patch_session,
    zone_state,
)

STORE_PREFIX = f"{DOMAIN}.analytics."


class Clock:
    """Controllable monotonic clock for the analytics runtime."""

    def __init__(self) -> None:
        self.now = time.monotonic()

    def __call__(self) -> float:
        return self.now


def use_clock(runtime) -> Clock:
    clock = Clock()
    runtime.analytics._clock = clock
    return clock


@pytest.fixture
def cloud(houses_payload, status_payload):
    """Patch the cloud client; units run in Smart/heat recovery by default."""
    api = mock_api(houses_payload, status_payload)
    with patch_session(api), fake_listeners():
        yield api


def counters(entry, serial: str = MASTER) -> dict[str, Any]:
    """Return the persisted command counters of a unit."""
    return entry.runtime_data.analytics.engine.export_state()["devices"][serial][
        "commands"
    ]


# --- store -------------------------------------------------------------------


async def test_setup_restores_the_counters_and_unload_saves_them(
    hass: HomeAssistant, cloud, hass_storage
) -> None:
    """Counters survive a restart through their own store."""
    entry_id = "analytics-entry"
    hass_storage[f"{STORE_PREFIX}{entry_id}"] = {
        "version": 1,
        "minor_version": 1,
        "key": f"{STORE_PREFIX}{entry_id}",
        "data": {
            "version": 1,
            "devices": {MASTER: {"operating_s": 7200.0, "on_s": 7200.0}},
        },
    }
    entry = MockConfigEntry(
        domain=DOMAIN,
        entry_id=entry_id,
        unique_id=f"{DOMAIN}_42",
        data=DATA,
        options={"local_udp": "on"},
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    hours = state_of(hass, "sensor", f"{MASTER}_operating_hours")
    assert float(hours.state) >= 2.0

    assert await hass.config_entries.async_unload(entry.entry_id)
    saved = hass_storage[f"{STORE_PREFIX}{entry_id}"]["data"]
    assert saved["version"] == 1
    assert saved["devices"][MASTER]["operating_s"] >= 7200.0


async def test_counters_are_saved_every_five_minutes_not_rescheduled(
    hass: HomeAssistant, cloud, hass_storage
) -> None:
    """The delayed save is not pushed back by the many updates in between."""
    entry = await setup_entry(hass)
    key = f"{STORE_PREFIX}{entry.entry_id}"
    assert key not in hass_storage

    runtime = entry.runtime_data
    for _ in range(5):
        runtime.analytics.refresh()
    await tick(hass, 120)
    assert key not in hass_storage
    runtime.analytics.refresh()

    await tick(hass, 301)
    assert key in hass_storage
    assert MASTER in hass_storage[key]["data"]["devices"]

    # The next refresh schedules the following write.
    del hass_storage[key]
    runtime.analytics.refresh()
    await tick(hass, 700)
    assert key in hass_storage


async def test_removing_the_entry_deletes_the_analytics_store(
    hass: HomeAssistant, cloud, hass_storage
) -> None:
    """The counters of a removed entry are not kept."""
    entry = await setup_entry(hass)
    key = f"{STORE_PREFIX}{entry.entry_id}"
    assert await hass.config_entries.async_unload(entry.entry_id)
    assert key in hass_storage
    await hass.config_entries.async_remove(entry.entry_id)
    assert key not in hass_storage


# --- update triggers ---------------------------------------------------------


async def test_every_coordinator_update_updates_the_engine(
    hass: HomeAssistant, cloud
) -> None:
    """A poll updates all units; there is no second poll of the cloud."""
    entry = await setup_entry(hass)
    analytics = entry.runtime_data.analytics
    before = analytics.updates
    calls = cloud.async_house_devices_status.await_count

    await entry.runtime_data.coordinator.async_refresh()
    await hass.async_block_till_done()

    assert analytics.updates == before + 1
    assert cloud.async_house_devices_status.await_count == calls + 1
    assert set(analytics.engine.serials) == {MASTER, GEMINI}


async def test_local_changes_update_but_heartbeats_do_not(
    hass: HomeAssistant, cloud
) -> None:
    """Only a changed frame runs the engine."""
    entry = await setup_entry(hass)
    runtime = entry.runtime_data
    analytics = runtime.analytics
    before = analytics.updates

    runtime.local._on_state(zone_state(n=1, wall=dt_util.utcnow()), True)
    await hass.async_block_till_done()
    after_change = analytics.updates
    assert after_change > before

    for n in range(2, 6):
        runtime.local._on_state(zone_state(n=n, wall=dt_util.utcnow()), False)
    await hass.async_block_till_done()
    assert analytics.updates == after_change


async def test_reversals_of_the_local_zone_reach_the_engine(
    hass: HomeAssistant, cloud
) -> None:
    """The snapshot carries the tracker's reversal count of the unit's zone."""
    from dataclasses import replace

    entry = await setup_entry(hass)
    runtime = entry.runtime_data
    state = replace(zone_state(n=1, wall=dt_util.utcnow()), reversals=7)
    runtime.local._on_state(state, True)
    await hass.async_block_till_done()
    snapshot = runtime.analytics._snapshot(runtime.analytics.units()[MASTER])
    assert snapshot.reversals == 7
    assert snapshot.zone == "10_1" and snapshot.house == "10"


async def test_timer_updates_every_minute_while_a_unit_runs(
    hass: HomeAssistant, cloud
) -> None:
    """The 60 s timer feeds a running unit and stays quiet otherwise."""
    entry = await setup_entry(hass)
    analytics = entry.runtime_data.analytics
    before = analytics.updates
    await tick(hass, 61)
    assert analytics.updates > before


async def test_timer_is_idle_while_no_unit_is_on(
    hass: HomeAssistant, houses_payload, status_payload
) -> None:
    """Switched-off units need no minute update."""
    status_payload["operatingMode"] = "Off"
    api = mock_api(houses_payload, status_payload)
    with patch_session(api), fake_listeners():
        entry = await setup_entry(hass)
        analytics = entry.runtime_data.analytics
        before = analytics.updates
        await tick(hass, 61)
        assert analytics.updates == before


async def test_state_is_written_only_for_reported_changes(
    hass: HomeAssistant, cloud
) -> None:
    """Unchanged values and heartbeats never write a state."""
    entry = await setup_entry(hass)
    runtime = entry.runtime_data
    clock = use_clock(runtime)
    target = entity_id(hass, "sensor", "10_house_running_units")
    runtime.analytics.refresh()
    written: list[Any] = []
    hass.bus.async_listen(
        EVENT_STATE_CHANGED,
        lambda event: (
            written.append(event) if event.data["entity_id"] == target else None
        ),
    )

    for _ in range(3):
        clock.now += 70
        runtime.analytics.refresh()
        await hass.async_block_till_done()
    runtime.local._on_state(zone_state(n=9, wall=dt_util.utcnow()), False)
    await hass.async_block_till_done()
    assert written == []


# --- command statistics -------------------------------------------------------


async def test_confirmed_command_counts_sent_and_confirmed(
    hass: HomeAssistant, cloud, status_payload
) -> None:
    """The read-back that shows the command confirms it (source cloud)."""
    entry = await setup_entry(hass)
    coordinator = entry.runtime_data.coordinator
    status_payload["operatingMode"] = "Night"
    cloud.async_change_mode = AsyncMock()

    await coordinator.async_write_state(MASTER, operating_mode="Night")

    assert counters(entry)["sent"] == 1
    assert counters(entry)["confirmed"] == 1
    assert counters(entry)["rejected"] == 0


async def test_failed_commands_are_rejected(hass: HomeAssistant, cloud) -> None:
    """Every failure path of the write counts as rejected."""
    entry = await setup_entry(hass)
    coordinator = entry.runtime_data.coordinator
    for number, error in enumerate(
        (
            AmbientikaApiError("boom"),
            AmbientikaDeviceOfflineError("offline"),
            AmbientikaAuthError("auth"),
        ),
        start=1,
    ):
        cloud.async_change_mode = AsyncMock(side_effect=error)
        with (
            patch.object(entry, "async_start_reauth"),
            pytest.raises(HomeAssistantError),
        ):
            await coordinator.async_write_state(MASTER, operating_mode="Night")
        assert counters(entry)["rejected"] == number
    assert counters(entry)["sent"] == 3
    assert counters(entry)["confirmed"] == 0


async def test_command_without_confirmation_expires_as_unconfirmed(
    hass: HomeAssistant, cloud
) -> None:
    """A command the read-back never shows is counted after 60 s."""
    entry = await setup_entry(hass)
    runtime = entry.runtime_data
    clock = use_clock(runtime)
    cloud.async_change_mode = AsyncMock()

    # The read-back keeps the old mode: never confirmed.
    await runtime.coordinator.async_write_state(MASTER, operating_mode="Night")
    assert counters(entry)["confirmed"] == 0
    assert runtime.analytics.diagnostics()["pending_commands"] == 1

    clock.now += 61
    runtime.analytics._on_tick(dt_util.utcnow())
    assert counters(entry)["unconfirmed"] == 1
    assert runtime.analytics.diagnostics()["pending_commands"] == 0


async def test_fresh_local_frame_confirms_a_matching_command(
    hass: HomeAssistant, cloud
) -> None:
    """Power or coupling shown by a local change confirms with source local."""
    entry = await setup_entry(hass)
    runtime = entry.runtime_data
    analytics = runtime.analytics
    clock = use_clock(runtime)

    token = analytics.command_sent(MASTER, {"operating_mode": "ManualHeatRecovery"})
    clock.now += 2.5
    runtime.local._on_state(zone_state(n=1, wall=dt_util.utcnow()), True)
    await hass.async_block_till_done()
    runtime.local._on_state(zone_state(n=2, wall=dt_util.utcnow()), True)
    await hass.async_block_till_done()
    assert counters(entry)["confirmed"] == 1
    device = analytics.engine._devices[MASTER]
    assert device.latency_local == pytest.approx(2.5)
    # A later change does not confirm the same command again.
    assert analytics._pending[MASTER][token].local_done is True


@pytest.mark.parametrize(
    ("mode", "coupling", "direction", "matches"),
    [
        ("ManualHeatRecovery", Coupling.OPPOSITE, Direction.EXHAUST, True),
        ("Expulsion", Coupling.SAME, Direction.EXHAUST, True),
        ("Smart", Coupling.OPPOSITE, Direction.INTAKE, True),
        ("Expulsion", Coupling.OPPOSITE, Direction.EXHAUST, False),
        ("Smart", Coupling.IDLE, Direction.STOPPED, False),
        ("Off", Coupling.OPPOSITE, Direction.EXHAUST, False),
        ("Off", Coupling.IDLE, Direction.STOPPED, True),
        (None, Coupling.OPPOSITE, Direction.EXHAUST, False),
    ],
)
async def test_local_match_rules(
    hass: HomeAssistant, cloud, mode, coupling, direction, matches
) -> None:
    """Power and coupling decide whether a local frame shows the command."""
    entry = await setup_entry(hass)
    runtime = entry.runtime_data
    runtime.local._on_state(
        zone_state(n=1, wall=dt_util.utcnow(), coupling=coupling, direction=direction),
        False,
    )
    expected = {"operating_mode": mode} if mode else {"fan_speed": "High"}
    assert runtime.analytics._local_matches(MASTER, expected) is matches


async def test_idle_frame_confirms_switching_off(hass: HomeAssistant, cloud) -> None:
    """Off is confirmed locally by the idle coupling."""
    entry = await setup_entry(hass)
    runtime = entry.runtime_data
    use_clock(runtime)
    runtime.analytics.command_sent(MASTER, {"operating_mode": "Off"})
    runtime.local._on_state(
        zone_state(
            n=1,
            wall=dt_util.utcnow(),
            coupling=Coupling.IDLE,
            direction=Direction.STOPPED,
        ),
        True,
    )
    await hass.async_block_till_done()
    assert counters(entry)["confirmed"] == 1


# --- filter reset --------------------------------------------------------------


async def test_filter_reset_resets_the_engine_too(
    hass: HomeAssistant, houses_payload, status_payload
) -> None:
    """The reset button restarts the filter counters of the engine."""
    status_payload["filtersStatus"] = "Bad"
    api = mock_api(houses_payload, status_payload)
    api.async_reset_filter = AsyncMock()
    with patch_session(api), fake_listeners():
        entry = await setup_entry(hass)
        analytics = entry.runtime_data.analytics
        before = analytics.engine.export_state()["devices"][MASTER]["filter_reset"]
        assert before is None

        await entry.runtime_data.coordinator.async_reset_filter(MASTER)
        await hass.async_block_till_done()

        after = analytics.engine.export_state()["devices"][MASTER]
        assert after["filter_reset"] is not None
        assert after["filter_s"] < 5.0
        assert entity_id(hass, "sensor", f"{MASTER}_filter_last_reset")


async def test_failed_filter_reset_leaves_the_engine_alone(
    hass: HomeAssistant, houses_payload, status_payload
) -> None:
    """Only an accepted reset counts."""
    status_payload["filtersStatus"] = "Bad"
    api = mock_api(houses_payload, status_payload)
    api.async_reset_filter = AsyncMock(side_effect=AmbientikaApiError("boom"))
    with patch_session(api), fake_listeners():
        entry = await setup_entry(hass)
        with pytest.raises(HomeAssistantError):
            await entry.runtime_data.coordinator.async_reset_filter(MASTER)
        engine = entry.runtime_data.analytics.engine
        assert engine.export_state()["devices"][MASTER]["filter_reset"] is None


async def test_diagnostics_summarize_the_runtime_without_values(
    hass: HomeAssistant, cloud
) -> None:
    """The counts and settings are exposed, no names, ids or readings."""
    entry = await setup_entry(hass)
    info = entry.runtime_data.analytics.diagnostics()
    assert info == {
        "units": 2,
        "updates": info["updates"],
        "outdoor_configured": False,
        "outdoor_reading": False,
        "room_volumes_configured": 0,
        "filter_basis": "operating",
        "pending_commands": 0,
    }


async def test_listener_errors_do_not_stop_the_update(
    hass: HomeAssistant, cloud, caplog
) -> None:
    """A failing subscriber is logged; others still get their changes."""
    entry = await setup_entry(hass)
    analytics = entry.runtime_data.analytics
    received: list[dict[str, Any]] = []

    def broken(changed: Any) -> None:
        raise RuntimeError("boom")

    off_broken = analytics.subscribe("house:10", broken)
    off_good = analytics.subscribe("house:10", received.append)
    analytics._publish("house:10", {}, {"house_units": 3})
    assert received == [{"house_units": 3}]
    assert "Derived value listener failed" in caplog.text

    off_broken()
    off_broken()  # a second call is harmless
    off_good()
    analytics._publish("house:10", {}, {"house_units": 4})
    assert received == [{"house_units": 3}]
    analytics._listeners.clear()
    off_good()


async def test_units_that_left_the_account_are_forgotten(
    hass: HomeAssistant, cloud
) -> None:
    """Restored counters of a unit the cloud no longer reports are dropped."""
    entry = await setup_entry(hass)
    analytics = entry.runtime_data.analytics
    analytics.engine.import_state(
        {"version": 1, "devices": {"GONE": {"operating_s": 5.0}}}
    )
    assert "GONE" in analytics.engine.serials
    analytics.refresh()
    assert "GONE" not in analytics.engine.serials


async def test_only_expired_commands_leave_the_pending_list(
    hass: HomeAssistant, cloud
) -> None:
    """A recent command stays pending while an old one expires."""
    entry = await setup_entry(hass)
    runtime = entry.runtime_data
    clock = use_clock(runtime)
    analytics = runtime.analytics
    analytics.command_sent(MASTER, {"operating_mode": "Night"})
    clock.now += 50
    analytics.command_sent(MASTER, {"operating_mode": "Smart"})
    clock.now += 20
    analytics._on_tick(dt_util.utcnow())
    assert analytics.diagnostics()["pending_commands"] == 1
    analytics._prune("UNKNOWN", clock.now)


async def test_units_without_a_house_only_get_unit_entities(
    hass: HomeAssistant, cloud
) -> None:
    """Zone and house entities need the house and zone of the unit."""
    from custom_components.ambientika_ventilation.analytics_manager import _Unit

    entry = await setup_entry(hass)
    analytics = entry.runtime_data.analytics
    device = entry.runtime_data.coordinator.data.devices[MASTER].device
    analytics.units = lambda: {MASTER: _Unit(MASTER, device, None, None)}
    targets = analytics.targets("sensor")
    assert targets and {t.description.scope for t in targets} == {"device"}


async def test_poll_listeners_run_on_every_poll_and_survive_errors(
    hass: HomeAssistant, cloud, caplog
) -> None:
    """Unchanged polls reach poll listeners; a failing one is only logged."""
    entry = await setup_entry(hass)
    coordinator = entry.runtime_data.coordinator
    calls: list[int] = []

    def broken() -> None:
        raise RuntimeError("boom")

    remove_broken = coordinator.async_add_poll_listener(broken)
    remove_good = coordinator.async_add_poll_listener(lambda: calls.append(1))
    await coordinator.async_refresh()
    await coordinator.async_refresh()  # identical data: regular listeners stay quiet
    assert calls == [1, 1]
    assert "Poll listener failed" in caplog.text

    remove_broken()
    remove_broken()
    remove_good()
    await coordinator.async_refresh()
    assert calls == [1, 1]


async def test_local_change_listeners_survive_errors(
    hass: HomeAssistant, cloud, caplog
) -> None:
    """A failing change listener is logged and can be removed twice."""
    entry = await setup_entry(hass)
    local = entry.runtime_data.local

    def broken(_serials: tuple[str, ...]) -> None:
        raise RuntimeError("boom")

    remove = local.add_change_listener(broken)
    local._on_state(zone_state(n=1, wall=dt_util.utcnow()), True)
    await hass.async_block_till_done()
    assert "Local change listener failed" in caplog.text
    remove()
    remove()
