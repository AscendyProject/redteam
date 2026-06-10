"""Tier-aware routing — orchestrator integration (issue #13, phase 2).

process_task resolves a fresh task's tier once, then routes the phase order and
per-role models from the resolved profile. Off (no [tiers]) → unchanged.
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


_CFG_WITH_TIERS = """
[project]
name = "p"
source_dirs = ["app/"]
test_file_glob = "test_*.py"
verification_allowlist = ["pytest"]

[models]
implementer = "claude-sonnet-4-6"

[tiers.0]
phases = ["implement"]
models = { implementer = "claude-haiku-4-5" }

[tiers.2]
phases = ["plan_outcome", "implement", "review_code", "create_pr"]

[tier_triggers]
"**/auth/**" = 2
default = 0
"""


def _setup(tmp_path: Path, cfg_body: str, input_md: str):
    (tmp_path / ".redteam").mkdir()
    (tmp_path / ".redteam" / "config.toml").write_text(cfg_body, encoding="utf-8")
    task_dir = tmp_path / "batch" / "tasks" / "task-001"
    task_dir.mkdir(parents=True)
    (task_dir / "input.md").write_text(input_md, encoding="utf-8")
    state = {
        "task_id": "task-001",
        "mode": "agent-pair",
        "phase": "created",
        "phases_completed": [],
        "next_phase": "plan_outcome",
        "models": {"implementer": "claude-sonnet-4-6"},
        "retries": {},
        "max_retries_per_phase": 2,
        "verification": {},
        "escape": {"ask_user": False, "reason": None, "return_phase": None},
    }
    (task_dir / "state.json").write_text(json.dumps(state), encoding="utf-8")
    return task_dir


def _common_mocks(orch, monkeypatch, tmp_path):
    monkeypatch.setattr(orch, "repo_root", lambda: tmp_path)
    monkeypatch.setattr(orch, "_ensure_task_branch", lambda task_id, repo, bp, bb: f"{bp}/{task_id}")


def test_routing_off_when_no_tiers(monkeypatch, tmp_path):
    """No [tiers] → no tier resolved, no tier_phases, default order honored."""
    orch = _load_orchestrator()
    task_dir = _setup(
        tmp_path,
        '[project]\nname="p"\nsource_dirs=["app/"]\ntest_file_glob="*.py"\nverification_allowlist=["pytest"]\n',
        "brief",
    )
    _common_mocks(orch, monkeypatch, tmp_path)
    # ask_user halts the loop at a gate immediately (no real reviewer/worker subprocess).
    monkeypatch.setitem(
        orch.PHASE_RUNNERS,
        "plan_outcome",
        lambda td, st: {"status": "ask_user", "feedback": "stop", "log": "", "diff": ""},
    )
    orch.process_task(task_dir)
    saved = json.loads((task_dir / "state.json").read_text())
    assert "tier" not in saved
    assert "tier_phases" not in saved


def test_tier0_profile_drives_phases_and_models(monkeypatch, tmp_path):
    """Front-matter tier=0 → profile phases=['implement'] + cheap model; the run
    routes implement → done using the tier order, not the default pipeline."""
    orch = _load_orchestrator()
    task_dir = _setup(tmp_path, _CFG_WITH_TIERS, "+++\ntier = 0\n+++\nbrief")
    _common_mocks(orch, monkeypatch, tmp_path)
    ran = []
    monkeypatch.setitem(
        orch.PHASE_RUNNERS,
        "implement",
        lambda td, st: ran.append("implement") or {"status": "approved", "feedback": "", "log": "", "diff": ""},
    )

    outcome = orch.process_task(task_dir)
    saved = json.loads((task_dir / "state.json").read_text())

    assert saved["tier"] == 0
    assert saved["tier_phases"] == ["implement"]
    assert saved["models"]["implementer"] == "claude-haiku-4-5"  # tier override merged
    assert ran == ["implement"]
    assert outcome == "done"
    assert saved["next_phase"] == "done"


def test_path_trigger_floors_tier_above_explicit(monkeypatch, tmp_path):
    """Front-matter declares tier 0 but paths touch auth → trigger floors to 2;
    the heavier profile's phase order is used (can't be lowered)."""
    orch = _load_orchestrator()
    task_dir = _setup(tmp_path, _CFG_WITH_TIERS, '+++\ntier = 0\npaths = ["app/auth/login.py"]\n+++\nbrief')
    _common_mocks(orch, monkeypatch, tmp_path)
    monkeypatch.setitem(
        orch.PHASE_RUNNERS,
        "plan_outcome",
        lambda td, st: {"status": "ask_user", "feedback": "stop", "log": "", "diff": ""},
    )

    orch.process_task(task_dir)
    saved = json.loads((task_dir / "state.json").read_text())

    assert saved["tier"] == 2  # floored up by the auth trigger, not lowered to 0
    assert saved["tier_phases"][0] == "plan_outcome"


def test_unknown_phase_in_profile_fails_closed(monkeypatch, tmp_path):
    orch = _load_orchestrator()
    cfg = '[project]\nname="p"\nsource_dirs=["app/"]\ntest_file_glob="*.py"\nverification_allowlist=["pytest"]\n[tiers.0]\nphases=["nope_not_a_phase"]\n[tier_triggers]\ndefault=0\n'
    task_dir = _setup(tmp_path, cfg, "brief")
    _common_mocks(orch, monkeypatch, tmp_path)

    outcome = orch.process_task(task_dir)
    saved = json.loads((task_dir / "state.json").read_text())

    assert outcome == "error"
    assert saved["next_phase"] == "deferred"
    assert saved["last_failure_reason"] == "tier_resolution_failed"
