"""Tests for pipeline/daily.py's CLI parsing and composition root (FR-12).

`_compose_dependencies` is exercised with `load_secrets`/`get_sp500_universe`
monkeypatched to avoid any real network access or dependency on a
developer's local `.env` (never read directly in this suite).
"""

from __future__ import annotations

from datetime import date

import pytest

from swing_copilot.config import Secrets, load_settings
from swing_copilot.exceptions import ConfigError
from swing_copilot.models import DailyRunOptions
from swing_copilot.pipeline import daily as daily_module
from swing_copilot.pipeline.daily import (
    DailyDependencies,
    _compose_dependencies,
    _parse_args,
    _required_features,
    main,
)
from swing_copilot.universe import UniverseMember


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
                "--no-open",
            ]
        )

        assert options == DailyRunOptions(
            as_of=date(2026, 7, 20),
            is_dry_run=True,
            skip_text=True,
            skip_llm=True,
            limit=5,
            no_open=True,
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
    def test_missing_required_secret_raises_config_error(self, monkeypatch):
        monkeypatch.setattr(daily_module, "load_secrets", _isolated_secrets)
        settings = load_settings("config/settings.yaml")
        strategies = daily_module.load_strategies("config/strategies.yaml")

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
        strategies = daily_module.load_strategies("config/strategies.yaml")

        deps = _compose_dependencies(
            DailyRunOptions(skip_text=True, skip_llm=True), settings, strategies
        )

        assert isinstance(deps, DailyDependencies)
        assert deps.edgar_client is not None
        assert deps.news_client is None
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
        strategies = daily_module.load_strategies("config/strategies.yaml")

        deps = _compose_dependencies(DailyRunOptions(), settings, strategies)

        assert deps.news_client is not None
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
        strategies = daily_module.load_strategies("config/strategies.yaml")

        with pytest.raises(ConfigError, match="discord_webhook_url"):
            _compose_dependencies(
                DailyRunOptions(skip_text=True, skip_llm=True), settings, strategies
            )


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

            return _Result()

        monkeypatch.setattr(daily_module, "load_settings", lambda: "fake-settings")
        monkeypatch.setattr(daily_module, "load_strategies", lambda: "fake-strategies")
        monkeypatch.setattr(daily_module, "_compose_dependencies", fake_compose)
        monkeypatch.setattr(daily_module, "run_daily", fake_run_daily)

        with pytest.raises(SystemExit) as exc_info:
            main(["--dry-run"])

        assert exc_info.value.code == 7
        assert calls["options"].is_dry_run is True
        assert calls["run_daily"] == (calls["options"], "fake-deps")
