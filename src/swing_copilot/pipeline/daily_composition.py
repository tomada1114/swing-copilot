"""CLI parsing and real-adapter composition for ``copilot-daily``."""

from __future__ import annotations

import argparse
import logging
import shutil
import sys
import traceback
from datetime import date
from typing import TYPE_CHECKING, cast

from rich.console import Console

from swing_copilot.cli_support import ExitPolicy, run_cli
from swing_copilot.clock import SystemClock
from swing_copilot.config import (
    load_secrets,
    load_settings,
    load_strategies,
    require_secrets,
)
from swing_copilot.data.earnings_finnhub import FinnhubEarningsClient
from swing_copilot.data.edgar import EdgarClient
from swing_copilot.data.yfinance_provider import YFinanceProvider
from swing_copilot.exceptions import ConfigError, PreflightAbort
from swing_copilot.models import DailyRunOptions
from swing_copilot.pipeline.daily import (
    _LOG_LEVELS,
    DailyDependencies,
    _paths_for_mode,
    _run_mode,
)
from swing_copilot.pipeline.daily_runner import run_daily
from swing_copilot.pipeline.progress import ProgressReporter
from swing_copilot.ratelimit import (
    FINNHUB_MIN_REQUEST_INTERVAL_SECONDS,
    MinIntervalThrottle,
)
from swing_copilot.report.discord_notify import DiscordNotifier
from swing_copilot.report.terminal_report import (
    TerminalPaths,
    TerminalRunSummary,
    render_run_summary,
    render_terminal,
)
from swing_copilot.storage.database import Database
from swing_copilot.storage.market_store import MarketStore
from swing_copilot.storage.state_store import StateStore
from swing_copilot.text.calendar_fred import FredCalendarClient
from swing_copilot.text.news_finnhub import FinnhubNewsClient
from swing_copilot.universe import (
    UniverseError,
    UniverseFetchOptions,
    resolve_daily_universe,
)

if TYPE_CHECKING:
    from collections.abc import Iterable

    from swing_copilot.config import Secrets, Settings, StrategiesConfig

logger = logging.getLogger(__name__)


def _preflight_abort_message(exc: Exception) -> str:
    """Render the machine-readable first stderr line of a preflight abort.

    The `PREFLIGHT_ABORT[<reason>]:` prefix is a contract with the `swing-daily`
    skill: both abort causes share exit code 2, and without the tag the skill's
    "already analyzed today" summary would swallow a configuration problem. The
    cast is safe because `_PREFLIGHT_EXIT` only converts `PreflightAbort`.
    """
    abort = cast("PreflightAbort", exc)
    return f"PREFLIGHT_ABORT[{abort.reason}]: {abort}"


#: The universe cannot be resolved: the argparse convention (message as the
#: exit status, stderr, exit 1).
_UNIVERSE_EXIT = ExitPolicy(errors=(UniverseError,))
#: `run_daily` raises this for the same-day rerun guard (P8-118), since
#: `run_date` only resolves after prefetch, deep inside the run.
_PREFLIGHT_EXIT = ExitPolicy(
    errors=(PreflightAbort,), code=2, format_message=_preflight_abort_message
)


def _non_negative_int(raw_value: str) -> int:
    """Parse a CLI count without admitting Python's negative slicing semantics."""
    value = int(raw_value)
    if value < 0:
        msg = "must be greater than or equal to 0"
        raise argparse.ArgumentTypeError(msg)
    return value


def _parse_args(argv: list[str] | None = None) -> DailyRunOptions:
    parser = argparse.ArgumentParser(prog="copilot-daily")
    parser.add_argument("--as-of", type=date.fromisoformat, default=None)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--skip-text", action="store_true")
    parser.add_argument(
        "--limit",
        type=_non_negative_int,
        default=None,
        help="screen at most this many universe symbols; 0 keeps only open holdings",
    )
    parser.add_argument("--strategy", default="default")
    parser.add_argument("--log-level", choices=tuple(_LOG_LEVELS), default=None)
    parser.add_argument(
        "--allow-same-day-rerun",
        action="store_true",
        help=(
            "bypass the same-day rerun guard: proceed even when a successful "
            "run already exists for the resolved run_date"
        ),
    )
    args = parser.parse_args(argv)
    return DailyRunOptions(
        as_of=args.as_of,
        is_dry_run=args.dry_run,
        skip_text=args.skip_text,
        limit=args.limit,
        strategy_key=args.strategy,
        log_level=args.log_level,
        allow_same_day_rerun=args.allow_same_day_rerun,
    )


def _required_features(options: DailyRunOptions, settings: Settings) -> set[str]:
    features = {"edgar"}
    if not options.skip_text:
        features |= {"finnhub", "fred"}
    if settings.notification.enabled:
        features.add("discord")
    return features


def _finnhub_clients(
    secrets: Secrets, options: DailyRunOptions
) -> tuple[FinnhubNewsClient | None, FinnhubEarningsClient | None]:
    """Build the Finnhub clients behind one account-wide throttle.

    Finnhub's 60 calls/minute cap applies to the account behind the API key,
    not to a client object. Two clients each keeping their own interval state
    would together be free to exceed it, so the composition root hands both the
    same budget (Issue #263).

    Args:
        secrets: Loaded secrets; no client is built without a Finnhub key.
        options: Parsed run options; `skip_text` drops the news client only.

    Returns:
        The news client (`None` when text is skipped or no key is configured)
        and the earnings client (`None` when no key is configured).
    """
    if not secrets.finnhub_api_key:
        return None, None
    throttle = MinIntervalThrottle(FINNHUB_MIN_REQUEST_INTERVAL_SECONDS)
    news_client = (
        None
        if options.skip_text
        else FinnhubNewsClient(secrets.finnhub_api_key, throttle=throttle)
    )
    return news_client, FinnhubEarningsClient(
        secrets.finnhub_api_key, throttle=throttle
    )


def _compose_dependencies(
    options: DailyRunOptions, settings: Settings, strategies: StrategiesConfig
) -> DailyDependencies:
    """Wire real adapters for a live (non-test) run (composition root, FR-12)."""
    if options.strategy_key not in strategies.strategies:
        available = ", ".join(sorted(strategies.strategies))
        msg = f"Unknown strategy {options.strategy_key!r}; available: {available}"
        raise ConfigError(msg)
    secrets = load_secrets()
    require_secrets(secrets, _required_features(options, settings))

    mode = _run_mode(options)
    db_path, output_dir = _paths_for_mode(mode)
    database = Database(db_path)
    market_store = MarketStore(database)
    state_store = StateStore(database)
    state_store.init_schema()
    clock = SystemClock()

    universe_resolution = resolve_daily_universe(
        options.as_of or clock.today(),
        state_store,
        is_historical=options.as_of is not None,
        refresh_interval_days=settings.universe.refresh_interval_days,
        options=UniverseFetchOptions(
            snapshot_path=settings.universe.snapshot_path,
            manual_include=settings.universe.manual_include,
            manual_exclude=settings.universe.manual_exclude,
        ),
    )

    edgar_client = (
        EdgarClient(secrets.edgar_identity) if secrets.edgar_identity else None
    )
    news_client, earnings_client = _finnhub_clients(secrets, options)
    calendar_client = (
        FredCalendarClient(secrets.fred_api_key)
        if secrets.fred_api_key and not options.skip_text
        else None
    )
    notifier = (
        DiscordNotifier(secrets.discord_webhook_url)
        if settings.notification.enabled and secrets.discord_webhook_url
        else None
    )

    return DailyDependencies(
        data_provider=YFinanceProvider(),
        market_store=market_store,
        state_store=state_store,
        settings=settings,
        universe=universe_resolution.members,
        strategies_config=strategies.model_dump(),
        clock=clock,
        universe_snapshot_date=universe_resolution.snapshot_date,
        universe_warning=universe_resolution.warning,
        edgar_client=edgar_client,
        earnings_client=earnings_client,
        news_client=news_client,
        calendar_client=calendar_client,
        notifier=notifier,
        output_dir=output_dir,
        strategy_key=options.strategy_key,
        progress=ProgressReporter(Console(stderr=True)),
    )


class _SecretRedactionFilter(logging.Filter):
    """Replace configured secret values before a log record reaches stderr."""

    def __init__(self, secrets: Iterable[str | None]) -> None:
        super().__init__()
        self._secrets = tuple(
            sorted({secret for secret in secrets if secret}, key=len, reverse=True)
        )

    def filter(self, record: logging.LogRecord) -> bool:
        if not self._secrets:
            return True

        record.msg = self._redact(record.getMessage())
        record.args = ()
        if record.exc_info is not None:
            formatted = "".join(traceback.format_exception(*record.exc_info))
            record.exc_text = self._redact(formatted)
            record.exc_info = None
        return True

    def _redact(self, value: str) -> str:
        redacted = value
        for secret in self._secrets:
            redacted = redacted.replace(secret, "[REDACTED]")
        return redacted


def _configure_logging(secrets: Secrets, *, level: str | None = None) -> None:
    """Configure stderr logging levels and redaction for configured secrets."""
    root_level = _LOG_LEVELS[level] if level is not None else logging.WARNING
    application_level = _LOG_LEVELS[level] if level is not None else logging.INFO
    logging.basicConfig(
        level=root_level,
        stream=sys.stderr,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    logging.getLogger().setLevel(root_level)
    logging.getLogger("swing_copilot").setLevel(application_level)
    redaction_filter = _SecretRedactionFilter(
        (
            secrets.finnhub_api_key,
            secrets.fred_api_key,
            secrets.discord_webhook_url,
        )
    )
    for handler in logging.root.handlers:
        handler.addFilter(redaction_filter)


def main(argv: list[str] | None = None) -> None:
    """Parse options, compose collaborators, then render a terminal summary."""
    options = _parse_args(argv)
    _configure_logging(load_secrets(), level=options.log_level)
    settings = load_settings()
    strategies = load_strategies()
    deps = run_cli(
        lambda: _compose_dependencies(options, settings, strategies), _UNIVERSE_EXIT
    )
    result = run_cli(lambda: run_daily(options, deps), _PREFLIGHT_EXIT)
    paths = TerminalPaths(
        report=result.report_path,
        analysis_input=result.analysis_input_path,
    )
    summary = TerminalRunSummary(
        run_id=result.run_id,
        status=result.status,
        exit_code=result.exit_code,
        provider_name=result.provider_name,
        data_tier=result.data_tier,
        missing_sources=result.missing_sources,
        paths=paths,
    )
    width = shutil.get_terminal_size(fallback=(120, 24)).columns
    if result.brief is not None:
        sys.stdout.write(
            render_terminal(
                result.brief,
                result.status,
                width=width,
                color=sys.stdout.isatty(),
            )
        )
    sys.stdout.write(
        render_run_summary(summary, width=width, color=sys.stdout.isatty())
    )
    raise SystemExit(result.exit_code)
