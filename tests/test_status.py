"""history_rows and status_report rendering."""

from pyairtouch import AcMode, AcPowerState

from conftest import make_config, make_group


def rows_by_unit(ctl, **kwargs):
    return {row[0]: row for row in ctl.history_rows(**kwargs)}


class TestHistoryRows:
    def test_running_unit_reports_activity(self):
        ctl, _, _ = make_group(
            make_config(),
            {"A": 20.0, "B": 22.0},
            modes={"A": AcMode.HEAT},
            powers={"A": AcPowerState.ON},
        )
        row = rows_by_unit(ctl)["A"]
        assert row == ("A", 20.0, None, 1, "HEAT", "heating")

    def test_manual_run_while_suspended(self):
        ctl, units, _ = make_group(
            make_config(),
            {"A": 22.0, "B": 22.0},
            modes={"A": AcMode.COOL},
        )
        ctl._state.rooms["A"].running_for = None
        units["A"].power_state = AcPowerState.ON
        row = rows_by_unit(ctl, suspended=True)["A"]
        assert row[5] == "cooling (manual)"

    def test_on_without_known_activity(self):
        ctl, units, _ = make_group(make_config(), {"A": 22.0, "B": 22.0})
        units["A"].power_state = AcPowerState.ON
        units["A"].active_mode = AcMode.FAN  # not heat/cool: activity unknown
        ctl._state.rooms["A"].running_for = None
        assert rows_by_unit(ctl, suspended=True)["A"][5] == "on"
        assert rows_by_unit(ctl)["A"][5] == "on"

    def test_off_is_idle(self):
        ctl, _, _ = make_group(make_config(), {"A": 22.0, "B": 22.0})
        row = rows_by_unit(ctl)["A"]
        assert row == ("A", 22.0, None, 0, None, "idle")


class TestStatusReport:
    def make(self):
        return make_group(
            make_config(),
            {"A": 20.0, "B": 22.0},
            modes={"A": AcMode.HEAT},
            powers={"A": AcPowerState.ON},
        )

    def test_signature_ignores_temperature_drift(self):
        ctl, units, _ = self.make()
        sig1, _ = ctl.status_report(now=0.0)
        units["A"].current_temperature = 20.4
        units["B"].current_temperature = 22.3
        sig2, _ = ctl.status_report(now=100.0)
        assert sig1 == sig2

    def test_signature_tracks_power_change(self):
        ctl, units, _ = self.make()
        sig1, _ = ctl.status_report(now=0.0)
        units["A"].power_state = AcPowerState.OFF
        ctl._state.rooms["A"].running_for = None
        sig2, _ = ctl.status_report(now=0.0)
        assert sig1 != sig2

    def test_running_line_shows_threshold(self):
        ctl, _, _ = self.make()
        _, lines = ctl.status_report(now=0.0)
        line_a = next(line for line in lines if line.strip().startswith("A"))
        assert "heating to 21.4°C" in line_a  # target_low + hysteresis

    def test_suspended_rendering(self):
        ctl, _, _ = self.make()
        _, lines = ctl.status_report(now=0.0, suspended=True)
        line_a = next(line for line in lines if line.strip().startswith("A"))
        assert "on manually (control suspended)" in line_a
        line_b = next(line for line in lines if line.strip().startswith("B"))
        assert "control suspended" in line_b

    def test_waiting_for_mode_switch(self):
        ctl, _, _ = make_group(
            make_config(),
            {"A": 22.0, "B": 25.0},  # B wants COOL, group is heating
            modes={"A": AcMode.HEAT},
        )
        ctl._state.last_mode_change = 0.0
        _, lines = ctl.status_report(now=60.0)
        line_b = next(line for line in lines if line.strip().startswith("B"))
        assert "needs COOL, waiting for mode switch" in line_b
        assert "mode dwell" in line_b
