"""Tests for topology cache, cloud health and malformed inputs."""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest
from custom_components.ambientika_ventilation.datapoints import Source
from custom_components.ambientika_ventilation.hub import (
    TOPOLOGY_VERSION,
    CloudHealth,
)
from custom_components.ambientika_ventilation.local.models import Direction

from .hub_helpers import HOUSE, MASTER, SLAVE, cloud, device, local, make_hub


def test_topology_round_trip_through_json() -> None:
    hub, _clock = make_hub()
    hub.update_cloud(cloud(device(MASTER), device(SLAVE, role="SlaveOppositeMaster")))
    exported = hub.export_topology()
    assert exported["version"] == TOPOLOGY_VERSION
    assert exported["devices"][SLAVE] == {
        "house_id": HOUSE,
        "zone_index": 1,
        "role": "SlaveOppositeMaster",
        "device_type": "Ghost",
    }
    stored = json.loads(json.dumps(exported))

    fresh, _ = make_hub()
    assert fresh.import_topology(stored) == 2
    assert fresh.export_topology() == exported
    assert fresh.serials == {MASTER, SLAVE}
    assert fresh.devices_in_zone(HOUSE, 1) == (MASTER, SLAVE)


def test_local_only_startup_before_first_cloud_data() -> None:
    source, _ = make_hub()
    source.update_cloud(
        cloud(device(MASTER), device(SLAVE, role="SlaveOppositeMaster"))
    )
    hub, clock = make_hub()
    hub.import_topology(source.export_topology())
    assert not hub.cloud_received
    assert hub.data_source(MASTER) == "offline"
    assert hub.resolve(MASTER, "temperature").value is None
    assert hub.update_local(local(clock, n=1)) == (MASTER, SLAVE)
    assert hub.resolve(MASTER, "direction").value is Direction.EXHAUST
    assert hub.resolve(SLAVE, "direction").value is Direction.INTAKE
    power = hub.resolve(MASTER, "power")
    assert (power.value, power.source) == (True, Source.LOCAL_UDP)
    assert hub.data_source(MASTER) == "degraded"
    assert not hub.cloud_reachable()
    # Real cloud data replaces the cache and later imports are ignored.
    hub.update_cloud(cloud(device(MASTER)))
    assert hub.cloud_received
    assert hub.serials == {MASTER}
    assert hub.import_topology(source.export_topology()) == 0
    assert hub.serials == {MASTER}
    assert hub.resolve(MASTER, "temperature").value == 21


@pytest.mark.parametrize(
    "payload",
    [
        None,
        "text",
        [],
        {},
        {"version": 99, "devices": {}},
        {"version": TOPOLOGY_VERSION},
        {"version": TOPOLOGY_VERSION, "devices": []},
    ],
)
def test_import_rejects_malformed_payloads(payload: Any) -> None:
    hub, _ = make_hub()
    assert hub.import_topology(payload) == 0
    assert hub.serials == frozenset()


def test_import_skips_malformed_entries() -> None:
    hub, _ = make_hub()
    loaded = hub.import_topology(
        {
            "version": TOPOLOGY_VERSION,
            "devices": {
                MASTER: {"house_id": True, "zone_index": "1", "role": 5},
                SLAVE: {"house_id": HOUSE, "zone_index": 1, "role": "Master"},
                "": {"house_id": 1},
                7: {"house_id": 1},
                "AA0000000003": "broken",
            },
        }
    )
    assert loaded == 2
    assert hub.export_topology()["devices"][MASTER] == {
        "house_id": None,
        "zone_index": None,
        "role": None,
        "device_type": None,
    }
    assert hub.devices_in_zone(HOUSE, 1) == (SLAVE,)


def test_topology_notifications() -> None:
    hub, _ = make_hub()
    seen: list[frozenset[str]] = []
    unsubscribe = hub.subscribe_topology(seen.append)
    hub.update_cloud(cloud(device(MASTER)))
    hub.update_cloud(cloud(device(MASTER)))  # unchanged
    assert seen == [frozenset({MASTER})]
    hub.update_cloud(cloud(device(MASTER), device(SLAVE, role="SlaveEqualMaster")))
    hub.update_cloud(
        cloud(device(MASTER, zone=2), device(SLAVE, role="SlaveEqualMaster"))
    )
    assert len(seen) == 3
    hub.update_cloud(cloud(device(MASTER)))  # slave removed
    assert seen[-1] == frozenset({MASTER})
    assert hub.resolve(SLAVE, "power").value is None
    unsubscribe()
    unsubscribe()
    hub.update_cloud(cloud())
    assert len(seen) == 4
    assert hub.serials == frozenset()


def test_cloud_health_and_reachability() -> None:
    hub, clock = make_hub()
    seen: list[tuple[bool, CloudHealth]] = []
    unsubscribe = hub.subscribe_hub(lambda ok, health: seen.append((ok, health)))
    assert not hub.cloud_reachable()
    hub.update_cloud(cloud(device()))
    assert [ok for ok, _ in seen] == [True]
    healthy = CloudHealth(
        last_success_at=clock.now,
        requests_total=10,
        failures_total=1,
        last_latency_s=0.4,
    )
    hub.update_cloud_health(healthy)
    hub.update_cloud_health(healthy)  # unchanged
    assert hub.cloud_health is healthy
    assert healthy.success_rate == 0.9
    assert len(seen) == 2
    failing = CloudHealth(
        consecutive_failures=3,
        last_failure_kind="timeout",
        failures_by_kind={"timeout": 3},
    )
    hub.update_cloud_health(failing)
    assert seen[-1][0] is False
    assert not hub.cloud_reachable()
    hub.update_cloud_health(healthy)
    assert seen[-1][0] is True
    clock.advance(200)
    hub.evaluate()  # no poll for too long
    assert seen[-1][0] is False
    unsubscribe()
    hub.update_cloud(cloud(device()))
    assert len(seen) == 5


def test_health_success_rate_without_requests() -> None:
    assert CloudHealth().success_rate is None
    assert CloudHealth(requests_total=2, failures_total=5).success_rate == 0.0


def test_malformed_cloud_inputs_are_ignored() -> None:
    hub, _ = make_hub()
    hub.update_cloud(None)
    hub.update_cloud(SimpleNamespace())  # type: ignore[arg-type]
    hub.update_cloud(SimpleNamespace(devices=[1, 2]))  # type: ignore[arg-type]
    assert not hub.cloud_received
    hub.update_cloud(
        SimpleNamespace(  # type: ignore[arg-type]
            devices={
                5: device(MASTER),
                "no-device": SimpleNamespace(device=None),
                SLAVE: device(SLAVE, role=7),  # type: ignore[arg-type]
            }
        )
    )
    assert hub.serials == {SLAVE}
    assert hub.export_topology()["devices"][SLAVE]["role"] is None
    assert hub.cloud_received


def test_malformed_local_inputs_are_ignored() -> None:
    hub, clock = make_hub()
    hub.update_cloud(cloud(device()))
    assert hub.update_local(None) == ()
    assert hub.resolve(MASTER, "direction").value is Direction.UNKNOWN
    hub.update_local(local(clock, with_frame=False))
    assert hub.resolve(MASTER, "local_link").value is False
    assert hub.resolve(MASTER, "heartbeat_age").value is None
