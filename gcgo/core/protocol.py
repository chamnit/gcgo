"""GRBL streaming protocol over a byte-level Transport.

Single-threaded: file streaming is a non-blocking pump() state machine driven
by the caller's loop (a sync REPL loop or an async task), so it runs unchanged
on CPython and MicroPython. Real-time keys and blocking prompt commands run in
that same loop, so no locks are needed. Line assembly is done here on top of
the transport's raw byte I/O.
"""

import gc
import time

from gcgo.core.clock import diff_ms, now_ms
from gcgo.core.status import GRBLStatus

RX_BUFFER_SIZE = 127
GRBL_BAUD = 115200
GC_SLACK_MS = 250   # min interval between slack-point gc.collect() calls

SRC_CAP = 256       # source read buffer (must exceed the longest gcode line)
SCRATCH_CAP = 96    # transport readinto scratch
RING_CAP = 96       # max gcode lines in flight (127B RX / shortest line)

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
        # response assembly (offset-consumed; no per-line realloc)
        self._rx = bytearray()
        self._rx_pos = 0
        # preallocated streaming buffers (reused across jobs — no per-line alloc)
        self._src = bytearray(SRC_CAP)
        self._srcmv = memoryview(self._src)
        self._scratch = bytearray(SCRATCH_CAP)
        self._scratchmv = memoryview(self._scratch)
        self._ring = [0] * RING_CAP   # FIFO of in-flight line byte-lengths
        # Opt-in: collect garbage at slack points (GRBL buffer full) during a
        # stream, so GC pauses land while there's buffered motion to cover them.
        # Left off on desktop (CPython GC is incremental and cheap).
        self.gc_collect = False
        self._gc_at = 0
        self.status = GRBLStatus()
        self._reset_stream()

    def _reset_stream(self) -> None:
        self._file = None
        self._size = 0
        self._consumed = 0          # source bytes consumed (for progress)
        self._src_pos = 0           # next unread index in _src
        self._src_end = 0           # valid bytes in _src
        self._eof = False
        self._sent = 0              # lines sent
        self._acked = 0             # lines acknowledged
        self._buf_used = 0          # bytes in flight (GRBL RX budget)
        self._if_head = 0           # in-flight ring head/tail/count
        self._if_tail = 0
        self._if_count = 0
        self._sent_any = False
        self._state = IDLE
        self._poll_ms = 0
        self._poll_at = 0
        self.on_sent = None
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
        self._rx_pos = 0
        if self._io.is_open():
            self._io.reset_input()

    @property
    def connected(self) -> bool:
        return self._io.is_open()

    # --- line assembly ---

    def _next_line(self):
        """Pop one complete line from the rx buffer, or None if none buffered.

        Consumes via a read offset and compacts only occasionally, so there is
        no per-line O(n) buffer reallocation.
        """
        nl = self._rx.find(b"\n", self._rx_pos)
        if nl < 0:
            return None
        s = self._rx[self._rx_pos:nl].decode("utf-8", "replace").strip()
        self._rx_pos = nl + 1
        if self._rx_pos > 64:  # compact infrequently, not every line
            self._rx = self._rx[self._rx_pos:]
            self._rx_pos = 0
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

    # --- streaming (non-blocking pump, streamed from a file) ---

    @property
    def state(self) -> str:
        return self._state

    @property
    def sent(self) -> int:
        return self._sent

    @property
    def progress(self) -> float:
        """Fraction of the source file consumed, 0.0..1.0 (0 if size unknown)."""
        return (self._consumed / self._size) if self._size else 0.0

    @property
    def sent_any(self) -> bool:
        return self._sent_any

    def begin(self, path, on_sent=None, on_response=None, on_message=None,
              status_interval: float = 1.0) -> None:
        """Start streaming a gcode file by path. Lines are read on demand into a
        preallocated buffer and forwarded as raw bytes (GRBL handles comments,
        spaces, and case), so nothing is loaded into RAM up front.

        Callbacks are invoked (with decoded text) only when set, so the MCU hot
        path stays allocation-free by leaving them None.
        """
        self._reset_stream()
        self.on_sent = on_sent
        self.on_response = on_response
        self.on_message = on_message
        self.flush_input()
        try:
            self._size = _file_size(path)
            self._file = open(path, "rb")
        except OSError:
            self._state = ERROR
            return
        self._state = RUNNING
        self._poll_ms = int(status_interval * 1000)
        if self._poll_ms > 0:
            self._io.write(b"?")  # immediate first status
            self._poll_at = now_ms() + self._poll_ms

    def request_stop(self) -> None:
        if self._state == RUNNING:
            self._state = STOPPED
            self._close_file()

    def _close_file(self) -> None:
        if self._file is not None:
            try:
                self._file.close()
            except OSError:
                pass
            self._file = None

    def _refill_src(self) -> None:
        """Compact consumed bytes and read more of the file into _src."""
        rem = self._src_end - self._src_pos
        if self._src_pos > 0:
            self._src[0:rem] = self._src[self._src_pos:self._src_end]
            self._src_pos = 0
            self._src_end = rem
        if self._src_end >= SRC_CAP:
            return  # buffer full with no newline — line too long (caught below)
        k = self._file.readinto(self._srcmv[self._src_end:])
        if not k:
            self._eof = True
        else:
            self._src_end += k

    def _fill_grbl(self) -> None:
        """Send as many whole lines as fit in GRBL's RX buffer."""
        while True:
            nl = self._src.find(b"\n", self._src_pos, self._src_end)
            if nl < 0:
                if self._eof:
                    if self._src_end > self._src_pos:        # last line, no newline
                        if has_code(self._src, self._src_pos, self._src_end):
                            self._send_line(self._src_pos, self._src_end, False)
                        else:
                            self._consumed += self._src_end - self._src_pos
                            self._src_pos = self._src_end
                    return
                if self._src_pos == 0 and self._src_end >= SRC_CAP:
                    self._state = ERROR                      # line longer than SRC_CAP
                    return
                self._refill_src()
                if self._state == ERROR:
                    return
                continue
            # whole line [start, nl) plus its newline
            start = self._src_pos
            nbytes = (nl + 1) - start
            if not has_code(self._src, start, nl):            # blank/comment-only — skip
                self._consumed += nbytes
                self._src_pos = nl + 1
                continue
            if nbytes > RX_BUFFER_SIZE:                        # can never fit — bad file
                self._state = ERROR
                return
            if self._buf_used + nbytes > RX_BUFFER_SIZE:
                return                                        # full; resume next pump
            if not self._send_line(start, nl, True):
                return

    def _send_line(self, start: int, content_end: int, has_nl: bool) -> bool:
        """Write one line (raw bytes) to GRBL and record it in the in-flight ring.
        content_end is the index of '\\n' (has_nl) or end-of-data. Returns False
        if it didn't fit (no newline case re-checked here)."""
        if has_nl:
            nbytes = (content_end + 1) - start
        else:
            nbytes = (content_end - start) + 1  # we append the missing newline
        if self._buf_used + nbytes > RX_BUFFER_SIZE:
            return False
        if has_nl:
            self._io.write(self._srcmv[start:content_end + 1])
        else:
            self._io.write(self._srcmv[start:content_end])
            self._io.write(b"\n")
        self._ring[self._if_tail] = nbytes
        self._if_tail = (self._if_tail + 1) % RING_CAP
        self._if_count += 1
        self._buf_used += nbytes
        self._consumed += nbytes
        self._sent += 1
        self._sent_any = True
        if self.on_sent:
            text = self._src[start:content_end].decode("utf-8", "replace").strip()
            self.on_sent(self._sent, text)
        if has_nl:
            self._src_pos = content_end + 1
        else:
            self._src_pos = content_end
        return True

    def _ack(self) -> None:
        if self._if_count:
            self._buf_used -= self._ring[self._if_head]
            self._if_head = (self._if_head + 1) % RING_CAP
            self._if_count -= 1
        self._acked += 1

    def pump(self) -> str:
        """Advance the stream by a bounded, non-blocking step. Returns state."""
        if self._state != RUNNING:
            return self._state

        # 1) fill GRBL's RX buffer from the file
        self._fill_grbl()
        if self._state == ERROR:
            self._close_file()
            return self._state

        # 1b) collect garbage now if asked and GRBL's buffer is full — the
        #     planner has buffered motion to cover the pause. Throttled.
        if self.gc_collect and self._buf_used >= RX_BUFFER_SIZE - 40:
            now = now_ms()
            if diff_ms(now, self._gc_at) >= 0:
                gc.collect()
                self._gc_at = now + GC_SLACK_MS

        # 2) timed status poll
        if self._poll_ms and diff_ms(now_ms(), self._poll_at) >= 0:
            self._io.write(b"?")
            self._poll_at = now_ms() + self._poll_ms

        # 3) consume responses, accounting acks against the in-flight buffer
        self._read_and_route(True)
        if self._state == ERROR:
            return self._state

        # 4) completion: file exhausted and every sent line acknowledged
        if self._eof and self._src_end == self._src_pos and self._acked >= self._sent:
            self._state = DONE
            self._close_file()
        return self._state

    def _read_and_route(self, streaming: bool) -> None:
        """Read available response bytes and route complete lines (no decode on
        the hot ok/error path — classify by leading byte). When streaming, ok/
        error acknowledge the in-flight buffer and an error halts the stream."""
        avail = self._io.any()
        if avail:
            n = self._io.readinto(self._scratchmv[:min(avail, SCRATCH_CAP)])
            if n:
                self._rx.extend(self._scratchmv[:n])
        while True:
            nl = self._rx.find(b"\n", self._rx_pos)
            if nl < 0:
                break
            start = self._rx_pos
            end = nl
            if end > start and self._rx[end - 1] == 0x0d:
                end -= 1
            self._rx_pos = nl + 1
            if end <= start:
                continue
            c0 = self._rx[start]
            if c0 == 0x6f or c0 == 0x65:              # 'o'k / 'e'rror
                if streaming:
                    self._ack()
                if self.on_response:
                    self.on_response(self._acked,
                                     self._rx[start:end].decode("utf-8", "replace"))
                if c0 == 0x65 and streaming:           # error -> halt the stream
                    self._state = ERROR
                    self._close_file()
                    self._compact_rx()
                    return
            elif c0 == 0x3c:                           # '<' status report
                msg = self._rx[start:end].decode("utf-8", "replace")
                self.status.update(msg)
                if self.on_message:
                    self.on_message(msg)
            else:                                      # [MSG:...], ALARM:, etc.
                if self.on_message:
                    self.on_message(self._rx[start:end].decode("utf-8", "replace"))
        self._compact_rx()

    # --- async-friendly non-blocking command surface (web/uasyncio) ---
    #
    # These never block: writes go out immediately and responses are picked up
    # later by service() (when idle) or pump() (while streaming) and delivered
    # through on_response / on_message. Set those callbacks once and call
    # service()/pump() from your event loop.

    def service(self) -> None:
        """Non-blocking: read and route any pending GRBL output. Use when idle
        (pump() does this while streaming)."""
        self._read_and_route(False)

    def request_status(self) -> None:
        """Non-blocking status request ('?'); reply arrives via service()."""
        self._io.write(b"?")

    def write_line(self, line: str) -> None:
        """Non-blocking command send (e.g. MDI); ok/error arrives via service()."""
        self._send_raw(line)

    def request_reset(self) -> None:
        """Non-blocking soft-reset; greeting arrives via service()."""
        self._io.write(b"\x18")

    def request_cancel(self) -> None:
        """Non-blocking abort: feed-hold then soft-reset; output via service()."""
        self._io.write(b"!")
        self._io.write(b"\x18")

    def _compact_rx(self) -> None:
        if self._rx_pos > 64:  # compact infrequently, not every pump
            self._rx = self._rx[self._rx_pos:]
            self._rx_pos = 0


def _file_size(path) -> int:
    try:
        import os
        return os.stat(path)[6]
    except (OSError, ImportError):
        return 0


def has_code(buf, start: int, end: int) -> bool:
    """True if buf[start:end] holds executable g-code (not blank and not a
    pure comment). Scans bytes in place — no allocation. GRBL handles inline
    '(...)' / ';' comments itself, so those are left in lines that have code;
    this only filters out lines main would have dropped entirely."""
    in_paren = False
    i = start
    while i < end:
        c = buf[i]
        if c == 0x3b:          # ';' -> rest of line is a comment
            break
        elif c == 0x28:        # '('
            in_paren = True
        elif c == 0x29:        # ')'
            in_paren = False
        elif not in_paren and c != 0x20 and c != 0x09 and c != 0x0d:
            return True         # a real (non-space) code character
        i += 1
    return False
