GOAL: Finish swing-copilot's P2 checklist items P2-4 through FINAL (report rendering, paper trading journal, full 9-step pipeline wiring, and final verification) on the existing branch `feat/p1-p2-implementation`, preserving the reviewed architecture — do not redesign it.

CONTEXT: Resolve every path below from the repository root. Before changing anything, read in this order:
1. `docs/goal-prompts/swing-copilot-p1p2-implementation/decisions.md` (D1–D10, still binding)
2. `docs/goal-prompts/swing-copilot-p1p2-implementation/checklist.yaml` (the work queue and current item status; verify it against fresh Git/tests before relying on it)
3. `docs/goal-prompts/swing-copilot-p2-report-paper-wrapup/design.md` (target design for P2-4/P2-5 — read fully before starting P2-4)
4. `docs/goal-prompts/swing-copilot-p2-report-paper-wrapup/decisions.md` (pre-answered questions for this phase)
5. `docs/04_detailed_design.md`, `docs/05_ui_design.md`, `docs/mockups/ui-mockup-morning-briefing.html`

BASELINE: Checklist items P1-0 through P2-3 are marked done and P2-4/P2-5/P2-6/FINAL remain, but derive the current branch, worktree, implementation state, test result, and coverage from fresh commands. Do not copy test counts or cleanliness from this prompt. Do NOT redo P1-0..P2-3 unless fresh evidence exposes a regression required by the active item. Never read, edit, or print `.env`; preserve unrelated worktree changes.

DO: Execute checklist.yaml's remaining items (P2-4, P2-5, P2-6, FINAL) in order. For every behavior change: write the exact acceptance scenario first, run it and paste the failing output, implement to green, then commit implementation, regression test, and any canonical design correction in the same logical commit. Apply `AGENTS.md` and `docs/04_detailed_design.md` 8.5 to changed paths. Update status only after fresh acceptance output. Where design.md gives a concrete contract, implement it; do not invent a different shape for something already specified.

DONE WHEN: All of P2-4/P2-5/P2-6/FINAL show fresh passing acceptance output; `just verify` exits 0 on the committed tree; branch is `feat/p1-p2-implementation`; `git status --short` shows no uncommitted implementation changes. Then print exactly `GOAL_DONE: P1+P2 offline verification passed`.

VERIFY: Each item's own acceptance command from checklist.yaml, plus `just verify` and `git status --short`, all pasted with fresh output/exit codes — no prose-only claims or historical test counts.

CONSTRAINTS:
- Integrity: never skip/xfail/disable/delete tests, weaken assertions, lower the 95% coverage gate, or stub the unit under test. `tests/report`/`tests/paper` must stay fully offline (no real network/webhook/chart-JS download) — inject fakes.
- Scope: only P2-4/P2-5/P2-6/FINAL. No EODHD/P4, brokerage APIs, servers, schedulers, or unrelated cleanup. Follow design.md §2.1 exactly on fundamentals scope (no EPS YoY / profitability-streak).
- Never touch `.env`, `.agents/`, `.codex/`. Never use `--no-verify` or force-push. Do not push to remote.
- Divergence rule: distinguish a stale support-file fact from a canonical requirement. Do not silently choose between canonical design and schema/API; preserve compatibility, record the divergence, update the stale canonical source when authorized, or apply decisions.md's fallback/stop.

STOP RULES: Stop after 70 turns. If one item stays red after 4 materially different fixes, preserve green commits, leave it pending, and print `GOAL_STOPPED: <item> blocked — <reason>`.
