"""Pre-implement snapshot invariant (#39).

implement must NEVER run unpinned. The verify_command + allowlist are normally
snapshotted at plan_review / plan_outcome approval / the ask_user→implement
hand-off, but a state that reaches next_phase==implement with an absent or
PARTIAL snapshot (corrupted/legacy state.json, or a future transition that
forgets) would otherwise run the implementer before the gate is pinned. A single
fail-closed backstop at implement dispatch re-snapshots (or defers) — "pinned"
requires BOTH verify_command (str) and verify_allowlist (list).
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


def _setup(tmp_path: Path, verification: dict):
    """A task sitting directly at next_phase==implement (NOT via ask_user), with
    the given `verification` snapshot state."""
    task_dir = tmp_path / "batch" / "tasks" / "task-001"
    task_dir.mkdir(parents=True)
    (task_dir / "outcome.md").write_text(_OUTCOME, encoding="utf-8")
    state = {
        "task_id": "task-001",
        "mode": "agent-pair",
        "phase": "implement",
        "phases_completed": ["plan_outcome", "plan_review", "human_gate_outcome"],
        "next_phase": "implement",
        "verification": verification,
        "retries": {},
        "max_retries_per_phase": 2,
    }
    (task_dir / "state.json").write_text(json.dumps(state), encoding="utf-8")
    return task_dir


def _run(orch, monkeypatch, tmp_path, task_dir):
    monkeypatch.setattr(orch, "repo_root", lambda: tmp_path)
    monkeypatch.setattr(orch, "_ensure_task_branch", lambda *a, **k: "redteam/task-001")
    seen = {"implement_called": False, "verify_command": None, "allowlist": None}

    def fake_implement(td, st):
        seen["implement_called"] = True
        v = st.get("verification", {})
        seen["verify_command"] = v.get("verify_command")
        seen["allowlist"] = v.get("verify_allowlist")
        return {"status": "ask_user", "feedback": "halt", "log": "", "diff": ""}

    monkeypatch.setitem(orch.PHASE_RUNNERS, "implement", fake_implement)
    outcome = orch.process_task(task_dir)
    return outcome, seen


def test_dispatch_snapshots_when_state_arrives_unpinned(monkeypatch, tmp_path):
    """A state reaching implement with verification=={} (e.g. a corrupted/legacy
    state.json, NOT via the snapshot sites) is snapshotted at dispatch so the
    implementer runs pinned."""
    orch = _load_orchestrator()
    task_dir = _setup(tmp_path, {})
    _, seen = _run(orch, monkeypatch, tmp_path, task_dir)

    assert seen["implement_called"] is True
    assert seen["verify_command"] == "bash .redteam/scripts/verify.sh"  # pinned at dispatch
    assert isinstance(seen["allowlist"], list)


def test_partial_pin_is_treated_as_unpinned(monkeypatch, tmp_path):
    """#39 IR-001: verify_command present but verify_allowlist MISSING is NOT
    pinned — the dispatch guard must re-snapshot (both fields), not let the
    implementer run on a half-pinned snapshot that implement.py would only catch
    after mutation."""
    orch = _load_orchestrator()
    task_dir = _setup(tmp_path, {"verify_command": "PARTIAL", "commands": ["PARTIAL"]})  # no allowlist
    _, seen = _run(orch, monkeypatch, tmp_path, task_dir)

    assert seen["implement_called"] is True
    assert seen["verify_command"] == "bash .redteam/scripts/verify.sh"  # re-snapshotted, not "PARTIAL"
    assert isinstance(seen["allowlist"], list)


def test_defers_and_does_not_run_implement_when_snapshot_fails(monkeypatch, tmp_path):
    """If the snapshot cannot be taken, fail closed: defer and NEVER invoke the
    implementer on an unpinned tree."""
    orch = _load_orchestrator()
    task_dir = _setup(tmp_path, {})
    monkeypatch.setattr(orch, "_snapshot_verification_commands", lambda td, st: False)
    outcome, seen = _run(orch, monkeypatch, tmp_path, task_dir)

    assert seen["implement_called"] is False  # implementer never ran unpinned
    assert outcome in ("error", "deferred")
    persisted = json.loads((task_dir / "state.json").read_text(encoding="utf-8"))
    assert persisted["next_phase"] == "deferred"
    assert persisted["last_failure_reason"] == "unpinned_verification_snapshot"


def test_fully_pinned_snapshot_is_not_re_taken(monkeypatch, tmp_path):
    """The backstop only fills a missing/partial snapshot — a fully-pinned state
    (both fields) is left untouched (no clobber, preserving IR-001)."""
    orch = _load_orchestrator()
    task_dir = _setup(tmp_path, {"verify_command": "PRESET", "verify_allowlist": ["pytest"], "commands": ["PRESET"]})
    _, seen = _run(orch, monkeypatch, tmp_path, task_dir)

    assert seen["implement_called"] is True
    assert seen["verify_command"] == "PRESET"  # not re-snapshotted
