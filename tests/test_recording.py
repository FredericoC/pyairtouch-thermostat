"""HistoryRecorder sampling and WeatherStation freshness."""

import sqlite3
from dataclasses import dataclass

import pytest
from pyairtouch import AcMode, AcPowerState

from climate_service import WEATHER_STALE_AFTER, HistoryRecorder, WeatherStation
from conftest import make_config, make_group


class TestHistoryRecorder:
    @pytest.fixture
    def recorder(self, tmp_path):
        rec = HistoryRecorder(tmp_path / "history.db", interval=60.0)
        yield rec
        rec.close()

    def rows(self, tmp_path, table="readings"):
        conn = sqlite3.connect(tmp_path / "history.db")
        try:
            order = "ts, unit" if table == "readings" else "ts"
            return conn.execute(f"SELECT * FROM {table} ORDER BY {order}").fetchall()
        finally:
            conn.close()

    def test_records_then_waits_out_the_interval(self, tmp_path, recorder):
        ctl, _, _ = make_group(make_config(), {"A": 20.0, "B": 22.0})
        recorder.maybe_record(1000.0, [ctl])
        assert len(self.rows(tmp_path)) == 2  # one row per unit
        recorder.maybe_record(1059.9, [ctl])
        assert len(self.rows(tmp_path)) == 2  # inside the interval: skipped
        recorder.maybe_record(1060.0, [ctl])
        assert len(self.rows(tmp_path)) == 4

    def test_row_content_matches_history_rows(self, tmp_path, recorder):
        ctl, _, _ = make_group(
            make_config(),
            {"A": 20.0, "B": 22.0},
            modes={"A": AcMode.HEAT},
            powers={"A": AcPowerState.ON},
            setpoints={"A": 23.0},
        )
        recorder.maybe_record(1000.0, [ctl])
        stored = self.rows(tmp_path)
        assert [row[1:] for row in stored] == ctl.history_rows()
        assert stored[0][1:] == ("A", 20.0, 23.0, 1, "HEAT", "heating")
        ts = {row[0] for row in stored}
        assert len(ts) == 1 and isinstance(ts.pop(), int)  # wall-clock epoch

    def test_suspended_flag_reaches_history_rows(self, tmp_path, recorder):
        ctl, units, _ = make_group(
            make_config(), {"A": 20.0, "B": 22.0}, modes={"A": AcMode.HEAT}
        )
        ctl._state.rooms["A"].running_for = None
        units["A"].power_state = AcPowerState.ON
        recorder.maybe_record(1000.0, [ctl], suspended=True)
        assert self.rows(tmp_path)[0][6] == "heating (manual)"

    def test_weather_row_only_when_sampled(self, tmp_path, recorder):
        ctl, _, _ = make_group(make_config(), {"A": 20.0, "B": 22.0})
        recorder.maybe_record(1000.0, [ctl], weather=None)
        assert self.rows(tmp_path, "weather") == []
        recorder.maybe_record(2000.0, [ctl], weather=(12.5, None))
        weather = self.rows(tmp_path, "weather")
        assert len(weather) == 1
        assert weather[0][1:] == (12.5, None)

    def test_multiple_controllers_in_one_sample(self, tmp_path, recorder):
        cfg = make_config()
        ctl1, _, _ = make_group(cfg, {"A": 20.0, "B": 22.0})
        ctl2, _, _ = make_group(cfg, {"A": 21.0, "B": 23.0})
        recorder.maybe_record(1000.0, [ctl1, ctl2])
        assert len(self.rows(tmp_path)) == 4

    def test_reopening_existing_database_keeps_rows(self, tmp_path):
        ctl, _, _ = make_group(make_config(), {"A": 20.0, "B": 22.0})
        rec = HistoryRecorder(tmp_path / "history.db", interval=60.0)
        rec.maybe_record(1000.0, [ctl])
        rec.close()
        rec = HistoryRecorder(tmp_path / "history.db", interval=60.0)  # IF NOT EXISTS
        rec.close()
        assert len(self.rows(tmp_path)) == 2


@dataclass
class FakeSensor:
    key: str
    value: object
    last_update_m: float


class FakeListener:
    def __init__(self, *sensors: FakeSensor) -> None:
        self.sensors = {f"{s.key}_{i}": s for i, s in enumerate(sensors)}
        self.started = False
        self.stopped = False

    async def start(self) -> None:
        self.started = True

    async def stop(self) -> None:
        self.stopped = True


def station_with(*sensors: FakeSensor) -> WeatherStation:
    station = WeatherStation(port=0, path="/data/report/")
    station._listener = FakeListener(*sensors)
    return station


class TestWeatherSample:
    NOW = 10_000.0

    def test_no_listener(self):
        assert WeatherStation(port=0, path="/").sample(self.NOW) is None

    def test_fresh_values(self):
        station = station_with(
            FakeSensor("tempc", "12.5", self.NOW - 30),
            FakeSensor("solarradiation", 300, self.NOW - 30),
        )
        assert station.sample(self.NOW) == (12.5, 300.0)

    def test_stale_value_becomes_none(self):
        station = station_with(
            FakeSensor("tempc", 12.5, self.NOW - WEATHER_STALE_AFTER - 1),
            FakeSensor("solarradiation", 300, self.NOW - WEATHER_STALE_AFTER),
        )
        assert station.sample(self.NOW) == (None, 300.0)  # boundary is inclusive

    def test_all_stale_is_none(self):
        station = station_with(
            FakeSensor("tempc", 12.5, self.NOW - 3600),
            FakeSensor("solarradiation", 300, self.NOW - 3600),
        )
        assert station.sample(self.NOW) is None

    def test_none_value_and_unrelated_keys_ignored(self):
        station = station_with(
            FakeSensor("tempc", None, self.NOW),
            FakeSensor("humidity", 55, self.NOW),
            FakeSensor("solarradiation", 0, self.NOW),
        )
        assert station.sample(self.NOW) == (None, 0.0)


class TestWeatherStartStop:
    async def test_start_failure_disables_weather(self, monkeypatch):
        import aioecowitt

        class Failing:
            def __init__(self, *, port, path):
                pass

            async def start(self):
                raise OSError(48, "address in use")

        monkeypatch.setattr(aioecowitt, "EcoWittListener", Failing)
        station = WeatherStation(port=8090, path="/data/report/")
        await station.start()  # logs, doesn't raise
        assert station._listener is None
        assert station.sample(0.0) is None

    async def test_start_then_stop(self, monkeypatch):
        import aioecowitt

        created = []

        class Ok(FakeListener):
            def __init__(self, *, port, path):
                super().__init__()
                self.args = (port, path)
                created.append(self)

        monkeypatch.setattr(aioecowitt, "EcoWittListener", Ok)
        station = WeatherStation(port=8090, path="/data/report/")
        await station.start()
        assert created[0].args == (8090, "/data/report/")
        assert created[0].started
        await station.stop()
        assert created[0].stopped
        assert station._listener is None
        await station.stop()  # idempotent
