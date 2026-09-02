"""Shared test support: fakes, script loading, and `runs`-row seeding (Issue #398).

Everything here is reused by multiple test modules on purpose -- fixing one
copy used to mean hunting down its siblings, which had already drifted
(`tests/pipeline/test_failsoft.py`'s `FakeDataProvider` silently dropped
`failures`, for example). One implementation per fake/helper means a fix
lands everywhere at once.
"""

from __future__ import annotations
