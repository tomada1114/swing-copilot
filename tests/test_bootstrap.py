"""Tests for scripts/bootstrap.py."""

from __future__ import annotations

import importlib.util
import shutil
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from types import ModuleType

REPO_ROOT = Path(__file__).resolve().parents[1]
PLACEHOLDERS = (
    "my-package",
    "my_package",
    "your-username",
    "Your Name",
    "you@example.com",
)


def _load_bootstrap_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "bootstrap", REPO_ROOT / "scripts" / "bootstrap.py"
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _copy_tracked_files(destination: Path) -> None:
    result = subprocess.run(  # noqa: S603
        ["git", "-C", str(REPO_ROOT), "ls-files"],  # noqa: S607
        check=True,
        capture_output=True,
        text=True,
    )
    for relative_path in result.stdout.splitlines():
        source = REPO_ROOT / relative_path
        if not source.is_file():
            continue
        target = destination / relative_path
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)


def test_bootstrap_replaces_all_placeholders(tmp_path):
    _copy_tracked_files(tmp_path)
    bootstrap = _load_bootstrap_module()

    module_name = bootstrap.bootstrap(
        tmp_path,
        package_name="acme-widgets",
        author="Ada Lovelace",
        email="ada@example.com",
        github_user="ada",
    )

    assert module_name == "acme_widgets"
    assert (tmp_path / "src" / "acme_widgets").is_dir()
    assert not (tmp_path / "src" / "my_package").exists()

    for path in tmp_path.rglob("*"):
        if not path.is_file() or path.name == "uv.lock":
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        for placeholder in PLACEHOLDERS:
            assert placeholder not in text, f"{placeholder!r} still present in {path}"


def test_bootstrap_rejects_invalid_package_name(tmp_path):
    _copy_tracked_files(tmp_path)
    bootstrap = _load_bootstrap_module()

    with pytest.raises(SystemExit):
        bootstrap.bootstrap(
            tmp_path,
            package_name="1-invalid-name",
            author=None,
            email=None,
            github_user=None,
        )
