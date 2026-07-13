from __future__ import annotations

import struct
import zlib
from pathlib import Path
from typing import Tuple

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


class PngError(ValueError):
    pass


def validate_png(path: Path) -> Tuple[int, int]:
    """Validate the PNG chunk stream without loading image pixels into memory."""
    with path.open("rb") as stream:
        if stream.read(8) != PNG_SIGNATURE:
            raise PngError("invalid PNG signature")
        width = height = None
        saw_ihdr = saw_iend = False
        chunk_index = 0
        while True:
            length_bytes = stream.read(4)
            if not length_bytes:
                break
            if len(length_bytes) != 4:
                raise PngError("truncated chunk length")
            length = struct.unpack(">I", length_bytes)[0]
            chunk_type = stream.read(4)
            if len(chunk_type) != 4:
                raise PngError("truncated chunk type")
            if chunk_index == 0 and chunk_type != b"IHDR":
                raise PngError("IHDR must be the first chunk")
            crc = zlib.crc32(chunk_type)
            remaining = length
            ihdr = bytearray()
            while remaining:
                block = stream.read(min(65536, remaining))
                if not block:
                    raise PngError("truncated chunk data")
                if chunk_type == b"IHDR":
                    ihdr.extend(block)
                crc = zlib.crc32(block, crc)
                remaining -= len(block)
            expected_crc = stream.read(4)
            if len(expected_crc) != 4:
                raise PngError("truncated chunk CRC")
            if struct.unpack(">I", expected_crc)[0] != crc & 0xFFFFFFFF:
                raise PngError("PNG chunk CRC mismatch")
            if chunk_type == b"IHDR":
                if saw_ihdr or length != 13:
                    raise PngError("invalid IHDR")
                width, height = struct.unpack(">II", ihdr[:8])
                if width == 0 or height == 0:
                    raise PngError("PNG dimensions must be positive")
                saw_ihdr = True
            elif chunk_type == b"IEND":
                if length != 0:
                    raise PngError("invalid IEND")
                saw_iend = True
                if stream.read(1):
                    raise PngError("data after IEND")
                break
            chunk_index += 1
        if not saw_ihdr or not saw_iend:
            raise PngError("incomplete PNG")
        return int(width), int(height)
