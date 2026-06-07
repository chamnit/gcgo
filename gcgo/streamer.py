"""GRBL serial streamer using the buffer-fill (character-counting) protocol.

I/O goes through an injected byte-level Transport (see gcgo/ports/base.py), so
this layer carries no platform serial dependency; line assembly is done here.
"""

import threading
import time
from pathlib import Path

RX_BUFFER_SIZE = 127
GRBL_BAUD = 115200


class GRBLStreamer:
    def __init__(self, transport, timeout: float = 2.0):
        self._io = transport
        self._timeout = timeout
        self._io.set_timeout(timeout)
        self._rx = bytearray()  # buffered, not-yet-newline-terminated input
        self._stop_event = threading.Event()
        self._lock = threading.Lock()

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
            nl = self._rx.find(b"\n")
            if nl < 0:
                break
            s = self._rx[:nl].decode("utf-8", "replace").strip()
            del self._rx[:nl + 1]
            if s:
                last = s
        return last

    def disconnect(self):
        self._io.close()

    def flush_input(self) -> None:
        """Drop any pending input (transport buffer and our line buffer)."""
        self._rx = bytearray()
        if self._io.is_open():
            self._io.reset_input()

    @property
    def connected(self) -> bool:
        return self._io.is_open()

    # --- low-level I/O ---

    def _send_raw(self, line: str) -> None:
        self._io.write((line.strip() + "\n").encode())

    def _readline(self) -> str:
        """Assemble one line from the transport; "" on timeout (partial kept)."""
        while True:
            nl = self._rx.find(b"\n")
            if nl >= 0:
                s = self._rx[:nl].decode("utf-8", "replace").strip()
                del self._rx[:nl + 1]
                return s
            chunk = self._io.read(64)
            if not chunk:
                return ""  # timeout, no complete line yet
            self._rx.extend(chunk)

    # --- single command (blocking) ---

    def send_command(self, cmd: str) -> str:
        """Send one command, drain all response lines, return the terminal ok/error."""
        with self._lock:
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

    def send_command_verbose(self, cmd: str, on_line=None, read_timeout: float | None = None) -> None:
        """Send one command and call on_line for every response line received.

        read_timeout overrides the serial read timeout for this command only,
        useful for long-running commands like homing ($H).
        """
        with self._lock:
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

    # --- status ---

    def query_status(self) -> str:
        with self._lock:
            self._io.write(b"?")
            return self._readline()

    # --- real-time commands (bypass lock — safe per GRBL protocol) ---

    def _send_realtime(self, byte: bytes) -> None:
        self._io.write(byte)

    def feed_hold(self) -> None:
        self._send_realtime(b"!")

    def cycle_start(self) -> None:
        self._send_realtime(b"~")

    def feed_override_reset(self) -> None:
        self._send_realtime(b"\x90")

    def feed_override_plus10(self) -> None:
        self._send_realtime(b"\x91")

    def feed_override_minus10(self) -> None:
        self._send_realtime(b"\x92")

    def feed_override_plus1(self) -> None:
        self._send_realtime(b"\x93")

    def feed_override_minus1(self) -> None:
        self._send_realtime(b"\x94")

    def rapid_override_full(self) -> None:
        self._send_realtime(b"\x95")

    def rapid_override_half(self) -> None:
        self._send_realtime(b"\x96")

    def rapid_override_quarter(self) -> None:
        self._send_realtime(b"\x97")

    def spindle_override_reset(self) -> None:
        self._send_realtime(b"\x99")

    def spindle_override_plus10(self) -> None:
        self._send_realtime(b"\x9a")

    def spindle_override_minus10(self) -> None:
        self._send_realtime(b"\x9b")

    def spindle_override_plus1(self) -> None:
        self._send_realtime(b"\x9c")

    def spindle_override_minus1(self) -> None:
        self._send_realtime(b"\x9d")

    def spindle_stop_toggle(self) -> None:
        self._send_realtime(b"\x9e")

    def flood_toggle(self) -> None:
        self._send_realtime(b"\xa0")

    def mist_toggle(self) -> None:
        self._send_realtime(b"\xa1")

    # --- soft-reset ---

    def soft_reset(self) -> str:
        with self._lock:
            self._io.write(b"\x18")
            time.sleep(1)
            return self._read_available()

    # --- file streaming (buffer-fill protocol) ---

    def stream_file(
        self,
        path: str | Path,
        on_response=None,
        on_progress=None,
        on_message=None,
        status_interval: float = 1.0,
    ) -> None:
        """Stream a gcode file using GRBL's character-counting buffer protocol.

        on_response(line_num, response): called for each 'ok'/'error' received
        on_progress(sent, total, line):  called after each line is sent;
                                         line is the gcode text that was sent
        on_message(msg):                 called for any other line GRBL sends
                                         (status reports, [MSG:...], ALARM:, etc.)
        status_interval:                 seconds between automatic '?' status polls;
                                         0 disables polling
        """
        lines = load_gcode(path)
        total = len(lines)

        buf_counts: list[int] = []
        sent_idx = 0
        recv_idx = 0
        buf_used = 0

        self._stop_event.clear()
        self.flush_input()  # drop any stale bytes from a prior run

        poll_stop = threading.Event()
        if status_interval > 0:
            self._send_realtime(b"?")
            def _poll():
                while not poll_stop.wait(status_interval):
                    self._send_realtime(b"?")
            threading.Thread(target=_poll, daemon=True).start()

        try:
            while recv_idx < total and not self._stop_event.is_set():
                # fill the buffer
                while sent_idx < total and not self._stop_event.is_set():
                    line = lines[sent_idx] + "\n"
                    if buf_used + len(line) > RX_BUFFER_SIZE:
                        break
                    with self._lock:
                        self._io.write(line.encode())
                    buf_counts.append(len(line))
                    buf_used += len(line)
                    sent_idx += 1
                    if on_progress:
                        on_progress(sent_idx, total, line.strip())

                # read until we get the terminal ok/error for this gcode line;
                # any other lines (status reports, messages, alarms) are routed
                # to on_message and do not count against the buffer tracker.
                # The stop check lets us bail even if GRBL goes unresponsive,
                # and empty reads (serial timeouts) are skipped, not displayed.
                resp = ""
                with self._lock:
                    while not self._stop_event.is_set():
                        resp = self._readline()
                        if not resp:
                            continue
                        if resp.startswith(("ok", "error")):
                            break
                        if on_message:
                            on_message(resp)

                if not resp.startswith(("ok", "error")):
                    break  # stopped while waiting for a response

                buf_used -= buf_counts.pop(0)
                recv_idx += 1
                if on_response:
                    on_response(recv_idx, resp)
                if resp.startswith("error"):
                    break
        finally:
            poll_stop.set()

    def stop_stream(self) -> None:
        self._stop_event.set()

    def cancel(self) -> str:
        """Abort an in-progress job.

        Stopping the stream only stops *sending* — GRBL keeps running the lines
        already in its serial RX and planner buffers. To truly halt, feed-hold to
        decelerate, then soft-reset to abort and flush GRBL's buffers. soft_reset
        also drains the serial input so stale 'ok's don't corrupt the next run.
        Returns the reset greeting.
        """
        self._send_realtime(b"!")  # feed hold (decelerate)
        time.sleep(0.3)
        return self.soft_reset()   # 0x18: abort + flush + drain input


def load_gcode(path: str | Path) -> list[str]:
    """Read a gcode file and return the cleaned, non-empty lines as streamed.

    Raises ValueError if any line (plus its newline) can't fit in GRBL's RX
    buffer, which would otherwise deadlock the character-counting stream loop.
    """
    raw = Path(path).read_text().splitlines()
    lines = [_strip_comment(l) for l in raw]
    lines = [l for l in lines if l]
    for n, l in enumerate(lines, 1):
        if len(l) + 1 > RX_BUFFER_SIZE:
            raise ValueError(
                f"line {n} is {len(l) + 1} chars, exceeds the "
                f"{RX_BUFFER_SIZE}-byte GRBL buffer: {l[:40]}..."
            )
    return lines


def _strip_comment(line: str) -> str:
    """Remove GRBL inline comments and whitespace."""
    line = line.split(";")[0]
    paren = line.find("(")
    if paren != -1:
        end = line.find(")", paren)
        line = line[:paren] + (line[end + 1 :] if end != -1 else "")
    return line.strip().upper()
