"""Version-controlled per-model Claude API pricing (NFR-01, FR-08).

An unknown model ID is a configuration error, never a silent zero-cost
default (`docs/04_detailed_design.md` 3.16). Adding a model here requires
checking the official pricing page first — this table is never inferred.
"""

from __future__ import annotations

from swing_copilot.exceptions import ConfigError

# (input $/MTok, output $/MTok), verified against docs/03_basic_design.md 6.2.
_KNOWN_PRICING: dict[str, tuple[float, float]] = {
    "claude-haiku-4-5-20251001": (1.0, 5.0),
}


class ModelPricing:
    """Looks up `(input_price_per_mtok, output_price_per_mtok)` for a model ID."""

    def get(self, model: str) -> tuple[float, float]:
        """Return `(input_price_per_mtok, output_price_per_mtok)` for `model`.

        Args:
            model: The Claude model ID.

        Returns:
            USD price per million input/output tokens.

        Raises:
            ConfigError: `model` is not in the known pricing table.
        """
        if model not in _KNOWN_PRICING:
            msg = (
                f"Unknown pricing for model {model!r}. Check the official Anthropic "
                "pricing page and add it to llm/pricing.py before using this model."
            )
            raise ConfigError(msg)
        return _KNOWN_PRICING[model]
