"""Golden replay: 3 real days through the policy must reproduce the same
decision sequence.

The fixture (2026-07-12 → 2026-07-15, exported from history.db) contains
several group mode flips and six shutdown-window boundaries. The config is
pinned HERE as literals — the live config.toml changing must not silently
change this test.

When a behaviour change is intentional, regenerate the golden file:

    python - <<'EOF'
    from tests.test_replay_regression import regenerate
    regenerate()
    EOF

and review the diff like any other code change.
"""

from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from climate_service import GroupConfig, RoomConfig
from conftest import make_config
from replay import load_feed_csv, run_replay

FIXTURES = Path(__file__).parent / "fixtures"
FIXTURE = FIXTURES / "replay_2026-07-12_3d.csv.gz"
GOLDEN = FIXTURES / "replay_2026-07-12_3d.expected.txt"

TZ = ZoneInfo("Australia/Brisbane")  # the Pi's timezone; no DST
START = datetime(2026, 7, 12, tzinfo=TZ).timestamp()
END = datetime(2026, 7, 15, tzinfo=TZ).timestamp()


def pinned_config():
    """The house config as of 2026-08-03, as literals (see module docstring).

    Predates the per-mode split (2026-09): equal hysteresis/boost for both
    modes, heating on everywhere, and no fan-speed control — so the golden
    decision log also proves those additions are behaviour-preserving at
    their legacy values.
    """
    return make_config(
        poll_interval=30.0,
        heat_hysteresis=0.4,
        cool_hysteresis=0.4,
        demand_persist_polls=2,
        min_mode_dwell=60 * 60.0,
        min_power_toggle=10 * 60.0,
        manage_setpoints=True,
        heat_setpoint_boost=1.0,
        cool_setpoint_boost=1.0,
        heat_fan_speed=None,
        cool_fan_speed=None,
        pending_off_fan_speed=None,
        history_interval=60.0,
        shutdown_windows=((1200, 480),),  # 20:00–08:00
        groups=(
            GroupConfig(name="north", master="MPR", members=("MPR", "Bed 3", "Bed 4")),
            GroupConfig(
                name="south", master="Study",
                members=("Study", "Living", "Master", "Bed 2"),
            ),
        ),
        rooms={
            "MPR": RoomConfig(23.0, 24.8),
            "Bed 3": RoomConfig(23.0, 24.8),
            "Bed 4": RoomConfig(23.0, 26.0),
            "Study": RoomConfig(23.0, 24.8),
            "Living": RoomConfig(22.6, 24.8),
            "Master": RoomConfig(22.2, 24.8),
            "Bed 2": RoomConfig(22.2, 24.8),
        },
    )


def replay_lines():
    result = run_replay(pinned_config(), load_feed_csv(FIXTURE), START, END, TZ)
    lines = [
        f"{int(ts)} {unit} {verb} {value}" for ts, unit, verb, value in result.events
    ]
    return result, lines


def regenerate():
    _, lines = replay_lines()
    GOLDEN.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"wrote {len(lines)} events to {GOLDEN}")


def test_replay_matches_golden_decision_log():
    result, lines = replay_lines()
    assert lines == GOLDEN.read_text(encoding="utf-8").splitlines()


def test_replay_coarse_shape():
    # Coarse counts so golden-file diffs stay reviewable: if these move, the
    # change is behavioural, not cosmetic.
    result, _ = replay_lines()
    # 3 days at 30s; the very first tick precedes the fixture's first sample
    # (the export starts exactly at the window edge) and counts as a gap.
    assert result.ticks == 8639
    assert result.gap_ticks == 1
    assert result.metrics["north"].mode_flips == 5
    assert result.metrics["south"].mode_flips == 5
    assert result.metrics["north"].compressor_starts == 13
    assert result.metrics["south"].compressor_starts == 6
