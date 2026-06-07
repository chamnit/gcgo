"""Terminal input: readline-based command entry with history + tab completion,
and raw-mode single-key polling for real-time control during streaming."""

from __future__ import annotations

import atexit
import glob
import os
import readline
import select
import sys
import termios
import tty

from gcgo.desktop.paths import HISTORY_FILE

# REPL command names (for tab-completion of the first word)
COMMANDS = (
    "load", "run", "mdi", "settings", "params", "unlock", "home", "check",
    "config", "reset", "status", "ports", "ls", "cd", "help",
    "quit", "exit",
)

# commands whose argument is a filesystem path (for path completion)
_PATH_COMMANDS = ("load", "cd", "ls")


def _path_matches(text: str) -> list[str]:
    """Filesystem completions for the given partial path."""
    expanded = os.path.expanduser(text)
    out = []
    for p in glob.glob(expanded + "*"):
        # restore a leading ~ the user typed, since glob expands it away
        if text.startswith("~"):
            p = "~" + p[len(os.path.expanduser("~")):]
        out.append(p + "/" if os.path.isdir(os.path.expanduser(p)) else p)
    return sorted(out)


def _completer(text: str, state: int):
    line = readline.get_line_buffer().lstrip()
    if " " not in line:
        matches = [c + " " for c in COMMANDS if c.startswith(text.lower())]
    else:
        cmd = line.split(None, 1)[0].lower()
        matches = _path_matches(text) if cmd in _PATH_COMMANDS else []
    return matches[state] if state < len(matches) else None


def install() -> None:
    """Load history and enable tab completion. Call once at startup."""
    readline.set_history_length(500)
    try:
        readline.read_history_file(HISTORY_FILE)
    except OSError:
        pass  # missing, unreadable, or malformed history — start fresh
    atexit.register(readline.write_history_file, HISTORY_FILE)

    # treat only whitespace as word breaks so '/', '.', '-' stay part of paths
    readline.set_completer_delims(" \t\n")
    readline.set_completer(_completer)
    if readline.__doc__ and "libedit" in readline.__doc__:
        readline.parse_and_bind("bind ^I rl_complete")  # macOS libedit
    else:
        readline.parse_and_bind("tab: complete")


class completion_suspended:
    """Context manager that disables tab completion (used in MDI mode)."""

    def __enter__(self):
        self._saved = readline.get_completer()
        readline.set_completer(None)
        return self

    def __exit__(self, *exc):
        readline.set_completer(self._saved)


class stream_keys:
    """Context manager for raw-mode single-key polling during streaming.

    poll_key() returns one character if available, else None. A no-op (poll_key
    always None) when stdin is not a TTY, so headless/piped runs still stream.
    """

    def __enter__(self):
        self.interactive = sys.stdin.isatty()
        if self.interactive:
            self._fd = sys.stdin.fileno()
            self._old = termios.tcgetattr(self._fd)
            tty.setcbreak(self._fd)
        return self

    def __exit__(self, *exc):
        if self.interactive:
            termios.tcsetattr(self._fd, termios.TCSADRAIN, self._old)

    def poll_key(self):
        if self.interactive and select.select([sys.stdin], [], [], 0)[0]:
            return sys.stdin.read(1)
        return None
