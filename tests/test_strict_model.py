"""`StrictModel`: the one `extra="forbid"` declaration point (Issue #394)."""

from __future__ import annotations

import pytest
from pydantic import ConfigDict, ValidationError

from swing_copilot.strict_model import StrictModel


class _Widget(StrictModel):
    name: str
    count: int = 0


class _FrozenWidget(StrictModel):
    """A subclass adding config must keep the inherited `extra="forbid"`."""

    model_config = ConfigDict(frozen=True)

    name: str


def test_a_known_field_parses():
    widget = _Widget(name="bolt", count=3)

    assert widget.name == "bolt"
    assert widget.count == 3


def test_an_unknown_field_is_rejected():
    with pytest.raises(ValidationError, match="extra_field"):
        _Widget.model_validate({"name": "bolt", "extra_field": "unexpected"})


def test_a_subclasss_own_config_merges_with_and_does_not_replace_extra_forbid():
    """A subclass config must merge with, not replace, `StrictModel`'s.

    Pydantic merges `model_config` down the inheritance chain: a subclass
    that only sets `frozen=True` still inherits `extra="forbid"` from
    `StrictModel` rather than losing it -- the property Issue #394's design
    step relies on to let `scripts/data_sync.py`'s `FileEntry`
    (`frozen=True`) migrate onto `StrictModel` without repeating
    `extra="forbid"` itself.
    """
    assert _FrozenWidget.model_config.get("extra") == "forbid"
    assert _FrozenWidget.model_config.get("frozen") is True

    widget = _FrozenWidget(name="bolt")
    with pytest.raises(ValidationError, match="frozen"):
        widget.name = "nut"

    with pytest.raises(ValidationError, match="extra_field"):
        _FrozenWidget.model_validate({"name": "bolt", "extra_field": "unexpected"})
