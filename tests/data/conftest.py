"""Shared fixtures for the external market/filings adapter tests.

`tests/data/` covers adapters that reach real client libraries, so the one
thing this tier owns is undoing a *process-wide* side effect those libraries
carry: `stamina`'s global retry flag.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
import stamina

if TYPE_CHECKING:
    from collections.abc import Iterator


@pytest.fixture(autouse=True)
def _restore_stamina_retry_state() -> Iterator[None]:
    """Reset `stamina`'s process-wide active flag around every test (Issue #429).

    `EdgarClient.__init__` calls `stamina.set_active(False)` as a real,
    documented side effect (not a fake): every `EdgarClient(...)` any test
    here builds leaves `stamina` globally deactivated for whatever test runs
    next in this worker process. `TestEdgartoolsInternalRetry` needs to start
    from a known "active" state to prove construction is what turns it off,
    and that has to hold under `-n auto` / any test order, not just when one
    file happens to run first -- so set it explicitly before yielding rather
    than relying on whatever a previous test left behind, and restore the
    pre-test value afterwards so a later, unrelated test file is not left
    looking at a permanently deactivated `stamina`.

    It lives in this package's `conftest.py` rather than in one module
    because `test_edgar.py` and `test_edgar_http_boundary.py` both construct
    `EdgarClient`, and a fixture local to one of them would leave the other
    leaking the deactivated flag.
    """
    previous = stamina.is_active()
    stamina.set_active(True)
    yield
    stamina.set_active(previous)
