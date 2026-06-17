# Reviewer transport & the sub-agent adapter (#37 steps 5–6)

Status: **both steps DECIDED** — the multiplexer-transport (step 6) is
**REJECTED**, and the sub-agent reviewer adapter (step 5) is **REJECTED** too
(operator call, 2026-06-17; closes #67). Neither will be built; this note records
why and the guard rails should either ever be revisited.
Date: 2026-06-17. Context: #37, #67. Supersedes nothing.

Background: redteam drives the adversarial reviewer through the `ReviewerAdapter`
seam — headless CLIs today (`codex exec --sandbox read-only`,
`claude -p --permission-mode plan`). #37 step 4 (the fail-closed fallback ladder)
shipped in 0.3.0. This note records the remaining two steps.

---

## Decision (step 6): do NOT add a terminal-multiplexer transport

**Rejected.** A transport that sends prompts to a live interactive agent pane in a
multiplexer (tmux/screen/etc.) and scrapes the pane for the review is not worth
its cost:

- It reintroduces exactly the fragility the adapter seam exists to avoid:
  ambiguous pane addressing, bracketed-paste swallowing input,
  completion-detection heuristics, and cross-platform multiplexer detection.
- The `ReviewerAdapter` protocol is already the single, robust point of variation;
  visibility/resilience belong there, not in a screen-scraper.
- Screen-scraped output is untrusted by construction — it must never be read as an
  automatic `APPROVED`.

**Guard rail if ever revisited:** a multiplexer/interactive transport may exist
only if it is explicitly labelled *experimental/manual* and **cannot produce an
automatic APPROVED** — it may only feed the manual/sentinel flow (a human still
pastes/confirms the decision). It must never satisfy the engine's automatic
review gate.

---

## Decision (step 5): do NOT build a sub-agent reviewer adapter

**Rejected** (operator call, 2026-06-17; closes #67). The idea was: when the
harness runs **inside a Claude Code session**, get a reviewer with
visibility/steering (and no screen-scraping) by spawning the bundled
`code-security-reviewer` sub-agent via the Agent/Task tool, returning a normal
`ReviewResult` so the orchestrator stays transport-agnostic.

**Why rejected.** The headless `claude -p --permission-mode plan` reviewer
**already** covers the "Claude is the reviewer" case cross-provider. The only
thing a sub-agent adapter adds is *in-session visibility/steering* — a marginal
benefit that does not justify (a) a second execution surface that strains the
project-agnostic-engine and zero-runtime-deps invariants, and (b) the hard
security prerequisite below (family-vs-key normalization) that must land first
before any sub-agent result could ever satisfy an automatic review gate. The cost
outweighs the gain; the headless adapter is sufficient.

The analysis that led here is kept below as the guard rail for any future
revisit — the feasibility constraint and the self-review-bypass prerequisite are
exactly what a revived step 5 would have to clear.

### The feasibility constraint (why it needed a whole new execution mode)

The **CLI orchestrator is a bare Python subprocess**: it has no access to Claude
Code's Agent/Task tool, so it *cannot* spawn a sub-agent. A sub-agent reviewer is
therefore only meaningful in a **new execution mode where the pipeline is driven
from inside a Claude Code session** that owns the Agent tool. The options that
were weighed (and that a future revisit would weigh again) were:

1. **A Claude-Code-hosted entrypoint** (e.g. a slash command / SDK harness) that
   runs the pipeline and, for the reviewer phase, calls the Agent tool. The CLI
   orchestrator keeps using the headless CLI adapters; the sub-agent adapter is
   selected only in the hosted mode.
2. **A capability probe**: the engine detects whether an Agent-tool callback was
   injected at startup; if present, the sub-agent adapter is eligible, else it
   resolves to the headless adapter. Keeps one orchestrator, two runtimes.
3. **Reject step 5** if neither path is worth the new execution surface — the
   headless `claude -p` reviewer already covers the "Claude reviewer" case; the
   only thing the sub-agent adds is in-session visibility/steering. **← this is
   the option taken.**

### Hard constraints (enforced by the engine for today's adapters; the adapter must comply — but see the cross-provider PREREQUISITE below)

- **Cross-provider** — a Claude sub-agent reviewing Claude-written code is the
  same self-review collapse #28 prevents. The sub-agent reviewer is for the
  provider OPPOSITE the worker; the other side stays a headless CLI.
  **⚠️ PREREQUISITE (plan_review finding PR-001, HIGH):** the existing guard does
  NOT apply unchanged if step 5 adds a new adapter key. `worker_provider()`
  returns a provider *family* (`"claude"`/`"codex"`) while `reviewer_provider()`
  returns the raw reviewer-adapter *key*, and both the in-pipeline guard
  (`rp != wp`) and the fallback ladder (`fb == worker_provider(state)`) compare
  those directly. So a new key like `"claude-subagent"` would compare
  `"claude-subagent" != "claude"` → falsely read as cross-provider → **bypass the
  self-review collapse guard** (Claude reviewing Claude). Step 5 MUST therefore
  either (a) resolve the sub-agent through the EXISTING `claude`/`codex` family
  key (introduce no new provider key), OR (b) first add explicit provider-family
  *normalization* to `reviewer_provider` and the fallback `fb == worker_provider`
  comparison — and no sub-agent result may satisfy an automatic review gate until
  that normalization is in place. This is a build prerequisite, not optional.
- **Read-only** — declare `read_only_enforced: True` only if the sub-agent truly
  cannot write (the Agent invocation must disallow Edit/Write). The fallback
  ladder already refuses to trust a non-read-only fallback's APPROVED.
- **Fail-closed** — no parseable `REVIEW_DECISION` ⇒ `parse_status != "ok"` ⇒
  treated as `MISSING`, never approval (same contract as the CLI adapters; the
  fallback ladder + manual_required handling already cover it).

### Open questions a revisit would have to answer (left unresolved by the rejection)

- How does the hosted mode inject the Agent-tool callback into the engine without
  the engine importing a Claude-Code-only dependency (zero-runtime-deps rule)?
- Does `code_review.md` / `plan_review.md` persistence stay identical so the
  orchestrator remains transport-agnostic? (Should: yes.)
- Where does the audit trail record "reviewed via sub-agent" — reuse the
  structured `fallback_audit`/`review_audit` shape?
- Is the in-session visibility/steering benefit worth a second execution mode, or
  is the headless `claude` reviewer sufficient (option 3)?
- Family-vs-key normalization (PR-001): if step 5 needs a distinct adapter key,
  the `reviewer_provider`/fallback comparisons must normalize key → family first;
  pin a regression that a same-family sub-agent reviewer is flagged as self-review.

**Outcome:** option 3 (reject) taken on 2026-06-17; #67 closed. If step 5 is ever
revived, it MUST start from a fresh cross-provider `plan_review` and clear the
family-vs-key prerequisite (PR-001) before any sub-agent result can satisfy an
automatic review gate.
