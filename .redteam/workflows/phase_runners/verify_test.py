"""Phase 4 — invoke the test-verifier (fresh reviewer) and parse REVIEW_DECISION."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from adapters import get_worker_adapter

from ._base import (
    PhaseResult,
    compute_repo_diff,
    parse_review_decision,
    project_config,
    read_text_if_exists,
    repo_root,
)


AGENT_NAME = "test-verifier"


def run(task_dir: Path, state: dict[str, Any]) -> PhaseResult:
    proj = project_config()
    td = proj.test_dir
    # The verifier is a fresh reviewer; it does not see prior failure feedback.
    prompt = (
        f"Review the new test file(s) under `{td}` (the canonical path from "
        f"outcome.md's Affected files) against {task_dir}/outcome.md, applying the "
        f"project's test conventions at {proj.test_conventions_file}. Use "
        f"`git status --short {td}` to identify which file(s) the test-author added.\n"
        f"Produce {task_dir}/test_review.md ending with `REVIEW_DECISION: APPROVED` or "
        "`REVIEW_DECISION: CHANGES_REQUESTED`. Follow your agent definition exactly."
    )

    rr = repo_root()
    result = get_worker_adapter(state).invoke(role="reviewer", agent=AGENT_NAME, prompt=prompt, cwd=rr)
    diff = compute_repo_diff(cwd=rr)

    review_path = task_dir / "test_review.md"
    review_text = read_text_if_exists(review_path)
    if review_text is None:
        feedback = (
            f"test_review.md was not produced.\n"
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
        "test_review.md is missing a final `REVIEW_DECISION:` line. "
        "The verifier output is malformed.\n\n"
        f"Last 30 lines:\n{chr(10).join(review_text.splitlines()[-30:])}"
    )
    return PhaseResult(status="error", feedback=feedback, log=feedback, diff=diff)
