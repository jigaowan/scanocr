from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, BinaryIO, Callable, Dict, Optional, Tuple


class MultipartError(ValueError):
    pass


class PayloadTooLarge(MultipartError):
    pass


class UnsupportedMedia(MultipartError):
    pass


class LimitedInput:
    def __init__(self, source: BinaryIO, content_length: int, maximum: int):
        if content_length < 0:
            raise MultipartError("Content-Length is required")
        if content_length > maximum:
            raise PayloadTooLarge("request body exceeds configured limit")
        self.source = source
        self.remaining = content_length
        self.maximum = maximum
        self.read_count = 0

    def read(self, size: int) -> bytes:
        if self.remaining <= 0:
            return b""
        data = self.source.read(min(size, self.remaining))
        self.remaining -= len(data)
        self.read_count += len(data)
        if self.read_count > self.maximum:
            raise PayloadTooLarge("request body exceeds configured limit")
        return data


class ChunkedInput:
    """Decode an HTTP/1.1 chunked request while enforcing the same body fuse."""

    def __init__(self, source: BinaryIO, maximum: int):
        self.source = source
        self.maximum = maximum
        self.read_count = 0
        self.chunk_remaining = 0
        self.finished = False

    def _line(self) -> bytes:
        line = self.source.readline(8193)
        if not line.endswith(b"\r\n") or len(line) > 8192:
            raise MultipartError("invalid chunked transfer framing")
        return line[:-2]

    def read(self, size: int) -> bytes:
        if self.finished:
            return b""
        if self.chunk_remaining == 0:
            raw_size = self._line().split(b";", 1)[0]
            try:
                self.chunk_remaining = int(raw_size, 16)
            except ValueError:
                raise MultipartError("invalid chunk size")
            if self.chunk_remaining == 0:
                while self._line():
                    pass
                self.finished = True
                return b""
        data = self.source.read(min(size, self.chunk_remaining))
        if not data:
            raise MultipartError("truncated chunked request")
        self.chunk_remaining -= len(data)
        self.read_count += len(data)
        if self.read_count > self.maximum:
            raise PayloadTooLarge("request body exceeds configured limit")
        if self.chunk_remaining == 0 and self.source.read(2) != b"\r\n":
            raise MultipartError("invalid chunk terminator")
        return data


@dataclass
class Part:
    name: str
    filename: Optional[str]
    content_type: str


class MultipartStream:
    """Small streaming multipart parser with a bounded rolling buffer."""

    def __init__(self, source: Any, boundary: bytes):
        if not boundary or len(boundary) > 200:
            raise MultipartError("invalid multipart boundary")
        self.source = source
        self.boundary = boundary
        self.buffer = bytearray()

    def _fill(self, wanted: int = 1) -> bool:
        while len(self.buffer) < wanted:
            block = self.source.read(65536)
            if not block:
                return False
            self.buffer.extend(block)
        return True

    def readline(self, limit: int = 16384) -> bytes:
        while True:
            position = self.buffer.find(b"\r\n")
            if position >= 0:
                line = bytes(self.buffer[:position + 2])
                del self.buffer[:position + 2]
                return line
            if len(self.buffer) > limit:
                raise MultipartError("multipart header line is too long")
            block = self.source.read(4096)
            if not block:
                raise MultipartError("unexpected end of multipart body")
            self.buffer.extend(block)

    def start(self) -> bool:
        line = self.readline()
        if line == b"--" + self.boundary + b"--\r\n":
            return False
        if line != b"--" + self.boundary + b"\r\n":
            raise MultipartError("missing initial multipart boundary")
        return True

    def headers(self) -> Tuple[Part, Dict[str, str]]:
        headers: Dict[str, str] = {}
        total = 0
        while True:
            line = self.readline()
            total += len(line)
            if total > 65536:
                raise MultipartError("multipart headers are too large")
            if line == b"\r\n":
                break
            try:
                key, value = line[:-2].decode("utf-8").split(":", 1)
            except (UnicodeDecodeError, ValueError):
                raise MultipartError("invalid multipart header")
            headers[key.strip().lower()] = value.strip()
        disposition = headers.get("content-disposition", "")
        name_match = re.search(r'(?:^|;)\s*name="([^"]*)"', disposition)
        filename_match = re.search(r'(?:^|;)\s*filename="([^"]*)"', disposition)
        if not name_match:
            raise MultipartError("part is missing a name")
        return Part(
            name=name_match.group(1),
            filename=filename_match.group(1) if filename_match else None,
            content_type=headers.get("content-type", "application/octet-stream").split(";", 1)[0].strip().lower(),
        ), headers

    def body(self, consume: Callable[[bytes], None]) -> bool:
        marker = b"\r\n--" + self.boundary
        keep = len(marker) + 4
        while True:
            position = self.buffer.find(marker)
            if position >= 0:
                consume(bytes(self.buffer[:position]))
                del self.buffer[:position + len(marker)]
                self._fill(2)
                if self.buffer.startswith(b"--"):
                    del self.buffer[:2]
                    self._fill(2)
                    if self.buffer.startswith(b"\r\n"):
                        del self.buffer[:2]
                    return True
                if self.buffer.startswith(b"\r\n"):
                    del self.buffer[:2]
                    return False
                raise MultipartError("invalid multipart boundary suffix")
            if len(self.buffer) > keep:
                cut = len(self.buffer) - keep
                consume(bytes(self.buffer[:cut]))
                del self.buffer[:cut]
            block = self.source.read(65536)
            if not block:
                raise MultipartError("multipart body ended before boundary")
            self.buffer.extend(block)


def boundary_from_content_type(content_type: str) -> bytes:
    if not content_type.lower().startswith("multipart/form-data"):
        raise UnsupportedMedia("Content-Type must be multipart/form-data")
    match = re.search(r'boundary=(?:"([^"]+)"|([^;\s]+))', content_type, re.I)
    if not match:
        raise MultipartError("multipart boundary is missing")
    return (match.group(1) or match.group(2)).encode("ascii", "strict")
