# swing-copilot

[![CI](https://github.com/tomada1114/swing-copilot/actions/workflows/ci.yml/badge.svg)](https://github.com/tomada1114/swing-copilot/actions/workflows/ci.yml)
[![codecov](https://codecov.io/gh/tomada1114/swing-copilot/branch/main/graph/badge.svg)](https://codecov.io/gh/tomada1114/swing-copilot)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

A decision-support batch pipeline for US equity swing/position trading. It
screens the S&P 500 universe, checks risk parameters, summarizes news and
filings with an LLM, and renders a daily HTML report — all run locally with a
single command. It never places orders; the human always makes the final
buy/sell decision (see `docs/01_requirements.md`).

## Quickstart

```bash
uv sync --all-groups
cp .env.example .env  # fill in API keys for the features you enable
uv run copilot-daily --dry-run
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
For packaging verification alone, run `just smoke` (or `uv build && uv run python scripts/smoke_test.py`)
to install the freshly built wheel into a temporary virtual environment and
confirm the distribution imports from the wheel, not from `src/`.

## Documentation

- [Getting Started](https://tomada1114.github.io/swing-copilot/getting-started/)
- [API Reference](https://tomada1114.github.io/swing-copilot/reference/)
- Design docs: `docs/01_requirements.md`, `docs/03_basic_design.md`,
  `docs/04_detailed_design.md`, `docs/05_ui_design.md`

## License

[MIT](LICENSE)
