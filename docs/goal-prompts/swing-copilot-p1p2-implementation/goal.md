GOAL: Implement P1 and P2 of swing-copilot in the current repository, preserving the reviewed architecture and producing a verified local branch. Resolve every path below from the repository root.

CONTEXT: Before changing files, read in order:
1. `docs/goal-prompts/swing-copilot-p1p2-implementation/decisions.md`
2. The same directory's `research.md` and `checklist.yaml`
3. `docs/01_requirements.md`, `docs/03_basic_design.md`, `docs/04_detailed_design.md`, then `docs/05_ui_design.md`
`docs/04_detailed_design.md` sections 2.1 and 2.2 are the architecture contract; implement them, do not redesign them.

BASELINE: This is already a Git repository with code and reviewed docs. Derive the current branch, status, implemented items, test results, and coverage from fresh Git/test commands; never trust test counts or cleanliness copied into this prompt. Do not copy another template and do not run `git init`. Never read, stage, edit, delete, or commit `.env`, `.agents/`, or `.codex/`. Preserve unrelated worktree changes and follow decisions.md D2.

DO: Execute pending `checklist.yaml` items in strict order. For every behavior change, write the acceptance scenario first, run it and paste the failing assertion/error, implement to green, then commit implementation, regression test, and any canonical design correction as one logical unit. Before marking done, apply the changed-path invariant review in `AGENTS.md` and `docs/04_detailed_design.md` 8.5. Update status only after pasting fresh output. Print `progress: <done>/<total>; next: <id>` after each item.

DONE WHEN: Every checklist item is `done`; fresh output from the committed tree shows `just verify` exit 0; the current branch is `feat/p1-p2-implementation`; and `git status --short` contains no implementation changes. Then, and only immediately after that evidence, print exactly `GOAL_DONE: P1+P2 offline verification passed`. If a live canary is possible, also run it and report its result; live credentials/network are not required for this sentinel.

VERIFY: Use each checklist acceptance command. Final verification is `just verify`, `git status --short`, and `git log --oneline --decorate -20`. Paste summaries and exit codes from the current code; prose claims, historical test counts, and coverage percentages are not evidence.

CONSTRAINTS:
- Integrity: never skip/xfail/disable/delete tests, weaken assertions, lower coverage, add broad ignores, stub the unit under test, or mock domain/application code. Fakes are allowed only at external ports and clock/filesystem boundaries.
- Scope: P1+P2 only. Do not implement EODHD/P4, brokerage APIs, servers, schedulers, plugin discovery, or unrelated cleanup.
- Follow the architecture contract: modular monolith; functional core/imperative shell; boundary-only ports/adapters; Parquet plus one DuckDB; explicit `as_of`; deterministic candidates; no TA-Lib, backtesting.py, or SQLite.
- Never read aloud, edit, stage, or commit `.env`. Never stage `.agents/` or `.codex/`. Never use `--no-verify` or force-push. Do not push.
- If a support file contradicts canonical requirements/design or the current schema/API, do not silently choose. Preserve compatibility, record the divergence, update the stale canonical source in the same change when authorized, or stop for a decision.

STOP RULES: Stop after 90 turns. If one item remains red after 4 materially different fixes, preserve green commits, leave its status pending, and print `GOAL_STOPPED: <item> blocked — <reason>`. Dependency/auth/network failure may degrade only the live canary, never offline checks.
