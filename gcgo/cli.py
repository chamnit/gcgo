"""Interactive REPL for gcgo."""

import argparse
import atexit
import os
from pathlib import Path
import readline
import select
import shutil
import sys
import termios
import threading
import tty

from gcgo.streamer import GRBLStreamer

def _history_file() -> Path:
    if sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    elif sys.platform == "win32":
        base = Path(os.environ.get("APPDATA", Path.home()))
    else:
        base = Path.home() / ".config"
    return base / "gcgo" / "history"

_HISTORY_FILE = _history_file()
_HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
readline.set_history_length(500)
try:
    readline.read_history_file(_HISTORY_FILE)
except FileNotFoundError:
    pass
atexit.register(readline.write_history_file, _HISTORY_FILE)


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
  reset         Send GRBL soft-reset (Ctrl-X)
  status        Query GRBL status (?)
  ports         List available serial ports
  help          Show this message
  quit / exit   Exit

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


def _run_mdi(streamer: GRBLStreamer) -> None:
    """MDI mode: send gcode commands one at a time. Exit with 'exit' or Ctrl-C."""
    print("MDI mode — type gcode commands, 'exit' to return.")
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

        # Real-time commands: bypass the normal send/wait-for-ok path.
        if line == "?":
            print(f"  {streamer.query_status()}")
        elif line == "!":
            streamer.feed_hold()
        elif line == "~":
            streamer.cycle_start()
        else:
            streamer.send_command_verbose(line, on_line=lambda l: print(f"  {l}"))


def _run_stream(streamer: GRBLStreamer, path: str) -> None:
    """Stream a file and read interactive keypresses until done."""
    grbl_status = ""
    progress = ""

    def status_line() -> str:
        parts = [p for p in (grbl_status, progress) if p]
        return "  ".join(parts)

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
        nonlocal grbl_status
        if msg.startswith("<"):
            grbl_status = msg
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

    print(f"Connecting to {port} at {args.baud} baud...")
    try:
        greeting = streamer.connect()
        print(f"GRBL: {greeting}")
    except Exception as e:
        print(f"Connection failed: {e}")
        sys.exit(1)

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
                    loaded_file = arg
                    print(f"Loaded: {loaded_file}")

            elif cmd == "mdi":
                _run_mdi(streamer)

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
                    _run_stream(streamer, loaded_file)

            elif cmd == "fo":
                streamer.feed_override_reset()
                print("Feed override reset to 100%.")

            elif cmd == "reset":
                streamer.soft_reset()
                print("Reset sent.")

            elif cmd == "status":
                print(streamer.query_status())

            else:
                print(f"Unknown command: {cmd!r}. Type 'help' for available commands or 'mdi' to send gcode directly.")

    finally:
        streamer.disconnect()
        print("Disconnected.")


if __name__ == "__main__":
    main()
