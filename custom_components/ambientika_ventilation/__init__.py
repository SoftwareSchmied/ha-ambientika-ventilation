"""Ambientika integration setup."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta

import aiohttp
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME, EVENT_HOMEASSISTANT_CLOSE
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.util.ssl import client_context

from .api import (
    AmbientikaApiClient,
    AmbientikaApiError,
    AmbientikaToken,
    describe_error,
)
from .const import (
    CLOUD_DEVICE_PREFIX,
    CLOUD_TTL_FACTOR,
    CONF_BASE_URL,
    CONF_EXPIRES_AT,
    CONF_RAW_VARIANTS,
    CONF_TOKEN,
    CONF_USER_ID,
    CONNECT_TIMEOUT,
    DEFAULT_BASE_URL,
    DEFAULT_RAW_VARIANTS,
    DOMAIN,
    KEEPALIVE_TIMEOUT,
    MIN_CLOUD_TTL,
    PLATFORMS,
    REQUEST_TIMEOUT,
    TOKEN_CHECK_INTERVAL,
    TOKEN_RENEW_BEFORE,
)
from .coordinator import AmbientikaCoordinator
from .datapoints import variant_keys
from .hub import EVALUATE_INTERVAL_S, AmbientikaHub
from .local_manager import AmbientikaLocal, make_store

LOGGER = logging.getLogger(__name__)


def create_session(hass: HomeAssistant) -> aiohttp.ClientSession:
    """Create the session of one entry with long-lived keep-alive connections.

    The shared Home Assistant session closes idle connections after 15 s while
    the cloud keeps them for about two minutes, so every poll would open a new
    TLS connection. A dedicated session reuses it.
    """
    return aiohttp.ClientSession(
        connector=aiohttp.TCPConnector(
            ssl=client_context(), keepalive_timeout=KEEPALIVE_TIMEOUT
        ),
        timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT, connect=CONNECT_TIMEOUT),
    )


@dataclass(slots=True)
class AmbientikaRuntimeData:
    """Runtime objects owned by one config entry."""

    api: AmbientikaApiClient
    coordinator: AmbientikaCoordinator
    hub: AmbientikaHub
    local: AmbientikaLocal
    raw_variants: bool = False


AmbientikaConfigEntry = ConfigEntry[AmbientikaRuntimeData]


async def async_setup_entry(hass: HomeAssistant, entry: AmbientikaConfigEntry) -> bool:
    """Set up Ambientika from a config entry."""

    def update_token(token: AmbientikaToken) -> None:
        data = {
            **entry.data,
            CONF_USER_ID: token.user_id,
            CONF_TOKEN: token.token,
            CONF_EXPIRES_AT: token.expires_at,
        }
        hass.config_entries.async_update_entry(
            entry,
            data=data,
            unique_id=entry.unique_id or f"{DOMAIN}_{token.user_id}",
        )

    session = create_session(hass)
    close_listener_fired = False

    async def close_session(_event: Event | None = None) -> None:
        nonlocal close_listener_fired
        close_listener_fired = True
        await session.close()

    unsubscribe_close = hass.bus.async_listen_once(
        EVENT_HOMEASSISTANT_CLOSE, close_session
    )

    async def release_session() -> None:
        if not close_listener_fired:
            unsubscribe_close()
        await session.close()

    entry.async_on_unload(release_session)

    api = AmbientikaApiClient(
        session,
        entry.data.get(CONF_BASE_URL, DEFAULT_BASE_URL),
        entry.data[CONF_USERNAME],
        entry.data[CONF_PASSWORD],
        token=entry.data.get(CONF_TOKEN),
        user_id=entry.data.get(CONF_USER_ID),
        expires_at=entry.data.get(CONF_EXPIRES_AT),
        token_callback=update_token,
    )
    coordinator = AmbientikaCoordinator(hass, entry, api)
    hub = AmbientikaHub(cloud_ttl_s=cloud_ttl(coordinator.base_interval))
    coordinator.hub = hub
    local = AmbientikaLocal(hass, entry, coordinator, hub, make_store(hass, entry))
    await local.async_load()
    entry.async_on_unload(local.attach())
    await coordinator.async_config_entry_first_refresh()
    raw_variants = bool(entry.options.get(CONF_RAW_VARIANTS, DEFAULT_RAW_VARIANTS))
    entry.runtime_data = AmbientikaRuntimeData(
        api=api,
        coordinator=coordinator,
        hub=hub,
        local=local,
        raw_variants=raw_variants,
    )

    @callback
    def evaluate_hub(_now: datetime) -> None:
        hub.evaluate()
        local.check()

    entry.async_on_unload(
        async_track_time_interval(
            hass, evaluate_hub, timedelta(seconds=EVALUATE_INTERVAL_S)
        )
    )
    entry.async_on_unload(local.async_stop)

    async def renew_token(_now: datetime | None = None) -> None:
        try:
            if await api.async_renew_token_if_due(TOKEN_RENEW_BEFORE):
                LOGGER.info("Renewed the Ambientika token ahead of its expiry")
        except AmbientikaApiError as err:
            LOGGER.warning(
                "Unable to renew the Ambientika token ahead of its expiry (%s); "
                "it will be retried daily",
                describe_error(err),
            )

    entry.async_on_unload(
        async_track_time_interval(hass, renew_token, TOKEN_CHECK_INTERVAL)
    )
    await renew_token()
    if raw_variants:
        enable_variant_entities(hass, entry)
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    await local.async_start()
    return True


def cloud_ttl(scan_interval: timedelta) -> float:
    """Return how long a cloud poll counts as fresh (2.5 scan intervals, >= 150 s)."""
    return max(MIN_CLOUD_TTL, CLOUD_TTL_FACTOR * scan_interval.total_seconds())


def enable_variant_entities(hass: HomeAssistant, entry: AmbientikaConfigEntry) -> int:
    """Enable the raw `_cloud`/`_local` variant entities; return how many changed.

    Only entities disabled by the integration are touched, never a choice of
    the user. Run before the platforms load so no reload is needed.
    """
    registry = er.async_get(hass)
    suffixes = tuple(
        f"_{key}_{variant}" for key in variant_keys() for variant in ("cloud", "local")
    )
    changed = 0
    for item in er.async_entries_for_config_entry(registry, entry.entry_id):
        if (
            item.disabled_by is er.RegistryEntryDisabler.INTEGRATION
            and item.unique_id.endswith(suffixes)
        ):
            registry.async_update_entity(item.entity_id, disabled_by=None)
            changed += 1
    return changed


async def async_unload_entry(hass: HomeAssistant, entry: AmbientikaConfigEntry) -> bool:
    """Unload an Ambientika config entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def async_remove_entry(hass: HomeAssistant, entry: AmbientikaConfigEntry) -> None:
    """Delete the cached topology and probe result of a removed entry."""
    await make_store(hass, entry).async_remove()


async def async_remove_config_entry_device(
    hass: HomeAssistant,
    entry: AmbientikaConfigEntry,
    device_entry: dr.DeviceEntry,
) -> bool:
    """Allow removal only after a device disappeared from the cloud account."""
    known_devices = entry.runtime_data.coordinator.data.devices
    return not any(
        domain == DOMAIN
        and (identifier in known_devices or identifier.startswith(CLOUD_DEVICE_PREFIX))
        for domain, identifier in device_entry.identifiers
    )
