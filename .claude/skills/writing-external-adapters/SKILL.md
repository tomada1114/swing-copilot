---
name: writing-external-adapters
description: >
  Covers `src/swing_copilot/data/**` and `src/swing_copilot/text/**` plus the
  `retry.py`/`ratelimit.py` primitives: `retry_external_call`'s three-attempt
  deterministic backoff, `is_retryable_external_error`'s retryable/programming
  split, `MinIntervalThrottle.before_request` firing on every attempt
  including retries, injectable `http_get`/`Clock`/`sleep_fn` seams, and the
  offline pytest contract (`tests/conftest.py`'s socket blocker). Use when
  adding or changing a client in `data/edgar.py`, `data/yfinance_provider.py`,
  `data/earnings_finnhub.py`, `text/news_finnhub.py`, `text/calendar_fred.py`,
  or wiring a new external HTTP call anywhere in this repository.
---

# Writing External Adapters

**Owns:** how this repo talks to an outside service and how those calls stay
deterministic and testable offline, for `data/**`, `text/**`, `retry.py`, and
`ratelimit.py`. **Does not own:** what the fetched data means once stored
(`writing-storage-code`); the `as_of` predicate a fetch is bounded by
(`enforcing-point-in-time`); whether a `SwingCopilotError` subclass or a
`code` fits a given failure (`designing-errors`); trusting what a *model*
writes back through the skill boundary (`guarding-analysis-boundary` — text
adapters are untrusted for a different reason, see below).

## Timeout, retryable set, attempt ceiling — all explicit

Every external HTTP call carries an explicit timeout — `httpx.get(url,
params=params, timeout=10.0)` in `text/news_finnhub.py`,
`data/earnings_finnhub.py`, `text/calendar_fred.py`; `yfinance_provider.py`
uses its own `_REQUEST_TIMEOUT_SECONDS`. `retry.py`'s
`retry_external_call(operation, *, before_attempt, sleep_fn, is_retryable=...)`
is the one retry loop every adapter shares: `RETRY_DELAYS_SECONDS = (1.0,
2.0)` gives exactly three total attempts with deterministic backoff between
them — no jitter, no exponential growth, nothing that would make a test's
expected sleep sequence non-reproducible. A test asserts the *exact* backoff
sequence with a fake `sleep_fn` that records calls instead of blocking:

```python
def test_retries_rate_limited_request_and_throttles_every_attempt(self):
    ...
    result = client.fetch_company_news("AAPL", date(2027, 1, 1), as_of=date(2027, 1, 10))
    assert calls == 2
    assert sleeps == [
        pytest.approx(1.0),  # RETRY_DELAYS_SECONDS[0]
        pytest.approx(_MIN_REQUEST_INTERVAL_SECONDS - 1.0),  # the throttle's own wait
    ]
```

No real `time.sleep` anywhere in the offline suite — every adapter takes an
injectable `sleep_fn`, defaulting to `time.sleep` in production and to a
list-appending fake in tests.

## Check the wrapped library's own retry loop

`retry_external_call` being the only retry loop *this repository writes* does
not mean it is the only one that runs. `data/edgar.py` wraps `edgartools`,
and for years `EdgarClient._with_retries`'s three-attempt contract looked
correct from the outside while the `edgartools` calls inside it
(`Company.get_facts()`, `get_filings()`, `filing.text()`) ran their own
independent `stamina`-based retry loop underneath, with real `time.sleep` and
no injection seam — a transport failure could trigger up to
`QUICK_RETRY_ATTEMPTS = 5` real, un-injected retries *inside* every one of
the outer loop's 3 attempts (Issue #429; up to 15 requests and 48s of real
sleep for one logical call). A `company_factory`-level fake can never
observe this: it fakes the adapter's own declared seam, one layer *above*
where the vendored library's internal retry runs.

Before trusting a new (or newly reviewed) adapter's "N attempts,
deterministic backoff" claim, check whether the wrapped library retries on
its own — grep its source for a retry decorator (`stamina`, `tenacity`,
`backoff`, a hand-rolled loop) around the function the adapter actually
calls. If it does, disable it at the narrowest point that reflects the
invariant you actually need (`data/edgar.py::_disable_edgartools_internal_retries`
calls `stamina.set_active(False)` once from `EdgarClient.__init__`, right
alongside the existing `edgar.set_identity(identity)` construction-time
side effect, rather than trying to wrap every call site) rather than
declaring victory because your own `retry_external_call` call site looks
correct in isolation.

## Retryable vs. programming/validation errors

`is_retryable_external_error` only ever says yes to transport-level failure:
`ConnectionError`, `TimeoutError`, `httpx.TransportError` unconditionally, and
`httpx.HTTPStatusError` only for `408`, `429`, or `>= 500`. A `4xx` other than
408/429 — bad auth, a malformed request, a schema mismatch — is not retried:
`retry_external_call` re-raises immediately the moment `is_retryable(exc)` is
`False`, on the *first* attempt.

```python
def test_does_not_retry_non_transient_http_error(self):
    ...  # a 401 from http_get
    with pytest.raises(httpx.HTTPStatusError, match="unauthorized"):
        client.fetch_company_news(...)
    assert calls == 1
    assert sleeps == []
```

The distinction that matters: a retryable failure is about the *channel*
(the request could plausibly succeed if sent again — a dropped connection, a
rate limit, a transient 5xx); a non-retryable one is about the *request or
the caller's own state* (wrong credentials, malformed input, a `ValueError`
raised before any network call happens at all). Retrying the second kind
just burns the attempt ceiling on a request that will fail identically every
time. Never widen `is_retryable_external_error` to swallow a validation or
programming error to "be safe" — that turns a bug that should crash loudly
into one that silently eats three attempts and a few seconds of backoff
before crashing anyway.

## Rate limiting counts every attempt, including retries

`ratelimit.MinIntervalThrottle.before_request()` is passed as
`retry_external_call`'s `before_attempt`, so it fires once per *attempt*, not
once per logical call — a failed 429 attempt still consumes the account's
rate budget, because it really was issued against the provider. Skipping the
throttle on a failed attempt would let the immediately-following retry land
inside the same interval, doubling the effective rate exactly when the
provider has just told you to slow down. The required test drives a client
through a failure-then-success and asserts the gap between the *issued*
timestamps, not the call count:

```python
def test_retried_attempts_keep_the_minimum_issue_interval(self):
    """A retry attempt is a request and resets the same clock (Issue #253)."""
    ...
    assert timeline.gaps_below(_MIN_REQUEST_INTERVAL_SECONDS) == []
```

Finnhub's cap (`FINNHUB_MIN_REQUEST_INTERVAL_SECONDS = 1.05`,
`ratelimit.py`) is per *account*, not per client object: `text/news_finnhub.py`
and `data/earnings_finnhub.py` share one API key, so
`pipeline/daily_composition.py` injects one `MinIntervalThrottle` instance
into both (Issue #263) rather than letting each client bound only itself.
`throttle` and a client's own `rate_clock`/timing fields are mutually
exclusive — passing both raises `ValueError`, because a shared budget can
only be measured on one clock; silently preferring one would leave the other
caller believing a timeline that never actually runs.

## The offline contract

The default `pytest` suite never touches the network.
`tests/conftest.py`'s autouse `_block_real_network` monkeypatches
`socket.socket.connect` to raise `AssertionError` the instant anything tries
to cross an uninjected boundary — this fixture must stay, and a new adapter
test that trips "Real network access is forbidden" means an HTTP call was
not faked, not that the guard is wrong. Fakes are injected at the *port*:
every client takes an `http_get`-shaped callable (`_HttpGet` Protocol in
`text/news_finnhub.py`), a `Clock`, and a `sleep_fn`/rate clock as
constructor arguments, defaulting to the real implementation in production.
Never fake by patching something two layers down (`httpx.Client.send`,
module-level `requests` internals) — patch the seam the adapter itself
declares.

This repository has no separate "live" pytest marker; the offline suite *is*
the whole automated contract, and it is what CI's success signal depends on.
A manual credential-backed sanity check against a real provider (if ever
needed) is not part of that signal — never let its result gate the offline
suite's pass/fail, and never write it as a `pytest` test that only happens to
skip without credentials, since that reads as a collected-and-skipped test
rather than something structurally excluded.

## Fatal vs. fail-soft is the pipeline's call, not the adapter's

An adapter reports failure — it raises after exhausting retries, or (for a
batch fetch) returns per-symbol failures like
`data/base.py`'s `BarFetchResult.failures` — but it does not decide whether
that failure should abort the run or degrade it. That boundary is a design
decision recorded once, in the pipeline composition, not a per-adapter
improvisation: a price-fetch failure is fatal (FR-12), a text-collection
failure degrades (NFR-04). **REQUIRED:** `wiring-the-pipeline` for where that
boundary is drawn and how a new adapter's failure gets wired into it.

## Text adapters return untrusted content

A `text.base.TextItem`'s `content_text` (news body, filing excerpt, calendar
note) is untrusted the moment it leaves the adapter: it is prose from an
outside source, not code this repository wrote, and it must never be
interpolated into anything executable (a SQL string, a shell command, a
format string later `eval`'d) and never trusted as its own provenance — an
adapter stamps `source_id`, `source_url`, `filed_at`/`published_at` itself
from the *response envelope*, never by parsing something the article body
claims about itself. What downstream analysis is allowed to do with this
untrusted text, and how a skill's citations get checked against it, is
**REQUIRED:** `guarding-analysis-boundary`.

## Adapter review checklist

- Does every external call carry an explicit numeric timeout?
- Does the retry path go through `retry_external_call`, and does a test
  assert the exact attempt count and backoff sequence with a fake `sleep_fn`?
- Is `is_retryable_external_error` (or a narrower predicate) the only thing
  deciding retry — no `except Exception: retry` anywhere?
- Does a test prove a non-retryable error (auth, malformed request) fails on
  the first attempt with zero sleeps?
- If this adapter shares an account/rate budget with another client, is the
  throttle injected and shared rather than defaulted per-instance?
- Does a retry test prove the throttle fires on the failed attempt too, not
  only on the eventual success?
- Is every network/clock/sleep boundary reached through an injected
  constructor argument, never module-level monkeypatching two layers deep?
- Does the new test run clean under the existing offline suite — no real
  socket connection, no `pytest.mark.skip`-style live escape hatch?
- Does the wrapped library itself retry underneath `retry_external_call`
  (a `stamina`/`tenacity`/hand-rolled loop around the function the adapter
  actually calls), and if so, is it disabled or otherwise accounted for
  (Issue #429)?
