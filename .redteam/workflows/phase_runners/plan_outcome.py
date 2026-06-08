"""Phase 1 — invoke the outcome-planner sub-agent."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from adapters import get_worker_adapter

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
    base = (
        f"Plan outcome.md for the task at: {task_dir}\n"
        f"Read the brief at {task_dir}/input.md and produce {task_dir}/outcome.md.\n"
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

    outcome_path = task_dir / "outcome.md"
    if result["returncode"] == 0 and outcome_path.exists() and outcome_path.stat().st_size > 0:
        return PhaseResult(status="approved", feedback="", log=result["stdout"], diff=diff)

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
    )
