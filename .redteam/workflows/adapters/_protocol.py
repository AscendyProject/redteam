"""Reviewer-adapter protocol — the model-agnostic seam for review gates.

The harness core (runners + orchestrator) addresses a *reviewer role*, never a
provider. A `ReviewerAdapter` encapsulates "how do I invoke this provider
headless and parse its result into the review decision contract". The Codex
adapter is the first implementation; a future Claude/Gemini/etc. adapter
implements the same protocol without the runners changing.

Read-only contract: a reviewer adapter only reads the repo/diff and returns a
decision — it never mutates the working tree.

Scope note (design #151, roadmap step 1): this carries a `ReviewTarget`
descriptor and `ReviewerCapabilities` so the core states WHAT is to be reviewed
and the adapter declares what it can do (incl. its hard timeout). Full
diff-completeness enforcement (artifact hash / byte count / truncation
reconciliation) is a later milestone — it belongs to a `codex review --base`
transition where the core can hash the diff and compare it against what the
reviewer actually saw. With `codex exec`, the reviewer reads `git diff`
directly, so the core passes the target descriptor and the adapter fails closed
on a non-zero/timeout/unparseable result.
"""

from __future__ import annotations

from pathlib import Path
from typing import NotRequired, Optional, Protocol, TypedDict

from phase_runners._base import ReviewDecision


class ReviewTarget(TypedDict):
    # What the core wants reviewed. "plan" = the outcome.md plan; "branch_diff"
    # = the working-branch diff against `base`.
    kind: str  # "plan" | "branch_diff"
    base: Optional[str]  # base branch for branch_diff (e.g. "main"); None for plan


class ReviewerCapabilities(TypedDict):
    # Has a dedicated base-diff review mode (e.g. `codex review --base`). False
    # for the current `codex exec` adapter; a True adapter is the milestone that
    # enables enforceable diff-completeness.
    native_diff_review: bool
    # Hard timeout; the adapter MUST fail closed (parse_status="error") on it.
    timeout_sec: int
    # The adapter runs the reviewer with NO write capability (read-only sandbox /
    # plan permission mode). The engine requires this True before trusting an
    # AUTOMATIC fallback APPROVED from this adapter (#37 adapter trust boundary).
    read_only_enforced: bool


class ReviewResult(TypedDict):
    # Parsed REVIEW_DECISION (APPROVED|CHANGES_REQUESTED|RESCUE_REQUIRED|ASK_USER|MISSING).
    decision: ReviewDecision
    # Full reviewer stdout — persisted as the review artifact (plan_review.md / code_review.md).
    raw: str
    # "ok" | "unparseable" | "error" — the core fails closed on anything but "ok".
    parse_status: str
    # Structured fallback provenance: set ONLY by review_with_fallback when an
    # automatic fallback produced this result. The engine trusts THIS (not any
    # in-band marker in `raw`) for the audit trail (#37 review PR-002).
    fallback_audit: NotRequired[str]


class ReviewerAdapter(Protocol):
    name: str
    capabilities: ReviewerCapabilities

    def review(self, *, role: str, prompt: str, cwd: Path, target: ReviewTarget) -> ReviewResult: ...


# --- Worker (mutating) adapter -------------------------------------------------
# Worker/planner phases invoke a provider that WRITES the working tree (produces
# outcome.md, tests, the implementation). Today that provider is Claude
# (`run_claude`); the adapter seam lets a different provider be configured
# without the runners changing. Full mutating enforcement (sandbox/write-scope
# declaration, changed-path validation) is a later milestone — for now the
# adapter wraps the existing run and the core verifies via the dedicated
# verification phase.


class WorkerRunResult(TypedDict):
    returncode: int
    stdout: str
    stderr: str


class WorkerAdapter(Protocol):
    name: str

    def invoke(self, *, role: str, agent: str, prompt: str, cwd: Path) -> WorkerRunResult: ...
