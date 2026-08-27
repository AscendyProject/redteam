"""Phase 1 — invoke the outcome-planner sub-agent."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from adapters import get_worker_adapter, worker_provider

from ._base import (
    PhaseResult,
    build_prompt_with_feedback,
    compute_repo_diff,
    project_config,
    repo_root,
)


AGENT_NAME = "outcome-planner"


def run(task_dir: Path, state: dict[str, Any]) -> PhaseResult:
    proj = project_config()
    outcome_path = task_dir / "outcome.md"
    # AMEND mode (#183): set by the orchestrator on a plan_review backtrack. The
    # planner previously regenerated outcome.md wholesale, discarding any human
    # edit made between rounds — the documented "fix, then resume" escape — and
    # reopening findings state.json already tracked as resolved, so the loop could
    # not converge once a human co-authored the document. The flag comes from the
    # backtrack rather than from "outcome.md exists", which cannot distinguish a
    # review rejection from a planner error-retry that left a file worth discarding.
    amending = bool(state.get("plan_outcome_amend")) and outcome_path.exists() and outcome_path.stat().st_size > 0

    if amending:
        base = (
            f"AMEND the existing plan at {task_dir}/outcome.md for the task at: {task_dir}\n"
            f"That file already exists and MAY CONTAIN HUMAN EDITS — treat it as the current "
            f"document, not a draft to replace. Do NOT regenerate it from scratch.\n"
            f"Apply ONLY the changes the review findings below require, and leave every other "
            f"section byte-identical. Re-deriving an untouched section risks silently reopening a "
            f"finding that was already resolved.\n"
            f"The brief is at {task_dir}/input.md (context; the plan itself is outcome.md).\n"
        )
    else:
        base = (
            f"Plan outcome.md for the task at: {task_dir}\n"
            f"Read the brief at {task_dir}/input.md and produce {task_dir}/outcome.md.\n"
        )

    base += (
        f"Project context (hard rules + architecture facts): {proj.context_file}.\n"
        f"Source dirs: {', '.join(proj.source_dirs)}. Test dir: {proj.test_dir}. "
        f"New test files must match the pattern `{proj.test_file_glob}`.\n"
        f"The project verify command (use it as the 'Existing' verification hook): "
        f"{proj.verify_command}.\n"
        "Follow your agent definition exactly. Do not modify any code."
    )
    prompt = build_prompt_with_feedback(base, state.get("last_failure_log"))

    rr = repo_root()
    result = get_worker_adapter(state).invoke(role="planner", agent=AGENT_NAME, prompt=prompt, cwd=rr)
    diff = compute_repo_diff(cwd=rr)
    _tele = dict(
        cost_usd=result.get("cost_usd"),
        duration_sec=result.get("duration_sec"),
        model=result.get("model"),
        provider=worker_provider(state),
    )

    outcome_path = task_dir / "outcome.md"
    if result["returncode"] == 0 and outcome_path.exists() and outcome_path.stat().st_size > 0:
        return PhaseResult(status="approved", feedback="", log=result["stdout"], diff=diff, **_tele)

    feedback = (
        f"outcome.md was not produced or is empty.\n"
        f"returncode={result['returncode']}\n"
        f"stderr (truncated):\n{result['stderr'][:2000]}"
    )
    return PhaseResult(
        status="error",
        feedback=feedback,
        log=feedback,
        diff=diff,
        **_tele,
    )
