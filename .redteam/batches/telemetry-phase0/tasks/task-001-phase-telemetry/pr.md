## What
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

## Why
Phase 0 of the #146 benchmark design
(`docs/decisions/2026-07-13-benchmark-design.md`): the always-on foundation.
Today, the Claude worker adapter receives `total_cost_usd` and `duration_ms`
from `claude --output-format stream-json`, prints them to stderr, then discards
them. This task captures that signal (plus resolved provider/model and the
phase's terminal outcome) into `state.json` so a later Phase 1 can consume real
numbers rather than re-deriving them. Strictly the capture side; no new CLI,
config toggle, or aggregation.

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

## Verification
- Tests: test_claude_adapter_invoke_populates_telemetry, test_claude_adapter_invoke_none_parsed_json, test_claude_adapter_missing_keys_in_parsed_json, test_plan_outcome_approved_carries_telemetry, test_plan_outcome_error_carries_telemetry, test_orchestrator_appends_telemetry_entry_claude, test_orchestrator_appends_telemetry_codex_path, test_orchestrator_appends_telemetry_missing_signal, test_orchestrator_appends_telemetry_create_pr_approved, test_orchestrator_appends_telemetry_create_pr_error, test_plan_review_does_not_append_telemetry, test_review_code_does_not_append_telemetry, test_rescue_does_not_append_telemetry, test_telemetry_does_not_change_phase_result_status, test_telemetry_append_error_swallowed_to_stderr, test_legacy_state_missing_key_still_appends, test_state_template_has_phase_telemetry, test_telemetry_entry_has_exactly_six_allowed_keys
- Verify command: `bash .redteam/scripts/verify.sh` ✅

## Code review summary
- Diff summary: adapters `_protocol.py` + `claude.py` gain four `NotRequired` telemetry fields; `PhaseResult` mirrors them; six worker runners materialize telemetry after `.invoke(...)`; orchestrator appends one entry per worker-invoking phase; state template seeds `"phase_telemetry": []`; 771-line test module covers Claude / Codex / missing-signal / reviewer-silence / legacy-state / no-secret-bleed invariants.
- IR-002 (major, resolved): append point is correctly placed after `runner(task_dir, state)` and before downstream save/branch logic; entry carries exactly six fields with no free-text leakage.
- IR-003 (major, resolved): every worker-invoking runner (plan_outcome / implement agent-pair / implement TDD / write_test / verify_test / create_pr) materializes telemetry via `.get(...)` fallthrough + `worker_provider(state)`.
- IR-004 (minor, resolved): new tests discriminate against pre-change behavior — pre-change had no adapter telemetry fields, no runner `provider` sentinel, no orchestrator append, no template key.
- IR-001 (minor, open at phase exit, non-blocking): Claude adapter omits telemetry fields when `parsed_json is None` rather than emitting `provider="claude"`. Invariant is preserved because runners re-materialize `provider=worker_provider(state)` after `.invoke(...)` and `WorkerRunResult.provider` is `NotRequired`.
- REVIEW_DECISION: APPROVED (Codex, agent-pair mode).

## Generated by
redteam / batch telemetry-phase0 / task task-001-phase-telemetry
