"""Data update coordinator for the Qubo integration."""
from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from qubopy import QuboAuth, QuboClient
from qubopy.devices import QuboAirPurifier
from qubopy.exceptions import QuboAuthError, QuboConnectionError, QuboError

from .const import (
    AUTH_METHOD_PHONE,
    CONF_ACCESS_TOKEN,
    CONF_AUTH_METHOD,
    CONF_USER_UUID,
    DEVICE_ATTRIBUTE,
    DOMAIN,
    SCAN_INTERVAL,
)

_LOGGER = logging.getLogger(__name__)


class QuboDataUpdateCoordinator(DataUpdateCoordinator[dict[str, QuboAirPurifier]]):
    """Fetch and cache state for every Qubo air purifier on the account."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=SCAN_INTERVAL,
        )
        self.entry = entry
        self.auth: QuboAuth | None = None
        self.client: QuboClient | None = None
        self._logged_in = False

    async def _async_ensure_client(self) -> None:
        """Lazily construct the auth/client pair off the event loop.

        QuboAuth() reads/writes a local device-id file synchronously in its
        constructor, so it must not be called directly from async code.
        """
        if self.client is None:
            self.auth = await self.hass.async_add_executor_job(QuboAuth)
            self.client = QuboClient(self.auth)

    @property
    def _is_phone_login(self) -> bool:
        return self.entry.data.get(CONF_AUTH_METHOD) == AUTH_METHOD_PHONE

    async def _async_login(self) -> None:
        await self._async_ensure_client()
        if self._is_phone_login:
            # Qubo issues no refresh token for phone-verified sessions, so
            # there is nothing to silently re-authenticate with here - just
            # adopt the access token obtained once during the config flow.
            # It will stop working after it expires (about an hour); see
            # the auth-error handling below and the README.
            self.auth.set_tokens(
                access_token=self.entry.data[CONF_ACCESS_TOKEN],
                user_uuid=self.entry.data[CONF_USER_UUID],
            )
        else:
            await self.auth.login_with_email(
                self.entry.data[CONF_EMAIL],
                self.entry.data[CONF_PASSWORD],
                device_attribute=DEVICE_ATTRIBUTE,
            )
        self._logged_in = True

    async def _async_update_data(self) -> dict[str, QuboAirPurifier]:
        try:
            if not self._logged_in:
                await self._async_login()
            devices = await self.client.get_all_devices()
        except QuboAuthError as err:
            if self._is_phone_login:
                # Re-running _async_login() would just reapply the same
                # expired token - there is no silent way to get a new one
                # without a fresh OTP, so fail this cycle outright instead
                # of retrying pointlessly.
                raise UpdateFailed(
                    "Qubo phone-login session has expired (phone logins only "
                    "last about an hour and cannot be silently renewed). "
                    "Remove and re-add this integration with a fresh OTP, or "
                    "switch to email login for an unattended setup."
                ) from err
            # Access token rejected and could not be silently refreshed - retry once
            # with a full re-login before giving up on this cycle.
            self._logged_in = False
            try:
                await self._async_login()
                devices = await self.client.get_all_devices()
            except QuboError as login_err:
                raise UpdateFailed(
                    f"Re-authentication with Qubo failed: {login_err}"
                ) from login_err
        except QuboConnectionError as err:
            raise UpdateFailed(f"Error communicating with Qubo cloud: {err}") from err

        purifiers: dict[str, QuboAirPurifier] = {
            device.device_id: device
            for device in devices
            if isinstance(device, QuboAirPurifier)
        }

        for purifier in purifiers.values():
            try:
                await purifier.refresh()
            except QuboError as err:
                _LOGGER.debug("Failed to refresh %s: %s", purifier.name, err)

        return purifiers

    async def async_close(self) -> None:
        """Release the underlying HTTP session."""
        await self.client.close()
