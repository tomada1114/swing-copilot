"""`copilot-dashboard` argument handling, preflight, and bind defaults."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from swing_copilot.dashboard import cli

if TYPE_CHECKING:
    from tests.dashboard.conftest import Builder, Fixture


@pytest.fixture
def served(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, object]]:
    """Capture what would have been served instead of binding a port."""
    calls: list[dict[str, object]] = []

    def fake_run(app: object, **kwargs: object) -> None:
        calls.append({"app": app, **kwargs})

    monkeypatch.setattr("uvicorn.run", fake_run)
    return calls


class TestDefaults:
    def test_binds_loopback_only(
        self, served: list[dict[str, object]], builder: Builder, dashboard_db: Fixture
    ) -> None:
        # Never widen this silently: the dashboard has no authentication
        # because it is a local viewer, not because the history is public.
        builder.run()

        cli.main(["--db", str(dashboard_db.db_path)])

        assert served[0]["host"] == "127.0.0.1"
        assert served[0]["port"] == 8787

    def test_host_and_port_are_overridable(
        self, served: list[dict[str, object]], builder: Builder, dashboard_db: Fixture
    ) -> None:
        builder.run()

        cli.main(
            ["--db", str(dashboard_db.db_path), "--host", "0.0.0.0", "--port", "9001"]  # noqa: S104 - asserting the flag is honoured, not a default
        )

        assert served[0]["host"] == "0.0.0.0"  # noqa: S104
        assert served[0]["port"] == 9001

    def test_reports_directory_defaults_to_the_pipeline_output(self) -> None:
        args = cli._parse_args([])  # noqa: SLF001 - the parser is the unit under test

        assert args.reports_dir == Path("reports")
        assert args.db == Path("data/copilot.duckdb")


class TestPreflight:
    def test_an_unreadable_database_exits_before_the_server_starts(
        self, served: list[dict[str, object]], tmp_path: Path
    ) -> None:
        # Failing here puts the message in the terminal the operator is
        # already watching, instead of only inside a page they must open.
        with pytest.raises(SystemExit, match="database file not found"):
            cli.main(["--db", str(tmp_path / "absent.duckdb")])

        assert served == []

    def test_a_readable_database_reaches_the_server(
        self, served: list[dict[str, object]], builder: Builder, dashboard_db: Fixture
    ) -> None:
        builder.run()

        cli.main(
            [
                "--db",
                str(dashboard_db.db_path),
                "--reports-dir",
                str(dashboard_db.reports_root),
            ]
        )

        assert len(served) == 1
