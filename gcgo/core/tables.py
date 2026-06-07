"""Static GRBL reference data and the streaming key-action registry.

Pure data — no platform dependencies.
"""

from __future__ import annotations

# Streaming real-time key actions, in display order.
# (id, description, default_key, default_enabled, streamer_method)
# method is None for the special "stop streaming" action.
STREAM_ACTIONS = (
    ("hold",          "feed hold",             "!", True,  "feed_hold"),
    ("resume",        "cycle start / resume",  "~", True,  "cycle_start"),
    ("stop",          "stop streaming",        "q", True,  None),
    ("feed_reset",    "feed override 100%",    "0", True,  "feed_override_reset"),
    ("feed_up",       "feed override +10%",    "+", True,  "feed_override_plus10"),
    ("feed_down",     "feed override -10%",    "-", True,  "feed_override_minus10"),
    ("feed_up1",      "feed override +1%",     "",  False, "feed_override_plus1"),
    ("feed_down1",    "feed override -1%",     "",  False, "feed_override_minus1"),
    ("rapid_100",     "rapid override 100%",   "",  False, "rapid_override_full"),
    ("rapid_50",      "rapid override 50%",    "",  False, "rapid_override_half"),
    ("rapid_25",      "rapid override 25%",    "",  False, "rapid_override_quarter"),
    ("spindle_reset", "spindle override 100%", "",  False, "spindle_override_reset"),
    ("spindle_up",    "spindle override +10%", "",  False, "spindle_override_plus10"),
    ("spindle_down",  "spindle override -10%", "",  False, "spindle_override_minus10"),
    ("spindle_up1",   "spindle override +1%",  "",  False, "spindle_override_plus1"),
    ("spindle_down1", "spindle override -1%",  "",  False, "spindle_override_minus1"),
    ("spindle_stop",  "toggle spindle stop",   "",  False, "spindle_stop_toggle"),
    ("flood",         "toggle flood coolant",  "",  False, "flood_toggle"),
    ("mist",          "toggle mist coolant",   "",  False, "mist_toggle"),
)
ACTION_METHOD = {aid: method for aid, _d, _k, _e, method in STREAM_ACTIONS}
ACTION_DESC = {aid: desc for aid, desc, _k, _e, _m in STREAM_ACTIONS}

# GRBL 1.1 error code → human-readable description
GRBL_ERRORS: dict[int, str] = {
    1:  "Expected command letter",
    2:  "Bad number format",
    3:  "Invalid statement",
    4:  "Negative value not allowed",
    5:  "Setting disabled",
    6:  "Step pulse time must be > 3 µs",
    7:  "EEPROM read failed — using defaults",
    8:  "Command only valid when idle",
    9:  "G-code locked out during alarm or jog state",
    10: "Soft limits require homing to be enabled",
    11: "Line too long — truncated",
    12: "Step rate would exceed 30 kHz",
    13: "Safety door opened",
    14: "Build info or startup line too long for EEPROM",
    15: "Jog target exceeds machine travel",
    16: "Invalid jog command",
    17: "Laser mode requires PWM output",
    20: "Unsupported g-code command",
    21: "Modal group violation — conflicting g-code commands",
    22: "Feed rate undefined",
    23: "G-code command requires an integer value",
    24: "Two commands both require XYZ axis words",
    25: "G-code word repeated in block",
    26: "Axis words required but not found",
    27: "Line number out of range (1–9,999,999)",
    28: "Missing required P or L value",
    29: "Axis words present but unused by any command",
    30: "No axis words found for command that requires them",
    31: "Value of zero not allowed",
    32: "Arc motion requires a specific active plane",
    33: "Arc radius tolerance exceeded — not a valid arc",
    34: "Missing required value word for command",
    35: "G53 requires G0 or G1 motion mode",
    36: "Unused axis words with G80 active",
    37: "Missing offset word for G2/G3 arc",
    38: "Motion command targets unconfigured axis",
    39: "Invalid G2/G3 target or undefined radius",
}

# GRBL 1.1 setting index → (description, unit); unit "bool" → enabled/disabled
GRBL_SETTINGS: dict[int, tuple[str, str]] = {
    0:   ("Step pulse time",            "µs"),
    1:   ("Step idle delay",            "ms"),
    2:   ("Step pulse invert",          "mask"),
    3:   ("Step direction invert",      "mask"),
    4:   ("Invert step enable pin",     "bool"),
    5:   ("Invert limit pins",          "bool"),
    6:   ("Invert probe pin",           "bool"),
    10:  ("Status report options",      "mask"),
    11:  ("Junction deviation",         "mm"),
    12:  ("Arc tolerance",              "mm"),
    13:  ("Report in inches",           "bool"),
    20:  ("Soft limits",                "bool"),
    21:  ("Hard limits",                "bool"),
    22:  ("Homing cycle",               "bool"),
    23:  ("Homing direction invert",    "mask"),
    24:  ("Homing locate feed rate",    "mm/min"),
    25:  ("Homing search seek rate",    "mm/min"),
    26:  ("Homing switch debounce",     "ms"),
    27:  ("Homing switch pull-off",     "mm"),
    30:  ("Max spindle speed",          "RPM"),
    31:  ("Min spindle speed",          "RPM"),
    32:  ("Laser mode",                 "bool"),
    100: ("X-axis steps/mm",            "steps/mm"),
    101: ("Y-axis steps/mm",            "steps/mm"),
    102: ("Z-axis steps/mm",            "steps/mm"),
    110: ("X-axis max rate",            "mm/min"),
    111: ("Y-axis max rate",            "mm/min"),
    112: ("Z-axis max rate",            "mm/min"),
    120: ("X-axis acceleration",        "mm/sec²"),
    121: ("Y-axis acceleration",        "mm/sec²"),
    122: ("Z-axis acceleration",        "mm/sec²"),
    130: ("X-axis max travel",          "mm"),
    131: ("Y-axis max travel",          "mm"),
    132: ("Z-axis max travel",          "mm"),
}

# Coordinate-parameter key ($#) → human-readable name
PARAM_NAMES: dict[str, str] = {
    "G54": "Work offset 1",
    "G55": "Work offset 2",
    "G56": "Work offset 3",
    "G57": "Work offset 4",
    "G58": "Work offset 5",
    "G59": "Work offset 6",
    "G28": "Stored home 1",
    "G30": "Stored home 2",
    "G92": "Coordinate offset",
    "TLO": "Tool length offset",
    "PRB": "Probe position",
}
