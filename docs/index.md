# swing-copilot

A decision-support batch pipeline for US equity swing/position trading. It
screens the S&P 500 universe, checks risk parameters, exports the collected
news and filings for a Claude Code skill to interpret, prints a terminal brief,
and archives generated Markdown. It never places orders — the human always
makes the final buy/sell decision.

## Quick Example

```bash
uv run copilot-daily --dry-run
```

## Next Steps

- [Getting Started](getting-started.md) — setup and first steps
- [API Reference](reference.md) — full API documentation
- [Contributing](contributing.md) — how to contribute
