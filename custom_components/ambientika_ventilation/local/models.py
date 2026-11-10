"""Shared types for local (LAN) data sources.

These types are the contract between the local listeners (``local.udp``) and the
hub that merges local and cloud data (``hub``/``datapoints``). They carry no Home
Assistant dependency.

UDP master broadcast (verified on Ghost firmware, see docs/API_REFERENCE.md):
7 bytes ``65 3Z MS <house id, 4 bytes big endian>``; ``Z`` is the zone index, the
frame is sent to port ``45000 + zone`` from source port ``46000 + zone``. ``M``
(high nibble of byte 2) is the zone coupling, ``S`` (low nibble) holds the
direction in bits 3-2 and the fan stage in bits 1-0. A heartbeat is sent every
~27 s and an extra frame immediately on every change.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

UDP_FRAME_LENGTH = 7
UDP_FRAME_HEADER = 0x65
UDP_BASE_PORT = 45000
UDP_SOURCE_BASE_PORT = 46000
UDP_HEARTBEAT_INTERVAL = 26.97
"""Measured master heartbeat period in seconds (±1 s)."""
LOCAL_STALE_AFTER = 3 * UDP_HEARTBEAT_INTERVAL
"""Age in seconds from which a zone is STALE (~81 s); shared by tracker and hub."""
LOCAL_LOST_AFTER = 300.0
"""Age in seconds from which a zone is LOST; shared by tracker and hub."""


class Coupling(StrEnum):
    """How slaves of a zone run relative to the master (high nibble ``M``)."""

    IDLE = "idle"  # M = 0: off or idle
    OPPOSITE = "opposite"  # M = 2: heat recovery, night, smart, both flow modes
    SAME = "same"  # M = 3: exhaust, intake, surveillance, humidity exhaust, timed
    UNKNOWN = "unknown"  # any other value; the raw nibble is kept


class Direction(StrEnum):
    """Master airflow direction (bits 3-2 of ``S``)."""

    STOPPED = "stopped"  # 00: standing still or reversing
    EXHAUST = "exhaust"  # 01
    INTAKE = "intake"  # 10
    UNKNOWN = "unknown"  # 11 or not decodable


class LocalSpeed(StrEnum):
    """Physical fan stage (bits 1-0 of ``S``); also kept while stopped."""

    NIGHT = "night"  # 0
    LOW = "low"  # 1
    MEDIUM = "medium"  # 2
    HIGH = "high"  # 3


class Freshness(StrEnum):
    """Freshness of a local source for one zone.

    FRESH: last frame younger than 3 heartbeats (~81 s).
    STALE: 3 or more heartbeats missed.
    LOST: no frame for 5 minutes or more.
    NEVER: no frame received since start.
    """

    FRESH = "fresh"
    STALE = "stale"
    LOST = "lost"
    NEVER = "never"


@dataclass(frozen=True, slots=True)
class UdpFrame:
    """One decoded master broadcast."""

    zone_index: int
    house_id: int
    coupling: Coupling
    coupling_raw: int
    direction: Direction
    speed: LocalSpeed
    state_raw: int
    """Full byte 2 (``MS``) for research and unknown values."""
    raw: bytes
    source_host: str
    source_port: int

    @property
    def running(self) -> bool:
        """Return whether the master fan is physically moving air right now."""
        return self.direction in (Direction.EXHAUST, Direction.INTAKE)


@dataclass(frozen=True, slots=True)
class LocalZoneState:
    """Current local view of one zone, maintained by the UDP tracker.

    ``received_at``/``last_change_at`` use the monotonic clock; ``received_wall``
    is the wall clock time of the last frame for display.
    """

    zone_index: int
    house_id: int
    frame: UdpFrame | None
    freshness: Freshness
    received_at: float | None
    received_wall: datetime | None
    last_change_at: float | None
    frames_total: int
    heartbeats: int
    changes: int
    reversals: int
    """Direction changes exhaust <-> intake (heat recovery cycles x 2)."""
    rejected: int
    """Frames for this zone rejected by plausibility or spoofing checks."""
    running_speed: LocalSpeed | None = None
    """Stage of the last frame that moved air since the zone left idle.

    A stopped frame (start-up or the ~10 s reversal) still carries a stage
    byte, which during start-up is a leftover; this keeps the real one.
    """
