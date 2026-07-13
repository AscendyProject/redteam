# Goal: persist per-phase model/cost/duration telemetry to state.json (#146 Phase 0)

## Intent

Phase 0 of the #146 benchmark design (`docs/decisions/2026-07-13-benchmark-design.md`):
the always-on foundation. The Claude worker adapter already receives the model's
cost + duration from `claude --output-format ...` (the streaming `result` event
with `total_cost_usd` / `duration_ms`, surfaced in
`phase_runners/_base.py` around the `elif t == "result":` branch that today only
**prints** `DONE (786.1s, $2.431)`), but that data is thrown away after logging.
Capture it and **persist a per-phase telemetry record to the task's `state.json`**
so a later benchmark (Phase 1) and the #92/#95 cost work can read real numbers
instead of re-deriving them.

Concretely, after each worker phase completes, append a telemetry entry to a new
`state["phase_telemetry"]` list. Each entry records at least:

- `phase` — the phase name (e.g. `plan_outcome`, `implement`).
- `provider` — `"claude"` or `"codex"` (the resolved worker/role provider).
- `model` — the resolved model string when known, else `null`.
- `cost_usd` — the Claude `total_cost_usd` for that invocation; **`null` when
  unavailable** (the Codex adapter is stdout-only and exposes no cost — do NOT
  fabricate or estimate a number).
- `duration_sec` — from `duration_ms / 1000` when available, else `null`.
- `outcome` — the phase's terminal status for that attempt (e.g. `approved`,
  `changes_requested`, `error`).

The review-loop counters already in `state.json` (`implement_round_count`,
`retries`, rescue/wall-clock counters) are NOT re-implemented here — they already
persist; Phase 0 only adds the missing model/cost/duration/outcome capture.

## Hard constraints

- **Zero runtime dependencies.** Stdlib only — no new imports beyond what the
  engine already uses.
- **Engine stays project-agnostic.** No project/stack fingerprints in
  `.redteam/workflows/` or non-example tests (`test_agents_generic_prompts.py`
  must stay green).
- **Do not change the adapter trust model or sandbox flags.** The reviewer stays
  read-only; the worker stays workspace-write. This task only *reads* the cost/
  duration the adapter already produces and threads it out.
- **No secret leakage.** Persist only the numeric/label telemetry above — never
  raw stdout/stderr, `last_failure_log`, review text, or a diff into the
  telemetry record (same IR-002/IR-004 posture as `status --json`).
- **Backward compatible / fail-safe.** A phase whose telemetry is unavailable
  (Codex path, a CLI that didn't emit a result event, an older state.json without
  the key) must degrade to `null`/absent, never raise. Telemetry capture must
  NEVER change a phase's pass/fail outcome — it is observability, strictly
  additive.

## Operator delegation (autonomy clause)

Plan-level scope questions in this run are delegated to the operator agent:
prefer the narrowest change that persists the telemetry, and record any such
decision in `ask_user_response.md` (or the final report) instead of waiting for a
human. Security-boundary weakening (adapter trust model, sandbox, secret
redaction) is NOT delegated — that stops the run.

## Non-goals

- **No `benchmark` / `benchmark-report` / `recommend-models` subcommand** — that
  is Phase 1, a separate goal.
- No aggregation, scoring, Pareto, LLM-judge, matrix expansion, or JSONL/sqlite
  benchmark store.
- No change to the Codex adapter to add cost capture (it stays stdout-only;
  Codex-role cost is `null` in Phase 0).
- No new config keys or CLI surface.

## Notes for decomposition

- This is a single cohesive change in the worker-telemetry path
  (`phase_runners/_base.py` + wherever the phase result is persisted to
  `state.json`, plus `state.template.json` if the key is seeded there) with its
  tests. It most likely decomposes to **one task**; do not split it into
  artificially-coupled tasks that both edit `_base.py`.
- The task's `outcome.md` must carry a parseable `## Verification` section with a
  fenced ```yaml `commands:` list containing the project verify command (the
  planner skeleton now emits this — keep it).
- Pin the task's Affected files strictly inside `.redteam/workflows/` and
  `.redteam/tests/` (plus `.redteam/**/state.template.json` if the telemetry key
  is seeded there). Do not add files under `docs/` or elsewhere outside those.
