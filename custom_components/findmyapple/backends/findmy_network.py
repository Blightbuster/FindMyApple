"""Find My network backend: AirTags and "Works with Find My" tags, via FindMy.py.

The account signs in as an emulated Mac (see macanisette.py). Tag keys come from the account's
iCloud keychain (BeaconStore), unlocked once with an Apple device's screen passcode.

Everything here is async except opening the emulated Mac, which runs in an executor.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from findmy import AsyncAppleAccount, FindMyAccessory, LoginState
from findmy.errors import (
    AppleServiceUnavailableError,
    EmptyResponseError,
    GrandSlamRefusedError,
    InvalidCredentialsError,
    UnauthorizedError,
    UnhandledProtocolError,
)
from findmy.reports.twofactor import (
    AsyncSecondFactorMethod,
    SmsSecondFactorMethod,
    TrustedDeviceSecondFactorMethod,
)

from ..const import SOURCE_FINDMY_NETWORK
from ..macanisette import EmulatedMac, EmulatedMacAnisetteProvider, EmulatedMacError
from ..models import TrackedItem
from .icloud import AuthFailed, Throttled, TwoFactorRequired

_LOGGER = logging.getLogger(__name__)

# A tag whose key range for the last 7 days spans more indices than this is "stale": its last
# known alignment is old (~16 days at 15-minute rotation), so a normal locate would scan thousands
# of keys. Stale tags are probed with their daily (secondary) keys instead: one batched request.
STALE_SPAN = 1536
# How often a stale tag is probed at most.
PROBE_INTERVAL = timedelta(hours=24)
REPORT_WINDOW = timedelta(days=7)  # Apple keeps reports for about a week
_PRIMARY_PER_SECONDARY = 96  # 15-minute primary keys per daily secondary key

# Models that are Apple devices with their own iCloud backend; not pre-selected as tags.
_APPLE_DEVICE_PREFIXES = ("iPhone", "iPad", "Mac", "Watch", "AirPods", "iPod", "AppleTV")


class MachineError(Exception):
    """The emulated Mac is missing, broken, or Apple refused its headers."""


@dataclass(slots=True)
class TagInfo:
    id: str
    name: str
    model: str | None

    @property
    def is_apple_device(self) -> bool:
        return bool(self.model) and self.model.startswith(_APPLE_DEVICE_PREFIXES)


def tag_id(accessory: FindMyAccessory) -> str:
    return accessory.identifier or accessory.master_key.hex()[:32]


def describe(accessory: FindMyAccessory) -> TagInfo:
    return TagInfo(id=tag_id(accessory), name=accessory.name or "Tag", model=accessory.model)


async def fetch_emojis(client: Any) -> dict[str, str]:
    """The emoji the owner gave each tag, by tag id. Cosmetic, so any failure just means "none".

    The library drops it when it builds an accessory, but the naming record it comes from is in the
    same zone, so one more fetch with the keys the client already holds gets it.
    """
    from findmy.cloudkit.beacons import decrypt_records, group_records

    try:
        zone_keys = await client.zone_keys()
        store = client.store
        records = await store.fetch_records()
        decrypted = decrypt_records(
            records, zone_keys, default_master_keys=await store.zone_record_defaults(zone_keys)
        )
        emojis: dict[str, str] = {}
        for group in group_records(decrypted):
            emoji = group.naming.values.get("emoji") if group.naming is not None else None
            if isinstance(emoji, str) and emoji.strip():
                emojis[group.beacon.name] = emoji.strip()[:16]
        return emojis
    except Exception:
        _LOGGER.debug("Could not read the tags' emoji", exc_info=True)
        return {}


async def async_open_machine(root: Path, *, provision: bool) -> EmulatedMacAnisetteProvider:
    """Open (and on first use provision) the account's emulated Mac. Blocking work runs in a thread."""

    def _open() -> EmulatedMacAnisetteProvider:
        return EmulatedMacAnisetteProvider(EmulatedMac.open(root, provision=provision))

    try:
        return await asyncio.to_thread(_open)
    except EmulatedMacError as err:
        raise MachineError(str(err)) from err


def new_account(provider: EmulatedMacAnisetteProvider) -> AsyncAppleAccount:
    # The machine's own ids: provisioning registered exactly these with Apple.
    return AsyncAppleAccount(provider, uid=provider.uid, devid=provider.devid)


def restore_account(provider: EmulatedMacAnisetteProvider, state: dict[str, Any]) -> AsyncAppleAccount:
    return AsyncAppleAccount(provider, state_info=state)


def two_factor_label(method: AsyncSecondFactorMethod) -> str:
    if isinstance(method, TrustedDeviceSecondFactorMethod):
        return "trusted_device"
    if isinstance(method, SmsSecondFactorMethod):
        return f"sms {method.phone_number}"
    return type(method).__name__


def classify(err: BaseException) -> Exception:
    """Map FindMy.py errors to the integration's: sign in again / machine trouble / transient."""
    text = str(err)
    if isinstance(err, GrandSlamRefusedError) and (
        err.before_password or str(err.ec) in ("-80009", "-22421")
    ):
        # Refused at SRP init (before the password is used) or "MID is invalid" / resync wanted:
        # Apple declined the emulated Mac's headers, so retyping the password wouldn't help.
        return MachineError(text)
    if isinstance(err, (InvalidCredentialsError, UnauthorizedError)):
        return AuthFailed(text)
    if isinstance(err, EmulatedMacError):
        return MachineError(text)
    if isinstance(err, (AppleServiceUnavailableError, EmptyResponseError, UnhandledProtocolError, OSError)):
        return Throttled(text)
    return Throttled(f"{type(err).__name__}: {text}")


class TagSession:
    """A signed-in account plus the accessories to locate. Used by the coordinator."""

    def __init__(
        self,
        account: AsyncAppleAccount,
        accessories: dict[str, FindMyAccessory],
        emojis: dict[str, str] | None = None,
    ) -> None:
        self.account = account
        self.accessories = accessories
        self.emojis = emojis or {}
        self._last_probe: dict[str, datetime] = {}

    @classmethod
    def from_state(cls, provider: EmulatedMacAnisetteProvider, state: dict[str, Any]) -> TagSession:
        account = restore_account(provider, state["account"])
        accessories = {}
        for raw in state.get("accessories", []):
            accessory = FindMyAccessory.from_json(raw)
            accessories[tag_id(accessory)] = accessory
        return cls(account, accessories, dict(state.get("emojis") or {}))

    def to_state(self) -> dict[str, Any]:
        return {
            "account": self.account.to_json(),
            "accessories": [a.to_json() for a in self.accessories.values()],
            "emojis": self.emojis,
        }

    async def close(self) -> None:
        await self.account.close()

    def is_stale(self, accessory: FindMyAccessory, now: datetime) -> bool:
        return accessory.get_max_index(now) - accessory.get_min_index(now - REPORT_WINDOW) > STALE_SPAN

    async def fetch(self, tracked: Iterable[str], now: datetime | None = None) -> dict[str, TrackedItem]:
        """Locate the tracked tags. Fresh tags: normal fetch. Stale tags: daily-key probe, at most daily."""
        now = now or datetime.now(UTC)
        if self.account.login_state != LoginState.LOGGED_IN:
            raise AuthFailed(f"Find My session is {self.account.login_state.name}")
        wanted = [self.accessories[i] for i in tracked if i in self.accessories]
        stale = [a for a in wanted if self.is_stale(a, now)]
        fresh = [a for a in wanted if a not in stale]

        never = datetime.min.replace(tzinfo=UTC)
        due = [a for a in stale if now - self._last_probe.get(tag_id(a), never) >= PROBE_INTERVAL]
        try:
            if due:
                await self._probe(due, now)
                # Probed tags that got an alignment are no longer stale: locate them normally now.
                fresh += [a for a in due if not self.is_stale(a, now)]
            reports: dict[Any, Any] = {}
            if fresh:
                result = await self.account.fetch_location(fresh)
                reports = result or {}
        except (AuthFailed, Throttled, MachineError, TwoFactorRequired):
            raise
        except Exception as err:
            raise classify(err) from err

        items: dict[str, TrackedItem] = {}
        for accessory in wanted:
            report = reports.get(accessory)
            item = report_to_item(accessory, report)
            item.emoji = self.emojis.get(item.id)
            items[item.id] = item
        return items

    async def _probe(self, accessories: list[FindMyAccessory], now: datetime) -> None:
        """One batched request with each stale tag's possible daily keys; a hit re-aligns the tag."""
        candidates: dict[Any, tuple[FindMyAccessory, int]] = {}
        for accessory in accessories:
            self._last_probe[tag_id(accessory)] = now
            low = accessory.get_min_index(now - REPORT_WINDOW) // _PRIMARY_PER_SECONDARY + 1
            high = accessory.get_max_index(now) // _PRIMARY_PER_SECONDARY + 2
            for secondary_index in range(max(low, 1), high + 1):
                # FindMy.py has no public accessor for one secondary key; this is the generator
                # its own keys_at() uses.
                key = accessory._secondary_gen[secondary_index]
                candidates[key] = (accessory, secondary_index)
        if not candidates:
            return
        _LOGGER.debug("Probing %d stale tag(s) with %d daily keys", len(accessories), len(candidates))
        result = await self.account.fetch_location(list(candidates)) or {}
        best: dict[str, tuple[datetime, int]] = {}
        for key, report in result.items():
            if report is None:
                continue
            accessory, secondary_index = candidates[key]
            # secondary index s is used at primary indices [(s-2)*96, s*96): the lower bound is safe
            # (update_alignment ignores going backwards, and underestimating only widens the search).
            primary_lower = max(0, (secondary_index - 2) * _PRIMARY_PER_SECONDARY)
            current = best.get(tag_id(accessory))
            if current is None or report.timestamp > current[0]:
                best[tag_id(accessory)] = (report.timestamp, primary_lower)
        for accessory in accessories:
            hit = best.get(tag_id(accessory))
            if hit is not None:
                _LOGGER.info("Re-aligned stale tag %s from a daily-key report", accessory.name)
                accessory.update_alignment(hit[0], hit[1])


def report_to_item(accessory: FindMyAccessory, report: Any) -> TrackedItem:
    info = describe(accessory)
    item = TrackedItem(id=info.id, name=info.name, source=SOURCE_FINDMY_NETWORK, model=info.model)
    if report is not None:
        item.latitude = report.latitude
        item.longitude = report.longitude
        item.accuracy = report.horizontal_accuracy
        item.timestamp = report.timestamp
        item.confidence = report.confidence
        item.raw = {"status": report.status, "confidence": report.confidence}
    return item
