"""Retained GRBL status, accumulated across `?` status reports."""

from __future__ import annotations


class GRBLStatus:
    """Accumulates GRBL status fields across reports.

    GRBL omits fields that haven't changed (WCO, Ov, Pn), so each update
    only touches the fields present in that report. Missing Pn means no
    pins active; all other absent fields retain their last known value.
    """

    def __init__(self):
        self.state: str = ""
        self._mpos: list[float] = [0.0, 0.0, 0.0]
        self._wco: list[float] = [0.0, 0.0, 0.0]
        self.feed: float = 0.0
        self.spindle: float = 0.0
        self.pins: str = ""
        self.feed_ov: int = 100
        self.rapid_ov: int = 100
        self.spindle_ov: int = 100

    @property
    def mpos(self) -> tuple[float, float, float]:
        return (self._mpos[0], self._mpos[1], self._mpos[2])

    @property
    def wpos(self) -> tuple[float, float, float]:
        return (
            self._mpos[0] - self._wco[0],
            self._mpos[1] - self._wco[1],
            self._mpos[2] - self._wco[2],
        )

    @property
    def wco(self) -> tuple[float, float, float]:
        return (self._wco[0], self._wco[1], self._wco[2])

    def update(self, raw: str) -> bool:
        """Parse a GRBL status string and update retained state.

        Returns False (leaving prior state intact) if the report is malformed,
        e.g. truncated by serial noise — a bad frame must never raise, since
        that would abort a running stream.
        """
        if not (raw.startswith("<") and raw.endswith(">")):
            return False
        parts = raw[1:-1].split("|")
        if not parts or not parts[0]:
            return False

        fields = {}
        for part in parts[1:]:
            key, _, val = part.partition(":")
            fields[key] = val

        try:
            # WCO first, so a WPos report can be converted with the current offset
            if "WCO" in fields:
                self._wco = [float(v) for v in fields["WCO"].split(",")]
            if "MPos" in fields:
                self._mpos = [float(v) for v in fields["MPos"].split(",")]
            elif "WPos" in fields:
                wpos = [float(v) for v in fields["WPos"].split(",")]
                self._mpos = [wpos[i] + self._wco[i] for i in range(len(wpos))]
            if "FS" in fields:
                fs = fields["FS"].split(",")
                self.feed = float(fs[0])
                self.spindle = float(fs[1]) if len(fs) > 1 else 0.0
            elif "F" in fields:
                self.feed = float(fields["F"])
            if "Ov" in fields:
                ov = fields["Ov"].split(",")
                if len(ov) >= 3:
                    self.feed_ov = int(ov[0])
                    self.rapid_ov = int(ov[1])
                    self.spindle_ov = int(ov[2])
        except (ValueError, IndexError):
            return False

        self.pins = fields.get("Pn", "")
        self.state = parts[0]
        return True
