"""Passive asyncio UDP listener for Ambientika master broadcasts.

No Home Assistant imports. Local data is informative only: every datagram is
validated by ``decode_frame`` and ``UdpZoneTracker`` before it is exposed.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import socket
import time
from collections.abc import Callable, Iterable
from functools import partial
from typing import Any

from .decoder import RejectReason, decode_frame
from .models import (
    UDP_BASE_PORT,
    UDP_SOURCE_BASE_PORT,
    LocalZoneState,
    UdpFrame,
)
from .tracker import UdpZoneTracker

_LOGGER = logging.getLogger(__name__)

StateCallback = Callable[[LocalZoneState, bool], None]
FrameCallback = Callable[[UdpFrame], None]

EVALUATE_INTERVAL = 10.0


class UdpBindError(OSError):
    """Binding one or more zone ports failed."""

    def __init__(self, port: int, errno_: int | None, errors: dict[int, int | None]):
        """Store the first failing ``port``/``errno_`` and all failures by port."""
        super().__init__(errno_, f"cannot bind UDP port {port}")
        self.port = port
        self.errors = errors


class _ZoneProtocol(asyncio.DatagramProtocol):
    def __init__(self, listener: AmbientikaUdpListener, port: int) -> None:
        self._listener = listener
        self._port = port

    def datagram_received(self, data: bytes, addr: Any) -> None:
        self._listener._on_datagram(self._port, data, addr)

    def error_received(self, exc: Exception) -> None:
        _LOGGER.debug("UDP error on port %s: %s", self._port, exc)


def _bind_socket(host: str, port: int) -> socket.socket:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        if hasattr(socket, "SO_REUSEPORT"):  # pragma: no branch
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sock.setblocking(False)
        sock.bind((host, port))
    except OSError:
        sock.close()
        raise
    return sock


class AmbientikaUdpListener:
    """Receive master broadcasts, one socket per zone port."""

    def __init__(
        self,
        house_id: int,
        zones: Iterable[int],
        on_state: StateCallback,
        *,
        on_frame: FrameCallback | None = None,
        base_port: int = UDP_BASE_PORT,
        source_base_port: int | None = UDP_SOURCE_BASE_PORT,
        bind_host: str = "0.0.0.0",
        clock: Callable[[], float] = time.monotonic,
        evaluate_interval: float = EVALUATE_INTERVAL,
        tracker: UdpZoneTracker | None = None,
    ) -> None:
        """Create a listener; call ``start`` to bind.

        ``on_state(state, changed)`` is called for every accepted frame.
        ``changed`` is False for a plain heartbeat and True for a changed frame,
        a recovery to FRESH, or a freshness drop (STALE/LOST) found by the timer.
        ``on_frame`` receives every accepted frame (used by ``probe``).
        """
        self._on_state = on_state
        self._on_frame = on_frame
        self._base_port = base_port
        self._source_base_port = source_base_port
        self._bind_host = bind_host
        self._evaluate_interval = evaluate_interval
        self._zones = set(zones)
        self.tracker = tracker or UdpZoneTracker(house_id, self._zones, clock=clock)
        self._transports: dict[int, asyncio.DatagramTransport] = {}
        self._bind_errors: dict[int, int | None] = {}
        self._task: asyncio.Task[None] | None = None
        self._running = False

    @property
    def bound_ports(self) -> tuple[int, ...]:
        """Return the ports currently bound."""
        return tuple(sorted(self._transports))

    async def start(self) -> None:
        """Bind all zone ports and start the freshness timer.

        Zones that bind keep running even if others fail; a ``UdpBindError`` for
        the failures is raised afterwards (``errors`` maps port to errno).
        """
        self._running = True
        if self._task is None:
            self._task = asyncio.get_running_loop().create_task(self._watchdog())
        await self._bind_missing()

    async def stop(self) -> None:
        """Close all sockets and stop the timer."""
        self._running = False
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        for transport in self._transports.values():
            transport.close()
        self._transports.clear()

    async def set_zones(self, zones: Iterable[int]) -> None:
        """Listen on exactly ``zones``; bind new ports and close removed ones."""
        self._zones = set(zones)
        self.tracker.set_zones(self._zones)
        wanted_ports = {self._base_port + zone for zone in self._zones}
        for port in [p for p in self._transports if p not in wanted_ports]:
            self._transports.pop(port).close()
        self._bind_errors = {
            p: e for p, e in self._bind_errors.items() if p in wanted_ports
        }
        if self._running:
            await self._bind_missing()

    def diagnostics(self) -> dict[str, Any]:
        """Return counters without house id or full addresses."""
        return {
            "running": self._running,
            "bound_ports": list(self.bound_ports),
            "bind_errors": {
                str(port): err for port, err in sorted(self._bind_errors.items())
            },
            **self.tracker.diagnostics(),
        }

    async def _bind_missing(self) -> None:
        loop = asyncio.get_running_loop()
        failed: dict[int, int | None] = {}
        for zone in sorted(self._zones):
            port = self._base_port + zone
            if port in self._transports:
                continue
            try:
                sock = _bind_socket(self._bind_host, port)
                transport, _ = await loop.create_datagram_endpoint(
                    partial(_ZoneProtocol, self, port), sock=sock
                )
            except OSError as err:
                failed[port] = err.errno
                _LOGGER.warning("Local UDP: cannot bind port %s (%s)", port, err)
                continue
            self._transports[port] = transport
        self._bind_errors = failed
        if failed:
            first = min(failed)
            raise UdpBindError(first, failed[first], failed)

    def _on_datagram(self, port: int, data: bytes, addr: Any) -> None:
        zone_hint = port - self._base_port
        source = (str(addr[0]), int(addr[1]))
        decoded = decode_frame(
            data,
            source,
            port,
            base_port=self._base_port,
            source_base_port=self._source_base_port,
        )
        if isinstance(decoded, RejectReason):
            self.tracker.count_reject(decoded, zone_hint)
            return
        result = self.tracker.process(decoded)
        if result.state is None:
            return
        self._emit(
            result.state,
            result.changed or result.freshness_changed,
            decoded,
        )

    def _emit(
        self, state: LocalZoneState, changed: bool, frame: UdpFrame | None = None
    ) -> None:
        try:
            if frame is not None and self._on_frame is not None:
                self._on_frame(frame)
            self._on_state(state, changed)
        except Exception:
            _LOGGER.exception("Local UDP callback failed")

    async def _watchdog(self) -> None:
        while True:
            await asyncio.sleep(self._evaluate_interval)
            for state in self.tracker.evaluate():
                self._emit(state, True)


async def probe(
    house_id: int,
    zones: Iterable[int],
    duration: float = 60.0,
    *,
    min_frames: int | None = None,
    base_port: int = UDP_BASE_PORT,
    source_base_port: int | None = UDP_SOURCE_BASE_PORT,
    bind_host: str = "0.0.0.0",
    report: Callable[[dict[str, Any]], None] | None = None,
) -> dict[int, list[UdpFrame]]:
    """Listen for ``duration`` seconds and return accepted frames per zone.

    With ``min_frames`` the probe ends early once every bound zone has seen that
    many frames. Zones whose port cannot be bound are skipped; if none can be
    bound the ``UdpBindError`` is raised. ``report`` receives the listener
    diagnostics (bound ports, bind errors, rejected frames per reason) before
    the sockets close.
    """
    wanted = sorted(set(zones))
    seen: dict[int, list[UdpFrame]] = {zone: [] for zone in wanted}
    done = asyncio.Event()

    def on_frame(frame: UdpFrame) -> None:
        seen[frame.zone_index].append(frame)
        if min_frames is not None and all(
            len(seen[zone]) >= min_frames for zone in bound_zones
        ):
            done.set()

    listener = AmbientikaUdpListener(
        house_id,
        wanted,
        lambda _state, _changed: None,
        on_frame=on_frame,
        base_port=base_port,
        source_base_port=source_base_port,
        bind_host=bind_host,
    )
    bound_zones = set(wanted)
    try:
        try:
            await listener.start()
        except UdpBindError as err:
            if len(err.errors) >= len(wanted):
                raise
            bound_zones = {z for z in wanted if base_port + z not in err.errors}
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(done.wait(), duration)
        if report is not None:
            report(listener.diagnostics())
    finally:
        await listener.stop()
    return seen
