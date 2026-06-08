"""Dependency-free async web front-end for gcgo.

One driver task owns the Streamer (pump while streaming, service when idle) and
periodically pushes status JSON to all connected WebSocket clients; clients send
command JSON back. Works on CPython (asyncio) and MicroPython (uasyncio).
"""

import json

try:
    import asyncio
except ImportError:                      # MicroPython <1.20
    import uasyncio as asyncio

from gcgo.core.clock import diff_ms, now_ms
from gcgo.core.gcode import validate_gcode
from gcgo.core.protocol import RUNNING
from gcgo.core.tables import ACTION_METHOD
from gcgo.frontends.web import ws as wsproto

_CT = {"html": "text/html", "js": "application/javascript", "css": "text/css"}
_GCODE_EXT = (".gcode", ".nc", ".g", ".gc", ".ngc")


class WebServer:
    def __init__(self, streamer, cfg, gdir, static_dir, config_file=None):
        self.s = streamer
        self.cfg = cfg
        self.gdir = gdir
        self.static = static_dir
        self.config_file = config_file
        self.clients = []      # connected WS writers
        self.pending = []      # JSON objects queued for broadcast
        self.loaded = None
        self.s.on_response = self._on_response
        self.s.on_message = self._on_message
        self.s.gc_collect = True
        self._apply_units()

    def _on_response(self, i, r):
        # while streaming, only surface errors (avoid one msg per 'ok'); idle,
        # surface every response so MDI replies show in the log.
        if self.s.state != RUNNING or r.startswith("error"):
            self.broadcast({"type": "msg", "line": r})

    def _on_message(self, m):
        if not m.startswith("<"):   # don't spam status reports into the log
            self.broadcast({"type": "msg", "line": m})

    # --- helpers ---

    def _apply_units(self):
        self.s.write_line("$13=" + self.cfg.grbl_inch)

    def broadcast(self, obj):
        self.pending.append(obj)

    def status_obj(self):
        st = self.s.status
        return {
            "type": "status", "state": st.state,
            "wpos": list(st.wpos), "mpos": list(st.mpos),
            "feed": st.feed, "spindle": st.spindle,
            "ov": [st.feed_ov, st.rapid_ov, st.spindle_ov],
            "pins": st.pins, "units": self.cfg.pos_unit,
            "stream": {"state": self.s.state, "sent": self.s.sent,
                       "progress": self.s.progress},
            "loaded": self.loaded,
        }

    def file_list(self):
        out = []
        try:
            import os
            for n in os.listdir(self.gdir):
                if n.lower().endswith(_GCODE_EXT):
                    out.append(n)
        except OSError:
            pass
        out.sort()
        return out

    # --- command handling (from the browser) ---

    def apply(self, cmd):
        c = cmd.get("cmd")
        if c == "rt":
            a = cmd.get("action")
            if a == "stop":
                self.s.request_stop()
            else:
                m = ACTION_METHOD.get(a)
                if m:
                    getattr(self.s, m)()
        elif c == "reset":
            self.s.request_reset()
        elif c == "mdi":
            if self.s.state != RUNNING:
                self.s.write_line(cmd.get("line", ""))
        elif c == "load":
            self.loaded = cmd.get("file")
            self.broadcast({"type": "msg", "line": "loaded " + str(self.loaded)})
        elif c == "run":
            self._start_run(cmd.get("file") or self.loaded)
        elif c == "stop":
            self.s.request_stop()
        elif c == "units":
            v = cmd.get("value")
            if v in ("mm", "inch"):
                self.cfg.units = v
                if self.config_file:
                    try:
                        self.cfg.save(self.config_file)
                    except OSError:
                        pass
                self._apply_units()
        elif c == "files":
            self.broadcast({"type": "files", "files": self.file_list()})

    def _start_run(self, f):
        if self.s.state == RUNNING:
            return
        if not f:
            self.broadcast({"type": "msg", "line": "no file loaded"})
            return
        path = self.gdir + "/" + f
        try:
            n = validate_gcode(path)
        except (OSError, ValueError) as e:
            self.broadcast({"type": "msg", "line": "error: " + str(e)})
            return
        self.loaded = f
        self.s.begin(path, on_response=self._on_response, on_message=self._on_message,
                     status_interval=self.cfg.rate)
        self.broadcast({"type": "msg", "line": "streaming %s (%d lines)" % (f, n)})

    # --- the single driver task ---

    async def driver(self):
        poll_ms = int((self.cfg.rate or 1.0) * 1000)
        poll_at = 0
        bcast_at = 0
        while True:
            if self.s.state == RUNNING:
                if self.s.pump() != RUNNING:
                    st = self.s.state
                    if st != "done" and self.s.sent_any:
                        self.s.request_cancel()
                    self.broadcast({"type": "msg",
                                    "line": "stream %s (%d lines)" % (st, self.s.sent)})
            else:
                self.s.service()
                if diff_ms(now_ms(), poll_at) >= 0:
                    self.s.request_status()
                    poll_at = now_ms() + poll_ms
            if diff_ms(now_ms(), bcast_at) >= 0:
                self.broadcast(self.status_obj())
                bcast_at = now_ms() + 200
            if self.pending and self.clients:
                await self._flush()
            else:
                self.pending = []
            await asyncio.sleep(0.005)

    async def _flush(self):
        frames = [wsproto.encode_text(json.dumps(o)) for o in self.pending]
        self.pending = []
        dead = []
        # snapshot: a client handler may add/remove itself across the awaits below
        for w in list(self.clients):
            try:
                for fr in frames:
                    w.write(fr)
                await w.drain()
            except Exception:
                dead.append(w)
        for w in dead:
            if w in self.clients:
                self.clients.remove(w)

    # --- HTTP / WebSocket connection handling ---

    async def handle(self, reader, writer):
        try:
            headers = await self._read_headers(reader)
            if not headers:
                return
            method, path, _ = (headers[0] + "  ").split(" ", 2)
            hdrs = {}
            for line in headers[1:]:
                k, _, v = line.partition(":")
                hdrs[k.strip().lower()] = v.strip()
            if hdrs.get("upgrade", "").lower() == "websocket":
                await self._serve_ws(reader, writer, hdrs)
            else:
                await self._serve_http(writer, path)
        except (EOFError, OSError):
            pass
        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except (OSError, AttributeError):
                pass

    async def _read_headers(self, reader):
        lines = []
        while True:
            raw = await reader.readline()
            if not raw:
                break
            line = raw.decode("utf-8", "replace").rstrip("\r\n")
            if not line:
                break
            lines.append(line)
        return lines

    async def _serve_http(self, writer, path):
        if path == "/" or not path:
            path = "/index.html"
        name = path.lstrip("/").split("?", 1)[0]
        try:
            with open(self.static + "/" + name, "rb") as f:
                body = f.read()
        except OSError:
            writer.write(b"HTTP/1.1 404 Not Found\r\nContent-Length: 0\r\n"
                         b"Connection: close\r\n\r\n")
            await writer.drain()
            return
        ct = _CT.get(name.rsplit(".", 1)[-1], "application/octet-stream")
        head = ("HTTP/1.1 200 OK\r\nContent-Type: %s\r\nContent-Length: %d\r\n"
                "Connection: close\r\n\r\n" % (ct, len(body)))
        writer.write(head.encode() + body)
        await writer.drain()

    async def _serve_ws(self, reader, writer, hdrs):
        key = hdrs.get("sec-websocket-key", "").encode()
        resp = ("HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\n"
                "Connection: Upgrade\r\nSec-WebSocket-Accept: %s\r\n\r\n"
                % wsproto.accept_key(key).decode())
        writer.write(resp.encode())
        await writer.drain()
        self.clients.append(writer)
        # prime the new client
        for obj in ({"type": "files", "files": self.file_list()}, self.status_obj()):
            writer.write(wsproto.encode_text(json.dumps(obj)))
        await writer.drain()
        try:
            while True:
                kind, data = await wsproto.read_message(reader)
                if kind == "text":
                    try:
                        self.apply(json.loads(data))
                    except ValueError:
                        pass
                elif kind == "ping":
                    writer.write(wsproto.encode_pong(data))
                    await writer.drain()
                elif kind == "close":
                    break
        except (EOFError, OSError):
            pass
        finally:
            if writer in self.clients:
                self.clients.remove(writer)


def _default_static():
    return __file__.rsplit("/", 1)[0] + "/static"


async def serve(streamer, cfg, gdir, config_file=None, host="0.0.0.0",
                port=8080, static_dir=None):
    srv = WebServer(streamer, cfg, gdir, static_dir or _default_static(), config_file)
    loop = asyncio.get_event_loop()
    loop.create_task(srv.driver())
    server = await asyncio.start_server(srv.handle, host, port)
    print("gcgo web UI on http://%s:%d  (gcode dir: %s)" % (host, port, gdir))
    while True:
        await asyncio.sleep(3600)
