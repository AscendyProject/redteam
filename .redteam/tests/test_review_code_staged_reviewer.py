"""P3 round-staged reviewer — D1–D8 behaviors (outcome.md task-001-p3-staged-reviewer).

Tests cover:
- Config parsing (happy path and fail-loud)
- Tier-level staging is globally rejected
- Default parity (no staging = today's behavior)
- D5 dispatch cases A / B / C / D
- Cheap-APPROVED promotion (D3) and artifact persistence (D7)
- Approval-authority invariant (D3 hard guard)
- review_with_fallback_for_provider shares the ladder (D6)
- D8 first-pass same-provider guard
- review_audit wiring for staging_audit
- First-pass artifact rotation via _clear_manual_phase_artifacts
- plan_review.run still calls review_with_fallback (not _for_provider)
- Dogfood-config assertion (this repo's config.toml has review_stages=None)
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
from config import ReviewStagesConfig, load_config  # noqa: E402

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


def _staged_config(tmp_path: Path, first_pass: str = "codex", escalate_after: int = 2) -> None:
    """Write a config with [models.review_stages] staging enabled."""
    _write_config(
        tmp_path,
        f'\n[models.review_stages]\nfirst_pass_reviewer = "{first_pass}"\nescalate_after = {escalate_after}\n',
    )


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


def _infra_fail() -> dict:
    return {"decision": "MISSING", "raw": "timeout", "parse_status": "error"}


def _manual_result() -> dict:
    return {"decision": "MISSING", "raw": "manual required", "parse_status": adapters.MANUAL_REQUIRED}


def _fake_adapter(decision: str = "APPROVED", parse_status: str = "ok", read_only: bool = True) -> MagicMock:
    a = MagicMock()
    a.name = "fake"
    a.capabilities = {"read_only_enforced": read_only, "native_diff_review": False}
    a.review.return_value = {"decision": decision, "raw": "review body", "parse_status": parse_status}
    return a


def _run_review(
    tmp_path: Path,
    state: dict,
    rwf_return: dict,
    *,
    rwf_fp_return: dict | None = None,
    head_rev: str = _HEAD_REV,
    is_ancestor: bool = False,
    diff_nonempty: bool = False,
) -> tuple:
    """Run review_code.run() with all I/O patched.

    Returns (result, rwf_mock, rwf_fp_mock) where rwf_fp_mock is the
    review_with_fallback_for_provider mock.
    """
    fp_return = rwf_fp_return if rwf_fp_return is not None else _ok("CHANGES_REQUESTED")
    with (
        patch("phase_runners.review_code.get_reviewer_adapter", return_value=MagicMock()),
        patch("phase_runners.review_code.review_with_fallback", return_value=rwf_return) as rwf,
        patch("phase_runners.review_code.review_with_fallback_for_provider", return_value=fp_return) as rwf_fp,
        patch("phase_runners.review_code.compute_repo_diff", return_value=""),
        patch("phase_runners.review_code.repo_root", return_value=tmp_path),
        patch("phase_runners.review_code.git_rev_parse", return_value=head_rev),
        patch("phase_runners.review_code._is_ancestor", return_value=is_ancestor),
        patch("phase_runners.review_code._incremental_diff_nonempty", return_value=diff_nonempty),
    ):
        res = review_code.run(tmp_path, state)
    return res, rwf, rwf_fp


# ---------------------------------------------------------------------------
# Config parsing — happy path (D1)
# ---------------------------------------------------------------------------


def test_review_stages_happy_path(tmp_path: Path) -> None:
    """Valid [models.review_stages] loads cleanly and is reachable."""
    _staged_config(tmp_path, first_pass="codex", escalate_after=3)
    cfg = load_config(tmp_path)
    rs = cfg.models.review_stages
    assert rs is not None
    assert isinstance(rs, ReviewStagesConfig)
    assert rs.first_pass_reviewer == "codex"
    assert rs.escalate_after == 3


def test_review_stages_with_claude_first_pass(tmp_path: Path) -> None:
    """claude is also a valid first_pass_reviewer."""
    _staged_config(tmp_path, first_pass="claude", escalate_after=1)
    cfg = load_config(tmp_path)
    rs = cfg.models.review_stages
    assert rs is not None
    assert rs.first_pass_reviewer == "claude"
    assert rs.escalate_after == 1


# ---------------------------------------------------------------------------
# Config parsing — fail-loud (D1)
# ---------------------------------------------------------------------------


def test_review_stages_unknown_key_rejected(tmp_path: Path) -> None:
    """Unknown key inside [models.review_stages] raises ValueError."""
    _write_config(
        tmp_path,
        '\n[models.review_stages]\nfirst_pass_reviewer = "codex"\nescalate_after = 2\nunknown_key = "bad"\n',
    )
    with pytest.raises(ValueError, match="Unknown models.review_stages config key"):
        load_config(tmp_path)


def test_review_stages_unknown_provider_rejected(tmp_path: Path) -> None:
    """first_pass_reviewer that is not a registered adapter key raises."""
    _write_config(
        tmp_path,
        '\n[models.review_stages]\nfirst_pass_reviewer = "gpt-5"\nescalate_after = 2\n',
    )
    with pytest.raises(ValueError, match="registered reviewer adapter key"):
        load_config(tmp_path)


def test_review_stages_manual_first_pass_rejected(tmp_path: Path) -> None:
    _write_config(
        tmp_path,
        '\n[models.review_stages]\nfirst_pass_reviewer = "manual"\nescalate_after = 2\n',
    )
    with pytest.raises(ValueError, match="cannot be 'manual' or 'human'"):
        load_config(tmp_path)


def test_review_stages_human_first_pass_rejected(tmp_path: Path) -> None:
    _write_config(
        tmp_path,
        '\n[models.review_stages]\nfirst_pass_reviewer = "human"\nescalate_after = 2\n',
    )
    with pytest.raises(ValueError, match="cannot be 'manual' or 'human'"):
        load_config(tmp_path)


def test_review_stages_escalate_after_zero_rejected(tmp_path: Path) -> None:
    _write_config(
        tmp_path,
        '\n[models.review_stages]\nfirst_pass_reviewer = "codex"\nescalate_after = 0\n',
    )
    with pytest.raises(ValueError, match="must be an int >= 1"):
        load_config(tmp_path)


def test_review_stages_escalate_after_negative_rejected(tmp_path: Path) -> None:
    _write_config(
        tmp_path,
        '\n[models.review_stages]\nfirst_pass_reviewer = "codex"\nescalate_after = -1\n',
    )
    with pytest.raises(ValueError, match="must be an int >= 1"):
        load_config(tmp_path)


def test_review_stages_escalate_after_bool_rejected(tmp_path: Path) -> None:
    """bool values for escalate_after must be rejected (bool subclasses int)."""
    _write_config(
        tmp_path,
        '\n[models.review_stages]\nfirst_pass_reviewer = "codex"\nescalate_after = true\n',
    )
    with pytest.raises(ValueError, match="bool values rejected"):
        load_config(tmp_path)


def test_review_stages_missing_first_pass_reviewer_rejected(tmp_path: Path) -> None:
    _write_config(
        tmp_path,
        "\n[models.review_stages]\nescalate_after = 2\n",
    )
    with pytest.raises(ValueError, match="first_pass_reviewer is required"):
        load_config(tmp_path)


def test_review_stages_missing_escalate_after_rejected(tmp_path: Path) -> None:
    _write_config(
        tmp_path,
        '\n[models.review_stages]\nfirst_pass_reviewer = "codex"\n',
    )
    with pytest.raises(ValueError, match="escalate_after is required"):
        load_config(tmp_path)


# ---------------------------------------------------------------------------
# Tier-level staging is REJECTED — global-only invariant (D1)
# ---------------------------------------------------------------------------


def test_tier_level_review_stages_dict_rejected(tmp_path: Path) -> None:
    """A [tiers.N].models table with a review_stages sub-table is rejected
    by the existing unknown-role OR non-str-value fail-loud path."""
    # review_stages as a dict value — hits the isinstance(model, str) check
    # in _parse_tiers because dicts are not strings.
    (tmp_path / ".redteam").mkdir(exist_ok=True)
    (tmp_path / ".redteam" / "config.toml").write_text(
        '[project]\nname = "p"\nsource_dirs = ["src/"]\ntest_file_glob = "test_*.py"\n'
        'verification_allowlist = ["pytest"]\n'
        "[tiers.1]\nreview = true\n"
        '[tiers.1.models.review_stages]\nfirst_pass_reviewer = "codex"\nescalate_after = 2\n'
        "[tier_triggers]\ndefault = 1\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError):
        load_config(tmp_path)


def test_tier_level_review_stages_string_rejected(tmp_path: Path) -> None:
    """review_stages as a bare string role value is rejected as an unknown role."""
    (tmp_path / ".redteam").mkdir(exist_ok=True)
    (tmp_path / ".redteam" / "config.toml").write_text(
        '[project]\nname = "p"\nsource_dirs = ["src/"]\ntest_file_glob = "test_*.py"\n'
        'verification_allowlist = ["pytest"]\n'
        "[tiers.1]\nreview = true\n"
        '[tiers.1.models]\nreview_stages = "codex"\n'
        "[tier_triggers]\ndefault = 1\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="unknown role"):
        load_config(tmp_path)


# ---------------------------------------------------------------------------
# Default parity — no staging (D4 / Done-when)
# ---------------------------------------------------------------------------


def test_default_config_review_stages_is_none() -> None:
    """With no config.toml, review_stages is None (default parity)."""
    import tempfile

    with tempfile.TemporaryDirectory() as d:
        cfg = load_config(Path(d))
    assert cfg.models.review_stages is None


def test_no_staging_uses_review_with_fallback_once(tmp_path: Path) -> None:
    """With staging OFF, review_code.run invokes review_with_fallback exactly once
    (Case B) and never calls review_with_fallback_for_provider."""
    # No config.toml → load_config returns defaults with review_stages=None
    state = _agent_pair_state(implement_round_count=1)
    res, rwf, rwf_fp = _run_review(tmp_path, state, _ok("APPROVED"))
    assert res["status"] == "approved"
    rwf.assert_called_once()
    rwf_fp.assert_not_called()


def test_no_staging_no_first_pass_artifact(tmp_path: Path) -> None:
    """Without staging, code_review.first_pass.md is never produced."""
    state = _agent_pair_state(implement_round_count=1)
    _run_review(tmp_path, state, _ok("APPROVED"))
    assert not (tmp_path / "code_review.first_pass.md").exists()


def test_no_staging_prompt_byte_identical(tmp_path: Path) -> None:
    """With staging OFF, the prompt is the same as _code_review_prompt (Case B)."""
    state = _agent_pair_state()
    _, rwf, _ = _run_review(tmp_path, state, _ok("APPROVED"))
    kwargs = rwf.call_args.kwargs if hasattr(rwf.call_args, "kwargs") else rwf.call_args[1]
    assert kwargs["prompt"] == review_code._code_review_prompt(tmp_path, _BASE_BRANCH)


# ---------------------------------------------------------------------------
# Case A — manual reviewer bypasses staging
# ---------------------------------------------------------------------------


def test_case_a_human_reviewer_bypasses_staging(tmp_path: Path) -> None:
    """reviewer=human → Case A; staging is bypassed even when configured."""
    _staged_config(tmp_path, first_pass="codex", escalate_after=2)
    state = _agent_pair_state(implement_round_count=1)
    state["models"]["reviewer"] = "human"
    (tmp_path / "code_review.md").write_text("body\nREVIEW_DECISION: APPROVED", encoding="utf-8")

    with (
        patch("phase_runners.review_code.compute_repo_diff", return_value=""),
        patch("phase_runners.review_code.repo_root", return_value=tmp_path),
        patch("phase_runners.review_code.review_with_fallback") as rwf,
        patch("phase_runners.review_code.review_with_fallback_for_provider") as rwf_fp,
    ):
        res = review_code.run(tmp_path, state)

    assert res["status"] == "approved"
    rwf.assert_not_called()
    rwf_fp.assert_not_called()


def test_case_a_prior_manual_required_bypasses_staging(tmp_path: Path) -> None:
    """manual_review_required flag → Case A even with headless adapter + staging."""
    _staged_config(tmp_path, first_pass="codex", escalate_after=2)
    state = _agent_pair_state(implement_round_count=1)
    state["manual_review_required"] = {"review_code": "fallback exhausted"}
    (tmp_path / "code_review.md").write_text("body\nREVIEW_DECISION: CHANGES_REQUESTED", encoding="utf-8")

    with (
        patch("phase_runners.review_code.get_reviewer_adapter", return_value=MagicMock()),
        patch("phase_runners.review_code.compute_repo_diff", return_value=""),
        patch("phase_runners.review_code.repo_root", return_value=tmp_path),
        patch("phase_runners.review_code.review_with_fallback") as rwf,
        patch("phase_runners.review_code.review_with_fallback_for_provider") as rwf_fp,
    ):
        res = review_code.run(tmp_path, state)

    assert res["status"] == "changes_requested"
    rwf.assert_not_called()
    rwf_fp.assert_not_called()


# ---------------------------------------------------------------------------
# Case B / C — staging ON, frontier round
# ---------------------------------------------------------------------------


def test_case_c_frontier_round_uses_review_with_fallback(tmp_path: Path) -> None:
    """implement_round_count > escalate_after → Case C: review_with_fallback called,
    NOT review_with_fallback_for_provider."""
    _staged_config(tmp_path, first_pass="codex", escalate_after=2)
    # Round 3 > escalate_after 2 → frontier
    state = _agent_pair_state(implement_round_count=3)
    res, rwf, rwf_fp = _run_review(tmp_path, state, _ok("APPROVED"))
    assert res["status"] == "approved"
    rwf.assert_called_once()
    rwf_fp.assert_not_called()
    assert not (tmp_path / "code_review.first_pass.md").exists()


def test_case_c_frontier_round_no_first_pass_artifact(tmp_path: Path) -> None:
    """Case C never writes code_review.first_pass.md."""
    _staged_config(tmp_path, first_pass="codex", escalate_after=1)
    state = _agent_pair_state(implement_round_count=2)
    _run_review(tmp_path, state, _ok("CHANGES_REQUESTED"))
    assert not (tmp_path / "code_review.first_pass.md").exists()


# ---------------------------------------------------------------------------
# Case D — routing progression
# ---------------------------------------------------------------------------


def test_case_d_routing_progression(tmp_path: Path) -> None:
    """With escalate_after=2:
    - rounds 1 and 2 → first-pass provider (review_with_fallback_for_provider)
    - round 3 → frontier (review_with_fallback)
    """
    _staged_config(tmp_path, first_pass="codex", escalate_after=2)

    invoked_as: list[str] = []

    for round_n in [1, 2, 3]:
        state = _agent_pair_state(implement_round_count=round_n)
        with (
            patch("phase_runners.review_code.get_reviewer_adapter", return_value=MagicMock()),
            patch(
                "phase_runners.review_code.review_with_fallback",
                side_effect=lambda *a, **k: invoked_as.append("frontier") or _ok("APPROVED"),
            ),
            patch(
                "phase_runners.review_code.review_with_fallback_for_provider",
                side_effect=lambda *a, **k: invoked_as.append("first_pass") or _ok("CHANGES_REQUESTED"),
            ),
            patch("phase_runners.review_code.compute_repo_diff", return_value=""),
            patch("phase_runners.review_code.repo_root", return_value=tmp_path),
            patch("phase_runners.review_code.git_rev_parse", return_value=_HEAD_REV),
            patch("phase_runners.review_code._is_ancestor", return_value=False),
            patch("phase_runners.review_code._incremental_diff_nonempty", return_value=False),
        ):
            review_code.run(tmp_path, state)

    assert invoked_as == ["first_pass", "first_pass", "frontier"]


def test_case_d_first_pass_provider_key_passed(tmp_path: Path) -> None:
    """review_with_fallback_for_provider is called with primary_provider=first_pass_reviewer."""
    _staged_config(tmp_path, first_pass="codex", escalate_after=5)
    state = _agent_pair_state(implement_round_count=1)
    _, _, rwf_fp = _run_review(tmp_path, state, _ok("APPROVED"), rwf_fp_return=_ok("CHANGES_REQUESTED"))
    rwf_fp.assert_called_once()
    # primary_provider kwarg must be "codex"
    _, kwargs = rwf_fp.call_args
    assert kwargs.get("primary_provider") == "codex"


# ---------------------------------------------------------------------------
# Case D — cheap-APPROVED promotion (D3)
# ---------------------------------------------------------------------------


def test_case_d_cheap_approved_triggers_frontier_call(tmp_path: Path) -> None:
    """First-pass APPROVED (parse_status=ok) → runner calls review_with_fallback
    a SECOND time within the same run()."""
    _staged_config(tmp_path, first_pass="codex", escalate_after=2)
    state = _agent_pair_state(implement_round_count=1)
    # first-pass returns APPROVED, frontier returns CHANGES_REQUESTED
    frontier_result = _ok("CHANGES_REQUESTED", raw="IR-001\nREVIEW_DECISION: CHANGES_REQUESTED")
    _, rwf, rwf_fp = _run_review(tmp_path, state, frontier_result, rwf_fp_return=_ok("APPROVED"))
    rwf.assert_called_once()  # exactly one frontier call
    rwf_fp.assert_called_once()  # exactly one first-pass call


def test_case_d_cheap_approved_staging_audit_set(tmp_path: Path) -> None:
    """On promotion, PhaseResult.staging_audit is a non-empty string."""
    _staged_config(tmp_path, first_pass="codex", escalate_after=2)
    state = _agent_pair_state(implement_round_count=1)
    frontier_result = _ok("CHANGES_REQUESTED")
    res, _, _ = _run_review(tmp_path, state, frontier_result, rwf_fp_return=_ok("APPROVED"))
    assert res.get("staging_audit")
    assert isinstance(res["staging_audit"], str) and len(res["staging_audit"]) > 0


def test_case_d_cheap_approved_staging_audit_names_round(tmp_path: Path) -> None:
    """staging_audit identifies the round number and the first-pass provider."""
    _staged_config(tmp_path, first_pass="codex", escalate_after=3)
    state = _agent_pair_state(implement_round_count=2)
    frontier_result = _ok("APPROVED")
    res, _, _ = _run_review(tmp_path, state, frontier_result, rwf_fp_return=_ok("APPROVED"))
    audit = res.get("staging_audit", "")
    assert "2" in audit  # round number
    assert "codex" in audit  # first-pass provider


# ---------------------------------------------------------------------------
# Case D — cheap-APPROVED promotion: frontier rejects (D3)
# ---------------------------------------------------------------------------


def test_case_d_frontier_rejects_after_promotion(tmp_path: Path) -> None:
    """First-pass APPROVED → promoted → frontier CHANGES_REQUESTED:
    final status is changes_requested, code_review.md has frontier raw,
    code_review.first_pass.md has first-pass raw."""
    _staged_config(tmp_path, first_pass="codex", escalate_after=2)
    state = _agent_pair_state(implement_round_count=1)
    first_pass_raw = "first pass body\nREVIEW_DECISION: APPROVED"
    frontier_raw = "IR-001\nREVIEW_DECISION: CHANGES_REQUESTED"
    _, rwf, rwf_fp = _run_review(
        tmp_path,
        state,
        _ok("CHANGES_REQUESTED", raw=frontier_raw),
        rwf_fp_return=_ok("APPROVED", raw=first_pass_raw),
    )
    assert (tmp_path / "code_review.md").read_text(encoding="utf-8") == frontier_raw
    assert (tmp_path / "code_review.first_pass.md").read_text(encoding="utf-8") == first_pass_raw


# ---------------------------------------------------------------------------
# Case D — first-pass CHANGES_REQUESTED is NOT promoted
# ---------------------------------------------------------------------------


def test_case_d_changes_requested_not_promoted(tmp_path: Path) -> None:
    """First-pass CHANGES_REQUESTED → no second call; status=changes_requested."""
    _staged_config(tmp_path, first_pass="codex", escalate_after=2)
    state = _agent_pair_state(implement_round_count=1)
    res, rwf, rwf_fp = _run_review(
        tmp_path,
        state,
        _ok("APPROVED"),  # would be returned by frontier if called
        rwf_fp_return=_ok("CHANGES_REQUESTED"),
    )
    assert res["status"] == "changes_requested"
    rwf.assert_not_called()  # frontier not called
    rwf_fp.assert_called_once()
    assert not (tmp_path / "code_review.first_pass.md").exists()


def test_case_d_rescue_required_not_promoted(tmp_path: Path) -> None:
    """First-pass RESCUE_REQUIRED → no second call."""
    _staged_config(tmp_path, first_pass="codex", escalate_after=2)
    state = _agent_pair_state(implement_round_count=1)
    res, rwf, rwf_fp = _run_review(
        tmp_path,
        state,
        _ok("APPROVED"),
        rwf_fp_return=_ok("RESCUE_REQUIRED"),
    )
    assert res["status"] == "rescue_required"
    rwf.assert_not_called()


def test_case_d_ask_user_not_promoted(tmp_path: Path) -> None:
    """First-pass ASK_USER → no second call."""
    _staged_config(tmp_path, first_pass="codex", escalate_after=2)
    state = _agent_pair_state(implement_round_count=1)
    res, rwf, rwf_fp = _run_review(
        tmp_path,
        state,
        _ok("APPROVED"),
        rwf_fp_return=_ok("ASK_USER"),
    )
    assert res["status"] == "ask_user"
    rwf.assert_not_called()


def test_case_d_unparseable_not_promoted(tmp_path: Path) -> None:
    """First-pass parse_status='unparseable' → no second call; error result."""
    _staged_config(tmp_path, first_pass="codex", escalate_after=2)
    state = _agent_pair_state(implement_round_count=1)
    res, rwf, rwf_fp = _run_review(
        tmp_path,
        state,
        _ok("APPROVED"),
        rwf_fp_return={"decision": "MISSING", "raw": "garbled", "parse_status": "unparseable"},
    )
    assert res["status"] == "error"
    rwf.assert_not_called()


def test_case_d_manual_required_not_promoted(tmp_path: Path) -> None:
    """First-pass MANUAL_REQUIRED → no second call; manual_required result."""
    _staged_config(tmp_path, first_pass="codex", escalate_after=2)
    state = _agent_pair_state(implement_round_count=1)
    res, rwf, rwf_fp = _run_review(
        tmp_path,
        state,
        _ok("APPROVED"),
        rwf_fp_return=_manual_result(),
    )
    assert res["status"] == "manual_required"
    rwf.assert_not_called()


# ---------------------------------------------------------------------------
# Approval-authority invariant (D3 hard guard)
# ---------------------------------------------------------------------------


def test_approval_authority_invariant(tmp_path: Path) -> None:
    """No code path must exist by which a first-pass APPROVED maps directly to
    PhaseResult(status='approved').

    This is the hard regression: we assert that when review_with_fallback_for_provider
    returns APPROVED, the runner ALWAYS invokes review_with_fallback before returning
    an approved status — even if a future refactor attempts to short-circuit the
    promotion.  We verify by checking which call produced the final 'approved'
    status: it must be the SECOND call (review_with_fallback), not the first-pass call.
    """
    _staged_config(tmp_path, first_pass="codex", escalate_after=2)
    state = _agent_pair_state(implement_round_count=1)

    # Scenario: first-pass APPROVED, frontier also APPROVED (common happy path).
    res, rwf, rwf_fp = _run_review(tmp_path, state, _ok("APPROVED"), rwf_fp_return=_ok("APPROVED"))

    # The final status is approved, but it came from the FRONTIER call.
    assert res["status"] == "approved"
    # Both calls must have happened (promotion occurred).
    rwf.assert_called_once()
    rwf_fp.assert_called_once()
    # staging_audit proves promotion happened.
    assert res.get("staging_audit"), "staging_audit must be set on a promoted round"

    # Confirm that if we suppress the frontier call (monkeypatch review_with_fallback
    # to NOT be called), the test will detect the absence of the staging_audit.
    state2 = _agent_pair_state(implement_round_count=1)
    res2, rwf2, rwf_fp2 = _run_review(tmp_path, state2, _ok("APPROVED"), rwf_fp_return=_ok("CHANGES_REQUESTED"))
    # First-pass CHANGES_REQUESTED → no promotion → frontier not called.
    assert res2["status"] == "changes_requested"
    rwf2.assert_not_called()
    assert not res2.get("staging_audit"), "no staging_audit when not promoted"


# ---------------------------------------------------------------------------
# review_with_fallback_for_provider shares the ladder (D6)
# ---------------------------------------------------------------------------


def _state(*, implementer="claude-sonnet-4-6", reviewer="codex", fallback="manual"):
    return {"models": {"implementer": implementer, "reviewer": reviewer, "reviewer_fallback": fallback}}


def test_for_provider_valid_primary_returned_unchanged(monkeypatch) -> None:
    """review_with_fallback_for_provider returns a valid primary result as-is."""
    primary = _fake_adapter("APPROVED")
    monkeypatch.setattr(adapters, "get_reviewer_adapter_by_provider", lambda n: primary)
    result = adapters.review_with_fallback_for_provider(
        _state(), role="review_code", prompt="x", cwd=Path("."), target=_TARGET, primary_provider="codex"
    )
    assert result["parse_status"] == "ok" and result["decision"] == "APPROVED"


def test_for_provider_infra_failure_falls_back_to_manual(monkeypatch) -> None:
    """On primary INFRA failure with fallback=manual, result is manual_required."""
    primary = _fake_adapter("MISSING", parse_status="error")
    monkeypatch.setattr(adapters, "get_reviewer_adapter_by_provider", lambda n: primary)
    result = adapters.review_with_fallback_for_provider(
        _state(fallback="manual"),
        role="review_code",
        prompt="x",
        cwd=Path("."),
        target=_TARGET,
        primary_provider="codex",
    )
    assert result["parse_status"] == adapters.MANUAL_REQUIRED


def test_for_provider_same_provider_fallback_blocked(monkeypatch) -> None:
    """fallback same as worker provider → self-review → manual_required."""
    primary = _fake_adapter("MISSING", parse_status="error")
    fb = _fake_adapter("APPROVED")
    monkeypatch.setattr(adapters, "get_reviewer_adapter_by_provider", lambda n: primary)
    monkeypatch.setattr(adapters, "_REVIEWER_ADAPTERS", {"codex": lambda: fb, "claude": lambda: fb})
    result = adapters.review_with_fallback_for_provider(
        _state(implementer="claude-sonnet-4-6", fallback="claude"),
        role="review_code",
        prompt="x",
        cwd=Path("."),
        target=_TARGET,
        primary_provider="codex",
    )
    assert result["parse_status"] == adapters.MANUAL_REQUIRED
    fb.review.assert_not_called()


def test_for_provider_non_read_only_fallback_blocked(monkeypatch) -> None:
    """Non-read-only fallback → manual_required."""
    primary = _fake_adapter("MISSING", parse_status="error")
    fb = _fake_adapter("APPROVED", read_only=False)
    monkeypatch.setattr(adapters, "get_reviewer_adapter_by_provider", lambda n: primary)
    monkeypatch.setattr(adapters, "_REVIEWER_ADAPTERS", {"codex": lambda: fb, "claude": lambda: fb})
    result = adapters.review_with_fallback_for_provider(
        _state(implementer="claude-sonnet-4-6", fallback="codex"),
        role="review_code",
        prompt="x",
        cwd=Path("."),
        target=_TARGET,
        primary_provider="claude",
    )
    assert result["parse_status"] == adapters.MANUAL_REQUIRED


def test_for_provider_unknown_fallback_blocked(monkeypatch) -> None:
    """Fallback not in registry → manual_required."""
    primary = _fake_adapter("MISSING", parse_status="error")
    monkeypatch.setattr(adapters, "get_reviewer_adapter_by_provider", lambda n: primary)
    monkeypatch.setattr(adapters, "_REVIEWER_ADAPTERS", {})
    result = adapters.review_with_fallback_for_provider(
        _state(fallback="codex"),
        role="review_code",
        prompt="x",
        cwd=Path("."),
        target=_TARGET,
        primary_provider="claude",
    )
    assert result["parse_status"] == adapters.MANUAL_REQUIRED


# ---------------------------------------------------------------------------
# Unknown first-pass provider fails MANUAL_REQUIRED (D6)
# ---------------------------------------------------------------------------


def test_unknown_first_pass_provider_returns_manual_required() -> None:
    """get_reviewer_adapter_by_provider returns None → MANUAL_REQUIRED naming mismatch."""
    result = adapters.review_with_fallback_for_provider(
        _state(),
        role="review_code",
        prompt="x",
        cwd=Path("."),
        target=_TARGET,
        primary_provider="nonexistent_provider",
    )
    assert result["parse_status"] == adapters.MANUAL_REQUIRED
    assert "nonexistent_provider" in result["raw"]


def test_get_reviewer_adapter_by_provider_returns_none_for_unknown() -> None:
    assert adapters.get_reviewer_adapter_by_provider("nonexistent") is None


def test_get_reviewer_adapter_by_provider_returns_adapter_for_codex() -> None:
    adapter = adapters.get_reviewer_adapter_by_provider("codex")
    assert adapter is not None


# ---------------------------------------------------------------------------
# D8 — first-pass same-provider guard
# ---------------------------------------------------------------------------


def _load_orch():
    import _engine

    return _engine.orchestrator()


def _seed_task(tmp_path: Path, state: dict) -> Path:
    task_dir = tmp_path / "batch" / "tasks" / "task-001"
    task_dir.mkdir(parents=True)
    (task_dir / "input.md").write_text("brief", encoding="utf-8")
    (task_dir / "outcome.md").write_text("plan", encoding="utf-8")
    (task_dir / "outcome.approved").write_text("", encoding="utf-8")
    (task_dir / "state.json").write_text(json.dumps(state), encoding="utf-8")
    return task_dir


def test_d8_first_pass_same_provider_fails_closed(tmp_path: Path) -> None:
    """worker=claude + first_pass_reviewer=claude → D8 fires (frontier reviewer is codex)."""
    _staged_config(tmp_path, first_pass="claude", escalate_after=2)
    orch = _load_orch()
    state = {
        "mode": "agent-pair",
        "models": {"implementer": "claude-sonnet-4-6", "reviewer": "codex"},
    }
    # Directly test _adversarial_pairing_error with repo_root patched
    original_repo_root = orch.repo_root
    orch.repo_root = lambda: tmp_path
    try:
        err = orch._adversarial_pairing_error(state)
    finally:
        orch.repo_root = original_repo_root
    assert err is not None
    assert "first-pass" in err or "first_pass" in err
    assert "self-review" in err or "collapsed" in err


def test_d8_codex_worker_codex_first_pass_fails_closed(tmp_path: Path) -> None:
    """worker=codex + first_pass_reviewer=codex → D8 fires."""
    _staged_config(tmp_path, first_pass="codex", escalate_after=2)
    orch = _load_orch()
    state = {
        "mode": "agent-pair",
        "models": {"implementer": "codex", "reviewer": "claude"},
    }
    original_repo_root = orch.repo_root
    orch.repo_root = lambda: tmp_path
    try:
        err = orch._adversarial_pairing_error(state)
    finally:
        orch.repo_root = original_repo_root
    assert err is not None


def test_d8_cross_provider_staging_passes_guard(tmp_path: Path) -> None:
    """worker=claude, first_pass=codex, frontier=codex → guard returns None."""
    _staged_config(tmp_path, first_pass="codex", escalate_after=2)
    orch = _load_orch()
    state = {
        "mode": "agent-pair",
        "models": {"implementer": "claude-sonnet-4-6", "reviewer": "codex"},
    }
    original_repo_root = orch.repo_root
    orch.repo_root = lambda: tmp_path
    try:
        err = orch._adversarial_pairing_error(state)
    finally:
        orch.repo_root = original_repo_root
    assert err is None


def test_d8_guard_does_not_fire_when_staging_off(tmp_path: Path) -> None:
    """With review_stages=None (no config.toml), D8 does not change behavior."""
    # No config.toml → review_stages is None
    orch = _load_orch()
    state = {
        "mode": "agent-pair",
        "models": {"implementer": "claude-sonnet-4-6", "reviewer": "codex"},
    }
    original_repo_root = orch.repo_root
    orch.repo_root = lambda: tmp_path
    try:
        err = orch._adversarial_pairing_error(state)
    finally:
        orch.repo_root = original_repo_root
    assert err is None


# ---------------------------------------------------------------------------
# review_audit receives the promotion (orchestrator wiring)
# ---------------------------------------------------------------------------


def _task_state_for_orch(tmp_path: Path, staged: bool = False) -> Path:
    """Create a minimal task + config for orchestrator integration tests."""
    if staged:
        _staged_config(tmp_path, first_pass="codex", escalate_after=2)
    else:
        _write_config(tmp_path)
    task_dir = tmp_path / "batch" / "tasks" / "task-001"
    task_dir.mkdir(parents=True)
    (task_dir / "input.md").write_text("do it", encoding="utf-8")
    state = {
        "task_id": "task-001",
        "mode": "agent-pair",
        "phase": "created",
        "phases_completed": ["plan_outcome"],
        "next_phase": "plan_review",
        "models": {"implementer": "claude-sonnet-4-6", "reviewer": "codex", "reviewer_fallback": "manual"},
        "retries": {},
        "max_retries_per_phase": 2,
        "verification": {"verify_command": "x", "commands": ["x"]},
        "escape": {"ask_user": False, "reason": None, "return_phase": None},
    }
    (task_dir / "state.json").write_text(json.dumps(state), encoding="utf-8")
    return task_dir


def test_review_audit_receives_staging_audit(monkeypatch, tmp_path: Path) -> None:
    """After a promoted round the orchestrator's review_audit list contains an
    entry with phase='review_code' and the staging_audit reason string."""
    orch = _load_orch()
    task_dir = _task_state_for_orch(tmp_path, staged=True)
    monkeypatch.setattr(orch, "repo_root", lambda: tmp_path)
    monkeypatch.setattr(orch, "_ensure_task_branch", lambda *a, **k: "redteam/task-001")
    staging_reason = "round 1: first-pass 'codex' approved; promoted to frontier"
    monkeypatch.setitem(
        orch.PHASE_RUNNERS,
        "plan_review",
        lambda td, st: {
            "status": "changes_requested",
            "feedback": "PR-001 severity:major status:open — fix it\nREVIEW_DECISION: CHANGES_REQUESTED",
            "log": "PR-001 severity:major status:open — fix it\nREVIEW_DECISION: CHANGES_REQUESTED",
            "diff": "",
            "staging_audit": staging_reason,
        },
    )
    monkeypatch.setitem(
        orch.PHASE_RUNNERS,
        "plan_outcome",
        lambda td, st: {"status": "ask_user", "feedback": "", "log": "", "diff": ""},
    )

    orch.process_task(task_dir)

    st = json.loads((task_dir / "state.json").read_text(encoding="utf-8"))
    audit = st.get("review_audit", [])
    assert any(a.get("phase") == "plan_review" and a.get("reason") == staging_reason for a in audit), (
        f"staging_audit not in review_audit: {audit}"
    )


# ---------------------------------------------------------------------------
# First-pass artifact rotation (_clear_manual_phase_artifacts)
# ---------------------------------------------------------------------------


def test_first_pass_artifact_rotation_two_promoted_rounds(tmp_path: Path) -> None:
    """After 2 promoted rounds _clear_manual_phase_artifacts accumulates
    code_review.first_pass.round1.md and the latest stays at code_review.first_pass.md."""
    orch = _load_orch()
    task_dir = tmp_path / "t"
    task_dir.mkdir()
    cr = task_dir / "code_review.md"
    fp = task_dir / "code_review.first_pass.md"

    # Simulate round 1: write both artifacts, then clear before round 2.
    cr.write_text("frontier round-1", encoding="utf-8")
    fp.write_text("first-pass round-1", encoding="utf-8")
    orch._clear_manual_phase_artifacts(task_dir, "review_code")
    assert not cr.exists()
    assert not fp.exists()
    assert (task_dir / "code_review.round1.md").read_text(encoding="utf-8") == "frontier round-1"
    assert (task_dir / "code_review.first_pass.round1.md").read_text(encoding="utf-8") == "first-pass round-1"

    # Simulate round 2: write fresh artifacts.
    cr.write_text("frontier round-2", encoding="utf-8")
    fp.write_text("first-pass round-2", encoding="utf-8")
    # Round-2 archive still lives at the canonical name.
    assert cr.read_text(encoding="utf-8") == "frontier round-2"
    assert fp.read_text(encoding="utf-8") == "first-pass round-2"


def test_first_pass_rotation_noop_when_absent(tmp_path: Path) -> None:
    """_clear_manual_phase_artifacts is a no-op for first_pass.md when absent."""
    orch = _load_orch()
    task_dir = tmp_path / "t"
    task_dir.mkdir()
    # Only code_review.md present, no first_pass artifact.
    (task_dir / "code_review.md").write_text("round-1", encoding="utf-8")
    orch._clear_manual_phase_artifacts(task_dir, "review_code")
    assert not (task_dir / "code_review.first_pass.round1.md").exists()


# ---------------------------------------------------------------------------
# plan_review not routed through staging
# ---------------------------------------------------------------------------


def test_plan_review_not_staged(tmp_path: Path) -> None:
    """plan_review.run continues to call review_with_fallback (not _for_provider)
    regardless of review_stages config — staging scope is review_code only."""
    import phase_runners.plan_review as plan_review

    with (
        patch("phase_runners.plan_review.get_reviewer_adapter", return_value=MagicMock()),
        patch("phase_runners.plan_review.review_with_fallback", return_value=_ok("CHANGES_REQUESTED")) as rwf,
        patch("phase_runners.plan_review.compute_repo_diff", return_value=""),
        patch("phase_runners.plan_review.repo_root", return_value=tmp_path),
    ):
        # plan_review module does not import review_with_fallback_for_provider;
        # verify the call goes to review_with_fallback.
        state = {"mode": "agent-pair", "models": {"reviewer": "codex"}}
        plan_review.run(tmp_path, state)

    rwf.assert_called_once()
    # Confirm review_with_fallback_for_provider is not even imported in plan_review.
    import importlib

    plan_review_src = Path(
        importlib.util.find_spec("phase_runners.plan_review").origin  # type: ignore[union-attr]
    ).read_text(encoding="utf-8")
    assert "review_with_fallback_for_provider" not in plan_review_src


# ---------------------------------------------------------------------------
# Dogfood-config assertion (Done-when)
# ---------------------------------------------------------------------------


def test_dogfood_config_review_stages_is_none() -> None:
    """This repo's own .redteam/config.toml must have review_stages=None
    (staging is opt-in and not enabled in the harness's own config)."""
    repo_root = Path(__file__).resolve().parents[2]
    cfg = load_config(repo_root)
    assert cfg.models.review_stages is None, (
        "This repo's config.toml must NOT enable [models.review_stages]; "
        "staging is opt-in and must remain off in the dogfood config."
    )
