"""`status --json` machine-readable surface + goal-aware status summary.

Covers: per-task JSON fields, the no-secret-leak posture (last_failure_log and
deferred feedback NEVER emitted — IR-002/IR-004), goal.json-aware reporting
(valid / invalid / absent), the human-readable goal summary line, and the CLI
`--json` flag dispatch (including unknown-argument rejection).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

_WF = Path(__file__).resolve().parents[1] / "workflows"
if str(_WF) not in sys.path:
    sys.path.insert(0, str(_WF))


def _orch():
    import _engine

    return _engine.orchestrator()


# ---------- helpers ----------


def _make_batch(tmp_path: Path, states: dict[str, dict | None], goal_json: str | None = None) -> Path:
    """Batch with one task dir per key; value is the state.json dict (None = no state)."""
    batch_dir = tmp_path / "batch"
    tasks_root = batch_dir / "tasks"
    for tid, state in states.items():
        td = tasks_root / tid
        td.mkdir(parents=True)
        (td / "input.md").write_text("brief", encoding="utf-8")
        if state is not None:
            (td / "state.json").write_text(json.dumps(state), encoding="utf-8")
    if goal_json is not None:
        (batch_dir / "goal.json").write_text(goal_json, encoding="utf-8")
    return batch_dir


def _chain_manifest(parent: str, child: str) -> str:
    return json.dumps({"goal": "test", "tasks": {parent: {"depends_on": []}, child: {"depends_on": [parent]}}})


def _json_status(orch, batch_dir: Path, capsys) -> dict:
    rc = orch.cmd_status(batch_dir, as_json=True)
    assert rc == 0
    return json.loads(capsys.readouterr().out)


# ---------- per-task JSON fields ----------


def test_status_json_task_fields(tmp_path, capsys):
    orch = _orch()
    batch_dir = _make_batch(
        tmp_path,
        {
            "task-a": {
                "task_id": "task-a",
                "next_phase": "review_code",
                "phases_completed": ["plan_outcome", "implement"],
                "last_failure_reason": "error",
            },
            "task-b": None,
        },
    )
    report = _json_status(orch, batch_dir, capsys)
    assert report["batch"] == "batch"
    by_id = {t["id"]: t for t in report["tasks"]}
    a = by_id["task-a"]
    assert a["state"] == "ok"
    assert a["next_phase"] == "review_code"
    assert a["phases_completed"] == ["plan_outcome", "implement"]
    assert a["last_failure_reason"] == "error"
    assert by_id["task-b"]["state"] == "no_state"
    assert report["goal"] is None  # flat mode


def test_status_json_never_leaks_log_or_deferred_feedback(tmp_path, capsys):
    """IR-002/IR-004: last_failure_log and deferred feedback can carry secrets
    quoted from raw stderr/diffs — the JSON surface must omit them entirely."""
    orch = _orch()
    batch_dir = _make_batch(
        tmp_path,
        {
            "task-a": {
                "task_id": "task-a",
                "next_phase": "deferred",
                "last_failure_reason": "error",
                "last_failure_log": "stderr: Authorization: Bearer sk-SECRETTOKEN",
                "deferred_requirements": [
                    {
                        "phase": "review_code",
                        "backtrack_to": "implement",
                        "reason": "max_retries_exceeded",
                        "attempts": 4,
                        "feedback": "quoted diff with sk-FEEDBACKSECRET",
                    }
                ],
            }
        },
    )
    rc = orch.cmd_status(batch_dir, as_json=True)
    assert rc == 0
    out = capsys.readouterr().out
    assert "sk-SECRETTOKEN" not in out
    assert "sk-FEEDBACKSECRET" not in out
    task = json.loads(out)["tasks"][0]
    assert task["deferred"] == [
        {"phase": "review_code", "backtrack_to": "implement", "reason": "max_retries_exceeded", "attempts": 4}
    ]


def test_status_json_pr_url_from_state_or_file(tmp_path, capsys):
    orch = _orch()
    batch_dir = _make_batch(
        tmp_path,
        {
            "task-a": {"task_id": "task-a", "next_phase": "done", "pr_url": "https://github.com/x/y/pull/1"},
            "task-b": {"task_id": "task-b", "next_phase": "done"},
        },
    )
    (batch_dir / "tasks" / "task-b" / "pr_url.txt").write_text("https://github.com/x/y/pull/2\n", encoding="utf-8")
    by_id = {t["id"]: t for t in _json_status(orch, batch_dir, capsys)["tasks"]}
    assert by_id["task-a"]["pr_url"] == "https://github.com/x/y/pull/1"
    assert by_id["task-b"]["pr_url"] == "https://github.com/x/y/pull/2"


def test_status_json_gate_sentinel_surfaced(tmp_path, capsys):
    orch = _orch()
    batch_dir = _make_batch(tmp_path, {"task-a": {"task_id": "task-a", "next_phase": "human_gate_pr"}})
    task = _json_status(orch, batch_dir, capsys)["tasks"][0]
    assert task["gate_sentinel"] == "pr.reviewed"


def test_status_json_corrupt_state(tmp_path, capsys):
    orch = _orch()
    batch_dir = _make_batch(tmp_path, {"task-a": None})
    (batch_dir / "tasks" / "task-a" / "state.json").write_text("{not json", encoding="utf-8")
    task = _json_status(orch, batch_dir, capsys)["tasks"][0]
    assert task["state"] == "corrupt_state"


# ---------- goal-aware reporting ----------


def test_status_json_goal_progress(tmp_path, capsys):
    orch = _orch()
    batch_dir = _make_batch(
        tmp_path,
        {
            "task-a": {"task_id": "task-a", "next_phase": "done"},
            "task-b": {"task_id": "task-b", "next_phase": "implement"},
        },
        goal_json=_chain_manifest("task-a", "task-b"),
    )
    goal = _json_status(orch, batch_dir, capsys)["goal"]
    assert goal["valid"] is True
    assert goal["total"] == 2
    assert goal["done"] == 1
    assert goal["complete"] is False
    assert goal["incomplete_ids"] == ["task-b"]
    assert goal["deps"] == {"task-a": None, "task-b": "task-a"}


def test_status_json_goal_complete(tmp_path, capsys):
    orch = _orch()
    batch_dir = _make_batch(
        tmp_path,
        {
            "task-a": {"task_id": "task-a", "next_phase": "done"},
            "task-b": {"task_id": "task-b", "next_phase": "done"},
        },
        goal_json=_chain_manifest("task-a", "task-b"),
    )
    goal = _json_status(orch, batch_dir, capsys)["goal"]
    assert goal["complete"] is True
    assert goal["incomplete_ids"] == []


def test_status_json_invalid_goal_reported_not_raised(tmp_path, capsys):
    """status is read-only: an invalid manifest is reported, never raised —
    unlike start/resume, which (correctly) fail the batch closed."""
    orch = _orch()
    batch_dir = _make_batch(
        tmp_path,
        {"task-a": {"task_id": "task-a", "next_phase": "done"}},
        goal_json='{"goal": "x", "tasks": {}}',  # empty tasks → invalid
    )
    goal = _json_status(orch, batch_dir, capsys)["goal"]
    assert goal["valid"] is False
    assert "error" in goal


def test_status_human_goal_summary_line(tmp_path, capsys):
    orch = _orch()
    batch_dir = _make_batch(
        tmp_path,
        {
            "task-a": {"task_id": "task-a", "next_phase": "done"},
            "task-b": {"task_id": "task-b", "next_phase": "implement"},
        },
        goal_json=_chain_manifest("task-a", "task-b"),
    )
    assert orch.cmd_status(batch_dir) == 0
    out = capsys.readouterr().out
    assert "goal: 1/2 done — incomplete: task-b" in out


def test_status_human_flat_mode_has_no_goal_line(tmp_path, capsys):
    orch = _orch()
    batch_dir = _make_batch(tmp_path, {"task-a": {"task_id": "task-a", "next_phase": "done"}})
    assert orch.cmd_status(batch_dir) == 0
    assert "goal:" not in capsys.readouterr().out


# ---------- CLI dispatch ----------


def test_main_status_json_flag(tmp_path, capsys):
    orch = _orch()
    batch_dir = _make_batch(tmp_path, {"task-a": {"task_id": "task-a", "next_phase": "done"}})
    rc = orch.main(["orchestrator.py", "status", str(batch_dir), "--json"])
    assert rc == 0
    report = json.loads(capsys.readouterr().out)
    assert report["tasks"][0]["id"] == "task-a"


def test_main_status_rejects_unknown_extra_args(tmp_path, capsys):
    orch = _orch()
    batch_dir = _make_batch(tmp_path, {"task-a": None})
    rc = orch.main(["orchestrator.py", "status", str(batch_dir), "--jsn"])
    assert rc == 2
    assert "unknown status argument" in capsys.readouterr().err
