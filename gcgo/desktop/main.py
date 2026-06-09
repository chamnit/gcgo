"""Desktop entry point: parse args, pick a port, wire adapters, run the REPL."""

from __future__ import annotations

import argparse
import sys

from gcgo.core.config import StatusConfig
from gcgo.core.protocol import Streamer
from gcgo.desktop.paths import CONFIG_FILE
from gcgo.desktop.transport import PySerialTransport, list_ports
from gcgo.frontends import terminal


def _pick_port() -> str:
    ports = list_ports()
    if not ports:
        print("No serial ports found. Specify a port: gcgo <port>")
        sys.exit(1)
    if len(ports) == 1:
        print(f"Using {ports[0]}")
        return ports[0]
    print("Available ports:")
    for i, p in enumerate(ports):
        print(f"  [{i}] {p}")
    try:
        return ports[int(input("Select port number: "))]
    except (ValueError, IndexError):
        print("Invalid selection.")
        sys.exit(1)


def main():
    parser = argparse.ArgumentParser(
        prog="gcgo",
        description="Interactive GRBL gcode streamer",
    )
    parser.add_argument("port", nargs="?", help="Serial port (e.g. /dev/ttyUSB0)")
    parser.add_argument("-b", "--baud", type=int, default=115200, help="Baud rate")
    args = parser.parse_args()

    port = args.port or _pick_port()

    cfg = StatusConfig()
    cfg.load(CONFIG_FILE)

    print(f"Connecting to {port} at {args.baud} baud...")
    try:
        transport = PySerialTransport(port, args.baud)
    except Exception as e:
        print(f"Connection failed: {e}")
        sys.exit(1)

    terminal.run(Streamer(transport), cfg)


if __name__ == "__main__":
    main()
