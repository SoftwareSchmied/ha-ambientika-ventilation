"""Config flow for Ambientika."""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import (
    AmbientikaApiClient,
    AmbientikaApiError,
    AmbientikaAuthError,
    AmbientikaResponseError,
)
from .const import (
    CONF_BASE_URL,
    CONF_EXPIRES_AT,
    CONF_TOKEN,
    CONF_USER_ID,
    DEFAULT_BASE_URL,
    DOMAIN,
)
from .models import parse_houses

LOGGER = logging.getLogger(__name__)

USER_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_USERNAME): str,
        vol.Required(CONF_PASSWORD): str,
    }
)


async def _validate_input(
    hass: HomeAssistant, user_input: dict[str, Any]
) -> dict[str, Any]:
    """Authenticate and verify that the account contains devices."""
    client = AmbientikaApiClient(
        async_get_clientsession(hass),
        DEFAULT_BASE_URL,
        user_input[CONF_USERNAME],
        user_input[CONF_PASSWORD],
    )
    token = await client.async_authenticate()
    houses = await client.async_houses_info()
    if not parse_houses(houses):
        raise NoDevicesError
    return {
        CONF_USERNAME: user_input[CONF_USERNAME],
        CONF_PASSWORD: user_input[CONF_PASSWORD],
        CONF_BASE_URL: DEFAULT_BASE_URL,
        CONF_USER_ID: token.user_id,
        CONF_TOKEN: token.token,
        CONF_EXPIRES_AT: token.expires_at,
    }


class NoDevicesError(AmbientikaApiError):
    """The Ambientika account contains no usable devices."""


class AmbientikaConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle an Ambientika config flow."""

    VERSION = 1
    MINOR_VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle initial account setup."""
        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                data = await _validate_input(self.hass, user_input)
            except AmbientikaAuthError:
                errors["base"] = "invalid_auth"
            except NoDevicesError:
                errors["base"] = "no_devices"
            except AmbientikaResponseError:
                errors["base"] = "invalid_response"
            except AmbientikaApiError:
                errors["base"] = "cannot_connect"
            except Exception:
                LOGGER.exception("Unexpected error validating Ambientika credentials")
                errors["base"] = "unknown"
            else:
                await self.async_set_unique_id(f"{DOMAIN}_{data[CONF_USER_ID]}")
                self._abort_if_unique_id_configured()
                return self.async_create_entry(
                    title="Ambientika Ventilation", data=data
                )

        return self.async_show_form(
            step_id="user",
            data_schema=USER_SCHEMA,
            errors=errors,
        )

    async def async_step_reauth(self, _entry_data: dict[str, Any]) -> ConfigFlowResult:
        """Start reauthentication for an existing entry."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Validate replacement account credentials."""
        errors: dict[str, str] = {}
        reauth_entry = self._get_reauth_entry()
        if user_input is not None:
            try:
                data = await _validate_input(self.hass, user_input)
            except AmbientikaAuthError:
                errors["base"] = "invalid_auth"
            except NoDevicesError:
                errors["base"] = "no_devices"
            except AmbientikaResponseError:
                errors["base"] = "invalid_response"
            except AmbientikaApiError:
                errors["base"] = "cannot_connect"
            except Exception:
                LOGGER.exception("Unexpected error reauthenticating Ambientika")
                errors["base"] = "unknown"
            else:
                if data[CONF_USER_ID] != reauth_entry.data.get(CONF_USER_ID):
                    errors["base"] = "wrong_account"
                else:
                    return self.async_update_reload_and_abort(
                        reauth_entry,
                        data=data,
                    )

        schema = vol.Schema(
            {
                vol.Required(
                    CONF_USERNAME,
                    default=reauth_entry.data.get(CONF_USERNAME, ""),
                ): str,
                vol.Required(CONF_PASSWORD): str,
            }
        )
        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=schema,
            errors=errors,
        )
