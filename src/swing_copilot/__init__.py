"""Public package interface for swing_copilot."""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version

from .core import add

try:
    __version__ = version("swing-copilot")
except PackageNotFoundError:
    __version__ = "0.0.0+unknown"

__all__ = ["__version__", "add"]
