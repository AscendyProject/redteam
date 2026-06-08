"""Phase 7 — invoke the pr-author. Validates that a draft PR URL was produced."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from adapters import get_worker_adapter

from ._base import (
    PhaseResult,
    compute_repo_diff,
    read_text_if_exists,
    repo_root,
)
from config import load_config


AGENT_NAME = "pr-author"


def _pr_branch(state: dict[str, Any], task_id: str, branch_prefix: str) -> str:
    """Branch the pr-author pushes to. process_task normally saved
    state["branch"] (config-driven, step 2); the prefix-based fallback covers a
    legacy/partial state and is also config-driven (no hardcoded prefix)."""
    return state.get("branch") or f"{branch_prefix}/{task_id}"


def run(task_dir: Path, state: dict[str, Any]) -> PhaseResult:
    task_id = task_dir.name
    rr = repo_root()
    cfg = load_config(rr)
    branch = _pr_branch(state, task_id, cfg.project.branch_prefix)
    proj = cfg.project

    prompt = (
        f"Create the draft PR for task: {task_id}\n"
        f"Inputs: every artifact under {task_dir} (outcome.md, test_review.md, "
        "impl_diff.patch, code_review.md, input.md, state.json) PLUS the new test "
        f"file the test-author created at the canonical path under `{proj.test_dir}` "
        f"(declared in outcome.md's Affected files) and the implementer's changes under "
        f"{', '.join(proj.source_dirs)}.\n"
        f"Push to branch `{branch}` against base branch `{proj.base_branch}` (use "
        f"`gh pr create --base {proj.base_branch}`), write {task_dir}/pr.md, and save the "
        f"PR URL to {task_dir}/pr_url.txt.\n"
        "ALWAYS pass `--draft`. Never `git push --force`. Follow your agent definition "
        "exactly."
    )

    result = get_worker_adapter(state).invoke(role="planner", agent=AGENT_NAME, prompt=prompt, cwd=rr)
    diff = compute_repo_diff(cwd=rr)

    pr_url_text = read_text_if_exists(task_dir / "pr_url.txt")
    pr_md_text = read_text_if_exists(task_dir / "pr.md")

    if (
        result["returncode"] == 0
        and pr_url_text is not None
        and pr_url_text.strip().startswith("https://")
        and pr_md_text is not None
    ):
        # Persist the PR URL into state for downstream visibility.
        state["pr_url"] = pr_url_text.strip()
        state["branch"] = branch
        return PhaseResult(
            status="approved",
            feedback="",
            log=result["stdout"] + f"\nPR URL: {pr_url_text.strip()}",
            diff=diff,
        )

    feedback_lines = [
        "pr-author phase did not produce the expected outputs.",
        f"returncode={result['returncode']}",
        f"pr.md exists: {pr_md_text is not None}",
        f"pr_url.txt content: {pr_url_text!r}",
        f"stderr (truncated): {result['stderr'][:2000]}",
    ]
    feedback = "\n".join(feedback_lines)
    return PhaseResult(status="error", feedback=feedback, log=feedback, diff=diff)
