"""Package-level exception hierarchy."""

from __future__ import annotations

from typing import Literal

#: Why a run was intentionally aborted before any state was written. A closed
#: vocabulary, kept as a `Literal`, because the unattended `swing-daily` skill
#: branches on the tag rather than on prose: today a `same_day_rerun` is
#: summarized as "already analyzed today", and a `no_trading_day` (Issue #372)
#: as "no trading day to analyze yet". The `account_equity_unset` cause went
#: with the real-trade record removal (2026-08) -- no daily run has realized
#: P&L to halt over any more.
PreflightAbortReason = Literal["same_day_rerun", "no_trading_day"]


class SwingCopilotError(Exception):
    """Base class for all errors raised by swing_copilot."""


class ConfigError(SwingCopilotError):
    """Raised when settings or secrets fail validation."""


class StorageSchemaError(SwingCopilotError):
    """Raised when a read-only command cannot find its required schema."""


class PreflightAbort(SwingCopilotError):  # noqa: N818 - named "Abort" per P8-117's design
    """Raised to intentionally abort a run before any state is recorded.

    `main()` converts this to exit code 2 -- distinct from 0 (success) and 1
    (failure): continuing would not fail, it would just be pointless (P8-117).
    Not named `PreflightAbortError` -- this is an intentional control-flow
    signal, not a failure, and #118 (which raises the same exception from a
    later preflight condition) depends on this exact name.

    `reason` names the abort cause, and `main()` prefixes stderr with the
    machine-readable `PREFLIGHT_ABORT[<reason>]:` so the consuming skill never
    has to infer it from prose.
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
