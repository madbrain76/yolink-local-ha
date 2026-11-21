"""Constants for the YoLink Local integration."""

from datetime import timedelta

DOMAIN = "yolocal"
EVENT_DATA_RECORD = f"{DOMAIN}_data_record"

# Configuration keys
CONF_HUB_IP = "hub_ip"
CONF_SECONDARY_HUB_IP = "secondary_hub_ip"
CONF_CLIENT_ID = "client_id"
CONF_CLIENT_SECRET = "client_secret"
CONF_NET_ID = "net_id"

# Default ports
DEFAULT_HTTP_PORT = 1080
DEFAULT_MQTT_PORT = 18080
UPDATE_INTERVAL = timedelta(minutes=5)
DEVICE_DISCOVERY_INTERVAL = timedelta(minutes=1)

# API endpoints
TOKEN_ENDPOINT = "/open/yolink/token"
API_ENDPOINT = "/open/yolink/v2/api"

# Platforms we support
PLATFORMS: list[str] = [
    "sensor",
    "binary_sensor",
    "lock",
    "switch",
    "siren",
    "valve",
    "button",
]

# Garage door controllers only pulse their relay. They get a toggle button;
# open/closed comes from whichever sensor the user pairs with it in Home
# Assistant (for example a template cover). Finger (YS4908) is not enabled:
# no captures confirm its behavior yet.
GARAGE_DOOR_DEVICE_TYPES = frozenset({"GarageDoor"})
