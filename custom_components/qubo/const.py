"""Constants for the Qubo Smart Home integration."""
from datetime import timedelta

from homeassistant.const import Platform

DOMAIN = "qubo"
MANUFACTURER = "Qubo"

# Only air purifiers (HPH series) are wired up today. qubopy also models
# plugs, lights, locks, cameras and sensors - support for those device
# types can be added as separate platforms without touching this constant.
PLATFORMS = [Platform.FAN, Platform.SENSOR]

DEVICE_ATTRIBUTE = "Home Assistant"
SCAN_INTERVAL = timedelta(seconds=60)

SPEED_LEVELS = [1, 2, 3]
PRESET_MODES = ["auto", "sleep", "manual"]
