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

    def test_non_2xx_response_returns_false_without_raising(self):
        def fake_post(url, json):
            return _FakeResponse(500)

        notifier = DiscordNotifier(
            "https://discord.example/webhook", http_post=fake_post
        )

        result = notifier.notify("summary", None)

        assert result is False

    def test_network_exception_returns_false_without_raising(self):
        def fake_post(url, json):
            raise httpx.ConnectError(_CONNECT_ERROR_MESSAGE)

        notifier = DiscordNotifier(
            "https://discord.example/webhook", http_post=fake_post
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


class TestNotifierProtocolConformance:
    def test_discord_notifier_satisfies_notifier_protocol(self):
        notifier: Notifier = DiscordNotifier(
            "https://discord.example/webhook", http_post=_fake_post_ok
        )
        assert notifier.notify("x", None) is True
