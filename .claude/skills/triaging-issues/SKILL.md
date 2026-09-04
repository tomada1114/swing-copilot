---
name: triaging-issues
description: >
  Covers this repository's GitHub issue vocabulary: the `priority: P0`-`P3`
  labels, `blocked: design`, the `area:*` labels, the legacy `p1`/`p2`/`p3` and
  `phase-*`/`foundation`/`parallel`/`has-dependency`/`integration` labels still
  visible on closed issues, and what an issue body must contain to survive
  being picked up by a different session. Use when filing a GitHub issue,
  triaging or re-prioritizing the backlog, choosing a `priority:` label,
  writing a `Depends on #N` line, or running `gh label list` / `gh issue view`.
---

# Triaging Issues

**Owns:** this repository's issue vocabulary — which label carries a triage
decision, what each priority tier means, and what an issue body must contain.
**Does not own:** implementing an issue or PR conventions (`create-pr`); what
the resulting change breaks or which semver level it implies (`release-impact`).

Labels carry the triage decision so it is made once and read back, not
re-derived every time the backlog is looked at. `.github/ISSUE_TEMPLATE/bug_report.yml`
and `feature_request.yml` apply `bug` / `enhancement` at filing time; neither
sets a priority. Triage is the separate act of adding one.

## Priority labels

| Label | When to apply it |
|---|---|
| `priority: P0` | A real blocking chain (another open issue names it as the reason it can't start) or active damage — a broken main, a live vulnerability. Not "this feels urgent." |
| `priority: P1` | Foundational work — shared boundary, schema, or config — that later issues will build on, even before one names it as a dependency. Once one does, the resulting chain likely makes it P0 instead. |
| `priority: P2` | The default tier. Before leaving something here, check whether it actually blocks an open issue (P0) or is groundwork a later issue will need (P1) — P2 is not a parking space for work nobody has evaluated. |
| `priority: P3` | Genuinely low impact: nothing open depends on it and no chain runs through it. Not a synonym for "nobody wants to do this" — an issue that matters but is unappealing to implement belongs at its real tier, not P3. |

Priority ranks impact on the rest of the backlog, not how urgent or appealing
the work feels. `priority: P0` tracks a real blocking chain or active damage, so
an empty P0 tier is the normal state — reach for it when another open issue names
the blocker, not when something feels pressing.

## `blocked: design`

`blocked: design` means real, unresolved alternatives a human must choose
between — not "nobody has looked at it yet." Readiness and priority are
independent judgments: a `blocked: design` issue still gets a priority tier,
so it ranks correctly the instant the design question is answered. Never
leave a `blocked: design` issue untiered on the theory that the tier can wait
— it can't be re-derived later without redoing the judgment call.

The sibling label `blocked` (`Not shippable yet — a stated gate must be met
first`) is for a stated external gate, not a design choice. Both exist in
`gh label list`; use whichever matches what is actually unresolved.

A label that is wrong is corrected, not mentally overridden. Ranking around a
stale label in your head just leaves the next reader to repeat the mistake.

## Area labels

`area:regime`, `area:risk`, `area:screening`, `area:report`, `area:feedback`,
`area:llm`, `area:backtest`, `area:quality` mark which architectural layer an
issue touches (roughly the `src/swing_copilot/` package boundaries plus a
cross-cutting `area:quality`). They are informational routing, not a triage
decision by themselves — an `area:*` label never substitutes for a priority.

## Legacy label sets — do not triage with these

`gh label list` in this repo returns two generations of labels. Only
`priority: P0`-`P3`, `blocked`, `blocked: design`, and `area:*` are current;
everything below is legacy, kept on closed issues for history and carried by
zero open issues today:

- `p1` / `p2` / `p3` (lowercase) — an earlier priority scheme, superseded by
  `priority: P0`-`P3`.
- `phase-1` through `phase-6` — an earlier roadmap-phase grouping.
- `foundation`, `parallel`, `has-dependency`, `integration` — earlier
  scheduling/dependency metadata, superseded by the `Depends on #N` body
  convention below.

If an open issue still carries a legacy label, correct it to the current
label rather than reading it as if it still meant something — a stale label
left in place is what makes the next triage pass repeat the same confusion.

## Changelog-only labels are not triage labels

`.github/workflows/pr-label.yml` applies `enhancement` / `bug` / `documentation`
/ `ci` to a **pull request** from its title's Conventional Commit type
(`feat`/`fix`/`docs`/`ci`), purely so `.github/release.yml` can bucket the
generated changelog. This is a different mechanism from the issue-template
labels of the same name: it acts on PRs, is derived automatically from the
title, and carries no priority information. Never read a PR's `bug`/`enhancement`
label as if it were a triage decision, and never apply it to an issue by hand.

## What an issue body must contain

The GitHub issue forms only require a description, repro steps, and expected
vs. actual behavior (`bug_report.yml`) or a problem and proposed solution
(`feature_request.yml`) — neither field enforces what a session picking the
issue up later actually needs. Add these two things by hand, because nothing
else in the tracker can recover them:

- **What is wrong today, located with a `path:line`.** A symptom without a
  location forces whoever picks the issue up to re-find what the filer
  already knew — e.g. `daily_runner.py:24-55` in issue #400, not "the runner
  imports too many names."
- **An observable close condition, named as a test or a command.** Not a
  feeling of doneness ("cleaned up", "works correctly"). Issue #400's `## DoD`
  checklist is the model: each line is either a test name or a command
  (`just verify` 緑, a named contract test), never a description of intent.

## Ordering constraints

Write an ordering constraint as `Depends on #N` in a `## Dependencies`
section — one line per dependency, e.g. issue #399's `Depends on #394 — 同じ
ファイルを触るため、先にマージされていること`. This repo has no
dependency-specific label (`gh label list` has no `blocked: dependency`); the
body line is the only machine-parseable record of the edge, and it is what
downstream tooling (dependency-ordered backlog runs) greps for. Prose like
"after the guard work lands" is not machine-readable and will be missed.

When a blocking issue closes, whoever lands it clears the block by hand on
every issue that named it — `blocked` / `blocked: design`, if one was applied
for the same reason, is removed in the same PR or a prompt follow-up, not
left for someone to notice later. Nothing in this repo clears it
automatically.
