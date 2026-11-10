"""Tests for token-only storage and DHCP discovery."""

from __future__ import annotations

import logging
from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from custom_components.ambientika_ventilation import async_setup_entry
from custom_components.ambientika_ventilation.api import (
    AmbientikaApiClient,
    AmbientikaAuthError,
    AmbientikaToken,
)
from custom_components.ambientika_ventilation.config_flow import _validate_input
from custom_components.ambientika_ventilation.const import (
    CONF_TOKEN_ONLY,
    CONF_USER_ID,
    DOMAIN,
)
from homeassistant import config_entries
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers.service_info.dhcp import DhcpServiceInfo
from pytest_homeassistant_custom_component.common import MockConfigEntry

from .test_api import FakeResponse, make_client
from .test_api_robustness import expiry_in


def _client() -> AsyncMock:
    client = AsyncMock()
    client.async_authenticate.return_value = AmbientikaToken(
        user_id=42, token="secret", expires_at="2099-01-01T00:00:00+00:00"
    )
    client.async_houses_info.return_value = [
        {"devices": [{"serialNumber": "AABBCCDDEEFF", "name": "Unit"}]}
    ]
    return client


@pytest.mark.parametrize(("token_only", "stored"), [(False, True), (True, False)])
async def test_validated_data_omits_the_password_in_token_only_mode(
    hass, token_only, stored
) -> None:
    """Only the token is kept when the user opts out of storing the password."""
    with patch(
        "custom_components.ambientika_ventilation.config_flow.AmbientikaApiClient",
        return_value=_client(),
    ):
        data = await _validate_input(
            hass,
            {
                CONF_USERNAME: "user@example.com",
                CONF_PASSWORD: "password",
                CONF_TOKEN_ONLY: token_only,
            },
        )

    assert (CONF_PASSWORD in data) is stored
    assert data[CONF_TOKEN_ONLY] is token_only
    assert data["token"] == "secret"


async def test_reconfigure_can_switch_to_token_only(hass) -> None:
    """Reconfiguration replaces the stored data, dropping the password."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="ambientika_ventilation_42",
        data={CONF_USERNAME: "u@example.com", CONF_PASSWORD: "p", CONF_USER_ID: 42},
    )
    entry.add_to_hass(hass)
    with (
        patch(
            "custom_components.ambientika_ventilation.config_flow.AmbientikaApiClient",
            return_value=_client(),
        ),
        patch(
            "custom_components.ambientika_ventilation.async_setup_entry",
            new=AsyncMock(return_value=True),
        ),
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN,
            context={
                "source": config_entries.SOURCE_RECONFIGURE,
                "entry_id": entry.entry_id,
            },
        )
        schema_keys = {str(key): key for key in result["data_schema"].schema}
        assert schema_keys[CONF_TOKEN_ONLY].default() is False
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {
                CONF_USERNAME: "u@example.com",
                CONF_PASSWORD: "p",
                CONF_TOKEN_ONLY: True,
            },
        )
        await hass.async_block_till_done()

    assert result["reason"] == "reconfigure_successful"
    assert CONF_PASSWORD not in entry.data
    assert entry.data[CONF_TOKEN_ONLY] is True


async def test_client_without_password_requires_reauthentication() -> None:
    """A rejected token cannot be replaced without a stored password."""
    client = make_client([FakeResponse(401)])
    client._password = None
    client._expires_at = expiry_in(5)

    with pytest.raises(AmbientikaAuthError):
        await client.async_renew_token_if_due(timedelta(days=30))
    with pytest.raises(AmbientikaAuthError):
        await client.async_authenticate()


async def test_failed_renewal_starts_reauthentication(hass, caplog) -> None:
    """A token that cannot be renewed asks the user to sign in again."""
    entry = MockConfigEntry(domain=DOMAIN, data={CONF_USERNAME: "u@example.com"})
    entry.add_to_hass(hass)
    api = MagicMock(spec=AmbientikaApiClient)
    api.async_renew_token_if_due = AsyncMock(side_effect=AmbientikaAuthError)
    coordinator = MagicMock()
    coordinator.async_config_entry_first_refresh = AsyncMock()
    session = MagicMock()
    session.close = AsyncMock()

    with (
        patch(
            "custom_components.ambientika_ventilation.create_session",
            return_value=session,
        ),
        patch(
            "custom_components.ambientika_ventilation.AmbientikaApiClient",
            return_value=api,
        ) as client_class,
        patch(
            "custom_components.ambientika_ventilation.AmbientikaCoordinator",
            return_value=coordinator,
        ),
        patch.object(hass.config_entries, "async_forward_entry_setups", AsyncMock()),
        patch.object(entry, "async_start_reauth") as start_reauth,
        caplog.at_level(logging.WARNING),
    ):
        assert await async_setup_entry(hass, entry) is True
        await entry._async_process_on_unload(hass)

    assert client_class.call_args.args[3] is None
    start_reauth.assert_called_once_with(hass)
    assert "starting reauthentication" in caplog.text


def _dhcp(hostname: str) -> DhcpServiceInfo:
    return DhcpServiceInfo(
        ip="192.0.2.10", hostname=hostname, macaddress="aabbccddeeff"
    )


@pytest.mark.parametrize("hostname", ["vmc_aabbccddeeff", "gmn_aabbccddeeff"])
async def test_dhcp_discovery_offers_the_credential_form(hass, hostname) -> None:
    """A unit on the network leads to the normal credential step."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": config_entries.SOURCE_DHCP},
        data=_dhcp(hostname),
    )

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"

    again = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": config_entries.SOURCE_DHCP},
        data=_dhcp(hostname),
    )
    assert again["type"] is FlowResultType.ABORT
    assert again["reason"] == "already_in_progress"


async def test_dhcp_discovery_aborts_when_configured(hass) -> None:
    """Nothing is offered once the account exists."""
    MockConfigEntry(domain=DOMAIN, data={}).add_to_hass(hass)

    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": config_entries.SOURCE_DHCP},
        data=_dhcp("vmc_aabbccddeeff"),
    )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


def test_manifest_matches_the_unit_host_names() -> None:
    """The DHCP matchers cover both host name families."""
    import json
    from pathlib import Path

    manifest = json.loads(
        (
            Path(__file__).parents[1]
            / "custom_components/ambientika_ventilation/manifest.json"
        ).read_text("utf-8")
    )
    assert {item["hostname"] for item in manifest["dhcp"]} == {"vmc_*", "gmn_*"}
