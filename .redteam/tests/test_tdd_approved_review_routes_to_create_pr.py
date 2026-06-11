"""Regression (#7.5 finding F-E): in TDD mode an APPROVED code review must route
to create_pr, NOT fall through into rescue.

Bug: the review_code-approved → create_pr transition (which skips the
conditionally-entered `rescue` phase) was gated on `_mode == "agent-pair"`. In
TDD mode the approved branch fell to the generic `_next_phase(state,
"review_code")`, and since TDD_PHASE_ORDER lists `review_code → rescue → …`,
that returned "rescue". The orchestrator then ran rescue, which never produced
`rescue_report.md`, failed twice, and deferred the task — even though every
sub-agent had done its job and the review genuinely passed.

The cross-stack validator hit this because the seeded state.json defaults to
`mode: "tdd"`. rescue must only be reachable via the explicit conditional
`next_phase = "rescue"` branches (RESCUE_REQUIRED / blocker-carryover), never by
linear fall-through after an approved review — in either mode.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path


def _load_orchestrator_module():
    module_path = Path(__file__).resolve().parents[1] / "workflows" / "orchestrator.py"
    spec = importlib.util.spec_from_file_location("redteam_orchestrator", module_path)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_tdd_mode_approved_review_routes_to_create_pr_not_rescue(monkeypatch, tmp_path):
    orchestrator = _load_orchestrator_module()
    task_dir = tmp_path / "batch" / "tasks" / "task-001-demo"
    task_dir.mkdir(parents=True)
    state = {
        "task_id": "task-001-demo",
        "mode": "tdd",
        "phase": "review_code",
        "phases_completed": [
            "plan_outcome",
            "human_gate_outcome",
            "write_test",
            "verify_test",
            "implement",
        ],
        "next_phase": "review_code",
        "retries": {},
        "max_retries_per_phase": 2,
        "verification": {"last_exit_code": 0},
        "escape": {"ask_user": False, "reason": None, "return_phase": None},
    }
    (task_dir / "state.json").write_text(json.dumps(state), encoding="utf-8")

    monkeypatch.setattr(
        orchestrator,
        "_ensure_task_branch",
        lambda task_id, repo, branch_prefix, base_branch: f"proj/{task_id}",
    )
    monkeypatch.setattr(orchestrator, "repo_root", lambda: tmp_path)
    approved = {"status": "approved", "feedback": "", "log": "REVIEW_DECISION: APPROVED", "diff": ""}
    monkeypatch.setitem(orchestrator.PHASE_RUNNERS, "review_code", lambda task_dir, state: approved)
    # Mock create_pr too, so the loop finishes cleanly rather than invoking a real
    # worker adapter. If the bug is present, the loop would instead run the
    # (unmocked) rescue phase. The default order has no human_gate_pr (the draft
    # PR is the human checkpoint), so an approved create_pr advances to done.
    monkeypatch.setitem(orchestrator.PHASE_RUNNERS, "create_pr", lambda task_dir, state: approved)

    # Guard: if routing regresses to rescue, fail loudly here rather than
    # executing the real rescue runner.
    def _fail_rescue(task_dir, state):
        raise AssertionError("rescue must not run after an APPROVED review")

    monkeypatch.setitem(orchestrator.PHASE_RUNNERS, "rescue", _fail_rescue)

    outcome = orchestrator.process_task(task_dir)
    saved = json.loads((task_dir / "state.json").read_text(encoding="utf-8"))

    assert outcome == "done"
    assert saved["next_phase"] == "done"
    assert "review_code" in saved["phases_completed"]
    assert "create_pr" in saved["phases_completed"]
    assert "rescue" not in saved["phases_completed"]
    assert saved.get("deferred_requirements", []) == []
