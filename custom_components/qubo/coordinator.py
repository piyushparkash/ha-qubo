"""Data update coordinator for the Qubo integration."""
from __future__ import annotations

import functools
import logging
import time

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
    CONF_DEVICE_ID,
    CONF_REFRESH_TOKEN,
    CONF_USER_UUID,
    DOMAIN,
    SCAN_INTERVAL,
)

_LOGGER = logging.getLogger(__name__)

# Qubo's cloud caps logins per account ({"code":"ERR_SM_050","message":
# ["Login Limit reached"]}). Retrying every poll only keeps the cap tripped.
RATE_LIMIT_MARKER = "ERR_SM_050"
RATE_LIMIT_BACKOFF = 30 * 60  # seconds


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
        self._login_blocked_until = 0.0
        self._mqtt: QuboMqttListener | None = None

    async def _async_ensure_client(self) -> None:
        """Lazily construct the auth/client pair off the event loop.

        QuboAuth() reads/writes a local device-id file synchronously in its
        constructor, so it must not be called directly from async code.

        The device ID is kept in the config entry so it survives container
        rebuilds (Qubo counts each distinct ID as a logged-in device). Entries
        created before that existed have none: they keep using the ID qubopy
        already derives from its file - so nothing changes for them on the
        wire - and it is saved to the entry here for next time.
        """
        if self.client is None:
            stored = self.entry.data.get(CONF_DEVICE_ID)
            if stored:
                self.auth = await self.hass.async_add_executor_job(
                    functools.partial(QuboAuth, device_id=str(stored))
                )
            else:
                self.auth = await self.hass.async_add_executor_job(QuboAuth)
                self.hass.config_entries.async_update_entry(
                    self.entry,
                    data={**self.entry.data, CONF_DEVICE_ID: self.auth.device_id},
                )
            self.client = QuboClient(self.auth)

    @property
    def _is_phone_login(self) -> bool:
        return self.entry.data.get(CONF_AUTH_METHOD) == AUTH_METHOD_PHONE

    async def _async_login(self, *, force_fresh: bool = False) -> None:
        await self._async_ensure_client()
        if time.monotonic() < self._login_blocked_until:
            raise QuboAuthError(
                "Qubo login limit reached; waiting before trying to log in again"
            )
        stored_refresh = self.entry.data.get(CONF_REFRESH_TOKEN)
        if self._is_phone_login or (stored_refresh and not force_fresh):
            # Reuse the session saved in the config entry instead of logging
            # in again: every full login counts against Qubo's per-account
            # login limit, and used to happen on every HA restart. Phone
            # entries always work this way (see qubopy_patches); email
            # entries start doing so once a session has been saved.
            self.auth.set_tokens(
                access_token=self.entry.data[CONF_ACCESS_TOKEN],
                refresh_token=stored_refresh,
                user_uuid=self.entry.data[CONF_USER_UUID],
            )
        else:
            _LOGGER.debug("Performing a full Qubo email login (no reusable session)")
            try:
                await self.auth.login_with_email(
                    self.entry.data[CONF_EMAIL],
                    self.entry.data[CONF_PASSWORD],
                    device_attribute=self.auth.device_id,
                )
            except QuboAuthError as err:
                if RATE_LIMIT_MARKER in str(err):
                    self._login_blocked_until = time.monotonic() + RATE_LIMIT_BACKOFF
                raise
        self._logged_in = True

    @callback
    def _persist_session(self) -> None:
        """Save the live session so the next setup can reuse it, not log in."""
        tokens = self.auth.tokens if self.auth else None
        if tokens is None or not tokens.refresh_token:
            return
        data = self.entry.data
        if (
            data.get(CONF_ACCESS_TOKEN) == tokens.access_token
            and data.get(CONF_REFRESH_TOKEN) == tokens.refresh_token
        ):
            return
        self.hass.config_entries.async_update_entry(
            self.entry,
            data={
                **data,
                CONF_ACCESS_TOKEN: tokens.access_token,
                CONF_REFRESH_TOKEN: tokens.refresh_token,
                CONF_USER_UUID: tokens.user_uuid,
            },
        )

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
                await self._async_login(force_fresh=True)
                devices = await self.client.get_all_devices()
            except QuboError as login_err:
                raise UpdateFailed(
                    f"Re-authentication with Qubo failed: {login_err}"
                ) from login_err
        except QuboConnectionError as err:
            raise UpdateFailed(f"Error communicating with Qubo cloud: {err}") from err

        self._persist_session()

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
