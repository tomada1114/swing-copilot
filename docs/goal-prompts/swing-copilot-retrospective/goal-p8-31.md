GOAL: Implement roadmap item P8-31 of swing-copilot's retrospective mechanism: the `export` stage of `copilot-retro` — aggregate metrics, signal-performance bundle, human-alignment cross-tab, source-contribution table, surprise dossiers with freshness data, config snapshot, and an atomically written `retro_input.json` under strict schema `retro-input-v1` — plus `RetroConfig` and the `prepare` umbrella subcommand. New branch, ending with an open PR.

CONTEXT: Resolve every path from the repository root. Read in this order:
1. `docs/goal-prompts/swing-copilot-retrospective/design.md` — §3.4 (metric definitions and rationale), §5.3 (export contract), §10
2. same dir `decisions.md` (D1–D10; D5/D6 govern threshold and primitive reuse)
3. same dir `execution-decisions.md` (ED1–ED7 and E31.1–E31.5)
4. same dir `research.md` (R0, R3, R4, R7; `compute_signal_performance` in R1)
5. `docs/06_reliability_roadmap.md` — the P8-31 seed

BASELINE: Preflight — P8-30 must already be on main: `verdict_outcomes` DDL present in `src/swing_copilot/storage/schema.py` and `copilot-retro` `collect`/`evaluate` subcommands implemented. If not, print `GOAL_STOPPED: P8-31 requires P8-30 on main` and stop (ED2 — do not re-implement P8-30). Derive all state from fresh commands. Preserve unrelated worktree changes; never read or print `.env`.

DO: Create branch `feat/p8-31-retro-export` from main. Per ED3 (test-first, one logical unit per commit):
(a) Add `RetroConfig` per E31.1 and wire it into `Settings` (research R4), with strict-validation tests (unknown field rejected, bounds enforced).
(b) Add `retro-input-v1` strict models to `retro/schemas.py` per design §5.3 items 1–8 and E31.2 (schema_version constant, input_digest).
(c) Implement the §3.4 aggregates: per-horizon and weight-composed values, preliminary flag below `preliminary_sample_threshold`, separation, proceed severe-miss rate with baseline comparison, skip hit rate, human-alignment cross-tab (join per E31.5), source contribution. Tests assert hand-calculated expected values, empty-data behavior, and the n<20 preliminary flag boundary.
(d) Implement surprise selection (MISS_SEVERE both directions, cap at `max_surprises` by |forward_return| with the dropped count reported — no silent cap) and freshness fetch through the existing text adapters per E31.3; offline tests with injected fakes cover the success path and the fail-soft fetch-failure path.
(e) Add the `export` subcommand (atomic write, imitate `write_json_atomically`) and the `prepare` umbrella per E31.4.
Finish per ED1: push and open the PR.

DONE WHEN: `just verify` exits 0 on the committed branch; every 動作確認 bullet of the roadmap P8-31 seed is demonstrated by fresh pytest output; `git status --short` clean; PR open. Then print exactly `GOAL_DONE: P8-31 just verify exit 0, PR <url>`. If push/PR still fails after ED1's retries, print `GOAL_DONE: P8-31 just verify exit 0, PR skipped (<reason>)`.

VERIFY: narrowest targets first (`tests/retro/`, `tests/test_config.py`), then `just verify` and `git status --short` — fresh output pasted.

CONSTRAINTS:
- Integrity: never skip/xfail/disable/delete tests, weaken assertions, lower the 95% coverage gate, or stub the unit under test; suite stays offline (socket guard), fakes injected.
- Scope: only P8-31. No `ingest`, no ledger, no skill, no changes to text adapters or postmortem. Thresholds come from `settings.postmortem` (D6) — do not duplicate them into `RetroConfig`. No new dependencies (ED6).
- ED1–ED7 apply; divergence rule ED4. Conventional Commits; never `--no-verify`, force-push, or `uv.lock` edits.

STOP RULES: Stop after 55 turns. If one unit stays red after 4 materially different attempts, preserve green commits and print `GOAL_STOPPED: P8-31 <unit> blocked — <reason>`.
