"""Data update coordinator for the Qubo integration."""
from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from qubopy import QuboAuth, QuboClient
from qubopy.mqtt import QuboMqttEvent, QuboMqttListener
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
        self._mqtt: QuboMqttListener | None = None

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

        await self._async_setup_mqtt(purifiers)
        await self._async_request_aqi(purifiers)

        return purifiers

    async def _async_setup_mqtt(self, purifiers: dict[str, QuboAirPurifier]) -> None:
        """Start the MQTT listener once, so air-quality arrives by push."""
        if self._mqtt is not None or not purifiers:
            return
        unit_id = next((p.unit_id for p in purifiers.values() if p.unit_id), None)
        if not unit_id:
            return
        listener = QuboMqttListener(self.client, unit_uuid=unit_id)
        try:
            await listener.start()
        except (QuboError, ImportError, OSError) as err:
            # Push is an optimisation: polling still works without it.
            _LOGGER.debug("MQTT listener unavailable, staying on polling: %s", err)
            return
        listener.add_event_listener(self._handle_mqtt_event, service="aqiStatus")
        self._mqtt = listener

    @callback
    def _handle_mqtt_event(self, event: QuboMqttEvent) -> None:
        """Apply a pushed aqiStatus reading without waiting for the next poll."""
        if not self.data:
            return
        purifier = self.data.get(event.device_uuid)
        if purifier is None or purifier.shadow is None:
            return
        pm25 = event.parameters.get("PM25")
        if pm25 is None:
            return
        purifier.shadow.reported["aqiStatus_PM25"] = pm25
        self.async_set_updated_data(self.data)

    async def _async_request_aqi(self, purifiers: dict[str, QuboAirPurifier]) -> None:
        """Ask each purifier for a fresh air-quality sample.

        These devices report air quality only when asked, and the request is
        only honoured over MQTT - the REST command endpoint accepts the same
        envelope and returns success without the device acting on it.
        """
        if self._mqtt is None:
            return
        for purifier in purifiers.values():
            try:
                await self._mqtt.publish_service_command(
                    device_uuid=purifier.device_id,
                    service="aqiRefresh",
                    command="refresh",
                    unit_uuid=purifier.unit_id,
                )
            except (QuboError, ValueError) as err:
                _LOGGER.debug("aqiRefresh failed for %s: %s", purifier.name, err)

    async def async_close(self) -> None:
        """Release the MQTT listener and the underlying HTTP session."""
        if self._mqtt is not None:
            await self._mqtt.stop()
            self._mqtt = None
        await self.client.close()
