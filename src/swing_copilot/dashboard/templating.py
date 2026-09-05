"""Jinja environment and the page chrome every template receives."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from jinja2 import Environment, FileSystemLoader, StrictUndefined, select_autoescape

from swing_copilot.dashboard import charts

if TYPE_CHECKING:
    from swing_copilot.dashboard.models import RunRef

TEMPLATE_DIR = Path(__file__).parent / "templates"
STATIC_DIR = Path(__file__).parent / "static"


@dataclass(frozen=True, slots=True)
class Chrome:
    """The header state shared by every page."""

    runs: tuple[RunRef, ...]
    current_run_id: str | None
    nav: str

    @property
    def current(self) -> RunRef | None:
        """The run the switcher shows as selected."""
        return next(
            (run for run in self.runs if run.run_id == self.current_run_id), None
        )


def build_environment() -> Environment:
    """Create the template environment.

    `StrictUndefined` is deliberate: a renamed view-model field must break the
    page loudly rather than render an empty cell that reads as missing data.
    """
    environment = Environment(
        loader=FileSystemLoader(TEMPLATE_DIR),
        autoescape=select_autoescape(("html",)),
        undefined=StrictUndefined,
        trim_blocks=True,
        lstrip_blocks=True,
    )
    environment.globals["classification_chart"] = charts.classification_chart
    environment.globals["regime_chart"] = charts.regime_chart
    return environment


# Any: Jinja receives a heterogeneous context assembled from view models.
def render(environment: Environment, template: str, context: dict[str, Any]) -> str:
    """Render one template with the given context."""
    return environment.get_template(template).render(**context)
