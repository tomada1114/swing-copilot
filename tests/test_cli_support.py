"""Shared CLI error-to-exit-code conversion (`cli_support.py`, Issue #193)."""

from __future__ import annotations

import pytest

from swing_copilot.cli_support import ExitPolicy, run_cli
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
