#!/usr/bin/env python3
"""Receive live sensor data pushed by the Ecowitt WS-2551 (HP2551 console).

The HP2551 console (192.168.5.44, EasyWeather firmware) has no pollable
live-data API — its port-45000 protocol only serves configuration. Live data
comes via "customized upload": the console HTTP-POSTs all sensor values to a
server of your choosing on a fixed interval. This script is that server,
built on aioecowitt (the library Home Assistant uses for the same job),
which parses the upload and derives metric values (tempc, solarradiation in
W/m2, etc.).

Configure the console (WS View Plus app -> device -> Customized, or console
menu Setup -> Weather Server -> Customized Website):

    Protocol:        Ecowitt
    Server IP:       <IP of the machine running this script>
    Path:            /data/report/
    Port:            8090
    Upload interval: 60 (seconds)

Run:

    python ecowitt_listener.py            # listen on port 8090
    python ecowitt_listener.py --port 9000 --verbose
"""

import argparse
import asyncio
import logging
import time

from aioecowitt import EcoWittListener

# Sensor keys for the one-line summary printed on each report.
SUMMARY = [
    ("tempc", "out", "\N{DEGREE SIGN}C"),
    ("tempinc", "in", "\N{DEGREE SIGN}C"),
    ("solarradiation", "sun", "W/m\N{SUPERSCRIPT TWO}"),
    ("uv", "uv", ""),
    ("humidity", "hum", "%"),
    ("windspeedkmh", "wind", "km/h"),
    ("rainratemm", "rain", "mm/h"),
]


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--port", type=int, default=8090)
    parser.add_argument("--path", default="/data/report/")
    parser.add_argument(
        "--verbose", action="store_true", help="log every sensor as it appears"
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    listener = EcoWittListener(port=args.port, path=args.path)

    def print_summary():
        by_key = {s.key: s.value for s in listener.sensors.values()}
        parts = [
            f"{label} {by_key[key]}{unit}"
            for key, label, unit in SUMMARY
            if by_key.get(key) is not None
        ]
        print(f"{time.strftime('%H:%M:%S')}  " + "  ".join(parts), flush=True)

    def on_new_sensor(sensor):
        if args.verbose:
            print(f"found sensor: {sensor.key} ({sensor.name}) = {sensor.value}")
        # Every report carries dateutc, so one summary line per report;
        # call_soon defers the print until the full report is processed.
        if sensor.key == "dateutc":
            sensor.update_cb.append(
                lambda: asyncio.get_running_loop().call_soon(print_summary)
            )

    listener.new_sensor_cb.append(on_new_sensor)

    async def run():
        await listener.start()
        print(
            f"Listening on port {args.port} — point the console's customized "
            f"upload (protocol Ecowitt, path {args.path}) at this machine.",
            flush=True,
        )
        await asyncio.Event().wait()

    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
