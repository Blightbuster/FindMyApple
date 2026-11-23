"""
Control experiment for `poll`: does findmyservice/v2/fetch check anisette, and is a 200 real?

Uses the saved `state/mac` session and one key-aligned accessory. Three arms, same request:
  mac      headers from the emulated Mac that signed in
  none     empty X-Apple-I-MD / X-Apple-I-MD-M
  badtoken empty anisette AND a corrupted searchPartyToken (negative control: must fail)

Prints the HTTP status and the anisette header *shapes* (lengths, never values). Nothing is saved.

    uv run control_poll.py
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

from findmy import AsyncAppleAccount, FindMyAccessory

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "custom_components" / "findmyapple"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from macanisette import EmulatedMac, EmulatedMacAnisetteProvider  # noqa: E402
from spike import NullAnisetteProvider  # noqa: E402

STATE = Path(__file__).parent / "state" / "mac"


def pick_aligned() -> FindMyAccessory:
    for path in sorted((STATE / "accessories").glob("*.json")):
        acc = FindMyAccessory.from_json(path)
        data = json.loads(path.read_text(encoding="utf-8"))
        if data.get("alignment_index") or data.get("alignment_date") or "alignment" in json.dumps(data):
            return acc
    raise SystemExit("no accessory with key alignment saved")


async def arm(name: str, provider, mapping: dict, accessory: FindMyAccessory, *, corrupt: bool = False) -> None:
    mapping = json.loads(json.dumps(mapping))
    account = AsyncAppleAccount(provider, state_info=mapping)
    try:
        if corrupt:
            tokens = account.service_tokens
            good = tokens["searchPartyToken"]
            tokens["searchPartyToken"] = good[:-6] + ("A" if good[-6] != "A" else "B") + good[-5:]
        headers = await account.get_anisette_headers()
        shape = {k: len(v) for k, v in headers.items() if k.startswith("X-Apple-I-MD")}
        status = "?"
        orig_post = account._http.post  # noqa: SLF001 - spike instrumentation

        async def spy(url, *a, **kw):
            nonlocal status
            resp = await orig_post(url, *a, **kw)
            if "findmyservice" in url:
                status = resp.status_code
            return resp

        account._http.post = spy  # noqa: SLF001
        try:
            report = await account.fetch_location(accessory)
            result = "located" if report is not None else "no report"
        except Exception as e:  # noqa: BLE001
            result = f"{type(e).__name__}: {str(e)[:120]}"
        print(f"{name:9} HTTP {status}  {result}  anisette {shape}")
    finally:
        await account.close()


async def main() -> None:
    mapping = json.loads((STATE / "account.json").read_text(encoding="utf-8"))
    accessory = pick_aligned()
    print(f"accessory: {accessory.name}")
    mac = EmulatedMac.open(STATE / "emulated-mac", provision=False)
    await arm("mac", EmulatedMacAnisetteProvider(mac), mapping, accessory)
    await arm("none", NullAnisetteProvider(), mapping, accessory)
    await arm("badtoken", NullAnisetteProvider(), mapping, accessory, corrupt=True)


if __name__ == "__main__":
    asyncio.run(main())
