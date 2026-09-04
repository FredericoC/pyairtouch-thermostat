"""GroupController.tick: actuation, compressor gating, shutdown, dry-run."""

import pytest
from pyairtouch import AcFanSpeed, AcMode, AcPowerControl, AcPowerState

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
    async def test_unknown_compressor_history_is_never_held(self):
        # Same satisfied-last-unit scenario as test_last_unit_off_is_held_and_
        # parked, but compressor_change is None (fresh adoption): we don't know
        # when the outdoor unit last toggled, so no hold applies.
        ctl, units, commands = make_group(
            make_config(),
            {"A": 22.6, "B": 22.0},
            modes={"A": AcMode.HEAT},
            powers={"A": AcPowerState.ON},
        )
        assert ctl._state.compressor_change is None
        await ctl.tick(now=1000.0)
        assert ("A", "power", AcPowerControl.TURN_OFF) in commands
        assert ("A", "setpoint", 22.0) not in commands  # not parked

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


class TestManageSetpointsOff:
    async def test_power_only_no_setpoint_or_member_mode(self):
        # Without setpoint management the service only toggles power; member
        # mode alignment exists to make setpoints land on the right mode, so
        # it is skipped too. The master's mode still drives the group.
        ctl, units, commands = make_group(
            make_config(manage_setpoints=False),
            {"A": 22.0, "B": 20.0},
            modes={"A": AcMode.COOL, "B": AcMode.COOL},
        )
        await ctl.tick(now=0.0)
        assert commands == [
            ("A", "mode", AcMode.HEAT),
            ("B", "power", AcPowerControl.TURN_ON),
        ]

    async def test_pending_off_not_parked(self):
        ctl, units, commands = make_group(
            make_config(manage_setpoints=False),
            {"A": 22.6, "B": 22.0},
            modes={"A": AcMode.HEAT},
            powers={"A": AcPowerState.ON},
        )
        ctl._state.compressor_change = 970.0
        await ctl.tick(now=1000.0)
        assert commands == []  # held on, but no idle setpoint sent


class TestDryRun:
    @pytest.mark.filterwarnings("error::RuntimeWarning")
    async def test_dry_run_sends_nothing(self):
        ctl, units, commands = make_group(
            make_config(dry_run=True), {"A": 20.0, "B": 22.0}
        )
        await ctl.tick(now=0.0)
        assert commands == []  # coroutines closed, never executed
        assert units["A"].power_state is AcPowerState.OFF


class TestFanSpeed:
    """Per-mode fan speed on power-on; quiet drop while pending off."""

    FANS = dict(
        heat_fan_speed=AcFanSpeed.AUTO,
        cool_fan_speed=AcFanSpeed.MEDIUM,
        pending_off_fan_speed=AcFanSpeed.QUIET,
    )

    async def test_power_on_sets_mode_fan_before_power(self):
        ctl, units, commands = make_group(
            make_config(**self.FANS), {"A": 25.0, "B": 22.0}, modes={"A": AcMode.COOL}
        )
        await ctl.tick(now=0.0)
        assert commands == [
            ("A", "setpoint", 22.0),
            ("A", "fan", AcFanSpeed.MEDIUM),
            ("A", "power", AcPowerControl.TURN_ON),
        ]
        assert units["A"].selected_fan_speed is AcFanSpeed.MEDIUM

    async def test_fan_already_right_sends_nothing(self):
        ctl, _, commands = make_group(
            make_config(**self.FANS),
            {"A": 20.0, "B": 22.0},
            modes={"A": AcMode.HEAT},
            fans={"A": AcFanSpeed.AUTO},
        )
        await ctl.tick(now=0.0)
        assert [c for c in commands if c[1] == "fan"] == []

    async def test_fan_not_reasserted_while_running(self):
        # Changed on the wall panel mid-run: left alone.
        ctl, units, commands = make_group(
            make_config(**self.FANS),
            {"A": 25.0, "B": 22.0},
            modes={"A": AcMode.COOL},
            powers={"A": AcPowerState.ON},
            fans={"A": AcFanSpeed.HIGH},
        )
        await ctl.tick(now=0.0)
        assert [c for c in commands if c[1] == "fan"] == []

    def pending_off(self, **cfg_overrides):
        ctl, units, commands = make_group(
            make_config(**{**self.FANS, **cfg_overrides}),
            {"A": 23.3, "B": 22.0},  # below 24 - 0.4: satisfied
            modes={"A": AcMode.COOL},
            powers={"A": AcPowerState.ON},
            fans={"A": AcFanSpeed.MEDIUM},
        )
        ctl._state.compressor_change = 970.0  # compressor started 30s ago
        return ctl, units, commands

    async def test_pending_off_drops_fan_once(self):
        ctl, units, commands = self.pending_off()
        await ctl.tick(now=1000.0)
        assert commands == [
            ("A", "setpoint", 24.0),  # parked: ceil(23.3)
            ("A", "fan", AcFanSpeed.QUIET),
        ]
        assert ctl._state.rooms["A"].fan_parked
        commands.clear()
        await ctl.tick(now=1030.0)  # still held: nothing repeated
        assert commands == []

    async def test_pending_off_drop_without_setpoint_management(self):
        ctl, units, commands = self.pending_off(manage_setpoints=False)
        await ctl.tick(now=1000.0)
        assert commands == [("A", "fan", AcFanSpeed.QUIET)]

    async def test_hold_elapses_then_off_and_next_run_restores_fan(self):
        ctl, units, commands = self.pending_off()
        await ctl.tick(now=1000.0)
        await ctl.tick(now=1570.0)  # hold over
        assert ("A", "power", AcPowerControl.TURN_OFF) in commands
        assert units["A"].selected_fan_speed is AcFanSpeed.QUIET  # console remembers
        commands.clear()
        units["A"].current_temperature = 24.5
        for now in (1600.0, 1630.0):  # debounce, then compressor hold from the stop
            await ctl.tick(now=now)
        await ctl.tick(now=1570.0 + 600.0)
        assert ("A", "fan", AcFanSpeed.MEDIUM) in commands
        assert commands.index(("A", "fan", AcFanSpeed.MEDIUM)) < commands.index(
            ("A", "power", AcPowerControl.TURN_ON)
        )
        assert not ctl._state.rooms["A"].fan_parked

    async def test_demand_returns_while_parked_restores_fan(self):
        ctl, units, commands = self.pending_off()
        await ctl.tick(now=1000.0)
        commands.clear()
        units["A"].current_temperature = 24.5  # hot again before the hold let it off
        await ctl.tick(now=1030.0)
        await ctl.tick(now=1060.0)  # debounced
        assert ("A", "fan", AcFanSpeed.MEDIUM) in commands
        assert [c for c in commands if c[1] == "power"] == []
        assert not ctl._state.rooms["A"].fan_parked

    async def test_keep_mode_restores_pre_park_speed(self):
        ctl, units, commands = self.pending_off(cool_fan_speed=None)
        units["A"].selected_fan_speed = AcFanSpeed.HIGH  # whatever the user had
        await ctl.tick(now=1000.0)
        assert ("A", "fan", AcFanSpeed.QUIET) in commands
        commands.clear()
        units["A"].current_temperature = 24.5
        await ctl.tick(now=1030.0)
        await ctl.tick(now=1060.0)
        assert ("A", "fan", AcFanSpeed.HIGH) in commands

    async def test_no_pending_off_speed_no_drop(self):
        ctl, units, commands = self.pending_off(pending_off_fan_speed=None)
        await ctl.tick(now=1000.0)
        assert [c for c in commands if c[1] == "fan"] == []
        assert not ctl._state.rooms["A"].fan_parked

    async def test_quiet_unsupported_falls_back_to_low(self):
        ctl, units, commands = self.pending_off()
        units["A"].supported_fan_speeds = (AcFanSpeed.AUTO, AcFanSpeed.LOW, AcFanSpeed.HIGH)
        await ctl.tick(now=1000.0)
        assert ("A", "fan", AcFanSpeed.LOW) in commands

    async def test_unsupported_speed_warns_and_skips(self, caplog):
        ctl, units, commands = make_group(
            make_config(**self.FANS), {"A": 25.0, "B": 22.0}, modes={"A": AcMode.COOL}
        )
        units["A"].supported_fan_speeds = (AcFanSpeed.AUTO, AcFanSpeed.LOW, AcFanSpeed.HIGH)
        with caplog.at_level("WARNING", logger="climate"):
            await ctl.tick(now=0.0)
        assert [c for c in commands if c[1] == "fan"] == []
        assert ("A", "power", AcPowerControl.TURN_ON) in commands
        assert "does not support fan speed MEDIUM" in caplog.text


class TestPerModeBoost:
    async def test_cool_boost_zero_keeps_floor_rounding(self):
        ctl, _, commands = make_group(
            make_config(cool_hysteresis=1.2, cool_setpoint_boost=0.0),
            {"A": 25.0, "B": 22.0},
            modes={"A": AcMode.COOL},
        )
        await ctl.tick(now=0.0)
        assert ("A", "setpoint", 22.0) in commands  # floor(24 - 1.2)

    async def test_heat_boost_untouched(self):
        ctl, _, commands = make_group(
            make_config(cool_setpoint_boost=0.0), {"A": 20.0, "B": 22.0}
        )
        await ctl.tick(now=0.0)
        assert ("A", "setpoint", 23.0) in commands  # ceil(21 + 0.4 + 1)
