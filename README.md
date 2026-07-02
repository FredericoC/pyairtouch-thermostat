# pyairtouch connectivity test

A minimal script to check whether a Polyaire AirTouch (4 or 5) controller can be
discovered and connected to on the local network, using
[pyairtouch](https://pypi.org/project/pyairtouch/).

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
