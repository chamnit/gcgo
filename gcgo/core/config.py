"""User-facing gcgo configuration: status fields, poll rate, units, key binds.

Persisted as JSON. File I/O uses the builtin open() so it works on both CPython
and MicroPython; the caller supplies the path.
"""

import json

from gcgo.core.tables import STREAM_ACTIONS


class StatusConfig:
    """gcgo display/interaction config: status fields, poll rate, units,
    and streaming real-time key bindings.

    Choices are persisted so they stick per install/machine.
    """

    DEFAULT_RATE = 1.0       # seconds between '?' status polls; 0 disables
    DEFAULT_UNITS = "mm"     # "mm" or "inch"; gcgo owns GRBL's $13 to match
    DEFAULT_AFTER = "keep"   # after a completed run: "keep" or "clear" the file

    # ordered (key, description, default) — order is the display order
    FIELDS = (
        ("state",      "machine state",      True),
        ("wpos",       "work position",      True),
        ("mpos",       "machine position",   False),
        ("wco",        "work coord offset",  False),
        ("feed",       "feed rate",          True),
        ("spindle",    "spindle speed",      True),
        ("feed_ov",    "feed override",      True),
        ("rapid_ov",   "rapid override",     True),
        ("spindle_ov", "spindle override",   True),
        ("pins",       "limit/control pins", True),
    )

    def __init__(self):
        self.show = {key: default for key, _, default in self.FIELDS}
        self.rate = self.DEFAULT_RATE
        self.units = self.DEFAULT_UNITS
        self.after = self.DEFAULT_AFTER
        self.keys = {
            aid: {"key": dkey, "enabled": denabled}
            for aid, _desc, dkey, denabled, _m in STREAM_ACTIONS
        }

    @property
    def pos_unit(self) -> str:
        return "in" if self.units == "inch" else "mm"

    @property
    def feed_unit(self) -> str:
        return "in/min" if self.units == "inch" else "mm/min"

    @property
    def grbl_inch(self) -> str:
        """The $13 value matching this units setting."""
        return "1" if self.units == "inch" else "0"

    def load(self, path) -> None:
        try:
            with open(path) as f:
                data = json.loads(f.read())
        except (OSError, ValueError):
            return  # missing, unreadable, or invalid JSON — keep defaults
        if not isinstance(data, dict):
            return
        fields = data.get("fields", {})
        if not isinstance(fields, dict):
            fields = {}
        for key in self.show:
            if key in fields:
                self.show[key] = bool(fields[key])
        if "rate" in data:
            try:
                self.rate = max(0.0, float(data["rate"]))
            except (TypeError, ValueError):
                pass
        if data.get("units") in ("mm", "inch"):
            self.units = data["units"]
        if data.get("after") in ("keep", "clear"):
            self.after = data["after"]
        saved_keys = data.get("keys", {})
        if not isinstance(saved_keys, dict):
            saved_keys = {}
        for aid, entry in self.keys.items():
            k = saved_keys.get(aid)
            if isinstance(k, dict):
                if "key" in k:
                    entry["key"] = str(k["key"])
                if "enabled" in k:
                    entry["enabled"] = bool(k["enabled"])

    def save(self, path) -> None:
        with open(path, "w") as f:
            f.write(json.dumps(
                {
                    "fields": self.show,
                    "rate": self.rate,
                    "units": self.units,
                    "after": self.after,
                    "keys": self.keys,
                },
                indent=2,
            ))
