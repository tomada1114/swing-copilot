#!/usr/bin/env bash

# Codex cloud environment setup.
#
# Keep this script independent of repository secrets. Codex exposes secrets only
# while this setup phase runs, then removes them before the agent phase starts.

set -euo pipefail

readonly SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
readonly REPO_ROOT="$(cd -- "${SCRIPT_DIR}/.." && pwd)"

cd -- "${REPO_ROOT}"

if ! command -v uv >/dev/null 2>&1; then
    printf 'error: uv is required in the Codex cloud image\n' >&2
    exit 1
fi

# Use the lockfile so the cloud environment matches CI and local development.
uv sync --locked --all-groups

# Fail during setup if the interpreter or project installation is unusable.
uv run python - <<'PY'
import sys

import swing_copilot

if sys.version_info < (3, 14):
    raise SystemExit(
        f"Python 3.14+ is required, got {sys.version.split()[0]}"
    )

print(f"swing-copilot {swing_copilot.__version__} ready")
PY
