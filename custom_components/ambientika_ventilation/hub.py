"""Hub that merges cloud, local and pending-command data per device.

The hub is pure Python (no Home Assistant imports). The integration feeds it
with :meth:`AmbientikaHub.update_cloud` (every successful cloud poll),
:meth:`AmbientikaHub.update_local` (every UDP frame or tracker change),
:meth:`AmbientikaHub.set_expected` (pending commands) and one periodic
:meth:`AmbientikaHub.evaluate` (every :data:`EVALUATE_INTERVAL_S`). Entities
read :meth:`AmbientikaHub.resolve` and subscribe to ``(serial, key)`` changes.

All timestamps passed to or created by the hub use one monotonic clock, which
must be the clock of the local tracker (``time.monotonic`` by default).
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Final

from .datapoints import (
    CLOUD_FRESH_S,
    CONFLICT_CYCLES,
    DATAPOINTS,
    DEFAULT_TTL,
    EXPECTED_TTL_S,
    HYSTERESIS_FRAMES,
    LOCAL_FRESH_S,
    LOCAL_LOST_S,
    LOCAL_SOURCES,
    SLAVE_ROLES,
    DatapointDescription,
    DatapointInputs,
    LocalView,
    MergePolicy,
    Source,
    derive_direction,
)
from .local.models import Freshness, LocalZoneState
from .models import AmbientikaData, AmbientikaStatus

_LOGGER = logging.getLogger(__name__)

EVALUATE_INTERVAL_S: Final = 10.0
"""Period of the single freshness timer."""
TOPOLOGY_VERSION: Final = 1
EXPECTABLE_KEYS: Final = (
    "operating_mode",
    "fan_speed",
    "humidity_level",
    "light_sensor_level",
)
"""Datapoints that accept a pending command value."""
CLOUD_FAILURE_LIMIT: Final = 3
"""Consecutive failed cloud polls after which the cloud counts as unreachable."""

DATA_SOURCE_LOCAL: Final = "local_udp"
DATA_SOURCE_CLOUD: Final = "cloud"
DATA_SOURCE_DEGRADED: Final = "degraded"
DATA_SOURCE_OFFLINE: Final = "offline"

ZoneKey = tuple[int, int]
Callback = Callable[["ResolvedValue"], None]


@dataclass(frozen=True, slots=True)
class ResolvedValue:
    """Effective value of one datapoint of one device."""

    key: str
    value: Any
    source: Source | None
    age_s: float | None
    """Age of the selected source; do not record, it changes constantly."""
    variants: Mapping[Source, Any]
    """Raw value per source (None when the source has nothing usable)."""

    @property
    def available(self) -> bool:
        """Return whether a value exists."""
        return self.value is not None


@dataclass(frozen=True, slots=True)
class CloudHealth:
    """Cloud polling health, supplied by the coordinator (F3)."""

    last_success_at: float | None = None
    last_failure_at: float | None = None
    consecutive_failures: int = 0
    last_failure_kind: str | None = None
    failures_by_kind: Mapping[str, int] = field(default_factory=dict)
    requests_total: int = 0
    failures_total: int = 0
    last_latency_s: float | None = None
    last_change_at: float | None = None
    """Monotonic time of the last poll whose data differed from the previous."""

    @property
    def success_rate(self) -> float | None:
        """Return the share of successful requests, or None without requests."""
        if self.requests_total <= 0:
            return None
        ok = max(self.requests_total - self.failures_total, 0)
        return ok / self.requests_total


@dataclass(frozen=True, slots=True)
class DeviceLiveness:
    """Liveness of one device (F2/B4/E5)."""

    data_source: str
    local_freshness: Freshness
    local_age_s: float | None
    local_last_seen: datetime | None
    local_engaged: bool
    """Whether local data has replaced cloud data (hysteresis passed)."""
    cloud_reachable: bool
    cloud_age_s: float | None
    conflicts: int
    in_conflict: bool


@dataclass(frozen=True, slots=True)
class TopologyEntry:
    """Cached placement of one device, enough to map zones to devices."""

    serial: str
    house_id: int | None
    zone_index: int | None
    role: str | None
    device_type: str | None

    @property
    def zone_key(self) -> ZoneKey | None:
        """Return the (house, zone) key or None for devices without a zone."""
        if self.house_id is None or self.zone_index is None:
            return None
        return (self.house_id, self.zone_index)


@dataclass(slots=True)
class _Record:
    status: AmbientikaStatus | None = None
    failed: bool = False
    cloud_at: float | None = None
    cloud_wall: datetime | None = None
    expected: dict[str, tuple[Any, float, float]] = field(default_factory=dict)
    """key -> (value, set_at, expires_at)."""
    cycles: int = 0
    """Cloud cycles since the last local power change."""
    last_power: bool | None = None
    conflicts: int = 0
    in_conflict: bool = False


@dataclass(frozen=True, slots=True)
class _Context:
    inputs: DatapointInputs
    engaged: bool
    expected_age_s: float | None
    liveness: DeviceLiveness


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _int_or_none(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _str_or_none(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


class AmbientikaHub:
    """Merge cloud, local and expected values; notify only on real changes."""

    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], datetime] = _utcnow,
        cloud_ttl_s: float = CLOUD_FRESH_S,
    ) -> None:
        """Create a hub with an injectable monotonic and wall clock."""
        self._clock = clock
        self._wall_clock = wall_clock
        self._ttl: dict[Source, float] = {**DEFAULT_TTL}
        self._ttl[Source.CLOUD] = cloud_ttl_s
        self._ttl[Source.CLOUD_HEURISTIC] = cloud_ttl_s
        self._topology: dict[str, TopologyEntry] = {}
        self._zones: dict[ZoneKey, tuple[str, ...]] = {}
        self._records: dict[str, _Record] = {}
        self._local: dict[ZoneKey, LocalZoneState] = {}
        self._streak: dict[ZoneKey, int] = {}
        self._cloud_received = False
        self._last_cloud_at: float | None = None
        self._health = CloudHealth()
        self._subs: dict[str, dict[str, dict[int, Callback]]] = {}
        self._cache: dict[tuple[str, str], object] = {}
        self._topology_subs: dict[int, Callable[[frozenset[str]], None]] = {}
        self._hub_subs: dict[int, Callable[[bool, CloudHealth], None]] = {}
        self._hub_signature: object = None
        self._token = 0

    # ------------------------------------------------------------------ inputs

    def update_cloud(
        self, data: AmbientikaData | None, at: float | None = None
    ) -> None:
        """Store a successful cloud poll.

        Call on every successful poll, also when nothing changed, because the
        poll time is what keeps cloud values fresh.
        """
        devices = getattr(data, "devices", None)
        if not isinstance(devices, Mapping):
            return
        now = self._now(at)
        wall = self._wall_clock()
        failed: frozenset[str] = getattr(data, "failed_devices", frozenset())
        topology: dict[str, TopologyEntry] = {}
        records: dict[str, _Record] = {}
        for serial, device_data in devices.items():
            device = getattr(device_data, "device", None)
            if not isinstance(serial, str) or device is None:
                continue
            topology[serial] = TopologyEntry(
                serial,
                _int_or_none(device.house_id),
                _int_or_none(device.zone_index),
                _str_or_none(device.role),
                _str_or_none(device.device_type),
            )
            record = self._records.get(serial) or _Record()
            status = getattr(device_data, "status", None)
            record.status = status
            record.failed = serial in failed
            if status is not None and not record.failed:
                record.cloud_at = now
                record.cloud_wall = wall
            record.cycles += 1
            records[serial] = record
        self._cloud_received = True
        self._last_cloud_at = now
        self._records = records
        self._set_topology(topology)
        for serial, record in records.items():
            self._confirm_expected(serial, record, now)
        self._refresh_all(now)
        self._refresh_hub(now)

    def update_cloud_status(
        self, serial: str, status: AmbientikaStatus | None, at: float | None = None
    ) -> None:
        """Store a single device status read from the cloud outside a poll.

        Used for the read-back after a command or filter reset, so the hub sees
        the confirmed state without waiting for the next poll. Unknown devices
        and missing statuses are ignored.
        """
        record = self._records.get(serial)
        if record is None or status is None:
            return
        now = self._now(at)
        record.status = status
        record.failed = False
        record.cloud_at = now
        record.cloud_wall = self._wall_clock()
        record.cycles += 1
        self._confirm_expected(serial, record, now)
        self._refresh_device(serial, now)

    def update_local(
        self, state: LocalZoneState | None, at: float | None = None
    ) -> tuple[str, ...]:
        """Store the local view of one zone; return the devices it applies to.

        Frames for zones without a known device are kept and apply as soon as
        the topology contains the zone.
        """
        if state is None:
            return ()
        now = self._now(at)
        zone = (state.house_id, state.zone_index)
        previous = self._local.get(zone)
        fresh = self._freshness(state, now) is Freshness.FRESH
        is_new = (
            previous is None
            or previous.received_at != state.received_at
            or previous.frames_total != state.frames_total
        )
        streak = self._streak.get(zone, 0)
        if not fresh:
            streak = 0
        elif is_new:
            gap = (
                state.received_at - previous.received_at
                if previous is not None
                and previous.received_at is not None
                and state.received_at is not None
                else None
            )
            streak = 1 if gap is None or gap >= LOCAL_FRESH_S else streak + 1
        self._streak[zone] = streak
        self._local[zone] = state
        serials = self._zones.get(zone, ())
        for serial in serials:
            self._refresh_device(serial, now)
        return serials

    def set_expected(
        self,
        serial: str,
        values: Mapping[str, Any],
        ttl: float = EXPECTED_TTL_S,
        at: float | None = None,
    ) -> tuple[str, ...]:
        """Record pending command values; return the accepted keys.

        Expected values win over cloud until the cloud confirms them or the
        ttl expires. Unknown keys and None values are ignored.
        """
        record = self._records.get(serial)
        if record is None:
            return ()
        now = self._now(at)
        accepted = tuple(key for key in EXPECTABLE_KEYS if values.get(key) is not None)
        for key in accepted:
            record.expected[key] = (values[key], now, now + max(ttl, 0.0))
        if accepted:
            self._refresh_device(serial, now)
        return accepted

    def clear_expected(
        self, serial: str, keys: Iterable[str] | None = None, at: float | None = None
    ) -> None:
        """Drop pending values (all, or only ``keys``), e.g. after a failed command."""
        record = self._records.get(serial)
        if record is None:
            return
        for key in list(record.expected) if keys is None else keys:
            record.expected.pop(key, None)
        self._refresh_device(serial, self._now(at))

    def update_cloud_health(self, health: CloudHealth, at: float | None = None) -> None:
        """Store cloud polling counters from the coordinator."""
        self._health = health
        self._refresh_hub(self._now(at))

    def evaluate(self, now: float | None = None) -> None:
        """Run the freshness timer tick (call every ``EVALUATE_INTERVAL_S``)."""
        current = self._now(now)
        for zone, state in self._local.items():
            if self._freshness(state, current) is not Freshness.FRESH:
                self._streak[zone] = 0
        for record in self._records.values():
            record.expected = {
                key: item for key, item in record.expected.items() if item[2] > current
            }
        self._refresh_all(current)
        self._refresh_hub(current)

    # ----------------------------------------------------------------- outputs

    def resolve(self, serial: str, key: str, now: float | None = None) -> ResolvedValue:
        """Return the effective value of a datapoint.

        Raises ``KeyError`` for a key that is not in the datapoint table. An
        unknown serial yields an unavailable value.
        """
        description = DATAPOINTS[key]
        current = self._now(now)
        context = self._context(serial, current)
        if context is None:
            return ResolvedValue(key, None, None, None, {})
        return self._resolve(description, context, current)

    def liveness(self, serial: str, now: float | None = None) -> DeviceLiveness | None:
        """Return liveness data of a device or None for an unknown serial."""
        context = self._context(serial, self._now(now))
        return None if context is None else context.liveness

    def data_source(self, serial: str, now: float | None = None) -> str:
        """Return ``local_udp``/``cloud``/``degraded``/``offline`` (E5)."""
        liveness = self.liveness(serial, now)
        return DATA_SOURCE_OFFLINE if liveness is None else liveness.data_source

    @property
    def cloud_health(self) -> CloudHealth:
        """Return the last cloud health supplied by the coordinator."""
        return self._health

    def cloud_reachable(self, now: float | None = None) -> bool:
        """Return whether the cloud delivered data recently and did not fail."""
        current = self._now(now)
        return (
            self._last_cloud_at is not None
            and current - self._last_cloud_at < self._ttl[Source.CLOUD]
            and self._health.consecutive_failures < CLOUD_FAILURE_LIMIT
        )

    @property
    def serials(self) -> frozenset[str]:
        """Return all known device serials."""
        return frozenset(self._topology)

    @property
    def cloud_received(self) -> bool:
        """Return whether real cloud data arrived (not only cached topology)."""
        return self._cloud_received

    def unmapped_zones(self) -> frozenset[ZoneKey]:
        """Return zones with local frames but no known device."""
        return frozenset(zone for zone in self._local if zone not in self._zones)

    def local_state(self, house_id: int, zone_index: int) -> LocalZoneState | None:
        """Return the latest local view of a zone (None before the first frame)."""
        return self._local.get((house_id, zone_index))

    def devices_in_zone(self, house_id: int, zone_index: int) -> tuple[str, ...]:
        """Return the serials of a zone."""
        return self._zones.get((house_id, zone_index), ())

    # ----------------------------------------------------------- subscriptions

    def subscribe(
        self, serial: str, key: str, callback: Callback
    ) -> Callable[[], None]:
        """Call ``callback(resolved)`` when the datapoint changes; return unsubscribe.

        Changes of the effective value, the source, availability or any raw
        variant notify. Volatile datapoints (``last_seen``, ``heartbeat_age``)
        only notify when the value or one of its variants becomes (un)available.
        """
        if key not in DATAPOINTS:
            raise KeyError(key)
        token = self._next_token()
        self._subs.setdefault(serial, {}).setdefault(key, {})[token] = callback
        if (serial, key) not in self._cache:
            self._cache[(serial, key)] = self._signature(
                DATAPOINTS[key], self.resolve(serial, key)
            )

        def unsubscribe() -> None:
            callbacks = self._subs.get(serial, {}).get(key)
            if callbacks is None or token not in callbacks:
                return
            del callbacks[token]
            if not callbacks:
                del self._subs[serial][key]
                self._cache.pop((serial, key), None)
                if not self._subs[serial]:
                    del self._subs[serial]

        return unsubscribe

    def subscribe_topology(
        self, callback: Callable[[frozenset[str]], None]
    ) -> Callable[[], None]:
        """Call ``callback(serials)`` when devices or their zones change."""
        token = self._next_token()
        self._topology_subs[token] = callback

        def unsubscribe() -> None:
            self._topology_subs.pop(token, None)

        return unsubscribe

    def subscribe_hub(
        self, callback: Callable[[bool, CloudHealth], None]
    ) -> Callable[[], None]:
        """Call ``callback(cloud_reachable, health)`` when either changes."""
        token = self._next_token()
        self._hub_subs[token] = callback

        def unsubscribe() -> None:
            self._hub_subs.pop(token, None)

        return unsubscribe

    # ---------------------------------------------------------------- topology

    def export_topology(self) -> dict[str, Any]:
        """Return the device placement as plain data for persistent storage."""
        return {
            "version": TOPOLOGY_VERSION,
            "devices": {
                serial: {
                    "house_id": entry.house_id,
                    "zone_index": entry.zone_index,
                    "role": entry.role,
                    "device_type": entry.device_type,
                }
                for serial, entry in self._topology.items()
            },
        }

    def import_topology(self, data: object) -> int:
        """Load a cached topology (local-only startup); return devices loaded.

        Ignored once real cloud data arrived. Malformed data is skipped.
        """
        if self._cloud_received or not isinstance(data, Mapping):
            return 0
        if data.get("version") != TOPOLOGY_VERSION:
            return 0
        devices = data.get("devices")
        if not isinstance(devices, Mapping):
            return 0
        topology: dict[str, TopologyEntry] = {}
        for serial, raw in devices.items():
            if (
                not isinstance(serial, str)
                or not serial
                or not isinstance(raw, Mapping)
            ):
                continue
            topology[serial] = TopologyEntry(
                serial,
                _int_or_none(raw.get("house_id")),
                _int_or_none(raw.get("zone_index")),
                _str_or_none(raw.get("role")),
                _str_or_none(raw.get("device_type")),
            )
        self._records = {
            serial: self._records.get(serial) or _Record() for serial in topology
        }
        self._set_topology(topology)
        self._refresh_all(self._now(None))
        return len(topology)

    # ---------------------------------------------------------------- internal

    def _now(self, at: float | None) -> float:
        return self._clock() if at is None else at

    def _next_token(self) -> int:
        self._token += 1
        return self._token

    def _set_topology(self, topology: dict[str, TopologyEntry]) -> None:
        changed = topology != self._topology
        self._topology = topology
        zones: dict[ZoneKey, list[str]] = {}
        for serial, entry in topology.items():
            if entry.zone_key is not None:
                zones.setdefault(entry.zone_key, []).append(serial)
        self._zones = {zone: tuple(serials) for zone, serials in zones.items()}
        if changed:
            for callback in list(self._topology_subs.values()):
                self._call(callback, frozenset(topology))

    @staticmethod
    def _call(callback: Callable[..., None], *args: object) -> None:
        try:
            callback(*args)
        except Exception:
            _LOGGER.exception("Subscriber callback failed")

    @staticmethod
    def _freshness(state: LocalZoneState, now: float) -> Freshness:
        if state.frame is None or state.received_at is None:
            return Freshness.NEVER
        age = now - state.received_at
        if age >= LOCAL_LOST_S:
            return Freshness.LOST
        if age >= LOCAL_FRESH_S or state.freshness is not Freshness.FRESH:
            return Freshness.STALE
        return Freshness.FRESH

    def _local_view(self, entry: TopologyEntry, now: float) -> LocalView | None:
        zone = entry.zone_key
        state = self._local.get(zone) if zone is not None else None
        if state is None or entry.role == "NotConfigured":
            return None
        freshness = self._freshness(state, now)
        frame = state.frame
        if frame is None or state.received_at is None:
            return None
        return LocalView(
            freshness=freshness,
            age_s=max(now - state.received_at, 0.0),
            received_wall=state.received_wall,
            coupling=frame.coupling,
            direction=derive_direction(entry.role, frame.coupling, frame.direction),
            speed=frame.speed,
            derived=entry.role in SLAVE_ROLES,
        )

    def _context(self, serial: str, now: float) -> _Context | None:
        record = self._records.get(serial)
        entry = self._topology.get(serial)
        if record is None or entry is None:
            return None
        local = self._local_view(entry, now)
        zone = entry.zone_key
        engaged = (
            local is not None
            and local.fresh
            and zone is not None
            and self._streak.get(zone, 0) >= HYSTERESIS_FRAMES
        )
        cloud_age = None if record.cloud_at is None else max(now - record.cloud_at, 0.0)
        status = None if record.failed else record.status
        cloud_fresh = (
            status is not None
            and cloud_age is not None
            and cloud_age < self._ttl[Source.CLOUD]
        )
        local_fresh = local is not None and local.fresh
        if local_fresh and cloud_fresh:
            source = DATA_SOURCE_LOCAL if engaged else DATA_SOURCE_CLOUD
        elif local_fresh or (cloud_fresh and local is not None):
            source = DATA_SOURCE_DEGRADED
        elif cloud_fresh:
            source = DATA_SOURCE_CLOUD
        else:
            source = DATA_SOURCE_OFFLINE
        expected = {key: item for key, item in record.expected.items() if item[2] > now}
        liveness = DeviceLiveness(
            data_source=source,
            local_freshness=(local.freshness if local is not None else Freshness.NEVER),
            local_age_s=None if local is None else local.age_s,
            local_last_seen=None if local is None else local.received_wall,
            local_engaged=engaged,
            cloud_reachable=cloud_fresh,
            cloud_age_s=cloud_age,
            conflicts=record.conflicts,
            in_conflict=record.in_conflict,
        )
        inputs = DatapointInputs(
            serial=serial,
            now=now,
            status=status,
            cloud_age_s=cloud_age,
            cloud_wall=record.cloud_wall,
            expected={key: item[0] for key, item in expected.items()},
            local=local,
            cloud_fresh=cloud_fresh,
            data_source=source,
        )
        expected_age = (
            max(now - max(item[1] for item in expected.values()), 0.0)
            if expected
            else None
        )
        return _Context(inputs, engaged, expected_age, liveness)

    def _usable(
        self, source: Source, description: DatapointDescription, context: _Context
    ) -> bool:
        ttl = description.ttl.get(source, self._ttl[source])
        inputs = context.inputs
        if source in LOCAL_SOURCES:
            local = inputs.local
            return (
                source is Source.LOCAL_UDP
                and local is not None
                and local.fresh
                and local.age_s < ttl
            )
        if source is Source.EXPECTED:
            return True
        return inputs.cloud_age_s is not None and inputs.cloud_age_s < ttl

    def _resolve(
        self, description: DatapointDescription, context: _Context, now: float
    ) -> ResolvedValue:
        inputs = context.inputs
        key = description.key
        if description.policy is MergePolicy.DERIVED:
            assert description.derive is not None
            return ResolvedValue(key, description.derive(inputs), None, None, {})
        variants: dict[Source, Any] = {}
        for source in description.sources:
            if source in description.stale_ok or self._usable(
                source, description, context
            ):
                variants[source] = description.extractors[source](inputs)
            else:
                variants[source] = None
        present = [source for source, value in variants.items() if value is not None]
        chosen: Source | None
        if description.policy is MergePolicy.LATEST:
            chosen = max(present, key=lambda source: variants[source], default=None)
        else:
            local = [source for source in present if source in LOCAL_SOURCES]
            rest = [source for source in present if source not in LOCAL_SOURCES]
            if local and (context.engaged or not rest):
                chosen = local[0]
            else:
                chosen = rest[0] if rest else None
        if chosen is None:
            return ResolvedValue(key, description.stale_value, None, None, variants)
        if chosen in LOCAL_SOURCES:
            age = inputs.local.age_s if inputs.local is not None else None
        elif chosen is Source.EXPECTED:
            age = context.expected_age_s
        else:
            age = inputs.cloud_age_s
        return ResolvedValue(key, variants[chosen], chosen, age, variants)

    @staticmethod
    def _signature(
        description: DatapointDescription, resolved: ResolvedValue
    ) -> object:
        if description.volatile:
            # The source of a "latest" value flips with every cloud poll and
            # heartbeat, so only the availability of the value and of each
            # variant notifies.
            return (
                resolved.value is not None,
                tuple((s, v is not None) for s, v in resolved.variants.items()),
            )
        return (
            resolved.value,
            resolved.source,
            tuple(resolved.variants.items()),
        )

    def _confirm_expected(self, serial: str, record: _Record, now: float) -> None:
        """Drop pending values the fresh cloud status now confirms."""
        context = self._context(serial, now)
        assert context is not None  # records and topology share their serials
        for key, (value, _set_at, _expires) in list(record.expected.items()):
            if DATAPOINTS[key].extractors[Source.CLOUD](context.inputs) == value:
                del record.expected[key]

    def _track_conflict(self, record: _Record, context: _Context, now: float) -> None:
        power = self._resolve(DATAPOINTS["power"], context, now).variants
        local = power.get(Source.LOCAL_UDP)
        cloud = power.get(Source.CLOUD)
        if local is not None and local != record.last_power:
            record.last_power = local
            record.cycles = 0
        disagree = (
            local is not None
            and cloud is not None
            and local != cloud
            and record.cycles > CONFLICT_CYCLES
        )
        if disagree and not record.in_conflict:
            record.conflicts += 1
        record.in_conflict = disagree

    def _refresh_all(self, now: float) -> None:
        for serial in list(self._records):
            self._refresh_device(serial, now)

    def _refresh_device(self, serial: str, now: float) -> None:
        record = self._records[serial]  # records and topology share their serials
        context = self._context(serial, now)
        assert context is not None
        self._track_conflict(record, context, now)
        subscriptions = self._subs.get(serial)
        if not subscriptions:
            return
        context = self._context(serial, now) or context
        for key, callbacks in list(subscriptions.items()):
            description = DATAPOINTS[key]
            resolved = self._resolve(description, context, now)
            signature = self._signature(description, resolved)
            if signature == self._cache.get((serial, key)):
                continue
            self._cache[(serial, key)] = signature
            for callback in list(callbacks.values()):
                self._call(callback, resolved)

    def _refresh_hub(self, now: float) -> None:
        signature = (self.cloud_reachable(now), self._health)
        if signature == self._hub_signature:
            return
        self._hub_signature = signature
        for callback in list(self._hub_subs.values()):
            self._call(callback, *signature)
