"""Qualitative-analysis boundary between the Python pipeline and Claude Code skills.

The pipeline exports everything a skill needs to reason about (`export.py`),
the skill writes its answer back as JSON, and `validate.py` machine-checks that
answer (schema, provenance, CON-03) before any of it reaches a report. No
module here calls an LLM API: the model runs in the operator's Claude Code
session, never inside this process.
"""

from __future__ import annotations
