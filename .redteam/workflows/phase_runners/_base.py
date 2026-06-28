"""Shared helpers for every phase runner.

The orchestrator stays simple by pushing all subprocess plumbing here:
- `run_claude` invokes `claude -p` with a named sub-agent. Output is consumed as
  stream-json so the orchestrator can print live progress to stderr while the
  agent runs (no more 30-min black box waits).
- `parse_review_decision` extracts the final REVIEW_DECISION line from a review file.
- `compute_repo_diff` returns the working-tree diff (used for stall detection).
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Literal, NotRequired, TypedDict


# Default per-phase timeout in seconds. Halved from the original 1800s. With
# `max_retries_per_phase=2` (state.template.json), worst-case task time is now
# ~3 attempts × 900s ≈ 45 min instead of 4 × 1800s ≈ 2 hours. A phase that
# legitimately needs >15 min is usually a sign the task should be decomposed.
DEFAULT_TIMEOUT_SEC = 900


def default_model_for_role(role: str) -> str | None:
    """Project-default model for a role, from `.redteam/config.toml` [models].

    Replaces the old hardcoded DEFAULT_MODELS dict — model choices are now
    config-driven (the model-freedom seam): a project sets its own role→model
    in config.toml. `state.models` still overrides this per task. Lazy import
    keeps module load free of the config dependency (workflows is on sys.path
    by call time, via the orchestrator or a test's path setup)."""
    from config import load_config

    return getattr(load_config(repo_root()).models, role, None)


PhaseStatus = Literal["approved", "changes_requested", "rescue_required", "ask_user", "error", "manual_required"]
ReviewDecision = Literal["APPROVED", "CHANGES_REQUESTED", "RESCUE_REQUIRED", "ASK_USER", "MISSING"]


class PhaseResult(TypedDict):
    """Return shape every phase runner emits."""

    status: PhaseStatus
    feedback: str
    log: str
    diff: str
    # Structured provenance: set ONLY by the engine (review_with_fallback → runner)
    # when an automatic fallback produced this decision. Trusted for the audit
    # trail so reviewer-controlled text can't spoof it (#37 review PR-002).
    fallback_audit: NotRequired[str]


class ClaudeRunResult(TypedDict):
    """Outcome of a single `claude -p` subprocess call."""

    returncode: int
    stdout: str
    stderr: str
    parsed_json: dict | None


def repo_root() -> Path:
    """Repository root, derived from this file's location.

    `_base.py` lives at `<repo>/.redteam/workflows/phase_runners/_base.py`,
    so `parents[3]` is the repo root.
    """
    return Path(__file__).resolve().parents[3]


def incomplete_briefs(batch_dir: Path) -> list[str] | None:
    """Task IDs in goal.json that lack a non-empty tasks/<id>/input.md.

    Single source of truth for the brief-completeness check, shared by the
    decomposer runner contract and cmd_decompose's pre-approval gate so the two
    cannot diverge. Returns None when goal.json is absent or unparseable (the
    caller decides whether that is fail-closed or a no-op); otherwise the list of
    task IDs whose input.md is missing or empty (empty list == all briefs present).
    """
    goal_path = batch_dir / "goal.json"
    try:
        data = json.loads(goal_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    incomplete = []
    for tid in data.get("tasks", {}):
        brief = batch_dir / "tasks" / tid / "input.md"
        if not brief.is_file() or brief.stat().st_size == 0:
            incomplete.append(tid)
    return incomplete


def _print_stream_event(line: str, agent: str) -> dict | None:
    """Parse one stream-json line and print a short summary to stderr.

    Returns the parsed event dict (or None if the line wasn't valid JSON), so
    callers can latch onto the final `result` event.

    Output is intentionally compact — one line per event — so a `tail -f` of
    the orchestrator log stays readable. We surface:
      - `system init`: model name
      - `assistant` text: first 140 chars
      - `assistant` tool_use: tool name + arg keys
      - `user` tool_result: ✓ or ✗
      - `result`: total duration + cost
    """
    line = line.strip()
    if not line:
        return None
    try:
        event = json.loads(line)
    except json.JSONDecodeError:
        return None
    if not isinstance(event, dict):
        return None

    label = f"[{agent}]"
    t = event.get("type")

    if t == "system" and event.get("subtype") == "init":
        model = event.get("model", "?")
        print(f"{label} init (model={model})", file=sys.stderr, flush=True)

    elif t == "assistant":
        msg = event.get("message", {}) or {}
        content = msg.get("content", []) or []
        for block in content:
            if not isinstance(block, dict):
                continue
            btype = block.get("type")
            if btype == "text":
                text = (block.get("text") or "").strip()
                if text:
                    snippet = text[:140].replace("\n", " ")
                    print(f"{label} assistant: {snippet}", file=sys.stderr, flush=True)
            elif btype == "tool_use":
                name = block.get("name", "?")
                inp = block.get("input")
                if isinstance(inp, dict):
                    keys = ", ".join(list(inp.keys())[:3])
                else:
                    keys = ""
                print(f"{label} tool: {name}({keys})", file=sys.stderr, flush=True)

    elif t == "user":
        msg = event.get("message", {}) or {}
        content = msg.get("content", []) or []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "tool_result":
                marker = "✗" if block.get("is_error") else "✓"
                print(f"{label} tool result: {marker}", file=sys.stderr, flush=True)

    elif t == "result":
        if event.get("is_error"):
            print(f"{label} DONE (ERROR)", file=sys.stderr, flush=True)
        else:
            cost = event.get("total_cost_usd", 0) or 0
            duration_ms = event.get("duration_ms", 0) or 0
            print(
                f"{label} DONE ({duration_ms / 1000:.1f}s, ${cost:.3f})",
                file=sys.stderr,
                flush=True,
            )

    return event


# Permission mode for the spawned worker `claude`. Defaults to bypassPermissions
# (the harness runs as an unattended batch driver, so the worker can't stop for
# interactive approval). Some environments — notably enterprise managed settings
# with `disableBypassPermissionsMode` — refuse bypassPermissions, leaving the
# headless worker unable to write its outputs (outcome.md, code). The env var
# `REDTEAM_CLAUDE_PERMISSION_MODE` lets the operator pick a policy-compatible mode
# (e.g. "acceptEdits", which auto-accepts file writes without bypass). Restricted
# to known Claude Code modes so a typo fails loud instead of silently weakening
# (or breaking) the gate.
_VALID_PERMISSION_MODES = frozenset({"bypassPermissions", "acceptEdits", "default", "plan"})


def _worker_permission_mode() -> str:
    mode = os.environ.get("REDTEAM_CLAUDE_PERMISSION_MODE", "bypassPermissions").strip()
    if mode not in _VALID_PERMISSION_MODES:
        raise ValueError(
            f"REDTEAM_CLAUDE_PERMISSION_MODE={mode!r} is not a recognized Claude Code "
            f"permission mode. Use one of: {', '.join(sorted(_VALID_PERMISSION_MODES))}."
        )
    return mode


def _worker_allowed_tools() -> list[str]:
    """Optional `--allowedTools` allowlist for the spawned worker.

    bypassPermissions pre-approves every tool. Under a non-bypass mode like
    acceptEdits (forced by enterprise managed settings) only file edits are
    auto-accepted — shell tools (Bash/PowerShell) still prompt and so fail
    headless, which stops the implementer from running ruff/pytest to
    self-verify. `REDTEAM_CLAUDE_ALLOWED_TOOLS` (space- or comma-separated)
    pre-approves the named tools so the worker can self-verify under such a
    policy. Default empty → no `--allowedTools` flag (behavior unchanged).
    """
    raw = os.environ.get("REDTEAM_CLAUDE_ALLOWED_TOOLS", "").strip()
    if not raw:
        return []
    return [tool for tool in raw.replace(",", " ").split() if tool]


def run_claude(
    *,
    agent: str,
    prompt: str,
    cwd: Path | None = None,
    timeout_sec: int = DEFAULT_TIMEOUT_SEC,
    model: str | None = None,
) -> ClaudeRunResult:
    """Invoke `claude --print` with the named sub-agent and stream its output.

    Uses `--output-format stream-json` so the orchestrator can print a live
    one-line summary of every event (init / assistant message / tool use /
    tool result / final result) to stderr while the agent runs. The final
    `type: "result"` event is captured into `parsed_json` for the caller.
    """
    cmd = [
        "claude",
        "--agent",
        agent,
        "--permission-mode",
        _worker_permission_mode(),
    ]
    allowed_tools = _worker_allowed_tools()
    if allowed_tools:
        # --allowedTools consumes values up to the next flag (--output-format).
        cmd += ["--allowedTools", *allowed_tools]
    cmd += [
        "--output-format",
        "stream-json",
        "--verbose",  # required when --output-format=stream-json
        "--print",
        prompt,
    ]
    if model is not None:
        cmd[1:1] = ["--model", model]
    try:
        proc = subprocess.Popen(
            cmd,
            cwd=str(cwd) if cwd else None,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",  # #32: pin UTF-8 so non-ASCII stream output doesn't crash on a cp949 default
            bufsize=1,  # line-buffered
        )
    except FileNotFoundError:
        return ClaudeRunResult(
            returncode=127,
            stdout="",
            stderr="`claude` executable not found on PATH",
            parsed_json=None,
        )

    deadline = time.monotonic() + timeout_sec
    raw_lines: list[str] = []
    final_result: dict | None = None

    assert proc.stdout is not None  # subprocess.PIPE means stdout is a pipe

    print(f"[{agent}] starting…", file=sys.stderr, flush=True)
    try:
        for line in proc.stdout:
            if time.monotonic() > deadline:
                proc.kill()
                proc.wait(timeout=5)
                stderr_tail = proc.stderr.read() if proc.stderr else ""
                print(
                    f"[{agent}] TIMEOUT after {timeout_sec}s",
                    file=sys.stderr,
                    flush=True,
                )
                return ClaudeRunResult(
                    returncode=124,
                    stdout="".join(raw_lines),
                    stderr=f"timeout after {timeout_sec}s\n{stderr_tail[:2000]}",
                    parsed_json=final_result,
                )
            raw_lines.append(line)
            event = _print_stream_event(line, agent)
            if event is not None and event.get("type") == "result":
                final_result = event
    except Exception as e:
        proc.kill()
        proc.wait(timeout=5)
        return ClaudeRunResult(
            returncode=125,
            stdout="".join(raw_lines),
            stderr=f"stream read error: {e!r}",
            parsed_json=final_result,
        )

    proc.wait()
    stderr_output = proc.stderr.read() if proc.stderr else ""

    return ClaudeRunResult(
        returncode=proc.returncode,
        stdout="".join(raw_lines),
        stderr=stderr_output,
        parsed_json=final_result,
    )


def parse_review_decision(text: str) -> ReviewDecision:
    """Pull the LAST `REVIEW_DECISION:` line from a review document.

    Reviewer agents (`test-verifier`, `code-security-reviewer`) emit
    `REVIEW_DECISION: APPROVED` or `REVIEW_DECISION: CHANGES_REQUESTED` on the
    final line. Anything else returns "MISSING" so the orchestrator can flag
    a malformed review rather than silently approving.
    """
    lines = [line.rstrip() for line in text.strip().splitlines()]
    for line in reversed(lines):
        if line.startswith("REVIEW_DECISION:"):
            value = line.split(":", 1)[1].strip()
            if value in {"APPROVED", "CHANGES_REQUESTED", "RESCUE_REQUIRED", "ASK_USER"}:
                return value  # type: ignore[return-value]
            return "MISSING"
    return "MISSING"


def compute_repo_diff(cwd: Path | None = None) -> str:
    """Return the current working-tree diff for stall detection."""
    proc = subprocess.run(
        ["git", "diff"],
        cwd=str(cwd) if cwd else None,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    return proc.stdout


def git_rev_parse(ref: str, repo: Path) -> str:
    """Return the full SHA of a git ref. Raises RuntimeError on failure (ref not found or git error).
    Used by the orchestrator pin step and the freeze guard inside `pinned_base_branch`."""
    proc = subprocess.run(
        ["git", "rev-parse", ref],
        cwd=str(repo),
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"git rev-parse {ref!r} failed (exit {proc.returncode})")
    return proc.stdout.strip()


def pinned_base_branch(state: dict[str, Any], repo: Path) -> str:
    """The reviewed-range base branch PINNED at task-branch creation (#91), read by
    every per-task consumer instead of live config so a mid-task edit to config.toml's
    `[project].base_branch` can't move the reviewed range or the PR base. Fail closed:
    raise if the pin is absent — the orchestrator pins it before any writable phase and
    fails closed for legacy unpinned state, so a missing pin here is a contract
    violation, never a silent fall back to (possibly worker-moved) live config.

    Centralized freeze guard: when `state["base_branch_sha"]` is recorded (dependent
    tasks only), re-resolves the live parent branch tip and raises if it moved. Root
    tasks (no recorded SHA) skip the guard — base is a config branch, no freeze needed.
    """
    base = state.get("base_branch")
    if not isinstance(base, str) or not base:
        raise ValueError(
            "state.base_branch is not pinned; refusing to derive the reviewed range from "
            "live config (#91). The orchestrator pins it before any writable phase runs."
        )
    sha = state.get("base_branch_sha")
    if isinstance(sha, str) and sha:
        try:
            live_sha = git_rev_parse(base, repo)
        except RuntimeError as exc:
            raise ValueError(
                f"freeze guard: could not re-resolve parent branch {base!r} to verify its tip: {exc}"
            ) from exc
        if live_sha != sha:
            raise ValueError(
                f"freeze guard: parent branch {base!r} tip moved from {sha!r} to {live_sha!r} "
                "after this task was pinned (amended or force-pushed). "
                "Do NOT proceed — the reviewed range and the PR base are no longer consistent."
            )
    return base


def compute_branch_diff(cwd: Path | None = None, base_branch: str | None = None) -> str:
    """Return the task branch diff against the base branch, including tracked
    uncommitted changes (committed + unstaged + staged). NOTE: plain `git diff` does
    NOT include UNTRACKED files, so a brand-new file appears here only once it is
    staged/committed (the agent-pair commit step stages new files before regenerating
    the patch). `base_branch` is the per-task PINNED base (#91) when called from a task
    phase; None falls back to live config (only safe for non-task callers)."""
    base = cwd or repo_root()
    base_branch = base_branch or project_config().base_branch
    committed = subprocess.run(
        ["git", "diff", f"{base_branch}...HEAD"],
        cwd=str(base),
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    ).stdout
    unstaged = subprocess.run(
        ["git", "diff"],
        cwd=str(base),
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    ).stdout
    staged = subprocess.run(
        ["git", "diff", "--cached"],
        cwd=str(base),
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    ).stdout
    return committed + unstaged + staged


def compute_branch_changed_paths(cwd: Path | None = None, base_branch: str | None = None) -> list[str]:
    """Paths changed on the task branch vs the base (committed + unstaged + staged),
    as ground truth — NOT parsed from patch headers.

    Uses `git diff -z --name-only` (NUL-delimited) with `core.quotepath=false`, so
    paths with spaces, non-ASCII, or other special characters are returned exactly,
    never mangled or silently dropped. This matters for the tier downgrade guard
    (a missed path would fail OPEN, letting a downgrade bypass slip through). `base_branch`
    is the per-task PINNED base (#91) when called from a task phase; None falls back to
    live config (only safe for non-task callers)."""
    base = cwd or repo_root()
    base_branch = base_branch or project_config().base_branch
    diff_args = (
        ["diff", "-z", "--name-only", f"{base_branch}...HEAD"],
        ["diff", "-z", "--name-only"],
        ["diff", "-z", "--name-only", "--cached"],
    )
    seen: set[str] = set()
    paths: list[str] = []
    for args in diff_args:
        out = subprocess.run(
            ["git", "-c", "core.quotepath=false", *args],
            cwd=str(base),
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
        ).stdout
        for p in out.split("\0"):
            if p and p not in seen:
                seen.add(p)
                paths.append(p)
    return paths


def extract_verification_commands(outcome_text: str) -> list[str]:
    """Extract command list from the fenced yaml block under `## Verification`."""
    lines = outcome_text.splitlines()
    in_verification = False
    in_yaml = False
    block_seen = False
    commands: list[str] = []
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("## "):
            if in_verification and in_yaml:
                break
            in_verification = stripped == "## Verification"
            in_yaml = False
            continue
        if not in_verification:
            continue
        if stripped.startswith("```"):
            if not in_yaml and stripped in {"```yaml", "```yml"}:
                in_yaml = True
                block_seen = True
                continue
            if in_yaml:
                break
        if in_yaml and stripped.startswith("- "):
            command = stripped[2:].strip().strip("\"'")
            if command:
                commands.append(command)
    if block_seen and not commands:
        raise ValueError("Verification yaml block exists but contains no `- command` entries")
    if not block_seen:
        raise ValueError("Missing `## Verification` fenced yaml block")
    return commands


def validate_verification_commands(
    commands: list[str],
    project_verify_command: str | None = None,
    allowlist: tuple[str, ...] | list[str] | None = None,
) -> list[list[str]]:
    """Return argv commands after enforcing a small verification-only allowlist.

    The project's configured `verify_command` (`.redteam/config.toml [project]`)
    is project-authored — trusted as much as the repo's own scripts — so its
    EXACT argv is allowed even if it names a non-allowlisted executable or a
    path (e.g. `bash .redteam/scripts/verify.sh`, or `npm test` for a JS repo).
    Any other command must name a tool from the project's configured
    `verification_allowlist` (or `python -m <tool>`), so an LLM-authored
    outcome.md cannot smuggle an arbitrary command — only the project-declared
    bare tools or the one project-declared verify_command.

    `project_verify_command` and `allowlist` let the caller pin the PLAN-TIME
    values so re-validation after the implementer ran does not depend on the
    (possibly mutated) current config — the agent-pair path passes the
    snapshotted values. When BOTH are None the current config is read once
    (fail-loud on a malformed config.toml). Pass them together (from one config
    load) so the verify_command and allowlist cannot come from different reads.
    """
    if project_verify_command is None and allowlist is None:
        from config import load_config  # lazy, mirrors default_model_for_role

        # Let load_config() fail loud on a malformed config.toml (unknown key /
        # bad type / empty) — every caller already handles the ValueError, so a
        # broken config surfaces as a verification failure rather than being
        # silently treated as "no configured verifier" (masking the error).
        proj = load_config(repo_root()).project
        project_verify_command = proj.verify_command
        allowlist = proj.verification_allowlist
    elif allowlist is None:
        # A caller pinned verify_command but not the allowlist. Do NOT fall back
        # to live config (that reintroduces the drift this snapshotting fixes);
        # the caller is responsible for passing the pinned allowlist too.
        raise ValueError(
            "verification allowlist not provided alongside a pinned verify_command "
            "(legacy/partial state) — re-run planning to snapshot it."
        )

    allowed_tools = set(allowlist)
    allowed_python_modules = set(allowlist)
    shell_metachars = {";", "|", "&&", "||", ">", "<", "`", "$("}
    project_verify_argv = shlex.split(project_verify_command) if project_verify_command else []
    validated: list[list[str]] = []

    for command in commands:
        try:
            argv = shlex.split(command)
        except ValueError as exc:
            raise ValueError(f"Invalid verification command syntax: {command!r}") from exc
        if not argv:
            raise ValueError("Empty verification command")

        if project_verify_argv and argv == project_verify_argv:
            validated.append(argv)
            continue

        if "/" in argv[0] or argv[0].startswith("."):
            raise ValueError(
                f"Verification executables must be bare names ({', '.join(sorted(allowed_tools))}) "
                "or the project-configured verify_command. "
                f"Got: {argv[0]!r}"
            )
        if any(any(meta in arg for meta in shell_metachars) for arg in argv):
            raise ValueError(f"Verification command contains shell metacharacters: {command!r}")

        executable = argv[0]
        if executable in allowed_tools:
            validated.append(argv)
            continue

        if len(argv) >= 3 and executable == "python" and argv[1] == "-m" and argv[2] in allowed_python_modules:
            validated.append(argv)
            continue
        if len(argv) >= 3 and executable == "python3" and argv[1] == "-m" and argv[2] in allowed_python_modules:
            validated.append(argv)
            continue

        tools = ", ".join(sorted(allowed_tools))
        raise ValueError(
            f"Unsafe or unsupported verification command. Allowed: {tools}, "
            f"python -m <{tools}>, or the project-configured verify_command. "
            f"Got: {command!r}"
        )

    return validated


def build_prompt_with_feedback(base_prompt: str, feedback: str | None) -> str:
    """Append rejection feedback from a previous attempt onto a phase prompt."""
    if not feedback:
        return base_prompt
    return (
        base_prompt + "\n\n## Previous attempt was rejected — address every item below before retrying.\n\n" + feedback
    )


def read_text_if_exists(path: Path) -> str | None:
    """Return file contents or None if the file is absent."""
    if not path.exists():
        return None
    return path.read_text(encoding="utf-8")


def project_config():
    """The project config block (doc paths, source/test dirs, branch prefix).

    Phase runners inject these into the worker/reviewer prompt so the agent
    definitions stay generic: the project-specific paths come from
    `.redteam/config.toml [project]` at runtime, not hardcoded in the prompt or
    the `.claude/agents/*.md` bodies. Lazy import mirrors default_model_for_role.
    """
    from config import load_config

    return load_config(repo_root()).project


# ---------- shared git primitives (fail-closed) ----------
#
# Both `implement` (agent-pair + tdd) and `write_test` (tdd) commit worker output to
# the task branch, so the low-level git plumbing lives here as ONE implementation —
# every call checks the return code and OMITS stderr from any error (a git error
# message can carry a credentialed remote URL, IR-002). The commit/stage path must
# never proceed on a silently-failed git op: that would hand review (and the PR) a
# stale or incomplete committed range.


def run_git_checked(args: list[str], cwd: Path, *, stdin: str | None = None) -> subprocess.CompletedProcess[str]:
    """Run a git command and FAIL CLOSED on a non-zero exit (stderr omitted, IR-002)."""
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


def untracked_files(cwd: Path) -> set[str]:
    """Non-ignored untracked paths, NUL-delimited so special-character names are exact.
    Fail-closed (raises) on a git failure."""
    out = run_git_checked(
        ["-c", "core.quotepath=false", "ls-files", "--others", "--exclude-standard", "-z"], cwd
    ).stdout
    return {p for p in out.split("\0") if p}


def commit_paths(cwd: Path, paths: list[str], message: str) -> bool:
    """Stage and commit EXACTLY `paths` and nothing else. Returns whether a commit was
    made. Fail-closed: any git error raises. A `path` that no longer exists stages as a
    deletion (the caller may pass an owned-but-deleted test path on purpose).

    Scope discipline (#82): the commit is `--only` the named paths, and the
    "anything to commit?" probe is `git diff --cached -- <paths>` scoped to them — so a
    pre-existing STAGED operator change elsewhere in the index is neither mistaken for
    our change nor swept into our commit. Paths are passed as a LITERAL, NUL-delimited
    pathspec list (stage/commit) so pathspec magic `:(...)` or special chars can't match
    unintended files; the scoped probe passes them as literal argv after `--` (safe —
    `valid_tdd_test_files` already rejects NUL/control chars, and the implement caller's
    paths come from git itself)."""
    if not paths:
        return False
    nul = "\0".join(paths)
    run_git_checked(["--literal-pathspecs", "add", "--pathspec-from-file=-", "--pathspec-file-nul"], cwd, stdin=nul)
    cached = subprocess.run(
        ["git", "--literal-pathspecs", "diff", "--cached", "--quiet", "--", *paths],
        cwd=str(cwd),
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    if cached.returncode == 0:
        return False  # nothing staged FOR OUR PATHS (already committed / unchanged)
    if cached.returncode != 1:
        # 0 = no diff, 1 = diff present; anything else is a real git error → fail closed.
        raise RuntimeError(f"git diff --cached --quiet failed (exit {cached.returncode})")
    # `--only` commits just the named paths, leaving any unrelated staged index intact.
    run_git_checked(
        ["--literal-pathspecs", "commit", "-m", message, "--only", "--pathspec-from-file=-", "--pathspec-file-nul"],
        cwd,
        stdin=nul,
    )
    return True


def normalized_test_root(proj: Any) -> str:
    """The project's test dir as a canonical, trailing-slash POSIX prefix. The single
    source of truth for "is this path a test file?" — manifest validation AND write_test
    discovery use it, so a non-canonical config test_dir can't make them disagree (#82)."""
    import posixpath

    root = posixpath.normpath(proj.test_dir.replace("\\", "/"))
    return root if root.endswith("/") else root + "/"


def valid_tdd_test_files(value: Any, proj: Any) -> list[str]:
    """Shape/ownership validation of the `tdd_test_files` manifest (#82), applied on
    EVERY read and INDEPENDENT of whether the path currently exists (a worker may have
    deleted an owned test — existence/symlink is filtered separately, at persist/stage
    time). A malformed or hand-edited state can therefore never smuggle an arbitrary
    path into `commit_paths`.

    Cross-platform explicit (the repo supports cp949/Windows hosts): each entry must be
    a string, contain no backslash, be repo-relative (not absolute), have no `..`
    segment, sit under the once-normalized POSIX `test_dir`, and have a basename
    matching `test_file_glob`. Returns the de-duplicated, sorted, validated list.
    Raises ValueError on any violation (fail closed)."""
    import posixpath
    from fnmatch import fnmatch

    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(p, str) for p in value):
        raise ValueError("tdd_test_files must be a list of strings")

    test_root = normalized_test_root(proj)

    out: list[str] = []
    seen: set[str] = set()
    for raw in value:
        if any(ord(c) < 0x20 for c in raw):
            # A NUL (or other control char) would split into extra pathspecs at the
            # `--pathspec-file-nul` staging boundary — reject so a crafted entry can't
            # smuggle a second, out-of-scope path into the commit.
            raise ValueError(f"tdd_test_files entry has a control character: {raw!r}")
        if "\\" in raw:
            raise ValueError(f"tdd_test_files entry has a backslash (use forward slashes): {raw!r}")
        if raw.startswith("/") or (len(raw) > 1 and raw[1] == ":"):
            raise ValueError(f"tdd_test_files entry is not repo-relative: {raw!r}")
        if ".." in raw.split("/"):
            raise ValueError(f"tdd_test_files entry has a '..' segment: {raw!r}")
        norm = posixpath.normpath(raw)
        if norm.startswith("../") or norm == ".." or norm.startswith("/"):
            raise ValueError(f"tdd_test_files entry escapes the repo: {raw!r}")
        if not (norm + "/").startswith(test_root):
            raise ValueError(f"tdd_test_files entry is not under {test_root!r}: {raw!r}")
        if not fnmatch(posixpath.basename(norm), proj.test_file_glob):
            raise ValueError(f"tdd_test_files entry does not match {proj.test_file_glob!r}: {raw!r}")
        if norm not in seen:
            seen.add(norm)
            out.append(norm)
    return sorted(out)


def claude_model_for_role(state: dict, role: str) -> str | None:
    """Return the configured Claude model for a role, ignoring non-Claude owners.

    `state.models.reviewer` and `state.models.rescue` are `codex` by default in
    agent-pair mode. Those values describe ownership, not a valid `claude
    --model` target, so legacy Claude reviewer phases keep the CLI default.
    """
    models = state.get("models")
    configured = models.get(role) if isinstance(models, dict) else None
    model = configured or default_model_for_role(role)
    if not isinstance(model, str):
        return None
    if model == "codex":
        return None
    return model
