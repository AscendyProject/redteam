from __future__ import annotations

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
    monkeypatch.setattr(implement, "compute_branch_diff", lambda cwd: "diff --git a/app/x.py b/app/x.py\n")

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


def _untracked_probe(argv):
    return "-c" in argv and "ls-files" in argv and "--others" in argv


def _tracked_probe(argv):
    return "-c" in argv and argv[3:5] == ["diff", "-z"]


def _state():
    return {"task_id": "task-001", "mode": "agent-pair", "verification": {"commands": []}}


def test_agent_pair_commits_tracked_and_new_untracked(monkeypatch, tmp_path):
    """The implementer creates a NEW (untracked) file during invoke; plain git diff
    doesn't see it, so the commit step must stage it via the before/after snapshot
    and commit it — otherwise it would be lost from the reviewed range."""
    implement = _load_implement_module()
    task_dir = tmp_path / "t"
    task_dir.mkdir()
    repo = tmp_path / "repo"
    repo.mkdir()
    phase = {"invoked": False, "staged_stdin": None}

    _wire(implement, monkeypatch, repo, on_invoke=lambda phase=phase: phase.__setitem__("invoked", True))

    def fake_run(argv, **kwargs):
        if _untracked_probe(argv):
            # nothing untracked before invoke; one new file after
            return _ok("app/new.py\0" if phase["invoked"] else "")
        if _tracked_probe(argv):
            return _ok("app/x.py\0")  # one tracked change
        if _is(argv, "--literal-pathspecs", "add"):
            phase["staged_stdin"] = kwargs.get("input")
            return _ok()
        if _is(argv, "diff", "--cached", "--quiet"):
            return SimpleNamespace(returncode=1, stdout="", stderr="")  # staged → commit proceeds
        if _is(argv, "commit"):
            return _ok()
        return _ok()  # integrity-gate probes: clean

    monkeypatch.setattr(implement.subprocess, "run", fake_run)
    result = implement._run_agent_pair(task_dir, _state())

    assert result["status"] == "approved"
    staged = set((phase["staged_stdin"] or "").split("\0")) - {""}
    assert staged == {"app/x.py", "app/new.py"}  # tracked change + the new untracked file


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
        if _is(argv, "diff", "--cached", "--quiet"):
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
        if _is(argv, "diff", "--cached", "--quiet"):
            return SimpleNamespace(returncode=1, stdout="", stderr="")
        if _is(argv, "commit"):
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
        if _is(argv, "diff", "--cached", "--quiet"):
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
        if _is(argv, "diff", "--cached", "--quiet"):
            return SimpleNamespace(returncode=1, stdout="", stderr="")
        if _is(argv, "commit"):
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
        if _is(argv, "diff", "--cached", "--quiet"):
            return SimpleNamespace(returncode=1, stdout="", stderr="")
        if _is(argv, "commit"):
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
