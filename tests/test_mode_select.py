"""Group mode selection: first-run pick, stickiness, dwell."""

from pyairtouch import AcMode

from conftest import make_config, make_group


def demands(ctl, **by_room):
    for name, mode in by_room.items():
        ctl._state.rooms[name].demand = mode


class TestFirstRun:
    def test_heat_demand_picks_heat(self):
        ctl, _, _ = make_group(make_config(), {"A": 20.0, "B": 22.0})
        assert ctl._state.desired_mode is None  # master had no heat/cool mode
        assert ctl._select_mode(now=5.0) is AcMode.HEAT
        assert ctl._state.last_mode_change == 5.0

    def test_cool_only_picks_cool(self):
        ctl, _, _ = make_group(make_config(), {"A": 25.0, "B": 22.0})
        assert ctl._select_mode(now=0.0) is AcMode.COOL

    def test_tie_prefers_heat(self):
        ctl, _, _ = make_group(make_config(), {"A": 20.0, "B": 25.0})
        assert ctl._select_mode(now=0.0) is AcMode.HEAT

    def test_no_demand_defaults_to_heat(self):
        ctl, _, _ = make_group(make_config(), {"A": 22.0, "B": 22.0})
        assert ctl._select_mode(now=0.0) is AcMode.HEAT

    def test_master_mode_adopted(self):
        ctl, _, _ = make_group(
            make_config(), {"A": 22.0, "B": 22.0}, modes={"A": AcMode.COOL}
        )
        assert ctl._state.desired_mode is AcMode.COOL


class TestStickiness:
    def make(self):
        ctl, _, _ = make_group(
            make_config(), {"A": 22.0, "B": 22.0}, modes={"A": AcMode.HEAT}
        )
        ctl._state.last_mode_change = 0.0
        return ctl

    def test_no_demand_no_flip(self):
        ctl = self.make()
        assert ctl._select_mode(now=10_000.0) is AcMode.HEAT

    def test_current_demand_holds_mode(self):
        ctl = self.make()
        demands(ctl, A=AcMode.HEAT, B=AcMode.COOL)
        assert ctl._select_mode(now=10_000.0) is AcMode.HEAT

    def test_opposite_demand_within_dwell_waits(self):
        ctl = self.make()
        demands(ctl, B=AcMode.COOL)
        assert ctl._select_mode(now=3599.0) is AcMode.HEAT
        assert ctl._state.desired_mode is AcMode.HEAT

    def test_opposite_demand_after_dwell_flips(self):
        ctl = self.make()
        demands(ctl, B=AcMode.COOL)
        assert ctl._select_mode(now=3600.0) is AcMode.COOL
        assert ctl._state.last_mode_change == 3600.0


class TestModeSwitchBlocker:
    def test_named_holding_room(self):
        ctl, _, _ = make_group(
            make_config(), {"A": 20.0, "B": 22.0}, modes={"A": AcMode.HEAT}
        )
        assert ctl._mode_switch_blocker(now=0.0) == "blocked by A still needing HEAT"

    def test_dwell_remaining(self):
        ctl, _, _ = make_group(
            make_config(), {"A": 22.0, "B": 22.0}, modes={"A": AcMode.HEAT}
        )
        ctl._state.last_mode_change = 0.0
        assert ctl._mode_switch_blocker(now=100.0) == "mode dwell 58m left"

    def test_switching_next_pass(self):
        ctl, _, _ = make_group(
            make_config(), {"A": 22.0, "B": 22.0}, modes={"A": AcMode.HEAT}
        )
        assert ctl._mode_switch_blocker(now=0.0) == "switching next pass"
