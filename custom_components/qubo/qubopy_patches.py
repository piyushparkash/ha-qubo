"""Runtime patches for known-broken qubopy device control methods.

qubopy's QuboAirPurifier.turn_on/turn_off/set_fan_speed/set_mode/set_child_lock
send a guessed command payload to the wrong endpoint. The Qubo cloud accepts it
and returns a fake success, but the physical device never receives the command.

The real request shape and endpoint were confirmed directly against the Qubo
Android app's own Java source (`ControlMonitor` / the `shadow/attribute` PUT
call site) and verified end-to-end against a live Q400 air purifier:

    PUT .../devices/{device_id}/shadow/attribute
    {"instanceId": 0, "serviceName": ..., "attributeName": ..., "attributeValue": ...}

The reported-state convenience properties (is_on, fan_speed, pm25, etc.) were
also looking for the wrong keys - the real device shadow reports values under
"<serviceName>_<attributeName>" keys (e.g. "lcSwitchControl_power"), not the
plain names qubopy assumed.

This module monkey-patches qubopy.devices.purifier.QuboAirPurifier in place at
integration startup. Remove once these fixes land in an upstream qubopy
release (tracked: https://github.com/selvakk2k/qubopy).
"""
from __future__ import annotations

import logging
from typing import Any

from qubopy.devices import purifier as _purifier_module
from qubopy.devices.base import _as_bool, _first_not_none
from qubopy.exceptions import QuboCommandError

_LOGGER = logging.getLogger(__name__)

_PATCHED_ATTR = "_qubo_integration_patched"


def _is_on(self: Any) -> bool:
    state = _first_not_none(
        self.reported_state, "lcSwitchControl_power", "state", "power", "power_state"
    )
    if isinstance(state, str):
        return state.strip().lower() == "on"
    return bool(_as_bool(state))


def _fan_speed(self: Any) -> int | None:
    val = _first_not_none(
        self.reported_state, "fanSpeedControl_speed", "speed", "fan_speed", "fanSpeed"
    )
    try:
        return int(val) if val is not None else None
    except (ValueError, TypeError):
        return None


def _mode(self: Any) -> str | None:
    val = _first_not_none(self.reported_state, "fanControlMode_state", "mode", "preset_mode")
    return str(val).lower() if val is not None else None


def _pm25(self: Any) -> int | None:
    val = _first_not_none(self.reported_state, "aqiStatus_PM25", "pm2_5", "pm25", "aqi")
    try:
        return int(val) if val is not None else None
    except (ValueError, TypeError):
        return None


def _filter_life_percent(self: Any) -> int | None:
    val = _first_not_none(
        self.reported_state, "filterReset_percentage", "filter_life", "filter_life_percent"
    )
    try:
        return int(val) if val is not None else None
    except (ValueError, TypeError):
        return None


def _is_child_lock_on(self: Any) -> bool:
    val = _first_not_none(self.reported_state, "childLockControl_state", "child_lock", "childLock")
    if isinstance(val, str):
        return val.strip().lower() == "enable"
    return bool(_as_bool(val))


async def _set_shadow_attribute(
    self: Any, service_name: str, attribute_name: str, attribute_value: Any
) -> dict[str, Any]:
    return await self.update_attribute(
        {
            "instanceId": 0,
            "serviceName": service_name,
            "attributeName": attribute_name,
            "attributeValue": attribute_value,
        }
    )


async def _turn_on(self: Any) -> dict[str, Any]:
    return await _set_shadow_attribute(self, "lcSwitchControl", "power", "on")


async def _turn_off(self: Any) -> dict[str, Any]:
    return await _set_shadow_attribute(self, "lcSwitchControl", "power", "off")


async def _set_fan_speed(self: Any, speed: int) -> dict[str, Any]:
    return await _set_shadow_attribute(self, "fanSpeedControl", "speed", str(speed))


async def _set_mode(self: Any, mode: str) -> dict[str, Any]:
    return await _set_shadow_attribute(self, "fanControlMode", "state", mode.lower())


async def _set_child_lock(self: Any, enabled: bool) -> dict[str, Any]:
    return await _set_shadow_attribute(
        self, "childLockControl", "state", "enable" if enabled else "disable"
    )


def apply_patches() -> None:
    """Patch QuboAirPurifier with verified-correct control/state behavior."""
    cls = _purifier_module.QuboAirPurifier
    if getattr(cls, _PATCHED_ATTR, False):
        return

    cls.is_on = property(_is_on)
    cls.fan_speed = property(_fan_speed)
    cls.mode = property(_mode)
    cls.pm25 = property(_pm25)
    cls.filter_life_percent = property(_filter_life_percent)
    cls.is_child_lock_on = property(_is_child_lock_on)
    cls.turn_on = _turn_on
    cls.turn_off = _turn_off
    cls.set_fan_speed = _set_fan_speed
    cls.set_mode = _set_mode
    cls.set_child_lock = _set_child_lock
    setattr(cls, _PATCHED_ATTR, True)

    _LOGGER.debug("Applied qubopy QuboAirPurifier control/state patches")
