"""
FindMyApple spike: which anisette source does Apple accept for each step of the chain?

Two phases, run per anisette mode:

  full   Apple ID login + 2FA  ->  iCloud keychain session  ->  CloudKit BeaconStore records
         ->  escrow recovery options  ->  unlock with a device passcode  ->  decrypt accessories
         ->  save their keys  ->  locate them on the Find My network.

  poll   Restore the saved session + saved accessory keys and ONLY locate them -- what the HA
         integration would do every 15 minutes. Can use a different anisette mode than the
         one that logged in (including `none`), to learn what day-to-day polling really needs.

Anisette modes:
  mac     Apple's macOS CoreADI under unicorn (machoadi, in a subprocess). The only mode Apple
          still issues the Find My token to (since 2026-10-12).
  local   FindMy.py's LocalAnisetteProvider: Apple's *Android* ADI libs run in unicorn.
  remote  A remote Anisette v3 server (SideStore-style; also Android ADI, on someone else's box).
  none    Empty X-Apple-I-MD headers. Only meaningful for `poll`.

Every step's outcome goes to state/results.jsonl so runs can be compared afterwards.
Secrets (session, tag keys) go to state/<mode>/ -- gitignored. Passwords and the device
passcode are read with getpass and never written anywhere.

    uv run spike.py full --anisette local
    uv run spike.py full --anisette remote --anisette-url https://ani.sidestore.io
    uv run spike.py poll --anisette none  --from local
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import json
import logging
import re
import sys
import urllib.request
import time
import traceback
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, AsyncIterator

from findmy import (
    AsyncAppleAccount,
    BaseAnisetteProvider,
    DeviceIdentity,
    FindMyAccessory,
    LocalAnisetteProvider,
    LoginState,
    MobileMeDelegateError,
    RemoteAnisetteProvider,
    SmsSecondFactorMethod,
    TrustedDeviceSecondFactorMethod,
)
from findmy.icloud import AsyncFindMyClient

# The integration's own emulated-Mac provider (and CoreADI fetcher), shared with the spike.
# Loaded standalone (not as the HA package, whose __init__ needs Home Assistant).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "custom_components" / "findmyapple"))
from macanisette import EmulatedMac, EmulatedMacAnisetteProvider, for_cloudkit  # noqa: E402

HERE = Path(__file__).parent
STATE = HERE / "state"
RESULTS = STATE / "results.jsonl"
DEFAULT_REMOTE = "https://ani.sidestore.io"
MAX_FULL_HISTORY_SPAN = 5000  # key indices; above this a locate scans the tag's whole history

log = logging.getLogger("spike")


# --------------------------------------------------------------------------- anisette


class NullAnisetteProvider(BaseAnisetteProvider):
    """Sends the anisette headers with empty OTP / machine values. Poll-mode experiment only."""

    @property
    def otp(self) -> str:
        return ""

    @property
    def machine(self) -> str:
        return ""

    async def close(self) -> None:
        return None

    def to_json(self, dst: Any = None, /) -> dict:  # noqa: ARG002
        msg = "the null provider is never persisted"
        raise RuntimeError(msg)

    @classmethod
    def from_json(cls, val: Any, /) -> NullAnisetteProvider:  # noqa: ARG003
        return cls()


# CFNetwork / Darwin versions belonging to a macOS release. The identity's parts must describe one
# real release (see findmy.reports.anisette.DeviceIdentity), so only known releases are used.
MACOS_RELEASES = {
    ("13.1", "22C65"): ("1404.0.5", "22.2.0"),
    ("13.4.1", "22F8"): ("1408.0.4", "22.5.0"),
}


def server_identity(url: str) -> DeviceIdentity | None:
    """
    The device an Anisette v3 server's data was provisioned as, from its /v3/client_info.

    Apple sees both the anisette headers (from the server) and the client info the library sends.
    If they name different machines, Grand Slam can refuse at SRP init (ec=-80043) before the
    password is even checked -- so make the library claim the server's machine.
    """
    try:
        with urllib.request.urlopen(url.rstrip("/") + "/v3/client_info", timeout=20) as r:  # noqa: S310
            info = json.load(r)["client_info"]
    except Exception as e:  # noqa: BLE001
        print(f"  could not read server client_info ({e}); using the library identity")
        return None
    m = re.match(r"<([^>]+)> <([^;>]+);([^;>]+);([^>]+)>", info)
    if not m:
        print(f"  unrecognised client_info {info!r}; using the library identity")
        return None
    model, os_name, os_version, os_build = m.groups()
    release = MACOS_RELEASES.get((os_version, os_build))
    if release is None:
        print(f"  server claims unknown release {os_version} ({os_build}); using the library identity")
        return None
    cfnetwork, darwin = release
    return DeviceIdentity(model=model, os_name=os_name, os_version=os_version, os_build=os_build,
                          cfnetwork=cfnetwork, darwin=darwin)


def make_provider(mode: str, url: str | None, state_dir: Path, *, match_server: bool = True) -> BaseAnisetteProvider:
    if mode == "mac":
        # Provisions the machine with Apple on first use (no Apple ID involved), then reuses it.
        return EmulatedMacAnisetteProvider(EmulatedMac.open(state_dir / "emulated-mac"))
    if mode == "local":
        return LocalAnisetteProvider(libs_path=STATE / "ani_libs.bin")
    if mode == "remote":
        url = url or DEFAULT_REMOTE
        identity = server_identity(url) if match_server else None
        if identity is not None:
            print(f"  claiming the server's identity: {identity.platform}")
            return RemoteAnisetteProvider(url, timeout=60, identity=identity)
        return RemoteAnisetteProvider(url, timeout=60)
    if mode == "none":
        return NullAnisetteProvider()
    msg = f"unknown anisette mode {mode!r}"
    raise ValueError(msg)


# --------------------------------------------------------------------------- recording


class Recorder:
    """Appends one JSON line per step; stops the run on the first failure."""

    def __init__(self, phase: str, mode: str, login_mode: str) -> None:
        self.run = {
            "run_at": datetime.now(tz=timezone.utc).isoformat(timespec="seconds"),
            "phase": phase,
            "anisette": mode,
            "login_anisette": login_mode,
        }
        self.steps: list[dict] = []
        STATE.mkdir(exist_ok=True)

    @asynccontextmanager
    async def step(self, name: str) -> AsyncIterator[dict]:
        info: dict = {}
        print(f"\n=== {name} ===", flush=True)
        started = time.monotonic()
        try:
            yield info
        except BaseException as e:
            self._write(name, ok=False, started=started, info=info, error=e)
            raise
        self._write(name, ok=True, started=started, info=info)

    def _write(self, name: str, *, ok: bool, started: float, info: dict, error: BaseException | None = None) -> None:
        entry = {**self.run, "step": name, "ok": ok, "seconds": round(time.monotonic() - started, 1), **info}
        if error is not None:
            entry["error"] = f"{type(error).__name__}: {error}"[:500]
            entry["trace_tail"] = traceback.format_exception(error)[-3:]
        self.steps.append(entry)
        with RESULTS.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, default=str) + "\n")
        print(f"--> {'OK' if ok else 'FAILED'}  {name}  {json.dumps(info, default=str)[:300]}")
        if error is not None:
            print(f"    {entry['error']}")

    def summary(self) -> None:
        print("\n" + "-" * 60)
        print(f"{self.run['phase']} / anisette={self.run['anisette']} (login: {self.run['login_anisette']})")
        for s in self.steps:
            print(f"  {'OK  ' if s['ok'] else 'FAIL'} {s['step']:<28} {s['seconds']:>6}s")
        print(f"results appended to {RESULTS}")


# --------------------------------------------------------------------------- login


def restore_account(mode: str, account_file: Path, mode_dir: Path) -> AsyncAppleAccount:
    """A saved session, on the machine that signed it in."""
    if mode == "mac":
        mapping = json.loads(account_file.read_text(encoding="utf-8"))
        mac = EmulatedMac.open(mode_dir / "emulated-mac", provision=False)  # never a new machine here
        return AsyncAppleAccount(EmulatedMacAnisetteProvider(mac), state_info=mapping)
    return AsyncAppleAccount.from_json(account_file, anisette_libs_path=STATE / "ani_libs.bin")


async def interactive_login(account: AsyncAppleAccount) -> dict:
    email = input("Apple ID email > ").strip()
    password = getpass.getpass("Apple ID password (not echoed) > ")
    print(f"  (password received: {len(password)} characters)")
    try:
        state = await account.login(email, password)
    finally:
        del password

    info: dict = {"state_after_password": state.name}
    if state == LoginState.REQUIRE_2FA:
        methods = await account.get_2fa_methods()
        for i, m in enumerate(methods):
            if isinstance(m, TrustedDeviceSecondFactorMethod):
                print(f"  {i} - trusted device")
            elif isinstance(m, SmsSecondFactorMethod):
                print(f"  {i} - SMS {m.phone_number}")
        method = methods[int(input("2FA method > "))]
        info["2fa_method"] = type(method).__name__
        await method.request()
        state = await method.submit(input("2FA code > ").strip())
    info["state"] = state.name
    if state != LoginState.LOGGED_IN:
        msg = f"login ended in state {state.name}"
        raise RuntimeError(msg)
    return info


def token_names(account: AsyncAppleAccount) -> list[str]:
    """Names (never values) of the service tokens the MobileMe delegate handed out."""
    try:
        return sorted(account.service_tokens.keys())
    except Exception:  # noqa: BLE001
        return []


# --------------------------------------------------------------------------- locate


async def locate_all(account: AsyncAppleAccount, accessories: list[FindMyAccessory], rec: Recorder) -> None:
    now = datetime.now(tz=timezone.utc)
    for acc in accessories:
        name = acc.name or acc.identifier or "<unnamed>"
        span = acc.get_max_index(now) - acc.get_min_index(now - timedelta(days=7))
        if span > MAX_FULL_HISTORY_SPAN:
            print(f"  skipping {name}: no key alignment, would scan {span:,} indices")
            continue
        async with rec.step(f"locate:{name}") as info:
            report = await account.fetch_location(acc)
            if report is None:
                info["result"] = "no report in the last 7 days"
            else:
                age = now - report.timestamp
                info["result"] = "located"
                info["age_minutes"] = round(age.total_seconds() / 60)
                info["accuracy_m"] = report.horizontal_accuracy
                # Coordinates are deliberately rounded: enough to sanity-check, not to dox.
                info["approx"] = (round(report.latitude, 2), round(report.longitude, 2))


# --------------------------------------------------------------------------- phases


async def phase_full(args: argparse.Namespace) -> int:
    mode_dir = STATE / args.anisette
    mode_dir.mkdir(parents=True, exist_ok=True)
    account_file = mode_dir / "account.json"
    rec = Recorder("full", args.anisette, args.anisette)

    try:
        async with rec.step("anisette_headers") as info:
            if account_file.exists() and not args.relogin:
                account = restore_account(args.anisette, account_file, mode_dir)
                info["restored_session"] = True
            else:
                provider = make_provider(args.anisette, args.anisette_url, mode_dir,
                                         match_server=not args.library_identity)
                if isinstance(provider, EmulatedMacAnisetteProvider):
                    # The machine's own ids: provisioning registered them with Apple.
                    account = AsyncAppleAccount(provider, uid=provider.uid, devid=provider.devid)
                    info["serial"] = provider.serial
                else:
                    account = AsyncAppleAccount(provider)
                info["restored_session"] = False
            info["identity"] = account._anisette.identity.platform  # noqa: SLF001 -- spike reporting
            headers = await account.get_anisette_headers()
            info["md_present"] = bool(headers.get("X-Apple-I-MD"))

        try:
            if account.login_state != LoginState.LOGGED_IN:
                async with rec.step("login_srp_2fa") as info:
                    try:
                        info.update(await interactive_login(account))
                    except MobileMeDelegateError:
                        info["hint"] = "iCloud terms not accepted? see examples/accept_icloud_terms.py"
                        raise
                account.to_json(account_file)

            async with rec.step("mobileme_tokens") as info:
                info["token_names"] = token_names(account)
                if "searchPartyToken" not in info["token_names"]:
                    msg = "no searchPartyToken: Apple did not issue the Find My token"
                    raise RuntimeError(msg)

            async with rec.step("keychain_session_open"):
                client = await AsyncFindMyClient.open(for_cloudkit(account))

            async with client:
                async with rec.step("cloudkit_container_zones") as info:
                    await client.store.client.open_container()
                    zones = await client.store.client.zone_retrieve()
                    info["zones"] = [z.target_zone.zone_identifier.value.name for z in zones]

                async with rec.step("beaconstore_records") as info:
                    records = await client.records()
                    by_type: dict[str, int] = {}
                    for r in records:
                        by_type[r.record_type] = by_type.get(r.record_type, 0) + 1
                    info["by_type"] = by_type

                async with rec.step("recovery_options") as info:
                    options = await client.recovery_options()
                    info["recoverable"] = len(options.recoverable)
                    for i, r in enumerate(options.recoverable):
                        print(f"  [{i}] {r.describe()}")
                    if not options.recoverable:
                        msg = "no recoverable escrow record on this account"
                        raise RuntimeError(msg)

                async with rec.step("keychain_unlock") as info:
                    chosen = None
                    while chosen is None:
                        answer = input("\nNumber or serial of the device whose passcode you'll enter > ").strip()
                        if answer.isdigit() and int(answer) < len(options.recoverable):
                            chosen = options.recoverable[int(answer)]
                        else:
                            chosen = next((r for r in options.recoverable
                                           if (r.serial or "").casefold() == answer.casefold()), None)
                        if chosen is None:
                            print("  not in the list; try again")
                    info["chosen"] = chosen.describe().split(",")[1].strip() if "," in chosen.describe() else "?"
                    passcode = getpass.getpass("That device's screen passcode (not echoed) > ")
                    print(f"  (passcode received: {len(passcode)} characters)")
                    try:
                        keys = await client.unlock(chosen, passcode)
                    finally:
                        del passcode
                    info["keychain_keys"] = len(keys)

                async with rec.step("decrypt_accessories") as info:
                    accessories = await client.accessories()
                    info["count"] = len(accessories)
                    info["models"] = sorted({str(a.model) for a in accessories})

            async with rec.step("save_keys") as info:
                acc_dir = mode_dir / "accessories"
                acc_dir.mkdir(exist_ok=True)
                for a in accessories:
                    a.to_json(acc_dir / f"{a.identifier}.json")
                info["saved"] = len(accessories)
                account.to_json(account_file)

            await locate_all(account, accessories, rec)
            account.to_json(account_file)
        finally:
            await account.close()
    except Exception:  # noqa: BLE001 -- already recorded; summary is the point
        rec.summary()
        return 1
    rec.summary()
    return 0


async def phase_probe(args: argparse.Namespace) -> int:
    """No Apple ID needed: can this anisette source produce headers at all (ADI provisioning)?"""
    rec = Recorder("probe", args.anisette, "-")
    provider = make_provider(args.anisette, args.anisette_url, STATE, match_server=not args.library_identity)
    account = AsyncAppleAccount(provider)
    try:
        async with rec.step("anisette_headers") as info:
            info["identity"] = provider.identity.platform
            headers = await account.get_anisette_headers()
            info["md_len"] = len(headers.get("X-Apple-I-MD", ""))
            info["machine_len"] = len(headers.get("X-Apple-I-MD-M", ""))
            info["rinfo"] = headers.get("X-Apple-I-MD-RINFO")
    except Exception:  # noqa: BLE001
        rec.summary()
        return 1
    finally:
        await account.close()
    rec.summary()
    return 0


async def phase_poll(args: argparse.Namespace) -> int:
    src_dir = STATE / args.from_mode
    account_file = src_dir / "account.json"
    acc_dir = src_dir / "accessories"
    if not account_file.exists() or not acc_dir.exists():
        print(f"Run `full --anisette {args.from_mode}` first; nothing saved in {src_dir}.")
        return 2

    rec = Recorder("poll", args.anisette, args.from_mode)
    try:
        async with rec.step("restore_session") as info:
            if args.anisette == args.from_mode:
                # Same provider as the login, including its saved ADI provisioning.
                account = restore_account(args.anisette, account_file, src_dir)
            else:
                mapping = json.loads(account_file.read_text(encoding="utf-8"))
                provider = make_provider(args.anisette, args.anisette_url, src_dir,
                                         match_server=not args.library_identity)
                account = AsyncAppleAccount(provider, state_info=mapping)
            accessories = [FindMyAccessory.from_json(p) for p in sorted(acc_dir.glob("*.json"))]
            info["accessories"] = len(accessories)
            info["login_state"] = account.login_state.name
        try:
            await locate_all(account, accessories, rec)
            # Persist refreshed tokens/alignment only when the provider is the one that logged in.
            if args.anisette == args.from_mode:
                account.to_json(account_file)
                for a in accessories:
                    a.to_json(acc_dir / f"{a.identifier}.json")
        finally:
            await account.close()
    except Exception:  # noqa: BLE001
        rec.summary()
        return 1
    rec.summary()
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("phase", choices=["probe", "full", "poll"])
    p.add_argument("--anisette", choices=["mac", "local", "remote", "none"], required=True)
    p.add_argument("--anisette-url", help=f"remote Anisette v3 server (default {DEFAULT_REMOTE})")
    p.add_argument("--from", dest="from_mode", choices=["mac", "local", "remote"],
                   help="poll: whose saved session to use")
    p.add_argument("--relogin", action="store_true", help="full: ignore a saved session and log in again")
    p.add_argument("--library-identity", action="store_true",
                   help="remote: keep FindMy.py's own MacBookPro18,3 identity instead of the server's")
    p.add_argument("-v", "--verbose", action="store_true", help="debug logging for findmy.*")
    args = p.parse_args()

    if args.phase in ("full", "probe") and args.anisette == "none":
        p.error("`none` cannot log in; use it with `poll --from local|remote`")
    if args.phase == "poll" and not args.from_mode:
        args.from_mode = args.anisette if args.anisette != "none" else p.error("poll --anisette none needs --from")

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(levelname)-7s %(name)s: %(message)s")
    run = {"probe": phase_probe, "full": phase_full, "poll": phase_poll}[args.phase]
    return asyncio.run(run(args))


if __name__ == "__main__":
    sys.exit(main())
