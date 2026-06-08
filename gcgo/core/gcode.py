"""G-code validation (portable; streams the file, never loads it into RAM)."""

from gcgo.core.protocol import RX_BUFFER_SIZE


def validate_gcode(path) -> int:
    """Stream the file once to count non-blank lines and reject any line that
    can't fit GRBL's RX buffer (which would deadlock the stream). Returns the
    non-blank line count. Used at load time for feedback; streaming itself
    reads the file again on demand without buffering it.
    """
    n = 0
    with open(path, "rb") as f:
        while True:
            line = f.readline()
            if not line:
                break
            content = line.rstrip(b"\r\n")
            if not content:
                continue  # blank line — streamer skips it
            if len(content) + 1 > RX_BUFFER_SIZE:
                raise ValueError(
                    "a line is %d chars, exceeds the %d-byte GRBL buffer"
                    % (len(content) + 1, RX_BUFFER_SIZE)
                )
            n += 1
    return n
