"""Interactive REPL for gcgo."""

import argparse
import atexit
import glob
import json
import os
from pathlib import Path
import readline
import select
import shutil
import sys
import termios
import threading
import tty

from gcgo.streamer import GRBLStatus, GRBLStreamer


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
except FileNotFoundError:
    pass
atexit.register(readline.write_history_file, _HISTORY_FILE)


# REPL command names (for tab-completion of the first word)
COMMANDS = (
    "load", "run", "mdi", "settings", "params", "unlock", "home", "check",
    "fo", "config", "reset", "status", "ports", "ls", "cd", "help",
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


class StatusConfig:
    """Streaming status display config: which fields show and how often
    GRBL is polled for a status report.

    Choices are persisted so they stick per install/machine.
    """

    DEFAULT_RATE = 1.0     # seconds between '?' status polls; 0 disables
    DEFAULT_UNITS = "mm"   # "mm" or "inch"; gcgo owns GRBL's $13 to match

    # ordered (key, description, default) — order is the display order
    FIELDS = (
        ("state",      "machine state",      True),
        ("wpos",       "work position",      True),
        ("mpos",       "machine position",   False),
        ("wco",        "work coord offset",  False),
        ("feed",       "feed rate",          True),
        ("spindle",    "spindle speed",      True),
        ("feed_ov",    "feed override",      True),
        ("rapid_ov",   "rapid override",     True),
        ("spindle_ov", "spindle override",   True),
        ("pins",       "limit/control pins", True),
    )

    def __init__(self):
        self.show = {key: default for key, _, default in self.FIELDS}
        self.rate = self.DEFAULT_RATE
        self.units = self.DEFAULT_UNITS

    @property
    def pos_unit(self) -> str:
        return "in" if self.units == "inch" else "mm"

    @property
    def feed_unit(self) -> str:
        return "in/min" if self.units == "inch" else "mm/min"

    @property
    def grbl_inch(self) -> str:
        """The $13 value matching this units setting."""
        return "1" if self.units == "inch" else "0"

    def load(self, path: Path) -> None:
        try:
            data = json.loads(path.read_text())
        except (FileNotFoundError, ValueError):
            return
        fields = data.get("fields", {})
        for key in self.show:
            if key in fields:
                self.show[key] = bool(fields[key])
        if "rate" in data:
            try:
                self.rate = max(0.0, float(data["rate"]))
            except (TypeError, ValueError):
                pass
        if data.get("units") in ("mm", "inch"):
            self.units = data["units"]

    def save(self, path: Path) -> None:
        path.write_text(json.dumps(
            {"fields": self.show, "rate": self.rate, "units": self.units},
            indent=2,
        ))


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
  fo            Reset feed rate override to 100%
  config        Configure status fields, query rate, and units
  reset         Send GRBL soft-reset (Ctrl-X)
  status        Query GRBL status (?)
  ports         List available serial ports
  ls [dir]      List files in a directory
  cd [dir]      Change working directory
  help          Show this message
  quit / exit   Exit

Tab completes commands and file paths (load/cd/ls).
"""

# GRBL 1.1 setting index → (description, unit)
# unit "bool" triggers enabled/disabled formatting
_GRBL_SETTINGS: dict[int, tuple[str, str]] = {
    0:   ("Step pulse time",            "µs"),
    1:   ("Step idle delay",            "ms"),
    2:   ("Step pulse invert",          "mask"),
    3:   ("Step direction invert",      "mask"),
    4:   ("Invert step enable pin",     "bool"),
    5:   ("Invert limit pins",          "bool"),
    6:   ("Invert probe pin",           "bool"),
    10:  ("Status report options",      "mask"),
    11:  ("Junction deviation",         "mm"),
    12:  ("Arc tolerance",              "mm"),
    13:  ("Report in inches",           "bool"),
    20:  ("Soft limits",                "bool"),
    21:  ("Hard limits",                "bool"),
    22:  ("Homing cycle",               "bool"),
    23:  ("Homing direction invert",    "mask"),
    24:  ("Homing locate feed rate",    "mm/min"),
    25:  ("Homing search seek rate",    "mm/min"),
    26:  ("Homing switch debounce",     "ms"),
    27:  ("Homing switch pull-off",     "mm"),
    30:  ("Max spindle speed",          "RPM"),
    31:  ("Min spindle speed",          "RPM"),
    32:  ("Laser mode",                 "bool"),
    100: ("X-axis steps/mm",            "steps/mm"),
    101: ("Y-axis steps/mm",            "steps/mm"),
    102: ("Z-axis steps/mm",            "steps/mm"),
    110: ("X-axis max rate",            "mm/min"),
    111: ("Y-axis max rate",            "mm/min"),
    112: ("Z-axis max rate",            "mm/min"),
    120: ("X-axis acceleration",        "mm/sec²"),
    121: ("Y-axis acceleration",        "mm/sec²"),
    122: ("Z-axis acceleration",        "mm/sec²"),
    130: ("X-axis max travel",          "mm"),
    131: ("Y-axis max travel",          "mm"),
    132: ("Z-axis max travel",          "mm"),
}


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


_PARAM_NAMES: dict[str, str] = {
    "G54": "Work offset 1",
    "G55": "Work offset 2",
    "G56": "Work offset 3",
    "G57": "Work offset 4",
    "G58": "Work offset 5",
    "G59": "Work offset 6",
    "G28": "Stored home 1",
    "G30": "Stored home 2",
    "G92": "Coordinate offset",
    "TLO": "Tool length offset",
    "PRB": "Probe position",
}


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

STREAM_KEYS = (
    "  Streaming — keys: [!] hold  [~] resume  "
    "[+/-] feed ±10%  [0] feed reset  [q] stop\n"
)

# GRBL 1.1 error code → human-readable description
_GRBL_ERRORS: dict[int, str] = {
    1:  "Expected command letter",
    2:  "Bad number format",
    3:  "Invalid statement",
    4:  "Negative value not allowed",
    5:  "Setting disabled",
    6:  "Step pulse time must be > 3 µs",
    7:  "EEPROM read failed — using defaults",
    8:  "Command only valid when idle",
    9:  "G-code locked out during alarm or jog state",
    10: "Soft limits require homing to be enabled",
    11: "Line too long — truncated",
    12: "Step rate would exceed 30 kHz",
    13: "Safety door opened",
    14: "Build info or startup line too long for EEPROM",
    15: "Jog target exceeds machine travel",
    16: "Invalid jog command",
    17: "Laser mode requires PWM output",
    20: "Unsupported g-code command",
    21: "Modal group violation — conflicting g-code commands",
    22: "Feed rate undefined",
    23: "G-code command requires an integer value",
    24: "Two commands both require XYZ axis words",
    25: "G-code word repeated in block",
    26: "Axis words required but not found",
    27: "Line number out of range (1–9,999,999)",
    28: "Missing required P or L value",
    29: "Axis words present but unused by any command",
    30: "No axis words found for command that requires them",
    31: "Value of zero not allowed",
    32: "Arc motion requires a specific active plane",
    33: "Arc radius tolerance exceeded — not a valid arc",
    34: "Missing required value word for command",
    35: "G53 requires G0 or G1 motion mode",
    36: "Unused axis words with G80 active",
    37: "Missing offset word for G2/G3 arc",
    38: "Motion command targets unconfigured axis",
    39: "Invalid G2/G3 target or undefined radius",
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
            print(f"  {streamer.query_status()}")
        elif line == "!":
            streamer.feed_hold()
        elif line == "~":
            streamer.cycle_start()
        elif "\x18" in line:
            greeting = streamer.soft_reset()
            if greeting:
                print(f"  {greeting}")
        else:
            streamer.send_command_verbose(line, on_line=lambda l: print(f"  {l}"))
            # gcgo owns $13 to keep status units in sync with its labels.
            if line.replace(" ", "").lower().startswith("$13="):
                _apply_units(streamer, cfg)
                print(f"  [gcgo] $13 re-asserted to {cfg.grbl_inch} (units={cfg.units})")


def _run_stream(streamer: GRBLStreamer, path: str, st: GRBLStatus, cfg: StatusConfig) -> None:
    """Stream a file and read interactive keypresses until done."""
    progress = ""

    def status_line() -> str:
        return _format_status_line(st, progress, cfg)

    def on_response(n, resp):
        if not resp.startswith("ok"):
            desc = ""
            if resp.startswith("error:"):
                try:
                    code = int(resp.split(":")[1])
                    desc = f"  — {_GRBL_ERRORS[code]}" if code in _GRBL_ERRORS else ""
                except (IndexError, ValueError):
                    pass
            _print_above(f"  [{n}] {resp}{desc}", status_line())

    def on_progress(s, t, line):
        nonlocal progress
        progress = f"{s}/{t} ({s * 100 // t}%)"
        _print_above(f"  >> {line}", status_line())

    def on_message(msg):
        if msg.startswith("<"):
            st.update(msg)
            _redraw_status(status_line())
        else:
            _print_above(f"  {msg}", status_line())

    stream_done = threading.Event()

    def _stream():
        try:
            streamer.stream_file(
                path,
                on_response=on_response,
                on_progress=on_progress,
                on_message=on_message,
                status_interval=cfg.rate,
            )
        except Exception as e:
            _print_above(f"  Stream error: {e}", "")
        finally:
            stream_done.set()

    threading.Thread(target=_stream, daemon=True).start()

    sys.stdout.write(STREAM_KEYS)
    sys.stdout.flush()

    if sys.stdin.isatty():
        fd = sys.stdin.fileno()
        old_settings = termios.tcgetattr(fd)
        try:
            tty.setcbreak(fd)
            while not stream_done.is_set():
                if select.select([sys.stdin], [], [], 0.05)[0]:
                    key = sys.stdin.read(1)
                    if key == "!":
                        streamer.feed_hold()
                        _print_above("  [feed hold]", status_line())
                    elif key == "~":
                        streamer.cycle_start()
                        _print_above("  [cycle start]", status_line())
                    elif key == "+":
                        streamer.feed_override_plus10()
                        _print_above("  [feed +10%]", status_line())
                    elif key == "-":
                        streamer.feed_override_minus10()
                        _print_above("  [feed -10%]", status_line())
                    elif key == "0":
                        streamer.feed_override_reset()
                        _print_above("  [feed reset 100%]", status_line())
                    elif key == "q":
                        streamer.stop_stream()
                        break
        except KeyboardInterrupt:
            streamer.stop_stream()
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)

    stream_done.wait()
    with _stdout_lock:
        sys.stdout.write("\nDone.\n")
        sys.stdout.flush()


def _apply_units(streamer: GRBLStreamer, cfg: StatusConfig) -> None:
    """Write GRBL's $13 to match the configured display units."""
    streamer.send_command(f"$13={cfg.grbl_inch}")


def _run_config(cfg: StatusConfig, arg: str, streamer: GRBLStreamer) -> None:
    """View or set gcgo display config (status fields, poll rate, units)."""
    valid = {key for key, _, _ in StatusConfig.FIELDS}
    parts = arg.split()

    if not parts:
        print("Streaming status config:")
        for key, desc, _ in StatusConfig.FIELDS:
            state = "on " if cfg.show[key] else "off"
            print(f"  [{state}] {key:<11} {desc}")
        rate = f"{cfg.rate}s" if cfg.rate > 0 else "off"
        print(f"        rate        status query rate ({rate})")
        print(f"        units       report units / $13 ({cfg.units})")
        print("Usage: config <field> [on|off]   (omit on/off to toggle)")
        print("       config rate <seconds>     (0 disables polling)")
        print("       config units <mm|inch>")
        return

    key = parts[0].lower()

    if key == "rate":
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

    if key == "units":
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

    if key not in valid:
        print(f"Unknown field: {key!r}. Valid: {', '.join(sorted(valid))}, rate, units")
        return

    if len(parts) > 1:
        cfg.show[key] = parts[1].lower() in ("on", "1", "true", "yes")
    else:
        cfg.show[key] = not cfg.show[key]

    cfg.save(_CONFIG_FILE)
    print(f"  {key} = {'on' if cfg.show[key] else 'off'}")


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

    streamer = GRBLStreamer(port, args.baud)
    loaded_file: str | None = None
    grbl_status = GRBLStatus()
    field_config = StatusConfig()
    field_config.load(_CONFIG_FILE)

    print(f"Connecting to {port} at {args.baud} baud...")
    try:
        greeting = streamer.connect()
        print(f"GRBL: {greeting}")
    except Exception as e:
        print(f"Connection failed: {e}")
        sys.exit(1)

    # gcgo owns GRBL's $13 so report units always match its display labels.
    _apply_units(streamer, field_config)
    print(f"Report units: {field_config.units} ($13={field_config.grbl_inch})")

    _install_completer()
    print(HELP)

    try:
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

            if cmd in ("quit", "exit"):
                break

            elif cmd == "help":
                print(HELP)

            elif cmd == "ports":
                for p in list_ports():
                    print(f"  {p}")

            elif cmd == "load":
                if not arg:
                    print("Usage: load <file>")
                else:
                    loaded_file = os.path.expanduser(arg)
                    print(f"Loaded: {loaded_file}")

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
                streamer.send_command_verbose("$X", on_line=lambda l: print(f"  {l}"))

            elif cmd == "home":
                print("Homing...")
                streamer.send_command_verbose("$H", on_line=lambda l: print(f"  {l}"), read_timeout=120)

            elif cmd == "check":
                streamer.send_command_verbose("$C", on_line=lambda l: print(f"  {l}"))

            elif cmd == "run":
                if not loaded_file:
                    print("No file loaded. Use: load <file>")
                else:
                    _run_stream(streamer, loaded_file, grbl_status, field_config)

            elif cmd == "config":
                _run_config(field_config, arg, streamer)

            elif cmd == "fo":
                streamer.feed_override_reset()
                print("Feed override reset to 100%.")

            elif cmd == "reset":
                greeting = streamer.soft_reset()
                if greeting:
                    print(f"  {greeting}")

            elif cmd == "status":
                raw = streamer.query_status()
                grbl_status.update(raw)
                _print_status_detail(grbl_status, field_config)

            else:
                print(f"Unknown command: {cmd!r}. Type 'help' for available commands or 'mdi' to send gcode directly.")

    finally:
        streamer.disconnect()
        print("Disconnected.")


if __name__ == "__main__":
    main()
