"""Translated Home Assistant errors for Ambientika actions."""

from __future__ import annotations

from homeassistant.exceptions import (
    ConfigEntryAuthFailed,
    HomeAssistantError,
    ServiceValidationError,
)
from homeassistant.helpers.update_coordinator import UpdateFailed

from .const import DOMAIN


def action_error(key: str) -> HomeAssistantError:
    """Return a translated runtime action error."""
    return HomeAssistantError(translation_domain=DOMAIN, translation_key=key)


def validation_error(
    key: str, *, placeholders: dict[str, str] | None = None
) -> ServiceValidationError:
    """Return a translated service validation error."""
    return ServiceValidationError(
        translation_domain=DOMAIN,
        translation_key=key,
        translation_placeholders=placeholders,
    )


def update_failed(details: str) -> UpdateFailed:
    """Return an update failure that keeps its English text and carries a key."""
    return UpdateFailed(
        details,
        translation_domain=DOMAIN,
        translation_key="update_failed",
        translation_placeholders={"details": details},
    )


def auth_failed_error() -> ConfigEntryAuthFailed:
    """Return the translated error that starts reauthentication."""
    return ConfigEntryAuthFailed(
        translation_domain=DOMAIN, translation_key="auth_failed"
    )
