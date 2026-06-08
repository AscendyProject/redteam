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


def test_review_code_changes_requested_routes_to_rescue_after_two_implement_retries(monkeypatch, tmp_path):
    orchestrator = _load_orchestrator_module()
    task_dir = tmp_path / "batch" / "tasks" / "task-001-demo"
    task_dir.mkdir(parents=True)
    (task_dir / "code_review.done").write_text("", encoding="utf-8")
    state = {
        "task_id": "task-001-demo",
        "mode": "agent-pair",
        "phase": "review_code",
        "phases_completed": ["plan_outcome", "plan_review", "human_gate_outcome", "implement"],
        "next_phase": "review_code",
        "review_items": [],
        "retries": {"implement": 2},
        "max_retries_per_phase": 2,
        "verification": {"last_exit_code": 0},
        "escape": {"ask_user": False, "reason": None, "return_phase": None},
    }
    (task_dir / "state.json").write_text(json.dumps(state), encoding="utf-8")

    monkeypatch.setattr(orchestrator, "_ensure_task_branch", lambda task_id, repo, branch_prefix: f"proj/{task_id}")
    monkeypatch.setattr(orchestrator, "repo_root", lambda: tmp_path)
    monkeypatch.setitem(
        orchestrator.PHASE_RUNNERS,
        "review_code",
        lambda task_dir, state: {
            "status": "changes_requested",
            "feedback": "still broken",
            "log": "IR-001 severity:major status:open\nREVIEW_DECISION: CHANGES_REQUESTED",
            "diff": "diff",
        },
    )

    outcome = orchestrator.process_task(task_dir)
    saved = json.loads((task_dir / "state.json").read_text(encoding="utf-8"))

    assert outcome == "blocked_on_human_gate"
    assert saved["next_phase"] == "rescue"
    assert saved["last_failure_reason"] == "changes_requested"
