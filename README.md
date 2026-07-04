# AirTouch 5 AC Unit climate control service

Tools for a Polyaire AirTouch 5 controller, using
[pyairtouch](https://pypi.org/project/pyairtouch/) (thank you!)

> **Note:** This is built for my specific house: 7 individual AC units and
> **no zones** — every unit is its own room, so the usual AirTouch
> zone-damper model doesn't apply here. If your setup uses zones, the control
> logic won't map onto it without changes. The config also caters to my needs
> ~99% of this code was written by [Claude](https://claude.com/claude-code) 
> using [pyairtouch](https://pypi.org/project/pyairtouch/).
>
> **Motivation:** I have a passive house with an ERV, and I needed a way
> to control the individual units to keep a reasonably consistent temperature
> throughout. I also hate HVAC noise — which is why I chose a design with 
> individual units that can be switched off once a room reaches temperature, 
> instead of a ducted system running 24/7.

Contents:

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
turning the individual AC units on and off. There are no zones to manage —
the house has 7 individual AC units (one per room) in two groups, each with a
**master** unit that dictates whether the group heats or cools:

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

### Temperature history

The service records a sample per unit (temperature, setpoint, power, mode,
activity) to a SQLite database — `history.db` by default, every 60s, tunable
in the `[history]` section of `config.toml`. This is the data source for
future temperature-over-time graphs, per unit or all units overlaid.

The schema is one `readings` table with `ts` (unix epoch seconds, UTC),
`unit`, `temperature`, `setpoint`, `power` (0/1), `mode`, `activity`. Example
queries:

```sql
-- one unit over time
SELECT ts, temperature FROM readings WHERE unit = 'Bed 2' ORDER BY ts;

-- all units, ready to pivot into one overlaid graph
SELECT ts, unit, temperature FROM readings WHERE ts > unixepoch('now', '-1 day');
```

Or from Python: `pd.read_sql_query("SELECT * FROM readings", sqlite3.connect("history.db"))`.

### Web dashboard

`webui.py` serves a browser dashboard over the recorded history — no extra
dependencies, stdlib only:

```sh
python webui.py            # http://localhost:8765
python webui.py --port 80  # custom port
```

It binds to all interfaces by default, so it's reachable from any device on
the LAN at `http://<this-machine>:8765`. It shows:

- a combined temperature-over-time chart for all 7 units;
- one chart per unit, with the target range shaded and a heat/cool strip along
  the bottom showing when the unit was running;
- current temperature, activity, and a latest-readings table;
- time range presets (6h–30d), hover tooltips, auto-refresh every 60s,
  automatic light/dark mode.

Endpoints: `/` (the page), `/api/data?hours=N` (downsampled JSON),
`/api/readings.csv?hours=N` (raw CSV export). The database is opened
read-only, so the dashboard can never interfere with the control service.

## Install as a service

Both the control service and the web dashboard are meant to run permanently.
Service definitions are included for macOS (launchd) and Raspberry Pi OS /
any Linux with systemd.

### macOS (launchd)

```sh
cp com.frederico.airtouch-climate.plist ~/Library/LaunchAgents/   # control service
cp com.frederico.airtouch-webui.plist ~/Library/LaunchAgents/     # web dashboard
launchctl load ~/Library/LaunchAgents/com.frederico.airtouch-climate.plist
launchctl load ~/Library/LaunchAgents/com.frederico.airtouch-webui.plist

# logs
tail -f ~/Library/Logs/airtouch-climate.log
tail -f ~/Library/Logs/airtouch-webui.log

# stop / uninstall
launchctl unload ~/Library/LaunchAgents/com.frederico.airtouch-climate.plist
launchctl unload ~/Library/LaunchAgents/com.frederico.airtouch-webui.plist
```

The plists assume the repo lives at `/Users/Frederico/Projects/pyairtouch` —
edit the paths if it moves. `KeepAlive` restarts the services if they exit.

### Raspberry Pi OS (systemd)

Needs Python 3.11+ (`tomllib`), which Raspberry Pi OS Bookworm or later ships
by default. On the Pi:

```sh
# 1. Get the code and set up the venv
git clone <this repo> ~/pyairtouch
cd ~/pyairtouch
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

# 2. Sanity checks — the service uses local time for the shutdown windows,
#    and needs to reach the AirTouch across the VLAN
timedatectl                                   # check "Time zone" is correct
.venv/bin/python main.py 192.168.5.221        # connectivity check
.venv/bin/python climate_service.py --dry-run --once

# 3. Install the units
#    The unit files assume /home/pi/pyairtouch and User=pi — edit both
#    files first if your username or checkout path differ.
sudo cp airtouch-climate.service airtouch-webui.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now airtouch-climate airtouch-webui
```

If the timezone is wrong, fix it with `sudo timedatectl set-timezone
Australia/Adelaide` (or your zone) before enabling — otherwise the
`[shutdown]` windows fire at the wrong hours.

Manage the services:

```sh
systemctl status airtouch-climate airtouch-webui

# logs (journald — no log files to manage)
journalctl -u airtouch-climate -f
journalctl -u airtouch-webui --since today

# restart after editing config.toml (it is read only at startup)
sudo systemctl restart airtouch-climate

# stop / uninstall
sudo systemctl disable --now airtouch-climate airtouch-webui
sudo rm /etc/systemd/system/airtouch-climate.service /etc/systemd/system/airtouch-webui.service
sudo systemctl daemon-reload
```

`Restart=always` with `RestartSec=30` restarts either service if it exits
(mirroring launchd's `KeepAlive`/`ThrottleInterval`), and
`After=network-online.target` delays startup at boot until the network is up.
The dashboard is then at `http://<pi-hostname>:8765`.

## TODO

- Fetch data from the Ecowitt weather station and plot outside temperature and
  solar radiation (W/m²) alongside inside temperatures, to identify further
  efficiencies (e.g. pre-cooling before solar load, using free heating/cooling
  from outside conditions).
- Have the web UI display live `climate_service` logs.
