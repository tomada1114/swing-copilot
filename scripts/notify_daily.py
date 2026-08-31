"""Send the once-a-day post-verdict Discord notification (Issue #383, FR-09).

Thin CI composition root, mirroring `scripts/check_daily_complete.py`'s shape:
this file only resolves paths/secrets and calls `DiscordNotifier`; every
deterministic decision about *what* to say lives in the pure, unit-tested
`swing_copilot.report.verdict_notification` module.

`.github/workflows/swing-daily.yml` runs this unconditionally (`always()`)
after `check_daily_complete.py`, live-mode only, so every terminal state --
success, degraded, failed, or any preflight abort -- gets exactly one Discord
send. `continue-on-error: true` on that step keeps a notification failure from
taking down the day's R2 push, which already happened earlier in the job.
"""

from __future__ import annotations

import argparse
import logging
import os
from pathlib import Path

from swing_copilot.cli_support import LOG_LEVELS, configure_cli_logging
from swing_copilot.config import load_secrets, load_settings
from swing_copilot.report.discord_notify import DiscordNotifier
from swing_copilot.report.verdict_notification import build_daily_notification

logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_REPORTS_DIR = REPO_ROOT / "reports"

#: Mirrors `pipeline.daily_composition._OUTCOME_FILE_ENV_VAR`: `copilot-daily`
#: writes its terminal outcome here on every documented exit path, and the
#: workflow exports the same variable job-wide for every step to read.
_OUTCOME_FILE_ENV_VAR = "COPILOT_DAILY_OUTCOME_FILE"


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="notify_daily",
        description="定性 verdict 確定後の日次 Discord 通知を送る",
    )
    parser.add_argument(
        "--outcome-file",
        type=Path,
        # An unset/empty environment fallback must resolve to `None`, not
        # `Path("")`, the same way `daily_composition.py`'s own `--outcome-file`
        # default does.
        default=os.environ.get(_OUTCOME_FILE_ENV_VAR) or None,
        help=(
            "copilot-daily の終了状態 JSON のパス。"
            f"既定では {_OUTCOME_FILE_ENV_VAR} 環境変数を読む"
        ),
    )
    parser.add_argument(
        "--reports-dir",
        type=Path,
        default=DEFAULT_REPORTS_DIR,
        help="日次runアーカイブの場所 (既定: リポジトリの reports/)",
    )
    parser.add_argument(
        "--settings",
        type=Path,
        default=Path("config/settings.yaml"),
        help="settings.yaml のパス (notification.enabled を読む)",
    )
    parser.add_argument("--log-level", choices=tuple(LOG_LEVELS), default=None)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """CLI entry point. Returns 1 on a configuration problem or a failed send."""
    args = _parse_args(argv)
    secrets = load_secrets()
    configure_cli_logging(secrets, level=args.log_level)
    settings = load_settings(str(args.settings))

    if not settings.notification.enabled:
        logger.info("notification.enabled=false; skipping the daily notification")
        return 0
    if not secrets.discord_webhook_url:
        logger.error(
            "notification.enabled=true but DISCORD_WEBHOOK_URL is not configured; "
            "cannot send the daily notification"
        )
        return 1

    messages = build_daily_notification(
        outcome_file=args.outcome_file, reports_dir=args.reports_dir
    )
    notifier = DiscordNotifier(secrets.discord_webhook_url)
    for index, message in enumerate(messages, start=1):
        if not notifier.notify(message, None):
            logger.error(
                "Discord notification failed on message %d/%d; stopping",
                index,
                len(messages),
            )
            return 1
    logger.info("sent %d Discord message(s) for the daily notification", len(messages))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
