# swing-copilot

[![CI](https://github.com/tomada1114/swing-copilot/actions/workflows/ci.yml/badge.svg)](https://github.com/tomada1114/swing-copilot/actions/workflows/ci.yml)
[![codecov](https://codecov.io/gh/tomada1114/swing-copilot/branch/main/graph/badge.svg)](https://codecov.io/gh/tomada1114/swing-copilot)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

> **⚠️ Archived on 2026-09-08.** The scheduled daily run is disabled and this
> repository is read-only. The code works; the project was stopped because the
> live loop could not produce decision-grade evidence in a useful timeframe.
> See [Why this was archived](#why-this-was-archived) for the measurements and
> the reasoning, including what was *not* the reason.

A decision-support batch pipeline for US equity swing/position trading. Its
deterministic batch can run locally: it screens the S&P 500 universe, checks
risk parameters, collects news and filing text, prints a readable terminal
brief, and archives Markdown. It never places orders; the human always makes
the final buy/sell decision (see `docs/01_requirements.md`).

Qualitative analysis of that news and filing text is done by Claude Code
skills, not by this process. The GitHub Actions `swing-daily.yml` job invokes
the `swing-daily` skill after `copilot-daily` exports `analysis_input.json`,
then runs `copilot-ingest-analysis` to machine-verify the answer (schema,
source provenance, CON-03) before re-rendering the report. The qualitative
skill is CI-only; it is not a local interactive workflow. The slice command
uses the ignored CI scratch directory:

```bash
uv run copilot-export-slices reports/2026-07-29/<run-id> \
  --out-dir .swing-daily-scratch/slices
```

## Why this was archived

The pipeline ran unattended on weekdays from **2026-07-29 to 2026-09-07** — 29
runs over 25 run dates, roughly six weeks. It never traded real money; every
position below is a virtual tracking-ledger entry. This section records what
that period measured, so a future reader does not have to re-derive it.

### The decisive finding: the loop barely traded

Of 192 verdicts, **7 were `proceed`** (3.6%) and 185 were `skip`. Those 7
appeared on **3 of 21 verdict dates**, and 6 of them landed in the first two
sessions. The regime layer allowed new entries on only 7 of 29 runs
(`CASH_PRIORITY` 9, `REDUCE_ONLY` 13); `binding_constraint` was `regime` on 89
scorecard rows. Over the same window SPY returned **+3.79%**.

And of those 7, only **one** was an actual buy proposal rather than a
counterfactual ledger entry (see below). Six weeks of unattended operation
produced a single tradeable signal.

Sitting in cash through a rising market was a far larger drag than anything the
stock selection did. More importantly, at that rate, reaching a sample that
could settle the strategy question (~20 matured `proceed` positions under one
config, per issue #412's own restart trigger — let alone across three regimes)
would take **years**. The live loop is an operational-validation device, not an
evidence-generating one. Issue #412 names the sharper version of this: the
system stopped producing the very signal its feedback loop needs, a
self-concealing state where "no `proceed` ever fires" is itself the finding.

### What the outcomes looked like — and why the headline number misleads

The observation that started this review was "all 7 `proceed` verdicts lost".
That is literally true and analytically almost worthless. **Six of those seven
were `no_trade=true` days**: the risk engine had rejected every candidate
(`binding_constraint` = `regime` ×4, `earnings` ×2), and the ledger opened
counterfactual positions purely to score verdict quality. They were never buy
proposals. They were also produced by gate logic that no longer exists —
#345 (regime redesign from EMA50+VIX to SMA200+FTD), #112 (SEVERE thresholds),
#352 (account-dependent rules removed) and #241 (earnings demotion) all landed
afterwards.

**Under the current configuration, exactly one real buy proposal was ever made:
HWM on 2026-08-24, stopped out in 4 sessions at −5.85%. n = 1.** Issue #412
established this on 2026-09-02 and correctly held off the retrospective loop
for it; this review re-derived the same conclusion with five more days of data.

So the cohort table below describes a system that has since been partly
replaced. Read it as context, not as a verdict on the current logic:

| Cohort | Mean | Win rate | n |
|---|---|---|---|
| `proceed`, closed (6 of 7 counterfactual, retired logic) | −5.99% | 0% | 7 |
| `proceed`, blind 20-day hold | −4.81% (−9.61% vs SPY) | 0% | 5 |
| `skip`, closed | −3.37% (−2.65% ex. a stale row) | 25% | 64 |
| `skip`, blind 20-day hold | +1.73% (+0.97% vs SPY) | 48% | 52 |

There is no evidence the `proceed` cohort did worse than `skip`, and none that
it did better. That comparison is simply unmeasured.

One structural defect is visible without any statistics: **67 of 71 closed
positions (94.4%) exited on the trailing stop**, at a mean stop distance of
~4.6% of entry, against a 25-session (five-week) max hold. Only 4 of 71 reached
`max_hold`, and those averaged **+10.60%**. A ~4.6% trailing stop sits inside a
single name's ordinary five-week noise, so the exit rule and the holding period
were never compatible. For the `skip` cohort, blind-holding 20 days (+1.73%)
beat the stop-managed result (−3.37%) by about 5pp.

No edge was detectable in screening or ranking. Candidates versus rejected
names: −0.39% vs −0.58% at 5 days (n=48 / 2952), and −1.41% vs −0.06% at 20
days (n=32 / 1963) — candidates were *worse* at the longer horizon. The rank
correlation between composite score and forward return was +0.070 at 5 days
(n=132) and −0.205 at 20 days (n=57), with the top score tercile performing
worst.

### Read those numbers with the caveats

They do **not** show that individual-stock swing trading cannot work:

- Six weeks, one regime. SPY's max drawdown in the window was −2.52% and VIX
  sat at 14–20 — a low-volatility grinding bull, which is simultaneously the
  friendliest environment for index buy-and-hold and the harshest for
  stop-managed single-name swing trading.
- The `proceed` sample is 1 real proposal, plus 6 counterfactual entries from
  two adjacent sessions under retired logic — not 7 independent observations.
- The control-group comparisons rest on only 4–7 independent dates, roughly
  eight different cuts were tried (multiple comparisons), and nothing is
  regime-adjusted — the known weakness recorded as R7 in
  `docs/08_architecture_review_2026-08.md`.

The honest summary is "no edge was demonstrated", not "no edge exists".

### What was *not* the reason

An unadjusted 2:1 split in `APH` produced a phantom −48.86% tracking row
(entry recorded at 155.55 on a day the stock closed at 77.775). That row is a
**pre-fix residue, not an open bug**: split re-basing was already designed,
found broken, and repaired across #418, #419, #420, #422, #426 and #450 between
2026-09-02 and 2026-09-05, and `copilot-track rebuild` exists to reconstruct
affected positions.

The repair demonstrably worked. `MNST` had the same contamination — issue #412
recorded its 20-day outcome as −50.8%, which alone dragged the `proceed` 20-day
mean to −14.35%. After the fix and backfill it reads −1.66% / −1.83% / −3.01%,
and the same cohort mean is −4.81%. `APH` is simply a row the rebuild has not
been re-run over. The ledger handles corporate actions; data hygiene is not why
this stopped.

### If this is ever revisited

Answer the strategy question in `backtest/` over multiple years and at least
three regimes — it is universe-agnostic and already exists — rather than in the
live loop. Fix the exit-rule/holding-period mismatch and the trade-frequency
collapse first, and treat both as hypotheses to test rather than thresholds to
tune (`docs/08_architecture_review_2026-08.md`, principle 3: never change a
setting on a point estimate alone).

An ETF pivot was considered and rejected. It would have addressed none of the
defects above, and `docs/10_investment_philosophy.md` adopts its CAN SLIM /
SEPA lineage explicitly for *stock selection and entry quality* — the regime
gate, ATR stops and R-measurement carry over to ETFs, but the financial-quality
premise and the news/filings analysis layer do not.

## Quickstart

```bash
uv sync --all-groups
cp .env.example .env  # fill in API keys for the features you enable
uv run copilot-daily --dry-run
```

The final decision-support brief is written to stdout. Generated Markdown is
stored under `reports/<run-date>/<run-id>.md`, with `reports/latest.md` as a
convenience copy. DuckDB remains the source of truth.

Review past runs, candidates, and rejections read-only with:

```bash
uv run copilot-history runs
uv run copilot-history symbol AAPL
```

For ad-hoc analysis in Python (notebooks, scripts), `swing_copilot.research`
returns the accumulated history as pandas DataFrames over read-only DuckDB
connections — e.g. `research.scorecard()` joins each verdict to its forward
return, score breakdown, market regime, tracked exit, and sector in one call.
See `docs/09_research_guide.md`:

```python
from swing_copilot import research

df = research.scorecard()
df.groupby(["gate_verdict", "recommendation"])["forward_return_pct"].mean()

# ...and the control groups: what the screen threw away also has a return.
universe = research.universe_forward_returns()
universe[universe.outcome_class == "rejected"].groupby("reason_code")[
    "forward_return_pct"
].mean()
```

To browse the same history in a browser instead, `copilot-dashboard` serves a
read-only local viewer — one run's overview, one symbol's reasoning, and the
history of both. It binds `127.0.0.1` only, never writes, and renders its
charts as server-side inline SVG, so it works with the machine offline:

```bash
uv run copilot-dashboard              # http://127.0.0.1:8787
just dashboard                        # same, via Just
```

Backtest a strategy over a historical window, with risk-adjusted metrics
(Sharpe, max drawdown, win rate, profit factor, expectancy, R-multiple):

```bash
uv run copilot-backtest --strategy default --start 2025-01-01 --end 2026-06-30 --limit 30
```

Add `--pessimistic` to also run a higher-slippage scenario (1.75x) and print a
normal-vs-pessimistic comparison, checking the strategy doesn't rely on
unrealistically favorable fills.

`copilot-backtest` opens the existing DuckDB read-only; initialize or pull the
database first when using a new `--db` path.

Backtest sizing uses `backtest.sim_trade_risk_pct`,
`backtest.sim_position_cap_pct`, and `backtest.max_concurrent_positions` as
nominal simulation values. They are not production account settings or
investment advice values.

`--policy` decides which of the production entry gates the simulation applies
between a candidate and a fill. Pass several arms to compare them over one
identical candidate stream, so the difference is attributable to the gates and
nothing else:

```bash
uv run copilot-backtest --strategy default --start 2025-01-01 --end 2026-06-30 \
    --limit 30 --policy none,regime,regime+earnings
```

Check whether a strategy is overfit to its ATR-stop/max-hold parameters with a
sensitivity grid:

```bash
uv run copilot-backtest grid --strategy default --start 2025-01-01 --end 2026-06-30 --limit 30
```

Measure the five fixed entry-limit ATR-multiple values with the same candidate
stream:

```bash
uv run copilot-backtest entry-grid --strategy default --start 2025-01-01 --end 2026-06-30 --limit 30
```

Diagnose configured thresholds read-only, without touching `settings.yaml`.
`copilot-filter-matrix` applies each screening filter/signal independently to
the whole universe; `copilot-dd-forward` replays the stored history and reports
the forward return and drawdown that followed each Distribution Day level,
isolating the levels themselves rather than a whole gated strategy the way
`copilot-backtest --policy` does:

```bash
uv run copilot-filter-matrix --as-of 2026-07-29
uv run copilot-dd-forward --as-of 2026-08-06
```

See `docs/00_human_preparation.md` for the full setup checklist and
`docs/03_basic_design.md` / `docs/04_detailed_design.md` for the architecture.

## Development

See [CONTRIBUTING.md](CONTRIBUTING.md) for full setup instructions.

```bash
uv sync --all-groups
uv run pre-commit install --install-hooks
just fmt
just verify
```

`just verify` is the fast, non-mutating pre-PR gate: lint and type checks, a
strict docs build, and only the tests the current diff can plausibly affect
(with a 90% line+branch coverage floor on the changed files). CI runs the
full gate — the whole suite at 95% line+branch coverage, plus a wheel smoke
test — on every PR regardless, so `just verify` deliberately does not
duplicate that locally; run `just verify-full` for the same full gate before
a release or a direct-to-main completion claim. For packaging verification
alone, run `just smoke` (or `uv build --wheel && uv run python scripts/smoke_test.py`)
to install the freshly built wheel into a temporary virtual environment and
confirm the distribution imports from the wheel, not from `src/`.

## Documentation

- [Investment Philosophy](https://tomada1114.github.io/swing-copilot/10_investment_philosophy/)
- [Getting Started](https://tomada1114.github.io/swing-copilot/getting-started/)
- [API Reference](https://tomada1114.github.io/swing-copilot/reference/)
- Design docs: `docs/01_requirements.md`, `docs/03_basic_design.md`,
  `docs/04_detailed_design.md`, `docs/05_ui_design.md`

## License

[MIT](LICENSE)
