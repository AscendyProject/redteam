"""create_pr branch resolution.

The pr-author phase pushes to a branch. Normally process_task has already saved
state["branch"] (the config-driven name). The prefix-based fallback — for a
legacy/partial state with no saved branch — must also use the config
branch_prefix, not a hardcoded prefix. Extracting `_pr_branch` makes that
resolution unit-testable without driving the whole pr-author phase.
"""

from __future__ import annotations

import sys
from pathlib import Path

_WF = Path(__file__).resolve().parents[1] / "workflows"
if str(_WF) not in sys.path:
    sys.path.insert(0, str(_WF))

from phase_runners.create_pr import _pr_branch  # noqa: E402


def test_pr_branch_prefers_saved_state_branch() -> None:
    """Normal flow: process_task already saved the branch — use it verbatim."""
    assert _pr_branch({"branch": "proj/task-001"}, "task-001", "proj") == "proj/task-001"
    # A custom-prefix saved branch is honored as-is too.
    assert _pr_branch({"branch": "custom/task-001"}, "task-001", "proj") == "custom/task-001"


def test_pr_branch_fallback_uses_config_prefix() -> None:
    """Legacy/partial state without a saved branch → prefix-based fallback is
    config-driven, no longer a hardcoded prefix."""
    assert _pr_branch({}, "task-001", "custom") == "custom/task-001"
    assert _pr_branch({"branch": None}, "task-001", "custom") == "custom/task-001"
