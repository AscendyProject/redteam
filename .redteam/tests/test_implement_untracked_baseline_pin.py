"""Regression tests for the #112 untracked-baseline pin.

Covers:
- get_or_set_untracked_baseline helper (key-absent, key-present, idempotency)
- persist_state / save_state parity (deterministic clock)
- Durable pre-worker flush (both implement paths, real git)
- Layer 1 floor (source/test file caught baseline-independently)
- Layer 2 outside-scope widening (new migration committed/uncommitted, user scratch, task-dir)
- Updated feedback wording (union gate, no "source/test" claim for Layer-2-only)
- Multi-round user scratch exclusion
"""

from __future__ import annotations

import copy
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any

# ---------- engine module loaders ----------


def _base():
    import _engine

    return _engine.base()


def _impl():
    import _engine

    return _engine.implement()


def _orch():
    import _engine

    return _engine.orchestrator()


# ---------- shared test fixtures / helpers ----------

_PROJ = SimpleNamespace(source_dirs=["app/"], test_dir="tests/", context_file="docs/ctx.md", base_branch="main")


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


def _wire_agent_pair(impl, monkeypatch, repo: Path, *, on_invoke=None) -> None:
    """Common stubs for agent-pair path tests."""
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


def _committed_paths(repo: Path) -> list[str]:
    result = subprocess.run(
        ["git", "diff", "--name-only", "main...HEAD"],
        cwd=repo,
        capture_output=True,
        text=True,
    )
    return [p for p in result.stdout.split("\n") if p]


# =============================================================================
# Helper unit tests (pure, no real git)
# =============================================================================


def test_get_or_set_untracked_baseline_key_absent(monkeypatch, tmp_path):
    """Key absent → calls untracked_files exactly once, stores sorted list, returns set."""
    base = _base()

    calls: list[Path] = []

    def fake_untracked(cwd: Path) -> set[str]:
        calls.append(cwd)
        return {"app/b.py", "app/a.py"}

    monkeypatch.setattr(base, "untracked_files", fake_untracked)

    state: dict[str, Any] = {}
    result = base.get_or_set_untracked_baseline(state, tmp_path)

    assert result == {"app/a.py", "app/b.py"}
    assert state["implement_untracked_baseline"] == ["app/a.py", "app/b.py"]  # sorted list
    assert len(calls) == 1


def test_get_or_set_untracked_baseline_does_not_persist(monkeypatch, tmp_path):
    """Helper does NOT write state.json; caller is responsible for persist_state."""
    base = _base()

    monkeypatch.setattr(base, "untracked_files", lambda cwd: {"app/x.py"})

    state: dict[str, Any] = {}
    base.get_or_set_untracked_baseline(state, tmp_path)

    # state.json must NOT exist — the helper never calls persist_state
    assert not (tmp_path / "state.json").exists()


def test_get_or_set_untracked_baseline_key_present_returns_stored(monkeypatch, tmp_path):
    """Key present → returns stored list as set, does NOT call untracked_files."""
    base = _base()

    called = {"n": 0}

    def fake_untracked(cwd: Path) -> set[str]:
        called["n"] += 1
        return {"app/live.py"}  # different from persisted

    monkeypatch.setattr(base, "untracked_files", fake_untracked)

    state: dict[str, Any] = {"implement_untracked_baseline": ["app/persisted.py"]}
    result = base.get_or_set_untracked_baseline(state, tmp_path)

    assert result == {"app/persisted.py"}
    assert state["implement_untracked_baseline"] == ["app/persisted.py"]  # unchanged
    assert called["n"] == 0  # untracked_files was never called


def test_get_or_set_untracked_baseline_idempotent_under_prior_run_signals(monkeypatch, tmp_path):
    """Set-once decision is keyed SOLELY on key presence — no prior-run signals consulted."""
    base = _base()

    called = {"n": 0}
    monkeypatch.setattr(base, "untracked_files", lambda cwd: called.__setitem__("n", called["n"] + 1) or {"x.py"})

    # State with key present but varying other signals that must NOT trigger re-snapshot
    for implement_round_count, phases_completed, last_run_at in [
        (5, ["plan_outcome", "implement", "implement"], "2026-01-01T00:00:00Z"),
        (0, [], None),
        (1, ["implement"], "2026-06-27T00:00:00Z"),
    ]:
        state: dict[str, Any] = {
            "implement_untracked_baseline": ["app/fixed.py"],
            "implement_round_count": implement_round_count,
            "phases_completed": phases_completed,
            "verification": {"last_run_at": last_run_at},
        }
        result = base.get_or_set_untracked_baseline(state, tmp_path)
        assert result == {"app/fixed.py"}

    assert called["n"] == 0  # never re-snapshotted regardless of those signals


# =============================================================================
# persist_state / save_state parity
# =============================================================================


def test_persist_state_and_save_state_write_byte_identical_state_json(monkeypatch, tmp_path):
    """Both writers must produce byte-identical state.json content (deterministic clock)."""
    import phase_runners._base as _pbase

    base = _base()
    orch = _orch()

    # There are TWO module objects for _base.py:
    #   (a) redteam_phase_base — loaded by _engine.base() via importlib
    #   (b) phase_runners._base — the actual package module that orchestrator's
    #       imported `persist_state` and `utc_now` closures live in.
    # Patching only (a) leaves (b)'s utc_now at wall-clock time, so orch.save_state
    # writes a different updated_at and the byte comparison fails. Patch BOTH.
    fixed_time = "2026-06-28T00:00:00.000000+00:00"
    monkeypatch.setattr(base, "utc_now", lambda: fixed_time)
    monkeypatch.setattr(_pbase, "utc_now", lambda: fixed_time)

    td1 = tmp_path / "t1"
    td1.mkdir()
    td2 = tmp_path / "t2"
    td2.mkdir()

    st = {"task_id": "t1", "next_phase": "implement"}

    # persist_state writes via _base directly
    base.persist_state(td1, copy.deepcopy(st))
    bytes1 = (td1 / "state.json").read_bytes()

    # save_state delegates to persist_state, so it should write the same bytes
    orch.save_state(td2, copy.deepcopy(st))
    bytes2 = (td2 / "state.json").read_bytes()

    assert bytes1 == bytes2, "persist_state and save_state must write byte-identical state.json"


def test_save_state_still_writes_progress_md(monkeypatch, tmp_path):
    """After refactoring, save_state still produces progress.md."""
    orch = _orch()
    base = _base()
    monkeypatch.setattr(base, "utc_now", lambda: "2026-06-28T00:00:00.000000+00:00")

    state = {"task_id": "t1", "next_phase": "implement"}
    orch.save_state(tmp_path, state)

    assert (tmp_path / "progress.md").exists()
    text = (tmp_path / "progress.md").read_text(encoding="utf-8")
    assert "t1" in text


# =============================================================================
# Durable pre-worker flush — real git, BOTH paths
# =============================================================================


def test_durable_preworker_flush_agent_pair(monkeypatch, tmp_path):
    """Agent-pair: baseline is persisted BEFORE worker runs.

    Round 1: worker creates a new source file then returns non-zero RC (early exit
    before _commit_worker_diff). After the call, state.json must contain the baseline
    WITHOUT the new file. Round 2: baseline is re-used; the new file is committed.
    """
    impl = _impl()
    repo, task_dir = _make_repo(tmp_path)

    monkeypatch.setattr(impl, "repo_root", lambda: repo)
    monkeypatch.setattr(impl, "project_config", lambda: _PROJ)
    monkeypatch.setattr(
        impl,
        "_run_verification_commands",
        lambda cwd, cmds, project_verify_command=None, allowlist=None: (0, "ok\n"),
    )

    new_file = repo / "app" / "new_feature.py"

    def invoke_round1(**kwargs: Any) -> dict[str, Any]:
        # Creates the file AFTER persist_state has already written the baseline
        new_file.write_text("x = 1\n", encoding="utf-8")
        return {"returncode": 1, "stdout": "", "stderr": "crash"}  # non-zero → early exit

    monkeypatch.setattr(impl, "get_worker_adapter", lambda state: SimpleNamespace(invoke=invoke_round1))

    st = _state()
    # Pre-write state.json so persist_state can write into it
    (task_dir / "state.json").write_text(json.dumps(st), encoding="utf-8")

    result1 = impl._run_agent_pair(task_dir, st)
    assert result1["status"] == "error"
    assert "exited non-zero" in result1["feedback"]

    # Read back from disk: baseline must be present and must NOT include new_feature.py
    persisted = json.loads((task_dir / "state.json").read_text(encoding="utf-8"))
    assert "implement_untracked_baseline" in persisted
    assert isinstance(persisted["implement_untracked_baseline"], list)
    assert "app/new_feature.py" not in persisted["implement_untracked_baseline"]

    # Round 2: worker is a no-op, new_feature.py is still untracked
    def invoke_noop(**kwargs: Any) -> dict[str, Any]:
        return {"returncode": 0, "stdout": "done", "stderr": ""}

    monkeypatch.setattr(impl, "get_worker_adapter", lambda state: SimpleNamespace(invoke=invoke_noop))

    st2 = json.loads((task_dir / "state.json").read_text(encoding="utf-8"))
    result2 = impl._run_agent_pair(task_dir, st2)

    assert result2["status"] == "approved", result2["feedback"]
    assert "app/new_feature.py" in _committed_paths(repo)


def test_durable_preworker_flush_tdd(monkeypatch, tmp_path):
    """TDD path: same durable pre-worker flush guarantee.

    Round 1: worker creates source file then returns non-zero RC. After call,
    state.json has baseline without the new file. Round 2: file gets committed.
    """
    impl = _impl()
    repo, task_dir = _make_repo(tmp_path)

    # TDD path needs load_config patched
    import config as _config

    monkeypatch_proj = SimpleNamespace(
        source_dirs=["app/"],
        test_dir="tests/",
        context_file="c",
        base_branch="main",
        verify_command="true",
        verification_allowlist=("true",),
    )
    monkeypatch.setattr(_config, "load_config", lambda *_a, **_k: SimpleNamespace(project=monkeypatch_proj))
    monkeypatch.setattr(impl, "repo_root", lambda: repo)
    monkeypatch.setattr(impl, "project_config", lambda: monkeypatch_proj)
    monkeypatch.setattr(impl, "_run_verify_sh", lambda cwd, argv: (0, "ok\n"))

    new_file = repo / "app" / "tdd_new.py"

    def invoke_round1(**kwargs: Any) -> dict[str, Any]:
        new_file.write_text("y = 2\n", encoding="utf-8")
        return {"returncode": 1, "stdout": "", "stderr": "crash"}

    monkeypatch.setattr(impl, "get_worker_adapter", lambda state: SimpleNamespace(invoke=invoke_round1))

    st = {"task_id": "task-001", "mode": "tdd", "base_branch": "main", "verification": {"commands": []}}
    (task_dir / "state.json").write_text(json.dumps(st), encoding="utf-8")

    result1 = impl.run(task_dir, st)
    assert result1["status"] == "error"

    # Baseline persisted, new file NOT in it
    persisted = json.loads((task_dir / "state.json").read_text(encoding="utf-8"))
    assert "implement_untracked_baseline" in persisted
    assert "app/tdd_new.py" not in persisted["implement_untracked_baseline"]

    # Round 2: baseline re-used, file committed
    def invoke_noop(**kwargs: Any) -> dict[str, Any]:
        return {"returncode": 0, "stdout": "done", "stderr": ""}

    monkeypatch.setattr(impl, "get_worker_adapter", lambda state: SimpleNamespace(invoke=invoke_noop))

    st2 = json.loads((task_dir / "state.json").read_text(encoding="utf-8"))
    result2 = impl.run(task_dir, st2)
    assert result2["status"] == "approved", result2["feedback"]
    assert "app/tdd_new.py" in _committed_paths(repo)


# =============================================================================
# Layer 1 floor: source/test file in baseline → still flagged (baseline-INDEPENDENT)
# =============================================================================


def test_layer1_source_file_flagged_even_in_baseline_agent_pair(monkeypatch, tmp_path):
    """Layer 1 is baseline-INDEPENDENT: a source/test file that ended up in the
    baseline (pre-fix crash residual) is still caught by _uncommitted_scope_files.

    Scenario: 'app/leaked.py' is already untracked before the first implement entry
    (e.g. a prior interrupted round created it). Fresh snapshot includes it →
    _commit_worker_diff skips it (it's in baseline) → Layer 1 still flags it.
    """
    impl = _impl()
    repo, task_dir = _make_repo(tmp_path)

    leaked = repo / "app" / "leaked.py"
    leaked.write_text("leak = True\n", encoding="utf-8")  # untracked BEFORE entry

    _wire_agent_pair(impl, monkeypatch, repo)  # worker is a no-op

    st = _state()
    (task_dir / "state.json").write_text(json.dumps(st), encoding="utf-8")

    result = impl._run_agent_pair(task_dir, st)

    # The fresh snapshot includes leaked.py, so _commit_worker_diff doesn't commit it.
    # Layer 1 catches it as an uncommitted source file.
    assert result["status"] == "error"
    assert "app/leaked.py" in result["feedback"]
    assert "stale" in result["feedback"].lower() or "STALE" in result["feedback"]
    # Must NOT have been committed
    assert "app/leaked.py" not in _committed_paths(repo)


def test_layer1_source_file_flagged_even_in_baseline_tdd(monkeypatch, tmp_path):
    """TDD path: same Layer-1 floor guarantee for a source file in the baseline."""
    impl = _impl()
    repo, task_dir = _make_repo(tmp_path)

    leaked = repo / "app" / "leaked_tdd.py"
    leaked.write_text("leak = True\n", encoding="utf-8")

    import config as _config

    monkeypatch_proj = SimpleNamespace(
        source_dirs=["app/"],
        test_dir="tests/",
        context_file="c",
        base_branch="main",
        verify_command="true",
        verification_allowlist=("true",),
    )
    monkeypatch.setattr(_config, "load_config", lambda *_a, **_k: SimpleNamespace(project=monkeypatch_proj))
    monkeypatch.setattr(impl, "repo_root", lambda: repo)
    monkeypatch.setattr(impl, "project_config", lambda: monkeypatch_proj)
    monkeypatch.setattr(impl, "_run_verify_sh", lambda cwd, argv: (0, "ok\n"))
    monkeypatch.setattr(
        impl,
        "get_worker_adapter",
        lambda state: SimpleNamespace(invoke=lambda **kw: {"returncode": 0, "stdout": "done", "stderr": ""}),
    )

    st = {"task_id": "task-001", "mode": "tdd", "base_branch": "main", "verification": {"commands": []}}
    (task_dir / "state.json").write_text(json.dumps(st), encoding="utf-8")

    result = impl.run(task_dir, st)

    assert result["status"] == "error"
    assert "app/leaked_tdd.py" in result["feedback"]
    assert "app/leaked_tdd.py" not in _committed_paths(repo)


# =============================================================================
# Layer 2 outside-scope widening — real git
# =============================================================================


def test_layer2_outside_scope_uncommitted_flagged(monkeypatch, tmp_path):
    """Layer 2 flags a new file outside source/test that the worker left uncommitted.

    The file is NOT in the persisted baseline (it was created by the worker in this
    round), is outside app/ and tests/, and _commit_worker_diff somehow missed it
    (simulated by making the worker return 0 but leave the file untracked).
    """
    impl = _impl()
    repo, task_dir = _make_repo(tmp_path)

    # Pre-set baseline (key present = set-once, baseline is empty = no pre-existing scratch)
    st = _state(implement_untracked_baseline=[])
    (task_dir / "state.json").write_text(json.dumps(st), encoding="utf-8")

    migration_dir = repo / "migrations"
    migration_dir.mkdir()
    migration = migration_dir / "0001.sql"

    def invoke(**kwargs: Any) -> dict[str, Any]:
        # Worker creates a migration file but returns success;
        # we'll verify Layer 2 catches it if it stays uncommitted.
        migration.write_text("-- up\n", encoding="utf-8")
        return {"returncode": 0, "stdout": "done", "stderr": ""}

    monkeypatch.setattr(impl, "repo_root", lambda: repo)
    monkeypatch.setattr(impl, "project_config", lambda: _PROJ)
    monkeypatch.setattr(impl, "get_worker_adapter", lambda state: SimpleNamespace(invoke=invoke))
    monkeypatch.setattr(
        impl,
        "_run_verification_commands",
        lambda cwd, cmds, project_verify_command=None, allowlist=None: (0, "ok\n"),
    )

    # Intercept _commit_worker_diff to make it NOT commit the migration
    # (simulate a scenario where _commit_worker_diff missed an outside-scope file
    # — here we test Layer 2's detection capability)
    real_commit = impl._commit_worker_diff

    def commit_no_migration(td, state, cwd, before, before_tracked):
        # Run real commit but the migration was untracked so it would be picked up
        # normally. To test Layer 2 detection, we want it to remain untracked.
        # We achieve this by: letting _commit_worker_diff run normally (it commits
        # migrations/) and then checking via the gate. Actually _commit_worker_diff
        # WILL commit it. So for this test to isolate Layer 2, we need the migration
        # to NOT be committed.
        # Instead: let real commit run but afterward unstage the migration.
        real_commit(td, state, cwd, before, before_tracked)
        # Now reset the migration commit so it's untracked again (simulating missed commit)
        subprocess.run(["git", "reset", "HEAD~1"], cwd=cwd, check=False, capture_output=True)

    monkeypatch.setattr(impl, "_commit_worker_diff", commit_no_migration)

    result = impl._run_agent_pair(task_dir, st)

    # Layer 2 should flag the migration file (it's outside app/ and tests/, not in baseline)
    assert result["status"] == "error"
    assert "migrations/0001.sql" in result["feedback"]
    assert "stale" in result["feedback"].lower() or "STALE" in result["feedback"]


def test_layer2_outside_scope_committed_passes(monkeypatch, tmp_path):
    """Layer 2 does NOT flag a new outside-scope file that was properly committed."""
    impl = _impl()
    repo, task_dir = _make_repo(tmp_path)

    st = _state(implement_untracked_baseline=[])
    (task_dir / "state.json").write_text(json.dumps(st), encoding="utf-8")

    def invoke(**kwargs: Any) -> dict[str, Any]:
        (repo / "migrations").mkdir(exist_ok=True)
        (repo / "migrations" / "0001.sql").write_text("-- up\n", encoding="utf-8")
        return {"returncode": 0, "stdout": "done", "stderr": ""}

    _wire_agent_pair(impl, monkeypatch, repo, on_invoke=None)
    monkeypatch.setattr(impl, "get_worker_adapter", lambda state: SimpleNamespace(invoke=invoke))

    result = impl._run_agent_pair(task_dir, st)

    # _commit_worker_diff commits migrations/0001.sql; gate should pass
    assert result["status"] == "approved", result["feedback"]
    assert "migrations/0001.sql" in _committed_paths(repo)


def test_layer2_user_scratch_in_baseline_not_flagged(monkeypatch, tmp_path):
    """A user's pre-existing scratch file (outside source/test, IN the baseline)
    is NOT flagged by Layer 2."""
    impl = _impl()
    repo, task_dir = _make_repo(tmp_path)

    # User has a notes.txt outside app/ and tests/
    notes = repo / "notes.txt"
    notes.write_text("my scratch\n", encoding="utf-8")

    # First, take the baseline (key absent) — notes.txt goes into the baseline
    st = _state()
    (task_dir / "state.json").write_text(json.dumps(st), encoding="utf-8")

    _wire_agent_pair(impl, monkeypatch, repo)  # worker no-op
    impl._trusted_task_dirs.add(task_dir)  # bypass cross-run floor: test covers Layer-2 only

    result = impl._run_agent_pair(task_dir, st)

    # notes.txt is in the baseline → Layer 2 does NOT flag it
    assert result["status"] == "approved", result["feedback"]
    # notes.txt not in committed paths (it was in baseline, so _commit_worker_diff skips it)
    assert "notes.txt" not in _committed_paths(repo)


def test_layer2_task_dir_artifact_not_flagged(monkeypatch, tmp_path):
    """A task-dir artifact (e.g. impl_diff.patch) is NOT flagged by Layer 2."""
    impl = _impl()
    repo, task_dir = _make_repo(tmp_path)

    # Pre-set empty baseline (worker creates impl_diff.patch in task_dir)
    st = _state(implement_untracked_baseline=[])
    (task_dir / "state.json").write_text(json.dumps(st), encoding="utf-8")

    def invoke(**kwargs: Any) -> dict[str, Any]:
        # A harness-written artifact in the task dir
        (task_dir / "some_artifact.txt").write_text("artifact\n", encoding="utf-8")
        return {"returncode": 0, "stdout": "done", "stderr": ""}

    _wire_agent_pair(impl, monkeypatch, repo, on_invoke=None)
    monkeypatch.setattr(impl, "get_worker_adapter", lambda state: SimpleNamespace(invoke=invoke))

    result = impl._run_agent_pair(task_dir, st)

    # task_dir artifact is excluded from Layer 2 (same _in_task_dir check as _commit_worker_diff)
    assert result["status"] == "approved", result["feedback"]


# =============================================================================
# Feedback wording tests
# =============================================================================


def test_feedback_wording_layer2_only_no_source_test_claim(monkeypatch, tmp_path):
    """When only Layer 2 fires, feedback must NOT claim 'source/test changes'.

    The new wording uses 'changes uncommitted' (not 'source/test changes uncommitted').
    """
    impl = _impl()
    repo, task_dir = _make_repo(tmp_path)

    # Empty baseline; worker creates a migration file and we intercept commit to skip it
    st = _state(implement_untracked_baseline=[])
    (task_dir / "state.json").write_text(json.dumps(st), encoding="utf-8")

    def invoke(**kwargs: Any) -> dict[str, Any]:
        (repo / "migrations").mkdir(exist_ok=True)
        (repo / "migrations" / "schema.sql").write_text("-- schema\n", encoding="utf-8")
        return {"returncode": 0, "stdout": "done", "stderr": ""}

    monkeypatch.setattr(impl, "repo_root", lambda: repo)
    monkeypatch.setattr(impl, "project_config", lambda: _PROJ)
    monkeypatch.setattr(impl, "get_worker_adapter", lambda state: SimpleNamespace(invoke=invoke))
    monkeypatch.setattr(
        impl,
        "_run_verification_commands",
        lambda cwd, cmds, project_verify_command=None, allowlist=None: (0, "ok\n"),
    )

    real_commit = impl._commit_worker_diff

    def commit_skip_migration(td, state, cwd, before, before_tracked):
        real_commit(td, state, cwd, before, before_tracked)
        subprocess.run(["git", "reset", "HEAD~1"], cwd=cwd, check=False, capture_output=True)

    monkeypatch.setattr(impl, "_commit_worker_diff", commit_skip_migration)

    result = impl._run_agent_pair(task_dir, st)

    assert result["status"] == "error"
    fb = result["feedback"]
    # Layer 2 only fires (no source/test file involved)
    assert "migrations/schema.sql" in fb
    # The wording must NOT claim "source/test changes uncommitted" when only Layer 2 fires
    assert "source/test changes" not in fb
    # Must carry the stale-range intent
    assert "STALE" in fb or "stale" in fb.lower()
    # No git stderr leakage
    assert "fatal:" not in fb


def test_feedback_wording_layer1_only_agent_pair(monkeypatch, tmp_path):
    """When only Layer 1 fires (source/test uncommitted), feedback names the file."""
    impl = _impl()
    repo, task_dir = _make_repo(tmp_path)

    # Worker creates a source file but we simulate it being left uncommitted in scope
    st = _state(implement_untracked_baseline=[])
    (task_dir / "state.json").write_text(json.dumps(st), encoding="utf-8")

    leaked = repo / "app" / "unscoped.py"

    def invoke(**kwargs: Any) -> dict[str, Any]:
        leaked.write_text("x = 1\n", encoding="utf-8")
        return {"returncode": 0, "stdout": "done", "stderr": ""}

    monkeypatch.setattr(impl, "repo_root", lambda: repo)
    monkeypatch.setattr(impl, "project_config", lambda: _PROJ)
    monkeypatch.setattr(impl, "get_worker_adapter", lambda state: SimpleNamespace(invoke=invoke))
    monkeypatch.setattr(
        impl,
        "_run_verification_commands",
        lambda cwd, cmds, project_verify_command=None, allowlist=None: (0, "ok\n"),
    )

    real_commit = impl._commit_worker_diff

    def commit_skip_source(td, state, cwd, before, before_tracked):
        real_commit(td, state, cwd, before, before_tracked)
        subprocess.run(["git", "reset", "HEAD~1"], cwd=cwd, check=False, capture_output=True)

    monkeypatch.setattr(impl, "_commit_worker_diff", commit_skip_source)

    result = impl._run_agent_pair(task_dir, st)

    assert result["status"] == "error"
    fb = result["feedback"]
    assert "app/unscoped.py" in fb
    assert "STALE" in fb or "stale" in fb.lower()
    assert "fatal:" not in fb


def test_feedback_wording_both_layers_names_all_files(monkeypatch, tmp_path):
    """When both Layer 1 and Layer 2 fire, all stray files are named in feedback."""
    impl = _impl()
    repo, task_dir = _make_repo(tmp_path)

    # Empty baseline
    st = _state(implement_untracked_baseline=[])
    (task_dir / "state.json").write_text(json.dumps(st), encoding="utf-8")

    source_file = repo / "app" / "scope_file.py"
    migration = repo / "migrations" / "001.sql"

    def invoke(**kwargs: Any) -> dict[str, Any]:
        source_file.write_text("a = 1\n", encoding="utf-8")
        (repo / "migrations").mkdir(exist_ok=True)
        migration.write_text("-- m\n", encoding="utf-8")
        return {"returncode": 0, "stdout": "done", "stderr": ""}

    monkeypatch.setattr(impl, "repo_root", lambda: repo)
    monkeypatch.setattr(impl, "project_config", lambda: _PROJ)
    monkeypatch.setattr(impl, "get_worker_adapter", lambda state: SimpleNamespace(invoke=invoke))
    monkeypatch.setattr(
        impl,
        "_run_verification_commands",
        lambda cwd, cmds, project_verify_command=None, allowlist=None: (0, "ok\n"),
    )

    real_commit = impl._commit_worker_diff

    def commit_skip_all(td, state, cwd, before, before_tracked):
        real_commit(td, state, cwd, before, before_tracked)
        subprocess.run(["git", "reset", "HEAD~1"], cwd=cwd, check=False, capture_output=True)

    monkeypatch.setattr(impl, "_commit_worker_diff", commit_skip_all)

    result = impl._run_agent_pair(task_dir, st)

    assert result["status"] == "error"
    fb = result["feedback"]
    # Both files must appear
    assert "app/scope_file.py" in fb
    assert "migrations/001.sql" in fb
    assert "fatal:" not in fb


def test_feedback_wording_tdd_path(monkeypatch, tmp_path):
    """TDD path also emits the updated union wording (no 'source/test changes' claim)."""
    impl = _impl()
    repo, task_dir = _make_repo(tmp_path)

    import config as _config

    monkeypatch_proj = SimpleNamespace(
        source_dirs=["app/"],
        test_dir="tests/",
        context_file="c",
        base_branch="main",
        verify_command="true",
        verification_allowlist=("true",),
    )
    monkeypatch.setattr(_config, "load_config", lambda *_a, **_k: SimpleNamespace(project=monkeypatch_proj))
    monkeypatch.setattr(impl, "repo_root", lambda: repo)
    monkeypatch.setattr(impl, "project_config", lambda: monkeypatch_proj)
    monkeypatch.setattr(impl, "_run_verify_sh", lambda cwd, argv: (0, "ok\n"))

    # Empty baseline + worker creates migration but we simulate it left uncommitted
    st = {
        "task_id": "task-001",
        "mode": "tdd",
        "base_branch": "main",
        "verification": {"commands": []},
        "implement_untracked_baseline": [],
    }
    (task_dir / "state.json").write_text(json.dumps(st), encoding="utf-8")

    def invoke(**kwargs: Any) -> dict[str, Any]:
        (repo / "migrations").mkdir(exist_ok=True)
        (repo / "migrations" / "tdd_migration.sql").write_text("-- tdd\n", encoding="utf-8")
        return {"returncode": 0, "stdout": "done", "stderr": ""}

    monkeypatch.setattr(impl, "get_worker_adapter", lambda state: SimpleNamespace(invoke=invoke))

    real_commit = impl._commit_worker_diff

    def commit_skip_migration(td, state, cwd, before, before_tracked):
        real_commit(td, state, cwd, before, before_tracked)
        subprocess.run(["git", "reset", "HEAD~1"], cwd=cwd, check=False, capture_output=True)

    monkeypatch.setattr(impl, "_commit_worker_diff", commit_skip_migration)

    result = impl.run(task_dir, st)

    assert result["status"] == "error"
    fb = result["feedback"]
    assert "migrations/tdd_migration.sql" in fb
    assert "source/test changes" not in fb
    assert "STALE" in fb or "stale" in fb.lower()
    assert "fatal:" not in fb


# =============================================================================
# Multi-round user scratch exclusion (≥2 rounds)
# =============================================================================


def test_user_scratch_excluded_across_multiple_rounds(monkeypatch, tmp_path):
    """A user's pre-existing scratch (outside source/test) captured in the baseline
    at first entry stays excluded from commits across at least two rounds."""
    impl = _impl()
    repo, task_dir = _make_repo(tmp_path)

    # User has a scratch file before the FIRST implement entry
    user_scratch = repo / "scratch.md"
    user_scratch.write_text("my notes\n", encoding="utf-8")

    # First round: worker creates a source file
    round_num = {"n": 0}

    def invoke(**kwargs: Any) -> dict[str, Any]:
        round_num["n"] += 1
        if round_num["n"] == 1:
            (repo / "app" / "round1.py").write_text("r = 1\n", encoding="utf-8")
        else:
            (repo / "app" / "round2.py").write_text("r = 2\n", encoding="utf-8")
        return {"returncode": 0, "stdout": "done", "stderr": ""}

    monkeypatch.setattr(impl, "repo_root", lambda: repo)
    monkeypatch.setattr(impl, "project_config", lambda: _PROJ)
    monkeypatch.setattr(impl, "get_worker_adapter", lambda state: SimpleNamespace(invoke=invoke))
    monkeypatch.setattr(
        impl,
        "_run_verification_commands",
        lambda cwd, cmds, project_verify_command=None, allowlist=None: (0, "ok\n"),
    )

    # Round 1: baseline is taken (scratch.md goes in); round1.py is committed
    st = _state()
    (task_dir / "state.json").write_text(json.dumps(st), encoding="utf-8")
    impl._trusted_task_dirs.add(task_dir)  # bypass cross-run floor: test covers Layer-2 only
    r1 = impl._run_agent_pair(task_dir, st)
    assert r1["status"] == "approved", r1["feedback"]
    assert "app/round1.py" in _committed_paths(repo)
    assert "scratch.md" not in _committed_paths(repo)

    # Round 2: baseline re-used; round2.py is committed, scratch.md still excluded
    st2 = json.loads((task_dir / "state.json").read_text(encoding="utf-8"))
    r2 = impl._run_agent_pair(task_dir, st2)
    assert r2["status"] == "approved", r2["feedback"]
    assert "app/round2.py" in _committed_paths(repo)
    assert "scratch.md" not in _committed_paths(repo)


# =============================================================================
# Shared-helper coverage: both paths import the SAME symbols
# =============================================================================


def test_shared_helper_symbols_exported_from_base():
    """Both get_or_set_untracked_baseline and persist_state are exported from _base."""
    base = _base()
    assert callable(getattr(base, "get_or_set_untracked_baseline", None))
    assert callable(getattr(base, "persist_state", None))


def test_implement_module_imports_shared_helpers():
    """implement.py imports the shared helpers from _base (not local copies).

    Note: _engine.base() loads _base.py as 'redteam_phase_base' via importlib,
    which is a DIFFERENT module object from the 'phase_runners._base' package
    module that implement.py actually imports from.  The `is` check must compare
    against phase_runners._base (the same module object implement imports).
    """
    import phase_runners._base as _pbase

    impl = _impl()
    # impl is phase_runners.implement; its helper bindings must resolve to the
    # same function objects as phase_runners._base (one shared definition, not copies).
    assert impl.get_or_set_untracked_baseline is _pbase.get_or_set_untracked_baseline
    assert impl.persist_state is _pbase.persist_state
