"""Slice A — centralized freeze guard inside pinned_base_branch.

Tests that the accessor raises when the parent tip SHA differs from the recorded
value, and is a no-op for root tasks (no SHA). Also verifies that the guard is
exercised from the review_code, write_test, and create_pr call sites, so every
reader fails closed automatically.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

_WF = Path(__file__).resolve().parents[1] / "workflows"
if str(_WF) not in sys.path:
    sys.path.insert(0, str(_WF))


def _base():
    import _engine

    return _engine.base()


def _orch():
    import _engine

    return _engine.orchestrator()


# ---------- accessor unit tests ----------


def test_freeze_guard_no_op_for_root_task():
    """Root tasks have no base_branch_sha → freeze guard is a no-op."""
    base = _base()
    # No base_branch_sha → no git call needed, no error
    result = base.pinned_base_branch({"base_branch": "main"}, Path("/fake-repo"))
    assert result == "main"


def test_freeze_guard_no_op_when_sha_empty_string():
    """Empty-string base_branch_sha is treated as absent → no-op."""
    base = _base()
    result = base.pinned_base_branch({"base_branch": "main", "base_branch_sha": ""}, Path("/fake"))
    assert result == "main"


def test_freeze_guard_passes_when_sha_matches(monkeypatch):
    """When the live SHA matches the recorded SHA, no error is raised."""
    base = _base()
    monkeypatch.setattr(base, "git_rev_parse", lambda ref, repo: "abc123")
    state: dict[str, Any] = {"base_branch": "redteam/parent", "base_branch_sha": "abc123"}
    result = base.pinned_base_branch(state, Path("/fake"))
    assert result == "redteam/parent"


def test_freeze_guard_raises_when_sha_moved(monkeypatch):
    """Raises ValueError when parent tip moved (recorded vs live SHA differ)."""
    base = _base()
    monkeypatch.setattr(base, "git_rev_parse", lambda ref, repo: "new_sha_xyz")
    state: dict[str, Any] = {"base_branch": "redteam/parent", "base_branch_sha": "old_sha_abc"}
    with pytest.raises(ValueError, match="freeze guard"):
        base.pinned_base_branch(state, Path("/fake"))


def test_freeze_guard_raises_when_rev_parse_fails(monkeypatch):
    """If git rev-parse fails (branch deleted?), accessor raises fail-closed."""
    base = _base()

    def _fail(ref, repo):
        raise RuntimeError("git rev-parse failed (exit 128)")

    monkeypatch.setattr(base, "git_rev_parse", _fail)
    state: dict[str, Any] = {"base_branch": "redteam/parent", "base_branch_sha": "some_sha"}
    with pytest.raises(ValueError, match="freeze guard"):
        base.pinned_base_branch(state, Path("/fake"))


# ---------- freeze guard from review_code call site ----------


def test_freeze_guard_triggered_from_review_code(monkeypatch):
    """review_code.run raises when the freeze guard detects a moved parent tip."""
    import phase_runners.review_code as rc

    # Patch pinned_base_branch on the module to simulate a SHA drift
    def _frozen(state, repo):
        raise ValueError("freeze guard: parent branch tip moved")

    monkeypatch.setattr(rc, "pinned_base_branch", _frozen)

    state: dict[str, Any] = {
        "mode": "agent-pair",
        "base_branch": "redteam/parent",
        "base_branch_sha": "old",
    }
    result = rc.run(Path("/fake/task"), state)
    # Should surface as an error (not a crash)
    assert result["status"] == "error"
    assert "freeze guard" in result["feedback"]


# ---------- freeze guard from write_test call site ----------


def test_freeze_guard_triggered_from_write_test(monkeypatch):
    """write_test.run raises when the freeze guard detects a moved parent tip."""
    import phase_runners.write_test as wt

    def _frozen(state, repo):
        raise ValueError("freeze guard: parent branch tip moved")

    monkeypatch.setattr(wt, "pinned_base_branch", _frozen)

    # write_test.run also needs project_config and get_worker_adapter — mock them minimally
    def _fake_project_config():
        from types import SimpleNamespace

        return SimpleNamespace(
            test_dir=".redteam/tests",
            test_file_glob="test_*.py",
            test_conventions_file=".redteam/docs/test-conventions.md",
            verify_command="pytest",
            verification_allowlist=["pytest"],
        )

    monkeypatch.setattr(wt, "project_config", _fake_project_config)

    state: dict[str, Any] = {
        "base_branch": "redteam/parent",
        "base_branch_sha": "old",
        "tdd_test_files": None,
    }
    result = wt.run(Path("/fake/task"), state)
    assert result["status"] == "error"
    assert "freeze guard" in result["feedback"]


# ---------- freeze guard from create_pr call site ----------


def test_freeze_guard_triggered_from_create_pr(monkeypatch, tmp_path):
    """create_pr.run propagates the freeze guard error as a phase error."""
    import phase_runners.create_pr as cp

    def _frozen(state, repo):
        raise ValueError("freeze guard: parent branch tip moved")

    monkeypatch.setattr(cp, "pinned_base_branch", _frozen)

    # Mock the preflight to pass (focus on the freeze guard)
    monkeypatch.setattr(cp, "_preflight_pr_auth", lambda cwd: None)

    # Mock load_config to return a minimal object
    def _fake_load_config(repo):
        return SimpleNamespace(project=SimpleNamespace(branch_prefix="redteam"))

    monkeypatch.setattr(cp, "load_config", _fake_load_config)
    monkeypatch.setattr(cp, "repo_root", lambda: tmp_path)

    state: dict[str, Any] = {
        "base_branch": "redteam/parent",
        "base_branch_sha": "old",
        "task_id": "task-child",
        "branch": "redteam/task-child",
    }
    task_dir = tmp_path / "task-child"
    task_dir.mkdir()

    result = cp.run(task_dir, state)
    assert result["status"] == "error"
    assert "freeze guard" in result["feedback"]
