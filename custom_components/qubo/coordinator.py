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
    CONF_REFRESH_TOKEN,
    CONF_USER_UUID,
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
            # Adopt the access/refresh token pair obtained once during the
            # config flow (via finalize_phone_login(), see qubopy_patches) -
            # qubopy's own refresh_session()/get_valid_access_token() renew
            # this silently from here on, same as email login. Entries added
            # before that finalize step existed have no refresh token in
            # their data; CONF_REFRESH_TOKEN is then None and the session
            # will die after ~1hr with no way to renew (see the auth-error
            # handling below) - re-adding the integration fixes that.
            self.auth.set_tokens(
                access_token=self.entry.data[CONF_ACCESS_TOKEN],
                refresh_token=self.entry.data.get(CONF_REFRESH_TOKEN),
                user_uuid=self.entry.data[CONF_USER_UUID],
            )
        else:
            await self.auth.login_with_email(
                self.entry.data[CONF_EMAIL],
                self.entry.data[CONF_PASSWORD],
                device_attribute=self.auth.device_id,
            )
        self._logged_in = True

    async def _async_update_data(self) -> dict[str, QuboAirPurifier]:
        try:
            if not self._logged_in:
                await self._async_login()
            devices = await self.client.get_all_devices()
        except QuboAuthError as err:
            if self._is_phone_login and not self.entry.data.get(CONF_REFRESH_TOKEN):
                # Pre-finalize-step entry with no refresh token at all -
                # nothing to silently renew; re-running _async_login() would
                # just reapply the same expired access token.
                raise UpdateFailed(
                    "Qubo phone-login session has expired and this entry has "
                    "no refresh token to renew it (it was added before this "
                    "integration learned how to get one). Remove and re-add "
                    "this integration with a fresh OTP."
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
