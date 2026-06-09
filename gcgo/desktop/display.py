"""Terminal display primitives: a persistent bottom status line maintained with
carriage returns, with scrolling log lines printed above it."""

from __future__ import annotations

import shutil
import sys


def term_width() -> int:
    return shutil.get_terminal_size().columns


def print_above(text: str, status: str) -> None:
    """Print a scrolling line above the persistent status line."""
    w = term_width()
    sys.stdout.write(f"\r{' ' * w}\r{text}\n{status}")
    sys.stdout.flush()


def redraw_status(status: str) -> None:
    sys.stdout.write(f"\r{status}")
    sys.stdout.flush()
