"""Direct contracts for collected-text persistence helpers."""

from __future__ import annotations

from typing import TYPE_CHECKING

from swing_copilot.storage import text_records

if TYPE_CHECKING:
    import pytest

    from swing_copilot.storage.state_store import StateStore


def test_empty_text_batch_does_not_open_a_write_transaction(
    state_store: StateStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail_transaction(*_args: object, **_kwargs: object) -> None:
        raise AssertionError

    monkeypatch.setattr(state_store.database, "transaction", fail_transaction)

    text_records.record_text_items(state_store.database, ())
