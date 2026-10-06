"""Minimal MicroPython serial-console front-end.

A bare REPL over the board's USB serial console — no display or keyboard
required. Drives the shared gcgo core; the UART talks to the GRBL board.

Usage on the board:
    from gcgo.micropython.main import start
    start(uart_id=1, baud=115200, tx=4, rx=5)   # pins are board-specific
"""

import time

from gcgo.core.config import REPORT_MM, StatusConfig
from gcgo.core.gcode import validate_gcode
from gcgo.core.protocol import FINISHING, RUNNING, Streamer
from gcgo.micropython.transport import UARTTransport

CONFIG_FILE = "gcgo_config.json"


def _status_line(st, cfg):
    if not st.state:
        return ""
    k = cfg.scale   # reports are mm; convert to the display units
    wx, wy, wz = st.wpos
    u = cfg.pos_unit
    return "[%s] W:%.3f %.3f %.3f %s F:%.0f" % (st.state, wx * k, wy * k, wz * k,
                                                u, st.feed * k)


def _stream(streamer, cfg, path):
    try:
        total = validate_gcode(path)
    except (OSError, ValueError) as e:
        print("  " + str(e))
        return

    # No per-line callbacks on the MCU hot path — keeps the pump allocation-free.
    streamer.gc_collect = True   # collect at slack points (GRBL buffer full)
    streamer.begin(path, status_interval=cfg.rate)
    print("Streaming %s (%d lines) — Ctrl-C to stop" % (path, total))

    next_print = time.ticks_ms()
    eof_noted = False
    try:
        while True:
            st = streamer.pump()
            if st == FINISHING and not eof_noted:
                # File fully sent; keep polling while GRBL drains buffered motion.
                eof_noted = True
                print("  end of file (%d lines) — finishing motion, Ctrl-C to stop"
                      % streamer.sent)
            if st != RUNNING and st != FINISHING:
                break
            now = time.ticks_ms()
            if time.ticks_diff(now, next_print) >= 0:
                print("  %d/%d  %.0f%%  %s" % (streamer.sent, total,
                                               streamer.progress * 100,
                                               _status_line(streamer.status, cfg)))
                next_print = now + 1000
            time.sleep_ms(3)
    except KeyboardInterrupt:
        streamer.request_stop()

    state = streamer.state
    if state == "done":
        print("Stream complete: %s (%d lines)" % (path, streamer.sent))
    else:
        print("Stream %s: %s (%d/%d)" % (state, path, streamer.sent, total))
    if state != "done" and streamer.sent_any:
        g = streamer.cancel()
        print("Machine reset to halt motion." + ((' "%s"' % g) if g else ""))


def run(streamer, cfg):
    greeting = streamer.connect() or streamer.query_status()
    if greeting:
        print('GRBL: "%s"' % greeting)
    streamer.send_command(REPORT_MM)   # reports in mm; gcgo converts for display
    print("gcgo (MicroPython) — commands: ls, cd, load <f>, run, status, reset, units mm|inch, quit")
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
            elif cmd == "ls":
                import os
                try:
                    for nm in sorted(os.listdir(arg or ".")):
                        print("  " + nm)
                except OSError as e:
                    print("  " + str(e))
            elif cmd == "cd":
                import os
                try:
                    os.chdir(arg or "/")
                    print("  " + os.getcwd())
                except OSError as e:
                    print("  " + str(e))
            elif cmd == "load":
                if not arg:
                    print("Usage: load <file>")
                else:
                    n = validate_gcode(arg)
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
                print("  units = %s" % cfg.units)
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
