# Getting Started

## Installation

```bash
git clone https://github.com/tomada1114/swing-copilot.git
cd swing-copilot
uv sync --all-groups
```

Copy `.env.example` to `.env` and fill in the API keys for the features you
enable (see `docs/00_human_preparation.md`). `ANTHROPIC_API_KEY` is not one of
them: this project never calls a model API directly. Qualitative analysis
(news/filings/screening interpretation) runs entirely inside a Claude Code
skill (Stage 2 below), so no LLM billing is incurred by the Python pipeline
itself; the only requirement is a working Claude Code environment that can
read this repository's `.claude/skills/`.

## Two-stage Daily Workflow

A daily run has two stages: a deterministic Python pipeline (Stage 1) and a
Claude Code skill that adds qualitative analysis on top of it (Stage 2). Stage
1 alone already produces a complete, decision-ready report; Stage 2 is
optional and only enriches it with narrative context. Either way, the final
buy/sell decision is always made by a human.

### Stage 1: `copilot-daily` (deterministic pipeline)

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

Other useful options: `--as-of YYYY-MM-DD` replays a past point-in-time date,
`--limit N` narrows the universe scan to `N` symbols (plus any held
positions), `--skip-text` skips news/filing collection (and therefore Stage
2's input), `--strategy <key>` selects a non-default strategy from
`config/strategies.yaml`, and `--log-level` controls stderr verbosity. Run
`uv run copilot-daily --help` for the authoritative list.

The batch collects prices/fundamentals, screens the strategy, checks
portfolio risk, and — as long as there are candidates and collected text —
exports `reports/<run-date>/analysis_input.json` for Stage 2. It prints its
final brief to stdout and stores a generated Markdown snapshot at
`reports/<run-date>/<run-id>.md`; progress logs go to stderr. Because no model
is called yet, the report's qualitative sections read as pending analysis,
and the terminal output ends with the absolute path to `analysis_input.json`
so it can be handed to Stage 2:

```text
詳細レポート: /path/to/reports/2026-07-28/<run-id>.md
分析入力(analysis_input.json): /path/to/reports/2026-07-28/analysis_input.json
```

If no candidates survive screening (or text collection produced nothing),
`analysis_input.json` is not exported and there is nothing for Stage 2 to do;
the Markdown report and terminal brief are still produced.

### Stage 2: the `swing-daily` Claude Code skill (qualitative analysis)

In a Claude Code session opened on this repository, run the `swing-daily`
skill (e.g. by asking for "日次分析" / "run the pipeline", or invoking it by
name). It re-runs `copilot-daily` itself if needed, then:

1. reads `analysis_input.json`;
2. fans out per-symbol news, filing, and screening interpretation to the
   `analyze-news`, `analyze-filings`, and `interpret-screening` expert skills,
   running them in parallel subagents regardless of symbol count;
3. reconciles their findings and decides a per-symbol `proceed`/`skip`
   verdict — a recommendation only, never an order, and never a rewrite of
   the deterministic scores/ranking;
4. writes `analysis_result.json` next to `analysis_input.json`; and
5. runs `copilot-ingest-analysis` to verify the result against strict
   schemas, `source_ids` provenance, and the CON-03 no-imperative-language
   check, then re-renders the Markdown report with only the qualitative
   sections filled in.

```bash
uv run copilot-ingest-analysis /path/to/reports/2026-07-28/analysis_result.json
```

`copilot-ingest-analysis` takes the result file (or its containing directory)
as its first argument and resolves `analysis_input.json` /
`report_context.json` from the same directory unless `--input` / `--context`
override them. It performs no network access and no re-screening; a symbol
that fails verification is withheld fail-closed (its qualitative section
stays pending) rather than retried, and a schema mismatch exits nonzero. Run
`uv run copilot-ingest-analysis --help` for the full option list.

If every symbol ends up as `skip`, or the market regime does not favor new
entries, the report may instead show a "本日は取引なし" (no trade today)
verdict with a reason. Either way, `swing-daily` never places an order —
every buy/sell decision is made by a human reading the final report.

## Tracking Verdicts

Every `proceed`/`skip` verdict is carried forward as a virtual position under
the same exit rules the backtest uses (ATR trailing stop, max hold), via
`copilot-daily`'s own `track_update` fail-soft step — no separate action is
required after reviewing the brief. To review the ledger or catch up
manually:

```bash
uv run copilot-track update --as-of 2026-07-28   # catch up manually if needed
uv run copilot-track list --status open          # unrealized P&L, stop, sessions left
uv run copilot-track show --symbol AAPL          # verdict reasons and daily marks
uv run copilot-track stats                       # win rate, PF, expectancy by verdict side
```

These commands are read-only except for `update`, which only ever replays the
deterministic exit rules — see [API Reference](reference.md) for the full
subcommand list.

## What's Next?

See the [API Reference](reference.md) for the complete API documentation, and
`docs/03_basic_design.md` / `docs/04_detailed_design.md` for the architecture.
