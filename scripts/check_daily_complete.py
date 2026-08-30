"""Fail the scheduled run when its qualitative analysis never landed.

The GitHub Actions job runs the deterministic pipeline and the `swing-daily`
skill in one step, and that step reports success as long as the Claude session
exits cleanly. A session can exit cleanly having produced almost nothing --
observed once with two of thirty analysis fragments on disk, no
`analysis_result.json` and no ingest -- and the day then looks green while the
report carries an empty qualitative layer.

This check runs *after* the R2 push on purpose. The prices, fundamentals and
ledger updates of a half-finished day are still worth persisting; what must not
happen is that the day passes for complete. So the data is published first and
the job is failed afterwards, loudly.

The signal is `analysis_result.json`: the skill's only contracted artifact, and
the file `copilot-ingest-analysis` needs in order to render the qualitative
layer at all. It stays the signal even now that `reports/` is synced through
R2 and `copilot-retro collect` runs inside the same job (Issue #370): the
`verdicts` table would work as a signal in principle, but `analysis_result.json`
is the simpler one already sitting on the contract boundary, so there is no
reason to switch. A run with no candidates owes no analysis, which is what the
candidate count decides.

Because `reports/` is now pulled from R2 at job start, the workspace can hold
*previous* days' `analysis_result.json` files too. Left unscoped, that would
turn this check into a false negative on a day the pipeline never ran at all:
it would find yesterday's report, call the (nonexistent) run of today
complete, and the day would look green having analyzed nothing. `--started-after`
closes that gap by requiring the latest run to have actually started in this
job (see `check()`).

`--outcome-file` (Issue #372) closes a related gap from the other direction:
`copilot-daily` itself now writes its terminal outcome to this path on every
exit, `PreflightAbort` included (`pipeline/daily_composition.py`). When it is
given, a missing file means `copilot-daily` never even ran -- independent of
whatever the DB happens to hold from a previous day -- and an `outcome` of
`"preflight_abort"` is a legitimate stop, not an incomplete day, only when its
`reason` is on the `_LEGITIMATE_STOP_REASONS` whitelist (`same_day_rerun`,
`no_trading_day`). That is deliberately a whitelist rather than "any
preflight_abort passes": Issue #376 found a `price_fetch_failed` reason (a
data-provider outage during the closed-session `run_date` check) sharing the
same `outcome` value, and treating every `preflight_abort` as legitimate would
have turned that failure into a silently green job. A reason outside the
whitelist -- including `price_fetch_failed`, an unrecognized future reason, or
a missing/`null` reason -- fails this check instead of passing it.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

from swing_copilot import research

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_REPORTS_DIR = REPO_ROOT / "reports"


class IncompleteRunError(Exception):
    """The most recent run owes a qualitative analysis that is not there."""


#: Abort reasons that mean "this day legitimately has no analysis to produce".
#: Deliberately a whitelist, not `outcome == "preflight_abort"`: a new abort
#: reason must be classified on purpose, and an unrecognized one is treated as
#: an incomplete day rather than silently turning the job green (Issue #372,
#: hardened by #376).
_LEGITIMATE_STOP_REASONS = frozenset({"same_day_rerun", "no_trading_day"})


def _as_aware_utc(value: datetime) -> datetime:
    """Attach UTC to a naive `datetime`; leave an already-aware one alone.

    `runs.started_at` is a DuckDB `TIMESTAMPTZ` and always comes back as an
    aware `pandas.Timestamp` (verified against `research.runs()`), but this
    keeps a naive/aware mismatch from ever raising on comparison regardless.
    """
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _latest_run(db_path: Path | None) -> tuple[str, str, datetime]:
    """Return `(run_id, run_date, started_at)` of the most recently started run."""
    runs = research.runs(db_path=db_path) if db_path else research.runs()
    if runs.empty:
        message = "runs テーブルが空である (パイプラインが走っていない)"
        raise IncompleteRunError(message)
    latest = runs.sort_values("started_at").iloc[-1]
    # `run_date` arrives as a pandas Timestamp; the reports tree is keyed by the
    # bare ISO date, so drop the time component rather than stringifying it.
    run_date = str(latest["run_date"])[:10]
    started_at = latest["started_at"].to_pydatetime()
    return str(latest["run_id"]), run_date, started_at


def _candidate_count(run_id: str, db_path: Path | None) -> int:
    """Return how many screening candidates the run handed to the analysis."""
    sql = "SELECT count(*) AS candidates FROM candidates WHERE run_id = ?"
    params = [run_id]
    frame = (
        research.query(sql, params=params, db_path=db_path)
        if db_path
        else research.query(sql, params=params)
    )
    return int(frame.iloc[0]["candidates"])


def _outcome_file_is_a_legitimate_stop(outcome_file: Path) -> bool:
    """Whether `outcome_file` says this day was legitimately not analyzed.

    Implements the table from Issue #372, hardened by #376: a missing file
    means `copilot-daily` never started at all (fails loudly here, independent
    of whatever the DB holds from an earlier day); an `outcome` of
    `"preflight_abort"` whose `reason` is in `_LEGITIMATE_STOP_REASONS` means
    it started and stopped for a documented, non-actionable reason (e.g. no
    closed trading day yet); anything else -- including a `preflight_abort`
    with an unrecognized or missing `reason` (`price_fetch_failed` included)
    -- fails this check outright rather than falling through to the
    candidate-count / `analysis_result.json` check below, since a `run_date`
    was never even resolved for this day to look up.

    Args:
        outcome_file: Path `copilot-daily` was told to write its terminal
            outcome to.

    Returns:
        Whether `check()` should pass without consulting the database.

    Raises:
        IncompleteRunError: The file is missing, unreadable as JSON, or
            records a `preflight_abort` whose `reason` is not a legitimate
            stop.
    """
    if not outcome_file.exists():
        message = (
            f"outcome ファイル {outcome_file} が無い。"
            "copilot-daily が一度も起動していない。"
        )
        raise IncompleteRunError(message)
    try:
        payload = json.loads(outcome_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        message = f"outcome ファイル {outcome_file} を読み込めない: {error}"
        raise IncompleteRunError(message) from error
    outcome = payload.get("outcome")
    if outcome != "preflight_abort":
        return False
    reason = payload.get("reason")
    if reason not in _LEGITIMATE_STOP_REASONS:
        message = (
            f"copilot-daily は preflight abort したが reason={reason!r} は"
            "正当な中止として認められていない。その日は分析されていない。"
        )
        raise IncompleteRunError(message)
    print(f"copilot-daily は preflight abort で正常終了 (reason={reason})。OK")
    return True


def check(
    reports_dir: Path,
    db_path: Path | None = None,
    started_after: datetime | None = None,
    outcome_file: Path | None = None,
) -> None:
    """Raise `IncompleteRunError` unless the latest run produced its analysis.

    Args:
        reports_dir: Where the daily run archive lives.
        db_path: DuckDB file to read (default: the operator's live database).
        started_after: When given, the latest run must have started at or
            after this (aware) timestamp. Without it, a previous day's run --
            visible in the workspace now that `reports/` is pulled from R2 --
            could vouch for a day this job never actually ran.
        outcome_file: When given, `copilot-daily`'s own terminal-outcome file
            (Issue #372). Its absence fails immediately; an `outcome` of
            `"preflight_abort"` passes immediately only when `reason` is on
            the `_LEGITIMATE_STOP_REASONS` whitelist, and fails immediately
            otherwise (Issue #376); any other `outcome` falls through to the
            checks below, unchanged. Omitting it (the default) leaves
            existing callers' behavior untouched.
    """
    if outcome_file is not None and _outcome_file_is_a_legitimate_stop(outcome_file):
        return

    run_id, run_date, started_at = _latest_run(db_path)
    if started_after is not None and _as_aware_utc(started_at) < _as_aware_utc(
        started_after
    ):
        message = (
            f"最新の run {run_id} ({run_date}) の開始時刻 {started_at.isoformat()} が"
            f" このジョブの開始時刻 {started_after.isoformat()} より前。"
            " このジョブ自身の run が無い (copilot-daily が走っていない)。"
        )
        raise IncompleteRunError(message)

    candidates = _candidate_count(run_id, db_path)
    result_path = reports_dir / run_date / run_id / "analysis_result.json"

    if candidates == 0:
        print(f"run {run_id} ({run_date}): 候補 0 件のため定性分析は不要。OK")
        return
    if result_path.exists():
        print(
            f"run {run_id} ({run_date}): 候補 {candidates} 件、analysis_result.json あり。OK"
        )
        return

    message = (
        f"run {run_id} ({run_date}) は候補 {candidates} 件を出したのに"
        f" {result_path} が無い。定性分析が完了していない。"
        " データは R2 へ書き戻し済みなので、その日の分析だけを対話セッションの"
        " /swing-daily で再入して仕上げられる。"
    )
    raise IncompleteRunError(message)


def _parse_started_after(value: str) -> datetime:
    """Parse `--started-after` into an aware UTC `datetime`.

    Raises:
        ValueError: When `value` does not parse as ISO-8601 -- argparse turns
            this into a normal CLI usage error.
    """
    return _as_aware_utc(datetime.fromisoformat(value))


def main(argv: list[str] | None = None) -> int:
    """CLI entry point. Returns 1 when the run is missing its analysis."""
    parser = argparse.ArgumentParser(
        prog="check_daily_complete",
        description="直近の run が定性分析まで完了しているかを確認する",
    )
    parser.add_argument(
        "--reports-dir",
        type=Path,
        default=DEFAULT_REPORTS_DIR,
        help="レポートの出力先 (既定: リポジトリの reports/)",
    )
    parser.add_argument("--db", type=Path, default=None, help="DuckDB ファイルのパス")
    parser.add_argument(
        "--started-after",
        type=_parse_started_after,
        default=None,
        help=(
            "この時刻 (ISO-8601, UTC 推奨) より前に開始した run は、"
            "このジョブ自身の run とは認めない (既定: 制限なし)"
        ),
    )
    parser.add_argument(
        "--outcome-file",
        type=Path,
        default=None,
        help=(
            "copilot-daily が書いた終了状態 JSON のパス (既定: 未指定=従来どおり)。"
            "ファイルが無ければ即失敗、outcome=preflight_abort なら即合格とする"
        ),
    )
    args = parser.parse_args(argv)
    try:
        check(args.reports_dir, args.db, args.started_after, args.outcome_file)
    except IncompleteRunError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
