"""Routes for the local read-only dashboard.

Deliberately thin: a route validates its path parameters, asks `queries` for
the frames, hands them to a view model, and renders. Every interpretation of
the data lives in `viewmodels/`, and every read is a short-lived read-only
connection opened inside `swing_copilot.research`. Nothing here writes, and
nothing here holds a connection.
"""

from __future__ import annotations

from http import HTTPStatus
from typing import TYPE_CHECKING, Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from swing_copilot.dashboard import queries, templating, viewmodels
from swing_copilot.dashboard.templating import STATIC_DIR, Chrome
from swing_copilot.dashboard.viewmodels import common
from swing_copilot.research import ResearchError
from swing_copilot.tracking.board import DEFAULT_PUBLISHED_RETENTION_BUSINESS_DAYS

if TYPE_CHECKING:
    from pathlib import Path

    from swing_copilot.dashboard.models import RunRef

_ENSURE_VIEWS_HINT = (
    'uv run python -c "from swing_copilot import research; research.ensure_views()"'
)

_MISSING_VIEWS_NOTE = (
    "分析ビュー（v_*）がこのデータベースに存在しない。ダッシュボードは読み取り専用で、"
    "ビュー作成には読み書き接続が必要なため自分では作らない。次を別シェルで一度実行する:"
)


def create_app(
    db_path: Path,
    reports_root: Path,
    *,
    tracking_retention_business_days: int = DEFAULT_PUBLISHED_RETENTION_BUSINESS_DAYS,
) -> FastAPI:
    """Build the dashboard application.

    Args:
        db_path: The DuckDB file to read. Never opened read-write.
        reports_root: The daily pipeline's output directory, read only to
            detect runs whose analysis phase never finished.
        tracking_retention_business_days: How long closed recommendations stay
            visible on `/tracking`; passed in as a plain value so the
            dashboard remains independent of settings files.

    Returns:
        A FastAPI application with no write route of any kind.
    """
    app = FastAPI(title="swing-copilot dashboard", docs_url=None, redoc_url=None)
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    environment = templating.build_environment()

    def page(template: str, context: dict[str, Any], status: int = 200) -> Response:
        body = templating.render(environment, template, context)
        return HTMLResponse(body, status_code=status)

    def error_page(title: str, detail: str, status: int, hint: str = "") -> Response:
        return page(
            "error.html",
            {
                "chrome": Chrome(runs=(), current_run_id=None, nav=""),
                "title": title,
                "detail": detail,
                "hint": hint,
            },
            status,
        )

    def chrome(current_run_id: str | None, nav: str) -> Chrome:
        return Chrome(
            runs=common.run_refs(queries.runs(db_path)),
            current_run_id=current_run_id,
            nav=nav,
        )

    def find_run(run_id: str) -> RunRef:
        run = next(
            (
                item
                for item in common.run_refs(queries.runs(db_path))
                if item.run_id == run_id
            ),
            None,
        )
        if run is None:
            raise HTTPException(
                status_code=HTTPStatus.NOT_FOUND,
                detail=f"run が見つからない: {run_id}",
            )
        return run

    @app.exception_handler(ResearchError)
    async def _research_error(_request: Request, exc: ResearchError) -> Response:
        """Turn a read failure into an instruction, not a stack trace."""
        message = str(exc)
        is_missing_views = "ensure_views" in message
        return error_page(
            "データベースを読めない",
            f"{_MISSING_VIEWS_NOTE} {message}" if is_missing_views else message,
            HTTPStatus.SERVICE_UNAVAILABLE,
            hint=_ENSURE_VIEWS_HINT if is_missing_views else "",
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(_request: Request, exc: StarletteHTTPException) -> Response:
        """Render every HTTP error as a page, including an unrouted URL.

        Registered on Starlette's base class rather than FastAPI's subclass:
        a request for a path no route matches raises the base class, and a
        JSON body there would be the one place the viewer stops being HTML.
        """
        return error_page("見つからない", str(exc.detail), exc.status_code)

    @app.get("/", include_in_schema=False)
    def index() -> Response:
        runs = common.run_refs(queries.runs(db_path))
        if not runs:
            return error_page(
                "run がまだ無い",
                "このデータベースには runs 行が1件も無い。"
                "`copilot-daily` を1度実行すると最初の run が記録される。",
                HTTPStatus.NOT_FOUND,
            )
        return RedirectResponse(f"/runs/{runs[0].run_id}", status_code=HTTPStatus.FOUND)

    @app.get("/runs/{run_id}", response_class=HTMLResponse)
    def run_page(run_id: str) -> Response:
        run = find_run(run_id)
        overview = viewmodels.build_run_overview(
            viewmodels.RunSources(
                run=run,
                regime=queries.regime_snapshots(db_path),
                candidates=queries.candidates_for_run(db_path, run_id),
                scorecard=queries.scorecard_for_run(db_path, run_id),
                rejections=queries.rejections_for_run(db_path, run_id),
                is_analysis_missing=run_id
                in queries.analysis_missing_run_ids(db_path, reports_root),
            )
        )
        return page(
            "run.html",
            {"chrome": chrome(run_id, "run"), "view": overview},
        )

    @app.get("/runs/{run_id}/symbols/{symbol}", response_class=HTMLResponse)
    def symbol_page(run_id: str, symbol: str) -> Response:
        run = find_run(run_id)
        detail = viewmodels.build_symbol_detail(
            viewmodels.SymbolSources(
                run=run,
                symbol=symbol,
                candidates=queries.candidates_for_run(db_path, run_id),
                scorecard=queries.scorecard_for_run(db_path, run_id),
                reasons=queries.reasons_for_symbol(db_path, run_id, symbol),
                positions=queries.tracked_positions(db_path),
            )
        )
        if detail is None:
            raise HTTPException(
                status_code=HTTPStatus.NOT_FOUND,
                detail=f"この run に {symbol} の候補も verdict も無い",
            )
        return page(
            "symbol.html",
            {"chrome": chrome(run_id, "run"), "view": detail},
        )

    @app.get("/history", response_class=HTMLResponse)
    def history_page() -> Response:
        view = viewmodels.build_history(
            viewmodels.HistorySources(
                scorecard=queries.scorecard(db_path),
                regime=queries.regime_snapshots(db_path),
                positions=queries.tracked_positions(db_path),
            )
        )
        return page(
            "history.html",
            {"chrome": chrome(None, "history"), "view": view},
        )

    @app.get("/tracking", response_class=HTMLResponse)
    def tracking_page() -> Response:
        view = viewmodels.build_tracking(
            viewmodels.TrackingSources(
                positions=queries.tracked_positions(db_path),
                retention_business_days=tracking_retention_business_days,
            )
        )
        return page(
            "tracking.html",
            {"chrome": chrome(None, "tracking"), "view": view},
        )

    return app
