"""The web settings page's controller settings: read, write, save."""

import json
import os

import pytest

from gcgo.core.config import StatusConfig
from gcgo.frontends.web import server as srv
from test_jog import answer


@pytest.fixture
def server(streamer, port, clock, monkeypatch):
    monkeypatch.setattr(srv, "now_ms", clock)
    s = srv.WebServer(streamer, StatusConfig(), ".", ".")
    answer(port, streamer, "ok")                 # its $13=0
    port.out.clear()
    return s


def sent(s):
    return [o for o in s.pending if o["type"].startswith("ctl_")]


def test_capy_settings_are_read_into_rows(server, port, streamer):
    server.apply({"cmd": "ctl_settings"})
    assert port.lines() == ["$settings.all"]
    answer(port, streamer, "; --- kin ---",
           "$kin.type=cartesian            ; * machine family",
           "; --- motor ---",
           "$motor.1.steps=80              ; * steps/mm",
           "; x: linear, motor 1",
           "ok")
    assert sent(server) == [{"type": "ctl_settings", "fw": "capy", "rows": [
        ["kin.type", "cartesian", "kin"], ["motor.1.steps", "80", "motor"]]}]
    assert not any(o.get("line", "").startswith(("$", ";")) for o in server.pending)


def test_grbl_falls_back_to_dollar_dollar(server, port, streamer):
    server.apply({"cmd": "ctl_settings"})
    answer(port, streamer, "error:3")
    assert port.lines()[-1] == "$$"
    answer(port, streamer, "$0=10", "$110=500.000", "ok")
    assert sent(server)[-1] == {"type": "ctl_settings", "fw": "grbl",
                                "rows": [["0", "10", ""], ["110", "500.000", ""]]}


def test_a_refused_read_says_why(server, port, streamer):
    server.apply({"cmd": "ctl_settings"})
    answer(port, streamer, "error:8 ; Not idle")
    assert sent(server)[-1]["error"].startswith("error:8")
    answer(port, streamer, "[MSG:later]")          # capture is over: back to the log
    assert server.pending[-1] == {"type": "msg", "line": "[MSG:later]"}


def test_a_write_and_its_reply(server, port, streamer):
    server.apply({"cmd": "ctl_set", "key": "motor.1.steps", "value": " 100 "})
    assert port.lines() == ["$motor.1.steps=100"]
    answer(port, streamer, "error:225 ; Setting value not a number")
    assert sent(server)[-1] == {"type": "ctl_set", "key": "motor.1.steps", "value": "100",
                                "reply": "error:225 ; Setting value not a number"}


@pytest.mark.parametrize("key,value", [("motor.1.steps", "1\n$H"), ("x;y", "1"),
                                       ("a", ""), ("", "1"), ("a", "1=2")])
def test_bad_names_and_values_are_not_sent(server, port, key, value):
    server.apply({"cmd": "ctl_set", "key": key, "value": value})
    assert port.out == [] and sent(server)[-1]["reply"].startswith("not sent")


def test_grbl_dollar_13_keeps_reports_in_mm(server, port, streamer):
    server.apply({"cmd": "ctl_set", "key": "13", "value": "1"})
    answer(port, streamer, "ok")
    assert port.lines() == ["$13=1", "$13=0"]


def test_save(server, port, streamer):
    server.apply({"cmd": "ctl_save"})
    assert port.lines() == ["$save"]
    answer(port, streamer, "[MSG:Settings saved]", "ok")
    assert sent(server)[-1] == {"type": "ctl_saved", "reply": "ok"}


def test_settings_json_matches_capy_csv():
    p = os.path.join(os.path.dirname(srv.__file__), "static", "settings.json")
    j = json.load(open(p, encoding="utf-8"))
    assert j["capy"] and all(len(r) == 6 for r in j["capy"])
    assert j["grbl"]["110"][0] == "X-axis max rate"
