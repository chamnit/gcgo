"""Minimal RFC 6455 WebSocket helpers (text frames only) — no dependencies,
works on CPython asyncio and MicroPython uasyncio."""

import hashlib
import binascii

_MAGIC = b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


def accept_key(client_key: bytes) -> bytes:
    """Compute the Sec-WebSocket-Accept value for the handshake."""
    digest = hashlib.sha1(client_key + _MAGIC).digest()
    return binascii.b2a_base64(digest).strip()


async def _read_exactly(reader, n: int) -> bytes:
    buf = b""
    while len(buf) < n:
        chunk = await reader.read(n - len(buf))
        if not chunk:
            raise EOFError
        buf += chunk
    return buf


async def read_message(reader):
    """Read one WebSocket frame. Returns ('text', str), ('close', None),
    ('ping', bytes), or ('pong', bytes). Raises EOFError on disconnect."""
    b0, b1 = await _read_exactly(reader, 2)
    opcode = b0 & 0x0F
    masked = b1 & 0x80
    length = b1 & 0x7F
    if length == 126:
        ext = await _read_exactly(reader, 2)
        length = (ext[0] << 8) | ext[1]
    elif length == 127:
        ext = await _read_exactly(reader, 8)
        length = 0
        for byte in ext:
            length = (length << 8) | byte
    mask = await _read_exactly(reader, 4) if masked else b""
    payload = await _read_exactly(reader, length) if length else b""
    if masked:
        payload = bytes(payload[i] ^ mask[i & 3] for i in range(length))
    if opcode == 0x8:
        return ("close", None)
    if opcode == 0x9:
        return ("ping", payload)
    if opcode == 0xA:
        return ("pong", payload)
    return ("text", payload.decode("utf-8", "replace"))


def encode_text(text: str) -> bytes:
    """Encode a server->client (unmasked) text frame."""
    payload = text.encode("utf-8")
    n = len(payload)
    if n < 126:
        header = bytes((0x81, n))
    elif n < 65536:
        header = bytes((0x81, 126, (n >> 8) & 0xFF, n & 0xFF))
    else:
        header = bytes((0x81, 127, 0, 0, 0, 0,
                        (n >> 24) & 0xFF, (n >> 16) & 0xFF, (n >> 8) & 0xFF, n & 0xFF))
    return header + payload


def encode_pong(payload: bytes) -> bytes:
    return bytes((0x8A, len(payload) & 0x7F)) + payload
