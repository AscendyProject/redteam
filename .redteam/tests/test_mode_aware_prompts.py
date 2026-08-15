"""Phase prompts don't assume a TDD test-author/verifier ran in agent-pair (#73).

The default mode is agent-pair (no write_test/verify_test — the implementer writes
the tests inside implement). These pin that:
- the agent-pair implement prompt tells the implementer to WRITE the planned tests
  and not touch pre-existing ones, and treats plan_review.md as optional (a
  review=false tier produces none);
- the tdd implement prompt keeps the upstream-test-author contract;
- the pr-author prompt is mode/tier-neutral — it reads the diff and treats the
  review/test artifacts as optional, never naming a "test-author" file.
"""

from __future__ import annotations

import sys
from pathlib import Path


def _impl():
    import _engine

    return _engine.implement()


def _create_pr():
    import _engine

    return _engine.create_pr()


class _Proj:
    context_file = ".redteam/docs/project-context.md"
    source_dirs = ("src/",)
    test_dir = "tests/"
    base_branch = "main"
    test_conventions_file = ".redteam/docs/test-conventions.md"


_TD = Path("/tmp/batch/tasks/task-001")


# ---- agent-pair implement prompt ----


def test_agent_pair_prompt_assigns_test_authoring_and_protects_existing():
    p = _impl()._agent_pair_base_prompt(_TD, _Proj())
    assert "Mode: agent-pair" in p
    assert "YOU write the tests" in p
    assert "Do not modify, delete, or rename any pre-existing test" in p
    # agent-pair has no upstream test-author artifacts
    assert "test_review.md" not in p
    assert "test-author" not in p


def test_agent_pair_prompt_does_not_hard_require_plan_review():
    """review=false tier runs plan_outcome→implement with no plan_review.md — the
    prompt must treat it as optional, not a required input."""
    p = _impl()._agent_pair_base_prompt(_TD, _Proj())
    assert "that are present" in p
    assert "without a plan review" in p


# ---- tdd implement prompt ----


def test_tdd_prompt_keeps_upstream_test_author_contract():
    p = _impl()._tdd_base_prompt(_TD, _Proj())
    assert "Mode: tdd" in p
    assert "test_review.md" in p
    assert "the test file the test-author created" in p


# ---- pr-author prompt (mode/tier-neutral) ----


def test_pr_author_prompt_is_mode_neutral_and_diff_based():
    p = _create_pr()._pr_author_prompt("task-001", _TD, "redteam/task-001", _Proj())
    # never assumes a separate test-author artifact
    assert "test-author" not in p
    # reads the diff for the change (tests are in it regardless of mode)
    assert "impl_diff.patch" in p
    # review/test artifacts are optional, not hard-required
    assert "whichever of" in p
    assert "draft" in p.lower()


# ---- helper loaders for review_code and TDD runners ----


def _review_code():
    _wf = str(Path(__file__).resolve().parents[1] / "workflows")
    if _wf not in sys.path:
        sys.path.insert(0, _wf)
    import phase_runners.review_code as m

    return m


def _write_test():
    import _engine

    return _engine.write_test()


def _verify_test():
    import _engine

    return _engine.verify_test()


# ---- agent-pair implement + tdd regression: test-conventions injection (#160) ----

_CONVENTIONS_DEFAULT = ".redteam/docs/test-conventions.md"


def test_agent_pair_includes_and_tdd_excludes_test_conventions_file():
    """_agent_pair_base_prompt includes test_conventions_file (new in #160) while
    _tdd_base_prompt must NOT — TDD injection stays in write_test/verify_test."""

    class _ProjWithConventions(_Proj):
        test_conventions_file = "docs/my-test-conventions.md"

    # NEW: agent-pair includes conventions (fails against pre-change code)
    p_ap = _impl()._agent_pair_base_prompt(_TD, _ProjWithConventions())
    assert "docs/my-test-conventions.md" in p_ap

    # REGRESSION: TDD must NOT include conventions
    p_tdd = _impl()._tdd_base_prompt(_TD, _ProjWithConventions())
    assert "docs/my-test-conventions.md" not in p_tdd


# ---- review_code prompts: test-conventions injection (#160) ----


def test_code_review_prompt_names_test_conventions_file():
    """_code_review_prompt names the project test-conventions file so the reviewer
    judges agent-pair tests against them (#160)."""
    p = _review_code()._code_review_prompt(_TD, "main")
    assert _CONVENTIONS_DEFAULT in p


def test_narrowed_code_review_prompt_names_test_conventions_file():
    """_narrowed_code_review_prompt names the project test-conventions file (#160)."""
    p = _review_code()._narrowed_code_review_prompt(_TD, "main", "abc1234", [])
    assert _CONVENTIONS_DEFAULT in p


# ---- TDD-phase regression: write_test and verify_test still inject conventions ----


def test_tdd_phase_prompts_still_inject_test_conventions_file(monkeypatch, tmp_path):
    """TDD-phase regression: write_test and verify_test prompts still name test_conventions_file.
    The agent-pair assertion at the start ensures this function fails against pre-change code (#160)."""
    # New behavior assertion (fails against pre-change code)
    p = _impl()._agent_pair_base_prompt(_TD, _Proj())
    assert _CONVENTIONS_DEFAULT in p

    _MY_CONVENTIONS = "docs/my-conventions.md"

    # --- write_test regression ---
    wt = _write_test()
    captured_wt: dict = {}

    class _FakeProjWt:
        source_dirs = ("src/",)
        test_dir = "tests/"
        test_file_glob = "test_*.py"
        test_conventions_file = _MY_CONVENTIONS
        context_file = "ctx.md"
        base_branch = "main"

    class _FakeAdapterWt:
        def invoke(self, *, role, agent, prompt, cwd):
            captured_wt["prompt"] = prompt
            return {
                "stdout": "",
                "stderr": "no tests found",
                "returncode": 1,
                "cost_usd": None,
                "duration_sec": None,
                "model": None,
            }

    monkeypatch.setattr(wt, "project_config", lambda: _FakeProjWt())
    monkeypatch.setattr(wt, "get_worker_adapter", lambda state: _FakeAdapterWt())
    monkeypatch.setattr(wt, "worker_provider", lambda state: "claude")
    monkeypatch.setattr(wt, "repo_root", lambda: tmp_path)
    monkeypatch.setattr(wt, "pinned_base_branch", lambda state, rr: "main")
    monkeypatch.setattr(wt, "valid_tdd_test_files", lambda manifest, proj: [])
    monkeypatch.setattr(wt, "untracked_files", lambda rr: set())
    monkeypatch.setattr(wt, "_committed_test_files", lambda rr, proj, task_id, base_branch: [])
    monkeypatch.setattr(wt, "compute_branch_diff", lambda cwd, base_branch: "")

    task_dir_wt = tmp_path / "task-001"
    task_dir_wt.mkdir()
    (task_dir_wt / "outcome.md").write_text("# Outcome\n", encoding="utf-8")

    wt.run(task_dir_wt, {"task_id": "task-001", "worker_provider": "claude"})

    assert _MY_CONVENTIONS in captured_wt["prompt"]

    # --- verify_test regression ---
    vt = _verify_test()
    captured_vt: dict = {}

    class _FakeProjVt:
        source_dirs = ("src/",)
        test_dir = "tests/"
        test_file_glob = "test_*.py"
        test_conventions_file = _MY_CONVENTIONS
        context_file = "ctx.md"
        base_branch = "main"

    class _FakeAdapterVt:
        def invoke(self, *, role, agent, prompt, cwd):
            captured_vt["prompt"] = prompt
            return {
                "stdout": "",
                "stderr": "",
                "returncode": 0,
                "cost_usd": None,
                "duration_sec": None,
                "model": None,
            }

    monkeypatch.setattr(vt, "project_config", lambda: _FakeProjVt())
    monkeypatch.setattr(vt, "get_worker_adapter", lambda state: _FakeAdapterVt())
    monkeypatch.setattr(vt, "worker_provider", lambda state: "claude")
    monkeypatch.setattr(vt, "repo_root", lambda: tmp_path)
    monkeypatch.setattr(vt, "valid_tdd_test_files", lambda manifest, proj: ["tests/test_foo.py"])
    monkeypatch.setattr(vt, "compute_repo_diff", lambda cwd: "")
    monkeypatch.setattr(vt, "read_text_if_exists", lambda path: None)  # → early return after invoke

    task_dir_vt = tmp_path / "task-002"
    task_dir_vt.mkdir()

    vt.run(task_dir_vt, {"tdd_test_files": ["tests/test_foo.py"], "worker_provider": "claude"})

    assert _MY_CONVENTIONS in captured_vt["prompt"]
