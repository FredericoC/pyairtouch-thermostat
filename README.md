# pyairtouch climate control

Tools for a Polyaire AirTouch 5 controller, using
[pyairtouch](https://pypi.org/project/pyairtouch/):

- `main.py` — minimal connectivity check (discover, connect, print status).
- `climate_service.py` — a long-running whole-house climate control service
  (see below).

## Setup

```sh
python3 -m venv .venv
source .venv/bin/activate        # fish: source .venv/bin/activate.fish
pip install -r requirements.txt
```

## Run

On the **same LAN** as the controller, broadcast discovery works with no args:

```sh
python main.py
```

Across subnets (e.g. the controller is on an IoT VLAN) broadcast doesn't route,
so pass the controller's IP for directed (unicast) discovery instead:

```sh
python main.py 192.168.5.221
# or: AIRTOUCH_HOST=192.168.5.221 python main.py
```

Expected output on success:

- `Discovered: <name> (<host ip>)` for each controller found
- an `AC Status` line and one `Zone Status` line per zone

If you see `No AirTouch discovered`, the script couldn't find a controller —
check you're on the same network segment and that firewall rules allow UDP
broadcast.

The demo stays connected for 5 minutes printing live status updates, then shuts
down. Press `Ctrl+C` to stop early.

## Climate control service

`climate_service.py` keeps every room inside a configured temperature range by
turning the individual AC units on and off. The house has 7 AC units in two
groups, each with a **master** unit that dictates whether the group heats or
cools:

- **MPR** (master) → Bed 3, Bed 4
- **Study** (master) → Living, Master, Bed 2

The service:

- polls temperatures every `poll_interval_seconds` and turns each unit on when
  its room leaves the range (`target_low`–`target_high`) and off once it is
  `hysteresis` degrees back inside it;
- sends mode (heat/cool) commands **only to the masters, without powering them
  on** — member units are only ever powered on/off;
- switches a group between heating and cooling reluctantly: only when no room
  still demands the current mode, at least one room is `mode_switch_buffer`
  degrees past its range, and `min_mode_dwell_minutes` has elapsed since the
  last switch;
- avoids compressor short-cycling via `min_power_toggle_minutes` per unit;
- optionally pushes each unit's setpoint to match the range
  (`manage_setpoints`);
- reconnects automatically with backoff if the connection drops.

All tuning lives in [`config.toml`](config.toml), including per-room range
overrides. Note the service acts as the thermostat: units toggled manually on
the wall panel will be brought back in line within one poll interval.

### Run it

```sh
python climate_service.py --dry-run --once   # one pass, log decisions only
python climate_service.py --dry-run          # continuous, no commands sent
python climate_service.py                    # the real thing
```

### Install as a service (macOS launchd)

```sh
cp com.frederico.airtouch-climate.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.frederico.airtouch-climate.plist

# logs
tail -f ~/Library/Logs/airtouch-climate.log

# stop / uninstall
launchctl unload ~/Library/LaunchAgents/com.frederico.airtouch-climate.plist
```

The plist assumes the repo lives at `/Users/Frederico/Projects/pyairtouch` —
edit the paths if it moves. `KeepAlive` restarts the service if it ever exits.
