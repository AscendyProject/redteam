"""P5 hard ceilings on the review_code loop — D1-D6 behaviors.

Tests cover:
- Config parsing (happy path and fail-loud) — D1
- Tier-level ceilings are globally rejected — D1
- Default parity (no ceilings = today's byte-identical behavior) — D2/D3/D4
- State-template unchanged (no new counter fields) — Done-when
- Round ceiling enforcement (pre-dispatch, triggers on max+1) — D2/D4
- Wall-clock ceiling enforcement (pre-dispatch skip, post-dispatch upgrade) — D3/D4
- Approval-authority invariant (no ceiling_hit + approved simultaneously) — D4/D5
- Orchestrator routing on ceiling hit (defer, not rescue) — D6
- review_audit wiring on ceiling hit — D6
- Ceiling record shape in deferred_requirements — D6
- Accrual is monotonic and cumulative — D3
- Case A (manual) accrues no wall-clock but still increments round counter — D4
- Ceilings + P3 staging interoperate — D1
- Legacy state (missing counters) treated as zero — D2/D3
- Persistence across resumes — D2/D3
- Convergence does NOT reset ceiling counters — D4
- Adapter files unchanged (no cache-control code) — D7
- Decision doc exists — D7
- Dogfood-config assertion (this repo has review_ceilings=None) — Done-when
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

_WF = Path(__file__).resolve().parents[1] / "workflows"
if str(_WF) not in sys.path:
    sys.path.insert(0, str(_WF))

import adapters  # noqa: E402
import phase_runners.review_code as review_code  # noqa: E402
from config import ReviewCeilingsConfig, load_config  # noqa: E402

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_BASE_BRANCH = "main"
_HEAD_REV = "deadbeef12345678"
_TARGET = {"kind": "branch_diff", "base": _BASE_BRANCH}


def _write_config(tmp_path: Path, extra: str = "") -> None:
    """Write a minimal valid config.toml, optionally with extra TOML content."""
    (tmp_path / ".redteam").mkdir(exist_ok=True)
    base = (
        '[project]\nname = "p"\nsource_dirs = ["src/"]\ntest_file_glob = "test_*.py"\n'
        'verification_allowlist = ["pytest"]\n'
        '[models]\nimplementer = "claude-sonnet-4-6"\nreviewer = "codex"\n'
    )
    (tmp_path / ".redteam" / "config.toml").write_text(base + extra, encoding="utf-8")


def _ceilings_config(
    tmp_path: Path,
    max_review_rounds: int | None = None,
    max_wall_clock_sec: int | None = None,
) -> None:
    """Write a config with [models.review_ceilings] enabled."""
    parts = []
    if max_review_rounds is not None:
        parts.append(f"max_review_rounds = {max_review_rounds}")
    if max_wall_clock_sec is not None:
        parts.append(f"max_wall_clock_sec = {max_wall_clock_sec}")
    extra = "\n[models.review_ceilings]\n" + "\n".join(parts) + "\n"
    _write_config(tmp_path, extra)


def _agent_pair_state(**extra) -> dict:
    base: dict = {
        "mode": "agent-pair",
        "base_branch": _BASE_BRANCH,
        "models": {"implementer": "claude-sonnet-4-6", "reviewer": "codex", "reviewer_fallback": "manual"},
        "review_items": [],
    }
    base.update(extra)
    return base


def _ok(decision: str = "APPROVED", raw: str | None = None) -> dict:
    return {
        "decision": decision,
        "raw": raw or f"body\nREVIEW_DECISION: {decision}",
        "parse_status": "ok",
    }


def _manual_result() -> dict:
    return {"decision": "MISSING", "raw": "manual required", "parse_status": adapters.MANUAL_REQUIRED}


def _run_review(
    tmp_path: Path,
    state: dict,
    rwf_return: dict,
    *,
    monotonic_seq: list[float] | None = None,
) -> tuple:
    """Run review_code.run() with all I/O patched.

    Returns (result, rwf_mock).
    monotonic_seq: if provided, time.monotonic() returns values from this list
    in sequence (each call pops the next value).
    """
    mono_iter = iter(monotonic_seq) if monotonic_seq else None

    def _mono():
        if mono_iter is not None:
            return next(mono_iter)
        return 0.0

    with (
        patch("phase_runners.review_code.get_reviewer_adapter", return_value=MagicMock()),
        patch("phase_runners.review_code.review_with_fallback", return_value=rwf_return) as rwf,
        patch("phase_runners.review_code.review_with_fallback_for_provider", return_value=_ok("CHANGES_REQUESTED")),
        patch("phase_runners.review_code.compute_repo_diff", return_value=""),
        patch("phase_runners.review_code.repo_root", return_value=tmp_path),
        patch("phase_runners.review_code.git_rev_parse", return_value=_HEAD_REV),
        patch("phase_runners.review_code._is_ancestor", return_value=False),
        patch("phase_runners.review_code._incremental_diff_nonempty", return_value=False),
        patch("phase_runners.review_code.time") as mock_time,
    ):
        mock_time.monotonic = _mono
        res = review_code.run(tmp_path, state)
    return res, rwf


# ---------------------------------------------------------------------------
# Config parsing — happy path (D1)
# ---------------------------------------------------------------------------


def test_ceilings_max_rounds_only(tmp_path: Path) -> None:
    """max_review_rounds alone loads cleanly; max_wall_clock_sec is None."""
    _ceilings_config(tmp_path, max_review_rounds=3)
    cfg = load_config(tmp_path)
    rc = cfg.models.review_ceilings
    assert rc is not None
    assert isinstance(rc, ReviewCeilingsConfig)
    assert rc.max_review_rounds == 3
    assert rc.max_wall_clock_sec is None


def test_ceilings_max_wall_clock_only(tmp_path: Path) -> None:
    """max_wall_clock_sec alone loads cleanly; max_review_rounds is None."""
    _ceilings_config(tmp_path, max_wall_clock_sec=300)
    cfg = load_config(tmp_path)
    rc = cfg.models.review_ceilings
    assert rc is not None
    assert rc.max_review_rounds is None
    assert rc.max_wall_clock_sec == 300


def test_ceilings_both_set(tmp_path: Path) -> None:
    """Both keys set: both are reachable."""
    _ceilings_config(tmp_path, max_review_rounds=5, max_wall_clock_sec=600)
    cfg = load_config(tmp_path)
    rc = cfg.models.review_ceilings
    assert rc is not None
    assert rc.max_review_rounds == 5
    assert rc.max_wall_clock_sec == 600


# ---------------------------------------------------------------------------
# Config parsing — fail-loud (D1)
# ---------------------------------------------------------------------------


def test_ceilings_unknown_key_rejected(tmp_path: Path) -> None:
    """Unknown key inside [models.review_ceilings] raises ValueError."""
    _write_config(tmp_path, "\n[models.review_ceilings]\nmax_review_rounds = 3\nunknown_key = 1\n")
    with pytest.raises(ValueError, match="Unknown models.review_ceilings config key"):
        load_config(tmp_path)


def test_ceilings_empty_subtable_rejected(tmp_path: Path) -> None:
    """Subtable present but both keys absent → fail loud."""
    _write_config(tmp_path, "\n[models.review_ceilings]\n")
    with pytest.raises(ValueError, match="both keys are absent"):
        load_config(tmp_path)


def test_ceilings_max_rounds_bool_rejected(tmp_path: Path) -> None:
    _write_config(tmp_path, "\n[models.review_ceilings]\nmax_review_rounds = true\n")
    with pytest.raises(ValueError, match="bool values rejected"):
        load_config(tmp_path)


def test_ceilings_max_rounds_zero_rejected(tmp_path: Path) -> None:
    _write_config(tmp_path, "\n[models.review_ceilings]\nmax_review_rounds = 0\n")
    with pytest.raises(ValueError, match="must be an int >= 1"):
        load_config(tmp_path)


def test_ceilings_max_rounds_negative_rejected(tmp_path: Path) -> None:
    _write_config(tmp_path, "\n[models.review_ceilings]\nmax_review_rounds = -1\n")
    with pytest.raises(ValueError, match="must be an int >= 1"):
        load_config(tmp_path)


def test_ceilings_max_rounds_string_rejected(tmp_path: Path) -> None:
    """Wrong TOML type (string) for max_review_rounds raises ValueError."""
    _write_config(tmp_path, '\n[models.review_ceilings]\nmax_review_rounds = "three"\n')
    with pytest.raises(ValueError, match="must be an int >= 1"):
        load_config(tmp_path)


def test_ceilings_max_wall_clock_bool_rejected(tmp_path: Path) -> None:
    _write_config(tmp_path, "\n[models.review_ceilings]\nmax_wall_clock_sec = true\n")
    with pytest.raises(ValueError, match="bool values rejected"):
        load_config(tmp_path)


def test_ceilings_max_wall_clock_zero_rejected(tmp_path: Path) -> None:
    _write_config(tmp_path, "\n[models.review_ceilings]\nmax_wall_clock_sec = 0\n")
    with pytest.raises(ValueError, match="must be an int >= 1"):
        load_config(tmp_path)


def test_ceilings_max_wall_clock_negative_rejected(tmp_path: Path) -> None:
    _write_config(tmp_path, "\n[models.review_ceilings]\nmax_wall_clock_sec = -5\n")
    with pytest.raises(ValueError, match="must be an int >= 1"):
        load_config(tmp_path)


def test_ceilings_max_wall_clock_string_rejected(tmp_path: Path) -> None:
    """Wrong TOML type (string) for max_wall_clock_sec raises ValueError."""
    _write_config(tmp_path, '\n[models.review_ceilings]\nmax_wall_clock_sec = "300"\n')
    with pytest.raises(ValueError, match="must be an int >= 1"):
        load_config(tmp_path)


# ---------------------------------------------------------------------------
# Tier-level ceilings are REJECTED (global-only invariant — D1)
# ---------------------------------------------------------------------------


def test_tier_level_ceilings_dict_rejected(tmp_path: Path) -> None:
    """A [tiers.N].models table with a review_ceilings sub-table is rejected."""
    (tmp_path / ".redteam").mkdir(exist_ok=True)
    (tmp_path / ".redteam" / "config.toml").write_text(
        '[project]\nname = "p"\nsource_dirs = ["src/"]\ntest_file_glob = "test_*.py"\n'
        'verification_allowlist = ["pytest"]\n'
        "[tiers.1]\nreview = true\n"
        "[tiers.1.models.review_ceilings]\nmax_review_rounds = 3\n"
        "[tier_triggers]\ndefault = 1\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError):
        load_config(tmp_path)


def test_tier_level_ceilings_string_rejected(tmp_path: Path) -> None:
    """review_ceilings as a bare string role value is rejected as unknown role."""
    (tmp_path / ".redteam").mkdir(exist_ok=True)
    (tmp_path / ".redteam" / "config.toml").write_text(
        '[project]\nname = "p"\nsource_dirs = ["src/"]\ntest_file_glob = "test_*.py"\n'
        'verification_allowlist = ["pytest"]\n'
        "[tiers.1]\nreview = true\n"
        '[tiers.1.models]\nreview_ceilings = "codex"\n'
        "[tier_triggers]\ndefault = 1\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="unknown role"):
        load_config(tmp_path)


# ---------------------------------------------------------------------------
# Default parity — no ceilings (D4 Done-when)
# ---------------------------------------------------------------------------


def test_default_config_review_ceilings_is_none() -> None:
    """With no config.toml, review_ceilings is None (default parity)."""
    import tempfile

    with tempfile.TemporaryDirectory() as d:
        cfg = load_config(Path(d))
    assert cfg.models.review_ceilings is None


def test_no_ceilings_no_state_growth(tmp_path: Path) -> None:
    """With review_ceilings=None, run() neither reads nor writes the counter fields."""
    # No config.toml → review_ceilings=None
    state = _agent_pair_state(implement_round_count=1)
    state_before_keys = set(state.keys())
    _run_review(tmp_path, state, _ok("APPROVED"))
    # Counter fields must not appear in state
    assert "review_code_round_count" not in state
    assert "review_code_wall_clock_sec" not in state
    # Only the keys the runner already touched today should differ
    added_keys = set(state.keys()) - state_before_keys
    assert "review_code_round_count" not in added_keys
    assert "review_code_wall_clock_sec" not in added_keys


def test_no_ceilings_no_ceiling_hit_field(tmp_path: Path) -> None:
    """With review_ceilings=None, no PhaseResult ever carries ceiling_hit."""
    state = _agent_pair_state()
    res, _ = _run_review(tmp_path, state, _ok("APPROVED"))
    assert "ceiling_hit" not in res


def test_no_ceilings_time_monotonic_not_called(tmp_path: Path) -> None:
    """With review_ceilings=None, time.monotonic() is never called."""
    state = _agent_pair_state()
    calls: list = []
    with (
        patch("phase_runners.review_code.get_reviewer_adapter", return_value=MagicMock()),
        patch("phase_runners.review_code.review_with_fallback", return_value=_ok("APPROVED")),
        patch("phase_runners.review_code.review_with_fallback_for_provider", return_value=_ok("CHANGES_REQUESTED")),
        patch("phase_runners.review_code.compute_repo_diff", return_value=""),
        patch("phase_runners.review_code.repo_root", return_value=tmp_path),
        patch("phase_runners.review_code.git_rev_parse", return_value=_HEAD_REV),
        patch("phase_runners.review_code._is_ancestor", return_value=False),
        patch("phase_runners.review_code._incremental_diff_nonempty", return_value=False),
        patch("phase_runners.review_code.time") as mock_time,
    ):
        mock_time.monotonic.side_effect = lambda: calls.append(1) or 0.0
        review_code.run(tmp_path, state)
    assert calls == [], "time.monotonic() must not be called when review_ceilings is None"


# ---------------------------------------------------------------------------
# State template unchanged (Done-when)
# ---------------------------------------------------------------------------


def test_state_template_unchanged() -> None:
    """state.template.json does NOT contain the new counter keys."""
    tmpl = Path(__file__).resolve().parents[1] / "templates" / "state.template.json"
    data = json.loads(tmpl.read_text(encoding="utf-8"))
    assert "review_code_round_count" not in data
    assert "review_code_wall_clock_sec" not in data


# ---------------------------------------------------------------------------
# Round ceiling — counter increment and pre-dispatch check (D2, D4)
# ---------------------------------------------------------------------------


def test_round_counter_increments_each_invocation(tmp_path: Path) -> None:
    """With max_review_rounds configured, counter increments on each run."""
    _ceilings_config(tmp_path, max_review_rounds=5)
    state = _agent_pair_state()
    for i in range(1, 4):
        _run_review(tmp_path, state, _ok("CHANGES_REQUESTED"))
        assert state["review_code_round_count"] == i


def test_round_ceiling_not_triggered_within_budget(tmp_path: Path) -> None:
    """Invocations 1..max_review_rounds run the reviewer normally (no ceiling)."""
    _ceilings_config(tmp_path, max_review_rounds=2)
    state = _agent_pair_state()
    for _ in range(2):
        res, rwf = _run_review(tmp_path, state, _ok("CHANGES_REQUESTED"))
        assert "ceiling_hit" not in res
        rwf.assert_called()


def test_round_ceiling_triggers_on_max_plus_one(tmp_path: Path) -> None:
    """Invocation max+1 returns ceiling_hit='max_review_rounds', no reviewer call."""
    _ceilings_config(tmp_path, max_review_rounds=2)
    state = _agent_pair_state()
    # Exhaust the budget (invocations 1 and 2)
    _run_review(tmp_path, state, _ok("CHANGES_REQUESTED"))
    _run_review(tmp_path, state, _ok("CHANGES_REQUESTED"))
    # Invocation 3 (max+1) should hit the ceiling
    res, rwf = _run_review(tmp_path, state, _ok("APPROVED"))
    assert res.get("ceiling_hit") == "max_review_rounds"
    assert res["status"] == "error"
    rwf.assert_not_called()


def test_round_ceiling_no_wall_clock_written(tmp_path: Path) -> None:
    """With only max_review_rounds configured, wall-clock field is never written."""
    _ceilings_config(tmp_path, max_review_rounds=1)
    state = _agent_pair_state()
    # Exhaust and trigger
    _run_review(tmp_path, state, _ok("CHANGES_REQUESTED"))
    _run_review(tmp_path, state, _ok("APPROVED"))
    assert "review_code_wall_clock_sec" not in state


def test_round_counter_not_written_when_only_wall_clock_configured(tmp_path: Path) -> None:
    """With only max_wall_clock_sec configured, round counter is never written."""
    _ceilings_config(tmp_path, max_wall_clock_sec=300)
    state = _agent_pair_state()
    _run_review(tmp_path, state, _ok("CHANGES_REQUESTED"), monotonic_seq=[0.0, 1.0])
    assert "review_code_round_count" not in state


# ---------------------------------------------------------------------------
# Wall-clock ceiling — pre-dispatch skip (D3, D4 step 3)
# ---------------------------------------------------------------------------


def test_wall_clock_pre_dispatch_skip(tmp_path: Path) -> None:
    """With accrued >= max at entry, reviewer is NOT invoked; ceiling is returned."""
    _ceilings_config(tmp_path, max_wall_clock_sec=100)
    state = _agent_pair_state(review_code_wall_clock_sec=100.0)
    res, rwf = _run_review(tmp_path, state, _ok("APPROVED"))
    assert res.get("ceiling_hit") == "max_wall_clock_sec"
    assert res["status"] == "error"
    rwf.assert_not_called()


def test_wall_clock_pre_dispatch_skip_equal_exactly(tmp_path: Path) -> None:
    """Exactly at the ceiling value (>=) also skips dispatch."""
    _ceilings_config(tmp_path, max_wall_clock_sec=50)
    state = _agent_pair_state(review_code_wall_clock_sec=50.0)
    res, rwf = _run_review(tmp_path, state, _ok("APPROVED"))
    assert res.get("ceiling_hit") == "max_wall_clock_sec"
    rwf.assert_not_called()


# ---------------------------------------------------------------------------
# Wall-clock ceiling — post-dispatch upgrade (D4 step 6)
# ---------------------------------------------------------------------------


def test_wall_clock_post_dispatch_upgrade_approved(tmp_path: Path) -> None:
    """Reviewer returns APPROVED but accrual pushes total >= max: ceiling returned."""
    _ceilings_config(tmp_path, max_wall_clock_sec=10)
    # Start just below the ceiling
    state = _agent_pair_state(review_code_wall_clock_sec=9.0)
    # monotonic seq: t0=0.0, t1=5.0 → dt=5.0, total=14.0 >= 10
    res, rwf = _run_review(tmp_path, state, _ok("APPROVED"), monotonic_seq=[0.0, 5.0])
    assert res.get("ceiling_hit") == "max_wall_clock_sec"
    assert res["status"] == "error"
    rwf.assert_called_once()


def test_wall_clock_post_dispatch_upgrade_raw_persisted(tmp_path: Path) -> None:
    """Reviewer's raw IS persisted to code_review.md even when ceiling is hit."""
    _ceilings_config(tmp_path, max_wall_clock_sec=10)
    state = _agent_pair_state(review_code_wall_clock_sec=9.5)
    _run_review(tmp_path, state, _ok("APPROVED", raw="raw body\nREVIEW_DECISION: APPROVED"), monotonic_seq=[0.0, 1.0])
    review_path = tmp_path / "code_review.md"
    assert review_path.exists()
    assert "raw body" in review_path.read_text(encoding="utf-8")


def test_wall_clock_post_dispatch_upgrade_no_approval(tmp_path: Path) -> None:
    """Post-dispatch ceiling hit: status is 'error', not 'approved'."""
    _ceilings_config(tmp_path, max_wall_clock_sec=10)
    state = _agent_pair_state(review_code_wall_clock_sec=9.9)
    res, _ = _run_review(tmp_path, state, _ok("APPROVED"), monotonic_seq=[0.0, 5.0])
    assert res["status"] == "error"
    assert res.get("ceiling_hit") == "max_wall_clock_sec"
    assert res["status"] != "approved"


# ---------------------------------------------------------------------------
# Approval-authority invariant (D4, D5)
# ---------------------------------------------------------------------------


def test_no_path_sets_ceiling_hit_and_approved(tmp_path: Path) -> None:
    """Hard invariant: ceiling_hit and status=='approved' cannot coexist."""
    _ceilings_config(tmp_path, max_review_rounds=1, max_wall_clock_sec=10)
    state = _agent_pair_state()
    # Test multiple scenarios that could theoretically produce both
    scenarios = [
        (_ok("APPROVED"), [0.0, 0.1]),  # within budget (no ceiling hit)
        (_ok("APPROVED"), [0.0, 20.0]),  # wall-clock crossed (post-dispatch)
    ]
    for rwf_ret, mono in scenarios:
        state_copy = dict(state)
        state_copy["review_code_round_count"] = 0
        state_copy.pop("review_code_wall_clock_sec", None)
        res, _ = _run_review(tmp_path, state_copy, rwf_ret, monotonic_seq=mono)
        if res.get("ceiling_hit"):
            assert res["status"] != "approved", (
                f"ceiling_hit={res['ceiling_hit']!r} but status='approved' — invariant violated"
            )


def test_ceiling_hit_never_produces_approved_status(tmp_path: Path) -> None:
    """No matter which ceiling is crossed, the result status must not be 'approved'."""
    _ceilings_config(tmp_path, max_review_rounds=2)
    state = _agent_pair_state()
    # Run 3 times — 3rd hits the round ceiling
    _run_review(tmp_path, state, _ok("APPROVED"))
    _run_review(tmp_path, state, _ok("APPROVED"))
    res, _ = _run_review(tmp_path, state, _ok("APPROVED"))
    assert res.get("ceiling_hit") == "max_review_rounds"
    assert res["status"] == "error"
    assert res["status"] != "approved"


# ---------------------------------------------------------------------------
# Orchestrator routing — defer, not rescue (D6)
# ---------------------------------------------------------------------------


def _orch():
    import _engine

    return _engine.orchestrator()


def _build_orch_state(**extra) -> dict:
    base = {
        "task_id": "t1",
        "mode": "agent-pair",
        "base_branch": "main",
        "phase": "review_code",
        "phases_completed": ["plan_outcome", "plan_review", "implement"],
        "next_phase": "review_code",
        "review_items": [],
        "retries": {},
        "rescue_entry_count": 0,
        "max_retries_per_phase": 2,
        "verification": {"last_exit_code": 0},
        "escape": {"ask_user": False, "reason": None, "return_phase": None},
    }
    base.update(extra)
    return base


def _ceiling_result(ceiling_hit: str) -> dict:
    return {
        "status": "error",
        "feedback": f"ceiling: {ceiling_hit}",
        "log": f"ceiling: {ceiling_hit}",
        "diff": "",
        "ceiling_hit": ceiling_hit,
    }


def test_orchestrator_ceiling_routes_to_deferred(tmp_path: Path) -> None:
    """A ceiling_hit result defers the task (not rescue, not retry)."""
    orch = _orch()
    task_dir = tmp_path / "tasks" / "t1"
    task_dir.mkdir(parents=True)
    (task_dir / "code_review.done").write_text("", encoding="utf-8")
    state = _build_orch_state(review_code_round_count=3)
    (task_dir / "state.json").write_text(json.dumps(state), encoding="utf-8")

    orch.monkeypatch = None  # ensure clean state
    with (
        patch.object(orch, "_ensure_task_branch", return_value="p/t1"),
        patch.object(orch, "repo_root", return_value=tmp_path),
    ):
        orch.PHASE_RUNNERS["review_code"] = lambda td, s: _ceiling_result("max_review_rounds")
        outcome = orch.process_task(task_dir)

    saved = json.loads((task_dir / "state.json").read_text(encoding="utf-8"))
    assert outcome == "deferred"
    assert saved["next_phase"] == "deferred"


def test_orchestrator_ceiling_deferred_requirements_entry(tmp_path: Path) -> None:
    """The deferred_requirements entry has the correct structure from D6."""
    orch = _orch()
    task_dir = tmp_path / "tasks" / "t2"
    task_dir.mkdir(parents=True)
    (task_dir / "code_review.done").write_text("", encoding="utf-8")
    state = _build_orch_state(review_code_round_count=5, review_code_wall_clock_sec=12.5)
    (task_dir / "state.json").write_text(json.dumps(state), encoding="utf-8")

    with (
        patch.object(orch, "_ensure_task_branch", return_value="p/t2"),
        patch.object(orch, "repo_root", return_value=tmp_path),
    ):
        orch.PHASE_RUNNERS["review_code"] = lambda td, s: _ceiling_result("max_review_rounds")
        orch.process_task(task_dir)

    saved = json.loads((task_dir / "state.json").read_text(encoding="utf-8"))
    entries = [r for r in saved.get("deferred_requirements", []) if "exceeded" in r.get("reason", "")]
    assert len(entries) == 1
    entry = entries[0]
    assert entry["phase"] == "review_code"
    assert entry["reason"] == "review_code_max_review_rounds_exceeded"
    assert entry["round_count"] == 5
    assert entry["wall_clock_sec"] == 12.5
    assert "feedback" in entry


def test_orchestrator_ceiling_wall_clock_entry_shape(tmp_path: Path) -> None:
    """Wall-clock ceiling produces the correct reason string in deferred_requirements."""
    orch = _orch()
    task_dir = tmp_path / "tasks" / "t3"
    task_dir.mkdir(parents=True)
    (task_dir / "code_review.done").write_text("", encoding="utf-8")
    state = _build_orch_state(review_code_wall_clock_sec=600.5)
    (task_dir / "state.json").write_text(json.dumps(state), encoding="utf-8")

    with (
        patch.object(orch, "_ensure_task_branch", return_value="p/t3"),
        patch.object(orch, "repo_root", return_value=tmp_path),
    ):
        orch.PHASE_RUNNERS["review_code"] = lambda td, s: _ceiling_result("max_wall_clock_sec")
        orch.process_task(task_dir)

    saved = json.loads((task_dir / "state.json").read_text(encoding="utf-8"))
    entries = [r for r in saved.get("deferred_requirements", []) if "exceeded" in r.get("reason", "")]
    assert len(entries) == 1
    assert entries[0]["reason"] == "review_code_max_wall_clock_sec_exceeded"
    assert entries[0]["wall_clock_sec"] == 600.5


def test_orchestrator_ceiling_review_audit_entry(tmp_path: Path) -> None:
    """A ceiling-terminated round appends an entry to review_audit."""
    orch = _orch()
    task_dir = tmp_path / "tasks" / "t4"
    task_dir.mkdir(parents=True)
    (task_dir / "code_review.done").write_text("", encoding="utf-8")
    state = _build_orch_state()
    (task_dir / "state.json").write_text(json.dumps(state), encoding="utf-8")

    with (
        patch.object(orch, "_ensure_task_branch", return_value="p/t4"),
        patch.object(orch, "repo_root", return_value=tmp_path),
    ):
        orch.PHASE_RUNNERS["review_code"] = lambda td, s: _ceiling_result("max_wall_clock_sec")
        orch.process_task(task_dir)

    saved = json.loads((task_dir / "state.json").read_text(encoding="utf-8"))
    audit = saved.get("review_audit", [])
    ceiling_entries = [e for e in audit if e.get("reason") == "max_wall_clock_sec"]
    assert len(ceiling_entries) == 1
    assert ceiling_entries[0]["phase"] == "review_code"


def test_orchestrator_ceiling_not_routed_to_rescue(tmp_path: Path) -> None:
    """A ceiling hit does NOT route to rescue — rescue_entry_count is unchanged."""
    orch = _orch()
    task_dir = tmp_path / "tasks" / "t5"
    task_dir.mkdir(parents=True)
    (task_dir / "code_review.done").write_text("", encoding="utf-8")
    state = _build_orch_state(rescue_entry_count=0)
    (task_dir / "state.json").write_text(json.dumps(state), encoding="utf-8")

    with (
        patch.object(orch, "_ensure_task_branch", return_value="p/t5"),
        patch.object(orch, "repo_root", return_value=tmp_path),
    ):
        orch.PHASE_RUNNERS["review_code"] = lambda td, s: _ceiling_result("max_review_rounds")
        orch.process_task(task_dir)

    saved = json.loads((task_dir / "state.json").read_text(encoding="utf-8"))
    assert saved["next_phase"] == "deferred"
    # rescue_entry_count must not have been incremented
    assert saved.get("rescue_entry_count", 0) == 0


# ---------------------------------------------------------------------------
# Wall-clock accrual — monotonic and cumulative (D3)
# ---------------------------------------------------------------------------


def test_wall_clock_accrual_cumulative(tmp_path: Path) -> None:
    """After N invocations, state wall_clock_sec equals the sum of N dts."""
    _ceilings_config(tmp_path, max_wall_clock_sec=9999)
    state = _agent_pair_state()
    # Invocation 1: t0=0, t1=3 → dt=3
    _run_review(tmp_path, state, _ok("CHANGES_REQUESTED"), monotonic_seq=[0.0, 3.0])
    assert abs(state.get("review_code_wall_clock_sec", 0.0) - 3.0) < 0.001
    # Invocation 2: t0=0, t1=5 → dt=5, cumulative=8
    _run_review(tmp_path, state, _ok("CHANGES_REQUESTED"), monotonic_seq=[0.0, 5.0])
    assert abs(state.get("review_code_wall_clock_sec", 0.0) - 8.0) < 0.001


def test_wall_clock_legacy_state_starts_at_zero(tmp_path: Path) -> None:
    """Legacy state without the counter treats it as 0.0."""
    _ceilings_config(tmp_path, max_wall_clock_sec=9999)
    state = _agent_pair_state()  # no review_code_wall_clock_sec
    _run_review(tmp_path, state, _ok("CHANGES_REQUESTED"), monotonic_seq=[0.0, 2.0])
    assert abs(state.get("review_code_wall_clock_sec", 0.0) - 2.0) < 0.001


# ---------------------------------------------------------------------------
# Case A (manual) — no wall-clock accrual, round counter still increments (D4)
# ---------------------------------------------------------------------------


def test_case_a_no_wall_clock_accrual(tmp_path: Path) -> None:
    """Manual reviewer (Case A): wall-clock is NOT accrued even with ceiling configured."""
    _ceilings_config(tmp_path, max_review_rounds=5, max_wall_clock_sec=9999)
    state = _agent_pair_state()
    state["models"]["reviewer"] = "human"
    (tmp_path / "code_review.md").write_text("body\nREVIEW_DECISION: APPROVED", encoding="utf-8")
    calls: list = []
    with (
        patch("phase_runners.review_code.compute_repo_diff", return_value=""),
        patch("phase_runners.review_code.repo_root", return_value=tmp_path),
        patch("phase_runners.review_code.review_with_fallback"),
        patch("phase_runners.review_code.review_with_fallback_for_provider"),
        patch("phase_runners.review_code.time") as mock_time,
    ):
        mock_time.monotonic.side_effect = lambda: calls.append(1) or 0.0
        res = review_code.run(tmp_path, state)
    assert res["status"] == "approved"
    assert "review_code_wall_clock_sec" not in state
    assert calls == [], "time.monotonic() must not be called on Case A (manual)"


def test_case_a_round_counter_increments(tmp_path: Path) -> None:
    """Manual reviewer (Case A): round counter DOES increment (budget is per-invocation)."""
    _ceilings_config(tmp_path, max_review_rounds=5)
    state = _agent_pair_state()
    state["models"]["reviewer"] = "human"
    (tmp_path / "code_review.md").write_text("body\nREVIEW_DECISION: APPROVED", encoding="utf-8")
    with (
        patch("phase_runners.review_code.compute_repo_diff", return_value=""),
        patch("phase_runners.review_code.repo_root", return_value=tmp_path),
        patch("phase_runners.review_code.review_with_fallback"),
        patch("phase_runners.review_code.review_with_fallback_for_provider"),
        patch("phase_runners.review_code.time"),
    ):
        review_code.run(tmp_path, state)
    assert state.get("review_code_round_count") == 1


def test_case_a_hits_round_ceiling(tmp_path: Path) -> None:
    """Manual reviewer (Case A) can still hit the round ceiling."""
    _ceilings_config(tmp_path, max_review_rounds=1)
    state = _agent_pair_state()
    state["models"]["reviewer"] = "human"
    (tmp_path / "code_review.md").write_text("body\nREVIEW_DECISION: APPROVED", encoding="utf-8")
    # First invocation: consumed the budget
    with (
        patch("phase_runners.review_code.compute_repo_diff", return_value=""),
        patch("phase_runners.review_code.repo_root", return_value=tmp_path),
        patch("phase_runners.review_code.review_with_fallback"),
        patch("phase_runners.review_code.review_with_fallback_for_provider"),
        patch("phase_runners.review_code.time"),
    ):
        review_code.run(tmp_path, state)
    assert state["review_code_round_count"] == 1
    # Second invocation: should hit the ceiling before reading code_review.md
    with (
        patch("phase_runners.review_code.compute_repo_diff", return_value=""),
        patch("phase_runners.review_code.repo_root", return_value=tmp_path),
        patch("phase_runners.review_code.review_with_fallback"),
        patch("phase_runners.review_code.review_with_fallback_for_provider"),
        patch("phase_runners.review_code.time"),
    ):
        res = review_code.run(tmp_path, state)
    assert res.get("ceiling_hit") == "max_review_rounds"
    assert res["status"] == "error"


# ---------------------------------------------------------------------------
# Ceilings + P3 staging interoperate (D1)
# ---------------------------------------------------------------------------


def test_ceilings_and_staging_coexist(tmp_path: Path) -> None:
    """Both [models.review_stages] and [models.review_ceilings] load without error."""
    _write_config(
        tmp_path,
        '\n[models.review_stages]\nfirst_pass_reviewer = "codex"\nescalate_after = 2\n'
        "\n[models.review_ceilings]\nmax_review_rounds = 5\nmax_wall_clock_sec = 300\n",
    )
    cfg = load_config(tmp_path)
    assert cfg.models.review_stages is not None
    assert cfg.models.review_stages.first_pass_reviewer == "codex"
    assert cfg.models.review_ceilings is not None
    assert cfg.models.review_ceilings.max_review_rounds == 5
    assert cfg.models.review_ceilings.max_wall_clock_sec == 300


def test_ceiling_on_first_pass_round_counts(tmp_path: Path) -> None:
    """A first-pass round (Case D) still increments the round counter."""
    _write_config(
        tmp_path,
        '\n[models.review_stages]\nfirst_pass_reviewer = "codex"\nescalate_after = 5\n'
        "\n[models.review_ceilings]\nmax_review_rounds = 3\n",
    )
    state = _agent_pair_state(implement_round_count=1)
    with (
        patch("phase_runners.review_code.get_reviewer_adapter", return_value=MagicMock()),
        patch("phase_runners.review_code.review_with_fallback", return_value=_ok("CHANGES_REQUESTED")),
        patch(
            "phase_runners.review_code.review_with_fallback_for_provider",
            return_value=_ok("CHANGES_REQUESTED"),
        ),
        patch("phase_runners.review_code.compute_repo_diff", return_value=""),
        patch("phase_runners.review_code.repo_root", return_value=tmp_path),
        patch("phase_runners.review_code.git_rev_parse", return_value=_HEAD_REV),
        patch("phase_runners.review_code._is_ancestor", return_value=False),
        patch("phase_runners.review_code._incremental_diff_nonempty", return_value=False),
        patch("phase_runners.review_code.time") as mock_time,
    ):
        mock_time.monotonic.return_value = 0.0
        review_code.run(tmp_path, state)
    assert state["review_code_round_count"] == 1


def test_ceiling_on_promoted_round_terminates_non_approved(tmp_path: Path) -> None:
    """A ceiling hit on a promoted round (first_pass → frontier) returns ceiling, not approved."""
    _write_config(
        tmp_path,
        '\n[models.review_stages]\nfirst_pass_reviewer = "codex"\nescalate_after = 5\n'
        "\n[models.review_ceilings]\nmax_wall_clock_sec = 1\n",
    )
    # Already at budget boundary — post-dispatch will cross it
    state = _agent_pair_state(implement_round_count=1, review_code_wall_clock_sec=0.5)
    mono_seq = iter([0.0, 5.0])  # dt=5s, pushes total to 5.5 >= 1
    with (
        patch("phase_runners.review_code.get_reviewer_adapter", return_value=MagicMock()),
        # Frontier reviewer returns APPROVED
        patch("phase_runners.review_code.review_with_fallback", return_value=_ok("APPROVED")),
        # First-pass also returns APPROVED to trigger promotion
        patch(
            "phase_runners.review_code.review_with_fallback_for_provider",
            return_value=_ok("APPROVED"),
        ),
        patch("phase_runners.review_code.compute_repo_diff", return_value=""),
        patch("phase_runners.review_code.repo_root", return_value=tmp_path),
        patch("phase_runners.review_code.git_rev_parse", return_value=_HEAD_REV),
        patch("phase_runners.review_code._is_ancestor", return_value=False),
        patch("phase_runners.review_code._incremental_diff_nonempty", return_value=False),
        patch("phase_runners.review_code.time") as mock_time,
    ):
        mock_time.monotonic.side_effect = lambda: next(mono_seq)
        res = review_code.run(tmp_path, state)
    assert res.get("ceiling_hit") == "max_wall_clock_sec"
    assert res["status"] != "approved"


# ---------------------------------------------------------------------------
# Persistence across resumes (D2, D3)
# ---------------------------------------------------------------------------


def test_persistence_round_counter_survives_resume(tmp_path: Path) -> None:
    """After a run, the counter is persisted and resumes correctly on next call."""
    _ceilings_config(tmp_path, max_review_rounds=10)
    state = _agent_pair_state()
    _run_review(tmp_path, state, _ok("CHANGES_REQUESTED"))
    assert state["review_code_round_count"] == 1
    # Simulate resume: start from the same state dict
    _run_review(tmp_path, state, _ok("CHANGES_REQUESTED"))
    assert state["review_code_round_count"] == 2


def test_persistence_wall_clock_survives_resume(tmp_path: Path) -> None:
    """Wall-clock accrual persists across calls (simulating resume)."""
    _ceilings_config(tmp_path, max_wall_clock_sec=9999)
    state = _agent_pair_state()
    _run_review(tmp_path, state, _ok("CHANGES_REQUESTED"), monotonic_seq=[0.0, 4.0])
    wc1 = state.get("review_code_wall_clock_sec", 0.0)
    _run_review(tmp_path, state, _ok("CHANGES_REQUESTED"), monotonic_seq=[0.0, 3.0])
    wc2 = state.get("review_code_wall_clock_sec", 0.0)
    assert abs(wc1 - 4.0) < 0.001
    assert abs(wc2 - 7.0) < 0.001


# ---------------------------------------------------------------------------
# Convergence does NOT reset ceiling counters (D4)
# ---------------------------------------------------------------------------


def test_convergence_does_not_reset_round_counter(tmp_path: Path) -> None:
    """On an APPROVED result, review_code_round_count is NOT reset."""
    _ceilings_config(tmp_path, max_review_rounds=10)
    state = _agent_pair_state()
    _run_review(tmp_path, state, _ok("CHANGES_REQUESTED"))
    _run_review(tmp_path, state, _ok("APPROVED"))
    # Counter should be 2, not reset to 0 or 1
    assert state.get("review_code_round_count") == 2


def test_convergence_does_not_reset_wall_clock(tmp_path: Path) -> None:
    """On an APPROVED result, review_code_wall_clock_sec is NOT reset."""
    _ceilings_config(tmp_path, max_wall_clock_sec=9999)
    state = _agent_pair_state()
    _run_review(tmp_path, state, _ok("CHANGES_REQUESTED"), monotonic_seq=[0.0, 5.0])
    _run_review(tmp_path, state, _ok("APPROVED"), monotonic_seq=[0.0, 3.0])
    # Total should be 8, not reset
    assert abs(state.get("review_code_wall_clock_sec", 0.0) - 8.0) < 0.001


# ---------------------------------------------------------------------------
# Adapter files unchanged — no cache-control code (D7)
# ---------------------------------------------------------------------------


def test_adapter_claude_no_cache_control() -> None:
    """adapters/claude.py contains no prompt-caching / cache-control code."""
    adapter_path = _WF / "adapters" / "claude.py"
    content = adapter_path.read_text(encoding="utf-8")
    for marker in ("cache_control", "prompt_cache", "--cache"):
        assert marker not in content, f"Found {marker!r} in adapters/claude.py (D7: must be untouched)"


def test_adapter_codex_no_cache_control() -> None:
    """adapters/codex.py contains no prompt-caching / cache-control code."""
    adapter_path = _WF / "adapters" / "codex.py"
    content = adapter_path.read_text(encoding="utf-8")
    for marker in ("cache_control", "prompt_cache", "--cache"):
        assert marker not in content, f"Found {marker!r} in adapters/codex.py (D7: must be untouched)"


# ---------------------------------------------------------------------------
# Decision doc exists (D7)
# ---------------------------------------------------------------------------


def test_decision_doc_exists() -> None:
    """docs/decisions/2026-07-05-reviewer-prompt-caching.md exists and is non-empty."""
    repo = Path(__file__).resolve().parents[2]
    doc = repo / "docs" / "decisions" / "2026-07-05-reviewer-prompt-caching.md"
    assert doc.is_file(), f"Decision doc not found at {doc}"
    assert doc.stat().st_size > 0, "Decision doc is empty"


# ---------------------------------------------------------------------------
# Dogfood config assertion (Done-when)
# ---------------------------------------------------------------------------


def test_dogfood_config_review_ceilings_is_none() -> None:
    """This repo's own .redteam/config.toml has review_ceilings=None (not opted in)."""
    repo = Path(__file__).resolve().parents[2]
    cfg = load_config(repo)
    assert cfg.models.review_ceilings is None, "This repo must NOT opt into ceilings — only users of the harness do so"
