"""Package-level exception hierarchy."""

from __future__ import annotations


class SwingCopilotError(Exception):
    """Base class for all errors raised by swing_copilot."""


class ConfigError(SwingCopilotError):
    """Raised when settings or secrets fail validation."""
