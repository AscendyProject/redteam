"""Phase 4 — invoke the test-verifier (fresh reviewer) and parse REVIEW_DECISION."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from adapters import get_worker_adapter, worker_provider

from ._base import (
    PhaseResult,
    compute_repo_diff,
    parse_review_decision,
    project_config,
    read_text_if_exists,
    repo_root,
    valid_tdd_test_files,
)


AGENT_NAME = "test-verifier"


def run(task_dir: Path, state: dict[str, Any]) -> PhaseResult:
    proj = project_config()
    # write_test commits the test(s) and records them in the validated `tdd_test_files`
    # manifest (#82), so the worktree is clean here — name the committed paths
    # explicitly rather than inferring identity from `git status` (which is now empty).
    try:
        manifest = valid_tdd_test_files(state.get("tdd_test_files"), proj)
    except ValueError as exc:
        msg = f"invalid tdd_test_files manifest in state: {exc}"
        return PhaseResult(status="error", feedback=msg, log=msg, diff=compute_repo_diff(cwd=repo_root()))
    if not manifest:
        # Fail closed: no heuristic fallback. A legacy in-flight tdd task predating the
        # commit-discipline change has no manifest — it must be restarted.
        msg = (
            "tdd_test_files manifest is missing/empty — write_test did not record the test files. "
            "If this is a tdd task started before the commit-discipline change (#82), restart it."
        )
        return PhaseResult(status="error", feedback=msg, log=msg, diff=compute_repo_diff(cwd=repo_root()))

    # The verifier is a fresh reviewer; it does not see prior failure feedback.
    prompt = (
        f"Review the test file(s) the test-author committed for this task — "
        f"{', '.join(manifest)} — against {task_dir}/outcome.md, applying the "
        f"project's test conventions at {proj.test_conventions_file}.\n"
        f"Produce {task_dir}/test_review.md ending with `REVIEW_DECISION: APPROVED` or "
        "`REVIEW_DECISION: CHANGES_REQUESTED`. Follow your agent definition exactly."
    )

    rr = repo_root()
    result = get_worker_adapter(state).invoke(role="reviewer", agent=AGENT_NAME, prompt=prompt, cwd=rr)
    diff = compute_repo_diff(cwd=rr)
    _tele = dict(
        cost_usd=result.get("cost_usd"),
        duration_sec=result.get("duration_sec"),
        model=result.get("model"),
        provider=worker_provider(state),
    )

    review_path = task_dir / "test_review.md"
    review_text = read_text_if_exists(review_path)
    if review_text is None:
        feedback = (
            f"test_review.md was not produced.\n"
            f"returncode={result['returncode']}\n"
            f"stderr (truncated):\n{result['stderr'][:2000]}"
        )
        return PhaseResult(status="error", feedback=feedback, log=feedback, diff=diff, **_tele)

    decision = parse_review_decision(review_text)
    if decision == "APPROVED":
        return PhaseResult(status="approved", feedback="", log=review_text, diff=diff, **_tele)
    if decision == "CHANGES_REQUESTED":
        return PhaseResult(
            status="changes_requested",
            feedback=review_text,
            log=review_text,
            diff=diff,
            **_tele,
        )
    feedback = (
        "test_review.md is missing a final `REVIEW_DECISION:` line. "
        "The verifier output is malformed.\n\n"
        f"Last 30 lines:\n{chr(10).join(review_text.splitlines()[-30:])}"
    )
    return PhaseResult(status="error", feedback=feedback, log=feedback, diff=diff, **_tele)
