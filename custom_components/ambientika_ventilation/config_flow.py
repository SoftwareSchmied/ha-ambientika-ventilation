"""Config flow for Ambientika."""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlowWithReload,
)
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    BooleanSelector,
    EntitySelector,
    EntitySelectorConfig,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
)
from homeassistant.helpers.service_info.dhcp import DhcpServiceInfo

from .api import (
    AmbientikaApiClient,
    AmbientikaApiError,
    AmbientikaAuthError,
    AmbientikaResponseError,
)
from .const import (
    CONF_BASE_URL,
    CONF_EXPIRES_AT,
    CONF_FILTER_BASIS,
    CONF_LOCAL_UDP,
    CONF_OUTDOOR_HUMIDITY,
    CONF_OUTDOOR_TEMPERATURE,
    CONF_OUTDOOR_WEATHER,
    CONF_RAW_VARIANTS,
    CONF_ROOM_VOLUMES,
    CONF_SCAN_INTERVAL,
    CONF_TOKEN,
    CONF_TOKEN_ONLY,
    CONF_USER_ID,
    DEFAULT_BASE_URL,
    DEFAULT_FILTER_BASIS,
    DEFAULT_LOCAL_UDP,
    DEFAULT_RAW_VARIANTS,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    FILTER_BASES,
    LOCAL_UDP_MODES,
    MAX_ROOM_VOLUME,
    MAX_SCAN_INTERVAL,
    MIN_SCAN_INTERVAL,
)
from .entity import hub_device_serials
from .models import parse_houses

LOGGER = logging.getLogger(__name__)

USER_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_USERNAME): str,
        vol.Required(CONF_PASSWORD): str,
        vol.Optional(CONF_TOKEN_ONLY, default=False): bool,
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
    token_only = bool(user_input.get(CONF_TOKEN_ONLY, False))
    data: dict[str, Any] = {
        CONF_USERNAME: user_input[CONF_USERNAME],
        CONF_BASE_URL: DEFAULT_BASE_URL,
        CONF_USER_ID: token.user_id,
        CONF_TOKEN: token.token,
        CONF_EXPIRES_AT: token.expires_at,
        CONF_TOKEN_ONLY: token_only,
    }
    if not token_only:
        # With the option enabled only the renewable token is stored.
        data[CONF_PASSWORD] = user_input[CONF_PASSWORD]
    return data


class NoDevicesError(AmbientikaApiError):
    """The Ambientika account contains no usable devices."""


async def _async_try_validate(
    hass: HomeAssistant, user_input: dict[str, Any], errors: dict[str, str]
) -> dict[str, Any] | None:
    """Validate credentials and map every failure to a frontend error key."""
    try:
        return await _validate_input(hass, user_input)
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
    return None


def _credentials_schema(username: str, token_only: bool) -> vol.Schema:
    """Return the credential form prefilled with the stored settings."""
    return vol.Schema(
        {
            vol.Required(CONF_USERNAME, default=username): str,
            vol.Required(CONF_PASSWORD): str,
            vol.Optional(CONF_TOKEN_ONLY, default=token_only): bool,
        }
    )


class AmbientikaConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle an Ambientika config flow."""

    VERSION = 1
    MINOR_VERSION = 1

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> AmbientikaOptionsFlow:
        """Return the options flow of an entry."""
        return AmbientikaOptionsFlow()

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle initial account setup."""
        errors: dict[str, str] = {}
        if user_input is not None:
            data = await _async_try_validate(self.hass, user_input, errors)
            if data is not None:
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

    async def async_step_dhcp(
        self, discovery_info: DhcpServiceInfo
    ) -> ConfigFlowResult:
        """Offer setup when an Ambientika unit announces itself on the network.

        The units use the DHCP host names ``VMC_<MAC>`` and ``GMN_<MAC>``. A
        unit says nothing about the cloud account, so the credential form
        follows. Nothing is offered when the account is already configured.
        """
        if self._async_current_entries():
            return self.async_abort(reason="already_configured")
        await self.async_set_unique_id(f"{DOMAIN}_dhcp")
        return await self.async_step_user()

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
            data = await _async_try_validate(self.hass, user_input, errors)
            if data is not None:
                if data[CONF_USER_ID] != reauth_entry.data.get(CONF_USER_ID):
                    errors["base"] = "wrong_account"
                else:
                    return self.async_update_reload_and_abort(
                        reauth_entry,
                        data=data,
                    )

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=_credentials_schema(
                reauth_entry.data.get(CONF_USERNAME, ""),
                bool(reauth_entry.data.get(CONF_TOKEN_ONLY, False)),
            ),
            errors=errors,
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Replace the stored credentials of the same Ambientika account."""
        errors: dict[str, str] = {}
        entry = self._get_reconfigure_entry()
        if user_input is not None:
            data = await _async_try_validate(self.hass, user_input, errors)
            if data is not None:
                if data[CONF_USER_ID] != entry.data.get(CONF_USER_ID):
                    errors["base"] = "wrong_account"
                else:
                    return self.async_update_reload_and_abort(entry, data=data)

        return self.async_show_form(
            step_id="reconfigure",
            data_schema=_credentials_schema(
                entry.data.get(CONF_USERNAME, ""),
                bool(entry.data.get(CONF_TOKEN_ONLY, False)),
            ),
            errors=errors,
        )


class AmbientikaOptionsFlow(OptionsFlowWithReload):
    """Let the user choose polling, local data and the derived-data inputs."""

    def __init__(self) -> None:
        """Initialize the flow."""
        self._options: dict[str, Any] = {}
        self._units: dict[str, str] = {}

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Show and store the options; the entry reloads afterwards."""
        if user_input is not None:
            self._options = {
                CONF_SCAN_INTERVAL: int(user_input[CONF_SCAN_INTERVAL]),
                CONF_LOCAL_UDP: user_input[CONF_LOCAL_UDP],
                CONF_RAW_VARIANTS: bool(user_input[CONF_RAW_VARIANTS]),
                CONF_FILTER_BASIS: user_input[CONF_FILTER_BASIS],
                **{
                    key: user_input[key]
                    for key in (
                        CONF_OUTDOOR_WEATHER,
                        CONF_OUTDOOR_TEMPERATURE,
                        CONF_OUTDOOR_HUMIDITY,
                    )
                    if user_input.get(key)
                },
            }
            self._units = self._unit_labels()
            if self._units:
                return await self.async_step_volumes()
            return self._finish(self.config_entry.options.get(CONF_ROOM_VOLUMES))

        options = self.config_entry.options
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_SCAN_INTERVAL,
                        default=options.get(
                            CONF_SCAN_INTERVAL,
                            int(DEFAULT_SCAN_INTERVAL.total_seconds()),
                        ),
                    ): NumberSelector(
                        NumberSelectorConfig(
                            min=MIN_SCAN_INTERVAL,
                            max=MAX_SCAN_INTERVAL,
                            step=5,
                            unit_of_measurement="s",
                            mode=NumberSelectorMode.BOX,
                        )
                    ),
                    vol.Required(
                        CONF_LOCAL_UDP,
                        default=options.get(CONF_LOCAL_UDP, DEFAULT_LOCAL_UDP),
                    ): SelectSelector(
                        SelectSelectorConfig(
                            options=list(LOCAL_UDP_MODES),
                            mode=SelectSelectorMode.DROPDOWN,
                            translation_key=CONF_LOCAL_UDP,
                        )
                    ),
                    vol.Required(
                        CONF_RAW_VARIANTS,
                        default=options.get(CONF_RAW_VARIANTS, DEFAULT_RAW_VARIANTS),
                    ): BooleanSelector(),
                    vol.Optional(
                        CONF_OUTDOOR_WEATHER,
                        description={
                            "suggested_value": options.get(CONF_OUTDOOR_WEATHER)
                        },
                    ): EntitySelector(EntitySelectorConfig(domain="weather")),
                    vol.Optional(
                        CONF_OUTDOOR_TEMPERATURE,
                        description={
                            "suggested_value": options.get(CONF_OUTDOOR_TEMPERATURE)
                        },
                    ): EntitySelector(
                        EntitySelectorConfig(
                            domain="sensor", device_class="temperature"
                        )
                    ),
                    vol.Optional(
                        CONF_OUTDOOR_HUMIDITY,
                        description={
                            "suggested_value": options.get(CONF_OUTDOOR_HUMIDITY)
                        },
                    ): EntitySelector(
                        EntitySelectorConfig(domain="sensor", device_class="humidity")
                    ),
                    vol.Required(
                        CONF_FILTER_BASIS,
                        default=options.get(CONF_FILTER_BASIS, DEFAULT_FILTER_BASIS),
                    ): SelectSelector(
                        SelectSelectorConfig(
                            options=list(FILTER_BASES),
                            mode=SelectSelectorMode.DROPDOWN,
                            translation_key=CONF_FILTER_BASIS,
                        )
                    ),
                }
            ),
        )

    async def async_step_volumes(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask for the room volume that each unit serves (optional)."""
        if user_input is not None:
            volumes = {
                serial: float(user_input[label])
                for label, serial in self._units.items()
                if user_input.get(label)
            }
            return self._finish(volumes)

        stored = self.config_entry.options.get(CONF_ROOM_VOLUMES) or {}
        return self.async_show_form(
            step_id="volumes",
            data_schema=vol.Schema(
                {
                    vol.Optional(
                        label, description={"suggested_value": stored.get(serial)}
                    ): NumberSelector(
                        NumberSelectorConfig(
                            min=1,
                            max=MAX_ROOM_VOLUME,
                            step=0.5,
                            unit_of_measurement="m\u00b3",
                            mode=NumberSelectorMode.BOX,
                        )
                    )
                    for label, serial in self._units.items()
                }
            ),
        )

    def _unit_labels(self) -> dict[str, str]:
        """Return the unit serials by form label (the unit name)."""
        runtime = getattr(self.config_entry, "runtime_data", None)
        if runtime is None:
            return {}
        labels: dict[str, str] = {}
        for serial in sorted(hub_device_serials(runtime.coordinator)):
            name = runtime.coordinator.data.devices[serial].device.name or "Unit"
            label, number = name, 1
            while label in labels:
                number += 1
                label = f"{name} ({number})"
            labels[label] = serial
        return labels

    def _finish(self, volumes: dict[str, float] | None) -> ConfigFlowResult:
        """Store all options; the entry reloads afterwards."""
        data = dict(self._options)
        if volumes:
            data[CONF_ROOM_VOLUMES] = volumes
        return self.async_create_entry(data=data)
