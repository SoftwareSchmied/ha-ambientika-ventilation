"""Tests for the Ambientika config flow."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from custom_components.ambientika_ventilation.api import (
    AmbientikaApiError,
    AmbientikaAuthError,
    AmbientikaResponseError,
    AmbientikaToken,
)
from custom_components.ambientika_ventilation.config_flow import (
    NoDevicesError,
    _validate_input,
)
from custom_components.ambientika_ventilation.const import CONF_USER_ID, DOMAIN
from homeassistant import config_entries
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry


async def test_user_flow_success(hass) -> None:
    """Valid credentials create a unique account entry."""
    data = {
        CONF_USERNAME: "user@example.com",
        CONF_PASSWORD: "password",
        CONF_USER_ID: 42,
        "base_url": "https://ambientika.test",
        "token": "secret",
        "expires_at": "2099-01-01T00:00:00+00:00",
    }
    with (
        patch(
            "custom_components.ambientika_ventilation.config_flow._validate_input",
            return_value=data,
        ),
        patch(
            "custom_components.ambientika_ventilation.async_setup_entry",
            new=AsyncMock(return_value=True),
        ),
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN,
            context={"source": config_entries.SOURCE_USER},
            data={CONF_USERNAME: "user@example.com", CONF_PASSWORD: "password"},
        )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Ambientika Ventilation"
    assert result["data"][CONF_USER_ID] == 42
    assert result["result"].unique_id == "ambientika_ventilation_42"


async def test_user_flow_invalid_auth(hass) -> None:
    """Authentication errors are shown without exposing details."""
    with patch(
        "custom_components.ambientika_ventilation.config_flow._validate_input",
        side_effect=AmbientikaAuthError,
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN,
            context={"source": config_entries.SOURCE_USER},
            data={CONF_USERNAME: "user@example.com", CONF_PASSWORD: "bad"},
        )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "invalid_auth"}


async def test_reauthentication_updates_existing_entry(hass) -> None:
    """Reauthentication accepts only credentials for the existing account."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="ambientika_ventilation_42",
        data={
            CONF_USERNAME: "old@example.com",
            CONF_PASSWORD: "old-password",
            CONF_USER_ID: 42,
        },
    )
    entry.add_to_hass(hass)
    new_data = {
        CONF_USERNAME: "user@example.com",
        CONF_PASSWORD: "new-password",
        CONF_USER_ID: 42,
        "base_url": "https://ambientika.test",
        "token": "renewed",
        "expires_at": "2099-01-01T00:00:00+00:00",
    }
    with patch(
        "custom_components.ambientika_ventilation.config_flow._validate_input",
        return_value=new_data,
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN,
            context={
                "source": config_entries.SOURCE_REAUTH,
                "entry_id": entry.entry_id,
            },
            data=entry.data,
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {CONF_USERNAME: "user@example.com", CONF_PASSWORD: "new-password"},
        )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert entry.data["token"] == "renewed"


async def test_validate_input_authenticates_and_requires_devices(hass) -> None:
    """Input validation authenticates and rejects empty accounts."""
    client = AsyncMock()
    client.async_authenticate.return_value = AmbientikaToken(
        user_id=42,
        token="secret",
        expires_at="2099-01-01T00:00:00+00:00",
    )
    client.async_houses_info.return_value = [
        {"devices": [{"serialNumber": "AABBCCDDEEFF", "name": "Unit"}]}
    ]

    with patch(
        "custom_components.ambientika_ventilation.config_flow.AmbientikaApiClient",
        return_value=client,
    ):
        result = await _validate_input(
            hass,
            {CONF_USERNAME: "user@example.com", CONF_PASSWORD: "password"},
        )

    assert result[CONF_USER_ID] == 42
    assert result["token"] == "secret"

    client.async_houses_info.return_value = []
    with (
        patch(
            "custom_components.ambientika_ventilation.config_flow.AmbientikaApiClient",
            return_value=client,
        ),
        pytest.raises(NoDevicesError),
    ):
        await _validate_input(
            hass,
            {CONF_USERNAME: "user@example.com", CONF_PASSWORD: "password"},
        )


@pytest.mark.parametrize(
    ("error", "error_key"),
    [
        (NoDevicesError(), "no_devices"),
        (AmbientikaResponseError(), "invalid_response"),
        (AmbientikaApiError(), "cannot_connect"),
        (RuntimeError(), "unknown"),
    ],
)
async def test_user_flow_errors(hass, error, error_key) -> None:
    """All setup failures are mapped to stable frontend error keys."""
    with patch(
        "custom_components.ambientika_ventilation.config_flow._validate_input",
        side_effect=error,
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN,
            context={"source": config_entries.SOURCE_USER},
            data={CONF_USERNAME: "user@example.com", CONF_PASSWORD: "password"},
        )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": error_key}


async def test_user_flow_shows_form_and_rejects_duplicate(hass) -> None:
    """The initial form is available and duplicate account IDs are rejected."""
    initial = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert initial["type"] is FlowResultType.FORM

    existing = MockConfigEntry(
        domain=DOMAIN,
        unique_id="ambientika_ventilation_42",
        data={CONF_USER_ID: 42},
    )
    existing.add_to_hass(hass)
    data = {
        CONF_USERNAME: "user@example.com",
        CONF_PASSWORD: "password",
        CONF_USER_ID: 42,
    }
    with patch(
        "custom_components.ambientika_ventilation.config_flow._validate_input",
        return_value=data,
    ):
        result = await hass.config_entries.flow.async_configure(
            initial["flow_id"],
            {CONF_USERNAME: "user@example.com", CONF_PASSWORD: "password"},
        )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


@pytest.mark.parametrize(
    ("error", "error_key"),
    [
        (AmbientikaAuthError(), "invalid_auth"),
        (NoDevicesError(), "no_devices"),
        (AmbientikaResponseError(), "invalid_response"),
        (AmbientikaApiError(), "cannot_connect"),
        (RuntimeError(), "unknown"),
    ],
)
async def test_reauthentication_errors(hass, error, error_key) -> None:
    """Reauthentication exposes translated error keys for every failure."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="ambientika_ventilation_42",
        data={
            CONF_USERNAME: "old@example.com",
            CONF_PASSWORD: "old-password",
            CONF_USER_ID: 42,
        },
    )
    entry.add_to_hass(hass)
    with patch(
        "custom_components.ambientika_ventilation.config_flow._validate_input",
        side_effect=error,
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN,
            context={
                "source": config_entries.SOURCE_REAUTH,
                "entry_id": entry.entry_id,
            },
            data=entry.data,
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {CONF_USERNAME: "user@example.com", CONF_PASSWORD: "password"},
        )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": error_key}


async def test_reauthentication_rejects_different_account(hass) -> None:
    """Credentials for another account cannot replace an existing entry."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="ambientika_ventilation_42",
        data={CONF_USERNAME: "old@example.com", CONF_PASSWORD: "old", CONF_USER_ID: 42},
    )
    entry.add_to_hass(hass)
    with patch(
        "custom_components.ambientika_ventilation.config_flow._validate_input",
        return_value={CONF_USER_ID: 7},
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN,
            context={
                "source": config_entries.SOURCE_REAUTH,
                "entry_id": entry.entry_id,
            },
            data=entry.data,
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {CONF_USERNAME: "other@example.com", CONF_PASSWORD: "password"},
        )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "wrong_account"}
