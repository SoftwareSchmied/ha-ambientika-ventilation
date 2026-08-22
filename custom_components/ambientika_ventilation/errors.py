"""Translated Home Assistant errors for Ambientika actions."""

from __future__ import annotations

from homeassistant.exceptions import HomeAssistantError, ServiceValidationError

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
