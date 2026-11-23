"""Buttons per iDevice: play sound, refresh now."""

from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import FindMyAppleConfigEntry
from .coordinator import FindMyAppleCoordinator
from .entity import FindMyAppleEntity, add_entities_for_new_items
from .models import TrackedItem


def _buttons(coordinator: FindMyAppleCoordinator, item_id: str, item: TrackedItem) -> list[ButtonEntity]:
    buttons: list[ButtonEntity] = [RefreshButton(coordinator, item_id)]
    if item.can_play_sound:
        buttons.append(PlaySoundButton(coordinator, item_id))
    return buttons


async def async_setup_entry(
    hass: HomeAssistant, entry: FindMyAppleConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    entry.async_on_unload(add_entities_for_new_items(entry.runtime_data.coordinator, async_add_entities, _buttons))


class PlaySoundButton(FindMyAppleEntity, ButtonEntity):
    _attr_translation_key = "play_sound"

    def __init__(self, coordinator: FindMyAppleCoordinator, item_id: str) -> None:
        super().__init__(coordinator, item_id, "play_sound")

    async def async_press(self) -> None:
        await self.coordinator.async_play_sound(self._item_id)


class RefreshButton(FindMyAppleEntity, ButtonEntity):
    _attr_translation_key = "refresh"

    def __init__(self, coordinator: FindMyAppleCoordinator, item_id: str) -> None:
        super().__init__(coordinator, item_id, "refresh")

    async def async_press(self) -> None:
        await self.coordinator.async_locate_now()
