"""GPS device_tracker per tracked item: shows it on the map."""

from __future__ import annotations

from typing import Any

from homeassistant.components.device_tracker import SourceType, TrackerEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import FindMyAppleConfigEntry
from .coordinator import FindMyAppleCoordinator
from .entity import FindMyAppleEntity, add_entities_for_new_items


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
    def battery_level(self) -> int | None:
        return self.item.battery_level if self.item else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        item = self.item
        if item is None:
            return {}
        return {
            "last_seen": item.timestamp.isoformat() if item.timestamp else None,
            "source": item.source,
            "position_type": item.position_type,
            "is_old": item.is_old,
            "is_inaccurate": item.is_inaccurate,
            "owner": item.owner,
        }
