"""Mirror the operator-owned `data/` and `reports/` trees to/from R2.

The R2 bucket is the source of truth for two local trees, synced together as
one commit:

- `data/`: `copilot.duckdb` and the `data/bars/year=YYYY/*.parquet` Hive tree.
- `reports/`: the daily run archive only -- `reports/<date>/<run_id>.md` and
  everything under `reports/<date>/<run_id>/` (the `analysis_input.json` /
  `analysis_result.json` pair `copilot-retro collect` needs). Local/derived
  artifacts such as `reports/backtests/`, `reports/dry_run/`, `reports/assets/`,
  `reports/retro/`, and `reports/latest.md` are deliberately excluded.

Both this machine and the GitHub Actions runner use the same three verbs:
`pull` before working, `push` after. The pipeline itself is untouched -- this
script only moves bytes.

There is exactly **one** manifest, one generation counter, and one local
state file across both trees -- see `SyncRoot` below. Splitting either would
break the optimistic-lock invariant they exist to enforce.

Two invariants make that safe:

**`manifest.json` is the commit point.** A `push` goes in three steps: upload
the changed data objects, write `manifest.json`, then delete the objects the
new manifest does not reference. Every S3-compatible PUT lands as one whole
object, so the middle step is the instant the new generation becomes real, and
each half of a crashed push has a defined meaning:

- *Crashed after the manifest write.* The new generation is complete: every
  object it references was uploaded before the manifest. What survives is
  unreferenced garbage, which the next `push` collects (deletions are listed
  live, not read from the manifest). A reader sees a fully consistent tree.
- *Crashed before the manifest write.* The previous manifest still stands, and
  nothing it references has been deleted -- deletion happens only after the
  commit. Object keys are paths rather than content hashes, though, so a key
  the old manifest references may already hold the *new* generation's bytes.
  `pull` verifies every object against the manifest's sha256 and size, so that
  shows up as a loud mismatch rather than a silently half-updated tree. The
  fix is to re-run `push` from the machine holding the full local tree.

**A generation counter is the only concurrency guard.** `pull` records the
manifest's `generation` in `data/.r2_sync_state.json`; `push` refuses unless
the remote generation still equals that recorded value, then writes
`generation + 1`. So a write from elsewhere is detected rather than silently
overwritten -- which is why a writing session must be one unbroken
pull -> work -> push, never left open overnight.

`data/copilot.duckdb` is treated as an opaque binary here and is never opened
as a database: DuckDB's file lock is exclusive, and taking it could fail a
concurrent run.

**`reports/` is unbounded and `pull --reports-window N` bounds only the CI
cost of fetching it, never the remote history.** `reports/<date>/<run_id>/`
accumulates one run per weekday forever (Issue #370 made it R2-canonical), so
a fresh GitHub Actions runner's full `pull` grows linearly with calendar time.
`--reports-window N` restricts a `pull` to the `data/` tree (always full) plus
only the `reports/` keys belonging to the `N` most recent *run dates* (never
calendar days, so a holiday or a missed run cannot shrink the window below `N`
actual runs). This makes a windowed local tree intentionally incomplete, which
the rest of the sync has to tolerate without treating the missing history as
"deleted upstream":

- The window actually applied is recorded in `SyncState.reports_window`, not
  passed to `push` as a separate flag -- so a `push` that forgot the flag
  cannot silently garbage-collect real history. `push` reads it back and,
  whenever it is set, suppresses `reports/`'s garbage collection entirely:
  every key `pull` did not fetch stays right where it is on the remote.
- `--reports-append-only`'s guard is scoped the same way: a `reports/` key
  outside the recorded window is expected to be locally absent (the GC
  suppression above is what protects it), so only keys the window did fetch
  are checked for a same-content match. A missing or rewritten key *inside*
  the window is still a violation, exactly as before windowing existed.
- `_guard_against_emptying_a_populated_root` and the mirror-deletion in `pull`
  need no special case: a windowed local `reports/` tree is non-empty (it has
  the window's own files) and every key `pull` chose not to fetch is still
  listed in the manifest, so it is never mistaken for a genuine deletion.

Recovering a windowed CI runner's blind spot -- a correction to an
out-of-window archive -- is an operator task: a full local `pull` (no window)
-> `copilot-retro collect` -> `push` (without `--reports-append-only`).
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
import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime
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
DEFAULT_REPORTS_DIR = REPO_ROOT / "reports"

BUCKET_NAME = "swing-copilot-data-duckdb"
MANIFEST_KEY = "manifest.json"
DATA_PREFIX = "data/"
REPORTS_PREFIX = "reports/"
STATE_FILE_NAME = ".r2_sync_state.json"
BARS_DIR_NAME = "bars"

# Local-only artifacts. Excluded from both directions: never uploaded, and
# never removed by `pull`'s mirror deletion.
EXCLUDED_FILE_NAMES = frozenset({"copilot_dry_run.duckdb"})
EXCLUDED_COMPONENT_GLOB = "*.bak-*"

_HASH_CHUNK_BYTES = 1 << 20
_MISSING_OBJECT_CODES = frozenset({"NoSuchKey", "NotFound", "404"})
_CREDENTIAL_FIELDS = ("r2_account_id", "r2_access_key_id", "r2_secret_access_key")
_REPORTS_RUN_MD_PARTS = 2  # "<date>/<run_id>.md"


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
    """The generation this working copy last pulled.

    `reports_window` defaults to `None` so a state file written before this
    field existed still parses. It records whether the pull that produced
    this working copy's `reports/` tree was windowed, and by how much -- the
    single source `push` reads to decide whether `reports/`'s garbage
    collection and append-only guard must tolerate keys this copy never
    fetched.
    """

    model_config = ConfigDict(extra="forbid")

    generation: int
    reports_window: int | None = None


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
#  Sync roots
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class SyncRoot:
    """One local tree synced under its own bucket-key prefix.

    All roots share the single `manifest.json` / `generation` / state file --
    see the module docstring. A root only contributes its own key prefix, its
    own local directory, and its own shape predicate.
    """

    name: str
    prefix: str
    local_dir: Path
    is_synced_shape: Callable[[PurePosixPath], bool]


def _is_iso_date(value: str) -> bool:
    """Whether `value` parses as a bare ISO-8601 date (`YYYY-MM-DD`)."""
    try:
        date.fromisoformat(value)
    except ValueError:
        return False
    return True


def _is_uuid(value: str) -> bool:
    """Whether `value` parses as a UUID (a run id)."""
    try:
        uuid.UUID(value)
    except ValueError:
        return False
    return True


def _has_data_sync_shape(relative: PurePosixPath) -> bool:
    """Whether a `data/`-relative path is one of the two synced artifacts."""
    parts = relative.parts
    if len(parts) == 1:
        return relative.suffix == ".duckdb"
    return parts[0] == BARS_DIR_NAME and relative.suffix == ".parquet"


def _has_reports_sync_shape(relative: PurePosixPath) -> bool:
    """Whether a `reports/`-relative path belongs to the daily run archive.

    Only `<date>/<run_id>.md` and anything under `<date>/<run_id>/` (any
    depth) qualifies, where `<date>` parses with `date.fromisoformat` and
    `<run_id>` parses as a UUID. This deliberately excludes
    `reports/backtests/`, `reports/dry_run/`, `reports/assets/`,
    `reports/retro/`, and any top-level file such as `reports/latest.md` --
    they are local/derived artifacts, not the run archive `copilot-retro
    collect` needs.
    """
    parts = relative.parts
    if len(parts) < _REPORTS_RUN_MD_PARTS or not _is_iso_date(parts[0]):
        return False
    if len(parts) == _REPORTS_RUN_MD_PARTS:
        candidate = PurePosixPath(parts[1])
        return candidate.suffix == ".md" and _is_uuid(candidate.stem)
    return _is_uuid(parts[1])


def _reports_run_date(key: str) -> str:
    """The `<date>` component of a `reports/`-prefixed object key.

    Callers only ever pass a key already known to start with `REPORTS_PREFIX`
    and to have `_has_reports_sync_shape`'s shape, so the first path segment
    after the prefix is always the run date.
    """
    return key[len(REPORTS_PREFIX) :].split("/", 1)[0]


def _windowed_reports_keys(
    files: Mapping[str, FileEntry], window: int
) -> frozenset[str]:
    """The `reports/` keys of `files` belonging to the `window` newest run dates.

    Counted in run dates, not calendar days, so a market holiday or a missed
    run cannot shrink the window below `window` actual runs (Issue #373). A
    `window` at or beyond the number of run dates present simply selects all
    of them -- `list[:window]` on a shorter list is the whole list. Keys under
    `data/` never appear here; `data/` is never windowed.
    """
    dates = sorted(
        {_reports_run_date(key) for key in files if key.startswith(REPORTS_PREFIX)},
        reverse=True,
    )
    selected_dates = frozenset(dates[:window])
    return frozenset(
        key
        for key in files
        if key.startswith(REPORTS_PREFIX) and _reports_run_date(key) in selected_dates
    )


def build_roots(data_dir: Path, reports_dir: Path) -> tuple[SyncRoot, ...]:
    """Build the `data/` and `reports/` sync roots for a given local pair."""
    return (
        SyncRoot(
            name="data",
            prefix=DATA_PREFIX,
            local_dir=data_dir,
            is_synced_shape=_has_data_sync_shape,
        ),
        SyncRoot(
            name="reports",
            prefix=REPORTS_PREFIX,
            local_dir=reports_dir,
            is_synced_shape=_has_reports_sync_shape,
        ),
    )


DEFAULT_ROOTS = build_roots(DEFAULT_DATA_DIR, DEFAULT_REPORTS_DIR)


def _data_root(roots: tuple[SyncRoot, ...]) -> SyncRoot:
    """The root holding the single shared state file (`data/`, by prefix).

    Raises:
        DataSyncError: When no root in `roots` carries `DATA_PREFIX`. Cannot
            happen through the CLI or `build_roots`; guards against a
            hand-built `roots` tuple omitting the state-holding root.
    """
    for root in roots:
        if root.prefix == DATA_PREFIX:
            return root
    msg = f"{DATA_PREFIX} を持つルートが roots に無い (状態ファイルを置けない)"
    raise DataSyncError(msg)


# --------------------------------------------------------------------------- #
#  Local tree
# --------------------------------------------------------------------------- #


def _is_excluded(relative: PurePosixPath) -> bool:
    """Whether a root-relative path is local-only and must never sync.

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


def iter_sync_paths(root: SyncRoot) -> Iterator[Path]:
    """Yield every local file under `root` belonging to its synced set."""
    if not root.local_dir.is_dir():
        return
    for path in sorted(root.local_dir.rglob("*")):
        if not path.is_file():
            continue
        relative = PurePosixPath(path.relative_to(root.local_dir).as_posix())
        if _is_excluded(relative) or not root.is_synced_shape(relative):
            continue
        yield path


def _object_key(root: SyncRoot, path: Path) -> str:
    """Object key for a local path: the root-relative path, prefixed."""
    return f"{root.prefix}{path.relative_to(root.local_dir).as_posix()}"


def _find_root(roots: tuple[SyncRoot, ...], key: str) -> SyncRoot:
    """Pick the root whose prefix owns `key`.

    Raises:
        DataSyncError: When no known root's prefix matches.
    """
    for root in roots:
        if key.startswith(root.prefix):
            return root
    msg = f"同期対象外のオブジェクトキー: {key!r}"
    raise DataSyncError(msg)


def _local_path(roots: tuple[SyncRoot, ...], key: str) -> Path:
    """Resolve an object key back to a local path inside its owning root.

    Raises:
        DataSyncError: When the key matches no known root's prefix, or
            escapes that root's local directory through `..` or an absolute
            component. A manifest is remote input, so it is treated as
            untrusted for path purposes.
    """
    root = _find_root(roots, key)
    relative = PurePosixPath(key[len(root.prefix) :])
    if relative.is_absolute() or not relative.parts or ".." in relative.parts:
        msg = f"不正なオブジェクトキー: {key!r}"
        raise DataSyncError(msg)
    candidate = root.local_dir / Path(*relative.parts)
    if root.local_dir.resolve() not in candidate.resolve().parents:
        msg = f"{root.prefix} の外を指すオブジェクトキー: {key!r}"
        raise DataSyncError(msg)
    return candidate


def _sha256(path: Path) -> str:
    """Hash a file in chunks. Never opens `copilot.duckdb` as a database."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(_HASH_CHUNK_BYTES):
            digest.update(chunk)
    return digest.hexdigest()


def scan_local(roots: tuple[SyncRoot, ...]) -> dict[str, FileEntry]:
    """Fingerprint every local file in the synced set, keyed by object key."""
    return {
        _object_key(root, path): FileEntry(
            sha256=_sha256(path), size=path.stat().st_size
        )
        for root in roots
        for path in iter_sync_paths(root)
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


def write_state(
    data_dir: Path, generation: int, *, reports_window: int | None = None
) -> None:
    """Record the generation (and `reports/` window, if any) now held.

    Args:
        data_dir: The `data/` root that carries the shared state file.
        generation: The manifest generation this working copy now matches.
        reports_window: The `reports/` window applied to produce this
            working copy's `reports/` tree, or `None` for a full tree.
    """
    data_dir.mkdir(parents=True, exist_ok=True)
    body = SyncState(
        generation=generation, reports_window=reports_window
    ).model_dump_json(indent=2)
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
    retained: tuple[str, ...]
    reports_window: int | None = None

    def render(self) -> str:
        """Human-readable summary."""
        summary = (
            f"pull 完了: generation={self.generation} "
            f"取得={len(self.downloaded)} 既存流用={len(self.skipped)} "
            f"削除={len(self.deleted)}"
        )
        if self.reports_window is not None:
            summary = (
                f"{summary}\n"
                f"  reports/ は直近 {self.reports_window} run date だけを対象にした"
                "(--reports-window)。push はこの窓を記録済みの state から読み、"
                "窓外の reports/ は削除しない"
            )
        if not self.retained:
            return summary
        return (
            f"{summary} 温存={len(self.retained)}\n"
            "  リモートがまだ一度も持っていないツリーのローカルファイルを"
            "削除せずに残した。push すればこの温存は解消する"
        )


def _retainable_prefixes(
    roots: tuple[SyncRoot, ...], manifest_files: Mapping[str, FileEntry]
) -> frozenset[str]:
    """Prefixes of roots the remote manifest holds no key for at all.

    `pull` mirrors, so a local file the manifest does not list is normally
    deleted. That inference only holds for a root the bucket actually tracks.
    When a root was just added to this script (Issue #370 added `reports/` to
    a bucket that had only ever carried `data/`), the remote legitimately has
    nothing under it yet, and mirroring would read that emptiness as "deleted
    upstream" and wipe the operator's local archive on the very first pull --
    silently, and exactly the copy needed to seed the remote.

    So a root with zero keys in the manifest is left alone entirely. The guard
    is self-limiting: one successful `push` publishes the root and it never
    applies to that root again. It cannot mask a genuine upstream deletion of
    everything under a tracked root either -- `push` refuses to publish that
    manifest in the first place (`_guard_against_emptying_a_populated_root`).
    """
    return frozenset(
        root.prefix
        for root in roots
        if not any(key.startswith(root.prefix) for key in manifest_files)
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


def pull(
    store: ObjectStore,
    roots: tuple[SyncRoot, ...],
    *,
    reports_window: int | None = None,
) -> PullReport:
    """Make the local `data/` and `reports/` trees match the remote manifest.

    Files whose sha256 already matches are left alone; everything else is
    downloaded and verified. Local files in either root's synced set that the
    manifest no longer lists are deleted, so each tree mirrors the remote
    instead of accumulating -- with one exception, `_retainable_prefixes`: a
    root the remote has never carried a single key for is not mirrored down to
    empty, because "the bucket has not adopted this root yet" and "the root's
    contents were deleted upstream" are indistinguishable from the manifest
    alone, and only one of them justifies deleting the operator's local copy.

    Args:
        store: The remote object store.
        roots: The local trees to pull into.
        reports_window: When given, only fetch `reports/` keys belonging to
            the `reports_window` most recent run dates (`data/` is always
            fetched in full regardless). A `reports/` key the manifest lists
            but this window excludes is left exactly as it is locally --
            fetched if a prior full or wider-windowed pull already put it
            there, absent if not -- never deleted, since deletion is decided
            against the *full* manifest below, unaffected by the window. The
            window actually used is recorded in the shared state file so
            `push` can derive the same tolerance (Issue #373).

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

    if reports_window is None:
        keys_to_fetch: Mapping[str, FileEntry] = manifest.files
    else:
        windowed = _windowed_reports_keys(manifest.files, reports_window)
        keys_to_fetch = {
            key: entry
            for key, entry in manifest.files.items()
            if not key.startswith(REPORTS_PREFIX) or key in windowed
        }

    local = scan_local(roots)
    downloaded: list[str] = []
    skipped: list[str] = []
    for key, entry in sorted(keys_to_fetch.items()):
        current = local.get(key)
        if current is not None and current.sha256 == entry.sha256:
            skipped.append(key)
            continue
        _download_verified(store, key, _local_path(roots, key), entry)
        downloaded.append(key)

    # Deletion always compares against the *full* manifest, windowed or not:
    # a key this pull chose not to fetch is still listed there, so it is
    # never mistaken for a genuine upstream deletion.
    retainable = _retainable_prefixes(roots, manifest.files)
    deleted: list[str] = []
    retained: list[str] = []
    for key in sorted(local):
        if key in manifest.files:
            continue
        if any(key.startswith(prefix) for prefix in retainable):
            retained.append(key)
            continue
        deleted.append(key)
    for key in deleted:
        _local_path(roots, key).unlink()

    write_state(
        _data_root(roots).local_dir, manifest.generation, reports_window=reports_window
    )
    return PullReport(
        generation=manifest.generation,
        downloaded=tuple(downloaded),
        skipped=tuple(skipped),
        deleted=tuple(deleted),
        retained=tuple(retained),
        reports_window=reports_window,
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


def _guard_against_emptying_a_populated_root(
    roots: tuple[SyncRoot, ...],
    remote_files: Mapping[str, FileEntry],
    local: Mapping[str, FileEntry],
) -> None:
    """Refuse a push that would empty a root the remote still has content in.

    The whole-tree "nothing to sync" guard below is not enough once there are
    two roots: a checkout with `data/` but no `reports/` still has *something*
    to sync, so that guard alone would let a push publish a manifest with zero
    `reports/` keys -- and the garbage collector would then delete the entire
    remote `reports/` history.

    Raises:
        DataSyncError: When a root has at least one key in the remote manifest
            but the local scan found none under that root.
    """
    for root in roots:
        remote_has_root_keys = any(key.startswith(root.prefix) for key in remote_files)
        local_has_root_keys = any(key.startswith(root.prefix) for key in local)
        if remote_has_root_keys and not local_has_root_keys:
            msg = (
                f"リモートには {root.prefix} 配下のオブジェクトがあるのに、"
                f"ローカル {root.local_dir} には同期対象のファイルが 1 件もない。"
                "このまま push するとリモートの当該ツリーを丸ごと空にしてしまうため中止する"
            )
            raise DataSyncError(msg)


def _guard_append_only(
    prefixes: tuple[str, ...],
    remote_files: Mapping[str, FileEntry],
    local: Mapping[str, FileEntry],
    *,
    windowed_reports_keys: frozenset[str] | None = None,
) -> None:
    """Refuse a push that rewrites or drops an already-published object.

    Used by the unattended daily job for `reports/`. That job's analysis
    session reads untrusted news and filing text and is allowed to Write/Edit
    under `reports/`, which was harmless while `reports/` held only the run in
    progress. Now that the tree is the R2-canonical archive and is pulled in
    full before the session starts, a prompt injection could rewrite a
    *historical* `analysis_result.json`; `copilot-retro collect` re-parses any
    archive whose digest changed, and the push would then make the rewritten
    verdicts permanent.

    So in CI the tree is append-only: new run directories may appear, existing
    objects may not change or vanish. Interactive use does not pass this --
    correcting an archive and re-collecting it is a supported operator
    workflow, and the digest-based scan exists to serve it.

    Args:
        prefixes: Root prefixes this push must not rewrite or drop objects
            under.
        remote_files: The manifest currently published on the remote.
        local: This push's local scan.
        windowed_reports_keys: When the pull that produced `local`'s
            `reports/` tree was windowed (Issue #373), the `reports/` keys
            that window fetched. A remote `reports/` key outside this set is
            *expected* to be absent locally -- that absence is what
            `reports/`'s suppressed garbage collection relies on -- so a
            missing local entry for such a key is skipped rather than treated
            as a deletion. It does NOT excuse a rewrite: a key outside the
            window that is nonetheless present locally with different bytes
            is still a violation, so the sha256 comparison always runs when
            the key exists locally. `None` means the pull was not windowed:
            every remote key under `prefixes` is expected locally, exactly as
            before this parameter existed.

    Raises:
        DataSyncError: When a key the remote already publishes under one of
            `prefixes` is present locally with different content, or is
            missing locally and this window expects it to be present.
    """
    for key, remote_entry in sorted(remote_files.items()):
        if not any(key.startswith(prefix) for prefix in prefixes):
            continue
        local_entry = local.get(key)
        if local_entry is None:
            if (
                windowed_reports_keys is not None
                and key.startswith(REPORTS_PREFIX)
                and key not in windowed_reports_keys
            ):
                continue
            msg = (
                f"append-only 違反: 既にリモートにある {key} がローカルに無い。"
                "無人実行は既存のアーカイブを削除できない"
            )
            raise DataSyncError(msg)
        if local_entry.sha256 != remote_entry.sha256:
            msg = (
                f"append-only 違反: 既にリモートにある {key} の内容が書き換わっている。"
                "無人実行は既存のアーカイブを変更できない"
            )
            raise DataSyncError(msg)


def _manifest_files_for_push(
    remote_files: Mapping[str, FileEntry],
    local: Mapping[str, FileEntry],
    reports_window: int | None,
) -> dict[str, FileEntry]:
    """The full set of keys the new manifest must reference.

    A full (unwindowed) pull's `local` scan already covers everything the
    manifest should list, so this is just `dict(local)`.

    A windowed pull's `local` scan does not: it deliberately never fetched the
    `reports/` keys outside its window. Writing `Manifest(files=local)`
    directly would silently stop referencing every one of them -- the
    underlying object survives (GC is suppressed for the very same reason,
    see `push`'s GC step), but a manifest that no longer lists it is
    equivalent to losing it for every future `pull`, which reads only
    `manifest.files` and never lists the bucket itself. So those out-of-window
    `reports/` entries are carried forward unchanged from `remote_files`; a
    local key wins over a carried-forward one on the rare overlap (an
    operator's local tree that still holds more than the recorded window and
    corrected one of those older archives).
    """
    if reports_window is None:
        return dict(local)
    fetched = _windowed_reports_keys(remote_files, reports_window)
    preserved = {
        key: entry
        for key, entry in remote_files.items()
        if key.startswith(REPORTS_PREFIX) and key not in fetched
    }
    return preserved | dict(local)


def push(
    store: ObjectStore,
    roots: tuple[SyncRoot, ...],
    *,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
    append_only_prefixes: tuple[str, ...] = (),
) -> PushReport:
    """Publish the local `data/` and `reports/` trees as the next generation.

    Order matters: changed objects go up first, `manifest.json` is written
    second -- that write is the commit point -- and only then are the objects
    the new generation no longer references deleted (see the module docstring).

    Args:
        store: The remote object store.
        roots: The local trees to publish.
        now: Clock for the manifest's `updated_at`.
        append_only_prefixes: Prefixes under which already-published objects
            may not be modified or removed by this push. The unattended daily
            job passes `reports/` -- see `_guard_append_only`.

    Raises:
        DataSyncError: When the optimistic-lock precondition is unmet, the
            local tree holds nothing to sync at all, one root would be emptied
            on the remote while the other still has local content, or an
            append-only prefix's published object changed within the pull
            window this working copy actually holds.
        ConcurrentWriteError: When the remote advanced since the recorded pull.
    """
    remote = read_remote_manifest(store)
    state_dir = _data_root(roots).local_dir
    state = read_state(state_dir)
    generation = _next_generation(remote, state)
    reports_window = state.reports_window if state is not None else None

    local = scan_local(roots)
    if not local:
        directories = ", ".join(str(root.local_dir) for root in roots)
        msg = (
            f"{directories} に同期対象のファイルが 1 件もない。"
            "リモート正本を空にしないため push を中止する"
        )
        raise DataSyncError(msg)

    remote_files: Mapping[str, FileEntry] = remote.files if remote else {}
    _guard_against_emptying_a_populated_root(roots, remote_files, local)
    if append_only_prefixes:
        windowed_reports_keys = (
            _windowed_reports_keys(remote_files, reports_window)
            if reports_window is not None
            else None
        )
        _guard_append_only(
            append_only_prefixes,
            remote_files,
            local,
            windowed_reports_keys=windowed_reports_keys,
        )

    uploaded: list[str] = []
    unchanged: list[str] = []
    for key, entry in sorted(local.items()):
        previous = remote_files.get(key)
        if previous is not None and previous.sha256 == entry.sha256:
            unchanged.append(key)
            continue
        store.upload(key, _local_path(roots, key))
        uploaded.append(key)

    # The commit point. Everything above only added objects the old manifest
    # does not reference; everything below only removes objects the new one
    # does not reference. `manifest_files` is `local` plus (only when this
    # push follows a windowed pull) the out-of-window `reports/` entries
    # carried forward untouched from `remote_files` -- see
    # `_manifest_files_for_push`.
    manifest_files = _manifest_files_for_push(remote_files, local, reports_window)
    _write_remote_manifest(
        store,
        Manifest(generation=generation, updated_at=now(), files=manifest_files),
    )
    write_state(state_dir, generation, reports_window=reports_window)

    # Garbage collection, listed live per root rather than read from the
    # manifest so that objects orphaned by an earlier interrupted push are
    # collected too. `manifest.json` itself sits outside every root's prefix
    # and is never touched here. `reports/` is skipped entirely when this
    # working copy's last pull was windowed (Issue #373): every key the
    # window did not fetch would otherwise look unreferenced (it is not in
    # `local`, and -- unlike a full pull -- not necessarily preserved as a
    # standalone key here either, since `manifest_files` only carries it as
    # data, not as a GC allowlist) and be deleted, destroying the very
    # history a windowed CI pull is meant to leave alone.
    reports_gc_suppressed = reports_window is not None
    deleted = sorted(
        key
        for root in roots
        if not (reports_gc_suppressed and root.prefix == REPORTS_PREFIX)
        for key in store.list_keys(root.prefix)
        if key not in manifest_files
    )
    for key in deleted:
        store.delete(key)

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
    """A read-only comparison of the local trees against the remote manifest.

    `added`/`removed`/`modified` hold object keys across *every* root; each
    key's own prefix is what identifies its root, and `render()` groups by
    `prefixes` so each root is visible on its own. `prefixes` is carried
    rather than hardcoded so a root added to `build_roots` cannot end up
    counted in the totals but missing from the listing.
    """

    status: SyncStatus
    remote_generation: int | None
    remote_updated_at: datetime | None
    remote_file_count: int
    remote_total_bytes: int
    local_generation: int | None
    added: tuple[str, ...]
    removed: tuple[str, ...]
    modified: tuple[str, ...]
    prefixes: tuple[str, ...]

    def render(self) -> str:
        """Human-readable summary, broken out by root."""
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
        for prefix in self.prefixes:
            lines.append(f"  [{prefix}]")
            lines.extend(f"  + {key}" for key in self.added if key.startswith(prefix))
            lines.extend(f"  - {key}" for key in self.removed if key.startswith(prefix))
            lines.extend(
                f"  M {key}" for key in self.modified if key.startswith(prefix)
            )
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


def status(store: ObjectStore, roots: tuple[SyncRoot, ...]) -> StatusReport:
    """Compare local and remote without writing anything on either side.

    A working copy produced by a windowed pull (Issue #373) never fetched the
    `reports/` keys outside its recorded window, and is not expected to hold
    them -- that absence is exactly what the append-only guard's and push's
    GC suppression both treat as normal. Without accounting for that here,
    `removed` would list every one of those keys forever, so a windowed
    working copy could never report `in-sync` even immediately after a pull.
    """
    manifest = read_remote_manifest(store)
    state = read_state(_data_root(roots).local_dir)
    local = scan_local(roots)
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
            prefixes=tuple(root.prefix for root in roots),
        )

    reports_window = state.reports_window if state is not None else None
    expected_absent: frozenset[str] = frozenset()
    if reports_window is not None:
        windowed = _windowed_reports_keys(manifest.files, reports_window)
        expected_absent = frozenset(
            key
            for key in manifest.files
            if key.startswith(REPORTS_PREFIX) and key not in windowed
        )
    added = tuple(key for key in sorted(local) if key not in manifest.files)
    removed = tuple(
        key
        for key in sorted(manifest.files)
        if key not in local and key not in expected_absent
    )
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
        prefixes=tuple(root.prefix for root in roots),
    )


# --------------------------------------------------------------------------- #
#  CLI
# --------------------------------------------------------------------------- #


def run(
    command: str,
    store: ObjectStore,
    roots: tuple[SyncRoot, ...],
    *,
    reports_append_only: bool = False,
    reports_window: int | None = None,
) -> int:
    """Execute one subcommand against an injected store and print its report."""
    match command:
        case "pull":
            print(pull(store, roots, reports_window=reports_window).render())
        case "push":
            prefixes = (REPORTS_PREFIX,) if reports_append_only else ()
            print(push(store, roots, append_only_prefixes=prefixes).render())
        case "status":
            print(status(store, roots).render())
        case _:  # pragma: no cover - argparse rejects unknown subcommands
            msg = f"未知のサブコマンド: {command!r}"
            raise DataSyncError(msg)
    return 0


def _positive_int(value: str) -> int:
    """`argparse` type: an integer strictly greater than zero.

    Raises:
        argparse.ArgumentTypeError: When `value` is not a positive integer.
    """
    try:
        parsed = int(value)
    except ValueError as error:
        msg = f"整数を指定する: {value!r}"
        raise argparse.ArgumentTypeError(msg) from error
    if parsed <= 0:
        msg = f"1 以上の整数を指定する: {value!r}"
        raise argparse.ArgumentTypeError(msg)
    return parsed


def _build_parser() -> argparse.ArgumentParser:
    """Build the `pull` / `push` / `status` subcommand parser."""
    parser = argparse.ArgumentParser(
        prog="data_sync",
        description=(
            f"Cloudflare R2 バケット {BUCKET_NAME} と data/・reports/ を同期する"
        ),
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    pull_parser = subparsers.add_parser("pull", help="リモート正本をローカルへ取得する")
    pull_parser.add_argument(
        "--reports-window",
        type=_positive_int,
        default=None,
        metavar="N",
        help=(
            "reports/ を直近 N run date だけ取得する (data/ は常に全件。"
            "既定は指定なし = 全件取得。無人実行の CI job だけがこれを付ける"
            " -- 適用した窓は state ファイルに記録され、push はそこから GC 抑止と"
            " append-only の判定範囲を導出する)"
        ),
    )
    push_parser = subparsers.add_parser(
        "push", help="ローカルを次世代としてリモートへ公開する"
    )
    push_parser.add_argument(
        "--reports-append-only",
        action="store_true",
        help=(
            "既にリモートにある reports/ 配下のオブジェクトの変更・削除を拒否する"
            " (信頼できないテキストを読む無人実行専用。ローカルでの"
            "アーカイブ訂正・再取り込みは通常どおり行えるよう既定では無効)"
        ),
    )
    subparsers.add_parser("status", help="ローカルとリモートの差分を表示する")
    return parser


def main(argv: list[str] | None = None) -> int:
    """CLI entry point. Returns a non-zero code for any sync failure."""
    args = _build_parser().parse_args(argv)
    try:
        store = build_object_store(R2Settings().require())
        return run(
            str(args.command),
            store,
            DEFAULT_ROOTS,
            reports_append_only=bool(getattr(args, "reports_append_only", False)),
            reports_window=getattr(args, "reports_window", None),
        )
    except DataSyncError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
