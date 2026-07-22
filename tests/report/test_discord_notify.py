"""Acceptance tests for `report/discord_notify.py` (FR-09)."""

from __future__ import annotations

from pathlib import Path

import httpx

from swing_copilot.report.discord_notify import DiscordNotifier, Notifier

_ERROR_MESSAGE = "error"
_CONNECT_ERROR_MESSAGE = "boom"
_UNEXPECTED_CALL_MESSAGE = (
    "real httpx.post must never be called in the offline test suite"
)
_UNEXPECTED_SLEEP_MESSAGE = "real time.sleep must never be called in tests"


def _fake_post_ok(url, json):
    return _FakeResponse(204)


class _FakeResponse:
    def __init__(self, status_code: int) -> None:
        self.status_code = status_code

    def raise_for_status(self) -> None:
        if self.status_code >= httpx.codes.BAD_REQUEST:
            request = httpx.Request("POST", "https://discord.example/webhook")
            response = httpx.Response(self.status_code, request=request)
            raise httpx.HTTPStatusError(
                _ERROR_MESSAGE, request=request, response=response
            )


class TestDiscordNotifierNotify:
    def test_successful_post_returns_true(self):
        calls = []

        def fake_post(url, json):
            calls.append((url, json))
            return _FakeResponse(204)

        notifier = DiscordNotifier(
            "https://discord.example/webhook", http_post=fake_post
        )

        result = notifier.notify("5 candidates today", Path("reports/2026-07-20.html"))

        assert result is True
        assert calls == [
            (
                "https://discord.example/webhook",
                {"content": "5 candidates today\nreports/2026-07-20.html"},
            )
        ]

    def test_report_path_none_omits_it_from_content(self):
        calls = []

        def fake_post(url, json):
            calls.append(json)
            return _FakeResponse(204)

        notifier = DiscordNotifier(
            "https://discord.example/webhook", http_post=fake_post
        )

        notifier.notify("summary only", None)

        assert calls == [{"content": "summary only"}]

    def test_success_on_first_attempt_makes_exactly_one_call_and_no_backoff(self):
        calls = []
        sleeps: list[float] = []

        def fake_post(url, json):
            calls.append(1)
            return _FakeResponse(204)

        notifier = DiscordNotifier(
            "https://discord.example/webhook",
            http_post=fake_post,
            sleep_fn=sleeps.append,
        )

        result = notifier.notify("summary", None)

        assert result is True
        assert len(calls) == 1
        assert sleeps == []

    def test_400_response_is_not_retried_and_returns_false(self):
        calls = []

        def fake_post(url, json):
            calls.append(1)
            return _FakeResponse(400)

        notifier = DiscordNotifier(
            "https://discord.example/webhook", http_post=fake_post
        )

        result = notifier.notify("summary", None)

        assert result is False
        assert len(calls) == 1

    def test_429_response_is_retried_then_succeeds(self):
        responses = iter([_FakeResponse(429), _FakeResponse(204)])
        calls = []
        sleeps: list[float] = []

        def fake_post(url, json):
            calls.append(1)
            return next(responses)

        notifier = DiscordNotifier(
            "https://discord.example/webhook",
            http_post=fake_post,
            sleep_fn=sleeps.append,
        )

        result = notifier.notify("summary", None)

        assert result is True
        assert len(calls) == 2
        assert sleeps == [1.0]

    def test_500_response_is_retried_then_succeeds(self):
        responses = iter([_FakeResponse(500), _FakeResponse(204)])
        calls = []
        sleeps: list[float] = []

        def fake_post(url, json):
            calls.append(1)
            return next(responses)

        notifier = DiscordNotifier(
            "https://discord.example/webhook",
            http_post=fake_post,
            sleep_fn=sleeps.append,
        )

        result = notifier.notify("summary", None)

        assert result is True
        assert len(calls) == 2
        assert sleeps == [1.0]

    def test_transport_error_then_success_retries_with_deterministic_backoff(self):
        calls = []
        sleeps: list[float] = []

        def fake_post(url, json):
            calls.append(1)
            if len(calls) == 1:
                raise httpx.ConnectError(_CONNECT_ERROR_MESSAGE)
            return _FakeResponse(204)

        notifier = DiscordNotifier(
            "https://discord.example/webhook",
            http_post=fake_post,
            sleep_fn=sleeps.append,
        )

        result = notifier.notify("summary", None)

        assert result is True
        assert len(calls) == 2
        assert sleeps == [1.0]

    def test_all_attempts_fail_returns_false_after_ceiling_with_no_exception(self):
        calls = []
        sleeps: list[float] = []

        def fake_post(url, json):
            calls.append(1)
            raise httpx.ConnectError(_CONNECT_ERROR_MESSAGE)

        notifier = DiscordNotifier(
            "https://discord.example/webhook",
            http_post=fake_post,
            sleep_fn=sleeps.append,
        )

        result = notifier.notify("summary", None)

        assert result is False
        assert len(calls) == 3
        assert sleeps == [1.0, 2.0]

    def test_non_2xx_response_returns_false_without_raising(self):
        def fake_post(url, json):
            return _FakeResponse(500)

        notifier = DiscordNotifier(
            "https://discord.example/webhook",
            http_post=fake_post,
            sleep_fn=lambda _delay: None,
        )

        result = notifier.notify("summary", None)

        assert result is False

    def test_network_exception_returns_false_without_raising(self):
        def fake_post(url, json):
            raise httpx.ConnectError(_CONNECT_ERROR_MESSAGE)

        notifier = DiscordNotifier(
            "https://discord.example/webhook",
            http_post=fake_post,
            sleep_fn=lambda _delay: None,
        )

        result = notifier.notify("summary", None)

        assert result is False

    def test_real_http_post_is_not_called_when_fake_injected(self, monkeypatch):
        def fail_if_called(*args, **kwargs):
            raise AssertionError(_UNEXPECTED_CALL_MESSAGE)

        monkeypatch.setattr(httpx, "post", fail_if_called)
        notifier = DiscordNotifier(
            "https://discord.example/webhook", http_post=_fake_post_ok
        )

        result = notifier.notify("summary", None)

        assert result is True

    def test_real_time_sleep_is_not_called_when_fake_injected(self, monkeypatch):
        def fail_if_called(*args, **kwargs):
            raise AssertionError(_UNEXPECTED_SLEEP_MESSAGE)

        monkeypatch.setattr("time.sleep", fail_if_called)

        def fake_post(url, json):
            raise httpx.ConnectError(_CONNECT_ERROR_MESSAGE)

        notifier = DiscordNotifier(
            "https://discord.example/webhook",
            http_post=fake_post,
            sleep_fn=lambda _delay: None,
        )

        result = notifier.notify("summary", None)

        assert result is False


class TestNotifierProtocolConformance:
    def test_discord_notifier_satisfies_notifier_protocol(self):
        notifier: Notifier = DiscordNotifier(
            "https://discord.example/webhook", http_post=_fake_post_ok
        )
        assert notifier.notify("x", None) is True
