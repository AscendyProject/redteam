"""Phase 6 — invoke the code-security-reviewer (fresh reviewer)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from adapters import get_reviewer_adapter, get_worker_adapter

from ._base import (
    PhaseResult,
    compute_repo_diff,
    parse_review_decision,
    project_config,
    read_text_if_exists,
    repo_root,
)


AGENT_NAME = "code-security-reviewer"


def _code_review_prompt(task_dir: Path) -> str:
    # Headless-specific: read-only sandbox, so output to stdout only — do not
    # write review files or touch sentinels.
    proj = project_config()
    return (
        f"Act as an adversarial code-security reviewer for the implementation of the task at "
        f"{task_dir}/. Review `git diff {proj.base_branch}...HEAD`. Inputs: {task_dir}/outcome.md, "
        f"{task_dir}/plan_review.md, {task_dir}/impl_diff.patch, and the git diff. Apply the review "
        f"criteria described in .redteam/prompts/codex/code_review.md, the project security checklist "
        f"at {proj.security_checklist}, and the project hard rules at {proj.context_file}, but DO NOT "
        f"write any files or touch any sentinels — output the ENTIRE review to stdout only. End with a "
        f"final line `REVIEW_DECISION: APPROVED` (or CHANGES_REQUESTED / RESCUE_REQUIRED / ASK_USER), "
        f"with IR-NNN findings above it."
    )


def run(task_dir: Path, state: dict[str, Any]) -> PhaseResult:
    if state.get("mode") == "agent-pair":
        diff = compute_repo_diff(cwd=repo_root())
        review_path = task_dir / "code_review.md"

        adapter = get_reviewer_adapter(state)
        if adapter is not None:
            result = adapter.review(
                role="review_code",
                prompt=_code_review_prompt(task_dir),
                cwd=repo_root(),
                target={"kind": "branch_diff", "base": project_config().base_branch},
            )
            review_text = result["raw"]
            review_path.write_text(review_text, encoding="utf-8")
            # Fail closed on any non-ok parse status; trust the adapter's decision
            # rather than re-parsing the raw body.
            if result["parse_status"] != "ok":
                feedback = f"reviewer returned parse_status={result['parse_status']}\n\n{review_text[-2000:]}"
                return PhaseResult(status="error", feedback=feedback, log=review_text, diff=diff)
            decision = result["decision"]
        else:
            review_text = read_text_if_exists(review_path)
            if review_text is None:
                feedback = f"code_review.md was not produced at {review_path}"
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
            "code_review.md is missing a final valid `REVIEW_DECISION:` line.\n"
            "Allowed: APPROVED, CHANGES_REQUESTED, RESCUE_REQUIRED, ASK_USER.\n\n"
            f"Last 30 lines:\n{chr(10).join(review_text.splitlines()[-30:])}"
        )
        return PhaseResult(status="error", feedback=feedback, log=feedback, diff=diff)

    # Like the test verifier, this is a fresh reviewer; no prior feedback is forwarded.
    proj = project_config()
    prompt = (
        f"Review `git diff {proj.base_branch}...HEAD` for the implementation of the task at: {task_dir}\n"
        f"Apply the project security checklist at {proj.security_checklist}, the project hard "
        f"rules at {proj.context_file}, and {task_dir}/outcome.md.\n"
        f"Produce {task_dir}/code_review.md ending with `REVIEW_DECISION: APPROVED` or "
        "`REVIEW_DECISION: CHANGES_REQUESTED`. Follow your agent definition exactly."
    )

    rr = repo_root()
    result = get_worker_adapter(state).invoke(role="reviewer", agent=AGENT_NAME, prompt=prompt, cwd=rr)
    diff = compute_repo_diff(cwd=rr)

    review_path = task_dir / "code_review.md"
    review_text = read_text_if_exists(review_path)
    if review_text is None:
        feedback = (
            f"code_review.md was not produced.\n"
            f"returncode={result['returncode']}\n"
            f"stderr (truncated):\n{result['stderr'][:2000]}"
        )
        return PhaseResult(status="error", feedback=feedback, log=feedback, diff=diff)

    decision = parse_review_decision(review_text)
    if decision == "APPROVED":
        return PhaseResult(status="approved", feedback="", log=review_text, diff=diff)
    if decision == "CHANGES_REQUESTED":
        return PhaseResult(
            status="changes_requested",
            feedback=review_text,
            log=review_text,
            diff=diff,
        )
    feedback = (
        "code_review.md is missing a final `REVIEW_DECISION:` line. "
        "The reviewer output is malformed.\n\n"
        f"Last 30 lines:\n{chr(10).join(review_text.splitlines()[-30:])}"
    )
    return PhaseResult(status="error", feedback=feedback, log=feedback, diff=diff)
