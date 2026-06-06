"""Interactive REPL for gcgo."""

import argparse
import select
import shutil
import sys
import termios
import threading
import tty

from gcgo.streamer import GRBLStreamer


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
  hold          Feed hold (!)
  resume        Cycle start / resume (~)
  fo            Feed rate override reset to 100%
  fo+           Feed rate override +10%
  fo-           Feed rate override -10%
  fo++          Feed rate override +1%
  fo--          Feed rate override -1%
  reset         Send GRBL soft-reset (Ctrl-X)
  status        Query GRBL status (?)
  ports         List available serial ports
  help          Show this message
  quit / exit   Exit

Any other input is sent as a raw gcode command.
"""

STREAM_KEYS = (
    "  Streaming — keys: [!] hold  [~] resume  "
    "[+/-] feed ±10%  [0] feed reset  [q] stop\n"
)


def list_ports() -> list[str]:
    from serial.tools import list_ports
    return [p.device for p in list_ports.comports()]


def _run_stream(streamer: GRBLStreamer, path: str) -> None:
    """Stream a file and read interactive keypresses until done."""
    grbl_status = ""
    progress = ""

    def status_line() -> str:
        parts = [p for p in (grbl_status, progress) if p]
        return "  ".join(parts)

    def on_response(n, resp):
        if not resp.startswith("ok"):
            _print_above(f"  {resp}", status_line())

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

            elif cmd == "run":
                if not loaded_file:
                    print("No file loaded. Use: load <file>")
                else:
                    _run_stream(streamer, loaded_file)

            elif cmd == "hold":
                streamer.feed_hold()
                print("Feed hold.")

            elif cmd == "resume":
                streamer.cycle_start()
                print("Cycle start.")

            elif cmd == "fo":
                streamer.feed_override_reset()
                print("Feed override reset to 100%.")

            elif cmd == "fo+":
                streamer.feed_override_plus10()
                print("Feed override +10%.")

            elif cmd == "fo-":
                streamer.feed_override_minus10()
                print("Feed override -10%.")

            elif cmd == "fo++":
                streamer.feed_override_plus1()
                print("Feed override +1%.")

            elif cmd == "fo--":
                streamer.feed_override_minus1()
                print("Feed override -1%.")

            elif cmd == "reset":
                streamer.soft_reset()
                print("Reset sent.")

            elif cmd == "status":
                print(streamer.query_status())

            else:
                resp = streamer.send_command(raw)
                print(f"  {resp}")

    finally:
        streamer.disconnect()
        print("Disconnected.")


if __name__ == "__main__":
    main()
