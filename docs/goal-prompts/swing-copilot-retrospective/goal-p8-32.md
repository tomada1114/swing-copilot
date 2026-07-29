GOAL: Implement roadmap item P8-32 of swing-copilot's retrospective mechanism: the `ingest` stage of `copilot-retro` — strict `retro-result-v1` validation, artifact-identity check, evidence-reference validation, central CON-03 inspection, re-proposal guard, `retro_report.md` rendering, and proposal-ledger generation/append. New branch, ending with an open PR.

CONTEXT: Resolve every path from the repository root. Read in this order:
1. `docs/goal-prompts/swing-copilot-retrospective/design.md` — §5.4 (ingest contract), §7 (failure_class enum), §8.1 (proposal fields), §8.2 (ledger semantics)
2. same dir `decisions.md` (D1–D10; D3/D10 govern the ledger and status ownership)
3. same dir `execution-decisions.md` (ED1–ED7 and E32.1–E32.4)
4. same dir `research.md` (R0, R3 — imitate `validate_artifact_identity` and `check_display_texts` usage)
5. `docs/06_reliability_roadmap.md` — the P8-32 seed

BASELINE: Preflight — P8-31 must already be on main: `copilot-retro` has `export` and `prepare` subcommands and `retro/schemas.py` defines retro-input-v1. If not, print `GOAL_STOPPED: P8-32 requires P8-31 on main` and stop (ED2). Derive all state from fresh commands. Preserve unrelated worktree changes; never read or print `.env`.

DO: Create branch `feat/p8-32-retro-ingest` from main. Per ED3 (test-first, one logical unit per commit):
(a) Add `retro-result-v1` strict models to `retro/schemas.py`: per-surprise narration with mandatory single `failure_class` from the closed 5-value enum (§7), proposals with the §8.1 mandatory fields (`proposal_key`, `level`, target, `evidence_refs`, `evidence_basis`, claim/expected effect, `verification_plan`, risks) and optional `reopen_justification`; unknown fields rejected.
(b) Implement the validation pipeline per §5.4: `as_of`/`input_digest` mismatch is a hard fail for the whole run (imitate `validate_artifact_identity`); `evidence_refs` must be a subset of the ID space defined in E32.4, violating proposals withheld; CON-03 via `analysis/safety.py` `check_display_texts` over every user-visible text field, violating proposal/narration withheld fail-closed per item, no retry.
(c) Implement the re-proposal guard per E32.2 against ledger rows with status rejected/verification_failed.
(d) Render `retro_report.md` (atomic replace, E32.3) and append to the ledger per E32.1: `docs/retro/proposals.md` one-line-per-proposal plus full text at `docs/retro/proposals/RP-NNN-<slug>.md`; ingest writes status=proposed only (D10) and generates the ledger with its header when absent.
(e) Add the `ingest` subcommand.
Tests must cover the mandatory matrix in E32.5.
Finish per ED1: push and open the PR.

DONE WHEN: `just verify` exits 0 on the committed branch; every 動作確認 bullet of the roadmap P8-32 seed is demonstrated by fresh pytest output; `git status --short` clean; PR open. Then print exactly `GOAL_DONE: P8-32 just verify exit 0, PR <url>`. If push/PR still fails after ED1's retries, print `GOAL_DONE: P8-32 just verify exit 0, PR skipped (<reason>)`.

VERIFY: narrowest targets first (`tests/retro/`), then `just verify` and `git status --short` — fresh output pasted.

CONSTRAINTS:
- Integrity: never skip/xfail/disable/delete tests, weaken assertions, lower the 95% coverage gate, or stub the unit under test; suite stays offline (socket guard) — ingest itself needs no network.
- Scope: only P8-32. No skill, no status transitions beyond `proposed` (those belong to the P8-33 skill per D10), no changes to export/collect/evaluate beyond what ingest genuinely requires, no config/code write paths from ingest (design §10), no new dependencies (ED6).
- ED1–ED7 apply; divergence rule ED4. Conventional Commits; never `--no-verify`, force-push, or `uv.lock` edits.

STOP RULES: Stop after 55 turns. If one unit stays red after 4 materially different attempts, preserve green commits and print `GOAL_STOPPED: P8-32 <unit> blocked — <reason>`.
