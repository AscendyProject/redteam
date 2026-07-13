# Outcome — task-001-phase-telemetry: persist per-phase model/cost/duration/outcome to state.json

## Goal
After each worker-adapter-invoking phase runs, `state["phase_telemetry"]` gains
exactly one dict entry recording `phase`, `provider`, `model`, `cost_usd`,
`duration_sec`, and `outcome` — so a later Phase 1 (a separate goal) can read
real numbers instead of re-deriving them. Capture is strictly additive: the
cost/duration `run_claude` already parses off `claude`'s stream-json `result`
event is threaded from `ClaudeWorkerAdapter` → `WorkerRunResult` → `PhaseResult`
→ the orchestrator dispatch loop. Phases that invoke `get_worker_adapter` but
whose worker exposes no cost (the Codex worker path, or a timed-out Claude run)
still record a well-formed entry with `cost_usd`/`duration_sec`/`model` set to
`null`. Telemetry capture never alters a phase's `PhaseResult.status`.

## Done-when
- [ ] `.redteam/workflows/adapters/_protocol.py` extends `WorkerRunResult` with
      `NotRequired` fields `cost_usd: float | None`, `duration_sec: float | None`,
      `model: str | None`, `provider: str`; the existing
      `returncode`/`stdout`/`stderr` shape is unchanged so every current caller
      stays valid.
- [ ] `.redteam/workflows/adapters/claude.py`'s `ClaudeWorkerAdapter.invoke(...)`
      populates the four new fields from `run_claude(...)['parsed_json']`:
      `cost_usd = parsed_json["total_cost_usd"]` (else `None`),
      `duration_sec = parsed_json["duration_ms"] / 1000` (else `None`),
      `model = parsed_json.get("model")` (else `None`), and
      `provider = "claude"`. A `None` `parsed_json` (timeout, transport error, or
      non-Claude path) or any missing key degrades to `None` and never raises.
- [ ] `.redteam/workflows/adapters/codex.py` is **not modified** in this task.
      The runner materialization rule below is what guarantees Codex-driven
      worker phases still emit a well-formed entry; the Codex adapter keeps its
      unchanged `WorkerRunResult(returncode, stdout, stderr)` shape and its
      unchanged trust model / sandbox.
- [ ] `.redteam/workflows/phase_runners/_base.py` extends the `PhaseResult`
      TypedDict with `NotRequired` fields `cost_usd: float | None`,
      `duration_sec: float | None`, `model: str | None`, `provider: str`,
      mirroring the additive `fallback_audit` / `staging_audit` / `ceiling_hit`
      pattern. Every existing `PhaseResult(...)` construction stays valid. A
      small `build_telemetry_entry(state, phase, result)` helper MAY be added
      next to `PhaseResult` in `_base.py` (kept out of `orchestrator.py` so the
      dispatch site stays a one-liner and helper import stays project-agnostic).
- [ ] **Runner materialization rule (settles PR-002).** In every runner listed
      below, after any `get_worker_adapter(state).invoke(...)` call, every
      subsequent `PhaseResult(...)` return in that call's continuation
      materializes ALL FOUR telemetry fields on the returned `PhaseResult`:
      `cost_usd`, `duration_sec`, `model` set from
      `WorkerRunResult.get("cost_usd" | "duration_sec" | "model")` — falling
      through to `None` when the key is absent (Codex path) or the value is
      `None` (Claude path with no `parsed_json`); and `provider` set to
      `adapters.worker_provider(state)` (unconditionally — never `None`). This
      makes the "one entry per worker-adapter invocation" invariant total: a
      Codex-driven worker phase produces the entry
      `{provider:"codex", cost_usd:null, duration_sec:null, model:null,
      outcome:...}` rather than a silently-missing entry.
- [ ] Runners covered: `plan_outcome.py`, `implement.py` (BOTH the
      `_run_agent_pair` and TDD `run` branches, at every `PhaseResult(...)`
      return path that comes after the worker `invoke` call — including the
      `error`/`changes_requested`/`approved` returns from
      `_uncommitted_scope_files` / `_uncommitted_outside_scope_files` /
      `_uncommitted_plan_affected_paths` / verify-failure branches),
      `write_test.py`, `verify_test.py`, and `create_pr.py`. `PhaseResult(...)`
      returns that fire BEFORE the worker `invoke` (early manifest / branch /
      snapshot / preflight failures, out-of-scope floor rejects, trust-root
      floor rejects) set NO telemetry fields — no invocation happened.
- [ ] Runners NOT modified: `rescue.py` (no model invoked), `plan_review.py`
      (reviewer transport via `review_with_fallback` / reviewer adapter), and
      `review_code.py` (reviewer-transport phase — the agent-pair branch runs
      the headless reviewer adapter, and the TDD branch is semantically the
      reviewer role; reviewer-cost capture is the separate #92 track, out of
      Phase 0 scope). `decompose.py` is not a `PHASE_RUNNERS` entry and is
      untouched.
- [ ] `.redteam/workflows/orchestrator.py`'s `process_task` dispatch loop,
      immediately after `result = runner(task_dir, state)` returns and BEFORE
      any of the downstream `save_state(task_dir, state)` sites that persist
      the phase transition, appends exactly one dict to
      `state.setdefault("phase_telemetry", [])` iff the returned `PhaseResult`
      carries a `provider` field (the total-invariant signal — always set by
      the runner materialization rule iff the worker adapter was invoked;
      absent otherwise). Uses `state.setdefault` so a legacy `state.json`
      without the key is treated as `[]` and never raises `KeyError`.
- [ ] The appended entry has exactly these six keys, all present (missing
      numerics/strings recorded as JSON `null`, never omitted):
      ```json
      {"phase": "<phase name>",
       "provider": "claude" | "codex",
       "model": "<str>" | null,
       "cost_usd": <float> | null,
       "duration_sec": <float> | null,
       "outcome": "<PhaseResult.status>"}
      ```
      `phase` is the runner's phase name in `PHASE_RUNNERS` (`plan_outcome`,
      `implement`, `write_test`, `verify_test`, `create_pr`). `outcome` is
      exactly `PhaseResult["status"]` — no new vocabulary. The record contains
      NO free-text field (no `stdout`, `stderr`, `feedback`, `log`, `diff`,
      `last_failure_log`, or review text) — same IR-002/IR-004 posture as
      `status --json`.
- [ ] The orchestrator's telemetry-append site is defensively wrapped so any
      exception from building or appending the entry is swallowed to
      `sys.stderr` (`except Exception: print(..., file=sys.stderr)`) and does
      not change `PhaseResult["status"]` nor the branch the dispatch loop
      takes.
- [ ] `.redteam/templates/state.template.json` seeds `"phase_telemetry": []`
      as a top-level key so freshly-scaffolded tasks are born with the list.
- [ ] `bash .redteam/scripts/verify.sh` is green (ruff + pytest), including
      the new tests under `.redteam/tests/`, with
      `test_agents_generic_prompts.py` still green (no project/stack
      fingerprint leaks into agent skeletons or engine code).

## Out of scope
- No `benchmark` / `benchmark-report` / `recommend-models` subcommand or any
  consumer of the recorded telemetry — that is Phase 1 (a separate goal).
- No aggregation, scoring, Pareto, LLM-judge, matrix expansion, or a JSONL /
  sqlite benchmark store.
- No cost/duration capture for the Codex worker adapter (no stdout parsing, no
  new adapter method, no new subprocess). Codex-role cost stays `null` in
  Phase 0; the runner materialization rule produces the entry.
- No cost/duration capture for reviewer-transport phases (`plan_review`,
  `review_code` in either mode) — those are the #92 track. `plan_review.py` and
  `review_code.py` are not modified.
- No modification to `rescue.py` (no model invoked) or `decompose.py` (not a
  per-task `PHASE_RUNNERS` runner — goal-decomposer path).
- No change to the reviewer/worker sandbox flags, adapter trust model,
  `run_claude`'s subprocess shape, or `--output-format stream-json`'s parsing
  beyond reading fields the existing `result` event already carries.
- No new config keys, no new CLI subcommand, no `--telemetry` flag, no
  environment-variable toggle — `phase_telemetry` is always on.
- No re-implementation of `implement_round_count`, `retries`, rescue counter,
  or wall-clock timers — those already persist in `state.json`.
- No rotation / size cap on `phase_telemetry` (a Phase 1 concern if it surfaces).
- No changes under `docs/`, `.redteam/prompts/`, `.redteam/scripts/`,
  `.claude/agents/`, `examples/`, `pyproject.toml`, `CHANGELOG.md`, or the
  plugin manifest.

## Affected files
- `.redteam/workflows/adapters/_protocol.py` — extend `WorkerRunResult` with
  four `NotRequired` telemetry fields.
- `.redteam/workflows/adapters/claude.py` — `ClaudeWorkerAdapter.invoke`
  populates the four new fields from `run_claude(...)['parsed_json']`; None-safe.
- `.redteam/workflows/phase_runners/_base.py` — extend `PhaseResult` with the
  four `NotRequired` telemetry fields; optionally add a small
  `build_telemetry_entry(state, phase, result)` factory alongside `PhaseResult`.
  `run_claude` is not restructured.
- `.redteam/workflows/phase_runners/plan_outcome.py` — after the
  `get_worker_adapter(...).invoke(...)` call, copy `WorkerRunResult` telemetry
  and set `provider = worker_provider(state)` onto every returned `PhaseResult`.
- `.redteam/workflows/phase_runners/implement.py` — same, on every
  `PhaseResult(...)` return after the worker `invoke` in BOTH `_run_agent_pair`
  and the TDD `run` branch (approved, changes_requested, error paths).
- `.redteam/workflows/phase_runners/write_test.py` — same.
- `.redteam/workflows/phase_runners/verify_test.py` — same.
- `.redteam/workflows/phase_runners/create_pr.py` — same (pr-author call at
  `create_pr.py:174`); its `approved` and `error` returns after `invoke` carry
  the telemetry fields (settles PR-001).
- `.redteam/workflows/orchestrator.py` — in `process_task`, after
  `result = runner(task_dir, state)`, append the telemetry entry via
  `state.setdefault("phase_telemetry", []).append(...)` iff
  `result.get("provider")` is present; wrap in a bare `except Exception:` that
  writes to `sys.stderr` and continues. Placement is BEFORE the existing
  ceiling / manual-required / audit / retry branches so every dispatch path
  records the entry exactly once.
- `.redteam/templates/state.template.json`
- `(new) .redteam/tests/test_phase_telemetry.py` — pytest tests covering the
  invariants below; monkey-patches `run_claude` and worker `invoke`, never
  spawns the real `claude` or `codex` CLI.

## Verification

### Existing (must continue to pass)

```yaml
commands:
  - bash .redteam/scripts/verify.sh
```

### To be created (the test-writing phase will define exact test names)
- Under `.redteam/tests/test_phase_telemetry.py`, covering:
  - **Claude-worker happy path.** With a synthetic `ClaudeRunResult` whose
    `parsed_json` mimics the `type: "result"` event (`total_cost_usd`,
    `duration_ms`, and `model`), invoking `ClaudeWorkerAdapter.invoke(...)`
    returns a `WorkerRunResult` carrying `cost_usd`, `duration_sec =
    duration_ms / 1000`, `model`, and `provider == "claude"`.
  - **Runner materialization.** A worker-invoking runner (e.g. `plan_outcome`
    or the `create_pr` happy path) returns a `PhaseResult` carrying all four
    telemetry fields after `.invoke(...)`, with `provider ==
    worker_provider(state)`.
  - **Orchestrator append — Claude happy path.** After the dispatch loop runs
    a phase whose runner returned a `PhaseResult` with a Claude-populated
    `provider`, `state["phase_telemetry"]` gains exactly one entry with the
    expected numeric values and `provider == "claude"`; persisted via
    `save_state`.
  - **Codex-worker path (settles PR-002).** With the worker resolved to Codex
    (`state.models.implementer = "codex"` and `default_model_for_role` faked
    accordingly), a worker-invoking runner returns a `PhaseResult` with
    `provider == "codex"`, `cost_usd is None`, `duration_sec is None`,
    `model is None`; the orchestrator appends an entry with exactly those
    values. No number is fabricated from stdout.
  - **Missing-signal path.** `run_claude` returns `parsed_json=None` (timeout
    / transport error). The `PhaseResult` still carries `provider == "claude"`
    with `cost_usd is None` and `duration_sec is None`; the orchestrator
    appends the entry and `outcome` reflects the actual `PhaseResult.status`
    (e.g. `error`).
  - **create_pr entry (settles PR-001).** After `create_pr.run` returns from
    its pr-author `.invoke(...)` call, exactly one `phase_telemetry` entry is
    appended with `phase == "create_pr"` and the resolved provider, for both
    the `approved` and the post-invoke `error` returns.
  - **Reviewer-transport phases silent.** After `plan_review.run` or
    `review_code.run` (either mode) returns, `state["phase_telemetry"]` is
    unchanged — the returned `PhaseResult` carries no `provider` field, so no
    entry is appended. Same for `rescue.run` (no model invoked).
  - **Non-mutation guarantee.** Telemetry capture never rewrites
    `PhaseResult.status` — an `approved` result stays `approved`, an `error`
    result stays `error`. When the append itself raises (e.g. patched to raise
    `RuntimeError`), the exception is swallowed to stderr, the phase's
    downstream dispatch branch is unchanged, and the outcome is preserved.
  - **Legacy state.json.** Dispatching a worker-invoking phase from a state
    dict that lacks `phase_telemetry` still succeeds — the missing key is
    treated as `[]` via `setdefault` and the new entry is appended without
    `KeyError`.
  - **State template shape.** `.redteam/templates/state.template.json` parses
    as JSON and contains `phase_telemetry` as an empty list at top level.
  - **No-secret-bleed shape.** The appended entry's keys are exactly
    `{phase, provider, model, cost_usd, duration_sec, outcome}` — no
    `feedback`, `log`, `diff`, `stderr`, `stdout`, `last_failure_log`, or
    review-text key leaks in.

## Risks
- **`model` field for Claude may be `None` even on success.** `run_claude`'s
  `parsed_json` is the `result` event; the existing `_print_stream_event` reads
  `model` off the `system init` event, not the `result` event. If the Claude
  `result` event does not carry `model`, `ClaudeWorkerAdapter` records
  `model: None` even for a successful run. This is the brief's "where cheaply
  available" concession and is intentional; no new capture path is added
  (would require restructuring `run_claude`, which the brief forbids).
- **`state["phase_telemetry"]` grows unbounded across long tasks.** No rotation
  / cap is added in Phase 0 per the brief's "always-on foundation" framing. If
  a later cost/size concern surfaces, a Phase 1 change can add a cap. Flagged
  because state.json is git-tracked in some dogfood runs (goal-mode drops it
  under `.redteam/batches/<batch>/tasks/<task>/`).
- **`_run_agent_pair` in `implement.py` has many post-invoke return sites**
  (verification failure, three-layer integrity gate, non-zero worker exit,
  approved). Each of them must carry the telemetry fields under the runner
  materialization rule — the risk is missing one and producing a silent gap.
  The test matrix above pins the `approved` and `changes_requested`/`error`
  branches, but a linter would catch it more reliably; none is added here.
