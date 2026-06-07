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

## Requirements

- Python 3.11+
- `pyserial`
- A GRBL 1.1 controller (e.g. an Arduino running GRBL)
