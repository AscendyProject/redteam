"""Standalone `orchestrator.py review` — one-shot cross-model review of the
current branch diff, no task/state machine.

Pins the command's contract:
- it resolves the configured reviewer adapter and runs it read-only on the diff;
- it prints the full review and persists it to .redteam/last_review.md;
- the exit code encodes the decision (0 APPROVED / 1 issues / 2 reviewer failed),
  fail-closed (a failed reviewer is never reported as an approval);
- a non-headless reviewer (reviewer="human") exits with guidance, not a crash.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from unittest.mock import MagicMock

_WF = Path(__file__).resolve().parents[1] / "workflows"
if str(_WF) not in sys.path:
    sys.path.insert(0, str(_WF))


def _load_orchestrator_module():
    module_path = _WF / "orchestrator.py"
    spec = importlib.util.spec_from_file_location("redteam_orchestrator", module_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _fake_adapter(decision: str, parse_status: str = "ok", raw: str | None = None) -> MagicMock:
    fake = MagicMock()
    # Name the reviewer "codex" so the cross-provider guard sees a real string
    # (the shipped default: codex reviewer / claude worker = cross-provider).
    fake.name = "codex"
    fake.review.return_value = {
        "decision": decision,
        "raw": raw if raw is not None else f"IR-001 ...\nREVIEW_DECISION: {decision}",
        "parse_status": parse_status,
    }
    return fake


def test_review_approved_returns_zero_and_saves(monkeypatch, tmp_path, capsys) -> None:
    orch = _load_orchestrator_module()
    (tmp_path / ".redteam").mkdir()
    fake = _fake_adapter("APPROVED")
    monkeypatch.setattr(orch, "get_reviewer_adapter", lambda state: fake)

    rc = orch.cmd_review(repo=tmp_path)

    assert rc == 0
    out = capsys.readouterr().out
    assert "REVIEW_DECISION: APPROVED" in out  # full review printed
    saved = (tmp_path / ".redteam" / "last_review.md").read_text(encoding="utf-8")
    assert "REVIEW_DECISION: APPROVED" in saved  # persisted for reference
    # The adapter was asked for a read-only branch_diff review.
    _, kwargs = fake.review.call_args
    assert kwargs["target"] == {"kind": "branch_diff", "base": "main"}
    assert kwargs["role"] == "review_code"


def test_review_changes_requested_returns_one(monkeypatch, tmp_path) -> None:
    """Issues found is a SUCCESSFUL review run, but a non-zero exit so it can gate CI."""
    orch = _load_orchestrator_module()
    (tmp_path / ".redteam").mkdir()
    monkeypatch.setattr(orch, "get_reviewer_adapter", lambda state: _fake_adapter("CHANGES_REQUESTED"))
    assert orch.cmd_review(repo=tmp_path) == 1


def test_review_failed_reviewer_returns_two(monkeypatch, tmp_path) -> None:
    """Fail-closed: a reviewer that errored/timed out (parse_status != ok) must
    exit 2, never be read as an approval even if the raw body says APPROVED."""
    orch = _load_orchestrator_module()
    (tmp_path / ".redteam").mkdir()
    fake = _fake_adapter("MISSING", parse_status="error", raw="codex exec timed out\nREVIEW_DECISION: APPROVED")
    monkeypatch.setattr(orch, "get_reviewer_adapter", lambda state: fake)
    assert orch.cmd_review(repo=tmp_path) == 2


def test_review_without_headless_reviewer_exits_with_guidance(monkeypatch, tmp_path, capsys) -> None:
    orch = _load_orchestrator_module()
    monkeypatch.setattr(orch, "get_reviewer_adapter", lambda state: None)
    rc = orch.cmd_review(repo=tmp_path)
    assert rc == 2
    err = capsys.readouterr().err
    assert "human" in err and "reviewer" in err  # actionable guidance, no crash


def test_review_refuses_same_provider_self_review(monkeypatch, tmp_path, capsys) -> None:
    """Fail-closed cross-provider guard: when the configured reviewer collapses to
    the worker's own provider, `review` must refuse (exit 2) WITHOUT running the
    reviewer — a standalone review can't become a hole that silently self-reviews.
    The worker adapter is named "claude-code" while the claude reviewer is named
    "claude"; the guard must still see these as the same provider family."""
    orch = _load_orchestrator_module()
    reviewer = MagicMock()
    reviewer.name = "claude"
    worker = MagicMock()
    worker.name = "claude-code"  # the Claude worker's name differs from the reviewer's
    monkeypatch.setattr(orch, "get_reviewer_adapter", lambda state: reviewer)
    monkeypatch.setattr(orch, "get_worker_adapter", lambda state: worker)

    rc = orch.cmd_review(repo=tmp_path)

    assert rc == 2
    err = capsys.readouterr().err
    assert "self-review" in err  # actionable, names the collapse
    reviewer.review.assert_not_called()  # refused before the reviewer ran


def test_review_dispatched_by_main_without_batch(monkeypatch) -> None:
    """`review` takes no batch dir — main must route it without requiring argv[2]."""
    orch = _load_orchestrator_module()
    called = {"review": False}

    def _fake_review():
        called["review"] = True
        return 0

    monkeypatch.setattr(orch, "cmd_review", _fake_review)
    assert orch.main(["orchestrator.py", "review"]) == 0
    assert called["review"] is True
