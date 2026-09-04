"""Parsers, shutdown windows and load_config validation."""

from datetime import datetime

import pytest
from pyairtouch import AcFanSpeed, AcMode

from climate_service import (
    _parse_shutdown_window,
    _parse_time_of_day,
    load_config,
)
from conftest import make_config


class TestParseTimeOfDay:
    @pytest.mark.parametrize(
        ("text", "minutes"),
        [
            ("00:00", 0),
            ("08:05", 485),
            ("9:05", 545),
            ("23:59", 1439),
            (" 20:00 ", 1200),
        ],
    )
    def test_valid(self, text, minutes):
        assert _parse_time_of_day(text) == minutes

    @pytest.mark.parametrize(
        "text",
        ["0800", "9:5", "08:60", "25:00", "24:01", "a:bc", "-1:00", ""],
    )
    def test_invalid(self, text):
        with pytest.raises(ValueError):
            _parse_time_of_day(text)

    def test_2400_needs_flag(self):
        with pytest.raises(ValueError):
            _parse_time_of_day("24:00")
        assert _parse_time_of_day("24:00", allow_2400=True) == 1440


class TestParseShutdownWindow:
    def test_simple(self):
        assert _parse_shutdown_window("13:00-15:30") == (780, 930)

    def test_midnight_crossing(self):
        assert _parse_shutdown_window("20:00-08:00") == (1200, 480)

    def test_all_day(self):
        assert _parse_shutdown_window("00:00-24:00") == (0, 1440)

    def test_missing_dash(self):
        with pytest.raises(ValueError, match="expected"):
            _parse_shutdown_window("20:00")

    def test_zero_length(self):
        with pytest.raises(ValueError, match="zero length"):
            _parse_shutdown_window("08:00-08:00")


class TestShutdownActive:
    def test_simple_window(self):
        cfg = make_config(shutdown_windows=((600, 720),))  # 10:00–12:00
        assert not cfg.shutdown_active(datetime(2026, 1, 1, 9, 59))
        assert cfg.shutdown_active(datetime(2026, 1, 1, 10, 0))  # start inclusive
        assert cfg.shutdown_active(datetime(2026, 1, 1, 11, 30))
        assert not cfg.shutdown_active(datetime(2026, 1, 1, 12, 0))  # end exclusive

    def test_midnight_crossing_window(self):
        cfg = make_config(shutdown_windows=((1200, 480),))  # 20:00–08:00
        assert cfg.shutdown_active(datetime(2026, 1, 1, 23, 0))
        assert cfg.shutdown_active(datetime(2026, 1, 1, 3, 0))
        assert cfg.shutdown_active(datetime(2026, 1, 1, 20, 0))
        assert not cfg.shutdown_active(datetime(2026, 1, 1, 8, 0))
        assert not cfg.shutdown_active(datetime(2026, 1, 1, 12, 0))

    def test_no_windows(self):
        assert not make_config().shutdown_active(datetime(2026, 1, 1, 12, 0))


class TestNextShutdownBoundary:
    CFG = make_config(shutdown_windows=((1200, 480),))  # 20:00–08:00

    def test_before_window_start(self):
        boundary = self.CFG.next_shutdown_boundary(datetime(2026, 1, 1, 12, 0, 30))
        assert boundary == datetime(2026, 1, 1, 20, 0)  # seconds zeroed too

    def test_inside_window(self):
        boundary = self.CFG.next_shutdown_boundary(datetime(2026, 1, 1, 21, 0))
        assert boundary == datetime(2026, 1, 2, 8, 0)

    def test_exactly_at_boundary_rolls_over(self):
        # A boundary at the current minute counts as tomorrow's occurrence,
        # so the *other* boundary is nearest.
        boundary = self.CFG.next_shutdown_boundary(datetime(2026, 1, 1, 20, 0))
        assert boundary == datetime(2026, 1, 2, 8, 0)

    def test_no_windows(self):
        assert make_config().next_shutdown_boundary(datetime(2026, 1, 1)) is None


BASE_TOML = """
[service]
host = "1.2.3.4"

[defaults]
target_low = 21
target_high = 24

[groups.g]
master = "A"
members = ["A", "B"]
"""


def write_config(tmp_path, text):
    path = tmp_path / "config.toml"
    path.write_text(text, encoding="utf-8")
    return path


class TestLoadConfig:
    def test_happy_path(self, tmp_path):
        cfg = load_config(
            write_config(
                tmp_path,
                BASE_TOML
                + """
[shutdown]
windows = ["20:00-08:00"]

[rooms.B]
target_low = 20
target_high = 25
""",
            )
        )
        assert cfg.host == "1.2.3.4"
        assert cfg.groups[0].master == "A"
        assert cfg.rooms["A"].target_low == 21.0  # defaults applied
        assert cfg.rooms["B"] .target_high == 25.0  # per-room override
        assert cfg.shutdown_windows == ((1200, 480),)
        assert cfg.min_mode_dwell == 60 * 60  # minutes converted to seconds
        assert cfg.min_power_toggle == 10 * 60
        # Relative paths resolve beside the config file.
        assert cfg.history_path == tmp_path / "history.db"
        assert cfg.log_path == tmp_path / "climate.log"
        assert cfg.override_path == tmp_path / "control_override.json"

    def test_no_groups(self, tmp_path):
        with pytest.raises(ValueError, match="no .groups"):
            load_config(write_config(tmp_path, "[defaults]\ntarget_low = 21\n"))

    def test_master_not_member(self, tmp_path):
        toml = BASE_TOML.replace('master = "A"', 'master = "Z"')
        with pytest.raises(ValueError, match="must be a member"):
            load_config(write_config(tmp_path, toml))

    def test_room_override_for_unknown_member(self, tmp_path):
        with pytest.raises(ValueError, match="does not match any group member"):
            load_config(write_config(tmp_path, BASE_TOML + "[rooms.Z]\ntarget_low = 20\n"))

    def test_low_not_below_high(self, tmp_path):
        toml = BASE_TOML + "[rooms.B]\ntarget_low = 25\ntarget_high = 24\n"
        with pytest.raises(ValueError, match="target_low must be"):
            load_config(write_config(tmp_path, toml))

    def test_range_narrower_than_hysteresis(self, tmp_path):
        toml = BASE_TOML + "[rooms.B]\ntarget_low = 23.5\ntarget_high = 24\n"
        with pytest.raises(ValueError, match="too\\s+narrow"):
            load_config(write_config(tmp_path, toml))

    def test_bad_windows_type(self, tmp_path):
        toml = BASE_TOML + '[shutdown]\nwindows = "20:00-08:00"\n'
        with pytest.raises(ValueError, match="list"):
            load_config(write_config(tmp_path, toml))

    def test_disabled_sections_collapse_to_none(self, tmp_path):
        cfg = load_config(
            write_config(
                tmp_path,
                BASE_TOML
                + """
[shutdown]
enabled = false
windows = ["20:00-08:00"]

[history]
enabled = false

[weather]
enabled = false
""",
            )
        )
        assert cfg.shutdown_windows == ()
        assert cfg.history_path is None
        assert cfg.weather_port is None

    def test_empty_log_file_disables_file_logging(self, tmp_path):
        toml = BASE_TOML.replace('host = "1.2.3.4"', 'host = "1.2.3.4"\nlog_file = ""')
        cfg = load_config(write_config(tmp_path, toml))
        assert cfg.log_path is None

    def test_demand_persist_clamped_to_one(self, tmp_path):
        toml = BASE_TOML.replace("target_low = 21", "target_low = 21\ndemand_persist_polls = 0")
        cfg = load_config(write_config(tmp_path, toml))
        assert cfg.demand_persist_polls == 1


class TestPerModeTuning:
    """heat_/cool_ hysteresis and boost, the heating switch, fan speeds."""

    def test_legacy_keys_apply_to_both_modes(self, tmp_path):
        toml = BASE_TOML.replace(
            "target_low = 21", "target_low = 21\nhysteresis = 0.6\nsetpoint_boost = 1"
        )
        cfg = load_config(write_config(tmp_path, toml))
        assert (cfg.heat_hysteresis, cfg.cool_hysteresis) == (0.6, 0.6)
        assert (cfg.heat_setpoint_boost, cfg.cool_setpoint_boost) == (1.0, 1.0)

    def test_defaults(self, tmp_path):
        cfg = load_config(write_config(tmp_path, BASE_TOML))
        assert (cfg.heat_hysteresis, cfg.cool_hysteresis) == (0.4, 0.4)
        assert (cfg.heat_setpoint_boost, cfg.cool_setpoint_boost) == (0.0, 0.0)
        assert cfg.heat_fan_speed is AcFanSpeed.AUTO
        assert cfg.cool_fan_speed is AcFanSpeed.MEDIUM
        assert cfg.pending_off_fan_speed is AcFanSpeed.QUIET
        assert all(room.heating for room in cfg.rooms.values())

    def test_per_mode_keys_win(self, tmp_path):
        toml = BASE_TOML.replace(
            "target_low = 21",
            "target_low = 21\nhysteresis = 0.4\ncool_hysteresis = 1.2\n"
            "setpoint_boost = 1\ncool_setpoint_boost = 0",
        )
        cfg = load_config(write_config(tmp_path, toml))
        assert (cfg.heat_hysteresis, cfg.cool_hysteresis) == (0.4, 1.2)
        assert (cfg.heat_setpoint_boost, cfg.cool_setpoint_boost) == (1.0, 0.0)
        assert cfg.hysteresis(AcMode.COOL) == 1.2
        assert cfg.setpoint_boost(AcMode.HEAT) == 1.0

    def test_negative_hysteresis_rejected(self, tmp_path):
        toml = BASE_TOML.replace("target_low = 21", "target_low = 21\ncool_hysteresis = -1")
        with pytest.raises(ValueError, match=">= 0"):
            load_config(write_config(tmp_path, toml))

    def test_cool_hysteresis_may_not_cross_heating_off_threshold(self, tmp_path):
        # 21–24 with heat 0.4: heating-off 21.4; cool 2.6 → cooling-off 21.4.
        toml = BASE_TOML.replace("target_low = 21", "target_low = 21\ncool_hysteresis = 2.6")
        with pytest.raises(ValueError, match="cooling-off threshold"):
            load_config(write_config(tmp_path, toml))

    def test_heating_off_relaxes_the_check_to_the_range_bottom(self, tmp_path):
        toml = BASE_TOML.replace(
            "target_low = 21", "target_low = 21\nheating = false\ncool_hysteresis = 2.6"
        )
        cfg = load_config(write_config(tmp_path, toml))  # 24 - 2.6 = 21.4 > 21
        assert cfg.cool_hysteresis == 2.6
        toml = BASE_TOML.replace(
            "target_low = 21", "target_low = 21\nheating = false\ncool_hysteresis = 3"
        )
        with pytest.raises(ValueError, match="bottom of the range"):
            load_config(write_config(tmp_path, toml))

    def test_heating_switch_global_and_per_room(self, tmp_path):
        toml = BASE_TOML.replace("target_low = 21", "target_low = 21\nheating = false")
        toml += "\n[rooms.B]\nheating = true\n"
        cfg = load_config(write_config(tmp_path, toml))
        assert cfg.rooms["A"].heating is False
        assert cfg.rooms["B"].heating is True

    @pytest.mark.parametrize(
        ("key", "value", "expected"),
        [
            ("cool_fan_speed", '"low"', AcFanSpeed.LOW),
            ("cool_fan_speed", '"High"', AcFanSpeed.HIGH),  # case-insensitive
            ("cool_fan_speed", '"keep"', None),
            ("heat_fan_speed", '"keep"', None),
            ("pending_off_fan_speed", '"off"', None),
            ("pending_off_fan_speed", '"low"', AcFanSpeed.LOW),
        ],
    )
    def test_fan_speed_values(self, tmp_path, key, value, expected):
        toml = BASE_TOML.replace("target_low = 21", f"target_low = 21\n{key} = {value}")
        cfg = load_config(write_config(tmp_path, toml))
        assert getattr(cfg, key) is expected

    @pytest.mark.parametrize(
        ("key", "value"),
        [
            ("cool_fan_speed", '"off"'),  # "off" is the pending-off word, not keep
            ("pending_off_fan_speed", '"keep"'),
            ("cool_fan_speed", '"turbo-ish"'),
            ("cool_fan_speed", "3"),
        ],
    )
    def test_bad_fan_speed_rejected(self, tmp_path, key, value):
        toml = BASE_TOML.replace("target_low = 21", f"target_low = 21\n{key} = {value}")
        with pytest.raises(ValueError, match=key):
            load_config(write_config(tmp_path, toml))
