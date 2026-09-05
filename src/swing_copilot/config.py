"""Load `config/settings.yaml`, `config/strategies.yaml`, and environment secrets.

Feature-gated secret validation (`require_secrets`) lets the configuration
load unconditionally offline while still failing fast when a feature that
needs a secret is actually enabled (`docs/04_detailed_design.md` 2.1 #6).
"""

from __future__ import annotations

import math
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final, Literal, Self

import yaml
from pydantic import (
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)
from pydantic_settings import BaseSettings, SettingsConfigDict

from swing_copilot.analysis.filing_selection import MIN_FILING_CHARS
from swing_copilot.analysis.news_supply import DEFAULT_SUFFICIENT_SYMBOL_MENTION_ITEMS
from swing_copilot.analysis.schemas import canonical_json_digest
from swing_copilot.documents import read_text_document
from swing_copilot.exceptions import ConfigError
from swing_copilot.strict_model import StrictModel
from swing_copilot.tracking.board import DEFAULT_PUBLISHED_RETENTION_BUSINESS_DAYS

if TYPE_CHECKING:
    from collections.abc import Mapping

FEATURE_SECRET_ATTRS: dict[str, str] = {
    "finnhub": "finnhub_api_key",
    "fred": "fred_api_key",
    "discord": "discord_webhook_url",
    "edgar": "edgar_identity",
    "eodhd": "eodhd_api_key",
}


class Secrets(BaseSettings):
    """Secrets loaded from environment variables (and a local `.env`)."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    finnhub_api_key: str | None = None
    fred_api_key: str | None = None
    discord_webhook_url: str | None = None
    edgar_identity: str | None = None
    eodhd_api_key: str | None = None  # unused until the P4 EODHD provider

    @field_validator(
        "finnhub_api_key",
        "fred_api_key",
        "discord_webhook_url",
        "edgar_identity",
        "eodhd_api_key",
        mode="before",
    )
    @classmethod
    def _blank_means_unset(cls, value: object) -> object:
        """Treat a declared-but-empty `.env` entry (`KEY=`) as unset.

        `.env.example`-style files often ship with keys present but blank;
        python-dotenv reads that as `""`, not absent, so without this the
        value would be silently treated as "configured".
        """
        if isinstance(value, str) and not value.strip():
            return None
        return value


class _StrictModel(StrictModel):
    """Base for settings.yaml sections: reject unknown keys (fail fast).

    `extra="forbid"` comes from `StrictModel` (Issue #394's one declaration
    point); pydantic merges a subclass's `model_config` with its parent's
    rather than replacing it, so adding `strict=True` here keeps the
    inherited `extra="forbid"` (`tests/test_strict_model.py` fixes that merge
    behavior).
    """

    model_config = ConfigDict(strict=True)


class UniverseConfig(_StrictModel):
    """`universe.*` in `settings.yaml`."""

    index: str = "sp500"
    refresh_interval_days: int = Field(default=7, ge=1)
    snapshot_path: str = "config/universe_snapshot.csv"
    manual_include: list[str] = Field(default_factory=list)
    manual_exclude: list[str] = Field(default_factory=list)


class RiskConfig(_StrictModel):
    """`risk.*` in `settings.yaml`."""

    # Earnings-calendar lookahead window in calendar days (P8-115). 45 covers
    # max_hold_days=25 business days (~35 calendar days) plus a
    # weekend/holiday margin, so an event due late in the hold period is not
    # missed and misreported as EARNINGS_DATE_UNKNOWN.
    earnings_lookahead_days: int = Field(default=45, ge=1)
    # Weekday-only earnings proximity thresholds (roadmap §5 P4-18;
    # parabolic-short-trade-planner, 要検証).
    earnings_block_business_days: int = Field(default=2, ge=0)
    earnings_warn_business_days: int = Field(default=5, ge=0)
    # Stop distance as a % of entry price above which a WIDE_STOP sizing
    # warning is raised (roadmap §5 P1-03, 要検証).
    wide_stop_threshold_pct: float = Field(default=10.0, gt=0.0)

    @model_validator(mode="after")
    def _validate_earnings_threshold_order(self) -> RiskConfig:
        if self.earnings_warn_business_days < self.earnings_block_business_days:
            msg = "earnings_warn_business_days must be >= earnings_block_business_days"
            raise ValueError(msg)
        return self


class FundamentalFilterConfig(_StrictModel):
    """`fundamental_filters.*` in `settings.yaml`."""

    min_profitable_quarters: int = Field(default=4, ge=1)
    require_positive_fcf: bool = True
    min_equity_ratio: float = Field(default=0.30, ge=0.0, le=1.0)


class TrendSignalConfig(_StrictModel):
    """`technical_signals.trend.*` in `settings.yaml`."""

    sma_short: int = Field(default=50, ge=1)
    sma_long: int = Field(default=200, ge=1)

    @model_validator(mode="after")
    def _validate_window_order(self) -> TrendSignalConfig:
        if self.sma_short >= self.sma_long:
            msg = "sma_short must be < sma_long"
            raise ValueError(msg)
        return self


class PullbackSignalConfig(_StrictModel):
    """`technical_signals.pullback.*` in `settings.yaml`."""

    rsi_period: int = Field(default=14, ge=1)
    rsi_threshold: float = Field(default=45.0, ge=0.0, le=100.0)
    # When set, the distance from SMA50 is measured in ATR14 units instead of
    # the fixed compatibility band. A fixed 3% band admits low-volatility
    # names roughly 4.5x as often as high-volatility ones, which is the
    # low-volatility bias this mode exists to remove. Left at None so older
    # settings that omit the ATR mode keep their screening behavior.
    band_atr_multiple: float | None = Field(default=None, gt=0.0)


class VolumeFilterConfig(_StrictModel):
    """`technical_signals.volume.*` in `settings.yaml`."""

    avg_volume_days: int = Field(default=20, ge=1)
    min_avg_volume: int = Field(default=1_000_000, ge=0)


class MinerviniSignalConfig(_StrictModel):
    """`technical_signals.minervini.*` (roadmap §5 P5-21)."""

    # All defaults below are roadmap §5 P5-21 values. The RS threshold and
    # weighting are explicitly 要検証 in that source, so remain configuration.
    sma200_rising_days: int = Field(default=22, ge=1)
    min_low_multiple: float = Field(default=1.25, ge=1.0)
    min_high_multiple: float = Field(default=0.75, gt=0.0, le=1.0)
    min_rs_percentile: float = Field(default=70.0, ge=0.0, le=100.0)
    rs_weight_63d: float = Field(default=0.40, ge=0.0)
    rs_weight_126d: float = Field(default=0.20, ge=0.0)
    rs_weight_189d: float = Field(default=0.20, ge=0.0)
    rs_weight_252d: float = Field(default=0.20, ge=0.0)

    @model_validator(mode="after")
    def _validate_rs_weights(self) -> MinerviniSignalConfig:
        weights = (
            self.rs_weight_63d,
            self.rs_weight_126d,
            self.rs_weight_189d,
            self.rs_weight_252d,
        )
        if not math.isclose(sum(weights), 1.0, abs_tol=1e-9):
            msg = "minervini RS weights must sum to 1.0"
            raise ValueError(msg)
        return self


class ExecutionStateConfig(_StrictModel):
    """P5-23 ATR-normalized entry-state thresholds (roadmap §5, 要検証)."""

    damaged_max_d: float = -3.0
    fair_max_d: float = 2.0
    extended_max_d: float = 4.0

    @model_validator(mode="after")
    def _validate_order(self) -> ExecutionStateConfig:
        if not self.damaged_max_d < 0.0 < self.fair_max_d < self.extended_max_d:
            msg = "execution thresholds must satisfy damaged < 0 < fair < extended"
            raise ValueError(msg)
        return self


class VcpSignalConfig(_StrictModel):
    """P5-24 VCP thresholds (roadmap §5; every value is 要検証)."""

    zigzag_atr_multiplier: float = Field(default=2.0, gt=0.0)
    first_depth_min: float = Field(default=0.08, gt=0.0, le=1.0)
    first_depth_max: float = Field(default=0.35, gt=0.0, le=1.0)
    small_cap_first_depth_max: float = Field(default=0.50, gt=0.0, le=1.0)
    contraction_ratio_max: float = Field(default=0.75, gt=0.0, le=1.0)
    min_contractions: int = Field(default=2, ge=2)
    max_contractions: int = Field(default=4, ge=2)
    pattern_days_min: int = Field(default=15, ge=1)
    pattern_days_max: int = Field(default=325, ge=1)
    dry_up_ideal_max: float = Field(default=0.30, gt=0.0, le=1.0)
    dry_up_weak_min: float = Field(default=0.70, gt=0.0, le=1.0)
    chase_pivot_pct: float = Field(default=0.05, gt=0.0, le=1.0)

    @model_validator(mode="after")
    def _validate_ranges(self) -> VcpSignalConfig:
        if (
            not self.first_depth_min
            <= self.first_depth_max
            <= self.small_cap_first_depth_max
        ):
            msg = "VCP first-depth thresholds must be ordered"
            raise ValueError(msg)
        if self.pattern_days_max < self.pattern_days_min:
            msg = "VCP pattern_days_max must be >= pattern_days_min"
            raise ValueError(msg)
        if self.max_contractions < self.min_contractions:
            msg = "VCP max_contractions must be >= min_contractions"
            raise ValueError(msg)
        if self.dry_up_weak_min <= self.dry_up_ideal_max:
            msg = "VCP dry-up weak threshold must exceed ideal threshold"
            raise ValueError(msg)
        return self


class TechnicalSignalConfig(_StrictModel):
    """`technical_signals.*` in `settings.yaml`."""

    trend: TrendSignalConfig = TrendSignalConfig()
    pullback: PullbackSignalConfig = PullbackSignalConfig()
    volume: VolumeFilterConfig = VolumeFilterConfig()
    minervini: MinerviniSignalConfig = MinerviniSignalConfig()
    execution: ExecutionStateConfig = ExecutionStateConfig()
    vcp: VcpSignalConfig = VcpSignalConfig()


class TradePlanConfig(_StrictModel):
    """The trade plan shared by production advice, tracking, and backtests."""

    entry_limit_atr_multiple: float = Field(default=0.0, ge=0.0)
    exit_atr_multiple: float = Field(default=2.5, gt=0.0)
    exit_atr_period: int = Field(default=14, ge=1)
    max_hold_days: int = Field(default=25, ge=1)


class TrackingConfig(_StrictModel):
    """Published tracking-board display settings."""

    published_retention_business_days: int = Field(
        default=DEFAULT_PUBLISHED_RETENTION_BUSINESS_DAYS, ge=0
    )


class BacktestConfig(_StrictModel):
    """`backtest.*` simulation and cost settings in `settings.yaml`."""

    initial_cash_usd: float = Field(default=100_000, gt=0.0)
    # The engine consumes this mode: candidates are queued after the signal
    # close and evaluated against the next session's OHLC. `next_open` keeps
    # the zero-k compatibility arm; `next_limit` always applies the Day-limit
    # gate. Keeping a Literal prevents an arbitrary, silently ignored string
    # from becoming config.
    entry: Literal["next_open", "next_limit"] = "next_open"
    # These are nominal simulation values, never production advice values.
    sim_trade_risk_pct: float = Field(default=0.01, gt=0.0, le=1.0)
    sim_position_cap_pct: float = Field(default=0.10, gt=0.0, le=1.0)
    max_concurrent_positions: int = Field(default=10, ge=1)
    commission_pct: float = Field(default=0.001, ge=0.0, lt=1.0)
    slippage_pct: float = Field(default=0.001, ge=0.0, lt=1.0)
    benchmark: str = "SPY"
    # Multiplier applied to slippage_pct on both entry and exit (incl. forced
    # liquidation), roadmap §5 P2-09. 1.0 == no change from the base
    # slippage_pct; --pessimistic overrides it with pessimistic_slippage_multiplier.
    slippage_multiplier: float = Field(default=1.0, gt=0.0)
    # Pessimistic-scenario preset (roadmap §5 P2-09, 要検証: median of
    # backtest-expert's cited 1.5-2.0x range).
    pessimistic_slippage_multiplier: float = Field(default=1.75, gt=1.0)
    # P2-10 sensitivity grid: best cell's expectancy_per_trade strictly above
    # this multiple of its (non-gray) neighbors' median triggers a "spike"
    # (overfitting suspicion) verdict (roadmap §5 P2-10, 要検証).
    sensitivity_spike_multiplier: float = Field(default=1.5, gt=1.0)
    # P2-10 sensitivity grid: fraction (e.g. 0.20 == 20%) around the best
    # cell's value within which every non-gray cell must fall for a
    # "plateau" (robust) verdict (roadmap §5 P2-10, 要検証; basis point is the
    # best cell's own value -- not specified in the seed, fixed here).
    sensitivity_plateau_tolerance_pct: float = Field(default=0.20, ge=0.0)
    # trade_count below this draws a "statistically insufficient" warning;
    # below preliminary_trade_count_threshold (but >= this) draws a
    # "preliminary" warning (roadmap §5 P2-07, out: backtest-expert).
    insufficient_trade_count_threshold: int = Field(default=30, ge=1)
    preliminary_trade_count_threshold: int = Field(default=100, ge=1)
    # win_rate (fraction, e.g. 0.90 == 90%) strictly above this, or
    # max_drawdown_pct strictly below lookahead_suspicion_max_drawdown, draws
    # a look-ahead-bias suspicion warning (roadmap §5 P2-07). The win_rate
    # bound is from the roadmap; the drawdown bound has no seed value and is
    # fixed here as 要検証 per Issue #16's boundary note.
    lookahead_suspicion_win_rate: float = Field(default=0.90, ge=0.0, le=1.0)
    lookahead_suspicion_max_drawdown: float = Field(default=0.01, ge=0.0)

    @model_validator(mode="after")
    def _validate_trade_count_thresholds(self) -> BacktestConfig:
        if (
            self.preliminary_trade_count_threshold
            < self.insufficient_trade_count_threshold
        ):
            msg = (
                "preliminary_trade_count_threshold must be >= "
                "insufficient_trade_count_threshold"
            )
            raise ValueError(msg)
        return self


class AnalysisConfig(_StrictModel):
    """`analysis.*` in `settings.yaml`.

    Bounds on the untrusted text handed to the qualitative-analysis skill via
    `analysis_input.json`. These replace the old `llm.*` section: the API
    caller is gone, but the collection and export limits it defined are still
    what keeps one run's exported text bounded (roadmap §5 P6-26).
    """

    max_news_items_per_symbol: int = Field(default=20, ge=1)
    max_news_chars_per_item: int = Field(default=4000, ge=1)
    # Total characters of one filing's text exported for analysis. Formerly
    # derived as `filing_chunk_chars * max_filing_chunks` from the previous
    # chunked-analysis bounds; consolidated into a single setting since
    # nothing chunks filings for separate calls anymore (the skill reads one
    # filing's export in a single context).
    max_filing_chars: int = Field(default=120_000, ge=1)
    # Combined filing text for one symbol. This keeps a fresh filing-analysis
    # agent comfortably below a 200k-token context even when collection found
    # several long forms; individual filings remain bounded above.
    max_filing_chars_per_symbol: int = Field(default=240_000, ge=1)
    # Filing *collection* recency bound and per-symbol count cap, applied in
    # `text/edgar_filings.py::fetch_recent_filings_text()` -- symmetric with
    # the news-side limit above (roadmap §5 P6-26).
    filing_lookback_days: int = Field(default=90, ge=1)
    max_filings_per_symbol: int = Field(default=3, ge=1)
    # Bounds on the run-wide macro/economic-calendar events surfaced in
    # `context.calendar_events` (not per-symbol: a calendar event isn't tied
    # to any one candidate).
    max_calendar_events: int = Field(default=20, ge=1)
    max_calendar_chars_per_item: int = Field(default=2000, ge=1)
    # How many exported articles must name the candidate before its news feed
    # is graded `sufficient` (Issue #130's measurement, made configurable by
    # Issue #191). The shipped default is one run's calibration, so it is a
    # threshold requiring validation and therefore belongs in settings rather
    # than in a module constant.
    sufficient_news_mention_items: int = Field(
        default=DEFAULT_SUFFICIENT_SYMBOL_MENTION_ITEMS, ge=1
    )

    @model_validator(mode="after")
    def _verify_filing_export_budgets(self) -> Self:
        """Reject a per-symbol ceiling too small for the filings it must hold.

        Two separate lower bounds, both invalid limits rather than degradations
        to absorb at export time:

        - the per-symbol ceiling must fit one filing at the per-filing ceiling,
          otherwise `max_filing_chars` is unreachable and misdescribes itself;
        - it must also fit every collected filing's guaranteed minimum
          reservation (Issue #268). `analysis/filing_selection.py` reserves
          `min(len(text), max_filing_chars, MIN_FILING_CHARS)` per filing out of
          one shared ceiling, so `max_filings_per_symbol` filings need
          `max_filings_per_symbol * min(max_filing_chars, MIN_FILING_CHARS)`
          between them before any of them is served beyond its floor. Under
          less than that, the reservations run out mid-list and the trailing
          filings export as `omitted_symbol_budget` on *every* run -- a broken
          configuration, not a property of the day's filings, so it fails fast
          here rather than after EDGAR has been called.
        """
        if self.max_filing_chars_per_symbol < self.max_filing_chars:
            msg = "max_filing_chars_per_symbol must be >= max_filing_chars"
            raise ValueError(msg)
        guaranteed_per_filing = min(self.max_filing_chars, MIN_FILING_CHARS)
        required = self.max_filings_per_symbol * guaranteed_per_filing
        if self.max_filing_chars_per_symbol < required:
            msg = (
                f"max_filing_chars_per_symbol ({self.max_filing_chars_per_symbol}) "
                f"must be >= {required} = max_filings_per_symbol "
                f"({self.max_filings_per_symbol}) * the guaranteed minimum per "
                f"filing ({guaranteed_per_filing} = min(max_filing_chars="
                f"{self.max_filing_chars}, MIN_FILING_CHARS={MIN_FILING_CHARS})); "
                "otherwise filings past the first are always starved of budget"
            )
            raise ValueError(msg)
        return self


class ScheduleConfig(_StrictModel):
    """`schedule.*` in `settings.yaml` (NFR-03)."""

    timeout_minutes: int = Field(default=35, ge=1)


class NotificationConfig(_StrictModel):
    """`notification.*` in `settings.yaml`. Discord notification is opt-in."""

    enabled: bool = False


class PostmortemConfig(_StrictModel):
    """`postmortem.*` in `settings.yaml` (P2-11, roadmap §5 P2-11)."""

    horizon_5d_weight: float = Field(default=0.6, ge=0.0)
    horizon_20d_weight: float = Field(default=0.4, ge=0.0)
    neutral_threshold_pct: float = Field(
        default=0.5, ge=0.0
    )  # |return%| <= this -> NEUTRAL (要検証)
    # return% < -this -> FALSE_POSITIVE_SEVERE (要検証)
    severe_threshold_pct: float = Field(default=2.0, ge=0.0)
    preliminary_sample_threshold: int = Field(default=20, ge=1)
    lookback_window_days: int = Field(default=90, ge=1)

    @model_validator(mode="after")
    def _validate_postmortem_thresholds(self) -> PostmortemConfig:
        if not math.isclose(
            self.horizon_5d_weight + self.horizon_20d_weight,
            1.0,
            abs_tol=1e-9,
        ):
            msg = "postmortem horizon weights must sum to 1.0"
            raise ValueError(msg)
        if self.severe_threshold_pct < self.neutral_threshold_pct:
            msg = "severe_threshold_pct must be >= neutral_threshold_pct"
            raise ValueError(msg)
        return self


class RegimeConfig(_StrictModel):
    """`regime.*` thresholds for the deterministic market gate."""

    sma_period: int = Field(default=200, ge=1)
    bear_spy_sma_ratio: float = Field(default=0.97, gt=0.0, lt=1.0)
    bear_vix_min: float = Field(default=30.0, ge=0.0)
    distribution_window_days: int = Field(default=25, ge=1)
    dd_decline_pct: float = Field(default=-0.002, le=0.0)
    stall_abs_change_pct: float = Field(default=0.001, ge=0.0)
    recovery_pct: float = Field(default=0.05, ge=0.0)
    # Distribution Day level-classification boundaries (roadmap §5 P3-13).
    # severe defaults follow the 2026-08-07 decision (Issue #111; see
    # reports/regime/2026-08-06-dd-threshold-review.md §10). high/caution
    # defaults reproduce the previously hardcoded display boundaries.
    dd_severe_d25: int = Field(default=7, ge=1)
    dd_severe_d15: int = Field(default=6, ge=1)
    dd_high_d25: int = Field(default=5, ge=1)
    dd_high_d15: int = Field(default=3, ge=1)
    dd_high_d5: int = Field(default=2, ge=1)
    dd_caution_d25: int = Field(default=3, ge=1)
    # roadmap §5 P3-16（要検証）: Follow-Through Day thresholds. The state is
    # now also consumed by the exposure gate as a narrow re-entry exception.
    ftd_correction_decline_pct: float = Field(default=0.03, gt=0.0)
    ftd_correction_down_days: int = Field(default=3, ge=1)
    ftd_gain_pct: float = Field(default=0.0125, gt=0.0)

    @model_validator(mode="after")
    def _validate_dd_level_order(self) -> RegimeConfig:
        if not (self.dd_severe_d25 > self.dd_high_d25 > self.dd_caution_d25):
            msg = "dd_severe_d25 must be > dd_high_d25 > dd_caution_d25"
            raise ValueError(msg)
        if not (self.dd_severe_d15 > self.dd_high_d15):
            msg = "dd_severe_d15 must be > dd_high_d15"
            raise ValueError(msg)
        return self


class RetroConfig(_StrictModel):
    """`retro.*` in `settings.yaml` (P8-31, roadmap §5 P8-31).

    Deliberately tiny. The retrospective's evaluation window, horizon weights,
    noise/severity boundaries, and preliminary-sample floor all come from
    `settings.postmortem` (decision D6): a verdict and a signal are measured
    through the same window so their performance stays comparable, and one
    quantity never gets two configurable names.
    """

    # How many MISS_SEVERE symbols get a full evidence dossier in
    # `retro_input.json` (要検証: set from the reading budget of one
    # retrospective pass, not from data). Overflow is truncated by
    # |forward_return| with the dropped count reported, never silently.
    max_surprises: int = Field(default=5, ge=1)


class Settings(_StrictModel):
    """Parsed, validated `config/settings.yaml`."""

    universe: UniverseConfig = UniverseConfig()
    risk: RiskConfig = RiskConfig()
    fundamental_filters: FundamentalFilterConfig = FundamentalFilterConfig()
    technical_signals: TechnicalSignalConfig = TechnicalSignalConfig()
    trade_plan: TradePlanConfig = TradePlanConfig()
    tracking: TrackingConfig = TrackingConfig()
    backtest: BacktestConfig = BacktestConfig()
    analysis: AnalysisConfig = AnalysisConfig()
    schedule: ScheduleConfig = ScheduleConfig()
    notification: NotificationConfig = NotificationConfig()
    postmortem: PostmortemConfig = PostmortemConfig()
    regime: RegimeConfig = RegimeConfig()
    retro: RetroConfig = RetroConfig()


#: Settings a retrospective proposal could plausibly target. Delivery and
#: scheduling plumbing (`notification`, `schedule`) and the universe source are
#: excluded: they are not analysis parameters, and a snapshot that included
#: everything would make the snapshot hash churn for unrelated edits.
#:
#: Lives here rather than in `retro/export.py` (Issue #189) because two callers
#: now need the same nine sections: the dossier's `config_snapshot`, and
#: `pipeline/daily_runner.py`'s `config_versions` ledger row. Two lists would
#: drift, and a drifted snapshot silently splits a comparison window.
CONFIG_SNAPSHOT_SECTIONS: Final = (
    "risk",
    "fundamental_filters",
    "technical_signals",
    "trade_plan",
    "tracking",
    "backtest",
    "analysis",
    "postmortem",
    "regime",
    "retro",
)


# Any: model_dump returns heterogeneous JSON-ready values for each settings section.
def config_snapshot_sections(settings: Settings) -> dict[str, Any]:
    """Return the proposal-relevant settings sections, JSON-ready.

    Args:
        settings: The validated settings a run or an export observed.

    Returns:
        `CONFIG_SNAPSHOT_SECTIONS` mapped to their dumped values, in the order
        the constant declares.
    """
    # Any: `model_dump` is untyped per-section, and the values are handed
    # straight to a JSON serializer / a `JsonValue` pydantic field.
    dumped: dict[str, Any] = settings.model_dump(mode="json")
    return {name: dumped[name] for name in CONFIG_SNAPSHOT_SECTIONS}


# Any: the hash consumes the heterogeneous JSON sections emitted above.
def config_snapshot_hash(sections: Mapping[str, Any]) -> str:
    """Return the SHA-256 fingerprint of one snapshot's sections.

    Distinct from `runs.config_hash`, which fingerprints the *whole* effective
    configuration plus the selected strategy: this one moves only when a
    setting a proposal could target moves.

    Args:
        sections: The mapping `config_snapshot_sections` returned.

    Returns:
        The full hex SHA-256 of the sections' canonical JSON.
    """
    return canonical_json_digest(dict(sections), excluded_field="config_hash")


_SCORE_WEIGHT_SUM_TOLERANCE = 1e-9


class ScoreWeights(_StrictModel):
    """Component weights for the composite ranking score (P1-01, roadmap §5).

    Defaults are unvalidated (要検証); P2-10's sensitivity grid is the
    intended follow-up to ground them empirically.

    Every field declared here is a ranking component: the sum-to-1.0 check and
    `screening/pipeline.py`'s per-component breakdown both enumerate
    `model_fields` rather than repeating the names, so a component added here
    can never be silently left out of one of them (Issue #251).
    """

    rsi_pullback: float = Field(default=0.5, ge=0.0)
    trend_quality: float = Field(default=0.3, ge=0.0)
    liquidity: float = Field(default=0.2, ge=0.0)
    # Rewards volatility, countering `rsi_pullback`'s structural preference
    # for quiet names ("the lower the RSI, the higher the score"). Defaults to
    # 0.0 so no shipped strategy's ranking changes; adopting it is a human
    # decision made against the comparison report.
    atr_pct: float = Field(default=0.0, ge=0.0)
    # Issue #251: strategy-specific components. Every shipped strategy scores
    # candidates with the same pullback-oriented weights above, so
    # `vcp_breakout` -- a breakout strategy -- is ranked by how DEEP a
    # candidate's pullback is, the opposite of its intent. These three read
    # metrics only a particular signal produces, so a non-zero weight requires
    # that signal (`_SCORE_COMPONENT_REQUIRED_SIGNAL` below). They default to
    # 0.0 for the same reason `atr_pct` does: adding the mechanism must not
    # move any shipped strategy's ranking. Choosing non-zero defaults is a
    # separate, evidence-backed decision (issue #251 stage 2).
    pivot_proximity: float = Field(default=0.0, ge=0.0)
    rs_percentile: float = Field(default=0.0, ge=0.0)
    criteria_met: float = Field(default=0.0, ge=0.0)


#: The signal each strategy-specific ranking component reads its metric from.
#: A component absent here is universal (computed from `ranking_metrics`, which
#: every candidate has). Weighting a component whose signal the strategy does
#: not run is rejected at parse time rather than silently scoring every
#: candidate identically on it.
_SCORE_COMPONENT_REQUIRED_SIGNAL: Mapping[str, str] = {
    "pivot_proximity": "vcp_breakout",
    "rs_percentile": "minervini_stage2",
    "criteria_met": "minervini_stage2",
}


class RankingConfig(_StrictModel):
    """`ranking.*` in one `strategies.yaml` strategy entry."""

    score_weights: ScoreWeights = ScoreWeights()


class MinerviniStrategyConfig(_StrictModel):
    """Per-strategy P5-21 acceptance threshold."""

    min_criteria: int = Field(default=6, ge=1, le=7)


class StrategySpec(_StrictModel):
    """Validated composition and ranking rules for one screening strategy."""

    model_config = ConfigDict(frozen=True)

    # YAML sequences deserialize as lists. Keep scalar values strict while
    # accepting that serialization boundary for this immutable tuple API.
    filters_all: tuple[str, ...] = Field(strict=False)
    signals_all: tuple[str, ...] = Field(strict=False)
    candidate_limit: int = Field(gt=0, le=10)
    ranking: RankingConfig = RankingConfig()
    minervini: MinerviniStrategyConfig | None = None


class StrategiesConfig(_StrictModel):
    """Typed contents of `config/strategies.yaml`."""

    model_config = ConfigDict(frozen=True)

    strategies: dict[str, StrategySpec] = Field(min_length=1)

    @model_validator(mode="after")
    def _require_score_weights_sum_to_one(self) -> StrategiesConfig:
        for key, spec in self.strategies.items():
            weights = spec.ranking.score_weights
            total = math.fsum(
                float(getattr(weights, name)) for name in ScoreWeights.model_fields
            )
            if not math.isclose(total, 1.0, abs_tol=_SCORE_WEIGHT_SUM_TOLERANCE):
                msg = (
                    f"strategy '{key}': ranking.score_weights must sum to "
                    f"1.0, got {total}"
                )
                raise ValueError(msg)
        return self

    @model_validator(mode="after")
    def _require_signal_for_weighted_component(self) -> StrategiesConfig:
        """Reject a weighted component the strategy's signals never produce.

        Fail-fast at parse time (before any external I/O) rather than at run
        time: the metric would simply be missing for every candidate, so the
        component would contribute a constant 0.0 and quietly shrink the
        effective weight of the components that do work.
        """
        for key, spec in self.strategies.items():
            weights = spec.ranking.score_weights
            for component, signal_name in _SCORE_COMPONENT_REQUIRED_SIGNAL.items():
                if getattr(weights, component) <= 0.0:
                    continue
                if signal_name not in spec.signals_all:
                    msg = (
                        f"strategy '{key}': ranking.score_weights."
                        f"{component} needs signal '{signal_name}' in "
                        f"signals_all, got {list(spec.signals_all)}"
                    )
                    raise ValueError(msg)
        return self


def load_settings(path: str = "config/settings.yaml") -> Settings:
    """Load and validate `settings.yaml`.

    Args:
        path: Path to the settings YAML file.

    Returns:
        The validated settings object.

    Raises:
        ConfigError: The file is missing, unreadable, not valid UTF-8, not
            valid YAML, or fails schema validation (unknown keys, wrong types,
            missing required values).
    """
    settings_path = Path(path)
    if not settings_path.is_file():
        msg = f"Settings file not found: {settings_path}"
        raise ConfigError(msg)

    text = read_text_document(
        settings_path, label="Settings file", error_type=ConfigError
    )
    try:
        raw = yaml.safe_load(text) or {}
    except yaml.YAMLError as exc:
        msg = f"Settings file is not valid YAML: {settings_path}"
        raise ConfigError(msg) from exc

    try:
        return Settings.model_validate(raw)
    except ValidationError as exc:
        msg = f"Settings file failed validation: {settings_path}\n{exc}"
        raise ConfigError(msg) from exc


def load_strategies(path: str = "config/strategies.yaml") -> StrategiesConfig:
    """Load and validate named screening strategies.

    Args:
        path: Path to the strategies YAML file.

    Returns:
        Typed strategy specifications with bounded candidate counts and
        composite-ranking score weights that sum to 1.0 (P1-01).

    Raises:
        ConfigError: The file is missing, unreadable, not valid UTF-8,
            malformed, or violates the schema.
    """
    strategies_path = Path(path)
    if not strategies_path.is_file():
        msg = f"Strategies file not found: {strategies_path}"
        raise ConfigError(msg)

    text = read_text_document(
        strategies_path, label="Strategies file", error_type=ConfigError
    )
    try:
        raw = yaml.safe_load(text) or {}
    except yaml.YAMLError as exc:
        msg = f"Strategies file is not valid YAML: {strategies_path}"
        raise ConfigError(msg) from exc

    try:
        return StrategiesConfig.model_validate(raw)
    except ValidationError as exc:
        msg = f"Strategies file failed validation: {strategies_path}\n{exc}"
        raise ConfigError(msg) from exc


def load_secrets() -> Secrets:
    """Load secrets from environment variables and a local `.env` if present.

    Returns:
        The secrets object. Missing values are `None`; validate only the
        secrets a given feature actually needs with `require_secrets`.
    """
    return Secrets()


def require_secrets(secrets: Secrets, features: set[str]) -> None:
    """Validate that every secret needed by `features` is present.

    Args:
        secrets: The loaded secrets to check.
        features: Feature keys to validate (see `FEATURE_SECRET_ATTRS`), e.g.
            `{"finnhub", "fred"}`.

    Raises:
        ConfigError: `features` contains an unknown key, or one or more
            required secrets are missing. All missing secrets are reported
            together.
    """
    unknown = features - FEATURE_SECRET_ATTRS.keys()
    if unknown:
        msg = f"require_secrets got unknown feature(s): {sorted(unknown)}"
        raise ConfigError(msg)

    missing = [
        FEATURE_SECRET_ATTRS[feature]
        for feature in sorted(features)
        if getattr(secrets, FEATURE_SECRET_ATTRS[feature]) is None
    ]
    if missing:
        msg = f"Missing required secret(s) for enabled feature(s): {missing}"
        raise ConfigError(msg)
