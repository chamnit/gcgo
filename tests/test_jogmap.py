import math

import pytest

from gcgo.core.jogmap import shuttle_velocity, stick_to_velocity


def speed(v):
    return math.sqrt(sum(c * c for c in v))


def test_deadband_is_exactly_zero():
    assert stick_to_velocity(0.05, -0.05) == (0.0, 0.0, 0.0)


def test_square_law_on_the_magnitude():
    # 0.6 of a 100 mm/s stick: ((0.6 - 0.08) / 0.92)^2 * 100
    assert speed(stick_to_velocity(0.6, 0, speed=100)) == pytest.approx(31.95, abs=0.01)


def test_same_speed_in_every_direction():
    a = stick_to_velocity(0.6, 0, speed=100)
    d = stick_to_velocity(0.6 / math.sqrt(2), 0.6 / math.sqrt(2), speed=100)
    assert speed(a) == pytest.approx(speed(d))


def test_direction_is_kept():
    v = stick_to_velocity(1.0, 0.3, speed=100)
    assert math.degrees(math.atan2(v[1], v[0])) == pytest.approx(16.70, abs=0.01)
    assert speed(v) == pytest.approx(100)        # clipped to full deflection


def test_slow_axis_binds_without_bending():
    v = stick_to_velocity(1, 0, 1, speed=100, vmax=(100, 100, 25))
    assert v == pytest.approx((25, 0, 25))


def test_shuttle_table():
    assert shuttle_velocity(0) == 0
    assert shuttle_velocity(3) == 2.0 and shuttle_velocity(-3) == -2.0
    assert shuttle_velocity(9) == 60.0          # past the end clamps
