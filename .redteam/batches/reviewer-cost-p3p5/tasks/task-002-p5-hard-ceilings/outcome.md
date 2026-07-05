# Outcome — P5: Hard ceilings on the review loop

## Goal
Add opt-in per-task hard ceilings — a maximum number of `review_code` rounds
and a maximum cumulative wall-clock time spent in `review_code` — on top of
the existing retry / rescue-entry ladder, so a rare reviewer↔worker ping-pong
has a bounded tail cost. When a ceiling is hit the task must terminate
deterministically at the same fail-closed / deferred outcome the existing
rescue-entry ceiling uses — never as a silent approval. Default behavior
(no `[models.review_ceilings]` subtable in config) reproduces today's pipeline
with no new state fields written, no new counters mutated, and no new
runtime work.

## Design decisions (fixed here; the implementer must not re-negotiate)

Each decision closes a specific ambiguity in `input.md`. Tests are written
against them.

### D1. Config namespace — new sibling subtable `[models.review_ceilings]`

Ceilings live in a NEW nested subtable under the existing top-level `[models]`
block, as a SIBLING to the P3 `[models.review_stages]` subtable (task-001).
Two independent, both-optional keys — the subtable can carry one, the other,
or both:

- `max_review_rounds: int | None` — must satisfy `int >= 1` (Python `bool`
  values rejected, mirroring the P3 `escalate_after` bool guard); absent = no
  round ceiling. Semantics: `review_code.run` invocations `1..max_review_rounds`
  are allowed; invocation `max_review_rounds + 1` triggers the ceiling.
- `max_wall_clock_sec: int | None` — must satisfy `int >= 1` (Python `bool`
  values rejected); absent = no wall-clock ceiling. Semantics: cumulative
  `time.monotonic()`-measured wall-clock spent inside the headless-review
  dispatch of `review_code.run` (Cases B / C / D — Case A / manual never
  accrues) is capped at this value.

Additional loader rules:
- Absence of the whole `[models.review_ceilings]` subtable = no ceilings,
  today's behavior byte-for-byte (see D2/D3 default-off gating).
- Presence with BOTH keys absent (empty subtable) = fail loud (at least one
  ceiling key must be set when the subtable is declared).
- Any key inside the subtable other than the two names above = fail loud
  with the same `Unknown ... config key(s)` shape as `_build` /
  `_parse_review_stages`.
- Wrong TOML type on either key = fail loud.

Ceilings are independent of `[models.review_stages]`: both may be configured
together (P3 + P5), either alone, or neither.

**Tier-level ceilings are EXPLICITLY OUT OF SCOPE for v1.** Ceilings are
configured only under the top-level `[models]`. This task does NOT change
the `TierProfile.models` type contract (`dict[str, str]`), does NOT change
`_parse_tiers`'s per-role `str`-value enforcement, and does NOT accept a
`review_ceilings` key inside `[tiers.N].models`. The `_KNOWN_ROLES`
exclusion set (which already excludes `review_stages`) additionally excludes
`review_ceilings`, so a `[tiers.N].models.review_ceilings` sub-table
continues to be rejected by today's "unknown role(s)" fail-loud path.

### D2. Round-count source — new per-task counter, gated on `max_review_rounds`

`state["review_code_round_count"]` is a NEW per-task counter, **populated
and incremented ONLY when `cfg.models.review_ceilings is not None` AND
`cfg.models.review_ceilings.max_review_rounds is not None`**. When the
gate is off, the field is not written to state.json and no bookkeeping
runs — legacy state and default-config state stay byte-identical.

When the gate is on, the counter is incremented AT the top of the agent-pair
branch of `phase_runners.review_code.run`, BEFORE any dispatch decision
(before Case A / B / C / D resolution), via
`state["review_code_round_count"] = int(state.get("review_code_round_count") or 0) + 1`.
This is distinct from `implement_round_count` (which counts `implement`
calls) because the P5 ceiling caps `review_code` invocations, and a
`review_code` re-entry without a fresh `implement` (e.g. a
`parse_status != "ok"` retry loop) must still count against the ceiling.

- Type: `int`, treated as `0` when absent from state.json (legacy state,
  or a state.json written before the operator opted into the ceiling).
- Persisted through the normal orchestrator `save_state` path — NO
  addition to `.redteam/templates/state.template.json` (the template stays
  byte-identical to today; the field appears in state.json lazily on
  first opted-in run).
- NEVER reset on convergence. The ceiling is a per-task budget, not a
  per-cycle budget — a re-entered `review_code` after `approved` + rescue
  keeps accumulating. This matches "caps the total number of review rounds
  for a single task" from `input.md:19-20`.

### D3. Wall-clock source — new per-task counter, gated on `max_wall_clock_sec`

`state["review_code_wall_clock_sec"]` is a NEW cumulative `float` seconds
accrued across every headless-review dispatch (Cases B / C / D) in
`review_code.run`, using `time.monotonic()` (stdlib, zero new deps).
**Populated and accrued ONLY when `cfg.models.review_ceilings is not None`
AND `cfg.models.review_ceilings.max_wall_clock_sec is not None`.** When the
gate is off, no `time.monotonic()` reads happen, no field is written to
state.json, and no bookkeeping runs — legacy state and default-config state
stay byte-identical.

Case A (manual: `code_review.md` read from disk) does NOT accrue — its time
is bounded by a filesystem read and is not what the input's "cumulative
wall-clock time spent in review" means.

- Type: `float`, treated as `0.0` when absent from state.json.
- Persisted through the normal orchestrator `save_state` path — NO
  addition to `.redteam/templates/state.template.json`.
- Accrual pattern (only under the gate): `t0 = time.monotonic()`
  immediately before the `review_with_fallback*` invocation(s) that this
  `run()` will make; `state["review_code_wall_clock_sec"] = float(state.get("review_code_wall_clock_sec") or 0.0) + (time.monotonic() - t0)`
  immediately after they return (regardless of the reviewer's verdict, and
  regardless of whether the round was cheap-APPROVED-promoted in D3 of
  task-001 — a promoted round measures the combined first-pass + frontier
  time as one dt).
- `time.monotonic()` (NOT wall-clock `time.time()`) so a mid-run clock jump
  cannot inflate or deflate the budget.

### D4. Ceiling enforcement seam — inside `review_code.run` (agent-pair branch)

All ceiling checks live in `phase_runners/review_code.py::run`, agent-pair
branch ONLY (`state.get("mode") == "agent-pair"`); the tdd / sub-agent tail
is untouched, mirroring the P3 scope.

**Master gate.** At the top of the agent-pair branch, read
`review_ceilings = load_config(repo_root()).models.review_ceilings` (mirrors
the lazy live-config read pattern already used by `_resolve_round_stage`).
**If `review_ceilings is None`, the runner behaves byte-identically to
today — no counter increment, no `time.monotonic()` reads, no ceiling
checks, no state.json growth.** Enforcement below runs only when
`review_ceilings is not None`.

Enforcement points, in this exact order at the top of the agent-pair branch
(all inside `if review_ceilings is not None:`):

1. If `review_ceilings.max_review_rounds is not None`: increment
   `state["review_code_round_count"]` per D2. Persist NOTHING yet — the
   value is written when the runner returns via the normal orchestrator
   save_state path.
2. **Pre-dispatch round-ceiling check.** If
   `review_ceilings.max_review_rounds is not None` AND
   `state["review_code_round_count"] > review_ceilings.max_review_rounds`:
   return a ceiling-terminal `PhaseResult` (D5) with
   `ceiling_hit="max_review_rounds"`. NO reviewer invocation happens — the
   counter increment already recorded the attempt, and the orchestrator
   side (D6) will defer.
3. **Pre-dispatch wall-clock check.** If
   `review_ceilings.max_wall_clock_sec is not None` AND
   `float(state.get("review_code_wall_clock_sec") or 0.0) >= review_ceilings.max_wall_clock_sec`:
   return a ceiling-terminal `PhaseResult` with
   `ceiling_hit="max_wall_clock_sec"`. NO reviewer invocation happens.
4. Resolve the D5 dispatch case from task-001 (`_resolve_round_stage`).
   Case A (manual) BYPASSES wall-clock accrual and post-dispatch check —
   fall through to the existing manual branch unchanged.
5. For Cases B / C / D — **only when `review_ceilings.max_wall_clock_sec is not None`**:
   `t0 = time.monotonic()`; run the existing dispatch (including the D3
   cheap-APPROVED promotion from task-001, which may issue TWO ladder
   calls); on return (or on an exception, via `try` / `finally`),
   accrue per D3. When `max_wall_clock_sec is None`, the dispatch runs
   without a timer — no `time.monotonic()` reads.
6. **Post-dispatch wall-clock check.** After accrual, if
   `review_ceilings.max_wall_clock_sec is not None` AND
   `float(state.get("review_code_wall_clock_sec") or 0.0) >= review_ceilings.max_wall_clock_sec`:
   return a ceiling-terminal `PhaseResult` with
   `ceiling_hit="max_wall_clock_sec"` — **regardless of the reviewer's
   verdict**. This is the "no silent approvals under time pressure"
   invariant from `input.md:46-48`: a reviewer's APPROVED emitted just
   as the wall-clock crosses the ceiling is NOT finalized. The
   reviewer's raw is still persisted (`code_review.md`,
   `code_review.first_pass.md` on a promoted round) for the audit trail;
   only the `PhaseResult` is upgraded to ceiling-terminal.

There is no path by which `PhaseResult(status="approved")` can be produced
by the runner in the SAME `run()` invocation that crosses either ceiling.
Round-ceiling crossings never invoke a reviewer; wall-clock crossings
discard the reviewer's verdict.

### D5. Ceiling-terminal `PhaseResult` shape — new `ceiling_hit` structured field

`phase_runners/_base.py::PhaseResult` gains an optional field
`ceiling_hit: NotRequired[str]`, mirroring `staging_audit` and
`fallback_audit` — set ONLY by the runner (never parsed from reviewer
text), so a reviewer cannot spoof a ceiling record.

The runner emits `PhaseResult(status="error", feedback=<human-readable
one-liner naming the ceiling and the counter values>, log=<same as
feedback plus a short body when a reviewer raw is available>, diff=<the
current `compute_repo_diff(cwd=repo_root())`>, ceiling_hit=<"max_review_rounds"
or "max_wall_clock_sec">)`. `status="error"` is chosen (not a new status
literal) so the existing `PhaseStatus` `Literal` type does not need to
grow; the orchestrator's ceiling handler (D6) inspects `ceiling_hit` to
decide the deferral routing BEFORE the generic error handling runs.

`ceiling_hit` values are enumerated — exactly `"max_review_rounds"` or
`"max_wall_clock_sec"`.

### D6. Orchestrator routing — defer via the existing `deferred_requirements` seam

`orchestrator.process_task`'s result-handling block (near
`.redteam/workflows/orchestrator.py:1444-1622`, before the
`fallback_audit` / `staging_audit` audit-append site and before the generic
retry/backtrack accounting) gains a NEW pre-check: if
`result.get("ceiling_hit")` is set, the orchestrator:

1. Appends to `state["deferred_requirements"]` an entry with:
   - `"phase": "review_code"`
   - `"reason": f"review_code_{ceiling_hit}_exceeded"` (i.e.
     `"review_code_max_review_rounds_exceeded"` or
     `"review_code_max_wall_clock_sec_exceeded"`)
   - `"round_count": int(state.get("review_code_round_count") or 0)`
   - `"wall_clock_sec": float(state.get("review_code_wall_clock_sec") or 0.0)`
   - `"feedback": result["feedback"][:4000]`
2. Records failure via `_record_failure(state, result)` (matches the
   rescue-cycle deferral pattern at `orchestrator.py:733`).
3. Sets `state["next_phase"] = "deferred"`.
4. `save_state(task_dir, state)`.
5. Returns `"deferred"` from `process_task`.

Mirrors the rescue-cycle terminal pattern
(`orchestrator._route_to_rescue_or_defer` @ lines 730-746). The
`ceiling_hit` check runs BEFORE `manual_required`, BEFORE the
`fallback_audit` / `staging_audit` audit-append site, and BEFORE the
retries / rescue routing — so the ceiling is always the outermost
fail-closed exit, and a `ceiling_hit` result cannot be misrouted to
rescue (which would blow the budget again) or to a silent approval.

`review_audit` also receives an entry mirroring the `staging_audit` /
`fallback_audit` wiring: on a `ceiling_hit` result the orchestrator
appends `{"phase": "review_code", "reason": <ceiling_hit>}` so the
machine-readable audit trail covers ceiling terminations.

### D7. Adapter prompt-caching — NOT implementable at the CLI seam; documented no-op

Investigation of both CLI adapters:

- `adapters/codex.py::CodexReviewerAdapter` invokes
  `codex exec --sandbox read-only -` with the prompt on stdin. `codex exec`
  exposes NO documented `--cache*` / prompt-cache-control flag at the CLI
  seam.
- `adapters/claude.py::ClaudeReviewerAdapter` invokes
  `claude -p <prompt> --permission-mode plan --allowedTools ...
  --disallowedTools ... --output-format json`. The Claude CLI exposes NO
  documented CLI flag for prompt cache-control (`cache_control:
  ephemeral` is an Anthropic API-level parameter on
  `messages.create`, not a `claude -p` CLI knob). Any prompt-caching
  the Claude API applies automatically to a cacheable prefix is
  server-side and transparent to the CLI adapter.

Consequence: implementing "prompt caching of the fixed reviewer prompt
portion" at the CLI adapter layer is NOT POSSIBLE without changing the
adapter transport (e.g. switching to the Anthropic HTTP API), which is
explicitly a separate, future adapter-transport decision governed by the
"reviewer-transport" decision doc pattern
(`docs/decisions/2026-06-17-reviewer-transport-and-subagent.md`).

**Decision (fixed here):** the prompt-caching sub-bullet of `input.md`
(`input.md:31-37`) is landed as a `docs/decisions/` note that records the
"not implementable at the CLI adapter layer" determination, with a guard
rail that any future revisit go through `plan_review` on the adapter
transport. `adapters/claude.py` and `adapters/codex.py` are NOT touched
by this task. No stub, no fake caching flag, no dead argument.

## Done-when
- [ ] `.redteam/workflows/config.py` gains a frozen dataclass
      `ReviewCeilingsConfig(max_review_rounds: int | None,
      max_wall_clock_sec: int | None)` (both fields default `None`), and
      `ModelsConfig` gains an optional field `review_ceilings:
      ReviewCeilingsConfig | None = None`.
- [ ] `config.load_config` fails loud with `ValueError` on every one of
      the following inputs under `[models.review_ceilings]`: (a) unknown
      key inside the subtable; (b) subtable present but both keys absent;
      (c) `max_review_rounds` that is a `bool`, non-`int`, `0`, or
      negative; (d) `max_wall_clock_sec` that is a `bool`, non-`int`,
      `0`, or negative; (e) either key of the wrong TOML type. The error
      message shape matches the P3 `_parse_review_stages` style
      (`Unknown models.review_ceilings config key(s): [...]. Known keys:
      [...]`).
- [ ] `_parse_tiers` is UNCHANGED. `TierProfile.models` stays
      `dict[str, str]`. A `[tiers.N].models.review_ceilings` sub-table
      continues to be rejected by today's fail-loud path (unknown-role OR
      non-str value). `_KNOWN_ROLES` additionally excludes
      `"review_ceilings"` (P3 already excludes `"review_stages"`).
      Regression test locks both rejection paths.
- [ ] With `[models.review_ceilings]` absent from `.redteam/config.toml`,
      `load_config(repo_root)` returns a `RedteamConfig` whose
      `models.review_ceilings is None`, and every other `ModelsConfig`
      field is byte-identical to today's shipped defaults.
      Regression-locked.
- [ ] `.redteam/config.toml` in THIS repo is UNCHANGED — no
      `[models.review_ceilings]` subtable is added. Regression-locked by
      a test that loads this repo's own config and asserts
      `cfg.models.review_ceilings is None`.
- [ ] `.redteam/templates/state.template.json` is UNCHANGED — no new
      counter fields are added to the template. The two new counters
      (`review_code_round_count`, `review_code_wall_clock_sec`) appear in
      state.json lazily on the first opted-in `review_code.run` and are
      treated as `0` / `0.0` when absent. Regression-locked by a test
      that reads the template file bytes and asserts equality to the
      pre-change bytes (or by a field-set assertion that excludes the
      two new keys).
- [ ] `.redteam/workflows/phase_runners/_base.py::PhaseResult` gains an
      optional field `ceiling_hit: NotRequired[str]` — set ONLY by the
      runner (mirrors `staging_audit` / `fallback_audit`), never parsed
      from reviewer text.
- [ ] `phase_runners/review_code.py::run` (agent-pair branch ONLY) reads
      `review_ceilings = load_config(repo_root()).models.review_ceilings`
      at the top of the agent-pair branch. When `review_ceilings is
      None`, the runner is byte-identical to today: no counter increment,
      no `time.monotonic()` reads, no ceiling checks, no `ceiling_hit`
      field ever set, no growth of state.json. Regression-locked.
- [ ] When `review_ceilings is not None` AND
      `review_ceilings.max_review_rounds is not None`,
      `state["review_code_round_count"]` is incremented at the TOP of the
      agent-pair branch, BEFORE the pre-dispatch check. When
      `review_ceilings is None` OR `max_review_rounds is None`, the
      counter is not written and not read.
- [ ] When `review_ceilings is not None` AND
      `review_ceilings.max_wall_clock_sec is not None`, the runner wraps
      the existing dispatch (Cases B / C / D from task-001) with a
      `time.monotonic()` timer per D3 and runs the post-dispatch
      wall-clock check. When `review_ceilings is None` OR
      `max_wall_clock_sec is None`, no `time.monotonic()` reads happen
      and `state["review_code_wall_clock_sec"]` is neither written nor
      read.
- [ ] On any ceiling crossing the runner returns
      `PhaseResult(status="error", ceiling_hit=<"max_review_rounds" |
      "max_wall_clock_sec">, ...)` per D5. Legacy state without the two
      new counters is treated as `0` / `0.0`.
- [ ] The round-ceiling check is triggered on invocation
      `max_review_rounds + 1`, NOT on `max_review_rounds`. Invocations
      `1..max_review_rounds` run the reviewer normally.
- [ ] The wall-clock ceiling triggers whenever accrued time crosses the
      configured value, EITHER before dispatch (accrued >= max before
      the call) OR after dispatch (accrual pushes total >= max). On the
      after-dispatch crossing the reviewer's raw is still persisted to
      `code_review.md` (and `code_review.first_pass.md` on a promoted
      round) for the audit trail, but the `PhaseResult` is upgraded to
      `ceiling_hit="max_wall_clock_sec"` — no approval is finalized.
- [ ] There is NO code path by which either ceiling crossing produces
      `PhaseResult.status == "approved"`. Regression test locks this in
      as a hard invariant against future refactors.
- [ ] `orchestrator.process_task` gains a ceiling-handling pre-check
      that runs BEFORE `manual_required`, BEFORE the `fallback_audit` /
      `staging_audit` audit-append site, and BEFORE the retries / rescue
      routing. On `result.get("ceiling_hit")` it appends a
      `deferred_requirements` entry (with `"reason"`, `"round_count"`,
      `"wall_clock_sec"`, `"feedback"` per D6), records failure, sets
      `state["next_phase"] = "deferred"`, saves, and returns
      `"deferred"`.
- [ ] The `review_audit` list receives an entry
      `{"phase": "review_code", "reason": <ceiling_hit value>}` on a
      ceiling-terminated round (mirrors the P3 `staging_audit` wiring
      at `.redteam/workflows/orchestrator.py:1474-1479`).
- [ ] `state["review_code_round_count"]` increments on EVERY
      `review_code.run` invocation in agent-pair mode (Cases A / B / C /
      D — even Case A, so a manual-heavy task can still hit the round
      ceiling) **when the round-ceiling is configured**.
      `state["review_code_wall_clock_sec"]` is accrued ONLY on Cases
      B / C / D **when the wall-clock ceiling is configured**. Neither
      counter is reset on convergence — the budget is per-task,
      per-lifetime.
- [ ] Ceilings and P3 staging (`[models.review_stages]`) are independent:
      the loader accepts `[models.review_ceilings]` alone,
      `[models.review_stages]` alone, both, or neither. A ceiling
      triggered on a first-pass (cheap) round terminates the same way as
      one triggered on a frontier round — the "no approval under ceiling"
      invariant holds on both staging paths (regression-locked).
- [ ] `adapters/claude.py` and `adapters/codex.py` are UNCHANGED by this
      task (the D7 investigation says CLI seam has no caching control).
      Verified by a `git diff` scope assertion in the affected-files
      list.
- [ ] `(new) docs/decisions/2026-07-05-reviewer-prompt-caching.md` exists
      and records the D7 determination: CLI-seam caching investigation,
      the "not implementable at the CLI adapter layer" conclusion, and a
      guard rail that any future revisit go through `plan_review` on the
      adapter transport. No code lands from this bullet.
- [ ] `bash .redteam/scripts/verify.sh` is green (ruff check + ruff
      format --check + full pytest under `.redteam/tests/`) — including
      the new test file AND every existing test.

## Out of scope
- **Prompt caching of the fixed reviewer prompt.** Recorded as a
  documented no-op per D7. Implementing prompt caching requires a
  different adapter transport (Anthropic HTTP API instead of `claude -p`,
  or a hypothetical `codex exec --cache*` flag that does not exist);
  that transport change is a separate, later `plan_review`-gated
  decision, not this task.
- **Tier-level ceilings (`[tiers.N].models.review_ceilings`).** Ceilings
  are GLOBAL-ONLY in v1, matching the P3 global-only staging decision.
  `TierProfile.models`'s `dict[str, str]` type contract is UNCHANGED. A
  future task may revisit this.
- **Ceilings on `plan_review`, `implement`, `rescue`, `create_pr`, or
  any other phase.** This task's enforcement seam is `review_code` only,
  matching the input's "cumulative wall-clock time spent in review for a
  task" scope.
- **Changes to `review_with_fallback` / `review_with_fallback_for_provider`
  or their ladder logic.** The runner wraps the existing dispatch with
  a timer; it does NOT modify the ladder itself.
- **Changes to the P3 `[models.review_stages]` subtable, the P3
  cheap-APPROVED promotion (D3 of task-001), the P3 approval-authority
  invariant, or the P3 cross-provider guard (D8 of task-001).** They
  remain in force and every existing task-001 regression must stay green
  under this task's changes.
- **Auto-escalation, ceiling-relaxation on human intervention, or
  ceiling reset on convergence.** The ceiling is a per-task, per-lifetime
  budget — hitting it ALWAYS defers.
- **The worker / implementer side of the loop** (`implement.py`,
  `write_test.py`, `rescue.py`) — no changes.
- **The `state.get("mode") != "agent-pair"` tail of `review_code.py`**
  (sub-agent / TDD reviewer path). Ceilings, like P3 staging, are
  agent-pair only.
- **Bookkeeping when ceilings are unconfigured.** No counter fields are
  added to state.json when `models.review_ceilings is None`, and no
  fields are added to the state template. Default operation is
  byte-identical.
- **Making ceilings the default.** They stay opt-in and off in this
  repo's own `.redteam/config.toml`.
- **Changing the `PhaseStatus` `Literal`** to add a new
  `"ceiling_hit"` status. D5 reuses `"error"` + the structured
  `ceiling_hit` field.

## Affected files
- `.redteam/workflows/config.py` — add `ReviewCeilingsConfig`, extend
  `ModelsConfig.review_ceilings`, add `_parse_review_ceilings` fail-loud
  validation, extend `load_config` to pre-process the
  `[models.review_ceilings]` subtable (mirrors the P3
  `[models.review_stages]` pre-processing at lines 419-422), extend
  `_KNOWN_ROLES` to exclude `"review_ceilings"`.
- `.redteam/workflows/phase_runners/review_code.py` — at the TOP of the
  agent-pair branch of `run()`, read `models.review_ceilings` via
  `load_config(repo_root())`; if `None`, fall through unchanged (byte
  identical). Otherwise implement the D4 enforcement order: increment
  `state["review_code_round_count"]` (gated on `max_review_rounds`),
  pre-dispatch checks, wrap the existing dispatch (Cases B / C / D) with
  a `time.monotonic()` timer for wall-clock accrual (gated on
  `max_wall_clock_sec`), post-dispatch wall-clock check. Return a
  ceiling-terminal `PhaseResult` with `ceiling_hit` set on any
  crossing. `time` is a stdlib import.
- `.redteam/workflows/phase_runners/_base.py` — add
  `ceiling_hit: NotRequired[str]` to `PhaseResult` (mirrors
  `staging_audit` and `fallback_audit`).
- `.redteam/workflows/orchestrator.py` — in `process_task`'s result
  block, add a ceiling-handling pre-check that runs BEFORE the
  `manual_required` / `fallback_audit` / `staging_audit` /
  retries / rescue paths: append a `deferred_requirements` entry per
  D6, record failure, set `next_phase = "deferred"`, save, return
  `"deferred"`. Also append the `review_audit` mirror entry per D6.
- `(new) docs/decisions/2026-07-05-reviewer-prompt-caching.md` — records
  the D7 determination (CLI seam has no caching control; not
  implementable at the adapter layer without a transport change; any
  revisit goes through `plan_review`).
- `(new) .redteam/tests/test_review_code_hard_ceilings.py` — new tests
  covering the D1-D6 behaviors listed under "Verification hooks → To be
  created" below. The test-writing phase (agent-pair implementer)
  defines the exact test function names.

Explicitly NOT touched:
- `.redteam/templates/state.template.json` — no new counter fields; the
  template stays byte-identical.
- `.redteam/workflows/adapters/claude.py`,
  `.redteam/workflows/adapters/codex.py`,
  `.redteam/workflows/adapters/__init__.py`,
  `.redteam/workflows/adapters/_protocol.py` — the D7 investigation says
  the CLI seam has no caching control, so no adapter change is needed.
- `.redteam/workflows/phase_runners/implement.py`,
  `.redteam/workflows/phase_runners/write_test.py`,
  `.redteam/workflows/phase_runners/rescue.py`,
  `.redteam/workflows/phase_runners/plan_review.py`,
  `.redteam/workflows/phase_runners/plan_outcome.py`,
  `.redteam/workflows/phase_runners/verify_test.py`,
  `.redteam/workflows/phase_runners/create_pr.py`,
  `.redteam/workflows/phase_runners/decompose.py` — the worker /
  planner / other-review side is untouched.
- `.redteam/config.toml` — this repo does NOT opt into ceilings.

## Verification

```yaml
commands:
  - bash .redteam/scripts/verify.sh
```

## Verification hooks

### Existing (must continue to pass)
- `bash .redteam/scripts/verify.sh` — full suite (ruff check + ruff
  format --check + pytest under `.redteam/tests/`).
- `.redteam/tests/test_config.py` — existing config-loader discipline
  (unknown-key / bad-type rejection) must remain green under the new
  nested `[models.review_ceilings]` subtable.
- `.redteam/tests/test_tier_routing_config.py` — tier parsing behavior
  must be UNCHANGED (this task does not touch `_parse_tiers` beyond
  extending `_KNOWN_ROLES`'s exclusion set).
- `.redteam/tests/test_review_code_staged_reviewer.py` — every P3
  regression must stay green: staging + ceilings coexist without
  interference.
- `.redteam/tests/test_adversarial_pairing_guard.py` — the D8 P3 guard
  must remain green.
- `.redteam/tests/test_rescue_loop_bounded.py` — the rescue-entry
  ceiling routing must remain green (this task's ceiling is a
  DIFFERENT counter with a DIFFERENT enforcement seam, so nothing here
  changes).
- `.redteam/tests/test_state_bootstrap.py` — the state.template.json
  bootstrap must remain green with NO changes (the template file is
  UNCHANGED under this task).
- `.redteam/tests/test_review_round_archive.py`,
  `.redteam/tests/test_review_code_narrow_context.py` — the existing
  `code_review.md` / narrowed-prompt behavior must be unchanged with no
  ceilings configured.
- `.redteam/tests/test_reviewer_fallback.py`,
  `.redteam/tests/test_reviewer_adapter.py`,
  `.redteam/tests/test_claude_reviewer_adapter.py` — reviewer adapter
  and fallback-ladder contract must remain green (adapter files
  untouched per D7).
- `.redteam/tests/test_gateless_default_common_path.py` — default
  common path (no ceilings) must remain unchanged.
- `.redteam/tests/test_agents_generic_prompts.py` — agent bodies remain
  project-agnostic (this task does not touch agent prompts).

### To be created (the test-writing phase will define exact test names)
Tests under `.redteam/tests/` in a new file matching `test_*.py` (target
file: `test_review_code_hard_ceilings.py`), covering these behaviors:

- **Config parsing (happy path).** A `[models.review_ceilings]` table
  with a valid `max_review_rounds` alone loads cleanly and is reachable
  at `cfg.models.review_ceilings.max_review_rounds` (with
  `.max_wall_clock_sec is None`). Same for `max_wall_clock_sec` alone.
  Same for BOTH set.
- **Config parsing (fail-loud).** `load_config` raises `ValueError` on
  each of: unknown key inside the subtable; subtable present but empty
  (both keys absent); `max_review_rounds` set to a `bool`, `0`,
  negative int, non-int; `max_wall_clock_sec` set to a `bool`, `0`,
  negative int, non-int; each key of the wrong TOML type.
- **Tier-level ceilings are REJECTED (global-only invariant).** A
  config with `[tiers.N].models.review_ceilings = { ... }` (or a bare
  `review_ceilings` role value) fails loud at load time via the
  existing per-role rejection path; `TierProfile.models` remains
  `dict[str, str]`.
- **Default parity (no ceilings) — byte-identical.** With
  `load_config(repo_root()).models.review_ceilings is None`,
  `review_code.run` neither reads nor writes
  `state["review_code_round_count"]` or
  `state["review_code_wall_clock_sec"]`; `time.monotonic` is not called;
  no `ceiling_hit` field ever appears on the `PhaseResult`; the state
  dict before and after `run()` differs ONLY in the same keys today's
  runner already touches.
- **State-template unchanged.** A regression test asserts
  `.redteam/templates/state.template.json` does NOT contain
  `review_code_round_count` or `review_code_wall_clock_sec` keys
  (i.e. this task adds no new fields to the shipped template).
- **Legacy state without counters — round ceiling only.** With
  `max_review_rounds=N` set and `max_wall_clock_sec` absent, and a
  state.json missing both counter fields at entry: invocations
  `1..N` run the reviewer, invocation `N+1` returns
  `ceiling_hit="max_review_rounds"`; `state["review_code_wall_clock_sec"]`
  is never written.
- **Legacy state without counters — wall-clock ceiling only.** With
  `max_wall_clock_sec=T` set and `max_review_rounds` absent, and a
  state.json missing both counter fields at entry:
  `state["review_code_round_count"]` is never written; only
  `state["review_code_wall_clock_sec"]` is accrued.
- **Round-ceiling triggers on max+1.** With
  `max_review_rounds == N` and no wall-clock ceiling: invocations
  `1..N` of `review_code.run` run the reviewer and return normally;
  invocation `N+1` returns without invoking the reviewer, with
  `ceiling_hit="max_review_rounds"` on the `PhaseResult` and
  `status == "error"`.
- **Round-ceiling routes to defer, not rescue.** After the max+1
  invocation, `orchestrator.process_task` appends a
  `deferred_requirements` entry with
  `reason="review_code_max_review_rounds_exceeded"`, sets
  `next_phase="deferred"`, and returns `"deferred"`. No rescue entry,
  no retries increment, no approval.
- **Wall-clock ceiling — pre-dispatch skip.** With
  `state["review_code_wall_clock_sec"] >= max_wall_clock_sec` at
  `run()` entry: the reviewer is NOT invoked; the runner returns
  `ceiling_hit="max_wall_clock_sec"`.
- **Wall-clock ceiling — post-dispatch upgrade.** With
  `state["review_code_wall_clock_sec"] < max_wall_clock_sec` at entry
  and a mocked `time.monotonic()` sequence that makes the accrual push
  the total to `>= max_wall_clock_sec`: the reviewer IS invoked; its
  raw is persisted to `code_review.md`; the runner returns
  `ceiling_hit="max_wall_clock_sec"` with `status == "error"`
  REGARDLESS of the reviewer's verdict — specifically including the
  case where the reviewer returned `APPROVED` (this test is the
  approval-authority invariant regression). The reviewer's raw is
  preserved for audit; only the `PhaseResult` is upgraded.
- **Wall-clock ceiling routes to defer.** After the post-dispatch
  upgrade, the orchestrator appends
  `deferred_requirements` with
  `reason="review_code_max_wall_clock_sec_exceeded"` and
  `next_phase="deferred"`.
- **Approval-authority invariant (hard).** A regression test asserts
  that no code path in `review_code.run` allows `ceiling_hit` to be
  set AND `PhaseResult.status == "approved"` in the SAME return value.
  Written against the runner's return, not incidental intermediate
  state, so a future control-flow refactor cannot smuggle an approval
  past the ceiling.
- **Wall-clock accrual is monotonic and cumulative (when configured).**
  With `max_wall_clock_sec` set but never crossed: after N successive
  `review_code.run` invocations, the state's
  `review_code_wall_clock_sec` equals the sum of the N mocked `dt`s.
  Legacy state without the field starts from `0.0`.
- **Case A (manual) accrues NO wall-clock.** With staging OFF,
  `state["models"]["reviewer"] == "human"`, and a wall-clock ceiling
  configured: the manual branch runs, `time.monotonic()` is NOT
  measured around a reviewer call, and
  `state["review_code_wall_clock_sec"]` is unchanged. The round
  counter DOES still increment (round-ceiling configured).
- **Case A can still hit the round ceiling.** With staging OFF,
  manual reviewer, and `max_review_rounds == 1`: the second `run()`
  call returns the round ceiling BEFORE reading `code_review.md`, so
  the manual sentinel is never re-consumed under a burnt budget.
- **Ceilings + P3 staging interoperate.** With `[models.review_stages]`
  AND `[models.review_ceilings]` both configured:
  (a) a cheap first-pass round counts toward `max_review_rounds`
      (the counter increments regardless of stage);
  (b) a promoted round (cheap-APPROVED → frontier per task-001 D3)
      measures COMBINED cheap+frontier time as one `dt` (not two);
  (c) a ceiling triggered on a promoted round terminates
      non-approved (the invariant holds on the promoted path too).
- **Legacy state (missing counters) treated as zero — with ceilings on.**
  A state.json without `review_code_round_count` /
  `review_code_wall_clock_sec` starts at `0` / `0.0` on the first
  `run()` invocation under an opted-in config.
- **Persistence across resumes.** After a `run()` invocation that
  accrues wall-clock and increments the counter (with ceilings
  configured), the saved state.json contains the updated values, and a
  subsequent load-and-run transparently resumes the counter (i.e. the
  ceiling budget is not reset by a `resume`).
- **Convergence does NOT reset ceiling counters.** After a `run()`
  that returns `approved` and the orchestrator moves to `create_pr`
  (with ceilings configured), `state["review_code_round_count"]` and
  `state["review_code_wall_clock_sec"]` are UNCHANGED (contrast:
  `rescue_entry_count` is explicitly reset on the same convergence
  path at `orchestrator.py:1526`).
- **Cross-provider guard interaction unchanged.** The P3 D8 first-pass
  same-provider guard fires or does not fire independently of any
  ceiling configuration.
- **Ceiling record shape in `deferred_requirements`.** Each ceiling
  deferral entry carries the fields listed in D6:
  `"phase"=="review_code"`, `"reason"` matches the enumerated string,
  `"round_count"` matches `int(state.get("review_code_round_count") or 0)`
  at termination, `"wall_clock_sec"` matches
  `float(state.get("review_code_wall_clock_sec") or 0.0)` at
  termination, `"feedback"` present.
- **`review_audit` receives the ceiling reason.** After a
  ceiling-terminated round, the orchestrator's `review_audit` list
  contains an entry `{"phase": "review_code", "reason": <ceiling_hit
  value>}`, appended at the same site as the P3 `staging_audit`
  wiring.
- **Adapter files unchanged.** A structural test (or a
  file-content-hash check equivalent) asserts that
  `.redteam/workflows/adapters/claude.py` and
  `.redteam/workflows/adapters/codex.py` contain no
  cache-control / caching-flag code — the D7 investigation outcome is
  reflected by their being untouched.
- **`docs/decisions/2026-07-05-reviewer-prompt-caching.md` exists.** A
  test asserts the file exists and is non-empty, so the decision doc
  cannot silently regress.
- **Dogfood-config assertion.** Loading this repo's own
  `.redteam/config.toml` yields `cfg.models.review_ceilings is None`.

Test scaffolding restrictions (mirroring sibling tests in
`.redteam/tests/`): monkeypatch `time.monotonic` in
`phase_runners.review_code`, monkeypatch `review_with_fallback` and
`review_with_fallback_for_provider`, monkeypatch `_REVIEWER_ADAPTERS`,
`compute_repo_diff`, `repo_root`, and `git_rev_parse` as needed. Do NOT
spawn `codex` / `claude` subprocesses and do NOT touch a real remote.

## Risks
- **P3 outcome suggested nesting P5 keys INSIDE `[models.review_stages]`.**
  Task-001's outcome (in its Out of scope section) said "P5 can add keys
  like `max_review_rounds` alongside without another restructure",
  implying the same subtable. This outcome instead uses a SIBLING
  `[models.review_ceilings]` subtable because (a) `[models.review_stages]`
  in the shipped task-001 code has `_parse_review_stages` reject unknown
  keys (so extending it in-place would still be a restructure), and
  (b) ceilings apply independently of staging (a task can opt into
  ceilings without opting into staging). If the human at the plan gate
  prefers the same-subtable shape, the fix is mechanical (rename the
  dataclass and shift key parsing) but changes the loader test surface;
  surface this at `plan_review` if it matters.
- **Post-dispatch wall-clock upgrade discards a legitimate APPROVED.**
  A frontier reviewer's APPROVED emitted on the round that crosses
  the wall-clock ceiling is discarded (per D4 step 6 and the input's
  "no silent approvals under time pressure" rule). This is the input's
  explicit stance, but a real operator will occasionally see a task
  that "almost" approved defer to the human. The `code_review.md`
  raw is preserved so a human can quickly re-approve via the draft-PR
  checkpoint — but the automation refuses to. Confirm this is the
  intended tradeoff at the plan gate.
- **`time.monotonic()` accrual granularity.** Accrual measures only the
  time inside the reviewer call(s); it does NOT include the time the
  runner spends building the prompt or writing files. A task with
  extreme prompt-build overhead could underestimate its "wall clock
  spent in review". This matches the input's "cumulative wall-clock
  time spent in review" scope but is worth flagging.
- **Bookkeeping increments the counter on Case A (manual).** When the
  round ceiling is configured, the manual reviewer path always
  increments `review_code_round_count`. If an operator uses the manual
  reviewer + a tight `max_review_rounds`, a human pasting a review
  still consumes a round. The alternative — not counting Case A
  rounds — is unsafe (would let a manual-then-headless flip evade the
  ceiling). This design counts every invocation; confirm at the plan
  gate.
- **Prompt caching is landed as a `docs/decisions/` no-op.** The D7
  determination is that CLI-seam caching is not implementable. If a
  future `claude -p` or `codex exec` release adds a CLI cache flag,
  the decision must be revisited via a new plan_review round. This
  outcome does NOT pre-commit that revisit.
- **Adapter-identifier lazy-import cycle.** `_parse_review_ceilings`
  has no adapter dependency (it validates `int` shapes only), unlike
  `_parse_review_stages` which lazy-imports `_REVIEWER_ADAPTERS`. So
  no cycle risk here; flagged for reviewer awareness only.
- **`PhaseStatus` `Literal` was NOT extended.** D5 chose to reuse
  `"error"` + a structured `ceiling_hit` field rather than add a new
  literal value, keeping the `PhaseStatus` type stable across the
  codebase. The orchestrator's ceiling handler must run BEFORE the
  generic `"error"` handling — a mis-ordering would treat a ceiling
  as a normal error and trigger the retries/rescue path (blowing the
  budget). The test surface locks the ordering via the "routes to
  defer, not rescue" tests, but flagged for reviewer awareness.
- **Mid-task opt-in / opt-out semantics.** Because counters are gated
  on the live `models.review_ceilings` read, an operator who edits
  `.redteam/config.toml` mid-task can toggle bookkeeping. Turning ON
  mid-task starts the counter at `0` (past rounds are NOT retroactively
  counted). Turning OFF mid-task strands existing counter values in
  state.json — they are ignored until the operator turns ceilings
  back on. Both behaviors are intentional but worth surfacing at
  plan_review.
