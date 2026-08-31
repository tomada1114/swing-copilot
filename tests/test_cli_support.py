"""Shared CLI error-to-exit-code conversion and logging setup (`cli_support.py`).

`ExitPolicy`/`run_cli` are Issue #193; `configure_cli_logging`/
`SecretRedactionFilter` were extracted here from `pipeline/daily_composition.py`
(Issue #381) so `copilot-retro`'s `export`/`prepare` -- which also make
authenticated Finnhub/EDGAR calls and can therefore leak a secret through
`logger.exception` -- share the same redaction, instead of duplicating it or
going without.
"""

from __future__ import annotations

import logging

import httpx
import pytest

from swing_copilot.cli_support import (
    ExitPolicy,
    SecretRedactionFilter,
    configure_cli_logging,
    run_cli,
)
from swing_copilot.config import Secrets
from swing_copilot.exceptions import ConfigError, SwingCopilotError


class TestSuccess:
    def test_the_bodys_return_value_is_passed_through(self):
        policy = ExitPolicy(errors=(ConfigError,))

        assert run_cli(lambda: "settings", policy) == "settings"


class TestMessageAsExitStatus:
    """`code=None` is the argparse convention every `SystemExit(str)` used."""

    def test_the_message_becomes_the_exit_status(self):
        policy = ExitPolicy(errors=(ConfigError,))

        def _explode() -> None:
            msg = "config/settings.yaml が読めません"
            raise ConfigError(msg)

        with pytest.raises(SystemExit) as exit_info:
            run_cli(_explode, policy)

        assert exit_info.value.code == "config/settings.yaml が読めません"

    def test_nothing_is_written_to_stderr_by_this_process(self, capsys):
        """The interpreter prints the message at exit; the code must not."""
        policy = ExitPolicy(errors=(ConfigError,))

        def _explode() -> None:
            msg = "bad config"
            raise ConfigError(msg)

        with pytest.raises(SystemExit):
            run_cli(_explode, policy)

        assert capsys.readouterr().err == ""


class TestNumericExitStatus:
    def test_the_message_is_written_to_stderr_with_the_given_code(self, capsys):
        policy = ExitPolicy(errors=(ConfigError,), code=1)

        def _explode() -> None:
            msg = "bad config"
            raise ConfigError(msg)

        with pytest.raises(SystemExit) as exit_info:
            run_cli(_explode, policy)

        assert exit_info.value.code == 1
        assert capsys.readouterr().err == "bad config\n"

    def test_a_custom_reporter_replaces_stderr(self, capsys):
        reported: list[str] = []
        policy = ExitPolicy(
            errors=(ConfigError,),
            code=2,
            format_message=lambda exc: f"verification could not run: {exc}",
            report=reported.append,
        )

        def _explode() -> None:
            msg = "missing document"
            raise ConfigError(msg)

        with pytest.raises(SystemExit) as exit_info:
            run_cli(_explode, policy)

        assert exit_info.value.code == 2
        assert reported == ["verification could not run: missing document"]
        assert capsys.readouterr().err == ""


class TestUnconvertedErrors:
    def test_an_unlisted_exception_propagates_instead_of_becoming_an_exit(self):
        """A programming error must still surface as a traceback."""
        policy = ExitPolicy(errors=(ConfigError,))

        def _explode() -> None:
            msg = "not a config problem"
            raise SwingCopilotError(msg)

        with pytest.raises(SwingCopilotError, match="not a config problem"):
            run_cli(_explode, policy)

    def test_the_original_error_stays_the_cause(self):
        """`--tb` and `raise ... from` keep pointing at the real failure."""
        policy = ExitPolicy(errors=(ConfigError,), code=1)
        original = ConfigError("bad config")

        def _explode() -> None:
            raise original

        with pytest.raises(SystemExit) as exit_info:
            run_cli(_explode, policy)

        assert exit_info.value.__cause__ is original


def _make_status_error(message: str) -> httpx.HTTPStatusError:
    request = httpx.Request("GET", "https://example.com")
    response = httpx.Response(401, request=request)
    return httpx.HTTPStatusError(message, request=request, response=response)


def _isolated_secrets(**overrides: str) -> Secrets:
    """Build `Secrets` isolated from any real `.env` a developer has locally."""
    return Secrets(_env_file=None, **overrides)  # type: ignore[call-arg]


@pytest.fixture
def _restore_logging_state():
    """Undo whatever `configure_cli_logging` mutates on the root/app loggers.

    `configure_cli_logging` is process-global by design (it configures
    `logging.root`), so a test that calls it must not leak handlers, levels,
    or filters into whatever runs next in the same pytest session.

    Restoring `root_logger.handlers` alone is not enough. A handler that
    already existed (pytest's own session-scoped capture handler survives the
    whole worker) is reused rather than replaced, and `configure_cli_logging`
    appends its `SecretRedactionFilter` to that SAME object's `.filters` list.
    A filter left behind there keeps clearing every later record's `args` and
    `exc_info` for the rest of the worker, so each pre-existing handler's own
    filter list is snapshotted and restored too.
    """
    root_logger = logging.getLogger()
    application_logger = logging.getLogger("swing_copilot")
    saved_handlers = list(root_logger.handlers)
    saved_filters = list(root_logger.filters)
    saved_handler_filters = [list(handler.filters) for handler in saved_handlers]
    saved_root_level = root_logger.level
    saved_application_level = application_logger.level
    try:
        yield
    finally:
        root_logger.handlers = saved_handlers
        root_logger.filters = saved_filters
        for handler, filters in zip(saved_handlers, saved_handler_filters, strict=True):
            handler.filters = filters
        root_logger.setLevel(saved_root_level)
        application_logger.setLevel(saved_application_level)


@pytest.mark.usefixtures("_restore_logging_state")
class TestConfigureCliLoggingRedactsSecrets:
    """Tests `SecretRedactionFilter`, attached to root logging by `configure_cli_logging`.

    It must strip every configured secret from both the record message and
    any attached exception traceback (AGENTS.md: "never log secrets") -- see
    `text/calendar_fred.py`/`text/news_finnhub.py`, which send their API keys
    as URL query params that `httpx.HTTPStatusError` embeds verbatim in its
    message.
    """

    def test_defaults_to_quiet_root_and_informative_application_logger(self):
        configure_cli_logging(_isolated_secrets())

        assert logging.getLogger().level == logging.WARNING
        assert logging.getLogger("swing_copilot").level == logging.INFO

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
        configure_cli_logging(_isolated_secrets(), level=level_name)

        assert logging.getLogger().level == level
        assert logging.getLogger("swing_copilot").level == level

    def test_redacts_secret_from_message_and_traceback(self, caplog):
        secrets = _isolated_secrets(
            finnhub_api_key="finnhub-sekrit123",
            fred_api_key="fred-sekrit456",
            discord_webhook_url="https://discord.com/api/webhooks/sekrit-hook",
        )
        configure_cli_logging(secrets)
        logger = logging.getLogger("swing_copilot.cli_support.test")

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
        configure_cli_logging(secrets)
        logger = logging.getLogger("swing_copilot.cli_support.test")

        with caplog.at_level(logging.ERROR):
            logger.error("ordinary message with no secrets in it")

        assert "ordinary message with no secrets in it" in caplog.text
        assert "[REDACTED]" not in caplog.text


class TestSecretRedactionFilterDirectly:
    """`SecretRedactionFilter` is public (Issue #381): exercise it standalone."""

    def test_no_secrets_configured_passes_records_through_unchanged(self):
        record = logging.LogRecord(
            name="test",
            level=logging.INFO,
            pathname=__file__,
            lineno=1,
            msg="hello",
            args=(),
            exc_info=None,
        )

        assert SecretRedactionFilter((None, "")).filter(record) is True
        assert record.msg == "hello"
