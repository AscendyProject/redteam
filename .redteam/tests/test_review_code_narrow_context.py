"""Tests for round-over-round narrowed reviewer context (#92 Proposal 2).

The agent-pair branch of review_code.run() selects between the full-diff
prompt and a narrowed prompt (incremental delta + carried-over open findings)
based on state and git probe results.  All git I/O and the reviewer adapter
are monkeypatched — no codex/claude subprocess, no network, no real git remote.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

_WF = Path(__file__).resolve().parents[1] / "workflows"
if str(_WF) not in sys.path:
    sys.path.insert(0, str(_WF))

import phase_runners.review_code as review_code  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_PRIOR_REV = "abc1234567890abc"
_HEAD_REV = "deadbeef12345678"
_BASE_BRANCH = "main"

_OPEN_ITEM: dict = {
    "id": "IR-001",
    "severity": "major",
    "status": "open",
    "summary": "IR-001 severity:major status:open — dangerous call",
    "carry_over_count": 1,
    "phase": "review_code",
}


def _agent_pair_state(**extra) -> dict:
    base = {
        "mode": "agent-pair",
        "base_branch": _BASE_BRANCH,
        "models": {"reviewer": "codex"},
        "review_items": [],
    }
    base.update(extra)
    return base


def _approved_result() -> dict:
    return {
        "decision": "APPROVED",
        "raw": "Looks good.\nREVIEW_DECISION: APPROVED",
        "parse_status": "ok",
    }


def _changes_result(raw: str | None = None) -> dict:
    raw = raw or "IR-002 severity:major status:open — new bug\nREVIEW_DECISION: CHANGES_REQUESTED"
    return {"decision": "CHANGES_REQUESTED", "raw": raw, "parse_status": "ok"}


def _run_with(
    tmp_path: Path,
    state: dict,
    rwf_return: dict,
    *,
    is_ancestor: bool = True,
    diff_nonempty: bool = True,
    head_rev: str = _HEAD_REV,
):
    """Call review_code.run() with the given state, returning (result, rwf_mock)."""
    with (
        patch("phase_runners.review_code.get_reviewer_adapter", return_value=MagicMock()),
        patch("phase_runners.review_code.review_with_fallback", return_value=rwf_return) as rwf,
        patch("phase_runners.review_code.compute_repo_diff", return_value=""),
        patch("phase_runners.review_code.repo_root", return_value=tmp_path),
        patch("phase_runners.review_code.git_rev_parse", return_value=head_rev),
        patch("phase_runners.review_code._is_ancestor", return_value=is_ancestor),
        patch("phase_runners.review_code._incremental_diff_nonempty", return_value=diff_nonempty),
    ):
        res = review_code.run(tmp_path, state)
    return res, rwf


# ---------------------------------------------------------------------------
# Test 1 — First round → full diff
# ---------------------------------------------------------------------------


def test_first_round_uses_full_diff_prompt(tmp_path: Path) -> None:
    """No last_reviewed_rev in state → full-diff prompt, byte-identical to _code_review_prompt."""
    state = _agent_pair_state()  # no last_reviewed_rev, review_items=[]
    res, rwf = _run_with(tmp_path, state, _approved_result())

    assert res["status"] == "approved"
    kwargs = rwf.call_args.kwargs if hasattr(rwf.call_args, "kwargs") else rwf.call_args[1]
    actual_prompt = kwargs["prompt"]
    expected_prompt = review_code._code_review_prompt(tmp_path, _BASE_BRANCH)
    assert actual_prompt == expected_prompt, "First round must use the byte-identical full-diff prompt"


def test_first_round_writes_last_reviewed_rev(tmp_path: Path) -> None:
    """After a successful first round, state['last_reviewed_rev'] is set to HEAD SHA."""
    state = _agent_pair_state()
    _run_with(tmp_path, state, _approved_result(), head_rev=_HEAD_REV)
    assert state.get("last_reviewed_rev") == _HEAD_REV


# ---------------------------------------------------------------------------
# Test 2 — Subsequent round → narrowed prompt
# ---------------------------------------------------------------------------


def test_subsequent_round_uses_narrowed_prompt(tmp_path: Path) -> None:
    """With prior rev + open items + passing probes → narrowed prompt."""
    state = _agent_pair_state(last_reviewed_rev=_PRIOR_REV, review_items=[_OPEN_ITEM])
    res, rwf = _run_with(tmp_path, state, _approved_result(), is_ancestor=True, diff_nonempty=True)

    assert res["status"] == "approved"
    kwargs = rwf.call_args.kwargs if hasattr(rwf.call_args, "kwargs") else rwf.call_args[1]
    prompt = kwargs["prompt"]

    # Must reference the incremental range
    assert f"Review `git diff {_PRIOR_REV}...HEAD`." in prompt, "narrowed prompt must name the prior rev range"
    # Must include each open item field
    assert _OPEN_ITEM["id"] in prompt
    assert f"severity:{_OPEN_ITEM['severity']}" in prompt
    assert f"status:{_OPEN_ITEM['status']}" in prompt
    assert "dangerous call" in prompt  # summary substring


def test_subsequent_round_excludes_full_range(tmp_path: Path) -> None:
    """The narrowed prompt must NOT embed the full <base>...HEAD range."""
    state = _agent_pair_state(last_reviewed_rev=_PRIOR_REV, review_items=[_OPEN_ITEM])
    _, rwf = _run_with(tmp_path, state, _approved_result(), is_ancestor=True, diff_nonempty=True)

    kwargs = rwf.call_args.kwargs if hasattr(rwf.call_args, "kwargs") else rwf.call_args[1]
    prompt = kwargs["prompt"]
    assert f"git diff {_BASE_BRANCH}...HEAD" not in prompt, "full accumulated range must not be re-embedded"


def test_subsequent_round_target_uses_pinned_base(tmp_path: Path) -> None:
    """target.base must remain the pinned base branch even on the narrowed path."""
    state = _agent_pair_state(last_reviewed_rev=_PRIOR_REV, review_items=[_OPEN_ITEM])
    _, rwf = _run_with(tmp_path, state, _approved_result(), is_ancestor=True, diff_nonempty=True)

    kwargs = rwf.call_args.kwargs if hasattr(rwf.call_args, "kwargs") else rwf.call_args[1]
    assert kwargs["target"] == {"kind": "branch_diff", "base": _BASE_BRANCH}


# ---------------------------------------------------------------------------
# Test 3 — New issue in the delta is still caught
# ---------------------------------------------------------------------------


def test_new_issue_in_delta_caught_on_narrowed_path(tmp_path: Path) -> None:
    """A new IR-002 raised by the reviewer on the incremental delta flows through."""
    state = _agent_pair_state(last_reviewed_rev=_PRIOR_REV, review_items=[_OPEN_ITEM])
    raw = "IR-002 severity:major status:open — new bug found\nREVIEW_DECISION: CHANGES_REQUESTED"
    res, _ = _run_with(tmp_path, state, _changes_result(raw=raw), is_ancestor=True, diff_nonempty=True)

    assert res["status"] == "changes_requested"
    assert "IR-002" in res["log"], "log must carry the new IR-002 finding"


def test_new_issue_syncs_into_review_items(tmp_path: Path) -> None:
    """_sync_review_items on the runner log lands IR-002 with status=open, carry_over_count=1."""
    import _engine

    orch = _engine.orchestrator()

    state = _agent_pair_state(last_reviewed_rev=_PRIOR_REV, review_items=[_OPEN_ITEM])
    raw = "IR-002 severity:major status:open — new bug found\nREVIEW_DECISION: CHANGES_REQUESTED"
    res, _ = _run_with(tmp_path, state, _changes_result(raw=raw), is_ancestor=True, diff_nonempty=True)

    # Simulate what the orchestrator does after run() returns
    sync_state: dict = {"review_items": []}
    orch._sync_review_items(sync_state, "review_code", res["log"])

    items_by_id = {i["id"]: i for i in sync_state["review_items"]}
    assert "IR-002" in items_by_id, "IR-002 must be synced into review_items"
    ir002 = items_by_id["IR-002"]
    assert ir002["status"] == "open"
    assert ir002["carry_over_count"] == 1


# ---------------------------------------------------------------------------
# Test 4 — Fail-safe fallback
# ---------------------------------------------------------------------------


def _assert_uses_full_prompt(tmp_path: Path, state: dict, **run_kwargs) -> None:
    res, rwf = _run_with(tmp_path, state, _approved_result(), **run_kwargs)
    kwargs = rwf.call_args.kwargs if hasattr(rwf.call_args, "kwargs") else rwf.call_args[1]
    expected = review_code._code_review_prompt(tmp_path, _BASE_BRANCH)
    assert kwargs["prompt"] == expected, "fail-safe must produce byte-identical full-diff prompt"
    assert res["status"] == "approved"


def test_fallback_no_last_reviewed_rev(tmp_path: Path) -> None:
    """4a: last_reviewed_rev absent → full diff."""
    state = _agent_pair_state(review_items=[_OPEN_ITEM])  # no last_reviewed_rev
    _assert_uses_full_prompt(tmp_path, state)


def test_fallback_not_ancestor(tmp_path: Path) -> None:
    """4b: last_reviewed_rev present but _is_ancestor returns False → full diff."""
    state = _agent_pair_state(last_reviewed_rev=_PRIOR_REV, review_items=[_OPEN_ITEM])
    _assert_uses_full_prompt(tmp_path, state, is_ancestor=False)


def test_fallback_empty_incremental_diff(tmp_path: Path) -> None:
    """4c: _incremental_diff_nonempty returns False → full diff."""
    state = _agent_pair_state(last_reviewed_rev=_PRIOR_REV, review_items=[_OPEN_ITEM])
    _assert_uses_full_prompt(tmp_path, state, is_ancestor=True, diff_nonempty=False)


def test_fallback_no_open_items_empty_list(tmp_path: Path) -> None:
    """4d-i: review_items empty → full diff."""
    state = _agent_pair_state(last_reviewed_rev=_PRIOR_REV, review_items=[])
    _assert_uses_full_prompt(tmp_path, state)


def test_fallback_no_open_items_all_closed(tmp_path: Path) -> None:
    """4d-ii: all review_items status != 'open' → full diff."""
    closed_item = dict(_OPEN_ITEM, status="resolved")
    state = _agent_pair_state(last_reviewed_rev=_PRIOR_REV, review_items=[closed_item])
    _assert_uses_full_prompt(tmp_path, state)


# ---------------------------------------------------------------------------
# Test 5 — Contract intact on the narrowed path
# ---------------------------------------------------------------------------


def test_manual_required_no_rev_mutation(tmp_path: Path) -> None:
    """5a: MANUAL_REQUIRED result → status=manual_required, last_reviewed_rev unchanged."""
    state = _agent_pair_state(last_reviewed_rev=_PRIOR_REV, review_items=[_OPEN_ITEM])
    manual_result = {"decision": "MISSING", "raw": "needs human", "parse_status": "manual_required"}

    res, _ = _run_with(tmp_path, state, manual_result, is_ancestor=True, diff_nonempty=True)

    assert res["status"] == "manual_required"
    assert state.get("last_reviewed_rev") == _PRIOR_REV, "MANUAL_REQUIRED must not mutate last_reviewed_rev"


def test_unparseable_result_no_rev_mutation(tmp_path: Path) -> None:
    """5b: parse_status != 'ok' → status=error, last_reviewed_rev unchanged."""
    state = _agent_pair_state(last_reviewed_rev=_PRIOR_REV, review_items=[_OPEN_ITEM])
    bad_result = {"decision": "MISSING", "raw": "garbage", "parse_status": "unparseable"}

    res, _ = _run_with(tmp_path, state, bad_result, is_ancestor=True, diff_nonempty=True)

    assert res["status"] == "error"
    assert state.get("last_reviewed_rev") == _PRIOR_REV, "parse error must not mutate last_reviewed_rev"


def test_code_review_md_written_on_success(tmp_path: Path) -> None:
    """5c: on a successful ok decision, code_review.md is written with utf-8 encoding."""
    state = _agent_pair_state(last_reviewed_rev=_PRIOR_REV, review_items=[_OPEN_ITEM])
    review_body = "Looks good.\nREVIEW_DECISION: APPROVED"
    res, _ = _run_with(
        tmp_path,
        state,
        {"decision": "APPROVED", "raw": review_body, "parse_status": "ok"},
        is_ancestor=True,
        diff_nonempty=True,
    )

    assert res["status"] == "approved"
    written = (tmp_path / "code_review.md").read_text(encoding="utf-8")
    assert written == review_body


def test_rev_written_on_all_valid_decisions(tmp_path: Path) -> None:
    """last_reviewed_rev is captured for APPROVED / CHANGES_REQUESTED / RESCUE_REQUIRED / ASK_USER."""
    for decision in ("APPROVED", "CHANGES_REQUESTED", "RESCUE_REQUIRED", "ASK_USER"):
        state = _agent_pair_state(review_items=[_OPEN_ITEM])
        result = {"decision": decision, "raw": f"body\nREVIEW_DECISION: {decision}", "parse_status": "ok"}
        _run_with(tmp_path, state, result, head_rev=_HEAD_REV)
        assert state.get("last_reviewed_rev") == _HEAD_REV, f"last_reviewed_rev must be set for {decision}"


def test_rev_not_written_when_git_fails(tmp_path: Path) -> None:
    """If git_rev_parse raises RuntimeError, last_reviewed_rev is left untouched."""
    state = _agent_pair_state(last_reviewed_rev=_PRIOR_REV, review_items=[_OPEN_ITEM])
    with (
        patch("phase_runners.review_code.get_reviewer_adapter", return_value=MagicMock()),
        patch("phase_runners.review_code.review_with_fallback", return_value=_approved_result()),
        patch("phase_runners.review_code.compute_repo_diff", return_value=""),
        patch("phase_runners.review_code.repo_root", return_value=tmp_path),
        patch("phase_runners.review_code.git_rev_parse", side_effect=RuntimeError("git not a repo")),
        patch("phase_runners.review_code._is_ancestor", return_value=True),
        patch("phase_runners.review_code._incremental_diff_nonempty", return_value=True),
    ):
        res = review_code.run(tmp_path, state)

    # run() must not propagate the RuntimeError
    assert res["status"] == "approved"
    # last_reviewed_rev must remain at its prior value (untouched)
    assert state.get("last_reviewed_rev") == _PRIOR_REV
