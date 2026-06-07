"""Platform-agnostic gcgo core: GRBL protocol, streaming, status, and config.

Modules here must not import any platform-specific libraries (pyserial,
readline, termios, threading, pathlib, etc.) so they run unchanged on both
CPython and MicroPython. Platform concerns live in the desktop/ and
micropython/ adapters and the frontends/.
"""
