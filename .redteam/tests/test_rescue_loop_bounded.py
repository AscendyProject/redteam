"""The rescue-ENTRY route is bounded and defers at the ceiling (#87).

The agent-pair escalation branches (rescue_required; review_code blocker carried
over; review_code still rejecting after implement retries) route to `rescue` and
`continue` before the generic retry/stall accounting, so they had NO ceiling — a
sticky `retries["implement"] >= 2` re-routed every review_code CHANGES_REQUESTED to
rescue forever, never deferring (a ~1h52m runaway was observed). A dedicated
`rescue_entry_count` now caps rescue entries: `max_retries` are allowed, the next
one defers to a human.
"""

from __future__ import annotations

import json


def _orch():
    import _engine

    return _engine.orchestrator()


def _result(status="changes_requested", n=1):
    return {"status": status, "feedback": "still broken", "log": f"log-{n}", "diff": f"diff-{n}"}


def test_route_helper_allows_max_entries_then_defers(tmp_path):
    orch = _orch()
    task_dir = tmp_path / "t"
    task_dir.mkdir()
    state = {"task_id": "t", "max_retries_per_phase": 3}

    # entries 1..3 route to rescue (no defer)
    for i in range(1, 4):
        out = orch._route_to_rescue_or_defer(task_dir, state, "review_code", _result(n=i))
        assert out is None
        assert state["next_phase"] == "rescue"
        assert state["rescue_entry_count"] == i

    # the 4th entry (> max_retries) defers to a human with a terminal record
    out = orch._route_to_rescue_or_defer(task_dir, state, "review_code", _result(n=4))
    assert out == "deferred"
    assert state["next_phase"] == "deferred"
    assert state["rescue_entry_count"] == 4
    terminal = [r for r in state["deferred_requirements"] if r.get("reason") == "rescue_cycle_exceeded"]
    assert len(terminal) == 1 and terminal[0]["attempts"] == 4
    # #172: the refused 4th attempt never entered rescue, so it must not be counted.
    # Counting it would report 4 rescues for a task that took 3 and would penalise
    # exactly the configurations that reach the ceiling.
    assert state["rescue_total_count"] == 3


def test_route_helper_records_escalation_until_terminal_then_only_terminal(tmp_path):
    """rescue_required entries append their escalation record while under the ceiling;
    the terminal (deferring) entry appends ONLY the rescue_cycle_exceeded record, not
    another escalation (no duplicate)."""
    orch = _orch()
    task_dir = tmp_path / "t"
    task_dir.mkdir()
    state = {"task_id": "t", "max_retries_per_phase": 1}

    def esc():
        return {"phase": "review_code", "reason": "rescue_required", "feedback": "f"}

    # entry 1 (<= 1): routes to rescue, escalation recorded
    assert (
        orch._route_to_rescue_or_defer(
            task_dir, state, "review_code", _result("rescue_required"), escalation_record=esc()
        )
        is None
    )
    # entry 2 (> 1): defers, terminal record only
    assert (
        orch._route_to_rescue_or_defer(
            task_dir, state, "review_code", _result("rescue_required"), escalation_record=esc()
        )
        == "deferred"
    )
    reasons = [r["reason"] for r in state["deferred_requirements"]]
    assert reasons.count("rescue_required") == 1  # only the non-terminal entry
    assert reasons.count("rescue_cycle_exceeded") == 1


def test_review_code_defers_instead_of_looping_when_rescue_budget_exhausted(monkeypatch, tmp_path):
    """Integration: with the rescue-entry budget already spent, a review_code that
    still requests changes (after >=2 implement retries) DEFERS instead of routing to
    rescue again — closing the unbounded loop."""
    orch = _orch()
    task_dir = tmp_path / "batch" / "tasks" / "task-001"
    task_dir.mkdir(parents=True)
    (task_dir / "code_review.done").write_text("", encoding="utf-8")
    state = {
        "task_id": "task-001",
        "mode": "agent-pair",
        "base_branch": "main",
        "phase": "review_code",
        "phases_completed": ["plan_outcome", "plan_review", "human_gate_outcome", "implement"],
        "next_phase": "review_code",
        "review_items": [],
        "retries": {"implement": 2},
        "rescue_entry_count": 2,  # budget already spent (max_retries_per_phase=2)
        "max_retries_per_phase": 2,
        "verification": {"last_exit_code": 0},
        "escape": {"ask_user": False, "reason": None, "return_phase": None},
    }
    (task_dir / "state.json").write_text(json.dumps(state), encoding="utf-8")

    monkeypatch.setattr(orch, "_ensure_task_branch", lambda task_id, repo, branch_prefix, base_branch: f"p/{task_id}")
    monkeypatch.setattr(orch, "repo_root", lambda: tmp_path)
    monkeypatch.setitem(
        orch.PHASE_RUNNERS,
        "review_code",
        lambda task_dir, state: {
            "status": "changes_requested",
            "feedback": "still broken",
            "log": "IR-001 severity:major status:open\nREVIEW_DECISION: CHANGES_REQUESTED",
            "diff": "diff",
        },
    )

    outcome = orch.process_task(task_dir)
    saved = json.loads((task_dir / "state.json").read_text(encoding="utf-8"))

    assert outcome == "deferred"  # NOT blocked_on_human_gate / not routed to rescue again
    assert saved["next_phase"] == "deferred"
    assert any(r.get("reason") == "rescue_cycle_exceeded" for r in saved["deferred_requirements"])


def test_cumulative_rescue_counter_tracks_entries_without_touching_the_budget(tmp_path):
    """#172: rescue_total_count accumulates alongside the budget counter.

    The budget (rescue_entry_count) must keep its exact semantics — it is what
    bounds the #87 runaway — so this asserts both move together here, and a
    separate test covers the counter surviving the convergence reset that zeroes
    the budget.
    """
    orch = _orch()
    task_dir = tmp_path / "t"
    task_dir.mkdir()
    state = {"task_id": "t", "max_retries_per_phase": 3}

    for i in range(1, 4):
        orch._route_to_rescue_or_defer(task_dir, state, "review_code", _result(n=i))
        assert state["rescue_entry_count"] == i  # budget unchanged in behaviour
        assert state["rescue_total_count"] == i  # cumulative tracks actual entries

    # The 4th is refused: the budget still counts the attempt, the total does not.
    orch._route_to_rescue_or_defer(task_dir, state, "review_code", _result(n=4))
    assert state["rescue_entry_count"] == 4
    assert state["rescue_total_count"] == 3

    # Convergence zeroes the BUDGET only; the cumulative total must survive, or a
    # successful task reports zero rescues no matter how many it actually took.
    state["rescue_entry_count"] = 0
    assert state["rescue_total_count"] == 3
