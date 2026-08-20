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
layer at all. The `verdicts` table cannot serve here -- it is filled later by
`copilot-retro collect` scanning `reports/`, so it lags the run that produced
it. A run with no candidates owes no analysis, which is what the candidate
count decides.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from swing_copilot import research

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_REPORTS_DIR = REPO_ROOT / "reports"


class IncompleteRunError(Exception):
    """The most recent run owes a qualitative analysis that is not there."""


def _latest_run(db_path: Path | None) -> tuple[str, str]:
    """Return `(run_id, run_date)` of the most recently started run."""
    runs = research.runs(db_path=db_path) if db_path else research.runs()
    if runs.empty:
        message = "runs テーブルが空である (パイプラインが走っていない)"
        raise IncompleteRunError(message)
    latest = runs.sort_values("started_at").iloc[-1]
    # `run_date` arrives as a pandas Timestamp; the reports tree is keyed by the
    # bare ISO date, so drop the time component rather than stringifying it.
    run_date = str(latest["run_date"])[:10]
    return str(latest["run_id"]), run_date


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


def check(reports_dir: Path, db_path: Path | None = None) -> None:
    """Raise `IncompleteRunError` unless the latest run produced its analysis."""
    run_id, run_date = _latest_run(db_path)
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
    args = parser.parse_args(argv)
    try:
        check(args.reports_dir, args.db)
    except IncompleteRunError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
