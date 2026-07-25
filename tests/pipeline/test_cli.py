"""Tests for pipeline/daily.py's CLI parsing and composition root (FR-12).

`_compose_dependencies` is exercised with `load_secrets`/`get_sp500_universe`
monkeypatched to avoid any real network access or dependency on a
developer's local `.env` (never read directly in this suite).
"""

from __future__ import annotations

import logging
from datetime import date
from pathlib import Path

import httpx
import pytest

from swing_copilot.config import Secrets, load_settings, load_strategies
from swing_copilot.exceptions import ConfigError
from swing_copilot.models import DailyRunOptions, RunMode
from swing_copilot.pipeline import daily as daily_module
from swing_copilot.pipeline.daily import (
    DailyDependencies,
    _compose_dependencies,
    _configure_logging,
    _parse_args,
    _paths_for_mode,
    _required_features,
    main,
)
from swing_copilot.storage.database import DEFAULT_DB_PATH
from swing_copilot.universe import UniverseMember


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
                "--skip-llm",
                "--limit",
                "5",
            ]
        )

        assert options == DailyRunOptions(
            as_of=date(2026, 7, 20),
            is_dry_run=True,
            skip_text=True,
            skip_llm=True,
            limit=5,
        )

    def test_strategy_defaults_to_default_and_accepts_named_strategy(self):
        assert _parse_args([]).strategy_key == "default"
        assert _parse_args(["--strategy", "minervini_stage2"]).strategy_key == (
            "minervini_stage2"
        )


class TestRequiredFeatures:
    def test_full_run_requires_edgar_finnhub_fred_and_llm(self):
        settings = load_settings("config/settings.yaml")

        features = _required_features(DailyRunOptions(), settings)

        assert features == {"edgar", "finnhub", "fred", "llm"}

    def test_skip_text_drops_finnhub_and_fred(self):
        settings = load_settings("config/settings.yaml")

        features = _required_features(DailyRunOptions(skip_text=True), settings)

        assert features == {"edgar", "llm"}

    def test_skip_llm_drops_llm(self):
        settings = load_settings("config/settings.yaml")

        features = _required_features(DailyRunOptions(skip_llm=True), settings)

        assert features == {"edgar", "finnhub", "fred"}

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
    monkeypatch.setattr(
        daily_module, "get_sp500_universe", lambda *_args, **_kwargs: members
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
            _compose_dependencies(
                DailyRunOptions(skip_text=True, skip_llm=True), settings, strategies
            )

    def test_skip_text_and_skip_llm_leaves_those_clients_none(self, monkeypatch):
        monkeypatch.setattr(
            daily_module,
            "load_secrets",
            lambda: _isolated_secrets(edgar_identity="Test test@example.com"),
        )
        settings = load_settings("config/settings.yaml")
        strategies = load_strategies("config/strategies.yaml")

        deps = _compose_dependencies(
            DailyRunOptions(skip_text=True, skip_llm=True), settings, strategies
        )

        assert isinstance(deps, DailyDependencies)
        assert deps.edgar_client is not None
        assert deps.news_client is None
        assert deps.earnings_client is None
        assert deps.calendar_client is None
        assert deps.llm_client is None
        assert deps.notifier is None

    def test_configured_secrets_wire_up_the_matching_clients(self, monkeypatch):
        monkeypatch.setattr(
            daily_module,
            "load_secrets",
            lambda: _isolated_secrets(
                edgar_identity="Test test@example.com",
                finnhub_api_key="finnhub-key",
                fred_api_key="fred-key",
                anthropic_api_key="sk-test",
            ),
        )
        settings = load_settings("config/settings.yaml")
        strategies = load_strategies("config/strategies.yaml")

        deps = _compose_dependencies(DailyRunOptions(), settings, strategies)

        assert deps.news_client is not None
        assert deps.earnings_client is not None
        assert deps.calendar_client is not None
        assert deps.llm_client is not None

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
            _compose_dependencies(
                DailyRunOptions(skip_text=True, skip_llm=True), settings, strategies
            )

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
            DailyRunOptions(is_dry_run=True, skip_text=True, skip_llm=True),
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
            DailyRunOptions(skip_text=True, skip_llm=True), settings, strategies
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

            return _Result()

        monkeypatch.setattr(daily_module, "load_secrets", _isolated_secrets)
        monkeypatch.setattr(daily_module, "load_settings", lambda: "fake-settings")
        monkeypatch.setattr(daily_module, "load_strategies", lambda: "fake-strategies")
        monkeypatch.setattr(daily_module, "_compose_dependencies", fake_compose)
        monkeypatch.setattr(daily_module, "run_daily", fake_run_daily)

        with pytest.raises(SystemExit) as exc_info:
            main(["--dry-run"])

        assert exc_info.value.code == 7
        assert calls["options"].is_dry_run is True
        assert calls["run_daily"] == (calls["options"], "fake-deps")


class TestConfigureLoggingRedactsSecrets:
    """Tests `_SecretRedactionFilter`, attached to root logging by `_configure_logging`.

    It must strip every configured secret from both the record message and
    any attached exception traceback (AGENTS.md: "never log secrets") -- see
    `text/calendar_fred.py`/`text/news_finnhub.py`, which send their API keys
    as URL query params that `httpx.HTTPStatusError` embeds verbatim in its
    message.
    """

    def test_redacts_secret_from_message_and_traceback(self, caplog):
        secrets = _isolated_secrets(
            finnhub_api_key="finnhub-sekrit123",
            fred_api_key="fred-sekrit456",
            anthropic_api_key="sk-ant-sekrit789",
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
