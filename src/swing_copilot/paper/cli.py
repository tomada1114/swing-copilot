"""Explicit, non-interactive CLI for recording one human decision."""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

from swing_copilot.cli_support import ExitPolicy, run_cli
from swing_copilot.exceptions import SwingCopilotError
from swing_copilot.paper.journal import PaperJournal
from swing_copilot.report.markdown_report import update_markdown_decisions
from swing_copilot.storage.database import DEFAULT_DB_PATH, Database
from swing_copilot.storage.paper_records import TradeDecisionRecord
from swing_copilot.storage.state_store import StateStore


class DecisionCommandError(SwingCopilotError):
    """Raised when a decision command does not identify one audited candidate."""


#: The argparse convention: the message itself is the exit status (stderr, 1).
_EXIT_POLICY = ExitPolicy(errors=(DecisionCommandError,))


@dataclass(frozen=True, slots=True)
class DecisionCommand:
    """Validated-shape input for recording one audited decision."""

    run_id: UUID
    symbol: str
    decision: str
    reason: str | None = None
    fill_price: float | None = None
    strategy_key: str | None = None


def record_decision_command(
    state_store: StateStore,
    command: DecisionCommand,
) -> TradeDecisionRecord:
    """Validate, persist, and refresh the run's generated Markdown decision block."""
    normalized_symbol = command.symbol.strip().upper()
    strategy_keys = state_store.get_candidate_strategy_keys(
        command.run_id, normalized_symbol
    )
    if not strategy_keys:
        msg = (
            f"{normalized_symbol} is not an audited candidate for run {command.run_id}"
        )
        raise DecisionCommandError(msg)
    if command.strategy_key is None:
        if len(strategy_keys) != 1:
            msg = (
                f"candidate matches multiple strategies; choose one of {strategy_keys}"
            )
            raise DecisionCommandError(msg)
        resolved_strategy = strategy_keys[0]
    elif command.strategy_key not in strategy_keys:
        msg = (
            f"strategy {command.strategy_key!r} did not produce {normalized_symbol} "
            f"in run {command.run_id}"
        )
        raise DecisionCommandError(msg)
    else:
        resolved_strategy = command.strategy_key

    journal = PaperJournal(state_store)
    journal.record_decision(
        command.run_id,
        normalized_symbol,
        resolved_strategy,
        command.decision,
        command.reason,
        command.fill_price,
    )
    record = TradeDecisionRecord(
        run_id=command.run_id,
        symbol=normalized_symbol,
        strategy_key=resolved_strategy,
        position_id=None,
        decision=command.decision,
        reason_memo=command.reason,
        virtual_fill_price=command.fill_price,
    )
    report_path = state_store.get_run_report_path(command.run_id)
    if report_path is not None and report_path.is_file():
        update_markdown_decisions(
            report_path, state_store.get_trade_decisions(command.run_id)
        )
        latest_path = report_path.parent.parent / "latest.md"
        if (
            latest_path.is_file()
            and state_store.get_latest_run_report_path() == report_path
        ):
            update_markdown_decisions(
                latest_path, state_store.get_trade_decisions(command.run_id)
            )
    return record


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="copilot-decision")
    parser.add_argument("--run-id", required=True, type=UUID)
    parser.add_argument("--symbol", required=True)
    parser.add_argument(
        "--decision", required=True, choices=("followed", "ignored", "modified")
    )
    parser.add_argument("--reason")
    parser.add_argument("--fill-price", type=float)
    parser.add_argument("--strategy")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    """Record one human decision and exit with a concise confirmation."""
    args = _parse_args(argv)
    state_store = StateStore(Database(args.db))
    state_store.init_schema()
    command = DecisionCommand(
        run_id=args.run_id,
        symbol=args.symbol,
        decision=args.decision,
        reason=args.reason,
        fill_price=args.fill_price,
        strategy_key=args.strategy,
    )
    record = run_cli(
        lambda: record_decision_command(state_store, command), _EXIT_POLICY
    )
    sys.stdout.write(
        f"Recorded {record.decision}: {record.symbol} "
        f"({record.strategy_key}, run {record.run_id})\n"
    )


if __name__ == "__main__":  # pragma: no cover
    main()
