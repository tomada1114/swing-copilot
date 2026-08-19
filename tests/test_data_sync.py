"""Tests for scripts/data_sync.py (offline: the object store is a fake)."""

from __future__ import annotations

import importlib.util
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

if TYPE_CHECKING:
    from types import ModuleType

REPO_ROOT = Path(__file__).resolve().parents[1]


def _load_data_sync() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "data_sync", REPO_ROOT / "scripts" / "data_sync.py"
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # `dataclasses` resolves annotations through `sys.modules[cls.__module__]`,
    # so a spec-loaded module has to be registered before it executes.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


data_sync = _load_data_sync()

FIXED_NOW = datetime(2026, 8, 19, 23, 0, tzinfo=UTC)
DUCKDB_KEY = "data/copilot.duckdb"
BARS_2024_KEY = "data/bars/year=2024/data.parquet"
BARS_2025_KEY = "data/bars/year=2025/data.parquet"


class UploadFailedError(RuntimeError):
    """Injected transport failure, distinct from any DataSyncError."""


class FakeObjectStore:
    """In-memory stand-in for the S3-compatible subset `data_sync` uses."""

    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.uploaded: list[str] = []
        self.written: list[str] = []
        self.deleted: list[str] = []
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


# `data_sync` is spec-loaded, so everything it returns is dynamically typed.
def push(store: FakeObjectStore, data_dir: Path) -> Any:
    return data_sync.push(store, data_dir, now=lambda: FIXED_NOW)


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
    pull_report = data_sync.pull(store, mirror)

    assert pull_report.generation == 1
    assert set(pull_report.downloaded) == {DUCKDB_KEY, BARS_2024_KEY, BARS_2025_KEY}
    assert (mirror / "copilot.duckdb").read_bytes() == b"duckdb-v1"
    assert data_sync.status(store, mirror).status is data_sync.SyncStatus.IN_SYNC

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
    report = data_sync.pull(store, mirror)

    assert report.downloaded == ()
    assert set(report.skipped) == {DUCKDB_KEY, BARS_2024_KEY, BARS_2025_KEY}


# --------------------------------------------------------------------------- #
#  push: the optimistic lock
# --------------------------------------------------------------------------- #


def test_push_rejects_when_remote_generation_moved_ahead_and_uploads_nothing(tmp_path):
    origin = make_workspace(tmp_path, "origin")
    store = FakeObjectStore()
    push(store, origin)

    stale = tmp_path / "stale" / "data"
    stale.mkdir(parents=True)
    data_sync.pull(store, stale)

    # Somebody else pulls, works, and pushes generation 2 in the meantime.
    other = tmp_path / "other" / "data"
    other.mkdir(parents=True)
    data_sync.pull(store, other)
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
    data_sync.pull(store, empty)
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

    pull_report = data_sync.pull(store, data_dir)

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


# --------------------------------------------------------------------------- #
#  pull: verification, mirroring, atomic replacement
# --------------------------------------------------------------------------- #


def test_pull_from_an_empty_bucket_reports_that_the_remote_is_empty(tmp_path):
    data_dir = make_workspace(tmp_path)
    store = FakeObjectStore()

    with pytest.raises(data_sync.DataSyncError, match=r"リモートバケットが空"):
        data_sync.pull(store, data_dir)


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
    report = data_sync.pull(store, mirror)

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
        data_sync.pull(store, mirror)

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
    report = data_sync.pull(store, mirror)

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
    data_sync.pull(store, mirror)

    (origin / "bars" / "year=2025" / "data.parquet").unlink()
    push(store, origin)

    report = data_sync.pull(store, mirror)

    assert report.deleted == (BARS_2025_KEY,)
    assert not (mirror / "bars" / "year=2025" / "data.parquet").exists()
    assert (mirror / "bars" / "year=2024" / "data.parquet").is_file()
    assert BARS_2025_KEY not in store.objects


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
        data_sync.pull(store, mirror)

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
        data_sync.pull(store, data_dir)


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

    report = data_sync.status(store, data_dir)

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
    data_sync.pull(store, mirror)

    report = data_sync.status(store, mirror)

    assert report.status is data_sync.SyncStatus.IN_SYNC
    assert report.remote_generation == 1
    assert report.local_generation == 1
    assert report.remote_file_count == 3
    assert report.remote_total_bytes == sum(
        len(body) for body in (b"duckdb-v1", b"bars-2024-v1", b"bars-2025-v1")
    )
    assert (report.added, report.removed, report.modified) == ((), (), ())
    assert "status: in-sync" in report.render()


def test_status_reports_local_changed_when_only_the_local_tree_moved(tmp_path):
    origin = make_workspace(tmp_path, "origin")
    store = FakeObjectStore()
    push(store, origin)

    (origin / "copilot.duckdb").write_bytes(b"duckdb-edited")
    report = data_sync.status(store, origin)

    assert report.status is data_sync.SyncStatus.LOCAL_CHANGED
    assert report.modified == (DUCKDB_KEY,)


def test_status_reports_remote_ahead_when_only_the_remote_moved(tmp_path):
    origin = make_workspace(tmp_path, "origin")
    store = FakeObjectStore()
    push(store, origin)
    mirror = tmp_path / "mirror" / "data"
    mirror.mkdir(parents=True)
    data_sync.pull(store, mirror)

    (origin / "copilot.duckdb").write_bytes(b"duckdb-v2")
    push(store, origin)
    # The mirror is one generation behind but has no edits of its own.
    (mirror / "copilot.duckdb").write_bytes(b"duckdb-v2")

    report = data_sync.status(store, mirror)

    assert report.status is data_sync.SyncStatus.REMOTE_AHEAD
    assert (report.remote_generation, report.local_generation) == (2, 1)


def test_status_reports_diverged_when_both_sides_moved(tmp_path):
    origin = make_workspace(tmp_path, "origin")
    store = FakeObjectStore()
    push(store, origin)
    mirror = tmp_path / "mirror" / "data"
    mirror.mkdir(parents=True)
    data_sync.pull(store, mirror)

    (origin / "copilot.duckdb").write_bytes(b"duckdb-remote-edit")
    push(store, origin)
    (mirror / "copilot.duckdb").write_bytes(b"duckdb-local-edit")

    report = data_sync.status(store, mirror)

    assert report.status is data_sync.SyncStatus.DIVERGED
    assert report.modified == (DUCKDB_KEY,)
    assert "status: diverged" in report.render()


def test_status_reports_no_local_state_before_the_first_pull(tmp_path):
    origin = make_workspace(tmp_path, "origin")
    store = FakeObjectStore()
    push(store, origin)
    fresh = make_workspace(tmp_path, "fresh")

    report = data_sync.status(store, fresh)

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


def test_run_prints_the_status_report(tmp_path, capsys):
    data_dir = make_workspace(tmp_path)
    store = FakeObjectStore()

    assert data_sync.run("status", store, data_dir) == 0

    assert "status: remote-empty" in capsys.readouterr().out


def test_importing_the_module_does_not_require_the_optional_boto3_group(monkeypatch):
    # CI installs only the `dev` group, so importing this module -- which the
    # whole test file does -- must not reach for boto3.
    monkeypatch.setitem(sys.modules, "boto3", None)
    monkeypatch.setitem(sys.modules, "botocore", None)

    reloaded = _load_data_sync()

    assert reloaded.BUCKET_NAME == data_sync.BUCKET_NAME


def test_cli_requires_a_subcommand():
    with pytest.raises(SystemExit) as failure:
        data_sync.main([])

    assert failure.value.code == 2
