"""Local UDP reception, topology cache and repair issues of one config entry.

The manager owns one :class:`AmbientikaUdpListener` per house of the account,
feeds the hub with every frame, asks the cloud to confirm physical changes
(rate limited), runs the automatic probe and raises repair issues when local
reception was requested but does not work.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import Any, Final

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.storage import Store

from .const import (
    CONF_LOCAL_UDP,
    DEFAULT_LOCAL_UDP,
    DOMAIN,
    ISSUE_LOCAL_UDP_BIND,
    ISSUE_LOCAL_UDP_NO_FRAMES,
    LOCAL_UDP_MODES,
    LOCAL_UDP_OFF,
    LOCAL_UDP_ON,
    NO_FRAMES_AFTER,
    PROBE_DURATION,
    STORE_KEY_PREFIX,
    STORE_VERSION,
    UDP_REFRESH_MIN_INTERVAL,
)
from .coordinator import AmbientikaCoordinator
from .hub import AmbientikaHub
from .local.listener import AmbientikaUdpListener, UdpBindError, probe
from .local.models import Freshness, LocalZoneState

_LOGGER = logging.getLogger(__name__)

SAVE_DELAY: Final = 10.0
"""Seconds a topology or probe change waits before it is written to disk."""

PROBE_IDLE: Final = "idle"
PROBE_RUNNING: Final = "probing"
PROBE_DETECTED: Final = "detected"
PROBE_NOTHING: Final = "nothing_seen"


def make_store(hass: HomeAssistant, entry: ConfigEntry[Any]) -> Store[dict[str, Any]]:
    """Return the persistent store of one entry (topology and probe result)."""
    return Store(hass, STORE_VERSION, f"{STORE_KEY_PREFIX}.{entry.entry_id}")


def local_mode(entry: ConfigEntry[Any]) -> str:
    """Return the configured reception mode, falling back to the default."""
    mode = entry.options.get(CONF_LOCAL_UDP, DEFAULT_LOCAL_UDP)
    return mode if mode in LOCAL_UDP_MODES else DEFAULT_LOCAL_UDP


class AmbientikaLocal:
    """Manage local UDP reception for one entry."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry[Any],
        coordinator: AmbientikaCoordinator,
        hub: AmbientikaHub,
        store: Store[dict[str, Any]],
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        """Create the manager; call :meth:`async_load` and :meth:`async_start`."""
        self._hass = hass
        self._entry = entry
        self._coordinator = coordinator
        self._hub = hub
        self._store = store
        self._clock = clock
        self.mode = local_mode(entry)
        self.probe_state = PROBE_IDLE
        self._probe_detected = False
        self._enabled = False
        self._dirty = False
        self._listeners: dict[int, AmbientikaUdpListener] = {}
        self._sync_lock = asyncio.Lock()
        self._last_frame_at: float | None = None
        self._last_refresh_at: float | None = None
        self._refreshes_requested = 0
        self._bind_issue = False
        self._no_frames_issue = False
        self._issue_suffix = entry.entry_id
        self._change_listeners: list[Callable[[tuple[str, ...]], None]] = []

    # ------------------------------------------------------------------ setup

    async def async_load(self) -> None:
        """Load the cached topology and the last probe result."""
        stored = await self._store.async_load()
        if not isinstance(stored, Mapping):
            return
        self._hub.import_topology(stored.get("topology"))
        probe_result = stored.get("probe")
        if isinstance(probe_result, Mapping):
            self._probe_detected = probe_result.get("detected") is True

    def attach(self) -> Callable[[], None]:
        """Persist topology changes and follow them with the listeners."""
        return self._hub.subscribe_topology(self._on_topology)

    def add_change_listener(
        self, listener: Callable[[tuple[str, ...]], None]
    ) -> Callable[[], None]:
        """Call ``listener(serials)`` when a local frame changes a zone's state.

        Heartbeats (identical frames) do not call back.
        """
        self._change_listeners.append(listener)

        def remove() -> None:
            if listener in self._change_listeners:
                self._change_listeners.remove(listener)

        return remove

    async def async_start(self) -> None:
        """Start the listeners or schedule the probe, depending on the mode."""
        if self.mode == LOCAL_UDP_OFF:
            return
        if self.mode == LOCAL_UDP_ON or self._probe_detected:
            self._enabled = True
            await self._async_sync()
        elif self._zones_by_house():
            self._entry.async_create_background_task(
                self._hass, self._async_auto_probe(), f"{DOMAIN}_local_probe"
            )

    async def async_stop(self) -> None:
        """Stop all listeners and withdraw the repair issues."""
        self._enabled = False
        async with self._sync_lock:
            listeners = list(self._listeners.values())
            self._listeners.clear()
        for listener in listeners:
            await listener.stop()
        self._clear_issues()
        if self._dirty:
            await self._store.async_save(self._store_data())

    # ------------------------------------------------------------ persistence

    def _store_data(self) -> dict[str, Any]:
        self._dirty = False
        return {
            "topology": self._hub.export_topology(),
            "probe": {
                "detected": self._probe_detected,
                "at": datetime.now(UTC).isoformat(),
            },
        }

    def _schedule_save(self) -> None:
        self._dirty = True
        self._store.async_delay_save(self._store_data, SAVE_DELAY)

    def _on_topology(self, _serials: frozenset[str]) -> None:
        self._schedule_save()
        if self._enabled:
            self._entry.async_create_background_task(
                self._hass, self._async_sync(), f"{DOMAIN}_local_zones"
            )

    # -------------------------------------------------------------- listeners

    def _zones_by_house(self) -> dict[int, set[int]]:
        """Return the zone indexes of every house in the known topology."""
        houses: dict[int, set[int]] = {}
        devices = self._hub.export_topology()["devices"]
        for placement in devices.values():
            house = placement["house_id"]
            zone = placement["zone_index"]
            if house is not None and zone is not None:
                houses.setdefault(house, set()).add(zone)
        return houses

    async def _async_sync(self) -> None:
        """Make the listeners match the houses and zones of the topology."""
        async with self._sync_lock:
            if not self._enabled:
                return
            wanted = self._zones_by_house()
            for house in [h for h in self._listeners if h not in wanted]:
                await self._listeners.pop(house).stop()
            for house, zones in wanted.items():
                listener = self._listeners.get(house)
                try:
                    if listener is None:
                        listener = AmbientikaUdpListener(house, zones, self._on_state)
                        self._listeners[house] = listener
                        if self._last_frame_at is None:
                            self._last_frame_at = self._clock()
                        await listener.start()
                    else:
                        await listener.set_zones(zones)
                except UdpBindError:
                    continue  # reported below from the listeners' bind errors
            self._report_bind_errors()

    def _failed_ports(self) -> list[int]:
        return sorted(
            {
                int(port)
                for listener in self._listeners.values()
                for port in listener.diagnostics()["bind_errors"]
            }
        )

    def _report_bind_errors(self) -> None:
        failed = self._failed_ports()
        if not failed:
            self._delete_issue(ISSUE_LOCAL_UDP_BIND)
            self._bind_issue = False
            return
        _LOGGER.warning(
            "Local UDP reception cannot bind port(s) %s; see the repair issue "
            "for the network requirements",
            ", ".join(map(str, failed)),
        )
        if self.mode == LOCAL_UDP_ON:
            self._create_issue(ISSUE_LOCAL_UDP_BIND, failed)
            self._bind_issue = True

    def _on_state(self, state: LocalZoneState, changed: bool) -> None:
        serials = self._hub.update_local(state)
        if state.freshness is not Freshness.FRESH:
            # Watchdog report (STALE/LOST) from the listener timer, not a frame.
            return
        self._last_frame_at = self._clock()
        self._clear_issues()
        if changed and serials:
            for listener in list(self._change_listeners):
                try:
                    listener(serials)
                except Exception:
                    _LOGGER.exception("Local change listener failed")
            self._request_refresh()

    def _request_refresh(self) -> None:
        """Ask the cloud to confirm a physical change, at most every 30 s."""
        now = self._clock()
        if (
            self._last_refresh_at is not None
            and now - self._last_refresh_at < UDP_REFRESH_MIN_INTERVAL
        ):
            return
        self._last_refresh_at = now
        self._refreshes_requested += 1
        self._entry.async_create_background_task(
            self._hass,
            self._coordinator.async_request_refresh(),
            f"{DOMAIN}_local_confirm",
        )

    # ------------------------------------------------------------------ probe

    async def _async_auto_probe(self) -> None:
        """Listen for the account's own broadcasts and enable the listener."""
        houses = self._zones_by_house()
        self.probe_state = PROBE_RUNNING
        results = await asyncio.gather(
            *(
                self._probe_house(house, zones)
                for house, zones in sorted(houses.items())
            )
        )
        self._probe_detected = any(results)
        self.probe_state = PROBE_DETECTED if self._probe_detected else PROBE_NOTHING
        self._schedule_save()
        if self._probe_detected:
            _LOGGER.info("Local UDP broadcasts detected; local reception enabled")
            self._enabled = True
            await self._async_sync()
        else:
            _LOGGER.debug("No local UDP broadcasts seen; the cloud is used alone")

    @staticmethod
    async def _probe_house(house: int, zones: set[int]) -> bool:
        reports: list[dict[str, Any]] = []
        try:
            frames = await probe(
                house,
                sorted(zones),
                PROBE_DURATION,
                min_frames=1,
                report=reports.append,
            )
        except OSError as err:
            _LOGGER.info("Local UDP probe could not bind: %s", err)
            return False
        heard = any(frames.values())
        if not heard:
            report = reports[0] if reports else {}
            _LOGGER.info(
                "Local UDP probe heard no broadcasts of this account in %.0f s "
                "(zones %s, bound ports %s, bind errors %s, rejected frames %s)",
                PROBE_DURATION,
                sorted(zones),
                report.get("bound_ports"),
                report.get("bind_errors") or {},
                report.get("rejects") or {},
            )
        return heard

    # ----------------------------------------------------------------- issues

    def check(self) -> None:
        """Raise the "no frames" issue when an explicit local mode hears nothing."""
        if (
            self.mode != LOCAL_UDP_ON
            or self._no_frames_issue
            or self._bind_issue
            or self._last_frame_at is None
            or not self._listeners
        ):
            return
        if self._clock() - self._last_frame_at >= NO_FRAMES_AFTER:
            ports = sorted(
                port
                for listener in self._listeners.values()
                for port in listener.bound_ports
            )
            self._create_issue(ISSUE_LOCAL_UDP_NO_FRAMES, ports)
            self._no_frames_issue = True

    def _issue_id(self, issue: str) -> str:
        return f"{issue}_{self._issue_suffix}"

    def _create_issue(self, issue: str, ports: list[int]) -> None:
        ir.async_create_issue(
            self._hass,
            DOMAIN,
            self._issue_id(issue),
            is_fixable=False,
            severity=ir.IssueSeverity.WARNING,
            translation_key=issue,
            translation_placeholders={"ports": ", ".join(map(str, ports)) or "-"},
        )

    def _delete_issue(self, issue: str) -> None:
        ir.async_delete_issue(self._hass, DOMAIN, self._issue_id(issue))

    def _clear_issues(self) -> None:
        """Resolve the no-frames issue; the bind issue only once all ports bind."""
        if self._no_frames_issue:
            self._delete_issue(ISSUE_LOCAL_UDP_NO_FRAMES)
            self._no_frames_issue = False
        if self._bind_issue and not self._failed_ports():
            self._delete_issue(ISSUE_LOCAL_UDP_BIND)
            self._bind_issue = False

    # ------------------------------------------------------------ diagnostics

    def diagnostics(self) -> dict[str, Any]:
        """Return counters without house ids or addresses."""
        listeners = []
        for listener in self._listeners.values():
            info = listener.diagnostics()
            for zone in info["zones"].values():
                zone.pop("sender", None)
            listeners.append(info)
        return {
            "mode": self.mode,
            "enabled": self._enabled,
            "probe_state": self.probe_state,
            "probe_detected_before": self._probe_detected,
            "houses": len(self._zones_by_house()),
            "listeners": listeners,
            "refreshes_requested": self._refreshes_requested,
            "seconds_since_last_frame": (
                None
                if self._last_frame_at is None
                else round(self._clock() - self._last_frame_at, 1)
            ),
            "issues": {
                "bind_failed": self._bind_issue,
                "no_frames": self._no_frames_issue,
            },
        }
