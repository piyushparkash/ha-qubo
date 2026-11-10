"""Fan platform for Qubo air purifiers."""
from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.fan import FanEntity, FanEntityFeature
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util.percentage import (
    ordered_list_item_to_percentage,
    percentage_to_ordered_list_item,
)

from qubopy.devices import QuboAirPurifier

from .const import DOMAIN, MANUFACTURER, PRESET_MODES, SPEED_LEVELS
from .coordinator import QuboDataUpdateCoordinator

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Qubo air purifier fan entities from a config entry."""
    coordinator: QuboDataUpdateCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        QuboAirPurifierFan(coordinator, device_id) for device_id in coordinator.data
    )


class QuboAirPurifierFan(CoordinatorEntity[QuboDataUpdateCoordinator], FanEntity):
    """Representation of a Qubo air purifier as a Fan entity."""

    _attr_has_entity_name = True
    _attr_name = None
    _attr_translation_key = "purifier"
    _attr_supported_features = (
        FanEntityFeature.SET_SPEED
        | FanEntityFeature.PRESET_MODE
        | FanEntityFeature.TURN_ON
        | FanEntityFeature.TURN_OFF
    )
    _attr_preset_modes = PRESET_MODES
    _attr_speed_count = len(SPEED_LEVELS)

    def __init__(self, coordinator: QuboDataUpdateCoordinator, device_id: str) -> None:
        """Initialize the fan entity."""
        super().__init__(coordinator)
        self._device_id = device_id
        self._attr_unique_id = device_id
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, device_id)},
            name=self._device.name,
            manufacturer=MANUFACTURER,
            model=self._device.model,
        )

    @property
    def _device(self) -> QuboAirPurifier:
        return self.coordinator.data[self._device_id]

    @property
    def available(self) -> bool:
        """Return True if the coordinator still reports this device."""
        return super().available and self._device_id in self.coordinator.data

    @property
    def is_on(self) -> bool | None:
        """Return True if the purifier is running."""
        return self._device.is_on

    @property
    def percentage(self) -> int | None:
        """Return the current fan speed as a percentage."""
        speed = self._device.fan_speed
        if speed is None or speed not in SPEED_LEVELS:
            return None
        return ordered_list_item_to_percentage(SPEED_LEVELS, speed)

    @property
    def preset_mode(self) -> str | None:
        """Return the current preset mode."""
        mode = self._device.mode
        return mode if mode in PRESET_MODES else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return extra diagnostic attributes."""
        return {
            "pm2_5": self._device.pm25,
            "filter_life_percent": self._device.filter_life_percent,
            "child_lock": self._device.is_child_lock_on,
        }

    async def async_turn_on(
        self,
        percentage: int | None = None,
        preset_mode: str | None = None,
        **kwargs: Any,
    ) -> None:
        """Turn the purifier on, optionally applying a speed or preset."""
        await self._device.turn_on()
        if percentage is not None:
            await self.async_set_percentage(percentage)
        if preset_mode is not None:
            await self.async_set_preset_mode(preset_mode)
        await self.coordinator.async_request_refresh()

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn the purifier off."""
        await self._device.turn_off()
        await self.coordinator.async_request_refresh()

    async def async_set_percentage(self, percentage: int) -> None:
        """Set the fan speed from a percentage, mapped onto the 3 hardware levels."""
        if percentage == 0:
            await self.async_turn_off()
            return
        speed = percentage_to_ordered_list_item(SPEED_LEVELS, percentage)
        await self._device.set_fan_speed(speed)
        await self.coordinator.async_request_refresh()

    async def async_set_preset_mode(self, preset_mode: str) -> None:
        """Set the preset mode (auto / sleep / manual)."""
        await self._device.set_mode(preset_mode)
        await self.coordinator.async_request_refresh()
