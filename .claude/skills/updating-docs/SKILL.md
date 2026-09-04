---
name: updating-docs
description: >
  Decides whether a change needs a documentation update and which surface it
  lands on — README.md, the docs/NN_*.md canonical set, docs/reference.md, or a
  docstring. Use when triaging whether a PR touching
  src/swing_copilot/**, docs/**, or README.md needs a doc change, deciding
  which docs/NN_*.md is the canonical source for a design question, resolving
  a conflict between docs/03_basic_design.md and the current schema, or
  running `just docs-check` / `mkdocs build --strict`.
---

# Updating Documentation

**Owns:** deciding whether a change needs a documentation update, and which
surface it lands on. **Does not own:** the semver level or CHANGELOG wording
of a release (`release-impact`); what may be exported (`public-api-contract`);
how a skill's own SKILL.md is written (`authoring-skills`).

## Route by canonical source, not by habit

AGENTS.md's "Sources of Truth" table is the routing table; read it as a
decision, not a reference list. The judgment it does not spell out: a doc
change lands on the source that owns the concern, not on whichever file
happens to be open, and two files disagreeing about the same concern is a
defect in itself, independent of which one is "more right." The two rows most
often routed to the wrong surface are *behavioral invariants* (`docs/03_basic_design.md`,
then `docs/04_detailed_design.md`'s contract sections — not a README example)
and *current data/API shape* (`models.py`, `storage/schema.py`, and the public
signatures themselves — never a prose restatement of a schema).

## When canonical design and current code disagree

Do not silently pick one. `src/swing_copilot/models.py`, `storage/schema.py`,
and public signatures are truth about what the system does today;
`docs/03_basic_design.md` / `docs/04_detailed_design.md` are truth about what
it is supposed to do. When they diverge: preserve compatibility (don't change
behavior to match stale prose as a side effect of a doc pass), record the
divergence, and either update the stale canonical source or request a
decision. Never resolve the conflict by quietly editing whichever file is
more convenient to touch.

## `docs/goal-prompts/**` is not a documentation surface

`docs/goal-prompts/**` holds one autonomous run's instructions and its
recorded decisions — execution support and history, never an evergreen
replacement for `docs/01_requirements.md` or `docs/03_basic_design.md`. It is
also excluded from the built site entirely (`mkdocs.yml`'s `exclude_docs:
goal-prompts/`), so nothing there is reachable by a reader browsing the docs
site regardless of nav. A durable rule discovered while executing a goal
prompt belongs in the canonical doc it affects, not left to live only under
`docs/goal-prompts/<name>/`.

## Same-commit requirement

Implementation, its regression test, and the required canonical-doc update
land in the same logical commit — never as a follow-up. A behavior change
merged without its doc update is the fact drifting out of sync from the day
it lands, not later.

## When nothing is owed

An internal refactor that changes no public signature, no default, no
observable runtime behavior, no `PREFLIGHT_ABORT[...]`/error reason string,
and no CLI surface needs no documentation change. Say so explicitly rather
than treating "no doc change" as something to double-check away — it is a
legitimate outcome of this decision, not a shortcut.

## `just docs-check` fails on warnings

`just docs-check` runs `uv run mkdocs build --strict` (see `justfile`), and
`just verify` / `just verify-full` both run it. Strict mode turns every
warning into a build failure, so:

- A new page under `docs/` must be added to `mkdocs.yml`'s `nav:` — an
  unreferenced page is a warning, and `--strict` fails the build over it.
- Every internal Markdown link and `pymdownx.snippets` include (such as
  `docs/contributing.md`'s `--8<-- "CONTRIBUTING.md"`) must resolve. A
  renamed or moved target breaks silently for a reader building without
  `--strict`, but never passes CI.
- `docs/reference.md`'s `::: swing_copilot` directive (mkdocstrings) pulls
  docstrings automatically — a broken cross-reference inside a docstring
  fails the same strict build. The hand-written prose sections of
  `docs/reference.md` (e.g. its cross-cutting-primitives section) are not
  generated and need the same manual update discipline as any other page.

## Code examples must work against the current API

Every fenced code example in README.md and `docs/**` must be valid against
the current public API. There is no compiled-snippet test in this repository
(unlike a TypeScript project's doc-compile check) — an example going stale is
caught only by a human reading the diff, so treat a signature change that
breaks a documented example as part of the same change, not a follow-up.

## What belongs in prose

Document non-obvious behavior, architecture decisions, and trade-offs. Do not
restate what a type signature or the code itself already says — a docstring
or a doc page that repeats the function's parameter list in prose is noise a
reader has to read past. Use MkDocs admonitions (`!!! note`, `!!! warning`,
`!!! tip` — enabled via the `admonition` extension in `mkdocs.yml`) for a
callout that would otherwise be a paragraph of hedging.

## Language

Prose may be Japanese or English — most of `docs/reference.md`'s hand-written
sections are Japanese, `docs/index.md` is English — but stay internally
consistent within one document or PR; do not mix languages mid-page. Code
identifiers and Conventional Commit type/scope tokens stay English regardless
of the surrounding prose language.
