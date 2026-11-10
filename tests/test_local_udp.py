"""Tests for the local UDP decoder and zone tracker."""

from __future__ import annotations

from datetime import datetime

import pytest
from custom_components.ambientika_ventilation.local.decoder import (
    RejectReason,
    decode_frame,
)
from custom_components.ambientika_ventilation.local.models import (
    Coupling,
    Direction,
    Freshness,
    LocalSpeed,
    UdpFrame,
)
from custom_components.ambientika_ventilation.local.tracker import (
    UdpZoneTracker,
    mask_host,
)

HOUSE = 0x01020304


def make(zone: int = 0, state: int = 0x25, house: int = HOUSE) -> bytes:
    return bytes([0x65, 0x30 | zone, state]) + house.to_bytes(4, "big")


def frame(
    zone: int = 0, state: int = 0x25, host: str = "10.1.2.3", house: int = HOUSE
) -> UdpFrame:
    result = decode_frame(make(zone, state, house), (host, 46000 + zone), 45000 + zone)
    assert isinstance(result, UdpFrame)
    return result


@pytest.mark.parametrize(
    ("state", "coupling", "direction", "speed"),
    [
        (0x00, Coupling.IDLE, Direction.STOPPED, LocalSpeed.NIGHT),
        (0x25, Coupling.OPPOSITE, Direction.EXHAUST, LocalSpeed.LOW),
        (0x2A, Coupling.OPPOSITE, Direction.INTAKE, LocalSpeed.MEDIUM),
        (0x3F, Coupling.SAME, Direction.UNKNOWN, LocalSpeed.HIGH),
        (0x33, Coupling.SAME, Direction.STOPPED, LocalSpeed.HIGH),
        (0x51, Coupling.UNKNOWN, Direction.STOPPED, LocalSpeed.LOW),
    ],
)
def test_decode_values(state, coupling, direction, speed) -> None:
    result = frame(zone=2, state=state)
    assert result.zone_index == 2
    assert result.house_id == HOUSE
    assert result.coupling is coupling
    assert result.coupling_raw == state >> 4
    assert result.direction is direction
    assert result.speed is speed
    assert result.state_raw == state
    assert result.raw == make(2, state)
    assert result.source_port == 46002
    assert result.running is (direction in (Direction.EXHAUST, Direction.INTAKE))


@pytest.mark.parametrize(
    ("data", "dest", "src_port", "reason"),
    [
        (b"", 45000, 46000, RejectReason.LENGTH),
        (make() + b"\x00", 45000, 46000, RejectReason.LENGTH),
        (b"\x66" + make()[1:], 45000, 46000, RejectReason.HEADER),
        (b"\x65\x40" + make()[2:], 45000, 46000, RejectReason.ZONE_NIBBLE),
        (make(1), 45000, 46001, RejectReason.ZONE_PORT),
        (make(0), 45000, 46001, RejectReason.SOURCE_PORT),
    ],
)
def test_decode_rejects(data, dest, src_port, reason) -> None:
    assert decode_frame(data, ("10.0.0.1", src_port), dest) is reason


def test_decode_custom_ports_and_skipped_source_check() -> None:
    result = decode_frame(
        make(1), ("h", 5), 1001, base_port=1000, source_base_port=None
    )
    assert isinstance(result, UdpFrame)
    assert result.zone_index == 1


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def tracker(clock: Clock) -> UdpZoneTracker:
    return UdpZoneTracker(
        HOUSE, [0, 1], clock=clock, wall_clock=lambda: datetime(2026, 1, 1)
    )


def test_initial_state_never(tracker: UdpZoneTracker) -> None:
    assert tracker.zones == (0, 1)
    state = tracker.state(0)
    assert state is not None
    assert state.freshness is Freshness.NEVER
    assert state.frame is None
    assert tracker.state(5) is None
    assert tracker.histogram(5) == {}
    assert set(tracker.states()) == {0, 1}
    assert tracker.evaluate() == []


def test_heartbeat_vs_change(tracker: UdpZoneTracker, clock: Clock) -> None:
    first = tracker.process(frame())
    assert first.changed and first.freshness_changed
    assert first.state is not None and first.state.changes == 1
    assert first.state.received_wall == datetime(2026, 1, 1)
    clock.now += 27
    beat = tracker.process(frame())
    assert not beat.changed and not beat.freshness_changed
    assert beat.state is not None
    assert beat.state.heartbeats == 1
    assert beat.state.last_change_at == 1000.0
    assert beat.state.received_at == 1027.0
    clock.now += 27
    change = tracker.process(frame(state=0x29))
    assert change.changed
    assert change.state is not None
    assert change.state.changes == 2
    assert change.state.last_change_at == 1054.0
    assert change.state.frames_total == 3
    assert tracker.histogram(0) == {0x25: 2, 0x29: 1}


def test_reversals(tracker: UdpZoneTracker, clock: Clock) -> None:
    for state in (0x25, 0x20, 0x29, 0x29, 0x20, 0x25, 0x35):
        clock.now += 11
        tracker.process(frame(state=state))
    zone = tracker.state(0)
    assert zone is not None
    # exhaust -> intake -> exhaust; same exhaust stage change is no reversal
    assert zone.reversals == 2


def test_freshness_transitions(tracker: UdpZoneTracker, clock: Clock) -> None:
    tracker.process(frame())
    clock.now += 80
    assert tracker.evaluate() == []
    clock.now += 2
    stale = tracker.evaluate()
    assert [s.freshness for s in stale] == [Freshness.STALE]
    assert tracker.evaluate() == []
    clock.now += 217
    assert tracker.evaluate() == []
    clock.now += 1
    assert [s.freshness for s in tracker.evaluate()] == [Freshness.LOST]
    # a heartbeat recovers and reports a freshness change
    result = tracker.process(frame())
    assert not result.changed and result.freshness_changed
    assert tracker.evaluate(now=clock.now + 1) == []
    # evaluate with explicit now can also move back to fresh from stale
    tracker.evaluate(now=clock.now + 100)
    again = tracker.evaluate(now=clock.now + 1)
    assert [s.freshness for s in again] == [Freshness.FRESH]


def test_rejects_unknown_zone_and_house(tracker: UdpZoneTracker) -> None:
    assert tracker.process(frame(zone=7)).reason is RejectReason.UNKNOWN_ZONE
    assert tracker.process(frame(house=5)).reason is RejectReason.HOUSE_ID
    state = tracker.state(0)
    assert state is not None
    assert state.rejected == 1 and state.frame is None
    assert tracker.rejects[RejectReason.UNKNOWN_ZONE] == 1
    assert tracker.rejects[RejectReason.HOUSE_ID] == 1


def test_sender_pinning(tracker: UdpZoneTracker, clock: Clock) -> None:
    tracker.process(frame(host="10.0.0.1"))
    clock.now += 10
    other = tracker.process(frame(state=0x29, host="10.0.0.2"))
    assert other.reason is RejectReason.SENDER_PINNED
    state = tracker.state(0)
    assert state is not None
    assert state.frame is not None and state.frame.source_host == "10.0.0.1"
    assert state.rejected == 1
    clock.now += 290  # 300 s since the last accepted frame
    accepted = tracker.process(frame(state=0x29, host="10.0.0.2"))
    assert accepted.changed and accepted.state is not None
    # the new host is pinned now
    assert tracker.process(frame(host="10.0.0.1")).reason is RejectReason.SENDER_PINNED


def test_rate_limit(tracker: UdpZoneTracker, clock: Clock) -> None:
    for i in range(10):
        assert tracker.process(frame(state=0x21 + (i % 2) * 4)).state is not None
        clock.now += 0.5
    over = tracker.process(frame())
    assert over.reason is RejectReason.RATE_LIMIT
    state = tracker.state(0)
    assert state is not None and state.frames_total == 10 and state.rejected == 1
    # other zones are independent
    assert tracker.process(frame(zone=1)).state is not None
    clock.now += 6  # the oldest frames left the 10 s window
    assert tracker.process(frame()).state is not None


def test_count_reject_and_set_zones(tracker: UdpZoneTracker) -> None:
    tracker.count_reject(RejectReason.LENGTH)
    tracker.count_reject(RejectReason.HEADER, 1)
    tracker.count_reject(RejectReason.HEADER, 9)
    assert tracker.rejects[RejectReason.HEADER] == 2
    state1 = tracker.state(1)
    assert state1 is not None and state1.rejected == 1
    tracker.process(frame())
    tracker.set_zones([0, 2])
    assert tracker.zones == (0, 2)
    state0 = tracker.state(0)
    assert state0 is not None and state0.frames_total == 1
    assert tracker.state(1) is None


def test_diagnostics_has_no_personal_data(tracker: UdpZoneTracker) -> None:
    tracker.process(frame(host="192.168.7.9"))
    tracker.count_reject(RejectReason.LENGTH)
    diag = tracker.diagnostics()
    text = repr(diag)
    assert "192.168" not in text and str(HOUSE) not in text
    assert diag["zones"]["0"]["sender"] == "192.*.*.*"
    assert diag["zones"]["0"]["state_histogram"] == {"0x25": 1}
    assert diag["zones"]["1"]["sender"] is None
    assert diag["rejects"] == {"length": 1}


def test_mask_host() -> None:
    assert mask_host(None) is None
    assert mask_host("10.1.2.3") == "10.*.*.*"
    assert mask_host("fe80::1") == "masked"


def test_default_wall_clock() -> None:
    tracker = UdpZoneTracker(HOUSE, [0], clock=lambda: 1.0)
    result = tracker.process(frame())
    assert result.state is not None and result.state.received_wall is not None
