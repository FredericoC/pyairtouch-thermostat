"""Whole-house climate control service for a Polyaire AirTouch 5.

Keeps every room inside a configured temperature range by turning individual
AC units on and off. Two units (the group "masters") dictate whether their
group heats or cools: the masters get mode commands every pass — without
powering them on — and member units are aligned to the master's mode before
any setpoint command (setpoints apply to the unit's selected mode). All units
(masters included) are powered on/off purely on their own room's demand.

Heat/cool mode switching is deliberately sticky: a group only flips mode when
no room still demands the current mode, at least one room demands the opposite
mode, and `min_mode_dwell_minutes` has elapsed since the last flip.

Demand itself is debounced: a change in what a room asks for must hold for
`demand_persist_polls` consecutive polls before it drives mode or power
decisions, so a single glitched console sample can't flip a group's mode
(which then sticks for the dwell time) or toggle a unit.

Optional `[shutdown]` windows (e.g. "21:00-07:00", local time) switch every
unit off for night or away periods: when a window starts, a single off pass
turns everything off and the normal control policy is suspended for the rest
of the window. A unit switched on manually during the window is left alone.

Run with:
    python climate_service.py                 # uses ./config.toml
    python climate_service.py --dry-run       # log decisions, send nothing
    python climate_service.py --once          # single control pass, then exit
"""

import argparse
import asyncio
import logging
import logging.handlers
import math
import signal
import sqlite3
import sys
import time
import tomllib
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import pyairtouch
from pyairtouch import AcMode, AcPowerControl, AcPowerState, AirConditioner

_LOGGER = logging.getLogger("climate")

ON_STATES = frozenset({AcPowerState.ON, AcPowerState.ON_AWAY, AcPowerState.SLEEP})

STATUS_HEARTBEAT = 15 * 60  # seconds between full status logs when nothing changes

LOG_FORMAT = "%(asctime)s %(levelname)-7s %(message)s"
LOG_DATEFMT = "%Y-%m-%d %H:%M:%S"


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
    demand_persist_polls: int  # consecutive polls before a demand change is real
    min_mode_dwell: float  # seconds
    min_power_toggle: float  # seconds
    manage_setpoints: bool
    setpoint_boost: float  # °C past the off threshold to push unit setpoints
    history_path: Path | None  # None = history recording disabled
    history_interval: float  # seconds
    weather_port: int | None  # None = Ecowitt weather listener disabled
    weather_path: str
    log_path: Path | None  # None = file logging disabled (stdout only)
    shutdown_windows: tuple[tuple[int, int], ...]  # (start, end) minutes past midnight
    groups: tuple[GroupConfig, ...]
    rooms: dict[str, RoomConfig]

    def room(self, name: str) -> RoomConfig:
        return self.rooms[name]

    def shutdown_active(self, local_now: datetime) -> bool:
        minute = local_now.hour * 60 + local_now.minute
        for start, end in self.shutdown_windows:
            if start < end:
                if start <= minute < end:
                    return True
            elif minute >= start or minute < end:  # window crosses midnight
                return True
        return False


def _parse_time_of_day(text: str, *, allow_2400: bool = False) -> int:
    """Parse 'HH:MM' into minutes past midnight."""
    hh, sep, mm = text.strip().partition(":")
    if not sep or not hh.isdigit() or not mm.isdigit() or len(mm) != 2 or int(mm) > 59:
        raise ValueError(f"invalid time of day {text!r} (expected HH:MM)")
    minutes = int(hh) * 60 + int(mm)
    if minutes > 24 * 60 or (minutes == 24 * 60 and not allow_2400):
        raise ValueError(f"invalid time of day {text!r}")
    return minutes


def _parse_shutdown_window(spec: str) -> tuple[int, int]:
    start_s, sep, end_s = spec.partition("-")
    if not sep:
        raise ValueError(
            f"invalid shutdown window {spec!r} (expected 'HH:MM-HH:MM')"
        )
    start = _parse_time_of_day(start_s)
    end = _parse_time_of_day(end_s, allow_2400=True)
    if start == end:
        # Zero length is surely a mistake — for all-day use "00:00-24:00".
        raise ValueError(f"shutdown window {spec!r} has zero length")
    return start, end


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
    hysteresis = float(defaults.get("hysteresis", 0.4))
    for name, cfg in rooms.items():
        if cfg.target_low >= cfg.target_high:
            raise ValueError(f"room {name!r}: target_low must be < target_high")
        if cfg.target_high - cfg.target_low <= 2 * hysteresis:
            raise ValueError(
                f"room {name!r}: range {cfg.target_low}–{cfg.target_high} is too "
                f"narrow — it must be wider than 2 × hysteresis "
                f"({2 * hysteresis}), or the heating-off threshold "
                f"({cfg.target_low + hysteresis}) would overlap the cooling-off "
                f"threshold ({cfg.target_high - hysteresis})"
            )

    shutdown = raw.get("shutdown", {})
    shutdown_windows: tuple[tuple[int, int], ...] = ()
    if shutdown.get("enabled", True):
        specs = shutdown.get("windows", [])
        if not isinstance(specs, list) or not all(isinstance(s, str) for s in specs):
            raise ValueError("[shutdown] windows must be a list of 'HH:MM-HH:MM' strings")
        shutdown_windows = tuple(_parse_shutdown_window(s) for s in specs)

    history = raw.get("history", {})
    history_path: Path | None = None
    if history.get("enabled", True):
        history_path = Path(history.get("path", "history.db"))
        if not history_path.is_absolute():
            # Relative paths are resolved against the config file so the
            # database lands in the repo regardless of the working directory.
            history_path = path.parent / history_path

    weather = raw.get("weather", {})
    weather_port: int | None = None
    if weather.get("enabled", True):
        weather_port = int(weather.get("port", 8090))

    log_file = str(service.get("log_file", "climate.log"))
    log_path: Path | None = None
    if log_file:
        log_path = Path(log_file)
        if not log_path.is_absolute():
            log_path = path.parent / log_path

    return Config(
        host=service.get("host", ""),
        poll_interval=float(service.get("poll_interval_seconds", 30)),
        dry_run=bool(service.get("dry_run", False)),
        hysteresis=hysteresis,
        demand_persist_polls=max(1, int(defaults.get("demand_persist_polls", 2))),
        min_mode_dwell=float(defaults.get("min_mode_dwell_minutes", 60)) * 60,
        min_power_toggle=float(defaults.get("min_power_toggle_minutes", 10)) * 60,
        manage_setpoints=bool(defaults.get("manage_setpoints", True)),
        setpoint_boost=float(defaults.get("setpoint_boost", 0.0)),
        history_path=history_path,
        history_interval=float(history.get("interval_seconds", 60)),
        weather_port=weather_port,
        weather_path=str(weather.get("path", "/data/report/")),
        log_path=log_path,
        shutdown_windows=shutdown_windows,
        groups=groups,
        rooms=rooms,
    )


# ---------------------------------------------------------------------------
# Control logic


@dataclass
class RoomState:
    running_for: AcMode | None = None  # why *we* have the unit on (HEAT or COOL)
    last_power_change: float | None = None  # monotonic timestamp
    demand: AcMode | None = None  # debounced demand (drives mode and power)
    demand_candidate: AcMode | None = None  # raw demand awaiting confirmation
    demand_streak: int = 0  # consecutive polls the candidate has held


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
        # Seed the debounced demand from the current readings so the first
        # pass acts on them (an adopted running unit must not be switched off
        # as "satisfied" just because its demand hasn't been confirmed yet).
        for name in self._group.members:
            self._state.rooms[name].demand = self._raw_demand(name)

    # -- demand ------------------------------------------------------------

    def _raw_demand(self, name: str) -> AcMode | None:
        """This poll's instantaneous demand for a room, with hysteresis.

        A room starts demanding when it crosses its range boundary and keeps
        demanding until it has moved `hysteresis` past the boundary. The two
        thresholds can't overlap (the config loader enforces a wide-enough
        range), so at most one mode is demanded.
        """
        unit = self._units[name]
        temp = unit.current_temperature
        if temp is None:
            return None
        room = self._cfg.room(name)
        running = self._state.rooms[name].running_for
        low = room.target_low + (self._cfg.hysteresis if running is AcMode.HEAT else 0.0)
        if temp < low:
            return AcMode.HEAT
        high = room.target_high - (self._cfg.hysteresis if running is AcMode.COOL else 0.0)
        if temp > high:
            return AcMode.COOL
        return None

    def _update_demands(self) -> None:
        """Debounce each room's demand; call once at the start of every pass.

        A single glitched sample (the console has produced spurious readings,
        e.g. three rooms reporting exactly 25.0°C for one poll) must not flip
        a group's mode — which then sticks for `min_mode_dwell_minutes` — or
        toggle a unit's power. A change in a room's demand only takes effect
        once the same raw demand has held for `demand_persist_polls`
        consecutive polls.
        """
        for name in self._group.members:
            room_state = self._state.rooms[name]
            raw = self._raw_demand(name)
            if raw == room_state.demand:
                if room_state.demand_candidate is not None or room_state.demand_streak:
                    _LOGGER.info(
                        "[%s] %s: transient reading passed, demand stays %s",
                        self._group.name, name,
                        room_state.demand.name if room_state.demand else "none",
                    )
                room_state.demand_candidate = None
                room_state.demand_streak = 0
                continue
            if raw == room_state.demand_candidate:
                room_state.demand_streak += 1
            else:
                room_state.demand_candidate = raw
                room_state.demand_streak = 1
            if room_state.demand_streak >= self._cfg.demand_persist_polls:
                room_state.demand = raw
                room_state.demand_candidate = None
                room_state.demand_streak = 0
            elif room_state.demand_streak == 1:
                unit = self._units[name]
                temp = unit.current_temperature
                _LOGGER.info(
                    "[%s] %s: %s suggests demand %s -> %s; waiting for it to "
                    "persist (%d more poll%s)",
                    self._group.name, name,
                    f"{temp:.1f}°C" if temp is not None else "no reading",
                    room_state.demand.name if room_state.demand else "none",
                    raw.name if raw else "none",
                    self._cfg.demand_persist_polls - 1,
                    "" if self._cfg.demand_persist_polls == 2 else "s",
                )

    def _wants(self, name: str, mode: AcMode) -> bool:
        """Whether a room's debounced demand is for the given mode."""
        return self._state.rooms[name].demand is mode

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

        if (
            state.last_mode_change is not None
            and now - state.last_mode_change < self._cfg.min_mode_dwell
        ):
            remaining = self._cfg.min_mode_dwell - (now - state.last_mode_change)
            _LOGGER.info(
                "[%s] would switch to %s (for %s) but mode dwell has %dm left",
                self._group.name, opposite.name, ", ".join(demand_opposite),
                remaining // 60,
            )
            return current

        _LOGGER.info(
            "[%s] switching mode %s -> %s (demand from: %s)",
            self._group.name, current.name, opposite.name, ", ".join(demand_opposite),
        )
        state.desired_mode = opposite
        state.last_mode_change = now
        return opposite

    # -- actuation ---------------------------------------------------------

    async def _send(self, description: str, coro) -> None:
        if self._cfg.dry_run:
            _LOGGER.info(">> CMD (dry-run) %s", description)
            coro.close()
            return
        _LOGGER.info(">> CMD %s", description)
        await coro
        # Small gap between consecutive commands to be gentle on the console.
        await asyncio.sleep(0.5)

    async def _apply_setpoint(self, name: str, mode: AcMode) -> None:
        unit = self._units[name]
        room = self._cfg.room(name)
        # Whole-degree setpoints only (fractional values may not be honoured by
        # the units). Round towards the demand side: up for heat, down for cool,
        # so the unit's internal thermostat can't idle short of our power-off
        # threshold (target_low/high ± hysteresis). setpoint_boost pushes the
        # setpoint further past the threshold: the unit modulates on its own
        # return-air sensor, which reads warm before the room sensor reaches
        # target, so without the boost it tapers to a trickle short of temp.
        # We power off on the room sensor, so the boost can't overheat the room.
        boost = self._cfg.setpoint_boost
        if mode is AcMode.HEAT:
            target = float(math.ceil(room.target_low + self._cfg.hysteresis + boost))
        else:
            target = float(math.floor(room.target_high - self._cfg.hysteresis - boost))
        await self._align_mode(name, mode)
        await self._send_setpoint(name, target)

    async def _apply_idle_setpoint(self, name: str, mode: AcMode, temp: float) -> None:
        # The room is satisfied but min_power_toggle_minutes is holding the
        # unit on. Park the setpoint at the room temperature — rounded down
        # for heat, up for cool — so the unit's own thermostat idles instead
        # of pushing more heat/cool into an already-satisfied room. The normal
        # boosted setpoint is restored on the next power-on (or if demand
        # returns while the unit is still on).
        if mode is AcMode.HEAT:
            target = float(math.floor(temp))
        else:
            target = float(math.ceil(temp))
        await self._align_mode(name, mode)
        await self._send_setpoint(name, target, note=" (idling out power-toggle hold)")

    async def _align_mode(self, name: str, mode: AcMode) -> None:
        # Setpoint commands apply to the unit's currently-selected mode, so a
        # member left in another mode (e.g. by the wall panel) would take our
        # setpoint on the wrong mode's target. Match it to the master's mode
        # first, never powering it on. The master itself is handled in tick(),
        # where its mode drives the whole group.
        if name == self._group.master:
            return
        unit = self._units[name]
        if unit.selected_mode is not mode:
            await self._send(
                f"[{self._group.name}] {name}: mode "
                f"{unit.selected_mode.name if unit.selected_mode else '?'} "
                f"→ {mode.name} (matching master)",
                unit.set_mode(mode, power_on=False),
            )

    async def _send_setpoint(self, name: str, target: float, note: str = "") -> None:
        unit = self._units[name]
        target = max(unit.min_target_temperature, min(unit.max_target_temperature, target))
        resolution = unit.target_temperature_resolution or 0.5
        current = unit.target_temperature
        if current is not None and abs(current - target) < resolution / 2:
            return
        current_s = f"{current:.1f}°C" if current is not None else "?"
        await self._send(
            f"[{self._group.name}] {name}: setpoint {current_s} → {target:.1f}°C{note}",
            unit.set_target_temperature(target),
        )

    async def enforce_shutdown(self, now: float) -> None:
        """Force every unit off, regardless of demand or anti-short-cycle timers.

        Called once when a shutdown window starts (not every poll), so a unit
        switched on manually during the window stays on until the control
        policy resumes at the end of the window.
        """
        for name in self._group.members:
            unit = self._units[name]
            room_state = self._state.rooms[name]
            room_state.running_for = None
            if unit.power_state in ON_STATES:
                await self._send(
                    f"[{self._group.name}] {name}: power → OFF (shutdown window)",
                    unit.set_power(AcPowerControl.TURN_OFF),
                )
                room_state.last_power_change = now

    async def tick(self, now: float) -> None:
        self._update_demands()
        mode = self._select_mode(now)
        master = self._units[self._group.master]

        # Mode goes to the master only, and never powers it on.
        if master.selected_mode is not mode:
            power = "on" if master.power_state in ON_STATES else "off"
            await self._send(
                f"[{self._group.name}] {self._group.master} (master): "
                f"mode {master.selected_mode.name if master.selected_mode else '?'} "
                f"→ {mode.name}",
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
            # Anti short-cycling: too soon since the last toggle. If the unit
            # is pending off, stop it heating/cooling the room in the meantime.
            if is_on and self._cfg.manage_setpoints:
                await self._apply_idle_setpoint(name, mode, temp)
            return

        room = self._cfg.room(name)
        if should_run:
            if self._cfg.manage_setpoints:
                await self._apply_setpoint(name, mode)
            if mode is AcMode.HEAT:
                reason = f"{temp:.1f}°C is below {room.target_low:.1f}°C"
            else:
                reason = f"{temp:.1f}°C is above {room.target_high:.1f}°C"
            await self._send(
                f"[{self._group.name}] {name}: power → ON, "
                f"{mode.name.lower()}ing ({reason})",
                unit.set_power(AcPowerControl.TURN_ON),
            )
        else:
            await self._send(
                f"[{self._group.name}] {name}: power → OFF, satisfied "
                f"({temp:.1f}°C, range {room.target_low:.1f}–{room.target_high:.1f})",
                unit.set_power(AcPowerControl.TURN_OFF),
            )
        room_state.last_power_change = now

    def history_rows(self, *, shutdown: bool = False) -> list[tuple]:
        """One (unit, temperature, setpoint, power, mode, activity) per member."""
        rows = []
        for name in self._group.members:
            unit = self._units[name]
            on = unit.power_state in ON_STATES
            running_for = self._state.rooms[name].running_for
            if on and running_for:
                activity = f"{running_for.name.lower()}ing"
            elif on and shutdown and unit.active_mode in (AcMode.HEAT, AcMode.COOL):
                # Switched on manually during a shutdown window: the control
                # policy isn't driving it, but the unit's own mode says what
                # it's doing. active_mode resolves AUTO to heat/cool.
                activity = f"{unit.active_mode.name.lower()}ing (manual)"
            elif on:
                activity = "on"
            else:
                activity = "idle"
            rows.append((
                name,
                unit.current_temperature,
                unit.target_temperature,
                1 if on else 0,
                unit.selected_mode.name if unit.selected_mode else None,
                activity,
            ))
        return rows

    def _mode_switch_blocker(self, now: float) -> str:
        """Which gate is currently stopping the group flipping modes."""
        mode = self._state.desired_mode
        holding = [m for m in self._group.members if self._wants(m, mode)]
        if holding:
            return f"blocked by {', '.join(holding)} still needing {mode.name}"
        last = self._state.last_mode_change
        if last is not None and now - last < self._cfg.min_mode_dwell:
            remaining = self._cfg.min_mode_dwell - (now - last)
            return f"mode dwell {int(remaining // 60)}m left"
        return "switching next pass"

    def status_report(
        self, now: float, *, shutdown: bool = False
    ) -> tuple[str, list[str]]:
        """Render the group's status as human-readable lines.

        Returns a (signature, lines) pair. The signature excludes temperatures
        and setpoints so the caller can re-log the status only when something
        meaningful changes (power, activity, mode) rather than on every 0.1°C
        drift.
        """
        mode = self._state.desired_mode
        opposite = AcMode.COOL if mode is AcMode.HEAT else AcMode.HEAT
        master = self._units[self._group.master]
        header = (
            f"[{self._group.name}] mode {mode.name if mode else '?'}   "
            f"master {self._group.master} "
            f"({'on' if master.power_state in ON_STATES else 'off'})"
        )
        lines = [header]
        sig_parts = [header]
        name_width = max(len(n) for n in self._group.members)
        for name in self._group.members:
            unit = self._units[name]
            room = self._cfg.room(name)
            temp = unit.current_temperature
            on = unit.power_state in ON_STATES
            running_for = self._state.rooms[name].running_for

            detail = ""
            if shutdown:
                activity = "shutdown" if not on else "on manually (shutdown window)"
            elif on and running_for:
                activity = f"{running_for.name.lower()}ing"
                if running_for is AcMode.HEAT:
                    threshold = room.target_low + self._cfg.hysteresis
                else:
                    threshold = room.target_high - self._cfg.hysteresis
                detail = f" to {threshold:.1f}°C"
                setpoint = unit.target_temperature
                if setpoint is not None:
                    detail += f" (unit set to {setpoint:.1f}°C)"
            elif on:
                activity = "on, pending off"
            elif mode and self._wants(name, mode):
                activity = "pending on"
            elif mode and self._wants(name, opposite):
                activity = f"needs {opposite.name}, waiting for mode switch"
                detail = f" ({self._mode_switch_blocker(now)})"
            else:
                activity = "in range"

            temp_s = f"{temp:5.1f}°C" if temp is not None else "    ?°C"
            lines.append(
                f"  {name:<{name_width}}  {temp_s}  "
                f"[{room.target_low:.1f}–{room.target_high:.1f}]  "
                f"{'ON ' if on else 'off'}  {activity}{detail}"
            )
            sig_parts.append(f"{name}:{on}:{activity}")
        return "|".join(sig_parts), lines


# ---------------------------------------------------------------------------
# Weather (Ecowitt customized upload)


WEATHER_STALE_AFTER = 5 * 60  # seconds without a fresh value before it's dropped


class WeatherStation:
    """Receives Ecowitt customized-upload reports and exposes the latest values.

    The HP2551 console has no pollable API — it HTTP-POSTs all sensor values
    to a configured server on a fixed interval (see ECOWITT.md). This embeds
    that server (aioecowitt) in the service so outdoor conditions land in the
    history database alongside the unit samples.
    """

    def __init__(self, port: int, path: str) -> None:
        self._port = port
        self._path = path
        self._listener = None

    async def start(self) -> None:
        from aioecowitt import EcoWittListener

        # One console report per minute would otherwise add an aiohttp access
        # log line to the service log each time.
        logging.getLogger("aiohttp.access").setLevel(logging.WARNING)
        listener = EcoWittListener(port=self._port, path=self._path)
        try:
            await listener.start()
        except OSError as exc:
            _LOGGER.error(
                "Ecowitt listener failed to start on port %d (%s) — weather "
                "recording disabled. Is the standalone ecowitt_listener.py "
                "still running?", self._port, exc,
            )
            return
        self._listener = listener
        _LOGGER.info("Ecowitt listener on port %d (path %s)", self._port, self._path)

    async def stop(self) -> None:
        if self._listener is not None:
            await self._listener.stop()
            self._listener = None

    def sample(self, now: float) -> tuple[float | None, float | None] | None:
        """Latest (outdoor °C, solar W/m²), or None when there's nothing fresh.

        Stale values (console offline, sensor dropout) become None so the
        charts show a gap instead of a flat line.
        """
        temp = self._fresh_value("tempc", now)
        solar = self._fresh_value("solarradiation", now)
        if temp is None and solar is None:
            return None
        return temp, solar

    def _fresh_value(self, key: str, now: float) -> float | None:
        if self._listener is None:
            return None
        for sensor in self._listener.sensors.values():
            if (
                sensor.key == key
                and sensor.value is not None
                and now - sensor.last_update_m <= WEATHER_STALE_AFTER
            ):
                return float(sensor.value)
        return None


# ---------------------------------------------------------------------------
# Temperature history


class HistoryRecorder:
    """Appends periodic per-unit samples to a SQLite database.

    One row per unit per sample: enough to plot temperature over time for a
    single unit or all units together, and to overlay power/mode activity.
    """

    _SCHEMA = """
        CREATE TABLE IF NOT EXISTS readings (
            ts          INTEGER NOT NULL,  -- unix epoch seconds (UTC)
            unit        TEXT    NOT NULL,
            temperature REAL,              -- NULL if the unit reports none
            setpoint    REAL,
            power       INTEGER NOT NULL,  -- 1 = on, 0 = off
            mode        TEXT,              -- unit's selected mode (HEAT/COOL/...)
            activity    TEXT NOT NULL      -- heating / cooling / on / idle
        );
        CREATE INDEX IF NOT EXISTS idx_readings_unit_ts ON readings (unit, ts);
        CREATE INDEX IF NOT EXISTS idx_readings_ts ON readings (ts);
        CREATE TABLE IF NOT EXISTS weather (
            ts           INTEGER NOT NULL,  -- unix epoch seconds (UTC)
            outdoor_temp REAL,              -- °C, NULL if missing/stale
            solar        REAL               -- W/m², NULL if missing/stale
        );
        CREATE INDEX IF NOT EXISTS idx_weather_ts ON weather (ts);
    """

    def __init__(self, path: Path, interval: float) -> None:
        self._interval = interval
        self._last_sample = 0.0  # monotonic; 0 so the first tick records
        self._conn = sqlite3.connect(path)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(self._SCHEMA)
        self._conn.commit()
        _LOGGER.info("Recording temperature history to %s every %.0fs",
                     path, interval)

    def maybe_record(
        self,
        now: float,
        controllers: list["GroupController"],
        weather: tuple[float | None, float | None] | None = None,
        *,
        shutdown: bool = False,
    ) -> None:
        if now - self._last_sample < self._interval:
            return
        self._last_sample = now
        ts = int(time.time())
        rows = [row for c in controllers for row in c.history_rows(shutdown=shutdown)]
        self._conn.executemany(
            "INSERT INTO readings VALUES (?, ?, ?, ?, ?, ?, ?)",
            [(ts, *row) for row in rows],
        )
        if weather is not None:
            self._conn.execute("INSERT INTO weather VALUES (?, ?, ?)", (ts, *weather))
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()


# ---------------------------------------------------------------------------
# Service wrapper: connect, control loop, reconnect


class ClimateService:
    def __init__(self, cfg: Config) -> None:
        self._cfg = cfg
        self._stop = asyncio.Event()
        # True once the off pass for the current shutdown window has been sent;
        # lives on the service so a reconnect mid-window does not repeat the
        # pass and override manual changes.
        self._was_shutdown = False
        self._history: HistoryRecorder | None = None
        if cfg.history_path is not None:
            self._history = HistoryRecorder(cfg.history_path, cfg.history_interval)
        self._weather: WeatherStation | None = None
        if cfg.weather_port is not None:
            self._weather = WeatherStation(cfg.weather_port, cfg.weather_path)

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
        # The weather listener lives outside the connect/reconnect loop: it
        # keeps receiving console reports while the AirTouch is unreachable.
        if self._weather:
            await self._weather.start()
        try:
            await self._run(once=once)
        finally:
            if self._weather:
                await self._weather.stop()
            if self._history:
                self._history.close()

    async def _run(self, *, once: bool) -> None:
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
        last_signature = ""
        last_logged = 0.0
        while not self._stop.is_set():
            if not airtouch.initialised:
                raise ConnectionError("AirTouch connection is no longer initialised")

            now = time.monotonic()
            shutdown = self._cfg.shutdown_active(datetime.now())
            for controller in controllers:
                if shutdown:
                    # One off pass at window start only; manual changes made
                    # during the window are left alone.
                    if not self._was_shutdown:
                        await controller.enforce_shutdown(now)
                else:
                    await controller.tick(now)
            self._was_shutdown = shutdown

            if self._history:
                self._history.maybe_record(
                    now, controllers,
                    self._weather.sample(now) if self._weather else None,
                    shutdown=shutdown,
                )

            signatures, lines = [], []
            if shutdown:
                lines.append("SHUTDOWN window active — units switched off at "
                             "window start; manual changes are respected")
                signatures.append("shutdown")
            for controller in controllers:
                sig, group_lines = controller.status_report(now, shutdown=shutdown)
                signatures.append(sig)
                lines.extend(group_lines)
            signature = "||".join(signatures)
            status = "\n".join(lines)

            # Log the status block whenever something meaningful changed, and
            # at least every STATUS_HEARTBEAT as a sign of life.
            if signature != last_signature or now - last_logged >= STATUS_HEARTBEAT:
                _LOGGER.info("status:\n%s", status)
                last_signature = signature
                last_logged = now
            else:
                _LOGGER.debug("status:\n%s", status)

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
        format=LOG_FORMAT,
        datefmt=LOG_DATEFMT,
        stream=sys.stdout,
    )
    if not args.verbose:
        # The library logs protocol details we don't need in the service log.
        logging.getLogger("pyairtouch").setLevel(logging.WARNING)

    cfg = load_config(args.config)
    if cfg.log_path is not None:
        # Mirror the log to a file so the web dashboard can tail it. Rotation
        # keeps the footprint small (the Pi runs off an SD card); the webui
        # only reads the current file, so backups exist purely as history.
        file_handler = logging.handlers.RotatingFileHandler(
            cfg.log_path, maxBytes=1_000_000, backupCount=2, encoding="utf-8"
        )
        file_handler.setFormatter(logging.Formatter(LOG_FORMAT, LOG_DATEFMT))
        logging.getLogger().addHandler(file_handler)
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
