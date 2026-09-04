# Plan: Summer efficiency — fewer compressor starts, optional no-heat

Status: **approved 4 Sep 2026; proposals 1, 3 and 5 implemented** (not yet
deployed to the Pi). Proposals 2 and 4 remain ideas only. Replay of
26 Aug–3 Sep with the new config as a variant: north compressor starts
101 → 15, mode flips 16 → 0 (decision-level only, see the replay caveat).
Data examined: `history.db` (2 Jul – 2 Sep 2026), `climate.log` (30 Aug – 2 Sep).
Independent of `QUIET_MODE_PLAN.md`; neither depends on the other.

## What the data shows

**Cooling is short-cycling, and it's getting worse with the sun.** Unit starts
per week (from `readings` power transitions):

| week (ISO) | Bed 3 cool starts | MPR cool starts | north-group mode flips |
|---|---|---|---|
| 30 (late Jul) | 24 | 13 | 20 |
| 32 | 49 | 16 | 13 |
| 34 (late Aug) | 64 | 29 | 10 |
| 35 (partial) | 24 | 16 | 6 |

Last 3 weeks, cooling runs: Bed 3 had 138 runs averaging 10.7 min, 135 of them
under 20 min; MPR 66 runs averaging 9.6 min, 34 under 10 min. Study's cooling
runs are exactly 10.0 min long — the room is satisfied within a couple of
minutes and the unit is only kept on by the 10-minute compressor hold, parked
at an idle setpoint. Off gaps between Bed 3 runs average 38 min, 57 of them
under 15 min. The north outdoor unit started 10–16 times a day from 26 Aug
to 2 Sep (south: 4–9).

A typical afternoon (Bed 3, 28 Aug): cool on at 24.9, off at 24.4 six to
eight minutes later, sun brings the room back to 24.9 in 10–15 min, repeat —
14 starts between 11:12 and 17:07 at 25-minute intervals, while outdoor was
32–34 °C and solar 600–900 W/m². `climate.log` for 1 Sep shows the same
alternating Bed 3 / MPR pattern every ~25 min from 13:00 onwards.

**Heating is now a daily morning top-up that gets undone by lunchtime.**
Rooms sit at 20.7–22 °C at 08:00 (end of the shutdown window), every unit
heats for ~40 min right at 08:00 (200–315 unit-minutes/day), and by ~11:00
Bed 3 reaches 24.9 on its own most days (Master/Bed 2 reach ~23), at which
point the group flips to COOL and starts pumping that heat back out. Outdoor
max was 29–37 °C every day of the last two weeks.

## Root cause

The hysteresis (0.4) is shared between heating and cooling, but the two modes
have very different dynamics in this house:

| unit | cooling rate (on) | solar warm-up (off, 11–16h) | est. cycle at 0.5 °C band | at 1.2 °C | at 1.5 °C |
|---|---|---|---|---|---|
| Bed 3 | −0.062 °C/min | +0.045 °C/min | 19 min | 46 min | 57 min |
| MPR | −0.062 | +0.020 | 34 min | 80 min | 101 min |
| Study | −0.066 | +0.020 | 33 min | 78 min | 98 min |
| Living | −0.033 | +0.028 | 33 min | 79 min | 99 min |

(medians over the last 14 days; observed Bed 3 cycles are ~25 min because the
10-min hold pads each run). The cooling deadband is 0.5 °C = five 0.1 °C sensor
counts, and the room sensor responds to cold supply air within minutes, so a
run ends almost as soon as it starts. Heating runs are long because heat slews
slowly. `min_power_toggle_minutes` does not reduce starts — it only stretches
each run with idle time — and the daily heat→cool flip adds a mode change and
a 60-min dwell lock on top.

## Proposals

### 1. Per-mode hysteresis (recommended, do first)

Split `hysteresis` into `heat_hysteresis` and `cool_hysteresis`, keeping
`hysteresis` as the fallback for both so existing configs are unchanged.

```toml
[defaults]
heat_hysteresis = 0.4     # heating-off threshold = target_low + this
cool_hysteresis = 1.2     # cooling-off threshold = target_high - this
```

Effect at 1.2: cooling-off drops from 24.4 to 23.6 for the default range.
Bed 3's cycle goes from ~19 min to ~46 min (≈2.5× fewer starts), and each run
becomes ~19 min of real cooling instead of 8 min plus 2 min of parked idle.
Rooms swing 23.6–24.9 in the afternoon instead of 24.4–24.9; Bed 4 (high 26)
swings 24.8–26.

Why 1.2 and not more: the config validator requires the heating-off and
cooling-off thresholds not to overlap, and for the default room
(23–24.8) that caps `cool_hysteresis` at 1.4 with `heat_hysteresis` 0.4. If
proposal 3 (no heat) is on, the heating threshold is moot and 1.5 becomes
safe; make the validator only check the heat side for rooms with heating
enabled.

Code touch points:
- `Config`: two fields replace `hysteresis`; `load_config` reads the new keys
  with fallback, validation becomes `target_high - cool_hyst > target_low +
  heat_hyst` (per room, heat side skipped when the room's heating is off).
- `_raw_demand`, `demand_setpoint`, `status_report` thresholds, and the
  `setpoint_boost` docstring: pick the hysteresis by mode. Cooling setpoint
  becomes `floor(24.8 − 1.2 − 1) = 22`; that's fine — power-off is still
  decided by the room sensor, the boost just stops the unit tapering early.
- `replay.py` variant parsing (already generic over Config fields; only the
  field rename matters) and its regression fixture must still produce the
  same events when only `hysteresis` is set.
- Tests: `test_config.py` (fallback, validation), `test_demand.py`,
  `test_setpoints.py`, `test_status.py`.
- `config.toml`, `README.md`, `CLAUDE.md`.

### 2. Optional safety floor: `min_run_minutes`

If you'd rather keep the cooling band tight, an alternative is a minimum run
time: once a unit is switched on it keeps conditioning (boosted setpoint, not
the parked one) for at least N minutes, and only then can the satisfied check
switch it off. It guarantees compressor runs ≥ N min regardless of band, but
its effect on room temperature depends on the room's rate. I'd skip it
initially: proposal 1 addresses the cause directly and is easier to reason
about. Listed so the choice is explicit.

### 3. Heating switch (recommended)

```toml
[defaults]
heating = true            # false = never heat; rooms only demand cooling

[rooms."Bed 4"]
heating = true            # per-room override, e.g. keep Jeni's room heated
```

Semantics:
- A room with `heating = false` never has HEAT demand (`_raw_demand` returns
  None below `target_low`). Its `target_low` is still used for status/charts
  and the validator.
- A group with no heat-enabled room never selects HEAT: first-run default and
  `_select_mode` pick COOL; the master's mode is aligned to COOL once and the
  60-min dwell never bites. Groups with some heat-enabled rooms behave as
  today, just with fewer voters.
- Status report and dashboard cards show "heat off" so it's obvious why a
  20.9 °C room isn't being heated.

Expected effect from the last two weeks: ~250 unit-minutes/day of heating
removed, the daily HEAT→COOL flip removed, and the north group stays in COOL
all day so its first cooling call isn't preceded by a mode change.

Optional dashboard toggle (phase 2): a "Heating on/off" button on the webui
writing `{"heating": false}` into `control_override.json` (alongside
`pause`), read every poll like the other override fields, no expiry. Saves an
SSH + config edit + restart on the Pi when the season changes. Config sets the
default; the override wins while present. Touch points: `read_control_override`,
`set_override` in `webui.py`, `webui.html`, `_override_state`, `test_override.py`,
`test_webui_api.py`.

### 4. Later, only if 1+3 aren't enough: solar-aware band

With the Ecowitt feed already recorded, `cool_hysteresis` could widen by an
extra amount while solar > ~400 W/m² (or the cooling target could rise by a
few tenths in direct-sun hours). Not proposed now: measure 1+3 for a week
first; the rates table suggests they're sufficient, and an adaptive band makes
the log harder to read.

### 5. Cooling should not use the setpoint boost (recommended, do with 1)

Frederico's observation: the boosted cooling setpoint feels colder and noisier
than heating and seems to overshoot. The data agrees, and shows *why*:

- Over the last 30 days, while a unit was ON in COOL, the room fell at the
  same rate whether the setpoint was boosted below the room (Bed 3: −0.30 °C
  per 3 min, n=1117) or parked at/above the room reading during the
  power-toggle hold (−0.30 °C per 3 min, n=274). MPR and Bed 4 are identical.
  In cooling the unit's own thermostat does not taper early — the return-air
  intake at the ceiling reads warmer than the AirTouch room sensor, so the
  unit keeps cooling regardless of what we set. (Heating is the mirror image:
  warm air pools at the intake and the unit tapers before the room is warm,
  which is exactly what `setpoint_boost` was added for.)
- So in cooling the boost buys no extra run; it only widens the gap the unit
  sees, which in AUTO fan pushes fan speed and compressor capacity to maximum
  → cold draft, noise, and a short violent run. The room then keeps dropping
  after power-off: median −0.2 °C, worst −0.4 (Bed 3/MPR) to −0.6 (Bed 4,
  Master) within 8 minutes of the OFF command.
- The parked "idle" setpoint also does nothing in cooling: a unit held on
  by the compressor hold keeps conditioning the satisfied room. That's a
  second reason cooling runs end below target.

Changes:

```toml
[defaults]
heat_setpoint_boost = 1      # keep — heating tapers early without it
cool_setpoint_boost = 0      # cooling never tapers early; the boost only
                             # adds fan speed, draft and undershoot
cool_fan_speed = "medium"    # fan speed pushed when a unit starts cooling
                             # (auto / quiet / low / medium / high / keep);
                             # default medium
heat_fan_speed = "auto"      # same for heating; default auto (= today)
pending_off_fan_speed = "quiet"  # fan while the compressor hold keeps a
                             # satisfied unit on; "off" disables the drop
```

- Split `setpoint_boost` per mode (`setpoint_boost` stays as the fallback).
  `demand_setpoint` uses the mode's boost. With `cool_hysteresis = 1.2` the
  cooling setpoint becomes `floor(24.8 − 1.2) = 23` instead of 22 — the floor
  rounding already gives an implicit 0.6 °C margin, which is plenty given the
  unit never tapers early.
- Fan speed is the lever that actually changes how a cooling run *feels*,
  because the setpoint demonstrably doesn't: `cool_fan_speed` (default
  `"medium"`, decided 4 Sep) and `heat_fan_speed` (default `"auto"`) sent via
  `ac.set_fan_speed(...)` alongside the setpoint when a unit is switched on
  for that mode, only if it differs from `selected_fan_speed`. A lower fan
  means gentler, quieter supply air, a colder coil (more dehumidification),
  and a slower room pull-down — longer runs, which is what proposal 1 wants
  anyway. This overlaps with the fan modulation in `QUIET_MODE_PLAN.md` only
  in mechanism; it's a static per-mode setting, not idle-on control.
  The units currently run fan AUTO (confirmed 4 Sep), so the option is live:
  AUTO is what turns the boosted gap into a full-speed blast.
- The console remembers each unit's selected fan speed across power cycles,
  so a LOW pushed for cooling would silently carry into the next heating
  run. Rule: the service owns fan speed for both modes and pushes the mode's
  `*_fan_speed` whenever it switches a unit on — `cool_fan_speed` defaults
  to `"medium"`, `heat_fan_speed` to `"auto"` (today's behaviour). The value
  `"keep"` opts a mode out (never touch its fan speed). Unsupported speeds
  (`supported_fan_speeds`) are rejected at config load, not at runtime.
- Manual changes on the wall panel/app during a run are left alone: the fan
  command is sent only on the power-ON transition (and on the pending-off
  drop), never re-asserted every poll.
- **Pending off always drops the fan to the quietest speed**, regardless of
  the `*_fan_speed` settings (decided 4 Sep: the unit should be as silent
  as possible while the hold keeps it on). `pending_off_fan_speed` defaults
  to `"quiet"`, falling back to `"low"` for units that don't support QUIET;
  `"off"` disables the drop. Sent together with the parked setpoint the
  first time the hold bites, not every poll. In heating the parked
  setpoint idles the compressor and the low fan quiets the indoor unit; in
  cooling, where the parked setpoint has no effect, the low fan is what
  actually reduces the conditioning and the noise.
- The drop is undone by the next power-ON, which pushes that mode's
  `*_fan_speed` (medium for cooling, auto for heating by default). This
  also covers a service restart mid-hold — nothing to remember, the mode's
  speed is simply re-asserted on the next start. Only
  `pending_off_fan_speed = "off"` with both `*_fan_speed = "keep"` leaves
  fan speed entirely alone.
- Tests: `test_setpoints.py` (per-mode boost + fallback), `test_tick.py`
  (fan command sent once, not every poll), `test_config.py`.

### Not proposed

- Raising `min_power_toggle_minutes`: pads runs with parked idle time instead
  of preventing starts.
- Leaving units on and letting their own thermostats modulate (setpoint
  following): a different controller, and the return-air-sensor taper the
  boost exists for would need an adaptive offset. That's the quiet-mode
  territory and out of scope here.
- Changing `target_high`/`target_low` values: comfort preference, not
  mechanics; the plan works with the current ranges.

## Rollout and verification

1. Implement 1, 3 and 5 (config keys default to today's behaviour), tests green,
   `python replay.py --db history.db --from 2026-08-26 --to 2026-09-02` with
   `--variant "wide:cool_hysteresis=1.2"` and `--variant "noheat:heating=false"`
   as a sanity check that decisions and mode-flip counts move the right way
   (replay can't predict cycle counts for a different band — temperatures
   were recorded under the old one).
2. Set `cool_hysteresis = 1.2`, `heating = false`, `cool_setpoint_boost = 0`
   in `config.toml` (fan speeds take their defaults: cooling medium, heating
   auto, pending-off quiet); push; pull + restart on the Pi.
3. After 3–4 sunny days compare against the baseline above (north starts/day
   10–16, Bed 3 runs ~10 min):

```sql
-- unit starts per day, last 7 days
WITH r AS (SELECT unit, ts, power, LAG(power) OVER (PARTITION BY unit ORDER BY ts) p
           FROM readings WHERE ts > strftime('%s','now')-7*86400)
SELECT date(ts,'unixepoch','localtime') day, unit, COUNT(*) starts
FROM r WHERE power=1 AND p=0 GROUP BY day, unit ORDER BY day, unit;
```

   Targets: north compressor starts ≤ 6/day on a sunny day, Bed 3 cooling
   runs ≥ 15 min, zero heating minutes, zero mode flips.
4. Optionally add a "starts" column to `/api/stats` and the stats page so this
   is visible without SQL (small, separate change).
