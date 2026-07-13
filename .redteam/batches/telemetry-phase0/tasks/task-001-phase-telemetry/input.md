# task-001-phase-telemetry — Persist per-phase model/cost/duration/outcome to state.json

## Summary

Phase 0 of the #146 benchmark design
(`docs/decisions/2026-07-13-benchmark-design.md`): the always-on foundation.
Today, the Claude worker adapter receives `total_cost_usd` and `duration_ms`
from `claude --output-format stream-json` — the values are printed to stderr
(`[agent] DONE (786.1s, $2.431)`) and then discarded. This task captures that
signal (plus the resolved provider/model and the phase's terminal outcome) and
**appends one entry per worker-phase attempt to a new `state["phase_telemetry"]`
list** in the task's `state.json`. A later Phase 1 (a separate goal) will
consume these numbers; this task is strictly the capture side.

## Where the signal lives today

- `.redteam/workflows/phase_runners/_base.py`
  - `_print_stream_event(line, agent)` (around the `elif t == "result":` branch)
    already parses `total_cost_usd` and `duration_ms` from the final `result`
    event to print the `DONE (…)` line.
  - `run_claude(...)` returns a `ClaudeRunResult` TypedDict whose `parsed_json`
    field is the captured final `result` event dict (or `None` if none arrived
    — e.g. timeout, transport error, or a Codex path that doesn't emit this
    event).
- `.redteam/workflows/adapters/claude.py`
  - `ClaudeWorkerAdapter.invoke(...)` currently converts `ClaudeRunResult` into
    `WorkerRunResult(returncode, stdout, stderr)` and **drops `parsed_json`** on
    the way out. This is the primary place where the cost/duration is lost to
    the runner layer.
- `.redteam/workflows/adapters/_protocol.py`
  - `WorkerRunResult` TypedDict — currently `returncode | stdout | stderr` only.
- `.redteam/workflows/adapters/codex.py`
  - Codex worker adapter — stdout-only, exposes NO cost. Do not modify its
    trust model / sandbox. Codex-role telemetry entries must degrade to
    `cost_usd: null, duration_sec: null` (and `model: null` when unknown), never
    fabricate a value.
- `.redteam/workflows/adapters/__init__.py`
  - `worker_provider(state) -> "claude" | "codex"` and
    `reviewer_provider(state) -> str | None` are the canonical resolvers for the
    `provider` field on the telemetry record.
- `.redteam/workflows/orchestrator.py`
  - `save_state(task_dir, state)` is the atomic writer.
  - `process_task(...)` is the phase dispatch loop that calls each phase
    runner's `run(task_dir, state)` and then persists state (many
    `save_state(task_dir, state)` sites).
- `.redteam/templates/state.template.json` — the seed shape for `state.json`.
  New tasks should be born with `"phase_telemetry": []` so the key is present
  from the start.

## What to build

The narrowest change that makes each of the following true.

### 1. Thread cost/duration/model out of the worker adapter

- Extend `WorkerRunResult` (in `.redteam/workflows/adapters/_protocol.py`) with
  optional (`NotRequired`) fields that carry the observability signal from the
  worker invocation. Suggested shape (planner may refine):
  - `cost_usd: float | None` (NotRequired)
  - `duration_sec: float | None` (NotRequired)
  - `model: str | None` (NotRequired)
  - `provider: str` (NotRequired) — `"claude"` or `"codex"`
  These are strictly additive; existing callers that don't read them must be
  unaffected.
- `ClaudeWorkerAdapter.invoke(...)` populates the new fields from
  `run_claude(...)`'s `parsed_json` (the final `result` event: `total_cost_usd`,
  `duration_ms / 1000`, and — where cheaply available — `model`). Missing keys
  or a `None` `parsed_json` degrade to `None`, never raise. `provider` is
  `"claude"`.
- `CodexWorkerAdapter.invoke(...)` populates `provider="codex"` and leaves the
  cost/duration/model fields at `None` (Codex is stdout-only in Phase 0 — do
  NOT parse Codex stdout for a cost number; do NOT change the Codex sandbox or
  trust model). The planner may choose to skip populating even `provider` from
  the Codex path if the recorder can resolve it from `worker_provider(state)` —
  pick the narrower path.

### 1b. Propagate the telemetry through `PhaseResult` (the crux — decompose PR-002)

`WorkerRunResult`'s new fields live INSIDE each phase runner; `process_task` only
sees the runner's returned `PhaseResult`, which today has no telemetry fields. So
the observability signal cannot reach the orchestrator dispatch layer unless the
runner threads it through. Do this the same way the engine already carries
runner→orchestrator provenance:

- Add matching `NotRequired` telemetry fields to `PhaseResult`
  (`phase_runners/_base.py`), mirroring the existing additive `fallback_audit` /
  `staging_audit` / `ceiling_hit` pattern: `cost_usd`, `duration_sec`, `model`,
  `provider` (all optional). Strictly additive — every existing `PhaseResult(...)`
  construction stays valid.
- Each runner that invokes a model adapter copies the `WorkerRunResult`
  telemetry into the `PhaseResult` it returns. A phase that invokes **no** model
  (e.g. `rescue.run`, which only reads `rescue_report.md` / `impl_diff.patch` and
  never calls `get_worker_adapter`) simply does not set these fields — so it
  produces no telemetry, correctly.

### 2. Record a `phase_telemetry` entry per model-invoking phase

- After each phase's `run(task_dir, state) -> PhaseResult` returns in
  `process_task`, append one dict to `state["phase_telemetry"]` and persist via
  the existing `save_state(task_dir, state)` — but **only when the returned
  `PhaseResult` actually carries telemetry fields** (i.e. a model was invoked).
  This is data-driven, not a hardcoded phase list: a phase that invoked no model
  (rescue, create_pr, pure bookkeeping) carries no telemetry and records no
  entry, so there is no fabricated/null row for a non-worker phase. Do this in
  the **orchestrator** dispatch layer (one code path, not N); if a small helper
  is clearer, put it next to `save_state` in `orchestrator.py`.
- Entry shape (all keys always present; use `null` for unavailable numeric
  fields; never omit-and-guess):
  ```json
  {
    "phase": "<phase name, e.g. plan_outcome | implement | ...>",
    "provider": "claude" | "codex",
    "model": "<resolved model string>" | null,
    "cost_usd": <float> | null,
    "duration_sec": <float> | null,
    "outcome": "<PhaseResult.status, e.g. approved | changes_requested | error | ...>",
    "timestamp": "<UTC ISO-8601, e.g. 2026-07-13T12:34:56Z>"
  }
  ```
  `timestamp` is optional — include it only if the planner considers it part
  of the minimal record. `outcome` is the phase's `PhaseResult.status` for
  that attempt (the same enum already used by the engine — do NOT invent a
  new vocabulary).
- **Which phases record telemetry?** Whichever phases actually **invoke a model
  adapter** and therefore return a `PhaseResult` carrying the telemetry fields —
  this is decided by the data (section 1b), NOT a hardcoded list. In practice
  that is the worker phases that call `get_worker_adapter(...).invoke(...)`
  (`plan_outcome`, `implement`, and the TDD `write_test` / `verify_test`).
  `rescue.run` invokes no model, so it records nothing (decompose PR-001).
  Reviewer phases: cost is unavailable (Codex adapter is stdout-only), so if a
  reviewer phase records at all it records `cost_usd: null`; the design keeps
  reviewer-cost capture separate, so do NOT add a
  new subprocess call, a new adapter method, or parse reviewer stdout to
  reach parity. Record the delegation you took in `ask_user_response.md` or
  the final report per the operator-delegation clause in `goal.md`.
- The record must NEVER change a phase's pass/fail outcome. If constructing
  the entry raises (defensive belt-and-braces), swallow-and-log to stderr —
  it is observability, strictly additive.

### 3. Seed the key in the state template

- Add `"phase_telemetry": []` to `.redteam/templates/state.template.json` so
  freshly-scaffolded tasks are born with the list.
- Any state-loader path that reads legacy `state.json` files must tolerate the
  key being absent — treat missing as `[]` and append on the fly. Never raise
  on an older state.json.

### 4. Tests

Add tests under `.redteam/tests/` that pin the invariants above. The planner
picks the exact split, but at minimum:

- Claude worker path: given a synthetic `parsed_json` (built to mimic the
  `type: "result"` event with `total_cost_usd`, `duration_ms`, and — where
  parsed — `model`), a `phase_telemetry` entry with the expected numeric
  values is appended after the phase.
- Codex worker path: `cost_usd is None`, `duration_sec is None`,
  `provider == "codex"`. No fabricated numbers.
- Missing signal path: `parsed_json is None` (e.g. timeout, transport error)
  → the entry is still appended with `cost_usd is None`, `duration_sec is
  None`; the phase's `PhaseResult.status` (e.g. `error`) is preserved on
  `outcome`.
- Legacy state.json: a state dict without the `phase_telemetry` key still
  works (missing → treated as empty list; append succeeds).
- Non-mutation guarantee: telemetry capture never changes the phase's
  returned status or pushes a phase from `approved` → `error`.

Tests must use monkey-patching / fakes for `run_claude` (already the pattern
in this repo's tests) — do NOT invoke the real `claude` CLI.

## Hard constraints (inherited from goal.md)

- **Zero runtime dependencies.** Stdlib only — no new imports beyond what the
  engine already uses. `datetime`, `json`, `typing.NotRequired` are already
  imported in the affected modules.
- **Engine stays project-agnostic.** No project/stack fingerprints in
  `.redteam/workflows/` or non-example tests. `test_agents_generic_prompts.py`
  must stay green.
- **Do not change the adapter trust model or sandbox flags.** Reviewer stays
  read-only; worker stays workspace-write. This task only *reads* cost/duration
  that the Claude adapter already produces.
- **No secret leakage.** Persist ONLY the numeric/label telemetry above —
  never raw stdout/stderr, `last_failure_log`, review text, prompt text, or a
  diff into the telemetry record (same IR-002/IR-004 posture as `status --json`).
  The `phase_telemetry` entry must be free of any free-text field that could
  carry model output.
- **Backward compatible / fail-safe.** Missing signal → `null`, not a raise.
  Legacy `state.json` without the key → treated as absent-and-append. Telemetry
  capture NEVER changes a phase's pass/fail outcome.
- **No new config keys or CLI surface.** Do NOT add a `--telemetry` flag, a
  new subcommand, or a config toggle. `phase_telemetry` is always on.

## Non-goals (do not do)

- No `benchmark` / `benchmark-report` / `recommend-models` subcommand — that
  is Phase 1.
- No aggregation, scoring, Pareto, LLM-judge, matrix expansion, or JSONL /
  sqlite benchmark store.
- No change to the Codex adapter to add cost capture. It stays stdout-only;
  Codex-role cost is `null` in Phase 0.
- No re-implementation of `implement_round_count`, `retries`, rescue counter,
  or wall-clock timers — those already persist in `state.json`.

## Affected files (pin the Affected list in outcome.md to this shape)

Strictly inside `.redteam/workflows/` and `.redteam/tests/` (plus the state
template). Do NOT add files under `docs/` or elsewhere.

- `.redteam/workflows/adapters/_protocol.py` — extend `WorkerRunResult`.
- `.redteam/workflows/adapters/claude.py` — populate the new fields from
  `run_claude(...).parsed_json`.
- `.redteam/workflows/adapters/codex.py` — populate `provider="codex"` (and
  leave cost/duration/model at `None`); only if the recorder cannot resolve
  provider from `worker_provider(state)` on its own.
- `.redteam/workflows/orchestrator.py` — append the `phase_telemetry` entry
  after each worker-phase run, then `save_state(...)`.
- `.redteam/workflows/phase_runners/_base.py` — small helper only if needed
  (e.g. a `build_telemetry_entry(...)` factory). Do NOT restructure `run_claude`.
- `.redteam/templates/state.template.json` — seed `"phase_telemetry": []`.
- `.redteam/tests/test_phase_telemetry.py` (or a similarly-named new file)
  covering the invariants above.

If the planner discovers a file outside this list is required to complete the
change, treat that as a plan-level scope question and use the operator-delegation
clause in `goal.md` — record the deviation in `ask_user_response.md` and the
final report.

## Verification

The outcome.md must carry a parseable `## Verification` section with a fenced
`yaml` `commands:` list containing the project verify command. The planner
skeleton already emits this — do not rename it, do not swap the fence to
plain-text, do not split the yaml keys.

Expected verify command (from `.redteam/config.toml`):

```yaml
commands:
  - "bash .redteam/scripts/verify.sh"
```

This runs `ruff` + `pytest` over `.redteam/`; the new tests must pass and the
generic-agent-prompts test must remain green.

## Done when

- `state["phase_telemetry"]` is a list of dicts as specified, one entry per
  worker-phase attempt, persisted through `save_state`.
- `.redteam/templates/state.template.json` seeds `"phase_telemetry": []`.
- The Claude path records real `cost_usd` and `duration_sec` (from the
  `result` event); the Codex path records `null`s; missing / timed-out
  signal degrades to `null`s.
- Telemetry never mutates a phase's pass/fail outcome.
- No new pip dependency, no new CLI surface, no new config key, no engine
  project-fingerprint, no adapter trust-model / sandbox change, no secret
  bleed-through in the recorded fields.
- `bash .redteam/scripts/verify.sh` is green (ruff + pytest), including new
  tests for the invariants above.
