"""Package-level exception hierarchy."""

from __future__ import annotations

from typing import Literal

#: Why a run was intentionally aborted before any state was written. A closed
#: vocabulary because the unattended `swing-daily` skill branches on it: a
#: `same_day_rerun` is summarized as "already analyzed today", while an
#: `account_equity_unset` must surface as a configuration problem — the two
#: share exit code 2 but demand opposite reactions.
PreflightAbortReason = Literal["account_equity_unset", "same_day_rerun"]


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

    `reason` distinguishes the abort causes that share exit code 2, and
    `main()` prefixes stderr with the machine-readable
    `PREFLIGHT_ABORT[<reason>]:` so the consuming skill never has to infer
    the cause from prose (which silently misclassified an
    `account_equity_unset` abort as "already analyzed" before this field
    existed).
    """

    def __init__(self, message: str, *, reason: PreflightAbortReason) -> None:
        """Create the abort signal.

        Args:
            message: Human-readable explanation, written for stderr.
            reason: Which preflight condition fired; see
                `PreflightAbortReason`.
        """
        super().__init__(message)
        self.reason: PreflightAbortReason = reason
