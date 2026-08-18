"""Tests for the daily CLI/composition boundary (FR-12).

`_compose_dependencies` is exercised with `load_secrets`/`resolve_daily_universe`
monkeypatched to avoid any real network access or dependency on a
developer's local `.env` (never read directly in this suite).
"""

from __future__ import annotations

import logging
from datetime import UTC, date, datetime
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest

from swing_copilot.config import Secrets, load_settings, load_strategies
from swing_copilot.exceptions import ConfigError, PreflightAbort
from swing_copilot.models import DailyRunOptions, DataTier, Position, RunMode, RunStatus
from swing_copilot.pipeline import daily_composition as daily_module
from swing_copilot.pipeline.daily import DailyDependencies, _paths_for_mode
from swing_copilot.pipeline.daily_composition import (
    _compose_dependencies,
    _configure_logging,
    _parse_args,
    _preflight,
    _required_features,
    main,
)
from swing_copilot.storage.database import DEFAULT_DB_PATH
from swing_copilot.universe import UniverseError, UniverseMember, UniverseResolution


def _make_status_error(message: str) -> httpx.HTTPStatusError:
    request = httpx.Request("GET", "https://example.com")
    response = httpx.Response(401, request=request)
    return httpx.HTTPStatusError(message, request=request, response=response)


def _isolated_secrets(**overrides: str) -> Secrets:
    """Build `Secrets` isolated from any real `.env` a developer has locally."""
    return Secrets(_env_file=None, **overrides)  # type: ignore[call-arg]


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
        settings = load_settings("config/settings.yaml")

        features = _required_features(DailyRunOptions(), settings)

        assert features == {"edgar", "finnhub", "fred"}

    def test_skip_text_drops_finnhub_and_fred(self):
        settings = load_settings("config/settings.yaml")

        features = _required_features(DailyRunOptions(skip_text=True), settings)

        assert features == {"edgar"}

    def test_notification_enabled_adds_discord(self):
        settings = load_settings("config/settings.yaml")
        object.__setattr__(settings.notification, "enabled", True)

        features = _required_features(DailyRunOptions(), settings)

        assert "discord" in features


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


@pytest.mark.usefixtures("fake_universe")
class TestComposeDependencies:
    @pytest.mark.usefixtures("fake_universe")
    def test_unknown_strategy_fails_before_secret_or_network_composition(
        self, monkeypatch
    ):
        settings = load_settings("config/settings.yaml")
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
        monkeypatch.setattr(daily_module, "load_secrets", _isolated_secrets)
        settings = load_settings("config/settings.yaml")
        strategies = load_strategies("config/strategies.yaml")

        with pytest.raises(ConfigError, match="edgar_identity"):
            _compose_dependencies(DailyRunOptions(skip_text=True), settings, strategies)

    def test_skip_text_leaves_text_and_calendar_clients_none(self, monkeypatch):
        monkeypatch.setattr(
            daily_module,
            "load_secrets",
            lambda: _isolated_secrets(edgar_identity="Test test@example.com"),
        )
        settings = load_settings("config/settings.yaml")
        strategies = load_strategies("config/strategies.yaml")

        deps = _compose_dependencies(
            DailyRunOptions(skip_text=True), settings, strategies
        )

        assert isinstance(deps, DailyDependencies)
        assert deps.edgar_client is not None
        assert deps.news_client is None
        assert deps.earnings_client is None
        assert deps.calendar_client is None
        assert deps.notifier is None

    def test_explicit_as_of_uses_the_point_in_time_universe_resolver(self, monkeypatch):
        settings = load_settings("config/settings.yaml")
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
            lambda: _isolated_secrets(edgar_identity="Test test@example.com"),
        )

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

    def test_configured_secrets_wire_up_the_matching_clients(self, monkeypatch):
        monkeypatch.setattr(
            daily_module,
            "load_secrets",
            lambda: _isolated_secrets(
                edgar_identity="Test test@example.com",
                finnhub_api_key="finnhub-key",
                fred_api_key="fred-key",
            ),
        )
        settings = load_settings("config/settings.yaml")
        strategies = load_strategies("config/strategies.yaml")

        deps = _compose_dependencies(DailyRunOptions(), settings, strategies)

        assert deps.news_client is not None
        assert deps.earnings_client is not None
        assert deps.calendar_client is not None

    def test_notification_enabled_without_webhook_is_a_fail_fast_config_error(
        self, monkeypatch
    ):
        # Feature-gated secret validation (D7): enabling a feature without its
        # secret is a configuration error to fix, not something to silently
        # degrade around.
        monkeypatch.setattr(
            daily_module,
            "load_secrets",
            lambda: _isolated_secrets(edgar_identity="Test test@example.com"),
        )
        settings = load_settings("config/settings.yaml")
        object.__setattr__(settings.notification, "enabled", True)
        strategies = load_strategies("config/strategies.yaml")

        with pytest.raises(ConfigError, match="discord_webhook_url"):
            _compose_dependencies(DailyRunOptions(skip_text=True), settings, strategies)

    def test_dry_run_composes_an_isolated_db_and_report_dir(
        self, monkeypatch, tmp_path
    ):
        monkeypatch.setattr(
            daily_module,
            "load_secrets",
            lambda: _isolated_secrets(edgar_identity="Test test@example.com"),
        )
        settings = load_settings("config/settings.yaml")
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
            lambda: _isolated_secrets(edgar_identity="Test test@example.com"),
        )
        settings = load_settings("config/settings.yaml")
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


def _closed_position(**overrides: object) -> Position:
    fields: dict[str, object] = {
        "position_id": uuid4(),
        "symbol": "AAPL",
        "is_paper": True,
        "entry_date": date(2027, 1, 1),
        "entry_price": 100.0,
        "shares": 10,
        "status": "closed",
        "stop_price": 95.0,
        "close_date": date(2027, 1, 10),
        "close_at": datetime(2027, 1, 10, 20, 0, tzinfo=UTC),
        "close_price": 105.0,
        "exit_reason": "target",
    }
    fields.update(overrides)
    return Position(**fields)  # type: ignore[arg-type]


def _equity_settings(settings, account_equity_usd):
    return settings.model_copy(
        update={
            "risk": settings.risk.model_copy(
                update={"account_equity_usd": account_equity_usd}
            )
        }
    )


def _preflight_deps(state_store, settings):
    """A minimal stand-in for `DailyDependencies`: `_preflight` reads only these two."""
    return SimpleNamespace(state_store=state_store, settings=settings)


class TestPreflight:
    """P8-117: abort before any state is written when continuing is pointless."""

    def test_equity_set_neither_warns_nor_aborts(self, settings, state_store, caplog):
        state_store.upsert_position(_closed_position())
        deps = _preflight_deps(state_store, _equity_settings(settings, 100_000.0))

        with caplog.at_level(logging.WARNING):
            _preflight(deps, DailyRunOptions())

        assert caplog.records == []

    def test_equity_unset_zero_closed_warns_and_continues(
        self, settings, state_store, caplog
    ):
        deps = _preflight_deps(state_store, _equity_settings(settings, None))

        with caplog.at_level(logging.WARNING):
            _preflight(deps, DailyRunOptions())

        assert any("account_equity_usd" in record.message for record in caplog.records)

    def test_equity_unset_one_closed_aborts(self, settings, state_store):
        state_store.upsert_position(_closed_position())
        deps = _preflight_deps(state_store, _equity_settings(settings, None))

        with pytest.raises(
            PreflightAbort, match=r"risk\.account_equity_usd"
        ) as exc_info:
            _preflight(deps, DailyRunOptions())

        assert "CIRCUIT_BREAKER_HALTED" in str(exc_info.value)
        # The consuming skill branches on this tag: without it, a config
        # problem is indistinguishable from "already analyzed today".
        assert exc_info.value.reason == "account_equity_unset"

    def test_a_close_price_of_none_still_counts_as_one_closed_position(
        self, settings, state_store
    ):
        state_store.upsert_position(_closed_position(close_price=None))
        deps = _preflight_deps(state_store, _equity_settings(settings, None))

        with pytest.raises(PreflightAbort):
            _preflight(deps, DailyRunOptions())

    def test_dry_run_applies_the_same_rules(self, settings, state_store):
        state_store.upsert_position(_closed_position())
        deps = _preflight_deps(state_store, _equity_settings(settings, None))

        with pytest.raises(PreflightAbort):
            _preflight(deps, DailyRunOptions(is_dry_run=True))

    def test_historical_as_of_skips_the_abort_but_still_warns(
        self, settings, state_store, caplog
    ):
        state_store.upsert_position(_closed_position())
        deps = _preflight_deps(state_store, _equity_settings(settings, None))

        with caplog.at_level(logging.WARNING):
            _preflight(deps, DailyRunOptions(as_of=date(2027, 1, 15)))

        assert any("account_equity_usd" in record.message for record in caplog.records)

    def test_abort_does_not_touch_storage(self, settings, state_store):
        position = _closed_position()
        state_store.upsert_position(position)
        deps = _preflight_deps(state_store, _equity_settings(settings, None))

        with pytest.raises(PreflightAbort):
            _preflight(deps, DailyRunOptions())

        assert state_store.get_closed_positions(is_paper=True) == [position]

    def test_main_exits_with_code_two_and_creates_no_run(
        self, monkeypatch, capsys, settings, state_store
    ):
        settings = _equity_settings(settings, None)
        state_store.upsert_position(_closed_position())
        run_daily_calls = []
        monkeypatch.setattr(daily_module, "load_secrets", _isolated_secrets)
        monkeypatch.setattr(daily_module, "load_settings", lambda: settings)
        monkeypatch.setattr(daily_module, "load_strategies", lambda: "fake-strategies")
        monkeypatch.setattr(
            daily_module,
            "_compose_dependencies",
            lambda *_args: _preflight_deps(state_store, settings),
        )
        monkeypatch.setattr(
            daily_module,
            "run_daily",
            lambda *_args: run_daily_calls.append(_args),
        )

        with pytest.raises(SystemExit) as exc_info:
            main([])

        assert exc_info.value.code == 2
        assert run_daily_calls == []
        err = capsys.readouterr().err
        assert "risk.account_equity_usd" in err
        # Machine-readable prefix contract with the swing-daily skill: both
        # abort causes share exit code 2, so stderr must carry the reason.
        assert "PREFLIGHT_ABORT[account_equity_unset]:" in err
        with state_store._database.connect() as conn:  # noqa: SLF001
            count = conn.execute("SELECT count(*) FROM runs").fetchone()
        assert count == (0,)


class TestMain:
    def test_historical_missing_snapshot_exits_before_price_provider(
        self, monkeypatch, tmp_path
    ):
        settings = load_settings("config/settings.yaml")
        strategies = load_strategies("config/strategies.yaml")

        def _missing_snapshot(*_args, **_kwargs):
            msg = "No persisted universe snapshot is available at or before 2026-07-20"
            raise UniverseError(msg)

        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(
            daily_module,
            "load_secrets",
            lambda: _isolated_secrets(edgar_identity="Test test@example.com"),
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

        monkeypatch.setattr(daily_module, "load_secrets", _isolated_secrets)
        monkeypatch.setattr(daily_module, "load_settings", lambda: "fake-settings")
        monkeypatch.setattr(daily_module, "load_strategies", lambda: "fake-strategies")
        monkeypatch.setattr(daily_module, "_compose_dependencies", fake_compose)
        monkeypatch.setattr(daily_module, "_preflight", lambda *_args, **_kwargs: None)
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

        monkeypatch.setattr(daily_module, "load_secrets", _isolated_secrets)
        monkeypatch.setattr(daily_module, "load_settings", lambda: "fake-settings")
        monkeypatch.setattr(daily_module, "load_strategies", lambda: "fake-strategies")
        monkeypatch.setattr(
            daily_module, "_compose_dependencies", lambda *_args: "fake-deps"
        )
        monkeypatch.setattr(daily_module, "_preflight", lambda *_args, **_kwargs: None)
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


class TestConfigureLoggingRedactsSecrets:
    """Tests `_SecretRedactionFilter`, attached to root logging by `_configure_logging`.

    It must strip every configured secret from both the record message and
    any attached exception traceback (AGENTS.md: "never log secrets") -- see
    `text/calendar_fred.py`/`text/news_finnhub.py`, which send their API keys
    as URL query params that `httpx.HTTPStatusError` embeds verbatim in its
    message.
    """

    def test_defaults_to_quiet_root_and_informative_application_logger(self):
        root_logger = logging.getLogger()
        application_logger = logging.getLogger("swing_copilot")
        previous_root_level = root_logger.level
        previous_application_level = application_logger.level
        try:
            _configure_logging(_isolated_secrets())

            assert root_logger.level == logging.WARNING
            assert application_logger.level == logging.INFO
        finally:
            root_logger.setLevel(previous_root_level)
            application_logger.setLevel(previous_application_level)

    @pytest.mark.parametrize(
        ("level_name", "level"),
        [
            ("DEBUG", logging.DEBUG),
            ("INFO", logging.INFO),
            ("WARNING", logging.WARNING),
            ("ERROR", logging.ERROR),
        ],
    )
    def test_explicit_log_level_applies_to_root_and_application_logger(
        self, level_name, level
    ):
        root_logger = logging.getLogger()
        application_logger = logging.getLogger("swing_copilot")
        previous_root_level = root_logger.level
        previous_application_level = application_logger.level
        try:
            _configure_logging(_isolated_secrets(), level=level_name)

            assert root_logger.level == level
            assert application_logger.level == level
        finally:
            root_logger.setLevel(previous_root_level)
            application_logger.setLevel(previous_application_level)

    def test_redacts_secret_from_message_and_traceback(self, caplog):
        secrets = _isolated_secrets(
            finnhub_api_key="finnhub-sekrit123",
            fred_api_key="fred-sekrit456",
            discord_webhook_url="https://discord.com/api/webhooks/sekrit-hook",
        )
        _configure_logging(secrets)
        logger = logging.getLogger("swing_copilot.pipeline.daily.test")

        with caplog.at_level(logging.ERROR):
            try:
                error = _make_status_error(
                    "401 error for url "
                    "'https://fred.stlouisfed.org/releases?api_key=fred-sekrit456'"
                )
                raise error
            except httpx.HTTPStatusError:
                logger.exception("fetch failed for token=%s", "finnhub-sekrit123")

        assert "fred-sekrit456" not in caplog.text
        assert "finnhub-sekrit123" not in caplog.text
        assert "[REDACTED]" in caplog.text
        # Both the rendered message line and the appended traceback text are
        # redacted, not just one of the two.
        record = caplog.records[-1]
        assert "fred-sekrit456" not in record.message
        assert "finnhub-sekrit123" not in record.message
        assert record.exc_text is not None
        assert "fred-sekrit456" not in record.exc_text
        assert "[REDACTED]" in record.exc_text

    def test_empty_and_none_secrets_are_never_redacted(self, caplog):
        secrets = _isolated_secrets()  # every secret unset (None)
        _configure_logging(secrets)
        logger = logging.getLogger("swing_copilot.pipeline.daily.test")

        with caplog.at_level(logging.ERROR):
            logger.error("ordinary message with no secrets in it")

        assert "ordinary message with no secrets in it" in caplog.text
        assert "[REDACTED]" not in caplog.text


class TestPreflightAbortStderrContract:
    """The tag `swing-daily` branches on must survive refactors (Issue #193).

    Both abort causes exit `2`, so the reason has to be readable from stderr's
    first line without parsing prose. `_preflight` raises for the
    account-equity trap and `run_daily` raises for the same-day rerun guard;
    the rendered line must be identical either way.
    """

    @staticmethod
    def _stub_composition(monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(daily_module, "load_secrets", _isolated_secrets)
        monkeypatch.setattr(daily_module, "load_settings", lambda: "fake-settings")
        monkeypatch.setattr(daily_module, "load_strategies", lambda: "fake-strategies")
        monkeypatch.setattr(
            daily_module, "_compose_dependencies", lambda *_args: "fake-deps"
        )
        monkeypatch.setattr(daily_module, "_preflight", lambda *_args, **_kwargs: None)
        monkeypatch.setattr(daily_module, "run_daily", lambda *_args, **_kwargs: None)

    @pytest.mark.parametrize("aborting_step", ["_preflight", "run_daily"])
    @pytest.mark.parametrize("reason", ["account_equity_unset", "same_day_rerun"])
    def test_the_first_stderr_line_carries_the_tagged_reason(
        self, monkeypatch, capsys, aborting_step, reason
    ):
        self._stub_composition(monkeypatch)

        message = "中止した理由の説明"

        def _abort(*_args, **_kwargs):
            raise PreflightAbort(message, reason=reason)

        monkeypatch.setattr(daily_module, aborting_step, _abort)

        with pytest.raises(SystemExit) as exc_info:
            main([])

        assert exc_info.value.code == 2
        first_line = capsys.readouterr().err.splitlines()[0]
        assert first_line == f"PREFLIGHT_ABORT[{reason}]: 中止した理由の説明"
