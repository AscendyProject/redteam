"""Tests for #183 — a plan_review backtrack must amend outcome.md, not rewrite it.

The reported failure: when plan_review returns CHANGES_REQUESTED the orchestrator
backtracks to plan_outcome, which regenerated outcome.md from scratch. Any edit a
human made between rounds — the documented "fix, then `orchestrator resume`"
escape from a stuck planner — was destroyed, and findings state.json already
tracked as resolved were reopened. Observed as 6 plan-review rounds ending in the
reviewer returning RESCUE_REQUIRED because the loop, not the plan, was the defect.

Two mechanisms are covered here:

- the orchestrator SNAPSHOTS outcome.md and flags the next plan_outcome as an
  amend (copy, not move — a review file is vacated for the next round, but
  outcome.md has to stay in place for the planner to amend it);
- plan_outcome switches its prompt to amend-in-place when that flag is set.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

_WF = Path(__file__).resolve().parents[1] / "workflows"
if str(_WF) not in sys.path:
    sys.path.insert(0, str(_WF))

import phase_runners.plan_outcome as _po  # noqa: E402

_PROJ = SimpleNamespace(
    context_file="ctx.md",
    source_dirs=["app/"],
    test_dir="tests/",
    test_file_glob="test_*.py",
    verify_command="bash verify.sh",
)


def _orch():
    import _engine

    return _engine.orchestrator()


def _capture_prompt(task_dir: Path, state: dict) -> str:
    """Run plan_outcome with the worker stubbed; return the prompt it was given."""
    captured: dict[str, str] = {}

    def _invoke(**kwargs):
        captured["prompt"] = kwargs["prompt"]
        return {"returncode": 1, "stdout": "", "stderr": ""}

    with (
        patch.object(_po, "project_config", return_value=_PROJ),
        patch.object(_po, "repo_root", return_value=task_dir),
        patch.object(_po, "compute_repo_diff", return_value=""),
        patch.object(_po, "get_worker_adapter", return_value=SimpleNamespace(invoke=_invoke)),
        patch.object(_po, "worker_provider", return_value="claude"),
    ):
        _po.run(task_dir, state)
    return captured["prompt"]


# ---------------------------------------------------------------------------
# plan_outcome — amend vs fresh
# ---------------------------------------------------------------------------


def test_backtrack_prompt_amends_instead_of_regenerating(tmp_path):
    """#183: with the amend flag set, the planner is told to preserve the document.

    Fails against pre-change code, where the prompt said "produce outcome.md"
    unconditionally and never referenced the existing file.
    """
    (tmp_path / "outcome.md").write_text("# Plan\n\nHuman-edited section.\n", encoding="utf-8")

    prompt = _capture_prompt(tmp_path, {"plan_outcome_amend": True})

    assert "AMEND" in prompt
    assert "HUMAN EDITS" in prompt
    assert "Do NOT regenerate it from scratch" in prompt
    # The reason the whole-rewrite is dangerous must be stated, not just forbidden:
    # re-deriving an untouched section is how a resolved finding silently reopens.
    assert "byte-identical" in prompt


def test_amend_requires_both_the_flag_and_a_real_document(tmp_path):
    """Amend mode is entered on a backtrack with a document, and NOT otherwise.

    Four cases in one test because only the first differs from pre-change code —
    the three negatives are all "generates from the brief", which is exactly what
    the old unconditional prompt did, so none of them can discriminate alone.
    Together they pin that the new branch is entered precisely when it should be.
    """
    # 1 — flag + document → amend (new behaviour).
    (tmp_path / "outcome.md").write_text("# Plan\n\nHuman-edited.\n", encoding="utf-8")
    assert "AMEND" in _capture_prompt(tmp_path, {"plan_outcome_amend": True})

    # 2 — document but no flag: a first run, or a planner error-retry whose
    # half-written file is worth discarding rather than building on.
    assert "AMEND" not in _capture_prompt(tmp_path, {})

    # 3 — empty document: nothing to preserve, so amending would ask the planner
    # to build on nothing.
    (tmp_path / "outcome.md").write_text("", encoding="utf-8")
    assert "AMEND" not in _capture_prompt(tmp_path, {"plan_outcome_amend": True})

    # 4 — stale flag, no file at all: must not point the planner at a missing doc.
    (tmp_path / "outcome.md").unlink()
    fresh = _capture_prompt(tmp_path, {"plan_outcome_amend": True})
    assert "AMEND" not in fresh
    assert "Plan outcome.md for the task at" in fresh


# ---------------------------------------------------------------------------
# orchestrator — snapshot + flag on backtrack
# ---------------------------------------------------------------------------


def test_backtrack_snapshots_outcome_and_keeps_the_original(tmp_path):
    """#183 D: the human's work becomes recoverable AND stays in place.

    A copy, not a move: _archive_review_round vacates a review file so the next
    round writes fresh, but outcome.md must survive for the planner to amend. A
    move here would satisfy "recoverable" while breaking the amend fix.
    """
    orch = _orch()
    original = "# Plan\n\nHuman-edited section.\n"
    (tmp_path / "outcome.md").write_text(original, encoding="utf-8")
    state: dict = {}

    orch._preserve_outcome_for_amend(tmp_path, state)

    assert (tmp_path / "outcome.md").read_text(encoding="utf-8") == original, "the working document must remain"
    assert (tmp_path / "outcome.round1.md").read_text(encoding="utf-8") == original
    assert state["plan_outcome_amend"] is True


def test_repeated_backtracks_do_not_overwrite_earlier_snapshots(tmp_path):
    """Each round gets its own slot, so a multi-round loop keeps full history —
    the six-round run in the report would otherwise retain only the last state."""
    orch = _orch()
    (tmp_path / "outcome.md").write_text("round one\n", encoding="utf-8")
    orch._preserve_outcome_for_amend(tmp_path, {})

    (tmp_path / "outcome.md").write_text("round two\n", encoding="utf-8")
    orch._preserve_outcome_for_amend(tmp_path, {})

    assert (tmp_path / "outcome.round1.md").read_text(encoding="utf-8") == "round one\n"
    assert (tmp_path / "outcome.round2.md").read_text(encoding="utf-8") == "round two\n"


def test_snapshot_is_best_effort_and_never_blocks_the_backtrack(tmp_path):
    """Losing the history copy must not strand the run — the backtrack still has
    to proceed, and the amend flag still has to be set."""
    orch = _orch()
    (tmp_path / "outcome.md").write_text("plan\n", encoding="utf-8")
    state: dict = {}

    with patch.object(orch.Path, "write_text", side_effect=OSError("read-only")):
        orch._preserve_outcome_for_amend(tmp_path, state)

    assert state["plan_outcome_amend"] is True


def test_no_outcome_file_is_a_no_op(tmp_path):
    """A backtrack before any outcome.md exists must not create a phantom
    snapshot or flag an amend of nothing."""
    orch = _orch()
    state: dict = {}

    orch._preserve_outcome_for_amend(tmp_path, state)

    assert not list(tmp_path.glob("outcome.round*.md"))
    assert "plan_outcome_amend" not in state


# ---------------------------------------------------------------------------
# End to end through the runner
# ---------------------------------------------------------------------------


def test_snapshot_then_amend_preserves_the_human_edit_end_to_end():
    """The two halves compose: snapshot leaves the edited document in place, and
    the next plan_outcome is told to amend that exact file."""
    orch = _orch()
    with tempfile.TemporaryDirectory() as td:
        task_dir = Path(td)
        human = "# Plan\n\n## Done-when\n- [ ] the thing a human wrote\n"
        (task_dir / "outcome.md").write_text(human, encoding="utf-8")
        state: dict = {}

        orch._preserve_outcome_for_amend(task_dir, state)
        prompt = _capture_prompt(task_dir, state)

        assert (task_dir / "outcome.md").read_text(encoding="utf-8") == human
        assert (task_dir / "outcome.round1.md").read_text(encoding="utf-8") == human
        assert "AMEND" in prompt


# ---------------------------------------------------------------------------
# Every route back to plan_outcome, not just the CHANGES_REQUESTED backtrack
# ---------------------------------------------------------------------------


_OUTCOME_WITH_VERIFY = """# Outcome

Human-edited section that must survive.

## Verification

```yaml
commands:
  - bash .redteam/scripts/verify.sh
```
"""


def _ask_user_task(tmp_path: Path, decision: str) -> Path:
    import json

    task_dir = tmp_path / "batch" / "tasks" / "task-001"
    task_dir.mkdir(parents=True)
    (task_dir / "outcome.md").write_text(_OUTCOME_WITH_VERIFY, encoding="utf-8")
    (task_dir / "ask_user_response.md").write_text(f"revise\n\nUSER_DECISION: {decision}\n", encoding="utf-8")
    (task_dir / "ask_user.resolved").write_text("", encoding="utf-8")
    state = {
        "task_id": "task-001",
        "mode": "agent-pair",
        "phase": "ask_user",
        "phases_completed": ["plan_outcome"],
        "next_phase": "ask_user",
        "verification": {},
        "escape": {"ask_user": True, "reason": "carried-over blocker", "return_phase": "plan_review"},
        "retries": {},
        "max_retries_per_phase": 2,
    }
    (task_dir / "state.json").write_text(json.dumps(state), encoding="utf-8")
    return task_dir


def test_revise_plan_decision_preserves_the_human_edit(monkeypatch, tmp_path):
    """#183 review IR-001: `USER_DECISION: REVISE_PLAN` must snapshot and amend.

    This is the path MOST likely to carry human edits — the operator was
    explicitly asked to intervene and then chose to revise the plan — so
    regenerating here discards exactly the work the escalation solicited. It
    routes to plan_outcome outside the CHANGES_REQUESTED backtrack, so the first
    fix missed it entirely.
    """
    import json

    orch = _orch()
    task_dir = _ask_user_task(tmp_path, "REVISE_PLAN")
    monkeypatch.setattr(orch, "repo_root", lambda: tmp_path)
    monkeypatch.setattr(orch, "_ensure_task_branch", lambda *a, **k: "redteam/task-001")

    seen: dict = {}

    def fake_plan_outcome(td, st):
        seen["amend"] = st.get("plan_outcome_amend")
        return {"status": "ask_user", "feedback": "halt", "log": "", "diff": ""}

    monkeypatch.setitem(orch.PHASE_RUNNERS, "plan_outcome", fake_plan_outcome)

    orch.process_task(task_dir)

    assert seen["amend"] is True, "the planner must be told to amend, not regenerate"
    assert (task_dir / "outcome.md").read_text(encoding="utf-8") == _OUTCOME_WITH_VERIFY
    assert (task_dir / "outcome.round1.md").read_text(encoding="utf-8") == _OUTCOME_WITH_VERIFY
    saved = json.loads((task_dir / "state.json").read_text(encoding="utf-8"))
    assert saved.get("plan_outcome_amend") is True


def test_approve_decision_does_not_flag_an_amend(monkeypatch, tmp_path):
    """Scoping: APPROVE proceeds to implement and must not mark the plan for
    amendment — only routes back to plan_outcome do."""
    orch = _orch()
    task_dir = _ask_user_task(tmp_path, "APPROVE")
    monkeypatch.setattr(orch, "repo_root", lambda: tmp_path)
    monkeypatch.setattr(orch, "_ensure_task_branch", lambda *a, **k: "redteam/task-001")
    monkeypatch.setitem(
        orch.PHASE_RUNNERS,
        "implement",
        lambda td, st: {"status": "ask_user", "feedback": "halt", "log": "", "diff": ""},
    )

    orch.process_task(task_dir)

    assert not list(task_dir.glob("outcome.round*.md"))
