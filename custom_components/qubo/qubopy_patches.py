"""Runtime patches for known-broken/incomplete qubopy behavior.

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

Separately, qubopy's phone+OTP login (QuboAuth.get_phone_otp_profiles /
select_profile) only ever captures the short-lived (~1hr) `token` returned by
`/user/phone/verify`, which carries no refreshToken - so a phone-login session
silently dies after an hour with no way to renew it. Tracing what the real
Android app does after OTP verification (`MobileOtpVerificationFragment`'s
"LOGIN_OTP_VALIDATE" success path -> native `Mobile_userManager::userPhoneLogin`)
showed it makes one more call, `/user/emailprofile/login`, authenticated with
that short-lived token as a `Subscriber-Key` header, which mints a real
accessToken *and* a long-lived (~179 day) refreshToken. `finalize_phone_login`
below replays that call so phone-login sessions can be refreshed indefinitely
via qubopy's own (already-correct) `QuboAuth.refresh_session()`.

This module monkey-patches qubopy.devices.purifier.QuboAirPurifier and
qubopy.auth.QuboAuth in place at integration startup. Remove the purifier
patches once their fixes land in an upstream qubopy release, and remove
`finalize_phone_login` once qubopy's own phone-login flow calls
`/user/emailprofile/login` itself (tracked: https://github.com/selvakk2k/qubopy).
"""
from __future__ import annotations

import json
import logging
import time
from typing import Any

import aiohttp

from qubopy.auth import QuboAuth as _QuboAuthClass
from qubopy.auth import _decode_jwt_expiry
from qubopy.devices import purifier as _purifier_module
from qubopy.devices.base import _as_bool, _first_not_none
from qubopy.exceptions import QuboAuthError, QuboCommandError, QuboConnectionError
from qubopy.models import TokenPayload

_LOGGER = logging.getLogger(__name__)

_PATCHED_ATTR = "_qubo_integration_patched"

# Not in qubopy.const - only the native app's phone-login finalize step uses it.
PATH_EMAIL_PROFILE_LOGIN = "/sms/api/v1/sp/{sp}/user/emailprofile/login"


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


async def _finalize_phone_login(self: Any, profile: dict[str, Any]) -> TokenPayload:
    """Exchange a phone-OTP session token for one with a real refresh token.

    `profile` is one of the dicts returned by `get_phone_otp_profiles()` /
    `verify_phone_otp()` - i.e. `{"uuid", "email", "name", "token"}`. The
    account must have an email on file (`profile["email"]`); Qubo rejects this
    call for phone-only accounts with `ERR_SM_002 "Invalid user"`.
    """
    temp_token = profile.get("accessToken") or profile.get("token")
    user_uuid = profile.get("uuid") or profile.get("userUUID") or profile.get("userId")
    email = profile.get("email")
    if not temp_token or not user_uuid:
        raise QuboAuthError("Missing tokens in OTP response")
    if not email:
        raise QuboAuthError(
            "Qubo account has no email on file; phone-login session renewal "
            "requires one - add an email to your Qubo profile first, or use "
            "email login directly"
        )

    url = f"{self.base_url}{PATH_EMAIL_PROFILE_LOGIN.format(sp=self.sp_id)}"
    headers = {
        "Content-Type": "application/json",
        "Token-Type": "USER",
        "App-Id": self.app_id,
        "Source": "Android",
        "Source-Device-Id": self.device_id,
        "User-UUID": str(user_uuid),
        "Subscriber-Key": temp_token,
    }
    payload = {"username": email, "deviceAttribute": self.device_id}

    session = await self._get_session()
    try:
        async with session.post(url, json=payload, headers=headers) as response:
            text = await response.text()
            if response.status != 200:
                _LOGGER.debug(
                    "Phone-login finalize error response (HTTP %s): %s",
                    response.status,
                    text[:200],
                )
                raise QuboAuthError(
                    f"Phone-login finalize failed (HTTP {response.status}): {text[:200]}"
                )
            data = json.loads(text)
    except aiohttp.ClientError as err:
        raise QuboConnectionError(f"Network error finalizing phone login: {err}") from err
    except json.JSONDecodeError as err:
        raise QuboAuthError(f"Invalid JSON returned by server: {err}") from err

    access_token = data.get("accessToken")
    if not access_token:
        raise QuboAuthError("Missing access token in phone-login finalize response")
    refresh_token = data.get("refreshToken")
    expires_at = _decode_jwt_expiry(access_token) or (time.time() + 86400)

    self._tokens = TokenPayload(
        access_token=access_token,
        refresh_token=refresh_token,
        user_uuid=str(user_uuid),
        mqtt_password=data.get("mqttPassword"),
        expires_at=expires_at,
    )
    return self._tokens


def apply_patches() -> None:
    """Patch QuboAirPurifier/QuboAuth with verified-correct behavior.

    Safe to call repeatedly (and from multiple modules): each target class is
    only patched once, guarded by its own `_qubo_integration_patched` flag.
    """
    purifier_cls = _purifier_module.QuboAirPurifier
    if not getattr(purifier_cls, _PATCHED_ATTR, False):
        purifier_cls.is_on = property(_is_on)
        purifier_cls.fan_speed = property(_fan_speed)
        purifier_cls.mode = property(_mode)
        purifier_cls.pm25 = property(_pm25)
        purifier_cls.filter_life_percent = property(_filter_life_percent)
        purifier_cls.is_child_lock_on = property(_is_child_lock_on)
        purifier_cls.turn_on = _turn_on
        purifier_cls.turn_off = _turn_off
        purifier_cls.set_fan_speed = _set_fan_speed
        purifier_cls.set_mode = _set_mode
        purifier_cls.set_child_lock = _set_child_lock
        setattr(purifier_cls, _PATCHED_ATTR, True)
        _LOGGER.debug("Applied qubopy QuboAirPurifier control/state patches")

    if not getattr(_QuboAuthClass, _PATCHED_ATTR, False):
        _QuboAuthClass.finalize_phone_login = _finalize_phone_login
        setattr(_QuboAuthClass, _PATCHED_ATTR, True)
        _LOGGER.debug("Applied qubopy QuboAuth.finalize_phone_login patch")
