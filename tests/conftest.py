"""Shared test fixtures: a fake AirConditioner, config factory, group builder.

pyairtouch's AirConditioner is a typing.Protocol, so FakeAc satisfies it
structurally — no library machinery involved. Command coroutines mutate the
fake's state immediately (the console echo the live service waits for) and
append to a shared per-group command log for assertions.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from pyairtouch import AcMode, AcPowerControl, AcPowerState

import climate_service
from climate_service import Config, GroupConfig, GroupController, RoomConfig


class FakeAc:
    def __init__(
        self,
        name: str,
        *,
        temp: float | None = None,
        setpoint: float | None = None,
        power: AcPowerState = AcPowerState.OFF,
        mode: AcMode | None = None,
        commands: list | None = None,
    ) -> None:
        self.name = name
        self.current_temperature = temp
        self.target_temperature = setpoint
        self.power_state = power
        self.selected_mode = mode
        self.active_mode = mode
        self.min_target_temperature = 16.0
        self.max_target_temperature = 31.0
        self.target_temperature_resolution: float | None = 0.1
        self.commands = commands if commands is not None else []

    async def set_power(self, power_control: AcPowerControl) -> None:
        self.commands.append((self.name, "power", power_control))
        if power_control is AcPowerControl.TURN_ON:
            self.power_state = AcPowerState.ON
        elif power_control is AcPowerControl.TURN_OFF:
            self.power_state = AcPowerState.OFF

    async def set_mode(self, mode: AcMode, *, power_on: bool = False) -> None:
        self.commands.append((self.name, "mode", mode))
        self.selected_mode = mode
        self.active_mode = mode
        if power_on:
            self.power_state = AcPowerState.ON

    async def set_target_temperature(self, temperature: float) -> None:
        self.commands.append((self.name, "setpoint", temperature))
        self.target_temperature = temperature


def make_config(**overrides) -> Config:
    """A minimal valid Config: one group, master A + member B, range 21–24."""
    base = dict(
        host="",
        poll_interval=30.0,
        dry_run=False,
        hysteresis=0.4,
        demand_persist_polls=2,
        min_mode_dwell=3600.0,
        min_power_toggle=600.0,
        manage_setpoints=True,
        setpoint_boost=1.0,
        history_path=None,
        history_interval=60.0,
        weather_port=None,
        weather_path="/data/report/",
        log_path=None,
        shutdown_windows=(),
        override_path=Path("/nonexistent/control_override.json"),
        groups=(GroupConfig(name="g", master="A", members=("A", "B")),),
        rooms={"A": RoomConfig(21.0, 24.0), "B": RoomConfig(21.0, 24.0)},
    )
    base.update(overrides)
    return Config(**base)


def make_group(
    cfg: Config,
    temps: dict[str, float | None],
    *,
    modes: dict[str, AcMode] | None = None,
    powers: dict[str, AcPowerState] | None = None,
    setpoints: dict[str, float] | None = None,
) -> tuple[GroupController, dict[str, FakeAc], list]:
    """Build a GroupController over fake units for cfg's first group.

    Unit state must be final before construction: _adopt_current_state runs
    in __init__ and seeds mode/running_for/demand from it.
    """
    commands: list = []
    group = cfg.groups[0]
    units = {
        name: FakeAc(
            name,
            temp=temps.get(name),
            mode=(modes or {}).get(name),
            power=(powers or {}).get(name, AcPowerState.OFF),
            setpoint=(setpoints or {}).get(name),
            commands=commands,
        )
        for name in group.members
    }
    controller = GroupController(cfg, group, units)
    return controller, units, commands


@pytest.fixture(autouse=True)
def _no_command_gap(monkeypatch):
    """Commands in tests shouldn't sleep 0.5s each."""
    monkeypatch.setattr(climate_service, "COMMAND_GAP", 0)


def make_history_db(
    path: Path,
    rows: list[tuple],
    weather_rows: list[tuple] | None = None,
    *,
    with_weather_table: bool = True,
) -> None:
    """Create a history DB from (ts, unit, temp, setpoint, power, mode,
    activity) rows, reusing the real schema."""
    conn = sqlite3.connect(path)
    conn.executescript(climate_service.HistoryRecorder._SCHEMA)
    if not with_weather_table:
        conn.execute("DROP TABLE weather")
    conn.executemany("INSERT INTO readings VALUES (?, ?, ?, ?, ?, ?, ?)", rows)
    if weather_rows:
        conn.executemany("INSERT INTO weather VALUES (?, ?, ?)", weather_rows)
    conn.commit()
    conn.close()
