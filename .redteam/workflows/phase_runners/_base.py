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
import shlex
import subprocess
import sys
import time
from pathlib import Path
from typing import Literal, TypedDict


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


PhaseStatus = Literal["approved", "changes_requested", "rescue_required", "ask_user", "error"]
ReviewDecision = Literal["APPROVED", "CHANGES_REQUESTED", "RESCUE_REQUIRED", "ASK_USER", "MISSING"]


class PhaseResult(TypedDict):
    """Return shape every phase runner emits."""

    status: PhaseStatus
    feedback: str
    log: str
    diff: str


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
        "bypassPermissions",
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
        check=False,
    )
    return proc.stdout


def compute_branch_diff(cwd: Path | None = None) -> str:
    """Return the task branch diff against the configured base branch, including
    uncommitted changes."""
    base = cwd or repo_root()
    base_branch = project_config().base_branch
    committed = subprocess.run(
        ["git", "diff", f"{base_branch}...HEAD"],
        cwd=str(base),
        capture_output=True,
        text=True,
        check=False,
    ).stdout
    unstaged = subprocess.run(
        ["git", "diff"],
        cwd=str(base),
        capture_output=True,
        text=True,
        check=False,
    ).stdout
    staged = subprocess.run(
        ["git", "diff", "--cached"],
        cwd=str(base),
        capture_output=True,
        text=True,
        check=False,
    ).stdout
    return committed + unstaged + staged


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


def validate_verification_commands(commands: list[str], project_verify_command: str | None = None) -> list[list[str]]:
    """Return argv commands after enforcing a small verification-only allowlist.

    The project's configured `verify_command` (`.redteam/config.toml [project]`)
    is project-authored — trusted as much as the repo's own scripts — so its
    EXACT argv is allowed even if it names a non-allowlisted executable or a
    path (e.g. `bash .redteam/scripts/verify.sh`, or `npm test` for a JS repo).
    Any other command still falls through the restrictive allowlist
    (pytest/ruff/mypy), so an LLM-authored outcome.md cannot smuggle an
    arbitrary command — only the bare tools or the one project-declared command.

    `project_verify_command` lets the caller pin the PLAN-TIME verify command so
    re-validation after the implementer ran does not depend on the (possibly
    mutated) current config — the agent-pair path passes the snapshotted value.
    When None, the current config is read (fail-loud on a malformed config.toml).
    """
    # Keep this allowlist in sync with AGENTS.md "Allowed verification command families".
    allowed_tools = {"pytest", "ruff", "mypy"}
    allowed_python_modules = {"pytest", "ruff", "mypy"}
    shell_metachars = {";", "|", "&&", "||", ">", "<", "`", "$("}
    if project_verify_command is None:
        from config import load_config  # lazy, mirrors default_model_for_role

        # Let load_config() fail loud on a malformed config.toml (unknown key /
        # bad type / empty) — every caller already handles the ValueError, so a
        # broken config surfaces as a verification failure rather than being
        # silently treated as "no configured verifier" (masking the error).
        project_verify_command = load_config(repo_root()).project.verify_command
    project_verify_argv = shlex.split(project_verify_command)
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
                "Verification executables must be bare names (pytest, ruff, mypy) "
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

        raise ValueError(
            "Unsafe or unsupported verification command. Allowed: pytest, ruff, mypy, "
            "python -m pytest|ruff|mypy, or the project-configured verify_command. "
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
