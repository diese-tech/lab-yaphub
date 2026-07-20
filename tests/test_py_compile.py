"""Basic syntax sanity check: every tracked .py file in the repo must at
least compile. This catches stray syntax errors that unit tests covering
only a subset of modules could otherwise miss.
"""

from __future__ import annotations

import py_compile
import subprocess

import pytest

from tests.conftest import REPO_ROOT


def _tracked_python_files() -> list[str]:
    result = subprocess.run(
        ["git", "ls-files", "*.py"],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        check=True,
    )
    return [line for line in result.stdout.splitlines() if line]


@pytest.mark.parametrize("relative_path", _tracked_python_files())
def test_file_compiles(relative_path: str):
    py_compile.compile(str(REPO_ROOT / relative_path), doraise=True)
