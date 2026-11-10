"""Ambientika integration setup."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime

import aiohttp
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME, EVENT_HOMEASSISTANT_CLOSE
from homeassistant.core import Event, HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.util.ssl import client_context

from .api import (
    AmbientikaApiClient,
    AmbientikaApiError,
    AmbientikaAuthError,
    AmbientikaToken,
    describe_error,
)
from .const import (
    CONF_BASE_URL,
    CONF_EXPIRES_AT,
    CONF_TOKEN,
    CONF_USER_ID,
    CONNECT_TIMEOUT,
    DEFAULT_BASE_URL,
    DOMAIN,
    KEEPALIVE_TIMEOUT,
    PLATFORMS,
    REQUEST_TIMEOUT,
    TOKEN_CHECK_INTERVAL,
    TOKEN_RENEW_BEFORE,
)
from .coordinator import AmbientikaCoordinator
from .stale import is_removable

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
        entry.data.get(CONF_PASSWORD),
        token=entry.data.get(CONF_TOKEN),
        user_id=entry.data.get(CONF_USER_ID),
        expires_at=entry.data.get(CONF_EXPIRES_AT),
        token_callback=update_token,
    )
    coordinator = AmbientikaCoordinator(hass, entry, api)
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = AmbientikaRuntimeData(api=api, coordinator=coordinator)

    async def renew_token(_now: datetime | None = None) -> None:
        try:
            if await api.async_renew_token_if_due(TOKEN_RENEW_BEFORE):
                LOGGER.info("Renewed the Ambientika token ahead of its expiry")
        except AmbientikaAuthError:
            LOGGER.warning(
                "The Ambientika token could not be renewed; starting reauthentication"
            )
            entry.async_start_reauth(hass)
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
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: AmbientikaConfigEntry) -> bool:
    """Unload an Ambientika config entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def async_remove_config_entry_device(
    hass: HomeAssistant,
    entry: AmbientikaConfigEntry,
    device_entry: dr.DeviceEntry,
) -> bool:
    """Allow removal only after a device disappeared from the cloud account."""
    return is_removable(entry.runtime_data.coordinator.data.devices, device_entry)
