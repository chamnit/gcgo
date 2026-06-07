"""G-code loading and validation (portable; uses builtin open())."""

from __future__ import annotations

from gcgo.core.protocol import RX_BUFFER_SIZE


def load_gcode(path) -> list[str]:
    """Read a gcode file and return the cleaned, non-empty lines as streamed.

    Raises ValueError if any line (plus its newline) can't fit in GRBL's RX
    buffer, which would otherwise deadlock the character-counting stream loop.
    """
    with open(path) as f:
        raw = f.read().splitlines()
    lines = [_strip_comment(l) for l in raw]
    lines = [l for l in lines if l]
    for n, l in enumerate(lines, 1):
        if len(l) + 1 > RX_BUFFER_SIZE:
            raise ValueError(
                "line %d is %d chars, exceeds the %d-byte GRBL buffer: %s..."
                % (n, len(l) + 1, RX_BUFFER_SIZE, l[:40])
            )
    return lines


def _strip_comment(line: str) -> str:
    """Remove GRBL inline comments and whitespace."""
    line = line.split(";")[0]
    paren = line.find("(")
    if paren != -1:
        end = line.find(")", paren)
        line = line[:paren] + (line[end + 1:] if end != -1 else "")
    return line.strip().upper()
