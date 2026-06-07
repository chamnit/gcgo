"""Adapter contracts. These are documentation/duck-typed interfaces — no abc
import (MicroPython-friendly). An adapter just needs to provide these methods.

Transport — byte-level serial link to the GRBL controller. The protocol layer
does its own line assembly on top, so this stays a thin, portable primitive
backed by pyserial on desktop and machine.UART on MicroPython.
"""


class Transport:
    """Byte-level serial transport to GRBL."""

    def write(self, data: bytes) -> None:
        """Write raw bytes to the port."""
        raise NotImplementedError

    def read(self, n: int = 64) -> bytes:
        """Read up to n bytes, blocking at most the current timeout.
        Return b"" if nothing arrived within the timeout."""
        raise NotImplementedError

    def any(self) -> int:
        """Number of bytes available to read right now, without blocking."""
        raise NotImplementedError

    def set_timeout(self, seconds: float) -> None:
        """Set the read timeout used by read()."""
        raise NotImplementedError

    def reset_input(self) -> None:
        """Discard any bytes currently buffered in the input."""
        raise NotImplementedError

    def is_open(self) -> bool:
        raise NotImplementedError

    def close(self) -> None:
        raise NotImplementedError
