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

## What's Next?

See the [API Reference](reference.md) for the complete API documentation, and
`docs/03_basic_design.md` / `docs/04_detailed_design.md` for the architecture.
