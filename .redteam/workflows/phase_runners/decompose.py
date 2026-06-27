"""Goal decomposer runner.

Invokes the goal-decomposer worker agent and enforces the three-outcome contract:

  (a) success:          worker exit 0 AND goal.json present on disk.
                        The caller (cmd_decompose) then runs the full brief-
                        completeness gate before and after the review.
  (b) cannot-decompose: worker exit 0 AND goal.json ABSENT AND final stdout line
                        is exactly DECOMPOSE_DECISION: CANNOT_DECOMPOSE AND
                        decompose_blocked.md is present and non-empty.
  (c) anything else:   fail closed — partial writes are left untouched for the
                        operator to inspect; the runner itself writes nothing.

The runner owns the contract; the prompt only echoes it.  Mirrors plan_review.py's
shape; reuses fail-closed semantics.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from adapters import get_worker_adapter

from ._base import incomplete_briefs, repo_root

AGENT_NAME = "goal-decomposer"
CANNOT_DECOMPOSE_MARKER = "DECOMPOSE_DECISION: CANNOT_DECOMPOSE"


def _decomposer_prompt(batch_dir: Path, goal_text: str) -> str:
    """Build the project-agnostic decomposer prompt."""
    return (
        f"You are the goal decomposer for a redteam batch.\n\n"
        f"Batch directory: {batch_dir}\n\n"
        f"Goal document contents:\n\n{goal_text}\n\n"
        f"Follow your agent definition at .claude/agents/goal-decomposer.md exactly.\n"
        f"Write goal.json and tasks/<id>/input.md files under {batch_dir}.\n"
        f"On cannot-decompose: write {batch_dir}/decompose_blocked.md and end stdout with:\n"
        f"{CANNOT_DECOMPOSE_MARKER}"
    )


def run(batch_dir: Path, state: dict[str, Any]) -> dict[str, Any]:
    """Invoke the decomposer worker and enforce the three-outcome contract.

    Returns a dict with:
        status:   "success" | "cannot_decompose" | "error"
        log:      worker stdout (for operator inspection on error)
        message:  human-readable description of the outcome
    """
    goal_path = batch_dir / "goal.md"
    goal_text = goal_path.read_text(encoding="utf-8")

    prompt = _decomposer_prompt(batch_dir, goal_text)
    result = get_worker_adapter(state).invoke(role="planner", agent=AGENT_NAME, prompt=prompt, cwd=repo_root())

    stdout: str = result["stdout"] or ""
    exit_code: int = result["returncode"]
    goal_json_path = batch_dir / "goal.json"
    blocked_path = batch_dir / "decompose_blocked.md"

    goal_json_exists = goal_json_path.is_file()
    stdout_stripped = stdout.strip()
    stdout_lines = [ln.strip() for ln in stdout_stripped.splitlines() if ln.strip()]
    final_stdout_line = stdout_lines[-1] if stdout_lines else ""
    blocked_exists = blocked_path.is_file() and blocked_path.stat().st_size > 0

    # Outcome (b): cannot-decompose — all four conditions must hold
    if exit_code == 0 and not goal_json_exists and final_stdout_line == CANNOT_DECOMPOSE_MARKER and blocked_exists:
        return {
            "status": "cannot_decompose",
            "log": stdout,
            "message": f"decomposer cannot decompose this goal; see {blocked_path}",
        }

    # Outcome (a): success requires exit 0 AND goal.json present AND every task in
    # goal.json backed by a non-empty input.md. A goal.json with missing/empty briefs
    # (or an unparseable goal.json) is a partial write → outcome (c), fail closed.
    # The completeness check is the runner's own contract (not deferred to the caller),
    # so a future direct caller of run() gets the same guarantee.
    if exit_code == 0 and goal_json_exists:
        incomplete = incomplete_briefs(batch_dir)
        if incomplete is None:
            return {
                "status": "error",
                "log": stdout,
                "message": f"decomposer wrote an unparseable goal.json at {goal_json_path}",
            }
        if incomplete:
            return {
                "status": "error",
                "log": stdout,
                "message": (
                    f"decomposer wrote goal.json but task(s) {', '.join(incomplete)} "
                    "have a missing or empty input.md (partial write)"
                ),
            }
        return {
            "status": "success",
            "log": stdout,
            "message": "decomposer completed successfully",
        }

    # Outcome (c): anything else — fail closed
    reason = f"decomposer exited {exit_code} without writing goal.json or emitting {CANNOT_DECOMPOSE_MARKER!r}"
    return {
        "status": "error",
        "log": stdout,
        "message": reason,
    }
