"""Privacy test: no entity attribute exposes identifying account data."""

from __future__ import annotations

import json
from dataclasses import replace

from custom_components.ambientika_ventilation.binary_sensor import (
    BINARY_SENSORS,
    AmbientikaBinarySensor,
)
from custom_components.ambientika_ventilation.button import (
    AmbientikaResetFilterButton,
)
from custom_components.ambientika_ventilation.fan import AmbientikaFan
from custom_components.ambientika_ventilation.models import (
    AmbientikaSchedule,
    AmbientikaTimeSlot,
)
from custom_components.ambientika_ventilation.select import SELECTS, AmbientikaSelect
from custom_components.ambientika_ventilation.sensor import (
    DEVICE_SENSORS,
    SENSORS,
    AmbientikaDeviceSensor,
    AmbientikaScheduleSensor,
    AmbientikaSensor,
)
from custom_components.ambientika_ventilation.switch import AmbientikaScheduleSwitch

from .test_entities import SERIAL, coordinator_with_status

HOUSE_ID = 987654
ADDRESS = "Example street 12, Sampletown"
LATITUDE = 47.123456
LONGITUDE = 8.654321
EMAIL = "someone@example.com"
FORBIDDEN_KEYS = {
    "serial",
    "serial_number",
    "house_id",
    "address",
    "latitude",
    "longitude",
    "email",
    "username",
    "current_house_time",
}


def _all_entities():
    coordinator = coordinator_with_status()
    data = coordinator.data.devices[SERIAL]
    coordinator.data.devices[SERIAL] = replace(
        data,
        device=replace(
            data.device,
            device_id=11,
            house_id=HOUSE_ID,
            house_name=EMAIL,
            house_address=ADDRESS,
            house_latitude=LATITUDE,
            house_longitude=LONGITUDE,
            house_timezone=2,
            house_iana_timezone="Europe/Berlin",
            house_current_time="2026-01-01T00:00:00",
            house_zones_count=1,
            house_devices_count=2,
            room_id=3,
            zone_id=4,
            zone_index=1,
            room_devices_count=1,
        ),
        schedule=AmbientikaSchedule(
            schedule_id=7,
            zone_id=4,
            house_id=HOUSE_ID,
            device_id=11,
            time_slots=(
                AmbientikaTimeSlot(slot_id=8, day_of_week="Monday", schedule_id=7),
            ),
        ),
    )
    entities = [
        AmbientikaFan(coordinator, SERIAL),
        AmbientikaScheduleSwitch(coordinator, SERIAL),
        AmbientikaResetFilterButton(coordinator, SERIAL),
        AmbientikaScheduleSensor(coordinator, SERIAL),
    ]
    entities += [AmbientikaSensor(coordinator, SERIAL, item) for item in SENSORS]
    entities += [AmbientikaDeviceSensor(coordinator, SERIAL, i) for i in DEVICE_SENSORS]
    entities += [
        AmbientikaBinarySensor(coordinator, SERIAL, item) for item in BINARY_SENSORS
    ]
    entities += [AmbientikaSelect(coordinator, SERIAL, item) for item in SELECTS]
    return entities


def _attributes(entity) -> dict:
    attributes: dict = {}
    for name in ("extra_state_attributes", "capability_attributes", "state_attributes"):
        attributes.update(getattr(entity, name, None) or {})
    return attributes


def test_entity_attributes_never_expose_identifying_data() -> None:
    """Keys and values of every entity attribute are free of private data."""
    entities = _all_entities()
    assert len(entities) > 30

    for entity in entities:
        attributes = _attributes(entity)
        assert not FORBIDDEN_KEYS & {key.lower() for key in attributes}, entity
        text = json.dumps(attributes, default=str)
        for private in (SERIAL, str(HOUSE_ID), ADDRESS, str(LATITUDE), str(LONGITUDE)):
            assert private not in text, (entity, private)
        assert EMAIL not in text, entity
