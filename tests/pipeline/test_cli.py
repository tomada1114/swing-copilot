"""Tests for the daily CLI/composition boundary (FR-12).

`_compose_dependencies` is exercised with `load_secrets`/`resolve_daily_universe`
monkeypatched to avoid any real network access or dependency on a
developer's local `.env` (never read directly in this suite).
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, date, datetime
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from swing_copilot.config import Secrets, Settings, load_settings, load_strategies
from swing_copilot.exceptions import ConfigError, PreflightAbort
from swing_copilot.models import DailyRunOptions, DataTier, RunMode, RunStatus
from swing_copilot.pipeline import daily_composition as daily_module
from swing_copilot.pipeline.daily import DailyDependencies, _paths_for_mode
from swing_copilot.pipeline.daily_composition import (
    _OUTCOME_FILE_ENV_VAR,
    _compose_dependencies,
    _finnhub_clients,
    _parse_args,
    _required_features,
    _run_daily_and_record_outcome,
    _RunOutcome,
    _write_outcome_file,
    main,
)
from swing_copilot.storage.database import DEFAULT_DB_PATH
from swing_copilot.universe import UniverseError, UniverseMember, UniverseResolution


def _shipped_settings() -> Settings:
    """Load the shipped `config/settings.yaml`."""
    return load_settings("config/settings.yaml")


class TestParseArgs:
    def test_defaults(self):
        options = _parse_args([])

        assert options == DailyRunOptions()

    def test_all_flags(self):
        options = _parse_args(
            [
                "--as-of",
                "2026-07-20",
                "--dry-run",
                "--skip-text",
                "--limit",
                "5",
                "--log-level",
                "DEBUG",
            ]
        )

        assert options == DailyRunOptions(
            as_of=date(2026, 7, 20),
            is_dry_run=True,
            skip_text=True,
            limit=5,
            log_level="DEBUG",
        )

    def test_strategy_defaults_to_default_and_accepts_named_strategy(self):
        assert _parse_args([]).strategy_key == "default"
        assert _parse_args(["--strategy", "minervini_stage2"]).strategy_key == (
            "minervini_stage2"
        )

    @pytest.mark.parametrize(
        ("argv", "expected_limit"),
        [
            pytest.param([], None, id="unset"),
            pytest.param(["--limit", "0"], 0, id="zero"),
            pytest.param(["--limit", "1"], 1, id="one"),
        ],
    )
    def test_limit_accepts_documented_candidate_scope_values(
        self, argv, expected_limit
    ):
        assert _parse_args(argv).limit == expected_limit

    def test_negative_limit_is_usage_error_before_composition(self, monkeypatch):
        monkeypatch.setattr(
            daily_module,
            "load_settings",
            lambda: pytest.fail("invalid CLI input must not load configuration"),
        )
        monkeypatch.setattr(
            daily_module,
            "_compose_dependencies",
            lambda *_args: pytest.fail("invalid CLI input must not compose I/O"),
        )

        with pytest.raises(SystemExit) as exc_info:
            main(["--limit", "-1"])

        assert exc_info.value.code == 2

    def test_log_level_is_optional_and_restricted_to_supported_levels(self):
        assert _parse_args([]).log_level is None
        assert _parse_args(["--log-level", "WARNING"]).log_level == "WARNING"
        with pytest.raises(SystemExit):
            _parse_args(["--log-level", "TRACE"])


class TestRequiredFeatures:
    def test_full_run_requires_edgar_finnhub_and_fred(self):
        features = _required_features(DailyRunOptions())

        assert features == {"edgar", "finnhub", "fred"}

    def test_skip_text_drops_finnhub_and_fred(self):
        features = _required_features(DailyRunOptions(skip_text=True))

        assert features == {"edgar"}


@pytest.fixture
def fake_universe(monkeypatch):
    members = [
        UniverseMember(
            symbol="AAPL",
            company_name="Apple Inc.",
            gics_sector="Information Technology",
            source_symbol="AAPL",
        )
    ]
    resolution = UniverseResolution(
        members=tuple(members), snapshot_date=date(2026, 7, 20)
    )
    monkeypatch.setattr(
        daily_module,
        "resolve_daily_universe",
        lambda *_args, **_kwargs: resolution,
    )
    # edgar.set_identity() sets the real EDGAR_IDENTITY environment variable
    # (see tests/data/test_edgar.py) -- never let a composition-root test
    # actually invoke it, or the leak pollutes every later test in this
    # process.
    monkeypatch.setattr(
        "swing_copilot.data.edgar.edgar.set_identity", lambda _identity: None
    )
    return members


class TestFinnhubClients:
    """Issue #263: one API key is one metered account, so one throttle."""

    def test_both_clients_share_one_account_wide_throttle(self):
        # Asserted on the throttle object's identity because sharing *is* the
        # contract here: the two clients' combined issue rate can only be
        # bounded by one budget, and the composition root is where a second
        # `MinIntervalThrottle` would silently reappear. The rate behavior it
        # buys is fixed in `tests/test_ratelimit.py`.
        news_client, earnings_client = _finnhub_clients(
            Secrets(finnhub_api_key="finnhub-key"), DailyRunOptions()
        )

        assert news_client is not None
        assert earnings_client is not None
        assert news_client._throttle is earnings_client._throttle  # noqa: SLF001

    def test_skip_text_drops_the_news_client_but_keeps_earnings(self):
        news_client, earnings_client = _finnhub_clients(
            Secrets(finnhub_api_key="finnhub-key"),
            DailyRunOptions(skip_text=True),
        )

        assert news_client is None
        assert earnings_client is not None

    def test_no_api_key_builds_no_client_at_all(self):
        assert _finnhub_clients(Secrets(), DailyRunOptions()) == (None, None)


@pytest.mark.usefixtures("fake_universe")
class TestComposeDependencies:
    @pytest.mark.usefixtures("fake_universe")
    def test_unknown_strategy_fails_before_secret_or_network_composition(
        self, monkeypatch
    ):
        settings = _shipped_settings()
        strategies = load_strategies("config/strategies.yaml")
        monkeypatch.setattr(
            daily_module,
            "load_secrets",
            lambda: pytest.fail("unknown strategy must fail before loading secrets"),
        )

        with pytest.raises(ConfigError, match="Unknown strategy 'missing'"):
            _compose_dependencies(
                DailyRunOptions(strategy_key="missing"), settings, strategies
            )

    def test_missing_required_secret_raises_config_error(self, monkeypatch):
        monkeypatch.setattr(daily_module, "load_secrets", Secrets)
        settings = _shipped_settings()
        strategies = load_strategies("config/strategies.yaml")

        with pytest.raises(ConfigError, match="edgar_identity"):
            _compose_dependencies(DailyRunOptions(skip_text=True), settings, strategies)

    def test_skip_text_leaves_text_and_calendar_clients_none(
        self, monkeypatch, tmp_path
    ):
        monkeypatch.setattr(
            daily_module,
            "load_secrets",
            lambda: Secrets(edgar_identity="Test test@example.com"),
        )
        settings = _shipped_settings()
        strategies = load_strategies("config/strategies.yaml")
        monkeypatch.chdir(tmp_path)

        deps = _compose_dependencies(
            DailyRunOptions(skip_text=True), settings, strategies
        )

        assert isinstance(deps, DailyDependencies)
        assert deps.edgar_client is not None
        assert deps.news_client is None
        assert deps.earnings_client is None
        assert deps.calendar_client is None

    def test_explicit_as_of_uses_the_point_in_time_universe_resolver(
        self, monkeypatch, tmp_path
    ):
        settings = _shipped_settings()
        strategies = load_strategies("config/strategies.yaml")
        expected_as_of = date(2026, 7, 20)
        captured = {}

        def _resolve(as_of, state_store, **kwargs):
            captured["as_of"] = as_of
            captured["state_store"] = state_store
            captured.update(kwargs)
            return UniverseResolution(
                members=(
                    UniverseMember(
                        symbol="PIT",
                        company_name="Point in time Corp.",
                        gics_sector="Industrials",
                        source_symbol="PIT",
                    ),
                ),
                snapshot_date=expected_as_of,
            )

        monkeypatch.setattr(daily_module, "resolve_daily_universe", _resolve)
        monkeypatch.setattr(
            daily_module,
            "load_secrets",
            lambda: Secrets(edgar_identity="Test test@example.com"),
        )
        monkeypatch.chdir(tmp_path)

        deps = _compose_dependencies(
            DailyRunOptions(as_of=expected_as_of, skip_text=True),
            settings,
            strategies,
        )

        assert captured["as_of"] == expected_as_of
        assert captured["state_store"] is deps.state_store
        assert captured["is_historical"] is True
        assert (
            captured["refresh_interval_days"] == settings.universe.refresh_interval_days
        )
        assert [member.symbol for member in deps.universe] == ["PIT"]
        assert deps.universe_snapshot_date == expected_as_of

    def test_configured_secrets_wire_up_the_matching_clients(
        self, monkeypatch, tmp_path
    ):
        monkeypatch.setattr(
            daily_module,
            "load_secrets",
            lambda: Secrets(
                edgar_identity="Test test@example.com",
                finnhub_api_key="finnhub-key",
                fred_api_key="fred-key",
            ),
        )
        settings = _shipped_settings()
        strategies = load_strategies("config/strategies.yaml")
        monkeypatch.chdir(tmp_path)

        deps = _compose_dependencies(DailyRunOptions(), settings, strategies)

        assert deps.news_client is not None
        assert deps.earnings_client is not None
        assert deps.calendar_client is not None

    def test_dry_run_composes_an_isolated_db_and_report_dir(
        self, monkeypatch, tmp_path
    ):
        monkeypatch.setattr(
            daily_module,
            "load_secrets",
            lambda: Secrets(edgar_identity="Test test@example.com"),
        )
        settings = _shipped_settings()
        strategies = load_strategies("config/strategies.yaml")
        monkeypatch.chdir(tmp_path)

        deps = _compose_dependencies(
            DailyRunOptions(is_dry_run=True, skip_text=True),
            settings,
            strategies,
        )

        assert deps.output_dir == "reports/dry_run"
        assert deps.market_store._database.db_path == Path(  # noqa: SLF001
            "data/copilot_dry_run.duckdb"
        )

    def test_live_run_composes_the_default_db_and_report_dir(
        self, monkeypatch, tmp_path
    ):
        monkeypatch.setattr(
            daily_module,
            "load_secrets",
            lambda: Secrets(edgar_identity="Test test@example.com"),
        )
        settings = _shipped_settings()
        strategies = load_strategies("config/strategies.yaml")
        monkeypatch.chdir(tmp_path)

        deps = _compose_dependencies(
            DailyRunOptions(skip_text=True), settings, strategies
        )

        assert deps.output_dir == "reports"
        assert deps.market_store._database.db_path == DEFAULT_DB_PATH  # noqa: SLF001


class TestPathsForMode:
    def test_live_mode_uses_the_default_db_and_reports_dir(self):
        db_path, output_dir = _paths_for_mode(RunMode.LIVE)

        assert db_path == DEFAULT_DB_PATH
        assert output_dir == "reports"

    def test_dry_run_mode_uses_an_isolated_db_and_reports_subdir(self):
        db_path, output_dir = _paths_for_mode(RunMode.DRY_RUN)

        assert db_path == Path("data/copilot_dry_run.duckdb")
        assert output_dir == "reports/dry_run"
        assert db_path != DEFAULT_DB_PATH


class TestMain:
    def test_historical_missing_snapshot_exits_before_price_provider(
        self, monkeypatch, tmp_path
    ):
        settings = _shipped_settings()
        strategies = load_strategies("config/strategies.yaml")

        def _missing_snapshot(*_args, **_kwargs):
            msg = "No persisted universe snapshot is available at or before 2026-07-20"
            raise UniverseError(msg)

        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(
            daily_module,
            "load_secrets",
            lambda: Secrets(edgar_identity="Test test@example.com"),
        )
        monkeypatch.setattr(daily_module, "load_settings", lambda: settings)
        monkeypatch.setattr(daily_module, "load_strategies", lambda: strategies)
        monkeypatch.setattr(daily_module, "resolve_daily_universe", _missing_snapshot)
        monkeypatch.setattr(
            daily_module,
            "YFinanceProvider",
            lambda: pytest.fail(
                "price provider must not be composed without a snapshot"
            ),
        )

        with pytest.raises(SystemExit, match="No persisted universe snapshot"):
            main(["--as-of", "2026-07-20", "--skip-text"])

    def test_parses_args_composes_and_exits_with_run_result_code(self, monkeypatch):
        calls = {}

        def fake_compose(options, settings, strategies):
            calls["options"] = options
            return "fake-deps"

        def fake_run_daily(options, deps):
            calls["run_daily"] = (options, deps)

            class _Result:
                exit_code = 7
                brief = None
                run_id = uuid4()
                status = RunStatus.FAILED
                report_path = None
                analysis_input_path = None
                provider_name = "yfinance"
                data_tier = DataTier.PROTOTYPE
                missing_sources = ()

            return _Result()

        monkeypatch.setattr(daily_module, "load_secrets", Secrets)
        monkeypatch.setattr(daily_module, "load_settings", lambda: "fake-settings")
        monkeypatch.setattr(daily_module, "load_strategies", lambda: "fake-strategies")
        monkeypatch.setattr(daily_module, "_compose_dependencies", fake_compose)
        monkeypatch.setattr(daily_module, "run_daily", fake_run_daily)

        with pytest.raises(SystemExit) as exc_info:
            main(["--dry-run"])

        assert exc_info.value.code == 7
        assert calls["options"].is_dry_run is True
        assert calls["run_daily"] == (calls["options"], "fake-deps")

    def test_renders_brief_with_report_path(self, monkeypatch, capsys):
        brief = object()
        report_path = Path("reports/2026-07-22/report.md")
        analysis_input_path = Path("reports/2026-07-22/analysis_input.json")
        calls = {}

        monkeypatch.setattr(daily_module, "load_secrets", Secrets)
        monkeypatch.setattr(daily_module, "load_settings", lambda: "fake-settings")
        monkeypatch.setattr(daily_module, "load_strategies", lambda: "fake-strategies")
        monkeypatch.setattr(
            daily_module, "_compose_dependencies", lambda *_args: "fake-deps"
        )
        monkeypatch.setattr(
            daily_module,
            "run_daily",
            lambda *_args: SimpleNamespace(
                exit_code=0,
                brief=brief,
                status=RunStatus.SUCCESS,
                report_path=report_path,
                analysis_input_path=analysis_input_path,
                run_id=uuid4(),
                provider_name="yfinance",
                data_tier=DataTier.PROTOTYPE,
                missing_sources=(),
            ),
        )

        def fake_render_terminal(brief_arg, status, **kwargs):
            calls["render"] = (brief_arg, status, kwargs)
            return "terminal output\n"

        monkeypatch.setattr(daily_module, "render_terminal", fake_render_terminal)
        monkeypatch.setattr(
            daily_module,
            "render_run_summary",
            lambda *_args, **_kwargs: "run summary\n",
        )

        with pytest.raises(SystemExit) as exc_info:
            main([])

        assert exc_info.value.code == 0
        assert capsys.readouterr().out == "terminal output\nrun summary\n"
        assert calls["render"] == (
            brief,
            RunStatus.SUCCESS,
            {
                "width": 120,
                "color": False,
            },
        )


class TestPreflightAbortStderrContract:
    """The tag `swing-daily` branches on must survive refactors (Issue #193).

    An abort exits `2`, so the reason has to be readable from stderr's first
    line without parsing prose. `run_daily` raises for the same-day rerun guard.
    """

    @staticmethod
    def _stub_composition(monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(daily_module, "load_secrets", Secrets)
        monkeypatch.setattr(daily_module, "load_settings", lambda: "fake-settings")
        monkeypatch.setattr(daily_module, "load_strategies", lambda: "fake-strategies")
        monkeypatch.setattr(
            daily_module, "_compose_dependencies", lambda *_args: "fake-deps"
        )
        monkeypatch.setattr(daily_module, "run_daily", lambda *_args, **_kwargs: None)

    @pytest.mark.parametrize(
        "reason", ["same_day_rerun", "no_trading_day", "price_fetch_failed"]
    )
    def test_the_first_stderr_line_carries_the_tagged_reason(
        self, monkeypatch, capsys, reason
    ):
        self._stub_composition(monkeypatch)

        message = "中止した理由の説明"

        def _abort(*_args, **_kwargs):
            raise PreflightAbort(message, reason=reason)

        monkeypatch.setattr(daily_module, "run_daily", _abort)

        with pytest.raises(SystemExit) as exc_info:
            main([])

        assert exc_info.value.code == 2
        first_line = capsys.readouterr().err.splitlines()[0]
        assert first_line == f"PREFLIGHT_ABORT[{reason}]: 中止した理由の説明"


class _FixedClock:
    """A minimal `Clock` stand-in: a fixed `now()`/`today()`, nothing else."""

    def __init__(self, instant: datetime) -> None:
        self._instant = instant

    def now(self) -> datetime:
        return self._instant

    def today(self):
        return self._instant.date()


class TestWriteOutcomeFileNoop:
    """`_write_outcome_file(None, ...)` is a deliberate no-op (Issue #372).

    Kept even though every current caller already guards on
    `options.outcome_file is not None` before calling in: the guard exists to
    avoid touching `deps.clock` needlessly, not to be the only place this
    contract is enforced, so the function's own `None` branch stays covered
    directly.
    """

    def test_none_outcome_file_writes_nothing(self, tmp_path):
        outcome = _RunOutcome(
            outcome="success",
            reason=None,
            run_id="r1",
            run_date="2027-03-01",
            candidates=0,
            started_at=datetime(2027, 3, 1, tzinfo=UTC),
            finished_at=datetime(2027, 3, 1, tzinfo=UTC),
        )

        _write_outcome_file(None, outcome)

        assert list(tmp_path.iterdir()) == []


class TestOutcomeFile:
    """Issue #372: `copilot-daily` records its terminal state on every exit.

    `main()` is driven end-to-end with `load_secrets`/`load_settings`/
    `load_strategies`/`_compose_dependencies`/`run_daily` stubbed, mirroring
    `TestPreflightAbortStderrContract`. `brief=None` on every fake result
    keeps `render_terminal` (the real one; unmocked here) out of the picture,
    since only the outcome file is under test.
    """

    _STARTED_AT = datetime(2027, 3, 2, 11, 0, tzinfo=UTC)
    _FINISHED_AT = datetime(2027, 3, 2, 12, 0, tzinfo=UTC)

    def _stub(self, monkeypatch, *, run_daily):
        monkeypatch.setattr(daily_module, "load_secrets", Secrets)
        monkeypatch.setattr(daily_module, "load_settings", lambda: "fake-settings")
        monkeypatch.setattr(daily_module, "load_strategies", lambda: "fake-strategies")
        monkeypatch.setattr(
            daily_module, "SystemClock", lambda: _FixedClock(self._STARTED_AT)
        )
        monkeypatch.setattr(
            daily_module,
            "_compose_dependencies",
            lambda *_args: SimpleNamespace(clock=_FixedClock(self._FINISHED_AT)),
        )
        monkeypatch.setattr(daily_module, "run_daily", run_daily)

    def _fake_result(self, status):
        # `brief=None` keeps the real (unmocked) `render_terminal` out of the
        # picture in these tests -- it needs a genuine `DailyBrief`, and only
        # the outcome file is under test here. `candidates` in the outcome
        # file is therefore `None`; `TestOutcomeCandidateCount` below covers
        # the counting logic directly.
        return SimpleNamespace(
            exit_code=0 if status is not RunStatus.FAILED else 1,
            brief=None,
            status=status,
            run_id=uuid4(),
            run_date=date(2027, 3, 1),
            report_path=None,
            analysis_input_path=None,
            provider_name="yfinance",
            data_tier=DataTier.PROTOTYPE,
            missing_sources=(),
        )

    @pytest.mark.parametrize(
        "status", [RunStatus.SUCCESS, RunStatus.DEGRADED, RunStatus.FAILED]
    )
    def test_every_terminal_status_writes_the_outcome_file(
        self, monkeypatch, tmp_path, status
    ):
        outcome_file = tmp_path / "outcome.json"
        result = self._fake_result(status)
        self._stub(monkeypatch, run_daily=lambda *_a, **_k: result)

        with pytest.raises(SystemExit):
            main(["--outcome-file", str(outcome_file)])

        payload = json.loads(outcome_file.read_text(encoding="utf-8"))
        assert payload == {
            "outcome": status.value,
            "reason": None,
            "run_id": str(result.run_id),
            "run_date": "2027-03-01",
            "candidates": None,
            "started_at": self._STARTED_AT.isoformat(),
            "finished_at": self._FINISHED_AT.isoformat(),
        }

    @pytest.mark.parametrize(
        "reason", ["no_trading_day", "price_fetch_failed", "same_day_rerun"]
    )
    def test_preflight_abort_writes_the_outcome_file(
        self, monkeypatch, tmp_path, reason
    ):
        """The whole point of #372: the abort path must not go unrecorded.

        Parametrized over every `PreflightAbortReason` (Issue #372 added
        `price_fetch_failed`): the outcome file must faithfully record
        whichever reason fired, since `scripts/check_daily_complete.py`'s
        legitimate-stop whitelist depends on that exact value surviving here.
        """
        outcome_file = tmp_path / "outcome.json"

        def _abort(*_args, **_kwargs):
            message = "中止した"
            raise PreflightAbort(message, reason=reason)

        self._stub(monkeypatch, run_daily=_abort)

        with pytest.raises(SystemExit) as exc_info:
            main(["--outcome-file", str(outcome_file)])

        assert exc_info.value.code == 2
        payload = json.loads(outcome_file.read_text(encoding="utf-8"))
        assert payload == {
            "outcome": "preflight_abort",
            "reason": reason,
            "run_id": None,
            "run_date": None,
            "candidates": None,
            "started_at": self._STARTED_AT.isoformat(),
            "finished_at": self._FINISHED_AT.isoformat(),
        }

    def test_no_outcome_file_flag_writes_nothing(self, monkeypatch, tmp_path):
        result = self._fake_result(RunStatus.SUCCESS)
        self._stub(monkeypatch, run_daily=lambda *_a, **_k: result)
        monkeypatch.delenv(_OUTCOME_FILE_ENV_VAR, raising=False)

        with pytest.raises(SystemExit):
            main([])

        assert list(tmp_path.iterdir()) == []

    def test_a_write_failure_is_fail_soft(self, monkeypatch, tmp_path, caplog):
        """A missing destination directory must not crash a real run."""
        outcome_file = tmp_path / "missing-dir" / "outcome.json"
        result = self._fake_result(RunStatus.SUCCESS)
        self._stub(monkeypatch, run_daily=lambda *_a, **_k: result)

        with caplog.at_level(logging.ERROR), pytest.raises(SystemExit) as exc_info:
            main(["--outcome-file", str(outcome_file)])

        assert exc_info.value.code == 0
        assert not outcome_file.exists()
        assert "outcome file" in caplog.text


class TestOutcomeCandidateCount:
    """`candidates` in the outcome file counts `result.brief.candidates`.

    Calls `_run_daily_and_record_outcome` directly (bypassing `main()`'s
    terminal rendering, which needs a genuine `DailyBrief`) since only the
    counting logic is under test here.
    """

    def test_candidates_is_the_brief_candidate_count(self, monkeypatch, tmp_path):
        outcome_file = tmp_path / "outcome.json"
        result = SimpleNamespace(
            status=RunStatus.SUCCESS,
            run_id=uuid4(),
            run_date=date(2027, 3, 1),
            brief=SimpleNamespace(candidates=(object(), object(), object())),
        )
        deps = SimpleNamespace(clock=_FixedClock(datetime(2027, 3, 2, tzinfo=UTC)))
        monkeypatch.setattr(daily_module, "run_daily", lambda *_a, **_k: result)

        _run_daily_and_record_outcome(
            DailyRunOptions(outcome_file=outcome_file),
            deps,  # type: ignore[arg-type]
            datetime(2027, 3, 2, tzinfo=UTC),
        )

        payload = json.loads(outcome_file.read_text(encoding="utf-8"))
        assert payload["candidates"] == 3

    def test_no_brief_leaves_candidates_null(self, monkeypatch, tmp_path):
        outcome_file = tmp_path / "outcome.json"
        result = SimpleNamespace(
            status=RunStatus.FAILED,
            run_id=uuid4(),
            run_date=date(2027, 3, 1),
            brief=None,
        )
        deps = SimpleNamespace(clock=_FixedClock(datetime(2027, 3, 2, tzinfo=UTC)))
        monkeypatch.setattr(daily_module, "run_daily", lambda *_a, **_k: result)

        _run_daily_and_record_outcome(
            DailyRunOptions(outcome_file=outcome_file),
            deps,  # type: ignore[arg-type]
            datetime(2027, 3, 2, tzinfo=UTC),
        )

        payload = json.loads(outcome_file.read_text(encoding="utf-8"))
        assert payload["candidates"] is None


class TestOutcomeFileEnvironmentFallback:
    """`--outcome-file` wins; `COPILOT_DAILY_OUTCOME_FILE` is only a fallback."""

    def test_environment_variable_is_used_when_the_flag_is_absent(
        self, monkeypatch, tmp_path
    ):
        env_path = tmp_path / "from-env" / "outcome.json"
        monkeypatch.setenv(_OUTCOME_FILE_ENV_VAR, str(env_path))

        options = _parse_args([])

        assert options.outcome_file == env_path

    def test_explicit_flag_overrides_the_environment_variable(
        self, monkeypatch, tmp_path
    ):
        env_path = tmp_path / "from-env" / "outcome.json"
        flag_path = tmp_path / "from-flag" / "outcome.json"
        monkeypatch.setenv(_OUTCOME_FILE_ENV_VAR, str(env_path))

        options = _parse_args(["--outcome-file", str(flag_path)])

        assert options.outcome_file == flag_path

    def test_neither_set_writes_nothing(self, monkeypatch):
        monkeypatch.delenv(_OUTCOME_FILE_ENV_VAR, raising=False)

        options = _parse_args([])

        assert options.outcome_file is None
