"""Tests for the asyncio UDP listener and probe using loopback sockets."""

from __future__ import annotations

import asyncio
import errno
import socket
import sys
from collections.abc import Callable, Iterator
from contextlib import contextmanager

import pytest
from custom_components.ambientika_ventilation.local.decoder import RejectReason
from custom_components.ambientika_ventilation.local.listener import (
    AmbientikaUdpListener,
    UdpBindError,
    _ZoneProtocol,
    probe,
)
from custom_components.ambientika_ventilation.local.models import (
    Freshness,
    LocalZoneState,
    UdpFrame,
)

pytestmark = pytest.mark.usefixtures("socket_enabled")

HOUSE = 0x0A0B0C0D


def make(zone: int = 0, state: int = 0x25, house: int = HOUSE) -> bytes:
    return bytes([0x65, 0x30 | zone, state]) + house.to_bytes(4, "big")


def free_range(count: int = 3) -> int:
    """Return a base port whose next ``count`` UDP ports are free on loopback."""
    for _ in range(100):
        probe_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        probe_sock.bind(("127.0.0.1", 0))
        base = probe_sock.getsockname()[1]
        probe_sock.close()
        socks = []
        try:
            for i in range(count):
                s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                socks.append(s)
                s.bind(("127.0.0.1", base + i))
        except OSError:
            continue
        finally:
            for s in socks:
                s.close()
        return base
    raise RuntimeError("no free port range")  # pragma: no cover


class Sender:
    """Send datagrams from a fixed source port on loopback."""

    def __init__(self) -> None:
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(("127.0.0.1", 0))
        self.port = self.sock.getsockname()[1]

    def send(self, data: bytes, dest_port: int) -> None:
        self.sock.sendto(data, ("127.0.0.1", dest_port))

    def close(self) -> None:
        self.sock.close()


@contextmanager
def sender_for_zone0() -> Iterator[Sender]:
    sender = Sender()
    try:
        yield sender
    finally:
        sender.close()


async def wait_for(predicate: Callable[[], bool], limit: float = 2.0) -> None:
    async with asyncio.timeout(limit):
        while not predicate():  # noqa: ASYNC110
            await asyncio.sleep(0.005)


class Clock:
    now = 1000.0

    def __call__(self) -> float:
        return self.now


async def test_receive_callbacks_and_diagnostics() -> None:
    base = free_range()
    events: list[tuple[LocalZoneState, bool]] = []
    with sender_for_zone0() as sender:
        listener = AmbientikaUdpListener(
            HOUSE,
            [0, 1],
            lambda state, changed: events.append((state, changed)),
            base_port=base,
            source_base_port=sender.port,
            bind_host="127.0.0.1",
        )
        await listener.start()
        await listener.start()  # idempotent
        assert listener.bound_ports == (base, base + 1)
        _ZoneProtocol(listener, base).error_received(OSError("x"))
        sender.send(make(0), base)
        await wait_for(lambda: len(events) == 1)
        sender.send(make(0), base)
        await wait_for(lambda: len(events) == 2)
        sender.send(make(0, 0x29), base)
        await wait_for(lambda: len(events) == 3)
        assert [changed for _, changed in events] == [True, False, True]
        assert events[-1][0].frame is not None
        assert events[-1][0].frame.source_host == "127.0.0.1"

        # rejects: bad length, other house, wrong zone byte for the port,
        # wrong source port for zone 1
        sender.send(b"\x65\x30", base)
        sender.send(make(0, house=1), base)
        sender.send(make(1), base)
        sender.send(make(1), base + 1)
        await wait_for(lambda: sum(listener.tracker.rejects.values()) == 4)
        assert listener.tracker.rejects[RejectReason.LENGTH] == 1
        assert listener.tracker.rejects[RejectReason.HOUSE_ID] == 1
        assert listener.tracker.rejects[RejectReason.ZONE_PORT] == 1
        assert listener.tracker.rejects[RejectReason.SOURCE_PORT] == 1
        assert len(events) == 3

        diag = listener.diagnostics()
        assert diag["running"] is True
        assert diag["bound_ports"] == [base, base + 1]
        assert diag["bind_errors"] == {}
        assert "127.0.0.1" not in repr(diag) and str(HOUSE) not in repr(diag)
        await listener.stop()
        assert listener.bound_ports == ()
        assert listener.diagnostics()["running"] is False
        await listener.stop()


async def test_callback_exception_is_contained() -> None:
    base = free_range(1)
    calls: list[int] = []

    def boom(state: LocalZoneState, changed: bool) -> None:
        calls.append(1)
        raise RuntimeError("callback")

    with sender_for_zone0() as sender:
        listener = AmbientikaUdpListener(
            HOUSE,
            [0],
            boom,
            base_port=base,
            source_base_port=sender.port,
            bind_host="127.0.0.1",
        )
        await listener.start()
        sender.send(make(0), base)
        sender.send(make(0, 0x29), base)
        await wait_for(lambda: len(calls) == 2)
        await listener.stop()


async def test_watchdog_reports_freshness() -> None:
    base = free_range(1)
    clock = Clock()
    events: list[tuple[LocalZoneState, bool]] = []
    with sender_for_zone0() as sender:
        listener = AmbientikaUdpListener(
            HOUSE,
            [0],
            lambda s, c: events.append((s, c)),
            base_port=base,
            source_base_port=sender.port,
            bind_host="127.0.0.1",
            clock=clock,
            evaluate_interval=0.01,
        )
        await listener.start()
        sender.send(make(0), base)
        await wait_for(lambda: len(events) == 1)
        clock.now += 100
        await wait_for(lambda: len(events) == 2)
        assert events[1][0].freshness is Freshness.STALE and events[1][1] is True
        clock.now += 300
        await wait_for(lambda: len(events) == 3)
        assert events[2][0].freshness is Freshness.LOST
        await listener.stop()


@pytest.mark.skipif(sys.platform == "win32", reason="SO_REUSEADDR semantics")
async def test_bind_error_does_not_stop_other_zones() -> None:
    base = free_range()
    blocker = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    blocker.bind(("127.0.0.1", base))  # no reuse flags -> others cannot bind
    events: list[tuple[LocalZoneState, bool]] = []
    with sender_for_zone0() as sender:
        listener = AmbientikaUdpListener(
            HOUSE,
            [0, 1],
            lambda s, c: events.append((s, c)),
            base_port=base,
            source_base_port=sender.port - 1,
            bind_host="127.0.0.1",
        )
        try:
            with pytest.raises(UdpBindError) as info:
                await listener.start()
            assert info.value.port == base
            assert info.value.errno == errno.EADDRINUSE
            assert info.value.errors == {base: errno.EADDRINUSE}
            assert listener.bound_ports == (base + 1,)
            assert listener.diagnostics()["bind_errors"] == {
                str(base): errno.EADDRINUSE
            }
            sender.send(make(1), base + 1)
            await wait_for(lambda: len(events) == 1)

            # removing the failing zone clears the error
            await listener.set_zones([1])
            assert listener.diagnostics()["bind_errors"] == {}
        finally:
            await listener.stop()
            blocker.close()


async def test_set_zones_adds_and_removes() -> None:
    base = free_range()
    with sender_for_zone0() as sender:
        listener = AmbientikaUdpListener(
            HOUSE,
            [0],
            lambda s, c: None,
            base_port=base,
            source_base_port=sender.port,
            bind_host="127.0.0.1",
        )
        # not running yet: only the zone set changes
        await listener.set_zones([0, 1])
        assert listener.bound_ports == ()
        await listener.start()
        assert listener.bound_ports == (base, base + 1)
        await listener.set_zones([1, 2])
        assert listener.bound_ports == (base + 1, base + 2)
        assert listener.tracker.zones == (1, 2)
        await listener.stop()


async def test_probe_collects_frames() -> None:
    base = free_range()
    with sender_for_zone0() as sender:

        async def send_later() -> None:
            await asyncio.sleep(0.1)
            sender.send(make(0), base)
            sender.send(make(0, 0x29), base)
            sender.send(make(0, house=3), base)

        task = asyncio.create_task(send_later())
        reports: list[dict] = []
        seen = await probe(
            HOUSE,
            [0, 1],
            duration=0.5,
            base_port=base,
            source_base_port=sender.port,
            bind_host="127.0.0.1",
            report=reports.append,
        )
        await task
    assert [f.state_raw for f in seen[0]] == [0x25, 0x29]
    (report,) = reports
    assert sorted(report["bound_ports"]) == [base, base + 1]
    assert report["rejects"] == {"house_id": 1}
    assert seen[1] == []
    assert isinstance(seen[0][0], UdpFrame)


async def test_probe_ends_early_with_min_frames() -> None:
    base = free_range()
    with sender_for_zone0() as sender:

        async def send_later() -> None:
            await asyncio.sleep(0.1)
            sender.send(make(0), base)

        task = asyncio.create_task(send_later())
        async with asyncio.timeout(5):
            seen = await probe(
                HOUSE,
                [0],
                duration=30,
                min_frames=1,
                base_port=base,
                source_base_port=sender.port,
                bind_host="127.0.0.1",
            )
        await task
    assert len(seen[0]) == 1


@pytest.mark.skipif(sys.platform == "win32", reason="SO_REUSEADDR semantics")
async def test_probe_bind_errors() -> None:
    base = free_range()
    blockers = []
    for i in range(2):
        b = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        b.bind(("127.0.0.1", base + i))
        blockers.append(b)
    try:
        # all zones fail -> raises
        with pytest.raises(UdpBindError):
            await probe(HOUSE, [0, 1], 0.1, base_port=base, bind_host="127.0.0.1")
        # one zone fails -> others still probed, early end ignores the bad one
        blockers.pop().close()
        with sender_for_zone0() as sender:
            seen = await probe(
                HOUSE,
                [0, 1],
                0.2,
                min_frames=1,
                base_port=base,
                source_base_port=sender.port - 1,
                bind_host="127.0.0.1",
            )
        assert set(seen) == {0, 1}
    finally:
        for b in blockers:
            b.close()
