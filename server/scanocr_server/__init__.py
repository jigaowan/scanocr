"""ScanOCR macOS server."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("scanocr-server")
except PackageNotFoundError:
    __version__ = "0.0.0.dev0+unknown"
