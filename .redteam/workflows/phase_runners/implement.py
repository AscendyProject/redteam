"""Phase 5 — invoke the implementer, then independently run verify.sh."""

from __future__ import annotations

import fnmatch
import hashlib
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from adapters import get_worker_adapter, worker_provider

from ._base import (
    PhaseResult,
    _tracked_changed_paths,
    build_prompt_with_feedback,
    commit_paths,
    compute_repo_diff,
    get_or_set_tracked_baseline,
    get_or_set_untracked_baseline,
    persist_state,
    pinned_base_branch,
    project_config,
    run_git_checked,
    untracked_files,
    validate_verification_commands,
    repo_root,
)


AGENT_NAME = "implementer"

# ---------------------------------------------------------------------------
# Cross-run trust-root floor marker (#117).
# In-memory ONLY — never serialized, never written to state.json, never read
# from disk.  Reset on process exit.  Keyed by resolved task_dir Path so
# same-process sibling tasks in goal-mode do not cross-pollinate.
# ---------------------------------------------------------------------------
_trusted_task_dirs: set[Path] = set()

# ---------------------------------------------------------------------------
# Shared allowlist predicate for pre-worker floors (#136).
# ---------------------------------------------------------------------------
_BATCH_ROOT_ARTIFACT_ALLOWLIST = frozenset({"goal.md", "goal.json", "decompose_review.md", "decompose_blocked.md"})
_SIBLING_BASENAME_ALLOWLIST = frozenset({"state.json", "outcome.md", "pr.md", "input.md"})


def _is_harness_artifact(p: str, task_dir: Path, cwd: Path, scope_roots: list[str]) -> bool:
    """Shared allowlist predicate used by both pre-worker floors (#136).

    Returns True iff `p` is an exempt harness artifact — one of:
    1. Under the current task dir (decision-trail files: state.json, outcome.md, etc.).
    2. Under source_dirs / test_dir (scope roots).
    3. A file directly at the batch root (task_dir.parent.parent) whose basename is
       exactly one of the four goal-mode decompose artifacts (goal.md, goal.json,
       decompose_review.md, decompose_blocked.md). Files in a batch-root subdirectory
       or with a different basename are NOT exempt.
    4. A top-level harness decision-trail file (state.json, outcome.md, pr.md, input.md,
       or *_review.md) directly under a SIBLING task dir in the same batch's tasks/ root
       (task_dir.parent). Files buried in a sibling subdirectory, non-allowlisted basenames,
       and paths under a different batch's tasks/ root are NOT exempt.

    Used by `_floor_outside_scope` and `_cross_run_trust_root_floor` so their "allowed"
    definitions cannot drift apart.
    """
    # 1. Task-dir exemption.
    try:
        task_rel = task_dir.resolve().relative_to(cwd.resolve()).as_posix()
    except ValueError:
        task_rel = None
    if task_rel is not None and (p == task_rel or p.startswith(task_rel + "/")):
        return True

    # 2. Scope roots exemption.
    if any(p.replace("\\", "/").startswith(root) for root in scope_roots):
        return True

    # 3. Batch-root decompose artifact exemption (#136).
    # batch root = task_dir.parent.parent (the batch directory containing goal.md etc.)
    try:
        batch_root_rel = task_dir.parent.parent.resolve().relative_to(cwd.resolve()).as_posix()
    except ValueError:
        batch_root_rel = None
    if batch_root_rel is not None:
        batch_prefix = batch_root_rel + "/"
        if p.startswith(batch_prefix):
            remainder = p[len(batch_prefix) :]
            if "/" not in remainder and remainder in _BATCH_ROOT_ARTIFACT_ALLOWLIST:
                return True

    # 4. Sibling top-level artifact exemption (#124 extended by #136).
    # tasks root = task_dir.parent (the tasks/ directory containing sibling task dirs)
    try:
        tasks_rel = task_dir.parent.resolve().relative_to(cwd.resolve()).as_posix()
    except ValueError:
        tasks_rel = None
    if tasks_rel is not None:
        tasks_prefix = tasks_rel + "/"
        if p.startswith(tasks_prefix):
            remainder = p[len(tasks_prefix) :]  # "<sibling-id>/<artifact>" or deeper
            slash_idx = remainder.find("/")
            if slash_idx >= 0:
                sibling_id = remainder[:slash_idx]
                if sibling_id != task_dir.name:  # not the current task's own dir
                    artifact_name = remainder[slash_idx + 1 :]
                    if "/" not in artifact_name:  # top-level only, not buried in a subdir
                        if artifact_name in _SIBLING_BASENAME_ALLOWLIST or fnmatch.fnmatchcase(
                            artifact_name, "*_review.md"
                        ):
                            return True

    return False


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
        try:
            proc = subprocess.run(
                argv,
                cwd=str(cwd),
                capture_output=True,
                text=True,
                encoding="utf-8",
                check=False,
            )
        except FileNotFoundError:
            # The command passed the allowlist but its executable is not on PATH
            # (e.g. a tool that only lives in a project venv the verify command
            # activates). Fail closed LEGIBLY: return a non-zero code with an
            # actionable reason so the phase surfaces a PhaseResult error, instead
            # of letting a raw FileNotFoundError bubble to the batch driver, which
            # drops the traceback and reports an opaque `error: FileNotFoundError`.
            chunks.append(
                f"[error] verification command not found on PATH: {argv[0]!r}. "
                "Is it installed and resolvable in this environment? Tools that only "
                "live in a project venv are not on PATH unless the verify command "
                "activates it — prefer the project verify command (e.g. a wrapper "
                "script) over a bare tool invocation.\n"
            )
            return 127, "".join(chunks)
        output = (proc.stdout or "") + (("\n" + proc.stderr) if proc.stderr else "")
        chunks.append(output)
        chunks.append(f"\n[exit {proc.returncode}]\n\n")
        if proc.returncode != 0:
            return proc.returncode, "".join(chunks)
    return 0, "".join(chunks)


def _branch_diff_checked(cwd: Path, base_branch: str) -> str:
    """Full branch diff (committed + unstaged + staged), FAIL-CLOSED — raises on a
    git error instead of returning a silently empty/partial diff. impl_diff.patch is
    what the reviewer reads; a partial patch behind a green status would let an
    incomplete change be approved. (agent-pair path.) `base_branch` is the per-task
    PINNED base (#91), not live config."""
    return "".join(
        run_git_checked(args, cwd).stdout
        for args in (["diff", f"{base_branch}...HEAD"], ["diff"], ["diff", "--cached"])
    )


def _committed_range_diff(cwd: Path, base_branch: str) -> str:
    """The COMMITTED range only, `git diff <base>...HEAD`, FAIL-CLOSED. Used to
    regenerate impl_diff.patch in the tdd path (#82): tdd now truly commits (write_test
    commits the test, implement commits the rest), so the committed range IS the work —
    and a committed-only patch equals EXACTLY what the PR will contain (reviewer-input
    integrity). Anything left uncommitted in scope is caught by the integrity gate; out
    of scope is the operator's, not the task's. `base_branch` is the per-task PINNED base
    (#91), pinned pre-worker so a mid-task config edit can't move the reviewed range."""
    return run_git_checked(["diff", f"{base_branch}...HEAD"], cwd).stdout


def _write_current_diff(task_dir: Path, cwd: Path, base_branch: str, diff: str | None = None) -> tuple[str, str]:
    if diff is None:
        diff = _branch_diff_checked(cwd, base_branch)
    patch_path = task_dir / "impl_diff.patch"
    patch_path.write_text(diff, encoding="utf-8")
    digest = hashlib.sha256(diff.encode("utf-8")).hexdigest()
    return diff, digest


def _scope_root(r: str) -> str:
    """Normalize a source/test dir root to a trailing-slash POSIX prefix.
    Used by the pre-worker out-of-scope tracked floor (#91 Part A)."""
    r = r.replace("\\", "/")
    return r if r.endswith("/") else r + "/"


def _plan_affected_files(task_dir: Path) -> frozenset[str]:
    """Parse the Affected files section of task_dir/outcome.md into a frozenset of
    repo-relative POSIX paths.  Used by _get_or_set_plan_affected_files_baseline (#137).

    Fail-closed: returns an empty frozenset when outcome.md is absent, unreadable,
    or contains no Affected files heading (levels #/##/###).  Per-entry malformed
    paths (absolute, or containing a '..' segment) are skipped without rejecting the
    whole file.  A '(new) ' prefix (case-insensitive, positional, single occurrence)
    is stripped from each item before validation.
    """
    try:
        text = (task_dir / "outcome.md").read_text(encoding="utf-8")
    except (FileNotFoundError, OSError, UnicodeDecodeError):
        return frozenset()

    heading_re = re.compile(r"^(#{1,6})\s+(.*)")
    bullet_re = re.compile(r"^[*-]\s+(.*)")
    new_prefix_re = re.compile(r"^\(new\)\s+", re.IGNORECASE)

    heading_level: int | None = None
    paths: list[str] = []

    for line in text.splitlines():
        hm = heading_re.match(line)
        if hm:
            level = len(hm.group(1))
            title = hm.group(2).strip()
            if heading_level is None:
                # Looking for the Affected files heading (levels 1–3 only).
                if title.lower() == "affected files" and level <= 3:
                    heading_level = level
            else:
                # Inside the section — stop at same or higher level.
                if level <= heading_level:
                    break
        elif heading_level is not None:
            bm = bullet_re.match(line)
            if bm:
                body = bm.group(1).strip()
                # Strip a single positional '(new) ' prefix that may precede
                # the backtick span or the bare path (case-insensitive).
                body = new_prefix_re.sub("", body).strip()
                if body.startswith("`"):
                    # Backtick form: extract content of the first `...` span;
                    # any trailing residue (` — reason`, ` - reason`, etc.) is
                    # discarded.  Empty span or no closing backtick → fail-closed.
                    m = re.match(r"`([^`]+)`", body)
                    if not m:
                        continue
                    raw = m.group(1).strip()
                    # Strip '(new) ' that may appear inside the backtick span.
                    raw = new_prefix_re.sub("", raw).strip()
                else:
                    # Bare path: take text up to the first description separator
                    # (' — ' em-dash or ' - ' hyphen); whole line if none present.
                    sep_m = re.search(r" (?:—|-) ", body)
                    raw = body[: sep_m.start()].strip() if sep_m else body.strip()
                raw = raw.replace("\\", "/")
                if not raw:
                    continue
                if raw.startswith("/"):
                    continue
                if ".." in raw.split("/"):
                    continue
                paths.append(raw)

    return frozenset(paths)


def _get_or_set_plan_affected_files_baseline(state: dict[str, Any], task_dir: Path) -> frozenset[str]:
    """Set-once getter for the plan-declared Affected files snapshot (#137).

    key-present (stored as list) → return frozenset(list) WITHOUT re-parsing outcome.md.
    key-absent → parse outcome.md exactly once, store result as sorted list, return frozenset.
    Does NOT call persist_state — caller persists alongside the tracked/untracked baselines.
    """
    key = "implement_plan_affected_files"
    stored = state.get(key)
    if isinstance(stored, list):
        return frozenset(stored)
    result = _plan_affected_files(task_dir)
    state[key] = sorted(result)
    return result


def _floor_outside_scope(
    current_tracked: set[str],
    proj: Any,
    task_dir: Path,
    cwd: Path,
    *,
    plan_affected: frozenset[str] = frozenset(),
) -> set[str]:
    """Pre-worker out-of-scope tracked floor (#91 Part A): the operator's tracked paths
    changed vs the pinned base that lie OUTSIDE source_dirs/test_dir. The task dir is
    EXEMPT — its files (outcome.md, state.json, the *_review.md trail) are the harness's
    own decision trail, excluded here for the same reason `_commit_worker_diff` and the
    Layer-2 untracked gate (`_uncommitted_outside_scope_files`) exclude them: a consumer
    who commits their batch dir onto the task branch must not be falsely refused over the
    harness's own artifacts. Genuine operator tracked WIP outside scope still trips the
    floor. Mirrors the `_is_harness_artifact` POSIX-prefix logic used by those two functions.

    Sibling-task artifact exemption (#124): in a stacked goal-mode run, sibling task dirs
    under the SAME batch's tasks root (i.e. task_dir.parent, POSIX-prefix) also expose
    harness decision-trail artifacts (state.json, outcome.md, pr.md, input.md, *_review.md).
    Only TOP-LEVEL paths directly under a sibling task dir (the relative-to-sibling path
    must contain no '/') are exempt; a path in a sibling subdirectory, a non-allowlisted
    sibling basename, or a path under a DIFFERENT batch's tasks dir still trips the floor.

    Batch-root decompose artifact exemption (#136): goal-mode decompose artifacts
    (goal.md, goal.json, decompose_review.md, decompose_blocked.md) may be tracked at the
    batch root (task_dir.parent.parent). These are exempt when placed directly at the batch
    root; the same basenames in a batch-root subdirectory still trip the floor.

    Uses the shared _is_harness_artifact predicate so this floor and
    _cross_run_trust_root_floor cannot drift apart in their "allowed" definitions."""
    scope_roots = [_scope_root(r) for r in (*proj.source_dirs, proj.test_dir)]
    return {
        p for p in current_tracked if not _is_harness_artifact(p, task_dir, cwd, scope_roots) and p not in plan_affected
    }


def _cross_run_trust_root_floor(
    state: dict[str, Any],
    task_dir: Path,
    cwd: Path,
    proj: Any,
    current_untracked: set[str],
) -> set[str]:
    """Cross-run trust-root floor (#117).

    Returns the set of offending paths; an empty set means the floor passes.
    Called at most once per task_dir per orchestrator process (the caller guards
    with `_trusted_task_dirs`).

    Two checks — either failing adds paths to the offending set:

    1. **Live floor**: the current outside-scope, outside-task_dir untracked
       surface (`current_untracked`) must be empty. A worker-created file that
       survived across runs (or a pre-#112 migration-window crash residual)
       is caught here.

    2. **Stored-baseline contents floor**: `state["implement_untracked_baseline"]`
       and `state["implement_tracked_baseline"]`, when key-present, must contain
       no path outside source_dirs/test_dir and outside task_dir. A legitimately
       stored baseline never contains such a path (the pre-worker floors ensure the
       outside-scope surface is clean BEFORE the set-once snapshot is taken), so
       any outside-scope entry is by construction worker-injected.

    Both checks use `_is_harness_artifact` — the shared allowlist predicate — so
    this floor and `_floor_outside_scope` cannot drift apart in their "allowed"
    definitions (#136). The predicate covers task-dir, scope roots, batch-root
    decompose artifacts, and sibling top-level artifacts (incl. input.md).

    Uses the same `_scope_root` + `_is_harness_artifact` POSIX-prefix logic as
    `_floor_outside_scope` — one definition, no duplication.
    """
    scope_roots = [_scope_root(r) for r in (*proj.source_dirs, proj.test_dir)]

    def _is_allowed(p: str) -> bool:
        return _is_harness_artifact(p, task_dir, cwd, scope_roots)

    offending: set[str] = set()

    # Check 1 — live floor: outside-scope untracked surface must be empty.
    for p in current_untracked:
        if not _is_allowed(p):
            offending.add(p)

    # Check 2 — stored-baseline contents floor: no stored baseline entry may
    # lie outside source_dirs/test_dir and outside task_dir.
    for key in ("implement_untracked_baseline", "implement_tracked_baseline"):
        stored = state.get(key)
        if isinstance(stored, list):
            for p in stored:
                if not _is_allowed(p):
                    offending.add(p)

    return offending


def _commit_worker_diff(
    task_dir: Path,
    state: dict[str, Any],
    cwd: Path,
    before_untracked: set[str],
    before_tracked: set[str],
) -> None:
    """Commit the implementer's work — tracked changes PLUS files it newly created —
    then refresh impl_diff.patch from the committed range. Mode-generic (used by BOTH
    the agent-pair and tdd implement paths, #82).

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

    `before_tracked` (#91 Part A) is the pre-worker tracked baseline, passed in BY THE
    CALLER as the SAME in-memory `set` it got from ``get_or_set_tracked_baseline``
    (NOT re-read from disk here, so the same-round TOCTOU safety of IR-006 is preserved;
    the worker runs as a separate subprocess and cannot modify the caller's in-memory
    set). ``set(_tracked_changed_paths(now)) - before_tracked`` is exactly the tracked
    changes the implementer made this round, so the operator's pre-existing tracked
    modifications are not swept into the task commit.

    Known limitation — untracked side: a snapshot diffs by NAME, so a file that was
    ALREADY untracked before the worker ran and is then *modified* in place (not
    created) is not detected — it stays in `before_untracked`, so `current -
    before_untracked` excludes it. This is a pathological case (the implementer is
    scoped to outcome.md's Affected files; a pre-existing untracked file is the
    user's own scratch, not task scope) and such a change would not be in the PR
    anyway; closing it would require content-hashing the whole untracked set, which
    is disproportionate here.

    Known limitation — tracked side: the tracked baseline is also a snapshot by NAME.
    If the operator modified a tracked file in place BEFORE the worker AND the worker
    then *also* modifies that same file, the path is in ``before_tracked``, so
    ``now − before_tracked`` excludes the worker's change to it. The out-of-scope floor
    (run pre-worker) already removes the most damaging case (an operator pre-edit
    outside source/test scope); the residual is an in-scope tracked file the operator
    pre-edited — itself anomalous since the worker is scoped to outcome.md's Affected
    files. Closing it would require content-hashing the whole tracked set, which is
    judged disproportionate here.
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

    base_branch = pinned_base_branch(state, cwd)
    new_untracked = untracked_files(cwd) - before_untracked
    tracked_delta = set(_tracked_changed_paths(cwd, base_branch)) - before_tracked
    seen: set[str] = set()
    to_stage: list[str] = []
    for path in sorted(tracked_delta) + sorted(new_untracked):
        if path not in seen and not _in_task_dir(path):  # filter BOTH tracked + new untracked
            seen.add(path)
            to_stage.append(path)

    task_id = str(state.get("task_id") or task_dir.name)
    if commit_paths(cwd, to_stage, f"wip({task_id}): implement round {round_n}"):
        _write_current_diff(task_dir, cwd, base_branch)


def _uncommitted_scope_files(cwd: Path, proj: Any) -> list[str]:
    """Source/test files still uncommitted AFTER the WIP commit (#50).

    `_commit_worker_diff` stages the implementer's tracked changes PLUS the files
    it newly created, so after it runs the worktree should be clean within scope.
    This is defense-in-depth: if anything source/test still shows uncommitted (a
    failed commit, a file changed after the snapshot), the committed range
    `git diff <base>...HEAD` the reviewer inspects would be STALE relative to the tree
    verification just passed on (verify ran on the dirty worktree).

    Layer 1 of the post-commit two-layer integrity gate is intentionally
    baseline-INDEPENDENT (#91 Part A): an operator's pre-existing in-scope tracked
    edit is subtracted from the task commit by `_commit_worker_diff` and so remains
    uncommitted in the worktree — this gate still flags it, deferring the round, so a
    contaminated in-scope tree never produces a stale reviewed range. The operator must
    commit or stash even in-scope WIP before re-running; the out-of-scope floor handles
    the out-of-scope case earlier (pre-worker). This keeps the #50/#112 gate unchanged.

    Returns the uncommitted *source/test* files across all three states that would
    diverge the committed range from the worktree verification ran on:
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


def _uncommitted_plan_affected_paths(cwd: Path, plan_affected: frozenset[str]) -> set[str]:
    """Plan-affected paths still dirty (uncommitted) after the WIP commit (IR-001).

    ``_commit_worker_diff`` excludes paths that were in ``before_tracked`` from the
    staged set.  A plan-affected outside-scope path that was already dirty vs base
    BEFORE the first implement round is stored in ``before_tracked`` and therefore not
    committed by the WIP commit.  If it still has uncommitted tracked changes (unstaged
    or staged-but-uncommitted), the reviewed range ``git diff <base>...HEAD`` omits
    them — a stale range on the integrity boundary this family guards.

    Returns the subset of ``plan_affected`` that appears in either ``git diff``
    (unstaged) or ``git diff --cached`` (staged but uncommitted).  An empty
    ``plan_affected`` fast-paths to an empty set without any git call.

    Fail-closed: raises ``RuntimeError`` via ``run_git_checked`` on git error.
    """
    if not plan_affected:
        return set()
    uncommitted: set[str] = set()
    for args in (
        ["-c", "core.quotepath=false", "diff", "-z", "--name-only"],
        ["-c", "core.quotepath=false", "diff", "-z", "--name-only", "--cached"],
    ):
        out = run_git_checked(args, cwd).stdout
        uncommitted.update(p for p in out.split("\0") if p and p in plan_affected)
    return uncommitted


def _uncommitted_outside_scope_files(cwd: Path, task_dir: Path, proj: Any, baseline: set[str]) -> list[str]:
    """NEW untracked files OUTSIDE source_dirs/test_dir that are not in the baseline
    and not under the task dir — Layer 2 of the two-layer integrity gate (#112).

    Uses the module-level ``untracked_files`` (same ``--exclude-standard`` semantics
    as ``_commit_worker_diff``'s untracked capture) and returns paths in
    ``current_untracked - baseline`` that are not under ``task_dir``, not under any
    ``proj.source_dirs`` root, and not under ``proj.test_dir``. Fail-closed on a
    non-zero git exit via ``untracked_files`` (raises RuntimeError, omits stderr —
    IR-002). Using the module-level ``untracked_files`` name also makes this function
    patchable in tests that patch ``phase_runners.implement.untracked_files``.
    Stdlib-only.
    """
    current = untracked_files(cwd)
    new_files = current - baseline

    try:
        task_rel = task_dir.resolve().relative_to(cwd.resolve()).as_posix()
    except ValueError:
        task_rel = None

    def _in_task_dir(p: str) -> bool:
        return task_rel is not None and (p == task_rel or p.startswith(task_rel + "/"))

    def _root(r: str) -> str:
        r = r.replace("\\", "/")
        return r if r.endswith("/") else r + "/"

    scope_roots = [_root(r) for r in (*proj.source_dirs, proj.test_dir)]

    def _in_scope(p: str) -> bool:
        return any(p.replace("\\", "/").startswith(root) for root in scope_roots)

    return sorted(p for p in new_files if not _in_task_dir(p) and not _in_scope(p))


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
        f"(outcome.md's 'Verification hooks > To be created') together with the implementation; "
        f"follow the project's test conventions at {proj.test_conventions_file}. "
        "Do not modify, delete, or rename any pre-existing test. "
        "Stay within the approved plan; if the work needs new scope, stop and update outcome.md instead. "
        "Do not create a PR. After editing, stop; the orchestrator will run verification."
    )


def _run_agent_pair(task_dir: Path, state: dict[str, Any]) -> PhaseResult:
    proj = project_config()
    base = _agent_pair_base_prompt(task_dir, proj)
    prompt = build_prompt_with_feedback(base, state.get("last_failure_log"))

    rr = repo_root()
    base_branch = pinned_base_branch(state, rr)  # #91: pinned pre-worker, not live config
    # Pre-worker baseline snapshot (#91 Part A + #112):
    #   1. Fresh tracked probe for the out-of-scope floor (fail-closed on git error).
    #   2. Out-of-scope floor: if any tracked path lies outside source_dirs/test_dir,
    #      refuse and tell the operator to stash/commit — WITHOUT persisting a baseline
    #      so a clean re-run is never self-locked (PR-001 fix).
    #   3. Set-once tracked baseline (key-absent → store; key-present → reuse stored).
    #   4. Set-once untracked baseline.
    #   5. Single persist covering BOTH baselines.
    try:
        current_tracked = _tracked_changed_paths(rr, base_branch)
        current_tracked_set = set(current_tracked)
        plan_affected = _get_or_set_plan_affected_files_baseline(state, task_dir)
        outside_scope = _floor_outside_scope(current_tracked_set, proj, task_dir, rr, plan_affected=plan_affected)
        if outside_scope:
            fb = (
                "refusing to sweep operator tracked WIP into the task commit; "
                "commit or stash your unrelated tracked WIP before re-running. "
                f"Out-of-scope tracked paths: {', '.join(sorted(outside_scope))}"
            )
            return PhaseResult(status="error", feedback=fb, log=fb, diff="")
        # Cross-run trust-root floor (#117) — runs IFF task_dir is NOT yet in
        # the in-memory marker set (i.e., first entry of this orchestrator
        # process for this task).  On the failure path, returns BEFORE any
        # baseline is persisted so a clean re-run is never self-locked.
        #
        # Same-user, stdlib-only, worker-needs-Bash ⇒ tamper PREVENTION of the
        # on-disk baseline is impossible. The target is fail-closed
        # tamper-DETECTION on cross-run consumption. Trust is re-derived from
        # (a) the live outside-scope untracked surface AND (b) the stored
        # baseline contents — outside-scope paths never legitimately appear in
        # either, so the union of those two probes catches both the
        # "future-create" and "leave-on-disk" variants of an adversarial
        # baseline rewrite.
        current_untracked_set = untracked_files(rr)
        if task_dir not in _trusted_task_dirs:
            offending = _cross_run_trust_root_floor(state, task_dir, rr, proj, current_untracked_set)
            if offending:
                fb = (
                    "cross-run trust-root floor: outside-scope paths detected before "
                    "worker invocation; commit or stash these files and re-run. "
                    f"Offending paths: {', '.join(sorted(offending))}"
                )
                return PhaseResult(status="error", feedback=fb, log=fb, diff="")
        _trusted_task_dirs.add(task_dir)
        before_tracked = get_or_set_tracked_baseline(state, rr, base_branch, _tracked_fn=lambda *_: current_tracked_set)
        before_untracked = get_or_set_untracked_baseline(state, rr, _untracked_fn=lambda _cwd: current_untracked_set)
        persist_state(task_dir, state)
    except (RuntimeError, OSError) as exc:
        fb = f"could not snapshot the working tree before implement ({exc})."
        return PhaseResult(status="error", feedback=fb, log=fb, diff="")
    result = get_worker_adapter(state).invoke(role="implementer", agent=AGENT_NAME, prompt=prompt, cwd=rr)
    _tele = dict(
        cost_usd=result.get("cost_usd"),
        duration_sec=result.get("duration_sec"),
        model=result.get("model"),
        provider=worker_provider(state),
    )
    try:
        diff, diff_sha = _write_current_diff(task_dir, rr, base_branch)
    except (RuntimeError, OSError) as exc:
        fb = f"could not capture the implementation diff ({exc})."
        return PhaseResult(status="error", feedback=fb, log=fb, diff="", **_tele)

    verification = state.setdefault("verification", {})
    verification["last_diff_sha256"] = diff_sha
    verification["last_output_path"] = "verification.log"

    if result["returncode"] != 0:
        feedback = (
            f"implementer agent exited non-zero.\n"
            f"returncode={result['returncode']}\n"
            f"stderr (truncated):\n{result['stderr'][:2000]}"
        )
        return PhaseResult(status="error", feedback=feedback, log=feedback, diff=diff, **_tele)

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
        return PhaseResult(status="error", feedback=feedback, log=feedback, diff=diff, **_tele)
    rc, verify_output = _run_verification_commands(rr, commands, project_verify_command, verify_allowlist)

    verification["commands"] = commands
    verification["last_exit_code"] = rc
    verification["last_run_at"] = datetime.now(timezone.utc).isoformat()
    (task_dir / "verification.log").write_text(verify_output, encoding="utf-8")
    try:
        _commit_worker_diff(task_dir, state, rr, before_untracked, before_tracked)
    except (RuntimeError, OSError) as exc:
        fb = f"could not commit the implementer's changes ({exc}); refusing to hand a stale range to review."
        return PhaseResult(status="error", feedback=fb, log=fb, diff="", **_tele)
    try:
        diff, diff_sha = _write_current_diff(task_dir, rr, base_branch)
    except (RuntimeError, OSError) as exc:
        fb = f"could not regenerate the review diff after commit ({exc}); refusing to approve on a partial range."
        return PhaseResult(status="error", feedback=fb, log=fb, diff="", **_tele)
    verification["last_diff_sha256"] = diff_sha

    if rc == 0:
        # Three-layer integrity gate (#50 + #112 + IR-001): verification passed on
        # the WORKTREE, but review_code inspects the COMMITTED range. Any uncommitted
        # change makes that range stale — fail closed.
        # Layer 1 (baseline-INDEPENDENT): source/test files still uncommitted after
        # the WIP commit. Layer 2 (baseline-RELATIVE): new files outside source/test
        # that are not in the persisted baseline (the worker created them but left
        # them uncommitted). Layer 3 (IR-001): plan-affected outside-scope tracked
        # paths that were dirty before the worker and excluded from before_tracked →
        # not committed by _commit_worker_diff → still dirty after the commit.
        # status="error" routes through the generic retry; a repeat defers/escalates
        # normally (#50 review PR-001).
        try:
            layer1 = _uncommitted_scope_files(rr, proj)
            layer2 = _uncommitted_outside_scope_files(rr, task_dir, proj, before_untracked)
            layer3 = _uncommitted_plan_affected_paths(rr, plan_affected)
        except (OSError, RuntimeError) as exc:
            # A git probe failed → can't confirm the committed range is fresh. Fail
            # closed (don't approve a possibly-stale range); the generic retry path
            # re-runs, a repeat defers (#50 review PR-001).
            feedback = f"could not verify commit integrity ({exc}); refusing to hand a possibly-stale range to review."
            return PhaseResult(status="error", feedback=feedback, log=feedback, diff=diff, **_tele)
        stray = sorted(set(layer1) | set(layer2) | set(layer3))
        if stray:
            feedback = (
                "implement left changes uncommitted after the [WIP] commit, so the reviewed range "
                "`git diff <base>...HEAD` would be STALE relative to the tree "
                "verification just passed on. Uncommitted: " + ", ".join(stray) + ". Commit these "
                "(they belong in the implementation diff), or remove them — refusing to "
                "hand a stale committed range to review."
            )
            return PhaseResult(status="error", feedback=feedback, log=feedback, diff=diff, **_tele)
        return PhaseResult(
            status="approved",
            feedback="",
            log=result["stdout"] + "\n--- verification ---\n" + verify_output,
            diff=diff,
            **_tele,
        )

    feedback = f"verification failed (exit {rc}). Address the failures below and try again.\n\n{verify_output[-4000:]}"
    return PhaseResult(status="changes_requested", feedback=feedback, log=feedback, diff=diff, **_tele)


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
        "the test file the test-author created. After implementing, stop — the "
        "orchestrator runs verification and saves the diff. Follow your agent definition exactly."
    )


def run(task_dir: Path, state: dict[str, Any]) -> PhaseResult:
    if state.get("mode") == "agent-pair":
        return _run_agent_pair(task_dir, state)

    proj = project_config()
    base = _tdd_base_prompt(task_dir, proj)
    prompt = build_prompt_with_feedback(base, state.get("last_failure_log"))

    rr = repo_root()
    base_branch = pinned_base_branch(state, rr)  # #91: pinned pre-worker, not live config
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

    # Pre-worker baseline snapshot (#91 Part A + #112), same as agent-pair path:
    #   1. Fresh tracked probe for the out-of-scope floor (fail-closed on git error).
    #   2. Out-of-scope floor: if any tracked path lies outside source_dirs/test_dir,
    #      refuse — WITHOUT persisting a baseline so a clean re-run is never self-locked.
    #   3. Set-once tracked baseline. 4. Set-once untracked baseline. 5. Single persist.
    # The test was already committed by write_test (#82); it is therefore in the tracked
    # baseline (not new_untracked) and flows through the normal tracked-delta path.
    try:
        current_tracked = _tracked_changed_paths(rr, base_branch)
        current_tracked_set = set(current_tracked)
        plan_affected = _get_or_set_plan_affected_files_baseline(state, task_dir)
        outside_scope = _floor_outside_scope(current_tracked_set, proj, task_dir, rr, plan_affected=plan_affected)
        if outside_scope:
            fb = (
                "refusing to sweep operator tracked WIP into the task commit; "
                "commit or stash your unrelated tracked WIP before re-running. "
                f"Out-of-scope tracked paths: {', '.join(sorted(outside_scope))}"
            )
            return PhaseResult(status="error", feedback=fb, log=fb, diff="")
        # Cross-run trust-root floor (#117) — same as agent-pair path.
        #
        # Same-user, stdlib-only, worker-needs-Bash ⇒ tamper PREVENTION of the
        # on-disk baseline is impossible. The target is fail-closed
        # tamper-DETECTION on cross-run consumption. Trust is re-derived from
        # (a) the live outside-scope untracked surface AND (b) the stored
        # baseline contents — outside-scope paths never legitimately appear in
        # either, so the union of those two probes catches both the
        # "future-create" and "leave-on-disk" variants of an adversarial
        # baseline rewrite.
        current_untracked_set = untracked_files(rr)
        if task_dir not in _trusted_task_dirs:
            offending = _cross_run_trust_root_floor(state, task_dir, rr, proj, current_untracked_set)
            if offending:
                fb = (
                    "cross-run trust-root floor: outside-scope paths detected before "
                    "worker invocation; commit or stash these files and re-run. "
                    f"Offending paths: {', '.join(sorted(offending))}"
                )
                return PhaseResult(status="error", feedback=fb, log=fb, diff="")
        _trusted_task_dirs.add(task_dir)
        before_tracked = get_or_set_tracked_baseline(state, rr, base_branch, _tracked_fn=lambda *_: current_tracked_set)
        before_untracked = get_or_set_untracked_baseline(state, rr, _untracked_fn=lambda _cwd: current_untracked_set)
        persist_state(task_dir, state)
    except (RuntimeError, OSError) as exc:
        fb = f"could not snapshot the working tree before implement ({exc})."
        return PhaseResult(status="error", feedback=fb, log=fb, diff="")

    result = get_worker_adapter(state).invoke(role="implementer", agent=AGENT_NAME, prompt=prompt, cwd=rr)
    diff = compute_repo_diff(cwd=rr)
    _tele = dict(
        cost_usd=result.get("cost_usd"),
        duration_sec=result.get("duration_sec"),
        model=result.get("model"),
        provider=worker_provider(state),
    )

    if result["returncode"] != 0:
        feedback = (
            f"implementer agent exited non-zero.\n"
            f"returncode={result['returncode']}\n"
            f"stderr (truncated):\n{result['stderr'][:2000]}"
        )
        return PhaseResult(status="error", feedback=feedback, log=feedback, diff=diff, **_tele)

    rc, verify_output = _run_verify_sh(rr, verify_argv)

    # tdd now truly COMMITS (#82): commit implement's work (source + any new file
    # anywhere, incl. out-of-root migrations/config — current − before), then
    # regenerate impl_diff.patch from the COMMITTED range only, so the patch the
    # reviewer reads equals EXACTLY what the PR will contain. The verify ran on the
    # worktree; commit/regen happen AFTER it so the patch matches the tree that passed.
    try:
        _commit_worker_diff(task_dir, state, rr, before_untracked, before_tracked)
    except (RuntimeError, OSError) as exc:
        fb = f"could not commit the implementer's changes ({exc}); refusing to hand a stale range to review."
        return PhaseResult(status="error", feedback=fb, log=fb, diff="", **_tele)
    try:
        diff, _ = _write_current_diff(task_dir, rr, base_branch, _committed_range_diff(rr, base_branch))
    except (RuntimeError, OSError) as exc:
        fb = f"could not regenerate the review diff after commit ({exc}); refusing to approve on a partial range."
        return PhaseResult(status="error", feedback=fb, log=fb, diff="", **_tele)

    if rc == 0:
        # Three-layer integrity gate (#50 + #112 + IR-001): verify passed on the
        # WORKTREE, but review_code inspects the COMMITTED range. Layer 1
        # (baseline-INDEPENDENT): source/test files still uncommitted. Layer 2
        # (baseline-RELATIVE): new files outside source/test not in the persisted
        # baseline. Layer 3 (IR-001): plan-affected outside-scope tracked paths dirty
        # before the worker, excluded from before_tracked, still dirty after commit.
        # Also the safety net for a legacy in-flight tdd task whose test was never
        # committed (trips Layer 1).
        try:
            layer1 = _uncommitted_scope_files(rr, proj)
            layer2 = _uncommitted_outside_scope_files(rr, task_dir, proj, before_untracked)
            layer3 = _uncommitted_plan_affected_paths(rr, plan_affected)
        except (OSError, RuntimeError) as exc:
            fb = f"could not verify commit integrity ({exc}); refusing to hand a possibly-stale range to review."
            return PhaseResult(status="error", feedback=fb, log=fb, diff=diff, **_tele)
        stray = sorted(set(layer1) | set(layer2) | set(layer3))
        if stray:
            feedback = (
                "implement left changes uncommitted after the [WIP] commit, so the reviewed range "
                "`git diff <base>...HEAD` would be STALE relative to the tree "
                "verification just passed on. Uncommitted: " + ", ".join(stray) + ". Commit these "
                "(they belong in the implementation diff), or remove them — refusing to "
                "hand a stale committed range to review."
            )
            return PhaseResult(status="error", feedback=feedback, log=feedback, diff=diff, **_tele)
        return PhaseResult(
            status="approved",
            feedback="",
            log=result["stdout"] + "\n--- verify.sh ---\n" + verify_output,
            diff=diff,
            **_tele,
        )

    feedback = f"verify.sh failed (exit {rc}). Address the failures below and try again.\n\n{verify_output[-4000:]}"
    return PhaseResult(
        status="changes_requested",
        feedback=feedback,
        log=feedback,
        diff=diff,
        **_tele,
    )
