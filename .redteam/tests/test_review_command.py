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

import sys
from pathlib import Path
from unittest.mock import MagicMock

_WF = Path(__file__).resolve().parents[1] / "workflows"
if str(_WF) not in sys.path:
    sys.path.insert(0, str(_WF))


def _load_orchestrator_module():
    import _engine

    return _engine.orchestrator()


def _fake_adapter(decision: str, parse_status: str = "ok", raw: str | None = None) -> MagicMock:
    fake = MagicMock()
    fake.review.return_value = {
        "decision": decision,
        "raw": raw if raw is not None else f"IR-001 ...\nREVIEW_DECISION: {decision}",
        "parse_status": parse_status,
    }
    return fake


def _result(decision: str, parse_status: str = "ok", raw: str | None = None) -> dict:
    return {
        "decision": decision,
        "raw": raw if raw is not None else f"IR-001 ...\nREVIEW_DECISION: {decision}",
        "parse_status": parse_status,
    }


def test_review_approved_returns_zero_and_saves(monkeypatch, tmp_path, capsys) -> None:
    orch = _load_orchestrator_module()
    (tmp_path / ".redteam").mkdir()
    # get_reviewer_adapter is still used for the None check + self-review guard;
    # the actual review now flows through the fallback ladder (review_with_fallback).
    monkeypatch.setattr(orch, "get_reviewer_adapter", lambda state: _fake_adapter("APPROVED"))
    rwf = MagicMock(return_value=_result("APPROVED"))
    monkeypatch.setattr(orch, "review_with_fallback", rwf)

    rc = orch.cmd_review(repo=tmp_path)

    assert rc == 0
    out = capsys.readouterr().out
    assert "REVIEW_DECISION: APPROVED" in out  # full review printed
    saved = (tmp_path / ".redteam" / "last_review.md").read_text(encoding="utf-8")
    assert "REVIEW_DECISION: APPROVED" in saved  # persisted for reference
    # The ladder was asked for a read-only branch_diff review.
    _, kwargs = rwf.call_args
    assert kwargs["target"] == {"kind": "branch_diff", "base": "main"}
    assert kwargs["role"] == "review_code"


def test_review_changes_requested_returns_one(monkeypatch, tmp_path) -> None:
    """Issues found is a SUCCESSFUL review run, but a non-zero exit so it can gate CI."""
    orch = _load_orchestrator_module()
    (tmp_path / ".redteam").mkdir()
    monkeypatch.setattr(orch, "get_reviewer_adapter", lambda state: _fake_adapter("CHANGES_REQUESTED"))
    monkeypatch.setattr(orch, "review_with_fallback", lambda *a, **k: _result("CHANGES_REQUESTED"))
    assert orch.cmd_review(repo=tmp_path) == 1


def test_review_failed_reviewer_returns_two(monkeypatch, tmp_path) -> None:
    """Fail-closed: when the reviewer fails infra and the fallback ladder exhausts
    to manual (parse_status manual_required), `review` exits 2 — never an approval
    even if a stray body says APPROVED."""
    orch = _load_orchestrator_module()
    (tmp_path / ".redteam").mkdir()
    monkeypatch.setattr(orch, "get_reviewer_adapter", lambda state: _fake_adapter("APPROVED"))
    monkeypatch.setattr(
        orch,
        "review_with_fallback",
        lambda *a, **k: _result("MISSING", parse_status=orch.MANUAL_REQUIRED, raw="codex timed out; manual required"),
    )
    assert orch.cmd_review(repo=tmp_path) == 2


def test_review_without_headless_reviewer_exits_with_guidance(monkeypatch, tmp_path, capsys) -> None:
    orch = _load_orchestrator_module()
    monkeypatch.setattr(orch, "get_reviewer_adapter", lambda state: None)
    rc = orch.cmd_review(repo=tmp_path)
    assert rc == 2
    err = capsys.readouterr().err
    assert "human" in err and "reviewer" in err  # actionable guidance, no crash


def test_review_refuses_same_provider_self_review(monkeypatch, tmp_path, capsys) -> None:
    """Fail-closed cross-provider guard AND a pin that cmd_review resolves providers
    through the shared worker_provider/reviewer_provider (not the adapter's own
    .name). The reviewer adapter is deliberately named "codex" while the resolvers
    report a "claude"/"claude" collapse: ONLY code that consults the resolvers
    refuses here. The pre-convergence implementation keyed off adapter.name /
    get_worker_adapter().name, so it would see "codex" vs "claude", NOT collapse,
    and run the reviewer — failing this test (so it pins the convergence)."""
    orch = _load_orchestrator_module()
    (tmp_path / ".redteam").mkdir()
    reviewer = MagicMock()
    reviewer.name = "codex"  # adapter name disagrees with the resolver verdict on purpose
    monkeypatch.setattr(orch, "get_reviewer_adapter", lambda state: reviewer)
    monkeypatch.setattr(orch, "worker_provider", lambda state: "claude")
    monkeypatch.setattr(orch, "reviewer_provider", lambda state: "claude")  # collapse per the resolvers

    rc = orch.cmd_review(repo=tmp_path)

    assert rc == 2
    err = capsys.readouterr().err
    assert "self-review" in err  # actionable, names the collapse
    reviewer.review.assert_not_called()  # refused via the resolver path, before running the reviewer


def test_review_fails_closed_on_bad_config(monkeypatch, tmp_path, capsys) -> None:
    """#40: a malformed/unreadable .redteam/config.toml must exit 2 with guidance
    (fail-closed), not raise a traceback (exit 1), and never resolve/run a reviewer."""
    orch = _load_orchestrator_module()
    called = {"reviewer": False}

    def _boom(rr):
        raise ValueError("unknown key 'verfy_command' in [project]")

    def _reviewer(state):
        called["reviewer"] = True
        return MagicMock()

    monkeypatch.setattr(orch, "load_config", _boom)
    monkeypatch.setattr(orch, "get_reviewer_adapter", _reviewer)

    rc = orch.cmd_review(repo=tmp_path)

    assert rc == 2
    assert "config" in capsys.readouterr().err.lower()
    assert called["reviewer"] is False  # bailed before resolving the reviewer


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
