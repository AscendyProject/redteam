"""Pipeline mode selection + enum validation (issue #36).

`mode` decides which review gates run (agent-pair vs tdd). Before #36 it was
hidden in state.json and any non-"agent-pair" value silently fell through to the
TDD order, so a typo ran the wrong pipeline. These tests pin: an unknown mode
fails closed; a fresh task can select the mode via input.md front-matter; and a
front-matter mode conflicts loudly with tier routing (which governs the order).
"""

from __future__ import annotations

import json
from pathlib import Path


def _load_orchestrator():
    import _engine

    return _engine.orchestrator()


_CFG_NO_TIERS = """
[project]
name = "p"
source_dirs = ["app/"]
test_file_glob = "test_*.py"
verification_allowlist = ["pytest"]

[models]
implementer = "claude-sonnet-4-6"
reviewer = "codex"
"""

_CFG_WITH_TIERS = (
    _CFG_NO_TIERS
    + """
[tiers.0]
review = false

[tier_triggers]
default = 0
"""
)


def _setup(tmp_path: Path, cfg_body: str, *, mode: str = "agent-pair", input_md: str = "", phases_completed=None):
    (tmp_path / ".redteam").mkdir(exist_ok=True)
    (tmp_path / ".redteam" / "config.toml").write_text(cfg_body, encoding="utf-8")
    task_dir = tmp_path / "batch" / "tasks" / "task-001"
    task_dir.mkdir(parents=True)
    (task_dir / "input.md").write_text(input_md, encoding="utf-8")
    state = {
        "task_id": "task-001",
        "mode": mode,
        "phase": "created",
        "phases_completed": phases_completed or [],
        "next_phase": "plan_outcome",
        "models": {"implementer": "claude-sonnet-4-6", "reviewer": "codex"},
        "retries": {},
        "max_retries_per_phase": 2,
        "verification": {},
        "escape": {"ask_user": False, "reason": None, "return_phase": None},
    }
    (task_dir / "state.json").write_text(json.dumps(state), encoding="utf-8")
    return task_dir


def _mocks(orch, monkeypatch, tmp_path):
    monkeypatch.setattr(orch, "repo_root", lambda: tmp_path)
    monkeypatch.setattr(orch, "_ensure_task_branch", lambda task_id, repo, bp, bb: f"{bp}/{task_id}")


def _read_state(task_dir: Path) -> dict:
    return json.loads((task_dir / "state.json").read_text(encoding="utf-8"))


def test_parse_frontmatter_returns_mode(tmp_path) -> None:
    """The front-matter parser surfaces `mode` (raw) alongside tier/paths."""
    orch = _load_orchestrator()
    task_dir = tmp_path / "t"
    task_dir.mkdir()
    (task_dir / "input.md").write_text('+++\nmode = "tdd"\n+++\nbody\n', encoding="utf-8")
    tier, paths, mode = orch._parse_input_frontmatter(task_dir)
    assert mode == "tdd"


def test_unknown_mode_in_state_fails_closed(monkeypatch, tmp_path) -> None:
    """A typo'd mode in state.json defers (invalid_mode) instead of silently
    running TDD — the core #36 footgun."""
    orch = _load_orchestrator()
    task_dir = _setup(tmp_path, _CFG_NO_TIERS, mode="agentpair")  # typo
    _mocks(orch, monkeypatch, tmp_path)

    ran = {"any": False}

    def spy(td, st):
        ran["any"] = True
        return {"status": "ask_user", "feedback": "", "log": "", "diff": ""}

    monkeypatch.setitem(orch.PHASE_RUNNERS, "plan_outcome", spy)

    outcome = orch.process_task(task_dir)

    assert outcome == "error"
    assert ran["any"] is False  # guard ran before any phase
    st = _read_state(task_dir)
    assert st["last_failure_reason"] == "invalid_mode"
    assert st["next_phase"] == "deferred"


def test_frontmatter_selects_tdd_on_fresh_task(monkeypatch, tmp_path) -> None:
    """A fresh task with `mode = "tdd"` in front-matter runs the TDD pipeline."""
    orch = _load_orchestrator()
    task_dir = _setup(tmp_path, _CFG_NO_TIERS, mode="agent-pair", input_md='+++\nmode = "tdd"\n+++\n')
    _mocks(orch, monkeypatch, tmp_path)

    monkeypatch.setitem(
        orch.PHASE_RUNNERS, "plan_outcome", lambda td, st: {"status": "ask_user", "feedback": "", "log": "", "diff": ""}
    )

    orch.process_task(task_dir)

    st = _read_state(task_dir)
    assert st["mode"] == "tdd"
    assert orch._phase_order(st) == orch.TDD_PHASE_ORDER


def test_frontmatter_invalid_mode_fails_closed(monkeypatch, tmp_path) -> None:
    """A bogus front-matter mode defers rather than falling through to TDD."""
    orch = _load_orchestrator()
    task_dir = _setup(tmp_path, _CFG_NO_TIERS, input_md='+++\nmode = "bogus"\n+++\n')
    _mocks(orch, monkeypatch, tmp_path)

    outcome = orch.process_task(task_dir)

    assert outcome == "error"
    st = _read_state(task_dir)
    assert st["last_failure_reason"] == "invalid_mode"


def test_frontmatter_mode_conflicts_with_tier_routing(monkeypatch, tmp_path) -> None:
    """When tier routing is active the tier profile governs the order, so a
    front-matter `mode` would be ignored — reject it loudly (issue #36 reconcile)."""
    orch = _load_orchestrator()
    task_dir = _setup(tmp_path, _CFG_WITH_TIERS, input_md='+++\nmode = "tdd"\n+++\n')
    _mocks(orch, monkeypatch, tmp_path)

    outcome = orch.process_task(task_dir)

    assert outcome == "error"
    st = _read_state(task_dir)
    assert st["last_failure_reason"] == "mode_tier_conflict"
    assert st["next_phase"] == "deferred"


def test_mode_conflict_fires_when_tier_already_persisted(monkeypatch, tmp_path) -> None:
    """PR-002: a prior run can persist tier/tier_phases yet stop before completing
    a phase (phases_completed still empty). On the next start the tier block is
    skipped (`tier` already in state), but tier_phases still governs the order — so
    a front-matter mode must STILL be rejected, not silently applied-then-ignored."""
    orch = _load_orchestrator()
    task_dir = _setup(tmp_path, _CFG_WITH_TIERS, input_md='+++\nmode = "tdd"\n+++\n')
    # simulate a half-run: tier resolved + persisted, but no phase completed yet
    st = _read_state(task_dir)
    st["tier"] = 0
    st["tier_phases"] = ["plan_outcome", "implement", "create_pr", "done"]
    (task_dir / "state.json").write_text(json.dumps(st), encoding="utf-8")
    _mocks(orch, monkeypatch, tmp_path)

    outcome = orch.process_task(task_dir)

    assert outcome == "error"
    assert _read_state(task_dir)["last_failure_reason"] == "mode_tier_conflict"


def test_frontmatter_mode_ignored_on_resume(monkeypatch, tmp_path) -> None:
    """Mode is only selectable at a fresh task's start; an in-flight task keeps its
    persisted mode even if input.md is edited mid-run."""
    orch = _load_orchestrator()
    task_dir = _setup(
        tmp_path,
        _CFG_NO_TIERS,
        mode="agent-pair",
        input_md='+++\nmode = "tdd"\n+++\n',
        phases_completed=["plan_outcome"],
    )
    _mocks(orch, monkeypatch, tmp_path)
    # halt immediately at whatever next phase runs (stub every runner this could hit
    # so no real subprocess is ever spawned)
    for ph in ("plan_outcome", "implement", "plan_review", "review_code", "write_test"):
        monkeypatch.setitem(
            orch.PHASE_RUNNERS, ph, lambda td, st: {"status": "ask_user", "feedback": "", "log": "", "diff": ""}
        )

    orch.process_task(task_dir)

    st = _read_state(task_dir)
    assert st["mode"] == "agent-pair"  # not overridden by the edited front-matter
