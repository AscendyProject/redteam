from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace


def _load_implement_module():
    workflows_path = Path(__file__).resolve().parents[1] / "workflows"
    if str(workflows_path) not in sys.path:
        sys.path.insert(0, str(workflows_path))
    spec = importlib.util.find_spec("phase_runners.implement")
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_agent_pair_implement_commits_patch_file_paths(monkeypatch, tmp_path):
    implement = _load_implement_module()
    task_dir = tmp_path / "batch" / "tasks" / "task-001-demo"
    task_dir.mkdir(parents=True)
    repo = tmp_path / "repo"
    repo.mkdir()
    diff = "\n".join(
        [
            "diff --git a/app/example.py b/app/example.py",
            "index 1111111..2222222 100644",
            "--- a/app/example.py",
            "+++ b/app/example.py",
            "@@ -1 +1 @@",
            "-old",
            "+new",
            "diff --git a/tests/test_example.py b/tests/test_example.py",
            "new file mode 100644",
            "index 0000000..3333333",
            "--- /dev/null",
            "+++ b/tests/test_example.py",
            "@@ -0,0 +1 @@",
            "+def test_example(): pass",
        ]
    )
    calls: list[list[str]] = []

    monkeypatch.setattr(implement, "repo_root", lambda: repo)
    # implement now invokes the worker via the adapter seam, not run_claude directly.
    monkeypatch.setattr(
        implement,
        "get_worker_adapter",
        lambda state: SimpleNamespace(invoke=lambda **kwargs: {"returncode": 0, "stdout": "done", "stderr": ""}),
    )
    monkeypatch.setattr(
        implement,
        "_run_verification_commands",
        lambda cwd, commands, project_verify_command=None, allowlist=None: (0, "ok\n"),
    )
    monkeypatch.setattr(implement, "compute_branch_diff", lambda cwd: diff)

    def fake_run(argv, **kwargs):
        calls.append(argv)
        if argv[:3] == ["git", "diff", "--cached"]:
            return SimpleNamespace(returncode=1, stdout="", stderr="")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(implement.subprocess, "run", fake_run)

    state = {
        "task_id": "task-001-demo",
        "mode": "agent-pair",
        "verification": {"commands": ["pytest .redteam/tests/test_run_claude_model.py -q"]},
    }

    result = implement._run_agent_pair(task_dir, state)

    assert result["status"] == "approved"
    assert state["implement_round_count"] == 1
    assert ["git", "add", "--", "app/example.py", "tests/test_example.py"] in calls
    assert ["git", "commit", "-m", "wip(task-001-demo): implement round 1"] in calls
    assert (task_dir / "impl_diff.patch").read_text(encoding="utf-8") == diff


def test_agent_pair_fails_closed_on_uncommitted_source_after_commit(monkeypatch, tmp_path):
    """#50: when verification passes but the scoped commit left a source/test file
    uncommitted, the committed range is stale — the phase must FAIL CLOSED (status
    error, naming the stray file) instead of returning approved into review_code."""
    implement = _load_implement_module()
    task_dir = tmp_path / "batch" / "tasks" / "task-001-demo"
    task_dir.mkdir(parents=True)
    repo = tmp_path / "repo"
    repo.mkdir()

    monkeypatch.setattr(implement, "repo_root", lambda: repo)
    monkeypatch.setattr(
        implement,
        "project_config",
        lambda: SimpleNamespace(source_dirs=["app/"], test_dir="tests/", context_file="docs/ctx.md"),
    )
    monkeypatch.setattr(
        implement,
        "get_worker_adapter",
        lambda state: SimpleNamespace(invoke=lambda **kwargs: {"returncode": 0, "stdout": "done", "stderr": ""}),
    )
    monkeypatch.setattr(
        implement,
        "_run_verification_commands",
        lambda cwd, commands, project_verify_command=None, allowlist=None: (0, "ok\n"),
    )
    monkeypatch.setattr(implement, "compute_branch_diff", lambda cwd: "diff --git a/app/example.py b/app/example.py\n")

    def fake_run(argv, **kwargs):
        if argv[:3] == ["git", "diff", "--cached"]:
            return SimpleNamespace(returncode=1, stdout="", stderr="")  # staged → commit proceeds
        if argv[:3] == ["git", "diff", "--name-only"]:
            # a tracked source file left modified-but-uncommitted after the scoped commit
            return SimpleNamespace(returncode=0, stdout="app/leftover.py\0", stderr="")
        if argv[:2] == ["git", "ls-files"]:
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(implement.subprocess, "run", fake_run)

    state = {"task_id": "task-001-demo", "mode": "agent-pair", "verification": {"commands": []}}
    result = implement._run_agent_pair(task_dir, state)

    assert result["status"] == "error"  # fail closed, NOT approved
    assert "app/leftover.py" in result["feedback"]  # names the stray file
    assert "stale" in result["feedback"].lower()


def test_agent_pair_fails_closed_on_staged_uncommitted_after_failed_commit(monkeypatch, tmp_path):
    """#50 round-2 (IR-001): _commit_agent_pair_diff ignores git commit's returncode,
    so a failed commit / hook can leave changes STAGED but not in HEAD — the committed
    range is still stale. The clean-check must catch staged-but-uncommitted source/test
    files (git diff --cached), not only unstaged/untracked."""
    implement = _load_implement_module()
    task_dir = tmp_path / "batch" / "tasks" / "task-001-demo"
    task_dir.mkdir(parents=True)
    repo = tmp_path / "repo"
    repo.mkdir()

    monkeypatch.setattr(implement, "repo_root", lambda: repo)
    monkeypatch.setattr(
        implement,
        "project_config",
        lambda: SimpleNamespace(source_dirs=["app/"], test_dir="tests/", context_file="docs/ctx.md"),
    )
    monkeypatch.setattr(
        implement,
        "get_worker_adapter",
        lambda state: SimpleNamespace(invoke=lambda **kwargs: {"returncode": 0, "stdout": "done", "stderr": ""}),
    )
    monkeypatch.setattr(
        implement,
        "_run_verification_commands",
        lambda cwd, commands, project_verify_command=None, allowlist=None: (0, "ok\n"),
    )
    monkeypatch.setattr(implement, "compute_branch_diff", lambda cwd: "diff --git a/app/example.py b/app/example.py\n")

    def fake_run(argv, **kwargs):
        if argv == ["git", "diff", "--cached", "--quiet"]:
            return SimpleNamespace(returncode=1, stdout="", stderr="")  # commit helper: staged exists
        if argv[:4] == ["git", "diff", "--cached", "--name-only"]:
            # staged but not landed in HEAD (commit failed): clean-check must see this
            return SimpleNamespace(returncode=0, stdout="app/staged_leftover.py\0", stderr="")
        if argv[:3] == ["git", "diff", "--name-only"]:
            return SimpleNamespace(returncode=0, stdout="", stderr="")  # nothing unstaged
        if argv[:2] == ["git", "ls-files"]:
            return SimpleNamespace(returncode=0, stdout="", stderr="")  # nothing untracked
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(implement.subprocess, "run", fake_run)

    state = {"task_id": "task-001-demo", "mode": "agent-pair", "verification": {"commands": []}}
    result = implement._run_agent_pair(task_dir, state)

    assert result["status"] == "error"  # fail closed on staged-but-uncommitted
    assert "app/staged_leftover.py" in result["feedback"]


def test_uncommitted_scope_files_ignores_artifacts_outside_source_dirs(monkeypatch, tmp_path):
    """The clean-check is restricted to source_dirs/test_dir, so harness artifacts and
    files outside those roots (e.g. impl_diff.patch, README.md) never trip it."""
    implement = _load_implement_module()
    repo = tmp_path / "repo"
    repo.mkdir()

    def fake_run(argv, **kwargs):
        if argv[:3] == ["git", "diff", "--name-only"]:
            return SimpleNamespace(returncode=0, stdout="impl_diff.patch\0README.md\0", stderr="")
        if argv[:2] == ["git", "ls-files"]:
            return SimpleNamespace(returncode=0, stdout="app/new_module.py\0", stderr="")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(implement.subprocess, "run", fake_run)
    proj = SimpleNamespace(source_dirs=["app/"], test_dir="tests/")

    stray = implement._uncommitted_scope_files(repo, proj)

    assert stray == ["app/new_module.py"]  # only the in-scope untracked source file
