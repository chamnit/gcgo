"""Desktop Transport backed by pyserial."""

from __future__ import annotations

import serial
from serial.tools import list_ports as _list_ports

GRBL_BAUD = 115200

# what a port that went away raises (SerialException is an OSError; pyserial's
# termios calls raise termios.error, which is not)
try:
    import termios
    _ERRORS = (OSError, termios.error)
except ImportError:                       # Windows
    _ERRORS = (OSError,)


def list_ports() -> list[str]:
    """Names of available serial ports."""
    return [p.device for p in _list_ports.comports()]


class PySerialTransport:
    """Byte-level Transport over a pyserial port. Opens the port on construction.

    A port that fails later (a USB cable pulled, the controller power-cycled)
    closes itself instead of raising: is_open() turns False, writes are
    dropped and reads find nothing, so no caller dies on it. reopen() tries
    the same port again (the Streamer's reconnect())."""

    def __init__(self, port: str, baud: int = GRBL_BAUD, timeout: float = 2.0):
        self.port = port
        self._baud = baud
        self._timeout = timeout
        self._s = serial.Serial(port, baud, timeout=timeout)

    def _lost(self):
        try:
            self._s.close()
        except _ERRORS:
            pass

    def write(self, data: bytes) -> None:
        if self._s.is_open:
            try:
                self._s.write(data)
            except _ERRORS:
                self._lost()

    def read(self, n: int = 64) -> bytes:
        # pyserial's read(n) blocks trying to fill all n bytes, holding the
        # read open for the full timeout even once a short reply (e.g.
        # "ok\n") has already arrived. Wait out the timeout for the first
        # byte only, then drain whatever's already buffered without
        # blocking further, to match this method's documented contract.
        if not self._s.is_open:
            return b""
        try:
            data = self._s.read(1)
            if data and n > 1 and self._s.in_waiting:
                data += self._s.read(min(n - 1, self._s.in_waiting))
            return data
        except _ERRORS:
            self._lost()
            return b""

    def readinto(self, buf) -> int:
        if not self._s.is_open:
            return 0
        try:
            return self._s.readinto(buf) or 0
        except _ERRORS:
            self._lost()
            return 0

    def any(self) -> int:
        if not self._s.is_open:
            return 0
        try:
            return self._s.in_waiting
        except _ERRORS:
            self._lost()
            return 0

    def set_timeout(self, seconds: float) -> None:
        self._timeout = seconds
        if self._s.is_open:
            try:
                self._s.timeout = seconds
            except _ERRORS:
                self._lost()

    def reset_input(self) -> None:
        if self._s.is_open:
            try:
                self._s.reset_input_buffer()
            except _ERRORS:
                self._lost()

    def is_open(self) -> bool:
        return bool(self._s and self._s.is_open)

    def reopen(self) -> bool:
        """Open the port again after it was lost; False if it is not back yet."""
        if self._s.is_open:
            return True
        try:
            self._s = serial.Serial(self.port, self._baud, timeout=self._timeout)
        except _ERRORS:
            return False
        return True

    def close(self) -> None:
        if self._s and self._s.is_open:
            self._lost()
