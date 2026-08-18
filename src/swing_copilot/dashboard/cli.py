"""`copilot-dashboard`: serve the read-only viewer on localhost.

Binds `127.0.0.1` by default and has no authentication, because it has no
write path and nothing to authorize — it is a local viewer over a local file.
Exposing it on `0.0.0.0` would publish the decision history to the network,
so the default is never widened silently.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import uvicorn

from swing_copilot.cli_support import ExitPolicy, run_cli
from swing_copilot.dashboard import queries
from swing_copilot.dashboard.app import create_app
from swing_copilot.research import ResearchError
from swing_copilot.storage.database import DEFAULT_DB_PATH

DEFAULT_REPORTS_DIR = Path("reports")
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8787

#: The argparse convention: the message itself is the exit status (stderr, 1).
_EXIT_POLICY = ExitPolicy(errors=(ResearchError,))


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="copilot-dashboard",
        description=(
            "蓄積された日次分析結果を閲覧するローカルダッシュボード（読み取り専用）"
        ),
    )
    parser.add_argument(
        "--db",
        type=Path,
        default=DEFAULT_DB_PATH,
        help=f"読み取る DuckDB ファイル（既定: {DEFAULT_DB_PATH}）",
    )
    parser.add_argument(
        "--reports-dir",
        type=Path,
        default=DEFAULT_REPORTS_DIR,
        help=f"run アーカイブのルート（既定: {DEFAULT_REPORTS_DIR}）",
    )
    parser.add_argument(
        "--host",
        default=DEFAULT_HOST,
        help=f"バインドするホスト（既定: {DEFAULT_HOST}）",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=DEFAULT_PORT,
        help=f"待ち受けポート（既定: {DEFAULT_PORT}）",
    )
    return parser.parse_args(argv)


def _serve(args: argparse.Namespace) -> None:
    """Verify the database is readable, then serve until interrupted.

    The preflight read is deliberate: a wrong `--db` should say so in the
    terminal the operator is already looking at, not only inside a browser
    page they have yet to open. It is one short read-only query through
    `research`, so it takes and releases the file lock in milliseconds.
    """
    queries.runs(args.db)
    app = create_app(db_path=args.db, reports_root=args.reports_dir)
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")


def main(argv: list[str] | None = None) -> None:
    """Start the dashboard server.

    Args:
        argv: Command-line arguments; `None` reads `sys.argv`.
    """
    args = _parse_args(argv)
    run_cli(lambda: _serve(args), _EXIT_POLICY)


if __name__ == "__main__":  # pragma: no cover
    main()
