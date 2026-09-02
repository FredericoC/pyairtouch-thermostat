"""ClimateService._control_loop: the shutdown-window, pause and override
transitions that decide whether a pass ticks, forces units off, or sends
nothing at all.

The loop reads the wall clock via `datetime.now()` for the override/schedule
state, so the module's `datetime` is swapped for a frozen subclass that the
test advances by hand. Controller timing (`time.monotonic()`) is left real:
each test's passes are milliseconds apart, so a compressor toggle in one
pass holds the next — which is exactly what the shutdown off pass must
bypass and a resumed tick must not.
"""

from datetime import datetime

import pytest
from pyairtouch import AcMode, AcPowerControl, AcPowerState

import climate_service
from climate_service import ClimateService
from conftest import make_config, make_group

WINDOWS = ((1200, 480),)  # 20:00–08:00


class Clock:
    """Freezes climate_service.datetime.now() at a settable local time."""

    def __init__(self, monkeypatch, start: datetime) -> None:
        self.now = start
        clock = self

        class Frozen(datetime):
            @classmethod
            def now(cls, tz=None):  # noqa: ARG003 (signature compatibility)
                return clock.now

        monkeypatch.setattr(climate_service, "datetime", Frozen)

    def set(self, hour: int, minute: int = 0, *, day: int = 1) -> None:
        self.now = datetime(2026, 1, day, hour, minute)

    def timestamp(self) -> float:
        return self.now.timestamp()


class FakeAirTouch:
    def __init__(self, initialised: bool = True) -> None:
        self.initialised = initialised


def write_override(tmp_path, payload) -> None:
    import json

    (tmp_path / "control_override.json").write_text(json.dumps(payload))


def clear_override(tmp_path) -> None:
    path = tmp_path / "control_override.json"
    if path.exists():
        path.unlink()


@pytest.fixture
def clock(monkeypatch):
    return Clock(monkeypatch, datetime(2026, 1, 1, 12, 0))


def build(tmp_path, clock, temps, *, history=False, **group_kwargs):
    """A service + one controller over fake units, built at the clock's time."""
    cfg = make_config(
        shutdown_windows=WINDOWS,
        override_path=tmp_path / "control_override.json",
        history_path=(tmp_path / "history.db") if history else None,
    )
    ctl, units, commands = make_group(cfg, temps, **group_kwargs)
    service = ClimateService(cfg)
    return service, ctl, units, commands


async def run_pass(service, ctl, airtouch=None) -> None:
    await service._control_loop(airtouch or FakeAirTouch(), [ctl], once=True)


def power_commands(commands):
    return [c for c in commands if c[1] == "power"]


class TestSchedule:
    async def test_outside_window_ticks(self, tmp_path, clock):
        service, ctl, units, commands = build(tmp_path, clock, {"A": 20.0, "B": 22.0})
        await run_pass(service, ctl)
        assert ("A", "power", AcPowerControl.TURN_ON) in commands
        assert units["A"].power_state is AcPowerState.ON

    async def test_window_start_runs_off_pass_once(self, tmp_path, clock):
        clock.set(19, 59)
        service, ctl, units, commands = build(tmp_path, clock, {"A": 20.0, "B": 22.0})
        await run_pass(service, ctl)
        assert units["A"].power_state is AcPowerState.ON
        commands.clear()

        clock.set(20, 0)  # window opens: one off pass, hold bypassed
        await run_pass(service, ctl)
        assert power_commands(commands) == [("A", "power", AcPowerControl.TURN_OFF)]
        assert ctl._state.rooms["A"].running_for is None
        commands.clear()

        units["A"].power_state = AcPowerState.ON  # switched on manually
        clock.set(20, 1)
        await run_pass(service, ctl)
        assert commands == []  # left alone for the rest of the window
        clock.set(3, 0, day=2)
        await run_pass(service, ctl)
        assert commands == []

    async def test_restart_mid_window_skips_off_pass(self, tmp_path, clock):
        clock.set(21, 0)
        service, ctl, units, commands = build(
            tmp_path, clock, {"A": 20.0, "B": 22.0},
            modes={"A": AcMode.HEAT}, powers={"A": AcPowerState.ON},
        )
        await run_pass(service, ctl)
        assert commands == []

    async def test_window_end_resumes_control(self, tmp_path, clock):
        clock.set(21, 0)
        # Manually on and already satisfied; control must take it back over.
        service, ctl, units, commands = build(
            tmp_path, clock, {"A": 22.0, "B": 22.0},
            modes={"A": AcMode.HEAT}, powers={"A": AcPowerState.ON},
        )
        await run_pass(service, ctl)
        assert commands == []
        clock.set(8, 0, day=2)
        await run_pass(service, ctl)
        assert power_commands(commands) == [("A", "power", AcPowerControl.TURN_OFF)]

    async def test_window_start_with_nothing_on_sends_nothing(self, tmp_path, clock):
        clock.set(19, 59)
        service, ctl, units, commands = build(
            tmp_path, clock, {"A": 22.0, "B": 22.0}, modes={"A": AcMode.HEAT}
        )
        await run_pass(service, ctl)
        clock.set(20, 0)
        await run_pass(service, ctl)
        assert commands == []


class TestPause:
    async def test_pause_releases_without_commands(self, tmp_path, clock):
        service, ctl, units, commands = build(tmp_path, clock, {"A": 20.0, "B": 22.0})
        await run_pass(service, ctl)
        assert ctl._state.rooms["A"].running_for is AcMode.HEAT
        commands.clear()

        write_override(tmp_path, {"pause": True})
        clock.set(12, 1)
        await run_pass(service, ctl)
        assert commands == []
        assert ctl._state.rooms["A"].running_for is None  # now "manual"
        assert units["A"].power_state is AcPowerState.ON  # not switched off

    async def test_pause_suspends_indefinitely_and_across_windows(self, tmp_path, clock):
        service, ctl, units, commands = build(
            tmp_path, clock, {"A": 20.0, "B": 22.0},
            modes={"A": AcMode.HEAT}, powers={"A": AcPowerState.ON},
        )
        write_override(tmp_path, {"pause": True})
        for hour, minute, day in [(12, 0, 1), (20, 0, 1), (23, 0, 1), (9, 0, 2)]:
            clock.set(hour, minute, day=day)
            await run_pass(service, ctl)
        assert commands == []  # no shutdown off pass, no ticks

    async def test_resume_outside_window_retakes_control(self, tmp_path, clock):
        service, ctl, units, commands = build(tmp_path, clock, {"A": 20.0, "B": 22.0})
        await run_pass(service, ctl)
        write_override(tmp_path, {"pause": True})
        clock.set(12, 1)
        await run_pass(service, ctl)
        assert ctl._state.rooms["A"].running_for is None
        commands.clear()

        clear_override(tmp_path)
        clock.set(12, 2)
        await run_pass(service, ctl)
        assert ctl._state.rooms["A"].running_for is AcMode.HEAT
        assert power_commands(commands) == []  # already on: nothing to toggle

    async def test_resume_inside_window_runs_off_pass(self, tmp_path, clock):
        clock.set(21, 0)
        service, ctl, units, commands = build(tmp_path, clock, {"A": 22.0, "B": 22.0})
        write_override(tmp_path, {"pause": True})
        await run_pass(service, ctl)
        units["A"].power_state = AcPowerState.ON  # run manually while paused
        assert commands == []

        clear_override(tmp_path)
        clock.set(21, 5)  # resume = back to schedule → the window's off pass
        await run_pass(service, ctl)
        assert power_commands(commands) == [("A", "power", AcPowerControl.TURN_OFF)]
        commands.clear()
        clock.set(21, 6)
        await run_pass(service, ctl)
        assert commands == []  # and only once


class TestShutdownOverride:
    async def test_turn_on_inside_window_resumes_control(self, tmp_path, clock):
        clock.set(21, 0)
        service, ctl, units, commands = build(tmp_path, clock, {"A": 20.0, "B": 22.0})
        write_override(tmp_path, {"shutdown": False, "expires": clock.timestamp() + 3600})
        await run_pass(service, ctl)
        assert ("A", "power", AcPowerControl.TURN_ON) in commands

    async def test_override_expiry_reverts_to_schedule(self, tmp_path, clock):
        clock.set(21, 0)
        service, ctl, units, commands = build(tmp_path, clock, {"A": 20.0, "B": 22.0})
        write_override(tmp_path, {"shutdown": False, "expires": clock.timestamp() + 60})
        await run_pass(service, ctl)
        assert units["A"].power_state is AcPowerState.ON
        commands.clear()

        clock.set(21, 1)  # expires <= now: back inside the window → off pass
        await run_pass(service, ctl)
        assert power_commands(commands) == [("A", "power", AcPowerControl.TURN_OFF)]

    async def test_turn_off_outside_window_forces_off(self, tmp_path, clock):
        service, ctl, units, commands = build(tmp_path, clock, {"A": 20.0, "B": 22.0})
        await run_pass(service, ctl)
        assert units["A"].power_state is AcPowerState.ON
        commands.clear()

        write_override(tmp_path, {"shutdown": True, "expires": clock.timestamp() + 3600})
        clock.set(12, 1)
        await run_pass(service, ctl)
        assert power_commands(commands) == [("A", "power", AcPowerControl.TURN_OFF)]


class TestRecordingAndConnection:
    async def test_history_records_manual_activity_while_suspended(self, tmp_path, clock):
        import sqlite3

        # Window opens (off pass), A is switched back on by hand: the next
        # sample must say "(manual)" — the policy isn't driving it.
        clock.set(20, 0)
        service, ctl, units, commands = build(
            tmp_path, clock, {"A": 20.0, "B": 22.0}, history=True,
            modes={"A": AcMode.HEAT}, powers={"A": AcPowerState.ON},
        )
        service._was_shutdown = False  # window began this pass, not before
        try:
            await run_pass(service, ctl)
            assert units["A"].power_state is AcPowerState.OFF
            units["A"].power_state = AcPowerState.ON
            service._history._last_sample = 0.0  # don't wait out the interval
            clock.set(20, 1)
            await run_pass(service, ctl)
        finally:
            service._history.close()
        rows = sqlite3.connect(tmp_path / "history.db").execute(
            "SELECT unit, power, activity FROM readings ORDER BY rowid"
        ).fetchall()
        assert rows == [
            ("A", 0, "idle"), ("B", 0, "idle"),  # after the off pass
            ("A", 1, "heating (manual)"), ("B", 0, "idle"),
        ]

    async def test_uninitialised_connection_raises(self, tmp_path, clock):
        service, ctl, _, commands = build(tmp_path, clock, {"A": 20.0, "B": 22.0})
        with pytest.raises(ConnectionError):
            await run_pass(service, ctl, FakeAirTouch(initialised=False))
        assert commands == []
