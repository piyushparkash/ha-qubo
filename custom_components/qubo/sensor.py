"""Sensor platform for Qubo air purifiers."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONCENTRATION_MICROGRAMS_PER_CUBIC_METER, PERCENTAGE
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from qubopy.devices import QuboAirPurifier

from .const import DOMAIN, MANUFACTURER
from .coordinator import QuboDataUpdateCoordinator


@dataclass(frozen=True, kw_only=True)
class QuboSensorEntityDescription(SensorEntityDescription):
    """Describes a Qubo air purifier sensor entity."""

    value_fn: Callable[[QuboAirPurifier], Any]


SENSOR_DESCRIPTIONS: tuple[QuboSensorEntityDescription, ...] = (
    QuboSensorEntityDescription(
        key="pm25",
        translation_key="pm25",
        device_class=SensorDeviceClass.PM25,
        native_unit_of_measurement=CONCENTRATION_MICROGRAMS_PER_CUBIC_METER,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda device: device.pm25,
    ),
    QuboSensorEntityDescription(
        key="filter_life",
        translation_key="filter_life",
        icon="mdi:air-filter",
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda device: device.filter_life_percent,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Qubo air purifier sensor entities from a config entry."""
    coordinator: QuboDataUpdateCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        QuboSensor(coordinator, device_id, description)
        for device_id in coordinator.data
        for description in SENSOR_DESCRIPTIONS
    )


class QuboSensor(CoordinatorEntity[QuboDataUpdateCoordinator], SensorEntity):
    """Representation of a single Qubo air purifier sensor value."""

    entity_description: QuboSensorEntityDescription
    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: QuboDataUpdateCoordinator,
        device_id: str,
        description: QuboSensorEntityDescription,
    ) -> None:
        """Initialize the sensor entity."""
        super().__init__(coordinator)
        self.entity_description = description
        self._device_id = device_id
        self._attr_unique_id = f"{device_id}_{description.key}"
        device = coordinator.data[device_id]
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, device_id)},
            name=device.name,
            manufacturer=MANUFACTURER,
            model=device.model,
        )

    @property
    def _device(self) -> QuboAirPurifier:
        return self.coordinator.data[self._device_id]

    @property
    def available(self) -> bool:
        """Return True if the coordinator still reports this device."""
        return super().available and self._device_id in self.coordinator.data

    @property
    def native_value(self) -> Any:
        """Return the current sensor value."""
        return self.entity_description.value_fn(self._device)
