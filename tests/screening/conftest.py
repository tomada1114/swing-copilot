"""Shared fixtures for screening tests."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pandas as pd
import yaml


@dataclass(frozen=True, slots=True)
class FundamentalsSpec:
    accession_no: str
    fiscal_period_end: date
    filed_at: datetime
    net_income: float
    fcf: float
    equity: float
    assets: float


def make_bars(
    symbol: str, closes: list[float], *, start: date, volume: int = 2_000_000
) -> pd.DataFrame:
    """Build a tidy bars DataFrame for `symbol` from a list of closes.

    High/low are set +/-0.5 around the close and open equals the prior
    close, which is enough for SMA/RSI/ATR to behave sensibly in tests.
    """
    dates = [start + timedelta(days=i) for i in range(len(closes))]
    rows = []
    prev_close = closes[0]
    for bar_date, close in zip(dates, closes, strict=True):
        rows.append(
            {
                "symbol": symbol,
                "date": bar_date,
                "open": prev_close,
                "high": max(prev_close, close) + 0.5,
                "low": min(prev_close, close) - 0.5,
                "close": close,
                "volume": volume,
            }
        )
        prev_close = close
    return pd.DataFrame(rows)


def make_fundamentals_row(symbol: str, spec: FundamentalsSpec) -> dict[str, object]:
    return {
        "accession_no": spec.accession_no,
        "symbol": symbol,
        "form": "10-Q",
        "fiscal_period_end": spec.fiscal_period_end,
        "filed_at": spec.filed_at,
        "revenue": abs(spec.net_income) * 10,
        "net_income": spec.net_income,
        "fcf": spec.fcf,
        "equity": spec.equity,
        "assets": spec.assets,
        "shares": 1_000_000.0,
        "source_url": "https://www.sec.gov/example",
        "fetched_at": datetime(2026, 7, 20, tzinfo=UTC),
    }


def pinned_settings_path(tmp_path: Path) -> Path:
    """A copy of the real settings.yaml with hand-calculated thresholds pinned.

    Mirrors `tests/backtest/test_cli.py::_settings_copy`: the real file stays
    the base, so structural drift still breaks these tests, but the handful of
    numbers a hand-calculated expectation depends on are frozen. Tuning
    `config/settings.yaml` is the workflow `copilot-filter-matrix` exists to
    support and must not turn the suite red on its own.
    """
    raw = yaml.safe_load(Path("config/settings.yaml").read_text(encoding="utf-8"))
    raw["fundamental_filters"] |= {
        "min_profitable_quarters": 4,
        "require_positive_fcf": True,
        "min_equity_ratio": 0.30,
    }
    raw["technical_signals"]["trend"] |= {"sma_short": 50, "sma_long": 200}
    raw["technical_signals"]["pullback"] |= {
        "rsi_period": 14,
        "rsi_threshold": 45,
        "sma_band_pct": 0.03,
        "band_atr_multiple": None,
    }
    raw["technical_signals"]["volume"] |= {
        "avg_volume_days": 20,
        "min_avg_volume": 1_000_000,
    }
    path = tmp_path / "settings-pinned.yaml"
    path.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
    return path


def pinned_strategies_path(tmp_path: Path, **overrides: object) -> Path:
    """A copy of the real strategies.yaml with `default`'s check list pinned.

    Args:
        tmp_path: Directory to write the isolated copy into.
        overrides: Extra `default` keys to replace, e.g. a deliberately
            duplicated or empty `filters_all`/`signals_all`.

    Returns:
        Path to the written YAML, for `--strategies` / `load_strategies`.
    """
    raw = yaml.safe_load(Path("config/strategies.yaml").read_text(encoding="utf-8"))
    raw["strategies"]["default"] |= {
        "filters_all": ["profitable_positive_fcf_equity", "volume_min"],
        "signals_all": ["trend_sma", "pullback_rsi"],
        **overrides,
    }
    path = tmp_path / "strategies-pinned.yaml"
    path.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
    return path
