# FindMyApple

A Home Assistant integration that puts your Apple devices on the map. Every selected iPhone, iPad,
Mac, Apple Watch or AirPods becomes a `device_tracker`, plus sensors.

**Status: v0.1, iCloud devices only.** AirTags and other Find My tags are planned as an
experimental second backend (see [PLAN.md](PLAN.md)).

## What you get per device

| Entity | Notes |
|---|---|
| `device_tracker.<name>` | GPS position, accuracy, `last_seen`, `position_type`, family member |
| `sensor.<name>_battery` | percent |
| `sensor.<name>_last_seen` | when the position was measured |
| `sensor.<name>_distance_from_home` | km from your Home Assistant home location |
| `sensor.<name>_location_accuracy` | diagnostic |
| `binary_sensor.<name>_charging`, `_online` | |
| `button.<name>_play_sound` | devices that support it |
| `button.<name>_refresh_location` | asks for a fresh fix now |

## Install

1. HACS → Integrations → ⋮ → Custom repositories → add this repository as an *Integration*.
2. Install **FindMyApple**, restart Home Assistant.
3. Settings → Devices & services → Add integration → **FindMyApple**.
4. Sign in with your Apple Account, enter the two-factor code, pick the devices to track.

## Polling and battery

Each update asks the selected devices for a fresh GPS fix (like opening Find My), so the interval
adapts:

- inside any zone: every 15 min
- just outside a zone: every 5 min, rising to 30 min at 50 km from the nearest zone

Throttling or errors from Apple back off exponentially (2 min up to 1 h). All intervals are in the
integration's options.

## Privacy and security: read this

- **Password storage.** If you tick *Remember password*, your Apple Account password is stored
  **unencrypted** in `.storage/core.config_entries` (and therefore in every Home Assistant backup).
  Without it, Home Assistant asks you to sign in again when Apple ends the session (about every two
  months).
- **Session files.** The iCloud session and trust token are kept in `.storage/findmyapple/`. Anyone
  with access to your Home Assistant config can use them to locate your devices. Removing the
  integration deletes them.
- Diagnostics downloads redact your Apple ID, names, IDs and coordinates.

## FAQ

**"Find My isn't available on iCloud.com for this account."** You have Advanced Data Protection on.
On your iPhone: Settings → your name → iCloud → turn on *Access iCloud Data on the Web*.

**My account uses a hardware security key.** Not supported yet; sign-in aborts with a message.

**China mainland accounts.** Choose *China mainland* as the region during setup.

**Two Home Assistant integrations for one account?** Works, but each signs in separately, and
Home Assistant core's own `icloud` integration pins its own `pyicloud`, so the two can conflict.

## Development

Home Assistant doesn't run on Windows, so the tests run in Docker:

```
docker run --rm -v "$PWD:/repo:ro" python:3.13 sh /repo/scripts_test_docker.sh
```

Research spikes (anisette, Find My tags) live in [spike/](spike/README.md).
