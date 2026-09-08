"""Tests that pin the packaging decisions docs/DESIGN.md records.

These read ``pyproject.toml`` from the checkout rather than package
metadata, so they say what the project declares rather than what happens to
be installed in the current environment.
"""

from __future__ import annotations

import re
import tomllib
from importlib import resources
from pathlib import Path

import pytest

_PYPROJECT = Path(__file__).resolve().parents[1] / "pyproject.toml"

#: Anything that would make this a two-HTTP-stack or two-CLI-framework
#: project. The CLI and hook use ``urllib`` and ``argparse`` deliberately, so
#: they start fast enough to sit in front of every prompt.
_SECOND_FRAMEWORKS = {
    "click",
    "django",
    "fastapi",
    "flask",
    "httpx",
    "requests",
    "starlette",
    "typer",
    "urllib3",
    "uvicorn",
}


def _runtime_dependency_names() -> set[str]:
    data = tomllib.loads(_PYPROJECT.read_text(encoding="utf-8"))
    raw: list[str] = data["project"]["dependencies"]
    return {re.split(r"[\[<>=!~; ]", spec, maxsplit=1)[0].strip().lower() for spec in raw}


@pytest.mark.invariant(decision="One runtime dependency, argparse over typer")
def test_no_second_http_or_cli_framework_is_a_runtime_dependency() -> None:
    """``aiohttp`` serves and forwards; nothing else may join it in that role."""
    names = _runtime_dependency_names()
    assert "aiohttp" in names
    assert not (names & _SECOND_FRAMEWORKS), (
        f"second framework declared: {names & _SECOND_FRAMEWORKS}"
    )


#: The three packaged shadow-review-fork example templates; see
#: ``src/claude_gateway_extensions/templates/`` and the "Shadow review fork"
#: section in ``docs/DESIGN.md``.
_PACKAGED_TEMPLATES = ["task-assignment", "permission-classifier", "btw"]


@pytest.mark.parametrize("name", _PACKAGED_TEMPLATES)
def test_packaged_fork_review_template_is_resource_readable(name: str) -> None:
    """Each example template ships inside the package and is readable via ``importlib.resources``.

    This is what :func:`~claude_gateway_extensions.cli.fork_review._load_template`
    falls back to once the user's own ``CGX_TEMPLATE_DIR`` has nothing by that
    name, in an installed layout (wheel or editable install) where there is
    no ``src/`` checkout to read from directly.
    """
    resource = resources.files("claude_gateway_extensions").joinpath("templates", f"{name}.txt")
    assert resource.is_file(), f"template resource missing from package: {name}"
    text = resource.read_text(encoding="utf-8")
    assert "intervene:" in text
    assert "verdict" in text
