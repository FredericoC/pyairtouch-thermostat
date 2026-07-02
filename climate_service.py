"""Whole-house climate control service for a Polyaire AirTouch 5.

Keeps every room inside a configured temperature range by turning individual
AC units on and off. Two units (the group "masters") dictate whether their
group heats or cools: mode commands are sent only to the masters — without
powering them on — and all units (masters included) are powered on/off purely
on their own room's demand.

Heat/cool mode switching is deliberately sticky: a group only flips mode when
no room still demands the current mode, at least one room exceeds its range by
`mode_switch_buffer`, and `min_mode_dwell_minutes` has elapsed since the last
flip.

Run with:
    python climate_service.py                 # uses ./config.toml
    python climate_service.py --dry-run       # log decisions, send nothing
    python climate_service.py --once          # single control pass, then exit
"""

import argparse
import asyncio
import logging
import signal
import sys
import time
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

import pyairtouch
from pyairtouch import AcMode, AcPowerControl, AcPowerState, AirConditioner

_LOGGER = logging.getLogger("climate")

ON_STATES = frozenset({AcPowerState.ON, AcPowerState.ON_AWAY, AcPowerState.SLEEP})


# ---------------------------------------------------------------------------
# Configuration


@dataclass(frozen=True)
class RoomConfig:
    target_low: float
    target_high: float


@dataclass(frozen=True)
class GroupConfig:
    name: str
    master: str
    members: tuple[str, ...]


@dataclass(frozen=True)
class Config:
    host: str
    poll_interval: float
    dry_run: bool
    hysteresis: float
    mode_switch_buffer: float
    min_mode_dwell: float  # seconds
    min_power_toggle: float  # seconds
    manage_setpoints: bool
    groups: tuple[GroupConfig, ...]
    rooms: dict[str, RoomConfig]

    def room(self, name: str) -> RoomConfig:
        return self.rooms[name]


def load_config(path: Path) -> Config:
    with path.open("rb") as f:
        raw = tomllib.load(f)

    service = raw.get("service", {})
    defaults = raw.get("defaults", {})
    default_low = float(defaults.get("target_low", 21.0))
    default_high = float(defaults.get("target_high", 23.5))

    groups = tuple(
        GroupConfig(name=name, master=g["master"], members=tuple(g["members"]))
        for name, g in raw.get("groups", {}).items()
    )
    if not groups:
        raise ValueError("config defines no [groups.*] sections")

    overrides = raw.get("rooms", {})
    rooms: dict[str, RoomConfig] = {}
    for group in groups:
        if group.master not in group.members:
            raise ValueError(
                f"group {group.name!r}: master {group.master!r} must be a member"
            )
        for member in group.members:
            o = overrides.get(member, {})
            rooms[member] = RoomConfig(
                target_low=float(o.get("target_low", default_low)),
                target_high=float(o.get("target_high", default_high)),
            )
    for room_cfg_name in overrides:
        if room_cfg_name not in rooms:
            raise ValueError(
                f"[rooms.{room_cfg_name!r}] does not match any group member"
            )
    for name, cfg in rooms.items():
        if cfg.target_low >= cfg.target_high:
            raise ValueError(f"room {name!r}: target_low must be < target_high")

    return Config(
        host=service.get("host", ""),
        poll_interval=float(service.get("poll_interval_seconds", 30)),
        dry_run=bool(service.get("dry_run", False)),
        hysteresis=float(defaults.get("hysteresis", 0.4)),
        mode_switch_buffer=float(defaults.get("mode_switch_buffer", 1.0)),
        min_mode_dwell=float(defaults.get("min_mode_dwell_minutes", 60)) * 60,
        min_power_toggle=float(defaults.get("min_power_toggle_minutes", 10)) * 60,
        manage_setpoints=bool(defaults.get("manage_setpoints", True)),
        groups=groups,
        rooms=rooms,
    )


# ---------------------------------------------------------------------------
# Control logic


@dataclass
class RoomState:
    running_for: AcMode | None = None  # why *we* have the unit on (HEAT or COOL)
    last_power_change: float | None = None  # monotonic timestamp


@dataclass
class GroupState:
    desired_mode: AcMode | None = None
    last_mode_change: float | None = None
    rooms: dict[str, RoomState] = field(default_factory=dict)


class GroupController:
    """Applies the control policy to one master + its member units."""

    def __init__(
        self,
        cfg: Config,
        group: GroupConfig,
        units: dict[str, AirConditioner],
    ) -> None:
        self._cfg = cfg
        self._group = group
        self._units = units  # name -> AirConditioner, for all group members
        self._state = GroupState(
            rooms={name: RoomState() for name in group.members}
        )
        self._adopt_current_state()

    def _adopt_current_state(self) -> None:
        """Take over whatever the system is doing right now without a jolt."""
        master = self._units[self._group.master]
        if master.selected_mode in (AcMode.HEAT, AcMode.COOL):
            self._state.desired_mode = master.selected_mode
        for name, unit in self._units.items():
            if unit.power_state in ON_STATES and self._state.desired_mode:
                self._state.rooms[name].running_for = self._state.desired_mode

    # -- demand ------------------------------------------------------------

    def _wants(self, name: str, mode: AcMode) -> bool:
        """Whether a room demands the given mode, with hysteresis.

        A room starts demanding when it crosses its range boundary and keeps
        demanding until it has moved `hysteresis` past the boundary.
        """
        unit = self._units[name]
        temp = unit.current_temperature
        if temp is None:
            return False
        room = self._cfg.room(name)
        running = self._state.rooms[name].running_for == mode
        if mode is AcMode.HEAT:
            threshold = room.target_low + (self._cfg.hysteresis if running else 0.0)
            return temp < threshold
        threshold = room.target_high - (self._cfg.hysteresis if running else 0.0)
        return temp > threshold

    def _exceeds_buffer(self, name: str, mode: AcMode) -> bool:
        """Whether a room is past its range by more than the mode-switch buffer."""
        unit = self._units[name]
        temp = unit.current_temperature
        if temp is None:
            return False
        room = self._cfg.room(name)
        if mode is AcMode.HEAT:
            return temp < room.target_low - self._cfg.mode_switch_buffer
        return temp > room.target_high + self._cfg.mode_switch_buffer

    # -- mode selection ----------------------------------------------------

    def _select_mode(self, now: float) -> AcMode:
        state = self._state
        heat_rooms = [n for n in self._group.members if self._wants(n, AcMode.HEAT)]
        cool_rooms = [n for n in self._group.members if self._wants(n, AcMode.COOL)]

        if state.desired_mode is None:
            # First run with the master in a non-heat/cool mode: pick whichever
            # side has demand (heat wins a tie — this is a passive house, ties
            # are rare and heating is the safer default).
            state.desired_mode = AcMode.COOL if cool_rooms and not heat_rooms else AcMode.HEAT
            state.last_mode_change = now
            return state.desired_mode

        current = state.desired_mode
        opposite = AcMode.COOL if current is AcMode.HEAT else AcMode.HEAT
        demand_current = heat_rooms if current is AcMode.HEAT else cool_rooms
        demand_opposite = cool_rooms if current is AcMode.HEAT else heat_rooms

        if not demand_opposite or demand_current:
            return current

        past_buffer = [n for n in demand_opposite if self._exceeds_buffer(n, opposite)]
        if not past_buffer:
            return current
        if (
            state.last_mode_change is not None
            and now - state.last_mode_change < self._cfg.min_mode_dwell
        ):
            remaining = self._cfg.min_mode_dwell - (now - state.last_mode_change)
            _LOGGER.info(
                "[%s] would switch to %s (%s past buffer) but mode dwell has %dm left",
                self._group.name, opposite.name, ", ".join(past_buffer), remaining // 60,
            )
            return current

        _LOGGER.info(
            "[%s] switching mode %s -> %s (demand from: %s)",
            self._group.name, current.name, opposite.name, ", ".join(past_buffer),
        )
        state.desired_mode = opposite
        state.last_mode_change = now
        return opposite

    # -- actuation ---------------------------------------------------------

    async def _send(self, description: str, coro) -> None:
        if self._cfg.dry_run:
            _LOGGER.info("DRY RUN: %s", description)
            coro.close()
            return
        _LOGGER.info("%s", description)
        await coro
        # Small gap between consecutive commands to be gentle on the console.
        await asyncio.sleep(0.5)

    async def _apply_setpoint(self, name: str, mode: AcMode) -> None:
        unit = self._units[name]
        room = self._cfg.room(name)
        if mode is AcMode.HEAT:
            target = room.target_low + self._cfg.hysteresis
        else:
            target = room.target_high - self._cfg.hysteresis
        target = max(unit.min_target_temperature, min(unit.max_target_temperature, target))
        resolution = unit.target_temperature_resolution or 0.5
        if (
            unit.target_temperature is not None
            and abs(unit.target_temperature - target) < resolution / 2
        ):
            return
        await self._send(
            f"[{self._group.name}] {name}: setpoint -> {target:.1f}",
            unit.set_target_temperature(target),
        )

    async def tick(self, now: float) -> None:
        mode = self._select_mode(now)
        master = self._units[self._group.master]

        # Mode goes to the master only, and never powers it on.
        if master.selected_mode is not mode:
            await self._send(
                f"[{self._group.name}] master {self._group.master}: "
                f"mode {master.selected_mode.name if master.selected_mode else '?'} "
                f"-> {mode.name} (unit stays {master.power_state.name if master.power_state else '?'})",
                master.set_mode(mode, power_on=False),
            )

        for name in self._group.members:
            await self._tick_room(name, mode, now)

    async def _tick_room(self, name: str, mode: AcMode, now: float) -> None:
        unit = self._units[name]
        room_state = self._state.rooms[name]
        temp = unit.current_temperature
        if temp is None:
            _LOGGER.warning("[%s] %s reports no temperature; leaving it alone",
                            self._group.name, name)
            return

        should_run = self._wants(name, mode)
        is_on = unit.power_state in ON_STATES

        if should_run and not room_state.running_for:
            room_state.running_for = mode
        elif not should_run and room_state.running_for:
            room_state.running_for = None

        if should_run == is_on:
            if should_run and self._cfg.manage_setpoints:
                await self._apply_setpoint(name, mode)
            return

        if (
            room_state.last_power_change is not None
            and now - room_state.last_power_change < self._cfg.min_power_toggle
        ):
            return  # anti short-cycling: too soon since the last toggle

        room = self._cfg.room(name)
        if should_run:
            if self._cfg.manage_setpoints:
                await self._apply_setpoint(name, mode)
            await self._send(
                f"[{self._group.name}] {name}: ON to {mode.name} "
                f"({temp:.1f}°C, range {room.target_low:.1f}-{room.target_high:.1f})",
                unit.set_power(AcPowerControl.TURN_ON),
            )
        else:
            await self._send(
                f"[{self._group.name}] {name}: OFF, satisfied "
                f"({temp:.1f}°C, range {room.target_low:.1f}-{room.target_high:.1f})",
                unit.set_power(AcPowerControl.TURN_OFF),
            )
        room_state.last_power_change = now

    def status_line(self) -> str:
        parts = []
        for name in self._group.members:
            unit = self._units[name]
            temp = unit.current_temperature
            on = unit.power_state in ON_STATES
            parts.append(
                f"{name}={temp:.1f}°C/{'ON' if on else 'off'}"
                if temp is not None
                else f"{name}=?/{'ON' if on else 'off'}"
            )
        mode = self._state.desired_mode.name if self._state.desired_mode else "?"
        return f"[{self._group.name}:{mode}] " + " ".join(parts)


# ---------------------------------------------------------------------------
# Service wrapper: connect, control loop, reconnect


class ClimateService:
    def __init__(self, cfg: Config) -> None:
        self._cfg = cfg
        self._stop = asyncio.Event()

    def request_stop(self) -> None:
        self._stop.set()

    async def _connect(self) -> tuple[pyairtouch.AirTouch, list[GroupController]]:
        discovered = await pyairtouch.discover(remote_host=self._cfg.host or None)
        if not discovered:
            raise ConnectionError(f"no AirTouch discovered at {self._cfg.host or 'broadcast'}")
        airtouch = discovered[0]
        if not await airtouch.init():
            raise ConnectionError(f"failed to initialise {airtouch.host}")

        units = {ac.name: ac for ac in airtouch.air_conditioners}
        missing = [
            name
            for group in self._cfg.groups
            for name in group.members
            if name not in units
        ]
        if missing:
            await airtouch.shutdown()
            raise ValueError(
                f"config names units not present on the AirTouch: {missing}; "
                f"available: {sorted(units)}"
            )

        controllers = [
            GroupController(self._cfg, group, {n: units[n] for n in group.members})
            for group in self._cfg.groups
        ]
        _LOGGER.info(
            "Connected to %s (%s), controlling %d units in %d groups%s",
            airtouch.name, airtouch.host,
            sum(len(g.members) for g in self._cfg.groups), len(self._cfg.groups),
            " [DRY RUN]" if self._cfg.dry_run else "",
        )
        return airtouch, controllers

    async def run(self, *, once: bool = False) -> None:
        backoff = 5.0
        while not self._stop.is_set():
            try:
                airtouch, controllers = await self._connect()
            except (ConnectionError, OSError, asyncio.TimeoutError) as exc:
                _LOGGER.error("Connection failed: %s — retrying in %.0fs", exc, backoff)
                await self._sleep(backoff)
                backoff = min(backoff * 2, 300)
                continue

            backoff = 5.0
            try:
                await self._control_loop(airtouch, controllers, once=once)
                if once or self._stop.is_set():
                    return
            except (ConnectionError, OSError, asyncio.TimeoutError) as exc:
                _LOGGER.error("Lost connection: %s — reconnecting", exc)
            finally:
                await airtouch.shutdown()

    async def _control_loop(
        self,
        airtouch: pyairtouch.AirTouch,
        controllers: list[GroupController],
        *,
        once: bool,
    ) -> None:
        last_status = ""
        while not self._stop.is_set():
            if not airtouch.initialised:
                raise ConnectionError("AirTouch connection is no longer initialised")

            now = time.monotonic()
            for controller in controllers:
                await controller.tick(now)

            status = " | ".join(c.status_line() for c in controllers)
            if status != last_status:
                _LOGGER.info("%s", status)
                last_status = status
            else:
                _LOGGER.debug("%s", status)

            if once:
                return
            await self._sleep(self._cfg.poll_interval)

    async def _sleep(self, seconds: float) -> None:
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=seconds)
        except TimeoutError:
            pass


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument(
        "--config", type=Path, default=Path(__file__).parent / "config.toml",
        help="path to the TOML config (default: config.toml beside this script)",
    )
    parser.add_argument("--dry-run", action="store_true",
                        help="log intended commands without sending them")
    parser.add_argument("--once", action="store_true",
                        help="run a single control pass and exit")
    parser.add_argument("--verbose", action="store_true", help="debug logging")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        stream=sys.stdout,
    )

    cfg = load_config(args.config)
    if args.dry_run:
        cfg = Config(**{**cfg.__dict__, "dry_run": True})

    service = ClimateService(cfg)
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, service.request_stop)

    await service.run(once=args.once)
    _LOGGER.info("Stopped.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
