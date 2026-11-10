"""Pure per-zone state tracker for local UDP frames (no Home Assistant imports)."""

from __future__ import annotations

from collections import Counter, deque
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import Any

from .decoder import RejectReason
from .models import (
    LOCAL_LOST_AFTER,
    LOCAL_STALE_AFTER,
    Coupling,
    Direction,
    Freshness,
    LocalZoneState,
    UdpFrame,
)

STALE_AFTER = LOCAL_STALE_AFTER
"""Age in seconds from which a zone is STALE (~81 s)."""
LOST_AFTER = LOCAL_LOST_AFTER
"""Age in seconds from which a zone is LOST."""
RATE_LIMIT_FRAMES = 10
RATE_LIMIT_WINDOW = 10.0


def mask_host(host: str | None) -> str | None:
    """Mask a sender address for diagnostics (keep only the first IPv4 octet)."""
    if host is None:
        return None
    parts = host.split(".")
    if len(parts) == 4:
        return f"{parts[0]}.*.*.*"
    return "masked"


@dataclass(frozen=True, slots=True)
class ProcessResult:
    """Outcome of feeding one decoded frame into the tracker."""

    state: LocalZoneState | None
    """New state, or ``None`` when the frame was rejected."""
    changed: bool = False
    """The frame differs from the previous one (not a plain heartbeat)."""
    freshness_changed: bool = False
    """The zone just became FRESH again."""
    reason: RejectReason | None = None


@dataclass(slots=True)
class _Zone:
    state: LocalZoneState
    pinned_host: str | None = None
    last_received: float = 0.0
    last_running: Direction | None = None
    recent: deque[float] = field(default_factory=deque)
    histogram: Counter[int] = field(default_factory=Counter)


class UdpZoneTracker:
    """Maintain ``LocalZoneState`` per zone and enforce plausibility rules.

    Local data is informative only: rejected frames never change state.
    """

    def __init__(
        self,
        house_id: int,
        zones: Iterable[int],
        *,
        clock: Callable[[], float],
        wall_clock: Callable[[], datetime] | None = None,
        stale_after: float = STALE_AFTER,
        lost_after: float = LOST_AFTER,
        rate_limit: int = RATE_LIMIT_FRAMES,
        rate_window: float = RATE_LIMIT_WINDOW,
    ) -> None:
        """Create a tracker for ``house_id`` and the given zone indexes."""
        self._house_id = house_id
        self._clock = clock
        self._wall_clock = wall_clock or (lambda: datetime.now(UTC))
        self._stale_after = stale_after
        self._lost_after = lost_after
        self._rate_limit = rate_limit
        self._rate_window = rate_window
        self._zones: dict[int, _Zone] = {}
        self.rejects: Counter[RejectReason] = Counter()
        self.set_zones(zones)

    @property
    def zones(self) -> tuple[int, ...]:
        """Return the tracked zone indexes."""
        return tuple(sorted(self._zones))

    def set_zones(self, zones: Iterable[int]) -> None:
        """Track exactly ``zones``; keep state of zones that stay."""
        wanted = set(zones)
        for zone in set(self._zones) - wanted:
            del self._zones[zone]
        for zone in wanted - set(self._zones):
            self._zones[zone] = _Zone(
                LocalZoneState(
                    zone_index=zone,
                    house_id=self._house_id,
                    frame=None,
                    freshness=Freshness.NEVER,
                    received_at=None,
                    received_wall=None,
                    last_change_at=None,
                    frames_total=0,
                    heartbeats=0,
                    changes=0,
                    reversals=0,
                    rejected=0,
                )
            )

    def state(self, zone: int) -> LocalZoneState | None:
        """Return the state of ``zone`` or ``None`` when not tracked."""
        entry = self._zones.get(zone)
        return entry.state if entry else None

    def states(self) -> dict[int, LocalZoneState]:
        """Return all zone states."""
        return {zone: entry.state for zone, entry in sorted(self._zones.items())}

    def histogram(self, zone: int) -> dict[int, int]:
        """Return raw state byte (``MS``) counts seen for ``zone``."""
        entry = self._zones.get(zone)
        return dict(sorted(entry.histogram.items())) if entry else {}

    def count_reject(self, reason: RejectReason, zone: int | None = None) -> None:
        """Count a rejected frame globally and, if tracked, for ``zone``."""
        self.rejects[reason] += 1
        entry = self._zones.get(zone) if zone is not None else None
        if entry is not None:
            entry.state = replace(entry.state, rejected=entry.state.rejected + 1)

    def process(self, frame: UdpFrame) -> ProcessResult:
        """Apply plausibility rules to ``frame`` and update the zone state."""
        entry = self._zones.get(frame.zone_index)
        if entry is None:
            return self._reject(RejectReason.UNKNOWN_ZONE, None)
        zone = frame.zone_index
        if frame.house_id != self._house_id:
            return self._reject(RejectReason.HOUSE_ID, zone)
        now = self._clock()
        previous = entry.state
        if (
            entry.pinned_host is not None
            and frame.source_host != entry.pinned_host
            and now - entry.last_received < self._stale_after
        ):
            return self._reject(RejectReason.SENDER_PINNED, zone)
        recent = entry.recent
        while recent and now - recent[0] >= self._rate_window:
            recent.popleft()
        if len(recent) >= self._rate_limit:
            return self._reject(RejectReason.RATE_LIMIT, zone)
        recent.append(now)

        entry.pinned_host = frame.source_host
        entry.last_received = now
        entry.histogram[frame.state_raw] += 1
        heartbeat = previous.frame is not None and previous.frame.raw == frame.raw
        reversal = False
        running_speed = previous.running_speed
        if frame.running:
            reversal = (
                entry.last_running is not None and entry.last_running != frame.direction
            )
            entry.last_running = frame.direction
            running_speed = frame.speed
        elif frame.coupling is Coupling.IDLE:
            running_speed = None
        entry.state = replace(
            previous,
            frame=frame,
            freshness=Freshness.FRESH,
            received_at=now,
            received_wall=self._wall_clock(),
            last_change_at=previous.last_change_at if heartbeat else now,
            frames_total=previous.frames_total + 1,
            heartbeats=previous.heartbeats + (1 if heartbeat else 0),
            changes=previous.changes + (0 if heartbeat else 1),
            reversals=previous.reversals + (1 if reversal else 0),
            running_speed=running_speed,
        )
        return ProcessResult(
            state=entry.state,
            changed=not heartbeat,
            freshness_changed=previous.freshness is not Freshness.FRESH,
        )

    def evaluate(self, now: float | None = None) -> list[LocalZoneState]:
        """Return states of zones whose freshness changed due to elapsed time."""
        now = self._clock() if now is None else now
        changed: list[LocalZoneState] = []
        for entry in self._zones.values():
            received = entry.state.received_at
            if received is None:
                continue
            age = now - received
            if age >= self._lost_after:
                fresh = Freshness.LOST
            elif age >= self._stale_after:
                fresh = Freshness.STALE
            else:
                fresh = Freshness.FRESH
            if fresh is not entry.state.freshness:
                entry.state = replace(entry.state, freshness=fresh)
                changed.append(entry.state)
        return changed

    def diagnostics(self) -> dict[str, Any]:
        """Return counters without house id; sender hosts are masked."""
        return {
            "rejects": {reason.value: n for reason, n in sorted(self.rejects.items())},
            "zones": {
                str(zone): {
                    "freshness": entry.state.freshness.value,
                    "frames_total": entry.state.frames_total,
                    "heartbeats": entry.state.heartbeats,
                    "changes": entry.state.changes,
                    "reversals": entry.state.reversals,
                    "rejected": entry.state.rejected,
                    "sender": mask_host(entry.pinned_host),
                    "state_histogram": {
                        f"0x{byte:02x}": n
                        for byte, n in sorted(entry.histogram.items())
                    },
                }
                for zone, entry in sorted(self._zones.items())
            },
        }

    def _reject(self, reason: RejectReason, zone: int | None) -> ProcessResult:
        self.count_reject(reason, zone)
        return ProcessResult(state=None, reason=reason)
