"""Agent-pair rescue — validate a manually produced Codex rescue report."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ._base import PhaseResult, compute_repo_diff, read_text_if_exists, repo_root


def run(task_dir: Path, state: dict[str, Any]) -> PhaseResult:
    diff = compute_repo_diff(cwd=repo_root())
    report = read_text_if_exists(task_dir / "rescue_report.md")
    if report is None:
        feedback = f"rescue_report.md was not produced at {task_dir / 'rescue_report.md'}"
        return PhaseResult(status="error", feedback=feedback, log=feedback, diff=diff)
    if not (task_dir / "impl_diff.patch").exists():
        feedback = f"impl_diff.patch was not updated at {task_dir / 'impl_diff.patch'}"
        return PhaseResult(status="error", feedback=feedback, log=feedback, diff=diff)
    return PhaseResult(status="approved", feedback="", log=report, diff=diff)
