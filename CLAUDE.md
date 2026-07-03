# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

Climate control for a Polyaire AirTouch 5 HVAC controller, using the
[pyairtouch](https://pypi.org/project/pyairtouch/) library:

- `main.py` — single-script connectivity test (discover, connect, print status).
- `climate_service.py` — long-running service that keeps each room inside a
  configured temperature range, tuned via `config.toml`. Also records per-unit
  temperature samples to `history.db` (SQLite, `readings` table, ts in unix
  epoch UTC) for temperature-over-time graphs.
- `webui.py` + `webui.html` — stdlib-only web dashboard over `history.db`
  (default port 8765): combined + per-unit temperature charts, JSON/CSV API.
  Reuses `load_config` from `climate_service.py`; opens the DB read-only.
- `com.frederico.airtouch-climate.plist`, `com.frederico.airtouch-webui.plist`
  — launchd definitions for the two services.

There are no tests, linters, or build steps.

## Setup and run

```sh
python3 -m venv .venv
source .venv/bin/activate        # fish: source .venv/bin/activate.fish
pip install -r requirements.txt

python main.py 192.168.5.221                 # connectivity check
python climate_service.py --dry-run --once   # one control pass, no commands sent
python climate_service.py                    # run the climate service
```

## Key constraint: discovery across subnets

The AirTouch controller lives on a separate IoT VLAN at `192.168.5.221`. UDP
broadcast discovery does not route across subnets, so directed discovery is
required: the host arg / `AIRTOUCH_HOST` env var for `main.py`, or
`service.host` in `config.toml` for the service.

## System layout (important)

The house has **7 AC units and no zones** — every `AirConditioner` in the
pyairtouch API is one room. Two units are *masters* that dictate heat/cool mode
for their group (multi-split outdoor-unit constraint):

- **MPR** (master) → Bed 3, Bed 4
- **Study** (master) → Living, Master, Bed 2

Mode commands must only be sent to masters, via
`ac.set_mode(mode, power_on=False)` so the master is not switched on as a side
effect. Member units are only ever powered on/off (`ac.set_power(...)`).

## Control policy (climate_service.py)

Per-room on/off thermostat with hysteresis, plus sticky group mode selection:
a group flips heat<->cool only when no room demands the current mode, a room is
`mode_switch_buffer` degrees past its range, and `min_mode_dwell_minutes` has
passed. Per-unit `min_power_toggle_minutes` prevents compressor short-cycling.
All thresholds live in `config.toml`. Temperatures can be `None` — handle that
when reading `current_temperature`.

`[shutdown]` windows (`"HH:MM-HH:MM"` local time, may cross midnight) switch
everything off for night/away periods: when a window starts, one off pass
powers every unit off (bypassing `min_power_toggle_minutes`) and the control
policy is suspended for the rest of the window. A unit switched on manually
during the window is left on.
