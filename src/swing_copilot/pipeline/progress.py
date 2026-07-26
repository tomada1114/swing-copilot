"""Terminal progress rendering for the daily pipeline."""

from __future__ import annotations

from rich.console import Console
from rich.progress import BarColumn, Progress, TaskID, TaskProgressColumn, TextColumn


class ProgressReporter:
    """Render daily pipeline progress to stderr.

    TTY では Rich の進捗バーを、非 TTY では決定的なプレーンテキストを使う。
    """

    def __init__(self, console: Console | None = None) -> None:
        """Initialize a reporter for the supplied or stderr console."""
        self._console = console or Console(stderr=True)
        self._progress: Progress | None = None
        self._step_task_id: TaskID | None = None
        self._substep_task_id: TaskID | None = None

    def step_started(self, index: int, total: int, name: str) -> None:
        """Start rendering one named pipeline step."""
        if not self._console.is_terminal:
            return
        progress = self._ensure_progress()
        self._step_task_id = progress.add_task(f"[{index}/{total}] {name}", total=1)

    def step_finished(
        self, index: int, total: int, name: str, status: str, elapsed_s: float
    ) -> None:
        """Render the completed step with its outcome and elapsed time."""
        if not self._console.is_terminal:
            self._console.print(f"[{index}/{total}] {name} {status} ({elapsed_s:.1f}s)")
            return
        if self._step_task_id is not None:
            self._ensure_progress().update(
                self._step_task_id,
                completed=1,
                description=f"[{index}/{total}] {name} {status}",
            )

    def substep(self, done: int, total: int, label: str) -> None:
        """Update the TTY-only fundamentals sub-progress bar."""
        if not self._console.is_terminal:
            return
        progress = self._ensure_progress()
        if self._substep_task_id is None:
            self._substep_task_id = progress.add_task(label, total=total)
        progress.update(self._substep_task_id, completed=done, total=total)

    def _ensure_progress(self) -> Progress:
        if self._progress is None:
            self._progress = Progress(
                TextColumn("{task.description}"),
                BarColumn(),
                TaskProgressColumn(),
                console=self._console,
                transient=True,
            )
            self._progress.start()
        return self._progress


class NullProgressReporter(ProgressReporter):
    """Keep tests and library use silent with no-op methods."""

    def __init__(self) -> None:
        """Initialize the silent reporter."""

    def step_started(self, index: int, total: int, name: str) -> None:
        """Do nothing when a step starts."""

    def step_finished(
        self, index: int, total: int, name: str, status: str, elapsed_s: float
    ) -> None:
        """Do nothing when a step finishes."""

    def substep(self, done: int, total: int, label: str) -> None:
        """Do nothing for substep updates."""
