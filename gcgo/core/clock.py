"""Monotonic millisecond clock, portable across CPython and MicroPython."""

import time

if hasattr(time, "ticks_ms"):  # MicroPython
    def now_ms() -> int:
        return time.ticks_ms()

    def diff_ms(a: int, b: int) -> int:
        return time.ticks_diff(a, b)
else:  # CPython
    def now_ms() -> int:
        return int(time.monotonic() * 1000)

    def diff_ms(a: int, b: int) -> int:
        return a - b
