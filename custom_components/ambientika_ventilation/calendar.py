"""Calendar platform: the weekly schedule of a ventilation unit."""

from __future__ import annotations

from datetime import datetime, timedelta, tzinfo
from typing import Any

import voluptuous as vol
from homeassistant.components.calendar import CalendarEntity, CalendarEvent
from homeassistant.components.calendar.const import CalendarEntityFeature
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, SupportsResponse, callback
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import entity_platform
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.util import dt as dt_util

from . import AmbientikaRuntimeData
from .const import (
    API_TO_HA,
    HUMIDITY_LEVELS,
    LIGHT_SENSOR_LEVELS,
    OPERATING_MODES,
    WRITABLE_FAN_SPEEDS,
)
from .coordinator import AmbientikaCoordinator
from .entity import AmbientikaEntity
from .errors import validation_error
from .models import AmbientikaTimeSlot, is_controllable_device
from .schedule import (
    HA_DAYS,
    HA_TO_API,
    ScheduleValidationError,
    event_description,
    event_summary,
    expand_slots,
    parse_event_settings,
    parse_weekly_rrule,
    schedule_timezone,
    slot_interval,
    slot_options,
    weekly_rrule,
)

PARALLEL_UPDATES = 1

SERVICE_SET_TIME_SLOT = "set_time_slot"
SERVICE_DELETE_TIME_SLOT = "delete_time_slot"
SERVICE_CLEAR_SCHEDULE = "clear_schedule"
SERVICE_GET_SCHEDULE = "get_schedule"

# How far ahead the "next event" of the entity looks (a slot recurs weekly).
LOOKAHEAD = timedelta(days=8)

FIELD_OPTIONS: dict[str, tuple[str, ...]] = {
    "operating_mode": OPERATING_MODES,
    "fan_speed": WRITABLE_FAN_SPEEDS,
    "humidity_level": HUMIDITY_LEVELS,
    "light_sensor_level": LIGHT_SENSOR_LEVELS[1:],
}
SLOT_ID = vol.All(vol.Coerce(int), vol.Range(min=1))
SET_TIME_SLOT_SCHEMA = vol.Schema(
    {
        vol.Optional("slot_id"): SLOT_ID,
        vol.Optional("day"): vol.In(HA_DAYS),
        vol.Optional("start"): cv.time,
        vol.Optional("end"): cv.time,
        **{
            vol.Optional(field): vol.In([API_TO_HA[value] for value in values])
            for field, values in FIELD_OPTIONS.items()
        },
    }
)
DELETE_TIME_SLOT_SCHEMA = vol.Schema({vol.Required("slot_id"): SLOT_ID})
CLEAR_SCHEDULE_SCHEMA = vol.Schema({vol.Required("confirm"): cv.boolean})


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry[AmbientikaRuntimeData],
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up one schedule calendar per unit that has a schedule."""
    coordinator = entry.runtime_data.coordinator
    known: set[str] = set()

    @callback
    def add_new_entities() -> None:
        serials = {
            serial
            for serial, device_data in coordinator.data.devices.items()
            if device_data.schedule is not None
            and is_controllable_device(device_data.device, device_data.status)
        } - known
        if serials:
            async_add_entities(
                AmbientikaScheduleCalendar(coordinator, serial) for serial in serials
            )
            known.update(serials)

    platform = entity_platform.async_get_current_platform()
    platform.async_register_entity_service(
        SERVICE_SET_TIME_SLOT, SET_TIME_SLOT_SCHEMA.schema, "async_set_time_slot"
    )
    platform.async_register_entity_service(
        SERVICE_DELETE_TIME_SLOT,
        DELETE_TIME_SLOT_SCHEMA.schema,
        "async_delete_time_slot",
    )
    platform.async_register_entity_service(
        SERVICE_CLEAR_SCHEDULE, CLEAR_SCHEDULE_SCHEMA.schema, "async_clear_schedule"
    )
    platform.async_register_entity_service(
        SERVICE_GET_SCHEDULE,
        None,
        "async_get_schedule",
        supports_response=SupportsResponse.ONLY,
    )

    add_new_entities()
    entry.async_on_unload(coordinator.async_add_topology_listener(add_new_entities))


class AmbientikaScheduleCalendar(AmbientikaEntity, CalendarEntity):
    """The weekly time slots of one unit as recurring calendar events."""

    _attr_translation_key = "schedule"

    def __init__(self, coordinator: AmbientikaCoordinator, serial: str) -> None:
        """Initialize the calendar of one unit."""
        super().__init__(coordinator, serial, "schedule_calendar")

    @property
    def _slots(self) -> tuple[AmbientikaTimeSlot, ...]:
        """Return the known time slots (none when no schedule exists)."""
        schedule = self.device_data.schedule
        return schedule.time_slots if schedule is not None else ()

    @property
    def _zone(self) -> tzinfo:
        """Return the zone the slot times are meant in."""
        return schedule_timezone(self.device_data.device, dt_util.DEFAULT_TIME_ZONE)

    @property
    def writable(self) -> bool:
        """Return whether events may be created, changed and deleted."""
        return self.coordinator.schedule_writable(self._serial)

    @property
    def supported_features(self) -> CalendarEntityFeature:
        """Offer editing only when the account and the unit allow it."""
        if not self.writable:
            return CalendarEntityFeature(0)
        return (
            CalendarEntityFeature.CREATE_EVENT
            | CalendarEntityFeature.UPDATE_EVENT
            | CalendarEntityFeature.DELETE_EVENT
        )

    def _events(self, start: datetime, end: datetime) -> list[CalendarEvent]:
        """Return the occurrences of all slots in a range."""
        language = self.hass.config.language if self.hass else "en"
        events: list[CalendarEvent] = []
        for slot, begin, finish in expand_slots(self._slots, self._zone, start, end):
            events.append(
                CalendarEvent(
                    start=begin,
                    end=finish,
                    summary=event_summary(slot, language),
                    description=event_description(slot),
                    uid=str(slot.slot_id) if slot.slot_id is not None else None,
                    recurrence_id=begin.isoformat(),
                    rrule=weekly_rrule((begin.weekday() + 1) % 7),
                )
            )
        return events

    @property
    def event(self) -> CalendarEvent | None:
        """Return the current or next time slot."""
        now = dt_util.now()
        return next(iter(self._events(now, now + LOOKAHEAD)), None)

    async def async_get_events(
        self, hass: HomeAssistant, start_date: datetime, end_date: datetime
    ) -> list[CalendarEvent]:
        """Return the occurrences within a range."""
        return self._events(start_date, end_date)

    # ------------------------------------------------------------------
    # Calendar editing
    # ------------------------------------------------------------------

    def _event_times(
        self, dtstart: object, dtend: object, rrule: object
    ) -> dict[str, Any]:
        """Map event start, end and recurrence to a day and two clock times."""
        if not isinstance(dtstart, datetime) or not isinstance(dtend, datetime):
            raise ScheduleValidationError("calendar_event_not_representable")
        zone = self._zone
        start = _in_zone(dtstart, zone)
        end = _in_zone(dtend, zone)
        if start.date() != end.date():
            raise ScheduleValidationError("calendar_event_not_representable")
        day = (start.weekday() + 1) % 7
        parse_weekly_rrule(rrule, day)
        return {"day_of_week": day, "start_time": start.time(), "end_time": end.time()}

    @staticmethod
    def _reject_instance(
        recurrence_id: str | None, recurrence_range: str | None
    ) -> None:
        """Only a whole weekly series maps to a time slot, never one occurrence."""
        if recurrence_id is not None or recurrence_range is not None:
            raise validation_error("calendar_recurrence_unsupported")

    async def async_create_event(self, **kwargs: Any) -> None:
        """Create a time slot from a weekly event."""
        try:
            fields = self._event_times(
                kwargs.get("dtstart"), kwargs.get("dtend"), kwargs.get("rrule")
            )
            settings = parse_event_settings(
                kwargs.get("summary"), kwargs.get("description")
            )
            if "operating_mode" not in settings:
                raise ScheduleValidationError("calendar_event_mode_missing")
        except ScheduleValidationError as err:
            raise validation_error(err.code, placeholders=err.placeholders) from err
        await self.coordinator.async_create_time_slot(
            self._serial, **fields, **settings
        )

    async def async_update_event(
        self,
        uid: str,
        event: dict[str, Any],
        recurrence_id: str | None = None,
        recurrence_range: str | None = None,
    ) -> None:
        """Change a time slot from an edited weekly event."""
        self._reject_instance(recurrence_id, recurrence_range)
        slot_id = _slot_id(uid)
        try:
            fields: dict[str, Any] = {}
            if "dtstart" in event or "dtend" in event:
                fields.update(
                    self._event_times(
                        event.get("dtstart"), event.get("dtend"), event.get("rrule")
                    )
                )
            fields.update(
                parse_event_settings(event.get("summary"), event.get("description"))
            )
        except ScheduleValidationError as err:
            raise validation_error(err.code, placeholders=err.placeholders) from err
        await self.coordinator.async_update_time_slot(self._serial, slot_id, **fields)

    async def async_delete_event(
        self,
        uid: str,
        recurrence_id: str | None = None,
        recurrence_range: str | None = None,
    ) -> None:
        """Delete a time slot (the whole weekly series)."""
        self._reject_instance(recurrence_id, recurrence_range)
        await self.coordinator.async_delete_time_slot(self._serial, _slot_id(uid))

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------

    async def async_set_time_slot(self, **fields: Any) -> None:
        """Create a time slot, or change the one named by ``slot_id``."""
        slot_id = fields.pop("slot_id", None)
        values: dict[str, Any] = {
            "day_of_week": fields.get("day"),
            "start_time": fields.get("start"),
            "end_time": fields.get("end"),
            **{
                field: HA_TO_API[fields[field]]
                for field in FIELD_OPTIONS
                if field in fields
            },
        }
        if slot_id is not None:
            await self.coordinator.async_update_time_slot(
                self._serial, slot_id, **values
            )
            return
        if any(
            values.get(key) is None
            for key in ("day_of_week", "start_time", "end_time", "operating_mode")
        ):
            raise validation_error("time_slot_incomplete")
        await self.coordinator.async_create_time_slot(self._serial, **values)

    async def async_delete_time_slot(self, slot_id: int) -> None:
        """Delete one time slot."""
        await self.coordinator.async_delete_time_slot(self._serial, slot_id)

    async def async_clear_schedule(self, confirm: bool) -> None:
        """Delete every time slot after explicit confirmation."""
        if not confirm:
            raise validation_error("clear_schedule_not_confirmed")
        await self.coordinator.async_clear_schedule(self._serial)

    async def async_get_schedule(self) -> dict[str, Any]:
        """Return the time slots (response-only action)."""
        schedule = self.device_data.schedule
        slots = []
        for slot in self._slots:
            interval = slot_interval(slot)
            slots.append(
                {
                    "slot_id": slot.slot_id,
                    "day": HA_DAYS[interval[0]] if interval else None,
                    "start": slot.start_time,
                    "end": slot.end_time,
                    **slot_options(slot),
                }
            )
        return {
            "schedule_available": schedule is not None,
            "writable": self.writable,
            "time_slots": slots,
        }


def _in_zone(value: datetime, zone: tzinfo) -> datetime:
    """Return an event time in the schedule zone (naive times are local there)."""
    if value.tzinfo is None:
        return value.replace(tzinfo=zone)
    return value.astimezone(zone)


def _slot_id(uid: str) -> int:
    """Return the slot id of an event uid."""
    if isinstance(uid, str) and uid.isascii() and uid.isdigit() and int(uid) > 0:
        return int(uid)
    raise validation_error("time_slot_not_found")
