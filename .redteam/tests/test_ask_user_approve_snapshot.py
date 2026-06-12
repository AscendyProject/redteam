"""Regression (#35): ask_user APPROVE → implement must take the verification
snapshot if plan_review escalated here without ever approving.

In agent-pair mode a plan_review blocker that carries over escalates to
ask_user. Answering APPROVE routed straight to implement WITHOUT the
verification-commands snapshot (which normally happens at plan_review approval),
so implement ran with `state.verification.commands == []`, failed closed, and the
task was falsely deferred. The fix takes the snapshot at the ask_user→implement
hand-off when it's missing, fail-closed.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path


def _load_orchestrator():
    p = Path(__file__).resolve().parents[1] / "workflows" / "orchestrator.py"
    spec = importlib.util.spec_from_file_location("redteam_orchestrator", p)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_OUTCOME = """# Outcome

## Verification

```yaml
commands:
  - bash .redteam/scripts/verify.sh
```
"""


def _setup(tmp_path: Path, decision: str):
    task_dir = tmp_path / "batch" / "tasks" / "task-001"
    task_dir.mkdir(parents=True)
    (task_dir / "outcome.md").write_text(_OUTCOME, encoding="utf-8")
    (task_dir / "ask_user_response.md").write_text(f"approve it\n\nUSER_DECISION: {decision}\n", encoding="utf-8")
    (task_dir / "ask_user.resolved").write_text("", encoding="utf-8")
    state = {
        "task_id": "task-001",
        "mode": "agent-pair",
        "phase": "ask_user",
        "phases_completed": ["plan_outcome"],
        "next_phase": "ask_user",
        "verification": {},  # NO snapshot yet — plan_review escalated, never approved
        "escape": {"ask_user": True, "reason": "plan review blocker carried over twice", "return_phase": "plan_review"},
        "retries": {},
        "max_retries_per_phase": 2,
    }
    (task_dir / "state.json").write_text(json.dumps(state), encoding="utf-8")
    return task_dir


def test_approve_takes_snapshot_before_implement(monkeypatch, tmp_path):
    orch = _load_orchestrator()
    task_dir = _setup(tmp_path, "APPROVE")
    monkeypatch.setattr(orch, "repo_root", lambda: tmp_path)
    monkeypatch.setattr(orch, "_ensure_task_branch", lambda *a, **k: "redteam/task-001")

    seen = {}

    def fake_implement(td, st):
        # capture the snapshot state AT implement time, then halt cleanly
        seen["verify_command"] = st.get("verification", {}).get("verify_command")
        seen["commands"] = st.get("verification", {}).get("commands")
        return {"status": "ask_user", "feedback": "halt", "log": "", "diff": ""}

    monkeypatch.setitem(orch.PHASE_RUNNERS, "implement", fake_implement)

    orch.process_task(task_dir)

    # the snapshot was taken before implement ran (issue #35: previously empty)
    assert seen["verify_command"] == "bash .redteam/scripts/verify.sh"
    assert seen["commands"] == ["bash .redteam/scripts/verify.sh"]


def test_existing_snapshot_is_not_overwritten(monkeypatch, tmp_path):
    """REVISE_IMPLEMENTATION after an approved plan_review already has a snapshot;
    the guard must leave it untouched (only fills a MISSING one)."""
    orch = _load_orchestrator()
    task_dir = _setup(tmp_path, "REVISE_IMPLEMENTATION")
    # pre-existing snapshot with a distinct value
    state = json.loads((task_dir / "state.json").read_text())
    state["verification"] = {"verify_command": "PRESET", "verify_allowlist": ["pytest"], "commands": ["PRESET"]}
    (task_dir / "state.json").write_text(json.dumps(state), encoding="utf-8")
    monkeypatch.setattr(orch, "repo_root", lambda: tmp_path)
    monkeypatch.setattr(orch, "_ensure_task_branch", lambda *a, **k: "redteam/task-001")

    seen = {}

    def fake_implement(td, st):
        seen["verify_command"] = st.get("verification", {}).get("verify_command")
        return {"status": "ask_user", "feedback": "halt", "log": "", "diff": ""}

    monkeypatch.setitem(orch.PHASE_RUNNERS, "implement", fake_implement)
    orch.process_task(task_dir)

    assert seen["verify_command"] == "PRESET"  # not re-snapshotted
