from __future__ import annotations

import json
from types import SimpleNamespace


def _load_orchestrator_module():
    import _engine

    return _engine.orchestrator()


def test_ensure_task_branch_stashes_before_checkout_and_pops(monkeypatch, tmp_path):
    orchestrator = _load_orchestrator_module()
    calls: list[list[str]] = []

    def fake_run(argv, **kwargs):
        calls.append(argv)
        if argv[:3] == ["git", "stash", "push"]:
            return SimpleNamespace(returncode=0, stdout="Saved working directory\n", stderr="")
        if argv[:3] == ["git", "rev-parse", "--verify"]:
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(orchestrator.subprocess, "run", fake_run)

    branch = orchestrator._ensure_task_branch("task-001-demo", tmp_path)

    assert branch == "redteam/task-001-demo"  # generic default branch_prefix
    assert calls[0][:3] == ["git", "stash", "push"]
    checkout_main_index = calls.index(["git", "checkout", "main"])
    stash_pop_index = calls.index(["git", "stash", "pop"])
    assert checkout_main_index > 0
    assert stash_pop_index > checkout_main_index


def test_ensure_task_branch_skips_pop_when_no_changes(monkeypatch, tmp_path):
    orchestrator = _load_orchestrator_module()
    calls: list[list[str]] = []

    def fake_run(argv, **kwargs):
        calls.append(argv)
        if argv[:3] == ["git", "stash", "push"]:
            return SimpleNamespace(returncode=0, stdout="No local changes to save\n", stderr="")
        if argv[:3] == ["git", "rev-parse", "--verify"]:
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(orchestrator.subprocess, "run", fake_run)

    orchestrator._ensure_task_branch("task-001-demo", tmp_path)

    assert ["git", "stash", "pop"] not in calls


def test_stash_pop_failure_defers_task(monkeypatch, tmp_path):
    orchestrator = _load_orchestrator_module()
    task_dir = tmp_path / "batch" / "tasks" / "task-001-demo"
    task_dir.mkdir(parents=True)
    state = {
        "task_id": "task-001-demo",
        "mode": "agent-pair",
        "phase": "created",
        "phases_completed": [],
        "next_phase": "plan_outcome",
        "review_items": [],
        "retries": {},
    }
    (task_dir / "state.json").write_text(json.dumps(state), encoding="utf-8")

    monkeypatch.setattr(orchestrator, "repo_root", lambda: tmp_path)
    monkeypatch.setattr(
        orchestrator,
        "_ensure_task_branch",
        lambda task_id, repo, branch_prefix, base_branch: (_ for _ in ()).throw(RuntimeError("stash pop conflict")),
    )

    outcome = orchestrator.process_task(task_dir)
    saved = json.loads((task_dir / "state.json").read_text(encoding="utf-8"))

    assert outcome == "error"
    assert saved["next_phase"] == "deferred"
    assert saved["last_failure_reason"] == "branch_setup_failed"
    assert "stash pop conflict" in saved["last_failure_log"]


def test_ensure_task_branch_uses_custom_prefix(monkeypatch, tmp_path):
    """branch_prefix is config-driven; the prefix is no longer hardcoded."""
    orchestrator = _load_orchestrator_module()

    def fake_run(argv, **kwargs):
        if argv[:3] == ["git", "stash", "push"]:
            return SimpleNamespace(returncode=0, stdout="No local changes to save\n", stderr="")
        if argv[:3] == ["git", "rev-parse", "--verify"]:
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(orchestrator.subprocess, "run", fake_run)
    branch = orchestrator._ensure_task_branch("task-001-demo", tmp_path, branch_prefix="task")
    assert branch == "task/task-001-demo"


def test_ensure_task_branch_uses_custom_base_branch(monkeypatch, tmp_path):
    """base_branch is config-driven; a consumer on `develop` checks out/pulls
    develop, not a hardcoded `main`."""
    orchestrator = _load_orchestrator_module()
    calls = []

    def fake_run(argv, **kwargs):
        calls.append(argv)
        if argv[:3] == ["git", "stash", "push"]:
            return SimpleNamespace(returncode=0, stdout="No local changes to save\n", stderr="")
        if argv[:3] == ["git", "rev-parse", "--verify"]:
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(orchestrator.subprocess, "run", fake_run)
    orchestrator._ensure_task_branch("task-001-demo", tmp_path, branch_prefix="task", base_branch="develop")
    assert ["git", "checkout", "develop"] in calls
    assert ["git", "pull", "--ff-only", "origin", "develop"] in calls
    assert ["git", "checkout", "main"] not in calls


def test_process_task_passes_config_branch_prefix(monkeypatch, tmp_path):
    """process_task loads .redteam/config.toml and threads branch_prefix through
    to _ensure_task_branch (guards the wiring, not just the helper)."""
    orchestrator = _load_orchestrator_module()
    (tmp_path / ".redteam").mkdir()
    (tmp_path / ".redteam" / "config.toml").write_text('[project]\nbranch_prefix = "custom"\n')
    task_dir = tmp_path / "batch" / "tasks" / "task-001-demo"
    task_dir.mkdir(parents=True)
    state = {
        "task_id": "task-001-demo",
        "mode": "agent-pair",
        "phase": "created",
        "phases_completed": [],
        "next_phase": "plan_outcome",
        "review_items": [],
        "retries": {},
    }
    (task_dir / "state.json").write_text(json.dumps(state), encoding="utf-8")

    monkeypatch.setattr(orchestrator, "repo_root", lambda: tmp_path)
    received: dict = {}

    def spy(task_id, repo, branch_prefix, base_branch):
        received["prefix"] = branch_prefix
        received["base"] = base_branch
        raise RuntimeError("stop after branch")

    monkeypatch.setattr(orchestrator, "_ensure_task_branch", spy)
    orchestrator.process_task(task_dir)
    assert received["prefix"] == "custom"
    assert received["base"] == "main"  # default base_branch threaded through
