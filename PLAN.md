# FindMyApple – Home Assistant integration plan

A HACS-installable custom integration that tracks Apple devices **and** Find My tags in Home
Assistant. Every tracked item becomes a `device_tracker` (shown on the HA map) plus companion
sensors.

It combines two backends, because neither one covers everything:

| Backend | Library | Covers | Doesn't cover |
|---|---|---|---|
| **iCloud "Find My iPhone"** (same approach as iCloud3) | `pyicloud` (maintained fork by timlaing) | iPhone, iPad, Mac, Watch, AirPods, Family Sharing devices; battery level | AirTags / "Items" |
| **Find My network** | [FindMy.py](https://github.com/malmeloo/FindMy.py) (`findmy`) | AirTags, "Works with Find My" tags, OpenHaystack/DIY tags | Listing iCloud devices |

---

## 1. Background research

### 1a. How iCloud3 tracks iPhones/iPads
- It uses the **iCloud web API** that icloud.com/find uses, through its own customised copy of
  `pyicloud`. It isn't the Find My network.
- **Login:**
  - SRP password sign-in at `idmsa.apple.com/appleauth/auth/signin/init|complete`.
  - Then `setup.icloud.com/setup/ws/1/accountLogin`.
  - Two-factor codes come from a trusted-device push or SMS. Hardware security keys are also supported.
- **Session:** the session token and trust token are saved and tried first. The password login runs
  only when Apple refuses them. The trust lasts about 90 days, and the session is refreshed at least hourly.
- **Locate:**
  - `POST {findme_root}/fmipservice/client/web/refreshClient` with `shouldLocate: true` and
    `fmly: true` (also returns Family Sharing devices).
  - `findme_root` comes from the `webservices.findme.url` field in the login response.
- **Fields returned:**
  - `latitude`, `longitude`, `horizontalAccuracy`, `timeStamp` (ms), `positionType`, `isOld`,
    `isInaccurate`, `locationFinished`
  - `batteryLevel`, `batteryStatus`, `lowPowerMode`
  - `deviceStatus` (200 online / 201 offline / 203 pending), `deviceClass`, `name`, `id`,
    `prsId` (identifies the family member)
- **Commands:** `playSound`, `sendMessage`, `lostDevice`.
- **China:** uses `icloud.com.cn` and converts GCJ-02 coordinates to WGS-84.
- **Throttling lessons:**
  - Space out password logins: at least 30 s apart, and at least 15 min when Apple asks for re-auth.
    Apple returns `-900007` or 503 otherwise.
  - Back off on 5xx errors.
  - Adapt polling to distance from zones: 15 s close to a zone, up to 1 h when far away.
    Each locate makes the phone fetch its own GPS position, so frequent polling can drain the battery.
- **Second source:** iCloud3 also uses the HA Companion App and keeps whichever location is newer.
- **Licence:** iCloud3 is MIT. The licence of its vendored pyicloud parts wasn't checked, so we use the
  `pyicloud` package instead of copying iCloud3's code.

### 1b. FindMy.py
| Topic | Finding | Consequence |
|---|---|---|
| Version / Python | 0.10.x, Python `>=3.10,<3.15` | Fine for HA (3.13). |
| API | `AsyncAppleAccount` (aiohttp) | Runs natively in HA's event loop. |
| Login | `login()` → `LoginState.REQUIRE_2FA` → `get_2fa_methods()` → `request()` / `submit(code)` | Needs its own two-factor flow. This is a **separate login** from the iCloud web one. |
| Session | `to_json()` / `from_json()`. Every fresh login adds a "trusted device" | Save the session and log in again only when it stops working. |
| Anisette | `LocalAnisetteProvider(libs_path=...)` or `RemoteAnisetteProvider(url)` | Local by default, with a remote server URL as fallback. |
| Locatable | Key-based only: `FindMyAccessory` (AirTags etc.), `KeyPair` (DIY) | Tags are imported from key files. |
| Getting keys | Upstream: `python -m findmy decrypt` on a Mac only. **Download from iCloud: see 1c** | Primary route is automatic (1c). Uploading a key file stays as a fallback. |
| Fetch | `fetch_location([keys])` → `{key: LocationReport \| None}` (batched) | One request per poll covers all tags. |
| Report | `timestamp`, `latitude`, `longitude`, `horizontal_accuracy`, `confidence`, `status` byte | Battery can only be guessed from the AirTag status byte. |

### 1c. Downloading tag keys from iCloud (no Mac), like OpenTagViewer
- Apple stores tag keys (BeaconStore records) in **CloudKit**, encrypted with keys from the user's
  **iCloud Keychain**.
- OpenTagViewer's exporter (`python/exporter/icloud.py`, MIT) gets them in four steps:
  1. Apple ID login + two-factor code.
  2. List the account's devices with `recovery_options()`.
  3. The user enters the **screen passcode of one of their Apple devices** (iPhone PIN or Mac login
     password). It's used once and not stored. This unlocks the keychain escrow (up to 3 tries,
     because Apple locks the account after failed attempts).
  4. Download the BeaconStore records from CloudKit, decrypt them, and turn them into accessory
     plists: master key, skn, sks, key alignment and name.
- **Where the code lives:** only in the fork `parawanderer/FindMy.py` (pinned commit `37d5ca0b`),
  marked "unreleased". Modules:
  - `findmy.icloud.AsyncFindMyClient` (`accessories()`, `recovery_options()`, `unlock(record, passcode)`)
  - `findmy.keychain.session.AsyncKeychainSession`
  - `findmy.cloudkit.client.AsyncCloudKitClient`
  - `findmy.cloudkit.beacons.*` (`decrypt_records`, `to_owned_beacon_plist`, `to_key_alignment_plist`, …)
  
  Upstream FindMy.py 0.10.2 doesn't have this yet.
- **Login:** the exporter signs in as an emulated Mac. Apple stopped serving the Android anisette
  data, so it runs the GPL-2.0 `machoadi` engine in a **separate process** (never imported, so it
  doesn't affect our licence).
- **Effects on the Apple account:**
  - **One "MacBook Pro" entry** appears in the account's device list. It isn't trusted, can't receive
    codes, and can be removed at any time without breaking imported tags.
  - **No escrow record or trust-circle peer is created** on the read path.
- **Consequence for us:** tags become automatic. Log in, pick a device, enter its passcode once, and
  every tag on the account is imported.

### 1d. Our own FindMy.py fork, and where Mac emulation is needed
**Key point:** Mac emulation is only needed for **anisette**, the device-attestation headers
(`X-Apple-I-MD*`). The keychain, CloudKit and decryption steps are plain protocol code and need no
emulation. So the work splits into two independent parts. Part A is solved. Part B was settled by the
spike: Mac emulation can't be avoided for login (and probably for polling).

> **Spike status (2026-11-23):** a spike built on the parawanderer fork lives in `spike/`. Its
> dependencies install cleanly on the dev machine (incl. `unicorn`, `protobuf`; not yet checked on
> HA OS); the fork's `icloud`/`cloudkit`/`keychain` modules import. The non-Mac anisette options all
> failed: see part B.

**A. Protocol part (keychain + CloudKit + PCS decryption): solved, pure code.**
- Two existing implementations:
  - `rustpush` (Rust; `cloudkit.rs`, `keychain.rs`, `findmy.rs`), used by `stek29/export-findmy`.
    Its author describes the chain as GSA login → Octagon/Cuttlefish trust → PCS keys → CloudKit.
  - The parawanderer FindMy.py fork: a **Python** port (`findmy.keychain`, `findmy.cloudkit`, `findmy.icloud`).
- export-findmy's flow, as a reference:
  1. SRP login + two-factor.
  2. MobileMe delegate via `setup.icloud.com/setup/iosbuddy/loginDelegates`.
  3. Escrow proxy (`pXX-escrowproxy.icloud.com`): `get_viable_bottles()`, then the device passcode,
     then `join_clique_from_escrow`.
  4. CloudKit `BeaconStore` zone in the Find My container → `FetchRecordChangesOperation`.
  5. PCS-decrypt `MasterBeaconRecord`, `BeaconNamingRecord` and `KeyAlignmentRecord`.
- It sends **empty Mac validation data** and a made-up device profile (serial, model, UDID), so no
  real or emulated Mac hardware is needed for this part. rustpush's closed parts (ClearADI,
  OpenAbsinthe) aren't used on this path.
- Difference: export-findmy **joins the trust circle** and creates its own escrow bottle, which it
  deletes afterwards. The parawanderer fork's read path adds no peer, which is gentler on the account,
  so we should prefer it.
- **Plan:** a local fork of upstream FindMy.py (vendored as `findmyapple/_findmy/` or published as
  `findmy-ha`). Take the Python `keychain`/`cloudkit`/`icloud` modules from the parawanderer fork (MIT)
  and check them against rustpush's behaviour. Make anisette **pluggable**: the default provider is
  `EmulatedMacAnisetteProvider` (part B), and a "remote anisette URL" provider points at the
  companion add-on. Neither imports GPL code.

**B. Anisette: settled by the spike (2026-11-23). Mac emulation is required for login.**

Every option except a real Apple device runs Apple's ADI code somewhere. Spike results:

| Option | Emulation? | Result |
|---|---|---|
| Remote Anisette v3 server (SideStore-style, Android ADI) | Not on our side | **Dead.** `ani.npeg.us` and `anisette.wedotstud.io` produce valid headers (`ani.sidestore.io` is down), but logins fail before reaching the Find My token. |
| Local Android ADI (`anisette` package, FindMy.py's default) | Yes (unicorn, ARM Android libs) | **Dead.** Provisioning returns no `ptm`/`tk` → `KeyError: 'ptm'`. |
| rustpush iPhone-style login (`export-findmy`) | No | **Failed** before the password step. |
| Mac CoreADI 4.19 under emulation (`machoadi`) | Yes (unicorn, x86_64 Mac lib) | **Chosen.** Proven end to end by a third party (see below). **Ours, 2026-11-23: full chain works with a real account: sign-in with 2FA, `searchPartyToken`, keychain unlock, 11 accessories decrypted, 5 tags located.** |
| Real Mac/iPhone as anisette source | No | Defeats "no Apple device needed". |

**Why:** OpenTagViewer's engineering notes explain the failures. Since 2026-10-12 Apple only issues
`searchPartyToken` to a login whose anisette was minted by a Mac or an iPhone. An Android-minted login
gets a different token, and every report fetch then returns 401. Since ~2026-10-20 Apple's edge returns
503 for any login whose client info names `Android`.

**Chosen approach: `machoadi`** from `parawanderer/os-findmy-e@595d398` (`tools/machoadi`,
GPL-2.0-only, pip-installable, `unicorn` + FindMy.py), driven by our own
`custom_components/findmyapple/macanisette.py` (`EmulatedMac`, `EmulatedMacAnisetteProvider`,
`for_cloudkit`), with Apple's library fetched by `coreadi.py`. Both are written and the spike uses them.
- `machoadi` runs as a **subprocess** (`python -m machoadi.mac_anisette provision|info|headers
  --state DIR`); it's never imported, so GPL code stays at arm's length.
- **Not `macOS-ADI-emulator`** (the plan's earlier pick, same repo, sacca97 upstream): it emulates
  the **macOS 26 `adid` daemon**, whose binary has to be "separately obtained" from a macOS 26
  system and can't be fetched from Apple with a small range request. Its default client info also
  spells the OS `macOS`, which Apple's edge refuses at sign-in since 2026-11-20. The parawanderer pin
  of `machoadi` spells it `Mac OS X` and takes a `--serial`.
- CloudKit parses the account's device id as a UUID, and a Mac's (`00006000-…`) isn't one, so the
  keychain/CloudKit client gets a view of the account whose `device_uuid` is the Mac's host UUID
  (`for_cloudkit`).
- Serial: `0FMAHA` + 6 random characters, drawn once per machine, so users can recognise the
  "MacBook Pro" entry in their device list.
- Apple's real x86_64 CoreADI library is fetched **from Apple at runtime** (one 3.5 MB HTTP range
  request into the iTunes-12.8.3-for-Mac installer, checked against two pinned SHA-256 hashes). We
  never ship it, and the README must say the integration downloads it.
- One Unicorn process reliably crashes after ~8 OTP requests, so the subprocess is restarted once
  per 30-second OTP window by design.
- Identity: `MacBookPro13,2`, OS spelled **`Mac OS X`** (never `macOS`, which Apple's edge has
  returned 503 for since 2026-11-20), version `13.1`, build `22C65`. Client info must name the daemon
  `com.apple.akd`, never `com.apple.dt.Xcode` (503 otherwise).

**Evidence:** `os-findmy-e`'s `EVIDENCE.md` (2026-10-16) shows the whole chain against a real account:
emulated-Mac login with 2FA → `searchPartyToken` → `AsyncKeychainSession.recovery_options()` → escrow
recovery with a device passcode → `AsyncBeaconStore` decrypt → 2 accessories recovered (an iPhone + a
third-party tag) → the tag's `fetch_location` returned 8 real reports (newest within 30 min, accuracy
52–102 m). That's the same FindMy.py-family API we use. `tools/machoadi/tag_keys.py` there is a
documented reference implementation of our "1c" key-download flow.

**Operational rules this imposes (from that repo, to re-verify ourselves):**
- The provisioned machine needs a **real, non-frozen system clock**.
- Apple provisions a device ID only **once**; a second attempt returns HTTP 433. The provisioning
  state (machine ID, ADI data) must be **saved in `.storage` and reused** for every reauth and HA
  restart. Otherwise every reauth or reinstall adds another fake "MacBook Pro" to the account.
- An `hsc 434` reply means Apple wants a resync; handle it the way the repo documents.
- HTTP 429 from `gsa.apple.com` is **not** a rate limit (EVIDENCE §4 corrects this): it's Apple
  refusing a second request on a reused connection. Use a fresh connection per Grand Slam request
  rather than backing off. (The earlier "back off 45 min" rule here was wrong.)
- Never leave a stale `adi.pb` in the store: it makes the library report "not provisioned" and Apple
  answers `ec -80009` ("MID is invalid").
- Observed lifetimes: headers accepted at ~4 min old, a token worked at 16 min, login completed at
  29 min. Still open: whether the token and machine survive hours or days.

**Open question #1, answered by our own test (2026-11-23): report fetches don't check anisette,
sign-in does.** With a real account, `findmyservice/v2/fetch` returned 200 with emulated-Mac headers
*and* with empty anisette headers, and 401 with a corrupted token (negative control,
`spike/control_poll.py`). os-findmy-e's "token bound to its machine" result (EVIDENCE §6) was measured
on `acsnservice/fetch`, which FindMy.py no longer uses. But every **Grand Slam** call needs the Mac:
after the 401, FindMy.py's automatic re-auth with empty headers was refused (`ec -80009`, "MID is
invalid"). Token renewal (PET refresh, which the keychain step also triggers) is a Grand Slam call.

**Decision:** keep sending the emulated Mac's headers on every request anyway. It costs one engine
run per 30-second window (~0.1 s on amd64; polls are minutes apart), it's what a real client does,
and it keeps working if Apple starts checking. Polling no longer *depends* on the emulator, so an
emulator failure degrades to "polling continues, re-auth fails" instead of an immediate outage.
Still unknown: how long the token lasts before a Grand Slam renewal is forced.

**Where the emulator lives: inside the integration (no add-on needed).** Spike 0b ran the full
requirement set in the HA 2026.11.2 container on amd64 and aarch64 (musl, Python 3.14, no
compiler): `unicorn` installs from a wheel, nothing conflicts, and CoreADI boots under Unicorn. The
add-on stays a fallback only if a real Pi turns out too slow or memory-starved (QEMU showed 84 MB
RSS per engine run; to measure on hardware).

Still to confirm with a real login (`spike.py full --anisette mac`, then `poll`): that
`poll --anisette none --from mac` gets 401, which would confirm the binding for our own setup.

Note: the device passcode of a real Apple device is still needed, because the escrow bottle only
exists if a device created it. This isn't a manual export, just one password field in the setup dialog.

---

## 2. Architecture

```
custom_components/findmyapple/
├── __init__.py            # setup/unload, create backends + coordinators
├── manifest.json          # requirements: findmy, pyicloud; iot_class cloud_polling
├── const.py
├── models.py              # TrackedLocation dataclass (normalised across both backends)
├── backends/
│   ├── base.py            # LocationBackend protocol: authenticate(), fetch() -> {id: TrackedLocation}
│   ├── icloud.py          # pyicloud wrapper (sync → hass.async_add_executor_job)
│   └── findmy_network.py  # FindMy.py wrapper (async)
├── anisette.py            # EmulatedMacAnisetteProvider (subprocess) + remote-URL provider (add-on)
├── storage.py             # HA Store: sessions, trust tokens, emulated-Mac provisioning state,
│                          #   accessory alignment, last locations
├── coordinator.py         # one DataUpdateCoordinator per backend
├── config_flow.py         # account login + 2FA per backend, reauth, options, tag subentries
├── entity.py              # base CoordinatorEntity + DeviceInfo
├── device_tracker.py      # TrackerEntity (GPS) → map
├── sensor.py              # battery, last seen, distance from home, accuracy, status
├── button.py              # refresh, play sound (iDevices)
├── diagnostics.py         # redacted dump
├── strings.json, translations/{en,de}.json
hacs.json
tests/
```

### Normalised model
`TrackedLocation`:
- `latitude`, `longitude`, `accuracy`, `timestamp`, `source` (`icloud` | `findmy_network`)
- `battery_level`, `battery_charging`, `online`, `position_type`, `confidence`, `raw` (diagnostics only)

Entities only ever read this model, so they don't care which backend produced the data.

### Config entries
- **One config entry per Apple account.** In the setup dialog you turn on either or both backends:
  - **iCloud devices** (on by default). Devices are found automatically: own devices, plus optionally
    Family Sharing devices. You choose which to track in the options; new ones are added disabled.
  - **Find My network tags** (optional, labelled **experimental**). Each tag is a **config subentry**.
    - **Import from iCloud (default):** unlock the keychain once, and every tag on the account
      (including shared ones the account owns) is listed to pick from.
    - **Fallbacks:** upload a key JSON from a Mac, or paste a DIY OpenHaystack key.
    - **New tags:** a "Sync tags from iCloud" button/service re-runs the import. It asks for the
      passcode again unless the unlocked keychain session can be reused (to verify in the spike).
    - The tracked devices' keys are saved in `.storage`, and diagnostics redact them. Anyone with
      access to the HA config could locate these tags, so the README must say so.
- The two backends log in separately, so you may get **two two-factor prompts** during setup. Each
  backend has its own reauth so one failing doesn't break the other.

### Coordinators and polling
- **iCloud:** an adaptive interval instead of a fixed one:
  - In the home zone: 15 min (default). Away: scaled by distance to the nearest zone, from 5 min to
    30 min. The 5 min floor is deliberate: each `shouldLocate` wakes the phone's GPS, and Apple
    throttles frequent callers. A lower floor can be an advanced option later.
  - The account is refreshed at least hourly to keep the session alive.
  - Uses `shouldLocate` only when a fresh position is actually needed, which keeps phone battery use down.
- **Find My network:** fixed 15 min (configurable 5–60), matching how often the tags change their keys.
  One batched `fetch_location`, then save the alignment state. Fetches send the emulated Mac's headers
  (not strictly required by `findmyservice/v2/fetch`, see 1d B), and any re-auth must go through the
  Mac. Header fetches are a subprocess run, so call them via the executor.
  - **Tags without a `KeyAlignmentRecord`** (6 of 11 in our test account, incl. AirTags) would scan
    ~13,000 key indices (~45 requests) on the first locate. Do a careful, rate-limited alignment probe
    once per tag (OpenTagViewer's `main.py` probe as reference), save the alignment, and never
    repeat the full scan.
  - `hsc 434` → anisette resync. HTTP 429 from GSA → retry on a fresh connection (not a rate limit).
    Never re-provision the emulated Mac (HTTP 433); always reuse the saved machine.
- **Shared rules for both:**
  - Keep the last good location when a new one is missing or older.
  - Restore locations after an HA restart.
  - Auth errors raise `ConfigEntryAuthFailed`, which starts reauth.
  - Throttling or 5xx errors raise `UpdateFailed` with exponential backoff and a minimum gap between
    password logins (the iCloud3 lessons).

### Entities per tracked item (one HA device each)
| Entity | iDevice | Tag | Content |
|---|---|---|---|
| `device_tracker.<name>` (GPS) | ✓ | ✓ | lat/lon/accuracy; attributes: `last_seen`, `source`, `position_type` / `confidence` |
| `sensor.<name>_battery` | ✓ | best-effort | iCloud `batteryLevel`; AirTag status byte |
| `binary_sensor.<name>_charging` | ✓ | – | `batteryStatus` |
| `sensor.<name>_last_seen` | ✓ | ✓ | timestamp |
| `sensor.<name>_distance_from_home` | ✓ | ✓ | haversine to `zone.home` (HA's built-in Proximity integration also works) |
| `sensor.<name>_location_accuracy` | ✓ | ✓ | diagnostic |
| `binary_sensor.<name>_online` | ✓ | – | `deviceStatus` |
| `button.<name>_play_sound` | ✓ | – | `playSound` |
| `button.<name>_refresh` | ✓ | ✓ | refresh now |

Later: lost-mode / send-message services, a reverse-geocoded address sensor, a location history
service, and merging with Companion App trackers (use the newer location, like iCloud3).

---

## 3. Config flow
1. **user**: Apple ID, password, server region (global / China), backends to enable.
2. **iCloud login** (pyicloud SRP) → if two-factor is needed → choose trusted device or SMS → **code**
   → save session/trust → discover devices.
3. **Find My network login** (FindMy.py with Mac-emulated anisette), if enabled → its own
   two-factor method and code steps → save the session **and the emulated-Mac provisioning state**.
   Show a progress step: the first provisioning includes the one-time 3.5 MB CoreADI download from
   Apple. Reauth reuses the saved machine, so no new "MacBook Pro" appears in the account.
4. **select_devices**: pick which iCloud devices to track (own + family).
5. Create the entry. **Reauth** runs again only for the backend that failed.
6. **Options**: device selection, polling intervals, include family, distance unit, keep stale location.
7. **Tag import (if tags are enabled):**
   - Pick an Apple device from `recovery_options()`.
   - Enter that device's passcode, using a password field. It's never stored or logged, and you get at
     most 3 attempts.
   - Fetch the BeaconStore records → multi-select the tags to track → one subentry is created per tag.
   - Your own iPhones/Macs are hidden from this list, because the iCloud backend already covers them.
8. **Subentry "Add tag manually"** (fallback): file upload or paste → validate
   (`FindMyAccessory.from_json` / `KeyPair.from_b64`) → name.

The password is kept because pyicloud needs it for silent re-login when Apple drops the session
token. It is **not encrypted**: it sits in plain text in `.storage/core.config_entries`, protected
only by file permissions and by whoever can read HA backups. Make this an explicit choice in the flow
("remember password for automatic re-login"), say so plainly in the dialog and the README, and
redact it in diagnostics.

---

## 4. Risks to verify early (spike before building UI)
1. **Dependency conflicts** with HA pins: `bleak` (findmy pins `<3.1`), `aiohttp`, `cryptography`,
   `srp`. Also the `pyicloud` version: HA core's `icloud` integration pins its own, which matters if a
   user runs both.
2. **`unicorn` on HA OS: resolved (spike 0b, 2026-11-23).** In the HA 2026.11.2 container
   (Alpine/musl, Python 3.14, no compiler) `unicorn` 2.1.4 installs from a prebuilt wheel on both
   amd64 and aarch64, and CoreADI boots under it. Still to measure: CPU/RAM on a real Pi 4 (QEMU showed
   ~1.1 s and 84 MB per engine run). The add-on fallback is only needed if that's too much.
3. **pyicloud maturity**: check that the timlaing fork handles SRP, two-factor, trust tokens and China,
   and that it's async-safe in an executor. If it falls short, write our own small fmipservice client
   using iCloud3 as a reference (re-implemented, not copied).
4. **Advanced Data Protection**: the iCloud web API needs "Access iCloud Data on the Web" turned on.
   Detect this and show a clear error.
5. **Apple throttling**: undocumented. Use conservative defaults, jitter, and backoff.
6. **iCloud key download depends on an unreleased fork.** Options, in order of preference:
   - (a) Upstream FindMy.py releases the `icloud`/`keychain`/`cloudkit` modules → just bump the
     version. Watch the releases.
   - (b) Until then, publish a wheel of our fork to PyPI under its own name (e.g. `findmy-ha`) and
     require that. This is the realistic option, not a last resort.
   - (c) Pinning the fork as a git URL in `manifest.json`
     (`findmy @ git+https://github.com/parawanderer/FindMy.py@<sha>`): the HA 2026.11 container
     **does** have `git` (spike 0b), so installs may work, but hassfest/HACS validation may still
     reject it and older installs may lack git. Prefer (b). The same applies to `machoadi`, which also
     needs publishing as a wheel (or vendoring as a separate GPL package).
7. **Mac emulation is required** (settled, see 1d B). Remaining questions:
   - ~~Does polling need Mac anisette too?~~ Yes (token bound to machine, 1d B). The emulator lives
     in the integration; spike 0b showed it installs and runs in HA core.
   - How long do the token and the provisioned machine stay valid (hours? days?), and how often does
     login have to be repeated?
   - Does the saved provisioning state really prevent new fake "MacBook Pro" entries on reauth?
   - **Licensing:** `macOS-ADI-emulator` is GPL-2.0-only. Call it only as a subprocess (or in the
     add-on), never import it. The README must state that the integration downloads Apple's CoreADI
     library from Apple's servers at runtime; we never redistribute it.
8. **Apple changes**: this route has broken repeatedly in 2026 (Aug–Sep sign-in refusals, Android ADI
   cut off 2026-10-12, Android client info refused ~10-20, `macOS` spelling refused 11-20). Keep manual
   key upload as a permanent fallback, keep the emulated identity (model, OS string, build) in one
   constant so a fix is a one-line release, and give clear error messages.

---

## 5. Milestones
0. **Spikes** (standalone scripts, run inside an HA container on amd64 + aarch64):
   a) pyicloud: login + two-factor, save the session, list devices, one `refreshClient`.
   b) ✅ **Done 2026-11-23** (`spike/ha_install_check.sh`, logs in `spike/state/`). **Install check first** (it decides the architecture): install pyicloud + the FindMy.py fork +
      `macOS-ADI-emulator` into the HA core environment on amd64 and aarch64. Check for dependency
      conflicts, whether `unicorn` installs without a compiler, and whether the emulator can provision
      and produce headers there. Measure CPU/RAM on a Pi 4.
   c) ✅ Real-account `full --anisette mac` end to end (2026-11-23): sign-in, `searchPartyToken`,
      keychain unlock, 11 accessories decrypted, 5 located; `poll` works with Mac and with empty anisette
      (401 control). **Remaining: token lifetime (re-run `poll` after 1 h, 24 h, 3 days).** Local FindMy.py fork with the `EmulatedMacAnisetteProvider`: login + two-factor → keychain
      unlock with a device passcode → download BeaconStore records → fetch the location of one AirTag.
      Record which steps succeed. Then answer the open questions:
      - Does `fetch_location` work with **no** anisette, or with Android/remote-v3 anisette, after an
        emulated-Mac login? (Decides integration vs. add-on.)
      - Restart the script with the saved provisioning state: no HTTP 433, no new "MacBook Pro" entry?
      - How long do the session and token last (re-run after 1 h, 24 h, 3 days)?
      - Can the unlocked keychain session be reused for later "Sync tags" runs without the passcode?
   (The earlier remote-v3 / local-Android / rustpush comparison is done; see 1d B.)
> **Status 2026-11-23:** milestones 1–3 and most of 5–6 are written for the iCloud backend:
> config flow (2FA, reauth, options), adaptive coordinator with backoff and restore, tracker,
> sensors, binary sensors, buttons, diagnostics, en/de translations, README. 32 tests pass (in a
> Linux container; HA doesn't run on Windows), ruff and hassfest are clean. **Not yet tried against
> a real Apple account or a real HA instance.**

1. **Scaffold**: repo, `hacs.json`, manifest, dev environment, ruff/mypy, GitHub Actions (hassfest, HACS validation, pytest).
2. **iCloud backend end-to-end**: config flow with two-factor + reauth, device discovery, coordinator,
   `device_tracker` on the map. (Fastest path to something useful.)
3. **Sensors and buttons for iDevices**: battery, charging, online, last seen, distance, play sound.
   → **First release (v0.1): iCloud devices only.** Useful on its own and free of the emulation risk.
4. **Find My network backend (experimental)**: anisette provider (in-process or add-on, per spike
   0b/0c), second login, **iCloud tag import (keychain unlock)**, the manual fallback, tag trackers
   and sensors, and the "Sync tags" button. Ship behind an "experimental" label.
5. **Adaptive polling, backoff, restore on restart, diagnostics, translations (en/de).**
6. **Docs**: README with setup, the key-extraction guide, and an FAQ (ADP, passkeys, China). Also:
   the stored password is plain text, tag keys in `.storage` let anyone locate the tags, the
   integration downloads Apple's CoreADI at runtime, and it adds one "MacBook Pro" to your account.
7. **Later**: Companion App merge, lost mode, history, address sensor.

## 6. Testing
- Unit tests with mocked backends:
  - Config flow: success, two-factor paths, bad password, ADP error, partial reauth.
  - Coordinators: `None` and stale reports, auth failure, throttling backoff.
  - Normalisation of both payload formats into `TrackedLocation`.
- Recorded (redacted) iCloud `refreshClient` responses as fixtures.
- Manual end-to-end on a dev HA instance with a real account: phone, iPad, one AirTag.

## Update 2026-11-23: one login for devices and tags

The two backends (pyicloud web login for devices, emulated-Mac login for tags) are merged. The
emulated-Mac session's `mmeFMIPAppToken`, sent with the Mac's anisette headers, works against
`fmipmobile.icloud.com` (`spike/fmip_poll.py`: HTTP 200, devices with locations and battery).
`mmeFMIPToken` gets a 401 on `initClient`, so the app token is the one used. pyicloud is gone.

- `backends/fmip.py`: the FMIP client (Basic auth `dsid:token`, partition-host redirect, renewal on
  401 via the stored password). Apple's root CA is bundled as `apple_root_ca.pem`, since the host
  isn't signed by a public CA.
- One config entry per account (`AccountCoordinator`): devices adapt to zones, tags at most every
  15 min, a half that fails keeps its last positions.
- The config flow is one path: sign in, 2FA, keychain passcode, one list of devices and tags.
- Dropped: the China region (untested on this route) and the optional password storage.
