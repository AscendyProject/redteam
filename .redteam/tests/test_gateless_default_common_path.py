"""Gateless default common path.

The default (untiered) common path carries NO `human_gate_outcome`: the
adversarial pair + verify is the automated trust and the output is a draft PR
(the human checkpoint before merge). A plan-approval gate is opt-in per tier
profile (`gates = ["outcome"]`). This pins:

1. neither static order contains `human_gate_outcome`, and the worker advances
   straight past plan (agent-pair: `plan_review → implement`; tdd:
   `plan_outcome → write_test`);
2. a legacy task persisted while parked at `human_gate_outcome` migrates forward
   to the phase that used to follow it instead of silently falling through to
   "done" (the dispatch-time regression `_next_phase` would otherwise cause).

The opt-in tier gate and the lean (gateless) tier order are covered in
test_tier_routing_orchestrator.py.
"""

from __future__ import annotations

import json
from pathlib import Path


def _load_orchestrator():
    import _engine

    return _engine.orchestrator()


# ---- 1. static default orders are gateless ----


def test_static_orders_carry_no_outcome_gate():
    orch = _load_orchestrator()
    assert "human_gate_outcome" not in orch.AGENT_PAIR_PHASE_ORDER
    assert "human_gate_outcome" not in orch.TDD_PHASE_ORDER


def test_agent_pair_plan_review_advances_to_implement():
    """No gate between plan_review and implement in the default agent-pair order."""
    orch = _load_orchestrator()
    assert orch._next_phase({"mode": "agent-pair"}, "plan_review") == "implement"


def test_tdd_plan_outcome_advances_to_write_test():
    """No gate between plan_outcome and write_test in the default tdd order."""
    orch = _load_orchestrator()
    assert orch._next_phase({"mode": "tdd"}, "plan_outcome") == "write_test"


# ---- 2. legacy parked-task migration (the BLOCKER from plan_review) ----


def _setup_parked(tmp_path: Path, mode: str) -> Path:
    """A task persisted while pointed at the now-removed human_gate_outcome gate,
    untiered, with a fully-pinned verification snapshot so the agent-pair
    dispatch backstop is a no-op and the test isolates the migration."""
    task_dir = tmp_path / "batch" / "tasks" / "task-001"
    task_dir.mkdir(parents=True)
    # A real parked task already has its approved outcome.md (a pre-implement
    # check fails closed without it); the test isolates the migration, not that.
    (task_dir / "outcome.md").write_text(
        "# Outcome\n\n## Verification\n\n```yaml\ncommands:\n  - bash .redteam/scripts/verify.sh\n```\n",
        encoding="utf-8",
    )
    completed = ["plan_outcome", "plan_review"] if mode == "agent-pair" else ["plan_outcome"]
    state = {
        "task_id": "task-001",
        "mode": mode,
        "phase": "human_gate_outcome",
        "phases_completed": completed,
        "next_phase": "human_gate_outcome",
        "verification": {
            "verify_command": "bash .redteam/scripts/verify.sh",
            "verify_allowlist": ["pytest"],
            "commands": ["bash .redteam/scripts/verify.sh"],
        },
        "retries": {},
        "max_retries_per_phase": 2,
    }
    (task_dir / "state.json").write_text(json.dumps(state), encoding="utf-8")
    return task_dir


def _run_to_first_phase(orch, monkeypatch, tmp_path, task_dir, target_phase: str):
    """Stub the expected target runner to record it ran and halt the pipeline."""
    monkeypatch.setattr(orch, "repo_root", lambda: tmp_path)
    monkeypatch.setattr(orch, "_ensure_task_branch", lambda *a, **k: "redteam/task-001")
    seen = {"ran": False}

    def fake_runner(td, st):
        seen["ran"] = True
        return {"status": "ask_user", "feedback": "halt", "log": "", "diff": ""}

    monkeypatch.setitem(orch.PHASE_RUNNERS, target_phase, fake_runner)
    outcome = orch.process_task(task_dir)
    return outcome, seen


def test_legacy_parked_agent_pair_task_migrates_to_implement(monkeypatch, tmp_path):
    """An agent-pair task parked at human_gate_outcome resumes into implement —
    NOT skipped to "done"."""
    orch = _load_orchestrator()
    task_dir = _setup_parked(tmp_path, "agent-pair")
    outcome, seen = _run_to_first_phase(orch, monkeypatch, tmp_path, task_dir, "implement")

    assert seen["ran"] is True, "migration must route a parked agent-pair task into implement"
    assert outcome != "done"
    persisted = json.loads((task_dir / "state.json").read_text(encoding="utf-8"))
    assert persisted["next_phase"] != "done"


def test_legacy_parked_tdd_task_migrates_to_write_test(monkeypatch, tmp_path):
    """A tdd task parked at human_gate_outcome resumes into write_test —
    NOT skipped to "done"."""
    orch = _load_orchestrator()
    task_dir = _setup_parked(tmp_path, "tdd")
    outcome, seen = _run_to_first_phase(orch, monkeypatch, tmp_path, task_dir, "write_test")

    assert seen["ran"] is True, "migration must route a parked tdd task into write_test"
    assert outcome != "done"


def test_migration_persists_next_phase_off_the_gate(monkeypatch, tmp_path):
    """The migration rewrites and persists `next_phase` forward, off the removed
    gate — so a subsequent resume starts from the real work, never re-parks at the
    vanished gate and never falls through to "done"."""
    orch = _load_orchestrator()
    task_dir = _setup_parked(tmp_path, "agent-pair")
    _run_to_first_phase(orch, monkeypatch, tmp_path, task_dir, "implement")
    persisted = json.loads((task_dir / "state.json").read_text(encoding="utf-8"))
    assert persisted["next_phase"] not in ("human_gate_outcome", "done")


# ---- 3. an opted-in tier gate is NOT migrated away — it still blocks ----


def test_tier_optin_outcome_gate_still_blocks(monkeypatch, tmp_path):
    """A tier that opts the outcome gate back in (`gates=["outcome"]`) keeps
    `human_gate_outcome` in its persisted `tier_phases`, so the migration must NOT
    fire and the task blocks until the sentinel is touched."""
    orch = _load_orchestrator()
    from config import TierProfile  # type: ignore

    tier_order = orch._build_tier_phase_order(TierProfile(review=True, gates=("outcome",)))
    assert "human_gate_outcome" in tier_order  # opted in

    task_dir = tmp_path / "batch" / "tasks" / "task-001"
    task_dir.mkdir(parents=True)
    state = {
        "task_id": "task-001",
        "mode": "agent-pair",
        "phase": "human_gate_outcome",
        "phases_completed": ["plan_outcome", "plan_review"],
        "next_phase": "human_gate_outcome",
        "tier": 4,
        "tier_phases": tier_order,
        "retries": {},
        "max_retries_per_phase": 2,
    }
    (task_dir / "state.json").write_text(json.dumps(state), encoding="utf-8")
    monkeypatch.setattr(orch, "repo_root", lambda: tmp_path)
    monkeypatch.setattr(orch, "_ensure_task_branch", lambda *a, **k: "redteam/task-001")

    ran = {"implement": False}
    monkeypatch.setitem(
        orch.PHASE_RUNNERS,
        "implement",
        lambda td, st: (
            ran.__setitem__("implement", True) or {"status": "ask_user", "feedback": "h", "log": "", "diff": ""}
        ),
    )
    outcome = orch.process_task(task_dir)

    assert outcome == "blocked_on_human_gate"  # gate still gates
    assert ran["implement"] is False  # migration did NOT skip the opted-in gate
