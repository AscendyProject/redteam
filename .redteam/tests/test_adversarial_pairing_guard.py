"""Adversarial-pairing guard — refuse self-review when the configured reviewer
collapses to the worker's own provider.

The redteam harness's whole premise is a cross-provider pair: one model writes,
a DIFFERENT model reviews. If the reviewer is silently pointed at the worker's
own provider (e.g. an agent flipping reviewer="claude" while the worker is also
claude), the code is reviewed by the same model that wrote it — self-review,
which defeats the point of the harness. These tests pin:
- the provider resolvers map config values to a provider family (codex/claude/None);
- the orchestrator's policy helper flags only a genuine same-provider collapse
  on a task that actually runs a reviewer phase;
- process_task fails closed (defers, runs no phase) on a collapsed pairing, and
  a real cross-provider pair passes the gate.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

_WF = Path(__file__).resolve().parents[1] / "workflows"
if str(_WF) not in sys.path:
    sys.path.insert(0, str(_WF))

from adapters import reviewer_provider, worker_provider  # noqa: E402


def _load_orchestrator_module():
    import _engine

    return _engine.orchestrator()


# ---- provider resolvers ----


def test_reviewer_provider_resolves_family() -> None:
    assert reviewer_provider({"models": {"reviewer": "codex"}}) == "codex"
    assert reviewer_provider({"models": {"reviewer": "claude"}}) == "claude"
    # A non-adapter value (manual/human review) is a distinct adversary → None.
    assert reviewer_provider({"models": {"reviewer": "human"}}) is None
    # Absent reviewer inherits the shipped default (reviewer="codex").
    assert reviewer_provider({}) == "codex"


def test_worker_provider_resolves_family() -> None:
    assert worker_provider({"models": {"implementer": "codex"}}) == "codex"
    assert worker_provider({"models": {"implementer": "claude-sonnet-4-6"}}) == "claude"
    # Absent implementer inherits the shipped default (a claude model) → claude.
    assert worker_provider({}) == "claude"


# ---- policy helper ----


def test_same_provider_collapse_is_flagged() -> None:
    """worker=claude + reviewer=claude on an agent-pair task → self-review."""
    orch = _load_orchestrator_module()
    state = {"mode": "agent-pair", "models": {"implementer": "claude-sonnet-4-6", "reviewer": "claude"}}
    err = orch._adversarial_pairing_error(state)
    assert err is not None
    assert "self-review" in err and "claude" in err


def test_codex_self_review_is_flagged() -> None:
    """Role reversal still must be cross-provider: codex worker + codex reviewer
    is the same collapse the other way round."""
    orch = _load_orchestrator_module()
    state = {"mode": "agent-pair", "models": {"implementer": "codex", "reviewer": "codex"}}
    assert orch._adversarial_pairing_error(state) is not None


def test_cross_provider_pair_passes() -> None:
    orch = _load_orchestrator_module()
    # claude worker / codex reviewer (the shipped default shape) and the reverse.
    assert (
        orch._adversarial_pairing_error(
            {"mode": "agent-pair", "models": {"implementer": "claude-sonnet-4-6", "reviewer": "codex"}}
        )
        is None
    )
    assert (
        orch._adversarial_pairing_error(
            {"mode": "agent-pair", "models": {"implementer": "codex", "reviewer": "claude"}}
        )
        is None
    )


def test_human_reviewer_passes() -> None:
    """A manual/human reviewer is always a distinct adversary, even with a claude
    worker — no headless adapter, so no self-review risk."""
    orch = _load_orchestrator_module()
    state = {"mode": "agent-pair", "models": {"implementer": "claude-sonnet-4-6", "reviewer": "human"}}
    assert orch._adversarial_pairing_error(state) is None


def test_single_agent_tier_passes_despite_same_provider() -> None:
    """A review=false (single-agent) phase order has no reviewer phase, so a
    same-provider config is an explicit operator choice, not a silent collapse."""
    orch = _load_orchestrator_module()
    state = {
        "mode": "agent-pair",
        "tier_phases": ["plan_outcome", "implement", "create_pr", "done"],
        "models": {"implementer": "claude-sonnet-4-6", "reviewer": "claude"},
    }
    assert orch._adversarial_pairing_error(state) is None


def test_tdd_mode_same_provider_passes() -> None:
    """TDD mode reviews via the WORKER adapter (get_worker_adapter role=reviewer),
    not a headless reviewer adapter, so its reviewer is the same agent test-first
    by design. The guard polices only the agent-pair headless path; a same-provider
    TDD config must NOT be flagged (would be a false positive). Regression for the
    #28 review finding PR-001."""
    orch = _load_orchestrator_module()
    state = {"mode": "tdd", "models": {"implementer": "codex", "reviewer": "codex"}}
    assert orch._adversarial_pairing_error(state) is None


def test_tier_plan_review_flagged_even_in_tdd_mode() -> None:
    """plan_review always runs the HEADLESS reviewer adapter regardless of mode, so
    a tier-routed order containing plan_review is a same-provider self-review risk
    even when mode="tdd". The guard must fire here — gating purely on
    mode=="agent-pair" would miss it (#36 review finding PR-001, HIGH)."""
    orch = _load_orchestrator_module()
    state = {
        "mode": "tdd",
        "tier_phases": ["plan_outcome", "plan_review", "implement", "review_code", "rescue", "create_pr", "done"],
        "models": {"implementer": "codex", "reviewer": "codex"},
    }
    err = orch._adversarial_pairing_error(state)
    assert err is not None and "self-review" in err


# ---- orchestrator integration ----


def _seed_task(tmp_path: Path, state: dict) -> Path:
    task_dir = tmp_path / "batch" / "tasks" / "task-001-demo"
    task_dir.mkdir(parents=True)
    (task_dir / "input.md").write_text("brief", encoding="utf-8")
    (task_dir / "outcome.md").write_text("plan", encoding="utf-8")
    (task_dir / "outcome.approved").write_text("", encoding="utf-8")
    (task_dir / "state.json").write_text(json.dumps(state), encoding="utf-8")
    return task_dir


def test_process_task_defers_on_self_review(monkeypatch, tmp_path) -> None:
    """A collapsed pairing fails closed: the task defers with a clear reason and
    NO phase runner is ever invoked."""
    orch = _load_orchestrator_module()
    state = {
        "task_id": "task-001-demo",
        "mode": "agent-pair",
        "phase": "plan_review",
        "phases_completed": ["plan_outcome"],
        "next_phase": "plan_review",
        "models": {"implementer": "claude-sonnet-4-6", "reviewer": "claude"},
        "escape": {"ask_user": False, "reason": None, "return_phase": None},
    }
    task_dir = _seed_task(tmp_path, state)
    monkeypatch.setattr(
        orch, "_ensure_task_branch", lambda task_id, repo, branch_prefix, base_branch: f"redteam/{task_id}"
    )
    monkeypatch.setattr(orch, "repo_root", lambda: tmp_path)
    ran = {"any": False}

    def _boom(td, st):
        ran["any"] = True
        return {"status": "ask_user", "feedback": "should never run", "log": "", "diff": ""}

    monkeypatch.setitem(orch.PHASE_RUNNERS, "plan_review", _boom)

    outcome = orch.process_task(task_dir)
    assert outcome == "error"
    assert ran["any"] is False  # guard ran before any phase
    persisted = json.loads((task_dir / "state.json").read_text(encoding="utf-8"))
    assert persisted["last_failure_reason"] == "adversarial_pairing_violation"
    assert persisted["next_phase"] == "deferred"


def test_process_task_cross_provider_passes_guard(monkeypatch, tmp_path) -> None:
    """A real cross-provider pair (claude worker / codex reviewer) passes the
    guard and proceeds to run the phase."""
    orch = _load_orchestrator_module()
    state = {
        "task_id": "task-001-demo",
        "mode": "agent-pair",
        "phase": "plan_review",
        "phases_completed": ["plan_outcome"],
        "next_phase": "plan_review",
        "models": {"implementer": "claude-sonnet-4-6", "reviewer": "codex"},
        "escape": {"ask_user": False, "reason": None, "return_phase": None},
    }
    task_dir = _seed_task(tmp_path, state)
    monkeypatch.setattr(
        orch, "_ensure_task_branch", lambda task_id, repo, branch_prefix, base_branch: f"redteam/{task_id}"
    )
    monkeypatch.setattr(orch, "repo_root", lambda: tmp_path)
    ran = {"plan_review": False}

    def _fake_plan_review(td, st):
        ran["plan_review"] = True
        # Halt right after the phase instead of advancing into implement.
        return {"status": "ask_user", "feedback": "halt", "log": "", "diff": ""}

    monkeypatch.setitem(orch.PHASE_RUNNERS, "plan_review", _fake_plan_review)

    orch.process_task(task_dir)
    assert ran["plan_review"] is True  # guard passed; phase ran
