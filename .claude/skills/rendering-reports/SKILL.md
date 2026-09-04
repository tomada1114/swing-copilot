---
name: rendering-reports
description: >
  Covers src/swing_copilot/report/**: turning a completed run into the
  Markdown/terminal/Discord artifacts a human reads (markdown_report.py,
  terminal_report.py, discord_notify.py, verdict_notification.py,
  incomplete_runs.py, rejections.py, history_cli.py, daily_brief.py) and the
  honesty obligations rendering owes. Use when changing a report/*.py module,
  adding a new rendered field or run-outcome message, deciding what a
  withheld or degraded candidate looks like on screen, wiring
  scripts/notify_daily.py, or reviewing a diff under report/**.
---

# Rendering Reports

**Owns:** `src/swing_copilot/report/**` — assembling `DailyBrief`
(`daily_brief.py`) and rendering it to stdout (`terminal_report.py`),
`reports/<run_date>/<run_id>.md` (`markdown_report.py`), Discord
(`discord_notify.py`, `verdict_notification.py`), plus the run-completeness
(`incomplete_runs.py`), non-candidate (`rejections.py`), and read-only history
(`history_cli.py`) artifacts. **Does not own:** CON-03 checking itself, which
happens at ingest before anything reaches rendering
(`guarding-analysis-boundary`); atomic file replacement mechanics
(`writing-storage-code`, `io_atomic`); the read-only web dashboard, which
carries its own `src/swing_copilot/dashboard/AGENTS.md`; what a verdict
outcome means after the fact (`scoring-verdict-outcomes`); computing the
regime/exposure/FTD state this layer displays (`writing-regime-gates`) —
`daily_brief.py` only copies `RegimeSnapshot`/`ExposureDecision` fields into
`BriefRegime`/`BriefExposure`, never recomputes them.

## Output surfaces: canonical vs. derived

| Surface | Path | Status |
|---|---|---|
| Per-run Markdown | `reports/<run_date>/<run_id>.md` | Canonical, synced to R2 |
| Analysis audit trio + rejections | `reports/<run_date>/<run_id>/{analysis_input,analysis_result,report_context,rejections}.json` | Canonical, synced to R2 |
| `latest.md` | `reports/latest.md` | Local convenience copy, never synced |
| Terminal output | stdout | Ephemeral, not archived |
| Discord message(s) | Discord webhook | Side effect only, no artifact |

`render_markdown` (`markdown_report.py`) and `render_terminal`
(`terminal_report.py`) both take the same `DailyBrief` — per
`docs/05_ui_design.md` §3, neither renderer fetches data, computes an
indicator, or makes a risk decision; they format values `daily_brief.py`
already assembled. Rejecting a renderer-side database read or market-store
call in review is not pedantry: it is how the two surfaces stay unable to
disagree.

## Withheld stays visible — a missing row is worse than a shown one

`daily_brief.py::build_analysis_brief` collapses every unhappy path to
`degraded=True` with an explanatory `conclusion`, never a silently absent
section: `PENDING_ANALYSIS_MESSAGE` ("分析待ち…") when analysis has not run,
`MISSING_ANALYSIS_MESSAGE` ("定性分析なし") when the candidate has no entry,
`WITHHELD_ANALYSIS_MESSAGE` ("検証不合格のため非表示") when
`analysis/validate.py` withheld it fail-closed. All three keep the candidate's
row, score, and risk figures on screen — only the qualitative section changes
— and `format_verdict` returns `None` rather than a fabricated verdict line
whenever `analysis.degraded` is true, with the comment in
`markdown_report.py::_candidate_section` stating the reason directly:
"silence must never read as '懸念なし'".

`verdict_notification.py::_withheld_block` follows the identical shape one
layer further out: a symbol `analysis/validate.py` withheld still gets its
own `■ {symbol}` block in the Discord message, with
`_WITHHELD_CON03_NOTE` or `_WITHHELD_GENERIC_NOTE` explaining *why*, never a
block that simply doesn't exist. Dropping the entry would read as "nothing to
say about this symbol" to an operator who never saw the raw analysis; keeping
it, with the reason, reads as "this was suppressed on purpose." Reject a diff
that makes a withheld/degraded/pending case stop emitting a row, block, or
line — the fix is always a new labeled state, never an early `continue`.

## An incomplete run must not render as a complete one

`incomplete_runs.py` exists because `copilot-daily` writes
`analysis_input.json` and stops; the run only *finishes* when the following
`/swing-daily` skill session writes `analysis_result.json` back into the same
directory. If that session dies partway, `runs.status` stays `success` and
the directory still exists — a naive "does a directory exist for the previous
business day?" preflight can never see the gap, because the directory itself
was created by the deterministic pipeline, not by the missing analysis.
`find_incomplete_runs` classifies every such directory into an
`IncompleteRunKind` (`ANALYSIS_MISSING`, `SAME_DAY_SUPERSEDED`,
`PIPELINE_UNFINISHED`, `RUN_ROW_MISSING`, `HISTORICAL_REPLAY`), and only
`ANALYSIS_MISSING`/`RUN_ROW_MISSING` are `is_actionable` — the others are
listed for visibility but do not fail `copilot-history incomplete`'s exit
code, because they either aren't a gap at all (a same-day sibling completed,
or an `--as-of` replay stamped with `HISTORICAL_REPLAY_FILENAME` never owed
one) or are already visible through `runs.status`. The classification is read
once, in this one module, so the daily preflight, the CLI, and the dashboard
banner cannot disagree about the same directory.

`verdict_notification.py` carries the same discipline into the notification:
`build_daily_notification` branches on the outcome file's `outcome` field
first (`preflight_abort` vs. abnormal vs. `success`/`degraded`) before it ever
tries to read `analysis_result.json`, and a `success`/`degraded` outcome whose
result file is missing gets `_no_analysis_message`, not a message that quietly
omits the verdict section. A missing/unreadable outcome file itself is
genuinely ambiguous — `_read_outcome` returns `None` and the caller sends an
explicit "終了状態が確認できませんでした" message rather than guessing a status.

## Never invent imperative wording in a template

CON-03 is enforced centrally at `copilot-ingest-analysis`, over every
user-visible field of the *ingested* analysis — see `guarding-analysis-boundary`
for the mechanism. That check runs before a template exists; it cannot see
wording a renderer's own f-string introduces afterward. This layer's own
model for taking that seriously is `verdict_notification.py::_safe_block`,
which re-runs `analysis/safety.py::check_display_texts` a second time over
each *fully assembled* Discord block, right before it is queued to send —
never trusting that ingest already made this redundant. A block that fails
this second check is withheld the same way an ingest-time violation is
(`_WITHHELD_CON03_NOTE`), and the day still gets its one message. Any new
copy this layer writes into a template — a header, a status label, a fallback
message — is exactly the gap the ingest-time check cannot cover; keep it
descriptive ("見送り推奨", "検証不合格のため非表示") and never phrase it as an
instruction to act.

## Determinism and diffability

Two runs of the same inputs must render the same bytes, or an operator diffing
`reports/<date>/<run-1>.md` against a re-rendered copy sees noise instead of
real change. The codebase's pattern for this: `daily_brief.py::_rejection_counts`
tallies with `Counter` but always emits `sorted(counts.items())`;
`rejections.py::_payload` sorts rejections by `symbol` and truncations by
`rank`, with the module's own docstring stating why — "a rerun of the same
`as_of` produces a byte-identical file, which is what makes two run
directories diffable at all." A new section that iterates a `dict` or `set`
directly into rendered lines, without an explicit sort, reintroduces exactly
the non-determinism this pattern exists to avoid. `generated_at` is the one
deliberate exception — it is part of what a fresh run reports, not something
`copilot-ingest-analysis`'s re-render should vary, and `analysis/snapshot.py`
archives the built `DailyBrief` (including `generated_at`) into
`report_context.json` precisely so re-rendering reuses it instead of
re-stamping a new wall-clock value.

## Notification delivery cannot fail the run's own record

`DiscordNotifier.notify()` never raises — see the module docstring's cited
`docs/04_detailed_design.md` divergence note: it returns `bool` instead of the
originally documented `-> None` specifically so a caller can detect a failed
send without the failure propagating as an exception. `scripts/notify_daily.py`
runs as a separate CI step, `always()` plus `continue-on-error: true`, entirely
after the day's R2 push already happened — a failed Discord send changes that
step's own exit code, never the run's `success`/`degraded`/`failed` status.
Never log the webhook URL itself on a failure path; see `designing-errors` for
the shared secret-redaction convention (`configure_cli_logging`).

## A renderer formats values, it never recomputes them

Every score, risk, and execution-state figure a candidate shows travels
straight from the screening/risk pipeline through `daily_brief.py` (`getattr`
off `candidate.metrics`, or copied field-by-field in `_risk_brief`) into a
`_number`/`_money`/`_percent`/`_one_r` format call in `terminal_report.py` or
`markdown_report.py` — neither renderer performs arithmetic on a screening or
sizing number. Where this layer does derive a value (`_fundamentals`'s
PER/FCF/equity-ratio from raw fundamentals, `_market_items`'s day-over-day
`pct_change`), the derivation happens exactly once, inside `daily_brief.py`,
so both renderers consume the same already-computed field and cannot drift
apart. `verdict_notification.py::_per_share_risk` is the one explicitly
labeled exception to "never recompute": its docstring states plainly that
every other figure in a proceed block is `RiskAssessment`'s own value shown
unchanged, and this one subtraction (`limit_price - stop_price`) exists only
because the account size needed to turn it into a share count is unknown to
this product. A new derived value anywhere in `report/**` needs the same
explicit justification, or it belongs in `daily_brief.py`'s single shared
assembly step instead.

## Review checklist for a diff under `report/**`

- A new degraded/withheld/pending state: does it still emit a row/block with
  a stated reason, or does it make the entry disappear?
- A new outcome or notification path: does it distinguish
  `success`/`degraded`/`failed`/`preflight_abort` explicitly, and can any of
  the non-success paths ever produce the same message shape as a clean run?
- New free-text copy introduced in a template (not echoed from an ingested
  analysis field): is it descriptive, never imperative, and — if it reaches
  Discord — does it pass through `_safe_block`'s second CON-03 check?
- A new computed/derived value: does it live once in `daily_brief.py` (or
  carry the same explicit one-line justification `_per_share_risk` does),
  rather than being computed separately in each renderer?
- A new list built from a `dict`/`set`: is it sorted before becoming rendered
  lines, the way `_rejection_counts` and `rejections.py::_payload` are?
- A new Discord/webhook code path: does a failed send return `False`/log
  without raising, and does no log line ever include the webhook URL?
- A new atomic write under `reports/`: does it go through `io_atomic`
  (`writing-storage-code` owns the mechanics), not an in-place `open(..., "w")`?
