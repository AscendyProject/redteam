"""Agent-pair plan review.

If a headless reviewer adapter is configured (`state.models.reviewer`), invoke
it to produce `plan_review.md` synchronously. Otherwise fall back to the legacy
manual flow (a human pastes the Codex review + touches the sentinel) and just
parse the existing file.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from adapters import MANUAL_REQUIRED, get_reviewer_adapter, review_with_fallback

from ._base import PhaseResult, compute_repo_diff, parse_review_decision, read_text_if_exists, repo_root


def _plan_review_prompt(task_dir: Path) -> str:
    # Headless-specific: the reviewer runs in a read-only sandbox, so it must
    # NOT write files or touch sentinels (the manual prompt asks for that).
    return (
        f"Act as an adversarial plan reviewer for the task at {task_dir}/. "
        f"Inputs: {task_dir}/input.md, {task_dir}/outcome.md, {task_dir}/state.json. "
        f"Apply the review criteria described in .redteam/prompts/codex/plan_review.md, but DO NOT "
        f"write any files or touch any sentinels — output the ENTIRE review to stdout only. End with a "
        f"final line `REVIEW_DECISION: APPROVED` (or CHANGES_REQUESTED / RESCUE_REQUIRED / ASK_USER), "
        f"with PR-NNN findings above it."
    )


def run(task_dir: Path, state: dict[str, Any]) -> PhaseResult:
    diff = compute_repo_diff(cwd=repo_root())
    review_path = task_dir / "plan_review.md"

    # When a prior fallback exhausted to manual for THIS phase, the orchestrator
    # flags it and waits on the pasted-review sentinel; take the manual branch
    # rather than re-invoking the failing headless primary (#37).
    manual_required = "plan_review" in (state.get("manual_review_required") or {})
    adapter = get_reviewer_adapter(state)
    if adapter is not None and not manual_required:
        result = review_with_fallback(
            state,
            role="plan_review",
            prompt=_plan_review_prompt(task_dir),
            cwd=repo_root(),
            target={"kind": "plan", "base": None},
        )
        # The reviewer (primary or fallback) failed infra → block for a manual
        # review. The audit is NOT persisted as plan_review.md (it is not a review).
        if result["parse_status"] == MANUAL_REQUIRED:
            return PhaseResult(status="manual_required", feedback=result["raw"], log=result["raw"], diff=diff)
        review_text = result["raw"]
        review_path.write_text(review_text, encoding="utf-8")
        # Fail closed on ANY non-ok parse status; trust the adapter's decision
        # rather than re-parsing the raw text (an unparseable result must not be
        # rescued by a stray REVIEW_DECISION line in the body).
        if result["parse_status"] != "ok":
            feedback = f"reviewer returned parse_status={result['parse_status']}\n\n{review_text[-2000:]}"
            return PhaseResult(status="error", feedback=feedback, log=review_text, diff=diff)
        decision = result["decision"]
        fallback_audit = result.get("fallback_audit")  # structured provenance, not text
    else:
        review_text = read_text_if_exists(review_path)
        if review_text is None:
            feedback = f"plan_review.md was not produced at {review_path}"
            return PhaseResult(status="error", feedback=feedback, log=feedback, diff=diff)
        decision = parse_review_decision(review_text)
        fallback_audit = None

    def _emit(status: str, feedback: str) -> PhaseResult:
        res = PhaseResult(status=status, feedback=feedback, log=review_text, diff=diff)
        if fallback_audit:
            res["fallback_audit"] = fallback_audit
        return res

    if decision == "APPROVED":
        return _emit("approved", "")
    if decision == "CHANGES_REQUESTED":
        return _emit("changes_requested", review_text)
    if decision == "RESCUE_REQUIRED":
        return _emit("rescue_required", review_text)
    if decision == "ASK_USER":
        return _emit("ask_user", review_text)

    feedback = (
        "plan_review.md is missing a final valid `REVIEW_DECISION:` line.\n"
        "Allowed: APPROVED, CHANGES_REQUESTED, RESCUE_REQUIRED, ASK_USER.\n\n"
        f"Last 30 lines:\n{chr(10).join(review_text.splitlines()[-30:])}"
    )
    return PhaseResult(status="error", feedback=feedback, log=feedback, diff=diff)
