"""Dashboard override file and the service's override/shutdown state."""

import json
from datetime import datetime

from climate_service import ClimateService, read_control_override
from conftest import make_config


def write_override(tmp_path, payload) -> "Path":
    path = tmp_path / "control_override.json"
    path.write_text(json.dumps(payload) if not isinstance(payload, str) else payload)
    return path


class TestReadControlOverride:
    def test_missing_file(self, tmp_path):
        assert read_control_override(tmp_path / "nope.json") is None

    def test_garbage_json(self, tmp_path):
        assert read_control_override(write_override(tmp_path, "{not json")) is None

    def test_non_dict(self, tmp_path):
        assert read_control_override(write_override(tmp_path, "[1, 2]")) is None

    def test_missing_expires(self, tmp_path):
        assert read_control_override(write_override(tmp_path, {"shutdown": True})) is None

    def test_pause_ignores_expiry(self, tmp_path):
        path = write_override(tmp_path, {"pause": True})
        assert read_control_override(path, now=1e12) == {"pause": True}

    def test_valid_shutdown_override(self, tmp_path):
        path = write_override(tmp_path, {"shutdown": False, "expires": 1000.0})
        assert read_control_override(path, now=999.0) == {
            "shutdown": False,
            "expires": 1000.0,
        }

    def test_expired(self, tmp_path):
        path = write_override(tmp_path, {"shutdown": False, "expires": 1000.0})
        assert read_control_override(path, now=1000.0) is None  # >= is expired


class TestOverrideState:
    WINDOWS = ((1200, 480),)  # 20:00–08:00

    def make_service(self, tmp_path, **overrides) -> ClimateService:
        cfg = make_config(
            shutdown_windows=self.WINDOWS,
            override_path=tmp_path / "control_override.json",
            **overrides,
        )
        return ClimateService(cfg)

    def test_schedule_only(self, tmp_path):
        service = self.make_service(tmp_path)
        override, paused, shutdown = service._override_state(datetime(2026, 1, 1, 23, 0))
        assert (override, paused, shutdown) == (None, False, True)
        override, paused, shutdown = service._override_state(datetime(2026, 1, 1, 12, 0))
        assert (override, paused, shutdown) == (None, False, False)

    def test_shutdown_override_beats_schedule(self, tmp_path):
        service = self.make_service(tmp_path)
        local_now = datetime(2026, 1, 1, 23, 0)
        write_override(
            tmp_path, {"shutdown": False, "expires": local_now.timestamp() + 3600}
        )
        override, paused, shutdown = service._override_state(local_now)
        assert override == {"shutdown": False, "expires": local_now.timestamp() + 3600}
        assert not paused
        assert not shutdown  # inside the window, but overridden on

    def test_expired_override_falls_back_to_schedule(self, tmp_path):
        service = self.make_service(tmp_path)
        local_now = datetime(2026, 1, 1, 23, 0)
        write_override(
            tmp_path, {"shutdown": False, "expires": local_now.timestamp() - 1}
        )
        override, paused, shutdown = service._override_state(local_now)
        assert override is None
        assert shutdown

    def test_pause_leaves_schedule_untouched(self, tmp_path):
        service = self.make_service(tmp_path)
        write_override(tmp_path, {"pause": True})
        override, paused, shutdown = service._override_state(datetime(2026, 1, 1, 23, 0))
        assert paused
        assert shutdown  # scheduled state reported as-is, not overridden


class TestRestartMidWindow:
    def test_restart_inside_window_skips_off_pass(self, tmp_path):
        # 00:00–24:00 → always inside a window regardless of the wall clock,
        # which __init__ consults for the seeding.
        cfg = make_config(
            shutdown_windows=((0, 1440),),
            override_path=tmp_path / "control_override.json",
        )
        assert ClimateService(cfg)._was_shutdown  # off pass assumed already run

    def test_start_outside_window(self, tmp_path):
        cfg = make_config(
            shutdown_windows=(),
            override_path=tmp_path / "control_override.json",
        )
        assert not ClimateService(cfg)._was_shutdown
