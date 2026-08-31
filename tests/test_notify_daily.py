"""Tests for scripts/notify_daily.py (Issue #383, FR-09).

Fully offline: `load_secrets`/`DiscordNotifier`/`build_daily_notification` are
all monkeypatched on the dynamically loaded module, so no real `.env`,
webhook, or run archive is ever touched.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar

import pytest

from swing_copilot.config import Secrets

if TYPE_CHECKING:
    from types import ModuleType

REPO_ROOT = Path(__file__).resolve().parents[1]


def _load_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "notify_daily", REPO_ROOT / "scripts" / "notify_daily.py"
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


notify_daily = _load_module()


def _write_settings(tmp_path: Path, *, notification_enabled: bool) -> Path:
    path = tmp_path / "settings.yaml"
    path.write_text(
        f"notification:\n  enabled: {str(notification_enabled).lower()}\n",
        encoding="utf-8",
    )
    return path


class _FakeNotifier:
    """Records every `notify()` call; the third arg mirrors `DiscordNotifier.__init__`."""

    instances: ClassVar[list[_FakeNotifier]] = []

    def __init__(self, webhook_url: str) -> None:
        self.webhook_url = webhook_url
        self.calls: list[str] = []
        self.results: list[bool] = []
        type(self).instances.append(self)

    def notify(self, summary: str, report_path: object) -> bool:
        del report_path
        self.calls.append(summary)
        if self.results:
            return self.results.pop(0)
        return True


@pytest.fixture(autouse=True)
def _reset_fake_notifier_instances() -> None:
    _FakeNotifier.instances = []


class TestMain:
    def test_notification_disabled_sends_nothing_and_exits_zero(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        settings_path = _write_settings(tmp_path, notification_enabled=False)
        monkeypatch.setattr(notify_daily, "load_secrets", Secrets)
        monkeypatch.setattr(
            notify_daily,
            "build_daily_notification",
            lambda **_kwargs: pytest.fail(
                "must not build a notification when disabled"
            ),
        )

        exit_code = notify_daily.main(["--settings", str(settings_path)])

        assert exit_code == 0

    def test_enabled_without_webhook_is_a_configuration_error(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        settings_path = _write_settings(tmp_path, notification_enabled=True)
        monkeypatch.setattr(notify_daily, "load_secrets", Secrets)

        exit_code = notify_daily.main(["--settings", str(settings_path)])

        assert exit_code == 1

    def test_enabled_sends_every_built_message_in_order(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        settings_path = _write_settings(tmp_path, notification_enabled=True)
        monkeypatch.setattr(
            notify_daily,
            "load_secrets",
            lambda: Secrets(discord_webhook_url="https://discord.example/hook"),
        )
        monkeypatch.setattr(
            notify_daily,
            "build_daily_notification",
            lambda **_kwargs: ["message one", "message two"],
        )
        monkeypatch.setattr(notify_daily, "DiscordNotifier", _FakeNotifier)

        exit_code = notify_daily.main(
            [
                "--settings",
                str(settings_path),
                "--outcome-file",
                str(tmp_path / "outcome.json"),
            ]
        )

        assert exit_code == 0
        assert len(_FakeNotifier.instances) == 1
        sent = _FakeNotifier.instances[0]
        assert sent.webhook_url == "https://discord.example/hook"
        assert sent.calls == ["message one", "message two"]

    def test_a_failed_send_stops_the_remaining_messages_and_exits_nonzero(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        settings_path = _write_settings(tmp_path, notification_enabled=True)
        monkeypatch.setattr(
            notify_daily,
            "load_secrets",
            lambda: Secrets(discord_webhook_url="https://discord.example/hook"),
        )
        monkeypatch.setattr(
            notify_daily,
            "build_daily_notification",
            lambda **_kwargs: ["message one", "message two", "message three"],
        )

        def _failing_notifier(webhook_url: str) -> _FakeNotifier:
            fake = _FakeNotifier(webhook_url)
            fake.results = [False]  # fails on the first call
            return fake

        monkeypatch.setattr(notify_daily, "DiscordNotifier", _failing_notifier)

        exit_code = notify_daily.main(["--settings", str(settings_path)])

        assert exit_code == 1
        sent = _FakeNotifier.instances[0]
        # Only the first (failed) message was attempted; the rest were never sent.
        assert sent.calls == ["message one"]

    def test_outcome_file_env_fallback_is_used_when_no_flag_is_given(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        settings_path = _write_settings(tmp_path, notification_enabled=True)
        outcome_path = tmp_path / "outcome-from-env.json"
        monkeypatch.setenv("COPILOT_DAILY_OUTCOME_FILE", str(outcome_path))
        monkeypatch.setattr(
            notify_daily,
            "load_secrets",
            lambda: Secrets(discord_webhook_url="https://discord.example/hook"),
        )
        captured: dict[str, object] = {}

        def _capture_build(**kwargs: object) -> list[str]:
            captured.update(kwargs)
            return ["only message"]

        monkeypatch.setattr(notify_daily, "build_daily_notification", _capture_build)
        monkeypatch.setattr(notify_daily, "DiscordNotifier", _FakeNotifier)

        exit_code = notify_daily.main(["--settings", str(settings_path)])

        assert exit_code == 0
        assert captured["outcome_file"] == outcome_path
