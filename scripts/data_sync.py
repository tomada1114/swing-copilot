"""Mirror the operator-owned `data/` tree to and from the Cloudflare R2 bucket.

The R2 bucket is the source of truth for `data/copilot.duckdb` and the
`data/bars/year=YYYY/*.parquet` Hive tree. Both this machine and the GitHub
Actions runner use the same three verbs: `pull` before working, `push` after.
The pipeline itself is untouched -- this script only moves bytes.

Two invariants make that safe:

**`manifest.json` is the commit point.** A `push` writes changed data objects
first, deletes remote objects the local tree no longer has, and writes
`manifest.json` last. Every S3-compatible PUT lands as one whole object, so a
crash before the manifest write leaves the previous generation's manifest in
place; a reader then finds an object whose bytes disagree with the sha256 the
manifest recorded and fails loudly instead of loading a half-written mix.
`pull` therefore verifies every downloaded object against the manifest.

The one window this leaves open: a crash *after* the deletion step but before
the manifest write leaves the old manifest referencing objects that are gone.
`pull` fails loudly there too (the object is missing), and the fix is to re-run
`push` from the machine that still holds the full local tree.

**A generation counter is the only concurrency guard.** `pull` records the
manifest's `generation` in `data/.r2_sync_state.json`; `push` refuses unless
the remote generation still equals that recorded value, then writes
`generation + 1`. So a write from elsewhere is detected rather than silently
overwritten -- which is why a writing session must be one unbroken
pull -> work -> push, never left open overnight.

`data/copilot.duckdb` is treated as an opaque binary here and is never opened
as a database: DuckDB's file lock is exclusive, and taking it could fail a
concurrent run.
"""

from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
import os
import shutil
import sys
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Protocol

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    SecretStr,
    ValidationError,
    field_validator,
)
from pydantic_settings import BaseSettings, SettingsConfigDict

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator, Mapping

    from mypy_boto3_s3.client import S3Client

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATA_DIR = REPO_ROOT / "data"

BUCKET_NAME = "swing-copilot-data-duckdb"
MANIFEST_KEY = "manifest.json"
DATA_PREFIX = "data/"
STATE_FILE_NAME = ".r2_sync_state.json"
BARS_DIR_NAME = "bars"

# Local-only artifacts. Excluded from both directions: never uploaded, and
# never removed by `pull`'s mirror deletion.
EXCLUDED_FILE_NAMES = frozenset({"copilot_dry_run.duckdb"})
EXCLUDED_COMPONENT_GLOB = "*.bak-*"

_HASH_CHUNK_BYTES = 1 << 20
_MISSING_OBJECT_CODES = frozenset({"NoSuchKey", "NotFound", "404"})
_CREDENTIAL_FIELDS = ("r2_account_id", "r2_access_key_id", "r2_secret_access_key")


class DataSyncError(RuntimeError):
    """A sync failure that should stop the command with a readable message."""


class ConcurrentWriteError(DataSyncError):
    """The remote moved on since the local generation was recorded."""


# --------------------------------------------------------------------------- #
#  Credentials
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class R2Credentials:
    """Resolved R2 connection secrets."""

    account_id: SecretStr
    access_key_id: SecretStr
    secret_access_key: SecretStr

    @property
    def endpoint_url(self) -> str:
        """S3-compatible endpoint for this account.

        Carries the account id, so it is passed to boto3 and never printed.
        """
        return f"https://{self.account_id.get_secret_value()}.r2.cloudflarestorage.com"


class R2Settings(BaseSettings):
    """R2 credentials from the environment or the repository-root `.env`.

    `extra="ignore"` so the shared `.env` can keep the pipeline's own keys.
    """

    model_config = SettingsConfigDict(env_file=REPO_ROOT / ".env", extra="ignore")

    r2_account_id: SecretStr | None = None
    r2_access_key_id: SecretStr | None = None
    r2_secret_access_key: SecretStr | None = None

    @field_validator(*_CREDENTIAL_FIELDS, mode="before")
    @classmethod
    def _blank_means_unset(cls, value: object) -> object:
        """Treat a declared-but-empty `.env` entry (`KEY=`) as unset."""
        if isinstance(value, str) and not value.strip():
            return None
        return value

    def require(self) -> R2Credentials:
        """Return the credentials, naming any that are missing.

        Raises:
            DataSyncError: When one or more variables are unset or blank. Only
                the variable *names* appear in the message.
        """
        present: dict[str, SecretStr] = {}
        missing: list[str] = []
        for name in _CREDENTIAL_FIELDS:
            value = getattr(self, name)
            if value is None:
                missing.append(name.upper())
            else:
                present[name] = value
        if missing:
            msg = (
                f"R2 の接続情報が未設定: {', '.join(missing)}。"
                "環境変数かリポジトリルートの .env に設定する"
            )
            raise DataSyncError(msg)
        return R2Credentials(
            account_id=present["r2_account_id"],
            access_key_id=present["r2_access_key_id"],
            secret_access_key=present["r2_secret_access_key"],
        )


# --------------------------------------------------------------------------- #
#  Manifest and local sync state
# --------------------------------------------------------------------------- #


class FileEntry(BaseModel):
    """One synced object's content fingerprint."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    sha256: str
    size: int


class Manifest(BaseModel):
    """The bucket's commit record, written last by every `push`."""

    model_config = ConfigDict(extra="forbid")

    generation: int
    updated_at: AwareDatetime
    files: dict[str, FileEntry]

    @property
    def total_bytes(self) -> int:
        """Summed size of every object this generation references."""
        return sum(entry.size for entry in self.files.values())


class SyncState(BaseModel):
    """The generation this working copy last pulled."""

    model_config = ConfigDict(extra="forbid")

    generation: int


# --------------------------------------------------------------------------- #
#  Object store port
# --------------------------------------------------------------------------- #


class ObjectStore(Protocol):
    """The S3-compatible subset this script needs, as an injectable port."""

    def read_bytes(self, key: str) -> bytes | None:
        """Return the object body, or `None` when the key does not exist."""
        ...

    def write_bytes(self, key: str, body: bytes) -> None:
        """Store `body` as one whole object."""
        ...

    def upload(self, key: str, source: Path) -> None:
        """Store the file at `source` as one whole object."""
        ...

    def download(self, key: str, destination: Path) -> None:
        """Write the object's body to `destination`, raising when absent."""
        ...

    def delete(self, key: str) -> None:
        """Remove the object; absence is not an error."""
        ...

    def list_keys(self, prefix: str) -> list[str]:
        """Return every existing key under `prefix`."""
        ...


class R2ObjectStore:
    """`ObjectStore` backed by boto3 against one Cloudflare R2 bucket."""

    def __init__(self, client: S3Client, bucket: str) -> None:
        self._client = client
        self._bucket = bucket

    def read_bytes(self, key: str) -> bytes | None:
        """Return the object body, or `None` when the key does not exist."""
        # Imported lazily, like boto3 itself: the `ops` dependency group is
        # optional, and this module must stay importable without it so the
        # offline test suite can drive it through an injected fake store.
        from botocore.exceptions import ClientError  # noqa: PLC0415

        try:
            response = self._client.get_object(Bucket=self._bucket, Key=key)
        except ClientError as error:
            if error.response.get("Error", {}).get("Code") in _MISSING_OBJECT_CODES:
                return None
            raise
        body: bytes = response["Body"].read()
        return body

    def write_bytes(self, key: str, body: bytes) -> None:
        """Store `body` as one whole object."""
        self._client.put_object(Bucket=self._bucket, Key=key, Body=body)

    def upload(self, key: str, source: Path) -> None:
        """Store the file as a single PUT rather than a multipart upload.

        A single PUT keeps the "one object appears atomically" guarantee the
        manifest scheme relies on, and leaves no orphaned parts behind when a
        transfer dies mid-flight.
        """
        with source.open("rb") as handle:
            self._client.put_object(Bucket=self._bucket, Key=key, Body=handle)

    def download(self, key: str, destination: Path) -> None:
        """Stream the object's body to `destination`."""
        response = self._client.get_object(Bucket=self._bucket, Key=key)
        with destination.open("wb") as handle:
            shutil.copyfileobj(response["Body"], handle)

    def delete(self, key: str) -> None:
        """Remove the object; absence is not an error."""
        self._client.delete_object(Bucket=self._bucket, Key=key)

    def list_keys(self, prefix: str) -> list[str]:
        """Return every existing key under `prefix`."""
        paginator = self._client.get_paginator("list_objects_v2")
        return [
            item["Key"]
            for page in paginator.paginate(Bucket=self._bucket, Prefix=prefix)
            for item in page.get("Contents", [])
            if "Key" in item
        ]


def build_object_store(credentials: R2Credentials) -> R2ObjectStore:
    """Build the boto3-backed store for `BUCKET_NAME` (the `ops` group).

    boto3 is imported here rather than at module scope so the module stays
    importable — and testable against an injected fake — without the optional
    `ops` dependency group installed.
    """
    import boto3  # noqa: PLC0415
    from botocore.config import Config  # noqa: PLC0415

    client = boto3.client(
        "s3",
        endpoint_url=credentials.endpoint_url,
        aws_access_key_id=credentials.access_key_id.get_secret_value(),
        aws_secret_access_key=credentials.secret_access_key.get_secret_value(),
        region_name="auto",
        config=Config(
            signature_version="s3v4",
            retries={"max_attempts": 5, "mode": "standard"},
            connect_timeout=30,
            read_timeout=300,
        ),
    )
    return R2ObjectStore(client, BUCKET_NAME)


# --------------------------------------------------------------------------- #
#  Local tree
# --------------------------------------------------------------------------- #


def _is_excluded(relative: PurePosixPath) -> bool:
    """Whether a `data/`-relative path is local-only and must never sync.

    Checked per path component, so a backed-up *directory*
    (`bars/year=2024.bak-20260811/data.parquet`) is skipped just like a
    backed-up file. Hidden entries are excluded too, which covers this
    script's own state file and the temporary files `pull` writes.
    """
    return any(
        component.startswith(".")
        or component in EXCLUDED_FILE_NAMES
        or fnmatch.fnmatch(component, EXCLUDED_COMPONENT_GLOB)
        for component in relative.parts
    )


def _has_sync_shape(relative: PurePosixPath) -> bool:
    """Whether a `data/`-relative path is one of the two synced artifacts."""
    parts = relative.parts
    if len(parts) == 1:
        return relative.suffix == ".duckdb"
    return parts[0] == BARS_DIR_NAME and relative.suffix == ".parquet"


def iter_sync_paths(data_dir: Path) -> Iterator[Path]:
    """Yield every local file belonging to the synced set, in key order."""
    if not data_dir.is_dir():
        return
    for path in sorted(data_dir.rglob("*")):
        if not path.is_file():
            continue
        relative = PurePosixPath(path.relative_to(data_dir).as_posix())
        if _is_excluded(relative) or not _has_sync_shape(relative):
            continue
        yield path


def _object_key(data_dir: Path, path: Path) -> str:
    """Object key for a local path: the repo-relative path, verbatim."""
    return f"{DATA_PREFIX}{path.relative_to(data_dir).as_posix()}"


def _local_path(data_dir: Path, key: str) -> Path:
    """Resolve an object key back to a local path inside `data_dir`.

    Raises:
        DataSyncError: When the key is outside `data/`, or escapes `data_dir`
            through `..` or an absolute component. A manifest is remote input,
            so it is treated as untrusted for path purposes.
    """
    if not key.startswith(DATA_PREFIX):
        msg = f"同期対象外のオブジェクトキー: {key!r}"
        raise DataSyncError(msg)
    relative = PurePosixPath(key[len(DATA_PREFIX) :])
    if relative.is_absolute() or not relative.parts or ".." in relative.parts:
        msg = f"不正なオブジェクトキー: {key!r}"
        raise DataSyncError(msg)
    candidate = data_dir / Path(*relative.parts)
    if data_dir.resolve() not in candidate.resolve().parents:
        msg = f"data/ の外を指すオブジェクトキー: {key!r}"
        raise DataSyncError(msg)
    return candidate


def _sha256(path: Path) -> str:
    """Hash a file in chunks. Never opens `copilot.duckdb` as a database."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(_HASH_CHUNK_BYTES):
            digest.update(chunk)
    return digest.hexdigest()


def scan_local(data_dir: Path) -> dict[str, FileEntry]:
    """Fingerprint every local file in the synced set, keyed by object key."""
    return {
        _object_key(data_dir, path): FileEntry(
            sha256=_sha256(path), size=path.stat().st_size
        )
        for path in iter_sync_paths(data_dir)
    }


# --------------------------------------------------------------------------- #
#  Manifest / state I/O
# --------------------------------------------------------------------------- #


def read_remote_manifest(store: ObjectStore) -> Manifest | None:
    """Read the bucket's manifest, or `None` when the bucket is empty.

    Raises:
        DataSyncError: When the manifest exists but does not parse.
    """
    raw = store.read_bytes(MANIFEST_KEY)
    if raw is None:
        return None
    try:
        return Manifest.model_validate_json(raw)
    except ValidationError as error:
        msg = f"{MANIFEST_KEY} を解釈できない: {error}"
        raise DataSyncError(msg) from error


def _write_remote_manifest(store: ObjectStore, manifest: Manifest) -> None:
    """Write the manifest -- the commit point of a `push`."""
    body = json.dumps(
        manifest.model_dump(mode="json"), indent=2, sort_keys=True
    ).encode("utf-8")
    store.write_bytes(MANIFEST_KEY, body)


def read_state(data_dir: Path) -> SyncState | None:
    """Read the recorded generation, or `None` when this copy never pulled.

    Raises:
        DataSyncError: When the state file exists but does not parse.
    """
    path = data_dir / STATE_FILE_NAME
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return None
    try:
        return SyncState.model_validate_json(raw)
    except ValidationError as error:
        msg = f"{path} を解釈できない: {error}"
        raise DataSyncError(msg) from error


def write_state(data_dir: Path, generation: int) -> None:
    """Record the generation this working copy now holds."""
    data_dir.mkdir(parents=True, exist_ok=True)
    body = SyncState(generation=generation).model_dump_json(indent=2)
    _replace_atomically(data_dir / STATE_FILE_NAME, body.encode("utf-8"))


def _replace_atomically(destination: Path, body: bytes) -> None:
    """Write `body` through a temporary file in the destination directory."""
    descriptor, name = _make_temporary(destination)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(body)
        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)


def _make_temporary(destination: Path) -> tuple[int, str]:
    """Open a hidden temporary file beside `destination`, on the same device.

    Hidden (leading dot) so a leftover is skipped by `_is_excluded` rather
    than mistaken for a synced artifact, and beside the destination so that
    `Path.replace` is a same-filesystem atomic rename.
    """
    return tempfile.mkstemp(
        dir=destination.parent, prefix=f".{destination.name}.", suffix=".tmp"
    )


# --------------------------------------------------------------------------- #
#  pull
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class PullReport:
    """What one `pull` did."""

    generation: int
    downloaded: tuple[str, ...]
    skipped: tuple[str, ...]
    deleted: tuple[str, ...]

    def render(self) -> str:
        """Human-readable summary."""
        return (
            f"pull 完了: generation={self.generation} "
            f"取得={len(self.downloaded)} 既存流用={len(self.skipped)} "
            f"削除={len(self.deleted)}"
        )


def _download_verified(
    store: ObjectStore, key: str, destination: Path, entry: FileEntry
) -> None:
    """Download one object and only then let it replace `destination`.

    The body lands in a temporary file inside the destination directory, is
    checked against the manifest's size and sha256, and is moved into place
    with `Path.replace`. A mismatch -- the signature of a `push` that died
    before writing its manifest -- raises, leaving the previous local file
    untouched and no temporary file behind.

    Raises:
        DataSyncError: When the downloaded bytes disagree with the manifest.
    """
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = _make_temporary(destination)
    os.close(descriptor)
    temporary = Path(name)
    try:
        store.download(key, temporary)
        size = temporary.stat().st_size
        digest = _sha256(temporary)
        if size != entry.size or digest != entry.sha256:
            msg = (
                f"{key} の内容が manifest と一致しない "
                f"(期待 sha256={entry.sha256} size={entry.size} / "
                f"実際 sha256={digest} size={size})。"
                "push が manifest 書き込み前に失敗した可能性がある。"
                "ローカル正本を持つ環境から push をやり直す"
            )
            raise DataSyncError(msg)
        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)


def pull(store: ObjectStore, data_dir: Path) -> PullReport:
    """Make the local `data/` tree match the remote manifest exactly.

    Files whose sha256 already matches are left alone; everything else is
    downloaded and verified. Local files in the synced set that the manifest
    no longer lists are deleted, so the local tree mirrors the remote instead
    of accumulating.

    Raises:
        DataSyncError: When the bucket has no manifest, or a downloaded object
            fails verification.
    """
    manifest = read_remote_manifest(store)
    if manifest is None:
        msg = (
            f"リモートバケットが空 ({MANIFEST_KEY} がない)。"
            "初回のデータ投入は正本を持つ環境から push する"
        )
        raise DataSyncError(msg)

    local = scan_local(data_dir)
    downloaded: list[str] = []
    skipped: list[str] = []
    for key, entry in sorted(manifest.files.items()):
        current = local.get(key)
        if current is not None and current.sha256 == entry.sha256:
            skipped.append(key)
            continue
        _download_verified(store, key, _local_path(data_dir, key), entry)
        downloaded.append(key)

    deleted = [key for key in sorted(local) if key not in manifest.files]
    for key in deleted:
        _local_path(data_dir, key).unlink()

    write_state(data_dir, manifest.generation)
    return PullReport(
        generation=manifest.generation,
        downloaded=tuple(downloaded),
        skipped=tuple(skipped),
        deleted=tuple(deleted),
    )


# --------------------------------------------------------------------------- #
#  push
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class PushReport:
    """What one `push` did."""

    generation: int
    uploaded: tuple[str, ...]
    unchanged: tuple[str, ...]
    deleted: tuple[str, ...]

    def render(self) -> str:
        """Human-readable summary."""
        return (
            f"push 完了: generation={self.generation} "
            f"送信={len(self.uploaded)} 変更なし={len(self.unchanged)} "
            f"削除={len(self.deleted)}"
        )


def _next_generation(remote: Manifest | None, state: SyncState | None) -> int:
    """Resolve the generation to write, enforcing the optimistic lock.

    An empty bucket accepts a first `push` at generation 1 regardless of local
    state: with no manifest there is nothing to conflict with.

    Raises:
        DataSyncError: When this working copy never pulled.
        ConcurrentWriteError: When the remote advanced since the recorded pull.
    """
    if remote is None:
        return 1
    if state is None:
        msg = (
            f"ローカルに同期状態 ({STATE_FILE_NAME}) がないのに"
            f"リモートは generation={remote.generation}。"
            "上書き事故を避けるため先に pull する"
        )
        raise DataSyncError(msg)
    if state.generation != remote.generation:
        msg = (
            f"リモートが別の場所で書き換えられている "
            f"(ローカル控え generation={state.generation} / "
            f"リモート generation={remote.generation})。"
            "何も送信していない。pull し直して作業をやり直す"
        )
        raise ConcurrentWriteError(msg)
    return remote.generation + 1


def push(
    store: ObjectStore,
    data_dir: Path,
    *,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> PushReport:
    """Publish the local `data/` tree as the next remote generation.

    Order matters: changed objects go up first, remote objects the local tree
    no longer has are removed second, and `manifest.json` is written last, so
    the manifest write is the commit point (see the module docstring).

    Raises:
        DataSyncError: When the optimistic-lock precondition is unmet, or the
            local tree holds nothing to sync.
        ConcurrentWriteError: When the remote advanced since the recorded pull.
    """
    remote = read_remote_manifest(store)
    generation = _next_generation(remote, read_state(data_dir))

    local = scan_local(data_dir)
    if not local:
        msg = (
            f"{data_dir} に同期対象のファイルが 1 件もない。"
            "リモート正本を空にしないため push を中止する"
        )
        raise DataSyncError(msg)

    remote_files: Mapping[str, FileEntry] = remote.files if remote else {}
    uploaded: list[str] = []
    unchanged: list[str] = []
    for key, entry in sorted(local.items()):
        previous = remote_files.get(key)
        if previous is not None and previous.sha256 == entry.sha256:
            unchanged.append(key)
            continue
        store.upload(key, _local_path(data_dir, key))
        uploaded.append(key)

    # Listed live rather than read from the manifest, so objects orphaned by an
    # earlier interrupted push are collected too.
    deleted = [key for key in sorted(store.list_keys(DATA_PREFIX)) if key not in local]
    for key in deleted:
        store.delete(key)

    _write_remote_manifest(
        store, Manifest(generation=generation, updated_at=now(), files=local)
    )
    write_state(data_dir, generation)
    return PushReport(
        generation=generation,
        uploaded=tuple(uploaded),
        unchanged=tuple(unchanged),
        deleted=tuple(deleted),
    )


# --------------------------------------------------------------------------- #
#  status
# --------------------------------------------------------------------------- #


class SyncStatus(Enum):
    """How the local tree relates to the remote generation."""

    REMOTE_EMPTY = "remote-empty"
    NO_LOCAL_STATE = "no-local-state"
    IN_SYNC = "in-sync"
    LOCAL_CHANGED = "local-changed"
    REMOTE_AHEAD = "remote-ahead"
    DIVERGED = "diverged"


@dataclass(frozen=True, slots=True)
class StatusReport:
    """A read-only comparison of the local tree against the remote manifest."""

    status: SyncStatus
    remote_generation: int | None
    remote_updated_at: datetime | None
    remote_file_count: int
    remote_total_bytes: int
    local_generation: int | None
    added: tuple[str, ...]
    removed: tuple[str, ...]
    modified: tuple[str, ...]

    def render(self) -> str:
        """Human-readable summary."""
        if self.remote_generation is None:
            remote = f"remote: 空 ({MANIFEST_KEY} なし)"
        else:
            updated = (
                self.remote_updated_at.isoformat() if self.remote_updated_at else "-"
            )
            remote = (
                f"remote: generation={self.remote_generation} "
                f"updated_at={updated} "
                f"files={self.remote_file_count} "
                f"bytes={self.remote_total_bytes}"
            )
        local_generation = (
            "未 pull" if self.local_generation is None else str(self.local_generation)
        )
        lines = [
            f"status: {self.status.value}",
            remote,
            f"local:  generation={local_generation} "
            f"追加={len(self.added)} 欠落={len(self.removed)} "
            f"変更={len(self.modified)}",
        ]
        lines.extend(f"  + {key}" for key in self.added)
        lines.extend(f"  - {key}" for key in self.removed)
        lines.extend(f"  M {key}" for key in self.modified)
        return "\n".join(lines)


def _classify(
    manifest: Manifest, state: SyncState | None, *, has_local_changes: bool
) -> SyncStatus:
    """Map (generation comparison, local diff) onto a status."""
    if state is None:
        return SyncStatus.NO_LOCAL_STATE
    if state.generation == manifest.generation:
        return SyncStatus.LOCAL_CHANGED if has_local_changes else SyncStatus.IN_SYNC
    if state.generation < manifest.generation and not has_local_changes:
        return SyncStatus.REMOTE_AHEAD
    return SyncStatus.DIVERGED


def status(store: ObjectStore, data_dir: Path) -> StatusReport:
    """Compare local and remote without writing anything on either side."""
    manifest = read_remote_manifest(store)
    state = read_state(data_dir)
    local = scan_local(data_dir)
    local_generation = state.generation if state else None

    if manifest is None:
        return StatusReport(
            status=SyncStatus.REMOTE_EMPTY,
            remote_generation=None,
            remote_updated_at=None,
            remote_file_count=0,
            remote_total_bytes=0,
            local_generation=local_generation,
            added=tuple(sorted(local)),
            removed=(),
            modified=(),
        )

    added = tuple(key for key in sorted(local) if key not in manifest.files)
    removed = tuple(key for key in sorted(manifest.files) if key not in local)
    modified = tuple(
        key
        for key in sorted(local)
        if key in manifest.files and manifest.files[key].sha256 != local[key].sha256
    )
    return StatusReport(
        status=_classify(
            manifest, state, has_local_changes=bool(added or removed or modified)
        ),
        remote_generation=manifest.generation,
        remote_updated_at=manifest.updated_at,
        remote_file_count=len(manifest.files),
        remote_total_bytes=manifest.total_bytes,
        local_generation=local_generation,
        added=added,
        removed=removed,
        modified=modified,
    )


# --------------------------------------------------------------------------- #
#  CLI
# --------------------------------------------------------------------------- #


def run(command: str, store: ObjectStore, data_dir: Path) -> int:
    """Execute one subcommand against an injected store and print its report."""
    match command:
        case "pull":
            print(pull(store, data_dir).render())
        case "push":
            print(push(store, data_dir).render())
        case "status":
            print(status(store, data_dir).render())
        case _:  # pragma: no cover - argparse rejects unknown subcommands
            msg = f"未知のサブコマンド: {command!r}"
            raise DataSyncError(msg)
    return 0


def _build_parser() -> argparse.ArgumentParser:
    """Build the `pull` / `push` / `status` subcommand parser."""
    parser = argparse.ArgumentParser(
        prog="data_sync",
        description=f"Cloudflare R2 バケット {BUCKET_NAME} と data/ を同期する",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("pull", help="リモート正本をローカルへ取得する")
    subparsers.add_parser("push", help="ローカルを次世代としてリモートへ公開する")
    subparsers.add_parser("status", help="ローカルとリモートの差分を表示する")
    return parser


def main(argv: list[str] | None = None) -> int:
    """CLI entry point. Returns a non-zero code for any sync failure."""
    args = _build_parser().parse_args(argv)
    try:
        store = build_object_store(R2Settings().require())
        return run(str(args.command), store, DEFAULT_DATA_DIR)
    except DataSyncError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
