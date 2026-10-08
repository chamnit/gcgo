"""Shared fixtures: a fake serial port under the real Streamer, and a clock
the tests move by hand (jog timing without sleeping)."""

import pytest

from gcgo.core import jog as jogmod
from gcgo.core.protocol import Streamer


class FakePort:
    """Transport stand-in: records writes, serves bytes queued by feed()."""

    def __init__(self):
        self.out = []
        self._rx = bytearray()

    def feed(self, text):
        self._rx += text.encode()

    # Transport contract
    def write(self, data):
        self.out.append(bytes(data))

    def any(self):
        return len(self._rx)

    def read(self, n=64):
        d, self._rx = bytes(self._rx[:n]), self._rx[n:]
        return d

    def readinto(self, buf):
        n = min(len(buf), len(self._rx))
        buf[:n] = self._rx[:n]
        self._rx = self._rx[n:]
        return n

    def set_timeout(self, s):
        pass

    def reset_input(self):
        self._rx = bytearray()

    def is_open(self):
        return True

    def close(self):
        pass

    def lines(self):
        """What was written, as text lines / realtime bytes."""
        return [o.decode().strip() if o[:1] != b"\x85" else o for o in self.out]


class Clock:
    def __init__(self):
        self.t = 1000

    def __call__(self):
        return self.t

    def advance(self, ms):
        self.t += ms


@pytest.fixture
def clock(monkeypatch):
    c = Clock()
    monkeypatch.setattr(jogmod, "now_ms", c)
    return c


@pytest.fixture
def port():
    return FakePort()


@pytest.fixture
def streamer(port):
    return Streamer(port)
