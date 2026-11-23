# FindMyApple

A Home Assistant integration that puts your Apple devices **and** your Find My tags on the map, from
**one Apple Account sign-in**. Every selected iPhone, iPad, Mac, Apple Watch, AirPods, AirTag or
"Works with Find My" tag becomes a `device_tracker`, plus sensors.

**Status: experimental.** The integration signs in as an emulated Mac (see below). That is what makes
a single login possible, but it is an unofficial route that Apple can change.

## How it works

You sign in once (password, two-factor code), then enter a device passcode once so the iCloud Keychain
can hand over your tags' keys. After that, one session provides:

- **Devices** (iPhone, iPad, Mac, family devices): live location, battery and sound, from Apple's
  Find My iPhone service. Polled with an interval that adapts to your zones.
- **Tags** (AirTags and other Find My tags): locations reported by the Find My network, polled every
  15 minutes (their keys rotate that often).

Your own Apple devices also show up in the keychain as tags; they are left out of the tag list because
the device route is more accurate.

## What you get per device

| Entity | Notes |
|---|---|
| `device_tracker.<name>` | GPS position, accuracy, `last_seen`, `position_type`, family member |
| `sensor.<name>_battery` | percent (devices only) |
| `sensor.<name>_last_seen` | when the position was measured |
| `sensor.<name>_distance_from_home` | km from your Home Assistant home location |
| `sensor.<name>_location_accuracy` | diagnostic |
| `binary_sensor.<name>_charging`, `_online` | devices only |
| `button.<name>_play_sound` | devices that support it |
| `button.<name>_refresh_location` | asks for a fresh fix now |

## Install

1. HACS → Integrations → ⋮ → Custom repositories → add this repository as an *Integration*.
2. Install **FindMyApple**, restart Home Assistant.
3. Settings → Devices & services → Add integration → **FindMyApple**.
4. Sign in with your Apple Account and enter the two-factor code.
5. Enter the screen passcode of one of your Apple devices (used once to unlock the keychain, never
   stored). If no device holds a keychain record, setup continues with devices only.
6. Pick the devices and tags to track.

## Polling and battery

Each device update asks the selected devices for a fresh GPS fix (like opening Find My), so the
interval adapts:

- inside any zone: every 15 min
- just outside a zone: every 5 min, rising to 30 min at 50 km from the nearest zone

Tags are located at most every 15 minutes. Throttling or errors from Apple back off exponentially
(2 min up to 1 h); if only one half fails, the other keeps updating and the last known positions are
kept. All intervals are in the integration's options.

## Privacy and security: read this

- **The emulated Mac.** On first sign-in Home Assistant downloads Apple's macOS ADI library from Apple
  (about 3.5 MB) and registers one "MacBook Pro" with a serial starting with `0FMAHA` on your Apple
  Account. You can remove it at account.apple.com. It is kept when you remove the integration, so
  adding the account again doesn't create a second one.
- **Password storage.** The password is stored **unencrypted** in Home Assistant's `.storage` folder
  (and therefore in backups), because Apple's session renewal needs it.
- **Session and tag keys.** The signed-in session and your tags' keys are kept in `.storage/`. Anyone
  with access to your Home Assistant config can use them to locate your devices and tags. Removing
  the integration deletes them.
- **TLS.** Apple's Find My iPhone host is signed by Apple's own root CA, which is not in the usual CA
  bundles, so the integration bundles `apple_root_ca.pem` and uses it only for that host.
- Diagnostics downloads redact your Apple ID, names, IDs and coordinates.

## FAQ

**My account uses a hardware security key.** Not supported yet; sign-in aborts with a message.

**China mainland accounts.** Not supported by this sign-in route (untested).

**Wrong keychain passcode.** Apple locks keychain recovery after repeated wrong passcodes, so setup
stops after three attempts.

**Two Home Assistant integrations for one account?** Works; each signs in separately.

## Development

Home Assistant doesn't run on Windows, so the tests run in Docker:

```
docker run --rm -v "$PWD:/repo:ro" python:3.13 sh /repo/scripts_test_docker.sh
```

Research spikes (anisette, Find My tags, FMIP with the emulated Mac) live in [spike/](spike/README.md).
