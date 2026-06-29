"""Regression tests for the #117 cross-run trust-root floor.

Covers:
- Adversarial cross-run: poisoned untracked baseline with file already on disk
- Adversarial cross-run: poisoned untracked baseline with file absent at entry (PR-001 r3)
- Adversarial cross-run: poisoned tracked baseline with file absent at entry
- Adversarial cross-run via TDD path
- Pre-#112 in-flight migration window (no baseline keys, outside-scope file present)
- Healthy cross-run resume: clean baselines pass, stored lists unchanged
- Healthy in-process multi-round: round 2 skips the floor
- Fresh TDD task: write_test commits in-scope test first → no false positive
- task_dir scratch exempt from both live floor and stored-baseline-contents floor
- In-scope untracked NOT flagged by the floor
- Feedback names all offending paths (de-duplicated, sorted); worker not invoked on failure
- Contract preservation: _commit_worker_diff args, Layer-1 baseline-independent, Layer-2 task_dir-exempt
- No HMAC/env-scrub/load_state mutation
- Same-process sibling-task isolation (goal-mode)
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any


# ---------- engine module loaders ----------


def _impl():
    import _engine

    return _engine.implement()


def _orch():
    import _engine

    return _engine.orchestrator()


# ---------- shared fixtures ----------

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


def _wire_agent_pair(impl, monkeypatch, repo: Path, *, invoke_fn=None) -> list[dict]:
    """Wire agent-pair stubs; returns a list that records each invoke call."""
    calls: list[dict] = []
    monkeypatch.setattr(impl, "repo_root", lambda: repo)
    monkeypatch.setattr(impl, "project_config", lambda: _PROJ)

    def invoke(**kwargs: Any) -> dict[str, Any]:
        calls.append(dict(kwargs))
        if invoke_fn is not None:
            invoke_fn()
        return {"returncode": 0, "stdout": "done", "stderr": ""}

    monkeypatch.setattr(impl, "get_worker_adapter", lambda state: SimpleNamespace(invoke=invoke))
    monkeypatch.setattr(
        impl,
        "_run_verification_commands",
        lambda cwd, commands, project_verify_command=None, allowlist=None: (0, "ok\n"),
    )
    return calls


def _wire_tdd(impl, monkeypatch, repo: Path, *, invoke_fn=None) -> list[dict]:
    """Wire TDD stubs; returns a list that records each invoke call."""
    import config as _config

    calls: list[dict] = []
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
        calls.append(dict(kwargs))
        if invoke_fn is not None:
            invoke_fn()
        return {"returncode": 0, "stdout": "done", "stderr": ""}

    monkeypatch.setattr(impl, "get_worker_adapter", lambda state: SimpleNamespace(invoke=invoke))
    return calls


# =============================================================================
# 1. Adversarial cross-run: file ALREADY on disk (agent-pair)
# =============================================================================


def test_adversarial_untracked_baseline_file_on_disk_agent_pair(monkeypatch, tmp_path):
    """Worker poisons implement_untracked_baseline with an outside-scope path
    that is ALREADY present in the worktree. Fresh process (marker cleared)
    → status='error', worker NOT invoked, no re-stamp of baseline.
    """
    impl = _impl()
    repo, task_dir = _make_repo(tmp_path)

    # Worker-created outside-scope file survives on disk
    (repo / "scratch").mkdir()
    (repo / "scratch" / "leaked.txt").write_text("secret\n", encoding="utf-8")

    # Poisoned baseline on disk
    st = _state(
        implement_untracked_baseline=["scratch/leaked.txt"],
        phases_completed=["plan_outcome"],
    )
    (task_dir / "state.json").write_text(json.dumps(st), encoding="utf-8")

    invoke_calls: list[dict] = _wire_agent_pair(impl, monkeypatch, repo)

    # Simulate fresh process by ensuring task_dir is NOT in the marker
    impl._trusted_task_dirs.discard(task_dir)

    result = impl._run_agent_pair(task_dir, st)

    assert result["status"] == "error", result["feedback"]
    assert "scratch/leaked.txt" in result["feedback"]
    # Worker must NOT have been invoked
    assert invoke_calls == [], "worker was invoked despite floor failure"
    # Baseline keys on disk unchanged (no re-stamp)
    on_disk = json.loads((task_dir / "state.json").read_text(encoding="utf-8"))
    assert on_disk.get("implement_untracked_baseline") == ["scratch/leaked.txt"]


# =============================================================================
# 2. Adversarial cross-run: file ABSENT at entry (PR-001 round-3 variant)
# =============================================================================


def test_adversarial_untracked_baseline_absent_at_entry_agent_pair(monkeypatch, tmp_path):
    """Worker poisons implement_untracked_baseline with scratch/secret.txt
    while that path is ABSENT from the worktree at restart.
    Stored-baseline-contents floor fires → status='error'; worker NOT invoked.
    """
    impl = _impl()
    repo, task_dir = _make_repo(tmp_path)

    # scratch/secret.txt does NOT exist yet on disk
    st = _state(
        implement_untracked_baseline=["scratch/secret.txt"],
        phases_completed=["plan_outcome"],
    )
    (task_dir / "state.json").write_text(json.dumps(st), encoding="utf-8")

    invoke_calls: list[dict] = _wire_agent_pair(impl, monkeypatch, repo)
    impl._trusted_task_dirs.discard(task_dir)

    result = impl._run_agent_pair(task_dir, st)

    assert result["status"] == "error", result["feedback"]
    assert "scratch/secret.txt" in result["feedback"]
    assert invoke_calls == [], "worker was invoked despite floor failure"
    # Baseline on disk unchanged
    on_disk = json.loads((task_dir / "state.json").read_text(encoding="utf-8"))
    assert on_disk.get("implement_untracked_baseline") == ["scratch/secret.txt"]


# =============================================================================
# 3. Adversarial cross-run via TRACKED baseline, file ABSENT at entry
# =============================================================================


def test_adversarial_tracked_baseline_absent_at_entry_agent_pair(monkeypatch, tmp_path):
    """Worker poisons implement_tracked_baseline with an outside-scope path
    absent from _tracked_changed_paths at restart.
    Stored-baseline-contents floor fires → status='error'.
    """
    impl = _impl()
    repo, task_dir = _make_repo(tmp_path)

    # outside-scope path in tracked baseline, not actually tracked
    st = _state(
        implement_tracked_baseline=["secret_config/prod.env"],
        phases_completed=["plan_outcome"],
    )
    (task_dir / "state.json").write_text(json.dumps(st), encoding="utf-8")

    invoke_calls: list[dict] = _wire_agent_pair(impl, monkeypatch, repo)
    impl._trusted_task_dirs.discard(task_dir)

    result = impl._run_agent_pair(task_dir, st)

    assert result["status"] == "error", result["feedback"]
    assert "secret_config/prod.env" in result["feedback"]
    assert invoke_calls == [], "worker was invoked despite floor failure"


# =============================================================================
# 4. Adversarial cross-run (TDD path)
# =============================================================================


def test_adversarial_untracked_baseline_absent_at_entry_tdd(monkeypatch, tmp_path):
    """TDD run path: absent-at-entry untracked baseline poison → status='error'."""
    impl = _impl()
    repo, task_dir = _make_repo(tmp_path)

    st = {
        "task_id": "task-001",
        "mode": "tdd",
        "base_branch": "main",
        "verification": {"commands": []},
        "implement_untracked_baseline": ["scratch/secret.txt"],
        "phases_completed": ["plan_outcome"],
    }
    (task_dir / "state.json").write_text(json.dumps(st), encoding="utf-8")

    invoke_calls: list[dict] = _wire_tdd(impl, monkeypatch, repo)
    impl._trusted_task_dirs.discard(task_dir)

    result = impl.run(task_dir, st)

    assert result["status"] == "error", result["feedback"]
    assert "scratch/secret.txt" in result["feedback"]
    assert invoke_calls == [], "worker was invoked despite floor failure"


# =============================================================================
# 5. Pre-#112 in-flight migration window
# =============================================================================


def test_pre112_migration_window_outside_scope_file_present(monkeypatch, tmp_path):
    """State with NO baseline keys + outside-scope untracked file present
    (worker-created before pre-#112 crash). Fresh process → status='error';
    implement_untracked_baseline key remains absent after the call.
    A clean re-run (file gone) succeeds.
    """
    impl = _impl()
    repo, task_dir = _make_repo(tmp_path)

    # Worker-created outside-scope file survived the crash
    (repo / "scratch").mkdir()
    (repo / "scratch" / "worker_output.txt").write_text("data\n", encoding="utf-8")

    # No baseline keys at all — pre-#112 state
    st = _state(phases_completed=["plan_outcome", "implement"])
    (task_dir / "state.json").write_text(json.dumps(st), encoding="utf-8")

    invoke_calls: list[dict] = _wire_agent_pair(impl, monkeypatch, repo)
    impl._trusted_task_dirs.discard(task_dir)

    result = impl._run_agent_pair(task_dir, st)

    assert result["status"] == "error", result["feedback"]
    assert "scratch/worker_output.txt" in result["feedback"]
    assert invoke_calls == [], "worker was invoked despite floor failure"
    # Baseline key must remain absent — set-once snapshot was NOT taken
    on_disk = json.loads((task_dir / "state.json").read_text(encoding="utf-8"))
    assert "implement_untracked_baseline" not in on_disk, "baseline was stamped on failure path"

    # Clean re-run: remove the outside-scope file, fresh state, marker cleared
    (repo / "scratch" / "worker_output.txt").unlink()
    (repo / "scratch").rmdir()
    invoke_calls.clear()
    st2 = _state(phases_completed=["plan_outcome", "implement"])
    (task_dir / "state.json").write_text(json.dumps(st2), encoding="utf-8")
    impl._trusted_task_dirs.discard(task_dir)

    result2 = impl._run_agent_pair(task_dir, st2)
    assert result2["status"] == "approved", result2["feedback"]


# =============================================================================
# 6. Healthy cross-run resume — no false positive
# =============================================================================


def test_healthy_cross_run_resume_no_false_positive(monkeypatch, tmp_path):
    """Key-present clean baselines + clean outside-scope worktree.
    Fresh process first entry: floor passes; stored baselines reused UNCHANGED.
    """
    impl = _impl()
    repo, task_dir = _make_repo(tmp_path)

    # Some in-scope tracked change already captured in baseline
    (repo / "app" / "module.py").write_text("x = 1\n", encoding="utf-8")
    _git(repo, "add", "app/module.py")
    _git(repo, "commit", "-m", "wip")

    # Stored baselines: in-scope only
    stored_untracked: list[str] = []
    stored_tracked: list[str] = ["app/module.py"]
    st = _state(
        implement_untracked_baseline=list(stored_untracked),
        implement_tracked_baseline=list(stored_tracked),
        phases_completed=["plan_outcome"],
    )
    (task_dir / "state.json").write_text(json.dumps(st), encoding="utf-8")

    invoke_calls: list[dict] = _wire_agent_pair(impl, monkeypatch, repo)
    impl._trusted_task_dirs.discard(task_dir)

    result = impl._run_agent_pair(task_dir, st)

    assert result["status"] == "approved", result["feedback"]
    # Worker was invoked (floor passed)
    assert len(invoke_calls) == 1

    # Stored baselines unchanged
    on_disk = json.loads((task_dir / "state.json").read_text(encoding="utf-8"))
    assert on_disk["implement_untracked_baseline"] == stored_untracked
    assert on_disk["implement_tracked_baseline"] == stored_tracked


# =============================================================================
# 7. Healthy in-process multi-round — no false positive
# =============================================================================


def test_healthy_in_process_multi_round_floor_skipped_on_round2(monkeypatch, tmp_path):
    """Within ONE process, round 1 stamps the marker; round 2 skips the floor.
    Verify the floor helper is NOT called on round 2.
    """
    impl = _impl()
    repo, task_dir = _make_repo(tmp_path)

    # No pre-existing baseline; outside-scope surface clean
    st = _state()
    (task_dir / "state.json").write_text(json.dumps(st), encoding="utf-8")

    floor_call_count = {"n": 0}
    original_floor = impl._cross_run_trust_root_floor

    def counting_floor(*args, **kwargs):
        floor_call_count["n"] += 1
        return original_floor(*args, **kwargs)

    monkeypatch.setattr(impl, "_cross_run_trust_root_floor", counting_floor)
    _wire_agent_pair(impl, monkeypatch, repo)
    impl._trusted_task_dirs.discard(task_dir)

    # Round 1: marker not set → floor runs
    result1 = impl._run_agent_pair(task_dir, st)
    assert result1["status"] == "approved", result1["feedback"]
    assert floor_call_count["n"] == 1
    assert task_dir in impl._trusted_task_dirs

    # Round 2: marker is set → floor is NOT called
    st2 = json.loads((task_dir / "state.json").read_text(encoding="utf-8"))
    result2 = impl._run_agent_pair(task_dir, st2)
    assert result2["status"] == "approved", result2["feedback"]
    # Floor was NOT called a second time
    assert floor_call_count["n"] == 1


# =============================================================================
# 8. Fresh TDD task — no false positive
# =============================================================================


def test_fresh_tdd_task_no_false_positive(monkeypatch, tmp_path):
    """write_test commits the test inside test_dir first (tracked, in-scope),
    then implement runs in the same process. Floor passes, marker stamped,
    round returns 'approved' (not 'error' for a baseline/floor reason).
    """
    impl = _impl()
    repo, task_dir = _make_repo(tmp_path)

    # Simulate write_test: test was committed already
    (repo / "tests" / "test_feature.py").write_text("def test_x(): pass\n", encoding="utf-8")
    _git(repo, "add", "tests/test_feature.py")
    _git(repo, "commit", "-m", "test(task): add test_feature")

    # No baseline keys yet, clean outside-scope surface
    st = {
        "task_id": "task-001",
        "mode": "tdd",
        "base_branch": "main",
        "verification": {"commands": []},
    }
    (task_dir / "state.json").write_text(json.dumps(st), encoding="utf-8")

    invoke_calls: list[dict] = _wire_tdd(impl, monkeypatch, repo)
    impl._trusted_task_dirs.discard(task_dir)

    result = impl.run(task_dir, st)

    # Must NOT be 'error' for a baseline/floor reason
    assert result["status"] in ("approved", "changes_requested"), result["feedback"]
    assert "cross-run trust-root floor" not in result["feedback"]
    assert len(invoke_calls) == 1
    assert task_dir in impl._trusted_task_dirs


# =============================================================================
# 9. task_dir scratch exempt
# =============================================================================


def test_task_dir_scratch_exempt_live_floor(monkeypatch, tmp_path):
    """Untracked file under task_dir does NOT trip the live floor."""
    impl = _impl()
    repo, task_dir = _make_repo(tmp_path)

    # An operator note / harness artifact under task_dir
    (task_dir / "verification.log").write_text("log\n", encoding="utf-8")
    (task_dir / "impl_diff.patch").write_text("diff\n", encoding="utf-8")

    st = _state()
    (task_dir / "state.json").write_text(json.dumps(st), encoding="utf-8")

    _wire_agent_pair(impl, monkeypatch, repo)
    impl._trusted_task_dirs.discard(task_dir)

    result = impl._run_agent_pair(task_dir, st)

    assert result["status"] == "approved", result["feedback"]
    assert "cross-run trust-root floor" not in result["feedback"]


def test_task_dir_entry_in_baseline_exempt_stored_contents_floor(monkeypatch, tmp_path):
    """A stored baseline entry under task_dir POSIX-prefix does NOT trip the
    stored-baseline-contents floor.
    """
    impl = _impl()
    repo, task_dir = _make_repo(tmp_path)

    # task_dir relative path as a baseline entry (harness artifact)
    try:
        task_rel = task_dir.resolve().relative_to(repo.resolve()).as_posix()
    except ValueError:
        task_rel = ".redteam/batches/b/tasks/task-001"

    task_artifact = task_rel + "/verification.log"
    st = _state(
        implement_untracked_baseline=[task_artifact],
        phases_completed=["plan_outcome"],
    )
    (task_dir / "state.json").write_text(json.dumps(st), encoding="utf-8")

    _wire_agent_pair(impl, monkeypatch, repo)
    impl._trusted_task_dirs.discard(task_dir)

    result = impl._run_agent_pair(task_dir, st)

    assert result["status"] == "approved", result["feedback"]
    assert "cross-run trust-root floor" not in result["feedback"]


# =============================================================================
# 10. In-scope untracked NOT flagged by the floor
# =============================================================================


def test_in_scope_untracked_not_flagged_by_floor(monkeypatch, tmp_path):
    """Untracked file inside source_dirs does NOT trip the new floor.
    It flows through the existing #112 Layer-1 / _commit_worker_diff paths.
    """
    impl = _impl()
    repo, task_dir = _make_repo(tmp_path)

    # In-scope untracked file (inside app/)
    (repo / "app" / "new_module.py").write_text("x = 1\n", encoding="utf-8")

    # Key-absent state — fresh entry
    st = _state()
    (task_dir / "state.json").write_text(json.dumps(st), encoding="utf-8")

    _wire_agent_pair(impl, monkeypatch, repo)
    impl._trusted_task_dirs.discard(task_dir)

    result = impl._run_agent_pair(task_dir, st)

    # The floor must NOT fire for in-scope files (they are handled by Layer-1/commit)
    # The run may succeed or fail for other reasons (Layer-1) but not "cross-run trust-root"
    assert "cross-run trust-root floor" not in result["feedback"]


# =============================================================================
# 11. Feedback names all offending paths (de-duplicated, sorted)
# =============================================================================


def test_feedback_names_all_offending_paths_deduped_sorted(monkeypatch, tmp_path):
    """Paths from BOTH the live floor AND the stored-baseline-contents floor
    appear verbatim, de-duplicated, sorted in the returned feedback.
    """
    impl = _impl()
    repo, task_dir = _make_repo(tmp_path)

    # Two outside-scope files on disk (live floor)
    (repo / "scratch").mkdir()
    (repo / "scratch" / "b_file.txt").write_text("b\n", encoding="utf-8")
    (repo / "scratch" / "a_file.txt").write_text("a\n", encoding="utf-8")

    # Baseline also contains an additional path not on disk (stored-baseline-contents floor)
    # Plus one that IS on disk (overlap — must be de-duplicated)
    st = _state(
        implement_untracked_baseline=["scratch/a_file.txt", "outside/z_file.dat"],
        phases_completed=["plan_outcome"],
    )
    (task_dir / "state.json").write_text(json.dumps(st), encoding="utf-8")

    invoke_calls: list[dict] = _wire_agent_pair(impl, monkeypatch, repo)
    impl._trusted_task_dirs.discard(task_dir)

    result = impl._run_agent_pair(task_dir, st)

    assert result["status"] == "error", result["feedback"]
    fb = result["feedback"]

    # All three unique offending paths named
    assert "scratch/a_file.txt" in fb
    assert "scratch/b_file.txt" in fb
    assert "outside/z_file.dat" in fb

    # Worker NOT invoked
    assert invoke_calls == []

    # Paths appear sorted in the feedback string (extract from "Offending paths: ..." part)
    idx_a = fb.index("scratch/a_file.txt")
    idx_b = fb.index("scratch/b_file.txt")
    idx_z = fb.index("outside/z_file.dat")
    # sorted order: outside/ < scratch/a < scratch/b
    assert idx_z < idx_a < idx_b, f"paths not sorted: z={idx_z}, a={idx_a}, b={idx_b}"

    # persist_state NOT called with a new snapshot (baseline on disk unchanged)
    on_disk = json.loads((task_dir / "state.json").read_text(encoding="utf-8"))
    assert on_disk.get("implement_untracked_baseline") == ["scratch/a_file.txt", "outside/z_file.dat"]


# =============================================================================
# 12. Contract preservation
# =============================================================================


def test_commit_worker_diff_receives_in_memory_baselines(monkeypatch, tmp_path):
    """_commit_worker_diff is called with the caller's in-memory sets,
    not a re-read from state.json. Verify via monkeypatching.
    """
    impl = _impl()
    repo, task_dir = _make_repo(tmp_path)

    # Pre-set baselines on disk
    stored_untracked = ["notes.txt"]
    stored_tracked: list[str] = []
    st = _state(
        implement_untracked_baseline=list(stored_untracked),
        implement_tracked_baseline=list(stored_tracked),
    )
    (task_dir / "state.json").write_text(json.dumps(st), encoding="utf-8")

    captured_commit_args: list[dict] = []
    real_commit = impl._commit_worker_diff

    def capturing_commit(td, state, cwd, before_untracked, before_tracked):
        captured_commit_args.append({"before_untracked": before_untracked, "before_tracked": before_tracked})
        real_commit(td, state, cwd, before_untracked, before_tracked)

    monkeypatch.setattr(impl, "_commit_worker_diff", capturing_commit)
    _wire_agent_pair(impl, monkeypatch, repo)
    impl._trusted_task_dirs.add(task_dir)  # bypass cross-run floor: test covers contracts only

    result = impl._run_agent_pair(task_dir, st)

    assert result["status"] == "approved", result["feedback"]
    assert len(captured_commit_args) == 1
    args = captured_commit_args[0]
    # In-memory sets match the stored baseline
    assert args["before_untracked"] == set(stored_untracked)
    assert args["before_tracked"] == set(stored_tracked)


def test_uncommitted_scope_files_no_baseline_arg(monkeypatch, tmp_path):
    """_uncommitted_scope_files has signature (cwd, proj) — no baseline argument
    (Layer-1 stays baseline-INDEPENDENT per #112 contract).
    """
    impl = _impl()
    import inspect

    sig = inspect.signature(impl._uncommitted_scope_files)
    params = list(sig.parameters.keys())
    assert "baseline" not in params, f"_uncommitted_scope_files must not have a baseline param, got {params}"
    assert params == ["cwd", "proj"], f"unexpected signature: {params}"


def test_uncommitted_outside_scope_files_task_dir_exempt(monkeypatch, tmp_path):
    """_uncommitted_outside_scope_files keeps the task_dir exemption (Layer-2 contract)."""
    impl = _impl()
    repo, task_dir = _make_repo(tmp_path)

    # Untracked file under task_dir
    (task_dir / "artifact.txt").write_text("a\n", encoding="utf-8")

    # Baseline is empty — artifact would appear as "new" but task_dir-exempt
    baseline: set[str] = set()
    result = impl._uncommitted_outside_scope_files(repo, task_dir, _PROJ, baseline)

    # task_dir artifact must NOT appear
    for p in result:
        task_rel = task_dir.resolve().relative_to(repo.resolve()).as_posix()
        assert not p.startswith(task_rel), f"task_dir artifact leaked into Layer-2: {p}"


# =============================================================================
# 13. No HMAC, no env-scrub, no load_state mutation
# =============================================================================


def test_no_hmac_in_implement_module():
    """implement.py must not import hmac or reference baseline_hmac/_baseline_hmac."""
    workflows_dir = Path(__file__).resolve().parents[1] / "workflows"
    implement_src = (workflows_dir / "phase_runners" / "implement.py").read_text(encoding="utf-8")
    # No hmac import
    assert "import hmac" not in implement_src
    assert "baseline_hmac" not in implement_src
    assert "_baseline_hmac" not in implement_src


def test_no_hmac_in_workflows():
    """No hmac symbol appears in any workflow file (baseline_hmac / _baseline_hmac)."""
    workflows_dir = Path(__file__).resolve().parents[1] / "workflows"
    for py_file in workflows_dir.rglob("*.py"):
        src = py_file.read_text(encoding="utf-8")
        assert "baseline_hmac" not in src, f"{py_file} contains baseline_hmac"
        assert "_baseline_hmac" not in src, f"{py_file} contains _baseline_hmac"


def test_load_state_no_git_probe():
    """orchestrator.load_state must contain no git subprocess call or baseline check."""
    workflows_dir = Path(__file__).resolve().parents[1] / "workflows"
    orch_src = (workflows_dir / "orchestrator.py").read_text(encoding="utf-8")

    # Extract just the load_state function body
    start = orch_src.index("def load_state(")
    # Find next top-level def
    next_def = orch_src.index("\ndef ", start + 1)
    load_state_body = orch_src[start:next_def]

    assert "subprocess" not in load_state_body, "load_state must not call subprocess"
    assert "git " not in load_state_body, "load_state must not probe git"
    assert "implement_untracked_baseline" not in load_state_body
    assert "implement_tracked_baseline" not in load_state_body
    assert "trust" not in load_state_body.lower()


def test_worker_adapter_no_new_env_kwarg(monkeypatch, tmp_path):
    """ClaudeWorkerAdapter.invoke does not add a new env= kwarg to the worker subprocess."""
    workflows_dir = Path(__file__).resolve().parents[1] / "workflows"
    claude_src = (workflows_dir / "adapters" / "claude.py").read_text(encoding="utf-8")
    # The worker invocation (run_claude) must not pass an env= kwarg to scrub env
    # (defense-in-depth tool-deny is out of scope per outcome.md)
    # We verify no new env= assignment appears in run_claude or ClaudeWorkerAdapter.invoke
    # We check the adapter source doesn't contain 'env=' on a Popen/run_claude call
    # (a false positive here is acceptable; the test is a spot-check, not a full AST parse)
    import ast

    tree = ast.parse(claude_src)
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            for kw in node.keywords:
                if kw.arg == "env":
                    # There should be no env= kwarg on subprocess calls in the worker path
                    # (the reviewer adapter may use it for --permission-mode but not worker)
                    src_line = claude_src.splitlines()[node.lineno - 1]
                    # Only fail if it's in a worker context (not the reviewer env scrub)
                    if "disallowed" in src_line.lower() or "worker" in src_line.lower():
                        raise AssertionError(f"Unexpected env= on worker path at line {node.lineno}: {src_line}")


# =============================================================================
# 14. Same-process sibling-task isolation (goal-mode)
# =============================================================================


def test_sibling_task_isolation_goal_mode(monkeypatch, tmp_path):
    """Two sibling tasks each stamp their OWN task_dir. Trust-root floor decision
    for task B does not bleed across to task A.

    In goal-mode two tasks share the same process and each task_dir is independent.
    Task A runs first with a clean state; task B runs after with a poisoned state.
    """
    impl = _impl()
    repo, task_dir_a = _make_repo(tmp_path)

    # task_dir_b is a sibling, not under task_dir_a — created as an empty dir only
    # (no state.json yet) so it does not create an outside-scope untracked file
    # during task A's run.
    task_dir_b = repo / ".redteam" / "batches" / "b" / "tasks" / "task-002"
    task_dir_b.mkdir(parents=True)

    st_a = _state()
    (task_dir_a / "state.json").write_text(json.dumps(st_a), encoding="utf-8")

    _wire_agent_pair(impl, monkeypatch, repo)

    # Ensure neither task is in the marker initially
    impl._trusted_task_dirs.discard(task_dir_a)
    impl._trusted_task_dirs.discard(task_dir_b)

    # Run task A: marker for A is stamped; B still absent
    result_a = impl._run_agent_pair(task_dir_a, st_a)
    assert result_a["status"] == "approved", result_a["feedback"]
    assert task_dir_a in impl._trusted_task_dirs
    assert task_dir_b not in impl._trusted_task_dirs

    # Now write task B's state AFTER task A — simulate the next goal-mode task dispatch
    st_b = _state()
    st_b["task_id"] = "task-002"
    # Inject a poisoned baseline into B's state
    st_b["implement_untracked_baseline"] = ["scratch/poison.txt"]
    (task_dir_b / "state.json").write_text(json.dumps(st_b), encoding="utf-8")

    # task_dir_b/state.json is now untracked, but it's under task_dir_b's scope,
    # which is NOT task_dir_a — so it's outside task_dir_b's own exempt prefix.
    # For task B's run, the floor checks task_dir_b's own exemption, so state.json
    # itself is exempt. The poisoned baseline entry "scratch/poison.txt" fires the
    # stored-baseline-contents floor.
    result_b = impl._run_agent_pair(task_dir_b, st_b)
    assert result_b["status"] == "error", result_b["feedback"]
    assert "scratch/poison.txt" in result_b["feedback"]

    # Task A's marker must not have been cleared by task B's failure
    assert task_dir_a in impl._trusted_task_dirs
