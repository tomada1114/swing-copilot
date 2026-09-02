"""The one `extra="forbid"` declaration point for skill-boundary schemas.

AGENTS.md requires that a skill's exported and ingested documents "parse
under strict (`extra="forbid"`) schemas, so an invented or renamed field
fails loudly instead of being silently dropped." Every schema that crosses
that boundary -- `analysis_input.json`, `analysis_result.json`, expert
fragments and slices, and the retrospective's dossier and result -- needs
that same behavior, so it is declared once, here, instead of being
re-declared per module.

This module depends on nothing but Pydantic, matching `io_atomic.py`'s
dependency-zero shape: wanting a strict schema base should never pull in an
unrelated package.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

__all__ = ["StrictModel"]


class StrictModel(BaseModel):
    """Base for every schema that crosses the skill boundary.

    An unknown key -- a renamed or invented field -- fails validation loudly
    instead of being silently dropped.
    """

    model_config = ConfigDict(extra="forbid")
