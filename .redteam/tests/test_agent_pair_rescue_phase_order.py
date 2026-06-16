"""Regression: agent-pair rescue must reach human_gate_rescue, not fall to done.

Bug: AGENT_PAIR_PHASE_ORDER omitted `rescue` and `human_gate_rescue`. The
orchestrator enters rescue conditionally by setting next_phase="rescue"
(orchestrator.py rescue_required / review_code-blocker paths). After rescue
runs and is approved, the generic transition `_next_phase(state, "rescue")`
looked rescue up in the order, did not find it, and returned "done"
(orchestrator.py _next_phase: `if current not in phase_order: return "done"`).
So a Codex rescue in agent-pair mode bypassed BOTH the human gate
(`human_gate_rescue`) and PR creation — silently going straight to done.

Fix: add `rescue` + `human_gate_rescue` to AGENT_PAIR_PHASE_ORDER between
review_code and create_pr. The normal path is unaffected because
review_code-approved sets next_phase="create_pr" explicitly (it never
consults _next_phase).
"""

from __future__ import annotations


def _load_orchestrator_module():
    import _engine

    return _engine.orchestrator()


def test_agent_pair_rescue_advances_to_human_gate_not_done():
    """The bug: _next_phase('rescue') returned 'done' in agent-pair mode."""
    orch = _load_orchestrator_module()
    state = {"mode": "agent-pair"}
    assert orch._next_phase(state, "rescue") == "human_gate_rescue"
    assert orch._next_phase(state, "human_gate_rescue") == "create_pr"


def test_agent_pair_normal_flow_unaffected_by_rescue_insertion():
    """Inserting rescue into the order must not divert the happy path:
    implement still flows to review_code, and human_gate_pr to done."""
    orch = _load_orchestrator_module()
    state = {"mode": "agent-pair"}
    assert orch._next_phase(state, "implement") == "review_code"
    assert orch._next_phase(state, "human_gate_pr") == "done"


def test_tdd_mode_phase_order_unchanged():
    """TDD mode already had rescue/human_gate_rescue; confirm it still does."""
    orch = _load_orchestrator_module()
    state = {"mode": "tdd"}
    assert orch._next_phase(state, "rescue") == "human_gate_rescue"
    assert orch._next_phase(state, "human_gate_rescue") == "create_pr"
