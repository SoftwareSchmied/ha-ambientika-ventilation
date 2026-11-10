"""Home Assistant side of the derived-data engine.

One :class:`AmbientikaAnalytics` per config entry owns the
:class:`~.analytics.AnalyticsEngine`, loads and saves its counters, feeds it and
publishes the values to the derived entities.

When the engine is updated (see ``docs/DERIVED_DATA.md``):

* after every coordinator update (a listener, no second poll),
* on every local *change* reported by :class:`~.local_manager.AmbientikaLocal`
  (heartbeats do not call back),
* from a 60 s timer while any unit is on or running.

Every pass updates all units of the account, then the zone and house totals,
and notifies the subscribed entities with ``Derived.changed`` only. Command
statistics hook into the coordinator's command path (see
:class:`~.coordinator.CommandObserver`) and into the local frames that confirm a
command.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from itertools import count
from typing import Any, Final

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    ATTR_UNIT_OF_MEASUREMENT,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
)
from homeassistant.core import Event, EventStateChangedData, HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.event import (
    async_track_state_change_event,
    async_track_time_interval,
)
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .analytics import (
    SNAPSHOT_KEYS,
    AnalyticsConfig,
    AnalyticsEngine,
    Snapshot,
    snapshot_from_values,
)
from .analytics_descriptions import (
    DESCRIPTIONS,
    REQ_OUTDOOR,
    REQ_ROOM_VOLUME,
    REQ_UDP,
    DerivedDescription,
    Platform,
)
from .const import (
    ANALYTICS_SAVE_DELAY,
    ANALYTICS_STORE_KEY_PREFIX,
    ANALYTICS_STORE_VERSION,
    ANALYTICS_TICK,
    CONF_FILTER_BASIS,
    CONF_OUTDOOR_HUMIDITY,
    CONF_OUTDOOR_TEMPERATURE,
    CONF_OUTDOOR_WEATHER,
    CONF_ROOM_VOLUMES,
    DEFAULT_FILTER_BASIS,
    FILTER_BASES,
    LOCAL_UDP_OFF,
    MAX_ROOM_VOLUME,
)
from .coordinator import AmbientikaCoordinator
from .datapoints import Source
from .entity import hub_device_serials, house_device_info, unit_device_info
from .hub import AmbientikaHub
from .local.models import Coupling
from .local_manager import AmbientikaLocal
from .models import AmbientikaDevice
from .weather_input import WeatherInput

_LOGGER = logging.getLogger(__name__)

Listener = Callable[[Mapping[str, Any]], None]

_UNAVAILABLE: Final = frozenset({STATE_UNAVAILABLE, STATE_UNKNOWN, "", "none"})

_COUPLING_BY_MODE: Final[Mapping[str, Coupling]] = {
    "ManualHeatRecovery": Coupling.OPPOSITE,
    "Night": Coupling.OPPOSITE,
    "MasterSlaveFlow": Coupling.OPPOSITE,
    "SlaveMasterFlow": Coupling.OPPOSITE,
    "Expulsion": Coupling.SAME,
    "Intake": Coupling.SAME,
    "TimedExpulsion": Coupling.SAME,
}
"""Zone coupling a mode produces. Smart, Auto, Surveillance and Away alternate
or idle depending on the situation, so only the power state confirms them."""


def make_analytics_store(
    hass: HomeAssistant, entry: ConfigEntry[Any]
) -> Store[dict[str, Any]]:
    """Return the persistent store of the engine counters of one entry."""
    return Store(
        hass, ANALYTICS_STORE_VERSION, f"{ANALYTICS_STORE_KEY_PREFIX}.{entry.entry_id}"
    )


def room_volumes(entry: ConfigEntry[Any]) -> dict[str, float]:
    """Return the configured room volumes by serial (valid values only)."""
    stored = entry.options.get(CONF_ROOM_VOLUMES)
    volumes: dict[str, float] = {}
    if isinstance(stored, Mapping):
        for serial, raw in stored.items():
            try:
                volume = float(raw)
            except (TypeError, ValueError):
                continue
            if 0 < volume <= MAX_ROOM_VOLUME:
                volumes[str(serial)] = volume
    return volumes


def analytics_config(entry: ConfigEntry[Any]) -> AnalyticsConfig:
    """Build the engine parameters from the options of an entry."""
    basis = entry.options.get(CONF_FILTER_BASIS, DEFAULT_FILTER_BASIS)
    return AnalyticsConfig(
        room_volumes=room_volumes(entry),
        filter_basis=basis if basis in FILTER_BASES else DEFAULT_FILTER_BASIS,
    )


@dataclass(frozen=True, slots=True)
class DerivedTarget:
    """One entity to create: description, where its values live, ids and device."""

    description: DerivedDescription
    scope_id: str
    unique_id: str
    device_info: DeviceInfo


@dataclass(frozen=True, slots=True)
class _Unit:
    serial: str
    device: AmbientikaDevice
    house: str | None
    zone: str | None


@dataclass(slots=True)
class _Pending:
    expected: Mapping[str, str]
    sent_at: float
    local_done: bool = False


class AmbientikaAnalytics:
    """Feed the derived-data engine and publish its values to entities."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry[Any],
        coordinator: AmbientikaCoordinator,
        hub: AmbientikaHub,
        local: AmbientikaLocal,
        store: Store[dict[str, Any]],
        *,
        clock: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], datetime] = dt_util.now,
    ) -> None:
        """Create the runtime; call :meth:`async_load`, :meth:`attach`, :meth:`refresh`."""
        self._hass = hass
        self._entry = entry
        self._coordinator = coordinator
        self._hub = hub
        self._local = local
        self._store = store
        self._clock = clock
        self._wall_clock = wall_clock
        self.config = analytics_config(entry)
        self.engine = AnalyticsEngine(self.config)
        self.weather = WeatherInput()
        self._values: dict[str, Mapping[str, Any]] = {}
        self._listeners: dict[str, dict[int, Listener]] = {}
        self._tokens = count(1)
        self._pending: dict[str, dict[int, _Pending]] = {}
        self._active = False
        self._save_pending = False
        self.updates = 0

    # ------------------------------------------------------------------ setup

    async def async_load(self) -> None:
        """Restore the counters saved by the previous run."""
        self.engine.import_state(await self._store.async_load())

    def attach(self) -> Callable[[], None]:
        """Follow the coordinator, the local changes, the timer and the outdoor entities."""
        unsubscribers = [
            self._coordinator.async_add_listener(self._on_coordinator),
            self._local.add_change_listener(self._on_local_change),
            async_track_time_interval(self._hass, self._on_tick, ANALYTICS_TICK),
        ]
        outdoor_ids = self.outdoor_entity_ids
        if outdoor_ids:
            unsubscribers.append(
                async_track_state_change_event(
                    self._hass, outdoor_ids, self._on_outdoor_event
                )
            )
        self._read_outdoor()

        @callback
        def detach() -> None:
            for unsubscribe in unsubscribers:
                unsubscribe()

        return detach

    async def async_stop(self) -> None:
        """Write the counters one last time."""
        self._save_pending = False
        await self._store.async_save(self.engine.export_state())

    def _schedule_save(self) -> None:
        """Write the counters in a few minutes; a pending write is kept.

        ``async_delay_save`` restarts its delay on every call, so a pending
        write is not rescheduled or it would never happen.
        """
        if self._save_pending:
            return
        self._save_pending = True
        self._store.async_delay_save(self._export, ANALYTICS_SAVE_DELAY)

    def _export(self) -> dict[str, Any]:
        self._save_pending = False
        return self.engine.export_state()

    # ---------------------------------------------------------------- options

    @property
    def outdoor_entity_ids(self) -> list[str]:
        """Return the configured outdoor source entities."""
        options = self._entry.options
        return [
            entity_id
            for key in (
                CONF_OUTDOOR_WEATHER,
                CONF_OUTDOOR_TEMPERATURE,
                CONF_OUTDOOR_HUMIDITY,
            )
            if (entity_id := options.get(key))
        ]

    @property
    def outdoor_configured(self) -> bool:
        """Return whether an outdoor temperature source is configured."""
        options = self._entry.options
        return bool(
            options.get(CONF_OUTDOOR_WEATHER) or options.get(CONF_OUTDOOR_TEMPERATURE)
        )

    def requirement_met(self, requirement: str, serial: str | None = None) -> bool:
        """Return whether one ``requires`` entry of a description holds."""
        if requirement == REQ_OUTDOOR:
            return self.outdoor_configured
        if requirement == REQ_ROOM_VOLUME:
            return serial in self.config.room_volumes
        if requirement == REQ_UDP:
            return self._local.mode != LOCAL_UDP_OFF
        return False

    # ----------------------------------------------------------------- scopes

    def units(self) -> dict[str, _Unit]:
        """Return the units that take part (zone members), by serial."""
        units: dict[str, _Unit] = {}
        for serial in sorted(hub_device_serials(self._coordinator)):
            device = self._coordinator.data.devices[serial].device
            house = None if device.house_id is None else str(device.house_id)
            zone = (
                None
                if house is None or device.zone_index is None
                else f"{house}_{device.zone_index}"
            )
            units[serial] = _Unit(serial, device, house, zone)
        return units

    def targets(self, platform: Platform) -> list[DerivedTarget]:
        """Return every derived entity of a platform that should exist now."""
        units = self.units()
        zones: dict[str, list[_Unit]] = {}
        houses: dict[str, list[_Unit]] = {}
        for unit in units.values():
            if unit.zone is not None:
                zones.setdefault(unit.zone, []).append(unit)
            if unit.house is not None:
                houses.setdefault(unit.house, []).append(unit)
        targets: list[DerivedTarget] = []
        for description in DESCRIPTIONS:
            if description.platform != platform:
                continue
            if description.scope == "device":
                targets.extend(
                    DerivedTarget(
                        description,
                        unit.serial,
                        f"{unit.serial}_{description.key}",
                        unit_device_info(unit.serial, unit.device),
                    )
                    for unit in units.values()
                    if self._requires(description, unit.serial)
                )
            elif description.scope == "zone" and self._requires(description):
                for zone, members in zones.items():
                    master = next(
                        (m for m in members if m.device.role == "Master"), members[0]
                    )
                    targets.append(
                        DerivedTarget(
                            description,
                            f"zone:{zone}",
                            f"{zone}_{description.key}",
                            unit_device_info(master.serial, master.device),
                        )
                    )
            elif description.scope == "house" and self._requires(description):
                for house, members in houses.items():
                    targets.append(
                        DerivedTarget(
                            description,
                            f"house:{house}",
                            f"{house}_{description.key}",
                            house_device_info(int(house), members[0].device.house_name),
                        )
                    )
        return targets

    def _requires(self, description: DerivedDescription, serial: str | None = None) -> bool:
        return all(self.requirement_met(req, serial) for req in description.requires)

    # ---------------------------------------------------------- entity access

    def values(self, scope_id: str) -> Mapping[str, Any]:
        """Return the latest values of a scope (empty before the first pass)."""
        return self._values.get(scope_id, {})

    def subscribe(self, scope_id: str, listener: Listener) -> Callable[[], None]:
        """Call ``listener(changed)`` with the reportable changes of a scope."""
        token = next(self._tokens)
        self._listeners.setdefault(scope_id, {})[token] = listener

        def unsubscribe() -> None:
            callbacks = self._listeners.get(scope_id)
            if callbacks is not None:
                callbacks.pop(token, None)

        return unsubscribe

    def _publish(self, scope_id: str, values: Mapping[str, Any], changed: Mapping[str, Any]) -> None:
        self._values[scope_id] = values
        if not changed:
            return
        for listener in list(self._listeners.get(scope_id, {}).values()):
            try:
                listener(changed)
            except Exception:
                _LOGGER.exception("Derived value listener failed")

    # ---------------------------------------------------------------- updates

    def _snapshot(self, unit: _Unit) -> Snapshot:
        values = {
            key: self._hub.resolve(unit.serial, key).value for key in SNAPSHOT_KEYS
        }
        device = unit.device
        local = (
            self._hub.local_state(device.house_id, device.zone_index)
            if device.house_id is not None and device.zone_index is not None
            else None
        )
        return snapshot_from_values(
            values,
            zone=unit.zone,
            house=unit.house,
            reversals=None if local is None else local.reversals,
        )

    def refresh(self) -> None:
        """Update every unit, then the zone and house totals; notify changes."""
        now = self._clock()
        wall = self._wall_clock()
        self._read_outdoor()
        outdoor = self.weather.current(now)
        units = self.units()
        for serial in set(self.engine.serials) - units.keys():
            self.engine.forget(serial)
        self._active = False
        for unit in units.values():
            snapshot = self._snapshot(unit)
            self._active = self._active or bool(snapshot.power or snapshot.running)
            derived = self.engine.update(unit.serial, snapshot, now, wall, outdoor)
            self._publish(unit.serial, derived.values, derived.changed)
        self._publish_totals(units, now)
        self.updates += 1
        self._schedule_save()

    def _publish_totals(self, units: Mapping[str, _Unit], now: float) -> None:
        outdoor = self.weather.current(now)
        for zone in sorted({u.zone for u in units.values() if u.zone}):
            derived = self.engine.zone_values(zone, now)
            self._publish(f"zone:{zone}", derived.values, derived.changed)
        for house in sorted({u.house for u in units.values() if u.house}):
            derived = self.engine.house_values(now, outdoor, house)
            self._publish(f"house:{house}", derived.values, derived.changed)

    @callback
    def _on_coordinator(self) -> None:
        self.refresh()

    @callback
    def _on_local_change(self, serials: tuple[str, ...]) -> None:
        """A local frame changed something: confirm commands and update."""
        now = self._clock()
        for serial in serials:
            for token, pending in list(self._pending.get(serial, {}).items()):
                if not pending.local_done and self._local_matches(
                    serial, pending.expected
                ):
                    pending.local_done = True
                    self.engine.command_confirmed(serial, token, now, source="local")
        self.refresh()

    @callback
    def _on_tick(self, _now: datetime) -> None:
        """Expire commands; update while any unit runs."""
        now = self._clock()
        for serial in set(self.engine.serials) | self._pending.keys():
            self.engine.expire_commands(serial, now)
            self._prune(serial, now)
        if self._active:
            self.refresh()

    @callback
    def _on_outdoor_event(self, _event: Event[EventStateChangedData]) -> None:
        self._read_outdoor()
        self._publish_totals(self.units(), self._clock())

    # ---------------------------------------------------------------- outdoor

    def _read(
        self, entity_id: str | None, attribute: str | None
    ) -> tuple[Any, str | None, float] | None:
        """Return (value, unit, age in s) of an entity, None when unusable."""
        if not entity_id:
            return None
        state = self._hass.states.get(entity_id)
        if state is None or state.state.lower() in _UNAVAILABLE:
            return None
        if attribute is None:
            value = state.state
            unit = state.attributes.get(ATTR_UNIT_OF_MEASUREMENT)
        else:
            value = state.attributes.get(attribute)
            unit = state.attributes.get("temperature_unit")
        age = max((dt_util.utcnow() - state.last_reported).total_seconds(), 0.0)
        return value, unit, age

    def _read_outdoor(self) -> None:
        """Feed the weather input from the configured entities.

        A temperature sensor wins over the weather entity; the same holds for
        the humidity. Unavailable entities clear the input.
        """
        options = self._entry.options
        weather_id = options.get(CONF_OUTDOOR_WEATHER)
        temperature_id = options.get(CONF_OUTDOOR_TEMPERATURE)
        humidity_id = options.get(CONF_OUTDOOR_HUMIDITY)
        if temperature_id:
            temperature = self._read(temperature_id, None)
        else:
            temperature = self._read(weather_id, "temperature")
        if temperature is None:
            self.weather.clear()
            return
        humidity = (
            self._read(humidity_id, None)
            if humidity_id
            else self._read(weather_id, "humidity")
        )
        value, unit, age = temperature
        self.weather.set(
            value,
            None if humidity is None else humidity[0],
            self._clock() - age,
            temperature_unit=unit,
        )

    # ------------------------------------------------------ command statistics

    def command_sent(self, serial: str, expected: Mapping[str, str]) -> int:
        """Record a command about to be sent; return its token."""
        now = self._clock()
        token = next(self._tokens)
        self._pending.setdefault(serial, {})[token] = _Pending(expected, now)
        self.engine.command_sent(serial, token, now)
        return token

    def command_confirmed(self, serial: str, token: int) -> None:
        """The cloud read-back shows the command."""
        self.engine.command_confirmed(serial, token, self._clock(), source="cloud")

    def command_rejected(self, serial: str, token: int) -> None:
        """The command failed (cloud error, offline unit, authentication)."""
        self._pending.get(serial, {}).pop(token, None)
        self.engine.command_rejected(serial, token, self._clock())
        self.refresh()

    def filter_reset(self, serial: str) -> None:
        """The cloud accepted a filter reset: restart the filter counters."""
        self.engine.reset_filter(serial, self._wall_clock())
        self.refresh()

    def _prune(self, serial: str, now: float) -> None:
        pending = self._pending.get(serial)
        if pending is None:
            return
        timeout = self.config.command_timeout_s
        for token in [t for t, p in pending.items() if now - p.sent_at >= timeout]:
            del pending[token]
        if not pending:
            del self._pending[serial]

    def _local_matches(self, serial: str, expected: Mapping[str, str]) -> bool:
        """Return whether the fresh local frame shows the expected power/coupling.

        Local ``power`` claims "on" only for moving air or a coupled zone, so an
        idle frame confirms only "Off".
        """
        mode = expected.get("operating_mode")
        if mode is None:
            return False
        coupling = self._hub.resolve(serial, "coupling").value
        if mode == "Off":
            return coupling == Coupling.IDLE
        power = self._hub.resolve(serial, "power").variants.get(Source.LOCAL_UDP)
        if power is not True:
            return False
        wanted = _COUPLING_BY_MODE.get(mode)
        return wanted is None or coupling == wanted

    # ------------------------------------------------------------ diagnostics

    def diagnostics(self) -> dict[str, Any]:
        """Return counts and settings without values, names or identifiers."""
        return {
            "units": len(self.engine.serials),
            "updates": self.updates,
            "outdoor_configured": self.outdoor_configured,
            "outdoor_reading": self.weather.current(self._clock()) is not None,
            "room_volumes_configured": len(self.config.room_volumes),
            "filter_basis": self.config.filter_basis,
            "pending_commands": sum(len(p) for p in self._pending.values()),
        }
