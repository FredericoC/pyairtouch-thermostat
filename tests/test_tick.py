"""GroupController.tick: actuation, compressor gating, shutdown, dry-run."""

import pytest
from pyairtouch import AcMode, AcPowerControl, AcPowerState

from conftest import make_config, make_group


class TestPowerCommands:
    async def test_power_on_sends_setpoint_first(self):
        ctl, units, commands = make_group(make_config(), {"A": 20.0, "B": 22.0})
        await ctl.tick(now=0.0)
        assert commands == [
            ("A", "mode", AcMode.HEAT),  # master aligned to the selected mode
            ("A", "setpoint", 23.0),  # ceil(21 + 0.4 + 1)
            ("A", "power", AcPowerControl.TURN_ON),
        ]
        assert units["A"].power_state is AcPowerState.ON
        assert ctl._state.compressor_on
        assert ctl._state.compressor_change == 0.0

    async def test_power_off_when_satisfied(self):
        ctl, units, commands = make_group(
            make_config(),
            {"A": 22.0, "B": 22.0},
            modes={"A": AcMode.HEAT},
            powers={"A": AcPowerState.ON},
        )
        await ctl.tick(now=0.0)
        assert ("A", "power", AcPowerControl.TURN_OFF) in commands
        assert not ctl._state.compressor_on
        assert ctl._state.compressor_change == 0.0

    async def test_member_mode_aligned_before_setpoint(self):
        ctl, units, commands = make_group(
            make_config(),
            {"A": 22.0, "B": 20.0},
            modes={"A": AcMode.HEAT, "B": AcMode.COOL},
        )
        await ctl.tick(now=0.0)
        assert commands == [
            ("B", "mode", AcMode.HEAT),  # matched to the master, power stays off
            ("B", "setpoint", 23.0),
            ("B", "power", AcPowerControl.TURN_ON),
        ]

    async def test_no_reading_is_left_alone(self):
        ctl, _, commands = make_group(
            make_config(), {"A": 22.0, "B": None}, modes={"A": AcMode.HEAT}
        )
        await ctl.tick(now=0.0)
        assert [c for c in commands if c[0] == "B"] == []

    async def test_adopted_running_unit_not_jolted_off(self):
        # On at 20.5°C: inside the plain range but below low+hysteresis, so
        # the seeded demand keeps it running through the first pass.
        ctl, units, commands = make_group(
            make_config(),
            {"A": 20.5, "B": 22.0},
            modes={"A": AcMode.HEAT},
            powers={"A": AcPowerState.ON},
        )
        await ctl.tick(now=0.0)
        assert [c for c in commands if c[1] == "power"] == []
        assert ("A", "setpoint", 23.0) in commands  # setpoint still managed


class TestCompressorGating:
    async def test_first_pass_never_held(self):
        # compressor_change is None after adoption: unknown history, no hold.
        ctl, _, commands = make_group(make_config(), {"A": 20.0, "B": 22.0})
        await ctl.tick(now=0.0)
        assert ("A", "power", AcPowerControl.TURN_ON) in commands

    async def test_peer_toggle_is_free_while_compressor_runs(self):
        ctl, units, commands = make_group(
            make_config(),
            {"A": 19.0, "B": 20.0},
            modes={"A": AcMode.HEAT},
            powers={"A": AcPowerState.ON},
        )
        ctl._state.compressor_change = 970.0  # compressor started 30s ago
        await ctl.tick(now=1000.0)
        assert ("B", "power", AcPowerControl.TURN_ON) in commands
        assert ctl._state.compressor_change == 970.0  # not a compressor event

    async def test_last_unit_off_is_held_and_parked(self):
        ctl, units, commands = make_group(
            make_config(),
            {"A": 22.6, "B": 22.0},
            modes={"A": AcMode.HEAT},
            powers={"A": AcPowerState.ON},
        )
        ctl._state.compressor_change = 970.0
        await ctl.tick(now=1000.0)
        assert [c for c in commands if c[1] == "power"] == []
        # Pending off: setpoint parked at floor(room temp) so it idles.
        assert ("A", "setpoint", 22.0) in commands
        assert units["A"].power_state is AcPowerState.ON

    async def test_hold_elapses_then_powers_off(self):
        ctl, units, commands = make_group(
            make_config(),
            {"A": 22.6, "B": 22.0},
            modes={"A": AcMode.HEAT},
            powers={"A": AcPowerState.ON},
        )
        ctl._state.compressor_change = 970.0
        await ctl.tick(now=970.0 + 600.0)  # min_power_toggle elapsed
        assert ("A", "power", AcPowerControl.TURN_OFF) in commands

    async def test_same_pass_command_counts_as_peer(self):
        # A powers on earlier in the pass; B's power-off is then free even
        # though the console hasn't echoed A's new state yet.
        ctl, units, commands = make_group(
            make_config(),
            {"A": 20.0, "B": 22.0},
            modes={"A": AcMode.HEAT},
            powers={"B": AcPowerState.ON},
        )
        ctl._state.compressor_change = 995.0  # 5s ago: any compressor toggle held
        await ctl.tick(now=1000.0)
        assert ("A", "power", AcPowerControl.TURN_ON) in commands
        assert ("B", "power", AcPowerControl.TURN_OFF) in commands

    async def test_external_power_on_is_observed(self):
        ctl, units, _ = make_group(
            make_config(), {"A": 22.0, "B": 22.0}, modes={"A": AcMode.HEAT}
        )
        assert not ctl._state.compressor_on
        units["B"].power_state = AcPowerState.ON  # wall panel / app
        await ctl.tick(now=50.0)
        assert ctl._state.compressor_on
        assert ctl._state.compressor_change == 50.0


class TestEnforceShutdown:
    async def test_all_on_units_forced_off(self):
        ctl, units, commands = make_group(
            make_config(),
            {"A": 20.0, "B": 20.0},
            modes={"A": AcMode.HEAT},
            powers={"A": AcPowerState.ON, "B": AcPowerState.ON},
        )
        ctl._state.compressor_change = 999.0  # recent: hold would normally apply
        await ctl.enforce_shutdown(now=1000.0)
        assert ("A", "power", AcPowerControl.TURN_OFF) in commands
        assert ("B", "power", AcPowerControl.TURN_OFF) in commands
        assert all(r.running_for is None for r in ctl._state.rooms.values())
        assert not ctl._state.compressor_on

    async def test_off_units_get_no_command(self):
        ctl, _, commands = make_group(
            make_config(), {"A": 22.0, "B": 22.0}, modes={"A": AcMode.HEAT}
        )
        await ctl.enforce_shutdown(now=0.0)
        assert commands == []


class TestDryRun:
    @pytest.mark.filterwarnings("error::RuntimeWarning")
    async def test_dry_run_sends_nothing(self):
        ctl, units, commands = make_group(
            make_config(dry_run=True), {"A": 20.0, "B": 22.0}
        )
        await ctl.tick(now=0.0)
        assert commands == []  # coroutines closed, never executed
        assert units["A"].power_state is AcPowerState.OFF
