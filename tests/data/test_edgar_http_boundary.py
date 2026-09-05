"""Regression tests for `EdgarClient`'s retry contract at the real HTTP boundary.

`tests/data/test_edgar.py` fakes `EdgarClient`'s own declared seam
(`company_factory`), so it never exercises `edgar.httprequests`' synchronous
request functions (`get_with_retry`, `stream_with_retry`, `post_with_retry`)
that `Company.get_facts()` calls into -- which is exactly where Issue #429's
bug lived: those functions carry their own `stamina`-based retry loop, with
real `time.sleep` and no injection seam, running *underneath*
`EdgarClient._with_retries`. A `company_factory` fake can never observe that
second, hidden retry loop, so the repository's stated "3 attempts,
deterministic 1s/2s backoff" contract was never actually verified for EDGAR.

This module deliberately violates `writing-external-adapters`' "test the
adapter's own declared seam, not two layers down" rule, on purpose: it fakes
`edgar.httprequests.http_client`, the one function every synchronous
edgartools request path obtains its `httpx.Client` from. That is a layer
*below* `company_factory`, but it is also the narrowest seam that lets
edgartools' own `@stamina.retry(...)`-decorated functions run for real while
still letting a test count the requests that reach the transport and inspect
the values passed to the injected `sleep_fn`. Counting requests rather than
asserting on `stamina` itself is deliberate: if edgartools ever swaps its
retry mechanism for something other than `stamina`, these tests still fail
(the request/sleep counts would move), rather than silently going stale.

This is intentionally scoped to this one module -- `placing-tests` promotes
a helper to `tests/support/` only once a second file needs it, and nothing
else in the suite reaches this low today.

Offline guarantee: `httpx.MockTransport` never opens a socket, so
`tests/conftest.py`'s autouse `_block_real_network` (which patches
`socket.socket.connect`) stays untouched and still fires on any accidental
real call. `edgar.Company(symbol)`'s ticker-to-CIK resolution reads
edgartools' bundled `company_tickers.parquet` and touches neither the network
nor `~/.edgar`, which is exercised (and confirmed) with the network guard
active; `EDGAR_LOCAL_DATA_DIR` is still redirected to `tmp_path` as a second
layer of insurance.

Every fixture below uses an empty `companyfacts` JSON body
(`{"facts": {}}`): `EntityFactsParser.parse_company_facts` returns `None` for
it, so `fetch_fundamentals` returns `[]` without exercising the fact-grouping
logic already covered by `tests/data/test_edgar.py`, and -- deliberately --
`edgar.entity.entity_facts`'s module-level `_company_facts_cache` (keyed by
CIK, populated only on a successful *non-empty* parse) never gets primed by
one test in a way that would let a later test in this file skip its own HTTP
request.
"""

from __future__ import annotations

import contextlib
from datetime import UTC, datetime
from itertools import count
from typing import TYPE_CHECKING

import httpx
import pytest

from swing_copilot.data.edgar import _MIN_REQUEST_INTERVAL_SECONDS, EdgarClient
from tests.conftest import ThrottleTimeline

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator
    from pathlib import Path

    from swing_copilot.storage.market_store import FundamentalsRecord

IDENTITY = "swing-copilot http-boundary tester tmasuyama1114@gmail.com"

#: A `companyfacts` response body that parses to no XBRL facts at all -- see
#: the module docstring for why every fixture here uses this same body.
_EMPTY_FACTS_JSON = {"cik": 320193, "entityName": "Apple Inc.", "facts": {}}

#: `data/edgar.py::_DEFAULT_FUNDAMENTALS_LOOKBACK_DAYS`'s `as_of`, restated
#: so a test's request doesn't depend on the module's default lookback.
_AS_OF = datetime(2026, 7, 20, tzinfo=UTC)


class _NoThrottleClock:
    """A monotonic-shaped clock whose steps always clear the request throttle.

    Every retry-backoff assertion below (`sleeps == [1.0, 2.0]` and similar)
    would otherwise pick up extra throttle waits from `EdgarClient._throttle`
    firing between fast successive attempts -- that invariant already has its
    own coverage in `tests/data/test_edgar.py::TestRateLimiting`. T6 below is
    the one exception: it uses `ThrottleTimeline` instead, specifically to
    prove the throttle *does* fire on every attempt at the real HTTP layer.
    """

    def __init__(self, step_seconds: float = 1.0) -> None:
        self._now = 0.0
        self._step_seconds = step_seconds

    def __call__(self) -> float:
        self._now += self._step_seconds
        return self._now


def _install_responder(
    monkeypatch: pytest.MonkeyPatch,
    responder: Callable[[httpx.Request, int], httpx.Response],
) -> list[str]:
    """Fake `edgar.httprequests.http_client`, recording every request's URL.

    `http_client(**kwargs) -> Generator[httpx.Client, None, None]` is the one
    function `get_with_retry`/`stream_with_retry`/`post_with_retry` (in
    `edgar.httprequests`) call to obtain their `httpx.Client`; replacing it
    with an `httpx.MockTransport`-backed client leaves edgartools' own
    `@stamina.retry(...)` decorators running for real, so the request count
    reflects the actual number of attempts edgartools made -- not a
    `company_factory`-level approximation of it.

    Returns:
        The list every request's URL is appended to, in issue order.
    """
    requests: list[str] = []
    attempt_numbers = count(1)

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(str(request.url))
        return responder(request, next(attempt_numbers))

    @contextlib.contextmanager
    def fake_http_client(**_kwargs: object) -> Iterator[httpx.Client]:
        with httpx.Client(transport=httpx.MockTransport(handler)) as client:
            yield client

    monkeypatch.setattr("edgar.httprequests.http_client", fake_http_client)
    return requests


def _build_client(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    responder: Callable[[httpx.Request, int], httpx.Response],
    *,
    clock: Callable[[], float] | None = None,
    sleep_fn: Callable[[float], None] | None = None,
) -> tuple[EdgarClient, list[str]]:
    """Build a real `EdgarClient` whose HTTP layer is `responder`, fully offline.

    Returns:
        The client, and the list of request URLs `responder` will be fed
        into as the client issues them.
    """
    # `EdgarClient.__init__` calls the real `edgar.set_identity`, which sets
    # the real process `EDGAR_IDENTITY` env var; fake it the same way
    # `tests/data/test_edgar.py::_no_real_edgar_identity_mutation` does, and
    # supply the identity edgartools' own `@with_identity` reads from the
    # environment when a call site (like `get_with_retry`) passes none.
    monkeypatch.setattr("swing_copilot.data.edgar.edgar.set_identity", lambda _i: None)
    monkeypatch.setenv("EDGAR_IDENTITY", IDENTITY)
    monkeypatch.setenv("EDGAR_LOCAL_DATA_DIR", str(tmp_path))
    requests = _install_responder(monkeypatch, responder)
    client = EdgarClient(
        IDENTITY,
        clock=clock or _NoThrottleClock(),
        sleep_fn=sleep_fn or (lambda _seconds: None),
    )
    return client, requests


def _fetch(client: EdgarClient) -> list[FundamentalsRecord]:
    """Fetch AAPL's fundamentals through the public entry point."""
    return client.fetch_fundamentals("AAPL", _AS_OF)


def _always_connect_error(_request: httpx.Request, _attempt: int) -> httpx.Response:
    msg = "transport failure"
    raise httpx.ConnectError(msg, request=_request)


def _always_status(status_code: int) -> Callable[[httpx.Request, int], httpx.Response]:
    def responder(_request: httpx.Request, _attempt: int) -> httpx.Response:
        return httpx.Response(status_code, json={})

    return responder


def _empty_facts(_request: httpx.Request, _attempt: int) -> httpx.Response:
    return httpx.Response(200, json=_EMPTY_FACTS_JSON)


def _fails_then_succeeds(
    failing_attempts: int,
) -> Callable[[httpx.Request, int], httpx.Response]:
    """A responder that raises `ConnectError` for the first N attempts, then succeeds."""

    def responder(request: httpx.Request, attempt: int) -> httpx.Response:
        if attempt <= failing_attempts:
            msg = "transport failure"
            raise httpx.ConnectError(msg, request=request)
        return _empty_facts(request, attempt)

    return responder


class TestPersistentTransportFailure:
    def test_persistent_transport_failure_issues_exactly_three_requests(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """The core Issue #429 regression.

        Before the fix (edgartools' internal `stamina` retry left active),
        this same scenario measured 15 requests and 48.0s of un-injected real
        sleep: the outer `_with_retries` (3 attempts) each triggered
        edgartools' own inner retry (`QUICK_RETRY_ATTEMPTS = 5`), so
        3 x 5 = 15. With `stamina.set_active(False)` (called from
        `EdgarClient.__init__`), edgartools makes exactly one request per
        outer attempt, so the total is exactly 3.
        """
        sleeps: list[float] = []
        client, requests = _build_client(
            monkeypatch, tmp_path, _always_connect_error, sleep_fn=sleeps.append
        )

        with pytest.raises(httpx.ConnectError, match="transport failure"):
            _fetch(client)

        assert len(requests) == 3
        assert sleeps == [1.0, 2.0]


class TestTransportFailureRecovery:
    def test_transport_failure_recovering_on_the_third_attempt_sleeps_the_declared_backoff(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Assert the wait between attempts is now the repository's own loop.

        Before the fix, the wait between attempts was edgartools' own real
        `time.sleep`, invisible to the injected `sleep_fn` (`sleeps == []`
        even though the caller really did wait). This asserts the wait is
        now the repository's own loop -- not just that a request count
        matches, but that *our* backoff is what the caller actually waits on.
        """
        sleeps: list[float] = []
        client, requests = _build_client(
            monkeypatch,
            tmp_path,
            _fails_then_succeeds(failing_attempts=2),
            sleep_fn=sleeps.append,
        )

        result = _fetch(client)

        assert len(requests) == 3
        assert sleeps == [1.0, 2.0]
        assert result == []


class TestRetryableServerError:
    def test_a_retryable_server_error_still_stops_at_three_requests(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        sleeps: list[float] = []
        client, requests = _build_client(
            monkeypatch, tmp_path, _always_status(500), sleep_fn=sleeps.append
        )

        with pytest.raises(httpx.HTTPStatusError):
            _fetch(client)

        assert len(requests) == 3
        assert sleeps == [1.0, 2.0]


class TestNonRetryableClientError:
    def test_a_forbidden_response_fails_on_the_first_attempt(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """A non-retryable 4xx fails on the first attempt.

        408/429 aside, a 4xx is not retried -- fixed here at the real HTTP
        layer rather than only through a `company_factory` fake.
        """
        sleeps: list[float] = []
        client, requests = _build_client(
            monkeypatch, tmp_path, _always_status(403), sleep_fn=sleeps.append
        )

        with pytest.raises(httpx.HTTPStatusError):
            _fetch(client)

        assert len(requests) == 1
        assert sleeps == []


class TestMissingCompanyFacts:
    def test_a_missing_companyfacts_document_returns_no_records_without_retrying(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """A 404 on the bulk companyfacts document is an absence, not a failure.

        It means "no XBRL facts on file" (the existing docstring's
        contract): edgartools maps it to `None`, and `fetch_fundamentals`
        returns `[]`.
        """
        sleeps: list[float] = []
        client, requests = _build_client(
            monkeypatch, tmp_path, _always_status(404), sleep_fn=sleeps.append
        )

        result = _fetch(client)

        assert len(requests) == 1
        assert sleeps == []
        assert result == []


class TestThrottleAtTheRealHttpLayer:
    def test_every_retried_attempt_is_throttled_at_the_real_http_layer(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """The throttle fires on every attempt, including a failed one.

        `writing-external-adapters` requires this; proven here through the
        real HTTP layer rather than a `company_factory` fake that never sees
        the request edgartools itself issues.
        """
        timeline = ThrottleTimeline(request_seconds=0.02)

        def responder(request: httpx.Request, attempt: int) -> httpx.Response:
            timeline.issue_request()
            return _fails_then_succeeds(failing_attempts=2)(request, attempt)

        client, requests = _build_client(
            monkeypatch,
            tmp_path,
            responder,
            clock=timeline.clock,
            sleep_fn=timeline.sleep,
        )

        _fetch(client)

        assert len(requests) == 3
        assert timeline.gaps_below(_MIN_REQUEST_INTERVAL_SECONDS) == []


class TestHappyPath:
    def test_the_happy_path_issues_exactly_one_request(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """The disable has no side effect on the ordinary case.

        One request, no sleeps, whether or not edgartools' internal retry is
        active.
        """
        sleeps: list[float] = []
        client, requests = _build_client(
            monkeypatch, tmp_path, _empty_facts, sleep_fn=sleeps.append
        )

        result = _fetch(client)

        assert len(requests) == 1
        assert sleeps == []
        assert result == []
