"""Codex adapters — review (read-only) and worker (workspace-write) via `codex exec`.

`CodexReviewerAdapter` is the headless review gate; `CodexWorkerAdapter` (A2,
role reversal) lets codex write the code when `state.models.implementer ==
"codex"`. They differ only in sandbox mode (read-only vs workspace-write) and
result contract (ReviewResult vs WorkerRunResult).

Sends the review prompt on stdin to `codex exec --sandbox read-only` and
returns the raw stdout plus the parsed REVIEW_DECISION. Read-only sandbox: it
never writes the working tree. Fails closed on a non-zero exit, a timeout, or
an unparseable result, so a failed/partial run is never read as an approval.

native_diff_review is False: this adapter has the reviewer read `git diff`
directly (per the target descriptor). A `codex review --base` adapter that
enables enforceable diff-completeness is a later milestone.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from phase_runners._base import DEFAULT_TIMEOUT_SEC, parse_review_decision

from ._protocol import ReviewerCapabilities, ReviewResult, ReviewTarget, WorkerRunResult

# Codex reviews can run several minutes (repo exploration + reasoning).
_DEFAULT_TIMEOUT_SEC = 900

# Actionable hints appended to fail-closed errors so the operator knows what to
# do (the gate stops rather than silently approving).
_MANUAL_HINT = 'To review this task by hand instead, set state.models.reviewer="human" and resume.'
_AUTH_HINT = (
    "This often means the codex login expired or is missing. Run `codex login status` "
    "(and `codex login` if needed), then re-run `orchestrator resume`. " + _MANUAL_HINT
)


class CodexReviewerAdapter:
    name = "codex"
    capabilities: ReviewerCapabilities = {
        "native_diff_review": False,
        "timeout_sec": _DEFAULT_TIMEOUT_SEC,
        "read_only_enforced": True,  # codex exec --sandbox read-only
    }

    def review(self, *, role: str, prompt: str, cwd: Path, target: ReviewTarget) -> ReviewResult:
        timeout_sec = self.capabilities["timeout_sec"]
        cmd = ["codex", "exec", "--sandbox", "read-only", "-"]
        try:
            proc = subprocess.run(
                cmd,
                input=prompt,
                cwd=str(cwd),
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=timeout_sec,
            )
        except FileNotFoundError:
            return ReviewResult(
                decision="MISSING",
                raw=f"`codex` executable not found on PATH. Install the codex CLI. {_MANUAL_HINT}",
                parse_status="error",
            )
        except subprocess.TimeoutExpired:
            # A hung / over-long reviewer must fail closed, not block the batch.
            return ReviewResult(
                decision="MISSING",
                raw=f"codex exec timed out after {timeout_sec}s. {_AUTH_HINT}",
                parse_status="error",
            )

        raw = proc.stdout or ""
        if proc.returncode != 0:
            # Fail closed on ANY non-zero exit — a partial/failed run must never
            # be read as an approval even if stdout happens to end with one.
            # stderr is intentionally NOT included: codex can put the request
            # URL / credentials there, and truncation is not redaction (cf. the
            # telegram token-in-log incident). stdout is the review body (no
            # secret) so a short tail is kept for debugging.
            return ReviewResult(
                decision="MISSING",
                raw=(
                    f"codex exec failed (rc={proc.returncode}). stdout tail:\n{raw[-500:]}\n"
                    f"(stderr omitted — it can carry credentials.) {_AUTH_HINT}"
                ),
                parse_status="error",
            )

        decision = parse_review_decision(raw)
        return ReviewResult(
            decision=decision,
            raw=raw,
            parse_status="ok" if decision != "MISSING" else "unparseable",
        )


def _load_agent_rules(agent: str, cwd: Path) -> str:
    """Return the body of `.claude/agents/<agent>.md` (frontmatter stripped).

    The Claude worker loads a sub-agent's operating rules via `--agent <name>`;
    Codex has no sub-agent concept, so those rules — which the phase prompts
    explicitly rely on ("Follow your agent definition exactly") — must be
    inlined into the prompt. Returns "" when the file is missing/unreadable or
    has no body; the caller (`invoke`) treats that as fatal and refuses to run
    the mutating worker unscoped (IR-001), since every worker phase ships with a
    checked-in agent definition, so an absent one means a broken install, not a
    runtime condition.
    """
    path = cwd / ".claude" / "agents" / f"{agent}.md"
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return ""
    # Strip a leading YAML frontmatter block (--- ... ---); keep the body, which
    # is the agent's system prompt. Codex doesn't consume the frontmatter
    # (name/model/allowed-tools) — those are Claude sub-agent metadata.
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end != -1:
            text = text[end + len("\n---") :].lstrip("\n")
    return text.strip()


def _sanitized_stderr(stderr: str | None) -> str:
    """Drop raw codex stderr, returning only a fixed omission note when present.

    `codex exec` stderr can carry the request URL / credentials (IR-002), and
    runners persist worker stderr into phase feedback/logs. Truncation is not
    redaction (cf. the telegram token-in-log incident), so the raw is never
    forwarded — the returncode + stdout carry the actionable signal.
    """
    if not (stderr or "").strip():
        return ""
    return "(codex exec stderr omitted — it can carry credentials; see stdout / returncode.)"


class CodexWorkerAdapter:
    """Codex worker adapter — mutating code generation via the `codex exec` CLI.

    The role-reversal counterpart to the Claude worker: with
    `state.models.implementer == "codex"`, the worker/planner phases run on
    codex instead of claude. Runs `codex exec --sandbox workspace-write` so the
    agent may write the working tree (vs the reviewer's read-only sandbox), with
    the prompt on stdin. Returns the same `WorkerRunResult` contract the runners
    already consume, so no runner changes: a non-zero returncode (incl.
    missing-binary 127 / timeout 124) makes the runner report the phase errored.

    Model selection: A2 doesn't pass `-m`. `config.models` carries the provider
    key "codex" (not a codex model name) for the worker roles, so codex uses its
    configured default model; per-model selection is a later milestone (the same
    deferral noted for the reviewer side).
    """

    name = "codex"

    def invoke(self, *, role: str, agent: str, prompt: str, cwd: Path) -> WorkerRunResult:
        rules = _load_agent_rules(agent, cwd)
        if not rules:
            # IR-001: codex has no `--agent`, so the agent definition is the only
            # thing that scopes a workspace-write run. If it's missing, refuse to
            # run the mutating worker unscoped — fail the phase before codex can
            # touch the tree, rather than degrading to a bare, rule-less prompt.
            return WorkerRunResult(
                returncode=2,
                stdout="",
                stderr=(
                    f"agent rules not found/empty at .claude/agents/{agent}.md — refusing to run "
                    "the codex worker unscoped. (codex has no --agent; the inlined rules are the "
                    "only scope on a workspace-write run.)"
                ),
            )
        full_prompt = (
            f'You are operating as the "{agent}" sub-agent in the redteam harness. '
            "Follow these operating rules exactly:\n\n"
            f"{rules}\n\n--- TASK ---\n\n{prompt}"
        )
        cmd = ["codex", "exec", "--sandbox", "workspace-write", "-"]
        try:
            proc = subprocess.run(
                cmd,
                input=full_prompt,
                cwd=str(cwd),
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=DEFAULT_TIMEOUT_SEC,
            )
        except FileNotFoundError:
            return WorkerRunResult(
                returncode=127,
                stdout="",
                stderr="`codex` executable not found on PATH. Install the codex CLI.",
            )
        except subprocess.TimeoutExpired:
            return WorkerRunResult(
                returncode=124,
                stdout="",
                stderr=f"codex exec timed out after {DEFAULT_TIMEOUT_SEC}s.",
            )
        # IR-002: never propagate raw codex stderr. `codex exec` can emit the
        # request URL / credentials there, and runners persist worker stderr into
        # phase feedback/logs (cf. the reviewer adapter, which omits it for the
        # same reason). stdout carries the work log and is returned as-is; a
        # non-zero returncode still signals the failure to the runner.
        return WorkerRunResult(
            returncode=proc.returncode,
            stdout=proc.stdout or "",
            stderr=_sanitized_stderr(proc.stderr),
        )
