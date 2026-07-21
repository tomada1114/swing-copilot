"""Structured LLM output schemas: facts/interpretation separated (FR-08, CON-03).

Separating `facts` from `interpretation` alone cannot prevent hallucination,
so every fact also carries the input source IDs it came from
(`SourcedFact.source_ids`) — `llm/client.py` validates these are a subset of
the source IDs actually given to the model, and a report can always trace a
fact back to its origin. Imperative "buy/sell" language is prevented by
prompt instructions and a post-output banned-word check (`llm/summarize.py`,
`llm/filings_analysis.py`), not by this schema alone.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, Field, StringConstraints

SourceId = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class SourcedFact(BaseModel):
    """One factual statement, tied to the input source(s) it came from."""

    statement: str
    source_ids: Annotated[list[SourceId], Field(min_length=1)]


class NewsSummary(BaseModel):
    """Structured news summary (`llm/summarize.py`)."""

    symbol: str
    period: str
    facts: list[SourcedFact]
    interpretation: list[str]
    sentiment: Literal[-1, 0, 1]
    risk_flags: list[str]
    sources: list[str]


class FilingAnalysis(BaseModel):
    """Structured filing interpretation (`llm/filings_analysis.py`)."""

    symbol: str
    filing_type: str
    facts: list[SourcedFact]
    interpretation: list[str]
    red_flags: list[str]
    yoy_changes: list[str]
    guidance_direction: Literal["positive", "negative", "neutral", "not_disclosed"]
