"""Shared `importlib` loader for `scripts/*.py` modules (Issue #398).

Before this module, four test files each hand-rolled the same
`importlib.util.spec_from_file_location` dance to import a `scripts/*.py`
file as a normal module (they live outside the `src/` package, so a plain
`import` can't reach them). Three of the four registered the loaded module in
`sys.modules`; `tests/test_bootstrap.py` did not, which is harmless for
`bootstrap.py` alone but was the kind of drift this module exists to close
off. Registering under a fixed name matters beyond deduplication, too:
`dataclasses` resolves a class's field annotations through
`sys.modules[cls.__module__]`, so a script module with a dataclass (e.g.
`scripts/data_sync.py`'s `R2Settings`) has to be registered under the same
name every time it is loaded, or a second load produces a *different* class
object that other code (a `monkeypatch.setattr` targeting "the" `R2Settings`)
silently misses.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from types import ModuleType

#: `tests/support/script_loader.py` -> `tests/support` -> `tests` -> repo root.
REPO_ROOT = Path(__file__).resolve().parents[2]


def load_script_module(name: str, relative_path: str) -> ModuleType:
    """Load `scripts/<file>.py` once, registered in `sys.modules` under `name`.

    Args:
        name: The module name to register in `sys.modules`. Callers use a
            fixed name per script (e.g. `"data_sync"`) so every caller
            resolves to the same module object instead of a fresh one.
        relative_path: The script's path, relative to the repository root
            (e.g. `"scripts/data_sync.py"`).

    Returns:
        The loaded module -- or the already-loaded one from `sys.modules`,
        if a prior call already loaded it under this `name`.
    """
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, REPO_ROOT / relative_path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # Always register before executing: `exec_module` may itself trigger a
    # dataclass field-annotation resolution against `sys.modules[name]`.
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module
