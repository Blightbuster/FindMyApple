"""Constants for FindMyApple."""

from __future__ import annotations

from datetime import timedelta
from typing import Final

DOMAIN: Final = "findmyapple"

CONF_REGION: Final = "region"
CONF_REMEMBER_PASSWORD: Final = "remember_password"
CONF_INCLUDE_FAMILY: Final = "include_family"
CONF_TRACKED_DEVICES: Final = "tracked_devices"
CONF_HOME_INTERVAL: Final = "home_interval"
CONF_AWAY_MIN_INTERVAL: Final = "away_min_interval"
CONF_AWAY_MAX_INTERVAL: Final = "away_max_interval"
CONF_KIND: Final = "kind"
CONF_TRACKED_TAGS: Final = "tracked_tags"
CONF_TAG_INTERVAL: Final = "tag_interval"

# A config entry is either an account's iCloud devices or its Find My network tags.
KIND_ICLOUD: Final = "icloud"
KIND_TAGS: Final = "tags"

REGION_GLOBAL: Final = "global"
REGION_CHINA: Final = "china"

DEFAULT_HOME_INTERVAL: Final = 15  # minutes
DEFAULT_AWAY_MIN_INTERVAL: Final = 5  # minutes; each locate wakes the phone's GPS
DEFAULT_AWAY_MAX_INTERVAL: Final = 30  # minutes
# Distance from the nearest zone at which the away interval reaches its maximum.
AWAY_MAX_DISTANCE_KM: Final = 50.0

DEFAULT_TAG_INTERVAL: Final = 15  # minutes; tags rotate keys every 15 min
# Exponential backoff for throttling / 5xx, capped.
BACKOFF_START: Final = timedelta(minutes=2)
BACKOFF_MAX: Final = timedelta(hours=1)

SOURCE_ICLOUD: Final = "icloud"
SOURCE_FINDMY_NETWORK: Final = "findmy_network"

STORAGE_VERSION: Final = 1
