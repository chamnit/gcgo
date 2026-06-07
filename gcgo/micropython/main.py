"""Minimal MicroPython serial-console front-end.

A bare REPL over the board's USB serial console — no display or keyboard
required. Drives the shared gcgo core; the UART talks to the GRBL board.

Usage on the board:
    from gcgo.micropython.main import start
    start(uart_id=1, baud=115200, tx=4, rx=5)   # pins are board-specific
"""

import time

from gcgo.core.config import StatusConfig
from gcgo.core.gcode import load_gcode
from gcgo.core.protocol import RUNNING, Streamer
from gcgo.micropython.transport import UARTTransport

CONFIG_FILE = "gcgo_config.json"


def _apply_units(streamer, cfg):
    streamer.send_command("$13=" + cfg.grbl_inch)


def _status_line(st, cfg):
    if not st.state:
        return ""
    wx, wy, wz = st.wpos
    u = cfg.pos_unit
    return "[%s] W:%.3f %.3f %.3f %s F:%.0f" % (st.state, wx, wy, wz, u, st.feed)


def _stream(streamer, cfg, path):
    try:
        lines = load_gcode(path)
    except (OSError, ValueError) as e:
        print("  " + str(e))
        return

    def on_response(n, resp):
        if resp.startswith("error"):
            print('  [%d] "%s"' % (n, resp))

    def on_message(msg):
        if not msg.startswith("<"):
            print('  "%s"' % msg)

    streamer.begin(lines, on_response=on_response, on_message=on_message,
                   status_interval=cfg.rate)
    print("Streaming %s (%d lines) — Ctrl-C to stop" % (path, streamer.total))

    next_print = time.ticks_ms()
    try:
        while streamer.pump() == RUNNING:
            now = time.ticks_ms()
            if time.ticks_diff(now, next_print) >= 0:
                print("  %d/%d  %s" % (streamer.sent, streamer.total,
                                       _status_line(streamer.status, cfg)))
                next_print = now + 1000
            time.sleep_ms(3)
    except KeyboardInterrupt:
        streamer.request_stop()

    state = streamer.state
    if state == "done":
        print("Stream complete: %s (%d lines)" % (path, streamer.total))
    else:
        print("Stream %s: %s (%d/%d)" % (state, path, streamer.sent, streamer.total))
    if state != "done" and streamer.sent_any:
        g = streamer.cancel()
        print("Machine reset to halt motion." + ((' "%s"' % g) if g else ""))


def run(streamer, cfg):
    greeting = streamer.connect() or streamer.query_status()
    if greeting:
        print('GRBL: "%s"' % greeting)
    _apply_units(streamer, cfg)
    print("gcgo (MicroPython) — commands: load <f>, run, status, reset, units mm|inch, quit")
    print("Any other input is sent to GRBL as a command.")

    loaded = None
    while True:
        try:
            raw = input("gcgo> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not raw:
            continue
        parts = raw.split(None, 1)
        cmd = parts[0].lower()
        arg = parts[1] if len(parts) > 1 else ""

        try:
            if cmd in ("quit", "exit"):
                break
            elif cmd == "status":
                print('  "%s"' % streamer.query_status())
            elif cmd == "load":
                if not arg:
                    print("Usage: load <file>")
                else:
                    n = len(load_gcode(arg))
                    loaded = arg
                    print("Loaded %s (%d lines)" % (arg, n))
            elif cmd == "run":
                if not loaded:
                    print("No file loaded.")
                else:
                    _stream(streamer, cfg, loaded)
            elif cmd == "reset":
                g = streamer.soft_reset()
                if g:
                    print('  "%s"' % g)
            elif cmd == "units":
                if arg in ("mm", "inch"):
                    cfg.units = arg
                    cfg.save(CONFIG_FILE)
                    _apply_units(streamer, cfg)
                print("  units = %s ($13=%s)" % (cfg.units, cfg.grbl_inch))
            else:
                streamer.send_command_verbose(raw, on_line=lambda l: print('  "%s"' % l))
        except KeyboardInterrupt:
            print("^C")
            streamer.flush_input()
        except Exception as e:
            print("Error: " + str(e))

    streamer.disconnect()
    print("Disconnected.")


def start(uart_id=1, baud=115200, **kw):
    cfg = StatusConfig()
    cfg.load(CONFIG_FILE)
    streamer = Streamer(UARTTransport(uart_id, baud, **kw))
    run(streamer, cfg)
