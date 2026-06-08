# gcgo

An interactive GRBL g-code streamer for the terminal. Streams using GRBL's
character-counting (buffer-fill) protocol to keep the planner fed on fast jobs,
with a live status line, real-time overrides, an MDI mode, and configurable
display — all in a single small package with one dependency (`pyserial`).

## Install

The recommended way is [pipx](https://pipx.pypa.io) (or `uv tool`), which
installs into an isolated environment and puts a `gcgo` command on your PATH —
this also avoids the "externally-managed-environment" error on Raspberry Pi OS
and other recent distros:

```
pipx install git+https://github.com/chamnit/gcgo
# or, once published to PyPI:
pipx install gcgo
```

From source (development):

```
git clone https://github.com/chamnit/gcgo
cd gcgo
python -m gcgo /dev/ttyACM0      # or: pip install -e .
```

**Linux serial permissions:** you must be in the `dialout` group to open the
port, otherwise you'll get a permission error:

```
sudo usermod -aG dialout $USER   # then log out and back in
```

## Usage

```
gcgo                 # auto-detect the port (prompts if there's more than one)
gcgo /dev/ttyACM0    # specify the port
gcgo -b 115200 ...   # override the baud rate (default 115200)
```

At the `gcgo>` prompt:

```
load <file>   Load a g-code file
run           Stream the loaded file
mdi           Enter MDI mode (send g-code/$-commands directly to GRBL)
settings      Show GRBL $$ settings in readable form
params        Show GRBL $# coordinate parameters
unlock        Unlock GRBL alarm state ($X)
home          Run homing cycle ($H)
check         Toggle check mode ($C)
config        Configure status fields, poll rate, units, and stream keys
reset         Soft-reset GRBL (Ctrl-X); restores overrides to 100%
status        Query GRBL status (?)
ports         List available serial ports
ls / cd       List files / change directory
help          Show the command list
quit / exit   Exit
```

Tab completes commands and file paths. Command history persists between runs.

### Streaming

`load` a file, then `run`. While streaming, a live status line at the bottom
shows state, position, feed, spindle, overrides, and pins, while sent g-code
and GRBL messages scroll above. Single keypresses send real-time commands —
by default:

```
!  feed hold      ~  cycle start / resume
+  feed +10%      -  feed -10%      0  feed reset 100%      q  stop
```

`q` (or Ctrl-C) performs a true stop: it feed-holds, then soft-resets to halt
motion and flush GRBL's buffers — stopping only the stream would leave GRBL
running its already-buffered moves.

### Configuration

`config` manages everything, persisted to `~/.config/gcgo/config.json`
(`~/Library/Application Support/gcgo/` on macOS):

```
config                      Show all settings
config fields <name> on|off Toggle a status-line field
config rate <seconds>       Status query interval (0 disables)
config units <mm|inch>      Display units; gcgo sets GRBL's $13 to match
config after <keep|clear>   Keep or unload the file after a completed run
config keys <action> <char> Bind a streaming real-time key (or 'off' to disable)
```

Units note: gcgo owns GRBL's `$13` (report units) so displayed values always
match their labels — it sets `$13` on connect to match `config units` and
re-asserts it if you change `$13` in MDI.

## Architecture

gcgo is split so the GRBL "brain" is shared across platforms:

- `gcgo/core/` — platform-agnostic logic (protocol/streaming pump, status,
  config, gcode, clock). No platform imports; runs on CPython and MicroPython.
- `gcgo/ports/` — adapter contracts (e.g. the byte-level `Transport`).
- `gcgo/desktop/` — CPython adapters: pyserial transport, terminal display,
  termios/readline keyboard, paths.
- `gcgo/frontends/` — front-ends that drive the core (terminal today).
- `gcgo/micropython/` — `machine.UART` transport + a minimal serial-console
  front-end.

Streaming is a single-threaded non-blocking `pump()`, so the same core drops
into a desktop loop or an MCU loop unchanged.

## MicroPython (experimental)

gcgo's core runs on MicroPython 1.2x. A board can act as a standalone GRBL
sender: it talks to the GRBL controller over a UART and gives you a serial
console.

Deploy and run (using [mpremote](https://docs.micropython.org/en/latest/reference/mpremote.html)):

```
mpremote connect /dev/ttyACM0 cp -r gcgo :        # copy the package to the board
mpremote connect /dev/ttyACM0 exec "from gcgo.micropython.main import start; start(uart_id=1, baud=115200)"
```

`uart_id`/pins are board-specific — pass what your board needs (e.g.
`start(1, tx=4, rx=5)` on many ESP32s). Wire the board's UART TX/RX/GND to the
GRBL controller's serial pins.

G-code is streamed from a file on demand (nothing is loaded into RAM), so file
size isn't limited by memory — put jobs on an SD card or flash.

### Headless web pendant (Pico W)

A WiFi board can serve a browser UI (live status, file run/stop, feed
overrides, MDI) with no display or keyboard — the whole stack is a dependency-
free `uasyncio` HTTP + WebSocket server sharing the same core. Verified on a
Raspberry Pi Pico W with GRBL on UART0 (GP16/17) and a hardware reset line on
GP18, through level shifters:

```
mpremote connect /dev/ttyACM0 cp -r gcgo :
# put WIFI_SSID / WIFI_PASS in a secrets.py on the board, then:
mpremote connect /dev/ttyACM0 exec "from gcgo.micropython.webmain import start; start()"
```

It prints the board's IP; open `http://<board-ip>/`. The desktop build serves
the identical UI via `python -m gcgo.desktop.webmain <port>`.

**Wiring note:** on an ATmega Arduino, the USB port and pins 0/1 are the same
UART — don't drive the board from USB and the Pico's UART at once (bus
contention). Use one at a time.

## Requirements

- Python 3.11+ and `pyserial` (desktop), or MicroPython 1.2x (board)
- A GRBL 1.1 controller (e.g. an Arduino running GRBL)

## License

MIT — see [LICENSE](LICENSE).
