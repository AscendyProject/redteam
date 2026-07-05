# Decision: Reviewer Prompt Caching — Not Implementable at the CLI Adapter Layer

**Date:** 2026-07-05  
**Status:** Closed (not implementable; revisit via plan_review on adapter transport)  
**Related task:** P5 hard ceilings on the review loop (task-002-p5-hard-ceilings)  
**Related doc:** `docs/decisions/2026-06-17-reviewer-transport-and-subagent.md`

## Context

The P5 task input (`input.md`) listed "prompt caching of the fixed reviewer prompt
portion" as a potential cost-reduction sub-bullet alongside the hard ceilings work.
The P5 outcome (D7) investigated whether prompt caching is implementable at the
current CLI adapter seams before landing any code.

## Investigation

Both CLI adapters were inspected:

**`adapters/codex.py` (`CodexReviewerAdapter`)** invokes:
```
codex exec --sandbox read-only -
```
with the full prompt on stdin. `codex exec` exposes **no documented
`--cache*` / prompt-cache-control flag** at the CLI seam. There is no mechanism
to hint a cacheable prefix via the `codex` CLI.

**`adapters/claude.py` (`ClaudeReviewerAdapter`)** invokes:
```
claude -p <prompt> --permission-mode plan --allowedTools ... --disallowedTools ... --output-format json
```
The Claude CLI (`claude -p`) exposes **no documented CLI flag for
prompt cache-control**. The Anthropic API-level `cache_control: ephemeral`
parameter (available on `messages.create` calls) is not surfaced as a `claude -p`
flag. Any prompt-caching the Claude API applies automatically to a cacheable prefix
is server-side and transparent to the CLI adapter — the adapter has no way to
influence it.

## Decision

Implementing "prompt caching of the fixed reviewer prompt portion" at the CLI
adapter layer is **NOT POSSIBLE** without changing the adapter transport
(e.g. switching to the Anthropic HTTP API directly, or a hypothetical
`codex exec --cache*` flag that does not exist today).

A transport change is a separate, later, security-boundary decision governed by
the reviewer-transport decision pattern
(`docs/decisions/2026-06-17-reviewer-transport-and-subagent.md`).

**Guard rail:** Any future revisit of reviewer prompt caching **must go through
`plan_review`** on the adapter transport before implementation. The two adapter
files (`adapters/claude.py`, `adapters/codex.py`) are explicitly NOT touched by
the P5 task as a result of this determination.

## What was NOT landed

- No stub, no fake caching flag, no dead argument in either adapter.
- No `--cache*` argument in the CLI invocations.
- No changes to `adapters/claude.py` or `adapters/codex.py`.

The P5 task landed only the hard ceilings (max rounds + max wall-clock) which are
orthogonal to prompt caching and do not require any adapter transport change.
