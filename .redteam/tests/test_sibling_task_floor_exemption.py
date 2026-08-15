"""Regression tests for #124 — sibling-task harness artifact exemption.

The out-of-scope tracked floor (_floor_outside_scope in implement.py) must NOT
fire when the only out-of-scope tracked change vs the pinned base is a sibling
task's top-level harness-owned decision-trail artifact under the SAME batch's
tasks/ root. All fail-closed behaviors — subdirectory artifacts, non-allowlisted
basenames, cross-batch paths, and root-level paths — must still trip the floor.

Each behavioral case is asserted in BOTH the agent-pair (_run_agent_pair) and
the tdd (run with mode="tdd") implement paths, mirroring the dual-helper pattern
from test_tracked_baseline_attribution.py:84-122.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest


def _impl():
    import _engine

    return _engine.implement()


_PROJ = SimpleNamespace(
    source_dirs=["app/"],
    test_dir="tests/",
    context_file="docs/ctx.md",
    base_branch="main",
    test_conventions_file="docs/test-conventions.md",
)


def _state(**extra: Any) -> dict[str, Any]:
    s = {"task_id": "task-002", "mode": "agent-pair", "base_branch": "main", "verification": {"commands": []}}
    s.update(extra)
    return s


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True, encoding="utf-8")


def _make_stacked_repo(tmp_path: Path) -> tuple[Path, Path, Path]:
    """Return (repo, task_dir, sibling_dir) with a clean stacked layout.

    task_dir   = <repo>/.redteam/batches/b/tasks/task-002/  (current task)
    sibling_dir = <repo>/.redteam/batches/b/tasks/task-001/  (sibling, same batch)
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "t@e")
    _git(repo, "config", "user.name", "t")
    (repo / "app").mkdir()
    (repo / "tests").mkdir()
    (repo / "tests" / ".gitkeep").write_text("", encoding="utf-8")
    batch_tasks = repo / ".redteam" / "batches" / "b" / "tasks"
    sibling_dir = batch_tasks / "task-001"
    sibling_dir.mkdir(parents=True)
    task_dir = batch_tasks / "task-002"
    task_dir.mkdir(parents=True)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "init")
    _git(repo, "checkout", "-b", "redteam/task-002")
    return repo, task_dir, sibling_dir


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
# Proceeds-on-sibling-top-level-artifact (one assertion per basename)
# =============================================================================


@pytest.mark.parametrize("artifact_name", ["state.json", "outcome.md", "pr.md", "code_review.md", "pr_url.txt"])
def test_sibling_top_level_artifact_proceeds_agent_pair(monkeypatch, tmp_path, artifact_name):
    """agent-pair: sibling top-level artifact → floor does NOT fire, worker IS invoked."""
    impl = _impl()
    repo, task_dir, sibling_dir = _make_stacked_repo(tmp_path)

    # Stage the sibling artifact at the TOP LEVEL of the sibling task dir
    (sibling_dir / artifact_name).write_text("{}\n", encoding="utf-8")
    _git(repo, "add", (sibling_dir / artifact_name).relative_to(repo).as_posix())

    invoked = {"yes": False}
    _wire_agent_pair(impl, monkeypatch, repo, on_invoke=lambda: invoked.__setitem__("yes", True))

    st = _state()
    (task_dir / "state.json").write_text(json.dumps(st), encoding="utf-8")

    result = impl._run_agent_pair(task_dir, st)

    assert result["status"] == "approved", result.get("feedback", "")
    assert invoked["yes"] is True


@pytest.mark.parametrize("artifact_name", ["state.json", "outcome.md", "pr.md", "code_review.md", "pr_url.txt"])
def test_sibling_top_level_artifact_proceeds_tdd(monkeypatch, tmp_path, artifact_name):
    """tdd: sibling top-level artifact → floor does NOT fire, worker IS invoked."""
    impl = _impl()
    repo, task_dir, sibling_dir = _make_stacked_repo(tmp_path)

    (sibling_dir / artifact_name).write_text("{}\n", encoding="utf-8")
    _git(repo, "add", (sibling_dir / artifact_name).relative_to(repo).as_posix())

    invoked = {"yes": False}
    _wire_tdd(impl, monkeypatch, repo, on_invoke=lambda: invoked.__setitem__("yes", True))

    st = {"task_id": "task-002", "mode": "tdd", "base_branch": "main", "verification": {"commands": []}}
    (task_dir / "state.json").write_text(json.dumps(st), encoding="utf-8")

    result = impl.run(task_dir, st)

    assert result["status"] == "approved", result.get("feedback", "")
    assert invoked["yes"] is True


# =============================================================================
# Still-trips-on-sibling-subdirectory-with-allowlisted-basename (PR-002 negative)
# =============================================================================


@pytest.mark.parametrize(
    "sub_artifact",
    ["sub/state.json", "archive/code_review.md"],
)
def test_sibling_subdirectory_allowlisted_still_trips_agent_pair(monkeypatch, tmp_path, sub_artifact):
    """agent-pair (PR-002): allowlisted basename BURIED in sibling subdir → floor fires."""
    impl = _impl()
    repo, task_dir, sibling_dir = _make_stacked_repo(tmp_path)

    artifact_path = sibling_dir / sub_artifact
    artifact_path.parent.mkdir(parents=True, exist_ok=True)
    artifact_path.write_text("{}\n", encoding="utf-8")
    _git(repo, "add", artifact_path.relative_to(repo).as_posix())

    invoked = {"yes": False}
    _wire_agent_pair(impl, monkeypatch, repo, on_invoke=lambda: invoked.__setitem__("yes", True))

    st = _state()
    (task_dir / "state.json").write_text(json.dumps(st), encoding="utf-8")

    result = impl._run_agent_pair(task_dir, st)

    assert result["status"] == "error"
    assert "commit or stash" in result["feedback"]
    assert artifact_path.relative_to(repo).as_posix() in result["feedback"]
    assert invoked["yes"] is False


@pytest.mark.parametrize(
    "sub_artifact",
    ["sub/state.json", "archive/code_review.md"],
)
def test_sibling_subdirectory_allowlisted_still_trips_tdd(monkeypatch, tmp_path, sub_artifact):
    """tdd (PR-002): allowlisted basename BURIED in sibling subdir → floor fires."""
    impl = _impl()
    repo, task_dir, sibling_dir = _make_stacked_repo(tmp_path)

    artifact_path = sibling_dir / sub_artifact
    artifact_path.parent.mkdir(parents=True, exist_ok=True)
    artifact_path.write_text("{}\n", encoding="utf-8")
    _git(repo, "add", artifact_path.relative_to(repo).as_posix())

    invoked = {"yes": False}
    _wire_tdd(impl, monkeypatch, repo, on_invoke=lambda: invoked.__setitem__("yes", True))

    st = {"task_id": "task-002", "mode": "tdd", "base_branch": "main", "verification": {"commands": []}}
    (task_dir / "state.json").write_text(json.dumps(st), encoding="utf-8")

    result = impl.run(task_dir, st)

    assert result["status"] == "error"
    assert "commit or stash" in result["feedback"]
    assert artifact_path.relative_to(repo).as_posix() in result["feedback"]
    assert invoked["yes"] is False


# =============================================================================
# Still-trips-on-non-allowlisted-top-level-sibling-path
# =============================================================================


@pytest.mark.parametrize("bad_name", ["scratch.py", "verification.log"])
def test_non_allowlisted_sibling_top_level_still_trips_agent_pair(monkeypatch, tmp_path, bad_name):
    """agent-pair: non-allowlisted top-level path under sibling task dir → floor fires."""
    impl = _impl()
    repo, task_dir, sibling_dir = _make_stacked_repo(tmp_path)

    artifact_path = sibling_dir / bad_name
    artifact_path.write_text("data\n", encoding="utf-8")
    _git(repo, "add", artifact_path.relative_to(repo).as_posix())

    invoked = {"yes": False}
    _wire_agent_pair(impl, monkeypatch, repo, on_invoke=lambda: invoked.__setitem__("yes", True))

    st = _state()
    (task_dir / "state.json").write_text(json.dumps(st), encoding="utf-8")

    result = impl._run_agent_pair(task_dir, st)

    assert result["status"] == "error"
    assert "commit or stash" in result["feedback"]
    assert artifact_path.relative_to(repo).as_posix() in result["feedback"]
    assert invoked["yes"] is False


@pytest.mark.parametrize("bad_name", ["scratch.py", "verification.log"])
def test_non_allowlisted_sibling_top_level_still_trips_tdd(monkeypatch, tmp_path, bad_name):
    """tdd: non-allowlisted top-level path under sibling task dir → floor fires."""
    impl = _impl()
    repo, task_dir, sibling_dir = _make_stacked_repo(tmp_path)

    artifact_path = sibling_dir / bad_name
    artifact_path.write_text("data\n", encoding="utf-8")
    _git(repo, "add", artifact_path.relative_to(repo).as_posix())

    invoked = {"yes": False}
    _wire_tdd(impl, monkeypatch, repo, on_invoke=lambda: invoked.__setitem__("yes", True))

    st = {"task_id": "task-002", "mode": "tdd", "base_branch": "main", "verification": {"commands": []}}
    (task_dir / "state.json").write_text(json.dumps(st), encoding="utf-8")

    result = impl.run(task_dir, st)

    assert result["status"] == "error"
    assert "commit or stash" in result["feedback"]
    assert artifact_path.relative_to(repo).as_posix() in result["feedback"]
    assert invoked["yes"] is False


# =============================================================================
# Still-trips-on-cross-batch-allowlisted-path
# =============================================================================


def test_cross_batch_allowlisted_still_trips_agent_pair(monkeypatch, tmp_path):
    """agent-pair: allowlisted artifact under a DIFFERENT batch's task dir → floor fires."""
    impl = _impl()
    repo, task_dir, sibling_dir = _make_stacked_repo(tmp_path)

    # Path under "other-batch", NOT the current batch "b"
    cross_dir = repo / ".redteam" / "batches" / "other-batch" / "tasks" / "task-001"
    cross_dir.mkdir(parents=True)
    cross_artifact = cross_dir / "state.json"
    cross_artifact.write_text("{}\n", encoding="utf-8")
    _git(repo, "add", cross_artifact.relative_to(repo).as_posix())

    invoked = {"yes": False}
    _wire_agent_pair(impl, monkeypatch, repo, on_invoke=lambda: invoked.__setitem__("yes", True))

    st = _state()
    (task_dir / "state.json").write_text(json.dumps(st), encoding="utf-8")

    result = impl._run_agent_pair(task_dir, st)

    assert result["status"] == "error"
    assert "commit or stash" in result["feedback"]
    assert "other-batch" in result["feedback"]
    assert invoked["yes"] is False


def test_cross_batch_allowlisted_still_trips_tdd(monkeypatch, tmp_path):
    """tdd: allowlisted artifact under a DIFFERENT batch's task dir → floor fires."""
    impl = _impl()
    repo, task_dir, sibling_dir = _make_stacked_repo(tmp_path)

    cross_dir = repo / ".redteam" / "batches" / "other-batch" / "tasks" / "task-001"
    cross_dir.mkdir(parents=True)
    cross_artifact = cross_dir / "state.json"
    cross_artifact.write_text("{}\n", encoding="utf-8")
    _git(repo, "add", cross_artifact.relative_to(repo).as_posix())

    invoked = {"yes": False}
    _wire_tdd(impl, monkeypatch, repo, on_invoke=lambda: invoked.__setitem__("yes", True))

    st = {"task_id": "task-002", "mode": "tdd", "base_branch": "main", "verification": {"commands": []}}
    (task_dir / "state.json").write_text(json.dumps(st), encoding="utf-8")

    result = impl.run(task_dir, st)

    assert result["status"] == "error"
    assert "commit or stash" in result["feedback"]
    assert "other-batch" in result["feedback"]
    assert invoked["yes"] is False


# =============================================================================
# Still-trips-on-root-level-out-of-scope-path (regression guard)
# =============================================================================


def test_root_level_out_of_scope_still_trips_agent_pair(monkeypatch, tmp_path):
    """agent-pair: root-level out-of-scope tracked path → floor fires (regression guard)."""
    impl = _impl()
    repo, task_dir, sibling_dir = _make_stacked_repo(tmp_path)

    (repo / "README.md").write_text("readme\n", encoding="utf-8")
    _git(repo, "add", "README.md")

    invoked = {"yes": False}
    _wire_agent_pair(impl, monkeypatch, repo, on_invoke=lambda: invoked.__setitem__("yes", True))

    st = _state()
    (task_dir / "state.json").write_text(json.dumps(st), encoding="utf-8")

    result = impl._run_agent_pair(task_dir, st)

    assert result["status"] == "error"
    assert "README.md" in result["feedback"]
    assert "commit or stash" in result["feedback"]
    assert invoked["yes"] is False


def test_root_level_out_of_scope_still_trips_tdd(monkeypatch, tmp_path):
    """tdd: root-level out-of-scope tracked path → floor fires (regression guard)."""
    impl = _impl()
    repo, task_dir, sibling_dir = _make_stacked_repo(tmp_path)

    (repo / "README.md").write_text("readme\n", encoding="utf-8")
    _git(repo, "add", "README.md")

    invoked = {"yes": False}
    _wire_tdd(impl, monkeypatch, repo, on_invoke=lambda: invoked.__setitem__("yes", True))

    st = {"task_id": "task-002", "mode": "tdd", "base_branch": "main", "verification": {"commands": []}}
    (task_dir / "state.json").write_text(json.dumps(st), encoding="utf-8")

    result = impl.run(task_dir, st)

    assert result["status"] == "error"
    assert "README.md" in result["feedback"]
    assert "commit or stash" in result["feedback"]
    assert invoked["yes"] is False


# =============================================================================
# #158 — sibling pr_url.txt, UNTRACKED, must not trip the cross-run trust-root floor
# =============================================================================
#
# The tests above stage their artifact, so they exercise _floor_outside_scope
# (the tracked path). The failure #158 actually reports comes from the other
# floor: create_pr writes pr_url.txt and never commits it, so in a stacked goal
# run the next task sees it as an UNTRACKED outside-scope path and
# _cross_run_trust_root_floor fails closed. Reproduce that shape specifically —
# the file is written and deliberately NOT added to the index.


def test_sibling_untracked_pr_url_does_not_trip_trust_root_floor(monkeypatch, tmp_path):
    """#158: untracked sibling pr_url.txt → trust-root floor does NOT fire.

    Fails against pre-change code: pr_url.txt was absent from
    _SIBLING_BASENAME_ALLOWLIST, so _is_harness_artifact returned False and the
    floor refused to invoke the worker.
    """
    impl = _impl()
    repo, task_dir, sibling_dir = _make_stacked_repo(tmp_path)

    # Written by create_pr, never staged — exactly how the real run leaves it.
    (sibling_dir / "pr_url.txt").write_text("https://github.com/AscendyProject/redteam/pull/171\n", encoding="utf-8")

    invoked = {"yes": False}
    _wire_agent_pair(impl, monkeypatch, repo, on_invoke=lambda: invoked.__setitem__("yes", True))

    st = _state()
    (task_dir / "state.json").write_text(json.dumps(st), encoding="utf-8")

    result = impl._run_agent_pair(task_dir, st)

    assert result["status"] == "approved", result.get("feedback", "")
    assert invoked["yes"] is True


def test_sibling_untracked_pr_url_exemption_is_basename_exact(monkeypatch, tmp_path):
    """#158: the exemption covers pr_url.txt and ONLY pr_url.txt.

    Both files are present, so the floor fires either way and a bare "does it
    fire" assertion could not tell the versions apart. What changes is the
    CONTENT of the offending list: pre-change it names both files, post-change
    it names only the .bak. Asserting the exact set therefore fails against
    pre-change code while still proving a near-miss basename stays fail-closed
    (a prefix or glob fix would wrongly drop the .bak from this list).
    """
    impl = _impl()
    repo, task_dir, sibling_dir = _make_stacked_repo(tmp_path)

    (sibling_dir / "pr_url.txt").write_text("https://example.invalid/pull/1\n", encoding="utf-8")
    (sibling_dir / "pr_url.txt.bak").write_text("https://example.invalid/pull/0\n", encoding="utf-8")

    invoked = {"yes": False}
    _wire_agent_pair(impl, monkeypatch, repo, on_invoke=lambda: invoked.__setitem__("yes", True))

    st = _state()
    (task_dir / "state.json").write_text(json.dumps(st), encoding="utf-8")

    result = impl._run_agent_pair(task_dir, st)

    assert result["status"] == "error"
    assert invoked["yes"] is False

    listed = result["feedback"].split("Offending paths: ", 1)[1].strip()
    offending = {entry.strip().rsplit("/", 1)[-1] for entry in listed.split(",") if entry.strip()}
    assert offending == {"pr_url.txt.bak"}, f"expected only the .bak to remain offending, got {offending}"
