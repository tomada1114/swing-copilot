"""Tests for ModelPricing (NFR-01, FR-08)."""

from __future__ import annotations

import pytest

from swing_copilot.exceptions import ConfigError
from swing_copilot.llm.pricing import ModelPricing


def test_returns_known_pricing_for_the_default_model():
    input_price, output_price = ModelPricing().get("claude-haiku-4-5-20251001")
    assert input_price == pytest.approx(1.0)
    assert output_price == pytest.approx(5.0)


def test_unknown_model_raises_config_error_not_zero_cost():
    with pytest.raises(ConfigError, match="Unknown pricing"):
        ModelPricing().get("claude-made-up-model")
