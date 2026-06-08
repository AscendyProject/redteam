"""Agent-pair plan review.

If a headless reviewer adapter is configured (`state.models.reviewer`), invoke
it to produce `plan_review.md` synchronously. Otherwise fall back to the legacy
manual flow (a human pastes the Codex review + touches the sentinel) and just
parse the existing file.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from adapters import get_reviewer_adapter

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

    adapter = get_reviewer_adapter(state)
    if adapter is not None:
        result = adapter.review(
            role="plan_review",
            prompt=_plan_review_prompt(task_dir),
            cwd=repo_root(),
            target={"kind": "plan", "base": None},
        )
        review_text = result["raw"]
        review_path.write_text(review_text, encoding="utf-8")
        # Fail closed on ANY non-ok parse status; trust the adapter's decision
        # rather than re-parsing the raw text (an unparseable result must not be
        # rescued by a stray REVIEW_DECISION line in the body).
        if result["parse_status"] != "ok":
            feedback = f"reviewer returned parse_status={result['parse_status']}\n\n{review_text[-2000:]}"
            return PhaseResult(status="error", feedback=feedback, log=review_text, diff=diff)
        decision = result["decision"]
    else:
        review_text = read_text_if_exists(review_path)
        if review_text is None:
            feedback = f"plan_review.md was not produced at {review_path}"
            return PhaseResult(status="error", feedback=feedback, log=feedback, diff=diff)
        decision = parse_review_decision(review_text)
    if decision == "APPROVED":
        return PhaseResult(status="approved", feedback="", log=review_text, diff=diff)
    if decision == "CHANGES_REQUESTED":
        return PhaseResult(status="changes_requested", feedback=review_text, log=review_text, diff=diff)
    if decision == "RESCUE_REQUIRED":
        return PhaseResult(status="rescue_required", feedback=review_text, log=review_text, diff=diff)
    if decision == "ASK_USER":
        return PhaseResult(status="ask_user", feedback=review_text, log=review_text, diff=diff)

    feedback = (
        "plan_review.md is missing a final valid `REVIEW_DECISION:` line.\n"
        "Allowed: APPROVED, CHANGES_REQUESTED, RESCUE_REQUIRED, ASK_USER.\n\n"
        f"Last 30 lines:\n{chr(10).join(review_text.splitlines()[-30:])}"
    )
    return PhaseResult(status="error", feedback=feedback, log=feedback, diff=diff)
