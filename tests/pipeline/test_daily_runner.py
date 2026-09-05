"""Tests for pipeline/daily_runner.py's lifecycle (FR-12).

Covers `run_daily`'s imperative sequencing and terminal-state decisions: the
fatal-step short-circuit, the NFR-03 timeout budget's effect on the whole
run, run-date resolution (#372), the same-day rerun guard (P8-118), and the
prior-analysis-gap preflight check (#254). Step behavior (fundamentals
freshness, screening, risk, output) is covered by
tests/pipeline/test_daily_steps.py; further fail-soft coverage lives in
tests/pipeline/test_failsoft.py and tests/test_e2e_smoke.py.
"""

from __future__ import annotations

import hashlib
import itertools
import json
import logging
import sys
from dataclasses import replace
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from uuid import uuid4
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from swing_copilot.config import (
    StrategiesConfig,
    config_snapshot_hash,
    config_snapshot_sections,
)
from swing_copilot.data.base import FetchFailure
from swing_copilot.exceptions import PreflightAbort
from swing_copilot.models import DailyRunOptions, RunStatus
from swing_copilot.pipeline.daily import (
    DailyDependencies,
    config_hash,
    run_daily,
)
from swing_copilot.pipeline.daily_runner import _ANALYSIS_GAP_LOOKBACK_DAYS
from swing_copilot.screening import (
    fundamental_filters as _fundamental_filters,  # noqa: F401 - imported for its @register_filter side effect
)
from swing_copilot.screening import (
    technical_signals as _technical_signals,  # noqa: F401 - imported for its @register_signal side effect
)
from swing_copilot.storage.market_store import (
    FundamentalsRecord,
)
from tests.pipeline.conftest import (
    _NOW,
    AS_OF,
    STRATEGIES_CONFIG,
    FakeMonotonic,
    _bars_for,
    _member,
)
from tests.support.fakes import FixedClock, StubDataProvider
from tests.support.runs import seed_run

#: `date.weekday()` value for Friday, named for `TestRunDateResolvesOnlyClosedSessions`.
_FRIDAY = 4


# TestPastDecisionsThreading: a second strategy key, to distinguish "the
# correct strategy_key threaded through" from "it happened to match the
# only strategy that exists".
TWO_STRATEGIES_CONFIG = StrategiesConfig.model_validate(
    {
        "strategies": {
            "default": {
                "filters_all": ["volume_min"],
                "signals_all": ["trend_sma"],
                "candidate_limit": 10,
            },
            "growth_v2": {
                "filters_all": ["volume_min"],
                "signals_all": ["trend_sma"],
                "candidate_limit": 10,
            },
        }
    }
)


class _FixedNowClock:
    """A `Clock` whose `now()`/`today()` are pinned to one instant.

    `FixedClock(AS_OF, _NOW)` (module-level) pins `now()` to a fixed UTC noon regardless of
    what it is asked about; the closed-session tests below need `now()` to
    land at exact minute-level offsets from a session's 16:00 ET close, so
    they construct this directly instead.
    """

    def __init__(self, now: datetime) -> None:
        self._now = now

    def now(self) -> datetime:
        return self._now

    def today(self) -> date:
        return self._now.date()


def _empty_bars() -> pd.DataFrame:
    """A bar frame with the right columns and no rows."""
    return pd.DataFrame(
        columns=["symbol", "date", "open", "high", "low", "close", "volume"]
    )


class _RaisingDataProvider:
    """A `DataProvider` whose price fetch always raises."""

    def __init__(self, error: Exception) -> None:
        self._error = error

    def get_daily_bars(self, symbols, start, end):
        del symbols, start, end
        raise self._error

    def get_latest_bars(self, symbols, as_of):
        del symbols, as_of
        raise self._error


_LIVE_RUN_DATE = AS_OF - timedelta(days=1)


_PRIOR_RUN_DATE = _LIVE_RUN_DATE - timedelta(days=1)
_GAP_TAG = "ANALYSIS_GAP[missing_analysis_result]:"
#: Keeps every archived run's `started_at` distinct and ordered.
_ARCHIVE_SEQUENCE = itertools.count()


def _archive_run(
    deps,
    run_date=_PRIOR_RUN_DATE,
    status="success",
    *,
    exported=True,
    analyzed=True,
):
    """Recreate one finished run's `runs` row and its `reports/` artifacts.

    Consecutive calls get strictly later `started_at` values, so two runs
    archived for the same date are ordered by the order they were archived in.
    """
    run_id = uuid4()
    run_dir = Path(deps.output_dir) / run_date.isoformat() / str(run_id)
    run_dir.mkdir(parents=True)
    (Path(deps.output_dir) / run_date.isoformat() / f"{run_id}.md").write_text(
        "# report", encoding="utf-8"
    )
    if exported:
        (run_dir / "analysis_input.json").write_text("{}", encoding="utf-8")
    if analyzed:
        (run_dir / "analysis_result.json").write_text("{}", encoding="utf-8")
    started_at = datetime(
        run_date.year, run_date.month, run_date.day, 18, tzinfo=UTC
    ) + timedelta(minutes=next(_ARCHIVE_SEQUENCE))
    seed_run(deps.state_store, run_id, run_date, status=status, started_at=started_at)
    return run_id, run_dir


def _stored_gaps(state_store, run_id):
    with state_store.database.connect() as conn:
        row = conn.execute(
            "SELECT metadata_json FROM runs WHERE run_id = ?", [str(run_id)]
        ).fetchone()
    return json.loads(row[0]).get("prior_analysis_gaps")


def _gap_lines(capsys):
    """The stderr lines that actually start with the machine-readable tag."""
    return [
        line
        for line in capsys.readouterr().err.splitlines()
        if line.startswith(_GAP_TAG)
    ]


class _BrokenStderr:
    """A stderr whose every write fails, like a closed `| head` pipe."""

    def write(self, _text):
        msg = "Broken pipe"
        raise BrokenPipeError(32, msg)

    def flush(self):
        return None


class TestHappyPath:
    def test_completes_all_pipeline_steps_successfully(self, deps, state_store):
        result = run_daily(DailyRunOptions(as_of=AS_OF, is_dry_run=True), deps)

        assert result.status == RunStatus.SUCCESS
        assert result.exit_code == 0
        assert result.run_date == AS_OF

        with state_store.database.connect() as conn:
            steps = conn.execute(
                "SELECT step, status FROM run_steps WHERE run_id = ? ORDER BY step",
                [str(result.run_id)],
            ).fetchall()
        assert [s[0] for s in steps] == [
            "1_prices",
            "2_fundamentals",
            "3_screening",
            "4_risk",
            "5_text",
            "6_analysis_export",
            "8_output",
            "postmortem",
            "retro_collect",
            "retro_evaluate",
            "track_update",
        ]
        # 1/3/4/8 succeed outright; 2/5/6 are deliberate skips (no optional
        # clients configured); postmortem (P2-11), the retro collect/evaluate
        # steps (P8-30) and verdict tracking succeed with nothing to look back
        # at yet — none of these are failures.
        assert all(status in {"success", "skipped"} for _step, status in steps)

        bars = deps.market_store.read_bars(
            ["AAPL", "MSFT"], AS_OF - timedelta(days=400), AS_OF, AS_OF
        )
        assert set(bars["fetched_at"]) == {pd.Timestamp("2027-03-01T12:00:00Z")}

    def test_default_null_progress_keeps_run_daily_stderr_silent(self, deps, capsys):
        run_daily(DailyRunOptions(as_of=AS_OF, is_dry_run=True), deps)

        assert capsys.readouterr().err == ""

    def test_persists_insufficient_regime_snapshot_without_stopping_run(
        self, deps, state_store
    ):
        result = run_daily(DailyRunOptions(as_of=AS_OF, is_dry_run=True), deps)

        with state_store.database.connect() as conn:
            row = conn.execute(
                "SELECT gate_verdict, dd_level, data_quality FROM regime_snapshots WHERE run_id = ?",
                [str(result.run_id)],
            ).fetchone()
        assert result.status is RunStatus.SUCCESS
        assert row == ("UNKNOWN", "UNKNOWN", "INSUFFICIENT")

    def test_persists_conservative_exposure_for_insufficient_regime(
        self, deps, state_store
    ):
        result = run_daily(DailyRunOptions(as_of=AS_OF, is_dry_run=True), deps)

        with state_store.database.connect() as conn:
            row = conn.execute(
                "SELECT verdict, data_quality FROM exposure_decisions WHERE run_id = ?",
                [str(result.run_id)],
            ).fetchone()
        assert row == ("CASH_PRIORITY", "INSUFFICIENT")


class TestRunFingerprintAndMetadata:
    def test_fingerprint_is_canonical_and_covers_settings_strategy_and_key(self, deps):
        canonical = config_hash(deps.settings, STRATEGIES_CONFIG, "default")
        reordered = StrategiesConfig.model_validate(
            {
                "strategies": {
                    "default": {
                        "candidate_limit": 10,
                        "signals_all": ["trend_sma"],
                        "filters_all": ["volume_min"],
                    }
                }
            }
        )
        changed_strategy = StrategiesConfig.model_validate(
            {
                "strategies": {
                    "default": {
                        "filters_all": ["volume_min"],
                        "signals_all": ["trend_sma"],
                        "candidate_limit": 9,
                    }
                }
            }
        )
        changed_risk = deps.settings.risk.model_copy(
            update={"wide_stop_threshold_pct": 12.0}
        )
        changed_settings = deps.settings.model_copy(update={"risk": changed_risk})

        assert len(canonical) == 64
        assert canonical == config_hash(deps.settings, reordered, "default")
        assert canonical != config_hash(deps.settings, changed_strategy, "default")
        assert canonical != config_hash(changed_settings, STRATEGIES_CONFIG, "default")
        assert canonical != config_hash(
            deps.settings, TWO_STRATEGIES_CONFIG, "growth_v2"
        )

    def test_config_hash_matches_the_pre_refactor_dict_round_trip_algorithm(self, deps):
        """Fingerprint stability across #396's typed threading.

        Threading `StrategiesConfig` through typed instead of a
        `model_dump()`'d dict indexed by `config_hash` must not move this
        fingerprint -- it is persisted (`runs.config_hash`) and drives rerun
        identity.
        """
        new_hash = config_hash(deps.settings, STRATEGIES_CONFIG, "default")

        # Reproduce the pre-#396 shape byte-for-byte: `DailyDependencies.
        # strategies_config` as a `model_dump()`'d dict, dict-indexed by
        # `config_hash` instead of read off the typed model.
        legacy_strategies_config = STRATEGIES_CONFIG.model_dump()
        legacy_selected_strategy = legacy_strategies_config["strategies"]["default"]
        legacy_payload = {
            "settings": deps.settings.model_dump(mode="json"),
            "strategy_key": "default",
            "strategy_spec": legacy_selected_strategy,
        }
        legacy_canonical = json.dumps(
            legacy_payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True
        )
        legacy_hash = hashlib.sha256(legacy_canonical.encode("utf-8")).hexdigest()

        assert new_hash == legacy_hash
        # Pinned literal: fails loudly if a future change (accidental or not)
        # ever moves this fingerprint for this exact fixture set.
        assert new_hash == (
            "62ff69bea5dbbc313d9084e8ee0d978da9b77a8f5b540453de05e007912f83cc"
        )

    def test_run_records_what_its_config_hash_stood_for(self, deps, state_store):
        """Issue #189: the hash alone is one-way, so the values are written too."""
        result = run_daily(DailyRunOptions(as_of=AS_OF, is_dry_run=True), deps)

        (version,) = state_store.get_config_versions()
        assert (
            version.config_hash
            == state_store.get_run_config_hashes(AS_OF)[result.run_id]
        )
        assert version.sections == config_snapshot_sections(deps.settings)
        assert version.snapshot_hash == config_snapshot_hash(version.sections)

    def test_the_ledger_keeps_the_first_run_date_a_configuration_was_seen_on(
        self, deps, state_store
    ):
        run_daily(DailyRunOptions(as_of=AS_OF, is_dry_run=True), deps)

        run_daily(
            DailyRunOptions(as_of=AS_OF, is_dry_run=True, allow_same_day_rerun=True),
            deps,
        )

        (version,) = state_store.get_config_versions()
        assert version.first_seen_run_date == AS_OF

    def test_run_persists_reconstructable_metadata(self, deps, state_store):
        tracked_deps = replace(
            deps,
            universe_snapshot_date=date(2027, 2, 20),
        )

        result = run_daily(DailyRunOptions(as_of=AS_OF, is_dry_run=True), tracked_deps)

        with state_store.database.connect() as conn:
            row = conn.execute(
                "SELECT config_hash, metadata_json FROM runs WHERE run_id = ?",
                [str(result.run_id)],
            ).fetchone()
        metadata = json.loads(row[1])
        assert row[0] == config_hash(
            tracked_deps.settings,
            tracked_deps.strategies_config,
            tracked_deps.strategy_key,
        )
        assert metadata["schema_version"] == "run-metadata-v1"
        assert metadata["app_version"]
        assert metadata["provider"] == {
            "name": "yfinance",
            "data_tier": "prototype",
        }
        assert metadata["universe_snapshot"]["snapshot_date"] == "2027-02-20"
        assert len(metadata["universe_snapshot"]["identity"]) == 64


class TestIdempotency:
    def test_two_runs_get_distinct_run_ids_and_no_duplicate_bars(
        self, deps, market_store
    ):
        first = run_daily(DailyRunOptions(as_of=AS_OF, is_dry_run=True), deps)
        second = run_daily(
            DailyRunOptions(as_of=AS_OF, is_dry_run=True, allow_same_day_rerun=True),
            deps,
        )

        assert first.run_id != second.run_id
        assert first.status == RunStatus.SUCCESS
        assert second.status == RunStatus.SUCCESS

        bars = market_store.read_bars(
            ["AAPL", "MSFT"], AS_OF - timedelta(days=400), AS_OF, AS_OF
        )
        # Re-running must not duplicate (symbol, date) rows.
        assert not bars.duplicated(subset=["symbol", "date"]).any()

    def test_two_runs_have_independent_step_histories(self, deps, state_store):
        first = run_daily(DailyRunOptions(as_of=AS_OF, is_dry_run=True), deps)
        second = run_daily(
            DailyRunOptions(as_of=AS_OF, is_dry_run=True, allow_same_day_rerun=True),
            deps,
        )

        with state_store.database.connect() as conn:
            first_steps = conn.execute(
                "SELECT count(*) FROM run_steps WHERE run_id = ?", [str(first.run_id)]
            ).fetchone()
            second_steps = conn.execute(
                "SELECT count(*) FROM run_steps WHERE run_id = ?", [str(second.run_id)]
            ).fetchone()
        # 7 pipeline steps (Issue #383 removed `7_notify`) + local postmortem,
        # MAE/MFE, the two retro (collect/evaluate) steps, and verdict tracking.
        assert first_steps == (11,)
        assert second_steps == (11,)


class TestFatalStepFailure:
    def test_price_fetch_failure_marks_run_failed_and_stops(
        self, settings, market_store, state_store, tmp_path
    ):
        universe = (_member("AAPL"),)
        empty_bars = pd.DataFrame(
            columns=["symbol", "date", "open", "high", "low", "close", "volume"]
        )
        failing_deps = DailyDependencies(
            data_provider=StubDataProvider(empty_bars),
            market_store=market_store,
            state_store=state_store,
            settings=settings,
            universe=universe,
            strategies_config=STRATEGIES_CONFIG,
            clock=FixedClock(AS_OF, _NOW),
            output_dir=str(tmp_path),
        )

        result = run_daily(DailyRunOptions(as_of=AS_OF, is_dry_run=True), failing_deps)

        assert result.status == RunStatus.FAILED
        assert result.exit_code == 1

        with state_store.database.connect() as conn:
            steps = conn.execute(
                "SELECT step FROM run_steps WHERE run_id = ?", [str(result.run_id)]
            ).fetchall()
            run_row = conn.execute(
                "SELECT status FROM runs WHERE run_id = ?", [str(result.run_id)]
            ).fetchone()
        assert [s[0] for s in steps] == ["1_prices"]
        assert run_row == ("failed",)

    def test_failed_run_can_be_followed_by_a_successful_rerun(
        self, settings, market_store, state_store, tmp_path
    ):
        universe = (_member("AAPL"), _member("MSFT"))
        empty_bars = pd.DataFrame(
            columns=["symbol", "date", "open", "high", "low", "close", "volume"]
        )
        failing_deps = DailyDependencies(
            data_provider=StubDataProvider(empty_bars),
            market_store=market_store,
            state_store=state_store,
            settings=settings,
            universe=universe,
            strategies_config=STRATEGIES_CONFIG,
            clock=FixedClock(AS_OF, _NOW),
            output_dir=str(tmp_path),
        )
        failed_result = run_daily(
            DailyRunOptions(as_of=AS_OF, is_dry_run=True), failing_deps
        )
        assert failed_result.status == RunStatus.FAILED

        working_deps = DailyDependencies(
            data_provider=StubDataProvider(_bars_for(["AAPL", "MSFT"], AS_OF)),
            market_store=market_store,
            state_store=state_store,
            settings=settings,
            universe=universe,
            strategies_config=STRATEGIES_CONFIG,
            clock=FixedClock(AS_OF, _NOW),
            output_dir=str(tmp_path),
        )
        retry_result = run_daily(
            DailyRunOptions(as_of=AS_OF, is_dry_run=True), working_deps
        )

        assert retry_result.status == RunStatus.SUCCESS
        assert retry_result.run_id != failed_result.run_id


class TestAsOfDefaulting:
    def test_missing_as_of_uses_latest_date_in_fetched_bars(self, deps):
        result = run_daily(DailyRunOptions(is_dry_run=True), deps)
        assert result.run_date == AS_OF - timedelta(days=1)

    def test_explicit_as_of_bypasses_the_closed_session_check(
        self, settings, market_store, state_store, tmp_path
    ):
        """#372: `--as-of` must not go through prefetch-based run_date resolution.

        `clock.now()` here is hours before 16:00 ET on `AS_OF` -- the closed-
        session gate would abort a live run at this instant -- but an explicit
        `--as-of` never prefetches at all (`options.as_of is not None` skips
        the whole branch in `run_daily()`), so `run_date` is exactly `AS_OF`
        regardless of the wall clock.
        """
        early_utc_morning = datetime(AS_OF.year, AS_OF.month, AS_OF.day, 9, tzinfo=UTC)
        deps = DailyDependencies(
            data_provider=StubDataProvider(_bars_for(["AAPL", "MSFT"], AS_OF)),
            market_store=market_store,
            state_store=state_store,
            settings=settings,
            universe=(_member("AAPL"), _member("MSFT")),
            strategies_config=STRATEGIES_CONFIG,
            clock=_FixedNowClock(early_utc_morning),
            edgar_client=None,
            output_dir=str(tmp_path / "reports"),
        )

        result = run_daily(DailyRunOptions(as_of=AS_OF, is_dry_run=True), deps)

        assert result.run_date == AS_OF
        assert result.status == RunStatus.SUCCESS


class TestRunDateResolvesOnlyClosedSessions:
    """#372: `run_date` must be a session that has closed, never merely fetched.

    Covers both defects the issue traces to: the wall-clock fallback on an
    empty/failed prefetch (defect 1), and booking a session before its 16:00
    ET close just because it is the newest fetched bar (defect 2). Each test
    starts from the `deps` fixture (bars/universe already wired) and swaps
    only `data_provider`/`clock` via `dataclasses.replace`.
    """

    def test_empty_prefetch_aborts_with_no_trading_day_and_writes_no_run(
        self, deps, state_store
    ):
        """A clean empty answer -- no `failures` -- is the legitimate stop."""
        broken_deps = replace(deps, data_provider=StubDataProvider(_empty_bars()))

        with pytest.raises(PreflightAbort) as exc_info:
            run_daily(DailyRunOptions(is_dry_run=True), broken_deps)

        assert exc_info.value.reason == "no_trading_day"
        with state_store.database.connect() as conn:
            count = conn.execute("SELECT count(*) FROM runs").fetchone()
        assert count == (0,)

    def test_empty_prefetch_with_failures_is_price_fetch_failed_not_no_trading_day(
        self, deps, state_store
    ):
        """The shape a real provider outage actually arrives in.

        `YFinanceProvider.get_daily_bars` never raises: a download exception
        and an empty provider response are both folded into an empty frame
        plus per-symbol `FetchFailure`s. Classifying that as `no_trading_day`
        would put it on `check_daily_complete.py`'s legitimate-stop
        whitelist, so a provider outage would leave the unattended job green
        with nothing analyzed -- the exact hole the `price_fetch_failed`
        split exists to close.
        """
        failures = tuple(
            FetchFailure(symbol=symbol, reason="no data returned", retryable=True)
            for symbol in ("AAPL", "MSFT")
        )
        broken_deps = replace(
            deps, data_provider=StubDataProvider(_empty_bars(), failures)
        )

        with pytest.raises(PreflightAbort) as exc_info:
            run_daily(DailyRunOptions(is_dry_run=True), broken_deps)

        assert exc_info.value.reason == "price_fetch_failed"
        assert "no data returned" in str(exc_info.value)
        with state_store.database.connect() as conn:
            count = conn.execute("SELECT count(*) FROM runs").fetchone()
        assert count == (0,)

    def test_prefetch_exception_aborts_with_price_fetch_failed_and_writes_no_run(
        self, deps, state_store
    ):
        """Issue #372: distinct from `no_trading_day` -- this is a real failure.

        An empty/all-still-open prefetch is a legitimate "no session has
        closed yet" stop; a prefetch that *raises* means the closed-session
        judgment could not even be attempted, e.g. a data-provider outage.
        Conflating the two let a transient network failure exit clean and
        leave the CI job green (`scripts/check_daily_complete.py`'s
        legitimate-stop whitelist depends on this distinction).
        """
        broken_deps = replace(
            deps, data_provider=_RaisingDataProvider(RuntimeError("network boom"))
        )

        with pytest.raises(PreflightAbort) as exc_info:
            run_daily(DailyRunOptions(is_dry_run=True), broken_deps)

        assert exc_info.value.reason == "price_fetch_failed"
        assert "network boom" in str(exc_info.value)
        with state_store.database.connect() as conn:
            count = conn.execute("SELECT count(*) FROM runs").fetchone()
        assert count == (0,)

    def test_every_fetched_bar_is_still_mid_session_aborts_with_no_trading_day(
        self, deps
    ):
        """Not just the newest date is open -- every fetched date is.

        Distinct from the "falls back to the prior close" case below: here
        there is no earlier closed session to fall back to at all (a single
        day's worth of bars, e.g. a symbol's very first session), so the
        closed-session set is empty and the run must abort.
        """
        session_date = date(2027, 3, 1)
        close_at = datetime.combine(
            session_date, time(16, 0), tzinfo=ZoneInfo("America/New_York")
        )
        still_open = close_at - timedelta(hours=1)
        only_todays_bars = pd.DataFrame(
            [
                {
                    "symbol": symbol,
                    "date": session_date,
                    "open": 100.0,
                    "high": 101.0,
                    "low": 99.0,
                    "close": 100.0,
                    "volume": 2_000_000,
                }
                for symbol in ("AAPL", "MSFT")
            ]
        )
        broken_deps = replace(
            deps,
            data_provider=StubDataProvider(only_todays_bars),
            clock=_FixedNowClock(still_open),
        )

        with pytest.raises(PreflightAbort) as exc_info:
            run_daily(DailyRunOptions(is_dry_run=True), broken_deps)

        assert exc_info.value.reason == "no_trading_day"

    def test_latest_bar_still_mid_session_falls_back_to_the_prior_close(self, deps):
        """The newest fetched bar's session has not closed yet (16:00 ET)."""
        session_date = date(2027, 3, 1)
        close_at = datetime.combine(
            session_date, time(16, 0), tzinfo=ZoneInfo("America/New_York")
        )
        just_before_close = close_at - timedelta(minutes=1)
        bars = _bars_for(["AAPL", "MSFT"], session_date + timedelta(days=1))
        mid_session_deps = replace(
            deps,
            data_provider=StubDataProvider(bars),
            clock=_FixedNowClock(just_before_close),
        )

        result = run_daily(DailyRunOptions(is_dry_run=True), mid_session_deps)

        assert result.run_date == session_date - timedelta(days=1)

    def test_latest_bar_exactly_at_the_close_boundary_is_used(self, deps):
        """16:00 ET exactly counts as closed (inclusive boundary)."""
        session_date = date(2027, 3, 1)
        close_at = datetime.combine(
            session_date, time(16, 0), tzinfo=ZoneInfo("America/New_York")
        )
        bars = _bars_for(["AAPL", "MSFT"], session_date + timedelta(days=1))
        at_close_deps = replace(
            deps, data_provider=StubDataProvider(bars), clock=_FixedNowClock(close_at)
        )

        result = run_daily(DailyRunOptions(is_dry_run=True), at_close_deps)

        assert result.run_date == session_date

    def test_latest_bar_just_after_the_close_is_used(self, deps):
        session_date = date(2027, 3, 1)
        close_at = datetime.combine(
            session_date, time(16, 0), tzinfo=ZoneInfo("America/New_York")
        )
        just_after_close = close_at + timedelta(minutes=1)
        bars = _bars_for(["AAPL", "MSFT"], session_date + timedelta(days=1))
        after_close_deps = replace(
            deps,
            data_provider=StubDataProvider(bars),
            clock=_FixedNowClock(just_after_close),
        )

        result = run_daily(DailyRunOptions(is_dry_run=True), after_close_deps)

        assert result.run_date == session_date

    def test_saturday_wall_clock_with_fridays_bar_resolves_to_friday(self, deps):
        """A delayed Saturday firing must not book Saturday.

        `today()` would say Saturday, but the newest *closed* session is
        Friday's -- the case the 2026-08-29 incident traces to.
        """
        friday = date(2027, 2, 26)
        assert friday.weekday() == _FRIDAY
        saturday_evening_et = datetime.combine(
            friday + timedelta(days=1),
            time(10, 0),
            tzinfo=ZoneInfo("America/New_York"),
        )
        bars = _bars_for(["AAPL", "MSFT"], friday + timedelta(days=1))
        saturday_deps = replace(
            deps,
            data_provider=StubDataProvider(bars),
            clock=_FixedNowClock(saturday_evening_et),
        )

        result = run_daily(DailyRunOptions(is_dry_run=True), saturday_deps)

        assert result.run_date == friday


class TestSameDayRerunGuard:
    """P8-118: abort before start_run when run_date already has a success run."""

    def test_existing_success_run_aborts_before_start_run(self, deps, state_store):
        existing_id = uuid4()
        # `StateStore.insert_run()` has no `report_path` parameter (only
        # `complete_run()` sets it), and this test's abort-message assertion
        # below needs one on the pre-existing row, so this seed stays raw SQL.
        with state_store.database.connect() as conn:
            conn.execute(
                "INSERT INTO runs (run_id, run_date, mode, config_hash, status, "
                "started_at, report_path) VALUES (?, ?, 'live', 'cfg', 'success', "
                "?, ?)",
                [
                    str(existing_id),
                    _LIVE_RUN_DATE,
                    datetime(2027, 2, 28, 15, 5, tzinfo=UTC),
                    "reports/2027-02-28/x.md",
                ],
            )

        with pytest.raises(PreflightAbort) as exc_info:
            run_daily(DailyRunOptions(is_dry_run=True), deps)

        assert exc_info.value.reason == "same_day_rerun"
        message = str(exc_info.value)
        assert str(existing_id) in message
        assert _LIVE_RUN_DATE.isoformat() in message
        assert "reports/2027-02-28/x.md" in message
        with state_store.database.connect() as conn:
            count = conn.execute("SELECT count(*) FROM runs").fetchone()
        assert count == (1,)

    def test_allow_same_day_rerun_bypasses_the_guard(self, deps, state_store):
        seed_run(
            state_store,
            uuid4(),
            _LIVE_RUN_DATE,
            started_at=datetime(2027, 2, 28, 15, 5, tzinfo=UTC),
        )

        result = run_daily(
            DailyRunOptions(is_dry_run=True, allow_same_day_rerun=True), deps
        )

        assert result.status == RunStatus.SUCCESS

    def test_only_failed_or_running_existing_runs_do_not_abort(self, deps, state_store):
        for status in (RunStatus.FAILED, RunStatus.RUNNING):
            seed_run(
                state_store,
                uuid4(),
                _LIVE_RUN_DATE,
                status=status,
                started_at=datetime(2027, 2, 28, 15, 5, tzinfo=UTC),
            )

        result = run_daily(DailyRunOptions(is_dry_run=True), deps)

        assert result.status == RunStatus.SUCCESS

    def test_historical_as_of_applies_the_same_guard(self, deps, state_store):
        seed_run(
            state_store,
            uuid4(),
            AS_OF,
            started_at=datetime(2027, 3, 1, 15, 5, tzinfo=UTC),
        )

        with pytest.raises(PreflightAbort):
            run_daily(DailyRunOptions(as_of=AS_OF, is_dry_run=True), deps)

    def test_a_market_holiday_evening_run_checks_the_resolved_run_date(
        self, deps, state_store
    ):
        # fetch_cutoff (deps.clock.today() == AS_OF) has no bar that day; the
        # latest prefetched bar resolves run_date to an earlier trading day
        # instead. _bars_for(symbols, X)'s last session is X - 1 day, so
        # asking for bars "as of" holiday_run_date + 1 day lands the latest
        # bar exactly on holiday_run_date.
        holiday_run_date = AS_OF - timedelta(days=5)
        seed_run(
            state_store,
            uuid4(),
            holiday_run_date,
            started_at=datetime(2027, 2, 24, 15, 5, tzinfo=UTC),
        )
        holiday_provider = StubDataProvider(
            _bars_for(["AAPL", "MSFT"], holiday_run_date + timedelta(days=1))
        )
        holiday_deps = replace(deps, data_provider=holiday_provider)

        with pytest.raises(PreflightAbort) as exc_info:
            run_daily(DailyRunOptions(is_dry_run=True), holiday_deps)

        assert holiday_run_date.isoformat() in str(exc_info.value)


class TestPriorAnalysisGapDetection:
    """#254: an earlier run's unfinished qualitative phase must not stay silent."""

    def test_a_completed_prior_analysis_is_not_reported(
        self, deps, state_store, capsys
    ):
        _archive_run(deps)

        result = run_daily(DailyRunOptions(), deps)

        assert result.status == RunStatus.SUCCESS
        assert _stored_gaps(state_store, result.run_id) is None
        assert _gap_lines(capsys) == []

    def test_a_missing_analysis_result_warns_on_stderr_and_is_recorded(
        self, deps, state_store, capsys
    ):
        prior_id, run_dir = _archive_run(deps, analyzed=False)

        result = run_daily(DailyRunOptions(), deps)

        # Fail-soft: the gap is an earlier day's fact, never today's abort.
        assert result.status == RunStatus.SUCCESS
        assert _stored_gaps(state_store, result.run_id) == [
            {
                "reason": "missing_analysis_result",
                "run_id": str(prior_id),
                "run_date": _PRIOR_RUN_DATE.isoformat(),
                "run_directory": str(run_dir),
            }
        ]
        # The tag has to sit at column zero to be greppable the way
        # PREFLIGHT_ABORT[...] is; a logging formatter would prefix it.
        lines = _gap_lines(capsys)
        assert len(lines) == 1
        assert lines[0] == (
            f"{_GAP_TAG} run_date={_PRIOR_RUN_DATE.isoformat()} "
            f"run_id={prior_id} run_directory={run_dir}"
        )

    def test_a_missing_analysis_result_adds_a_report_notice(self, deps):
        # #273: `notices` is the path an operator sees who only reads the
        # Markdown report, not stderr from an unattended run.
        _archive_run(deps, analyzed=False)

        result = run_daily(DailyRunOptions(), deps)

        assert result.brief is not None
        matching = [
            notice
            for notice in result.brief.notices
            if _PRIOR_RUN_DATE.isoformat() in notice
        ]
        assert len(matching) == 1
        assert "--allow-same-day-rerun" in matching[0]

    def test_a_completed_prior_analysis_adds_no_notice(self, deps):
        _archive_run(deps)

        result = run_daily(DailyRunOptions(), deps)

        assert result.brief is not None
        assert not any(
            "--allow-same-day-rerun" in notice for notice in result.brief.notices
        )

    def test_the_first_run_ever_reports_no_gap(self, deps, state_store, capsys):
        result = run_daily(DailyRunOptions(), deps)

        assert result.status == RunStatus.SUCCESS
        assert _stored_gaps(state_store, result.run_id) is None
        assert _gap_lines(capsys) == []

    @pytest.mark.parametrize(
        "analyzed_first",
        [True, False],
        ids=["analysis-in-the-earlier-sibling", "analysis-in-the-later-sibling"],
    )
    def test_a_same_day_sibling_holding_the_analysis_is_not_a_gap(
        self, deps, state_store, capsys, analyzed_first
    ):
        # A same-day double start (what #118 now blocks at the door) leaves two
        # directories for one date. The day's analysis lives in whichever
        # sibling answered, and which of the two started first does not change
        # that -- `find_incomplete_runs` keys `SAME_DAY_SUPERSEDED` on the
        # date, so both orders must come out the same.
        _archive_run(deps, analyzed=analyzed_first)
        _archive_run(deps, analyzed=not analyzed_first)

        result = run_daily(DailyRunOptions(), deps)

        assert result.status == RunStatus.SUCCESS
        assert _stored_gaps(state_store, result.run_id) is None
        assert _gap_lines(capsys) == []

    def test_failed_and_running_prior_runs_are_not_gaps(
        self, deps, state_store, capsys
    ):
        # A failed run handed the skill nothing, and a `running` row is work
        # that never reached its own terminal state -- neither is evidence
        # that a qualitative analysis went missing.
        _archive_run(deps, status="failed", analyzed=False)
        _archive_run(deps, status="running", analyzed=False)

        result = run_daily(DailyRunOptions(), deps)

        assert result.status == RunStatus.SUCCESS
        assert _stored_gaps(state_store, result.run_id) is None
        assert _gap_lines(capsys) == []

    def test_a_prior_run_that_exported_nothing_is_not_a_gap(
        self, deps, state_store, capsys
    ):
        # No analysis_input.json means no candidates or no text to analyse:
        # there was never an analysis owed for that day.
        _archive_run(deps, exported=False, analyzed=False)

        result = run_daily(DailyRunOptions(), deps)

        assert result.status == RunStatus.SUCCESS
        assert _stored_gaps(state_store, result.run_id) is None
        assert _gap_lines(capsys) == []

    def test_a_degraded_prior_run_is_still_checked(self, deps, state_store):
        prior_id, _ = _archive_run(deps, status="degraded", analyzed=False)

        result = run_daily(DailyRunOptions(), deps)

        gaps = _stored_gaps(state_store, result.run_id)
        assert [gap["run_id"] for gap in gaps] == [str(prior_id)]

    def test_todays_own_unanswered_directory_is_not_a_gap(
        self, deps, state_store, capsys
    ):
        # An `--allow-same-day-rerun` sibling of today has an export but no
        # answer yet: today's analysis is not due until this run's own skill
        # session ends.
        _archive_run(deps, run_date=_LIVE_RUN_DATE, analyzed=False)

        result = run_daily(DailyRunOptions(allow_same_day_rerun=True), deps)

        assert result.status == RunStatus.SUCCESS
        assert _stored_gaps(state_store, result.run_id) is None
        assert _gap_lines(capsys) == []

    def test_a_gap_exactly_at_the_lookback_boundary_is_still_reported(
        self, deps, state_store, capsys
    ):
        # The window is inclusive at its far edge: `since` is exactly
        # `run_date - _ANALYSIS_GAP_LOOKBACK_DAYS`, so that day still counts.
        # Expressed through the constant, so changing the number moves the
        # boundary rather than silently changing what "in the window" means.
        prior_id, _ = _archive_run(
            deps,
            run_date=_LIVE_RUN_DATE - timedelta(days=_ANALYSIS_GAP_LOOKBACK_DAYS),
            analyzed=False,
        )

        result = run_daily(DailyRunOptions(), deps)

        gaps = _stored_gaps(state_store, result.run_id)
        assert [gap["run_id"] for gap in gaps] == [str(prior_id)]
        assert len(_gap_lines(capsys)) == 1

    def test_a_gap_one_day_older_than_the_lookback_window_is_not_reported(
        self, deps, state_store, capsys
    ):
        # Bounded on purpose: a gap nobody backfilled must stop being
        # re-reported forever. `copilot-history incomplete` still lists it.
        _archive_run(
            deps,
            run_date=_LIVE_RUN_DATE - timedelta(days=_ANALYSIS_GAP_LOOKBACK_DAYS + 1),
            analyzed=False,
        )

        result = run_daily(DailyRunOptions(), deps)

        assert _stored_gaps(state_store, result.run_id) is None
        assert _gap_lines(capsys) == []

    def test_a_historical_replay_reports_nothing(self, deps, state_store, capsys):
        # A replay writes an analysis_input.json no skill session will answer;
        # counting replays would make the next live run record a false gap.
        _archive_run(deps, run_date=AS_OF - timedelta(days=1), analyzed=False)

        result = run_daily(DailyRunOptions(as_of=AS_OF), deps)

        assert result.status == RunStatus.SUCCESS
        assert _stored_gaps(state_store, result.run_id) is None
        assert _gap_lines(capsys) == []

    def test_a_dry_run_never_reports_its_own_throwaway_exports(
        self, deps, state_store, capsys
    ):
        # `--dry-run` gets its own database and `reports/dry_run` tree, but
        # step 6 still exports there and no skill answers it. Two dry runs a
        # few days apart must not make the second report the first.
        _archive_run(deps, analyzed=False)

        result = run_daily(DailyRunOptions(is_dry_run=True), deps)

        assert result.status == RunStatus.SUCCESS
        assert _stored_gaps(state_store, result.run_id) is None
        assert _gap_lines(capsys) == []

    def test_a_directory_left_by_a_replay_is_not_a_gap(self, deps, state_store, capsys):
        # The `--as-of` guard only silences the replay itself; the directory
        # it leaves behind outlives it, so the replay stamps its own export
        # and every later live run skips what it stamped.
        _, run_dir = _archive_run(deps, analyzed=False)
        (run_dir / "historical_replay.json").write_text("{}", encoding="utf-8")

        result = run_daily(DailyRunOptions(), deps)

        assert result.status == RunStatus.SUCCESS
        assert _stored_gaps(state_store, result.run_id) is None
        assert _gap_lines(capsys) == []

    def test_a_gap_just_inside_the_lookback_window_is_reported(
        self, deps, state_store, capsys
    ):
        prior_id, _ = _archive_run(
            deps,
            run_date=_LIVE_RUN_DATE - timedelta(days=_ANALYSIS_GAP_LOOKBACK_DAYS - 1),
            analyzed=False,
        )

        result = run_daily(DailyRunOptions(), deps)

        gaps = _stored_gaps(state_store, result.run_id)
        assert [gap["run_id"] for gap in gaps] == [str(prior_id)]
        assert len(_gap_lines(capsys)) == 1

    def test_an_unwritable_stderr_costs_the_line_but_not_the_record(
        self, deps, state_store, monkeypatch, caplog
    ):
        # `copilot-daily 2>&1 | head -20` closes the pipe early. The warning
        # is worth less than the run: a BrokenPipeError here would otherwise
        # kill the batch before `start_run` and leave no `runs` row at all.
        # The two exposure routes fail independently, so the durable one still
        # carries the gap the scan did find.
        prior_id, run_dir = _archive_run(deps, analyzed=False)
        monkeypatch.setattr(sys, "stderr", _BrokenStderr())

        with caplog.at_level(logging.ERROR):
            result = run_daily(DailyRunOptions(), deps)

        assert result.status == RunStatus.SUCCESS
        assert _stored_gaps(state_store, result.run_id) == [
            {
                "reason": "missing_analysis_result",
                "run_id": str(prior_id),
                "run_date": _PRIOR_RUN_DATE.isoformat(),
                "run_directory": str(run_dir),
            }
        ]
        assert any(
            "could not be written to stderr" in record.getMessage()
            for record in caplog.records
        )

    def test_a_failing_check_never_stops_todays_run(
        self, deps, state_store, monkeypatch, caplog, capsys
    ):
        _archive_run(deps, analyzed=False)

        def explode(*_args, **_kwargs):
            msg = "reports tree unreadable"
            raise RuntimeError(msg)

        monkeypatch.setattr(
            "swing_copilot.pipeline.daily_runner.find_incomplete_runs", explode
        )

        with caplog.at_level(logging.ERROR):
            result = run_daily(DailyRunOptions(), deps)

        assert result.status == RunStatus.SUCCESS
        assert _stored_gaps(state_store, result.run_id) is None
        assert _gap_lines(capsys) == []
        assert any(
            "prior-run analysis gap check failed" in record.getMessage()
            for record in caplog.records
        )


class TestUnexpectedStepException:
    def test_unexpected_exception_is_recorded_as_a_failed_step_not_a_crash(
        self, deps, state_store
    ):
        class ExplodingDataProvider:
            def get_daily_bars(self, symbols, start, end):
                del symbols, start, end
                msg = "boom"
                raise RuntimeError(msg)

            def get_latest_bars(self, symbols, as_of):
                del symbols, as_of
                msg = "boom"
                raise RuntimeError(msg)

        exploding_deps = DailyDependencies(
            data_provider=ExplodingDataProvider(),
            market_store=deps.market_store,
            state_store=deps.state_store,
            settings=deps.settings,
            universe=deps.universe,
            strategies_config=deps.strategies_config,
            clock=deps.clock,
        )

        result = run_daily(
            DailyRunOptions(as_of=AS_OF, is_dry_run=True), exploding_deps
        )

        assert result.status == RunStatus.FAILED
        with state_store.database.connect() as conn:
            row = conn.execute(
                "SELECT status, detail FROM run_steps WHERE run_id = ? AND step = '1_prices'",
                [str(result.run_id)],
            ).fetchone()
        assert row[0] == "failed"
        assert "boom" in row[1]


class TestTimeoutBudget:
    """NFR-03 run-timeout budget (`deps.monotonic`/`settings.schedule.timeout_minutes`)."""

    def test_no_breach_completes_normally(self, deps):
        deps_with_monotonic = replace(deps, monotonic=FakeMonotonic(0.0))

        result = run_daily(
            DailyRunOptions(as_of=AS_OF, is_dry_run=True), deps_with_monotonic
        )

        assert result.status == RunStatus.SUCCESS

    def test_mid_fundamentals_breach_degrades_but_the_run_still_completes(
        self, settings, market_store, state_store, tmp_path
    ):
        object.__setattr__(settings.schedule, "timeout_minutes", 1)  # 60s budget

        class SlowEdgarClient:
            def fetch_fundamentals(self, symbol, as_of):
                del as_of
                return [
                    FundamentalsRecord(
                        accession_no=f"acc-{symbol}",
                        symbol=symbol,
                        form="10-Q",
                        fiscal_period_end=AS_OF,
                        filed_at=datetime.combine(
                            AS_OF, datetime.min.time(), tzinfo=UTC
                        ),
                        revenue=1.0,
                        net_income=1.0,
                        fcf=1.0,
                        equity=1.0,
                        assets=2.0,
                        shares=1.0,
                        source_url="https://www.sec.gov/example",
                        fetched_at=datetime.combine(
                            AS_OF, datetime.min.time(), tzinfo=UTC
                        ),
                    )
                ]

            def fetch_filing_texts(
                self, symbol, form_types, *, as_of, since=None, limit=None
            ):
                del symbol, form_types, as_of, since, limit
                return []

        universe = (_member("AAPL"), _member("MSFT"))
        # run_started_at=0.0 -> deadline=60.0; index0(AAPL) check=10.0 (ok,
        # fetched); index1(MSFT) check=70.0 (breach, stops before fetching).
        deps_with_edgar = DailyDependencies(
            data_provider=StubDataProvider(_bars_for(["AAPL", "MSFT"], AS_OF)),
            market_store=market_store,
            state_store=state_store,
            settings=settings,
            universe=universe,
            strategies_config=STRATEGIES_CONFIG,
            clock=FixedClock(AS_OF, _NOW),
            edgar_client=SlowEdgarClient(),
            monotonic=FakeMonotonic(0.0, 10.0, 70.0),
            output_dir=str(tmp_path / "reports"),
        )

        result = run_daily(
            DailyRunOptions(as_of=AS_OF, is_dry_run=True), deps_with_edgar
        )

        assert result.status == RunStatus.DEGRADED
        assert result.exit_code == 0
        assert result.report_path is not None
        assert result.report_path.is_file()

        with state_store.database.connect() as conn:
            fundamentals_row = conn.execute(
                "SELECT status, detail FROM run_steps "
                "WHERE run_id = ? AND step = '2_fundamentals'",
                [str(result.run_id)],
            ).fetchone()
            text_row = conn.execute(
                "SELECT status, detail FROM run_steps "
                "WHERE run_id = ? AND step = '5_text'",
                [str(result.run_id)],
            ).fetchone()
            output_row = conn.execute(
                "SELECT status FROM run_steps WHERE run_id = ? AND step = '8_output'",
                [str(result.run_id)],
            ).fetchone()

        # Not fatal: fundamentals stopped early with a partial result, not a
        # failure -- one symbol was already fetched before the budget broke.
        assert fundamentals_row[0] == "success"
        assert "time budget exceeded after 1/2 symbols" in fundamentals_row[1]
        # Once the budget is exceeded it stays exceeded, so the next
        # network-bound step is skipped too -- this is what degrades the run.
        assert text_row[0] == "skipped"
        assert "time budget exceeded" in text_row[1]
        # Cheap/local steps still ran and produced a report.
        assert output_row[0] == "success"

    def test_pre_step_breach_skips_network_steps_but_the_run_still_completes(
        self, deps, state_store
    ):
        object.__setattr__(deps.settings.schedule, "timeout_minutes", 1)  # 60s budget
        # run_started_at=0.0 -> deadline=60.0; by the time steps 5/6 check,
        # "elapsed" is already far past the budget, even though nothing in
        # the fatal steps (1-4) itself was individually slow.
        deps_late = replace(deps, monotonic=FakeMonotonic(0.0, 999_999.0))

        result = run_daily(DailyRunOptions(as_of=AS_OF, is_dry_run=True), deps_late)

        assert result.status == RunStatus.DEGRADED
        assert result.exit_code == 0
        assert result.report_path is not None
        assert result.report_path.is_file()

        with state_store.database.connect() as conn:
            rows = dict(
                conn.execute(
                    "SELECT step, status FROM run_steps WHERE run_id = ?",
                    [str(result.run_id)],
                ).fetchall()
            )
        assert rows["5_text"] == "skipped"
        assert rows["6_analysis_export"] == "skipped"
        assert rows["8_output"] == "success"
