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

    def readinto(self, buf) -> int:
        """Read up to len(buf) bytes into the preallocated buffer/memoryview,
        returning the count (0 if none). Used by the low-alloc stream pump."""
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


class Display:
    """A small pixel display for the local-UI front-end (e.g. an ILI9341 TFT).

    Colors are (r, g, b) tuples 0-255; the adapter converts to the panel's
    native format. Primitives are kept minimal so both a real driver and the
    desktop PNG preview can implement them. Origin is top-left.
    """

    width = 0
    height = 0

    def fill(self, rgb) -> None:
        """Clear the whole screen to one color."""
        raise NotImplementedError

    def rect(self, x, y, w, h, rgb) -> None:
        """Draw a filled rectangle."""
        raise NotImplementedError

    def text(self, x, y, s, rgb, scale: int = 1) -> None:
        """Draw a line of text at (x, y). Monospace; each glyph cell is
        8*scale wide and 8*scale tall."""
        raise NotImplementedError

    def show(self) -> None:
        """Flush the back buffer to the panel (no-op for direct-draw drivers)."""
        raise NotImplementedError


class Input:
    """A source of high-level UI events for the local-UI front-end.

    poll() returns a (possibly empty) list of event strings drained since the
    last call. This abstraction is what lets a rotary-encoder+buttons adapter
    and a future touchscreen adapter feed the same UI:

        "cw" / "ccw"             rotary turn (or +/- region)
        "click"                  encoder push
        "x" "y" "z"              the axis buttons (short press)
        "x_hold" "y_hold" "z_hold"  axis button long-press (zero that axis)
        "menu"                   the menu button
    """

    def poll(self) -> list:
        raise NotImplementedError
