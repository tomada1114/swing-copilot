---
paths:
  - "pyproject.toml"
---

- Runtime dependencies go under `[project] dependencies`
- Dev dependencies go under `[dependency-groups] dev`; docs under `[dependency-groups] docs`
- Before adding a dependency: verify active maintenance, compatible license (MIT/BSD/Apache), and minimal transitive dependencies
- Use version ranges (`>=X.Y`) for runtime dependencies -- never pin exact versions in a library
- NEVER remove existing ruff rules without explicit user approval
- NEVER lower the coverage threshold (currently 80%)
- After modifying dependencies, run `uv sync --all-groups`
- The `uv.lock` file MUST be committed alongside dependency changes

## `[tool.uv] exclude-newer`

`exclude-newer` is a supply-chain cooldown: `uv lock` and `uv sync` ignore any
package version published after the given timestamp, so a dependency cannot be
resolved until it has survived in the wild for a while. This complements the
Dependabot `cooldown.default-days` setting in `.github/dependabot.yml`, which
delays *update PRs* by the same idea — together they keep both fresh installs
and automated upgrades off packages published in the last few days.

Bump cadence: whenever dependencies are updated, move the `exclude-newer`
timestamp forward to roughly "today minus 14 days"; do this at least monthly
even if no dependency changed, so the cutoff doesn't drift too far behind.

Procedure:

1. Edit the `exclude-newer` date in `pyproject.toml`.
2. Run `uv lock` to regenerate `uv.lock` against the new cutoff.
3. Commit `pyproject.toml` and `uv.lock` together in the same commit.
