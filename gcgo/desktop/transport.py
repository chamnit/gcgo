"""Desktop Transport backed by pyserial."""

from __future__ import annotations

import serial
from serial.tools import list_ports as _list_ports

GRBL_BAUD = 115200


def list_ports() -> list[str]:
    """Names of available serial ports."""
    return [p.device for p in _list_ports.comports()]


class PySerialTransport:
    """Byte-level Transport over a pyserial port. Opens the port on construction."""

    def __init__(self, port: str, baud: int = GRBL_BAUD, timeout: float = 2.0):
        self._s = serial.Serial(port, baud, timeout=timeout)

    def write(self, data: bytes) -> None:
        self._s.write(data)

    def read(self, n: int = 64) -> bytes:
        return self._s.read(n)

    def readinto(self, buf) -> int:
        return self._s.readinto(buf) or 0

    def any(self) -> int:
        return self._s.in_waiting

    def set_timeout(self, seconds: float) -> None:
        self._s.timeout = seconds

    def reset_input(self) -> None:
        self._s.reset_input_buffer()

    def is_open(self) -> bool:
        return bool(self._s and self._s.is_open)

    def close(self) -> None:
        if self._s and self._s.is_open:
            self._s.close()
