"""GPS device_tracker per tracked item: shows it on the map."""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

from homeassistant.components.device_tracker import SourceType, TrackerEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import FindMyAppleConfigEntry
from .const import SOURCE_ICLOUD
from .coordinator import FindMyAppleCoordinator
from .entity import FindMyAppleEntity, add_entities_for_new_items
from .models import TrackedItem

# First match wins. The model name or Apple's device class, e.g. "iPhone16-1-1-0" or "iPod touch (4th generation)".
_ICONS = (
    ("iPhone", "mdi:cellphone"),
    ("iPod", "mdi:cellphone"),
    ("iPad", "mdi:tablet"),
    ("Mac", "mdi:laptop"),
    ("Watch", "mdi:watch"),
    ("AirPods", "mdi:earbuds"),
    ("AppleTV", "mdi:television"),
    ("Apple TV", "mdi:television"),
)


def icon_for(item: TrackedItem) -> str | None:
    """A Material icon for the kind of device (what shows when there is no emoji)."""
    if item.source != SOURCE_ICLOUD:
        return "mdi:tag"
    for text in (item.raw.get("deviceClass"), item.model, item.model_display_name):
        for prefix, icon in _ICONS:
            if isinstance(text, str) and text.startswith(prefix):
                return icon
    return None


def emoji_picture(emoji: str) -> str:
    """The emoji as a small image, so every card and the map's markers can show it as the entity picture."""
    svg = (
        "<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'>"
        "<circle cx='50' cy='50' r='50' fill='#fff'/>"
        "<text x='50' y='52' font-size='62' text-anchor='middle' dominant-baseline='central'>"
        f"{emoji.replace('&', '&amp;').replace('<', '&lt;')}</text></svg>"
    )
    return "data:image/svg+xml;charset=utf-8," + quote(svg)


async def async_setup_entry(
    hass: HomeAssistant, entry: FindMyAppleConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    entry.async_on_unload(
        add_entities_for_new_items(
            entry.runtime_data.coordinator,
            async_add_entities,
            lambda coordinator, item_id, _item: [FindMyAppleTracker(coordinator, item_id)],
        )
    )


class FindMyAppleTracker(FindMyAppleEntity, TrackerEntity):
    _attr_name = None  # use the device name
    _attr_translation_key = "location"

    def __init__(self, coordinator: FindMyAppleCoordinator, item_id: str) -> None:
        super().__init__(coordinator, item_id, "tracker")

    @property
    def source_type(self) -> SourceType:
        return SourceType.GPS

    @property
    def latitude(self) -> float | None:
        return self.item.latitude if self.item else None

    @property
    def longitude(self) -> float | None:
        return self.item.longitude if self.item else None

    @property
    def location_accuracy(self) -> float:
        return float(self.item.accuracy or 0) if self.item else 0.0

    @property
    def icon(self) -> str | None:
        return icon_for(self.item) if self.item else None

    @property
    def entity_picture(self) -> str | None:
        return emoji_picture(self.item.emoji) if self.item and self.item.emoji else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        item = self.item
        if item is None:
            return {}
        return {
            # Not the deprecated TrackerEntity.battery_level property: same attribute, plain data.
            "battery_level": item.battery_level,
            "last_seen": item.timestamp.isoformat() if item.timestamp else None,
            "source": item.source,
            "position_type": item.position_type,
            "is_old": item.is_old,
            "is_inaccurate": item.is_inaccurate,
            "owner": item.owner,
        }
