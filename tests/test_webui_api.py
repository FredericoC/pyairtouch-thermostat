"""webui.Api: bucketing, runtime math, override writes — against fixture DBs.

The data()/csv() queries filter with SQLite's unixepoch('now'), which can't
be frozen, so fixture rows are timestamped relative to the wall clock.
"""

import json
import time
from datetime import date, datetime, time as dtime, timedelta
from pathlib import Path

import pytest

from webui import Api
from conftest import make_config, make_history_db


def make_api(tmp_path, rows, weather_rows=None, *, with_weather_table=True, **cfg_overrides):
    db = tmp_path / "history.db"
    make_history_db(
        db, rows, weather_rows, with_weather_table=with_weather_table
    )
    cfg = make_config(
        history_path=db,
        override_path=tmp_path / "control_override.json",
        **cfg_overrides,
    )
    return Api(cfg)


def reading(ts, unit, temp, *, setpoint=None, power=0, mode=None, activity="idle"):
    return (int(ts), unit, temp, setpoint, power, mode, activity)


class TestData:
    def test_series_and_latest(self, tmp_path):
        now = int(time.time())
        api = make_api(
            tmp_path,
            [
                reading(now - 180, "A", 20.0, setpoint=23.0, power=1,
                        mode="HEAT", activity="heating"),
                reading(now - 60, "A", 20.5, setpoint=23.0, power=1,
                        mode="HEAT", activity="heating"),
                reading(now - 60, "B", 22.0),
            ],
        )
        data = api.data(hours=1.0)
        assert data["units"] == ["A", "B"]
        assert data["bucket"] == 60  # max(history_interval, 3600 // 700)
        assert len(data["series"]["A"]) == 2
        # Point shape [t, temp, setpoint, power, activity].
        t, temp, setpoint, power, activity = data["series"]["A"][-1]
        assert (temp, setpoint, power, activity) == (20.5, 23.0, 1, "heating")
        assert data["latest_ts"] == now - 60
        # Latest adds the unit's selected mode at index 5.
        assert data["latest"]["A"] == [now - 60, 20.5, 23.0, 1, "heating", "HEAT"]
        assert data["latest"]["B"][5] is None
        assert data["override"] is None
        assert data["ranges"]["A"] == [21.0, 24.0]

    def test_mixed_bucket_takes_highest_activity(self, tmp_path):
        now = int(time.time())
        t0 = (now // 60) * 60 - 600  # one bucket, two samples
        api = make_api(
            tmp_path,
            [
                reading(t0, "A", 20.0, power=1, activity="on"),
                reading(t0 + 20, "A", 20.2, power=0, activity="heating"),
            ],
        )
        points = api.data(hours=1.0)["series"]["A"]
        assert len(points) == 1
        assert points[0][4] == "heating"  # highest control-priority wins
        assert points[0][3] == 1  # MAX(power)

    def test_unknown_unit_dropped(self, tmp_path):
        now = int(time.time())
        api = make_api(tmp_path, [reading(now - 60, "X", 20.0)])
        data = api.data(hours=1.0)
        assert "X" not in data["series"]
        assert "X" not in data["latest"]

    def test_weather_series(self, tmp_path):
        now = int(time.time())
        api = make_api(
            tmp_path,
            [reading(now - 60, "A", 20.0)],
            weather_rows=[(now - 60, 12.5, 300.0)],
        )
        weather = api.data(hours=1.0)["weather"]
        assert len(weather) == 1
        assert tuple(weather[0][1:]) == (12.5, 300.0)

    def test_database_without_weather_table(self, tmp_path):
        now = int(time.time())
        api = make_api(
            tmp_path, [reading(now - 60, "A", 20.0)], with_weather_table=False
        )
        assert api.data(hours=1.0)["weather"] == []

    def test_requires_history_enabled(self, tmp_path):
        with pytest.raises(SystemExit):
            Api(make_config(history_path=None))


class TestStats:
    def test_runtime_gap_sum(self, tmp_path):
        # Three consecutive on-samples 60s apart: the last has no successor,
        # so runtime is the two 60s gaps.
        base = int(time.time()) - 600
        api = make_api(
            tmp_path,
            [
                reading(base, "A", 20.0, power=1, activity="heating"),
                reading(base + 60, "A", 20.1, power=1, activity="heating"),
                reading(base + 120, "A", 20.2, power=1, activity="heating"),
            ],
        )
        stats = api.stats(days=1)
        bucket = date.today().isoformat()
        on_s, heat_s, cool_s, coverage = stats["runtime"]["A"][bucket]
        assert (on_s, heat_s, cool_s) == (120, 120, 0)
        assert stats["buckets"] == [bucket]
        assert stats["bucket_seconds"] == 86400

    def test_recording_gap_capped(self, tmp_path):
        # A 10-minute silence between on-samples counts as 2× the sample
        # interval, not 10 minutes of runtime.
        base = int(time.time()) - 900
        api = make_api(
            tmp_path,
            [
                reading(base, "A", 20.0, power=1, activity="heating"),
                reading(base + 600, "A", 20.1, power=1, activity="heating"),
            ],
        )
        on_s, heat_s, *_ = api.stats(days=1)["runtime"]["A"][date.today().isoformat()]
        assert on_s == 120  # capped at 2 × history_interval
        assert heat_s == 120

    def test_local_midnight_bucket_assignment(self, tmp_path):
        yesterday = date.today() - timedelta(days=1)
        before = int(datetime.combine(date.today(), dtime.min).timestamp()) - 30
        after = before + 60
        api = make_api(
            tmp_path,
            [
                reading(before, "A", 20.0, power=1, activity="heating"),
                reading(after, "A", 20.1, power=1, activity="heating"),
            ],
        )
        runtime = api.stats(days=2)["runtime"]["A"]
        # The straddling gap belongs to the sample before midnight.
        assert runtime[yesterday.isoformat()][0] == 60
        assert runtime[date.today().isoformat()][0] == 0

    def test_hourly_buckets(self, tmp_path):
        now = datetime.now().replace(minute=0, second=0, microsecond=0)
        api = make_api(
            tmp_path,
            [
                reading(int(now.timestamp()) + 60, "A", 20.0, power=1,
                        activity="cooling"),
                reading(int(now.timestamp()) + 120, "A", 20.0, power=1,
                        activity="cooling"),
            ],
        )
        stats = api.stats(hours=2)
        expected = [
            (now - timedelta(hours=1)).strftime("%Y-%m-%dT%H:00"),
            now.strftime("%Y-%m-%dT%H:00"),
        ]
        assert stats["buckets"] == expected
        assert stats["bucket_seconds"] == 3600
        on_s, heat_s, cool_s, _ = stats["runtime"]["A"][expected[1]]
        assert (on_s, heat_s, cool_s) == (60, 0, 60)


class TestSetOverride:
    WINDOWS = ((1200, 480),)  # 20:00–08:00

    def make(self, tmp_path):
        return make_api(tmp_path, [], shutdown_windows=self.WINDOWS)

    def test_pause(self, tmp_path):
        api = self.make(tmp_path)
        assert api.set_override({"pause": True}) == {"pause": True}
        path = tmp_path / "control_override.json"
        assert json.loads(path.read_text()) == {"pause": True}
        assert not path.with_name(path.name + ".tmp").exists()

    def test_shutdown_expires_at_next_boundary(self, tmp_path):
        api = self.make(tmp_path)
        override = api.set_override({"shutdown": True})
        expected = api._cfg.next_shutdown_boundary(datetime.now()).timestamp()
        assert override["shutdown"] is True
        assert abs(override["expires"] - expected) <= 61  # minute rollover slack

    def test_clear(self, tmp_path):
        api = self.make(tmp_path)
        api.set_override({"pause": True})
        assert api.set_override({"shutdown": None}) is None
        assert not (tmp_path / "control_override.json").exists()

    def test_rejects_non_bool(self, tmp_path):
        api = self.make(tmp_path)
        with pytest.raises(ValueError):
            api.set_override({"pause": "yes"})
        with pytest.raises(ValueError):
            api.set_override({"shutdown": 1})

    def test_shutdown_without_windows(self, tmp_path):
        api = make_api(tmp_path, [], shutdown_windows=())
        with pytest.raises(ValueError, match="no shutdown windows"):
            api.set_override({"shutdown": True})


class TestCsvAndLog:
    def test_csv(self, tmp_path):
        now = int(time.time())
        api = make_api(
            tmp_path,
            [reading(now - 60, "A", 20.0, power=1, mode="HEAT", activity="heating")],
        )
        lines = api.csv(hours=1.0).strip().splitlines()
        assert lines[0] == "ts,unit,temperature,setpoint,power,mode,activity"
        assert len(lines) == 2
        assert lines[1].startswith(f"{now - 60},A,20.0")

    def test_log_tail(self, tmp_path):
        log = tmp_path / "climate.log"
        log.write_text("\n".join(f"line {i}" for i in range(10)))
        api = make_api(tmp_path, [], log_path=log)
        assert api.log_tail(3) == "line 7\nline 8\nline 9"

    def test_log_tail_disabled(self, tmp_path):
        api = make_api(tmp_path, [], log_path=None)
        assert "disabled" in api.log_tail(10)

    def test_log_tail_missing_file(self, tmp_path):
        api = make_api(tmp_path, [], log_path=tmp_path / "nope.log")
        assert "not found" in api.log_tail(10)
