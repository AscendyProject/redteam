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

from typing import Any

from phase_runners._base import default_model_for_role

from ._protocol import ReviewerAdapter, ReviewResult, WorkerAdapter
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


__all__ = [
    "ReviewerAdapter",
    "ReviewResult",
    "WorkerAdapter",
    "get_reviewer_adapter",
    "get_worker_adapter",
]
