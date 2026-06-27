"""Slice A — pin-before-branch ordering, ancestry fail-closed, pull-skip,
and create_pr parent-branch wiring.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

_WF = Path(__file__).resolve().parents[1] / "workflows"
if str(_WF) not in sys.path:
    sys.path.insert(0, str(_WF))


def _orch():
    import _engine

    return _engine.orchestrator()


# ---------- helpers ----------


def _make_state(task_id: str, *, next_phase: str = "plan_outcome") -> dict:
    return {
        "task_id": task_id,
        "mode": "agent-pair",
        "phase": "created",
        "phases_completed": [],
        "next_phase": next_phase,
        "retries": {},
        "max_retries_per_phase": 2,
    }


def _setup_task(tmp_path: Path, orch, monkeypatch, task_id: str, **state_extra):
    task_dir = tmp_path / "batch" / "tasks" / task_id
    task_dir.mkdir(parents=True)
    state = _make_state(task_id)
    state.update(state_extra)
    (task_dir / "state.json").write_text(json.dumps(state), encoding="utf-8")
    monkeypatch.setattr(orch, "repo_root", lambda: tmp_path)
    return task_dir


# ---------- pin-before-branch ordering ----------


def test_base_branch_pinned_before_ensure_task_branch(monkeypatch, tmp_path):
    """state['base_branch'] is pinned BEFORE _ensure_task_branch is called,
    using the resolved_base argument, not live config."""
    orch = _orch()
    call_order: list[str] = []
    received_base: dict = {}

    (tmp_path / ".redteam").mkdir()
    (tmp_path / ".redteam" / "config.toml").write_text('[project]\nbranch_prefix = "redteam"\n', encoding="utf-8")
    task_dir = _setup_task(tmp_path, orch, monkeypatch, "task-001")

    def fake_branch(task_id, repo, branch_prefix, base_branch, **kw):
        # By the time this is called, state['base_branch'] must already be pinned
        saved = json.loads((task_dir / "state.json").read_text(encoding="utf-8"))
        call_order.append("ensure_branch")
        received_base["pinned_before_call"] = saved.get("base_branch")
        received_base["arg"] = base_branch
        return f"{branch_prefix}/{task_id}"

    monkeypatch.setattr(orch, "_ensure_task_branch", fake_branch)
    # Mock git_rev_parse so the SHA recording step succeeds (no real git repo in tmp_path)
    monkeypatch.setattr(orch, "git_rev_parse", lambda ref, repo: "fake_sha_pin_test")
    # Halt after branch setup by having the first phase runner stop
    monkeypatch.setitem(
        orch.PHASE_RUNNERS,
        "plan_outcome",
        lambda td, st: {"status": "ask_user", "feedback": "stop", "log": "", "diff": ""},
    )

    orch.process_task(task_dir, resolved_base="redteam/parent-task", base_is_parent=True)

    # base_branch was pinned in state BEFORE ensure_task_branch was called
    assert call_order == ["ensure_branch"]
    assert received_base["pinned_before_call"] == "redteam/parent-task"
    assert received_base["arg"] == "redteam/parent-task"


def test_base_branch_sha_recorded_at_pin_time(monkeypatch, tmp_path):
    """For dependent tasks, base_branch_sha is recorded at pin time."""
    orch = _orch()

    (tmp_path / ".redteam").mkdir()
    (tmp_path / ".redteam" / "config.toml").write_text('[project]\nbranch_prefix = "redteam"\n', encoding="utf-8")
    task_dir = _setup_task(tmp_path, orch, monkeypatch, "task-001")

    monkeypatch.setattr(orch, "_ensure_task_branch", lambda tid, repo, bp, bb, **kw: f"{bp}/{tid}")
    # Mock git_rev_parse to return a known SHA
    monkeypatch.setattr(orch, "git_rev_parse", lambda ref, repo: "abc123")
    monkeypatch.setitem(
        orch.PHASE_RUNNERS,
        "plan_outcome",
        lambda td, st: {"status": "ask_user", "feedback": "stop", "log": "", "diff": ""},
    )

    orch.process_task(task_dir, resolved_base="redteam/parent-task", base_is_parent=True)

    saved = json.loads((task_dir / "state.json").read_text(encoding="utf-8"))
    assert saved.get("base_branch_sha") == "abc123"


def test_root_task_no_base_branch_sha(monkeypatch, tmp_path):
    """Root tasks (base_is_parent=False) do NOT record base_branch_sha."""
    orch = _orch()

    (tmp_path / ".redteam").mkdir()
    (tmp_path / ".redteam" / "config.toml").write_text('[project]\nbranch_prefix = "redteam"\n', encoding="utf-8")
    task_dir = _setup_task(tmp_path, orch, monkeypatch, "task-001")

    monkeypatch.setattr(orch, "_ensure_task_branch", lambda tid, repo, bp, bb, **kw: f"{bp}/{tid}")
    monkeypatch.setitem(
        orch.PHASE_RUNNERS,
        "plan_outcome",
        lambda td, st: {"status": "ask_user", "feedback": "stop", "log": "", "diff": ""},
    )

    orch.process_task(task_dir)  # no base_is_parent → root task

    saved = json.loads((task_dir / "state.json").read_text(encoding="utf-8"))
    assert "base_branch_sha" not in saved


# ---------- ancestry check ----------


def test_ancestry_check_defers_when_branch_wrong_base(monkeypatch, tmp_path):
    """If the existing task branch does NOT descend from the pinned parent,
    process_task defers with a clear reason and never deletes the branch."""
    orch = _orch()

    (tmp_path / ".redteam").mkdir()
    (tmp_path / ".redteam" / "config.toml").write_text('[project]\nbranch_prefix = "redteam"\n', encoding="utf-8")
    task_dir = _setup_task(tmp_path, orch, monkeypatch, "task-001")

    branch_calls: list[list] = []

    def fake_subprocess_run(argv, **kwargs):
        branch_calls.append(list(argv))
        if argv[:3] == ["git", "rev-parse", "--verify"]:
            return SimpleNamespace(returncode=0, stdout="", stderr="")  # branch exists
        if argv[:2] == ["git", "merge-base"]:
            return SimpleNamespace(returncode=1, stdout="", stderr="")  # NOT ancestor
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(orch.subprocess, "run", fake_subprocess_run)

    outcome = orch.process_task(
        task_dir,
        resolved_base="redteam/parent-task",
        base_is_parent=True,
    )

    saved = json.loads((task_dir / "state.json").read_text(encoding="utf-8"))
    assert outcome == "deferred"
    assert saved["last_failure_reason"] == "dependent_branch_not_descended_from_parent"
    assert "parent" in saved["last_failure_log"]
    # No git branch delete command was issued
    delete_calls = [c for c in branch_calls if "branch" in c and "-D" in c]
    assert delete_calls == []


def test_ancestry_check_skipped_when_branch_does_not_exist(monkeypatch, tmp_path):
    """If the task branch doesn't exist yet, no ancestry check is done."""
    orch = _orch()

    (tmp_path / ".redteam").mkdir()
    (tmp_path / ".redteam" / "config.toml").write_text('[project]\nbranch_prefix = "redteam"\n', encoding="utf-8")
    task_dir = _setup_task(tmp_path, orch, monkeypatch, "task-001")

    anc_called = []

    def fake_subprocess_run(argv, **kwargs):
        if argv[:2] == ["git", "merge-base"]:
            anc_called.append(argv)
        if argv[:3] == ["git", "rev-parse", "--verify"]:
            return SimpleNamespace(returncode=1, stdout="", stderr="")  # branch does NOT exist
        if argv[:3] == ["git", "stash", "push"]:
            return SimpleNamespace(returncode=0, stdout="No local changes to save", stderr="")
        if argv[:3] == ["git", "rev-parse"]:
            # git_rev_parse for SHA recording
            return SimpleNamespace(returncode=0, stdout="abc123\n", stderr="")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(orch.subprocess, "run", fake_subprocess_run)
    monkeypatch.setitem(
        orch.PHASE_RUNNERS,
        "plan_outcome",
        lambda td, st: {"status": "ask_user", "feedback": "stop", "log": "", "diff": ""},
    )

    outcome = orch.process_task(task_dir, resolved_base="redteam/parent-task", base_is_parent=True)

    # Did not defer due to ancestry (branch didn't exist)
    assert anc_called == []
    assert outcome == "blocked_on_human_gate"  # stopped at ask_user gate


# ---------- pull-skip for base_is_parent ----------


def test_ensure_task_branch_pull_skipped_when_base_is_parent(monkeypatch, tmp_path):
    """When base_is_parent=True, the git pull --ff-only is skipped."""
    orch = _orch()
    pull_calls: list = []

    def fake_run(argv, **kwargs):
        if argv[:3] == ["git", "pull", "--ff-only"]:
            pull_calls.append(argv)
        if argv[:3] == ["git", "stash", "push"]:
            return SimpleNamespace(returncode=0, stdout="No local changes to save", stderr="")
        if argv[:3] == ["git", "rev-parse", "--verify"]:
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(orch.subprocess, "run", fake_run)

    orch._ensure_task_branch("task-001", tmp_path, base_is_parent=True)

    assert pull_calls == [], "pull should be skipped when base_is_parent=True"


def test_ensure_task_branch_pull_issued_when_not_base_is_parent(monkeypatch, tmp_path):
    """When base_is_parent=False (default), the git pull --ff-only IS issued."""
    orch = _orch()
    pull_calls: list = []

    def fake_run(argv, **kwargs):
        if argv[:3] == ["git", "pull", "--ff-only"]:
            pull_calls.append(argv)
        if argv[:3] == ["git", "stash", "push"]:
            return SimpleNamespace(returncode=0, stdout="No local changes to save", stderr="")
        if argv[:3] == ["git", "rev-parse", "--verify"]:
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(orch.subprocess, "run", fake_run)

    orch._ensure_task_branch("task-001", tmp_path, base_is_parent=False)

    assert len(pull_calls) == 1


# ---------- create_pr receives parent branch as --base ----------


def test_create_pr_prompt_uses_parent_branch_as_base(monkeypatch, tmp_path):
    """create_pr._pr_author_prompt receives the pinned parent branch as base_branch,
    so the gh pr create --base arg is the parent task branch."""
    import phase_runners.create_pr as create_pr_mod

    parent_branch = "redteam/task-parent"
    prompt = create_pr_mod._pr_author_prompt(
        "task-child",
        tmp_path / "task-child",
        "redteam/task-child",
        parent_branch,
    )
    assert f"--base {parent_branch}" in prompt
    assert f"against base branch `{parent_branch}`" in prompt
