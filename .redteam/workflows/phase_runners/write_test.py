"""Phase 3 — invoke the test-author sub-agent (after outcome.md is approved).

TDD mode only. The test-author writes the new test file at the canonical location
declared in outcome.md (under `test_dir`). This phase then COMMITS the worker's
test(s) to the task branch and persists a `tdd_test_files` manifest (#82), so:
- `verify_test` reviews the committed test via the manifest (not worktree status);
- `implement`'s committed range, `review_code`, and the PR all see the test as a
  real commit — `git diff <base>...HEAD` no longer omits it.

Attribution is strict: only files the worker JUST created (before/after untracked
snapshot), files already recorded as task-owned (`prior_manifest`), or test files
already committed on the task branch (`committed_tests`, the crash/resume recovery)
are committed. It NEVER stages `git diff` unstaged/staged content, so the operator's
stash-popped tracked modifications are never swept into the task commit (#91).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from adapters import get_worker_adapter, worker_provider

from ._base import (
    PhaseResult,
    build_prompt_with_feedback,
    commit_paths,
    compute_branch_diff,
    pinned_base_branch,
    project_config,
    repo_root,
    run_git_checked,
    untracked_files,
    valid_tdd_test_files,
)


AGENT_NAME = "test-author"

# The harness's own write-test commit message; the {task_id} makes it attributable to
# THIS task so crash-recovery never picks up an operator's test commit (#82 IR-007).
_WRITE_TEST_COMMIT_MSG = "test({task_id}): write tests"


def _is_test_path(path: str, proj: Any) -> bool:
    """Discovery uses the EXACT same predicate as manifest validation (#82 IR-006) — a
    path counts as a task test only if `valid_tdd_test_files` would accept it (under the
    normalized test_dir, glob match, no backslash/abs/`..`/control char). Git emits
    POSIX paths, so a normal new test passes; a pathological name is consistently rejected
    by BOTH validation and discovery."""
    try:
        return bool(valid_tdd_test_files([path], proj))
    except ValueError:
        return False


def _committed_test_files(rr: Path, proj: Any, task_id: str, base_branch: str) -> list[str]:
    """Test files committed by THIS task's prior write_test round(s) — recovers a test
    committed before a crash lost the in-memory manifest (#82 IR-004). Scoped to the
    harness's OWN write-test commits (found by their `test(<task_id>): write tests`
    message within base..HEAD), NOT the whole branch range — so on a reused branch an
    operator's own test commit is never attributed to the task (IR-007), and a normal
    first run finds nothing (no such commit yet). `base_branch` is the per-task PINNED
    base (#91), so a test-author that edits config.toml can't move the attribution range."""
    target = _WRITE_TEST_COMMIT_MSG.format(task_id=task_id)
    # Match the commit SUBJECT EXACTLY (not `--grep`, a substring match — an operator
    # commit "...: write tests manually" or one carrying the text in its body would
    # wrongly match, IR-008). Records are framed with `-z` (NUL): a git commit message
    # CANNOT contain NUL, so a crafted subject can't forge a record boundary (IR-009);
    # %x1f splits sha from subject (the 40-hex sha never contains it).
    out = run_git_checked(["log", "-z", "--format=%H%x1f%s", f"{base_branch}..HEAD"], rr).stdout
    shas: list[str] = []
    for rec in out.split("\0"):
        if not rec:
            continue
        sha, _, subject = rec.partition("\x1f")
        if subject == target:
            shas.append(sha)
    files: set[str] = set()
    for sha in shas:
        diff = run_git_checked(["-c", "core.quotepath=false", "diff", "-z", "--name-only", f"{sha}^!"], rr).stdout
        files.update(p for p in diff.split("\0") if p and _is_test_path(p, proj))
    return sorted(files)


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
    try:
        base_branch = pinned_base_branch(state, rr)  # #91: pinned pre-worker, not live config
    except ValueError as exc:
        msg = str(exc)
        return PhaseResult(status="error", feedback=msg, log=msg, diff="")
    # Shape-validate the persisted manifest (path-injection defense, existence-INDEPENDENT
    # so an owned-but-deleted test still validates) BEFORE invoking the worker.
    try:
        prior_manifest = valid_tdd_test_files(state.get("tdd_test_files"), proj)
    except ValueError as exc:
        msg = f"invalid tdd_test_files manifest in state: {exc}"
        return PhaseResult(status="error", feedback=msg, log=msg, diff="")

    # Snapshot untracked BEFORE the worker runs so we attribute ONLY what it creates.
    try:
        before_untracked = untracked_files(rr)
    except (RuntimeError, OSError) as exc:
        fb = f"could not snapshot the working tree before write_test ({exc})."
        return PhaseResult(status="error", feedback=fb, log=fb, diff="")

    task_id = str(state.get("task_id") or task_dir.name)
    result = get_worker_adapter(state).invoke(role="planner", agent=AGENT_NAME, prompt=prompt, cwd=rr)
    _tele = dict(
        cost_usd=result.get("cost_usd"),
        duration_sec=result.get("duration_sec"),
        model=result.get("model"),
        provider=worker_provider(state),
    )

    # Task-owned sources only — NEVER `git diff` unstaged/staged (operator mods).
    # committed_tests is message-scoped to THIS task's prior write-test commits, so it is
    # safe on a first run (finds nothing) AND on a reused branch (ignores operator commits)
    # while still recovering a crash that lost the in-memory manifest (IR-004/IR-007).
    try:
        new_untracked = [p for p in (untracked_files(rr) - before_untracked) if _is_test_path(p, proj)]
        committed_tests = _committed_test_files(rr, proj, task_id, base_branch)
    except (RuntimeError, OSError) as exc:
        fb = f"could not enumerate the task's test files ({exc})."
        return PhaseResult(status="error", feedback=fb, log=fb, diff="", **_tele)
    task_tests = sorted(set(new_untracked) | set(prior_manifest) | set(committed_tests))

    if result["returncode"] != 0 or not task_tests:
        feedback = (
            f"No task-owned test files were found after the test-author phase. "
            f"Expected at least one file matching `{proj.test_file_glob}` under `{proj.test_dir}` "
            f"(the canonical path declared in outcome.md's Affected files).\n"
            f"returncode={result['returncode']}\n"
            f"stderr (truncated):\n{result['stderr'][:2000]}"
        )
        return PhaseResult(
            status="error",
            feedback=feedback,
            log=feedback,
            diff=compute_branch_diff(cwd=rr, base_branch=base_branch),
            **_tele,
        )

    # Reject an owned path that EXISTS as a symlink / non-regular file (the contract is
    # "test files"); a MISSING owned path is allowed (it stages as a deletion below).
    for rel in task_tests:
        p = rr / rel
        if p.is_symlink() or (p.exists() and not p.is_file()):
            fb = f"refusing to stage a non-regular/symlink test path: {rel}"
            return PhaseResult(
                status="error",
                feedback=fb,
                log=fb,
                diff=compute_branch_diff(cwd=rr, base_branch=base_branch),
                **_tele,
            )

    try:
        commit_paths(rr, task_tests, _WRITE_TEST_COMMIT_MSG.format(task_id=task_id))
    except (RuntimeError, OSError) as exc:
        fb = f"could not commit the test files ({exc}); refusing to hand a stale range to review."
        return PhaseResult(status="error", feedback=fb, log=fb, diff="", **_tele)

    # Persist the CURRENT LIVE set: drop any path the worker deleted/renamed away (its
    # deletion is still committed). The manifest is thus always the live test files.
    state["tdd_test_files"] = [rel for rel in task_tests if (rr / rel).is_file()]

    # A committed-inclusive branch diff (not the post-commit-empty working-tree diff)
    # so stall detection across retries stays meaningful.
    return PhaseResult(
        status="approved",
        feedback="",
        log=result["stdout"] + "\n--- task test files ---\n" + "\n".join(task_tests),
        diff=compute_branch_diff(cwd=rr, base_branch=base_branch),
        **_tele,
    )
