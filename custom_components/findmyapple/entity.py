"""Base entity: one HA device per tracked item."""

from __future__ import annotations

from collections.abc import Callable, Iterable

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, SOURCE_ICLOUD
from .coordinator import FindMyAppleCoordinator
from .models import TrackedItem


class FindMyAppleEntity(CoordinatorEntity[FindMyAppleCoordinator]):
    """An entity bound to one tracked item of a coordinator."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: FindMyAppleCoordinator, item_id: str, key: str) -> None:
        super().__init__(coordinator)
        self._item_id = item_id
        self._attr_unique_id = f"{item_id}_{key}"
        item = self.item
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, item_id)},
            name=item.name if item else item_id,
            manufacturer="Apple" if item is None or item.source == SOURCE_ICLOUD else None,
            model=(item.model_display_name or item.model) if item else None,
        )

    @property
    def item(self) -> TrackedItem | None:
        return (self.coordinator.data or {}).get(self._item_id)

    @property
    def available(self) -> bool:
        return super().available and self.item is not None


def add_entities_for_new_items(
    coordinator: FindMyAppleCoordinator,
    async_add_entities: Callable[[Iterable[Entity]], None],
    factory: Callable[[FindMyAppleCoordinator, str, TrackedItem], Iterable[Entity]],
) -> Callable[[], None]:
    """Create entities for every item now, and for items that appear later. Returns the unsubscribe."""
    known: set[str] = set()

    def _add_new() -> None:
        data = coordinator.data or {}
        new = [item_id for item_id in data if item_id not in known]
        known.update(new)
        entities = [entity for item_id in new for entity in factory(coordinator, item_id, data[item_id])]
        if entities:
            async_add_entities(entities)

    _add_new()
    return coordinator.async_add_listener(_add_new)
