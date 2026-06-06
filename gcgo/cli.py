"""Interactive REPL for gcgo."""

import argparse
import sys
import threading

from gcgo.streamer import GRBLStreamer


HELP = """
Commands:
  load <file>   Load a gcode file (does not start streaming)
  run           Stream the loaded file
  stop          Abort streaming
  reset         Send GRBL soft-reset (Ctrl-X)
  status        Query GRBL status (?)
  ports         List available serial ports
  help          Show this message
  quit / exit   Exit

Any other input is sent as a raw gcode command.
"""


def list_ports() -> list[str]:
    from serial.tools import list_ports
    return [p.device for p in list_ports.comports()]


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
    stream_thread: threading.Thread | None = None

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
                elif stream_thread and stream_thread.is_alive():
                    print("Already streaming. Use 'stop' first.")
                else:
                    def _stream():
                        try:
                            streamer.stream_file(
                                loaded_file,
                                on_response=lambda n, r: print(f"  [{n}] {r}"),
                                on_progress=lambda s, t: print(
                                    f"\r  Sent {s}/{t}", end="", flush=True
                                ),
                            )
                        except Exception as e:
                            print(f"\nStream error: {e}")
                        else:
                            print("\nDone.")

                    stream_thread = threading.Thread(target=_stream, daemon=True)
                    stream_thread.start()

            elif cmd == "stop":
                streamer.stop_stream()
                print("Stop requested.")

            elif cmd == "reset":
                streamer.soft_reset()
                print("Reset sent.")

            elif cmd == "status":
                print(streamer.query_status())

            else:
                # treat as raw gcode
                resp = streamer.send_command(raw)
                print(f"  {resp}")

    finally:
        streamer.disconnect()
        print("Disconnected.")


if __name__ == "__main__":
    main()
