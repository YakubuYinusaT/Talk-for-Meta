"""Asterisk's AudioSocket protocol: one TCP connection per call, a stream of frames in both directions.

A frame is one byte of kind, two bytes of length (big endian), then the payload:
    0x01  the call's id (16 bytes, a UUID), sent once, first
    0x10  audio: 8 kHz, 16-bit signed little-endian, mono; Asterisk sends 20 ms (320 bytes) at a time
    0x03  one keypad digit, as a character
    0x00  hang up (either side)
    0xff  an error on Asterisk's side
"""

import asyncio
import re
import uuid

HANGUP, ID, DTMF, AUDIO, ERROR = 0x00, 0x01, 0x03, 0x10, 0xFF
FRAME_MS = 20
FRAME_BYTES = 320  # 20 ms at 8 kHz, 16 bits


def pack(kind: int, payload: bytes = b"") -> bytes:
    return bytes([kind]) + len(payload).to_bytes(2, "big") + payload


async def read_frame(reader: asyncio.StreamReader) -> tuple[int, bytes] | None:
    """The next frame, or None when the connection has ended."""
    try:
        head = await reader.readexactly(3)
        size = int.from_bytes(head[1:], "big")
        return head[0], (await reader.readexactly(size) if size else b"")
    except (asyncio.IncompleteReadError, ConnectionError):
        return None


def uuid_text(payload: bytes) -> str:
    return str(uuid.UUID(bytes=payload))


def uuid_for_number(number: str) -> str:
    """The id a dialplan would build from a caller's digits (see deploy/setup_sip.sh): the digits right-aligned in 32 hex places and zero-filled. The tests use it."""
    digits = re.sub(r"\D", "", number)[-32:].rjust(32, "0")
    return f"{digits[:8]}-{digits[8:12]}-{digits[12:16]}-{digits[16:20]}-{digits[20:]}"


def number_from_uuid(text: str) -> str:
    """The caller's number, as the dialplan hid it in the call's id. Asterisk's AudioSocket carries no caller id of its own, so the dialplan writes the
    digits into the id it passes. Empty when the caller hid the number."""
    digits = text.replace("-", "").lstrip("0")
    if not digits:
        return ""
    if digits.startswith("234") or len(digits) != 10:
        return "+" + digits
    return "+234" + digits  # a national number that lost its leading zero (8012345678)
