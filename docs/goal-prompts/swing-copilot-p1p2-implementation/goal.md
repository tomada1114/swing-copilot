GOAL: Implement P1 and P2 of swing-copilot in the existing repository at `/Users/masuyama/ghq/github.com/tomada1114/swing-copilot`, preserving the reviewed architecture and producing a verified local branch.

CONTEXT: Before changing files, read in order:
1. `/Users/masuyama/ghq/github.com/tomada1114/swing-copilot/docs/goal-prompts/swing-copilot-p1p2-implementation/decisions.md`
2. The same directory's `research.md` and `checklist.yaml`
3. `docs/01_requirements.md`, `docs/03_basic_design.md`, `docs/04_detailed_design.md`, then `docs/05_ui_design.md`
`docs/04_detailed_design.md` sections 2.1 and 2.2 are the architecture contract; implement them, do not redesign them.

BASELINE: This is already a Git repository on `main`, with template code and reviewed docs committed. Do not copy another template and do not run `git init`. At authoring time lint/mypy pass and 8 template tests pass at 100% coverage. `.env` is absent. `.agents/` and `.codex/` are pre-existing untracked user files; never stage, edit, delete, or commit them. Reviewed docs and `mkdocs.yml` may be dirty when this run starts; follow decisions.md D2.

DO: Execute `checklist.yaml` in strict order. For every behavior change, write the test first, run it and paste the failing assertion/error, implement to green, then commit that logical unit with its tests. Update an item's status only after pasting its fresh acceptance output. Print `progress: <done>/<total>; next: <id>` after each item.

DONE WHEN: Every checklist item is `done`; fresh output shows `just check`, `uv run pytest tests/test_e2e_smoke.py -v`, and `uv run mkdocs build --strict` exit 0; the current branch is `feat/p1-p2-implementation`; and `git status --short` contains no implementation changes. Then, and only immediately after that evidence, print exactly `GOAL_DONE: P1+P2 offline verification passed`. If a live canary is possible, also run it and report its result; live credentials/network are not required for this sentinel.

VERIFY: Use each checklist acceptance command. Final verification is the three commands in DONE WHEN plus `git status --short` and `git log --oneline --decorate -20`. Paste summaries and exit codes from the current code; prose claims are not evidence.

CONSTRAINTS:
- Integrity: never skip/xfail/disable/delete tests, weaken assertions, lower coverage, add broad ignores, stub the unit under test, or mock domain/application code. Fakes are allowed only at external ports and clock/filesystem boundaries.
- Scope: P1+P2 only. Do not implement EODHD/P4, brokerage APIs, servers, schedulers, plugin discovery, or unrelated cleanup.
- Follow the architecture contract: modular monolith; functional core/imperative shell; boundary-only ports/adapters; Parquet plus one DuckDB; explicit `as_of`; deterministic candidates; no TA-Lib, backtesting.py, or SQLite.
- Never read aloud, edit, stage, or commit `.env`. Never stage `.agents/` or `.codex/`. Never use `--no-verify` or force-push. Do not push.
- If repository facts contradict support-file facts, trust the repository and report the divergence. If a specified design is impossible, apply decisions.md; otherwise stop and report, never silently invent another design.

STOP RULES: Stop after 90 turns. If one item remains red after 4 materially different fixes, preserve green commits, leave its status pending, and print `GOAL_STOPPED: <item> blocked — <reason>`. Dependency/auth/network failure may degrade only the live canary, never offline checks.
