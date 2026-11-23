"""Backend-independent location model. Entities only ever read this."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


@dataclass(slots=True)
class TrackedItem:
    """One trackable thing (an iDevice or a tag) and its latest known location."""

    id: str
    name: str
    source: str
    model: str | None = None
    model_display_name: str | None = None
    owner: str | None = None  # family member name for Family Sharing devices; None = account owner
    latitude: float | None = None
    longitude: float | None = None
    accuracy: float | None = None  # metres
    timestamp: datetime | None = None  # when the position was measured
    position_type: str | None = None
    is_old: bool = False
    is_inaccurate: bool = False
    battery_level: int | None = None  # percent
    battery_charging: bool | None = None
    low_power_mode: bool | None = None
    online: bool | None = None
    confidence: int | None = None  # Find My network reports only
    can_play_sound: bool = False
    raw: dict[str, Any] = field(default_factory=dict, repr=False)  # diagnostics only

    @property
    def has_location(self) -> bool:
        return self.latitude is not None and self.longitude is not None

    def merge_previous(self, previous: TrackedItem | None) -> TrackedItem:
        """Keep the previous position when this one is missing or older ("keep last good location")."""
        if previous is None or not previous.has_location:
            return self
        if not self.has_location or (
            self.timestamp is not None and previous.timestamp is not None and self.timestamp < previous.timestamp
        ):
            self.latitude = previous.latitude
            self.longitude = previous.longitude
            self.accuracy = previous.accuracy
            self.timestamp = previous.timestamp
            self.position_type = previous.position_type
            self.is_old = True
        return self

    def to_storage(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "source": self.source,
            "model": self.model,
            "model_display_name": self.model_display_name,
            "owner": self.owner,
            "latitude": self.latitude,
            "longitude": self.longitude,
            "accuracy": self.accuracy,
            "timestamp": self.timestamp.isoformat() if self.timestamp else None,
            "position_type": self.position_type,
            "battery_level": self.battery_level,
            "battery_charging": self.battery_charging,
        }

    @classmethod
    def from_storage(cls, data: dict[str, Any]) -> TrackedItem:
        ts = data.get("timestamp")
        return cls(
            id=data["id"],
            name=data["name"],
            source=data["source"],
            model=data.get("model"),
            model_display_name=data.get("model_display_name"),
            owner=data.get("owner"),
            latitude=data.get("latitude"),
            longitude=data.get("longitude"),
            accuracy=data.get("accuracy"),
            timestamp=datetime.fromisoformat(ts) if ts else None,
            position_type=data.get("position_type"),
            battery_level=data.get("battery_level"),
            battery_charging=data.get("battery_charging"),
            is_old=True,
        )
