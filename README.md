# swing-copilot

[![CI](https://github.com/tomada1114/swing-copilot/actions/workflows/ci.yml/badge.svg)](https://github.com/tomada1114/swing-copilot/actions/workflows/ci.yml)
[![codecov](https://codecov.io/gh/tomada1114/swing-copilot/branch/main/graph/badge.svg)](https://codecov.io/gh/tomada1114/swing-copilot)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

A decision-support batch pipeline for US equity swing/position trading. It
screens the S&P 500 universe, checks risk parameters, collects news and filing
text, prints a readable terminal brief, and archives Markdown — all run
locally. It never places orders; the human always makes the final buy/sell
decision (see `docs/01_requirements.md`).

Qualitative analysis of that news and filing text is done by Claude Code
skills, not by this process: `copilot-daily` exports `analysis_input.json`
beside the day's report, the `swing-daily` skill analyzes it, and
`copilot-ingest-analysis` machine-verifies the answer (schema, source
provenance, CON-03) before re-rendering the report.

## Quickstart

```bash
uv sync --all-groups
cp .env.example .env  # fill in API keys for the features you enable
uv run copilot-daily --dry-run
```

The final decision-support brief is written to stdout. Generated Markdown is
stored under `reports/<run-date>/<run-id>.md`, with `reports/latest.md` as a
convenience copy. DuckDB remains the source of truth.

Record a human decision against an audited candidate with:

```bash
uv run copilot-decision \
  --run-id <run-id> \
  --symbol AAPL \
  --decision ignored \
  --reason "相関リスクが高いため"
```

Review past runs, candidates, rejections, and paper-trading performance
read-only with:

```bash
uv run copilot-history runs
uv run copilot-history symbol AAPL
uv run copilot-history performance
```

Backtest a strategy over a historical window, with risk-adjusted metrics
(Sharpe, max drawdown, win rate, profit factor, expectancy, R-multiple):

```bash
uv run copilot-backtest --strategy default --start 2025-01-01 --end 2026-06-30 --limit 30
```

Add `--pessimistic` to also run a higher-slippage scenario (1.75x) and print a
normal-vs-pessimistic comparison, checking the strategy doesn't rely on
unrealistically favorable fills.

Check whether a strategy is overfit to its ATR-stop/max-hold parameters with a
sensitivity grid:

```bash
uv run copilot-backtest grid --strategy default --start 2025-01-01 --end 2026-06-30 --limit 30
```

Diagnose configured thresholds read-only, without touching `settings.yaml`.
`copilot-filter-matrix` applies each screening filter/signal independently to
the whole universe; `copilot-dd-forward` replays the stored history and reports
the forward return and drawdown that followed each Distribution Day level, which
`copilot-backtest` cannot measure at all (it never imports `regime/`):

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

`just verify` is the non-mutating PR/release gate: lint and type checks, tests
with 95% line+branch coverage, a strict docs build, and wheel smoke testing.
For packaging verification alone, run `just smoke` (or `uv build --wheel && uv run python scripts/smoke_test.py`)
to install the freshly built wheel into a temporary virtual environment and
confirm the distribution imports from the wheel, not from `src/`.

## Documentation

- [Getting Started](https://tomada1114.github.io/swing-copilot/getting-started/)
- [API Reference](https://tomada1114.github.io/swing-copilot/reference/)
- Design docs: `docs/01_requirements.md`, `docs/03_basic_design.md`,
  `docs/04_detailed_design.md`, `docs/05_ui_design.md`

## License

[MIT](LICENSE)
