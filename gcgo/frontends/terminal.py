"""Desktop terminal front-end: the interactive REPL, status formatting, and
command dispatch, driving the core over the desktop adapters."""

from __future__ import annotations

import os
import re
import sys
import time

from gcgo.core.codes import ALARMS as _ALARMS, ERRORS as _ERRORS
from gcgo.core.config import REPORT_MM, StatusConfig
from gcgo.core.gcode import validate_gcode
from gcgo.core.protocol import FINISHING, RUNNING, Streamer
from gcgo.core.status import GRBLStatus
from gcgo.core.tables import (
    ACTION_DESC as _ACTION_DESC,
    ACTION_METHOD as _ACTION_METHOD,
    GRBL_SETTINGS as _GRBL_SETTINGS,
    PARAM_NAMES as _PARAM_NAMES,
    STREAM_ACTIONS,
)
from gcgo.desktop import keyboard
from gcgo.desktop.display import print_above, redraw_status
from gcgo.desktop.paths import CONFIG_FILE
from gcgo.desktop.transport import list_ports


def _explain(line: str) -> str:
    """The meaning of an error or alarm line, or "" if it isn't one or the code
    is unknown. Handles GRBL's "error:N" / "ALARM:N" and caPy's
    "error:N ; Message" (the text after ';' is already the short message)."""
    if line.startswith("error:"):
        table = _ERRORS
    elif line.startswith("ALARM:"):
        table = _ALARMS
    else:
        return ""
    tail = line.split(":", 1)[1].partition(";")[0].strip()
    try:
        return table.get(int(tail), "")
    except ValueError:
        return ""


# Lines whose 'ok' waits for motion: a probe (G38.x) answers [PRB:] then ok, a
# program end (M2/M30) [MSG:Pgm End] then ok, a dwell (G4) and $H once done.
_HOLDS_OK = re.compile(r"G0*38|G0*4(?![0-9.])|M0*2(?![0-9.])|M30(?![0-9.])|^\$H$")


def _holds_ok(line: str) -> bool:
    """True if the controller holds this MDI line's 'ok' until motion finishes,
    so the read has to wait longer than the usual timeout."""
    return bool(_HOLDS_OK.search(line.upper().replace(" ", "")))


def _tidy(line: str) -> str:
    """caPy's "$" reply rows come as "[$:text]"; show just the text."""
    if line.startswith("[$:") and line.endswith("]"):
        return line[3:-1]
    return line


def _grbl(line: str) -> str:
    """Format a raw GRBL message for display: indented and quoted, so GRBL's
    own words (ok, error:N, [MSG:...], the welcome string) are visually
    distinct from gcgo's output. Errors and alarms get their meaning appended."""
    why = _explain(line)
    line = _tidy(line)
    return f'  "{line}"  — {why}' if why else f'  "{line}"'


def _format_status_line(st: GRBLStatus, progress: str, cfg: StatusConfig) -> str:
    """Compact single-line status for the streaming display.

    All values are fixed-width so they stay put when the line is reprinted.
    Fields can be turned off via StatusConfig.
    """
    if not st.state:
        return progress
    show = cfg.show
    u = cfg.pos_unit
    k = cfg.scale   # reports are mm; convert to the display units
    wx, wy, wz = (v * k for v in st.wpos)
    mx, my, mz = (v * k for v in st.mpos)
    ox, oy, oz = (v * k for v in st.wco)
    pins = st.pins if st.pins else "-"

    parts = []
    if show["state"]:
        parts.append(f"[{st.state:^7}]")
    if show["wpos"]:
        parts.append(f"W:{wx:9.3f} {wy:9.3f} {wz:9.3f} {u}")
    if show["mpos"]:
        parts.append(f"M:{mx:9.3f} {my:9.3f} {mz:9.3f} {u}")
    if show["wco"]:
        parts.append(f"O:{ox:9.3f} {oy:9.3f} {oz:9.3f} {u}")
    if show["feed"]:
        parts.append(f"F:{st.feed * k:6.0f} {cfg.feed_unit}")
    if show["spindle"]:
        parts.append(f"S:{st.spindle:6.0f} RPM")

    ov = []
    if show["feed_ov"]:
        ov.append(f"F{st.feed_ov:3d}%")
    if show["rapid_ov"]:
        ov.append(f"R{st.rapid_ov:3d}%")
    if show["spindle_ov"]:
        ov.append(f"S{st.spindle_ov:3d}%")
    if ov:
        parts.append("Ov:" + " ".join(ov))

    if show["pins"]:
        parts.append(f"Pn:{pins:<5}")
    if progress:
        parts.append(progress)
    return " | ".join(parts)


def _print_status_detail(st: GRBLStatus, cfg: StatusConfig) -> None:
    """Multi-line formatted status for the status command."""
    u = cfg.pos_unit
    k = cfg.scale   # reports are mm; convert to the display units
    wx, wy, wz = (v * k for v in st.wpos)
    mx, my, mz = (v * k for v in st.mpos)
    pins_str = " ".join(st.pins) if st.pins else "none"
    print(f"""
  State:      {st.state}
  Work pos:   X: {wx:10.3f}  Y: {wy:10.3f}  Z: {wz:10.3f}  {u}
  Mach pos:   X: {mx:10.3f}  Y: {my:10.3f}  Z: {mz:10.3f}  {u}
  Feed:       {st.feed * k:.0f} {cfg.feed_unit}
  Spindle:    {st.spindle:.0f} RPM
  Overrides:  Feed {st.feed_ov}%  Rapid {st.rapid_ov}%  Spindle {st.spindle_ov}%
  Limit pins: {pins_str}
""")


HELP = """
Commands:
  load <file>   Load a gcode file (does not start streaming)
  run           Stream the loaded file
  mdi           Enter MDI mode (send gcode commands directly)
  settings      Show GRBL $$ settings in readable form
                (caPy: settings GROUP for one group, settings all for every setting)
  params        Show GRBL $# coordinate parameters
  unlock        Unlock GRBL alarm state ($X)
  home          Run homing cycle ($H)
  check         Toggle check mode ($C)
  config        Configure status fields, rate, units, and stream keys
  reset         Send GRBL soft-reset (Ctrl-X); restores overrides to 100%
  status        Query GRBL status (?)
  ports         List available serial ports
  ls [dir]      List files in a directory
  cd [dir]      Change working directory
  help          Show this message
  quit / exit   Exit

Tab completes commands and file paths (load/cd/ls).
"""


def _tidy_number(val: str) -> str:
    """A setting value without padding zeros ("10000.000" -> "10000"), so the
    table reads the same whether the firmware prints "1" (GRBL) or "1.000"
    (caPy). Non-numeric values pass through unchanged."""
    val = val.strip()
    try:
        float(val)
    except ValueError:
        return val
    if "." in val:
        val = val.rstrip("0").rstrip(".")
    return val or "0"


def _is_capy(streamer: Streamer) -> bool:
    """True if the controller's build info ($I) identifies it as caPy."""
    lines: list[str] = []
    streamer.send_command_verbose("$I", on_line=lines.append)
    return any(l.startswith("[VER:") and "capy" in l.lower() for l in lines)


def _print_capy_settings(streamer: Streamer, cmd: str) -> None:
    """Show a caPy settings reply as sent: it is already self-describing
    ("$key=value ; meaning", "; --- group ---" headers, [$:...] rows)."""
    lines: list[str] = []
    streamer.send_command_verbose(cmd, on_line=lines.append)
    if lines and lines[-1].startswith("error:8"):
        print(f"  {cmd} needs the machine Idle — wait for Idle and retry"
              " (settings GROUP works any time).")
        return
    rows = False
    for line in lines:
        if line == "ok":
            continue
        if line.startswith("error"):
            print(_grbl(line))
        elif line.startswith(";"):
            print(f"  {line[1:].strip()}")
        else:
            rows = rows or line.startswith("$")
            print(f"  {_tidy(line)}")
    if rows:
        print("  (* = applies at the next reset, ** = at the next boot: $save then $reboot;"
              " change one in mdi with $key=value)")


def _print_settings(streamer: Streamer, arg: str = "") -> None:
    """GRBL's $$ in readable form. On caPy, also its setting groups;
    `settings GROUP` lists one group and `settings all` every setting."""
    capy = _is_capy(streamer)
    if arg:
        print()
        if not capy:
            print("  settings GROUP / all are caPy only; GRBL has just $$.")
        elif arg.lower() == "all":
            _print_capy_settings(streamer, "$settings.all")
        else:
            _print_capy_settings(streamer, "$settings." + arg)
        print()
        return

    raw_lines: list[str] = []
    streamer.send_command_verbose("$$", on_line=raw_lines.append)

    print()
    for line in raw_lines:
        if line == "ok":
            continue
        if not (line.startswith("$") and "=" in line):
            print(f"  {line}")
            continue
        key, _, val = line.partition("=")
        try:
            n = int(key[1:])
        except ValueError:
            print(f"  {line}")
            continue

        val = _tidy_number(val)
        desc, unit = _GRBL_SETTINGS.get(n, ("", ""))
        if unit == "bool":
            val_str = "disabled" if val == "0" else "enabled"
        elif unit:
            val_str = f"{val}  ({unit})"
        else:
            val_str = val

        desc_str = f"  {desc}" if desc else ""
        print(f"  {key:<5} = {val_str:<24}{desc_str}")
    print()

    # caPy keeps its full set under named group.name keys; $$ above is only the
    # GRBL-numbered subset. Show the group map, not the long full dump.
    if capy:
        _print_capy_settings(streamer, "$settings")
        print("  settings GROUP lists one group, settings all every setting.")
        print()


def _print_params(streamer: Streamer) -> None:
    raw_lines: list[str] = []
    streamer.send_command_verbose("$#", on_line=raw_lines.append)

    print()
    for line in raw_lines:
        if line == "ok":
            continue
        if not (line.startswith("[") and ":" in line):
            print(f"  {line}")
            continue
        inner = line[1:-1]
        key, _, rest = inner.partition(":")
        name = _PARAM_NAMES.get(key, key)

        if key == "TLO":
            print(f"  {key:<5}  {name:<18}  Z: {rest:>10}")
        elif key == "PRB":
            coords, _, success = rest.rpartition(":")
            x, y, z = coords.split(",")
            result = "success" if success == "1" else "failed"
            print(f"  {key:<5}  {name:<18}  X: {x:>10}  Y: {y:>10}  Z: {z:>10}  ({result})")
        else:
            x, y, z = rest.split(",")
            print(f"  {key:<5}  {name:<18}  X: {x:>10}  Y: {y:>10}  Z: {z:>10}")
    print()


def _stream_keys_banner(cfg: StatusConfig) -> str:
    """Build the streaming key-hint banner from the enabled bindings."""
    bound = [
        f"[{cfg.keys[aid]['key']}] {desc}"
        for aid, desc, _k, _e, _m in STREAM_ACTIONS
        if cfg.keys[aid]["enabled"] and cfg.keys[aid]["key"]
    ]
    if not bound:
        return "  Streaming (no keys bound — use 'config keys' to configure)\n"
    return "  Streaming — " + "   ".join(bound) + "\n"


def _build_keymap(cfg: StatusConfig) -> dict:
    """char -> action id for all enabled, bound actions."""
    return {
        cfg.keys[aid]["key"]: aid
        for aid, _d, _k, _e, _m in STREAM_ACTIONS
        if cfg.keys[aid]["enabled"] and cfg.keys[aid]["key"]
    }


def _run_mdi(streamer: Streamer, cfg: StatusConfig) -> None:
    """MDI mode: send gcode commands one at a time. Exit with 'exit' or Ctrl-C."""
    print("MDI mode — type gcode commands, 'exit' to return.")
    with keyboard.completion_suspended():
        _mdi_loop(streamer, cfg)


def _mdi_loop(streamer: Streamer, cfg: StatusConfig) -> None:
    while True:
        try:
            line = input("mdi> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not line:
            continue
        if line.lower() == "exit":
            break

        # Commands that need special handling outside the normal send/wait path.
        if line == "?":
            print(_grbl(streamer.query_status()))
        elif line == "!":
            streamer.feed_hold()
        elif line == "~":
            streamer.cycle_start()
        elif "\x18" in line:
            greeting = streamer.soft_reset()
            if greeting:
                print(_grbl(greeting))
        else:
            streamer.send_command_verbose(line, on_line=lambda l: print(_grbl(l)),
                                          read_timeout=120 if _holds_ok(line) else None)
            # gcgo converts units itself, so reports must stay in mm.
            if line.replace(" ", "").startswith("$13="):
                _report_mm(streamer)
                print(f"  [gcgo] {REPORT_MM} re-asserted (gcgo converts to {cfg.units} itself)")


def _run_stream(streamer: Streamer, path: str, cfg: StatusConfig) -> bool:
    """Stream a file with a live status line and interactive real-time keys,
    driving the non-blocking pump in one cooperative loop (no threads).

    Returns True only if the stream completed cleanly (no error, not stopped).
    """
    try:
        total = validate_gcode(path)
    except (OSError, ValueError) as e:
        print(f"  {e}")
        return False

    def status_line() -> str:
        sent = streamer.sent
        pct = (sent * 100 // total) if total else 0
        return _format_status_line(streamer.status, f"{sent}/{total} ({pct}%)", cfg)

    def on_sent(n, line):
        print_above(f"  >> {line}", status_line())

    def on_response(n, resp):
        if not resp.startswith("ok"):
            why = _explain(resp)
            print_above(f'  [{n}] "{resp}"' + (f"  — {why}" if why else ""), status_line())

    def on_message(msg):
        if msg.startswith("<"):
            redraw_status(status_line())  # pump already parsed it into status
        else:
            print_above(_grbl(msg), status_line())

    streamer.begin(path, on_sent=on_sent, on_response=on_response,
                   on_message=on_message, status_interval=cfg.rate)

    keymap = _build_keymap(cfg)
    sys.stdout.write(_stream_keys_banner(cfg))
    sys.stdout.flush()

    err = None
    with keyboard.stream_keys() as keys:
        try:
            prev = RUNNING
            while True:
                st = streamer.pump()
                if st == FINISHING and prev != FINISHING:
                    # File fully sent; GRBL keeps executing buffered motion.
                    # Keep polling (loop continues) so status stays live until Idle.
                    print_above(f"  end of file ({streamer.sent} lines) — "
                                "finishing motion, keys still active",
                                status_line())
                prev = st
                if st != RUNNING and st != FINISHING:
                    break
                key = keys.poll_key()
                if key is not None:
                    aid = keymap.get(key)
                    if aid == "stop":
                        streamer.request_stop()
                    elif aid is not None:
                        getattr(streamer, _ACTION_METHOD[aid])()
                        print_above(f"  [{_ACTION_DESC[aid]}]", status_line())
                time.sleep(0.003)
        except KeyboardInterrupt:
            streamer.request_stop()
        except Exception as e:
            # Never abandon a running stream on a control-path error — stop it,
            # then fall through to the cancel cleanup below.
            err = e
            streamer.request_stop()

    state, sent = streamer.state, streamer.sent
    name = os.path.basename(path)
    if err is not None:
        print_above(f"  stream error: {err}", "")
    if state == "error":
        msg = f"Stream halted on error: {name} ({sent}/{total} lines sent)"
    elif state == "stopped":
        msg = f"Stream stopped: {name} ({sent}/{total} lines sent)"
    else:
        msg = f"Stream complete: {name} ({total} lines)"
    sys.stdout.write(f"\n{msg}\n")
    sys.stdout.flush()

    # A stopped/errored stream leaves GRBL still running its buffered motion.
    # Abort and flush so the machine halts and the next run starts clean — but
    # only if we actually sent something; otherwise there's nothing to halt and
    # a needless soft-reset would wipe overrides / alarm a homing machine.
    completed = state == "done"
    if not completed and streamer.sent_any:
        greeting = streamer.cancel()
        sys.stdout.write("Machine reset to halt motion and flush buffers.\n")
        if greeting:
            sys.stdout.write(_grbl(greeting) + "\n")
        sys.stdout.flush()

    return completed


def _connect_message(greeting: str, streamer: Streamer) -> str:
    """GRBL's own words to show on connect: the welcome string if it arrived,
    otherwise a status report from '?' (this board doesn't reset on serial open).
    Empty if GRBL doesn't respond at all."""
    return greeting or streamer.query_status()


def _report_mm(streamer: Streamer) -> None:
    """Keep the controller's reports in mm; gcgo converts for display."""
    streamer.send_command(REPORT_MM)


def _key_conflict(cfg: StatusConfig, aid: str, key: str):
    """Return the id of another enabled action already bound to `key`, if any."""
    for other, entry in cfg.keys.items():
        if other != aid and entry["enabled"] and entry["key"] == key:
            return other
    return None


def _config_keys(cfg: StatusConfig, parts: list) -> None:
    """Handle 'config keys ...': view or set streaming real-time key bindings."""
    if not parts:
        print("Streaming real-time keys:")
        for aid, desc, _k, _e, _m in STREAM_ACTIONS:
            entry = cfg.keys[aid]
            state = "on " if entry["enabled"] else "off"
            key = repr(entry["key"]) if entry["key"] else "(unbound)"
            print(f"  [{state}] {aid:<14} {key:<10} {desc}")
        print("Usage: config keys <action> <char>   bind key and enable")
        print("       config keys <action> off       disable")
        return

    aid = parts[0].lower()
    if aid not in cfg.keys:
        print(f"Unknown action: {aid!r}. Run 'config keys' to list them.")
        return

    if len(parts) < 2:
        entry = cfg.keys[aid]
        print(f"  {aid}: key={entry['key']!r} enabled={entry['enabled']}")
        return

    val = parts[1]
    low = val.lower()

    if low in ("off", "disable", "none"):
        cfg.keys[aid]["enabled"] = False
        cfg.save(CONFIG_FILE)
        print(f"  {aid} disabled")
        return

    if low in ("on", "enable"):
        key = cfg.keys[aid]["key"]
        if not key:
            print(f"  {aid} has no key bound. Use: config keys {aid} <char>")
            return
        conflict = _key_conflict(cfg, aid, key)
        if conflict:
            print(f"Key {key!r} already bound to {conflict!r}. Disable it first.")
            return
        cfg.keys[aid]["enabled"] = True
        cfg.save(CONFIG_FILE)
        print(f"  {aid} enabled (key={key!r})")
        return

    if len(val) != 1:
        print(f"Key must be a single character: {val!r}")
        return

    conflict = _key_conflict(cfg, aid, val)
    if conflict:
        print(f"Key {val!r} already bound to {conflict!r}. Disable it first.")
        return

    cfg.keys[aid]["key"] = val
    cfg.keys[aid]["enabled"] = True
    cfg.save(CONFIG_FILE)
    print(f"  {aid} -> {val!r} (enabled)")


def _config_fields(cfg: StatusConfig, parts: list) -> None:
    """Handle 'config fields ...': view or set status-line fields."""
    valid = {key for key, _, _ in StatusConfig.FIELDS}

    if not parts:
        print("Status fields:")
        for key, desc, _ in StatusConfig.FIELDS:
            state = "on " if cfg.show[key] else "off"
            print(f"  [{state}] {key:<11} {desc}")
        print("Usage: config fields <name> [on|off]   (omit on/off to toggle)")
        return

    key = parts[0].lower()
    if key not in valid:
        print(f"Unknown field: {key!r}. Valid: {', '.join(sorted(valid))}")
        return

    if len(parts) > 1:
        cfg.show[key] = parts[1].lower() in ("on", "1", "true", "yes")
    else:
        cfg.show[key] = not cfg.show[key]

    cfg.save(CONFIG_FILE)
    print(f"  {key} = {'on' if cfg.show[key] else 'off'}")


def _run_config(cfg: StatusConfig, arg: str) -> None:
    """View or dispatch gcgo config subsections: fields, rate, units, keys."""
    parts = arg.split()

    if not parts:
        on_fields = sum(1 for v in cfg.show.values() if v)
        rate = f"{cfg.rate}s" if cfg.rate > 0 else "off"
        bound = "  ".join(
            f"[{cfg.keys[aid]['key']}] {aid}"
            for aid, *_ in STREAM_ACTIONS
            if cfg.keys[aid]["enabled"] and cfg.keys[aid]["key"]
        )
        print("Config:")
        print(f"  fields   {on_fields}/{len(cfg.show)} shown   (config fields)")
        print(f"  rate     {rate}            (config rate <seconds>)")
        print(f"  units    {cfg.units}             (config units <mm|inch>)")
        print(f"  after    {cfg.after}          (config after <keep|clear>)")
        print(f"  keys     {bound or '(none bound)'}")
        print("           (config keys)")
        return

    section = parts[0].lower()

    if section == "fields":
        _config_fields(cfg, parts[1:])
        return

    if section == "keys":
        _config_keys(cfg, parts[1:])
        return

    if section == "rate":
        if len(parts) > 1:
            try:
                cfg.rate = max(0.0, float(parts[1]))
            except ValueError:
                print(f"Invalid rate: {parts[1]!r}")
                return
            cfg.save(CONFIG_FILE)
        rate = f"{cfg.rate}s" if cfg.rate > 0 else "off"
        print(f"  rate = {rate}")
        return

    if section == "units":
        if len(parts) > 1:
            val = parts[1].lower()
            if val in ("mm", "metric"):
                cfg.units = "mm"
            elif val in ("inch", "inches", "in", "imperial"):
                cfg.units = "inch"
            else:
                print(f"Invalid units: {parts[1]!r} (use mm or inch)")
                return
            cfg.save(CONFIG_FILE)
        print(f"  units = {cfg.units}")
        return

    if section == "after":
        if len(parts) > 1:
            val = parts[1].lower()
            if val not in ("keep", "clear"):
                print(f"Invalid value: {parts[1]!r} (use keep or clear)")
                return
            cfg.after = val
            cfg.save(CONFIG_FILE)
        print(f"  after = {cfg.after}")
        return

    print(f"Unknown config section: {section!r}. Valid: fields, rate, units, after, keys")


def run(streamer: Streamer, cfg: StatusConfig) -> None:
    """Connect banner + unit sync, then the interactive REPL."""
    try:
        greeting = streamer.connect()
        msg = _connect_message(greeting, streamer)
        print(_grbl(msg) if msg else "  (no response from GRBL — check baud/port)")
        # Reports stay in mm and gcgo converts, so values always match labels.
        _report_mm(streamer)
        print(f"Display units: {cfg.units}")
    except KeyboardInterrupt:
        print("\nConnection aborted.")
        streamer.disconnect()
        sys.exit(1)
    except Exception as e:
        print(f"Connection failed: {e}")
        streamer.disconnect()
        sys.exit(1)

    keyboard.install()
    print(HELP)
    try:
        _repl(streamer, cfg)
    finally:
        streamer.disconnect()
        print("Disconnected.")


def _repl(streamer: Streamer, cfg: StatusConfig) -> None:
    loaded_file = None
    grbl_status = streamer.status  # single retained status, updated by the pump

    while True:
        prompt = f"gcgo [{os.path.basename(loaded_file)}]> " if loaded_file else "gcgo [no file]> "
        try:
            raw = input(prompt).strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break

        if not raw:
            continue

        parts = raw.split(None, 1)
        cmd = parts[0].lower()
        arg = parts[1] if len(parts) > 1 else ""

        if cmd in ("quit", "exit"):
            break

        # One bad command (parse error, transient serial hiccup) must not
        # end the session — report it and return to the prompt.
        try:
            if cmd == "help":
                print(HELP)

            elif cmd == "ports":
                for p in list_ports():
                    print(f"  {p}")

            elif cmd == "load":
                if not arg:
                    print("Usage: load <file>")
                else:
                    path = os.path.expanduser(arg)
                    try:
                        n = validate_gcode(path)
                    except (OSError, ValueError) as e:
                        print(f"  {e}")
                    else:
                        loaded_file = path
                        print(f"Loaded {os.path.basename(path)} ({n} lines)")

            elif cmd == "ls":
                target = os.path.expanduser(arg) if arg else "."
                try:
                    for name in sorted(os.listdir(target)):
                        suffix = "/" if os.path.isdir(os.path.join(target, name)) else ""
                        print(f"  {name}{suffix}")
                except OSError as e:
                    print(f"  {e}")

            elif cmd == "cd":
                try:
                    os.chdir(os.path.expanduser(arg) if arg else os.path.expanduser("~"))
                    print(f"  {os.getcwd()}")
                except OSError as e:
                    print(f"  {e}")

            elif cmd == "mdi":
                _run_mdi(streamer, cfg)

            elif cmd == "settings":
                _print_settings(streamer, arg.strip())

            elif cmd == "params":
                _print_params(streamer)

            elif cmd == "unlock":
                streamer.send_command_verbose("$X", on_line=lambda l: print(_grbl(l)))

            elif cmd == "home":
                print("Homing...")
                streamer.send_command_verbose("$H", on_line=lambda l: print(_grbl(l)), read_timeout=120)

            elif cmd == "check":
                streamer.send_command_verbose("$C", on_line=lambda l: print(_grbl(l)))

            elif cmd == "run":
                if not loaded_file:
                    print("No file loaded. Use: load <file>")
                else:
                    completed = _run_stream(streamer, loaded_file, cfg)
                    if completed and cfg.after == "clear":
                        loaded_file = None
                        print("File unloaded.")

            elif cmd == "config":
                _run_config(cfg, arg)

            elif cmd == "reset":
                greeting = streamer.soft_reset()
                if greeting:
                    print(_grbl(greeting))

            elif cmd == "status":
                grbl_status.update(streamer.query_status())
                _print_status_detail(grbl_status, cfg)

            else:
                print(f"Unknown command: {cmd!r}. Type 'help' for available commands or 'mdi' to send gcode directly.")

        except KeyboardInterrupt:
            # Abort the current command and return to the prompt (rather than
            # crashing out of a blocking read like 'home'). Drop any partial
            # response so it can't desync the next command.
            print("^C")
            streamer.flush_input()
        except Exception as e:
            print(f"Error: {e}")
