from __future__ import annotations

import subprocess
from types import SimpleNamespace


def _load_implement_module():
    import _engine

    return _engine.implement()


_PROJ = SimpleNamespace(source_dirs=["app/"], test_dir="tests/", context_file="docs/ctx.md", base_branch="main")


def _wire(implement, monkeypatch, repo, *, on_invoke=None):
    """Common stubs: repo root, project config (with base_branch), a worker adapter
    whose invoke() may simulate the implementer creating files, and a passing verify."""
    monkeypatch.setattr(implement, "repo_root", lambda: repo)
    monkeypatch.setattr(implement, "project_config", lambda: _PROJ)
    # impl_diff.patch is regenerated via _branch_diff_checked → git (fake_run handles
    # the `git diff …` calls through its fall-through).

    def invoke(**kwargs):
        if on_invoke is not None:
            on_invoke()
        return {"returncode": 0, "stdout": "done", "stderr": ""}

    monkeypatch.setattr(implement, "get_worker_adapter", lambda state: SimpleNamespace(invoke=invoke))
    monkeypatch.setattr(
        implement,
        "_run_verification_commands",
        lambda cwd, commands, project_verify_command=None, allowlist=None: (0, "ok\n"),
    )


def _ok(stdout=""):
    return SimpleNamespace(returncode=0, stdout=stdout, stderr="")


def _is(argv, *parts):
    return argv[: len(parts) + 1] == ["git", *parts]


def _cached_quiet_probe(argv):
    # commit_paths now scopes the staged-changes probe to the named paths (#82):
    # `git --literal-pathspecs diff --cached --quiet -- <paths>`.
    return argv[:6] == ["git", "--literal-pathspecs", "diff", "--cached", "--quiet", "--"]


def _commit_cmd(argv):
    # commit_paths commits ONLY the named paths: `git --literal-pathspecs commit ... --only ...`.
    return _is(argv, "--literal-pathspecs", "commit")


def _untracked_probe(argv):
    return "-c" in argv and "ls-files" in argv and "--others" in argv


def _tracked_probe(argv):
    return "-c" in argv and argv[3:5] == ["diff", "-z"]


def _state():
    return {"task_id": "task-001", "mode": "agent-pair", "base_branch": "main", "verification": {"commands": []}}


def test_agent_pair_commits_tracked_and_new_untracked(monkeypatch, tmp_path):
    """The implementer creates a NEW (untracked) file during invoke; plain git diff
    doesn't see it, so the commit step must stage it via the before/after snapshot
    and commit it — otherwise it would be lost from the reviewed range."""
    implement = _load_implement_module()
    task_dir = tmp_path / "t"
    task_dir.mkdir()
    repo = tmp_path / "repo"
    repo.mkdir()
    phase = {"invoked": False, "staged_stdin": None, "add_argv": None}

    _wire(implement, monkeypatch, repo, on_invoke=lambda phase=phase: phase.__setitem__("invoked", True))

    def fake_run(argv, **kwargs):
        if _untracked_probe(argv):
            # nothing untracked before invoke; one new file after
            return _ok("app/new.py\0" if phase["invoked"] else "")
        if _tracked_probe(argv):
            return _ok("app/x.py\0")  # one tracked change
        if _is(argv, "--literal-pathspecs", "add"):
            phase["staged_stdin"] = kwargs.get("input")
            phase["add_argv"] = argv
            return _ok()
        if _cached_quiet_probe(argv):
            return SimpleNamespace(returncode=1, stdout="", stderr="")  # staged → commit proceeds
        if _commit_cmd(argv):
            return _ok()
        return _ok()  # integrity-gate probes: clean

    monkeypatch.setattr(implement.subprocess, "run", fake_run)
    result = implement._run_agent_pair(task_dir, _state())

    assert result["status"] == "approved"
    staged = set((phase["staged_stdin"] or "").split("\0")) - {""}
    assert staged == {"app/x.py", "app/new.py"}  # tracked change + the new untracked file
    # staged via the literal, NUL-delimited pathspec interface (not `add -- <paths>`)
    assert "--literal-pathspecs" in phase["add_argv"]
    assert "--pathspec-from-file=-" in phase["add_argv"]
    assert "--pathspec-file-nul" in phase["add_argv"]


def test_pre_existing_untracked_is_not_swept_into_the_commit(monkeypatch, tmp_path):
    """A file already untracked BEFORE the implementer ran is the user's, not the
    task's — current minus before excludes it, so it is never staged."""
    implement = _load_implement_module()
    task_dir = tmp_path / "t"
    task_dir.mkdir()
    repo = tmp_path / "repo"
    repo.mkdir()
    phase = {"staged_stdin": None}
    _wire(implement, monkeypatch, repo)

    def fake_run(argv, **kwargs):
        if _untracked_probe(argv):
            return _ok("app/preexisting.py\0")  # present BOTH before and after → delta is empty
        if _tracked_probe(argv):
            return _ok("app/x.py\0")
        if _is(argv, "--literal-pathspecs", "add"):
            phase["staged_stdin"] = kwargs.get("input")
            return _ok()
        if _cached_quiet_probe(argv):
            return SimpleNamespace(returncode=1, stdout="", stderr="")
        return _ok()

    monkeypatch.setattr(implement.subprocess, "run", fake_run)
    result = implement._run_agent_pair(task_dir, _state())

    assert result["status"] == "approved"
    staged = set((phase["staged_stdin"] or "").split("\0")) - {""}
    assert staged == {"app/x.py"}  # tracked change only; the pre-existing untracked file is excluded


def test_pure_new_file_change_still_commits(monkeypatch, tmp_path):
    """No tracked modifications, only a brand-new untracked file — the old
    patch-header path list would be empty and skip the commit; the snapshot path
    must still stage + commit the new file."""
    implement = _load_implement_module()
    task_dir = tmp_path / "t"
    task_dir.mkdir()
    repo = tmp_path / "repo"
    repo.mkdir()
    phase = {"invoked": False, "committed": False}
    _wire(implement, monkeypatch, repo, on_invoke=lambda phase=phase: phase.__setitem__("invoked", True))

    def fake_run(argv, **kwargs):
        if _untracked_probe(argv):
            return _ok("app/brand_new.py\0" if phase["invoked"] else "")
        if _tracked_probe(argv):
            return _ok("")  # NO tracked changes
        if _is(argv, "--literal-pathspecs", "add"):
            return _ok()
        if _cached_quiet_probe(argv):
            return SimpleNamespace(returncode=1, stdout="", stderr="")
        if _commit_cmd(argv):
            phase["committed"] = True
            return _ok()
        return _ok()

    monkeypatch.setattr(implement.subprocess, "run", fake_run)
    result = implement._run_agent_pair(task_dir, _state())

    assert result["status"] == "approved"
    assert phase["committed"] is True  # committed despite no tracked diff


def test_fails_closed_when_git_add_fails(monkeypatch, tmp_path):
    """A failed `git add` must fail the phase closed, not proceed to review on an
    incompletely-staged tree."""
    implement = _load_implement_module()
    task_dir = tmp_path / "t"
    task_dir.mkdir()
    repo = tmp_path / "repo"
    repo.mkdir()
    phase = {"invoked": False}
    _wire(implement, monkeypatch, repo, on_invoke=lambda phase=phase: phase.__setitem__("invoked", True))

    def fake_run(argv, **kwargs):
        if _untracked_probe(argv):
            return _ok("app/new.py\0" if phase["invoked"] else "")
        if _tracked_probe(argv):
            return _ok("app/x.py\0")
        if _is(argv, "--literal-pathspecs", "add"):
            return SimpleNamespace(returncode=128, stdout="", stderr="fatal: ...")  # add fails
        return _ok()

    monkeypatch.setattr(implement.subprocess, "run", fake_run)
    result = implement._run_agent_pair(task_dir, _state())

    assert result["status"] == "error"
    assert "could not commit" in result["feedback"]
    assert "fatal" not in result["feedback"]  # stderr (possible secrets) not leaked


def test_fails_closed_when_a_staging_probe_fails(monkeypatch, tmp_path):
    """If a tracked-path probe itself errors, the staging set is incomplete — fail
    closed rather than committing a partial set."""
    implement = _load_implement_module()
    task_dir = tmp_path / "t"
    task_dir.mkdir()
    repo = tmp_path / "repo"
    repo.mkdir()
    _wire(implement, monkeypatch, repo)

    def fake_run(argv, **kwargs):
        if _untracked_probe(argv):
            return _ok("")
        if _tracked_probe(argv):
            return SimpleNamespace(returncode=128, stdout="", stderr="fatal: index.lock")  # probe errors
        return _ok()

    monkeypatch.setattr(implement.subprocess, "run", fake_run)
    result = implement._run_agent_pair(task_dir, _state())

    assert result["status"] == "error"
    assert "could not commit" in result["feedback"]
    assert "index.lock" not in result["feedback"]


def test_fails_closed_when_cached_quiet_returns_error(monkeypatch, tmp_path):
    """`git diff --cached --quiet` returns 0 (no diff) or 1 (diff); anything else is
    a git error and must not be read as 'changes present' → fail closed."""
    implement = _load_implement_module()
    task_dir = tmp_path / "t"
    task_dir.mkdir()
    repo = tmp_path / "repo"
    repo.mkdir()
    phase = {"invoked": False}
    _wire(implement, monkeypatch, repo, on_invoke=lambda phase=phase: phase.__setitem__("invoked", True))

    def fake_run(argv, **kwargs):
        if _untracked_probe(argv):
            return _ok("app/new.py\0" if phase["invoked"] else "")
        if _tracked_probe(argv):
            return _ok("app/x.py\0")
        if _is(argv, "--literal-pathspecs", "add"):
            return _ok()
        if _cached_quiet_probe(argv):
            return SimpleNamespace(returncode=128, stdout="", stderr="fatal")  # neither 0 nor 1
        return _ok()

    monkeypatch.setattr(implement.subprocess, "run", fake_run)
    result = implement._run_agent_pair(task_dir, _state())

    assert result["status"] == "error"
    assert "could not commit" in result["feedback"]


# ---- integrity gate (#50) — preserved as defense-in-depth ----


def test_integrity_gate_fails_closed_on_uncommitted_source_after_commit(monkeypatch, tmp_path):
    """If, after the commit, a source/test file is still uncommitted, the reviewed
    range would be stale — fail closed naming the stray file."""
    implement = _load_implement_module()
    task_dir = tmp_path / "t"
    task_dir.mkdir()
    repo = tmp_path / "repo"
    repo.mkdir()
    phase = {"invoked": False}
    _wire(implement, monkeypatch, repo, on_invoke=lambda phase=phase: phase.__setitem__("invoked", True))

    def fake_run(argv, **kwargs):
        if _untracked_probe(argv):
            return _ok("app/new.py\0" if phase["invoked"] else "")
        if _tracked_probe(argv):
            return _ok("app/x.py\0")
        if _is(argv, "--literal-pathspecs", "add"):
            return _ok()
        if _cached_quiet_probe(argv):
            return SimpleNamespace(returncode=1, stdout="", stderr="")
        if _commit_cmd(argv):
            return _ok()
        # integrity gate probes (no -c prefix): a tracked source file left unstaged
        if _is(argv, "diff", "--cached", "--name-only"):
            return _ok("")
        if _is(argv, "diff", "--name-only"):
            return _ok("app/leftover.py\0")
        if _is(argv, "ls-files"):
            return _ok("")
        return _ok()

    monkeypatch.setattr(implement.subprocess, "run", fake_run)
    result = implement._run_agent_pair(task_dir, _state())

    assert result["status"] == "error"
    assert "app/leftover.py" in result["feedback"]
    assert "stale" in result["feedback"].lower()


def test_integrity_gate_fails_closed_when_a_probe_fails(monkeypatch, tmp_path):
    """A failed integrity probe (empty stdout) must NOT read as 'clean'."""
    implement = _load_implement_module()
    task_dir = tmp_path / "t"
    task_dir.mkdir()
    repo = tmp_path / "repo"
    repo.mkdir()
    phase = {"invoked": False}
    _wire(implement, monkeypatch, repo, on_invoke=lambda phase=phase: phase.__setitem__("invoked", True))

    def fake_run(argv, **kwargs):
        if _untracked_probe(argv):
            return _ok("app/new.py\0" if phase["invoked"] else "")
        if _tracked_probe(argv):
            return _ok("app/x.py\0")
        if _is(argv, "--literal-pathspecs", "add"):
            return _ok()
        if _cached_quiet_probe(argv):
            return SimpleNamespace(returncode=1, stdout="", stderr="")
        if _commit_cmd(argv):
            return _ok()
        if _is(argv, "diff", "--name-only") and "--cached" not in argv:
            return SimpleNamespace(returncode=128, stdout="", stderr="fatal: index.lock exists")
        return _ok()

    monkeypatch.setattr(implement.subprocess, "run", fake_run)
    result = implement._run_agent_pair(task_dir, _state())

    assert result["status"] == "error"
    assert "could not verify commit integrity" in result["feedback"]
    assert "index.lock" not in result["feedback"]


def test_uncommitted_scope_files_ignores_artifacts_outside_source_dirs(monkeypatch, tmp_path):
    """The integrity check is restricted to source_dirs/test_dir, so harness artifacts
    and out-of-scope files (impl_diff.patch, README.md) never trip it."""
    implement = _load_implement_module()
    repo = tmp_path / "repo"
    repo.mkdir()

    def fake_run(argv, **kwargs):
        if _is(argv, "diff", "--name-only") and "--cached" not in argv:
            return _ok("impl_diff.patch\0README.md\0")
        if _is(argv, "ls-files"):
            return _ok("app/new_module.py\0")
        return _ok()

    monkeypatch.setattr(implement.subprocess, "run", fake_run)
    proj = SimpleNamespace(source_dirs=["app/"], test_dir="tests/")
    stray = implement._uncommitted_scope_files(repo, proj)
    assert stray == ["app/new_module.py"]


def test_task_dir_artifacts_are_excluded_from_the_commit(monkeypatch, tmp_path):
    """Harness artifacts under the task dir must never be staged — even a TRACKED
    one (the filter applies to the whole stage set, tracked + new untracked)."""
    implement = _load_implement_module()
    repo = tmp_path / "repo"
    repo.mkdir()
    task_dir = repo / ".redteam" / "batches" / "b" / "tasks" / "task-001"
    task_dir.mkdir(parents=True)
    phase = {"invoked": False, "staged_stdin": None}
    _wire(implement, monkeypatch, repo, on_invoke=lambda phase=phase: phase.__setitem__("invoked", True))

    def fake_run(argv, **kwargs):
        if _untracked_probe(argv):
            # after invoke: a new source file AND a new task-dir artifact (impl_diff.patch)
            return _ok("app/new.py\0.redteam/batches/b/tasks/task-001/impl_diff.patch\0" if phase["invoked"] else "")
        if _tracked_probe(argv):
            # a tracked source change AND a tracked task-dir artifact
            return _ok("app/x.py\0.redteam/batches/b/tasks/task-001/outcome.md\0")
        if _is(argv, "--literal-pathspecs", "add"):
            phase["staged_stdin"] = kwargs.get("input")
            return _ok()
        if _cached_quiet_probe(argv):
            return SimpleNamespace(returncode=1, stdout="", stderr="")
        return _ok()

    monkeypatch.setattr(implement.subprocess, "run", fake_run)
    result = implement._run_agent_pair(task_dir, _state())

    assert result["status"] == "approved"
    staged = set((phase["staged_stdin"] or "").split("\0")) - {""}
    assert staged == {"app/x.py", "app/new.py"}  # both task-dir artifacts excluded


def test_fails_closed_when_pre_invoke_snapshot_raises_oserror(monkeypatch, tmp_path):
    """A process-launch failure (git missing) raises OSError, not RuntimeError — the
    pre-invoke snapshot must fail closed and never invoke the worker."""
    implement = _load_implement_module()
    task_dir = tmp_path / "t"
    task_dir.mkdir()
    repo = tmp_path / "repo"
    repo.mkdir()
    invoked = {"yes": False}
    monkeypatch.setattr(implement, "repo_root", lambda: repo)
    monkeypatch.setattr(implement, "project_config", lambda: _PROJ)

    def invoke(**kwargs):
        invoked["yes"] = True
        return {"returncode": 0, "stdout": "", "stderr": ""}

    monkeypatch.setattr(implement, "get_worker_adapter", lambda state: SimpleNamespace(invoke=invoke))

    def boom(argv, **kwargs):
        raise OSError("git: command not found")

    monkeypatch.setattr(implement.subprocess, "run", boom)
    result = implement._run_agent_pair(task_dir, _state())

    assert result["status"] == "error"
    assert invoked["yes"] is False  # fail closed BEFORE running the worker


# ---- real-git integration (not mocked) ----


def _git(repo, *args):
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True)


def test_real_git_commits_new_untracked_and_excludes_task_artifacts(monkeypatch, tmp_path):
    """End-to-end against a REAL git repo (no subprocess mocking): the implementer
    creates a NEW untracked test file and modifies a tracked file; both must land in
    the committed range `base...HEAD`, while the harness's own task-dir artifacts
    (impl_diff.patch, verification.log) must NOT be committed."""
    implement = _load_implement_module()
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "t@e")
    _git(repo, "config", "user.name", "t")
    (repo / "app").mkdir()
    (repo / "app" / "existing.py").write_text("x = 1\n", encoding="utf-8")
    (repo / "tests").mkdir()
    (repo / "tests" / ".gitkeep").write_text("", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "init")
    _git(repo, "checkout", "-b", "redteam/task-001")

    task_dir = repo / ".redteam" / "batches" / "b" / "tasks" / "task-001"
    task_dir.mkdir(parents=True)

    monkeypatch.setattr(implement, "repo_root", lambda: repo)
    monkeypatch.setattr(
        implement,
        "project_config",
        lambda: SimpleNamespace(source_dirs=["app/"], test_dir="tests/", context_file="c", base_branch="main"),
    )

    def invoke(**kwargs):
        (repo / "tests" / "test_new.py").write_text("def test_x():\n    assert True\n", encoding="utf-8")
        (repo / "app" / "existing.py").write_text("x = 2\n", encoding="utf-8")
        # a NEW file outside source_dirs/test_dir (e.g. a migration) must still commit —
        # the snapshot stages new files anywhere, not just within scoped roots.
        (repo / "migrations").mkdir()
        (repo / "migrations" / "0001_init.sql").write_text("-- up\n", encoding="utf-8")
        return {"returncode": 0, "stdout": "done", "stderr": ""}

    monkeypatch.setattr(implement, "get_worker_adapter", lambda state: SimpleNamespace(invoke=invoke))
    monkeypatch.setattr(
        implement,
        "_run_verification_commands",
        lambda cwd, commands, project_verify_command=None, allowlist=None: (0, "ok\n"),
    )

    result = implement._run_agent_pair(task_dir, _state())
    assert result["status"] == "approved", result["feedback"]

    committed = subprocess.run(
        ["git", "diff", "--name-only", "main...HEAD"], cwd=repo, capture_output=True, text=True
    ).stdout.split("\n")
    assert "tests/test_new.py" in committed  # the NEW untracked file is in the reviewed range
    assert "app/existing.py" in committed  # the tracked modification too
    assert "migrations/0001_init.sql" in committed  # a new file OUTSIDE source/test scope, too
    # the harness's own scratch artifacts were NOT swept into the task commit
    assert not any(c.startswith(".redteam/batches/") for c in committed if c)


def test_real_git_tdd_implement_commits_out_of_root_file_into_committed_range(monkeypatch, tmp_path):
    """#82 full fix: tdd `implement` now truly COMMITS. Against a REAL git repo: with
    the test already committed by write_test, the implementer creates source AND a file
    OUTSIDE the source/test roots (a migration); both must land in the committed range
    `base...HEAD`, impl_diff.patch is the committed-only diff, and the task-dir
    artifacts are NOT committed."""
    implement = _load_implement_module()
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "t@e")
    _git(repo, "config", "user.name", "t")
    (repo / "app").mkdir()
    (repo / "app" / "existing.py").write_text("x = 1\n", encoding="utf-8")
    (repo / "tests").mkdir()
    (repo / "tests" / ".gitkeep").write_text("", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "init")
    _git(repo, "checkout", "-b", "redteam/task-001")
    # write_test already committed the test on this branch.
    (repo / "tests" / "test_feature.py").write_text("def test_x():\n    assert True\n", encoding="utf-8")
    _git(repo, "add", "tests/test_feature.py")
    _git(repo, "commit", "-m", "test(task-001): write tests")

    task_dir = repo / ".redteam" / "batches" / "b" / "tasks" / "task-001"
    task_dir.mkdir(parents=True)

    monkeypatch_proj = SimpleNamespace(
        source_dirs=["app/"],
        test_dir="tests/",
        context_file="c",
        base_branch="main",
        verify_command="true",
        verification_allowlist=("true",),
    )
    import config as _config

    monkeypatch.setattr(_config, "load_config", lambda *_a, **_k: SimpleNamespace(project=monkeypatch_proj))
    monkeypatch.setattr(implement, "repo_root", lambda: repo)
    monkeypatch.setattr(implement, "project_config", lambda: monkeypatch_proj)
    monkeypatch.setattr(implement, "_run_verify_sh", lambda cwd, argv: (0, "ok\n"))

    def invoke(**kwargs):
        (repo / "app" / "existing.py").write_text("x = 2\n", encoding="utf-8")
        (repo / "migrations").mkdir()
        (repo / "migrations" / "0001_init.sql").write_text("-- up\n", encoding="utf-8")
        return {"returncode": 0, "stdout": "done", "stderr": ""}

    monkeypatch.setattr(implement, "get_worker_adapter", lambda state: SimpleNamespace(invoke=invoke))

    result = implement.run(
        task_dir, {"task_id": "task-001", "mode": "tdd", "base_branch": "main", "verification": {"commands": []}}
    )
    assert result["status"] == "approved", result["feedback"]
    committed = subprocess.run(
        ["git", "diff", "--name-only", "main...HEAD"], cwd=repo, capture_output=True, text=True
    ).stdout.split("\n")
    assert "tests/test_feature.py" in committed  # the write_test commit
    assert "app/existing.py" in committed  # implement's source change
    assert "migrations/0001_init.sql" in committed  # a NEW file OUTSIDE source/test roots
    assert not any(c.startswith(".redteam/batches/") for c in committed if c)
    # impl_diff.patch is the committed-only range (no uncommitted phantom content).
    patch_text = (task_dir / "impl_diff.patch").read_text(encoding="utf-8")
    assert "migrations/0001_init.sql" in patch_text and "app/existing.py" in patch_text
