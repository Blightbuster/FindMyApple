"""Shared fixtures: a fake iCloud backend standing in for pyicloud."""

from __future__ import annotations

import json
from collections.abc import Generator
from pathlib import Path
from typing import Any, ClassVar
from unittest.mock import patch

import pytest

from custom_components.findmyapple.backends.icloud import DeviceSummary, TwoFactorRequired, parse_device
from custom_components.findmyapple.models import TrackedItem

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations: None) -> None:
    """Load custom_components/ in every test."""


def load_devices() -> list[dict[str, Any]]:
    return json.loads((FIXTURES / "refresh_client.json").read_text(encoding="utf-8"))["content"]


class FakeBackend:
    """Behaves like ICloudBackend. Class attributes steer every instance a test creates."""

    login_error: Exception | None = None
    needs_2fa: bool = False
    security_key: bool = False
    valid_code: str = "123456"
    fetch_error: Exception | None = None
    devices: ClassVar[list[dict[str, Any]]] = []
    instances: ClassVar[list[FakeBackend]] = []

    def __init__(self, apple_id: str, password: str | None, session_dir: Path, **kwargs: Any) -> None:
        self.apple_id = apple_id
        self.password = password
        self.kwargs = kwargs
        self.fetch_calls: list[bool] = []
        self.sounds: list[str] = []
        self._pending_2fa = False
        FakeBackend.instances.append(self)

    def login(self) -> None:
        if FakeBackend.login_error is not None:
            raise FakeBackend.login_error
        if FakeBackend.needs_2fa:
            self._pending_2fa = True
            raise TwoFactorRequired

    @property
    def uses_security_key(self) -> bool:
        return FakeBackend.security_key

    @property
    def two_factor_delivery(self) -> str:
        return "trusted_device"

    def validate_2fa_code(self, code: str) -> bool:
        ok = code == FakeBackend.valid_code
        if ok:
            self._pending_2fa = False
        return ok

    def list_devices(self) -> list[DeviceSummary]:
        if FakeBackend.fetch_error is not None:
            raise FakeBackend.fetch_error
        return [
            DeviceSummary(id=d["id"], name=d["name"], model=d.get("deviceDisplayName", ""), owner=None)
            for d in FakeBackend.devices
        ]

    def fetch(self, locate: bool) -> dict[str, TrackedItem]:
        self.fetch_calls.append(locate)
        if FakeBackend.fetch_error is not None:
            raise FakeBackend.fetch_error
        return {d["id"]: parse_device(d) for d in FakeBackend.devices}

    def play_sound(self, device_id: str) -> None:
        self.sounds.append(device_id)

    def close(self) -> None:
        pass


@pytest.fixture
def fake_backend() -> Generator[type[FakeBackend]]:
    FakeBackend.login_error = None
    FakeBackend.needs_2fa = False
    FakeBackend.security_key = False
    FakeBackend.fetch_error = None
    FakeBackend.devices = load_devices()
    FakeBackend.instances = []
    with (
        patch("custom_components.findmyapple.ICloudBackend", FakeBackend),
        patch("custom_components.findmyapple.config_flow.ICloudBackend", FakeBackend),
    ):
        yield FakeBackend
