"""Config flow for the Qubo Smart Home integration."""
from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.data_entry_flow import FlowResult
from homeassistant.exceptions import HomeAssistantError

from qubopy import QuboAuth
from qubopy.exceptions import QuboAuthError, QuboConnectionError

from .const import DEVICE_ATTRIBUTE, DOMAIN

_LOGGER = logging.getLogger(__name__)

STEP_USER_DATA_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_EMAIL): str,
        vol.Required(CONF_PASSWORD): str,
    }
)


async def _validate_credentials(email: str, password: str) -> str:
    """Validate credentials against the Qubo cloud and return the account UUID."""
    auth = QuboAuth()
    try:
        tokens = await auth.login_with_email(
            email, password, device_attribute=DEVICE_ATTRIBUTE
        )
    except QuboAuthError as err:
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

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle the initial step."""
        errors: dict[str, str] = {}
        if user_input is not None:
            self._async_abort_entries_match({CONF_EMAIL: user_input[CONF_EMAIL]})
            try:
                await _validate_credentials(
                    user_input[CONF_EMAIL], user_input[CONF_PASSWORD]
                )
            except CannotConnect:
                errors["base"] = "cannot_connect"
            except InvalidAuth:
                errors["base"] = "invalid_auth"
            except Exception:  # noqa: BLE001
                _LOGGER.exception("Unexpected exception during Qubo login")
                errors["base"] = "unknown"
            else:
                return self.async_create_entry(
                    title=user_input[CONF_EMAIL],
                    data=user_input,
                )

        return self.async_show_form(
            step_id="user", data_schema=STEP_USER_DATA_SCHEMA, errors=errors
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
