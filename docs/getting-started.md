# Getting Started

## Installation

```bash
git clone https://github.com/tomada1114/swing-copilot.git
cd swing-copilot
uv sync --all-groups
```

Copy `.env.example` to `.env` and fill in the API keys for the features you
enable (see `docs/00_human_preparation.md`).

## Basic Usage

```bash
uv run copilot-daily --dry-run
```

!!! note
    The S&P 500 universe list (`config/universe_snapshot.csv`) is not shipped
    with the repository; it is auto-generated on the first successful fetch
    from Wikipedia and then serves as the offline/failure fallback for every
    later run. The first run therefore requires network access to Wikipedia.

!!! note
    `--dry-run` never touches the live database or report output: it reads
    and writes `data/copilot_dry_run.duckdb` and `reports/dry_run/` instead
    of `data/copilot.duckdb` and `reports/`. It never sends a Discord notification,
    even if notifications are
    enabled in `settings.yaml`. It still uses real data providers (EDGAR,
    Finnhub, FRED, yfinance) over the real network. Drop `--dry-run` for a
    normal live run.

The batch prints its final brief to stdout and stores a generated Markdown
snapshot at `reports/<run-date>/<run-id>.md`. Progress logs go to stderr.
To record a decision after reviewing the brief:

```bash
uv run copilot-decision \
  --run-id <run-id> \
  --symbol AAPL \
  --decision followed \
  --fill-price 225.80 \
  --reason "出来高増加を確認"
```

The command writes to DuckDB and refreshes the generated decision section in
the run's Markdown file. Do not edit generated Markdown as a source of truth.

## What's Next?

See the [API Reference](reference.md) for the complete API documentation, and
`docs/03_basic_design.md` / `docs/04_detailed_design.md` for the architecture.
