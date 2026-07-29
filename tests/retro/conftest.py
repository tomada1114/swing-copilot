"""Shared builders for the retrospective (`retro/`) tests.

Reuses `tests/analysis/conftest.py`'s payload builders so the fixtures stay
bound to the one strict schema pair `collect` actually parses, rather than
drifting into a second hand-maintained copy of it.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import pytest

from swing_copilot.analysis.export import (
    ANALYSIS_INPUT_FILENAME,
    ANALYSIS_RESULT_FILENAME,
)
from tests.analysis.conftest import (
    AS_OF,
    CALENDAR_ID,
    FILING_ID,
    NEWS_ID,
    RUN_ID,
    input_payload,
    result_payload,
    symbol_payload,
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from datetime import date
    from pathlib import Path

__all__ = [
    "AS_OF",
    "CALENDAR_ID",
    "FILING_ID",
    "NEWS_ID",
    "RUN_ID",
    "input_payload",
    "result_payload",
    "symbol_payload",
]


@pytest.fixture
def reports_root(tmp_path: Path) -> Path:
    """An empty `reports/` root, mirroring `pipeline/daily.py`'s output dir."""
    root = tmp_path / "reports"
    root.mkdir()
    return root


#: Distinguishes "caller said nothing" (write the default document) from an
#: explicit `None` (leave that document out of the run directory entirely).
_DEFAULT: Any = object()


@pytest.fixture
def write_run(reports_root: Path) -> Callable[..., Path]:
    """Write one `reports/<date>/<run_id>/` pair of analysis documents.

    Passing `None` for either document omits that file, which is how the
    fail-soft tests build an incomplete run archive.
    """

    def _write(
        analysis_input: dict[str, Any] | str | None = _DEFAULT,
        result: dict[str, Any] | str | None = _DEFAULT,
        *,
        run_id: str = RUN_ID,
        run_date: date = AS_OF,
    ) -> Path:
        directory = reports_root / run_date.isoformat() / run_id
        directory.mkdir(parents=True, exist_ok=True)
        if analysis_input is not None:
            _dump(
                directory / ANALYSIS_INPUT_FILENAME,
                input_payload() if analysis_input is _DEFAULT else analysis_input,
            )
        if result is not None:
            _dump(
                directory / ANALYSIS_RESULT_FILENAME,
                result_payload() if result is _DEFAULT else result,
            )
        return directory

    return _write


def _dump(path: Path, payload: dict[str, Any] | str) -> None:
    text = payload if isinstance(payload, str) else json.dumps(payload)
    path.write_text(text, encoding="utf-8")
