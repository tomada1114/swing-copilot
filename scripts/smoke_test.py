"""Smoke-test the built wheel in an isolated virtual environment."""

from __future__ import annotations

import subprocess
import sys
import tempfile
import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
ARGUMENTS_WITH_WHEEL = 2
VALIDATE_IMPORTS_SCRIPT = """
from __future__ import annotations

import importlib
import importlib.metadata as metadata
from pathlib import Path, PurePosixPath
import sys

project_name = sys.argv[1]
repo_root = Path(sys.argv[2]).resolve()
distribution = metadata.distribution(project_name)
files = distribution.files or []
import_names: set[str] = set()

for file in files:
    path = PurePosixPath(str(file))
    root = path.parts[0]

    if root.endswith((".dist-info", ".data")):
        continue

    if len(path.parts) == 1:
        if path.suffix == ".py":
            import_names.add(path.stem)
        continue

    if path.suffix == ".py":
        import_names.add(root)

if not import_names:
    raise SystemExit(
        f"Could not determine any import targets from distribution {project_name!r}."
    )

for import_name in sorted(import_names):
    module = importlib.import_module(import_name)
    module_file = getattr(module, "__file__", None)
    if module_file is None:
        continue

    module_path = Path(module_file).resolve()
    if repo_root in module_path.parents:
        raise SystemExit(
            f"Imported {import_name!r} from the repository tree instead of the wheel: "
            f"{module_path}"
        )
"""


def _load_project_name(pyproject_path: Path) -> str:
    """Return the normalized distribution name from pyproject metadata."""
    pyproject = tomllib.loads(pyproject_path.read_text(encoding="utf-8"))
    project = pyproject.get("project")
    if not isinstance(project, dict):
        msg = "pyproject.toml is missing a [project] table."
        raise SystemExit(msg)

    project_name = project.get("name")
    if not isinstance(project_name, str) or not project_name:
        msg = "pyproject.toml must define a non-empty project.name value."
        raise SystemExit(msg)

    return project_name


def _resolve_wheel_path(arguments: list[str]) -> Path:
    """Return the wheel path passed on the command line or auto-detected from dist/."""
    if len(arguments) > ARGUMENTS_WITH_WHEEL:
        msg = "Pass at most one wheel path."
        raise SystemExit(msg)

    if len(arguments) == ARGUMENTS_WITH_WHEEL:
        wheel_path = Path(arguments[1]).resolve()
        if not wheel_path.is_file():
            msg = f"Wheel not found: {wheel_path}"
            raise SystemExit(msg)
        return wheel_path

    wheel_paths = sorted((REPO_ROOT / "dist").glob("*.whl"))
    if len(wheel_paths) != 1:
        msg = "Expected exactly one wheel in dist/. Pass the wheel path explicitly."
        raise SystemExit(msg)

    return wheel_paths[0].resolve()


def _venv_python(venv_dir: Path) -> Path:
    """Return the Python executable path for the temporary virtual environment."""
    if sys.platform == "win32":
        return venv_dir / "Scripts" / "python.exe"
    return venv_dir / "bin" / "python"


def _run(command: list[str]) -> None:
    """Run a command and fail fast if it exits unsuccessfully."""
    # Commands are passed as fixed argument lists with shell=False.
    subprocess.run(command, check=True, cwd=REPO_ROOT)  # noqa: S603


def main(arguments: list[str]) -> int:
    """Install the built wheel into a temp environment and verify imports."""
    project_name = _load_project_name(REPO_ROOT / "pyproject.toml")
    wheel_path = _resolve_wheel_path(arguments)

    with tempfile.TemporaryDirectory(prefix="wheel-smoke-test-") as temp_dir:
        venv_dir = Path(temp_dir) / "venv"
        _run(["uv", "venv", "--python", sys.executable, str(venv_dir)])

        venv_python = _venv_python(venv_dir)
        _run(
            [
                "uv",
                "pip",
                "install",
                "--python",
                str(venv_python),
                "--no-cache",
                str(wheel_path),
            ]
        )
        _run(
            [
                str(venv_python),
                "-c",
                VALIDATE_IMPORTS_SCRIPT,
                project_name,
                str(REPO_ROOT),
            ]
        )

    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
