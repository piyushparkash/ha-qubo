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

# Config entry data keys for auth. "email" stores email/password and lets the
# coordinator log in fresh on every HA restart (long-lived refresh token).
# "phone" stores the access/refresh token pair obtained via phone+OTP at setup
# time, finalized through qubopy_patches.finalize_phone_login() into a real
# session (same ~179-day refresh token lifetime as email login) - see that
# module's docstring for why this extra step is needed.
CONF_AUTH_METHOD = "auth_method"
AUTH_METHOD_EMAIL = "email"
AUTH_METHOD_PHONE = "phone"

CONF_PHONE = "phone"
CONF_ISD_CODE = "isd_code"
CONF_ACCESS_TOKEN = "access_token"
CONF_REFRESH_TOKEN = "refresh_token"
CONF_USER_UUID = "user_uuid"

DEFAULT_ISD_CODE = "+91"
