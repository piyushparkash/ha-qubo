"""Config flow for the Qubo Smart Home integration."""
from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResult
from homeassistant.exceptions import HomeAssistantError

from qubopy import QuboAuth
from qubopy.exceptions import QuboAuthError, QuboConnectionError

from .const import (
    AUTH_METHOD_EMAIL,
    AUTH_METHOD_PHONE,
    CONF_ACCESS_TOKEN,
    CONF_AUTH_METHOD,
    CONF_ISD_CODE,
    CONF_PHONE,
    CONF_REFRESH_TOKEN,
    CONF_USER_UUID,
    DEFAULT_ISD_CODE,
    DEVICE_ATTRIBUTE,
    DOMAIN,
)
from .qubopy_patches import apply_patches

_LOGGER = logging.getLogger(__name__)

# finalize_phone_login() (used below) only exists on QuboAuth once this has
# run; __init__.py only applies it on async_setup_entry, which is too late -
# the config flow uses QuboAuth itself, before any entry exists.
apply_patches()

STEP_EMAIL_DATA_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_EMAIL): str,
        vol.Required(CONF_PASSWORD): str,
    }
)

STEP_PHONE_DATA_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_ISD_CODE, default=DEFAULT_ISD_CODE): str,
        vol.Required(CONF_PHONE): str,
    }
)

STEP_PHONE_OTP_DATA_SCHEMA = vol.Schema(
    {
        vol.Required("otp"): str,
    }
)

# Substring Qubo's cloud uses in its rate-limit response body
# ({"code":"ERR_SM_050","message":["Login Limit reached"]}). Matched on the
# exception text since qubopy only exposes it as a formatted string.
RATE_LIMIT_MARKER = "ERR_SM_050"


async def _validate_credentials(hass: HomeAssistant, email: str, password: str) -> str:
    """Validate credentials against the Qubo cloud and return the account UUID."""
    # QuboAuth() reads/writes a local device-id file synchronously in its
    # constructor; keep that off the event loop.
    auth = await hass.async_add_executor_job(QuboAuth)
    try:
        tokens = await auth.login_with_email(
            email, password, device_attribute=DEVICE_ATTRIBUTE
        )
    except QuboAuthError as err:
        if RATE_LIMIT_MARKER in str(err):
            raise RateLimited from err
        raise InvalidAuth from err
    except QuboConnectionError as err:
        raise CannotConnect from err
    else:
        return tokens.user_uuid
    finally:
        await auth.close()


class QuboConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Qubo."""

    VERSION = 1

    def __init__(self) -> None:
        """Initialize the phone-login flow state."""
        self._phone: str | None = None
        self._isd_code: str | None = None
        self._phone_session_uuid: str | None = None
        self._phone_profiles: list[dict[str, Any]] = []
        self._phone_auth: QuboAuth | None = None

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Let the user choose email or phone login."""
        return self.async_show_menu(
            step_id="user",
            menu_options=["email", "phone"],
        )

    async def async_step_email(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle email/password login."""
        errors: dict[str, str] = {}
        if user_input is not None:
            self._async_abort_entries_match({CONF_EMAIL: user_input[CONF_EMAIL]})
            try:
                await _validate_credentials(
                    self.hass, user_input[CONF_EMAIL], user_input[CONF_PASSWORD]
                )
            except CannotConnect:
                errors["base"] = "cannot_connect"
            except RateLimited:
                errors["base"] = "rate_limited"
            except InvalidAuth:
                errors["base"] = "invalid_auth"
            except Exception:  # noqa: BLE001
                _LOGGER.exception("Unexpected exception during Qubo login")
                errors["base"] = "unknown"
            else:
                return self.async_create_entry(
                    title=user_input[CONF_EMAIL],
                    data={**user_input, CONF_AUTH_METHOD: AUTH_METHOD_EMAIL},
                )

        return self.async_show_form(
            step_id="email", data_schema=STEP_EMAIL_DATA_SCHEMA, errors=errors
        )

    async def async_step_phone(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Request an OTP for the given phone number."""
        errors: dict[str, str] = {}
        if user_input is not None:
            phone = user_input[CONF_PHONE]
            isd_code = user_input[CONF_ISD_CODE]
            self._async_abort_entries_match({CONF_PHONE: phone})
            auth = await self.hass.async_add_executor_job(QuboAuth)
            try:
                otp_response = await auth.request_phone_otp(
                    phone, isd_code=isd_code, device_attribute=DEVICE_ATTRIBUTE
                )
            except QuboConnectionError:
                errors["base"] = "cannot_connect"
            except QuboAuthError:
                errors["base"] = "invalid_auth"
            except Exception:  # noqa: BLE001
                _LOGGER.exception("Unexpected exception requesting Qubo phone OTP")
                errors["base"] = "unknown"
            else:
                self._phone = phone
                self._isd_code = isd_code
                self._phone_session_uuid = otp_response.get("uuid")
                self._phone_auth = auth
                return await self.async_step_phone_otp()

        return self.async_show_form(
            step_id="phone", data_schema=STEP_PHONE_DATA_SCHEMA, errors=errors
        )

    async def async_step_phone_otp(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Verify the OTP code and resolve the account profile."""
        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                profiles = await self._phone_auth.get_phone_otp_profiles(
                    self._phone,
                    user_input["otp"],
                    isd_code=self._isd_code,
                    session_uuid=self._phone_session_uuid,
                    device_attribute=DEVICE_ATTRIBUTE,
                )
            except QuboConnectionError:
                errors["base"] = "cannot_connect"
            except QuboAuthError:
                errors["base"] = "invalid_auth"
            except Exception:  # noqa: BLE001
                _LOGGER.exception("Unexpected exception verifying Qubo phone OTP")
                errors["base"] = "unknown"
            else:
                if len(profiles) == 1:
                    try:
                        return await self._async_finish_phone_login(profiles, 0)
                    except QuboConnectionError:
                        errors["base"] = "cannot_connect"
                    except QuboAuthError:
                        errors["base"] = "invalid_auth"
                    except Exception:  # noqa: BLE001
                        _LOGGER.exception(
                            "Unexpected exception finalizing Qubo phone login"
                        )
                        errors["base"] = "unknown"
                else:
                    self._phone_profiles = profiles
                    return await self.async_step_phone_select_profile()

        return self.async_show_form(
            step_id="phone_otp", data_schema=STEP_PHONE_OTP_DATA_SCHEMA, errors=errors
        )

    async def async_step_phone_select_profile(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Let the user pick an account when several are linked to one phone."""
        errors: dict[str, str] = {}
        profile_choices = {
            str(p.get("uuid")): p.get("email") or p.get("name") or str(p.get("uuid"))
            for p in self._phone_profiles
        }
        if user_input is not None:
            selected_uuid = user_input["account_uuid"]
            index = next(
                i
                for i, p in enumerate(self._phone_profiles)
                if str(p.get("uuid")) == selected_uuid
            )
            try:
                return await self._async_finish_phone_login(self._phone_profiles, index)
            except QuboConnectionError:
                errors["base"] = "cannot_connect"
            except QuboAuthError:
                errors["base"] = "invalid_auth"
            except Exception:  # noqa: BLE001
                _LOGGER.exception("Unexpected exception finalizing Qubo phone login")
                errors["base"] = "unknown"

        schema = vol.Schema({vol.Required("account_uuid"): vol.In(profile_choices)})
        return self.async_show_form(
            step_id="phone_select_profile", data_schema=schema, errors=errors
        )

    async def _async_finish_phone_login(
        self, profiles: list[dict[str, Any]], index: int
    ) -> FlowResult:
        """Finalize the selected profile's session and create the config entry.

        The OTP-verify response only carries a short-lived (~1hr) token with
        no refresh token; finalize_phone_login() exchanges it for a real
        session (see qubopy_patches docstring for why this extra step exists).
        """
        try:
            tokens = await self._phone_auth.finalize_phone_login(profiles[index])
        finally:
            await self._phone_auth.close()

        title = profiles[index].get("email") or profiles[index].get("name") or self._phone
        return self.async_create_entry(
            title=f"{title} (phone login)",
            data={
                CONF_AUTH_METHOD: AUTH_METHOD_PHONE,
                CONF_PHONE: self._phone,
                CONF_ISD_CODE: self._isd_code,
                CONF_ACCESS_TOKEN: tokens.access_token,
                CONF_REFRESH_TOKEN: tokens.refresh_token,
                CONF_USER_UUID: tokens.user_uuid,
            },
        )


class CannotConnect(HomeAssistantError):
    """Error to indicate we cannot connect to the Qubo cloud."""


class InvalidAuth(HomeAssistantError):
    """Error to indicate invalid credentials.

    Note: Qubo rejects email/password login for accounts that were
    registered via phone number (HTTP 400 ERR_SM_002). Users on a
    phone-registered account need to add an email to their Qubo profile
    first (see the README) - this also surfaces as invalid_auth here.
    """


class RateLimited(HomeAssistantError):
    """Error to indicate Qubo's cloud is rate-limiting login attempts.

    Qubo returns HTTP 400 ERR_SM_050 ("Login Limit reached") after a
    handful of attempts in a short window, independent of whether the
    credentials are actually correct.
    """
