GOAL: Finish swing-copilot's P2 checklist items P2-4 through FINAL (report rendering, paper trading journal, full 9-step pipeline wiring, and final verification) on the existing branch `feat/p1-p2-implementation`, preserving the reviewed architecture — do not redesign it.

CONTEXT: Before changing anything, read in this order:
1. `/Users/masuyama/ghq/github.com/tomada1114/swing-copilot/docs/goal-prompts/swing-copilot-p1p2-implementation/decisions.md` (D1–D10, still binding)
2. `/Users/masuyama/ghq/github.com/tomada1114/swing-copilot/docs/goal-prompts/swing-copilot-p1p2-implementation/checklist.yaml` (the work queue — see BASELINE below, its `status` field is stale)
3. `/Users/masuyama/ghq/github.com/tomada1114/swing-copilot/docs/goal-prompts/swing-copilot-p2-report-paper-wrapup/design.md` (target design for P2-4/P2-5 — read fully before starting P2-4)
4. This directory's `decisions.md` (pre-answered questions for this phase)
5. `docs/04_detailed_design.md`, `docs/05_ui_design.md`, `docs/mockups/ui-mockup-morning-briefing.html`

BASELINE: Checklist items P1-0 through P2-3 (14 of 18) are already implemented and committed (verify with `git log --oneline`); their `status: pending` in checklist.yaml is stale — the file is never edited during execution, only printed as progress. Do NOT redo or re-verify P1-0..P2-3; start directly at P2-4. Only P2-4, P2-5, P2-6, FINAL remain. Current branch is `feat/p1-p2-implementation`, working tree is clean, and `just check` currently exits 0 (227 tests, 99.6% coverage). `.env` is populated with real keys — never read, edit, or print its contents.

DO: Execute checklist.yaml's remaining items (P2-4, P2-5, P2-6, FINAL) in order. For every behavior change: write the test first, run it and paste the failing output, implement to green, commit that logical unit with its tests in the same commit. Update status only after pasting fresh acceptance output (the file may stay unedited if that's this session's convention — printing `progress: <done>/<total>; next: <id>` after each item is what matters). Where design.md gives a concrete contract, implement it; do not invent a different shape for something design.md already specifies.

DONE WHEN: All of P2-4/P2-5/P2-6/FINAL show fresh passing acceptance output; `just check`, `uv run pytest tests/test_e2e_smoke.py -v`, and `uv run mkdocs build --strict` all exit 0; branch is `feat/p1-p2-implementation`; `git status --short` shows no uncommitted implementation changes. Then print exactly `GOAL_DONE: P1+P2 offline verification passed`.

VERIFY: Each item's own acceptance command from checklist.yaml, plus the three DONE WHEN commands and `git status --short`, all pasted with fresh output/exit codes — no prose-only claims.

CONSTRAINTS:
- Integrity: never skip/xfail/disable/delete tests, weaken assertions, lower the 95% coverage gate, or stub the unit under test. `tests/report`/`tests/paper` must stay fully offline (no real network/webhook/chart-JS download) — inject fakes.
- Scope: only P2-4/P2-5/P2-6/FINAL. No EODHD/P4, brokerage APIs, servers, schedulers, or unrelated cleanup. Follow design.md §2.1 exactly on fundamentals scope (no EPS YoY / profitability-streak).
- Never touch `.env`, `.agents/`, `.codex/`. Never use `--no-verify` or force-push. Do not push to remote.
- Divergence rule: if the repo contradicts a support-file fact, trust the repo and note it in your final report. If a design.md element proves impossible as specified, apply decisions.md's fallback or stop and report — never silently invent a different design.

STOP RULES: Stop after 70 turns. If one item stays red after 4 materially different fixes, preserve green commits, leave it pending, and print `GOAL_STOPPED: <item> blocked — <reason>`.
