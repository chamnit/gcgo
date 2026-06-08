"""Optional SD-card mounting for MicroPython deployments.

SD cards are 3.3 V like the MCU, so no level shifting is needed. Wire the card
to an SPI bus (SCK/MOSI/MISO) plus a chip-select pin, then mount it before
handing its path to gcgo as the g-code directory.

Boards differ: ESP32 has a builtin machine.SDCard; RP2040/Pico W needs the
standard `sdcard.py` driver copied onto the board. This tries both and never
raises — a missing card or driver just returns None so the app keeps running
from flash.
"""


def mount_sd(mount="/sd", spi_id=1, sck=10, mosi=11, miso=12, cs=13):
    """Mount an SPI SD card at `mount`. Returns the mount path on success, or
    None (with a printed reason) if there's no card or driver. Non-fatal."""
    import os
    from machine import Pin, SPI

    try:
        card = _make_card(spi_id, sck, mosi, miso, cs)
    except ImportError:
        print("SD: no 'sdcard' driver on board (copy sdcard.py); skipping")
        return None
    except OSError as e:
        print("SD: no card / init failed (%s); skipping" % e)
        return None

    try:
        os.mount(card, mount)
    except OSError as e:
        # already mounted? treat as success; otherwise report and skip
        if "EPERM" in str(e) or "already" in str(e):
            print("SD: already mounted at", mount)
            return mount
        print("SD: mount failed (%s); skipping" % e)
        return None
    print("SD mounted at", mount)
    return mount


def _make_card(spi_id, sck, mosi, miso, cs):
    from machine import Pin, SPI
    try:
        # ESP32 and similar: builtin SD support
        from machine import SDCard
        return SDCard(slot=spi_id)
    except (ImportError, TypeError, ValueError):
        pass
    # RP2040/Pico W and most others: the sdcard.py driver over SPI
    import sdcard
    spi = SPI(spi_id, sck=Pin(sck), mosi=Pin(mosi), miso=Pin(miso))
    return sdcard.SDCard(spi, Pin(cs))
