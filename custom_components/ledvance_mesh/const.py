"""Constants of the Ledvance Mesh integration."""

from __future__ import annotations

DOMAIN = "ledvance_mesh"

SUBENTRY_TYPE_LIGHT = "light"

# Config entry data: the mesh network.
CONF_NET_KEY = "net_key"
CONF_APP_KEY = "app_key"
CONF_LOCAL_ADDRESS = "local_address"

# Data of a "light" subentry.
CONF_ADDRESS = "address"  # Bluetooth address of the lamp
CONF_NAME = "name"
CONF_UNICAST = "unicast"  # mesh address of the first element
CONF_ELEMENT_COUNT = "element_count"
CONF_DEVICE_KEY = "device_key"
# Version of the configuration applied to the lamp (see `lamp.SETUP_VERSION`); a lamp lagging
# behind is configured again as soon as it is reachable.
CONF_SETUP_VERSION = "setup_version"

# Address plan: 0x0001 for Home Assistant, then lamps from 0x0010.
LOCAL_ADDRESS = 0x0001
FIRST_NODE_ADDRESS = 0x0010
LAST_NODE_ADDRESS = 0x7FFF
# Group address the lamps publish their state to.
STATUS_GROUP_ADDRESS = 0xC000

# A reset lamp advertises several times per second, while HA caches advertisements for several
# minutes: past this delay without an advertisement, the lamp is considered switched off.
ADVERTISEMENT_MAX_AGE = 30.0  # seconds

# Lamp presence. When powered, a lamp sends a Bluetooth advertisement about every second: if the
# Home Assistant adapter heard them and then stops hearing them for this long, the lamp is asked;
# without an answer it is unreachable (switched off at the wall).
ADVERTISEMENT_TIMEOUT = 15.0  # seconds
# For a lamp out of the adapter's direct range (reached through relays), we rely on the state it
# publishes at a regular interval: its heartbeat. These messages use the lamp's sequence counter,
# not ours.
HEARTBEAT_INTERVAL = 60.0  # seconds
# More than three heartbeats missed: the lamp is unreachable.
HEARTBEAT_TIMEOUT = 3.5 * HEARTBEAT_INTERVAL

# Event fired when a lamp restarts (power cut then restored, even briefly).
# Data: entity_id, subentry_id.
EVENT_LAMP_RESTARTED = f"{DOMAIN}_lamp_restarted"
