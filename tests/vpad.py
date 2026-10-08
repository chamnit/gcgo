"""A virtual Xbox-style gamepad through /dev/uinput (stdlib only), for tests."""
import fcntl, os, struct, time
UI_SET_EVBIT, UI_SET_KEYBIT, UI_SET_ABSBIT, UI_DEV_CREATE, UI_DEV_DESTROY = 0x40045564, 0x40045565, 0x40045567, 0x5501, 0x5502
EV_SYN, EV_KEY, EV_ABS = 0, 1, 3
BTN = {"a": 0x130, "b": 0x131, "x": 0x133, "y": 0x134, "lb": 0x136, "rb": 0x137,
       "back": 0x13a, "start": 0x13b, "mode": 0x13c, "ls": 0x13d, "rs": 0x13e}
ABS = {"lx": 0, "ly": 1, "lt": 2, "rx": 3, "ry": 4, "rt": 5, "hatx": 0x10, "haty": 0x11}
class VPad:
    def __init__(self, name=b"gcgo test pad"):
        self.fd = os.open("/dev/uinput", os.O_WRONLY | os.O_NONBLOCK)
        for ev in (EV_KEY, EV_ABS): fcntl.ioctl(self.fd, UI_SET_EVBIT, ev)
        for c in BTN.values(): fcntl.ioctl(self.fd, UI_SET_KEYBIT, c)
        for c in ABS.values(): fcntl.ioctl(self.fd, UI_SET_ABSBIT, c)
        amax, amin = [0] * 64, [0] * 64
        for n, c in ABS.items():
            amin[c], amax[c] = (-1, 1) if n.startswith("hat") else ((0, 255) if n in ("lt", "rt") else (-32768, 32767))
        dev = struct.pack("80sHHHHI", name, 3, 0x045e, 0x028e, 1, 0) + struct.pack("64i", *amax) + struct.pack("64i", *amin) + struct.pack("64i", *[0] * 64) * 2
        os.write(self.fd, dev)
        fcntl.ioctl(self.fd, UI_DEV_CREATE)
        time.sleep(0.5)
    def _ev(self, t, c, v): os.write(self.fd, struct.pack("llHHi", 0, 0, t, c, v))
    def axis(self, name, v):   # v -1..1 (hat: -1/0/1)
        self._ev(EV_ABS, ABS[name], int(v) if name.startswith("hat") else int(max(-32768, min(32767, v * 32767))))
        self._ev(EV_SYN, 0, 0); return time.time()
    def press(self, name, hold=0.05):
        self._ev(EV_KEY, BTN[name], 1); self._ev(EV_SYN, 0, 0); t = time.time(); time.sleep(hold)
        self._ev(EV_KEY, BTN[name], 0); self._ev(EV_SYN, 0, 0); return t
    def close(self): fcntl.ioctl(self.fd, UI_DEV_DESTROY); os.close(self.fd)
