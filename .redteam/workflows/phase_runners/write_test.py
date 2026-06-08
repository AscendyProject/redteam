"""Phase 3 — invoke the test-author sub-agent (after outcome.md is approved)."""

from __future__ import annotations

import fnmatch
import subprocess
from pathlib import Path
from posixpath import basename
from typing import Any

from adapters import get_worker_adapter

from ._base import (
    PhaseResult,
    build_prompt_with_feedback,
    compute_repo_diff,
    project_config,
    repo_root,
)


AGENT_NAME = "test-author"


def _new_test_files_under_tests(rr: Path, test_dir: str, test_file_glob: str) -> list[str]:
    """Return paths of untracked test files newly created under the project's
    test dir, matching the project's test-file glob.

    The test-author writes the new test file at the canonical location declared
    in outcome.md rather than under `<task_dir>/`, so we look for fresh untracked
    files under `test_dir` whose basename matches `test_file_glob` (e.g.
    `test_*.py` for pytest, `*.test.ts` / `*.spec.ts` for a JS project).
    """
    proc = subprocess.run(
        ["git", "ls-files", "--others", "--exclude-standard", test_dir],
        cwd=str(rr),
        capture_output=True,
        text=True,
        check=False,
    )
    return [line for line in proc.stdout.splitlines() if fnmatch.fnmatch(basename(line), test_file_glob)]


def run(task_dir: Path, state: dict[str, Any]) -> PhaseResult:
    proj = project_config()
    base = (
        f"Write red-phase tests for the task at: {task_dir}\n"
        f"Inputs: {task_dir}/outcome.md, and the project's test conventions at "
        f"{proj.test_conventions_file}.\n"
        f"Output: the new test file at the canonical location declared in "
        f"outcome.md's Affected files (under `{proj.test_dir}`, named to match the project's "
        f"test-file pattern `{proj.test_file_glob}`). Do NOT create files under "
        f"`<task_dir>/`; the test runner discovers tests from `{proj.test_dir}`.\n"
        "Follow your agent definition exactly — every test must currently fail "
        "and must docstring-cite the Done-when item it covers."
    )
    prompt = build_prompt_with_feedback(base, state.get("last_failure_log"))

    rr = repo_root()
    result = get_worker_adapter(state).invoke(role="planner", agent=AGENT_NAME, prompt=prompt, cwd=rr)
    diff = compute_repo_diff(cwd=rr)

    new_tests = _new_test_files_under_tests(rr, proj.test_dir, proj.test_file_glob)

    if result["returncode"] == 0 and new_tests:
        return PhaseResult(
            status="approved",
            feedback="",
            log=result["stdout"] + "\n--- new test files under tests/ ---\n" + "\n".join(new_tests),
            diff=diff,
        )

    feedback = (
        f"No new test files appeared under `{proj.test_dir}` after the test-author phase. "
        f"Expected at least one untracked file matching `{proj.test_file_glob}` at the "
        "canonical path declared in outcome.md's Affected files.\n"
        f"returncode={result['returncode']}\n"
        f"stderr (truncated):\n{result['stderr'][:2000]}"
    )
    return PhaseResult(status="error", feedback=feedback, log=feedback, diff=diff)
