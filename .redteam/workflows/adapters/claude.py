"""Claude adapters.

- `ClaudeWorkerAdapter` (mutating) wraps `run_claude` (`claude --print --agent`).
- `ClaudeReviewerAdapter` (read-only) runs `claude -p --permission-mode plan` to
  produce a review headless — the Claude counterpart of the codex reviewer, so
  `state.models.reviewer="claude"` works, not just "codex".

The mutating worker/planner phases used to call `run_claude` directly; they now
go through the worker adapter so the core no longer hardcodes the provider.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from phase_runners._base import claude_model_for_role, parse_review_decision, run_claude

from ._protocol import ReviewerCapabilities, ReviewResult, ReviewTarget, WorkerRunResult

# A read-only review can still take a while (repo exploration + reasoning).
_REVIEWER_TIMEOUT_SEC = 900

_MANUAL_HINT = 'To review this task by hand instead, set state.models.reviewer="human" and resume.'


class ClaudeWorkerAdapter:
    name = "claude-code"

    def __init__(self, state: dict) -> None:
        # Held so the adapter resolves the per-role Claude model the same way
        # the legacy call sites did (claude_model_for_role(state, role)).
        self._state = state

    def invoke(self, *, role: str, agent: str, prompt: str, cwd: Path) -> WorkerRunResult:
        result = run_claude(
            agent=agent,
            prompt=prompt,
            cwd=cwd,
            model=claude_model_for_role(self._state, role),
        )
        return WorkerRunResult(
            returncode=result["returncode"],
            stdout=result["stdout"],
            stderr=result["stderr"],
        )


class ClaudeReviewerAdapter:
    """Headless read-only reviewer via the `claude` CLI.

    Mirrors `CodexReviewerAdapter`'s fail-closed contract (non-zero exit /
    timeout / unparseable → MISSING+error, never a silent approval) and reads
    `git diff` directly per the target descriptor. Read-only is enforced with
    `--permission-mode plan` (the codex `--sandbox read-only` equivalent) plus an
    explicit `--disallowedTools` for the mutating tools. The final decision text
    comes from `--output-format json` (`.result`).

    NOTE: the exact claude flags (`--permission-mode plan`, `--allowedTools`,
    `--disallowedTools`, `--output-format json`) should be re-verified against
    `claude --help` before public release; they are current as of early 2026.
    """

    name = "claude"
    capabilities: ReviewerCapabilities = {
        "native_diff_review": False,
        "timeout_sec": _REVIEWER_TIMEOUT_SEC,
    }

    def review(self, *, role: str, prompt: str, cwd: Path, target: ReviewTarget) -> ReviewResult:
        timeout_sec = self.capabilities["timeout_sec"]
        cmd = [
            "claude",
            "-p",
            prompt,
            "--permission-mode",
            "plan",
            "--allowedTools",
            # Read-only git only — NOT a broad Bash(git *), which would permit
            # mutating subcommands (commit/checkout/reset) if plan mode didn't
            # block them. Narrow to the read subcommands a reviewer needs.
            "Read,Grep,Glob,Bash(git diff *),Bash(git log *),Bash(git show *),Bash(git status *)",
            "--disallowedTools",
            "Edit,Write,NotebookEdit",
            "--output-format",
            "json",
        ]
        try:
            proc = subprocess.run(
                cmd,
                cwd=str(cwd),
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=timeout_sec,
            )
        except FileNotFoundError:
            return ReviewResult(
                decision="MISSING",
                raw=f"`claude` executable not found on PATH. Install the Claude CLI. {_MANUAL_HINT}",
                parse_status="error",
            )
        except subprocess.TimeoutExpired:
            return ReviewResult(
                decision="MISSING",
                raw=f"claude review timed out after {timeout_sec}s. {_MANUAL_HINT}",
                parse_status="error",
            )

        out = proc.stdout or ""
        if proc.returncode != 0:
            # Fail closed on ANY non-zero exit. stderr is intentionally omitted —
            # it can carry the request URL / credentials, and truncation is not
            # redaction (cf. the codex adapter + the telegram token-in-log
            # incident). stdout is the review body so a short tail aids debugging.
            return ReviewResult(
                decision="MISSING",
                raw=(
                    f"claude review failed (rc={proc.returncode}). stdout tail:\n{out[-500:]}\n"
                    f"(stderr omitted — it can carry credentials.) {_MANUAL_HINT}"
                ),
                parse_status="error",
            )

        # `claude --output-format json` emits a single JSON OBJECT with the final
        # assistant text in `.result` (a string). Anything else fails closed: a
        # non-object payload or a non-string/`null` `result` must NOT reach
        # parse_review_decision (it would raise rather than return MISSING).
        try:
            parsed = json.loads(out)
        except json.JSONDecodeError:
            return ReviewResult(
                decision="MISSING",
                raw=f"claude review produced unparseable JSON output. stdout tail:\n{out[-500:]}\n{_MANUAL_HINT}",
                parse_status="error",
            )
        result_text = parsed.get("result") if isinstance(parsed, dict) else None
        if not isinstance(result_text, str):
            return ReviewResult(
                decision="MISSING",
                raw=f"claude review JSON had no string 'result' field. stdout tail:\n{out[-500:]}\n{_MANUAL_HINT}",
                parse_status="error",
            )

        decision = parse_review_decision(result_text)
        return ReviewResult(
            decision=decision,
            raw=result_text,
            parse_status="ok" if decision != "MISSING" else "unparseable",
        )
