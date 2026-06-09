"""MicroPython Transport backed by machine.UART (wired to the GRBL board)."""

from machine import UART


class UARTTransport:
    """Byte-level Transport over a hardware UART.

    uart_id / tx / rx are board-specific — pass what your board needs, e.g.
        UARTTransport(1, tx=4, rx=5)            # ESP32
        UARTTransport(0)                         # RP2040 default UART0 pins
    """

    def __init__(self, uart_id=1, baud=115200, timeout=2.0, **kw):
        self._timeout_ms = int(timeout * 1000)
        self._kw = kw
        self._baud = baud
        self._uart_id = uart_id
        self._uart = UART(uart_id, baudrate=baud, timeout=self._timeout_ms, **kw)

    def write(self, data):
        self._uart.write(data)

    def read(self, n=64):
        data = self._uart.read(n)
        return data if data is not None else b""

    def readinto(self, buf) -> int:
        return self._uart.readinto(buf) or 0

    def any(self):
        return self._uart.any()

    def set_timeout(self, seconds):
        self._timeout_ms = int(seconds * 1000)
        # re-init to apply the new read timeout (keeps baud + pin config)
        self._uart.init(baudrate=self._baud, timeout=self._timeout_ms, **self._kw)

    def reset_input(self):
        while self._uart.any():
            self._uart.read(self._uart.any())

    def is_open(self):
        return True

    def close(self):
        self._uart.deinit()


def hard_reset(pin_num, settle=2.0):
    """Pulse an active-low reset line (via level shifter) to reboot the GRBL
    board, then wait for it to come back up. Returns nothing."""
    import time
    from machine import Pin
    p = Pin(pin_num, Pin.OUT, value=1)
    p.value(0)
    time.sleep_ms(20)
    p.value(1)
    time.sleep(settle)
