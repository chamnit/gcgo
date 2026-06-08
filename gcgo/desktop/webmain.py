"""Desktop launcher for the web front-end: connect over pyserial and serve the
browser UI with asyncio.  Run:  python -m gcgo.desktop.webmain /dev/ttyACM0
"""

import argparse
import asyncio
import os
import sys

from gcgo.core.config import StatusConfig
from gcgo.core.protocol import Streamer
from gcgo.desktop.paths import CONFIG_FILE
from gcgo.desktop.transport import PySerialTransport, list_ports
from gcgo.frontends.web.server import serve


def main():
    p = argparse.ArgumentParser(prog="gcgo-web", description="gcgo web front-end")
    p.add_argument("port", nargs="?", help="Serial port (e.g. /dev/ttyUSB0)")
    p.add_argument("-b", "--baud", type=int, default=115200)
    p.add_argument("--http-port", type=int, default=8080)
    p.add_argument("--dir", default=".", help="directory of g-code files to serve")
    args = p.parse_args()

    port = args.port
    if not port:
        ports = list_ports()
        if not ports:
            print("No serial ports found. Specify a port.")
            sys.exit(1)
        port = ports[0]
        print(f"Using {port}")

    cfg = StatusConfig()
    cfg.load(CONFIG_FILE)

    print(f"Connecting to {port} at {args.baud} baud...")
    try:
        streamer = Streamer(PySerialTransport(port, args.baud))
        streamer.connect()
    except Exception as e:
        print(f"Connection failed: {e}")
        sys.exit(1)

    gdir = os.path.abspath(args.dir)
    try:
        asyncio.run(serve(streamer, cfg, gdir, config_file=CONFIG_FILE,
                          port=args.http_port))
    except KeyboardInterrupt:
        print("\nShutting down.")
    finally:
        streamer.disconnect()


if __name__ == "__main__":
    main()
