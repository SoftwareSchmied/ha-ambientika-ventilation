"""Fakes and builders for tests of the cloud + local wiring."""

from __future__ import annotations

import time
from collections.abc import Callable, Iterable
from contextlib import contextmanager
from typing import Any, ClassVar
from unittest.mock import AsyncMock, MagicMock, patch

from custom_components.ambientika_ventilation.api import RequestMetrics
from custom_components.ambientika_ventilation.local.listener import UdpBindError
from custom_components.ambientika_ventilation.local.models import (
    Coupling,
    Direction,
    Freshness,
    LocalSpeed,
    LocalZoneState,
    UdpFrame,
)

HOUSE_ID = 10
"""House id of the shared ``houses_payload`` fixture (synthetic)."""
MASTER = "AABBCCDDEEFF"


class FakeListener:
    """Stand-in for ``AmbientikaUdpListener``; no sockets (they are blocked)."""

    instances: ClassVar[list[FakeListener]] = []
    failing_ports: ClassVar[set[int]] = set()

    def __init__(
        self, house_id: int, zones: Iterable[int], on_state: Callable[..., None]
    ) -> None:
        self.house_id = house_id
        self.zones = set(zones)
        self.on_state = on_state
        self.running = False
        self.stopped = False
        self.zone_history: list[set[int]] = [set(self.zones)]
        FakeListener.instances.append(self)

    @property
    def bound_ports(self) -> tuple[int, ...]:
        if not self.running:
            return ()
        return tuple(
            sorted(45000 + z for z in self.zones if 45000 + z not in self.failing_ports)
        )

    def _errors(self) -> dict[int, int | None]:
        return {
            45000 + z: 98 for z in self.zones if 45000 + z in FakeListener.failing_ports
        }

    async def start(self) -> None:
        self.running = True
        if errors := self._errors():
            first = min(errors)
            raise UdpBindError(first, errors[first], errors)

    async def stop(self) -> None:
        self.running = False
        self.stopped = True

    async def set_zones(self, zones: Iterable[int]) -> None:
        self.zones = set(zones)
        self.zone_history.append(set(self.zones))
        if errors := self._errors():
            first = min(errors)
            raise UdpBindError(first, errors[first], errors)

    def diagnostics(self) -> dict[str, Any]:
        return {
            "running": self.running,
            "bound_ports": list(self.bound_ports),
            "bind_errors": {str(p): e for p, e in self._errors().items()},
            "rejects": {},
            "zones": {
                str(z): {"freshness": "fresh", "sender": "192.*.*.*"}
                for z in sorted(self.zones)
            },
        }


@contextmanager
def fake_listeners(probe_result: dict[int, list[Any]] | Exception | None = None):
    """Replace the listener class and ``probe`` of the manager module."""
    FakeListener.instances = []
    FakeListener.failing_ports = set()
    probe = AsyncMock()
    if isinstance(probe_result, Exception):
        probe.side_effect = probe_result
    else:
        probe.return_value = probe_result if probe_result is not None else {}
    with (
        patch(
            "custom_components.ambientika_ventilation.local_manager."
            "AmbientikaUdpListener",
            FakeListener,
        ),
        patch("custom_components.ambientika_ventilation.local_manager.probe", probe),
    ):
        yield probe


def zone_state(
    *,
    n: int = 1,
    house: int = HOUSE_ID,
    zone: int = 1,
    at: float | None = None,
    coupling: Coupling = Coupling.OPPOSITE,
    direction: Direction = Direction.EXHAUST,
    speed: LocalSpeed = LocalSpeed.MEDIUM,
    wall: Any = None,
) -> LocalZoneState:
    """Return a fresh local state stamped with the real monotonic clock."""
    received = time.monotonic() if at is None else at
    frame = UdpFrame(
        zone_index=zone,
        house_id=house,
        coupling=coupling,
        coupling_raw=0,
        direction=direction,
        speed=speed,
        state_raw=0,
        raw=b"",
        source_host="192.0.2.1",
        source_port=46000 + zone,
    )
    return LocalZoneState(
        zone_index=zone,
        house_id=house,
        frame=frame,
        freshness=Freshness.FRESH,
        received_at=received,
        received_wall=wall,
        last_change_at=received,
        frames_total=n,
        heartbeats=0,
        changes=0,
        reversals=0,
        rejected=0,
    )


def mock_api(houses_payload: list[dict[str, Any]], status_payload: dict[str, Any]):
    """Return a cloud API double with one master and one Gemini unit."""
    api = AsyncMock()
    api.metrics = RequestMetrics()
    api.token_expires_at = "2099-01-01T00:00:00+00:00"
    api.async_houses_info.return_value = houses_payload
    api.async_feature_flags.return_value = {}
    api.async_house_devices_status.side_effect = None
    api.async_house_devices_status.return_value = {
        "zoneDevicesInfo": [{"statusPacket": status_payload}],
        "geminiDevicesInfo": [],
    }
    api.async_device_status.side_effect = lambda serial: {
        **status_payload,
        "deviceSerialNumber": serial,
    }
    api.async_renew_token_if_due = AsyncMock(return_value=False)
    return api


def session_double() -> MagicMock:
    session = MagicMock()
    session.close = AsyncMock()
    return session


@contextmanager
def patch_session(api: AsyncMock):
    """Patch the session and the cloud client of the setup with ``api``."""
    with (
        patch(
            "custom_components.ambientika_ventilation.create_session",
            return_value=session_double(),
        ),
        patch(
            "custom_components.ambientika_ventilation.AmbientikaApiClient",
            return_value=api,
        ),
    ):
        yield api


SECOND = "AABBCCDDEE02"


def two_zone_payload(houses_payload: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return one house with two zones, each with one master (no Gemini unit)."""
    import copy

    payload = copy.deepcopy(houses_payload)
    house = payload[0]
    house["geminiDevices"] = []
    house["houseZonesCount"] = 2
    second = copy.deepcopy(house["nonGeminiZones"][0])
    second["id"] = 302
    second["name"] = "Upper floor"
    room = second["rooms"][0]
    room["id"] = 202
    room["name"] = "Bedroom"
    device = room["devices"][0]
    device.update(
        {"id": 103, "serialNumber": SECOND, "name": "Bedroom unit", "zoneIndex": 2}
    )
    device["roomId"] = 202
    house["nonGeminiZones"].append(second)
    return payload
