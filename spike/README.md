# FindMyApple spike

Goal: prove we can download Find My tag keys from iCloud and locate the tags **without Mac
emulation**, and find out which anisette source Apple accepts for each step.

`spike.py` runs the chain in recorded steps and writes one JSON line per step to
`state/results.jsonl`. Secrets (session, tag keys) stay under `state/<mode>/`, which is
gitignored. Passwords and the device passcode are read with `getpass` and never stored.

## Setup

```
cd spike
uv sync
```

## Phases

- `probe` — no Apple ID. Can this anisette source produce headers at all?
- `full`  — log in, open the iCloud keychain, download BeaconStore records, recover keys with
  a device passcode, decrypt accessories, save their keys, and locate them. **Interactive.**
- `poll`  — restore the saved session + keys and only locate (what HA does every 15 min).
  Can run with a *different* anisette source than the login, including `none`.

```
uv run spike.py probe --anisette local
uv run spike.py probe --anisette remote --anisette-url https://anisette.wedotstud.io

uv run spike.py full  --anisette remote --anisette-url https://anisette.wedotstud.io
uv run spike.py poll  --anisette none   --from remote
```

For `full` you need: your Apple ID + password, a 2FA code, and the **screen passcode of one
of your Apple devices** (used once to unlock the keychain; an existing Apple device must have
created an escrow record). Nothing is written to your Apple account except possibly one
unobtrusive "MacBook Pro" entry in the device list, which you can remove at account.apple.com.

## Findings so far (2026-11-23)

Environment: Windows, Python 3.12, `findmy` = parawanderer fork (upstream 0.10.2 + iCloud
keychain/CloudKit modules). `probe` phase, no Apple ID yet.

| Anisette source | Result |
|---|---|
| `local` (FindMy.py's bundled Android ADI in unicorn) | **Fails.** Apple's provisioning returns no `ptm`/`tk` field → `KeyError: 'ptm'`. Consistent with reports that Apple stopped provisioning Android ADI directly. |
| `remote` `https://ani.sidestore.io` (the library default) | **Down** (HTTP 500/502). |
| `remote` `https://ani.npeg.us` | **OK** — valid headers (MD 40B, machine 80B, RINFO 17106176). |
| `remote` `https://anisette.wedotstud.io` | **OK** — same. |

**First `full` run (remote, `anisette.wedotstud.io`):** refused at SRP **init** with
`GrandSlamRefusedError ec=-80043` ("This action cannot be completed at this time"). Init only
carries the email + anisette headers, so this is Apple rejecting the machine identity, not the
password. Suspected cause: the server's anisette is provisioned as `<MacBookPro13,2>
<macOS;13.1;22C65>` (from `/v3/client_info`), while FindMy.py claims `<MacBookPro18,3> <Mac OS
X;13.4.1;22F8>` in the same request. `spike.py` now copies the server's identity by default
(`--library-identity` restores the old behaviour for comparison).

**Second `full` run (same server, server's identity):** worse. Apple's edge answered HTTP 503 with
a 190-byte HTML page, so the request never reached the login service. The library's
`User-Agent` for this request is hardcoded to macOS 10.14 and doesn't follow the identity, so
this request still contradicted itself. A 503 can also be temporary after repeated attempts.

Conclusion so far: two remote-anisette login attempts, two refusals (identity refused at init,
then blocked at the edge). Neither reached the password or the Find My token.

**Trying `stek29/export-findmy` instead** (`third_party/export-findmy`). Same goal, different
login path: it is Rust on `rustpush`, and it impersonates an **iPhone** (iOS 17.4) via the iOS
`iosbuddy` MobileMe delegate, where FindMy.py impersonates a Mac via the macOS GSA path. If
Apple's block is specific to the Mac/Xcode login path, the iOS path may get through. It still
uses a remote Anisette v3 server. Unlike the FindMy.py fork, it **joins the keychain trust
circle** (creates its own escrow bottle, removable afterwards with `--delete-own-escrow-bottle`).
Built with a `.local/device-profile.toml` copied from the template (model_class corrected to
`iPhone`).

**Build: OK on Windows** (`cargo build --release`, 1m20s) after two environment fixes:
`protoc` (winget `Google.Protobuf`) and an MSVC OpenSSL (winget `ShiningLight.OpenSSL.Dev`), built
with `OPENSSL_NO_VENDOR=1` and `OPENSSL_DIR/OPENSSL_LIB_DIR/OPENSSL_INCLUDE_DIR` set. The vendored
OpenSSL build fails under Git Bash's incomplete Perl. `--help` runs. Its default anisette server
`ani.sidestore.io` was down earlier, so pass `--anisette-url https://anisette.wedotstud.io`.
Interactive login still pending (needs your Apple ID, 2FA and a device passcode).

**First export-findmy run (`anisette.wedotstud.io`):** `ErrorGettingAnisette ... 400 "WebSocket
endpoint only accepts Connection: upgrade requests"`. Failed before any request to Apple. That
server answers FindMy.py's plain header fetch but rejects a proper WebSocket upgrade at
`/v3/provisioning_session` (checked with curl), which export-findmy needs. `ani.npeg.us` accepts the
upgrade (101), so use that one for this tool.

**Takeaways**
- No Mac emulation is needed to *produce* anisette headers: working remote v3 servers exist.
- The library's own local provider is the broken path right now, not remote anisette.
- Still open, needs an interactive `full` run with a real Apple ID: whether Apple accepts
  those remote-anisette logins for the **Find My / searchParty token** (OpenTagViewer claims
  it doesn't since Oct 2026), and whether `poll --anisette none` can locate tags once a
  session exists. These are the decisions that shape how the HA integration handles anisette.

Depending on a public anisette server in production is a privacy and reliability concern
(it sees login-time OTP header requests from the user's IP). If remote anisette turns out to
be required, the integration should let the user point at their own server.

## Emulated Mac (`--anisette mac`), 2026-11-23

The route Apple still serves: Apple's macOS CoreADI 4.19 (fetched from Apple by one 3.5 MB range
request into the iTunes 12.8.3 installer, hash-pinned) under Unicorn, run by `machoadi`
(parawanderer/os-findmy-e@595d398, GPL-2.0) **as a subprocess** through the integration's own
`custom_components/findmyapple/macanisette.py`. The machine lives in `state/mac/emulated-mac/`;
never delete it after signing in (Apple provisions a device id only once, and the Find My token
only works with headers from the machine that signed in).

**`probe --anisette mac`: OK.** Provisioning accepted by Apple (`ptm`/`tk` issued, which every
Android route failed to get), headers produced: MD 40 B, machine 80 B, RINFO 33883392, identity
`<MacBookPro13,2> <Mac OS X;13.1;22C65>`.

**HA container check (`ha_install_check.sh`, spike 0b): OK on amd64 and aarch64.** HA 2026.11.2,
Python 3.14, musl, no compiler: `unicorn` 2.1.4 installs from a wheel, pyicloud + the FindMy.py
fork + machoadi install with no broken requirements, and CoreADI boots under Unicorn
(37 MB RSS amd64; 84 MB under QEMU arm64). `git` is present in the image. Logs:
`state/install_check_{amd64,arm64}.log`.

**First `full --anisette mac` with a real account (2026-11-23): signed in, token issued.**
Login + 2FA (trusted-device code) → `LOGGED_IN` with ten service tokens **including
`searchPartyToken`**; PET refresh, keychain session, CloudKit `BeaconStore` zone, records
(11 `MasterBeaconRecord`, 12 naming, 9 key-alignment, 15 owned-device keys) and escrow recovery
options (2 recoverable iPhones) all OK. Stopped at `keychain_unlock` only because the spike matched
the serial case-sensitively (fixed: pick by number or any-case serial). Rerunning restores the saved
session on the same machine: no new login or device entry.

**Full chain with a real account (2026-11-23, second run, restored session): works end to end.**
Keychain unlock with an iPhone passcode (145 keychain keys) → 11 accessories decrypted (third-party
tags, AirTags, 2 iPhones) → **5 located** with reports 16 min to ~2 days old, 58–83 m accuracy.
The other 6 were skipped by the spike: no `KeyAlignmentRecord`, so a locate would scan ~13,000
key indices (~45 requests). The integration needs a careful alignment probe for those.

**Polling without anisette works on `findmyservice/v2/fetch`.** `poll --anisette none --from mac`
located the same 5 tags. `control_poll.py` confirms it with a negative control:

| Arm | HTTP |
|---|---|
| emulated-Mac headers | 200 |
| empty `X-Apple-I-MD` / `-M` | 200 |
| empty anisette + corrupted `searchPartyToken` | **401** |

So the report fetch checks the token, not anisette. (os-findmy-e's "token bound to its machine"
result was measured on `acsnservice/fetch`, which FindMy.py no longer uses.) But after the 401
FindMy.py re-authenticated with the empty headers and Grand Slam refused (`ec -80009`, "MID is
invalid"): **sign-in and token renewal need the emulated Mac**; plain fetches don't.

### Next: the interactive run (needs you)

```
uv run spike.py full --anisette mac
```

Asks for your Apple ID, password, the 2FA code, then the serial + screen passcode of one of your
Apple devices (shown in a list). Expect one new "MacBook Pro" with serial `0FMAHA…` in your
device list. Then, to answer the open questions:

```
uv run spike.py poll --anisette mac              # same machine: should locate
uv run spike.py poll --anisette none --from mac  # no anisette: expected 401
```

Re-run the first `poll` after 1 h, 24 h and 3 days to learn how long the session lasts.

## Three login attempts, three refusals before the password/token (2026-11-23)

1. FindMy.py's local (Android-style) anisette provider: Apple's provisioning omits `ptm`/`tk`.
2. Remote v3 servers (`anisette.wedotstud.io`, `ani.npeg.us`) via the parawanderer FindMy.py fork:
   refused at SRP `init` (`ec=-80043`) and then blocked at Apple's edge (HTTP 503) depending on
   header consistency.
3. `stek29/export-findmy` (Rust, rustpush, impersonates an **iPhone** instead of a Mac): built
   successfully on Windows after installing `protoc` and an MSVC OpenSSL (see below), but panicked
   on the same missing `ptm`/`tk` fields during provisioning against `ani.npeg.us`.

**Root cause, confirmed by OpenTagViewer's own engineering notes** (`third_party/OpenTagViewer/AGENTS.md`,
rule 21): since 2026-10-12 Apple only issues the Find My token to logins whose anisette was minted
by a Mac or an iPhone *in a way Apple itself recognises* — and specifically rejects anything
Android-minted, which is what `anisette`/FindMy.py's local provider and ordinary remote v3 servers
produce. export-findmy's iPhone impersonation apparently isn't convincing enough either (or hits
the same provisioning-level block before identity is even checked).

**The fix OpenTagViewer ships:** `python/exporter/macadi.py` — Apple's real macOS CoreADI library,
fetched from Apple (an HTTP range request into the iTunes 12.8.3 installer, pinned by hash), run
under the `unicorn` CPU emulator already in our dependency tree, always in its own subprocess. See
PLAN.md section 1d for the concrete porting plan. **This looks like the only working path to
avoid manual Mac key export right now.**

### Windows build notes for export-findmy (kept for reference)

- `cargo build --release` first failed: `openssl-sys` tried to compile OpenSSL from source using
  Git Bash's incomplete Perl (`Can't locate Locale/Maketext/Simple.pm`).
- Fix: installed `protoc` (`winget install Google.Protobuf`) and an MSVC-compatible OpenSSL
  (`winget install ShiningLight.OpenSSL.Dev`), then built with:
  ```
  OPENSSL_NO_VENDOR=1
  OPENSSL_DIR="C:\Program Files\OpenSSL-Win64"
  OPENSSL_LIB_DIR="C:\Program Files\OpenSSL-Win64\lib\VC\x64\MD"
  OPENSSL_INCLUDE_DIR="C:\Program Files\OpenSSL-Win64\include"
  ```
- Its default anisette server (`ani.sidestore.io`) was down; `anisette.wedotstud.io` rejects the
  WebSocket upgrade this tool needs (plain HTTP works, WS doesn't); `ani.npeg.us` accepts the
  WebSocket but then the provisioning call itself fails the same way as everywhere else.
