"""Regression test for the deliberate circular-import hazard between
services/panel.py (imports apply_* from services/room_actions.py at module
scope) and services/room_actions.py (imports refresh_panel_message from
services/panel.py lazily, inside function bodies, specifically to avoid a
circular import).

Normal pytest collection imports every test module (and transitively every
module under test) in whatever order pytest discovers files, which can
happen to import one of these two modules first and mask a regression.
Running each import order in a fresh subprocess makes sure a change that
reintroduces the cycle at module scope is caught regardless of collection
order.
"""

from __future__ import annotations

import subprocess
import sys

from tests.conftest import REPO_ROOT


def _run(code: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-c", code],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        timeout=30,
    )


def test_import_room_actions_then_panel():
    result = _run("import services.room_actions; import services.panel")
    assert result.returncode == 0, result.stderr


def test_import_panel_then_room_actions():
    result = _run("import services.panel; import services.room_actions")
    assert result.returncode == 0, result.stderr
