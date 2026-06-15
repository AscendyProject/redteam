"""Operator progress surface (#49).

The orchestrator writes a best-effort, human-facing `progress.md` per task on
every `save_state`, so an operator (especially on a detached run) can read one
file for "where are we / since when / next action". It must:
- never break a phase transition if rendering fails (state.json is the source of
  truth and is written first);
- never echo `last_failure_log` (can carry credentials) — only the reason;
- record a real per-phase start timestamp set at dispatch (not just updated_at).
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


def test_write_progress_renders_and_is_defensive(tmp_path):
    orch = _load_orchestrator()
    # A sparse/odd-shaped state must not raise and must still produce the file.
    orch._write_progress(tmp_path, {"task_id": "t1", "phase": "implement", "review_items": "not-a-list"})
    text = (tmp_path / "progress.md").read_text(encoding="utf-8")
    assert "t1" in text and "implement" in text


def test_progress_uses_failure_reason_not_log(tmp_path):
    orch = _load_orchestrator()
    state = {
        "task_id": "t1",
        "phase": "create_pr",
        "last_failure_reason": "branch_setup_failed",
        "last_failure_log": "fatal: remote token gho_SECRETSECRET leaked here",
    }
    orch._write_progress(tmp_path, state)
    text = (tmp_path / "progress.md").read_text(encoding="utf-8")
    assert "branch_setup_failed" in text  # the reason is shown
    assert "gho_SECRETSECRET" not in text and "remote token" not in text  # the log is NOT


def test_save_state_persists_even_if_progress_render_fails(monkeypatch, tmp_path):
    orch = _load_orchestrator()

    def _boom(task_dir, state):
        raise RuntimeError("render blew up")

    monkeypatch.setattr(orch, "_write_progress", _boom)
    state = {"task_id": "t1", "next_phase": "implement"}

    orch.save_state(tmp_path, state)  # must NOT raise

    persisted = json.loads((tmp_path / "state.json").read_text(encoding="utf-8"))
    assert persisted["next_phase"] == "implement"  # state.json written despite render error


def test_dispatch_sets_phase_started_at_and_writes_progress(monkeypatch, tmp_path):
    """process_task stamps a per-phase start time before running the phase, and
    progress.md lands in the task dir."""
    orch = _load_orchestrator()
    task_dir = tmp_path / "batch" / "tasks" / "task-001"
    task_dir.mkdir(parents=True)
    (task_dir / "outcome.md").write_text("# Outcome\n", encoding="utf-8")
    state = {
        "task_id": "task-001",
        "mode": "agent-pair",
        "phase": "implement",
        "phases_completed": ["plan_outcome"],
        "next_phase": "implement",
        "verification": {"verify_command": "x", "verify_allowlist": ["pytest"], "commands": ["x"]},
        "retries": {},
        "max_retries_per_phase": 2,
    }
    (task_dir / "state.json").write_text(json.dumps(state), encoding="utf-8")
    monkeypatch.setattr(orch, "repo_root", lambda: tmp_path)
    monkeypatch.setattr(orch, "_ensure_task_branch", lambda *a, **k: "redteam/task-001")

    seen = {}

    def fake_implement(td, st):
        # Capture the surface AS the implement phase runs (dispatch wrote it just
        # before calling us); later transitions would rewrite progress.md.
        seen["phase_started_at"] = st.get("phase_started_at")
        seen["progress_during_implement"] = (task_dir / "progress.md").read_text(encoding="utf-8")
        return {"status": "ask_user", "feedback": "halt", "log": "", "diff": ""}

    monkeypatch.setitem(orch.PHASE_RUNNERS, "implement", fake_implement)
    orch.process_task(task_dir)

    assert seen["phase_started_at"]  # a real timestamp was stamped before the runner
    assert (task_dir / "progress.md").is_file()  # operator surface written
    # while implement was running, the surface reflected that phase + its start time
    assert "implement" in seen["progress_during_implement"]
    assert seen["phase_started_at"] in seen["progress_during_implement"]
