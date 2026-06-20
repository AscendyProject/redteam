"""Phase 5 — invoke the implementer, then independently run verify.sh."""

from __future__ import annotations

import hashlib
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from adapters import get_worker_adapter

from ._base import (
    PhaseResult,
    build_prompt_with_feedback,
    compute_repo_diff,
    project_config,
    validate_verification_commands,
    repo_root,
)


AGENT_NAME = "implementer"


def _run_verify_sh(cwd: Path, argv: list[str]) -> tuple[int, str]:
    # `argv` is the pre-validated verify command, snapshotted BEFORE the
    # implementer runs (IR-001) so a same-round edit to config.toml cannot
    # neuter the gate. Run shell-free.
    proc = subprocess.run(
        argv,
        cwd=str(cwd),
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    combined = (proc.stdout or "") + ("\n" + proc.stderr if proc.stderr else "")
    return proc.returncode, combined


def _run_verification_commands(
    cwd: Path,
    commands: list[str],
    project_verify_command: str | None = None,
    allowlist: list[str] | None = None,
) -> tuple[int, str]:
    if not commands:
        return 2, "No verification commands were snapshotted in state.verification.commands.\n"

    chunks: list[str] = []
    try:
        validated = validate_verification_commands(commands, project_verify_command, allowlist)
    except ValueError as exc:
        return 2, f"{exc}\n"

    for argv in validated:
        chunks.append(f"$ {' '.join(argv)}\n")
        proc = subprocess.run(
            argv,
            cwd=str(cwd),
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
        )
        output = (proc.stdout or "") + (("\n" + proc.stderr) if proc.stderr else "")
        chunks.append(output)
        chunks.append(f"\n[exit {proc.returncode}]\n\n")
        if proc.returncode != 0:
            return proc.returncode, "".join(chunks)
    return 0, "".join(chunks)


def _branch_diff_checked(cwd: Path) -> str:
    """Full branch diff (committed + unstaged + staged), FAIL-CLOSED — raises on a
    git error instead of returning a silently empty/partial diff. impl_diff.patch is
    what the reviewer reads; a partial patch behind a green status would let an
    incomplete change be approved."""
    base_branch = project_config().base_branch
    return "".join(
        _run_git_checked(args, cwd).stdout
        for args in (["diff", f"{base_branch}...HEAD"], ["diff"], ["diff", "--cached"])
    )


def _write_current_diff(task_dir: Path, cwd: Path) -> tuple[str, str]:
    diff = _branch_diff_checked(cwd)
    patch_path = task_dir / "impl_diff.patch"
    patch_path.write_text(diff, encoding="utf-8")
    digest = hashlib.sha256(diff.encode("utf-8")).hexdigest()
    return diff, digest


def _run_git_checked(args: list[str], cwd: Path, *, stdin: str | None = None) -> subprocess.CompletedProcess[str]:
    """Run a git command and FAIL CLOSED on a non-zero exit. stderr is omitted from
    the error (it can carry a credentialed remote URL, IR-002). The commit/stage path
    must never proceed on a silently-failed git op — that would hand review a stale or
    incomplete committed range."""
    proc = subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        input=stdin,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"git invocation failed (exit {proc.returncode})")
    return proc


def _untracked_files(cwd: Path) -> set[str]:
    """Non-ignored untracked paths, NUL-delimited so special-character names are
    exact. Fail-closed (raises) on a git failure."""
    out = _run_git_checked(
        ["-c", "core.quotepath=false", "ls-files", "--others", "--exclude-standard", "-z"], cwd
    ).stdout
    return {p for p in out.split("\0") if p}


def _tracked_changed_paths(cwd: Path) -> list[str]:
    """Tracked paths changed on the branch (committed + unstaged + staged), NUL-safe
    and FAIL-CLOSED — unlike `compute_branch_changed_paths` this raises on a git error
    instead of reading partial stdout, so the staging set can't silently miss a file."""
    base_branch = project_config().base_branch
    seen: set[str] = set()
    paths: list[str] = []
    for args in (
        ["diff", "-z", "--name-only", f"{base_branch}...HEAD"],
        ["diff", "-z", "--name-only"],
        ["diff", "-z", "--name-only", "--cached"],
    ):
        out = _run_git_checked(["-c", "core.quotepath=false", *args], cwd).stdout
        for p in out.split("\0"):
            if p and p not in seen:
                seen.add(p)
                paths.append(p)
    return paths


def _commit_agent_pair_diff(task_dir: Path, state: dict[str, Any], cwd: Path, before_untracked: set[str]) -> None:
    """Commit the implementer's work — tracked changes PLUS files it newly created —
    then refresh impl_diff.patch from the committed range.

    `before_untracked` is the untracked set captured BEFORE the worker ran; `current -
    before` is exactly the files the implementer newly created (a new test / source /
    migration / fixture in any non-ignored location), so a pre-existing untracked file
    the user left in the tree is never swept into the task commit. GITIGNORED files are
    intentionally excluded (both snapshots use `--exclude-standard`, matching the
    integrity gate) — they are not committed, by design. Plain `git diff` (and so the
    old patch-header file list) does NOT include untracked files — without this an
    agent-pair task that creates a new file would leave it uncommitted and the
    integrity gate would reject it. Staged via a NUL-delimited, LITERAL pathspec list
    so a filename with pathspec magic (`:(...)`) or special characters can't match
    unintended files. Fail-closed: any git failure raises (the caller turns it into a
    phase error) rather than proceeding to review on a stale/incomplete range.

    Known limitation: a snapshot diffs by NAME, so a file that was ALREADY untracked
    before the worker ran and is then *modified* in place (not created) is not
    detected — it stays in `before`, so `current - before` excludes it. This is a
    pathological case (the implementer is scoped to outcome.md's Affected files; a
    pre-existing untracked file is the user's own scratch, not task scope) and such a
    change would not be in the PR anyway; closing it would require content-hashing the
    whole untracked set, which is disproportionate here.
    """
    state["implement_round_count"] = int(state.get("implement_round_count") or 0) + 1
    round_n = int(state["implement_round_count"])

    # Exclude the task dir from the staged NEW files: the harness writes its own
    # scratch artifacts there during the run (impl_diff.patch, verification.log, the
    # *_review.md / outcome.md trail) — those are not the implementer's code and the
    # pr-author stages them deliberately at PR time, not the WIP commit.
    try:
        task_rel = task_dir.resolve().relative_to(cwd.resolve()).as_posix()
    except ValueError:
        task_rel = None

    def _in_task_dir(p: str) -> bool:
        return task_rel is not None and (p == task_rel or p.startswith(task_rel + "/"))

    new_untracked = _untracked_files(cwd) - before_untracked
    seen: set[str] = set()
    to_stage: list[str] = []
    for path in _tracked_changed_paths(cwd) + sorted(new_untracked):
        if path not in seen and not _in_task_dir(path):  # filter BOTH tracked + new untracked
            seen.add(path)
            to_stage.append(path)
    if not to_stage:
        return

    _run_git_checked(
        ["--literal-pathspecs", "add", "--pathspec-from-file=-", "--pathspec-file-nul"],
        cwd,
        stdin="\0".join(to_stage),
    )

    cached = subprocess.run(
        ["git", "diff", "--cached", "--quiet"],
        cwd=str(cwd),
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    if cached.returncode == 0:
        return  # nothing actually staged (e.g. all paths already committed this round)
    if cached.returncode != 1:
        # 0 = no diff, 1 = diff present; anything else is a real git error → fail closed.
        raise RuntimeError(f"git diff --cached --quiet failed (exit {cached.returncode})")

    task_id = str(state.get("task_id") or task_dir.name)
    _run_git_checked(["commit", "-m", f"wip({task_id}): implement round {round_n}"], cwd)
    _write_current_diff(task_dir, cwd)


def _uncommitted_scope_files(cwd: Path, proj: Any) -> list[str]:
    """Source/test files still uncommitted AFTER the WIP commit (#50).

    `_commit_agent_pair_diff` stages the implementer's tracked changes PLUS the files
    it newly created, so after it runs the worktree should be clean within scope.
    This is defense-in-depth: if anything source/test still shows uncommitted (a
    failed commit, a file changed after the snapshot), the committed range
    `git diff <base>...HEAD` the reviewer inspects would be STALE relative to the tree
    verification just passed on (verify ran on the dirty worktree). Returns
    the uncommitted *source/test* files across all three states that would diverge
    the committed range from the worktree verification ran on:
      - staged-but-uncommitted (`git diff --cached`): defense-in-depth — the commit
        is fail-closed now, but a hook or partial commit could still leave staged
        changes out of HEAD, so this stays caught here;
      - tracked-but-unstaged modifications (`git diff`);
      - untracked, non-ignored new files (`git ls-files --others`).
    Restricted to the project's source_dirs / test_dir, so harness artifacts
    (impl_diff.patch, verification.log) and gitignored files (e.g. __pycache__,
    *.pyc) never trip it — only real code/test changes. After a SUCCESSFUL commit
    the index equals HEAD, so the `--cached` probe is empty (no false positive).
    Trade-off (#50): the scope is deliberately source/test only — checking ALL paths
    would flag the user's own untracked scratch as "stray" (false positives), the
    exact noise #50 scoping avoids. The residual gap (a commit hook mutating a tracked
    file OUTSIDE source/test after staging) is pathological and accepted, not widened.
    """

    def _names(args: list[str]) -> list[str]:
        proc = subprocess.run(
            ["git", *args],
            cwd=str(cwd),
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        if proc.returncode != 0:
            # Fail closed: a FAILED probe (index lock, repo corruption, bad cwd)
            # must NOT be read as "no stray files = clean", which would hand a
            # possibly-stale committed range to review (#50 review PR-001). stderr is
            # omitted from the message (it can carry secrets, cf. IR-002).
            raise RuntimeError(f"git {' '.join(args)} failed (exit {proc.returncode}) — cannot verify commit integrity")
        return [n for n in (proc.stdout or "").split("\0") if n]

    candidates = (
        _names(["diff", "--cached", "--name-only", "-z"])
        + _names(["diff", "--name-only", "-z"])
        + _names(["ls-files", "--others", "--exclude-standard", "-z"])
    )

    def _root(r: str) -> str:
        r = r.replace("\\", "/")  # normalize roots too, not just candidates (portability)
        return r if r.endswith("/") else r + "/"

    roots = [_root(r) for r in (*proj.source_dirs, proj.test_dir)]
    stray = {path for path in candidates if any(path.replace("\\", "/").startswith(root) for root in roots)}
    return sorted(stray)


def _agent_pair_base_prompt(task_dir: Path, proj: Any) -> str:
    """Agent-pair implement prompt. plan_review.md / code_review.md are OPTIONAL —
    a review=false tier runs plan_outcome→implement with neither — so they are
    named as "if present", never hard-required. The implementer writes the planned
    tests itself (no test-author phase in agent-pair) and must not touch pre-existing
    tests."""
    return (
        f"Mode: agent-pair. Implement the approved plan for the task at: {task_dir}\n"
        f"Inputs: {task_dir}/input.md, {task_dir}/outcome.md, and any "
        f"{task_dir}/plan_review.md and previous {task_dir}/code_review.md that are present "
        "(a lighter tier may run without a plan review).\n"
        f"Respect the project hard rules in {proj.context_file}. Source dirs: "
        f"{', '.join(proj.source_dirs)}; test dir: {proj.test_dir}.\n"
        "In agent-pair mode YOU write the tests the approved plan calls for "
        "(outcome.md's 'Verification hooks > To be created') together with the implementation. "
        "Do not modify, delete, or rename any pre-existing test. "
        "Stay within the approved plan; if the work needs new scope, stop and update outcome.md instead. "
        "Do not create a PR. After editing, stop; the orchestrator will run verification."
    )


def _run_agent_pair(task_dir: Path, state: dict[str, Any]) -> PhaseResult:
    proj = project_config()
    base = _agent_pair_base_prompt(task_dir, proj)
    prompt = build_prompt_with_feedback(base, state.get("last_failure_log"))

    rr = repo_root()
    # Snapshot untracked files BEFORE the worker runs so the commit step can stage
    # exactly what it creates (current − before), never a pre-existing untracked file.
    try:
        before_untracked = _untracked_files(rr)
    except (RuntimeError, OSError) as exc:
        fb = f"could not snapshot the working tree before implement ({exc})."
        return PhaseResult(status="error", feedback=fb, log=fb, diff="")
    result = get_worker_adapter(state).invoke(role="implementer", agent=AGENT_NAME, prompt=prompt, cwd=rr)
    try:
        diff, diff_sha = _write_current_diff(task_dir, rr)
    except (RuntimeError, OSError) as exc:
        fb = f"could not capture the implementation diff ({exc})."
        return PhaseResult(status="error", feedback=fb, log=fb, diff="")

    verification = state.setdefault("verification", {})
    verification["last_diff_sha256"] = diff_sha
    verification["last_output_path"] = "verification.log"

    if result["returncode"] != 0:
        feedback = (
            f"implementer agent exited non-zero.\n"
            f"returncode={result['returncode']}\n"
            f"stderr (truncated):\n{result['stderr'][:2000]}"
        )
        return PhaseResult(status="error", feedback=feedback, log=feedback, diff=diff)

    commands = state.get("verification", {}).get("commands") or []
    if not isinstance(commands, list) or not all(isinstance(command, str) for command in commands):
        commands = []
    # Validate against the PLAN-TIME verify command + allowlist snapshotted
    # before the implementer ran, not the current (possibly mutated) config
    # (IR-001). Legacy in-flight state predating the allowlist snapshot fails
    # closed below rather than silently reading live config.
    snap = state.get("verification", {})
    project_verify_command = snap.get("verify_command")
    verify_allowlist = snap.get("verify_allowlist")
    if project_verify_command is not None and verify_allowlist is None:
        feedback = (
            "legacy state is missing verification.verify_allowlist; re-run planning "
            "to snapshot the verification allowlist."
        )
        return PhaseResult(status="error", feedback=feedback, log=feedback, diff=diff)
    rc, verify_output = _run_verification_commands(rr, commands, project_verify_command, verify_allowlist)

    verification["commands"] = commands
    verification["last_exit_code"] = rc
    verification["last_run_at"] = datetime.now(timezone.utc).isoformat()
    (task_dir / "verification.log").write_text(verify_output, encoding="utf-8")
    try:
        _commit_agent_pair_diff(task_dir, state, rr, before_untracked)
    except (RuntimeError, OSError) as exc:
        fb = f"could not commit the implementer's changes ({exc}); refusing to hand a stale range to review."
        return PhaseResult(status="error", feedback=fb, log=fb, diff="")
    try:
        diff, diff_sha = _write_current_diff(task_dir, rr)
    except (RuntimeError, OSError) as exc:
        fb = f"could not regenerate the review diff after commit ({exc}); refusing to approve on a partial range."
        return PhaseResult(status="error", feedback=fb, log=fb, diff="")
    verification["last_diff_sha256"] = diff_sha

    if rc == 0:
        # Integrity gate (#50): verification passed on the WORKTREE, but review_code
        # inspects the committed range. If the WIP commit left source/test changes
        # uncommitted, that range is stale — fail closed (don't hand a stale range to
        # the reviewer). status="error" routes through the generic retry, carrying the
        # stray-file list back to the implementer; a repeat defers/escalates normally.
        try:
            stray = _uncommitted_scope_files(rr, proj)
        except (OSError, RuntimeError) as exc:
            # A git probe failed → can't confirm the committed range is fresh. Fail
            # closed (don't approve a possibly-stale range); the generic retry path
            # re-runs, a repeat defers (#50 review PR-001).
            feedback = f"could not verify commit integrity ({exc}); refusing to hand a possibly-stale range to review."
            return PhaseResult(status="error", feedback=feedback, log=feedback, diff=diff)
        if stray:
            feedback = (
                "implement left source/test changes uncommitted after the WIP commit, so the "
                "reviewed range (git diff <base>...HEAD) would be STALE relative to the tree "
                "verification just passed on. Uncommitted: " + ", ".join(stray) + ". Commit these "
                "(they belong in the implementation diff), or remove them — refusing to "
                "hand a stale committed range to review."
            )
            return PhaseResult(status="error", feedback=feedback, log=feedback, diff=diff)
        return PhaseResult(
            status="approved",
            feedback="",
            log=result["stdout"] + "\n--- verification ---\n" + verify_output,
            diff=diff,
        )

    feedback = f"verification failed (exit {rc}). Address the failures below and try again.\n\n{verify_output[-4000:]}"
    return PhaseResult(status="changes_requested", feedback=feedback, log=feedback, diff=diff)


def _tdd_base_prompt(task_dir: Path, proj: Any) -> str:
    """TDD implement prompt. The test file + test_review.md were authored upstream
    and are read-only here — the implementer only turns the red tests green."""
    return (
        "Mode: tdd. Implement the minimum code to make the new test file (the canonical path "
        "declared in outcome.md's Affected files) pass.\n"
        f"Inputs: {task_dir}/outcome.md, the new test file under `{proj.test_dir}`, "
        f"{task_dir}/test_review.md. Respect the project hard rules in {proj.context_file}; "
        f"source dirs: {', '.join(proj.source_dirs)}.\n"
        "Stay strictly within the Affected files listed in outcome.md. Do NOT modify "
        "the test file the test-author created. After implementing, save your full "
        f"diff to {task_dir}/impl_diff.patch via "
        f"`git diff > {task_dir}/impl_diff.patch`. Follow your agent definition exactly."
    )


def run(task_dir: Path, state: dict[str, Any]) -> PhaseResult:
    if state.get("mode") == "agent-pair":
        return _run_agent_pair(task_dir, state)

    proj = project_config()
    base = _tdd_base_prompt(task_dir, proj)
    prompt = build_prompt_with_feedback(base, state.get("last_failure_log"))

    rr = repo_root()
    # Snapshot + validate the verify command BEFORE the implementer runs, so a
    # same-round edit to config.toml's verify_command cannot self-neuter the
    # gate (IR-001). The agent-pair path snapshots at plan time for the same
    # reason; this keeps the legacy/TDD path consistent.
    from config import load_config

    try:
        # One config load → pass verify_command and allowlist together, so the
        # two cannot come from different reads.
        _proj = load_config(rr).project
        verify_argv = validate_verification_commands(
            [_proj.verify_command], _proj.verify_command, list(_proj.verification_allowlist)
        )[0]
    except ValueError as exc:
        msg = f"invalid project.verify_command in config: {exc}"
        return PhaseResult(status="error", feedback=msg, log=msg, diff="")

    result = get_worker_adapter(state).invoke(role="implementer", agent=AGENT_NAME, prompt=prompt, cwd=rr)
    diff = compute_repo_diff(cwd=rr)

    if result["returncode"] != 0:
        feedback = (
            f"implementer agent exited non-zero.\n"
            f"returncode={result['returncode']}\n"
            f"stderr (truncated):\n{result['stderr'][:2000]}"
        )
        return PhaseResult(status="error", feedback=feedback, log=feedback, diff=diff)

    patch_path = task_dir / "impl_diff.patch"
    if not patch_path.exists():
        feedback = (
            f"impl_diff.patch missing — implementer didn't save the diff.\n"
            "Re-run after instructing the agent to write `git diff > "
            f"{task_dir}/impl_diff.patch` before exiting."
        )
        return PhaseResult(status="error", feedback=feedback, log=feedback, diff=diff)

    rc, verify_output = _run_verify_sh(rr, verify_argv)
    if rc == 0:
        return PhaseResult(
            status="approved",
            feedback="",
            log=result["stdout"] + "\n--- verify.sh ---\n" + verify_output,
            diff=diff,
        )

    feedback = f"verify.sh failed (exit {rc}). Address the failures below and try again.\n\n{verify_output[-4000:]}"
    return PhaseResult(
        status="changes_requested",
        feedback=feedback,
        log=feedback,
        diff=diff,
    )
