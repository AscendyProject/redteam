"""Phase 6 — invoke the code-security-reviewer (fresh reviewer)."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

from adapters import MANUAL_REQUIRED, get_reviewer_adapter, get_worker_adapter, review_with_fallback

from ._base import (
    PhaseResult,
    compute_repo_diff,
    git_rev_parse,
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


def _is_ancestor(prior: str, repo: Path) -> bool:
    """Return True if *prior* is an ancestor of HEAD (git merge-base exit 0).

    Shell-free, encoding="utf-8".  Never raises — any error → False so the
    caller transparently falls back to the full-diff path.
    """
    try:
        proc = subprocess.run(
            ["git", "merge-base", "--is-ancestor", prior, "HEAD"],
            cwd=str(repo),
            capture_output=True,
            encoding="utf-8",
            check=False,
        )
        return proc.returncode == 0
    except Exception:
        return False


def _incremental_diff_nonempty(prior: str, repo: Path) -> bool:
    """Return True if `git diff <prior>...HEAD` exits 0 with non-empty stdout.

    Shell-free, encoding="utf-8".  Never raises — any error → False.
    """
    try:
        proc = subprocess.run(
            ["git", "diff", f"{prior}...HEAD"],
            cwd=str(repo),
            capture_output=True,
            encoding="utf-8",
            check=False,
        )
        return proc.returncode == 0 and bool(proc.stdout)
    except Exception:
        return False


def _narrowed_code_review_prompt(
    task_dir: Path,
    base_branch: str,
    prior_rev: str,
    open_items: list[dict],
) -> str:
    """Build the narrowed prompt for round-over-round incremental review.

    The reviewer receives only the delta since the previously-reviewed
    revision, plus the carried-over open findings to adjudicate.  The
    pinned base stays in the prompt as context so the reviewer knows the
    PR scope.  Output is byte-deterministic for a given input.
    """
    proj = project_config()
    parts = [
        f"Act as an adversarial code-security reviewer for the implementation of the task at "
        f"{task_dir}/. Review `git diff {prior_rev}...HEAD`.",
        f"Pinned base for the PR remains {base_branch}; the narrowed diff above is the "
        f"round-over-round delta, not a replacement for the base.",
        "",
        "Carried-over open findings (adjudicate each as resolved or still open):",
    ]
    for item in open_items:
        parts.append(
            f"- {item.get('id')} severity:{item.get('severity')} status:{item.get('status')} — {item.get('summary')}"
        )
    parts += [
        "",
        f"Inputs: {task_dir}/outcome.md, {task_dir}/plan_review.md, {task_dir}/impl_diff.patch. "
        f"Apply the review criteria described in .redteam/prompts/codex/code_review.md, the project "
        f"security checklist at {proj.security_checklist}, and the project hard rules at "
        f"{proj.context_file}, but DO NOT write any files or touch any sentinels — output the "
        f"ENTIRE review to stdout only. End with a final line `REVIEW_DECISION: APPROVED` (or "
        f"CHANGES_REQUESTED / RESCUE_REQUIRED / ASK_USER), with IR-NNN findings above it.",
    ]
    return "\n".join(parts)


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
            # Choose narrowed vs full prompt.  Narrowing is allowed only when ALL
            # four preconditions pass; any failure falls back to the full-diff
            # path (fail toward MORE review, not less).
            prior_rev = state.get("last_reviewed_rev")
            open_items = [
                i for i in (state.get("review_items") or []) if isinstance(i, dict) and i.get("status") == "open"
            ]
            use_narrowed = bool(
                isinstance(prior_rev, str)
                and prior_rev
                and open_items
                and _is_ancestor(prior_rev, rr)
                and _incremental_diff_nonempty(prior_rev, rr)
            )
            if use_narrowed:
                prompt = _narrowed_code_review_prompt(task_dir, base_branch, prior_rev, open_items)
            else:
                prompt = _code_review_prompt(task_dir, base_branch)
            result = review_with_fallback(
                state,
                role="review_code",
                prompt=prompt,
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
            # Capture the reviewed HEAD revision after a successful parsed round.
            # MUST NOT be set on the MANUAL_REQUIRED or parse_status != "ok" paths
            # above.  On RuntimeError (not a git repo / git unavailable) leave
            # last_reviewed_rev untouched so the next round takes the full-diff path.
            if decision in {"APPROVED", "CHANGES_REQUESTED", "RESCUE_REQUIRED", "ASK_USER"}:
                try:
                    state["last_reviewed_rev"] = git_rev_parse("HEAD", rr)
                except RuntimeError:
                    pass
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
