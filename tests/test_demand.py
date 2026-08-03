"""Per-room demand: hysteresis and the persistence debounce."""

from pyairtouch import AcMode

from conftest import make_config, make_group


class TestRawDemand:
    def test_below_range_demands_heat(self):
        ctl, _, _ = make_group(make_config(), {"A": 20.9, "B": 22.0})
        assert ctl._raw_demand("A") is AcMode.HEAT

    def test_above_range_demands_cool(self):
        ctl, _, _ = make_group(make_config(), {"A": 24.1, "B": 22.0})
        assert ctl._raw_demand("A") is AcMode.COOL

    def test_in_range_demands_nothing(self):
        ctl, _, _ = make_group(make_config(), {"A": 22.0, "B": 22.0})
        assert ctl._raw_demand("A") is None

    def test_no_reading_demands_nothing(self):
        ctl, _, _ = make_group(make_config(), {"A": None, "B": 22.0})
        assert ctl._raw_demand("A") is None

    def test_heat_latch_while_running(self):
        # Past the boundary but not yet past boundary+hysteresis: still
        # demands while the unit runs for HEAT, satisfied once it isn't.
        ctl, _, _ = make_group(make_config(), {"A": 21.2, "B": 22.0})
        ctl._state.rooms["A"].running_for = AcMode.HEAT
        assert ctl._raw_demand("A") is AcMode.HEAT
        ctl._state.rooms["A"].running_for = None
        assert ctl._raw_demand("A") is None

    def test_cool_latch_while_running(self):
        ctl, _, _ = make_group(make_config(), {"A": 23.8, "B": 22.0})
        ctl._state.rooms["A"].running_for = AcMode.COOL
        assert ctl._raw_demand("A") is AcMode.COOL
        ctl._state.rooms["A"].running_for = None
        assert ctl._raw_demand("A") is None


class TestDebounce:
    def test_adoption_seeds_demand_immediately(self):
        ctl, _, _ = make_group(make_config(), {"A": 20.0, "B": 22.0})
        room = ctl._state.rooms["A"]
        assert room.demand is AcMode.HEAT
        assert room.demand_temp == 20.0

    def test_single_glitch_is_ignored(self):
        ctl, units, _ = make_group(make_config(), {"A": 22.0, "B": 22.0})
        room = ctl._state.rooms["A"]
        units["A"].current_temperature = 20.0  # glitch sample
        ctl._update_demands()
        assert room.demand is None
        assert room.demand_candidate is AcMode.HEAT
        assert room.demand_streak == 1
        units["A"].current_temperature = 22.0  # back to normal
        ctl._update_demands()
        assert room.demand is None
        assert room.demand_candidate is None
        assert room.demand_streak == 0

    def test_persistent_change_latches(self):
        ctl, units, _ = make_group(make_config(), {"A": 22.0, "B": 22.0})
        room = ctl._state.rooms["A"]
        units["A"].current_temperature = 20.0
        ctl._update_demands()
        units["A"].current_temperature = 20.1
        ctl._update_demands()  # second consecutive poll: demand is real
        assert room.demand is AcMode.HEAT
        assert room.demand_temp == 20.1  # the confirming reading
        assert room.demand_candidate is None
        assert room.demand_streak == 0

    def test_candidate_flip_resets_streak(self):
        ctl, units, _ = make_group(make_config(), {"A": 22.0, "B": 22.0})
        room = ctl._state.rooms["A"]
        units["A"].current_temperature = 20.0
        ctl._update_demands()
        units["A"].current_temperature = 25.0  # different change: start over
        ctl._update_demands()
        assert room.demand is None
        assert room.demand_candidate is AcMode.COOL
        assert room.demand_streak == 1
        units["A"].current_temperature = 25.0
        ctl._update_demands()
        assert room.demand is AcMode.COOL

    def test_persist_one_latches_immediately(self):
        ctl, units, _ = make_group(
            make_config(demand_persist_polls=1), {"A": 22.0, "B": 22.0}
        )
        units["A"].current_temperature = 20.0
        ctl._update_demands()
        assert ctl._state.rooms["A"].demand is AcMode.HEAT

    def test_return_to_no_demand_is_also_debounced(self):
        ctl, units, _ = make_group(make_config(), {"A": 20.0, "B": 22.0})
        room = ctl._state.rooms["A"]
        assert room.demand is AcMode.HEAT  # seeded at adoption
        units["A"].current_temperature = 22.0
        ctl._update_demands()
        assert room.demand is AcMode.HEAT  # one in-range poll isn't enough
        ctl._update_demands()
        assert room.demand is None
        assert room.demand_temp is None
