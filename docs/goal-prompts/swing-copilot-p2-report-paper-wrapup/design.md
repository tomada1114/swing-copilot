# Design: report/, paper/, and final pipeline wiring (P2-4 → FINAL)

This covers the parts of `docs/04_detailed_design.md` §3.18–3.21 and `docs/05_ui_design.md` that
are incomplete, ambiguous, or diverge from the actual current codebase. Where those docs already
give a precise, unambiguous contract (colors, typography, chart JS options, prompt text), this file
does not repeat it — read the docs directly. This file exists only for what you would otherwise
have to guess.

## 1. `report/chart_data.py`

Follow `docs/05_ui_design.md` §10.1 and §8.4 exactly for `OHLCVPoint`/`SMAPoint`/`ChartData` and the
JSON shape. One detail the doc doesn't spell out:

**SMA200 lookback buffer**: to show correct SMA50/SMA200 values across a 6-month display window,
`read_bars()` must be called with `start` pushed back by enough *trading* days to seed the SMA200
window before the display window begins — not just 6 calendar months. Use a buffer of 200
*calendar* days beyond the 6-month display start (safely covers 200 trading days) purely for the
SMA calculation input; then slice the output `ohlcv`/`sma50`/`sma200` lists back down to the actual
6-month display range before returning `ChartData`. Reuse `screening.indicators.sma` — do not
reimplement SMA math here (`docs/04_detailed_design.md` 2.1 #5 shared-indicator invariant).

`time` fields are `date.isoformat()` strings (`yyyy-mm-dd`). Points where SMA is `NaN` (insufficient
history) are omitted from `sma50`/`sma200`, not zero-filled — use `math.isnan()` per this repo's
established pandas-NaN convention (see `risk/checks.py`, `backtest/engine.py`).

## 2. `report/html_report.py`

### 2.1 Fundamentals scope (resolved — do not expand)

`MarketStore` currently has no read method for the `fundamentals` table at all (only
`upsert_fundamentals`). Add exactly one:

```python
def get_latest_fundamentals(self, symbol: str, as_of: date) -> FundamentalsRecord | None:
    """Most recent fundamentals row for `symbol` filed at or before `as_of`, or None."""
```

The report's "ファンダメンタル" block shows only what one `FundamentalsRecord` can answer directly:
- PER = `as_of` close / (`net_income` / `shares`) — show `"N/A"` if `net_income`, `shares`, or the
  close price is missing/non-positive.
- FCF (raw `fcf` value, or `"N/A"` if `None`).
- 自己資本比率 (equity ratio) = `equity / assets` — `"N/A"` if either is `None` or `assets == 0`.
- 直近EPS = `net_income / shares` — `"N/A"` if either is `None`/zero.

**Do NOT implement** "EPS YoY" or "黒字継続 N/4四半期" (the mockup shows these, but they require a
multi-quarter fundamentals history query that doesn't exist yet and is out of scope for this pass —
user decision, 2026-07-21). Omit those two rows from the template entirely rather than faking a
value. List this as a known follow-up in your final report; do not add a new query method to chase
it.

### 2.2 `classify_change` and badge mapping

`classify_change(pct: float) -> Literal["up", "down", "neutral"]` per `docs/05_ui_design.md` §3.3
thresholds (±0.1%), implemented once in this module and reused by market strip, summary table,
sparklines, and detail cards (never re-implement the threshold elsewhere).

Signal badge Japanese labels per §6.1's table (`trend_sma` → "SMA200上抜け", `pullback_rsi` →
"RSI押し目"; `volume_min` is never badged). An unknown `signal_name` (future Filter/Signal you
haven't mapped yet) must still render — show the raw key, HTML-escaped — never drop it silently.

### 2.3 Template location and rendering

Match the repo tree in `docs/04_detailed_design.md` §2 exactly: `templates/report.html.j2` and
`reports/assets/` live at the **repository root** (sibling to `src/`), not inside the package —
same convention as `config/settings.yaml` (see `config.load_settings(path: str =
"config/settings.yaml")`, resolved relative to CWD, not `Path(__file__)`). Mirror that: default
`templates_dir: str = "templates"`, `output_dir: str = "reports"` parameters on `render_report()`
(or a small grouped request dataclass if that pushes the function over 5 args), resolved relative
to CWD, overridable by tests with a `tmp_path`-based value. Do not invent a `Path(__file__)`-based
resolution scheme.

Use `jinja2.Environment(loader=FileSystemLoader(templates_dir), autoescape=True)` explicitly (don't
rely on `select_autoescape`'s extension sniffing — `.j2` won't match its defaults). Pass chart JSON
into the template with the `| tojson` filter so `<`, `>`, `&`, `</script>` are escaped automatically
(`docs/05_ui_design.md` §8.4) — never mark LLM/news/company-name strings `| safe`.

**Visual reference**: `docs/mockups/ui-mockup-morning-briefing.html` (absolute path:
`/Users/masuyama/ghq/github.com/tomada1114/swing-copilot/docs/mockups/ui-mockup-morning-briefing.html`)
is the literal CSS/HTML/structure to reproduce in `templates/report.html.j2` and
`reports/assets/style.css` — copy its `<style>` block into `style.css` (drop the mockup-only
`.mockup-banner` rule), and copy its section markup, replacing static dummy content with Jinja2
variables/loops. The one structural deviation: replace the mockup's static `<svg class="candle-chart">`
per-card with a live chart mount (`<div id="chart-{{ symbol }}">` + a `<script>` block calling
`LightweightCharts.createChart` per `docs/05_ui_design.md` §8.2–8.4, using the vendored
`assets/lightweight-charts.standalone.production.js`).

### 2.4 Atomic writes

Write to a temp file in the **same directory** as the destination (so the rename is same-filesystem
and atomic), then `os.replace()` it onto `reports/{run_date}.html`; only on success, repeat for
`reports/latest.html`. A failure writing the dated file must leave any previous `latest.html`
untouched.

### 2.5 Fail-soft LLM block

When `news_summaries`/`filing_analyses` passed to `render_report()` are `None` (steps 5/6 failed,
`docs/03_basic_design.md` §7 fail-soft), the LLM block renders a fixed degraded message
("本日はニュース・開示分析を取得できませんでした") — every other card block (chart, technical,
fundamentals, risk) renders normally. Never hide the whole card.

## 3. `report/discord_notify.py`

Implement the `Notifier` Protocol and `DiscordNotifier` exactly per
`docs/04_detailed_design.md` §3.18. `DiscordNotifier.notify()` must catch every exception from the
webhook call (network errors, non-2xx responses) and never raise — on failure it's the caller's
(`pipeline/daily.py`, step 8) job to record a `run_steps` failure; `discord_notify.py` itself has no
`StateStore` dependency. Keep the HTTP call injectable (same `httpx`-post-function-parameter pattern
as `text/news_finnhub.py`'s `_HttpGet`) so tests never hit a real network/webhook.

## 4. `scripts/fetch_assets.py`

One-time vendoring script per `docs/05_ui_design.md` §10.5 — **not** part of the daily batch and
**not** exercised by `tests/report` (those tests must stay fully offline). Tests that need
`reports/assets/lightweight-charts.standalone.production.js` to exist should write a small stub file
themselves into a `tmp_path`-based assets dir — never invoke `fetch_assets.py` or touch the network
from a test. If `render_report()`'s chart step needs to detect a missing vendored JS file (per
§10.5's acceptance criterion), that check only needs to run when actually invoked through
`pipeline/daily.py` (P2-6), not as a `report/html_report.py`-level hard dependency for every test.

## 5. `paper/journal.py` (FR-11, CON-04)

`docs/04_detailed_design.md` §3.20's shown signatures are stale placeholders — they don't match the
actual `trades_journal`/`positions` schema (`storage/schema.py`). Use the schema as ground truth:

```python
class PaperJournal:
    """Wraps StateStore — does not own a second connection to positions/trades_journal."""

    def __init__(self, state_store: StateStore) -> None: ...

    def record_decision(
        self, run_id: UUID, symbol: str, strategy_key: str,
        decision: str,  # "followed" | "ignored" | "modified"
        reason_memo: str | None, virtual_fill_price: float | None,
    ) -> None:
        """Upsert trades_journal keyed on (run_id, symbol, strategy_key) — re-recording the
        same key updates the row (idempotent), it does not insert a duplicate."""

    def close_position(self, position_id: UUID, close_date: date, close_price: float) -> None:
        """Close an open paper position via StateStore.upsert_position(status='closed', ...).

        Raises a domain error (define in paper/journal.py, derive from SwingCopilotError) if
        position_id doesn't exist or is already closed — closing must be a real state transition,
        not a silent no-op.
        """

    def summarize_performance(self, market_store: MarketStore, as_of: date) -> PerformanceSummary:
        """Closed paper trades' aggregate P&L/win-rate vs. a SPY buy-and-hold benchmark over the
        same span (earliest closed entry_date .. as_of), mirroring backtest/engine.py's benchmark
        idea but over real paper trades instead of a simulation."""
```

```python
@dataclass(frozen=True, slots=True)
class PerformanceSummary:
    closed_trade_count: int
    total_pnl_usd: float
    win_rate: float          # fraction of closed trades with positive P&L; 0.0 if none closed
    spy_return_pct: float | None  # None if SPY bars are insufficient for the span
```

`record_decision`'s idempotent upsert needs a new `StateStore.record_trade_decision(record)` method
that delegates to a new `storage/paper_records.py` (same extraction pattern as `llm_records.py`/
`audit_records.py` — keeps `state_store.py` from growing past the 300-line guideline). `close_position`
needs a `StateStore.get_position(position_id) -> Position | None` (there's currently only
`get_open_positions`, no single-position lookup) — add it. `summarize_performance` needs
`StateStore.get_closed_positions(is_paper: bool = True) -> list[Position]` (sibling to the existing
`get_open_positions`).

## 6. P2-6 pipeline wiring (steps 5–9)

`docs/03_basic_design.md` §4/§7 fixes the 9-step order and fail-soft boundary: steps 1–4 (prices,
fundamentals, screening, risk — already wired) are fatal on failure; steps 5 (text collection), 6
(LLM analysis) are fail-soft — their failure degrades steps 7/8 to a screening-only report, it does
not stop the run; steps 7 (report), 8 (Discord notify), 9 (auto-open) must always attempt to
complete. Extend `pipeline/daily.py`'s existing steps list/try-except pattern (see `_run_step_risk`
and the `steps` tuple in `run_daily()`) — do not restructure what's already wired for steps 1–4.

`_maybe_open_report()` (step 9) is specified verbatim in `docs/05_ui_design.md` §10.3 — implement it
as shown, including the `CI` env var check and the `is_dry_run`/`no_open` guards.

The offline five-symbol E2E smoke test (`tests/test_e2e_smoke.py`) and
`tests/pipeline/test_failsoft.py` must inject fakes for every port (`DataProvider`, `TextProvider`,
`EdgarClient`, `LLMClient`, `Notifier`, `Clock`) — no real network/API calls anywhere in the test
suite, matching every existing test module's pattern in this repo.
