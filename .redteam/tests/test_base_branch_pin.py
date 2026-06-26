"""#91 Part B — pin the reviewed-range base_branch pre-worker.

Every per-task consumer (review diff base, committed-range / changed-paths helpers,
PR base) reads the PINNED base instead of live config, so a worker that edits
`.redteam/config.toml [project].base_branch` mid-task cannot move the reviewed range or
make the PR base differ from it. A legacy in-flight task already past a writable phase
with no pin fails closed rather than backfilling from a possibly-moved config.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_WF = Path(__file__).resolve().parents[1] / "workflows"
if str(_WF) not in sys.path:
    sys.path.insert(0, str(_WF))


def _orch():
    import _engine

    return _engine.orchestrator()


def _base_mod():
    import _engine

    return _engine.base()


# ---------- resolver + signal helpers (unit) ----------


def test_pinned_base_branch_returns_the_pin():
    base = _base_mod()
    assert base.pinned_base_branch({"base_branch": "develop"}) == "develop"


def test_pinned_base_branch_fails_closed_when_absent_or_invalid():
    base = _base_mod()
    for bad in ({}, {"base_branch": ""}, {"base_branch": None}, {"base_branch": 123}):
        with pytest.raises(ValueError, match="base_branch is not pinned"):
            base.pinned_base_branch(bad)


def test_writable_phase_started_counts_any_signal(tmp_path):
    orch = _orch()
    assert orch._writable_phase_started({"phases_completed": ["plan_outcome"]}, tmp_path) is False
    assert orch._writable_phase_started({"phases_completed": ["plan_outcome", "implement"]}, tmp_path) is True
    assert orch._writable_phase_started({"phases_completed": ["write_test"]}, tmp_path) is True
    assert orch._writable_phase_started({"verification": {"last_run_at": "2026-06-26T00:00:00Z"}}, tmp_path) is True
    assert orch._writable_phase_started({"phase": "implement"}, tmp_path) is True
    assert orch._writable_phase_started({"next_phase": "review_code"}, tmp_path) is True
    assert orch._writable_phase_started({"next_phase": "plan_outcome"}, tmp_path) is False
    (tmp_path / "impl_diff.patch").write_text("x", encoding="utf-8")
    assert orch._writable_phase_started({}, tmp_path) is True


# ---------- orchestrator pin behavior (integration) ----------


def _setup(orch, monkeypatch, tmp_path, state):
    task_dir = tmp_path / "batch" / "tasks" / "task-001"
    task_dir.mkdir(parents=True)
    (task_dir / "state.json").write_text(json.dumps(state), encoding="utf-8")
    monkeypatch.setattr(
        orch, "_ensure_task_branch", lambda task_id, repo, branch_prefix, base_branch: f"proj/{task_id}"
    )
    monkeypatch.setattr(orch, "repo_root", lambda: tmp_path)
    return task_dir


def test_fresh_task_pins_base_branch_pre_worker(monkeypatch, tmp_path):
    """A fresh task (no writable phase, no pin) pins base_branch from config BEFORE any
    phase runs — so every consumer reads the pinned value, not live config."""
    orch = _orch()
    state = {"task_id": "task-001", "mode": "agent-pair", "phases_completed": [], "next_phase": "plan_outcome"}
    task_dir = _setup(orch, monkeypatch, tmp_path, state)
    # Block on the first phase so the run stops right after the pin (no real worker).
    monkeypatch.setitem(
        orch.PHASE_RUNNERS,
        "plan_outcome",
        lambda task_dir, state: {"status": "ask_user", "feedback": "stop here", "log": "", "diff": ""},
    )
    orch.process_task(task_dir)
    saved = json.loads((task_dir / "state.json").read_text(encoding="utf-8"))
    assert saved["base_branch"] == "main"  # config default, pinned pre-worker


def test_legacy_unpinned_past_writable_phase_fails_closed(monkeypatch, tmp_path):
    """A legacy in-flight task already past implement with no pin must FAIL CLOSED — never
    backfill base_branch from a config the worker could already have moved (#91 plan_review
    IR-001). The pin block runs before any phase, so no reviewer/PR work happens."""
    orch = _orch()
    state = {
        "task_id": "task-001",
        "mode": "agent-pair",
        "phases_completed": ["plan_outcome", "plan_review", "implement"],
        "next_phase": "review_code",
    }
    task_dir = _setup(orch, monkeypatch, tmp_path, state)
    outcome = orch.process_task(task_dir)
    saved = json.loads((task_dir / "state.json").read_text(encoding="utf-8"))
    assert outcome in ("error", "deferred")
    assert saved["last_failure_reason"] == "unpinned_base_branch"
    assert "base_branch" not in saved  # NOT backfilled from live config


def test_legacy_unpinned_next_phase_fails_closed_regression(monkeypatch, tmp_path):
    """An in-flight legacy task with next_phase=review_code but empty phases_completed
    must FAIL CLOSED (unpinned_base_branch) rather than backfilling base_branch."""
    orch = _orch()
    state = {
        "task_id": "task-001",
        "mode": "agent-pair",
        "phases_completed": [],
        "next_phase": "review_code",
    }
    task_dir = _setup(orch, monkeypatch, tmp_path, state)
    outcome = orch.process_task(task_dir)
    saved = json.loads((task_dir / "state.json").read_text(encoding="utf-8"))
    assert outcome in ("error", "deferred")
    assert saved["last_failure_reason"] == "unpinned_base_branch"
    assert "base_branch" not in saved


def test_pinned_base_drives_consumer_not_live_config():
    """A consumer (the headless code-review prompt) reviews the PINNED base, even when it
    differs from whatever live config now says — the core #91 property."""
    from phase_runners import review_code  # noqa: E402

    prompt = review_code._code_review_prompt(Path("/t"), "release-1.2")
    assert "release-1.2...HEAD" in prompt
