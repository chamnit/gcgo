"""Input mapping for jog devices: raw readings -> what to hand JogSession.

Pure functions, no I/O, so every input module (web stick, gamepad, shuttle,
keyboard) shares the same feel. The recipes follow caPy-planner's
SIDE_MOTION.md section C.
"""

import math

DEADBAND = 0.08    # stick magnitude below this is exactly zero
CURVE = 2.0        # magnitude exponent: 2 = square law (half stick = quarter speed)


def stick_to_velocity(x, y, z=0.0, speed=50.0, vmax=None,
                      deadband=DEADBAND, curve=CURVE):
    """A stick deflection (each axis -1..1) as a velocity (mm/s).

    The stick is a vector: its direction is the direction of motion and its
    magnitude, through the curve, is the speed -- the same in every direction,
    up to `speed` (mm/s) at full deflection. The deadband is radial and the
    curve acts on the magnitude, so neither bends the direction (a per-axis
    deadband snaps toward the axes, a per-axis curve turns (1, 0.3) from 17 to
    5 degrees). `vmax` (per-axis mm/s, optional) caps the speed along that
    direction so the slowest axis binds and the direction is kept."""
    m = math.sqrt(x * x + y * y + z * z)
    if m <= deadband:
        return (0.0, 0.0, 0.0)
    u = (x / m, y / m, z / m)
    m = min(m, 1.0)
    s = speed * ((m - deadband) / (1.0 - deadband)) ** curve
    v = [u[0] * s, u[1] * s, u[2] * s]
    if vmax:
        k = 1.0
        for i in range(3):
            if v[i] and vmax[i] > 0:
                k = min(k, vmax[i] / abs(v[i]))
        v = [c * k for c in v]
    return (v[0], v[1], v[2])


# A spring-return shuttle ring (ShuttleXpress/ShuttlePro, positions -7..+7):
# mm/s per position, roughly logarithmic.
SHUTTLE_TABLE = (0.0, 0.5, 1.0, 2.0, 5.0, 10.0, 25.0, 60.0)


def shuttle_velocity(pos, table=SHUTTLE_TABLE):
    """A shuttle position (-7..+7) as a signed speed (mm/s) on the chosen axis.
    The input must re-send it while held off centre (the TTL / keepalive);
    the spring's return is one zero."""
    n = min(abs(int(pos)), len(table) - 1)
    return table[n] if pos > 0 else -table[n]
