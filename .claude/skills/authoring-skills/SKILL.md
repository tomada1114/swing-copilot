---
name: authoring-skills
description: >
  Covers how a skill in this repository is written and where its knowledge
  belongs — the .claude/skills/<name>/SKILL.md layout, the frontmatter
  contract, the workflow-skill vs knowledge-skill distinction, when material
  goes in references/ or scripts/ versus the body, and the boundary between a
  skill and AGENTS.md. Use when adding or editing a .claude/skills/*/SKILL.md,
  deciding whether new material belongs in AGENTS.md or a skill, writing a
  skill description, or a skill never fires when it should.
---

# Authoring Skills

**Owns:** how a skill in *this* repository is authored and where its
knowledge belongs. **Does not own:** the content of any particular skill;
the general-purpose mechanics of the Agent Skills format (frontmatter
parsing rules, size limits, the validator itself) live in the user-level
`creating-agent-skills` skill and are not duplicated here.

**REQUIRED:** `creating-agent-skills` for the format's general mechanics and
the validator script. This skill is the repo-specific layer on top of it:
where things live in *this* repo, and the judgment calls specific to this
codebase's skill roster.

## Layout

Every skill lives at `.claude/skills/<name>/SKILL.md`, authored once. There
is no mirrored `.agents/skills/` tree and no sync script in this repository
— unlike a dual-platform (Claude Code + Codex CLI) project, `.claude/skills/`
is the only tree an agent reads here, so there is no drift-between-copies
concern to guard against.

## Frontmatter contract

Exactly two keys: `name` and `description`.

```yaml
---
name: <exact-directory-name>
description: >
  <what this skill owns>. Use when <concrete triggers>.
---
```

`description` is the **only** text the model sees before deciding to load
the body. Skill selection matches on meaning, not on the body content, so a
description that only summarizes the skill's internal steps rather than the
situation that should trigger it is why a skill never fires. Name concrete
triggers: real paths (`src/swing_copilot/storage/**`), real commands
(`just verify`, `copilot-daily`), real identifiers (`SwingCopilotError`,
`PREFLIGHT_ABORT[no_trading_day]`), real label names (`priority: P0`). "Use
when working with configuration" fires on nothing in particular; "Use when
editing `pyproject.toml`'s `[tool.ruff]` table" fires reliably.

## Two kinds of skill

**Workflow skills** drive a procedure end to end, usually with numbered
steps and an explicit tool sequence: `create-pr`, `smart-commit`,
`merge-dependabot`, `swing-daily`, `swing-retro`. These are invoked to *do*
something, and their body reads like a runbook.

**Knowledge skills** own a body of convention and judgment, and are pulled
in when a matching file or decision is touched rather than run start-to-finish
— `writing-python`, `writing-tests`, `placing-tests`, `designing-errors`,
`public-api-contract`, `changing-gates`, and this repo-operations layer
(`triaging-issues`, `updating-docs`, `authoring-skills` itself). Their body
reads like a review checklist with reasons, not a sequence of commands.

Knowing which kind a new skill is decides its shape: a workflow skill without
numbered steps is unusable, and a knowledge skill padded with a fake
procedure buries the judgment calls a reviewer actually needs.

## The Owns / Does not own line is not optional

Every SKILL.md opens its body with an ownership line naming what it decides
and what a named sibling decides instead, e.g. this skill's own line above.
This is a hard requirement, not a style preference: two skills that both
silently claim the same territory will eventually give contradictory
guidance, and nothing catches that except a reader noticing by luck. Name
the sibling in backticks so it is grep-able.

## references/ and scripts/

Move material out of the body, rather than letting the body grow past its
target size, in two cases:

- **`references/<topic>.md`** for material too long to keep inline —
  `swing-daily/references/analysis-conventions.md` and
  `output-schema.md`, `swing-retro/references/proposal-rules.md` and
  `result-schema.md`. Link relatively from the body; do not inline the whole
  thing and also duplicate it in a reference file.
- **`scripts/`** for deterministic work that should not be re-derived as
  prose each time — `merge-dependabot/scripts/survey_prs.py`, invoked from
  the body as `uv run --script .claude/skills/merge-dependabot/scripts/survey_prs.py`.
  If a step's output must be byte-identical given the same input, it belongs
  in a script the skill calls, not in instructions asking the agent to
  reproduce the logic by hand.

## Where new material belongs: AGENTS.md vs. a skill

A rule every session must obey regardless of its declared task — "never
weaken a gate to make a run pass," an as-of boundary invariant, a storage
transaction rule — goes in `AGENTS.md`, where every agent reads it whether or
not the current task looks related. A skill holds the *judgment* behind such
a rule: when it applies, the boundary cases, the anti-pattern that actually
bit in this repo, the reasoning that lets an agent apply the rule to a case
AGENTS.md never enumerated.

Never write the same rule in both places in full. `AGENTS.md` keeps the
one-line, always-loaded version; the skill is the deep layer loaded on
demand. Duplicating the full text in both is how they drift — one gets
updated after an incident and the other doesn't, and now two conflicting
statements of the same rule exist.

## English is the authoring language

Write every SKILL.md — frontmatter and body — in English, even though repo
prose elsewhere (commit messages, PR bodies, and this repository's other
skills' procedural bodies) may be Japanese per AGENTS.md's Language and
Scope. Skill *selection* matches the description against meaning, and an
English description is what keeps matching reliable regardless of which
language the invoking conversation is in.

## Domain analysis skills carry an extra constraint

`analyze-news`, `analyze-filings`, `interpret-screening`, and `swing-daily`
interpret untrusted external text (news, filings) and turn it into
user-facing report content. Never edit one of them in a way that weakens its
provenance requirements or CON-03 safety language discipline as a side
effect of an unrelated change — a rewording that drops a `source_ids`
requirement or loosens the ban on imperative buy/sell language looks like a
harmless simplification but removes a fail-closed guarantee.
**BACKGROUND:** `guarding-analysis-boundary` for why these rules exist and
what enforces them outside the skill text itself.

## Validate before committing

```bash
python3 ~/.claude/skills/creating-agent-skills/scripts/validate_skill.py \
  .claude/skills/<name>
```

It checks the two-key frontmatter, `name`'s format and length, `description`'s
length cap, body size (warns above 200 lines, errors above 500), that every
relative link under `references/`/`scripts/`/`assets/` resolves, and that no
single code block exceeds 25 lines. Fix every error; size warnings are
advisory but a body pushing past 150 lines is usually a sign that some of it
belongs in `references/` instead.
