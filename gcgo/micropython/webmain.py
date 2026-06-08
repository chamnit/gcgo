"""Pico W web pendant: connect WiFi, hardware-reset GRBL, and serve the gcgo
web UI over uasyncio, talking to GRBL on UART0.

Wiring (this board): GP16 = UART0 TX -> Arduino RX, GP17 = UART0 RX <- Arduino
TX, GP18 -> Arduino RESET (active-low, through level shifters).

On the board:
    from gcgo.micropython.webmain import start
    start()                      # reads WiFi creds from secrets.py
    start("ssid", "password")    # or pass them directly
"""

import time

try:
    import asyncio
except ImportError:
    import uasyncio as asyncio

from machine import Pin

from gcgo.core.config import StatusConfig
from gcgo.core.protocol import Streamer
from gcgo.micropython.transport import UARTTransport, hard_reset
from gcgo.frontends.web.server import serve

CONFIG_FILE = "gcgo_config.json"


def connect_wifi(ssid, password, timeout=20):
    import network
    wlan = network.WLAN(network.STA_IF)
    wlan.active(True)
    if not wlan.isconnected():
        wlan.connect(ssid, password)
        deadline = time.ticks_add(time.ticks_ms(), timeout * 1000)
        while not wlan.isconnected():
            if time.ticks_diff(deadline, time.ticks_ms()) <= 0:
                raise OSError("WiFi connect timed out")
            time.sleep_ms(250)
    return wlan.ifconfig()[0]


def start(ssid=None, password=None, uart_id=0, tx=16, rx=17, reset=18,
          gdir="/gcode", http_port=80):
    if ssid is None:
        import secrets  # a secrets.py with WIFI_SSID / WIFI_PASS on the board
        ssid, password = secrets.WIFI_SSID, secrets.WIFI_PASS

    ip = connect_wifi(ssid, password)
    print("WiFi connected:", ip)

    hard_reset(reset)  # reboot GRBL into a known state; welcome read by service()

    transport = UARTTransport(uart_id, tx=Pin(tx), rx=Pin(rx))
    cfg = StatusConfig()
    cfg.load(CONFIG_FILE)
    streamer = Streamer(transport)

    print("Open http://%s:%d/ in a browser" % (ip, http_port))
    asyncio.run(serve(streamer, cfg, gdir, config_file=CONFIG_FILE,
                      host="0.0.0.0", port=http_port))
