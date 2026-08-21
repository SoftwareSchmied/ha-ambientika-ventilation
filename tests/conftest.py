"""Shared test fixtures for Ambientika."""

from __future__ import annotations

from typing import Any

import pytest

pytest_plugins = "pytest_homeassistant_custom_component"


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations: Any) -> None:
    """Enable custom integrations for all tests."""


@pytest.fixture
def houses_payload() -> list[dict[str, Any]]:
    """Return a representative multi-device account payload."""
    return [
        {
            "houseId": 10,
            "houseName": "Home",
            "nonGeminiZones": [
                {
                    "name": "Ground floor",
                    "rooms": [
                        {
                            "name": "LivingRoom",
                            "devices": [
                                {
                                    "id": 101,
                                    "serialNumber": "AABBCCDDEEFF",
                                    "name": "Living room",
                                    "deviceType": "Diamond",
                                    "deviceSubtype": "Version160",
                                    "role": "Master",
                                    "zoneIndex": 1,
                                    "installation": "2025-01-02T03:04:05+00:00",
                                    "roomId": 201,
                                    "microFwVersion": "1.2.3",
                                    "radioFwVersion": "2.3.4",
                                    "radioAtCommandsFwVersion": "3.4.5",
                                }
                            ],
                        }
                    ],
                }
            ],
            "geminiDevices": [
                {
                    "id": 102,
                    "serialNumber": "112233445566",
                    "name": "Bedroom",
                    "deviceType": "Gemini",
                    "role": "Master",
                }
            ],
        }
    ]


@pytest.fixture
def status_payload() -> dict[str, Any]:
    """Return a complete current API status payload."""
    return {
        "deviceSerialNumber": "AABBCCDDEEFF",
        "operatingMode": "ManualHeatRecovery",
        "fanSpeed": "Medium",
        "humidityLevel": "Normal",
        "lightSensorLevel": "Low",
        "temperature": 21,
        "humidity": 48,
        "airQuality": "Good",
        "humidityAlarm": False,
        "filtersStatus": "Medium",
        "nightAlarm": False,
        "deviceRole": "Master",
        "lastOperatingMode": "ManualHeatRecovery",
        "signalStrenght": 73,
        "isScheduled": "Off",
        "isTurboAvailable": True,
    }
