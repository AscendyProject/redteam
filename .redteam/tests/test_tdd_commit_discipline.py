"""#82 full fix — tdd cross-phase commit discipline.

write_test commits the worker's test(s) via strict task-owned attribution (never the
operator's tracked mods), persists a validated `tdd_test_files` manifest, and verify_test
reviews via that manifest (fail-closed if absent). Real-git integration where it matters.

All module patching goes through `monkeypatch` so it auto-reverts — the engine modules
are shared singletons (#54), and a leaked attribute would pollute later tests.
"""

from __future__ import annotations

import subprocess
from types import SimpleNamespace

import pytest


def _base():
    import _engine

    return _engine.base()


def _write_test():
    import _engine

    return _engine.write_test()


def _verify_test():
    import _engine

    return _engine.verify_test()


def _git(repo, *args):
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True)


def _proj():
    return SimpleNamespace(
        source_dirs=["app/"],
        test_dir="tests/",
        test_file_glob="test_*.py",
        test_conventions_file="tc.md",
        context_file="c.md",
        base_branch="main",
    )


def _repo(tmp_path):
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
    (repo / "td").mkdir()
    return repo


def _wire(wt, mp, repo, on_invoke):
    mp.setattr(wt, "repo_root", lambda: repo)
    mp.setattr(wt, "project_config", lambda: _proj())
    mp.setattr(
        wt,
        "get_worker_adapter",
        lambda state: SimpleNamespace(
            invoke=lambda **kw: (on_invoke(), {"returncode": 0, "stdout": "ok", "stderr": ""})[1]
        ),
    )


def _committed(repo):
    return subprocess.run(
        ["git", "diff", "--name-only", "main...HEAD"], cwd=repo, capture_output=True, text=True
    ).stdout.split("\n")


def test_write_test_commits_worker_test_and_sets_manifest(monkeypatch, tmp_path):
    wt = _write_test()
    repo = _repo(tmp_path)

    def invoke():
        (repo / "tests" / "test_feature.py").write_text("def test_x():\n    assert False\n", encoding="utf-8")

    _wire(wt, monkeypatch, repo, invoke)
    state = {"task_id": "task-001", "base_branch": "main"}
    res = wt.run(repo / "td", state)
    assert res["status"] == "approved", res["feedback"]
    assert state["tdd_test_files"] == ["tests/test_feature.py"]
    assert "tests/test_feature.py" in _committed(repo)  # truly committed on the task branch


def test_write_test_does_not_sweep_operator_changes(monkeypatch, tmp_path):
    """Operator's PRE-EXISTING untracked test AND an unstaged edit to a tracked test
    (committed on the base, not the task branch) are NOT swept into the task commit —
    only the worker's just-created test is."""
    # operator-owned tracked test lives on BASE (main), before the task branch
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "t@e")
    _git(repo, "config", "user.name", "t")
    (repo / "tests").mkdir()
    (repo / "tests" / "test_operator.py").write_text("def test_op():\n    assert True\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "operator test on main")
    _git(repo, "checkout", "-b", "redteam/task-001")
    (repo / "td").mkdir()
    # operator's pre-existing dirty state on the task branch: an unstaged edit + an untracked scratch test
    (repo / "tests" / "test_operator.py").write_text(
        "def test_op():\n    assert False  # operator wip\n", encoding="utf-8"
    )
    (repo / "tests" / "test_scratch.py").write_text("def test_scratch():\n    pass\n", encoding="utf-8")

    wt = _write_test()

    def invoke():
        (repo / "tests" / "test_feature.py").write_text("def test_x():\n    assert False\n", encoding="utf-8")

    _wire(wt, monkeypatch, repo, invoke)
    state = {"task_id": "task-001", "base_branch": "main"}
    res = wt.run(repo / "td", state)
    assert res["status"] == "approved", res["feedback"]
    assert state["tdd_test_files"] == ["tests/test_feature.py"]
    # the committed range touches ONLY the worker test, not the operator's
    assert [c for c in _committed(repo) if c] == ["tests/test_feature.py"]
    # operator's wip edit is still uncommitted, scratch still untracked
    assert "operator wip" in (repo / "tests" / "test_operator.py").read_text(encoding="utf-8")
    status = subprocess.run(["git", "status", "--short"], cwd=repo, capture_output=True, text=True).stdout
    assert "test_scratch.py" in status and "test_operator.py" in status


def test_write_test_retry_recommits_edited_tracked_test(monkeypatch, tmp_path):
    """A verify_test→write_test retry: the worker edits the now-tracked test; the prior
    manifest carries identity so the edit is staged + committed (not a 'no untracked' error)."""
    wt = _write_test()
    repo = _repo(tmp_path)

    _wire(wt, monkeypatch, repo, lambda: (repo / "tests" / "test_feature.py").write_text("v1\n", encoding="utf-8"))
    state = {"task_id": "task-001", "base_branch": "main"}
    wt.run(repo / "td", state)

    # retry: test is now tracked/committed; the worker edits it in place
    monkeypatch.setattr(
        wt,
        "get_worker_adapter",
        lambda s: SimpleNamespace(
            invoke=lambda **kw: (
                (repo / "tests" / "test_feature.py").write_text("v2 revised\n", encoding="utf-8"),
                {"returncode": 0, "stdout": "ok", "stderr": ""},
            )[1]
        ),
    )
    res = wt.run(repo / "td", state)
    assert res["status"] == "approved", res["feedback"]
    head = subprocess.run(
        ["git", "show", "HEAD:tests/test_feature.py"], cwd=repo, capture_output=True, text=True
    ).stdout
    assert head == "v2 revised\n"


def test_write_test_recovers_committed_test_when_manifest_lost(monkeypatch, tmp_path):
    """Crash between the write_test commit and state-save: re-run with an EMPTY manifest
    recovers the committed test via `committed_tests` rather than failing."""
    wt = _write_test()
    repo = _repo(tmp_path)
    (repo / "tests" / "test_feature.py").write_text("v1\n", encoding="utf-8")
    _git(repo, "add", "tests/test_feature.py")
    _git(repo, "commit", "-m", "test(task-001): write tests")  # committed, but state lost it

    _wire(wt, monkeypatch, repo, lambda: None)  # worker does nothing this run
    state = {"task_id": "task-001", "base_branch": "main"}  # no tdd_test_files
    res = wt.run(repo / "td", state)
    assert res["status"] == "approved", res["feedback"]
    assert state["tdd_test_files"] == ["tests/test_feature.py"]


def test_write_test_commits_deletion_and_drops_from_manifest(monkeypatch, tmp_path):
    """A worker rename/delete of a prior task test: the deletion is committed and the
    path drops out of the persisted manifest (the live set)."""
    wt = _write_test()
    repo = _repo(tmp_path)
    (repo / "tests" / "test_old.py").write_text("old\n", encoding="utf-8")
    _git(repo, "add", "tests/test_old.py")
    _git(repo, "commit", "-m", "test(task-001): write tests")

    def invoke():
        (repo / "tests" / "test_old.py").unlink()
        (repo / "tests" / "test_new.py").write_text("new\n", encoding="utf-8")

    _wire(wt, monkeypatch, repo, invoke)
    state = {"task_id": "task-001", "base_branch": "main", "tdd_test_files": ["tests/test_old.py"]}
    res = wt.run(repo / "td", state)
    assert res["status"] == "approved", res["feedback"]
    assert state["tdd_test_files"] == ["tests/test_new.py"]
    assert "tests/test_new.py" in _committed(repo)
    assert not (repo / "tests" / "test_old.py").exists()
    # the old path's deletion is recorded at HEAD
    tracked = subprocess.run(["git", "ls-files", "tests/"], cwd=repo, capture_output=True, text=True).stdout
    assert "test_old.py" not in tracked


def test_write_test_ignores_operator_test_commit_on_reused_branch(monkeypatch, tmp_path):
    """#82 IR-007: a first run (empty manifest) on a REUSED branch that already has an
    operator-committed test in base..HEAD must NOT attribute it — committed_tests is
    scoped to the harness's own `test(<task_id>): write tests` commits, so only the
    worker's freshly-created test is committed and recorded."""
    wt = _write_test()
    repo = _repo(tmp_path)
    # operator committed their own test on this (reused) task branch, beyond base —
    # including a SUBJECT that is a superstring of the harness message (IR-008: a
    # substring `--grep` would have matched this) and one carrying the text in the body.
    (repo / "tests" / "test_operator.py").write_text("def test_op():\n    assert True\n", encoding="utf-8")
    _git(repo, "add", "tests/test_operator.py")
    _git(repo, "commit", "-m", "test(task-001): write tests manually")
    (repo / "tests" / "test_operator2.py").write_text("def test_op2():\n    assert True\n", encoding="utf-8")
    _git(repo, "add", "tests/test_operator2.py")
    _git(repo, "commit", "-m", "operator change\n\nbody mentions test(task-001): write tests verbatim")
    # IR-009: a subject that embeds the record/field delimiters can't forge a match
    # (NUL framing — a git subject can't contain NUL).
    (repo / "tests" / "test_operator3.py").write_text("def test_op3():\n    assert True\n", encoding="utf-8")
    _git(repo, "add", "tests/test_operator3.py")
    _git(repo, "commit", "-m", "test(task-001): write tests\x1e\x1fforged")

    def invoke():
        (repo / "tests" / "test_feature.py").write_text("def test_x():\n    assert False\n", encoding="utf-8")

    _wire(wt, monkeypatch, repo, invoke)
    state = {"task_id": "task-001", "base_branch": "main"}  # empty manifest (first run)
    res = wt.run(repo / "td", state)
    assert res["status"] == "approved", res["feedback"]
    # only the worker test is attributed/committed; the operator test is left as-is
    assert state["tdd_test_files"] == ["tests/test_feature.py"]
    last = subprocess.run(
        ["git", "show", "--name-only", "--format=", "HEAD"], cwd=repo, capture_output=True, text=True
    ).stdout.split()
    assert last == ["tests/test_feature.py"]


def test_write_test_rejects_malformed_manifest(monkeypatch, tmp_path):
    wt = _write_test()
    repo = _repo(tmp_path)
    monkeypatch.setattr(wt, "repo_root", lambda: repo)
    monkeypatch.setattr(wt, "project_config", lambda: _proj())
    bads = (
        ["../evil.py"],
        ["/abs/test_x.py"],
        ["app/test_x.py"],
        ["tests\\test_x.py"],
        ["tests/sub/../../app/test_x.py"],  # raw '..' segment (escapes after normpath)
        ["tests/test_ok.py\0app/test_evil.py"],  # NUL injection — would split into 2 pathspecs
    )
    for bad in bads:
        res = wt.run(repo / "td", {"task_id": "t", "base_branch": "main", "tdd_test_files": bad})
        assert res["status"] == "error", bad
        assert "tdd_test_files" in res["feedback"]


def test_write_test_does_not_sweep_pre_staged_operator_file(monkeypatch, tmp_path):
    """A pre-existing STAGED operator change elsewhere in the index is NOT swept into
    the test commit (commit_paths is `--only` the named paths)."""
    wt = _write_test()
    repo = _repo(tmp_path)
    (repo / "app" / "operator.py").write_text("operator staged\n", encoding="utf-8")
    _git(repo, "add", "app/operator.py")  # operator pre-stages an unrelated file

    def invoke():
        (repo / "tests" / "test_feature.py").write_text("def test_x():\n    assert False\n", encoding="utf-8")

    _wire(wt, monkeypatch, repo, invoke)
    state = {"task_id": "task-001", "base_branch": "main"}
    res = wt.run(repo / "td", state)
    assert res["status"] == "approved", res["feedback"]
    assert [c for c in _committed(repo) if c] == ["tests/test_feature.py"]  # operator.py NOT committed
    staged = subprocess.run(["git", "diff", "--cached", "--name-only"], cwd=repo, capture_output=True, text=True).stdout
    assert "app/operator.py" in staged  # still staged, untouched


def test_noncanonical_test_root_is_consistent_between_validation_and_discovery(tmp_path):
    """#82 IR-006: a non-canonical config test_dir is normalized via ONE shared helper,
    so manifest validation and write_test discovery agree on which paths are tests."""
    base = _base()
    wt = _write_test()
    proj = SimpleNamespace(test_dir="tests/../spec", test_file_glob="test_*.py")
    root = base.normalized_test_root(proj)
    assert root == "spec/"
    # validation accepts a path under the NORMALIZED root and rejects one under the raw prefix
    assert base.valid_tdd_test_files(["spec/test_x.py"], proj) == ["spec/test_x.py"]
    with pytest.raises(ValueError):
        base.valid_tdd_test_files(["tests/test_x.py"], proj)
    # discovery uses the SAME predicate as validation → same verdict
    assert wt._is_test_path("spec/test_x.py", proj) is True
    assert wt._is_test_path("tests/test_x.py", proj) is False
    assert wt._is_test_path("spec\\test_x.py", proj) is False  # backslash rejected by both


def test_verify_test_fails_closed_without_manifest(monkeypatch, tmp_path):
    vt = _verify_test()
    repo = _repo(tmp_path)
    monkeypatch.setattr(vt, "repo_root", lambda: repo)
    monkeypatch.setattr(vt, "project_config", lambda: _proj())
    res = vt.run(repo / "td", {"task_id": "t"})  # no tdd_test_files
    assert res["status"] == "error"
    assert "manifest is missing" in res["feedback"]


def test_verify_test_prompt_names_manifest_paths(monkeypatch, tmp_path):
    vt = _verify_test()
    repo = _repo(tmp_path)
    captured = {}
    monkeypatch.setattr(vt, "repo_root", lambda: repo)
    monkeypatch.setattr(vt, "project_config", lambda: _proj())
    monkeypatch.setattr(vt, "compute_repo_diff", lambda cwd=None: "")
    monkeypatch.setattr(
        vt,
        "get_worker_adapter",
        lambda s: SimpleNamespace(
            invoke=lambda **kw: (
                captured.__setitem__("p", kw["prompt"]),
                {"returncode": 0, "stdout": "", "stderr": ""},
            )[1]
        ),
    )
    vt.run(repo / "td", {"task_id": "t", "tdd_test_files": ["tests/test_feature.py"]})
    assert "tests/test_feature.py" in captured["p"]
    assert "git status" not in captured["p"]  # no worktree-status heuristic


if __name__ == "__main__":
    pytest.main([__file__, "-q"])
