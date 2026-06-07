"""GRBL streaming protocol over a byte-level Transport.

Single-threaded: file streaming is a non-blocking pump() state machine driven
by the caller's loop (a sync REPL loop or an async task), so it runs unchanged
on CPython and MicroPython. Real-time keys and blocking prompt commands run in
that same loop, so no locks are needed. Line assembly is done here on top of
the transport's raw byte I/O.
"""

from __future__ import annotations

import time

from gcgo.core.clock import diff_ms, now_ms
from gcgo.core.status import GRBLStatus

RX_BUFFER_SIZE = 127
GRBL_BAUD = 115200

# stream states
IDLE = "idle"
RUNNING = "running"
DONE = "done"
ERROR = "error"
STOPPED = "stopped"


class Streamer:
    def __init__(self, transport, timeout: float = 2.0):
        self._io = transport
        self._timeout = timeout
        self._io.set_timeout(timeout)
        self._rx = bytearray()  # buffered, not-yet-newline-terminated input
        self.status = GRBLStatus()
        self._reset_stream()

    def _reset_stream(self) -> None:
        self._lines: list[str] = []
        self._total = 0
        self._sent = 0
        self._recv = 0
        self._buf_counts: list[int] = []
        self._buf_used = 0
        self._sent_any = False
        self._state = IDLE
        self._poll_ms = 0
        self._poll_at = 0
        self.on_progress = None
        self.on_response = None
        self.on_message = None

    # --- connection ---

    def connect(self) -> str:
        time.sleep(2)  # GRBL resets on USB-serial open; let it boot
        return self._read_available()

    def _read_available(self) -> str:
        """Drain all currently-available input, return the last complete line."""
        while self._io.any():
            self._rx.extend(self._io.read(self._io.any()))
        last = ""
        while True:
            line = self._next_line()
            if line is None:
                break
            if line:
                last = line
        return last

    def disconnect(self) -> None:
        self._io.close()

    def flush_input(self) -> None:
        """Drop any pending input (transport buffer and our line buffer)."""
        self._rx = bytearray()
        if self._io.is_open():
            self._io.reset_input()

    @property
    def connected(self) -> bool:
        return self._io.is_open()

    # --- line assembly ---

    def _next_line(self):
        """Pop one complete line from the rx buffer, or None if none buffered."""
        nl = self._rx.find(b"\n")
        if nl < 0:
            return None
        s = self._rx[:nl].decode("utf-8", "replace").strip()
        del self._rx[:nl + 1]
        return s

    def _readline(self) -> str:
        """Blocking line read; "" on timeout (partial kept for next call)."""
        while True:
            line = self._next_line()
            if line is not None:
                return line
            chunk = self._io.read(64)
            if not chunk:
                return ""
            self._rx.extend(chunk)

    def _send_raw(self, line: str) -> None:
        self._io.write((line.strip() + "\n").encode())

    # --- blocking commands (prompt phase) ---

    def send_command(self, cmd: str) -> str:
        """Send one command, drain all response lines, return terminal ok/error."""
        self._send_raw(cmd)
        last = ""
        while True:
            line = self._readline()
            if not line:
                break
            last = line
            if line.startswith(("ok", "error")):
                break
        return last

    def send_command_verbose(self, cmd: str, on_line=None, read_timeout=None) -> None:
        """Send one command, calling on_line for every response line received.

        read_timeout overrides the read timeout for this command only (e.g. $H).
        """
        if read_timeout is not None:
            self._io.set_timeout(read_timeout)
        try:
            self._send_raw(cmd)
            while True:
                line = self._readline()
                if not line:
                    break
                if on_line:
                    on_line(line)
                if line.startswith(("ok", "error")):
                    break
        finally:
            if read_timeout is not None:
                self._io.set_timeout(self._timeout)

    def query_status(self) -> str:
        self._io.write(b"?")
        return self._readline()

    # --- real-time commands (single bytes; no response expected) ---

    def _send_realtime(self, byte: bytes) -> None:
        self._io.write(byte)

    def feed_hold(self):                 self._send_realtime(b"!")
    def cycle_start(self):               self._send_realtime(b"~")
    def feed_override_reset(self):       self._send_realtime(b"\x90")
    def feed_override_plus10(self):      self._send_realtime(b"\x91")
    def feed_override_minus10(self):     self._send_realtime(b"\x92")
    def feed_override_plus1(self):       self._send_realtime(b"\x93")
    def feed_override_minus1(self):      self._send_realtime(b"\x94")
    def rapid_override_full(self):       self._send_realtime(b"\x95")
    def rapid_override_half(self):       self._send_realtime(b"\x96")
    def rapid_override_quarter(self):    self._send_realtime(b"\x97")
    def spindle_override_reset(self):    self._send_realtime(b"\x99")
    def spindle_override_plus10(self):   self._send_realtime(b"\x9a")
    def spindle_override_minus10(self):  self._send_realtime(b"\x9b")
    def spindle_override_plus1(self):    self._send_realtime(b"\x9c")
    def spindle_override_minus1(self):   self._send_realtime(b"\x9d")
    def spindle_stop_toggle(self):       self._send_realtime(b"\x9e")
    def flood_toggle(self):              self._send_realtime(b"\xa0")
    def mist_toggle(self):               self._send_realtime(b"\xa1")

    def soft_reset(self) -> str:
        self._io.write(b"\x18")
        time.sleep(1)
        return self._read_available()

    def cancel(self) -> str:
        """Abort an in-progress job: feed-hold to decelerate, then soft-reset to
        halt and flush GRBL's planner/serial buffers. Returns the reset greeting."""
        self._send_realtime(b"!")
        time.sleep(0.3)
        return self.soft_reset()

    # --- streaming (non-blocking pump) ---

    @property
    def state(self) -> str:
        return self._state

    @property
    def sent(self) -> int:
        return self._sent

    @property
    def total(self) -> int:
        return self._total

    @property
    def sent_any(self) -> bool:
        return self._sent_any

    def begin(self, lines, on_progress=None, on_response=None, on_message=None,
              status_interval: float = 1.0) -> None:
        """Start streaming the given list of cleaned gcode lines."""
        self._reset_stream()
        self._lines = lines
        self._total = len(lines)
        self.on_progress = on_progress
        self.on_response = on_response
        self.on_message = on_message
        self.flush_input()
        if self._total == 0:
            self._state = DONE
            return
        self._state = RUNNING
        self._poll_ms = int(status_interval * 1000)
        if self._poll_ms > 0:
            self._io.write(b"?")  # immediate first status
            self._poll_at = now_ms() + self._poll_ms

    def request_stop(self) -> None:
        if self._state == RUNNING:
            self._state = STOPPED

    def pump(self) -> str:
        """Advance the stream by a bounded, non-blocking step. Returns state."""
        if self._state != RUNNING:
            return self._state

        # 1) fill GRBL's RX buffer as far as it fits
        while self._sent < self._total:
            line = self._lines[self._sent] + "\n"
            n = len(line)
            if self._buf_used + n > RX_BUFFER_SIZE:
                break
            self._io.write(line.encode())
            self._buf_counts.append(n)
            self._buf_used += n
            self._sent += 1
            self._sent_any = True
            if self.on_progress:
                self.on_progress(self._sent, self._total, line.strip())

        # 2) timed status poll
        if self._poll_ms and diff_ms(now_ms(), self._poll_at) >= 0:
            self._io.write(b"?")
            self._poll_at = now_ms() + self._poll_ms

        # 3) consume all complete response lines available right now
        avail = self._io.any()
        if avail:
            self._rx.extend(self._io.read(avail))
        while True:
            line = self._next_line()
            if line is None:
                break
            if not line:
                continue
            if line.startswith(("ok", "error")):
                if self._buf_counts:
                    self._buf_used -= self._buf_counts.pop(0)
                self._recv += 1
                if self.on_response:
                    self.on_response(self._recv, line)
                if line.startswith("error"):
                    self._state = ERROR
                    return self._state
                if self._recv >= self._total:
                    self._state = DONE
                    return self._state
            else:
                if line.startswith("<"):
                    self.status.update(line)
                if self.on_message:
                    self.on_message(line)

        return self._state
