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

from .const import DEVICE_ATTRIBUTE, DOMAIN, SCAN_INTERVAL

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

    async def _async_login(self) -> None:
        await self._async_ensure_client()
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
        except QuboAuthError:
            # Access token rejected and could not be silently refreshed - retry once
            # with a full re-login before giving up on this cycle.
            self._logged_in = False
            try:
                await self._async_login()
                devices = await self.client.get_all_devices()
            except QuboError as err:
                raise UpdateFailed(f"Re-authentication with Qubo failed: {err}") from err
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
