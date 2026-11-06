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
# "phone" stores a one-time access token/user UUID obtained via phone+OTP at
# setup time - Qubo issues no refresh token for phone logins, so this session
# simply stops working after it expires (about 1 hour); there is no silent
# re-authentication for this method yet.
CONF_AUTH_METHOD = "auth_method"
AUTH_METHOD_EMAIL = "email"
AUTH_METHOD_PHONE = "phone"

CONF_PHONE = "phone"
CONF_ISD_CODE = "isd_code"
CONF_ACCESS_TOKEN = "access_token"
CONF_USER_UUID = "user_uuid"

DEFAULT_ISD_CODE = "+91"
