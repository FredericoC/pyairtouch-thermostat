"""Setpoint arithmetic and the send-side clamping/suppression."""

import pytest
from pyairtouch import AcMode

from climate_service import RoomConfig, demand_setpoint, idle_setpoint
from conftest import make_config, make_group

ROOM = RoomConfig(target_low=21.0, target_high=24.0)


class TestDemandSetpoint:
    @pytest.mark.parametrize(
        ("mode", "hysteresis", "boost", "expected"),
        [
            (AcMode.HEAT, 0.4, 1.0, 23.0),  # ceil(21 + 0.4 + 1)
            (AcMode.COOL, 0.4, 1.0, 22.0),  # floor(24 - 0.4 - 1)
            (AcMode.HEAT, 0.4, 0.0, 22.0),  # ceil(21.4)
            (AcMode.COOL, 0.4, 0.0, 23.0),  # floor(23.6)
            (AcMode.HEAT, 0.0, 0.0, 21.0),  # integral stays put
            (AcMode.COOL, 0.0, 0.0, 24.0),
        ],
    )
    def test_rounds_toward_demand_side(self, mode, hysteresis, boost, expected):
        assert demand_setpoint(ROOM, mode, hysteresis, boost) == expected


class TestIdleSetpoint:
    @pytest.mark.parametrize(
        ("mode", "temp", "expected"),
        [
            (AcMode.HEAT, 22.7, 22.0),  # floor: stop pushing heat
            (AcMode.COOL, 22.3, 23.0),  # ceil: stop pushing cool
            (AcMode.HEAT, 22.0, 22.0),
            (AcMode.COOL, 22.0, 22.0),
        ],
    )
    def test_parks_at_room_temperature(self, mode, temp, expected):
        assert idle_setpoint(mode, temp) == expected


class TestSendSetpoint:
    async def test_sends_changed_setpoint(self):
        ctl, units, commands = make_group(
            make_config(), {"A": 22.0, "B": 22.0}, setpoints={"A": 22.0}
        )
        await ctl._send_setpoint("A", 23.0)
        assert commands == [("A", "setpoint", 23.0)]

    async def test_suppresses_sub_resolution_change(self):
        ctl, units, commands = make_group(
            make_config(), {"A": 22.0, "B": 22.0}, setpoints={"A": 22.0}
        )
        units["A"].target_temperature_resolution = 0.1
        await ctl._send_setpoint("A", 22.04)
        assert commands == []

    async def test_clamps_to_unit_limits(self):
        ctl, units, commands = make_group(make_config(), {"A": 22.0, "B": 22.0})
        await ctl._send_setpoint("A", 35.0)
        assert commands == [("A", "setpoint", 31.0)]  # unit max
        commands.clear()
        await ctl._send_setpoint("A", 10.0)
        assert commands == [("A", "setpoint", 16.0)]  # unit min

    async def test_missing_resolution_defaults_to_half_degree(self):
        ctl, units, commands = make_group(
            make_config(), {"A": 22.0, "B": 22.0}, setpoints={"A": 24.8}
        )
        units["A"].target_temperature_resolution = None
        await ctl._send_setpoint("A", 25.0)  # |24.8 - 25.0| < 0.5 / 2
        assert commands == []
