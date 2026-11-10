"""Entity base for the derived values of the analytics engine.

The entities are generic and description driven: all behaviour comes from the
:class:`~.analytics_descriptions.DerivedDescription` of the value. An entity
writes state only when the engine reports its key in ``Derived.changed`` (the
recorder budget), so heartbeats and unchanged values never write state.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from homeassistant.core import callback
from homeassistant.helpers.entity import Entity

from .analytics_manager import AmbientikaAnalytics, DerivedTarget


class AmbientikaDerivedEntity(Entity):
    """Entity of one derived value in one scope (unit, zone or house)."""

    _attr_has_entity_name = True
    _attr_should_poll = False
    _unrecorded_attributes = frozenset({"measurement_context", "air_quality_raw"})
    """Volatile helper attributes; they must not fill the recorder."""

    def __init__(self, analytics: AmbientikaAnalytics, target: DerivedTarget) -> None:
        """Initialize the entity with the target built by the analytics runtime."""
        description = target.description
        self._analytics = analytics
        self._derived = description
        self._scope_id = target.scope_id
        self._attr_unique_id = target.unique_id
        self._attr_device_info = target.device_info
        self._value: Any = analytics.values(target.scope_id).get(description.key)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Mark estimates and add the helper values of the description."""
        attributes: dict[str, Any] = {}
        if self._derived.estimate:
            attributes["estimate"] = True
        values = self._analytics.values(self._scope_id)
        for key in self._derived.attributes:
            if (value := values.get(key)) is not None:
                attributes[key] = value
        return attributes or None

    async def async_added_to_hass(self) -> None:
        """Follow the engine's reportable changes of this scope."""
        self.async_on_remove(
            self._analytics.subscribe(self._scope_id, self._on_changed)
        )

    @callback
    def _on_changed(self, changed: Mapping[str, Any]) -> None:
        key = self._derived.key
        if key in changed:
            self._value = changed[key]
            self.async_write_ha_state()
