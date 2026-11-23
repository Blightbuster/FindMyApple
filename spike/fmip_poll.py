"""
Spike: does the emulated-Mac session's mmeFMIPToken work against the FMIP (Find My iPhone) service?

Uses the saved `state/mac` session (no new login). Tries the legacy mobile endpoint with
Basic auth dsid:mmeFMIPToken, with and without the emulated Mac's anisette headers.
Prints HTTP status, redirect host and device counts only; never token values. Nothing is saved.

    uv run fmip_poll.py
"""

from __future__ import annotations

import asyncio
import base64
import json
import sys
from pathlib import Path

import ssl
import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "custom_components" / "findmyapple"))
from macanisette import EmulatedMac, EmulatedMacAnisetteProvider  # noqa: E402

STATE = Path(__file__).parent / "state" / "mac"
BODY = {
    "clientContext": {
        "fmly": True,
        "shouldLocate": True,
        "selectedDevice": "all",
        "deviceListVersion": 1,
        "appName": "FindMyiPhone",
        "appVersion": "6.0",
    }
}


def summarize(payload: dict) -> str:
    devs = payload.get("content") or []
    located = sum(1 for d in devs if d.get("location"))
    classes = sorted({str(d.get("deviceClass")) for d in devs})
    return f"{len(devs)} devices, {located} with location, classes={classes}"


async def attempt(name: str, host: str, dsid: str, token: str, extra: dict[str, str]) -> None:
    auth = base64.b64encode(f"{dsid}:{token}".encode()).decode()
    headers = {
        "Authorization": f"Basic {auth}",
        "Content-Type": "application/json; charset=utf-8",
        "X-Apple-Realm-Support": "1.0",
        "X-Apple-Find-API-Ver": "3.0",
        "X-Apple-AuthScheme": "Forever",
        "User-Agent": "FindMyiPhone/500 CFNetwork/758.0.2 Darwin/15.0.0",
        **extra,
    }
    url = f"https://{host}/fmipservice/device/{dsid}/initClient"
    async with httpx.AsyncClient(timeout=30, follow_redirects=False, verify=ssl.create_default_context(cafile=str(STATE.parent / "AppleRoot.pem"))) as http:
        resp = await http.post(url, headers=headers, json=BODY)
        redirect = resp.headers.get("X-Apple-MMe-Host")
        if resp.status_code == 330 and redirect:  # "use this partition host instead"
            resp = await http.post(
                f"https://{redirect}/fmipservice/device/{dsid}/initClient", headers=headers, json=BODY
            )
            name += f" (via {redirect})"
        detail = ""
        if resp.status_code == 200:
            try:
                detail = summarize(resp.json())
            except ValueError:
                detail = "200 but not JSON"
        print(f"{name:40} HTTP {resp.status_code}  {detail}")


async def main() -> None:
    mapping = json.loads((STATE / "account.json").read_text(encoding="utf-8"))
    dsid = mapping["login"]["data"]["dsid"]
    tokens = mapping["login"]["data"]["mobileme_data"]["tokens"]
    mac = EmulatedMac.open(STATE / "emulated-mac", provision=False)
    provider = EmulatedMacAnisetteProvider(mac)
    mac_headers = dict(await provider.get_headers(mapping["ids"]["uid"], mapping["ids"]["devid"]))
    print("anisette header shapes:", {k: len(v) for k, v in mac_headers.items() if k.startswith("X-Apple-I-MD")})
    for tok_name in ("mmeFMIPToken", "mmeFMIPAppToken"):
        for label, extra in (("plain", {}), ("+mac anisette", mac_headers)):
            await attempt(f"{tok_name} {label}", "p01-fmipmobile.icloud.com", dsid, tokens[tok_name], extra)


asyncio.run(main())
