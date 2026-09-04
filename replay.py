#!/usr/bin/env python3
"""Replay recorded temperatures through the climate control policy, offline.

Feeds the room temperatures recorded in history.db (or an exported fixture)
to the real GroupController policy, with fake units that apply commands
instantly. Two uses:

- regression: the committed fixture replay must keep producing the same
  decision sequence (tests/test_replay_regression.py);
- backtesting: compare what alternate tunings would have decided, e.g.
  --variant "hyst6:hysteresis=0.6" --variant "dwell90:min_mode_dwell_minutes=90"

CAVEAT: the recorded temperatures are the *result* of the real config's
conditioning. A variant replay answers "what would the controller have
decided at each moment, seeing these temperatures" — mode-flip counts,
compressor gating and short-cycle proximity are directionally meaningful,
but under a different config the rooms would have followed different
trajectories, so temperature outcomes are not predictions. No thermal model
is attempted. Dashboard overrides/pauses were never recorded and are not
replayed; manual power/mode changes present in the recording are likewise
invisible to the replayed controller, which owns its units' state.

Examples:
    python replay.py --db history.db --from 2026-07-12 --to 2026-07-15
    python replay.py --db history.db --variant "hyst6:hysteresis=0.6"
    python replay.py --csv tests/fixtures/replay_2026-07-12_3d.csv.gz
    python replay.py --db history.db --from 2026-07-12 --to 2026-07-15 \\
        --export tests/fixtures/replay_2026-07-12_3d.csv.gz
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import dataclasses
import gzip
import io
import logging
import sqlite3
import sys
from bisect import bisect_right
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from pyairtouch import AcFanSpeed, AcMode, AcPowerControl, AcPowerState

import climate_service
from climate_service import Config, GroupController, ON_STATES, load_config

DEFAULT_TZ = "Australia/Brisbane"  # the household's timezone (no DST)

_MODE_BY_NAME = {m.name: m for m in AcMode}


class ReplayUnit:
    """Satisfies the pyairtouch AirConditioner protocol for replay.

    Commands mutate state immediately (the echo the live service eventually
    gets from the console) and append to the shared event log stamped with
    the replay clock.
    """

    def __init__(self, name: str, events: list, clock: dict) -> None:
        self.name = name
        self.current_temperature: float | None = None
        self.target_temperature: float | None = None
        self.power_state = AcPowerState.OFF
        self.selected_mode: AcMode | None = None
        self.active_mode: AcMode | None = None
        # Fan speed isn't recorded in history.db; assume the house's AUTO
        # (a variant that sets fan speeds will show the first-run command).
        self.selected_fan_speed: AcFanSpeed | None = AcFanSpeed.AUTO
        self.active_fan_speed: AcFanSpeed | None = AcFanSpeed.AUTO
        self.supported_fan_speeds = (
            AcFanSpeed.AUTO, AcFanSpeed.QUIET, AcFanSpeed.LOW,
            AcFanSpeed.MEDIUM, AcFanSpeed.HIGH, AcFanSpeed.POWERFUL,
        )
        self.min_target_temperature = 16.0
        self.max_target_temperature = 31.0
        self.target_temperature_resolution: float | None = 1.0
        self._events = events
        self._clock = clock

    def _log(self, verb: str, value: str) -> None:
        self._events.append((self._clock["now"], self.name, verb, value))

    async def set_fan_speed(self, fan_speed: AcFanSpeed) -> None:
        self._log("fan", fan_speed.name)
        self.selected_fan_speed = fan_speed
        self.active_fan_speed = fan_speed

    async def set_power(self, power_control: AcPowerControl) -> None:
        self._log("power", power_control.name)
        if power_control is AcPowerControl.TURN_ON:
            self.power_state = AcPowerState.ON
        elif power_control is AcPowerControl.TURN_OFF:
            self.power_state = AcPowerState.OFF

    async def set_mode(self, mode: AcMode, *, power_on: bool = False) -> None:
        self._log("mode", mode.name)
        self.selected_mode = mode
        self.active_mode = mode
        if power_on:
            self.power_state = AcPowerState.ON

    async def set_target_temperature(self, temperature: float) -> None:
        self._log("setpoint", f"{temperature:.1f}")
        self.target_temperature = temperature


@dataclass
class GroupMetrics:
    mode_flips: int = 0
    compressor_starts: int = 0
    compressor_stops: int = 0
    shortest_on: float | None = None  # seconds
    shortest_off: float | None = None
    idle_parks: int = 0
    power_cmds: Counter = field(default_factory=Counter)  # "TURN_ON"/"TURN_OFF"
    # internal transition tracking
    _on: bool = False
    _since: float | None = None


@dataclass
class ReplayResult:
    label: str
    events: list  # (ts, unit, verb, value)
    metrics: dict[str, GroupMetrics]  # by group name
    ticks: int = 0
    gap_ticks: int = 0


# ---------------------------------------------------------------------------
# Feed loading: unit -> parallel arrays (ts, temp, setpoint, power, mode)

Feed = dict[str, tuple[list[int], list, list, list, list]]

_FEED_QUERY = (
    "SELECT ts, unit, temperature, setpoint, power, mode FROM readings"
    " WHERE ts >= ? AND ts < ? ORDER BY ts"
)


def _feed_from_rows(rows) -> Feed:
    feed: Feed = {}
    for ts, unit, temp, setpoint, power, mode in rows:
        cols = feed.setdefault(unit, ([], [], [], [], []))
        cols[0].append(int(ts))
        cols[1].append(temp)
        cols[2].append(setpoint)
        cols[3].append(int(power))
        cols[4].append(mode)
    return feed


def load_feed_db(db: Path, start_ts: float, end_ts: float) -> Feed:
    with sqlite3.connect(f"file:{db}?mode=ro", uri=True) as conn:
        # Reach a little before the window so the units can be seeded with
        # their state at the start instead of running blind until the first
        # in-window sample.
        rows = conn.execute(_FEED_QUERY, (int(start_ts) - 3600, int(end_ts))).fetchall()
    return _feed_from_rows(rows)


def load_feed_csv(path: Path) -> Feed:
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rt", encoding="utf-8", newline="") as f:
        rows = [
            (
                int(r["ts"]),
                r["unit"],
                float(r["temperature"]) if r["temperature"] else None,
                float(r["setpoint"]) if r["setpoint"] else None,
                int(r["power"]),
                r["mode"] or None,
            )
            for r in csv.DictReader(f)
        ]
    return _feed_from_rows(rows)


def export_fixture(db: Path, out: Path, start_ts: float, end_ts: float) -> int:
    with sqlite3.connect(f"file:{db}?mode=ro", uri=True) as conn:
        rows = conn.execute(_FEED_QUERY, (int(start_ts), int(end_ts))).fetchall()
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["ts", "unit", "temperature", "setpoint", "power", "mode"])
    writer.writerows(rows)
    data = buf.getvalue().encode()
    if out.suffix == ".gz":
        # mtime=0 so re-exporting identical data yields an identical file.
        out.write_bytes(gzip.compress(data, mtime=0))
    else:
        out.write_bytes(data)
    return len(rows)


# ---------------------------------------------------------------------------
# The replay itself


def run_replay(
    cfg: Config,
    feed: Feed,
    start_ts: float,
    end_ts: float,
    tz: ZoneInfo,
    *,
    no_shutdown: bool = False,
    label: str = "baseline",
) -> ReplayResult:
    return asyncio.run(_replay(cfg, feed, start_ts, end_ts, tz, no_shutdown, label))


async def _replay(
    cfg: Config,
    feed: Feed,
    start_ts: float,
    end_ts: float,
    tz: ZoneInfo,
    no_shutdown: bool,
    label: str,
) -> ReplayResult:
    climate_service.COMMAND_GAP = 0  # no sleeps between replayed commands
    events: list = []
    clock = {"now": start_ts}

    units_by_group: dict[str, dict[str, ReplayUnit]] = {}
    all_units: dict[str, ReplayUnit] = {}
    for group in cfg.groups:
        units = {}
        for name in group.members:
            if name not in feed or not feed[name][0]:
                raise SystemExit(f"no recorded data for unit {name!r} in the window")
            units[name] = ReplayUnit(name, events, clock)
        units_by_group[group.name] = units
        all_units.update(units)

    # Seed each unit with its recorded state at the window start so
    # _adopt_current_state matches what the live service saw.
    for name, unit in all_units.items():
        ts_list, temps, setpoints, powers, modes = feed[name]
        i = max(bisect_right(ts_list, start_ts) - 1, 0)
        unit.current_temperature = temps[i] if ts_list[i] <= start_ts else None
        unit.target_temperature = setpoints[i]
        unit.power_state = AcPowerState.ON if powers[i] else AcPowerState.OFF
        unit.selected_mode = _MODE_BY_NAME.get(modes[i] or "")
        unit.active_mode = unit.selected_mode

    controllers = {
        group.name: GroupController(cfg, group, units_by_group[group.name])
        for group in cfg.groups
    }

    metrics = {name: GroupMetrics() for name in controllers}
    prev_desired = {}
    for name, ctl in controllers.items():
        m = metrics[name]
        m._on = any(u.power_state in ON_STATES for u in units_by_group[name].values())
        prev_desired[name] = ctl._state.desired_mode

    # Count idle-setpoint parking (the pending-off behaviour) by wrapping the
    # controller method for the duration of this replay.
    original_idle = GroupController._apply_idle_setpoint

    async def counting_idle(self, name, mode, temp):
        metrics[self._group.name].idle_parks += 1
        await original_idle(self, name, mode, temp)

    GroupController._apply_idle_setpoint = counting_idle

    result = ReplayResult(label=label, events=events, metrics=metrics)
    stale_after = 2 * cfg.history_interval
    # Emulate a service (re)start at the window start: a window already in
    # progress is assumed to have had its off pass (see ClimateService).
    was_shutdown = (not no_shutdown) and cfg.shutdown_active(
        datetime.fromtimestamp(start_ts, tz)
    )

    try:
        now = float(start_ts)
        while now < end_ts:
            freshest = 0
            for name, unit in all_units.items():
                ts_list, temps, *_ = feed[name]
                i = bisect_right(ts_list, now) - 1
                if i >= 0:
                    unit.current_temperature = temps[i]
                    freshest = max(freshest, ts_list[i])

            shutdown = (not no_shutdown) and cfg.shutdown_active(
                datetime.fromtimestamp(now, tz)
            )
            if now - freshest > stale_after:
                # Recording gap = the service wasn't running. No ticks; the
                # shutdown latch reseeds on restart, so a window that began
                # during the gap gets no off pass — same as live.
                result.gap_ticks += 1
                was_shutdown = shutdown
                now += cfg.poll_interval
                continue

            clock["now"] = now
            if shutdown:
                if not was_shutdown:
                    for ctl in controllers.values():
                        await ctl.enforce_shutdown(now)
            else:
                for ctl in controllers.values():
                    await ctl.tick(now)
            was_shutdown = shutdown
            result.ticks += 1

            for name, ctl in controllers.items():
                m = metrics[name]
                desired = ctl._state.desired_mode
                if prev_desired[name] is not None and desired is not prev_desired[name]:
                    m.mode_flips += 1
                prev_desired[name] = desired

                on = any(
                    u.power_state in ON_STATES
                    for u in units_by_group[name].values()
                )
                if on != m._on:
                    duration = None if m._since is None else now - m._since
                    if on:
                        m.compressor_starts += 1
                        if duration is not None:
                            m.shortest_off = (
                                duration if m.shortest_off is None
                                else min(m.shortest_off, duration)
                            )
                    else:
                        m.compressor_stops += 1
                        if duration is not None:
                            m.shortest_on = (
                                duration if m.shortest_on is None
                                else min(m.shortest_on, duration)
                            )
                    m._on = on
                    m._since = now

            now += cfg.poll_interval
    finally:
        GroupController._apply_idle_setpoint = original_idle

    for ts, unit, verb, value in events:
        if verb == "power":
            group = next(g.name for g in cfg.groups if unit in g.members)
            metrics[group].power_cmds[value] += 1
    return result


def out_of_range_minutes(cfg: Config, feed: Feed, start_ts: float, end_ts: float):
    """Recorded comfort: minutes below/above each room's range in the window.

    A property of the recording (the baseline config's real outcome) — it is
    NOT recomputed for variants, whose temperature trajectories are unknown.
    """
    cap = 2 * cfg.history_interval
    result = {}
    for name, (ts_list, temps, *_rest) in feed.items():
        if name not in cfg.rooms:
            continue
        room = cfg.rooms[name]
        below = above = 0.0
        for k, ts in enumerate(ts_list):
            if not start_ts <= ts < end_ts or temps[k] is None:
                continue
            dur = min(ts_list[k + 1] - ts, cap) if k + 1 < len(ts_list) else 0
            if temps[k] < room.target_low:
                below += dur
            elif temps[k] > room.target_high:
                above += dur
        result[name] = (below / 60, above / 60)
    return result


# ---------------------------------------------------------------------------
# CLI


def _parse_variant(spec: str, base: Config) -> tuple[str, Config]:
    def boolean(v: str) -> bool:
        return v.lower() in ("1", "true", "yes", "on")

    def fan(none_word: str):
        return lambda v: climate_service._parse_fan_speed("fan_speed", v, none_word=none_word)

    # key -> (Config attribute(s), converter). Same names as config.toml.
    converters = {
        "hysteresis": (("heat_hysteresis", "cool_hysteresis"), float),
        "heat_hysteresis": (("heat_hysteresis",), float),
        "cool_hysteresis": (("cool_hysteresis",), float),
        "setpoint_boost": (("heat_setpoint_boost", "cool_setpoint_boost"), float),
        "heat_setpoint_boost": (("heat_setpoint_boost",), float),
        "cool_setpoint_boost": (("cool_setpoint_boost",), float),
        "heat_fan_speed": (("heat_fan_speed",), fan("keep")),
        "cool_fan_speed": (("cool_fan_speed",), fan("keep")),
        "pending_off_fan_speed": (("pending_off_fan_speed",), fan("off")),
        "demand_persist_polls": (("demand_persist_polls",), int),
        "min_mode_dwell_minutes": (("min_mode_dwell",), lambda v: float(v) * 60),
        "min_power_toggle_minutes": (("min_power_toggle",), lambda v: float(v) * 60),
        "poll_interval_seconds": (("poll_interval",), float),
        "manage_setpoints": (("manage_setpoints",), boolean),
        "heating": (("rooms",), None),  # applies to every room; handled below
    }
    label, sep, rest = spec.partition(":")
    if not sep or not rest:
        raise SystemExit(f"--variant {spec!r}: expected 'name:key=value[,key=value]'")
    changes = {}
    for pair in rest.split(","):
        key, sep, value = pair.partition("=")
        key = key.strip()
        if not sep or key not in converters:
            raise SystemExit(
                f"--variant {spec!r}: unknown setting {key!r} "
                f"(known: {', '.join(sorted(converters))})"
            )
        attrs, conv = converters[key]
        if key == "heating":
            changes["rooms"] = {
                name: dataclasses.replace(room, heating=boolean(value.strip()))
                for name, room in base.rooms.items()
            }
            continue
        try:
            converted = conv(value.strip())
        except ValueError as exc:
            raise SystemExit(f"--variant {spec!r}: {exc}") from None
        for attr in attrs:
            changes[attr] = converted
    return label, dataclasses.replace(base, **changes)


def _parse_local(text: str, tz: ZoneInfo) -> float:
    return datetime.fromisoformat(text).replace(tzinfo=tz).timestamp()


def _fmt_duration(seconds: float | None) -> str:
    if seconds is None:
        return "—"
    minutes = int(seconds // 60)
    return f"{minutes // 60}h{minutes % 60:02d}m" if minutes >= 60 else f"{minutes}m"


def _print_report(cfg, results, comfort, start_ts, end_ts, tz):
    span = (
        f"{datetime.fromtimestamp(start_ts, tz):%Y-%m-%d %H:%M} → "
        f"{datetime.fromtimestamp(end_ts, tz):%Y-%m-%d %H:%M} ({tz.key})"
    )
    print(f"Replay {span}")
    print(
        "NOTE: recorded temperatures are the baseline config's real outcome; "
        "variant rows show per-timestep decisions only (see module docstring)."
    )
    base = results[0]
    gaps = base.gap_ticks
    print(f"{base.ticks} ticks per variant" + (f", {gaps} skipped in recording gaps" if gaps else ""))

    header = (
        f"{'variant':<12} {'group':<8} {'flips':>5} {'starts':>6} {'stops':>6} "
        f"{'min-on':>7} {'min-off':>7} {'on-cmds':>7} {'off-cmds':>8} {'parks':>6}"
    )
    print()
    print(header)
    print("-" * len(header))
    for result in results:
        for group in cfg.groups:
            m = result.metrics[group.name]
            print(
                f"{result.label:<12} {group.name:<8} {m.mode_flips:>5} "
                f"{m.compressor_starts:>6} {m.compressor_stops:>6} "
                f"{_fmt_duration(m.shortest_on):>7} {_fmt_duration(m.shortest_off):>7} "
                f"{m.power_cmds['TURN_ON']:>7} {m.power_cmds['TURN_OFF']:>8} "
                f"{m.idle_parks:>6}"
            )

    print()
    print("Recorded comfort (baseline outcome, not a variant prediction):")
    print(f"{'room':<8} {'below range':>12} {'above range':>12}")
    for name, (below, above) in comfort.items():
        print(f"{name:<8} {below:>11.0f}m {above:>11.0f}m")


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__.split("\n")[0],
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--db", type=Path, help="history.db to replay from")
    source.add_argument("--csv", type=Path, help="exported fixture (.csv or .csv.gz)")
    parser.add_argument("--config", type=Path,
                        default=Path(__file__).parent / "config.toml")
    parser.add_argument("--from", dest="start", metavar="LOCAL",
                        help="window start, YYYY-MM-DD[THH:MM] local (default: data start)")
    parser.add_argument("--to", dest="end", metavar="LOCAL",
                        help="window end, exclusive (default: data end)")
    parser.add_argument("--tz", default=DEFAULT_TZ, help=f"zoneinfo name (default {DEFAULT_TZ})")
    parser.add_argument("--variant", action="append", default=[], metavar="SPEC",
                        help="'name:key=value[,key=value]', repeatable; baseline always runs")
    parser.add_argument("--no-shutdown", action="store_true",
                        help="ignore shutdown windows (policy-only replay)")
    parser.add_argument("--dump-events", type=Path, metavar="CSV",
                        help="write the baseline decision log")
    parser.add_argument("--export", type=Path, metavar="CSV_GZ",
                        help="export the readings window as a fixture and exit")
    parser.add_argument("--verbose", action="store_true",
                        help="show the controller's own log lines")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(message)s", stream=sys.stdout,
    )
    tz = ZoneInfo(args.tz)
    cfg = load_config(args.config)

    if args.db:
        with sqlite3.connect(f"file:{args.db}?mode=ro", uri=True) as conn:
            lo, hi = conn.execute("SELECT MIN(ts), MAX(ts) FROM readings").fetchone()
        start_ts = _parse_local(args.start, tz) if args.start else float(lo)
        end_ts = _parse_local(args.end, tz) if args.end else float(hi) + 1
        if args.export:
            rows = export_fixture(args.db, args.export, start_ts, end_ts)
            print(f"exported {rows} rows to {args.export}")
            return 0
        feed = load_feed_db(args.db, start_ts, end_ts)
    else:
        if args.export:
            raise SystemExit("--export needs --db")
        feed = load_feed_csv(args.csv)
        all_ts = sorted(ts for cols in feed.values() for ts in cols[0])
        start_ts = _parse_local(args.start, tz) if args.start else float(all_ts[0])
        end_ts = _parse_local(args.end, tz) if args.end else float(all_ts[-1]) + 1

    variants = [("baseline", cfg)]
    variants += [_parse_variant(spec, cfg) for spec in args.variant]

    results = [
        run_replay(vcfg, feed, start_ts, end_ts, tz, no_shutdown=args.no_shutdown,
                   label=label)
        for label, vcfg in variants
    ]
    comfort = out_of_range_minutes(cfg, feed, start_ts, end_ts)
    _print_report(cfg, results, comfort, start_ts, end_ts, tz)

    if args.dump_events:
        with args.dump_events.open("w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(["ts", "local", "unit", "verb", "value"])
            for ts, unit, verb, value in results[0].events:
                local = datetime.fromtimestamp(ts, tz).strftime("%Y-%m-%d %H:%M:%S")
                writer.writerow([int(ts), local, unit, verb, value])
        print(f"\nwrote {len(results[0].events)} events to {args.dump_events}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
