"""Regression tests for #91 Part A — tracked-change attribution to the worker.

Covers:
- get_or_set_tracked_baseline helper (key-absent, key-present, idempotency, no-persist)
- Out-of-scope tracked WIP → fail closed, no persisted baseline (PR-001 fix) — both paths
- Out-of-scope floor uses FRESH probe, not stored baseline — both paths
- Operator in-scope pre-edit NOT attributed to worker — both paths (real git)
- Worker's own tracked changes still land — both paths (real git)
- Set-once / resume: tracked baseline survives interrupted round — both paths
- Clean-tree run unchanged (real git)
- Pinned base usage: no live config bleed
- _commit_worker_diff before_tracked passthrough (via state["implement_tracked_baseline"])
- Mode-neutrality: single _tracked_changed_paths + get_or_set_tracked_baseline definition
- No stderr leakage on probe failure — both paths
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any


def _base():
    import _engine

    return _engine.base()


def _impl():
    import _engine

    return _engine.implement()


_PROJ = SimpleNamespace(
    source_dirs=["app/"],
    test_dir="tests/",
    context_file="docs/ctx.md",
    base_branch="main",
)


def _state(**extra: Any) -> dict[str, Any]:
    s = {"task_id": "task-001", "mode": "agent-pair", "base_branch": "main", "verification": {"commands": []}}
    s.update(extra)
    return s


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True)


def _make_repo(tmp_path: Path) -> tuple[Path, Path]:
    """Return (repo, task_dir) with a clean git repo on a task branch."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "t@e")
    _git(repo, "config", "user.name", "t")
    (repo / "app").mkdir()
    (repo / "tests").mkdir()
    (repo / "tests" / ".gitkeep").write_text("", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "init")
    _git(repo, "checkout", "-b", "redteam/task-001")
    task_dir = repo / ".redteam" / "batches" / "b" / "tasks" / "task-001"
    task_dir.mkdir(parents=True)
    return repo, task_dir


def _committed_paths(repo: Path) -> list[str]:
    result = subprocess.run(
        ["git", "diff", "--name-only", "main...HEAD"],
        cwd=repo,
        capture_output=True,
        text=True,
    )
    return [p for p in result.stdout.split("\n") if p]


def _wire_agent_pair(impl, monkeypatch, repo: Path, *, on_invoke=None) -> None:
    monkeypatch.setattr(impl, "repo_root", lambda: repo)
    monkeypatch.setattr(impl, "project_config", lambda: _PROJ)

    def invoke(**kwargs: Any) -> dict[str, Any]:
        if on_invoke is not None:
            on_invoke()
        return {"returncode": 0, "stdout": "done", "stderr": ""}

    monkeypatch.setattr(impl, "get_worker_adapter", lambda state: SimpleNamespace(invoke=invoke))
    monkeypatch.setattr(
        impl,
        "_run_verification_commands",
        lambda cwd, commands, project_verify_command=None, allowlist=None: (0, "ok\n"),
    )


def _wire_tdd(impl, monkeypatch, repo: Path, *, on_invoke=None) -> None:
    import config as _config

    tdd_proj = SimpleNamespace(
        source_dirs=["app/"],
        test_dir="tests/",
        context_file="c",
        base_branch="main",
        verify_command="true",
        verification_allowlist=("true",),
    )
    monkeypatch.setattr(_config, "load_config", lambda *_a, **_k: SimpleNamespace(project=tdd_proj))
    monkeypatch.setattr(impl, "repo_root", lambda: repo)
    monkeypatch.setattr(impl, "project_config", lambda: tdd_proj)
    monkeypatch.setattr(impl, "_run_verify_sh", lambda cwd, argv: (0, "ok\n"))

    def invoke(**kwargs: Any) -> dict[str, Any]:
        if on_invoke is not None:
            on_invoke()
        return {"returncode": 0, "stdout": "done", "stderr": ""}

    monkeypatch.setattr(impl, "get_worker_adapter", lambda state: SimpleNamespace(invoke=invoke))


# =============================================================================
# Helper unit tests (pure, no real git)
# =============================================================================


def test_get_or_set_tracked_baseline_key_absent(tmp_path):
    """Key absent → calls probe exactly once, stores sorted list, returns set."""
    base = _base()
    calls: list = []

    def fake_tracked(cwd: Path, base_branch: str) -> list[str]:
        calls.append((cwd, base_branch))
        return ["app/b.py", "app/a.py"]

    state: dict[str, Any] = {}
    result = base.get_or_set_tracked_baseline(state, tmp_path, "main", _tracked_fn=fake_tracked)

    assert result == {"app/a.py", "app/b.py"}
    assert state["implement_tracked_baseline"] == ["app/a.py", "app/b.py"]  # sorted list
    assert len(calls) == 1
    assert calls[0][1] == "main"


def test_get_or_set_tracked_baseline_key_absent_live_probe(monkeypatch, tmp_path):
    """Key absent with no _tracked_fn → calls _tracked_changed_paths from base."""
    base = _base()
    calls: list = []

    def fake_probe(cwd, base_branch):
        calls.append(base_branch)
        return ["app/x.py"]

    monkeypatch.setattr(base, "_tracked_changed_paths", fake_probe)
    state: dict[str, Any] = {}
    result = base.get_or_set_tracked_baseline(state, tmp_path, "main")

    assert result == {"app/x.py"}
    assert len(calls) == 1


def test_get_or_set_tracked_baseline_does_not_persist(tmp_path):
    """Helper does NOT write state.json; caller is responsible for persist_state."""
    base = _base()

    state: dict[str, Any] = {}
    base.get_or_set_tracked_baseline(state, tmp_path, "main", _tracked_fn=lambda *_: ["app/x.py"])

    assert not (tmp_path / "state.json").exists()


def test_get_or_set_tracked_baseline_key_present_set_once(tmp_path):
    """Key present → returns stored list as set, does NOT call probe or _tracked_fn."""
    base = _base()
    called = {"n": 0}

    def fake_tracked(cwd, base_branch):
        called["n"] += 1
        return ["app/live.py"]

    state: dict[str, Any] = {"implement_tracked_baseline": ["app/persisted.py"]}
    result = base.get_or_set_tracked_baseline(state, tmp_path, "main", _tracked_fn=fake_tracked)

    assert result == {"app/persisted.py"}
    assert state["implement_tracked_baseline"] == ["app/persisted.py"]  # unchanged
    assert called["n"] == 0


def test_get_or_set_tracked_baseline_idempotent_under_prior_run_signals(tmp_path):
    """Set-once decision is keyed solely on key presence — no prior-run signals consulted."""
    base = _base()
    called = {"n": 0}

    for implement_round_count, phases_completed, last_run_at in [
        (5, ["plan_outcome", "implement"], "2026-01-01T00:00:00Z"),
        (0, [], None),
        (1, ["implement"], "2026-06-27T00:00:00Z"),
    ]:
        state: dict[str, Any] = {
            "implement_tracked_baseline": ["app/fixed.py"],
            "implement_round_count": implement_round_count,
            "phases_completed": phases_completed,
            "verification": {"last_run_at": last_run_at},
        }
        result = base.get_or_set_tracked_baseline(
            state, tmp_path, "main", _tracked_fn=lambda *_: called.__setitem__("n", called["n"] + 1) or ["app/live.py"]
        )
        assert result == {"app/fixed.py"}

    assert called["n"] == 0


# =============================================================================
# Out-of-scope floor — fail closed, NO persisted baseline (PR-001 fix)
# =============================================================================


def test_out_of_scope_tracked_fails_closed_no_baseline_agent_pair(monkeypatch, tmp_path):
    """agent-pair: out-of-scope tracked path → error, baseline absent on disk, worker not called."""
    impl = _impl()
    repo, task_dir = _make_repo(tmp_path)

    # Tracked file OUTSIDE app/ and tests/
    outside = repo / "README.md"
    outside.write_text("changed\n", encoding="utf-8")
    _git(repo, "add", "README.md")

    invoked = {"yes": False}

    def invoke(**kwargs):
        invoked["yes"] = True
        return {"returncode": 0, "stdout": "", "stderr": ""}

    monkeypatch.setattr(impl, "repo_root", lambda: repo)
    monkeypatch.setattr(impl, "project_config", lambda: _PROJ)
    monkeypatch.setattr(impl, "get_worker_adapter", lambda state: SimpleNamespace(invoke=invoke))

    st = _state()
    (task_dir / "state.json").write_text(json.dumps(st), encoding="utf-8")

    result = impl._run_agent_pair(task_dir, st)

    assert result["status"] == "error"
    assert "README.md" in result["feedback"]
    assert "commit or stash" in result["feedback"]
    assert "fatal:" not in result["feedback"]
    assert invoked["yes"] is False

    # PR-001 fix: baseline must NOT be on disk
    persisted = json.loads((task_dir / "state.json").read_text(encoding="utf-8"))
    assert "implement_tracked_baseline" not in persisted


def test_out_of_scope_tracked_fails_closed_no_baseline_tdd(monkeypatch, tmp_path):
    """tdd: out-of-scope tracked path → error, baseline absent on disk, worker not called."""
    impl = _impl()
    repo, task_dir = _make_repo(tmp_path)

    outside = repo / "README.md"
    outside.write_text("changed\n", encoding="utf-8")
    _git(repo, "add", "README.md")

    invoked = {"yes": False}
    _wire_tdd(impl, monkeypatch, repo, on_invoke=lambda: invoked.__setitem__("yes", True))

    st = {"task_id": "task-001", "mode": "tdd", "base_branch": "main", "verification": {"commands": []}}
    (task_dir / "state.json").write_text(json.dumps(st), encoding="utf-8")

    result = impl.run(task_dir, st)

    assert result["status"] == "error"
    assert "README.md" in result["feedback"]
    assert "commit or stash" in result["feedback"]
    assert "fatal:" not in result["feedback"]
    assert invoked["yes"] is False

    persisted = json.loads((task_dir / "state.json").read_text(encoding="utf-8"))
    assert "implement_tracked_baseline" not in persisted


def test_pr001_self_lock_clean_rerun_after_stash_agent_pair(monkeypatch, tmp_path):
    """PR-001 self-lock fix: after stashing out-of-scope WIP, clean re-run succeeds."""
    impl = _impl()
    repo, task_dir = _make_repo(tmp_path)

    # First: out-of-scope tracked file present
    outside = repo / "README.md"
    outside.write_text("changed\n", encoding="utf-8")
    _git(repo, "add", "README.md")

    monkeypatch.setattr(impl, "repo_root", lambda: repo)
    monkeypatch.setattr(impl, "project_config", lambda: _PROJ)
    monkeypatch.setattr(
        impl,
        "get_worker_adapter",
        lambda state: SimpleNamespace(invoke=lambda **kw: {"returncode": 0, "stdout": "done", "stderr": ""}),
    )
    monkeypatch.setattr(
        impl,
        "_run_verification_commands",
        lambda cwd, commands, project_verify_command=None, allowlist=None: (0, "ok\n"),
    )

    st = _state()
    (task_dir / "state.json").write_text(json.dumps(st), encoding="utf-8")

    result1 = impl._run_agent_pair(task_dir, st)
    assert result1["status"] == "error"

    # Simulate operator stashing: unstage README.md, then remove the now-untracked
    # file (it was never committed, so `git checkout` has no blob to restore).
    _git(repo, "restore", "--staged", "README.md")
    (repo / "README.md").unlink()

    # Re-run with the same state (implement_tracked_baseline absent — clean re-run)
    st2 = json.loads((task_dir / "state.json").read_text(encoding="utf-8"))
    assert "implement_tracked_baseline" not in st2

    result2 = impl._run_agent_pair(task_dir, st2)
    assert result2["status"] == "approved", result2["feedback"]


# =============================================================================
# Out-of-scope floor uses FRESH probe, not stored baseline
# =============================================================================


def test_out_of_scope_floor_fresh_probe_agent_pair(monkeypatch, tmp_path):
    """Floor checks CURRENT tree even when baseline is already stored (baseline-independent)."""
    impl = _impl()
    repo, task_dir = _make_repo(tmp_path)

    # Seed a 'clean' baseline in state (in-scope paths only)
    st = _state(implement_tracked_baseline=["app/existing.py"])
    (task_dir / "state.json").write_text(json.dumps(st), encoding="utf-8")

    # Now add a freshly-staged out-of-scope change AFTER baseline was set
    outside = repo / "NOTES.md"
    outside.write_text("oops\n", encoding="utf-8")
    _git(repo, "add", "NOTES.md")

    invoked = {"yes": False}
    monkeypatch.setattr(impl, "repo_root", lambda: repo)
    monkeypatch.setattr(impl, "project_config", lambda: _PROJ)
    monkeypatch.setattr(
        impl,
        "get_worker_adapter",
        lambda state: SimpleNamespace(
            invoke=lambda **kw: invoked.__setitem__("yes", True) or {"returncode": 0, "stdout": "", "stderr": ""}
        ),
    )

    result = impl._run_agent_pair(task_dir, st)

    assert result["status"] == "error"
    assert "NOTES.md" in result["feedback"]
    assert invoked["yes"] is False
    # Seeded baseline unchanged
    assert st["implement_tracked_baseline"] == ["app/existing.py"]


def test_out_of_scope_floor_fresh_probe_tdd(monkeypatch, tmp_path):
    """Floor checks CURRENT tree in TDD path too (baseline-independent)."""
    impl = _impl()
    repo, task_dir = _make_repo(tmp_path)

    # Seed a 'clean' baseline
    st = {
        "task_id": "task-001",
        "mode": "tdd",
        "base_branch": "main",
        "verification": {"commands": []},
        "implement_tracked_baseline": ["app/existing.py"],
    }
    (task_dir / "state.json").write_text(json.dumps(st), encoding="utf-8")

    # Freshly-staged out-of-scope change
    outside = repo / "NOTES.md"
    outside.write_text("oops\n", encoding="utf-8")
    _git(repo, "add", "NOTES.md")

    _wire_tdd(impl, monkeypatch, repo)

    result = impl.run(task_dir, st)

    assert result["status"] == "error"
    assert "NOTES.md" in result["feedback"]
    # Seeded baseline unchanged
    assert st["implement_tracked_baseline"] == ["app/existing.py"]


# =============================================================================
# In-scope pre-existing tracked mod NOT attributed to worker (real git)
# =============================================================================


def test_in_scope_preexisting_tracked_not_attributed_agent_pair(monkeypatch, tmp_path):
    """Operator pre-edits a source file; worker is no-op. The pre-edit is NOT swept into
    the commit (subtracted via the baseline), and because it remains uncommitted in
    scope the baseline-INDEPENDENT Layer-1 gate defers the round (status=error) — the
    locked behavior: the operator must commit/stash even in-scope WIP before re-running."""
    impl = _impl()
    repo, task_dir = _make_repo(tmp_path)

    # Commit an existing source file on main, then check out task branch
    _git(repo, "checkout", "main")
    (repo / "app" / "existing.py").write_text("x = 1\n", encoding="utf-8")
    _git(repo, "add", "app/existing.py")
    _git(repo, "commit", "-m", "add existing")

    # Checkout task branch
    _git(repo, "checkout", "-b", "redteam/task-001b")

    # Operator pre-edits existing.py (tracked, in-scope, staged)
    (repo / "app" / "existing.py").write_text("x = 2\n", encoding="utf-8")
    _git(repo, "add", "app/existing.py")

    _wire_agent_pair(impl, monkeypatch, repo)  # worker no-op

    st = _state()
    (task_dir / "state.json").write_text(json.dumps(st), encoding="utf-8")

    result = impl._run_agent_pair(task_dir, st)

    # Floor passes (in-scope); pre-edit captured in baseline → no tracked delta → NOT
    # committed. The leftover in-scope edit then trips Layer 1 (baseline-INDEPENDENT).
    assert result["status"] == "error", result["feedback"]
    assert "app/existing.py" in result["feedback"]
    committed = _committed_paths(repo)
    assert "app/existing.py" not in committed

    # Edit still in worktree (not committed)
    content = (repo / "app" / "existing.py").read_text(encoding="utf-8")
    assert content == "x = 2\n"


def test_in_scope_preexisting_tracked_not_attributed_tdd(monkeypatch, tmp_path):
    """TDD path: operator pre-edits source; worker is no-op. Pre-edit subtracted (not
    committed); the leftover in-scope edit trips the baseline-INDEPENDENT Layer-1 gate so
    the round defers (status=error) — the same locked behavior as the agent-pair path."""
    impl = _impl()
    repo, task_dir = _make_repo(tmp_path)

    _git(repo, "checkout", "main")
    (repo / "app" / "existing.py").write_text("x = 1\n", encoding="utf-8")
    _git(repo, "add", "app/existing.py")
    _git(repo, "commit", "-m", "add existing")
    _git(repo, "checkout", "-b", "redteam/task-001b")

    (repo / "app" / "existing.py").write_text("x = 2\n", encoding="utf-8")
    _git(repo, "add", "app/existing.py")

    _wire_tdd(impl, monkeypatch, repo)

    st = {"task_id": "task-001", "mode": "tdd", "base_branch": "main", "verification": {"commands": []}}
    (task_dir / "state.json").write_text(json.dumps(st), encoding="utf-8")

    result = impl.run(task_dir, st)

    assert result["status"] == "error", result["feedback"]
    assert "app/existing.py" in result["feedback"]
    committed = _committed_paths(repo)
    assert "app/existing.py" not in committed
    assert (repo / "app" / "existing.py").read_text(encoding="utf-8") == "x = 2\n"


# =============================================================================
# Worker's own tracked changes still land (real git)
# =============================================================================


def test_worker_tracked_changes_still_land_agent_pair(monkeypatch, tmp_path):
    """Clean tree: worker's tracked+new changes land in committed range."""
    impl = _impl()
    repo, task_dir = _make_repo(tmp_path)

    _git(repo, "checkout", "main")
    (repo / "app" / "existing.py").write_text("x = 1\n", encoding="utf-8")
    _git(repo, "add", "app/existing.py")
    _git(repo, "commit", "-m", "add existing")
    _git(repo, "checkout", "-b", "redteam/task-001b")

    def invoke(**kwargs):
        (repo / "app" / "existing.py").write_text("x = 2\n", encoding="utf-8")
        (repo / "tests" / "test_new.py").write_text("def test_x(): pass\n", encoding="utf-8")
        return {"returncode": 0, "stdout": "done", "stderr": ""}

    monkeypatch.setattr(impl, "repo_root", lambda: repo)
    monkeypatch.setattr(impl, "project_config", lambda: _PROJ)
    monkeypatch.setattr(impl, "get_worker_adapter", lambda state: SimpleNamespace(invoke=invoke))
    monkeypatch.setattr(
        impl,
        "_run_verification_commands",
        lambda cwd, commands, project_verify_command=None, allowlist=None: (0, "ok\n"),
    )

    st = _state()
    (task_dir / "state.json").write_text(json.dumps(st), encoding="utf-8")

    result = impl._run_agent_pair(task_dir, st)

    assert result["status"] == "approved", result["feedback"]
    committed = _committed_paths(repo)
    assert "app/existing.py" in committed
    assert "tests/test_new.py" in committed
    patch = (task_dir / "impl_diff.patch").read_text(encoding="utf-8")
    assert "app/existing.py" in patch
    assert "tests/test_new.py" in patch


def test_worker_tracked_changes_still_land_tdd(monkeypatch, tmp_path):
    """TDD path: worker's tracked+new changes land in committed range."""
    impl = _impl()
    repo, task_dir = _make_repo(tmp_path)

    _git(repo, "checkout", "main")
    (repo / "app" / "existing.py").write_text("x = 1\n", encoding="utf-8")
    _git(repo, "add", "app/existing.py")
    _git(repo, "commit", "-m", "add existing")
    _git(repo, "checkout", "-b", "redteam/task-001b")

    def invoke(**kwargs):
        (repo / "app" / "existing.py").write_text("x = 2\n", encoding="utf-8")
        (repo / "tests" / "test_new.py").write_text("def test_x(): pass\n", encoding="utf-8")
        return {"returncode": 0, "stdout": "done", "stderr": ""}

    _wire_tdd(impl, monkeypatch, repo, on_invoke=None)
    monkeypatch.setattr(impl, "get_worker_adapter", lambda state: SimpleNamespace(invoke=invoke))

    st = {"task_id": "task-001", "mode": "tdd", "base_branch": "main", "verification": {"commands": []}}
    (task_dir / "state.json").write_text(json.dumps(st), encoding="utf-8")

    result = impl.run(task_dir, st)

    assert result["status"] == "approved", result["feedback"]
    committed = _committed_paths(repo)
    assert "app/existing.py" in committed
    assert "tests/test_new.py" in committed


# =============================================================================
# Set-once / resume: tracked baseline survives interrupted round
# =============================================================================


def test_durable_preworker_flush_tracked_agent_pair(monkeypatch, tmp_path):
    """agent-pair: tracked baseline persisted before worker; survives non-zero exit; reused on R2."""
    impl = _impl()
    repo, task_dir = _make_repo(tmp_path)

    _git(repo, "checkout", "main")
    (repo / "app" / "existing.py").write_text("x = 1\n", encoding="utf-8")
    _git(repo, "add", "app/existing.py")
    _git(repo, "commit", "-m", "add existing")
    _git(repo, "checkout", "-b", "redteam/task-001b")

    # Operator pre-edits existing.py (in-scope tracked)
    (repo / "app" / "existing.py").write_text("x = 2\n", encoding="utf-8")

    monkeypatch.setattr(impl, "repo_root", lambda: repo)
    monkeypatch.setattr(impl, "project_config", lambda: _PROJ)
    monkeypatch.setattr(
        impl,
        "_run_verification_commands",
        lambda cwd, cmds, project_verify_command=None, allowlist=None: (0, "ok\n"),
    )

    # Round 1: worker crashes after persist_state already wrote baseline
    def invoke_round1(**kwargs):
        return {"returncode": 1, "stdout": "", "stderr": "crash"}

    monkeypatch.setattr(impl, "get_worker_adapter", lambda state: SimpleNamespace(invoke=invoke_round1))

    st = _state()
    (task_dir / "state.json").write_text(json.dumps(st), encoding="utf-8")

    result1 = impl._run_agent_pair(task_dir, st)
    assert result1["status"] == "error"
    assert "exited non-zero" in result1["feedback"]

    # baseline must be on disk and include the operator pre-edit
    persisted = json.loads((task_dir / "state.json").read_text(encoding="utf-8"))
    assert "implement_tracked_baseline" in persisted
    assert isinstance(persisted["implement_tracked_baseline"], list)
    assert "app/existing.py" in persisted["implement_tracked_baseline"]

    # Round 2: worker makes a NEW tracked change; pre-edit must NOT be committed.
    # The R1 baseline (with app/existing.py) is REUSED — get_or_set_tracked_baseline
    # returns the stored value without re-snapshotting — so existing.py is still
    # subtracted while the worker's new_feature.py lands in the WIP commit. The leftover
    # in-scope pre-edit then trips the baseline-INDEPENDENT Layer-1 gate (status=error);
    # the set-once guarantee is in the committed paths, not the final status.
    def invoke_round2(**kwargs):
        (repo / "app" / "new_feature.py").write_text("y = 3\n", encoding="utf-8")
        return {"returncode": 0, "stdout": "done", "stderr": ""}

    monkeypatch.setattr(impl, "get_worker_adapter", lambda state: SimpleNamespace(invoke=invoke_round2))

    st2 = json.loads((task_dir / "state.json").read_text(encoding="utf-8"))
    result2 = impl._run_agent_pair(task_dir, st2)
    assert result2["status"] == "error", result2["feedback"]
    assert "app/existing.py" in result2["feedback"]

    committed = _committed_paths(repo)
    assert "app/new_feature.py" in committed  # worker's R2 change lands in the WIP commit
    assert "app/existing.py" not in committed  # pre-edit NOT attributed to worker (baseline reused)


def test_durable_preworker_flush_tracked_tdd(monkeypatch, tmp_path):
    """TDD path: tracked baseline persisted before worker; survives crash; reused on R2."""
    impl = _impl()
    repo, task_dir = _make_repo(tmp_path)

    _git(repo, "checkout", "main")
    (repo / "app" / "existing.py").write_text("x = 1\n", encoding="utf-8")
    _git(repo, "add", "app/existing.py")
    _git(repo, "commit", "-m", "add existing")
    _git(repo, "checkout", "-b", "redteam/task-001b")

    (repo / "app" / "existing.py").write_text("x = 2\n", encoding="utf-8")

    import config as _config

    tdd_proj = SimpleNamespace(
        source_dirs=["app/"],
        test_dir="tests/",
        context_file="c",
        base_branch="main",
        verify_command="true",
        verification_allowlist=("true",),
    )
    monkeypatch.setattr(_config, "load_config", lambda *_a, **_k: SimpleNamespace(project=tdd_proj))
    monkeypatch.setattr(impl, "repo_root", lambda: repo)
    monkeypatch.setattr(impl, "project_config", lambda: tdd_proj)
    monkeypatch.setattr(impl, "_run_verify_sh", lambda cwd, argv: (0, "ok\n"))

    def invoke_crash(**kwargs):
        return {"returncode": 1, "stdout": "", "stderr": "crash"}

    monkeypatch.setattr(impl, "get_worker_adapter", lambda state: SimpleNamespace(invoke=invoke_crash))

    st = {"task_id": "task-001", "mode": "tdd", "base_branch": "main", "verification": {"commands": []}}
    (task_dir / "state.json").write_text(json.dumps(st), encoding="utf-8")

    result1 = impl.run(task_dir, st)
    assert result1["status"] == "error"

    persisted = json.loads((task_dir / "state.json").read_text(encoding="utf-8"))
    assert "implement_tracked_baseline" in persisted
    assert "app/existing.py" in persisted["implement_tracked_baseline"]

    # Round 2 reuses the persisted baseline (no re-snapshot): existing.py is subtracted
    # while new_feature.py lands in the WIP commit; the leftover in-scope pre-edit then
    # trips the baseline-INDEPENDENT Layer-1 gate (status=error), the locked behavior.
    def invoke_r2(**kwargs):
        (repo / "app" / "new_feature.py").write_text("y = 3\n", encoding="utf-8")
        return {"returncode": 0, "stdout": "done", "stderr": ""}

    monkeypatch.setattr(impl, "get_worker_adapter", lambda state: SimpleNamespace(invoke=invoke_r2))

    st2 = json.loads((task_dir / "state.json").read_text(encoding="utf-8"))
    result2 = impl.run(task_dir, st2)
    assert result2["status"] == "error", result2["feedback"]
    assert "app/existing.py" in result2["feedback"]

    committed = _committed_paths(repo)
    assert "app/new_feature.py" in committed  # worker's R2 change lands in the WIP commit
    assert "app/existing.py" not in committed  # baseline reused → pre-edit subtracted


# =============================================================================
# Clean-tree run unchanged (real git)
# =============================================================================


def test_clean_tree_run_unchanged_agent_pair(monkeypatch, tmp_path):
    """Clean tree: baseline is empty, floor passes, worker's changes land as before fix."""
    impl = _impl()
    repo, task_dir = _make_repo(tmp_path)

    _git(repo, "checkout", "main")
    (repo / "app" / "existing.py").write_text("x = 1\n", encoding="utf-8")
    _git(repo, "add", "app/existing.py")
    _git(repo, "commit", "-m", "add existing")
    _git(repo, "checkout", "-b", "redteam/task-001b")

    def invoke(**kwargs):
        (repo / "app" / "existing.py").write_text("x = 99\n", encoding="utf-8")
        (repo / "tests" / "test_clean.py").write_text("def test_clean(): pass\n", encoding="utf-8")
        return {"returncode": 0, "stdout": "done", "stderr": ""}

    monkeypatch.setattr(impl, "repo_root", lambda: repo)
    monkeypatch.setattr(impl, "project_config", lambda: _PROJ)
    monkeypatch.setattr(impl, "get_worker_adapter", lambda state: SimpleNamespace(invoke=invoke))
    monkeypatch.setattr(
        impl,
        "_run_verification_commands",
        lambda cwd, commands, project_verify_command=None, allowlist=None: (0, "ok\n"),
    )

    st = _state()
    (task_dir / "state.json").write_text(json.dumps(st), encoding="utf-8")

    result = impl._run_agent_pair(task_dir, st)
    assert result["status"] == "approved", result["feedback"]

    committed = _committed_paths(repo)
    assert "app/existing.py" in committed
    assert "tests/test_clean.py" in committed

    persisted = json.loads((task_dir / "state.json").read_text(encoding="utf-8"))
    assert persisted.get("implement_tracked_baseline") == []  # empty on clean tree


def test_clean_tree_run_unchanged_tdd(monkeypatch, tmp_path):
    """TDD: clean tree, worker's changes land normally."""
    impl = _impl()
    repo, task_dir = _make_repo(tmp_path)

    _git(repo, "checkout", "main")
    (repo / "app" / "existing.py").write_text("x = 1\n", encoding="utf-8")
    _git(repo, "add", "app/existing.py")
    _git(repo, "commit", "-m", "add existing")
    _git(repo, "checkout", "-b", "redteam/task-001b")

    def invoke(**kwargs):
        (repo / "app" / "existing.py").write_text("x = 99\n", encoding="utf-8")
        return {"returncode": 0, "stdout": "done", "stderr": ""}

    _wire_tdd(impl, monkeypatch, repo)
    monkeypatch.setattr(impl, "get_worker_adapter", lambda state: SimpleNamespace(invoke=invoke))

    st = {"task_id": "task-001", "mode": "tdd", "base_branch": "main", "verification": {"commands": []}}
    (task_dir / "state.json").write_text(json.dumps(st), encoding="utf-8")

    result = impl.run(task_dir, st)
    assert result["status"] == "approved", result["feedback"]
    assert "app/existing.py" in _committed_paths(repo)


# =============================================================================
# Pinned base usage — no live config bleed
# =============================================================================


def test_pinned_base_no_live_config_bleed(tmp_path):
    """Helper uses the caller-supplied base_branch string, not live config."""
    base = _base()

    calls: list[str] = []

    def fake_probe(cwd, branch: str) -> list[str]:
        calls.append(branch)
        return []

    state: dict[str, Any] = {}
    base.get_or_set_tracked_baseline(state, tmp_path, "v1-stable", _tracked_fn=fake_probe)

    assert calls == ["v1-stable"]


# =============================================================================
# _commit_worker_diff before_tracked passthrough (via state["implement_tracked_baseline"])
# =============================================================================


def test_commit_worker_diff_before_tracked_excludes_preexisting(monkeypatch, tmp_path):
    """_commit_worker_diff takes before_tracked as an EXPLICIT argument (outcome.md:
    arg passthrough): a path in the passed set is excluded from the commit even when it
    currently shows in _tracked_changed_paths, while a path NOT in the set still lands."""
    impl = _impl()
    repo, task_dir = _make_repo(tmp_path)

    _git(repo, "checkout", "main")
    (repo / "app" / "pre.py").write_text("x = 1\n", encoding="utf-8")
    (repo / "app" / "worker.py").write_text("w = 0\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "seed")
    _git(repo, "checkout", "-b", "redteam/task-001b")

    # Operator staged a tracked change to pre.py before worker
    (repo / "app" / "pre.py").write_text("x = 2\n", encoding="utf-8")

    st = _state()

    # Worker now also modifies worker.py (different file)
    (repo / "app" / "worker.py").write_text("w = 99\n", encoding="utf-8")

    # Call _commit_worker_diff directly with an EXPLICIT before_tracked set — NOT read
    # from state (the runner passes the same in-memory set get_or_set_tracked_baseline
    # returned). app/pre.py is in the set; app/worker.py is not.
    before_untracked: set[str] = set()
    impl._commit_worker_diff(task_dir, st, repo, before_untracked, {"app/pre.py"})

    committed = _committed_paths(repo)
    assert "app/worker.py" in committed  # worker's own change lands (NOT in before_tracked)
    assert "app/pre.py" not in committed  # operator pre-edit excluded (in before_tracked)


# =============================================================================
# Mode-neutrality: single definition for _tracked_changed_paths and helper
# =============================================================================


def test_mode_neutrality_single_definition():
    """implement.py imports _tracked_changed_paths and get_or_set_tracked_baseline from _base."""
    import phase_runners._base as _pbase

    impl = _impl()
    assert impl._tracked_changed_paths is _pbase._tracked_changed_paths
    assert impl.get_or_set_tracked_baseline is _pbase.get_or_set_tracked_baseline


# =============================================================================
# No stderr leakage / fail-closed on probe failure — both paths
# =============================================================================


def test_fail_closed_probe_failure_no_stderr_leakage_agent_pair(monkeypatch, tmp_path):
    """agent-pair: probe RuntimeError → status=error, no fatal:, baseline absent."""
    impl = _impl()
    repo, task_dir = _make_repo(tmp_path)

    monkeypatch.setattr(impl, "repo_root", lambda: repo)
    monkeypatch.setattr(impl, "project_config", lambda: _PROJ)
    invoked = {"yes": False}
    monkeypatch.setattr(
        impl,
        "get_worker_adapter",
        lambda state: SimpleNamespace(
            invoke=lambda **kw: invoked.__setitem__("yes", True) or {"returncode": 0, "stdout": "", "stderr": ""}
        ),
    )

    # Make _tracked_changed_paths raise RuntimeError (simulate git failure)
    def bad_tracked(cwd, base_branch):
        raise RuntimeError("git invocation failed (exit 128)")

    monkeypatch.setattr(impl, "_tracked_changed_paths", bad_tracked)

    st = _state()
    (task_dir / "state.json").write_text(json.dumps(st), encoding="utf-8")

    result = impl._run_agent_pair(task_dir, st)

    assert result["status"] == "error"
    assert "fatal:" not in result["feedback"]
    assert invoked["yes"] is False

    persisted = json.loads((task_dir / "state.json").read_text(encoding="utf-8"))
    assert "implement_tracked_baseline" not in persisted


def test_fail_closed_probe_failure_no_stderr_leakage_tdd(monkeypatch, tmp_path):
    """tdd: probe RuntimeError → status=error, no fatal:, baseline absent."""
    impl = _impl()
    repo, task_dir = _make_repo(tmp_path)

    _wire_tdd(impl, monkeypatch, repo)

    def bad_tracked(cwd, base_branch):
        raise RuntimeError("git invocation failed (exit 128)")

    monkeypatch.setattr(impl, "_tracked_changed_paths", bad_tracked)

    st = {"task_id": "task-001", "mode": "tdd", "base_branch": "main", "verification": {"commands": []}}
    (task_dir / "state.json").write_text(json.dumps(st), encoding="utf-8")

    result = impl.run(task_dir, st)

    assert result["status"] == "error"
    assert "fatal:" not in result["feedback"]

    persisted = json.loads((task_dir / "state.json").read_text(encoding="utf-8"))
    assert "implement_tracked_baseline" not in persisted
