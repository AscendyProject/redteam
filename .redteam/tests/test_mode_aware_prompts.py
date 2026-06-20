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
