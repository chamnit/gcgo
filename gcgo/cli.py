"""Interactive REPL for gcgo."""

import argparse
import atexit
import glob
import os
from pathlib import Path
import readline
import select
import shutil
import sys
import termios
import threading
import tty

from gcgo.core.config import StatusConfig
from gcgo.core.status import GRBLStatus
from gcgo.core.tables import (
    ACTION_DESC as _ACTION_DESC,
    ACTION_METHOD as _ACTION_METHOD,
    GRBL_ERRORS as _GRBL_ERRORS,
    GRBL_SETTINGS as _GRBL_SETTINGS,
    PARAM_NAMES as _PARAM_NAMES,
    STREAM_ACTIONS,
)
from gcgo.desktop.transport import PySerialTransport
from gcgo.streamer import GRBLStreamer, load_gcode


def _config_dir() -> Path:
    if sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    elif sys.platform == "win32":
        base = Path(os.environ.get("APPDATA", Path.home()))
    else:
        base = Path.home() / ".config"
    return base / "gcgo"


_CONFIG_DIR = _config_dir()
_CONFIG_DIR.mkdir(parents=True, exist_ok=True)
_HISTORY_FILE = _CONFIG_DIR / "history"
_CONFIG_FILE = _CONFIG_DIR / "config.json"

readline.set_history_length(500)
try:
    readline.read_history_file(_HISTORY_FILE)
except OSError:
    pass  # missing, unreadable, or malformed history — start fresh
atexit.register(readline.write_history_file, _HISTORY_FILE)


# REPL command names (for tab-completion of the first word)
COMMANDS = (
    "load", "run", "mdi", "settings", "params", "unlock", "home", "check",
    "config", "reset", "status", "ports", "ls", "cd", "help",
    "quit", "exit",
)

# commands whose argument is a filesystem path (for path completion)
_PATH_COMMANDS = ("load", "cd", "ls")


def _path_matches(text: str) -> list[str]:
    """Filesystem completions for the given partial path."""
    expanded = os.path.expanduser(text)
    out = []
    for p in glob.glob(expanded + "*"):
        # restore a leading ~ the user typed, since glob expands it away
        if text.startswith("~"):
            p = "~" + p[len(os.path.expanduser("~")):]
        out.append(p + "/" if os.path.isdir(os.path.expanduser(p)) else p)
    return sorted(out)


def _completer(text: str, state: int):
    line = readline.get_line_buffer().lstrip()
    if " " not in line:
        matches = [c + " " for c in COMMANDS if c.startswith(text.lower())]
    else:
        cmd = line.split(None, 1)[0].lower()
        matches = _path_matches(text) if cmd in _PATH_COMMANDS else []
    return matches[state] if state < len(matches) else None


def _install_completer() -> None:
    # treat only whitespace as word breaks so '/', '.', '-' stay part of paths
    readline.set_completer_delims(" \t\n")
    readline.set_completer(_completer)
    if readline.__doc__ and "libedit" in readline.__doc__:
        readline.parse_and_bind("bind ^I rl_complete")  # macOS libedit
    else:
        readline.parse_and_bind("tab: complete")


_stdout_lock = threading.Lock()


def _term_width() -> int:
    return shutil.get_terminal_size().columns


def _print_above(text: str, status: str) -> None:
    """Print a scrolling line above the persistent status line."""
    w = _term_width()
    with _stdout_lock:
        sys.stdout.write(f"\r{' ' * w}\r{text}\n{status}")
        sys.stdout.flush()


def _redraw_status(status: str) -> None:
    with _stdout_lock:
        sys.stdout.write(f"\r{status}")
        sys.stdout.flush()


def _grbl(line: str) -> str:
    """Format a raw GRBL message for display: indented and quoted, so GRBL's
    own words (ok, error:N, [MSG:...], the welcome string) are visually
    distinct from gcgo's output."""
    return f'  "{line}"'


def _format_status_line(st: GRBLStatus, progress: str, cfg: StatusConfig) -> str:
    """Compact single-line status for the streaming display.

    All values are fixed-width so they stay put when the line is reprinted.
    Fields can be turned off via StatusConfig.
    """
    if not st.state:
        return progress
    show = cfg.show
    u = cfg.pos_unit
    wx, wy, wz = st.wpos
    mx, my, mz = st.mpos
    ox, oy, oz = st.wco
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
        parts.append(f"F:{st.feed:6.0f} {cfg.feed_unit}")
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
    wx, wy, wz = st.wpos
    mx, my, mz = st.mpos
    pins_str = " ".join(st.pins) if st.pins else "none"
    print(f"""
  State:      {st.state}
  Work pos:   X: {wx:10.3f}  Y: {wy:10.3f}  Z: {wz:10.3f}  {u}
  Mach pos:   X: {mx:10.3f}  Y: {my:10.3f}  Z: {mz:10.3f}  {u}
  Feed:       {st.feed:.0f} {cfg.feed_unit}
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


def _print_settings(streamer: GRBLStreamer) -> None:
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

        desc, unit = _GRBL_SETTINGS.get(n, ("", ""))
        if unit == "bool":
            val_str = "enabled" if val.strip() == "1" else "disabled"
        elif unit:
            val_str = f"{val}  ({unit})"
        else:
            val_str = val

        desc_str = f"  {desc}" if desc else ""
        print(f"  {key:<5} = {val_str:<24}{desc_str}")
    print()


def _print_params(streamer: GRBLStreamer) -> None:
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
            print(f"  {key}  {name:<18}  Z: {rest:>10}")
        elif key == "PRB":
            coords, _, success = rest.rpartition(":")
            x, y, z = coords.split(",")
            result = "success" if success == "1" else "failed"
            print(f"  {key}  {name:<18}  X: {x:>10}  Y: {y:>10}  Z: {z:>10}  ({result})")
        else:
            x, y, z = rest.split(",")
            print(f"  {key}  {name:<18}  X: {x:>10}  Y: {y:>10}  Z: {z:>10}")
    print()

def _stream_keys_banner(cfg: StatusConfig) -> str:
    """Build the streaming key-hint banner from the enabled bindings."""
    bound = [
        f"[{cfg.keys[aid]['key']}] {desc}"
        for aid, desc, _k, _e, _m in STREAM_ACTIONS
        if cfg.keys[aid]["enabled"] and cfg.keys[aid]["key"]
    ]
    if not bound:
        return "  Streaming (no keys bound — use 'keys' to configure)\n"
    return "  Streaming — " + "   ".join(bound) + "\n"


def _build_keymap(cfg: StatusConfig) -> dict[str, str]:
    """char -> action id for all enabled, bound actions."""
    return {
        cfg.keys[aid]["key"]: aid
        for aid, _d, _k, _e, _m in STREAM_ACTIONS
        if cfg.keys[aid]["enabled"] and cfg.keys[aid]["key"]
    }

def list_ports() -> list[str]:
    from serial.tools import list_ports
    return [p.device for p in list_ports.comports()]


def _run_mdi(streamer: GRBLStreamer, cfg: StatusConfig) -> None:
    """MDI mode: send gcode commands one at a time. Exit with 'exit' or Ctrl-C."""
    print("MDI mode — type gcode commands, 'exit' to return.")
    saved_completer = readline.get_completer()
    readline.set_completer(None)  # no command/path completion at the mdi> prompt
    try:
        _mdi_loop(streamer, cfg)
    finally:
        readline.set_completer(saved_completer)


def _mdi_loop(streamer: GRBLStreamer, cfg: StatusConfig) -> None:
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

        # Commands that need special handling outside the normal send/wait-for-ok path.
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
            streamer.send_command_verbose(line, on_line=lambda l: print(_grbl(l)))
            # gcgo owns $13 to keep status units in sync with its labels.
            if line.replace(" ", "").lower().startswith("$13="):
                _apply_units(streamer, cfg)
                print(f"  [gcgo] $13 re-asserted to {cfg.grbl_inch} (units={cfg.units})")


def _run_stream(streamer: GRBLStreamer, path: str, st: GRBLStatus, cfg: StatusConfig) -> bool:
    """Stream a file and read interactive keypresses until done.

    Returns True only if the stream completed cleanly (no error, not stopped).
    """
    progress = ""
    sent = total = 0
    errored = False
    stopped = False

    def status_line() -> str:
        return _format_status_line(st, progress, cfg)

    def on_response(n, resp):
        nonlocal errored
        if not resp.startswith("ok"):
            desc = ""
            if resp.startswith("error:"):
                errored = True
                try:
                    code = int(resp.split(":")[1])
                    desc = f"  — {_GRBL_ERRORS[code]}" if code in _GRBL_ERRORS else ""
                except (IndexError, ValueError):
                    pass
            _print_above(f'  [{n}] "{resp}"{desc}', status_line())

    def on_progress(s, t, line):
        nonlocal progress, sent, total
        progress = f"{s}/{t} ({s * 100 // t}%)"
        sent, total = s, t
        _print_above(f"  >> {line}", status_line())

    def on_message(msg):
        if msg.startswith("<"):
            st.update(msg)
            _redraw_status(status_line())
        else:
            _print_above(_grbl(msg), status_line())

    stream_done = threading.Event()

    def _stream():
        nonlocal errored
        try:
            streamer.stream_file(
                path,
                on_response=on_response,
                on_progress=on_progress,
                on_message=on_message,
                status_interval=cfg.rate,
            )
        except Exception as e:
            errored = True
            _print_above(f"  Stream error: {e}", "")
        finally:
            stream_done.set()

    threading.Thread(target=_stream, daemon=True).start()

    keymap = _build_keymap(cfg)
    sys.stdout.write(_stream_keys_banner(cfg))
    sys.stdout.flush()

    if sys.stdin.isatty():
        fd = sys.stdin.fileno()
        old_settings = termios.tcgetattr(fd)
        try:
            tty.setcbreak(fd)
            while not stream_done.is_set():
                if select.select([sys.stdin], [], [], 0.05)[0]:
                    key = sys.stdin.read(1)
                    aid = keymap.get(key)
                    if aid is None:
                        continue
                    if aid == "stop":
                        stopped = True
                        streamer.stop_stream()
                        break
                    getattr(streamer, _ACTION_METHOD[aid])()
                    _print_above(f"  [{_ACTION_DESC[aid]}]", status_line())
        except KeyboardInterrupt:
            stopped = True
            streamer.stop_stream()
        except Exception as e:
            # Never abandon a running stream on a control-path error — stop it,
            # then fall through to the wait + cancel cleanup below.
            stopped = True
            streamer.stop_stream()
            _print_above(f"  input error: {e}", "")
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)

    stream_done.wait()
    completed = not errored and not stopped
    name = os.path.basename(path)
    if errored:
        msg = f"Stream halted on error: {name} ({sent}/{total} lines sent)"
    elif stopped:
        msg = f"Stream stopped: {name} ({sent}/{total} lines sent)"
    else:
        msg = f"Stream complete: {name} ({total} lines)"
    with _stdout_lock:
        sys.stdout.write(f"\n{msg}\n")
        sys.stdout.flush()

    # A stopped/errored stream leaves GRBL still running its buffered motion.
    # Abort and flush so the machine halts and the next run starts clean — but
    # only if we actually sent something; otherwise there's nothing to halt and
    # a needless soft-reset would wipe overrides / alarm a homing machine.
    if not completed and sent > 0:
        greeting = streamer.cancel()
        with _stdout_lock:
            sys.stdout.write("Machine reset to halt motion and flush buffers.\n")
            if greeting:
                sys.stdout.write(_grbl(greeting) + "\n")
            sys.stdout.flush()

    return completed


def _connect_message(greeting: str, streamer: GRBLStreamer) -> str:
    """GRBL's own words to show on connect: the welcome string if it arrived,
    otherwise a status report from '?' (this board doesn't reset on serial open).
    Empty if GRBL doesn't respond at all."""
    return greeting or streamer.query_status()


def _apply_units(streamer: GRBLStreamer, cfg: StatusConfig) -> None:
    """Write GRBL's $13 to match the configured display units."""
    streamer.send_command(f"$13={cfg.grbl_inch}")


def _key_conflict(cfg: StatusConfig, aid: str, key: str) -> str | None:
    """Return the id of another enabled action already bound to `key`, if any."""
    for other, entry in cfg.keys.items():
        if other != aid and entry["enabled"] and entry["key"] == key:
            return other
    return None


def _config_keys(cfg: StatusConfig, parts: list[str]) -> None:
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
        cfg.save(_CONFIG_FILE)
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
        cfg.save(_CONFIG_FILE)
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
    cfg.save(_CONFIG_FILE)
    print(f"  {aid} -> {val!r} (enabled)")


def _config_fields(cfg: StatusConfig, parts: list[str]) -> None:
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

    cfg.save(_CONFIG_FILE)
    print(f"  {key} = {'on' if cfg.show[key] else 'off'}")


def _run_config(cfg: StatusConfig, arg: str, streamer: GRBLStreamer) -> None:
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
        print(f"  units    {cfg.units} ($13={cfg.grbl_inch})       (config units <mm|inch>)")
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
            cfg.save(_CONFIG_FILE)
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
            cfg.save(_CONFIG_FILE)
            _apply_units(streamer, cfg)
        print(f"  units = {cfg.units}  ($13={cfg.grbl_inch})")
        return

    if section == "after":
        if len(parts) > 1:
            val = parts[1].lower()
            if val not in ("keep", "clear"):
                print(f"Invalid value: {parts[1]!r} (use keep or clear)")
                return
            cfg.after = val
            cfg.save(_CONFIG_FILE)
        print(f"  after = {cfg.after}")
        return

    print(f"Unknown config section: {section!r}. Valid: fields, rate, units, after, keys")


def main():
    parser = argparse.ArgumentParser(
        prog="gcgo",
        description="Interactive GRBL gcode streamer",
    )
    parser.add_argument("port", nargs="?", help="Serial port (e.g. /dev/ttyUSB0)")
    parser.add_argument("-b", "--baud", type=int, default=115200, help="Baud rate")
    args = parser.parse_args()

    port = args.port
    if not port:
        ports = list_ports()
        if not ports:
            print("No serial ports found. Specify a port: gcgo <port>")
            sys.exit(1)
        if len(ports) == 1:
            port = ports[0]
            print(f"Using {port}")
        else:
            print("Available ports:")
            for i, p in enumerate(ports):
                print(f"  [{i}] {p}")
            try:
                choice = int(input("Select port number: "))
                port = ports[choice]
            except (ValueError, IndexError):
                print("Invalid selection.")
                sys.exit(1)

    loaded_file: str | None = None
    grbl_status = GRBLStatus()
    field_config = StatusConfig()
    field_config.load(_CONFIG_FILE)

    print(f"Connecting to {port} at {args.baud} baud...")
    try:
        transport = PySerialTransport(port, args.baud)
    except Exception as e:
        print(f"Connection failed: {e}")
        sys.exit(1)
    streamer = GRBLStreamer(transport)

    try:
        greeting = streamer.connect()
        msg = _connect_message(greeting, streamer)
        print(_grbl(msg) if msg else "  (no response from GRBL — check baud/port)")
        # gcgo owns GRBL's $13 so report units always match its display labels.
        _apply_units(streamer, field_config)
        print(f"Report units: {field_config.units} ($13={field_config.grbl_inch})")
    except Exception as e:
        print(f"Connection failed: {e}")
        streamer.disconnect()
        sys.exit(1)

    _install_completer()
    print(HELP)

    try:
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
                            n = len(load_gcode(path))
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
                        os.chdir(os.path.expanduser(arg) if arg else Path.home())
                        print(f"  {os.getcwd()}")
                    except OSError as e:
                        print(f"  {e}")

                elif cmd == "mdi":
                    _run_mdi(streamer, field_config)

                elif cmd == "settings":
                    _print_settings(streamer)

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
                        completed = _run_stream(streamer, loaded_file, grbl_status, field_config)
                        if completed and field_config.after == "clear":
                            loaded_file = None
                            print("File unloaded.")

                elif cmd == "config":
                    _run_config(field_config, arg, streamer)

                elif cmd == "reset":
                    greeting = streamer.soft_reset()
                    if greeting:
                        print(_grbl(greeting))

                elif cmd == "status":
                    raw = streamer.query_status()
                    grbl_status.update(raw)
                    _print_status_detail(grbl_status, field_config)

                else:
                    print(f"Unknown command: {cmd!r}. Type 'help' for available commands or 'mdi' to send gcode directly.")

            except KeyboardInterrupt:
                # Abort the current command and return to the prompt (rather
                # than crashing out of a blocking read like 'home'). Drop any
                # partial response so it can't desync the next command.
                print("^C")
                streamer.flush_input()
            except Exception as e:
                print(f"Error: {e}")

    finally:
        streamer.disconnect()
        print("Disconnected.")


if __name__ == "__main__":
    main()
