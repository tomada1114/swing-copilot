# Verified baseline and external facts

Verified on 2026-07-21 in `/Users/masuyama/ghq/github.com/tomada1114/swing-copilot`.

## Repository baseline

```text
git branch --show-current -> main
git log -2 --oneline ->
2aead48 docs: add product design docs for P1+P2 implementation
5eb6c54 Initial commit
origin -> https://github.com/tomada1114/swing-copilot.git
.env -> absent
git status --short -> ?? .agents/ and ?? .codex/
```

```text
just lint -> PASS (ruff check, ruff format --check, mypy)
just test -> PASS (8 passed, 100% coverage; threshold currently 80%)
uv run mkdocs build --strict -> could not start because the docs dependency group was not installed in the active environment
```

The repository is already a uv-template clone with Git history and design docs. The old bundle assumption that the target contained only `.env` was false and dangerous.

## Existing project rules

- `AGENTS.md`: strict src layout, public API via `__init__.__all__`, `just check`, typed/docstringed public APIs, update public docs, no unnecessary dependencies.
- `.claude/rules/python.md`: modules under 300 lines, functions under 40 lines, Protocol over ABC, frozen/slotted dataclasses internally, Pydantic only at boundaries, context managers, package exception hierarchy.
- `.claude/rules/testing.md`: behavior/contract tests, happy and error paths, `tmp_path`, boundary fakes, no skip, no sleep.
- `.claude/rules/pyproject.md`: runtime deps in project dependencies, lock with pyproject, 14-day `exclude-newer` cooldown.

## Template facts

- `scripts/bootstrap.py` and its tests already exist. Run it in place; do not copy a second template.
- Coverage flags occur in `justfile` and `.github/workflows/ci.yml`.
- Wheel build/smoke checks exist and remain useful for a CLI package.
- Pre-commit rejects direct commits on `main`; create the feature branch first.

## Official external facts checked during review

- yfinance `download` currently defaults to `auto_adjust=True` and multi-level columns for multiple tickers. Normalize this explicitly; never depend on defaults.
- SEC fair-access guidance limits automated access to at most 10 requests/second and requires a declared User-Agent identity.
- edgartools documents `set_identity(...)` and the `EDGAR_IDENTITY` environment variable.
- Claude structured outputs support JSON Schema and the Python SDK `messages.parse()` Pydantic helper for supported models.
- Lightweight Charts v5 replaced v4 `addCandlestickSeries`/`addLineSeries` methods with `addSeries(SeriesType, options)`.

Re-check exact installed APIs against official sources when implementing adapters; do not revisit the architecture decisions.
