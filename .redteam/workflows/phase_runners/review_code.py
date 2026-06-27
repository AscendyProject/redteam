"""Phase 6 — invoke the code-security-reviewer (fresh reviewer)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from adapters import MANUAL_REQUIRED, get_reviewer_adapter, get_worker_adapter, review_with_fallback

from ._base import (
    PhaseResult,
    compute_repo_diff,
    parse_review_decision,
    pinned_base_branch,
    project_config,
    read_text_if_exists,
    repo_root,
)


AGENT_NAME = "code-security-reviewer"


def _code_review_prompt(task_dir: Path, base_branch: str) -> str:
    # Headless-specific: read-only sandbox, so output to stdout only — do not
    # write review files or touch sentinels. `base_branch` is the per-task PINNED
    # base (#91), so a mid-task config edit can't move the reviewed range.
    proj = project_config()
    return (
        f"Act as an adversarial code-security reviewer for the implementation of the task at "
        f"{task_dir}/. Review `git diff {base_branch}...HEAD`. Inputs: {task_dir}/outcome.md, "
        f"{task_dir}/plan_review.md, {task_dir}/impl_diff.patch, and the git diff. Apply the review "
        f"criteria described in .redteam/prompts/codex/code_review.md, the project security checklist "
        f"at {proj.security_checklist}, and the project hard rules at {proj.context_file}, but DO NOT "
        f"write any files or touch any sentinels — output the ENTIRE review to stdout only. End with a "
        f"final line `REVIEW_DECISION: APPROVED` (or CHANGES_REQUESTED / RESCUE_REQUIRED / ASK_USER), "
        f"with IR-NNN findings above it."
    )


def run(task_dir: Path, state: dict[str, Any]) -> PhaseResult:
    if state.get("mode") == "agent-pair":
        rr = repo_root()
        try:
            base_branch = pinned_base_branch(state, rr)  # #91: pinned pre-worker, not live config
        except ValueError as exc:
            msg = str(exc)
            return PhaseResult(status="error", feedback=msg, log=msg, diff=compute_repo_diff(cwd=rr))
        diff = compute_repo_diff(cwd=repo_root())
        review_path = task_dir / "code_review.md"

        # A prior fallback exhausted to manual for THIS phase → take the manual
        # branch and wait on the pasted-review sentinel rather than re-invoking the
        # failing headless primary (#37).
        manual_required = "review_code" in (state.get("manual_review_required") or {})
        adapter = get_reviewer_adapter(state)
        if adapter is not None and not manual_required:
            result = review_with_fallback(
                state,
                role="review_code",
                prompt=_code_review_prompt(task_dir, base_branch),
                cwd=repo_root(),
                target={"kind": "branch_diff", "base": base_branch},
            )
            if result["parse_status"] == MANUAL_REQUIRED:
                return PhaseResult(status="manual_required", feedback=result["raw"], log=result["raw"], diff=diff)
            review_text = result["raw"]
            review_path.write_text(review_text, encoding="utf-8")
            # Fail closed on any non-ok parse status; trust the adapter's decision
            # rather than re-parsing the raw body.
            if result["parse_status"] != "ok":
                feedback = f"reviewer returned parse_status={result['parse_status']}\n\n{review_text[-2000:]}"
                return PhaseResult(status="error", feedback=feedback, log=review_text, diff=diff)
            decision = result["decision"]
            fallback_audit = result.get("fallback_audit")  # structured provenance, not text
        else:
            review_text = read_text_if_exists(review_path)
            if review_text is None:
                feedback = f"code_review.md was not produced at {review_path}"
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
            "code_review.md is missing a final valid `REVIEW_DECISION:` line.\n"
            "Allowed: APPROVED, CHANGES_REQUESTED, RESCUE_REQUIRED, ASK_USER.\n\n"
            f"Last 30 lines:\n{chr(10).join(review_text.splitlines()[-30:])}"
        )
        return PhaseResult(status="error", feedback=feedback, log=feedback, diff=diff)

    # Like the test verifier, this is a fresh reviewer; no prior feedback is forwarded.
    proj = project_config()
    # tdd now truly commits (write_test commits the test, implement commits the rest),
    # so the harness writes `impl_diff.patch` from the COMMITTED range `git diff
    # base...HEAD` — a complete, faithful view of what the PR will contain, incl. new
    # files anywhere (not just under source/test roots). The reviewer reads that (#82).
    prompt = (
        f"Review the implementation in `{task_dir}/impl_diff.patch` for the task at: {task_dir}\n"
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
