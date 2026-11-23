"""Anisette from an emulated Mac: Apple's own macOS CoreADI run under Unicorn by `machoadi`.

Why: since 2026-10-12 Apple issues the Find My token (`searchPartyToken`) only to sign-ins whose
anisette came from a Mac or an iPhone, and the token only works alongside anisette from **that
same machine**. So the emulated Mac signs in *and* serves every later report fetch.

How:
- `machoadi` (GPL-2.0-only, parawanderer/os-findmy-e) is **never imported**. It runs as a separate
  program (`python -m machoadi.mac_anisette`), which keeps GPL code at arm's length and also suits
  it technically: one Unicorn process faults after ~8 OTP requests, so a fresh process answers
  each 30-second OTP window and the answer is reused within it.
- Apple's library is fetched from Apple (see `coreadi.py`), never shipped.
- One machine per account, for good. Apple provisions a device id only once (HTTP 433 after), and
  a lost machine means a new "MacBook Pro" entry in the user's device list. The state directory
  *is* the machine; keep it with the account and never re-provision a machine that has a store.

Adapted from OpenTagViewer's python/exporter/macadi.py (MIT, see THIRD_PARTY_NOTICES.md).

This module imports FindMy.py; the iCloud-only integration doesn't need it, so nothing imports it
at setup time yet.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import secrets
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any

from findmy.reports.anisette import BaseAnisetteProvider, DeviceIdentity

try:
    from .coreadi import LIBRARY_FILENAME, ensure_library
except ImportError:  # loaded standalone (spike scripts put this directory on sys.path)
    from coreadi import LIBRARY_FILENAME, ensure_library  # type: ignore[no-redef]

_LOGGER = logging.getLogger(__name__)

# Must match machoadi's DEFAULT_CLIENT_INFO ("<MacBookPro13,2> <Mac OS X;13.1;22C65> ..."): the
# sign-in has to describe the same machine that provisioning registered. "Mac OS X", never
# "macOS": Apple's edge has refused the latter at sign-in since 2026-11-20.
IDENTITY = DeviceIdentity(
    model="MacBookPro13,2",
    os_name="Mac OS X",
    os_version="13.1",
    os_build="22C65",
    cfnetwork="1404.0.5",
    darwin="22.2.0",
)

WINDOW_SECONDS = 30
ENGINE_TIMEOUT_SECONDS = 180  # provisioning is two round trips to Apple plus emulation
ENGINE_MODULE = "machoadi.mac_anisette"

# Serial: a label the user sees in their Apple device list. Uppercase alphanumeric, no I/O.
# The prefix makes the entry recognisable as this integration's.
SERIAL_PREFIX = "0FMAHA"
_SERIAL_ALPHABET = "0123456789ABCDEFGHJKLMNPQRSTUVWXYZ"


class EmulatedMacError(RuntimeError):
    """The emulated Mac could not be fetched, provisioned or asked for headers."""


def new_serial() -> str:
    return SERIAL_PREFIX + "".join(secrets.choice(_SERIAL_ALPHABET) for _ in range(6))


class EmulatedMac:
    """One provisioned machine: its state directory, and Apple's library to run it with. Blocking."""

    def __init__(self, root: Path, *, python: str = sys.executable) -> None:
        self.root = root
        self.state = root / "machine"
        self.library = root / LIBRARY_FILENAME
        self._python = python

    @classmethod
    def open(cls, root: Path, serial: str | None = None, *, provision: bool = True) -> EmulatedMac:
        """The machine kept under `root`, provisioning one first if there is none (and `provision`)."""
        mac = cls(root)
        ensure_library(mac.library)
        if not mac.info()["provisioned"]:
            if (mac.state / "identity.json").exists() and (mac.state / "store").exists():
                # A store that says "not provisioned" is a broken machine, not a new one. Never
                # re-provision silently: that would be a second device in the user's list.
                raise EmulatedMacError(f"The emulated Mac in {mac.state} lost its provisioning")
            if not provision:
                raise EmulatedMacError("No emulated Mac has been provisioned for this account")
            _LOGGER.info("Provisioning an emulated Mac with Apple (no Apple Account involved)")
            mac._engine("provision", "--serial", serial or new_serial())
        return mac

    def info(self) -> dict[str, Any]:
        return json.loads(self._engine("info"))

    @property
    def identity(self) -> dict[str, Any]:
        """machoadi's identity.json: device_id, local_user_uuid, client_info, host_uuid, serial, routing."""
        return json.loads((self.state / "identity.json").read_text(encoding="utf-8"))

    def headers(self) -> dict[str, str]:
        return json.loads(self._engine("headers"))

    def _engine(self, command: str, *extra: str) -> str:
        args = [self._python, "-m", ENGINE_MODULE, command, "--state", str(self.state), *extra]
        env = {**os.environ, "COREADI_PATH": str(self.library)}
        env.pop("ADI_FROZEN_TIME", None)  # a frozen clock gives a constant OTP Apple refuses after ~1 min
        try:
            done = subprocess.run(
                args,
                capture_output=True,
                text=True,
                timeout=ENGINE_TIMEOUT_SECONDS,
                env=env,
                check=False,
                stdin=subprocess.DEVNULL,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except subprocess.TimeoutExpired as err:
            raise EmulatedMacError(f"The emulated Mac did not answer `{command}` in {ENGINE_TIMEOUT_SECONDS}s") from err
        if done.returncode != 0:
            detail = (done.stderr or done.stdout).strip().splitlines()
            raise EmulatedMacError(f"The emulated Mac failed `{command}`: {detail[-1] if detail else done.returncode}")
        return done.stdout


class EmulatedMacAnisetteProvider(BaseAnisetteProvider):
    """FindMy.py anisette provider backed by an EmulatedMac, asked once per OTP window.

    Header fetches block for a subprocess run (~0.1-1 s); in Home Assistant, fetch them in an
    executor or accept the short stall once per 30 s.
    """

    def __init__(self, mac: EmulatedMac) -> None:
        self._mac = mac
        self._machine = mac.identity
        super().__init__(serial=self._machine["serial"], identity=IDENTITY)
        self._window: int | None = None
        self._headers: dict[str, str] | None = None

    @property
    def uid(self) -> str:
        """Local user id the machine was provisioned with (FindMy.py sends base64 of it)."""
        return self._machine["local_user_uuid"]

    @property
    def devid(self) -> str:
        """The machine's provisioning UDID (`00006000-` + 16 hex). Not a UUID."""
        return self._machine["device_id"]

    @property
    def cloudkit_device_uuid(self) -> str:
        """A UUID for CloudKit, which can't parse a Mac device id: the machine's host UUID."""
        return str(uuid.UUID(self._machine["host_uuid"])).upper()

    def _fetch(self) -> dict[str, str]:
        window = int(time.time()) // WINDOW_SECONDS
        if self._headers is None or window != self._window:
            self._headers = self._mac.headers()
            self._window = window
        return self._headers

    async def get_headers(
        self,
        user_id: str,
        device_id: str,
        serial: str | None = None,
        with_client_info: bool = False,
    ) -> dict[str, str]:
        # Run the engine (a subprocess) in a thread, so an event loop is never blocked; the
        # property reads in the base implementation then hit this window's cache.
        await asyncio.to_thread(self._fetch)
        return await super().get_headers(user_id, device_id, serial, with_client_info)

    @property
    def otp(self) -> str:
        return self._fetch()["X-Apple-I-MD"]

    @property
    def machine(self) -> str:
        return self._fetch()["X-Apple-I-MD-M"]

    @property
    def router(self) -> str:
        return str(self._fetch()["X-Apple-I-MD-RINFO"])

    @property
    def timestamp(self) -> str:
        return self._fetch()["X-Apple-I-Client-Time"]

    def to_json(self, dst: str | Path | None = None, /) -> Any:
        # The state directory is the machine; this only records which kind of machine it was.
        return {"type": "aniEmulatedMac"}

    @classmethod
    def from_json(cls, val: Any) -> EmulatedMacAnisetteProvider:
        raise NotImplementedError("An emulated Mac is restored from its state directory, not from JSON")

    async def close(self) -> None:
        self._headers = None
        self._window = None


class CloudKitDeviceView:
    """The account, except `device_uuid` is a UUID CloudKit can parse (the Mac's host UUID)."""

    def __init__(self, account: Any, device_uuid: str) -> None:
        object.__setattr__(self, "_account", account)
        object.__setattr__(self, "_device_uuid", device_uuid)

    @property
    def device_uuid(self) -> str:
        return self._device_uuid

    def __getattr__(self, name: str) -> Any:
        return getattr(self._account, name)

    def __setattr__(self, name: str, value: Any) -> None:
        setattr(self._account, name, value)


def for_cloudkit(account: Any) -> Any:
    """`account`, or a view of it whose device id CloudKit can parse."""
    provider = getattr(account, "_anisette", None)
    if isinstance(provider, EmulatedMacAnisetteProvider):
        return CloudKitDeviceView(account, provider.cloudkit_device_uuid)
    return account
