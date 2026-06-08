"""Regression (#7.5 finding F-A): task bootstrap + default mode.

The standalone repo has no task-init step — the only path to a `state.json` was a
"SKILL" that lived in the origin monorepo and does not exist here, so an operator
who followed the README ("drop an input.md and run start") hit a refusal whose
error pointed at that non-existent SKILL. And the seed template defaulted to
`mode: "tdd"`, which routes a single-model flow even though the project's thesis
is the agent PAIR (and the validator had a codex reviewer configured).

Fix: `start`/`resume` auto-seed `state.json` from the template when an `input.md`
exists but no state does; the missing-state error no longer mentions a SKILL; and
the default mode is `agent-pair`.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path


def _load():
    module_path = Path(__file__).resolve().parents[1] / "workflows" / "orchestrator.py"
    spec = importlib.util.spec_from_file_location("redteam_orchestrator", module_path)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


_REPO_ROOT = Path(__file__).resolve().parents[2]


def test_default_mode_is_agent_pair():
    orch = _load()
    assert orch._mode({}) == "agent-pair"
    assert orch._mode({"mode": "tdd"}) == "tdd"  # explicit still honored


def test_seed_state_from_template(tmp_path, monkeypatch):
    orch = _load()
    monkeypatch.setattr(orch, "repo_root", lambda: _REPO_ROOT)
    task_dir = tmp_path / "tasks" / "task-001-demo"
    task_dir.mkdir(parents=True)
    (task_dir / "input.md").write_text("do a thing", encoding="utf-8")

    orch._seed_state(task_dir)

    state = json.loads((task_dir / "state.json").read_text(encoding="utf-8"))
    assert state["task_id"] == "task-001-demo"
    assert state["mode"] == "agent-pair"
    assert state["created_at"]
    assert state["next_phase"] == "plan_outcome"


def test_process_batch_seeds_when_input_present(tmp_path, monkeypatch):
    orch = _load()
    monkeypatch.setattr(orch, "repo_root", lambda: _REPO_ROOT)
    batch = tmp_path / "batch"
    task_dir = batch / "tasks" / "task-001-demo"
    task_dir.mkdir(parents=True)
    (task_dir / "input.md").write_text("brief", encoding="utf-8")

    seen: dict[str, bool] = {}

    def fake_process(td: Path):
        seen["state_existed"] = (td / "state.json").is_file()
        return "blocked_on_human_gate"

    monkeypatch.setattr(orch, "process_task", fake_process)
    results = orch.process_batch(batch)

    assert results["task-001-demo"] == "blocked_on_human_gate"
    assert seen["state_existed"] is True  # seeded BEFORE process_task ran


def test_process_batch_reports_no_input_md(tmp_path):
    orch = _load()
    batch = tmp_path / "batch"
    task_dir = batch / "tasks" / "task-empty"
    task_dir.mkdir(parents=True)

    results = orch.process_batch(batch)

    assert results["task-empty"] == "no_input_md"


def test_missing_state_error_has_no_skill_reference(tmp_path):
    orch = _load()
    task_dir = tmp_path / "task"
    task_dir.mkdir()
    try:
        orch.load_state(task_dir)
    except FileNotFoundError as e:
        assert "SKILL" not in str(e)
        assert "state.json" in str(e)
    else:
        raise AssertionError("expected FileNotFoundError")
