"""Test harness: a minimal Home Assistant stand-in plus the real qubopy.

Only the handful of Home Assistant symbols the integration imports at module
level are stubbed, so the tests run anywhere with plain `pip install pytest
pytest-asyncio qubopy[mqtt]` - no full Home Assistant install needed.
"""
from __future__ import annotations

import enum
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def _mod(name: str, **attrs):
    module = types.ModuleType(name)
    module.__dict__.update(attrs)
    sys.modules[name] = module
    return module


class _Platform(str, enum.Enum):
    FAN = "fan"
    SENSOR = "sensor"


class UpdateFailed(Exception):
    """Stand-in for homeassistant.helpers.update_coordinator.UpdateFailed."""


class _DataUpdateCoordinator:
    def __class_getitem__(cls, item):
        return cls

    def __init__(self, hass, logger, *, name=None, update_interval=None):
        self.hass = hass
        self.name = name
        self.data = None

    def async_set_updated_data(self, data):
        self.data = data


def _install_homeassistant_stubs() -> None:
    _mod("homeassistant")
    _mod("homeassistant.config_entries", ConfigEntry=object)
    _mod(
        "homeassistant.const",
        CONF_EMAIL="email",
        CONF_PASSWORD="password",
        Platform=_Platform,
    )
    _mod("homeassistant.core", HomeAssistant=object, callback=lambda func: func)
    _mod("homeassistant.helpers")
    _mod(
        "homeassistant.helpers.update_coordinator",
        DataUpdateCoordinator=_DataUpdateCoordinator,
        UpdateFailed=UpdateFailed,
    )


_install_homeassistant_stubs()


class FakeEntry:
    """Config entry stand-in: just `.data`."""

    def __init__(self, data: dict):
        self.data = dict(data)


class FakeConfigEntries:
    def async_update_entry(self, entry, *, data):
        entry.data = dict(data)


class FakeHass:
    def __init__(self):
        self.config_entries = FakeConfigEntries()

    async def async_add_executor_job(self, func, *args):
        return func(*args)


@pytest.fixture
def hass():
    return FakeHass()


@pytest.fixture
def make_entry():
    return FakeEntry
