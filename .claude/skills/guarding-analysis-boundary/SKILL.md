---
name: guarding-analysis-boundary
description: >
  Covers the trust boundary between the deterministic pipeline and the
  qualitative analysis a Claude Code skill writes: the strict analysis_input.json
  / analysis_result.json schemas in src/swing_copilot/analysis/schemas.py,
  copilot-ingest-analysis (analysis/validate.py, analysis/cli.py), source_ids
  provenance, evidence_quote verification (analysis/evidence.py), and CON-03
  enforcement (analysis/safety.py). Use when changing analysis/schemas.py,
  analysis/validate.py, analysis/safety.py, or analysis/export.py, deciding
  what an ingest failure should hard-fail versus withhold, or reviewing a
  SourcedFact/VerdictReason field.
---

# Guarding the Analysis Boundary

**Owns:** the strict schemas in both directions, provenance proof,
`evidence_quote` verification, and CON-03 enforcement at
`copilot-ingest-analysis`. **Does not own:** how the analysis skills
themselves reason or are structured (`authoring-skills`; `swing-daily` and
`swing-retro` own their own procedure), fetching the underlying news/filing
text (`writing-external-adapters`), the deterministic screening/sizing/ranking
values an analysis is checked against (`checking-risk-math`,
`wiring-the-pipeline`).

## The boundary is two JSON documents, not a function call

Qualitative analysis runs in a Claude Code skill, never inside this process
(FR-08). The pipeline's step 6 writes `analysis_input.json`
(`analysis/export.py::build_analysis_input`/`write_analysis_input`) and stops.
A skill reads it, writes `analysis_result.json`, and
`copilot-ingest-analysis` (`analysis/cli.py::ingest`) reads both plus the
archived `report_context.json`, verifies the result, and re-renders the same
Markdown report with only the qualitative sections replaced. `ingest` never
opens a network connection and never re-runs screening, risk, or ranking —
it is inert with respect to everything deterministic.

Both documents parse under `StrictModel` (`extra="forbid"`,
`swing_copilot.strict_model`). An invented or renamed field fails loudly
instead of being silently dropped — that is the point. A schema that
tolerated unknown keys would let a field a skill *thinks* it wrote (because
an instruction changed, or it invented a plausible name) vanish on
`model_validate`, producing an analysis that looks complete and is not. The
strict boundary turns that into a `ValidationError` at ingest, which fails
loudly instead of shipping a quietly thinner report.

## Nothing a skill writes is trusted

Every `SourcedFact` (`analysis/schemas.py`) carries a non-empty
`source_ids: list[SourceId]` (`Field(min_length=1)`). `validate.py`'s
`_provenance_error` proves each cited ID is a *subset* of the IDs actually
exported for that symbol (news + filings + the run-wide calendar events) —
never merely "some ID that exists somewhere in the input." Citing another
symbol's `source_id` fails the same way an invented one does.

Provenance by ID is necessary but not sufficient: it proves a source was
*supplied*, not that the sentence was *written from* it. A 2026-07-30
incident showed the gap — an expert subagent read a different symbol's
filing, wrote the finding under its own symbol, and cited its own, entirely
correct, `source_id`. Every ID check passed. `SourcedFact.evidence_quote`
closes it: `validate.py::_evidence_error` proves the quote occurs verbatim
(via `evidence.py::normalize_evidence_text` — NFKC, typographic folding,
whitespace collapse, case-fold, `MIN_EVIDENCE_QUOTE_CHARS=12` to
`MAX_EVIDENCE_QUOTE_CHARS=300`) inside the body of one of the IDs the fact
cites. A statement written from a slice the writer was never given has no
such excerpt to offer, no matter how correct the cited ID is.

Code-owned metadata — filing form type, filed date, source URL — is
**resolved from `analysis_input.json`**, never echoed back from the result:
`FilingAnalysis` deliberately carries no `form_type`/`filed_at` field, and
`validate.py::verify_symbol_analysis` builds `ResolvedFiling` by joining the
result's `source_id` back to the candidate's own `FilingInput`. An echoed
value is unverifiable — a skill could type the wrong date and nothing would
catch it — so the report only ever shows what the code itself resolved.

Deterministic screening, sizing, and ranking values are never rewritten by an
analysis: `analysis/context.py` renders `score_breakdown`/`risk_constraints`
as pre-formatted text blocks so a narrative can be checked *against* the
code's own numbers, never so it can restate or override them, and
`_rebuild_brief` (`analysis/cli.py`) replaces only each candidate's
`analysis` field on the existing `DailyBrief` — every deterministic field
carries over untouched.

## CON-03: enforced centrally, at ingest, before anything reaches a report

CON-03 (`docs/01_requirements.md`'s constraint table): *"投資助言に該当する
出力（断定的な売買指示）をCLI・Markdown・通知に含めない。最終判断は人間。検査は
スキルへの指示だけに依存せず、`copilot-ingest-analysis`（`analysis/safety.py`
の純関数）が全ユーザー表示テキストへ一元適用し、違反銘柄はfail-closedで縮退表示
する"* — no output that amounts to investment advice (definitive buy/sell
instructions) may appear in the CLI, Markdown, or notifications; the final
decision is always the human's; and the check does not rely on instructing
the skill well — a pure function in `analysis/safety.py`, applied uniformly
by `copilot-ingest-analysis`, is the enforcement, and a violating symbol is
shown withheld rather than shown at all.

`safety.py::check_display_texts` runs `check_no_imperative_language` (a
`FORBIDDEN_PHRASES` tuple plus `_JAPANESE_TRADE_IMPERATIVE_PATTERN` /
`_ENGLISH_TRADE_OBLIGATION_PATTERN` regexes, both applied after
`unicodedata.normalize("NFKC", ...)`) and
`check_no_unevidenced_behavioral_claims` (a bare "investor sentiment/panic"
diagnosis is forbidden unless paired with a hedge phrase *and* a concrete
actual-vs-planned percentage in the same text). `validate.py::_display_texts`
enumerates every field a report would actually render — fact text, news and
filing `interpretation`/`risk_flags`/`red_flags`/`yoy_changes`, the
`ScreeningAssessment` summary/strengths/concerns, and every `VerdictReason`
text — so the check runs over what a reader sees, not over a hand-picked
subset. `no_trade_reason` gets the same check separately
(`_verified_no_trade_reason`), because it lives outside any one symbol's
section.

Skill instructions alone are insufficient — `.claude/skills/swing-daily`'s
`analysis-conventions.md` documents the same rules as AC3–AC5 for the writer,
but a model can still slip, so the machine check is the actual boundary, not
a courtesy backstop.

## Fail-closed per symbol, hard-fail for the whole run

Two different severities exist, and mixing them up is the anti-pattern to
reject in review:

- **Per-symbol, fail-closed, no retry**: a provenance failure, an
  unsupported `evidence_quote`, or a CON-03 violation withholds *that
  symbol's* qualitative section (`validate.py::_withheld`, which logs a
  warning and returns a `SymbolOutcome` whose `error` is set — every other
  field on that outcome then stays empty, so a caller cannot accidentally
  render withheld content by forgetting to check the flag). One bad symbol
  must not cost the whole day's report, and retrying is wrong: the failure
  is a property of what was *written*, not a transient fault, so retrying
  either reproduces the same failure or masks a real integrity problem by
  hoping for a different roll.
- **Hard failure for the whole run**: broken JSON, a schema violation, an
  `as_of` mismatch between `analysis_result.json` and `analysis_input.json`,
  or a missing/extra symbol in `result.symbols` versus
  `analysis_input.candidates` (`validate.py::_verify_complete_symbol_coverage`)
  all raise `AnalysisIngestError` before any symbol is processed. There is
  no safe partial reading of a file that may describe a different trading
  day — an `as_of` mismatch means the *entire document* could be answering
  yesterday's candidates, so nothing in it can be trusted symbol-by-symbol.
  `validate_artifact_identity` additionally hard-fails on `run_id`,
  `strategy_key`, and `input_digest` disagreement across
  `analysis_result.json`/`report_context.json`/`analysis_input.json` before
  any per-symbol check runs at all.

A fifth check, `numeric_consistency.py`'s figure comparison, is deliberately
**not** fail-closed — it stays a `logger.warning` because it compares digits
across unit systems the input never states, so a false positive must cost a
human a second look, not a withheld analysis.

## Design decision D2: `copilot-ingest-analysis` never touches the DB

`copilot-ingest-analysis` reads three local files and rewrites a report; it
never opens a database connection. `copilot-retro collect`
(`src/swing_copilot/retro/collect.py`) is the only path that fills the
`verdicts`/`verdict_sources` tables, by scanning
`reports/<date>/<run_id>/analysis_result.json` after the fact and upserting
idempotently. Keeping ingest DB-free means a re-run of `copilot-ingest-analysis`
against the same files is always safe (it only rewrites a Markdown file), and
the one place that persists verdicts can be re-run separately for backfill
without re-triggering report rendering.

## What this layer's tests owe

`tests/analysis/test_validate.py` is the model: `TestHardFailures` (schema
version, `as_of` mismatch, symbol-set mismatch, artifact-identity mismatch)
must raise `AnalysisIngestError`, never withhold; `TestProvenance` and
`TestEvidenceQuotes` must cover an unknown `source_id`, a cited ID from the
*wrong* symbol, a missing `evidence_quote`, and a quote present verbatim but
in the wrong body; `TestCon03` must cover a forbidden phrase, an imperative
pattern, and an unevidenced behavioral claim, each exercised through a real
`SymbolAnalysis` rather than by calling `safety.py` in isolation; and
`TestResolvedMetadata` must prove `form_type`/`filed_at` come from the input,
not the result. Every fail-closed case asserts the symbol comes back
withheld with a logged reason, not that the whole ingest raises.
