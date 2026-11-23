"""Binary sensors per iDevice: charging, online."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import FindMyAppleConfigEntry
from .const import SOURCE_ICLOUD
from .coordinator import FindMyAppleCoordinator
from .entity import FindMyAppleEntity, add_entities_for_new_items
from .models import TrackedItem


@dataclass(frozen=True, kw_only=True)
class FMABinaryDescription(BinarySensorEntityDescription):
    value_fn: Callable[[TrackedItem], bool | None]


BINARY_SENSORS: tuple[FMABinaryDescription, ...] = (
    FMABinaryDescription(
        key="charging",
        device_class=BinarySensorDeviceClass.BATTERY_CHARGING,
        value_fn=lambda item: item.battery_charging,
    ),
    FMABinaryDescription(
        key="online",
        translation_key="online",
        device_class=BinarySensorDeviceClass.CONNECTIVITY,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda item: item.online,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant, entry: FindMyAppleConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    entry.async_on_unload(
        add_entities_for_new_items(
            entry.runtime_data.coordinator,
            async_add_entities,
            lambda coordinator, item_id, item: (
                [FMABinarySensor(coordinator, item_id, d) for d in BINARY_SENSORS]
                if item.source == SOURCE_ICLOUD
                else []  # charging / online are iCloud-only
            ),
        )
    )


class FMABinarySensor(FindMyAppleEntity, BinarySensorEntity):
    entity_description: FMABinaryDescription

    def __init__(self, coordinator: FindMyAppleCoordinator, item_id: str, description: FMABinaryDescription) -> None:
        self.entity_description = description
        super().__init__(coordinator, item_id, description.key)

    @property
    def is_on(self) -> bool | None:
        item = self.item
        return self.entity_description.value_fn(item) if item else None
