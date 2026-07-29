GOAL: Implement roadmap item P8-33, the final phase of swing-copilot's retrospective mechanism: author the `.claude/skills/swing-retro/` skill, commit the initialized empty proposal ledger, promote the design into `docs/04_detailed_design.md` as a new §3.23, and mark the P8 roadmap seeds complete. New branch, ending with an open PR.

CONTEXT: Resolve every path from the repository root. Read in this order:
1. `docs/goal-prompts/swing-copilot-retrospective/design.md` — read fully; §6 (skill steps 1–7), §7, §8 are the skill's contract, the whole document feeds the §3.23 promotion
2. same dir `decisions.md` (D1–D10; D9/D10 shape the skill's gating and apply flow)
3. same dir `execution-decisions.md` (ED1–ED7 and E33.1–E33.3)
4. same dir `research.md` (R8)
5. `docs/06_reliability_roadmap.md` — the P8-33 seed
6. `.claude/skills/swing-daily/SKILL.md` — the structural model to imitate

BASELINE: Preflight — P8-30..P8-32 must all be on main: `copilot-retro` implements `collect`, `evaluate`, `export`, `prepare`, and `ingest`. If not, print `GOAL_STOPPED: P8-33 requires P8-30..32 on main` and stop (ED2). Derive all state from fresh commands. Preserve unrelated worktree changes; never read or print `.env`.

DO: Create branch `feat/p8-33-retro-skill` from main. Per ED3, one logical unit per commit:
(a) Author `.claude/skills/swing-retro/SKILL.md` (+ `references/` as needed) per E33.1, which enumerates the mandatory content: design §6 steps 1–7, the L1 immediate-apply and L2/L3 `AskUserQuestion` flows, the per-run self-question discipline, D10 ledger status recording, and referencing (not copying) `analysis-conventions.md`.
(b) Commit the initialized empty ledger `docs/retro/proposals.md` produced by the same header logic ingest uses (E32.1) — add a test or grep evidence proving the committed header matches what ingest generates, so the formats cannot diverge.
(c) Promote the design into `docs/04_detailed_design.md` §3.23 per E33.2: data model, maturity/as_of semantics (explicitly contrasting `signal_outcomes.as_of` per D7), evaluation framework, both schema contracts, ingest validation, approval model. Follow the existing §3.x style and density; `just docs-check` must pass strict.
(d) Update `docs/06_reliability_roadmap.md`: mark P8-30..P8-33 seeds complete following the convention used by earlier completed seeds (verify the convention in git history first, E33.2).
No production Python changes are expected; if a genuinely required small fix emerges (e.g. docs-check failure), keep it minimal and report it explicitly.
Finish per ED1: push and open the PR.

DONE WHEN: `just verify` exits 0 on the committed branch (this includes docs-check strict); the E33.3 structural checks are demonstrated with pasted grep/command output; `git status --short` clean; PR open. Then print exactly `GOAL_DONE: P8-33 just verify exit 0, PR <url>`. If push/PR still fails after ED1's retries, print `GOAL_DONE: P8-33 just verify exit 0, PR skipped (<reason>)`.

VERIFY: `just docs-check` early and after each doc unit; E33.3 grep checks; then `just verify` and `git status --short` — fresh output pasted.

CONSTRAINTS:
- Integrity: never skip/xfail/disable/delete tests or weaken assertions; do not relax mkdocs strictness to make docs-check pass.
- Scope: only P8-33. Running the skill against real data is out of scope (E33.3). Do not modify `copilot-retro` behavior, the daily flow, or other skills. No new dependencies (ED6).
- ED1–ED7 apply; divergence rule ED4 — if the implemented schemas/CLI on main disagree with design.md prose, the promotion into §3.23 must document the implemented reality and record the divergence, not silently restate stale design. Conventional Commits; never `--no-verify`, force-push, or `uv.lock` edits.

STOP RULES: Stop after 45 turns. If one unit stays blocked after 4 materially different attempts, preserve green commits and print `GOAL_STOPPED: P8-33 <unit> blocked — <reason>`.
