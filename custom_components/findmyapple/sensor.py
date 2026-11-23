"""Sensors per tracked item: battery, last seen, distance from home, accuracy."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import PERCENTAGE, EntityCategory, UnitOfLength
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.util.location import distance

from . import FindMyAppleConfigEntry
from .const import SOURCE_ICLOUD
from .coordinator import FindMyAppleCoordinator
from .entity import FindMyAppleEntity, add_entities_for_new_items
from .models import TrackedItem


@dataclass(frozen=True, kw_only=True)
class FMASensorDescription(SensorEntityDescription):
    value_fn: Callable[[TrackedItem, HomeAssistant], float | int | datetime | None]
    icloud_only: bool = False  # Find My network reports carry no reliable battery level


def _distance_km(item: TrackedItem, hass: HomeAssistant) -> float | None:
    if item.latitude is None or item.longitude is None:
        return None
    metres = distance(hass.config.latitude, hass.config.longitude, item.latitude, item.longitude)
    return round(metres / 1000, 3) if metres is not None else None


SENSORS: tuple[FMASensorDescription, ...] = (
    FMASensorDescription(
        key="battery",
        icloud_only=True,
        device_class=SensorDeviceClass.BATTERY,
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda item, _hass: item.battery_level,
    ),
    FMASensorDescription(
        key="last_seen",
        translation_key="last_seen",
        device_class=SensorDeviceClass.TIMESTAMP,
        value_fn=lambda item, _hass: item.timestamp,
    ),
    FMASensorDescription(
        key="distance_from_home",
        translation_key="distance_from_home",
        device_class=SensorDeviceClass.DISTANCE,
        native_unit_of_measurement=UnitOfLength.KILOMETERS,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
        value_fn=_distance_km,
    ),
    FMASensorDescription(
        key="location_accuracy",
        translation_key="location_accuracy",
        device_class=SensorDeviceClass.DISTANCE,
        native_unit_of_measurement=UnitOfLength.METERS,
        entity_category=EntityCategory.DIAGNOSTIC,
        suggested_display_precision=0,
        value_fn=lambda item, _hass: item.accuracy,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant, entry: FindMyAppleConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    entry.async_on_unload(
        add_entities_for_new_items(
            entry.runtime_data.coordinator,
            async_add_entities,
            lambda coordinator, item_id, item: [
                FMASensor(coordinator, item_id, d)
                for d in SENSORS
                if not d.icloud_only or item.source == SOURCE_ICLOUD
            ],
        )
    )


class FMASensor(FindMyAppleEntity, SensorEntity):
    entity_description: FMASensorDescription

    def __init__(self, coordinator: FindMyAppleCoordinator, item_id: str, description: FMASensorDescription) -> None:
        self.entity_description = description
        super().__init__(coordinator, item_id, description.key)

    @property
    def native_value(self) -> float | int | datetime | None:
        item = self.item
        return self.entity_description.value_fn(item, self.hass) if item else None
