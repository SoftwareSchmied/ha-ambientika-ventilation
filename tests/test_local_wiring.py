"""End-to-end tests of the hybrid wiring: setup, entities, options and unload."""

from __future__ import annotations

import time
from datetime import timedelta
from typing import Any
from unittest.mock import patch

import pytest
from custom_components.ambientika_ventilation.const import DOMAIN
from custom_components.ambientika_ventilation.datapoints import Source
from custom_components.ambientika_ventilation.local.models import (
    Coupling,
    Direction,
    LocalSpeed,
)
from homeassistant.const import EVENT_STATE_CHANGED, STATE_UNAVAILABLE
from homeassistant.core import Event, HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from .wiring_helpers import (
    MASTER,
    FakeListener,
    fake_listeners,
    mock_api,
    session_double,
    zone_state,
)

DATA = {
    "username": "user@example.com",
    "password": "password",
    "user_id": 42,
    "token": "token",
    "expires_at": "2099-01-01T00:00:00+00:00",
}
GEMINI = "112233445566"


@pytest.fixture
def cloud(houses_payload, status_payload):
    """Patch the cloud client and session of the setup; yield the API double."""
    api = mock_api(houses_payload, status_payload)
    with (
        patch(
            "custom_components.ambientika_ventilation.create_session",
            return_value=session_double(),
        ),
        patch(
            "custom_components.ambientika_ventilation.AmbientikaApiClient",
            return_value=api,
        ),
        fake_listeners(),
    ):
        yield api


async def setup_entry(
    hass: HomeAssistant, options: dict[str, Any] | None = None
) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=f"{DOMAIN}_42",
        data=DATA,
        options={"local_udp": "on", **(options or {})},
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


def entity_id(hass: HomeAssistant, domain: str, unique_id: str) -> str:
    found = er.async_get(hass).async_get_entity_id(domain, DOMAIN, unique_id)
    assert found, unique_id
    return found


def state_of(hass: HomeAssistant, domain: str, unique_id: str):
    return hass.states.get(entity_id(hass, domain, unique_id))


async def tick(hass: HomeAssistant, seconds: float) -> None:
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=seconds))
    await hass.async_block_till_done()


def frames(runtime, count: int = 2, **kwargs: Any) -> None:
    """Feed local frames of the master's zone through the manager's callback."""
    for n in range(count):
        runtime.local._on_state(
            zone_state(n=n + 1, wall=dt_util.utcnow(), **kwargs), n == 0
        )


# --- setup and unload --------------------------------------------------------


async def test_setup_creates_one_hub_fed_by_the_coordinator(hass, cloud) -> None:
    """The entry owns one hub that already holds the first poll."""
    entry = await setup_entry(hass)
    runtime = entry.runtime_data

    assert runtime.hub.serials == {MASTER, GEMINI}
    power = runtime.hub.resolve(MASTER, "power")
    assert (power.value, power.source) == (True, Source.CLOUD)
    assert runtime.hub.cloud_reachable()
    assert runtime.hub.cloud_health.requests_total >= 0
    assert runtime.coordinator.hub is runtime.hub
    # 2.5 x 60 s is below the minimum of 150 s.
    assert runtime.hub._ttl[Source.CLOUD] == 150.0

    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_cloud_ttl_follows_the_scan_interval(hass, cloud) -> None:
    """A long scan interval stretches the freshness of cloud values."""
    entry = await setup_entry(hass, {"scan_interval": 120})
    assert entry.runtime_data.hub._ttl[Source.CLOUD] == 300.0


async def test_every_poll_refreshes_the_hub_even_when_unchanged(hass, cloud) -> None:
    """The poll time keeps cloud values fresh although no entity is notified."""
    entry = await setup_entry(hass)
    runtime = entry.runtime_data
    first = runtime.hub.resolve(MASTER, "power").age_s
    assert first is not None

    with patch.object(
        runtime.hub, "update_cloud", wraps=runtime.hub.update_cloud
    ) as update:
        await runtime.coordinator.async_refresh()
        await runtime.coordinator.async_refresh()

    assert update.call_count == 2
    assert runtime.hub.resolve(MASTER, "power").age_s < 5


async def test_timer_evaluates_the_hub_every_ten_seconds(hass, cloud) -> None:
    """One timer drives freshness; it is cancelled on unload."""
    entry = await setup_entry(hass)
    with patch.object(entry.runtime_data.hub, "evaluate") as evaluate:
        await tick(hass, 11)
        assert evaluate.call_count == 1
        assert await hass.config_entries.async_unload(entry.entry_id)
        await tick(hass, 22)
    assert evaluate.call_count == 1


async def test_unload_stops_listeners_and_removal_deletes_the_store(
    hass, cloud, hass_storage
) -> None:
    """Unload closes the sockets; removing the entry removes the cache."""
    with fake_listeners():
        entry = await setup_entry(hass, {"local_udp": "on"})
        (listener,) = FakeListener.instances
        assert listener.running
        assert await hass.config_entries.async_unload(entry.entry_id)
        assert listener.stopped
        key = f"{DOMAIN}.{entry.entry_id}"
        assert key in hass_storage  # topology cached when the entry stopped
        await hass.config_entries.async_remove(entry.entry_id)
    assert key not in hass_storage


# --- entities ----------------------------------------------------------------

EFFECTIVE = {
    "binary_sensor": ["power", "running"],
    "sensor": [
        "direction",
        "physical_stage",
        "coupling",
        "data_source",
        "last_seen",
    ],
}
VARIANTS = {
    "binary_sensor": [
        "power_cloud",
        "power_local",
        "running_cloud",
        "running_local",
    ],
    "sensor": ["last_seen_cloud"],
}


async def test_effective_entities_exist_and_variants_are_disabled(hass, cloud) -> None:
    """Unique ids keep their pattern; raw variants start disabled."""
    await setup_entry(hass)
    registry = er.async_get(hass)
    for domain, keys in EFFECTIVE.items():
        for key in keys:
            entry = registry.async_get(entity_id(hass, domain, f"{MASTER}_{key}"))
            assert entry is not None and entry.disabled_by is None, key
    for domain, keys in VARIANTS.items():
        for key in keys:
            entry = registry.async_get(entity_id(hass, domain, f"{MASTER}_{key}"))
            assert entry is not None
            assert entry.disabled_by is er.RegistryEntryDisabler.INTEGRATION, key
            assert hass.states.get(entry.entity_id) is None
    # The local last-seen time replaces the former last local frame entity.
    last_local = registry.async_get(
        entity_id(hass, "sensor", f"{MASTER}_last_seen_local")
    )
    assert last_local is not None and last_local.disabled_by is None
    assert registry.async_get_entity_id("sensor", DOMAIN, f"{MASTER}_last_local_frame") is None
    heartbeat = registry.async_get(entity_id(hass, "sensor", f"{MASTER}_heartbeat_age"))
    assert heartbeat is not None
    assert heartbeat.disabled_by is er.RegistryEntryDisabler.INTEGRATION
    # The existing cloud entities keep their unique ids.
    assert entity_id(hass, "sensor", f"{MASTER}_temperature")
    assert entity_id(hass, "fan", f"{MASTER}_ventilation")


async def test_cloud_health_entities_belong_to_one_service_device(hass, cloud) -> None:
    """Cloud diagnostics hang on an account-level device, not on a unit."""
    entry = await setup_entry(hass)
    registry = er.async_get(hass)
    reachable = registry.async_get(
        entity_id(hass, "binary_sensor", f"{entry.entry_id}_cloud_reachable")
    )
    failures = registry.async_get(
        entity_id(hass, "sensor", f"{entry.entry_id}_cloud_consecutive_failures")
    )
    assert reachable is not None and failures is not None
    assert reachable.device_id == failures.device_id
    assert reachable.disabled_by is None
    assert hass.states.get(reachable.entity_id).state == "on"
    assert hass.states.get(failures.entity_id).state == "0"
    for key in ("success_rate", "last_latency", "last_success", "last_failure_kind"):
        item = registry.async_get(
            entity_id(hass, "sensor", f"{entry.entry_id}_cloud_{key}")
        )
        assert item is not None
        assert item.disabled_by is er.RegistryEntryDisabler.INTEGRATION


async def test_cloud_health_values_follow_the_hub(hass, cloud) -> None:
    """Health entities report the coordinator's metrics and reachability."""
    entry = await setup_entry(hass)
    runtime = entry.runtime_data
    # The detail sensors are disabled by default; build them directly.
    from custom_components.ambientika_ventilation.sensor import (
        CLOUD_SENSORS,
        AmbientikaCloudSensor,
    )

    cloud.metrics.successful_requests = 9
    cloud.metrics.failed_requests = 1
    cloud.metrics.last_request_duration_ms = 250
    cloud.metrics.record_error("timeout", endpoint="/x")
    await runtime.coordinator.async_refresh()
    sensors = {
        d.key: AmbientikaCloudSensor(entry.entry_id, runtime.hub, d)
        for d in CLOUD_SENSORS
    }
    assert sensors["cloud_success_rate"].native_value == 90.0
    assert sensors["cloud_last_latency"].native_value == 0.25
    assert sensors["cloud_last_failure_kind"].native_value == "timeout"
    assert sensors["cloud_last_failure_kind"].extra_state_attributes == {"timeout": 1}
    assert sensors["cloud_consecutive_failures"].extra_state_attributes is None
    stamp = sensors["cloud_last_success"].native_value
    assert stamp is not None and abs((dt_util.utcnow() - stamp).total_seconds()) < 5
    assert sensors["cloud_last_success"].native_value is stamp  # cached, no jitter

    cloud.metrics.successful_requests = 0
    cloud.metrics.failed_requests = 0
    runtime.hub.update_cloud_health(type(runtime.hub.cloud_health)())
    assert sensors["cloud_success_rate"].native_value is None
    assert sensors["cloud_last_success"].native_value is None
    assert sensors["cloud_last_failure_kind"].extra_state_attributes is None


async def test_effective_values_prefer_fresh_local_and_keep_cloud_settings(
    hass, cloud
) -> None:
    """Local direction and stage appear; settings still follow the cloud (E6)."""
    entry = await setup_entry(hass)
    runtime = entry.runtime_data
    assert state_of(hass, "sensor", f"{MASTER}_direction").state == STATE_UNAVAILABLE
    assert state_of(hass, "sensor", f"{MASTER}_data_source").state == "cloud"

    frames(runtime)
    await hass.async_block_till_done()

    assert state_of(hass, "sensor", f"{MASTER}_direction").state == "exhaust"
    assert state_of(hass, "sensor", f"{MASTER}_physical_stage").state == "medium"
    assert state_of(hass, "sensor", f"{MASTER}_coupling").state == "opposite"
    assert state_of(hass, "sensor", f"{MASTER}_data_source").state == "local_udp"
    running = state_of(hass, "binary_sensor", f"{MASTER}_running")
    assert running.state == "on"
    assert running.attributes["source"] == "local_udp"
    assert "age_s" in running.attributes
    # The setting entities keep following the cloud setting.
    assert state_of(hass, "select", f"{MASTER}_operating_mode").state == "heat_recovery"
    assert state_of(hass, "fan", f"{MASTER}_ventilation").state == "on"


async def test_idle_local_frame_keeps_the_cloud_power_and_hides_the_stage(
    hass, cloud
) -> None:
    """Away mode idles locally while the unit is on (corrections 4a and 4b)."""
    entry = await setup_entry(hass)
    frames(
        entry.runtime_data,
        coupling=Coupling.IDLE,
        direction=Direction.STOPPED,
        speed=LocalSpeed.HIGH,
    )
    await hass.async_block_till_done()

    power = state_of(hass, "binary_sensor", f"{MASTER}_power")
    assert power.state == "on"
    assert power.attributes["source"] == "cloud"
    assert state_of(hass, "binary_sensor", f"{MASTER}_running").state == "off"
    assert state_of(hass, "sensor", f"{MASTER}_physical_stage").state == (
        STATE_UNAVAILABLE
    )
    assert state_of(hass, "sensor", f"{MASTER}_coupling").state == "idle"


async def test_heartbeats_do_not_write_states(hass, cloud) -> None:
    """After the first frames, plain heartbeats leave every state untouched."""
    entry = await setup_entry(hass)
    runtime = entry.runtime_data
    frames(runtime)
    await hass.async_block_till_done()
    ours = {
        entity.entity_id
        for entity in er.async_entries_for_config_entry(
            er.async_get(hass), entry.entry_id
        )
    }
    written: list[str] = []

    def record(event: Event) -> None:
        if event.data["entity_id"] in ours:
            written.append(event.data["entity_id"])

    hass.bus.async_listen(EVENT_STATE_CHANGED, record)
    for n in range(3, 8):
        runtime.local._on_state(zone_state(n=n, wall=dt_util.utcnow()), False)
        await hass.async_block_till_done()

    assert written == []


async def test_throttled_entities_write_once_per_minute(hass, cloud) -> None:
    """The local last-seen time follows heartbeats only on the 60 s timer."""
    entry = await setup_entry(hass)
    runtime = entry.runtime_data
    unique = f"{MASTER}_last_seen_local"
    first_wall = (dt_util.utcnow() - timedelta(seconds=30)).replace(microsecond=0)
    runtime.local._on_state(zone_state(n=1, wall=first_wall), True)
    await hass.async_block_till_done()
    first = state_of(hass, "sensor", unique).state
    assert dt_util.parse_datetime(first) == first_wall

    second_wall = dt_util.utcnow().replace(microsecond=0)
    runtime.local._on_state(zone_state(n=2, wall=second_wall), False)
    await hass.async_block_till_done()
    assert state_of(hass, "sensor", unique).state == first

    await tick(hass, 61)
    assert dt_util.parse_datetime(state_of(hass, "sensor", unique).state) == (
        second_wall
    )


async def test_variant_entities_show_the_raw_sources(hass, cloud) -> None:
    """Enabled variants expose the cloud and local raw value of one datapoint."""
    entry = await setup_entry(hass)
    runtime = entry.runtime_data
    from custom_components.ambientika_ventilation.binary_sensor import (
        HUB_BINARY_SENSORS,
        AmbientikaHubBinarySensor,
    )

    by_key = {d.key: d for d in HUB_BINARY_SENSORS}
    coordinator = runtime.coordinator
    effective, cloud_variant, local_variant = (
        AmbientikaHubBinarySensor(
            coordinator, runtime.hub, MASTER, by_key[key], runtime.raw_variants
        )
        for key in ("power", "power_cloud", "power_local")
    )
    assert (effective.is_on, cloud_variant.is_on, local_variant.is_on) == (
        True,
        True,
        None,
    )
    assert not local_variant.available
    assert local_variant.entity_registry_enabled_default is False
    assert effective.entity_registry_enabled_default is True
    assert local_variant.extra_state_attributes is None

    frames(runtime)
    assert local_variant.is_on is None  # not subscribed here, resolved at add time
    local_variant._resolved = runtime.hub.resolve(MASTER, "power")
    assert local_variant.is_on is True
    assert local_variant.available


async def test_raw_variants_option_enables_the_variant_entities(hass, cloud) -> None:
    """With the option on, disabled variants are enabled in the registry."""
    entry = await setup_entry(hass)
    registry = er.async_get(hass)
    variant = entity_id(hass, "binary_sensor", f"{MASTER}_power_local")
    assert (
        registry.async_get(variant).disabled_by is er.RegistryEntryDisabler.INTEGRATION
    )
    user_choice = entity_id(hass, "binary_sensor", f"{MASTER}_running_cloud")
    registry.async_update_entity(user_choice, disabled_by=er.RegistryEntryDisabler.USER)
    assert await hass.config_entries.async_unload(entry.entry_id)

    hass.config_entries.async_update_entry(
        entry, options={"local_udp": "on", "raw_variants": True}
    )
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert registry.async_get(variant).disabled_by is None
    assert hass.states.get(variant) is not None
    assert registry.async_get(user_choice).disabled_by is er.RegistryEntryDisabler.USER
    assert hass.states.get(user_choice) is None
    assert (
        hass.states.get(entity_id(hass, "sensor", f"{MASTER}_last_seen_cloud"))
        is not None
    )


async def test_new_installation_with_raw_variants_creates_them_enabled(
    hass, cloud
) -> None:
    """On a fresh entry the option decides the enabled default."""
    await setup_entry(hass, {"raw_variants": True})
    registry = er.async_get(hass)
    for domain, keys in VARIANTS.items():
        for key in keys:
            entry = registry.async_get(entity_id(hass, domain, f"{MASTER}_{key}"))
            assert entry is not None and entry.disabled_by is None, key
            assert hass.states.get(entry.entity_id) is not None


async def test_hub_entities_ignore_coordinator_updates_and_follow_the_hub(
    hass, cloud
) -> None:
    """Cloud changes reach hub entities through the hub, once."""
    entry = await setup_entry(hass)
    runtime = entry.runtime_data
    power = entity_id(hass, "binary_sensor", f"{MASTER}_power")
    assert hass.states.get(power).state == "on"
    cloud.async_device_status.side_effect = None
    cloud.async_house_devices_status.return_value = {
        "zoneDevicesInfo": [
            {
                "statusPacket": {
                    **cloud.async_house_devices_status.return_value["zoneDevicesInfo"][
                        0
                    ]["statusPacket"],
                    "operatingMode": "Off",
                }
            }
        ],
        "geminiDevicesInfo": [],
    }
    await runtime.coordinator.async_refresh()
    await hass.async_block_till_done()
    assert hass.states.get(power).state == "off"


async def test_data_source_degrades_when_the_cloud_fails(hass, cloud) -> None:
    """Without cloud data but with local frames the source is degraded."""
    entry = await setup_entry(hass)
    runtime = entry.runtime_data
    frames(runtime)
    await hass.async_block_till_done()
    assert state_of(hass, "sensor", f"{MASTER}_data_source").state == "local_udp"
    runtime.hub._ttl[Source.CLOUD] = 0.0  # cloud values expired
    runtime.hub.evaluate()
    await hass.async_block_till_done()
    assert state_of(hass, "sensor", f"{MASTER}_data_source").state == "degraded"
    assert (
        hass.states.get(
            entity_id(hass, "binary_sensor", f"{entry.entry_id}_cloud_reachable")
        ).state
        == "off"
    )


# --- options flow ------------------------------------------------------------


async def test_options_flow_offers_local_options_with_defaults(hass) -> None:
    """The form lists the polling interval, the UDP mode and the raw variants."""
    entry = MockConfigEntry(domain=DOMAIN, data={})
    entry.add_to_hass(hass)

    result = await hass.config_entries.options.async_init(entry.entry_id)

    defaults = {
        str(key): key.default()
        for key in result["data_schema"].schema
        if callable(key.default)
    }
    assert defaults == {
        "scan_interval": 60,
        "local_udp": "auto",
        "raw_variants": False,
        "filter_basis": "operating",
    }


async def test_options_flow_saves_local_options(hass) -> None:
    """Mode and raw variants are stored with the polling interval."""
    entry = MockConfigEntry(domain=DOMAIN, data={})
    entry.add_to_hass(hass)

    with patch.object(hass.config_entries, "async_schedule_reload") as reload:
        result = await hass.config_entries.options.async_init(entry.entry_id)
        result = await hass.config_entries.options.async_configure(
            result["flow_id"],
            {
                "scan_interval": 90,
                "local_udp": "on",
                "raw_variants": True,
                "filter_basis": "operating",
            },
        )

    assert entry.options == {
        "scan_interval": 90,
        "local_udp": "on",
        "raw_variants": True,
        "filter_basis": "operating",
    }
    reload.assert_called_once_with(entry.entry_id)


async def test_options_flow_rejects_an_unknown_mode(hass) -> None:
    """Only auto, on and off are accepted."""
    import voluptuous as vol

    entry = MockConfigEntry(domain=DOMAIN, data={})
    entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    with pytest.raises(vol.Invalid):
        result["data_schema"]({"scan_interval": 60, "local_udp": "sometimes"})


# --- repair issues -----------------------------------------------------------


def issue_ids(hass: HomeAssistant) -> set[str]:
    return {
        issue_id for (domain, issue_id) in ir.async_get(hass).issues if domain == DOMAIN
    }


async def test_bind_failure_in_on_mode_raises_a_repair_issue(hass, cloud) -> None:
    """A port that cannot be bound is reported with its number."""
    with fake_listeners():
        FakeListener.failing_ports = {45001}
        entry = await setup_entry(hass, {"local_udp": "on"})
        assert issue_ids(hass) == {f"local_udp_bind_failed_{entry.entry_id}"}
        issue = ir.async_get(hass).async_get_issue(
            DOMAIN, f"local_udp_bind_failed_{entry.entry_id}"
        )
        assert issue.translation_placeholders == {"ports": "45001"}
        assert issue.translation_key == "local_udp_bind_failed"
        assert not issue.is_fixable

        # Resolves itself once frames arrive.
        FakeListener.failing_ports = set()
        frames(entry.runtime_data)
        assert issue_ids(hass) == set()
        assert await hass.config_entries.async_unload(entry.entry_id)


async def test_missing_frames_in_on_mode_raise_and_clear_an_issue(hass, cloud) -> None:
    """Five silent minutes raise the issue; the first frame removes it."""
    with fake_listeners():
        entry = await setup_entry(hass, {"local_udp": "on"})
        local = entry.runtime_data.local
        local._clock = lambda: time.monotonic() + 301
        local.check()
        assert issue_ids(hass) == {f"local_udp_no_frames_{entry.entry_id}"}
        local.check()  # raised once only
        assert entry.runtime_data.local.diagnostics()["issues"]["no_frames"] is True

        local._clock = time.monotonic
        frames(entry.runtime_data, 1)
        assert issue_ids(hass) == set()
        assert await hass.config_entries.async_unload(entry.entry_id)


async def test_issue_is_withdrawn_on_unload(hass, cloud) -> None:
    """An unloaded entry leaves no stale repair issue behind."""
    with fake_listeners():
        FakeListener.failing_ports = {45001}
        entry = await setup_entry(hass, {"local_udp": "on"})
        assert issue_ids(hass)
        assert await hass.config_entries.async_unload(entry.entry_id)
        assert not issue_ids(hass)


async def test_auto_mode_never_raises_issues(hass, cloud) -> None:
    """Automatic mode degrades silently to cloud-only."""
    with fake_listeners(probe_result={}):
        FakeListener.failing_ports = {45001}
        entry = await setup_entry(hass, {"local_udp": "auto"})
        await hass.async_block_till_done()
        local = entry.runtime_data.local
        local._clock = lambda: time.monotonic() + 1000
        local.check()
        assert not issue_ids(hass)


def test_translations_describe_the_network_requirements() -> None:
    """The repair texts name the three network setups in both languages."""
    import json
    from pathlib import Path

    root = Path(__file__).parents[1] / "custom_components" / DOMAIN
    for language, words in (
        ("en", ("Home Assistant OS", "network_mode: host", "VLAN")),
        ("de", ("Home Assistant OS", "network_mode: host", "VLAN")),
    ):
        data = json.loads(
            (root / "translations" / f"{language}.json").read_text("utf-8")
        )
        for issue in ("local_udp_bind_failed", "local_udp_no_frames"):
            text = data["issues"][issue]["description"]
            assert "{ports}" in text
            assert all(word in text for word in words)
