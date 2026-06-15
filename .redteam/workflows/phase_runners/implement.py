"""Phase 5 — invoke the implementer, then independently run verify.sh."""

from __future__ import annotations

import hashlib
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from adapters import get_worker_adapter

from ._base import (
    PhaseResult,
    build_prompt_with_feedback,
    compute_branch_diff,
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


def _write_current_diff(task_dir: Path, cwd: Path) -> tuple[str, str]:
    diff = compute_branch_diff(cwd=cwd)
    patch_path = task_dir / "impl_diff.patch"
    patch_path.write_text(diff, encoding="utf-8")
    digest = hashlib.sha256(diff.encode("utf-8")).hexdigest()
    return diff, digest


DIFF_GIT_RE = re.compile(r"^diff --git a/(.*?) b/(.*?)$")


def _paths_from_patch(diff: str) -> list[str]:
    paths: list[str] = []
    seen: set[str] = set()
    for line in diff.splitlines():
        match = DIFF_GIT_RE.match(line)
        if match is None:
            continue
        for path in match.groups():
            if path == "/dev/null" or path in seen:
                continue
            seen.add(path)
            paths.append(path)
    return paths


def _commit_agent_pair_diff(task_dir: Path, state: dict[str, Any], cwd: Path, diff: str) -> None:
    """Commit only the files present in impl_diff.patch, then refresh the patch.

    The pre-commit patch is the source of truth for the file list. After the WIP
    commit lands, impl_diff.patch is regenerated so it still represents the
    branch diff against main, while the working tree itself can be clean.
    """
    state["implement_round_count"] = int(state.get("implement_round_count") or 0) + 1
    round_n = int(state["implement_round_count"])
    paths = _paths_from_patch(diff)
    if not paths:
        return

    subprocess.run(
        ["git", "add", "--", *paths],
        cwd=str(cwd),
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    diff_check = subprocess.run(
        ["git", "diff", "--cached", "--quiet"],
        cwd=str(cwd),
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    if diff_check.returncode == 0:
        return

    task_id = str(state.get("task_id") or task_dir.name)
    subprocess.run(
        ["git", "commit", "-m", f"wip({task_id}): implement round {round_n}"],
        cwd=str(cwd),
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    _write_current_diff(task_dir, cwd)


def _uncommitted_scope_files(cwd: Path, proj: Any) -> list[str]:
    """Source/test files still uncommitted AFTER the scoped commit (#50).

    `_commit_agent_pair_diff` stages only the declared Affected files, so anything
    the implementer changed OUTSIDE that set stays in the working tree — making the
    committed range `git diff <base>...HEAD` the reviewer inspects STALE relative to
    the tree verification just passed on (verify ran on the dirty worktree). Returns
    the uncommitted *source/test* files across all three states that would diverge
    the committed range from the worktree verification ran on:
      - staged-but-uncommitted (`git diff --cached`): the commit did not land them
        (a `git commit` failure / hook); `_commit_agent_pair_diff` ignores the
        commit returncode, so this is reachable and MUST be caught here;
      - tracked-but-unstaged modifications (`git diff`);
      - untracked, non-ignored new files (`git ls-files --others`).
    Restricted to the project's source_dirs / test_dir, so harness artifacts
    (impl_diff.patch, verification.log) and gitignored files (e.g. __pycache__,
    *.pyc) never trip it — only real code/test changes. After a SUCCESSFUL commit
    the index equals HEAD, so the `--cached` probe is empty (no false positive).
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


def _run_agent_pair(task_dir: Path, state: dict[str, Any]) -> PhaseResult:
    proj = project_config()
    base = (
        f"Implement the approved plan for the task at: {task_dir}\n"
        f"Inputs: {task_dir}/input.md, {task_dir}/outcome.md, "
        f"{task_dir}/plan_review.md, and any previous {task_dir}/code_review.md.\n"
        f"Respect the project hard rules in {proj.context_file}. Source dirs: "
        f"{', '.join(proj.source_dirs)}; test dir: {proj.test_dir}.\n"
        "Stay within the approved plan. If the work needs new scope, stop and update outcome.md instead. "
        "Do not create a PR. After editing, stop; the orchestrator will run verification."
    )
    prompt = build_prompt_with_feedback(base, state.get("last_failure_log"))

    rr = repo_root()
    result = get_worker_adapter(state).invoke(role="implementer", agent=AGENT_NAME, prompt=prompt, cwd=rr)
    diff, diff_sha = _write_current_diff(task_dir, rr)

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
    _commit_agent_pair_diff(task_dir, state, rr, diff)
    diff, diff_sha = _write_current_diff(task_dir, rr)
    verification["last_diff_sha256"] = diff_sha

    if rc == 0:
        # Integrity gate (#50): verification passed on the WORKTREE, but review_code
        # inspects the committed range. If the scoped commit left source/test changes
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
                "implement left source/test changes uncommitted after the scoped commit, so the "
                "reviewed range (git diff <base>...HEAD) would be STALE relative to the tree "
                "verification just passed on. Uncommitted: " + ", ".join(stray) + ". Declare these "
                "in outcome.md's Affected files so they are committed, or remove them — refusing to "
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


def run(task_dir: Path, state: dict[str, Any]) -> PhaseResult:
    if state.get("mode") == "agent-pair":
        return _run_agent_pair(task_dir, state)

    proj = project_config()
    base = (
        "Implement the minimum code to make the new test file (the canonical path "
        "declared in outcome.md's Affected files) pass.\n"
        f"Inputs: {task_dir}/outcome.md, the new test file under `{proj.test_dir}`, "
        f"{task_dir}/test_review.md. Respect the project hard rules in {proj.context_file}; "
        f"source dirs: {', '.join(proj.source_dirs)}.\n"
        "Stay strictly within the Affected files listed in outcome.md. Do NOT modify "
        "the test file the test-author created. After implementing, save your full "
        f"diff to {task_dir}/impl_diff.patch via "
        f"`git diff > {task_dir}/impl_diff.patch`. Follow your agent definition exactly."
    )
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
