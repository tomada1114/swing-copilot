"""Public package interface for swing_copilot."""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version

from swing_copilot.config import (
    Secrets,
    Settings,
    load_secrets,
    load_settings,
    require_secrets,
)
from swing_copilot.exceptions import ConfigError, SwingCopilotError

try:
    __version__ = version("swing-copilot")
except PackageNotFoundError:
    __version__ = "0.0.0+unknown"

__all__ = [
    "ConfigError",
    "Secrets",
    "Settings",
    "SwingCopilotError",
    "__version__",
    "load_secrets",
    "load_settings",
    "require_secrets",
]
