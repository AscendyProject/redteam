"""Reviewer fallback ladder — engine policy (#37 step 4).

When the primary reviewer fails on INFRA (missing CLI / auth / timeout /
unparseable), the engine applies the reviewer_fallback ladder, FAIL-CLOSED:
- a valid parsed decision (incl. CHANGES_REQUESTED) is never a fallback trigger;
- fallback "manual" → a manual_required result that blocks for a pasted review;
- a provider fallback's APPROVED is trusted ONLY if it is cross-provider from the
  worker, read_only_enforced, and its own parse is a valid decision;
- manual_required never becomes an approval, never seeds review_items, never
  counts a retry.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

_WF = Path(__file__).resolve().parents[1] / "workflows"
if str(_WF) not in sys.path:
    sys.path.insert(0, str(_WF))

import adapters  # noqa: E402
from config import load_config  # noqa: E402


def _load_orchestrator():
    import _engine

    return _engine.orchestrator()


_TARGET = {"kind": "branch_diff", "base": "main"}


def _fake(decision: str, parse_status: str = "ok", raw: str = "body", read_only: bool = True, name: str = "codex"):
    a = MagicMock()
    a.name = name
    a.capabilities = {"read_only_enforced": read_only, "native_diff_review": False, "timeout_sec": 1}
    a.review.return_value = {"decision": decision, "raw": raw, "parse_status": parse_status}
    return a


def _state(*, implementer="claude-sonnet-4-6", reviewer="codex", fallback="manual"):
    return {"models": {"implementer": implementer, "reviewer": reviewer, "reviewer_fallback": fallback}}


def _run(state):
    return adapters.review_with_fallback(state, role="review_code", prompt="x", cwd=Path("."), target=_TARGET)


# ---- the ladder ----


def test_valid_primary_result_returned_unchanged(monkeypatch):
    primary = _fake("APPROVED")
    monkeypatch.setattr(adapters, "get_reviewer_adapter", lambda s: primary)
    r = _run(_state())
    assert r["parse_status"] == "ok" and r["decision"] == "APPROVED"


def test_changes_requested_never_falls_back(monkeypatch):
    """CHANGES_REQUESTED is a valid review result, not an infra failure — it must
    be returned as-is and the fallback must never run."""
    primary = _fake("CHANGES_REQUESTED")
    fb = _fake("APPROVED")
    monkeypatch.setattr(adapters, "get_reviewer_adapter", lambda s: primary)
    monkeypatch.setattr(adapters, "_REVIEWER_ADAPTERS", {"codex": lambda: fb, "claude": lambda: fb})
    r = _run(_state(fallback="codex"))
    assert r["decision"] == "CHANGES_REQUESTED" and r["parse_status"] == "ok"
    fb.review.assert_not_called()


def test_infra_failure_manual_fallback_blocks(monkeypatch):
    primary = _fake("MISSING", parse_status="error", raw="codex timed out")
    monkeypatch.setattr(adapters, "get_reviewer_adapter", lambda s: primary)
    r = _run(_state(fallback="manual"))
    assert r["parse_status"] == adapters.MANUAL_REQUIRED


def test_cross_provider_fallback_approval_is_trusted(monkeypatch):
    """worker=claude, primary reviewer fails, fallback=codex (cross-provider,
    read-only) returns APPROVED → trusted, with the audit recorded in raw."""
    primary = _fake("MISSING", parse_status="error", raw="primary down")
    fb = _fake("APPROVED", raw="IR-000\nREVIEW_DECISION: APPROVED")
    monkeypatch.setattr(adapters, "get_reviewer_adapter", lambda s: primary)
    monkeypatch.setattr(adapters, "_REVIEWER_ADAPTERS", {"codex": lambda: fb, "claude": lambda: fb})
    r = _run(_state(implementer="claude-sonnet-4-6", fallback="codex"))
    assert r["parse_status"] == "ok" and r["decision"] == "APPROVED"
    assert "fallback" in r["raw"].lower()
    fb.review.assert_called_once()


def test_same_provider_fallback_cannot_auto_approve(monkeypatch):
    """A fallback that resolves to the worker's own provider is self-review — its
    APPROVED must NOT stand; degrade to manual_required."""
    primary = _fake("MISSING", parse_status="error")
    fb = _fake("APPROVED")
    monkeypatch.setattr(adapters, "get_reviewer_adapter", lambda s: primary)
    monkeypatch.setattr(adapters, "_REVIEWER_ADAPTERS", {"codex": lambda: fb, "claude": lambda: fb})
    r = _run(_state(implementer="claude-sonnet-4-6", fallback="claude"))  # worker=claude, fallback=claude
    assert r["parse_status"] == adapters.MANUAL_REQUIRED
    fb.review.assert_not_called()


def test_non_read_only_fallback_cannot_auto_approve(monkeypatch):
    primary = _fake("MISSING", parse_status="error")
    fb = _fake("APPROVED", read_only=False)
    monkeypatch.setattr(adapters, "get_reviewer_adapter", lambda s: primary)
    monkeypatch.setattr(adapters, "_REVIEWER_ADAPTERS", {"codex": lambda: fb, "claude": lambda: fb})
    r = _run(_state(implementer="claude-sonnet-4-6", fallback="codex"))
    assert r["parse_status"] == adapters.MANUAL_REQUIRED
    fb.review.assert_not_called()


def test_both_reviewers_fail_blocks(monkeypatch):
    primary = _fake("MISSING", parse_status="error")
    fb = _fake("MISSING", parse_status="error")
    monkeypatch.setattr(adapters, "get_reviewer_adapter", lambda s: primary)
    monkeypatch.setattr(adapters, "_REVIEWER_ADAPTERS", {"codex": lambda: fb, "claude": lambda: fb})
    r = _run(_state(implementer="claude-sonnet-4-6", fallback="codex"))
    assert r["parse_status"] == adapters.MANUAL_REQUIRED


def test_missing_decision_with_ok_status_is_not_trusted(monkeypatch):
    """Defensive: a MISSING decision mis-paired with parse_status ok is treated as
    an infra failure, never returned as a decision."""
    primary = _fake("MISSING", parse_status="ok")
    monkeypatch.setattr(adapters, "get_reviewer_adapter", lambda s: primary)
    r = _run(_state(fallback="manual"))
    assert r["parse_status"] == adapters.MANUAL_REQUIRED


# ---- config validation ----


def test_config_rejects_unknown_reviewer_fallback(tmp_path):
    (tmp_path / ".redteam").mkdir()
    (tmp_path / ".redteam" / "config.toml").write_text(
        '[project]\nname = "p"\nsource_dirs = ["a/"]\ntest_file_glob = "test_*.py"\n'
        'verification_allowlist = ["pytest"]\n[models]\nreviewer_fallback = "codx"\n',
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="reviewer_fallback"):
        load_config(tmp_path)


def test_config_rejects_unknown_reviewer_fallback_in_tier_override(tmp_path):
    """A tier model override must validate reviewer_fallback with the same loud
    discipline as the top-level block — a typo can't slip through #37 / PR-001."""
    (tmp_path / ".redteam").mkdir()
    (tmp_path / ".redteam" / "config.toml").write_text(
        '[project]\nname = "p"\nsource_dirs = ["a/"]\ntest_file_glob = "test_*.py"\n'
        'verification_allowlist = ["pytest"]\n[tiers.2]\nreview = true\n'
        '[tiers.2.models]\nreviewer_fallback = "codx"\n',
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="reviewer_fallback"):
        load_config(tmp_path)


# ---- orchestrator manual_required handling ----


def _task(tmp_path: Path) -> Path:
    (tmp_path / ".redteam").mkdir()
    (tmp_path / ".redteam" / "config.toml").write_text(
        '[project]\nname = "p"\nsource_dirs = ["a/"]\ntest_file_glob = "test_*.py"\n'
        'verification_allowlist = ["pytest"]\n[models]\nimplementer = "claude-sonnet-4-6"\nreviewer = "codex"\n',
        encoding="utf-8",
    )
    task_dir = tmp_path / "batch" / "tasks" / "task-001"
    task_dir.mkdir(parents=True)
    (task_dir / "input.md").write_text("do it", encoding="utf-8")
    state = {
        "task_id": "task-001",
        "mode": "agent-pair",
        "phase": "created",
        "phases_completed": ["plan_outcome"],
        "next_phase": "plan_review",
        "models": {"implementer": "claude-sonnet-4-6", "reviewer": "codex"},
        "retries": {},
        "max_retries_per_phase": 2,
        "verification": {"verify_command": "x", "commands": ["x"]},
        "escape": {"ask_user": False, "reason": None, "return_phase": None},
    }
    (task_dir / "state.json").write_text(json.dumps(state), encoding="utf-8")
    return task_dir


def test_manual_required_blocks_without_seeding_review_items(monkeypatch, tmp_path):
    """A manual_required result blocks at the review phase, records audit, sets the
    flag, counts NO retry, and — critically — does NOT seed review_items from the
    failed/manual audit body even though it contains PR-NNN-looking lines (#37)."""
    orch = _load_orchestrator()
    task_dir = _task(tmp_path)
    monkeypatch.setattr(orch, "repo_root", lambda: tmp_path)
    monkeypatch.setattr(orch, "_ensure_task_branch", lambda *a, **k: "redteam/task-001")
    audit = "PR-001 severity:high status:open primary reviewer failed"
    monkeypatch.setitem(
        orch.PHASE_RUNNERS,
        "plan_review",
        lambda td, st: {"status": "manual_required", "feedback": audit, "log": audit, "diff": ""},
    )

    outcome = orch.process_task(task_dir)

    assert outcome == "blocked_on_human_gate"
    st = json.loads((task_dir / "state.json").read_text(encoding="utf-8"))
    assert "plan_review" in st.get("manual_review_required", {})
    assert st.get("review_items", []) == []  # NOT seeded from the failed body
    assert st.get("review_audit")  # audit recorded
    assert st.get("retries", {}).get("plan_review", 0) == 0  # no retry counted


def test_pasted_manual_review_clears_flag_and_syncs_items(monkeypatch, tmp_path):
    """Recovery: once the operator pastes a real review for a flagged phase and
    touches the sentinel, the runner's manual branch yields a decision — the flag
    clears and review_items sync normally from the valid body (#37 / codex req)."""
    orch = _load_orchestrator()
    task_dir = _task(tmp_path)
    # pre-flag the phase + provide the pasted review + sentinel (operator action)
    st = json.loads((task_dir / "state.json").read_text(encoding="utf-8"))
    st["manual_review_required"] = {"plan_review": "reviewer fallback exhausted to manual"}
    (task_dir / "state.json").write_text(json.dumps(st), encoding="utf-8")
    (task_dir / "plan_review.md").write_text(
        "PR-007 severity:medium status:open something\nREVIEW_DECISION: CHANGES_REQUESTED", encoding="utf-8"
    )
    (task_dir / "plan_review.done").write_text("", encoding="utf-8")
    monkeypatch.setattr(orch, "repo_root", lambda: tmp_path)
    monkeypatch.setattr(orch, "_ensure_task_branch", lambda *a, **k: "redteam/task-001")
    # CHANGES_REQUESTED backtracks to plan_outcome; halt there so no real
    # subprocess runs (the flag-clear + item-sync already happened by then).
    monkeypatch.setitem(
        orch.PHASE_RUNNERS, "plan_outcome", lambda td, st: {"status": "ask_user", "feedback": "", "log": "", "diff": ""}
    )

    orch.process_task(task_dir)

    st = json.loads((task_dir / "state.json").read_text(encoding="utf-8"))
    assert "plan_review" not in st.get("manual_review_required", {})  # flag cleared
    ids = [it.get("id") for it in st.get("review_items", [])]
    assert "PR-007" in ids  # synced from the genuine pasted review


def test_successful_fallback_records_state_audit(monkeypatch, tmp_path):
    """An AUTOMATIC fallback approval/decision records the audit trail in
    state["review_audit"] too (not just the manual-required block), so the
    machine-readable trail covers the success path (#37 / code-review follow-up)."""
    orch = _load_orchestrator()
    task_dir = _task(tmp_path)
    monkeypatch.setattr(orch, "repo_root", lambda: tmp_path)
    monkeypatch.setattr(orch, "_ensure_task_branch", lambda *a, **k: "redteam/task-001")
    body = "PR-009 severity:low status:open nit\nREVIEW_DECISION: CHANGES_REQUESTED"
    monkeypatch.setitem(
        orch.PHASE_RUNNERS,
        "plan_review",
        # the runner carries STRUCTURED fallback provenance, not an in-band marker
        lambda td, st: {
            "status": "changes_requested",
            "feedback": body,
            "log": body,
            "diff": "",
            "fallback_audit": "primary reviewer 'codex' failed. Fell back to 'claude'.",
        },
    )
    # CHANGES_REQUESTED backtracks to plan_outcome; halt there (no real subprocess).
    monkeypatch.setitem(
        orch.PHASE_RUNNERS, "plan_outcome", lambda td, st: {"status": "ask_user", "feedback": "", "log": "", "diff": ""}
    )

    orch.process_task(task_dir)

    st = json.loads((task_dir / "state.json").read_text(encoding="utf-8"))
    audit = st.get("review_audit", [])
    assert any(a.get("phase") == "plan_review" and "Fell back" in a.get("reason", "") for a in audit)


def test_fallback_ask_user_records_state_audit(monkeypatch, tmp_path):
    """ASK_USER is a valid automatic-fallback outcome too — its structured audit
    must be recorded in state, same as approved/changes_requested (#37 PR-003)."""
    orch = _load_orchestrator()
    task_dir = _task(tmp_path)
    monkeypatch.setattr(orch, "repo_root", lambda: tmp_path)
    monkeypatch.setattr(orch, "_ensure_task_branch", lambda *a, **k: "redteam/task-001")
    monkeypatch.setitem(
        orch.PHASE_RUNNERS,
        "plan_review",
        lambda td, st: {
            "status": "ask_user",
            "feedback": "needs a human call",
            "log": "needs a human call",
            "diff": "",
            "fallback_audit": "primary reviewer 'codex' failed. Fell back to 'claude'.",
        },
    )

    orch.process_task(task_dir)

    st = json.loads((task_dir / "state.json").read_text(encoding="utf-8"))
    assert any(
        a.get("phase") == "plan_review" and "Fell back" in a.get("reason", "") for a in st.get("review_audit", [])
    )


def test_genuine_review_text_cannot_spoof_fallback_audit(monkeypatch, tmp_path):
    """A real (non-fallback) review whose BODY happens to start with the fallback
    marker text must NOT create a state audit entry — provenance is the structured
    fallback_audit field, never in-band review text (#37 review PR-002)."""
    orch = _load_orchestrator()
    task_dir = _task(tmp_path)
    monkeypatch.setattr(orch, "repo_root", lambda: tmp_path)
    monkeypatch.setattr(orch, "_ensure_task_branch", lambda *a, **k: "redteam/task-001")
    spoof = "[redteam fallback] not really\nREVIEW_DECISION: APPROVED"  # no fallback_audit field
    monkeypatch.setitem(
        orch.PHASE_RUNNERS,
        "plan_review",
        lambda td, st: {"status": "approved", "feedback": "", "log": spoof, "diff": ""},
    )
    # approved plan_review snapshots verification (no outcome.md here → backtracks to
    # plan_outcome) or advances; stub both downstream phases so no real subprocess runs.
    for ph in ("plan_outcome", "implement"):
        monkeypatch.setitem(
            orch.PHASE_RUNNERS, ph, lambda td, st: {"status": "ask_user", "feedback": "", "log": "", "diff": ""}
        )

    orch.process_task(task_dir)

    st = json.loads((task_dir / "state.json").read_text(encoding="utf-8"))
    assert st.get("review_audit", []) == []  # spoofed marker did not create an audit entry
