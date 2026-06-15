"""Adapter registry + resolvers (reviewer + worker).

Capability-based, not provider-branched: runners ask `get_reviewer_adapter` /
`get_worker_adapter` which adapter owns the role for this task and call it —
they never hardcode "codex". Headless reviewer is the DEFAULT: an absent
`state.models.reviewer` inherits the config default (reviewer="codex" unless a
project overrides `.redteam/config.toml` [models]). A None reviewer result (only
when `reviewer` is explicitly a non-adapter value like "human") means the legacy
manual flow (human pastes the review + touches the sentinel). The worker
resolver always returns an adapter (the worker is never manual); it picks codex
vs claude from the configured implementer model.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from phase_runners._base import default_model_for_role

from ._protocol import ReviewerAdapter, ReviewResult, ReviewTarget, WorkerAdapter
from .claude import ClaudeReviewerAdapter, ClaudeWorkerAdapter
from .codex import CodexReviewerAdapter, CodexWorkerAdapter

# reviewer-role value in state.models → adapter factory.
_REVIEWER_ADAPTERS = {
    "codex": CodexReviewerAdapter,
    "claude": ClaudeReviewerAdapter,
}

# worker provider key (the implementer-role value in state.models) → mutating
# worker adapter factory. A claude model name is absent here and falls through
# to the Claude worker default.
_WORKER_ADAPTERS = {
    "codex": CodexWorkerAdapter,
}


def get_worker_adapter(state: dict[str, Any]) -> WorkerAdapter:
    """Resolve the mutating worker/planner provider from config.

    Selection keys off the *implementer* role — the canonical code-writing role
    (and the one pinned by the worker-resolver test). `implementer == "codex"`
    routes to the codex worker (role reversal: codex writes the code); anything
    else (a claude model name, the default) stays on the Claude worker. A
    "codex main" config sets planner+implementer = codex so every worker phase
    runs on codex; per-role worker mixing (planner≠implementer provider) is
    deferred, the same as the per-model deferral on the reviewer side.

    Routing through here keeps the core free of a hardcoded provider, so
    swapping the worker is a config change, not a runner change.
    """
    models = state.get("models")
    configured = models.get("implementer") if isinstance(models, dict) else None
    implementer = configured or default_model_for_role("implementer")
    factory = _WORKER_ADAPTERS.get(implementer) if isinstance(implementer, str) else None
    if factory is not None:
        return factory()
    return ClaudeWorkerAdapter(state)


def worker_provider(state: dict[str, Any]) -> str:
    """Provider family the worker/planner resolves to: "codex" or "claude".

    Mirrors `get_worker_adapter`'s selection (keyed off the implementer role)
    without instantiating an adapter, so a guard can compare provider identities
    cheaply. Anything not registered as a codex worker is the Claude default.
    """
    models = state.get("models")
    configured = models.get("implementer") if isinstance(models, dict) else None
    implementer = configured or default_model_for_role("implementer")
    if isinstance(implementer, str) and implementer in _WORKER_ADAPTERS:
        return "codex"
    return "claude"


def reviewer_provider(state: dict[str, Any]) -> str | None:
    """Provider family of the configured headless reviewer ("codex"/"claude"),
    or None when the reviewer is manual/human (no headless adapter).

    Mirrors `get_reviewer_adapter`'s resolution: a value that maps to a registered
    reviewer adapter returns that provider key; anything else (e.g. "human") is
    the manual flow and returns None — a human is always a distinct adversary.
    """
    models = state.get("models")
    configured = models.get("reviewer") if isinstance(models, dict) else None
    reviewer = configured or default_model_for_role("reviewer")
    if isinstance(reviewer, str) and reviewer in _REVIEWER_ADAPTERS:
        return reviewer
    return None


def get_reviewer_adapter(state: dict[str, Any]) -> ReviewerAdapter | None:
    """Resolve the reviewer role to a headless adapter, or None for the legacy
    manual flow.

    Mirrors `claude_model_for_role`: an absent `state.models` inherits the
    config default (reviewer="codex"), so a legacy agent-pair task with no
    models block still gets the headless reviewer rather than silently falling
    back to the manual sentinel path. An explicit non-codex value (e.g.
    "human") opts back out to manual.
    """
    models = state.get("models")
    configured = models.get("reviewer") if isinstance(models, dict) else None
    reviewer = configured or default_model_for_role("reviewer")
    factory = _REVIEWER_ADAPTERS.get(reviewer) if isinstance(reviewer, str) else None
    return factory() if factory is not None else None


# parse_status the fallback ladder uses to signal "no trusted automatic result —
# a human must review". Distinct from ok/unparseable/error so the engine routes
# it to the manual gate, never to a retry/defer/approval (#37).
MANUAL_REQUIRED = "manual_required"
_MANUAL_FALLBACKS = frozenset({"manual", "human"})
# Prefix on a successful-fallback review's raw, so the engine can record the audit
# trail in state for an automatic fallback approval too (not just the manual case).
FALLBACK_AUDIT_MARKER = "[redteam fallback]"


def _resolved_reviewer_fallback(state: dict[str, Any]) -> str:
    models = state.get("models")
    configured = models.get("reviewer_fallback") if isinstance(models, dict) else None
    return configured or default_model_for_role("reviewer_fallback") or "manual"


def _is_valid_result(result: ReviewResult) -> bool:
    """A trusted review result: cleanly parsed AND an actual decision. A MISSING
    decision (even if a future adapter mis-pairs it with parse_status "ok") is
    NOT trusted — defensive per the #37 plan."""
    return result["parse_status"] == "ok" and result["decision"] != "MISSING"


def _manual_required(audit: str) -> ReviewResult:
    return {"decision": "MISSING", "raw": audit, "parse_status": MANUAL_REQUIRED}


def review_with_fallback(
    state: dict[str, Any], *, role: str, prompt: str, cwd: Path, target: ReviewTarget
) -> ReviewResult:
    """Run the configured reviewer; on an INFRA failure (not a valid decision),
    apply the reviewer_fallback ladder — fail-closed (#37 step 4):

    - A valid parsed decision (incl. CHANGES_REQUESTED / RESCUE_REQUIRED /
      ASK_USER) is returned as-is; it is NEVER a fallback trigger.
    - fallback "manual"/"human" → a `manual_required` result (the engine blocks
      for a pasted review; never an automatic approval).
    - fallback = a provider → its APPROVED is trusted ONLY if it is cross-provider
      (≠ the worker provider, else self-review #28), read_only_enforced, and its
      own parse is a valid decision; otherwise → `manual_required`.
    - The returned `raw` records the audit (primary failure + fallback outcome).
    """
    primary = get_reviewer_adapter(state)
    if primary is None:  # defensive: callers handle the manual flow before here
        return _manual_required("no headless reviewer adapter configured — manual review required")
    result = primary.review(role=role, prompt=prompt, cwd=cwd, target=target)
    if _is_valid_result(result):
        return result

    primary_name = reviewer_provider(state) or primary.name
    audit = f"primary reviewer '{primary_name}' failed (parse_status={result['parse_status']}, decision={result['decision']})."
    fb = _resolved_reviewer_fallback(state)

    if fb in _MANUAL_FALLBACKS:
        return _manual_required(
            f"{audit} Fallback policy is manual — human review required.\n\n{result['raw'][-2000:]}"
        )
    fb_factory = _REVIEWER_ADAPTERS.get(fb)
    if fb_factory is None:
        return _manual_required(f"{audit} Fallback '{fb}' is not a known reviewer adapter — manual review required.")
    if fb == worker_provider(state):
        return _manual_required(
            f"{audit} Fallback '{fb}' is the worker's own provider (self-review) — manual review required."
        )
    fb_adapter = fb_factory()
    if not fb_adapter.capabilities.get("read_only_enforced"):
        return _manual_required(f"{audit} Fallback '{fb}' is not read-only-enforced — manual review required.")

    fb_result = fb_adapter.review(role=role, prompt=prompt, cwd=cwd, target=target)
    if _is_valid_result(fb_result):
        audit_line = f"{audit} Fell back to '{fb}'."
        return {
            "decision": fb_result["decision"],
            # The marker stays in raw for a HUMAN reading the persisted artifact; the
            # engine trusts the structured fallback_audit field, not this text.
            "raw": f"{FALLBACK_AUDIT_MARKER} {audit_line}\n\n{fb_result['raw']}",
            "parse_status": "ok",
            "fallback_audit": audit_line,
        }
    return _manual_required(
        f"{audit} Fallback '{fb}' also failed (parse_status={fb_result['parse_status']}) — manual review required.\n\n{fb_result['raw'][-2000:]}"
    )


__all__ = [
    "FALLBACK_AUDIT_MARKER",
    "MANUAL_REQUIRED",
    "ReviewerAdapter",
    "ReviewResult",
    "WorkerAdapter",
    "get_reviewer_adapter",
    "get_worker_adapter",
    "review_with_fallback",
    "reviewer_provider",
    "worker_provider",
]
