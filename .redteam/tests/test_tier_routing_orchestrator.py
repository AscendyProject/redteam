"""Tier-aware routing — orchestrator integration (issue #13, phase 2).

process_task resolves a fresh task's tier once, then builds its phase order from
the profile's declarative toggles (review/gates) and applies per-tier model
overrides. Off (no [tiers]) → unchanged.
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
review = false
models = { implementer = "claude-haiku-4-5" }

[tiers.2]
review = true
gates = ["pr"]

[tiers.4]
review = true
gates = ["outcome", "pr", "rescue"]

[tier_triggers]
"**/auth/**" = 4
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


def _approve(td, st):
    return {"status": "approved", "feedback": "", "log": "", "diff": ""}


def _ask(td, st):  # halts the loop at a gate immediately (no real subprocess)
    return {"status": "ask_user", "feedback": "stop", "log": "", "diff": ""}


def test_build_tier_phase_order_composes_safely(monkeypatch, tmp_path):
    """The engine builds the order from toggles; review/gates always compose with
    a correctly-placed rescue + create_pr tail (no unsafe skip)."""
    orch = _load_orchestrator()
    from config import TierProfile  # type: ignore

    # review off → single-agent, no review/rescue, lean (no gates)
    assert orch._build_tier_phase_order(TierProfile(review=False, gates=())) == [
        "plan_outcome",
        "implement",
        "create_pr",
        "done",
    ]
    # review on, no gates → adversarial pair, rescue slot present, no human gates
    assert orch._build_tier_phase_order(TierProfile(review=True, gates=())) == [
        "plan_outcome",
        "plan_review",
        "implement",
        "review_code",
        "rescue",
        "create_pr",
        "done",
    ]
    # all gates → human checkpoints inserted at the right points
    assert orch._build_tier_phase_order(TierProfile(review=True, gates=("outcome", "pr", "rescue"))) == [
        "plan_outcome",
        "plan_review",
        "human_gate_outcome",
        "implement",
        "review_code",
        "rescue",
        "human_gate_rescue",
        "create_pr",
        "human_gate_pr",
        "done",
    ]


def test_routing_off_when_no_tiers(monkeypatch, tmp_path):
    orch = _load_orchestrator()
    task_dir = _setup(
        tmp_path,
        '[project]\nname="p"\nsource_dirs=["app/"]\ntest_file_glob="*.py"\nverification_allowlist=["pytest"]\n',
        "brief",
    )
    _common_mocks(orch, monkeypatch, tmp_path)
    monkeypatch.setitem(orch.PHASE_RUNNERS, "plan_outcome", _ask)
    orch.process_task(task_dir)
    saved = json.loads((task_dir / "state.json").read_text())
    assert "tier" not in saved
    assert "tier_phases" not in saved


def test_tier0_review_false_is_single_agent_with_model_override(monkeypatch, tmp_path):
    """Front-matter tier=0 (review=false) → plan→implement→PR, no review/gates,
    cheap model merged. The verification snapshot is taken at plan_outcome (no
    plan_review exists). Runs to done without any review phase."""
    orch = _load_orchestrator()
    task_dir = _setup(tmp_path, _CFG_WITH_TIERS, "+++\ntier = 0\n+++\nbrief")
    _common_mocks(orch, monkeypatch, tmp_path)
    ran = []

    def _plan(td, st):
        # mimic the real plan_outcome: write outcome.md with a verification block
        (td / "outcome.md").write_text(
            "# Outcome\n## Verification\n```yaml\ncommands:\n  - pytest\n```\n", encoding="utf-8"
        )
        ran.append("plan_outcome")
        return _approve(td, st)

    monkeypatch.setitem(orch.PHASE_RUNNERS, "plan_outcome", _plan)
    for ph in ("implement", "create_pr"):
        monkeypatch.setitem(
            orch.PHASE_RUNNERS, ph, (lambda name: lambda td, st: ran.append(name) or _approve(td, st))(ph)
        )

    outcome = orch.process_task(task_dir)
    saved = json.loads((task_dir / "state.json").read_text())

    assert saved["tier"] == 0
    assert saved["tier_phases"] == ["plan_outcome", "implement", "create_pr", "done"]
    assert saved["models"]["implementer"] == "claude-haiku-4-5"  # tier override merged
    assert ran == ["plan_outcome", "implement", "create_pr"]  # no review_code
    assert outcome == "done"
    # security boundary preserved: the allowlist snapshot happened despite no plan_review
    assert saved["verification"].get("verify_allowlist") == ["pytest"]


def test_auth_trigger_floors_to_full_review_tier(monkeypatch, tmp_path):
    """Declared tier 0 but paths touch auth → trigger floors to 4 (can't lower);
    the built order has the full adversarial pair + all human gates."""
    orch = _load_orchestrator()
    task_dir = _setup(tmp_path, _CFG_WITH_TIERS, '+++\ntier = 0\npaths = ["app/auth/login.py"]\n+++\nbrief')
    _common_mocks(orch, monkeypatch, tmp_path)
    monkeypatch.setitem(orch.PHASE_RUNNERS, "plan_outcome", _ask)

    orch.process_task(task_dir)
    saved = json.loads((task_dir / "state.json").read_text())

    assert saved["tier"] == 4  # floored up by the auth trigger, not lowered to 0
    assert "plan_review" in saved["tier_phases"]
    assert "review_code" in saved["tier_phases"]
    assert "human_gate_pr" in saved["tier_phases"]


def test_default_pipeline_has_no_pr_gate(monkeypatch, tmp_path):
    """The lean default (no tiers): create_pr → done, no human_gate_pr (the draft
    PR is the human checkpoint)."""
    orch = _load_orchestrator()
    assert "human_gate_pr" not in orch.AGENT_PAIR_PHASE_ORDER
    assert orch._next_phase({"mode": "agent-pair"}, "create_pr") == "done"
