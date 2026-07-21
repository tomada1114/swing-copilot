"""Rename this template into a new project by replacing its placeholders."""

from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

OLD_DISTRIBUTION_NAME = "my-package"
OLD_MODULE_NAME = "my_package"
OLD_GITHUB_USER = "your-username"
OLD_AUTHOR_NAME = "Your Name"
OLD_AUTHOR_EMAIL = "you@example.com"

EXCLUDED_FILE_NAMES = {"uv.lock"}
# Only used for the non-git fallback walk (e.g. after ``.git`` was removed):
# generated/untracked directories that must never be rewritten.
EXCLUDED_DIR_NAMES = {
    ".git",
    ".venv",
    "venv",
    "dist",
    "build",
    "site",
    "__pycache__",
    ".mypy_cache",
    ".ruff_cache",
    ".pytest_cache",
    "htmlcov",
    ".tox",
    "node_modules",
}

_MODULE_NAME_PATTERN = re.compile(r"^[a-z][a-z0-9_]*$")


def _normalize_module_name(package_name: str) -> str:
    """Return the snake_case module name derived from a distribution name."""
    module_name = package_name.replace("-", "_")
    if not _MODULE_NAME_PATTERN.fullmatch(module_name):
        msg = (
            f"Invalid package name {package_name!r}: must become a valid Python "
            "identifier once hyphens are replaced with underscores."
        )
        raise SystemExit(msg)
    return module_name


def _git_tracked_files(repo_root: Path) -> list[Path] | None:
    """Return absolute paths of git-tracked files under repo_root.

    Returns:
        The tracked files (excluding EXCLUDED_FILE_NAMES), or None when
        repo_root is not a git repository or git is unavailable.
    """
    try:
        result = subprocess.run(  # noqa: S603
            ["git", "-C", str(repo_root), "ls-files", "-z"],  # noqa: S607
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return None

    files = []
    for relative_path in result.stdout.split("\0"):
        if not relative_path or Path(relative_path).name in EXCLUDED_FILE_NAMES:
            continue
        path = repo_root / relative_path
        if path.is_file():
            files.append(path)
    return files


def _walk_project_files(repo_root: Path) -> list[Path]:
    """Return every file under repo_root, skipping excluded dirs and files."""
    files = []
    for path in repo_root.rglob("*"):
        if not path.is_file():
            continue
        if path.name in EXCLUDED_FILE_NAMES:
            continue
        if EXCLUDED_DIR_NAMES & set(path.relative_to(repo_root).parts):
            continue
        files.append(path)
    return files


def _iter_project_files(repo_root: Path) -> list[Path]:
    """Return the files to rewrite.

    Prefers git-tracked files so generated/untracked trees (``.venv``,
    caches, build output) are never read or rewritten. Falls back to a
    filtered filesystem walk when repo_root is not a git repository, e.g.
    after the template's ``.git`` directory has been removed.
    """
    tracked = _git_tracked_files(repo_root)
    if tracked is not None:
        return tracked
    return _walk_project_files(repo_root)


def _replace_placeholders_in_file(path: Path, replacements: dict[str, str]) -> bool:
    """Replace every placeholder occurrence in a single file.

    Returns:
        True if the file's contents changed.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        return False

    new_text = text
    for old, new in replacements.items():
        new_text = new_text.replace(old, new)

    if new_text == text:
        return False

    path.write_text(new_text, encoding="utf-8")
    return True


def _rename_source_directory(repo_root: Path, new_module_name: str) -> None:
    """Rename src/my_package to src/<new_module_name> in place."""
    old_dir = repo_root / "src" / OLD_MODULE_NAME
    new_dir = repo_root / "src" / new_module_name
    if old_dir == new_dir:
        return
    if not old_dir.is_dir():
        msg = f"Expected source directory not found: {old_dir}"
        raise SystemExit(msg)
    shutil.move(str(old_dir), str(new_dir))


def bootstrap(
    repo_root: Path,
    package_name: str,
    author: str | None,
    email: str | None,
    github_user: str | None,
) -> str:
    """Rename the package and replace template placeholders in-place.

    Returns:
        The normalized module name the source directory was renamed to.
    """
    module_name = _normalize_module_name(package_name)

    replacements = {
        OLD_MODULE_NAME: module_name,
        OLD_DISTRIBUTION_NAME: package_name,
    }
    if github_user:
        replacements[OLD_GITHUB_USER] = github_user
    if author:
        replacements[OLD_AUTHOR_NAME] = author
    if email:
        replacements[OLD_AUTHOR_EMAIL] = email

    for path in _iter_project_files(repo_root):
        _replace_placeholders_in_file(path, replacements)

    _rename_source_directory(repo_root, module_name)
    return module_name


def main(argv: list[str]) -> int:
    """Parse arguments and bootstrap the template in place."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("name", help="New package name, e.g. 'my-cool-lib'")
    parser.add_argument("--author", default=None, help="Author name")
    parser.add_argument("--email", default=None, help="Author email")
    parser.add_argument("--github-user", default=None, help="GitHub username or org")
    args = parser.parse_args(argv)

    module_name = bootstrap(
        REPO_ROOT, args.name, args.author, args.email, args.github_user
    )

    print(f"Bootstrapped {args.name!r} (module: {module_name}).")
    print("Run `uv lock` to regenerate uv.lock for the renamed project.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
