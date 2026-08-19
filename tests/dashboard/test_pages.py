"""Route-level contracts, exercised in-process through Starlette's TestClient.

`TestClient` speaks ASGI directly, so nothing here opens a socket and the
autouse network guard in `tests/conftest.py` stays satisfied.
"""

from __future__ import annotations

from http import HTTPStatus
from typing import TYPE_CHECKING

import duckdb
import pytest
from starlette.testclient import TestClient

from swing_copilot.dashboard import create_app, guidance
from tests.dashboard.conftest import RUN_ID, Builder, Fixture, write_run_archive

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture
def client(dashboard_db: Fixture) -> TestClient:
    app = create_app(
        db_path=dashboard_db.db_path, reports_root=dashboard_db.reports_root
    )
    return TestClient(app)


@pytest.fixture
def populated(builder: Builder) -> Builder:
    """One finished run with a candidate, a verdict, and a rejection."""
    builder.run()
    builder.universe("AAPL")
    builder.regime()
    builder.candidate("AAPL")
    builder.verdict("AAPL", recommendation="proceed")
    builder.risk("AAPL")
    builder.reason("AAPL", index=0, text="開示に新規の懸念は見当たらない")
    builder.outcome(
        "AAPL", horizon_days=5, forward_return_pct=2.5, classification="HIT"
    )
    builder.position("AAPL")
    builder.rejection(
        "BBB", stage="fundamental_filter", reason_code="FILTER_LOW_LIQUIDITY"
    )
    return builder


@pytest.mark.usefixtures("populated")
class TestIndex:
    def test_redirects_to_the_newest_run(self, client: TestClient) -> None:
        response = client.get("/", follow_redirects=False)

        assert response.status_code == HTTPStatus.FOUND
        assert response.headers["location"] == f"/runs/{RUN_ID}"


class TestIndexWithoutRuns:
    def test_an_empty_database_explains_itself_instead_of_redirecting(
        self, client: TestClient
    ) -> None:
        response = client.get("/", follow_redirects=False)

        assert response.status_code == HTTPStatus.NOT_FOUND
        assert "run がまだ無い" in response.text


@pytest.mark.usefixtures("populated")
class TestRunPage:
    def test_renders_the_regime_verdict_and_rejection_sections(
        self, client: TestClient
    ) -> None:
        response = client.get(f"/runs/{RUN_ID}")

        assert response.status_code == HTTPStatus.OK
        for expected in (
            "2027-03-01",
            "BULL",
            "CAUTION",
            "proceed",
            "approved",
            "FILTER_LOW_LIQUIDITY",
            f"/runs/{RUN_ID}/symbols/AAPL",
        ):
            assert expected in response.text

    def test_shows_the_null_vocabulary_legend(self, client: TestClient) -> None:
        response = client.get(f"/runs/{RUN_ID}")

        assert "このページの NULL 語彙" in response.text
        assert "計測導入前" not in response.text, "only this page's tokens belong here"

    def test_an_unknown_run_is_a_rendered_404(self, client: TestClient) -> None:
        response = client.get("/runs/does-not-exist")

        assert response.status_code == HTTPStatus.NOT_FOUND
        assert "run が見つからない" in response.text

    def test_an_unrouted_path_is_also_a_rendered_page(self, client: TestClient) -> None:
        response = client.get("/no/such/page")

        assert response.status_code == HTTPStatus.NOT_FOUND
        assert "<html" in response.text


@pytest.mark.usefixtures("populated")
class TestAnalysisPendingBanner:
    def test_a_run_whose_analysis_never_finished_is_flagged(
        self, client: TestClient, dashboard_db: Fixture
    ) -> None:
        write_run_archive(dashboard_db.reports_root, has_result=False)

        response = client.get(f"/runs/{RUN_ID}")

        assert "分析待ち" in response.text

    def test_a_finished_run_is_not_flagged(
        self, client: TestClient, dashboard_db: Fixture
    ) -> None:
        write_run_archive(dashboard_db.reports_root, has_result=True)

        response = client.get(f"/runs/{RUN_ID}")

        assert "分析待ち" not in response.text


@pytest.mark.usefixtures("populated")
class TestSymbolPage:
    def test_renders_reasons_scores_tracking_and_outcomes(
        self, client: TestClient
    ) -> None:
        response = client.get(f"/runs/{RUN_ID}/symbols/AAPL")

        assert response.status_code == HTTPStatus.OK
        for expected in (
            "開示に新規の懸念は見当たらない",
            "Information Technology",
            "101.25",
            "HIT",
            "+2.50%",
            "open",
        ):
            assert expected in response.text

    def test_a_symbol_absent_from_the_run_is_a_rendered_404(
        self, client: TestClient
    ) -> None:
        response = client.get(f"/runs/{RUN_ID}/symbols/ZZZZ")

        assert response.status_code == HTTPStatus.NOT_FOUND
        assert "候補も verdict も無い" in response.text

    def test_an_unknown_run_is_rejected_before_the_symbol_lookup(
        self, client: TestClient
    ) -> None:
        response = client.get("/runs/does-not-exist/symbols/AAPL")

        assert response.status_code == HTTPStatus.NOT_FOUND
        assert "run が見つからない" in response.text


@pytest.mark.usefixtures("populated")
class TestHistoryPage:
    def test_renders_the_facets_and_the_ledger(self, client: TestClient) -> None:
        response = client.get("/history")

        assert response.status_code == HTTPStatus.OK
        assert "判定成績" in response.text
        assert "<svg" in response.text
        assert "同じ数字を表で見る" in response.text
        assert f"/runs/{RUN_ID}/symbols/AAPL" in response.text


class TestHistoryPageWithoutData:
    def test_an_empty_database_still_renders_every_section(
        self, client: TestClient
    ) -> None:
        response = client.get("/history")

        assert response.status_code == HTTPStatus.OK
        assert "満期を迎えた verdict がまだ無い" in response.text
        assert "建玉中の仮想ポジションは無い" in response.text


@pytest.mark.usefixtures("populated")
class TestReadingHints:
    """Every section that shows a judgement carries its own "how to read it".

    The text lives in `dashboard/guidance.py`; these assertions pin that it
    reaches the page, so a section can never ship the values without the
    caption that makes them interpretable.
    """

    def test_the_run_page_explains_the_regime_scales(self, client: TestClient) -> None:
        response = client.get(f"/runs/{RUN_ID}")

        assert guidance.REGIME.summary in response.text
        assert guidance.REGIME.details[0] in response.text

    def test_the_run_page_explains_the_outcome_direction(
        self, client: TestClient
    ) -> None:
        response = client.get(f"/runs/{RUN_ID}")

        assert guidance.OUTCOME.summary in response.text

    def test_the_run_page_explains_the_verdict_ingestion_lag(
        self, client: TestClient
    ) -> None:
        response = client.get(f"/runs/{RUN_ID}")

        assert guidance.VERDICT_INGESTION.summary in response.text

    def test_the_symbol_page_explains_the_outcome_direction(
        self, client: TestClient
    ) -> None:
        response = client.get(f"/runs/{RUN_ID}/symbols/AAPL")

        assert guidance.OUTCOME.summary in response.text
        assert guidance.OUTCOME.details[-1] in response.text

    def test_the_history_page_explains_facets_regime_and_ledger(
        self, client: TestClient
    ) -> None:
        response = client.get("/history")

        assert guidance.CLASSIFICATION_FACETS.summary in response.text
        assert guidance.REGIME_TIMELINE.summary in response.text
        assert guidance.LEDGER.summary in response.text


@pytest.mark.usefixtures("populated")
class TestVerdictIngestionHintPlacement:
    def test_a_pending_run_states_the_lag_in_the_banner_only(
        self, client: TestClient, dashboard_db: Fixture
    ) -> None:
        # Both texts say the same thing; showing them together two sections
        # apart would read as a contradiction rather than a clarification.
        write_run_archive(dashboard_db.reports_root, has_result=False)

        response = client.get(f"/runs/{RUN_ID}")

        assert guidance.ANALYSIS_PENDING in response.text
        assert guidance.VERDICT_INGESTION.summary not in response.text


class TestUnreadableDatabase:
    def test_a_missing_file_reports_the_path_not_a_traceback(
        self, tmp_path: Path
    ) -> None:
        client = TestClient(
            create_app(db_path=tmp_path / "absent.duckdb", reports_root=tmp_path)
        )

        response = client.get("/history")

        assert response.status_code == HTTPStatus.SERVICE_UNAVAILABLE
        assert "database file not found" in response.text

    def test_a_database_without_the_views_prints_the_ensure_views_command(
        self, tmp_path: Path
    ) -> None:
        # A file that predates the analysis views. The dashboard must not
        # create them itself: that needs a read-write connection, which is
        # exactly what this process refuses to open.
        empty = tmp_path / "pre_views.duckdb"
        with duckdb.connect(str(empty)) as connection:
            connection.execute("CREATE TABLE placeholder (x INTEGER)")

        client = TestClient(create_app(db_path=empty, reports_root=tmp_path))
        response = client.get("/history")

        assert response.status_code == HTTPStatus.SERVICE_UNAVAILABLE
        assert "research.ensure_views()" in response.text


class TestStaticAssets:
    def test_the_stylesheet_is_served_and_needs_no_external_host(
        self, client: TestClient
    ) -> None:
        response = client.get("/static/app.css")

        assert response.status_code == HTTPStatus.OK
        directives = [
            line.strip()
            for line in response.text.splitlines()
            if line.strip().startswith("@import") or "url(http" in line
        ]
        assert directives == [], "the stylesheet must not fetch anything"
