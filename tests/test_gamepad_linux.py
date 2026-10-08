"""The Linux reader against a virtual pad (uinput). Skipped where uinput or
joydev is not available."""

import glob
import os
import time

import pytest

from gcgo.desktop import gamepad_linux
from gcgo.desktop.gamepad_linux import LinuxGamepad

if not os.access("/dev/uinput", os.W_OK):
    pytest.skip("needs write access to /dev/uinput", allow_module_level=True)

from vpad import VPad  # noqa: E402


@pytest.fixture
def pad(monkeypatch):
    before = set(glob.glob("/dev/input/js*"))
    p = VPad()
    new = sorted(set(glob.glob("/dev/input/js*")) - before)
    if not new or not os.access(new[0], os.R_OK):
        p.close()
        pytest.skip("no readable joystick device for the virtual pad (joydev?)")
    monkeypatch.setattr(gamepad_linux, "RETRY_MS", 0)
    yield p, LinuxGamepad(new[0])
    try:
        p.close()
    except OSError:
        pass


def settle(reader):
    time.sleep(0.05)
    return reader.state()


def test_axes_buttons_and_name(pad):
    p, g = pad
    s = g.state()
    assert s["axes"]["lx"] == 0.0 and g.name == "gcgo test pad"
    p.axis("lx", 0.5)
    p.axis("ry", -1.0)
    s = settle(g)
    assert s["axes"]["lx"] == pytest.approx(0.5, abs=1e-3)
    assert s["axes"]["ry"] == pytest.approx(-1.0, abs=1e-3)


def test_short_taps_are_latched(pad):
    p, g = pad
    g.state()
    p.press("a", hold=0)                         # down and up between two reads
    p.axis("hatx", 1)
    p.axis("hatx", 0)
    s = settle(g)
    assert set(s["pressed"]) == {"a", "dright"} and s["buttons"] == ()
    assert settle(g)["pressed"] == ()            # read once


def test_unplug_and_replug(pad):
    p, g = pad
    assert g.state() is not None
    p.close()
    assert settle(g) is None
