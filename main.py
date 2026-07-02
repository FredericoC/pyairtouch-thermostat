"""Minimal connectivity check for a Polyaire AirTouch system on the local network.

Based on the official pyairtouch example:
https://pypi.org/project/pyairtouch/

Run with:
    python main.py                 # broadcast discovery on the local LAN
    python main.py 192.168.5.221   # directed discovery for a known host
                                   # (needed across subnets, e.g. an IoT VLAN)

The host may also be provided via the AIRTOUCH_HOST environment variable.
"""

import asyncio
import os
import sys

import pyairtouch


async def main() -> None:
    # A known host is required when the AirTouch is on a different subnet,
    # because broadcast discovery does not route across subnets. Passing
    # remote_host performs a directed (unicast) discovery to that host.
    remote_host = sys.argv[1] if len(sys.argv) > 1 else os.environ.get("AIRTOUCH_HOST")

    if remote_host:
        print(f"Discovering AirTouch at {remote_host} ...")
    else:
        print("Broadcasting for AirTouch on the local network ...")

    discovered_airtouches = await pyairtouch.discover(remote_host=remote_host)
    if not discovered_airtouches:
        print("No AirTouch discovered")
        return

    for airtouch in discovered_airtouches:
        print(f"Discovered: {airtouch.name} ({airtouch.host})")

    # In this example we use the first discovered AirTouch
    # (typically there is only one per network).
    airtouch = discovered_airtouches[0]

    # Connect to the AirTouch and read initial state.
    success = await airtouch.init()
    if not success:
        print(f"Failed to connect to {airtouch.name} ({airtouch.host})")
        return

    def _temp(value: float | None) -> str:
        # Temperatures are None when a unit has no sensor.
        return f"{value:.1f}" if value is not None else "--"

    def _print_ac(aircon: pyairtouch.AirConditioner) -> None:
        print(f"\nAC Unit [{aircon.ac_id}]: {aircon.name}")
        print(f"  Power        : {aircon.power_state.name}")
        print(f"  Mode         : {aircon.active_mode.name} "
              f"(selected: {aircon.selected_mode.name})")
        print(f"  Fan speed    : {aircon.active_fan_speed.name} "
              f"(selected: {aircon.selected_fan_speed.name})")
        print(f"  Temperature  : {_temp(aircon.current_temperature)} °C")
        print(f"  Set point    : {_temp(aircon.target_temperature)} °C "
              f"(range {_temp(aircon.min_target_temperature)}"
              f"–{_temp(aircon.max_target_temperature)})")
        print(f"  Spill state  : {aircon.spill_state.name}")
        print(f"  Supported    : modes={[m.name for m in aircon.supported_modes]} "
              f"fan={[f.name for f in aircon.supported_fan_speeds]}")

    async def _on_ac_status_updated(ac_id: int) -> None:
        _print_ac(airtouch.air_conditioners[ac_id])

    # List all AC units and their current properties.
    print(f"\nFound {len(airtouch.air_conditioners)} AC unit(s).")
    for aircon in airtouch.air_conditioners:
        _print_ac(aircon)
        # Subscribe so subsequent status changes are printed live.
        aircon.subscribe(_on_ac_status_updated)

    # Keep the connection open to receive live updates for a few minutes.
    print("\nListening for status updates (Ctrl+C to stop)...")
    await asyncio.sleep(300)

    # Shutdown the connection
    await airtouch.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
