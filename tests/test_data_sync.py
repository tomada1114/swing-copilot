"""Tests for scripts/data_sync.py (offline: the object store is a fake)."""

from __future__ import annotations

import json
import shutil
import stat
import sys
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import pytest

from swing_copilot.storage.market_store import (
    BARS_FORMAT_MARKER_NAME,
    BarsFormatError,
    validate_bars_format,
)
from tests.support.script_loader import load_script_module

if TYPE_CHECKING:
    from pathlib import Path

data_sync = load_script_module("data_sync", "scripts/data_sync.py")

FIXED_NOW = datetime(2026, 8, 19, 23, 0, tzinfo=UTC)
DUCKDB_KEY = "data/copilot.duckdb"
BARS_2024_KEY = "data/bars/year=2024/data.parquet"
BARS_2025_KEY = "data/bars/year=2025/data.parquet"
BARS_MARKER_KEY = f"data/bars/{BARS_FORMAT_MARKER_NAME}"
MARKER_BODY = b'{"basis": "raw", "version": 2}\n'

REPORT_RUN_ID = "33333333-3333-4333-8333-333333333333"
REPORT_RUN_DATE = "2026-08-19"
REPORT_MD_KEY = f"reports/{REPORT_RUN_DATE}/{REPORT_RUN_ID}.md"
REPORT_RESULT_KEY = f"reports/{REPORT_RUN_DATE}/{REPORT_RUN_ID}/analysis_result.json"


class UploadFailedError(RuntimeError):
    """Injected transport failure, distinct from any DataSyncError."""


class FakeObjectStore:
    """In-memory stand-in for the S3-compatible subset `data_sync` uses."""

    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.uploaded: list[str] = []
        self.written: list[str] = []
        self.deleted: list[str] = []
        self.downloaded: list[str] = []
        self.fail_on_write: set[str] = set()
        self.fail_on_delete: set[str] = set()

    def _guard(self, key: str) -> None:
        if key in self.fail_on_write:
            msg = f"injected failure writing {key}"
            raise UploadFailedError(msg)

    def read_bytes(self, key: str) -> bytes | None:
        return self.objects.get(key)

    def write_bytes(self, key: str, body: bytes) -> None:
        self._guard(key)
        self.objects[key] = body
        self.written.append(key)

    def upload(self, key: str, source: Path) -> None:
        self._guard(key)
        self.objects[key] = source.read_bytes()
        self.uploaded.append(key)

    def download(self, key: str, destination: Path) -> None:
        body = self.objects.get(key)
        if body is None:
            msg = f"no such key: {key}"
            raise KeyError(msg)
        destination.write_bytes(body)
        self.downloaded.append(key)

    def delete(self, key: str) -> None:
        if key in self.fail_on_delete:
            msg = f"injected failure deleting {key}"
            raise UploadFailedError(msg)
        self.objects.pop(key, None)
        self.deleted.append(key)

    def list_keys(self, prefix: str) -> list[str]:
        return sorted(key for key in self.objects if key.startswith(prefix))


def _write(path: Path, body: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(body)


def make_workspace(root: Path, name: str = "workspace") -> Path:
    """Create an isolated `data/` tree with the two synced artifact shapes."""
    data_dir = root / name / "data"
    _write(data_dir / "copilot.duckdb", b"duckdb-v1")
    _write(data_dir / "bars" / "year=2024" / "data.parquet", b"bars-2024-v1")
    _write(data_dir / "bars" / "year=2025" / "data.parquet", b"bars-2025-v1")
    return data_dir


def make_reports_workspace(root: Path, name: str = "workspace") -> Path:
    """Create an isolated `reports/` tree holding one daily run archive."""
    reports_dir = root / name / "reports"
    _write(reports_dir / REPORT_RUN_DATE / f"{REPORT_RUN_ID}.md", b"report-md-v1")
    _write(
        reports_dir / REPORT_RUN_DATE / REPORT_RUN_ID / "analysis_result.json",
        b"result-v1",
    )
    return reports_dir


def _run_id(n: int) -> str:
    """A distinct valid-UUID-shaped run id for the `n`th synthetic run."""
    return f"00000000-0000-4000-8000-{n:012d}"


def _seed_report_run_dates(reports_dir: Path, dates: list[str]) -> dict[str, str]:
    """Write one minimal run archive (`.md` + `analysis_result.json`) per date.

    Returns the date -> run_id mapping so callers can build expected keys.
    """
    run_ids: dict[str, str] = {}
    for i, run_date in enumerate(dates):
        run_id = _run_id(i)
        run_ids[run_date] = run_id
        _write(reports_dir / run_date / f"{run_id}.md", f"md-{run_date}".encode())
        _write(
            reports_dir / run_date / run_id / "analysis_result.json",
            f"result-{run_date}".encode(),
        )
    return run_ids


def _report_keys_for_date(run_date: str, run_id: str) -> tuple[str, str]:
    """The `.md` and `analysis_result.json` object keys for one seeded run."""
    return (
        f"reports/{run_date}/{run_id}.md",
        f"reports/{run_date}/{run_id}/analysis_result.json",
    )


def _roots(data_dir: Path) -> Any:
    """Build both sync roots for a workspace, given only its `data/` path.

    `data_dir` is always `<workspace>/data` (see `make_workspace`), so its
    sibling `reports/` is derived rather than threaded through every call.
    """
    return data_sync.build_roots(data_dir, data_dir.parent / "reports")


# `data_sync` is spec-loaded, so everything it returns is dynamically typed.
def push(store: FakeObjectStore, data_dir: Path) -> Any:
    return data_sync.push(store, _roots(data_dir), now=lambda: FIXED_NOW)


def pull(store: FakeObjectStore, data_dir: Path) -> Any:
    return data_sync.pull(store, _roots(data_dir))


def status(store: FakeObjectStore, data_dir: Path) -> Any:
    return data_sync.status(store, _roots(data_dir))


def read_manifest(store: FakeObjectStore) -> Any:
    return json.loads(store.objects[data_sync.MANIFEST_KEY])


# --------------------------------------------------------------------------- #
#  push: first publication and the round trip
# --------------------------------------------------------------------------- #


def test_push_to_empty_bucket_writes_generation_one_manifest(tmp_path):
    data_dir = make_workspace(tmp_path)
    store = FakeObjectStore()

    report = push(store, data_dir)

    assert report.generation == 1
    assert set(report.uploaded) == {DUCKDB_KEY, BARS_2024_KEY, BARS_2025_KEY}
    assert report.unchanged == ()
    manifest = read_manifest(store)
    assert manifest["generation"] == 1
    assert set(manifest["files"]) == {DUCKDB_KEY, BARS_2024_KEY, BARS_2025_KEY}
    assert manifest["files"][DUCKDB_KEY]["size"] == len(b"duckdb-v1")
    assert store.objects[DUCKDB_KEY] == b"duckdb-v1"
    assert data_sync.read_state(data_dir).generation == 1


def test_push_manifest_records_sha256_and_size_of_every_synced_file(tmp_path):
    data_dir = make_workspace(tmp_path)
    store = FakeObjectStore()

    push(store, data_dir)

    manifest = read_manifest(store)
    for key, expected in (
        (DUCKDB_KEY, b"duckdb-v1"),
        (BARS_2024_KEY, b"bars-2024-v1"),
        (BARS_2025_KEY, b"bars-2025-v1"),
    ):
        entry = manifest["files"][key]
        assert entry["size"] == len(expected)
        assert len(entry["sha256"]) == 64


def test_pull_then_push_round_trip_uploads_only_the_changed_file(tmp_path):
    origin = make_workspace(tmp_path, "origin")
    store = FakeObjectStore()
    push(store, origin)

    mirror = tmp_path / "mirror" / "data"
    mirror.mkdir(parents=True)
    pull_report = pull(store, mirror)

    assert pull_report.generation == 1
    assert set(pull_report.downloaded) == {DUCKDB_KEY, BARS_2024_KEY, BARS_2025_KEY}
    assert (mirror / "copilot.duckdb").read_bytes() == b"duckdb-v1"
    assert status(store, mirror).status is data_sync.SyncStatus.IN_SYNC

    (mirror / "copilot.duckdb").write_bytes(b"duckdb-v2")
    store.uploaded.clear()
    push_report = push(store, mirror)

    assert push_report.generation == 2
    assert push_report.uploaded == (DUCKDB_KEY,)
    assert set(push_report.unchanged) == {BARS_2024_KEY, BARS_2025_KEY}
    assert store.uploaded == [DUCKDB_KEY]
    assert store.objects[DUCKDB_KEY] == b"duckdb-v2"
    assert read_manifest(store)["generation"] == 2


def test_pull_reuses_local_files_whose_sha256_already_matches(tmp_path):
    origin = make_workspace(tmp_path, "origin")
    store = FakeObjectStore()
    push(store, origin)

    mirror = make_workspace(tmp_path, "mirror")
    report = pull(store, mirror)

    assert report.downloaded == ()
    assert set(report.skipped) == {DUCKDB_KEY, BARS_2024_KEY, BARS_2025_KEY}


def test_pull_writes_downloaded_files_owner_only(tmp_path):
    """A pulled file must stay `0600`, never inherit the process umask.

    `_download_verified` used to stage through `tempfile.mkstemp`, which is
    `0600` by construction; replacing it with a plain `Path`-based staging
    file silently widened every pulled artifact -- the DuckDB trading
    history and the run archive -- to whatever `0666 & ~umask` resolves to
    (typically `0644`, group/world-readable). Pinned here so that regression
    cannot recur unnoticed.
    """
    origin = make_workspace(tmp_path, "origin")
    store = FakeObjectStore()
    push(store, origin)

    mirror = tmp_path / "mirror" / "data"
    mirror.mkdir(parents=True)
    pull(store, mirror)

    for relative in ("copilot.duckdb", "bars/year=2024/data.parquet"):
        mode = stat.S_IMODE((mirror / relative).stat().st_mode)
        assert mode == 0o600, f"{relative}: expected 0o600, got {oct(mode)}"


# --------------------------------------------------------------------------- #
#  push: the optimistic lock
# --------------------------------------------------------------------------- #


def test_push_rejects_when_remote_generation_moved_ahead_and_uploads_nothing(tmp_path):
    origin = make_workspace(tmp_path, "origin")
    store = FakeObjectStore()
    push(store, origin)

    stale = tmp_path / "stale" / "data"
    stale.mkdir(parents=True)
    pull(store, stale)

    # Somebody else pulls, works, and pushes generation 2 in the meantime.
    other = tmp_path / "other" / "data"
    other.mkdir(parents=True)
    pull(store, other)
    (other / "copilot.duckdb").write_bytes(b"duckdb-from-elsewhere")
    push(store, other)

    (stale / "copilot.duckdb").write_bytes(b"duckdb-stale-edit")
    store.uploaded.clear()
    store.written.clear()
    store.deleted.clear()

    with pytest.raises(data_sync.ConcurrentWriteError, match=r"別の場所で書き換え"):
        push(store, stale)

    assert store.uploaded == []
    assert store.written == []
    assert store.deleted == []
    assert read_manifest(store)["generation"] == 2
    assert store.objects[DUCKDB_KEY] == b"duckdb-from-elsewhere"
    assert data_sync.read_state(stale).generation == 1


def test_push_without_local_state_but_with_remote_manifest_demands_pull_first(tmp_path):
    origin = make_workspace(tmp_path, "origin")
    store = FakeObjectStore()
    push(store, origin)

    fresh = make_workspace(tmp_path, "fresh")
    store.uploaded.clear()
    store.written.clear()

    with pytest.raises(data_sync.DataSyncError, match=r"先に pull する"):
        push(store, fresh)

    assert store.uploaded == []
    assert store.written == []
    assert read_manifest(store)["generation"] == 1


def test_push_refuses_an_empty_local_tree_rather_than_emptying_the_remote(tmp_path):
    origin = make_workspace(tmp_path, "origin")
    store = FakeObjectStore()
    push(store, origin)

    empty = tmp_path / "empty" / "data"
    empty.mkdir(parents=True)
    pull(store, empty)
    for path in [*empty.rglob("*.parquet"), empty / "copilot.duckdb"]:
        path.unlink()
    store.deleted.clear()

    with pytest.raises(data_sync.DataSyncError, match=r"1 件もない"):
        push(store, empty)

    assert store.deleted == []
    assert read_manifest(store)["generation"] == 1


# --------------------------------------------------------------------------- #
#  Exclusions
# --------------------------------------------------------------------------- #


def test_local_only_files_are_never_uploaded_nor_mirror_deleted(tmp_path):
    data_dir = make_workspace(tmp_path)
    dry_run = data_dir / "copilot_dry_run.duckdb"
    dry_run.write_bytes(b"dry-run")
    backup = data_dir / "copilot.duckdb.bak-20260811"
    backup.write_bytes(b"backup")
    backed_up_partition = data_dir / "bars" / "year=2024.bak-20260811" / "data.parquet"
    _write(backed_up_partition, b"partition-backup")
    store = FakeObjectStore()

    report = push(store, data_dir)

    assert set(report.uploaded) == {DUCKDB_KEY, BARS_2024_KEY, BARS_2025_KEY}
    assert not [key for key in store.objects if "bak-" in key or "dry_run" in key]

    pull_report = pull(store, data_dir)

    assert pull_report.deleted == ()
    assert dry_run.read_bytes() == b"dry-run"
    assert backup.read_bytes() == b"backup"
    assert backed_up_partition.read_bytes() == b"partition-backup"


def test_sync_state_file_is_not_part_of_the_synced_set(tmp_path):
    data_dir = make_workspace(tmp_path)
    store = FakeObjectStore()

    push(store, data_dir)

    assert (data_dir / data_sync.STATE_FILE_NAME).is_file()
    assert not [key for key in store.objects if data_sync.STATE_FILE_NAME in key]


def test_bars_format_marker_travels_with_its_partitions_to_a_fresh_mirror(tmp_path):
    """The marker must sync, or every fresh runner refuses the store it pulled.

    `MarketStore` fails closed on a partitioned bars root whose adjustment
    -basis marker is missing (Issue #413), and the scheduled run always starts
    from an empty checkout. Mirroring the Parquet files without `_format.json`
    would therefore publish a store that no CI run can read, with an error
    telling the operator to run the rebuild they had just run.
    """
    origin = make_workspace(tmp_path, "origin")
    _write(origin / "bars" / BARS_FORMAT_MARKER_NAME, MARKER_BODY)
    store = FakeObjectStore()

    push_report = push(store, origin)

    assert BARS_MARKER_KEY in push_report.uploaded

    mirror = tmp_path / "mirror" / "data"
    mirror.mkdir(parents=True)
    pull_report = pull(store, mirror)

    assert BARS_MARKER_KEY in pull_report.downloaded
    # The end-to-end invariant, not merely "a file arrived": the store itself
    # accepts the mirrored tree.
    validate_bars_format(mirror / "bars")


def test_a_mirror_without_the_bars_format_marker_is_rejected_by_the_store(tmp_path):
    """The counterfactual that pins why the test above matters."""
    origin = make_workspace(tmp_path, "origin")
    store = FakeObjectStore()
    push(store, origin)

    mirror = tmp_path / "mirror" / "data"
    mirror.mkdir(parents=True)
    pull(store, mirror)

    assert BARS_MARKER_KEY not in store.objects
    with pytest.raises(BarsFormatError):
        validate_bars_format(mirror / "bars")


# --------------------------------------------------------------------------- #
#  reports/ shape predicate
# --------------------------------------------------------------------------- #


def test_reports_shape_predicate_includes_only_the_run_archive(tmp_path):
    reports_dir = tmp_path / "workspace" / "reports"
    included = {
        reports_dir / REPORT_RUN_DATE / f"{REPORT_RUN_ID}.md",
        reports_dir / REPORT_RUN_DATE / REPORT_RUN_ID / "analysis_result.json",
        reports_dir / REPORT_RUN_DATE / REPORT_RUN_ID / "analysis_work" / "news-1.json",
    }
    excluded = {
        reports_dir / "backtests" / "2026-08-17-strategy-comparison.md",
        reports_dir / "dry_run" / REPORT_RUN_DATE / f"{REPORT_RUN_ID}.md",
        reports_dir / "assets" / "style.css",
        reports_dir / "retro" / REPORT_RUN_DATE / "retro_result.json",
        reports_dir / "latest.md",
        reports_dir / REPORT_RUN_DATE / "not-a-uuid.md",
        reports_dir / "not-a-date" / f"{REPORT_RUN_ID}.md",
    }
    for path in included | excluded:
        _write(path, b"x")

    roots = data_sync.build_roots(tmp_path / "workspace" / "data", reports_dir)
    scanned = data_sync.scan_local(roots)

    expected_keys = {
        f"reports/{path.relative_to(reports_dir).as_posix()}" for path in included
    }
    assert set(scanned) == expected_keys


# --------------------------------------------------------------------------- #
#  push/pull: both roots together, one shared generation
# --------------------------------------------------------------------------- #


def test_pull_then_push_round_trip_covers_both_roots_with_one_generation(tmp_path):
    origin = make_workspace(tmp_path, "origin")
    make_reports_workspace(tmp_path, "origin")
    store = FakeObjectStore()

    report = push(store, origin)

    assert report.generation == 1
    manifest = read_manifest(store)
    assert manifest["generation"] == 1
    assert {
        DUCKDB_KEY,
        BARS_2024_KEY,
        BARS_2025_KEY,
        REPORT_MD_KEY,
        REPORT_RESULT_KEY,
    } <= set(manifest["files"])

    mirror = tmp_path / "mirror" / "data"
    mirror.mkdir(parents=True)
    pull_report = pull(store, mirror)

    assert pull_report.generation == 1
    assert {REPORT_MD_KEY, REPORT_RESULT_KEY} <= set(pull_report.downloaded)
    mirror_reports = tmp_path / "mirror" / "reports"
    assert (
        mirror_reports / REPORT_RUN_DATE / f"{REPORT_RUN_ID}.md"
    ).read_bytes() == b"report-md-v1"
    assert (
        mirror_reports / REPORT_RUN_DATE / REPORT_RUN_ID / "analysis_result.json"
    ).read_bytes() == b"result-v1"

    # Editing only a `reports/` file still advances the single shared
    # generation counter -- there is no separate reports-side counter.
    (mirror_reports / REPORT_RUN_DATE / f"{REPORT_RUN_ID}.md").write_bytes(
        b"report-md-v2"
    )
    push_report = push(store, mirror)

    assert push_report.generation == 2
    assert push_report.uploaded == (REPORT_MD_KEY,)
    assert data_sync.read_state(mirror).generation == 2
    assert read_manifest(store)["generation"] == 2


def test_push_refuses_to_empty_the_remote_reports_tree_when_reports_dir_is_missing(
    tmp_path,
):
    origin = make_workspace(tmp_path, "origin")
    make_reports_workspace(tmp_path, "origin")
    store = FakeObjectStore()
    push(store, origin)

    # A checkout that pulled but then lost its `reports/` tree entirely (or
    # was never given one) must not be allowed to publish a manifest with
    # zero `reports/` keys -- the GC step would then delete the remote
    # `reports/` history.
    stale = tmp_path / "stale" / "data"
    stale.mkdir(parents=True)
    pull(store, stale)
    shutil.rmtree(tmp_path / "stale" / "reports")
    store.deleted.clear()
    store.uploaded.clear()

    with pytest.raises(data_sync.DataSyncError, match=r"reports/ 配下"):
        push(store, stale)

    assert store.deleted == []
    assert store.uploaded == []
    assert read_manifest(store)["generation"] == 1


def test_push_gc_deletes_unreferenced_keys_under_both_prefixes_and_leaves_manifest_alone(
    tmp_path,
):
    origin = make_workspace(tmp_path, "origin")
    make_reports_workspace(tmp_path, "origin")
    store = FakeObjectStore()
    push(store, origin)

    (origin / "bars" / "year=2024" / "data.parquet").unlink()
    (
        origin.parent
        / "reports"
        / REPORT_RUN_DATE
        / REPORT_RUN_ID
        / "analysis_result.json"
    ).unlink()

    report = push(store, origin)

    assert set(report.deleted) == {BARS_2024_KEY, REPORT_RESULT_KEY}
    assert BARS_2024_KEY not in store.objects
    assert REPORT_RESULT_KEY not in store.objects
    assert REPORT_MD_KEY in store.objects  # still referenced, untouched
    assert data_sync.MANIFEST_KEY in store.objects
    assert read_manifest(store)["generation"] == 2


# --------------------------------------------------------------------------- #
#  pull: verification, mirroring, atomic replacement
# --------------------------------------------------------------------------- #


def test_append_only_push_rejects_a_rewritten_published_report(tmp_path):
    """The unattended job must not be able to republish a rewritten archive.

    The analysis session reads untrusted news/filing text and can Write/Edit
    under `reports/`, which now holds the whole canonical history. Rewriting a
    historical `analysis_result.json` would be re-collected (the scan is
    digest-based) and then made permanent by the push.
    """
    workspace = make_workspace(tmp_path)
    reports = make_reports_workspace(tmp_path)
    store = FakeObjectStore()
    push(store, workspace)

    (reports / REPORT_RUN_DATE / REPORT_RUN_ID / "analysis_result.json").write_bytes(
        b"result-rewritten-by-injection"
    )

    with pytest.raises(data_sync.DataSyncError, match="append-only"):
        data_sync.push(
            store,
            _roots(workspace),
            now=lambda: FIXED_NOW,
            append_only_prefixes=(data_sync.REPORTS_PREFIX,),
        )

    assert store.objects[REPORT_RESULT_KEY] == b"result-v1"
    assert read_manifest(store)["generation"] == 1


def test_append_only_push_rejects_a_deleted_published_report(tmp_path):
    workspace = make_workspace(tmp_path)
    reports = make_reports_workspace(tmp_path)
    store = FakeObjectStore()
    push(store, workspace)

    (reports / REPORT_RUN_DATE / REPORT_RUN_ID / "analysis_result.json").unlink()

    with pytest.raises(data_sync.DataSyncError, match="append-only"):
        data_sync.push(
            store,
            _roots(workspace),
            now=lambda: FIXED_NOW,
            append_only_prefixes=(data_sync.REPORTS_PREFIX,),
        )

    assert REPORT_RESULT_KEY in store.objects


def test_append_only_push_still_admits_a_new_run_directory(tmp_path):
    """Append-only blocks rewrites, not the day's own new archive."""
    workspace = make_workspace(tmp_path)
    reports = make_reports_workspace(tmp_path)
    store = FakeObjectStore()
    push(store, workspace)

    new_run = "44444444-4444-4444-8444-444444444444"
    _write(reports / "2026-08-20" / new_run / "analysis_result.json", b"today")

    report = data_sync.push(
        store,
        _roots(workspace),
        now=lambda: FIXED_NOW,
        append_only_prefixes=(data_sync.REPORTS_PREFIX,),
    )

    assert f"reports/2026-08-20/{new_run}/analysis_result.json" in report.uploaded
    assert store.objects[REPORT_RESULT_KEY] == b"result-v1"


def test_push_without_append_only_still_allows_correcting_an_archive(tmp_path):
    """Interactive correction and re-collection stays supported (design D2)."""
    workspace = make_workspace(tmp_path)
    reports = make_reports_workspace(tmp_path)
    store = FakeObjectStore()
    push(store, workspace)

    (reports / REPORT_RUN_DATE / REPORT_RUN_ID / "analysis_result.json").write_bytes(
        b"result-corrected"
    )

    report = push(store, workspace)

    assert REPORT_RESULT_KEY in report.uploaded
    assert store.objects[REPORT_RESULT_KEY] == b"result-corrected"


# --------------------------------------------------------------------------- #
#  pull --reports-window / push: Issue #373
# --------------------------------------------------------------------------- #

# 15 run dates: newest 10 are the window, oldest 5 fall outside it.
_WINDOW_DATES = [f"2026-08-{day:02d}" for day in range(1, 16)]
_WINDOW_NEWEST_10 = sorted(_WINDOW_DATES, reverse=True)[:10]
_WINDOW_OLDEST_5 = sorted(_WINDOW_DATES, reverse=True)[10:]


def test_windowed_pull_fetches_only_the_newest_n_run_dates(tmp_path):
    origin = make_workspace(tmp_path, "origin")
    run_ids = _seed_report_run_dates(origin.parent / "reports", _WINDOW_DATES)
    store = FakeObjectStore()
    push(store, origin)

    mirror = tmp_path / "mirror" / "data"
    mirror.mkdir(parents=True)
    report = data_sync.pull(store, _roots(mirror), reports_window=10)

    assert report.reports_window == 10
    downloaded_dates = {
        key.split("/")[1] for key in report.downloaded if key.startswith("reports/")
    }
    assert downloaded_dates == set(_WINDOW_NEWEST_10)
    # Never GET a key belonging to an out-of-window run date.
    assert not any(
        key.startswith("reports/") and key.split("/")[1] in _WINDOW_OLDEST_5
        for key in store.downloaded
    )
    mirror_reports = tmp_path / "mirror" / "reports"
    for run_date in _WINDOW_OLDEST_5:
        assert not (mirror_reports / run_date).exists()
    for run_date in _WINDOW_NEWEST_10:
        run_id = run_ids[run_date]
        assert (mirror_reports / run_date / f"{run_id}.md").is_file()
        assert data_sync.read_state(mirror).reports_window == 10


def test_windowed_pull_then_push_never_deletes_out_of_window_remote_keys(tmp_path):
    """The most important guarantee: a windowed pull's push must not GC history.

    Without GC suppression, `push`'s garbage collector would see every
    out-of-window `reports/` key as "not in local" (the windowed pull never
    fetched it) and delete it from the remote -- destroying the archive a
    windowed CI pull exists specifically to leave alone.
    """
    origin = make_workspace(tmp_path, "origin")
    run_ids = _seed_report_run_dates(origin.parent / "reports", _WINDOW_DATES)
    store = FakeObjectStore()
    push(store, origin)
    remote_keys_before = {key for key in store.objects if key != data_sync.MANIFEST_KEY}
    assert len(remote_keys_before) == len(_WINDOW_DATES) * 2 + 3  # reports + data/

    mirror = tmp_path / "mirror" / "data"
    mirror.mkdir(parents=True)
    data_sync.pull(store, _roots(mirror), reports_window=10)

    # The unattended job's own new run for today, always present locally.
    new_run_date, new_run_id = "2026-08-16", _run_id(999)
    _write(mirror.parent / "reports" / new_run_date / f"{new_run_id}.md", b"todays-run")

    store.deleted.clear()
    push_report = data_sync.push(store, _roots(mirror), now=lambda: FIXED_NOW)

    assert push_report.deleted == ()
    assert store.deleted == []
    assert remote_keys_before <= set(store.objects)

    # And the out-of-window bytes are still exactly what they were --
    # untouched, not merely undeleted.
    for run_date in _WINDOW_OLDEST_5:
        md_key, _ = _report_keys_for_date(run_date, run_ids[run_date])
        assert store.objects[md_key] == f"md-{run_date}".encode()

    # The new manifest still references the out-of-window archives too, not
    # just the ones this working copy actually holds -- otherwise a later
    # full pull would read their absence from the manifest as a deletion and
    # wipe them from an operator's complete local copy.
    manifest = read_manifest(store)
    for run_date in _WINDOW_OLDEST_5:
        md_key, result_key = _report_keys_for_date(run_date, run_ids[run_date])
        assert md_key in manifest["files"]
        assert result_key in manifest["files"]


def test_windowed_pull_then_push_still_never_deletes_out_of_window_keys_with_an_orphan_present(
    tmp_path,
):
    """Issue #382 regression: narrowing GC to the key level must not regress #373's guarantee above.

    Same setup as `test_windowed_pull_then_push_never_deletes_out_of_window_remote_keys`,
    plus an orphan present at the same time -- an object an earlier,
    interrupted push uploaded but whose manifest write never happened, so the
    orphan is absent from `remote.files` too. The orphan (an in-window run
    date) is reclaimed; every out-of-window archive key is untouched, exactly
    as without the orphan.
    """
    origin = make_workspace(tmp_path, "origin")
    run_ids = _seed_report_run_dates(origin.parent / "reports", _WINDOW_DATES)
    store = FakeObjectStore()
    push(store, origin)
    remote_keys_before = {key for key in store.objects if key != data_sync.MANIFEST_KEY}

    mirror = tmp_path / "mirror" / "data"
    mirror.mkdir(parents=True)
    data_sync.pull(store, _roots(mirror), reports_window=10)

    new_run_date, new_run_id = "2026-08-16", _run_id(999)
    _write(mirror.parent / "reports" / new_run_date / f"{new_run_id}.md", b"todays-run")

    orphan_run_date = _WINDOW_NEWEST_10[0]
    orphan_key = f"reports/{orphan_run_date}/{_run_id(998)}/analysis_result.json"
    store.objects[orphan_key] = b"orphaned-upload"

    push_report = data_sync.push(store, _roots(mirror), now=lambda: FIXED_NOW)

    assert push_report.deleted == (orphan_key,)
    assert orphan_key not in store.objects
    assert remote_keys_before <= set(store.objects)
    for run_date in _WINDOW_OLDEST_5:
        md_key, result_key = _report_keys_for_date(run_date, run_ids[run_date])
        assert store.objects[md_key] == f"md-{run_date}".encode()
        assert store.objects[result_key] == f"result-{run_date}".encode()


def test_windowed_pull_then_push_reclaims_an_orphan_from_an_interrupted_push(tmp_path):
    """A genuine orphan is exactly what windowed GC exists to recover (Issue #382).

    Uploaded but never referenced by any manifest -- this push deletes that
    orphan and nothing else.
    """
    origin = make_workspace(tmp_path, "origin")
    _seed_report_run_dates(origin.parent / "reports", _WINDOW_DATES)
    store = FakeObjectStore()
    push(store, origin)

    mirror = tmp_path / "mirror" / "data"
    mirror.mkdir(parents=True)
    data_sync.pull(store, _roots(mirror), reports_window=10)

    orphan_run_date = _WINDOW_NEWEST_10[0]
    orphan_key = f"reports/{orphan_run_date}/{_run_id(998)}/analysis_result.json"
    store.objects[orphan_key] = b"orphaned-upload"

    push_report = data_sync.push(store, _roots(mirror), now=lambda: FIXED_NOW)

    assert push_report.deleted == (orphan_key,)
    assert orphan_key not in store.objects


def test_windowed_pull_then_push_leaves_an_out_of_window_orphan_alone(tmp_path):
    """Accepted trade-off (Issue #382): an out-of-window orphan is not reclaimed.

    It is left for an operator's full pull -> collect -> push, the same
    recovery path #373 already relies on for out-of-window corrections.
    """
    origin = make_workspace(tmp_path, "origin")
    _seed_report_run_dates(origin.parent / "reports", _WINDOW_DATES)
    store = FakeObjectStore()
    push(store, origin)

    mirror = tmp_path / "mirror" / "data"
    mirror.mkdir(parents=True)
    data_sync.pull(store, _roots(mirror), reports_window=10)

    orphan_run_date = _WINDOW_OLDEST_5[0]
    orphan_key = f"reports/{orphan_run_date}/{_run_id(997)}/analysis_result.json"
    store.objects[orphan_key] = b"orphaned-upload"

    push_report = data_sync.push(store, _roots(mirror), now=lambda: FIXED_NOW)

    assert orphan_key not in push_report.deleted
    assert orphan_key in store.objects


def test_windowed_pull_then_push_never_deletes_an_in_window_archive_missing_only_locally(
    tmp_path,
):
    """Issue #382 regression: a real published archive is never an orphan, even if locally absent.

    A genuine orphan (what `_is_reclaimable_reports_orphan` exists to reclaim)
    was uploaded but never referenced by any committed manifest. This case is
    different: the key IS in the last committed remote manifest -- it is a
    real, previously-published archive -- and is merely missing from the
    local tree for some unrelated reason (an accidental local delete, a
    partial checkout, a bug elsewhere), not because it fell outside the
    recorded window. The reclaim predicate must tell these apart and must
    never delete the latter.
    """
    origin = make_workspace(tmp_path, "origin")
    run_ids = _seed_report_run_dates(origin.parent / "reports", _WINDOW_DATES)
    store = FakeObjectStore()
    push(store, origin)

    mirror = tmp_path / "mirror" / "data"
    mirror.mkdir(parents=True)
    data_sync.pull(store, _roots(mirror), reports_window=10)

    # An in-window archive, present in `remote_files` (this push's manifest
    # will read it back from the store), goes missing from the local mirror
    # for a reason unrelated to windowing.
    affected_date = _WINDOW_NEWEST_10[3]
    affected_run_id = run_ids[affected_date]
    md_key, result_key = _report_keys_for_date(affected_date, affected_run_id)
    shutil.rmtree(mirror.parent / "reports" / affected_date)

    push_report = data_sync.push(store, _roots(mirror), now=lambda: FIXED_NOW)

    assert md_key not in push_report.deleted
    assert result_key not in push_report.deleted
    assert store.objects[md_key] == f"md-{affected_date}".encode()
    assert store.objects[result_key] == f"result-{affected_date}".encode()


def test_windowed_pull_then_push_never_deletes_an_unrecognized_reports_key(tmp_path):
    """A key that does not look like a run archive is left alone rather than guessed at (Issue #382).

    `_has_reports_sync_shape` is a hard requirement, independent of whether
    the key's apparent run date happens to fall inside or outside the window.
    """
    origin = make_workspace(tmp_path, "origin")
    _seed_report_run_dates(origin.parent / "reports", _WINDOW_DATES)
    store = FakeObjectStore()
    push(store, origin)

    mirror = tmp_path / "mirror" / "data"
    mirror.mkdir(parents=True)
    data_sync.pull(store, _roots(mirror), reports_window=10)

    junk_key = "reports/junk.txt"
    malformed_key = "reports/2026-08-16/notauuid/x"
    store.objects[junk_key] = b"junk"
    store.objects[malformed_key] = b"malformed"

    push_report = data_sync.push(store, _roots(mirror), now=lambda: FIXED_NOW)

    assert junk_key not in push_report.deleted
    assert malformed_key not in push_report.deleted
    assert junk_key in store.objects
    assert malformed_key in store.objects


def test_windowed_pull_then_push_still_publishes_a_new_run_directory(tmp_path):
    origin = make_workspace(tmp_path, "origin")
    _seed_report_run_dates(origin.parent / "reports", _WINDOW_DATES)
    store = FakeObjectStore()
    push(store, origin)

    mirror = tmp_path / "mirror" / "data"
    mirror.mkdir(parents=True)
    data_sync.pull(store, _roots(mirror), reports_window=10)

    new_run_date, new_run_id = "2026-08-16", _run_id(999)
    new_key = f"reports/{new_run_date}/{new_run_id}.md"
    _write(mirror.parent / "reports" / new_run_date / f"{new_run_id}.md", b"new-run")

    push_report = data_sync.push(store, _roots(mirror), now=lambda: FIXED_NOW)

    assert new_key in push_report.uploaded
    assert store.objects[new_key] == b"new-run"
    assert new_key in read_manifest(store)["files"]


def test_windowed_pull_then_append_only_push_still_rejects_an_in_window_rewrite(
    tmp_path,
):
    origin = make_workspace(tmp_path, "origin")
    _seed_report_run_dates(origin.parent / "reports", _WINDOW_DATES)
    store = FakeObjectStore()
    push(store, origin)

    mirror = tmp_path / "mirror" / "data"
    mirror.mkdir(parents=True)
    data_sync.pull(store, _roots(mirror), reports_window=10)

    newest = _WINDOW_NEWEST_10[0]
    run_id = _run_id(_WINDOW_DATES.index(newest))
    result_path = mirror.parent / "reports" / newest / run_id / "analysis_result.json"
    original = result_path.read_bytes()
    result_path.write_bytes(b"rewritten-by-injection")

    with pytest.raises(data_sync.DataSyncError, match="append-only"):
        data_sync.push(
            store,
            _roots(mirror),
            now=lambda: FIXED_NOW,
            append_only_prefixes=(data_sync.REPORTS_PREFIX,),
        )

    _, result_key = _report_keys_for_date(newest, run_id)
    assert store.objects[result_key] == original


def test_windowed_pull_then_append_only_push_still_rejects_an_in_window_deletion(
    tmp_path,
):
    origin = make_workspace(tmp_path, "origin")
    _seed_report_run_dates(origin.parent / "reports", _WINDOW_DATES)
    store = FakeObjectStore()
    push(store, origin)

    mirror = tmp_path / "mirror" / "data"
    mirror.mkdir(parents=True)
    data_sync.pull(store, _roots(mirror), reports_window=10)

    newest = _WINDOW_NEWEST_10[0]
    run_id = _run_id(_WINDOW_DATES.index(newest))
    (mirror.parent / "reports" / newest / run_id / "analysis_result.json").unlink()

    with pytest.raises(data_sync.DataSyncError, match="append-only"):
        data_sync.push(
            store,
            _roots(mirror),
            now=lambda: FIXED_NOW,
            append_only_prefixes=(data_sync.REPORTS_PREFIX,),
        )


def test_windowed_pull_then_append_only_push_does_not_flag_out_of_window_absence(
    tmp_path,
):
    """An out-of-window key missing locally is expected, not a violation.

    The append-only guard must not treat "outside the recorded window" the
    same as "deleted by the analysis session" -- GC suppression is what
    protects those keys, and the guard would otherwise make every windowed CI
    push fail.
    """
    origin = make_workspace(tmp_path, "origin")
    _seed_report_run_dates(origin.parent / "reports", _WINDOW_DATES)
    store = FakeObjectStore()
    push(store, origin)

    mirror = tmp_path / "mirror" / "data"
    mirror.mkdir(parents=True)
    data_sync.pull(store, _roots(mirror), reports_window=10)

    new_run_date, new_run_id = "2026-08-16", _run_id(999)
    _write(mirror.parent / "reports" / new_run_date / f"{new_run_id}.md", b"todays-run")

    report = data_sync.push(
        store,
        _roots(mirror),
        now=lambda: FIXED_NOW,
        append_only_prefixes=(data_sync.REPORTS_PREFIX,),
    )

    assert f"reports/{new_run_date}/{new_run_id}.md" in report.uploaded


def test_windowed_pull_then_append_only_push_still_rejects_an_out_of_window_rewrite(
    tmp_path,
):
    """Windowing excuses absence only, never a rewrite of a present key.

    Regression for the bug where the window skip fired before the local
    entry was even looked up: an out-of-window key that IS present locally
    with different bytes (e.g. written back by a compromised analysis
    session) must still trip the append-only guard, exactly as an in-window
    rewrite does.
    """
    origin = make_workspace(tmp_path, "origin")
    run_ids = _seed_report_run_dates(origin.parent / "reports", _WINDOW_DATES)
    store = FakeObjectStore()
    push(store, origin)

    mirror = tmp_path / "mirror" / "data"
    mirror.mkdir(parents=True)
    data_sync.pull(store, _roots(mirror), reports_window=10)

    out_of_window = _WINDOW_OLDEST_5[0]
    run_id = run_ids[out_of_window]
    result_path = (
        mirror.parent / "reports" / out_of_window / run_id / "analysis_result.json"
    )
    _write(result_path, b"rewritten-by-injection")

    with pytest.raises(data_sync.DataSyncError, match="append-only"):
        data_sync.push(
            store,
            _roots(mirror),
            now=lambda: FIXED_NOW,
            append_only_prefixes=(data_sync.REPORTS_PREFIX,),
        )

    _, result_key = _report_keys_for_date(out_of_window, run_id)
    assert store.objects[result_key] == f"result-{out_of_window}".encode()


def test_full_pull_then_push_still_gcs_normally_after_windowed_pull_landed(tmp_path):
    """A full pull resets the recorded window, so ordinary GC applies again.

    Verifies windowed suppression does not leak into an operator's later
    full-tree pull/push cycle on the same working copy.
    """
    origin = make_workspace(tmp_path, "origin")
    _seed_report_run_dates(origin.parent / "reports", _WINDOW_DATES)
    store = FakeObjectStore()
    push(store, origin)

    mirror = tmp_path / "mirror" / "data"
    mirror.mkdir(parents=True)
    data_sync.pull(store, _roots(mirror), reports_window=10)
    assert data_sync.read_state(mirror).reports_window == 10

    # An operator later does a full pull on the same working copy.
    full_report = data_sync.pull(store, _roots(mirror))
    assert full_report.reports_window is None
    assert data_sync.read_state(mirror).reports_window is None
    for run_date in _WINDOW_OLDEST_5:
        assert (mirror.parent / "reports" / run_date).exists()

    # Remove one out-of-window archive locally, then push: ordinary GC (not
    # suppressed) must delete it from the remote, exactly as before windowing
    # existed.
    victim = _WINDOW_OLDEST_5[0]
    shutil.rmtree(mirror.parent / "reports" / victim)

    push_report = data_sync.push(store, _roots(mirror), now=lambda: FIXED_NOW)

    assert any(key.startswith(f"reports/{victim}/") for key in push_report.deleted)


def test_windowed_pull_when_the_window_exceeds_the_available_run_dates(tmp_path):
    origin = make_workspace(tmp_path, "origin")
    dates = _WINDOW_DATES[:3]  # only 3 run dates exist
    run_ids = _seed_report_run_dates(origin.parent / "reports", dates)
    store = FakeObjectStore()
    push(store, origin)

    mirror = tmp_path / "mirror" / "data"
    mirror.mkdir(parents=True)
    report = data_sync.pull(store, _roots(mirror), reports_window=10)

    downloaded_dates = {
        key.split("/")[1] for key in report.downloaded if key.startswith("reports/")
    }
    assert downloaded_dates == set(dates)
    for run_date in dates:
        assert (
            mirror.parent / "reports" / run_date / f"{run_ids[run_date]}.md"
        ).is_file()


def test_windowed_pull_when_reports_is_still_empty_on_the_remote(tmp_path):
    """Windowing composes with the Issue #370 retention guard.

    The remote has only ever carried `data/`; a windowed pull must behave
    exactly like a full pull did before windowing existed -- retain any local
    `reports/` content rather than delete it, since the manifest's silence
    here means "not adopted yet", not "deleted upstream".
    """
    origin = make_workspace(tmp_path, "origin")
    store = FakeObjectStore()
    push(store, origin)
    assert not any(key.startswith("reports/") for key in read_manifest(store)["files"])

    mirror = make_workspace(tmp_path, "mirror")
    archive = make_reports_workspace(tmp_path, "mirror")

    report = data_sync.pull(store, _roots(mirror), reports_window=10)

    assert report.deleted == ()
    assert report.retained == (REPORT_MD_KEY, REPORT_RESULT_KEY)
    assert (archive / REPORT_RUN_DATE / f"{REPORT_RUN_ID}.md").is_file()


def test_windowed_local_still_only_trips_the_empty_root_guard_when_truly_empty(
    tmp_path,
):
    """`_guard_against_emptying_a_populated_root` needs no windowing special case.

    A windowed local `reports/` tree is non-empty (it holds the window's own
    files), so the guard stays silent -- it only fires once `reports/` is
    truly empty locally, exactly as it did before windowing existed.
    """
    origin = make_workspace(tmp_path, "origin")
    _seed_report_run_dates(origin.parent / "reports", _WINDOW_DATES)
    store = FakeObjectStore()
    push(store, origin)

    mirror = tmp_path / "mirror" / "data"
    mirror.mkdir(parents=True)
    data_sync.pull(store, _roots(mirror), reports_window=10)

    # Non-empty (windowed) local reports/: the guard must not fire.
    push(store, mirror)  # no DataSyncError

    # Now genuinely empty it, and the guard must fire.
    shutil.rmtree(mirror.parent / "reports")
    store.deleted.clear()

    with pytest.raises(data_sync.DataSyncError, match=r"reports/ 配下"):
        push(store, mirror)

    assert store.deleted == []


def test_status_render_lists_every_root_it_was_built_from(tmp_path):
    """`render()` groups by the roots' own prefixes, not a hardcoded pair."""
    workspace = make_workspace(tmp_path)
    make_reports_workspace(tmp_path)
    store = FakeObjectStore()

    rendered = status(store, workspace).render()

    assert rendered.count(f"[{data_sync.DATA_PREFIX}]") == 1
    assert rendered.count(f"[{data_sync.REPORTS_PREFIX}]") == 1
    assert status(store, workspace).prefixes == (
        data_sync.DATA_PREFIX,
        data_sync.REPORTS_PREFIX,
    )


def test_pull_from_an_empty_bucket_reports_that_the_remote_is_empty(tmp_path):
    data_dir = make_workspace(tmp_path)
    store = FakeObjectStore()

    with pytest.raises(data_sync.DataSyncError, match=r"リモートバケットが空"):
        pull(store, data_dir)


def test_a_push_interrupted_before_its_manifest_leaves_the_old_generation_readable(
    tmp_path,
):
    # Only *new* keys were uploaded, so nothing the old manifest references was
    # touched and a reader still sees generation 1 whole.
    origin = make_workspace(tmp_path, "origin")
    store = FakeObjectStore()
    push(store, origin)
    generation_one_manifest = store.objects[data_sync.MANIFEST_KEY]

    _write(origin / "bars" / "year=2026" / "data.parquet", b"bars-2026-v1")
    store.fail_on_write = {data_sync.MANIFEST_KEY}
    with pytest.raises(UploadFailedError):
        push(store, origin)
    store.fail_on_write = set()

    assert store.objects[data_sync.MANIFEST_KEY] == generation_one_manifest
    assert "data/bars/year=2026/data.parquet" in store.objects

    mirror = tmp_path / "mirror" / "data"
    mirror.mkdir(parents=True)
    report = pull(store, mirror)

    assert report.generation == 1
    assert set(report.downloaded) == {DUCKDB_KEY, BARS_2024_KEY, BARS_2025_KEY}
    assert not (mirror / "bars" / "year=2026").exists()
    assert (mirror / "copilot.duckdb").read_bytes() == b"duckdb-v1"


def test_a_push_interrupted_before_its_manifest_fails_loudly_on_an_overwritten_key(
    tmp_path,
):
    # Keys are paths, not content hashes, so re-uploading an existing key does
    # replace bytes the old manifest still describes. That is the case the
    # sha256 verification exists for: loud mismatch, never a silent half-tree.
    origin = make_workspace(tmp_path, "origin")
    store = FakeObjectStore()
    push(store, origin)
    generation_one_manifest = store.objects[data_sync.MANIFEST_KEY]

    (origin / "copilot.duckdb").write_bytes(b"duckdb-v2")
    store.fail_on_write = {data_sync.MANIFEST_KEY}
    with pytest.raises(UploadFailedError):
        push(store, origin)
    store.fail_on_write = set()

    assert store.objects[data_sync.MANIFEST_KEY] == generation_one_manifest
    assert store.objects[DUCKDB_KEY] == b"duckdb-v2"

    mirror = tmp_path / "mirror" / "data"
    mirror.mkdir(parents=True)
    with pytest.raises(data_sync.DataSyncError, match=r"manifest と一致しない"):
        pull(store, mirror)

    assert data_sync.read_state(mirror) is None
    assert not list(mirror.glob("*.tmp"))
    assert not list(mirror.glob(".*.tmp"))


def test_a_push_interrupted_after_its_manifest_leaves_the_new_generation_consistent(
    tmp_path,
):
    origin = make_workspace(tmp_path, "origin")
    store = FakeObjectStore()
    push(store, origin)

    # Second push: the manifest commits, the garbage collection that follows
    # it dies before removing the now-unreferenced object.
    (origin / "bars" / "year=2025" / "data.parquet").unlink()
    store.fail_on_delete = {BARS_2025_KEY}
    with pytest.raises(UploadFailedError):
        push(store, origin)
    store.fail_on_delete = set()

    assert read_manifest(store)["generation"] == 2
    assert BARS_2025_KEY not in read_manifest(store)["files"]
    assert BARS_2025_KEY in store.objects  # leftover garbage

    mirror = tmp_path / "mirror" / "data"
    mirror.mkdir(parents=True)
    report = pull(store, mirror)

    assert report.generation == 2
    assert set(report.downloaded) == {DUCKDB_KEY, BARS_2024_KEY}
    assert not (mirror / "bars" / "year=2025").exists()

    # The interrupted push still committed, so the local state advanced with it
    # and the next push is accepted -- and collects the leftover.
    assert data_sync.read_state(origin).generation == 2
    follow_up = push(store, origin)

    assert follow_up.generation == 3
    assert follow_up.deleted == (BARS_2025_KEY,)
    assert BARS_2025_KEY not in store.objects


def test_pull_deletes_local_files_the_manifest_no_longer_lists(tmp_path):
    origin = make_workspace(tmp_path, "origin")
    store = FakeObjectStore()
    push(store, origin)

    mirror = make_workspace(tmp_path, "mirror")
    pull(store, mirror)

    (origin / "bars" / "year=2025" / "data.parquet").unlink()
    push(store, origin)

    report = pull(store, mirror)

    assert report.deleted == (BARS_2025_KEY,)
    assert not (mirror / "bars" / "year=2025" / "data.parquet").exists()
    assert (mirror / "bars" / "year=2024" / "data.parquet").is_file()
    assert BARS_2025_KEY not in store.objects


def test_pull_retains_a_local_root_the_remote_has_never_published(tmp_path):
    """Issue #370's migration hazard: adding a root must not wipe it locally.

    The bucket has only ever carried `data/`. A checkout that already holds a
    `reports/` archive pulls for the first time after `reports/` becomes a
    synced root -- mirroring would read the manifest's silence as "deleted
    upstream" and destroy the only copy, which is also the copy that would
    have seeded the remote.
    """
    origin = make_workspace(tmp_path, "origin")
    store = FakeObjectStore()
    push(store, origin)
    assert not any(key.startswith("reports/") for key in read_manifest(store)["files"])

    mirror = make_workspace(tmp_path, "mirror")
    archive = make_reports_workspace(tmp_path, "mirror")

    report = pull(store, mirror)

    assert report.deleted == ()
    assert report.retained == (REPORT_MD_KEY, REPORT_RESULT_KEY)
    assert (archive / REPORT_RUN_DATE / f"{REPORT_RUN_ID}.md").is_file()
    assert (
        archive / REPORT_RUN_DATE / REPORT_RUN_ID / "analysis_result.json"
    ).is_file()
    assert "温存=2" in report.render()


def test_pull_mirror_deletes_within_a_root_the_remote_already_publishes(tmp_path):
    """The retention guard is self-limiting: it lifts once the root exists.

    Once one push has published `reports/`, a file genuinely removed upstream
    must be mirrored away locally exactly as a `data/` file is -- otherwise the
    guard would have turned mirroring off for that root permanently.
    """
    origin = make_workspace(tmp_path, "origin")
    make_reports_workspace(tmp_path, "origin")
    store = FakeObjectStore()
    push(store, origin)

    mirror = make_workspace(tmp_path, "mirror")
    mirror_reports = make_reports_workspace(tmp_path, "mirror")
    pull(store, mirror)

    (origin.parent / "reports" / REPORT_RUN_DATE / f"{REPORT_RUN_ID}.md").unlink()
    push(store, origin)

    report = pull(store, mirror)

    assert report.deleted == (REPORT_MD_KEY,)
    assert report.retained == ()
    assert not (mirror_reports / REPORT_RUN_DATE / f"{REPORT_RUN_ID}.md").exists()
    assert (
        mirror_reports / REPORT_RUN_DATE / REPORT_RUN_ID / "analysis_result.json"
    ).is_file()


def test_pull_keeps_the_previous_local_file_when_verification_fails(tmp_path):
    origin = make_workspace(tmp_path, "origin")
    store = FakeObjectStore()
    push(store, origin)

    mirror = tmp_path / "mirror" / "data"
    mirror.mkdir(parents=True)
    destination = mirror / "copilot.duckdb"
    destination.write_bytes(b"previous-local-content")
    # The remote object no longer matches what the manifest recorded.
    store.objects[DUCKDB_KEY] = b"corrupted-remote-bytes"

    with pytest.raises(data_sync.DataSyncError, match=r"manifest と一致しない"):
        pull(store, mirror)

    assert destination.read_bytes() == b"previous-local-content"
    assert sorted(path.name for path in mirror.iterdir() if path.is_file()) == [
        "copilot.duckdb"
    ]


def test_pull_rejects_a_manifest_key_that_escapes_the_data_directory(tmp_path):
    data_dir = make_workspace(tmp_path)
    store = FakeObjectStore()
    store.objects[data_sync.MANIFEST_KEY] = json.dumps(
        {
            "generation": 1,
            "updated_at": FIXED_NOW.isoformat(),
            "files": {"data/../../escaped.duckdb": {"sha256": "0" * 64, "size": 1}},
        }
    ).encode("utf-8")

    with pytest.raises(data_sync.DataSyncError, match=r"不正なオブジェクトキー"):
        pull(store, data_dir)


def test_pull_rejects_a_manifest_key_that_escapes_the_reports_directory(tmp_path):
    data_dir = make_workspace(tmp_path)
    store = FakeObjectStore()
    store.objects[data_sync.MANIFEST_KEY] = json.dumps(
        {
            "generation": 1,
            "updated_at": FIXED_NOW.isoformat(),
            "files": {"reports/../../escaped.md": {"sha256": "0" * 64, "size": 1}},
        }
    ).encode("utf-8")

    with pytest.raises(data_sync.DataSyncError, match=r"不正なオブジェクトキー"):
        pull(store, data_dir)


def test_pull_rejects_a_manifest_key_under_no_known_prefix(tmp_path):
    data_dir = make_workspace(tmp_path)
    store = FakeObjectStore()
    store.objects[data_sync.MANIFEST_KEY] = json.dumps(
        {
            "generation": 1,
            "updated_at": FIXED_NOW.isoformat(),
            "files": {"backups/copilot.duckdb": {"sha256": "0" * 64, "size": 1}},
        }
    ).encode("utf-8")

    with pytest.raises(data_sync.DataSyncError, match=r"同期対象外のオブジェクトキー"):
        pull(store, data_dir)


def test_read_remote_manifest_rejects_an_unparsable_manifest():
    store = FakeObjectStore()
    store.objects[data_sync.MANIFEST_KEY] = b'{"generation": "not-a-number"}'

    with pytest.raises(data_sync.DataSyncError, match=r"manifest.json を解釈できない"):
        data_sync.read_remote_manifest(store)


# --------------------------------------------------------------------------- #
#  push: orphan collection
# --------------------------------------------------------------------------- #


def test_push_deletes_remote_objects_the_local_tree_no_longer_has(tmp_path):
    origin = make_workspace(tmp_path, "origin")
    store = FakeObjectStore()
    push(store, origin)

    (origin / "bars" / "year=2024" / "data.parquet").unlink()
    report = push(store, origin)

    assert report.deleted == (BARS_2024_KEY,)
    assert BARS_2024_KEY not in store.objects
    assert BARS_2024_KEY not in read_manifest(store)["files"]


# --------------------------------------------------------------------------- #
#  status
# --------------------------------------------------------------------------- #


def test_status_reports_an_empty_remote_without_writing_anything(tmp_path):
    data_dir = make_workspace(tmp_path)
    store = FakeObjectStore()

    report = status(store, data_dir)

    assert report.status is data_sync.SyncStatus.REMOTE_EMPTY
    assert report.remote_generation is None
    assert report.local_generation is None
    assert set(report.added) == {DUCKDB_KEY, BARS_2024_KEY, BARS_2025_KEY}
    assert "remote: 空" in report.render()
    assert store.objects == {}
    assert not (data_dir / data_sync.STATE_FILE_NAME).exists()


def test_status_reports_in_sync_after_a_pull(tmp_path):
    origin = make_workspace(tmp_path, "origin")
    store = FakeObjectStore()
    push(store, origin)
    mirror = tmp_path / "mirror" / "data"
    mirror.mkdir(parents=True)
    pull(store, mirror)

    report = status(store, mirror)

    assert report.status is data_sync.SyncStatus.IN_SYNC
    assert report.remote_generation == 1
    assert report.local_generation == 1
    assert report.remote_file_count == 3
    assert report.remote_total_bytes == sum(
        len(body) for body in (b"duckdb-v1", b"bars-2024-v1", b"bars-2025-v1")
    )
    assert (report.added, report.removed, report.modified) == ((), (), ())
    assert "status: in-sync" in report.render()


def test_status_report_breaks_added_files_out_by_root(tmp_path):
    origin = make_workspace(tmp_path, "origin")
    make_reports_workspace(tmp_path, "origin")

    report = status(FakeObjectStore(), origin)
    rendered = report.render()

    assert f"  [{data_sync.DATA_PREFIX}]" in rendered
    assert f"  [{data_sync.REPORTS_PREFIX}]" in rendered
    assert f"  + {DUCKDB_KEY}" in rendered
    assert f"  + {REPORT_MD_KEY}" in rendered


def test_status_reports_local_changed_when_only_the_local_tree_moved(tmp_path):
    origin = make_workspace(tmp_path, "origin")
    store = FakeObjectStore()
    push(store, origin)

    (origin / "copilot.duckdb").write_bytes(b"duckdb-edited")
    report = status(store, origin)

    assert report.status is data_sync.SyncStatus.LOCAL_CHANGED
    assert report.modified == (DUCKDB_KEY,)


def test_status_reports_remote_ahead_when_only_the_remote_moved(tmp_path):
    origin = make_workspace(tmp_path, "origin")
    store = FakeObjectStore()
    push(store, origin)
    mirror = tmp_path / "mirror" / "data"
    mirror.mkdir(parents=True)
    pull(store, mirror)

    (origin / "copilot.duckdb").write_bytes(b"duckdb-v2")
    push(store, origin)
    # The mirror is one generation behind but has no edits of its own.
    (mirror / "copilot.duckdb").write_bytes(b"duckdb-v2")

    report = status(store, mirror)

    assert report.status is data_sync.SyncStatus.REMOTE_AHEAD
    assert (report.remote_generation, report.local_generation) == (2, 1)


def test_status_reports_diverged_when_both_sides_moved(tmp_path):
    origin = make_workspace(tmp_path, "origin")
    store = FakeObjectStore()
    push(store, origin)
    mirror = tmp_path / "mirror" / "data"
    mirror.mkdir(parents=True)
    pull(store, mirror)

    (origin / "copilot.duckdb").write_bytes(b"duckdb-remote-edit")
    push(store, origin)
    (mirror / "copilot.duckdb").write_bytes(b"duckdb-local-edit")

    report = status(store, mirror)

    assert report.status is data_sync.SyncStatus.DIVERGED
    assert report.modified == (DUCKDB_KEY,)
    assert "status: diverged" in report.render()


def test_status_after_windowed_pull_reports_in_sync(tmp_path):
    """A windowed working copy must be able to report in-sync (Issue #373).

    Without window awareness, `removed` would list every out-of-window
    `reports/` key the pull deliberately never fetched -- exactly the keys
    the window is designed to leave alone -- so `status` could never say
    `in-sync` on any machine that has ever used `--reports-window`.
    """
    origin = make_workspace(tmp_path, "origin")
    _seed_report_run_dates(origin.parent / "reports", _WINDOW_DATES)
    store = FakeObjectStore()
    push(store, origin)

    mirror = tmp_path / "mirror" / "data"
    mirror.mkdir(parents=True)
    data_sync.pull(store, _roots(mirror), reports_window=10)

    report = status(store, mirror)

    assert report.status is data_sync.SyncStatus.IN_SYNC
    assert report.removed == ()


def test_status_after_windowed_pull_still_flags_a_missing_in_window_key(tmp_path):
    """Windowing excuses absence only outside the window, not inside it."""
    origin = make_workspace(tmp_path, "origin")
    run_ids = _seed_report_run_dates(origin.parent / "reports", _WINDOW_DATES)
    store = FakeObjectStore()
    push(store, origin)

    mirror = tmp_path / "mirror" / "data"
    mirror.mkdir(parents=True)
    data_sync.pull(store, _roots(mirror), reports_window=10)

    in_window = _WINDOW_NEWEST_10[0]
    run_id = run_ids[in_window]
    (mirror.parent / "reports" / in_window / run_id / "analysis_result.json").unlink()

    report = status(store, mirror)

    _, result_key = _report_keys_for_date(in_window, run_id)
    assert result_key in report.removed
    assert report.status is data_sync.SyncStatus.LOCAL_CHANGED


def test_status_reports_no_local_state_before_the_first_pull(tmp_path):
    origin = make_workspace(tmp_path, "origin")
    store = FakeObjectStore()
    push(store, origin)
    fresh = make_workspace(tmp_path, "fresh")

    report = status(store, fresh)

    assert report.status is data_sync.SyncStatus.NO_LOCAL_STATE
    assert report.local_generation is None


# --------------------------------------------------------------------------- #
#  Credentials and CLI
# --------------------------------------------------------------------------- #


def test_require_names_every_missing_credential_variable(monkeypatch):
    for name in ("R2_ACCOUNT_ID", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY"):
        monkeypatch.delenv(name, raising=False)
    settings = data_sync.R2Settings(_env_file=None)

    with pytest.raises(data_sync.DataSyncError) as failure:
        settings.require()

    message = str(failure.value)
    for name in ("R2_ACCOUNT_ID", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY"):
        assert name in message


def test_blank_credential_values_count_as_unset(monkeypatch):
    monkeypatch.setenv("R2_ACCOUNT_ID", "   ")
    monkeypatch.setenv("R2_ACCESS_KEY_ID", "key")
    monkeypatch.setenv("R2_SECRET_ACCESS_KEY", "secret")
    settings = data_sync.R2Settings(_env_file=None)

    with pytest.raises(data_sync.DataSyncError, match=r"R2_ACCOUNT_ID"):
        settings.require()


def test_credentials_do_not_leak_into_repr_or_str(monkeypatch):
    monkeypatch.setenv("R2_ACCOUNT_ID", "account-secret")
    monkeypatch.setenv("R2_ACCESS_KEY_ID", "key-secret")
    monkeypatch.setenv("R2_SECRET_ACCESS_KEY", "token-secret")
    credentials = data_sync.R2Settings(_env_file=None).require()

    rendered = f"{credentials!r} {credentials}"

    assert "account-secret" not in rendered
    assert "key-secret" not in rendered
    assert "token-secret" not in rendered
    assert credentials.endpoint_url == (
        "https://account-secret.r2.cloudflarestorage.com"
    )


def test_bare_r2settings_ignores_the_repo_dotenv_file():
    """Regression test for the autouse `.env` guard in `tests/conftest.py` (Issue #387).

    `R2Settings.model_config["env_file"]` is `REPO_ROOT / ".env"` -- an
    absolute path, so `monkeypatch.chdir` cannot dodge it the way it can for
    the repo-relative default used elsewhere. Without the guard's
    `env_file=None` patch, a bare `R2Settings()` here would read the
    operator's real R2 write credentials from the repository's `.env`.
    """
    settings = data_sync.R2Settings()

    assert settings.r2_account_id is None
    assert settings.r2_access_key_id is None
    assert settings.r2_secret_access_key is None


def test_r2settings_explicit_env_file_still_works(tmp_path):
    """The guard disables only the class-level default `env_file`.

    An explicit `_env_file=` at construction time overrides that default, so
    a test that genuinely wants to exercise `.env` parsing still can --
    against an isolated `tmp_path` file, never the operator's real one.
    """
    env_path = tmp_path / "r2.env"
    env_path.write_text(
        "R2_ACCOUNT_ID=explicit-account\n"
        "R2_ACCESS_KEY_ID=explicit-key\n"
        "R2_SECRET_ACCESS_KEY=explicit-secret\n"
    )

    settings = data_sync.R2Settings(_env_file=env_path)

    assert settings.r2_account_id is not None
    assert settings.r2_account_id.get_secret_value() == "explicit-account"


def test_run_prints_the_status_report(tmp_path, capsys):
    data_dir = make_workspace(tmp_path)
    store = FakeObjectStore()

    assert data_sync.run("status", store, _roots(data_dir)) == 0

    assert "status: remote-empty" in capsys.readouterr().out


def test_importing_the_module_does_not_require_the_optional_boto3_group(monkeypatch):
    # CI installs only the `dev` group, so importing this module -- which the
    # whole test file does -- must not reach for boto3.
    monkeypatch.setitem(sys.modules, "boto3", None)
    monkeypatch.setitem(sys.modules, "botocore", None)
    # `load_script_module` is memoized on `sys.modules["data_sync"]`; drop the
    # entry so this actually re-executes the module with boto3 blocked,
    # instead of just handing back the module already loaded at collection.
    monkeypatch.delitem(sys.modules, "data_sync", raising=False)

    reloaded = load_script_module("data_sync", "scripts/data_sync.py")

    assert reloaded.BUCKET_NAME == data_sync.BUCKET_NAME


def test_cli_requires_a_subcommand():
    with pytest.raises(SystemExit) as failure:
        data_sync.main([])

    assert failure.value.code == 2
