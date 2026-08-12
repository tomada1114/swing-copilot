"""Package-level exception hierarchy."""

from __future__ import annotations


class SwingCopilotError(Exception):
    """Base class for all errors raised by swing_copilot."""


class ConfigError(SwingCopilotError):
    """Raised when settings or secrets fail validation."""


class PreflightAbort(SwingCopilotError):  # noqa: N818 - named "Abort" per P8-117's design
    """Raised to intentionally abort a run before any state is recorded.

    `main()` converts this to exit code 2 -- distinct from 0 (success) and 1
    (failure): continuing would not fail, it would just be pointless (P8-117).
    Not named `PreflightAbortError` -- this is an intentional control-flow
    signal, not a failure, and #118 (which raises the same exception from a
    later preflight condition) depends on this exact name.
    """
