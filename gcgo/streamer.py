"""GRBL serial streamer using the buffer-fill (character-counting) protocol."""

import threading
import time
from pathlib import Path

import serial

RX_BUFFER_SIZE = 127
GRBL_BAUD = 115200
STATUS_POLL_INTERVAL = 0.5


class GRBLStreamer:
    def __init__(self, port: str, baud: int = GRBL_BAUD, timeout: float = 2.0):
        self.port = port
        self.baud = baud
        self.timeout = timeout
        self._serial: serial.Serial | None = None
        self._stop_event = threading.Event()
        self._lock = threading.Lock()

    # --- connection ---

    def connect(self) -> str:
        self._serial = serial.Serial(self.port, self.baud, timeout=self.timeout)
        time.sleep(2)  # GRBL resets on serial open
        self._serial.flushInput()
        greeting = self._serial.read_until(b"\n").decode(errors="replace").strip()
        return greeting

    def disconnect(self):
        if self._serial and self._serial.is_open:
            self._serial.close()

    @property
    def connected(self) -> bool:
        return bool(self._serial and self._serial.is_open)

    # --- low-level I/O ---

    def _send_raw(self, line: str) -> None:
        self._serial.write((line.strip() + "\n").encode())

    def _readline(self) -> str:
        return self._serial.readline().decode(errors="replace").strip()

    # --- single command (blocking) ---

    def send_command(self, cmd: str) -> str:
        """Send one gcode line, return the response."""
        with self._lock:
            self._send_raw(cmd)
            return self._readline()

    # --- status ---

    def query_status(self) -> str:
        with self._lock:
            self._serial.write(b"?")
            return self._readline()

    # --- soft-reset ---

    def soft_reset(self) -> None:
        with self._lock:
            self._serial.write(b"\x18")
            time.sleep(1)
            self._serial.flushInput()

    # --- file streaming (buffer-fill protocol) ---

    def stream_file(
        self,
        path: str | Path,
        on_response=None,
        on_progress=None,
    ) -> None:
        """Stream a gcode file using GRBL's character-counting buffer protocol.

        on_response(line_num, response): called for each 'ok'/'error' received
        on_progress(sent, total):        called after each line sent
        """
        lines = Path(path).read_text().splitlines()
        lines = [_strip_comment(l) for l in lines]
        lines = [l for l in lines if l]
        total = len(lines)

        buf_counts: list[int] = []
        sent_idx = 0
        recv_idx = 0
        buf_used = 0

        self._stop_event.clear()

        while recv_idx < total and not self._stop_event.is_set():
            # fill the buffer
            while sent_idx < total and not self._stop_event.is_set():
                line = lines[sent_idx] + "\n"
                if buf_used + len(line) > RX_BUFFER_SIZE:
                    break
                with self._lock:
                    self._serial.write(line.encode())
                buf_counts.append(len(line))
                buf_used += len(line)
                sent_idx += 1
                if on_progress:
                    on_progress(sent_idx, total)

            # read one response
            with self._lock:
                resp = self._readline()
            buf_used -= buf_counts.pop(0)
            recv_idx += 1
            if on_response:
                on_response(recv_idx, resp)
            if resp.startswith("error"):
                break

    def stop_stream(self) -> None:
        self._stop_event.set()


def _strip_comment(line: str) -> str:
    """Remove GRBL inline comments and whitespace."""
    line = line.split(";")[0]
    paren = line.find("(")
    if paren != -1:
        end = line.find(")", paren)
        line = line[:paren] + (line[end + 1 :] if end != -1 else "")
    return line.strip().upper()
